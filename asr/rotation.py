import asyncio
import logging
import time
from typing import AsyncGenerator, List, Optional

from asr.base import ASRBackend, ASRTranscriptionEvent
from asr.gemini_live import GeminiLiveASR

logger = logging.getLogger("asr.rotation")


class SeamlessRotationASR(ASRBackend):
    """
    Gestor de rotación con solape transparente (Seamless Overlap Rotation).
    Supera el límite estricto de 10 minutos de gemini-3.5-transcribe-live
    abriendo una sesión paralela B a los 8:30 min, enviando audio a ambas
    y promoviendo B al recibir el primer evento final después de los 9:00 min.
    """

    def __init__(
        self,
        language: Optional[str] = "es-419",
        custom_vocabulary: Optional[List[str]] = None,
        overlap_start_sec: float = 510.0,      # 8:30 min por defecto
        rotation_threshold_sec: float = 540.0   # 9:00 min por defecto
    ):
        self.language = language
        self.custom_vocabulary = custom_vocabulary or []
        self.overlap_start_sec = overlap_start_sec
        self.rotation_threshold_sec = rotation_threshold_sec

        self._active_backend: Optional[GeminiLiveASR] = None
        self._next_backend: Optional[GeminiLiveASR] = None
        self._active_start_time: float = 0.0
        self._rotation_counter: int = 1

        self._in_overlap: bool = False
        self._running: bool = False
        self._stop_event = asyncio.Event()

        self._output_event_queue: asyncio.Queue[Optional[ASRTranscriptionEvent]] = asyncio.Queue()
        self._active_listener_task: Optional[asyncio.Task] = None
        self._next_listener_task: Optional[asyncio.Task] = None
        self._next_event_buffer: asyncio.Queue[ASRTranscriptionEvent] = asyncio.Queue()

    def _create_backend_instance(self, session_num: int) -> GeminiLiveASR:
        return GeminiLiveASR(
            language=self.language,
            custom_vocabulary=self.custom_vocabulary,
            session_id=f"session-{session_num}"
        )

    async def start(self) -> None:
        """Inicia la sesión A primaria."""
        if self._running:
            return

        self._running = True
        self._stop_event.clear()
        self._rotation_counter = 1
        self._active_backend = self._create_backend_instance(self._rotation_counter)
        await self._active_backend.start()
        self._active_start_time = time.time()

        self._active_listener_task = asyncio.create_task(
            self._listen_to_active(self._active_backend),
            name=f"rotation-active-listener-{self._rotation_counter}"
        )
        logger.info(
            "SeamlessRotationASR iniciado. Sesión 1 activa. Solape programado para t=%.1fs, rotación tras t=%.1fs",
            self.overlap_start_sec, self.rotation_threshold_sec
        )

    async def _listen_to_active(self, backend: GeminiLiveASR) -> None:
        """Escucha eventos de la sesión activa y detecta el momento de conmutación."""
        try:
            async for event in backend.events():
                # Transmitir a la cola de salida visible para la audiencia
                await self._output_event_queue.put(event)

                # Verificar si alcanzamos el umbral para rotar
                elapsed = time.time() - self._active_start_time
                if (
                    self._in_overlap
                    and self._next_backend is not None
                    and elapsed >= self.rotation_threshold_sec
                    and event.is_final
                ):
                    logger.info(
                        "Punto de corte alcanzado (t=%.1fs >= %.1fs y evento final recibido). Conmutando sesiones...",
                        elapsed, self.rotation_threshold_sec
                    )
                    await self._switch_to_next()
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Error en listener de sesión activa: %s", e)

    async def _listen_to_next(self, backend: GeminiLiveASR) -> None:
        """Descarta o retiene temporalmente los eventos de B hasta que se promueva."""
        try:
            async for event in backend.events():
                if not self._in_overlap:
                    # Una vez promovida, sus eventos van directo a la salida
                    await self._output_event_queue.put(event)
                else:
                    # En solape: se descartan interinos y finales anteriores al corte
                    # para evitar duplicar el texto que la sesión A ya emitió.
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Error en listener de sesión standby: %s", e)

    async def _switch_to_next(self) -> None:
        """Promueve la sesión B a activa y descarta la sesión A anterior."""
        old_backend = self._active_backend
        self._active_backend = self._next_backend
        self._next_backend = None
        self._in_overlap = False
        self._active_start_time = time.time()
        self._rotation_counter += 1

        logger.info(
            "Rotación exitosa: Sesión %d promovida a ACTIVA. Deteniendo sesión previa...",
            self._rotation_counter
        )

        # Reasignar listener de la nueva sesión activa
        self._active_listener_task = self._next_listener_task
        self._next_listener_task = None

        # Detener la sesión vieja limpiamente en background
        if old_backend:
            asyncio.create_task(old_backend.stop())

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """
        Envía audio a la sesión activa y, si está en ventana de solape,
        también duplica el audio hacia la sesión en espera.
        """
        if not self._running or self._active_backend is None:
            return

        elapsed = time.time() - self._active_start_time

        # 1. Comprobar si debemos abrir la sesión B en paralelo
        if (
            elapsed >= self.overlap_start_sec
            and self._next_backend is None
            and not self._in_overlap
        ):
            logger.info(
                "Iniciando ventana de solape (t=%.1fs >= %.1fs). Conectando Sesión %d en paralelo...",
                elapsed, self.overlap_start_sec, self._rotation_counter + 1
            )
            self._in_overlap = True
            self._next_backend = self._create_backend_instance(self._rotation_counter + 1)
            await self._next_backend.start()
            self._next_listener_task = asyncio.create_task(
                self._listen_to_next(self._next_backend),
                name=f"rotation-next-listener-{self._rotation_counter + 1}"
            )

        # 2. Enviar chunk a la sesión activa
        await self._active_backend.send_audio(pcm_chunk)

        # 3. Enviar copia del chunk a la sesión standby durante el solape
        if self._in_overlap and self._next_backend is not None:
            await self._next_backend.send_audio(pcm_chunk)

    async def events(self) -> AsyncGenerator[ASRTranscriptionEvent, None]:
        """Generador unificado de eventos continuos para el orquestador y la audiencia."""
        while self._running:
            event = await self._output_event_queue.get()
            if event is None:
                break
            yield event

    async def stop(self) -> None:
        """Detiene ambas sesiones limpiamente."""
        self._running = False
        self._stop_event.set()

        if self._active_listener_task:
            self._active_listener_task.cancel()
        if self._next_listener_task:
            self._next_listener_task.cancel()

        tasks = []
        if self._active_backend:
            tasks.append(self._active_backend.stop())
        if self._next_backend:
            tasks.append(self._next_backend.stop())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        await self._output_event_queue.put(None)
        logger.info("SeamlessRotationASR detenido completamente.")
