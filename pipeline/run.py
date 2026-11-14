"""Command line entry point.

    uv run python -m pipeline.run fetch                 fetch active sources, save to the database
    uv run python -m pipeline.run fetch --local         save to data/ instead (no database needed)
    uv run python -m pipeline.run fetch --only nust-pg  fetch just one source (even an inactive one)

Exit code: 0 normally, 1 if every source failed, 2 for setup problems (bad config, unknown key).
"""

import argparse
import asyncio
import sys

import psycopg

from pipeline import db
from pipeline.config import Settings
from pipeline.models import FetchResult, FetchStatus, Source
from pipeline.nodes.detect_change import detect_changes
from pipeline.nodes.fetch import fetch_all
from pipeline.nodes.load_sources import SourcesError, load_sources
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
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
             for row in [headers, *rows]]  # fmt: skip
    lines.insert(1, "  ".join("-" * w for w in widths))

    counts = {status: sum(r.status is status for r in results) for status in FetchStatus}
    totals = ", ".join(f"{n} {status.value}" for status, n in counts.items() if n)
    lines.append(f"\n{len(results)} sources: {totals or 'none'}")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.run")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="download sources and detect changes")
    fetch.add_argument("--local", action="store_true", help="save to data/ instead of the DB")
    fetch.add_argument("--only", metavar="KEY", help="fetch only the source with this key")
    args = parser.parse_args(argv)

    try:
        results = run_fetch(Settings(), local=args.local, only=args.only)
    except (SetupError, SourcesError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(format_summary(results))
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
