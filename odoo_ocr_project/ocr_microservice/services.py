import base64
import os
from typing import Optional

import numpy as np
from fastapi import HTTPException
from paddleocr import PaddleOCR
from pdf2image import convert_from_bytes
from ultralytics import YOLO

from config import (
    DATE_REGEX,
    YOLO_CLASS_MAP,
    YOLO_CONFIDENCE_THRESHOLD,
    YOLO_MODEL_PATH,
    logger,
)

ocr_model = None
yolo_model = None


def load_models() -> None:
    """Carga los modelos de IA al iniciar la aplicación."""
    global ocr_model, yolo_model

    logger.info("Cargando PaddleOCR (lang='es', use_angle_cls=True)...")
    try:
        ocr_model = PaddleOCR(use_angle_cls=True, lang="es", show_log=False, use_gpu=False)
        logger.info("PaddleOCR cargado exitosamente.")
    except Exception as exc:
        logger.error("Error al cargar PaddleOCR: %s", exc)
        raise RuntimeError(f"No se pudo inicializar PaddleOCR: {exc}") from exc

    if os.path.isfile(YOLO_MODEL_PATH):
        logger.info("Cargando modelo YOLO desde '%s'...", YOLO_MODEL_PATH)
        try:
            yolo_model = YOLO(YOLO_MODEL_PATH)
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
            "  → El OCR de texto (PaddleOCR) seguirá operativo.\n"
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
        images = convert_from_bytes(pdf_bytes, dpi=200, fmt="png")
        logger.info("PDF convertido a %d página(s).", len(images))
        return images
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Error al convertir el PDF a imágenes: {exc}",
        ) from exc


def extract_text_statistics(full_text: str) -> tuple[int, int, int, Optional[str]]:
    import re
    # Contar palabra "fecha" o "fechas"
    fecha_word_count = len(re.findall(r"(?i)\bfechas?\b", full_text))
    # Contar palabra relacionada a firma (firma, firman, firmado, firmas)
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
    if ocr_model is None:
        logger.error("PaddleOCR no está inicializado.")
        return ""

    all_text_parts: list[str] = []

    for page_idx, pil_image in enumerate(images):
        img_array = np.array(pil_image)

        try:
            result = ocr_model.ocr(img_array, cls=True)
        except Exception as exc:
            logger.warning("Error en OCR para página %d: %s", page_idx + 1, exc)
            continue

        if result and result[0]:
            for line in result[0]:
                if line and len(line) >= 2:
                    text_content = line[1][0] if isinstance(line[1], (list, tuple)) else str(line[1])
                    all_text_parts.append(text_content)

    full_text = " ".join(all_text_parts)
    logger.info("OCR completado. Texto total extraído: %d caracteres.", len(full_text))
    return full_text


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


def run_yolo_detailed(images: list) -> tuple[int, int, list, list]:
    """Ejecuta YOLO y retorna conteos MÁS las listas de bounding boxes por clase.

    Returns:
        firma_count  (int)  – número de firmas detectadas
        huella_count (int)  – número de huellas detectadas
        boxes_firma  (list) – lista de [x1, y1, x2, y2] para cada firma
        boxes_huella (list) – lista de [x1, y1, x2, y2] para cada huella
    """
    if yolo_model is None:
        logger.info("Modelo YOLO no disponible. Saltando detección detallada.")
        return 0, 0, [], []

    firma_count = 0
    huella_count = 0
    boxes_firma: list[list[float]] = []
    boxes_huella: list[list[float]] = []

    for page_idx, pil_image in enumerate(images):
        img_array = np.array(pil_image)

        try:
            results = yolo_model.predict(
                source=img_array,
                conf=YOLO_CONFIDENCE_THRESHOLD,
                verbose=False,
            )
        except Exception as exc:
            logger.warning("Error en YOLO (detallado) página %d: %s", page_idx + 1, exc)
            continue

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
                    page_idx + 1, class_name, confidence * 100, coords,
                )

                if class_id == 0:
                    firma_count += 1
                    boxes_firma.append(coords)
                elif class_id == 1:
                    huella_count += 1
                    boxes_huella.append(coords)

    return firma_count, huella_count, boxes_firma, boxes_huella

