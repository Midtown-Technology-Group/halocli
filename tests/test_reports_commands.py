"""Tests for `reports run` and `reports clone` (issue #15).

Both are declared as ResourceOperations (so the coverage oracle verifies them
against the vendored spec and `reports --help` lists them) but carry a `handler`
because the generic dispatcher cannot express them: run shapes a multi-megabyte
execution response, clone derives its body from another report.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from halocli import schema
from halocli.cli import REPORT_ROW_CAP, app
from halocli.mcp_server import _capability_summary, _verbs
from halocli.resources import get_resource

runner = CliRunner()

# CI (and FORCE_COLOR runs) render click's rich output with ANSI styling, which
# interleaves escape codes inside option names. Strip before substring asserts.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    return _ANSI_RE.sub("", output)


def _install_mock(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """Route HaloClient through `handler`, returning the recorded requests."""
    async_client = httpx.AsyncClient
    calls: list[dict] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.url.params),
                "headers": dict(request.headers),
                "body": request.content,
            }
        )
        return handler(request)

    transport = httpx.MockTransport(recording)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    return calls


def _json_or_none(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


# ------------------------------------------------------------------ declarations


def test_reports_declares_run_and_clone() -> None:
    resource = get_resource("reports")
    ops = {op.name: op for op in resource.operations}
    assert set(ops) == {"run", "clone"}

    run = ops["run"]
    assert (run.method, run.path, run.args) == ("GET", "/Report/{id}", ("id",))
    assert run.handler == "report_run"
    assert not run.write  # reads never need --apply/--yes

    clone = ops["clone"]
    assert (clone.method, clone.path) == ("POST", "/Report")
    assert clone.handler == "report_clone"
    assert clone.write
    # Both were executed against the real tenant while building issue #15.
    assert run.verification == clone.verification == "live"


def test_declared_operations_exist_in_vendored_spec() -> None:
    """The coverage oracle's promise, asserted directly: spec backs both ops."""
    spec = schema.load_spec()
    assert spec is not None
    assert "get" in spec["paths"]["/Report/{id}"], "spec must document GET /Report/{id}"
    assert "post" in spec["paths"]["/Report"], "spec must document POST /Report"
    # loadreport is the execution flag run depends on; without it the command
    # returns a definition instead of rows.
    params = [
        p.get("name") for p in spec["paths"]["/Report/{id}"]["get"].get("parameters", [])
    ]
    assert "loadreport" in params


def test_reports_help_lists_both_commands() -> None:
    """Assert the *registered* contract string, not the rendered panel.

    Rich truncates help at the terminal width, so a long summary loses its
    trailing [verification] in rendered output -- the registered value is what
    `--help` is built from and is width-independent (same approach as
    test_registered_help_matches_contract_format in test_cli_operations.py).
    """
    from typer.main import get_command

    click_app = get_command(app)
    group = click_app.commands["reports"]
    assert {"list", "get", "run", "clone"} <= set(group.commands)

    resource = get_resource("reports")
    for op in resource.operations:
        help_text = group.commands[op.name].help
        # Pin the parts of the contract without hard-coding the separator: both
        # cli.py and the existing contract test use U+2014 (em dash), which
        # terminals commonly render as '-'. Writing the literal '-' here failed
        # exactly that way, so assert structure instead of a reconstructed string.
        assert help_text.startswith(f"{op.method} {op.path} "), help_text
        assert help_text.endswith(f"[{op.verification}]"), help_text
        assert op.summary in help_text, help_text


def test_reports_commands_render_in_help() -> None:
    """Both must survive typer's registration and reach the panel."""
    result = runner.invoke(app, ["reports", "--help"])
    assert result.exit_code == 0
    text = plain(result.output)
    for name in ("list", "get", "run", "clone"):
        assert re.search(rf"\b{re.escape(name)}\b", text), name


def test_reports_capability_advertises_clone() -> None:
    """A first-class POST must not be advertised as 'writes only via raw'."""
    resource = get_resource("reports")
    summary = _capability_summary(resource)
    assert "clone" in summary, summary
    assert "writes only via raw" not in summary, summary
    assert "GET" in _verbs(resource) and "POST" in _verbs(resource)


