#!/usr/bin/env python3
"""Live proof: --ticket-guard faults-table criteria in TICKET CONTEXT.

Runbook: [guard(if ticket.reportedby != "noreply@...": faults table,
notmet exits to Success) -> hop -> hop -> Success] bound to eventno3
(New Ticket Logged) via POST /Notification. Two probe tickets:
  A reportedby "someone@example.com" -> guard MET -> full path (exec3)
  B reportedby "noreply@voicemail.goto.com" -> guard NOTMET -> early
    exit (exec1, status2 - early-return = benign no-op complete)
Manual fires are impossible here: faults criteria + context need the
event's ticket (the note-step401 lesson). Self-cleaning + crash-safe
(name sweep in finally, bounded retries).

    python scripts/runbook_ticket_guard_probe.py [--profile dev] [--save PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable


def _safe_path(p: str | Path) -> Path:
    """Canonicalize a CLI-supplied path before touching the disk (S8707)."""
    return Path(p).expanduser().resolve()


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PROBE = "haloclidevprobeguard"
SENDER = "noreply@voicemail.goto.com"


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


def hop(step_id: int, next_id: int, *, start: bool = False) -> dict:
    return {
        "step_id": step_id,
        "name": f"work-{step_id}",
        "steptype": 2,
        "auto_action": 21,
        "duration": 0,
        "isstart": start,
        "allow_all_statuses": True,
        "actions": [edge(32, "Sleep Finished", step_id, next_id, 1)],
    }


def brief(r: dict) -> dict:
    return {
        k: r.get(k)
        for k in ("id", "status", "error", "steps_executed", "runbook_step", "execution_time")
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--save", default="", help="write result JSON here (evidence)")
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

        async def wait_run(before: set, wid: str, ticks: int = 15) -> dict | None:
            """Poll until a NEW run of wid appears; follow to terminal."""
            rows: list[dict] = []
            hit: dict | None = None
            rid = None
            for _ in range(ticks):
                await asyncio.sleep(3)
                if rid is None:
                    try:
                        rows = await runlog_list()
                    except Exception:  # noqa: BLE001
                        continue
                    mine = [
                        r for r in rows if r.get("id") not in before and r.get("runbook_id") == wid
                    ]
                    if mine:
                        rid = mine[0].get("id")
                    else:
                        top = max((r.get("id") or 0) for r in rows) if rows else 0
                        for cand in range(top, max(top - 8, 0), -1):
                            try:
                                d2 = await client.request("GET", f"/Automation/{cand}", timeout=20)
                            except Exception:  # noqa: BLE001
                                continue
                            if (
                                isinstance(d2, dict)
                                and d2.get("runbook_id") == wid
                                and d2.get("id") not in before
                            ):
                                rid = d2.get("id")
                                break
                if rid is None:
                    continue
                try:
                    snap = await client.request("GET", f"/Automation/{rid}", timeout=20)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(snap, dict):
                    hit = snap
                    if (snap.get("status") or 1) != 1 or (snap.get("error") or ""):
                        break
            return hit

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
                            done.append(f"runbook {r.get('name')}")
                        except Exception as exc:  # noqa: BLE001
                            done.append(f"{r.get('name')}: {str(exc)[:80]}")
            except Exception as exc:  # noqa: BLE001
                done.append(f"sweep failed: {str(exc)[:120]}")
            return done

        tickets: list[int] = []
        bindings: list[int] = []
        try:
            # ---- runbook: guard(early-exit) -> two hops -> Success ------
            resp = await client.request(
                "POST",
                "/Webhook",
                json_body=[
                    {
                        "name": f"{PROBE}-runbook",
                        "type": 1,
                        "active": True,
                        "runbook_start_type": 1,
                        "inbound_authentication_type": 0,
                        "steps": [
                            {
                                "step_id": 1,
                                "name": f"if ticket.reportedby != '{SENDER}':",
                                "steptype": 1,
                                "auto_action": 6,
                                "isstart": True,
                                "allow_all_statuses": True,
                                "step_conditions": [
                                    {
                                        "id": None,
                                        "rule_id": 0,
                                        "qualification_criteria_id": 0,
                                        "fieldname": "reportedby",
                                        "value_type": "string",
                                        "value_type_id": -1,
                                        "value_int": 0,
                                        "value_string": SENDER,
                                        "partialmatch": False,
                                        "matchseparatedvalues": False,
                                        "tablename": "faults",
                                        "type": 1,  # Is not equal to
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
                                ],
                                "actions": [
                                    edge(12, "Condition met", 1, 2, 1),
                                    edge(12, "Condition not met", 1, 4, 2),
                                ],
                            },
                            hop(2, 3),
                            hop(3, 4),
                            {
                                "step_id": 4,
                                "name": "Success",
                                "steptype": 3,
                                "isend": True,
                                "islaststep": True,
                                "allow_all_statuses": True,
                                "actions": [],
                            },
                        ],
                    }
                ],
                timeout=60,
            )
            row = resp[0] if isinstance(resp, list) else resp
            wid = str(row.get("id"))
            out["runbook"] = wid

            # ---- bind eventno3 ------------------------------------------
            r = await client.request(
                "POST",
                "/Notification",
                json_body=[
                    {
                        "guid": None,
                        "eventno": 3,
                        "name": "New Ticket Logged",
                        "type": -2,
                        "delivery_method": 6,
                        "agent_id": 0,
                        "webhook_id": wid,
                    }
                ],
                timeout=60,
            )
            nrow = r[0] if isinstance(r, list) and r else r
            bindings.append(nrow.get("id"))
            out["binding"] = {"id": nrow.get("id")}

            # ---- ticket A: sender != vendor -> guard MET (full path) ----
            before_a = {x.get("id") for x in await runlog_list()}
            ta = await client.request(
                "POST",
                "/Tickets",
                json_body=[{"summary": f"{PROBE} met leg", "reportedby": "someone@example.com"}],
                timeout=60,
            )
            trow = ta[0] if isinstance(ta, list) else ta
            if trow.get("id"):
                tickets.append(trow.get("id"))
            run_a = await wait_run(before_a, wid)
            out["met_leg"] = {
                "ticket": trow.get("id"),
                "expect": "exec3 (guard + two hops)",
                "run": brief(run_a) if run_a else "no run observed",
            }

            # ---- ticket B: vendor sender -> guard NOTMET (early exit) --
            before_b = {x.get("id") for x in await runlog_list()}
            tb = await client.request(
                "POST",
                "/Tickets",
                json_body=[{"summary": f"{PROBE} notmet leg", "reportedby": SENDER}],
                timeout=60,
            )
            trowb = tb[0] if isinstance(tb, list) else tb
            if trowb.get("id"):
                tickets.append(trowb.get("id"))
            run_b = await wait_run(before_b, wid)
            out["notmet_leg"] = {
                "ticket": trowb.get("id"),
                "expect": "exec1 (guard jumps straight to Success)",
                "run": brief(run_b) if run_b else "no run observed",
            }

            # ---- verdict --------------------------------------------------
            a_ok = isinstance(run_a, dict) and (run_a.get("status") or 0) == 2
            b_ok = isinstance(run_b, dict) and (run_b.get("status") or 0) == 2
            a_exec = run_a.get("steps_executed") if isinstance(run_a, dict) else None
            b_exec = run_b.get("steps_executed") if isinstance(run_b, dict) else None
            out["both_status2"] = bool(a_ok and b_ok)
            out["paths_differ"] = bool(
                isinstance(a_exec, int) and isinstance(b_exec, int) and a_exec > b_exec
            )
            out["verdict"] = (
                "PROVEN: met path ran the work hops, notmet exited early"
                if out["both_status2"] and out["paths_differ"]
                else "INCONCLUSIVE - inspect runs"
            )
        finally:
            for bid in [b for b in bindings if b]:
                try:
                    await client.request("DELETE", f"/Notification/{bid}", timeout=30)
                except Exception:  # noqa: BLE001
                    out.setdefault("cleanup_errors", []).append(f"binding {bid}")
            for tid in tickets:
                try:
                    await client.request("DELETE", f"/Tickets/{tid}", timeout=30)
                except Exception:  # noqa: BLE001
                    out.setdefault("cleanup_errors", []).append(f"ticket {tid}")
            out["cleanup"] = await sweep_cleanup()

    payload = json.dumps(out, indent=2, default=str)
    if args.save:
        _safe_path(args.save).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
