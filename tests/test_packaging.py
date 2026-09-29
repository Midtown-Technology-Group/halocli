"""Packaging guards: the frozen exe must be able to find its own entry point.

Regression this file exists for: every MSI from 0.5.0 to 0.8.0 shipped a
`halocli.exe` that exited 1 with "Console script entry point not found" on
every command. The build itself succeeded — PyInstaller only WARNs and skips
when a package it is asked to bundle is absent — so nothing failed until a
human ran the binary.

Two independent checks:

* the installed distribution really does expose the ``halocli`` console script
  (the thing ``halocli_launcher.py`` looks up at runtime);
* the PyInstaller spec still bundles ``halocli-*.dist-info`` via
  ``copy_metadata``, without which the lookup cannot succeed in a frozen exe.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "packaging" / "windows" / "halocli.spec"
LAUNCHER_PATH = REPO_ROOT / "packaging" / "windows" / "halocli_launcher.py"


def test_console_script_entry_point_is_discoverable() -> None:
    """What halocli_launcher.py does at runtime must actually resolve."""
    names = [ep.name for ep in entry_points(group="console_scripts")]
    assert "halocli" in names, (
        "importlib.metadata cannot see a 'halocli' console script; the frozen "
        "launcher would exit 1 with 'Console script entry point not found'. "
        f"Found: {sorted(names)}"
    )


def test_launcher_resolves_via_importlib_metadata() -> None:
    """Pin the launcher's mechanism so this test keeps meaning what it says."""
    source = LAUNCHER_PATH.read_text(encoding="utf-8")
    assert "entry_points" in source
    assert "console_scripts" in source
    assert "halocli" in source


def test_pyinstaller_spec_bundles_dist_info_metadata() -> None:
    """collect_data_files() does not include dist-info; copy_metadata does.

    Without this line the frozen exe cannot resolve its entry point, and
    PyInstaller only emits a WARNING for the missing data — the build still
    exits 0. build-msi.ps1 smoke-tests the resulting binary, and this test
    fails in PR CI so the omission is caught before a release is even tagged.
    """
    source = SPEC_PATH.read_text(encoding="utf-8")
    assert 'copy_metadata("halocli")' in source, (
        "packaging/windows/halocli.spec must call copy_metadata(\"halocli\"): "
        "collect_data_files() does not bundle halocli-*.dist-info, and "
        "halocli_launcher.py resolves the console script through "
        "importlib.metadata. Removing this ships an exe that exits 1 on every "
        "command (as every MSI from 0.5.0 to 0.8.0 did)."
    )
    # Guard against the call being present but commented out.
    active = [
        line
        for line in source.splitlines()
        if 'copy_metadata("halocli")' in line and not line.lstrip().startswith("#")
    ]
    assert active, "copy_metadata(\"halocli\") appears only in comments"


def test_msi_build_smoke_tests_the_binary() -> None:
    """The MSI build must verify the exe it just produced.

    PyInstaller exits 0 even when the bundled data is incomplete, so the only
    thing standing between a broken frozen exe and a release asset is an
    explicit run of the binary.
    """
    script = (REPO_ROOT / "packaging" / "windows" / "build-msi.ps1").read_text(encoding="utf-8")
    assert "--version" in script, "build-msi.ps1 must run halocli.exe --version"
    assert "Smoke test failed" in script, (
        "build-msi.ps1 must fail the build when the smoke test fails"
    )
