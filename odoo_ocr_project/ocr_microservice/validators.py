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
import unicodedata
from rapidfuzz import fuzz, process


@dataclass
class ValidationResult:
    """Resultado de una regla de validación individual."""
    code: str           # Ej. "VL-01"
    label: str          # Nombre legible
    passed: bool        # True  → campo OK | False → requiere atención
    severity: str       # "error" | "warning"
    detail: Optional[str] = None  # Mensaje explicativo para el oficial


def is_dummy_value(val: str) -> bool:
    """Retorna True si el valor extraído es considerado un dummy o nulo."""
    if not val:
        return True
    val_upper = val.upper().strip()
    dummy_words = (
        "NO ENCONTRADO", "NO APLICA", "NO REGISTRA", "SIN INFO", "NINGUNO", 
        "NINGUNA", "NO CONTIENE", "NOT A PICTURED", "NOT PICTURED", 
        "NOT A DOCUMENT", "NO SE MENCIONA", "INFORMACION NO DISPONIBLE"
    )
    if val_upper in ("", "NO", "N.A", "N/A", "?", "-", "NO DISPONIBLE"):
        return True
    for dummy in dummy_words:
        if dummy in val_upper:
            return True
    return False


# ─── Patrones regex ────────────────────────────────────────────────────────────

# VL-01 – Cédula en formato panameño: 8-1234-56789 | PE-12-3456 | N-12-3456 | 2017-450 (Ollama a veces omite 8-)
# Added \d{1,2}-\d{3,4}-\d{3,6} explicitly for 9-207-450 
_CEDULA_RE = re.compile(
    r'\b(?:(?:pe|n|e|pi|av)-\d{1,4}-\d{1,6}|(?:[1-9]|1[0-3])-\d{3,4}-\d{3,6})\b',
    re.IGNORECASE
)

# VL-02 – Motivo de préstamo
# Captura el VALOR justo después del label «motivo [de préstamo]»
# Se limita a 80 chars y exige al menos 3 palabras para evitar capturar
# encabezados de tabla ("OFICIAL DE CRÉDITO Jeyse...").
_MOTIVO_KEYWORD_RE = re.compile(r'(?i)\b(?:motivo|tipo\s+de\s+cr[eé]dito|tipo\s+de\s+pr[eé]stamo|prop[oó]sito)\b')
_MOTIVO_VALUE_RE = re.compile(
    r'(?i)\b(?:'
    r'razon\s+o\s+motivo\s+del?\s+pr[eé]stamo'
    r'|razon\s+o\s+motivo'
    r'|motivo\s+del?\s+pr[eé]stamo'
    r'|motivo'
    r'|prop[oó]sito\s+del?\s+pr[eé]stamo'
    r'|prop[oó]sito'
    r'|tipo\s+de\s+cr[eé]dito'
    r'|tipo\s+de\s+pr[eé]stamo'
    r')\s*[:\-=]?\s*'
    r'([a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00c1\u00c9\u00cd\u00d3\u00dañÑ/() ]{3,80})'
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
    r'(?:no|nro|n[uú]m(?:ero)?)\.?\s*(?:de\s+)?seguro\s+social'  # No./Nro./Núm./Numero de Seguro Social
    r'|seg\.?\s*social'                             # SEG.SOCIAL / SEG SOCIAL
    r'|seguro\s+social'                             # Seguro Social (genérico)
    r')'
)
_NSS_VALUE_RE = re.compile(
    r'\b([1-9A-Z][0-9A-Z]{0,3}(?:-[0-9A-Z]{2,6}){1,3})\b'
)

# VL-04 – Referencias
# Con la nueva plantilla de Ollama, buscará "Referencia Bancaria: [valor]"
_REF_BANCARIA_RE = re.compile(r'(?i)referencias?\s*bancarias?(?:\s*[:\-]?\s*(?!no\s+encontrado))')
_REF_PERSONAL_RE = re.compile(r'(?i)referencias?\s*personales?(?:\s*[:\-]?\s*(?!no\s+encontrado))')
_REF_OLLAMA_MISSING_RE = re.compile(r'(?i)referencia\s+(?:bancaria|personal)\s*[:\-]\s*no\s+encontrado')

# VL-05 – Cargo / Posición
# La tabla tiene:
#   Headers: TIPO DE CLIENTE | CARGO O POSICIÓN | SALARIO | TELÉFONO
#   Valores: Gobierno         | Otros            | 1200.01 | NO TIENE
#
# ESTRATEGIA OLLAMA: Ollama resume la fila como "- TIPO CLIENTE: GOBIERNO - DOCENTE"
# o "- CARGO: DOCENTE" o "TIPO CUENTA: EDUCADOR".
_CARGO_OLLAMA_RE = re.compile(
    # Captura valor hasta 60 chars en la misma línea, frena si ve otra etiqueta:
    r'(?i)(?:cargo|posici[oó]n|ocupaci[oó]n|profesi[oó]n)[^\S\r\n]*[:\-]+[^\S\r\n]*'
    r'([^\r\n]{3,60}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)'
)

_CARGO_LABEL_RE = re.compile(
    r'(?i)\b(?:cargo|posici[oó]n|ocupaci[oó]n|profesi[oó]n)\b'
)
# Estrategia PaddleOCR legacy: TIPO_CLIENTE seguido de CARGO (palabras antes del salario)
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
_SALARIO_KEYWORD_RE = re.compile(r'(?i)\b(?:rango\s+salarial|salario|sueldo|ingresos?)\b')
_SALARIO_OLLAMA_RE = re.compile(
    r'(?i)rango\s+salarial\s+o\s+salario\s*[:\-]\s*'
    r'(?!no\s+encontrado)(.{2,60}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)'
)
# Busca cualquier monto con formato moneda en el texto (ej. $1,671.41 o 1200.00 o B/. 850)
_SALARY_RANGE_RE = re.compile(
    r'(?:B/\.?\s*|\$\s*)?'             # prefijo opcional B/. o $
    r'(?<![A-Za-z0-9\-])(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d{1,}(?:[.,]\d{1,2})?)'  # número 1
    r'(?:\s*[-–]\s*(?:B/\.?\s*|\$\s*)?(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d{1,}(?:[.,]\d{1,2})?))?'  # rango opcional
    r'(?![A-Za-z0-9])'
)
# Ventana ampliada para coincidir con la lista de Ollama
_SALARY_CONTEXT_WINDOW = 120

