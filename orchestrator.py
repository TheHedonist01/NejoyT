import asyncio
import logging
import os
import time
import yaml
from typing import Dict, List, Optional
from pydantic import BaseModel

from audio.source import AudioSource
from asr.rotation import SeamlessRotationASR
from translate.gemini_text import GeminiTranslator
from bus import event_bus, SubtitleEvent
from store import subtitle_store
from config import RoomConfig, RoomPatch, RoomState, SourceKind

logger = logging.getLogger("nerdearla.orchestrator")


class RoomStatus(BaseModel):
    id: str
    name: str
    kind: SourceKind
    source_uri: str
    source_lang: str
    target_langs: List[str]
    custom_vocabulary: List[str]
    state: RoomState
    is_running: bool
    subscribers: int
    subtitles_count: int
    errors_count: int
    started_at: Optional[float] = None
    uptime_seconds: Optional[int] = None


class SessionWorker:
    """
    Trabajador asíncrono que encapsula el ciclo de vida y el pipeline de una sala:
    AudioSource (FFmpeg) -> SeamlessRotationASR -> GeminiTranslator -> EventBus + SubtitleStore
    """

    def __init__(self, room: RoomConfig):
        self.room = room
        self.state: RoomState = RoomState.STOPPED
        self.is_running: bool = False
        self.errors_count: int = 0
        self.started_at: Optional[float] = None
        self._tasks: List[asyncio.Task] = []
        self._stop_event = asyncio.Event()

        self.audio_source: Optional[AudioSource] = None
        self.asr: Optional[SeamlessRotationASR] = None
        self.translator = GeminiTranslator(
            custom_glossary=room.custom_vocabulary
        )

    def _set_state(self, new_state: RoomState, reason: str = "") -> None:
        """Transición explícita de estado y emisión a suscriptores WebSocket."""
        old_state = self.state
        self.state = new_state
        logger.info("[%s] Transición de estado: %s -> %s (%s)", self.room.id, old_state.value, new_state.value, reason)

        event_bus.publish(
            self.room.id,
            SubtitleEvent(
                room_id=self.room.id,
                event_type="status",
                text=f"Sala en estado {new_state.value}",
                language=self.room.source_lang,
                metadata={"status": new_state.value, "reason": reason}
            )
        )

    def _on_source_degraded(self) -> None:
        if self.state == RoomState.ACTIVE:
            self._set_state(RoomState.DEGRADED, "Reconectando fuente de audio...")

    def _on_source_recovered(self) -> None:
        if self.state == RoomState.DEGRADED:
            self._set_state(RoomState.ACTIVE, "Fuente de audio restablecida")

    async def start(self) -> None:
        """Inicia el pipeline de la sala de forma aislada."""
        if self.is_running or self.state in (RoomState.STARTING, RoomState.ACTIVE):
            logger.warning("[%s] Intento de iniciar worker que ya está activo o arrancando.", self.room.id)
            return

        self.is_running = True
        self._stop_event.clear()
        self.started_at = time.time()
        self._set_state(RoomState.STARTING, "Inicializando conexiones y ASR")
        subtitle_store.start_room_clock(self.room.id)

        try:
            self.audio_source = AudioSource(
                source_uri=self.room.source_uri,
                kind=self.room.kind,
                loop=self.room.loop,
                on_degraded=self._on_source_degraded,
                on_recovered=self._on_source_recovered
            )

            self.asr = SeamlessRotationASR(
                language=self.room.source_lang,
                custom_vocabulary=self.room.custom_vocabulary
            )

            await self.asr.start()

            feeder_task = asyncio.create_task(
                self._audio_feeder(),
                name=f"worker-feeder-{self.room.id}"
            )
            processor_task = asyncio.create_task(
                self._event_processor(),
                name=f"worker-processor-{self.room.id}"
            )
            self._tasks = [feeder_task, processor_task]
            self._set_state(RoomState.ACTIVE, "Transmisión y ASR en vivo")

        except Exception as e:
            self.errors_count += 1
            self._set_state(RoomState.ERROR, f"Error al arrancar: {e}")
            logger.error("[%s] Error crítico al arrancar worker: %s", self.room.id, e, exc_info=True)
            await self.stop()

    async def _audio_feeder(self) -> None:
        """Lee el audio de FFmpeg y lo envía continuamente a SeamlessRotationASR."""
        try:
            assert self.audio_source is not None
            assert self.asr is not None
            logger.info("[%s] Comenzando ingesta de audio...", self.room.id)

            async for chunk in self.audio_source.stream_chunks():
                if self._stop_event.is_set():
                    break
                await self.asr.send_audio(chunk)

            logger.info("[%s] Fin del flujo de audio normal.", self.room.id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.errors_count += 1
            logger.error("[%s] Error en ingesta de audio: %s", self.room.id, e, exc_info=True)
            self._set_state(RoomState.DEGRADED, f"Fallo en ingesta de audio: {e}")

    async def _event_processor(self) -> None:
        """Procesa los eventos emitidos por el ASR, traduce frases finales y publica en el bus."""
        try:
            assert self.asr is not None
            async for asr_event in self.asr.events():
                if self._stop_event.is_set():
                    break

                # 1. Evento interim (hipótesis parcial en idioma original al instante)
                if not asr_event.is_final:
                    event = SubtitleEvent(
                        room_id=self.room.id,
                        event_type="interim",
                        text=asr_event.text,
                        language=self.room.source_lang,
                        is_final=False
                    )
                    event_bus.publish(self.room.id, event)
                    continue

                # 2. Evento final (frase cerrada)
                final_event = SubtitleEvent(
                    room_id=self.room.id,
                    event_type="final",
                    text=asr_event.text,
                    language=self.room.source_lang,
                    is_final=True
                )
                event_bus.publish(self.room.id, final_event)

                # 3. Traducciones a idiomas objetivo (solo para frases finales)
                translations: Dict[str, str] = {}
                for target_lang in self.room.target_langs:
                    if target_lang != self.room.source_lang:
                        translated_text = await self.translator.translate(
                            text=asr_event.text,
                            target_lang=target_lang,
                            source_lang=self.room.source_lang
                        )
                        translations[target_lang] = translated_text

                        # Emitir evento de traducción para clientes que escuchan en este idioma
                        trans_event = SubtitleEvent(
                            room_id=self.room.id,
                            event_type="translation",
                            text=translated_text,
                            language=target_lang,
                            is_final=True
                        )
                        event_bus.publish(self.room.id, trans_event)

                # 4. Guardar en el acumulador SRT/VTT
                subtitle_store.add_subtitle(
                    room_id=self.room.id,
                    original_text=asr_event.text,
                    original_lang=self.room.source_lang,
                    translations=translations
                )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.errors_count += 1
            logger.error("[%s] Error en procesador de eventos ASR: %s", self.room.id, e)

    async def stop(self) -> None:
        """Detiene de forma limpia los subprocesos y tareas del worker."""
        if not self.is_running and self.state == RoomState.STOPPED:
            return

        self._set_state(RoomState.STOPPING, "Deteniendo procesos")
        self._stop_event.set()

        if self.audio_source:
            await self.audio_source.stop()
            self.audio_source = None

        if self.asr:
            await self.asr.stop()
            self.asr = None

        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        self.is_running = False
        self._set_state(RoomState.STOPPED, "Sesión detenida")
        logger.info("[%s] SessionWorker detenido por completo.", self.room.id)

    def patch(self, patch: RoomPatch) -> None:
        """Aplica modificaciones en caliente a la sala."""
        if patch.name is not None:
            self.room.name = patch.name
        if patch.target_langs is not None:
            self.room.target_langs = patch.target_langs
        if patch.custom_vocabulary is not None:
            self.room.custom_vocabulary = patch.custom_vocabulary
            self.translator.update_glossary(patch.custom_vocabulary)
        logger.info("[%s] Parámetros de sala actualizados: %s", self.room.id, patch)

    def get_status(self) -> RoomStatus:
        """Retorna el estado operativo detallado del worker."""
        history = subtitle_store.get_history(self.room.id)
        uptime = int(time.time() - self.started_at) if (self.is_running and self.started_at) else None

        return RoomStatus(
            id=self.room.id,
            name=self.room.name,
            kind=self.room.kind,
            source_uri=self.room.source_uri,
            source_lang=self.room.source_lang,
            target_langs=self.room.target_langs,
            custom_vocabulary=self.room.custom_vocabulary,
            state=self.state,
            is_running=self.is_running,
            subscribers=event_bus.subscriber_count(self.room.id),
            subtitles_count=len(history),
            errors_count=self.errors_count,
            started_at=self.started_at,
            uptime_seconds=uptime
        )


class Orchestrator:
    """
    Supervisor central de sesiones multi-sala.
    Carga rooms.yaml como semilla y gestiona el ciclo de vida de N SessionWorkers.
    """

    def __init__(self):
        self._workers: Dict[str, SessionWorker] = {}

    def load_rooms(self, config_path: str = "rooms.yaml") -> None:
        """Lee la configuración inicial de salas desde YAML."""
        if not os.path.exists(config_path):
            logger.warning("Archivo de configuración %s no encontrado.", config_path)
            return

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        rooms_data = data.get("rooms", [])
        for r_dict in rooms_data:
            room = RoomConfig(**r_dict)
            if room.id not in self._workers:
                self._workers[room.id] = SessionWorker(room)
                logger.info("Sala registrada en orquestador: %s ('%s', auto_start=%s)", room.id, room.name, room.auto_start)

    async def start_all(self) -> None:
        """Inicia únicamente las salas que tienen auto_start=True."""
        auto_workers = [w for w in self._workers.values() if w.room.auto_start]
        if auto_workers:
            logger.info("Iniciando %d salas configuradas con auto_start...", len(auto_workers))
            await asyncio.gather(*(w.start() for w in auto_workers), return_exceptions=True)
        else:
            logger.info("Salas cargadas en espera (STANDBY). Listas para arranque manual desde /admin.")

    async def stop_all(self) -> None:
        """Detiene todas las salas activas."""
        logger.info("Deteniendo todos los SessionWorkers...")
        stop_coros = [worker.stop() for worker in self._workers.values()]
        await asyncio.gather(*stop_coros, return_exceptions=True)

    def get_worker(self, room_id: str) -> Optional[SessionWorker]:
        return self._workers.get(room_id)

    async def start_room(self, room_id: str) -> bool:
        worker = self.get_worker(room_id)
        if not worker:
            return False
        await worker.start()
        return True

    async def stop_room(self, room_id: str) -> bool:
        worker = self.get_worker(room_id)
        if not worker:
            return False
        await worker.stop()
        return True

    async def create_room(self, room_config: RoomConfig, start: bool = False) -> SessionWorker:
        if room_config.id in self._workers:
            raise ValueError(f"La sala '{room_config.id}' ya existe.")

        worker = SessionWorker(room_config)
        self._workers[room_config.id] = worker
        logger.info("Nueva sala creada en caliente: %s ('%s')", room_config.id, room_config.name)

        if start or room_config.auto_start:
            await worker.start()

        return worker

    async def delete_room(self, room_id: str) -> bool:
        worker = self.get_worker(room_id)
        if not worker:
            return False
        await worker.stop()
        del self._workers[room_id]
        logger.info("Sala eliminada: %s", room_id)
        return True

    def patch_room(self, room_id: str, patch: RoomPatch) -> bool:
        worker = self.get_worker(room_id)
        if not worker:
            return False
        worker.patch(patch)
        return True

    def list_rooms(self) -> List[RoomStatus]:
        return [w.get_status() for w in self._workers.values()]


# Instancia singleton del orquestador
orchestrator = Orchestrator()
