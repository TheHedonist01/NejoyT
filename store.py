import logging
import time
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("nerdearla.store")


class SubtitleRecord(BaseModel):
    """Registro individual de una frase finalizada con timestamps relativos."""
    index: int = Field(..., description="Índice secuencial para el subtítulo")
    start_seconds: float = Field(..., description="Segundo de inicio relativo al inicio de la sala")
    end_seconds: float = Field(..., description="Segundo de finalización relativo")
    original_text: str = Field(..., description="Texto en idioma original")
    original_lang: str = Field("en", description="Idioma original detectado o configurado")
    translations: Dict[str, str] = Field(default_factory=dict, description="Traducciones por código de idioma")


class SubtitleStore:
    """
    Almacén en memoria por sala con marcas de tiempo relativas.
    Permite persistir y exportar los subtítulos en formatos estándar SRT y WebVTT.
    """

    def __init__(self):
        self._rooms_start: Dict[str, float] = {}
        self._rooms_history: Dict[str, List[SubtitleRecord]] = {}

    def start_room_clock(self, room_id: str) -> None:
        """Marca el t=0 para una sala."""
        self._rooms_start[room_id] = time.time()
        self._rooms_history[room_id] = []
        logger.info("Reloj iniciado para sala '%s'", room_id)

    def add_subtitle(
        self,
        room_id: str,
        original_text: str,
        original_lang: str = "en",
        translations: Optional[Dict[str, str]] = None,
        duration_sec: float = 3.0
    ) -> SubtitleRecord:
        """
        Agrega un subtítulo finalizado con marcas de tiempo relativas al inicio de la sala.
        """
        start_time_wall = self._rooms_start.get(room_id)
        if start_time_wall is None:
            self.start_room_clock(room_id)
            start_time_wall = self._rooms_start[room_id]

        history = self._rooms_history.setdefault(room_id, [])
        end_rel = max(time.time() - start_time_wall, 0.5)
        start_rel = max(end_rel - duration_sec, 0.0)

        record = SubtitleRecord(
            index=len(history) + 1,
            start_seconds=start_rel,
            end_seconds=end_rel,
            original_text=original_text,
            original_lang=original_lang,
            translations=translations or {}
        )
        history.append(record)
        return record

    def get_history(self, room_id: str) -> List[SubtitleRecord]:
        """Obtiene la lista completa de subtítulos emitidos en la sala."""
        return self._rooms_history.get(room_id, [])

    @staticmethod
    def _format_timestamp(seconds: float, vtt: bool = False) -> str:
        """Convierte segundos a formato HH:MM:SS,mmm (SRT) o HH:MM:SS.mmm (VTT)."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int(round((seconds - int(seconds)) * 1000))
        sep = "." if vtt else ","
        return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"

    def export_srt(self, room_id: str, lang: str = "es") -> str:
        """Exporta los subtítulos en formato SubRip (.srt)."""
        history = self.get_history(room_id)
        blocks = []
        for record in history:
            text = record.translations.get(lang, record.original_text)
            start_fmt = self._format_timestamp(record.start_seconds, vtt=False)
            end_fmt = self._format_timestamp(record.end_seconds, vtt=False)
            block = f"{record.index}\n{start_fmt} --> {end_fmt}\n{text}\n"
            blocks.append(block)
        return "\n".join(blocks)

    def export_vtt(self, room_id: str, lang: str = "es") -> str:
        """Exporta los subtítulos en formato WebVTT (.vtt)."""
        history = self.get_history(room_id)
        lines = ["WEBVTT\n"]
        for record in history:
            text = record.translations.get(lang, record.original_text)
            start_fmt = self._format_timestamp(record.start_seconds, vtt=True)
            end_fmt = self._format_timestamp(record.end_seconds, vtt=True)
            block = f"{record.index}\n{start_fmt} --> {end_fmt}\n{text}\n"
            lines.append(block)
        return "\n".join(lines)


# Singleton del almacén de subtítulos
subtitle_store = SubtitleStore()
