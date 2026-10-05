from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner as ClickCliRunner
from typer.main import get_command
from typer.testing import CliRunner

from halocli.cli import app
from halocli.client import HaloClient
from halocli.config import HaloProfile
from halocli.resources import RESOURCE_BY_COMMAND, RESOURCES, HaloResource, get_resource
from halocli.schema import load_spec


runner = CliRunner()
# Convert the Typer app to its click tree ONCE and drive it with click's own
# runner: typer's CliRunner would rebuild the whole command tree (181
# resources) on every invoke, which made the per-resource --help test ~75% of
# suite runtime (~5 min local, x12 in the CI matrix). The Typer runner also
# rejects a pre-converted tree, hence ClickCliRunner here.
click_app = get_command(app)
click_runner = ClickCliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]

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
    "users",
    "canned-text",
    "workflows",
    "custom-integrations",
    "custom-integration-methods",
    "timesheet-events",
    # Writes-batch-1 (1.7.0): config/reference entities with spec-verified
    # POST + DELETE /{id}. Never fired at the tenant (verification: spec).
    "asset-groups",
    "asset-types",
    "budget-types",
    "cabs",
    "call-scripts",
    "categories",
    "cost-centres",
    "email-templates",
    "faq-lists",
    "item-groups",
    "item-stocks",
    "outcomes",
    "pdf-templates",
    "qualifications",
    "releases",
    "release-types",
    "service-categories",
    "services",
    "stock-bins",
    "tags",
    "ticket-areas",
    "ticket-types",
    "to-do-groups",
    # Writes-batch-2 (1.8.0): config/reference entities chosen from sweep row
    # evidence (money, mail, security, automation and raw-SQL held back).
    "organisations",
    "teams",
    "suppliers",
    "slas",
    "workdays",
    "products",
    "fields",
    "field-groups",
    "field-infos",
    "custom-tables",
    "holidays",
    "lookups",
    # Writes-batch-3 (1.10.0): CUD on route-verified config/reference sets;
    # required fields are house choices from the POST schema (spec declares
    # required: [] everywhere) - bound to the schema by WRITE_BATCH3's test.
    "crm-note-replies",
    "certificates",
    "email-template-variables",
    "release-note-groups",
    "release-pipelines",
    "ticket-type-groups",
    "item-suppliers",
    "product-components",
    # POST-only tier: the spec offers no DELETE /{id}, so delete is withheld
    # (contract amended: delete iff the spec offers it).
    "agent-check-ins",
    "call-log",
    "to-dos",
    # Tackle-the-25 (2026-10-02): contact/address writes + contract visit plans.
    "address",
    "contact-groups",
    "contact-group-contacts",
    "contract-schedule-plans",
)
WRITE_POST_ONLY = (
    "agent-check-ins",
    "call-log",
    "to-dos",
)


def test_registry_names_and_aliases_are_unique() -> None:
    names = [resource.name for resource in RESOURCES]
    commands = [command_name for resource in RESOURCES for command_name in resource.command_names]

    assert len(names) == len(set(names))
    assert len(commands) == len(set(commands))


def test_registry_resources_have_endpoints_and_table_fields() -> None:
    for resource in RESOURCES:
        assert resource.endpoint.startswith("/")
        assert resource.table_fields


def test_registry_still_constructs_all_resources() -> None:
    """The registry size is pinned; a new resource must update this deliberately."""
    assert len(RESOURCES) == 185
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
    """Every WRITE_RESOURCES entry is full-CUD with preview starting at id."""
    assert len(WRITE_RESOURCES) == 67
    for name in WRITE_RESOURCES:
        resource = get_resource(name)
        assert resource.supports_write, name
        assert resource.supports_create, name
        assert resource.supports_update, name
        # Contract (amended for the POST-only tier): delete is offered iff the
        # spec offers DELETE /{id}. Everything not in WRITE_POST_ONLY must be
        # full-CUD; the trio must have no delete route in the spec at all.
        if name in WRITE_POST_ONLY:
            assert not resource.supports_delete, name
            spec_doc = load_spec()
            assert spec_doc is not None
            assert "delete" not in spec_doc["paths"].get(f"{resource.endpoint}/{{id}}", {}), name
        else:
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


