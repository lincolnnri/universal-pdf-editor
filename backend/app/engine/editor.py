"""Aplicação real das alterações ao documento.

Cada operação é aplicada sobre uma cópia do PDF original:
  * Texto nativo  → os caracteres originais são removidos do content stream (redação restrita
    a texto, preservando imagens e vetores) e o novo texto é escrito com a fonte original
    incorporada (ou a equivalente mais próxima), no mesmo tamanho, cor, posição e alinhamento.
  * Texto OCR     → a região é reconstruída visualmente (remendo com inpainting) e o novo texto
    é escrito como texto vetorial real (selecionável/pesquisável).
"""
from __future__ import annotations

import difflib
import os
import unicodedata
import math
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

from .analysis import Element, PageAnalysis, hex_to_rgb, to_unrot_rect
from .fonts import FontResolver, FontStyle, font_name_match
from .raster import erase_region

MIN_HSCALE = 0.82
MIN_SIZE_FACTOR = 0.75


class OpError(ValueError):
    """Operação inválida (mensagem exibida ao usuário)."""


@dataclass
class OpResult:
    index: int
    type: str
    page: int
    mode: str = "native"                   # native | raster | added
    font: str | None = None
    warnings: list[str] = field(default_factory=list)
    expected_lines: list[str] = field(default_factory=list)   # textos que devem existir
    removed_text: str | None = None        # texto que deve ter desaparecido
    check_rect: tuple | None = None        # unrot: onde procurar o resultado
    affected: list[tuple] = field(default_factory=list)       # unrot: regiões alteradas
    image_rect: tuple | None = None        # unrot: imagem adicionada / removida
    image_removed: bool = False
    field_xref: int | None = None          # campo de formulário alterado
    field_name: str | None = None
    rtl: bool = False
    field_value: str | None = None

    def to_json(self) -> dict:
        return {"index": self.index, "type": self.type, "page": self.page, "mode": self.mode,
                "font": self.font, "warnings": self.warnings}


def _tiny_rect(bbox: tuple) -> pymupdf.Rect:
    r = pymupdf.Rect(bbox)
    c = (r.tl + r.br) / 2
    w = max(abs(r.width) * 0.12, 0.15)
    h = max(abs(r.height) * 0.12, 0.15)
    return pymupdf.Rect(c.x - w, c.y - h, c.x + w, c.y + h)


def _style_for(el: Element | None, op: dict) -> FontStyle:
    base_family = op.get("font") or ""
    serif = op.get("fontFamily") == "serif" if "fontFamily" in op else (el.serif if el else False)
    mono = op.get("fontFamily") == "mono" if "fontFamily" in op else (el.mono if el else False)
    bold = bool(op["bold"]) if op.get("bold") is not None else (el.bold if el else False)
    italic = bool(op["italic"]) if op.get("italic") is not None else (el.italic if el else False)
    name = el.font_name if el else (base_family or "Helvetica")
    return FontStyle(name, name.split(",")[0].split("-")[0], bold, italic, serif, mono)


@dataclass
class _Patch:
    """Edição mínima: só os caracteres alterados são removidos e reescritos; o resto fica intacto."""
    line: Element
    redact: list[int]
    pieces: list[tuple[pymupdf.Point, str]]
    chosen: object
    size: float
    color: tuple
    angle: float
    area: pymupdf.Rect


def _affix(a: str, b: str) -> tuple[int, int]:
    """(prefixo comum, sufixo comum) sem sobreposição."""
    n = min(len(a), len(b))
    p = 0
    while p < n and a[p] == b[p]:
        p += 1
    s = 0
    while s < n - p and a[len(a) - 1 - s] == b[len(b) - 1 - s]:
        s += 1
    return p, s


STYLE_KEYS = ("size", "color", "bold", "italic", "align", "font", "fontFamily")


