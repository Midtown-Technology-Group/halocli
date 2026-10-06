#!/usr/bin/env python3
"""Two bounded primitive probes (trial, self-cleaning, crash-safe):

A) RUNBOOK CHAINING: aa24 (StartNewRunbookTerminateCurrentRunbook) has no
   default-edge catalog entry in the SPA - does it even need edges? Build
   target B (hop+Success), build A whose step1 = aa24 +
   start_new_runbook_id=B, fire A, observe: does B get a run? what is A's
   runlog? Cleanup sweeps by NAME in a finally (a transient httpx
   ReadError on the first attempt left leftovers - lesson applied).

B) CRITERIA29/30 ("Has a value"/"Does not have a value"): bare
   truthiness on a string param? Condition on <<label>> (type29) with
   then/else arms; fire with label "x" (expect met -> Success) and label
   "" (expect notmet -> Fail terminal).

    python scripts/runbook_chain_probe.py [--profile dev]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PROBE = "haloclidevprobechain2"


def edge(action_type: int, action_name: str, start: int, end: int, seq: int) -> dict:
    return {
        "action_type": action_type,
        "action_id": -action_type,
        "action_name": action_name,
        "start_step": start,
        "end_step": end,
        "seq": seq,
        "use_work_hours": True,
        "approval_result": 1 if seq == 1 else 0,
        "chat_selection_order": 1,
    }


def hop(step_id: int, name: str, next_id: int, *, start: bool = False) -> dict:
    return {
        "step_id": step_id,
        "name": name,
        "steptype": 2,
        "auto_action": 21,
        "duration": 0,
        "isstart": start,
        "allow_all_statuses": True,
        "actions": [edge(32, "Sleep Finished", step_id, next_id, 1)],
    }


def terminal(step_id: int, name: str, aa: int | None = None) -> dict:
    return {
        "step_id": step_id,
        "name": name,
        "steptype": 3,
        **({"auto_action": aa} if aa is not None else {}),
        "isend": True,
        "islaststep": True,
        "allow_all_statuses": True,
        "actions": [],
    }


def brief(r: dict) -> dict:
    return {
        k: r.get(k)
        for k in (
            "id",
            "runbook_id",
            "status",
            "error",
            "steps_executed",
            "runbook_step",
            "execution_time",
        )
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    parser.add_argument(
        "--save", default="", help="write the result JSON to this path (repo evidence)"
    )
    args = parser.parse_args()

    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: production host")

    out: dict[str, Any] = {"tenant": host}
    async with HaloClient(profile, profile_name=args.profile) as client:

        async def safe(factory: Callable[[], Awaitable[Any]], attempts: int = 3) -> Any:
            """Transient httpx.ReadError blips observed on the trial link."""
            last: Exception | None = None
            for i in range(attempts):
                try:
                    return await factory()
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    await asyncio.sleep(2 + i * 2)
            raise last  # type: ignore[misc]

        async def runlog_list() -> list[dict]:
            log = await safe(
                lambda: client.request("GET", "/Automation", params={"count": "1000"}, timeout=45)
            )
            return (
                log
                if isinstance(log, list)
                else next((v for v in log.values() if isinstance(v, list)), [])
            )

        async def find_runs(before: set, wids: set[str], ticks: int = 15) -> list[dict]:
            rows: list[dict] = []
            for _ in range(ticks):
                await asyncio.sleep(3)
                try:
                    rows = await runlog_list()
                except Exception:  # noqa: BLE001
                    continue
                hits = [
                    r for r in rows if r.get("id") not in before and r.get("runbook_id") in wids
                ]
                if hits:
                    return hits
            top = max((r.get("id") or 0) for r in rows) if rows else 0
            for cand in range(top, max(top - 8, 0), -1):
                try:
                    d2 = await client.request("GET", f"/Automation/{cand}", timeout=20)
                except Exception:  # noqa: BLE001
                    continue
                if (
                    isinstance(d2, dict)
                    and d2.get("runbook_id") in wids
                    and d2.get("id") not in before
                ):
                    return [d2]
            return []

        async def sweep_cleanup() -> list[str]:
            done = []
            try:
                body = await safe(
                    lambda: client.request(
                        "GET",
                        "/Webhook",
                        params={"showall": "true", "type": "1", "count": "200"},
                        timeout=45,
                    )
                )
                rows = (
                    body
                    if isinstance(body, list)
                    else next((v for v in body.values() if isinstance(v, list)), [])
                )
                for r in rows:
                    if PROBE in str(r.get("name", "")):
                        try:
                            await client.request("DELETE", f"/Webhook/{r.get('id')}", timeout=30)
                            done.append(str(r.get("name")))
                        except Exception as exc:  # noqa: BLE001
                            done.append(f"{r.get('name')}: {str(exc)[:80]}")
            except Exception as exc:  # noqa: BLE001
                done.append(f"sweep failed: {str(exc)[:120]}")
            return done

        try:
            # ---------- A: chaining --------------------------------------
            resp_b = await client.request(
                "POST",
                "/Webhook",
                json_body=[
                    {
                        "name": f"{PROBE}-target",
                        "type": 1,
                        "active": True,
                        "runbook_start_type": 0,
                        "steps": [hop(1, "TargetHop", 2, start=True), terminal(2, "Success")],
                    }
                ],
                timeout=60,
            )
            rb = resp_b[0] if isinstance(resp_b, list) else resp_b
            b_id = rb.get("id")
            resp_a = await client.request(
                "POST",
                "/Webhook",
                json_body=[
                    {
                        "name": f"{PROBE}-chain",
                        "type": 1,
                        "active": True,
                        # public-fireable: start_type0 correctly answers 401
                        # to a public POST (Halo-only enforcement - observed)
                        "runbook_start_type": 1,
                        "inbound_authentication_type": 0,
                        "steps": [
                            {
                                "step_id": 1,
                                "name": "Chain to target",
                                "steptype": 2,
                                "auto_action": 24,
                                "start_new_runbook_id": b_id,
                                "isstart": True,
                                "allow_all_statuses": True,
                                "actions": [],
                            }
                        ],
                    }
                ],
                timeout=60,
            )
            ra = resp_a[0] if isinstance(resp_a, list) else resp_a
            a_id = ra.get("id")
            out["chain"] = {"target": b_id, "source": a_id}

            before = {r.get("id") for r in await runlog_list()}
            try:
                await client.request(
                    "POST",
                    f"/Automation/{a_id}",
                    json_body={"formCollection": []},
                    timeout=90,
                )
                out["chain_fire"] = "ok"
            except Exception as exc:  # noqa: BLE001
                out["chain_fire"] = str(exc)[:250]
            runs = await find_runs(before, {a_id, b_id})
            out["chain_runs"] = [brief(r) for r in runs]
            out["target_ran"] = any(r.get("runbook_id") == b_id for r in runs)

            # ---------- B: criteria29/30 ---------------------------------
            crit = {
                "id": None,
                "rule_id": 0,
                "qualification_criteria_id": 0,
                "fieldname": "<<label>>",
                "value_type": "string",
                "value_type_id": -1,
                "value_int": 0,
                "value_string": "",
                "partialmatch": False,
                "matchseparatedvalues": False,
                "tablename": "runbookvariable",
                "type": 29,
                "flowsubdetails_criteria_id": 0,
                "use": 0,
                "chatprofile_id": None,
                "chatprofile_flow_seq": 1,
                "timezonestring": "",
                "match_after_start": False,
                "match_after_target": False,
                "eventrule_id": 0,
                "flow_id": 0,
                "flow_type": 0,
                "flow_seq": 0,
            }
            resp = await client.request(
                "POST",
                "/Webhook",
                json_body=[
                    {
                        "name": f"{PROBE}-value",
                        "type": 1,
                        "active": True,
                        "runbook_start_type": 1,
                        "inbound_authentication_type": 0,
                        "input_variables": [
                            {
                                "id": None,
                                "key": "label",
                                "value": "x",
                                "data_type": 2,
                                "description": "guard target",
                            },
                        ],
                        "steps": [
                            {
                                "step_id": 1,
                                "name": "if label:",
                                "steptype": 1,
                                "auto_action": 6,
                                "isstart": True,
                                "allow_all_statuses": True,
                                "step_conditions": [crit],
                                "actions": [
                                    edge(12, "Condition met", 1, 3, 1),
                                    edge(12, "Condition not met", 1, 4, 2),
                                ],
                            },
                            hop(2, "Unreached", 3),
                            terminal(3, "Success"),
                            {
                                "step_id": 4,
                                "name": "NoValue",
                                "steptype": 3,
                                "auto_action": 1,
                                "isend": True,
                                "allow_all_statuses": True,
                                "actions": [],
                            },
                        ],
                    }
                ],
                timeout=60,
            )
            rv = resp[0] if isinstance(resp, list) else resp
            v_id = rv.get("id")
            out["value_runbook"] = v_id

            legs: dict[str, Any] = {}
            for label_value, expect in (("x", "met"), ("", "notmet")):
                doc = await client.request(
                    "GET",
                    f"/Webhook/{v_id}",
                    params={"includedetails": "true"},
                    timeout=45,
                )
                ivars = []
                for v in doc.get("input_variables") or []:
                    v = dict(v)
                    if v.get("key") == "label":
                        v["value"] = label_value
                    ivars.append(v)
                upd = {
                    **doc,
                    "name": f"{PROBE}-value",
                    "type": 1,
                    "active": True,
                    "runbook_start_type": 1,
                    "inbound_authentication_type": 0,
                    "input_variables": ivars,
                }
                try:
                    await client.request("POST", "/Webhook", json_body=[upd], timeout=60)
                except Exception as exc:  # noqa: BLE001
                    legs[label_value] = {"update_error": str(exc)[:250]}
                    continue
                before2 = {r.get("id") for r in await runlog_list()}
                try:
                    await client.request(
                        "POST",
                        f"/Automation/{v_id}",
                        json_body={"formCollection": []},
                        timeout=90,
                    )
                except Exception as exc:  # noqa: BLE001
                    legs[label_value] = {"fire_error": str(exc)[:250]}
                    continue
                hits = await find_runs(before2, {v_id})
                leg: dict[str, Any] = {"expect": expect, "runs": [brief(r) for r in hits]}
                if hits:
                    leg["status"] = hits[0].get("status")
                    leg["ended_at_step"] = hits[0].get("runbook_step")
                legs[label_value] = leg
            out["value_legs"] = legs
        finally:
            out["cleanup"] = await sweep_cleanup()

    payload = json.dumps(out, indent=2, default=str)
    if args.save:
        Path(args.save).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
