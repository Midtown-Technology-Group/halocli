#!/usr/bin/env python3
"""(a) fetch matrix runlog rows BY ID (list window hides them);
(b) S6b: iteration with the array source wired (message + input var + form value).

    python scripts/runbook_chain_s6b.py [--profile dev]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PROBE = "haloclidevprobechain"

S6B_STEPS = [
    {
        "step_id": 1,
        "name": "Begin iteration",
        "steptype": 2,
        "auto_action": 12,
        "isstart": True,
        "allow_all_statuses": True,
        "message": "<<items>>",
        "actions": [
            {
                "action_type": 22,
                "action_id": -22,
                "action_name": "Has elements",
                "start_step": 1,
                "end_step": 2,
                "seq": 1,
                "use_work_hours": True,
                "approval_result": 1,
                "chat_selection_order": 1,
            },
            {
                "action_type": 22,
                "action_id": -22,
                "action_name": "Has no elements",
                "start_step": 1,
                "end_step": 4,
                "seq": 2,
                "use_work_hours": True,
                "approval_result": 1,
                "chat_selection_order": 1,
            },
        ],
    },
    {
        "step_id": 2,
        "name": "Body hop",
        "steptype": 2,
        "auto_action": 21,
        "duration": 0,
        "allow_all_statuses": True,
        "actions": [
            {
                "action_type": 32,
                "action_id": -32,
                "action_name": "Sleep Finished",
                "start_step": 2,
                "end_step": 3,
                "seq": 1,
                "use_work_hours": True,
                "approval_result": 1,
                "chat_selection_order": 1,
            }
        ],
    },
    {
        "step_id": 3,
        "name": "Next iteration",
        "steptype": 2,
        "auto_action": 13,
        "allow_all_statuses": True,
        "message": "<<items>>",
        "actions": [
            {
                "action_type": 23,
                "action_id": -23,
                "action_name": "Iteration finished",
                "start_step": 3,
                "end_step": 4,
                "seq": 1,
                "use_work_hours": True,
                "approval_result": 1,
                "chat_selection_order": 1,
            },
            {
                "action_type": 23,
                "action_id": -23,
                "action_name": "Next iteration",
                "start_step": 3,
                "end_step": -98,
                "seq": 2,
                "use_work_hours": True,
                "approval_result": 1,
                "chat_selection_order": 1,
            },
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
    parser.add_argument("--rows", default="2469,2470,2471,2472,2473,2474")
    args = parser.parse_args()

    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: production host")

    out: dict[str, Any] = {"tenant": host, "matrix_rows_by_id": []}
    async with HaloClient(profile, profile_name=args.profile) as client:
        # (a) by-id fetch
        for rid in [r.strip() for r in args.rows.split(",") if r.strip()]:
            try:
                doc = await client.request("GET", f"/Automation/{rid}", timeout=30)
                out["matrix_rows_by_id"].append(
                    {
                        k: doc.get(k)
                        for k in (
                            "id",
                            "runbook_name",
                            "status",
                            "error",
                            "steps_executed",
                            "runbook_step",
                            "runbook_step_name",
                            "iteration",
                        )
                    }
                    if isinstance(doc, dict)
                    else {"id": rid, "raw": str(doc)[:120]}
                )
            except Exception as exc:  # noqa: BLE001
                out["matrix_rows_by_id"].append({"id": rid, "error": str(exc)[:160]})

        # (b) S6b with the array wired
        doc = {
            "name": f"{PROBE}-s6b-iteration",
            "type": 1,
            "active": True,
            "runbook_start_type": 1,
            "inbound_authentication_type": 0,
            "input_variables": [
                {
                    "id": None,
                    "key": "items",
                    "value": '["a","b","c"]',
                    "data_type": 1,
                    "description": "array to iterate",
                },
            ],
            "steps": S6B_STEPS,
        }
        rec: dict[str, Any] = {}
        try:
            resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
            row = resp[0] if isinstance(resp, list) else resp
            wid = row.get("id")
            rec["id"] = wid
        except Exception as exc:  # noqa: BLE001
            rec["create_error"] = str(exc)[:400]
            out["s6b"] = rec
            print(json.dumps(out, indent=2, default=str))
            return 0

        try:
            await client.request(
                "POST",
                f"/Automation/{wid}",
                json_body={"formCollection": [{"Key": "items", "Value": '["a","b","c"]'}]},
                timeout=120,
            )
            rec["fire"] = "ok"
        except Exception as exc:  # noqa: BLE001
            rec["fire"] = str(exc)[:250]
        # find the row by id scan (fresh rows appear immediately after fire)
        found = None
        for i in range(10):
            time.sleep(2)
            body = await client.request("GET", "/Automation", params={"count": "50"}, timeout=45)
            rows = (
                body
                if isinstance(body, list)
                else next((v for v in body.values() if isinstance(v, list)), [])
            )
            mine = [r for r in rows if r.get("runbook_id") == wid]
            if mine:
                found = mine[0]
                break
            # try by-id guessing: last row id seen
            if rows:
                top = max(r.get("id") or 0 for r in rows)
                for cand in range(top, max(top - 4, 0), -1):
                    try:
                        d2 = await client.request("GET", f"/Automation/{cand}", timeout=20)
                        if isinstance(d2, dict) and d2.get("runbook_id") == wid:
                            found = d2
                            break
                    except Exception:  # noqa: BLE001
                        pass
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
                    "iteration",
                )
            }
        else:
            rec["runlog"] = "not found in list window"
        try:
            await client.request("DELETE", f"/Webhook/{wid}", timeout=30)
            rec["cleanup"] = "deleted"
        except Exception as exc:  # noqa: BLE001
            rec["cleanup"] = str(exc)[:160]
        out["s6b"] = rec

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
