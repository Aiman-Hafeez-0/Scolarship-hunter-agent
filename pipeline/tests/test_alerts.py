"""Tests for admin alerts (fake store and fake mailer: no database, nothing sent)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pipeline.notify.alerts import check_and_alert, month_start
from pipeline.notify.store import FailingSource, FinishedRun
from pipeline.tests.test_reminders import FakeMailer

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
ADMIN = "admin@example.com"
BUDGET = 1000


@dataclass
class FakeStore:
    """A healthy system by default; change a field to create a problem."""

    run: FinishedRun | None = field(
        default_factory=lambda: FinishedRun("weekly", "success", NOW - timedelta(days=1), None)
    )
    failing: list[FailingSource] = field(default_factory=list)
    last_weekly: datetime | None = NOW - timedelta(days=1)
    tokens: int = 100
    token_starts: list[datetime] = field(default_factory=list)
    # alert key -> resolved? (like notifications rows with kind 'admin'); history of every row
    rows: list[dict[str, object]] = field(default_factory=list)

    def last_finished_run(self) -> FinishedRun | None:
        return self.run

    def failing_sources(self, min_failures: int) -> list[FailingSource]:
        return [s for s in self.failing if s.failures >= min_failures]

    def last_good_weekly_at(self) -> datetime | None:
        return self.last_weekly

    def tokens_since(self, start: datetime) -> int:
        self.token_starts.append(start)
        return self.tokens

    def _open(self) -> list[dict[str, object]]:
        return [r for r in self.rows if not r["resolved"]]

    def open_alerts(self) -> list[str]:
        return [str(r["key"]) for r in self._open()]

    def claim_alert(self, key: str) -> str | None:
        if key in self.open_alerts():
            return None  # the unique index on open alerts
        self.rows.append({"id": f"n{len(self.rows)}", "key": key, "resolved": False,
                          "status": "pending"})  # fmt: skip
        return str(self.rows[-1]["id"])

    def resolve_alert(self, key: str) -> None:
        for r in self._open():
            if r["key"] == key:
                r["resolved"] = True

    def mark_sent(self, notification_id: str, message_id: str) -> None:
        next(r for r in self.rows if r["id"] == notification_id)["status"] = "sent"

    def release(self, notification_id: str) -> None:
        self.rows = [r for r in self.rows if r["id"] != notification_id]


def check(store: FakeStore, now: datetime = NOW) -> tuple[list[str], list[str]]:
    """(subjects emailed, keys resolved)."""
    mailer = FakeMailer()
    report = check_and_alert(store, mailer, ADMIN, BUDGET, now)
    assert all(email["to"] == ADMIN for email in mailer.sent)
    return [email["subject"] for email in mailer.sent], report.resolved


def test_healthy_system_sends_nothing() -> None:
    assert check(FakeStore()) == ([], [])


def test_failed_last_run_alerts_once_and_rearms() -> None:
    store = FakeStore(run=FinishedRun("fetch", "failed", NOW, "ConnectError: boom"))
    assert check(store)[0] == ["[Scholarship Hunter] The last run failed (fetch)"]
    assert check(store) == ([], [])  # still failing: no second email

    store.run = FinishedRun("fetch", "success", NOW, None)
    assert check(store) == ([], ["last_run_failed"])  # fixed: re-armed

    store.run = FinishedRun("weekly", "failed", NOW, None)
    assert check(store)[0] == ["[Scholarship Hunter] The last run failed (weekly)"]


def test_partial_run_is_not_a_failure() -> None:
    assert check(FakeStore(run=FinishedRun("weekly", "partial", NOW, None))) == ([], [])


def test_source_failing_two_runs_in_a_row() -> None:
    store = FakeStore(failing=[FailingSource("nust", "https://nust.edu.pk", 1, "timeout")])
    assert check(store) == ([], [])  # one failure is not enough

    store.failing = [FailingSource("nust", "https://nust.edu.pk", 2, "timeout"),
                     FailingSource("giki", "https://giki.edu.pk", 3, None)]  # fmt: skip
    subjects, _ = check(store)
    assert subjects == ["[Scholarship Hunter] Source nust failed 2 runs in a row",
                        "[Scholarship Hunter] Source giki failed 3 runs in a row"]  # fmt: skip
    store.failing[0] = FailingSource("nust", "https://nust.edu.pk", 3, "timeout")
    assert check(store) == ([], [])  # a third failure does not alert again

    store.failing = store.failing[1:]  # nust works again
    assert check(store) == ([], ["source_failing:nust"])
    store.failing.append(FailingSource("nust", "https://nust.edu.pk", 2, "timeout"))
    assert check(store)[0] == ["[Scholarship Hunter] Source nust failed 2 runs in a row"]


def test_no_successful_weekly_run_in_8_days() -> None:
    store = FakeStore(last_weekly=NOW - timedelta(days=8))
    assert check(store) == ([], [])  # exactly 8 days is still fine

    store.last_weekly = NOW - timedelta(days=8, minutes=1)
    assert check(store)[0] == ["[Scholarship Hunter] No successful weekly run in the last 8 days"]
    assert check(store) == ([], [])

    store.last_weekly = NOW
    assert check(store) == ([], ["no_weekly_run"])


def test_never_a_weekly_run_alerts() -> None:
    assert len(check(FakeStore(last_weekly=None))[0]) == 1


def test_token_budget_over_80_percent() -> None:
    store = FakeStore(tokens=800)
    assert check(store) == ([], [])  # exactly 80% is fine

    store.tokens = 801
    assert check(store)[0] == ["[Scholarship Hunter] LLM tokens at 80% of this month's budget"]
    assert check(store) == ([], [])

    # A new month: last month's alert is resolved, and this month's counts start over.
    store.tokens = 900
    december = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    store.last_weekly = december  # keep the weekly check quiet
    subjects, resolved = check(store, december)
    assert resolved == ["token_budget:2026-11"]
    assert subjects == ["[Scholarship Hunter] LLM tokens at 90% of this month's budget"]


def test_token_budget_zero_turns_the_check_off() -> None:
    store = FakeStore(tokens=10**9)
    report = check_and_alert(store, FakeMailer(), ADMIN, 0, NOW)
    assert report.sent == [] and store.token_starts == []


def test_month_starts_in_pakistan_time() -> None:
    # 19:30 UTC on 30 Nov is already 1 Dec in Pakistan.
    start = month_start(datetime(2026, 11, 30, 19, 30, tzinfo=UTC))
    assert start.isoformat() == "2026-12-01T00:00:00+05:00"


def test_failed_send_is_retried_next_check() -> None:
    store = FakeStore(last_weekly=None)
    report = check_and_alert(store, FakeMailer(fail=True), ADMIN, BUDGET, NOW)
    assert report.sent[0].error is not None
    assert store.rows == []
    assert len(check(store)[0]) == 1


def test_alert_text_has_details() -> None:
    store = FakeStore(failing=[FailingSource("nust", "https://nust.edu.pk", 2, "HTTP 503")])
    mailer = FakeMailer()
    check_and_alert(store, mailer, ADMIN, BUDGET, NOW)
    assert "https://nust.edu.pk" in mailer.sent[0]["text"]
    assert "Last error: HTTP 503" in mailer.sent[0]["text"]
