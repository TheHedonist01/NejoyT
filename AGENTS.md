# Directivas y Reglas de Desarrollo — Subtitulado Nerdearla 2026

## 1. Arquitectura del Sistema
El sistema realiza ingesta de audio, transcripción en tiempo real, traducción simultánea y emisión vía WebSocket para conferencias multi-sala.

```
┌──────────────┐
│  Fuente      │  micrófono / archivo / RTMP / HLS / YouTube
└──────┬───────┘
       │
┌──────▼───────────────────────────────┐
│  ffmpeg (subproceso)                 │  → PCM s16le 16 kHz mono (3.200 B / 100ms)
└──────┬───────────────────────────────┘
       │ Chunks de 100 ms (32.000 B/s)
┌──────▼───────────────────────────────┐
│  SessionWorker  (uno por sala)       │
│  ┌────────────────────────────────┐  │
│  │ ASRBackend                     │  │
│  │  ├ GeminiLiveASR (WS)          │  │  ← Rotación con solape cada ~9 min
│  │  └ LocalASR (Gemma4/Whisper)   │  │
│  └──────────┬─────────────────────┘  │
│             │ interim / final        │
│  ┌──────────▼─────────────────────┐  │
│  │ Traductor (Flash, con glosario)│  │  ← Solo traduce FINALES (cuota free tier)
│  └──────────┬─────────────────────┘  │
└─────────────┼────────────────────────┘
              │ eventos
┌─────────────▼────────────────────────┐
│  Event bus en memoria (asyncio)      │
└───┬──────────────┬───────────────┬───┘
    │              │               │
┌───▼────┐   ┌─────▼──────┐   ┌────▼─────┐
│ Web    │   │ Overlay    │   │ Archivo  │
│ audien.│   │ OBS        │   │ SRT/VTT  │
└────────┘   └────────────┘   └──────────┘
```

## 2. Advertencias Críticas de la API
1. **Límite de Sesión Live:** `gemini-3.5-transcribe-live` soporta un máximo de **10 minutos** continuos por WebSocket.
   - Es mandatorio el gestor de rotación con solape (`asr/rotation.py`): abre la sesión B a los 8:30 min, envía audio a ambas, y a los 9:00 min tras el primer `input_transcription` final, corta A y promueve B.
2. **Capa Gratuita:**
   - La API Key es de **capa gratuita**.
   - Solo se traduce el texto final (`input_transcription`), **nunca** cada hipótesis intermedia (`interim_input_transcription`), para proteger la cuota de RPM/TPM.
   - En pantalla los interinos se muestran en el idioma original, y la traducción al español se presenta al cerrar la frase.
3. **Sintaxis de google-genai:**
   - Modelo: `gemini-3.5-transcribe-live`
   - Config: `types.LiveConnectConfig(response_modalities=["TEXT"], input_audio_transcription=types.AudioTranscriptionConfig(mode="SMART", custom_vocabulary=[...]))`
   - Envío: `session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))`
   - Recepción: `server_content.interim_input_transcription.text` y `server_content.input_transcription.text`.

## 3. Convenciones de Código
- **Async de punta a punta:** Todo el flujo de datos (I/O, sockets, subprocesos) debe ser no bloqueante (`asyncio`).
- **Tipado estricto:** Type hints en todas las funciones y clases (`mypy` compliant).
- **Cero dependencias innecesarias:** No agregar paquetes sin justificación.
- **Frontend Vanilla:** HTML + JS vanilla sin build steps ni bundlers.

## 4. Estructura de Archivos
- `config.py`: Definición Pydantic de configuración de salas y entorno.
- `audio/source.py`: Ingestor único basado en subproceso de `ffmpeg` (produce PCM 16kHz s16le mono).
- `asr/base.py`: Clase abstracta `ASRBackend`.
- `asr/gemini_live.py`: Implementación cliente de `gemini-3.5-transcribe-live`.
- `asr/rotation.py`: Mecanismo de rotación sin fisuras para evadir el corte de 10 minutos.
- `translate/gemini_text.py`: Traductor batch de frases finales con glosario técnico.
- `bus.py`: Bus de eventos en memoria con `asyncio.Queue`.
- `store.py`: Almacén de transcripciones con timestamps para exportación SRT/VTT.
- `orchestrator.py`: Supervisor de N `SessionWorker` en paralelo.
- `main.py`: Aplicación FastAPI y endpoints de WebSocket/UI.
- `static/`: Frontend web accesible y responsive + vista overlay OBS.
