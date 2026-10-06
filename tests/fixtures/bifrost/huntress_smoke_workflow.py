"""Huntress smoke: the API calls come from spec-generated method rows.

The three awaits bind (via --phase-bindings) to methods rendered from
Huntress' own OpenAPI spec by scripts/openapi_methods.py - the same
hop-label pattern the bifrost-workspace solutions use, so a converted
runbook and this one look identical if you squint.

try/except maps to Halo failure-edge routing (v17): when the vendor
call fails (401 without creds, or unreachable - both recorded in the
evidence), the Unsuccessful edge runs `degraded` and the run still
ends Success.
"""

from bifrost import workflow


async def getV1Account() -> dict:
    """Stand-in: the real call is the bound spec-generated method (aa6)."""
    return {}


async def degraded() -> dict:
    """Recovery hop when the vendor call fails (try/except -> act17 edge2)."""
    return {"status": "degraded"}


@workflow(
    name="Huntress Smoke: Spec-Generated Methods",
    description="Spec-generated aa6 methods behind a try/except recovery.",
)
async def huntress_smoke() -> dict:
    try:
        await getV1Account()
    except Exception:
        await degraded()
    return {"status": "checked"}