WRITE_BATCH1 = (
    "asset-groups",
    "asset-types",
    "budget-types",
    "cabs",
    "call-scripts",
    "categories",
    "cost-centres",
    "email-templates",
    "faq-lists",
    "item-groups",
    "item-stocks",
    "outcomes",
    "pdf-templates",
    "qualifications",
    "releases",
    "release-types",
    "service-categories",
    "services",
    "stock-bins",
    "tags",
    "ticket-areas",
    "ticket-types",
    "to-do-groups",
)


def test_write_batch1_required_fields_are_observed_columns() -> None:
    """Batch-1 create requirements must be live-observed list columns.

    `table_fields` for these resources were derived from production GET sweep
    rows (sweep_results.json), so requiring `table_fields[primary]` ties each
    `required_create_fields` assumption ("the spec declares no required
    fields anywhere") to a column Halo actually returns. Endpoints are
    spec-verified separately by `find_write_mismatches` (POST on the
    collection, DELETE on `{endpoint}/{id}`).
    """
    spec = load_spec()
    assert spec is not None
    assert len(WRITE_BATCH1) == 23
    for name in WRITE_BATCH1:
        resource = get_resource(name)
        assert resource.supports_write, name
        assert resource.supports_delete, name
        assert resource.create_endpoint == resource.endpoint, name
        primary = resource.required_create_fields[0]
        assert primary in resource.table_fields, f"{name}: {primary} not observed"
        assert set(resource.required_create_fields) <= set(
            resource.effective_write_preview_fields
        ), name
        # spec-verified routes: POST collection + DELETE by id
        assert "post" in spec["paths"][resource.endpoint], name
        assert "delete" in spec["paths"][f"{resource.endpoint}/{{id}}"], name


WRITE_BATCH2 = (
    "organisations",
    "teams",
    "suppliers",
    "slas",
    "workdays",
    "products",
    "fields",
    "field-groups",
    "field-infos",
    "custom-tables",
    "holidays",
    "lookups",
)


def test_write_batch2_required_fields_are_observed_columns() -> None:
    """Batch-2 create requirements must be live-observed list columns.

    Same evidence binding as batch 1: `table_fields` derive from production
    sweep rows, so `table_fields[primary]` ties each required create field to
    a column Halo actually returns. Routes are spec-verified by
    `find_write_mismatches` (POST on the collection, DELETE on `{id}`).
    """
    spec = load_spec()
    assert spec is not None
    assert len(WRITE_BATCH2) == 12
    for name in WRITE_BATCH2:
        resource = get_resource(name)
        assert resource.supports_write, name
        assert resource.supports_delete, name
        assert resource.create_endpoint == resource.endpoint, name
        primary = resource.required_create_fields[0]
        assert primary in resource.table_fields, f"{name}: {primary} not observed"
        assert set(resource.required_create_fields) <= set(
            resource.effective_write_preview_fields
        ), name
        assert "post" in spec["paths"][resource.endpoint], name
        assert "delete" in spec["paths"][f"{resource.endpoint}/{{id}}"], name


def test_contracts_reads_client_contract_and_stays_read_only() -> None:
    contracts = get_resource("contracts")
    # Live-verified 2026-09-28: GET /Contract is 404 on the real tenant; reads
    # point at /ClientContract, which returns 200.
    assert contracts.endpoint == "/ClientContract"
    assert not contracts.supports_write
    assert not contracts.supports_create
    assert not contracts.supports_update
    assert contracts.supports_delete is False

    # The read endpoint is spec-documented with GET. The tackle-the-25 pass
    # (2026-10-02) resolved the long-standing write decision AGAINST
    # promotion, on evidence: POST /ClientContract's schema carries prepay
    # auto-topup fields (autotopup*) and outbound flags
    # (_send_appointment_invites/_send_outstanding_emails), approval carries
    # a signature/token, and POST /SupplierContract is a different entity
    # behind a 403 scope for our agents. The core is argued-raw in policy
    # op_overrides; `contracts next-ref` ships as a first-class op. This
    # guard keeps any future flip deliberate.
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
    assert actions.required_create_fields == ("ticket_id", "note", "outcome_id")
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
        # live-dev from the nested sweep (dev_write_results.json): lines and
        # view fired on the trial; approval was rejected ("quote has
        # expired") and therefore stays spec-backed.
        expected = "spec" if op.name == "approval" else "live-dev"
        assert op.verification == expected
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


