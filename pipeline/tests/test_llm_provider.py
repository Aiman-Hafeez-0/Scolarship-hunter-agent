"""Tests for pipeline.llm. Real SDK clients are replaced by small fakes: no API calls."""

from types import SimpleNamespace
from typing import Any, cast

import groq
import httpx
import pytest
from google import genai
from google.genai import errors

from pipeline.config import Settings
from pipeline.llm.gemini_provider import GeminiProvider
from pipeline.llm.groq_provider import GroqProvider
from pipeline.llm.provider import (
    BaseProvider,
    FallbackProvider,
    InvalidOutput,
    Pacer,
    ProviderConfigError,
    ProviderError,
    TransientError,
    build_provider,
    parse_output,
)
from pipeline.schemas import PageExtraction
from pipeline.tests.conftest import FakeProvider, make_source

GOOD_JSON = (
    '{"scholarships": [{"name": "KAUST Fellowship", "opportunity_type": "scholarship",'
    ' "location": "abroad"}]}'
)


@pytest.fixture
def llm_settings() -> Settings:
    return Settings(
        _env_file=None,
        gemini_api_key="test-gemini",
        groq_api_key="test-groq",
        llm_min_interval_s=0,
        llm_retry_backoff_s=10,
        llm_retries=2,
    )


class Sleeps:
    """Records sleeps instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class ScriptedProvider(BaseProvider):
    """A BaseProvider whose `_call` returns or raises scripted outcomes."""

    def __init__(self, settings: Settings, sleep: Sleeps, *outcomes: Any) -> None:
        super().__init__("scripted", settings, sleep)
        self.outcomes = list(outcomes)

    def _call(self, system: str, user: str) -> tuple[str, int, int]:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return cast(tuple[str, int, int], outcome)


# ---------- parse_output and Pacer ----------


def test_parse_output_valid() -> None:
    assert parse_output(GOOD_JSON, 1, 2).scholarships[0].name == "KAUST Fellowship"


@pytest.mark.parametrize("text", ["not json", "", '{"scholarships": [{"name": "x"}]}'])
def test_parse_output_invalid_keeps_tokens(text: str) -> None:
    with pytest.raises(InvalidOutput) as info:
        parse_output(text, 11, 22)
    assert (info.value.tokens_in, info.value.tokens_out) == (11, 22)


def test_pacer_waits_between_calls() -> None:
    now = [100.0]
    sleeps = Sleeps()
    pacer = Pacer(5.0, clock=lambda: now[0], sleep=sleeps)

    pacer.wait()  # first call: no wait
    now[0] += 2.0
    pacer.wait()  # only 2 s later: wait the remaining 3 s
    now[0] += 10.0
    pacer.wait()  # long enough ago: no wait

    assert sleeps.calls == [3.0]


# ---------- BaseProvider retries ----------


def test_retries_429_with_backoff_then_succeeds(llm_settings: Settings) -> None:
    sleeps = Sleeps()
    provider = ScriptedProvider(
        llm_settings, sleeps, TransientError("429"), TransientError("429"), (GOOD_JSON, 50, 7)
    )

    page, tokens_in, tokens_out = provider.extract("page", make_source())

    assert page.scholarships[0].name == "KAUST Fellowship"
    assert (tokens_in, tokens_out) == (50, 7)
    assert sleeps.calls == [10, 20]  # exponential backoff


def test_gives_up_after_retries(llm_settings: Settings) -> None:
    provider = ScriptedProvider(llm_settings, Sleeps(), *[TransientError("429")] * 3)
    with pytest.raises(ProviderError, match="429"):
        provider.extract("page", make_source())


def test_non_transient_error_is_not_retried(llm_settings: Settings) -> None:
    provider = ScriptedProvider(llm_settings, Sleeps(), ProviderError("bad key"), GOOD_JSON)
    with pytest.raises(ProviderError, match="bad key"):
        provider.extract("page", make_source())
    assert len(provider.outcomes) == 1  # second outcome never used


# ---------- FallbackProvider ----------


def test_fallback_used_when_primary_fails() -> None:
    page = PageExtraction()
    primary = FakeProvider(ProviderError("quota"), name="gemini")
    secondary = FakeProvider(page, name="groq")
    provider = FallbackProvider(primary, secondary)

    assert provider.extract("text", make_source())[0] is page
    assert provider.last_model == "groq"


def test_fallback_not_used_when_primary_works() -> None:
    primary = FakeProvider(PageExtraction(), name="gemini")
    secondary = FakeProvider(name="groq")
    provider = FallbackProvider(primary, secondary)

    provider.extract("text", make_source())

    assert provider.last_model == "gemini"
    assert secondary.calls == []


def test_invalid_output_is_not_a_reason_to_fall_back() -> None:
    primary = FakeProvider(InvalidOutput("bad", 1, 1), name="gemini")
    secondary = FakeProvider(name="groq")
    with pytest.raises(InvalidOutput):
        FallbackProvider(primary, secondary).extract("text", make_source())
    assert secondary.calls == []


# ---------- build_provider ----------


def test_build_provider_both_keys(llm_settings: Settings) -> None:
    provider = build_provider(llm_settings)
    assert isinstance(provider, FallbackProvider)
    assert provider.name == "gemini-3.8-flash -> openai/gpt-oss-120b"


def test_build_provider_one_key(llm_settings: Settings) -> None:
    only_groq = llm_settings.model_copy(update={"gemini_api_key": None})
    assert isinstance(build_provider(only_groq), GroqProvider)


def test_build_provider_no_keys(llm_settings: Settings) -> None:
    no_keys = llm_settings.model_copy(update={"gemini_api_key": None, "groq_api_key": None})
    with pytest.raises(ProviderConfigError, match="GEMINI_API_KEY"):
        build_provider(no_keys)


# ---------- Gemini (fake client) ----------


class FakeGeminiModels:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _gemini(settings: Settings, *outcomes: Any) -> tuple[GeminiProvider, FakeGeminiModels]:
    models = FakeGeminiModels(*outcomes)
    client = cast(genai.Client, SimpleNamespace(models=models))
    return GeminiProvider(settings, client=client, sleep=Sleeps()), models


def _gemini_response(text: str) -> Any:
    usage = SimpleNamespace(prompt_token_count=1000, candidates_token_count=200,
                            thoughts_token_count=50)  # fmt: skip
    return SimpleNamespace(text=text, usage_metadata=usage)


def test_gemini_request_and_tokens(llm_settings: Settings) -> None:
    provider, models = _gemini(llm_settings, _gemini_response(GOOD_JSON))

    page, tokens_in, tokens_out = provider.extract("page text", make_source())

    assert page.scholarships[0].name == "KAUST Fellowship"
    assert (tokens_in, tokens_out) == (1000, 250)  # thinking tokens count as output
    call = models.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    assert "page text" in call["contents"]
    config = call["config"]
    assert config.temperature == 0
    assert config.response_mime_type == "application/json"
    assert "scholarships" in config.response_json_schema["properties"]
    assert "Never use outside knowledge" in config.system_instruction


def test_gemini_retries_429(llm_settings: Settings) -> None:
    rate_limited = errors.APIError(429, {"error": {"code": 429, "message": "quota",
                                                  "status": "RESOURCE_EXHAUSTED"}})  # fmt: skip
    provider, models = _gemini(llm_settings, rate_limited, _gemini_response(GOOD_JSON))

    provider.extract("page text", make_source())

    assert len(models.calls) == 2


def test_gemini_400_is_provider_error(llm_settings: Settings) -> None:
    bad = errors.APIError(400, {"error": {"code": 400, "message": "API key not valid"}})
    provider, models = _gemini(llm_settings, bad)

    with pytest.raises(ProviderError, match="400"):
        provider.extract("page text", make_source())
    assert len(models.calls) == 1


# ---------- Groq (fake client) ----------


class FakeGroqCompletions:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _groq(settings: Settings, *outcomes: Any) -> tuple[GroqProvider, FakeGroqCompletions]:
    completions = FakeGroqCompletions(*outcomes)
    client = cast(groq.Groq, SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    return GroqProvider(settings, client=client, sleep=Sleeps()), completions


def _groq_response(text: str) -> Any:
    message = SimpleNamespace(content=text)
    usage = SimpleNamespace(prompt_tokens=900, completion_tokens=150)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_groq_request_and_tokens(llm_settings: Settings) -> None:
    provider, completions = _groq(llm_settings, _groq_response(GOOD_JSON))

    _, tokens_in, tokens_out = provider.extract("page text", make_source())

    assert (tokens_in, tokens_out) == (900, 150)
    call = completions.calls[0]
    assert call["model"] == "openai/gpt-oss-120b"
    assert call["temperature"] == 0
    assert call["response_format"]["type"] == "json_schema"
    assert [m["role"] for m in call["messages"]] == ["system", "user"]


def test_groq_retries_rate_limit(llm_settings: Settings) -> None:
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com"))
    rate_limited = groq.RateLimitError("rate limited", response=response, body=None)
    provider, completions = _groq(llm_settings, rate_limited, _groq_response(GOOD_JSON))

    provider.extract("page text", make_source())

    assert len(completions.calls) == 2