class Editor:
    def __init__(self, doc: pymupdf.Document, analyses: dict[int, PageAnalysis],
                 image_loader: Callable[[str], bytes] | None = None):
        self.doc = doc
        self.analyses = analyses
        self.image_loader = image_loader
        self.fonts = FontResolver(doc)

    # ------------------------------------------------------------------ helpers
    def _element(self, pno: int, eid: str) -> Element:
        a = self.analyses.get(pno)
        if a is None:
            raise OpError(f"A página {pno + 1} ainda não foi analisada.")
        for e in a.elements:
            if e.id == eid:
                return e
        raise OpError(f"Elemento {eid} não encontrado na página {pno + 1}.")

    def _image_element(self, pno: int, eid: str) -> dict:
        a = self.analyses.get(pno)
        for im in getattr(a, "images", []) or []:
            if im["id"] == eid:
                return im
        raise OpError(f"Imagem {eid} não encontrada na página {pno + 1}.")

    # ------------------------------------------------------------------ main
    def apply(self, ops: list[dict], forbid_embedded: set[int] | None = None,
              pages: set[int] | None = None) -> list[OpResult]:
        forbid_embedded = forbid_embedded or set()
        # Última operação por elemento prevalece.
        last_for_element: dict[str, int] = {}
        for i, op in enumerate(ops):
            if op.get("elementId"):
                last_for_element[op["elementId"]] = i
        by_page: dict[int, list[tuple[int, dict]]] = {}
        for i, op in enumerate(ops):
            if op.get("elementId") and last_for_element[op["elementId"]] != i:
                continue
            pno = int(op.get("page", -1))
            if pno < 0 or pno >= self.doc.page_count:
                raise OpError(f"Operação {i + 1}: página inválida.")
            if pages is not None and pno not in pages:
                continue
            by_page.setdefault(pno, []).append((i, op))
        results: list[OpResult] = []
        for pno in sorted(by_page):
            results.extend(self._apply_page(pno, by_page[pno], forbid_embedded))
        return results

    def _apply_page(self, pno: int, items: list[tuple[int, dict]], forbid_embedded: set[int]) -> list[OpResult]:
        page = self.doc[pno]
        results: dict[int, OpResult] = {}
        text_redactions: list[pymupdf.Rect] = []
        image_px_redactions: list[pymupdf.Rect] = []
        image_rm_redactions: list[pymupdf.Rect] = []
        raster_jobs: list[tuple[int, Element]] = []
        writes: list[tuple[int, dict, Element]] = []
        patches: list[tuple[int, _Patch]] = []

        for i, op in items:
            t = op.get("type")
            res = OpResult(index=i, type=t, page=pno)
            results[i] = res
            if t in ("edit_text", "delete_element"):
                eid = op.get("elementId", "")
                if "-i" in eid:  # imagem existente
                    if t != "delete_element":
                        raise OpError(f"Operação {i + 1}: imagens só podem ser excluídas.")
                    im = self._image_element(pno, eid)
                    r = to_unrot_rect(page, pymupdf.Rect(im["bbox"]))
                    if im.get("shared"):
                        image_px_redactions.append(r)
                    else:
                        c = (r.tl + r.br) / 2
                        image_rm_redactions.append(pymupdf.Rect(c.x - 0.5, c.y - 0.5, c.x + 0.5, c.y + 0.5))
                    res.mode = "native"
                    res.image_rect = tuple(r)
                    res.image_removed = not im.get("shared")
                    res.affected.append(tuple(r))
                    continue
                el = self._element(pno, eid)
                new_text = "" if t == "delete_element" else str(op.get("text", ""))
                res.mode = "raster" if el.kind == "ocr" else "native"
                rect = el.rect_unrot()
                patch = None
                if t == "edit_text" and i not in forbid_embedded:
                    try:
                        patch = self._minimal_patch(page, op, el)
                    except Exception:  # noqa: BLE001 - qualquer imprevisto: reescrita completa
                        patch = None
                if patch is not None:
                    text_redactions.extend(_tiny_rect(patch.line.char_rects[k]) for k in patch.redact)
                    patches.append((i, patch))
                    res.font = patch.chosen.source.split(":", 1)[0] + (
                        ":" + _short(patch.chosen.source) if ":" in patch.chosen.source else "")
                    res.affected.append(tuple(patch.area))
                    res.expected_lines = [prepare_text(new_text)]
                    res.check_rect = tuple(rect | patch.area)
                    if el.text.strip() and el.text.strip() not in new_text:
                        res.removed_text = el.text
                    continue
                res.affected.append(tuple(rect))
                if el.kind == "native" or el.invisible_layer or el.char_rects:
                    text_redactions.extend(_tiny_rect(cr) for cr in el.char_rects)
                if el.kind == "ocr":
                    raster_jobs.append((i, el))
                    if el.region:
                        res.affected.append(el.region)
                    if not el.char_rects and self.analyses[pno].mode == "scanned":
                        # texto nativo ilegível (sem Unicode) na região também é removido
                        text_redactions.append(pymupdf.Rect(el.region) if el.region else rect)
                if el.text.strip() and el.text.strip() not in new_text:
                    res.removed_text = el.text
                if new_text.strip():
                    writes.append((i, op, el))
                res.check_rect = tuple(rect)
            elif t in ("add_text", "add_image"):
                res.mode = "added"
            elif t == "set_field":
                res.mode = "form"
            else:
                raise OpError(f"Operação {i + 1}: tipo desconhecido '{t}'.")

        # 1) remoção de imagens existentes
        if image_rm_redactions:
            for r in image_rm_redactions:
                page.add_redact_annot(r)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=pymupdf.PDF_REDACT_TEXT_NONE)
        if image_px_redactions:
            for r in image_px_redactions:
                page.add_redact_annot(r, fill=(1, 1, 1))
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=pymupdf.PDF_REDACT_TEXT_NONE)
        # 2) remoção do texto nativo (somente texto — imagens e vetores intactos)
        if text_redactions:
            for r in text_redactions:
                page.add_redact_annot(r, fill=False)
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE, text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        # 3) reconstrução visual (OCR)
        for i, el in raster_jobs:
            region = pymupdf.Rect(el.region) if el.region else el.rect_unrot()
            info = erase_region(page, region)
            if not info.get("erased"):
                results[i].warnings.append("Não foi possível reconstruir a região: " + info.get("reason", ""))
            results[i].warnings.append("Edição aplicada por reconstrução visual (texto originalmente em imagem).")
        # 4a) edições mínimas (só os caracteres alterados)
        for i, patch in patches:
            for o, txt in patch.pieces:
                ln_ = patch.line
                patch.chosen.write(page, o, txt, patch.size, patch.color, patch.angle, patch.chosen.hscale,
                                   ln_.render_mode, ln_.line_width or 1.0, ln_.stroke_color)
        # 4) novo texto nos elementos editados
        for i, op, el in writes:
            self._write_edit(page, op, el, results[i], i in forbid_embedded)
        # 5) textos adicionados
        for i, op in items:
            if op.get("type") == "add_text":
                self._add_text(page, op, results[i])
        # 6) imagens adicionadas
        for i, op in items:
            if op.get("type") == "add_image":
                self._add_image(page, op, results[i])
        # 7) campos de formulário
        for i, op in items:
            if op.get("type") == "set_field":
                self._set_field(page, op, results[i])
        return [results[i] for i, _ in items]

    # ------------------------------------------------------------------ edição mínima
    def _minimal_patch(self, page: pymupdf.Page, op: dict, el: Element) -> _Patch | None:
        """Planeja uma edição que preserva, pixel a pixel, os caracteres não alterados.

        Só os caracteres que mudaram são removidos; o trecho novo é escrito exatamente na posição do
        primeiro caractere alterado, com a fonte original. Se a largura mudar, os caracteres seguintes
        (ou anteriores, em texto alinhado à direita) são deslocados preservando o espaçamento original.
        Retorna None quando a reescrita completa é mais segura (mudança de estilo, quebra de linha,
        texto que não cabe, fonte original indisponível...)."""
        if el.kind != "native" or el.invisible_layer or el.text_unreliable or os.environ.get("UPE_NO_PATCH"):
            return None
        if any(op.get(k) is not None for k in STYLE_KEYS):
            return None
        old = el.text
        new = prepare_text(str(op.get("text", "")))
        if not new.strip() or new == old or "\n" in new or "\r" in new or has_rtl(new) or has_rtl(old):
            return None
        line, avail = el, max(el.max_extent, el.width)
        old_l, new_l = old, new
        if el.is_paragraph:
            if len(old) != sum(len(ln.text) for ln in el.lines) + len(el.lines) - 1:
                return None   # houve junção de hifenização: posições não batem
            p, s = _affix(old, new)
            pos = 0
            for ln in el.lines:
                a, b = pos, pos + len(ln.text)
                if a <= p and len(old) - s <= b:
                    line = ln
                    old_l = ln.text
                    new_l = old_l[: p - a] + new[p: len(new) - s] + old_l[len(old) - s - a:]
                    break
                pos = b + 1
            else:
                return None   # alteração atravessa linhas: refluxo do parágrafo
            avail = el.width
            if not new_l.strip():
                return None
        n, m = len(old_l), len(new_l)
        if len(line.char_origins) != n or len(line.char_rects) != n or n == 0:
            return None
        p, s = _affix(old_l, new_l)
        dx, dy = line.dir
        horiz = abs(dx) >= abs(dy)
        org = [pymupdf.Point(o) for o in line.char_origins]

        def adv(k: int) -> float:
            r = pymupdf.Rect(line.char_rects[k])
            return abs(r.width) if horiz else abs(r.height)

        def proj(a: pymupdf.Point, b: pymupdf.Point) -> float:
            return (b.x - a.x) * dx + (b.y - a.y) * dy

        def move(a: pymupdf.Point, d: float) -> pymupdf.Point:
            return pymupdf.Point(a.x + d * dx, a.y + d * dy)

        end = move(org[n - 1], adv(n - 1))
        style = _style_for(el, {})
        align = "left" if el.is_paragraph else el.align
        mid_new = new_l[p: m - s]
        # texto que talvez precise ser escrito (trecho novo + caracteres deslocados)
        need = mid_new + (old_l[:p] if align == "right" else old_l[n - s:])
        chosen = self.fonts.choose(page, line.font_name, style, need, old_l, line.width, line.size)
        if chosen.kind not in ("original", "embedded"):
            alt = self._reuse_for_pieces(page, line, need, old_l, org, proj)
            if alt is not None:
                chosen = alt
        if chosen.kind not in ("original", "embedded") and self._font_embedded(page, line.font_name):
            return None   # a fonte original está incorporada mas não serve: misturar desenhos destoaria
        size = line.size
        hs = chosen.hscale

        def L(t: str) -> float:
            return chosen.text_length(t, size) * hs

        start = org[p] if p < n else end
        stop = org[n - s] if s > 0 else end
        old_adv = proj(start, stop)
        new_adv = L(mid_new)
        delta = new_adv - old_adv
        tol = 0.02 * size
        if align == "center" and abs(delta) > tol:
            return None
        if el.is_paragraph and el.justify and abs(delta) > tol and line is not el.lines[-1]:
            return None   # linha justificada mudou de largura: refluxo
        redact = list(range(p, n - s))
        pieces: list[tuple[pymupdf.Point, str]] = []

        def keep_run(idxs: list[int], shift: float) -> None:
            """Reescreve caracteres originais na posição exata (deslocados de `shift`).

            Agrupa caracteres enquanto a posição prevista pela fonte coincide com a real (erro
            acumulado < 1% do corpo); espaçamentos extras (Tc/Tw, kerning) iniciam um novo grupo."""
            group: list[int] = []

            def flush() -> None:
                while group and old_l[group[0]].isspace():
                    group.pop(0)
                txt = "".join(old_l[k] for k in group).rstrip()
                if txt:
                    pieces.append((move(org[group[0]], shift), txt))
                group.clear()

            acc = 0.0
            for k in idxs:
                if group:
                    acc += L(old_l[group[-1]])
                    if abs(proj(org[group[0]], org[k]) - acc) > 0.01 * size:
                        flush()
                        acc = 0.0
                if not group:
                    acc = 0.0
                    if old_l[k].isspace():
                        continue
                group.append(k)
            flush()

        def covers_all(t: str) -> bool:
            if chosen.reuse is not None:
                return chosen.reuse.covers(t)
            from .fonts import covers as font_covers
            return chosen.font is not None and font_covers(chosen.font, t)

        # Modo preferido: a linha inteira é reescrita com a MESMA fonte nas posições originais —
        # pixel a pixel idêntica e com o texto contíguo (busca/cópia corretas). Se a fonte não
        # cobrir todos os caracteres, só o trecho alterado é tocado.
        std_ok = False
        if chosen.kind == "base14":
            low = "".join(ch for ch in line.font_name.lower() if ch.isalpha())
            std_ok = (any(k in low for k in ("helvetica", "arial", "times", "courier"))
                      and not self._font_embedded(page, line.font_name))
        whole = (chosen.kind in ("original", "embedded") or std_ok) and covers_all(new_l)
        if not whole and chosen.kind not in ("original", "embedded"):
            return None   # fonte substituta: a reescrita completa mantém a linha homogênea
        if align == "right":
            if mid_new.strip():
                pieces.append((move(stop, -new_adv), mid_new))
            shift_prefix = abs(delta) > tol and p > 0
            if whole or shift_prefix:
                redact.extend(range(0, p))
                keep_run(list(range(0, p)), -delta if abs(delta) > tol else 0.0)
            if whole and s > 0:
                redact.extend(range(n - s, n))
                keep_run(list(range(n - s, n)), 0.0)
        else:
            total = proj(org[0], end) + delta
            if delta > tol and total > avail + 0.5:
                return None   # não cabe: a reescrita completa ajusta a largura
            if whole and p > 0:
                redact.extend(range(0, p))
                keep_run(list(range(0, p)), 0.0)
            if mid_new.strip():
                pieces.append((start, mid_new))
            if (abs(delta) > tol or whole) and s > 0:
                redact.extend(range(n - s, n))
                keep_run(list(range(n - s, n)), delta if abs(delta) > tol else 0.0)
        if not redact and not pieces:
            return None
        if not all(covers_all(t) for _, t in pieces):
            return None
        area = pymupdf.Rect()
        for k in redact:
            area |= pymupdf.Rect(line.char_rects[k])
        ux, uy = dy, -dx
        for o, txt in pieces:
            ln_ = L(txt)
            for t_, u_ in ((0, -0.3 * size), (ln_, -0.3 * size), (ln_, size), (0, size)):
                area |= pymupdf.Point(o.x + t_ * dx + u_ * ux, o.y + t_ * dy + u_ * uy)
        color = line.color
        return _Patch(line, sorted(set(redact)), pieces, chosen, size, color, line.angle, area)

    def _reuse_for_pieces(self, page, line, need: str, old_l: str, org, proj):
        """Fonte original do PDF que cobre apenas os caracteres a escrever (subconjuntos parciais).

        A escala horizontal é medida pelas distâncias reais entre caracteres consecutivos cobertos."""
        from .fonts import ChosenFont, clean_font_name
        from .pdffont import PdfFontReuse
        target = clean_font_name(line.font_name)
        for xref, name in self.fonts._fonts_on_page(page, include_type3=True):
            if not font_name_match(name, target):
                continue
            if xref not in self.fonts._reuse:
                self.fonts._reuse[xref] = PdfFontReuse(self.doc, xref)
            ru = self.fonts._reuse[xref]
            if not ru.covers(need):
                continue
            real = model = 0.0
            for k in range(len(old_l) - 1):
                a, b = old_l[k], old_l[k + 1]
                if a.isspace() or b.isspace() or not ru.covers(a):
                    continue   # espaços podem ter espaçamento extra (Tw)
                d = proj(org[k], org[k + 1])
                if d <= 0:
                    continue
                real += d
                model += ru.advance(a) * line.size
            ratio = real / model if model > 0 else 1.0
            if model > 0 and abs(ratio - 1) > 0.12:
                continue
            return ChosenFont(None, f"original:{xref}", min(1.15, max(0.85, ratio)), ratio, reuse=ru)
        # fonte incorporada extraída (programa de fonte com cmap próprio)
        from .fonts import covers as font_covers
        for xref, name in self.fonts._fonts_on_page(page):
            if not font_name_match(name, target):
                continue
            f = self.fonts._embedded_font(xref)
            if f is None or not font_covers(f, need):
                continue
            real = model = 0.0
            for k in range(len(old_l) - 1):
                a, b = old_l[k], old_l[k + 1]
                if a.isspace() or b.isspace() or not f.has_glyph(ord(a)):
                    continue
                d = proj(org[k], org[k + 1])
                if d <= 0:
                    continue
                real += d
                model += f.text_length(a, fontsize=line.size)
            ratio = real / model if model > 0 else 1.0
            if model > 0 and abs(ratio - 1) > 0.12:
                continue
            return ChosenFont(f, f"embedded:{xref}", min(1.15, max(0.85, ratio)), ratio)
        return None

    def _font_embedded(self, page: pymupdf.Page, span_font: str) -> bool:
        from .fonts import clean_font_name
        target = clean_font_name(span_font)
        try:
            for f in page.get_fonts(full=True):
                name = clean_font_name(f[3])
                if font_name_match(name, target) and f[1] not in ("n/a", ""):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # ------------------------------------------------------------------ escrita
    def _write_edit(self, page: pymupdf.Page, op: dict, el: Element, res: OpResult, forbid_embedded: bool) -> None:
        op = {**op, "text": prepare_text(str(op.get("text", "")))}
        if has_rtl(str(op.get("text", ""))) and abs(el.angle) < 0.5:
            self._write_rtl(page, op, el, res)
            return
        if el.is_paragraph:
            self._write_paragraph(page, op, el, res, forbid_embedded)
            return
        text = str(op.get("text", "")).replace("\r", "")
        lines = text.split("\n")
        style = _style_for(el, op)
        size = float(op.get("size") or el.size)
        if size <= 0 or size > 500:
            raise OpError(f"Operação {res.index + 1}: tamanho de fonte inválido.")
        color = hex_to_rgb(op["color"]) if op.get("color") else el.color
        align = op.get("align") or el.align
        style_changed = (style.bold != el.bold or style.italic != el.italic
                         or style.serif != el.serif or style.mono != el.mono)

        if el.kind == "native":
            chosen = self.fonts.choose(page, el.font_name, style, text.replace("\n", ""), el.text, el.width, el.size,
                                       forbid_embedded=forbid_embedded or style_changed)
            if style_changed or abs(size - el.size) > 0.01:
                chosen.hscale = 1.0 if style_changed else chosen.hscale
        else:
            chosen = self.fonts.for_new_text(style, text.replace("\n", ""))
            ref = chosen.text_length(el.text, el.size)
            if ref > 0 and el.width > 0:
                chosen.hscale = min(1.15, max(0.85, el.width / ref))
        res.font = chosen.source.split(":", 1)[0] + (":" + _short(chosen.source) if ":" in chosen.source else "")

        dx, dy = el.dir
        ux, uy = dy, -dx  # "para cima"
        origin0 = pymupdf.Point(el.origin)
        line_gap = size * 1.2
        res.expected_lines = [ln for ln in lines if ln.strip()]
        for k, line in enumerate(lines):
            if not line.strip():
                continue
            hscale = chosen.hscale
            lsize = size
            natural = chosen.text_length(line, lsize) * hscale
            avail = max(el.max_extent, el.width)
            if natural > avail + 0.5:
                need = avail / natural
                if need * hscale >= MIN_HSCALE:
                    hscale *= need
                else:
                    factor = max(MIN_SIZE_FACTOR, need * hscale / MIN_HSCALE)
                    hscale = MIN_HSCALE if hscale > MIN_HSCALE else hscale
                    lsize = size * factor
                    final = chosen.text_length(line, lsize) * hscale
                    if final > avail + 0.5:
                        res.warnings.append("O novo texto é maior que o espaço disponível e pode "
                                            "encostar em elementos vizinhos.")
                res.warnings.append("Texto ajustado para caber no espaço original.")
            length = chosen.text_length(line, lsize) * hscale
            if align == "right":
                shift = el.width - length
            elif align == "center":
                shift = (el.width - length) / 2
            else:
                shift = 0.0
            down = k * line_gap
            o = pymupdf.Point(origin0.x + shift * dx - down * ux, origin0.y + shift * dy - down * uy)
            chosen.write(page, o, line, lsize, color, el.angle, hscale, el.render_mode,
                         el.line_width or 1.0, (color if op.get("color") else el.stroke_color))
            # área afetada
            q = pymupdf.Rect(o, o)
            for t_, u_ in ((0, -0.3 * lsize), (length, -0.3 * lsize), (length, lsize), (0, lsize)):
                q |= pymupdf.Point(o.x + t_ * dx + u_ * ux, o.y + t_ * dy + u_ * uy)
            res.affected.append(tuple(q))
            res.check_rect = tuple(pymupdf.Rect(res.check_rect) | q) if res.check_rect else tuple(q)
        if len(lines) > 1:
            res.warnings.append("Texto com várias linhas: as linhas extras são escritas abaixo da original.")

    def _write_rtl(self, page: pymupdf.Page, op: dict, el: Element, res: OpResult) -> None:
        """Árabe/hebraico: composição com HarfBuzz (formas contextuais e ordem visual corretas)."""
        text = str(op.get("text", "")).replace("\r", "")
        size = float(op.get("size") or el.size)
        color = hex_to_rgb(op["color"]) if op.get("color") else el.color
        align = op.get("align") or ("justify" if el.justify else el.align)
        if align == "left" and not el.is_paragraph:
            align = "right" if has_rtl(el.text) else "left"
        bold = op.get("bold") if op.get("bold") is not None else el.bold
        r = el.rect_unrot()
        x0 = r.x0 if align != "right" else min(r.x0, r.x1 - max(el.max_extent, el.width))
        x1 = max(r.x1, r.x0 + max(el.max_extent, el.width)) if align != "right" else r.x1
        line_h = el.line_gap if el.is_paragraph else size * 1.15
        top = el.origin[1] - size * 0.88
        lines = max(1, text.count("\n") + 1) if not el.is_paragraph else max(len(el.lines), 1) + 2
        box = pymupdf.Rect(x0, top, x1, top + line_h * lines + size)
        css_color = "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in color)
        html = ('<div dir="rtl" style="margin:0;font-size:{s:.2f}px;line-height:{lh:.2f}px;color:{c};'
                'text-align:{a};font-weight:{w}">{t}</div>').format(
            s=size, lh=line_h, c=css_color, a={"left": "left", "center": "center"}.get(align, align),
            w="bold" if bold else "normal",
            t="<br>".join(_html_escape(t) for t in text.split("\n")))
        spare, _scale = page.insert_htmlbox(box, html, scale_low=0.8)
        if spare < 0:
            res.warnings.append("O texto em escrita da direita para a esquerda não coube totalmente no espaço.")
        res.font = "htmlbox:harfbuzz"
        res.expected_lines = [ln for ln in text.split("\n") if ln.strip()]
        res.rtl = True
        res.affected.append(tuple(box))
        res.check_rect = tuple(box)

    def _space_below(self, el: Element) -> float:
        """Distância (pt) da última linha do parágrafo até o próximo elemento abaixo."""
        r = el.rect_unrot()
        last_base = el.lines[-1].origin[1] if el.lines else el.origin[1]
        limit = (self.doc[el.page].rect * self.doc[el.page].derotation_matrix).normalize().y1 - 18
        for o in self.analyses[el.page].elements:
            if o is el or o.kind != "native" and o.kind != "ocr":
                continue
            orr = o.rect_unrot()
            if orr.y0 > last_base and orr.x1 > r.x0 and orr.x0 < r.x1:
                limit = min(limit, orr.y0)
        return max(0.0, limit - last_base)

    def _write_paragraph(self, page: pymupdf.Page, op: dict, el: Element, res: OpResult,
                         forbid_embedded: bool) -> None:
        """Reescreve um parágrafo com quebra automática de linhas (como num editor de texto)."""
        text = " ".join(str(op.get("text", "")).replace("\r", "").split(" "))
        style = _style_for(el, op)
        size = float(op.get("size") or el.size)
        if size <= 0 or size > 500:
            raise OpError(f"Operação {res.index + 1}: tamanho de fonte inválido.")
        color = hex_to_rgb(op["color"]) if op.get("color") else el.color
        align = op.get("align") or ("justify" if el.justify else el.align)
        style_changed = (style.bold != el.bold or style.italic != el.italic)
        # a última linha não é justificada: mede a largura natural da fonte com ela
        ref = el.lines[-1] if len(el.lines[-1].text) >= 8 else el.lines[0]
        chosen = self.fonts.choose(page, el.font_name, style, text.replace("\n", " "), ref.text, ref.width,
                                   el.size, forbid_embedded=forbid_embedded or style_changed)
        if style_changed:
            chosen.hscale = 1.0
        res.font = chosen.source.split(":", 1)[0] + (":" + _short(chosen.source) if ":" in chosen.source else "")
        width = el.width
        gap0 = el.line_gap or size * 1.2

        # Quebras de linha originais que continuam existindo no texto novo são preservadas,
        # para que uma edição pequena não reorganize o parágrafo inteiro.
        orig_words: list[str] = []
        orig_breaks: set[int] = set()   # índice da palavra que inicia uma nova linha
        for ln in el.lines:
            ws = ln.text.split()
            if orig_words and ws:
                orig_breaks.add(len(orig_words))
            orig_words.extend(ws)

        def wrap(sz: float, hs: float) -> list[tuple[str, bool]]:
            out: list[tuple[str, bool]] = []   # (linha, é_fim_de_parágrafo)
            for seg in text.split("\n"):
                words = seg.split()
                if not words:
                    out.append(("", True))
                    continue
                keep: set[int] = set()
                sm = difflib.SequenceMatcher(a=orig_words, b=words, autojunk=False)
                for a0, b0, size_ in sm.get_matching_blocks():
                    for j in range(size_):
                        if a0 + j in orig_breaks:
                            keep.add(b0 + j)
                line = words[0]
                for idx in range(1, len(words)):
                    w = words[idx]
                    cand = line + " " + w
                    if idx in keep or chosen.text_length(cand, sz) * hs > width * 1.005 + 0.3:
                        out.append((line, False))
                        line = w
                    else:
                        line = cand
                out.append((line, True))
            return out

        hscale = chosen.hscale
        lsize = size
        gap = gap0 * (size / el.size)
        avail_lines = len(el.lines) + int(self._space_below(el) // gap)
        lines = wrap(lsize, hscale)
        if len(lines) > avail_lines:
            for factor in (0.96, 0.92, 0.88, 0.85):
                lsize = size * factor
                gap = gap0 * (lsize / el.size)
                avail_lines = len(el.lines) + int(self._space_below(el) // gap)
                lines = wrap(lsize, hscale)
                if len(lines) <= avail_lines:
                    res.warnings.append("Fonte do parágrafo reduzida levemente para caber no espaço.")
                    break
            else:
                res.warnings.append("O parágrafo ficou maior que o espaço disponível e pode encostar no "
                                    "conteúdo abaixo.")
        if len(lines) > len(el.lines):
            res.warnings.append(f"O parágrafo passou de {len(el.lines)} para {len(lines)} linha(s).")

        ox, oy = el.origin
        res.expected_lines = [ln for ln, _ in lines if ln.strip()]
        for k, (line, last) in enumerate(lines):
            if not line.strip():
                continue
            y = oy + k * gap
            length = chosen.text_length(line, lsize) * hscale
            words = line.split(" ")
            if align == "justify" and not last and len(words) > 1:
                widths = [chosen.text_length(w, lsize) * hscale for w in words]
                spacing = (width - sum(widths)) / (len(words) - 1)
                x = ox
                for n_, (w, wl) in enumerate(zip(words, widths)):
                    # o espaço final mantém a separação de palavras na extração de texto
                    piece = w + " " if n_ < len(words) - 1 and chosen.text_length(" ", lsize) > 0 else w
                    chosen.write(page, pymupdf.Point(x, y), piece, lsize, color, 0.0, hscale)
                    x += wl + spacing
                length = width
                shift = 0.0
            else:
                shift = width - length if align == "right" else (width - length) / 2 if align == "center" else 0.0
                chosen.write(page, pymupdf.Point(ox + shift, y), line, lsize, color, 0.0, hscale)
            q = pymupdf.Rect(ox + shift, y - lsize, ox + shift + length, y + 0.3 * lsize)
            res.affected.append(tuple(q))
            res.check_rect = tuple(pymupdf.Rect(res.check_rect) | q) if res.check_rect else tuple(q)

    def _add_text(self, page: pymupdf.Page, op: dict, res: OpResult) -> None:
        text = prepare_text(str(op.get("text", "")))
        if has_rtl(text):
            res.warnings.append("Texto da direita para a esquerda adicionado com composição automática.")
        if not text.strip():
            raise OpError(f"Operação {res.index + 1}: texto vazio.")
        size = float(op.get("size") or 12)
        if size <= 0 or size > 500:
            raise OpError(f"Operação {res.index + 1}: tamanho de fonte inválido.")
        color = hex_to_rgb(op.get("color") or "#000000")
        style = _style_for(None, op)
        chosen = self.fonts.for_new_text(style, text.replace("\n", ""))
        res.font = chosen.source.split(":", 1)[0] + (":" + _short(chosen.source) if ":" in chosen.source else "")
        x, y = float(op.get("x", 0)), float(op.get("y", 0))  # canto superior esquerdo (display)
        angle = float(page.rotation % 360)
        lines = text.split("\n")
        res.expected_lines = [ln for ln in lines if ln.strip()]
        total = pymupdf.Rect()
        for k, line in enumerate(lines):
            base_disp = pymupdf.Point(x, y + size * 0.85 + k * size * 1.2)
            o = base_disp * page.derotation_matrix
            if line.strip():
                chosen.write(page, o, line, size, color, angle, 1.0)
            w = chosen.text_length(line, size)
            disp = pymupdf.Rect(x, y + k * size * 1.2, x + w, y + (k + 1) * size * 1.2)
            total |= disp
        unrot = to_unrot_rect(page, total)
        res.affected.append(tuple(unrot))
        res.check_rect = tuple(unrot)

    def _set_field(self, page: pymupdf.Page, op: dict, res: OpResult) -> None:
        eid = str(op.get("elementId") or "")
        try:
            xref = int(eid.rsplit("-f", 1)[1])
        except (IndexError, ValueError) as exc:
            raise OpError(f"Operação {res.index + 1}: campo inválido.") from exc
        widget = next((w for w in (page.widgets() or []) if w.xref == xref), None)
        if widget is None:
            raise OpError(f"Operação {res.index + 1}: campo de formulário não encontrado.")
        if widget.field_flags & 1:
            raise OpError(f"Operação {res.index + 1}: o campo '{widget.field_name}' é somente leitura.")
        value = op.get("value")
        if widget.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
            on = str(value) not in ("", "Off", "false", "False", "0")
            widget.field_value = widget.on_state() if on else "Off"
            stored = "on" if on else "Off"
        else:
            stored = "" if value is None else str(value)
            if widget.field_type in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
                opts = [o if isinstance(o, str) else o[0] for o in (widget.choice_values or [])]
                if opts and stored not in opts and widget.field_type == pymupdf.PDF_WIDGET_TYPE_LISTBOX:
                    raise OpError(f"Operação {res.index + 1}: opção inválida para '{widget.field_name}'.")
            widget.field_value = stored
        widget.update()
        res.field_xref = xref
        res.field_name = widget.field_name
        res.field_value = stored
        res.affected.append(tuple(widget.rect))
        res.check_rect = tuple(widget.rect)

    def _add_image(self, page: pymupdf.Page, op: dict, res: OpResult) -> None:
        if self.image_loader is None:
            raise OpError("Imagens não disponíveis nesta sessão.")
        data = self.image_loader(str(op.get("imageId", "")))
        rect = op.get("rect") or []
        if len(rect) != 4:
            raise OpError(f"Operação {res.index + 1}: retângulo da imagem inválido.")
        disp = pymupdf.Rect(rect).normalize()
        if disp.is_empty:
            raise OpError(f"Operação {res.index + 1}: retângulo da imagem vazio.")
        unrot = to_unrot_rect(page, disp)
        page.insert_image(unrot, stream=data, keep_proportion=False, rotate=page.rotation,
                          overlay=True)
        res.image_rect = tuple(unrot)
        res.affected.append(tuple(unrot))


_COMPAT_RANGES = ((0x1D400, 0x1D7FF), (0x2F00, 0x2FDF), (0x2E80, 0x2EFF), (0xFB00, 0xFB4F), (0x2000, 0x200A))


def prepare_text(text: str) -> str:
    """Normaliza caracteres que raramente existem nas fontes: espaços especiais, hífen discreto,
    ligaduras, letras matemáticas e radicais CJK (mapeados para a forma comum equivalente)."""
    out = []
    for ch in text.replace("\r", ""):
        cp = ord(ch)
        if ch in "\u00a0\u202f":
            out.append(" ")
        elif ch == "\u00ad" or ch in "\u200b\u200c\u200d\ufeff":
            continue
        elif any(a <= cp <= b for a, b in _COMPAT_RANGES):
            out.append(unicodedata.normalize("NFKC", ch))
        else:
            out.append(ch)
    return "".join(out)


def has_rtl(text: str) -> bool:
    return any(unicodedata.bidirectional(ch) in ("R", "AL") for ch in text)


def _html_escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short(source: str) -> str:
    kind, _, rest = source.partition(":")
    if kind == "system":
        return rest.rsplit("/", 1)[-1]
    return rest


def text_in(page: pymupdf.Page, rect: pymupdf.Rect) -> str:
    return page.get_text("text", clip=rect)


def norm(s: str) -> str:
    # NFKC: ligaduras (ﬁ, ﬀ) e formas de compatibilidade viram letras comuns
    return "".join(unicodedata.normalize("NFKC", s).split()).casefold()


def angle_of(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(-dy, dx))
