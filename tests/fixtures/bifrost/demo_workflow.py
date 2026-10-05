"""Bifrost demo workflow fixture: every runnable primitive in one function.

Used by tests/test_bifrost_convert.py and by the live apply demo
(scripts/bifrost_convert.py --apply) against the trial:

- ``await asyncio.sleep(1)``          -> timed hop (aa21 + duration)
- ``await fetch_remote(client)``      -> API-call step bound to a
  CustomIntegrationMethod via demo_methods.yaml's ``bind_phase``
- ``for item in items:`` + await      -> Halo iteration pair (aa12/aa13);
  the tuple default serializes to the JSON array value Halo's
  ``<<items>>`` expression parses (empty string makes aa12 throw)
- ``limit: int = 10``                 -> input_variable with a default
"""

from __future__ import annotations

import asyncio

from bifrost import workflow


async def fetch_remote(client) -> dict:
    """Would live in a modules/ file; the phase is bound to a Halo
    CustomIntegrationMethod via demo_methods.yaml's bind_phase."""


async def process_one(item) -> dict:
    """Loop body callee."""


async def fallback(client) -> dict:
    """Else-arm callee: runs when the guard is false."""


async def notify_step(client) -> dict:
    """limit > 5 then-arm callee."""


async def legacy_step(client) -> dict:
    """limit > 5 else-arm callee."""


@workflow(
    name="Bifrost Demo: Sync Things",
    description="Wait, call the probe sink once, then iterate the input list.",
    category="Demos",
    effects=[{"kind": "network.read"}],
    enforced_bounds={"max_duration_seconds": 60, "max_external_calls": 2},
)
async def sync_things(client, limit: int = 10, items: tuple[str, ...] = ("a", "b")) -> dict:
    await asyncio.sleep(1)
    await fetch_remote(client)
    if items:
        for item in items:
            await asyncio.sleep(1)
            await process_one(item)
    else:
        await fallback(client)
    if limit > 5:
        await notify_step(client)
    else:
        await legacy_step(client)
    return {"limit": limit}
