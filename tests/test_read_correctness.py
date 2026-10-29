from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from halocli.cli import app


@pytest.fixture
def mock_api(monkeypatch):
    """Exercise the real CLI/client without profiles or tenant access."""
    real_client = httpx.AsyncClient

    def install(handler):
        calls = []

        def dispatch(request):
            if request.url.path == "/auth/token":
                return httpx.Response(200, json={"access_token": "test", "expires_in": 3600})
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(
            "halocli.client.httpx.AsyncClient",
            lambda *args, **kwargs: real_client(transport=httpx.MockTransport(dispatch)),
        )
        monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
        monkeypatch.setenv("HALO_CLIENT_ID", "test")
        monkeypatch.setenv("HALO_CLIENT_SECRET", "test")
        return calls

    return install


@pytest.mark.parametrize("shape", ["array", "missing", "null", "invalid"])
def test_list_without_total_walks_pages_and_reports_ceiling(mock_api, shape):
    def handler(request):
        page = int(request.url.params["page_no"])
        rows = [{"id": i} for i in range((page - 1) * 2 + 1, page * 2 + 1)]
        if shape == "array":
            return httpx.Response(200, json=rows)
        body = {"clients": rows}
        if shape != "missing":
            body["record_count"] = None if shape == "null" else "unknown"
        return httpx.Response(200, json=body)

    calls = mock_api(handler)
    result = CliRunner().invoke(
        app, ["clients", "list", "--page-size", "2", "--max-records", "3", "--no-labels"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["id"] for row in payload["items"]] == [1, 2, 3]
    assert payload["truncated"] is True
    assert payload["total_available"] is None
    assert len(calls) == 2


def test_cursor_stall_without_total_reports_incomplete_json(mock_api):
    calls = mock_api(lambda request: httpx.Response(200, json={"feed": [{"id": 2}, {"id": 1}]}))
    result = CliRunner().invoke(
        app, ["feeds", "list", "--page-size", "2", "--max-pages", "4", "--no-labels"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload.get("truncated") is True
    assert payload["total_available"] is None
    assert [row["id"] for row in payload["items"]] == [2, 1]
    assert len(calls) == 2


def test_unknown_total_repeated_page_reports_incomplete_json(mock_api):
    calls = mock_api(lambda request: httpx.Response(200, json={"clients": [{"id": 1}, {"id": 2}]}))
    result = CliRunner().invoke(app, ["clients", "list", "--page-size", "2", "--no-labels"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["paging_ignored"] is True
    assert payload.get("truncated") is True
    assert payload["total_available"] is None
    assert [row["id"] for row in payload["items"]] == [1, 2]
    assert len(calls) == 2


@pytest.mark.parametrize("last_page", [[], [{"id": 3}]])
def test_unknown_total_exhaustion_is_complete(mock_api, last_page):
    def handler(request):
        rows = [{"id": 1}, {"id": 2}] if request.url.params["page_no"] == "1" else last_page
        return httpx.Response(200, json={"clients": rows})

    calls = mock_api(handler)
    result = CliRunner().invoke(
        app, ["clients", "list", "--page-size", "2", "--max-records", "10", "--no-labels"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 2 + len(last_page)
    assert "truncated" not in payload
    assert "total_available" not in payload
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("windows", "expected_calls", "expected_ids", "truncated"),
    [
        ([[{"id": "a"}, {"id": "b"}]], 1, ["a", "b"], True),
        ([[{"id": 4}, {"id": 3}], [{"id": 6}, {"id": 5}]], 2, [4, 3, 6, 5], True),
        ([[{"id": 2}, {"id": 1}], []], 2, [2, 1], False),
        ([[{"id": "a"}]], 1, ["a"], False),
    ],
)
def test_cursor_unknown_total_safety_stops_and_drain(
    mock_api, windows, expected_calls, expected_ids, truncated
):
    responses = iter(windows)
    calls = mock_api(lambda request: httpx.Response(200, json={"feed": next(responses)}))
    result = CliRunner().invoke(
        app, ["feeds", "list", "--page-size", "2", "--max-pages", "4", "--no-labels"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["id"] for row in payload["items"]] == expected_ids
    assert payload.get("truncated", False) is truncated
    assert len(calls) == expected_calls


@pytest.mark.parametrize(
    "item_id", ["part?active=false", "part#revision", "part/child", "part%2Fchild"]
)
def test_get_encodes_identifier_as_one_path_segment(mock_api, item_id):
    from urllib.parse import quote

    calls = mock_api(lambda request: httpx.Response(200, json={"id": item_id}))
    result = CliRunner().invoke(app, ["clients", "get", item_id, "--no-labels"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == item_id
    assert calls[0].url.raw_path == f"/api/Client/{quote(item_id, safe='')}".encode()
    assert not calls[0].url.query
