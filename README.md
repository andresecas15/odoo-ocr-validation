# project_ocr_validation — Módulo Odoo 16

Módulo de **validación documental automatizada** para expedientes de préstamos Cíes, Ons y Ventanilla.  
Integra un microservicio Python (FastAPI + Ollama con GPU remota) con Odoo para analizar PDFs y aplicar 13 reglas de cumplimiento documental.

> **Nota para IT:** Este módulo es autocontenido y funcional tal cual, pero está pensado para que el equipo técnico lo **herede** en su propio addon (`_inherit`) y lo adapte al modelo de negocio (carpeta de crédito, project.task, account.move, etc.) sin modificar este módulo base.

---

## Arquitectura general y Flujo en GPU

```
Odoo (addon)                               FastAPI OCR Engine (172.17.0.33)
┌──────────────────────────────┐          ┌──────────────────────────────────┐
│  LoanDocument                │          │  FastAPI (ThreadPoolExecutor)    │
│  (project.loan.document)     │  HTTP    │  POST /api/v1/validate-loan      │
│  Llamada vía Cron Asíncrono  │ ───────► │                                  │
│                              │          │  Ollama (Qwen2.5-VL) → Inferencia│
│  ValidationLine              │ ◄─────── │  YOLOv8              → Visual    │
│  (×13 reglas VL)             │   JSON   │  validators.py       → Eval 13VL │
└──────────────────────────────┘          └──────────────────────────────────┘
```

La conversión de PDF a imágenes se realiza a **200 DPI** para garantizar legibilidad. Las páginas del expediente se procesan de forma concurrente mediante un pool de hilos (`ThreadPoolExecutor(max_workers=4)`) consumiendo el motor Ollama en el servidor GPU dedicado (`172.17.0.33` con RTX 5080) con paralelismo activo (`OLLAMA_NUM_PARALLEL=4`).

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

Evaluación lógica de cumplimiento documental sobre la salida del OCR:

| Código | Nombre | Severidad | Criterio de Aprobación y Ajustes Recientes |
|---|---|---|---|
| **VL-01** | Cédula / Datos del cliente | Error | Busca un formato de cédula panameña. Retorna únicamente la primera cédula del cliente principal (evita duplicados con fiadores o cónyuges). |
| **VL-02** | Motivo de préstamo | Error | Comprueba la presencia de la etiqueta "Motivo" y que posea valor escrito; filtra encabezados de tabla o firmas. |
| **VL-03** | Número de Seguro Social (NSS) | Error | Comprueba formato válido de NSS. Se permite y acepta el valor de nómina `0999-9999`. |
| **VL-04** | Referencias personales/bancarias | Error | Evalúa y reporta por separado la presencia de referencias bancarias y personales con sus respectivos campos. |
| **VL-05** | Posición / Cargo | Error | Aísla la ocupación laboral real del cliente en los datos laborales; remueve automáticamente prefijos y clasificaciones (ej: "Gobierno"). |
| **VL-06** | Rango salarial / Sueldo | Error | Busca montos o rangos salariales >= 100.00. Prioriza rangos explícitos (ej. "1200.01 - 1500.00"). |
| **VL-07** | Lugar de nacimiento | Error | Valida que contenga una provincia o comarca panameña oficial (ej: Chiriquí, Veraguas, Herrera, Los Santos). |
| **VL-08** | Efectividades en órdenes | Warning | Alerta de validación manual al oficial para confirmar que las efectividades encontradas no estén vencidas. |
| **VL-09** | Firma en cotización | Error | Si el expediente contiene cotización, verifica mediante YOLOv8 que posea al menos una firma manuscrita. |
| **VL-10** | Número de planilla | Error | Identifica formatos de planilla (ej: `P0800013010` o `8-21-06-0-01979`). Excluye cédulas de identidad del cliente. |
| **VL-11** | Longitud de dirección | Warning | Extrae la dirección *únicamente* de la página de solicitud principal. Requiere un mínimo de 10 caracteres (evita alucinaciones cortas) y un máximo recomendado de 120. |
| **VL-12** | Información de cónyuge | Error | Exclusivo para casados/unidos. Extrae nombre, cédula de formulario y datos laborales de la sección de cónyuge. Aplica descarte automático de textos dummy (`is_dummy_value`). |
| **VL-13** | Proximidad huella-firma | Warning | Mide la distancia en px entre firmas y huellas con YOLOv8. Si faltan firmas o huellas, la regla falla con severidad **Aviso** y detalla el elemento ausente. |

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
        loan = self.env["project.loan.document"].create({
            "name": self.name,
            "loan_type": "cies",
            "pdf_file": self.pdf_file,
            "pdf_filename": self.pdf_filename,
        })
        loan.action_start_ocr_validation()
