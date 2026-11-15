"""Gemini through Google's official `google-genai` SDK, with JSON-schema output."""

import time
from collections.abc import Callable

import httpx
from google import genai
from google.genai import errors, types

from pipeline.config import Settings
from pipeline.llm.provider import BaseProvider, ProviderError, TransientError
from pipeline.schemas import llm_json_schema

TIMEOUT_MS = 120_000


class GeminiProvider(BaseProvider):
    def __init__(
        self,
        settings: Settings,
        client: genai.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(settings.gemini_model, settings, sleep)
        # attempts=1: the SDK must not retry on its own; BaseProvider paces and retries.
        self._client = client or genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=TIMEOUT_MS, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

    def _call(self, system: str, user: str) -> tuple[str, int, int]:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            response_mime_type="application/json",
            response_json_schema=llm_json_schema(),
        )
        try:
            response = self._client.models.generate_content(
                model=self.name, contents=user, config=config
            )
        except errors.APIError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise TransientError(f"HTTP {exc.code}: {exc}") from exc
            raise ProviderError(f"{self.name}: HTTP {exc.code}: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"network error: {exc}") from exc

        usage = response.usage_metadata
        tokens_in = (usage.prompt_token_count or 0) if usage else 0
        # Thinking tokens are billed and rate limited like output tokens, so count them too.
        tokens_out = (
            (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0) if usage else 0
        )
        return response.text or "", tokens_in, tokens_out
