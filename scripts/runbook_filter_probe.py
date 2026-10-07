#!/usr/bin/env python3
"""Two guard/filter proofs (trial, self-cleaning, crash-safe):

PART B - value criteria for bare guards (runbook vars, manual fire):
  V30: criteria30 "Does not have a value" on <<label>> (str):
       label "" -> MET (empty = no value); label "x" -> NOTMET -> Fail.
  Vint: criteria5 (Greater than) value_int0 on <<limit>> - int bare
       truthiness as ">0": limit=10 -> MET; limit=0 -> NOTMET.

PART C - THE SUBSCRIBER-FILTER PORT (binding-level conditions):
  POST /Notification with inline `conditions` (AI Triage's production
  shape: rule_id parent, tablename faults, filter_type 2) -> the run
  does NOT START for a non-matching ticket, and STARTS for a matching
  one. This is Bifrost's `event.body.ticket.reportedby == VENDOR`
  subscriber filter, ported to Halo natively (vs the step-guard early
  exit proven in ticket_guard_evidence.json).

    python scripts/runbook_filter_probe.py [--profile dev] [--save PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable


_EP_Webhook = "/Webhook"


def _safe_path(p: str | Path) -> Path:
    """Canonicalize a CLI-supplied path before touching the disk (S8707)."""
    return Path(p).expanduser().resolve()


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PROBE = "haloclidevprobefilter"
SENDER = "noreply@voicemail.goto.com"


def edge(action_type: int, action_name: str, start: int, end: int, seq: int) -> dict:
    return {
        "action_type": action_type,
        "action_id": -action_type,
        "action_name": action_name,
        "start_step": start,
        "end_step": end,
        "seq": seq,
        "use_work_hours": True,
        "approval_result": 1 if seq == 1 else 0,
        "chat_selection_order": 1,
    }


def base_criterion(fieldname: str, step_id: int | None = None) -> dict:
    return {
        "id": None,
        "rule_id": 0,
        "qualification_criteria_id": 0,
        "fieldname": fieldname,
        "value_type_id": -1,
        "value_int": 0,
        "value_string": "",
        "partialmatch": False,
        "matchseparatedvalues": False,
        "tablename": "runbookvariable",
        "flowsubdetails_criteria_id": 0,
        "use": 0,
        "chatprofile_id": None,
        "chatprofile_flow_seq": step_id or 1,
        "timezonestring": "",
        "match_after_start": False,
        "match_after_target": False,
        "eventrule_id": 0,
        "flow_id": 0,
        "flow_type": 0,
        "flow_seq": 0,
    }


def brief(r: dict) -> dict:
    return {
        k: r.get(k)
        for k in ("id", "status", "error", "steps_executed", "runbook_step", "execution_time")
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--save", default="", help="write result JSON here (evidence)")
    args = parser.parse_args()

    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: production host")

    out: dict[str, Any] = {"tenant": host}
    tickets: list[int] = []
    bindings: list[int] = []

    async with HaloClient(profile, profile_name=args.profile) as client:

        async def safe(factory: Callable[[], Awaitable[Any]], attempts: int = 3) -> Any:
            last: Exception | None = None
            for i in range(attempts):
                try:
                    return await factory()
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    await asyncio.sleep(2 + i * 2)
            raise last  # type: ignore[misc]

        async def runlog_list() -> list[dict]:
            log = await safe(
                lambda: client.request("GET", "/Automation", params={"count": "1000"}, timeout=45)
            )
            return (
                log
                if isinstance(log, list)
                else next((v for v in log.values() if isinstance(v, list)), [])
            )

        async def wait_run(before: set, wid: str, ticks: int = 15) -> dict | None:
            rid = None
            hit: dict | None = None
            rows: list[dict] = []
            for _ in range(ticks):
                await asyncio.sleep(3)
                if rid is None:
                    try:
                        rows = await runlog_list()
                    except Exception:  # noqa: BLE001
                        continue
                    mine = [
                        r for r in rows if r.get("id") not in before and r.get("runbook_id") == wid
                    ]
                    if mine:
                        rid = mine[0].get("id")
                    else:
                        top = max((r.get("id") or 0) for r in rows) if rows else 0
                        for cand in range(top, max(top - 8, 0), -1):
                            try:
                                d2 = await client.request("GET", f"/Automation/{cand}", timeout=20)
                            except Exception:  # noqa: BLE001
                                continue
                            if (
                                isinstance(d2, dict)
                                and d2.get("runbook_id") == wid
                                and d2.get("id") not in before
                            ):
                                rid = d2.get("id")
                                break
                if rid is None:
                    continue
                try:
                    snap = await client.request("GET", f"/Automation/{rid}", timeout=20)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(snap, dict):
                    hit = snap
                    if (snap.get("status") or 1) != 1 or (snap.get("error") or ""):
                        break
            return hit

        async def sweep_cleanup() -> list[str]:
            done = []
            try:
                body = await safe(
                    lambda: client.request(
                        "GET",
                        _EP_Webhook,
                        params={"showall": "true", "type": "1", "count": "200"},
                        timeout=45,
                    )
                )
                rows = (
                    body
                    if isinstance(body, list)
                    else next((v for v in body.values() if isinstance(v, list)), [])
                )
                for r in rows:
                    if PROBE in str(r.get("name", "")):
                        try:
                            await client.request("DELETE", f"/Webhook/{r.get('id')}", timeout=30)
                            done.append(f"runbook {r.get('name')}")
                        except Exception as exc:  # noqa: BLE001
                            done.append(f"{r.get('name')}: {str(exc)[:80]}")
            except Exception as exc:  # noqa: BLE001
                done.append(f"sweep failed: {str(exc)[:120]}")
            return done

        try:
            # ============ PART B: value criteria (manual fire) ============
            async def value_leg(
                crit_field: str,
                crit_type: int,
                value_type: str,
                value_int: int,
                value_string: str,
                values: dict[str, str],
                expect: str,
                seq: int,
            ) -> dict[str, Any]:
                """Bake values at CREATE - the proven pattern.

                The earlier update-round attempt failed, but that run ALSO
                had unwrapped fieldnames (the real bug) - update-round
                stays untested; note the fieldname wrap: runbookvariable
                criteria MUST be <<var>>, plain names are for the faults
                table (this probe's lesson, trial-confirmed).
                """
                crit = base_criterion(f"<<{crit_field}>>", 1)  # <<var>> = runbookvariable
                crit.update(
                    {
                        "type": crit_type,
                        "value_type": value_type,
                        "value_int": value_int,
                        "value_string": value_string,
                    }
                )
                name = f"{PROBE}-value{seq}"
                try:
                    resp = await client.request(
                        "POST",
                        _EP_Webhook,
                        json_body=[
                            {
                                "name": name,
                                "type": 1,
                                "active": True,
                                "runbook_start_type": 1,
                                "inbound_authentication_type": 0,
                                "input_variables": [
                                    {
                                        "id": None,
                                        "key": "label",
                                        "value": values.get("label", "x"),
                                        "data_type": 2,
                                        "description": "v",
                                    },
                                    {
                                        "id": None,
                                        "key": "limit",
                                        "value": values.get("limit", "10"),
                                        "data_type": 3,
                                        "description": "v",
                                    },
                                ],
                                "steps": [
                                    {
                                        "step_id": 1,
                                        "name": "guard",
                                        "steptype": 1,
                                        "auto_action": 6,
                                        "isstart": True,
                                        "allow_all_statuses": True,
                                        "step_conditions": [crit],
                                        "actions": [
                                            edge(12, "Condition met", 1, 3, 1),
                                            edge(12, "Condition not met", 1, 4, 2),
                                        ],
                                    },
                                    {
                                        "step_id": 2,
                                        "name": "Unreached",
                                        "steptype": 2,
                                        "auto_action": 21,
                                        "duration": 0,
                                        "allow_all_statuses": True,
                                        "actions": [edge(32, "Sleep Finished", 2, 3, 1)],
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
                                ],
                            }
                        ],
                        timeout=60,
                    )
                    row = resp[0] if isinstance(resp, list) else resp
                    rid_ = str(row.get("id"))
                except Exception as exc:  # noqa: BLE001
                    return {"create_error": str(exc)[:300]}

                result: dict[str, Any] = {"expect": expect}
                try:
                    fdoc = await client.request(
                        "GET",
                        f"/Webhook/{rid_}",
                        params={"includedetails": "true"},
                        timeout=45,
                    )
                    s1 = next(s for s in fdoc.get("steps") or [] if s.get("step_id") == 1)
                    pc = (s1.get("step_conditions") or [{}])[0]
                    result["persisted_criterion"] = {
                        k: pc.get(k)
                        for k in (
                            "id",
                            "type",
                            "fieldname",
                            "value_type",
                            "value_int",
                            "value_string",
                            "tablename",
                        )
                    }
                    result["persisted_inputs"] = {
                        v.get("key"): v.get("value") for v in fdoc.get("input_variables") or []
                    }
                except Exception as exc:  # noqa: BLE001
                    result["persisted_error"] = str(exc)[:200]
                before = {x.get("id") for x in await runlog_list()}
                try:
                    await client.request(
                        "POST",
                        f"/Automation/{rid_}",
                        json_body={"formCollection": []},
                        timeout=120,
                    )
                except Exception as exc:  # noqa: BLE001
                    result["fire_error"] = str(exc)[:200]
                else:
                    hit = await wait_run(before, rid_)
                    result["run"] = brief(hit) if hit else "no run"
                try:
                    await client.request("DELETE", f"/Webhook/{rid_}", timeout=30)
                except Exception:  # noqa: BLE001
                    pass
                return result

            # V30: "Does not have a value" - "" should be MET, "x" NOTMET
            out["V30_empty"] = await value_leg(
                "label",
                30,
                "string",
                0,
                "",
                {"label": ""},
                "met -> Success (exec1)",
                1,
            )
            out["V30_set"] = await value_leg(
                "label",
                30,
                "string",
                0,
                "",
                {"label": "x"},
                "notmet -> Fail",
                2,
            )
            # Vint: Greater than 0 - int bare truthiness
            out["Vint_10"] = await value_leg(
                "limit",
                5,
                "int",
                0,
                "",
                {"limit": "10"},
                "met -> Success (exec1)",
                3,
            )
            out["Vint_0"] = await value_leg(
                "limit",
                5,
                "int",
                0,
                "",
                {"limit": "0"},
                "notmet -> Fail",
                4,
            )

            # ============ PART C: subscriber-filter port =============
            # resolve reportedby's FieldInfo id (templates carry fieldid)
            reportedby_fieldid = None
            try:
                fb = await client.request("GET", "/FieldInfo", params={"count": "500"}, timeout=45)
                frows = (
                    fb
                    if isinstance(fb, list)
                    else next((v for v in fb.values() if isinstance(v, list)), [])
                )
                for fr in frows:
                    if str(fr.get("name") or "").lower() == "reportedby":
                        reportedby_fieldid = fr.get("id")
                        break
            except Exception as exc:  # noqa: BLE001
                out["fieldinfo_error"] = str(exc)[:160]
            out["reportedby_fieldid"] = reportedby_fieldid

            # runbook: hop -> Success
            resp = await client.request(
                "POST",
                _EP_Webhook,
                json_body=[
                    {
                        "name": f"{PROBE}-runbook",
                        "type": 1,
                        "active": True,
                        "runbook_start_type": 1,
                        "inbound_authentication_type": 0,
                        "steps": [
                            {
                                "step_id": 1,
                                "name": "Hop",
                                "steptype": 2,
                                "auto_action": 21,
                                "duration": 0,
                                "isstart": True,
                                "allow_all_statuses": True,
                                "actions": [edge(32, "Sleep Finished", 1, 2, 1)],
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
                        ],
                    }
                ],
                timeout=60,
            )
            row = resp[0] if isinstance(resp, list) else resp
            wid = str(row.get("id"))
            out["runbook"] = wid

            # binding WITH the subscriber condition (AI Triage's shape)
            cond = {
                "id": None,
                "rule_id": None,
                "fieldname": "reportedby",
                "fieldid": reportedby_fieldid,
                "change_context": 0,
                "type": 0,  # Is equal to
                "value_int": 0,
                "value_string": SENDER,
                "value_display": SENDER,
                "value_type": "string",
                "tablename": "faults",
            }
            try:
                r = await client.request(
                    "POST",
                    "/Notification",
                    json_body=[
                        {
                            "guid": None,
                            "eventno": 3,
                            "name": "New Ticket Logged",
                            "type": -2,
                            "delivery_method": 6,
                            "agent_id": 0,
                            "webhook_id": wid,
                            "filter_type": 2,
                            "condition_count": 1,
                            "conditions": [cond],
                        }
                    ],
                    timeout=60,
                )
                nrow = r[0] if isinstance(r, list) and r else r
                bindings.append(nrow.get("id"))
                out["binding"] = {"id": nrow.get("id")}
            except Exception as exc:  # noqa: BLE001
                out["binding"] = {"error": str(exc)[:400]}
                print(json.dumps(out, indent=2, default=str))
                return 0

            # verify conditions persisted
            try:
                ndoc = await client.request(
                    "GET",
                    f"/Notification/{nrow.get('id')}",
                    params={"includedetails": "true"},
                    timeout=45,
                )
                out["conditions_persisted"] = {
                    "count": ndoc.get("condition_count"),
                    "rows": [
                        {
                            k: c.get(k)
                            for k in ("id", "fieldname", "type", "value_string", "tablename")
                        }
                        for c in ndoc.get("conditions") or []
                    ],
                }
            except Exception as exc:  # noqa: BLE001
                out["conditions_persisted"] = {"error": str(exc)[:200]}

            # ticket A: NON-matching sender -> filter must BLOCK the run
            before_a = {x.get("id") for x in await runlog_list()}
            ta = await client.request(
                "POST",
                "/Tickets",
                json_body=[
                    {"summary": f"{PROBE} filtered out", "reportedby": "someone@example.com"}
                ],
                timeout=60,
            )
            trow = ta[0] if isinstance(ta, list) else ta
            if trow.get("id"):
                tickets.append(trow.get("id"))
            run_a = await wait_run(before_a, wid, ticks=10)
            out["blocked_leg"] = {
                "ticket": trow.get("id"),
                "expect": "NO run (filter blocks)",
                "run": brief(run_a) if run_a else "no run observed (correct)",
            }

            # ticket B: matching sender -> filter must ALLOW the run
            before_b = {x.get("id") for x in await runlog_list()}
            tb = await client.request(
                "POST",
                "/Tickets",
                json_body=[{"summary": f"{PROBE} passes filter", "reportedby": SENDER}],
                timeout=60,
            )
            trowb = tb[0] if isinstance(tb, list) else tb
            if trowb.get("id"):
                tickets.append(trowb.get("id"))
            run_b = await wait_run(before_b, wid)
            out["allowed_leg"] = {
                "ticket": trowb.get("id"),
                "expect": "run starts, status2",
                "run": brief(run_b) if run_b else "no run observed",
            }

            blocked = out["blocked_leg"]["run"] == "no run observed (correct)"
            allowed = isinstance(run_b, dict) and (run_b.get("status") or 0) == 2
            out["verdict"] = (
                "PROVEN: non-matching ticket blocked at the binding; matching ticket ran"
                if blocked and allowed
                else "INCONCLUSIVE - inspect legs"
            )
        finally:
            for bid in [b for b in bindings if b]:
                try:
                    await client.request("DELETE", f"/Notification/{bid}", timeout=30)
                except Exception:  # noqa: BLE001
                    out.setdefault("cleanup_errors", []).append(f"binding {bid}")
            for tid in tickets:
                try:
                    await client.request("DELETE", f"/Tickets/{tid}", timeout=30)
                except Exception:  # noqa: BLE001
                    out.setdefault("cleanup_errors", []).append(f"ticket {tid}")
            out["cleanup"] = await sweep_cleanup()

    payload = json.dumps(out, indent=2, default=str)
    if args.save:
        _safe_path(args.save).write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
