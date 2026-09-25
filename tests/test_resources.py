from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from halocli.cli import app
from halocli.client import HaloClient
from halocli.config import HaloProfile
from halocli.resources import RESOURCE_BY_COMMAND, RESOURCES, HaloResource, get_resource
from halocli.schema import load_spec


runner = CliRunner()

# Resources that must carry first-class write metadata.
WRITE_RESOURCES = (
    "tickets",
    "actions",
    "clients",
    "sites",
    "assets",
    "agents",
    "appointments",
    "statuses",
    "priorities",
)


def test_registry_names_and_aliases_are_unique() -> None:
    names = [resource.name for resource in RESOURCES]
    commands = [
        command_name
        for resource in RESOURCES
        for command_name in resource.command_names
    ]

    assert len(names) == len(set(names))
    assert len(commands) == len(set(commands))


def test_registry_resources_have_endpoints_and_table_fields() -> None:
    for resource in RESOURCES:
        assert resource.endpoint.startswith("/")
        assert resource.table_fields


def test_registry_still_constructs_all_resources() -> None:
    assert len(RESOURCES) == 32
    # Backward compatibility: a resource with no write metadata stays read-only.
    plain = HaloResource("plain", "/Plain")
    assert plain.create_endpoint is None
    assert plain.update_endpoint is None
    assert plain.required_create_fields == ()
    assert plain.required_update_fields == ()
    assert plain.supports_delete is False
    assert plain.write_preview_fields == ()
    assert plain.supports_write is False


def test_write_metadata_present_for_write_enabled_resources() -> None:
    assert len(WRITE_RESOURCES) == 9
    for name in WRITE_RESOURCES:
        resource = get_resource(name)
        assert resource.supports_write, name
        assert resource.supports_create, name
        assert resource.supports_update, name
        assert resource.supports_delete, name
        assert resource.create_endpoint == resource.endpoint, name
        assert resource.update_endpoint == resource.endpoint, name
        assert resource.required_create_fields, name
        assert "id" in resource.required_update_fields, name
        assert resource.effective_write_preview_fields[0] == "id", name
        assert set(resource.required_create_fields) <= set(
            resource.effective_write_preview_fields
        ), name

    # Resources outside the write set keep their read-only defaults.
    for resource in RESOURCES:
        if resource.name in WRITE_RESOURCES:
            continue
        assert not resource.supports_write, resource.name
        assert not resource.supports_delete, resource.name


def test_contracts_is_read_only_because_spec_has_no_post_contract() -> None:
    contracts = get_resource("contracts")
    assert contracts.endpoint == "/Contract"
    assert not contracts.supports_write
    assert not contracts.supports_create
    assert not contracts.supports_update
    assert contracts.supports_delete is False

    # The vendored spec documents no /Contract path at all, so there is no
    # verified write route; POST exists only on /ClientContract and
    # /SupplierContract (different semantics). Guard against re-enabling writes.
    spec = load_spec()
    assert spec is not None
    assert "/Contract" not in spec["paths"]
    assert "post" in spec["paths"]["/ClientContract"]
    assert "post" in spec["paths"]["/SupplierContract"]


def test_ticket_and_action_write_shapes() -> None:
    tickets = get_resource("tickets")
    assert tickets.required_create_fields == ("summary",)
    assert tickets.supports_delete is True

    actions = get_resource("actions")
    assert actions.required_create_fields == ("ticket_id", "note")
    assert "note" in actions.write_preview_fields


@pytest.mark.parametrize("resource", RESOURCES)
def test_generated_resource_commands_load(resource) -> None:
    assert runner.invoke(app, [resource.name, "list", "--help"]).exit_code == 0
    assert runner.invoke(app, [resource.name, "get", "--help"]).exit_code == 0


def test_known_aliases_resolve_to_canonical_resources() -> None:
    assert get_resource("ticket").name == "tickets"
    assert get_resource("ticket-types").endpoint == "/TicketType"
    assert RESOURCE_BY_COMMAND["software-license"].name == "software-licences"


@pytest.mark.asyncio
async def test_client_get_resource_uses_registry_endpoint() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={"id": 42, "name": "Site 42"})

    profile = HaloProfile(
        tenant_url="https://halo.example.com",
        client_id="id",
        client_secret="secret",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = HaloClient(profile, http=http)
        result = await client.get_resource("sites", "42")

    assert result == {"id": 42, "name": "Site 42"}
    assert seen_paths == ["/api/Site/42"]


def test_table_output_uses_registry_fields(monkeypatch) -> None:
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(
            200,
            json={
                "sites": [
                    {"id": 1, "name": "HQ", "client_name": "Example", "ignored": "hidden"}
                ],
                "record_count": 1,
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )

    result = runner.invoke(app, ["sites", "list", "--max-records", "1", "--output", "table"])

    assert result.exit_code == 0
    assert "client_name" in result.output
    assert "ignored" not in result.output


def test_get_outputs_json_item(monkeypatch) -> None:
    monkeypatch.setenv("HALO_TENANT_URL", "https://halo.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "secret")
    async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        return httpx.Response(200, json={"id": 7, "name": "Acme"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "halocli.client.httpx.AsyncClient",
        lambda *args, **kwargs: async_client(transport=transport),
    )

    result = runner.invoke(app, ["clients", "get", "7"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["resource"] == "clients"
    assert payload["item"] == {"id": 7, "name": "Acme"}
