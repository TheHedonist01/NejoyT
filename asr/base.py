import time
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional
from pydantic import BaseModel, Field


class ASRTranscriptionEvent(BaseModel):
    """Evento emitido por el backend de transcripción."""
    event_type: str = Field(..., description="'interim' (hipótesis parcial) o 'final' (frase cerrada)")
    text: str = Field(..., description="Texto reconocido")
    is_final: bool = Field(False, description="True si es una transcripción cerrada")
    language: Optional[str] = Field(None, description="Código de idioma detectado o configurado")
    translation: Optional[str] = Field(None, description="Traducción directa local si está disponible")
    translation_lang: Optional[str] = Field(None, description="Código de idioma de la traducción directa")
    timestamp: float = Field(default_factory=time.time, description="Marca de tiempo en segundos")


class ASRBackend(ABC):
    """Interfaz abstracta unificada para backends ASR (Gemini Live o Local)."""

    @abstractmethod
    async def start(self) -> None:
        """Inicializa la sesión o recursos del backend."""
        pass

    @abstractmethod
    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Envía un bloque de audio PCM (16 kHz, 16-bit mono, s16le)."""
        pass

    @abstractmethod
    async def events(self) -> AsyncGenerator[ASRTranscriptionEvent, None]:
        """Generador asíncrono que emite los eventos de transcripción."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Finaliza la sesión limpiamente y libera recursos."""
        pass
