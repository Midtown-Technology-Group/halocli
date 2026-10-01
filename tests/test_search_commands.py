"""Tests for `halocli search` (live tenant search) and the 1.0.0 rename.

`search` was offline catalog discovery until 1.0.0, when the name moved to
`catalog` and `search` gained the live `/Search` query. These tests pin the
new shape (grouped by the `use` key), the migration guard, and the
`searches` registry entry.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from typer.testing import CliRunner

from halocli import schema
from halocli.cli import app
from halocli.resources import get_resource

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(output: str) -> str:
    return _ANSI_RE.sub("", output)


def _install_mock(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict]:
    """Route HaloClient through `handler`, returning the recorded requests.

    The OAuth token call is answered internally and NOT recorded, so `calls`
    holds exactly the requests the command itself made (same shape as the
    reports harness, which counts "requests beyond the token").
    """
    async_client = httpx.AsyncClient
    calls: list[dict] = []

    def recording(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.url.params),
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


def _json_response(rows: list[dict]) -> httpx.Response:
    return httpx.Response(200, json=rows)


_HETEROGENEOUS = [
    {"use": "ticket", "id": 5, "summary": "Backup alert"},
    {"use": "asset", "id": 7, "name": "server-01"},
    {"use": "article", "id": 9, "name": "Restore runbook"},
    {"use": "service", "id": 11, "name": "Managed backup"},  # no `table` key
]


# ------------------------------------------------------------------ declaration


def test_searches_resource_is_registered_read_only() -> None:
    searches = get_resource("searches")
    assert searches.endpoint == "/Search"
    # GET /Search/{id} does not exist in the spec.
    assert searches.supports_get is False
    assert searches.create_endpoint is None
    assert searches.supports_delete is False
    assert searches.table_fields[0] == "use"  # the stable entity discriminator


def test_search_route_and_params_exist_in_vendored_spec() -> None:
    spec = schema.load_spec()
    assert spec is not None
    assert "get" in spec["paths"]["/Search"]
    assert "/Search/{id}" not in spec["paths"]
    params = {p["name"] for p in spec["paths"]["/Search"]["get"].get("parameters", [])}
    assert {"search", "count_per_entity"} <= params


def test_legacy_multi_term_invocation_shows_migration_hint() -> None:
    result = runner.invoke(app, ["search", "site", "page"])

    assert result.exit_code != 0
    output = plain(result.output)
    assert "halocli catalog" in output
    assert "formerly" in output


def test_search_rejects_blank_term_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_mock(monkeypatch, lambda request: _json_response([]))

    result = runner.invoke(app, ["search", "   "])

    assert result.exit_code != 0
    assert "non-empty" in plain(result.output)
    assert calls == []


def test_search_requires_a_term() -> None:
    result = runner.invoke(app, ["search"])

    assert result.exit_code != 0


def test_search_rejects_apply_flags() -> None:
    result = runner.invoke(app, ["search", "backup", "--apply"])

    assert result.exit_code != 0
    assert "no such option" in plain(result.output).lower()


def test_search_count_per_entity_bounds_are_enforced() -> None:
    for bad in ("0", "101"):
        result = runner.invoke(app, ["search", "x", "--count-per-entity", bad])
        assert result.exit_code != 0


# ------------------------------------------------------------------ shaping


def test_search_shapes_heterogeneous_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_mock(monkeypatch, lambda request: _json_response(_HETEROGENEOUS))

    result = runner.invoke(app, ["search", "backup", "--count-per-entity", "3"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["endpoint"] == "/Search"
    assert payload["query"] == "backup"
    assert payload["row_count"] == 4
    assert payload["count"] == 4
    # Grouped by `use`: `table` is missing on service rows, `use` never was.
    assert payload["entity_counts"] == {
        "ticket": 1,
        "asset": 1,
        "article": 1,
        "service": 1,
    }
    assert payload["items"] == _HETEROGENEOUS  # raw dicts preserved, unmodified

    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"].endswith("/Search")
    assert calls[0]["params"]["search"] == "backup"
    assert calls[0]["params"]["count_per_entity"] == "3"


def test_search_limit_truncates_but_reports_the_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [{"use": "ticket", "id": i} for i in range(10)]
    _install_mock(monkeypatch, lambda request: _json_response(rows))

    result = runner.invoke(app, ["search", "server", "--limit", "3"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["row_count"] == 10
    assert payload["count"] == 3
    assert len(payload["items"]) == 3
    assert "10 matches" in payload["hint"]
    assert "--limit 10" in payload["hint"]


def test_search_empty_result_is_success_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, lambda request: _json_response([]))

    result = runner.invoke(app, ["search", "zzzqqqnotfound"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["row_count"] == 0
    assert payload["entity_counts"] == {}
    assert "No matches" in payload["hint"]


def test_search_non_array_body_is_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, lambda request: httpx.Response(200, json={"nope": True}))

    result = runner.invoke(app, ["search", "backup"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["category"] == "validation"
    assert "expected an array" in payload["error"]


def test_search_permission_failure_exits_nonzero_with_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(
        monkeypatch,
        lambda request: httpx.Response(
            403, json={"category": "permission", "status_code": 403, "error": "denied"}
        ),
    )

    result = runner.invoke(app, ["search", "backup"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["category"] == "permission"
    assert payload["status_code"] == 403
    assert payload["diagnostic"]


# ------------------------------------------------------------------ rename


def test_catalog_still_answers_offline_discovery() -> None:
    result = runner.invoke(app, ["catalog", "tickets", "--limit", "3"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["query"] == "tickets"
    assert payload["count"] == len(payload["results"])
    assert payload["count"] <= 3
    assert any(r.get("name") == "tickets" for r in payload["results"])
