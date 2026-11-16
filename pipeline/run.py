"""Command line entry point.

    uv run python -m pipeline.run fetch                 fetch active sources, save to the database
    uv run python -m pipeline.run fetch --local         save to data/ instead (no database needed)
    uv run python -m pipeline.run fetch --only nust-pg  fetch just one source (even an inactive one)
    uv run python -m pipeline.run extract --only kaust-timeline --dry-run
                                                        LLM extraction of the latest saved page;
                                                        prints JSON, saves nothing
    uv run python -m pipeline.run weekly                fetch, extract, validate and save every
                                                        active source whose page changed
    uv run python -m pipeline.run weekly --only KEY --force
                                                        one source, re-extracted even if unchanged
                                                        (ends by matching, like `match`)
    uv run python -m pipeline.run weekly --provider groq
                                                        which LLM to use: auto (default: Gemini,
                                                        Groq as fallback), gemini or groq
    uv run python -m pipeline.run profile               save profile.yaml to the profiles table
    uv run python -m pipeline.run match                 match every profile against every
                                                        scholarship that is not closed

Exit code: 0 normally; 1 if every source failed (fetch, weekly) or the result needs review
(extract); 2 for setup problems (bad config, unknown key, missing snapshot or profile,
database unreachable). Database errors are printed with the password hidden.
"""

import argparse
import asyncio
import io
import json
import logging
import sys
from datetime import date
from typing import Any

import psycopg

from pipeline import db
from pipeline.config import Settings, mask_secrets
from pipeline.graph import SourceOutcome, run_sources
from pipeline.llm.provider import PROVIDER_CHOICES, LLMProvider, ProviderChoice, build_provider
from pipeline.models import Change, FetchResult, FetchStatus, Source
from pipeline.nodes.detect_change import detect_changes
from pipeline.nodes.extract import ExtractResult, extract_page
from pipeline.nodes.fetch import fetch_all
from pipeline.nodes.load_sources import SourcesError, load_sources
from pipeline.nodes.match import Match, run_matching
from pipeline.nodes.validate import is_closed
from pipeline.profile import ProfileError, load_profile
from pipeline.prompts import EXTRACT_VERSION
from pipeline.store_local import LocalStore

MAX_ERROR_WIDTH = 60


class SetupError(Exception):
    """A problem the user must fix before the run can start."""


def choose_sources(all_sources: list[Source], only: str | None) -> list[Source]:
    if only is None:
        return [s for s in all_sources if s.active]
    for source in all_sources:
        if source.key == only:
            return [source]
    raise SetupError(f"No source with key '{only}' in sources.yaml")


