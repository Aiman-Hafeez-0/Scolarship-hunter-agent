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
| `.github/workflows/` | CI (and later, the scheduled runs) |
| `data/` | Local downloaded files (not committed) |
