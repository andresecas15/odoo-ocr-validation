# Lo que armé esta semana

**Fecha:** 10 de marzo de 2026

---

Llevaba tiempo sabiendo que los préstamos de Cíes y Ons tenían un problema muy concreto: las devoluciones. Pasan por el mismo proceso una y otra vez por errores que son siempre los mismos — un campo vacío por aquí, un formato incorrecto por allá, datos que no coinciden con la carta de trabajo. Nada dramático, pero suficiente para retrasar los desembolsos y generar fricción innecesaria tanto para el cliente como para el equipo.

Así que decidí atacarlo directo desde el sistema.

## En qué consistía el problema

Los oficiales revisan los expedientes manualmente antes de procesarlos. El volumen hace que sea difícil no dejar pasar cosas, y el problema es que los errores no son aleatorios — hay una lista bastante predecible de lo que falla: la cédula mal escrita, el motivo del préstamo que se queda en blanco, el NSS que no coincide con la carta de trabajo, las referencias que nadie llena, el lugar de nacimiento que ponen incompleto... En fin, los de siempre.

Mi razonamiento fue: si los errores son siempre los mismos, se pueden codificar. Y si se pueden codificar, el sistema puede avisarte antes de que el expediente llegue a revisión.

## Lo que hice

### En el microservicio (FastAPI / Python)

Creé un módulo nuevo llamado `validators.py`. Ahí viven las 13 reglas de validación, una función por cada error posible. Cada función recibe el texto que PaddleOCR extrajo del PDF, más algunos datos del modelo YOLO cuando aplica, y devuelve un resultado estructurado: código de la regla, si pasó o no, la severidad (error crítico o aviso), y un mensaje que explica exactamente qué falla y por qué importa.

Las reglas en orden:

| Código | Qué valida |
|--------|-----------|
| VL-01 | Cédula en formato válido (regex panameño) |
| VL-02 | Motivo de préstamo presente y con contenido |
| VL-03 | Número de Seguro Social con formato correcto |
| VL-04 | Referencias bancarias Y personales (ambas, no una sola) |
| VL-05 | Cargo/posición presente — afecta hoja de datos y Cocotito pág. 1 y 2 |
| VL-06 | Rango salarial con valor numérico visible |
| VL-07 | Lugar de nacimiento con Provincia + País, no solo la provincia |
| VL-08 | Campo de efectividad presente en órdenes de descuento |
| VL-09 | Firma de oficial en cotización (detectada por YOLO) |
| VL-10 | Número de planilla con guiones correctos (afecta F1-b) |
| VL-11 | Longitud de dirección dentro del límite (evita truncamientos en contratos) |
| VL-12 | Datos de cónyuge obligatorios si estado civil es casado o unido |
| VL-13 | Huella no alejada de la firma (usa coordenadas de bounding boxes YOLO) |

Para VL-13 tuve que modificar `services.py` y añadir una función nueva llamada `run_yolo_detailed` que, además de contar las detecciones, devuelve las coordenadas exactas de cada bounding box. Con eso calculo la distancia euclidiana entre el centro de la firma más cercana y el centro de la huella más cercana, y si superan los 600px (aproximadamente 3 cm a 200 DPI) se marca como aviso.

También extendí `schemas.py` con los tipos `ValidationItem` y `LoanValidationResponse`, y agregué un nuevo endpoint en `main.py`:

```
POST /api/v1/validate-loan
```

Mismo pipeline que el análisis general — decodifica, convierte a imagen, OCR, YOLO — pero al final pasa el texto completo y los resultados de YOLO por los 13 validadores y devuelve un JSON con el resultado detallado de cada regla, más un campo `loan_compliance` con tres posibles valores: `conforme`, `observado` o `no_conforme`.

La lógica de compliance es simple pero funcional: cero errores críticos = conforme; hasta 3 = observado; más de 3 = no conforme. Los avisos no afectan el compliance pero quedan registrados para que el oficial los revise.

### En el addon de Odoo

Creé dos modelos nuevos:

- **`project.loan.document`**: el registro principal del expediente. Tiene tipo de préstamo (Cíes u Ons), el adjunto PDF, el estado del proceso (Borrador → Procesando → Validado / Error) y los campos de resultado: compliance, conteo de errores y avisos.

- **`project.loan.validation.line`**: una línea por cada regla. Se crea después de la validación y forma un checklist legible en la interfaz.

En la vista de formulario de Odoo, después de correr la validación aparece una alerta visual en verde, amarillo o rojo según el resultado, y un tab con el checklist de las 13 reglas. Cada línea se colorea automáticamente: verde si pasa, rojo si falla como error crítico, amarillo si es aviso. El oficial puede ver de un vistazo qué falla y qué dice el detalle antes de decidir si devuelve el expediente o lo procesa.

Los mensajes de detalle de cada validación los escribí pensando en el oficial que los va a leer, no en el desarrollador. No dicen "regex no encontrado", dicen cosas como *"Estado civil casado/unido pero NO se detectaron datos de cónyuge. Campo obligatorio — debe completarse antes de procesar el préstamo."*

## Consideraciones

Algunas de estas validaciones son checks de presencia — si el campo está o no está — porque el OCR extrae texto plano y no puede cruzar datos entre documentos. Comparar el nombre exacto del cliente contra la cédula, por ejemplo, o verificar que el cargo coincide con la carta de trabajo, son cosas que requieren o acceso al sistema de registros o un modelo más especializado. Lo que sí puede hacerse con texto plano es detectar si el campo existe, si tiene contenido y si el formato es el correcto.

La efectividad (VL-08) la dejé como aviso y no como error porque el OCR puede detectar que el campo está presente, pero no puede determinar si la fecha está vencida. Eso sigue siendo revisión manual, pero al menos el sistema te recuerda mirarlo.

## Para ponerlo a funcionar

El addon en Odoo no requiere reinstalación completa. Con actualizar el módulo desde Configuración → Módulos debería ser suficiente para cargar los nuevos modelos y vistas.

El microservicio sí necesita rebuildearse porque tiene código nuevo:

```bash
docker compose build ocr_engine
docker compose up -d
```

---

*Esto es lo que entregué. Ahora el sistema puede decirte qué tiene mal un expediente antes de que llegue a ser un problema.*
