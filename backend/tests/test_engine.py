"""Testes de persistência: abrir → identificar texto → editar → salvar → reabrir → conferir.

Cada teste valida, de forma independente do verificador interno:
  * a alteração está presente no PDF exportado (extração de texto do arquivo reaberto);
  * o texto antigo não está mais presente;
  * o restante da página continua idêntico (comparação de pixels fora da área editada);
  * as demais páginas continuam idênticas.
"""
from __future__ import annotations

import numpy as np
import pymupdf
import pytest

import pdf_factory as F
from app.engine.analysis import analyze_page
from app.engine.ocr import ocr_page, tesseract_available
from app.engine.verify import export_with_verification

DPI = 50


def analyze_all(data: bytes, ocr: bool = False):
    doc = pymupdf.open(stream=data, filetype="pdf")
    out = {}
    for page in doc:
        a = analyze_page(page)
        if ocr and a.needs_ocr:
            ocr_page(page, a)
        out[page.number] = a
    return out


def find(analyses, page: int, contains: str):
    for e in analyses[page].elements:
        if contains in e.text:
            return e
    raise AssertionError(f"elemento contendo {contains!r} não encontrado: "
                         f"{[e.text for e in analyses[page].elements]}")


def render(page: pymupdf.Page) -> np.ndarray:
    rot = page.rotation
    page.set_rotation(0)
    pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    page.set_rotation(rot)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).astype(np.int16)


def outside_change(orig: bytes, out: bytes, page: int, rects: list, margin: float = 3.0) -> float:
    a = render(pymupdf.open(stream=orig, filetype="pdf")[page])
    b = render(pymupdf.open(stream=out, filetype="pdf")[page])
    assert a.shape == b.shape
    mask = np.zeros(a.shape, bool)
    s = DPI / 72
    for r in rects:
        r = pymupdf.Rect(r)
        mask[max(0, int((r.y0 - margin) * s)):int((r.y1 + margin) * s) + 1,
             max(0, int((r.x0 - margin) * s)):int((r.x1 + margin) * s) + 1] = True
    diff = (np.abs(a - b) > 40) & ~mask
    return diff.sum() / max(1, (~mask).sum())


def page_text(data: bytes, page: int) -> str:
    return pymupdf.open(stream=data, filetype="pdf")[page].get_text()


def edit_and_check(data: bytes, page: int, contains: str, new_text: str, ocr: bool = False,
                   extra_ops: list | None = None, edit_region_grow: float = 0.0):
    analyses = analyze_all(data, ocr=ocr)
    el = find(analyses, page, contains)
    ops = [{"type": "edit_text", "page": page, "elementId": el.id, "text": new_text}] + (extra_ops or [])
    out, report = export_with_verification(data, analyses, ops, None)
    assert report.ok, report.errors
    assert out is not None
    # 1) reabre o arquivo exportado
    doc = pymupdf.open(stream=out, filetype="pdf")
    assert doc.page_count == pymupdf.open(stream=data, filetype="pdf").page_count
    # 2) alteração presente
    txt = " ".join(page_text(out, page).split())
    assert new_text in txt, txt
    # 3) texto antigo removido (quando não faz parte do novo)
    if el.kind == "native" and el.text not in new_text:
        assert el.text not in txt
    # 4) restante da página preservado
    r = el.rect_unrot()
    grow = max(edit_region_grow, 0)
    area = pymupdf.Rect(r.x0, r.y0, max(r.x1, r.x0 + max(el.max_extent, el.width)) + grow, r.y1)
    rects = [area] + ([pymupdf.Rect(el.region)] if el.region else [])
    assert outside_change(data, out, page, rects) < 0.002
    # 5) demais páginas idênticas
    for p in range(doc.page_count):
        if p != page:
            assert outside_change(data, out, p, []) == 0.0
    return out, report, el


# ------------------------------------------------------------------------------ casos
def test_native_text():
    out, rep, el = edit_and_check(F.native_text(), 0, "Cliente", "Cliente: Maria Aparecida")
    assert rep.ops[0]["mode"] == "native"


