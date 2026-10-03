#!/usr/bin/env python3
"""Targeted read-only probes for backlog GETs the default sweep could not read.

The Phase-1 sweep found 54 collections answering 200 with ZERO rows under
default params (scope-guarded, like /CRMNote's unfiltered record_count=0) and
3 more that hit ReadTimeout. This runner retries them with REAL scope params
(documented spec params first, then known-good tenant ids) and records the
evidence that promotions bind to: winning params, envelope, row keys (UNCAPPED
- the sweep's 30-key cap hid columns before), and a real-id detail probe.

SAFETY (structural, not advisory):
  - only ever issues method="GET" through HaloClient; no code path can send
    POST/PUT/PATCH/DELETE, so a write against production is impossible
  - every attempt is bounded (count/page_size/pageinate hints) with a 15s
    timeout; endpoints are probed sequentially with a pause between batches
  - path params are filled only with ids taken from a successful list probe
    on the same endpoint (never invented)

Resumable: results accumulate in probe_results.json; recorded endpoints are
skipped unless --force is given.

    python scripts/get_backlog_probes.py [--batch-size 25] [--pause 0.5] [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPO_ROOT / "coverage_ledger.json"
SPEC_PATH = REPO_ROOT / "src" / "halocli" / "spec" / "halo_openapi.json"
SWEEP_PATH = REPO_ROOT / "sweep_results.json"
RESULTS_PATH = REPO_ROOT / "probe_results.json"

sys.path.insert(0, str(REPO_ROOT / "src"))

BOUND_PARAMS = {"count": "1", "page_size": "1", "pageinate": "true"}
REQUEST_TIMEOUT = 15.0

# Known-good ids on this tenant (evidence from live sessions): client 625 is
# MTG Test Customer, agent 37 is the acting agent, toplevel 1 filters
# /CRMNote into rows, 162483/162484 are real tickets created by the
# appointment-completion chain.
KNOWN_VALUES: dict[str, list[str]] = {
    "client_id": ["625", "1"],
    "toplevel_id": ["1"],
    "agent_id": ["37"],
    "fault_id": ["162483", "1"],
    "ticket_id": ["162483", "1"],
    "site_id": ["626", "1"],
    "supplier_id": ["1"],
    "department_id": ["1"],
}

# Nested GETs on covered segments already answered 200 in the sweep; re-verify
# their envelope here so the ResourceOperation declarations carry fresh proof.
EXTRA_NESTED = [
    "GET /Agent/me",
    "GET /Site/StockBins",
    "GET /Asset/GetAllSoftwareVersions",
    "GET /Asset/NextTag",
    "GET /Timesheet/forecasting",
    "GET /Team/Tree",
]

BOOLEANISH = re.compile(
    r"^(active|enabled|include.*|outstanding.*|pending.*|only.*|.*_only|.*disabled)$",
    re.I,
)


def spec_optional_params(path: str) -> list[str]:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    op = spec.get("paths", {}).get(path, {}).get("get", {})
    return [
        p.get("name") for p in op.get("parameters", []) if p.get("name") and not p.get("required")
    ]


def value_for(name: str) -> str:
    if name in KNOWN_VALUES:
        return KNOWN_VALUES[name][0]
    if BOOLEANISH.match(name):
        return "true"
    if name.endswith("_id"):
        return "1"
    return "1"


def fallback_attempts(path: str, documented: list[str]) -> list[dict]:
    """Ordered attempt list: documented params first, then tenant scope ids."""
    attempts: list[dict] = []
    for name in documented[:5]:
        if name in ("count", "page_size", "pageinate"):
            continue
        for value in KNOWN_VALUES.get(name, [value_for(name)]):
            attempts.append({name: value})
    # scope fallbacks (skip ones already attempted above)
    for name in (
        "client_id",
        "toplevel_id",
        "agent_id",
        "site_id",
        "supplier_id",
        "department_id",
        "fault_id",
    ):
        if name in documented:
            continue
        for value in KNOWN_VALUES[name][:1]:
            attempts.append({name: value})
    return attempts[:12]


def describe_body(body) -> dict:
    """Envelope evidence with UNCAPPED row keys (cap 100 as a sanity bound)."""
    if isinstance(body, list):
        rows, envelope = body, "<bare array>"
    elif isinstance(body, dict):
        envelope, rows = None, None
        for key, value in body.items():
            if isinstance(value, list):
                envelope, rows = key, value
                break
        if envelope is None:
            return {"envelope": "<object>", "rows": None, "row_keys": None, "record_count": None}
    else:
        return {
            "envelope": f"<{type(body).__name__}>",
            "rows": None,
            "row_keys": None,
            "record_count": None,
        }

    keys = None
    if rows:
        for row in rows:
            if isinstance(row, dict):
                keys = sorted(row.keys())[:100]
                break
    record_count = None
    if isinstance(body, dict):
        try:
            record_count = int(body.get("record_count"))
        except (TypeError, ValueError):
            record_count = None
    return {"envelope": envelope, "rows": len(rows), "row_keys": keys, "record_count": record_count}


async def try_attempt(client, path: str, extra: dict) -> dict:
    params = {**BOUND_PARAMS, **extra}
    started = time.monotonic()
    try:
        body = await client.request("GET", path, params=params, timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        return {
            "params": extra,
            "status": f"error:{getattr(exc, 'category', type(exc).__name__)}"
            if status is None
            else status,
            "ms": int((time.monotonic() - started) * 1000),
            "error": (str(exc) or type(exc).__name__)[:200],
        }
    result = {
        "params": extra,
        "status": 200,
        "ms": int((time.monotonic() - started) * 1000),
        **describe_body(body),
    }
    return result


async def probe_endpoint(client, key: str, path: str, spec_params: list[str]) -> dict:
    """Bare attempt, then documented/scope params until rows come back."""
    attempts: list[dict] = []

    bare = await try_attempt(client, path, {})
    attempts.append(bare)
    winner = bare if bare.get("rows") else None

    if winner is None:
        for extra in fallback_attempts(path, spec_params):
            attempt = await try_attempt(client, path, extra)
            attempts.append(attempt)
            if attempt.get("rows"):
                winner = attempt
                break

    record: dict = {
        "path": path,
        "attempts": attempts,
        "rows": winner.get("rows") if winner else 0,
        "winner_params": winner.get("params") if winner else None,
        "envelope": winner.get("envelope") if winner else None,
        "record_count": winner.get("record_count") if winner else None,
        "row_keys": winner.get("row_keys") if winner else None,
    }
    return record


async def run(args) -> int:
    from halocli.client import HaloClient
    from halocli.config import load_profile

    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    sweep = json.loads(SWEEP_PATH.read_text(encoding="utf-8"))

    targets: list[tuple[str, str, list[str]]] = []  # (key, path, spec params)
    for entry in ledger["operations"]:
        if entry["method"] != "get" or entry["disposition"] != "backlog":
            continue
        path = entry["path"]
        if "{" in path:
            continue
        key = f"GET {path}"
        ev = sweep.get(key) or {}
        interesting = ev.get("status") == "error:ReadTimeout" or (
            ev.get("status") == 200 and not ev.get("rows")
        )
        if interesting:
            targets.append((key, path, spec_optional_params(path)))
    for key in EXTRA_NESTED:
        path = key.split(" ", 1)[1]
        targets.append((key, path, []))

    results: dict[str, dict] = {}
    if RESULTS_PATH.exists() and not args.force:
        results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    pending = [t for t in targets if t[0] not in results]
    print(f"{len(targets)} targets, {len(results)} recorded, {len(pending)} to probe")

    profile = load_profile(args.profile)
    async with HaloClient(profile, profile_name=args.profile) as client:
        for index, (key, path, spec_params) in enumerate(pending, 1):
            record = await probe_endpoint(client, key, path, spec_params)

            # real-id detail probe with the winner's actual first-row id
            if record.get("rows"):
                # re-fetch the winner to capture a real id (bounded count=1)
                body = await client.request(
                    "GET",
                    path,
                    params={**BOUND_PARAMS, **(record["winner_params"] or {})},
                    timeout=REQUEST_TIMEOUT,
                )
                first_id = None
                rows = body if isinstance(body, list) else None
                if rows is None and isinstance(body, dict):
                    for value in body.values():
                        if isinstance(value, list):
                            rows = value
                            break
                if rows:
                    first = rows[0]
                    if isinstance(first, dict):
                        first_id = first.get("id")
                if first_id is not None:
                    try:
                        detail = await client.request(
                            "GET",
                            f"{path}/{first_id}",
                            params={},
                            timeout=REQUEST_TIMEOUT,
                        )
                        record["id_probe"] = {
                            "id": first_id,
                            "status": 200,
                            "envelope": describe_body(detail)["envelope"],
                        }
                    except Exception as exc:
                        status = getattr(exc, "status_code", None)
                        record["id_probe"] = {
                            "id": first_id,
                            "status": status
                            if status is not None
                            else f"error:{getattr(exc, 'category', type(exc).__name__)}",
                        }

            results[key] = record
            winner = record.get("winner_params")
            print(
                f"[{index}/{len(pending)}] {key} -> rows={record.get('rows')} "
                f"params={winner} id_probe={record.get('id_probe', {}).get('status') if record.get('id_probe') else '-'}",
                flush=True,
            )
            RESULTS_PATH.write_text(
                json.dumps(dict(sorted(results.items())), indent=1, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if index % args.batch_size == 0:
                await asyncio.sleep(args.pause)

    with_rows = sum(1 for r in results.values() if r.get("rows"))
    print(f"DONE: {len(results)} endpoints, {with_rows} yielded rows")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="thomas")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--force", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
