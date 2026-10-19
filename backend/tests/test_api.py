"""Testes ponta a ponta da API: upload → análise → prévia → exportação → download → reabertura."""
from __future__ import annotations

import pymupdf
import pytest
from fastapi.testclient import TestClient

import pdf_factory as F
from app.config import settings
from app.main import app
from app.storage import store


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def upload(client, data: bytes, name="doc.pdf", password=None):
    files = {"file": (name, data, "application/pdf")}
    form = {"password": password} if password else {}
    return client.post("/api/documents", files=files, data=form)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"]


def test_full_flow_native(client):
    r = upload(client, F.native_text(), "conta.pdf")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["pageCount"] == 1
    a = client.get(f"/api/documents/{doc['id']}/pages/0/analysis").json()
    el = next(e for e in a["elements"] if e["text"].startswith("Cliente"))
    ops = [{"type": "edit_text", "page": 0, "elementId": el["id"], "text": "Cliente: Fulana de Tal"}]

    # prévia real (PNG gerado a partir do PDF modificado)
    p = client.post(f"/api/documents/{doc['id']}/pages/0/preview?scale=1", json={"ops": ops})
    assert p.status_code == 200 and p.headers["content-type"] == "image/png"
    orig = client.get(f"/api/documents/{doc['id']}/pages/0/image?scale=1")
    assert p.content != orig.content

    e = client.post(f"/api/documents/{doc['id']}/export", json={"ops": ops})
    assert e.status_code == 200, e.text
    rep = e.json()
    assert rep["ok"] and rep["downloadUrl"] and all(c["ok"] for c in rep["checks"])
    d = client.get(rep["downloadUrl"])
    assert d.status_code == 200 and d.headers["content-type"] == "application/pdf"
    assert "conta-editado.pdf" in d.headers["content-disposition"]
    out = pymupdf.open(stream=d.content, filetype="pdf")
    assert "Cliente: Fulana de Tal" in out[0].get_text()
    assert "João da Silva" not in out[0].get_text()
    # o arquivo baixado NÃO é o original
    assert d.content != F.native_text()

    # fechar documento remove arquivos temporários
    sdir = store.get(doc["id"]).dir
    assert client.delete(f"/api/documents/{doc['id']}").json()["deleted"]
    assert not sdir.exists()
    assert client.get(f"/api/documents/{doc['id']}/pages/0/analysis").status_code == 404


def test_add_image_via_api(client):
    doc = upload(client, F.native_text()).json()
    img = client.post(f"/api/documents/{doc['id']}/images",
                      files={"file": ("x.png", F.png_bytes(), "image/png")})
    assert img.status_code == 200, img.text
    ops = [{"type": "add_image", "page": 0, "rect": [300, 300, 420, 360], "imageId": img.json()["imageId"]},
           {"type": "add_text", "page": 0, "x": 300, "y": 380, "text": "Carimbo", "size": 12, "color": "#004080"}]
    rep = client.post(f"/api/documents/{doc['id']}/export", json={"ops": ops}).json()
    assert rep["ok"], rep
    out = pymupdf.open(stream=client.get(rep["downloadUrl"]).content, filetype="pdf")
    assert len(out[0].get_images()) == 1 and "Carimbo" in out[0].get_text()


@pytest.mark.skipif(not __import__("shutil").which("tesseract"), reason="Tesseract não instalado")
def test_scanned_flow_via_api(client):
    doc = upload(client, F.scanned()).json()
    a = client.get(f"/api/documents/{doc['id']}/pages/0/analysis").json()
    assert a["mode"] == "scanned" and a["ocrDone"]
    el = next(e for e in a["elements"] if "Valor" in e["text"])
    ops = [{"type": "edit_text", "page": 0, "elementId": el["id"], "text": "Valor: 2300 reais"}]
    rep = client.post(f"/api/documents/{doc['id']}/export", json={"ops": ops}).json()
    assert rep["ok"], rep
    assert rep["ops"][0]["mode"] == "raster"
    out = pymupdf.open(stream=client.get(rep["downloadUrl"]).content, filetype="pdf")
    assert "Valor: 2300 reais" in out[0].get_text()


def test_rejects_non_pdf(client):
    r = upload(client, b"isto nao e um pdf", "x.pdf")
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_a_pdf"


def test_rejects_corrupt_pdf(client):
    r = upload(client, b"%PDF-1.7\n garbage garbage", "x.pdf")
    assert r.status_code == 400


def test_rejects_too_large(client, monkeypatch):
    monkeypatch.setattr(type(settings), "max_upload_bytes", property(lambda self: 1000))
    r = upload(client, F.native_text())
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"


def test_password_protected(client):
    data = F.encrypted("1234")
    r = upload(client, data)
    assert r.status_code == 401 and r.json()["error"]["code"] == "password_required"
    r = upload(client, data, password="errada")
    assert r.status_code == 401 and r.json()["error"]["code"] == "wrong_password"
    r = upload(client, data, password="1234")
    assert r.status_code == 200, r.text
    doc = r.json()
    a = client.get(f"/api/documents/{doc['id']}/pages/0/analysis").json()
    el = next(e for e in a["elements"] if e["text"].startswith("Cliente"))
    rep = client.post(f"/api/documents/{doc['id']}/export",
                      json={"ops": [{"type": "edit_text", "page": 0, "elementId": el["id"], "text": "Cliente: X"}]}).json()
    assert rep["ok"], rep
    out = pymupdf.open(stream=client.get(rep["downloadUrl"]).content, filetype="pdf")
    assert out.needs_pass  # a proteção original é mantida
    assert out.authenticate("1234")
    assert "Cliente: X" in out[0].get_text()


