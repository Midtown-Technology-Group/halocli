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
    "kb",
    "crm-notes",
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
    assert len(RESOURCES) == 35
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
    assert len(WRITE_RESOURCES) == 11
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


def test_contracts_reads_client_contract_and_stays_read_only() -> None:
    contracts = get_resource("contracts")
    # Live-verified 2026-09-28: GET /Contract is 404 on the real tenant; reads
    # point at /ClientContract, which returns 200.
    assert contracts.endpoint == "/ClientContract"
    assert not contracts.supports_write
    assert not contracts.supports_create
    assert not contracts.supports_update
    assert contracts.supports_delete is False

    # The read endpoint is spec-documented with GET, but no verified *write*
    # route exists for generic "contracts": POST /ClientContract narrows the
    # semantics to client contracts only, POST /SupplierContract is a different
    # entity again, and /SupplierContract is permission-gated (403) for our
    # agents. Guard against re-enabling writes without that decision.
    spec = load_spec()
    assert spec is not None
    assert "/Contract" not in spec["paths"]
    assert "get" in spec["paths"]["/ClientContract"]
    assert "post" in spec["paths"]["/ClientContract"]
    assert "post" in spec["paths"]["/SupplierContract"]


def test_flagged_read_endpoints_use_spec_documented_paths() -> None:
    """The three oracle-flagged 404s must point at spec-documented paths.

    Live-verified against the real tenant (2026-09-28): /Contract,
    /Opportunity and /Project all return 404; /Projects returns 200,
    /ClientContract returns 200, /Opportunities is permission-gated (403,
    correct path). /Opportunities does NOT contain the substring "Opportunity"
    — a naive substring search misses it; the oracle's exact-path check does not.
    """
    spec = load_spec()
    assert spec is not None
    for resource_name, path in (
        ("contracts", "/ClientContract"),
        ("opportunities", "/Opportunities"),
        ("projects", "/Projects"),
    ):
        resource = get_resource(resource_name)
        assert resource.endpoint == path
        assert path in spec["paths"], f"{path} missing from spec"
        assert "get" in spec["paths"][path]
        assert f"{path}/{{id}}" in spec["paths"], f"{path}/{{id}} missing from spec"


def test_ticket_and_action_write_shapes() -> None:
    tickets = get_resource("tickets")
    assert tickets.required_create_fields == ("summary",)
    assert tickets.supports_delete is True

    actions = get_resource("actions")
    assert actions.required_create_fields == ("ticket_id", "note")
    assert "note" in actions.write_preview_fields


def test_kb_write_shape_matches_live_evidence() -> None:
    """kb write metadata, pinned to the 2026-09-30 live probes.

    Live: list -> {"articles": [...], "record_count": N} with real pagination;
    GET /KBArticle/391 -> 200; rows carry `name` but never `title`. The spec
    declares no required fields on POST, so the create set is a documented
    assumption. POST/DELETE were never fired (no write authorization).
    """
    kb = get_resource("kb")

    assert kb.endpoint == "/KBArticle"
    assert kb.list_key == "articles"
    assert kb.table_fields == ("id", "name", "type", "inactive", "date_edited")
    assert kb.create_endpoint == kb.endpoint
    assert kb.update_endpoint == kb.endpoint
    assert kb.supports_delete is True
    assert kb.required_create_fields == ("name", "description")
    assert "id" in kb.required_update_fields
    assert kb.effective_write_preview_fields[0] == "id"
    assert set(kb.required_create_fields) <= set(kb.effective_write_preview_fields)

    # Every write route exists in the vendored spec (coverage-oracle ally).
    spec = load_spec()
    assert spec is not None
    assert "post" in spec["paths"]["/KBArticle"]
    assert "delete" in spec["paths"]["/KBArticle/{id}"]
    # No PUT/PATCH: Halo's upsert convention is POST-with-id only.
    assert "put" not in spec["paths"]["/KBArticle"]
    assert "patch" not in spec["paths"]["/KBArticle"]


def test_quotation_operations_are_declared_and_spec_backed() -> None:
    """Quotation nested ops, pinned to the 2026-09-30 live read probes.

    Live: list -> 200 (223 rows under "quotes"); GET /Quotation/79 -> 200.
    The three POSTs are spec-documented bare arrays, never fired (no write
    authorization), so verification stays "spec".
    """
    quotations = get_resource("quotations")
    assert quotations.list_key == "quotes"
    # quote_number exists in neither the spec's QuotationHeader nor the
    # tenant; total only appears on the detail record, never on list rows.
    assert "quote_number" not in quotations.table_fields
    assert "total" not in quotations.table_fields

    ops = {(op.method, op.path): op for op in quotations.operations}
    assert set(ops) == {
        ("POST", "/Quotation/Lines"),
        ("POST", "/Quotation/Approval"),
        ("POST", "/Quotation/View"),
    }
    spec = load_spec()
    assert spec is not None
    for (method, path), op in ops.items():
        assert op.body is True
        assert op.args == ()
        assert op.verification == "spec"
        assert str(op.summary).strip()
        assert method.lower() in spec["paths"][path]


