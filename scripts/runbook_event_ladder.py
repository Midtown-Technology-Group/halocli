#!/usr/bin/env python3
"""Which Halo events fire for API-made changes? (the internal-trigger question)

Request-budget rewrite after a rate-limit crash: results persist to disk
AFTER EVERY STAGE, list-poll per tick, ONE bounded by-id sweep at the end.

Ladder (each stage self-cleaning for its own artifacts; existing runbooks
only READ):
L1 minimal binding (fresh guid, eventno3) on our probe runbook + ticket1
   -> poll ours + AI Auto Triage (its real eventno3 bindings) + NotificationLog.
L2 if silent: copy Auto Triage's own eventno3 row (guid NULLED - template
   guids upsert-and-hijack; learned the hard way) + ticket2 -> poll ours.
L3 action-add on ticket1 -> poll AI Agent Closure / SoW (eventno1004).

    python scripts/runbook_event_ladder.py [--profile dev]
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

PROBE = "haloclidevprobeladder"
DUMP = Path(r"C:\Users\ThomasBray\AppData\Local\Temp\opencode\working_runbooks.json")
RESULT = Path(r"C:\Users\ThomasBray\AppData\Local\Temp\opencode\event_ladder_result.json")


def save(out: dict[str, Any]) -> None:
    RESULT.write_text(json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")


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

    dump = json.loads(DUMP.read_text(encoding="utf-8"))
    by_name = {rb["name"]: rb["id"] for rb in dump["runbooks"]}
    autotriage = by_name.get("AI Auto Triage")
    closure = by_name.get("AI Agent Closure Review")

    out: dict[str, Any] = {"tenant": host, "stages": {}}
    save(out)
    tickets: list[int] = []
    bindings: list[int] = []
    our_runbook: str | None = None

    async with HaloClient(profile, profile_name=args.profile) as client:

        async def runlog_list() -> list[dict]:
            log = await client.request("GET", "/Automation", params={"count": "60"}, timeout=45)
            return (
                log
                if isinstance(log, list)
                else next((v for v in log.values() if isinstance(v, list)), [])
            )

        async def poll_runs(before: set, wids: set[str], ticks: int = 10) -> list[dict]:
            """List-poll per tick; ONE bounded by-id sweep at the end."""
            hits: list[dict] = []
            rows: list[dict] = []
            for _ in range(ticks):
                await asyncio.sleep(3)
                rows = await runlog_list()
                hits = [
                    r for r in rows if r.get("id") not in before and r.get("runbook_id") in wids
                ]
                if hits:
                    return hits
            # single bounded by-id sweep over the newest ids
            top = max((r.get("id") or 0 for r in rows), default=0)
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
            return hits

        async def make_ticket(summary: str) -> int | None:
            try:
                r = await client.request(
                    "POST", "/Tickets", json_body=[{"summary": summary}], timeout=60
                )
                row = r[0] if isinstance(r, list) and r else r
                tid = row.get("id") if isinstance(row, dict) else None
                if tid:
                    tickets.append(tid)
                return tid
            except Exception as exc:  # noqa: BLE001
                out.setdefault("errors", []).append(f"ticket: {str(exc)[:200]}")
                save(out)
                return None

        async def notif_log() -> list[dict]:
            try:
                b = await client.request(
                    "GET", "/NotificationLog", params={"count": "5"}, timeout=45
                )
                rows = (
                    b
                    if isinstance(b, list)
                    else next((v for v in b.values() if isinstance(v, list)), [])
                )
                return [
                    {k: n.get(k) for k in ("id", "eventno", "notification_id", "status", "error")}
                    for n in rows
                ]
            except Exception as exc:  # noqa: BLE001
                return [{"error": str(exc)[:140]}]

        base = await runlog_list()
        before = {r.get("id") for r in base}

        # ---- probe runbook + L1 minimal binding --------------------------
        try:
            resp = await client.request(
                "POST",
                "/Webhook",
                json_body=[
                    {
                        "name": f"{PROBE}-runbook",
                        "type": 1,
                        "active": True,
                        "runbook_start_type": 0,
                        "steps": [
                            {
                                "step_id": 1,
                                "name": "Hop",
                                "steptype": 2,
                                "auto_action": 21,
                                "duration": 0,
                                "isstart": True,
                                "allow_all_statuses": True,
                                "actions": [
                                    {
                                        "action_type": 32,
                                        "action_id": -32,
                                        "action_name": "Sleep Finished",
                                        "start_step": 1,
                                        "end_step": 2,
                                        "seq": 1,
                                        "use_work_hours": True,
                                        "approval_result": 1,
                                        "chat_selection_order": 1,
                                    }
                                ],
                            },
                            {
                                "step_id": 2,
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
            our_runbook = row.get("id")
            out["runbook"] = our_runbook
        except Exception as exc:  # noqa: BLE001
            out["runbook"] = {"error": str(exc)[:300]}
            save(out)
            print(json.dumps(out, indent=2, default=str))
            return 0
        try:
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
                        "webhook_id": our_runbook,
                    }
                ],
                timeout=60,
            )
            nrow = r[0] if isinstance(r, list) and r else r
            bindings.append(nrow.get("id"))
            out["L1_binding"] = {"id": nrow.get("id")}
        except Exception as exc:  # noqa: BLE001
            out["L1_binding"] = {"error": str(exc)[:300]}
        save(out)

        # ---- L1 ------------------------------------------------------------
        tid1 = await make_ticket(f"{PROBE} ladder1")
        wids_l1 = {w for w in (our_runbook, autotriage) if w}
        hits1 = await poll_runs(before, wids_l1)
        out["L1"] = {
            "ticket": tid1,
            "runs": [
                {k: h.get(k) for k in ("id", "runbook_id", "status", "steps_executed")}
                for h in hits1
            ],
            "notiflog": await notif_log(),
        }
        save(out)

        # ---- L2 (only if L1 silent) ----------------------------------------
        if not hits1 and our_runbook:
            try:
                adoc = await client.request(
                    "GET",
                    f"/Webhook/{autotriage}",
                    params={"includedetails": "true"},
                    timeout=45,
                )
                arow = next((e for e in adoc.get("events") or [] if e.get("eventno") == 3), None)
                if arow:
                    n2 = {k: v for k, v in arow.items()}
                    n2.update({"id": None, "guid": None, "webhook_id": our_runbook})
                    r = await client.request("POST", "/Notification", json_body=[n2], timeout=60)
                    nrow = r[0] if isinstance(r, list) and r else r
                    bindings.append(nrow.get("id"))
                    out["L2_binding"] = {"id": nrow.get("id"), "copied_from": arow.get("id")}
            except Exception as exc:  # noqa: BLE001
                out["L2_binding"] = {"error": str(exc)[:300]}
            save(out)
            tid2 = await make_ticket(f"{PROBE} ladder2")
            if tid2:
                before.add(tid2)
            hits2 = await poll_runs(before, {our_runbook})
            out["L2"] = {
                "ticket": tid2,
                "runs": [
                    {k: h.get(k) for k in ("id", "runbook_id", "status", "steps_executed")}
                    for h in hits2
                ],
                "notiflog": await notif_log(),
            }
            save(out)

        # ---- L3: action-add (eventno1004) ---------------------------------
        if tickets:
            try:
                r = await client.request(
                    "POST",
                    "/Actions",
                    json_body=[{"ticket_id": tickets[0], "note": f"{PROBE} action"}],
                    timeout=60,
                )
                arow = r[0] if isinstance(r, list) and r else r
                out["L3_action"] = {"id": arow.get("id") if isinstance(arow, dict) else None}
            except Exception as exc:  # noqa: BLE001
                out["L3_action"] = {"error": str(exc)[:300]}
            save(out)
            hits3 = await poll_runs(before, {w for w in (closure,) if w})
            out["L3"] = {
                "runs": [
                    {k: h.get(k) for k in ("id", "runbook_id", "status", "steps_executed")}
                    for h in hits3
                ],
                "notiflog": await notif_log(),
            }
            save(out)

        # ---- cleanup ----------------------------------------------------------
        cleanup = []
        for bid in bindings:
            try:
                await client.request("DELETE", f"/Notification/{bid}", timeout=30)
                cleanup.append(f"binding {bid} deleted")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"binding {bid}: {str(exc)[:100]}")
        for tid in tickets:
            try:
                await client.request("DELETE", f"/Tickets/{tid}", timeout=30)
                cleanup.append(f"ticket {tid} deleted")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"ticket {tid}: {str(exc)[:100]}")
        if our_runbook:
            try:
                await client.request("DELETE", f"/Webhook/{our_runbook}", timeout=30)
                cleanup.append("runbook deleted")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"runbook: {str(exc)[:100]}")
        out["cleanup"] = cleanup
        save(out)

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
