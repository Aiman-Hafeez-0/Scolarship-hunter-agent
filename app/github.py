"""Start the weekly workflow on GitHub Actions ("Run now" on the dashboard).

It calls the workflow_dispatch API for .github/workflows/weekly.yml with RUN_NOW_GITHUB_TOKEN.
The token only goes in the Authorization header: it is never put in a message, log or page.
"""

import re
from dataclasses import dataclass

import httpx

from pipeline.config import Settings

API = "https://api.github.com"
WORKFLOW_FILE = "weekly.yml"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
TIMEOUT_S = 30.0


@dataclass
class DispatchResult:
    ok: bool
    message: str
    actions_url: str | None  # where to watch the run (None when the repo is not set)


def actions_url(repo: str) -> str:
    return f"https://github.com/{repo}/actions/workflows/{WORKFLOW_FILE}"


def setup_problem(settings: Settings) -> str | None:
    if not settings.run_now_github_token:
        return "RUN_NOW_GITHUB_TOKEN is not set in .env."
    if not REPO_PATTERN.fullmatch(settings.run_now_github_repo):
        return "RUN_NOW_GITHUB_REPO is missing or not in the form owner/name."
    return None


def _explain(response: httpx.Response) -> str:
    """A short reason for a failed call. GitHub's own message is shown, never the token."""
    try:
        detail = str(response.json().get("message", ""))[:200]
    except ValueError:
        detail = ""
    hints = {
        401: "GitHub did not accept the token (wrong or expired).",
        403: "The token may not start workflows: it needs Actions: Read and write.",
        404: "Repository or weekly.yml not found, or the token cannot see this repository.",
        422: "GitHub refused the request (check RUN_NOW_GITHUB_REF is a real branch).",
    }
    hint = hints.get(response.status_code, "GitHub answered with an error.")
    return f"{hint} (HTTP {response.status_code}{': ' + detail if detail else ''})"


async def dispatch_weekly(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> DispatchResult:
    """Ask GitHub to start weekly.yml now (with its default inputs)."""
    problem = setup_problem(settings)
    if problem:
        return DispatchResult(False, problem, None)
    repo = settings.run_now_github_repo
    url = f"{API}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {settings.run_now_github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ScholarshipHunterDashboard/0.1",
    }
    link = actions_url(repo)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT_S)
    try:
        body = {"ref": settings.run_now_github_ref}
        response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        return DispatchResult(False, f"Could not reach GitHub ({type(exc).__name__}).", link)
    finally:
        if owns_client:
            await client.aclose()
    if response.is_success:
        return DispatchResult(True, "Started. The run shows up on GitHub in a few seconds.", link)
    return DispatchResult(False, _explain(response), link)
