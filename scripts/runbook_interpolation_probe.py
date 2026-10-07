#!/usr/bin/env python3
"""Dynamic-write + bool-encoding probe.

Two open questions, one control-driven probe (shared harness):

  INTERPOLATION   Do aa8 message bodies interpolate RUNBOOK INPUT
                  vars (<<subject>>), not just <<ticket^id>>?
                  - aat1 create with summary "<<subject>>": the
                    created ticket's summary must equal the input
                    value (dynamic ticket writes!)
                  - aat1 with "<<no_such_var>>": does the step fail
                    (the ticket^id precedent) or pass the token
                    through literally?
                  - aat3 note with note_html "<<note_text>>": the
                    Action row must contain the input value
  BOOL ENCODING   `flag == True` (eq value_int1) and `flag in [True]`
                  (type23 comma-set): which value spellings work on
                  data_type5 inputs - "1"/"0" (the v14 truthiness
                  proof) or "True"/"False"?

Every runbook and probe ticket is deleted in finally; evidence lands
in interpolation_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import probe_harness as ph


_C_flag = "<<flag>>"
SUBJECT_IN = "halocli-interp created-by-input"
NOTE_IN = "halocli-interp dynamic note"

BOOL_SOURCE = """
from bifrost import workflow


@workflow(name="Interp Bool Probe")
async def interp_bool(flag: bool, level: int) -> dict:
    if level == 1:
        await probe_yes()
        await probe_more()
    return {"flag": flag, "level": level}
