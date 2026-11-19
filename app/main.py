"""The dashboard web app: HTML pages plus the same data as JSON under /api/v1.

Start it with:  uv run uvicorn app.main:app --reload

Only /health and /login work without logging in. Every other page and API route goes
through `require_login` (see protected = APIRouter(...) below).
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import auth, github
from app.forms import (
    parse_profile_form,
    parse_source_form,
    profile_form_values,
    robots_allows,
    source_yaml,
)
from app.queries import (
    FUNDING_TYPES,
    LOCATIONS,
    MATCH_STATUSES,
    NOT_MATCHED,
    OPPORTUNITY_TYPES,
    DashboardStore,
    PostgresDashboardStore,
    ProfileEmailTaken,
    ProfileRecord,
    RunRow,
    ScholarshipDetail,
    ScholarshipRow,
    SourceRow,
    UpcomingDeadline,
    days_left,
    deadline_tone,
    filter_rows,
    match_counts,
    parse_filters,
)
from pipeline.config import Settings
from pipeline.notify.alerts import WEEKLY_MAX_AGE
from pipeline.notify.deadlines import PKT

TEMPLATES = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


# ---------- Template helpers ----------


def _pkt(value: datetime | None) -> str:
    """A time shown in Pakistan time, e.g. '18 Nov 2026, 08:17 PKT'."""
    return value.astimezone(PKT).strftime("%d %b %Y, %H:%M PKT") if value else "-"


def _day(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "no date"


def _label(value: str | None) -> str:
    return (value or "-").replace("_", " ")


def _as_json(value: Any) -> str:
    return "-" if value is None else json.dumps(value, ensure_ascii=False, default=str)


TEMPLATES.env.filters.update(pkt=_pkt, day=_day, label=_label, as_json=_as_json)
TEMPLATES.env.globals.update(tone=deadline_tone, days_left=days_left)


# ---------- Dependencies ----------


class NotLoggedIn(Exception):
    pass


class DatabaseMissing(Exception):
    pass


class CsrfRejected(Exception):
    pass


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


def require_login(request: Request, settings: SettingsDep) -> str:
    """The logged-in email; raises NotLoggedIn (-> login page or 401) otherwise."""
    email = auth.read_session(settings, request.cookies.get(auth.SESSION_COOKIE))
    if email is None:
        raise NotLoggedIn
    return email


def get_store(settings: SettingsDep) -> Iterator[DashboardStore]:
    """One database connection per request (fine for a one-person dashboard)."""
    if not settings.database_url:
        raise DatabaseMissing
    # connect_timeout: an unreachable database shows the error page instead of hanging.
    with psycopg.connect(settings.database_url, autocommit=True, connect_timeout=10) as conn:
        yield PostgresDashboardStore(conn)


def get_today() -> date:
    return datetime.now(PKT).date()


UserDep = Annotated[str, Depends(require_login)]
StoreDep = Annotated[DashboardStore, Depends(get_store)]
TodayDep = Annotated[date, Depends(get_today)]


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _set_cookie(request: Request, response: Response, name: str, value: str) -> None:
    response.set_cookie(
        name, value, max_age=auth.SESSION_MAX_AGE_S, path="/", httponly=True, samesite="lax",
        secure=not auth.is_local(request.url.hostname),
    )  # fmt: skip


def _csrf_token(request: Request, settings: Settings) -> tuple[str, bool]:
    """(token, is_new). The token lives in a signed cookie; new visitors get a fresh one."""
    current = auth.read_csrf(settings, request.cookies.get(auth.CSRF_COOKIE))
    return (current, False) if current else (auth.new_csrf_token(), True)


def render(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    """Render a page with the CSRF token every POST form needs (and set its cookie)."""
    settings = get_settings(request)
    token, is_new = _csrf_token(request, settings) if settings.session_secret else ("", False)
    response = TEMPLATES.TemplateResponse(
        request, name, {**context, "csrf_token": token}, status_code=status_code
    )
    if is_new:
        _set_cookie(request, response, auth.CSRF_COOKIE, auth.sign_csrf(settings, token))
    return response


def _safe_next(target: str | None) -> str:
    """Only go back to a page on this site (never to another website after login)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


