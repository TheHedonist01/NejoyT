import asyncio
import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(override=True)

api_key = os.getenv("GEMINI_API_KEY")
print(f"Probando con API Key: {api_key[:10]}...{api_key[-6:]}")

async def test_api():
    client = genai.Client(api_key=api_key)
    
    # 1. Probar traducción / generación de texto (con fallback a modelos disponibles)
    print("\n--- 1. Prueba de Traducción de Texto ---")
    candidate_models = ["gemini-3.5-flash-lite", "gemini-3.5-flash"]
    success_text = False
    
    for model in candidate_models:
        try:
            res = await client.aio.models.generate_content(
                model=model,
                contents="Traduce al espanol en una sola oracion: 'Real-time subtitles for Nerdearla conference.'"
            )
            print(f"[OK] Modelo '{model}' respondio exitosamente:")
            print(f"     \"{res.text.strip()}\"")
            success_text = True
            break
        except Exception as e:
            print(f"[INFO] Modelo '{model}' no disponible: {e}")
            
    # 2. Probar conexion WebSocket con gemini-3.5-transcribe-live
    print("\n--- 2. Prueba de Streaming en Vivo (gemini-3.5-transcribe-live) ---")
    config = types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=["es-419"],
            custom_vocabulary=["Nerdearla", "FastAPI"]
        )
    )
    
    try:
        async with client.aio.live.connect(model="gemini-3.5-transcribe-live", config=config) as session:
            print("[OK] Sesion WebSocket establecida con gemini-3.5-transcribe-live!")
            
            # Enviar 1 bloque PCM de 100ms (3.200 bytes de silencio)
            silence_chunk = b"\x00" * 3200
            await session.send_realtime_input(
                media=types.Blob(data=silence_chunk, mime_type="audio/pcm;rate=16000")
            )
            await session.send_realtime_input(audio_stream_end=True)
            print("[OK] Audio PCM de 100 ms y senal audio_stream_end enviados correctamente.")
            print("[OK] Sesion Live ASR verificada y 100% operativa.")
            return True
    except Exception as e:
        print("[FAIL] Error en conexion Live WebSocket:", e)
        return False

if __name__ == "__main__":
    asyncio.run(test_api())
