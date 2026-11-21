"""Tests for the dashboard's actions: verify, sources, profile and "Run now"."""

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app, get_store, get_today
from app.tests.conftest import (
    SID,
    TODAY,
    FakeStore,
    csrf_from,
    log_in,
    make_settings,
    use_fake_limiter,
)

TOKEN = "github_pat_SECRET123"
ACTIONS = "https://github.com/owner/scholarship-hunter/actions/workflows/weekly.yml"
DISPATCH_URL = (
    "https://api.github.com/repos/owner/scholarship-hunter/actions/workflows/weekly.yml/dispatches"
)


def _token(client: TestClient, page: str = "/") -> str:
    return csrf_from(client.get(page).text)


def _htmx(client: TestClient, page: str = "/") -> dict[str, str]:
    return {"HX-Request": "true", "X-CSRF-Token": _token(client, page)}


def _client_with(store: FakeStore, **settings: object) -> TestClient:
    app = create_app(make_settings(**settings))
    app.dependency_overrides[get_store] = lambda: store
    use_fake_limiter(app)
    app.dependency_overrides[get_today] = lambda: TODAY
    client = TestClient(app, base_url="http://localhost")
    log_in(client)
    return client


# ---------- verify ----------


def test_detail_page_has_the_verify_button(logged_in: TestClient) -> None:
    page = logged_in.get(f"/scholarships/{SID}").text
    assert f'hx-post="/scholarships/{SID}/verify"' in page
    assert "I checked this" in page and "not checked yet" in page


def test_verify_updates_the_badge_in_place(logged_in: TestClient, store: FakeStore) -> None:
    response = logged_in.post(f"/scholarships/{SID}/verify", headers=_htmx(logged_in))

    assert response.status_code == 200
    assert "<html" not in response.text  # only the badge, for HTMX to swap
    assert 'id="verify-status"' in response.text
    assert "checked " in response.text and "Mark checked again" in response.text
    assert "not checked yet" not in response.text
    assert store.details[SID].verified_at is not None


