#!/usr/bin/env python3
"""Phase-B evidence: custom integrations, methods, runbook definitions - shapes.

GET-only probe of the integration family on the trial:

- GET /CustomIntegration (+ list) - the integration definitions
- GET /CustomIntegrationMethod (+ detail) - the43-method library
- GET /IntegrationRunbookVariableGroup (+ {id}) - the variable palette
- WHERE ARE RUNBOOK DEFINITIONS? Hypothesis: nested under the
  integration document (the UI nests "Integration Runbooks" under
  Custom Integrations), like workflows carry their steps. Probe
  integration details for runbook/flow keys; also probe plausible
  dedicated paths.

    python scripts/integration_family_probe.py [--profile dev]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def shape(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        if depth >= 2:
            return {k: type(v).__name__ for k, v in list(value.items())[:50]}
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:70]}
    if isinstance(value, list):
        if not value:
            return []
        return [shape(value[0], depth + 1), f"...{len(value)} items"]
    if isinstance(value, str) and len(value) > 60:
        return f"str({len(value)})"
    return value


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    args = parser.parse_args()

    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")

    out: dict[str, Any] = {"tenant": host}

    async def get(key: str, path: str, params: dict | None = None) -> Any:
        try:
            body = await client.request("GET", path, params=params or {}, timeout=45)
            if isinstance(body, list):
                out[key] = {
                    "rows": len(body),
                    "first": shape(body[0]) if body else None,
                    "keys": sorted(body[0].keys()) if body else [],
                }
            elif isinstance(body, dict):
                rows = next((v for v in body.values() if isinstance(v, list)), None)
                if rows is not None:
                    out[key] = {
                        "envelope_rows": len(rows),
                        "first": shape(rows[0]) if rows else None,
                        "keys": sorted(rows[0].keys()) if rows else [],
                        "envelope_keys": sorted(body.keys()),
                    }
                else:
                    out[key] = {"shape": shape(body), "keys": sorted(body.keys())}
            else:
                out[key] = {"type": type(body).__name__}
        except Exception as exc:  # noqa: BLE001
            out[key] = {"error": str(exc)[:200]}

    async with HaloClient(profile, profile_name=args.profile) as client:
        await get("integrations_list", "/CustomIntegration", {"count": "3"})
        # pick a real integration id for the detail probe
        integration_id = None
        try:
            body = await client.request(
                "GET", "/CustomIntegration", params={"count": "3"}, timeout=45
            )
            rows = (
                body
                if isinstance(body, list)
                else next((v for v in body.values() if isinstance(v, list)), [])
            )
            if rows:
                integration_id = rows[0].get("id")
        except Exception:  # noqa: BLE001
            pass
        out["integration_id_sampled"] = integration_id
        if integration_id is not None:
            await get(
                "integration_detail",
                f"/CustomIntegration/{integration_id}",
                {"includedetails": "true"},
            )
            await get("integration_detail_bare", f"/CustomIntegration/{integration_id}")
        await get("methods_list", "/CustomIntegrationMethod", {"count": "3"})
        # method detail: where does the runbook/flow of a method live?
        try:
            body = await client.request(
                "GET", "/CustomIntegrationMethod", params={"count": "1"}, timeout=45
            )
            rows = (
                body
                if isinstance(body, list)
                else next((v for v in body.values() if isinstance(v, list)), [])
            )
            if rows:
                mid = rows[0].get("id")
                out["method_id_sampled"] = mid
                await get(
                    "method_detail", f"/CustomIntegrationMethod/{mid}", {"includedetails": "true"}
                )
                await get("method_detail_bare", f"/CustomIntegrationMethod/{mid}")
        except Exception as exc:  # noqa: BLE001
            out["method_detail_error"] = str(exc)[:200]
        await get("variable_groups", "/IntegrationRunbookVariableGroup", {"count": "3"})
        # plausible dedicated runbook-definition paths (GET-only probing)
        for key, path in (
            ("runbooks_list_guess", "/IntegrationRunbook"),
            ("runbooks_alt_guess", "/Runbook"),
            ("automation_def_guess", "/Automation/Runbook"),
        ):
            await get(key, path, {"count": "3"})

    print(json.dumps(out, indent=2, default=str)[:7000])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
