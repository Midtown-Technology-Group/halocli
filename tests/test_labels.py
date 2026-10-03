"""Label hydration: bare FKs resolve to tenant labels (no operator mapping table)."""
from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from halocli.cli import app
from halocli.labels import RESOLVERS, hydrate_items, label_key_for
from halocli.output import _columns
from halocli.resources import get_resource
from halocli.schema import load_spec

runner = CliRunner()


def _spec_post_props(path: str) -> set[str]:
    spec = load_spec()
    assert spec is not None
    op = spec["paths"].get(path, {}).get("post")
    if not op:
        return set()
    schema = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    if "$ref" in schema:
        node = spec
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        schema = node
    if schema.get("type") == "array":
        items = schema.get("items", {})
        if "$ref" in items:
            node = spec
            for part in items["$ref"].lstrip("#/").split("/"):
                node = node[part]
            items = node
        schema = items
    return set((schema.get("properties") or {}).keys())


def test_resolvers_bind_to_registry_and_spec() -> None:
    """Every resolver targets an existing resource with an evidenced label column."""
    assert len(RESOLVERS) == 37
    label_fields: dict[str, str] = {}
    for fk, (resource, field, _key) in RESOLVERS.items():
        reg = get_resource(resource)  # must exist
        assert field in reg.table_fields or field in _spec_post_props(reg.endpoint), (
            f"{fk}: label {field!r} not evidenced for {resource}"
        )
        # one label column per resource (memoised fetches assume consistency)
        assert label_fields.setdefault(resource, field) == field, resource
        # id-ish column or a curated numbered pair (categoryid_1..4)
        assert fk.endswith("_id") or fk.startswith("categoryid_"), fk


def test_label_key_follows_halo_convention() -> None:
    assert label_key_for("status_id") == "status_name"
    assert label_key_for("closure_agent_id") == "closure_agent_name"
    assert label_key_for("categoryid_1") == "category_1"
    assert label_key_for("item_assettype_id") == "assettype_name"


class FakeClient:
    """Canned list/detail responses; records every call."""

    def __init__(self, lists: dict[str, Any] | None = None,
                 details: dict[str, Any] | None = None) -> None:
        self.lists = lists or {}
        self.details = details or {}
        self.calls: list[tuple] = []

    async def list_resource(self, resource: str, **params: Any) -> Any:
        self.calls.append(("list", resource, sorted(params)))
        data = self.lists.get(resource, [])
        rows, record_count = (data if isinstance(data, tuple) else (data, len(data)))
        key = get_resource(resource).list_key or "rows"
        return {key: rows, "record_count": record_count}

    async def get_resource(self, resource: str, item_id: Any) -> Any:
        self.calls.append(("get", resource, str(item_id)))
        try:
            return self.details[(resource, str(item_id))]
        except KeyError:
            raise RuntimeError("404") from None


@pytest.mark.asyncio
async def test_hydrate_adds_labels_and_never_overwrites() -> None:
    item = {
        "id": 1,
        "status_id": 9,
        "priority_id": 3,
        "client_id": 12,
        "client_name": "Acme",          # Halo already sent this: must not change
        "department_id": 7,             # no resolver: stays bare
    }
    client = FakeClient(lists={
        "statuses": [{"id": 9, "name": "In Progress"}],
        # real shape: GUID primary key + integer priorityid join column
        "priorities": [{"id": "guid-3", "priorityid": 3, "name": "High"}],
    })
    added = await hydrate_items(client, [item])
    assert added == 2
    assert item["status_name"] == "In Progress"
    assert item["priority_name"] == "High"
    assert item["client_name"] == "Acme"          # untouched
    assert "department_name" not in item           # no resolver
    assert item["status_id"] == 9                  # raw id untouched
    # client_id skipped (label present) -> clients list never fetched
    assert sorted(c[1] for c in client.calls if c[0] == "list") == ["priorities", "statuses"]
    assert not any(c[0] == "get" for c in client.calls)


@pytest.mark.asyncio
async def test_existing_labels_prevent_fetches_entirely() -> None:
    item = {"id": 1, "status_id": 9, "status_name": "Done"}
    client = FakeClient()
    added = await hydrate_items(client, [item])
    assert added == 0
    assert client.calls == []
    assert item["status_name"] == "Done"


@pytest.mark.asyncio
async def test_guid_keyed_entity_joins_on_integer_column() -> None:
    """Halo quirk: /Priority rows are GUID-keyed but carry priorityid=<int>.

    Tickets store the integer and /Priority/<int> 404s, so the lookup joins on
    priorityid and never attempts a doomed detail fallback.
    """
    item = {"id": 1, "priority_id": 4}
    client = FakeClient(lists={
        "priorities": ([
            {"id": "c183eb27-aaaa", "priorityid": 1, "name": "Urgent"},
            {"id": "0f27f985-bbbb", "priorityid": 4, "name": "Medium"},
        ], 2),
    })
    added = await hydrate_items(client, [item])
    assert added == 1
    assert item["priority_name"] == "Medium"
    assert not any(c[0] == "get" for c in client.calls)  # no int-id detail probe


