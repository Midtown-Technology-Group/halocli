from __future__ import annotations

from dataclasses import dataclass, field


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
        table_fields=("id", "name", "title"),
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
        table_fields=("id", "quote_number", "client_name", "total"),
    ),
    HaloResource(
        "releases",
        "/Release",
        aliases=("release",),
        table_fields=("id", "name", "status_name"),
    ),
    HaloResource("reports", "/Report", aliases=("report",), table_fields=("id", "name", "type")),
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
        table_fields=("id", "client_id", "date", "note"),
    ),
    HaloResource("top-levels", "/TopLevel", aliases=("top-level",), table_fields=("id", "name")),
    HaloResource(
        "expenses",
        "/Expense",
        aliases=("expense",),
        table_fields=("id", "agent_name", "date", "value"),
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
