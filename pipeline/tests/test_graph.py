"""Tests for pipeline.graph: the whole per-source pipeline with fake HTTP, LLM and store."""

from datetime import date
from typing import Any

import pytest
import respx

from pipeline.config import Settings
from pipeline.graph import run_sources
from pipeline.llm.provider import InvalidOutput, ProviderError
from pipeline.models import Change, FetchResult, FetchStatus
from pipeline.nodes.validate import ValidatedScholarship
from pipeline.schemas import DeadlineItem, PageExtraction, ScholarshipExtraction
from pipeline.tests.conftest import FakeProvider, make_source
from pipeline.text import content_hash, html_to_text, normalize

TODAY = date(2026, 11, 15)
PAGE_HTML = (
    "<html><body><h1>MS Admissions</h1><p>Applications for the MS programme are open."
    " The deadline is 15 January 2027.</p></body></html>"
)


class FakeStore:
    """Remembers what the graph asked to save."""

    def __init__(self, fail_for: str | None = None) -> None:
        self.recorded: list[FetchResult] = []
        self.saved: dict[str, tuple[list[ValidatedScholarship], str, bool]] = {}
        self.fail_for = fail_for

    def record_fetch(self, result: FetchResult) -> None:
        self.recorded.append(result)

    def save(
        self,
        result: FetchResult,
        items: list[ValidatedScholarship],
        extraction_version: str,
        mark_extracted: bool,
    ) -> int:
        if result.source.key == self.fail_for:
            raise RuntimeError("database is down")
        self.saved[result.source.key] = (items, extraction_version, mark_extracted)
        return len(items)


def page(evidence: str = "The deadline is 15 January 2027.") -> PageExtraction:
    return PageExtraction(
        scholarships=[
            ScholarshipExtraction(
                name="MS Admission",
                opportunity_type="admission",
                location="pakistan",
                status="open",
                deadlines=[DeadlineItem(label="Application deadline",
                                        deadline_date=date(2027, 1, 15), evidence=evidence)],
            )  # fmt: skip
        ]
    )


@pytest.fixture
def site(mock_http: respx.MockRouter) -> respx.MockRouter:
    mock_http.get("https://example.edu.pk/robots.txt").respond(404)
    mock_http.get("https://example.edu.pk/good").respond(200, html=PAGE_HTML)
    mock_http.get("https://example.edu.pk/other").respond(200, html=PAGE_HTML)
    mock_http.get("https://example.edu.pk/broken").respond(404)
    return mock_http


async def test_new_page_is_extracted_validated_and_saved(
    site: respx.MockRouter, settings: Settings
) -> None:
    store, provider = FakeStore(), FakeProvider(page())

    [outcome] = await run_sources([make_source(key="good", url="https://example.edu.pk/good")],
                                  settings, provider, store, {}, today=TODAY)  # fmt: skip

    assert outcome.error is None
    assert outcome.fetched.change is Change.NEW
    assert outcome.saved == 1
    items, version, mark_extracted = store.saved["good"]
    assert version == "extract_v2+fake-model"
    assert mark_extracted is True
    assert not items[0].needs_review
    assert items[0].evidence[0].score == 100
    assert (outcome.extracted and outcome.extracted.tokens_in) == 100


async def test_unchanged_page_ends_without_llm(site: respx.MockRouter, settings: Settings) -> None:
    good = make_source(key="good", url="https://example.edu.pk/good")
    old_hash = content_hash(normalize(html_to_text(PAGE_HTML)))
    store, provider = FakeStore(), FakeProvider()

    [outcome] = await run_sources([good], settings, provider, store, {"good": old_hash})

    assert outcome.fetched.change is Change.UNCHANGED
    assert outcome.extracted is None
    assert provider.calls == []
    assert [r.source.key for r in store.recorded] == ["good"]


async def test_force_extracts_unchanged_page(site: respx.MockRouter, settings: Settings) -> None:
    good = make_source(key="good", url="https://example.edu.pk/good")
    old_hash = content_hash(normalize(html_to_text(PAGE_HTML)))
    store = FakeStore()

    [outcome] = await run_sources(
        [good], settings, FakeProvider(page()), store, {"good": old_hash}, force=True, today=TODAY
    )

    assert outcome.fetched.change is Change.UNCHANGED
    assert outcome.saved == 1
    assert "good" in store.saved


