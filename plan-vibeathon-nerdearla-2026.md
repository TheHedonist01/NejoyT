# Plan de ejecución — Vibeathon Nerdearla 2026
### Transcripción y traducción simultánea open source para conferencias

**Autor:** Mauro G. Martínez
**Modalidad:** individual

---

## 1. Decisiones de arquitectura (el "por qué" antes del "qué")

### 1.1 Hallazgos que condicionan todo el diseño

**Existe un modelo dedicado a esto.** `gemini-3.5-transcribe-live` hace speech-to-text en streaming sobre WebSocket. No es un agente conversacional: es un pipeline de reconocimiento de voz puro, con `response_modalities=["TEXT"]`. Emite dos campos distintos:

- `interim_input_transcription` — hipótesis parciales de baja latencia, se actualizan mientras la persona habla. Esto es lo que va a la pantalla al instante.
- `input_transcription` — el transcript finalizado cuando termina el turno de habla. Esto es lo que se traduce, se guarda y se exporta.

Esa separación es exactamente la que necesita un subtitulador en vivo, y mucha gente que compita no la va a usar bien.

**Acepta vocabulario personalizado.** Hasta 1.000 términos en `custom_vocabulary` para sesgar el reconocimiento (los mejores resultados son con hasta 100). Esto resuelve de entrada el opcional del glosario técnico, que es justo el dolor que Nerdearla menciona: nombres propios y jerga que las herramientas comerciales destrozan.

**Tiene modo SMART.** `mode="SMART"` limpia muletillas, resuelve autocorrecciones habladas y aplica puntuación y mayúsculas. Para subtítulos legibles es mejor que el `VERBATIM` por defecto.

**El límite duro: las sesiones soportan streaming continuo hasta 10 minutos.** Una charla dura 40-45. Esto no es un detalle, es *el* problema de ingeniería del proyecto. Si no lo resolvés, cada 10 minutos la audiencia se queda sin subtítulos. Si lo resolvés bien, tenés el diferencial técnico frente a los demás participantes.

**Detección automática de idioma.** Con `language_codes=[]` el modelo detecta el idioma solo y maneja code-switching (el orador que mete palabras en inglés en medio del español). Útil, pero en producción conviene fijar el idioma por sala: es más preciso y más predecible.

### 1.2 Las tres decisiones de fondo

**Decisión 1 — El audio se normaliza con ffmpeg, siempre.**
El requisito pide aceptar micrófono, archivo o streaming. En vez de escribir tres ingestores, se escribe uno solo: un subproceso de ffmpeg que toma *cualquier* fuente y escupe por stdout PCM crudo de 16 bits, 16 kHz, mono, little-endian — que es exactamente lo que pide la API. Archivo local, RTMP, HLS, un `.m3u8` de YouTube, un dispositivo de captura: todo entra por la misma cañería. Una sola abstracción, tres requisitos cumplidos.

**Decisión 2 — El ASR va detrás de una interfaz, no cableado.**
Se define una clase abstracta `ASRBackend` con dos implementaciones: `GeminiLiveASR` (cloud) y `LocalASR` (Gemma 4 E4B o faster-whisper sobre tu GPU). El orquestador no sabe cuál está usando. Esto te da tres cosas de un saque: cumplís el guiño del brief a Gemma, tenés plan B si la capa gratuita te corta la concurrencia, y le mostrás al jurado una solución que una conferencia sin presupuesto de nube también puede desplegar. En el criterio "despliegue y operación" eso vale oro.

**Decisión 3 — Un proceso asyncio, N sesiones.**
Esto es trabajo de I/O, no de CPU. Un solo proceso Python con asyncio maneja cómodamente 10 sesiones concurrentes de WebSocket. No hace falta Kubernetes ni colas. La escalabilidad se documenta, no se sobre-construye: el README explica cómo pasar a N contenedores con un reparto de salas por variable de entorno.

### 1.3 Diagrama lógico

