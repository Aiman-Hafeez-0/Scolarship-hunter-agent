"""Tests for pipeline.run (the command line)."""

import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
import respx

from pipeline import db
from pipeline.config import Settings
from pipeline.graph import SourceOutcome
from pipeline.models import Change, FetchStatus
from pipeline.nodes.extract import extract_page
from pipeline.nodes.validate import ValidatedScholarship, validate_extraction
from pipeline.profile import ProfileError
from pipeline.run import (
    SetupError,
    choose_sources,
    exit_code,
    extraction_report,
    format_saved_items,
    format_summary,
    format_weekly_summary,
    main,
    run_extract,
    run_fetch,
    run_match,
    run_profile,
    run_weekly,
)
from pipeline.schemas import DeadlineItem, PageExtraction, ScholarshipExtraction
from pipeline.tests.conftest import FakeProvider, make_result, make_source

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


# ---------- extract command ----------


def test_extract_requires_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["extract", "--only", "kaust-timeline"])
    assert info.value.code == 2
    assert "add --dry-run" in capsys.readouterr().err


def test_extract_needs_database_url(local_settings: Settings) -> None:
    with pytest.raises(SetupError, match="DATABASE_URL"):
        run_extract(local_settings, only="good", provider=FakeProvider())


def test_extraction_report() -> None:
    page = PageExtraction(
        scholarships=[
            ScholarshipExtraction(
                name="KAUST Fellowship", opportunity_type="scholarship", location="abroad"
            )
        ]
    )
    result = extract_page("page text", make_source(), FakeProvider(page))
    snapshot = db.Snapshot(
        id=uuid.UUID(int=1), fetched_at=datetime(2026, 11, 14, 9, 0, tzinfo=UTC), text="page text"
    )

    report = extraction_report(result, snapshot)

    assert report["source"] == "example"
    assert report["snapshot_fetched_at"] == "2026-11-14T09:00:00+00:00"
    assert report["page_chars"] == 9
    assert report["extract_version"] == "extract_v2"
    assert report["model"] == "fake-model"
    assert (report["tokens_in"], report["tokens_out"]) == (100, 20)
    assert report["needs_review"] is False
    assert report["extraction"]["scholarships"][0]["name"] == "KAUST Fellowship"
    json.dumps(report)  # everything in the report is plain JSON


# ---------- weekly command ----------


def _outcome(key: str, **fetch: Any) -> SourceOutcome:
    fetched = make_result(key, **fetch)
    return SourceOutcome(source=fetched.source, fetched=fetched)


def test_weekly_needs_database_url(local_settings: Settings) -> None:
    with pytest.raises(SetupError, match="DATABASE_URL"):
        run_weekly(local_settings, only=None, force=False, provider=FakeProvider())


def test_weekly_summary_and_saved_items() -> None:
    today = date(2026, 11, 15)
    item = ScholarshipExtraction(
        name="MS Admission",
        opportunity_type="admission",
        location="pakistan",
        status="open",
        deadlines=[
            DeadlineItem(label="Round 1", deadline_date=date(2026, 10, 1), evidence="1 Oct 2026"),
            DeadlineItem(label="Round 2", deadline_date=date(2027, 1, 15), evidence="15 Jan 2027",
                         time_and_timezone="23:59 PKT"),
        ],
    )  # fmt: skip
    flagged = ValidatedScholarship(item=item)
    flagged.flag("requirements[0] quote not found on page (score 40); value dropped")

    saved = _outcome("nust-pg", change=Change.CHANGED)
    saved.extracted = extract_page("text", saved.source, FakeProvider(PageExtraction()))
    saved.items, saved.saved = [flagged], 1
    unchanged = _outcome("kaust", change=Change.UNCHANGED)
    broken = _outcome("broken", status=FetchStatus.FAILED, error="HTTP 404")

    summary = format_weekly_summary([saved, unchanged, broken])
    lines = summary.splitlines()
    assert lines[0].split() == ["key", "fetch", "change", "items", "review", "tokens", "note"]
    assert lines[2].split() == ["nust-pg", "ok", "changed", "1", "1", "120"]
    assert lines[3].split() == ["kaust", "ok", "unchanged", "-", "-", "-"]
    assert lines[4].split() == ["broken", "failed", "-", "-", "-", "-", "HTTP", "404"]
    assert "Sources changed:      1" in summary
    assert "Items saved:          1" in summary
    assert "Items needing review: 1" in summary
    assert "Tokens used:          120 (100 in, 20 out)" in summary

    assert format_saved_items([saved, unchanged], today).splitlines() == [
        "[nust-pg] MS Admission (open)  [NEEDS REVIEW]",
        "    2026-10-01  Round 1  (passed)",
        "    2027-01-15  Round 2  (23:59 PKT)",
        "    review: requirements[0] quote not found on page (score 40); value dropped",
    ]
    assert format_saved_items([unchanged]) == "No items saved."


