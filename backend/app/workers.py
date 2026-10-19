"""Processamento isolado em processos separados.

Todo o trabalho pesado com PDFs (análise, OCR, prévia, exportação, renderização) roda em um
pool de processos:
  * vários documentos são processados em paralelo (o MuPDF não é thread-safe, mas processos sim);
  * um PDF malicioso ou corrompido que derrube o MuPDF derruba apenas o processo auxiliar —
    o servidor continua no ar e o pool é recriado;
  * cada tarefa tem tempo limite.

Com UPE_WORKERS=0 o processamento roda no próprio processo (útil em testes e depuração).
"""
from __future__ import annotations

import multiprocessing as mp
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

from .config import settings

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_inline_lock = threading.RLock()


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _get_pool() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=settings.workers, mp_context=mp.get_context("spawn"),
                                        initializer=_init_worker)
        return _pool


def _reset_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


def shutdown() -> None:
    _reset_pool()


def _init_worker() -> None:
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)


def run(fn, *args, timeout: float | None = None):
    """Executa `fn(*args)` em um processo auxiliar (ou inline com UPE_WORKERS=0)."""
    timeout = timeout or settings.task_timeout_s
    if settings.workers <= 0:
        with _inline_lock:
            return fn(*args)
    for attempt in range(2):
        pool = _get_pool()
        try:
            fut = pool.submit(fn, *args)
            return fut.result(timeout=timeout)
        except FutureTimeout as exc:
            # a tarefa travou: recria o pool para liberar o processo preso
            _reset_pool()
            raise WorkerError("timeout", "O processamento excedeu o tempo limite para este documento.") from exc
        except BrokenProcessPool as exc:
            _reset_pool()
            if attempt == 0:
                continue
            raise WorkerError("worker_crashed",
                              "O processamento deste PDF falhou de forma inesperada (arquivo possivelmente "
                              "corrompido ou com estrutura não suportada).") from exc
    raise WorkerError("worker_crashed", "Falha no processamento.")


# ---------------------------------------------------------------------------- tarefas
# (funções de módulo, para serem serializáveis no modo spawn)

def _open(path: str, password: str | None):
    import pymupdf

    doc = pymupdf.open(path)
    if doc.needs_pass:
        doc.authenticate(password or "")
    return doc


def task_inspect(path: str, password: str | None) -> dict:
    """Validação inicial do PDF (no processo isolado)."""
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception:
        return {"ok": False, "code": "invalid_pdf"}
    try:
        info: dict = {"ok": True, "needs_pass": bool(doc.needs_pass), "encrypted": bool(doc.is_encrypted)}
        if doc.needs_pass:
            if not password:
                info["code"] = "password_required"
                return info
            rc = doc.authenticate(password)
            if not rc:
                info["code"] = "wrong_password"
                return info
            info["owner"] = rc in (4, 6)
        else:
            info["owner"] = bool(password) and doc.authenticate(password) in (4, 6)
        info["page_count"] = doc.page_count
        info["perms"] = doc.permissions
        signed = False
        try:
            signed = doc.get_sigflags() > 0
        except Exception:
            pass
        if not signed:
            try:
                for page in doc:
                    if any(w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE for w in (page.widgets() or [])):
                        signed = True
                        break
            except Exception:
                pass
        info["signed"] = signed
        info["pages"] = [{"number": p.number, "width": round(p.rect.width, 2), "height": round(p.rect.height, 2),
                          "rotation": p.rotation} for p in doc]
        return info
    finally:
        doc.close()


def task_page_sizes(path: str, password: str | None) -> list[dict]:
    doc = _open(path, password)
    try:
        return [{"number": p.number, "width": round(p.rect.width, 2), "height": round(p.rect.height, 2),
                 "rotation": p.rotation} for p in doc]
    finally:
        doc.close()


def task_analyze(path: str, password: str | None, pno: int, ocr: str, existing):
    from .engine.analysis import analysis_to_json, analyze_page
    from .engine.ocr import ocr_page, ocr_regions

    doc = _open(path, password)
    try:
        page = doc[pno]
        a = existing if existing is not None else analyze_page(page)
        if (ocr == "force" and not a.ocr_done) or (ocr == "auto" and a.needs_ocr):
            ocr_page(page, a, exclude_native=True)
        if ocr != "off":
            ocr_regions(page, a)
        return a, analysis_to_json(page, a)
    finally:
        doc.close()


def task_render(path: str, password: str | None, pno: int, scale: float) -> bytes:
    import pymupdf

    doc = _open(path, password)
    try:
        return doc[pno].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
    finally:
        doc.close()


def _loader(images_dir: str):
    def load(image_id: str) -> bytes:
        import re

        if not re.fullmatch(r"[0-9a-f]{32}", image_id or ""):
            from .engine.editor import OpError

            raise OpError("Identificador de imagem inválido.")
        p = Path(images_dir) / image_id
        if not p.exists():
            from .engine.editor import OpError

            raise OpError("Imagem não encontrada nesta sessão.")
        return p.read_bytes()

    return load


def task_preview(path: str, password: str | None, analyses: dict, ops: list[dict], pno: int, scale: float,
                 images_dir: str) -> bytes:
    import pymupdf

    from .engine.editor import Editor

    doc = _open(path, password)
    try:
        Editor(doc, analyses, _loader(images_dir)).apply(ops, pages={pno})
        return doc[pno].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
    finally:
        doc.close()


def task_export(path: str, password: str | None, analyses: dict, ops: list[dict], images_dir: str,
                pages: list[int] | None = None):
    from .engine.verify import export_with_verification

    data, report = export_with_verification(Path(path).read_bytes(), analyses, ops, _loader(images_dir), password,
                                            pages)
    return data, report.to_json()
