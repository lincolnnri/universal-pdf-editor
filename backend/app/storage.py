"""Sessões de edição isoladas.

Cada documento enviado ganha um diretório temporário próprio (UUID), acessível somente por
esse identificador. Os arquivos são apagados ao fechar o documento, ao expirar o tempo de
inatividade configurado, ou na inicialização do servidor.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from PIL import Image

from .config import settings
from . import workers
from .engine.analysis import PageAnalysis


ID_RE = re.compile(r"^[0-9a-f]{32}$")


class SessionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class DocSession:
    id: str
    dir: Path
    filename: str
    page_count: int
    password: str | None = None
    created: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)
    analyses: dict[int, PageAnalysis] = field(default_factory=dict)
    signed: bool = False
    encrypted: bool = False
    exports: dict[str, Path] = field(default_factory=dict)
    pages: list[dict] = field(default_factory=list)

    @property
    def images_dir(self) -> Path:
        return self.dir / "images"

    def render(self, pno: int, scale: float) -> bytes:
        """Renderização da página original, com cache em disco."""
        if pno < 0 or pno >= self.page_count:
            raise SessionError(404, "page_not_found", "Página inexistente.")
        cache = self.dir / "cache" / f"{pno}_{scale:.3f}.png"
        if cache.exists():
            return cache.read_bytes()
        data = workers.run(workers.task_render, str(self.original_path), self.password, pno, scale)
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(data)
        return data

    @property
    def original_path(self) -> Path:
        return self.dir / "original.pdf"

    def original_bytes(self) -> bytes:
        return self.original_path.read_bytes()

    def open(self) -> pymupdf.Document:
        doc = pymupdf.open(self.original_path)
        if doc.needs_pass:
            doc.authenticate(self.password or "")
        return doc

    def touch(self) -> None:
        self.last_access = time.time()

    # --------------------------------------------------------------- análise
    def analysis(self, pno: int, ocr: str = "auto") -> tuple[PageAnalysis, dict]:
        if pno < 0 or pno >= self.page_count:
            raise SessionError(404, "page_not_found", f"Página {pno + 1} não existe.")
        existing = self.analyses.get(pno)
        a, js = workers.run(workers.task_analyze, str(self.original_path), self.password, pno, ocr, existing)
        self.analyses[pno] = a
        return a, js

    def ensure_analyses_for(self, ops: list[dict]) -> dict[int, PageAnalysis]:
        for op in ops:
            pno = int(op.get("page", -1))
            if pno < 0 or pno >= self.page_count:
                raise SessionError(400, "invalid_op", "Operação refere-se a uma página inexistente.")
            eid = str(op.get("elementId") or "")
            need_ocr = "-o" in eid
            a = self.analyses.get(pno) or self.analysis(pno, ocr="off")[0]
            if need_ocr and not a.ocr_done:
                self.analysis(pno, ocr="force")
        return self.analyses

    # --------------------------------------------------------------- imagens
    def save_image(self, data: bytes) -> dict:
        if len(data) > settings.max_image_bytes:
            raise SessionError(413, "image_too_large", f"Imagem maior que {settings.max_image_mb} MB.")
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
            with Image.open(io.BytesIO(data)) as im:
                fmt = (im.format or "").upper()
                w, h = im.size
                if fmt not in ("PNG", "JPEG"):
                    out = io.BytesIO()
                    im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(out, "PNG")
                    data = out.getvalue()
                    fmt = "PNG"
        except Exception as exc:
            raise SessionError(400, "invalid_image", "Arquivo de imagem inválido ou não suportado.") from exc
        image_id = uuid.uuid4().hex
        (self.dir / "images").mkdir(exist_ok=True)
        (self.dir / "images" / image_id).write_bytes(data)
        return {"imageId": image_id, "width": w, "height": h}

    def load_image(self, image_id: str) -> bytes:
        if not ID_RE.match(image_id or ""):
            raise SessionError(400, "invalid_image", "Identificador de imagem inválido.")
        p = self.dir / "images" / image_id
        if not p.exists():
            raise SessionError(404, "image_not_found", "Imagem não encontrada nesta sessão.")
        return p.read_bytes()


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, DocSession] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._janitor: threading.Thread | None = None

    # --------------------------------------------------------------- ciclo de vida
    def start(self) -> None:
        # remove sobras de execuções anteriores
        for child in self.root.iterdir():
            if child.is_dir() and ID_RE.match(child.name):
                shutil.rmtree(child, ignore_errors=True)
        self._janitor = threading.Thread(target=self._loop, daemon=True, name="session-janitor")
        self._janitor.start()

    def stop(self) -> None:
        self._stop.set()
        for sid in list(self._sessions):
            self.delete(sid)

    def _loop(self) -> None:
        while not self._stop.wait(60):
            self.cleanup_expired()

    def cleanup_expired(self) -> int:
        ttl = settings.session_ttl_minutes * 60
        now = time.time()
        expired = [s.id for s in list(self._sessions.values()) if now - s.last_access > ttl]
        for sid in expired:
            self.delete(sid)
        return len(expired)

    # --------------------------------------------------------------- operações
    def create(self, data: bytes, filename: str, password: str | None) -> DocSession:
        validate_pdf_bytes(data)
        sid = uuid.uuid4().hex
        d = self.root / sid
        d.mkdir(parents=True)
        try:
            (d / "original.pdf").write_bytes(data)
            info = workers.run(workers.task_inspect, str(d / "original.pdf"), password)
            if not info.get("ok"):
                raise SessionError(400, "invalid_pdf", "O arquivo não pôde ser aberto como PDF.")
            if info.get("code") == "password_required":
                raise SessionError(401, "password_required",
                                   "Este PDF é protegido por senha. Informe a senha para abri-lo.")
            if info.get("code") == "wrong_password":
                raise SessionError(401, "wrong_password", "Senha incorreta.")
            encrypted = info["needs_pass"] or info["encrypted"]
            page_count = info["page_count"]
            if page_count == 0:
                raise SessionError(400, "empty_pdf", "O PDF não contém páginas.")
            if page_count > settings.max_pages:
                raise SessionError(413, "too_many_pages",
                                   f"O PDF tem {page_count} páginas; o limite é {settings.max_pages}.")
            perms = info["perms"]
            if encrypted and perms and not (perms & pymupdf.PDF_PERM_MODIFY) and not info.get("owner"):
                raise SessionError(403, "edit_not_permitted",
                                   "O autor deste PDF não permite modificações. Abra-o com a senha de "
                                   "proprietário para editar.")
            signed = info["signed"]
            pages = info["pages"]
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        safe_name = re.sub(r"[^\w\-. ]+", "_", Path(filename or "documento.pdf").name)[:120] or "documento.pdf"
        s = DocSession(id=sid, dir=d, filename=safe_name, page_count=page_count, password=password,
                       signed=signed, encrypted=encrypted, pages=pages)
        (d / "meta.json").write_text(json.dumps({"filename": safe_name, "created": s.created}))
        with self._lock:
            self._sessions[sid] = s
        return s

    def get(self, sid: str) -> DocSession:
        if not ID_RE.match(sid or ""):
            raise SessionError(404, "not_found", "Documento não encontrado.")
        s = self._sessions.get(sid)
        if s is None or not s.dir.exists():
            raise SessionError(404, "not_found", "Documento não encontrado ou sessão expirada. Carregue o PDF novamente.")
        s.touch()
        return s

    def delete(self, sid: str) -> bool:
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s is None:
            return False
        shutil.rmtree(s.dir, ignore_errors=True)
        return True


def validate_pdf_bytes(data: bytes) -> None:
    if not data:
        raise SessionError(400, "empty_file", "Arquivo vazio.")
    if len(data) > settings.max_upload_bytes:
        raise SessionError(413, "file_too_large", f"Arquivo maior que o limite de {settings.max_upload_mb} MB.")
    if b"%PDF-" not in data[:1024]:
        raise SessionError(400, "not_a_pdf", "O arquivo enviado não é um PDF válido.")


store = SessionStore(settings.work_dir)
