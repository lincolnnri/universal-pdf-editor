"""Análise de páginas: classifica a página e extrai os elementos de texto editáveis.

Coordenadas:
  * "unrot" — espaço de coordenadas da página sem rotação (o usado pelo PyMuPDF para extração,
    redação e inserção). Todos os dados internos usam este espaço.
  * "display" — espaço da página como o usuário a vê (após /Rotate), em pontos PDF.
    É o que o frontend recebe e envia.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import pymupdf

from .fonts import FontStyle, style_from_span

ASCENT = 0.80   # fração do tamanho da fonte acima da linha de base (caixa de seleção)
DESCENT = 0.22  # fração abaixo da linha de base


@dataclass
class Element:
    id: str
    page: int
    kind: str                 # "native" | "ocr"
    text: str
    origin: tuple[float, float]   # linha de base inicial (unrot)
    angle: float              # graus, sentido do texto no espaço unrot (0 = esquerda→direita)
    width: float              # comprimento do texto ao longo da direção
    size: float
    color: tuple[float, float, float]
    font_name: str
    flags: int
    bold: bool
    italic: bool
    serif: bool
    mono: bool
    align: str = "left"       # left | right | center
    char_rects: list[tuple[float, float, float, float]] = field(default_factory=list)  # unrot (redação)
    char_origins: list[tuple[float, float]] = field(default_factory=list)  # unrot, 1 por caractere
    region: tuple[float, float, float, float] | None = None  # unrot, área a reconstruir (OCR)
    max_extent: float = 0.0   # espaço disponível ao longo da direção, a partir da âncora
    bg_color: tuple[float, float, float] | None = None
    invisible_layer: bool = False  # texto OCR invisível sobre imagem
    render_mode: int = 0           # 0 preenchido, 1 contorno, 2 preenchido+contorno
    line_width: float = 0.0        # espessura do contorno (modos 1/2)
    stroke_color: tuple[float, float, float] | None = None
    # Parágrafo (várias linhas com o mesmo estilo): linhas originais, entrelinha e justificação
    text_unreliable: bool = False   # texto nativo sem Unicode válido (reconhecido por OCR da região)
    lines: list["Element"] = field(default_factory=list)
    line_gap: float = 0.0
    justify: bool = False

    @property
    def is_paragraph(self) -> bool:
        return len(self.lines) > 1

    @property
    def style(self) -> FontStyle:
        return FontStyle(self.font_name, self.font_name.split(",")[0].split("-")[0],
                         self.bold, self.italic, self.serif, self.mono)

    @property
    def dir(self) -> tuple[float, float]:
        a = math.radians(self.angle)
        return (math.cos(a), -math.sin(a))

    def quad_unrot(self, size: float | None = None, width: float | None = None) -> list[pymupdf.Point]:
        s = size or self.size
        w = self.width if width is None else width
        dx, dy = self.dir
        ux, uy = dy, -dx  # vetor "para cima" do texto
        ox, oy = self.origin
        pts = []
        for t, u in ((0, -DESCENT * s), (w, -DESCENT * s), (w, ASCENT * s), (0, ASCENT * s)):
            pts.append(pymupdf.Point(ox + t * dx + u * ux, oy + t * dy + u * uy))
        return pts

    def all_points_unrot(self) -> list[pymupdf.Point]:
        if self.is_paragraph:
            pts: list[pymupdf.Point] = []
            for ln in self.lines:
                pts.extend(ln.quad_unrot(width=self.width))
            return pts
        return self.quad_unrot()

    def rect_unrot(self) -> pymupdf.Rect:
        pts = self.all_points_unrot()
        r = pymupdf.Rect(pts[0], pts[0])
        for p in pts:
            r |= p
        return r


def color_int_to_rgb(c: int) -> tuple[float, float, float]:
    return (((c >> 16) & 255) / 255.0, ((c >> 8) & 255) / 255.0, (c & 255) / 255.0)


def rgb_to_hex(c: tuple[float, float, float]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v * 255))) for v in c)


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def to_display_rect(page: pymupdf.Page, r: pymupdf.Rect) -> pymupdf.Rect:
    return (pymupdf.Rect(r) * page.rotation_matrix).normalize()


def to_unrot_rect(page: pymupdf.Page, r: pymupdf.Rect) -> pymupdf.Rect:
    return (pymupdf.Rect(r) * page.derotation_matrix).normalize()


def _is_garbage(ch: str) -> bool:
    cp = ord(ch)
    return cp == 0xFFFD or 0xE000 <= cp <= 0xF8FF or (cp < 32 and ch not in "\t\n\r")


@dataclass
class PageAnalysis:
    number: int
    width: float              # display
    height: float             # display
    rotation: int
    mode: str                 # "native" | "scanned" | "ocr_layer" | "empty" | "mixed"
    needs_ocr: bool
    ocr_done: bool
    elements: list[Element]
    notes: list[str] = field(default_factory=list)
    image_coverage: float = 0.0
    images: list[dict] = field(default_factory=list)   # imagens existentes (coords display)
    fields: list[dict] = field(default_factory=list)   # campos de formulário (AcroForm)


def _invisible_origins(page: pymupdf.Page) -> set[tuple[int, int]]:
    """Origens (arredondadas) de caracteres com render mode 3 (invisível) ou opacidade 0."""
    result: set[tuple[int, int]] = set()
    try:
        for span in page.get_texttrace():
            if span.get("type") == 3 or span.get("opacity", 1) == 0:
                for ch in span.get("chars", ()):
                    o = ch[2]
                    result.add((round(o[0] * 2), round(o[1] * 2)))
    except Exception:
        pass
    return result


def _trace_styles(page: pymupdf.Page) -> dict[tuple[int, int], tuple[int, float, tuple]]:
    """Origem do caractere → (modo de renderização, espessura do traço, cor)."""
    out: dict[tuple[int, int], tuple[int, float, tuple]] = {}
    try:
        for span in page.get_texttrace():
            t = span.get("type", 0)
            if t not in (1, 2):
                continue
            for ch in span.get("chars", ()):
                o = ch[2]
                out[(round(o[0] * 2), round(o[1] * 2))] = (t, float(span.get("linewidth") or 1.0),
                                                           tuple(span.get("color") or (0, 0, 0)))
    except Exception:
        pass
    return out


def _image_coverage(page: pymupdf.Page) -> float:
    area = abs(page.rect)
    if area <= 0:
        return 0.0
    covered = 0.0
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"]) & page.rect
            covered = max(covered, abs(r))  # maior imagem isolada (evita somar sobreposições)
    except Exception:
        pass
    return covered / area


def _visual_diff(a: tuple, b: tuple) -> bool:
    return a[0] != b[0] or abs(a[1] - b[1]) > 0.3 or a[2] != b[2] or (a[3] & 18) != (b[3] & 18)


def extract_native_elements(page: pymupdf.Page) -> tuple[list[Element], list[Element], int]:
    """Retorna (elementos visíveis, elementos de camada invisível, nº de caracteres-lixo)."""
    invisible = _invisible_origins(page)
    stroked = _trace_styles(page)
    rules = _vertical_rules(page)
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    visible_elems: list[Element] = []
    invisible_elems: list[Element] = []
    garbage = 0

    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            dx, dy = line["dir"]
            angle = math.degrees(math.atan2(-dy, dx))
            run: list[tuple[dict, dict]] = []  # (char, span)

            def flush():
                if not run:
                    return
                # remove espaços nas pontas
                while run and run[0][0]["c"].isspace():
                    run.pop(0)
                while run and run[-1][0]["c"].isspace():
                    run.pop()
                if not run:
                    return
                text = "".join(c["c"] for c, _ in run)
                span = run[0][1]
                first, last = run[0][0], run[-1][0]
                o0 = pymupdf.Point(first["origin"])
                lb = pymupdf.Rect(last["bbox"])
                lo = pymupdf.Point(last["origin"])
                adv = abs(lb.width) if abs(dx) >= abs(dy) else abs(lb.height)
                end = pymupdf.Point(lo.x + adv * dx, lo.y + adv * dy)
                width = math.hypot(end.x - o0.x, end.y - o0.y)
                inv = all((round(c["origin"][0] * 2), round(c["origin"][1] * 2)) in invisible for c, _ in run)
                st = style_from_span(span["font"], span["flags"])
                el = Element(
                    id="", page=page.number, kind="native", text=text,
                    origin=(o0.x, o0.y), angle=round(angle, 3), width=width, size=span["size"],
                    color=color_int_to_rgb(span["color"]), font_name=st.name, flags=span["flags"],
                    bold=st.bold, italic=st.italic, serif=st.serif, mono=st.mono,
                    char_rects=[tuple(c["bbox"]) for c, _ in run],
                    char_origins=[tuple(c["origin"]) for c, _ in run],
                    invisible_layer=inv,
                )
                if stroked:
                    marks = [stroked.get((round(c["origin"][0] * 2), round(c["origin"][1] * 2))) for c, _ in run
                             if not c["c"].isspace()]
                    hit = [m for m in marks if m]
                    if hit and len(hit) * 2 >= len(marks):
                        el.render_mode, el.line_width, sc = hit[0]
                        el.stroke_color = tuple(float(v) for v in sc[:3]) if len(sc) >= 3 else (0.0, 0.0, 0.0)
                (invisible_elems if inv else visible_elems).append(el)
                run.clear()

            prev_char = None
            prev_key = None  # estilo do último caractere não-espaço
            for span in line.get("spans", []):
                key = (span["font"], round(span["size"], 2), span["color"], span["flags"])
                for ch in span.get("chars", []):
                    if _is_garbage(ch["c"]):
                        garbage += 1
                    if prev_char is not None:
                        pb = pymupdf.Rect(prev_char["bbox"])
                        p_adv = abs(pb.width) if abs(dx) >= abs(dy) else abs(pb.height)
                        po, co = prev_char["origin"], ch["origin"]
                        along = (co[0] - po[0]) * dx + (co[1] - po[1]) * dy
                        gap = along - p_adv
                        # após um espaço (texto justificado) tolera lacunas maiores
                        limit = (2.2 if prev_char["c"] == " " else 0.9) * span["size"]
                        big_gap = gap > limit or along < -0.5 * span["size"]
                        if not big_gap and rules and abs(dy) < 1e-3 and dx > 0:
                            # borda de tabela entre os dois caracteres → células diferentes
                            xa, xb = po[0] + p_adv * 0.5, co[0] + 0.1
                            ymid = co[1] - 0.3 * span["size"]
                            big_gap = any(xa < rx < xb and ry0 <= ymid <= ry1 for rx, ry0, ry1 in rules)
                        style_change = (not ch["c"].isspace() and prev_key is not None
                                        and _visual_diff(prev_key, key))
                        if big_gap or style_change:
                            flush()
                    run.append((ch, span))
                    prev_char = ch
                    if not ch["c"].isspace():
                        prev_key = key
            flush()
    return visible_elems, invisible_elems, garbage


def _vertical_rules(page: pymupdf.Page) -> list[tuple[float, float, float]]:
    """Segmentos verticais desenhados (bordas de tabela/caixas): (x, y0, y1) no espaço unrot."""
    segs: list[tuple[float, float, float]] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return segs
    for d in drawings[:5000]:
        for item in d.get("items", []):
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.x - p2.x) < 0.8 and abs(p1.y - p2.y) > 3:
                    segs.append(((p1.x + p2.x) / 2, min(p1.y, p2.y), max(p1.y, p2.y)))
            elif item[0] == "re":
                r = pymupdf.Rect(item[1])
                if r.height > 3:
                    segs.append((r.x0, r.y0, r.y1))
                    segs.append((r.x1, r.y0, r.y1))
            elif item[0] == "qu":
                q = item[1]
                r = q.rect
                if r.height > 3:
                    segs.append((r.x0, r.y0, r.y1))
                    segs.append((r.x1, r.y0, r.y1))
    return segs


def _assign_layout(page: pymupdf.Page, elements: list[Element]) -> None:
    """Detecta alinhamento e espaço disponível (evita sobreposição com vizinhos e bordas)."""
    prect = to_unrot_rect(page, page.rect)
    horiz = [e for e in elements if abs(e.angle) < 0.5]
    boxes = {id(e): e.rect_unrot() for e in horiz}
    rules = _vertical_rules(page) if horiz else []
    for e in elements:
        if abs(e.angle) >= 0.5:
            e.max_extent = e.width * 1.6
            continue
        r = boxes[id(e)]
        x0, x1 = r.x0, r.x1
        cx = (x0 + x1) / 2
        votes = {"left": 0, "right": 0, "center": 0}
        right_limit = prect.x1 - 8
        left_limit = prect.x0 + 8
        mid_y0 = e.origin[1] - 0.6 * e.size
        mid_y1 = e.origin[1]
        for (sx, sy0, sy1) in rules:
            if sy1 < mid_y0 or sy0 > mid_y1:
                continue
            if sx >= x1 - 0.5:
                right_limit = min(right_limit, sx - 1.5)
            elif sx <= x0 + 0.5:
                left_limit = max(left_limit, sx + 1.5)
        for o in horiz:
            if o is e:
                continue
            orr = boxes[id(o)]
            same_band = not (orr.y1 <= r.y0 + 0.25 * e.size or orr.y0 >= r.y1 - 0.25 * e.size)
            if same_band:
                if orr.x0 >= x1 - 0.5:
                    right_limit = min(right_limit, orr.x0 - 0.35 * e.size)
                elif orr.x1 <= x0 + 0.5:
                    left_limit = max(left_limit, orr.x1 + 0.35 * e.size)
                continue
            if abs(orr.y0 - r.y0) > 300:
                continue
            same_left = abs(orr.x0 - x0) < 1.2
            same_right = abs(orr.x1 - x1) < 1.2
            if same_left and same_right:
                continue  # mesma largura: não indica alinhamento
            if same_left:
                votes["left"] += 1
            elif same_right:
                votes["right"] += 1
            elif abs((orr.x0 + orr.x1) / 2 - cx) < 1.2:
                votes["center"] += 1
        if votes["right"] > votes["left"] and votes["right"] >= votes["center"] and votes["right"] >= 1:
            e.align = "right"
        elif votes["center"] > votes["left"] and votes["center"] >= 2:
            e.align = "center"
        else:
            e.align = "left"
        if e.align == "left":
            e.max_extent = max(e.width, right_limit - x0)
        elif e.align == "right":
            e.max_extent = max(e.width, x1 - left_limit)
        else:
            half = min(cx - left_limit, right_limit - cx)
            e.max_extent = max(e.width, 2 * half)


def _style_key(e: Element) -> tuple:
    return (e.font_name, round(e.size, 1), tuple(round(c, 2) for c in e.color), e.bold, e.italic)


def group_paragraphs(elements: list[Element]) -> list[Element]:
    """Agrupa linhas consecutivas que formam um parágrafo (texto corrido).

    Critérios conservadores para não juntar colunas de tabela ou listas:
      * mesmo estilo, horizontal, mesma margem esquerda;
      * entrelinha regular (1,0–1,9× o tamanho da fonte);
      * a linha é a única coisa na sua faixa horizontal dentro da largura do parágrafo;
      * as linhas não finais ocupam quase toda a largura (texto que quebrou por falta de espaço).
    """
    horiz = [e for e in elements if e.kind == "native" and abs(e.angle) < 0.5 and not e.invisible_layer]
    if len(horiz) < 2:
        return elements
    horiz.sort(key=lambda e: (round(e.origin[1], 1), e.origin[0]))
    rects = {id(e): e.rect_unrot() for e in horiz}
    used: set[int] = set()
    groups: list[list[Element]] = []

    def alone_in_band(e: Element, x0: float, x1: float) -> bool:
        r = rects[id(e)]
        for o in horiz:
            if o is e:
                continue
            orr = rects[id(o)]
            if abs(o.origin[1] - e.origin[1]) < 0.4 * e.size and orr.x1 > x0 - 2 and orr.x0 < x1 + 2:
                return False
        return r is not None

    for i, e in enumerate(horiz):
        if id(e) in used:
            continue
        chain = [e]
        gap = None
        cur = e
        for f in horiz[i + 1:]:
            if id(f) in used or f is cur:
                continue
            dy = f.origin[1] - cur.origin[1]
            if dy <= 0.5 * cur.size:
                continue
            if dy > 2.0 * cur.size:
                break
            if _style_key(f) != _style_key(e) or abs(f.origin[0] - e.origin[0]) > 1.5:
                continue
            if not (1.0 * e.size <= dy <= 1.9 * e.size):
                break
            if gap is not None and abs(dy - gap) > 0.12 * gap:
                break
            gap = dy
            chain.append(f)
            cur = f
        if len(chain) < 2:
            continue
        width = max(c.width for c in chain)
        x0 = e.origin[0]
        x1 = x0 + width
        if not all(alone_in_band(c, x0, x1) for c in chain):
            continue
        body = chain[:-1]
        if any(c.width < 0.72 * width for c in body):
            continue
        if sum(len(c.text) for c in chain) < 40 or width < 12 * e.size:
            continue
        if len(chain) == 2:
            # duas linhas: só é parágrafo se a primeira for uma frase longa que continua na seguinte
            t0 = chain[0].text.rstrip()
            if len(t0) < 60 or t0[-1:] in ".:;!?":
                continue
        groups.append(chain)
        used.update(id(c) for c in chain)

    if not groups:
        return elements
    by_first = {id(g[0]): g for g in groups}
    out: list[Element] = []
    for e in elements:
        if id(e) in by_first:
            g = by_first[id(e)]
            width = max(c.width for c in g)
            ends = [c.origin[0] + c.width for c in g[:-1]]
            justify = len(g) > 2 and max(ends) - min(ends) < 1.0 and any(
                abs(c.width - width) < 1.0 for c in g[:-1])
            texts = []
            for k, c in enumerate(g):
                t = c.text
                if texts and texts[-1].endswith("-") and t[:1].islower():
                    texts[-1] = texts[-1][:-1]
                    texts[-1] += t
                else:
                    texts.append(t)
            gaps = [g[k + 1].origin[1] - g[k].origin[1] for k in range(len(g) - 1)]
            para = Element(
                id="", page=e.page, kind="native", text=" ".join(texts), origin=e.origin, angle=e.angle,
                width=width, size=e.size, color=e.color, font_name=e.font_name, flags=e.flags,
                bold=e.bold, italic=e.italic, serif=e.serif, mono=e.mono,
                char_rects=[r for c in g for r in c.char_rects],
                char_origins=[o for c in g for o in c.char_origins],
                lines=list(g), line_gap=sum(gaps) / len(gaps), justify=justify,
            )
            out.append(para)
        elif any(id(e) == id(c) for g in groups for c in g[1:]):
            continue
        else:
            out.append(e)
    return out


def analyze_page(page: pymupdf.Page) -> PageAnalysis:
    visible, invisible, garbage = extract_native_elements(page)
    n_vis_chars = sum(len(e.text) for e in visible)
    coverage = _image_coverage(page)
    notes: list[str] = []
    garbage_ratio = garbage / max(1, n_vis_chars)

    try:
        drawings = len(page.get_drawings()) if n_vis_chars < 10 else 0
    except Exception:
        drawings = 0

    if invisible and coverage > 0.3:
        mode = "ocr_layer"
        notes.append("Página digitalizada com camada de texto invisível (OCR prévio): "
                     "as edições usam reconstrução visual da imagem.")
        for e in invisible:
            e.kind = "ocr"
            e.region = tuple(e.rect_unrot())
        elements = visible + invisible
        needs_ocr = False
    elif garbage_ratio > 0.3:
        mode = "scanned"
        notes.append("O texto nativo desta página não possui mapeamento Unicode legível; "
                     "o reconhecimento óptico (OCR) será utilizado.")
        elements = []
        needs_ocr = True
    elif n_vis_chars >= 10 or (n_vis_chars > 0 and coverage < 0.5):
        mode = "native" if coverage < 0.5 else "mixed"
        elements = visible
        needs_ocr = False
        if mode == "mixed":
            notes.append("Página com texto nativo sobre imagem. Use 'Reconhecer texto (OCR)' "
                         "para editar textos que fazem parte da imagem.")
    elif coverage > 0.05 or drawings > 20:
        mode = "scanned"
        elements = visible
        needs_ocr = True
    else:
        mode = "empty" if not visible else "native"
        elements = visible
        needs_ocr = False

    # Texto que pertence a anotações/campos (aparência de anotação) não é conteúdo da página:
    # campos são editados como formulário; anotações ficam intactas.
    annot_rects = []
    try:
        for an in page.annots() or []:
            annot_rects.append(pymupdf.Rect(an.rect))
        for w in page.widgets() or []:
            annot_rects.append(pymupdf.Rect(w.rect))
    except Exception:
        pass
    if annot_rects:
        def inside_annot(e: Element) -> bool:
            r = e.rect_unrot()
            return any(abs(r & ar) > 0.6 * abs(r) for ar in annot_rects if abs(r) > 0)
        elements = [e for e in elements if not inside_annot(e)]

    # Trechos cuja fonte não tem mapeamento Unicode (caracteres-lixo): edição por reconstrução
    # visual e texto obtido por OCR da própria região.
    for e in elements:
        if e.kind == "native" and any(_is_garbage(ch) or ord(ch) == 0xFFFF for ch in e.text):
            e.kind = "ocr"
            e.text_unreliable = True
            e.region = tuple(e.rect_unrot())
    elements = group_paragraphs(elements)
    for i, e in enumerate(elements):
        e.id = f"p{page.number}-{'b' if e.is_paragraph else 'n'}{i}"
    _assign_layout(page, elements)
    return PageAnalysis(
        number=page.number, width=page.rect.width, height=page.rect.height, rotation=page.rotation,
        mode=mode, needs_ocr=needs_ocr, ocr_done=False, elements=elements, notes=notes,
        image_coverage=round(coverage, 3), images=_image_elements(page), fields=_form_fields(page),
    )


FIELD_TYPES = {
    pymupdf.PDF_WIDGET_TYPE_TEXT: "text",
    pymupdf.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
    pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    pymupdf.PDF_WIDGET_TYPE_COMBOBOX: "choice",
    pymupdf.PDF_WIDGET_TYPE_LISTBOX: "choice",
}


def _form_fields(page: pymupdf.Page) -> list[dict]:
    out: list[dict] = []
    try:
        widgets = list(page.widgets() or [])
    except Exception:
        return out
    for w in widgets:
        kind = FIELD_TYPES.get(w.field_type)
        if kind is None:
            continue
        r = (pymupdf.Rect(w.rect) * page.rotation_matrix).normalize()
        item = {
            "id": f"p{page.number}-f{w.xref}",
            "xref": w.xref,
            "name": w.field_name or "",
            "type": kind,
            "value": w.field_value if not isinstance(w.field_value, bool) else ("Yes" if w.field_value else "Off"),
            "bbox": [round(v, 2) for v in r],
            "readOnly": bool(w.field_flags & 1),
            "multiline": bool(kind == "text" and w.field_flags & (1 << 12)),
            "size": round(w.text_fontsize or 0, 2),
        }
        if kind == "choice":
            item["options"] = [o if isinstance(o, str) else o[0] for o in (w.choice_values or [])]
        if kind in ("checkbox", "radio"):
            try:
                item["onState"] = w.on_state() or "Yes"
            except Exception:
                item["onState"] = "Yes"
            item["value"] = "Off" if str(w.field_value) in ("Off", "False", "") else str(w.field_value)
        out.append(item)
    return out


def _image_elements(page: pymupdf.Page) -> list[dict]:
    """Imagens existentes que podem ser selecionadas/excluídas (exclui fundos de página inteira)."""
    out: list[dict] = []
    try:
        infos = page.get_image_info()
    except Exception:
        return out
    page_area = abs(page.rect)
    rects = []
    for info in infos:
        r = (pymupdf.Rect(info["bbox"]) * page.rotation_matrix).normalize() & page.rect
        rects.append(r)
    for idx, r in enumerate(rects):
        if r.is_empty or abs(r) < 16 or abs(r) > 0.7 * page_area:
            continue
        c = (r.tl + r.br) / 2
        shared = any(j != idx and c in o for j, o in enumerate(rects))
        out.append({"id": f"p{page.number}-i{idx}", "bbox": [round(v, 2) for v in r], "shared": shared})
    return out


def element_to_json(page: pymupdf.Page, e: Element) -> dict:
    """Representação enviada ao frontend (coordenadas de exibição)."""
    q = [p * page.rotation_matrix for p in e.all_points_unrot()]
    r = pymupdf.Rect(q[0], q[0])
    for p in q:
        r |= p
    # Ângulo do texto na exibição (0 = horizontal legível)
    dx, dy = e.dir
    v = pymupdf.Point(dx, dy) * pymupdf.Matrix(page.rotation_matrix.a, page.rotation_matrix.b,
                                               page.rotation_matrix.c, page.rotation_matrix.d, 0, 0)
    display_angle = round(math.degrees(math.atan2(-v.y, v.x)), 2)
    return {
        "id": e.id,
        "page": e.page,
        "kind": e.kind,
        "text": e.text,
        "bbox": [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)],
        "size": round(e.size, 2),
        "color": rgb_to_hex(e.color),
        "font": e.font_name,
        "bold": e.bold,
        "italic": e.italic,
        "serif": e.serif,
        "mono": e.mono,
        "align": "justify" if e.is_paragraph and e.justify else e.align,
        "angle": display_angle,
        "maxExtent": round(e.max_extent, 2),
        "width": round(e.width, 2),
        "bgColor": rgb_to_hex(e.bg_color) if e.bg_color else None,
        "multiline": e.is_paragraph,
        "lineHeight": round(e.line_gap, 2) if e.is_paragraph else None,
    }


def analysis_to_json(page: pymupdf.Page, a: PageAnalysis) -> dict:
    return {
        "number": a.number,
        "width": round(a.width, 2),
        "height": round(a.height, 2),
        "rotation": a.rotation,
        "mode": a.mode,
        "needsOcr": a.needs_ocr,
        "ocrDone": a.ocr_done,
        "notes": a.notes,
        "elements": [element_to_json(page, e) for e in a.elements],
        "images": [{"id": im["id"], "bbox": im["bbox"]} for im in a.images],
        "fields": a.fields,
    }


def element_asdict(e: Element) -> dict:
    return asdict(e)
