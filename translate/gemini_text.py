import asyncio
import logging
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
            f"3. Si se proporciona contexto previo, úsalo ÚNICAMENTE para mantener coherencia de género, número y tiempo verbal.\n"
            f"4. Devuelve EXCLUSIVAMENTE el texto traducido de la frase actual, sin introducciones, sin comillas, ni explicaciones."
        )

    async def translate(self, text: str, target_lang: str = "es", source_lang: Optional[str] = None) -> str:
        """
        Traduce una frase final al idioma objetivo.
        Si source_lang == target_lang o el texto está vacío, retorna el texto original.
        """
        cleaned = text.strip()
        if not cleaned:
            return ""

        # Si ya está en el idioma objetivo, no gastar cuota de API
        if source_lang and source_lang.lower().startswith(target_lang.lower()):
            self._history.append(cleaned)
            return cleaned

        client = self._get_client()
        system_instruction = self._build_system_instruction(target_lang)

        context_str = ""
        if self._history:
            context_str = "Contexto previo de la charla:\n" + "\n".join(f"- {h}" for h in self._history) + "\n\n"

        prompt = f"{context_str}Frase a traducir al '{target_lang}':\n\"{cleaned}\""

        try:
            call = client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.1,
                    thinking_config=types.ThinkingConfig(thinking_budget=0)
                )
            )
            # Timeout para subtítulos en vivo (evita retrasos acumulados)
            response = await asyncio.wait_for(call, timeout=5.0)
            translated_text = response.text.strip().strip('"') if response.text else cleaned
            self._history.append(cleaned)
            return translated_text


        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("Fallo o timeout en traducción con Gemini Flash (%s): %s. Devolviendo original.", self.model, e)
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

