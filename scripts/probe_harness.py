"""Shared harness for the runbook probe scripts (trial-only, self-cleaning).

Holds the pieces every probe repeats so cross-file duplication stays
out of the quality gate: the repo/src bootstrap, the halocli imports
the probes need, the CLI-module loader (fire/_gone helpers), and the
create-side cleanup that deletes every runbook it made.

Probe scripts import this directly (``python scripts/<probe>.py`` puts
scripts/ on sys.path; tests/conftest.py adds it for the test loaders).
"""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from halocli.bifrost_convert import _unquote_vars, convert_workflow  # noqa: E402,F401
from halocli.client import HaloClient  # noqa: E402,F401
from halocli.config import load_profile  # noqa: E402,F401

_spec = importlib.util.spec_from_file_location("bc_cli", REPO / "scripts" / "bifrost_convert.py")
assert _spec is not None and _spec.loader is not None
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


async def cleanup_runbooks(client: Any, created: list[str], ev: dict) -> None:
    """DELETE every runbook the probe created and record verification."""
    for wid in created:
        try:
            await client.request("DELETE", f"/Webhook/{wid}", timeout=30)
        except Exception as exc:  # noqa: BLE001
            ev.setdefault("cleanup_errors", []).append(f"{wid}: {str(exc)[:150]}")
    ev["cleanup"] = [await bc._gone(client, "DELETE check", f"/Webhook/{w}") for w in created]


def build_condition_leg(
    source: str,
    func: str,
    overrides: list[dict],
    name: str,
    inputs: dict[str, str],
) -> tuple[dict, list[dict]]:
    """One self-cleaning probe runbook: converter output + mutated condition.

    The converter's proven literal-compare condition step is the base;
    each ``overrides`` dict is merged over its criterion row (type,
    value_string, fieldname, value_type...), inputs patch the document's
    input variables, and the document gets the public-active probe shell.
    """
    conv = convert_workflow({}, source, func)
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


def aa8_runbook(name: str, aat: int, message: str, inputs: dict[str, str] | None = None) -> dict:
    """One-step Halo API Action runbook (aa8 + aat, act18 edge pair).

    ``inputs`` become document input_variables - aa8 message bodies can
    interpolate them as ``<<key>>`` (the dynamic-write question the
    interpolation probe pins).
    """

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
        "name": f"{bc.PROBE}-{name}"[:80],
        "type": 1,
        "active": True,
        "runbook_start_type": 1,
        "inbound_authentication_type": 0,
        "input_variables": [
            {"id": None, "key": k, "value": v, "data_type": 2, "description": "probe input"}
            for k, v in (inputs or {}).items()
        ],
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


async def fire_leg(
    client: Any,
    leg: str,
    doc: dict,
    view: list[dict],
    inputs: dict[str, str],
    ev: dict,
    width: int = 16,
) -> str:
    """POST one probe runbook, fire it, record the runlog - return its id."""
    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
    row = resp[0] if isinstance(resp, list) and resp else resp
    wid = str(row.get("id"))
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
    print(f"{leg:{width}s} exec={steps}")
    return wid
