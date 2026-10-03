"""Label hydration: resolve bare foreign keys to tenant-configured labels.

Halo returns bare ids for most entity references (`status_id: 9`) and only
occasionally denormalises a label (`client_name`, `site_name`). Operators
should never need a mapping table, so `list`/`get` hydrate a `<stem>_name`
label beside every resolvable FK - using Halo's own naming convention -
without ever touching the raw id or overwriting a label Halo already sent.

Resolution uses first-class reads: one bounded list fetch per lookup entity
(memoised per command run), with a capped per-id detail fallback when the id
is not on the first page. Preview stays zero-network by contract (ids are
the correct wire format there); hydration runs only where a live client
exists: `get`, `list`, and post-`apply` write results.

Every RESOLVERS entry is test-bound: the target resource must exist and the
label field must appear in its table_fields or the spec's POST properties.
"""

from __future__ import annotations

from typing import Any

from .resources import get_resource
from .utils import MAX_PAGE_SIZE, parse_page_result

# fk column -> (resource, label field, output key). Output key defaults to
# <fk minus _id>_name (Halo's own convention: status_id -> status_name).
RESOLVERS: dict[str, tuple[str, str, str | None]] = {
    "status_id": ("statuses", "name", None),
    "priority_id": ("priorities", "name", None),
    "agent_id": ("agents", "name", None),
    "closure_agent_id": ("agents", "name", "closure_agent_name"),
    "tickettype_id": ("ticket-types", "name", None),
    "client_id": ("clients", "name", None),
    "site_id": ("sites", "name", None),
    "team_id": ("teams", "name", None),
    "sla_id": ("slas", "name", None),
    "user_id": ("users", "name", None),
    "asset_id": ("assets", "name", None),
    "budgettype_id": ("budget-types", "name", None),
    "workday_id": ("workdays", "name", None),
    "item_id": ("items", "name", None),
    "supplier_id": ("suppliers", "name", None),
    "mailbox_id": ("mailboxes", "name", None),
    "outcome_id": ("outcomes", "buttonname", None),
    "product_id": ("products", "name", None),
    "release_id": ("releases", "name", None),
    "releasenotegroup_id": ("release-note-groups", "name", None),
    "organisation_id": ("organisations", "name", None),
    "top_level_id": ("top-levels", "name", None),
    "cost_centre_id": ("cost-centres", "name", None),
    "service_id": ("services", "name", None),
    "service_category_id": ("service-categories", "name", None),
    "assettype_id": ("asset-types", "name", None),
    "item_assettype_id": ("asset-types", "name", "assettype_name"),
    "stockbin_id": ("stock-bins", "name", None),
    "cab_id": ("cabs", "name", None),
    "workflow_id": ("workflows", "name", None),
    "approval_process_id": ("approval-process", "name", None),
    "appointment_id": ("appointments", "subject", None),
    "contract_schedule_plan_id": ("contract-schedule-plans", "subject", None),
    "categoryid_1": ("categories", "name", "category_1"),
    "categoryid_2": ("categories", "name", "category_2"),
    "categoryid_3": ("categories", "name", "category_3"),
    "categoryid_4": ("categories", "name", "category_4"),
}

# Per-run cap on per-id detail fetches (ids missing from the first lookup
# page of a large entity). Bounds worst-case cost on huge lists.
DETAIL_BUDGET = 100

# Join-column overrides: some entities key their list rows by GUID (`id`)
# while other records reference them by an integer column the list exposes
# separately. Halo quirk: /Priority rows carry id=<GUID> and priorityid=<int>;
# tickets store the int, and /Priority/<int> 404s - so we join on the column.
JOIN_FIELDS: dict[str, str] = {
    "priority_id": "priorityid",
}


def join_field_for(fk: str) -> str:
    return JOIN_FIELDS.get(fk, "id")


def label_key_for(fk: str) -> str:
    override = RESOLVERS[fk][2]
    return override or (fk[:-3] + "_name")


async def hydrate_items(
    client: Any,
    items: list[dict],
    *,
    enabled: bool = True,
    detail_budget: int = DETAIL_BUDGET,
) -> int:
    """Add tenant label keys for resolvable bare FKs; return labels added.

    Never overwrites a label Halo already sent, never touches the id itself,
    and tolerates lookup failures (one bad id must not fail the command).
    """
    if not enabled or not items:
        return 0

    pending: list[tuple[dict, str, str, str, str, Any]] = []
    needed: dict[str, set[str]] = {}
    # (item, fk, resource, label_field, label_key, id)
    for item in items:
        if not isinstance(item, dict):
            continue
        for fk, (resource, field, _override) in RESOLVERS.items():
            if fk not in item:
                continue
            label_key = label_key_for(fk)
            existing = item.get(label_key)
            if existing not in (None, ""):
                continue  # Halo already sent a label; never overwrite
            value = item[fk]
            if value in (None, "", 0):
                continue  # placeholder / unset
            if not isinstance(value, (int, str)):
                continue  # exotic id shapes stay bare
            pending.append((item, fk, resource, field, label_key, value))
            needed.setdefault(resource, set()).add(str(value))
    if not pending:
        return 0

    # one bounded list fetch per lookup entity (memoised for the run)
    lookup: dict[str, dict[str, str]] = {}
    leftover: dict[str, set[str]] = {}
    res_join: dict[str, str] = {}
    for p in pending:
        res_join.setdefault(p[2], join_field_for(p[1]))
    for resource, ids in needed.items():
        reg = get_resource(resource)
        join_col = res_join.get(resource, "id")
        body = await client.list_resource(
            resource, pageinate=True, page_no=1, page_size=MAX_PAGE_SIZE
        )
        page = parse_page_result(body, list_key=reg.list_key)
        table: dict[str, str] = {}
        field = _label_field(resource)
        for row in page.items:
            label = row.get(field)
            key = row.get(join_col)
            if label not in (None, "") and key not in (None, ""):
                table[str(key)] = label
        lookup[resource] = table
        missing = ids - set(table)
        if missing and join_col == "id":
            # detail fallback needs the entity's own id space; GUID-keyed
            # entities with an integer join (priorities) cannot translate,
            # so their misses stay bare rather than firing doomed lookups.
            leftover[resource] = missing

    # capped per-id detail fallback (exact, but bounded)
    spent = 0
    for resource, ids in leftover.items():
        for raw_id in sorted(ids):
            if spent >= detail_budget:
                break
            spent += 1
            try:
                detail = await client.get_resource(resource, raw_id)
            except Exception:  # noqa: BLE001 - a dead id must not kill the read
                continue
            if isinstance(detail, dict):
                label = detail.get(_label_field(resource))
                if label not in (None, ""):
                    lookup[resource][str(raw_id)] = label

    added = 0
    for item, _fk, resource, _field, label_key, value in pending:
        label = lookup.get(resource, {}).get(str(value))
        if label in (None, ""):
            continue
        item[label_key] = label
        added += 1
    return added


def _label_field(resource: str) -> str:
    """Label column for a lookup resource (RESOLVERS is authoritative)."""
    for _fk, (res, field, _key) in RESOLVERS.items():
        if res == resource:
            return field
    return "name"
