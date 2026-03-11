{
    "name": "Validación OCR de Documentos",
    "version": "16.0.1.2.0",
    "category": "Project",
    "summary": "Análisis OCR de documentos PDF y validación de expedientes de préstamos Cíes y Ons",
    "description": """
        Módulo de validación documental integrado con microservicio de IA.
        
        Funcionalidades:
        ─────────────────
        • Carga de documentos PDF adjuntos
        • Extracción automática de fechas mediante PaddleOCR
        • Detección de firmas manuscritas mediante modelo YOLO
        • Detección de huellas dactilares mediante modelo YOLO
        • Flujo de estados: Borrador → Procesando → Completado / Error
        • Integración con el chatter de Odoo (mail.thread)
        • Validación de expedientes de Préstamos Cíes y Ons (13 reglas)
    """,
    "author": "Grupo Saleta",
    "website": "https://www.gruposaleta.com",
    "license": "LGPL-3",
    "depends": [
        "base",
        "project",
        "mail",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/ocr_document_views.xml",
        "views/loan_document_views.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
