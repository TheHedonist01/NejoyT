import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from config import RoomConfig, RoomPatch, settings
from orchestrator import orchestrator
from audio.source import list_audio_devices
from bus import event_bus
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

# Servir archivos estáticos
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


def require_admin(x_admin_token: Optional[str] = Header(None)) -> str:
    """Verifica el token de administración en el header X-Admin-Token."""
    if not x_admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(
            status_code=401,
            detail="Token de administración no autorizado. Envíe 'X-Admin-Token' válido."
        )
    return x_admin_token


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
async def export_subtitles(room_id: str, format: str = "srt", lang: str = "es"):
    """
    Exporta los subtítulos acumulados de una sala en formato estándar .srt o .vtt.
    """
    format_lower = format.lower()
    if format_lower == "vtt":
        content = subtitle_store.export_vtt(room_id, lang=lang)
        media_type = "text/vtt"
        filename = f"{room_id}_{lang}.vtt"
    else:
        content = subtitle_store.export_srt(room_id, lang=lang)
        media_type = "text/plain"
        filename = f"{room_id}_{lang}.srt"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, lang: Optional[str] = "es"):
    """
    Canal WebSocket en tiempo real para transmisión de subtítulos (interim, final, status).
    """
    await websocket.accept()
    logger.info("Cliente WebSocket conectado a sala '%s' (idioma preferido: %s)", room_id, lang)

    queue = await event_bus.subscribe(room_id)

    try:
        while True:
            event = await queue.get()
            await websocket.send_text(event.model_dump_json())

    except WebSocketDisconnect:
        logger.info("Cliente WebSocket desconectado de sala '%s'", room_id)
    except Exception as e:
        logger.warning("Error en WebSocket de sala '%s': %s", room_id, e)
    finally:
        await event_bus.unsubscribe(room_id, queue)


@app.get("/health")
async def health_check():
    """Comprobación de salud del sistema, modelo y salas activas."""
    rooms = orchestrator.list_rooms()
    active_count = sum(1 for r in rooms if r.is_running)
    return {
        "status": "healthy",
        "service": "nerdearla-live-subtitles",
        "version": "0.2.0",
        "total_rooms": len(rooms),
        "active_rooms": active_count,
        "live_model": settings.gemini_live_model,
        "translate_model": settings.gemini_translate_model
    }
