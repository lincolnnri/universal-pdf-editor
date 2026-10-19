"""Reaproveitamento direto do objeto de fonte original do PDF.

Documentos gerados por sistemas (contas, boletos, relatórios) quase sempre usam fontes
*subset* com codificação Identity-H, cujo programa de fonte não tem tabela cmap — elas não
podem ser carregadas como fontes comuns. Mas o próprio PDF informa, via /ToUnicode, qual
código corresponde a cada caractere. Este módulo inverte esse mapa e escreve o novo texto
referenciando o MESMO objeto de fonte da página, com os mesmos glifos. O resultado é
visualmente idêntico ao original (mesmo desenho de letra) e continua pesquisável.

Só é utilizado quando todos os caracteres do novo texto existem no subset.
"""
from __future__ import annotations

import io
import re

import pymupdf

import logging

logging.getLogger("fontTools").setLevel(logging.ERROR)

try:
    from fontTools.ttLib import TTFont
except ImportError:  # pragma: no cover
    TTFont = None

_HEX = re.compile(r"<([0-9A-Fa-f]*)>")
_TOKEN = re.compile(r"\[|\]|-?\d+(?:\.\d+)?")


def _utf16(hexstr: str) -> str | None:
    try:
        b = bytes.fromhex(hexstr)
        s = b.decode("utf-16-be")
        return s
    except Exception:
        return None


def parse_tounicode(data: bytes) -> tuple[dict[int, str], int]:
    """Retorna (código → texto, nº de bytes por código)."""
    text = data.decode("latin-1", "replace")
    mapping: dict[int, str] = {}
    nbytes = 1
    for block in re.findall(r"begincodespacerange(.*?)endcodespacerange", text, re.S):
        codes = _HEX.findall(block)
        if codes:
            nbytes = max(nbytes, max(len(c) for c in codes) // 2)
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        items = _HEX.findall(block)
        for src, dst in zip(items[0::2], items[1::2]):
            u = _utf16(dst)
            if u is not None and src:
                mapping[int(src, 16)] = u
                nbytes = max(nbytes, len(src) // 2)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        # cada entrada: <lo> <hi> <dst>  ou  <lo> <hi> [<d1> <d2> ...]
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f]*>)", block):
            lo, hi, dst = int(m.group(1), 16), int(m.group(2), 16), m.group(3)
            nbytes = max(nbytes, len(m.group(1)) // 2)
            if hi - lo > 65535:
                continue
            if dst.startswith("["):
                for i, d in enumerate(_HEX.findall(dst)):
                    u = _utf16(d)
                    if u is not None:
                        mapping[lo + i] = u
            else:
                base = _utf16(dst[1:-1])
                if not base:
                    continue
                for i in range(hi - lo + 1):
                    mapping[lo + i] = base[:-1] + chr(ord(base[-1]) + i)
    return mapping, nbytes


def _array(doc: pymupdf.Document, xref: int, key: str) -> list:
    t, v = doc.xref_get_key(xref, key)
    if t == "xref":
        v = doc.xref_object(int(v.split()[0]))
    if t in ("null", "") or not v:
        return []
    out: list = []
    stack = [out]
    for tok in _TOKEN.findall(v):
        if tok == "[":
            new: list = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == "]":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].append(float(tok))
    return out[0] if len(out) == 1 and isinstance(out[0], list) else out


def _num(doc: pymupdf.Document, xref: int, key: str, default: float) -> float:
    t, v = doc.xref_get_key(xref, key)
    try:
        return float(v) if t in ("int", "float") else default
    except ValueError:
        return default


def descendant_xref(doc: pymupdf.Document, xref: int) -> int | None:
    """xref do CIDFont descendente (aceita array direto ou referência indireta a um array)."""
    t, desc = doc.xref_get_key(xref, "DescendantFonts")
    if t == "xref":
        try:
            obj = doc.xref_object(int(desc.split()[0]))
        except Exception:
            return None
        if obj.strip().startswith("<<"):
            return int(desc.split()[0])   # referência direta ao dicionário
        desc = obj
    m = re.search(r"(\d+) 0 R", desc or "")
    return int(m.group(1)) if m else None


