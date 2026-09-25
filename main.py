import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from config import RoomConfig, RoomPatch, SettingsUpdate, settings, update_runtime_settings
from orchestrator import orchestrator
from audio.source import list_audio_devices
from bus import event_bus, SubtitleEvent
from store import subtitle_store

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("nerdearla.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ciclo de vida de la aplicación: inicializa orquestador y salas."""
    logger.info("Iniciando NejoyT Subtitulado Nerdearla 2026...")
    logger.info("Modelo Live ASR: %s", settings.gemini_live_model)
    logger.info("Modelo Traducción: %s", settings.gemini_translate_model)

    # Cargar salas semilla desde rooms.yaml e iniciar las que tengan auto_start=True
    orchestrator.load_rooms("rooms.yaml")
    await orchestrator.start_all()

    yield

    logger.info("Deteniendo NejoyT y liberando recursos...")
    await orchestrator.stop_all()


app = FastAPI(
    title="NejoyT - Transcripción y Traducción en Vivo Nerdearla 2026",
    description="Sistema de subtitulado simultáneo multi-sala con Gemini Live",
    version="0.2.0",
    lifespan=lifespan
)

# Soporte completo de CORS para navegadores Chromium y Firefox
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir archivos estáticos
os.makedirs("static", exist_ok=True)
os.makedirs(os.path.join("static", "fonts"), exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/fonts", StaticFiles(directory=os.path.join("static", "fonts")), name="fonts")
if os.path.exists("img"):
    app.mount("/img", StaticFiles(directory="img"), name="img")


def require_admin(
    x_admin_token: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
) -> str:
    """Verifica el token de administración en el header X-Admin-Token o query param token."""
    token_to_check = (x_admin_token or token or "").strip()
    if not token_to_check or token_to_check != settings.admin_token:
        raise HTTPException(
            status_code=401,
            detail="Token de administración no autorizado."
        )
    return token_to_check


class AuthVerifyRequest(BaseModel):
    token: Optional[str] = None


@app.post("/api/auth/verify")
async def verify_auth_token(
    req: Optional[AuthVerifyRequest] = None,
    x_admin_token: Optional[str] = Header(None)
):
    """Verifica si la contraseña o token proporcionado coincide con el ADMIN_TOKEN predefinido."""
    token_to_check = (req.token if (req and req.token) else x_admin_token or "").strip()
    if token_to_check and token_to_check == settings.admin_token:
        return {"status": "ok", "authenticated": True, "message": "Acceso autorizado"}
    raise HTTPException(status_code=401, detail="Contraseña de administración incorrecta.")


@app.get("/", response_class=HTMLResponse)
async def index_page():
    """Página principal con el listado de salas para la audiencia."""
    index_path = os.path.join("static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>NejoyT API Activa</h1>")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Panel de administración y control operativo de salas para la organización."""
    admin_path = os.path.join("static", "admin.html")
    if os.path.exists(admin_path):
        return FileResponse(admin_path)
    return HTMLResponse("<h1>Panel Admin no encontrado en static/admin.html</h1>", status_code=404)


@app.get("/room/{room_id}", response_class=HTMLResponse)
async def room_page(room_id: str, overlay: Optional[int] = 0):
    """Vista de subtítulos para la audiencia o overlay transparente para OBS (?overlay=1)."""
    room_path = os.path.join("static", "room.html")
    if os.path.exists(room_path):
        return FileResponse(room_path)
    raise HTTPException(status_code=404, detail="Página de sala no encontrada")


@app.get("/api/rooms")
async def get_rooms():
    """Listado y estado en tiempo real de todas las salas configuradas."""
    rooms = orchestrator.list_rooms()
    return {"status": "ok", "rooms": [r.model_dump() for r in rooms]}


@app.post("/api/rooms")
async def create_room(config: RoomConfig, auto_start: bool = False, _: str = Depends(require_admin)):
    """Crea una nueva sala en caliente (archivo, micrófono o stream RTMP/HLS)."""
    try:
        worker = await orchestrator.create_room(config, start=auto_start)
        return {"status": "ok", "message": f"Sala '{config.id}' creada exitosamente", "room": worker.get_status().model_dump()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Error creando sala: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rooms/{room_id}/start")
async def start_room(room_id: str, _: str = Depends(require_admin)):
    """Inicia la sesión de subtitulado en vivo para la sala indicada."""
    success = await orchestrator.start_room(room_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Sala '{room_id}' no encontrada.")
    return {"status": "ok", "message": f"Transmisión iniciada en sala '{room_id}'"}


@app.post("/api/rooms/{room_id}/stop")
async def stop_room(room_id: str, _: str = Depends(require_admin)):
    """Detiene la sesión de subtitulado de la sala y flushea subtítulos."""
    success = await orchestrator.stop_room(room_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Sala '{room_id}' no encontrada.")
    return {"status": "ok", "message": f"Transmisión detenida en sala '{room_id}'"}


@app.delete("/api/rooms/{room_id}")
@app.post("/api/rooms/{room_id}/delete")
async def delete_room(room_id: str, _: str = Depends(require_admin)):
    """Elimina por completo una sala deteniendo sus procesos."""
    success = await orchestrator.delete_room(room_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Sala '{room_id}' no encontrada.")
    return {"status": "ok", "message": f"Sala '{room_id}' eliminada exitosamente"}


@app.patch("/api/rooms/{room_id}")
async def patch_room(room_id: str, patch: RoomPatch, _: str = Depends(require_admin)):
    """Actualiza parámetros mutables de la sala en caliente (glosario, idiomas, nombre)."""
    success = orchestrator.patch_room(room_id, patch)
    if not success:
        raise HTTPException(status_code=404, detail=f"Sala '{room_id}' no encontrada.")
    worker = orchestrator.get_worker(room_id)
    return {"status": "ok", "message": "Sala actualizada", "room": worker.get_status().model_dump() if worker else None}


@app.get("/api/devices/audio")
async def get_audio_devices():
    """Retorna los dispositivos de entrada de audio detectados por FFmpeg."""
    devices = list_audio_devices()
    return {"status": "ok", "devices": devices}


@app.get("/api/rooms/{room_id}/export")
async def export_subtitles(
    room_id: str,
    format: str = "html",
    lang: str = "es",
    print: Optional[int] = 0
):
    """
    Exporta la transcripción de una sala en diversos formatos:
    - format=txt: Texto plano estructurado en párrafos continuos (ideal para IA / lectura).
    - format=pdf: Archivo PDF profesional maquetado en A4 listo para descargar.
    - format=html: Documento web de lectura editorial centrado con opción de imprimir o copiar.
    - format=srt: Subtítulos cronometrados estándar SubRip para edición de video.
    - format=vtt: Subtítulos WebVTT para reproductores HTML5.
    """
    worker = orchestrator.get_worker(room_id)
    room_name = worker.room.name if worker else room_id
    fmt = format.lower().strip()

    if fmt == "pdf":
        pdf_bytes = subtitle_store.export_pdf(room_id, lang=lang, room_name=room_name)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{room_id}_{lang}.pdf"'}
        )
    elif fmt in ("txt", "text"):
        content = subtitle_store.export_txt(room_id, lang=lang, room_name=room_name)
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{room_id}_{lang}_transcripcion.txt"'}
        )
    elif fmt in ("html", "doc", "web"):
        html_doc = subtitle_store.export_html_doc(room_id, lang=lang, room_name=room_name, auto_print=(print == 1))
        return HTMLResponse(content=html_doc)
    elif fmt == "vtt":
        content = subtitle_store.export_vtt(room_id, lang=lang)
        return Response(
            content=content,
            media_type="text/vtt; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{room_id}_{lang}.vtt"'}
        )
    else:  # default or "srt"
        content = subtitle_store.export_srt(room_id, lang=lang)
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{room_id}_{lang}.srt"'}
        )


@app.get("/api/rooms/{room_id}/history")
async def get_room_history(room_id: str, lang: Optional[str] = "es", limit: Optional[int] = None):
    """
    Retorna el historial completo de subtítulos persistidos en SQLite para la sala.
    Permite a la audiencia cargar y leer todo lo que se habló desde el inicio de la conferencia.
    """
    records = subtitle_store.get_history(room_id, limit=limit)
    chosen_lang = (lang or "es").strip().lower()[:2]
    items = []
    for r in records:
        text = r.translations.get(chosen_lang) or r.original_text
        items.append({
            "index": r.index,
            "start_seconds": r.start_seconds,
            "end_seconds": r.end_seconds,
            "text": text,
            "original_text": r.original_text,
            "language": chosen_lang,
            "translations": r.translations,
            "speaker": r.speaker,
            "speaker_color": r.speaker_color
        })
    return {
        "status": "ok",
        "room_id": room_id,
        "count": len(items),
        "history": items
    }


@app.post("/api/speakers/test-sample")
async def test_speaker_sample(sample_uri: str = Query(..., description="Ruta relativa o absoluta al archivo de muestra")):
    """Endpoint de huella acústica (desactivado a pedido)."""
    return {
        "status": "disabled",
        "embed_dim": 0,
        "message": "Identificación de huella acústica y disertantes desactivada."
    }


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, lang: Optional[str] = None):
    """
    Canal WebSocket en tiempo real para transmisión de subtítulos (interim, final, status).
    """
    await websocket.accept()
    worker = orchestrator.get_worker(room_id)
    default_lang = worker.room.target_lang if worker else "es"
    chosen_lang = (lang or default_lang).strip().lower()[:2]

    logger.info("Cliente WebSocket conectado a sala '%s' (idioma preferido: %s)", room_id, chosen_lang)
    queue = await event_bus.subscribe(room_id, lang=chosen_lang)

    try:
        # Enviar estado actual de la sala
        current_status = worker.state.value if worker else "stopped"
        init_status = SubtitleEvent(
            room_id=room_id,
            event_type="status",
            text="",
            language=chosen_lang,
            is_final=False,
            metadata={"status": current_status}
        )
        await websocket.send_text(init_status.model_dump_json())

        # Para OBS overlay enviamos los últimos 2 para no saturar el tercio inferior.
        # Para la audiencia en vivo enviamos TODO el historial previo persistido en SQLite.
        is_overlay = (websocket.query_params.get("overlay") == "1")
        history_to_send = subtitle_store.get_history(room_id, limit=2 if is_overlay else None)
        for sub in history_to_send:
            text = sub.translations.get(chosen_lang) or sub.original_text
            hist_ev = SubtitleEvent(
                room_id=room_id,
                event_type="final",
                text=text,
                language=chosen_lang,
                is_final=True,
                timestamp=sub.start_seconds,
                metadata={
                    "index": sub.index,
                    "speaker": sub.speaker,
                    "speaker_color": sub.speaker_color
                }
            )
            await websocket.send_text(hist_ev.model_dump_json())

        while True:
            event = await queue.get()
            await websocket.send_text(event.model_dump_json())

    except (WebSocketDisconnect, asyncio.CancelledError):
        logger.info("Cliente WebSocket desconectado de sala '%s'", room_id)
    except Exception as e:
        logger.warning("Error en WebSocket de sala '%s': %s", room_id, e)
    finally:
        await event_bus.unsubscribe(room_id, queue)


def _mask_api_key(key: str) -> str:
    if not key or len(key) < 10:
        return "Configurada" if key else ""
    return f"{key[:6]}...{key[-4:]}"


@app.get("/api/config/settings")
async def get_system_settings(_: str = Depends(require_admin)):
    """Retorna la configuración actual del sistema y estado de credenciales."""
    return {
        "status": "ok",
        "gemini_api_key_masked": _mask_api_key(settings.gemini_api_key),
        "has_gemini_api_key": bool(settings.gemini_api_key and not settings.gemini_api_key.startswith("your_")),
        "gemini_live_model": settings.gemini_live_model,
        "gemini_translate_model": settings.gemini_translate_model,
        "secrets_file": "secrets/credentials.json"
    }


@app.post("/api/config/settings")
async def update_system_settings(update: SettingsUpdate, _: str = Depends(require_admin)):
    """Actualiza la API Key de Google Gemini y modelos en caliente."""
    update_runtime_settings(update)
    for worker in orchestrator._workers.values():
        if hasattr(worker, "translator") and worker.translator:
            worker.translator._client = None
            worker.translator.model = settings.gemini_translate_model
    return {
        "status": "ok",
        "message": "Configuración guardada en secrets/credentials.json exitosamente",
        "gemini_api_key_masked": _mask_api_key(settings.gemini_api_key),
        "has_gemini_api_key": bool(settings.gemini_api_key),
        "gemini_live_model": settings.gemini_live_model,
        "gemini_translate_model": settings.gemini_translate_model
    }


class GeminiTestRequest(BaseModel):
    api_key: Optional[str] = None


@app.post("/api/config/test-gemini")
async def test_gemini_connection(req: Optional[GeminiTestRequest] = None, _: str = Depends(require_admin)):
    """Valida la conectividad de la API Key con Google Gemini."""
    test_key = (req.api_key if (req and req.api_key) else settings.gemini_api_key).strip()
    if not test_key:
        raise HTTPException(status_code=400, detail="No se proporcionó API Key para la prueba.")

    try:
        from google import genai
        client = genai.Client(api_key=test_key)
        await client.aio.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents="Test"
        )
        return {"status": "ok", "message": "Conexión con Google Gemini verificada exitosamente."}
    except Exception as e:
        logger.warning("Error validando API Key de Gemini: %s", e)
        raise HTTPException(status_code=400, detail=f"Error validando API Key: {str(e)}")


@app.get("/api/hardware")
async def get_hardware_info():
    """
    Retorna el acelerador y dispositivo de hardware detectado dinámicamente
    (CUDA, ROCm, MPS o CPU con cuantización óptima).
    """
    from asr.device import detect_compute_device
    hw = detect_compute_device()
    return {
        "status": "ok",
        "device": hw.device,
        "compute_type": hw.compute_type,
        "device_index": hw.device_index,
        "description": hw.description
    }


@app.get("/api/fonts")
async def list_custom_fonts():
    """Escanea y lista fuentes tipográficas locales (.ttf, .otf, .woff, .woff2) en static/fonts."""
    fonts_dir = os.path.join("static", "fonts")
    fonts = []
    if os.path.exists(fonts_dir):
        for fname in os.listdir(fonts_dir):
            if fname.lower().endswith((".ttf", ".otf", ".woff", ".woff2")):
                name = os.path.splitext(fname)[0].replace("-", " ").replace("_", " ").title()
                fonts.append({
                    "name": name,
                    "filename": fname,
                    "url": f"/static/fonts/{fname}"
                })
    return {"status": "ok", "fonts": fonts}


@app.get("/api/samples")
async def list_samples():
    """Lista los archivos de audio de muestra y subidos disponibles en samples/."""
    samples_dir = "samples"
    os.makedirs(samples_dir, exist_ok=True)
    files = []
    valid_exts = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".webm")
    for fname in sorted(os.listdir(samples_dir)):
        if fname.lower().endswith(valid_exts):
            full_p = os.path.join(samples_dir, fname)
            try:
                sz = os.path.getsize(full_p)
            except OSError:
                sz = 0
            files.append({
                "filename": fname,
                "path": f"samples/{fname}",
                "size_bytes": sz
            })
    return {"status": "ok", "samples": files}


@app.post("/api/upload/audio")
async def upload_audio(request: Request, filename: Optional[str] = Query(None)):
    """
    Sube un archivo de audio/video directamente desde el navegador para usar como fuente de sala.
    Transmite en chunks para no saturar memoria y normaliza la ruta para FFmpeg.
    """
    fname = filename or request.headers.get("x-filename", "audio_sample.wav")
    clean_name = re.sub(r'[^a-zA-Z0-9_.-]', '_', os.path.basename(fname))
    valid_exts = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".webm")
    if not clean_name or not any(clean_name.lower().endswith(ext) for ext in valid_exts):
        clean_name = f"{os.path.splitext(clean_name)[0] or 'audio'}.wav"

    samples_dir = "samples"
    os.makedirs(samples_dir, exist_ok=True)
    target_path = os.path.join(samples_dir, clean_name)

    total_bytes = 0
    try:
        with open(target_path, "wb") as f:
            async for chunk in request.stream():
                if chunk:
                    f.write(chunk)
                    total_bytes += len(chunk)
    except Exception as e:
        logger.error("Error al escribir archivo de audio subido: %s", e)
        raise HTTPException(status_code=500, detail=f"Error guardando archivo: {str(e)}")

    if total_bytes == 0:
        if os.path.exists(target_path):
            os.remove(target_path)
        raise HTTPException(status_code=400, detail="El archivo de audio subido está vacío.")

    norm_path = f"samples/{clean_name}"
    logger.info("Archivo de audio subido exitosamente: %s (%d bytes)", norm_path, total_bytes)

    return {
        "status": "ok",
        "message": f"Archivo '{clean_name}' subido con éxito",
        "file_path": norm_path,
        "filename": clean_name,
        "size_bytes": total_bytes
    }


@app.get("/health")
async def health_check():
    """Comprobación de salud del sistema, modelo, hardware y salas activas."""
    from asr.device import detect_compute_device
    hw = detect_compute_device()
    rooms = orchestrator.list_rooms()
    active_count = sum(1 for r in rooms if r.is_running)
    return {
        "status": "healthy",
        "service": "nerdearla-live-subtitles",
        "version": "0.2.0",
        "hardware": {
            "device": hw.device,
            "compute_type": hw.compute_type,
            "description": hw.description
        },
        "total_rooms": len(rooms),
        "active_rooms": active_count,
        "live_model": settings.gemini_live_model,
        "translate_model": settings.gemini_translate_model
    }
