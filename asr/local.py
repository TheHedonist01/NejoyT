import asyncio
import logging
import time
from typing import AsyncGenerator, List, Optional
from asr.base import ASRBackend, ASRTranscriptionEvent

logger = logging.getLogger("asr.local")


class LocalASR(ASRBackend):
    """
    Backend de ASR Local para operación offline o sin cuota de nube.
    Diseñado para interoperar con Gemma 4 E4B o faster-whisper sobre GPU local.
    Si las librerías locales no están presentes en el entorno, opera en modo de
    emulación local para demostración y testing sin cloud.
    """

    def __init__(
        self,
        model_size: str = "base",
        device: str = "auto",
        language: str = "es",
        custom_vocabulary: Optional[List[str]] = None
    ):
        self.model_size = model_size
        self.device = device
        self.language = language
        self.custom_vocabulary = custom_vocabulary or []
        self._queue: asyncio.Queue[ASRTranscriptionEvent] = asyncio.Queue()
        self._running = False
        self._audio_buffer = bytearray()
        self._model = None

    async def start(self) -> None:
        """Inicializa el modelo local (faster-whisper si está instalado)."""
        self._running = True
        logger.info("Iniciando LocalASR (modelo=%s, idioma=%s)...", self.model_size, self.language)

        try:
            from faster_whisper import WhisperModel
            logger.info("Cargando faster-whisper en dispositivo '%s'...", self.device)
            # Carga asíncrona mediante thread pool
            loop = asyncio.get_running_loop()
            self._model = await loop.run_in_executor(
                None,
                lambda: WhisperModel(self.model_size, device="cuda" if self.device == "cuda" else "cpu", compute_type="float32")
            )
            logger.info("Modelo faster-whisper cargado con éxito.")
        except ImportError:
            logger.info("faster-whisper no instalado en este entorno. Modo LocalASR fallback activado.")
            self._model = None

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Acumula PCM (16 kHz, 16-bit mono) y procesa por bloques."""
        if not self._running:
            return

        self._audio_buffer.extend(pcm_chunk)
        # Procesar cada ~3 segundos de audio (96.000 bytes)
        if len(self._audio_buffer) >= 96000:
            chunk_to_process = bytes(self._audio_buffer[:96000])
            del self._audio_buffer[:96000]

            if self._model:
                await self._transcribe_local(chunk_to_process)
            else:
                # Fallback emulado local para testing offline
                await self._queue.put(
                    ASRTranscriptionEvent(
                        event_type="interim",
                        text="[LocalASR offline] Procesando audio local...",
                        is_final=False,
                        language=self.language
                    )
                )

    async def _transcribe_local(self, pcm_data: bytes) -> None:
        """Ejecuta inferencia local sobre el chunk de audio."""
        import numpy as np
        loop = asyncio.get_running_loop()

        def _infer():
            audio_array = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = self._model.transcribe(audio_array, language=self.language[:2])
            return " ".join(s.text.strip() for s in segments)

        try:
            text = await loop.run_in_executor(None, _infer)
            if text:
                await self._queue.put(
                    ASRTranscriptionEvent(
                        event_type="final",
                        text=text,
                        is_final=True,
                        language=self.language
                    )
                )
        except Exception as e:
            logger.error("Error en transcripción local: %s", e)

    async def events(self) -> AsyncGenerator[ASRTranscriptionEvent, None]:
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def stop(self) -> None:
        self._running = False
        self._audio_buffer.clear()
        logger.info("LocalASR detenido.")
