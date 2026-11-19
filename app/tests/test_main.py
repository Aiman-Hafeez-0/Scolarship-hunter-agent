"""Tests for app.main through FastAPI's TestClient, with a fake store (no database)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth
from app.main import create_app, get_store, get_today, protected, public, require_login
from app.queries import UpcomingDeadline
from app.tests.conftest import (
    EMAIL,
    PASSWORD,
    SID,
    TODAY,
    FakeStore,
    csrf_from,
    log_in,
    make_settings,
    row,
)

# ---------- health and login required ----------


def test_health_needs_no_login(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def _routes(router: APIRouter) -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


def _protected_get_paths() -> list[str]:
    return [route.path.replace("{sid}", SID) for route in _routes(protected)
            if "GET" in (route.methods or set())]  # fmt: skip


def test_only_health_and_login_are_public() -> None:
    assert sorted({route.path for route in _routes(public)}) == ["/health", "/login"]
    assert [d.dependency for d in protected.dependencies] == [require_login]
    paths = _protected_get_paths()
    assert {"/", "/scholarships", "/scholarships/table", f"/scholarships/{SID}", "/runs",
            "/api/v1/summary", "/api/v1/scholarships", f"/api/v1/scholarships/{SID}",
            "/api/v1/runs"} <= set(paths)  # fmt: skip


@pytest.mark.parametrize("path", _protected_get_paths())
def test_pages_and_api_need_login(client: TestClient, store: FakeStore, path: str) -> None:
    response = client.get(path, follow_redirects=False)

    if path.startswith("/api/"):
        assert response.status_code == 401
    else:
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")
    assert store.calls == []  # the database is never touched before login


def test_htmx_request_without_login_is_sent_to_login(client: TestClient) -> None:
    response = client.get("/scholarships/table", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


def test_forged_session_cookie_is_refused(client: TestClient) -> None:
    client.cookies.set(auth.SESSION_COOKIE, "eyJlbWFpbCI6Im93bmVyQGV4YW1wbGUuY29tIn0.forged")
    assert client.get("/api/v1/runs").status_code == 401


def test_logout_needs_login(client: TestClient) -> None:
    token = csrf_from(client.get("/login").text)
    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# ---------- logging in ----------


def test_right_password_logs_in_and_sets_a_safe_cookie(client: TestClient) -> None:
    response = log_in(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    session = next(h for h in response.headers.get_list("set-cookie")
                   if h.startswith(auth.SESSION_COOKIE))  # fmt: skip
    assert "HttpOnly" in session
    assert "SameSite=lax" in session
    assert "Max-Age=604800" in session  # 7 days
    assert "Secure" not in session  # localhost: plain http is allowed
    assert client.get("/").status_code == 200


def test_cookie_is_secure_when_not_on_localhost(store: FakeStore) -> None:
    app = create_app(make_settings())
    app.dependency_overrides[get_store] = lambda: store
    with TestClient(app, base_url="https://dashboard.example.org") as remote:
        response = log_in(remote)
    session = next(h for h in response.headers.get_list("set-cookie")
                   if h.startswith(auth.SESSION_COOKIE))  # fmt: skip
    assert "Secure" in session


def test_wrong_password_is_refused(client: TestClient) -> None:
    response = log_in(client, password="wrong password!!")

    assert response.status_code == 401
    assert "Wrong email or password." in response.text
    assert auth.SESSION_COOKIE not in response.cookies
    assert client.get("/api/v1/runs").status_code == 401


def test_wrong_email_is_refused(client: TestClient) -> None:
    token = csrf_from(client.get("/login").text)
    response = client.post("/login", data={"email": "someone@else.com", "password": PASSWORD,
                                           "csrf_token": token})  # fmt: skip
    assert response.status_code == 401


def test_login_goes_back_to_the_page_asked_for(client: TestClient) -> None:
    token = csrf_from(client.get("/login?next=/runs").text)
    response = client.post("/login", data={"email": EMAIL, "password": PASSWORD,
                                           "csrf_token": token, "next": "/runs"},
                           follow_redirects=False)  # fmt: skip
    assert response.headers["location"] == "/runs"


def test_login_never_redirects_to_another_site(client: TestClient) -> None:
    token = csrf_from(client.get("/login").text)
    response = client.post("/login", data={"email": EMAIL, "password": PASSWORD,
                                           "csrf_token": token, "next": "//evil.example"},
                           follow_redirects=False)  # fmt: skip
    assert response.headers["location"] == "/"


def test_login_page_explains_missing_setup(store: FakeStore) -> None:
    app = create_app(make_settings(dashboard_password_hash=None))
    with TestClient(app, base_url="http://localhost") as unset:
        page = unset.get("/login")
        response = log_in(unset)
    assert "DASHBOARD_PASSWORD_HASH is not set" in page.text
    assert response.status_code == 401


def test_logout_ends_the_session(logged_in: TestClient) -> None:
    token = csrf_from(logged_in.get("/").text)
    response = logged_in.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert logged_in.get("/api/v1/runs").status_code == 401


# ---------- rate limit ----------


def test_five_failed_logins_block_further_tries(client: TestClient) -> None:
    for _ in range(5):
        assert log_in(client, password="wrong password!!").status_code == 401

    blocked = log_in(client)  # even the right password is refused now

    assert blocked.status_code == 429
    assert "Too many failed logins" in blocked.text
    assert int(blocked.headers["Retry-After"]) > 0
    assert client.get("/api/v1/runs").status_code == 401


def test_successful_login_resets_the_count(client: TestClient) -> None:
    for _ in range(4):
        log_in(client, password="wrong password!!")
    assert log_in(client).status_code == 303
    client.cookies.clear()
    for _ in range(4):
        assert log_in(client, password="wrong password!!").status_code == 401


# ---------- CSRF ----------


def test_login_without_csrf_token_is_rejected(client: TestClient) -> None:
    client.get("/login")
    response = client.post("/login", data={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 403
    assert auth.SESSION_COOKIE not in response.cookies


def test_login_with_wrong_csrf_token_is_rejected(client: TestClient) -> None:
    client.get("/login")
    response = client.post("/login", data={"email": EMAIL, "password": PASSWORD,
                                           "csrf_token": "made-up"})  # fmt: skip
    assert response.status_code == 403


def test_csrf_token_without_its_cookie_is_rejected(client: TestClient) -> None:
    token = csrf_from(client.get("/login").text)
    client.cookies.clear()  # another site can copy a token, but not our signed cookie
    response = client.post("/login", data={"email": EMAIL, "password": PASSWORD,
                                           "csrf_token": token})  # fmt: skip
    assert response.status_code == 403


def test_rejected_csrf_does_not_count_as_a_login_try(client: TestClient) -> None:
    client.get("/login")
    for _ in range(6):
        client.post("/login", data={"email": EMAIL, "password": "x"})
    assert log_in(client).status_code == 303


def test_logout_without_csrf_token_is_rejected(logged_in: TestClient) -> None:
    response = logged_in.post("/logout", follow_redirects=False)
    assert response.status_code == 403
    assert logged_in.get("/api/v1/runs").status_code == 200  # still logged in


def test_csrf_token_in_header_is_accepted(logged_in: TestClient) -> None:
    token = csrf_from(logged_in.get("/").text)
    response = logged_in.post("/logout", headers={"X-CSRF-Token": token},
                              follow_redirects=False)  # fmt: skip
    assert response.status_code == 303


# ---------- home ----------


def test_home_shows_counts_deadlines_and_last_run(logged_in: TestClient, store: FakeStore) -> None:
    store.rows = [row(id="a", match_status="eligible"), row(id="b", match_status="maybe"),
                  row(id="c", match_status=None), row(id="d", status="closed")]  # fmt: skip
    store.upcoming = [
        UpcomingDeadline(SID, "Soon Scholarship", "Apply", TODAY + timedelta(days=5), False),
        UpcomingDeadline(SID, "Later Admission", "Apply", TODAY + timedelta(days=20), False),
        UpcomingDeadline(SID, "Far Test", "Register", TODAY + timedelta(days=90), True),
    ]

    page = logged_in.get("/").text

    assert "5 days left" in page and "bg-red-100" in page
    assert "20 days left" in page and "bg-orange-100" in page
    assert "90 days left" in page
    assert "(estimated)" in page
    assert "No successful weekly run" not in page
    summary = logged_in.get("/api/v1/summary").json()
    assert summary["match_counts"] == {"eligible": 1, "maybe": 1, "not_eligible": 0,
                                       "not_matched": 1}  # fmt: skip
    assert summary["last_run"]["status"] == "success"
    assert summary["run_overdue"] is False


def test_home_warns_when_no_good_run_in_8_days(logged_in: TestClient, store: FakeStore) -> None:
    store.last_good = datetime.now(UTC) - timedelta(days=9)
    assert "No successful weekly run in the last 8 days" in logged_in.get("/").text


def test_home_warns_when_there_was_never_a_good_run(
    logged_in: TestClient, store: FakeStore
) -> None:
    store.last_good, store.run_rows = None, []
    page = logged_in.get("/").text
    assert "There has never been one." in page
    assert "No runs recorded yet." in page


# ---------- scholarships and filters ----------


@pytest.fixture
def three(store: FakeStore) -> FakeStore:
    store.rows = [
        row(id="a", name="Hungary Grant", location="abroad", funding="full", score=90,
            next_deadline=date(2027, 1, 15), match_status="eligible"),
        row(id="b", name="NUST MS", provider="NUST", country="PK", location="pakistan",
            opportunity_type="admission", funding="unknown", score=40,
            next_deadline=date(2026, 12, 1), match_status="maybe"),
        row(id="c", name="HAT Test", provider="HEC", country="PK", location="pakistan",
            opportunity_type="entry_test", funding="unknown", score=None,
            next_deadline=None, match_status=None),
    ]  # fmt: skip
    return store


def _names(client: TestClient, query: str) -> list[str]:
    return [r["name"] for r in client.get(f"/api/v1/scholarships{query}").json()]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", ["NUST MS", "Hungary Grant", "HAT Test"]),  # by next deadline, none last
        ("?sort=score", ["Hungary Grant", "NUST MS", "HAT Test"]),  # unmatched last
        ("?type=admission", ["NUST MS"]),
        ("?location=pakistan", ["NUST MS", "HAT Test"]),
        ("?match=eligible", ["Hungary Grant"]),
        ("?match=not_matched", ["HAT Test"]),
        ("?funding=full", ["Hungary Grant"]),
        ("?deadline_before=2026-12-31", ["NUST MS"]),
        ("?q=hec", ["HAT Test"]),  # searches the provider too
        ("?type=nonsense&deadline_before=not-a-date", ["NUST MS", "Hungary Grant", "HAT Test"]),
    ],
)
def test_filters_and_sorting(
    logged_in: TestClient, three: FakeStore, query: str, expected: list[str]
) -> None:
    assert _names(logged_in, query) == expected


def test_scholarships_page_shows_the_form_and_table(
    logged_in: TestClient, three: FakeStore
) -> None:
    page = logged_in.get("/scholarships?location=pakistan").text
    assert 'hx-get="/scholarships/table"' in page
    assert 'value="pakistan" selected' in page
    assert "NUST MS" in page and "Hungary Grant" not in page
    assert "<html" in page


def test_htmx_table_is_only_the_table_and_updates_the_address(
    logged_in: TestClient, three: FakeStore
) -> None:
    response = logged_in.get("/scholarships/table?funding=full&sort=score",
                             headers={"HX-Request": "true"})  # fmt: skip
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "Hungary Grant" in response.text and "NUST MS" not in response.text
    assert response.headers["HX-Push-Url"] == "/scholarships?funding=full&sort=score"


def test_empty_result_says_so(logged_in: TestClient, three: FakeStore) -> None:
    assert "Nothing matches these filters." in logged_in.get("/scholarships/table?q=zzz").text


def test_closed_items_are_hidden_unless_asked(logged_in: TestClient, store: FakeStore) -> None:
    store.rows = [row(id="a", name="Open One"), row(id="b", name="Closed One", status="closed")]
    assert _names(logged_in, "") == ["Open One"]
    assert sorted(_names(logged_in, "?include_closed=1")) == ["Closed One", "Open One"]


# ---------- detail ----------


def test_detail_page_shows_evidence_badges_and_reasons(logged_in: TestClient) -> None:
    page = logged_in.get(f"/scholarships/{SID}").text

    assert "needs review" in page and "bg-yellow-100" in page
    assert "deadlines[1] quote not found on page (score 62)" in page
    assert "not checked yet" in page
    assert "<details" in page and "Full tuition waiver and stipend" in page
    assert "The deadline is 15 January 2027." in page
    assert 'href="https://example.org/sh"' in page  # the source link
    assert "take IELTS or TOEFL" in page  # a match action
    assert "Transcript" in page
    assert "not stated" in page  # Campus is empty


def test_verified_item_shows_when_it_was_checked(logged_in: TestClient, store: FakeStore) -> None:
    store.details[SID].verified_at = datetime(2026, 11, 18, 3, 0, tzinfo=UTC)
    store.details[SID].needs_review = False
    page = logged_in.get(f"/scholarships/{SID}").text
    assert "not checked yet" not in page
    assert "checked 18 Nov 2026, 08:00 PKT" in page
    assert "bg-yellow-100" not in page


def test_unknown_scholarship_is_404(logged_in: TestClient) -> None:
    assert logged_in.get("/scholarships/does-not-exist").status_code == 404
    assert logged_in.get("/api/v1/scholarships/does-not-exist").status_code == 404


def test_detail_json(logged_in: TestClient) -> None:
    data = logged_in.get(f"/api/v1/scholarships/{SID}").json()
    assert data["needs_review"] is True
    assert data["deadlines"][0]["quote"]["text"] == "The deadline is 15 January 2027."


# ---------- runs ----------


def test_runs_page_lists_the_history(logged_in: TestClient) -> None:
    page = logged_in.get("/runs").text
    assert "weekly" in page and "15,400" in page  # tokens in + out
    assert logged_in.get("/api/v1/runs").json()[0]["sources_failed"] == 1


# ---------- database problems ----------


def test_missing_database_url_shows_a_clear_error() -> None:
    app = create_app(make_settings())
    app.dependency_overrides[get_today] = lambda: TODAY
    with TestClient(app, base_url="http://localhost") as no_db:
        log_in(no_db)
        page = no_db.get("/runs")
        api = no_db.get("/api/v1/runs")
    assert page.status_code == 503 and "DATABASE_URL is not set" in page.text
    assert api.status_code == 503
