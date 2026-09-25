import json
import os
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRETS_DIR = "secrets"
CREDENTIALS_FILE = os.path.join(SECRETS_DIR, "credentials.json")


def load_stored_credentials() -> dict:
    """Carga credenciales y ajustes sensibles desde secrets/credentials.json si existen."""
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_stored_credentials(data: dict) -> None:
    """Persiste credenciales en la carpeta de seguridad secrets/credentials.json."""
    os.makedirs(SECRETS_DIR, exist_ok=True)
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


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


class ASRBackendKind(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


class SpeakerProfile(BaseModel):
    name: str = Field(..., description="Nombre del orador/disertante (ej. 'Andrew Tanenbaum')")
    sample_uri: Optional[str] = Field(None, description="Ruta o URI de la muestra de audio para huella vocal")
    color: Optional[str] = Field(None, description="Color distintivo HEX para su badge en pantalla")


class RoomConfig(BaseModel):
    id: str = Field(..., description="Identificador único de la sala (ej. auditorio-principal)")
    name: str = Field(..., description="Nombre descriptivo de la sala (ej. Auditorio Principal)")
    kind: SourceKind = Field(SourceKind.FILE, description="Tipo de fuente: 'file', 'mic' o 'stream'")
    backend: ASRBackendKind = Field(ASRBackendKind.LOCAL, description="Backend ASR: 'local' (Hardware agnóstico: CUDA/ROCm/MPS/CPU) o 'cloud' (Gemini Live)")
    source_uri: str = Field(..., description="Ruta al archivo, URI RTMP/HLS, o nombre del dispositivo de audio")
    source_lang: str = Field("auto", description="Idioma base hablado en el audio ('auto', 'en', 'es', 'pt')")
    target_lang: str = Field("es", description="Idioma de salida principal para los subtítulos ('es', 'en', 'pt')")
    target_langs: List[str] = Field(default_factory=lambda: ["es", "en", "pt"], description="Idiomas de subtitulado disponibles")
    whisper_model_size: str = Field("base", description="Tamaño del modelo Whisper local ('tiny', 'base', 'small', 'medium')")
    custom_vocabulary: List[str] = Field(default_factory=list, description="Términos técnicos para sesgar ASR")
    speakers: List[SpeakerProfile] = Field(default_factory=list, description="Perfiles de disertantes enrolados para Speaker ID")
    speaker_threshold: float = Field(0.70, description="Umbral de similitud coseno para asignar el orador (> 0.70)")
    loop: bool = Field(False, description="Si es True y la fuente es un archivo, loopea continuamente")
    auto_start: bool = Field(False, description="Si es True, arranca automáticamente al bootear el servidor")
    prevent_repetitions: bool = Field(True, description="Si es True, detecta y elimina bucles o palabras repetidas (ej. 'con con con') del ASR")


class RoomPatch(BaseModel):
    name: Optional[str] = None
    backend: Optional[ASRBackendKind] = None
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    target_langs: Optional[List[str]] = None
    whisper_model_size: Optional[str] = None
    custom_vocabulary: Optional[List[str]] = None
    speakers: Optional[List[SpeakerProfile]] = None
    speaker_threshold: Optional[float] = None
    prevent_repetitions: Optional[bool] = None


class SettingsUpdate(BaseModel):
    gemini_api_key: Optional[str] = None
    gemini_live_model: Optional[str] = None
    gemini_translate_model: Optional[str] = None
    admin_token: Optional[str] = None


class Settings(BaseSettings):
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")
    gemini_live_model: str = Field("gemini-3.5-transcribe-live", alias="GEMINI_LIVE_MODEL")
    gemini_translate_model: str = Field("gemini-3.5-flash-lite", alias="GEMINI_TRANSLATE_MODEL")
    admin_token: str = Field("nerdearla2026", alias="ADMIN_TOKEN")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()

# Sincronización inicial con secrets/credentials.json
_stored = load_stored_credentials()
if not _stored and settings.gemini_api_key:
    _stored = {
        "gemini_api_key": settings.gemini_api_key,
        "gemini_live_model": settings.gemini_live_model,
        "gemini_translate_model": settings.gemini_translate_model,
        "admin_token": settings.admin_token
    }
    save_stored_credentials(_stored)
elif _stored:
    if _stored.get("gemini_api_key"):
        settings.gemini_api_key = _stored["gemini_api_key"]
    if _stored.get("gemini_live_model"):
        settings.gemini_live_model = _stored["gemini_live_model"]
    if _stored.get("gemini_translate_model"):
        settings.gemini_translate_model = _stored["gemini_translate_model"]
    if _stored.get("admin_token"):
        settings.admin_token = _stored["admin_token"]


def update_runtime_settings(update: SettingsUpdate) -> dict:
    """Actualiza ajustes en memoria, en secrets/credentials.json y en .env."""
    stored = load_stored_credentials()
    if update.gemini_api_key is not None:
        key_clean = update.gemini_api_key.strip()
        settings.gemini_api_key = key_clean
        stored["gemini_api_key"] = key_clean
    if update.gemini_live_model is not None:
        settings.gemini_live_model = update.gemini_live_model.strip()
        stored["gemini_live_model"] = settings.gemini_live_model
    if update.gemini_translate_model is not None:
        settings.gemini_translate_model = update.gemini_translate_model.strip()
        stored["gemini_translate_model"] = settings.gemini_translate_model
    if update.admin_token is not None:
        tok_clean = update.admin_token.strip()
        if tok_clean:
            settings.admin_token = tok_clean
            stored["admin_token"] = tok_clean

    save_stored_credentials(stored)
    return stored

