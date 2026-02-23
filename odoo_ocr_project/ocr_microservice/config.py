import logging
import os
import re

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ocr_engine")

# Mapa de clases YOLO esperadas
# class_id 0 = firma, class_id 1 = huella
YOLO_CLASS_MAP = {
    0: "firma",
    1: "huella",
}

YOLO_MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "models_ml",
    "best.pt",
)

# Umbral de confianza mínima para detecciones YOLO
YOLO_CONFIDENCE_THRESHOLD = 0.5

# Regex robusta para detección de fechas en múltiples formatos
# Soporta: dd/mm/yyyy, dd-mm-yyyy, dd/mm/yy, d/m/yyyy, "16 de febrero de 2026", etc.
DATE_REGEX = re.compile(
    r"\b(\d{1,2})\s*(?:[-/\.]|\s+de\s+)\s*([a-zA-Z]+|\d{1,2})\s*(?:[-/\.]|\s+(?:de|del)\s+)\s*(\d{2,4})\b",
    re.IGNORECASE
)
