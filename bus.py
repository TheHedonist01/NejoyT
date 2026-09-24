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
        self._subscribers: Dict[str, Set[asyncio.Queue[SubtitleEvent]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, room_id: str) -> asyncio.Queue[SubtitleEvent]:
        """Crea una cola de eventos para un nuevo suscriptor en una sala."""
        queue: asyncio.Queue[SubtitleEvent] = asyncio.Queue(maxsize=self._max_queue_size)
        async with self._lock:
            if room_id not in self._subscribers:
                self._subscribers[room_id] = set()
            self._subscribers[room_id].add(queue)
            logger.info("Nuevo suscriptor a sala '%s'. Total activos: %d", room_id, len(self._subscribers[room_id]))
        return queue

    async def unsubscribe(self, room_id: str, queue: asyncio.Queue[SubtitleEvent]) -> None:
        """Elimina la cola de un suscriptor desconectado."""
        async with self._lock:
            if room_id in self._subscribers:
                self._subscribers[room_id].discard(queue)
                logger.info("Suscriptor desconectado de sala '%s'. Restantes: %d", room_id, len(self._subscribers[room_id]))
                if not self._subscribers[room_id]:
                    del self._subscribers[room_id]

    def publish(self, room_id: str, event: SubtitleEvent) -> None:
        """
        Publica un evento a todos los suscriptores de la sala.
        Si la cola de un suscriptor lento se llena, descarta el evento más viejo.
        """
        subscribers = self._subscribers.get(room_id)
        if not subscribers:
            return

        for queue in list(subscribers):
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
