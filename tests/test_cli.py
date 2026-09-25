from __future__ import annotations

import webbrowser
from pathlib import Path

import pytest
from typer.testing import CliRunner

import halocli.cli as halocli_cli
from conftest import ForbiddenTestSideEffect
from halocli.cli import app
from halocli.config import HaloProfile, save_profile


runner = CliRunner()


def test_help_loads() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "HaloPSA" in result.output


def test_version_loads() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.startswith("halocli ")


def test_tickets_list_help_loads() -> None:
    result = runner.invoke(app, ["tickets", "list", "--help"])

    assert result.exit_code == 0


def test_raw_write_requires_apply_and_yes() -> None:
    result = runner.invoke(app, ["raw", "POST", "/Tickets", "--data", "{}"])

    assert result.exit_code != 0


def test_configure_help_loads() -> None:
    result = runner.invoke(app, ["configure", "--help"])

    assert result.exit_code == 0


def test_auth_discover_help_loads() -> None:
    result = runner.invoke(app, ["auth", "discover", "--help"])

    assert result.exit_code == 0


def test_configure_supports_interactive_auth_mode() -> None:
    result = runner.invoke(app, ["configure", "--help"])

    assert result.exit_code == 0


def test_auth_login_refuses_without_discovery() -> None:
    # The conftest fixture isolates config to an empty tmp_path and clears HALO_*
    # env vars, so the 'thomas' profile (real or otherwise) cannot leak in and the
    # command must refuse before reaching webbrowser.open()/wait_for_callback().
    result = runner.invoke(app, ["auth", "login", "--profile", "thomas"])

    assert result.exit_code != 0
    assert "not confirmed" in result.output
    assert "Opening Halo login" not in result.output


def test_auth_login_refuses_profile_without_confirmed_discovery(tmp_path: Path) -> None:
    save_profile(
        "thomas",
        HaloProfile(
            tenant_url="https://halo.example.com",
            client_id="id",
            auth_mode="halo_interactive",
            interactive_discovered=False,
        ),
    )

    result = runner.invoke(app, ["auth", "login", "--profile", "thomas"])

    assert result.exit_code != 0
    assert "not confirmed" in result.output
    assert "Opening Halo login" not in result.output


def test_auth_login_refuses_client_credentials_profile(tmp_path: Path) -> None:
    save_profile(
        "automation",
        HaloProfile(
            tenant_url="https://halo.example.com",
            client_id="id",
            client_secret="secret",
            auth_mode="client_credentials",
            interactive_discovered=True,
        ),
    )

    result = runner.invoke(app, ["auth", "login", "--profile", "automation"])

    assert result.exit_code != 0
    assert "not confirmed" in result.output
    assert "Opening Halo login" not in result.output


def test_auth_logout_help_loads() -> None:
    result = runner.invoke(app, ["auth", "logout", "--help"])

    assert result.exit_code == 0


def test_webbrowser_open_fails_loudly_during_tests() -> None:
    # conftest replaces webbrowser.open with a guard that raises BaseException so it
    # cannot be swallowed by CliRunner; if this ever regresses, a test could pop a
    # real browser window on the developer's machine.
    with pytest.raises(ForbiddenTestSideEffect, match="webbrowser.open"):
        webbrowser.open("https://example.com")


def test_wait_for_callback_fails_loudly_during_tests() -> None:
    with pytest.raises(ForbiddenTestSideEffect, match="wait_for_callback"):
        halocli_cli.wait_for_callback(None)