def test_verify_without_javascript_goes_back_to_the_page(logged_in: TestClient) -> None:
    data = {"csrf_token": _token(logged_in)}
    response = logged_in.post(f"/scholarships/{SID}/verify", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/scholarships/{SID}"


def test_verify_needs_csrf(logged_in: TestClient, store: FakeStore) -> None:
    response = logged_in.post(f"/scholarships/{SID}/verify", headers={"HX-Request": "true"})
    assert response.status_code == 403
    assert store.details[SID].verified_at is None


def test_verify_unknown_item_is_404(logged_in: TestClient) -> None:
    assert logged_in.post("/scholarships/nope/verify", headers=_htmx(logged_in)).status_code == 404


def test_actions_need_login(client: TestClient, store: FakeStore) -> None:
    token = csrf_from(client.get("/login").text)
    for path in (f"/scholarships/{SID}/verify", "/sources", "/sources/nust-pg/active",
                 "/profile", "/runs/dispatch"):  # fmt: skip
        response = client.post(path, data={"csrf_token": token}, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login")
    assert store.details[SID].verified_at is None and not store.added


# ---------- sources ----------


def test_sources_page_lists_status(logged_in: TestClient) -> None:
    page = logged_in.get("/sources").text
    assert "NUST Postgraduate" in page and "https://nust.edu.pk/pg" in page
    assert "503" in page and "HTTP 503" in page  # last status and error
    assert ">3<" in page  # failures in a row
    assert "allowed" in page and "Fall intake" in page
    assert "file of record" not in page  # the database is the source of truth now
    assert logged_in.get("/api/v1/sources").json()[0]["key"] == "nust-pg"


def test_toggle_switches_a_source_off_in_place(logged_in: TestClient, store: FakeStore) -> None:
    response = logged_in.post("/sources/nust-pg/active", data={"active": "0"},
                              headers=_htmx(logged_in, "/sources"))  # fmt: skip
    assert response.status_code == 200
    assert 'id="source-nust-pg"' in response.text and "Paused" in response.text
    assert "<html" not in response.text
    assert store.source_rows[0].active is False


def test_toggle_needs_csrf_and_a_known_key(logged_in: TestClient, store: FakeStore) -> None:
    no_token = logged_in.post("/sources/nust-pg/active", data={"active": "0"})
    assert no_token.status_code == 403 and store.source_rows[0].active is True
    unknown = logged_in.post("/sources/nope/active", data={"active": "0"},
                             headers=_htmx(logged_in, "/sources"))  # fmt: skip
    assert unknown.status_code == 404


NEW_SOURCE = {"url": "https://www.example.edu/admissions", "name": "Example MS",
              "key": "example-ms", "country": "DE", "opportunity_type": "admission",
              "location": "abroad", "type": "official"}  # fmt: skip


def _add(client: TestClient, **changes: str) -> Any:  # the TestClient's Response
    data = {**NEW_SOURCE, **changes, "csrf_token": _token(client, "/sources")}
    return client.post("/sources", data=data)


def test_add_source_checks_robots_then_saves(
    logged_in: TestClient, store: FakeStore, mock_http: respx.MockRouter
) -> None:
    robots = mock_http.get("https://www.example.edu/robots.txt").respond(
        200, text="User-agent: *\nDisallow: /private/\n"
    )
    response = _add(logged_in)

    assert response.status_code == 200
    assert robots.called
    assert robots.calls[0].request.headers["user-agent"].startswith("ScholarshipHunterBot/0.1")
    assert [s.key for s in store.added] == ["example-ms"]
    assert store.added[0].program_key == "example-ms"  # blank program key = the key
    assert "The next run checks it" in response.text
    assert "Optional: the same entry for" in response.text
    assert "- key: example-ms" in response.text
    assert "url: https://www.example.edu/admissions" in response.text


def test_add_source_blocked_by_robots_is_not_saved(
    logged_in: TestClient, store: FakeStore, mock_http: respx.MockRouter
) -> None:
    mock_http.get("https://www.example.edu/robots.txt").respond(
        200, text="User-agent: ScholarshipHunterBot\nDisallow: /\n"
    )
    response = _add(logged_in)
    assert response.status_code == 400
    assert "does not allow ScholarshipHunterBot" in response.text
    assert store.added == []


def test_add_source_unreachable_robots_is_not_saved(
    logged_in: TestClient, store: FakeStore, mock_http: respx.MockRouter
) -> None:
    mock_http.get("https://www.example.edu/robots.txt").respond(503)
    assert _add(logged_in).status_code == 400
    assert store.added == []


def test_add_source_needs_https(
    logged_in: TestClient, store: FakeStore, mock_http: respx.MockRouter
) -> None:
    response = _add(logged_in, url="http://www.example.edu/admissions")
    assert response.status_code == 400
    assert "url must start with https://" in response.text
    assert not mock_http.calls  # robots.txt is not even asked
    assert 'value="http://www.example.edu/admissions"' in response.text  # input kept
    assert store.added == []


def test_add_source_rejects_bad_key(logged_in: TestClient, store: FakeStore) -> None:
    response = _add(logged_in, key="Not A Slug")
    assert response.status_code == 400 and "key:" in response.text


def test_add_source_refuses_duplicates(
    logged_in: TestClient, store: FakeStore, mock_http: respx.MockRouter
) -> None:
    mock_http.get("https://nust.edu.pk/robots.txt").respond(404)  # no robots.txt: allowed
    response = _add(logged_in, url="https://nust.edu.pk/pg", key="nust-pg")
    assert response.status_code == 400
    assert "already exists" in response.text


def test_add_source_needs_contact_email(store: FakeStore, mock_http: respx.MockRouter) -> None:
    with _client_with(store, contact_email="") as client:
        response = _add(client)
    assert response.status_code == 400 and "CONTACT_EMAIL" in response.text
    assert not mock_http.calls


def test_add_source_needs_csrf(logged_in: TestClient, store: FakeStore) -> None:
    assert logged_in.post("/sources", data=NEW_SOURCE).status_code == 403
    assert store.added == []


# ---------- profile ----------

PROFILE_FORM = {
    "full_name": "Owner Name", "email": "owner@example.com", "nationality": "PK",
    "date_of_birth": "2001-05-10", "degree": "BS CS", "cgpa": "3.4", "cgpa_scale": "4",
    "english_test": "IELTS", "english_score": "7", "work_experience_months": "6",
    "target_intake": "Fall 2027", "max_application_fee_eur": "",
    "target_fields": "Artificial Intelligence\nData Science, Robotics",
    "reminder_days": "7, 30, 14, 7", "moi_letter": "1", "fully_funded_only": "1",
    "emails_paused": "1",
}  # fmt: skip


def test_profile_page_shows_current_values(logged_in: TestClient) -> None:
    page = logged_in.get("/profile").text
    assert 'value="Owner Name"' in page
    assert 'value="30, 14, 7, 2"' in page
    assert "Artificial Intelligence\nData Science" in page
    assert logged_in.get("/api/v1/profile").json()["profile"]["cgpa"] == 3.2


def test_saving_the_profile(logged_in: TestClient, store: FakeStore) -> None:
    data = {**PROFILE_FORM, "csrf_token": _token(logged_in, "/profile")}
    response = logged_in.post("/profile", data=data, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/profile?saved=1"
    [(profile_id, saved, paused)] = store.saved_profiles
    assert profile_id == "p1" and paused is True
    assert saved.cgpa == 3.4 and saved.work_experience_months == 6
    assert saved.target_fields == ["Artificial Intelligence", "Data Science", "Robotics"]
    assert saved.reminder_days == [30, 14, 7]  # sorted, duplicates removed
    assert saved.moi_letter and saved.fully_funded_only and not saved.prefer_no_fee
    assert saved.max_application_fee_eur is None  # blank = no limit
    assert "Saved." in logged_in.get("/profile?saved=1").text


def test_bad_profile_values_are_shown_and_not_saved(
    logged_in: TestClient, store: FakeStore
) -> None:
    data = {**PROFILE_FORM, "cgpa": "4.5", "csrf_token": _token(logged_in, "/profile")}
    response = logged_in.post("/profile", data=data)
    assert response.status_code == 400
    assert "cgpa 4.5 is higher than cgpa_scale 4.0" in response.text
    assert 'value="Owner Name"' in response.text  # what was typed stays in the form
    assert store.saved_profiles == []


def test_bad_reminder_days_are_refused(logged_in: TestClient, store: FakeStore) -> None:
    data = {**PROFILE_FORM, "reminder_days": "30, soon",
            "csrf_token": _token(logged_in, "/profile")}  # fmt: skip
    response = logged_in.post("/profile", data=data)
    assert response.status_code == 400
    assert "&#39;soon&#39; is not a whole number from 1 to 365" in response.text  # escaped
    assert store.saved_profiles == []


def test_email_used_by_another_profile_is_refused(
    logged_in: TestClient, store: FakeStore
) -> None:
    store.taken_emails = {"taken@example.com"}
    data = {**PROFILE_FORM, "email": "taken@example.com",
            "csrf_token": _token(logged_in, "/profile")}  # fmt: skip
    response = logged_in.post("/profile", data=data)
    assert response.status_code == 400 and "another profile already uses" in response.text


def test_first_save_creates_the_profile(logged_in: TestClient, store: FakeStore) -> None:
    store.profile_record = None
    assert "No profile yet" in logged_in.get("/profile").text
    data = {**PROFILE_FORM, "csrf_token": _token(logged_in, "/profile")}
    assert logged_in.post("/profile", data=data, follow_redirects=False).status_code == 303
    assert store.saved_profiles[0][0] is None  # no id: create it


def test_profile_needs_csrf(logged_in: TestClient, store: FakeStore) -> None:
    assert logged_in.post("/profile", data=PROFILE_FORM).status_code == 403
    assert store.saved_profiles == []


# ---------- Run now ----------

GITHUB = {"run_now_github_token": TOKEN, "run_now_github_repo": "owner/scholarship-hunter"}
RUN_NOW_VARS = ("RUN_NOW_GITHUB_TOKEN", "RUN_NOW_GITHUB_REPO", "RUN_NOW_GITHUB_REF")


@pytest.fixture(autouse=True)
def _clean_run_now_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the Run now variables, so no test depends on the machine it runs on.

    GitHub Actions also sets GITHUB_TOKEN and GITHUB_REF; they must not change any result.
    """
    for name in RUN_NOW_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "the-runners-own-token")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/not-ours")


def test_run_now_starts_the_workflow(store: FakeStore, mock_http: respx.MockRouter) -> None:
    route = mock_http.post(DISPATCH_URL).respond(204)
    with _client_with(store, **GITHUB) as client:
        assert "Run now" in client.get("/runs").text
        response = client.post("/runs/dispatch", headers=_htmx(client, "/runs"))

    assert response.status_code == 200
    assert "Started." in response.text
    assert ACTIONS in response.text
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert request.content == b'{"ref":"main"}'
    assert TOKEN not in response.text


def test_run_now_explains_a_refused_token(store: FakeStore, mock_http: respx.MockRouter) -> None:
    mock_http.post(DISPATCH_URL).respond(403, json={"message": "Resource not accessible"})
    with _client_with(store, **GITHUB) as client:
        response = client.post("/runs/dispatch", headers=_htmx(client, "/runs"))
    assert response.status_code == 200  # HTMX swaps only 2xx answers, so the reason shows
    assert "Actions: Read and write" in response.text and "HTTP 403" in response.text
    assert "Resource not accessible" in response.text
    assert TOKEN not in response.text


def test_run_now_when_github_is_down(store: FakeStore, mock_http: respx.MockRouter) -> None:
    mock_http.post(DISPATCH_URL).mock(side_effect=httpx.ConnectError("boom"))
    with _client_with(store, **GITHUB) as client:
        response = client.post("/runs/dispatch", headers=_htmx(client, "/runs"))
    assert "Could not reach GitHub" in response.text


def test_run_now_without_javascript_shows_the_runs_page(
    store: FakeStore, mock_http: respx.MockRouter
) -> None:
    mock_http.post(DISPATCH_URL).respond(204)
    with _client_with(store, **GITHUB) as client:
        response = client.post("/runs/dispatch", data={"csrf_token": _token(client, "/runs")})
    assert "<html" in response.text and "Started." in response.text


def test_run_now_needs_setup(logged_in: TestClient, mock_http: respx.MockRouter) -> None:
    page = logged_in.get("/runs").text
    assert "RUN_NOW_GITHUB_TOKEN is not set" in page and 'action="/runs/dispatch"' not in page
    response = logged_in.post("/runs/dispatch", headers=_htmx(logged_in, "/runs"))
    assert "RUN_NOW_GITHUB_TOKEN is not set" in response.text
    assert not mock_http.calls


def test_run_now_rejects_a_strange_repo_name(
    store: FakeStore, mock_http: respx.MockRouter
) -> None:
    bad = {"run_now_github_token": TOKEN, "run_now_github_repo": "owner/repo/../../x"}
    with _client_with(store, **bad) as client:
        response = client.post("/runs/dispatch", headers=_htmx(client, "/runs"))
    assert "owner/name" in response.text
    assert not mock_http.calls


def test_run_now_reads_its_settings_from_the_environment(
    store: FakeStore, mock_http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUN_NOW_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("RUN_NOW_GITHUB_REPO", "owner/scholarship-hunter")
    monkeypatch.setenv("RUN_NOW_GITHUB_REF", "dev")
    route = mock_http.post(DISPATCH_URL).respond(204)
    with _client_with(store) as client:
        response = client.post("/runs/dispatch", headers=_htmx(client, "/runs"))
    assert "Started." in response.text
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.content == b'{"ref":"dev"}'


def test_run_now_needs_csrf(store: FakeStore, mock_http: respx.MockRouter) -> None:
    route = mock_http.post(DISPATCH_URL).respond(204)
    with _client_with(store, **GITHUB) as client:
        response = client.post("/runs/dispatch", headers={"HX-Request": "true"})
    assert response.status_code == 403
    assert not route.called


def test_token_never_appears_in_settings_repr() -> None:
    assert TOKEN not in repr(make_settings(**GITHUB))