async def check_csrf(request: Request, settings: SettingsDep) -> None:
    """Every POST must send the token from the CSRF cookie, in the form or a header."""
    expected = auth.read_csrf(settings, request.cookies.get(auth.CSRF_COOKIE))
    form = await request.form()
    sent = form.get(auth.CSRF_FIELD) or request.headers.get(auth.CSRF_HEADER)
    if not auth.csrf_ok(expected, sent if isinstance(sent, str) else None):
        raise CsrfRejected


CsrfDep = Depends(check_csrf)


# ---------- The app ----------


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Scholarship Hunter", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings or Settings()
    app.state.login_limiter = auth.LoginRateLimiter()

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request: Request, exc: NotLoggedIn) -> Response:
        if _wants_json(request):
            return JSONResponse({"detail": "Log in first."}, status_code=401)
        if request.headers.get("hx-request"):  # HTMX: send the whole page to the login form
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?{urlencode({'next': target})}", status_code=303)

    @app.exception_handler(CsrfRejected)
    async def csrf_rejected(request: Request, exc: CsrfRejected) -> Response:
        message = "This form expired or did not come from this site. Reload the page and try again."
        if _wants_json(request) or request.headers.get("hx-request"):
            return JSONResponse({"detail": message}, status_code=403)
        return render(request, "error.html", {"title": "Form rejected", "message": message}, 403)

    async def database_problem(request: Request, message: str) -> Response:
        if _wants_json(request):
            return JSONResponse({"detail": message}, status_code=503)
        return render(request, "error.html", {"title": "Database problem", "message": message},
                      503)  # fmt: skip

    @app.exception_handler(DatabaseMissing)
    async def database_missing(request: Request, exc: DatabaseMissing) -> Response:
        return await database_problem(request, "DATABASE_URL is not set. Add it to your .env file.")

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error) -> Response:
        # The error text may contain the connection string, so it is not shown.
        message = ("Could not read from the database. Try again in a minute; "
                   "if it keeps failing, check DATABASE_URL.")  # fmt: skip
        return await database_problem(request, message)

    app.include_router(public)
    app.include_router(protected)
    return app


public = APIRouter()
protected = APIRouter(dependencies=[Depends(require_login)])


@public.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@public.get("/login", response_class=HTMLResponse)
def login_page(request: Request, settings: SettingsDep, next: str = "/") -> Response:
    if auth.read_session(settings, request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", {"next": _safe_next(next), "error": None,
                                          "setup": auth.setup_problems(settings)})  # fmt: skip


