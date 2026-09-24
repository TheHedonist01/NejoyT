import os
from typing import List, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RoomConfig(BaseModel):
    id: str = Field(..., description="Identificador único de la sala (ej. main-stage)")
    name: str = Field(..., description="Nombre amigable de la sala (ej. Auditorio Principal)")
    source_uri: str = Field(..., description="Ruta al archivo, stream RTMP/HLS, o entrada de micrófono")
    source_lang: str = Field("es-419", description="Idioma de origen (ej. 'es-419', 'en', o vacía para auto)")
    target_langs: List[str] = Field(default_factory=lambda: ["es", "en"], description="Idiomas de subtitulado disponibles")
    custom_vocabulary: List[str] = Field(default_factory=list, description="Términos técnicos para sesgar ASR")


class Settings(BaseSettings):
    gemini_api_key: str = Field(..., alias="GEMINI_API_KEY")
    gemini_live_model: str = Field("gemini-3.5-transcribe-live", alias="GEMINI_LIVE_MODEL")
    gemini_translate_model: str = Field("gemini-2.5-flash", alias="GEMINI_TRANSLATE_MODEL")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
