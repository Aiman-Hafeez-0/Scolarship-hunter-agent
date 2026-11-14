# Scholarship Hunter

A personal AI agent that checks official scholarship, university admission and entry-test pages,
extracts deadlines and requirements with an LLM (every fact backed by an exact quote from the
page), matches them to the owner's profile, and emails a weekly digest and deadline reminders.

The owner is a beginner: explain what you do in simple words, keep code clean, typed and small.

## Stack

- Now: Python 3.12+, uv, ruff, mypy, pytest.
- Later: LangGraph, Pydantic v2, Gemini (Groq fallback), Supabase Postgres via psycopg 3,
  FastAPI + Jinja2 + HTMX, Gmail SMTP, GitHub Actions cron.
- Python only. No JavaScript frameworks.

## Layout

- `pipeline/` – agent code; `pipeline/nodes/` – graph steps; `pipeline/tests/` – tests.
- `pipeline/config.py` – all settings (read from `.env`).
- `db/migrations/` – SQL migrations. `data/` – local files, gitignored.

## Rules

### Polite scraping
- Obey robots.txt (RFC 9309) for every URL before fetching it.
- At most 1 request per domain every 5 seconds.
- User-Agent: `ScholarshipHunterBot/0.1 (contact: <CONTACT_EMAIL>)` (see `Settings.user_agent`).
- 30 s timeout. 2 retries with backoff on HTTP 429, 5xx and network errors.
- Never fetch pages behind a login.

### Never invent facts
- Every extracted fact needs an exact quote from the page. No quote, no fact.

### Secrets
- Secrets live only in `.env`, which is never committed. Add new keys to `.env.example` (empty).

### Git
- Never commit or push. The owner makes every git commit; leave changes in the working tree.

### Quality
- Every new module gets tests.
- Before saying a task is done, run and pass:
  ```
  uv run ruff check .
  uv run mypy pipeline
  uv run pytest
  ```
