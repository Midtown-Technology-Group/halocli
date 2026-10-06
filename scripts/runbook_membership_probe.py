#!/usr/bin/env python3
"""Membership guard probe: which Halo primitive executes `in` / `not in`?

No trial runbook uses a membership-style criterion (sweep:0 hits in20),
so every candidate is probed directly - one self-cleaning runbook per
leg, always with a control so negatives are interpretable.

Leg groups:
  eq-rows      are multiple step_conditions rows OR'd (set via eq rows)
               or AND'd?
  t23 / t24    "Includes" / "Does not include" pair: value = comma-set?
               negative legs rule out always-met; the overlap leg
               (value "xy", field "x") distinguishes STRICT comma-set
               (x not in {"xy"}:23 notmet /24 met) from substring
               superset ("xy" contains "x":23 met /24 notmet)
  t9           "Contains (comma separated value)" candidate
  array        the reverse Python form: `"x" in arr` on an Array param
               (fieldname swapped to <<arr>>)

Verdict reports which primitives are SHIPPABLE (true leg met + negative
leg notmet + control good). All runbooks deleted in finally; evidence
lands in membership_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

from probe_harness import HaloClient, REPO, bc, cleanup_runbooks, convert_workflow, load_profile

# any str-literal guard gives us a condition step to mutate (proven shape)
SOURCES = {
    "scalar": """
from bifrost import workflow


@workflow(name="Membership Probe")
async def membership_probe(a: str) -> dict:
    if a == "x":
        await matched_one()
        await matched_two()
    return {"a": a}
""",
    "array": """
from bifrost import workflow


@workflow(name="Membership Array Probe")
async def membership_array(arr: list[str], a: str) -> dict:
    if a == "x":
        await matched_one()
        await matched_two()
    return {"arr": arr, "a": a}
""",
}

# leg -> (source, rows [(type, value, fieldname|None)], inputs)
LEGS: dict[str, tuple[str, list[tuple[int, str, str | None]], dict[str, str]]] = {
    "control": ("scalar", [(0, "x", None)], {"a": "x"}),
    "two_eq_or": ("scalar", [(0, "x", None), (0, "y", None)], {"a": "x"}),
    "two_eq_neg": ("scalar", [(0, "x", None), (0, "y", None)], {"a": "z"}),
    "in_list_b": ("scalar", [(0, "x", None), (0, "y", None)], {"a": "y"}),
    "t23_single": ("scalar", [(23, "x", None)], {"a": "x"}),
    "t23_csv": ("scalar", [(23, "x,y", None)], {"a": "y"}),
    "t9_any": ("scalar", [(9, "x,y", None)], {"a": "y"}),
    "t9_neg": ("scalar", [(9, "x,y", None)], {"a": "z"}),
    "t23_neg_set": ("scalar", [(23, "x,y", None)], {"a": "z"}),
    "t23_neg_single": ("scalar", [(23, "x", None)], {"a": "z"}),
    "t24_notin_true": ("scalar", [(24, "x,y", None)], {"a": "z"}),
    "t24_notin_false": ("scalar", [(24, "x,y", None)], {"a": "y"}),
    "t23_overlap": ("scalar", [(23, "xy", None)], {"a": "x"}),
    "t24_overlap": ("scalar", [(24, "xy", None)], {"a": "x"}),
    # reverse form: `"x" in arr` -> fieldname <<arr>>
    "t23_arr_true": ("array", [(23, "x", "<<arr>>")], {"arr": '["x"]', "a": "zz"}),
    "t23_arr_false": ("array", [(23, "x", "<<arr>>")], {"arr": '["z"]', "a": "zz"}),
}


FUNC = {"scalar": "membership_probe", "array": "membership_array"}


def build_leg(
    source_key: str, rows: list[tuple[int, str, str | None]], name: str, inputs: dict[str, str]
) -> tuple[dict, list[dict]]:
    conv = convert_workflow({}, SOURCES[source_key], FUNC[source_key])
    if not conv.ok:
        raise SystemExit(f"conversion failed: {conv.notes}")
    payload = deepcopy(conv.payload)
    cond = next(s for s in payload["steps"] if s.get("step_conditions"))
    base = cond["step_conditions"][0]
    new_rows: list[dict] = []
    for typ, value, field in rows:
        row = deepcopy(base)
        row["type"] = typ
        row["value_string"] = value
        row["value_int"] = 0
        if field:
            row["fieldname"] = field
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
    if e["control"] != 3:
        return f"BROKEN PROBE: control={e['control']}, expected3"
    findings: list[str] = []
    if e["two_eq_or"] == 1 and e["two_eq_neg"] == 1:
        findings.append("multiple eq rows are AND'd (eq rows cannot express a set)")
    elif e["two_eq_or"] == 3 and e["two_eq_neg"] == 1 and e["in_list_b"] == 3:
        findings.append("multiple eq rows are OR'd -> set = one eq row per element")
    if (
        e["t23_single"] == 3
        and e["t23_neg_single"] == 1
        and e["t23_csv"] == 3
        and e["t23_neg_set"] == 1
    ):
        findings.append("SHIP: type23 = in-set membership (both legs proven)")
    elif e["t23_neg_set"] == 3:
        findings.append("type23 ALWAYS MET on this trial - unusable")
    if e["t24_notin_true"] == 3 and e["t24_notin_false"] == 1:
        findings.append("SHIP: type24 = not-in (both legs proven)")
    if e["t9_any"] == 3 and e["t9_neg"] == 1:
        findings.append("SHIP: type9 value-set-contains-field")
    if e["t23_overlap"] == 1 and e["t24_overlap"] == 3:
        findings.append("value side = STRICT comma-set (overlap case not matched)")
    elif e["t23_overlap"] == 3 and e["t24_overlap"] == 1:
        findings.append("value side = SUBSTRING superset (overlap can false-positive)")
    if e["t23_arr_true"] == 3 and e["t23_arr_false"] == 1:
        findings.append("SHIP: type23 also matches Array membership (`const in arr`)")
    elif e["t23_arr_true"] == 1:
        findings.append("type23 on Array field does NOT match element membership")
    if not findings:
        findings.append("NO SHIPPABLE MEMBERSHIP PRIMITIVE (flatten + precise note)")
    return f"exec={e} | " + " | ".join(findings)


async def main() -> int:
    profile = load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {"tenant": host, "question": "membership guard primitive", "legs": {}}
    created: list[str] = []
    try:
        async with HaloClient(profile, profile_name="dev") as client:
            try:
                for leg, (source_key, rows, inputs) in LEGS.items():
                    doc, view = build_leg(source_key, rows, f"membr-{leg}", inputs)
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
                    print(f"{leg:16s} exec={steps}")
            finally:
                await cleanup_runbooks(client, created, ev)
    finally:
        ev["verdict"] = verdict(ev)
        out = REPO / "membership_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"))
        print("evidence:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
