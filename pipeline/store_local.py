"""Save fetch results to plain files, so you can try the pipeline without a database.

data/state.json                          {"last_hashes": {"<key>": "<sha256>", ...}}
data/snapshots/<key>/<UTC timestamp>.txt  page text, written only when new or changed
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from pipeline.models import Change, FetchResult, FetchStatus

TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"  # no ":" so it is a valid file name on Windows too


class LocalStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.state_path = data_dir / "state.json"
        self.snapshots_dir = data_dir / "snapshots"

    def last_hashes(self) -> dict[str, str]:
        """The fingerprint we saved last time for each source key (empty on the first run)."""
        if not self.state_path.exists():
            return {}
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        return dict(state.get("last_hashes", {}))

    def save(self, results: list[FetchResult], now: datetime | None = None) -> list[Path]:
        """Write snapshots for new or changed pages and update state.json.

        Returns the snapshot files that were written.
        """
        stamp = (now or datetime.now(UTC)).strftime(TIMESTAMP_FORMAT)
        hashes = self.last_hashes()
        written: list[Path] = []

        for result in results:
            if result.status is not FetchStatus.OK or result.change not in (
                Change.NEW,
                Change.CHANGED,
            ):
                continue
            if result.text is None or result.content_hash is None:
                continue
            path = self.snapshots_dir / result.source.key / f"{stamp}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.text, encoding="utf-8")
            hashes[result.source.key] = result.content_hash
            written.append(path)

        self._write_state({"last_hashes": hashes})
        return written

    def _write_state(self, state: dict[str, dict[str, str]]) -> None:
        # Write to a temporary file first, then swap it in, so a crash never leaves half a file.
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)
