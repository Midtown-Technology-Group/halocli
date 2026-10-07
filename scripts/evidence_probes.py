#!/usr/bin/env python3
"""Targeted evidence probes for the four remaining hold-for-evidence routes.

- GET /incomingemail with a120s budget (the15s sweep budget timed out15/15)
- GET /FaultsForecasting/{real id}, /CustomTableCSV/{real id},
  /DatabaseLookupConfirmation/{real id}: single-op segments with no list
  route, so the only way to evidence the detail route is an id taken from a
  sibling resource (real ids, never invented)

SAFETY: GET-only through HaloClient; no write path exists here.
Results print as JSON for the policy-note updates that follow.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def describe(body) -> dict:
    if isinstance(body, list):
        return {"shape": "bare-array", "rows": len(body)}
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(value, list):
                return {
                    "shape": f"envelope:{key}",
                    "rows": len(value),
                    "row_keys": (
                        sorted(value[0].keys())[:20]
                        if value and isinstance(value[0], dict)
                        else None
                    ),
                    "record_count": body.get("record_count"),
                }
        return {"shape": "object", "keys": sorted(body.keys())[:20]}
    return {"shape": type(body).__name__}


async def main() -> int:
    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile("thomas")
    out: dict[str, dict] = {}
    async with HaloClient(profile, profile_name="thomas") as client:
        # 1. incomingemail with a generous budget
        try:
            body = await client.request(
                "GET", "/incomingemail", params={"count": "25"}, timeout=120.0
            )
            out["GET /incomingemail"] = {"status": 200, **describe(body)}
        except Exception as exc:
            out["GET /incomingemail"] = {
                "status": getattr(exc, "status_code", None)
                or f"error:{getattr(exc, 'category', type(exc).__name__)}",
                "error": (str(exc) or type(exc).__name__)[:160],
                "budget_s": 120,
            }

        # helper: first id from a sibling list
        async def first_id(path: str) -> int | None:
            body = await client.request("GET", path, params={"count": "1"}, timeout=30)
            rows = body if isinstance(body, list) else None
            if rows is None and isinstance(body, dict):
                rows = next((v for v in body.values() if isinstance(v, list)), None)
            if rows and isinstance(rows[0], dict):
                return rows[0].get("id")
            return None

        # 2. FaultsForecasting/{real fault id} - use a known ticket id
        for path, id_source, fallback in (
            ("/FaultsForecasting/{id}", None, 162483),  # known ticket
            ("/CustomTableCSV/{id}", "/CustomTable", None),  # custom-tables list
            ("/DatabaseLookupConfirmation/{id}", "/DatabaseLookup", None),
        ):
            target_id = fallback
            if id_source is not None:
                target_id = await first_id(id_source)
            if target_id is None:
                out[f"GET {path}"] = {"status": "no-id-available", "source": id_source}
                continue
            probe = path.replace("{id}", str(target_id))
            try:
                body = await client.request("GET", probe, params={}, timeout=30)
                out[f"GET {path}"] = {
                    "status": 200,
                    "id_used": target_id,
                    "via": probe,
                    **describe(body),
                }
            except Exception as exc:
                out[f"GET {path}"] = {
                    "status": getattr(exc, "status_code", None)
                    or f"error:{getattr(exc, 'category', type(exc).__name__)}",
                    "id_used": target_id,
                    "via": probe,
                    "error": (str(exc) or type(exc).__name__)[:160],
                }

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
