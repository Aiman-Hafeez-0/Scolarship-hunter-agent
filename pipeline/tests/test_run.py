"""Tests for pipeline.run (the command line)."""

from pathlib import Path

import pytest
import respx

from pipeline.config import Settings
from pipeline.models import Change, FetchStatus
from pipeline.run import SetupError, choose_sources, exit_code, format_summary, run_fetch
from pipeline.tests.conftest import make_result, make_source

SOURCES_YAML = """
sources:
  - key: good
    name: Good page
    url: https://example.edu.pk/good
    program_key: good
    country: PK
    opportunity_type: admission
    location: pakistan
  - key: broken
    name: Broken page
    url: https://example.edu.pk/broken
    program_key: broken
    country: PK
    opportunity_type: admission
    location: pakistan
  - key: later
    name: Not yet
    url: https://example.edu.pk/later
    program_key: later
    country: PK
    opportunity_type: admission
    location: pakistan
    active: false
"""
PAGE_HTML = (
    "<html><body><p>Applications for the MS programme are open."
    " Deadline: 15 January 2027.</p></body></html>"
)


@pytest.fixture
def local_settings(settings: Settings, tmp_path: Path) -> Settings:
    sources_file = tmp_path / "sources.yaml"
    sources_file.write_text(SOURCES_YAML, encoding="utf-8")
    return settings.model_copy(
        update={"sources_file": sources_file, "data_dir": tmp_path / "data", "database_url": None}
    )


def test_missing_database_url_is_a_clear_error(local_settings: Settings) -> None:
    with pytest.raises(SetupError, match="use --local or set DATABASE_URL"):
        run_fetch(local_settings, local=False, only=None)


def test_local_run_end_to_end(mock_http: respx.MockRouter, local_settings: Settings) -> None:
    mock_http.get("https://example.edu.pk/robots.txt").respond(404)
    mock_http.get("https://example.edu.pk/good").respond(200, html=PAGE_HTML)
    mock_http.get("https://example.edu.pk/broken").respond(404)

    first = run_fetch(local_settings, local=True, only=None)
    second = run_fetch(local_settings, local=True, only=None)

    assert [r.source.key for r in first] == ["good", "broken"]  # "later" is inactive
    assert [(r.status, r.change) for r in first] == [
        (FetchStatus.OK, Change.NEW),
        (FetchStatus.FAILED, None),
    ]
    assert second[0].change is Change.UNCHANGED
    assert len(list((local_settings.data_dir / "snapshots" / "good").iterdir())) == 1


def test_choose_sources() -> None:
    sources = [make_source(key="a"), make_source(key="b", active=False)]
    assert [s.key for s in choose_sources(sources, None)] == ["a"]
    assert [s.key for s in choose_sources(sources, "b")] == ["b"]  # --only works on inactive
    with pytest.raises(SetupError, match="No source with key 'zzz'"):
        choose_sources(sources, "zzz")


def test_exit_code() -> None:
    ok = make_result("a")
    failed = make_result("b", status=FetchStatus.FAILED)
    assert exit_code([ok, failed]) == 0
    assert exit_code([failed, failed]) == 1
    assert exit_code([]) == 0


def test_format_summary() -> None:
    results = [
        make_result("nust-pg", "x" * 120, change=Change.NEW),
        make_result("broken", status=FetchStatus.FAILED, http_status=404, error="HTTP 404"),
        make_result("long", status=FetchStatus.FAILED, error="boom " * 40),
    ]
    lines = format_summary(results).splitlines()

    assert lines[0].split() == ["key", "status", "change", "http", "chars", "error"]
    assert lines[2].split() == ["nust-pg", "ok", "new", "200", "120"]
    assert lines[3].split() == ["broken", "failed", "-", "404", "-", "HTTP", "404"]
    assert lines[4].endswith("...")
    assert lines[-1] == "3 sources: 1 ok, 2 failed"
