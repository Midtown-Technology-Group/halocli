#!/usr/bin/env python3
"""Custom-tables FULL coverage probe: data rows (_add_rows) + field-info link.

Two open questions from the sweep:

1. DATA ROWS: the PS module's recipe is POST /CustomTable with
   [{id: <table_id>, _add_rows: [...]}] - no /CustomTableData endpoint
   exists in any swagger we have. End-to-end on the trial: create a probe
   table -> _add_rows -> read back -> delete (rows cascade).

2. FIELD-INFO LINK: FieldInfo create rejected every integer link value
   with "No matching custom table". The schema (components/FieldInfo)
   carries table_id + table_guid + table_name as a trio - try that against
   a FRESH API-created table (its db_name is server-derived "CT...", the
   marker of a real custom table), then bounded fallbacks. Failures are
   free evidence (validation rejections make no changes).

Name rule learned en route: hyphens are REJECTED in custom-table names
("Invalid Name") - the probe name is alnum-only.

    python scripts/custom_tables_full.py [--profile dev]
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

    table_ids: list[int] = []
    mirror = default_mirror_path().with_name("mirror_dev.db")
    if mirror.exists():
        conn = sqlite3.connect(str(mirror))
        for (raw,) in conn.execute(
            "SELECT data FROM mirror_rows WHERE resource='custom-tables'"
        ).fetchall():
            d = json.loads(raw)
            if isinstance(d.get("id"), int):
                table_ids.append(d["id"])
        conn.close()

    out: dict[str, Any] = {"tenant": host, "table_ids_sample": table_ids[:6]}
    async with HaloClient(profile, profile_name=args.profile) as client:
        # ---------- 1. create a fresh probe table --------------------------
        table_id = None
        table_row: dict[str, Any] = {}
        try:
            resp = await client.request(
                "POST",
                "/CustomTable",
                # hyphens rejected ("Invalid Name") - alnum only
                json_body=[{"name": "haloclidevprobectable", "columns": 1, "table_type": 0}],
                timeout=45,
            )
            created = resp[0] if isinstance(resp, list) else resp
            table_row = created if isinstance(created, dict) else {}
            table_id = table_row.get("id")
            out["table_create"] = {
                "id": table_id,
                "db_name": table_row.get("db_name"),
                "ok": table_id is not None,
            }
        except Exception as exc:  # noqa: BLE001
            out["table_create"] = {"error": str(exc)[:300]}

        # ---------- 2. data rows via _add_rows -----------------------------
        if table_id is not None:
            try:
                resp = await client.request(
                    "POST",
                    "/CustomTable",
                    json_body=[
                        {
                            "id": table_id,
                            "_add_rows": [
                                {"column_1": f"{PROBE}-row-1"},
                                {"column_1": f"{PROBE}-row-2"},
                            ],
                        }
                    ],
                    timeout=45,
                )
                out["add_rows"] = {"ok": True, "response": str(resp)[:250]}
            except Exception as exc:  # noqa: BLE001
                out["add_rows"] = {"error": str(exc)[:300]}
            # read back: do rows show in the detail?
            try:
                doc = await client.request(
                    "GET",
                    f"/CustomTable/{table_id}",
                    params={"includedetails": "true"},
                    timeout=30,
                )
                blob = json.dumps(doc, default=str)
                out["rows_readable"] = {
                    "has_probe_row": PROBE in blob,
                    "detail_keys": sorted(doc.keys())[:25] if isinstance(doc, dict) else [],
                }
            except Exception as exc:  # noqa: BLE001
                out["rows_readable"] = {"error": str(exc)[:200]}

        # ---------- 3. FieldInfo link against the FRESH table --------------
        attempts: list[str] = []
        field_info_id = None
        trio = {
            "table_id": table_row.get("id"),
            "table_guid": table_row.get("guid"),
            "table_name": table_row.get("name"),
        }
        candidates: list[tuple[str, dict[str, Any]]] = []
        if trio["table_id"] is not None:
            candidates.append(("trio id+guid+name", dict(trio)))
            candidates.append(
                ("trio+customextratableid", {**trio, "customextratableid": trio["table_id"]})
            )
            candidates.append(("table_guid only", {"table_guid": trio["table_guid"]}))
            candidates.append(
                ("customextratableid=guid", {"customextratableid": trio["table_guid"]})
            )
        if table_ids:
            candidates.append(
                ("legacy customextratableid=int", {"customextratableid": table_ids[0]})
            )
        for label, link in candidates:
            payload: dict[str, Any] = {"name": f"{PROBE}fi"}
            payload.update(link)
            try:
                resp = await client.request("POST", "/FieldInfo", json_body=[payload], timeout=45)
                created = resp[0] if isinstance(resp, list) else resp
                fid = created.get("id") if isinstance(created, dict) else None
                attempts.append(f"{label}: ok id={fid}")
                field_info_id = fid
                out["field_info_link"] = {"winning_shape": label, "link": link, "id": fid}
                break
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                attempts.append(f"{label}: {msg[:170]}")
        out["field_info_attempts"] = attempts

        # ---------- 4. cleanup ----------------------------------------------
        cleanup = []
        if field_info_id is not None:
            try:
                await client.request("DELETE", f"/FieldInfo/{field_info_id}", timeout=30)
                cleanup.append(f"field-info {field_info_id}: deleted")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"field-info {field_info_id}: DELETE failed {str(exc)[:120]}")
        if table_id is not None:
            try:
                await client.request("DELETE", f"/CustomTable/{table_id}", timeout=30)
                try:
                    await client.request("GET", f"/CustomTable/{table_id}", timeout=30)
                    cleanup.append(f"table {table_id}: still readable?!")
                except Exception:  # noqa: BLE001
                    cleanup.append(f"table {table_id}: deleted (rows cascade)")
            except Exception as exc:  # noqa: BLE001
                cleanup.append(f"table {table_id}: DELETE failed {str(exc)[:140]}")
        out["cleanup"] = cleanup

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
