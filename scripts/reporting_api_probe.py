"""Live-confirm the reporting-API contract on the trial (POST /Report).

Three probes: a valid ad-hoc query (envelope), a view-incompatible query
(ORDER BY -> load_error?), and a `--` comment (rejected?). Plus the
ClientCache bootstrap endpoint the dispatch portal leans on.
POST /Report with _testonly executes SELECT-only SQL - read-only.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


async def main() -> None:
    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile("dev")
    out = {}

    async def run_sql(key: str, sql: str) -> None:
        try:
            body = await client.request(
                "POST",
                "/Report",
                json_body=[{"sql": sql, "_testonly": True, "_loadreportonly": True}],
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001
            out[key] = {"http_error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            return
        if isinstance(body, list) and body:
            body = body[0]
        report = (body or {}).get("report") or {}
        out[key] = {
            "columns": [c.get("name") for c in (body or {}).get("available_columns", [])[:8]],
            "rows": (report.get("rows") or [])[:3],
            "load_error": report.get("load_error"),
            "loaded": report.get("loaded"),
        }

    async with HaloClient(profile, profile_name="dev") as client:
        await run_sql("valid_select", "SELECT TOP 3 TABLE_NAME FROM INFORMATION_SCHEMA.TABLES")
        await run_sql(
            "order_by_probe",
            "SELECT TOP 3 TABLE_NAME FROM INFORMATION_SCHEMA.TABLES ORDER BY TABLE_NAME",
        )
        await run_sql("comment_probe", "SELECT 1 AS x -- trailing comment")
        await run_sql("semicolon_probe", "SELECT 1 AS x; SELECT 2 AS y")
        # ClientCache bootstrap (dispatch portal's first call)
        try:
            body = await client.request(
                "GET", "/ClientCache", params={"iscachebuild": "true"}, timeout=60
            )
            out["client_cache"] = {
                "keys": sorted(body.keys())[:20] if isinstance(body, dict) else type(body).__name__,
            }
        except Exception as exc:  # noqa: BLE001
            out["client_cache"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    print(json.dumps(out, indent=2, default=str))


asyncio.run(main())
