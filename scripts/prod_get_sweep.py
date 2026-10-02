#!/usr/bin/env python3
"""Phase 1: live read-only GET sweep across the production Halo API.

For every GET operation in coverage_ledger.json, probe the real tenant once
and record evidence: status, latency, envelope shape, row keys, payload size.

SAFETY (structural, not advisory):
  - this script ONLY ever issues method="GET" through HaloClient; there is
    no code path that can send POST/PUT/PATCH/DELETE (--data does not exist
    here), so a write against production is impossible by construction
  - paths whose FINAL segment is exactly an action verb (ClearCache,
    Process, Generate, ...) are skipped even though GET is read-by-convention
  - collection GETs send count/page_size/pageinate=1 hints (many endpoints
    honor them; ones that don't are recorded with their real size), every
    request carries a 15s timeout, batches of 50 with a pause between them

Resumable: results accumulate in sweep_results.json; already-probed
(path, method) pairs are skipped unless --force is given.

    python scripts/prod_get_sweep.py [--batch-size 50] [--pause 1.0] [--force]

Writes sweep_results.json at the repo root. Profile defaults to the live
tenant profile used throughout this repo's probe sessions.
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
RESULTS_PATH = REPO_ROOT / "sweep_results.json"

sys.path.insert(0, str(REPO_ROOT / "src"))

# Exact final-segment match: GET is read-by-convention, but never probe a
# path whose last segment IS the action (defense-in-depth against oddities).
ACTION_SEGMENT = re.compile(
    r"^(ClearCache|Process|Generate|Execute|Send|Delete|Remove|Purge|Flush|"
    r"Rebuild|Recalculate|Kill|Cancel|Restart|Wake|Dispatch)$",
    re.I,
)

BOUND_PARAMS = {"count": "1", "page_size": "1", "pageinate": "true"}
REQUEST_TIMEOUT = 15.0


def load_ledger_gets() -> list[dict]:
    """All GET entries from the coverage ledger (the sweep's target set)."""
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return [e for e in ledger["operations"] if e["method"] == "get"]


def probe_id_path(path: str) -> tuple[str, dict]:
    """Substitute path params with a probe token.

    A non-numeric token forces Halo's own validation (400 = route exists,
    404 = route exists with lookup miss) without ever touching a real record.
    """
    probe_path = re.sub(r"\{[^}]+\}", "probe", path)
    return probe_path, {}


def collection_params(path: str) -> dict:
    """Bound-list hints for id-less paths (ignored by endpoints that cannot)."""
    if "{" in path:
        return {}
    return dict(BOUND_PARAMS)


def describe_body(body) -> dict:
    """Compact envelope evidence: shape, row count, first row's keys."""
    if isinstance(body, list):
        rows = body
        envelope = "<bare array>"
    elif isinstance(body, dict):
        envelope = None
        rows = None
        for key, value in body.items():
            if isinstance(value, list):
                envelope, rows = key, value
                break
        if envelope is None:
            return {"envelope": "<object>", "rows": None, "row_keys": None}
    else:
        return {"envelope": f"<{type(body).__name__}>", "rows": None, "row_keys": None}

    keys = None
    if rows:
        for row in rows:
            if isinstance(row, dict):
                keys = sorted(row.keys())[:30]
                break
    return {"envelope": envelope, "rows": len(rows), "row_keys": keys}


async def probe_one(client, path: str, params: dict) -> dict:
    """Probe one GET path and compact the verdict (status/latency/shape)."""
    started = time.monotonic()
    try:
        body = await client.request("GET", path, params=params, timeout=REQUEST_TIMEOUT)
    except Exception as exc:  # HaloCLIError carries status; others are transport
        elapsed = int((time.monotonic() - started) * 1000)
        status = getattr(exc, "status_code", None)
        category = getattr(exc, "category", type(exc).__name__)
        error = str(exc) or type(exc).__name__
        result = {
            "status": f"error:{category}" if status is None else status,
            "ms": elapsed,
            "error": error[:300],
        }
        # Strict endpoints reject unknown params; retry once bare so a param
        # rejection does not masquerade as endpoint health.
        if status == 400 and params:
            try:
                body = await client.request("GET", path, timeout=REQUEST_TIMEOUT)
            except Exception as retry_exc:
                result["error"] = str(retry_exc) or type(retry_exc).__name__
                result["retried_bare"] = True
                return result
            result["retried_bare"] = True
        else:
            return result

    elapsed = int((time.monotonic() - started) * 1000)
    size = len(json.dumps(body, default=str))
    result = {
        "status": 200,
        "ms": elapsed,
        "size_bytes": size,
        **describe_body(body),
    }
    return result


async def run(args) -> int:
    """Probe every pending ledger GET in resumable batches; write results."""
    from halocli.client import HaloClient
    from halocli.config import load_profile

    gets = load_ledger_gets()
    results: dict[str, dict] = {}
    if RESULTS_PATH.exists() and not args.force:
        results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))

    profile = load_profile(args.profile)
    pending = []
    for entry in gets:
        key = f"{entry['method'].upper()} {entry['path']}"
        if key in results and not args.force:
            continue
        last_segment = entry["path"].rstrip("/").split("/")[-1]
        if ACTION_SEGMENT.match(last_segment):
            results[key] = {"status": "skipped-suspicious", "disposition": entry["disposition"]}
            continue
        pending.append((key, entry))

    print(f"{len(gets)} GET ops total, {len(results)} already recorded, {len(pending)} to probe")

    total = len(pending)
    batch_n = 0
    async with HaloClient(profile, profile_name=args.profile) as client:
        for index in range(0, total, args.batch_size):
            batch = pending[index : index + args.batch_size]
            for key, entry in batch:
                path = entry["path"]
                if "{" in path:
                    probe_path, params = probe_id_path(path)
                else:
                    probe_path, params = path, collection_params(path)
                result = await probe_one(client, probe_path, params)
                result["disposition"] = entry["disposition"]
                result["path_template"] = path
                results[key] = result
            batch_n += 1
            done = min(index + args.batch_size, total)
            print(
                f"batch {batch_n}: {done}/{total} probed "
                f"(last: {batch[-1][0]} -> {results[batch[-1][0]]['status']})",
                flush=True,
            )
            RESULTS_PATH.write_text(
                json.dumps(dict(sorted(results.items())), indent=1, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if done < total:
                await asyncio.sleep(args.pause)

    counts: dict[str, int] = {}
    for entry in results.values():
        status = str(entry.get("status"))
        counts[status] = counts.get(status, 0) + 1
    print("STATUS COUNTS:", json.dumps(dict(sorted(counts.items()))))
    return 0


def main() -> int:
    """CLI entry: parse sweep options and run the async probe loop."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="thomas")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between batches")
    parser.add_argument("--force", action="store_true", help="re-probe recorded entries")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
