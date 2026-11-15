"""Copy the latest saved page text of sources into the golden test set.

    uv run python -m pipeline.golden KEY [KEY ...]

Writes pipeline/tests/golden/<key>.txt for each key. The matching <key>.expected.json is
written by hand (by reading the page), never by the LLM. See pipeline/eval.py.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

import psycopg

from pipeline import db
from pipeline.config import Settings

GOLDEN_DIR = Path(__file__).resolve().parent / "tests" / "golden"


def save_golden_text(key: str, text: str, golden_dir: Path = GOLDEN_DIR) -> Path:
    """Write the page text to <golden_dir>/<key>.txt and return the path."""
    golden_dir.mkdir(parents=True, exist_ok=True)
    path = golden_dir / f"{key}.txt"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def copy_snapshots(
    keys: list[str],
    latest_text: Callable[[str], str | None],
    golden_dir: Path = GOLDEN_DIR,
) -> tuple[list[Path], list[str]]:
    """Copy each key's latest text. Returns (files written, keys with no snapshot)."""
    written: list[Path] = []
    missing: list[str] = []
    for key in keys:
        text = latest_text(key)
        if text is None:
            missing.append(key)
        else:
            written.append(save_golden_text(key, text, golden_dir))
    return written, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.golden")
    parser.add_argument("keys", nargs="+", metavar="KEY", help="source keys to copy")
    args = parser.parse_args(argv)

    settings = Settings()
    if not settings.database_url:
        print("Error: DATABASE_URL is not set in .env", file=sys.stderr)
        return 2
    with psycopg.connect(settings.database_url) as conn:

        def latest_text(key: str) -> str | None:
            snapshot = db.latest_snapshot(conn, key)
            return snapshot.text if snapshot else None

        written, missing = copy_snapshots(args.keys, latest_text)

    for path in written:
        print(f"wrote {path.name} ({path.stat().st_size:,} bytes)")
    for key in missing:
        print(f"no snapshot for {key}; run: uv run python -m pipeline.run fetch --only {key}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
