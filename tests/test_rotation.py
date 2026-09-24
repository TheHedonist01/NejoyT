import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import SourceKind
from audio.source import AudioSource
from asr.rotation import SeamlessRotationASR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_rotation")


async def main():
    logger.info("Iniciando prueba de SeamlessRotationASR con solape acelerado (3s / 5s)...")
    
    # Configuramos tiempos reducidos para probar la rotación en 8 segundos de audio
    rotation_asr = SeamlessRotationASR(
        language="es-419",
        custom_vocabulary=["Nerdearla", "Kubernetes", "FastAPI"],
        overlap_start_sec=3.0,       # Inicia Sesión B a los 3 segundos
        rotation_threshold_sec=5.0   # Conmuta tras 5 segundos
    )
    
    audio_source = AudioSource(kind=SourceKind.FILE, source_uri="samples/speech_sample.wav", loop=False)
    
    await rotation_asr.start()
    
    events_received = []

    async def feeder():
        logger.info("Enviando audio al gestor de rotación...")
        async for chunk in audio_source.stream_chunks():
            await rotation_asr.send_audio(chunk)
            
        logger.info("Fin de audio. Esperando conmutación y deteniendo...")
        await asyncio.sleep(4.0)
        await rotation_asr.stop()

    async def collector():
        async for event in rotation_asr.events():
            events_received.append(event)
            tag = "FINAL" if event.is_final else "INTERIM"
            print(f"[{tag}] {event.text}")

    await asyncio.gather(feeder(), collector())
    
    logger.info("Total eventos recibidos durante la rotación: %d", len(events_received))
    assert len(events_received) > 0, "No se recibieron eventos a través de SeamlessRotationASR"
    print("\n[OK] PRUEBA DE ROTACION TRANSPARENTE (SEAMLESS ROTATION) EXITOSA.")


if __name__ == "__main__":
    asyncio.run(main())