```
┌──────────────┐
│  Fuente      │  micrófono / archivo / RTMP / HLS / YouTube
└──────┬───────┘
       │
┌──────▼───────────────────────────────┐
│  ffmpeg (subproceso)                 │  → PCM s16le 16 kHz mono
└──────┬───────────────────────────────┘
       │ chunks de 100 ms
┌──────▼───────────────────────────────┐
│  SessionWorker  (uno por sala)       │
│  ┌────────────────────────────────┐  │
│  │ ASRBackend                     │  │
│  │  ├ GeminiLiveASR (WS)          │  │  ← rotación cada ~9 min
│  │  └ LocalASR (Gemma4/Whisper)   │  │
│  └──────────┬─────────────────────┘  │
│             │ interim / final        │
│  ┌──────────▼─────────────────────┐  │
│  │ Traductor (Flash, con glosario)│  │
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

### 1.4 La rotación de sesión, en detalle

Este es el corazón. El patrón es **solape, no corte**:

1. El worker abre la sesión **A** en t=0 y publica sus transcripciones.
2. En t≈8:30 abre la sesión **B** en paralelo.
3. Durante la ventana de solape (≈30 s) el audio se envía a **las dos**, pero solo se publica lo que sale de **A**.
4. Cuando **A** emite su primer `input_transcription` final después de t=9:00, el worker corta A y promueve B a activa.
5. Se descarta de B todo lo anterior al punto de corte para no duplicar texto.

Sin ese solape hay un hueco de 1-2 segundos y una palabra cortada al medio cada 10 minutos. Con solape la audiencia no percibe nada. Este mecanismo va explicado en el README y mostrado en el video: es el punto que demuestra que entendiste el problema real de una conferencia, no el de una demo de 3 minutos.

---

## 2. Stack

| Capa | Elección | Por qué |
|---|---|---|
| Lenguaje | Python 3.12 | Tu elección, y el SDK de Gemini es de primera clase acá |
| Web/API | FastAPI + uvicorn | WebSockets nativos, async de punta a punta |
| Concurrencia | asyncio | El trabajo es I/O bound |
| SDK IA | `google-genai` | SDK oficial, soporta `client.aio.live.connect` |
| Audio | ffmpeg (subproceso) | Universal, cero dependencias Python de audio |
| ASR local | Gemma 4 E4B (Apache 2.0) o faster-whisper | Corre en tu GPU; Gemma 4 E4B tiene audio nativo y hace ASR y traducción |
| VAD local | Silero VAD | Liviano, para recorte de silencios y el modo híbrido |
| Frontend | HTML + JS vanilla servido por FastAPI | Sin build step. En una hackathon de 24 h, npm es un enemigo |
| Gestor de deps | `uv` | Instalación rápida, lockfile reproducible |
| Contenedor | Dockerfile + docker-compose | El criterio de despliegue se gana con un `docker compose up` |
| Licencia | Apache 2.0 | Aprobada por la OSI e incluye cesión de patentes |

**Nota sobre el frontend:** la tentación de meter React es fuerte y es una trampa. La página de audiencia son dos divs, un selector y un WebSocket. Vanilla te ahorra dos horas que vas a necesitar el domingo a la mañana.

---

## 3. Fases

### FASE 0 — Preparación previa (antes del 23/09, sin escribir código del proyecto)

- [ ] Crear proyecto en Google Cloud / AI Studio y generar API key
- [ ] **Verificar en AI Studio los límites activos de tu proyecto** para `gemini-3.5-transcribe-live`, en especial la concurrencia de sesiones. Google ya no publica la tabla por modelo en la doc: te manda a tu panel. Este número define si tu demo son 2 salas o 10.
- [ ] Confirmar si tu Google AI Pro cambia algo en la API. **Probablemente no:** Pro te sirve para Antigravity y la app de Gemini, pero no te sube automáticamente de tier en la API. Asumí capa gratuita hasta comprobar lo contrario.
- [ ] Correr el quickstart oficial de Live Transcription tal cual, con un micrófono, solo para ver que la key funciona. Después borralo.
- [ ] Instalar: Python 3.12, `uv`, ffmpeg en PATH, drivers CUDA al día
- [ ] Descargar los pesos de Gemma 4 E4B y/o el modelo de faster-whisper. **Hacelo ahora**, no el sábado: son varios GB y no querés esperar una descarga con el reloj corriendo.
- [ ] Bajar 3-4 audios de charlas de Nerdearla de ediciones anteriores de YouTube: dos en inglés, dos en español. Que una tenga audio feo a propósito.
- [ ] Redactar el glosario técnico (~80 términos): Kubernetes, Terraform, Prometheus, DevOps, observabilidad, service mesh, los nombres de los speakers y sponsors, "Nerdearla", "Konex"...
- [ ] Crear cuenta en Devpost y registrarte en la Vibeathon
- [ ] Entrar al Discord, canal `#nerdearla-vibeathon`
- [ ] Decidir el nombre del proyecto y que el dominio del repo esté libre
- [ ] Bocetar el guion del video (60-90 s)

