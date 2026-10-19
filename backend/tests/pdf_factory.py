"""Gera PDFs de teste de diferentes tipos (sem depender de arquivos externos)."""
from __future__ import annotations

import io

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFont

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _png(w: int, h: int, color=(30, 120, 200)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    for i in range(0, w, 12):
        d.line([(i, 0), (w - i, h)], fill=(255, 255, 255), width=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def native_text() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 60), "CONTA DE ENERGIA ELÉTRICA", fontname="hebo", fontsize=16)
    p.insert_text((50, 100), "Cliente: João da Silva", fontname="helv", fontsize=11)
    p.insert_text((50, 116), "Endereço: Rua das Flores, 123 - Centro", fontname="helv", fontsize=11)
    p.insert_text((50, 132), "Vencimento: 10/11/2026", fontname="tiro", fontsize=11)
    p.insert_text((50, 148), "Código: 0042-7781", fontname="cour", fontsize=10)
    p.insert_text((50, 180), "Consumo do mês em kWh conforme leitura.", fontname="helv", fontsize=10,
                  color=(0.4, 0.4, 0.4))
    return doc.tobytes()


def scanned(skew: float = 0.0, faint: bool = False) -> bytes:
    """Página composta apenas por uma imagem com texto (simula digitalização)."""
    W, H = 1275, 1650  # 150 dpi carta
    img = Image.new("RGB", (W, H), (250, 249, 245))
    d = ImageDraw.Draw(img)
    f_big = ImageFont.truetype(DEJAVU_BOLD, 44)
    f = ImageFont.truetype(DEJAVU, 34)
    d.text((110, 120), "DECLARACAO DE RESIDENCIA", font=f_big, fill=(20, 20, 20))
    d.text((110, 260), "Nome: Carlos Pereira", font=f, fill=(20, 20, 20))
    d.text((110, 330), "Cidade: Florianopolis", font=f, fill=(20, 20, 20))
    d.text((110, 400), "Valor: 1500 reais", font=f, fill=(20, 20, 20))
    if skew:
        img = img.rotate(skew, resample=Image.BICUBIC, fillcolor=(250, 249, 245))
    arr = np.array(img).astype(np.int16)
    if faint:
        arr = 150 + (arr * 0.4)
    rng = np.random.default_rng(1)
    arr = np.clip(arr + rng.normal(0, 3, arr.shape), 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    p.insert_image(p.rect, stream=buf.getvalue())
    return doc.tobytes()


def scanned_with_ocr_layer() -> bytes:
    """Digitalização com camada de texto invisível (como produzida por scanners/OCRmyPDF)."""
    base = pymupdf.open(stream=scanned(), filetype="pdf")
    p = base[0]
    # posições aproximadas do texto da imagem (150 dpi → pt = px*0.48)
    for (x, y, t) in ((110, 260, "Nome: Carlos Pereira"), (110, 330, "Cidade: Florianopolis")):
        p.insert_text((x * 0.48, (y + 32) * 0.48), t, fontsize=16.3, render_mode=3)
    return base.tobytes()


def with_images() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_image(pymupdf.Rect(50, 50, 250, 150), stream=_png(400, 200))
    p.insert_text((50, 180), "Figura 1: Logotipo da empresa", fontname="helv", fontsize=10)
    p.insert_text((300, 100), "Comprovante de pagamento", fontname="hebo", fontsize=14)
    p.insert_text((300, 120), "Valor pago: R$ 250,00", fontname="helv", fontsize=11)
    return doc.tobytes()


def multipage(n: int = 5) -> bytes:
    doc = pymupdf.open()
    for i in range(n):
        p = doc.new_page()
        p.insert_text((72, 72), f"Relatório anual - página {i + 1}", fontname="hebo", fontsize=14)
        p.insert_text((72, 100), f"Seção {i + 1}: resultados do período", fontname="helv", fontsize=11)
        p.draw_line((72, 110), (540, 110), color=(0.5, 0.5, 0.5))
    return doc.tobytes()


def table() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 60), "Demonstrativo de Consumo", fontname="hebo", fontsize=14)
    cols = [50, 200, 330, 460, 560]
    rows = [80, 100, 120, 140, 160]
    for x in cols:
        p.draw_line((x, rows[0]), (x, rows[-1]), color=(0, 0, 0), width=0.8)
    for y in rows:
        p.draw_line((cols[0], y), (cols[-1], y), color=(0, 0, 0), width=0.8)
    data = [["Mês", "Leitura", "kWh", "Valor"], ["Agosto", "12345", "210", "R$ 180,50"],
            ["Setembro", "12555", "198", "R$ 170,20"], ["Outubro", "12753", "205", "R$ 176,90"]]
    for r, row in enumerate(data):
        for c, cell in enumerate(row):
            font = "hebo" if r == 0 else "helv"
            if c == 3:  # alinhado à direita
                w = pymupdf.get_text_length(cell, fontname=font, fontsize=10)
                p.insert_text((cols[c + 1] - 6 - w, rows[r] + 14), cell, fontname=font, fontsize=10)
            else:
                p.insert_text((cols[c] + 6, rows[r] + 14), cell, fontname=font, fontsize=10)
    return doc.tobytes()


