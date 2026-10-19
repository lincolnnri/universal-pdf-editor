"""API HTTP do Universal PDF Editor (FastAPI)."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import pymupdf
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import PROJECT_DIR, settings
from . import workers
from .engine.editor import OpError
from .engine.ocr import OCRUnavailable, tesseract_available
from .storage import SessionError, store

log = logging.getLogger("upe")
pymupdf.TOOLS.mupdf_display_errors(False)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.start()
    yield
    store.stop()
    workers.shutdown()


app = FastAPI(title="Universal PDF Editor", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["*"], allow_headers=["*"],
)


def err(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, **extra}})


@app.exception_handler(SessionError)
async def _session_error(_req: Request, exc: SessionError):
    return err(exc.status, exc.code, exc.message)


@app.exception_handler(OpError)
async def _op_error(_req: Request, exc: OpError):
    return err(400, "invalid_operation", str(exc))


@app.exception_handler(workers.WorkerError)
async def _worker_error(_req: Request, exc: workers.WorkerError):
    return err(503 if exc.code == "timeout" else 422, exc.code, exc.message)


@app.exception_handler(OCRUnavailable)
async def _ocr_error(_req: Request, exc: OCRUnavailable):
    return err(503, "ocr_unavailable", str(exc))


# ---------------------------------------------------------------------------- modelos
class Operation(BaseModel):
    type: Literal["edit_text", "delete_element", "add_text", "add_image", "set_field"]
    page: int
    elementId: str | None = None
    text: str | None = Field(default=None, max_length=5000)
    size: float | None = None
    color: str | None = Field(default=None, pattern=r"^#?[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")
    bold: bool | None = None
    italic: bool | None = None
    align: Literal["left", "right", "center"] | None = None
    fontFamily: Literal["sans", "serif", "mono"] | None = None
    x: float | None = None
    y: float | None = None
    rect: list[float] | None = None
    imageId: str | None = None
    value: str | bool | None = Field(default=None)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class OpsRequest(BaseModel):
    ops: list[Operation] = Field(default_factory=list, max_length=5000)


class ExportRequest(OpsRequest):
    acknowledgeSignature: bool = False
    pages: list[int] | None = Field(default=None, max_length=5000)   # 0-based; None = todas


# ---------------------------------------------------------------------------- rotas
@app.get("/api/health")
def health():
    return {"ok": True, "ocr": tesseract_available(), "ocrLanguages": settings.ocr_languages,
            "maxUploadMb": settings.max_upload_mb, "maxPages": settings.max_pages}


@app.post("/api/documents")
async def upload(file: UploadFile = File(...), password: str | None = Form(default=None)):
    data = await file.read(settings.max_upload_bytes + 1)
    s = store.create(data, file.filename or "documento.pdf", password or None)
    pages = s.pages
    warnings = []
    if s.signed:
        warnings.append("Este documento possui assinatura digital. Qualquer edição invalidará a assinatura; "
                        "o arquivo exportado não deve ser apresentado como a versão original assinada.")
    return {"id": s.id, "filename": s.filename, "pageCount": s.page_count, "pages": pages,
            "signed": s.signed, "encrypted": s.encrypted, "warnings": warnings}


@app.get("/api/documents/{doc_id}/pages/{pno}/analysis")
def page_analysis(doc_id: str, pno: int, ocr: Literal["auto", "force", "off"] = "auto"):
    s = store.get(doc_id)
    _a, js = s.analysis(pno, ocr=ocr)
    return js


def _png(data: bytes, cache: bool = True) -> Response:
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "private, max-age=3600" if cache else "no-store"})


@app.get("/api/documents/{doc_id}/pages/{pno}/image")
def page_image(doc_id: str, pno: int, scale: float = Query(1.5, gt=0.05, le=4.0)):
    s = store.get(doc_id)
    return _png(s.render(pno, round(scale, 3)))


@app.post("/api/documents/{doc_id}/pages/{pno}/preview")
def page_preview(doc_id: str, pno: int, req: OpsRequest, scale: float = Query(1.5, gt=0.05, le=4.0)):
    """Renderiza a página com as alterações realmente aplicadas ao PDF (não é uma simulação)."""
    s = store.get(doc_id)
    if pno < 0 or pno >= s.page_count:
        raise SessionError(404, "page_not_found", "Página inexistente.")
    ops = [o.as_dict() for o in req.ops if o.page == pno]
    analyses = s.ensure_analyses_for(ops)
    data = workers.run(workers.task_preview, str(s.original_path), s.password,
                       {k: v for k, v in analyses.items() if k == pno}, ops, pno, scale, str(s.images_dir))
    return _png(data, cache=False)


@app.post("/api/documents/{doc_id}/images")
async def upload_image(doc_id: str, file: UploadFile = File(...)):
    s = store.get(doc_id)
    data = await file.read(settings.max_image_bytes + 1)
    return s.save_image(data)


@app.post("/api/documents/{doc_id}/export")
def export(doc_id: str, req: ExportRequest):
    s = store.get(doc_id)
    if s.signed and not req.acknowledgeSignature:
        return err(409, "signature_ack_required",
                   "O documento possui assinatura digital que será invalidada pela edição. Confirme para continuar.")
    ops = [o.as_dict() for o in req.ops]
    pages = None
    if req.pages is not None:
        pages = sorted(set(req.pages))
        if not pages:
            return err(400, "no_pages", "Selecione pelo menos uma página para baixar.")
        if pages[0] < 0 or pages[-1] >= s.page_count:
            return err(400, "invalid_pages", f"Páginas inválidas: o documento tem {s.page_count} página(s).")
        if pages == list(range(s.page_count)):
            pages = None
        else:
            ops = [o for o in ops if int(o.get("page", -1)) in set(pages)]
    if not ops and pages is None:
        return err(400, "no_changes", "Nenhuma alteração para salvar.")
    analyses = s.ensure_analyses_for(ops)
    try:
        data, payload = workers.run(workers.task_export, str(s.original_path), s.password, analyses, ops,
                                    str(s.images_dir), pages)
    except (OpError, SessionError, workers.WorkerError):
        raise
    except Exception as exc:  # falha inesperada: nunca devolver o original como se fosse o editado
        log.exception("Falha na exportação")
        return err(500, "export_failed", f"Falha ao gerar o PDF: {exc}")
    if data is None:
        return JSONResponse(status_code=422, content={
            "error": {"code": "verification_failed",
                      "message": "O PDF gerado não passou na verificação e não foi disponibilizado.",
                      "report": payload}})
    token = secrets.token_hex(16)
    out_path = s.dir / f"export-{token}.pdf"
    out_path.write_bytes(data)
    # mantém somente a exportação mais recente
    for old in list(s.exports.values()):
        Path(old).unlink(missing_ok=True)
    s.exports = {token: out_path}
    stem = Path(s.filename).stem or "documento"
    payload.update({"downloadUrl": f"/api/documents/{s.id}/download/{token}", "filename": f"{stem}-editado.pdf"})
    if s.signed:
        payload["warnings"].insert(0, "A assinatura digital original não é mais válida neste arquivo.")
    return payload


@app.get("/api/documents/{doc_id}/download/{token}")
def download(doc_id: str, token: str):
    s = store.get(doc_id)
    path = s.exports.get(token)
    if path is None or not path.exists():
        return err(404, "export_not_found", "Arquivo exportado não encontrado. Gere o PDF novamente.")
    stem = Path(s.filename).stem or "documento"
    return FileResponse(path, media_type="application/pdf", filename=f"{stem}-editado.pdf",
                        headers={"Cache-Control": "no-store"})


@app.delete("/api/documents/{doc_id}")
def close_document(doc_id: str):
    return {"deleted": store.delete(doc_id)}


# ---------------------------------------------------------------------------- frontend
_DIST = PROJECT_DIR / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        candidate = (_DIST / full_path).resolve()
        if full_path and candidate.is_file() and _DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
