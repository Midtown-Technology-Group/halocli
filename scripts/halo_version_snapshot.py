#!/usr/bin/env python3
"""Snapshot a Halo instance's version + our belief-state about its API.

Run this the FIRST time we connect to an instance (and after any Halo
release move). It captures, per instance, the raw GET /InstanceInfo
payload - app_version, database_version and the hosted_group release
track (prod sits on Stable, trial tenants ride the beta track) - plus
what THIS repo believed at capture time: vendored spec sha256 and
operation counts, registry size and the coverage-ledger summary.

Entries merge into halo_version_snapshot.json keyed by host, so
`git diff` after a trial/beta capture answers directly: did the beta
instance move (version/track), and do our spec assumptions still match?

    python scripts/halo_version_snapshot.py [--profile NAME]

Pair with scripts/check_spec_currency.py: that one proves the vendored
spec matches UPSTREAM swagger; this one records WHICH builds we have
actually talked to. GET-only - it never writes to the instance.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

SNAPSHOT_FILE = REPO_ROOT / "halo_version_snapshot.json"
SPEC_FILE = REPO_ROOT / "src" / "halocli" / "spec" / "halo_openapi.json"
LEDGER_FILE = REPO_ROOT / "coverage_ledger.json"


def _belief_state() -> dict:
    spec_bytes = SPEC_FILE.read_bytes()
    spec = json.loads(spec_bytes.decode("utf-8"))
    operations = sum(
        len([m for m in item if m in {"get", "post", "put", "patch", "delete"}])
        for item in spec.get("paths", {}).values()
        if isinstance(item, dict)
    )
    ledger = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    from halocli.resources import RESOURCES

    return {
        "halocli": metadata.version("halocli"),
        "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
        "spec_info_version": spec.get("info", {}).get("version"),
        "spec_paths": len(spec.get("paths", {})),
        "spec_operations": operations,
        "registry_resources": len(RESOURCES),
        "ledger_totals": {
            key: value for key, value in sorted(ledger.items()) if isinstance(value, int)
        },
    }


async def _instance_info(profile_name: str) -> tuple[str, dict]:
    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(profile_name)
    async with HaloClient(profile, profile_name=profile_name) as client:
        body = await client.request("GET", "/InstanceInfo", timeout=30)
    if not isinstance(body, dict):
        raise SystemExit(f"unexpected /InstanceInfo shape: {type(body).__name__}")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    return host, body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        default="default",
        help="HaloCLI profile to connect with (default resolves a single profile).",
    )
    args = parser.parse_args()

    host, info = asyncio.run(_instance_info(args.profile))
    entry = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": args.profile,
        "tenant_id": info.get("tenant_id"),
        "halo": info,
        "beliefs": _belief_state(),
    }

    snapshot: dict = {}
    if SNAPSHOT_FILE.exists():
        snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    snapshot[host] = entry
    SNAPSHOT_FILE.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"snapshot written: {SNAPSHOT_FILE.name} [{host}]")
    print(
        f"  app_version={info.get('app_version')} "
        f"database_version={info.get('database_version')} "
        f"hosted_group={info.get('hosted_group')}"
    )
    current_sha = entry["beliefs"]["spec_sha256"]
    print(
        f"  beliefs: spec={current_sha[:12]}… "
        f"({entry['beliefs']['spec_operations']} ops, "
        f"{entry['beliefs']['registry_resources']} resources)"
    )
    if len(snapshot) > 1:
        print("  instances on record:")
        for name, existing in sorted(snapshot.items()):
            halo = existing.get("halo", {})
            stale = (
                "  [spec drift since capture]"
                if existing.get("beliefs", {}).get("spec_sha256") != current_sha
                else ""
            )
            print(f"    {name}: {halo.get('app_version')} ({halo.get('hosted_group')}){stale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
