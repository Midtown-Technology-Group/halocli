#!/usr/bin/env python3
"""Follow-up: traversal numbers from the matrix rows + the iteration recipe (S6).

1. Read-only: pull runlog rows 2469-2473 (the five matrix fires) and print
   steps_executed / runbook_step / runbook_step_name - the per-run traversal
   proof fields.
2. S6 iteration recipe: Begin Array Iteration (aa12/act22) -> body sleep
   -> Next Iteration (aa13/act23 with the template's -98 loop-back sentinel)
   -> Success. Fire it and record status + traversal.

    python scripts/runbook_chain_followup.py [--profile dev]
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

PROBE = "haloclidevprobechain"


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


S6_STEPS = [
    {
        "step_id": 1,
        "name": "Begin iteration",
        "steptype": 2,
        "auto_action": 12,
        "isstart": True,
        "allow_all_statuses": True,
        "actions": [
            edge(22, "Has elements", 1, 2, 1),
            edge(22, "Has no elements", 1, 4, 2),
        ],
    },
    {
        "step_id": 2,
        "name": "Body hop",
        "steptype": 2,
        "auto_action": 21,
        "duration": 0,
        "allow_all_statuses": True,
        "actions": [edge(32, "Sleep Finished", 2, 3, 1)],
    },
    {
        "step_id": 3,
        "name": "Next iteration",
        "steptype": 2,
        "auto_action": 13,
        "allow_all_statuses": True,
        "actions": [
            edge(23, "Iteration finished", 3, 4, 1),
            edge(23, "Next iteration", 3, -98, 2),
        ],
    },
    {
        "step_id": 4,
        "name": "Success",
        "steptype": 3,
        "isend": True,
        "islaststep": True,
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

    out: dict[str, Any] = {"tenant": host}
    async with HaloClient(profile, profile_name=args.profile) as client:
        # 1. read-only traversal numbers for the matrix rows
        body = await client.request("GET", "/Automation", params={"count": "40"}, timeout=45)
        rows = (
            body
            if isinstance(body, list)
            else next((v for v in body.values() if isinstance(v, list)), [])
        )
        out["matrix_traversal"] = [
            {
                k: r.get(k)
                for k in (
                    "id",
                    "runbook_name",
                    "status",
                    "error",
                    "steps_executed",
                    "runbook_step",
                    "runbook_step_name",
                )
            }
            for r in rows
            if str(r.get("runbook_name", "")).startswith(PROBE)
        ]

        # 2. S6 iteration recipe
        doc = {
            "name": f"{PROBE}-s6-iteration",
            "type": 1,
            "active": True,
            "runbook_start_type": 1,
            "inbound_authentication_type": 0,
            "steps": S6_STEPS,
        }
        rec: dict[str, Any] = {}
        try:
            resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
            row = resp[0] if isinstance(resp, list) else resp
            wid = row.get("id")
            rec["id"] = wid
        except Exception as exc:  # noqa: BLE001
            rec["create_error"] = str(exc)[:400]
            out["s6"] = rec
            print(json.dumps(out, indent=2, default=str))
            return 0

        before = {r.get("id") for r in rows}
        try:
            await client.request(
                "POST",
                f"/Automation/{wid}",
                json_body={"formCollection": [{"Key": "items", "Value": "3"}]},
                timeout=90,
            )
            rec["fire"] = "ok"
        except Exception as exc:  # noqa: BLE001
            rec["fire"] = str(exc)[:250]
        found = []
        for _ in range(8):
            await asyncio.sleep(2)
            body = await client.request("GET", "/Automation", params={"count": "40"}, timeout=45)
            rows2 = (
                body
                if isinstance(body, list)
                else next((v for v in body.values() if isinstance(v, list)), [])
            )
            found = [r for r in rows2 if r.get("id") not in before and r.get("runbook_id") == wid]
            if found:
                break
        if found:
            r = found[0]
            rec["runlog"] = {
                k: r.get(k)
                for k in (
                    "id",
                    "status",
                    "error",
                    "steps_executed",
                    "runbook_step",
                    "runbook_step_name",
                    "iteration",
                )
            }
        else:
            rec["runlog"] = "no new row"
        try:
            await client.request("DELETE", f"/Webhook/{wid}", timeout=30)
            rec["cleanup"] = "deleted"
        except Exception as exc:  # noqa: BLE001
            rec["cleanup"] = str(exc)[:160]
        out["s6"] = rec

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