"""

# leg -> ("aa8", aat, message, inputs) | ("cond", row_overrides, inputs)
LEGS: dict[str, tuple] = {
    "aat1_var": (
        "aa8",
        1,
        {"summary": "<<subject>>", "reportedby": "probe@example.com"},
        {"subject": SUBJECT_IN},
    ),
    "aat1_missing": (
        "aa8",
        1,
        {"summary": "<<no_such_var>>", "reportedby": "probe@example.com"},
        {},
    ),
    "aat3_var": ("aa8", 3, {}, {"note_text": NOTE_IN}),
    "bool_eq_t": (
        "cond",
        [{"type": 0, "value_int": 1, "fieldname": _C_flag}],
        {"flag": "1", "level": "5"},
    ),
    "bool_eq_f": (
        "cond",
        [{"type": 0, "value_int": 1, "fieldname": _C_flag}],
        {"flag": "0", "level": "5"},
    ),
    "boolset_t": (
        "cond",
        [{"type": 23, "value_int": 0, "value_string": "1", "fieldname": _C_flag}],
        {"flag": "1", "level": "5"},
    ),
    "boolset_f": (
        "cond",
        [{"type": 23, "value_int": 0, "value_string": "1", "fieldname": _C_flag}],
        {"flag": "0", "level": "5"},
    ),
    "boolset0_f": (
        "cond",
        [{"type": 23, "value_int": 0, "value_string": "0", "fieldname": _C_flag}],
        {"flag": "0", "level": "5"},
    ),
    "boolset0_t": (
        "cond",
        [{"type": 23, "value_int": 0, "value_string": "0", "fieldname": _C_flag}],
        {"flag": "1", "level": "5"},
    ),
    # case-only rename would collide (server names are case-insensitive)
    "boolset_capital": (
        "cond",
        [{"type": 23, "value_int": 0, "value_string": "True", "fieldname": _C_flag}],
        {"flag": "1", "level": "5"},
    ),
}

AA8 = {leg: spec for leg, spec in LEGS.items() if spec[0] == "aa8"}
COND = {leg: spec for leg, spec in LEGS.items() if spec[0] == "cond"}


def exec_of(ev: dict[str, Any], leg: str) -> Any:
    rl = ev["legs"].get(leg, {}).get("runlog")
    return rl.get("steps_executed") if isinstance(rl, dict) else None


def verdict(ev: dict[str, Any]) -> str:
    legs = ev["legs"]
    e = {k: exec_of(ev, k) for k in LEGS}
    if not all(e[k] for k in ("aat1_var", "bool_eq_t")):
        return f"BROKEN PROBE: exec={e}"
    findings: list[str] = []
    # interpolation
    if legs["aat1_var"].get("created_summary") == SUBJECT_IN:
        findings.append("SHIP: <<input>> vars interpolate in aa8 bodies (dynamic create)")
    elif legs["aat1_var"].get("created_id"):
        findings.append(
            f"aat1 created but summary={legs['aat1_var'].get('created_summary')!r} "
            "(input not interpolated)"
        )
    else:
        findings.append("aat1 input-var create NOT observed")
    missing = legs["aat1_missing"]
    if missing.get("created_id"):
        findings.append(
            f"unresolved token PASSED THROUGH literally (summary={missing.get('created_summary')!r})"
        )
    elif (missing.get("runlog") or {}).get("status") != 2:
        findings.append("unresolved token fails the step (same as <<ticket^id>> precedent)")
    else:
        findings.append("unresolved token: no ticket, run succeeded - inspect")
    if legs.get("aat3_var", {}).get("note_has_input"):
        findings.append("SHIP: dynamic note text via <<input>> (aat3)")
    else:
        findings.append("aat3 input-var note NOT observed")
    # bool encodings
    if e["bool_eq_t"] == 3 and e["bool_eq_f"] == 1:
        findings.append("SHIP: flag == True -> eq value_int1 (both legs)")
    else:
        findings.append(f"bool literal eq NOT pinned (exec t={e['bool_eq_t']} f={e['bool_eq_f']})")
    if e["boolset_t"] == 3 and e["boolset_f"] == 1:
        findings.append("SHIP: bool membership encodes as '1' (both legs)")
    elif e["boolset_t"] == 1:
        findings.append("bool membership with '1' does NOT match")
    if e["boolset0_f"] == 3 and e["boolset0_t"] == 1:
        findings.append("SHIP: bool membership encodes False as '0' (both legs)")
    elif e["boolset0_f"] == 1:
        findings.append("bool membership with '0' does NOT match")
    if e["boolset_capital"] == 1:
        findings.append("'True' spelling fails (canonical 1/0 required)")
    elif e["boolset_capital"] == 3:
        findings.append("'True' spelling ALSO works")
    return " | ".join(findings)


async def main() -> int:
    profile = ph.load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {"tenant": host, "question": "dynamic bodies + bool encoding", "legs": {}}
    created_runbooks: list[str] = []
    created_tickets: list[str] = []
    try:
        async with ph.HaloClient(profile, profile_name="dev") as client:
            try:
                t_note = await ph.create_ticket(client, "halocli-interp note target")
                if t_note:
                    created_tickets.append(t_note)
                pre_ids = await ph.ticket_id_set(client)

                # aa8 legs (dynamic body + unresolved-token behavior)
                for leg, (_kind, aat, message, inputs) in AA8.items():
                    body = dict(message)
                    if aat == 3 and t_note:
                        body["ticket_id"] = int(t_note)
                        body.update(
                            {
                                "outcome": "Internal Note",
                                "who": "Automation",
                                "hiddenfromuser": True,
                                "note_html": "<<note_text>>",
                            }
                        )
                    doc = ph.aa8_runbook(
                        f"interp-{leg}", aat, ph._unquote_vars(json.dumps(body)), inputs
                    )
                    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created_runbooks.append(wid)
                    fire = await ph.bc._fire_runbook(client, wid, dict(inputs))
                    ev["legs"][leg] = {
                        "id": wid,
                        "aat": aat,
                        "inputs": inputs,
                        "runlog": fire.get("runlog"),
                        "trigger_fire": fire.get("trigger_fire"),
                    }

                # readbacks for the aa8 legs: only claim tickets whose
                # summary ties them to THIS probe (foreign tickets on a
                # shared trial are ignored, not deleted)
                after = await ph.ticket_id_set(client)
                new_ids = sorted(after - pre_ids, key=lambda s: int(s) if s.isdigit() else 0)
                for tid in new_ids:
                    doc = await ph.ticket_doc(client, tid)
                    summary = str(doc.get("summary") or "")
                    if "halocli-interp" not in summary and "no_such_var" not in summary:
                        continue
                    created_tickets.append(tid)
                    key = "aat1_var" if SUBJECT_IN in summary else "aat1_missing"
                    rec = ev["legs"].setdefault(key, {})
                    rec["created_id"] = tid
                    rec["created_summary"] = summary
                if t_note:
                    actions = await client.request(
                        "GET",
                        "/Actions",
                        params={"ticket_id": str(t_note), "count": "50"},
                        timeout=45,
                    )
                    ev["legs"]["aat3_var"] = {
                        **ev["legs"].get("aat3_var", {}),
                        "note_has_input": NOTE_IN in json.dumps(actions, default=str),
                    }

                # bool condition legs
                for leg, (_kind, overrides, inputs) in COND.items():
                    doc, _view = ph.build_condition_leg(
                        BOOL_SOURCE, "interp_bool", overrides, f"interp-{leg}", inputs
                    )
                    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created_runbooks.append(wid)
                    fire = await ph.bc._fire_runbook(client, wid, dict(inputs))
                    ev["legs"][leg] = {
                        "id": wid,
                        "inputs": inputs,
                        "runlog": fire.get("runlog"),
                        "trigger_fire": fire.get("trigger_fire"),
                    }
                    steps = (fire.get("runlog") or {}).get("steps_executed")
                    print(f"{leg:14s} exec={steps}")
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
        out = ph.REPO / "interpolation_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"), "|", ev.get("ticket_cleanup"))
        print("evidence:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
