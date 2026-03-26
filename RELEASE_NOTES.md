# Release Notes — v16.0.1.1.0

**Fecha:** 10 de marzo de 2026

---

Después de una revisión más detallada del código, encontré un par de cosas que me estaban molestando y aproveché para limpiarlas. Nada crítico que haya tumbado el sistema, pero sí detalles que de no corregirlos, tarde o temprano iban a dar problemas.

## Qué se corrigió

### El resumen de validación mostraba siempre lo mismo

El campo `validation_summary` que aparece en la vista de cada documento nunca cambiaba, sin importar si se había detectado la firma, la huella o la fecha. El motivo era un error básico: la condición `if`/`else` tenía el mismo texto en ambas ramas. Le metí los indicadores ✅ y ❌ para que al menos de un vistazo sepas el estado real del documento sin tener que entrar a revisar cada campo por separado.

**Archivo:** `models/ocr_document.py`

---

### La URL del microservicio estaba definida dos veces

La constante `OCR_ENGINE_URL` aparecía en dos archivos distintos del addon, pero solo se usaba en uno. Es el típico copy-paste que no duele hasta que cambias la URL en un lado y se te olvida el otro. Quedó solo donde se necesita.

**Archivo:** `models/ocr_document.py`

---

### El aviso de YOLO faltante se perdía entre los logs

Cuando el archivo `best.pt` no existe en `models_ml/`, el microservicio arrancaba igualmente pero lo notificaba con una línea de log tan discreta que era fácil pasarla por alto. Cambié el mensaje para que sea imposible no verlo: un bloque bien delimitado con la ruta exacta donde hay que colocar el modelo y una aclaración de que el OCR de texto sigue funcionando sin él.

**Archivo:** `ocr_microservice/services.py`

---

### El microservicio solo atendía una solicitud a la vez

Uvicorn estaba configurado con un solo worker, lo que básicamente significa que si dos personas subían un PDF al mismo tiempo, una tenía que esperar a que terminara la otra. Con documentos pesados eso puede traducirse en timeouts del lado de Odoo. Subí a 4 workers. Un aviso para tener en cuenta: cada worker carga sus propios modelos en memoria, así que si el servidor tiene poca RAM, conviene ajustar ese número.

**Archivo:** `ocr_microservice/Dockerfile`

> **Fix posterior — crash al arrancar (HaltServer: Worker failed to boot)**
>
> Al levantar el contenedor con 4 workers, todos intentaban descargar y descomprimir los modelos de PaddleOCR al mismo tiempo, pisándose el archivo `.tar` entre ellos y terminando en un error fatal. Lo resolví en dos movimientos: primero cambié `services.py` para que, si PaddleOCR falla al cargar, el servicio avise y siga funcionando en lugar de reventar — el mismo patrón que ya usábamos con YOLO. Segundo, moví la descarga de los modelos al paso de `docker build`, de modo que quedan en la imagen y nunca se descargan en runtime. El primer build tarda un poco más, pero los arranques son limpios.
>
> **Archivos afectados:** `ocr_microservice/services.py`, `ocr_microservice/Dockerfile`

---

## Para aplicar los cambios del microservicio

El fix del `Dockerfile` requiere reconstruir la imagen **sin caché**:

```bash
docker compose build --no-cache ocr_engine
docker compose up -d
docker compose logs -f ocr_engine
```

El primer build tomará unos 3-5 minutos mientras descarga los modelos. A partir de ahí, cada reinicio del contenedor es inmediato.

El addon de Odoo no requiere reinstalación, pero si el registro ya tenía documentos con `validation_summary` guardado en caché, puede que quieras actualizar el módulo para refrescarlos.

---

*Eso es todo por esta versión. Pequeño pero necesario.*


---

# Release Notes — v16.0.1.2.0

**Fecha:** 11 de marzo de 2026

---

Sesión larga de refinamiento y corrección del motor de validación OCR. Se trabajó procesando múltiples contratos de ejemplo reales (PRES/0002, PRES/0003, PRES/0004) para sintonizar los regexes y la lógica de extracción de datos.

