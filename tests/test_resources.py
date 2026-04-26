from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from halocli.cli import app
from halocli.client import HaloClient
from halocli.config import HaloProfile
from halocli.resources import RESOURCE_BY_COMMAND, RESOURCES, get_resource


runner = CliRunner()


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