class PdfFontReuse:
    """Escreve texto usando um objeto de fonte já existente no PDF."""

    def __init__(self, doc: pymupdf.Document, xref: int):
        self.doc = doc
        self.xref = xref
        self.ok = False
        self.code_for: dict[str, int] = {}
        self.widths: dict[int, float] = {}
        self.default_width = 0.0
        self.nbytes = 1
        try:
            self._load()
        except Exception:
            self.ok = False

    def _load(self) -> None:
        doc, xref = self.doc, self.xref
        subtype = doc.xref_get_key(xref, "Subtype")[1]
        t, tu = doc.xref_get_key(xref, "ToUnicode")
        if t == "xref":
            mapping, nbytes = parse_tounicode(doc.xref_stream(int(tu.split()[0])) or b"")
        else:
            mapping, nbytes = {}, 2
        if not mapping and subtype == "/Type0":
            # sem /ToUnicode: aprende código→caractere a partir do texto que o MuPDF extrai da página
            mapping = self._learn_mapping()
        if not mapping:
            return
        if subtype == "/Type0":
            enc = doc.xref_get_key(xref, "Encoding")[1]
            if enc not in ("/Identity-H",):
                return  # vertical/CMaps personalizados: não suportado
            self.nbytes = 2
            dx = descendant_xref(doc, xref)
            if dx is None:
                return
            self.default_width = _num(doc, dx, "DW", 1000.0) / 1000.0
            w = _array(doc, dx, "W")
            i = 0
            while i < len(w):
                first = w[i]
                if i + 1 < len(w) and isinstance(w[i + 1], list):
                    for k, val in enumerate(w[i + 1]):
                        self.widths[int(first) + k] = val / 1000.0
                    i += 2
                elif i + 2 < len(w):
                    for c in range(int(first), int(w[i + 1]) + 1):
                        self.widths[c] = w[i + 2] / 1000.0
                    i += 3
                else:
                    break
        elif subtype in ("/TrueType", "/Type1", "/MMType1"):
            self.nbytes = 1
            if nbytes != 1:
                return
            first = int(_num(doc, xref, "FirstChar", 0))
            for k, val in enumerate(_array(doc, xref, "Widths")):
                if isinstance(val, float):
                    self.widths[first + k] = val / 1000.0
            if not self.widths:
                return
        else:
            return
        present = self._present_codes(subtype, mapping)
        if present is None:
            return
        for code, u in sorted(mapping.items()):
            if len(u) != 1:
                continue
            # só aceita códigos com largura conhecida e glifo realmente desenhado no subset
            w = self.widths.get(code, self.default_width if self.nbytes == 2 else 0.0)
            if w <= 0 and u != " ":
                continue
            if u != " " and code not in present:
                continue
            self.code_for.setdefault(u, code)
        # Vários códigos podem mapear o mesmo caractere (variantes de glifo): prefere o código
        # realmente usado no documento, para reproduzir exatamente o mesmo desenho.
        if subtype == "/Type0" and self.code_for:
            valid = set(self.code_for.values()) | {c for c, u in mapping.items() if len(u) == 1
                                                   and (u == " " or c in present)}
            for u, code in self._used_codes().items():
                if code in valid and mapping.get(code) == u:
                    self.code_for[u] = code
        self.ok = bool(self.code_for)

    def _font_spans(self):
        """Spans (texttrace) que usam esta fonte, nas primeiras páginas onde ela aparece."""
        doc = self.doc
        base = doc.xref_get_key(self.xref, "BaseFont")[1].lstrip("/")
        short = base.split("+", 1)[-1]
        for pno in range(min(doc.page_count, 40)):
            page = doc[pno]
            try:
                if not any(f[0] == self.xref for f in page.get_fonts()):
                    continue
                spans = page.get_texttrace()
            except Exception:
                continue
            for sp in spans:
                name = str(sp.get("font"))
                nm = name.split("+", 1)[-1]
                if nm == short or (len(nm) >= 6 and (short.startswith(nm) or nm.startswith(short))):
                    yield sp

    def _gid2cid(self) -> dict[int, int] | None | bool:
        """Mapa gid→CID (None = identidade; False = desconhecido)."""
        dx = descendant_xref(self.doc, self.xref)
        if dx is not None and self.doc.xref_get_key(dx, "Subtype")[1] == "/CIDFontType0":
            try:
                from fontTools.cffLib import CFFFontSet
                _n, _e, _t, buf = self.doc.extract_font(self.xref)
                cff = CFFFontSet()
                cff.decompile(io.BytesIO(buf), None)
                top = cff[cff.fontNames[0]]
                if not hasattr(top, "ROS"):
                    return None
                out = {}
                for gid, name in enumerate(top.charset):
                    if name.startswith("cid"):
                        out[gid] = int(name[3:])
                    elif name == ".notdef":
                        out[gid] = 0
                return out
            except Exception:
                return False
        cid2gid = self._cid_to_gid()
        if cid2gid is False:
            return False
        return {g: c for c, g in cid2gid.items()} if cid2gid else None

    def _used_codes(self) -> dict[str, int]:
        gid2cid = self._gid2cid()
        if gid2cid is False:
            return {}
        counts: dict[str, dict[int, int]] = {}
        for sp in self._font_spans():
            for ch in sp.get("chars", ()):
                uni, gid = ch[0], ch[1]
                if uni <= 0 or gid < 0:
                    continue
                code = gid2cid.get(gid) if gid2cid is not None else gid
                if code is None:
                    continue
                d = counts.setdefault(chr(uni), {})
                d[code] = d.get(code, 0) + 1
        return {u: max(d, key=d.get) for u, d in counts.items()}

    def _present_codes(self, subtype: str, mapping: dict[int, str]) -> set[int] | None:
        """Códigos cujo glifo existe (com contorno) no programa de fonte incorporado.

        Subsets frequentemente mantêm a tabela de larguras e o /ToUnicode completos, mas
        removem o desenho dos glifos não usados — escrever com eles produziria texto invisível.
        """
        if TTFont is None:
            return None
        try:
            _n, ext, _t, buf = self.doc.extract_font(self.xref)
        except Exception:
            return None
        if not buf or ext not in ("ttf", "otf", "cff", "cid"):
            return None
        if ext in ("cff", "cid"):
            return self._present_codes_cff(buf, subtype, mapping)
        try:
            font = TTFont(io.BytesIO(buf), lazy=True)
            order = font.getGlyphOrder()
        except Exception:
            return None
        present: set[int] = set()
        if subtype == "/Type0":
            cid2gid = self._cid_to_gid()
            if cid2gid is False:
                return None
            if "glyf" in font:
                glyf = font["glyf"]
                for code in mapping:
                    gid = cid2gid.get(code, code) if cid2gid else code
                    if 0 < gid < len(order):
                        g = glyf[order[gid]]
                        if g.numberOfContours != 0:
                            present.add(code)
            elif "CFF " in font:
                cff = font["CFF "].cff
                top = cff[cff.fontNames[0]]
                cs = top.CharStrings
                is_cid = hasattr(top, "ROS")
                for code in mapping:
                    name = f"cid{code:05d}" if is_cid else (order[code] if code < len(order) else None)
                    if name and name in cs:
                        c = cs[name]
                        c.decompile()
                        if len(c.program) > 1:
                            present.add(code)
            else:
                return None
        else:
            if "glyf" not in font or "cmap" not in font:
                return None
            glyf = font["glyf"]
            cmaps = [t for t in font["cmap"].tables if t.platformID in (3, 1)]
            for code in mapping:
                for t in cmaps:
                    name = t.cmap.get(code) or t.cmap.get(0xF000 + code)
                    if name and name in glyf and glyf[name].numberOfContours != 0:
                        present.add(code)
                        break
        return present

    def _learn_mapping(self) -> dict[int, str]:
        doc = self.doc
        if doc.xref_get_key(self.xref, "Encoding")[1] != "/Identity-H":
            return {}
        dx = descendant_xref(doc, self.xref)
        if dx is None or doc.xref_get_key(dx, "Subtype")[1] not in ("/CIDFontType2", "/CIDFontType0"):
            return {}
        gid2cid = self._gid2cid()
        if gid2cid is False:
            return {}
        mapping: dict[int, str] = {}
        conflicts: set[int] = set()
        for sp in self._font_spans():
            for ch in sp.get("chars", ()):
                uni, gid = ch[0], ch[1]
                if uni <= 0 or gid < 0 or uni == 0xFFFD:
                    continue
                code = gid2cid.get(gid) if gid2cid is not None else gid
                if code is None:
                    continue
                prev = mapping.get(code)
                if prev is not None and prev != chr(uni):
                    conflicts.add(code)
                mapping[code] = chr(uni)
        for c in conflicts:
            mapping.pop(c, None)
        return mapping

    def _present_codes_cff(self, buf: bytes, subtype: str, mapping: dict[int, str]) -> set[int] | None:
        """CFF puro (FontFile3): lê os CharStrings e confirma que o glifo tem desenho."""
        try:
            from fontTools.cffLib import CFFFontSet
            cff = CFFFontSet()
            cff.decompile(io.BytesIO(buf), None)
            top = cff[cff.fontNames[0]]
            cs = top.CharStrings
            is_cid = hasattr(top, "ROS")
            charset = top.charset
        except Exception:
            return None
        present: set[int] = set()
        cid2gid = self._cid_to_gid() if subtype == "/Type0" else {}
        if cid2gid is False:
            return None
        for code in mapping:
            if subtype == "/Type0":
                if is_cid:
                    name = f"cid{code:05d}"
                else:
                    gid = cid2gid.get(code, code) if cid2gid else code
                    name = charset[gid] if 0 <= gid < len(charset) else None
            else:
                return None   # fontes simples CFF: codificação depende de /Encoding — não arriscar
            if name and name in cs:
                try:
                    c = cs[name]
                    c.decompile()
                    if len(c.program) > 1:
                        present.add(code)
                except Exception:
                    continue
        return present

    def _cid_to_gid(self):
        doc = self.doc
        dx = descendant_xref(doc, self.xref)
        if dx is None:
            return False
        t, v = doc.xref_get_key(dx, "CIDToGIDMap")
        if t in ("null", "") or v == "/Identity":
            return {}
        if t == "xref":
            data = doc.xref_stream(int(v.split()[0])) or b""
            return {i // 2: (data[i] << 8) | data[i + 1] for i in range(0, len(data) - 1, 2)}
        return False

    def covers(self, text: str) -> bool:
        return self.ok and all(ch in self.code_for for ch in text if ch not in "\n\r")

    def advance(self, ch: str) -> float:
        code = self.code_for[ch]
        return self.widths.get(code, self.default_width)

    def text_length(self, text: str, fontsize: float) -> float:
        return sum(self.advance(ch) for ch in text) * fontsize

    def encode(self, text: str) -> str:
        fmt = "%04X" if self.nbytes == 2 else "%02X"
        return "".join(fmt % self.code_for[ch] for ch in text)

    def write(self, page: pymupdf.Page, origin: pymupdf.Point, text: str, size: float,
              color: tuple[float, float, float], angle: float, hscale: float, render_mode: int = 0,
              line_width: float = 1.0, stroke_color: tuple | None = None) -> None:
        """Acrescenta o texto ao content stream da página referenciando o objeto de fonte original."""
        doc = page.parent
        res_name = f"UPE{self.xref}"
        # registra a fonte nos recursos da página (sem duplicar o objeto de fonte)
        _ensure_font_resource(doc, page, res_name, self.xref)
        # Coordenadas: espaço unrot (y para baixo) → espaço PDF
        to_pdf = ~page.transformation_matrix
        o = origin * to_pdf
        rot = pymupdf.Matrix(float(angle))  # rotação no espaço y-baixo
        # direção do texto no espaço PDF (y para cima)
        d = pymupdf.Point(1, 0) * pymupdf.Matrix(rot.a, rot.b, rot.c, rot.d, 0, 0)
        d = pymupdf.Point(d.x, d.y) * pymupdf.Matrix(to_pdf.a, to_pdf.b, to_pdf.c, to_pdf.d, 0, 0)
        n = (d.x ** 2 + d.y ** 2) ** 0.5 or 1.0
        cx, sx = d.x / n, d.y / n
        # matriz de texto: eixo x ao longo da direção, eixo y perpendicular (para cima)
        tm = (cx * hscale, sx * hscale, -sx, cx, o.x, o.y)
        r, g, b = color
        sr, sg, sb = stroke_color or color
        mode = render_mode if render_mode in (0, 1, 2) else 0
        content = (
            "q %.4f w BT /%s %.4f Tf %.4f %.4f %.4f rg %.4f %.4f %.4f RG %d Tr 0 Tc 0 Tw 100 Tz "
            "%.6f %.6f %.6f %.6f %.4f %.4f Tm <%s> Tj ET Q\n"
            % (line_width if mode else 1.0, res_name, size, r, g, b, sr, sg, sb, mode, *tm,
               self.encode(text))
        ).encode("latin-1")
        if not page.is_wrapped:
            page.wrap_contents()
        new_xref = doc.get_new_xref()
        doc.update_object(new_xref, "<<>>")
        doc.update_stream(new_xref, content)
        contents = page.get_contents()
        doc.xref_set_key(page.xref, "Contents", "[" + " ".join(f"{x} 0 R" for x in contents + [new_xref]) + "]")


def _ensure_font_resource(doc: pymupdf.Document, page: pymupdf.Page, name: str, font_xref: int) -> None:
    t, res = doc.xref_get_key(page.xref, "Resources")
    if t == "xref":
        rx = int(res.split()[0])
        holder, prefix = rx, ""
    else:
        if t in ("null", ""):
            doc.xref_set_key(page.xref, "Resources", "<<>>")
        holder, prefix = page.xref, "Resources/"
    ft, fonts = doc.xref_get_key(holder, prefix + "Font")
    if ft == "xref":
        doc.xref_set_key(int(fonts.split()[0]), name, f"{font_xref} 0 R")
    else:
        if ft in ("null", ""):
            doc.xref_set_key(holder, prefix + "Font", "<<>>")
        doc.xref_set_key(holder, prefix + "Font/" + name, f"{font_xref} 0 R")


def page_font_xrefs(page: pymupdf.Page) -> list[tuple[int, str]]:
    """(xref, nome sem subset) de todas as fontes usadas na página (inclusive em XObjects)."""
    out = []
    try:
        for f in page.get_fonts(full=True):
            out.append((f[0], re.sub(r"^[A-Z]{6}\+", "", f[3] or "")))
    except Exception:
        pass
    return out