---

### FASE 1 — Esqueleto e ingesta (H+0 a H+2 · sáb 12:00-14:00)

**Objetivo:** que entre audio por un extremo y salgan bytes PCM medibles por el otro.

- Crear repo, `LICENSE` (Apache 2.0), `README.md` con un título, `pyproject.toml`
- **Primer commit con fecha del 24.** Esto es tu prueba de cumplimiento.
- `config.py`: modelo de configuración por sala (id, nombre, fuente, idioma origen, idiomas destino)
- `audio/source.py`: envoltorio de ffmpeg. Recibe una URI, devuelve un iterador async de chunks PCM
  - Comando base: `ffmpeg -i <fuente> -f s16le -acodec pcm_s16le -ar 16000 -ac 1 -`
  - Chunks de 100 ms (1.024 a 2.048 frames), que es lo que recomienda la doc
- `main.py`: FastAPI arriba, endpoint `/health`
- Prueba: levantar un archivo de audio y loguear bytes por segundo. Debe dar ~32.000 B/s.

**Criterio de salida:** ves bytes fluyendo a tasa constante desde un archivo y desde una URL.

---

### FASE 2 — ASR en vivo y rotación de sesión (H+2 a H+5 · sáb 14:00-17:00)

**Objetivo:** transcripción continua e ininterrumpida de una charla de 40 minutos.

- `asr/base.py`: interfaz `ASRBackend` (`start()`, `send_audio(chunk)`, `events()`, `stop()`)
- `asr/gemini_live.py`:
  - `client.aio.live.connect(model="gemini-3.5-transcribe-live", config=...)`
  - `response_modalities=["TEXT"]`, `input_audio_transcription` con `language_codes`, `custom_vocabulary` y `mode="SMART"`
  - Envío con `send_realtime_input(audio=Blob(data=..., mime_type="audio/pcm;rate=16000"))`
  - Lectura de `server_content.interim_input_transcription` y `server_content.input_transcription`
- **`asr/rotation.py`: el gestor de solape de la sección 1.4.** Dedicale tiempo. Es la pieza que te distingue.
- Reconexión con backoff exponencial ante caída de red
- Prueba: charla completa de 40 min desde archivo, sin huecos, sin frases duplicadas en los cortes

**Criterio de salida:** 40 minutos de transcripción continua, con los cortes de rotación invisibles en el texto.

> ⚠️ Si a las 17:00 esto no anda, **pasá al backend local y seguí**. No podés perder el sábado acá.

---

### FASE 3 — Traducción y bus de eventos (H+5 a H+7 · sáb 17:00-19:00)

**Objetivo:** cada frase final aparece también en español.

- `translate/gemini_text.py`: traducción texto a texto con un modelo Flash rápido
  - **Traducí solo los `final`**, no cada parcial. Traducir cada interim te funde la cuota en minutos.
  - Para los parciales: traducción con *debounce* de ~700 ms, y solo si la latencia lo permite. Si aprieta la cuota, los parciales se muestran únicamente en el idioma original y el español aparece al cerrar la frase. Es un comportamiento aceptable y honesto.
  - Contexto: pasá las 2-3 frases anteriores en el prompt. Sin eso la traducción pierde coherencia de pronombres y tiempos verbales.
  - El glosario va en la instrucción de sistema, con la forma "estos términos NO se traducen".
- `bus.py`: bus de eventos en memoria con asyncio. Tipos de evento: `interim`, `final`, `translation`, `status`
- `store.py`: acumulador por sala con marcas de tiempo relativas al inicio (base del export SRT)

**Criterio de salida:** por consola ves el flujo `[EN interim] → [EN final] → [ES final]` de una charla real.

---

### FASE 4 — Vista de audiencia (H+7 a H+10 · sáb 20:00-23:00)

**Objetivo:** que cualquier persona abra una URL, elija sala e idioma, y lea.

