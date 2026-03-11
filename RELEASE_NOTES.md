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