def test_users_write_shape_is_live_proven() -> None:
    """users write metadata, pinned to the 2026-10-01 live chain (issue #28).

    Live: created test user 4266 under client 625 with exactly this create
    set (Halo's sequential 400s named `name` and `site_id` explicitly);
    password set via update against the tenant policy; MFA reset via
    {"id": N, "_revoke_authenticatorapp": true} returned 200 even for an
    unenrolled user (write-only flag, not echoed).
    """
    users = get_resource("users")

    assert users.endpoint == "/Users"
    assert users.create_endpoint == users.endpoint
    assert users.update_endpoint == users.endpoint
    assert users.supports_delete is True
    assert users.required_create_fields == (
        "firstname",
        "surname",
        "name",
        "emailaddress",
        "client_id",
        "site_id",
    )
    assert "id" in users.required_update_fields
    assert users.effective_write_preview_fields[0] == "id"
    assert set(users.required_create_fields) <= set(users.effective_write_preview_fields)

    spec = load_spec()
    assert spec is not None
    assert "post" in spec["paths"]["/Users"]
    assert "delete" in spec["paths"]["/Users/{id}"]
    assert "put" not in spec["paths"]["/Users"]
    assert "patch" not in spec["paths"]["/Users"]
    # The MFA action flag lives on the schema the upsert sends.
    users_schema = spec["components"]["schemas"]["Users"]["properties"]
    assert "_revoke_authenticatorapp" in users_schema
    assert "resetpassword" in users_schema


def test_canned_text_shape_matches_live_evidence() -> None:
    """canned-text write metadata, pinned to the 2026-10-01 live reads.

    Live: GET /CannedText -> 200 BARE array (5 rows, so list_key stays None);
    GET /CannedText/1 -> 200 (15 keys, text/html present, _canupdate true).
    The spec declares no required fields; name+text is the practical
    minimum. POST/DELETE/favourite never fired.
    """
    canned = get_resource("canned-text")

    assert canned.endpoint == "/CannedText"
    assert canned.list_key is None  # bare array, not an envelope
    assert canned.table_fields == ("id", "name", "group_id", "restriction_type")
    assert canned.create_endpoint == canned.endpoint
    assert canned.update_endpoint == canned.endpoint
    assert canned.supports_delete is True
    assert canned.required_create_fields == ("name", "text")
    assert "id" in canned.required_update_fields
    assert canned.effective_write_preview_fields[0] == "id"
    assert set(canned.required_create_fields) <= set(canned.effective_write_preview_fields)

    favourite = {op.name: op for op in canned.operations}["favourite"]
    assert (favourite.method, favourite.path, favourite.body) == (
        "POST",
        "/CannedText/favourite",
        True,
    )
    # fired on the dev trial (dev_write_results.json -> nested.favourite)
    assert favourite.verification == "live-dev"

    spec = load_spec()
    assert spec is not None
    assert "post" in spec["paths"]["/CannedText"]
    assert "delete" in spec["paths"]["/CannedText/{id}"]
    assert "post" in spec["paths"]["/CannedText/favourite"]