@public.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    settings: SettingsDep,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    limiter: auth.LoginRateLimiter = request.app.state.login_limiter
    key = _client_key(request)
    context: dict[str, Any] = {"next": _safe_next(next), "setup": auth.setup_problems(settings),
                               "email": email}  # fmt: skip
    if limiter.blocked(key):
        minutes = -(-limiter.retry_after_s(key) // 60)
        error = f"Too many failed logins. Try again in {minutes} minute(s)."
        response: Response = render(request, "login.html", {**context, "error": error}, 429)
        response.headers["Retry-After"] = str(limiter.retry_after_s(key))
        return response
    await check_csrf(request, settings)  # a rejected form does not count as a login try
    if not auth.check_login(settings, email, password):
        limiter.record_failure(key)
        return render(request, "login.html", {**context, "error": "Wrong email or password."}, 401)

    limiter.reset(key)
    response = RedirectResponse(_safe_next(next), status_code=303)
    _set_cookie(request, response, auth.SESSION_COOKIE, auth.make_session(settings, email.strip()))
    token = auth.new_csrf_token()  # a fresh CSRF token for the logged-in session
    _set_cookie(request, response, auth.CSRF_COOKIE, auth.sign_csrf(settings, token))
    return response


@protected.post("/logout", dependencies=[CsrfDep])
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# ---------- Data shared by pages and JSON ----------


@dataclass
class HomeSummary:
    match_counts: dict[str, int]
    upcoming: list[UpcomingDeadline]
    last_run: RunRow | None
    last_good_run_at: datetime | None
    run_overdue: bool  # no successful weekly run in the last 8 days


def home_summary(store: DashboardStore, email: str, today: date) -> HomeSummary:
    profile_id = store.profile_id(email)
    runs = store.runs(1)
    last_good = store.last_good_weekly_at()
    overdue = last_good is None or datetime.now(PKT) - last_good > WEEKLY_MAX_AGE
    return HomeSummary(
        match_counts=match_counts(store.scholarship_rows(profile_id, today)),
        upcoming=store.upcoming_deadlines(today, 5),
        last_run=runs[0] if runs else None,
        last_good_run_at=last_good,
        run_overdue=overdue,
    )


def scholarship_list(
    request: Request, store: DashboardStore, email: str, today: date
) -> list[ScholarshipRow]:
    rows = store.scholarship_rows(store.profile_id(email), today)
    return filter_rows(rows, parse_filters(request.query_params))


def find_scholarship(store: DashboardStore, email: str, sid: str) -> ScholarshipDetail | None:
    return store.scholarship(store.profile_id(email), sid)


FILTER_CHOICES = {
    "types": OPPORTUNITY_TYPES,
    "locations": LOCATIONS,
    "match_statuses": (*MATCH_STATUSES, NOT_MATCHED),
    "funding_types": FUNDING_TYPES,
}


# ---------- HTML pages ----------


@protected.get("/", response_class=HTMLResponse)
def home(request: Request, user: UserDep, store: StoreDep, today: TodayDep) -> Response:
    summary = home_summary(store, user, today)
    return render(request, "home.html", {"s": summary, "today": today, "user": user})


@protected.get("/scholarships", response_class=HTMLResponse)
def scholarships(request: Request, user: UserDep, store: StoreDep, today: TodayDep) -> Response:
    filters = parse_filters(request.query_params)
    rows = scholarship_list(request, store, user, today)
    context = {"rows": rows, "f": filters, "today": today, "user": user, **FILTER_CHOICES}
    return render(request, "scholarships.html", context)


@protected.get("/scholarships/table", response_class=HTMLResponse)
def scholarships_table(
    request: Request, user: UserDep, store: StoreDep, today: TodayDep
) -> Response:
    """Only the table, for HTMX. The address bar is updated to the matching full page."""
    filters = parse_filters(request.query_params)
    rows = scholarship_list(request, store, user, today)
    response = render(request, "_scholarship_table.html", {"rows": rows, "today": today})
    query = urlencode(filters.as_query())
    response.headers["HX-Push-Url"] = "/scholarships" + (f"?{query}" if query else "")
    return response


@protected.get("/scholarships/{sid}", response_class=HTMLResponse)
def scholarship_page(request: Request, sid: str, user: UserDep, store: StoreDep,
                     today: TodayDep) -> Response:  # fmt: skip
    detail = find_scholarship(store, user, sid)
    if detail is None:
        context = {"title": "Not found", "message": "There is no scholarship with this id.",
                   "user": user}  # fmt: skip
        return render(request, "error.html", context, 404)
    return render(request, "detail.html", {"d": detail, "today": today, "user": user})


@protected.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, user: UserDep, store: StoreDep, settings: SettingsDep) -> Response:
    return render(request, "runs.html", runs_context(store, user, settings))


def runs_context(store: DashboardStore, user: str, settings: Settings) -> dict[str, Any]:
    return {"runs": store.runs(100), "user": user, "dispatch": None,
            "github_problem": github.setup_problem(settings)}  # fmt: skip


# ---------- Actions (POST: they all need the CSRF token) ----------


def _is_htmx(request: Request) -> bool:
    return bool(request.headers.get("hx-request"))


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


@protected.post("/scholarships/{sid}/verify", dependencies=[CsrfDep])
def verify_scholarship(request: Request, sid: str, user: UserDep, store: StoreDep) -> Response:
    """Record that you checked this item against the official page yourself."""
    verified_at = store.verify(sid)
    if verified_at is None:
        return JSONResponse({"detail": "Not found."}, status_code=404)
    if not _is_htmx(request):
        return RedirectResponse(f"/scholarships/{sid}", status_code=303)
    return render(request, "_verify.html", {"sid": sid, "verified_at": verified_at})


def sources_context(store: DashboardStore, user: str, **extra: Any) -> dict[str, Any]:
    context: dict[str, Any] = {"sources": store.sources(), "user": user, "values": {},
                               "errors": [], "added": None, "added_yaml": ""}  # fmt: skip
    return context | extra


@protected.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, user: UserDep, store: StoreDep) -> Response:
    return render(request, "sources.html", sources_context(store, user))


@protected.post("/sources/{key}/active", dependencies=[CsrfDep])
async def toggle_source(request: Request, key: str, user: UserDep, store: StoreDep) -> Response:
    active = (await _form(request)).get("active") == "1"
    source = store.set_source_active(key, active)
    if source is None:
        return JSONResponse({"detail": "Not found."}, status_code=404)
    if not _is_htmx(request):
        return RedirectResponse("/sources", status_code=303)
    return render(request, "_source_card.html", {"s": source})


