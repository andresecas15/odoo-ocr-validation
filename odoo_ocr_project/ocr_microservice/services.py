import base64
import os
from io import BytesIO
from typing import Optional

import numpy as np
from fastapi import HTTPException
from pdf2image import convert_from_bytes
from ultralytics import YOLO

from config import (
    DATE_REGEX,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL_NAME,
    YOLO_CLASS_MAP,
    YOLO_CONFIDENCE_THRESHOLD,
    YOLO_MODEL_PATH,
    logger,
)

ocr_client = None
yolo_model = None


def load_models() -> None:
    """Carga los modelos de IA al iniciar la aplicación."""
    global ocr_client, yolo_model

    if LLM_API_KEY:
        try:
            # Prescindimos de la librería openai ya que tiene conflictos de dependencias con httpx.
            # Enrutaremos peticiones JSON crudas mediante la librería "requests".
            ocr_client = "http_client_enabled"
            logger.info("Cliente HTTP nativo hacia Ollama / LLM configurado exitosamente (%s).", LLM_MODEL_NAME)
        except Exception as exc:
            logger.warning("Error al configurar cliente LLM: %s", exc)
            ocr_client = None
    else:
        logger.warning(
            "═══════════════════════════════════════════════════════════════\n"
            "  ⚠️  FALTA LA CLAVE DE API DEL LLM (LLM_API_KEY)\n"
            "  → La extracción de texto (OCR) con %s fallará.\n"
            "  → Agrega 'LLM_API_KEY' a tu docker-compose.yml o sistema.\n"
            "═══════════════════════════════════════════════════════════════",
            LLM_MODEL_NAME
        )
        ocr_client = None

    if os.path.isfile(YOLO_MODEL_PATH):
        logger.info("Cargando modelo YOLO desde '%s'...", YOLO_MODEL_PATH)
        try:
            import torch
            
            # Temporary patch to torch.load to force weights_only=False for YOLO model
            original_load = torch.load
            def patched_load(*args, **kwargs):
                kwargs['weights_only'] = False
                return original_load(*args, **kwargs)
            
            torch.load = patched_load
            try:
                yolo_model = YOLO(YOLO_MODEL_PATH)
            finally:
                torch.load = original_load
                
            logger.info("Modelo YOLO cargado exitosamente.")
        except Exception as exc:
            logger.warning(
                "No se pudo cargar el modelo YOLO: %s. "
                "La detección de firmas/huellas estará deshabilitada.",
                exc,
            )
            yolo_model = None
    else:
        logger.warning(
            "═══════════════════════════════════════════════════════════════\n"
            "  ⚠️  MODELO YOLO NO ENCONTRADO – detección visual DESHABILITADA\n"
            "  Ruta esperada: %s\n"
            "  → Entrena tu modelo y copia 'best.pt' en la carpeta models_ml/\n"
            "  → El OCR de texto seguirá operativo.\n"
            "═══════════════════════════════════════════════════════════════",
            YOLO_MODEL_PATH,
        )
        yolo_model = None


def decode_pdf(file_data_b64: str) -> bytes:
    try:
        return base64.b64decode(file_data_b64)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Error al decodificar el archivo Base64: {exc}",
        ) from exc


def pdf_to_images(pdf_bytes: bytes) -> list:
    try:
        # Mejor DPI para que el Vision Model pueda leer las tablas pequeñas ("Datos Laborales")
        # sin quedarse sin RAM (OOM) en un servidor local. 200 dpi es el balance óptimo.
        images = convert_from_bytes(pdf_bytes, dpi=200, fmt="jpeg")
        logger.info("PDF convertido a %d página(s).", len(images))
        return images
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Error al convertir el PDF a imágenes: {exc}",
        ) from exc


