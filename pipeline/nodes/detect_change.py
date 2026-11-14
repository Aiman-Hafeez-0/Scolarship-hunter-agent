"""Compare each page's fingerprint with the one we saw last time."""

from collections.abc import Mapping
from dataclasses import replace

from pipeline.models import Change, FetchResult, FetchStatus


def detect_changes(
    results: list[FetchResult], last_hashes: Mapping[str, str | None]
) -> list[FetchResult]:
    """Return copies of the results with `change` filled in.

    Only "ok" results get a change: new (never seen before), changed or unchanged.
    Everything else gets None, because we have no fresh text to compare.
    """
    updated: list[FetchResult] = []
    for result in results:
        change: Change | None = None
        if result.status is FetchStatus.OK and result.content_hash:
            previous = last_hashes.get(result.source.key)
            if previous is None:
                change = Change.NEW
            elif previous == result.content_hash:
                change = Change.UNCHANGED
            else:
                change = Change.CHANGED
        updated.append(replace(result, change=change))
    return updated
