"""Tests for pipeline.llm. Real SDK clients are replaced by small fakes: no API calls."""

from types import SimpleNamespace
from typing import Any, cast

import groq
import httpx
import pytest
from google import genai
from google.genai import errors

from pipeline.config import Settings
from pipeline.llm.gemini_provider import GeminiProvider, gemini_retry_after
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
    parse_seconds,
    retry_delay,
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
        llm_max_tries=2,
        llm_max_retry_wait_s=60,
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


def rate_limited(retry_after: float | None = None) -> TransientError:
    return TransientError("429", status=429, retry_after=retry_after)


def test_429_waits_the_retry_after_time_then_succeeds(llm_settings: Settings) -> None:
    sleeps = Sleeps()
    provider = ScriptedProvider(llm_settings, sleeps, rate_limited(17), (GOOD_JSON, 50, 7))

    page, tokens_in, tokens_out = provider.extract("page", make_source())

    assert page.scholarships[0].name == "KAUST Fellowship"
    assert (tokens_in, tokens_out) == (50, 7)
    assert sleeps.calls == [17]


def test_429_without_retry_after_uses_backoff(llm_settings: Settings) -> None:
    sleeps = Sleeps()
    provider = ScriptedProvider(llm_settings, sleeps, rate_limited(), (GOOD_JSON, 1, 1))
    provider.extract("page", make_source())
    assert sleeps.calls == [10]


def test_429_gives_up_after_two_tries(llm_settings: Settings) -> None:
    provider = ScriptedProvider(llm_settings, Sleeps(), rate_limited(5), rate_limited(5),
                                (GOOD_JSON, 1, 1))  # fmt: skip
    with pytest.raises(ProviderError, match="429"):
        provider.extract("page", make_source())
    assert len(provider.outcomes) == 1  # the third outcome was never asked for


def test_429_with_long_retry_after_gives_up_at_once(llm_settings: Settings) -> None:
    sleeps = Sleeps()
    provider = ScriptedProvider(llm_settings, sleeps, rate_limited(3600), (GOOD_JSON, 1, 1))
    with pytest.raises(ProviderError):
        provider.extract("page", make_source())
    assert sleeps.calls == []


@pytest.mark.parametrize("status", [503, 504])
def test_unavailable_is_retried_once(llm_settings: Settings, status: int) -> None:
    sleeps = Sleeps()
    busy = TransientError(f"HTTP {status}", status=status)
    provider = ScriptedProvider(llm_settings, sleeps, busy, busy, (GOOD_JSON, 1, 1))
    with pytest.raises(ProviderError, match=str(status)):
        provider.extract("page", make_source())
    assert sleeps.calls == [10]  # one retry, then give up


def test_timeout_is_not_retried(llm_settings: Settings) -> None:
    sleeps = Sleeps()
    slow = TransientError("no answer within 60 s", timed_out=True)
    provider = ScriptedProvider(llm_settings, sleeps, slow, (GOOD_JSON, 1, 1))
    with pytest.raises(ProviderError, match="60 s"):
        provider.extract("page", make_source())
    assert sleeps.calls == []


def test_non_transient_error_is_not_retried(llm_settings: Settings) -> None:
    provider = ScriptedProvider(llm_settings, Sleeps(), ProviderError("bad key"), GOOD_JSON)
    with pytest.raises(ProviderError, match="bad key"):
        provider.extract("page", make_source())
    assert len(provider.outcomes) == 1  # second outcome never used


def test_retry_delay(llm_settings: Settings) -> None:
    assert retry_delay(rate_limited(30), llm_settings) == 30
    assert retry_delay(rate_limited(61), llm_settings) is None
    assert retry_delay(rate_limited(), llm_settings) == 10
    assert retry_delay(TransientError("503", status=503), llm_settings) == 10
    assert retry_delay(TransientError("reset"), llm_settings) == 10
    assert retry_delay(TransientError("slow", timed_out=True), llm_settings) is None


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("17", 17.0), ("17s", 17.0), ("1.5s", 1.5), (20, 20.0), (None, None), ("soon", None)],
)
def test_parse_seconds(value: object, seconds: float | None) -> None:
    assert parse_seconds(value) == seconds


