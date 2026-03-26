# project_ocr_validation — Módulo Odoo 16

Módulo de **validación documental automatizada** para expedientes de préstamos Cíes, Ons y Ventanilla.  
Integra un microservicio Python (FastAPI + Ollama LLM) con Odoo para analizar PDFs y aplicar 13 reglas de cumplimiento documental.

> **Nota para IT:** Este módulo es autocontenido y funcional tal cual, pero está pensado para que el equipo técnico lo **herede** en su propio addon (`_inherit`) y lo adapte al modelo de negocio (carpeta de crédito, project.task, account.move, etc.) sin modificar este módulo base.

---

## Arquitectura general

```
Odoo (addon)                           Microservicio (Docker)
┌──────────────────────────────┐      ┌──────────────────────────────┐
│  LoanDocument                │      │  FastAPI (Background Thread) │
│  (project.loan.document)     │ HTTP │  POST /api/v1/validate-loan  │
│  Llamada vía Cron Asíncrono  │ ───► │                              │
│                              │      │  Ollama (MiniCPM) →  OCR     │
│  ValidationLine              │ ◄─── │  YOLO (Opcional)→  visual  │
│  (×13 reglas VL)             │ JSON │  validators.py    →  13 VLs  │
└──────────────────────────────┘      └──────────────────────────────┘
```

El addon también incluye `project.ocr.document` — modelo genérico de análisis OCR sin reglas de negocio, pensado para tipos de documento distintos a préstamos.

---

## Modelos

### `project.loan.document` — Expediente de Préstamo

Modelo principal. Un registro = un expediente PDF a validar.

| Campo | Tipo | Descripción |
|---|---|---|
| `name` | `Char` | Secuencia automática `PRES/0001`. Solo editable en Borrador. |
| `loan_type` | `Selection` | Tipo: `cies` (Préstamo Cíes) / `ons` (Préstamo Ons) / `ventanilla` (Préstamo Ventanilla). |
| `pdf_file` | `Binary` | PDF adjunto del expediente (almacenado como attachment). |
| `pdf_filename` | `Char` | Nombre del archivo, para mostrar en la vista. |
| `state` | `Selection` | Flujo: `draft → processing → done / error`. |
| `validation_ids` | `One2many` | Líneas de resultado, una por cada regla VL-01..VL-13. |
| `loan_compliance` | `Selection` | Resultado global: `conforme / observado / no_conforme`. |
| `total_errors` | `Integer` | Cantidad de reglas `error` no aprobadas. |
| `total_warnings` | `Integer` | Cantidad de reglas `warning` no aprobadas. |
| `error_message` | `Text` | Detalle si el microservicio falló. |

**Acción principal:** `action_validate_loan()` — envía el PDF al microservicio y guarda los resultados.

---

### `project.loan.validation.line` — Línea de Validación

Una por cada regla de cumplimiento. Relacionada con `loan_id` (cascade).

| Campo | Tipo | Descripción |
|---|---|---|
| `code` | `Char` | Código de regla: `VL-01`..`VL-13`. |
| `label` | `Char` | Nombre legible de la regla (ej: "Cédula / Datos del cliente"). |
| `passed` | `Boolean` | `True` si la validación fue aprobada. |
| `severity` | `Selection` | `error` = crítico bloqueante / `warning` = aviso informativo. |
| `detail` | `Char` | Texto explicativo devuelto por el motor OCR. |
| `status_icon` | `Char` (computed) | `✅` / `❌` / `⚠️` según `passed` y `severity`. |

---

### `project.ocr.document` — Documento OCR Genérico

Modelo base para análisis OCR sin reglas de negocio específicas.

| Campo | Tipo | Descripción |
|---|---|---|
| `name` | `Char` | Nombre del documento. |
| `document_type` | `Selection` | Tipo A / B / C (extensible). |
| `attachment_id` | `Many2one` → `ir.attachment` | PDF adjunto (domain: solo `application/pdf`). |
| `state` | `Selection` | `draft / processing / done / error`. |
| `has_signature` | `Boolean` | Firma detectada por el LLM o YOLO. |
| `has_fingerprint` | `Boolean` | Huella detectada por YOLO. |
| `has_date` | `Boolean` | Fecha encontrada por PaddleOCR. |
| `extracted_date` | `Char` | Valor(es) de fecha extraídos. |
| `fecha_word_count` | `Integer` | Ocurrencias de la palabra "fecha" en el texto. |
| `fecha_value_count` | `Integer` | Valores de fecha encontrados (patrones `dd/mm/yyyy`, etc.). |
| `firma_word_count` | `Integer` | Ocurrencias de la palabra "firma" en el texto. |
| `firma_detected_count` | `Integer` | Firmas detectadas por LLM visual o YOLO. |
| `analysis_details` | `Text` | JSON técnico interactivo con la predicción del LLM Ollama. |
| `validation_summary` | `Char` (computed) | Resumen visual: `✅ Firma \| ❌ Huella \| ✅ Fecha: 11/03/2026`. |

---

## Reglas de validación (VL-01 a VL-13)

Aplicadas sobre el texto OCR del expediente:

| Código | Nombre | Severidad | Qué verifica |
|---|---|---|---|
| VL-01 | Cédula / Datos del cliente | Error | Cédula panameña con formato válido (`N-NNNN-NNNN`). |
| VL-02 | Motivo de préstamo | Error | Campo "Motivo" presente y con valor legible. |
| VL-03 | Número de Seguro Social | Error | NSS presente. Busca labels `"No. Seguro Social"`, `"SEG.SOCIAL"`, etc. |
| VL-04 | Referencias bancarias y personales | Error | Ambas secciones presentes en el formulario. |
| VL-05 | Posición / Cargo | Error | Cargo del cliente detectado (para cotejo con carta de trabajo). |
| VL-06 | Rango salarial | Error | Monto o rango salarial presente en el formulario. |
| VL-07 | Lugar de nacimiento | Error | Provincia + País detectados. |
| VL-08 | Efectividades en órdenes de descuento | Warning | Campo de efectividad presente (puede no aplica según tipo). |
| VL-09 | Firma en cotización | Error | Al menos una firma detectada en el expediente. |
| VL-10 | Número de planilla | Error | Planilla presente para cotejo. |
| VL-11 | Longitud de dirección | Warning | Dirección residencial ≤ 120 caracteres. |
| VL-12 | Información de cónyuge | Warning | Si es casado/unido: verifica sección cónyuge. Si es soltero: `N/A`. |
| VL-13 | Proximidad huella-firma | Warning | Huella y firma no deben estar demasiado separadas en la página. |

---

## Vistas incluidas

| Archivo | Qué contiene |
|---|---|
| `views/loan_document_views.xml` | Form + List + botón «Validar» para `project.loan.document`. Los resultados VL-01..13 aparecen en una pestaña "Resultados de Validación" como lista con iconos. |
| `views/ocr_document_views.xml` | Form + List para `project.ocr.document`. Muestra firma/huella/fecha con los campos boolean e indicadores visuales. |

---

## Cómo heredar este módulo (guía IT)

Si necesitan integrar la validación en otro modelo (ej: `account.move`, `project.task`, `hr.employee`) **no modifiquen este módulo**. Creen un addon propio y hereden:

### Opción A — Heredar el modelo de préstamo directo

```python
# En su addon: models/mi_prestamo.py
from odoo import fields, models

class MiPrestamo(models.Model):
    _inherit = "project.loan.document"

    # Añadan sus campos propios
    sucursal_id = fields.Many2one("res.partner", string="Sucursal")
    oficial_id  = fields.Many2one("res.users",   string="Oficial de Crédito")
```

### Opción B — Añadir validación OCR a un modelo existente

```python
# En su addon: models/mi_solicitud.py
from odoo import api, fields, models

class MiSolicitudCredito(models.Model):
    _inherit = "mi.solicitud.credito"   # su modelo existente

    # Agregar el PDF y los resultados de validación
    pdf_file       = fields.Binary("Expediente PDF", attachment=True)
    pdf_filename   = fields.Char()
    validation_ids = fields.One2many(
        "project.loan.validation.line", "loan_id", readonly=True
    )
    loan_compliance = fields.Selection([
        ("conforme",    "✅ Conforme"),
        ("observado",   "⚠️ Observado"),
        ("no_conforme", "❌ No Conforme"),
    ], readonly=True)

    def action_validate(self):
        # Crear un LoanDocument temporal y programar la validación vía Cron Asíncrono
        loan = self.env["project.loan.document"].create({
            "name": self.name,
            "loan_type": "cies",
            "pdf_file": self.pdf_file,
            "pdf_filename": self.pdf_filename,
        })
        loan.action_start_ocr_validation()
        # NOTA: En este nuevo modelo asíncrono, los resultados se volcarán al
        # registro propio una vez el CRON background en Odoo termine. Mapear después.
```

### Opción C — Solo mostrar resultados en vista existente (heredar vista)

```xml
<!-- En su addon: views/mi_vista_heredada.xml -->
<record id="view_mi_solicitud_form_ocr" model="ir.ui.view">
    <field name="name">mi.solicitud.form.ocr</field>
    <field name="model">mi.solicitud.credito</field>
    <field name="inherit_id" ref="mi_addon.view_mi_solicitud_form"/>
    <field name="arch" type="xml">
        <xpath expr="//notebook" position="inside">
            <page string="Validación OCR" name="ocr_validation">
                <field name="loan_compliance" widget="badge"/>
                <field name="validation_ids">
                    <tree decoration-danger="not passed and severity == 'error'"
                          decoration-warning="not passed and severity == 'warning'">
                        <field name="status_icon"/>
                        <field name="code"/>
                        <field name="label"/>
                        <field name="detail"/>
                    </tree>
                </field>
            </page>
        </xpath>
    </field>
</record>
```

---

## Variables de entorno y configuración

| Variable / Constante | Dónde | Valor por defecto | Descripción |
|---|---|---|---|
| `LOAN_VALIDATE_URL` | `loan_document.py` | `http://ocr_engine:8000/api/v1/validate-loan` | Endpoint del microservicio para préstamos. |
| `OCR_ENGINE_URL` | `ocr_document_analysis.py` | `http://ocr_engine:8000/api/v1/analyze-pdf` | Endpoint genérico para FastOCR. |
| Timeout Web | `loan_document.py` | `3` segundos | Tiempo de disparo para encolar la tarea CRON de Validación de Expediente. |

> Para producción fuera de Docker, cambiar las URLs a la IP/hostname real del servidor del microservicio **NVIDIA GPU**.

---

## Requisitos

- **Odoo 16** (Community o Enterprise)
- **Python 3.10+**
- Microservicio OCR corriendo (ver `odoo_ocr_project/README` o `docker-compose.yml`)
- Dependencias Odoo: `base`, `project`, `mail`

---

## Instalación rápida

```bash
# 1. Copiar el addon al directorio de addons de Odoo
cp -r odoo_ocr_project/addons/project_ocr_validation /ruta/odoo/addons/

# 2. Levantar el microservicio OCR
cd odoo_ocr_project
docker compose up -d

# 3. En Odoo: Activar modo desarrollador → Actualizar lista de aplicaciones
#    → Instalar "Validación OCR de Documentos"
```

---

*Módulo desarrollado para Grupo Saleta. Para consultas técnicas, revisar `RELEASE_NOTES.md`.*