def format_summary(results: list[FetchResult]) -> str:
    """A plain text table with one row per source, plus a totals line."""
    headers = ["key", "status", "change", "http", "chars", "error"]
    rows = [
        [
            r.source.key,
            r.status.value,
            r.change.value if r.change else "-",
            str(r.http_status) if r.http_status is not None else "-",
            str(len(r.text)) if r.text is not None else "-",
            _shorten(r.error or ""),
        ]
        for r in results
    ]
    counts = {status: sum(r.status is status for r in results) for status in FetchStatus}
    totals = ", ".join(f"{n} {status.value}" for status, n in counts.items() if n)
    return f"{_table(headers, rows)}\n\n{len(results)} sources: {totals or 'none'}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    """Plain text columns with a dashed line under the headers."""
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
             for row in [headers, *rows]]  # fmt: skip
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_ERROR_WIDTH else text[: MAX_ERROR_WIDTH - 3] + "..."


def exit_code(results: list[FetchResult]) -> int:
    """1 only if there were sources and every one of them failed."""
    if results and all(r.status is FetchStatus.FAILED for r in results):
        return 1
    return 0


def run_fetch(settings: Settings, local: bool, only: str | None) -> list[FetchResult]:
    if not local and not settings.database_url:
        raise SetupError("DATABASE_URL is not set: use --local or set DATABASE_URL in .env")

    all_sources = load_sources(settings.sources_file, include_inactive=True)
    chosen = choose_sources(all_sources, only)

    if local:
        store = LocalStore(settings.data_dir)
        local_results = detect_changes(
            asyncio.run(fetch_all(chosen, settings)), store.last_hashes()
        )
        store.save(local_results)
        return local_results

    assert settings.database_url is not None
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        db.sync_sources(conn, all_sources)
        run_id = db.start_run(conn, "fetch")
        results: list[FetchResult] = []
        try:
            fetched = asyncio.run(fetch_all(chosen, settings))
            results = detect_changes(fetched, db.last_hashes(conn))
            for result in results:
                db.save_result(conn, result)
        except BaseException as exc:
            db.finish_run(conn, run_id, results, error=f"{type(exc).__name__}: {exc}")
            raise
        db.finish_run(conn, run_id, results)
    return results


def run_extract(
    settings: Settings, only: str, provider: LLMProvider | None = None
) -> dict[str, Any]:
    """Extract the latest saved snapshot of one source. Saves nothing; returns a report."""
    if not settings.database_url:
        raise SetupError("DATABASE_URL is not set: extract reads the latest snapshot from it")
    [source] = choose_sources(load_sources(settings.sources_file, include_inactive=True), only)

    with psycopg.connect(settings.database_url) as conn:
        snapshot = db.latest_snapshot(conn, source.key)
    if snapshot is None:
        raise SetupError(
            f"No saved snapshot for '{source.key}'. First run: "
            f"uv run python -m pipeline.run fetch --only {source.key}"
        )

    result = extract_page(snapshot.text, source, provider or build_provider(settings))
    return extraction_report(result, snapshot)


def extraction_report(result: ExtractResult, snapshot: db.Snapshot) -> dict[str, Any]:
    """Everything about one extraction as plain JSON-ready data."""
    return {
        "source": result.source.key,
        "snapshot_id": str(snapshot.id),
        "snapshot_fetched_at": snapshot.fetched_at.isoformat(),
        "page_chars": len(snapshot.text),
        "extract_version": EXTRACT_VERSION,
        "model": result.model,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "needs_review": result.needs_review,
        "errors": result.errors,
        "extraction": result.extraction.model_dump(mode="json"),
    }


# ---------- weekly command ----------


def run_weekly(
    settings: Settings,
    only: str | None,
    force: bool,
    provider: LLMProvider | None = None,
    today: date | None = None,
    provider_choice: ProviderChoice = "auto",
) -> tuple[list[SourceOutcome], list[Match]]:
    """Sync sources, run the graph for each chosen source, record the run, then match."""
    if not settings.database_url:
        raise SetupError("DATABASE_URL is not set: the weekly run saves to the database")
    all_sources = load_sources(settings.sources_file, include_inactive=True)
    chosen = choose_sources(all_sources, only)
    provider = provider or build_provider(settings, provider_choice)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        db.sync_sources(conn, all_sources)
        run_id = db.start_run(conn, "weekly")
        try:
            outcomes = asyncio.run(
                run_sources(chosen, settings, provider, db.PostgresStore(conn),
                            db.last_hashes(conn), force=force, today=today)
            )  # fmt: skip
        except BaseException as exc:
            db.finish_run(conn, run_id, [], error=f"{type(exc).__name__}: {exc}")
            raise
        db.finish_run(conn, run_id, [o.fetched for o in outcomes], totals=weekly_totals(outcomes))
        matches = run_matching(conn, today)
    return outcomes, matches


# ---------- profile and match commands ----------


def run_profile(settings: Settings) -> str:
    """Save profile.yaml to the database. Returns a one-line confirmation."""
    profile = load_profile(settings.profile_file)
    if not settings.database_url:
        raise SetupError("DATABASE_URL is not set: the profile is saved to the database")
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        db.upsert_profile(conn, profile)
    return f"Saved profile for {profile.full_name} ({profile.email})"


def run_match(settings: Settings, today: date | None = None) -> list[Match]:
    if not settings.database_url:
        raise SetupError("DATABASE_URL is not set: matching reads from the database")
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        return run_matching(conn, today)


def format_matches(matches: list[Match], top: int = 5) -> str:
    """Per profile: every match by score, then the reasons for the best `top`."""
    if not matches:
        return (
            "No matches: add your profile (fill in profile.yaml, then run"
            " `uv run python -m pipeline.run profile`) or save scholarships first."
        )
    lines: list[str] = []
    for name in dict.fromkeys(m.profile_name for m in matches):
        mine = [m for m in matches if m.profile_name == name]
        statuses = ("eligible", "maybe", "not_eligible")
        counts = {s: sum(m.status == s for m in mine) for s in statuses}
        lines.append(f"Matches for {name}: {len(mine)} "
                     f"({', '.join(f'{n} {s}' for s, n in counts.items())})")  # fmt: skip
        rows = [[str(m.score), m.status, m.scholarship.opportunity_type, m.scholarship.name]
                for m in mine]  # fmt: skip
        lines += [_table(["score", "status", "type", "name"], rows), "", f"Top {top} reasons:"]
        for rank, m in enumerate(mine[:top], start=1):
            lines.append(f"\n{rank}. {m.scholarship.name}  (score {m.score}, {m.status})")
            for r in m.reasons:
                action = f"  -> {r.action}" if r.action else ""
                lines.append(f"   {r.outcome:<8} {r.rule:<16} {r.reason}{action}")
        lines.append("")
    return "\n".join(lines).rstrip()


def weekly_totals(outcomes: list[SourceOutcome]) -> db.RunTotals:
    saved = [o for o in outcomes if o.saved]
    return db.RunTotals(
        tokens_in=sum(o.extracted.tokens_in for o in outcomes if o.extracted),
        tokens_out=sum(o.extracted.tokens_out for o in outcomes if o.extracted),
        items_saved=sum(o.saved for o in saved),
        items_needing_review=sum(item.needs_review for o in saved for item in o.items),
    )


def format_weekly_summary(outcomes: list[SourceOutcome]) -> str:
    """One row per source, then the totals."""
    headers = ["key", "fetch", "change", "items", "review", "tokens", "note"]
    rows: list[list[str]] = []
    for o in outcomes:
        tokens = o.extracted.tokens_in + o.extracted.tokens_out if o.extracted else 0
        errors = o.extracted.errors if o.extracted else []
        note = o.error or o.fetched.error or "; ".join(errors)
        rows.append([
            o.source.key,
            o.fetched.status.value,
            o.fetched.change.value if o.fetched.change else "-",
            str(o.saved) if o.extracted else "-",
            str(sum(item.needs_review for item in o.items)) if o.extracted else "-",
            str(tokens) if o.extracted else "-",
            _shorten(note),
        ])  # fmt: skip

    totals = weekly_totals(outcomes)
    changed = sum(o.fetched.change in (Change.NEW, Change.CHANGED) for o in outcomes)
    failed = sum(o.fetched.status is FetchStatus.FAILED for o in outcomes)
    extracted = sum(o.extracted is not None for o in outcomes)
    lines = [
        _table(headers, rows),
        "",
        f"Sources checked:      {len(outcomes)} ({failed} failed, {extracted} extracted)",
        f"Sources changed:      {changed}",
        f"Items saved:          {totals.items_saved}",
        f"Items needing review: {totals.items_needing_review}",
        f"Tokens used:          {totals.tokens_in + totals.tokens_out:,}"
        f" ({totals.tokens_in:,} in, {totals.tokens_out:,} out)",
    ]
    return "\n".join(lines)


def format_saved_items(outcomes: list[SourceOutcome], today: date | None = None) -> str:
    """Every saved item with its deadlines and, if any, why it needs review."""
    today = today or date.today()
    lines: list[str] = []
    for o in outcomes:
        if not o.saved:
            continue
        for v in o.items:
            item = v.item
            review = "  [NEEDS REVIEW]" if v.needs_review else ""
            lines.append(f"[{o.source.key}] {item.name} ({item.status}){review}")
            if not item.deadlines:
                lines.append("    no deadlines")
            for d in item.deadlines:
                when = d.deadline_date.isoformat() if d.deadline_date else "date unknown"
                extra = [d.time_and_timezone or "", "estimated" if d.is_estimated else "",
                         "passed" if is_closed(d, today) else ""]  # fmt: skip
                details = ", ".join(x for x in extra if x)
                lines.append(f"    {when}  {d.label}" + (f"  ({details})" if details else ""))
            lines.extend(f"    review: {note}" for note in v.notes)
    return "\n".join(lines) if lines else "No items saved."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.run")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="download sources and detect changes")
    fetch.add_argument("--local", action="store_true", help="save to data/ instead of the DB")
    fetch.add_argument("--only", metavar="KEY", help="fetch only the source with this key")
    extract = commands.add_parser("extract", help="extract facts from a saved page with the LLM")
    extract.add_argument("--only", metavar="KEY", required=True, help="the source to extract")
    extract.add_argument("--dry-run", action="store_true", help="print the JSON, save nothing")
    weekly = commands.add_parser("weekly", help="fetch, extract, validate and save every source")
    weekly.add_argument("--only", metavar="KEY", help="run only the source with this key")
    weekly.add_argument("--force", action="store_true", help="re-extract unchanged pages too")
    weekly.add_argument("--provider", choices=PROVIDER_CHOICES, default="auto",
                        help="LLM to use (auto = Gemini, then Groq if Gemini fails)")
    commands.add_parser("profile", help="save profile.yaml to the profiles table")
    commands.add_parser("match", help="match every profile against open scholarships")
    args = parser.parse_args(argv)

    if args.command == "extract" and not args.dry_run:
        parser.error("extract only prints, add --dry-run (to save, use: weekly --only KEY --force)")

    # Page text can contain any language; make sure printing it never crashes on Windows.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        if args.command == "extract":
            report = run_extract(Settings(), only=args.only)
        elif args.command == "weekly":
            outcomes, matches = run_weekly(Settings(), only=args.only, force=args.force,
                                           provider_choice=args.provider)
        elif args.command == "profile":
            print(run_profile(Settings()))
            return 0
        elif args.command == "match":
            matches = run_match(Settings())
        else:
            results = run_fetch(Settings(), local=args.local, only=args.only)
    except (SetupError, SourcesError, ProfileError, ValueError) as exc:
        print(f"Error: {mask_secrets(str(exc))}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:  # never print the connection string's password
        print(f"Database error: {mask_secrets(str(exc))}", file=sys.stderr)
        return 2

    if args.command == "extract":
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1 if report["needs_review"] else 0
    if args.command == "match":
        print(format_matches(matches))
        return 0
    if args.command == "weekly":
        print(format_weekly_summary(outcomes))
        print(f"\nSaved items:\n{format_saved_items(outcomes)}")
        print(f"\n{format_matches(matches)}")
        return exit_code([o.fetched for o in outcomes])
    print(format_summary(results))
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
