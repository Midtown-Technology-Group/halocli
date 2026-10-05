#!/usr/bin/env python3
"""Chaining matrix: which step recipes actually traverse on a public fire?

Recipes (each created active + public, fired, runlog polled for trace/status,
deleted; all probe-harmless - no real external calls):

S1 sleep duration:1 -> Success terminal           (neutral hop, expect complete)
S2 sleep duration:0 -> Success terminal           (instant variant)
S3 aa6 aat:99999 (nonexistent method) -> both
   act17 edges -> Fail terminal                   (expect failure-edge traversal)
S4 condition st:1 aa6, no criteria -> Success     (expect 'Condition met' advance)
S5 two chained sleeps -> terminal                 (expect 2-hop traversal)

Evidence: runlog rows keyed by our runbook_id incl. trace/status/error.

    python scripts/runbook_chain_matrix.py [--profile dev]
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
    """Canonical edge object copied from working trial runbooks."""
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


def sleep_step(step_id: int, name: str, duration: int, *, start: bool = False) -> dict:
    return {
        "step_id": step_id,
        "name": name,
        "steptype": 2,
        "auto_action": 21,
        "duration": duration,
        "isstart": start,
        "allow_all_statuses": True,
        "actions": [],
    }


def terminal(step_id: int, name: str, aa: int | None, *, end: bool = False) -> dict:
    return {
        "step_id": step_id,
        "name": name,
        "steptype": 3,
        **({"auto_action": aa} if aa is not None else {}),
        "isend": end,
        "islaststep": end,
        "allow_all_statuses": True,
        "actions": [],
    }


RECIPES: dict[str, list[dict]] = {
    "S1_sleep1_to_success": [
        {
            **sleep_step(1, "Wait a second", 1, start=True),
            "actions": [edge(32, "Sleep Finished", 1, 2, 1)],
        },
        terminal(2, "Success", None, end=True),
    ],
    "S2_sleep0_to_success": [
        {
            **sleep_step(1, "Instant hop", 0, start=True),
            "actions": [edge(32, "Sleep Finished", 1, 2, 1)],
        },
        terminal(2, "Success", None, end=True),
    ],
    "S3_api_fail_edge_traversal": [
        {
            "step_id": 1,
            "name": "Call nonexistent method",
            "steptype": 2,
            "auto_action": 6,
            "auto_action_type": 99999,
            "isstart": True,
            "allow_all_statuses": True,
            "actions": [
                edge(17, "Successful Response (200 - 299)", 1, 3, 1),
                edge(17, "Unsuccessful Response", 1, 2, 2),
            ],
        },
        terminal(2, "Fail", 1, end=True),
        terminal(3, "Success", None, end=True),
    ],
    "S4_condition_no_criteria": [
        {
            "step_id": 1,
            "name": "Always?",
            "steptype": 1,
            "auto_action": 6,
            "isstart": True,
            "allow_all_statuses": True,
            "actions": [
                edge(12, "Condition met", 1, 3, 1),
                edge(12, "Condition not met", 1, 2, 2),
            ],
        },
        terminal(2, "Not met", 1, end=True),
        terminal(3, "Success", None, end=True),
    ],
    "S5_double_sleep": [
        {
            **sleep_step(1, "Hop one", 0, start=True),
            "actions": [edge(32, "Sleep Finished", 1, 2, 1)],
        },
        {**sleep_step(2, "Hop two", 0), "actions": [edge(32, "Sleep Finished", 2, 3, 1)]},
        terminal(3, "Success", None, end=True),
    ],
}


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

    out: dict[str, Any] = {"tenant": host, "recipes": {}}
    async with HaloClient(profile, profile_name=args.profile) as client:

        async def runlog() -> list[dict]:
            body = await client.request("GET", "/Automation", params={"count": "30"}, timeout=45)
            rows = (
                body
                if isinstance(body, list)
                else next((v for v in body.values() if isinstance(v, list)), [])
            )
            return rows

        for label, steps in RECIPES.items():
            rec: dict[str, Any] = {"step_count": len(steps)}
            doc = {
                "name": f"{PROBE}-{label.lower()}",
                "type": 1,
                "active": True,
                "runbook_start_type": 1,
                "inbound_authentication_type": 0,
                "steps": steps,
            }
            try:
                resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                row = resp[0] if isinstance(resp, list) and resp else resp
                wid = row.get("id")
                rec["id"] = wid
            except Exception as exc:  # noqa: BLE001
                rec["create_error"] = str(exc)[:400]
                out["recipes"][label] = rec
                continue

            before = {r.get("id") for r in await runlog()}
            try:
                await client.request(
                    "POST",
                    f"/Automation/{wid}",
                    json_body={"formCollection": [{"Key": "probe", "Value": label}]},
                    timeout=90,
                )
                rec["fire"] = "ok"
            except Exception as exc:  # noqa: BLE001
                rec["fire"] = str(exc)[:250]

            rows: list[dict] = []
            for _ in range(8):
                await asyncio.sleep(2)
                rows = [
                    r
                    for r in await runlog()
                    if r.get("id") not in before and r.get("runbook_id") == wid
                ]
                if rows:
                    break
            if rows:
                r = rows[0]
                rec["runlog"] = {
                    k: r.get(k)
                    for k in r.keys()
                    if k
                    in (
                        "id",
                        "status",
                        "error",
                        "runbook_name",
                        "trace",
                        "workflow_id",
                        "step_id",
                        "guid",
                        "last_step",
                        "steps_completed",
                    )
                }
                rec["runlog_keys"] = sorted(r.keys())
            else:
                rec["runlog"] = "no new row"

            try:
                await client.request("DELETE", f"/Webhook/{wid}", timeout=30)
                rec["cleanup"] = "deleted"
            except Exception as exc:  # noqa: BLE001
                rec["cleanup"] = str(exc)[:160]
            out["recipes"][label] = rec

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