def test_timesheet_events_shape_matches_live_quirks() -> None:
    """timesheet-events metadata, pinned to the 2026-10-01 live probes.

    Live: BARE array whose count/page params are ignored (full dump per call,
    issue #24 - agent_id/ISO-date filters are the only real bound); the API
    view exposes id=0 on every row (real TSEeventid never surfaced);
    create-without-id -> 515, id=0 -> 515, unknown nonzero id -> "Record not
    found"; /mine -> 403. The create contract mirrors the workspace-proven
    QuickTime recipe (bifrost-workspace timeentry.py: subject + start/end,
    duration derived - no timetaken), whose richer shape lives in
    write_preview_fields.
    """
    tse = get_resource("timesheet-events")

    assert tse.endpoint == "/TimesheetEvent"
    assert tse.list_key is None  # bare array, not an envelope
    assert tse.table_fields == (
        "start_date",
        "end_date",
        "agent_id",
        "ticket_id",
        "timetaken",
        "subject",
    )
    assert tse.create_endpoint == tse.endpoint
    assert tse.update_endpoint == tse.endpoint
    assert tse.supports_delete is True
    # QuickTime core: Halo derives duration from start/end (production payload
    # sends no timetaken), so the old (subject, timetaken) set would block it.
    assert tse.required_create_fields == ("subject", "start_date", "end_date")
    assert "id" in tse.required_update_fields
    assert tse.effective_write_preview_fields[0] == "id"
    assert set(tse.required_create_fields) <= set(tse.effective_write_preview_fields)
    # The QuickTime shape shows in preview without pass-through warnings.
    for field in ("note", "event_type", "lognewticket", "charge_rate", "agents", "break_note"):
        assert field in tse.write_preview_fields, field

    mine = {op.name: op for op in tse.operations}["mine"]
    assert (mine.method, mine.path) == ("GET", "/TimesheetEvent/mine")
    assert mine.verification == "live:403"
    assert not mine.write

    spec = load_spec()
    assert spec is not None
    assert "post" in spec["paths"]["/TimesheetEvent"]
    assert "delete" in spec["paths"]["/TimesheetEvent/{id}"]
    assert "get" in spec["paths"]["/TimesheetEvent/mine"]
    assert "put" not in spec["paths"]["/TimesheetEvent"]


def test_appointments_completion_recipe_is_in_write_shape() -> None:
    """The appointment-completion recipe (workspace timeentry.py) previews clean.

    Live-exercised 2026-10-01: upserting an existing appointment with
    complete_* fields + full echo fields marks dispatch done and logs actual
    time. Every field of that recipe must sit in write_preview_fields so the
    preview shows it instead of firing a pass-through warning for each.
    """
    appts = get_resource("appointments")
    recipe = {
        "id",
        "subject",
        "start_date",
        "end_date",
        "allday",
        "is_private",
        "agents",
        "user_id",
        "ticket_id",
        "reminderminutes",
        "agent_status",
        "note_html",
        "is_task",
        "appointment_type_id",
        "shift_type_id",
        "followup_start_date",
        "followup_end_date",
        "followup_allday",
        "followup_is_private",
        "followup_user_id",
        "followup_reminderminutes",
        "followup_agent_status",
        "followup_note_html",
        "followup_agent_id",
        "complete_status",
        "chargerate",
        "complete_date",
        "complete_timetaken",
        "complete_notehtml",
        "complete_agent_id",
        "utcoffset",
        "apfaultidremoved",
        "agent_id",
    }
    missing = recipe - set(appts.write_preview_fields)
    assert not missing, (
        f"completion fields missing from appointments write shape: {sorted(missing)}"
    )
    assert "complete_timetaken" in appts.write_preview_fields


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
    assert click_runner.invoke(click_app, [resource.name, "list", "--help"]).exit_code == 0
    # `get` exists only when the registry promises an item route.
    get_help = click_runner.invoke(click_app, [resource.name, "get", "--help"]).exit_code
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
                "sites": [{"id": 1, "name": "HQ", "client_name": "Example", "ignored": "hidden"}],
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


