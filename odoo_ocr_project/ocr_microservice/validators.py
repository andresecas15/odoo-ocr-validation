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

# VL-03 – Número de Seguro Social (CSS Panamá) – solo para VL-03
# Acepta las variantes observadas en documentos:
#   'No. Seguro Social', 'Nro. Seguro Social', 'Número Seguro Social',
#   'SEG.SOCIAL', 'SEG SOCIAL', 'Seguro Social'
_NSS_LABEL_RE = re.compile(
    r'(?i)'
    r'(?:'
    r'(?:no|nro|n[uú]m)\.?\s*seguro\s+social'  # No./Nro./Núm. Seguro Social
    r'|seg\.?\s*social'                             # SEG.SOCIAL / SEG SOCIAL
    r'|seguro\s+social'                             # Seguro Social (genérico)
    r')'
)
_NSS_VALUE_RE = re.compile(
    r'\b([1-9A-Z][0-9A-Z]{0,3}(?:-[0-9A-Z]{2,6}){1,3})\b'
)

# VL-04 – Referencias
_REF_BANCARIA_RE = re.compile(r'(?i)referencias?\s*bancarias?')
_REF_PERSONAL_RE = re.compile(r'(?i)referencias?\s*personales?')

# VL-05 – Cargo / Posición
# La tabla tiene:
#   Headers: TIPO DE CLIENTE | CARGO O POSICIÓN | SALARIO | TELÉFONO
#   Valores: Gobierno         | Otros            | 1200.01 | NO TIENE
#
# NUEVA ESTRATEGIA: buscar directamente el patrón
#   TIPO_CLIENTE_KEYWORD <spaces> CARGO_WORDS <spaces> NÚMERO_DE_SALARIO
# Sin depender de saltos de línea (que PaddleOCR no garantiza en tablas).
#
# Los valores conocidos de TIPO DE CLIENTE son los anchos de tabla que
# aparecen SIEMPRE antes del cargo. Los usamos como punto de partida.
_CARGO_LABEL_RE = re.compile(
    r'(?i)\b(?:cargo|posici[oó]n|ocupaci[oó]n|profesi[oó]n)\b'
)
# Estrategia principal: TIPO_CLIENTE seguido de CARGO (palabras antes del salario)
_CARGO_FROM_TIPO_CLIENTE_RE = re.compile(
    r'(?i)\b(?:Gobierno|Privado|Independiente|Jubilado|Pensionado|P[uú]blico|Mixto)'
    r'\s+'
    r'([A-Za-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1\u00c1\u00c9\u00cd\u00d3\u00da\u00dc\u00d1][A-Za-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1\u00c1\u00c9\u00cd\u00d3\u00da\u00dc\u00d1\s]{1,50}?)'
    r'\s+\d{3,}'  # el cargo termina cuando empieza el salario (número 3+ dígitos)
)
# Estrategia respaldo: línea siguiente al label (\b para evitar substring match en OPOSICION)
_CARGO_NEXT_LINE_RE = re.compile(
    r'(?i)\bcargo\s+o\s+posici[oó]n\b[^\n]*\n([^\n]{3,100})'
)
_CARGO_HEADER_KW_RE = re.compile(
    r'(?i)\b(?:salario|tel[eé]fono|telefono|ingresos|planilla|tipo\sde|cargo\so)\b'
)

# VL-06 – Rango salarial
# Buscar el monto ÚNICAMENTE en el contexto de la línea/frase del label SALARIO
# para evitar capturar números de cédula u otros campos.
_SALARIO_KEYWORD_RE = re.compile(r'(?i)\b(?:salario|sueldo|ingreso)\b')
# Rango con o sin B/.: captura "1200.01 - 1500.00" o "B/. 850" o "1200.01"
_SALARY_RANGE_RE = re.compile(
    r'(?:B/\.?\s*)?'             # prefijo opcional
    r'(\d{3,}(?:[.,]\d{1,2})?)'  # primer monto
    r'(?:\s*[-–]\s*(?:B/\.?\s*)?(\d{3,}(?:[.,]\d{1,2})?))?'  # rango opcional
)
# Ventana de chars para buscar el monto después del keyword SALARIO
_SALARY_CONTEXT_WINDOW = 80

