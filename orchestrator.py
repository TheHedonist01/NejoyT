import asyncio
import logging
import os
import yaml
from typing import Dict, List, Optional
from pydantic import BaseModel

from audio.source import AudioSource
from asr.rotation import SeamlessRotationASR
from translate.gemini_text import GeminiTranslator
from bus import event_bus, SubtitleEvent
from store import subtitle_store
from config import RoomConfig

logger = logging.getLogger("nerdearla.orchestrator")


class RoomStatus(BaseModel):
    id: str
    name: str
    source_uri: str
    source_lang: str
    target_langs: List[str]
    is_running: bool
    subscribers: int
    subtitles_count: int
    errors_count: int


class SessionWorker:
    """
    Trabajador asíncrono que encapsula el pipeline completo de una sala:
    AudioSource (FFmpeg) -> SeamlessRotationASR -> GeminiTranslator -> EventBus + SubtitleStore
    """

    def __init__(self, room: RoomConfig):
        self.room = room
        self.is_running = False
        self.errors_count = 0
        self._tasks: List[asyncio.Task] = []
        self._stop_event = asyncio.Event()

        self.audio_source = AudioSource(room.source_uri, is_live_stream=False, loop=room.loop)
        self.asr = SeamlessRotationASR(
            language=room.source_lang,
            custom_vocabulary=room.custom_vocabulary
        )
        self.translator = GeminiTranslator(
            custom_glossary=room.custom_vocabulary
        )

    async def start(self) -> None:
        """Inicia el pipeline de la sala de forma aislada."""
        if self.is_running:
            return

        self.is_running = True
        self._stop_event.clear()
        subtitle_store.start_room_clock(self.room.id)

        logger.info("[%s] Iniciando SessionWorker para '%s'...", self.room.id, self.room.name)
        await self.asr.start()

        # Publicar evento de estado inicial
        event_bus.publish(
            self.room.id,
            SubtitleEvent(
                room_id=self.room.id,
                event_type="status",
                text="Transmisión iniciada",
                language=self.room.source_lang,
                metadata={"status": "online"}
            )
        )

        feeder_task = asyncio.create_task(
            self._audio_feeder(),
            name=f"worker-feeder-{self.room.id}"
        )
        processor_task = asyncio.create_task(
            self._event_processor(),
            name=f"worker-processor-{self.room.id}"
        )
        self._tasks = [feeder_task, processor_task]

    async def _audio_feeder(self) -> None:
        """Lee el audio de FFmpeg y lo envía continuamente a SeamlessRotationASR."""
        try:
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
            logger.error("[%s] Error en ingesta de audio:", self.room.id, exc_info=True)

    async def _event_processor(self) -> None:
        """Procesa los eventos emitidos por el ASR, traduce frases finales y publica en el bus."""
        try:
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
        if not self.is_running:
            return

        self._stop_event.set()
        await self.audio_source.stop()
        await self.asr.stop()

        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.is_running = False
        logger.info("[%s] SessionWorker detenido.", self.room.id)

    def get_status(self) -> RoomStatus:
        """Retorna el estado operativo del worker."""
        history = subtitle_store.get_history(self.room.id)
        return RoomStatus(
            id=self.room.id,
            name=self.room.name,
            source_uri=self.room.source_uri,
            source_lang=self.room.source_lang,
            target_langs=self.room.target_langs,
            is_running=self.is_running,
            subscribers=event_bus.subscriber_count(self.room.id),
            subtitles_count=len(history),
            errors_count=self.errors_count
        )


class Orchestrator:
    """
    Supervisor central de sesiones multi-sala.
    Carga rooms.yaml y gestiona N SessionWorkers de forma aislada e independiente.
    """

    def __init__(self):
        self._workers: Dict[str, SessionWorker] = {}

    def load_rooms(self, config_path: str = "rooms.yaml") -> None:
        """Lee la configuración de salas desde YAML."""
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
                logger.info("Sala registrada en orquestador: %s (%s)", room.id, room.name)

    async def start_all(self) -> None:
        """Inicia todas las salas configuradas."""
        logger.info("Iniciando todos los SessionWorkers (%d salas)...", len(self._workers))
        start_coros = [worker.start() for worker in self._workers.values()]
        await asyncio.gather(*start_coros, return_exceptions=True)

    async def stop_all(self) -> None:
        """Detiene todas las salas."""
        logger.info("Deteniendo todos los SessionWorkers...")
        stop_coros = [worker.stop() for worker in self._workers.values()]
        await asyncio.gather(*stop_coros, return_exceptions=True)

    def get_worker(self, room_id: str) -> Optional[SessionWorker]:
        return self._workers.get(room_id)

    def list_rooms(self) -> List[RoomStatus]:
        return [w.get_status() for w in self._workers.values()]


# Instancia singleton del orquestador
orchestrator = Orchestrator()
