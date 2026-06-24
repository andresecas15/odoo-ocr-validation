import base64
import hashlib
import json
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

    # YOLO reactivado para el servidor con GPU.
    # logger.info("Modelo YOLO DESACTIVADO manual/intencionalmente. La detección de firmas dependerá del LLM.")
    # yolo_model = None
    
    if os.path.isfile(YOLO_MODEL_PATH):
        logger.info("Cargando modelo YOLO desde '%s'...", YOLO_MODEL_PATH)
        try:
            import torch
            
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
                "No se pudo cargar el modelo YOLO: %s.", exc
            )
            yolo_model = None
    else:
        logger.warning("MODELO YOLO NO ENCONTRADO.")
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


def extract_text_statistics(full_text: str) -> tuple[int, int, int, Optional[str], bool]:
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
        
    llm_firma_detected = bool(re.search(r'(?i)firmas?\s*[:\-]?\s*s[ií]\b', full_text))
        
    return fecha_word_count, firma_word_count, fecha_value_count, fecha_str, llm_firma_detected


def run_ocr(images: list) -> str:
    import time
    from concurrent.futures import ThreadPoolExecutor
    if ocr_client is None:
        logger.error("Cliente LLM no está inicializado.")
        return ""

    logger.info("Preparando %d páginas para enviar a Ollama concurrentemente...", len(images))
    
    extracted_text_parts = [None] * len(images)
    
    def process_page(i: int, pil_image) -> None:
        logger.info("Procesando página %d/%d con modelo Ollama (%s)...", i+1, len(images), LLM_MODEL_NAME)
        
        # Redimensionar la imagen si supera 1024px en algún eje para reducir tokens de visión en Ollama
        max_size = 1024
        if pil_image.width > max_size or pil_image.height > max_size:
            pil_image = pil_image.copy()
            pil_image.thumbnail((max_size, max_size))

        # Convertir a Base64 con alta calidad para evitar difuminado de números
        buffered = BytesIO()
        pil_image.save(buffered, format="JPEG", quality=90)
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        
        messages = [
            {
                "role": "system",
                "content": (
                    "Eres un sistema de OCR y análisis visual de alta precisión. Tu tarea consiste en procesar la imagen de una página de un expediente de préstamo en dos partes:\n\n"
                    "1. CHEQUEOS VISUALES: Al inicio de tu respuesta, añade obligatoriamente una línea separadora '--- CHEQUEOS VISUALES ---' y proporciona la siguiente plantilla completada con metadatos sobre la página:\n"
                    "Firma Cliente: [SI o NO si detectas la firma manuscrita del deudor/cliente en esta página]\n"
                    "Huella Cliente: [SI o NO si detectas la huella dactilar o el óvalo gris del deudor/cliente en esta página, incluso si es muy leve, tenue, borrosa o apenas visible al lado de la firma]\n"
                    "Relacion Firma Huella: [CORRECTA, SOLAPADA, LEJOS o NO APLICA. Si hay firma y huella del cliente en la página, evalúa si la huella está al lado/encima de la firma o alejada. Si no hay huella o firma, escribe NO APLICA]\n"
                    "Firma Oficial: [SI o NO si detectas la firma de aprobación de un oficial o gerente en las áreas de firmas de la entidad]\n\n"
                    "A continuación, añade una línea separadora '--- FIN CHEQUEOS VISUALES ---' y continúa con:\n"
                    "2. TRANSCRIPCIÓN LITERAL (OCR): Transcribe de forma exacta, literal y línea por línea todo el texto visible en la imagen (incluyendo nombres, cédulas, tablas, encabezados, cargos, montos, direcciones y fechas). NO resumas, NO agrupes en categorías creadas por ti y NO omitas información."
                )
            },
            {
                "role": "user",
                "content": "Realiza primero los chequeos visuales completando la plantilla bajo la línea '--- CHEQUEOS VISUALES ---'. Luego, tras la línea '--- FIN CHEQUEOS VISUALES ---', transcribe literalmente todo el texto visible en la imagen de la página.",
                "images": [img_str]
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
                    "num_predict": 2048,
                    "num_ctx": 8192
                }
            }
            
            # Usar API nativa de Ollama /api/chat
            api_url = LLM_BASE_URL.replace('/v1', '/api/chat') if '/v1' in LLM_BASE_URL else f"{LLM_BASE_URL}/api/chat"
            response = requests.post(api_url, json=payload, timeout=3600)
            if response.status_code != 200:
                logger.error("Ollama error response: %s", response.text)
            response.raise_for_status()
            
            data = response.json()
            page_text = data.get("message", {}).get("content", "").strip()
            
            extracted_text_parts[i] = page_text
            logger.info("✓ Página %d completada (%d caracteres).", i+1, len(page_text))
            
        except Exception as e:
            logger.error("Error en Ollama para página %d: %s", i+1, e)
            extracted_text_parts[i] = f"[Error leyendo página {i+1}]"

    # Procesar concurrentemente con ThreadPoolExecutor (max_workers=4)
    with ThreadPoolExecutor(max_workers=4) as executor:
        executor.map(lambda pair: process_page(pair[0], pair[1]), enumerate(images))

    extracted_text = "\n\n--- PAGE_SEPARATOR ---\n\n".join(extracted_text_parts)

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


