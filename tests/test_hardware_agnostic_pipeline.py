import asyncio
import logging
import os
import sys
import time
import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from asr.device import detect_compute_device
from asr.local import LocalASR
from translate.local_marian import translate_local_sentence
from translate.gemini_text import GeminiTranslator
from bus import event_bus, SubtitleEvent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_hardware_pipeline")


async def main():
    logger.info("=== INICIANDO VALIDACIÓN DEL PIPELINE DUAL Y AGNOSTICO DE HARDWARE ===")

    # 1. Prueba de detección de hardware agnóstica
    device, compute_type, dev_idx = detect_compute_device()
    logger.info("[Hardware Detectado] Dispositivo: %s | Precisión: %s | Índice: %d", device.upper(), compute_type, dev_idx)
    assert device in ("cuda", "mps", "cpu"), f"Dispositivo desconocido: {device}"

    # 2. Prueba del traductor local agnóstico de hardware (CTranslate2 / MarianMT)
    logger.info("Probando traducción local en-es con glosario técnico...")
    phrases = [
        ("Welcome to Nerdearla 2026 conference.", "es"),
        ("Deploying Kubernetes pods with FastAPI and Docker.", "es"),
        ("Hola a todos, esta es una prueba de transcripción en tiempo real.", "en")
    ]
    glossary = ["Nerdearla", "Kubernetes", "FastAPI", "Docker"]

    for text, tgt in phrases:
        src = "en" if tgt == "es" else "es"
        t0 = time.time()
        res = translate_local_sentence(text, src, tgt, glossary=glossary)
        elapsed_ms = (time.time() - t0) * 1000
        logger.info("[%s -> %s] (%.1f ms): \"%s\" -> \"%s\"", src, tgt, elapsed_ms, text, res)
        assert res is not None and len(res) > 0

        # Verificar términos del glosario protegidos
        for term in glossary:
            if term.lower() in text.lower():
                assert term.lower() in res.lower(), f"Término '{term}' no fue preservado en: '{res}'"

    # 3. Prueba de LocalASR (Warmup, ingesta 100ms, streaming VAD, stop)
    logger.info("Inicializando LocalASR (modelo: base, hardware automático)...")
    asr = LocalASR(model_size="base", device=None, language="auto")
    await asr.start()
    logger.info("LocalASR levantado en: %s (%s)", asr.actual_device, asr.compute_type)

    # Enviar chunks de 100ms de audio sintético (silencio)
    silence_100ms = b"\x00" * 3200
    for _ in range(5):
        await asr.send_audio(silence_100ms)
        await asyncio.sleep(0.01)

    await asr.stop()
    logger.info("LocalASR detenido exitosamente.")

    # 4. Prueba del GeminiTranslator integrado (Local primero, fallback cloud)
    translator = GeminiTranslator(custom_glossary=glossary)
    trans_out = await translator.translate("All systems operational with zero delay.", target_lang="es", source_lang="en")
    logger.info("GeminiTranslator (en -> es): \"%s\"", trans_out)
    assert trans_out is not None and len(trans_out) > 0

    logger.info("=== TODAS LAS PRUEBAS DE HARDWARE, LATENCIA Y TRADUCCIÓN PASARON EXITOSAMENTE ===")


if __name__ == "__main__":
    asyncio.run(main())
