import re

text = "por lo que en señal de aceptación lo firman el 16 de febrero de 2026. Edición 20.06.2022"

DATE_REGEX = re.compile(
    r"\b(\d{1,2})\s*(?:[-/\.]|\s+de\s+)\s*([a-zA-Z]+|\d{1,2})\s*(?:[-/\.]|\s+(?:de|del)\s+)\s*(\d{2,4})\b",
    re.IGNORECASE
)

matches = DATE_REGEX.findall(text)
print(matches)

