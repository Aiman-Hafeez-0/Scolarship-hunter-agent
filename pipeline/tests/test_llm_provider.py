"""Tests for pipeline.llm. Real SDK clients are replaced by small fakes: no API calls."""

from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import groq
import httpx
import pytest
from google import genai
from google.genai import errors

from pipeline.config import Settings
from pipeline.llm.gemini_provider import GeminiProvider, gemini_retry_after, is_daily_quota
from pipeline.llm.groq_provider import GroqProvider, groq_error_text
from pipeline.llm.provider import (
    BaseProvider,
    FallbackProvider,
    InvalidOutput,
    Pacer,
    ProviderConfigError,
    ProviderError,
    TokenWindow,
    TooLarge,
    TransientError,
    build_provider,
    is_cut_off,
    parse_output,
    parse_seconds,
    parse_try_again,
    retry_delay,
    split_in_half,
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


def test_parse_output_drops_unknown_empty_keys() -> None:
    """gpt-oss sometimes adds "status_evidence": null, a key the schema does not have."""
    extra = '"location": "abroad", "status_evidence": null'
    answer = GOOD_JSON.replace('"location": "abroad"', extra)
    assert parse_output(answer, 1, 2).scholarships[0].name == "KAUST Fellowship"

    with_value = GOOD_JSON.replace('"location": "abroad"', '"location": "abroad", "x": "Pakistan"')
    with pytest.raises(InvalidOutput, match="Extra inputs"):
        parse_output(with_value, 1, 2)  # an unknown key that carries a value is still wrong


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


# ---------- Free-tier limits: token window, splitting, Gemini daily quota ----------


class FakeClock:
    """A clock that moves forward only when something sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _groq_with_clock(
    settings: Settings, *outcomes: Any
) -> tuple[GroqProvider, FakeGroqCompletions, FakeClock]:
    completions = FakeGroqCompletions(*outcomes)
    client = cast(groq.Groq, SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    clock = FakeClock()
    provider = GroqProvider(settings, client=client, sleep=clock.sleep, clock=clock)
    return provider, completions, clock


def _groq_error(cls: Any, status: int, error: dict[str, Any]) -> Any:
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.groq.com"))
    return cls(error.get("message", "error"), response=response, body={"error": error})


def _long_text(chars: int) -> str:
    line = "Applications for the MS programme close on 15 January 2027 for everyone.\n"
    return line * (chars // len(line))


def test_token_window_waits_until_the_request_fits() -> None:
    clock = FakeClock()
    window = TokenWindow(100, clock=clock, sleep=clock.sleep)

    window.reserve(60)
    clock.now += 10
    window.reserve(60)  # 120 > 100: wait until the first 60 are older than a minute

    assert clock.sleeps == [pytest.approx(50.1)]
    with pytest.raises(TooLarge):
        window.reserve(101)  # can never fit


def test_groq_sets_answer_budget_and_low_reasoning(llm_settings: Settings) -> None:
    provider, completions, _ = _groq_with_clock(llm_settings, _groq_response(GOOD_JSON))

    provider.extract("Deadline: 15 January 2027", make_source())

    call = completions.calls[0]
    assert call["reasoning_effort"] == "low"
    system, user = (m["content"] for m in call["messages"])
    # The answer gets what the prompt leaves of the per-minute budget, at most 4000.
    assert call["max_completion_tokens"] == provider.output_budget(system, user)[1]
    assert 3500 < call["max_completion_tokens"] <= 4000
    assert provider.budget == 7200  # 8000 tokens per minute minus a 10% margin


def test_groq_waits_for_the_per_minute_token_budget(llm_settings: Settings) -> None:
    heavy = _groq_response(GOOD_JSON)
    heavy.usage = SimpleNamespace(prompt_tokens=3000, completion_tokens=2000)
    provider, completions, clock = _groq_with_clock(llm_settings, heavy, _groq_response(GOOD_JSON))

    provider.extract("page one", make_source())
    provider.extract("page two", make_source())  # 5000 used + a new request > 7200

    assert len(completions.calls) == 2
    assert clock.sleeps and clock.sleeps[0] > 50  # waited for the first request to age out


def test_groq_text_too_large_for_the_budget_is_split_in_half(llm_settings: Settings) -> None:
    text = _long_text(20_000)  # about 6,700 estimated tokens: no room left for the answer
    provider, completions, _ = _groq_with_clock(llm_settings, *[_groq_response(GOOD_JSON)] * 4)

    page, tokens_in, _ = provider.extract(text, make_source())

    sent = [call["messages"][1]["content"] for call in completions.calls]
    assert len(sent) >= 2
    assert all(len(s) < len(text) for s in sent)
    assert len(page.scholarships) == len(sent)  # results of the halves are combined
    assert tokens_in == 900 * len(sent)


def test_groq_413_splits_the_text(llm_settings: Settings) -> None:
    too_large = _groq_error(groq.APIStatusError, 413, {"message": "Request too large"})
    provider, completions, _ = _groq_with_clock(
        llm_settings, too_large, _groq_response(GOOD_JSON), _groq_response(GOOD_JSON)
    )

    page, _, _ = provider.extract(_long_text(4000), make_source())

    assert len(completions.calls) == 3
    assert len(page.scholarships) == 2


def test_groq_answer_out_of_tokens_splits_the_text(llm_settings: Settings) -> None:
    """The polimi-merit error: HTTP 400 json_validate_failed, max completion tokens reached."""
    out_of_tokens = _groq_error(groq.BadRequestError, 400, {
        "message": "Failed to generate JSON. Please adjust your prompt.",
        "type": "invalid_request_error", "code": "json_validate_failed",
        "failed_generation": "max completion tokens reached before generating a valid document",
    })  # fmt: skip
    provider, completions, _ = _groq_with_clock(
        llm_settings, out_of_tokens, _groq_response(GOOD_JSON), _groq_response(GOOD_JSON)
    )

    provider.extract(_long_text(4000), make_source())

    assert len(completions.calls) == 3


def test_groq_cut_off_answer_splits_the_text(llm_settings: Settings) -> None:
    cut = _groq_response(GOOD_JSON)
    cut.choices[0].finish_reason = "length"
    provider, completions, _ = _groq_with_clock(
        llm_settings, cut, _groq_response(GOOD_JSON), _groq_response(GOOD_JSON)
    )
    provider.extract(_long_text(4000), make_source())
    assert len(completions.calls) == 3


def test_groq_other_invalid_json_is_invalid_output(llm_settings: Settings) -> None:
    bad_json = _groq_error(groq.BadRequestError, 400, {
        "message": "Failed to generate JSON.", "code": "json_validate_failed",
        "failed_generation": '{"scholarships": [{"name": 5}]}',
    })  # fmt: skip
    provider, _, _ = _groq_with_clock(llm_settings, bad_json)

    with pytest.raises(InvalidOutput, match="json_validate_failed"):
        provider.extract("page", make_source())


def test_groq_429_waits_try_again_time_up_to_five_tries(llm_settings: Settings) -> None:
    message = ("Rate limit reached for model `openai/gpt-oss-120b` on tokens per minute (TPM): "
               "Limit 8000, Used 4177, Requested 7807. Please try again in 7.5s.")  # fmt: skip
    limited = _groq_error(groq.RateLimitError, 429, {"message": message, "code": "rate_limit"})
    provider, completions, clock = _groq_with_clock(
        llm_settings, *[limited] * 4, _groq_response(GOOD_JSON)
    )

    provider.extract("page", make_source())

    assert len(completions.calls) == 5
    assert clock.sleeps == [7.5] * 4

    provider, completions, _ = _groq_with_clock(llm_settings, *[limited] * 6)
    with pytest.raises(ProviderError, match="try again in 7.5s"):
        provider.extract("page", make_source())
    assert len(completions.calls) == 5


def test_groq_error_text_shows_the_message() -> None:
    error = _groq_error(groq.BadRequestError, 400, {
        "message": "Failed to generate JSON.", "code": "json_validate_failed",
        "failed_generation": "max completion tokens reached"})  # fmt: skip
    assert groq_error_text(error) == (
        "HTTP 400 json_validate_failed: Failed to generate JSON. (max completion tokens reached)"
    )


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("Please try again in 29.88s. Need more tokens?", 29.88),
        ("Please try again in 1m2.5s.", 62.5),
        ("Please try again in 2m.", 120.0),
        ("Please try again in 1h2m3s.", 3723.0),
        ("Please try again in 45ms. Need more tokens?", 0.045),  # not 45 minutes!
        ("Please try again later.", None),
    ],
)
def test_parse_try_again(message: str, seconds: float | None) -> None:
    assert parse_try_again(message) == seconds


def test_split_in_half_cuts_at_a_line_break() -> None:
    first, second = split_in_half("aaaa\nbbbb\ncccc\ndddd")
    assert (first, second) == ("aaaa\nbbbb", "cccc\ndddd")
    assert split_in_half("x" * 10) == ("x" * 5, "x" * 5)


DAILY_QUOTA = errors.APIError(429, {"error": {
    "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota.",
    "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}],
}})  # fmt: skip


def test_gemini_daily_quota_stops_gemini_for_the_run(
    llm_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    gemini, models = _gemini(llm_settings, DAILY_QUOTA, _gemini_response(GOOD_JSON))
    groq_provider, completions, _ = _groq_with_clock(
        llm_settings, _groq_response(GOOD_JSON), _groq_response(GOOD_JSON)
    )
    provider = FallbackProvider(gemini, groq_provider)

    provider.extract("page one", make_source())
    provider.extract("page two", make_source())

    assert len(models.calls) == 1  # no retry, and never called again after the quota error
    assert not gemini.available
    assert len(completions.calls) == 2  # both pages went to Groq
    quota_logs = [r for r in caplog.records if "daily request quota" in r.getMessage()]
    assert len(quota_logs) == 1
    assert not any("falling back" in r.getMessage() for r in caplog.records)


def test_gemini_per_minute_429_is_not_the_daily_quota() -> None:
    per_minute = errors.APIError(429, {"error": {"code": 429, "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}})  # fmt: skip
    assert not is_daily_quota(per_minute)
    assert is_daily_quota(DAILY_QUOTA)


# ---------- Cut-off answers and today's date ----------


@pytest.mark.parametrize(
    ("text", "cut"),
    [(GOOD_JSON, False), (GOOD_JSON[:-10], True), ('{"scholarships": [', True), ("", False),
     ('{"scholarships": [{"name": 5,, "x": 1}]}', False)],  # broken in the middle: not cut off
)  # fmt: skip
def test_is_cut_off(text: str, cut: bool) -> None:
    assert is_cut_off(text) is cut


def test_cut_off_json_is_split_in_half_not_failed(llm_settings: Settings) -> None:
    provider = ScriptedProvider(llm_settings, Sleeps(), (GOOD_JSON[:-10], 500, 4000),
                                (GOOD_JSON, 300, 50), (GOOD_JSON, 300, 50))  # fmt: skip

    page, tokens_in, tokens_out = provider.extract(_long_text(4000), make_source())

    assert len(page.scholarships) == 2  # one from each half
    assert (tokens_in, tokens_out) == (1100, 4100)  # the cut-off answer's tokens still count


def test_short_cut_off_text_is_a_provider_error(llm_settings: Settings) -> None:
    provider = ScriptedProvider(llm_settings, Sleeps(), (GOOD_JSON[:-10], 500, 4000))
    with pytest.raises(ProviderError, match="cut off"):
        provider.extract("short page", make_source())


def test_gemini_max_tokens_splits_the_text(llm_settings: Settings) -> None:
    cut = _gemini_response(GOOD_JSON[:-10])
    cut.candidates = [SimpleNamespace(finish_reason=genai.types.FinishReason.MAX_TOKENS)]
    provider, models = _gemini(llm_settings, cut, _gemini_response(GOOD_JSON),
                               _gemini_response(GOOD_JSON))  # fmt: skip

    page, _, _ = provider.extract(_long_text(4000), make_source())

    assert len(models.calls) == 3
    assert len(page.scholarships) == 2


def test_today_goes_into_the_prompt_and_through_the_fallback(llm_settings: Settings) -> None:
    provider, models = _gemini(llm_settings, _gemini_response(GOOD_JSON))
    fallback = FallbackProvider(provider, FakeProvider())

    fallback.extract("page", make_source(), today=date(2026, 11, 14))

    system = models.calls[0]["config"].system_instruction
    assert "Today's date: 2026-11-14" in system
    assert "more than 30 days before today's date" in system
