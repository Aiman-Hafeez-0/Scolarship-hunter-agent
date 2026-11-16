"""Groq (fallback) through the official `groq` SDK, with JSON-schema output."""

import time
from collections.abc import Callable

import groq

from pipeline.config import Settings
from pipeline.llm.provider import BaseProvider, ProviderError, TransientError, parse_seconds
from pipeline.schemas import llm_json_schema


class GroqProvider(BaseProvider):
    def __init__(
        self,
        settings: Settings,
        client: groq.Groq | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(settings.groq_model, settings, sleep)
        # max_retries=0: the SDK must not retry on its own; BaseProvider paces and retries.
        self._client = client or groq.Groq(
            api_key=settings.groq_api_key, timeout=settings.llm_timeout_s, max_retries=0
        )

    def _call(self, system: str, user: str) -> tuple[str, int, int]:
        try:
            response = self._client.chat.completions.create(
                model=self.name,
                temperature=0,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                # strict=False ("best effort"): strict mode needs every field to be required,
                # which our schema (with optional fields) does not do. We validate ourselves.
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "page_extraction",
                        "schema": llm_json_schema(),
                        "strict": False,
                    },
                },
            )
        except groq.RateLimitError as exc:
            wait = parse_seconds(exc.response.headers.get("retry-after"))
            raise TransientError(f"HTTP 429: {exc}", status=429, retry_after=wait) from exc
        except groq.InternalServerError as exc:
            raise TransientError(f"HTTP {exc.status_code}: {exc}", status=exc.status_code) from exc
        except groq.APITimeoutError as exc:
            raise TransientError(f"no answer within {self._settings.llm_timeout_s:g} s",
                                 timed_out=True) from exc  # fmt: skip
        except groq.APIConnectionError as exc:
            raise TransientError(f"network error: {exc}") from exc
        except groq.APIError as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc

        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        return response.choices[0].message.content or "", tokens_in, tokens_out
