# Carpeta de Fuentes Personalizadas — NejoyT Nerdearla 2026

Coloca aquí cualquier archivo de tipografía en formato:
- `.woff2` (Recomendado para web y OBS)
- `.woff`
- `.ttf` (TrueType Font)
- `.otf` (OpenType Font)

El sistema (FastAPI y el frontend de `room.html` y OBS Overlay) detectará automáticamente las fuentes aquí alojadas a través del endpoint `/api/fonts`, registrará las reglas `@font-face` y las pondrá a disposición en el selector de estilos y subtítulos.