def extract_text_statistics(full_text: str) -> tuple[int, int, int, Optional[str]]:
    import re
    fecha_word_count = len(re.findall(r"(?i)\bfechas?\b", full_text))
    firma_word_count = len(re.findall(r"(?i)\bfirm\w*", full_text))
    
    matches = DATE_REGEX.findall(full_text)
    fecha_value_count = len(matches)
    
    fecha_str = None
    if matches:
        fechas_unicas = []
        for day, month, year in matches:
            f_format = f"{day}/{month}/{year}"
            if f_format not in fechas_unicas:
                fechas_unicas.append(f_format)
        
        fecha_str = " | ".join(fechas_unicas)
        logger.info("Fechas encontradas: %s", fecha_str)
        
    return fecha_word_count, firma_word_count, fecha_value_count, fecha_str


def run_ocr(images: list) -> str:
    import time
    if ocr_client is None:
        logger.error("Cliente LLM no está inicializado.")
        return ""

    logger.info("Preparando %d páginas para enviar a Ollama de a 1 por vez para ahorrar RAM en CPU...", len(images))
    
    extracted_text_parts = []
    
    # Procesar página por página para evitar desborde de RAM en LLM Local
    for i, pil_image in enumerate(images):
        logger.info("Procesando página %d/%d con modelo Ollama (%s)...", i+1, len(images), LLM_MODEL_NAME)
        
        # Convertir a Base64 con alta calidad para evitar difuminado de números
        buffered = BytesIO()
        pil_image.save(buffered, format="JPEG", quality=90)
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        
        messages = [
            {
                "role": "system",
                "content": (
                    "Eres un asistente experto en extracción de datos estructurados a partir de expedientes de crédito. "
                    "Tu objetivo es leer la imagen y extraer EXACTAMENTE los campos solicitados utilizando el formato clave-valor. "
                    "REGLAS CRÍTICAS:\n"
                    "1. Busca exhaustivamente en toda la imagen (tablas, encabezados, letras pequeñas, casillas de verificación).\n"
                    "2. NO inventes datos. Si un campo no está, escribe 'NO ENCONTRADO'.\n"
                    "3. DEBES devolver tu respuesta respetando exactamente esta plantilla:\n\n"
                    "Cedula: [valor]\n"
                    "Motivo de Prestamo: [tipo de crédito o préstamo, ej. PRESTAMOS CIES]\n"
                    "Numero de Seguro Social: [NSS si lo hay]\n"
                    "Referencia Bancaria: [mencionar si está presente]\n"
                    "Referencia Personal: [mencionar si está presente]\n"
                    "Cargo o Posicion: [ej. Educador, Doctor, etc.]\n"
                    "Rango Salarial o Salario: [Rango exacto o monto, ej. 1500.01 - 1800.00 o 1500.00]\n"
                    "Lugar de Nacimiento: [Provincia visible, ej. Veraguas, Chiriqui, Panama]\n"
                    "Efectividad: [mencionar si existe]\n"
                    "Numero de Planilla: [valor]\n"
                    "Estado Civil: [valor]\n"
                    "Texto Adicional: [breve resumen de campos extra que consideres útiles, como fechas o montos adicionales]"
                )
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text", 
                        "text": "Analiza la imagen y extrae la información completando estrictamente la plantilla solicitada. Presta mucha atención a las 'Referencias Bancarias/Personales' escritas en listas, a la 'Provincia' en el campo de nacimiento, al 'Motivo/Tipo de préstamo', y al 'Salario' en formato de moneda."
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_str}"
                        }
                    }
                ]
            }
        ]
        
        try:
            import requests
            
            payload = {
                "model": LLM_MODEL_NAME,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 2048
                }
            }
            
            response = requests.post(f"{LLM_BASE_URL}/chat/completions", json=payload, timeout=3600)
            response.raise_for_status()
            
            data = response.json()
            page_text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            
            extracted_text_parts.append(page_text)
            logger.info("✓ Página %d completada (%d caracteres).", i+1, len(page_text))
            
        except Exception as e:
            logger.error("Error en Ollama para página %d: %s", i+1, e)
            extracted_text_parts.append(f"[Error leyendo página {i+1}]")

    extracted_text = "\n\n".join(extracted_text_parts)

    logger.info("LLM OCR completado exitosamente con Ollama. Texto total: %d caracteres.", len(extracted_text))
    # Para monitoreo local
    logger.info("===== DEBUG TEXTO OLLAMA =====")
    if extracted_text:
        logger.info(extracted_text[:1000] + "\n...[truncado]")
    logger.info("==============================")
    
    return extracted_text



