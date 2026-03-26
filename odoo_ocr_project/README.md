# 📄 Validación OCR de Documentos – Préstamos Cíes, Ons y Ventanilla

> **Odoo 16 · FastAPI · Ollama (MiniCPM-V) · YOLO v8**

Módulo de validación documental que analiza expedientes de préstamos mediante
Inteligencia Artificial. Extrae texto de documentos PDF con un LLM multimodal
(Ollama + MiniCPM-V) y opcionalmente YOLO v8, y ejecuta
**13 reglas de validación automática** contra los datos del expediente.

---

## Índice

1. [Arquitectura](#arquitectura)
2. [Requisitos del servidor](#requisitos-del-servidor)
3. [Despliegue paso a paso](#despliegue-paso-a-paso)
4. [Escenarios de despliegue](#escenarios-de-despliegue)
5. [Variables de entorno](#variables-de-entorno)
6. [Heredar y extender el módulo](#heredar-y-extender-el-módulo-en-otro-addon)
7. [Validaciones implementadas](#validaciones-implementadas-vl-01--vl-13)
8. [Estructura del proyecto](#estructura-del-proyecto)
9. [Troubleshooting](#troubleshooting)

---

## Arquitectura

```
┌──────────────┐      HTTP/JSON       ┌──────────────────┐    OpenAI API    ┌──────────────┐
│  Odoo 16     │ ──────────────────▶  │  OCR Engine      │ ─────────────▶  │  Ollama       │
│  (port 8069) │                      │  FastAPI :8000   │                  │  (port 11434)│
│              │ ◀────────────────── │  + YOLO v8       │ ◀───────────── │  MiniCPM-V   │
└──────┬───────┘    Validaciones      └──────────────────┘    Texto OCR     └──────────────┘
       │                                                                          ▲
       ▼                                                                          │
┌──────────────┐                                                    Puede estar en
│ PostgreSQL   │                                                    OTRO servidor
│  14          │                                                    con GPU
└──────────────┘
```

**Flujo:**
1. El usuario sube un PDF en Odoo → el módulo lo envía al microservicio OCR.
2. El OCR Engine convierte cada página a imagen y la envía a Ollama para
   extracción de texto estructurado y detección de firmas.
3. Se detectan huellas dactilares y conteo primario de firmas con YOLO (si está habilitado).
4. Se ejecutan 13 validaciones regex/fuzzy contra el texto extraído.
5. Los resultados se devuelven a Odoo y se muestran como un checklist.

---

## Requisitos del servidor

Los servicios (Odoo, Ollama, OCR Engine, PostgreSQL) corren de forma
simultánea. Los requisitos aseguran que no haya contención de recursos
cuando se procesan documentos mientras otros usuarios trabajan en Odoo.

### Escenario B — Todo en un solo servidor

| Componente            | Mínimo (funciona con lentitud)  | Recomendado (producción fluida)  |
|-----------------------|---------------------------------|----------------------------------|
| **RAM**               | 16 GB                           | 32 GB                            |
| **CPU**               | 8 cores / 16 threads            | 16 cores / 32 threads            |
| **Disco SSD**         | 50 GB libres                    | 100 GB SSD NVMe                  |
| **GPU** (Ollama)      | — (funciona en CPU, más lento)  | NVIDIA con 8+ GB VRAM (RTX 3060+)|
| **Docker**            | 20.10+                          | 24+                              |
| **Docker Compose**    | v2.0+                           | v2.20+                           |
| **SO**                | Ubuntu 20.04 / Debian 11        | Ubuntu 22.04 LTS                 |
| **Red**               | 100 Mbps                        | 1 Gbps                           |

**Distribución estimada de RAM (simultáneo):**

| Servicio       | RAM estimada   | Notas                                      |
|----------------|----------------|---------------------------------------------|
| PostgreSQL     | 1 – 2 GB       | Cache de BD + conexiones activas            |
| Odoo           | 2 – 4 GB       | Workers, sesiones de usuarios               |
| Ollama         | 4 – 6 GB       | MiniCPM-V cargado en memoria               |
| OCR Engine     | 2 – 4 GB       | YOLO + procesamiento de imágenes (4 workers)|
| **Total**      | **9 – 16 GB**  | Dejar margen para el SO (~2 GB)             |

### Escenario A — Servidores separados

**Servidor Odoo (existente):**

| Componente | Mínimo        | Notas                                    |
|------------|---------------|------------------------------------------|
| **RAM**    | 4 GB libres   | Solo el addon OCR + requests             |
| **Disco**  | 10 GB         | Almacenamiento de PDFs                   |
| **Red**    | 100 Mbps      | Comunicación HTTP con servidor IA        |

**Servidor IA (dedicado):**

| Componente        | Mínimo                   | Recomendado                       |
|-------------------|--------------------------|-----------------------------------|
| **RAM**           | 12 GB                    | 16 GB                             |
| **CPU**           | 4 cores                  | 8 cores                           |
| **Disco SSD**     | 30 GB libres             | 50 GB SSD                         |
| **GPU**           | — (CPU funciona)         | NVIDIA 8+ GB VRAM (RTX 3060+)    |
| **Docker**        | 20.10+                   | 24+                               |
| **SO**            | Ubuntu 20.04 / Debian 11 | Ubuntu 22.04 LTS                  |

> ⚡ **GPU vs CPU:** Con GPU (NVIDIA + CUDA), Ollama procesa cada página
> en ~2-3 segundos. Sin GPU (solo CPU), el mismo proceso toma ~15-30
> segundos por página. Para expedientes de 18+ páginas, la GPU reduce
> el tiempo total de ~9 minutos a ~1 minuto.

---

## Despliegue paso a paso

### 1. Clonar el repositorio

```bash
git clone https://github.com/gruposaleta/odoo-ocr-validation.git
cd odoo-ocr-validation
```

### 2. Configurar variables de entorno (opcional)

Las variables por defecto funcionan si Ollama corre en el mismo docker-compose.
Si Ollama está en otro servidor, ver [la sección dedicada](#ejecutar-ollama-en-otro-dispositivo-gpu-remota).

### 3. Copiar el modelo YOLO

Colocar los pesos entrenados de YOLO en:

```bash
mkdir -p ocr_microservice/models_ml
cp /ruta/a/best.pt ocr_microservice/models_ml/best.pt
```

> **Nota:** El modelo `best.pt` detecta dos clases: `0 = firma`, `1 = huella`.
> Si no se proporciona, las validaciones VL-09 y VL-13 se omiten.

### 4. Levantar los servicios

```bash
# Construir y arrancar todo en segundo plano
docker compose up -d --build
```

Esto levanta 4 contenedores:

| Servicio      | Puerto | Descripción                           |
|---------------|--------|---------------------------------------|
| `db`          | —      | PostgreSQL 14 (solo red interna)      |
| `odoo`        | 8069   | Odoo 16 ERP                           |
| `ollama`      | 11434  | Servidor LLM (MiniCPM-V)             |
| `ocr_engine`  | 8000   | Microservicio OCR + YOLO (FastAPI)    |

### 5. Descargar el modelo de Ollama

La primera vez, hay que descargar el modelo MiniCPM-V dentro del contenedor:

```bash
docker exec -it odoo_ocr_ollama ollama pull minicpm-v
```

> ⏱ Esto descarga ~5 GB. Solo se hace una vez; los datos persisten en
> el volumen `./ollama_data`.

### 6. Instalar el módulo de Odoo

1. Acceder a Odoo en `http://<IP_SERVIDOR>:8069`.
2. Ir a **Ajustes → Aplicaciones → Actualizar lista de aplicaciones**.
3. Buscar `Validación OCR de Documentos` e instalar.

### 7. Verificar el microservicio

```bash
curl http://localhost:8000/health
# Debe retornar: {"status": "ok", ...}
```

---

## Escenarios de despliegue

Existen dos escenarios típicos según la infraestructura existente.
Elegir el que aplique a tu caso:

| Escenario | Cuándo usarlo | Servicios en cada servidor |
|-----------|---------------|---------------------------|
| **A** – Servidor dedicado | Ya existe un servidor con Odoo instalado (sin Docker). No se puede dockerizar Odoo. | **Servidor Odoo:** Odoo (nativo) · **Servidor IA:** Ollama + OCR Engine (Docker) |
| **B** – Todo en uno | Se instala todo desde cero en un solo servidor, o Odoo se puede correr en Docker. | **Un solo servidor:** Odoo + PostgreSQL + Ollama + OCR Engine (todo Docker) |

---

### Escenario A — Servidor dedicado para Ollama + OCR Engine

> **Caso real:** Ya tienes Odoo instalado directamente en un servidor (sin Docker)
> y no puedes ni quieres meter Odoo dentro de un contenedor. Solo necesitas
> desplegar el microservicio OCR y Ollama en **otro** servidor que se comunique
> con Odoo vía HTTP.

```
SERVIDOR ODOO (existente)                 SERVIDOR IA (nuevo – dedicado)
┌──────────────────────┐                  ┌──────────────────────────┐
│ Odoo 16 (nativo)     │                  │ Ollama        :11434     │
│ PostgreSQL           │                  │ (MiniCPM-V)              │
│                      │                  │                          │
│ Módulo OCR ──────────┼───── HTTP ─────▶ │ OCR Engine    :8000      │
│ (addon en            │                  │ (FastAPI + YOLO)         │
│  /mnt/extra-addons)  │                  └──────────────────────────┘
└──────────────────────┘
```

#### Paso 1: Preparar el servidor IA

En el servidor dedicado (con o sin GPU):

```bash
# 1. Instalar Docker y Docker Compose
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable docker --now
sudo usermod -aG docker $USER  # Cerrar sesión y volver a entrar

# 2. Clonar el repositorio
git clone https://github.com/gruposaleta/odoo-ocr-validation.git
cd odoo-ocr-validation
```

#### Paso 2: Crear un docker-compose simplificado (sin Odoo ni PostgreSQL)

Crear un archivo `docker-compose.server-ia.yml` en el servidor IA:

```yaml
# docker-compose.server-ia.yml
# Solo levanta Ollama + OCR Engine (Odoo está en otro servidor)
services:
  ollama:
    image: ollama/ollama:latest
    container_name: odoo_ocr_ollama
    restart: unless-stopped
    ports:
      - "11434:11434"
    volumes:
      - ./ollama_data:/root/.ollama
    # Si el servidor tiene GPU NVIDIA, descomentar:
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - driver: nvidia
    #           count: all
    #           capabilities: [gpu]
    networks:
      - ocr_net

  ocr_engine:
    build:
      context: ./ocr_microservice
      dockerfile: Dockerfile
    container_name: odoo_ocr_engine
    restart: unless-stopped
    depends_on:
      - ollama
    ports:
      - "8000:8000"     # Debe ser accesible desde el servidor Odoo
    environment:
      - LLM_API_KEY=ollama
      - LLM_BASE_URL=http://ollama:11434/v1
      - LLM_MODEL_NAME=minicpm-v
    volumes:
      - ./ocr_microservice/models_ml:/app/models_ml
      # Bind mounts para hot-reload sin rebuild
      - ./ocr_microservice/validators.py:/app/validators.py:ro
      - ./ocr_microservice/services.py:/app/services.py:ro
      - ./ocr_microservice/main.py:/app/main.py:ro
      - ./ocr_microservice/schemas.py:/app/schemas.py:ro
      - ./ocr_microservice/config.py:/app/config.py:ro
    networks:
      - ocr_net
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s

networks:
  ocr_net:
    driver: bridge
```

#### Paso 3: Levantar los servicios en el servidor IA

```bash
# Construir y arrancar
docker compose -f docker-compose.server-ia.yml up -d --build

# Descargar el modelo (solo la primera vez, ~5 GB)
docker exec -it odoo_ocr_ollama ollama pull minicpm-v

# Verificar
curl http://localhost:8000/health
# → {"status": "ok"}
```

#### Paso 4: Copiar el modelo YOLO

```bash
mkdir -p ocr_microservice/models_ml
# Copiar los pesos entrenados al servidor
scp /ruta/local/best.pt usuario@servidor-ia:/ruta/al/repo/ocr_microservice/models_ml/
```

#### Paso 5: Instalar el addon de Odoo (en el servidor Odoo)

En el servidor donde ya está Odoo instalado de forma nativa:

```bash
# 1. Copiar solo la carpeta del addon al directorio de addons de Odoo
cp -r addons/project_ocr_validation /ruta/a/odoo/extra-addons/

# 2. Instalar la dependencia Python que necesita el addon (si aplica)
pip3 install requests

# 3. Reiniciar Odoo
sudo systemctl restart odoo
```

#### Paso 6: Configurar la URL del microservicio en Odoo

El módulo de Odoo necesita saber la IP del servidor IA.
En el modelo `ocr_document.py`, la URL del microservicio se configura como:

```python
# En el archivo del addon, buscar la URL del endpoint OCR y cambiarla:
OCR_ENGINE_URL = "http://192.168.1.100:8000"  # ← IP del servidor IA
```

> **Firewall:** Abrir el puerto `8000` en el servidor IA para que Odoo
> pueda conectarse. El puerto `11434` (Ollama) **no** necesita estar
> expuesto externamente si Ollama y OCR Engine están en el mismo servidor.

#### Paso 7: Verificar conectividad entre servidores

```bash
# Desde el servidor Odoo, probar que alcanza al microservicio:
curl http://192.168.1.100:8000/health
# → {"status": "ok"}
```

---

### Escenario B — Todo en un solo servidor (Docker Compose completo)

> **Caso real:** Se instala todo desde cero, o se puede dockerizar Odoo.
> Todos los servicios corren en el mismo servidor con el `docker-compose.yml`
> incluido en el repositorio.

```
UN SOLO SERVIDOR
┌─────────────────────────────────────────────┐
│ PostgreSQL :5432    (red interna)           │
│ Odoo       :8069    (acceso ext.)          │
│ Ollama     :11434   (red interna)          │
│ OCR Engine :8000    (red interna)          │
└─────────────────────────────────────────────┘
```

#### Paso 1: Clonar y levantar

```bash
git clone https://github.com/gruposaleta/odoo-ocr-validation.git
cd odoo-ocr-validation

# Copiar modelo YOLO
mkdir -p ocr_microservice/models_ml
cp /ruta/a/best.pt ocr_microservice/models_ml/

# Levantar todo
docker compose up -d --build
```

#### Paso 2: Descargar el modelo de Ollama

```bash
docker exec -it odoo_ocr_ollama ollama pull minicpm-v
```

#### Paso 3: Instalar el módulo

1. Ir a `http://<IP_SERVIDOR>:8069`.
2. **Ajustes → Aplicaciones → Actualizar lista de aplicaciones**.
3. Buscar `Validación OCR de Documentos` e instalar.

#### Paso 4: Verificar

```bash
curl http://localhost:8000/health
```

> En este escenario, todos los servicios se comunican por la red interna
> de Docker (`odoo_net`). No hace falta abrir puertos adicionales ni
> configurar IPs — todo funciona con los nombres de servicio
> (`http://ollama:11434`, `http://ocr_engine:8000`).

---

### Resumen comparativo

| Aspecto | Escenario A (separado) | Escenario B (todo en uno) |
|---------|------------------------|---------------------------|
| Docker en servidor Odoo | ❌ No necesario | ✅ Requerido |
| Compose file | `docker-compose.server-ia.yml` | `docker-compose.yml` |
| Servicios en servidor IA | Ollama + OCR Engine | — |
| Comunicación | HTTP entre servidores (abrir puerto 8000) | Red interna Docker |
| GPU | Opcional (mejora velocidad de Ollama) | Opcional |
| Escalabilidad | ✅ Mejor (cada servidor con sus recursos) | Comparten RAM y CPU |



---

## Variables de entorno

| Variable             | Default                        | Descripción                              |
|----------------------|--------------------------------|------------------------------------------|
| `LLM_BASE_URL`       | `http://ollama:11434/v1`       | URL base de la API de Ollama            |
| `LLM_MODEL_NAME`     | `minicpm-v`                    | Modelo de LLM a usar                    |
| `LLM_API_KEY`        | `ollama`                       | API key (Ollama no la requiere realmente)|
| `POSTGRES_USER`      | `odoo`                         | Usuario de PostgreSQL                    |
| `POSTGRES_PASSWORD`  | `odoo_secure_pwd_2024`         | **Cambiar en producción**               |
| `POSTGRES_DB`        | `odoo`                         | Nombre de la BD                          |

> ⚠️ **Producción:** Cambiar la contraseña de PostgreSQL y considerar usar
> un archivo `.env` o secretos de Docker.

---

## Heredar y extender el módulo en otro addon

El módulo `project_ocr_validation` está diseñado para ser heredado.
A continuación se muestra cómo crear un addon que lo extienda.

### Ocultar el módulo del tablero de aplicaciones

Por defecto, `project_ocr_validation` tiene `"application": True` en su
manifest, lo que hace que aparezca como app independiente en el tablero de
Odoo. Si se va a usar **únicamente como dependencia** de otro módulo,
se recomienda convertirlo en módulo técnico para que solo aparezca como
menú dentro de otra aplicación.

**Opción 1 — Cambiar el manifest del módulo base (si tienes control del repo):**

En `addons/project_ocr_validation/__manifest__.py`, cambiar:

```diff
-    "application": True,
+    "application": False,
```

Esto lo elimina del tablero de aplicaciones. Los menús siguen existiendo
pero ya no se muestra como app independiente.

**Opción 2 — Sobrescribir desde el addon hijo (sin tocar el módulo base):**

Si no se desea modificar el módulo base, el addon hijo puede ocultar y
reasignar los menús a otro padre. Crear un XML en el addon hijo:

```xml
<!-- views/ocr_menu_override.xml -->
<odoo>
    <!-- 1. Ocultar el menú raíz original del módulo OCR -->
    <record id="project_ocr_validation.menu_ocr_root" model="ir.ui.menu">
        <field name="active" eval="False"/>
    </record>

    <!-- 2. Crear un nuevo menú hijo bajo la app padre deseada -->
    <!--    Ejemplo: colocarlo dentro del menú de "Proyecto" -->
    <menuitem
        id="menu_ocr_under_project"
        name="Validación OCR"
        parent="project.menu_main_pm"
        sequence="30"
    />

    <!-- 3. Reasignar las acciones del módulo OCR al nuevo padre -->
    <menuitem
        id="menu_ocr_documents_reparented"
        name="Documentos OCR"
        parent="menu_ocr_under_project"
        action="project_ocr_validation.action_ocr_document"
        sequence="10"
    />
    <menuitem
        id="menu_loan_documents_reparented"
        name="Préstamos"
        parent="menu_ocr_under_project"
        action="project_ocr_validation.action_loan_document"
        sequence="20"
    />
</odoo>
```

Y registrarlo en el manifest del addon hijo:

```python
{
    "name": "Mi Validación OCR Personalizada",
    "version": "16.0.1.0.0",
    "depends": ["project_ocr_validation", "project"],
    "data": [
        "views/ocr_menu_override.xml",   # ← Primero reasignar menús
        "views/ocr_document_custom_views.xml",
    ],
    "installable": True,
    "application": True,  # El addon HIJO es la app visible
}
```

> **Resultado:** El módulo base `project_ocr_validation` ya no aparece en
> el tablero de aplicaciones. Sus funcionalidades quedan accesibles como
> submenú dentro de la aplicación padre (ej. Proyecto, Contabilidad, etc.).

### Estructura del addon hijo

```
mi_addon_custom/
├── __init__.py
├── __manifest__.py
├── models/
│   ├── __init__.py
│   └── ocr_document_custom.py
└── views/
    └── ocr_document_custom_views.xml
```

### `__manifest__.py`

```python
{
    "name": "Mi Validación OCR Personalizada",
    "version": "16.0.1.0.0",
    "depends": [
        "project_ocr_validation",  # ← Dependencia al módulo base
    ],
    "data": [
        "views/ocr_document_custom_views.xml",
    ],
    "installable": True,
}
```

### Heredar el modelo `ocr.document`

```python
# models/ocr_document_custom.py
from odoo import models, fields, api

class OcrDocumentCustom(models.Model):
    _inherit = "ocr.document"

    # Agregar campos nuevos
    custom_field = fields.Char("Campo personalizado")
    validated_by = fields.Many2one("res.users", string="Validado por")

    # Sobrescribir el método de análisis si se necesita lógica extra
    def action_analyze(self):
        """Extiende el análisis OCR con validaciones adicionales."""
        result = super().action_analyze()
        # Lógica personalizada post-análisis...
        self.validated_by = self.env.user
        return result
```

### Heredar el modelo `ocr.document.analysis`

```python
# models/ocr_document_analysis_custom.py
from odoo import models, fields

class OcrDocumentAnalysisCustom(models.Model):
    _inherit = "ocr.document.analysis"

    # Agregar campos extra al resultado de análisis
    custom_score = fields.Float("Score personalizado")
```

### Heredar la vista (XML)

```xml
<!-- views/ocr_document_custom_views.xml -->
<odoo>
    <record id="view_ocr_document_form_inherit_custom" model="ir.ui.view">
        <field name="name">ocr.document.form.inherit.custom</field>
        <field name="model">ocr.document</field>
        <field name="inherit_id" ref="project_ocr_validation.view_ocr_document_form"/>
        <field name="arch" type="xml">
            <!-- Agregar un campo después del campo existente -->
            <xpath expr="//field[@name='state']" position="after">
                <field name="custom_field"/>
                <field name="validated_by"/>
            </xpath>
        </field>
    </record>
</odoo>
```

### Modelos disponibles para herencia

| Modelo                     | Descripción                                | Campos clave                                     |
|----------------------------|--------------------------------------------|--------------------------------------------------|
| `ocr.document`             | Documento principal (PDF + estado)          | `name`, `state`, `pdf_file`, `analysis_ids`      |
| `ocr.document.analysis`    | Resultado individual de cada validación     | `document_id`, `code`, `label`, `passed`, `detail`|
| `loan.document`            | Documento de préstamo vinculado             | `partner_id`, `document_ids`, `loan_type`        |

### Agregar nuevas reglas de validación

Para agregar validaciones personalizadas al microservicio, editar
`ocr_microservice/validators.py`:

```python
# Al final de validators.py, agregar la función:
def val_mi_validacion(text: str) -> ValidationResult:
    """VL-14: Mi validación personalizada."""
    if re.search(r'(?i)mi_campo_requerido', text):
        return ValidationResult(
            code="VL-14",
            label="Mi validación",
            passed=True,
            severity="error",
            detail="Campo encontrado correctamente.",
        )
    return ValidationResult(
        code="VL-14",
        label="Mi validación",
        passed=False,
        severity="error",
        detail="No se encontró el campo requerido.",
    )

# Y registrarla en run_all_validations():
def run_all_validations(text, ...):
    results = [
        # ... validaciones existentes ...
        val_mi_validacion(text),  # ← Agregar aquí
    ]
```

---

## Validaciones implementadas (VL-01 – VL-13)

| Código  | Validación                       | Severidad     | Descripción                                                     |
|---------|----------------------------------|---------------|-----------------------------------------------------------------|
| VL-01   | Cédula / Datos del cliente       | Error Crítico | Verifica que la cédula aparezca en el documento                |
| VL-02   | Motivo de préstamo               | Error Crítico | Extrae y valida el motivo/tipo de préstamo                     |
| VL-03   | Número de seguro social (NSS)    | Error Crítico | Busca NSS en formato panameño (X-XXX-XXXX)                    |
| VL-04   | Referencias bancarias/personales | Error Crítico | Verifica presencia de ambas referencias                         |
| VL-05   | Posición / Cargo                 | Error Crítico | Extrae cargo laboral del solicitante                            |
| VL-06   | Rango salarial                   | Error Crítico | Detecta rango salarial (prioriza rangos X-Y sobre montos simples)|
| VL-07   | Lugar de nacimiento              | Error Crítico | Debe contener provincia panameña válida + país                  |
| VL-08   | Efectividades                    | Aviso         | Detecta campo de efectividad en órdenes de descuento           |
| VL-09   | Firma en cotización              | Aviso         | Verifica firma del oficial en cotización                        |
| VL-10   | Número de planilla               | Error Crítico | Valida formato de planilla (P0800013010 o N.PR08-3722)         |
| VL-11   | Longitud de dirección            | Aviso         | Alerta si dirección > 120 chars (truncamiento en contratos)    |
| VL-12   | Información de cónyuge           | Error Crítico | Si casado/unido, verifica datos obligatorios del cónyuge       |
| VL-13   | Proximidad huella-firma          | Aviso         | Evalúa cercanía entre firma y huella dactilar (YOLO)           |

---

## Estructura del proyecto

```
odoo_ocr_project/
├── docker-compose.yml              # Orquestación de servicios
├── addons/
│   └── project_ocr_validation/     # Módulo Odoo 16
│       ├── __manifest__.py
│       ├── models/
│       │   ├── ocr_document.py          # Modelo principal ocr.document
│       │   ├── ocr_document_analysis.py # Resultados de validación
│       │   └── loan_document.py         # Documento de préstamo
│       ├── views/
│       │   ├── ocr_document_views.xml
│       │   └── loan_document_views.xml
│       ├── data/
│       │   └── sequence_data.xml
│       └── security/
│           └── ir.model.access.csv
├── ocr_microservice/               # Microservicio FastAPI
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                     # Endpoints de la API
│   ├── services.py                 # Lógica de Ollama + OCR
│   ├── validators.py               # 13 reglas de validación
│   ├── schemas.py                  # Modelos Pydantic
│   ├── config.py                   # Variables de configuración
│   └── models_ml/
│       └── best.pt                 # Pesos YOLO (firma/huella)
└── ollama_data/                    # Volumen de modelos LLM
```

---

## Troubleshooting

### El OCR no detecta texto

```bash
# Verificar que Ollama responde:
docker exec -it odoo_ocr_ollama ollama list
# Debe mostrar: minicpm-v

# Probar la API directamente:
curl http://localhost:11434/api/tags
```

### El módulo no aparece en Odoo

```bash
# Verificar que los addons están montados:
docker exec -it odoo_ocr_web ls /mnt/extra-addons/
# Debe mostrar: project_ocr_validation/

# Reiniciar Odoo con actualización de lista:
docker compose restart odoo
```

### Error de memoria con Ollama

MiniCPM-V requiere ~4 GB de RAM. Si el servidor no tiene suficiente:

```bash
# Opción 1: Usar un modelo más ligero
docker exec -it odoo_ocr_ollama ollama pull llava:7b
# Y cambiar LLM_MODEL_NAME=llava:7b en docker-compose.yml

# Opción 2: Mover Ollama a un servidor con GPU
# (ver la sección "Ejecutar Ollama en otro dispositivo")
```

### Cambios en validators.py no surten efecto

Los archivos `.py` del microservicio están montados como bind mounts `:ro`.
Solo se necesita reiniciar el servicio:

```bash
docker compose restart ocr_engine
```

Si se modificó el `Dockerfile` o `requirements.txt`:

```bash
docker compose up -d --build ocr_engine
```

---

## Licencia

LGPL-3 · **Grupo Saleta** · [gruposaleta.com](https://www.gruposaleta.com)
