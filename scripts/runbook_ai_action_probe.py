#!/usr/bin/env python3
"""Probe the auto-actions Halo's templates use but this repo never fired:
aa18 (SQL Query), aa25 (AI Evaluation), aa26 (Trigger AI Agent).

Shapes are replayed from halo_online_runbook_repository.json (Halo's own
authored steps):

  sql_query  aa18 + act29 pair: the AI Auto Triage impact/urgency SQL
             with <<ticket^id>> replaced by a LITERAL probe ticket id
             (the v16 rule: bare <<ticket^id>> needs trigger context),
             plus its runbook_variable_mappings (<<response[0]^col>> ->
             runbook vars, pre-created as inputs)
  ai_eval    aa25/aat44 + ai_ability_id + Halo's own message
  ai_agent   aa26 + ai_ability_id, NO message, input_values omitted
             (theirs reference <<ticket^id>>)

Each leg: create -> GET back (did the fields persist?) -> fire -> runlog;
aa18 additionally re-reads input variables to see the mapping land.
Self-cleaning: runbooks + the probe ticket deleted in finally.
Evidence: ai_action_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import probe_harness as ph

# verbatim from Halo's AI Auto Triage template (<<ticket^id>> -> literal)
SQL_TEMPLATE = (
    "select \r\n\r\nimpact.fvalue as [impact],\r\nurgency.fvalue as [urgency]\r\n\r\n"
    "from faults \r\n\r\n"
    "join lookup impact on impact.fid = 12 and impact.fcode = impact\r\n"
    "join lookup urgency on urgency.fid = 27 and urgency.fcode = urgency\r\n\r\n"
    "where faultid = {tid}"
)
AI_EVAL_ABILITY = "9d7b5165-2222-4908-a5d4-e02577e76504"
AI_AGENT_ABILITY = "17866d7c-9871-4842-b934-7e01e1091d79"

MAPPINGS = [
    {
        "guid": None,
        "id": None,
        "type": 4,
        "data_type": 2,
        "key": "impact_description",
        "value": "<<response[0]^impact>>",
        "mapping_type": 0,
    },
    {
        "guid": None,
        "id": None,
        "type": 4,
        "data_type": 2,
        "key": "urgency_description",
        "value": "<<response[0]^urgency>>",
        "mapping_type": 0,
    },
]


def verdict(ev: dict[str, Any]) -> str:
    legs = ev["legs"]
    findings: list[str] = []
    sql = legs.get("sql_query", {})
    rl = sql.get("runlog") or {}
    if rl.get("status") == 2:
        mapped = (sql.get("vars_after") or {}).get("impact_description")
        findings.append(
            f"SHIP: aa18 SQL ran (run {rl.get('id')} status2, exec {rl.get('steps_executed')})"
            + (
                f"; mapping landed (impact_description={mapped!r})"
                if mapped
                else "; mapping NOT read back"
            )
        )
    else:
        findings.append(
            f"aa18 NOT run (status={rl.get('status')}, error={(rl.get('error') or '')[:70]!r})"
        )
    for leg, label in (("ai_eval", "aa25"), ("ai_agent", "aa26")):
        lr = (legs.get(leg) or {}).get("runlog") or {}
        if lr.get("status") == 2:
            findings.append(f"SHIP: {label} ability ran (run {lr.get('id')} status2)")
        else:
            findings.append(
                f"{label} not run (status={lr.get('status')}, "
                f"error={(lr.get('error') or '')[:70]!r})"
            )
    return " | ".join(findings)


async def main() -> int:
    profile = ph.load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {"tenant": host, "question": "aa18/aa25/aa26 execution", "legs": {}}
    created_runbooks: list[str] = []
    created_tickets: list[str] = []
    try:
        async with ph.HaloClient(profile, profile_name="dev") as client:
            try:
                tid = await ph.create_ticket(client, "halocli ai-action probe target")
                if tid:
                    created_tickets.append(tid)

                legs_spec: dict[str, dict] = {
                    "sql_query": {
                        "aa": 18,
                        "message": SQL_TEMPLATE.format(tid=tid or 0),
                        "extra": {"runbook_variable_mappings": MAPPINGS},
                        "inputs": {"impact_description": "", "urgency_description": ""},
                    },
                    "ai_eval": {
                        "aa": 25,
                        "aat": 44,
                        "message": "Azure OpenAI: AI Evaluation",
                        "extra": {"ai_ability_id": AI_EVAL_ABILITY},
                        "inputs": {},
                    },
                    "ai_agent": {
                        "aa": 26,
                        "message": None,
                        "extra": {"ai_ability_id": AI_AGENT_ABILITY},
                        "inputs": {},
                    },
                }

                for leg, spec in legs_spec.items():
                    doc = ph.action_runbook(
                        f"ai-{leg}",
                        aa=spec["aa"],
                        message=spec.get("message"),
                        aat=spec.get("aat"),
                        extra=spec.get("extra"),
                        inputs=spec.get("inputs"),
                    )
                    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created_runbooks.append(wid)

                    # persistence check: did the server keep our fields?
                    full = await client.request(
                        "GET", f"/Webhook/{wid}", params={"includedetails": "true"}, timeout=60
                    )
                    step1 = next(
                        (s for s in (full.get("steps") or []) if s.get("step_id") == 1), {}
                    )
                    persisted = {
                        "auto_action": step1.get("auto_action"),
                        "auto_action_type": step1.get("auto_action_type"),
                        "message_len": len(str(step1.get("message") or "")),
                        "ai_ability_id": step1.get("ai_ability_id"),
                        "edge_types": [a.get("action_type") for a in (step1.get("actions") or [])],
                    }
                    ev["legs"][leg] = {"id": wid, "persisted": persisted}

                    fire = await ph.bc._fire_runbook(client, wid, dict(spec.get("inputs") or {}))
                    ev["legs"][leg]["runlog"] = fire.get("runlog")
                    rl = fire.get("runlog") or {}
                    print(
                        f"{leg:10s} aa={spec['aa']} persisted={persisted} "
                        f"-> status={rl.get('status')} exec={rl.get('steps_executed')} "
                        f"err={(rl.get('error') or '')[:60]!r}"
                    )

                    if leg == "sql_query":
                        # did runbook_variable_mappings land in the input vars?
                        after = await client.request(
                            "GET",
                            f"/Webhook/{wid}",
                            params={"includedetails": "true"},
                            timeout=60,
                        )
                        ev["legs"][leg]["vars_after"] = {
                            v.get("key"): v.get("value") for v in after.get("input_variables") or []
                        }
            finally:
                await ph.cleanup_runbooks(client, created_runbooks, ev)
                for t in created_tickets:
                    try:
                        await client.request("DELETE", f"/Tickets/{t}", timeout=30)
                    except Exception as exc:  # noqa: BLE001
                        ev.setdefault("cleanup_errors", []).append(f"ticket {t}: {exc}")
                ev["ticket_cleanup"] = f"deleted {len(created_tickets)} probe ticket(s)"
    finally:
        ev["verdict"] = verdict(ev)
        out = ph.REPO / "ai_action_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"), "|", ev.get("ticket_cleanup"))
        print("evidence:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
