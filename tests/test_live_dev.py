"""Opt-in LIVE write verification against a throwaway dev tenant.

Every test in this module FIRES REAL WRITES. They run only when a dev
tenant is configured via environment variables, so CI and default local
runs skip them entirely:

    HALO_DEV_TENANT_URL=https://<you>.halopsa.com
    HALO_DEV_CLIENT_ID=...
    HALO_DEV_CLIENT_SECRET=...
    pytest tests/test_live_dev.py -v

Ground rules enforced here, not by convention:
- the tenant must NOT be the production host (a loud refusal, so these
  can never be pointed at midtowntg by accident);
- every test cleans up after itself (create -> verify -> delete), so the
  trial stays tidy across campaign reruns;
- records are named with a recognizable prefix for any manual sweep.
"""

from __future__ import annotations

import os
import uuid

import pytest

from halocli.config import HaloProfile
from halocli.resources import get_resource
from halocli.writes import delete_resource, execute_write

ENV_KEYS = ("HALO_DEV_TENANT_URL", "HALO_DEV_CLIENT_ID", "HALO_DEV_CLIENT_SECRET")
PROD_HOST = "midtowntg.halopsa.com"

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(k) for k in ENV_KEYS),
    reason="dev tenant not configured (HALO_DEV_* env vars absent - see the "
    "dev-tenant setup issue for the trial flow)",
)

PROBE_PREFIX = "halocli-dev-probe-"


def dev_profile() -> HaloProfile:
    """Build the dev profile from env - with a hard refusal of prod."""
    url = os.environ["HALO_DEV_TENANT_URL"].rstrip("/")
    host = url.split("//", 1)[-1].split("/", 1)[0]
    if PROD_HOST in host:
        raise AssertionError(
            f"HALO_DEV_TENANT_URL points at PRODUCTION ({host}); live write "
            "verification must never target the production tenant"
        )
    return HaloProfile(
        tenant_url=url,
        client_id=os.environ["HALO_DEV_CLIENT_ID"],
        client_secret=os.environ["HALO_DEV_CLIENT_SECRET"],
        auth_mode="client_credentials",
    )


async def _client():
    from halocli.client import HaloClient

    return HaloClient(dev_profile(), profile_name="dev-verification")


@pytest.mark.asyncio
async def test_tags_full_roundtrip_create_update_delete() -> None:
    """The full CUD cycle on the simplest entity, self-cleaning."""
    tags = get_resource("tags")
    name = f"{PROBE_PREFIX}{uuid.uuid4().hex[:12]}"
    client = await _client()
    created_id: int | str | None = None
    async with client:
        created = await execute_write(
            client, tags, {"text": name, "type": 0}, update=False, apply=True
        )
        assert created["ok"], created
        result = created.get("result") or {}
        created_id = (result.get("id") if isinstance(result, dict) else None) or None
        if created_id is None and isinstance(result, dict):
            created_id = result.get("Id")
        assert created_id, f"create response carried no id: {created}"

        # read back: the record exists with our name (103 tags > one page)
        body = await client.list_resource("tags", count="500")
        from halocli.utils import parse_page_result

        rows = parse_page_result(body, list_key=tags.list_key).items
        assert any(str(r.get("id")) == str(created_id) for r in rows), (
            f"created tag {created_id} not visible in the list"
        )

        updated = await execute_write(
            client,
            tags,
            {"id": created_id, "text": name + "-v2"},
            update=True,
            apply=True,
        )
        assert updated["ok"], updated

    # cleanup OUTSIDE the create/update client block (fresh delete path)
    async with await _client() as client:
        deleted = await delete_resource(client, tags, created_id, apply=True)
        assert deleted["ok"], deleted


@pytest.mark.asyncio
async def test_todo_create_on_sample_ticket_leaves_labeled_orphan() -> None:
    """POST-only entity: create works; no delete route exists by design, so
    the record stays on the trial (named for the manual sweep)."""
    from halocli.utils import parse_page_result

    todos = get_resource("to-dos")
    assert todos.supports_create and not todos.supports_delete
    async with await _client() as client:
        body = await client.list_resource(
            "tickets", pageinate=True, page_no=1, page_size=1, count="1"
        )
        rows = parse_page_result(body, list_key=get_resource("tickets").list_key).items
        assert rows, "trial tenant has no sample tickets to attach to"
        ticket_id = rows[0]["id"]
        created = await execute_write(
            client,
            todos,
            {"ticket_id": ticket_id, "text": f"{PROBE_PREFIX}{uuid.uuid4().hex[:12]}"},
            update=False,
            apply=True,
        )
        assert created["ok"], created


@pytest.mark.asyncio
async def test_lookup_clearcache_action_executes() -> None:
    """Nested action op flips from verification: spec to live on this tenant."""
    lookups = get_resource("lookups")
    clear = next(op for op in lookups.operations if op.name == "clear-cache")
    assert clear.method == "POST"
    async with await _client() as client:
        # body=False op: no payload; reaching here without an exception IS
        # the live verification (spec-verified until now).
        response = await client.request("POST", clear.path)
        assert response is not None
