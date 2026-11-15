"""Tests for pipeline.nodes.extract, using a fake LLM provider (no API calls)."""

from typing import Any

from pipeline.llm.provider import InvalidOutput, ProviderError
from pipeline.nodes.extract import extract_page, merge_by_name, split_text
from pipeline.schemas import DeadlineItem, PageExtraction, ScholarshipExtraction
from pipeline.tests.conftest import FakeProvider, make_source


def scholarship(name: str, **fields: Any) -> ScholarshipExtraction:
    return ScholarshipExtraction(
        name=name, opportunity_type="scholarship", location="abroad", **fields
    )


def deadline(label: str) -> DeadlineItem:
    return DeadlineItem(label=label, evidence=f"{label}: 1 May 2027")


# ---------- extract_page ----------


def test_single_page() -> None:
    page = PageExtraction(scholarships=[scholarship("KAUST Fellowship")])
    provider = FakeProvider(page)

    result = extract_page("short page", make_source(), provider)

    assert result.extraction == page
    assert (result.tokens_in, result.tokens_out) == (100, 20)
    assert result.model == "fake-model"
    assert not result.needs_review and result.errors == []
    assert provider.calls == [("short page", None)]


def test_invalid_json_retried_once_with_error() -> None:
    page = PageExtraction(scholarships=[scholarship("KAUST Fellowship")])
    provider = FakeProvider(InvalidOutput("deadlines.0.evidence: Field required", 300, 40), page)

    result = extract_page("page", make_source(), provider)

    assert result.extraction == page
    assert not result.needs_review
    assert provider.calls[1] == ("page", "deadlines.0.evidence: Field required")
    assert (result.tokens_in, result.tokens_out) == (400, 60)  # both calls are counted


def test_invalid_json_twice_needs_review() -> None:
    provider = FakeProvider(InvalidOutput("bad", 1, 1), InvalidOutput("still bad", 1, 1))

    result = extract_page("page", make_source(), provider)

    assert result.needs_review
    assert result.extraction.scholarships == []
    assert result.errors == ["invalid JSON twice: still bad"]
    assert len(provider.calls) == 2


def test_provider_failure_needs_review() -> None:
    result = extract_page("page", make_source(), FakeProvider(ProviderError("quota used up")))

    assert result.needs_review
    assert result.errors == ["LLM call failed: quota used up"]


def test_unexpected_error_never_crashes() -> None:
    result = extract_page("page", make_source(), FakeProvider(RuntimeError("boom")))

    assert result.needs_review
    assert result.errors == ["RuntimeError: boom"]


def test_long_page_is_split_and_merged() -> None:
    part = "x" * 50_000
    text = f"Programme A\n{part}\n\nDeadlines\n{part}"
    provider = FakeProvider(
        PageExtraction(scholarships=[scholarship("Programme A", deadlines=[deadline("Round 1")])]),
        PageExtraction(scholarships=[scholarship("programme  a", deadlines=[deadline("Round 2")]),
                                     scholarship("Programme B")]),
    )  # fmt: skip

    result = extract_page(text, make_source(), provider)

    assert len(provider.calls) == 2
    assert provider.calls[0][0].startswith("Programme A")
    assert provider.calls[1][0].startswith("Deadlines")
    names = [s.name for s in result.extraction.scholarships]
    assert names == ["Programme A", "Programme B"]
    labels = [d.label for d in result.extraction.scholarships[0].deadlines]
    assert labels == ["Round 1", "Round 2"]
    assert (result.tokens_in, result.tokens_out) == (200, 40)


def test_one_failed_part_keeps_the_others() -> None:
    part = "x" * 50_000
    text = f"Part one\n{part}\n\nPart two\n{part}"
    provider = FakeProvider(
        ProviderError("timeout"), PageExtraction(scholarships=[scholarship("Programme B")])
    )

    result = extract_page(text, make_source(), provider)

    assert result.needs_review
    assert result.errors == ["part 1/2: LLM call failed: timeout"]
    assert [s.name for s in result.extraction.scholarships] == ["Programme B"]


# ---------- split_text ----------


def test_short_text_is_one_chunk() -> None:
    assert split_text("hello", max_chars=100) == ["hello"]


def test_split_prefers_headings() -> None:
    sentence = "This is a long sentence about the programme and its rules. "
    body = sentence * 3
    text = f"Overview\n{body}\n\nEligibility\n{body}\n\nDeadlines\n{body}"

    chunks = split_text(text, max_chars=len(body) * 2)

    assert [c.splitlines()[0] for c in chunks] == ["Overview", "Eligibility", "Deadlines"]
    assert all(len(c) <= len(body) * 2 for c in chunks)


def test_split_never_exceeds_limit_without_headings() -> None:
    text = "\n".join(["word " * 30] * 200) + "y" * 5000  # long lines and one huge line
    chunks = split_text(text, max_chars=1000)

    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


# ---------- merge_by_name ----------


def test_merge_fills_unknowns_and_combines_lists() -> None:
    first = scholarship("KAUST Fellowship", entry_tests=["GRE"], funding_type="unknown")
    second = scholarship(
        " kaust   fellowship ", entry_tests=["GRE", "IELTS"], funding_type="full", provider="KAUST"
    )

    [merged] = merge_by_name([first, second])

    assert merged.name == "KAUST Fellowship"  # first name kept
    assert merged.entry_tests == ["GRE", "IELTS"]
    assert merged.funding_type == "full"
    assert merged.provider == "KAUST"


def test_merge_keeps_first_known_value() -> None:
    first = scholarship("A", funding_type="partial")
    second = scholarship("A", funding_type="full")
    assert merge_by_name([first, second])[0].funding_type == "partial"
