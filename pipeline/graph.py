"""The weekly pipeline for one source, as a LangGraph StateGraph.

    fetch -> detect_change -> extract -> validate -> save
                  |                        |
                  +-> (unchanged) END      +-> (invalid JSON, first time) back to extract

- A page is "unchanged" when it matches the last page that was extracted and saved without
  errors (sources.last_extracted_hash). Such a page ends the run for that source, unless
  `force` is set. A page only fetched by `fetch`, or whose extraction failed, is extracted.
- A page that could not be fetched (failed, blocked by robots.txt, needs a browser) ends too.
- run_sources runs the graph for many sources, at most `max_concurrency` at a time.
  One broken source never stops the others: its error is recorded in its outcome.

Blocking work (LLM calls, database writes) runs in a worker thread so fetching continues
meanwhile. LLM calls take turns (one lock), because providers pace their requests.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal, Protocol, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from pipeline.config import Settings
from pipeline.llm.provider import LLMProvider
from pipeline.models import Change, FetchResult, FetchStatus, Source
from pipeline.nodes.detect_change import detect_changes
from pipeline.nodes.extract import ExtractResult, extract_first, extract_retry
from pipeline.nodes.fetch import fetch_source, make_client
from pipeline.nodes.validate import ValidatedScholarship, validate_extraction
from pipeline.prompts import EXTRACT_VERSION
from pipeline.ratelimit import DomainRateLimiter
from pipeline.robots import RobotsChecker

logger = logging.getLogger(__name__)

MAX_EXTRACT_ATTEMPTS = 2  # the first try plus one retry for invalid JSON


class Store(Protocol):
    """Where results are saved (Postgres in real runs, a fake in tests)."""

    def record_fetch(self, result: FetchResult) -> None: ...

    def save(
        self,
        result: FetchResult,
        items: list[ValidatedScholarship],
        extraction_version: str,
        mark_extracted: bool,
    ) -> int: ...


class SourceState(TypedDict, total=False):
    """What the graph knows about one source as it moves through the steps."""

    source: Source
    fetched: FetchResult
    extracted: ExtractResult
    attempts: int
    validated: list[ValidatedScholarship]
    saved: int


@dataclass
class Deps:
    """Everything the steps need that is shared by all sources in a run."""

    settings: Settings
    client: httpx.AsyncClient
    provider: LLMProvider
    store: Store
    extracted_hashes: Mapping[str, str]  # source key -> fingerprint of last extracted page
    force: bool
    today: date
    limiter: DomainRateLimiter
    robots: RobotsChecker
    llm_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class SourceOutcome:
    """What happened to one source, for the summary."""

    source: Source
    fetched: FetchResult
    extracted: ExtractResult | None = None
    items: list[ValidatedScholarship] = field(default_factory=list)
    saved: int = 0
    error: str | None = None


def should_extract(result: FetchResult, force: bool) -> bool:
    if result.status is not FetchStatus.OK or result.text is None:
        return False
    return force or result.change is not Change.UNCHANGED


def build_graph(deps: Deps) -> Any:
    """Wire the steps together. Returns the compiled graph (call .ainvoke on it)."""

    async def fetch(state: SourceState) -> SourceState:
        result = await fetch_source(
            state["source"], deps.client, deps.limiter, deps.robots, deps.settings
        )
        return {"fetched": result}

    async def detect_change(state: SourceState) -> SourceState:
        [result] = detect_changes([state["fetched"]], deps.extracted_hashes)
        if not should_extract(result, deps.force):
            await asyncio.to_thread(deps.store.record_fetch, result)
        return {"fetched": result}

    def after_detect(state: SourceState) -> str:
        return "extract" if should_extract(state["fetched"], deps.force) else END

    async def extract(state: SourceState) -> SourceState:
        text = state["fetched"].text
        assert text is not None
        previous = state.get("extracted")
        async with deps.llm_lock:
            if previous is not None and previous.invalid_parts:
                await asyncio.to_thread(extract_retry, text, previous, deps.provider)
                result = previous
            else:
                result = await asyncio.to_thread(
                    extract_first, text, state["source"], deps.provider, deps.today
                )
        return {"extracted": result, "attempts": state.get("attempts", 0) + 1}

    async def validate(state: SourceState) -> SourceState:
        extracted = state["extracted"]
        text = state["fetched"].text or ""
        location = state["source"].location.value
        items = validate_extraction(extracted.extraction, text, deps.today, location)
        for item in items:  # the page was only partly read, so every item may miss facts
            for problem in extracted.errors:
                item.flag(f"extraction incomplete: {problem}")
        return {"validated": items}

    def after_validate(state: SourceState) -> Literal["extract", "save"]:
        if state["extracted"].invalid_parts and state["attempts"] < MAX_EXTRACT_ATTEMPTS:
            return "extract"
        return "save"

    async def save(state: SourceState) -> SourceState:
        extracted = state["extracted"]
        # Only a complete extraction marks the page as extracted. If the LLM failed on part
        # of the page, it is extracted again next run. What was found is still saved.
        mark_extracted = not extracted.errors and not extracted.invalid_parts
        version = f"{EXTRACT_VERSION}+{extracted.model or 'none'}"
        saved = await asyncio.to_thread(
            deps.store.save, state["fetched"], state["validated"], version, mark_extracted
        )
        return {"saved": saved}

    graph = StateGraph(SourceState)
    graph.add_node("fetch", fetch)
    graph.add_node("detect_change", detect_change)
    graph.add_node("extract", extract)
    graph.add_node("validate", validate)
    graph.add_node("save", save)
    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "detect_change")
    graph.add_conditional_edges("detect_change", after_detect)
    graph.add_edge("extract", "validate")
    graph.add_conditional_edges("validate", after_validate)
    graph.add_edge("save", END)
    return graph.compile()


async def run_source(graph: Any, source: Source) -> SourceOutcome:
    """Run the graph for one source. Never raises: errors end up in the outcome."""
    state: SourceState = {"source": source}
    try:
        state = await graph.ainvoke(state)
    except Exception as exc:  # one broken source must never stop the others
        logger.exception("source %s failed", source.key)
        error = f"{type(exc).__name__}: {exc}"
        fetched = state.get("fetched") or FetchResult(source, FetchStatus.FAILED)
        return SourceOutcome(source, replace(fetched, status=FetchStatus.FAILED, error=error),
                             error=error)  # fmt: skip
    return SourceOutcome(
        source=source,
        fetched=state["fetched"],
        extracted=state.get("extracted"),
        items=state.get("validated", []),
        saved=state.get("saved", 0),
    )


async def run_sources(
    sources: list[Source],
    settings: Settings,
    provider: LLMProvider,
    store: Store,
    extracted_hashes: Mapping[str, str],
    force: bool = False,
    today: date | None = None,
) -> list[SourceOutcome]:
    """Run the graph for every source, at most settings.max_concurrency at a time."""
    if not settings.contact_email:
        raise ValueError("Set CONTACT_EMAIL in .env: it goes in the User-Agent we send.")

    semaphore = asyncio.Semaphore(settings.max_concurrency)
    async with make_client(settings) as client:
        limiter = DomainRateLimiter(settings.per_domain_delay_s)
        deps = Deps(
            settings=settings,
            client=client,
            provider=provider,
            store=store,
            extracted_hashes=extracted_hashes,
            force=force,
            today=today or date.today(),
            limiter=limiter,
            robots=RobotsChecker(client, limiter),
        )
        graph = build_graph(deps)

        async def one(source: Source) -> SourceOutcome:
            async with semaphore:
                return await run_source(graph, source)

        return list(await asyncio.gather(*(one(source) for source in sources)))