def run_yolo(images: list) -> tuple[int, int]:
    if yolo_model is None:
        logger.info("Modelo YOLO no disponible. Saltando detección de firmas/huellas.")
        return 0, 0

    firma_count = 0
    huella_count = 0

    for page_idx, pil_image in enumerate(images):
        img_array = np.array(pil_image)

        try:
            results = yolo_model.predict(
                source=img_array,
                conf=YOLO_CONFIDENCE_THRESHOLD,
                verbose=False,
            )
        except Exception as exc:
            logger.warning("Error en YOLO para página %d: %s", page_idx + 1, exc)
            continue

        for result in results:
            if result.boxes is None or len(result.boxes) == 0:
                continue

            for box in result.boxes:
                class_id = int(box.cls[0])
                confidence = float(box.conf[0])
                class_name = YOLO_CLASS_MAP.get(class_id, f"clase_{class_id}")

                logger.info(
                    "Detección en página %d: %s (confianza: %.2f%%)",
                    page_idx + 1,
                    class_name,
                    confidence * 100,
                )

                if class_id == 0:
                    firma_count += 1
                elif class_id == 1:
                    huella_count += 1

    return firma_count, huella_count


def run_yolo_detailed(images: list) -> tuple[int, int, list, list, list[int], list[int]]:
    """Ejecuta YOLO y retorna conteos, bounding boxes Y páginas por clase.

    Returns:
        firma_count   (int)       – número de firmas detectadas
        huella_count  (int)       – número de huellas detectadas
        boxes_firma   (list)      – lista de [x1, y1, x2, y2] para cada firma
        boxes_huella  (list)      – lista de [x1, y1, x2, y2] para cada huella
        pages_firma   (list[int]) – páginas (1-indexed) con al menos 1 firma
        pages_huella  (list[int]) – páginas (1-indexed) con al menos 1 huella
    """
    if yolo_model is None:
        logger.info("Modelo YOLO no disponible. Saltando detección detallada.")
        return 0, 0, [], [], [], []

    firma_count = 0
    huella_count = 0
    boxes_firma: list[list[float]] = []
    boxes_huella: list[list[float]] = []
    pages_firma: list[int] = []
    pages_huella: list[int] = []

    for page_idx, pil_image in enumerate(images):
        img_array = np.array(pil_image)
        page_num = page_idx + 1  # 1-indexed para el usuario

        try:
            results = yolo_model.predict(
                source=img_array,
                conf=YOLO_CONFIDENCE_THRESHOLD,
                verbose=False,
            )
        except Exception as exc:
            logger.warning("Error en YOLO (detallado) página %d: %s", page_num, exc)
            continue

        page_has_firma = False
        page_has_huella = False

        for result in results:
            if result.boxes is None or len(result.boxes) == 0:
                continue

            for box in result.boxes:
                class_id = int(box.cls[0])
                confidence = float(box.conf[0])
                class_name = YOLO_CLASS_MAP.get(class_id, f"clase_{class_id}")
                coords = box.xyxy[0].tolist()  # [x1, y1, x2, y2]

                logger.info(
                    "Detección detallada p.%d: %s conf=%.2f%% bbox=%s",
                    page_num, class_name, confidence * 100, coords,
                )

                if class_id == 0:
                    firma_count += 1
                    boxes_firma.append(coords)
                    page_has_firma = True
                elif class_id == 1:
                    huella_count += 1
                    boxes_huella.append(coords)
                    page_has_huella = True

        if page_has_firma:
            pages_firma.append(page_num)
        if page_has_huella:
            pages_huella.append(page_num)

    return firma_count, huella_count, boxes_firma, boxes_huella, pages_firma, pages_huella