- `GET /` — listado de salas activas
- `GET /room/{id}` — vista de subtítulos
- `WS /ws/{room_id}?lang=es` — canal de eventos
- La página:
  - Parciales en gris claro, finales en negro pleno. La transición es lo que hace que se sienta "en vivo".
  - Selector de idioma (original / español / inglés)
  - Control de tamaño de letra y modo alto contraste. **Esto no es cosmético: el desafío es de accesibilidad y el jurado lo va a leer así.**
  - Auto-scroll con pausa al tocar
  - Indicador de conexión y de latencia
  - Responsive de verdad: la gente lo va a abrir del celular en la butaca

**Criterio de salida:** abrís el celular y el navegador de la compu en la misma sala y los dos muestran lo mismo.

---

### FASE 5 — Multi-sesión (H+10 a H+12 · sáb 23:00-01:00)

**Objetivo:** el requisito mínimo formal: dos sesiones en simultáneo.

- `orchestrator.py`: levanta N `SessionWorker` desde `rooms.yaml`
- Aislamiento de fallos: si una sala se cae, las otras siguen
- `GET /api/rooms` con estado de cada sala
- Prueba: **dos charlas distintas en paralelo**, una en inglés y una en español
- Medir uso de CPU y memoria con 2 salas y extrapolar a 10 en el README

**Criterio de salida:** ✅ **MVP COMPLETO.** Acá cumplís los cinco requisitos mínimos de las bases. Andá a dormir.

> **Regla de disciplina:** si a la 1:00 no cerraste esta fase, cortá igual y dormí. El domingo con sueño se destruye más de lo que se construye. Todo lo que sigue es opcional.

---

### 😴 DESCANSO (01:00 a 08:00)

No es opcional. Te quedan 4 horas de trabajo y una entrega. Programá alarma.

---

### FASE 6 — Diferenciales (H+20 a H+22 · dom 08:00-10:00)

Por orden de retorno sobre esfuerzo. **Hacé de arriba hacia abajo y frená cuando el reloj lo diga.**

1. **Export SRT/VTT** (~25 min) — Los datos ya los tenés en `store.py`. Endpoint `GET /api/rooms/{id}/export?format=srt`. Es el opcional más barato del brief.
2. **Overlay para OBS** (~30 min) — La misma vista con `?overlay=1`: fondo transparente, texto con contorno, sin controles. Se agrega como Browser Source. Cumple el opcional de integración con streaming casi sin código nuevo.
3. **Panel de monitoreo** (~40 min) — `GET /admin`: estado por sala, latencia p50/p95, reconexiones, contador de rotaciones, errores. Pega directo en el criterio "despliegue y operación".
4. **Glosario en caliente** (~20 min) — Archivo `glossary.yaml` recargable sin reiniciar la sala.
5. **Portugués** (~15 min) — Es agregar un idioma destino a la lista. Barato, y el brief lo menciona.

---

### FASE 7 — Modo local (H+22 a H+23 · dom 10:00-11:00)

**Objetivo:** demostrar que la solución corre sin nube. Si vas retrasado, esto se salta y se documenta como trabajo futuro.

- `asr/local.py` con Gemma 4 E4B vía Transformers, o faster-whisper
- Ojo con el límite de Gemma 4: acepta hasta ~30 segundos de audio por entrada, así que necesitás una capa de troceado con VAD (Silero) que corte en los silencios, no a lo bruto cada 30 s
- Traducción local con el mismo Gemma 4
- Flag `--backend local` y sección en el README

**Por qué vale la pena aunque sea a medias:** el brief menciona Gemma explícitamente. Un participante que entregue las dos rutas responde la pregunta de costo antes de que el jurado la haga.

---

### FASE 8 — Entrega (H+23 a H+24 · dom 11:00-12:00)

**Esta es la hora que más gente subestima. No la comprimas.**

- [ ] `README.md` completo:
  - Qué hace y qué problema resuelve
  - Arquitectura con el diagrama
  - Instalación: `uv sync` / `docker compose up`
  - Credenciales necesarias y cómo obtenerlas
  - **Cómo probar con los audios incluidos en el repo** (las bases lo piden explícitamente)
  - **Cómo escalar de 2 a N sesiones** (también lo piden explícitamente)
  - La explicación del solape de sesión de 10 minutos
  - Limitaciones conocidas, dichas de frente
