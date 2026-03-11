# -*- coding: utf-8 -*-
"""
Modelo: Documento de Préstamo – project.loan.document
Gestión de documentos de préstamos Cíes y Ons con validación automática
a través del microservicio OCR + validadores de reglas de negocio.
"""

import base64
import json
import logging
import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# URL del endpoint de validación de préstamos en el microservicio OCR
LOAN_VALIDATE_URL = "http://ocr_engine:8000/api/v1/validate-loan"


class LoanValidationLine(models.Model):
    """Línea individual de resultado de validación (una por cada regla VL-01..VL-13)."""
    _name = "project.loan.validation.line"
    _description = "Línea de Validación de Préstamo"
    _order = "code"

    loan_id = fields.Many2one(
        comodel_name="project.loan.document",
        string="Documento de Préstamo",
        required=True,
        ondelete="cascade",
    )
    code = fields.Char(string="Código", readonly=True)
    label = fields.Char(string="Validación", readonly=True)
    passed = fields.Boolean(string="Aprobado", readonly=True, default=False)
    severity = fields.Selection(
        selection=[("error", "Error Crítico"), ("warning", "Aviso")],
        string="Severidad",
        readonly=True,
    )
    detail = fields.Char(string="Detalle", readonly=True)

    status_icon = fields.Char(
        string="Estado",
        compute="_compute_status_icon",
        store=False,
    )

    @api.depends("passed", "severity")
    def _compute_status_icon(self):
        for rec in self:
            if rec.passed:
                rec.status_icon = "✅"
            elif rec.severity == "error":
                rec.status_icon = "❌"
            else:
                rec.status_icon = "⚠️"


