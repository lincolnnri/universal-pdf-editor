"""Modo B — OCR (Tesseract) para páginas digitalizadas ou achatadas.

Detecta palavras e linhas com coordenadas, agrupa em elementos editáveis e estima o estilo
visual (tamanho, cor do texto, cor de fundo, negrito) a partir dos pixels da imagem.
"""
from __future__ import annotations

import csv
import io
import math
import os
import shutil
import subprocess
from collections import defaultdict

import cv2
import numpy as np
import pymupdf

from ..config import settings
from .analysis import Element, PageAnalysis, _assign_layout, to_unrot_rect

DESCENDERS = set("gjpqyQç,;")


class OCRUnavailable(RuntimeError):
    pass


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _run_tesseract(png: bytes, langs: str, psm: str = "3") -> list[dict]:
    if not tesseract_available():
        raise OCRUnavailable("Tesseract OCR não está instalado no servidor.")
    env = dict(os.environ)
    if settings.tessdata_dir:
        env["TESSDATA_PREFIX"] = settings.tessdata_dir
    cmd = ["tesseract", "stdin", "stdout", "-l", langs, "--psm", psm, "-c", "preserve_interword_spaces=1", "-c", "tessedit_create_tsv=1"]
    try:
        proc = subprocess.run(cmd, input=png, capture_output=True, timeout=settings.ocr_timeout_s, env=env)
    except subprocess.TimeoutExpired as exc:
        raise OCRUnavailable("O OCR excedeu o tempo limite para esta página.") from exc
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        # idioma ausente → tenta somente inglês
        if langs != "eng" and any("Failed loading language" in m or "Error opening data file" in m for m in msg):
            return _run_tesseract(png, "eng", psm)
        raise OCRUnavailable("Falha no OCR: " + (msg[-1] if msg else "erro desconhecido"))
    rows = list(csv.DictReader(io.StringIO(proc.stdout.decode("utf-8", "replace")), delimiter="\t",
                               quoting=csv.QUOTE_NONE))
    return rows


