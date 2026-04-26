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

    @property
    def command_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


RESOURCES: tuple[HaloResource, ...] = (
    HaloResource(
        "tickets",
        "/Tickets",
        aliases=("ticket",),
        table_fields=("id", "summary", "status_name", "client_name", "agent_name"),
    ),
    HaloResource(
        "clients",
        "/Client",
        aliases=("client",),
        table_fields=("id", "name", "accountmanager_name"),
    ),
    HaloResource("agents", "/Agent", aliases=("agent",), table_fields=("id", "name", "team", "use")),
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
    HaloResource("sites", "/Site", aliases=("site",), table_fields=("id", "name", "client_name")),
    HaloResource(
        "assets",
        "/Asset",
        aliases=("asset",),
        table_fields=("id", "inventory_number", "name", "client_name", "site_name"),
    ),
    HaloResource(
        "actions",
        "/Actions",
        aliases=("action",),
        table_fields=("id", "ticket_id", "who", "note"),
    ),
    HaloResource("statuses", "/Status", aliases=("status",), table_fields=("id", "name", "use")),
    HaloResource(
        "priorities",
        "/Priority",
        aliases=("priority",),
        table_fields=("id", "name", "sequence"),
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
    ),
    HaloResource(
        "contracts",
        "/Contract",
        aliases=("contract",),
        table_fields=("id", "name", "client_name"),
    ),
    HaloResource(
        "invoices",
        "/Invoice",
        aliases=("invoice",),
        table_fields=("id", "invoice_number", "client_name", "total"),
    ),
    HaloResource(
        "opportunities",
        "/Opportunity",
        aliases=("opportunity",),
        table_fields=("id", "summary", "client_name", "status_name"),
    ),
    HaloResource(
        "projects",
        "/Project",
        aliases=("project",),
        table_fields=("id", "name", "client_name", "status_name"),
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
