"""Make the password hash for the dashboard login.

Run it with:  uv run python -m app.hash_password

It asks for a password twice (nothing is shown while you type) and prints lines to paste
into .env. The password itself is never saved anywhere.
"""

import getpass
import secrets
import sys
from collections.abc import Callable

from app.auth import hash_password

MIN_PASSWORD_LENGTH = 12


def run(ask: Callable[[str], str] = getpass.getpass) -> tuple[str, int]:
    """Returns (what to print, exit code). `ask` reads one password (tests pass a fake)."""
    password = ask("Dashboard password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters.", 1
    if ask("Type it again: ") != password:
        return "The two passwords are different. Nothing was changed.", 1
    # Single quotes keep the $ signs in the hash exactly as they are.
    lines = [
        "Add these lines to .env (keep SESSION_SECRET if you already have one):",
        "",
        f"DASHBOARD_PASSWORD_HASH='{hash_password(password)}'",
        f"SESSION_SECRET={secrets.token_urlsafe(48)}",
    ]
    return "\n".join(lines), 0


def main() -> int:
    output, code = run()
    print(output, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
