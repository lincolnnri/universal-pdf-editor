"""Gera PDFs de exemplo em samples/ para experimentar o editor.

Uso:  python scripts/make_samples.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "tests"))
import pdf_factory as F  # noqa: E402

OUT = ROOT / "samples"
SANS = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
SANS_B = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"


def conta_energia() -> bytes:
    """Fatura no estilo de sistemas de faturamento: fontes subset Identity-H, tabela, caixas coloridas."""
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    have = Path(SANS).exists() and Path(SANS_B).exists()
    if have:
        p.insert_font(fontname="LS", fontfile=SANS)
        p.insert_font(fontname="LSB", fontfile=SANS_B)
    reg, bold = ("LS", "LSB") if have else ("helv", "hebo")
    blue = (0.06, 0.32, 0.62)
    p.draw_rect(pymupdf.Rect(0, 0, 595, 70), color=None, fill=blue)
    p.insert_text((36, 44), "ENERGIA SUL DISTRIBUIDORA S.A.", fontname=bold, fontsize=16, color=(1, 1, 1))
    p.insert_text((400, 44), "CNPJ 00.000.000/0001-00", fontname=reg, fontsize=9, color=(1, 1, 1))
    p.insert_text((36, 100), "CONTA DE ENERGIA ELÉTRICA", fontname=bold, fontsize=13, color=blue)
    p.insert_text((36, 125), "Cliente: MARIA DE SOUZA OLIVEIRA", fontname=reg, fontsize=10)
    p.insert_text((36, 140), "Endereço: Rua das Palmeiras, 1234 - Centro - Palhoça/SC", fontname=reg, fontsize=10)
    p.insert_text((36, 155), "Unidade consumidora: 4.5678.901-2   Classe: Residencial", fontname=reg, fontsize=10)
    box = pymupdf.Rect(380, 90, 559, 165)
    p.draw_rect(box, color=blue, fill=(0.93, 0.96, 1.0), width=1)
    p.insert_text((392, 110), "Vencimento", fontname=reg, fontsize=9, color=(0.3, 0.3, 0.3))
    p.insert_text((392, 128), "15/11/2026", fontname=bold, fontsize=14)
    p.insert_text((392, 146), "Total a pagar", fontname=reg, fontsize=9, color=(0.3, 0.3, 0.3))
    p.insert_text((480, 146), "R$ 287,43", fontname=bold, fontsize=12, color=blue)
    # tabela
    cols = [36, 250, 330, 420, 559]
    top = 195
    rows = [top + i * 20 for i in range(6)]
    p.draw_rect(pymupdf.Rect(36, rows[0], 559, rows[1]), color=None, fill=(0.9, 0.92, 0.95))
    for x in cols:
        p.draw_line((x, rows[0]), (x, rows[-1]), width=0.6)
    for y in rows:
        p.draw_line((cols[0], y), (cols[-1], y), width=0.6)
    data = [["Descrição", "Quant.", "Tarifa", "Valor (R$)"],
            ["Consumo ativo (kWh)", "312", "0,72110", "224,98"],
            ["Bandeira tarifária amarela", "312", "0,01885", "5,88"],
            ["Contrib. iluminação pública", "1", "18,50", "18,50"],
            ["ICMS", "", "", "38,07"]]
    for r, row in enumerate(data):
        for c, cell in enumerate(row):
            font = bold if r == 0 else reg
            if c == 0:
                p.insert_text((cols[0] + 6, rows[r] + 14), cell, fontname=font, fontsize=9)
            else:
                w = pymupdf.get_text_length(cell, fontname="hebo" if r == 0 else "helv", fontsize=9)
                p.insert_text((cols[c + 1] - 6 - w, rows[r] + 14), cell, fontname=font, fontsize=9)
    p.insert_text((36, 330), "Histórico de consumo: Out 298 · Set 305 · Ago 321 · Jul 333 kWh", fontname=reg,
                  fontsize=8, color=(0.35, 0.35, 0.35))
    p.insert_text((36, 800), "Documento de exemplo gerado para testes do Universal PDF Editor.", fontname=reg,
                  fontsize=7, color=(0.5, 0.5, 0.5))
    doc.subset_fonts()
    buf = io.BytesIO()
    doc.save(buf, garbage=3, deflate=True)
    return buf.getvalue()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    files = {
        "conta-energia-exemplo.pdf": conta_energia(),
        "declaracao-digitalizada.pdf": F.scanned(),
        "relatorio-varias-paginas.pdf": F.multipage(4),
        "tabela-consumo.pdf": F.table(),
        "paisagem-e-rotacao.pdf": F.orientations(),
    }
    for name, data in files.items():
        (OUT / name).write_bytes(data)
        print("gerado", OUT / name)


if __name__ == "__main__":
    main()
