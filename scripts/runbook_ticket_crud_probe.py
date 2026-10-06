#!/usr/bin/env python3
"""Ticket-write probe: Halo API Action variants aat1 (create) / aat2 (update).

The converter's only proven write binding is halo_note (aa8 + aat3 add
note), and it REQUIRES ticket context when the message interpolates
<<ticket^id>> (trigger path proven, runlog2552; formCollection fires
died at the note step). This probe pins the rest of the message-
template catalog with a control:

  aat3_lit   control: add-note with a LITERAL ticket id (no <<vars>>)
             on a plain formCollection fire - does the note land?
  aat1       create a ticket (raw POST /Tickets body, marker subject)
             - discoverable afterwards via the ticket list?
  aat2       update a probe ticket ({"id": <literal>, "summary": ...})
             - read back and compare?
  aat2_bad   negative: update a nonexistent id - the Unsuccessful
             edge must take the run off the Success path

Every runbook and every probe ticket is deleted in finally; evidence
lands in ticket_crud_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import probe_harness as ph

AAT3_MARKER = "halocli-crud-probe note marker"
AAT1_MARKER = "halocli-crud-probe created-by-aat1"
AAT2_MARKER = "halocli-crud-probe updated-by-aat2"


def aa8_runbook(name: str, aat: int, message: str) -> dict:
    """One-step Halo API Action runbook (aa8 + aat, act18 edge pair)."""

    def edge(action_name: str, end: int, seq: int) -> dict:
        return {
            "action_type": 18,
            "action_id": -18,
            "action_name": action_name,
            "start_step": 1,
            "end_step": end,
            "seq": seq,
            "use_work_hours": True,
            "approval_result": 1 if seq == 1 else 0,
            "chat_selection_order": 1,
        }

    return {
        "name": f"{ph.bc.PROBE}-{name}"[:80],
        "type": 1,
        "active": True,
        "runbook_start_type": 1,
        "inbound_authentication_type": 0,
        "input_variables": [],
        "steps": [
            {
                "step_id": 1,
                "name": "api-action",
                "steptype": 2,
                "auto_action": 8,
                "auto_action_type": aat,
                "isstart": True,
                "allow_all_statuses": True,
                "message": message,
                "actions": [edge("Successful", 2, 1), edge("Unsuccessful", 3, 2)],
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
            {
                "step_id": 3,
                "name": "Fail",
                "steptype": 3,
                "auto_action": 1,
                "isend": True,
                "allow_all_statuses": True,
                "actions": [],
            },
        ],
    }


async def create_ticket(client: Any, summary: str) -> str | None:
    ta = await client.request(
        "POST",
        "/Tickets",
        json_body=[{"summary": summary, "reportedby": "probe@example.com"}],
        timeout=60,
    )
    trow = ta[0] if isinstance(ta, list) else ta
    return str(trow.get("id")) if isinstance(trow, dict) and trow.get("id") else None


async def ticket_doc(client: Any, tid: str) -> dict:
    doc = await client.request(
        "GET", f"/Tickets/{tid}", params={"includedetails": "true"}, timeout=45
    )
    return doc if isinstance(doc, dict) else {}


async def ticket_id_set(client: Any) -> set[str]:
    rows = await client.request("GET", "/Tickets", params={"count": "1000"}, timeout=60)
    if isinstance(rows, dict):
        rows = next((v for v in rows.values() if isinstance(v, list)), [])
    if not isinstance(rows, list):
        return set()
    return {str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")}


async def find_by_summary(client: Any, marker: str) -> str | None:
    rows = await client.request("GET", "/Tickets", params={"count": "1000"}, timeout=60)
    if isinstance(rows, dict):
        rows = next((v for v in rows.values() if isinstance(v, list)), [])
    if not isinstance(rows, list):
        return None
    for r in rows:
        if marker in str(r.get("summary") or ""):
            return str(r.get("id"))
    return None


def verdict(ev: dict[str, Any]) -> str:
    legs = ev["legs"]
    findings: list[str] = []
    if legs["aat3_lit"].get("action_marker_found") or legs["aat3_lit"].get("marker_found"):
        findings.append("SHIP: aat3 note with literal id lands without ticket context")
    else:
        findings.append("aat3 literal-id note NOT found (context still required)")
    if legs["aat1_create"].get("created_id"):
        findings.append("SHIP: aat1 create step made a discoverable ticket")
    else:
        findings.append("aat1 create NOT observed")
    if legs["aat2_update"].get("updated"):
        findings.append("SHIP: aat2 update step changed the target ticket")
    else:
        findings.append("aat2 update NOT observed")
    if legs["aat2_bad"].get("took_failure_path"):
        findings.append("bad id routes the Unsuccessful edge off the Success path")
    else:
        findings.append("bad id did NOT route to failure - inspect runlog")
    if not findings:
        findings.append("NO WRITE PRIMITIVE PROVEN")
    return " | ".join(findings)


async def main() -> int:
    profile = ph.load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {"tenant": host, "question": "aat1/aat2/aat3-literal", "legs": {}}
    created_runbooks: list[str] = []
    created_tickets: list[str] = []
    try:
        async with ph.HaloClient(profile, profile_name="dev") as client:
            try:
                # target tickets for aat3-literal / aat2
                t_note = await create_ticket(client, "halocli-crud-probe note target")
                t_upd = await create_ticket(client, "halocli-crud-probe update target")
                for tid in (t_note, t_upd):
                    if tid:
                        created_tickets.append(tid)
                pre_ids = await ticket_id_set(client)

                legs: dict[str, tuple[int, str]] = {
                    "aat3_lit": (
                        3,
                        json.dumps(
                            {
                                "ticket_id": int(t_note) if t_note else 0,
                                "outcome": "Internal Note",
                                "who": "Automation",
                                "hiddenfromuser": True,
                                "note_html": AAT3_MARKER,
                            }
                        ),
                    ),
                    "aat1_create": (
                        1,
                        json.dumps({"summary": AAT1_MARKER, "reportedby": "probe@example.com"}),
                    ),
                    "aat2_update": (
                        2,
                        json.dumps({"id": int(t_upd) if t_upd else 0, "summary": AAT2_MARKER}),
                    ),
                    "aat2_bad": (2, json.dumps({"id": 99999999, "summary": "nope"})),
                }

                for leg, (aat, message) in legs.items():
                    doc = aa8_runbook(f"crud-{leg}", aat, message)
                    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created_runbooks.append(wid)
                    fire = await ph.bc._fire_runbook(client, wid, {})
                    ev["legs"][leg] = {
                        "id": wid,
                        "aat": aat,
                        "runlog": fire.get("runlog"),
                        "evolution": fire.get("evolution"),
                        "trigger_fire": fire.get("trigger_fire"),
                    }

                # read-backs
                if t_note:
                    doc = await ticket_doc(client, t_note)
                    ev["legs"]["aat3_lit"]["marker_found"] = AAT3_MARKER in json.dumps(doc)
                    # notes land as Action rows keyed by ticket_id (the
                    # CLI's proven readback: GET /Actions?ticket_id=)
                    actions = await client.request(
                        "GET",
                        "/Actions",
                        params={"ticket_id": str(t_note), "count": "50"},
                        timeout=45,
                    )
                    ev["legs"]["aat3_lit"]["action_marker_found"] = AAT3_MARKER in json.dumps(
                        actions, default=str
                    )
                    ev["legs"]["aat3_lit"]["ticket_id"] = t_note
                    ev["legs"]["aat3_lit"]["doc_keys"] = sorted(doc)[:40]
                # aat1: id-diff against the pre-fire snapshot (works even
                # if the template ignored the summary field) + summary scan
                after_ids = await ticket_id_set(client)
                new_ids = sorted(after_ids - pre_ids, key=lambda s: int(s) if s.isdigit() else 0)
                ev["legs"]["aat1_create"]["new_ticket_ids"] = new_ids
                created = await find_by_summary(client, AAT1_MARKER)
                ev["legs"]["aat1_create"]["created_id"] = created or (
                    new_ids[-1] if new_ids else None
                )
                if created or new_ids:
                    tid = created or new_ids[-1]
                    created_tickets.append(tid)
                    cdoc = await ticket_doc(client, tid)
                    ev["legs"]["aat1_create"]["created_summary"] = cdoc.get("summary")
                if t_upd:
                    doc = await ticket_doc(client, t_upd)
                    ev["legs"]["aat2_update"]["updated"] = AAT2_MARKER in str(
                        doc.get("summary") or ""
                    )
                    ev["legs"]["aat2_update"]["summary_now"] = doc.get("summary")
                rl = ev["legs"]["aat2_bad"].get("runlog")
                ev["legs"]["aat2_bad"]["took_failure_path"] = bool(
                    isinstance(rl, dict)
                    and (
                        rl.get("runbook_step") != 2
                        or rl.get("status") != 2
                        or (rl.get("error") or "")
                    )
                )
            finally:
                await ph.cleanup_runbooks(client, created_runbooks, ev)
                for tid in created_tickets:
                    try:
                        await client.request("DELETE", f"/Tickets/{tid}", timeout=30)
                    except Exception as exc:  # noqa: BLE001
                        ev.setdefault("cleanup_errors", []).append(f"ticket {tid}: {exc}")
                ev["ticket_cleanup"] = f"deleted {len(created_tickets)} probe ticket(s)"
    finally:
        ev["verdict"] = verdict(ev)
        out = ph.REPO / "ticket_crud_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"), "|", ev.get("ticket_cleanup"))
        print("evidence:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