SWEEP_PROMOTED = (
    # phase-2 batch 1
    "outgoing",
    "outgoing-attempts",
    "email-templates",
    "tags",
    "popup-notes",
    "lookups",
    "outcomes",
    "call-log",
    "mailboxes",
    "charge-rates",
    # phase-2 batch 2 (2026-10-02)
    "address",
    "agent-check-ins",
    "approval-process",
    "approval-process-rules",
    "asset-groups",
    "asset-types",
    "automations",
    "billing-templates",
    "booking-types",
    "budget-types",
    "cabs",
    "call-scripts",
    "client-prepays",
    "consignments",
    "cost-centres",
    "currencies",
    "custom-buttons",
    "custom-queries",
    "custom-tables",
    "dashboard-links",
    "database-lookups",
    "distribution-lists",
    "email-address-books",
    "email-rules",
    "email-stores",
    "events",
    "event-rules",
    "faq-lists",
    "feeds",
    "feedbacks",
    "fields",
    "field-groups",
    "field-infos",
    "holidays",
    "incoming-webhook-attempts",
    "invoice-changes",
    "item-groups",
    "item-stocks",
    "item-stock-histories",
    "journeys",
    "licence-changes",
    "notifications",
    "notification-messages",
    "organisations",
    "pdf-templates",
    "products",
    "purchase-orders",
    "qualifications",
    "release-types",
    "roles",
    "sales-mailboxes",
    "sales-mailbox-details",
    "sales-orders",
    "schedules",
    "schedule-occurrences",
    "services",
    "service-categories",
    "service-request-details",
    "service-restrictions",
    "stock-bins",
    "stock-traces",
    "taxes",
    "templates",
    "ticket-approvals",
    "ticket-areas",
    "ticket-rules",
    "ticket-type-fields",
    "to-do-groups",
    "user-changes",
    "user-roles",
    "view-columns",
    "view-filters",
    "view-list-groups",
    "view-lists",
    "workflows",
    "workflow-targets",
    "formattedemails",
    "workflowsteps",
)


def test_sweep_promoted_columns_come_from_sweep_evidence() -> None:
    """Every sweep-promoted declaration is bound to live evidence, not guesses.

    For each promoted resource: every non-id table_field must appear in the
    sweep-observed row keys (sweep_results.json; the sweep caps row_keys at 30
    sorted keys, which is why the universal `id` is exempt and checked against
    the spec schema instead), list_key must match the observed envelope, and
    supports_get must match what the id-route probe actually answered.
    """
    sweep = json.loads((REPO_ROOT / "sweep_results.json").read_text(encoding="utf-8"))
    spec = load_spec()
    assert spec is not None

    for name in SWEEP_PROMOTED:
        resource = get_resource(name)
        evidence = sweep[f"GET {resource.endpoint}"]
        assert evidence["status"] == 200 and evidence["rows"], name

        observed = set(evidence.get("row_keys") or [])
        declared = set(resource.table_fields)
        assert "id" in declared, name
        assert declared - {"id"} <= observed, (
            f"{name}: columns not observed on a live row: {sorted(declared - observed - {'id'})}"
        )

        envelope = evidence.get("envelope")
        if envelope in ("<bare array>", "<object>"):
            assert resource.list_key is None, name
        else:
            assert resource.list_key == envelope, name

        id_evidence = sweep.get(f"GET {resource.endpoint}/{{id}}")
        if resource.supports_get:
            # 500 counts as route-alive (the route exists; the probe token
            # crashed the handler) - cf. Holiday in the batch-2 comments.
            assert id_evidence and id_evidence["status"] in (400, 404, 500), (
                f"{name}: id-route evidence {id_evidence} does not support supports_get=True"
            )
            assert f"{resource.endpoint}/{{id}}" in spec["paths"], name
        else:
            assert id_evidence is None, name
            assert f"{resource.endpoint}/{{id}}" not in spec["paths"], name
        assert "get" in spec["paths"][resource.endpoint], name


ROUTE_VERIFIED_EMPTY = (
    "area-request-types",
    "audits",
    "bulk-emails",
    "cab-members",
    "cab-roles",
    "call-events",
    "certificates",
    "change-calendars",
    "confirm-closures",
    "contact-group-contacts",
    "contact-groups",
    "contract-rules",
    "contract-schedule-plans",
    "contract-schedules",
    "crm-note-replies",
    "csp-consumption-data",
    "csp-invoices",
    "csv-templates",
    "device-licences",
    "distribution-list-logs",
    "downtimes",
    "email-template-variables",
    "escalation-messages",
    "historical-ticket-volumes",
    "invoice-detail-prorata",
    "item-suppliers",
    "mail-campaign-logs",
    "meter-readings",
    "powershell-script-criteria",
    "powershell-script-processing",
    "powershell-scripts",
    "product-branches",
    "product-components",
    "publish-profiles",
    "recurring-items",
    "release-note-groups",
    "release-pipelines",
    "remote-sessions",
    "report-repositories",
    "resource-types",
    "saved-forecasts",
    "service-availabilities",
    "service-statuses",
    "single-sign-on-attempts",
    "software-licence-roles",
    "supplier-contracts",
    "tax-rules",
    "ticket-type-groups",
    "timeslots",
    "to-dos",
    "transcription-stores",
    "xtype-roles",
)


