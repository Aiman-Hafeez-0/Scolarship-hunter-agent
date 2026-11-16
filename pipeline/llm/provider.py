"""One small interface for "ask an LLM to extract a page", plus the shared plumbing.

    extract(page_text, source) -> (PageExtraction, tokens_in, tokens_out)

Errors a provider can raise:
- InvalidOutput: the model answered, but not with JSON matching the schema. The caller may
  retry once with the error message as feedback.
- ProviderError: the call itself failed (bad key, quota used up, still rate limited after
  retries, ...). FallbackProvider then tries the next provider.
- TooLarge (internal): the request does not fit the provider's limits. BaseProvider then
  splits the text in half and extracts each half.
"""

import json
import logging
import math
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from pipeline.config import Settings
from pipeline.models import Source
from pipeline.prompts import extract_system_prompt, extract_user_message
from pipeline.schemas import PageExtraction

logger = logging.getLogger(__name__)

MAX_FEEDBACK_CHARS = 2000
MIN_SPLIT_CHARS = 1000  # never split text shorter than this; give up instead
CHARS_PER_TOKEN = 3  # a cautious estimate: real text averages about 4 characters per token


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


class TooLarge(Exception):
    """The request is too big for the provider (HTTP 413, or no room left for the answer)."""


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
    """Validate the model's JSON answer, or raise InvalidOutput with the reason.

    Some models add keys that are not in the schema with an empty value, such as
    "status_evidence": null. Such keys say nothing, so they are dropped and the answer is
    checked again. An unknown key WITH a value is still an error.
    """
    try:
        return PageExtraction.model_validate_json(text)
    except ValidationError as exc:
        error = exc
    cleaned = _without_empty_extra_keys(text, error)
    if cleaned is not None:
        try:
            return PageExtraction.model_validate(cleaned)
        except ValidationError as exc:
            error = exc
    raise InvalidOutput(str(error)[:MAX_FEEDBACK_CHARS], tokens_in, tokens_out) from error


def _without_empty_extra_keys(text: str, error: ValidationError) -> Any | None:
    """The answer without unknown empty keys, or None if it has other problems too."""
    problems = error.errors()
    if not all(p["type"] == "extra_forbidden" and p["input"] in (None, "", [], {})
               for p in problems):  # fmt: skip
        return None
    data = json.loads(text)
    for problem in problems:
        *path, key = problem["loc"]
        node = data
        for step in path:
            node = node[step]
        node.pop(key, None)
    return data


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


def parse_try_again(message: str) -> float | None:
    """The wait in "Please try again in 29.88s" / "1m2.5s" / "2m" / "45ms", in seconds."""
    match = re.search(r"try again in\s+((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", message)
    if match is None:
        return None
    unit_seconds = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", match.group(1))
    return sum(float(number) * unit_seconds[unit] for number, unit in parts)


def estimate_tokens(text: str) -> int:
    """A cautious token count for a text (it is better to guess too many than too few)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def split_in_half(text: str) -> tuple[str, str]:
    """Cut text in two at the line break (or else the space) nearest the middle."""
    middle = len(text) // 2
    for separator in ("\n", " "):
        before, after = text.rfind(separator, 0, middle), text.find(separator, middle)
        cuts = [c for c in (before, after) if c > 0]
        if cuts:
            cut = min(cuts, key=lambda c: abs(c - middle))
            return text[:cut].strip(), text[cut:].strip()
    return text[:middle], text[middle:]


@dataclass
class _Use:
    time: float
    tokens: int


class TokenWindow:
    """Keep the tokens used in any 60 seconds under `limit` by waiting before a request.

    reserve(n) waits until n more tokens fit, then records them. The caller can correct the
    record afterwards with the real count (use.tokens = ...).
    """

    def __init__(
        self,
        limit: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        window_s: float = 60.0,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._sleep = sleep
        self._uses: list[_Use] = []

    def used(self) -> int:
        cutoff = self._clock() - self.window_s
        self._uses = [u for u in self._uses if u.time > cutoff]
        return sum(u.tokens for u in self._uses)

    def reserve(self, tokens: int) -> _Use:
        if tokens > self.limit:
            raise TooLarge(f"needs about {tokens} tokens, more than {self.limit} per minute")
        while self.used() + tokens > self.limit:
            wait = self._uses[0].time + self.window_s - self._clock() + 0.1
            logger.info("waiting %.0f s for the per-minute token limit (%d used, %d needed)",
                        wait, self.used(), tokens)  # fmt: skip
            self._sleep(max(wait, 0.1))
        use = _Use(self._clock(), tokens)
        self._uses.append(use)
        return use


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
        self.available = True  # False once the daily quota is used up: not called again
        self.rate_limit_tries = settings.llm_max_tries  # calls per request after HTTP 429
        self._settings = settings
        self._sleep = sleep
        self._pacer = Pacer(settings.llm_min_interval_s, sleep=sleep)

    def extract(
        self, page_text: str, source: Source, feedback: str | None = None
    ) -> tuple[PageExtraction, int, int]:
        """Extract the text; if the request is too large, extract each half instead."""
        if not self.available:
            raise ProviderError(f"{self.name}: daily quota used up, not called again this run")
        system = extract_system_prompt(source)
        user = extract_user_message(page_text, feedback)
        try:
            text, tokens_in, tokens_out = self._call_with_retries(system, user)
        except TooLarge as exc:
            if len(page_text) < MIN_SPLIT_CHARS:
                raise ProviderError(f"{self.name}: request too large: {exc}") from exc
            first, second = split_in_half(page_text)
            logger.info("%s: %s; splitting %d chars into %d + %d", self.name, exc,
                        len(page_text), len(first), len(second))  # fmt: skip
            page_a, in_a, out_a = self.extract(first, source, feedback)
            page_b, in_b, out_b = self.extract(second, source, feedback)
            both = PageExtraction(scholarships=page_a.scholarships + page_b.scholarships)
            return both, in_a + in_b, out_a + out_b
        self.last_model = self.name
        return parse_output(text, tokens_in, tokens_out), tokens_in, tokens_out

    def _call_with_retries(self, system: str, user: str) -> tuple[str, int, int]:
        attempt = 0
        while True:
            attempt += 1
            self._pacer.wait()
            try:
                return self._call(system, user)
            except TransientError as exc:
                limit = self.rate_limit_tries if exc.status == 429 else self._settings.llm_max_tries
                delay = retry_delay(exc, self._settings)
                if delay is None or attempt >= max(1, limit):
                    raise ProviderError(f"{self.name}: {exc}") from exc
                logger.warning("%s: %s; retrying in %.0f s", self.name, exc, delay)
                self._sleep(delay)

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
        # A primary whose daily quota is used up is skipped quietly (it logged that once).
        if getattr(self.primary, "available", True):
            try:
                result = self.primary.extract(page_text, source, feedback)
                self.last_model = self.primary.last_model
                return result
            except ProviderError as exc:
                if getattr(self.primary, "available", True):
                    logger.warning("%s failed (%s); falling back to %s", self.primary.name,
                                   exc, self.secondary.name)  # fmt: skip
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