def test_native_preserves_font_family_and_size():
    data = F.native_text()
    out, rep, el = edit_and_check(data, 0, "Vencimento", "Vencimento: 20/12/2026")
    spans = [s for b in pymupdf.open(stream=out, filetype="pdf")[0].get_text("dict")["blocks"]
             for l in b.get("lines", []) for s in l["spans"] if "20/12/2026" in s["text"]]
    assert spans, "novo texto não encontrado"
    s = spans[0]
    assert abs(s["size"] - el.size) < 0.6
    assert "Serif" in s["font"] or "Times" in s["font"] or "Tiro" in s["font"]
    assert abs(s["origin"][0] - el.origin[0]) < 0.6 and abs(s["origin"][1] - el.origin[1]) < 0.6


def test_bold_heading_and_color_preserved():
    data = F.native_text()
    out, rep, el = edit_and_check(data, 0, "CONTA DE ENERGIA", "CONTA DE ÁGUA")
    spans = [s for b in pymupdf.open(stream=out, filetype="pdf")[0].get_text("dict")["blocks"]
             for l in b.get("lines", []) for s in l["spans"] if "ÁGUA" in s["text"]]
    assert spans and ("Bold" in spans[0]["font"] or spans[0]["flags"] & 16)


def test_gray_color_preserved():
    out, rep, el = edit_and_check(F.native_text(), 0, "Consumo do mês", "Consumo medido em kWh.")
    spans = [s for b in pymupdf.open(stream=out, filetype="pdf")[0].get_text("dict")["blocks"]
             for l in b.get("lines", []) for s in l["spans"] if "medido" in s["text"]]
    assert spans[0]["color"] == pymupdf.sRGB_to_pdf(0x666666) or spans[0]["color"] == 0x666666


def test_delete_text():
    data = F.native_text()
    analyses = analyze_all(data)
    el = find(analyses, 0, "Código")
    out, rep = export_with_verification(data, analyses, [{"type": "delete_element", "page": 0, "elementId": el.id}], None)
    assert rep.ok, rep.errors
    assert "Código" not in page_text(out, 0)
    assert "Cliente: João da Silva" in page_text(out, 0)


@pytest.mark.skipif(not tesseract_available(), reason="Tesseract não instalado")
def test_scanned_pdf_ocr():
    data = F.scanned()
    analyses = analyze_all(data, ocr=True)
    assert analyses[0].mode == "scanned"
    texts = [e.text for e in analyses[0].elements]
    assert any("Carlos" in t for t in texts), texts
    out, rep, el = edit_and_check(data, 0, "Carlos", "Nome: Ana Beatriz", ocr=True)
    assert rep.ops[0]["mode"] == "raster"
    # o nome antigo não deve mais ser visível: OCR do resultado
    doc = pymupdf.open(stream=out, filetype="pdf")
    a2 = analyze_page(doc[0])
    a2.needs_ocr = True
    ocr_page(doc[0], a2, exclude_native=True)
    assert not any("Carlos" in e.text for e in a2.elements), [e.text for e in a2.elements]


def test_scanned_with_invisible_ocr_layer():
    data = F.scanned_with_ocr_layer()
    analyses = analyze_all(data)
    assert analyses[0].mode == "ocr_layer"
    out, rep, el = edit_and_check(data, 0, "Carlos", "Nome: Paulo Souza")
    assert rep.ops[0]["mode"] == "raster"
    assert "Carlos" not in page_text(out, 0)


def test_pdf_with_images():
    data = F.with_images()
    out, rep, el = edit_and_check(data, 0, "Valor pago", "Valor pago: R$ 990,00")
    # imagem continua intacta
    assert len(pymupdf.open(stream=out, filetype="pdf")[0].get_images()) == 1


def test_multipage():
    data = F.multipage(5)
    out, rep, el = edit_and_check(data, 2, "Seção 3", "Seção 3: resultados revisados")
    assert "página 3" in page_text(out, 2)