def test_crm_notes_write_shape_matches_live_evidence() -> None:
    """crm-notes write metadata, pinned to the 2026-09-30 live read probes.

    Live: envelope {"actions": [...], "record_count": N}; GET /CRMNote/14630
    -> 200; unfiltered list returns 0 records (scope filter required); rows
    carry `datetime`, never the old dead `date` column. The spec declares no
    required fields, so `note` alone is the documented assumption - the anchor
    is polymorphic, so requiring client_id would block supplier/quote notes.
    POST/DELETE were never fired (no write authorization).
    """
    crm = get_resource("crm-notes")

    assert crm.endpoint == "/CRMNote"
    assert crm.list_key == "actions"
    assert crm.table_fields == ("id", "client_id", "datetime", "who_agentid", "note")
    assert "date" not in crm.table_fields
    assert crm.create_endpoint == crm.endpoint
    assert crm.update_endpoint == crm.endpoint
    assert crm.supports_delete is True
    assert crm.required_create_fields == ("note",)
    assert "id" in crm.required_update_fields
    assert crm.effective_write_preview_fields[0] == "id"
    assert set(crm.required_create_fields) <= set(crm.effective_write_preview_fields)

    spec = load_spec()
    assert spec is not None
    assert "post" in spec["paths"]["/CRMNote"]
    assert "delete" in spec["paths"]["/CRMNote/{id}"]
    assert "put" not in spec["paths"]["/CRMNote"]
    assert "patch" not in spec["paths"]["/CRMNote"]


def test_billing_read_resources_stay_read_only() -> None:
    """Slice ④: money-adjacent reads ship first; writes stay raw by design.

    Live-verified 2026-10-01 (all read-only probes, profile thomas):
    invoice-payments -> envelope "payments" (client_name/amount/date on list
    rows); invoice-statuses -> envelope "data" (8 rows, no query params);
    recurring-invoices -> envelope "invoices" (client_name/total/
    nextcreationdate on list rows, negative ids observed). POST/DELETE on all
    three are spec-documented but were never fired: no write authorization,
    and bulk invoice generation / payment recording need read-back
    verification handlers first.
    """
    spec = load_spec()
    assert spec is not None
    for name, endpoint, list_key, fields in (
        (
            "invoice-payments",
            "/InvoicePayment",
            "payments",
            ("id", "invoice_id", "client_name", "amount", "date"),
        ),
        ("invoice-statuses", "/InvoiceStatus", "data", ("id", "status_name", "type")),
        (
            "recurring-invoices",
            "/RecurringInvoice",
            "invoices",
            ("id", "client_name", "total", "nextcreationdate"),
        ),
    ):
        resource = get_resource(name)
        assert resource.endpoint == endpoint, name
        assert resource.list_key == list_key, name
        assert resource.table_fields == fields, name
        # The deliberate slice-④ stance: reads only, no write metadata.
        assert resource.create_endpoint is None, name
        assert resource.update_endpoint is None, name
        assert resource.supports_delete is False, name
        assert not resource.supports_write, name
        # Both read routes exist in the vendored spec (prose gate ally).
        assert "get" in spec["paths"][endpoint], name
        assert "get" in spec["paths"][f"{endpoint}/{{id}}"], name


@pytest.mark.parametrize("resource", RESOURCES)
def test_generated_resource_commands_load(resource) -> None:
    assert runner.invoke(app, [resource.name, "list", "--help"]).exit_code == 0
    # `get` exists only when the registry promises an item route.
    get_help = runner.invoke(app, [resource.name, "get", "--help"]).exit_code
    if resource.supports_get:
        assert get_help == 0
    else:
        assert get_help != 0  # no such command


def test_expenses_exposes_no_get_command() -> None:
    """Halo has no GET /Expense/{id} (probed 404 on 2026-09-29), so `expenses get`
    must not exist — a promise the API cannot keep would always 404."""
    expenses = get_resource("expenses")
    assert expenses.supports_get is False
    result = runner.invoke(app, ["expenses", "get", "1"])
    assert result.exit_code != 0  # no such command
    assert runner.invoke(app, ["expenses", "list", "--help"]).exit_code == 0


def test_list_only_resource_still_advertises_get_for_the_collection() -> None:
    """supports_get=False gates the item route only. The collection route still
    works (GET /Expense answers 403 = permission, not missing), so the MCP
    capability string must stay list-capable and verbs must still offer GET —
    otherwise an agent would wrongly conclude the resource is unreadable."""
    from halocli.mcp_server import _capability_summary, _verbs

    expenses = get_resource("expenses")
    assert "list" in _capability_summary(expenses)
    assert "list/get" not in _capability_summary(expenses)  # no item route claimed
    assert "GET" in _verbs(expenses)


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
