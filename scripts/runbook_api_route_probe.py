#!/usr/bin/env python3
"""Routing diagnostic: does an aa6 step route when its HTTP call ANSWERS?

Two 3-step runbooks [api -> sleep -> Success] (+ Fail terminal):
  R1: method GET /api/InstanceInfo on https://clidev.trial.usehalo.com
      (proven reachable without auth - answers on the trial itself)
  R2: method GET /probe on https://example.com (external egress test)

Everything created is deleted (integration -> method -> runbook).

    python scripts/runbook_api_route_probe.py [--profile dev]
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

PROBE = "haloclidevprobeapi"

CASES = [
    ("self_instanceinfo", "https://clidev.trial.usehalo.com", "/api/InstanceInfo"),
    ("external_example", "https://example.com", "/probe"),
]


def edge(action_type: int, action_name: str, start: int, end: int, seq: int) -> dict:
    return {
        "action_type": action_type,
        "action_id": -action_type,
        "action_name": action_name,
        "start_step": start,
        "end_step": end,
        "seq": seq,
        "use_work_hours": True,
        "approval_result": 1,
        "chat_selection_order": 1,
    }


def steps_for(method_id: int) -> list[dict]:
    return [
        {
            "step_id": 1,
            "name": "Call",
            "steptype": 2,
            "auto_action": 6,
            "auto_action_type": method_id,
            "isstart": True,
            "allow_all_statuses": True,
            "actions": [
                edge(17, "Successful Response (200 - 299)", 1, 2, 1),
                edge(17, "Unsuccessful Response", 1, 4, 2),
            ],
        },
        {
            "step_id": 2,
            "name": "Hop",
            "steptype": 2,
            "auto_action": 21,
            "duration": 0,
            "allow_all_statuses": True,
            "actions": [edge(32, "Sleep Finished", 2, 3, 1)],
        },
        {
            "step_id": 3,
            "name": "Success",
            "steptype": 3,
            "isend": True,
            "islaststep": True,
            "allow_all_statuses": True,
            "actions": [],
        },
        {
            "step_id": 4,
            "name": "Fail",
            "steptype": 3,
            "auto_action": 1,
            "isend": True,
            "allow_all_statuses": True,
            "actions": [],
        },
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    args = parser.parse_args()

    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: production host")

    out: dict[str, Any] = {"tenant": host, "cases": {}}
    async with HaloClient(profile, profile_name=args.profile) as client:

        async def runlog_list() -> list[dict]:
            log = await client.request("GET", "/Automation", params={"count": "50"}, timeout=45)
            return (
                log
                if isinstance(log, list)
                else next((v for v in log.values() if isinstance(v, list)), [])
            )

        for label, base, path in CASES:
            rec: dict[str, Any] = {"base": base, "path": path}
            out["cases"][label] = rec
            # integration + method
            try:
                r = await client.request(
                    "POST",
                    "/CustomIntegration",
                    json_body=[
                        {
                            "name": f"{PROBE}-{label}",
                            "authorizationtype": 0,
                            "resourcebaseurl": base,
                        }
                    ],
                    timeout=60,
                )
                row = r[0] if isinstance(r, list) else r
                iid = row.get("id")
                r2 = await client.request(
                    "POST",
                    "/CustomIntegrationMethod",
                    json_body=[
                        {
                            "integration_id": iid,
                            "name": f"{PROBE}-{label}-m",
                            "path": path,
                            "method": 0,
                        }
                    ],
                    timeout=45,
                )
                row2 = r2[0] if isinstance(r2, list) else r2
                mid = row2.get("id")
                rec["integration_id"], rec["method_id"] = iid, mid
            except Exception as exc:  # noqa: BLE001
                rec["setup_error"] = str(exc)[:300]
                continue

            # runbook
            try:
                rb = await client.request(
                    "POST",
                    "/Webhook",
                    json_body=[
                        {
                            "name": f"{PROBE}-{label}",
                            "type": 1,
                            "active": True,
                            "runbook_start_type": 1,
                            "inbound_authentication_type": 0,
                            "steps": steps_for(mid),
                        }
                    ],
                    timeout=60,
                )
                row3 = rb[0] if isinstance(rb, list) else rb
                wid = row3.get("id")
                rec["runbook_id"] = wid
            except Exception as exc:  # noqa: BLE001
                rec["runbook_error"] = str(exc)[:300]
                continue

            before = {r_.get("id") for r_ in await runlog_list()}
            try:
                await client.request(
                    "POST",
                    f"/Automation/{wid}",
                    json_body={"formCollection": []},
                    timeout=120,
                )
                rec["fire"] = "ok"
            except Exception as exc:  # noqa: BLE001
                rec["fire"] = str(exc)[:250]

            found = None
            for _ in range(8):
                await asyncio.sleep(2)
                rows = await runlog_list()
                mine = [
                    r_ for r_ in rows if r_.get("id") not in before and r_.get("runbook_id") == wid
                ]
                if mine:
                    found = mine[0]
                    break
                top = max((r_.get("id") or 0 for r_ in rows), default=0)
                for cand in range(top, max(top - 15, 0), -1):
                    try:
                        d2 = await client.request("GET", f"/Automation/{cand}", timeout=20)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(d2, dict) and d2.get("runbook_id") == wid:
                        found = d2
                        break
                if found:
                    break
            if found:
                rec["runlog"] = {
                    k: found.get(k)
                    for k in (
                        "id",
                        "status",
                        "error",
                        "steps_executed",
                        "runbook_step",
                        "runbook_step_name",
                    )
                }
            else:
                rec["runlog"] = "not found"

            # cleanup: runbook, method, integration
            try:
                await client.request("DELETE", f"/Webhook/{wid}", timeout=30)
                rec["cleanup_runbook"] = "deleted"
            except Exception as exc:  # noqa: BLE001
                rec["cleanup_runbook"] = str(exc)[:140]
            try:
                await client.request("DELETE", f"/CustomIntegrationMethod/{mid}", timeout=30)
                await client.request("DELETE", f"/CustomIntegration/{iid}", timeout=30)
                rec["cleanup_integration"] = "deleted"
            except Exception as exc:  # noqa: BLE001
                rec["cleanup_integration"] = str(exc)[:140]

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
