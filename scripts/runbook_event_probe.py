#!/usr/bin/env python3
"""events[] acceptability + the full internal-trigger proof.

A) POST a probe runbook with events[] copied from a working template
   (ids nulled like the import sanitize) -> GET -> persisted?
B) THE PROOF: bind eventno3 "New Ticket Logged" (the event Bifrost's
   Halo source subscriber filters on) to a fresh probe runbook -> create
   ONE probe ticket on the trial -> poll the runlog for a run with our
   runbook_id -> delete runbook + ticket (self-cleaning).

    python scripts/runbook_event_probe.py [--profile dev]
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

PROBE = "haloclidevprobeevent"


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
        # ---------- A: acceptability (template event, ids nulled) --------
        # grab a template event from a working runbook (NinjaOne's "Closed")
        body = await client.request(
            "GET", "/Webhook", params={"showall": "true", "type": "1", "count": "50"}, timeout=45
        )
        rows = (
            body
            if isinstance(body, list)
            else next((v for v in body.values() if isinstance(v, list)), [])
        )
        template_event = None
        for r in rows:
            try:
                doc = await client.request(
                    "GET",
                    f"/Webhook/{r.get('id')}",
                    params={"includedetails": "true"},
                    timeout=45,
                )
            except Exception:  # noqa: BLE001
                continue
            evs = doc.get("events") or []
            if any(e.get("eventno") == 39 for e in evs):
                template_event = next(e for e in evs if e.get("eventno") == 39)
                break
        out["template_event"] = (
            {k: template_event.get(k) for k in ("id", "eventno", "name", "type", "delivery_method")}
            if template_event
            else None
        )

        new_ticket_event = {
            "id": None,
            "eventno": 3,
            "name": "New Ticket Logged",
            "type": -2,
            "delivery_method": 6,
            "agent_id": 0,
        }
        if template_event:
            # carry every template field, override id/eventno/name
            merged = dict(template_event)
            merged.update(new_ticket_event)
            new_ticket_event = merged

        base_doc = {
            "name": f"{PROBE}-triggered",
            "type": 1,
            "active": True,
            "runbook_start_type": 0,  # Halo-only: fires from Halo events
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
                        },
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
        try:
            resp = await client.request("POST", "/Webhook", json_body=[base_doc], timeout=60)
            row = resp[0] if isinstance(resp, list) else resp
            wid = row.get("id")
            out["create"] = {"id": wid}
        except Exception as exc:  # noqa: BLE001
            out["create"] = {"error": str(exc)[:400]}
            print(json.dumps(out, indent=2, default=str))
            return 0

        # events[] on the create is a READ-JOINED VIEW (dropped on POST) -
        # bindings are Notification rows carrying webhook_id (SPA:
        # V_ = {endpoint:"Notification"}, showNotificationDetails passes
        # webhook_id). Bind via POST /Notification with the template row.
        binding_id = None
        if template_event and wid:
            notif = dict(template_event)
            notif.update({"id": None, "eventno": 3, "name": "New Ticket Logged", "webhook_id": wid})
            try:
                nresp = await client.request("POST", "/Notification", json_body=[notif], timeout=60)
                nrow = nresp[0] if isinstance(nresp, list) and nresp else nresp
                binding_id = nrow.get("id") if isinstance(nrow, dict) else None
                out["binding"] = {"id": binding_id}
            except Exception as exc:  # noqa: BLE001
                out["binding"] = {"error": str(exc)[:400]}

        full = await client.request(
            "GET", f"/Webhook/{wid}", params={"includedetails": "true"}, timeout=45
        )
        out["events_persisted"] = [
            {k: e.get(k) for k in ("id", "eventno", "name", "type", "delivery_method")}
            for e in full.get("events") or []
        ]

        # ---------- B: fire it by creating a probe ticket -----------------
        before_log = await client.request("GET", "/Automation", params={"count": "40"}, timeout=45)
        brows = (
            before_log
            if isinstance(before_log, list)
            else next((v for v in before_log.values() if isinstance(v, list)), [])
        )
        before_ids = {r.get("id") for r in brows}
        ticket_id = None
        try:
            tresp = await client.request(
                "POST",
                "/Tickets",
                json_body=[{"summary": f"{PROBE} trigger ticket"}],
                timeout=60,
            )
            trow = tresp[0] if isinstance(tresp, list) and tresp else tresp
            ticket_id = trow.get("id") if isinstance(trow, dict) else None
            out["ticket"] = {"id": ticket_id}
        except Exception as exc:  # noqa: BLE001
            out["ticket"] = {"error": str(exc)[:400]}

        # poll for a run of OUR runbook (event-driven runs may take a moment)
        found = None
        rid = None
        for _ in range(15):
            await asyncio.sleep(2)
            if rid is None:
                log = await client.request("GET", "/Automation", params={"count": "60"}, timeout=45)
                rows2 = (
                    log
                    if isinstance(log, list)
                    else next((v for v in log.values() if isinstance(v, list)), [])
                )
                mine = [
                    r for r in rows2 if r.get("id") not in before_ids and r.get("runbook_id") == wid
                ]
                if mine:
                    rid = mine[0].get("id")
                else:
                    top = max((r.get("id") or 0 for r in rows2), default=0)
                    for cand in range(top, max(top - 40, 0), -1):
                        try:
                            d2 = await client.request("GET", f"/Automation/{cand}", timeout=20)
                        except Exception:  # noqa: BLE001
                            continue
                        if isinstance(d2, dict) and d2.get("runbook_id") == wid:
                            rid = d2.get("id")
                            break
            if rid is None:
                continue
            found = await client.request("GET", f"/Automation/{rid}", timeout=20)
            if (found.get("status") or 1) != 1 or (found.get("error") or ""):
                break
        out["runlog"] = (
            {
                k: found.get(k)
                for k in (
                    "id",
                    "status",
                    "error",
                    "steps_executed",
                    "runbook_step",
                    "execution_time",
                )
            }
            if isinstance(found, dict)
            else "no event-driven run observed"
        )

        # ---------- cleanup ---------------------------------------------
        cleanup = []
        if binding_id is not None:
            try:
                await client.request("DELETE", f"/Notification/{binding_id}", timeout=30)
                cleanup.append("notification binding deleted")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"notification DELETE failed {str(exc)[:140]}")
        try:
            await client.request("DELETE", f"/Webhook/{wid}", timeout=30)
            cleanup.append("runbook deleted")
        except Exception as exc:  # noqa: BLE001
            cleanup.append(f"runbook DELETE failed {str(exc)[:140]}")
        if ticket_id is not None:
            try:
                await client.request("DELETE", f"/Tickets/{ticket_id}", timeout=30)
                cleanup.append("ticket deleted")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"ticket DELETE failed {str(exc)[:140]}")
        out["cleanup"] = cleanup

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