def test_default_llm_settings_fail_fast() -> None:
    settings = Settings(_env_file=None)
    assert settings.gemini_model == "gemini-3.8-flash"
    assert settings.llm_timeout_s == 60
    assert settings.llm_max_tries == 2


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


def test_gemini_503_twice_switches_to_groq_for_that_page(llm_settings: Settings) -> None:
    busy = TransientError("HTTP 503", status=503)
    gemini = ScriptedProvider(llm_settings, Sleeps(), busy, busy)
    groq_like = ScriptedProvider(llm_settings, Sleeps(), (GOOD_JSON, 5, 5))
    groq_like.name = "groq"

    provider = FallbackProvider(gemini, groq_like)
    page, _, _ = provider.extract("text", make_source())

    assert page.scholarships[0].name == "KAUST Fellowship"
    assert provider.last_model == "groq"
    assert gemini.outcomes == []  # Gemini was tried exactly twice


# ---------- build_provider ----------


def test_build_provider_both_keys(llm_settings: Settings) -> None:
    provider = build_provider(llm_settings)
    assert isinstance(provider, FallbackProvider)
    assert provider.name == "gemini-3.8-flash -> openai/gpt-oss-120b"


def test_build_provider_one_key(llm_settings: Settings) -> None:
    only_groq = llm_settings.model_copy(update={"gemini_api_key": None})
    assert isinstance(build_provider(only_groq), GroqProvider)


@pytest.mark.parametrize(("choice", "cls"), [("gemini", GeminiProvider), ("groq", GroqProvider)])
def test_build_provider_choice(llm_settings: Settings, choice: Any, cls: type) -> None:
    assert isinstance(build_provider(llm_settings, choice), cls)


@pytest.mark.parametrize(
    ("choice", "key"), [("gemini", "gemini_api_key"), ("groq", "groq_api_key")]
)
def test_build_provider_choice_needs_its_key(
    llm_settings: Settings, choice: Any, key: str
) -> None:
    without = llm_settings.model_copy(update={key: None})
    with pytest.raises(ProviderConfigError, match=key.upper()):
        build_provider(without, choice)


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


def test_gemini_retry_after_from_error_body() -> None:
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure"},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"},
    ]}}  # fmt: skip
    assert gemini_retry_after(errors.APIError(429, body)) == 17
    assert gemini_retry_after(errors.APIError(429, {"error": {"code": 429}})) is None


def test_gemini_429_waits_retry_after(llm_settings: Settings) -> None:
    body = {"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}}  # fmt: skip
    sleeps = Sleeps()
    models = FakeGeminiModels(errors.APIError(429, body), _gemini_response(GOOD_JSON))
    client = cast(genai.Client, SimpleNamespace(models=models))
    GeminiProvider(llm_settings, client=client, sleep=sleeps).extract("page", make_source())
    assert sleeps.calls == [7]


def test_gemini_timeout_is_not_retried(llm_settings: Settings) -> None:
    provider, models = _gemini(llm_settings, httpx.ReadTimeout("slow"), _gemini_response("{}"))
    with pytest.raises(ProviderError, match="60 s"):
        provider.extract("page", make_source())
    assert len(models.calls) == 1


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


def test_groq_429_reads_retry_after_header(llm_settings: Settings) -> None:
    response = httpx.Response(429, headers={"retry-after": "3"},
                              request=httpx.Request("POST", "https://api.groq.com"))  # fmt: skip
    rate_limited_error = groq.RateLimitError("rate limited", response=response, body=None)
    sleeps = Sleeps()
    completions = FakeGroqCompletions(rate_limited_error, _groq_response(GOOD_JSON))
    client = cast(groq.Groq, SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    GroqProvider(llm_settings, client=client, sleep=sleeps).extract("page", make_source())
    assert sleeps.calls == [3]


def test_groq_timeout_is_not_retried(llm_settings: Settings) -> None:
    timeout = groq.APITimeoutError(request=httpx.Request("POST", "https://api.groq.com"))
    provider, completions = _groq(llm_settings, timeout, _groq_response(GOOD_JSON))
    with pytest.raises(ProviderError, match="60 s"):
        provider.extract("page", make_source())
    assert len(completions.calls) == 1


def test_groq_client_uses_60_second_timeout(llm_settings: Settings) -> None:
    provider = GroqProvider(llm_settings)
    assert provider._client.timeout == 60
