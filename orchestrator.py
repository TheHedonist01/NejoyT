import asyncio
import logging
import os
import time
import yaml
from typing import Dict, List, Optional
from pydantic import BaseModel

from audio.source import AudioSource
from asr.base import ASRBackend
from asr.local import LocalASR
from asr.rotation import SeamlessRotationASR
from translate.gemini_text import GeminiTranslator
from bus import event_bus, SubtitleEvent
from store import subtitle_store
from config import ASRBackendKind, RoomConfig, RoomPatch, RoomState, SourceKind

logger = logging.getLogger("nerdearla.orchestrator")


class RoomStatus(BaseModel):
    id: str
    name: str
    kind: SourceKind
    backend: ASRBackendKind
    source_uri: str
    source_lang: str
    target_lang: str = "es"
    target_langs: List[str]
    whisper_model_size: Optional[str] = "base"
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
    AudioSource (FFmpeg) -> ASRBackend (Local GPU o Gemini Cloud) -> Traductor -> EventBus + SubtitleStore
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
        self.asr: Optional[ASRBackend] = None
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
        self._set_state(RoomState.STARTING, f"Inicializando conexiones y ASR ({self.room.backend.value})")
        subtitle_store.start_room_clock(self.room.id)

        try:
            self.audio_source = AudioSource(
                source_uri=self.room.source_uri,
                kind=self.room.kind,
                loop=self.room.loop,
                on_degraded=self._on_source_degraded,
                on_recovered=self._on_source_recovered
            )

            if self.room.backend == ASRBackendKind.LOCAL:
                model_sz = getattr(self.room, "whisper_model_size", "base") or "base"
                logger.info("[%s] Usando backend Local ASR (modelo: %s, detección de hardware automática)...", self.room.id, model_sz)
                self.asr = LocalASR(
                    model_size=model_sz,
                    device=None,
                    language=self.room.source_lang,
                    custom_vocabulary=self.room.custom_vocabulary
                )
            else:
                logger.info("[%s] Usando backend Cloud Gemini Live ASR con rotación...", self.room.id)
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
            self._set_state(RoomState.ACTIVE, f"Transmisión y ASR activo ({self.room.backend.value})")

        except Exception as e:
            self.errors_count += 1
            self._set_state(RoomState.ERROR, f"Error al arrancar: {e}")
            logger.error("[%s] Error crítico al arrancar worker: %s", self.room.id, e, exc_info=True)
            await self.stop()

    async def _audio_feeder(self) -> None:
        """Lee el audio de FFmpeg y lo envía continuamente al ASR."""
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

                speaker_lang = (asr_event.language or self.room.source_lang or "es").strip().lower()[:2]
                if speaker_lang in ("au", "mu"):
                    speaker_lang = "es"

                primary_tgt = (self.room.target_lang or "es").strip().lower()[:2]

                # 1. Evento interim (hipótesis parcial en idioma original al instante)
                if not asr_event.is_final:
                    event = SubtitleEvent(
                        room_id=self.room.id,
                        event_type="interim",
                        text=asr_event.text,
                        language=speaker_lang,
                        is_final=False
                    )
                    event_bus.publish(self.room.id, event)

                    # Si el orador habla inglés y la audiencia principal escucha español (o viceversa),
                    # generar traducción de interim rápida (~60ms) para que el espectador vea español en vivo
                    if speaker_lang != primary_tgt and len(asr_event.text.split()) >= 2:
                        translated_interim = await self.translator.translate(
                            text=asr_event.text,
                            target_lang=primary_tgt,
                            source_lang=speaker_lang
                        )
                        if translated_interim and translated_interim != asr_event.text:
                            trans_interim_event = SubtitleEvent(
                                room_id=self.room.id,
                                event_type="interim",
                                text=translated_interim,
                                language=primary_tgt,
                                is_final=False
                            )
                            event_bus.publish(self.room.id, trans_interim_event)
                    continue

                # 2. Evento final (frase cerrada en idioma original hablado)
                final_event = SubtitleEvent(
                    room_id=self.room.id,
                    event_type="final",
                    text=asr_event.text,
                    language=speaker_lang,
                    is_final=True
                )
                event_bus.publish(self.room.id, final_event)

                # 3. Traducciones a idiomas objetivo
                translations: Dict[str, str] = {speaker_lang: asr_event.text}
                active_langs = event_bus.active_languages(self.room.id)

                candidate_targets = list(self.room.target_langs)
                if self.room.target_lang and self.room.target_lang not in candidate_targets:
                    candidate_targets.append(self.room.target_lang)

                for raw_target in candidate_targets:
                    tgt = raw_target.strip().lower()[:2]
                    if tgt == speaker_lang:
                        translations[tgt] = asr_event.text
                        continue

                    # Si ningún cliente conectado está escuchando este idioma y NO es el idioma principal configurado, omitir
                    is_primary = (tgt == primary_tgt)
                    if not is_primary and active_langs and tgt not in active_langs:
                        continue

                    translated_text = await self.translator.translate(
                        text=asr_event.text,
                        target_lang=tgt,
                        source_lang=speaker_lang
                    )
                    translations[tgt] = translated_text

                    # Emitir evento de traducción para clientes que escuchan en este idioma
                    trans_event = SubtitleEvent(
                        room_id=self.room.id,
                        event_type="translation",
                        text=translated_text,
                        language=tgt,
                        is_final=True
                    )
                    event_bus.publish(self.room.id, trans_event)

                # 4. Guardar en el acumulador SRT/VTT
                subtitle_store.add_subtitle(
                    room_id=self.room.id,
                    original_text=asr_event.text,
                    original_lang=speaker_lang,
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
        if patch.backend is not None:
            self.room.backend = patch.backend
        if patch.source_lang is not None:
            self.room.source_lang = patch.source_lang
            if self.asr and hasattr(self.asr, "language"):
                self.asr.language = patch.source_lang
        if patch.target_lang is not None:
            self.room.target_lang = patch.target_lang
            if patch.target_lang not in self.room.target_langs:
                self.room.target_langs.append(patch.target_lang)
        if patch.target_langs is not None:
            self.room.target_langs = patch.target_langs
        if patch.whisper_model_size is not None:
            self.room.whisper_model_size = patch.whisper_model_size
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
            backend=self.room.backend,
            source_uri=self.room.source_uri,
            source_lang=self.room.source_lang,
            target_lang=self.room.target_lang,
            target_langs=self.room.target_langs,
            whisper_model_size=getattr(self.room, "whisper_model_size", "base") or "base",
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
                logger.info("Sala registrada: %s ('%s', backend=%s, auto_start=%s)", room.id, room.name, room.backend.value, room.auto_start)

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
        self._save_rooms()
        logger.info("Nueva sala creada: %s ('%s', backend=%s)", room_config.id, room_config.name, room_config.backend.value)

        if start or room_config.auto_start:
            await worker.start()

        return worker

    async def delete_room(self, room_id: str) -> bool:
        worker = self.get_worker(room_id)
        if not worker:
            return False
        await worker.stop()
        del self._workers[room_id]
        self._save_rooms()
        logger.info("Sala eliminada: %s", room_id)
        return True

    def patch_room(self, room_id: str, patch: RoomPatch) -> bool:
        worker = self.get_worker(room_id)
        if not worker:
            return False
        worker.patch(patch)
        self._save_rooms()
        return True

    def _save_rooms(self, config_path: str = "rooms.yaml") -> None:
        """Persiste las salas actuales configuradas por el usuario en rooms.yaml."""
        try:
            data = {"rooms": [w.room.model_dump(mode="json") for w in self._workers.values()]}
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        except Exception as e:
            logger.warning("No se pudo guardar la configuración de salas en %s: %s", config_path, e)

    def list_rooms(self) -> List[RoomStatus]:
        return [w.get_status() for w in self._workers.values()]


# Instancia singleton del orquestador
orchestrator = Orchestrator()