async def test_invalid_json_goes_back_to_extract_once(
    site: respx.MockRouter, settings: Settings
) -> None:
    provider = FakeProvider(InvalidOutput("status: bad value", 50, 5), page())
    store = FakeStore()

    [outcome] = await run_sources([make_source(key="good", url="https://example.edu.pk/good")],
                                  settings, provider, store, {}, today=TODAY)  # fmt: skip

    assert [feedback for _, feedback in provider.calls] == [None, "status: bad value"]
    assert outcome.saved == 1
    assert store.saved["good"][2] is True  # retry worked: the page counts as extracted
    assert outcome.extracted is not None
    assert (outcome.extracted.tokens_in, outcome.extracted.tokens_out) == (150, 25)


async def test_invalid_json_twice_saves_nothing_and_retries_next_week(
    site: respx.MockRouter, settings: Settings
) -> None:
    provider = FakeProvider(InvalidOutput("bad", 1, 1), InvalidOutput("still bad", 1, 1))
    store = FakeStore()

    [outcome] = await run_sources([make_source(key="good", url="https://example.edu.pk/good")],
                                  settings, provider, store, {}, today=TODAY)  # fmt: skip

    assert len(provider.calls) == 2  # no third try
    items, _, mark_extracted = store.saved["good"]
    assert items == []
    assert mark_extracted is False
    assert outcome.extracted is not None
    assert outcome.extracted.errors == ["invalid JSON twice: still bad"]


async def test_invented_quote_is_flagged(site: respx.MockRouter, settings: Settings) -> None:
    provider = FakeProvider(page(evidence="Deadline: 15 January 2027, no exceptions."))
    store = FakeStore()

    await run_sources([make_source(key="good", url="https://example.edu.pk/good")],
                      settings, provider, store, {}, today=TODAY)  # fmt: skip

    [item] = store.saved["good"][0]
    assert item.needs_review
    assert item.item.deadlines == []


async def test_llm_failure_flags_items_and_forgets_hash(
    site: respx.MockRouter, settings: Settings
) -> None:
    store = FakeStore()

    await run_sources([make_source(key="good", url="https://example.edu.pk/good")],
                      settings, FakeProvider(ProviderError("quota")), store, {})  # fmt: skip

    assert store.saved["good"] == ([], "extract_v2+none", False)


async def test_one_broken_source_never_stops_the_others(
    site: respx.MockRouter, settings: Settings
) -> None:
    sources = [make_source(key=k, url=f"https://example.edu.pk/{k}")
               for k in ("good", "broken", "other")]  # fmt: skip
    store = FakeStore(fail_for="other")
    provider = FakeProvider(page(), page())

    outcomes = await run_sources(sources, settings, provider, store, {}, today=TODAY)

    by_key = {o.source.key: o for o in outcomes}
    assert [o.source.key for o in outcomes] == ["good", "broken", "other"]
    assert by_key["good"].saved == 1
    assert by_key["broken"].fetched.status is FetchStatus.FAILED  # HTTP 404
    assert by_key["broken"].extracted is None
    assert by_key["other"].error == "RuntimeError: database is down"
    assert by_key["other"].fetched.status is FetchStatus.FAILED
    assert [r.source.key for r in store.recorded] == ["broken"]


async def test_contact_email_is_required(settings: Settings) -> None:
    no_email = settings.model_copy(update={"contact_email": ""})
    with pytest.raises(ValueError, match="CONTACT_EMAIL"):
        await run_sources([], no_email, FakeProvider(), FakeStore(), {})


async def test_llm_calls_run_one_at_a_time(site: respx.MockRouter, settings: Settings) -> None:
    """Fetching may be parallel, but two LLM calls must never run at the same time."""
    import threading
    import time

    class SlowProvider:
        name = last_model = "slow"

        def __init__(self) -> None:
            self.running = 0
            self.most_at_once = 0
            self._lock = threading.Lock()

        def extract(self, page_text: str, source: object, feedback: str | None = None) -> Any:
            with self._lock:
                self.running += 1
                self.most_at_once = max(self.most_at_once, self.running)
            time.sleep(0.05)
            with self._lock:
                self.running -= 1
            return page(), 1, 1

    provider = SlowProvider()
    sources = [make_source(key="good"),
               make_source(key="other", url="https://example.edu.pk/other")]  # fmt: skip
    await run_sources(sources, settings, provider, FakeStore(), {}, today=TODAY)

    assert settings.max_concurrency > 1
    assert provider.most_at_once == 1
