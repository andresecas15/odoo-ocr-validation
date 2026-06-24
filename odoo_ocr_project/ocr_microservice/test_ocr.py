import os
import base64
import requests
from io import BytesIO
from pdf2image import convert_from_path

PDF_PATH = "/home/operador/Odoo_revision_documentacion_IA/odoo_ocr_project/PR07-5896.pdf"
if not os.path.exists(PDF_PATH):
    # Fallback si no está en esa ruta exacta en el host remoto
    PDF_PATH = "/app/PR07-5896.pdf"
    if not os.path.exists(PDF_PATH):
        # Intentar buscar en la raíz del proyecto
        PDF_PATH = "/app/ocr_microservice/PR07-5896.pdf"

print(f"Cargando PDF desde: {PDF_PATH}")
try:
    images = convert_from_path(PDF_PATH, dpi=200, fmt="jpeg")
    print(f"PDF cargado con {len(images)} páginas.")
except Exception as e:
    print(f"Error cargando PDF: {e}")
    images = []

LLM_BASE_URL = "http://ollama:11434/v1"
LLM_MODEL_NAME = "gemma4"

output_file = "/app/ocr_responses.txt"
with open(output_file, "w") as f:
    f.write("=== RESPUESTAS DE OCR OLLAMA ===\n\n")

for i in range(min(15, len(images))):
    print(f"Procesando página {i+1}...")
    pil_image = images[i]
    buffered = BytesIO()
    pil_image.save(buffered, format="JPEG", quality=90)
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    
    messages = [
        {
            "role": "system",
            "content": (
                "Eres un asistente experto en extracción de datos estructurados a partir de expedientes de crédito. "
                "Tu objetivo es leer la imagen y extraer EXACTAMENTE los campos solicitados utilizando el formato clave-valor. "
                "REGLAS CRÍTICAS:\n"
                "1. Busca exhaustivamente en toda la imagen (tablas, encabezados, letras pequeñas, casillas de verificación).\n"
                "2. NO inventes datos. Si un campo no está, escribe 'NO ENCONTRADO'.\n"
                "3. DEBES devolver tu respuesta respetando exactamente esta plantilla:\n\n"
                "Cedula: [valor]\n"
                "Motivo de Prestamo: [tipo de crédito o préstamo, ej. PRESTAMOS CIES]\n"
                "Numero de Seguro Social: [NSS si lo hay]\n"
                "Referencia Bancaria: [mencionar si está presente]\n"
                "Referencia Personal: [mencionar si está presente]\n"
                "Cargo o Posicion: [ej. Educador, Doctor, etc.]\n"
                "Rango Salarial o Salario: [Rango exacto o monto, ej. 1500.01 - 1800.00 o 1500.00]\n"
                "Lugar de Nacimiento: [Provincia visible, ej. Veraguas, Chiriqui, Panama]\n"
                "Efectividad: [mencionar si existe]\n"
                "Numero de Planilla: [valor]\n"
                "Estado Civil: [valor]\n"
                "Firmas: [Indica detalladamente SI detectas firmas manuscritas en la página y cuántas (Ej. SI, 2)]\n"
                "Texto Adicional: [breve resumen de campos extra que consideres útiles, como fechas o montos adicionales]"
            )
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text", 
                    "text": "Analiza la imagen y extrae la información completando estrictamente la plantilla solicitada. Presta mucha atención a las 'Referencias Bancarias/Personales' escritas en listas, a la 'Provincia' en el campo de nacimiento, al 'Motivo/Tipo de préstamo', y al 'Salario' en formato de moneda."
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{img_str}"
                    }
                }
            ]
        }
    ]
    
    payload = {
        "model": LLM_MODEL_NAME,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_predict": 2048
        }
    }
    
    try:
        response = requests.post(f"{LLM_BASE_URL}/chat/completions", json=payload, timeout=300)
        response.raise_for_status()
        data = response.json()
        page_text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        print(f"✓ Página {i+1} completada.")
        
        with open(output_file, "a") as f:
            f.write(f"--- PAGINA {i+1} ---\n")
            f.write(page_text + "\n\n")
    except Exception as e:
        print(f"Error página {i+1}: {e}")
        with open(output_file, "a") as f:
            f.write(f"--- PAGINA {i+1} (ERROR) ---\n")
            f.write(str(e) + "\n\n")

print(f"Proceso finalizado. Resultados guardados en: {output_file}")
