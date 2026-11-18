"""Admin alerts: tell ADMIN_EMAIL when something needs fixing.

Problems we check for:
  - the last run (of any kind) failed;
  - a source failed 2 runs in a row;
  - no weekly run worked (success or partial) in the last 8 days;
  - LLM tokens used this month (Pakistan time) are over 80% of monthly_token_budget.

Each problem has a key, e.g. "source_failing:nust-pg". The first time we see it, one email is
sent and a notifications row (kind 'admin') is kept open. While the row is open, no new email.
When a check no longer finds the problem, the row is closed (resolved_at), so if the problem
comes back later, a new alert is sent.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Protocol

from pipeline.notify.deadlines import PKT
from pipeline.notify.digest import Mailer, SendResult
from pipeline.notify.mailer import MailError
from pipeline.notify.store import FailingSource, FinishedRun

SOURCE_FAILURES = 2  # failed runs in a row before we alert
WEEKLY_MAX_AGE = timedelta(days=8)
TOKEN_SHARE = 0.8  # alert above this share of the monthly budget


class AlertStore(Protocol):
    def last_finished_run(self) -> FinishedRun | None: ...
    def failing_sources(self, min_failures: int) -> list[FailingSource]: ...
    def last_good_weekly_at(self) -> datetime | None: ...
    def tokens_since(self, start: datetime) -> int: ...
    def open_alerts(self) -> list[str]: ...
    def claim_alert(self, key: str) -> str | None: ...
    def resolve_alert(self, key: str) -> None: ...
    def mark_sent(self, notification_id: str, message_id: str) -> None: ...
    def release(self, notification_id: str) -> None: ...


@dataclass(frozen=True)
class Problem:
    key: str
    subject: str
    details: str


@dataclass
class AlertReport:
    sent: list[SendResult]
    resolved: list[str]  # keys of problems that are gone


def month_start(now: datetime) -> datetime:
    """Midnight on the 1st of this month, in Pakistan time."""
    local = now.astimezone(PKT)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def find_problems(store: AlertStore, token_budget: int, now: datetime) -> list[Problem]:
    problems: list[Problem] = []

    run = store.last_finished_run()
    if run is not None and run.status == "failed":
        problems.append(Problem(
            "last_run_failed",
            f"The last run failed ({run.kind})",
            f"The {run.kind} run that finished {run.finished_at.astimezone(PKT):%Y-%m-%d %H:%M}"
            f" PKT failed.\nError: {run.error or 'none recorded'}",
        ))  # fmt: skip

    for source in store.failing_sources(SOURCE_FAILURES):
        problems.append(Problem(
            f"source_failing:{source.key}",
            f"Source {source.key} failed {source.failures} runs in a row",
            f"{source.url}\nLast error: {source.last_error or 'none recorded'}",
        ))  # fmt: skip

    last_weekly = store.last_good_weekly_at()
    if last_weekly is None or now - last_weekly > WEEKLY_MAX_AGE:
        when = "never" if last_weekly is None else f"{last_weekly.astimezone(PKT):%Y-%m-%d} PKT"
        problems.append(Problem(
            "no_weekly_run",
            f"No successful weekly run in the last {WEEKLY_MAX_AGE.days} days",
            f"The last weekly run that worked finished: {when}.",
        ))  # fmt: skip

    if token_budget > 0:
        start = month_start(now)
        used = store.tokens_since(start)
        if used > TOKEN_SHARE * token_budget:
            problems.append(Problem(
                f"token_budget:{start:%Y-%m}",
                f"LLM tokens at {used / token_budget:.0%} of this month's budget",
                f"{used:,} of {token_budget:,} tokens used since {start:%d %b %Y}"
                " (MONTHLY_TOKEN_BUDGET).",
            ))  # fmt: skip

    return problems


def render_alert(problem: Problem) -> tuple[str, str, str]:
    """(subject, plain text, HTML)."""
    subject = f"[Scholarship Hunter] {problem.subject}"
    text = f"{problem.subject}\n\n{problem.details}\n"
    body = escape(problem.details).replace("\n", "<br>")
    return subject, text, f"<p><strong>{escape(problem.subject)}</strong></p><p>{body}</p>"


def check_and_alert(
    store: AlertStore,
    mailer: Mailer,
    admin_email: str,
    token_budget: int,
    now: datetime | None = None,
) -> AlertReport:
    """Close alerts whose problem is gone, then email each new problem once."""
    problems = find_problems(store, token_budget, now or datetime.now(UTC))
    active = {p.key for p in problems}

    resolved = [key for key in store.open_alerts() if key not in active]
    for key in resolved:
        store.resolve_alert(key)

    sent: list[SendResult] = []
    for problem in problems:
        claim = store.claim_alert(problem.key)
        if claim is None:
            continue  # already alerted, and not fixed yet
        subject, text, html = render_alert(problem)
        try:
            message_id = mailer.send(admin_email, subject, text, html)
        except MailError as exc:
            store.release(claim)
            sent.append(SendResult(admin_email, subject, str(exc)))
            continue
        store.mark_sent(claim, message_id)
        sent.append(SendResult(admin_email, subject))
    return AlertReport(sent, resolved)
