"""Tests for the Opportunities, detail and Deadlines pages, and the .ics download."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.queries import DeadlineDetail, Quote, UpcomingDeadline
from app.tests.conftest import SID, TODAY, FakeStore, detail, row

DAY = TODAY + timedelta(days=20)


@pytest.fixture
def duplicates(store: FakeStore) -> FakeStore:
    store.rows = [
        row(id="a", name="HEC Entry Test", opportunity_type="entry_test", location="pakistan",
            funding="unknown", next_deadline=DAY, score=None, match_status=None,
            source_name="HEC tests", source_url="https://hec.gov.pk/tests"),
        row(id="b", name="HEC Entry Test Calendar", opportunity_type="entry_test",
            location="pakistan", funding="unknown", next_deadline=DAY, score=None,
            match_status=None, source_name="HEC calendar",
            source_url="https://hec.gov.pk/calendar"),
        row(id="c", name="NUST MS", opportunity_type="admission", location="pakistan",
            funding="unknown", next_deadline=DAY + timedelta(days=3)),
        row(id="d", name="Stipendium Hungaricum", funding="full"),
    ]  # fmt: skip
    return store


# ---------- Opportunities ----------


def test_duplicates_show_once_with_two_sources(
    logged_in: TestClient, duplicates: FakeStore
) -> None:
    page = logged_in.get("/scholarships").text
    names = [r["name"] for r in logged_in.get("/api/v1/scholarships").json()]

    assert names.count("HEC Entry Test") == 1 and "HEC Entry Test Calendar" not in names
    assert "3 results" in page
    assert "2 sources" in page and "Found on HEC tests and HEC calendar" in page
    hec = next(r for r in logged_in.get("/api/v1/scholarships").json() if r["id"] == "a")
    assert [s["url"] for s in hec["sources"]] == ["https://hec.gov.pk/tests",
                                                  "https://hec.gov.pk/calendar"]  # fmt: skip


def test_tests_and_admissions_show_a_dash_for_funding(
    logged_in: TestClient, duplicates: FakeStore
) -> None:
    page = logged_in.get("/scholarships").text
    hec_row = page.split('data-href="/scholarships/a"')[1].split("</tr>")[0]
    hungary_row = page.split('data-href="/scholarships/d"')[1].split("</tr>")[0]
    assert ">—</td>" in hec_row and "Unknown" not in hec_row
    assert ">Full</td>" in hungary_row


def test_funding_chips_count_only_scholarships(
    logged_in: TestClient, duplicates: FakeStore
) -> None:
    api = logged_in.get("/api/v1/scholarships?funding=unknown").json()
    assert api == []  # admissions and tests have no funding, so they never match "unknown"
    chips = logged_in.get("/scholarships").text.split('id="chips"')[1]
    funding = chips.split("<legend")[4]
    assert "All<span" in funding and ">1</span>" in funding  # only Stipendium Hungaricum


def test_chips_show_counts_and_the_choice(logged_in: TestClient, duplicates: FakeStore) -> None:
    page = logged_in.get("/scholarships?type=entry_test").text
    chips = page.split('id="chips"')[1].split("</form>")[0]
    assert 'name="type" value="entry_test" checked' in chips
    # Type counts ignore the type filter itself: 1 scholarship, 1 admission, 1 entry test.
    type_group = chips.split("<legend")[1].split("</fieldset>")[0]
    assert type_group.count('<span class="tabular-nums opacity-80">1</span>') == 3
    # Location counts follow the chosen type: only the entry test, in Pakistan.
    location_group = chips.split("<legend")[2].split("</fieldset>")[0]
    assert "Pakistan<span" in location_group


def test_htmx_answer_brings_fresh_chips_out_of_band(
    logged_in: TestClient, duplicates: FakeStore
) -> None:
    response = logged_in.get("/scholarships/table?location=pakistan",
                             headers={"HX-Request": "true"})  # fmt: skip
    assert '<div id="chips" hx-swap-oob="true">' in response.text
    assert "<html" not in response.text


def test_no_date_yet_group_at_the_bottom(logged_in: TestClient, store: FakeStore) -> None:
    store.rows = [row(id="a", name="Dated", next_deadline=DAY),
                  row(id="b", name="Undated", next_deadline=None)]  # fmt: skip
    page = logged_in.get("/scholarships").text
    group = page.split("No date yet <span")[1]
    assert page.index("Dated") < page.index("No date yet <span")
    assert "Undated" in group and "Dated</a>" not in group
    assert '<details class="group mt-6">' in page  # folded while dated items exist


def test_pagination_25_per_page(logged_in: TestClient, store: FakeStore) -> None:
    store.rows = [row(id=f"r{i}", name=f"Item {i:02}", next_deadline=TODAY + timedelta(days=i))
                  for i in range(1, 31)] + [row(id="u", name="Undated", next_deadline=None)]
    first = logged_in.get("/scholarships").text
    assert "31 results" in first and "showing 1–25 of 30 with a date" in first
    assert "Item 25" in first and "Item 26" not in first and "Undated" not in first
    assert 'aria-label="Pages"' in first and 'href="/scholarships?page=2"' in first

    second = logged_in.get("/scholarships?page=2").text
    assert "Item 26" in second and "Item 25" not in second
    assert "Undated" in second  # the "No date yet" group is on the last page
    response = logged_in.get("/scholarships/table?page=2", headers={"HX-Request": "true"})
    assert response.headers["HX-Push-Url"] == "/scholarships?page=2"
    assert "showing 26–30" in logged_in.get("/scholarships?page=99").text  # past the end


def test_view_toggle_is_on_the_page(logged_in: TestClient) -> None:
    page = logged_in.get("/scholarships").text
    assert 'data-view-toggle="table"' in page and 'data-view-toggle="cards"' in page
    assert 'id="opps"' in page and 'class="opp-table' in page and 'class="opp-cards' in page


def test_rows_are_clickable(logged_in: TestClient) -> None:
    page = logged_in.get("/scholarships").text
    assert f'data-href="/scholarships/{SID}"' in page


def test_empty_search_explains_why(logged_in: TestClient) -> None:
    page = logged_in.get("/scholarships?q=zzz").text
    assert "Nothing matches these filters." in page and "“zzz”" in page
    assert 'href="/scholarships"' in page  # clear filters


# ---------- Detail ----------


def test_detail_header_sections_and_big_countdown(logged_in: TestClient, store: FakeStore) -> None:
    store.details[SID] = detail(location="abroad", country="Hungary")
    page = logged_in.get(f"/scholarships/{SID}").text
    assert '<h1 id="page-title"' in page and "Stipendium Hungaricum</h1>" in page
    assert 'data-badge="type"' in page and 'data-badge="location"' in page
    assert "Abroad · Hungary" in page
    big = page.split('data-badge="big-countdown"')[1].split("</div>")[0]
    assert ">57</p>" in big and "days left" in big  # 15 Jan 2027 is 57 days after 19 Nov
    assert "Open official page" in page and 'href="https://apply.example.org"' in page
    assert "I checked this" in page
    for section in ("deadlines", "eligibility", "funding", "requirements", "evidence", "history"):
        assert f'<section id="{section}"' in page, section


def test_eligibility_checklist(logged_in: TestClient) -> None:
    page = logged_in.get(f"/scholarships/{SID}").text
    assert 'data-outcome="pass"' in page and 'data-outcome="unknown"' in page
    assert "1 of 2 rules pass" in page and "take IELTS or TOEFL" in page


def test_evidence_links_to_the_quote_on_the_page(logged_in: TestClient) -> None:
    page = logged_in.get(f"/scholarships/{SID}").text
    evidence = page.split('<section id="evidence"')[1].split("</section>")[0]
    assert "Full tuition waiver and stipend" in evidence
    assert "https://example.org/sh#:~:text=Full%20tuition%20waiver%20and%20stipend" in evidence


def test_detail_of_a_test_shows_a_dash_for_funding(logged_in: TestClient, store: FakeStore) -> None:
    store.details[SID] = detail(opportunity_type="entry_test")
    page = logged_in.get(f"/scholarships/{SID}").text
    funding = page.split('data-field="funding"')[1].split("</dd>")[0]
    assert "—" in funding and "full" not in funding.lower().split("money")[0]


def test_detail_lists_every_source_of_a_merged_item(
    logged_in: TestClient, duplicates: FakeStore
) -> None:
    duplicates.details["a"] = detail(id="a", name="HEC Entry Test")
    page = logged_in.get("/scholarships/a").text
    assert "2 sources" in page
    assert 'href="https://hec.gov.pk/tests"' in page
    assert 'href="https://hec.gov.pk/calendar"' in page


def test_detail_without_future_deadline(logged_in: TestClient, store: FakeStore) -> None:
    store.details[SID] = detail(deadlines=[DeadlineDetail("Old", TODAY - timedelta(days=3), None,
                                                          False, None)])  # fmt: skip
    page = logged_in.get(f"/scholarships/{SID}").text
    assert "No date yet" in page and "calendar.ics" not in page and "passed" in page


# ---------- .ics download ----------


def test_add_to_calendar_downloads_an_ics_file(logged_in: TestClient) -> None:
    page = logged_in.get(f"/scholarships/{SID}").text
    assert f'href="/scholarships/{SID}/calendar.ics?d=0" download' in page

    response = logged_in.get(f"/scholarships/{SID}/calendar.ics?d=0")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert response.headers["content-disposition"] == (
        'attachment; filename="stipendium-hungaricum-2027-01-15.ics"'
    )
    assert "DTSTART;VALUE=DATE:20270115" in response.text
    assert "SUMMARY:Application deadline: Stipendium Hungaricum" in response.text


def test_ics_for_a_missing_deadline_is_404(logged_in: TestClient, store: FakeStore) -> None:
    assert logged_in.get(f"/scholarships/{SID}/calendar.ics?d=5").status_code == 404
    assert logged_in.get(f"/scholarships/{SID}/calendar.ics?d=-1").status_code == 404
    assert logged_in.get("/scholarships/nope/calendar.ics").status_code == 404
    undated = DeadlineDetail("TBA", None, None, False, Quote("to be announced", 90))
    store.details[SID] = detail(deadlines=[undated])
    assert logged_in.get(f"/scholarships/{SID}/calendar.ics?d=0").status_code == 404


def test_ics_needs_login(client: TestClient) -> None:
    response = client.get(f"/scholarships/{SID}/calendar.ics", follow_redirects=False)
    assert response.status_code == 303


# ---------- Deadlines ----------


def _deadline(days: int, name: str, kind: str = "scholarship", sid: str = SID) -> UpcomingDeadline:
    return UpcomingDeadline(sid, name, "Apply", TODAY + timedelta(days=days), False, kind)


def test_deadlines_page_groups_by_week(logged_in: TestClient, store: FakeStore) -> None:
    # TODAY is Thursday 19 Nov 2026: this week ends on Sunday 22 Nov.
    store.upcoming = [_deadline(1, "Soon"), _deadline(5, "Monday next"),
                      _deadline(30, "Far away", "admission")]  # fmt: skip
    page = logged_in.get("/deadlines").text
    this_week = page.split('id="this-week"')[1].split("</section>")[0]
    next_week = page.split('id="next-week"')[1].split("</section>")[0]
    later = page.split('id="later"')[1].split("</section>")[0]
    assert "Soon" in this_week and "Monday next" in next_week and "Far away" in later
    assert f'href="/scholarships/{SID}"' in this_week  # each entry links to its page


def test_deadlines_filter_by_type(logged_in: TestClient, store: FakeStore) -> None:
    store.upcoming = [_deadline(1, "A scholarship"), _deadline(2, "An admission", "admission")]
    page = logged_in.get("/deadlines?type=admission").text
    assert "An admission" in page and "A scholarship" not in page
    assert 'aria-current="true"' in page.split("An admission")[0]  # the chosen chip
    assert "Admission<span" in page


def test_deadlines_merges_duplicates(logged_in: TestClient, store: FakeStore) -> None:
    store.upcoming = [_deadline(3, "HEC Entry Test", "entry_test", "a"),
                      _deadline(3, "HEC Entry Test Calendar", "entry_test", "b")]  # fmt: skip
    page = logged_in.get("/deadlines").text
    assert "HEC Entry Test Calendar" not in page and "2 sources" in page


def test_deadlines_calendar_view(logged_in: TestClient, store: FakeStore) -> None:
    store.upcoming = [_deadline(12, "December thing")]  # 1 Dec 2026
    page = logged_in.get("/deadlines?view=calendar&month=2026-12").text
    assert "December 2026" in page and "December thing" in page
    assert 'aria-label="Previous month"' in page and "month=2027-01" in page
    assert 'aria-label="01 Dec 2026: 1 deadline"' in page

    november = logged_in.get("/deadlines?view=calendar").text
    assert "November 2026" in november and 'aria-label="Previous month"' not in november
    assert 'aria-current="date"' in november  # today is marked


def test_deadlines_empty_states(logged_in: TestClient, store: FakeStore) -> None:
    store.upcoming = []
    assert "No upcoming deadlines" in logged_in.get("/deadlines").text
    store.upcoming = [_deadline(1, "A scholarship")]
    assert "No upcoming entry test deadlines" in logged_in.get("/deadlines?type=entry_test").text


def test_deadlines_ignores_bad_parameters(logged_in: TestClient, store: FakeStore) -> None:
    store.upcoming = [_deadline(1, "A scholarship")]
    page = logged_in.get("/deadlines?view=nonsense&type=drop&month=xx").text
    assert "A scholarship" in page and 'id="this-week"' in page
