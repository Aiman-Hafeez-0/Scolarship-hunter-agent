"""Shared test helpers for the dashboard: settings with a known password and a fake store."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.main import create_app, get_store, get_today
from app.queries import (
    DeadlineDetail,
    FieldRow,
    MatchInfo,
    Quote,
    RunRow,
    ScholarshipDetail,
    ScholarshipRow,
    UpcomingDeadline,
)
from pipeline.config import Settings
from pipeline.migrate import apply_migrations
from pipeline.tests.conftest import test_conn  # noqa: F401  (a fixture used by `conn`)

EMAIL = "owner@example.com"
PASSWORD = "correct horse battery"
PASSWORD_HASH = hash_password(PASSWORD)  # hashed once: argon2 is slow on purpose
TODAY = date(2026, 11, 19)
SID = "11111111-1111-1111-1111-111111111111"

Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def conn(test_conn: Conn) -> Iterator[Conn]:  # noqa: F811
    """A throwaway schema with all migrations applied (skipped without TEST_DATABASE_URL)."""
    apply_migrations(test_conn)
    yield test_conn


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "dashboard_email": EMAIL,
        "dashboard_password_hash": PASSWORD_HASH,
        "session_secret": "x" * 40,
        "database_url": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def row(**overrides: object) -> ScholarshipRow:
    values: dict[str, object] = {
        "id": SID, "name": "Stipendium Hungaricum", "provider": "Tempus", "country": "Hungary",
        "opportunity_type": "scholarship", "location": "abroad", "funding": "full",
        "status": "open", "needs_review": False, "verified": False,
        "source_url": "https://example.org/sh", "next_deadline": date(2027, 1, 15),
        "match_status": "eligible", "score": 80,
    }  # fmt: skip
    values.update(overrides)
    return ScholarshipRow(**values)  # type: ignore[arg-type]


def detail(**overrides: object) -> ScholarshipDetail:
    values: dict[str, object] = {
        "id": SID, "name": "Stipendium Hungaricum", "source_name": "Tempus Public Foundation",
        "source_url": "https://example.org/sh", "application_url": "https://apply.example.org",
        "status": "open", "needs_review": True,
        "review_notes": ["deadlines[1] quote not found on page (score 62)"],
        "verified_at": None, "extracted_at": datetime(2026, 11, 18, 3, 20, tzinfo=UTC),
        "extraction_version": "extract_v3+gemini-3.8-flash",
        "fields": [FieldRow("Funding", "full", [Quote("Full tuition waiver and stipend", 100)],
                            has_evidence=True),
                   FieldRow("Campus", "", [], has_evidence=False)],
        "deadlines": [DeadlineDetail("Application deadline", date(2027, 1, 15), None, False,
                                     Quote("The deadline is 15 January 2027.", 98))],
        "requirements": [], "documents": ["Transcript"],
        "match": MatchInfo("eligible", 80, [{"rule": "nationality", "outcome": "pass",
                                             "reason": "open to Pakistan", "action": None},
                                            {"rule": "english", "outcome": "unknown",
                                             "reason": "no score in profile",
                                             "action": "take IELTS or TOEFL"}],
                           datetime(2026, 11, 18, 4, 0, tzinfo=UTC)),
        "changes": [],
    }  # fmt: skip
    values.update(overrides)
    return ScholarshipDetail(**values)  # type: ignore[arg-type]


def run_row(**overrides: object) -> RunRow:
    values: dict[str, object] = {
        "id": "r1", "kind": "weekly", "started_at": datetime.now(UTC) - timedelta(days=1),
        "finished_at": datetime.now(UTC) - timedelta(days=1), "status": "success",
        "sources_total": 18, "sources_changed": 3, "sources_failed": 1, "items_saved": 7,
        "items_needing_review": 2, "tokens_in": 12000, "tokens_out": 3400, "error": None,
    }  # fmt: skip
    values.update(overrides)
    return RunRow(**values)  # type: ignore[arg-type]


@dataclass
class FakeStore:
    """The DashboardStore methods, answered from lists. `calls` records what was asked."""

    rows: list[ScholarshipRow] = field(default_factory=lambda: [row()])
    details: dict[str, ScholarshipDetail] = field(default_factory=lambda: {SID: detail()})
    upcoming: list[UpcomingDeadline] = field(default_factory=list)
    run_rows: list[RunRow] = field(default_factory=lambda: [run_row()])
    last_good: datetime | None = field(default_factory=lambda: datetime.now(UTC))
    calls: list[str] = field(default_factory=list)

    def profile_id(self, email: str) -> str | None:
        self.calls.append(f"profile_id:{email}")
        return "p1"

    def scholarship_rows(self, profile_id: str | None, today: date) -> list[ScholarshipRow]:
        return list(self.rows)

    def upcoming_deadlines(self, today: date, limit: int) -> list[UpcomingDeadline]:
        return self.upcoming[:limit]

    def scholarship(self, profile_id: str | None, scholarship_id: str) -> ScholarshipDetail | None:
        return self.details.get(scholarship_id)

    def runs(self, limit: int) -> list[RunRow]:
        return self.run_rows[:limit]

    def last_good_weekly_at(self) -> datetime | None:
        return self.last_good


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def client(store: FakeStore) -> Iterator[TestClient]:
    """A client on http://localhost (so cookies work without https), not logged in."""
    app = create_app(make_settings())
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_today] = lambda: TODAY
    with TestClient(app, base_url="http://localhost") as test_client:
        yield test_client


def csrf_from(html: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert found, "page has no CSRF token"
    return found.group(1)


def log_in(client: TestClient, password: str = PASSWORD) -> Any:  # the TestClient's Response
    token = csrf_from(client.get("/login").text)
    return client.post("/login", data={"email": EMAIL, "password": password,
                                       "csrf_token": token}, follow_redirects=False)  # fmt: skip


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    response = log_in(client)
    assert response.status_code == 303
    return client
