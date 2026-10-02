"""Probe pass 2: truly-bare and page-hints-only GETs for still-empty endpoints.

Pass 1 always attached count/page_size/pageinate hints (inherited from the
sweep); an endpoint that mishandles `pageinate` would then look empty no
matter what. This pass retries every empty endpoint with (a) NO params at
all and (b) count=100 only, and upgrades probe_results.json in place when
rows come back (winner params, uncapped row keys, real-id detail probe).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = REPO_ROOT / "probe_results.json"
sys.path.insert(0, str(REPO_ROOT / "src"))

REQUEST_TIMEOUT = 15.0
ATTEMPTS = ({}, {"count": "100"})


def describe(body) -> dict:
    if isinstance(body, list):
        rows, envelope = body, "<bare array>"
    elif isinstance(body, dict):
        envelope, rows = None, None
        for key, value in body.items():
            if isinstance(value, list):
                envelope, rows = key, value
                break
        if envelope is None:
            return {"envelope": "<object>", "rows": None, "row_keys": None,
                    "record_count": None}
    else:
        return {"envelope": f"<{type(body).__name__}>", "rows": None,
                "row_keys": None, "record_count": None}
    keys = None
    if rows and isinstance(rows[0], dict):
        keys = sorted(rows[0].keys())[:100]
    record_count = None
    if isinstance(body, dict):
        try:
            record_count = int(body.get("record_count"))
        except (TypeError, ValueError):
            record_count = None
    return {"envelope": envelope, "rows": len(rows), "row_keys": keys,
            "record_count": record_count, "_raw_first_id": (
                rows[0].get("id") if rows and isinstance(rows[0], dict) else None)}


async def run() -> int:
    from halocli.client import HaloClient
    from halocli.config import load_profile

    results = json.loads(PROBE_PATH.read_text(encoding="utf-8"))
    empties = [k for k, v in results.items() if not v.get("rows")]
    print(f"{len(empties)} empty endpoints to retry truly-bare")

    profile = load_profile("thomas")
    unlocked = []
    async with HaloClient(profile, profile_name="thomas") as client:
        for key in empties:
            path = results[key]["path"]
            attempt_log = []
            winner_desc = None
            winner_params = None
            for params in ATTEMPTS:
                try:
                    body = await client.request(
                        "GET", path, params=dict(params), timeout=REQUEST_TIMEOUT
                    )
                except Exception as exc:
                    attempt_log.append({
                        "params": params,
                        "status": getattr(exc, "status_code", None)
                        or f"error:{getattr(exc, 'category', type(exc).__name__)}",
                    })
                    continue
                desc = describe(body)
                attempt_log.append({"params": params, "status": 200, **{
                    k: v for k, v in desc.items() if k != "_raw_first_id"}})
                if desc.get("rows"):
                    winner_desc, winner_params = desc, params
                    break
            results[key]["pass2_attempts"] = attempt_log
            if winner_desc:
                first_id = winner_desc.pop("_raw_first_id", None)
                rec = results[key]
                rec.update({
                    "rows": winner_desc["rows"],
                    "winner_params": winner_params,
                    "envelope": winner_desc["envelope"],
                    "record_count": winner_desc["record_count"],
                    "row_keys": winner_desc["row_keys"],
                    "pass": 2,
                })
                if first_id is not None:
                    try:
                        detail = await client.request(
                            "GET", f"{path}/{first_id}", params={},
                            timeout=REQUEST_TIMEOUT,
                        )
                        rec["id_probe"] = {"id": first_id, "status": 200,
                                           "envelope": describe(detail)["envelope"]}
                    except Exception as exc:
                        rec["id_probe"] = {
                            "id": first_id,
                            "status": getattr(exc, "status_code", None)
                            or f"error:{getattr(exc, 'category', type(exc).__name__)}",
                        }
                unlocked.append((key, winner_desc["rows"], winner_params))
                print(f"  UNLOCKED {key}: rows={winner_desc['rows']} params={winner_params}",
                      flush=True)
            PROBE_PATH.write_text(
                json.dumps(dict(sorted(results.items())), indent=1,
                           ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    print(f"pass2 done: {len(unlocked)} unlocked of {len(empties)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
