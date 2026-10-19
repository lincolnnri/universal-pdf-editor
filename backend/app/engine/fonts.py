"""Identificação e reaproveitamento de fontes.

Estratégia para reescrever um texto com a aparência original:
  1. Fonte incorporada no próprio PDF (se contiver todos os glifos necessários).
  2. Fonte do sistema equivalente (via fontconfig, com aliases métricos: Arial→Liberation Sans etc.).
  3. Fonte padrão PDF (Helvetica / Times / Courier) conforme estilo.

Cada candidata é avaliada pela largura: medimos o texto original com a fonte candidata
e comparamos com a largura real no PDF. Uma pequena escala horizontal compensa diferenças
métricas residuais.
"""
from __future__ import annotations

import functools
import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass

import pymupdf

from .pdffont import PdfFontReuse


SUBSET_RE = re.compile(r"^[A-Z]{6}\+")

# Aliases para fontes comerciais comuns em documentos brasileiros → equivalentes métricos livres.
FAMILY_ALIASES = {
    "arial": "Liberation Sans",
    "arialmt": "Liberation Sans",
    "arialnarrow": "Liberation Sans Narrow",
    "helvetica": "Liberation Sans",
    "helveticaneue": "Liberation Sans",
    "timesnewroman": "Liberation Serif",
    "timesnewromanps": "Liberation Serif",
    "timesnewromanpsmt": "Liberation Serif",
    "times": "Liberation Serif",
    "couriernew": "Liberation Mono",
    "couriernewpsmt": "Liberation Mono",
    "courier": "Liberation Mono",
    "calibri": "Carlito",
    "cambria": "Caladea",
    "verdana": "DejaVu Sans",
    "tahoma": "DejaVu Sans",
    "segoeui": "DejaVu Sans",
}

STYLE_WORDS = ("bold", "black", "heavy", "semibold", "demibold", "italic", "oblique", "regular",
               "medium", "light", "book", "roman", "mt", "ps", "psmt", "condensed", "narrow")


@dataclass
class FontStyle:
    name: str          # nome original (sem prefixo de subset)
    family: str        # família estimada
    bold: bool
    italic: bool
    serif: bool
    mono: bool


def font_name_match(a: str, b: str) -> bool:
    """Mesmo nome de fonte (tolerando sufixos truncados/codificação: 'Lucida-Extend_' ~ 'Lucida-Extend_850')."""
    if a == b or a.split(",")[0] == b.split(",")[0]:
        return True
    a2, b2 = a.rstrip("_"), b.rstrip("_")
    short, long_ = sorted((a2, b2), key=len)
    return len(short) >= 8 and long_.startswith(short)


def clean_font_name(name: str) -> str:
    return SUBSET_RE.sub("", name or "").strip()


def style_from_span(font_name: str, flags: int) -> FontStyle:
    clean = clean_font_name(font_name)
    low = clean.lower()
    bold = bool(flags & 16) or any(w in low for w in ("bold", "black", "heavy", "semibold", "demi"))
    italic = bool(flags & 2) or "italic" in low or "oblique" in low
    mono = bool(flags & 8) or "mono" in low or "courier" in low
    sans_name = any(w in low for w in ("arial", "helvetica", "verdana", "tahoma", "calibri", "segoe",
                                       "sans", "gothic", "grotesk", "univers", "futura", "frutiger"))
    serif = not sans_name and (bool(flags & 4) or "times" in low or "serif" in low or "roman" in low or "georgia" in low
             or "cambria" in low or "garamond" in low) and "sans" not in low
    family = re.split(r"[,\-]", clean)[0] if clean else "Helvetica"
    return FontStyle(clean or "Helvetica", family, bold, italic, serif, mono)


def _normalize_family(family: str) -> str:
    key = re.sub(r"[^a-z]", "", family.lower())
    for word in sorted(STYLE_WORDS, key=len, reverse=True):
        if key.endswith(word) and len(key) > len(word) + 2:
            key = key[: -len(word)]
    return key


