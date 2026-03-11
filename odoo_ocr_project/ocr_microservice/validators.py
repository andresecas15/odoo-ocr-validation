"""
Módulo de validaciones para documentos de Préstamos Cíes y Ons.

Cada función recibe el texto extraído por PaddleOCR (y datos auxiliares de YOLO)
y retorna un ValidationResult indicando si la regla se cumple o no.

Las validaciones se dividen en:
 - Checks de formato (regex): cédula, NSS, planilla, lugar de nacimiento
 - Checks de presencia: motivo, referencias, cargo, salario, cónyuge
 - Checks visuales: firma en cotización, proximidad huella-firma (requiere YOLO boxes)
 - Checks de longitud: dirección extensa
 - Checks de estado: efectividades (presencia del campo + aviso de revisión manual)
"""

import math
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ValidationResult:
    """Resultado de una regla de validación individual."""
    code: str           # Ej. "VL-01"
    label: str          # Nombre legible
    passed: bool        # True  → campo OK | False → requiere atención
    severity: str       # "error" | "warning"
    detail: Optional[str] = None  # Mensaje explicativo para el oficial


# ─── Patrones regex ────────────────────────────────────────────────────────────

# VL-01 – Cédula en formato panameño: 8-1234-56789 | PE-12-3456 | N-12-3456
_CEDULA_RE = re.compile(
    r'\b(?:[A-Z]{1,2}-\d{1,2}-\d{4,6}|\d{1,2}-\d{3,4}-\d{4,6})\b'
)

# VL-02 – Motivo de préstamo
# Captura el VALOR justo después del label «motivo [de préstamo]»
# Se limita a 80 chars y exige al menos 3 palabras para evitar capturar
# encabezados de tabla ("OFICIAL DE CRÉDITO Jeyse...").
_MOTIVO_KEYWORD_RE = re.compile(r'(?i)\bmotivo\b')
_MOTIVO_VALUE_RE = re.compile(
    r'(?i)\bmotivo\s*(?:de\s*pr[eé]stamo)?\s*[:\-=]?\s*'
    r'([A-Za-záéíóúÁÉÍÓÚñÑ][^\n\r]{4,80})'
)
# Palabras de encabezado que indican falso positivo en VL-02
_MOTIVO_FALSE_POSITIVE_RE = re.compile(
    r'(?i)\b(?:oficial|cr[eé]dito|firma|verie|planilla|nombre|tel[eé]fono)\b'
)

# VL-03 – Número de Seguro Social (CSS Panamá)
# Formato: 8-123-45678 (1-2 dígitos – 2-4 dígitos – 4-6 dígitos)
_NSS_RE = re.compile(r'\b(?:[1-9]-\d{2,4}-\d{4,6}|[1-9]-\d{3,5})\b')

# VL-04 – Referencias
_REF_BANCARIA_RE = re.compile(r'(?i)referencia\s*bancaria')
_REF_PERSONAL_RE = re.compile(r'(?i)referencia\s*personal')

# VL-05 – Cargo / Posición
# Excluye resultados con múltiples keywords de encabezado de tabla
_CARGO_VALUE_RE = re.compile(
    r'(?i)(?:cargo|posici[oó]n|ocupaci[oó]n)\s*[:\-]?\s*([^\n]{3,60})'
)
_CARGO_HEADER_RE = re.compile(
    r'(?i)(?:salario|tel[eé]fono|ingresos|otros|gobierno|planilla)', 
)

# VL-06 – Rango salarial
_SALARIO_KEYWORD_RE = re.compile(r'(?i)\b(?:salario|sueldo|ingreso)\b')
_SALARY_AMOUNT_RE = re.compile(
    r'B/\.?\s*[\d,]+\.?\d*|\$\s*[\d,]+\.?\d*|\b\d{3,}(?:[.,]\d{2})?\b'
)

