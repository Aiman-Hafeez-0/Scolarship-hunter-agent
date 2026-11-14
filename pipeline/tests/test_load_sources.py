"""Tests for pipeline.models.Source and pipeline.nodes.load_sources."""

from pathlib import Path

import pytest

from pipeline.models import Location, OpportunityType, SourceType
from pipeline.nodes.load_sources import SourcesError, load_sources

REAL_SOURCES = Path(__file__).resolve().parents[1] / "sources.yaml"

GOOD_ENTRY = """
  - key: {key}
    name: Example
    url: {url}
    program_key: example
    country: PK
    opportunity_type: admission
    location: pakistan
"""


def write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n" + body, encoding="utf-8")
    return path


def entry(key: str = "example", url: str = "https://example.edu.pk/") -> str:
    return GOOD_ENTRY.format(key=key, url=url)


# ---------- The real sources.yaml ----------


def test_real_sources_load() -> None:
    sources = load_sources(REAL_SOURCES, include_inactive=True)
    assert len(sources) == 23


def test_real_sources_keys_and_urls_are_unique() -> None:
    sources = load_sources(REAL_SOURCES, include_inactive=True)
    keys = [s.key for s in sources]
    urls = [s.url for s in sources]
    assert len(keys) == len(set(keys))
    assert len(urls) == len(set(urls))


def test_real_sources_are_all_https() -> None:
    for source in load_sources(REAL_SOURCES, include_inactive=True):
        assert source.url.startswith("https://"), source.key


def test_real_sources_inactive_filtered_by_default() -> None:
    every = load_sources(REAL_SOURCES, include_inactive=True)
    active = load_sources(REAL_SOURCES)

    assert all(s.active for s in active)
    assert {s.key for s in every if not s.active} == {
        "unibo-study-grants",
        "ergo-dsu",
        "esu-padova-dsu",
        "qau",
        "sukkur-iba",
        "hec-scholarships",
    }
    assert len(active) == len(every) - 6


# ---------- Defaults and validation ----------


def test_defaults_are_applied(tmp_path: Path) -> None:
    [source] = load_sources(write_yaml(tmp_path, entry()))

    assert source.type is SourceType.OFFICIAL
    assert source.opportunity_type is OpportunityType.ADMISSION
    assert source.location is Location.PAKISTAN
    assert source.render_js is False
    assert source.is_pdf is False
    assert source.active is True
    assert source.note is None


def test_inactive_kept_when_asked(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, entry() + "    active: false\n")
    assert load_sources(path) == []
    assert len(load_sources(path, include_inactive=True)) == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (entry(url="http://example.edu.pk/"), "https://"),
        (entry(key="Bad Key"), "key"),
        (entry().replace("location: pakistan", "location: mars"), "location"),
        (entry().replace("    country: PK\n", ""), "country"),
        (entry() + "    activ: false\n", "activ"),
    ],
    ids=["http-url", "bad-key", "bad-location", "missing-field", "unknown-field"],
)
def test_bad_entry_raises(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(SourcesError, match=message):
        load_sources(write_yaml(tmp_path, body))


def test_bad_inactive_entry_still_raises(tmp_path: Path) -> None:
    body = entry(url="http://example.edu.pk/") + "    active: false\n"
    with pytest.raises(SourcesError, match="https://"):
        load_sources(write_yaml(tmp_path, body))


def test_duplicate_key_raises(tmp_path: Path) -> None:
    body = entry(url="https://a.edu.pk/") + entry(url="https://b.edu.pk/")
    with pytest.raises(SourcesError, match="Duplicate source key 'example'"):
        load_sources(write_yaml(tmp_path, body))


def test_duplicate_url_raises(tmp_path: Path) -> None:
    body = entry(key="one") + entry(key="two")
    with pytest.raises(SourcesError, match="Duplicate source url"):
        load_sources(write_yaml(tmp_path, body))


def test_missing_sources_list_raises(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text("hello: world\n", encoding="utf-8")
    with pytest.raises(SourcesError, match="'sources:' list"):
        load_sources(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SourcesError, match="Could not read"):
        load_sources(tmp_path / "nope.yaml")
