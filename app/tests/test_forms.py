"""Tests for app.forms: reading the profile and source forms."""

import yaml

from app.forms import (
    parse_profile_form,
    parse_reminder_days,
    parse_source_form,
    profile_form_values,
    source_yaml,
)
from app.tests.conftest import profile
from pipeline.models import Source


def test_parse_reminder_days() -> None:
    assert parse_reminder_days("7, 30\n14, 7") == ([30, 14, 7], [])
    assert parse_reminder_days("") == ([], [])  # no reminders at all is allowed
    assert parse_reminder_days("0")[1] and parse_reminder_days("400")[1]
    assert parse_reminder_days("-3")[1] and parse_reminder_days("2.5")[1]


def test_empty_boxes_become_not_known() -> None:
    saved, paused, problems = parse_profile_form(
        {"full_name": "A", "email": "a@example.com", "cgpa": " ", "date_of_birth": ""}
    )
    assert problems == [] and saved is not None
    assert saved.cgpa is None and saved.date_of_birth is None
    assert saved.cgpa_scale == 4.0  # the model's default
    assert saved.moi_letter is False and saved.prefer_no_fee is False  # unticked boxes
    assert paused is False


def test_profile_problems_are_readable() -> None:
    saved, _, problems = parse_profile_form({"full_name": "", "email": "not-an-email"})
    assert saved is None
    assert any(p.startswith("full_name:") for p in problems)
    assert any(p.startswith("email:") for p in problems)


def test_form_values_round_trip() -> None:
    original = profile(cgpa=3.5, date_of_birth="2001-05-10", max_application_fee_eur=30,
                       reminder_days=[14, 3], moi_letter=True)  # fmt: skip
    values = profile_form_values(original, paused=True)
    form = {key: ("1" if value is True else value) for key, value in values.items()
            if value is not False}  # how a browser sends it back  # fmt: skip
    saved, paused, problems = parse_profile_form(form)
    assert problems == [] and paused is True
    assert saved == original


def test_parse_source_form() -> None:
    source, problems = parse_source_form({
        "url": "https://www.example.edu/a", "name": "A", "key": "a-ms", "country": "DE",
        "opportunity_type": "admission", "location": "abroad", "type": "lead",
        "note": "  ", "is_pdf": "1",
    })  # fmt: skip
    assert problems == [] and source is not None
    assert (source.program_key, source.is_pdf, source.note) == ("a-ms", True, None)
    assert source.type == "lead"


def test_parse_source_form_lists_every_problem() -> None:
    source, problems = parse_source_form({"url": "ftp://x", "key": "Bad Key"})
    assert source is None
    text = "\n".join(problems)
    assert "url must start with https://" in text and "key:" in text and "name:" in text


def test_source_yaml_pastes_back_into_sources_yaml() -> None:
    source = Source(key="a-ms", name="Ä MS", url="https://www.example.edu/a", program_key="a-ms",
                    country="DE", opportunity_type="admission", location="abroad")  # fmt: skip
    text = source_yaml(source)
    assert text.startswith("  - key: a-ms\n    name: Ä MS\n    url: https://")
    assert "active" not in text and "render_js" not in text  # defaults are left out
    [loaded] = yaml.safe_load("sources:\n" + text)["sources"]
    assert Source.model_validate(loaded) == source
