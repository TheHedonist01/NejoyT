import asyncio
import logging
from typing import AsyncGenerator, List, Optional
from google import genai
from google.genai import types

from asr.base import ASRBackend, ASRTranscriptionEvent
from config import settings

logger = logging.getLogger("asr.gemini_live")


class GeminiLiveASR(ASRBackend):
    """
    Cliente de streaming para gemini-3.5-transcribe-live
    mediante WebSocket bidireccional asíncrono.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        language: Optional[str] = "es-419",
        custom_vocabulary: Optional[List[str]] = None,
        session_id: Optional[str] = None
    ):
        self.model = model or settings.gemini_live_model
        self.language = language
        self.custom_vocabulary = custom_vocabulary or []
        self.session_id = session_id or "session-live"

        self._audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._event_queue: asyncio.Queue[Optional[ASRTranscriptionEvent]] = asyncio.Queue()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._stop_requested = asyncio.Event()

    def _build_config(self) -> types.LiveConnectConfig:
        lang_codes = [self.language] if self.language else []
        return types.LiveConnectConfig(
            response_modalities=["TEXT"],
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=lang_codes,
                custom_vocabulary=self.custom_vocabulary,
                mode="SMART"
            )
        )

    async def start(self) -> None:
        """Inicia el worker de conexión en segundo plano."""
        if self._running:
            return

        self._running = True
        self._stop_requested.clear()
        self._worker_task = asyncio.create_task(
            self._connection_manager(),
            name=f"gemini-live-worker-{self.session_id}"
        )
        logger.info("[%s] GeminiLiveASR iniciado para modelo '%s'", self.session_id, self.model)

    async def _connection_manager(self) -> None:
        """Maneja el ciclo de vida de la conexión WebSocket con reintentos."""
        client = genai.Client(api_key=settings.gemini_api_key)
        config = self._build_config()

        backoff = 1.0
        max_backoff = 16.0

        while self._running and not self._stop_requested.is_set():
            try:
                logger.info("[%s] Conectando WebSocket a %s...", self.session_id, self.model)
                async with client.aio.live.connect(model=self.model, config=config) as session:
                    logger.info("[%s] WebSocket conectado exitosamente.", self.session_id)
                    backoff = 1.0  # Reset backoff tras conexión exitosa

                    send_task = asyncio.create_task(self._send_loop(session))
                    recv_task = asyncio.create_task(self._receive_loop(session))

                    # Mantener la sesión abierta hasta que se solicite detención,
                    # o el receptor termine (cierre de socket / error del servidor).
                    # send_task finalizando (ej. fin de archivo) no debe matar al receptor.
                    while not self._stop_requested.is_set():
                        if recv_task.done():
                            exc = recv_task.exception()
                            if exc:
                                raise exc
                            break
                        if send_task.done():
                            exc = send_task.exception()
                            if exc:
                                raise exc
                        await asyncio.sleep(0.1)

                    send_task.cancel()
                    recv_task.cancel()
                    await asyncio.gather(send_task, recv_task, return_exceptions=True)

                    if self._stop_requested.is_set():
                        break


            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[%s] Error o desconexión en Gemini Live: %s", self.session_id, e)
                if self._stop_requested.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        # Señalizar fin de eventos
        await self._event_queue.put(None)
        self._running = False
        logger.info("[%s] GeminiLiveASR finalizado.", self.session_id)

    async def _send_loop(self, session) -> None:
        """Envía chunks PCM recibidos por send_audio a la sesión de Gemini."""
        while self._running and not self._stop_requested.is_set():
            chunk = await self._audio_queue.get()
            if chunk is None:
                # Señal de fin de flujo
                try:
                    await session.send_realtime_input(audio_stream_end=True)
                except Exception:
                    pass
                break

            try:
                await session.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type="audio/pcm;rate=16000"
                    )
                )
            except Exception as e:
                logger.error("[%s] Error enviando audio chunk: %s", self.session_id, e)
                raise

    async def _receive_loop(self, session) -> None:
        """Lee mensajes del servidor y emite ASRTranscriptionEvent."""
        try:
            async for response in session.receive():
                server_content = response.server_content
                if not server_content:
                    continue

                # 1. Hipótesis parcial interina (baja latencia)
                if server_content.interim_input_transcription:
                    text = server_content.interim_input_transcription.text
                    if text and text.strip():
                        event = ASRTranscriptionEvent(
                            event_type="interim",
                            text=text.strip(),
                            is_final=False,
                            language=self.language
                        )
                        await self._event_queue.put(event)

                # 2. Transcripción autoritativa final al terminar la frase
                if server_content.input_transcription:
                    text = server_content.input_transcription.text
                    if text and text.strip():
                        event = ASRTranscriptionEvent(
                            event_type="final",
                            text=text.strip(),
                            is_final=True,
                            language=self.language
                        )
                        await self._event_queue.put(event)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            err_str = str(e)
            if "1000" in err_str or self._stop_requested.is_set():
                logger.debug("[%s] WebSocket cerrado normalmente: %s", self.session_id, e)
            else:
                logger.warning("[%s] Excepción en receive loop: %s", self.session_id, e)
                raise

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Encola el bloque PCM para ser enviado por WebSocket."""
        if self._running:
            await self._audio_queue.put(pcm_chunk)

    async def finish_audio_stream(self) -> None:
        """Notifica fin de audio para vaciar buffers y forzar emisión de frases finales."""
        if self._running:
            await self._audio_queue.put(None)

    async def events(self) -> AsyncGenerator[ASRTranscriptionEvent, None]:
        """Consume eventos de transcripción en tiempo real."""
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

    async def stop(self) -> None:
        """Detiene la sesión y cierra el WebSocket."""
        self._stop_requested.set()
        await self._audio_queue.put(None)
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._running = False