# ---------- profile and match commands ----------


def test_profile_command_needs_profile_yaml(local_settings: Settings, tmp_path: Path) -> None:
    missing = local_settings.model_copy(update={"profile_file": tmp_path / "profile.yaml"})
    with pytest.raises(ProfileError, match="copy profile.example.yaml"):
        run_profile(missing)


def test_match_command_needs_database_url(local_settings: Settings) -> None:
    with pytest.raises(SetupError, match="DATABASE_URL"):
        run_match(local_settings)


def test_weekly_passes_provider_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_weekly(settings: Settings, **kwargs: Any) -> tuple[list[SourceOutcome], list[Any]]:
        seen.update(kwargs)
        return [], []

    monkeypatch.setattr("pipeline.run.run_weekly", fake_weekly)
    main(["weekly", "--provider", "groq", "--force"])
    assert seen["provider_choice"] == "groq"
    assert seen["force"] is True
    main(["weekly"])
    assert seen["provider_choice"] == "auto"


def test_weekly_rejects_unknown_provider() -> None:
    with pytest.raises(SystemExit):
        main(["weekly", "--provider", "openai"])


def test_database_error_hides_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(settings: Settings, **kwargs: Any) -> Any:
        raise psycopg.OperationalError(
            'missing "=" after "postgresql://me:s3cret@db.example.com/x" in connection info'
        )

    monkeypatch.setattr("pipeline.run.run_match", broken)
    assert main(["match"]) == 2
    err = capsys.readouterr().err
    assert "s3cret" not in err
    assert "postgresql://me:***@db.example.com/x" in err


# ---------- extract --provider, validation in the report, long errors ----------


def test_extract_passes_provider_choice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake_extract(settings: Settings, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"needs_review": False}

    monkeypatch.setattr("pipeline.run.run_extract", fake_extract)
    assert main(["extract", "--only", "kaust-timeline", "--dry-run", "--provider", "groq"]) == 0
    assert seen == {"only": "kaust-timeline", "provider_choice": "groq"}


def test_extraction_report_includes_validation_and_sizes() -> None:
    text = "MS Admissions Spring 2027. Apply by 15 January 2027."
    item = ScholarshipExtraction(
        name="MS Admission", opportunity_type="admission", location="pakistan", status="open",
        intake="Spring 2027", intake_evidence="MS Admissions Spring 2027.",
        deadlines=[DeadlineItem(label="Apply", deadline_date=date(2027, 1, 15),
                                evidence="Apply by 15 January 2027.")],
    )  # fmt: skip
    result = extract_page(text, make_source(), FakeProvider(PageExtraction(scholarships=[item])))
    snapshot = db.Snapshot(uuid.UUID(int=1), datetime(2026, 11, 14, tzinfo=UTC), text)

    report = extraction_report(result, snapshot, validate_extraction(
        result.extraction, text, date(2026, 11, 15)))  # fmt: skip

    sizes = (report["page_chars"], report["sent_chars"], report["parts"])
    assert sizes == (len(text), len(text), 1)
    [after] = report["after_validation"]
    assert after["status"] == "open"
    assert after["intake"] == "Spring 2027"
    assert after["deadlines"] == ["2027-01-15"]
    assert after["needs_review"] is False


def test_summary_shows_150_characters_of_an_error() -> None:
    error = "LLM call failed: openai/gpt-oss-120b: HTTP 400 json_validate_failed: " + "x" * 200
    outcome = _outcome("polimi-merit", status=FetchStatus.FAILED, error=error)
    note = format_weekly_summary([outcome]).splitlines()[2]

    assert "HTTP 400 json_validate_failed" in note
    shown = note[note.index("LLM call failed"):]
    assert len(shown) == 150 and shown.endswith("...")
