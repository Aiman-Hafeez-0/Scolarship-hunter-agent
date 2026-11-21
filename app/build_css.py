"""Build app/static/app.css with the Tailwind standalone CLI (no Node needed).

Run it after changing classes in a template or in app/static/app.js:

    uv run python -m app.build_css            # build once (minified)
    uv run python -m app.build_css --watch    # rebuild on every save while you work

The first run downloads the Tailwind CLI for your computer (about 100 MB) into .tools/,
which git ignores. The download is checked against the official SHA-256 checksum, so a
broken or changed file is never run.
"""

import argparse
import hashlib
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

VERSION = "4.1.11"
RELEASES = "https://github.com/tailwindlabs/tailwindcss/releases/download"

# From the release's sha256sums.txt.
CHECKSUMS = {
    "tailwindcss-linux-arm64": "0409aa4222969f47fa6f4160fe5387e79bf7269e7afe0e8b22f7532c98e1d314",
    "tailwindcss-linux-x64": "64805b84af4292e043ea6f86d242f191c0ac75359c1a498455dfe6c642afdbab",
    "tailwindcss-macos-arm64": "f5984b9c005c3e67841c33906c7a7c92e85e405f61e029e9bb62e880dd662e79",
    "tailwindcss-macos-x64": "76e27326506d10d50e65b751795f0537f9304ecb100abe835ec138c41774f38c",
    "tailwindcss-windows-x64.exe": (
        "6631a41de25a96eb8506f07b07ab56192df117a29849c7d7a995bd343329e900"
    ),
}

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / ".tools"
SOURCE = ROOT / "app" / "assets" / "tailwind.css"
OUTPUT = ROOT / "app" / "static" / "app.css"


class BuildError(Exception):
    pass


def binary_name(system: str, machine: str) -> str:
    """The Tailwind CLI file for this computer, e.g. 'tailwindcss-windows-x64.exe'."""
    os_name = {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(system.lower())
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        machine.lower()
    )
    name = f"tailwindcss-{os_name}-{arch}" + (".exe" if os_name == "windows" else "")
    if name not in CHECKSUMS:
        raise BuildError(f"No Tailwind CLI for {system} {machine}.")
    return name


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_cli(name: str) -> Path:
    """Download the CLI once (into .tools/) and check its checksum. Returns its path."""
    path = TOOLS_DIR / f"{VERSION}-{name}"
    if path.exists() and sha256(path) == CHECKSUMS[name]:
        return path
    TOOLS_DIR.mkdir(exist_ok=True)
    url = f"{RELEASES}/v{VERSION}/{name}"
    print(f"Downloading the Tailwind CLI {VERSION} ({name})...")
    partial = path.with_suffix(".part")
    urllib.request.urlretrieve(url, partial)  # noqa: S310 (a fixed https URL)
    if sha256(partial) != CHECKSUMS[name]:
        partial.unlink()
        raise BuildError(f"The download from {url} does not match its checksum. Not used.")
    partial.replace(path)
    path.chmod(0o755)
    return path


def command(cli: Path, watch: bool) -> list[str]:
    args = [str(cli), "--input", str(SOURCE), "--output", str(OUTPUT)]
    return args + (["--watch"] if watch else ["--minify"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build app/static/app.css with Tailwind.")
    parser.add_argument("--watch", action="store_true", help="rebuild on every change")
    args = parser.parse_args(argv)
    try:
        cli = ensure_cli(binary_name(platform.system(), platform.machine()))
    except BuildError as exc:
        print(exc, file=sys.stderr)
        return 1
    result = subprocess.run(command(cli, args.watch), cwd=ROOT, check=False)
    if result.returncode == 0 and not args.watch:
        print(f"Built {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size // 1024} KB).")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