## Qué se corrigió / mejoró

### VL-09 — Firma en cotización: ahora muestra las páginas detectadas

La validación de firma pasó de devolver un simple `true/false` a indicar en qué páginas específicas del PDF se detectaron firmas mediante YOLO. El detalle ahora incluye algo como: `"Firmas detectadas en 3 página(s): [5, 6, 15]. Verificar que firma de oficial esté presente en la cotización. ✅"`.

**Archivos:** `ocr_microservice/services.py`, `ocr_microservice/main.py`, `ocr_microservice/validators.py`

---

### VL-12 — Información de cónyuge: fallback "N/A (Soltero)"

Cuando el estado civil del cliente es **Soltero/a**, la validación ahora retorna explícitamente `"N/A (Soltero). Sección de cónyuge no aplica para persona soltera."` en lugar del mensaje genérico anterior.

**Archivo:** `ocr_microservice/validators.py`

---

### VL-12 — Detección de estado civil basada en label+ventana

El matcher de estado civil dejó de buscar `casado/unido` en todo el texto del documento (lo que generaba falsos positivos en secciones de PEP u otros formularios incluídos en el expediente). Ahora localiza el **label "ESTADO CIVIL"** y evalúa solo los 80 caracteres siguientes. Adicionalmente, el regex acepta el truncamiento OCR `"ESTADO CIVI"` (sin la L final) que produce PaddleOCR en algunos formularios.

**Archivo:** `ocr_microservice/validators.py`

---

### VL-12 — Extracción de datos del cónyuge reescrita

Los tres regexes de extracción (`_CONYUGUE_NOMBRE_RE`, `_LABORA_BLOCK_RE`, `_NOMBRE_EMPRESA_CONYUGUE_RE`) fueron reescritos en base a la estructura real que produce PaddleOCR al leer la sección de cónyuge:

```
CEDULA/PASAPORTE NOMBRE COMPLETO TELEFONO ELABORA?
[8-769-2475] [YENI UMILDA GONZALEZ] [68511512] [SI] [NO] [X]
```

- **Nombre:** ahora busca la cédula del cónyuge seguida del NOMBRE en mayúsculas y su teléfono, en lugar de buscar el patrón `LABORA? + NOMBRE`.
- **¿Labora?:** acepta `ELABORA` (con E inicial que añade el OCR) y detecta el checkbox `SI NO X` / `X SI NO` con hasta 300 chars de distancia.
- **Empresa:** usa lookahead negativo para no capturar headers de sección vacíos (`REFERENCIAS`, `DATOS`, etc.).

**Archivo:** `ocr_microservice/validators.py`

---

### VL-03 — NSS: estrategia label-ventana con múltiples variantes

El regex de Número de Seguro Social dejó de ser un simple patrón numérico (que se confundía con la cédula). Ahora:

1. Busca el **label** en sus variantes: `"No. Seguro Social"`, `"Nro. Seguro Social"`, `"SEG.SOCIAL"`, `"Seguro Social"`.
2. **Itera todas las ocurrencias** del label en el texto, tomando la primera cuya ventana de 80 chars contenga un valor numérico válido (ignorando menciones del tipo "Recibo de Seguro Social" en checklists de documentos).

**Archivo:** `ocr_microservice/validators.py`

---

### VL-04 — Referencias: regex corregido a plural

El regex de referencias bancarias y personales solo coincidía con la forma singular (`"referencia bancaria"`). Los formularios usan el plural (`"REFERENCIAS BANCARIAS"`, `"REFERENCIAS PERSONALES"`), por lo que la validación siempre fallaba. Corregido con `referencias?` y `bancarias?`/`personales?`.

**Archivo:** `ocr_microservice/validators.py`

---

### Timeout del motor OCR aumentado a 300 segundos

Los expedientes de 18–22 páginas estaban causando timeouts (el límite anterior era 120 s). Subido a 300 s para que el OCR y el análisis YOLO terminen correctamente en documentos pesados.

