from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ResourceOperation:
    """A first-class command for one nested endpoint under a resource.

    Example: ``halo invoices pdf 42`` → ``POST /Invoice/PDF/{id}``.

    ``path`` must be the *exact* spec path template (including ``{id}`` placeholders)
    so the coverage oracle can verify it against the vendored OpenAPI document.
    ``args`` lists the path parameters in template order; the CLI validates arity
    and substitutes them positionally.

    ``verification`` records how much we actually know, honestly:

    * ``live``             — probed against the real tenant, works
    * ``live:403`` / ``live:500`` / ``live:404`` — probed; tenant answered that status
    * ``route-verified``   — route existence proven (400 on a malformed id vs the
                             known-missing control), but no live data to fetch
    * ``spec``             — documented in the vendored spec, not probed (writes
                             are never fired at a real tenant without an operator)
    """

    name: str
    method: str
    path: str
    args: tuple[str, ...] = ()
    body: bool = False
    multipart: bool = False
    summary: str = ""
    verification: str = "spec"
    # Name of a custom CLI implementation registered in cli.py. Empty means the
    # generic dispatcher (preview/apply, --data/--file/--save) is used. Set it
    # when an operation needs behaviour the generic path cannot express, e.g.
    # `reports run` (execute a report and shape its result set) or
    # `reports clone` (derive the body from another report). Kept on the
    # declaration, not in a cli.py lookup table, so renaming the operation
    # cannot silently fall back to the generic implementation.
    handler: str = ""

    @property
    def write(self) -> bool:
        return self.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class HaloResource:
    name: str
    endpoint: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    table_fields: tuple[str, ...] = ("id", "name")
    list_key: str | None = None
    supports_get: bool = True
    # Write metadata. Halo convention: POST to the collection creates a record when no
    # `id` is supplied and updates the record when `id` is set; DELETE removes
    # `{collection}/{id}`. Resources without write metadata stay read-only.
    create_endpoint: str | None = None
    update_endpoint: str | None = None
    required_create_fields: tuple[str, ...] = ()
    required_update_fields: tuple[str, ...] = ()
    supports_delete: bool = False
    # Fields shown in a dry-run preview; when empty, defaults to id + required fields.
    write_preview_fields: tuple[str, ...] = ()
    # First-class commands for nested endpoints (see ResourceOperation).
    operations: tuple[ResourceOperation, ...] = ()

    @property
    def command_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def supports_create(self) -> bool:
        return self.create_endpoint is not None

    @property
    def supports_update(self) -> bool:
        return self.update_endpoint is not None

    @property
    def supports_write(self) -> bool:
        return self.supports_create or self.supports_update

    @property
    def effective_write_preview_fields(self) -> tuple[str, ...]:
        """Preview shape: the explicit override, else id plus the required fields."""
        if self.write_preview_fields:
            return self.write_preview_fields
        merged: list[str] = []
        for key in ("id", *self.required_create_fields, *self.required_update_fields):
            if key not in merged:
                merged.append(key)
        return tuple(merged)