def _style_from_pixels(rgb: np.ndarray, box: tuple[int, int, int, int]) -> dict:
    """Estima cor do texto, cor de fundo, uniformidade do fundo e espessura do traço."""
    h_img, w_img = rgb.shape[:2]
    x0, y0, x1, y1 = box
    pad = max(2, (y1 - y0) // 4)
    ex0, ey0 = max(0, x0 - pad), max(0, y0 - pad)
    ex1, ey1 = min(w_img, x1 + pad), min(h_img, y1 + pad)
    crop = rgb[ey0:ey1, ex0:ex1].astype(np.float32)
    if crop.size == 0:
        return {"color": (0, 0, 0), "bg": (1, 1, 1), "uniform": True, "bold": False}
    ring = np.concatenate([crop[0, :], crop[-1, :], crop[:, 0], crop[:, -1]])
    bg = np.median(ring, axis=0)
    diff = np.linalg.norm(crop - bg, axis=2)
    diff8 = np.clip(diff, 0, 255).astype(np.uint8)
    thr, _ = cv2.threshold(diff8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = diff8 > max(thr, 40)
    if mask.sum() < 4:
        return {"color": (0.0, 0.0, 0.0), "bg": tuple(float(v) for v in bg / 255),
                "uniform": float(ring.std()) < 12, "bold": False}
    ink = crop[mask]
    # núcleo do traço: pixels mais distantes do fundo (evita bordas suavizadas)
    d = diff[mask]
    core = ink[d >= np.percentile(d, 60)]
    color = np.median(core, axis=0)
    # espessura do traço ≈ área / comprimento do esqueleto
    bold = False
    try:
        m8 = (mask.astype(np.uint8) * 255)
        skel = cv2.ximgproc.thinning(m8)
        length = max(1, int((skel > 0).sum()))
        stroke = mask.sum() / length
        bold = stroke / max(1, (y1 - y0)) > 0.115
    except Exception:
        bold = mask.mean() > 0.32
    return {"color": tuple(float(v) for v in color / 255), "bg": tuple(float(v) for v in bg / 255),
            "uniform": float(ring.std()) < 12, "bold": bool(bold)}


def ocr_regions(page: pymupdf.Page, analysis: PageAnalysis) -> int:
    """Obtém por OCR o texto de trechos nativos sem Unicode válido (fontes sem /ToUnicode)."""
    targets = [e for e in analysis.elements if e.text_unreliable and not getattr(e, "_ocr_text_done", False)]
    if not targets or not tesseract_available():
        return 0
    scale = settings.ocr_dpi / 72.0
    done = 0
    for e in targets[:200]:
        r = pymupdf.Rect(e.region) * page.rotation_matrix
        r = r.normalize() + (-2, -2, 2, 2)
        pix = page.get_pixmap(dpi=settings.ocr_dpi, clip=r, alpha=False, annots=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        gray = cv2.copyMakeBorder(gray, 20, 20, 20, 20, cv2.BORDER_REPLICATE)
        ok, png = cv2.imencode(".png", gray)
        if not ok:
            continue
        try:
            rows = _run_tesseract(png.tobytes(), settings.ocr_languages, psm="7")
        except OCRUnavailable:
            break
        words = [(r_.get("text") or "").strip() for r_ in rows if r_.get("level") == "5"]
        text = " ".join(w for w in words if w)
        if text:
            e.text = text
            done += 1
        e._ocr_text_done = True  # type: ignore[attr-defined]
    _ = scale
    return done


def estimate_skew(gray: np.ndarray, max_angle: float = 6.0) -> float:
    """Ângulo (graus) que endireita o texto, por análise de projeção horizontal."""
    h, w = gray.shape
    f = 1200.0 / max(w, 1)
    small = cv2.resize(gray, (max(1, int(w * f)), max(1, int(h * f))), interpolation=cv2.INTER_AREA) if f < 1 else gray
    _t, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if bw.mean() < 0.5:  # página praticamente vazia
        return 0.0
    sh, sw = bw.shape
    center = (sw / 2, sh / 2)

    def score(a: float) -> float:
        m = cv2.getRotationMatrix2D(center, a, 1.0)
        r = cv2.warpAffine(bw, m, (sw, sh), flags=cv2.INTER_NEAREST, borderValue=0)
        prof = r.sum(axis=1).astype(np.float64)
        return float(np.var(prof))

    best = max(np.arange(-max_angle, max_angle + 0.01, 0.5), key=score)
    fine = max(np.arange(best - 0.5, best + 0.51, 0.1), key=score)
    return round(float(fine), 2) if abs(fine) >= 0.3 else 0.0


def ocr_page(page: pymupdf.Page, analysis: PageAnalysis, exclude_native: bool = True) -> PageAnalysis:
    """Executa OCR na página e adiciona elementos 'ocr' à análise."""
    dpi = settings.ocr_dpi
    scale = dpi / 72.0
    pix = page.get_pixmap(dpi=dpi, alpha=False, annots=False)
    rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Pré-processamento: realce de contraste para digitalizações apagadas e correção de inclinação
    if float(gray.std()) < 40:
        gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    skew = estimate_skew(gray)
    inv = None
    if abs(skew) >= 0.3:
        h_, w_ = gray.shape
        m = cv2.getRotationMatrix2D((w_ / 2, h_ / 2), skew, 1.0)
        gray = cv2.warpAffine(gray, m, (w_, h_), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        rgb = cv2.warpAffine(rgb, m, (w_, h_), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        inv = cv2.invertAffineTransform(m)
        analysis.notes.append(f"Página inclinada {skew:+.1f}°: inclinação corrigida para o reconhecimento.")

    def back(x: float, y: float) -> pymupdf.Point:
        """Pixel da imagem endireitada → ponto (pt) no espaço de exibição da página."""
        if inv is not None:
            x, y = inv[0, 0] * x + inv[0, 1] * y + inv[0, 2], inv[1, 0] * x + inv[1, 1] * y + inv[1, 2]
        return pymupdf.Point(x / scale, y / scale)

    ok, png = cv2.imencode(".png", gray)
    if not ok:
        raise OCRUnavailable("Não foi possível preparar a imagem para OCR.")
    rows = _run_tesseract(png.tobytes(), settings.ocr_languages)

    lines: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("level") != "5":
            continue
        text = (r.get("text") or "").strip()
        try:
            conf = float(r.get("conf", -1))
        except ValueError:
            conf = -1
        if not text or conf < 0:
            continue
        lines[(r["block_num"], r["par_num"], r["line_num"])].append({
            "text": text, "conf": conf,
            "x0": int(r["left"]), "y0": int(r["top"]),
            "x1": int(r["left"]) + int(r["width"]), "y1": int(r["top"]) + int(r["height"]),
        })

    font_ref = pymupdf.Font("helv")
    native_rects = [e.rect_unrot() for e in analysis.elements if e.kind == "native"] if exclude_native else []
    new_elems: list[Element] = []
    for words in lines.values():
        words.sort(key=lambda w: w["x0"])
        lh = float(np.median([w["y1"] - w["y0"] for w in words]))
        groups: list[list[dict]] = [[words[0]]]
        for w in words[1:]:
            if w["x0"] - groups[-1][-1]["x1"] > 1.3 * lh:
                groups.append([w])
            else:
                groups[-1].append(w)
        for g in groups:
            text = " ".join(w["text"] for w in g)
            conf = float(np.mean([w["conf"] for w in g]))
            if conf < 35 and len(text) <= 2:
                continue
            x0 = min(w["x0"] for w in g)
            y0 = min(w["y0"] for w in g)
            x1 = max(w["x1"] for w in g)
            y1 = max(w["y1"] for w in g)
            st = _style_from_pixels(rgb, (x0, y0, x1, y1))
            # medidas no espaço endireitado (pt)
            dx0, dy0, dx1, dy1 = x0 / scale, y0 / scale, x1 / scale, y1 / scale
            width = dx1 - dx0
            height = dy1 - dy0
            has_desc = any(c in DESCENDERS for c in text)
            size_h = height / (0.73 + (0.21 if has_desc else 0.0))
            ref_len = font_ref.text_length(text, fontsize=1)
            size_w = width / ref_len if ref_len > 0 else size_h
            size = size_w if 0.75 * size_h <= size_w <= 1.3 * size_h else size_h
            size = max(4.0, min(size, 200.0))
            base_px = y1 - (0.21 * size * scale if has_desc else 0.0)
            # volta para a página original (desfaz a correção de inclinação)
            p_start = back(x0, base_px)
            p_end = back(x1, base_px)
            corners = [back(x0 - scale, y0 - scale), back(x1 + scale, y0 - scale),
                       back(x1 + scale, y1 + scale), back(x0 - scale, y1 + scale)]
            region_disp = pymupdf.Rect(corners[0], corners[0])
            for c in corners:
                region_disp |= c
            region = to_unrot_rect(page, region_disp)
            if any(abs(region & nr) > 0.3 * abs(region) for nr in native_rects):
                continue
            disp_angle = math.degrees(math.atan2(-(p_end.y - p_start.y), p_end.x - p_start.x))
            origin = p_start * page.derotation_matrix
            el = Element(
                id="", page=page.number, kind="ocr", text=text, origin=(origin.x, origin.y),
                angle=round((page.rotation + disp_angle) % 360, 3), width=width, size=round(size, 2),
                color=st["color"], font_name="Helvetica-Bold" if st["bold"] else "Helvetica", flags=16 if st["bold"] else 0,
                bold=st["bold"], italic=False, serif=False, mono=False,
                region=tuple(region), bg_color=st["bg"],
            )
            new_elems.append(el)

    base = len(analysis.elements)
    for i, e in enumerate(new_elems):
        e.id = f"p{page.number}-o{base + i}"
    _assign_layout(page, new_elems)
    analysis.elements.extend(new_elems)
    analysis.ocr_done = True
    analysis.needs_ocr = False
    if new_elems:
        analysis.notes.append(
            f"OCR reconheceu {len(new_elems)} trecho(s) de texto. Edições nesses trechos usam "
            "reconstrução visual da região (o texto da imagem é apagado e reescrito).")
    else:
        analysis.notes.append("O OCR não encontrou texto legível nesta página.")
    return analysis
