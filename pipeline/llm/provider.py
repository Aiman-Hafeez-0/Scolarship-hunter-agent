"""One small interface for "ask an LLM to extract a page", plus the shared plumbing.

    extract(page_text, source) -> (PageExtraction, tokens_in, tokens_out)

Errors a provider can raise:
- InvalidOutput: the model answered, but not with JSON matching the schema. The caller may
  retry once with the error message as feedback.
- ProviderError: the call itself failed (bad key, quota used up, still rate limited after
  retries, ...). FallbackProvider then tries the next provider.
"""

import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Literal, Protocol

from pydantic import ValidationError

from pipeline.config import Settings
from pipeline.models import Source
from pipeline.prompts import extract_system_prompt, extract_user_message
from pipeline.schemas import PageExtraction

logger = logging.getLogger(__name__)

MAX_FEEDBACK_CHARS = 2000


class ProviderError(Exception):
    """The LLM call failed. Another provider might still work."""


class ProviderConfigError(ValueError):
    """No usable API key in the settings."""


ProviderChoice = Literal["auto", "gemini", "groq"]
PROVIDER_CHOICES: tuple[ProviderChoice, ...] = ("auto", "gemini", "groq")


class TransientError(Exception):
    """A failure that may work if tried again soon.

    - status 429: rate limited. `retry_after` is how long the server asked us to wait
      (None if it did not say).
    - status 5xx (e.g. 503/504): the service is busy or down.
    - status None: a network problem. `timed_out=True` means the call took too long.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        retry_after: float | None = None,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.timed_out = timed_out


class InvalidOutput(Exception):
    """The model's answer did not match the schema. Carries the tokens it still cost."""

    def __init__(self, message: str, tokens_in: int, tokens_out: int) -> None:
        super().__init__(message)
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


class LLMProvider(Protocol):
    name: str
    last_model: str  # the model that produced the most recent answer

    def extract(
        self, page_text: str, source: Source, feedback: str | None = None
    ) -> tuple[PageExtraction, int, int]: ...


def parse_output(text: str, tokens_in: int, tokens_out: int) -> PageExtraction:
    """Validate the model's JSON answer, or raise InvalidOutput with the reason."""
    try:
        return PageExtraction.model_validate_json(text)
    except ValidationError as exc:
        raise InvalidOutput(str(exc)[:MAX_FEEDBACK_CHARS], tokens_in, tokens_out) from exc


class Pacer:
    """Leave at least `min_interval_s` seconds between calls (free tiers limit calls/minute)."""

    def __init__(
        self,
        min_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.min_interval_s - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def retry_delay(exc: TransientError, settings: Settings) -> float | None:
    """How long to wait before trying again, or None to give up now (fail fast).

    - Timeout: give up. Each call may already have taken llm_timeout_s.
    - 429: wait as long as the server asks. If it asks for longer than
      llm_max_retry_wait_s (e.g. the daily quota is used up), give up.
      If it does not say, wait llm_retry_backoff_s.
    - 5xx or another network problem: wait llm_retry_backoff_s.
    """
    if exc.timed_out:
        return None
    if exc.status == 429 and exc.retry_after is not None:
        return exc.retry_after if exc.retry_after <= settings.llm_max_retry_wait_s else None
    return settings.llm_retry_backoff_s


def parse_seconds(value: object) -> float | None:
    """ "17", "17s", "1.5s" or 17 -> seconds as a float; anything else -> None."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*s?\s*", str(value)) if value is not None else None
    return float(match.group(1)) if match else None


class BaseProvider(ABC):
    """Shared logic: build the prompt, pace calls, retry 429/5xx, validate the answer.

    A concrete provider only implements `_call`, which sends one request and returns
    (answer_text, tokens_in, tokens_out). It raises TransientError for 429/5xx/network
    problems and ProviderError for everything else.

    Each page gets at most `llm_max_tries` calls (see retry_delay for the waits); after
    that a ProviderError is raised, so FallbackProvider can switch to the next provider.
    """

    def __init__(
        self, model: str, settings: Settings, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.name = model
        self.last_model = model
        self._settings = settings
        self._sleep = sleep
        self._pacer = Pacer(settings.llm_min_interval_s, sleep=sleep)

    def extract(
        self, page_text: str, source: Source, feedback: str | None = None
    ) -> tuple[PageExtraction, int, int]:
        system = extract_system_prompt(source)
        user = extract_user_message(page_text, feedback)
        text, tokens_in, tokens_out = self._call_with_retries(system, user)
        self.last_model = self.name
        return parse_output(text, tokens_in, tokens_out), tokens_in, tokens_out

    def _call_with_retries(self, system: str, user: str) -> tuple[str, int, int]:
        tries = max(1, self._settings.llm_max_tries)
        for attempt in range(1, tries + 1):
            self._pacer.wait()
            try:
                return self._call(system, user)
            except TransientError as exc:
                delay = retry_delay(exc, self._settings)
                if delay is None or attempt == tries:
                    raise ProviderError(f"{self.name}: {exc}") from exc
                logger.warning("%s: %s; retrying in %.0f s", self.name, exc, delay)
                self._sleep(delay)
        raise AssertionError("unreachable")  # the loop always returns or raises

    @abstractmethod
    def _call(self, system: str, user: str) -> tuple[str, int, int]: ...


class FallbackProvider:
    """Use `primary`; if its call fails (ProviderError), try `secondary` instead.

    InvalidOutput is not a call failure, so it is passed on for the caller to handle.
    """

    def __init__(self, primary: LLMProvider, secondary: LLMProvider) -> None:
        self.primary = primary
        self.secondary = secondary
        self.name = f"{primary.name} -> {secondary.name}"
        self.last_model = primary.name

    def extract(
        self, page_text: str, source: Source, feedback: str | None = None
    ) -> tuple[PageExtraction, int, int]:
        try:
            result = self.primary.extract(page_text, source, feedback)
            self.last_model = self.primary.last_model
        except ProviderError as exc:
            logger.warning("%s failed (%s); falling back to %s", self.primary.name, exc,
                           self.secondary.name)  # fmt: skip
            result = self.secondary.extract(page_text, source, feedback)
            self.last_model = self.secondary.last_model
        return result


def build_provider(settings: Settings, choice: ProviderChoice = "auto") -> LLMProvider:
    """Which LLM to use.

    auto:   Gemini first, Groq as fallback; whichever keys are set in .env.
    gemini: only Gemini (needs GEMINI_API_KEY).
    groq:   only Groq (needs GROQ_API_KEY).
    """
    # Imported here so tests and other commands never load the SDKs they do not need.
    from pipeline.llm.gemini_provider import GeminiProvider
    from pipeline.llm.groq_provider import GroqProvider

    if choice == "gemini":
        if not settings.gemini_api_key:
            raise ProviderConfigError("--provider gemini needs GEMINI_API_KEY in .env")
        return GeminiProvider(settings)
    if choice == "groq":
        if not settings.groq_api_key:
            raise ProviderConfigError("--provider groq needs GROQ_API_KEY in .env")
        return GroqProvider(settings)

    providers: list[LLMProvider] = []
    if settings.gemini_api_key:
        providers.append(GeminiProvider(settings))
    if settings.groq_api_key:
        providers.append(GroqProvider(settings))

    if not providers:
        raise ProviderConfigError("Set GEMINI_API_KEY (and GROQ_API_KEY as a fallback) in .env")
    if len(providers) == 1:
        return providers[0]
    return FallbackProvider(providers[0], providers[1])
