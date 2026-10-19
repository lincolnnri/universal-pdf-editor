"""Geração e verificação do PDF final.

Nunca entregamos um arquivo sem antes:
  1. reabrir o PDF gerado com dois leitores independentes (MuPDF e qpdf/pikepdf);
  2. confirmar que cada alteração solicitada está presente no arquivo exportado;
  3. confirmar que o texto removido/substituído não está mais lá;
  4. comparar a renderização das páginas: páginas não editadas devem ser idênticas
     e, nas editadas, nada fora das regiões alteradas pode mudar.
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pikepdf
import pymupdf

from ..config import settings
from .analysis import PageAnalysis
from .editor import Editor, OpResult, norm

VERIFY_DPI = 60


@dataclass
class ExportReport:
    ok: bool
    checks: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ops: list[dict] = field(default_factory=list)
    pages: int = 0
    size_bytes: int = 0
    duration_ms: int = 0

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            self.errors.append(f"{name}: {detail}" if detail else name)

    def to_json(self) -> dict:
        return {"ok": self.ok, "checks": self.checks, "errors": self.errors, "warnings": self.warnings,
                "ops": self.ops, "pages": self.pages, "sizeBytes": self.size_bytes, "durationMs": self.duration_ms}


def _render_gray(page: pymupdf.Page, dpi: int = VERIFY_DPI) -> np.ndarray:
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    try:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False, annots=True)
    finally:
        if rot:
            page.set_rotation(rot)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.int16)


def _changed_fraction(a: np.ndarray, b: np.ndarray, mask: np.ndarray | None = None) -> float:
    if a.shape != b.shape:
        return 1.0
    diff = np.abs(a - b) > 40
    if mask is not None:
        diff &= ~mask
        total = max(1, int((~mask).sum()))
    else:
        total = diff.size
    return float(diff.sum()) / total


def _mask_for(shape: tuple[int, int], rects: list[tuple], dpi: int, margin_pt: float = 2.5) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    s = dpi / 72.0
    for r in rects:
        x0, y0, x1, y1 = r
        a0 = max(0, int((min(x0, x1) - margin_pt) * s))
        b0 = max(0, int((min(y0, y1) - margin_pt) * s))
        a1 = min(shape[1], int(np.ceil((max(x0, x1) + margin_pt) * s)))
        b1 = min(shape[0], int(np.ceil((max(y0, y1) + margin_pt) * s)))
        mask[b0:b1, a0:a1] = True
    return mask


def _set_metadata(doc: pymupdf.Document) -> None:
    meta = dict(doc.metadata or {})
    now = time.strftime("D:%Y%m%d%H%M%S+00'00'", time.gmtime())
    producer = meta.get("producer") or ""
    if "Universal PDF Editor" not in producer:
        producer = (producer + " + " if producer else "") + "Universal PDF Editor"
    clean = {k: v for k, v in meta.items() if k in ("title", "author", "subject", "keywords", "creator", "creationDate")}
    clean.update({"producer": producer, "modDate": now})
    try:
        doc.set_metadata(clean)
    except Exception:
        pass


def build_document(original: bytes, analyses: dict[int, PageAnalysis], ops: list[dict],
                   image_loader: Callable[[str], bytes] | None, password: str | None = None,
                   forbid_embedded: set[int] | None = None) -> tuple[bytes, list[OpResult]]:
    doc = pymupdf.open(stream=original, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(password or ""):
        raise PermissionError("Senha necessária para abrir o documento.")
    was_encrypted = doc.is_encrypted or bool(password)
    editor = Editor(doc, analyses, image_loader)
    results = editor.apply(ops, forbid_embedded=forbid_embedded)
    _set_metadata(doc)
    try:
        doc.subset_fonts()
    except Exception:
        pass
    buf = io.BytesIO()
    save_kwargs = {"garbage": 3, "deflate": True}
    if was_encrypted:
        save_kwargs["encryption"] = pymupdf.PDF_ENCRYPT_KEEP
    doc.save(buf, **save_kwargs)
    doc.close()
    return buf.getvalue(), results


def verify_output(original: bytes, output: bytes, results: list[OpResult], password: str | None = None,
                  report: ExportReport | None = None,
                  only_pages: set[int] | None = None) -> tuple[ExportReport, set[int]]:
    """Verifica o arquivo exportado. Retorna o relatório e os índices de operações cujo texto não
    pôde ser confirmado (candidatas a nova tentativa sem a fonte incorporada)."""
    rep = report or ExportReport(ok=False)
    failed_text_ops: set[int] = set()

    # 1. Integridade estrutural (dois parsers independentes)
    try:
        with pikepdf.open(io.BytesIO(output), password=password or "") as pdf:
            checker = getattr(pdf, "check_pdf_syntax", None) or getattr(pdf, "check")
            problems = [p for p in checker() if "warning" not in p.lower()]
            rep.add("Estrutura PDF válida (qpdf)", not problems, "; ".join(problems[:3]))
    except Exception as exc:  # pragma: no cover - depende do arquivo
        rep.add("Estrutura PDF válida (qpdf)", False, str(exc))

    try:
        out = pymupdf.open(stream=output, filetype="pdf")
        if out.needs_pass:
            out.authenticate(password or "")
        orig = pymupdf.open(stream=original, filetype="pdf")
        if orig.needs_pass:
            orig.authenticate(password or "")
    except Exception as exc:
        rep.add("Reabrir PDF gerado", False, str(exc))
        rep.ok = False
        return rep, failed_text_ops
    rep.add("Reabrir PDF gerado (MuPDF)", True, f"{out.page_count} página(s)")
    rep.add("Número de páginas preservado", out.page_count == orig.page_count,
            f"original {orig.page_count}, gerado {out.page_count}")
    rep.pages = out.page_count
    if out.page_count != orig.page_count:
        rep.ok = False
        return rep, failed_text_ops

    # 2/3. Cada alteração presente no arquivo
    for r in results:
        p = out[r.page]
        label = f"Alteração {r.index + 1} (pág. {r.page + 1})"
        if r.expected_lines:
            clip = pymupdf.Rect(r.check_rect) if r.check_rect else None
            if clip is not None:
                clip = pymupdf.Rect(clip.x0 - 3, clip.y0 - 3, clip.x1 + 3, clip.y1 + 3)
            found = norm(_unrot_text(p, clip))
            if r.rtl:
                # texto da direita para a esquerda: a extração pode devolver formas contextuais
                # e ordem visual; compara o conjunto de caracteres
                missing = [ln for ln in r.expected_lines if _char_overlap(norm(ln), found) < 0.8]
            else:
                missing = [ln for ln in r.expected_lines if norm(ln) not in found]
            ok = not missing
            rep.add(f"{label}: texto novo presente", ok,
                    "" if ok else "não encontrado: " + ", ".join(repr(m) for m in missing))
            if not ok:
                failed_text_ops.add(r.index)
        if r.removed_text and r.mode == "native" and r.type != "add_text":
            area = pymupdf.Rect(r.affected[0]) if r.affected else None
            if area is not None:
                inner = pymupdf.Rect(area.x0 + 0.5, area.y0 + 0.2 * (area.height), area.x1 - 0.5,
                                     area.y1 - 0.2 * area.height) if area.height > 4 else area
                leftover = norm(_unrot_text(p, inner))
                still = norm(r.removed_text) in leftover and not any(
                    norm(r.removed_text) in norm(ln) for ln in r.expected_lines)
                rep.add(f"{label}: texto antigo removido", not still,
                        "" if not still else f"{r.removed_text!r} ainda presente")
        if r.type == "add_image" and r.image_rect:
            target = pymupdf.Rect(r.image_rect)
            found = any(_close(pymupdf.Rect(i["bbox"]), target) for i in _unrot_images(p))
            rep.add(f"{label}: imagem inserida", found)
        if r.image_removed and r.image_rect:
            target = pymupdf.Rect(r.image_rect)
            still = any(_close(pymupdf.Rect(i["bbox"]), target) for i in _unrot_images(p))
            rep.add(f"{label}: imagem removida", not still, "" if not still else "imagem ainda presente")
        if r.field_xref is not None:
            # os números de objeto mudam ao salvar: localiza pelo nome e posição
            target = pymupdf.Rect(r.check_rect) if r.check_rect else None
            w = next((w for w in (p.widgets() or []) if w.field_name == r.field_name
                      and (target is None or _close(pymupdf.Rect(w.rect), target))), None)
            got = None if w is None else w.field_value
            if r.field_value == "on":
                ok = got not in (None, "Off", False, "")
            elif r.field_value == "Off":
                ok = got in ("Off", False, "", None) and w is not None
            else:
                ok = w is not None and str(got) == r.field_value
            rep.add(f"{label}: campo de formulário preenchido", ok, "" if ok else f"valor gravado: {got!r}")
        rep.warnings.extend(f"Pág. {r.page + 1}: {w}" for w in r.warnings)
        rep.ops.append(r.to_json())

    # 4. Comparação visual
    edited_pages: dict[int, list[tuple]] = {}
    for r in results:
        edited_pages.setdefault(r.page, []).extend(r.affected)
    unchanged_bad = []
    checked = 0
    for pno in range(orig.page_count):
        if only_pages is not None and pno not in only_pages:
            continue   # página não incluída no arquivo final
        checked += 1
        a = _render_gray(orig[pno])
        b = _render_gray(out[pno])
        if pno in edited_pages:
            mask = _mask_for(a.shape, edited_pages[pno], VERIFY_DPI)
            frac = _changed_fraction(a, b, mask)
            ok = frac <= settings.verify_outside_tolerance
            rep.add(f"Pág. {pno + 1}: conteúdo fora das áreas editadas preservado", ok,
                    f"{frac * 100:.3f}% dos pixels alterados fora das edições")
            # a página editada precisa realmente ter mudado
            changed = _changed_fraction(a, b) > 0 or a.shape != b.shape
            rep.add(f"Pág. {pno + 1}: alterações visíveis na página", changed,
                    "" if changed else "a renderização é idêntica ao original")
        else:
            frac = _changed_fraction(a, b)
            if frac > settings.verify_outside_tolerance:
                unchanged_bad.append(f"{pno + 1} ({frac * 100:.2f}%)")
    rep.add("Páginas não editadas idênticas ao original", not unchanged_bad,
            "alteradas: " + ", ".join(unchanged_bad) if unchanged_bad else
            f"{checked - len(edited_pages)} página(s) conferida(s)")
    rep.ok = not rep.errors
    rep.size_bytes = len(output)
    out.close()
    orig.close()
    return rep, failed_text_ops


def _unrot_text(page: pymupdf.Page, clip: pymupdf.Rect | None) -> str:
    """Extrai texto de um recorte em coordenadas sem rotação."""
    words = page.get_text("words")
    if clip is None:
        return " ".join(w[4] for w in words)
    sel = [w for w in words if pymupdf.Rect(w[:4]).intersects(clip)
           and abs(pymupdf.Rect(w[:4]) & clip) >= 0.5 * max(abs(pymupdf.Rect(w[:4])), 1e-6)]
    return " ".join(w[4] for w in sel)


def _char_overlap(expected: str, found: str) -> float:
    from collections import Counter

    e, f = Counter(expected), Counter(found)
    if not e:
        return 1.0
    return sum(min(n, f[c]) for c, n in e.items()) / sum(e.values())


def _unrot_images(page: pymupdf.Page) -> list[dict]:
    try:
        return page.get_image_info()
    except Exception:
        return []


def _close(a: pymupdf.Rect, b: pymupdf.Rect, tol: float = 1.5) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def export_with_verification(original: bytes, analyses: dict[int, PageAnalysis], ops: list[dict],
                             image_loader: Callable[[str], bytes] | None,
                             password: str | None = None,
                             pages: list[int] | None = None) -> tuple[bytes | None, ExportReport]:
    """Gera, verifica e (opcionalmente) recorta o PDF às páginas escolhidas, verificando o recorte."""
    t0 = time.time()
    only = set(pages) if pages is not None else None
    if only is not None:
        ops = [o for o in ops if int(o.get("page", -1)) in only]   # edições em páginas descartadas não importam
    data, results = build_document(original, analyses, ops, image_loader, password)
    rep, failed = verify_output(original, data, results, password, only_pages=only)
    if not rep.ok and failed:
        # Nova tentativa: operações cujo texto não foi confirmado deixam de usar a fonte incorporada
        retry_rep = ExportReport(ok=False)
        retry_rep.warnings.append(
            "Algumas edições não puderam usar a fonte incorporada original; usada fonte equivalente.")
        data, results = build_document(original, analyses, ops, image_loader, password, forbid_embedded=failed)
        rep, _ = verify_output(original, data, results, password, retry_rep, only_pages=only)
    if rep.ok and pages is not None:
        data = _extract_pages(data, pages, password, rep)
        rep.ok = not rep.errors
    rep.duration_ms = int((time.time() - t0) * 1000)
    return (data if rep.ok else None), rep


def _extract_pages(full: bytes, pages: list[int], password: str | None, rep: ExportReport) -> bytes | None:
    """Mantém só as páginas escolhidas e confere cada uma contra o documento completo já verificado."""
    src = pymupdf.open(stream=full, filetype="pdf")
    if src.needs_pass:
        src.authenticate(password or "")
    if not pages or any(p < 0 or p >= src.page_count for p in pages):
        rep.add("Páginas selecionadas válidas", False, f"o documento tem {src.page_count} página(s)")
        return None
    if list(pages) == list(range(src.page_count)):
        rep.add("Páginas selecionadas", True, "todas as páginas")
        return full
    sub = pymupdf.open(stream=full, filetype="pdf")
    if sub.needs_pass:
        sub.authenticate(password or "")
    was_encrypted = sub.is_encrypted or bool(password)
    sub.select(list(pages))
    buf = io.BytesIO()
    kw = {"garbage": 3, "deflate": True}
    if was_encrypted:
        kw["encryption"] = pymupdf.PDF_ENCRYPT_KEEP
    sub.save(buf, **kw)
    sub.close()
    out = buf.getvalue()
    # conferência independente do arquivo recortado
    try:
        with pikepdf.open(io.BytesIO(out), password=password or "") as pdf:
            checker = getattr(pdf, "check_pdf_syntax", None) or getattr(pdf, "check")
            problems = [p for p in checker() if "warning" not in p.lower()]
            rep.add("Arquivo com as páginas escolhidas: estrutura válida (qpdf)", not problems, "; ".join(problems[:3]))
    except Exception as exc:  # pragma: no cover
        rep.add("Arquivo com as páginas escolhidas: estrutura válida (qpdf)", False, str(exc))
        return None
    chk = pymupdf.open(stream=out, filetype="pdf")
    if chk.needs_pass:
        chk.authenticate(password or "")
    ok_count = chk.page_count == len(pages)
    rep.add("Número de páginas do arquivo final", ok_count,
            f"{chk.page_count} de {len(pages)} página(s) escolhida(s)")
    if not ok_count:
        return None
    bad = []
    for i, pno in enumerate(pages):
        a, b = _render_gray(src[pno], 60), _render_gray(chk[i], 60)
        same_text = norm(src[pno].get_text()) == norm(chk[i].get_text())
        if a.shape != b.shape or _changed_fraction(a, b) > 0.0005 or not same_text:
            bad.append(str(pno + 1))
    rep.add("Páginas escolhidas idênticas às do documento editado", not bad,
            ("diferenças nas páginas " + ", ".join(bad)) if bad else
            "páginas " + _ranges([p + 1 for p in pages]))
    rep.pages = chk.page_count
    rep.size_bytes = len(out)
    chk.close()
    src.close()
    return out if not bad else None


def _ranges(nums: list[int]) -> str:
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}–{nums[j]}")
        i = j + 1
    return ", ".join(out)
