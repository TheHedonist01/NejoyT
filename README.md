# NejoyT: Subtitulado y Transcripción en Vivo para Nerdearla 2026

Sistema open source de reconocimiento de voz en streaming y traducción simultánea para conferencias técnicas con múltiples salas.

## Características Principales
- **Gemini 3.5 Transcribe Live:** Transcripción de bajísima latencia vía WebSocket utilizando `google-genai`.
- **Rotación con Solape (Anti-10 min):** Superación transparente del límite de 10 minutos por sesión mediante doble conexión y conmutación sin huecos ni palabras duplicadas.
- **Normalización Universal con FFmpeg:** Ingesta estandarizada para micrófono, archivos locales, listas HLS, streams RTMP y YouTube.
- **Glosario Técnico:** Sesgo de vocabulario especializado (`custom_vocabulary`) en modo `SMART` para preservar nombres de tecnologías, speakers y herramientas.
- **Traducción Eficiente en Capa Gratuita:** Traducción semántica sobre oraciones finales completas (`input_transcription`) con Gemini 2.5 Flash, optimizando cuota y contexto.
- **Multi-sala:** Soporte concurrente para conferencias con múltiples escenarios en paralelo.
- **Frontend Accesible y Overlay OBS:** Interfaz web ligera con opciones de alto contraste, tamaño de fuente ajustable y modo overlay transparente para transmisión.

## Licencia
Licenciado bajo Apache 2.0. Ver [LICENSE](LICENSE) para más detalles.
