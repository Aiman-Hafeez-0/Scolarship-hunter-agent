"""The dashboard web app: HTML pages plus the same data as JSON under /api/v1.

Start it with:  uv run uvicorn app.main:app --reload
On Vercel, `app` below is found automatically (see vercel.json and the README).

Only /health and /login work without logging in. Every other page and API route goes
through `require_login` (see protected = APIRouter(...) below).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import auth, github, ics, ui
from app.deadline_views import month_view, parse_month, week_groups
from app.forms import (
    parse_profile_form,
    parse_source_form,
    profile_form_values,
    robots_allows,
    source_yaml,
)
from app.merge import merge_deadlines, merge_rows, sources_of
from app.queries import (
    OPPORTUNITY_TYPES,
    DashboardStore,
    PostgresDashboardStore,
    PostgresLoginLimiter,
    ProfileEmailTaken,
    ProfileRecord,
    RecentChange,
    RunRow,
    ScholarshipDetail,
    ScholarshipRow,
    SourceRow,
    UpcomingDeadline,
    facets,
    filter_rows,
    match_counts,
    paginate,
    parse_filters,
)
from pipeline.config import Settings
from pipeline.notify.alerts import WEEKLY_MAX_AGE
from pipeline.notify.deadlines import PKT

TEMPLATES = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


# Template helpers (see app/ui.py). The look itself is described in docs/design.md.
TEMPLATES.env.filters.update(pkt=ui.pkt, day=ui.day, label=ui.label, as_json=ui.as_json,
                             ago=ui.relative_time)  # fmt: skip
TEMPLATES.env.globals.update(tone=ui.deadline_tone, days_left=ui.days_left,
                             countdown_text=ui.countdown_text, asset=ui.asset,
                             field_errors=ui.field_errors, funding_text=ui.funding_text,
                             quote_link=ui.quote_link)  # fmt: skip


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


def _connect(settings: Settings) -> psycopg.Connection[tuple[Any, ...]]:
    if not settings.database_url:
        raise DatabaseMissing
    # connect_timeout: an unreachable database shows the error page instead of hanging.
    return psycopg.connect(settings.database_url, autocommit=True, connect_timeout=10)


def get_store(settings: SettingsDep) -> Iterator[DashboardStore]:
    """One database connection per request (fine for a one-person dashboard)."""
    with _connect(settings) as conn:
        yield PostgresDashboardStore(conn)


def get_login_limiter(settings: SettingsDep) -> Iterator[auth.LoginLimiter]:
    """The login rate limit, kept in the database so all server instances share it."""
    with _connect(settings) as conn:
        yield PostgresLoginLimiter(conn)


def get_today() -> date:
    return datetime.now(PKT).date()


UserDep = Annotated[str, Depends(require_login)]
StoreDep = Annotated[DashboardStore, Depends(get_store)]
TodayDep = Annotated[date, Depends(get_today)]
LimiterDep = Annotated[auth.LoginLimiter, Depends(get_login_limiter)]


@dataclass
class Shell:
    """What the sidebar and top bar of every logged-in page show."""

    user: str
    initial: str  # the avatar letter
    last_run: RunRow | None
    run_now_problem: str | None  # why "Run now" cannot work yet (None: it can)


def get_shell(user: UserDep, store: StoreDep, settings: SettingsDep) -> Shell:
    runs = store.runs(1)
    return Shell(user, ui.initial(user), runs[0] if runs else None,
                 github.setup_problem(settings))  # fmt: skip


ShellDep = Annotated[Shell, Depends(get_shell)]


def _client_key(request: Request, settings: Settings) -> str:
    """The visitor's IP address, for the login rate limit.

    On Vercel the app sits behind Vercel's proxy, which puts the real address in
    X-Forwarded-For (Vercel replaces any value the visitor sent, so it cannot be faked).
    Anywhere else that header could be sent by anyone, so it is ignored.
    """
    if settings.vercel:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _set_cookie(
    request: Request, response: Response, name: str, value: str, keep: bool = True
) -> None:
    """keep=False: no Max-Age, so the browser deletes the cookie when it closes."""
    response.set_cookie(
        name, value, max_age=auth.SESSION_MAX_AGE_S if keep else None, path="/", httponly=True,
        samesite="lax", secure=not auth.is_local(request.url.hostname),
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
    # The built CSS, app.js and htmx (no login needed: they hold no data).
    app.mount("/static", StaticFiles(directory=ui.STATIC_DIR), name="static")

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
                                          "setup": auth.setup_problems(settings),
                                          "remember": True})  # fmt: skip


def too_many_tries(retry_after_s: int) -> str:
    minutes = -(-retry_after_s // 60)
    return f"Too many tries. Wait {minutes} minute{'' if minutes == 1 else 's'} and try again."


@public.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    settings: SettingsDep,
    limiter: LimiterDep,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
    remember: Annotated[str, Form()] = "",
) -> Response:
    key = _client_key(request, settings)
    keep = remember in ("1", "on")
    context: dict[str, Any] = {"next": _safe_next(next), "setup": auth.setup_problems(settings),
                               "email": email, "remember": keep}  # fmt: skip
    retry_after = limiter.retry_after_s(key)
    if retry_after > 0:
        error = too_many_tries(retry_after)
        response: Response = render(request, "login.html", {**context, "error": error}, 429)
        response.headers["Retry-After"] = str(retry_after)
        return response
    await check_csrf(request, settings)  # a rejected form does not count as a login try
    if not auth.check_login(settings, email, password):
        limiter.record_failure(key)
        return render(request, "login.html", {**context, "error": "Wrong email or password."}, 401)

    limiter.reset(key)
    response = RedirectResponse(_safe_next(next), status_code=303)
    session = auth.make_session(settings, email.strip(), remember=keep)
    _set_cookie(request, response, auth.SESSION_COOKIE, session, keep=keep)
    token = auth.new_csrf_token()  # a fresh CSRF token for the logged-in session
    _set_cookie(request, response, auth.CSRF_COOKIE, auth.sign_csrf(settings, token))
    return response


@protected.post("/logout", dependencies=[CsrfDep])
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


# ---------- Data shared by pages and JSON ----------


UPCOMING_ON_OVERVIEW = 10
SOON_DAYS = 14  # the "Deadlines in the next 14 days" card
LIST_ON_OVERVIEW = 5  # rows in "Recently changed" and "Needs review"
DEADLINES_PAGE_LIMIT = 300


@dataclass
class HomeSummary:
    match_counts: dict[str, int]
    upcoming: list[UpcomingDeadline]  # the next 10
    deadlines_soon: int  # deadlines in the next SOON_DAYS days (today included)
    recent_changes: list[RecentChange]
    needs_review: list[ScholarshipRow]  # open items that failed a check, soonest first
    needs_review_total: int
    last_run: RunRow | None
    last_good_run_at: datetime | None
    run_overdue: bool  # no successful weekly run in the last 8 days


def all_rows(store: DashboardStore, email: str, today: date) -> list[ScholarshipRow]:
    """Every item, with duplicates shown once (see app/merge.py)."""
    return merge_rows(store.scholarship_rows(store.profile_id(email), today))


def upcoming(store: DashboardStore, today: date) -> list[UpcomingDeadline]:
    return merge_deadlines(store.upcoming_deadlines(today, DEADLINES_PAGE_LIMIT))


def home_summary(store: DashboardStore, email: str, today: date) -> HomeSummary:
    rows = all_rows(store, email, today)
    runs = store.runs(1)
    last_good = store.last_good_weekly_at()
    overdue = last_good is None or datetime.now(PKT) - last_good > WEEKLY_MAX_AGE
    upcoming_all = upcoming(store, today)
    soon_until = today + timedelta(days=SOON_DAYS)
    review = filter_rows([r for r in rows if r.needs_review], parse_filters({}))
    return HomeSummary(
        match_counts=match_counts(rows),
        upcoming=upcoming_all[:UPCOMING_ON_OVERVIEW],
        deadlines_soon=sum(1 for d in upcoming_all if d.deadline_date <= soon_until),
        recent_changes=store.recent_changes(LIST_ON_OVERVIEW),
        needs_review=review[:LIST_ON_OVERVIEW],
        needs_review_total=len(review),
        last_run=runs[0] if runs else None,
        last_good_run_at=last_good,
        run_overdue=overdue,
    )


def scholarship_list(
    request: Request, store: DashboardStore, email: str, today: date
) -> list[ScholarshipRow]:
    return filter_rows(all_rows(store, email, today), parse_filters(request.query_params))


def opportunities_context(
    request: Request, store: DashboardStore, email: str, today: date
) -> dict[str, Any]:
    """The list page: filter chips with counts, then one page of dated items (25), and the
    items with no future deadline in a folded "No date yet" group on the last page."""
    filters = parse_filters(request.query_params)
    rows = all_rows(store, email, today)
    kept = filter_rows(rows, filters)
    dated = [r for r in kept if r.next_deadline is not None]
    page = paginate(dated, filters.page)
    undated = [r for r in kept if r.next_deadline is None] if page.number == page.pages else []
    return {"f": filters, "facets": facets(rows, filters), "page": page, "undated": undated,
            "total": len(kept), "today": today}  # fmt: skip


def find_scholarship(store: DashboardStore, email: str, sid: str) -> ScholarshipDetail | None:
    return store.scholarship(store.profile_id(email), sid)


# ---------- HTML pages ----------


@protected.get("/", response_class=HTMLResponse)
def home(
    request: Request, user: UserDep, store: StoreDep, today: TodayDep, shell: ShellDep
) -> Response:
    summary = home_summary(store, user, today)
    return render(request, "home.html", {"s": summary, "today": today, "shell": shell,
                                         "soon_days": SOON_DAYS})  # fmt: skip


@protected.get("/scholarships", response_class=HTMLResponse)
def scholarships(
    request: Request, user: UserDep, store: StoreDep, today: TodayDep, shell: ShellDep
) -> Response:
    context = opportunities_context(request, store, user, today) | {"shell": shell}
    return render(request, "scholarships.html", context)


@protected.get("/scholarships/table", response_class=HTMLResponse)
def scholarships_table(
    request: Request, user: UserDep, store: StoreDep, today: TodayDep
) -> Response:
    """Only the results (and, out of band, the filter chips with fresh counts), for HTMX.
    The address bar is updated to the matching full page."""
    context = opportunities_context(request, store, user, today) | {"oob": True}
    response = render(request, "_scholarship_table.html", context)
    response.headers["HX-Push-Url"] = context["f"].link(page=context["page"].number)
    return response


@protected.get("/scholarships/{sid}", response_class=HTMLResponse)
def scholarship_page(request: Request, sid: str, user: UserDep, store: StoreDep,
                     today: TodayDep, shell: ShellDep) -> Response:  # fmt: skip
    detail = find_scholarship(store, user, sid)
    if detail is None:
        context = {"title": "Not found", "message": "There is no scholarship with this id.",
                   "shell": shell}  # fmt: skip
        return render(request, "error.html", context, 404)
    rows = store.scholarship_rows(store.profile_id(user), today)
    context = {"d": detail, "today": today, "shell": shell, "sources": sources_of(rows, sid)}
    return render(request, "detail.html", context)


@protected.get("/scholarships/{sid}/calendar.ics")
def scholarship_calendar(sid: str, user: UserDep, store: StoreDep, d: int = 0) -> Response:
    """Deadline number `d` of this item as an .ics file ("Add to calendar")."""
    detail = find_scholarship(store, user, sid)
    deadline = detail.deadlines[d] if detail and 0 <= d < len(detail.deadlines) else None
    if detail is None or deadline is None or deadline.deadline_date is None:
        return PlainTextResponse("No such deadline.", status_code=404)
    day = deadline.deadline_date
    body = ics.deadline_event(
        uid=f"{detail.id}-{d}-{day.isoformat()}@scholarship-hunter", name=detail.name,
        label=deadline.label, day=day, url=detail.application_url or detail.source_url,
    )  # fmt: skip
    disposition = f'attachment; filename="{ics.filename(detail.name, day)}"'
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": disposition})  # fmt: skip


DEADLINE_VIEWS = ("list", "calendar")


@protected.get("/deadlines", response_class=HTMLResponse)
def deadlines_page(
    request: Request, store: StoreDep, today: TodayDep, shell: ShellDep,
    view: str = "list", type: str = "", month: str = "",
) -> Response:  # fmt: skip
    """Upcoming deadlines of open items: by week (list) or as a month calendar."""
    every = upcoming(store, today)
    kind = type if type in OPPORTUNITY_TYPES else ""
    shown = [d for d in every if not kind or d.opportunity_type == kind]
    counts = {t: sum(1 for d in every if d.opportunity_type == t) for t in OPPORTUNITY_TYPES}
    view = view if view in DEADLINE_VIEWS else "list"
    chosen = parse_month(month, today)
    context = {
        "shell": shell, "today": today, "view": view, "kind": kind, "counts": counts,
        "total": len(every), "shown": len(shown), "weeks": week_groups(shown, today),
        "cal": month_view(shown, chosen, today), "month_param": chosen.strftime("%Y-%m"),
    }  # fmt: skip
    return render(request, "deadlines.html", context)


@protected.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, store: StoreDep, shell: ShellDep) -> Response:
    return render(request, "runs.html", runs_context(store, shell))


def runs_context(store: DashboardStore, shell: Shell) -> dict[str, Any]:
    return {"runs": store.runs(100), "shell": shell, "dispatch": None,
            "github_problem": shell.run_now_problem}  # fmt: skip


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


def sources_context(store: DashboardStore, shell: Shell, **extra: Any) -> dict[str, Any]:
    context: dict[str, Any] = {"sources": store.sources(), "shell": shell, "values": {},
                               "errors": [], "added": None, "added_yaml": "",
                               "open_add": False}  # fmt: skip
    return context | extra


@protected.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, store: StoreDep, shell: ShellDep, add: str = "") -> Response:
    """?add=1 opens the "Add source" panel straight away (the Overview's quick action)."""
    return render(request, "sources.html", sources_context(store, shell, open_add=add == "1"))


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
    request: Request, store: StoreDep, settings: SettingsDep, shell: ShellDep
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
        context = sources_context(store, shell, values=values, errors=errors, open_add=True)
        return render(request, "sources.html", context, 400)
    context = sources_context(store, shell, added=source, added_yaml=source_yaml(source))
    return render(request, "sources.html", context)


def profile_context(store: DashboardStore, user: str) -> tuple[str | None, ProfileRecord | None]:
    profile_id = store.profile_id(user)
    return profile_id, (store.profile(profile_id) if profile_id else None)


@protected.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: UserDep, store: StoreDep, shell: ShellDep) -> Response:
    _, record = profile_context(store, user)
    values = profile_form_values(record.profile if record else None,
                                 record.emails_paused if record else False)  # fmt: skip
    saved = request.query_params.get("saved") == "1"
    return render(request, "profile.html", {"values": values, "errors": [], "saved": saved,
                                            "shell": shell, "is_new": record is None})  # fmt: skip


@protected.post("/profile", response_class=HTMLResponse, dependencies=[CsrfDep])
async def save_profile(
    request: Request, user: UserDep, store: StoreDep, shell: ShellDep
) -> Response:
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
    context = {"values": form, "errors": errors, "saved": False, "shell": shell,
               "is_new": record is None}  # fmt: skip
    return render(request, "profile.html", context, 400)


@protected.post("/runs/dispatch", response_class=HTMLResponse, dependencies=[CsrfDep])
async def run_now(
    request: Request, store: StoreDep, settings: SettingsDep, shell: ShellDep
) -> Response:
    """Start weekly.yml on GitHub. Answers 200 even when GitHub refused, so HTMX shows why
    (as a toast, from any page's "Run now" button)."""
    result = await github.dispatch_weekly(settings)
    if _is_htmx(request):
        return render(request, "_dispatch.html", {"r": result})
    context = runs_context(store, shell) | {"dispatch": result}
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
