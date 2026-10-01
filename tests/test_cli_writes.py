"""Integration tests for the write/preview, search, serve and spec-validated raw commands."""

from __future__ import annotations

import io
import json
import re

from typer.testing import CliRunner

from halocli import mcp_server
from halocli.cli import app
from halocli.resources import RESOURCE_BY_COMMAND

runner = CliRunner()

# CI (and FORCE_COLOR runs) render click's rich errors with ANSI styling, which
# interleaves escape codes inside option names ("--apply --yes"). Strip them
# before substring assertions so tests do not depend on the color environment.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    return _ANSI_RE.sub("", output)


def test_catalog_returns_ranked_registry_results() -> None:
    result = runner.invoke(app, ["catalog", "tickets"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["count"] > 0
    assert payload["results"][0]["name"] == "tickets"
    assert payload["results"][0]["source"] == "registry"


def test_catalog_limit_is_honored() -> None:
    result = runner.invoke(app, ["catalog", "ticket", "--limit", "2"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] <= 2


def test_serve_lists_tools_over_stdio() -> None:
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    stdout = io.StringIO()
    mcp_server.main(input_stream=stdin, output_stream=stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    assert len(responses) == 2
    tools = responses[1]["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["halo_search", "halo_execute", "halo_resources"]
    assert all(tool["description"] for tool in tools)


def test_write_preview_makes_no_network_calls() -> None:
    # Preview must not need a profile: config is isolated by conftest and no
    # profile exists, so any load_profile() call would fail this test.
    result = runner.invoke(
        app,
        ["tickets", "create", "--data", json.dumps({"summary": "preview me"})],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["apply"] is False
    assert payload["method"] == "POST"
    assert payload["endpoint"] == "/Tickets"
    assert payload["payload"]["summary"] == "preview me"


def test_write_validation_failure_exits_nonzero_in_preview() -> None:
    result = runner.invoke(app, ["tickets", "create", "--data", "{}"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert any("summary" in problem for problem in payload["errors"])


def test_write_requires_both_apply_and_yes() -> None:
    payload = json.dumps({"summary": "half confirmed"})
    for flags in (["--apply"], ["--yes"]):
        result = runner.invoke(
            app, ["tickets", "create", "--data", payload, *flags]
        )
        assert result.exit_code != 0
        assert "--apply --yes" in plain(result.output)


def test_update_injects_item_id_into_payload() -> None:
    result = runner.invoke(
        app,
        ["tickets", "update", "42", "--data", json.dumps({"summary": "renamed"})],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is False
    assert payload["payload"]["id"] == 42


def test_delete_preview_is_dry_run() -> None:
    result = runner.invoke(app, ["tickets", "delete", "42"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is False
    assert payload["method"] == "DELETE"
    assert payload["endpoint"] == "/Tickets/42"


def test_read_only_resources_have_no_write_commands() -> None:
    # contracts supports create/update but not delete; a resource with no write
    # metadata at all must expose only list/get.
    result = runner.invoke(app, ["contracts", "delete", "1"])

    assert result.exit_code != 0  # no such command
    assert RESOURCE_BY_COMMAND["contracts"].supports_delete is False


def test_raw_refuses_unknown_endpoint_via_spec() -> None:
    result = runner.invoke(app, ["raw", "GET", "/DefinitelyNotARealEndpoint"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["category"] == "validation"
    assert any("unknown endpoint" in problem for problem in payload["problems"])


def test_raw_no_validate_bypasses_spec_check() -> None:
    # Without a profile this now fails at config load instead of spec validation,
    # proving the spec check was skipped.
    result = runner.invoke(
        app, ["raw", "GET", "/DefinitelyNotARealEndpoint", "--no-validate"]
    )

    assert result.exit_code == 1
    output = plain(result.output)
    assert "unknown endpoint" not in output
    assert "profile" in output.lower()


def test_raw_write_gate_still_applies_before_spec_check() -> None:
    result = runner.invoke(app, ["raw", "POST", "/Tickets", "--data", "{}"])

    assert result.exit_code != 0
    assert "--apply --yes" in plain(result.output)
