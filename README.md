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

## 4. Requisitos y Puesta en Marcha

### Prerrequisitos
- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (gestor de paquetes y entornos)
- FFmpeg instalado en el sistema (incluido en Windows vía WinGet o en Linux vía `apt install ffmpeg`)
- API Key de [Google AI Studio](https://aistudio.google.com/)

### Instalación

```bash
# 1. Clonar el repositorio
git clone https://github.com/tu-usuario/nejoyt.git
cd nejoyt

# 2. Instalar dependencias con uv
uv sync

# 3. Configurar variables de entorno
cp .env.example .env
```

Configura tu `.env`:
```env
GEMINI_API_KEY="AIzaSy..."
GEMINI_LIVE_MODEL="gemini-3.5-transcribe-live"
GEMINI_TRANSLATE_MODEL="gemini-3.6-flash"
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

## 5. Pruebas Rápidas con Audios Incluidos

El repositorio incluye muestras de audio en la carpeta `samples/` listas para probar de inmediato:

1. Ingresa al panel de control en [http://localhost:8000/admin](http://localhost:8000/admin).
2. Verás las salas semilla cargadas desde `rooms.yaml` en estado `STANDBY`.
3. Haz clic en **▶ Iniciar** en la sala `Auditorio Principal`.
4. Abre la vista de audiencia en [http://localhost:8000/room/auditorio-principal](http://localhost:8000/room/auditorio-principal) o el Overlay OBS en [http://localhost:8000/room/auditorio-principal?overlay=1](http://localhost:8000/room/auditorio-principal?overlay=1).
5. Observarás cómo fluyen las hipótesis parciales (`interim`) en gris claro y se consolidan en negro pleno las frases finales con su respectiva traducción automática al español, inglés o portugués.
6. Al finalizar, exporta el archivo de subtítulos generado en [http://localhost:8000/api/rooms/auditorio-principal/export?format=srt](http://localhost:8000/api/rooms/auditorio-principal/export?format=srt).

---

## 6. Operación en Vivo: Micrófono y Streaming RTMP

### Uso con Micrófono de la Sala
1. En `/admin`, haz clic en **🎙 Detectar Micrófonos**. El sistema consultará los dispositivos DirectShow (Windows) o Pulse/ALSA (Linux).
2. Haz clic en **+ Nueva Sala**, selecciona tipo **Micrófono**, y elige el dispositivo detectado.
3. Al iniciar la sala, el audio del micrófono se transmitirá en vivo a Gemini sin almacenamiento previo en disco.

### Ingesta RTMP desde OBS / Mesa de Sonido
Configura la sala con:
- Tipo: `Stream RTMP/HLS`
- URI: `rtmp://0.0.0.0:1935/live/auditorio1`
FFmpeg esperará el flujo entrante desde tu encoder o consola de audio y comenzará a transcribir apenas detecte señal.

---

## 7. Escalabilidad: de 2 a N Salas

NejoyT utiliza un diseño 100% asíncrono no bloqueante (`asyncio`), donde las operaciones de red (WebSockets hacia Gemini y hacia clientes) son I/O *bound*.

- **Consumo de Recursos:** Un solo proceso Python maneja cómodamente entre **10 y 15 salas simultáneas** consumiendo menos de 250 MB de memoria RAM y bajo uso de CPU (FFmpeg solo realiza resampleo de audio a 16 kHz mono).
- **Escalado Horizontal:** Para conferencias de escala masiva (20+ salas), basta con desplegar contenedores independientes repartiendo los IDs de sala mediante variables de entorno o Docker Compose.

---

## 8. Licencia

Distribuido bajo licencia **Apache 2.0**. Consulta el archivo [LICENSE](LICENSE) para más detalles.
