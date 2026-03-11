# -*- coding: utf-8 -*-
"""
Modelo: Documento OCR – project.ocr.document
Gestión de documentos PDF para validación automática mediante
OCR (extracción de fechas) y detección visual (firmas/huellas).
"""

import base64
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)



class OcrDocument(models.Model):
    _name = "project.ocr.document"
    _description = "Documento de Validación OCR"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _rec_name = "name"

    # Campos del modelo
    name = fields.Char(
        string="Nombre del Documento",
        required=True,
        tracking=True,
        help="Nombre identificador del documento a analizar.",
    )

    document_type = fields.Selection(
        selection=[
            ("type_a", "Tipo A"),
            ("type_b", "Tipo B"),
            ("type_c", "Tipo C"),
        ],
        string="Tipo de Documento",
        required=True,
        tracking=True,
        help="Clasificación del documento a analizar.",
    )

    attachment_id = fields.Many2one(
        comodel_name="ir.attachment",
        string="Archivo PDF",
        required=True,
        help="Adjunte el archivo PDF que será enviado al motor de análisis OCR.",
        domain=[("mimetype", "=", "application/pdf")],
    )

    state = fields.Selection(
        selection=[
            ("draft", "Borrador"),
            ("processing", "Procesando"),
            ("done", "Completado"),
            ("error", "Error"),
        ],
        string="Estado",
        default="draft",
        tracking=True,
        help="Estado actual del procesamiento del documento.",
    )

    # ── Resultados del análisis ──
    has_signature = fields.Boolean(
        string="Firma Detectada",
        default=False,
        readonly=True,
        tracking=True,
        help="Indica si se detectó una firma manuscrita en el documento.",
    )

    has_fingerprint = fields.Boolean(
        string="Huella Detectada",
        default=False,
        readonly=True,
        tracking=True,
        help="Indica si se detectó una huella dactilar en el documento.",
    )

    has_date = fields.Boolean(
        string="Fecha Detectada",
        default=False,
        readonly=True,
        tracking=True,
        help="Indica si se encontró una fecha en el texto del documento.",
    )

    extracted_date = fields.Char(
        string="Fecha Extraída",
        readonly=True,
        tracking=True,
        help="Valor de la fecha extraída del documento por el motor OCR.",
    )

    fecha_word_count = fields.Integer(
        string="Palabras 'Fecha'",
        default=0,
        readonly=True,
    )

    fecha_value_count = fields.Integer(
        string="Fechas Valores",
        default=0,
        readonly=True,
    )

    firma_word_count = fields.Integer(
        string="Palabras 'Firma'",
        default=0,
        readonly=True,
    )

    firma_detected_count = fields.Integer(
        string="Firmas Detectadas",
        default=0,
        readonly=True,
    )

    analysis_details = fields.Text(
        string="Detalles del Análisis",
        readonly=True,
        help="Información técnica adicional devuelta por el microservicio.",
    )

    error_message = fields.Text(
        string="Mensaje de Error",
        readonly=True,
        help="Detalle del error ocurrido durante el procesamiento.",
    )

    validation_state = fields.Selection(
        selection=[
            ("conforme", "Conforme"),
            ("observado", "Observado / Revisión Manual"),
            ("no_conforme", "No Conforme / Rechazado"),
        ],
        string="Estado de Validación",
        readonly=True,
        tracking=True,
        help="Nivel de cumplimiento basado en los elementos detectados por el motor de IA.",
    )

    # ── Campo computado para indicador visual ──
    validation_summary = fields.Char(
        string="Resumen de Validación",
        compute="_compute_validation_summary",
        store=False,
    )

    # Métodos computados
    @api.depends("has_signature", "has_fingerprint", "has_date")
    def _compute_validation_summary(self):
        """Genera un resumen legible del estado de validación."""
        for record in self:
            parts = []
            parts.append("✅ Firma" if record.has_signature else "❌ Firma")
            parts.append("✅ Huella" if record.has_fingerprint else "❌ Huella")
            parts.append(f"✅ Fecha: {record.extracted_date or ''}" if record.has_date else "❌ Fecha")
            record.validation_summary = " | ".join(parts)