def audit_validations_with_llm(results: list, full_text: str) -> list:
    """
    Audita los resultados que pasaron la validación de regex para detectar falsos positivos.
    Utiliza el LLM para dar una opinión semántica.
    """
    if ocr_client is None:
        logger.warning("Cliente LLM no disponible para auditoría.")
        return results

    # Buscar qué reglas pasaron y tienen valores extraídos
    # Únicamente auditamos VL-10 (Planilla) para evitar falsos positivos
    # Las validaciones de la 1 a la 7 (VL-01 a VL-07) se mantienen puramente con regex/OCR directo.
    to_audit = {}
    for r in results:
        if not r.passed:
            continue
        
        val = None
        if r.code == "VL-10":
            import re
            m = re.search(r"detectada:\s*(\S+)", r.detail)
            if m: val = m.group(1)
            
        if val:
            to_audit[r.code] = {
                "field_name": r.label,
                "detected_value": val,
                "result_obj": r
            }

    if not to_audit:
        return results

    fields_desc = []
    for code, info in to_audit.items():
        fields_desc.append(f"- Regla {code} ({info['field_name']}): Valor detectado: '{info['detected_value']}'")
        
    fields_list_str = "\n".join(fields_desc)
    
    prompt = (
        "Actúa como un oficial de cumplimiento y auditor de datos. El sistema automático de expresiones regulares "
        "ha extraído los siguientes valores provisionales de un expediente de préstamo:\n\n"
        f"{fields_list_str}\n\n"
        "A partir del siguiente texto del documento extraído por OCR, audita cada uno de estos valores para verificar si son correctos o si son FALSOS POSITIVOS "
        "(por ejemplo: si el salario es en realidad un año, o si la planilla es en realidad otra cédula, o si no corresponde al campo del cliente).\n\n"
        "Texto del documento:\n"
        "\"\"\"\n"
        f"{full_text[:4000]}\n"
        "\"\"\"\n\n"
        "Responde ESTRICTAMENTE en formato JSON con la siguiente estructura y nada más:\n"
        "{\n"
        "  \"VL-10\": {\"is_correct\": true, \"corrected_value\": \"8-21-06-0-01979\", \"reason\": \"El número de planilla coincide con la nómina de gobierno\"}\n"
        "}"
    )
    
    messages = [
        {
            "role": "user",
            "content": prompt
        }
    ]
    
    payload = {
        "model": LLM_MODEL_NAME,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_predict": 1024
        }
    }
    
    try:
        import requests
        import json
        
        logger.info("Enviando auditoría de falsos positivos al LLM para las reglas: %s...", ", ".join(to_audit.keys()))
        response = requests.post(f"{LLM_BASE_URL}/chat/completions", json=payload, timeout=60)
        response.raise_for_status()
        
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        
        if content.startswith("```json"):
            content = content[7:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        
        audit_results = json.loads(content)
        
        for code, audit_info in audit_results.items():
            if code in to_audit:
                is_correct = audit_info.get("is_correct", True)
                reason = audit_info.get("reason", "")
                
                if not is_correct:
                    r = to_audit[code]["result_obj"]
                    r.passed = False
                    r.detail = f"Falso positivo detectado por LLM: {reason}"
                    logger.warning("⚠️ Falso positivo confirmado por LLM en regla %s: %s", code, reason)
                    
    except Exception as e:
        logger.error("Error en la auditoría del LLM: %s", e)
        
    return results


def parse_llm_visual_checks_from_text(extracted_text: str) -> tuple[bool, list[int], list[int], list[str]]:
    """
    Analiza el texto OCR consolidado página por página y extrae el estado
    de firmas, huellas y su proximidad semántica.
    
    Returns:
        firma_detected (bool)       – ¿se detectó firma en alguna página?
        pages_firma    (list[int])  – páginas con firma (1-indexed)
        pages_huella   (list[int])  – páginas con huella (1-indexed)
        relations      (list[str])  – lista de relaciones de proximidad de huella-firma encontradas
    """
    import re
    if "--- PAGE_SEPARATOR ---" in extracted_text:
        pages = extracted_text.split("--- PAGE_SEPARATOR ---")
    else:
        pages = extracted_text.split("\n\n")
    pages_firma = []
    pages_huella = []
    relations = []
    firma_detected = False
    
    for i, page_text in enumerate(pages):
        page_num = i + 1
        
        # Firma Cliente: SI/NO
        m_fc = re.search(r"(?i)Firma\s+Cliente:\s*(SI|NO)", page_text)
        m_f = re.search(r"(?i)Firmas?:\s*(SI|NO)", page_text)
        
        is_firma = False
        if m_fc and m_fc.group(1).upper() == "SI":
            is_firma = True
        elif m_f and m_f.group(1).upper() == "SI":
            is_firma = True
            
        # Firma Oficial: SI/NO (también cuenta como firma en el documento)
        m_fo = re.search(r"(?i)Firma\s+Oficial:\s*(SI|NO)", page_text)
        if m_fo and m_fo.group(1).upper() == "SI":
            is_firma = True
            
        if is_firma:
            pages_firma.append(page_num)
            firma_detected = True
            
        # Huella Cliente: SI/NO
        m_hc = re.search(r"(?i)Huella\s+Cliente:\s*(SI|NO)", page_text)
        is_huella = False
        if m_hc and m_hc.group(1).upper() == "SI":
            is_huella = True
            
        # Relacion Firma Huella: CORRECTA/SOLAPADA/LEJOS/NO APLICA
        m_rel = re.search(r"(?i)Relacion\s+Firma\s+Huella:\s*(\S+)", page_text)
        if m_rel:
            val = m_rel.group(1).upper().replace(",", "").replace(".", "").replace("[", "").replace("]", "").strip()
            if val in ("CORRECTA", "SOLAPADA", "LEJOS"):
                relations.append(val)
                is_huella = True
            elif val == "NO APLICA":
                relations.append(val)
                
        if is_huella:
            pages_huella.append(page_num)
                
    return firma_detected, pages_firma, pages_huella, relations


def get_ocr_cache_path() -> str:
    """Retorna la ruta del archivo de caché de OCR."""
    cache_dir = "/app/models_ml"
    if not os.path.exists(cache_dir):
        # Fallback para desarrollo local fuera de Docker
        cache_dir = os.path.join(os.path.dirname(__file__), "models_ml")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "ocr_text_cache.json")


