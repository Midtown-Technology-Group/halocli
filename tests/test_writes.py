from __future__ import annotations

from typing import Any

import pytest

from halocli.resources import HaloResource, get_resource
from halocli.writes import (
    collect_warnings,
    delete_resource,
    execute_write,
    preview_payload,
    validate_write,
)


class RecordingClient:
    """Fake client that records every request and returns a canned response."""

    def __init__(self, response: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = {"id": 42, "created": True} if response is None else response

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        self.calls.append(
            {"method": method, "path": path, "params": params, "json_body": json_body}
        )
        return self.response


class ExplodingClient:
    """Fake client that fails the test if any network call happens."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        raise AssertionError(f"dry run must not issue requests (got {method} {path})")


WIDGET = HaloResource(
    "widgets",
    "/Widget",
    create_endpoint="/Widget",
    update_endpoint="/WidgetUpdate",
    required_create_fields=("name",),
    required_update_fields=("id",),
    write_preview_fields=("id", "name"),
)


@pytest.mark.asyncio
async def test_preview_makes_no_network_calls() -> None:
    tickets = get_resource("tickets")

    preview = await execute_write(
        ExplodingClient(),
        tickets,
        {"summary": "Printer on fire", "client_id": 7},
    )

    assert preview["ok"] is True
    assert preview["apply"] is False
    assert preview["resource"] == "tickets"
    assert preview["method"] == "POST"
    assert preview["endpoint"] == "/Tickets"
    assert preview["payload"] == {"summary": "Printer on fire", "client_id": 7}
    assert preview["warnings"] == []


@pytest.mark.asyncio
async def test_missing_required_field_blocks_apply() -> None:
    tickets = get_resource("tickets")
    client = RecordingClient()

    result = await execute_write(
        client,
        tickets,
        {"client_id": 7},
        apply=True,
    )

    assert result["ok"] is False
    assert result["apply"] is True
    assert any("summary" in problem for problem in result["errors"])
    assert client.calls == []


@pytest.mark.asyncio
async def test_update_requires_id_before_any_network_call() -> None:
    tickets = get_resource("tickets")
    client = RecordingClient()

    result = await execute_write(
        client,
        tickets,
        {"summary": "No id here"},
        update=True,
        apply=True,
    )

    assert result["ok"] is False
    assert any("id" in problem for problem in result["errors"])
    assert client.calls == []


def test_validate_write_reports_required_fields_and_allows_extra_keys() -> None:
    tickets = get_resource("tickets")

    assert validate_write(tickets, {"summary": "ok", "client_id": 1}) == []
    problems = validate_write(tickets, {"client_id": 1})
    assert problems and "summary" in problems[0]
    assert validate_write(tickets, {"summary": "ok"}, update=True)
    # Extra keys are allowed: they are warnings, never validation errors.
    assert validate_write(tickets, {"summary": "ok", "custom_field_xyz": "kept"}) == []


def test_create_rejects_concrete_id_because_halo_treats_it_as_update() -> None:
    tickets = get_resource("tickets")

    problems = validate_write(tickets, {"id": 42, "summary": "Printer on fire"})

    assert problems
    assert any("id" in problem and "update" in problem for problem in problems)


def test_create_allows_absent_style_ids() -> None:
    tickets = get_resource("tickets")

    assert validate_write(tickets, {"id": None, "summary": "ok"}) == []
    assert validate_write(tickets, {"id": "", "summary": "ok"}) == []


def test_update_with_id_is_unaffected_by_create_id_rule() -> None:
    tickets = get_resource("tickets")

    assert validate_write(tickets, {"id": 42, "summary": "renamed"}, update=True) == []


@pytest.mark.asyncio
async def test_apply_refuses_create_payload_with_id_before_any_network_call() -> None:
    tickets = get_resource("tickets")
    client = RecordingClient()

    result = await execute_write(
        client,
        tickets,
        {"id": 42, "summary": "Printer on fire"},
        apply=True,
    )

    assert result["ok"] is False
    assert any("id" in problem for problem in result["errors"])
    assert client.calls == []


@pytest.mark.asyncio
async def test_apply_posts_to_create_endpoint_with_payload() -> None:
    tickets = get_resource("tickets")
    payload = {"summary": "VPN down", "client_id": 3, "status_id": 1}
    client = RecordingClient(response={"id": 99, "summary": "VPN down"})

    result = await execute_write(client, tickets, payload, apply=True)

    assert result["ok"] is True
    assert result["apply"] is True
    assert result["endpoint"] == "/Tickets"
    assert result["result"] == {"id": 99, "summary": "VPN down"}
    assert client.calls == [
        {"method": "POST", "path": "/Tickets", "params": None, "json_body": [payload]}
    ]


@pytest.mark.asyncio
async def test_update_uses_update_endpoint_from_metadata() -> None:
    client = RecordingClient(response={"id": 5, "name": "renamed"})

    result = await execute_write(
        client,
        WIDGET,
        {"id": 5, "name": "renamed"},
        update=True,
        apply=True,
    )

    assert result["ok"] is True
    assert result["endpoint"] == "/WidgetUpdate"
    assert client.calls[0]["path"] == "/WidgetUpdate"
    assert client.calls[0]["json_body"] == [{"id": 5, "name": "renamed"}]


def test_write_metadata_selects_endpoints_and_defaults_preview_fields() -> None:
    assert validate_write(WIDGET, {"name": "new"}) == []
    problems = validate_write(WIDGET, {}, update=False)
    assert problems and "name" in problems[0]

    # No explicit preview fields -> default shape is id plus the required fields.
    bare = HaloResource(
        "bare",
        "/Bare",
        create_endpoint="/Bare",
        required_create_fields=("name",),
    )
    assert bare.effective_write_preview_fields == ("id", "name")


def test_extra_keys_warn_in_preview_without_blocking() -> None:
    tickets = get_resource("tickets")
    payload = {"summary": "hi", "totally_unknown_field": 123}

    warnings = collect_warnings(tickets, payload)
    assert any("totally_unknown_field" in warning for warning in warnings)
    assert validate_write(tickets, payload) == []
    # The preview shows EVERY field apply will send: write_preview_fields only
    # orders/emphasises, it never redacts.
    assert preview_payload(tickets, payload) == {"summary": "hi", "totally_unknown_field": 123}


def test_preview_orders_declared_fields_first_then_extras_in_insertion_order() -> None:
    tickets = get_resource("tickets")
    payload = {"zzz_extra": 1, "summary": "hi", "client_id": 7, "aaa_extra": 2}

    preview = preview_payload(tickets, payload)

    assert list(preview) == ["summary", "client_id", "zzz_extra", "aaa_extra"]
    assert preview == payload


@pytest.mark.asyncio
async def test_preview_carries_warnings_for_extra_keys() -> None:
    tickets = get_resource("tickets")

    preview = await execute_write(
        ExplodingClient(),
        tickets,
        {"summary": "hi", "mystery_key": True},
    )

    assert preview["ok"] is True
    assert any("mystery_key" in warning for warning in preview["warnings"])
    # Warnings still fire, but the field is shown (and would be sent) regardless.
    assert preview["payload"]["mystery_key"] is True


@pytest.mark.asyncio
async def test_resource_without_write_metadata_is_refused() -> None:
    # currencies stays read-only by argument (money config); a future batch
    # that flips it must consciously update this refusal test.
    currencies = get_resource("currencies")
    client = RecordingClient()

    result = await execute_write(client, currencies, {"name": "Nope"}, apply=True)

    assert result["ok"] is False
    assert any("does not support create" in problem for problem in result["errors"])
    assert client.calls == []


@pytest.mark.asyncio
async def test_delete_refused_when_unsupported() -> None:
    currencies = get_resource("currencies")
    client = RecordingClient()

    result = await delete_resource(client, currencies, 1, apply=True)

    assert result["ok"] is False
    assert result["method"] == "DELETE"
    assert any("does not support delete" in problem for problem in result["errors"])
    assert client.calls == []


@pytest.mark.asyncio
async def test_delete_is_gated_on_apply() -> None:
    appointments = get_resource("appointments")

    preview = await delete_resource(ExplodingClient(), appointments, 7)
    assert preview["ok"] is True
    assert preview["apply"] is False
    assert preview["endpoint"] == "/Appointment/7"

    client = RecordingClient(response={"deleted": True})
    applied = await delete_resource(client, appointments, 7, apply=True)
    assert applied["ok"] is True
    assert applied["apply"] is True
    assert applied["result"] == {"deleted": True}
    assert client.calls == [
        {"method": "DELETE", "path": "/Appointment/7", "params": None, "json_body": None}
    ]


@pytest.mark.asyncio
async def test_delete_requires_a_non_empty_item_id() -> None:
    tickets = get_resource("tickets")
    client = RecordingClient()

    result = await delete_resource(client, tickets, "", apply=True)

    assert result["ok"] is False
    assert client.calls == []
