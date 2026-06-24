# project_ocr_validation — Módulo Odoo 16

Módulo de **validación documental automatizada** para expedientes de préstamos Cíes, Ons y Ventanilla de Grupo Saleta. Integra el análisis multimodal de visión y lenguaje con Odoo 16 para evaluar el cumplimiento de 13 reglas de negocio.

---

## 1. Bitácora de Cambios

### 24/06/2026 — Inferencia Multimodal Directa (Qwen2.5-VL), Detección de Huellas Leves y Sustitución de YOLO
* **Descripción del problema:** La detección de firmas y huellas dactilares mediante el modelo YOLOv8 (`best.pt`) requería un re-entrenamiento constante de bounding boxes y presentaba limitaciones severas para discernir el contexto semántico de los trazos (ej. determinar de quién es cada firma, asociar una huella específica al deudor o identificar firmas de oficiales/gerentes en áreas de aprobación). Adicionalmente, las huellas dactilares muy tenues presentes en las páginas de firma del contrato (págs. 27, 29, 31 y 33 del expediente `PR07-5896.pdf`) no eran detectadas debido a la baja resolución de entrada en el OCR, y en páginas extensas de contrato el bloque de chequeos visuales (anteriormente colocado al final) se truncaba por límites de tokens del LLM.
* **Acciones técnicas ejecutadas:**
  * **Migración a Qwen2.5-VL:** Se evaluaron múltiples modelos multimodales (incluyendo `gemma4` y `granite3.2-vision`), optando por **Qwen2.5-VL** por su superior precisión de OCR y análisis visual de elementos gráficos.
  * **Sustitución de YOLO:** Se eliminó el uso de YOLO en producción. Ahora, el System Prompt en [services.py](file:///home/aecas/Documentos/GrupoSaleta/odoo_ocr_project/ocr_microservice/services.py) instruye al LLM a realizar un análisis espacial en cada página previo a la transcripción literal, devolviendo metadatos estructurados (`Firma Cliente`, `Huella Cliente`, `Relacion Firma Huella`, `Firma Oficial`).
  * **Detección de Huellas Leves:** Se incrementó la resolución máxima de imágenes de `448px` a `1024px` en [services.py](file:///home/aecas/Documentos/GrupoSaleta/odoo_ocr_project/ocr_microservice/services.py) para no perder trazos tenues.
  * **Reordenación de Prompt (Prevención de Truncamiento):** Se colocó el bloque `--- CHEQUEOS VISUALES ---` al inicio de las respuestas del modelo.
  * **Paginación Exacta:** Se implementó el separador de página único `\n\n--- PAGE_SEPARATOR ---\n\n` para unir las páginas en [services.py](file:///home/aecas/Documentos/GrupoSaleta/odoo_ocr_project/ocr_microservice/services.py), corrigiendo el conteo erróneo de páginas en [main.py](file:///home/aecas/Documentos/GrupoSaleta/odoo_ocr_project/ocr_microservice/main.py) (que confundía saltos de párrafo con páginas físicas, reportando 419 en lugar de 33).
  * **Ajuste VL-13 (Proximidad):** Se flexibilizó `val_proximidad_huella_firma` en [validators.py](file:///home/aecas/Documentos/GrupoSaleta/odoo_ocr_project/ocr_microservice/validators.py) para priorizar los aciertos de proximidad sobre falsas alertas de lejanía provocadas por firmas de oficiales en la misma página.
  * **Caché OCR Persistente Segmentada:** Se modificó `models_ml/ocr_text_cache.json` en [main.py](file:///home/aecas/Documentos/GrupoSaleta/odoo_ocr_project/ocr_microservice/main.py) y [services.py](file:///home/aecas/Documentos/GrupoSaleta/odoo_ocr_project/ocr_microservice/services.py) para segmentar las entradas por modelo activo (`{model_name}_{file_hash}`), previniendo la mezcla de datos y reduciendo el tiempo de procesamiento de ~3 minutos a **1.7 segundos** en hits de caché.
  * **Extracción Resiliente de Cónyuge (VL-12):** Se implementó una lógica de fallback por proximidad de secciones en [validators.py](file:///home/aecas/Documentos/GrupoSaleta/odoo_ocr_project/ocr_microservice/validators.py) que delimita la búsqueda entre el final de los datos laborales (`mover fondos publicos`) y el comienzo de las `referencias personales`, resolviendo omisiones de cabecera y errores tipográficos comunes en el OCR como `codua` en la cédula del cónyuge.

### 21/06/2026 — Configuración de GPU Dedicada y Corrección de Errores de API
* **Descripción del problema:** Ocurrencia del error `400 Client Error: Bad Request` de forma constante en el endpoint de chat de Ollama. Cuelgues sistemáticos del contenedor local por agotamiento de memoria RAM/CPU al procesar de forma secuencial múltiples imágenes de alta resolución provenientes de PDFs de gran tamaño.
* **Acciones técnicas ejecutadas:**
  * Migración del motor de inferencia al servidor remoto `172.17.0.33` provisto de una GPU Nvidia RTX 5080 (16GB VRAM).
  * Edición del Docker Compose para integrar las reservas del driver `nvidia` y la API de GPU en los contenedores `ollama` y `ocr_engine`.
  * Parcheo de dependencias de python en la inicialización: se removió el cliente de OpenAI debido a conflictos de versiones con `httpx` y se implementaron peticiones JSON planas usando la librería nativa `requests` de Python.

### Habilitación de Procesamiento Concurrente (ThreadPoolExecutor)
* **Descripción del problema:** Latencia de procesamiento de expedientes excesiva (más de 5 minutos para un PDF de 33 páginas), lo cual superaba los límites de tiempo de transacción (timeouts) en Odoo.
* **Acciones técnicas ejecutadas:**
  * Inyección de la variable de entorno `OLLAMA_NUM_PARALLEL=4` para habilitar la cola de procesamiento en paralelo interna en Ollama.
  * Refactorización de `services.py`: Sustitución del ciclo imperativo `for` por un pool de hilos usando `ThreadPoolExecutor(max_workers=4)`, analizando en paralelo las páginas del documento.
  * Control de excepciones por hilo para asegurar que el fallo aislado en una página no detenga la extracción de texto del resto del expediente.
* **Resultado:** El tiempo de procesamiento total para un PDF de 33 páginas bajó de 300 segundos a **54 segundos** (~0.8s por página).

### Restricciones de Contexto en Prompts y Mitigación de Alucinaciones
* **Descripción del problema:** El modelo de visión sufría de fuga de contexto y alucinaba nombres de referencias o firmas descritas en copias de cédula o reportes APC ajenos, asignándolos erróneamente en campos como Dirección Residencial o Cónyuge.
* **Acciones técnicas ejecutadas:**
  * Se actualizó el System Prompt en `services.py` definiendo **reglas explícitas de exclusión por página**: se le instruye formalmente al modelo que la Dirección Residencial solo debe extraerse de la página con la cabecera `DATOS RESIDENCIALES`, devolviendo obligatoriamente `"NO ENCONTRADO"` en cualquier otra página del PDF.
  * Se aplicó la misma regla restrictiva al cónyuge, anclando su extracción a la sección formal de `INFORMACIÓN DEL CÓNYUGE` de la página de solicitud principal.
  * Afinamiento del prompt de cónyuge para obligar a extraer la cédula escrita a mano en la tabla (`8-724-379`), mitigando el error de buscar el documento físico de cédula.

### Refactorización y Limpieza de Algoritmos en validators.py
* **Descripción del problema:** El parser de Python carecía de mecanismos para sanear las respuestas del LLM, reportando como válidos textos dummy del OCR o confundiendo cédulas con planillas y tipos de cliente con posiciones.
* **Acciones técnicas ejecutadas:**
  * **Función is_dummy_value:** Se implementó un algoritmo centralizado de filtrado en el backend que identifica y anula respuestas dummy (ej. `"NOT A PICTURED DOCUMENT"`, `"NO ENCONTRADO"`, `"NINGUNO"`, `"N/A"`).
  * **Normalización OCR:** Creación de `normalize_ocr_text` para unificar minúsculas, remover acentos y corregir errores tipográficos conocidos en la salida del OCR (ej. unificar `0posicion` a `oposicion`).
  * **Ajuste VL-01 (Cédula):** Se limita el reporte a la primera cédula encontrada para evitar incluir cédulas secundarias detectadas en copias adjuntas.
  * **Ajuste VL-05 (Cargo):** Se limpiaron prefijos de cliente ("Gobierno", "Privado", "Independiente") para capturar la ocupación concreta.
  * **Ajuste VL-10 (Planilla):** Soporte de planilla de colaboradores gubernamentales (`8-21-06-0-01979`) y exclusión explícita de cédulas dentro del array de candidatos de planilla.
  * **Ajuste VL-11 (Dirección):** Se configuró un límite de longitud mínima de 10 caracteres para evadir falsos positivos cortos como nombres o firmas de páginas secundarias.
  * **Ajuste VL-13 (Proximidad):** Se reestructuró la evaluación de YOLO: si no se detectan firmas o huellas, la validación falla explícitamente (`passed=False`) reportando la alerta con severidad "Aviso", en lugar de retornar True.

---

## 2. Arquitectura de Ejecución

El motor de validación se divide en tres componentes principales:

1. **Odoo 16 (Addon custom):** Gestiona la carga de expedientes y la cola de procesamiento en segundo plano (asíncrono vía cron).
2. **FastAPI Engine (`ocr_engine`):** Orquesta el flujo del análisis. Convierte PDFs a JPGs a 200 DPI y dispara los hilos de inferencia paralela. Ejecuta la lógica de validación definida en `validators.py`.
3. **Ollama GPU (`odoo_ocr_ollama`):** Carga el modelo vision-language **Qwen2.5-VL** utilizando Nvidia Docker Container Toolkit (CUDA) en el host `172.17.0.33`.
4. **YOLOv8 (`best.pt`):** Desactivado en producción en favor de la inferencia multimodal directa y contextual provista por Qwen2.5-VL (bypass de cajas delimitadoras).

---

## 3. Matriz de Reglas de Validación (13 VL)

Estas reglas se ejecutan de manera secuencial dentro del módulo `validators.py`:

| Código | Regla | Severidad | Lógica de Validación |
|---|---|---|---|
| **VL-01** | Cédula / Datos del cliente | Error | Valida formato de cédula panameña. Retorna únicamente la primera cédula del cliente principal. |
| **VL-02** | Motivo de préstamo | Error | Comprueba presencia del campo "Motivo" y valor escrito; filtra falsos positivos de firmas/nombres. |
| **VL-03** | Número de Seguro Social (NSS) | Error | Valida la estructura del NSS. Permite y acepta el valor de nómina `0999-9999`. |
| **VL-04** | Referencias | Error | Verifica por separado la existencia de referencias bancarias y personales. |
| **VL-05** | Posición / Cargo | Error | Aísla la ocupación del cliente, ignorando su tipo de cliente (ej. "Gobierno"). |
| **VL-06** | Rango salarial / Sueldo | Error | Comprueba montos >= 100.00. Prioriza rangos explícitos (ej. "1200.01 - 1500.00"). |
| **VL-07** | Lugar de nacimiento | Error | Valida que contenga una provincia o comarca panameña oficial. |
| **VL-08** | Efectividades | Warning | Alerta de presencia para control manual de vigencia de fechas. |
| **VL-09** | Firma en cotización | Error | Si el expediente posee sección de cotización, verifica que cuente con la firma del oficial. |
| **VL-10** | Número de planilla | Error | Valida formatos de planilla de cobro. Excluye números de cédula identificados. |
| **VL-11** | Longitud de dirección | Warning | Dirección residencial entre 10 y 120 caracteres. Restringida a la página de datos residenciales. |
| **VL-12** | Información de cónyuge | Error | Exige datos de cónyuge si es casado/unido. Filtra valores dummy (`is_dummy_value`). |
| **VL-13** | Proximidad huella-firma | Warning | Evalúa la proximidad y correspondencia entre firmas y huellas del deudor analizadas por Qwen2.5-VL. |

---

## 4. Variables de Entorno y Configuración

| Variable | Dónde | Valor por defecto | Descripción |
|---|---|---|---|
| `LLM_BASE_URL` | `docker-compose.yml` | `http://ollama:11434/v1` | URL base de la API de Ollama. |
| `LLM_MODEL_NAME` | `docker-compose.yml` | `qwen2.5vl` | Nombre del modelo multimodal utilizado. |
| `OLLAMA_NUM_PARALLEL` | `docker-compose.yml` | `4` | Cantidad de peticiones simultáneas que Ollama procesa en GPU. |
| `LOAN_VALIDATE_URL` | `loan_document.py` | `http://172.17.0.33:8000/api/v1/validate-loan` | Endpoint de validación de FastAPI en Odoo. |
