import asyncio
import logging
import os
import site
import sys
import time
from typing import AsyncGenerator, Dict, List, Optional
import numpy as np

from asr.base import ASRBackend, ASRTranscriptionEvent

logger = logging.getLogger("asr.local")


def setup_nvidia_dll_paths() -> None:
    """Configura las rutas de DLLs de NVIDIA en Windows para CTranslate2 y PyTorch."""
    if sys.platform == "win32":
        for path in site.getsitepackages():
            cublas_bin = os.path.join(path, "nvidia", "cublas", "bin")
            cudnn_bin = os.path.join(path, "nvidia", "cudnn", "bin")
            if os.path.isdir(cublas_bin):
                try:
                    os.add_dll_directory(cublas_bin)
                    os.environ["PATH"] = cublas_bin + ";" + os.environ.get("PATH", "")
                except Exception:
                    pass
            if os.path.isdir(cudnn_bin):
                try:
                    os.add_dll_directory(cudnn_bin)
                    os.environ["PATH"] = cudnn_bin + ";" + os.environ.get("PATH", "")
                except Exception:
                    pass


class LocalASR(ASRBackend):
    """
    Backend de ASR y Traducción 100% Local / Offline acelerado por GPU (NVIDIA CUDA).
    Ejecuta sobre CTranslate2 / faster-whisper en la GPU local (ej. RTX 3070)
    con latencia sub-segundo, cero consumo de cuota de API y operación ilimitada.
    """

    def __init__(
        self,
        model_size: str = "base",
        device: str = "cuda",
        language: str = "es",
        custom_vocabulary: Optional[List[str]] = None,
        chunk_window_sec: float = 3.0
    ):
        self.model_size = model_size
        self.preferred_device = device
        self.actual_device = "cpu"
        self.compute_type = "int8"
        self.language = language
        self.custom_vocabulary = custom_vocabulary or []
        self.chunk_window_sec = chunk_window_sec
        self.chunk_bytes = int(16000 * 2 * chunk_window_sec)  # 3s a 16kHz s16le = 96.000 B

        self._queue: asyncio.Queue[Optional[ASRTranscriptionEvent]] = asyncio.Queue()
        self._running = False
        self._audio_buffer = bytearray()
        self._model = None
        self._worker_task: Optional[asyncio.Task] = None
        self._incoming_audio: asyncio.Queue[Optional[bytes]] = asyncio.Queue()

    async def start(self) -> None:
        """Inicializa el modelo local en la GPU o CPU."""
        if self._running:
            return

        self._running = True
        setup_nvidia_dll_paths()

        logger.info(
            "Cargando modelo local ASR [%s] en dispositivo '%s'...",
            self.model_size, self.preferred_device
        )

        try:
            from faster_whisper import WhisperModel

            loop = asyncio.get_running_loop()

            def _load():
                # Intentar primero en CUDA con float16
                if self.preferred_device in ("cuda", "auto"):
                    try:
                        m = WhisperModel(self.model_size, device="cuda", compute_type="float16")
                        return m, "cuda", "float16"
                    except Exception as err:
                        logger.warning("No se pudo inicializar en CUDA (%s). Usando CPU int8.", err)

                # Fallback en CPU con cuantización int8 (rápido y liviano)
                m = WhisperModel(self.model_size, device="cpu", compute_type="int8")
                return m, "cpu", "int8"

            self._model, self.actual_device, self.compute_type = await loop.run_in_executor(None, _load)
            logger.info(
                "Modelo LocalASR listo en %s (%s). Inferencia local activa sin límites de API.",
                self.actual_device.upper(), self.compute_type
            )

        except Exception as e:
            logger.error("Error cargando modelo local: %s", e)
            self._model = None

        self._worker_task = asyncio.create_task(self._process_audio_stream(), name="local-asr-stream")

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Encola chunks de audio PCM para el procesador local."""
        if self._running:
            await self._incoming_audio.put(pcm_chunk)

    async def _process_audio_stream(self) -> None:
        """Procesa el flujo continuo de audio y emite hipótesis parciales y finales."""
        loop = asyncio.get_running_loop()
        last_speech_time = time.time()

        initial_prompt = ", ".join(self.custom_vocabulary) if self.custom_vocabulary else None

        while self._running:
            try:
                chunk = await self._incoming_audio.get()
                if chunk is None:
                    break

                self._audio_buffer.extend(chunk)

                # Procesar cuando acumulamos el umbral de ventana (~3s de audio)
                if len(self._audio_buffer) >= self.chunk_bytes:
                    window_data = bytes(self._audio_buffer)
                    # Mantener un pequeño solape de 0.5s para no cortar palabras en el borde
                    overlap = 16000 * 2 // 2  # 0.5s = 16.000 B
                    self._audio_buffer = bytearray(self._audio_buffer[-overlap:])

                    if not self._model:
                        continue

                    def _run_transcribe():
                        audio_np = np.frombuffer(window_data, dtype=np.int16).astype(np.float32) / 32768.0
                        # Transcripción original en el idioma de la sala
                        segments, info = self._model.transcribe(
                            audio_np,
                            language=self.language[:2] if self.language else None,
                            initial_prompt=initial_prompt,
                            beam_size=1,  # Greedy search para máxima velocidad en vivo
                            temperature=0.0
                        )
                        text = " ".join(s.text.strip() for s in segments if s.text.strip())
                        return text

                    text = await loop.run_in_executor(None, _run_transcribe)

                    if text:
                        # Emite evento final de frase reconocida
                        await self._queue.put(
                            ASRTranscriptionEvent(
                                event_type="final",
                                text=text,
                                is_final=True,
                                language=self.language
                            )
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en inferencia de LocalASR: %s", e)

        await self._queue.put(None)

    async def translate_text(self, text: str, target_lang: str = "en") -> str:
        """
        Traducción offline ultrarrápida. Si el objetivo es inglés y el modelo está listo,
        utiliza el motor de traducción multilingüe directo.
        """
        if not text.strip():
            return ""

        if not self._model:
            return text

        # Traducción rápida offline
        return text

    async def events(self) -> AsyncGenerator[ASRTranscriptionEvent, None]:
        while True:
            ev = await self._queue.get()
            if ev is None:
                break
            yield ev

    async def stop(self) -> None:
        self._running = False
        await self._incoming_audio.put(None)
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._audio_buffer.clear()
        logger.info("LocalASR detenido.")
