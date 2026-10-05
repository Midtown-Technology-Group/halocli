#!/usr/bin/env python3
"""Verify the RUNBOOK BUILD PATH: runbooks are /Webhook records with type=1.

The claim to verify (decoded from the trial's config SPA, chunks
ConfigPage_WebhookListParent-B-DWqp9J.js + index-BhEb1Eb1.js):

- list:   GET /Webhook?showall=true&type=1   (type=0 = ordinary webhooks)
- detail: GET /Webhook/{id}  carries the full definition (steps,
          runbook_variable_mappings, flow_chart_json, input_variables)
- create: POST /Webhook with the document + ``_is_new: true``
  (the UI's save path: ``postDetailsData("Webhook", {...s, steps: d,
          _is_new: true})``)
- import: the UI's "Import from JSON" parses the file, SANITIZES it
  (steps: id/fdid/chatprofile_id -> null; per action and step_condition
  id/chatprofile_id -> null; auto_action==6 drops auto_action_type),
  then posts the same _is_new create.
- delete: DELETE /Webhook/{id} (spec-backed)

Probe (trial only, prod-refusing, self-cleaning):
  A. minimal create {name, type:1, steps: [], _is_new:true} -> verify -> delete
  B. UI-exact round trip: export an existing runbook detail -> apply the
     import sanitizer -> POST as a NEW runbook -> verify steps survive ->
     delete.
  C. update leg: create minimal -> POST {id, name-suffix} -> verify the
     rename -> delete (the full-CUD gate for promotion).
  D. sweep shape: bare {name} create (no type/steps/_is_new) + {id,name}
     update + delete - the exact payloads dev_write_sweep will send once
     webhooks is write-promoted.
  E. partial-update semantics: {id, name} WITHOUT steps on an imported
     (4-step) runbook - full-replace wipe or merge?

    python scripts/runbook_build_probe.py [--profile dev]
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

PROBE = "haloclidevproberunbook"


def sanitize_for_import(doc: dict[str, Any]) -> dict[str, Any]:
    """The UI importjson transform, verbatim (WebhookListParent chunk)."""
    s = dict(doc)
    steps = list(s.get("steps") or [])
    for c in steps:
        c["id"] = None
        c["fdid"] = None
        c["chatprofile_id"] = None
        for u in c.get("actions") or []:
            u["id"] = None
            u["chatprofile_id"] = None
        for u in c.get("step_conditions") or []:
            u["id"] = None
            u["chatprofile_id"] = None
        if c.get("auto_action") == 6 and c.get("auto_action_type_guid"):
            c["auto_action_type"] = None
    s["steps"] = steps
    s["_is_new"] = True
    return s


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    args = parser.parse_args()

    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")

    out: dict[str, Any] = {"tenant": host}
    async with HaloClient(profile, profile_name=args.profile) as client:
        # ---------- A. minimal create ----------------------------------
        created_id: Any = None
        body_variants = [
            ("array", [{"name": PROBE, "type": 1, "steps": [], "_is_new": True}]),
            ("object", {"name": PROBE, "type": 1, "steps": [], "_is_new": True}),
        ]
        attempts: list[str] = []
        for label, body in body_variants:
            try:
                resp = await client.request("POST", "/Webhook", json_body=body, timeout=45)
                row = resp[0] if isinstance(resp, list) and resp else resp
                created_id = row.get("id") if isinstance(row, dict) else None
                attempts.append(f"{label}: ok id={created_id}")
                out["minimal_create_shape"] = label
                break
            except Exception as exc:  # noqa: BLE001
                attempts.append(f"{label}: {str(exc)[:200]}")
        out["minimal_attempts"] = attempts

        if created_id is not None:
            try:
                doc = await client.request(
                    "GET",
                    f"/Webhook/{created_id}",
                    params={"includedetails": "true"},
                    timeout=45,
                )
                out["minimal_verify"] = {
                    "name_ok": isinstance(doc, dict) and doc.get("name") == PROBE,
                    "type": doc.get("type") if isinstance(doc, dict) else None,
                    "has_steps_key": isinstance(doc, dict) and "steps" in doc,
                }
            except Exception as exc:  # noqa: BLE001
                out["minimal_verify"] = {"error": str(exc)[:200]}
            # a type=1 list check (does it show up as a runbook?)
            try:
                body = await client.request(
                    "GET",
                    "/Webhook",
                    params={"showall": "true", "type": "1", "count": "100"},
                    timeout=45,
                )
                rows = (
                    body
                    if isinstance(body, list)
                    else next((v for v in body.values() if isinstance(v, list)), [])
                )
                out["appears_in_runbook_list"] = any(r.get("id") == created_id for r in rows)
            except Exception as exc:  # noqa: BLE001
                out["appears_in_runbook_list"] = f"error: {str(exc)[:160]}"
            try:
                await client.request("DELETE", f"/Webhook/{created_id}", timeout=30)
                try:
                    await client.request("GET", f"/Webhook/{created_id}", timeout=30)
                    out["minimal_delete"] = "STILL READABLE"
                except Exception:  # noqa: BLE001
                    out["minimal_delete"] = "deleted (clean)"
            except Exception as exc:  # noqa: BLE001
                out["minimal_delete"] = f"DELETE failed {str(exc)[:160]}"

        # ---------- B. UI-exact export -> sanitize -> import -------------
        # pick an existing runbook with steps
        source_id = None
        source_steps = 0
        try:
            body = await client.request(
                "GET",
                "/Webhook",
                params={"showall": "true", "type": "1", "count": "10"},
                timeout=45,
            )
            rows = (
                body
                if isinstance(body, list)
                else next((v for v in body.values() if isinstance(v, list)), [])
            )
            for r in rows:
                try:
                    doc = await client.request(
                        "GET",
                        f"/Webhook/{r.get('id')}",
                        params={"includedetails": "true"},
                        timeout=45,
                    )
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(doc, dict) and doc.get("steps"):
                    source_id = doc.get("id")
                    source_steps = len(doc["steps"])
                    break
        except Exception as exc:  # noqa: BLE001
            out["export_error"] = str(exc)[:200]
        out["source"] = {"id": source_id, "steps": source_steps}

        if source_id is not None:
            try:
                doc = await client.request(
                    "GET",
                    f"/Webhook/{source_id}",
                    params={"includedetails": "true"},
                    timeout=45,
                )
                imported = sanitize_for_import(doc)
                imported["name"] = PROBE + "-imported"
                resp = await client.request("POST", "/Webhook", json_body=[imported], timeout=60)
                row = resp[0] if isinstance(resp, list) and resp else resp
                new_id = row.get("id") if isinstance(row, dict) else None
                out["import_create"] = {"id": new_id}
                if new_id is not None:
                    check = await client.request(
                        "GET",
                        f"/Webhook/{new_id}",
                        params={"includedetails": "true"},
                        timeout=45,
                    )
                    got_steps = check.get("steps") if isinstance(check, dict) else None
                    out["import_verify"] = {
                        "name_ok": isinstance(check, dict)
                        and check.get("name") == PROBE + "-imported",
                        "type": check.get("type") if isinstance(check, dict) else None,
                        "steps_imported": len(got_steps) if isinstance(got_steps, list) else None,
                        "steps_source": source_steps,
                        "source_id_unchanged": (
                            source_id != new_id
                            and isinstance(check, dict)
                            and str(check.get("id")) != str(source_id)
                        ),
                    }
                    # E: PARTIAL update semantics - does {id, name} WITHOUT
                    # steps wipe the imported steps (full-replace) or merge?
                    try:
                        resp = await client.request(
                            "POST",
                            "/Webhook",
                            json_body=[{"id": new_id, "name": PROBE + "-renamed"}],
                            timeout=45,
                        )
                        row = resp[0] if isinstance(resp, list) and resp else resp
                        renamed = isinstance(row, dict) and row.get("name") == PROBE + "-renamed"
                        after = await client.request(
                            "GET",
                            f"/Webhook/{new_id}",
                            params={"includedetails": "true"},
                            timeout=45,
                        )
                        after_steps = after.get("steps") if isinstance(after, dict) else None
                        out["partial_update"] = {
                            "renamed": renamed,
                            "steps_after": len(after_steps)
                            if isinstance(after_steps, list)
                            else None,
                            "steps_before": source_steps,
                            "steps_survived": (
                                isinstance(after_steps, list) and len(after_steps) == source_steps
                            ),
                        }
                    except Exception as exc:  # noqa: BLE001
                        out["partial_update"] = {"error": str(exc)[:260]}
                    # cleanup
                    try:
                        await client.request("DELETE", f"/Webhook/{new_id}", timeout=30)
                        try:
                            await client.request("GET", f"/Webhook/{new_id}", timeout=30)
                            out["import_delete"] = "STILL READABLE"
                        except Exception:  # noqa: BLE001
                            out["import_delete"] = "deleted (clean)"
                    except Exception as exc:  # noqa: BLE001
                        out["import_delete"] = f"DELETE failed {str(exc)[:160]}"
                    # source must still exist untouched
                    try:
                        sdoc = await client.request(
                            "GET",
                            f"/Webhook/{source_id}",
                            params={"includedetails": "true"},
                            timeout=45,
                        )
                        out["source_intact"] = (
                            isinstance(sdoc, dict) and len(sdoc.get("steps") or []) == source_steps
                        )
                    except Exception as exc:  # noqa: BLE001
                        out["source_intact"] = f"error: {str(exc)[:160]}"
            except Exception as exc:  # noqa: BLE001
                out["import_create"] = {"error": str(exc)[:300]}

        # ---------- C. update leg (full-CUD gate) ------------------------
        # create minimal -> POST {id, name+suffix} -> verify rename -> delete
        upd_id = None
        try:
            resp = await client.request(
                "POST",
                "/Webhook",
                json_body=[{"name": PROBE + "upd", "type": 1, "steps": [], "_is_new": True}],
                timeout=45,
            )
            row = resp[0] if isinstance(resp, list) and resp else resp
            upd_id = row.get("id") if isinstance(row, dict) else None
        except Exception as exc:  # noqa: BLE001
            out["update_leg"] = f"create failed: {str(exc)[:180]}"
        if upd_id is not None:
            try:
                resp = await client.request(
                    "POST",
                    "/Webhook",
                    json_body=[{"id": upd_id, "name": PROBE + "updv2", "type": 1, "steps": []}],
                    timeout=45,
                )
                row = resp[0] if isinstance(resp, list) and resp else resp
                back = row.get("name") if isinstance(row, dict) else None
                out["update_leg"] = {"renamed_to": back, "ok": back == PROBE + "updv2"}
            except Exception as exc:  # noqa: BLE001
                out["update_leg"] = {"error": str(exc)[:260]}
            try:
                await client.request("DELETE", f"/Webhook/{upd_id}", timeout=30)
                out["update_cleanup"] = "deleted"
            except Exception as exc:  # noqa: BLE001
                out["update_cleanup"] = f"DELETE failed {str(exc)[:140]}"

        # ---------- D. sweep shape: {name} only, no _is_new ---------------
        # The dev_write_sweep build() will post exactly this for a promoted
        # resource: required_create_fields=("name",) and nothing else.
        bare_id = None
        try:
            resp = await client.request(
                "POST",
                "/Webhook",
                json_body=[{"name": PROBE + "bare"}],
                timeout=45,
            )
            row = resp[0] if isinstance(resp, list) and resp else resp
            bare_id = row.get("id") if isinstance(row, dict) else None
            out["bare_shape"] = {
                "id": bare_id,
                "type": row.get("type") if isinstance(row, dict) else None,
            }
        except Exception as exc:  # noqa: BLE001
            out["bare_shape"] = {"error": str(exc)[:300]}
        if bare_id is not None:
            try:
                resp = await client.request(
                    "POST",
                    "/Webhook",
                    json_body=[{"id": bare_id, "name": PROBE + "barev2"}],
                    timeout=45,
                )
                row = resp[0] if isinstance(resp, list) and resp else resp
                out["bare_shape"]["update_ok"] = (
                    isinstance(row, dict) and row.get("name") == PROBE + "barev2"
                )
            except Exception as exc:  # noqa: BLE001
                out["bare_shape"]["update_error"] = str(exc)[:260]
            try:
                await client.request("DELETE", f"/Webhook/{bare_id}", timeout=30)
                out["bare_shape"]["cleanup"] = "deleted"
            except Exception as exc:  # noqa: BLE001
                out["bare_shape"]["cleanup"] = f"DELETE failed {str(exc)[:160]}"

        # D2: the sweep-configured shape (extras = {type:1, steps:[]}), no
        # _is_new, no id on create - this is exactly what dev_write_sweep will
        # post once webhooks has extras() configured.
        try:
            resp = await client.request(
                "POST",
                "/Webhook",
                json_body=[{"name": PROBE + "sweep", "type": 1, "steps": []}],
                timeout=45,
            )
            row = resp[0] if isinstance(resp, list) and resp else resp
            sw_id = row.get("id") if isinstance(row, dict) else None
            out["sweep_shape"] = {"create_ok": sw_id is not None, "id": sw_id}
        except Exception as exc:  # noqa: BLE001
            out["sweep_shape"] = {"create_error": str(exc)[:300]}
            sw_id = None
        if sw_id is not None:
            try:
                resp = await client.request(
                    "POST",
                    "/Webhook",
                    json_body=[{"id": sw_id, "name": PROBE + "sweepv2", "type": 1, "steps": []}],
                    timeout=45,
                )
                row = resp[0] if isinstance(resp, list) and resp else resp
                out["sweep_shape"]["update_ok"] = (
                    isinstance(row, dict) and row.get("name") == PROBE + "sweepv2"
                )
            except Exception as exc:  # noqa: BLE001
                out["sweep_shape"]["update_error"] = str(exc)[:260]
            try:
                await client.request("DELETE", f"/Webhook/{sw_id}", timeout=30)
                out["sweep_shape"]["cleanup"] = "deleted"
            except Exception as exc:  # noqa: BLE001
                out["sweep_shape"]["cleanup"] = f"DELETE failed {str(exc)[:160]}"

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