- [ ] Carpeta `samples/` con los audios de prueba y un script de un comando
- [ ] `LICENSE` Apache 2.0 verificado
- [ ] `.env.example`
- [ ] Repo público
- [ ] **Video de 1-2 minutos:**
  - 0:00-0:15 el problema (30+ charlas en inglés, en simultáneo)
  - 0:15-0:50 demo en vivo con dos salas en paralelo
  - 0:50-1:15 el momento de rotación de sesión, mostrando que no se corta
  - 1:15-1:40 arquitectura y modo local
  - Subilo a YouTube
- [ ] **Subtítulos en inglés del video generados con tu propio proyecto.** Exportás el VTT desde tu herramienta y lo subís a YouTube. El brief lo sugiere como guiño y es la mejor prueba de que funciona.
- [ ] Envío en Devpost **antes de las 12:00 ART del domingo**

> **Poné una alarma a las 11:15.** El deadline es duro y Devpost no perdona.

---

## 4. Riesgos y planes B

| Riesgo | Probabilidad | Mitigación |
|---|---|---|
| La capa gratuita limita las sesiones concurrentes | **Alta** | Verificar el número en Fase 0. Si son 2, la demo son 2 salas cloud + N locales, y el README explica el cálculo de escalado con tier pago |
| La rotación de 10 min tiene bugs sutiles | **Alta** | Fase 2 con tiempo de sobra. Test dedicado con audio de 25 min |
| La latencia de traducción se acumula | Media | Traducir solo los finales; parciales en idioma original |
| El modelo es preview y cambia | Media | Fijar la versión exacta del modelo en config, no usar alias |
| Te quedás sin cuota el domingo a la mañana | Media | El backend local no tiene cuota. Grabá el video temprano si ves que apreta |
| Se te va el tiempo en el frontend | Media | Vanilla JS, cero build. Timebox estricto de 3 h |
| Audio de mala calidad del stream | Baja | Normalización con filtro de ffmpeg (`loudnorm`) |
| Falla de red durante la demo | Baja | Grabá la demo en video, no la hagas en vivo |

**El riesgo número uno no es técnico: es de alcance.** Estás solo contra equipos. Tu MVP tiene que estar cerrado el sábado a la noche. Todo lo demás es bonus.

---

## 5. Cómo trabajar con Antigravity

Como el código lo vas a generar con un agente, dos cosas hacen la diferencia:

**Poné un `AGENTS.md` en la raíz el sábado a primera hora** con: el diagrama de arquitectura, la lista de archivos y qué hace cada uno, las convenciones (async en todo, tipado, sin dependencias nuevas sin justificar) y la advertencia de que `gemini-3.5-transcribe-live` tiene un tope de sesión de 10 minutos. Sin eso, el agente te va a inventar una API de Gemini que no existe: el modelo cambió mucho en los últimos meses y el conocimiento entrenado de cualquier asistente está atrasado.

**Pegá el fragmento real de la doc en el prompt** cuando le pidas el módulo de Gemini. No confíes en que se acuerde de `interim_input_transcription` ni de la forma de `send_realtime_input`. Es el error más caro que podés cometer y se paga en horas de depuración.

**Una fase, un contexto.** No le pidas el proyecto entero de una. Módulo por módulo, con la prueba de aceptación de cada fase como criterio de cierre.

---

## 6. Mapa de criterios del jurado

| Criterio | Dónde lo ganás |
|---|---|
| **Calidad** | `custom_vocabulary` con el glosario + `mode="SMART"`. Mostralo con un término técnico que las herramientas comerciales suelen errar |
| **Latencia** | Parciales en pantalla al instante; medí y publicá el número en el panel |
| **Escalabilidad** | 2 salas demostradas + cálculo de 10 en el README + backend local sin cuota |
| **Despliegue** | `docker compose up` y listo. README que puede seguir alguien que no sos vos |
| **Innovación** | El solape de rotación de sesión, el overlay de OBS y el modo 100% local |

---

## 7. Checklist final de requisitos

- [ ] Recibe audio en vivo de al menos una fuente
- [ ] Transcripción en tiempo real del idioma original
- [ ] Traducción en tiempo real inglés → español
- [ ] Muestra los subtítulos en algún lado
- [ ] Procesa al menos dos sesiones simultáneas
- [ ] README explica cómo escalar a más
- [ ] Audios de prueba incluidos con opción sencilla de importarlos
- [ ] Repo público con licencia aprobada por la OSI
- [ ] Video de 1-2 min en YouTube
- [ ] Envío en Devpost antes del 25/09 15:00 UTC
