import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath("."))

from bus import EventBus, SubtitleEvent
from orchestrator import SessionWorker
from config import RoomConfig, SourceKind, ASRBackendKind


class TestInterimAndLanguageIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_bus_language_isolation_for_interim(self):
        bus = EventBus()
        # Suscriptor español
        q_es = await bus.subscribe("room1", "es")
        # Suscriptor inglés
        q_en = await bus.subscribe("room1", "en")

        # 1. Emitir interim en inglés
        ev_en = SubtitleEvent(
            room_id="room1",
            event_type="interim",
            text="Hello world",
            language="en",
            is_final=False
        )
        bus.publish("room1", ev_en)

        # q_en debe recibirlo, q_es NO debe recibirlo
        self.assertEqual(q_en.qsize(), 1)
        self.assertEqual(q_es.qsize(), 0)

        # 2. Emitir interim en español
        ev_es = SubtitleEvent(
            room_id="room1",
            event_type="interim",
            text="Hola mundo",
            language="es",
            is_final=False
        )
        bus.publish("room1", ev_es)

        # q_es debe recibirlo, q_en no debe recibir el español
        self.assertEqual(q_es.qsize(), 1)
        self.assertEqual(q_en.qsize(), 1)

        received_es = q_es.get_nowait()
        self.assertEqual(received_es.text, "Hola mundo")
        self.assertEqual(received_es.language, "es")

        received_en = q_en.get_nowait()
        self.assertEqual(received_en.text, "Hello world")
        self.assertEqual(received_en.language, "en")

    async def test_room_auto_seed_generation(self):
        import os
        from orchestrator import Orchestrator
        orch = Orchestrator()
        test_yaml = "test_rooms_tmp.yaml"
        if os.path.exists(test_yaml):
            os.remove(test_yaml)

        try:
            orch.load_rooms(test_yaml)
            self.assertTrue(os.path.exists(test_yaml))
            self.assertGreaterEqual(len(orch.list_rooms()), 1)
        finally:
            if os.path.exists(test_yaml):
                os.remove(test_yaml)


if __name__ == "__main__":
    unittest.main()