# ------------------------------------------------------------------------ run


def test_run_rejects_wrong_arity(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mock(monkeypatch, lambda r: httpx.Response(200, json={}))

    for argv in (["reports", "run"], ["reports", "run", "1", "extra"]):
        result = runner.invoke(app, argv)
        assert result.exit_code != 0, argv
        assert "expects 1 path argument" in plain(result.output)
    # Usage errors are pre-network: no request was made.
    assert calls == []


def test_run_rejects_non_positive_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mock(monkeypatch, lambda r: httpx.Response(200, json={}))

    result = runner.invoke(app, ["reports", "run", "147", "--limit", "0"])
    assert result.exit_code != 0
    assert "--limit must be at least 1" in plain(result.output)
    assert calls == []


def test_run_executes_and_shapes_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"Ticket ID": 1, "Subject": "Printer", "Date Opened": "2026-09-01"},
        {"Ticket ID": 2, "Subject": "Wifi", "Date Opened": "2026-09-02"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        assert request.url.path == "/api/Report/147", request.url.path
        assert request.url.params.get("loadreport") == "true", "run must execute"
        return httpx.Response(
            200,
            json={
                "id": 147,
                "name": "All Closed Tickets",
                "report": {"loaded": True, "rows": rows, "base_link": "", "table_html": "<table>"},
            },
        )

    calls = _install_mock(monkeypatch, handler)
    result = runner.invoke(app, ["reports", "run", "147"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["row_count"] == 2
    assert payload["count"] == 2
    assert payload["columns"] == ["Ticket ID", "Subject", "Date Opened"]
    assert payload["items"] == rows
    assert payload["name"] == "All Closed Tickets"
    assert payload.get("capped") is not True
    # Exactly one request beyond the token: GET /Report/147?loadreport=true
    report_calls = [c for c in calls if "/api/Report/147" in c["path"]]
    assert len(report_calls) == 1


def test_run_limit_truncates_but_reports_the_total(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"Ticket ID": i} for i in range(1, 6)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(200, json={"id": 1, "report": {"loaded": True, "rows": rows}})

    _install_mock(monkeypatch, handler)
    result = runner.invoke(app, ["reports", "run", "147", "--limit", "2"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["row_count"] == 5  # the truth
    assert payload["count"] == 2      # what we returned
    assert len(payload["items"]) == 2
    assert "--limit 5" in plain(payload.get("hint", ""))


def test_run_warns_when_halo_caps_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Halo silently truncates at 50,000 rows; the CLI must not be silent."""
    rows = [{"Ticket ID": i} for i in range(REPORT_ROW_CAP)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(200, json={"id": 1, "report": {"loaded": True, "rows": rows}})

    _install_mock(monkeypatch, handler)
    result = runner.invoke(app, ["reports", "run", "147"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["row_count"] == REPORT_ROW_CAP
    assert payload["capped"] is True
    assert "maximum" in payload["hint"]
    # The cap warning must not blow up the payload: only --limit rows are kept.
    assert len(payload["items"]) == 20


def test_run_surfaces_halo_load_error_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Halo returns HTTP 200 for a failed query; only load_error reveals it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "id": 147,
                "report": {
                    "loaded": False,
                    "load_error": "Invalid SQL Statement. Failed with error: Invalid column name 'rtisproject'.",
                    "base_link": "",
                    "table_html": "",
                },
            },
        )

    _install_mock(monkeypatch, handler)
    result = runner.invoke(app, ["reports", "run", "147"])

    assert result.exit_code == 1, "a failed query must not exit 0"
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["category"] == "validation"
    assert payload["status_code"] == 200  # what Halo actually returned
    assert "rtisproject" in payload["error"]


def test_run_rejects_apply_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """run is a read: it never takes confirmation flags."""
    calls = _install_mock(monkeypatch, lambda r: httpx.Response(200, json={}))

    result = runner.invoke(app, ["reports", "run", "147", "--apply", "--yes"])
    assert result.exit_code != 0
    assert "no such option" in plain(result.output).lower()
    assert calls == []


def test_run_rejects_non_positive_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mock(monkeypatch, lambda r: httpx.Response(200, json={}))

    result = runner.invoke(app, ["reports", "run", "147", "--timeout", "0"])
    assert result.exit_code != 0
    assert "--timeout must be greater than 0" in plain(result.output)
    assert calls == []


def test_run_timeout_never_renders_an_empty_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message-less timeout must not reach the operator as {"error": ""}.

    That was the live failure: a 30s profile timeout against a multi-megabyte
    report rendered as {"category": "unknown", "error": ""}, which says nothing.
    httpcore timeouts arrive mapped to httpx with an empty message; note that
    httpx.ReadTimeout() itself cannot even be constructed without a message
    (TypeError), so the empty-message shape is modelled locally.
    """

    class ReadTimeout(Exception):
        pass  # str() is "" for an argument-less Exception

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        raise ReadTimeout()

    _install_mock(monkeypatch, handler)
    result = runner.invoke(app, ["reports", "run", "147"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["category"] == "timeout"
    assert payload["error"], "the error must never be an empty string"
    assert "--timeout" in payload["hint"]


def test_run_gateway_timeout_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Halo 504s deterministically on a heavy report; retrying re-runs the query.

    Measured: ~60s to 504, repeatably. With the default 3 retries this failure
    would take roughly four minutes instead of one, so execution runs with
    max_retries=0 and the operator decides whether to re-run.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(504, text="Gateway Timeout")

    calls = _install_mock(monkeypatch, handler)
    result = runner.invoke(app, ["reports", "run", "147"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["category"] == "server"
    assert payload["status_code"] == 504
    assert "gateway" in payload["hint"]
    report_calls = [c for c in calls if "/api/Report/147" in c["path"]]
    assert len(report_calls) == 1, "a gateway deadline must not be retried"


# --------------------------------------------------------------------- clone


def test_clone_preview_is_zero_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mock(monkeypatch, lambda r: httpx.Response(200, json={}))

    result = runner.invoke(
        app, ["reports", "clone", "147", "--name", "Copy of closed"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["apply"] is False
    assert payload["method"] == "POST"
    assert payload["endpoint"] == "/Report"
    preview = payload["preview"]
    assert preview["source_id"] == "147"
    assert preview["name"] == "Copy of closed"
    # The fields that would otherwise make Halo update the source.
    assert "id" in preview["stripped_fields"] and "guid" in preview["stripped_fields"]
    assert calls == [], "preview must not touch the network"


def test_clone_requires_name(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mock(monkeypatch, lambda r: httpx.Response(200, json={}))

    result = runner.invoke(app, ["reports", "clone", "147"])
    assert result.exit_code != 0
    assert "--name" in plain(result.output)
    assert calls == []


def test_clone_half_confirmed_write_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mock(monkeypatch, lambda r: httpx.Response(200, json={}))

    result = runner.invoke(app, ["reports", "clone", "147", "--name", "x", "--apply"])
    assert result.exit_code != 0
    assert "--apply" in plain(result.output) and "--yes" in plain(result.output)
    assert calls == []


def _clone_fixture() -> tuple[dict, dict]:
    """Source report and the record Halo creates from an identity-stripped copy."""
    source = {
        "id": 147,
        "guid": "b2ed0965-0ddd-47cf-b887-5441e036120a",
        "name": "All Closed Tickets",
        "description": "",
        "sql": "select faultid as [Ticket ID] from faults where status=9",
        "availablefields": "Ticket ID",
        "is_published": False,
        "published_id": "",
        "builtinid": None,
        "type": 1,
        "_canupdate": True,
        "reportingperiod": 6,
    }
    created = dict(source)
    created.update({"id": 600, "guid": "1cc6af2f-7a88-454f-a44e-77ae85afc231", "name": "Copy"})
    return source, created


def test_clone_posts_an_array_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    source, created = _clone_fixture()
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        if request.method == "POST":
            seen["content_type"] = request.headers.get("content-type", "")
            seen["body"] = _json_or_none(request.content)
            created["name"] = "Copy of closed"
            return httpx.Response(201, json=created)
        # Both GETs (source before, source after, new record) return the source
        # shape; the new-record read is distinguished by its path below.
        if request.url.path.endswith("/Report/600"):
            record = dict(created)
            record["name"] = "Copy of closed"
            return httpx.Response(200, json=record)
        return httpx.Response(200, json=source)

    calls = _install_mock(monkeypatch, handler)
    result = runner.invoke(
        app, ["reports", "clone", "147", "--name", "Copy of closed", "--apply", "--yes"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["apply"] is True
    assert payload["id"] == 600

    # Halo wants AnalyzerProfile[] and a JSON content type: an object returns 400
    # and a missing content type returns 415 (both hit while building this).
    assert seen["content_type"].startswith("application/json")
    body = seen["body"]
    assert isinstance(body, list) and len(body) == 1, "POST body must be a one-element array"
    copy = body[0]
    assert "id" not in copy and "guid" not in copy, "identity fields must be stripped"
    assert copy["name"] == "Copy of closed"
    assert copy["sql"] == source["sql"], "SQL must be copied verbatim"

    # Verification, not trust: source re-read and compared, new record read back.
    assert payload["source_unchanged"] is True
    assert payload["sql_copied"] is True
    assert payload["applied_name"] is True
    assert payload["new_guid_differs"] is True

    paths = [c["path"] for c in calls]
    assert paths.count("/api/Report/147") == 2, "source read before and after"
    assert paths.count("/api/Report/600") == 1, "created record read back"


def test_run_reports_columns_for_an_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero-row run still has a defined shape, and Halo echoes it.

    `availablefields` was verified present in the execution response for
    reports 5/143/146, matching row keys exactly there. Without the fallback a
    empty result reported no columns at all, so --output table rendered nothing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "id": 9,
                "name": "No matches",
                "availablefields": "Ticket ID\nSubject\nStatus",
                "report": {"loaded": True, "rows": []},
            },
        )

    _install_mock(monkeypatch, handler)
    result = runner.invoke(app, ["reports", "run", "9"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["row_count"] == 0
    assert payload["count"] == 0
    assert payload["columns"] == ["Ticket ID", "Subject", "Status"]
    assert payload["items"] == []


def test_clone_verification_failure_still_reports_the_new_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the POST succeeds a copy exists: a bare error invites a duplicate.

    The verification reads can fail on a 401/5xx/timeout. Swallowing that into
    a generic error would hide the new id, and an operator who retries would
    create a second report. Found by review on PR #18.
    """
    source, created = _clone_fixture()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        if request.method == "POST":
            created["name"] = "Copy of closed"
            return httpx.Response(201, json=created)
        if request.url.path.endswith("/Report/600"):
            return httpx.Response(403, text="")  # verification read fails
        return httpx.Response(200, json=source)

    _install_mock(monkeypatch, handler)
    result = runner.invoke(
        app, ["reports", "clone", "147", "--name", "Copy of closed", "--apply", "--yes"]
    )

    assert result.exit_code == 1, "a failed verification must not report success"
    payload = json.loads(result.output)
    assert payload["ok"] is False
    # The id is the whole point: without it a retry duplicates the clone.
    assert payload["id"] == 600
    assert payload["category"] == "permission"
    assert payload["status_code"] == 403
    assert payload["verification"] == "skipped"
    assert "WAS created" in payload["hint"] and "second copy" in payload["hint"]
    # Permission failures keep their actionable diagnostic.
    assert "diagnostic" in payload and payload["diagnostic"]


def test_clone_reports_source_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the source changes under us, say so instead of reporting success."""
    source, created = _clone_fixture()
    mutated = dict(source)
    mutated["name"] = "Renamed by someone else"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        if request.method == "POST":
            return httpx.Response(201, json=created)
        if request.url.path.endswith("/Report/600"):
            return httpx.Response(200, json=created)
        # Source re-read returns a *different* name the second time.
        if handler.calls == 0:
            handler.calls = 1
            return httpx.Response(200, json=source)
        return httpx.Response(200, json=mutated)

    handler.calls = 0
    _install_mock(monkeypatch, handler)
    result = runner.invoke(
        app, ["reports", "clone", "147", "--name", "Copy", "--apply", "--yes"]
    )

    assert result.exit_code == 1, "a mutated source must not report success"
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["source_unchanged"] is False
    assert "name" in payload["source_changed_fields"]
