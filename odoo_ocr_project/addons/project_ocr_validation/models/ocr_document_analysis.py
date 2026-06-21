# -*- coding: utf-8 -*-
"""
Lógica Comercial: Documento OCR – project.ocr.document
Este archivo extiende el modelo base para manejar la lógica 
de la integración con el microservicio OCR vía HTTP.
"""

import json
import logging
import requests

from odoo import api, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# URL del microservicio OCR dentro de la red Docker
OCR_ENGINE_URL = "http://172.17.0.33:8000/api/v1/analyze-pdf"


class OcrDocumentAnalysis(models.Model):
    _inherit = "project.ocr.document"

    def action_analyze_document(self):
        """
        Orquesta el flujo principal: Validar, Preparar, Enviar, y Guardar.
        """
        self.ensure_one()

        self._validate_attachment()
        self._set_processing_state()

        try:
            payload = self._prepare_ocr_payload()
            result = self._send_ocr_request(payload)
            self._process_ocr_response(result)

        except requests.exceptions.ConnectionError:
            self._handle_ocr_error(_("No se pudo conectar con el motor de análisis OCR. Verifique que el servicio 'ocr_engine' esté ejecutándose."))
        except requests.exceptions.Timeout:
            self._handle_ocr_error(_("El motor de OCR no respondió a tiempo. El documento puede ser muy pesado."))
        except requests.exceptions.RequestException as exc:
            self._handle_ocr_error(_("Error al comunicarse con el motor OCR: %s") % str(exc))
        except (ValueError, KeyError) as exc:
            self._handle_ocr_error(_("Error al procesar la respuesta del motor OCR: %s") % str(exc))

        return True

    def action_reset_to_draft(self):
        """Reinicia el documento al estado borrador para reprocesamiento."""
        self.ensure_one()
        self.write({
            "state": "draft",
            "has_signature": False,
            "has_fingerprint": False,
            "has_date": False,
            "extracted_date": False,
            "fecha_word_count": 0,
            "fecha_value_count": 0,
            "firma_word_count": 0,
            "firma_detected_count": 0,
            "analysis_details": False,
            "error_message": False,
        })
        self.message_post(body=_("Documento reiniciado a estado borrador."), message_type="notification")
        return True

    def _validate_attachment(self):
        """Valida que el archivo PDF esté listo y tenga contenido."""
        if not self.attachment_id:
            raise UserError(_("Debe adjuntar un archivo PDF antes de ejecutar el análisis."))
        if not self.attachment_id.datas:
            raise UserError(_("El archivo adjunto no contiene datos. Por favor, suba nuevamente el archivo."))

    def _set_processing_state(self):
        """Cambia el estado del registro e informa en el Chatter."""
        self.write({"state": "processing", "error_message": False})
        self.message_post(
            body=_("Iniciando análisis del documento '%s'...") % self.name,
            message_type="notification",
        )

    def _prepare_ocr_payload(self) -> dict:
        """Decodifica el binario de Odoo y prepara el Payload HTTP."""
        return {
            "filename": self.attachment_id.name or self.name,
            "file_data": self.attachment_id.datas.decode("utf-8"),
        }

    def _send_ocr_request(self, payload: dict) -> dict:
        """Realiza la llamada HTTP al microservicio y retorna el JSON."""
        _logger.info("Enviando doc '%s' (ID: %s) al motor OCR: %s", self.name, self.id, OCR_ENGINE_URL)
        
        response = requests.post(
            OCR_ENGINE_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=3600,
        )
        response.raise_for_status()
        
        result = response.json()
        _logger.info("Respuesta cruda del motor OCR: %s", json.dumps(result, ensure_ascii=False))
        return result

    def _process_ocr_response(self, result: dict):
        """Estructura y guarda los datos de la respuesta en la Base de Datos."""
        update_values = self._extract_data_dict(result)
        self.write(update_values)
        self._post_analysis_chatter_message(result)

    def _extract_data_dict(self, result: dict) -> dict:
        """Mapea las llaves del JSON a los campos del Modelo."""
        # Use YOLO detection counts along with boolean fallbacks
        firma_count = result.get("firma_detected_count", 0)
        has_signature = result.get("firma", False) or firma_count > 0
        
        has_date = result.get("fecha_encontrada", False)
        
        # Determinar el Nivel de Cumplimiento Técnico (Compliance)
        if has_signature and has_date:
            validation_state = "conforme"
        elif has_signature or has_date:
            validation_state = "observado"
        else:
            validation_state = "no_conforme"

        values = {
            "state": "done",
            "has_signature": has_signature,
            "has_fingerprint": result.get("huella", False),
            "has_date": has_date,
            "validation_state": validation_state,
            "extracted_date": result.get("fecha_valor", ""),
            "fecha_word_count": result.get("fecha_word_count", 0),
            "fecha_value_count": result.get("fecha_value_count", 0),
            "firma_word_count": result.get("firma_word_count", 0),
            "firma_detected_count": result.get("firma_detected_count", 0),
            "error_message": False,
        }
        detalles = result.get("detalles")
        if detalles:
            values["analysis_details"] = json.dumps(detalles, indent=2, ensure_ascii=False)
        return values

    def _post_analysis_chatter_message(self, result: dict):
        """Arma y publica un resumen en el registro (Chatter)."""
        summary_parts = []
        
        f_word = result.get('firma_word_count', 0)
        f_det = result.get('firma_detected_count', 0)
        has_firm = result.get('firma', False) or f_det > 0
        summary_parts.append(
            f"Firma: {'detectada' if has_firm else 'no detectada'}"
        )
        
        summary_parts.append("Huella detectada" if result.get("huella") else "Huella no detectada")
        
        if result.get("fecha_encontrada"):
            d_word = result.get('fecha_word_count', 0)
            d_val = result.get('fecha_value_count', 0)
            summary_parts.append(
                f"Fecha encontrada: {result.get('fecha_valor', 'N/A')}"
            )
        else:
            summary_parts.append("No se encontró fecha")

        html = "<p><strong>Análisis completado</strong></p><ul>" + "".join(f"<li>{p}</li>" for p in summary_parts) + "</ul>"
        self.message_post(body=html, message_type="notification", subtype_xmlid="mail.mt_note")

    def _handle_ocr_error(self, error_msg: str):
        """Loguea y almacena el estado de error de forma centralizada."""
        _logger.error(error_msg)
        self.write({
            "state": "error",
            "error_message": error_msg,
        })
        self.message_post(body=f"<p><strong>Error:</strong> {error_msg}</p>", message_type="notification")
