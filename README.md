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
   uv run mypy pipeline
   uv run pytest
   ```

## Project layout

| Path | What it is |
| --- | --- |
| `pipeline/` | Agent code |
| `pipeline/nodes/` | Individual steps of the agent |
| `pipeline/tests/` | Tests |
| `pipeline/config.py` | Settings, read from `.env` |
| `db/migrations/` | Database migrations |
| `.github/workflows/` | CI and the scheduled runs |
| `data/` | Local downloaded files (not committed) |

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
