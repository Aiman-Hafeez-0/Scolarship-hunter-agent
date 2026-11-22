"""Tests for pipeline.prompts."""

from datetime import date

from pipeline.prompts import EXTRACT_VERSION, extract_system_prompt, extract_user_message
from pipeline.tests.conftest import make_source


def test_system_prompt_has_rules_and_hints() -> None:
    source = make_source(name="KAUST admissions timeline", opportunity_type="scholarship",
                         location="abroad")  # fmt: skip
    prompt = extract_system_prompt(source, today=date(2026, 11, 14))

    assert "Never use outside knowledge" in prompt
    assert "word for word" in prompt
    assert "Source: KAUST admissions timeline" in prompt
    assert "Expected kind of opportunity: scholarship" in prompt
    assert "Location: abroad" in prompt
    assert "Today's date: 2026-11-14" in prompt
    assert "{{" not in prompt  # every placeholder was filled


def test_user_message_wraps_page() -> None:
    assert extract_user_message("Deadline 1 May") == "<page>\nDeadline 1 May\n</page>"


def test_user_message_includes_feedback() -> None:
    message = extract_user_message("Deadline 1 May", feedback="deadlines.0.evidence missing")
    assert message.startswith("<page>\nDeadline 1 May\n</page>")
    assert "deadlines.0.evidence missing" in message


def test_extract_v4_rules() -> None:
    prompt = extract_system_prompt(make_source(), today=date(2026, 11, 16))

    assert EXTRACT_VERSION == "extract_v4"
    # v4: a test calendar row gives two separate deadlines (registration and test date).
    assert "every row has TWO deadlines" in prompt and "Never merge them" in prompt
    assert "share that same evidence" in prompt and 'A "*" before a date' in prompt
    assert "more than 30 days before today's date" in prompt
    assert "Tables:" in prompt and "the whole row" in prompt
    assert "named in a single sentence" in prompt
    assert "degree_levels_evidence" in prompt
    assert "intake_evidence" in prompt and "Spring 2027" in prompt
    assert "MS and PhD" in prompt
    assert "excerpt" in prompt  # the model knows it may see only the relevant lines
