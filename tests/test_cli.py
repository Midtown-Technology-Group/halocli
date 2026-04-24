from __future__ import annotations

from typer.testing import CliRunner

from halocli.cli import app


runner = CliRunner()


def test_help_loads() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "HaloPSA" in result.output


def test_tickets_list_help_loads() -> None:
    result = runner.invoke(app, ["tickets", "list", "--help"])

    assert result.exit_code == 0
    assert "--max-records" in result.output


def test_raw_write_requires_apply_and_yes() -> None:
    result = runner.invoke(app, ["raw", "POST", "/Tickets", "--data", "{}"])

    assert result.exit_code != 0
    assert "without --apply --yes" in result.output


def test_configure_help_loads() -> None:
    result = runner.invoke(app, ["configure", "--help"])

    assert result.exit_code == 0
    assert "--tenant-url" in result.output


def test_auth_discover_help_loads() -> None:
    result = runner.invoke(app, ["auth", "discover", "--help"])

    assert result.exit_code == 0
    assert "--tenant-url" in result.output


def test_configure_supports_interactive_auth_mode() -> None:
    result = runner.invoke(app, ["configure", "--help"])

    assert result.exit_code == 0
    assert "--auth-mode" in result.output
    assert "halo-interactive" in result.output


def test_auth_login_refuses_without_discovery() -> None:
    result = runner.invoke(app, ["auth", "login", "--profile", "jack"])

    assert result.exit_code != 0
    assert "not confirmed" in result.output


def test_auth_logout_help_loads() -> None:
    result = runner.invoke(app, ["auth", "logout", "--help"])

    assert result.exit_code == 0
