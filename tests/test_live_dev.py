"""Opt-in LIVE write verification against a throwaway dev tenant.

Every test in this module FIRES REAL WRITES. They run only when a dev
tenant is configured, so CI and default local runs skip them entirely.

Preferred (native profile, same shape as prod - no secrets in env):

    halocli configure --profile dev --tenant-url https://<you>.trial.usehalo.com \\
      --auth-mode halo-interactive --client-id <app id>
    halocli auth discover --tenant-url https://<you>.trial.usehalo.com --profile dev --save
    halocli auth login --profile dev
    $env:HALO_DEV_PROFILE = "dev"
    pytest tests/test_live_dev.py -v

Headless alternative (client-credentials secret in env):

    HALO_DEV_TENANT_URL=https://<you>.trial.usehalo.com
    HALO_DEV_CLIENT_ID=...
    HALO_DEV_CLIENT_SECRET=...
    pytest tests/test_live_dev.py -v

Ground rules enforced here, not by convention:
- the tenant must NOT be the production host (a loud refusal, so these
  can never be pointed at midtowntg by accident - checked in both modes);
- every test cleans up after itself (create -> verify -> delete), so the
  trial stays tidy across campaign reruns;
- records are named with a recognizable prefix for any manual sweep.

The module carries the ``real_state`` marker: profile mode must read the
real config file and the real OS credential store (the token minted by
``auth login``), which the suite's autouse isolation fixture would
otherwise redirect to tmp. CI still never runs these tests - they skip
without HALO_DEV_* configuration - and isolation stays on for every
other test.
"""

from __future__ import annotations

import os
import uuid

import pytest

from halocli.config import HaloProfile, load_profile
from halocli.resources import get_resource
from halocli.writes import delete_resource, execute_write

ENV_KEYS = ("HALO_DEV_TENANT_URL", "HALO_DEV_CLIENT_ID", "HALO_DEV_CLIENT_SECRET")
PROFILE_ENV = "HALO_DEV_PROFILE"
PROD_HOST = "midtowntg.halopsa.com"


def _active_mode() -> str | None:
    """Profile mode wins when both are set: it is the house style."""
    if os.environ.get(PROFILE_ENV):
        return "profile"
    if all(os.environ.get(k) for k in ENV_KEYS):
        return "env"
    return None


pytestmark = [
    pytest.mark.skipif(
        _active_mode() is None,
        reason="dev tenant not configured (set HALO_DEV_PROFILE=<name> for the "
        "native-profile path, or the HALO_DEV_TENANT_URL/CLIENT_ID/CLIENT_SECRET "
        "env trio - see the dev-tenant setup issue for the trial flow)",
    ),
    pytest.mark.real_state,
]

PROBE_PREFIX = "halocli-dev-probe-"


def _refuse_prod(tenant_url: str) -> None:
    host = tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if PROD_HOST in host:
        raise AssertionError(
            f"dev tenant URL points at PRODUCTION ({host}); live write "
            "verification must never target the production tenant"
        )


def dev_profile() -> tuple[HaloProfile, str]:
    """Resolve (profile, profile_name), with a hard refusal of prod.

    Profile mode loads the named native profile - whatever auth mode it
    carries (halo_interactive tokens come from the OS credential store;
    nothing secret lives in env). Env mode keeps the original
    client-credentials path for headless runs.
    """
    mode = _active_mode()
    assert mode is not None  # guarded by pytestmark
    if mode == "profile":
        name = os.environ[PROFILE_ENV]
        profile = load_profile(name)
        _refuse_prod(profile.tenant_url)
        return profile, name
    url = os.environ["HALO_DEV_TENANT_URL"].rstrip("/")
    _refuse_prod(url)
    return (
        HaloProfile(
            tenant_url=url,
            client_id=os.environ["HALO_DEV_CLIENT_ID"],
            client_secret=os.environ["HALO_DEV_CLIENT_SECRET"],
            auth_mode="client_credentials",
        ),
        "dev-verification",
    )


async def _client():
    from halocli.client import HaloClient

    profile, name = dev_profile()
    return HaloClient(profile, profile_name=name)


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
    assert todos.supports_create
    assert not todos.supports_delete
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
