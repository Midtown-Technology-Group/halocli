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
        operations=(
            ResourceOperation(
                name="me",
                method="GET",
                path="/Agent/me",
                summary="The acting agent's identity claims (JWT claims envelope; 108 entries live)",
                verification="live",
            ),
            ResourceOperation(
                name="clear-cache",
                method="POST",
                path="/Agent/ClearCache",
                summary="Clear the agent's cached data (config action)",
                verification="spec",
            ),
        ),
    ),
    HaloResource(
        "teams", "/Team", aliases=("team",), table_fields=("id", "name"),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/Team",
        update_endpoint="/Team",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "department_id", "inactive"),
        operations=(
            ResourceOperation(
                name="tree",
                method="GET",
                path="/Team/Tree",
                summary="Team tree with department nesting (bare array; 68 rows live)",
                verification="live",
            ),
        ),
    ),
    HaloResource(
        "users",
        "/Users",
        aliases=("user",),
        # Live-verified 2026-10-01 (test user 4266, client 625, profile
        # thomas; full evidence trail in issue #28): creation succeeds with
        # firstname/surname/name/emailaddress/client_id/site_id - Halo
        # validates one error at a time ("Username must be entered" -> "A Site
        # must be selected"). `new_password` follows the tenant policy (>=16
        # chars, <=2 identical in a row, lower+upper+number+special) and sets
        # cleanly via update. Flagship use: POST with
        # {"id": N, "_revoke_authenticatorapp": true} resets end-user MFA - it
        # returns 200 even for unenrolled users (write-only flag, not echoed).
        # MFA state is NOT readable on this scope (authenticatorapp_configured
        # never returned, 0/9 users sampled) and there is no admin-side
        # enable: twofactor_enabled/authenticatorapp_configured are silently
        # ignored, and the portal is Entra SSO-fronted.
        table_fields=("id", "name", "client_name", "emailaddress"),
        create_endpoint="/Users",
        update_endpoint="/Users",
        # The spec declares ZERO required fields; this is the live-proven
        # create set. Halo's two hard errors named `name` and `site_id`
        # explicitly; firstname/surname/client_id/emailaddress were in the
        # payload that worked and stay required until a minimal create proves
        # otherwise.
        required_create_fields=(
            "firstname",
            "surname",
            "name",
            "emailaddress",
            "client_id",
            "site_id",
        ),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=(
            "id",
            "name",
            "firstname",
            "surname",
            "emailaddress",
            "client_id",
            "site_id",
        ),
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
        operations=(
            ResourceOperation(
                name="view",
                method="POST",
                path="/KBArticle/View",
                body=True,
                summary="Render an article view (POST-as-read)",
                verification="spec",
            ),
            ResourceOperation(
                name="vote",
                method="POST",
                path="/KBArticle/vote",
                body=True,
                summary="Cast a vote on a knowledge article",
                verification="spec",
            ),
        ),
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
        operations=(
            ResourceOperation(
                name="stock-bins",
                method="GET",
                path="/Site/StockBins",
                summary="Stock bin rows across the tenant (bare array; 14 rows live)",
                verification="live",
            ),
        ),
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
        operations=(
            ResourceOperation(
                name="all-software-versions",
                method="GET",
                path="/Asset/GetAllSoftwareVersions",
                summary="Every software version row (bare array; 18,261 rows live - bounded by --max-records)",
                verification="live",
            ),
            ResourceOperation(
                name="next-tag",
                method="GET",
                path="/Asset/NextTag",
                summary="Next asset tag value ({last_tag, next_tag} object; live)",
                verification="live",
            ),
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
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/Category",
        update_endpoint="/Category",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name"),
    ),
    HaloResource(
        "ticket-types",
        "/TicketType",
        aliases=("ticket-type", "tickettypes"),
        table_fields=("id", "name", "guid"),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/TicketType",
        update_endpoint="/TicketType",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name"),
    ),
    HaloResource(
        "slas", "/SLA", aliases=("sla",), table_fields=("id", "name"),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/SLA",
        update_endpoint="/SLA",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "responsereset", "workday_id"),
    ),
    HaloResource(
        "appointments",
        "/Appointment",
        aliases=("appointment",),
        table_fields=("id", "subject", "agent_id", "start_date", "end_date"),
        # POST /Appointment creates or updates by id; DELETE /Appointment/{id} exists.
        # Assumption: subject + start_date + end_date are required; agent_id defaults to
        # the authenticated agent when omitted.
        #
        # Completion workflow (recipe proven in the workspace's
        # shared/halopsa/tools/timeentry.py::complete_appointment and exercised
        # live 2026-10-01): upserting an EXISTING appointment with
        # complete_status="0", complete_date, complete_timetaken (hours),
        # complete_notehtml and complete_agent_id marks it done on dispatch AND
        # logs the actual time (which may differ from the scheduled window -
        # that is the intended use: "a 30-min meeting that ran 2 hours").
        # Echo fields must ride along: agents=[{id,name,use}], user_id/ticket_id
        # (-1 when absent), note_html (invite HTML), followup_* mirrors,
        # agent_status, chargerate ("0" = no charge), utcoffset (240 = US/Eastern
        # minutes), apfaultidremoved (true when ticket_id is None/-1).
        # write_preview_fields covers that whole recipe so the preview shows it
        # and no pass-through warnings fire for it.
        create_endpoint="/Appointment",
        update_endpoint="/Appointment",
        required_create_fields=("subject", "start_date", "end_date"),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=(
            "id",
            "subject",
            "agent_id",
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
        ),
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
    HaloResource(
        "suppliers", "/Supplier", aliases=("supplier",), table_fields=("id", "name"),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/Supplier",
        update_endpoint="/Supplier",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "email_address", "phone_number"),
    ),
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
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/Release",
        update_endpoint="/Release",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name"),
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
            ResourceOperation(
                name="bookmark",
                method="POST",
                path="/Report/Bookmark",
                body=True,
                summary="Bookmark a report for the agent",
                verification="spec",
            ),
            ResourceOperation(
                name="create-pdf",
                method="POST",
                path="/Report/createpdf",
                body=True,
                summary="Generate a PDF rendering of a report (binary response)",
                verification="spec",
            ),
            ResourceOperation(
                name="print",
                method="POST",
                path="/Report/print",
                body=True,
                summary="Render a report for printing",
                verification="spec",
            ),
        ),
    ),
    HaloResource(
        "timesheet-events",
        "/TimesheetEvent",
        aliases=("timesheet-event", "time-events"),
        # Live-verified 2026-10-01: response is a BARE array and count/page
        # params are IGNORED - each list call dumps everything (~1,567 rows,
        # ~1.1 MB; the paging machinery cannot bound it, issue #24), so pass
        # --param agent_id=N or ISO start_date/end_date to bound the fetch
        # (agent_id filter works; ISO datetime ranges work; date-only values
        # silently mis-filter).
        #
        # id semantics (all three write branches probed live, authorized):
        # - the API view exposes id=0 on EVERY row - the real TSEeventid is
        #   never surfaced, so get/update/delete cannot be driven from list
        #   output (route probes: malformed id -> 400, missing -> 404)
        # - create with no id -> 515 "Cannot insert NULL into TSEeventid";
        #   id=0 -> same 515; nonzero unknown id -> "Record not found"
        #   (POST-with-id is a strict update; the INSERT path never
        #   auto-assigns). Minimal creates are dead end-to-end.
        # - 1,557 of 1,567 rows carry action_number+ticket_id: ticket time is
        #   derived from POST /Actions, not created here.
        #
        # The WORKSPACE-PROVEN create recipe is QuickTime
        # (bifrost-workspace shared/halopsa/tools/timeentry.py::log_quicktime,
        # a production tool): {start_date, end_date, subject, note,
        # ticket_id: null, tickettype_id: null, lognewticket, client_id,
        # agent_id as STRING, agents: [{id, name}], event_type (0=work,
        # 1=break with break_type/break_note), charge_rate} - Halo then
        # auto-creates AND closes a lightweight ticket per entry (that is why
        # tickets titled "Quick Time - <agent> - <datetime>" exist). NOT fired
        # from halocli this session -> verification stays spec.
        # GET /TimesheetEvent/mine answers 403 for this agent (live:403).
        table_fields=("start_date", "end_date", "agent_id", "ticket_id", "timetaken", "subject"),
        create_endpoint="/TimesheetEvent",
        update_endpoint="/TimesheetEvent",
        # Spec declares NO required fields. Required = the QuickTime recipe's
        # always-present core: Halo derives duration from start/end (their
        # production payload sends no timetaken at all), so requiring
        # timetaken would block the proven workflow.
        required_create_fields=("subject", "start_date", "end_date"),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=(
            "id",
            "subject",
            "start_date",
            "end_date",
            "timetaken",
            "note",
            "event_type",
            "break_type",
            "break_note",
            "lognewticket",
            "charge_rate",
            "agent_id",
            "agents",
            "ticket_id",
            "client_id",
        ),
        operations=(
            ResourceOperation(
                name="mine",
                method="GET",
                path="/TimesheetEvent/mine",
                summary="The signed-in agent's own timesheet events",
                verification="live:403",
            ),
            ResourceOperation(
                name="forecasting",
                method="GET",
                path="/Timesheet/forecasting",
                summary="Forecast rows for timesheet planning (bare array; 396 rows live)",
                verification="live",
            ),
        ),
    ),
    HaloResource(
        "canned-text",
        "/CannedText",
        aliases=("canned-texts", "cannedtext", "canned"),
        # Live-verified 2026-10-01: GET /CannedText -> 200, BARE array (5 rows;
        # list_key therefore stays None - the default bare-array path handles
        # it); GET /CannedText/1 -> 200 (15 keys, _canupdate true, text/html
        # present). The spec's list response declares no content schema even
        # though the tenant returns JSON. POST/DELETE/favourite were never
        # fired (no write authorization) -> verification: spec.
        table_fields=("id", "name", "group_id", "restriction_type"),
        create_endpoint="/CannedText",
        update_endpoint="/CannedText",
        # The spec declares no required fields; name + text is the usable
        # minimum (live rows always carry both - a canned text without text
        # would be noise).
        required_create_fields=("name", "text"),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "group_id", "text", "html"),
        operations=(
            ResourceOperation(
                name="favourite",
                method="POST",
                path="/CannedText/favourite",
                body=True,
                summary="Mark a canned text as favourite (body: id, ctid, unum)",
            ),
        ),
    ),
    HaloResource(
        "searches",
        "/Search",
        # /Search/{id} does not exist in the spec (same class as expenses), so
        # supports_get stays False. The registry entry buys coverage and a
        # list command; the shaped UX is the top-level `halocli search <term>`
        # command - `searches list` sends GET /Search without a term and Halo
        # answers an empty array. Read-only: /Search is GET-only in the spec.
        supports_get=False,
        table_fields=("use", "id", "name", "summary", "client_name"),
    ),
    # --------------------------------------------------------------------------
    # Phase 2 batch 1 (2026-10-02): promoted from backlog with production GET
    # sweep evidence (sweep_results.json) - every table_field below was observed
    # on a live list row, list_key matches the observed envelope, and the
    # GET/{id} route answered validation-400 (route-verified, no record touched).
    # Reads-first: POST/DELETE stay backlog until a write batch promotes them.
    # --------------------------------------------------------------------------
    HaloResource(
        "outgoing",
        "/Outgoing",
        # Sweep 2026-10-02: envelope "outgoing", 702 rows, GET/{id} -> 400.
        table_fields=("id", "timestamp", "status", "type", "subject", "mailboxid"),
        list_key="outgoing",
    ),
    HaloResource(
        "outgoing-attempts",
        "/OutgoingAttempt",
        # Sweep 2026-10-02: envelope "attempts", 7,047 rows (delivery attempts
        # - mail-flow troubleshooting surface), GET/{id} -> 400.
        table_fields=("id", "attemptdate", "status", "errorsubcode"),
        list_key="attempts",
    ),
    HaloResource(
        "email-templates",
        "/EmailTemplate",
        # Sweep 2026-10-02: bare array, 94 rows, GET/{id} -> 400.
        table_fields=("id", "name", "description", "template_group", "sectionid"),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/EmailTemplate",
        update_endpoint="/EmailTemplate",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "description", "template_group"),
        operations=(
            ResourceOperation(
                name="preview",
                method="POST",
                path="/EmailTemplate/preview",
                body=True,
                summary="Render an email template preview without sending",
                verification="spec",
            ),
        ),
    ),
    HaloResource(
        "tags",
        "/Tags",
        aliases=("tag",),
        # Sweep 2026-10-02: bare array, 103 rows, GET/{id} -> 400.
        table_fields=("id", "text", "type"),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/Tags",
        update_endpoint="/Tags",
        required_create_fields=("text",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "text", "type"),
    ),
    HaloResource(
        "popup-notes",
        "/PopupNote",
        # Sweep 2026-10-02: bare array, 164 rows. No GET /PopupNote/{id} in
        # the spec at all -> supports_get False (searches/expenses class).
        supports_get=False,
        table_fields=("id", "client_id", "date_created", "read_status", "note"),
    ),
    HaloResource(
        "lookups",
        "/Lookup",
        # Sweep 2026-10-02: bare array, 1,122 rows, GET/{id} -> 400. The
        # value columns are instance-shaped (value4/value6 present live) -
        # columns kept exactly to what list rows returned.
        table_fields=("id", "lookupid", "name", "value4", "value5", "value6"),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/Lookup",
        update_endpoint="/Lookup",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "lookupid", "value4"),
        operations=(
            ResourceOperation(
                name="clear-cache",
                method="POST",
                path="/Lookup/ClearCache",
                summary="Clear lookup value caches",
                verification="spec",
            ),
        ),
    ),
    HaloResource(
        "outcomes",
        "/Outcome",
        # Sweep 2026-10-02: bare array, 91 rows (TOutcome - ticket action
        # outcomes, 285p schema), GET/{id} -> 400.
        table_fields=("id", "buttonname", "colour", "chargerate", "actiongroup"),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/Outcome",
        update_endpoint="/Outcome",
        required_create_fields=("buttonname",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "buttonname", "colour", "chargerate"),
    ),
    HaloResource(
        "call-log",
        "/CallLog",
        # Sweep 2026-10-02: envelope "calllog", rows present (thin tenant),
        # GET/{id} -> 400.
        table_fields=("id", "start_date", "callerid", "call_status", "client_name", "agent_id"),
        list_key="calllog",
        # Writes: spec-verified POST (POST-only: the spec offers no DELETE /{id}) - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/CallLog",
        update_endpoint="/CallLog",
        required_create_fields=("summary",),
        required_update_fields=("id",),
        write_preview_fields=("id", "summary", "caller_primary_number", "client_id", "start_date"),
    ),
    HaloResource(
        "mailboxes",
        "/Mailbox",
        # Sweep 2026-10-02: bare array, 124p mailbox config rows, GET/{id} -> 400.
        table_fields=("id", "name", "display_address", "smtpaddress", "inbound_method", "enabled"),
    ),
    HaloResource(
        "charge-rates",
        "/ChargeRate",
        # Sweep 2026-10-02: bare array, 12 rows (billing charge-rate config),
        # GET/{id} -> 400. Read-only: billing config, writes stay backlog.
        table_fields=("id", "rate", "current_rate", "startdate", "area", "org"),
    ),
    # ------------------------------------------------------------------
    # Phase 2 batch 2 (2026-10-02): sweep-evidenced reads-first promotions.
    # Every table_field observed on a live row (sweep_results.json);
    # list_key matches the observed envelope; writes stay backlog.
    # ------------------------------------------------------------------
    HaloResource(
        "address",
        "/Address",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'type', 'date_active', 'inactive', 'line1', 'line2'),
    ),
    HaloResource(
        "agent-check-ins",
        "/AgentCheckIn",
        # Sweep 2026-10-02: envelope '<bare array>', 149 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'status', 'timestamp', 'agent_id'),
        # Writes: spec-verified POST (POST-only: the spec offers no DELETE /{id}) - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/AgentCheckIn",
        update_endpoint="/AgentCheckIn",
        required_create_fields=("agent_id",),
        required_update_fields=("id",),
        write_preview_fields=("id", "agent_id", "status", "timestamp"),
    ),
    HaloResource(
        "approval-process",
        "/ApprovalProcess",
        # Sweep 2026-10-02: envelope '<bare array>', 5 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'type', 'guid', 'hide_auto_approvals', 'no_status_change'),
    ),
    HaloResource(
        "approval-process-rules",
        "/ApprovalProcessRule",
        # Sweep 2026-10-02: envelope '<bare array>', 3 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'type', 'agent_name', 'emailaddress', 'accept_emailtemplate_id'),
    ),
    HaloResource(
        "asset-groups",
        "/AssetGroup",
        # Sweep 2026-10-02: envelope '<bare array>', 13 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'autogroupnewquotelines', 'connector', 'default_quantity_decimal_places', 'defaultsite'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/AssetGroup",
        update_endpoint="/AssetGroup",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name"),
    ),
    HaloResource(
        "asset-types",
        "/AssetType",
        # Sweep 2026-10-02: envelope '<bare array>', 34 rows, GET/{id} -> 400 (route-verified); name recovered via uncapped re-probe
        table_fields=('id', 'name'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/AssetType",
        update_endpoint="/AssetType",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name"),
    ),
    HaloResource(
        "automations",
        "/Automation",
        # Sweep 2026-10-02: envelope 'automations', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'status', 'date_processed', 'executionStartTime', 'last_attempt_date', 'next_retry_date'),
        list_key="automations",
    ),
    HaloResource(
        "billing-templates",
        "/BillingTemplate",
        # Sweep 2026-10-02: envelope '<bare array>', 4 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'active', 'autotopupbyamount', 'autotopupcostperhour', 'autotopupthreshhold'),
    ),
    HaloResource(
        "booking-types",
        "/BookingType",
        # Sweep 2026-10-02: envelope '<bare array>', 2 rows. No GET /BookingType/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'name', 'agentbooking_max_days_advance', 'agentbooking_min_hours_advance', 'appointment_type', 'assettype_id'),
    ),
    HaloResource(
        "budget-types",
        "/BudgetType",
        # Sweep 2026-10-02: envelope '<bare array>', 6 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'defaultrate'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/BudgetType",
        update_endpoint="/BudgetType",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "defaultrate"),
    ),
    HaloResource(
        "cabs",
        "/CAB",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'all_must_approve', 'approvals_needed', 'guid', 'rejection_threshold'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/CAB",
        update_endpoint="/CAB",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "all_must_approve", "approvals_needed"),
    ),
    HaloResource(
        "call-scripts",
        "/CallScript",
        # Sweep 2026-10-02: envelope '<bare array>', 2 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'note', 'category_1'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/CallScript",
        update_endpoint="/CallScript",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "note", "category_1"),
    ),
    HaloResource(
        "client-prepays",
        "/ClientPrepay",
        # Sweep 2026-10-02: envelope 'prepay', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'description', 'date', 'expirydate', 'invoicedate', 'client_id'),
        list_key="prepay",
    ),
    HaloResource(
        "consignments",
        "/Consignment",
        # Sweep 2026-10-02: envelope 'consignments', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'date', 'note', 'address', 'is_return', 'salesorder_id'),
        list_key="consignments",
    ),
    HaloResource(
        "cost-centres",
        "/CostCentres",
        # Sweep 2026-10-02: envelope '<bare array>', 3 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'addr1', 'addr2', 'addr3', 'addr4'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/CostCentres",
        update_endpoint="/CostCentres",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name"),
    ),
    HaloResource(
        "currencies",
        "/Currency",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'last_updated', 'code', 'conversion_rate', 'symbol'),
    ),
    HaloResource(
        "custom-buttons",
        "/CustomButton",
        # Sweep 2026-10-02: envelope '<bare array>', 2 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'type', 'access_control', 'access_control_level', 'button_visibility_ac'),
    ),
    HaloResource(
        "custom-queries",
        "/CustomQuery",
        # Sweep 2026-10-02: envelope '<bare array>', 2 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'top_max'),
    ),
    HaloResource(
        "custom-tables",
        "/CustomTable",
        # Sweep 2026-10-02: envelope '<bare array>', 31 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'clear_on_close', 'columns', 'customextratableid', 'customtable_orderby'),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/CustomTable",
        update_endpoint="/CustomTable",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "table_type", "data_entry_type"),
    ),
    HaloResource(
        "dashboard-links",
        "/DashboardLinks",
        # Sweep 2026-10-02: envelope 'dashboards', 26 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'allow_anonymous', 'dashboard_theme_override', 'display_type', 'guid'),
        list_key="dashboards",
    ),
    HaloResource(
        "database-lookups",
        "/DatabaseLookup",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'active', 'agent_id', 'client_id', 'allowmultipleresults'),
    ),
    HaloResource(
        "distribution-lists",
        "/DistributionLists",
        # Sweep 2026-10-02: envelope 'distributionlists', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'desc', 'dynamic_members', 'email_field', 'entity'),
        list_key="distributionlists",
    ),
    HaloResource(
        "email-address-books",
        "/EmailAddressBook",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows. No GET /EmailAddressBook/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'name', 'customer_id', 'display', 'email_address'),
    ),
    HaloResource(
        "email-rules",
        "/EmailRule",
        # Sweep 2026-10-02: envelope '<bare array>', 8 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'enabled', '2useendofline', '3useendofline', '3useticketuser'),
    ),
    HaloResource(
        "email-stores",
        "/EmailStore",
        # Sweep 2026-10-02: envelope '<bare array>', 80 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'datecreated', 'dateemailed', 'client_id', 'mailbox_id', 'ref'),
    ),
    HaloResource(
        "events",
        "/Event",
        # Sweep 2026-10-02: envelope 'events', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'description', 'status', 'type', 'last_attempt_date', 'next_retry_date'),
        list_key="events",
    ),
    HaloResource(
        "event-rules",
        "/EventRule",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'active', 'algorithm', 'alwayscreatenew', 'assetmatchingtype'),
    ),
    HaloResource(
        "faq-lists",
        "/FAQLists",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'description', 'name', 'group_id', 'type', 'allow_indexing'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/FAQLists",
        update_endpoint="/FAQLists",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "description", "group_id"),
    ),
    HaloResource(
        "feeds",
        "/Feed",
        # Sweep 2026-10-02: envelope 'feed', 1 rows. No GET /Feed/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'datetime', 'agent_id', 'note', 'content_id1', 'content_id2'),
        list_key="feed",
    ),
    HaloResource(
        "feedbacks",
        "/Feedback",
        # Sweep 2026-10-02: envelope '<bare array>', 35086 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'date', 'comment', 'feedback_faultid', 'score', 'score_band'),
    ),
    HaloResource(
        "fields",
        "/Field",
        # Sweep 2026-10-02: envelope '<bare array>', 32 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'inactiveupdatetype', 'moveupdatedefault', 'moveupdatetype', 'validate'),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/Field",
        update_endpoint="/Field",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "kind", "mandatory"),
    ),
    HaloResource(
        "field-groups",
        "/FieldGroup",
        # Sweep 2026-10-02: envelope '<bare array>', 38 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'restrictupdate', 'group_visibility_conditions', 'guid', 'header'),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/FieldGroup",
        update_endpoint="/FieldGroup",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "header", "restrictread"),
    ),
    HaloResource(
        "field-infos",
        "/FieldInfo",
        # Sweep 2026-10-02: envelope '<bare array>', 430 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'copytochildonupdate', 'defaultdate', 'addunknown', 'calculation'),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/FieldInfo",
        update_endpoint="/FieldInfo",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "inputtype", "hint"),
    ),
    HaloResource(
        "holidays",
        "/Holiday",
        # Sweep 2026-10-02: envelope '<bare array>', 492 rows; GET/{id} -> 500
        # (route alive - the probe token crashed the server handler), so the
        # detail route is kept but is known-broken for odd ids.
        table_fields=('id', 'name', 'date', 'end_date', 'agent_id', 'agent_name'),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/Holiday",
        update_endpoint="/Holiday",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "date", "duration"),
    ),
    HaloResource(
        "incoming-webhook-attempts",
        "/IncomingWebhookAttempt",
        # Sweep 2026-10-02: envelope 'attempts', 105 rows. No GET /IncomingWebhookAttempt/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'status', 'attemptdate', 'errormessage', 'relatedid'),
        list_key="attempts",
    ),
    HaloResource(
        "invoice-changes",
        "/InvoiceChange",
        # Sweep 2026-10-02: envelope 'changehistory', 1 rows. No GET /InvoiceChange/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'datetime', 'field_id', 'field_name', 'invoice_detail_prorata_id', 'invoice_detail_quantity_id'),
        list_key="changehistory",
    ),
    HaloResource(
        "item-groups",
        "/ItemGroup",
        # Sweep 2026-10-02: envelope '<bare array>', 10 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'category', 'type', 'add_all_group_items_quote', 'allow_users'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/ItemGroup",
        update_endpoint="/ItemGroup",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "category", "type"),
    ),
    HaloResource(
        "item-stocks",
        "/ItemStock",
        # Sweep 2026-10-02: envelope 'itemstock', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'date', 'cost', 'item_assettype_id', 'item_id', 'item_name'),
        list_key="itemstock",
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/ItemStock",
        update_endpoint="/ItemStock",
        required_create_fields=("item_id",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "item_id", "item_name", "cost"),
    ),
    HaloResource(
        "item-stock-histories",
        "/ItemStockHistory",
        # Sweep 2026-10-02: envelope 'changehistory', 1 rows, GET/{id} -> 404 (route-verified)
        table_fields=('id', 'date', 'note', 'asset_id', 'consignment_id', 'item_id'),
        list_key="changehistory",
    ),
    HaloResource(
        "journeys",
        "/Journey",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'description', 'type', 'startdate', 'actionnumber', 'faultid'),
    ),
    HaloResource(
        "licence-changes",
        "/LicenceChange",
        # Sweep 2026-10-02: envelope 'changes', 1 rows. No GET /LicenceChange/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'datetime', 'field_id', 'field_name', 'licence_id', 'new_value'),
        list_key="changes",
    ),
    HaloResource(
        "notifications",
        "/Notification",
        # Sweep 2026-10-02: envelope '<bare array>', 12 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'agent_id', 'agent_name', 'acknowledge_type', 'colour'),
    ),
    HaloResource(
        "notification-messages",
        "/NotificationMessage",
        # Sweep 2026-10-02: envelope '<bare array>', 106 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'guid'),
    ),
    HaloResource(
        "organisations",
        "/Organisation",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'email', 'address', 'all_user_faqlists_allowed', 'bank_details_line_1'),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/Organisation",
        update_endpoint="/Organisation",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "address", "phone"),
    ),
    HaloResource(
        "pdf-templates",
        "/PdfTemplate",
        # Sweep 2026-10-02: envelope '<bare array>', 21 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'type', 'colour', 'colour_type', 'config_source_type'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/PdfTemplate",
        update_endpoint="/PdfTemplate",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "type", "colour"),
    ),
    HaloResource(
        "products",
        "/Product",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'devops_details_id', 'release_count', 'third_party_id', 'third_party_name'),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/Product",
        update_endpoint="/Product",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "third_party_name", "release_count"),
    ),
    HaloResource(
        "purchase-orders",
        "/PurchaseOrder",
        # Sweep 2026-10-02: envelope 'purchaseorders', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'approvaldatetime', 'date', 'date_published', 'est_delivery_date', 'client_name'),
        list_key="purchaseorders",
    ),
    HaloResource(
        "qualifications",
        "/Qualification",
        # Sweep 2026-10-02: envelope '<bare array>', 2 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'criteria', 'guid', 'mustmatch', 'weight'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/Qualification",
        update_endpoint="/Qualification",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "criteria", "weight"),
    ),
    HaloResource(
        "release-types",
        "/ReleaseType",
        # Sweep 2026-10-02: envelope '<bare array>', 4 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'releasenoteset'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/ReleaseType",
        update_endpoint="/ReleaseType",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "releasenoteset"),
    ),
    HaloResource(
        "roles",
        "/Roles",
        # Sweep 2026-10-02: envelope '<bare array>', 11 rows, GET/{id} -> 404 (route-verified)
        table_fields=('id', 'name', 'id_int', 'notes'),
    ),
    HaloResource(
        "sales-mailboxes",
        "/SalesMailbox",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'type', '_exchangecodefortoken', 'applicationid', 'authorized'),
    ),
    HaloResource(
        "sales-mailbox-details",
        "/SalesMailboxDetail",
        # Sweep 2026-10-02: envelope '<bare array>', 8 rows. No GET /SalesMailboxDetail/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'name', 'enableautomatching', 'google_authorized', 'lasterror', 'lastsync'),
    ),
    HaloResource(
        "sales-orders",
        "/SalesOrder",
        # Sweep 2026-10-02: envelope 'salesorders', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'date', 'datereceived', 'client_id', 'client_name', 'accountsref'),
        list_key="salesorders",
    ),
    HaloResource(
        "schedules",
        "/Schedule",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'subject', 'type', 'enddate', 'monthlyrecurrencespecificdateinterval', 'startdate'),
    ),
    HaloResource(
        "schedule-occurrences",
        "/ScheduleOccurrence",
        # Sweep 2026-10-02: envelope 'occurrences', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'description', 'status', 'creation_date', 'date_processed', 'last_attempt_date'),
        list_key="occurrences",
    ),
    HaloResource(
        "services",
        "/Service",
        # Sweep 2026-10-02: envelope 'services', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'asset_count', 'assettype_id', 'business_owner_cab_id', 'business_owner_id'),
        list_key="services",
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/Service",
        update_endpoint="/Service",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "asset_count", "business_owner_id"),
    ),
    HaloResource(
        "service-categories",
        "/ServiceCategory",
        # Sweep 2026-10-02: envelope '<bare array>', 6 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'summary', 'guid', 'icon', 'important'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/ServiceCategory",
        update_endpoint="/ServiceCategory",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "summary", "important"),
    ),
    HaloResource(
        "service-request-details",
        "/ServiceRequestDetails",
        # Sweep 2026-10-02: envelope 'data', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'allow_all_items', 'allow_item_bundles', 'allowed_item_bundles', 'allowed_item_groups', 'allowed_items'),
        list_key="data",
    ),
    HaloResource(
        "service-restrictions",
        "/ServiceRestriction",
        # Sweep 2026-10-02: envelope '<bare array>', 11 rows. No GET /ServiceRestriction/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'type', 'allow_access', 'data_id', 'data_name', 'service_category_id'),
    ),
    HaloResource(
        "stock-bins",
        "/StockBin",
        # Sweep 2026-10-02: envelope '<bare array>', 14 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'dont_add_to_order', 'parent_id', 'parent_name', 'sequence'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/StockBin",
        update_endpoint="/StockBin",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "parent_id", "sequence"),
    ),
    HaloResource(
        "stock-traces",
        "/StockTrace",
        # Sweep 2026-10-02: envelope 'results', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'summary', 'agent_id', 'agent_name', 'action_id', 'item_id'),
        list_key="results",
    ),
    HaloResource(
        "taxes",
        "/Tax",
        # Sweep 2026-10-02: envelope '<bare array>', 18 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'value', 'code', 'is_composite', 'kashflow_tenant_id'),
    ),
    HaloResource(
        "templates",
        "/Template",
        # Sweep 2026-10-02: envelope 'stdrequests', 592 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'summary', 'group_id', 'type', 'end_date'),
        list_key="stdrequests",
    ),
    HaloResource(
        "ticket-approvals",
        "/TicketApproval",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'type', 'dateapproved', 'datetime', 'lastreminderdatetime', 'agent_id'),
    ),
    HaloResource(
        "ticket-areas",
        "/TicketArea",
        # Sweep 2026-10-02: envelope '<bare array>', 3 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'agents_with_no_tickets_display_type', 'allow_ticket_type_selection', 'area_use', 'default_columns_id'),
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/TicketArea",
        update_endpoint="/TicketArea",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "area_use", "default_columns_id"),
    ),
    HaloResource(
        "ticket-rules",
        "/TicketRules",
        # Sweep 2026-10-02: envelope '<bare array>', 21 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'active', 'group_id', 'batch_size', 'batch_sleep'),
    ),
    HaloResource(
        "ticket-type-fields",
        "/TicketTypeField",
        # Sweep 2026-10-02: envelope '<bare array>', 923 rows. No GET /TicketTypeField/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'copytochildonupdate', 'restrictupdate', 'copytochild', 'copytorelated', 'enduseraction'),
    ),
    HaloResource(
        "to-do-groups",
        "/ToDoGroup",
        # Sweep 2026-10-02: envelope 'data', 33 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'text', 'client_name'),
        list_key="data",
        # Writes: spec-verified POST + DELETE /{id}; never fired at the
        # tenant (verification: spec). Required set = the primary display
        # field - the spec declares no required fields anywhere (house
        # assumption, commented as such per the `agents` precedent).
        create_endpoint="/ToDoGroup",
        update_endpoint="/ToDoGroup",
        required_create_fields=("text",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "text", "client_name"),
    ),
    HaloResource(
        "user-changes",
        "/UserChange",
        # Sweep 2026-10-02: envelope 'changes', 1 rows. No GET /UserChange/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'datetime', 'customfield_id', 'field_id', 'field_name', 'new_client'),
        list_key="changes",
    ),
    HaloResource(
        "user-roles",
        "/UserRoles",
        # Sweep 2026-10-02: envelope '<bare array>', 5 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'allowviewclientdocs', 'allowviewsitedocs', 'canaccesscatalog', 'canaccessinvoices'),
    ),
    HaloResource(
        "view-columns",
        "/ViewColumns",
        # Sweep 2026-10-02: envelope '<bare array>', 26 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'type', 'agent_id', 'guid', 'team_id'),
    ),
    HaloResource(
        "view-filters",
        "/ViewFilter",
        # Sweep 2026-10-02: envelope '<bare array>', 27 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'type', 'agent_id', 'guid', 'sys_id'),
    ),
    HaloResource(
        "view-list-groups",
        "/ViewListGroup",
        # Sweep 2026-10-02: envelope '<bare array>', 7 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'type', 'collapsed', 'sequence'),
    ),
    HaloResource(
        "view-lists",
        "/ViewLists",
        # Sweep 2026-10-02: envelope '<bare array>', 58 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'group', 'type', 'agent_id', 'column_profile_id'),
    ),
    HaloResource(
        "workflows",
        "/Workflow",
        # Sweep 2026-10-02: envelope '<bare array>', 8 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'active', 'guid', 'notinuse'),
    ),
    HaloResource(
        "workflow-targets",
        "/WorkflowTarget",
        # Sweep 2026-10-02: envelope '<bare array>', 4 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'name', 'end_stage_id', 'end_stage_name', 'end_steps', 'flow_id'),
    ),
    HaloResource(
        "formattedemails",
        "/formattedemail",
        # Sweep 2026-10-02: envelope '<bare array>', 1 rows, GET/{id} -> 400 (route-verified)
        table_fields=('id', 'subject', 'dateopened', 'timeopened', 'customer', 'agent'),
    ),
    HaloResource(
        "workflowsteps",
        "/workflowstep",
        # Sweep 2026-10-02: envelope '<bare array>', 206 rows. No GET /workflowstep/{id} in spec -> supports_get False
        supports_get=False,
        table_fields=('id', 'name', 'actions', 'allow_all_statuses', 'allowed_statuses', 'auto_action'),
    ),
    HaloResource("webhooks", "/Webhook", aliases=("webhook",), table_fields=("id", "name", "url")),
    HaloResource(
        "workdays", "/Workday", aliases=("workday",), table_fields=("id", "name"),
        # Writes: spec-verified POST + DELETE /{id} (never fired at the
        # tenant - verification: spec). Required = the primary display
        # column observed on live rows (house assumption: the spec
        # declares no required fields anywhere).
        create_endpoint="/Workday",
        update_endpoint="/Workday",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "start", "end", "alldayssame"),
    ),
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
        # 50) but page_no/pageinate/page_size are IGNORED by Halo. Since 1.8.0
        # the generic list loop detects the repeated page, never appends it,
        # recovers the full set with one count=<record_count> fetch, and reports
        # `paging_ignored` in the payload (issue #24, live-verified: 133 of 133
        # distinct). --param count=<total> remains the manual workaround.
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
    HaloResource(
        "area-request-types",
        "/AreaRequestType",
        aliases=("area-request-type",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "audits",
        "/Audit",
        aliases=("audit",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 404 (route alive).
        table_fields=("id",),
        list_key="audit",
    ),
    HaloResource(
        "bulk-emails",
        "/BulkEmail",
        aliases=("bulk-email",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="results",
    ),
    HaloResource(
        "cab-members",
        "/CABMember",
        aliases=("cab-member",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        supports_get=False,
    ),
    HaloResource(
        "cab-roles",
        "/CABRole",
        aliases=("cab-role",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        supports_get=False,
    ),
    HaloResource(
        "crm-note-replies",
        "/CRMNoteReply",
        aliases=("crm-note-reply",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        # Writes: spec-verified POST + DELETE /{id} - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/CRMNoteReply",
        update_endpoint="/CRMNoteReply",
        required_create_fields=("note",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "note", "parent_id", "agent_id"),
    ),
    HaloResource(
        "csp-consumption-data",
        "/CSPConsumptionData",
        aliases=("csp-consumption",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="data",
    ),
    HaloResource(
        "csv-templates",
        "/CSVTemplate",
        aliases=("csv-template",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "call-events",
        "/CallEvent",
        aliases=("call-event",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="callevents",
    ),
    HaloResource(
        "certificates",
        "/Certificate",
        aliases=("certificate",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        # Writes: spec-verified POST + DELETE /{id} - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/Certificate",
        update_endpoint="/Certificate",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "issuer", "subject", "thumbprint"),
    ),
    HaloResource(
        "change-calendars",
        "/ChangeCalendar",
        aliases=("change-calendar",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        list_key="appointments",
        supports_get=False,
    ),
    HaloResource(
        "confirm-closures",
        "/ConfirmClosure",
        aliases=("confirm-closure",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "contact-groups",
        "/Contactgroup",
        aliases=("contact-group",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "contact-group-contacts",
        "/Contactgroupcontact",
        aliases=("contact-group-contact",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "contract-rules",
        "/ContractRule",
        aliases=("contract-rule",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "contract-schedules",
        "/ContractSchedule",
        aliases=("contract-schedule",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "contract-schedule-plans",
        "/ContractSchedulePlan",
        aliases=("contract-schedule-plan",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "device-licences",
        "/DeviceLicence",
        aliases=("device-licence",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        supports_get=False,
    ),
    HaloResource(
        "distribution-list-logs",
        "/DistributionListsLog",
        aliases=("distribution-list-log",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="distributionlistslog",
    ),
    HaloResource(
        "downtimes",
        "/Downtime",
        aliases=("downtime",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="downtime",
        operations=(
            ResourceOperation(
                name="downtime-calendar",
                method="GET",
                path="/Downtime/DowntimeCalendar",
                summary="Downtime calendar entries (route live 200; empty on this tenant)",
                verification="live",
            ),
        ),
    ),
    HaloResource(
        "email-template-variables",
        "/EmailTemplateVariable",
        aliases=("email-template-variable",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        # Writes: spec-verified POST + DELETE /{id} - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/EmailTemplateVariable",
        update_endpoint="/EmailTemplateVariable",
        required_create_fields=("variable",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "variable", "text", "emailtemplate_id"),
    ),
    HaloResource(
        "historical-ticket-volumes",
        "/HistoricalTicketVolumes",
        aliases=("historical-ticket-volume",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="historicalticketvolumes",
    ),
    HaloResource(
        "invoice-detail-prorata",
        "/InvoiceDetailProRata",
        aliases=("prorata-invoice-detail",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        list_key="prorata",
        supports_get=False,
    ),
    HaloResource(
        "mail-campaign-logs",
        "/MailCampaignLog",
        aliases=("mail-campaign-log",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="mailcampaignlog",
    ),
    HaloResource(
        "meter-readings",
        "/MeterReading",
        aliases=("meter-reading",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 12 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="readings",
    ),
    HaloResource(
        "escalation-messages",
        "/Notifications",
        aliases=("escalation-message",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 14 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="notifications",
    ),
    HaloResource(
        "powershell-scripts",
        "/PowerShellScript",
        aliases=("powershell-script",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 12 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "powershell-script-criteria",
        "/PowerShellScriptCriteria",
        aliases=("powershell-script-criterion",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 11 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "powershell-script-processing",
        "/PowerShellScriptProcessing",
        aliases=("powershell-script-queue",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 14 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="actions",
    ),
    HaloResource(
        "product-branches",
        "/ProductBranch",
        aliases=("product-branch",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 11 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        supports_get=False,
    ),
    HaloResource(
        "product-components",
        "/ProductComponent",
        aliases=("product-component",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 11 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        # Writes: spec-verified POST + DELETE /{id} - never fired at the
        # tenant (verification: spec). Required = primary field from the
        # POST schema (spec declares required: [] everywhere).
        create_endpoint="/ProductComponent",
        update_endpoint="/ProductComponent",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "active", "devops_details_id"),
    ),
    HaloResource(
        "publish-profiles",
        "/PublishProfiles",
        aliases=("publish-profile",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "recurring-items",
        "/RecurringItem",
        aliases=("recurring-item",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 12 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        supports_get=False,
    ),
    HaloResource(
        "release-note-groups",
        "/ReleaseNoteGroup",
        aliases=("release-note-group",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        # Writes: spec-verified POST + DELETE /{id} - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/ReleaseNoteGroup",
        update_endpoint="/ReleaseNoteGroup",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "releasenote"),
    ),
    HaloResource(
        "release-pipelines",
        "/ReleasePipeline",
        aliases=("release-pipeline",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="releasepipelines",
        # Writes: spec-verified POST + DELETE /{id} - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/ReleasePipeline",
        update_endpoint="/ReleasePipeline",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "active", "devops_pipeline_id"),
    ),
    HaloResource(
        "remote-sessions",
        "/RemoteSession",
        aliases=("remote-session",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 12 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="remotesessions",
    ),
    HaloResource(
        "report-repositories",
        "/ReportRepository",
        aliases=("report-repository",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 14 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        operations=(
            ResourceOperation(
                name="report-categories",
                method="GET",
                path="/ReportRepository/ReportCategories",
                summary="Report categories (route live 200; empty on this tenant)",
                verification="live",
            ),
        ),
    ),
    HaloResource(
        "resource-types",
        "/ResourceType",
        aliases=("resource-type",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 404 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "saved-forecasts",
        "/SavedForecast",
        aliases=("saved-forecast",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="savedforecasts",
    ),
    HaloResource(
        "service-availabilities",
        "/ServiceAvailability",
        aliases=("service-availability",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="serviceavailability",
    ),
    HaloResource(
        "service-statuses",
        "/ServiceStatus",
        aliases=("service-status",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 14 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 404 (route alive).
        table_fields=("id",),
        list_key="service_status",
    ),
    HaloResource(
        "single-sign-on-attempts",
        "/SingleSignOnAttempt",
        aliases=("single-sign-on-attempt",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="attempts",
    ),
    HaloResource(
        "software-licence-roles",
        "/SoftwareLicenceRole",
        aliases=("software-licence-role",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 11 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        supports_get=False,
    ),
    HaloResource(
        "supplier-contracts",
        "/SupplierContract",
        aliases=("supplier-contract",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 13 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="contracts",
    ),
    HaloResource(
        "tax-rules",
        "/TaxRule",
        aliases=("tax-rule",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "ticket-type-groups",
        "/TicketTypeGroup",
        aliases=("ticket-type-group",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        # Writes: spec-verified POST + DELETE /{id} - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/TicketTypeGroup",
        update_endpoint="/TicketTypeGroup",
        required_create_fields=("name",),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "name", "intent"),
    ),
    HaloResource(
        "timeslots",
        "/Timeslot",
        aliases=("timeslot",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 11 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        supports_get=False,
    ),
    HaloResource(
        "to-dos",
        "/ToDo",
        aliases=("to-do",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 12 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        supports_get=False,
        # Writes: spec-verified POST (POST-only: the spec offers no DELETE /{id}) - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/ToDo",
        update_endpoint="/ToDo",
        required_create_fields=("ticket_id", "text"),
        required_update_fields=("id",),
        write_preview_fields=("id", "ticket_id", "text", "group_id", "done"),
    ),
    HaloResource(
        "transcription-stores",
        "/TranscriptionStore",
        aliases=("transcription-store",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
    ),
    HaloResource(
        "xtype-roles",
        "/XtypeRole",
        aliases=("xtype-role",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 12 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # no GET /{id} route in spec -> list-only.
        table_fields=("id",),
        supports_get=False,
    ),
    HaloResource(
        "csp-invoices",
        "/cspinvoice",
        aliases=("csp-invoice",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        list_key="data",
    ),
    HaloResource(
        "item-suppliers",
        "/itemsupplier",
        aliases=("item-supplier",),
        # Route-verified 2026-10-02 (scripts/get_backlog_probes.py passes
        # 1+2): GET answered 200 across 10 attempts (documented +
        # scope params, truly bare, count=100) but the tenant holds no rows,
        # and the vendored spec ships no response schema - so table_fields
        # stays id-only until a populated tenant yields column evidence.
        # GET /{id} -> 400 (route alive).
        table_fields=("id",),
        # Writes: spec-verified POST + DELETE /{id} - never fired at the
        # tenant (verification: spec). The spec declares required: []
        # everywhere, so the required set is a commented house choice
        # from the POST schema's own properties (tests bind it there).
        create_endpoint="/itemsupplier",
        update_endpoint="/itemsupplier",
        required_create_fields=("item_id", "supplier_id"),
        required_update_fields=("id",),
        supports_delete=True,
        write_preview_fields=("id", "item_id", "supplier_id", "price", "note"),
    ),
    HaloResource(
        "asset-changes",
        "/AssetChange",
        aliases=("asset-change",),
        # Live 2026-10-02 (probe pass1): envelope changehistory,
        # 1 row(s) with asset_id=1 (a bare call returns 0 - the
        # filter is effectively required for rows). Columns observed on that
        # row (uncapped keys). No GET /{id} route in spec (detail probe
        # 404 = route absent) -> list-only.
        table_fields=("id", "asset_id", "asset_number", "asset_site", "customfield_id", "datetime", "field_desc", "field_id", "field_name", "item_id", "new_site", "new_value", "old_site", "old_value", "software_id", "software_user_id", "who", "who_id"),
        list_key="changehistory",
        supports_get=False,
    ),
    HaloResource(
        "asset-software",
        "/AssetSoftware",
        aliases=("installed-software",),
        # Live 2026-10-02 (probe pass1): envelope <bare array>,
        # 72 row(s) with device_id=1 (a bare call returns 0 - the
        # filter is effectively required for rows). Columns observed on that
        # row (uncapped keys). No GET /{id} route in spec (detail probe
        # 404 = route absent) -> list-only.
        table_fields=("id", "bundledesc", "cost", "count", "did", "install_date", "licence_id", "licence_name", "licence_required", "moduleid", "name", "role_id", "role_name", "snowid", "user_id", "version"),
        supports_get=False,
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
