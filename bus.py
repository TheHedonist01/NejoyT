import asyncio
import logging
import time
from typing import Dict, Optional, Set
from pydantic import BaseModel, Field

logger = logging.getLogger("nerdearla.bus")


class SubtitleEvent(BaseModel):
    """Evento normalizado de subtítulos para WebSocket y frontend."""
    room_id: str = Field(..., description="ID de la sala")
    event_type: str = Field(..., description="'interim' | 'final' | 'translation' | 'status'")
    text: str = Field(..., description="Texto subtitulado")
    language: str = Field("es", description="Código del idioma de este texto")
    is_final: bool = Field(False, description="True si es una frase cerrada")
    timestamp: float = Field(default_factory=time.time, description="Timestamp UNIX en segundos")
    metadata: Optional[dict] = Field(default=None, description="Datos adicionales opcionales")


class EventBus:
    """
    Bus de eventos en memoria no bloqueante basado en asyncio.
    Permite a múltiples clientes WebSocket (audiencia, OBS) suscribirse
    a los eventos emitidos por el SessionWorker de su sala.
    """

    def __init__(self, max_queue_size: int = 100):
        self._max_queue_size = max_queue_size
        self._subscribers: Dict[str, Set[tuple[asyncio.Queue[SubtitleEvent], str]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, room_id: str, lang: str = "es") -> asyncio.Queue[SubtitleEvent]:
        """Crea una cola de eventos para un nuevo suscriptor en una sala con idioma preferido."""
        queue: asyncio.Queue[SubtitleEvent] = asyncio.Queue(maxsize=self._max_queue_size)
        raw_lang = (lang or "es").strip().lower()
        clean_lang = "all" if raw_lang == "all" else raw_lang[:2]
        async with self._lock:
            if room_id not in self._subscribers:
                self._subscribers[room_id] = set()
            self._subscribers[room_id].add((queue, clean_lang))
            logger.info("Nuevo suscriptor a sala '%s' (idioma: %s). Total activos: %d", room_id, clean_lang, len(self._subscribers[room_id]))
        return queue

    async def unsubscribe(self, room_id: str, queue: asyncio.Queue[SubtitleEvent]) -> None:
        """Elimina la cola de un suscriptor desconectado."""
        async with self._lock:
            if room_id in self._subscribers:
                self._subscribers[room_id] = {item for item in self._subscribers[room_id] if item[0] != queue}
                logger.info("Suscriptor desconectado de sala '%s'. Restantes: %d", room_id, len(self._subscribers[room_id]))
                if not self._subscribers[room_id]:
                    del self._subscribers[room_id]

    def active_languages(self, room_id: str) -> Set[str]:
        """Retorna el conjunto de idiomas (códigos 2 letras) que están escuchando los suscriptores conectados."""
        subs = self._subscribers.get(room_id, set())
        return {item[1] for item in subs}

    def publish(self, room_id: str, event: SubtitleEvent) -> None:
        """
        Publica un evento a los suscriptores correspondientes de la sala.
        Filtra por idioma para que cada cliente reciba únicamente subtítulos en su idioma preferido,
        salvo eventos de control (status) o suscriptores omniscientes ('all').
        """
        subscribers = self._subscribers.get(room_id)
        if not subscribers:
            return

        event_lang = (event.language or "es").strip().lower()[:2]

        for queue, sub_lang in list(subscribers):
            if event.event_type == "status" or sub_lang == "all":
                should_send = True
            elif event.event_type == "interim":
                # Regla 2: Interinos se transmiten en vivo para feedback en tiempo real
                should_send = True
            elif event_lang == sub_lang:
                should_send = True
            else:
                should_send = False

            if not should_send:
                continue

            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()  # Descartar evento más viejo
                    queue.put_nowait(event)
                except Exception:
                    pass

    def subscriber_count(self, room_id: str) -> int:
        """Retorna la cantidad de clientes escuchando en la sala."""
        return len(self._subscribers.get(room_id, set()))


# Instancia singleton del bus para toda la aplicación
event_bus = EventBus()
