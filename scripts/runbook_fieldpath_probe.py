#!/usr/bin/env python3
"""Field-path probe: nested criteria on mapped vars + templated method paths.

Two open pins:

  NESTED CRITERIA   Does a condition fieldname `<<var^field>>` deref a
                    MAPPED runbook variable's field? (closes guards on
                    `x = mapped["id"]`-class values IF the deref works)
                    leg control   condition on <<probe_obj^summary>> with NO
                                  mapping -> notmet (exec1)
                    leg deref     aat2 update maps <<response>> into
                                  probe_obj, condition eq on
                                  <<probe_obj^summary>> == the update
                                  marker -> met (exec3) proves BOTH the
                                  object materialization and the deref
  METHOD PATHS      do templated paths create on an integration?
                    /v1/account/{account_id}   (OpenAPI style)
                    /v1/account/<<account_id>> (Halo house style)
                    -> create against integration43 (spec-built), then
                       DELETE (self-cleaning; the catalog is untouched)

Self-cleaning: runbooks + probe ticket deleted; method rows deleted.
Evidence: fieldpath_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import probe_harness as ph

COMPARE_SOURCE = """
from bifrost import workflow


@workflow(name="Fieldpath Compare")
async def fieldpath_probe(label: str) -> dict:
    if label == "needle":
        await yes()
    else:
        await no_way()
    return {}