@functools.lru_cache(maxsize=256)
def system_font_file(family: str, bold: bool, italic: bool) -> str | None:
    """Localiza uma fonte do sistema equivalente via fontconfig. Retorna caminho ou None."""
    if not shutil.which("fc-match"):
        return None
    norm = _normalize_family(family)
    target = FAMILY_ALIASES.get(norm)
    if target is None:
        # "Liberation Sans Bold" → "Liberation Sans" (o estilo vai no padrão do fc-match)
        words = family.replace("-", " ").split()
        while len(words) > 1 and re.sub(r"[^a-z]", "", words[-1].lower()) in STYLE_WORDS:
            words.pop()
        target = " ".join(words) or family
    style = []
    if bold:
        style.append("Bold")
    if italic:
        style.append("Italic")
    pattern = target + (":style=" + " ".join(style) if style else "")
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}|%{family}", pattern],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or "|" not in out.stdout:
        return None
    path, fam = out.stdout.split("|", 1)
    if not path.lower().endswith((".ttf", ".otf")):
        return None
    # Aceita somente se a família corresponde (evita trocar Arial por uma fonte qualquer).
    fam_norm = re.sub(r"[^a-z]", "", fam.lower())
    want = re.sub(r"[^a-z]", "", target.lower())
    if want and (want in fam_norm or fam_norm.startswith(want[:6])):
        return path
    return None


@functools.lru_cache(maxsize=64)
def _font_from_file(path: str) -> pymupdf.Font:
    return pymupdf.Font(fontfile=path)


def base14_font(style: FontStyle) -> tuple[pymupdf.Font, str]:
    if style.mono:
        code = {(0, 0): "cour", (1, 0): "cobo", (0, 1): "coit", (1, 1): "cobi"}[(int(style.bold), int(style.italic))]
    elif style.serif:
        code = {(0, 0): "tiro", (1, 0): "tibo", (0, 1): "tiit", (1, 1): "tibi"}[(int(style.bold), int(style.italic))]
    else:
        code = {(0, 0): "helv", (1, 0): "hebo", (0, 1): "heit", (1, 1): "hebi"}[(int(style.bold), int(style.italic))]
    return pymupdf.Font(code), code


def generic_unicode_font(style: FontStyle) -> tuple[pymupdf.Font, str] | None:
    """Fonte com ampla cobertura Unicode para caracteres fora do Latin-1."""
    fam = "Liberation Mono" if style.mono else ("Liberation Serif" if style.serif else "Liberation Sans")
    for candidate in (fam, "DejaVu Sans"):
        path = system_font_file(candidate, style.bold, style.italic)
        if path:
            return _font_from_file(path), f"system:{path}"
    return None


def covers(font: pymupdf.Font, text: str) -> bool:
    for ch in set(text):
        cp = ord(ch)
        if ch in "\n\r\t":
            continue
        if not font.has_glyph(cp):
            return False
        if ch != " " and not ch.isspace():
            try:
                bb = font.glyph_bbox(cp)
                if bb.is_empty and font.glyph_advance(cp) <= 0:
                    return False
            except Exception:  # pragma: no cover - defensivo
                return False
    return True


@functools.lru_cache(maxsize=1)
def _builtin_cjk() -> pymupdf.Font | None:
    try:
        return pymupdf.Font("cjk")
    except Exception:
        return None


@functools.lru_cache(maxsize=512)
def _system_font_for_char(cp: int) -> str | None:
    if not shutil.which("fc-match"):
        return None
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", f":charset={cp:x}"], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    path = out.stdout.strip()
    if path.lower().endswith((".ttf", ".otf")):
        try:
            if _font_from_file(path).has_glyph(cp):
                return path
        except Exception:
            return None
    return None


def fallback_font(text: str, style: FontStyle) -> tuple[pymupdf.Font, str] | None:
    """Fonte com cobertura para escritas não latinas (CJK, árabe, hebraico, cirílico, grego, símbolos)."""
    needed = {ch for ch in text if not ch.isspace()}
    if not needed:
        return None
    # 1) fonte do sistema que cubra o caractere mais raro
    for ch in sorted(needed, key=ord, reverse=True):
        path = _system_font_for_char(ord(ch))
        if path:
            f = _font_from_file(path)
            if covers(f, text):
                return f, f"system:{path}"
            break
    # 2) fonte CJK embutida no MuPDF (Droid Sans Fallback)
    cjk = _builtin_cjk()
    if cjk is not None and covers(cjk, text):
        return cjk, "builtin:cjk"
    return None