@pytest.mark.asyncio
async def test_detail_fallback_for_ids_beyond_first_page() -> None:
    item = {"id": 1, "agent_id": 7}
    client = FakeClient(
        lists={"agents": ([{"id": 1, "name": "Thomas"}], 50)},  # page incomplete, id 7 absent
        details={("agents", "7"): {"id": 7, "name": "Sam"}},
    )
    added = await hydrate_items(client, [item])
    assert added == 1
    assert item["agent_name"] == "Sam"
    assert ("get", "agents", "7") in client.calls


@pytest.mark.asyncio
async def test_detail_budget_caps_worst_case_cost() -> None:
    items = [{"id": i, "agent_id": 100 + i} for i in range(5)]
    client = FakeClient(lists={"agents": ([], 50)})
    added = await hydrate_items(client, items, detail_budget=0)
    assert added == 0
    assert not any(c[0] == "get" for c in client.calls)


@pytest.mark.asyncio
async def test_detail_failure_is_tolerated() -> None:
    item = {"id": 1, "agent_id": 7}
    client = FakeClient(lists={"agents": ([], 50)})  # fallback raises -> tolerated
    added = await hydrate_items(client, [item])
    assert added == 0
    assert "agent_name" not in item


@pytest.mark.asyncio
async def test_disabled_makes_zero_calls() -> None:
    item = {"id": 1, "status_id": 9}
    client = FakeClient(lists={"statuses": [{"id": 9, "name": "X"}]})
    assert await hydrate_items(client, [item], enabled=False) == 0
    assert client.calls == []
    assert "status_name" not in item


@pytest.mark.asyncio
async def test_placeholder_and_exotic_ids_stay_bare() -> None:
    items = [
        {"id": 1, "status_id": 0},          # placeholder zero
        {"id": 2, "status_id": None},
        {"id": 3, "status_id": [9, 10]},    # exotic shape
    ]
    client = FakeClient()
    assert await hydrate_items(client, items) == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_existing_category_label_pair_is_respected() -> None:
    item = {"id": 1, "categoryid_1": 5, "category_1": "Bugs"}
    client = FakeClient()
    assert await hydrate_items(client, [item]) == 0
    assert item["category_1"] == "Bugs"
    assert client.calls == []


def test_table_columns_prefer_hydrated_labels() -> None:
    rows = [{"id": 1, "subject": "Visit", "agent_id": 3, "agent_name": "Sam",
             "start_date": "2026-10-03"}]
    cols = _columns(rows, table_fields=("id", "subject", "agent_id", "start_date"))
    assert "agent_name" in cols and "agent_id" not in cols
    # without the hydrated label the raw id column stands
    rows2 = [{"id": 1, "agent_id": 3, "start_date": "2026-10-03"}]
    cols2 = _columns(rows2, table_fields=("id", "agent_id", "start_date"))
    assert "agent_id" in cols2 and "agent_name" not in cols2


# ------------------------------------------------------------------ CLI wiring


def _mock_halo(monkeypatch: pytest.MonkeyPatch, *, ticket_row: dict) -> list[str]:
    """Serve a tickets get + the status list; return seen paths."""
    import httpx

    async_client = httpx.AsyncClient
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        seen.append(request.url.path)
        if request.url.path == "/api/Tickets/5":
            return httpx.Response(200, json=ticket_row)
        if request.url.path == "/api/Status":
            return httpx.Response(200, json={"statuses": [{"id": 9, "name": "In Progress"}],
                                            "record_count": 1})
        if request.url.path == "/api/Priority":
            return httpx.Response(200, json={"priorities": [
                {"id": "guid-3", "priorityid": 3, "name": "High"}],
                "record_count": 1})
        return httpx.Response(404, json={"error": f"unexpected {request.url.path}"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    return seen


def test_get_cli_hydrates_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = {"id": 5, "summary": "Broken", "status_id": 9, "priority_id": 3,
              "client_id": 12, "client_name": "Acme"}
    _mock_halo(monkeypatch, ticket_row=ticket)

    result = runner.invoke(app, ["tickets", "get", "5"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    item = payload["item"]
    assert item["status_name"] == "In Progress"
    assert item["priority_name"] == "High"
    assert item["status_id"] == 9            # raw ids preserved
    assert payload["labels_added"] == 2


def test_get_cli_no_labels_flag_skips_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = {"id": 5, "summary": "Broken", "status_id": 9}
    seen = _mock_halo(monkeypatch, ticket_row=ticket)

    result = runner.invoke(app, ["tickets", "get", "5", "--no-labels"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "status_name" not in payload["item"]
    assert "labels_added" not in payload
    assert "/api/Status" not in seen


def test_labels_added_key_absent_when_nothing_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = {"id": 5, "summary": "Fine", "client_id": 12, "client_name": "Acme"}
    _mock_halo(monkeypatch, ticket_row=ticket)

    result = runner.invoke(app, ["tickets", "get", "5"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "labels_added" not in payload
    assert payload["item"]["client_name"] == "Acme"
