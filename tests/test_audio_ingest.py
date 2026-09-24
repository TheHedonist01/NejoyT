import asyncio
import logging
import time
from audio.source import AudioSource

from config import SourceKind

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test")


async def main():
    logger.info("Probando ingesta de audio con AudioSource sobre samples/test_sine.wav...")
    source = AudioSource(kind=SourceKind.FILE, source_uri="samples/test_sine.wav", loop=False)
    
    total_bytes = 0
    start = time.time()
    chunks_count = 0
    
    async for chunk in source.stream_chunks():
        total_bytes += len(chunk)
        chunks_count += 1
        assert len(chunk) == 3200, f"Chunk size incorrecto: {len(chunk)}"

    duration = time.time() - start
    rate = total_bytes / duration if duration > 0 else 0
    logger.info("Test completado.")
    logger.info("Total chunks: %d", chunks_count)
    logger.info("Total bytes: %d", total_bytes)
    logger.info("Duración medida: %.2f segundos", duration)
    logger.info("Tasa medida: %.1f B/s (Esperado ~32.000 B/s)", rate)

    # El audio dura 5 segundos, debe transferir ~160.000 bytes en ~5 segundos
    assert chunks_count == 50, f"Se esperaban 50 chunks de 100ms, se obtuvieron {chunks_count}"
    assert 28000 <= rate <= 37000, f"Tasa de transferencia fuera de rango: {rate}"
    print("\n[OK] PRUEBA DE INGESTA Y PACING EXITOSA.")


if __name__ == "__main__":
    asyncio.run(main())
