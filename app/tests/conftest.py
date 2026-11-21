"""Shared test helpers for the dashboard: settings with a known password and a fake store."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.auth import hash_password
from app.main import create_app, get_login_limiter, get_store, get_today
from app.queries import (
    DeadlineDetail,
    FieldRow,
    MatchInfo,
    ProfileEmailTaken,
    ProfileRecord,
    Quote,
    RecentChange,
    RunRow,
    ScholarshipDetail,
    ScholarshipRow,
    SourceRow,
    UpcomingDeadline,
)
from pipeline.config import Settings
from pipeline.migrate import apply_migrations
from pipeline.models import Source
from pipeline.profile import Profile
from pipeline.tests.conftest import mock_http, test_conn  # noqa: F401  (fixtures used here)

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
        "contact_email": "bot@example.com",
        "per_domain_delay_s": 0,
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


def source_row(**overrides: object) -> SourceRow:
    values: dict[str, object] = {
        "key": "nust-pg", "name": "NUST Postgraduate", "url": "https://nust.edu.pk/pg",
        "opportunity_type": "admission", "location": "pakistan", "active": True,
        "last_fetched_at": datetime(2026, 11, 18, 3, 17, tzinfo=UTC), "last_http_status": 503,
        "consecutive_failures": 3, "last_error": "HTTP 503", "robots_allowed": True,
        "robots_checked_at": datetime(2026, 11, 18, 3, 17, tzinfo=UTC), "note": "Fall intake",
    }  # fmt: skip
    values.update(overrides)
    return SourceRow(**values)  # type: ignore[arg-type]


def profile(**overrides: object) -> Profile:
    values: dict[str, object] = {
        "full_name": "Owner Name", "email": EMAIL, "degree": "BS Computer Science",
        "cgpa": 3.2, "target_fields": ["Artificial Intelligence", "Data Science"],
        "target_intake": "Fall 2027",
    }  # fmt: skip
    values.update(overrides)
    return Profile.model_validate(values)


@dataclass
class FakeStore:
    """The DashboardStore methods, answered from lists. `calls` records what was asked."""

    rows: list[ScholarshipRow] = field(default_factory=lambda: [row()])
    details: dict[str, ScholarshipDetail] = field(default_factory=lambda: {SID: detail()})
    upcoming: list[UpcomingDeadline] = field(default_factory=list)
    changes: list[RecentChange] = field(default_factory=list)
    run_rows: list[RunRow] = field(default_factory=lambda: [run_row()])
    last_good: datetime | None = field(default_factory=lambda: datetime.now(UTC))
    calls: list[str] = field(default_factory=list)
    source_rows: list[SourceRow] = field(default_factory=lambda: [source_row()])
    added: list[Source] = field(default_factory=list)
    profile_record: ProfileRecord | None = field(
        default_factory=lambda: ProfileRecord("p1", profile(), False)
    )
    saved_profiles: list[tuple[str | None, Profile, bool]] = field(default_factory=list)
    taken_emails: set[str] = field(default_factory=set)

    def profile_id(self, email: str) -> str | None:
        self.calls.append(f"profile_id:{email}")
        return "p1"

    def scholarship_rows(self, profile_id: str | None, today: date) -> list[ScholarshipRow]:
        return list(self.rows)

    def upcoming_deadlines(self, today: date, limit: int) -> list[UpcomingDeadline]:
        return self.upcoming[:limit]

    def recent_changes(self, limit: int) -> list[RecentChange]:
        return self.changes[:limit]

    def scholarship(self, profile_id: str | None, scholarship_id: str) -> ScholarshipDetail | None:
        return self.details.get(scholarship_id)

    def runs(self, limit: int) -> list[RunRow]:
        return self.run_rows[:limit]

    def last_good_weekly_at(self) -> datetime | None:
        return self.last_good

    def verify(self, scholarship_id: str) -> datetime | None:
        found = self.details.get(scholarship_id)
        if found is None:
            return None
        found.verified_at = datetime.now(UTC)
        return found.verified_at

    def sources(self) -> list[SourceRow]:
        return list(self.source_rows)

    def set_source_active(self, key: str, active: bool) -> SourceRow | None:
        for source in self.source_rows:
            if source.key == key:
                source.active = active
                return source
        return None

    def add_source(self, source: Source) -> bool:
        known = [(s.key, s.url) for s in self.source_rows] + [(s.key, s.url) for s in self.added]
        if any(source.key == key or source.url == url for key, url in known):
            return False
        self.added.append(source)
        return True

    def profile(self, profile_id: str) -> ProfileRecord | None:
        return self.profile_record

    def save_profile(self, profile_id: str | None, profile: Profile, paused: bool) -> str:
        if profile.email in self.taken_emails:
            raise ProfileEmailTaken(profile.email)
        self.saved_profiles.append((profile_id, profile, paused))
        return profile_id or "new-id"


@dataclass
class FakeLoginLimiter:
    """auth.LoginLimiter kept in a dict, with a clock the test can move."""

    now: float = 0.0
    failures: dict[str, list[float]] = field(default_factory=dict)

    def _recent(self, key: str) -> list[float]:
        cutoff = self.now - auth.LOGIN_WINDOW_S
        recent = [t for t in self.failures.get(key, []) if t > cutoff]
        self.failures[key] = recent
        return recent

    def retry_after_s(self, key: str) -> int:
        recent = self._recent(key)
        if len(recent) < auth.MAX_LOGIN_FAILURES:
            return 0
        return max(1, int(recent[-auth.MAX_LOGIN_FAILURES] + auth.LOGIN_WINDOW_S - self.now) + 1)

    def blocked(self, key: str) -> bool:
        return self.retry_after_s(key) > 0

    def record_failure(self, key: str) -> None:
        self._recent(key).append(self.now)

    def reset(self, key: str) -> None:
        self.failures.pop(key, None)


def use_fake_limiter(app: FastAPI) -> FakeLoginLimiter:
    """Make the app keep its login rate limit in memory instead of the database."""
    limiter = FakeLoginLimiter()
    app.dependency_overrides[get_login_limiter] = lambda: limiter
    return limiter


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def limiter() -> FakeLoginLimiter:
    return FakeLoginLimiter()


@pytest.fixture
def client(store: FakeStore, limiter: FakeLoginLimiter) -> Iterator[TestClient]:
    """A client on http://localhost (so cookies work without https), not logged in."""
    app = create_app(make_settings())
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_login_limiter] = lambda: limiter
    app.dependency_overrides[get_today] = lambda: TODAY
    with TestClient(app, base_url="http://localhost") as test_client:
        yield test_client


def csrf_from(html: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert found, "page has no CSRF token"
    return found.group(1)


def log_in(
    client: TestClient, password: str = PASSWORD, remember: bool = True
) -> Any:  # the TestClient's Response
    """Log in through the form ("Keep me signed in" ticked unless remember=False)."""
    token = csrf_from(client.get("/login").text)
    data = {"email": EMAIL, "password": password, "csrf_token": token}
    if remember:
        data["remember"] = "1"
    return client.post("/login", data=data, follow_redirects=False)


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    response = log_in(client)
    assert response.status_code == 303
    return client
