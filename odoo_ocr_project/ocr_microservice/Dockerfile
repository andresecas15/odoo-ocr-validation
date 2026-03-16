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

# (Carga de pesos para YOLO opcional se mapea via volumen)


# Puerto de exposición
EXPOSE 8000

# Comando de arranque
# Estrategia multi-worker correcta para modelos ML pesados:
# - gunicorn actúa como process manager
# - --preload: carga el app (y los modelos PaddleOCR/YOLO) UNA vez en el padre
#   antes de forkear los workers → evita race conditions de descarga simultánea
# - -w 4: 4 workers (ajustar según RAM; cada worker hereda los modelos via CoW)
# - UvicornWorker: mantiene el loop async de FastAPI
CMD ["gunicorn", "main:app", \
     "-w", "4", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--preload", \
     "--timeout", "7200"]