"""

UPDATE_MARKER = "halocli-fieldpath probe marker"
INTEGRATION_ID = 43  # the Huntress integration built earlier


def update_mapping_step(target_id: str) -> dict:
    body = json.dumps({"id": int(target_id), "summary": UPDATE_MARKER})
    return {
        "step_id": 1,
        "name": "update-with-mapping",
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
                "key": "probe_obj",
                "value": "<<response>>",
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
                "end_step": 5,
                "seq": 2,
                "use_work_hours": True,
                "approval_result": 0,
                "chat_selection_order": 1,
            },
        ],
    }


def condition_doc(name: str, fieldname: str, value: str, inputs: dict[str, str]) -> dict:
    """eq condition on a NESTED fieldname, built from the proven compare shape."""
    doc, _ = ph.build_condition_leg(
        COMPARE_SOURCE,
        "fieldpath_probe",
        [{"fieldname": fieldname, "value_string": value}],
        name,
        inputs,
    )
    return doc


def deref_leg_doc(name: str, target_id: str, inputs: dict[str, str]) -> dict:
    """[update+mapping, condition on <<probe_obj^summary>>, hops...] exec3 iff deref works."""
    cond = condition_doc(name + "-cond", "<<probe_obj^summary>>", UPDATE_MARKER, inputs)
    update = update_mapping_step(target_id)
    cstep = next(s for s in cond["steps"] if s.get("step_conditions"))
    # renumber the condition chain to sit after the update step
    cstep["step_id"] = 2
    for a in cstep["actions"]:
        a["start_step"] = 2
        a["end_step"] = 3 if a.get("seq") == 1 else 5  # met -> hop, notmet -> Fail
    met = next(s for s in cond["steps"] if s.get("name") == "yes")
    met["step_id"] = 3
    for a in met.get("actions") or []:
        a["start_step"], a["end_step"] = 3, 4
    other = next(s for s in cond["steps"] if s.get("name") == "no_way")
    other["step_id"] = 4
    for a in other.get("actions") or []:
        a["start_step"], a["end_step"] = 4, 5
    success = next(s for s in cond["steps"] if s.get("name") == "Success")
    success["step_id"] = 5
    doc = {
        "name": f"{ph.bc.PROBE}-{name}"[:80],
        "type": 1,
        "active": True,
        "runbook_start_type": 1,
        "inbound_authentication_type": 0,
        "input_variables": cond.get("input_variables"),
        "steps": [update, cstep, met, other, success],
    }
    # Fail terminal for the notmet edge
    doc["steps"].append(
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
    cstep_actions = cstep["actions"]
    cstep_actions[1]["end_step"] = 6  # notmet -> Fail
    return doc


def verdict(ev: dict[str, Any]) -> str:
    legs = ev["legs"]
    findings: list[str] = []
    ctl = (legs.get("control") or {}).get("runlog") or {}
    if ctl.get("steps_executed") != 2:
        return f"BROKEN PROBE: control exec={ctl.get('steps_executed')} expected2 (else-arm notmet)"
    deref = (legs.get("deref") or {}).get("runlog") or {}
    if deref.get("steps_executed") == 3 and deref.get("status") == 2:
        findings.append(
            "SHIP: <<var^field>> derefs a MAPPED variable (eq on "
            "<<probe_obj^summary>> met; object materialized too)"
        )
    elif deref.get("status") == 1:
        findings.append(
            "PROVEN NON-WORKING: <<var^field>> in a criterion fieldname does "
            "NOT deref (notmet -> Fail; materialization + response^summary "
            "path both proven elsewhere, so the caret is the failure) - "
            "derived-value guards stay flat + noted"
        )
    else:
        findings.append(
            f"nested deref inconclusive (status={deref.get('status')} "
            f"exec={deref.get('steps_executed')})"
        )
    tp = legs.get("method_paths") or {}
    brace_ok = "accepted" in str(tp.get("brace"))
    var_ok = "accepted" in str(tp.get("var"))
    if brace_ok and var_ok:
        findings.append(
            "SHIP: BOTH templated method-path styles create (OpenAPI {param} "
            "and Halo <<param>>) - the spec generator keeps {param} as-is"
        )
    else:
        findings.append(f"method paths: brace={tp.get('brace')} var={tp.get('var')}")
    return " | ".join(findings)


async def main() -> int:
    profile = ph.load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {"tenant": host, "question": "nested criteria + method paths", "legs": {}}
    created_runbooks: list[str] = []
    created_tickets: list[str] = []
    created_methods: list[int] = []
    try:
        async with ph.HaloClient(profile, profile_name="dev") as client:
            try:
                tid = await ph.create_ticket(client, "halocli fieldpath target")
                if tid:
                    created_tickets.append(tid)

                legs = {
                    "control": condition_doc(
                        "fp-control", "<<probe_obj^summary>>", UPDATE_MARKER, {"label": "x"}
                    ),
                    "deref": deref_leg_doc("fp-deref", tid or "0", {"label": "x"}),
                }
                for leg, doc in legs.items():
                    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created_runbooks.append(wid)
                    fire = await ph.bc._fire_runbook(client, wid, {"label": "x"})
                    ev["legs"][leg] = {"id": wid, "runlog": fire.get("runlog")}
                    rl = fire.get("runlog") or {}
                    print(f"{leg:10s} status={rl.get('status')} exec={rl.get('steps_executed')}")

                # A3: templated method paths on the spec-built integration
                tp: dict[str, str] = {}
                for label, path in (
                    ("brace", "/v1/account/{account_id}"),
                    ("var", "/v1/account/<<account_id>>"),
                ):
                    try:
                        r = await client.request(
                            "POST",
                            "/CustomIntegrationMethod",
                            json_body=[
                                {
                                    "name": f"halocli-pathprobe-{label}",
                                    "path": path,
                                    "method": 0,
                                    "integration_id": INTEGRATION_ID,
                                }
                            ],
                            timeout=45,
                        )
                        mr = r[0] if isinstance(r, list) and r else r
                        mid = mr.get("id")
                        if mid:
                            created_methods.append(int(mid))
                        tp[label] = f"accepted id={mid}"
                    except Exception as exc:  # noqa: BLE001
                        tp[label] = f"rejected: {str(exc)[:120]}"
                    print(f"path[{label}] {path} -> {tp[label]}")
                ev["legs"]["method_paths"] = tp
            finally:
                await ph.cleanup_runbooks(client, created_runbooks, ev)
                for mid in created_methods:
                    try:
                        await client.request(
                            "DELETE", f"/CustomIntegrationMethod/{mid}", timeout=30
                        )
                    except Exception as exc:  # noqa: BLE001
                        ev.setdefault("cleanup_errors", []).append(f"method {mid}: {exc}")
                for t in created_tickets:
                    try:
                        await client.request("DELETE", f"/Tickets/{t}", timeout=30)
                    except Exception as exc:  # noqa: BLE001
                        ev.setdefault("cleanup_errors", []).append(str(exc)[:100])
                ev["cleanup"] = (
                    f"{ev.get('cleanup', 'runbooks: none')}; "
                    f"{len(created_methods)} probe method(s) deleted; "
                    f"{len(created_tickets)} ticket(s) deleted"
                )
    finally:
        ev["verdict"] = verdict(ev)
        out = ph.REPO / "fieldpath_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
