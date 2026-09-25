import asyncio
import collections
import logging
import os
import re
import shutil
import time
import yaml
from typing import Dict, List, Optional
import numpy as np
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
    speakers_count: int = 0
    enrolled_speakers: List[str] = []
    started_at: Optional[float] = None
    uptime_seconds: Optional[int] = None


class SessionWorker:
    """
    Trabajador asíncrono que encapsula el ciclo de vida y el pipeline de una sala:
    AudioSource (FFmpeg) -> ASRBackend (Local GPU o Gemini Cloud) -> Traductor -> EventBus + SubtitleStore
    Integrado con Speaker ID local (ECAPA-TDNN ONNX).
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
        """Lee el audio de FFmpeg, lo guarda en el buffer circular para Speaker ID y lo envía al ASR."""
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

    @staticmethod
    def _heuristic_detect_lang(text: str, default: str = "es") -> str:
        """Detección ultrarrápida por stopwords comunes entre inglés y español (cero latencia)."""
        words = set(re.findall(r'\b[a-zA-ZáéíóúñÁÉÍÓÚÑ]+\b', text.lower()))
        if not words:
            return default
        es_stops = {"el", "la", "de", "que", "y", "en", "un", "una", "por", "para", "con", "no", "es", "este", "esta", "los", "las", "como", "al", "del", "pero", "su", "lo"}
        en_stops = {"the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it", "for", "not", "on", "with", "he", "as", "you", "do", "at", "this", "but", "his", "by", "from", "they", "we", "say", "her", "she", "or", "an", "will", "my", "one", "all", "would", "there", "their", "what", "so", "up", "out", "if", "about", "who", "get", "which", "go", "me", "is", "are"}
        es_count = len(words & es_stops)
        en_count = len(words & en_stops)
        if en_count > es_count:
            return "en"
        if es_count > en_count:
            return "es"
        return default

    async def _emit_final_segment(self, segment_text: str, speaker_lang: str, primary_tgt: str) -> None:
        """Traduce, almacena en SQLite y publica un segmento final confirmado."""
        cleaned = segment_text.strip()
        if not cleaned:
            return

        translations: Dict[str, str] = {speaker_lang: cleaned}
        active_langs = event_bus.active_languages(self.room.id)

        candidate_targets = list(self.room.target_langs)
        if self.room.target_lang and self.room.target_lang not in candidate_targets:
            candidate_targets.append(self.room.target_lang)

        for raw_target in candidate_targets:
            tgt = raw_target.strip().lower()[:2]
            if tgt == speaker_lang:
                translations[tgt] = cleaned
                continue

            # Si ningún cliente conectado está escuchando este idioma y NO es el principal, omitir
            is_primary = (tgt == primary_tgt)
            if not is_primary and (tgt not in active_langs):
                continue

            translated_text = await self.translator.translate(
                text=cleaned,
                target_lang=tgt,
                source_lang=speaker_lang
            )
            translations[tgt] = translated_text

        # Guardar en el acumulador SQLite y memoria
        record = subtitle_store.add_subtitle(
            room_id=self.room.id,
            original_text=cleaned,
            original_lang=speaker_lang,
            translations=translations,
            speaker=None
        )

        meta = {
            "index": record.index,
            "speaker": None,
            "speaker_score": None
        }
        final_event = SubtitleEvent(
            room_id=self.room.id,
            event_type="final",
            text=cleaned,
            language=speaker_lang,
            is_final=True,
            metadata=meta
        )
        event_bus.publish(self.room.id, final_event)

        for tgt, trans_text in translations.items():
            if tgt == speaker_lang:
                continue
            trans_event = SubtitleEvent(
                room_id=self.room.id,
                event_type="translation",
                text=trans_text,
                language=tgt,
                is_final=True,
                metadata=meta
            )
            event_bus.publish(self.room.id, trans_event)

    async def _event_processor(self) -> None:
        """Procesa los eventos emitidos por el ASR con segmentación streaming continua y traducción inmediata a español."""
        try:
            assert self.asr is not None
            committed_prefix = ""
            last_interim_trans_time = 0.0
            last_interim_text = ""

            async for asr_event in self.asr.events():
                if self._stop_event.is_set():
                    break

                raw_lang = (asr_event.language or self.room.source_lang or "").strip().lower()[:2]
                if raw_lang in ("au", "mu", ""):
                    speaker_lang = self._heuristic_detect_lang(asr_event.text, default="es")
                else:
                    speaker_lang = raw_lang

                primary_tgt = (self.room.target_lang or "es").strip().lower()[:2]

                # 1. Evento interim (hipótesis parcial continua)
                if not asr_event.is_final:
                    full_text = asr_event.text.strip()
                    if committed_prefix and full_text.startswith(committed_prefix):
                        new_text = full_text[len(committed_prefix):].strip()
                    else:
                        new_text = full_text

                    # Segmentación progresiva: si se detecta una oración terminada con [.!?]
                    # y hay texto subsiguiente, confirmarla inmediatamente sin esperar fin de charla
                    sent_match = re.search(r'^(.*?[.!?])\s+(\S.*)$', new_text, flags=re.DOTALL)
                    if sent_match and len(sent_match.group(1).strip()) > 3:
                        completed_sent = sent_match.group(1).strip()
                        remaining_new = sent_match.group(2).strip()
                        await self._emit_final_segment(completed_sent, speaker_lang, primary_tgt)
                        committed_prefix = full_text[:len(full_text) - len(remaining_new)].strip()
                        new_text = remaining_new
                    else:
                        # Si el orador habla continuamente sin puntos (>= 14 palabras), segmentar por cláusula
                        words = new_text.split()
                        if len(words) >= 14:
                            clause_match = re.search(r'^(.*?)([,;]|\s+(?:and|but|so|because|however|y|pero|o|que)\s+)(\S.*)$', new_text, flags=re.IGNORECASE | re.DOTALL)
                            if clause_match and len(clause_match.group(1).split()) >= 6:
                                completed_clause = (clause_match.group(1) + (clause_match.group(2) if clause_match.group(2).strip() in (',', ';') else '')).strip()
                                remaining_new = ((clause_match.group(2) + ' ' if clause_match.group(2).strip() not in (',', ';') else '') + clause_match.group(3)).strip()
                                await self._emit_final_segment(completed_clause, speaker_lang, primary_tgt)
                                committed_prefix = full_text[:len(full_text) - len(remaining_new)].strip()
                                new_text = remaining_new

                    if new_text:
                        # Publicar interim en idioma original para oyentes del idioma origen
                        event_bus.publish(self.room.id, SubtitleEvent(
                            room_id=self.room.id,
                            event_type="interim",
                            text=new_text,
                            language=speaker_lang,
                            is_final=False
                        ))

                        # Si el orador habla en un idioma distinto al objetivo principal (ej. inglés -> español),
                        # traducir el interim al instante para que la audiencia en español reciba subtítulos directamente en español
                        if speaker_lang != primary_tgt:
                            now = time.time()
                            if (now - last_interim_trans_time >= 0.25) or (new_text != last_interim_text):
                                last_interim_trans_time = now
                                last_interim_text = new_text
                                try:
                                    translated_interim = await self.translator.translate(
                                        text=new_text,
                                        target_lang=primary_tgt,
                                        source_lang=speaker_lang
                                    )
                                    if translated_interim:
                                        event_bus.publish(self.room.id, SubtitleEvent(
                                            room_id=self.room.id,
                                            event_type="interim",
                                            text=translated_interim,
                                            language=primary_tgt,
                                            is_final=False
                                        ))
                                except Exception as e:
                                    logger.debug("[%s] Error traduciendo interim: %s", self.room.id, e)

                    continue

                # 2. Evento final emitido por el backend ASR
                full_text = asr_event.text.strip()
                if committed_prefix and full_text.startswith(committed_prefix):
                    remaining_final = full_text[len(committed_prefix):].strip()
                else:
                    remaining_final = full_text

                if remaining_final:
                    await self._emit_final_segment(remaining_final, speaker_lang, primary_tgt)

                # Reset de acumuladores de turno
                committed_prefix = ""
                last_interim_text = ""

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
            try:
                await asyncio.wait_for(self.audio_source.stop(), timeout=2.0)
            except Exception as e:
                logger.warning("[%s] Excepción deteniendo audio_source: %s", self.room.id, e)
            finally:
                self.audio_source = None

        if self.asr:
            try:
                await asyncio.wait_for(self.asr.stop(), timeout=2.0)
            except Exception as e:
                logger.warning("[%s] Excepción deteniendo asr: %s", self.room.id, e)
            finally:
                self.asr = None

        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
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
            speakers_count=0,
            enrolled_speakers=[],
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
        """Lee la configuración inicial de salas desde YAML o la auto-crea si no existe."""
        if not os.path.exists(config_path):
            example_path = "rooms.example.yaml"
            if os.path.exists(example_path):
                try:
                    shutil.copyfile(example_path, config_path)
                    logger.info("Archivo %s inicializado automáticamente desde %s", config_path, example_path)
                except Exception as e:
                    logger.warning("No se pudo copiar %s a %s: %s", example_path, config_path, e)
            else:
                default_data = {
                    "rooms": [
                        {
                            "id": "auditorio-principal",
                            "name": "Auditorio Principal",
                            "kind": "mic",
                            "backend": "cloud",
                            "source_uri": "default",
                            "source_lang": "en",
                            "target_lang": "es",
                            "target_langs": ["es", "en", "pt"],
                            "whisper_model_size": "base",
                            "custom_vocabulary": ["Nerdearla", "Kubernetes", "Docker", "Python", "FastAPI"],
                            "speakers": [],
                            "speaker_threshold": 0.7,
                            "loop": False,
                            "auto_start": False
                        }
                    ]
                }
                try:
                    with open(config_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(default_data, f, allow_unicode=True, sort_keys=False)
                    logger.info("Archivo %s creado automáticamente con sala semilla.", config_path)
                except Exception as e:
                    logger.warning("No se pudo auto-crear %s: %s", config_path, e)

        if not os.path.exists(config_path):
            logger.warning("Archivo de configuración %s no encontrado.", config_path)
            return

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

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
            asyncio.create_task(self._safe_start_worker(worker))

        return worker

    async def _safe_start_worker(self, worker: SessionWorker) -> None:
        try:
            await worker.start()
        except Exception as e:
            logger.error("[%s] Error iniciando worker en segundo plano: %s", worker.room.id, e)

    async def delete_room(self, room_id: str) -> bool:
        worker = self.get_worker(room_id)
        if not worker:
            return False
        try:
            # Asegurar que worker.stop() nunca bloquee la eliminación indefinidamente
            await asyncio.wait_for(worker.stop(), timeout=3.0)
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("[%s] Timeout o error cerrando worker durante eliminación: %s", room_id, e)
        finally:
            self._workers.pop(room_id, None)
            self._save_rooms()
            logger.info("Sala eliminada definitivamente: %s", room_id)
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

