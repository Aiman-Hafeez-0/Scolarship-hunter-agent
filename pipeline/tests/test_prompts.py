"""Tests for pipeline.prompts."""

from datetime import date

from pipeline.prompts import extract_system_prompt, extract_user_message
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