**Archivo:** `addons/project_ocr_validation/models/loan_document.py`

---

## Para aplicar

Solo se necesita reiniciar el contenedor del motor OCR (no requiere rebuild de imagen):

```bash
docker compose restart ocr_engine
```

El addon de Odoo no requiere actualización.

---

*Muchos regex, mucho debug. El sistema ahora entiende mejor cómo PaddleOCR produce el texto de las tablas del formulario CIES.*

---

# Release Notes — v16.0.1.3.0

**Fechas:** 12 al 16 de marzo de 2026

---

**Gran refactorización analítica y estructural del Motor OCR:** Cambio de paradigma en la extracción de texto. Tras múltiples pruebas, PaddleOCR demostró ser ineficiente para el nivel de exactitud requerido, generando falsos positivos continuos, desorganizando los datos tabulares del formulario CIES, y perdiendo el contexto de lectura. Para solucionarlo, migramos la estrategia base a un modelo de IA Local Vision-Language (LLM) — **Ollama (MiniCPM-V)**.

Esto nos otorga control total, privacidad absoluta (los documentos no salen de sus servidores) y una comprensión semántica perfecta de los documentos escaneados.

> ⚠️ **Consideraciones de Hardware (Tiempos de Ejecución)**  
> Al ejecutar inferencia de IA localmente en el servidor actual (**Mi CPU**), el procesamiento de un expediente de crédito completo de 18 páginas toma **aproximadamente 2 horas**.  
> **Recomendación:** Para reducir este tiempo a tan solo **unos pocos minutos**, es imperativo dotar al nuevo servidor de hardware gráfico dedicado (**GPU NVIDIA**).

### Especificaciones de Hardware Recomendado (Servidor OCR / IA)
Para garantizar una inferencia fluida y tiempos de procesamiento óptimos (minutos en lugar de horas), el servidor de producción debe contar con:
- **Procesador (CPU):** 8 núcleos o más (ej. Intel Core i7/i9, AMD Ryzen 7/9, o equivalentes en servidor Xeon/EPYC).
- **Memoria RAM:** 32 GB mínimo (64 GB recomendado para manejar múltiples workers sin cuellos de botella).
- **GPU (Acelerador Gráfico):** Tarjeta NVIDIA con **mínimo 16 GB de VRAM** (ideal 24 GB+).  
  *Opciones de Consumo:* RTX 3090, RTX 4090.  
- **Almacenamiento:** Unidad SSD NVMe de 500 GB o superior. Los modelos de lenguaje pesan varios Gigabytes y requieren altas velocidades de lectura al cargarse en memoria.

### Instalación de Dependencias Previas en el Nuevo Servidor
Para que los contenedores de Docker (específicamente Ollama) puedan hacer uso de la GPU gráfica, no basta con instalar Docker. Se debe instalar el driver de NVIDIA y el **NVIDIA Container Toolkit** en el host (asumiendo Linux Ubuntu/Debian):

```bash
# 1. Actualizar repositorios e instalar drivers de la GPU NVIDIA
sudo apt update
sudo apt install -y nvidia-driver-535  # (o la versión recomendada para su hardware)

# 2. Configurar el repositorio del NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# 3. Instalar el toolkit
sudo apt update
sudo apt install -y nvidia-container-toolkit

# 4. Reiniciar Docker para que reconozca el runtime de NVIDIA
sudo systemctl restart docker
```

*(Nota: Una vez instalado esto, el archivo `docker-compose.yml` podrá asignar la GPU al contenedor de Ollama y el tiempo bajará drásticamente).*

## Qué se corrigió / mejoró

### 1. Manejo de Timeouts Asíncronos (Gunicorn & Odoo)

