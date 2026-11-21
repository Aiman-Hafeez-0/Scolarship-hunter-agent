"""Tests for the last redesign round: Sources table, live checks, Runs, Profile, error pages."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app, get_store, get_today
from app.tests.conftest import (
    SID,
    TODAY,
    FakeStore,
    csrf_from,
    log_in,
    make_settings,
    run_row,
    source_row,
    use_fake_limiter,
)


def _htmx(client: TestClient, page: str) -> dict[str, str]:
    return {"HX-Request": "true", "X-CSRF-Token": csrf_from(client.get(page).text)}


# ---------- Sources ----------


def test_sources_table_has_health_dots(logged_in: TestClient, store: FakeStore) -> None:
    store.source_rows = [
        source_row(),  # 3 failures: red
        source_row(key="ok", name="Working one", consecutive_failures=0, last_error=None),
        source_row(key="off", name="Paused one", active=False),
    ]
    page = logged_in.get("/sources").text
    rows = {key: page.split(f'<tr id="source-{key}"')[1].split("</tr>")[0]
            for key in ("nust-pg", "ok", "off")}  # fmt: skip
    assert 'data-health="failing"' in rows["nust-pg"] and "HTTP 503" in rows["nust-pg"]
    assert 'data-health="ok"' in rows["ok"]
    assert 'data-health="paused"' in rows["off"]
    assert 'role="switch"' in rows["ok"]
    assert "<time datetime=" in rows["nust-pg"] and "title=" in rows["nust-pg"]  # relative
    assert "1 failing" in page  # the summary line


def test_switch_answer_updates_the_row_and_the_phone_card(
    logged_in: TestClient, store: FakeStore
) -> None:
    response = logged_in.post("/sources/nust-pg/active", data={"active": "0"},
                              headers=_htmx(logged_in, "/sources"))  # fmt: skip
    assert '<tr id="source-nust-pg"' in response.text
    assert '<li id="source-card-nust-pg" hx-swap-oob="true"' in response.text
    assert 'data-health="paused"' in response.text


def test_add_source_form_checks_live(logged_in: TestClient) -> None:
    page = logged_in.get("/sources").text
    assert 'hx-post="/sources/validate"' in page and 'id="f-url-error"' in page

    response = logged_in.post("/sources/validate", data={"url": "http://x.org", "key": "nust-pg"},
                              headers=_htmx(logged_in, "/sources"))  # fmt: skip

    assert response.status_code == 200
    assert '<p id="f-url-error" hx-swap-oob="true"' in response.text
    assert "url must start with https://" in response.text
    assert "a source with this key already exists" in response.text
    assert 'data-field-error="name" class="mt-1.5 text-small text-red-700 empty:hidden' in (
        response.text
    )


def test_live_check_needs_csrf_and_login(logged_in: TestClient) -> None:
    assert logged_in.post("/sources/validate", data={"url": "x"}).status_code == 403
    logged_in.cookies.clear()  # logged out now
    response = logged_in.post("/sources/validate", follow_redirects=False)
    assert response.status_code == 303


def test_failed_save_shows_errors_under_the_fields(logged_in: TestClient) -> None:
    data = {"url": "http://x.org", "name": "X", "key": "x", "country": "DE",
            "csrf_token": csrf_from(logged_in.get("/sources").text)}  # fmt: skip
    page = logged_in.post("/sources", data=data).text
    line = page.split('id="f-url-error"')[1].split("</p>")[0]
    assert "url must start with https://" in line
    assert "open data-open-on-load" in page  # the panel stays open


# ---------- Runs ----------


def test_runs_table_has_duration_and_expandable_errors(
    logged_in: TestClient, store: FakeStore
) -> None:
    start = datetime.now(UTC) - timedelta(hours=1)
    store.run_rows = [run_row(id="r9", status="failed", started_at=start,
                              finished_at=start + timedelta(minutes=3, seconds=5),
                              error="Gemini: daily quota used up")]  # fmt: skip
    page = logged_in.get("/runs").text
    assert 'data-badge="run-failed"' in page and "3 min 05 s" in page
    assert 'data-toggle-row="run-r9"' in page and 'aria-controls="run-r9"' in page
    details = page.split('<tr id="run-r9"')[1].split("</tr>")[0]
    assert "hidden" in details.split(">")[0] and "Gemini: daily quota used up" in details


# ---------- Profile ----------


def test_profile_groups_helpers_and_save_bar(logged_in: TestClient) -> None:
    page = logged_in.get("/profile").text
    for heading in ("Academic", "English", "Preferences", "Notifications"):
        assert f"{heading}</h2>" in page, heading
    for name in ("degree", "cgpa", "english_test", "target_intake", "email", "reminder_days"):
        assert f'id="f-{name}-help"' in page, name
    assert "data-dirty-form" in page and "data-save-bar" in page
    assert 'data-dirty="true"' not in page


def test_profile_with_errors_keeps_the_save_bar_open(logged_in: TestClient) -> None:
    data = {"email": "owner@example.com", "cgpa": "9", "cgpa_scale": "4",
            "csrf_token": csrf_from(logged_in.get("/profile").text)}  # fmt: skip
    page = logged_in.post("/profile", data=data).text
    assert 'data-dirty="true"' in page


def test_profile_saved_toast(logged_in: TestClient) -> None:
    page = logged_in.get("/profile?saved=1").text
    assert 'class="toast' in page and "Saved." in page


# ---------- Error pages, favicon, titles ----------


def test_unknown_page_is_a_friendly_404(logged_in: TestClient) -> None:
    response = logged_in.get("/no-such-page")
    assert response.status_code == 404
    assert "ERROR 404" in response.text and "Page not found" in response.text
    assert "<title>Page not found · Scholarship Hunter</title>" in response.text
    api = logged_in.get("/api/v1/no-such-thing")
    assert api.status_code == 404 and api.json() == {"detail": "Not Found"}


def test_unknown_scholarship_uses_the_same_page(logged_in: TestClient) -> None:
    page = logged_in.get("/scholarships/does-not-exist").text
    assert "ERROR 404" in page and 'aria-label="Main"' in page  # inside the app frame


@pytest.fixture
def broken() -> Iterator[TestClient]:
    """An app whose store fails with a bug (not a database error)."""

    class Broken(FakeStore):
        def sources(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("secret detail")

    app = create_app(make_settings())
    app.dependency_overrides[get_store] = lambda: Broken()
    app.dependency_overrides[get_today] = lambda: TODAY
    use_fake_limiter(app)
    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as client:
        log_in(client)
        yield client


def test_a_bug_shows_a_calm_error_page(broken: TestClient) -> None:
    page = broken.get("/sources")
    assert page.status_code == 500
    assert "ERROR 500" in page.text and "Something went wrong" in page.text
    assert "secret detail" not in page.text
    api = broken.get("/api/v1/sources")
    assert api.status_code == 500 and "secret detail" not in api.text


def test_favicon(client: TestClient) -> None:
    page = client.get("/login").text
    assert 'rel="icon" type="image/svg+xml" href="/static/favicon.svg?v=' in page
    response = client.get("/favicon.ico", follow_redirects=False)
    assert response.status_code == 301 and response.headers["location"].startswith(
        "/static/favicon.svg"
    )
    assert client.get("/static/favicon.svg").status_code == 200


@pytest.mark.parametrize(
    ("path", "title"),
    [("/", "Overview"), ("/scholarships", "Opportunities"), ("/deadlines", "Deadlines"),
     ("/sources", "Sources"), ("/runs", "Runs"), ("/profile", "Profile"),
     (f"/scholarships/{SID}", "Stipendium Hungaricum")],
)  # fmt: skip
def test_page_titles(logged_in: TestClient, path: str, title: str) -> None:
    assert f"<title>{title} · Scholarship Hunter</title>" in logged_in.get(path).text


def test_every_page_has_the_same_header_line(logged_in: TestClient) -> None:
    for path in ("/", "/scholarships", "/deadlines", "/sources", "/runs", "/profile"):
        page = logged_in.get(path).text
        assert '<div class="-mt-1 mb-6 flex flex-col gap-3' in page, path


def test_opportunities_show_a_skeleton_while_loading(logged_in: TestClient) -> None:
    page = logged_in.get("/scholarships").text
    assert 'hx-indicator="#opps"' in page and 'class="skeleton-indicator"' in page
    assert "Loading…</span>" not in page


# ---------- Database problems are explained in the server log ----------


def test_redact_hides_the_database_url_and_passwords() -> None:
    from app.main import redact

    url = "postgresql://postgres.abc:s3cret@db.example.com:5432/postgres"
    assert redact(f"cannot use {url}", url) == "cannot use <DATABASE_URL>"
    assert redact("failed for postgresql://me:pa55@h:5432/db here", None) == (
        "failed for postgresql://me:***@h:5432/db here"
    )
    assert redact("plain message", "postgresql://x") == "plain message"


def test_database_error_is_logged_but_not_shown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import psycopg

    secret_url = "postgresql://postgres:hunter2@db.example.com:5432/postgres"

    class Down(FakeStore):
        def sources(self):  # type: ignore[no-untyped-def]
            raise psycopg.OperationalError(f'connection to "{secret_url}" failed: timeout expired')

    app = create_app(make_settings(database_url=secret_url))
    app.dependency_overrides[get_store] = lambda: Down()
    app.dependency_overrides[get_today] = lambda: TODAY
    use_fake_limiter(app)
    with TestClient(app, base_url="http://localhost") as client:
        log_in(client)
        with caplog.at_level("WARNING", logger="app.main"):
            page = client.get("/sources")

    assert page.status_code == 503 and "timeout expired" not in page.text
    logged = caplog.text
    assert "GET /sources: OperationalError" in logged and "timeout expired" in logged
    assert "hunter2" not in logged and "<DATABASE_URL>" in logged


def test_missing_database_url_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(make_settings())  # no DATABASE_URL, real limiter: needs the database
    with TestClient(app, base_url="http://localhost") as client:
        token = csrf_from(client.get("/login").text)
        with caplog.at_level("WARNING", logger="app.main"):
            response = client.post("/login", data={"email": "a@b.c", "password": "x",
                                                   "csrf_token": token})  # fmt: skip
    assert response.status_code == 503
    assert "POST /login: DATABASE_URL is not set" in caplog.text
