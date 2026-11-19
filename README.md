# Scholarship Hunter

A personal AI agent that watches official scholarship, admission and entry-test pages, pulls out
deadlines and requirements (each backed by an exact quote from the page), matches them to your
profile, and emails you a weekly digest and deadline reminders.

## Setup

1. **Install uv** (the Python package manager):
   - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
   - macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. **Install dependencies** (uv also downloads Python 3.12 if needed):
   ```
   uv sync
   ```
3. **Create your `.env`**: copy `.env.example` to `.env` and fill in the values. Each line has a
   comment saying where to get it. Never commit `.env`.
4. **Check everything works**:
   ```
   uv run ruff check .
   uv run mypy pipeline app
   uv run pytest
   ```

## Project layout

| Path | What it is |
| --- | --- |
| `pipeline/` | Agent code |
| `pipeline/nodes/` | Individual steps of the agent |
| `pipeline/tests/` | Tests |
| `pipeline/config.py` | Settings, read from `.env` |
| `app/` | The dashboard (FastAPI web app) and its tests |
| `db/migrations/` | Database migrations |
| `.github/workflows/` | CI and the scheduled runs |
| `data/` | Local downloaded files (not committed) |

## Dashboard

A small website that shows what the agent found: your matches, the next deadlines, every fact
with the quote it came from, the sources and the run history. You can also mark items as
checked, switch sources on or off, edit your profile and start a run.

### Run it on your computer

1. Make your login. It asks for a password twice and prints two lines:
   ```
   uv run python -m app.hash_password
   ```
2. Put those two lines in `.env`, plus `DASHBOARD_EMAIL=` with your email. `DATABASE_URL` must
   already be there. For the "Run now" button, also fill in `GITHUB_TOKEN` and `GITHUB_REPO`.
3. Start it:
   ```
   uv run uvicorn app.main:app --reload
   ```
4. Open <http://localhost:8000> and log in.

### Put it online with Render (free)

1. Push the repository to GitHub (with `Dockerfile` and `render.yaml`).
2. Sign in at <https://render.com> with your GitHub account.
3. Click **New > Blueprint**, pick this repository, and click **Connect**. Render reads
   `render.yaml` and plans one web service, `scholarship-hunter-dashboard`.
4. Render asks for a value for each environment variable. Paste them from your `.env`:
   - `DATABASE_URL`: the Supabase **Session pooler** URI (the direct one does not work on Render).
   - `DASHBOARD_EMAIL`, `DASHBOARD_PASSWORD_HASH`, `SESSION_SECRET`: your login (step 1 above).
     The hash may be pasted with or without its quotes.
   - `CONTACT_EMAIL`: your email (sent to websites when you add a source).
   - `GITHUB_TOKEN`, `GITHUB_REPO`: for "Run now" (leave empty if you do not want it).
5. Click **Apply**. The first build takes a few minutes. When it says **Live**, open the
   `https://...onrender.com` address shown at the top of the service page.
6. Later changes: every push to GitHub deploys again. Change a value under the service's
   **Environment** tab (it restarts by itself).

> **The free service sleeps.** After 15 minutes with no visits Render stops it, and the next
> visit starts it again, so the **first page takes about a minute** to load. After that it is fast.

Good to know:
- Use a long, unique password. On Render, failed logins from everyone are counted together
  (5 per 15 minutes), so someone guessing can lock you out for up to 15 minutes.
- Pages are slower if Render and Supabase are far apart. `render.yaml` picks Singapore, the
  same region as the database (`ap-southeast-1`).

### End-to-end test (optional)

A browser test logs in, opens the list, opens a detail page and presses the verify button. It
needs a real browser, so it is skipped unless `E2E=1` (CI skips it):
```
uv sync --group e2e
uv run --group e2e playwright install chromium
E2E=1 uv run --group e2e pytest app/tests/test_e2e.py
```
(In PowerShell: `$env:E2E = "1"` first, then the `pytest` line without `E2E=1`.)
It needs internet access, because the pages load HTMX and Tailwind from a CDN.

## Running in production

Two GitHub Actions workflows run on their own. Both can also be started by hand from the
**Actions** tab ("Run workflow").

| Workflow | When (Pakistan time) | What it does |
| --- | --- | --- |
| `daily.yml` | Every day, 07:47 PKT | Deadline reminders (30, 14, 7 and 2 days before) and admin alerts |
| `weekly.yml` | **Trial week:** every day, 08:17 PKT. **From 28 Nov 2026:** Mondays, 08:17 PKT | Fetch, extract and save every source, match profiles, then send the digest (Mondays only, or when you tick `send_digest` in a manual run) |

After 27 Nov 2026, change the cron line in `weekly.yml` from `"17 3 * * *"` to
`"17 3 * * 1"`. GitHub cron times are in UTC (PKT = UTC+5), and a scheduled run can start a
few minutes late.

A third workflow, `test-email.yml` (manual only), sends one test email to `ADMIN_EMAIL` so you
can check the mail secrets. It uses `MAIL_PROVIDER`, `RESEND_API_KEY` and `ADMIN_EMAIL` (or the
Gmail ones).

### Secrets

Add these under **Settings > Secrets and variables > Actions > New repository secret**. Each one
is described in `.env.example`.

| Secret | Needed for |
| --- | --- |
| `DATABASE_URL` | Both workflows |
| `CONTACT_EMAIL` | Both (shown in the bot's User-Agent) |
| `GEMINI_API_KEY` | Weekly (main LLM) |
| `GROQ_API_KEY` | Weekly (fallback LLM) |
| `MAIL_PROVIDER` | Both: `resend` (the default if empty) or `gmail` |
| `RESEND_API_KEY` | Both, when `MAIL_PROVIDER` is `resend` |
| `GMAIL_ADDRESS` | Both, when `MAIL_PROVIDER` is `gmail` |
| `GMAIL_APP_PASSWORD` | Both, when `MAIL_PROVIDER` is `gmail` |
| `ADMIN_EMAIL` | Both (admin alerts; without it, alerts are skipped) |
| `MONTHLY_TOKEN_BUDGET` | Optional, daily: LLM token budget per month (empty = 1,000,000) |

> **Warning:** in a **public** repository, GitHub turns off scheduled workflows after 60 days
> with no commits. GitHub emails you before it happens. To turn a workflow back on, open it in
> the Actions tab and click **Enable workflow**, or push any commit.
