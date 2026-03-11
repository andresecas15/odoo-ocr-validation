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

---

---

### El microservicio reventaba al arrancar si no había internet (o no había descargado los modelos de PaddleOCR)

Este fue el más visible de los problemas recientes. Al levantar el contenedor, el servicio moría de inmediato con un error de gunicorn — `HaltServer: Worker failed to boot.` — y ni llegaba a responder peticiones.

La raíz del problema tenía dos partes. Primera: `services.py` estaba configurado para lanzar un `RuntimeError` si PaddleOCR no podía cargar, lo que mataba directamente al worker. Segunda: PaddleOCR descarga sus modelos de detección y reconocimiento de texto la primera vez que arranca, pero con múltiples workers activos (los 4 que configuramos en la mejora anterior), todos intentaban descargar y descomprimir el mismo archivo `.tar` al mismo tiempo, pisándose entre sí y dejando el archivo a medias.

Se corrigió en dos movimientos: primero, cambié el comportamiento de error de `services.py` para que en lugar de reventar, registre un aviso y siga trabajando con las capacidades que tenga disponibles — igual que ya lo hace con el modelo YOLO cuando no existe el archivo `best.pt`. Segundo, moví la descarga de modelos de PaddleOCR al momento del `docker build`, de modo que quedan incorporados directamente en la imagen. El primer build tarda un poco más, pero los arranques siguientes son limpios y sin dependencia de red.

**Archivos:** `ocr_microservice/services.py`, `ocr_microservice/Dockerfile`

---

## Para aplicar los cambios del microservicio (versión actualizada)

El fix del `Dockerfile` requiere reconstruir la imagen **sin caché** para que el paso de descarga de modelos se ejecute como corresponde:

```bash
docker compose build --no-cache ocr_engine
docker compose up -d
docker compose logs -f ocr_engine
```

El primer build tomará unos 3-5 minutos mientras descarga los modelos. A partir de ahí, cada reinicio del contenedor es inmediato.

---

*Eso es todo por esta versión. Pequeño pero necesario.*
