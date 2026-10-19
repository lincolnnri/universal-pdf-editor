"""Configuração central — todos os valores podem ser sobrescritos por variáveis de ambiente
(ou por um arquivo .env na raiz do projeto)."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent


def _load_dotenv() -> None:
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    max_upload_mb: int = field(default_factory=lambda: _int("UPE_MAX_UPLOAD_MB", 50))
    max_pages: int = field(default_factory=lambda: _int("UPE_MAX_PAGES", 300))
    max_image_mb: int = field(default_factory=lambda: _int("UPE_MAX_IMAGE_MB", 10))
    session_ttl_minutes: int = field(default_factory=lambda: _int("UPE_SESSION_TTL_MINUTES", 60))
    work_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("UPE_WORK_DIR", Path(tempfile.gettempdir()) / "universal-pdf-editor"))
    )
    ocr_languages: str = field(default_factory=lambda: os.environ.get("UPE_OCR_LANGS", "por+eng"))
    ocr_dpi: int = field(default_factory=lambda: _int("UPE_OCR_DPI", 300))
    ocr_timeout_s: int = field(default_factory=lambda: _int("UPE_OCR_TIMEOUT_S", 120))
    tessdata_dir: str = field(
        default_factory=lambda: os.environ.get(
            "UPE_TESSDATA_DIR", str(BACKEND_DIR / "tessdata") if (BACKEND_DIR / "tessdata").exists() else ""
        )
    )
    # Tolerância (fração de pixels alterados) aceita fora das áreas editadas na verificação.
    verify_outside_tolerance: float = field(default_factory=lambda: _float("UPE_VERIFY_OUTSIDE_TOLERANCE", 0.002))
    # processos auxiliares para processar PDFs em paralelo (0 = no próprio processo)
    workers: int = field(default_factory=lambda: _int("UPE_WORKERS", min(4, os.cpu_count() or 2)))
    task_timeout_s: int = field(default_factory=lambda: _int("UPE_TASK_TIMEOUT_S", 180))
    cors_origins: str = field(default_factory=lambda: os.environ.get("UPE_CORS_ORIGINS", "http://localhost:5173"))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_image_bytes(self) -> int:
        return self.max_image_mb * 1024 * 1024


settings = Settings()
settings.work_dir.mkdir(parents=True, exist_ok=True)
