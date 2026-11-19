"""End-to-end test in a real browser (Playwright): log in, list, detail, verify.

Skipped unless E2E=1, because it needs Chromium and internet access (HTMX and Tailwind
come from a CDN). The app runs in a background thread with the fake store, so no database
is needed. How to run it is in the README ("End-to-end test").
"""

import os
import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from app.main import create_app, get_store, get_today
from app.tests.conftest import EMAIL, PASSWORD, SID, TODAY, FakeStore, make_settings

pytestmark = pytest.mark.skipif(os.environ.get("E2E") != "1", reason="set E2E=1 to run it")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_server(store: FakeStore) -> Iterator[str]:
    """The dashboard on http://127.0.0.1:<free port> (127.0.0.1: cookies work over http)."""
    app = create_app(make_settings())
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_today] = lambda: TODAY
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the test server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_log_in_open_list_and_detail_then_verify(live_server: str, store: FakeStore) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        expect = sync_api.expect

        # Logging in: any page sends us to the login form first.
        page.goto(live_server + "/scholarships")
        expect(page).to_have_url(f"{live_server}/login?next=%2Fscholarships")
        page.get_by_label("Email").fill(EMAIL)
        page.get_by_label("Password").fill(PASSWORD)
        page.get_by_role("button", name="Log in").click()

        # The list (it was the page we asked for).
        expect(page).to_have_url(f"{live_server}/scholarships")
        expect(page.get_by_text("1 result")).to_be_visible()
        page.get_by_role("link", name="Stipendium Hungaricum").click()

        # The detail page.
        expect(page).to_have_url(f"{live_server}/scholarships/{SID}")
        expect(page.get_by_text("needs review", exact=True)).to_be_visible()  # the badge
        status = page.locator("#verify-status")
        expect(status).to_contain_text("not checked yet")
        page.get_by_text("Show quote").first.click()
        expect(page.get_by_text("Full tuition waiver and stipend")).to_be_visible()

        # Verify: HTMX swaps the badge without reloading the page. A full reload would wipe
        # this marker, so finding it afterwards proves the update happened in place.
        page.evaluate("window.__noReload = true")
        page.get_by_role("button", name="I checked this").click()
        expect(status).to_contain_text("Mark checked again")
        expect(status).not_to_contain_text("not checked yet")
        assert page.evaluate("window.__noReload === true")
        assert store.details[SID].verified_at is not None

        browser.close()
