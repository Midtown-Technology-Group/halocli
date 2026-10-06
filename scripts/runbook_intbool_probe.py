#!/usr/bin/env python3
"""Guard-fill probe: int/float set membership + bool bare truthiness.

Two unpinned guard classes, one control-driven probe (harness shared
with the other runbook probes):

  INT/FLOAT SETS   `if level in [1, 2]` - does type23's comma-set work
                   when the param is numeric? Two value_type variants
                   ("int"/"float" inherited from the literal-compare
                   base row vs forced "string"), plus the overlap leg
                   (value "12", field1) to re-check STRICT set
                   semantics on numbers.
  BOOL TRUTHINESS  `if flag:` - Halo HAS a bool input data_type (id5).
                   The proven numeric-truthiness idiom (criteria5
                   ">0", runs2591/2592) is applied to <<flag>> with
                   value1/0 inputs, plus a "True"/"False" format
                   variant since the accepted spellings are unknown.

Every leg is its own self-cleaning runbook with a control so negatives
are interpretable. Verdict states exactly what becomes shippable.
Evidence: intbool_guard_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

from probe_harness import HaloClient, REPO, bc, cleanup_runbooks, convert_workflow, load_profile

SOURCES = {
    "int": """
from bifrost import workflow


@workflow(name="Int Guard Probe")
async def int_guard(level: int) -> dict:
    if level == 1:
        await matched_one()
        await matched_two()
    return {"level": level}
""",
    "float": """
from bifrost import workflow


@workflow(name="Float Guard Probe")
async def float_guard(limit: float) -> dict:
    if limit == 1.5:
        await matched_one()
        await matched_two()
    return {"limit": limit}
""",
    "bool": """
from bifrost import workflow


@workflow(name="Bool Guard Probe")
async def bool_guard(flag: bool, level: int) -> dict:
    if level == 1:
        await matched_one()
        await matched_two()
    return {"flag": flag, "level": level}