class LoanDocument(models.Model):
    """Documento de Préstamo Cíes u Ons con validación documental automatizada."""
    _name = "project.loan.document"
    _description = "Documento de Préstamo Cíes y Ons"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"
    _rec_name = "name"

    name = fields.Char(
        string="Nombre del Documento",
        required=True,
        copy=False,
        readonly=True,
        states={"draft": [("readonly", False)]},
        default=lambda self: _("Nuevo"),
        tracking=True,
    )
    loan_type = fields.Selection(
        selection=[
            ("cies", "Préstamo Cíes"),
            ("ons", "Préstamo Ons"),
        ],
        string="Tipo de Préstamo",
        required=True,
        tracking=True,
    )
    # Archivo PDF — subida directa (binary)
    pdf_file = fields.Binary(
        string="Archivo PDF",
        required=True,
        attachment=True,
        help="Sube el PDF del expediente de préstamo a validar.",
    )
    pdf_filename = fields.Char(string="Nombre del archivo PDF")
    state = fields.Selection(
        selection=[
            ("draft", "Borrador"),
            ("processing", "Procesando"),
            ("done", "Validado"),
            ("error", "Error"),
        ],
        string="Estado",
        default="draft",
        tracking=True,
    )

    # ── Resultados de validación ─────────────────────────────────────────────
    validation_ids = fields.One2many(
        comodel_name="project.loan.validation.line",
        inverse_name="loan_id",
        string="Resultados de Validación",
        readonly=True,
    )
    loan_compliance = fields.Selection(
        selection=[
            ("conforme", "✅ Conforme"),
            ("observado", "⚠️ Observado / Revisión Manual"),
            ("no_conforme", "❌ No Conforme / Devolver"),
        ],
        string="Estado de Cumplimiento",
        readonly=True,
        tracking=True,
    )
    total_errors = fields.Integer(string="Errores Críticos", default=0, readonly=True)
    total_warnings = fields.Integer(string="Avisos", default=0, readonly=True)
    error_message = fields.Text(string="Mensaje de Error", readonly=True)

    # ── ORM overrides ─────────────────────────────────────────────────────────

    @api.model
    def create(self, vals):
        if vals.get("name", _("Nuevo")) in (_("Nuevo"), False, ""):
            vals["name"] = (
                self.env["ir.sequence"].next_by_code("project.loan.document")
                or _("Nuevo")
            )
        return super().create(vals)

    # ── Acciones ─────────────────────────────────────────────────────────────

    def action_validate_loan(self):
        """Orquesta el flujo de validación: enviar PDF → recibir resultados → guardar."""
        self.ensure_one()

        if not self.pdf_file:
            raise UserError(_("Debe adjuntar un archivo PDF antes de ejecutar la validación."))

        self.write({"state": "processing", "error_message": False})
        self.message_post(
            body=_(
                "Iniciando validación del documento «%s» como Préstamo %s..."
            ) % (self.name, dict(self._fields["loan_type"].selection).get(self.loan_type, "")),
            message_type="notification",
        )

        try:
            # El campo binary devuelve bytes (Odoo 16 ya no usa b64decode aquí)
            pdf_bytes = self.pdf_file  # bytes en base64
            if isinstance(pdf_bytes, bytes):
                file_data = pdf_bytes.decode("utf-8")
            else:
                file_data = pdf_bytes

            payload = {
                "filename": self.pdf_filename or self.name,
                "file_data": file_data,
            }
            _logger.info(
                "Enviando PDF '%s' (ID: %s) al validador de préstamos: %s",
                self.name, self.id, LOAN_VALIDATE_URL,
            )
            response = requests.post(
                LOAN_VALIDATE_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=120,
            )
            response.raise_for_status()
            result = response.json()
            _logger.info("Respuesta del validador: %s", json.dumps(result, ensure_ascii=False))
            self._process_validation_result(result)

        except requests.exceptions.ConnectionError:
            self._handle_error(
                _("No se pudo conectar con el motor OCR. Verifique que el servicio 'ocr_engine' esté activo.")
            )
        except requests.exceptions.Timeout:
            self._handle_error(
                _("El motor OCR no respondió a tiempo. El documento puede ser muy pesado.")
            )
        except requests.exceptions.RequestException as exc:
            self._handle_error(_("Error de comunicación con el motor OCR: %s") % str(exc))
        except (ValueError, KeyError) as exc:
            self._handle_error(_("Error al procesar la respuesta del motor OCR: %s") % str(exc))

        return True

    def action_reset_to_draft(self):
        """Reinicia el documento a borrador para revalidar."""
        self.ensure_one()
        self.validation_ids.unlink()
        self.write({
            "state": "draft",
            "loan_compliance": False,
            "total_errors": 0,
            "total_warnings": 0,
            "error_message": False,
        })
        self.message_post(
            body=_("Documento reiniciado a borrador para nueva validación."),
            message_type="notification",
        )
        return True

    # ── Helpers privados ─────────────────────────────────────────────────────

    def _process_validation_result(self, result: dict):
        """Parsea la respuesta del microservicio y actualiza el modelo."""
        # Limpiar líneas anteriores
        self.validation_ids.unlink()

        validations = result.get("validations", [])
        lines = []
        for v in validations:
            lines.append({
                "loan_id": self.id,
                "code": v.get("code", ""),
                "label": v.get("label", ""),
                "passed": v.get("passed", False),
                "severity": v.get("severity", "warning"),
                "detail": v.get("detail", ""),
            })
        if lines:
            self.env["project.loan.validation.line"].create(lines)

        total_errors = result.get("total_errors", 0)
        total_warnings = result.get("total_warnings", 0)
        compliance = result.get("loan_compliance", "no_conforme")

        self.write({
            "state": "done",
            "loan_compliance": compliance,
            "total_errors": total_errors,
            "total_warnings": total_warnings,
        })

        # Resumen en chatter
        icons = {"conforme": "✅", "observado": "⚠️", "no_conforme": "❌"}
        icon = icons.get(compliance, "")
        failed = [v for v in validations if not v.get("passed")]
        issues_html = "".join(
            f"<li><strong>{v.get('code')}</strong> {v.get('label')}: {v.get('detail', '')}</li>"
            for v in failed
        )
        body = (
            f"<p><strong>Validación completada</strong> {icon} "
            f"Estado: <strong>{compliance.replace('_', ' ').title()}</strong></p>"
            f"<p>Errores: {total_errors} · Avisos: {total_warnings}</p>"
        )
        if issues_html:
            body += f"<ul>{issues_html}</ul>"
        self.message_post(body=body, message_type="notification", subtype_xmlid="mail.mt_note")

    def _handle_error(self, error_msg: str):
        """Centraliza el manejo de errores del proceso de validación."""
        _logger.error(error_msg)
        self.write({"state": "error", "error_message": error_msg})
        self.message_post(
            body=f"<p><strong>Error:</strong> {error_msg}</p>",
            message_type="notification",
        )
