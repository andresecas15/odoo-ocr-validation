# ──────────────────────────────────────────────────────────
# Dockerfile – Microservicio OCR + YOLO (FastAPI)
# Base: Python 3.10 slim para mantener imagen ligera
# ──────────────────────────────────────────────────────────
FROM python:3.10-slim

# Variables de entorno para evitar prompts interactivos
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dependencias de sistema necesarias para:
# - poppler-utils: conversión PDF → imagen (pdf2image)
# - libgl1-mesa-glx + libglib2.0-0: OpenCV headless
# - curl: healthcheck del contenedor
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libgl1 \
    libglib2.0-0 \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar e instalar dependencias Python primero (aprovecha cache de Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar código de la aplicación
COPY *.py .

# Crear directorio para modelos ML (se monta como volumen en compose)
RUN mkdir -p /app/models_ml

# Puerto de exposición
EXPOSE 8000

# Comando de arranque
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