def test_table_cell_right_aligned():
    data = F.table()
    analyses = analyze_all(data)
    el = find(analyses, 0, "R$ 170,20")
    assert el.align == "right"
    out, rep, el = edit_and_check(data, 0, "R$ 170,20", "R$ 1.070,25")
    words = [w for w in pymupdf.open(stream=out, filetype="pdf")[0].get_text("words") if w[4] == "1.070,25"]
    assert words, "valor não encontrado"
    # borda direita preservada (alinhamento à direita)
    assert abs(words[0][2] - el.rect_unrot().x1) < 1.0


def test_embedded_font_reused():
    data = F.embedded_font()
    out, rep, el = edit_and_check(data, 0, "Valor mensal", "Valor mensal 1405 reais")
    # usa o próprio objeto de fonte do PDF (mesmos glifos do subset)
    assert rep.ops[0]["font"].startswith("original"), rep.ops[0]


def test_embedded_font_missing_glyphs_fallback():
    data = F.embedded_font()
    out, rep, el = edit_and_check(data, 0, "Contrato", "Contrato aditivo XYZ 2027")
    # o subset não contém X, Y, Z, d, v → não pode reutilizar a fonte original
    assert not rep.ops[0]["font"].startswith(("embedded", "original"))
    assert "DejaVu" in rep.ops[0]["font"] or "base14" in rep.ops[0]["font"]


def test_small_text():
    out, rep, el = edit_and_check(F.small_text(), 0, "Protocolo", "Protocolo de autorização 999 em 19/10/2026")
    assert el.size < 6
    assert "Documento emitido" in page_text(out, 0)


def test_landscape_page():
    edit_and_check(F.orientations(), 0, "Total geral", "Total geral: 4.100")


def test_rotated_page():
    data = F.orientations()
    out, rep, el = edit_and_check(data, 1, "Saldo", "Saldo: 9.999")
    # o texto novo mantém a orientação do original
    lines = [l for b in pymupdf.open(stream=out, filetype="pdf")[1].get_text("dict")["blocks"]
             for l in b.get("lines", []) if any("9.999" in s["text"] for s in l["spans"])]
    assert lines and abs(lines[0]["dir"][1] - el.dir[1]) < 0.01


def test_overlapping_elements_background_preserved():
    data = F.overlapping()
    out, rep, el = edit_and_check(data, 0, "Texto sobre fundo", "Texto alterado")
    # fundo colorido continua sob o texto editado
    page = pymupdf.open(stream=out, filetype="pdf")[0]
    pix = page.get_pixmap(clip=pymupdf.Rect(200, 60, 220, 70), dpi=72)
    r, g, b = pix.pixel(5, 5)
    assert b > 200 and r < 240


def test_text_over_image():
    data = F.overlapping()
    out, rep, el = edit_and_check(data, 0, "Sobre imagem", "Novo rótulo")
    assert len(pymupdf.open(stream=out, filetype="pdf")[0].get_images()) == 1


def test_tight_line_spacing_neighbor_untouched():
    data = F.overlapping()
    out, rep, el = edit_and_check(data, 0, "Linha um", "Linha 1")
    assert "Linha dois muito próxima" in page_text(out, 0)


def test_add_text_and_image():
    data = F.native_text()
    analyses = analyze_all(data)
    ops = [{"type": "add_text", "page": 0, "x": 300, "y": 300, "text": "Anotação nova", "size": 14, "color": "#d00000"},
           {"type": "add_image", "page": 0, "rect": [300, 400, 420, 460], "imageId": "img"}]
    out, rep = export_with_verification(data, analyses, ops, lambda _id: F.png_bytes())
    assert rep.ok, rep.errors
    page = pymupdf.open(stream=out, filetype="pdf")[0]
    assert "Anotação nova" in page.get_text()
    assert len(page.get_images()) == 1