# VL-07 – Lugar de nacimiento (Provincia + País)
# Captura máximo 60 chars después del label para no incluir otros campos
_NACIMIENTO_RE = re.compile(
    r'(?i)lugar\s+de\s+nacimiento\s*[:\-]?\s*([A-Za-záéíóúÁÉÍÓÚñÑ ,]{4,60})'
)
_PAIS_RE = re.compile(
    r'(?i)\b(?:Panam[áa]|Venezuela|Colombia|Costa\s*Rica|M[eé]xico|Ecuador|Per[uú]|'
    r'Rep[uú]blica\s+Dominicana|Cuba|El\s+Salvador|Honduras|Nicaragua|Guatemala|Bolivia)\b'
)
# Si el valor capturado contiene keywords de encabezado → falso positivo
_NACIMIENTO_HEADER_RE = re.compile(
    r'(?i)\b(?:fecha|cedula|c[eé]dula|civil|nacionalidad|vto|estado)\b'
)

# VL-08 – Efectividades
_EFECTIVIDAD_RE = re.compile(r'(?i)\befectividad\b')

# VL-09 – Cotización (para check de firma en cotización)
_COTIZACION_RE = re.compile(r'(?i)\bcotizaci[oó]n\b')

# VL-10 – Número de planilla (debe tener guiones: 1-234567 o 1-234-5678)
_PLANILLA_FIELD_RE = re.compile(r'(?i)\bplanilla\b')
_PLANILLA_NUM_RE = re.compile(r'\b\d+-\d+(?:-\d+)?\b')

# VL-11 – Dirección (longitud)
# Captura máximo 200 chars para evitar leer el documento completo
# cuando PaddleOCR no inserta saltos de línea
_DIRECCION_RE = re.compile(
    r'(?i)(?:direcci[oó]n|domicilio)\s*[:\-]?\s*([^\n\r]{5,200})'
)
_ADDRESS_MAX_CHARS = 120

# VL-12 – Estado civil y cónyuge
_CASADO_RE = re.compile(r'(?i)\b(?:casad[oa]|unid[oa])\b')
_ESTADO_CIVIL_RE = re.compile(r'(?i)estado\s+civil\s*[:\-]?\s*(\w+)')
_CONYUGUE_RE = re.compile(
    r'(?i)c[oó]nyuge|esposo|esposa|nombre\s+del\s+c[oó]nyuge'
)

# VL-13 – Proximidad huella-firma (distancia máxima en píxeles @ 200 DPI)
_PROXIMITY_MAX_PX = 600  # ~3 cm


# ─── Funciones de validación ──────────────────────────────────────────────────

