#!/usr/bin/env python3
"""Phase-B promotion probe: CREATE round-trips for integrations + methods.

Cascade round-trip on the trial (inert probe objects, deleted in reverse):

1. POST /CustomIntegration - bounded ladder: {name} first, then defaults
   on validation rejection (evidence records which shape won).
2. POST /CustomIntegrationMethod - linked to the probe integration
   (integration_id cascade - like contact-groups in the sweep).
3. GET verify both (detail keys), then DELETE method -> integration ->
   verify gone.

Runbook DEFINITIONS are NOT probed: no endpoint exists (spec has only
IntegrationRunbookVariableGroup; /IntegrationRunbook, /Runbook,
/Automation/Runbook all404 - recorded by integration_family_probe.py).
The UI's "Import from JSON" is the documented interchange.

    python scripts/integration_create_probe.py [--profile dev]
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

PROBE = "haloclidevprobe"


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
    async with HaloClient(profile, profile_name=args.profile) as client:

        async def ladder(path: str, shapes: list[dict]) -> tuple[Any, list[str]]:
            attempts: list[str] = []
            for i, body in enumerate(shapes):
                try:
                    resp = await client.request("POST", path, json_body=[body], timeout=45)
                    created = resp[0] if isinstance(resp, list) and resp else resp
                    attempts.append(f"shape{i}: ok")
                    return created, attempts
                except Exception as exc:  # noqa: BLE001
                    attempts.append(f"shape{i}: {str(exc)[:180]}")
            return None, attempts

        # ----1. integration ------------------------------------------------
        integration, attempts = await ladder(
            "/CustomIntegration",
            [
                {"name": f"{PROBE}integration"},
                {"name": f"{PROBE}integration", "granttype": 0, "authorizationtype": 0},
                {
                    "name": f"{PROBE}integration",
                    "granttype": 0,
                    "authorizationtype": 0,
                    "resourcebaseurl": "https://example.invalid",
                    "scope": "all",
                },
            ],
        )
        out["integration_attempts"] = attempts
        iid = integration.get("id") if isinstance(integration, dict) else None
        out["integration_id"] = iid

        # ----2. method (cascade on the probe integration) -------------------
        method = None
        if iid is not None:
            method, attempts = await ladder(
                "/CustomIntegrationMethod",
                [
                    {
                        "integration_id": iid,
                        "name": f"{PROBE}method",
                        "path": "/probe",
                        "method": 1,
                    },
                    {
                        "integration_id": iid,
                        "name": f"{PROBE}method",
                        "path": "/probe",
                        "method": 1,
                        "resource": "https://example.invalid",
                        "requesttype": 0,
                        "responsetype": 0,
                    },
                ],
            )
            out["method_attempts"] = attempts
        mid = method.get("id") if isinstance(method, dict) else None
        out["method_id"] = mid

        # ----3. GET verify --------------------------------------------------
        if iid is not None:
            try:
                doc = await client.request("GET", f"/CustomIntegration/{iid}", timeout=30)
                out["integration_verify"] = (
                    isinstance(doc, dict) and doc.get("name") == f"{PROBE}integration"
                )
            except Exception as exc:  # noqa: BLE001
                out["integration_verify"] = f"error: {str(exc)[:160]}"
        if mid is not None:
            try:
                doc = await client.request("GET", f"/CustomIntegrationMethod/{mid}", timeout=30)
                out["method_verify"] = isinstance(doc, dict) and (
                    doc.get("integration_id") == iid or doc.get("name") == f"{PROBE}method"
                )
            except Exception as exc:  # noqa: BLE001
                out["method_verify"] = f"error: {str(exc)[:160]}"

        # ----4. cleanup: reverse order --------------------------------------
        cleanup = []
        if mid is not None:
            try:
                await client.request("DELETE", f"/CustomIntegrationMethod/{mid}", timeout=30)
                cleanup.append(f"method {mid}: deleted")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"method {mid}: DELETE failed {str(exc)[:130]}")
        if iid is not None:
            try:
                await client.request("DELETE", f"/CustomIntegration/{iid}", timeout=30)
                try:
                    await client.request("GET", f"/CustomIntegration/{iid}", timeout=30)
                    cleanup.append(f"integration {iid}: still readable?!")
                except Exception:  # noqa: BLE001
                    cleanup.append(f"integration {iid}: deleted (clean)")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"integration {iid}: DELETE failed {str(exc)[:130]}")
        out["cleanup"] = cleanup

        proven = bool(
            out.get("integration_id")
            and out.get("integration_verify") is True
            and out.get("method_id")
            and out.get("method_verify") is True
            and any("deleted (clean)" in c for c in cleanup)
        )
        out["roundtrip_proven"] = proven

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
