import asyncio
import logging
from translate.gemini_text import GeminiTranslator
from bus import event_bus, SubtitleEvent
from store import subtitle_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_pipeline")


async def main():
    logger.info("Probando pipeline de traducción técnica, event bus y almacén SRT/VTT...")
    
    room_id = "main-auditorium"
    subtitle_store.start_room_clock(room_id)
    
    # 1. Suscribir cliente ficticio al EventBus (escuchando todos los idiomas para la prueba)
    sub_queue = await event_bus.subscribe(room_id, lang="all")
    
    # 2. Instanciar traductor con glosario
    translator = GeminiTranslator(
        custom_glossary=["Nerdearla", "Kubernetes", "FastAPI"]
    )
    
    test_phrases = [
        "Welcome everyone to Nerdearla 2026 conference.",
        "Today we are demonstrating a real-time subtitle architecture running on FastAPI and Kubernetes.",
        "This system connects directly to Gemini Live streaming API with zero latency."
    ]
    
    for phrase in test_phrases:
        # Evento original
        orig_event = SubtitleEvent(
            room_id=room_id,
            event_type="final",
            text=phrase,
            language="en",
            is_final=True
        )
        event_bus.publish(room_id, orig_event)
        
        # Traducción con Gemini Flash
        logger.info("Traduciendo con glosario: \"%s\"", phrase)
        translation = await translator.translate(phrase, target_lang="es", source_lang="en")
        print(f" -> ES: \"{translation}\"")
        
        # Evento traducido
        trans_event = SubtitleEvent(
            room_id=room_id,
            event_type="translation",
            text=translation,
            language="es",
            is_final=True
        )
        event_bus.publish(room_id, trans_event)
        
        # Guardar en almacén
        subtitle_store.add_subtitle(
            room_id=room_id,
            original_text=phrase,
            original_lang="en",
            translations={"es": translation}
        )
        
        # Verificar términos en el glosario no traducidos
        for term in ["Nerdearla", "Kubernetes", "FastAPI"]:
            if term.lower() in phrase.lower():
                assert term.lower() in translation.lower(), f"Término clave '{term}' fue traducido o eliminado!"

    # 3. Verificar recepción en cola del suscriptor
    events_collected = []
    while not sub_queue.empty():
        events_collected.append(sub_queue.get_nowait())
        
    await event_bus.unsubscribe(room_id, sub_queue)
    logger.info("Eventos recibidos por el suscriptor del bus: %d", len(events_collected))
    assert len(events_collected) == len(test_phrases) * 2
    
    # 4. Verificar exportación SRT y VTT
    srt_out = subtitle_store.export_srt(room_id, lang="es")
    vtt_out = subtitle_store.export_vtt(room_id, lang="es")
    
    print("\n--- Muestra SRT exportado ---")
    print(srt_out)
    
    print("--- Muestra WebVTT exportado ---")
    print(vtt_out)
    
    assert "-->" in srt_out
    assert "WEBVTT" in vtt_out
    
    print("[OK] PRUEBA DE PIPELINE DE TRADUCCION, BUS Y EXPORT SRT/VTT EXITOSA.")


if __name__ == "__main__":
    asyncio.run(main())
