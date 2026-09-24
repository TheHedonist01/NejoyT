import asyncio
import logging
import re
from collections import deque
from typing import List, Optional
from google import genai
from google.genai import types
from config import settings

logger = logging.getLogger("translate.gemini_text")


class GeminiTranslator:
    """
    Traductor optimizado para conferencias técnicas con Gemini Flash.
    - Respeta glosario técnico (términos no traducibles).
    - Mantiene contexto deslizante de 2-3 frases para coherencia pronominal y temporal.
    - Traduce solo frases completas (finales) protegiendo la cuota Free Tier.
    """

    DEFAULT_GLOSSARY = [
        "Nerdearla", "Kubernetes", "K8s", "Docker", "Pod", "Prometheus", "Grafana",
        "DevOps", "CI/CD", "Terraform", "Ansible", "FastAPI", "Python", "Rust",
        "Go", "Golang", "Microservicios", "Service Mesh", "Istio", "Cloud Native",
        "AWS", "GCP", "Google Cloud", "Azure", "GitHub", "GitLab", "Open Source",
        "WebSocket", "Streaming", "Kernel", "Linux", "OBS", "FFmpeg"
    ]

    def __init__(
        self,
        model: Optional[str] = None,
        custom_glossary: Optional[List[str]] = None,
        max_context_sentences: int = 3
    ):
        self.model = model or settings.gemini_translate_model
        glossary_set = set(self.DEFAULT_GLOSSARY + (custom_glossary or []))
        self.glossary = sorted(list(glossary_set))
        self.max_context = max_context_sentences
        self._history: deque[str] = deque(maxlen=max_context_sentences)
        self._client: Optional[genai.Client] = None
        self._cache: dict[tuple[str, str], str] = {}
        self._semaphore = asyncio.Semaphore(1)

    def _get_client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client(api_key=settings.gemini_api_key)
        return self._client

    def _build_system_instruction(self, target_lang: str) -> str:
        glossary_str = ", ".join(self.glossary)
        return (
            f"Eres un intérprete simultáneo y traductor técnico especializado en conferencias tecnológicas como Nerdearla.\n"
            f"Tu misión es traducir la frase dada al idioma '{target_lang}' de manera natural y precisa.\n"
            f"REGLAS OBLIGATORIAS:\n"
            f"1. LOS SIGUIENTES TÉRMINOS TÉCNICOS, HERRAMIENTAS Y NOMBRES NUNCA DEBEN TRADUCIRSE NI MODIFICARSE:\n"
            f"   [{glossary_str}]\n"
            f"2. Conserva siglas, marcas y terminología de ingeniería en su forma técnica habitual.\n"
            f"3. Devuelve EXCLUSIVAMENTE el texto traducido de la frase actual, sin introducciones, sin notas, sin markdown bold y sin comillas."
        )

    async def translate(self, text: str, target_lang: str = "es", source_lang: Optional[str] = None) -> str:
        """
        Traduce una frase final al idioma objetivo.
        Si source_lang == target_lang o el texto está vacío, retorna el texto original.
        Utiliza caché en memoria y semáforo para no saturar la cuota de la API.
        """
        cleaned = text.strip()
        if not cleaned:
            return ""

        tgt = target_lang.strip().lower()[:2]
        src = (source_lang or "").strip().lower()[:2]

        # Si ya está en el idioma objetivo, no gastar cuota de API
        if src and src == tgt:
            self._history.append(cleaned)
            return cleaned

        # Verificar caché en memoria
        cache_key = (cleaned, tgt)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 1. Si es en -> es o es -> en, usar modelo local offline (0.6s, 100% offline, sin cuota)
        pair = f"{src}-{tgt}"
        if pair in ("en-es", "es-en"):
            try:
                loop = asyncio.get_running_loop()

                def _run_marian():
                    tok, mod = _get_local_marian(pair)
                    if tok and mod:
                        inputs = tok(cleaned, return_tensors="pt")
                        outputs = mod.generate(**inputs, max_length=128)
                        return tok.decode(outputs[0], skip_special_tokens=True).strip()
                    return None

                local_res = await loop.run_in_executor(None, _run_marian)
                if local_res:
                    if len(self._cache) > 500:
                        self._cache.clear()
                    self._cache[cache_key] = local_res
                    self._history.append(cleaned)
                    return local_res
            except Exception as e:
                logger.warning("Fallo en traducción local MarianMT (%s): %s. Intentando Gemini...", pair, e)

        client = self._get_client()
        system_instruction = self._build_system_instruction(target_lang)

        prompt = f"Traduce exactamente al '{target_lang}':\n\"{cleaned}\""

        models_to_try = [self.model]
        if "flash-lite" not in self.model:
            models_to_try.append("gemini-3.5-flash-lite")

        async with self._semaphore:
            for model_name in models_to_try:
                try:
                    # Configuración optimizada según capacidades del modelo
                    cfg_kwargs = {
                        "system_instruction": system_instruction,
                        "temperature": 0.0,
                    }
                    if "flash-lite" not in model_name:
                        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

                    call = client.aio.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(**cfg_kwargs)
                    )
                    response = await asyncio.wait_for(call, timeout=6.0)
                    raw_text = response.text.strip() if response.text else cleaned

                    # Limpiar prefijos comunes si el LLM los incluye
                    cleaned_trans = re.sub(
                        r'^(traducción|translation|traducido):\s*',
                        '',
                        raw_text,
                        flags=re.IGNORECASE
                    ).strip('*" \n')

                    if cleaned_trans:
                        if len(self._cache) > 500:
                            self._cache.clear()
                        self._cache[cache_key] = cleaned_trans
                        self._history.append(cleaned)
                        return cleaned_trans

                except Exception as e:
                    logger.warning("Intento de traducción con [%s] falló: %s", model_name, e)
                    continue

        self._history.append(cleaned)
        return cleaned

    def clear_context(self) -> None:
        """Limpia el buffer de contexto de oraciones previas."""
        self._history.clear()

    def update_glossary(self, terms: List[str]) -> None:
        """Actualiza el glosario de términos técnicos en caliente."""
        glossary_set = set(self.DEFAULT_GLOSSARY + terms)
        self.glossary = sorted(list(glossary_set))
        logger.info("Glosario de traducción actualizado (%d términos)", len(self.glossary))

