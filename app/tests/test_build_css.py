"""Tests for app.build_css (the parts that need no download)."""

from pathlib import Path

import pytest

from app import build_css


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", "tailwindcss-windows-x64.exe"),
        ("Linux", "x86_64", "tailwindcss-linux-x64"),
        ("Linux", "aarch64", "tailwindcss-linux-arm64"),
        ("Darwin", "arm64", "tailwindcss-macos-arm64"),
    ],
)
def test_binary_name(system: str, machine: str, expected: str) -> None:
    assert build_css.binary_name(system, machine) == expected


def test_unknown_computer_is_refused() -> None:
    with pytest.raises(build_css.BuildError):
        build_css.binary_name("Plan9", "mips")


def test_every_binary_has_a_checksum() -> None:
    assert all(len(value) == 64 for value in build_css.CHECKSUMS.values())


def test_command_builds_minified_or_watches() -> None:
    cli = Path("tw")
    assert build_css.command(cli, watch=False)[-1] == "--minify"
    assert build_css.command(cli, watch=True)[-1] == "--watch"
    assert str(build_css.SOURCE) in build_css.command(cli, watch=False)


def test_sha256(tmp_path: Path) -> None:
    file = tmp_path / "x"
    file.write_bytes(b"abc")
    assert build_css.sha256(file).startswith("ba7816bf")
