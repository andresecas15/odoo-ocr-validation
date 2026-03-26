from typing import Optional
from pydantic import BaseModel, Field

class AnalyzeRequest(BaseModel):
    """Esquema de entrada para el análisis de PDF."""
    filename: str = Field(
        ...,
        description="Nombre del archivo PDF",
        examples=["contrato_firmado.pdf"],
    )
    file_data: str = Field(
        ...,
        description="Contenido del PDF codificado en Base64",
    )
    loan_type: Optional[str] = Field(
        default="cies",
        description="Tipo de préstamo (cies, ons, ventanilla)"
    )

class AnalyzeResponse(BaseModel):
    """Esquema de respuesta del análisis."""
    status: str = Field(default="success", description="Estado del procesamiento")
    firma: bool = Field(default=False, description="Firma detectada en el documento")
    huella: bool = Field(default=False, description="Huella dactilar detectada")
    fecha_encontrada: bool = Field(default=False, description="Fecha encontrada en texto")
    fecha_valor: Optional[str] = Field(default=None, description="Valor de la fecha extraída")
    fecha_word_count: int = Field(default=0, description="Veces que aparece la palabra 'fecha'")
    fecha_value_count: int = Field(default=0, description="Cantidad de fechas extraídas")
    firma_word_count: int = Field(default=0, description="Veces que aparece la palabra 'firma'")
    firma_detected_count: int = Field(default=0, description="Cantidad de firmas detectadas visualmente")
    detalles: Optional[dict] = Field(default=None, description="Información adicional del análisis")

class ErrorResponse(BaseModel):
    """Esquema de respuesta de error."""
    status: str = "error"
    detail: str


# ─── Esquemas para validación de préstamos ────────────────────────────────────

class ValidationItem(BaseModel):
    """Resultado individual de una regla de validación."""
    code: str = Field(description="Código de la regla (ej. VL-01)")
    label: str = Field(description="Nombre legible de la validación")
    passed: bool = Field(description="True = cumple la regla, False = requiere atención")
    severity: str = Field(description="'error' | 'warning'")
    detail: Optional[str] = Field(default=None, description="Mensaje explicativo para el oficial")


class LoanValidationResponse(AnalyzeResponse):
    """Respuesta extendida con los resultados de las 13 validaciones de préstamos Cíes y Ons."""
    validations: list[ValidationItem] = Field(
        default=[],
        description="Lista de resultados por cada regla de validación",
    )
    total_errors: int = Field(default=0, description="Cantidad de errores críticos")
    total_warnings: int = Field(default=0, description="Cantidad de avisos/advertencias")
    loan_compliance: str = Field(
        default="no_conforme",
        description="Estado de cumplimiento: 'conforme' | 'observado' | 'no_conforme'",
    )

