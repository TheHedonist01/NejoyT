import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from asr.device import detect_compute_device, setup_accelerator_paths

logger = logging.getLogger("translate.local_marian")

# Cache global de modelos cargados: pair -> (translator_or_model, tokenizer, engine_type)
_MODEL_CACHE: Dict[str, Tuple[object, object, str]] = {}


def _get_translator(pair: str) -> Optional[Tuple[object, object, str]]:
    """
    Carga y retorna el traductor para el par de idiomas solicitado ('en-es' o 'es-en').
    Prioriza el modelo cuantizado en CTranslate2 en ./models/, o Transformers como fallback.
    Detecta automáticamente el mejor dispositivo (CUDA / MPS / CPU).
    """
    if pair in _MODEL_CACHE:
        return _MODEL_CACHE[pair]

    setup_accelerator_paths()
    device, compute_type, _ = detect_compute_device()

    ct2_dir = os.path.join(".", "models", f"opus-mt-{pair}-ct2")
    hf_model_id = f"Helsinki-NLP/opus-mt-{pair}"

    # 1. Intentar CTranslate2 (máxima velocidad, baja latencia ~50-80ms)
    if os.path.isdir(ct2_dir):
        try:
            import ctranslate2
            from transformers import MarianTokenizer

            logger.info("Cargando traductor local CTranslate2 [%s] en %s (%s)...", pair, device.upper(), compute_type)
            tokenizer = MarianTokenizer.from_pretrained(hf_model_id)
            translator = ctranslate2.Translator(ct2_dir, device=device, compute_type=compute_type)

            # Warm-up rápido
            warmup_tokens = tokenizer.convert_ids_to_tokens(tokenizer.encode("test"))
            translator.translate_batch([warmup_tokens], beam_size=1)

            _MODEL_CACHE[pair] = (translator, tokenizer, "ctranslate2")
            logger.info("Traductor local CTranslate2 [%s] listo en %s.", pair, device.upper())
            return _MODEL_CACHE[pair]
        except Exception as e:
            logger.warning("No se pudo iniciar CTranslate2 para %s (%s). Intentando Transformers...", pair, e)

    # 2. Fallback a Transformers (MarianMTModel)
    try:
        from transformers import MarianMTModel, MarianTokenizer

        logger.info("Cargando traductor MarianMT vía Transformers [%s] en %s...", pair, device.upper())
        tokenizer = MarianTokenizer.from_pretrained(hf_model_id)
        model = MarianMTModel.from_pretrained(hf_model_id)

        if device == "cuda":
            model = model.to("cuda")
        elif device == "mps":
            model = model.to("mps")

        model.eval()

        # Warm-up
        inp = tokenizer("test", return_tensors="pt")
        if device == "cuda":
            inp = {k: v.to("cuda") for k, v in inp.items()}
        elif device == "mps":
            inp = {k: v.to("mps") for k, v in inp.items()}
        model.generate(**inp, max_length=10)

        _MODEL_CACHE[pair] = (model, tokenizer, "transformers")
        logger.info("Traductor local Transformers [%s] listo en %s.", pair, device.upper())
        return _MODEL_CACHE[pair]
    except Exception as e:
        logger.error("Error al cargar modelo de traducción local para %s: %s", pair, e)
        return None


def translate_local_sentence(
    text: str,
    source_lang: str,
    target_lang: str,
    glossary: Optional[List[str]] = None
) -> Optional[str]:
    """
    Traduce una frase localmente de forma agnóstica de hardware.
    Soporta glosario técnico protegiendo términos antes de la inferencia.
    """
    cleaned = text.strip()
    if not cleaned:
        return ""

    src = source_lang.strip().lower()[:2]
    tgt = target_lang.strip().lower()[:2]
    if src == tgt:
        return cleaned

    pair = f"{src}-{tgt}"
    trans_info = _get_translator(pair)
    if not trans_info:
        return None

    model_or_trans, tokenizer, engine_type = trans_info

    # 1. Proteger términos del glosario técnico (usando límites de palabra para evitar colisiones)
    protected_text = cleaned
    replacements: Dict[str, str] = {}
    if glossary:
        for idx, term in enumerate(sorted(glossary, key=len, reverse=True)):
            clean_term = term.strip()
            if not clean_term:
                continue
            pattern = re.compile(r'\b' + re.escape(clean_term) + r'\b', re.IGNORECASE)
            if pattern.search(protected_text):
                token_placeholder = f"GLOSSTERM{idx}"
                replacements[token_placeholder] = clean_term
                protected_text = pattern.sub(token_placeholder, protected_text)

    t0 = time.time()
    try:
        if engine_type == "ctranslate2":
            tokens = tokenizer.convert_ids_to_tokens(tokenizer.encode(protected_text))
            results = model_or_trans.translate_batch([tokens], beam_size=1)
            translated_tokens = results[0].hypotheses[0]
            translated_ids = tokenizer.convert_tokens_to_ids(translated_tokens)
            translated_text = tokenizer.decode(translated_ids, skip_special_tokens=True).strip()
        else:
            device, _, _ = detect_compute_device()
            inputs = tokenizer(protected_text, return_tensors="pt")
            if device in ("cuda", "mps"):
                inputs = {k: v.to(device) for k, v in inputs.items()}
            outputs = model_or_trans.generate(**inputs, max_length=128)
            translated_text = tokenizer.decode(outputs[0], skip_special_tokens=True).strip()

        # 2. Restaurar términos del glosario respetando mayúsculas/minúsculas originales
        for placeholder, original_term in replacements.items():
            translated_text = re.sub(r'\b' + re.escape(placeholder) + r'\b', original_term, translated_text, flags=re.IGNORECASE)
            # Fallback en caso de que el tokenizador haya quitado espacios adyacentes
            translated_text = re.sub(re.escape(placeholder), original_term, translated_text, flags=re.IGNORECASE)

        latency_ms = (time.time() - t0) * 1000
        logger.debug("[Traducción Local %s] %s -> %s en %.1fms", pair, cleaned[:30], translated_text[:30], latency_ms)
        return translated_text

    except Exception as e:
        logger.warning("Fallo en inferencia de traducción local (%s): %s", pair, e)
        return None
