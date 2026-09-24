import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from config import settings
from orchestrator import orchestrator
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

    # Cargar salas desde rooms.yaml e iniciar los SessionWorkers
    orchestrator.load_rooms("rooms.yaml")
    await orchestrator.start_all()

    yield

    logger.info("Deteniendo NejoyT y liberando recursos...")
    await orchestrator.stop_all()


app = FastAPI(
    title="NejoyT - Transcripción y Traducción en Vivo Nerdearla 2026",
    description="Sistema de subtitulado simultáneo multi-sala con Gemini Live",
    version="0.1.0",
    lifespan=lifespan
)

# Servir archivos estáticos si existen
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index_page():
    """Página principal con el listado de salas activas."""
    index_path = os.path.join("static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>NejoyT API Activa</h1>")


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


@app.get("/api/rooms/{room_id}/export")
async def export_subtitles(room_id: str, format: str = "srt", lang: str = "es"):
    """
    Exporta los subtítulos de una sala en formato estándar .srt o .vtt.
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
    Canal WebSocket en tiempo real para transmisión de subtítulos (interim y final/translation).
    """
    await websocket.accept()
    logger.info("Cliente WebSocket conectado a sala '%s' (idioma preferido: %s)", room_id, lang)

    queue = await event_bus.subscribe(room_id)

    try:
        while True:
            # Esperar nuevo evento publicado en la sala
            event = await queue.get()

            # Enviar JSON al cliente
            await websocket.send_text(event.model_dump_json())

    except WebSocketDisconnect:
        logger.info("Cliente WebSocket desconectado de sala '%s'", room_id)
    except Exception as e:
        logger.warning("Error en WebSocket de sala '%s': %s", room_id, e)
    finally:
        await event_bus.unsubscribe(room_id, queue)


@app.get("/health")
async def health_check():
    """Comprobación de salud del sistema y orquestador."""
    rooms = orchestrator.list_rooms()
    return {
        "status": "healthy",
        "service": "nerdearla-live-subtitles",
        "version": "0.1.0",
        "active_rooms": len(rooms),
        "live_model": settings.gemini_live_model,
        "translate_model": settings.gemini_translate_model
    }
