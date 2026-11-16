"""Groq (fallback) through the official `groq` SDK, with JSON-schema output.

The free tier allows groq_tpm_limit tokens per minute, counting the prompt AND the answer
budget (max_completion_tokens). So before each request we:
1. estimate the prompt tokens (system prompt + page text + JSON schema),
2. give the answer what is left of the limit (minus a safety margin), at most
   groq_max_output_tokens; if less than groq_min_output_tokens is left, the text is too
   large and BaseProvider splits it in half,
3. wait until the request fits in the last 60 seconds' token budget.
On 429 we wait the "try again in X s" time (up to groq_rate_limit_tries calls).
On 413, or when the answer ran out of tokens, the text is split in half.
"""

import json
import time
from collections.abc import Callable
from typing import Any

import groq

from pipeline.config import Settings
from pipeline.llm.provider import (
    BaseProvider,
    InvalidOutput,
    ProviderError,
    TokenWindow,
    TooLarge,
    TransientError,
    estimate_tokens,
    parse_seconds,
    parse_try_again,
)
from pipeline.schemas import llm_json_schema


def groq_error_text(exc: groq.APIStatusError) -> str:
    """ "HTTP 400 json_validate_failed: Failed to generate JSON... (max completion tokens...)"."""
    body: Any = exc.body
    error = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error, dict) or not error.get("message"):
        return f"HTTP {exc.status_code}: {exc}"
    code = f" {error['code']}" if error.get("code") else ""
    detail = f" ({error['failed_generation']})" if error.get("failed_generation") else ""
    return f"HTTP {exc.status_code}{code}: {error['message']}{detail}"


class GroqProvider(BaseProvider):
    def __init__(
        self,
        settings: Settings,
        client: groq.Groq | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(settings.groq_model, settings, sleep)
        self.rate_limit_tries = settings.groq_rate_limit_tries
        self.budget = int(settings.groq_tpm_limit * (1 - settings.groq_tpm_margin))
        self.window = TokenWindow(self.budget, clock=clock, sleep=sleep)
        self._schema = llm_json_schema()
        self._schema_tokens = estimate_tokens(json.dumps(self._schema))
        # max_retries=0: the SDK must not retry on its own; BaseProvider paces and retries.
        self._client = client or groq.Groq(
            api_key=settings.groq_api_key, timeout=settings.llm_timeout_s, max_retries=0
        )

    def output_budget(self, system: str, user: str) -> tuple[int, int]:
        """(estimated prompt tokens, max answer tokens). Raises TooLarge if it cannot fit."""
        prompt = estimate_tokens(system) + estimate_tokens(user) + self._schema_tokens
        room = self.budget - prompt
        if room < self._settings.groq_min_output_tokens:
            raise TooLarge(f"about {prompt} prompt tokens leave only {room} of {self.budget} "
                           "tokens per minute for the answer")  # fmt: skip
        return prompt, min(room, self._settings.groq_max_output_tokens)

    def _call(self, system: str, user: str) -> tuple[str, int, int]:
        prompt_tokens, max_output = self.output_budget(system, user)
        use = self.window.reserve(prompt_tokens + max_output)
        try:
            response = self._send(system, user, max_output)
        except Exception:
            use.tokens = 0  # a refused request used no tokens (on 429, Groq's own wait applies)
            raise

        usage = response.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        if usage:
            use.tokens = tokens_in + tokens_out  # what was really used
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise TooLarge(f"the answer was cut off at {max_output} tokens")
        return choice.message.content or "", tokens_in, tokens_out

    def _send(self, system: str, user: str, max_output: int) -> Any:
        """One request; Groq errors are turned into our error types."""
        try:
            return self._client.chat.completions.create(
                model=self.name,
                temperature=0,
                max_completion_tokens=max_output,
                reasoning_effort=self._settings.groq_reasoning_effort,
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
                        "schema": self._schema,
                        "strict": False,
                    },
                },
            )
        except groq.RateLimitError as exc:
            text = groq_error_text(exc)
            wait = parse_try_again(text) or parse_seconds(exc.response.headers.get("retry-after"))
            raise TransientError(text, status=429, retry_after=wait) from exc
        except groq.InternalServerError as exc:
            raise TransientError(groq_error_text(exc), status=exc.status_code) from exc
        except groq.APITimeoutError as exc:
            raise TransientError(f"no answer within {self._settings.llm_timeout_s:g} s",
                                 timed_out=True) from exc  # fmt: skip
        except groq.APIConnectionError as exc:
            raise TransientError(f"network error: {exc}") from exc
        except groq.APIStatusError as exc:
            text = groq_error_text(exc)
            if exc.status_code == 413 or "max completion tokens" in text:
                raise TooLarge(text) from exc  # split the text and try the halves
            if "json_validate_failed" in text:
                raise InvalidOutput(text, 0, 0) from exc  # retried once with this feedback
            raise ProviderError(f"{self.name}: {text}") from exc
        except groq.APIError as exc:
            raise ProviderError(f"{self.name}: {exc}") from exc
