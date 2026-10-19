"""Teste de interface ponta a ponta (navegador real, via Playwright).

Fluxo: CARREGAR → CLICAR → EDITAR → SALVAR → BAIXAR → reabrir o PDF baixado e conferir.

Requisitos: frontend compilado (frontend/dist) e `pip install playwright && playwright install chromium`.
Executar:  python -m pytest e2e -q
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
pymupdf = pytest.importorskip("pymupdf")

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
SHOTS = Path(os.environ.get("UPE_E2E_SHOTS", ROOT / "e2e" / "screenshots"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        pytest.skip("frontend não compilado (execute: cd frontend && npm run build)")
    if not (SAMPLES / "conta-energia-exemplo.pdf").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_samples.py")], check=True)
    port = _free_port()
    env = dict(os.environ, UPE_WORK_DIR=str(tmp_path_factory.mktemp("work")))
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
                            cwd=ROOT / "backend", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture()
def page(server):
    SHOTS.mkdir(parents=True, exist_ok=True)
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 950}, accept_downloads=True)
        pg = ctx.new_page()
        pg.goto(server)
        yield pg
        browser.close()


def wait_idle(page):
    page.wait_for_timeout(250)
    page.wait_for_function(
        "() => !document.body.innerText.includes('Aplicando alteração') && !document.body.innerText.includes('Reconhecendo texto')",
        timeout=60_000)


def edit(page, label_prefix: str, new_text: str, shot: str | None = None):
    box = page.locator(f'[role=button][aria-label^="{label_prefix}"]').first
    box.click()
    ed = page.get_by_test_id("inline-editor")
    ed.wait_for()
    page.keyboard.press("Control+A")
    page.keyboard.type(new_text)
    if shot:
        page.screenshot(path=str(SHOTS / shot))
    page.keyboard.press("Enter")
    wait_idle(page)


def test_native_bill_full_flow(page, tmp_path):
    page.screenshot(path=str(SHOTS / "01-inicio.png"))
    page.set_input_files("input[type=file][accept*=pdf]", str(SAMPLES / "conta-energia-exemplo.pdf"))
    page.locator('[role=button][aria-label^="Cliente:"]').first.wait_for(timeout=30_000)

    edit(page, "Cliente:", "Cliente: JOANA PEREIRA LIMA")
    edit(page, "R$ 287,43", "R$ 301,90", shot="02a-editando.png")
    edit(page, "224,98", "239,45")
    page.screenshot(path=str(SHOTS / "02-editado.png"))
    assert "Não salvo · 3 alteração(ões)" in page.get_by_test_id("save-status").text_content()

    # desfazer / refazer
    page.get_by_test_id("undo").click()
    assert "2 alteração(ões)" in page.get_by_test_id("save-status").text_content()
    page.get_by_test_id("redo").click()
    assert "3 alteração(ões)" in page.get_by_test_id("save-status").text_content()

    # adicionar texto
    page.get_by_test_id("tool-text").click()
    pg0 = page.get_by_test_id("page-0")
    bb = pg0.bounding_box()
    pg0.click(position={"x": bb["width"] * 0.08, "y": bb["height"] * 0.45})
    page.get_by_test_id("inline-editor").wait_for()
    page.keyboard.press("Control+A")
    page.keyboard.type("Pago em 12/11/2026")
    page.keyboard.press("Enter")
    wait_idle(page)

    with page.expect_download(timeout=60_000) as dl:
        page.get_by_test_id("save").click()
    path = tmp_path / "baixado.pdf"
    dl.value.save_as(path)
    page.get_by_text("PDF salvo e verificado").wait_for()
    page.screenshot(path=str(SHOTS / "03-salvo.png"))
    assert dl.value.suggested_filename == "conta-energia-exemplo-editado.pdf"

    doc = pymupdf.open(path)
    text = " ".join(doc[0].get_text().split())
    assert "Cliente: JOANA PEREIRA LIMA" in text
    assert "R$ 301,90" in text and "R$ 287,43" not in text
    assert "239,45" in text and "224,98" not in text
    assert "Pago em 12/11/2026" in text
    assert "MARIA DE SOUZA" not in text
    assert "Unidade consumidora" in text  # conteúdo não editado preservado
    assert page.get_by_test_id("save-status").text_content() == "Salvo e verificado"
    doc[0].get_pixmap(dpi=90).save(str(SHOTS / "04-pdf-baixado.png"))


def test_scanned_document_ocr_edit(page, tmp_path):
    page.set_input_files("input[type=file][accept*=pdf]", str(SAMPLES / "declaracao-digitalizada.pdf"))
    page.locator('[role=button][aria-label*="Carlos"]').first.wait_for(timeout=90_000)
    edit(page, "Nome:", "Nome: Beatriz Ramos")
    page.screenshot(path=str(SHOTS / "05-digitalizado-editado.png"))
    with page.expect_download(timeout=60_000) as dl:
        page.get_by_test_id("save").click()
    path = tmp_path / "scan.pdf"
    dl.value.save_as(path)
    text = pymupdf.open(path)[0].get_text()
    assert "Nome: Beatriz Ramos" in text
    assert "reconstrução visual" in page.locator("[role=dialog]").inner_text()


def test_invalid_file_shows_error(page, tmp_path):
    bad = tmp_path / "falso.pdf"
    bad.write_bytes(b"nao sou pdf")
    page.set_input_files("input[type=file][accept*=pdf]", str(bad))
    page.get_by_role("alert").wait_for()
    assert "não é um PDF válido" in page.get_by_role("alert").inner_text()


def test_tiny_text_over_large_image_is_clickable(page, tmp_path):
    """Regressão: texto pequeno sobre imagem grande (coluna lateral de faturas) deve abrir o editor."""
    import io

    from PIL import Image

    img = Image.new("RGB", (300, 800), (230, 236, 245))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    doc = pymupdf.open()
    p = doc.new_page(width=842, height=1191)
    p.insert_image(pymupdf.Rect(0, 0, 290, 848), stream=buf.getvalue())
    p.insert_image(pymupdf.Rect(0, 0, 290, 848), stream=buf.getvalue())
    p.insert_text((19, 87), "GARDENIA DE SOUSA", fontname="hebo", fontsize=4.29)
    p.insert_text((400, 300), "Texto de referencia", fontname="helv", fontsize=10)
    path = tmp_path / "pequeno.pdf"
    doc.save(path)

    page.set_input_files("input[type=file][accept*=pdf]", str(path))
    box = page.locator('[role=button][aria-label="GARDENIA DE SOUSA"]')
    box.wait_for(timeout=30_000)
    box.click()
    page.get_by_test_id("inline-editor").wait_for(timeout=5_000)
    page.keyboard.press("Control+A")
    page.keyboard.type("MARIA DE SOUSA")
    page.keyboard.press("Enter")
    wait_idle(page)
    with page.expect_download(timeout=60_000) as dl:
        page.get_by_test_id("save").click()
    out = tmp_path / "out.pdf"
    dl.value.save_as(out)
    text = pymupdf.open(out)[0].get_text()
    assert "MARIA DE SOUSA" in text and "GARDENIA" not in text


def _factory():
    sys.path.insert(0, str(ROOT / "backend" / "tests"))
    import pdf_factory

    return pdf_factory


def test_paragraph_editing_in_browser(page, tmp_path):
    F = _factory()
    path = tmp_path / "contrato.pdf"
    path.write_bytes(F.paragraphs())
    page.set_input_files("input[type=file][accept*=pdf]", str(path))
    box = page.locator('[role=button][aria-label^="Pelo presente"]')
    box.wait_for(timeout=30_000)
    box.click()
    ed = page.get_by_test_id("inline-editor")
    ed.wait_for()
    assert "espécie." in ed.inner_text()  # o parágrafo inteiro abre para edição
    page.keyboard.press("Control+A")
    page.keyboard.type("Pelo presente instrumento as partes ajustam novas condições de pagamento, com "
                       "vencimento todo dia dez e reajuste anual pelo índice oficial, mantidas as demais "
                       "cláusulas do contrato original.")
    page.screenshot(path=str(SHOTS / "06-paragrafo-editando.png"))
    page.keyboard.press("Enter")
    wait_idle(page)
    page.screenshot(path=str(SHOTS / "07-paragrafo-editado.png"))
    with page.expect_download(timeout=60_000) as dl:
        page.get_by_test_id("save").click()
    out = tmp_path / "out.pdf"
    dl.value.save_as(out)
    text = " ".join(pymupdf.open(out)[0].get_text().split())
    assert "reajuste anual pelo índice oficial" in text and "Cláusula segunda" in text


def test_form_fields_in_browser(page, tmp_path):
    F = _factory()
    path = tmp_path / "ficha.pdf"
    path.write_bytes(F.form())
    page.set_input_files("input[type=file][accept*=pdf]", str(path))
    nome = page.get_by_label("nome", exact=True)
    nome.wait_for(timeout=30_000)
    nome.click()
    nome.fill("Maria José")
    nome.press("Enter")
    page.get_by_label("aceite", exact=True).check(force=True)
    page.get_by_label("estado", exact=True).select_option("PR")
    wait_idle(page)
    page.screenshot(path=str(SHOTS / "08-formulario.png"))
    with page.expect_download(timeout=60_000) as dl:
        page.get_by_test_id("save").click()
    out = tmp_path / "out.pdf"
    dl.value.save_as(out)
    vals = {w.field_name: w.field_value for w in pymupdf.open(out)[0].widgets()}
    assert vals["nome"] == "Maria José" and vals["estado"] == "PR" and vals["aceite"] not in ("Off", False)


def test_download_selected_pages(page, tmp_path):
    """Multipágina: editar a pág. 2, escolher só as páginas 2-3 na hora de baixar."""
    page.set_input_files("input[type=file][accept*=pdf]", str(SAMPLES / "relatorio-varias-paginas.pdf"))
    page.get_by_test_id("page-0").wait_for(timeout=30_000)
    page.get_by_test_id("page-1").scroll_into_view_if_needed()
    page.locator('[role=button][aria-label^="Relatório anual - página 2"]').first.wait_for(timeout=30_000)
    edit(page, "Relatório anual - página 2", "Relatório anual - página DOIS")
    page.get_by_test_id("save").click()
    page.get_by_test_id("page-picker").wait_for()
    page.get_by_test_id("pages-some").check()
    page.get_by_test_id("pages-input").fill("2-3")
    assert "2 de" in page.get_by_test_id("page-picker").text_content()
    page.screenshot(path=str(SHOTS / "05-escolher-paginas.png"))
    with page.expect_download(timeout=60_000) as dl:
        page.get_by_test_id("pages-confirm").click()
    path = tmp_path / "paginas.pdf"
    dl.value.save_as(path)
    page.get_by_text("PDF salvo e verificado").wait_for()
    doc = pymupdf.open(path)
    assert doc.page_count == 2
    assert "Relatório anual - página DOIS" in doc[0].get_text()
    assert "página 3" in doc[1].get_text()

    # clicando nas miniaturas também funciona (sem alterações extras: extrair só a página 1)
    page.get_by_role("button", name="Fechar", exact=True).click()
    page.get_by_test_id("save").click()
    page.get_by_test_id("pages-some").check()
    page.get_by_role("button", name="Nenhuma", exact=True).click()
    page.get_by_test_id("pick-1").click()
    with page.expect_download(timeout=60_000) as dl2:
        page.get_by_test_id("pages-confirm").click()
    p2 = tmp_path / "p1.pdf"
    dl2.value.save_as(p2)
    d2 = pymupdf.open(p2)
    assert d2.page_count == 1 and "página 1" in d2[0].get_text()
