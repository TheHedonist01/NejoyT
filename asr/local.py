import asyncio
import logging
import time
from typing import AsyncGenerator, List, Optional
import numpy as np

from asr.base import ASRBackend, ASRTranscriptionEvent
from asr.device import detect_compute_device, setup_accelerator_paths

logger = logging.getLogger("asr.local")


class LocalASR(ASRBackend):
    """
    Backend de ASR 100% Local / Offline y agnóstico de hardware.
    Detecta automáticamente el mejor dispositivo (CUDA, ROCm, MPS, CPU).
    Procesa audio en fragmentos de 100ms con segmentación Silero VAD
    para emitir transcripciones y traducciones con mínima latencia y cero llamadas de red.
    """

    def __init__(
        self,
        model_size: str = "base",
        device: Optional[str] = None,
        language: Optional[str] = "auto",
        custom_vocabulary: Optional[List[str]] = None,
        max_queue_chunks: int = 50,  # 50 chunks de 100ms = 5s de buffer máximo
        silence_threshold_sec: float = 0.40,  # 400ms de silencio para corte natural de frase
        min_speech_duration_sec: float = 0.50, # Mínimo 500ms de voz para transcribir
        max_speech_duration_sec: float = 6.0   # Máximo 6s continuos antes de forzar corte
    ):
        self.model_size = model_size
        self.user_device = device
        self.actual_device = "cpu"
        self.compute_type = "int8"
        self.device_index = 0
        self.language = language or "auto"
        self.custom_vocabulary = custom_vocabulary or []

        self.silence_threshold_sec = silence_threshold_sec
        self.min_speech_duration_sec = min_speech_duration_sec
        self.max_speech_duration_sec = max_speech_duration_sec

        self._queue: asyncio.Queue[Optional[ASRTranscriptionEvent]] = asyncio.Queue()
        self._incoming_audio: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_queue_chunks)
        self._running = False
        self._model = None
        self._vad = None
        self._worker_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Inicializa y calienta (warm-up) el modelo ASR y Silero VAD en el hardware detectado."""
        if self._running:
            return

        self._running = True
        setup_accelerator_paths()

        # Detección de hardware automática o según preferencia del usuario
        if not self.user_device or self.user_device in ("auto", "best"):
            self.actual_device, self.compute_type, self.device_index = detect_compute_device()
        else:
            self.actual_device = self.user_device
            self.compute_type = "float16" if self.actual_device in ("cuda", "mps") else "int8"
            self.device_index = 0

        logger.info(
            "Backend local corriendo en: %s (%s) | Modelo Whisper: '%s'",
            self.actual_device.upper(), self.compute_type, self.model_size
        )

        loop = asyncio.get_running_loop()

        def _load_and_warmup():
            from faster_whisper import WhisperModel
            from faster_whisper.vad import get_vad_model

            # Carga de Whisper
            try:
                model = WhisperModel(
                    self.model_size,
                    device=self.actual_device,
                    device_index=self.device_index,
                    compute_type=self.compute_type
                )
            except Exception as e:
                logger.warning(
                    "Fallo al cargar en dispositivo '%s' (%s): %s. Fallback a CPU (int8).",
                    self.actual_device, self.compute_type, e
                )
                self.actual_device = "cpu"
                self.compute_type = "int8"
                model = WhisperModel(self.model_size, device="cpu", compute_type="int8")

            # Carga de Silero VAD incorporado
            vad = get_vad_model()

            # Warm-up: inferencia rápida con audio dummy para eliminar latencia en la primera frase
            dummy_pcm = np.zeros(16000, dtype=np.float32)  # 1 segundo de silencio
            try:
                # Warmup VAD
                vad(dummy_pcm[:512])
                # Warmup Whisper
                list(model.transcribe(dummy_pcm, language="es", beam_size=1, temperature=0.0)[0])
            except Exception as warmup_err:
                logger.debug("Warmup finalizado con nota: %s", warmup_err)

            return model, vad

        try:
            self._model, self._vad = await loop.run_in_executor(None, _load_and_warmup)
            logger.info("Warm-up completado. LocalASR listo para transcripción en vivo.")
        except Exception as e:
            logger.error("Error crítico inicializando LocalASR: %s", e, exc_info=True)
            self._model = None

        self._worker_task = asyncio.create_task(self._process_stream(), name="local-asr-stream")

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """
        Encola chunks PCM (~100ms) con descarte por backpressure
        para evitar acumular retardo si el procesador se congestiona.
        """
        if not self._running:
            return

        try:
            self._incoming_audio.put_nowait(pcm_chunk)
        except asyncio.QueueFull:
            # Backpressure: descartar chunk más viejo para priorizar tiempo real
            try:
                self._incoming_audio.get_nowait()
                self._incoming_audio.put_nowait(pcm_chunk)
            except Exception:
                pass

    async def _process_stream(self) -> None:
        """
        Bucle de streaming en tiempo real:
        - Ingiere chunks de 100ms
        - Evalúa actividad de voz (Silero VAD)
        - Acumula voz activa y emite hipótesis preliminares (interim)
        - Al detectar silencio natural (>= silence_threshold), finaliza y emite (final)
        """
        loop = asyncio.get_running_loop()
        speech_buffer: List[np.ndarray] = []
        is_speaking = False
        speech_start_time = 0.0
        last_speech_time = 0.0
        last_interim_time = 0.0

        initial_prompt = ", ".join(self.custom_vocabulary) if self.custom_vocabulary else None
        target_lang = None if self.language in ("auto", "auto-detect", None, "") else self.language[:2]

        while self._running:
            try:
                chunk_bytes = await self._incoming_audio.get()
                if chunk_bytes is None:
                    break

                now = time.time()
                # Conversión a float32 normalizado (-1.0 a 1.0)
                samples = np.frombuffer(chunk_bytes, dtype=np.int16).astype(np.float32) / 32768.0

                # Evaluar actividad de voz con Silero VAD en bloques de 512 muestras
                has_speech = False
                if self._vad and len(samples) >= 512:
                    # Evaluar los primeros 512 y últimos 512
                    prob1 = self._vad(samples[:512])[0]
                    prob2 = self._vad(samples[-512:])[0]
                    has_speech = max(prob1, prob2) > 0.40
                else:
                    # Fallback por energía RMS si VAD no estuviera disponible
                    rms = np.sqrt(np.mean(samples ** 2))
                    has_speech = rms > 0.015

                if has_speech:
                    if not is_speaking:
                        is_speaking = True
                        speech_start_time = now
                        speech_buffer = []

                    last_speech_time = now
                    speech_buffer.append(samples)

                    speech_duration = now - speech_start_time

                    # Emisión de hipótesis parcial (interim) cada 600ms si el turno de habla se alarga
                    if speech_duration >= 1.0 and (now - last_interim_time) >= 0.60:
                        last_interim_time = now
                        current_audio = np.concatenate(speech_buffer)

                        def _run_interim():
                            if not self._model:
                                return ""
                            segments, _ = self._model.transcribe(
                                current_audio,
                                language=target_lang,
                                initial_prompt=initial_prompt,
                                beam_size=1,
                                temperature=0.0,
                                without_timestamps=True
                            )
                            return " ".join(s.text.strip() for s in segments if s.text.strip())

                        interim_text = await loop.run_in_executor(None, _run_interim)
                        if interim_text:
                            await self._queue.put(
                                ASRTranscriptionEvent(
                                    event_type="interim",
                                    text=interim_text,
                                    is_final=False,
                                    language=target_lang or "auto"
                                )
                            )

                    # Forzar corte si el orador habla continuamente sin pausa por más de max_speech_duration_sec
                    if speech_duration >= self.max_speech_duration_sec:
                        await self._finalize_phrase(speech_buffer, speech_start_time, target_lang, initial_prompt, loop)
                        speech_buffer = []
                        is_speaking = False

                else:
                    # Chunk de silencio
                    if is_speaking:
                        silence_duration = now - last_speech_time
                        speech_duration = last_speech_time - speech_start_time

                        # Si el silencio supera el umbral y hubo habla suficiente, finalizamos la frase
                        if silence_duration >= self.silence_threshold_sec:
                            if speech_duration >= self.min_speech_duration_sec and speech_buffer:
                                await self._finalize_phrase(speech_buffer, speech_start_time, target_lang, initial_prompt, loop)
                            speech_buffer = []
                            is_speaking = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error en streaming de LocalASR: %s", e)

        # Fin de la transmisión: vaciar buffer remanente si existiera
        if is_speaking and speech_buffer:
            await self._finalize_phrase(speech_buffer, speech_start_time, target_lang, initial_prompt, loop)

        await self._queue.put(None)

    async def _finalize_phrase(
        self,
        speech_buffer: List[np.ndarray],
        speech_start_time: float,
        target_lang: Optional[str],
        initial_prompt: Optional[str],
        loop: asyncio.AbstractEventLoop
    ) -> None:
        """Transcribe la frase finalizada, mide latencia y emite el evento final."""
        if not speech_buffer or not self._model:
            return

        audio_data = np.concatenate(speech_buffer)
        t_inference_start = time.time()

        def _transcribe():
            # Detección automática del idioma (si target_lang es None)
            segments, info = self._model.transcribe(
                audio_data,
                language=target_lang,
                initial_prompt=initial_prompt,
                beam_size=1,
                temperature=0.0
            )
            text = " ".join(s.text.strip() for s in segments if s.text.strip())
            detected_lang = info.language or (target_lang or "es")
            prob = info.language_probability or 1.0
            return text, detected_lang, prob

        try:
            text, detected_lang, lang_prob = await loop.run_in_executor(None, _transcribe)
            t_now = time.time()
            inference_ms = (t_now - t_inference_start) * 1000
            total_latency_ms = (t_now - speech_start_time) * 1000

            if text:
                logger.info(
                    "[LocalASR | %s] Frase final ('%s' prob=%.2f) en %.1fms (latencia total: %.1fms): \"%s\"",
                    self.actual_device.upper(), detected_lang, lang_prob, inference_ms, total_latency_ms, text
                )
                await self._queue.put(
                    ASRTranscriptionEvent(
                        event_type="final",
                        text=text,
                        is_final=True,
                        language=detected_lang,
                        timestamp=t_now
                    )
                )
        except Exception as e:
            logger.error("Error transcribiendo frase final en LocalASR: %s", e)

    async def events(self) -> AsyncGenerator[ASRTranscriptionEvent, None]:
        """Generador asíncrono de eventos de transcripción."""
        while True:
            ev = await self._queue.get()
            if ev is None:
                break
            yield ev

    async def stop(self) -> None:
        """Detiene el backend y libera recursos."""
        self._running = False
        try:
            self._incoming_audio.put_nowait(None)  # type: ignore
        except Exception:
            pass

        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        logger.info("LocalASR detenido.")
