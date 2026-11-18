"""Shared test helpers: fast settings, a sample source, a respx router and a test database."""

import os
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import psycopg
import pytest
import respx
from psycopg import sql

from pipeline.config import Settings
from pipeline.models import FetchResult, FetchStatus, Source
from pipeline.schemas import PageExtraction
from pipeline.text import content_hash


@pytest.fixture
def test_conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """A connection whose search_path points at a fresh schema, dropped afterwards.

    Skipped unless TEST_DATABASE_URL is set (CI sets it).
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")

    schema = f"pipeline_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(url, autocommit=True, options=f"-c search_path={schema}") as conn:
            yield conn
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


@pytest.fixture
def settings() -> Settings:
    """Settings with every delay set to 0 so tests run instantly."""
    return Settings(
        _env_file=None,
        contact_email="test@example.com",
        per_domain_delay_s=0,
        retry_backoff_s=0,
    )


@pytest.fixture
def mock_http() -> Iterator[respx.MockRouter]:
    """Intercept every httpx request; any URL without a route raises an error."""
    with respx.mock(assert_all_called=False) as router:
        yield router


def make_source(**overrides: Any) -> Source:
    fields: dict[str, Any] = {
        "key": "example",
        "name": "Example University",
        "url": "https://example.edu.pk/admissions",
        "program_key": "example",
        "country": "PK",
        "opportunity_type": "admission",
        "location": "pakistan",
    }
    fields.update(overrides)
    return Source.model_validate(fields)


def make_result(
    key: str = "example",
    text: str | None = "Applications are open. Deadline: 15 January 2027.",
    status: FetchStatus = FetchStatus.OK,
    **overrides: Any,
) -> FetchResult:
    """A FetchResult for a source with this key (its url is made unique from the key)."""
    ok = status is FetchStatus.OK
    fields: dict[str, Any] = {
        "http_status": 200 if ok else None,
        "content_type": "text/html" if ok else None,
        "text": text if ok else None,
        "content_hash": content_hash(text) if ok and text is not None else None,
    }
    fields.update(overrides)
    source = make_source(key=key, url=f"https://example.edu.pk/{key}")
    return FetchResult(source, status, **fields)


class FakeProvider:
    """An LLM provider for tests: returns (or raises) the scripted outcomes in order.

    Each outcome is a PageExtraction (returned with 100 tokens in, 20 out) or an exception.
    Every call is recorded as (page_text, feedback); the `today` of each call goes to `todays`.
    """

    def __init__(self, *outcomes: PageExtraction | Exception, name: str = "fake-model") -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str | None]] = []
        self.todays: list[date | None] = []
        self.name = name
        self.last_model = name

    def extract(
        self,
        page_text: str,
        source: Source,
        feedback: str | None = None,
        today: date | None = None,
    ) -> tuple[PageExtraction, int, int]:
        self.calls.append((page_text, feedback))
        self.todays.append(today)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.last_model = self.name
        return outcome, 100, 20
