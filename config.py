import os
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RoomState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    ERROR = "error"


class SourceKind(str, Enum):
    FILE = "file"
    MIC = "mic"
    STREAM = "stream"


class RoomConfig(BaseModel):
    id: str = Field(..., description="Identificador único de la sala (ej. auditorio-principal)")
    name: str = Field(..., description="Nombre descriptivo de la sala (ej. Auditorio Principal)")
    kind: SourceKind = Field(SourceKind.FILE, description="Tipo de fuente: 'file', 'mic' o 'stream'")
    source_uri: str = Field(..., description="Ruta al archivo, URI RTMP/HLS, o nombre del dispositivo de audio")
    source_lang: str = Field("es-419", description="Idioma de origen (ej. 'es-419', 'en', o vacía para auto)")
    target_langs: List[str] = Field(default_factory=lambda: ["es", "en"], description="Idiomas de subtitulado disponibles")
    custom_vocabulary: List[str] = Field(default_factory=list, description="Términos técnicos para sesgar ASR")
    loop: bool = Field(False, description="Si es True y la fuente es un archivo, loopea continuamente")
    auto_start: bool = Field(False, description="Si es True, arranca automáticamente al bootear el servidor")


class RoomPatch(BaseModel):
    name: Optional[str] = None
    target_langs: Optional[List[str]] = None
    custom_vocabulary: Optional[List[str]] = None


class Settings(BaseSettings):
    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")
    gemini_live_model: str = Field("gemini-3.5-transcribe-live", alias="GEMINI_LIVE_MODEL")
    gemini_translate_model: str = Field("gemini-3.6-flash", alias="GEMINI_TRANSLATE_MODEL")
    admin_token: str = Field("nerdearla2026", alias="ADMIN_TOKEN")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
