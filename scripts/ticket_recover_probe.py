#!/usr/bin/env python3
"""Ticket UNDELETE probe: the `_recover` recipe from homotechsual/HaloAPI.

Their Restore-HaloTicket posts [{id, _recover: true, _validate_updates: true}]
to /Tickets - neither flag appears in the vendored spec (shell schemas) or
anywhere in HaloCLI. Test the recipe end-to-end on the trial:

    create probe ticket -> DELETE it -> GET (expect gone) ->
    POST [{id, _recover: true, _validate_updates: true}] -> GET (restored?) ->
    DELETE again (cleanup).

    python scripts/ticket_recover_probe.py [--profile dev]
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

PROBE = "halocli-dev-probe-recover"


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
        # 1. create ------------------------------------------------------
        resp = await client.request(
            "POST",
            "/Tickets",
            json_body=[{"summary": f"{PROBE}: undelete round-trip"}],
            timeout=45,
        )
        ticket = resp[0] if isinstance(resp, list) and resp else resp
        tid = ticket.get("id")
        out["ticket_id"] = tid
        if tid is None:
            out["outcome"] = "create failed"
            print(json.dumps(out, indent=2))
            return 1

        # 2. delete ------------------------------------------------------
        await client.request("DELETE", f"/Tickets/{tid}", timeout=30)
        try:
            await client.request("GET", f"/Tickets/{tid}", timeout=30)
            out["after_delete"] = "STILL READABLE (soft delete?)"
        except Exception:  # noqa: BLE001
            out["after_delete"] = "gone (404) as expected"

        # 3. recover ------------------------------------------------------
        try:
            resp = await client.request(
                "POST",
                "/Tickets",
                json_body=[{"id": tid, "_recover": True, "_validate_updates": True}],
                timeout=45,
            )
            out["recover_response"] = str(resp)[:300]
        except Exception as exc:  # noqa: BLE001
            out["recover_error"] = str(exc)[:300]

        # 4. verify restored ----------------------------------------------
        try:
            doc = await client.request("GET", f"/Tickets/{tid}", timeout=30)
            out["restored"] = isinstance(doc, dict) and doc.get("summary")
        except Exception as exc:  # noqa: BLE001
            out["restored"] = False
            out["restore_verify_error"] = str(exc)[:200]

        # 5. cleanup: delete again (recovery needs the ticket gone first) --
        cleanup = []
        if out.get("restored"):
            try:
                await client.request("DELETE", f"/Tickets/{tid}", timeout=30)
                try:
                    await client.request("GET", f"/Tickets/{tid}", timeout=30)
                    cleanup.append("second delete: still readable?!")
                except Exception:  # noqa: BLE001
                    cleanup.append("second delete: gone (clean)")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"second delete failed: {str(exc)[:140]}")
        else:
            # ticket stayed deleted (or never restored) - nothing to clean
            cleanup.append("no restored record to clean")
        out["cleanup"] = cleanup

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