```

---

## Variables de entorno y configuración

| Variable | Dónde | Valor por defecto | Descripción |
|---|---|---|---|
| `LOAN_VALIDATE_URL` | `loan_document.py` | `http://ocr_engine:8000/api/v1/validate-loan` | Endpoint del microservicio para préstamos. |
| `OCR_ENGINE_URL` | `ocr_document_analysis.py` | `http://ocr_engine:8000/api/v1/analyze-pdf` | Endpoint genérico para FastOCR. |
| Timeout Web | `loan_document.py` | `3` segundos | Tiempo de disparo para encolar la tarea CRON de Validación de Expediente. |

---

## Requisitos y Despliegue

- **Odoo 16** (Community o Enterprise)
- **Servidor Remoto GPU dedicado (172.17.0.33):**
  - Drivers de Nvidia instalados (`nvidia-driver-535` o superior).
  - `nvidia-container-toolkit` configurado para habilitar runtime GPU en Docker.
  - Puertos expuestos: 8000 (FastAPI OCR Engine) y 11434 (Ollama, local bound).

---

## Bitácora de Cambios e Hitos (21/06/2026)

### Configuración de GPU Dedicada y Corrección de Errores de API
* **Problema:** Errores continuos `400 Client Error: Bad Request` en la API de chat de Ollama. Cuelgues en el contenedor local por falta de memoria RAM/CPU al procesar PDFs extensos de forma secuencial.
* **Solución:** Migración del servicio de inferencia al host dedicado `172.17.0.33` provisto de una GPU Nvidia RTX 5080 (16GB VRAM). Configuración de Docker Compose reservando driver `nvidia` y recursos `gpu`. Sustitución de la SDK de OpenAI por consultas HTTP directas estructuradas vía la librería nativa `requests` de Python para anular conflictos de dependencias.

### Habilitación de Procesamiento Concurrente (ThreadPoolExecutor)
* **Problema:** Latencia excesiva en el análisis del expediente completo (más de 5 minutos por PDF), provocando timeouts en las peticiones HTTP desde Odoo.
* **Solución:** Configuración de la variable de entorno `OLLAMA_NUM_PARALLEL=4` en Ollama. Refactorización de `services.py` integrando concurrencia paralela mediante `ThreadPoolExecutor(max_workers=4)` para renderizar y enviar las páginas simultáneamente.
* **Resultado:** Reducción del tiempo de respuesta total a **54 segundos** para un expediente de 33 páginas (~0.8s por página).

### Restricciones de Contexto en Prompts y Mitigación de Alucinaciones
* **Problema:** El modelo de visión sufría de fuga de contexto y alucinaba nombres de referencias o firmas descritas en copias de cédula o reportes APC ajenos, asignándolos erróneamente en campos como Dirección Residencial o Cónyuge.
* **Solución:** Reescritura del prompt en `services.py` inyectando reglas de exclusión por página: la Dirección Residencial solo se extrae de la página con la cabecera `DATOS RESIDENCIALES`, devolviendo obligatoriamente `"NO ENCONTRADO"` en las demás páginas. Lo mismo se aplicó para cónyuge, restringiendo su búsqueda a la sección formal de `INFORMACIÓN DEL CÓNYUGE`.

### Refactorización y Saneamiento de Reglas en validators.py
* **is_dummy_value:** Implementación de un saneador en el backend que filtra de forma automática textos dummy del OCR (ej. `"NOT A PICTURED DOCUMENT"`, `"NO ENCONTRADO"`, `"NINGUNO"`).
* **normalize_ocr_text:** Normalización de texto crudo para minúsculas, remoción de acentos y unificación de errores tipográficos comunes en OCR (ej. unificar `0posicion` a `oposicion`).
* **VL-01 (Cédula):** Limpieza para retornar únicamente el primer número de cédula única para el cliente principal.
* **VL-05 (Cargo):** Exclusión de prefijos y clasificaciones de tipo de cliente para capturar la ocupación concreta.
* **VL-10 (Planilla):** Exclusión de cédulas y compatibilidad con formatos de nómina gubernamentales complejos (ej. `8-21-06-0-01979`).
* **VL-11 (Dirección):** Restricción de longitud mínima a 10 caracteres para evadir falsos positivos cortos.
* **VL-13 (Proximidad):** Modificación en la evaluación de YOLO para que si faltan firmas o huellas, la regla falle explícitamente (`passed=False`) reportando la alerta con severidad "Aviso", en lugar de retornar True.

---

*Módulo desarrollado para Grupo Saleta. Para consultas técnicas, revisar RELEASE_NOTES.md.*
