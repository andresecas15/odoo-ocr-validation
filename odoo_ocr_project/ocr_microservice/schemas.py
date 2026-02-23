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