def test_add_text_on_rotated_page_is_upright():
    data = F.orientations()
    analyses = analyze_all(data)
    ops = [{"type": "add_text", "page": 1, "x": 100, "y": 300, "text": "Horizontal", "size": 12}]
    out, rep = export_with_verification(data, analyses, ops, None)
    assert rep.ok, rep.errors
    page = pymupdf.open(stream=out, filetype="pdf")[1]
    line = [l for b in page.get_text("dict")["blocks"] for l in b.get("lines", [])
            if any("Horizontal" in s["text"] for s in l["spans"])][0]
    # direção no espaço exibido deve ser horizontal
    d = pymupdf.Point(line["dir"]) * pymupdf.Matrix(page.rotation_matrix.a, page.rotation_matrix.b,
                                                     page.rotation_matrix.c, page.rotation_matrix.d, 0, 0)
    assert abs(d.x - 1) < 0.01 and abs(d.y) < 0.01


def test_delete_existing_image():
    data = F.with_images()
    analyses = analyze_all(data)
    assert analyses[0].images
    img = analyses[0].images[0]
    out, rep = export_with_verification(data, analyses, [{"type": "delete_element", "page": 0, "elementId": img["id"]}], None)
    assert rep.ok, rep.errors
    assert len(pymupdf.open(stream=out, filetype="pdf")[0].get_images()) == 0
    assert "Comprovante de pagamento" in page_text(out, 0)


def test_longer_text_is_fitted():
    data = F.table()
    analyses = analyze_all(data)
    el = find(analyses, 0, "Agosto")
    long = "Agosto de 2026 (leitura estimada pela distribuidora)"
    out, rep = export_with_verification(data, analyses, [{"type": "edit_text", "page": 0, "elementId": el.id, "text": long}], None)
    assert rep.ok, rep.errors
    words = pymupdf.open(stream=out, filetype="pdf")[0].get_text("words")
    xs = [w[2] for w in words if w[1] > 95 and w[3] < 125 and w[0] < 200]
    assert max(xs) <= 200.5, "texto invadiu a coluna vizinha"
    assert any("ajustado" in w for w in rep.warnings) or any("maior" in w for w in rep.warnings)


def test_style_override():
    data = F.native_text()
    analyses = analyze_all(data)
    el = find(analyses, 0, "Cliente")
    ops = [{"type": "edit_text", "page": 0, "elementId": el.id, "text": "Cliente: Destaque", "bold": True,
            "color": "#0055aa", "size": 13}]
    out, rep = export_with_verification(data, analyses, ops, None)
    assert rep.ok, rep.errors
    s = [s for b in pymupdf.open(stream=out, filetype="pdf")[0].get_text("dict")["blocks"]
         for l in b.get("lines", []) for s in l["spans"] if "Destaque" in s["text"]][0]
    assert s["color"] == 0x0055AA
    assert "Bold" in s["font"] or s["flags"] & 16
    assert abs(s["size"] - 13) < 0.5


def test_multiple_edits_same_page_and_last_wins():
    data = F.native_text()
    analyses = analyze_all(data)
    a = find(analyses, 0, "Cliente")
    b = find(analyses, 0, "Endereço")
    ops = [{"type": "edit_text", "page": 0, "elementId": a.id, "text": "Primeira versão"},
           {"type": "edit_text", "page": 0, "elementId": b.id, "text": "Endereço: Av. Brasil, 500"},
           {"type": "edit_text", "page": 0, "elementId": a.id, "text": "Cliente: Versão final"}]
    out, rep = export_with_verification(data, analyses, ops, None)
    assert rep.ok, rep.errors
    t = page_text(out, 0)
    assert "Cliente: Versão final" in t and "Primeira versão" not in t and "Av. Brasil, 500" in t


def test_output_opens_in_independent_parser():
    import io

    import pikepdf
    out, rep, el = edit_and_check(F.native_text(), 0, "Cliente", "Cliente: Teste qpdf")
    with pikepdf.open(io.BytesIO(out)) as pdf:
        assert len(pdf.pages) == 1
        assert not [p for p in pdf.check_pdf_syntax() if "warning" not in p.lower()]


