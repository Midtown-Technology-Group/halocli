#!/usr/bin/env python3
"""Issue #73 items 4 + 6: action ticket-change-fields, dormant-route re-probes.

ITEM 4 (actions mutate tickets): create a probe ticket, POST an action
carrying `new_*` change fields (DTC's Action Endpoints reference) with
sendemail=false and dont_do_rules=true so nothing leaves the trial and
no automation fires, then GET the ticket to prove the mutation landed.
Cleanup: delete action + ticket.

ITEM 6 (dormant re-probes with doc-suggested shapes): /Users/me (docs:
"currently authenticated User", no params), /Users/onbehalf (undocumented
even in DTC's reference - bare + plausible params), /Appointment/Booking
(GET bare + id params; POST as an empty array = validation-rejection
evidence only, never a real booking).

    python scripts/issue73_probe.py [--profile dev]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

PROBE = "halocli-dev-probe"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    args = parser.parse_args()

    from halocli.client import HaloClient
    from halocli.config import load_profile
    from halocli.mirror import default_mirror_path
    import sqlite3

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")

        # mirror ids for change-field values
    mirror = default_mirror_path().with_name("mirror_dev.db")
    priority_id = appointment_id = None
    outcome_candidates: list[int] = []
    if mirror.exists():
        conn = sqlite3.connect(str(mirror))
        row = conn.execute(
            "SELECT data FROM mirror_rows WHERE resource='priorities' LIMIT 1"
        ).fetchone()
        if row:
            priority_id = json.loads(row[0]).get("priorityid") or json.loads(row[0]).get("id")
        # outcome candidates: least-side-effect first (row_key order starts at
        # a role-restricted "Re-Assign" which the agent cannot access)
        for (data,) in conn.execute(
            "SELECT data FROM mirror_rows WHERE resource='outcomes'"
        ).fetchall():
            d = json.loads(data)
            if d.get("id") in (13, 21, 24, 25, 22, 83, 11) and d.get("id") is not None:
                outcome_candidates.append(d["id"])
        row = conn.execute(
            "SELECT data FROM mirror_rows WHERE resource='appointments' LIMIT 1"
        ).fetchone()
        if row:
            appointment_id = json.loads(row[0]).get("id")
        conn.close()

    out: dict[str, Any] = {"tenant": host}
    async with HaloClient(profile, profile_name=args.profile) as client:
        # ---------------- item 6: dormant re-probes ----------------------
        async def probe(key: str, method: str, path: str, params=None, body=None) -> None:
            kwargs: dict[str, Any] = {"params": params or {}}
            if body is not None:
                kwargs["json_body"] = body
            try:
                result = await client.request(method, path, timeout=30, **kwargs)
                if isinstance(result, bytes):
                    out[key] = {"ok": True, "response": f"<{len(result)} bytes>"}
                else:
                    text = json.dumps(result, default=str)
                    out[key] = {"ok": True, "response": text[:300]}
            except Exception as exc:  # noqa: BLE001
                out[key] = {"ok": False, "error": str(exc)[:250]}

        await probe("users_me", "GET", "/Users/me")
        await probe("users_onbehalf_bare", "GET", "/Users/onbehalf")
        await probe(
            "users_onbehalf_uname",
            "GET",
            "/Users/onbehalf",
            params={"uname": "thomas@midtowntg.com"},
        )
        await probe("users_onbehalf_userid", "GET", "/Users/onbehalf", params={"user_id": "3"})
        await probe("booking_get_bare", "GET", "/Appointment/Booking")
        if appointment_id is not None:
            await probe(
                "booking_get_appointment_id",
                "GET",
                "/Appointment/Booking",
                params={"appointment_id": str(appointment_id)},
            )
        # empty array: a validation rejection is free evidence, no booking made
        await probe("booking_post_empty", "POST", "/Appointment/Booking", body=[])

        # ---------------- item 4: action change fields ---------------------
        # safety: sendemail false, sendsms false, dont_do_rules true
        ticket = None
        try:
            resp = await client.request(
                "POST",
                "/Tickets",
                json_body=[
                    {
                        "summary": f"{PROBE}-ticket for action change-fields",
                        "impact": 3,
                        "urgency": 3,
                    }
                ],
                timeout=45,
            )
            ticket = resp[0] if isinstance(resp, list) and resp else resp
        except Exception as exc:  # noqa: BLE001
            out["action_change_fields"] = {"error": f"ticket create: {str(exc)[:200]}"}
        if isinstance(ticket, dict) and ticket.get("id"):
            tid = ticket["id"]
            out["probe_ticket_id"] = tid
            before = {}
            try:
                t = await client.request("GET", f"/Tickets/{tid}", timeout=30)
                before = {
                    k: t.get(k)
                    for k in ("impact", "urgency", "priority_id", "status_id", "summary")
                }
            except Exception:  # noqa: BLE001
                pass
            action_payload: dict[str, Any] = {
                "ticket_id": tid,
                "note": f"{PROBE}: exercising documented new_* change fields",
                "sendemail": False,
                "sendsms": False,
                "dont_do_rules": True,
                "new_impact": 2,
                "new_urgency": 1,
            }
            if priority_id is not None:
                action_payload["new_priority"] = priority_id
            action = None
            attempts: list[str] = []
            for cand in outcome_candidates or [None]:
                trial = dict(action_payload)
                if cand is not None:
                    # real field per action rows: outcome_id (not actoutcome,
                    # which is only a GET filter param)
                    trial["outcome_id"] = cand
                try:
                    resp = await client.request("POST", "/Actions", json_body=[trial], timeout=45)
                    action = resp[0] if isinstance(resp, list) and resp else resp
                    attempts.append(f"outcome_id={cand}:ok")
                    break
                except Exception as exc:  # noqa: BLE001
                    attempts.append(f"outcome_id={cand}: {str(exc)[:160]}")
            out["action_outcome_attempts"] = attempts
            if action is None and not outcome_candidates:
                out["action_create_error"] = "no outcome candidates in mirror"
            entry: dict[str, Any] = {"before": before}
            if isinstance(action, dict):
                entry["action_id"] = action.get("id")
                entry["action_response_keys"] = sorted(action.keys())[:20]
            try:
                after_doc = await client.request("GET", f"/Tickets/{tid}", timeout=30)
                after = {
                    k: after_doc.get(k)
                    for k in ("impact", "urgency", "priority_id", "status_id", "summary")
                }
                entry["after"] = after
                entry["changed"] = {
                    k: [before.get(k), after.get(k)] for k in after if before.get(k) != after.get(k)
                }
                entry["mutation_landed"] = bool(entry["changed"])
            except Exception as exc:  # noqa: BLE001
                entry["verify_error"] = str(exc)[:200]
            out["action_change_fields"] = entry

            # cleanup: ACTION first (its DELETE needs the live ticket_id -
            # deleting the ticket first strands the action as an orphan),
            # then the ticket
            cleanup = []
            action_id = (action or {}).get("id") if isinstance(action, dict) else None
            if action_id is not None:
                try:
                    await client.request(
                        "DELETE",
                        f"/Actions/{action_id}",
                        params={"ticket_id": str(tid)},
                        timeout=30,
                    )
                    cleanup.append(f"action {action_id}: deleted (ticket_id param)")
                except Exception as exc:  # noqa: BLE001
                    cleanup.append(f"action {action_id}: DELETE failed {str(exc)[:120]}")
            try:
                await client.request("DELETE", f"/Tickets/{tid}", timeout=30)
                cleanup.append(f"ticket {tid}: deleted")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"ticket {tid}: DELETE failed {str(exc)[:120]}")
            out["cleanup"] = cleanup

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