# VL-07 – Lugar de nacimiento (Provincia + País)
# NUEVA ESTRATEGIA: la provincia panameña es la fuente de verdad.
# 1) Buscar la provincia directamente en el texto (sin depender de layout de tabla)
# 2) Si la encuentra, el lugar de nacimiento está presente → passed=True
# 3) La siguiente línea al label es solo un PLUS si contiene texto válido.
_NACIMIENTO_LABEL_RE = re.compile(r'(?i)\blugar\s+de\s+nacimiento\b')
_NACIMIENTO_NEXT_LINE_RE = re.compile(
    r'(?i)\blugar\s+de\s+nacimiento\b[^\n]*\n([^\n]{3,80})'
)
_PROVINCIA_RE = re.compile(
    r'(?i)\b(?:Panam[aá]\s+Oeste|Panam[aá]\s+Este|Panam[aá]\s+Centro|'
    r'Chiriqu[ií]|Chiqu[ií]|Cocl[eé]|Col[oó]n|Dari[eé]n|Herrera|'
    r'Los\s+Santos|Veraguas|Bocas\s+del\s+Toro|Guna\s+Yala|'
    r'Ember[aá]|Ng[aä]be|Ngobe|Wargand[ií])\b'
)
_PAIS_RE = re.compile(
    r'(?i)\b(?:Panam[aá]|Venezuela|Colombia|Costa\s*Rica|M[eé]xico|Ecuador|Per[uú]|'
    r'Rep[uú]blica\s+Dominicana|Cuba|El\s+Salvador|Honduras|Nicaragua|Guatemala|Bolivia)\b'
)
_NACIMIENTO_HEADER_RE = re.compile(
    r'(?i)\b(?:fecha|cedula|c[eé]dula|civil|vto\.?|estado|nacimiento|nacionalidad)\b'
)

# VL-08 – Efectividades
_EFECTIVIDAD_RE = re.compile(r'(?i)\befectividad\b')

# VL-09 – Cotización (para check de firma en cotización)
_COTIZACION_RE = re.compile(r'(?i)\bcotizaci[oó]n\b')

# VL-10 – Número de planilla
# Dos formatos observados en los documentos CIES de Panamá:
#   1) P0800013010   → P + dígitos (número de planilla/nómina)
#   2) N.PR08-3722   → N.PR + código (nómina planilla con prefijo)
# También se acepta el formato 08-3722 (solo números con guín)
_PLANILLA_FIELD_RE = re.compile(r'(?i)\b(?:planilla|n[oó]mina)\b')
_PLANILLA_P_NUM_RE = re.compile(r'\bP(\d{7,12})\b')          # P0800013010
_PLANILLA_NPR_RE = re.compile(r'N\.?PR(\d{2,4}[-]\d{2,6})')  # N.PR08-3722
_PLANILLA_SIMPLE_RE = re.compile(r'\b(\d{2,4}-\d{3,6})\b')   # 08-3722

# VL-11 – Dirección (longitud)
# Captura máximo 200 chars para evitar leer el documento completo
# cuando PaddleOCR no inserta saltos de línea
_DIRECCION_RE = re.compile(
    r'(?i)(?:direcci[oó]n|domicilio)\s*[:\-]?\s*([^\n\r]{5,200})'
)
_ADDRESS_MAX_CHARS = 120