# VL-07 – Lugar de nacimiento (Provincia + País)
# NUEVA ESTRATEGIA MÚLTIPLE: 
# 1) Leer directamente el Key-Value de la plantilla de Ollama: 'Lugar de Nacimiento: Veraguas'
# 2) Buscar la provincia directamente en el texto
_NACIMIENTO_OLLAMA_RE = re.compile(
    r'(?i)\blugar\s+de\s+nacimiento\s*[:\-]\s*'
    r'(?!no\s+encontrado)(.{3,60}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)'
)
_NACIMIENTO_LABEL_RE = re.compile(r'(?i)\blugar\s+de\s+nacimiento\b')
_NACIMIENTO_NEXT_LINE_RE = re.compile(
    r'(?i)\blugar\s+de\s+nacimiento\b[^\n]*\n([^\n]{3,80})'
)
_PROVINCIA_RE = re.compile(
    r'(?i)\b(?:Panam[aá]\s+Oeste|Panam[aá]\s+Este|Panam[aá]\s+Centro|Panam[aá]|'
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
# También se acepta el formato 08-3722 (solo números con guión) o SORTE121 de Ollama
_PLANILLA_FIELD_RE = re.compile(r'(?i)\b(?:planilla|n[oó]mina|sorte)\b')
_PLANILLA_P_NUM_RE = re.compile(r'\bP(\d{5,12})\b')          # P0800013010
_PLANILLA_NPR_RE = re.compile(r'N\.?PR(\d{2,4}[-]\d{2,6})')  # N.PR08-3722
_PLANILLA_SIMPLE_RE = re.compile(r'\b(?<=[^A-Z\d-])(\d{2,4}-\d{3,6})\b')   # 08-3722 asegurando que no empiece tras un número o letra para no capturar el medio de la cédula 
_PLANILLA_GOB_RE = re.compile(r'\b(?<=[^A-Z\d-])(\d{1,2}-\d{1,4}-\d{1,4}-\d{1,4}-\d{1,6})\b') # 8-21-06-0-01979 (Colaborador Gobierno)
_PLANILLA_OLLAMA_RE = re.compile(r'(?i)\b(?:sorte|planilla|n[oó]mina)[\s:\-/]*(\d{2,6})') # SORTE/121
_PLANILLA_OLLAMA_FIELD_RE = re.compile(
    r'(?i)Numero de Planilla:\s*(?!no\s+encontrado)(.{2,40}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)'
)

# VL-11 – Dirección (longitud)
# Captura máximo 200 chars para evitar leer el documento completo
# cuando PaddleOCR no inserta saltos de línea
_DIRECCION_RE = re.compile(
    r'(?i)(?:direcci[oó]n(?:\s+residencial)?|datos\s+residenciales|domicilio|residenc(?:ia|ial)|ubicaci[oó]n)\s*[:\-]?\s*([^\n\r]{5,200})'
)
_ADDRESS_MAX_CHARS = 120

# VL-12 – Estado civil y cónyuge
# IMPORTANTE: NO buscar 'casado' en todo el texto (aparece en otras secciones del PDF).
# Buscar el LABEL 'estado civil' y evaluar el valor en una ventana de 80 chars.
# OJO: el OCR a veces trunca 'CIVIL' como 'CIVI' → la L es opcional.
_ESTADO_CIVIL_LABEL_RE = re.compile(r'(?i)\bestado\s+civil?\b')
_ESTADO_CIVIL_OLLAMA_RE = re.compile(r'(?i)Estado Civil:\s*([A-Za-z0-9ÁÉÍÓÚÑáéíóúñ\.\,\-( )]+)')
_ESTADO_CIVIL_VALUE_RE = re.compile(r'(?i)(casad[oa]|unid[oa]|solter[oa]|divorciado?|viud[oa]|separad[oa])')
_CONYUGUE_RE = re.compile(
    r'(?i)\bc[oó]nyug?e\b|esposo|esposa'
)
_CONYUGUE_NOMBRE_OLLAMA_RE = re.compile(
    r'(?i)Conyuge Nombre:\s*(?!no\s+encontrado)(.{2,60}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)'
)
_CONYUGUE_CEDULA_OLLAMA_RE = re.compile(
    r'(?i)Conyuge Cedula:\s*(?!no\s+encontrado)(.{2,60}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)'
)
_CONYUGUE_LABORA_OLLAMA_RE = re.compile(
    r'(?i)Conyuge Labora:\s*(?!no\s+encontrado)(.{2,10}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)'
)
_CONYUGUE_EMPRESA_OLLAMA_RE = re.compile(
    r'(?i)Conyuge Empresa:\s*(?!no\s+encontrado)(.{2,60}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)'
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


# ─── Funciones Auxiliares de NLP ──────────────────────────────────────────────

def normalize_ocr_text(text: str) -> str:
    """
    Limpia el texto proveniente de OCR para mejorar la fiabilidad de las validaciones:
    - Convierte a minúsculas
    - Remueve acentos (ej. 'MÉXICO' -> 'mexico')
    - Conserva el diseño de líneas pero unifica espacios horizontales
    - Reemplaza errores OCR comunes conocidos
    """
    if not text:
        return ""
    
    # 1. A minúsculas y sin acentos
    text = text.lower()
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    
    # 2. Correcciones críticas de OCR
    text = text.replace('0posicion', 'oposicion') \
               .replace('1ngreso', 'ingreso')
    
    # 3. Unificar espacios horizontales y limpiar saltos de línea múltiples
    text = re.sub(r'[^\S\r\n]+', ' ', text)
    text = re.sub(r'\r+', '', text)
    text = re.sub(r'\n+', '\n', text)
    return text.strip()

def fuzzy_find(text: str, keyword: str, threshold: float = 85.0) -> int:
    """
    Busca la palabra o frase 'keyword' dentro del 'text' usando RapidFuzz.
    Tolera errores tipográficos comunes en OCR (ej. 'estado civi', 'n0mbre empresa').
    
    Retorna:
        - El índice de inicio (start index) donde se encontró la mejor coincidencia
        - -1 si no se halló nada superior al `threshold`
    """
    # Usamos extractOne con un token_set_ratio o partial_ratio
    # partial_ratio es útil cuando keyword es mucho más pequeña que text
    match = process.extractOne(
        keyword,
        [text[i:i + len(keyword) + 10] for i in range(0, max(1, len(text) - len(keyword)), 5)],
        scorer=fuzz.partial_ratio
    )
    
    if match and len(match) >= 2 and isinstance(match[1], (int, float)) and match[1] >= threshold:
        # Recuperar índice aproximado original buscando el fragmento
        idx = text.find(str(match[0]))
        return idx
    return -1


def is_part_of_cedula(val: str, cedulas: list[str]) -> bool:
    """Retorna True si el valor parece ser parte de una cédula para evitar falsos positivos."""
    if not val or not cedulas:
        return False
        
    val_clean = re.sub(r'\D', '', val)
    cedulas_clean = [re.sub(r'\D', '', c) for c in cedulas]
    
    # Si es exactamente una cédula
    if val_clean in cedulas_clean:
        return True
        
    # Si es un rango (ej. "733 - 1006" o "933 - 1006")
    if '-' in val or '–' in val:
        # Extraer los números individuales
        nums = re.findall(r'\d+', val)
        for num in nums:
            # Si el número individual es igual al tomo (medio) o asiento (final) de alguna cédula
            for c in cedulas:
                parts = re.split(r'[- ]', c)
                if len(parts) >= 2:
                    tomo = re.sub(r'\D', '', parts[-2])
                    asiento = re.sub(r'\D', '', parts[-1])
                    if num == tomo or num == asiento:
                        return True
    return False


# ─── Funciones de validación ──────────────────────────────────────────────────

def val_cedula_format(text: str) -> ValidationResult:
    """VL-01: Cédula de identidad presente y con formato válido."""
    matches = _CEDULA_RE.findall(text)
    
    # Eliminar duplicados manteniendo el orden
    unique_matches = []
    for m in matches:
        if m not in unique_matches:
            unique_matches.append(m)
            
    passed = len(unique_matches) > 0
    first_cedula = unique_matches[0] if unique_matches else None
    detail = (
        f"Cédula encontrada: {first_cedula}"
        if first_cedula
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
    """VL-02: Hoja de datos contiene motivo de préstamo con contenido.
    Asegura que ignora reportes de 'NO ENCONTRADO' devueltos por páginas
    irrelevantes de Ollama, y usa la primera coincidencia válida.
    """
    keyword = bool(_MOTIVO_KEYWORD_RE.search(text))
    
    val_str = None
    is_false_positive = False

    for match_ in _MOTIVO_VALUE_RE.finditer(text):
        candidate = match_.group(1).strip()
        if candidate.upper() == "NO ENCONTRADO":
            continue
        
        # Verificar falso positivo: el match capturó un encabezado de tabla
        if bool(_MOTIVO_FALSE_POSITIVE_RE.search(candidate)):
            is_false_positive = True
            continue
            
        val_str = candidate
        break

    passed = keyword and (val_str is not None)

    if not keyword:
        detail = "No se encontró etiqueta de motivo (ej. 'PROPÓSITO DEL PRÉSTAMO')."
    elif val_str is None:
        detail = "Campo 'Motivo' encontrado pero el valor parece estar en blanco o no es legible."
    else:
        detail = f"Motivo detectado: «{val_str[:80]}»"
        
    return ValidationResult(
        code="VL-02",
        label="Motivo de préstamo",
        passed=passed,
        severity="error",
        detail=detail,
    )


def val_numero_seguro_social(text: str) -> ValidationResult:
    """VL-03: Número de Seguro Social (NSS) presente y con formato correcto.

    Itera TODOS los matches del label 'Seguro Social' y elige el primero.
    Evita falsos positivos descartando si el valor coincide con alguna cédula.
    """
    nss = None
    cedulas = _CEDULA_RE.findall(text)

    # 1. Intentar con salida estructurada de Ollama
    ollama_nss_re = re.compile(r'(?i)Numero de Seguro Social:\s*(?!no\s+encontrado)(.{2,40}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)')
    for m in ollama_nss_re.finditer(text):
        candidate = m.group(1).strip()
        if not is_dummy_value(candidate) and not is_part_of_cedula(candidate, cedulas):
            nss = candidate
            break

    # 2. Fallback local en texto
    if not nss:
        for label_m in _NSS_LABEL_RE.finditer(text):
            ventana = text[label_m.end(): label_m.end() + 80]
            val_m = _NSS_VALUE_RE.search(ventana)
            if val_m:
                candidate = val_m.group(1)
                if not is_part_of_cedula(candidate, cedulas):
                    nss = candidate
                    break

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
    """VL-04: Referencias bancarias y personales.
    Busca explícitamente la confirmación de Referencia Bancaria y Personal
    usando la estructura de Ollama y el lookahead.
    """
    bancaria_ok = False
    for match in re.finditer(r'(?i)referencias?\s*bancarias?\s*[:\-]\s*(.{1,60}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)', text):
        val = match.group(1).strip().lower()
        exclusions = ("no encontrado", "?", "no se menciona", "no aplica", "informacion no disponible", "no disponible", "si, ")
        if not any(exc in val for exc in exclusions) and "firma" not in val and "manuscrita" not in val:
            bancaria_ok = True
            break
            
    personal_ok = False
    for match in re.finditer(r'(?i)referencias?\s*personales?\s*[:\-]\s*(.{1,60}?)(?=\s+[A-Za-z0-9áéíóúÁÉÍÓÚñÑé\s/]+:|$)', text):
        val = match.group(1).strip().lower()
        exclusions = ("no encontrado", "?", "no se menciona", "no aplica", "informacion no disponible", "no disponible", "si, ")
        if not any(exc in val for exc in exclusions) and "firma" not in val and "manuscrita" not in val:
            personal_ok = True
            break
            
    # Si Ollama no extrajo la referencia estructurada, comprobamos de forma legacy.
    if not bancaria_ok:
        bancaria_ok = bool(re.search(r'(?i)referencias?\s*bancarias?\b', text))
    if not personal_ok:
        personal_ok = bool(re.search(r'(?i)referencias?\s*personales?\b', text))

    faltantes = []
    if not bancaria_ok: faltantes.append("referencia bancaria")
    if not personal_ok: faltantes.append("referencia personal")

    if faltantes:
        return ValidationResult(
            code="VL-04",
            label="Referencias bancarias y personales",
            passed=False,
            severity="error",
            detail=f"Faltan: {', '.join(faltantes)}. Afecta hoja de datos y solicitud de cuenta ahorro Cocotito pág. 2.",
        )
    return ValidationResult(
        code="VL-04",
        label="Referencias bancarias y personales",
        passed=True,
        severity="error",
        detail="Ambas referencias presentes (bancaria y personal).",
    )


def val_cargo_posicion(text: str) -> ValidationResult:
    """VL-05: Posición/cargo presente y con contenido.
    Estrategia principal: Buscar la respuesta de Ollama en el template.
    Si dice NO ENCONTRADO en todas las páginas, fallback al texto en bruto.
    """
    # ── Estrategia 0 (Ollama Markdown format) ──
    for match_ in _CARGO_OLLAMA_RE.finditer(text):
        cargo_ollama = match_.group(1).strip()
        # Clean prefix and check if it's just client type
        cargo_ollama = re.sub(r'^(?:gobierno|privado|independiente|jubilado|pensionado|publico|mixto)\s*[\-:]\s*', '', cargo_ollama, flags=re.IGNORECASE)
        if not is_dummy_value(cargo_ollama) and cargo_ollama.upper() not in ("GOBIERNO", "PRIVADO", "INDEPENDIENTE", "JUBILADO", "PENSIONADO", "PUBLICO", "MIXTO", "PERMANENTE", "TEMPORAL", "NO APLICA", "NINGUNO", "NINGUNA", "N/A", "NA", "CARGO", "POSICION", "PROFESION", "OCUPACION", "OFICIO"):
            # Filtro básico anti-numérico (no solo números)
            if len(cargo_ollama) >= 3 and not re.fullmatch(r'\d+', cargo_ollama.replace(' ', '')):
                return ValidationResult(
                    code="VL-05",
                    label="Posición / Cargo",
                    passed=True,
                    severity="error",
                    detail=f"Cargo detectado por LLM: «{cargo_ollama[:60].title()}». Verificar contra carta de trabajo.",
                )

    # ── Estrategia 1 (Fallback en texto bruto) ──
    has_label = bool(_CARGO_LABEL_RE.search(text))
    if not has_label:
        return ValidationResult(
            code="VL-05",
            label="Posición / Cargo",
            passed=False,
            severity="error",
            detail="No se encontró campo de cargo/posición. Afecta hoja de datos y Cocotito pág. 1 y 2.",
        )

    # Como la etiqueta existe en alguna parte del texto adicional, probar buscarla
    next_line_match = _CARGO_NEXT_LINE_RE.search(text)
    if next_line_match:
        # Usamos una expresión más específica para el raw text sin cruzar líneas
        for raw_match in re.finditer(r'(?i)(?:cargo|posici[oó]n|profesi[oó]n|oficio|ocupaci[oó]n)(?:[^\S\r\n]*[:\-]+[^\S\r\n]*|[^\S\r\n]+(?:de|del)[^\S\r\n]+)([a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00c1\u00c9\u00cd\u00d3\u00dañÑ/() ]{3,40})', text):
            candidate = raw_match.group(1).strip()
            norm_cand = candidate.upper().replace("Á", "A").replace("É", "E").replace("Í", "I").replace("Ó", "O").replace("Ú", "U").strip()
            if norm_cand not in ["O POSICION", "NO ENCONTRADO", "POSICION", "CARGO", "CARGO O POSICION", "PERMANENTE", "TEMPORAL", "NO APLICA", "NINGUNO", "NINGUNA", "N/A", "NA", "DIRECCION", "TELEFONO", "EMAIL", "CORREO", "CELULAR", "SALARIO", "SUELDO"] and len(candidate) >= 3:
                return ValidationResult(
                    code="VL-05",
                    label="Posición / Cargo",
                    passed=True,
                    severity="error",
                    detail=f"Cargo detectado en texto: «{candidate[:60].title()}». Verificar coincidencia con carta de trabajo.",
                )

    return ValidationResult(
        code="VL-05",
        label="Posición / Cargo",
        passed=False,
        severity="error",
        detail="Campo encontrado en documento, pero el valor parece estar en blanco o es ilegible.",
    )


def val_rango_salarial(text: str) -> ValidationResult:
    """VL-06: Rango salarial presente con valor numérico >= 100.

    Estrategia DOS PASADAS:
    1) Buscar un RANGO explícito (X - Y) primero en la plantilla de Ollama y
       luego en cualquier ventana del texto.  Un rango siempre es preferido a
       un monto suelto porque es la forma correcta que debe aparecer en el doc.
    2) Solo si no hay ningún rango, aceptar un monto simple >= 100.
    Evita falsos positivos filtrando números que formen parte de las cédulas detectadas.
    """

    # ── Regex para detectar específicamente un rango (dos números con guión) ──
    _RANGE_EXPLICIT_RE = re.compile(
        r'(?:B/\.?\s*|\$\s*)?'
        r'(?<![A-Za-z0-9\-])(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d{1,}(?:[.,]\d{1,2})?)'
        r'\s*[-–]\s*'
        r'(?:B/\.?\s*|\$\s*)?(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d{1,}(?:[.,]\d{1,2})?)'
        r'(?![A-Za-z0-9])'
    )

    def _to_float(s: str) -> float:
        s = s.replace('$', '').replace('B/.', '').replace('B/', '').strip()
        if not s:
            return 0.0
        # Determinar si la coma o el punto actúa como decimal
        if ',' in s and '.' in s:
            if s.find(',') > s.find('.'):
                # Formato: 1.200,00 -> eliminar punto, cambiar coma por punto
                s = s.replace('.', '').replace(',', '.')
            else:
                # Formato: 1,200.00 -> eliminar coma
                s = s.replace(',', '')
        elif ',' in s:
            # Solo comas: si hay una sola y tiene 1 o 2 decimales, es decimal
            parts = s.split(',')
            if len(parts) == 2 and len(parts[1]) in (1, 2):
                s = s.replace(',', '.')
            else:
                s = s.replace(',', '')
        elif '.' in s:
            # Solo puntos: si tiene 3 dígitos tras el punto (ej. 1.200), suele ser separador de miles
            parts = s.split('.')
            if len(parts) == 2 and len(parts[1]) == 3:
                s = s.replace('.', '')
        try:
            return float(s)
        except ValueError:
            return 0.0

    cedulas = _CEDULA_RE.findall(text)

    # ── Recopilar candidatos de Ollama ──
    ollama_ranges: list[tuple[str, str]] = []
    ollama_singles: list[str] = []
    for ollama_m in _SALARIO_OLLAMA_RE.finditer(text):
        val_str = ollama_m.group(1).strip()
        if val_str.upper() in ("NO ENCONTRADO", "?", ""):
            continue
        # ¿Tiene rango explícito?
        for rm in _RANGE_EXPLICIT_RE.finditer(val_str):
            try:
                candidate = rm.group(0).strip()
                if is_part_of_cedula(candidate, cedulas):
                    continue
                v1 = _to_float(rm.group(1))
                v2 = _to_float(rm.group(2))
                if v1 >= 100.0 and v2 >= 100.0:
                    ollama_ranges.append((rm.group(1), rm.group(2)))
            except (ValueError, AttributeError):
                pass
        if not ollama_ranges:
            # Guardar montos simples como fallback de Ollama
            for sm in _SALARY_RANGE_RE.finditer(val_str):
                if sm.group(1) and not sm.group(2):
                    try:
                        candidate = sm.group(1).strip()
                        if is_part_of_cedula(candidate, cedulas):
                            continue
                        if _to_float(sm.group(1)) >= 100.0:
                            ollama_singles.append(sm.group(1))
                    except (ValueError, AttributeError):
                        pass

    # ── Buscar rangos en TODO el texto (incluye páginas que Ollama no capturó) ──
    text_ranges: list[tuple[str, str]] = []
    for kw_m in _SALARIO_KEYWORD_RE.finditer(text):
        window = text[kw_m.start(): min(len(text), kw_m.start() + _SALARY_CONTEXT_WINDOW * 5)]
        for rm in _RANGE_EXPLICIT_RE.finditer(window):
            try:
                candidate = rm.group(0).strip()
                if is_part_of_cedula(candidate, cedulas):
                    continue
                v1 = _to_float(rm.group(1))
                v2 = _to_float(rm.group(2))
                if v1 >= 100.0 and v2 >= 100.0:
                    text_ranges.append((rm.group(1), rm.group(2)))
            except (ValueError, AttributeError):
                pass

    # ── Decidir el mejor candidato ──
    # Prioridad: rango Ollama > rango texto > single Ollama > single texto
    best_range = (ollama_ranges or text_ranges or [None])[0]
    if best_range and best_range is not None: # Added explicit check for None
        rango_str = f"{best_range[0]} - {best_range[1]}"
        return ValidationResult(
            code="VL-06",
            label="Rango salarial / Monto",
            passed=True,
            severity="error",
            detail=f"Monto detectado: {rango_str}. Verificar que coincida con carta de trabajo o cotización.",
        )

    if ollama_singles:
        return ValidationResult(
            code="VL-06",
            label="Rango salarial / Monto",
            passed=True,
            severity="error",
            detail=f"Monto detectado: {ollama_singles[0]}. Verificar que coincida con carta de trabajo o cotización.",
        )

    # ── Single en texto ──
    has_keyword = bool(_SALARIO_KEYWORD_RE.search(text))
    if not has_keyword:
        return ValidationResult(
            code="VL-06",
            label="Rango salarial",
            passed=False,
            severity="error",
            detail="No se encontró campo de salario/sueldo. Verificar coincidencia con carta de trabajo.",
        )
    for kw_m in _SALARIO_KEYWORD_RE.finditer(text):
        window = text[kw_m.start(): min(len(text), kw_m.start() + _SALARY_CONTEXT_WINDOW * 5)]
        for sm in _SALARY_RANGE_RE.finditer(window):
            if sm.group(1):
                try:
                    candidate = sm.group(0).strip()
                    if is_part_of_cedula(candidate, cedulas):
                        continue
                    if _to_float(sm.group(1)) >= 100.0:
                        rango_str = f"{sm.group(1)} - {sm.group(2)}" if sm.group(2) else sm.group(1)
                        return ValidationResult(
                            code="VL-06",
                            label="Rango salarial / Monto",
                            passed=True,
                            severity="error",
                            detail=f"Monto detectado: {rango_str}. Verificar que coincida con carta de trabajo o cotización.",
                        )
                except (ValueError, AttributeError):
                    continue

    return ValidationResult(
        code="VL-06",
        label="Rango salarial",
        passed=False,
        severity="error",
        detail="Campo 'Salario' encontrado pero sin monto numérico válido (>= 100). Verificar con carta de trabajo.",
    )


def val_lugar_nacimiento(text: str) -> ValidationResult:
    """VL-07: Lugar de nacimiento debe contener una Provincia panameña.

    Estrategias:
    1) Leer el Key-Value de Ollama y buscar una provincia panameña ahí, iterando
       sobre todas las páginas por si alguna responde NO ENCONTRADO.
    2) Si Ollama falló en todas las páginas, buscar la etiqueta 'Lugar de Nacimiento'
       en el texto OCR y buscar una provincia en una ventana de 150 caracteres.
    """
    # Evaluar respuesta de Ollama iterando
    for ollama_m in _NACIMIENTO_OLLAMA_RE.finditer(text):
        val_str = ollama_m.group(1).strip()
        if val_str.upper() != "NO ENCONTRADO":
            prov_match = _PROVINCIA_RE.search(val_str)
            pais_match = _PAIS_RE.search(val_str)
            if prov_match:
                lugar = f"{prov_match.group(0).strip()}, {pais_match.group(0).strip() if pais_match else 'Panamá'}"
                return ValidationResult(
                    code="VL-07",
                    label="Lugar de nacimiento (Provincia + País)",
                    passed=True,
                    severity="error",
                    detail=f"Lugar de nacimiento detectado por LLM: «{lugar}»",
                )

    # Fallback al texto local si Ollama ignoró el campo en todas las páginas
    label_match = _NACIMIENTO_LABEL_RE.search(text)
    if not label_match:
        return ValidationResult(
            code="VL-07",
            label="Lugar de nacimiento (Provincia + País)",
            passed=False,
            severity="error",
            detail="Campo 'Lugar de nacimiento' ausente. Afecta hoja de datos y Cocotito pág. 1.",
        )

    # Buscar la provincia solo CERCA del label
    window_start = label_match.end()
    window_end = min(len(text), window_start + 150)
    window_text = text[window_start:window_end]
    
    prov_match = _PROVINCIA_RE.search(window_text)
    if prov_match:
        pais_match = _PAIS_RE.search(window_text)
        lugar = f"{prov_match.group(0).strip()}, {pais_match.group(0).strip() if pais_match else 'Panamá'}"
        return ValidationResult(
            code="VL-07",
            label="Lugar de nacimiento (Provincia + País)",
            passed=True,
            severity="error",
            detail=f"Lugar de nacimiento detectado en texto: «{lugar}»",
        )

    return ValidationResult(
        code="VL-07",
        label="Lugar de nacimiento (Provincia + País)",
        passed=False,
        severity="error",
        detail="Campo encontrado, pero no se detectó una provincia panameña válida contigua. Afecta hoja de datos y Cocotito pág. 1.",
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

    Busca los distintos formatos reales de planilla panameña:
      - Estructurado por Ollama (ej. 8-21-06-0-01979)
      - P0800013010  (P + dígitos)
      - N.PR08-3722  (prefijo N.PR + código)
      - Formato Gobierno: 8-21-06-0-01979 (múltiples guiones)
      - Formato simple: 08-3722
    """
    has_field = bool(_PLANILLA_FIELD_RE.search(text))

    candidatos = []

    # 1. Intentar con salida estructurada de Ollama
    for ollama_field_m in _PLANILLA_OLLAMA_FIELD_RE.finditer(text):
        val = ollama_field_m.group(1).strip()
        if not is_dummy_value(val):
            candidatos.append(val)

    # 2. Buscar por expresiones regulares en fallback
    if not candidatos:
        gob_match = _PLANILLA_GOB_RE.search(text)
        p_match = _PLANILLA_P_NUM_RE.search(text)
        npr_match = _PLANILLA_NPR_RE.search(text)
        simple_match = _PLANILLA_SIMPLE_RE.search(text)
        ollama_match = _PLANILLA_OLLAMA_RE.search(text)

        if gob_match:
            candidatos.append(gob_match.group(1))
        if p_match:
            candidatos.append(f"P{p_match.group(1)}")
        if npr_match:
            candidatos.append(f"N.PR{npr_match.group(1)}")
        if simple_match and not p_match and not npr_match and not gob_match:
            candidatos.append(simple_match.group(1))
        if ollama_match and not p_match and not npr_match and not simple_match and not gob_match:
            candidatos.append(ollama_match.group(1))

    # Eliminar duplicados y CÉDULAS falsas
    cedulas = _CEDULA_RE.findall(text)
    unique_candidates = []
    for c in candidatos:
        if c not in unique_candidates and not is_part_of_cedula(c, cedulas):
            unique_candidates.append(c)

    has_candidates = len(unique_candidates) > 0

    if not has_field and not has_candidates:
        return ValidationResult(
            code="VL-10",
            label="Número de planilla",
            passed=False,
            severity="error",
            detail="No se encontró campo de planilla/nómina. Afecta F1-b en las generales.",
        )

    if has_candidates:
        if len(unique_candidates) == 1:
            detail = (
                f"Planilla detectada: {unique_candidates[0]}. "
                "Verificar que coincida con el número de planilla en carta de trabajo."
            )
        else:
            detail = (
                f"Dos candidatos detectados: {' y '.join(unique_candidates)}. "
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
    # 1. Buscar en la plantilla de Ollama
    ollama_dir_re = re.compile(r'(?i)Direccion Residencial:\s*(?!no\s+encontrado)([\s\S]+?)(?=\n[A-Za-z0-9\s]+:|$)')
    value = None
    for m in ollama_dir_re.finditer(text):
        val = m.group(1).strip()
        if not is_dummy_value(val) and len(val) >= 10:
            value = val
            break
            
    # 2. Fallback al regex tradicional
    if not value:
        match = _DIRECCION_RE.search(text)
        if match:
            candidate = match.group(1).strip()
            if not is_dummy_value(candidate) and len(candidate) >= 10:
                value = candidate

    if not value:
        return ValidationResult(
            code="VL-11",
            label="Longitud de dirección",
            passed=True,
            severity="warning",
            detail="No se detectó campo de dirección en el texto extraído.",
        )

    passed = len(value) <= _ADDRESS_MAX_CHARS
    detail = (
        f"Dirección detectada: «{value}» ({len(value)} caracteres). ✅"
        if passed
        else (
            f"Dirección extensa: «{value}» ({len(value)} caracteres, límite recomendado: {_ADDRESS_MAX_CHARS}). "
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
    Prioriza la salida estructurada de Ollama ('Estado Civil: ...').
    Usa una ventana de texto mediante Fuzzy Matching como fallback.
    """
    estado_str = "no detectado"
    is_casado = False
    es_soltero = False

    # 1. Intentar con salida de Ollama iterando páginas
    for ollama_m in _ESTADO_CIVIL_OLLAMA_RE.finditer(text):
        candidato = ollama_m.group(1).strip()
        if candidato.upper() != "NO ENCONTRADO":
            val_ec = _ESTADO_CIVIL_VALUE_RE.search(candidato)
            if val_ec:
                estado_str = val_ec.group(1).strip().title()
                is_casado = bool(re.search(r'\b(?:casad[oa]|unid[oa])\b', estado_str, re.IGNORECASE))
                es_soltero = bool(re.search(r'\bsolter[ao]\b', estado_str, re.IGNORECASE))
                break

    # 2. Fallback al OCR clásico si Ollama no encontró nada
    if estado_str == "no detectado":
        label_m = _ESTADO_CIVIL_LABEL_RE.search(text)
        idx_ec = label_m.start() if label_m else fuzzy_find(text, "estado civil", threshold=80.0)
        
        if idx_ec != -1:
            ventana_ec = text[idx_ec: idx_ec + 80]
            val_ec = _ESTADO_CIVIL_VALUE_RE.search(ventana_ec)
            if val_ec:
                estado_str = val_ec.group(1).strip().title()
                is_casado = bool(re.search(r'\b(?:casad[oa]|unid[oa])\b', estado_str, re.IGNORECASE))
                es_soltero = bool(re.search(r'\bsolter[ao]\b', estado_str, re.IGNORECASE))
    
    # Manejar soltero/no casado
    if not is_casado:
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

    # ── NUEVA ESTRATEGIA: Buscar campos estructurados de Ollama primero ──
    nombre_str = None
    cedula_str = None
    labora_str = None
    empresa_str = None

    # Find name first
    target_match = None
    for m in _CONYUGUE_NOMBRE_OLLAMA_RE.finditer(text):
        val = m.group(1).strip().title()
        if not is_dummy_value(val):
            nombre_str = val
            target_match = m
            break

    if target_match:
        # We found a spouse name! Now look for the other fields near this match
        start_idx = max(0, target_match.start() - 200)
        end_idx = min(len(text), target_match.end() + 800)
        window_text = text[start_idx:end_idx]

        # Search for Cedula in this window
        m_cedula = _CONYUGUE_CEDULA_OLLAMA_RE.search(window_text)
        if m_cedula:
            val = m_cedula.group(1).strip().upper()
            if not is_dummy_value(val):
                cedula_str = val

        # Search for Labora in this window
        m_labora = _CONYUGUE_LABORA_OLLAMA_RE.search(window_text)
        if m_labora:
            val = m_labora.group(1).strip().upper()
            if val.upper() not in ("NO ENCONTRADO", "?", "", "N/A", "NO APLICA"):
                labora_str = val

        # Search for Empresa in this window
        m_empresa = _CONYUGUE_EMPRESA_OLLAMA_RE.search(window_text)
        if m_empresa:
            val = m_empresa.group(1).strip().title()
            if not is_dummy_value(val):
                empresa_str = val

    # Fallback si no se encontró vía estructura de Ollama pero existe bloque de Contrato/Conyuge por OCR directo
    if not nombre_str:
        idx_contrato = text.find("informacion del contrato")
        if idx_contrato == -1:
            idx_contrato = text.find("informacion del conyuge")
            
        sub_text = None
        if idx_contrato != -1:
            sub_text = text[idx_contrato:idx_contrato + 400]
        else:
            # Fallback por proximidad de secciones si el OCR omitió el encabezado
            idx_laborales = text.find("fondos publicos")
            if idx_laborales == -1:
                idx_laborales = text.find("datos laborales")
            idx_referencias = text.find("referencias personales", idx_laborales)
            if idx_laborales != -1 and idx_referencias != -1 and idx_laborales < idx_referencias:
                sub_text = text[idx_laborales:idx_referencias]
            
        if sub_text:
            # Robust literal regex matching
            temp_nombre = None
            temp_cedula = None
            temp_labora = None
            temp_empresa = None
            
            m_nom = re.search(r'(?:nombre completo|nombre)\s*[:\-]\s*([a-z\u00e1\u00e9\u00ed\u00f3\u00fañ\s]+)', sub_text)
            if m_nom:
                temp_nombre = m_nom.group(1).split('\n')[0].strip()
                
            m_ced = re.search(r'(?:cedula/pasaporte|cedula|codua|pasaporte)\s*[:\-]\s*([a-z0-9\-]+)', sub_text)
            if m_ced:
                temp_cedula = m_ced.group(1).split('\n')[0].strip()
                
            m_lab = re.search(r'(?:laborat|labora|trabaja)\s*[:\-]?\s*([^\n]*)', sub_text)
            if m_lab:
                lab_line = m_lab.group(1).lower()
                lab_window_idx = sub_text.find(m_lab.group(0)) + len(m_lab.group(0))
                lab_window = sub_text[lab_window_idx:lab_window_idx + 100].lower()
                if 'si' in lab_line or 'si x' in lab_line or 'si x' in lab_window or 'si' in lab_window:
                    temp_labora = "si"
                else:
                    temp_labora = "no"
                    
            m_emp = re.search(r'(?:direccion laboral|nombre empresa|empresa)\s*[:\-]\s*([a-z\u00e1\u00e9\u00ed\u00f3\u00fañ\s]+)', sub_text)
            if m_emp:
                temp_empresa = m_emp.group(1).split('\n')[0].strip()
            
            # If any are missing, try line-by-line fallback
            lineas = [l.strip() for l in sub_text.split("\n") if l.strip()]
            for idx_l, line in enumerate(lineas[1:6]):
                if not temp_cedula:
                    m_ced = _CEDULA_RE.search(line)
                    if m_ced:
                        temp_cedula = m_ced.group(0)
                        if not temp_nombre and idx_l + 2 < len(lineas):
                            next_line = lineas[idx_l + 2]
                            if not any(kw in next_line for kw in ("direccion", "empresa", "telefono", "labora")):
                                temp_nombre = next_line
                if not temp_empresa:
                    if "empresa privada" in line:
                        temp_empresa = "empresa privada"
                    elif "empresa" in line or "nombre empresa" in line:
                        m_emp_lbl = re.search(r'(?:nombre\s+)?empresa\s*[:\-]?\s*([a-z\s]+)', line)
                        if m_emp_lbl:
                            temp_empresa = m_emp_lbl.group(1).strip()
                if not temp_labora:
                    if "labora" in line:
                        if "si" in line:
                            temp_labora = "si"
                        elif "no" in line:
                            temp_labora = "no"

            if temp_empresa and not temp_labora:
                temp_labora = "si"
                
            if temp_nombre:
                nombre_str = temp_nombre.title()
            if temp_cedula:
                cedula_str = temp_cedula.upper()
            if temp_empresa:
                empresa_str = temp_empresa.title()
            if temp_labora:
                labora_str = temp_labora.upper()

    partes = []
    if nombre_str:
        partes.append(f"Cónyuge: {nombre_str}")
    if cedula_str:
        partes.append(f"Cédula: {cedula_str}")
    if labora_str:
        partes.append(f"Labora: {labora_str}")
    if empresa_str:
        partes.append(f"Empresa: {empresa_str}")

    # Fallback legacy si la plantilla de Ollama no devolvió información estructurada del cónyuge
    if not partes:
        # Buscar "nombre empresa" (anclaje para la sección cónyuge) vía Fuzzy o Regex
        has_conyugue = bool(re.search(r'\bc[oó]nyug?e\b|esposo|esposa', text, re.IGNORECASE))
        
        # Tratamos de conseguir ancla con 'nombre empresa' 
        idx_empresa = fuzzy_find(text, "nombre empresa", threshold=85.0)
        
        if idx_empresa == -1 and not has_conyugue:
            # Intentar más anclas: "nombre del cónyuge", "datos del cónyuge", "datos conyugales"
            idx_empresa = fuzzy_find(text, "nombre del conyuge", threshold=80.0)
            if idx_empresa == -1:
                idx_empresa = fuzzy_find(text, "datos del conyuge", threshold=80.0)
        
        if idx_empresa == -1 and not has_conyugue:
            # OCR destruyó por completo o la sección no existe
            return ValidationResult(
                code="VL-12",
                label="Información de cónyuge",
                passed=False,
                severity="error",
                detail=(
                    f"Estado civil {estado_str.lower()} pero NO se detectó sección de cónyuge "
                    "(Anclas 'Nombre Empresa', 'Conyuge/Esposo'). Campo obligatorio."
                ),
            )

        # Si no hay ancla de empresa pero sí se detectó la palabra de cónyuge, 
        # buscamos una ventana alrededor de la palabra cónyuge/esposo.
        if idx_empresa == -1:
            match_c = re.search(r'\bc[oó]nyug?e\b|esposo|esposa', text, re.IGNORECASE)
            idx_anchor = match_c.start() if match_c else 0
        else:
            idx_anchor = idx_empresa

        win_start = max(0, idx_anchor - 400)
        win_end = min(len(text), idx_anchor + 200)
        ventana = text[win_start:win_end]

        # Extraer usando regex sobre la ventana acotada
        nombre_m = re.search(r'\b(?:[1-9n][0-9a-z\-]{3,11})\s+([a-záéíóúñ]{2,}(?:\s+[a-záéíóúñ]{2,}){1,4})\s+\d{7,9}\b', ventana)
        labora_m = re.search(r'(?:e?labora)[?!]?.{0,300}?\b(si)\b\s+no\s+x|(?:e?labora)[?!]?.{0,300}?x\s+(si)\b\s+no|(?:e?labora)[?!]?.{0,300}?\b(si)\s+x\b', ventana)
        empresa_m = re.search(r'nombre\s+empresa\s+(?!referencias?|datos|solicitud|direcci[oó]n|tel[eé]fono|parentesco)([a-záéíóúñ]{2,}(?:\s+[a-záéíóúñ]{2,}){0,4})', ventana)
        
        if nombre_m:
            nombre_val = nombre_m.group(1).strip().title()
            partes.append(f"Cónyuge: {nombre_val[:40]}")

        if labora_m:
            partes.append(f"Labora: SI")

        if empresa_m:
            empresa_val = empresa_m.group(1).strip().title()
            if len(empresa_val) >= 3:
                partes.append(f"Empresa: {empresa_val[:40]}")

    if partes:
        resumen = " | ".join(partes)
        detail = f"{resumen}. Verificar coincidencia con los documentos presentados."
        return ValidationResult(
            code="VL-12",
            label="Información de cónyuge",
            passed=True,
            severity="error",
            detail=detail,
        )
    else:
        detail = "Sección de cónyuge detectada pero valores ilegibles por OCR. Verificar nombre y datos laborales."

    return ValidationResult(
        code="VL-12",
        label="Información de cónyuge",
        passed=True,
        severity="error",
        detail=detail,
    )


def val_proximidad_huella_firma(
    pages_firma: list[int],
    pages_huella: list[int],
    relations: list[str],
    loan_type: str = "cies",
) -> ValidationResult:
    """VL-13: Firma y huella no deben estar alejadas entre sí en la página (verificado por LLM)."""
    if loan_type == "ventanilla":
        return ValidationResult(
            code="VL-13",
            label="Proximidad huella-firma",
            passed=True,
            severity="warning",
            detail="Validación de proximidad de huella-firma omitida para Préstamo Ventanilla.",
        )

    if not pages_firma or not pages_huella:
        missing = []
        if not pages_firma:
            missing.append("firmas")
        if not pages_huella:
            missing.append("huellas")
        detail = f"Faltante: No se detectaron {', '.join(missing)} del cliente en las páginas analizadas. Imposible evaluar proximidad."
        return ValidationResult(
            code="VL-13",
            label="Proximidad huella-firma",
            passed=False,
            severity="warning",
            detail=detail,
        )

    has_correcta = "CORRECTA" in relations or "SOLAPADA" in relations
    has_lejos = "LEJOS" in relations and not has_correcta

    if has_correcta:
        passed = True
        detail = "Huella y firma detectadas en proximidad correcta por el modelo de visión. ✅"
    elif has_lejos:
        passed = False
        detail = "La huella dactilar está alejada de la firma en la página (verificado por el modelo de visión)."
    else:
        passed = False
        detail = "Se detectaron firmas y huellas, pero el modelo de visión no pudo confirmar su proximidad correcta."

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
    pages_firma: list[int] | None = None,
    pages_huella: list[int] | None = None,
    relations: list[str] | None = None,
    loan_type: str = "cies",
) -> list[ValidationResult]:
    """Ejecuta las 13 validaciones en orden y retorna la lista de resultados."""
    # ── NORMALIZACIÓN PREVIA DE TEXTO (Aplica para todas las validaciones base) ──
    norm_text = normalize_ocr_text(text)
    
    p_firma = pages_firma or []
    p_huella = pages_huella or []
    rels = relations or []
    
    return [
        val_cedula_format(norm_text),
        val_motivo_prestamo(norm_text),
        val_numero_seguro_social(norm_text),
        val_referencias(norm_text),
        val_cargo_posicion(norm_text),
        val_rango_salarial(norm_text),
        val_lugar_nacimiento(norm_text),
        val_efectividades(norm_text),
        val_firma_cotizacion(norm_text, firma_detected, p_firma),
        val_numero_planilla(norm_text),
        val_direccion_longitud(norm_text),
        val_info_conyugue(norm_text),
        val_proximidad_huella_firma(p_firma, p_huella, rels, loan_type),
    ]
