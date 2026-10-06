#!/usr/bin/env python3
"""Probe: does a runbook_variable_mapping materialize a var a CONDITION can read?

Closes gap-class B(a) (guards on awaited results) IF proven:
  L1 control   condition on <<probe_var>> with NO mapping step -> notmet (exec1)
  L2 map_whole aa8/aat2 update (proven shape) + runbook_variable_mappings
               value '<<response>>' (CAT-MIP precedent) + condition
               has-value(<<probe_var>>) -> met (exec3) proves materialization
  L3 map_path   same with value '<<response^summary>>' (their house path style)

The CONDITION is the observer (GET readback of mapped vars is not
observable - v21 lesson): met vs notmet (exec3 vs2) IS the proof.
Self-cleaning: runbooks + probe ticket deleted in finally.
Evidence: mapping_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import probe_harness as ph

COND_SOURCE = """
from bifrost import workflow


@workflow(name="Mapping Probe Condition")
async def mapping_probe(label: str) -> dict:
    if label:
        await yes()
    else:
        await no_way()
    return {}
"""

UPDATE_BODY_KEY = "summary"
UPDATE_MARKER = "halocli-mapping-probe update marker"


def build_condition_doc(name: str, inputs: dict[str, str]) -> dict:
    """Converter-emitted has-value condition on <<label>>, field -> <<probe_var>>."""
    doc, _view = ph.build_condition_leg(
        COND_SOURCE, "mapping_probe", [{"fieldname": "<<probe_var>>"}], name, inputs
    )
    return doc


def verdict(ev: dict[str, Any]) -> str:
    legs = ev["legs"]
    e = {k: (v.get("runlog") or {}).get("steps_executed") for k, v in legs.items()}
    if e.get("control") != 1:
        return f"BROKEN PROBE: control exec={e.get('control')} expected1 (empty var notmet)"
    findings: list[str] = []
    if e.get("map_whole") == 3:
        findings.append("SHIP: <<response>> mapping materializes; has-value condition READS it")
    else:
        findings.append(f"map_whole not proven (exec={e.get('map_whole')})")
    if e.get("map_path") == 3:
        findings.append("SHIP: <<response^summary>> path mapping materializes")
    else:
        findings.append(f"map_path not proven (exec={e.get('map_path')})")
    return " | ".join(findings)


def mapping_step(name: str, value_key: str) -> dict:
    """aa8/aat2 update step carrying a runbook_variable_mapping (their shape)."""
    body = json.dumps({"id": "<<target_id>>", UPDATE_BODY_KEY: UPDATE_MARKER})
    return {
        "step_id": 1,
        "name": name,
        "steptype": 2,
        "auto_action": 8,
        "auto_action_type": 2,
        "isstart": True,
        "allow_all_statuses": True,
        "message": body,
        "runbook_variable_mappings": [
            {
                "guid": None,
                "id": None,
                "type": 4,
                "data_type": 0,
                "key": "probe_var",
                "value": value_key,
                "mapping_type": 0,
            }
        ],
        "actions": [
            {
                "action_type": 18,
                "action_id": -18,
                "action_name": "Successful",
                "start_step": 1,
                "end_step": 2,
                "seq": 1,
                "use_work_hours": True,
                "approval_result": 1,
                "chat_selection_order": 1,
            },
            {
                "action_type": 18,
                "action_id": -18,
                "action_name": "Unsuccessful",
                "start_step": 1,
                "end_step": 4,
                "seq": 2,
                "use_work_hours": True,
                "approval_result": 0,
                "chat_selection_order": 1,
            },
        ],
    }


def map_leg_doc(name: str, value_key: str, target_id: str, inputs: dict[str, str]) -> dict:
    """[update+mapping, condition, met-hop, Fail] - exec3 iff var materialized."""
    cond = build_condition_doc(name + "-cond", inputs)
    cstep = next(s for s in cond["steps"] if s.get("step_conditions"))
    # renumber: mapping step1, condition2, met-hop3? - simpler: prepend
    update = mapping_step(name + "-update", value_key)
    update["message"] = json.dumps({"id": int(target_id), UPDATE_BODY_KEY: UPDATE_MARKER})
    # condition was step1 -> now step2; fix its edges' start/end + hop renumber
    cstep["step_id"] = 2
    for a in cstep["actions"]:
        a["start_step"] = 2
        # met (seq1) -> hop (3); notmet (seq2) -> Fail (4)
        a["end_step"] = 3 if a.get("seq") == 1 else 4
    hop_step = next(s for s in cond["steps"] if s.get("name") == "yes")
    hop_step["step_id"] = 3
    for a in hop_step.get("actions") or []:
        a["start_step"] = 3
        a["end_step"] = 5  # Success
    other = next(s for s in cond["steps"] if s.get("name") == "no_way")
    other["step_id"] = 4
    for a in other.get("actions") or []:
        a["start_step"] = 4
        a["end_step"] = 5
    success = next(s for s in cond["steps"] if s.get("name") == "Success")
    success["step_id"] = 5
    fail = {
        "step_id": 6,
        "name": "Fail",
        "steptype": 3,
        "auto_action": 1,
        "isend": True,
        "allow_all_statuses": True,
        "actions": [],
    }
    # condition's notmet pointed at old Fail id - it targeted by name below
    return {
        "name": f"{ph.bc.PROBE}-{name}"[:80],
        "type": 1,
        "active": True,
        "runbook_start_type": 1,
        "inbound_authentication_type": 0,
        "input_variables": [
            {"id": None, "key": k, "value": v, "data_type": 2, "description": "probe input"}
            for k, v in inputs.items()
        ],
        "steps": [update, cstep, hop_step, other, success, fail],
    }


def control_doc(name: str, inputs: dict[str, str]) -> dict:
    """[condition, met-hop, Success, Fail] - var never set -> notmet exec1."""
    cond = build_condition_doc(name + "-cond", inputs)
    cstep = next(s for s in cond["steps"] if s.get("step_conditions"))
    for a in cstep["actions"]:
        a["start_step"] = 1
        a["end_step"] = 2 if a.get("seq") == 1 else 4
    return cond


async def main() -> int:
    profile = ph.load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {
        "tenant": host,
        "question": "mapping -> condition materialization",
        "legs": {},
    }
    created_runbooks: list[str] = []
    created_tickets: list[str] = []
    try:
        async with ph.HaloClient(profile, profile_name="dev") as client:
            try:
                tid = await ph.create_ticket(client, "halocli mapping probe target")
                if tid:
                    created_tickets.append(tid)

                legs: dict[str, dict] = {
                    "control": {
                        "doc": control_doc("map-control", {"label": "x"}),
                        "inputs": {"label": "x"},
                    },
                    "map_whole": {
                        "doc": map_leg_doc("map-whole", "<<response>>", tid or "0", {"label": "x"}),
                        "inputs": {"label": "x"},
                    },
                    "map_path": {
                        "doc": map_leg_doc(
                            "map-path", "<<response^summary>>", tid or "0", {"label": "x"}
                        ),
                        "inputs": {"label": "x"},
                    },
                }
                for leg, spec in legs.items():
                    resp = await client.request(
                        "POST", "/Webhook", json_body=[spec["doc"]], timeout=60
                    )
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created_runbooks.append(wid)
                    fire = await ph.bc._fire_runbook(client, wid, dict(spec["inputs"]))
                    ev["legs"][leg] = {"id": wid, "runlog": fire.get("runlog")}
                    rl = fire.get("runlog") or {}
                    print(
                        f"{leg:10s} status={rl.get('status')} exec={rl.get('steps_executed')} err={(rl.get('error') or '')[:60]!r}"
                    )
            finally:
                await ph.cleanup_runbooks(client, created_runbooks, ev)
                for t in created_tickets:
                    try:
                        await client.request("DELETE", f"/Tickets/{t}", timeout=30)
                    except Exception as exc:  # noqa: BLE001
                        ev.setdefault("cleanup_errors", []).append(str(exc)[:100])
                ev["ticket_cleanup"] = f"deleted {len(created_tickets)} probe ticket(s)"
    finally:
        ev["verdict"] = verdict(ev)
        out = ph.REPO / "mapping_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"), "|", ev.get("ticket_cleanup"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