# VL-12 – Estado civil y cónyuge
# IMPORTANTE: NO buscar 'casado' en todo el texto (aparece en otras secciones del PDF).
# Buscar el LABEL 'estado civil' y evaluar el valor en una ventana de 80 chars.
# OJO: el OCR a veces trunca 'CIVIL' como 'CIVI' → la L es opcional.
_ESTADO_CIVIL_LABEL_RE = re.compile(r'(?i)\bestado\s+civil?\b')
_ESTADO_CIVIL_VALUE_RE = re.compile(r'(?i)(casad[oa]|unid[oa]|solter[oa]|divorciado?|viud[oa]|separad[oa])')
_CONYUGUE_RE = re.compile(
    r'(?i)\bc[oó]nyuge\b|esposo|esposa'
)
# Anchor: 'nombre empresa' es el campo EXCLUSIVO de la sección cónyuge.
# No usar 'información del cónyuge' (OCR puede no escribirlo igual).
_CONYUGUE_SECTION_RE = re.compile(
    r'(?i)nombre\s+empresa'
)
_CONYUGUE_NOMBRE_RE = re.compile(
    # OCR funde headers+valores: 'ELABORA? [cédula] [NOMBRE COMPLETO] [teléfono]'
    # Captura NOMBRE (mayúsculas) entre la cédula del cónyuge y su teléfono.
    r'\b(?:[1-9N][0-9A-Z\-]{3,11})\s+'
    r'([A-ZÁÉÍÓÚÑ]{2,}(?:\s+[A-ZÁÉÍÓÚÑ]{2,}){1,4})'
    r'\s+\d{7,9}\b'
)
# Labora: acepta 'ELABORA' (OCR añade E inicial) y 'LABORA'.
# Estructura observada: ELABORA? [cedula] [nombre] [telefono] SI NO X
_LABORA_BLOCK_RE = re.compile(
    r'(?i)(?:e?labora)[?!]?.{0,300}?\b(si)\b\s+no\s+x'
    r'|(?:e?labora)[?!]?.{0,300}?x\s+(si)\b\s+no'
    r'|(?:e?labora)[?!]?.{0,300}?\b(si)\s+x\b',
    re.DOTALL
)
_NOMBRE_EMPRESA_CONYUGUE_RE = re.compile(
    # Captura empresa solo si hay palabras reales después de 'NOMBRE EMPRESA'
    # y antes del siguiente encabezado de sección.
    r'(?i)nombre\s+empresa\s+'
    r'(?!referencias?|datos|solicitud|direcci[oó]n|tel[eé]fono|parentesco)'
    r'([A-ZÁÉÍÓÚÑ]{2,}(?:\s+[A-ZÁÉÍÓÚÑ]{2,}){0,4})'
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
    """VL-03: Número de Seguro Social (NSS) presente y con formato correcto.

    Itera TODOS los matches del label 'Seguro Social' y elige el primero
    cuya ventana de 80 chars contenga un valor numérico con guiones.
    Esto evita confundir la mención en checklists de documentos ('Recibo
    Seguro Social') con el campo real del formulario.
    """
    nss = None
    for label_m in _NSS_LABEL_RE.finditer(text):
        ventana = text[label_m.end(): label_m.end() + 80]
        val_m = _NSS_VALUE_RE.search(ventana)
        if val_m:
            nss = val_m.group(1)
            break  # primer match con valor es el campo real

    passed = nss is not None
    detail = (
        f"NSS detectado: {nss}"
        if passed
        else "No se encontró un NSS con formato válido. Verificar contra carta de trabajo."
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
    """VL-05: Posición/cargo presente y con contenido.

    Estrategia principal: buscar el patrón
      TIPO_CLIENTE (Gobierno|Privado...) + CARGO_WORDS + NÚMERO_SALARIO
    directamente en el texto concatenado de PaddleOCR, sin depender de
    saltos de línea de tabla que son poco fiables.

    Estrategia de respaldo: línea siguiente al label 'CARGO O POSICIÓN'
    usando \\b para no hacer substring match en 'OPOSICION'.
    """
    # ── Estrategia 1 (principal): TIPO_CLIENTE → CARGO → SALARIO ────────────────
    # Se ejecuta ANTES del check de label porque el OCR a veces fusiona
    # 'CARGO O POSICIÓN' como 'CARGO OPOSICION' y el label no se detecta.
    tipo_match = _CARGO_FROM_TIPO_CLIENTE_RE.search(text)
    if tipo_match:
        cargo = tipo_match.group(1).strip()
        if cargo and len(cargo) >= 2 and not _CARGO_HEADER_KW_RE.search(cargo):
            return ValidationResult(
                code="VL-05",
                label="Posición / Cargo",
                passed=True,
                severity="error",
                detail=f"Cargo detectado: «{cargo[:60]}». Verificar coincidencia con carta de trabajo.",
            )

    has_label = bool(_CARGO_LABEL_RE.search(text))
    if not has_label:
        return ValidationResult(
            code="VL-05",
            label="Posición / Cargo",
            passed=False,
            severity="error",
            detail="No se encontró campo de cargo/posición. Afecta hoja de datos y Cocotito pág. 1 y 2.",
        )

    # ── Estrategia 2: siguiente línea al label con \b correcto ─────────────
    next_line_match = _CARGO_NEXT_LINE_RE.search(text)
    if next_line_match:
        raw_value = next_line_match.group(1).strip()
        # Quitar TIPO DE CLIENTE al inicio si lo hay
        tipo_prefix = re.match(
            r'(?i)^(?:Gobierno|Privado|Independiente|Jubilado|Pensionado|P[uú]blico|Mixto)\s+',
            raw_value
        )
        value = raw_value[tipo_prefix.end():].strip() if tipo_prefix else raw_value
        # Cortar antes del primer monto de salario
        m = re.search(r'\s+\d{3,}[\s,.]', value)
        if m:
            value = value[:m.start()].strip()
        if not _CARGO_HEADER_KW_RE.search(value) and len(value) >= 2:
            return ValidationResult(
                code="VL-05",
                label="Posición / Cargo",
                passed=True,
                severity="error",
                detail=f"Cargo detectado: «{value[:60]}». Verificar coincidencia con carta de trabajo.",
            )

    return ValidationResult(
        code="VL-05",
        label="Posición / Cargo",
        passed=False,
        severity="error",
        detail="Campo 'Cargo/Posición' encontrado pero valor no legible. Verificar en hoja de datos y Cocotito pág. 1 y 2.",
    )


def val_rango_salarial(text: str) -> ValidationResult:
    """VL-06: Rango salarial presente con valor numérico >= 100.

    Busca el monto únicamente en la ventana de texto que rodea la
    palabra clave SALARIO, evitando capturar números de cédula u otros.
    """
    has_keyword = bool(_SALARIO_KEYWORD_RE.search(text))
    if not has_keyword:
        return ValidationResult(
            code="VL-06",
            label="Rango salarial",
            passed=False,
            severity="error",
            detail="No se encontró campo de salario/sueldo. Verificar coincidencia con carta de trabajo.",
        )

    # Buscar el monto en la ventana posterior al keyword SALARIO
    salary_kw = _SALARIO_KEYWORD_RE.search(text)
    window_start = salary_kw.start()
    window_end = min(len(text), window_start + _SALARY_CONTEXT_WINDOW * 5)  # ~5 filas
    window = text[window_start:window_end]

    range_match = _SALARY_RANGE_RE.search(window)
    if range_match and range_match.group(1):
        monto1 = range_match.group(1)
        monto2 = range_match.group(2)
        # Filtrar: el monto debe ser >= 100 (evitar números de 3 dígitos de cédula como 762)
        try:
            val1 = float(monto1.replace(',', ''))
            val2 = float(monto2.replace(',', '')) if monto2 else val1
            if val1 >= 100:
                rango_str = f"{monto1} - {monto2}" if monto2 else monto1
                return ValidationResult(
                    code="VL-06",
                    label="Rango salarial",
                    passed=True,
                    severity="error",
                    detail=f"Monto detectado: {rango_str}. Verificar que coincida con la carta de trabajo.",
                )
        except (ValueError, AttributeError):
            pass

    return ValidationResult(
        code="VL-06",
        label="Rango salarial",
        passed=False,
        severity="error",
        detail="Campo 'Salario' encontrado pero sin monto numérico válido (>= 100). Verificar con carta de trabajo.",
    )


def val_lugar_nacimiento(text: str) -> ValidationResult:
    """VL-07: Lugar de nacimiento debe contener una Provincia panameña.

    NUEVA ESTRATEGIA:
    - Estrategia 1 (principal): buscar una Provincia panameña directamente
      en el texto completo. Si la encuentra, el lugar está presente.
    - Estrategia 2 (complemento): línea siguiente al label '\\blugar de nacimiento'
      (con \\b para evitar substring match). Si el campo no contiene keywords
      de encabezado, se muestra como detalle adicional.
    """
    has_label = bool(_NACIMIENTO_LABEL_RE.search(text))

    # ── Estrategia 1 (principal): buscar provincia panameña en todo el doc ──
    provincia_match = _PROVINCIA_RE.search(text)
    provincia_str = provincia_match.group(0).strip() if provincia_match else None

    # Verificar si cerca de la provincia aparece el país
    pais_near = False
    if provincia_match:
        start = max(0, provincia_match.start() - 20)
        end = min(len(text), provincia_match.end() + 60)
        pais_near = bool(_PAIS_RE.search(text[start:end]))

    # ── Estrategia 2 (complemento): capturar la línea siguiente al label ──
    next_line_value = None
    next_line_match = _NACIMIENTO_NEXT_LINE_RE.search(text)
    if next_line_match:
        candidate = next_line_match.group(1).strip()
        if not _NACIMIENTO_HEADER_RE.search(candidate) and len(candidate) >= 3:
            next_line_value = candidate

    if not has_label:
        return ValidationResult(
            code="VL-07",
            label="Lugar de nacimiento (Provincia + País)",
            passed=False,
            severity="error",
            detail="Campo 'Lugar de nacimiento' ausente. Afecta hoja de datos y Cocotito pág. 1.",
        )

    # Una provincia panameña en el documento es suficiente para PASSED
    if provincia_str:
        if next_line_value and not _NACIMIENTO_HEADER_RE.search(next_line_value):
            lugar = next_line_value[:60]
        else:
            lugar = f"{provincia_str}, Panamá" if pais_near else provincia_str
        return ValidationResult(
            code="VL-07",
            label="Lugar de nacimiento (Provincia + País)",
            passed=True,
            severity="error",
            detail=f"Lugar de nacimiento detectado: «{lugar}»",
        )

    # Si hay valor de siguiente línea aunque sea sin provincia
    if next_line_value:
        return ValidationResult(
            code="VL-07",
            label="Lugar de nacimiento (Provincia + País)",
            passed=False,
            severity="error",
            detail=(
                f"Valor «{next_line_value[:60]}» no incluye provincia panameña. "
                "Debe indicar Provincia + País (ej. 'Chiriquí, Panamá'). "
                "Afecta hoja de datos y Cocotito pág. 1."
            ),
        )

    return ValidationResult(
        code="VL-07",
        label="Lugar de nacimiento (Provincia + País)",
        passed=False,
        severity="error",
        detail=(
            "Campo encontrado pero valor ilegible o en blanco. "
            "Debe indicar Provincia + País (ej. 'Chiriquí, Panamá'). "
            "Afecta hoja de datos y Cocotito pág. 1."
        ),
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


def val_firma_cotizacion(text: str, firma_detected: bool, pages_firma: list[int] | None = None) -> ValidationResult:
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
    if passed and pages_firma:
        paginas_str = ", ".join(str(p) for p in sorted(pages_firma))
        total = len(pages_firma)
        detail = (
            f"Firmas detectadas en {total} página(s): [{paginas_str}]. "
            "Verificar que firma de oficial esté presente en la cotización. ✅"
        )
    elif passed:
        detail = "Firma detectada en documento con cotización. ✅"
    else:
        detail = "Cotización presente pero sin firma de oficial detectada por el modelo de visión. Revisar manualmente."
    return ValidationResult(
        code="VL-09",
        label="Firma en cotización",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_numero_planilla(text: str) -> ValidationResult:
    """VL-10: Número de planilla / nómina presente (afecta F1-b en generales).

    Busca los dos formatos reales de planilla panameña:
      - P0800013010  (P + dígitos)
      - N.PR08-3722  (prefijo N.PR + código)
    Muestra AMBOS candidatos si los encuentra y pide verificación.
    """
    has_field = bool(_PLANILLA_FIELD_RE.search(text))

    # Buscar los distintos formatos
    p_match = _PLANILLA_P_NUM_RE.search(text)
    npr_match = _PLANILLA_NPR_RE.search(text)
    simple_match = _PLANILLA_SIMPLE_RE.search(text)

    candidatos = []
    if p_match:
        candidatos.append(f"P{p_match.group(1)}")
    if npr_match:
        candidatos.append(f"N.PR{npr_match.group(1)}")
    if simple_match and not p_match and not npr_match:
        # Solo usar formato simple si no hay otro más específico
        candidatos.append(simple_match.group(1))

    has_candidates = len(candidatos) > 0

    if not has_field and not has_candidates:
        return ValidationResult(
            code="VL-10",
            label="Número de planilla",
            passed=False,
            severity="error",
            detail="No se encontró campo de planilla/nómina. Afecta F1-b en las generales.",
        )

    if has_candidates:
        if len(candidatos) == 1:
            detail = (
                f"Planilla detectada: {candidatos[0]}. "
                "Verificar que coincida con el número de planilla en carta de trabajo."
            )
        else:
            detail = (
                f"Dos candidatos detectados: {' y '.join(candidatos)}. "
                "Comparar con la carta de trabajo para confirmar cuál es el número de planilla correcto."
            )
        return ValidationResult(
            code="VL-10",
            label="Número de planilla",
            passed=True,
            severity="error",
            detail=detail,
        )

    # Hay el campo pero sin número identificable
    return ValidationResult(
        code="VL-10",
        label="Número de planilla",
        passed=False,
        severity="error",
        detail="Campo de planilla encontrado pero sin número reconocible (formatos: P0800013010 o N.PR08-3722).",
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
    """VL-12: Si estado civil es casado/unido, la info del cónyuge es obligatoria.

    Extrae: nombre del cónyuge, si labora y nombre de la empresa.
    Usa una ventana de texto a partir del header 'INFORMACIÓN DEL CÓNYUGE'
    para no depender de saltos de línea (PaddleOCR produce texto corrido).

    IMPORTANTE: El estado civil se busca SOLO en la ventana del label 'ESTADO CIVIL'
    para evitar detectar 'casado' en otras secciones del PDF (PEP, etc.).
    """
    # Buscar el label 'ESTADO CIVIL' y evaluar el valor en los 50 chars siguientes
    label_m = _ESTADO_CIVIL_LABEL_RE.search(text)
    estado_str = "no detectado"
    is_casado = False
    if label_m:
        ventana_ec = text[label_m.end(): label_m.end() + 80]
        print(f"[VL-12 DBG] label en pos={label_m.start()}, ventana={repr(ventana_ec)}", flush=True)
        val_ec = _ESTADO_CIVIL_VALUE_RE.search(ventana_ec)
        if val_ec:
            estado_str = val_ec.group(1).strip().title()
            is_casado = bool(re.search(r'(?i)\b(?:casad[oa]|unid[oa])\b', estado_str))
    else:
        idx = text.lower().find('estado')
        print(f"[VL-12 DBG] 'estado civil' NO encontrado. 'estado' en pos={idx}: {repr(text[max(0,idx-5):idx+30]) if idx>=0 else 'N/A'}", flush=True)

    if not is_casado:
        es_soltero = bool(re.search(r'(?i)\bsolter[ao]\b', estado_str))
        detail = (
            f"N/A ({estado_str}). Sección de cónyuge no aplica para persona soltera."
            if es_soltero
            else f"Estado civil: {estado_str}. No aplica obligatoriedad de cónyuge."
        )
        return ValidationResult(
            code="VL-12",
            label="Información de cónyuge",
            passed=True,
            severity="warning",
            detail=detail,
        )

    has_conyugue = bool(_CONYUGUE_RE.search(text))
    if not has_conyugue:
        return ValidationResult(
            code="VL-12",
            label="Información de cónyuge",
            passed=False,
            severity="error",
            detail=(
                "Estado civil casado/unido pero NO se detectó sección de cónyuge. "
                "Campo obligatorio — debe completarse antes de procesar el préstamo."
            ),
        )

    # La sección cónyuge se detecta por 'NOMBRE EMPRESA' (campo exclusivo de esa sección).
    # 'cónyuge' solo como verificacion de has_conyugue; la ventana la anclamos en NOMBRE EMPRESA.
    all_empresa = list(_CONYUGUE_SECTION_RE.finditer(text))  # 'nombre empresa'
    empresa_anchor = all_empresa[-1] if all_empresa else None  # ÚLTIMA = sección cónyuge
    if not all_empresa:
        # Si no hay NOMBRE EMPRESA, la sección no existe en el texto OCR
        return ValidationResult(
            code="VL-12",
            label="Información de cónyuge",
            passed=False,
            severity="error",
            detail=(
                "Estado civil casado/unido pero NO se detectó sección de cónyuge. "
                "Campo obligatorio — debe completarse antes de procesar el préstamo."
            ),
        )

    win_start = max(0, empresa_anchor.start() - 400)
    win_end = min(len(text), empresa_anchor.end() + 200)
    ventana = text[win_start:win_end]

    print(f"[VL-12 CON DBG] ventana='{ventana[:200]}'", flush=True)

    partes = []

    # Nombre del cónyuge
    nombre_m = _CONYUGUE_NOMBRE_RE.search(ventana)
    labora_m = _LABORA_BLOCK_RE.search(ventana)
    empresa_m = _NOMBRE_EMPRESA_CONYUGUE_RE.search(ventana)
    print(f"[VL-12 CON DBG] nombre_m={nombre_m}, labora_m={labora_m}, empresa_m={empresa_m}", flush=True)

    if nombre_m:
        nombre = nombre_m.group(1).strip()
        partes.append(f"Cónyuge: {nombre[:40]}")

    # Si labora
    if labora_m:
        partes.append(f"Labora: {labora_m.group(1).upper()}")

    # Empresa
    if empresa_m:
        empresa = empresa_m.group(1).strip()
        # Limpiar tokens de sección que el OCR puede fusionar al final (ej: "REFERENCIASPERSONALES")
        empresa = re.sub(r'\s*\b(?:REFERENCIAS\S*|DATOS\S*|SOLICITUD\S*)\b.*$', '', empresa, flags=re.IGNORECASE).strip()
        if len(empresa) >= 3:
            partes.append(f"Empresa: {empresa[:40]}")

    if partes:
        resumen = " | ".join(partes)
        detail = f"{resumen}. Verificar coincidencia con los documentos presentados."
    else:
        detail = "Sección de cónyuge presente. Verificar nombre, teléfono y datos laborales manualmente."

    return ValidationResult(
        code="VL-12",
        label="Información de cónyuge",
        passed=True,
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
    pages_firma: list[int] | None = None,
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
        val_firma_cotizacion(text, firma_detected, pages_firma),
        val_numero_planilla(text),
        val_direccion_longitud(text),
        val_info_conyugue(text),
        val_proximidad_huella_firma(boxes_firma, boxes_huella),
    ]