@dataclass
class ChosenFont:
    font: pymupdf.Font | None
    source: str            # "original:<xref>", "embedded:<xref>", "system:<path>", "base14:<code>"
    hscale: float          # escala horizontal para reproduzir a largura original
    width_ratio: float     # largura original / largura medida (1.0 = idêntica)
    reuse: PdfFontReuse | None = None   # objeto de fonte original do PDF (escrita direta)

    @property
    def kind(self) -> str:
        return self.source.split(":", 1)[0]

    def text_length(self, text: str, fontsize: float) -> float:
        if self.reuse is not None:
            return self.reuse.text_length(text, fontsize)
        return self.font.text_length(text, fontsize=fontsize)

    def write(self, page: pymupdf.Page, origin: pymupdf.Point, text: str, size: float,
              color: tuple, angle: float, hscale: float, render_mode: int = 0,
              line_width: float = 1.0, stroke_color: tuple | None = None) -> None:
        if self.reuse is not None:
            self.reuse.write(page, origin, text, size, color, angle, hscale, render_mode, line_width,
                             stroke_color)
            return
        # insert_text respeita CropBox deslocado e /UserUnit (o TextWriter não)
        t = page.transformation_matrix
        u = abs(t.a) or abs(t.b) or 1.0
        kind, _, code = self.source.partition(":")
        if kind == "base14":
            fontname = code
        else:
            fontname = "UPE" + hashlib.md5(self.font.buffer[:4096] + str(len(self.font.buffer)).encode()).hexdigest()[:10]
            if fontname not in {f[4] for f in page.get_fonts()}:
                page.insert_font(fontname=fontname, fontbuffer=self.font.buffer)
        o = pymupdf.Point(origin) / u
        m = pymupdf.Matrix(hscale, 0, 0, 1, 0, 0) * pymupdf.Matrix(float(angle))
        if render_mode in (1, 2):
            rc = page.insert_text(o, text, fontname=fontname, fontsize=size / u, color=stroke_color or color,
                                  fill=color if render_mode == 2 else None, render_mode=render_mode,
                                  border_width=max(0.001, line_width / max(size, 0.1)), morph=(o, m))
        else:
            rc = page.insert_text(o, text, fontname=fontname, fontsize=size / u, color=color, morph=(o, m))
        if rc < 0:
            raise RuntimeError("falha ao escrever o texto")