""",
}
FUNC = {"int": "int_guard", "float": "float_guard", "bool": "bool_guard"}

_INT_SET = {"type": 23, "value_int": 0, "value_string": "1,2"}
_INT_SET_STR = {**_INT_SET, "value_type": "string"}
_FLT_SET = {"type": 23, "value_int": 0, "value_string": "1.5,2.5"}
_FLT_SET_STR = {**_FLT_SET, "value_type": "string"}
_TRUTHY = {"type": 5, "value_int": 0, "fieldname": "<<flag>>"}  # ">0" idiom

# leg -> (source, row overrides, inputs)
LEGS: dict[str, tuple[str, list[dict], dict[str, str]]] = {
    # controls: the unmutated literal-compare condition must run3
    "int_ctrl": ("int", [{"type": 0, "value_int": 1}], {"level": "1"}),
    "float_ctrl": ("float", [{"type": 0, "value_int": 1, "value_float": 1.5}], {"limit": "1.5"}),
    "bool_ctrl": ("bool", [{"type": 0, "value_int": 1}], {"level": "1", "flag": "1"}),
    # int sets: value_type inherited ("int") vs forced string
    "intA_in": ("int", [_INT_SET], {"level": "2"}),
    "intA_out": ("int", [_INT_SET], {"level": "5"}),
    "intB_in": ("int", [_INT_SET_STR], {"level": "2"}),
    "intB_out": ("int", [_INT_SET_STR], {"level": "5"}),
    "int_overlap": ("int", [{"type": 23, "value_int": 0, "value_string": "12"}], {"level": "1"}),
    # float sets
    "fltA_in": ("float", [_FLT_SET], {"limit": "2.5"}),
    "fltA_out": ("float", [_FLT_SET], {"limit": "3.5"}),
    "fltB_in": ("float", [_FLT_SET_STR], {"limit": "2.5"}),
    "fltB_out": ("float", [_FLT_SET_STR], {"limit": "3.5"}),
    # bool truthiness: criteria5 ">0" on <<flag>>
    "bool_t_10": ("bool", [_TRUTHY], {"level": "5", "flag": "1"}),
    "bool_f_10": ("bool", [_TRUTHY], {"level": "5", "flag": "0"}),
    "bool_t_TF": ("bool", [_TRUTHY], {"level": "5", "flag": "True"}),
    "bool_f_TF": ("bool", [_TRUTHY], {"level": "5", "flag": "False"}),
}


def build_leg(
    source_key: str, overrides: list[dict], name: str, inputs: dict[str, str]
) -> tuple[dict, list[dict]]:
    conv = convert_workflow({}, SOURCES[source_key], FUNC[source_key])
    if not conv.ok:
        raise SystemExit(f"conversion failed: {conv.notes}")
    payload = deepcopy(conv.payload)
    cond = next(s for s in payload["steps"] if s.get("step_conditions"))
    base = cond["step_conditions"][0]
    new_rows: list[dict] = []
    for over in overrides:
        row = deepcopy(base)
        row.update(over)
        new_rows.append(row)
    cond["step_conditions"] = new_rows
    for v in payload["input_variables"]:
        if v["key"] in inputs:
            v["value"] = inputs[v["key"]]
    doc = {k: v for k, v in payload.items() if not k.startswith("_")}
    doc.update(
        {
            "name": f"{bc.PROBE}-{name}"[:80],
            "type": 1,
            "active": True,
            "runbook_start_type": 1,
            "inbound_authentication_type": 0,
        }
    )
    view = [
        {k: r.get(k) for k in ("type", "fieldname", "value_type", "value_string", "value_int")}
        for r in new_rows
    ]
    return doc, view


def exec_of(ev: dict[str, Any], leg: str) -> Any:
    rl = ev["legs"].get(leg, {}).get("runlog")
    return rl.get("steps_executed") if isinstance(rl, dict) else None


def verdict(ev: dict[str, Any]) -> str:
    e = {k: exec_of(ev, k) for k in LEGS}
    if e["int_ctrl"] != 3 or e["float_ctrl"] != 3 or e["bool_ctrl"] != 3:
        return (
            f"BROKEN PROBE: controls int={e['int_ctrl']} float={e['float_ctrl']} "
            f"bool={e['bool_ctrl']}, all expected3"
        )
    findings: list[str] = []
    # int sets
    if e["intA_in"] == 3 and e["intA_out"] == 1:
        findings.append("SHIP: int sets type23 with base value_type int")
    elif e["intB_in"] == 3 and e["intB_out"] == 1:
        findings.append("SHIP: int sets type23 with value_type string")
    else:
        findings.append("int set membership UNPROVEN -> stays flat")
    if e["int_overlap"] == 1:
        findings.append("int value side STRICT comma-set (overlap not matched)")
    elif e["int_overlap"] == 3:
        findings.append("int value side SUBSTRING (overlap matched - unsafe)")
    # float sets
    if e["fltA_in"] == 3 and e["fltA_out"] == 1:
        findings.append("SHIP: float sets type23 with base value_type float")
    elif e["fltB_in"] == 3 and e["fltB_out"] == 1:
        findings.append("SHIP: float sets type23 with value_type string")
    else:
        findings.append("float set membership UNPROVEN -> stays flat")
    # bool truthiness
    if e["bool_t_10"] == 3 and e["bool_f_10"] == 1:
        findings.append("SHIP: bool bare truthiness = criteria5 >0 with inputs 1/0")
    elif e["bool_t_TF"] == 3 and e["bool_f_TF"] == 1:
        findings.append("SHIP: bool bare truthiness = criteria5 >0 with inputs True/False")
    else:
        findings.append("bool truthiness UNPROVEN -> stays flat")
    return f"exec={e} | " + " | ".join(findings)


async def main() -> int:
    profile = load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {
        "tenant": host,
        "question": "int/float sets + bool truthiness",
        "legs": {},
    }
    created: list[str] = []
    try:
        async with HaloClient(profile, profile_name="dev") as client:
            try:
                for leg, (source_key, overrides, inputs) in LEGS.items():
                    doc, view = build_leg(source_key, overrides, f"ib-{leg}", inputs)
                    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created.append(wid)
                    fire = await bc._fire_runbook(client, wid, dict(inputs))
                    ev["legs"][leg] = {
                        "id": wid,
                        "inputs": inputs,
                        "criterion_rows": view,
                        "runlog": fire.get("runlog"),
                        "evolution": fire.get("evolution"),
                        "trigger_fire": fire.get("trigger_fire"),
                    }
                    rl = fire.get("runlog")
                    steps = rl.get("steps_executed") if isinstance(rl, dict) else None
                    print(f"{leg:14s} exec={steps}")
            finally:
                await cleanup_runbooks(client, created, ev)
    finally:
        ev["verdict"] = verdict(ev)
        out = REPO / "intbool_guard_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"))
        print("evidence:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
