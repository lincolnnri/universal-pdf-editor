FROM node:22-slim AS web
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-por tesseract-ocr-eng fontconfig fonts-liberation fonts-dejavu-core fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY --from=web /app/frontend/dist frontend/dist
RUN useradd -m editor && mkdir -p /tmp/universal-pdf-editor && chown editor /tmp/universal-pdf-editor
USER editor
WORKDIR /app/backend
ENV UPE_MAX_UPLOAD_MB=50 PYTHONUNBUFFERED=1
EXPOSE 8000
# Railway/Render/Fly definem $PORT; localmente usa 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