@protected.post("/sources", response_class=HTMLResponse, dependencies=[CsrfDep])
async def add_source(
    request: Request, user: UserDep, store: StoreDep, settings: SettingsDep
) -> Response:
    """Check the new page (https, robots.txt) and save it. The next run picks it up."""
    values = await _form(request)
    source, errors = parse_source_form(values)
    if source is not None and not settings.contact_email:
        errors = ["Set CONTACT_EMAIL in .env first: it goes in the User-Agent we send."]
    elif source is not None and not await robots_allows(source.url, settings):
        errors = ["robots.txt on this site does not allow ScholarshipHunterBot to fetch this "
                  "page (or the site could not be reached). Nothing was saved."]  # fmt: skip
    elif source is not None and not store.add_source(source):
        errors = [f"A source with key '{source.key}' or this URL already exists."]
    if errors or source is None:
        context = sources_context(store, user, values=values, errors=errors)
        return render(request, "sources.html", context, 400)
    context = sources_context(store, user, added=source, added_yaml=source_yaml(source))
    return render(request, "sources.html", context)


def profile_context(store: DashboardStore, user: str) -> tuple[str | None, ProfileRecord | None]:
    profile_id = store.profile_id(user)
    return profile_id, (store.profile(profile_id) if profile_id else None)


@protected.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: UserDep, store: StoreDep) -> Response:
    _, record = profile_context(store, user)
    values = profile_form_values(record.profile if record else None,
                                 record.emails_paused if record else False)  # fmt: skip
    saved = request.query_params.get("saved") == "1"
    return render(request, "profile.html", {"values": values, "errors": [], "saved": saved,
                                            "user": user, "is_new": record is None})  # fmt: skip


@protected.post("/profile", response_class=HTMLResponse, dependencies=[CsrfDep])
async def save_profile(request: Request, user: UserDep, store: StoreDep) -> Response:
    form = await _form(request)
    profile, paused, errors = parse_profile_form(form)
    profile_id, record = profile_context(store, user)
    if profile is not None:
        try:
            store.save_profile(profile_id if record else None, profile, paused)
        except ProfileEmailTaken:
            errors = [f"email: another profile already uses {profile.email}"]
        else:
            return RedirectResponse("/profile?saved=1", status_code=303)
    context = {"values": form, "errors": errors, "saved": False, "user": user,
               "is_new": record is None}  # fmt: skip
    return render(request, "profile.html", context, 400)


@protected.post("/runs/dispatch", response_class=HTMLResponse, dependencies=[CsrfDep])
async def run_now(
    request: Request, user: UserDep, store: StoreDep, settings: SettingsDep
) -> Response:
    """Start weekly.yml on GitHub. Answers 200 even when GitHub refused, so HTMX shows why."""
    result = await github.dispatch_weekly(settings)
    if _is_htmx(request):
        return render(request, "_dispatch.html", {"r": result})
    context = runs_context(store, user, settings) | {"dispatch": result}
    return render(request, "runs.html", context)


# ---------- JSON API ----------


@protected.get("/api/v1/summary")
def api_summary(user: UserDep, store: StoreDep, today: TodayDep) -> HomeSummary:
    return home_summary(store, user, today)


@protected.get("/api/v1/scholarships")
def api_scholarships(
    request: Request, user: UserDep, store: StoreDep, today: TodayDep
) -> list[ScholarshipRow]:
    return scholarship_list(request, store, user, today)


@protected.get("/api/v1/scholarships/{sid}", response_model=None)
def api_scholarship(sid: str, user: UserDep, store: StoreDep) -> ScholarshipDetail | Response:
    detail = find_scholarship(store, user, sid)
    if detail is None:
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return detail


@protected.get("/api/v1/sources")
def api_sources(store: StoreDep) -> list[SourceRow]:
    return store.sources()


@protected.get("/api/v1/profile", response_model=None)
def api_profile(user: UserDep, store: StoreDep) -> ProfileRecord | Response:
    _, record = profile_context(store, user)
    if record is None:
        return JSONResponse({"detail": "No profile yet."}, status_code=404)
    return record


@protected.get("/api/v1/runs")
def api_runs(store: StoreDep, limit: int = 100) -> list[RunRow]:
    return store.runs(max(1, min(limit, 500)))


app = create_app()
