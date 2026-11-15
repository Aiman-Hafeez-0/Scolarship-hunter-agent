"""Tests for pipeline.profile."""

from datetime import date
from pathlib import Path

import pytest

from pipeline.profile import ProfileError, load_profile

EXAMPLE = Path(__file__).resolve().parents[2] / "profile.example.yaml"


def test_example_profile_is_valid() -> None:
    profile = load_profile(EXAMPLE)
    assert profile.email == "you@example.com"
    assert profile.date_of_birth == date(2001, 5, 10)
    assert profile.english_test is None  # empty value
    assert profile.max_application_fee_eur is None  # empty = no cap
    assert profile.reminder_days == [30, 14, 7, 2]


def test_missing_file_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match=r"copy profile\.example\.yaml to profile\.yaml"):
        load_profile(tmp_path / "profile.yaml")


@pytest.mark.parametrize(
    ("yaml_text", "problem"),
    [
        ("full_name: A\nemail: not-an-email\n", "email"),
        ("full_name: A\nemail: a@b.co\ncgpa: 4.5\ncgpa_scale: 4\n", "higher than cgpa_scale"),
        ("full_name: A\nemail: a@b.co\nfavourite_colour: blue\n", "favourite_colour"),
    ],
)
def test_bad_values_are_reported(tmp_path: Path, yaml_text: str, problem: str) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ProfileError, match=problem):
        load_profile(path)
