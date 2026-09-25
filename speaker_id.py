"""
Módulo de Speaker ID (Identificación de Disertantes por Huella Vocal)
Basado en ECAPA-TDNN exportado a ONNX (192-dim embeddings con normalización L2).
Ejecución local ultrarrápida sobre CPU vía onnxruntime, sin dependencias de GPU ni red.
"""

import logging
import os
import subprocess
import threading
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger("nerdearla.speaker_id")

try:
    import speakeronnx
    HAS_SPEAKER_ONNX = True
except ImportError:
    HAS_SPEAKER_ONNX = False
    logger.warning("Librería 'speakeronnx' no disponible. Speaker ID funcionará en modo degradado.")


class SpeakerIdentifier:
    """
    Motor de identificación de oradores basado en huella acústica (voiceprint).
    Extrae embeddings de 192 dimensiones y evalúa similitud coseno en tiempo real.
    """

    def __init__(self, model_name: str = "wespeaker-ecapa512"):
        self.model_name = model_name
        self._embedder = None
        self._lock = threading.Lock()
        self._is_ready = False

    def _ensure_loaded(self) -> bool:
        """Inicializa de forma lazy el modelo ONNX ECAPA-TDNN."""
        if self._is_ready and self._embedder is not None:
            return True

        if not HAS_SPEAKER_ONNX:
            return False

        with self._lock:
            if self._embedder is None:
                try:
                    logger.info("Cargando modelo ONNX de Speaker ID (%s)...", self.model_name)
                    self._embedder = speakeronnx.SpeakerEmbedder(
                        model=self.model_name,
                        providers=["CPUExecutionProvider"]
                    )
                    self._is_ready = True
                    logger.info("Modelo ONNX ECAPA-TDNN listo. Dimensión de embedding: %d", getattr(self._embedder, "embed_dim", 192))
                except Exception as e:
                    logger.error("Error cargando modelo ONNX para Speaker ID: %s", e)
                    self._embedder = None
                    self._is_ready = False
                    return False
        return True

    def load_audio_from_file(self, file_path: str, max_duration_sec: float = 30.0) -> Optional[np.ndarray]:
        """
        Extrae audio PCM mono 16 kHz normalizado a [-1.0, 1.0] desde cualquier
        archivo de audio o video (MP4, MP3, WAV, MKV) usando FFmpeg.
        """
        if not os.path.exists(file_path):
            logger.warning("Archivo de muestra de voz no encontrado: %s", file_path)
            return None

        cmd = [
            "ffmpeg", "-y", "-v", "quiet",
            "-t", str(max_duration_sec),
            "-i", file_path,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            "pipe:1"
        ]
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            raw_bytes = proc.stdout
            if not raw_bytes:
                return None
            audio_i16 = np.frombuffer(raw_bytes, dtype=np.int16)
            return audio_i16.astype(np.float32) / 32768.0
        except Exception as e:
            logger.warning("Error extrayendo audio con FFmpeg para muestra '%s': %s", file_path, e)
            return None

    def pcm_to_float32(self, pcm_bytes: bytes) -> np.ndarray:
        """Convierte bytes PCM 16 kHz 16-bit mono a un vector float32 normalizado."""
        if not pcm_bytes:
            return np.empty(0, dtype=np.float32)
        arr = np.frombuffer(pcm_bytes, dtype=np.int16)
        return arr.astype(np.float32) / 32768.0

    def extract_embedding(self, source: Union[str, bytes, np.ndarray]) -> Optional[np.ndarray]:
        """
        Genera el vector de embedding de 192 dimensiones con normalización L2.
        Acepta una ruta a un archivo, bytes PCM crudos (16kHz s16le) o un array numpy float32.
        """
        if not self._ensure_loaded() or self._embedder is None:
            return None

        try:
            if isinstance(source, str):
                audio = self.load_audio_from_file(source)
                if audio is None or len(audio) < 1600:  # Mínimo 100 ms
                    return None
            elif isinstance(source, bytes):
                audio = self.pcm_to_float32(source)
                if len(audio) < 1600:
                    return None
            elif isinstance(source, np.ndarray):
                audio = source
                if audio.dtype != np.float32:
                    audio = audio.astype(np.float32)
                if len(audio) < 1600:
                    return None
            else:
                return None

            # Inferencia ONNX
            emb = self._embedder.embed(audio)
            norm = np.linalg.norm(emb)
            if norm > 1e-6:
                emb = emb / norm
            return emb
        except Exception as e:
            logger.warning("Error extrayendo embedding de voz: %s", e)
            return None

    @staticmethod
    def cosine_similarity(vec1: np.ndarray, vec2: np.ndarray) -> float:
        """Calcula la similitud coseno entre dos vectores normalizados."""
        dot = float(np.dot(vec1, vec2))
        norm1 = float(np.linalg.norm(vec1))
        norm2 = float(np.linalg.norm(vec2))
        if norm1 <= 1e-6 or norm2 <= 1e-6:
            return 0.0
        return dot / (norm1 * norm2)

    def identify_speaker(
        self,
        audio_chunk: Union[bytes, np.ndarray],
        enrolled_profiles: Dict[str, np.ndarray],
        threshold: float = 0.70
    ) -> Tuple[Optional[str], float]:
        """
        Compara el fragmento de audio contra los perfiles enrolados en memoria.
        Retorna (nombre_orador, score) si el score supera el umbral (> 0.70); de lo contrario (None, score).
        """
        if not enrolled_profiles:
            return None, 0.0

        emb = self.extract_embedding(audio_chunk)
        if emb is None:
            return None, 0.0

        best_speaker: Optional[str] = None
        best_score: float = -1.0

        for speaker_name, ref_emb in enrolled_profiles.items():
            if ref_emb is None:
                continue
            score = self.cosine_similarity(emb, ref_emb)
            if score > best_score:
                best_score = score
                best_speaker = speaker_name

        if best_speaker is not None and best_score >= threshold:
            return best_speaker, float(best_score)

        return None, float(max(best_score, 0.0))


# Instancia singleton para todo el proceso
speaker_identifier = SpeakerIdentifier()
