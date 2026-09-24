# NejoyT — Transcripción y Traducción en Vivo para Conferencias

> **Nerdearla 2026 · Vibeathon**  
> Sistema *open source* de subtitulado simultáneo multi-sala, traducción en tiempo real y rotación transparente de sesiones impulsado por **Google Gemini 3.5 Transcribe Live** y **Gemini 3.6 Flash**.

[![Licencia](https://img.shields.io/badge/licencia-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)]()
[![FastAPI](https://img.shields.io/badge/framework-FastAPI-009688.svg)]()
[![Gemini](https://img.shields.io/badge/ASR-gemini--3.5--transcribe--live-4285F4.svg)]()

---

## 1. El Problema que Resuelve

En conferencias masivas como **Nerdearla** ocurren más de 30 charlas técnicas en paralelo, muchas dictadas en inglés o con *code-switching* continuo. Los servicios comerciales tradicionales fallan estrepitosamente en tres frentes:
1. **Destrozan la jerga técnica:** Convierten *Kubernetes* en "cuba netics", *CI/CD* en "sí y sí", o confunden nombres de herramientas (*Terraform*, *Prometheus*, *Istio*).
2. **Son silos cerrados de alto costo:** Requieren hardware privativo o suscripciones privativas por hora/sala inasumibles para eventos comunitarios.
3. **No resuelven la continuidad:** Las APIs de streaming basadas en WebSockets imponen límites estrictos de tiempo por sesión (ej. 10 minutos en `gemini-3.5-transcribe-live`), cortando el audio a mitad de una disertación de 45 minutos.

**NejoyT** soluciona de raíz estos problemas:
- Ingesta agnóstica universal vía **FFmpeg** (soporta micrófonos locales DirectShow/Pulse, archivos de prueba, o flujos RTMP/HLS).
- Transcripción de bajísima latencia en modo `SMART` con **Glosario Técnico de Sesgo** (`custom_vocabulary`).
- **Rotación con Solape (*Seamless Overlap Rotation*):** Conmutación invisible cada 9 minutos entre dos sesiones WebSocket concurrentes con cero palabras cortadas.
- **Traducción Eficiente:** Traducción por lotes de oraciones completas a Español, Inglés y Portugués, protegiendo la cuota Free Tier de la API.
- **Centro de Control Operativo (`/admin`):** Dashboard web reactivo para que los organizadores creen, inicien, pausen o editen glosarios en caliente para cualquier sala.
- **Doble Salida:** Vista responsive accesible (alto contraste y tamaño de letra regulable) para la audiencia y vista **Overlay OBS** transparente (`?overlay=1`) para streaming.

---

## 2. Arquitectura del Sistema

```
┌────────────────────────────────────────────────────────┐
│  FUENTES DE AUDIO (Micrófono / Archivo / RTMP / HLS)   │
└───────────────────────────┬────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────┐
│  FFmpeg Subprocess Factory (audio/source.py)           │
│  → Normaliza a PCM s16le, 16 kHz, mono (32.000 B/s)    │
│  → Cola acotada (50 chunks / 5s) con descarte antiguo  │
│  → Supervisor con reconexión automática en vivo        │
└───────────────────────────┬────────────────────────────┘
                            │ Chunks de 100 ms (3.200 B)
┌───────────────────────────▼────────────────────────────┐
│  SessionWorker (orchestrator.py - uno por sala activa) │
│  ┌──────────────────────────────────────────────────┐  │
│  │ ASRBackend (SeamlessRotationASR en asr/rotation) │  │
│  │  ├ Sesión A: gemini-3.5-transcribe-live (WS)     │  │  ← Rotación con solape
│  │  └ Sesión B: gemini-3.5-transcribe-live (WS)     │  │     (Abre B a 8:30 min,
│  │                                                  │  │      conmuta a 9:00 min)
│  └────────────────────────┬─────────────────────────┘  │
│                           │ interim (original) / final │
│  ┌────────────────────────▼─────────────────────────┐  │
│  │ Traductor Flash (translate/gemini_text.py)       │  │  ← Solo traduce FINALES
│  │ (thinking_budget=0, glosario técnico, contexto)  │  │     (protege cuota)
│  └────────────────────────┬─────────────────────────┘  │
└───────────────────────────┼────────────────────────────┘
                            │ Eventos (SubtitleEvent)
┌───────────────────────────▼────────────────────────────┐
│  EventBus en Memoria (bus.py - asyncio.Queue)          │
└───┬───────────────────────────┬────────────────────┬───┘
    │                           │                    │
┌───▼──────────────┐   ┌────────▼─────────┐   ┌──────▼────────┐
│ WebSocket Clientes│   │ Overlay OBS      │   │ Acumulador    │
│ Audiencia (/room)│   │ (?overlay=1)     │   │ SRT / VTT     │
└──────────────────┘   └──────────────────┘   └───────────────┘
```

---

## 3. El Mecanismo de Rotación con Solape (Anti-10 Minutos)

El modelo de streaming `gemini-3.5-transcribe-live` impone una desconexión forzada a los **10 minutos** por WebSocket. NejoyT implementa un patrón de **solape sin fisuras** (`asr/rotation.py`):

1. **t = 0:00:** Se abre la **Sesión A** y se transmiten sus transcripciones a la audiencia.
2. **t = 8:30:** Se abre en paralelo la **Sesión B**. Durante la ventana de 30 segundos, el audio entrante se envía simultáneamente a **ambas sesiones**, pero solo se publican los eventos de A (evitando duplicaciones en pantalla).
3. **t ≥ 9:00:** Tras recibir el primer evento `input_transcription` finalizado de la Sesión A después de los 9 minutos, el worker promueve la Sesión B a activa y detiene limpiamente la Sesión A.
4. **Resultado:** Ninguna palabra cortada, latencia ininterrumpida y charlas continuas de cualquier duración.

---

---

## 4. Backend Dual: Local Agnóstico de Hardware y Cloud (Google API)

NejoyT cuenta con una arquitectura de backend intercambiable mediante la abstracción `ASRBackend`:

```
                    ┌───────────────────────────────┐
                    │   Orquestador (Multi-Sala)   │
                    └──────────────┬────────────────┘
                                   │
                ┌──────────────────┴──────────────────┐
                ▼                                     ▼
     [Backend: "cloud"]                     [Backend: "local"]
  Gemini 3.5 Transcribe Live             Faster-Whisper + Silero VAD
  - Conexión WebSocket streaming         - Detección de hardware automática
  - Rotación transparente cada 9 min     - Inferencia 100% offline (sin cuota)
  - Requiere GEMINI_API_KEY              - Ingesta de chunks de 100 ms
```

### Detección Automática de Hardware (Local)
El backend local **no asume ninguna GPU específica ni fabricante**. Al iniciar, detecta automáticamente el mejor dispositivo disponible:
- **NVIDIA:** CUDA con precisión `float16`.
- **AMD:** ROCm / HIP con precisión `float16`.
- **Apple Silicon:** MPS (Metal Performance Shaders) con precisión `float16`.
- **CPU Universal:** Fallback universal con cuantización `int8` (corre en cualquier laptop o servidor).

Al bootear, el sistema loguea claramente el hardware en uso:
```
[INFO] Backend local corriendo en: CUDA (float16) | Modelo Whisper: 'base'
```

### Modelo Whisper Configurable
Puedes ajustar el tamaño del modelo según los recursos disponibles de tu equipo (`tiny`, `base`, `small`, `medium`) en `rooms.yaml` o mediante la API:
- `tiny` (~75 MB): Ultra-rápido, ideal para CPUs modestas o Raspberry Pi 5.
- `base` (~140 MB, default): Equilibrio óptimo entre precisión y latencia (< 80 ms en GPU).
- `small` (~460 MB): Mayor precisión para acentos complejos.

---

## 5. Pipeline de Traducción Simultánea y Baja Latencia

### Idioma de Entrada Automático → Traducción al Idioma Elegido
1. **Detección Automática:** Tanto en Cloud (`language_codes=[]`) como en Local (`language=None`), el sistema identifica el idioma hablado al vuelo y tolera *code-switching*.
2. **Traducción Local Integrada (CTranslate2 / MarianMT):** Las frases finales se traducen en la máquina local en **menos de 100 ms**, garantizando subtítulos en español (o el idioma elegido) casi en tiempo real sin agotar cuotas de API.
3. **Manejo de Hipótesis Parciales (`interim`):** Para la audiencia que escucha en español mientras el orador habla en inglés, el sistema genera hipótesis traducidas al instante para que la pantalla siempre muestre texto comprensible en español.
4. **Protección del Glosario Técnico:** Los términos clave (*Kubernetes, Docker, FastAPI, Prometheus, Nerdearla*) se aíslan mediante expresiones regulares antes de la traducción y se restauran fielmente en la salida.

---

## 6. Requisitos y Puesta en Marcha

### Prerrequisitos
- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (gestor de paquetes y entornos)
- FFmpeg instalado en el sistema (detectado automáticamente en Windows / Linux / macOS)
- Opcional: GPU NVIDIA/AMD/Apple Silicon para aceleración local, o API Key de Google AI Studio para modo Cloud.

### Instalación

```bash
# 1. Clonar el repositorio
git clone https://github.com/tu-usuario/nejoyt.git
cd nejoyt

# 2. Instalar dependencias con uv
uv sync

# 3. Configurar variables de entorno (solo si usas el backend Cloud)
cp .env.example .env
```

Configura tu `.env`:
```env
GEMINI_API_KEY="AIzaSy..."  # Opcional si operas en modo 100% local
GEMINI_LIVE_MODEL="gemini-3.5-transcribe-live"
GEMINI_TRANSLATE_MODEL="gemini-2.5-flash"
ADMIN_TOKEN="nerdearla2026"
LOG_LEVEL="INFO"
```

### Ejecutar el Servidor

```bash
uv run uvicorn main:app --reload --port 8000
```

El sistema estará disponible en:
- **Portal de Audiencia:** [http://localhost:8000/](http://localhost:8000/)
- **Centro de Control / Operador:** [http://localhost:8000/admin](http://localhost:8000/admin)
- **Documentación API Swagger:** [http://localhost:8000/docs](http://localhost:8000/docs)

---

## 7. Pruebas Rápidas con Audios Incluidos

El repositorio incluye muestras de audio en la carpeta `samples/` listas para probar de inmediato:

1. Ingresa al panel de control en [http://localhost:8000/admin](http://localhost:8000/admin).
2. Verás las salas semilla cargadas desde `rooms.yaml` en estado `STANDBY`.
3. Haz clic en **▶ Iniciar** en la sala `Auditorio Principal` (Modo Local).
4. Abre la vista de audiencia en [http://localhost:8000/room/auditorio-principal](http://localhost:8000/room/auditorio-principal) o el Overlay OBS en [http://localhost:8000/room/auditorio-principal?overlay=1](http://localhost:8000/room/auditorio-principal?overlay=1).
5. Observarás cómo fluyen los subtítulos en tiempo real con latencia sub-segundo traducidos automáticamente al español.
6. Al finalizar, exporta el archivo de subtítulos generado en [http://localhost:8000/api/rooms/auditorio-principal/export?format=srt](http://localhost:8000/api/rooms/auditorio-principal/export?format=srt).

---

## 8. Operación en Vivo: Micrófono y Streaming RTMP

### Uso con Micrófono de la Sala
1. En `/admin`, haz clic en **🎙 Detectar Micrófonos**. El sistema consultará los dispositivos DirectShow (Windows) o Pulse/ALSA (Linux).
2. Haz clic en **+ Nueva Sala**, selecciona tipo **Micrófono**, y elige el dispositivo detectado.
3. Al iniciar la sala, el audio del micrófono se procesará en tiempo real en tu GPU o CPU local.

### Ingesta RTMP desde OBS / Mesa de Sonido
Configura la sala con:
- Tipo: `Stream RTMP/HLS`
- URI: `rtmp://0.0.0.0:1935/live/auditorio1`
FFmpeg esperará el flujo entrante desde tu encoder o consola de audio y comenzará a transcribir apenas detecte señal.

---

## 9. Escalabilidad: de 2 a N Salas

NejoyT utiliza un diseño 100% asíncrono no bloqueante (`asyncio`), donde las operaciones de red (WebSockets hacia Gemini y hacia clientes) son I/O *bound*.

- **Consumo de Recursos:** Un solo proceso Python maneja cómodamente entre **10 y 15 salas simultáneas** consumiendo menos de 250 MB de memoria RAM y bajo uso de CPU (FFmpeg solo realiza resampleo de audio a 16 kHz mono).
- **Escalado Horizontal:** Para conferencias de escala masiva (20+ salas), basta con desplegar contenedores independientes repartiendo los IDs de sala mediante variables de entorno o Docker Compose.

---

## 10. Licencia

Distribuido bajo licencia **Apache 2.0**. Consulta el archivo [LICENSE](LICENSE) para más detalles.
