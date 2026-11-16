"""Tests for pipeline.focus: the shorter page text sent to the LLM."""

import pytest

from pipeline.focus import focus_text, is_relevant

PAGE = """Home
About
Contact
Admissions

Our university was founded long ago by people with a vision.
Its founders wanted a place for learning and for friendship.
It has a beautiful campus with many trees and a library.
The library holds old books and quiet rooms for reading.
The campus cafeteria serves food every single day of the week.
Students enjoy sports, clubs and many cultural events each term.

MS Admissions Spring 2027
Applications are open for all MS programmes this semester.
The last date to apply is 15 January 2027.
Interviews are held on campus in the main hall.
Results are shared by email with every candidate.
The weather in the city is mild most of the year.
Parking is available near the north gate for visitors.
The sports complex opens at seven in the morning daily.

Copyright 2026 Example University. All rights reserved.
Privacy policy and cookie settings for this website."""


def test_keeps_relevant_lines_with_context_and_drops_the_rest() -> None:
    focused = focus_text(PAGE)
    lines = focused.splitlines()

    assert "The last date to apply is 15 January 2027." in lines
    assert "MS Admissions Spring 2027" in lines
    assert "Results are shared by email with every candidate." in lines  # 2 lines of context
    assert "The weather in the city is mild most of the year." not in focused  # too far away
    assert "beautiful campus" not in focused
    assert "Copyright" not in focused and "cookie" not in focused  # footer
    assert "Home" not in lines and "Contact" not in lines  # menu: a run of short lines
    assert len(focused) < 0.6 * len(PAGE)


def test_lines_are_copied_unchanged() -> None:
    page = "Intro text that is long enough to keep.\n   Deadline:   1 March 2027   \n"
    assert "Deadline:   1 March 2027" in focus_text(page)  # only the ends are trimmed


def test_paragraph_breaks_and_gaps_become_blank_lines() -> None:
    page = "\n".join([
        "Scholarship for MS students in computing.", "", "Second paragraph is here now.",
        "a", "b", "c", "d", "e", "f",
        "Tuition is fully covered for all PhD students.",
    ])  # fmt: skip
    blocks = focus_text(page).split("\n\n")
    assert blocks[0] == "Scholarship for MS students in computing."
    assert blocks[1].startswith("Second paragraph")
    assert blocks[-1].endswith("Tuition is fully covered for all PhD students.")


def test_nothing_relevant_gives_empty_text() -> None:
    assert focus_text("Welcome to our website.\nWe like trees and flowers.") == ""


@pytest.mark.parametrize(
    "line",
    [
        "The application deadline is near.",
        "Last date: 01/02/2027",
        "Scadenza del bando per le borse di studio",
        "Son başvuru tarihi Şubat sonudur",
        "Results on 3 May at noon",
        "Classes resume in 2027",
        "HAT and GAT scores are accepted",
        "PhD candidates welcome",
        "Fees and funding",
        "IELTS 6.5 or MOI letter",
    ],
)
def test_relevant_lines(line: str) -> None:
    assert is_relevant(line)


@pytest.mark.parametrize(
    "line",
    ["You may visit the library.", "We hate rain.", "Football is fun", "Our campus is green."],
)
def test_ordinary_lines_are_not_relevant(line: str) -> None:
    assert not is_relevant(line)