def test_route_verified_empty_resources_match_probe_evidence() -> None:
    """Route-verified reads bind to probe evidence: alive but tenant-empty.

    Each resource's table_fields must stay id-only (the spec ships no response
    schemas and the tenant holds no rows - inventing columns would be a lie),
    its list_key must equal the sweep-observed envelope, and supports_get must
    match the by-id probe verdict. probe_results.json records every attempt.
    """
    probe = json.loads((REPO_ROOT / "probe_results.json").read_text(encoding="utf-8"))
    sweep = json.loads((REPO_ROOT / "sweep_results.json").read_text(encoding="utf-8"))
    assert len(ROUTE_VERIFIED_EMPTY) == 52
    for name in ROUTE_VERIFIED_EMPTY:
        resource = get_resource(name)
        rec = probe[f"GET {resource.endpoint}"]
        attempts = rec.get("attempts", []) + rec.get("pass2_attempts", [])
        assert any(a.get("status") == 200 for a in attempts), name  # route alive
        assert not rec.get("rows"), name  # tenant holds no rows
        assert resource.table_fields == ("id",), name  # no invented columns
        env = (sweep.get(f"GET {resource.endpoint}") or {}).get("envelope")
        assert resource.list_key == (None if env == "<bare array>" else env), name
        sw_byid = sweep.get(f"GET {resource.endpoint}/{{id}}")
        alive = bool(sw_byid and str(sw_byid.get("status")) in {"400", "404", "500"})
        assert resource.supports_get is alive, name


PROBE_WINNERS = (
    "asset-changes",
    "asset-software",
    "incoming-emails",
)


def test_probe_winners_bind_to_observed_rows() -> None:
    """Row-evidenced promotions: columns come from the probe's live rows."""
    probe = json.loads((REPO_ROOT / "probe_results.json").read_text(encoding="utf-8"))
    sweep = json.loads((REPO_ROOT / "sweep_results.json").read_text(encoding="utf-8"))
    assert len(PROBE_WINNERS) == 3
    for name in PROBE_WINNERS:
        resource = get_resource(name)
        rec = probe[f"GET {resource.endpoint}"]
        assert rec.get("rows"), name
        assert rec.get("winner_params"), name  # rows needed a real filter
        assert set(resource.table_fields) <= set(rec["row_keys"]), name
        env = rec.get("envelope")
        assert resource.list_key == (None if env == "<bare array>" else env), name
        # supports_get binds to the by-id probe: absent route -> False,
        # probe-token alive (400/404/500) -> True
        sw_byid = sweep.get(f"GET {resource.endpoint}/{{id}}")
        alive = bool(sw_byid and str(sw_byid.get("status")) in {"400", "404", "500"})
        assert resource.supports_get is alive, name


ROUTE_VERIFIED_OPS = (
    ("agents", "/Agent/me"),
    ("sites", "/Site/StockBins"),
    ("assets", "/Asset/GetAllSoftwareVersions"),
    ("assets", "/Asset/NextTag"),
    ("teams", "/Team/Tree"),
    ("timesheet-events", "/Timesheet/forecasting"),
    ("downtimes", "/Downtime/DowntimeCalendar"),
    ("report-repositories", "/ReportRepository/ReportCategories"),
)