def get_cached_ocr(file_data_b64: str, model_name: str) -> Optional[str]:
    """Obtiene el texto OCR en caché a partir del hash del Base64 del PDF y el nombre del modelo."""
    if not file_data_b64 or not model_name:
        return None
    try:
        file_hash = hashlib.sha256(file_data_b64.encode('utf-8')).hexdigest()
        cache_key = f"{model_name}_{file_hash}"
        cache_path = get_ocr_cache_path()
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
                if cache_key in cache_data:
                    logger.info("⚡ Caché de OCR HIT para modelo %s y hash %s. Evitando procesamiento.", model_name, file_hash)
                    return cache_data[cache_key]
    except Exception as e:
        logger.warning("Error leyendo caché de OCR: %s", e)
    return None


def save_cached_ocr(file_data_b64: str, text: str, model_name: str) -> None:
    """Guarda el texto OCR en caché con clave basada en el modelo y el hash."""
    if not file_data_b64 or not text or not model_name:
        return
    try:
        file_hash = hashlib.sha256(file_data_b64.encode('utf-8')).hexdigest()
        cache_key = f"{model_name}_{file_hash}"
        cache_path = get_ocr_cache_path()
        cache_data = {}
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                try:
                    cache_data = json.load(f)
                except Exception:
                    cache_data = {}
        cache_data[cache_key] = text
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
        logger.info("✓ Guardado en caché de OCR para modelo %s bajo hash %s.", model_name, file_hash)
    except Exception as e:
        logger.warning("Error guardando caché de OCR: %s", e)