class FontResolver:
    """Resolve fontes para um documento. Cacheia fontes incorporadas extraídas."""

    def __init__(self, doc: pymupdf.Document):
        self.doc = doc
        self._embedded: dict[int, pymupdf.Font | None] = {}
        self._reuse: dict[int, PdfFontReuse] = {}
        self._page_fonts: dict[tuple, list[tuple[int, str]]] = {}

    def _fonts_on_page(self, page: pymupdf.Page, include_type3: bool = False) -> list[tuple[int, str]]:
        key = (page.number, include_type3)
        if key not in self._page_fonts:
            fonts = []
            try:
                for f in page.get_fonts(full=True):
                    xref, ext, ftype, basefont = f[0], f[1], f[2], f[3]
                    if not include_type3 and (ext in ("n/a", "") or ftype == "Type3"):
                        continue
                    if ftype == "Type3":
                        continue
                    fonts.append((xref, clean_font_name(basefont)))
            except Exception:
                pass
            self._page_fonts[key] = fonts
        return self._page_fonts[key]

    def _embedded_font(self, xref: int) -> pymupdf.Font | None:
        if xref not in self._embedded:
            font = None
            try:
                _name, ext, _type, buf = self.doc.extract_font(xref)
                if buf and ext not in ("n/a", ""):
                    f = pymupdf.Font(fontbuffer=buf)
                    if getattr(f, "is_writable", True):
                        font = f
            except Exception:
                font = None
            self._embedded[xref] = font
        return self._embedded[xref]

    def candidates(self, page: pymupdf.Page, span_font: str, style: FontStyle):
        target = clean_font_name(span_font)
        for xref, name in self._fonts_on_page(page):
            if font_name_match(name, target):
                f = self._embedded_font(xref)
                if f is not None:
                    yield f, f"embedded:{xref}"
        # Fonte-padrão NÃO incorporada (Helvetica/Arial/Times/Courier): o visualizador a desenha com
        # a sua fonte-padrão interna — escrever com a fonte-padrão PDF reproduz exatamente o desenho.
        low = re.sub(r"[^a-z]", "", target.lower())
        if any(k in low for k in ("helvetica", "arial", "times", "courier")):
            std = FontStyle(style.name, style.family, style.bold, style.italic,
                            "times" in low, "courier" in low)
            f, code = base14_font(std)
            yield f, f"base14:{code}"
        path = system_font_file(style.family, style.bold, style.italic)
        if path:
            yield _font_from_file(path), f"system:{path}"
        f, code = base14_font(style)
        yield f, f"base14:{code}"
        uni = generic_unicode_font(style)
        if uni:
            yield uni

    def choose(self, page: pymupdf.Page, span_font: str, style: FontStyle, new_text: str,
               original_text: str, original_width: float, size: float,
               forbid_embedded: bool = False) -> ChosenFont:
        """Escolhe a melhor fonte para escrever `new_text` imitando o texto original."""
        best: ChosenFont | None = None
        if not forbid_embedded:
            target = clean_font_name(span_font)
            for xref, name in self._fonts_on_page(page, include_type3=True):
                if not font_name_match(name, target):
                    continue
                if xref not in self._reuse:
                    self._reuse[xref] = PdfFontReuse(self.doc, xref)
                ru = self._reuse[xref]
                if ru.covers(new_text) and ru.covers(original_text):
                    measured = ru.text_length(original_text, size)
                    ratio = original_width / measured if measured > 0 and original_width > 0 else 1.0
                    if abs(ratio - 1) < 0.08:
                        return ChosenFont(None, f"original:{xref}", min(1.15, max(0.85, ratio)), ratio, reuse=ru)
        latin1 = all(ord(ch) < 256 for ch in new_text)
        for font, source in self.candidates(page, span_font, style):
            if forbid_embedded and source.startswith("embedded"):
                continue
            if source.startswith("base14") and not latin1:
                continue   # as fontes-padrão PDF só codificam Latin-1
            if not covers(font, new_text):
                continue
            ratio = 1.0
            if original_text.strip() and original_width > 0 and size > 0:
                measured = font.text_length(original_text, fontsize=size)
                if measured > 0:
                    ratio = original_width / measured
            hscale = min(1.15, max(0.85, ratio))
            chosen = ChosenFont(font, source, hscale, ratio)
            # Embutida que cobre tudo: ótima por definição (mesmo desenho de glifo).
            if source.startswith("embedded") and abs(ratio - 1) < 0.06:
                return chosen
            if abs(ratio - 1) < 0.03:
                return chosen
            if best is None or abs(ratio - 1) < abs(best.width_ratio - 1) - 0.02:
                best = chosen
        if best is None:
            fb = fallback_font(new_text, style)
            if fb is not None:
                f, src = fb
                ratio = 1.0
                if original_text.strip() and original_width > 0 and covers(f, original_text):
                    measured = f.text_length(original_text, fontsize=size)
                    ratio = original_width / measured if measured > 0 else 1.0
                return ChosenFont(f, src, min(1.15, max(0.85, ratio)), ratio)
            f, code = base14_font(style)
            best = ChosenFont(f, f"base14:{code}", 1.0, 1.0)
        return best

    def for_new_text(self, style: FontStyle, text: str) -> ChosenFont:
        """Fonte para texto novo (adicionado pelo usuário ou reconstrução OCR)."""
        path = system_font_file(
            "Liberation Mono" if style.mono else ("Liberation Serif" if style.serif else "Liberation Sans"),
            style.bold, style.italic)
        if path:
            f = _font_from_file(path)
            if covers(f, text):
                return ChosenFont(f, f"system:{path}", 1.0, 1.0)
        f, code = base14_font(style)
        if covers(f, text) and all(ord(ch) < 256 for ch in text):
            return ChosenFont(f, f"base14:{code}", 1.0, 1.0)
        uni = generic_unicode_font(style)
        if uni and covers(uni[0], text):
            return ChosenFont(uni[0], uni[1], 1.0, 1.0)
        fb = fallback_font(text, style)
        if fb is not None:
            return ChosenFont(fb[0], fb[1], 1.0, 1.0)
        return ChosenFont(f, f"base14:{code}", 1.0, 1.0)