Debido al incremento exponencial en el tiempo de procesamiento (2 hrs) en CPU, ocurrieron bloqueos masivos en la arquitectura:
- **Gunicorn (Motor OCR):** Moría por `[CRITICAL] WORKER TIMEOUT`. Se reescribió la lógica en FastAPI para mover la inferencia de Ollama a un hilo en segundo plano (`threading.Thread`), permitiendo que el worker de Gunicorn quede libre mientras la IA procesa.
- **Odoo (HTTP Timeout):** El botón de validación dejaba la petición HTTP web colgada, lo que Odoo interrumpía tirando la conexión y revirtiendo la transacción de base de datos a su estado inicial. Se separó la acción en dos: un disparador rápido (`action_start_ocr_validation`) que programa el análisis OCR para ser ejecutado en segundo plano y asíncronamente por las acciones planificadas de Odoo (`ir.cron`). 

**Archivos afectados:** `ocr_microservice/main.py`, `addons/project_ocr_validation/models/loan_document.py`

---

### 2. Actualización de Expresiones Regulares (RegEx) para Markdown de IA

Las expresiones regulares heredadas en `validators.py` estaban calculadas asumiendo texto crudo proveniente de PaddleOCR. Como el LLM entrega resúmenes estructurados y listas viñeteadas (ej. `- TIPO CUENTA: EDUCADOR`), todas las validaciones fallaban internamente. 
Adaptamos el motor para soportar de forma nativa salidas de la IA en:
- **VL-01 (Cédula)**
- **VL-05 (Cargo/Posición)**
- **VL-06 (Salario)**
- **VL-07 (Lugar de Nacimiento)**
- **VL-10 (Planilla)**

**Archivos afectados:** `ocr_microservice/validators.py`

---

### 3. Ingeniería de Prompt (Alucinaciones y Omisiones de Modelo)

Inicialmente la flexibilidad conversacional de Ollama causó que el modelo "resumiera" la información a su criterio, omitiendo por completo los campos de Rango Salarial y Lugar de Nacimiento, y alucinando etiquetas falsas (ej: "MONTÁM"). 
Se reescribió el *System Prompt* instruyendo al modelo a operar como un motor OCR estricto: forzando la **transcripción literal, palabra por palabra y línea por línea**, prohibiendo la creación de categorías en Markdown u omisiones arbitrarias de datos financieros/personales.

**Archivos afectados:** `ocr_microservice/services.py`

---

### 4. Bucle Iterativo de Rango Salarial (VL-06)

Se identificaron bugs de lógica matemática donde un dígito suelto o fecha engañaban al motor OCR capturándose como "primer salario detectado", y fallando la prueba del monto mínimo `>= 100.00`.
- Se implementó un bucle `finditer()` que escanea recursivamente toda la página hasta dar con el salario real.
- Las particiones de formato numérico de 3 dígitos (ej. `150` de `1,500.00`) ahora capturan `int` gigantescos ilimitados que incorporen comas, decimales, y prefijos americanos y panameños ($ o B/.). 

**Archivos afectados:** `ocr_microservice/validators.py`

---

## Para aplicar

Aplica reinicios simultáneos tanto en el bloque de Odoo (por los cambios de CRON) como en la API OCR asíncrona:

```bash
docker compose restart odoo ocr_engine
```

*(El reinicio de los contenedores será inmediato, la descarga del modelo de Ollama ya se encuentra en caché global).*

---

# Release Notes — v16.0.1.4.0

**Fechas:** 17 al 20 de marzo de 2026

---

**Depuración final del motor de validaciones y documentación del proyecto.** Tras múltiples pruebas con expedientes reales (PRES/0018, PRES/0019), se identificaron y corrigieron falsos positivos/negativos en 6 validaciones. Adicionalmente, se creó el `README.md` completo del proyecto con guías de despliegue y herencia del módulo.

## Qué se corrigió / mejoró

### VL-03 — NSS: detección del label "No. Seguro Social"

El regex `_NSS_LABEL_RE` no coincidía con la variante `"No. Seguro Social:"` (con la preposición "de") que aparece en ciertos formularios CIES. Se amplió el patrón para aceptar `(?:de\s+)?` de forma opcional entre el número y "seguro social".