def test_signature_warning_and_ack(client):
    r = upload(client, F.signed_like())
    doc = r.json()
    assert doc["signed"] and doc["warnings"]
    a = client.get(f"/api/documents/{doc['id']}/pages/0/analysis").json()
    el = a["elements"][0]
    ops = [{"type": "edit_text", "page": 0, "elementId": el["id"], "text": "Termo alterado"}]
    r = client.post(f"/api/documents/{doc['id']}/export", json={"ops": ops})
    assert r.status_code == 409 and r.json()["error"]["code"] == "signature_ack_required"
    r = client.post(f"/api/documents/{doc['id']}/export", json={"ops": ops, "acknowledgeSignature": True})
    assert r.status_code == 200 and r.json()["ok"]
    assert any("assinatura" in w.lower() for w in r.json()["warnings"])


def test_invalid_operation_errors(client):
    doc = upload(client, F.native_text()).json()
    r = client.post(f"/api/documents/{doc['id']}/export",
                    json={"ops": [{"type": "edit_text", "page": 0, "elementId": "p0-n999", "text": "x"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_operation"
    r = client.post(f"/api/documents/{doc['id']}/export", json={"ops": []})
    assert r.status_code == 400 and r.json()["error"]["code"] == "no_changes"
    r = client.post(f"/api/documents/{doc['id']}/export",
                    json={"ops": [{"type": "edit_text", "page": 7, "elementId": "p7-n0", "text": "x"}]})
    assert r.status_code == 400


def test_download_requires_valid_token(client):
    doc = upload(client, F.native_text()).json()
    r = client.get(f"/api/documents/{doc['id']}/download/" + "0" * 32)
    assert r.status_code == 404


def test_verification_failure_blocks_download(client, monkeypatch):
    """Se a verificação falhar, nenhum arquivo é disponibilizado (nunca o original)."""
    from app.engine import verify

    doc = upload(client, F.native_text()).json()
    a = client.get(f"/api/documents/{doc['id']}/pages/0/analysis").json()
    el = a["elements"][1]
    real_build = verify.build_document

    def broken_build(original, analyses, ops, loader, password=None, forbid_embedded=None):
        _data, results = real_build(original, analyses, ops, loader, password, forbid_embedded)
        return original, results  # simula um bug que devolveria o original

    monkeypatch.setattr(verify, "build_document", broken_build)
    r = client.post(f"/api/documents/{doc['id']}/export",
                    json={"ops": [{"type": "edit_text", "page": 0, "elementId": el["id"], "text": "Outro texto"}]})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "verification_failed"
    assert not body["report"]["ok"]
    assert "downloadUrl" not in body["report"]


def test_session_expiry_cleans_files(client, monkeypatch):
    doc = upload(client, F.native_text()).json()
    s = store.get(doc["id"])
    s.last_access -= 10_000_000
    assert store.cleanup_expired() >= 1
    assert not s.dir.exists()


def test_export_selected_pages_with_edit(client):
    """Baixar só algumas páginas: o arquivo final tem exatamente as páginas escolhidas, com a edição."""
    r = upload(client, F.multipage(5), "rel.pdf")
    doc = r.json()
    a = client.get(f"/api/documents/{doc['id']}/pages/2/analysis").json()
    el = next(e for e in a["elements"] if e["kind"] == "native")
    ops = [{"type": "edit_text", "page": 2, "elementId": el["id"], "text": "Página EDITADA"}]
    e = client.post(f"/api/documents/{doc['id']}/export", json={"ops": ops, "pages": [4, 2, 0]})
    assert e.status_code == 200, e.text
    rep = e.json()
    assert rep["ok"] and all(c["ok"] for c in rep["checks"]) and rep["pages"] == 3
    out = pymupdf.open(stream=client.get(rep["downloadUrl"]).content, filetype="pdf")
    full = pymupdf.open(stream=F.multipage(5), filetype="pdf")
    assert out.page_count == 3
    assert "Página EDITADA" in out[1].get_text()
    assert out[0].get_text() == full[0].get_text() and out[2].get_text() == full[4].get_text()


def test_export_selected_pages_without_edits(client):
    r = upload(client, F.multipage(4), "rel.pdf")
    doc = r.json()
    e = client.post(f"/api/documents/{doc['id']}/export", json={"ops": [], "pages": [1]})
    assert e.status_code == 200, e.text
    out = pymupdf.open(stream=client.get(e.json()["downloadUrl"]).content, filetype="pdf")
    assert out.page_count == 1
    assert out[0].get_text() == pymupdf.open(stream=F.multipage(4), filetype="pdf")[1].get_text()


def test_export_invalid_pages(client):
    doc = upload(client, F.multipage(3), "rel.pdf").json()
    assert client.post(f"/api/documents/{doc['id']}/export", json={"ops": [], "pages": [7]}).status_code == 400
    assert client.post(f"/api/documents/{doc['id']}/export", json={"ops": [], "pages": []}).status_code == 400
    assert client.post(f"/api/documents/{doc['id']}/export", json={"ops": []}).status_code == 400
