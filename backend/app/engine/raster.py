"""Modo C — reconstrução visual controlada de regiões.

Usado quando o texto não existe como texto nativo (digitalizações, texto achatado em imagem,
texto desenhado como vetores). A região é renderizada em alta resolução, os pixels de "tinta"
são removidos por inpainting (OpenCV) e o resultado é inserido como um remendo exatamente
sobre a região original. O novo texto é então escrito como texto vetorial real.
"""
from __future__ import annotations

import cv2
import numpy as np
import pymupdf

PATCH_DPI = 288


def _render_unrot(page: pymupdf.Page, clip: pymupdf.Rect, dpi: int) -> np.ndarray:
    """Renderiza um recorte no espaço sem rotação (a página é temporariamente desrotacionada)."""
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    try:
        pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False, annots=False)
    finally:
        if rot:
            page.set_rotation(rot)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3].copy()


def erase_region(page: pymupdf.Page, region: pymupdf.Rect, text_box: pymupdf.Rect | None = None) -> dict:
    """Apaga o conteúdo visual (texto) de `region` (coordenadas unrot) preservando o fundo.

    `text_box` delimita onde estão os pixels de texto (por padrão, a própria região).
    Retorna informações de diagnóstico.
    """
    region = pymupdf.Rect(region) & (page.rect * page.derotation_matrix).normalize()
    if region.is_empty or region.width < 1 or region.height < 1:
        return {"erased": False, "reason": "região vazia"}
    pad = max(2.0, min(region.height * 0.35, 8.0))
    outer = pymupdf.Rect(region.x0 - pad, region.y0 - pad, region.x1 + pad, region.y1 + pad)
    outer &= (page.rect * page.derotation_matrix).normalize()

    img = _render_unrot(page, outer, PATCH_DPI)
    h, w = img.shape[:2]
    sx = w / outer.width
    sy = h / outer.height

    def to_px(r: pymupdf.Rect) -> tuple[int, int, int, int]:
        return (max(0, int((r.x0 - outer.x0) * sx)), max(0, int((r.y0 - outer.y0) * sy)),
                min(w, int(np.ceil((r.x1 - outer.x0) * sx))), min(h, int(np.ceil((r.y1 - outer.y0) * sy))))

    rx0, ry0, rx1, ry1 = to_px(region)
    tb = to_px(pymupdf.Rect(text_box) & region) if text_box is not None else (rx0, ry0, rx1, ry1)

    # Fundo estimado pela moldura externa à região
    border = np.ones((h, w), dtype=bool)
    border[ry0:ry1, rx0:rx1] = False
    ring = img[border] if border.any() else img.reshape(-1, 3)
    bg = np.median(ring.astype(np.float32), axis=0)
    uniform = float(ring.astype(np.float32).std(axis=0).mean()) < 10.0

    diff = np.linalg.norm(img.astype(np.float32) - bg, axis=2)
    diff8 = np.clip(diff, 0, 255).astype(np.uint8)
    mask = np.zeros((h, w), dtype=np.uint8)
    sub = diff8[tb[1]:tb[3], tb[0]:tb[2]]
    if sub.size:
        thr, _ = cv2.threshold(sub, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        mask[tb[1]:tb[3], tb[0]:tb[2]] = (sub > max(25, thr * 0.6)).astype(np.uint8) * 255
    k = max(3, int(round(PATCH_DPI / 72 * 1.2)))
    mask = cv2.dilate(mask, np.ones((k, k), np.uint8), iterations=1)
    mask[:ry0, :] = 0
    mask[ry1:, :] = 0
    mask[:, :rx0] = 0
    mask[:, rx1:] = 0

    if uniform:
        out = img.copy()
        out[mask > 0] = bg.astype(np.uint8)
        method = "fill"
    else:
        bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        out = cv2.cvtColor(cv2.inpaint(bgr, mask, 5, cv2.INPAINT_TELEA), cv2.COLOR_BGR2RGB)
        method = "inpaint"

    patch = out[ry0:ry1, rx0:rx1]
    ok, png = cv2.imencode(".png", cv2.cvtColor(patch, cv2.COLOR_RGB2BGR))
    if not ok:
        return {"erased": False, "reason": "falha ao codificar remendo"}
    # Retângulo exato correspondente aos pixels recortados
    exact = pymupdf.Rect(outer.x0 + rx0 / sx, outer.y0 + ry0 / sy, outer.x0 + rx1 / sx, outer.y0 + ry1 / sy)
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    try:
        page.insert_image(exact, stream=png.tobytes(), keep_proportion=False, overlay=True)
    finally:
        if rot:
            page.set_rotation(rot)
    return {"erased": True, "method": method, "bg": tuple(float(v) / 255 for v in bg), "rect": tuple(exact)}