También se añadió soporte para la salida estructurada de Ollama que usa `"Numero de Seguro Social:"` como label.

**Archivo:** `ocr_microservice/validators.py`

---

### VL-05 — Cargo/Posición: filtro de valores falsos

Ollama ocasionalmente devolvía `"Rango"` como valor del cargo, confundiendo el label "Rango Salarial" adyacente con un cargo del solicitante. Se amplió la lista de valores rechazados (`"RANGO"`, `"NO"`, `"NO RANGO"`, `"N/A"`, etc.) para evitar que estos falsos positivos pasen como cargo válido.

También se aplicó un filtro anti-numérico: valores que son puramente números (ej. `"1992"`) ya no se aceptan como cargo.

**Archivo:** `ocr_microservice/validators.py`

---

### VL-06 — Rango Salarial: dos pasadas + validación de ambos montos

**Problema:** El sistema tomaba el primer número `>= 100` encontrado (ej. `1992`) y lo reportaba como salario, cuando el rango real `1200.01 - 1500.00` aparecía en otra sección del texto.

**Solución implementada:**

1. **Primera pasada (prioridad):** Buscar un **rango explícito** `X - Y` tanto en la salida de Ollama como en todas las ventanas del texto. Un rango siempre es preferido sobre un monto suelto.
2. **Segunda pasada (fallback):** Solo si no existe ningún rango, aceptar un monto simple `>= 100`.
3. **Validación de ambos montos:** Ahora AMBOS números del rango deben ser `>= 100` para ser aceptados. Esto elimina rangos absurdos como `1992 - 33.00` donde el segundo número claramente no es un salario.

**Archivo:** `ocr_microservice/validators.py`

---

### VL-07 — Lugar de Nacimiento: "Panamá" como provincia válida

Se agregó `"Panamá"` como provincia independiente válida en `_PROVINCIA_RE`. Anteriormente solo se aceptaba `"Panamá Oeste"` y la forma sola era rechazada, causando falsos negativos en personas nacidas en la capital.

**Archivo:** `ocr_microservice/validators.py`

---

### VL-11 — Dirección: soporte para "DATOS RESIDENCIALES" y "DIRECCIÓN RESIDENCIAL"

El regex `_DIRECCION_RE` solo coincidía con labels como `"DIRECCIÓN"` o `"DOMICILIO"`, pero los formularios CIES usan headers más específicos:

- `"DIRECCIÓN RESIDENCIAL"` → Ahora soportado con `(?:\s+residencial)?`
- `"DATOS RESIDENCIALES"` → Agregado como variante nueva del label

Esto permite que la dirección `"BARRIADA LA FORESTA A, CASA SIN NUMERO..."` sea correctamente detectada.

**Archivo:** `ocr_microservice/validators.py`

---

### VL-12 — Información de Cónyuge: anclas ampliadas

**Problema:** La validación dependía exclusivamente de encontrar `"Nombre Empresa"` mediante fuzzy matching como ancla de la sección de cónyuge. Si el OCR no producía ese texto exacto, la validación fallaba aunque el documento sí contenía datos del cónyuge.

**Solución:**

1. Se incorporó la detección de palabras clave como `cónyuge`, `esposo`, `esposa`, `matrimonio`, `pareja` en el texto. Si alguna existe, la validación no aborta por falta del ancla `"Nombre Empresa"`.
2. Se agregaron anclas alternativas via fuzzy matching: `"nombre del cónyuge"` y `"datos del cónyuge"`.
3. El mensaje de error ahora incluye el estado civil detectado para facilitar la depuración.

**Archivo:** `ocr_microservice/validators.py`

---

### Prevención de "spillover" entre campos

Se actualizaron los regexes de múltiples validaciones para usar **lookahead assertions** que cortan la captura cuando aparece el label del campo siguiente. Esto previene que, al normalizar el texto (eliminando saltos de línea), un campo capture el contenido del siguiente:

