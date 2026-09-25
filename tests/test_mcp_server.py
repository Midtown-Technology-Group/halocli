from __future__ import annotations

import io
import json
from typing import Any

from halocli import mcp_server
from halocli.errors import HaloCLIError
from halocli.resources import RESOURCES


class FakeClient:
    """Stands in for HaloClient so tests never touch the network."""

    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        self.calls.append({"method": method, "path": path, "params": params, "body": body})
        if self.error is not None:
            raise self.error
        return self.response


def run_server(*lines: str) -> list[dict[str, Any]]:
    stdin = io.StringIO("".join(f"{line}\n" for line in lines))
    stdout = io.StringIO()
    mcp_server.main(input_stream=stdin, output_stream=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def rpc(method: str, params: dict[str, Any] | None = None, *, msg_id: int = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    responses = run_server(json.dumps(message))
    assert len(responses) == 1, responses
    return responses[0]


def call_tool(name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    response = rpc("tools/call", {"name": name, "arguments": arguments})
    assert "result" in response, response
    result = response["result"]
    assert result["content"][0]["type"] == "text"
    return result, json.loads(result["content"][0]["text"])


def install_fake(monkeypatch: Any, fake: FakeClient) -> None:
    monkeypatch.setattr(mcp_server, "_client_for", lambda profile: fake)


# --------------------------------------------------------------------------------------
# initialize / notifications / tools/list
# --------------------------------------------------------------------------------------
def test_initialize_handshake_echoes_client_version() -> None:
    response = rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    )
    result = response["result"]
    assert response["jsonrpc"] == "2.0"
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "halocli"
    assert result["serverInfo"]["version"]


def test_initialize_defaults_to_latest_protocol_version() -> None:
    response = rpc("initialize", {})
    assert response["result"]["protocolVersion"] == mcp_server.LATEST_PROTOCOL_VERSION


def test_initialize_empty_protocol_version_returns_latest() -> None:
    response = rpc("initialize", {"protocolVersion": ""})
    assert response["result"]["protocolVersion"] == mcp_server.LATEST_PROTOCOL_VERSION


def test_initialize_unsupported_protocol_version_negotiates_to_latest() -> None:
    response = rpc(
        "initialize",
        {
            "protocolVersion": "1999-01-01",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    )
    result = response["result"]
    assert result["protocolVersion"] == mcp_server.LATEST_PROTOCOL_VERSION
    assert result["protocolVersion"] != "1999-01-01"
    # Every other initialize field stays unchanged.
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "halocli"


def test_latest_protocol_version_is_supported() -> None:
    assert mcp_server.LATEST_PROTOCOL_VERSION in mcp_server.SUPPORTED_PROTOCOL_VERSIONS
    assert "2024-11-05" in mcp_server.SUPPORTED_PROTOCOL_VERSIONS
    assert "2025-03-26" in mcp_server.SUPPORTED_PROTOCOL_VERSIONS


def test_initialized_notification_produces_no_response() -> None:
    responses = run_server(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    )
    # Only the initialize request (id 1) and the ping request get responses.
    assert len(responses) == 2
    assert responses[0]["id"] == 1
    assert responses[1]["result"] == {}


def test_tools_list_returns_exactly_three_tools() -> None:
    result = rpc("tools/list")["result"]
    tools = result["tools"]
    assert {tool["name"] for tool in tools} == {
        "halo_search",
        "halo_execute",
        "halo_resources",
    }
    for tool in tools:
        assert tool["description"].strip(), tool["name"]
        assert tool["inputSchema"]["type"] == "object"
    execute = next(tool for tool in tools if tool["name"] == "halo_execute")
    assert execute["inputSchema"]["properties"]["method"]["enum"] == list(
        mcp_server.ALLOWED_METHODS
    )
    assert set(execute["inputSchema"]["required"]) == {"method", "path"}


# --------------------------------------------------------------------------------------
# halo_search / halo_resources
# --------------------------------------------------------------------------------------
def test_halo_search_finds_tickets() -> None:
    result, payload = call_tool("halo_search", {"query": "tickets"})
    assert "isError" not in result
    assert payload["ok"] is True
    assert payload["results"], payload
    top = payload["results"][0]
    assert top["name"] == "tickets"
    assert top["endpoint"] == "/Tickets"
    assert top["source"] == "registry"
    assert top["capabilities"]
    assert "GET" in top["verbs"]


def test_halo_search_clamps_limit() -> None:
    _, payload = call_tool("halo_search", {"query": "e", "limit": 500})
    assert payload["count"] <= mcp_server.MAX_SEARCH_LIMIT


def test_halo_search_requires_query() -> None:
    result, payload = call_tool("halo_search", {})
    assert result["isError"] is True
    assert payload["ok"] is False
    assert payload["category"] == "validation"


def test_halo_resources_dumps_full_catalog() -> None:
    _, payload = call_tool("halo_resources", {})
    assert payload["ok"] is True
    assert payload["count"] == len(RESOURCES)
    assert payload["count"] >= 32
    by_name = {entry["name"]: entry for entry in payload["resources"]}
    assert by_name["tickets"]["endpoint"] == "/Tickets"
    assert by_name["tickets"]["table_fields"]
    assert "GET" in by_name["tickets"]["verbs"]
    assert by_name["quotations"]["aliases"] == ["quotation", "quotes", "quote"]


# --------------------------------------------------------------------------------------
# halo_execute: guardrails, execution, truncation, errors
# --------------------------------------------------------------------------------------
def test_execute_get_uses_client_and_returns_ok(monkeypatch: Any) -> None:
    fake = FakeClient(response={"items": [{"id": 7}]})
    install_fake(monkeypatch, fake)
    result, payload = call_tool(
        "halo_execute",
        {"method": "get", "path": "/Tickets", "params": {"take": "5"}},
    )
    assert "isError" not in result
    assert payload["ok"] is True
    assert payload["method"] == "GET"
    assert payload["path"] == "/Tickets"
    assert payload["data"] == {"items": [{"id": 7}]}
    assert fake.calls == [
        {"method": "GET", "path": "/Tickets", "params": {"take": "5"}, "body": None}
    ]


def test_execute_post_without_apply_refuses_without_client_call(monkeypatch: Any) -> None:
    fake = FakeClient(response={"id": 1})
    install_fake(monkeypatch, fake)
    result, payload = call_tool(
        "halo_execute",
        {"method": "POST", "path": "/Tickets", "body": {"summary": "nope"}},
    )
    assert result["isError"] is True
    assert payload["ok"] is False
    assert payload["refused"] is True
    assert payload["category"] == "guardrail"
    assert "apply: true" in payload["hint"]
    assert fake.calls == []


def test_execute_post_with_apply_calls_client(monkeypatch: Any) -> None:
    fake = FakeClient(response={"id": 42})
    install_fake(monkeypatch, fake)
    result, payload = call_tool(
        "halo_execute",
        {
            "method": "POST",
            "path": "/Tickets",
            "body": {"summary": "hi"},
            "apply": True,
        },
    )
    assert "isError" not in result
    assert payload["ok"] is True
    assert payload["data"] == {"id": 42}
    assert len(fake.calls) == 1
    assert fake.calls[0]["method"] == "POST"
    assert fake.calls[0]["body"] == {"summary": "hi"}


def test_execute_rejects_disallowed_method(monkeypatch: Any) -> None:
    fake = FakeClient(response={})
    install_fake(monkeypatch, fake)
    result, payload = call_tool("halo_execute", {"method": "TRACE", "path": "/Tickets"})
    assert result["isError"] is True
    assert payload["category"] == "validation"
    assert fake.calls == []


def test_execute_truncates_oversized_payload(monkeypatch: Any) -> None:
    fake = FakeClient(response={"items": ["x" * 60_000]})
    install_fake(monkeypatch, fake)
    _, payload = call_tool("halo_execute", {"method": "GET", "path": "/Tickets"})
    assert payload["truncated"] is True
    assert payload["total_chars"] > mcp_server.MAX_RESPONSE_CHARS
    assert len(payload["preview"]) == mcp_server.MAX_RESPONSE_CHARS
    assert payload["preview"].startswith('{"ok"')


def test_execute_wraps_halo_errors_as_payload(monkeypatch: Any) -> None:
    error = HaloCLIError(
        "HaloPSA not_found error (404) on /api/Tickets/9",
        category="not_found",
        status_code=404,
    )
    fake = FakeClient(error=error)
    install_fake(monkeypatch, fake)
    result, payload = call_tool("halo_execute", {"method": "GET", "path": "/Tickets/9"})
    assert result["isError"] is True
    assert payload["ok"] is False
    assert payload["category"] == "not_found"
    assert payload["status_code"] == 404
    assert "404" in payload["error"]


def test_execute_wraps_unexpected_client_errors(monkeypatch: Any) -> None:
    fake = FakeClient(error=RuntimeError("boom"))
    install_fake(monkeypatch, fake)
    result, payload = call_tool("halo_execute", {"method": "GET", "path": "/Tickets"})
    assert result["isError"] is True
    assert payload["ok"] is False
    assert payload["category"] == "unknown"
    assert "boom" in payload["error"]


def test_tool_crash_is_reported_not_fatal(monkeypatch: Any) -> None:
    async def boom(args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(mcp_server._TOOL_HANDLERS, "halo_search", boom)
    result, payload = call_tool("halo_search", {"query": "tickets"})
    assert result["isError"] is True
    assert payload["ok"] is False
    assert "handler exploded" in payload["error"]


# --------------------------------------------------------------------------------------
# JSON-RPC protocol errors / robustness
# --------------------------------------------------------------------------------------
def test_unknown_method_returns_method_not_found() -> None:
    response = rpc("does/not/exist")
    assert response["error"]["code"] == -32601


def test_unknown_tool_returns_invalid_params() -> None:
    response = rpc("tools/call", {"name": "halo_nope", "arguments": {}})
    assert response["error"]["code"] == -32602


def test_malformed_json_line_does_not_kill_server() -> None:
    responses = run_server(
        "{this is not json",
        json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}),
        json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/list"}),
    )
    assert len(responses) == 3
    assert responses[0]["error"]["code"] == -32700
    assert responses[0]["id"] is None
    assert responses[1]["result"] == {}
    assert len(responses[2]["result"]["tools"]) == 3


def test_blank_lines_are_ignored() -> None:
    responses = run_server(
        "",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
    )
    assert len(responses) == 1
    assert responses[0]["result"] == {}
