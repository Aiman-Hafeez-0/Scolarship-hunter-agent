"""Tests for the redesigned layout: sidebar, top bar, Overview, Deadlines, login page."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app import auth
from app.main import create_app, get_store, get_today, too_many_tries
from app.queries import RecentChange, UpcomingDeadline
from app.tests.conftest import (
    SID,
    TODAY,
    FakeStore,
    log_in,
    make_settings,
    row,
    run_row,
    use_fake_limiter,
)

GITHUB = {"run_now_github_token": "t" * 20, "run_now_github_repo": "owner/repo"}


def _deadline(days: int, name: str = "Soon", kind: str = "scholarship") -> UpcomingDeadline:
    return UpcomingDeadline(SID, name, "Apply", TODAY + timedelta(days=days), False, kind)


def _client(store: FakeStore, **settings: object) -> TestClient:
    app = create_app(make_settings(**settings))
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_today] = lambda: TODAY
    use_fake_limiter(app)
    return TestClient(app, base_url="http://localhost")


# ---------- the frame of every page ----------


def test_pages_use_the_built_css_not_the_tailwind_cdn(logged_in: TestClient) -> None:
    page = logged_in.get("/").text
    assert "cdn.tailwindcss.com" not in page
    assert '/static/app.css?v=' in page and '/static/htmx.min.js?v=' in page
    assert "fonts.googleapis.com/css2?family=Manrope" in page
    css = logged_in.get("/static/app.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]


def test_static_files_need_no_login(client: TestClient) -> None:
    assert client.get("/static/app.js").status_code == 200


def test_sidebar_highlights_the_current_page(logged_in: TestClient) -> None:
    for path, name in [("/", "Overview"), ("/scholarships", "Opportunities"),
                       ("/deadlines", "Deadlines"), ("/sources", "Sources"), ("/runs", "Runs"),
                       ("/profile", "Profile")]:  # fmt: skip
        page = logged_in.get(path).text
        navs = [part.split("</nav>")[0] for part in page.split('<nav aria-label="Main"')[1:]]
        assert len(navs) == 2  # desktop sidebar + phone drawer
        for nav in navs:
            assert nav.count('aria-current="page"') == 1, path
            assert name in nav.split('aria-current="page"')[1].split("</a>")[0], path


def test_sidebar_shows_the_last_run(logged_in: TestClient, store: FakeStore) -> None:
    store.run_rows = [run_row(status="failed", started_at=datetime.now(UTC) - timedelta(hours=2))]
    page = logged_in.get("/sources").text
    last_run = page.split('data-testid="sidebar-last-run"')[1].split("</a>")[0]
    assert 'data-badge="run-failed"' in last_run and "2 hours ago" in last_run
    store.run_rows = []
    assert "No runs yet" in logged_in.get("/sources").text


def test_top_bar_has_search_menu_drawer_and_theme_toggle(logged_in: TestClient) -> None:
    page = logged_in.get("/").text
    assert 'id="global-search"' in page and 'action="/scholarships"' in page and 'name="q"' in page
    assert 'aria-label="Open menu"' in page and 'id="mobile-nav"' in page
    assert "data-theme-toggle" in page
    assert 'aria-label="Account menu (owner@example.com)"' in page
    assert ">O</span>" in page  # the initial avatar
    assert 'action="/logout"' in page and 'href="/profile"' in page
    assert "Skip to content" in page


def test_breadcrumbs_on_the_detail_page(logged_in: TestClient) -> None:
    page = logged_in.get(f"/scholarships/{SID}").text
    crumbs = page.split('aria-label="Breadcrumb"')[1].split("</nav>")[0]
    assert 'href="/scholarships"' in crumbs and "Opportunities" in crumbs
    assert "<title>Stipendium Hungaricum · Scholarship Hunter</title>" in page


def test_run_now_button_asks_first_when_set_up(store: FakeStore) -> None:
    with _client(store, **GITHUB) as client:
        log_in(client)
        page = client.get("/deadlines").text
    assert 'hx-post="/runs/dispatch"' in page and 'hx-target="#toasts"' in page
    assert 'data-confirm-dialog="confirm-run"' in page and 'id="confirm-run"' in page


def test_run_now_button_explains_missing_setup(logged_in: TestClient) -> None:
    page = logged_in.get("/").text
    assert 'aria-disabled="true"' in page and "Run now needs setup" in page
    assert 'action="/runs/dispatch"' not in page and 'id="confirm-run"' not in page


def test_dispatch_answer_is_a_toast(logged_in: TestClient) -> None:
    from app.tests.conftest import csrf_from

    token = csrf_from(logged_in.get("/").text)
    response = logged_in.post("/runs/dispatch", headers={"HX-Request": "true",
                                                         "X-CSRF-Token": token})  # fmt: skip
    assert 'class="toast' in response.text and 'role="alert"' in response.text
    assert "RUN_NOW_GITHUB_TOKEN is not set" in response.text


# ---------- Overview ----------


def test_overview_stat_cards(logged_in: TestClient, store: FakeStore) -> None:
    store.rows = [row(id="a", name="A"), row(id="b", name="B"),
                  row(id="c", name="C", match_status="maybe")]  # fmt: skip
    store.upcoming = [_deadline(0), _deadline(14), _deadline(15)]
    store.run_rows = [run_row(started_at=datetime.now(UTC) - timedelta(hours=2))]

    page = logged_in.get("/").text
    summary = logged_in.get("/api/v1/summary").json()

    assert summary["match_counts"]["eligible"] == 2 and summary["match_counts"]["maybe"] == 1
    assert summary["deadlines_soon"] == 2  # today and day 14; day 15 is outside
    assert "Deadlines in the next 14 days" in page
    assert "2 hours ago" in page and 'title="' in page  # exact time on hover
    assert 'href="/scholarships?match=eligible"' in page


def test_overview_lists_the_next_10_deadlines_as_links(
    logged_in: TestClient, store: FakeStore
) -> None:
    store.upcoming = [_deadline(d, f"Item {d:02}", "entry_test") for d in range(1, 13)]
    page = logged_in.get("/").text
    assert "Item 10" in page and "Item 11" not in page
    assert page.count(f'href="/scholarships/{SID}"') >= 10  # each row is a link
    assert "1 day left" in page and 'data-badge="type"' in page and "Entry test" in page


def test_overview_recently_changed_and_needs_review(
    logged_in: TestClient, store: FakeStore
) -> None:
    store.changes = [RecentChange(SID, "Stipendium Hungaricum", "status", "upcoming", "open",
                                  datetime.now(UTC) - timedelta(minutes=5))]  # fmt: skip
    store.rows = [row(id="a", name="Checked One"),
                  row(id="b", name="Doubtful One", needs_review=True),
                  row(id="c", name="Closed Doubt", needs_review=True, status="closed")]  # fmt: skip

    page = logged_in.get("/").text
    summary = logged_in.get("/api/v1/summary").json()

    assert "Recently changed" in page and "&#34;open&#34;" in page and "5 minutes ago" in page
    assert [r["name"] for r in summary["needs_review"]] == ["Doubtful One"]  # closed left out
    assert summary["needs_review_total"] == 1
    assert 'href="/scholarships/b"' in page


def test_overview_empty_states_are_friendly(logged_in: TestClient, store: FakeStore) -> None:
    store.rows, store.upcoming, store.changes = [], [], []
    page = logged_in.get("/").text
    assert "No upcoming deadlines yet" in page
    assert "No changes yet" in page
    assert "Nothing to review" in page


def test_overview_quick_actions(logged_in: TestClient) -> None:
    page = logged_in.get("/").text
    assert "Quick actions" in page and 'href="/sources?add=1"' in page


# ---------- Sources slide-over ----------


def test_add_source_panel_opens_from_the_overview_link(logged_in: TestClient) -> None:
    closed = logged_in.get("/sources").text
    assert 'id="add-source"' in closed and "data-open-on-load" not in closed
    opened = logged_in.get("/sources?add=1").text
    assert 'id="add-source" aria-labelledby="add-source-title" open data-open-on-load' in opened


# ---------- Login page ----------


def test_login_page_has_brand_panel_eye_and_remember(client: TestClient) -> None:
    page = client.get("/login").text
    assert "Never miss a scholarship deadline again." in page
    assert page.count('class="flex gap-4"') == 3  # three benefits
    assert 'data-toggle-password="f-password"' in page and 'aria-label="Show password"' in page
    assert 'name="remember" type="checkbox" value="1" checked' in page
    assert "data-loading-form" in page and "Logging in…" in page


def test_without_keep_me_signed_in_the_cookie_ends_with_the_browser(client: TestClient) -> None:
    response = log_in(client, remember=False)
    session = next(h for h in response.headers.get_list("set-cookie")
                   if h.startswith(auth.SESSION_COOKIE))  # fmt: skip
    assert "Max-Age" not in session and "HttpOnly" in session
    assert client.get("/").status_code == 200


def test_short_session_expires_after_12_hours() -> None:
    settings = make_settings()
    short = auth.make_session(settings, "owner@example.com", remember=False)
    long = auth.make_session(settings, "owner@example.com", remember=True)
    later = datetime.now(UTC) + timedelta(hours=13)
    assert auth.read_session(settings, short) == "owner@example.com"
    assert auth.read_session(settings, short, now=later) is None
    assert auth.read_session(settings, long, now=later) == "owner@example.com"


def test_too_many_tries_message() -> None:
    assert too_many_tries(900) == "Too many tries. Wait 15 minutes and try again."
    assert too_many_tries(30) == "Too many tries. Wait 1 minute and try again."


def test_wrong_password_message_and_kept_choices(client: TestClient) -> None:
    response = log_in(client, password="wrong password!!", remember=False)
    assert "Wrong email or password." in response.text and 'role="alert"' in response.text
    assert 'value="owner@example.com"' in response.text  # the email stays filled in
    assert 'name="remember" type="checkbox" value="1" checked' not in response.text