def test_table_border_splits_adjacent_cells():
    """Células próximas separadas por borda de tabela viram elementos distintos."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.draw_line((95, 80), (95, 100), width=0.6)
    p.insert_text((56, 94), "Parcela", fontname="helv", fontsize=10)
    p.insert_text((97, 94), "Vencimento", fontname="helv", fontsize=10)
    data = doc.tobytes()
    analyses = analyze_all(data)
    texts = [e.text for e in analyses[0].elements]
    assert "Parcela" in texts and "Vencimento" in texts, texts
    out, rep, el = edit_and_check(data, 0, "Vencimento", "Data")
    assert "Parcela" in page_text(out, 0)


def test_paragraph_detected_and_reflowed():
    data = F.paragraphs()
    analyses = analyze_all(data)
    paras = [e for e in analyses[0].elements if e.is_paragraph]
    assert len(paras) == 1, [e.text[:30] for e in analyses[0].elements]
    para = paras[0]
    assert para.text.startswith("Pelo presente") and "espécie." in para.text
    assert para.justify
    new = para.text.replace("prestação de serviços", "prestação de serviços de manutenção predial e limpeza")
    out, rep = export_with_verification(data, analyses, [{"type": "edit_text", "page": 0, "elementId": para.id, "text": new}], None)
    assert rep.ok, rep.errors
    page = pymupdf.open(stream=out, filetype="pdf")[0]
    flat = " ".join(page.get_text().split())
    assert "manutenção predial e limpeza" in flat
    # nenhuma palavra ultrapassa a largura original do parágrafo
    words = [w for w in page.get_text("words") if 85 < w[1] < 230]
    assert max(w[2] for w in words) <= 72 + para.width + 1.0
    # conteúdo abaixo preservado
    assert "Cláusula segunda" in flat
    assert outside_change(data, out, 0, [pymupdf.Rect(60, 80, 410, 250)]) < 0.002


def test_paragraph_small_edit_keeps_line_breaks():
    data = F.paragraphs()
    analyses = analyze_all(data)
    para = [e for e in analyses[0].elements if e.is_paragraph][0]
    before = [ln.text for ln in para.lines]
    new = para.text.replace("instrumento", "documento")
    out, rep = export_with_verification(data, analyses, [{"type": "edit_text", "page": 0, "elementId": para.id, "text": new}], None)
    assert rep.ok, rep.errors
    a2 = analyze_page(pymupdf.open(stream=out, filetype="pdf")[0])
    para2 = [e for e in a2.elements if e.is_paragraph][0]
    after = [ln.text for ln in para2.lines]
    assert len(after) == len(before)
    assert after[1:] == before[1:]


def test_table_columns_not_grouped_as_paragraph():
    for data in (F.table(), F.native_text(), F.multipage(2)):
        analyses = analyze_all(data)
        assert not any(e.is_paragraph for a in analyses.values() for e in a.elements)


@pytest.mark.skipif(not tesseract_available(), reason="Tesseract não instalado")
def test_skewed_scan_is_deskewed_and_editable():
    data = F.scanned(skew=3.0)
    analyses = analyze_all(data, ocr=True)
    assert any("inclinada" in n for n in analyses[0].notes), analyses[0].notes
    el = find(analyses, 0, "Carlos")
    assert 1.5 < abs(el.angle % 360 if el.angle % 360 < 180 else el.angle % 360 - 360) < 4.5
    out, rep = export_with_verification(data, analyses, [{"type": "edit_text", "page": 0, "elementId": el.id, "text": "Nome: Ana Beatriz"}], None)
    assert rep.ok, rep.errors
    assert "Ana Beatriz" in pymupdf.open(stream=out, filetype="pdf")[0].get_text()


@pytest.mark.skipif(not tesseract_available(), reason="Tesseract não instalado")
def test_faint_scan_is_recognized():
    analyses = analyze_all(F.scanned(faint=True), ocr=True)
    texts = " ".join(e.text for e in analyses[0].elements)
    assert "Carlos" in texts and "Florianopolis" in texts, texts


def test_form_fields_detected_and_filled():
    data = F.form()
    analyses = analyze_all(data)
    fields = {f["name"]: f for f in analyses[0].fields}
    assert set(fields) == {"nome", "aceite", "estado", "protocolo"}
    assert fields["nome"]["value"] == "Fulano" and fields["protocolo"]["readOnly"]
    ops = [{"type": "set_field", "page": 0, "elementId": fields["nome"]["id"], "value": "Maria José"},
           {"type": "set_field", "page": 0, "elementId": fields["aceite"]["id"], "value": True},
           {"type": "set_field", "page": 0, "elementId": fields["estado"]["id"], "value": "PR"}]
    out, rep = export_with_verification(data, analyses, ops, None)
    assert rep.ok, rep.errors
    vals = {w.field_name: w.field_value for w in pymupdf.open(stream=out, filetype="pdf")[0].widgets()}
    assert vals["nome"] == "Maria José" and vals["estado"] == "PR" and vals["aceite"] not in ("Off", False)


def test_readonly_field_rejected():
    from app.engine.editor import OpError

    data = F.form()
    analyses = analyze_all(data)
    f = next(f for f in analyses[0].fields if f["name"] == "protocolo")
    with pytest.raises(OpError):
        export_with_verification(data, analyses, [{"type": "set_field", "page": 0, "elementId": f["id"], "value": "9"}], None)


def test_non_latin_scripts_edit():
    """Cirílico, grego, CJK e hebraico: fonte de fallback com cobertura e verificação aprovada."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_font(fontname="dv", fontfile=F.DEJAVU)
    p.insert_text((50, 80), "Масса нетто: 12 кг", fontname="dv", fontsize=12)
    p.insert_text((50, 110), "Ελληνικά κείμενο", fontname="dv", fontsize=12)
    p.insert_text((50, 140), "日本語のテキスト", fontname="china-s", fontsize=12)
    p.insert_text((50, 170), "German: Öffnungszeiten „heute“", fontname="helv", fontsize=12)
    data = doc.tobytes()
    analyses = analyze_all(data)
    cases = [("Масса", "Масса брутто: 15 кг"), ("Ελληνικά", "Ελληνικά νέο κείμενο"),
             ("日本語", "日本語の新しいテキスト"), ("German", "German: Öffnungszeiten „morgen“")]
    ops = [{"type": "edit_text", "page": 0, "elementId": find(analyses, 0, k).id, "text": v} for k, v in cases]
    out, rep = export_with_verification(data, analyses, ops, None)
    assert rep.ok, rep.errors
    txt = pymupdf.open(stream=out, filetype="pdf")[0].get_text()
    for _k, v in cases:
        assert v in txt, (v, txt)