def test_route_verified_ops_are_live_declared_and_prosed() -> None:
    """The 8 new nested GETs: declared, live-probed, spec prose present."""
    spec = load_spec()
    assert spec is not None
    probe = json.loads((REPO_ROOT / "probe_results.json").read_text(encoding="utf-8"))
    sweep = json.loads((REPO_ROOT / "sweep_results.json").read_text(encoding="utf-8"))
    assert len(ROUTE_VERIFIED_OPS) == 8
    for resource_name, op_path in ROUTE_VERIFIED_OPS:
        resource = get_resource(resource_name)
        op = next((o for o in resource.operations if o.path == op_path), None)
        assert op is not None, (resource_name, op_path)
        assert op.method == "GET", op_path
        assert op.verification == "live", op_path
        spec_op = spec["paths"].get(op_path, {}).get("get")
        assert spec_op is not None, op_path
        assert str(spec_op.get("summary") or "").strip(), op_path
        assert str(spec_op.get("description") or "").strip(), op_path
        evidence = sweep.get(f"GET {op_path}") or probe.get(f"GET {op_path}")
        assert evidence is not None, op_path
        assert evidence.get("status") == 200, op_path


WRITE_BATCH3 = (
    "crm-note-replies",
    "certificates",
    "email-template-variables",
    "release-note-groups",
    "release-pipelines",
    "ticket-type-groups",
    "item-suppliers",
    "product-components",
)


def test_write_batch3_required_fields_come_from_spec_post_schemas() -> None:
    """Batch-3 creates bind to the spec POST schemas (no rows exist to observe).

    The vendored spec declares `required: []` everywhere, so the required set
    is a commented house choice - but it must be a subset of the POST body's
    own properties, and the routes must exist (POST collection + DELETE {id}).
    """
    spec = load_spec()
    assert spec is not None

    def props(path: str) -> set[str]:
        schema = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        if schema.get("type") == "array":
            schema = schema.get("items", {})
        if "$ref" in schema:
            node = spec
            for part in schema["$ref"].lstrip("#/").split("/"):
                node = node[part]
            schema = node
        return set((schema.get("properties") or {}).keys())

    assert len(WRITE_BATCH3) == 8
    for name in WRITE_BATCH3:
        resource = get_resource(name)
        assert resource.supports_delete, name
        assert "post" in spec["paths"][resource.endpoint], name
        assert "delete" in spec["paths"][f"{resource.endpoint}/{{id}}"], name
        body_props = props(resource.endpoint)
        assert set(resource.required_create_fields) <= body_props, name
        assert resource.effective_write_preview_fields[0] == "id", name
        assert set(resource.required_create_fields) <= set(
            resource.effective_write_preview_fields
        ), name


def test_post_only_tier_offers_no_delete_command() -> None:
    """The POST-only trio: create/update first-class, delete withheld by spec."""
    spec = load_spec()
    assert spec is not None
    assert len(WRITE_POST_ONLY) == 3
    for name in WRITE_POST_ONLY:
        resource = get_resource(name)
        assert resource.supports_create and resource.supports_update, name
        assert not resource.supports_delete, name
        assert "post" in spec["paths"][resource.endpoint], name
        assert "delete" not in spec["paths"].get(f"{resource.endpoint}/{{id}}", {}), name
        assert resource.required_create_fields, name


TACKLE25_CUD = (
    "address",
    "contact-groups",
    "contact-group-contacts",
    "contract-schedule-plans",
)


def test_tackle25_writes_bind_to_spec_post_schemas() -> None:
    """The four promote-from-deferral writes bind to spec POST schema props."""
    spec = load_spec()
    assert spec is not None

    def props(path: str) -> set[str]:
        schema = spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        if "$ref" in schema:
            node = spec
            for part in schema["$ref"].lstrip("#/").split("/"):
                node = node[part]
            schema = node
        if schema.get("type") == "array":
            schema = schema.get("items", {})
            if "$ref" in schema:
                node = spec
                for part in schema["$ref"].lstrip("#/").split("/"):
                    node = node[part]
                schema = node
        return set((schema.get("properties") or {}).keys())

    assert len(TACKLE25_CUD) == 4
    for name in TACKLE25_CUD:
        resource = get_resource(name)
        assert resource.supports_create and resource.supports_update, name
        assert resource.supports_delete, name
        assert "post" in spec["paths"][resource.endpoint], name
        assert "delete" in spec["paths"][f"{resource.endpoint}/{{id}}"], name
        assert set(resource.required_create_fields) <= props(resource.endpoint), name
        assert resource.effective_write_preview_fields[0] == "id", name
