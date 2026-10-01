"""Integration tests for the write/preview, search, serve and spec-validated raw commands."""

from __future__ import annotations

import io
import json
import re

from typer.testing import CliRunner

from halocli import mcp_server
from halocli.cli import _split_spec_problems, app
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


def test_split_spec_problems_classifies_path_prefixed_warnings() -> None:
    """Array-body problems are path-prefixed; dict-body problems are not.

    The old startswith("warning: ") split treated every array-body warning as
    fatal (issue #28: the documented spec_warnings flow never happened for
    `POST /Users` array probes until --no-validate).
    """
    warnings, fatal = _split_spec_problems(
        [
            "body[0]: warning: unknown request-body property: twofactor_enabled",
            "warning: unknown request-body property: legacy_field",
            "missing required request-body property: name",
            "unknown endpoint: POST /Nope",
        ]
    )
    assert warnings == [
        "body[0]: warning: unknown request-body property: twofactor_enabled",
        "warning: unknown request-body property: legacy_field",
    ]
    assert fatal == [
        "missing required request-body property: name",
        "unknown endpoint: POST /Nope",
    ]


def test_raw_array_body_warning_is_not_fatal() -> None:
    """Regression: an unknown property inside an ARRAY body warns, not refuses.

    The command must clear spec validation (reaching the profile stage, like
    the --no-validate precedent) instead of exiting with
    "spec validation failed".
    """
    data = json.dumps([{"id": 1, "twofactor_enabled": True}])
    result = runner.invoke(
        app,
        ["raw", "POST", "/Users", "--data", data, "--apply", "--yes"],
    )

    assert result.exit_code == 1
    output = plain(result.output)
    assert "spec validation failed" not in output
    assert "unknown endpoint" not in output
    assert "profile" in output.lower()  # validation passed; config load is next


def test_raw_path_containing_warning_marker_stays_fatal() -> None:
    """A caller-crafted path must not downgrade unknown-endpoint to advisory.

    CodeRabbit review on PR #29: substring-based classification let path
    text containing "warning: " silence the refusal, letting an unspecced
    request proceed under default validation. Only the validator's own
    body[N] prefix is stripped before the warning check.
    """
    result = runner.invoke(app, ["raw", "GET", "/nope/warning: bypass"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["category"] == "validation"
    assert any("unknown endpoint" in problem for problem in payload["problems"])


def test_users_update_masks_password_in_preview_output() -> None:
    """Credential values never reach stdout; the request shape stays readable.

    CodeRabbit Medium on PR #29: `users update --data '{"new_password":…}'`
    echoed the submitted password in preview and post-apply output.
    """
    secret = "S3cret!value99"
    result = runner.invoke(
        app,
        ["users", "update", "4266", "--data", json.dumps({"new_password": secret})],
    )

    assert result.exit_code == 0, result.output
    assert secret not in result.output
    payload = json.loads(result.output)
    assert payload["payload"]["new_password"] == "***"
    assert payload["payload"]["id"] == 4266


def test_malformed_data_on_write_paths_is_a_clean_error() -> None:
    """Bad --data renders a validation payload instead of a raw traceback.

    Found on the installed 1.1.0: resource create/update (and raw) called
    _load_body directly, so a malformed inline body raised JSONDecodeError
    uncaught (full traceback on stderr). All --data paths now share the
    guarded loader; json.loads succeeding here proves the output is the
    structured error, not a crash dump.
    """
    for argv in (
        ["users", "update", "4266", "--data", "{not json"],
        ["users", "create", "--data", "{not json"],
        ["raw", "POST", "/Users", "--data", "{not json", "--apply", "--yes"],
    ):
        result = runner.invoke(app, argv)

        assert result.exit_code == 1, argv
        payload = json.loads(result.output)
        assert payload["ok"] is False, argv
        assert payload["category"] == "validation", argv
        assert "Invalid --data" in payload["error"], argv