def val_cedula_format(text: str) -> ValidationResult:
    """VL-01: Cédula de identidad presente y con formato válido."""
    matches = _CEDULA_RE.findall(text)
    passed = len(matches) > 0
    detail = (
        f"Cédulas encontradas: {', '.join(matches)}"
        if matches
        else "No se detectó ningún número de cédula con formato válido (ej. 8-1234-56789)."
    )
    return ValidationResult(
        code="VL-01",
        label="Cédula / Datos del cliente",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_motivo_prestamo(text: str) -> ValidationResult:
    """VL-02: Hoja de datos contiene motivo de préstamo con contenido."""
    keyword = bool(_MOTIVO_KEYWORD_RE.search(text))
    value_match = _MOTIVO_VALUE_RE.search(text)
    # Verificar falso positivo: el match capturó un encabezado de tabla
    is_false_positive = (
        value_match is not None
        and bool(_MOTIVO_FALSE_POSITIVE_RE.search(value_match.group(1)))
    )
    passed = bool(value_match and value_match.group(1).strip() and not is_false_positive)
    if not keyword:
        detail = "Campo 'Motivo de préstamo' ausente en el documento."
    elif not value_match or is_false_positive:
        detail = "Campo 'Motivo' encontrado pero el valor parece estar en blanco o no es legible."
    else:
        detail = f"Motivo detectado: «{value_match.group(1).strip()[:80]}»"
    return ValidationResult(
        code="VL-02",
        label="Motivo de préstamo",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_numero_seguro_social(text: str) -> ValidationResult:
    """VL-03: Número de Seguro Social (NSS) presente y con formato correcto."""
    matches = _NSS_RE.findall(text)
    passed = len(matches) > 0
    detail = (
        f"NSS detectado: {matches[0]}"
        if matches
        else "No se encontró un NSS con formato válido (ej. 8-123-4567). Verificar contra carta de trabajo."
    )
    return ValidationResult(
        code="VL-03",
        label="Número de seguro social (NSS)",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_referencias(text: str) -> ValidationResult:
    """VL-04: Referencias bancarias Y personales presentes (afecta hoja de datos y solicitud Cocotito)."""
    has_banco = bool(_REF_BANCARIA_RE.search(text))
    has_personal = bool(_REF_PERSONAL_RE.search(text))
    passed = has_banco and has_personal
    faltantes = []
    if not has_banco:
        faltantes.append("referencia bancaria")
    if not has_personal:
        faltantes.append("referencia personal")
    detail = (
        "Ambas referencias presentes (bancaria y personal)."
        if passed
        else f"Faltan: {', '.join(faltantes)}. Afecta hoja de datos y solicitud de cuenta ahorro Cocotito (pág. 1 y 2)."
    )
    return ValidationResult(
        code="VL-04",
        label="Referencias bancarias y personales",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_cargo_posicion(text: str) -> ValidationResult:
    """VL-05: Posición/cargo presente y con contenido (afecta hoja de datos y Cocotito pág. 1 y 2)."""
    match = _CARGO_VALUE_RE.search(text)
    # Falso positivo si el valor inmediato es un encabezado de tabla
    is_header = (
        match is not None
        and bool(_CARGO_HEADER_RE.search(match.group(1)))
    )
    passed = bool(match and match.group(1).strip() and not is_header)
    detail = (
        f"Cargo detectado: «{match.group(1).strip()[:60]}»"
        if passed
        else "No se encontró campo de cargo/posición con valor legible. Verificar coincidencia con carta de trabajo. Afecta hoja de datos y Cocotito pág. 1 y 2."
    )
    return ValidationResult(
        code="VL-05",
        label="Posición / Cargo",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_rango_salarial(text: str) -> ValidationResult:
    """VL-06: Rango salarial presente y con valor numérico visible."""
    has_keyword = bool(_SALARIO_KEYWORD_RE.search(text))
    amounts = _SALARY_AMOUNT_RE.findall(text)
    has_amount = len(amounts) > 0
    passed = has_keyword and has_amount
    if not has_keyword:
        detail = "No se encontró campo de salario/sueldo. Verificar coincidencia con carta de trabajo."
    elif not has_amount:
        detail = "Campo de salario encontrado pero sin monto numérico detectable."
    else:
        detail = f"Monto detectado: {amounts[0]}. Verificar que coincida con la carta de trabajo."
    return ValidationResult(
        code="VL-06",
        label="Rango salarial",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_lugar_nacimiento(text: str) -> ValidationResult:
    """VL-07: Lugar de nacimiento debe tener Provincia + País (afecta hoja de datos y Cocotito pág. 1)."""
    match = _NACIMIENTO_RE.search(text)
    # Verificar si el match capturó un encabezado de tabla en vez del valor real
    is_header = (
        match is not None
        and bool(_NACIMIENTO_HEADER_RE.search(match.group(1)))
    )
    if not match or is_header:
        return ValidationResult(
            code="VL-07",
            label="Lugar de nacimiento (Provincia + País)",
            passed=False,
            severity="error",
            detail="Campo 'Lugar de nacimiento' ausente, en blanco o ilegible. Debe indicar Provincia + País (ej. 'Panamá, Panamá'). Afecta hoja de datos y solicitud Cocotito pág. 1.",
        )
    value = match.group(1).strip()
    has_country = bool(_PAIS_RE.search(value))
    has_content = len(value) >= 4
    passed = has_content and has_country
    if not has_content:
        detail = "Campo de nacimiento vacío o demasiado corto."
    elif not has_country:
        detail = (
            f"Valor «{value[:60]}» no incluye el país. "
            "Debe indicar Provincia + País (ej. 'Panamá, Panamá'). "
            "Afecta hoja de datos y Cocotito pág. 1."
        )
    else:
        detail = f"Lugar de nacimiento detectado: «{value[:60]}»"
    return ValidationResult(
        code="VL-07",
        label="Lugar de nacimiento (Provincia + País)",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_efectividades(text: str) -> ValidationResult:
    """VL-08: Presencia del campo de efectividad en órdenes de descuento."""
    found = bool(_EFECTIVIDAD_RE.search(text))
    return ValidationResult(
        code="VL-08",
        label="Efectividades en órdenes de descuento",
        passed=found,
        severity="warning",
        detail=(
            "Campo 'Efectividad' detectado. Verificar manualmente que la fecha no esté vencida."
            if found
            else "No se detectó campo de efectividad. Revisar si aplica para este tipo de préstamo."
        ),
    )


def val_firma_cotizacion(text: str, firma_detected: bool) -> ValidationResult:
    """VL-09: Cotización debe llevar firma de oficial."""
    has_cotizacion = bool(_COTIZACION_RE.search(text))
    if not has_cotizacion:
        return ValidationResult(
            code="VL-09",
            label="Firma en cotización",
            passed=True,
            severity="warning",
            detail="No se detectó sección de cotización en este documento. Validación omitida.",
        )
    passed = firma_detected
    detail = (
        "Firma detectada en documento con cotización. ✅"
        if passed
        else "Cotización presente pero sin firma de oficial detectada por el modelo de visión. Revisar manualmente."
    )
    return ValidationResult(
        code="VL-09",
        label="Firma en cotización",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_numero_planilla(text: str) -> ValidationResult:
    """VL-10: Número de planilla presente y con guiones correctos (afecta F1-b en generales)."""
    has_field = bool(_PLANILLA_FIELD_RE.search(text))
    if not has_field:
        return ValidationResult(
            code="VL-10",
            label="Número de planilla",
            passed=False,
            severity="error",
            detail="No se encontró campo de número de planilla. Afecta F1-b en las generales.",
        )
    planilla_matches = _PLANILLA_NUM_RE.findall(text)
    has_format = len(planilla_matches) > 0
    detail = (
        f"Planilla: {planilla_matches[0]} — formato con guiones correcto."
        if has_format
        else "Campo de planilla encontrado pero sin formato correcto con guiones (ej. 1-23456789). Afecta F1-b."
    )
    return ValidationResult(
        code="VL-10",
        label="Número de planilla",
        passed=has_format,
        severity="error",
        detail=detail,
    )


def val_direccion_longitud(text: str) -> ValidationResult:
    """VL-11: Dirección no debe ser excesivamente larga (puede truncar contratos de cuenta ahorro)."""
    match = _DIRECCION_RE.search(text)
    if not match:
        return ValidationResult(
            code="VL-11",
            label="Longitud de dirección",
            passed=True,
            severity="warning",
            detail="No se detectó campo de dirección en el texto extraído.",
        )
    value = match.group(1).strip()
    passed = len(value) <= _ADDRESS_MAX_CHARS
    detail = (
        f"Dirección dentro del límite ({len(value)} caracteres). ✅"
        if passed
        else (
            f"Dirección extensa: {len(value)} caracteres (límite recomendado: {_ADDRESS_MAX_CHARS}). "
            "Puede generar truncamiento en los contratos de cuenta ahorro. Revisar con el oficial."
        )
    )
    return ValidationResult(
        code="VL-11",
        label="Longitud de dirección",
        passed=passed,
        severity="warning",
        detail=detail,
    )


def val_info_conyugue(text: str) -> ValidationResult:
    """VL-12: Si estado civil es casado/unido, la info del cónyuge es obligatoria."""
    is_casado = bool(_CASADO_RE.search(text))
    estado_match = _ESTADO_CIVIL_RE.search(text)
    if not is_casado:
        estado_str = estado_match.group(1) if estado_match else "no detectado"
        return ValidationResult(
            code="VL-12",
            label="Información de cónyuge",
            passed=True,
            severity="warning",
            detail=f"Estado civil: {estado_str}. No aplica obligatoriedad de cónyuge.",
        )
    has_conyugue = bool(_CONYUGUE_RE.search(text))
    detail = (
        "Estado civil casado/unido y datos de cónyuge presentes. ✅"
        if has_conyugue
        else (
            "Estado civil casado/unido pero NO se detectaron datos de cónyuge. "
            "Campo obligatorio — debe completarse antes de procesar el préstamo."
        )
    )
    return ValidationResult(
        code="VL-12",
        label="Información de cónyuge",
        passed=has_conyugue,
        severity="error",
        detail=detail,
    )


def val_proximidad_huella_firma(
    boxes_firma: list[list[float]],
    boxes_huella: list[list[float]],
) -> ValidationResult:
    """VL-13: Firma y huella no deben estar alejadas entre sí en la página.

    Args:
        boxes_firma:  Lista de bounding boxes [x1, y1, x2, y2] de firmas detectadas por YOLO.
        boxes_huella: Lista de bounding boxes [x1, y1, x2, y2] de huellas detectadas por YOLO.
    """
    if not boxes_firma or not boxes_huella:
        return ValidationResult(
            code="VL-13",
            label="Proximidad huella-firma",
            passed=True,
            severity="warning",
            detail=(
                "Sin suficientes detecciones para evaluar proximidad "
                "(se necesita al menos una firma y una huella)."
            ),
        )

    def _center(box: list[float]) -> tuple[float, float]:
        x1, y1, x2, y2 = box
        return (x1 + x2) / 2, (y1 + y2) / 2

    min_dist = float("inf")
    for bf in boxes_firma:
        cx_f, cy_f = _center(bf)
        for bh in boxes_huella:
            cx_h, cy_h = _center(bh)
            dist = math.sqrt((cx_f - cx_h) ** 2 + (cy_f - cy_h) ** 2)
            if dist < min_dist:
                min_dist = dist

    passed = min_dist <= _PROXIMITY_MAX_PX
    detail = (
        f"Distancia mínima huella-firma: {min_dist:.0f}px — dentro del rango aceptable. ✅"
        if passed
        else (
            f"Distancia huella-firma: {min_dist:.0f}px (límite: {_PROXIMITY_MAX_PX}px). "
            "La huella está alejada de la firma. Revisar posición en el documento."
        )
    )
    return ValidationResult(
        code="VL-13",
        label="Proximidad huella-firma",
        passed=passed,
        severity="warning",
        detail=detail,
    )


# ─── Punto de entrada principal ───────────────────────────────────────────────

def run_all_validations(
    text: str,
    firma_detected: bool,
    boxes_firma: list[list[float]],
    boxes_huella: list[list[float]],
) -> list[ValidationResult]:
    """Ejecuta las 13 validaciones en orden y retorna la lista de resultados."""
    return [
        val_cedula_format(text),
        val_motivo_prestamo(text),
        val_numero_seguro_social(text),
        val_referencias(text),
        val_cargo_posicion(text),
        val_rango_salarial(text),
        val_lugar_nacimiento(text),
        val_efectividades(text),
        val_firma_cotizacion(text, firma_detected),
        val_numero_planilla(text),
        val_direccion_longitud(text),
        val_info_conyugue(text),
        val_proximidad_huella_firma(boxes_firma, boxes_huella),
    ]