- `_MOTIVO_VALUE_RE` (VL-02)
- `_CARGO_OLLAMA_RE` (VL-05)
- `_NACIMIENTO_OLLAMA_RE` (VL-07)
- `val_referencias` (VL-04)
- `_SALARIO_OLLAMA_RE` (VL-06)

**Archivo:** `ocr_microservice/validators.py`

---

### Mejora en calidad de imagen para LLM

Se aumentó la resolución de conversión PDF → imagen de 150 DPI a **200 DPI**, y la calidad JPEG de 80 a **90**. Esto mejora significativamente la lectura de texto fino y tablas por parte de MiniCPM-V.

**Archivo:** `ocr_microservice/services.py`

---

### README.md del proyecto

Se creó la documentación completa del proyecto incluyendo:

- **Arquitectura** del sistema (diagrama de componentes)
- **Escenarios de despliegue:**
  - **Escenario A:** Odoo en servidor existente (nativo) + Ollama/OCR Engine en servidor dedicado
  - **Escenario B:** Todo en un solo servidor via Docker Compose
- **Guía de herencia** del módulo (modelos, vistas, menús, y validaciones custom)
- **Cómo ocultar el módulo del tablero** de aplicaciones Odoo al heredarlo
- **Tabla de las 13 validaciones**, requisitos de hardware, y troubleshooting

**Archivo:** `README.md`

---

## Para aplicar

```bash
# Reiniciar solo el motor OCR (validators.py se monta como bind mount):
docker compose restart ocr_engine
```

El addon de Odoo no requiere actualización para estos cambios.

---

# Release Notes — v16.0.1.5.0

**Fecha:** 25 de marzo de 2026

---

**Expansión de Tipos de Préstamo y Optimización de Rendimiento IA:** Se añadió soporte integral para un nuevo tipo de producto crediticio y se simplificó la arquitectura de IA deshabilitando modelos redundantes para salvaguardar el rendimiento (RAM/VRAM) del servidor.

## Qué se agregó / mejoró

### Soporte para "Préstamo Ventanilla"
Se agregó formalmente el **Préstamo Ventanilla** al ecosistema del OCR. 
- **Odoo:** Añadido `ventanilla` al campo `loan_type` de `project.loan.document`. El addon ahora transmite este tipo de documento al microservicio.
- **FastAPI:** El payload `AnalyzeRequest` recibe el `loan_type`. 
- **Lógica condicional:** Para Préstamo Ventanilla, las validaciones corren idénticas a Cíes/Ons, **excepto por VL-13 (Proximidad huella-firma)**, la cual se omite automáticamente y devuelve `passed=True` con severidad `warning`, pues no se requiere esta medida de seguridad para este producto.

**Archivos:** `addons/project_ocr_validation/models/loan_document.py`, `ocr_microservice/schemas.py`, `ocr_microservice/main.py`, `ocr_microservice/validators.py`.

---

### YOLOv8 Desactivado (Delegación total a LLM)
Para optimizar el rendimiento y disminuir cuellos de botella en procesamiento, **YOLOv8 fue desactivado por defecto**.
- Antes, YOLO consumía memoria leyendo la imagen en busca de las coordenadas (Bounding Boxes) de firmas y huellas para las validaciones VL-09 y VL-13.
- Ahora, como VL-13 ya no aplica a Ventanilla, **MiniCPM-V** asume directamente el análisis visual de las firmas. Se actualizó el **Prompt del Sistema** en `services.py` para obligar al LLM a indicar detalladamente si detecta firmas ("Firmas: SI/NO"). El conteo para VL-09 se alimenta ahora estrictamente del razonamiento multimodal del MiniCPM.

**Archivos:** `ocr_microservice/services.py`, `ocr_microservice/main.py`.

---

## Para aplicar

El código en Odoo fue modificado (`loan_document.py`); requiere **actualizar el módulo** en la interfaz. El contenedor OCR debe reiniciarse para aplicar el nuevo Prompt y apagar YOLO.

```bash
docker compose restart ocr_engine
```
