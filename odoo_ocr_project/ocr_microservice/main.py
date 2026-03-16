"""
Microservicio de Análisis Documental – OCR + Detección YOLO
Recibe PDFs codificados en Base64, extrae fechas con PaddleOCR
y detecta firmas/huellas dactilares con un modelo YOLO entrenado.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import YOLO_MODEL_PATH, logger
from schemas import AnalyzeRequest, AnalyzeResponse, ErrorResponse, LoanValidationResponse, ValidationItem
from services import (
    decode_pdf,
    extract_text_statistics,
    load_models,
    pdf_to_images,
    run_ocr,
    run_yolo,
    run_yolo_detailed,
)
from validators import run_all_validations
import services

app = FastAPI(
    title="OCR & Signature Detection Engine",
    description="Microservicio para extracción de fechas (PaddleOCR) "
                "y detección de firmas/huellas (YOLOv8/v11).",
    version="1.0.0",
)


@app.on_event("startup")
async def startup_event() -> None:
    """Evento de arranque: carga los modelos de IA."""
    load_models()


@app.get("/health", tags=["Sistema"])
async def health_check():
    """Endpoint de health check para Docker y monitoreo."""
    return {
        "status": "healthy",
        "ocr_loaded": services.ocr_client is not None,
        "yolo_loaded": services.yolo_model is not None,
        "yolo_model_path": YOLO_MODEL_PATH,
    }


@app.post(
    "/api/v1/analyze-pdf",
    response_model=AnalyzeResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Datos de entrada inválidos"},
        422: {"model": ErrorResponse, "description": "Error al procesar el PDF"},
        500: {"model": ErrorResponse, "description": "Error interno del servidor"},
    },
    tags=["Análisis"],
    summary="Analizar un documento PDF",
    description="Recibe un PDF en Base64, extrae fechas con OCR y "
                "detecta firmas/huellas con YOLO.",
)
async def analyze_pdf(request: AnalyzeRequest) -> AnalyzeResponse:
    """
    Pipeline completo de análisis documental:
    1. Decodifica el PDF de Base64
    2. Convierte cada página a imagen
    3. Ejecuta PaddleOCR para extracción de texto y fechas
    4. Ejecuta YOLO para detección de firmas y huellas
    """
    logger.info("-" * 60)
    logger.info("Nuevo análisis: '%s'", request.filename)
    logger.info("-" * 60)

    # 1: Decodificar PDF
    pdf_bytes = decode_pdf(request.file_data)
    logger.info("Archivo decodificado: %s (%.2f KB)", request.filename, len(pdf_bytes) / 1024)

    # 2: Convertir PDF a imágenes
    images = pdf_to_images(pdf_bytes)

    # 3: Extracción de texto y fecha con PaddleOCR
    extracted_text = run_ocr(images)
    fecha_word_count, firma_word_count, fecha_value_count, fecha_valor = extract_text_statistics(extracted_text)

    # 4: Detección de firmas y huellas con YOLO
    firma_count, huella_count = run_yolo(images)
    
    firma_detected = firma_count > 0
    huella_detected = huella_count > 0
    fecha_encontrada = fecha_value_count > 0

    # Construir respuesta
    response = AnalyzeResponse(
        status="success",
        firma=firma_detected,
        huella=huella_detected,
        fecha_encontrada=fecha_encontrada,
        fecha_valor=fecha_valor,
        fecha_word_count=fecha_word_count,
        fecha_value_count=fecha_value_count,
        firma_word_count=firma_word_count,
        firma_detected_count=firma_count,
        detalles={
            "paginas_procesadas": len(images),
            "texto_extraido_longitud": len(extracted_text),
            "yolo_disponible": services.yolo_model is not None,
        },
    )

    logger.info("Análisis completado para '%s':", request.filename)
    logger.info("   Firma:  %s (%d palabras vs %d detectadas)", "Sí" if firma_detected else "No", firma_word_count, firma_count)
    logger.info("   Huella: %s", "Sí" if huella_detected else "No")
    logger.info("   Fecha:  %s (%d palabras vs %d detectadas)", fecha_valor if fecha_valor else "No encontrada", fecha_word_count, fecha_value_count)
    logger.info("   Páginas: %d", len(images))

    return response


@app.post(
    "/api/v1/validate-loan",
    response_model=LoanValidationResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Datos de entrada inválidos"},
        422: {"model": ErrorResponse, "description": "Error al procesar el PDF"},
        500: {"model": ErrorResponse, "description": "Error interno del servidor"},
    },
    tags=["Validación Préstamos"],
    summary="Validar expediente de Préstamo Cíes u Ons",
    description=(
        "Recibe un PDF de expediente de préstamo en Base64, ejecuta OCR + YOLO "
        "y aplica las 13 reglas de validación específicas para préstamos Cíes y Ons."
    ),
)
async def validate_loan(request: AnalyzeRequest) -> LoanValidationResponse:
    """
    Pipeline de validación de préstamos:
    1. Decodifica y convierte el PDF a imágenes
    2. Extrae texto con PaddleOCR
    3. Detecta firmas/huellas con YOLO (incluyendo bounding boxes para proximidad)
    4. Ejecuta las 13 reglas de validación
    5. Calcula el estado de cumplimiento (conforme / observado / no_conforme)
    """
    logger.info("=" * 60)
    logger.info("Validación de préstamo: '%s'", request.filename)
    logger.info("=" * 60)

    pdf_bytes = decode_pdf(request.file_data)
    images = pdf_to_images(pdf_bytes)
    extracted_text = run_ocr(images)

    fecha_word_count, firma_word_count, fecha_value_count, fecha_valor = extract_text_statistics(extracted_text)

    # Usamos run_yolo_detailed para obtener las coordenadas de los bounding boxes y las páginas
    firma_count, huella_count, boxes_firma, boxes_huella, pages_firma, pages_huella = run_yolo_detailed(images)
    firma_detected = firma_count > 0
    huella_detected = huella_count > 0

    # Ejecutar las 13 validaciones
    results = run_all_validations(extracted_text, firma_detected, boxes_firma, boxes_huella, pages_firma)

    total_errors = sum(1 for r in results if not r.passed and r.severity == "error")
    total_warnings = sum(1 for r in results if not r.passed and r.severity == "warning")

    if total_errors == 0:
        loan_compliance = "conforme"
    elif total_errors <= 3:
        loan_compliance = "observado"
    else:
        loan_compliance = "no_conforme"

    logger.info("Validación completada para '%s':", request.filename)
    logger.info("  Errores: %d | Avisos: %d | Cumplimiento: %s", total_errors, total_warnings, loan_compliance)

    return LoanValidationResponse(
        status="success",
        firma=firma_detected,
        huella=huella_detected,
        fecha_encontrada=fecha_value_count > 0,
        fecha_valor=fecha_valor,
        fecha_word_count=fecha_word_count,
        fecha_value_count=fecha_value_count,
        firma_word_count=firma_word_count,
        firma_detected_count=firma_count,
        detalles={
            "paginas_procesadas": len(images),
            "texto_extraido_longitud": len(extracted_text),
            "yolo_disponible": services.yolo_model is not None,
        },
        validations=[
            ValidationItem(
                code=r.code,
                label=r.label,
                passed=r.passed,
                severity=r.severity,
                detail=r.detail,
            )
            for r in results
        ],
        total_errors=total_errors,
        total_warnings=total_warnings,
        loan_compliance=loan_compliance,
    )


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Captura excepciones no controladas para evitar crashes."""
    logger.exception("Error no controlado: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "detail": f"Error interno del servidor: {str(exc)}",
        },

    )
