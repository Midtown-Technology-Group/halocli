#!/usr/bin/env python3
"""Does Halo resolve ``<<b>>`` on the criteria VALUE side? (var vs var)

The converter maps ``if a == "literal"`` to a real condition step
(trial-proven), but ``if a == b`` (two runbook variables) stays flat
because only the LITERAL value side is pinned. This probe takes the
converter's proven literal-comparison condition and swaps the literal
for ``<<b>>``, then fires three legs:

  control  value "needle",  a=needle  -> expect met    (steps_executed 3)
  met      value "<<b>>",   a=x, b=x  -> expect met    (3) if it resolves
  notmet   value "<<b>>",   a=x, b=y  -> expect notmet (1) either way

met/notmet on the mutated legs is the whole question: 3 vs 1 tells us
whether the server substitutes the runbook variable before comparing.
Self-cleaning: every created runbook is deleted in ``finally`` and
verified gone. Evidence lands in var_compare_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

from probe_harness import HaloClient, REPO, bc, cleanup_runbooks, convert_workflow, load_profile

SOURCE = """
from bifrost import workflow


@workflow(name="Var Compare Probe")
async def var_compare(a: str, b: str) -> dict:
    if a == "needle":
        await matched_one()
        await matched_two()
    return {"a": a, "b": b}
"""

LEGS: dict[str, tuple[str, str, str]] = {
    # leg -> (criterion value, a, b)
    "control": ("needle", "needle", "y"),
    "met": ("<<b>>", "x", "x"),
    "notmet": ("<<b>>", "x", "y"),
}


def build_leg(value: str, name: str, a: str, b: str) -> tuple[dict, list[dict]]:
    conv = convert_workflow({}, SOURCE, "var_compare")
    if not conv.ok:
        raise SystemExit(f"conversion failed: {conv.notes}")
    payload = deepcopy(conv.payload)
    cond = next(s for s in payload["steps"] if s.get("step_conditions"))
    crit_view: list[dict] = []
    for c in cond["step_conditions"]:
        c["value_string"] = value
        crit_view.append(
            {k: c.get(k) for k in ("fieldname", "tablename", "type", "value_type", "value_string")}
        )
    for v in payload["input_variables"]:
        if v["key"] == "a":
            v["value"] = a
        if v["key"] == "b":
            v["value"] = b
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
    return doc, crit_view


def verdict(ev: dict[str, Any]) -> str:
    legs = ev["legs"]

    def exec_of(leg: str) -> Any:
        rl = legs[leg].get("runlog")
        return rl.get("steps_executed") if isinstance(rl, dict) else None

    control, met, notmet = exec_of("control"), exec_of("met"), exec_of("notmet")
    if control != 3:
        return f"BROKEN PROBE: control executed {control}, expected 3 (mechanics failed)"
    if met == 3 and notmet == 1:
        return "RESOLVES: <<b>> on the value side is substituted (met 3 / notmet 1)"
    if met == 1 and notmet == 1:
        return "DOES NOT RESOLVE: value side compares as the literal '<<b>>' (both legs notmet)"
    return f"UNEXPECTED: control={control} met={met} notmet={notmet}"


async def main() -> int:
    profile = load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {"tenant": host, "question": "value-side <<b>> resolution", "legs": {}}
    created: list[str] = []
    try:
        async with HaloClient(profile, profile_name="dev") as client:
            try:
                for leg, (value, a, b) in LEGS.items():
                    doc, crit = build_leg(value, f"varcmp-{leg}", a, b)
                    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created.append(wid)
                    fire = await bc._fire_runbook(client, wid, {"a": a, "b": b})
                    ev["legs"][leg] = {
                        "id": wid,
                        "inputs": {"a": a, "b": b},
                        "criterion": crit,
                        "trigger_fire": fire.get("trigger_fire"),
                        "runlog": fire.get("runlog"),
                        "evolution": fire.get("evolution"),
                    }
                    rl = fire.get("runlog")
                    step = rl.get("steps_executed") if isinstance(rl, dict) else None
                    print(
                        f"{leg:8s} exec={step} runlog={rl if not isinstance(rl, dict) else rl.get('id')}"
                    )
            finally:
                await cleanup_runbooks(client, created, ev)
    finally:
        ev["verdict"] = verdict(ev)
        out = REPO / "var_compare_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"))
        print("evidence:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
