import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("nerdearla.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Iniciando servicio de subtitulado en vivo Nerdearla 2026...")
    logger.info("Modelo Live ASR: %s", settings.gemini_live_model)
    logger.info("Modelo Traducción: %s", settings.gemini_translate_model)
    yield
    logger.info("Deteniendo servicio de subtitulado...")


app = FastAPI(
    title="NejoyT - Transcripción y Traducción en Vivo Nerdearla 2026",
    description="Sistema de subtitulado simultáneo multi-sala con Gemini Live",
    version="0.1.0",
    lifespan=lifespan
)


@app.get("/health")
async def health_check():
    """Endpoint de comprobación de salud del sistema."""
    return {
        "status": "healthy",
        "service": "nerdearla-live-subtitles",
        "version": "0.1.0",
        "live_model": settings.gemini_live_model,
        "translate_model": settings.gemini_translate_model
    }