def test_rtl_text_edit():
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_htmlbox(pymupdf.Rect(300, 60, 550, 90), '<div dir="rtl" style="font-size:14px">שלום עולם</div>')
    data = doc.tobytes()
    analyses = analyze_all(data)
    el = [e for e in analyses[0].elements if any("֐" <= c <= "׿" for c in e.text)][0]
    out, rep = export_with_verification(data, analyses, [{"type": "edit_text", "page": 0, "elementId": el.id, "text": "שלום חברים"}], None)
    assert rep.ok, rep.errors
    assert "חברים" in pymupdf.open(stream=out, filetype="pdf")[0].get_text()


def test_minimal_edit_keeps_unchanged_glyphs_and_reading_order():
    """Trocar dígitos num valor alinhado à direita: fonte original, sem nova tentativa,
    texto extraído na ordem certa e o resto da página intacto."""
    from pathlib import Path
    from app.engine.verify import export_with_verification
    sample = Path(__file__).resolve().parents[2] / "samples" / "conta-energia-exemplo.pdf"
    if not sample.exists():
        pytest.skip("amostra ausente")
    data = sample.read_bytes()
    d = pymupdf.open(stream=data)
    a = analyze_page(d[0])
    e = next(x for x in a.elements if x.text.startswith("224,98"))
    out, rep = export_with_verification(data, {0: a}, [{"type": "edit_text", "page": 0, "elementId": e.id,
                                                        "text": "239,45"}], None)
    assert rep.ok and not rep.warnings
    assert rep.ops[0]["font"].startswith("original")
    o = pymupdf.open(stream=out)
    assert "239,45" in o[0].get_text() and "224,98" not in o[0].get_text()
