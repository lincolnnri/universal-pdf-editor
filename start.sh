#!/usr/bin/env bash
# Inicia o Universal PDF Editor (backend + interface) em http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "→ Criando ambiente Python (.venv)"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
if [ ! -f .venv/.deps-ok ] || [ backend/requirements.txt -nt .venv/.deps-ok ]; then
  echo "→ Instalando dependências do backend"
  pip install -q --upgrade pip
  pip install -q -r backend/requirements-dev.txt
  touch .venv/.deps-ok
fi

if ! command -v tesseract >/dev/null 2>&1; then
  echo "⚠  Tesseract OCR não encontrado: PDFs digitalizados não poderão ser editados."
  echo "   Instale com: sudo apt install tesseract-ocr   (macOS: brew install tesseract)"
fi

if [ ! -f frontend/dist/index.html ] || [ -n "$(find frontend/src -newer frontend/dist/index.html -print -quit 2>/dev/null)" ]; then
  echo "→ Compilando a interface"
  (cd frontend && { [ -d node_modules ] || npm install --no-audit --no-fund; } && npm run build)
fi

PORT=${PORT:-8000}
echo "→ Universal PDF Editor em http://localhost:${PORT}"
cd backend
exec uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "$PORT"
