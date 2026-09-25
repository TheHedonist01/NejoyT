import asyncio
import logging
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from audio.source import AudioSource
from asr.gemini_live import GeminiLiveASR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_live_transcription")


async def main():
    logger.info("Iniciando prueba de transcripción en streaming con audio real...")
    
    from config import SourceKind
    audio_source = AudioSource(SourceKind.FILE, "samples/Nicolás Wolovick Sample -SOLO.mp4")
    asr = GeminiLiveASR(
        language="es-419",
        custom_vocabulary=["Nerdearla", "Kubernetes", "FastAPI"],
        session_id="test-speech-session"
    )
    
    await asr.start()
    
    events_received = []

    async def audio_feeder():
        logger.info("Comenzando streaming de audio hacia Gemini Live...")
        async for chunk in audio_source.stream_chunks():
            await asr.send_audio(chunk)
            
        logger.info("Fin de voz. Enviando 1 segundo de silencio para forzar cierre de turno VAD...")
        for _ in range(10):
            await asr.send_audio(b"\x00" * 3200)
            await asyncio.sleep(0.1)
            
        await asr.finish_audio_stream()
        # Esperar brevemente a que el servidor entregue los eventos finales
        await asyncio.sleep(2.0)
        await asr.stop()

    async def event_collector():
        async for event in asr.events():
            events_received.append(event)
            tag = "FINAL" if event.is_final else "INTERIM"
            print(f"[{tag}] {event.text}")

    await asyncio.gather(audio_feeder(), event_collector())
    
    logger.info("Resumen de prueba:")
    logger.info("Total eventos de transcripción recibidos: %d", len(events_received))
    final_events = [e for e in events_received if e.is_final]
    logger.info("Eventos finales: %d", len(final_events))
    
    if final_events:
        full_text = " ".join(e.text for e in final_events)
        print(f"\n[OK] Texto final reconocido: \"{full_text}\"")
    
    assert len(events_received) > 0, "No se recibieron eventos de transcripción"
    print("\n[OK] PRUEBA DE TRANSCRIPCION EN VIVO EXITOSA.")


if __name__ == "__main__":
    asyncio.run(main())
