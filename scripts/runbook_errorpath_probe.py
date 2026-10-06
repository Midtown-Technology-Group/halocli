#!/usr/bin/env python3
"""Error-path + request-interp probe.

  RECOVERY ROUTING   The try/except mapping needs runtime proof that
                     an aa8 "Unsuccessful" edge routes to an ARBITRARY
                     recovery hop (not just the Fail terminal) and
                     that the success path SKIPS the recovery block:
                       try_ok   -> valid aat2 update: write succeeds,
                                  success edge runs the two normal
                                  hops (exec3), recovery hop NOT run
                       try_fail -> bad id: write fails, Unsuccessful
                                  edge runs the recovery hop then
                                  converges to Success (exec2, status2)
                       control_fail -> bad id with edge2 -> Fail:
                                  contrast leg (v15 shape, status1)
  REQUEST INTERP      Does an aa8 body interpolate a key that exists
                     ONLY in formCollection (the <<request>> payload),
                     not in the document's input_variables? aat1 with
                     summary "<<extra>>", fired with formCollection
                     extra=<value>: created summary == value?

Self-cleaning: runbooks + probe tickets deleted in finally; evidence
lands in errorpath_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import probe_harness as ph

REQ_IN = "halocli-errorpath request-carried"

# leg -> (kind, spec): "try" shapes are hand-built4/6-step graphs,
# "req" is a one-shot aat1 body with a request-only token
LEGS: dict[str, dict] = {
    "try_ok": {
        "aat": 2,
        "body": {"id": "TARGET", "summary": "halocli-errorpath ok update"},
        "inputs": {},
        "recovery": "hop",
    },
    "try_fail": {
        "aat": 2,
        "body": {"id": 99999999, "summary": "nope"},
        "inputs": {},
        "recovery": "hop",
    },
    "control_fail": {
        "aat": 2,
        "body": {"id": 99999999, "summary": "nope"},
        "inputs": {},
        "recovery": "fail",
    },
    "req_interp": {
        "aat": 1,
        "body": {"summary": "<<extra>>", "reportedby": "probe@example.com"},
        "inputs": {},
        "recovery": None,  # plain one-step (v15 shape)
    },
}


def errorpath_runbook(name: str, spec: dict, target_id: int | None = None) -> dict:
    """try/except-shaped graph: write -> (normal hops | recovery) -> Success.

    step1 write (aa8/aat): edge1 -> step2 (normal), edge2 -> step4
    (recovery hop) or step5 (Fail) for the control; steps2/3 = the
    try-success continuation, step4 = the handler; both converge on
    Success. exec discriminates: ok path runs1+2 hops =3, recovered
    path runs1+1 hop =2.
    """
    body = dict(spec["body"])
    if body.get("id") == "TARGET":
        body["id"] = target_id or 0

    def edge(name_: str, end: int, seq: int) -> dict:
        return {
            "action_type": 18,
            "action_id": -18,
            "action_name": name_,
            "start_step": 1,
            "end_step": end,
            "seq": seq,
            "use_work_hours": True,
            "approval_result": 1 if seq == 1 else 0,
            "chat_selection_order": 1,
        }

    def hop(step_id: int, name_: str, end: int) -> dict:
        return {
            "step_id": step_id,
            "name": name_,
            "steptype": 2,
            "auto_action": 21,  # aa21 sequencing hop (converter shape)
            "duration": 0,
            "isstart": False,
            "allow_all_statuses": True,
            "actions": [
                {
                    "action_type": 32,
                    "action_id": -32,
                    "action_name": "Sleep Finished",
                    "start_step": step_id,
                    "end_step": end,
                    "seq": 1,
                    "use_work_hours": True,
                    "approval_result": 1,
                    "chat_selection_order": 1,
                }
            ],
        }

    if spec["recovery"] is None:
        # plain v15 shape: single write + terminals
        steps = [
            {
                "step_id": 1,
                "name": "write",
                "steptype": 2,
                "auto_action": 8,
                "auto_action_type": spec["aat"],
                "isstart": True,
                "allow_all_statuses": True,
                "message": ph._unquote_vars(json.dumps(body, indent=2)),
                "actions": [edge("Successful", 3, 1), edge("Unsuccessful", 4, 2)],
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
        return {
            "name": f"{ph.bc.PROBE}-{name}"[:80],
            "type": 1,
            "active": True,
            "runbook_start_type": 1,
            "inbound_authentication_type": 0,
            "input_variables": [],
            "steps": steps,
        }

    fail_end = 6 if spec["recovery"] == "fail" else 4
    steps = [
        {
            "step_id": 1,
            "name": "write",
            "steptype": 2,
            "auto_action": 8,
            "auto_action_type": spec["aat"],
            "isstart": True,
            "allow_all_statuses": True,
            "message": ph._unquote_vars(json.dumps(body, indent=2)),
            "actions": [edge("Successful", 2, 1), edge("Unsuccessful", fail_end, 2)],
        },
        hop(2, "normal one", 3),
        # success-path continuation converges straight to Success -
        # the try/except shape: normal flow SKIPS the recovery hop
        hop(3, "normal two", 5),
        hop(4, "recovered", 5),
        {
            "step_id": 5,
            "name": "Success",
            "steptype": 3,
            "isend": True,
            "islaststep": True,
            "allow_all_statuses": True,
            "actions": [],
        },
    ]
    if spec["recovery"] == "fail":
        steps.append(
            {
                "step_id": 6,
                "name": "Fail",
                "steptype": 3,
                "auto_action": 1,
                "isend": True,
                "allow_all_statuses": True,
                "actions": [],
            }
        )
    return {
        "name": f"{ph.bc.PROBE}-{name}"[:80],
        "type": 1,
        "active": True,
        "runbook_start_type": 1,
        "inbound_authentication_type": 0,
        "input_variables": [],
        "steps": steps,
    }


def exec_of(ev: dict[str, Any], leg: str) -> Any:
    rl = ev["legs"].get(leg, {}).get("runlog")
    return rl.get("steps_executed") if isinstance(rl, dict) else None


def status_of(ev: dict[str, Any], leg: str) -> Any:
    rl = ev["legs"].get(leg, {}).get("runlog")
    return rl.get("status") if isinstance(rl, dict) else None


def verdict(ev: dict[str, Any]) -> str:
    e = {k: exec_of(ev, k) for k in LEGS}
    s = {k: status_of(ev, k) for k in LEGS}
    if e["try_ok"] is None:
        return f"BROKEN PROBE: exec={e}"
    findings: list[str] = []
    if s["try_ok"] == 2 and e["try_ok"] == 3:
        findings.append("success path runs the normal hops (recovery skipped)")
    else:
        findings.append(f"try_ok unexpected (status={s['try_ok']} exec={e['try_ok']})")
    if s["try_fail"] == 2 and e["try_fail"] == 2:
        findings.append("SHIP: Unsuccessful edge routes to a recovery hop and converges")
    else:
        findings.append(f"try_fail unexpected (status={s['try_fail']} exec={e['try_fail']})")
    if s["control_fail"] != 2:
        findings.append("control: edge2 -> Fail keeps the run off status2 (contrast)")
    else:
        findings.append("control FAILED: edge2 -> Fail reached status2?! ")
    if ev["legs"]["req_interp"].get("created_summary") == REQ_IN:
        findings.append("SHIP: <<request>> keys interpolate in aa8 bodies (formCollection only)")
    elif ev["legs"]["req_interp"].get("created_id"):
        findings.append(
            f"request key NOT interpolated (summary={ev['legs']['req_interp'].get('created_summary')!r})"
        )
    else:
        rl = ev["legs"]["req_interp"].get("runlog") or {}
        findings.append(f"request-interp run failed (status={rl.get('status')})")
    return " | ".join(findings)


async def main() -> int:
    profile = ph.load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {
        "tenant": host,
        "question": "recovery routing + request interp",
        "legs": {},
    }
    created_runbooks: list[str] = []
    created_tickets: list[str] = []
    try:
        async with ph.HaloClient(profile, profile_name="dev") as client:
            try:
                t_ok = await ph.create_ticket(client, "halocli-errorpath update target")
                if t_ok:
                    created_tickets.append(t_ok)
                pre_ids = await ph.ticket_id_set(client)

                for leg, spec in LEGS.items():
                    doc = errorpath_runbook(f"err-{leg}", spec, int(t_ok) if t_ok else None)
                    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created_runbooks.append(wid)
                    fire_inputs = {"extra": REQ_IN} if leg == "req_interp" else dict(spec["inputs"])
                    fire = await ph.bc._fire_runbook(client, wid, fire_inputs)
                    ev["legs"][leg] = {
                        "id": wid,
                        "runlog": fire.get("runlog"),
                        "trigger_fire": fire.get("trigger_fire"),
                    }
                    rl = fire.get("runlog") or {}
                    print(f"{leg:14s} status={rl.get('status')} exec={rl.get('steps_executed')}")

                # request-interp readback: a ticket whose summary is the
                # formCollection-only value
                after = await ph.ticket_id_set(client)
                for tid in sorted(after - pre_ids, key=lambda s: int(s) if s.isdigit() else 0):
                    doc = await ph.ticket_doc(client, tid)
                    summary = str(doc.get("summary") or "")
                    if "halocli" in summary or "extra" in summary:
                        created_tickets.append(tid)
                        ev["legs"]["req_interp"]["created_id"] = tid
                        ev["legs"]["req_interp"]["created_summary"] = summary
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
        out = ph.REPO / "errorpath_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"), "|", ev.get("ticket_cleanup"))
        print("evidence:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