def embedded_font() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_font(fontname="dvs", fontfile=DEJAVU)
    p.insert_text((50, 80), "Contrato número 2026 0147", fontname="dvs", fontsize=12)
    p.insert_text((50, 100), "Valor mensal 1450 reais", fontname="dvs", fontsize=12)
    doc.subset_fonts()
    return doc.tobytes()


def small_text() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 60), "Nota fiscal", fontname="hebo", fontsize=12)
    p.insert_text((50, 72), "Protocolo de autorização 135260001234567 em 18/10/2026", fontname="helv", fontsize=5)
    p.insert_text((50, 78), "Documento emitido por ME ou EPP optante pelo Simples Nacional", fontname="helv", fontsize=5)
    return doc.tobytes()


def orientations() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page(width=842, height=595)  # paisagem nativa
    p.insert_text((60, 80), "Página em paisagem", fontname="hebo", fontsize=14)
    p.insert_text((60, 110), "Total geral: 3.200", fontname="helv", fontsize=11)
    q = doc.new_page(width=595, height=842)   # retrato com /Rotate 90 → exibido em paisagem
    q.set_rotation(90)
    # texto escrito de modo a aparecer na horizontal para o leitor
    disp = pymupdf.Point(60, 80) * q.derotation_matrix
    q.insert_text(disp, "Página rotacionada", fontname="hebo", fontsize=14, rotate=90)
    disp2 = pymupdf.Point(60, 110) * q.derotation_matrix
    q.insert_text(disp2, "Saldo: 7.450", fontname="helv", fontsize=11, rotate=90)
    return doc.tobytes()


def overlapping() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page()
    p.draw_rect(pymupdf.Rect(40, 40, 560, 140), color=None, fill=(0.85, 0.92, 1.0))
    p.insert_image(pymupdf.Rect(400, 50, 550, 130), stream=_png(300, 160, (240, 180, 60)))
    p.insert_text((50, 80), "Texto sobre fundo colorido", fontname="hebo", fontsize=14, color=(0, 0.2, 0.6))
    p.insert_text((410, 95), "Sobre imagem", fontname="hebo", fontsize=12, color=(1, 1, 1))
    p.insert_text((50, 110), "Linha um", fontname="helv", fontsize=11)
    p.insert_text((50, 119), "Linha dois muito próxima", fontname="helv", fontsize=11)  # entrelinha apertada
    return doc.tobytes()


def signed_like() -> bytes:
    """PDF com campo de assinatura (sem certificado real) — deve gerar aviso."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 80), "Termo de acordo", fontname="helv", fontsize=12)
    w = pymupdf.Widget()
    w.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    w.field_name = "Assinatura1"
    w.rect = pymupdf.Rect(50, 100, 250, 150)
    p.add_widget(w)
    return doc.tobytes()


def encrypted(user_pw: str = "1234") -> bytes:
    doc = pymupdf.open(stream=native_text(), filetype="pdf")
    buf = io.BytesIO()
    doc.save(buf, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=user_pw, owner_pw=user_pw + "owner",
             permissions=-1)
    return buf.getvalue()


def png_bytes() -> bytes:
    return _png(120, 60, (200, 30, 30))


LOREM = ("Pelo presente instrumento particular, as partes acima qualificadas resolvem celebrar o "
         "presente contrato de prestação de serviços, que se regerá pelas cláusulas e condições "
         "seguintes, obrigando-se a cumpri-las integralmente por si e por seus sucessores a qualquer "
         "título, na forma da legislação vigente aplicável à espécie.")


def paragraphs(justify: bool = True) -> bytes:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 70), "CONTRATO DE PRESTAÇÃO DE SERVIÇOS", fontname="hebo", fontsize=13)
    rect = pymupdf.Rect(72, 90, 400, 200)
    p.insert_textbox(rect, LOREM, fontname="tiro", fontsize=11, align=3 if justify else 0)
    p.insert_text((72, 260), "Cláusula segunda: o prazo é de doze meses.", fontname="tiro", fontsize=11)
    p.draw_line((72, 280), (520, 280))
    return doc.tobytes()


def form() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 60), "Ficha de cadastro", fontname="hebo", fontsize=14)
    specs = [
        (pymupdf.PDF_WIDGET_TYPE_TEXT, "nome", pymupdf.Rect(50, 80, 300, 100), "Fulano"),
        (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, "aceite", pymupdf.Rect(50, 110, 64, 124), False),
        (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "estado", pymupdf.Rect(50, 135, 200, 155), "SC"),
        (pymupdf.PDF_WIDGET_TYPE_TEXT, "protocolo", pymupdf.Rect(50, 165, 200, 185), "123"),
    ]
    for kind, name, rect, value in specs:
        w = pymupdf.Widget()
        w.field_type = kind
        w.field_name = name
        w.rect = rect
        if kind == pymupdf.PDF_WIDGET_TYPE_COMBOBOX:
            w.choice_values = ["SC", "PR", "RS"]
        w.field_value = value
        if name == "protocolo":
            w.field_flags = 1  # somente leitura
        w.text_fontsize = 11
        p.add_widget(w)
    return doc.tobytes()