RESOURCES: tuple[HaloResource, ...] = (
    HaloResource(
        "tickets",
        "/Tickets",
        aliases=("ticket",),
        table_fields=("id", "summary", "status_name", "client_name", "agent_name"),
        # POST /Tickets creates (no id) and updates (id set) — closing is an update that
        # sets status_id. Halo docs vary: some instances also require client_id and
        # tickettype_id on create, so only summary is enforced here. DELETE /Tickets/{id}.
        create_endpoint="/Tickets",
        update_endpoint="/Tickets",
        required_create_fields=("summary",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "summary", "client_id", "status_id", "priority_id"),
        operations=(
            ResourceOperation(
                "create-object",
                "POST",
                "/Tickets/Object",
                body=True,
                summary="Create a single ticket object (spec summary empty; POST-with-id updates)",
            ),
            ResourceOperation(
                "set-billable-project",
                "POST",
                "/Tickets/SetBillableProject",
                summary="Set a ticket's billable project (query params; no request body per spec)",
            ),
            ResourceOperation(
                "view",
                "POST",
                "/Tickets/View",
                body=True,
                summary="Run a saved ticket view (POST-as-read; still write-gated)",
            ),
            ResourceOperation(
                "process-children",
                "POST",
                "/Tickets/processchildren",
                body=True,
                summary="Process a ticket's child tickets",
            ),
            ResourceOperation(
                "salesmailbox",
                "GET",
                "/Tickets/salesmailbox",
                summary="Sales mailbox info; this tenant answers 500 (server error)",
                verification="live:500",
            ),
            ResourceOperation(
                "vote",
                "POST",
                "/Tickets/vote",
                body=True,
                summary="Cast a vote on a ticket",
            ),
            ResourceOperation(
                "zapier",
                "GET",
                "/Tickets/zapier",
                summary="Zapier integration config (live-verified against the tenant)",
                verification="live",
            ),
        ),
    ),
    HaloResource(
        "clients",
        "/Client",
        aliases=("client",),
        table_fields=("id", "name", "accountmanager_name"),
        # POST /Client (Area) creates or updates by id; DELETE /Client/{id} exists.
        create_endpoint="/Client",
        update_endpoint="/Client",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "inactive"),
    ),
    HaloResource(
        "agents",
        "/Agent",
        aliases=("agent",),
        table_fields=("id", "name", "team", "use"),
        # POST /Agent (Uname) creates or updates by id; DELETE /Agent/{id} exists.
        # Assumption: name + email are required to create an agent (email is the login);
        # instances may differ.
        create_endpoint="/Agent",
        update_endpoint="/Agent",
        required_create_fields=("name", "email"),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "email", "use", "team"),
    ),
    HaloResource("teams", "/Team", aliases=("team",), table_fields=("id", "name")),
    HaloResource(
        "users",
        "/Users",
        aliases=("user",),
        table_fields=("id", "name", "client_name", "emailaddress"),
    ),
    HaloResource(
        "kb",
        "/KBArticle",
        aliases=("kb-articles", "kb-article"),
        # Live-verified 2026-09-30: list -> 200 with envelope
        # {"articles": [...], "record_count": N} and real pagination (298
        # distinct rows across pages); GET /KBArticle/391 -> 200; malformed id
        # -> 400 validation (route-verified). table_fields corrected: live rows
        # carry `name` but never `title`. POST is a spec-declared bare array
        # (returns 201); execute_write's json_body=[payload] matches it, so no
        # custom handler is needed. POST/DELETE are never fired at the tenant
        # (no write authorization) -> writes stay verification: spec.
        table_fields=("id", "name", "type", "inactive", "date_edited"),
        list_key="articles",
        create_endpoint="/KBArticle",
        update_endpoint="/KBArticle",
        # The spec declares NO required fields on POST /KBArticle (requestBody,
        # schema and items all omit "required"). Assumption, per the `agents`
        # precedent: name (title) + description (body) are the minimum for a
        # useful article; Halo may accept less.
        required_create_fields=("name", "description"),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "description", "resolution", "type", "inactive"),
    ),
    HaloResource(
        "sites",
        "/Site",
        aliases=("site",),
        table_fields=("id", "name", "client_name"),
        # POST /Site creates or updates by id; DELETE /Site/{id} exists.
        create_endpoint="/Site",
        update_endpoint="/Site",
        required_create_fields=("name", "client_id"),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "client_id", "sla_id", "inactive"),
    ),
    HaloResource(
        "assets",
        "/Asset",
        aliases=("asset",),
        table_fields=("id", "inventory_number", "name", "client_name", "site_name"),
        # POST /Asset (Device) creates or updates by id; DELETE /Asset/{id} exists.
        # Assumption: the Device schema has no "name" field — assets are identified by
        # inventory_number (+ assettype_id); only client_id is enforced as required.
        create_endpoint="/Asset",
        update_endpoint="/Asset",
        required_create_fields=("client_id",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=(
            "id",
            "inventory_number",
            "assettype_id",
            "client_id",
            "site_id",
        ),
    ),
    HaloResource(
        "actions",
        "/Actions",
        aliases=("action",),
        table_fields=("id", "ticket_id", "who", "note"),
        # POST /Actions creates a ticket action/note (ticket_id set, no id) or updates
        # one (id set); DELETE /Actions/{id} exists. Instance config may additionally
        # require outcome/outcome_id for some action types, so only ticket_id + note are
        # enforced here.
        create_endpoint="/Actions",
        update_endpoint="/Actions",
        required_create_fields=("ticket_id", "note"),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "ticket_id", "note", "outcome", "hiddenfromuser"),
    ),
    HaloResource(
        "statuses",
        "/Status",
        aliases=("status",),
        table_fields=("id", "name", "use"),
        # POST /Status (TStatus) creates or updates by id; DELETE /Status/{id} exists.
        create_endpoint="/Status",
        update_endpoint="/Status",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "type", "sequence"),
    ),
    HaloResource(
        "priorities",
        "/Priority",
        aliases=("priority",),
        table_fields=("id", "name", "sequence"),
        # POST /Priority (Policy) creates or updates by id; DELETE /Priority/{id} exists.
        create_endpoint="/Priority",
        update_endpoint="/Priority",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "colour", "slaid"),
    ),
    HaloResource(
        "categories",
        "/Category",
        aliases=("category",),
        table_fields=("id", "name", "value"),
    ),
    HaloResource(
        "ticket-types",
        "/TicketType",
        aliases=("ticket-type", "tickettypes"),
        table_fields=("id", "name", "guid"),
    ),
    HaloResource("slas", "/SLA", aliases=("sla",), table_fields=("id", "name")),
    HaloResource(
        "appointments",
        "/Appointment",
        aliases=("appointment",),
        table_fields=("id", "subject", "agent_id", "start_date", "end_date"),
        # POST /Appointment creates or updates by id; DELETE /Appointment/{id} exists.
        # Assumption: subject + start_date + end_date are required; agent_id defaults to
        # the authenticated agent when omitted.
        create_endpoint="/Appointment",
        update_endpoint="/Appointment",
        required_create_fields=("subject", "start_date", "end_date"),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "subject", "agent_id", "start_date", "end_date"),
    ),
    HaloResource(
        "contracts",
        # Verified against the live tenant (2026-09-28): GET /Contract is 404 —
        # the spec has no /Contract path at all. GET /ClientContract returns 200
        # (list verified; Halo's own array key is "contracts"), so reads point
        # there (client contracts; supplier contracts live at /SupplierContract,
        # which this tenant's agents get 403 on). Note: GET /ClientContract/{id}
        # returns 401 for a team-leader agent while list succeeds — Halo uses 401
        # for module-permission denial on detail endpoints, so `contracts get`
        # needs contract-module view rights that `contracts list` does not.
        "/ClientContract",
        aliases=("contract",),
        table_fields=("id", "client_name", "contracttype_name", "end_date"),
        # Read-only: POST exists on /ClientContract, but enabling it would silently
        # narrow "contracts" writes to client contracts only — a semantic decision,
        # not a mechanical one. DELETE /ClientContract/{id} exists in the spec but
        # stays disabled until that decision is made deliberately.
        supports_delete=False,
    ),
    HaloResource(
        "invoices",
        "/Invoice",
        aliases=("invoice",),
        table_fields=("id", "invoice_number", "client_name", "total"),
        operations=(
            ResourceOperation(
                "pdf",
                "POST",
                "/Invoice/PDF/{id}",
                args=("id",),
                summary="Render an invoice PDF (binary response; use --save)",
            ),
            ResourceOperation(
                "view",
                "POST",
                "/Invoice/View",
                body=True,
                summary="Run a saved invoice view (POST-as-read; still write-gated)",
            ),
            ResourceOperation(
                "lines",
                "GET",
                "/Invoice/lines",
                summary="Invoice line items; 403 without billing permission",
                verification="live:403",
            ),
            ResourceOperation(
                "update-lines",
                "POST",
                "/Invoice/updatelines",
                body=True,
                summary="Update invoice line items from a JSON body",
            ),
            ResourceOperation(
                "void",
                "POST",
                "/Invoice/{id}/void",
                args=("id",),
                summary="Void an invoice (destructive; write-gated)",
            ),
        ),
    ),
    HaloResource(
        "invoice-payments",
        "/InvoicePayment",
        aliases=("invoice-payment",),
        # Live-verified 2026-10-01: list -> 200, envelope
        # {"payments": [...], "record_count": N}; client_name/amount/date all
        # present on list rows. Read-only BY DESIGN (slice ④): recording a
        # payment is money-adjacent with no idempotency key in the schema, so
        # POST /InvoicePayment stays raw until a handler can pre-flight the
        # invoice and read the write back (recon risk rank: high).
        table_fields=("id", "invoice_id", "client_name", "amount", "date"),
        list_key="payments",
    ),
    HaloResource(
        "invoice-statuses",
        "/InvoiceStatus",
        aliases=("invoice-status",),
        # Live-verified 2026-10-01: list -> 200, envelope {"data": [...]}
        # (8 rows); the route takes no query parameters. GET prose (summary +
        # description) was missing upstream and is filled from
        # halo_overlay.json. Read-only for now: POST/DELETE are spec-
        # documented config writes, never fired (no write authorization) -
        # they can join the preview-first surface later without new risk
        # beyond status configuration.
        table_fields=("id", "status_name", "type"),
        list_key="data",
    ),
    HaloResource(
        "recurring-invoices",
        "/RecurringInvoice",
        aliases=("recurring-invoice",),
        # Live-verified 2026-10-01: list -> 200, envelope
        # {"invoices": [...], "page_size", "record_count"}; client_name,
        # total and nextcreationdate all present on list rows. Ids are
        # negative on this tenant (observed -240, -237). Read-only BY DESIGN
        # (slice ④): schedules mint real invoices, so POST
        # /RecurringInvoice/process (bulk-generates invoices from an id array)
        # and the upsert stay raw - the highest-stakes write in the recon
        # needs a preview that GETs each id and shows client/total/dates first.
        table_fields=("id", "client_name", "total", "nextcreationdate"),
        list_key="invoices",
    ),
    HaloResource(
        "opportunities",
        # Verified against the live tenant (2026-09-28): GET /Opportunity is 404;
        # the spec documents /Opportunities (GET/POST), which this agent gets 403
        # on — correct path, no Sales-module permission. Table fields unverified
        # for the same reason; missing columns drop out of table output harmlessly.
        "/Opportunities",
        aliases=("opportunity",),
        table_fields=("id", "summary", "client_name", "status_name"),
    ),
    HaloResource(
        "projects",
        # Verified against the live tenant (2026-09-28): GET /Project is 404;
        # GET /Projects returns 200 and is the only project read the v2 API
        # offers — the spec models it as fault-shaped ("List of Faults",
        # /Projects/{id} = "Get one Faults", /Projects/View = array[Faults]),
        # i.e. Halo exposes projects through the Faults schema rather than as
        # separate entities. Rows carry `summary`, not `name`/`status_name`.
        "/Projects",
        aliases=("project",),
        table_fields=("id", "summary", "client_name"),
    ),
    HaloResource("suppliers", "/Supplier", aliases=("supplier",), table_fields=("id", "name")),
    HaloResource(
        "items",
        "/Item",
        aliases=("item",),
        table_fields=("id", "name", "sku", "sales_price"),
    ),
    HaloResource(
        "quotations",
        "/Quotation",
        aliases=("quotation", "quotes", "quote"),
        # Live-verified 2026-09-30: list -> 200 (223 rows under "quotes");
        # GET /Quotation/79 -> 200 (total/lines present on the detail record
        # only). `quote_number` exists in neither the spec's QuotationHeader
        # nor this tenant, and `total` never appears on list rows - both
        # dropped from the table projection.
        table_fields=("id", "title", "client_name", "status", "date"),
        list_key="quotes",
        operations=(
            # All three are spec-documented bare-array POSTs with no path args
            # and no query params -> generic dispatcher (zero-network preview,
            # execution requires both --apply and --yes; --data passes through
            # verbatim, so supply the array). Never fired at the tenant (no
            # write authorization) -> verification: spec.
            ResourceOperation(
                name="lines",
                method="POST",
                path="/Quotation/Lines",
                body=True,
                summary="Add or replace quotation lines",
            ),
            ResourceOperation(
                name="approval",
                method="POST",
                path="/Quotation/Approval",
                body=True,
                summary="Record a quotation approval decision (id, result, token, signature)",
            ),
            ResourceOperation(
                name="view",
                method="POST",
                path="/Quotation/View",
                body=True,
                summary="Record a quotation view event (POST-as-read; still write-gated)",
            ),
        ),
    ),
    HaloResource(
        "releases",
        "/Release",
        aliases=("release",),
        table_fields=("id", "name", "status_name"),
    ),
    HaloResource(
        "reports",
        "/Report",
        aliases=("report",),
        table_fields=("id", "name", "type"),
        operations=(
            # Both verified live against the tenant while building issue #15:
            # run executes via GET /Report/{id}?loadreport=true; clone POSTs an
            # array copy. See tests/test_reports_commands.py.
            ResourceOperation(
                name="run",
                method="GET",
                path="/Report/{id}",
                args=("id",),
                summary="Execute the report; rows under report.rows (Halo caps at 50,000)",
                verification="live",
                handler="report_run",
            ),
            ResourceOperation(
                name="clone",
                method="POST",
                path="/Report",
                summary="Copy a report to a new id, stripping identity fields",
                verification="live",
                handler="report_clone",
            ),
        ),
    ),
    HaloResource("webhooks", "/Webhook", aliases=("webhook",), table_fields=("id", "name", "url")),
    HaloResource("workdays", "/Workday", aliases=("workday",), table_fields=("id", "name")),
    HaloResource(
        "software-licences",
        "/SoftwareLicence",
        aliases=("software-licence", "software-licenses", "software-license"),
        table_fields=("id", "name", "client_name"),
    ),
    HaloResource(
        "crm-notes",
        "/CRMNote",
        aliases=("crm-note",),
        # Live-verified 2026-09-30: envelope {"actions": [...], "record_count": N};
        # GET /CRMNote/14630 -> 200. Unfiltered list returns 0 records (a scope
        # filter such as toplevel_id is required); `count` is honored (default
        # 50) but page_no/pageinate/page_size are IGNORED by Halo - the generic
        # list loop therefore duplicates rows whenever record_count exceeds the
        # per-page count (workaround: --param count=<record_count> for a
        # one-page fetch; tracked separately as a list-machinery issue).
        # table_fields corrected: live rows carry `datetime`, never `date`.
        # POST/DELETE are never fired at the tenant (no write authorization)
        # -> writes stay verification: spec.
        table_fields=("id", "client_id", "datetime", "who_agentid", "note"),
        list_key="actions",
        create_endpoint="/CRMNote",
        update_endpoint="/CRMNote",
        # The spec declares NO required fields (AreaNote schema has no
        # `required`). Only the content is enforced: the anchor is polymorphic
        # (client_id, supplier_id, quote_id, invoice_id, ...), so requiring
        # client_id would wrongly block supplier/quote notes - same semantic-
        # narrowing concern documented on `contracts`.
        required_create_fields=("note",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "client_id", "supplier_id", "datetime", "who", "note"),
    ),
    HaloResource("top-levels", "/TopLevel", aliases=("top-level",), table_fields=("id", "name")),
    HaloResource(
        "expenses",
        "/Expense",
        aliases=("expense",),
        table_fields=("id", "agent_name", "date", "value"),
        # No item route: Halo has no GET /Expense/{id} (probed 2026-09-29 — /Expense/1
        # and /Expense/0 both 404 while /Expense answers 403, and the official spec
        # documents only the collection). A `get` subcommand could only 404.
        supports_get=False,
    ),
    HaloResource(
        "timesheets",
        "/Timesheet",
        aliases=("timesheet",),
        table_fields=("id", "agent_name", "date", "hours"),
    ),
    HaloResource(
        "attachments",
        "/Attachment",
        aliases=("attachment",),
        table_fields=("id", "filename", "ticket_id"),
        # Nested routes verified against the live tenant (2026-09-28): a malformed
        # id answers 400 (route matched, id failed validation) while the known-missing
        # control also answers 400 via the {id} template — literal routes (e.g.
        # /Attachment/image) answer 404 when no record matches. The tenant has zero
        # attachments, so {id} reads are route-verified but data-unverified.
        operations=(
            ResourceOperation(
                "get-document",
                "GET",
                "/Attachment/document/{id}",
                args=("id",),
                summary="Fetch a document attachment by id",
                verification="route-verified",
            ),
            ResourceOperation(
                "delete-document",
                "DELETE",
                "/Attachment/document/{id}",
                args=("id",),
                summary="Delete a document attachment by id",
            ),
            ResourceOperation(
                "list-images",
                "GET",
                "/Attachment/image",
                summary="List image attachments; 404 when the tenant has none",
                verification="live:404",
            ),
            ResourceOperation(
                "upload-image",
                "POST",
                "/Attachment/image",
                multipart=True,
                summary="Upload an image (multipart form field 'file' per spec)",
            ),
            ResourceOperation(
                "get-image",
                "GET",
                "/Attachment/image/{id}",
                args=("id",),
                summary="Fetch an image attachment by id",
                verification="route-verified",
            ),
            ResourceOperation(
                "delete-image",
                "DELETE",
                "/Attachment/image/{id}",
                args=("id",),
                summary="Delete an image attachment by id",
            ),
            ResourceOperation(
                "get-nhserver",
                "GET",
                "/Attachment/nhserver/{id}",
                args=("id",),
                summary="Fetch an nhserver attachment by id",
                verification="route-verified",
            ),
            ResourceOperation(
                "presign-url",
                "POST",
                "/Attachment/GetS3PresignedURL",
                body=True,
                summary="Request an S3 presigned upload URL",
            ),
            ResourceOperation(
                "presign-complete",
                "POST",
                "/Attachment/PresignedURLUploadComplete",
                body=True,
                summary="Mark a presigned upload as complete",
            ),
            ResourceOperation(
                "create-document",
                "POST",
                "/Attachment/document",
                body=True,
                summary="Create a document attachment from a JSON body",
            ),
        ),
    ),
)

RESOURCE_BY_NAME = {resource.name: resource for resource in RESOURCES}
RESOURCE_BY_COMMAND = {
    command_name: resource
    for resource in RESOURCES
    for command_name in resource.command_names
}


def get_resource(name: str) -> HaloResource:
    try:
        return RESOURCE_BY_COMMAND[name]
    except KeyError as exc:
        raise KeyError(f"Unknown Halo resource: {name}") from exc
