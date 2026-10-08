#!/usr/bin/env python3
"""Census of Halo's Control properties: families, semantics, writability.

Control is a ~4371-property singleton; the Teams/chatbot work proved that
some fields are API-writable (teams_chat_profile), some are silently
dropped (welcome/help/allowed-tenants), some are server-normalized on any
save, and one is re-ciphered (merakiapplicationsecret). This tool builds
the durable handle:

    # static family census from the vendored spec (offline)
    python scripts/control_census.py

    # + empirical sentinel probe (TRIAL ONLY - write/restore per field)
    python scripts/control_census.py --profile dev --probe

Evidence: control_census_evidence.json.

The probe writes a sentinel value per targeted field, reads back, then
immediately restores the original - classifying each field as writable /
silently-dropped / rejected. It refuses the production host outright:
field-level effects belong on the trial first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from halocli.client import HaloClient  # noqa: E402
from halocli.config import load_profile  # noqa: E402

EVIDENCE_PATH = REPO_ROOT / "control_census_evidence.json"
SPEC_PATH = REPO_ROOT / "src" / "halocli" / "spec" / "halo_openapi.json"

EP_CONTROL = "/Control"

# Rewrites any Control save produces (observed trial+prod); they never
# count as probe residue.
KNOWN_NOISE_FIELDS = frozenset(
    {
        "stripepaymentmethodoptions",
        "autogenerate_itemaccountsid",
        "world_clock_5_timezone",
        "world_clock_5_label",
        "trophy_agents",
        "merakiapplicationsecret",
    }
)

# Fields whose names put them in the integration/communication families.
FAMILY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("teams", re.compile(r"teams", re.I)),
    ("chat", re.compile(r"chat", re.I)),
    ("notification", re.compile(r"notif", re.I)),
    ("azure/entra", re.compile(r"azure|entra|graph", re.I)),
)

ACTION_FIELD = re.compile(r"^_[a-z]")


def control_properties() -> dict[str, Any]:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    return spec["components"]["schemas"]["Control"]["properties"]


def classify_fields(props: dict[str, Any]) -> dict[str, Any]:
    """Static census: family buckets, action fields, type histogram."""
    families: dict[str, list[str]] = {name: [] for name, _ in FAMILY_PATTERNS}
    actions: list[str] = []
    types: dict[str, int] = {}
    for pname, pd in props.items():
        if ACTION_FIELD.match(pname):
            actions.append(pname)
        t = pd.get("type") or "ref"
        types[t] = types.get(t, 0) + 1
        for fam, pattern in FAMILY_PATTERNS:
            if pattern.search(pname):
                families[fam].append(pname)
    return {
        "total_properties": len(props),
        "families": {k: sorted(v) for k, v in families.items()},
        "family_counts": {k: len(v) for k, v in families.items()},
        "action_fields": sorted(actions),
        "type_histogram": dict(sorted(types.items(), key=lambda kv: -kv[1])),
    }


def probe_targets(props: dict[str, Any], limit: int) -> list[str]:
    """Integration/communication family fields, newest-scope first."""
    targets: list[str] = []
    for pname in sorted(props):
        if ACTION_FIELD.match(pname):
            continue
        if any(pattern.search(pname) for _, pattern in FAMILY_PATTERNS):
            targets.append(pname)
    return targets[:limit]


def sentinel_for(value: Any) -> Any:
    """A type-plausible probe value distinct from the original."""
    if value is None:
        return "__census_probe__"
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return (value or "") + "__probe"
    if isinstance(value, int):
        return -999001
    if isinstance(value, float):
        return -999001.0
    if isinstance(value, list):
        return [*value, "__probe"]
    if isinstance(value, dict):
        return {**value, "__census_probe__": True}
    return "__census_probe__"


def classify_readback(original: Any, sentinel: Any, readback: Any) -> str:
    if readback == sentinel:
        return "writable"
    if readback == original:
        return "silently-dropped"
    return "server-transformed"


def unwrap(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "value", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    return []


def pick_control(rows: list[Any]) -> dict[str, Any]:
    control = next(
        (r for r in rows if isinstance(r, dict) and "teams_chat_profile" in r),
        rows[0] if rows else None,
    )
    if not isinstance(control, dict):
        raise SystemExit("GET /Control returned no Control object")
    return control


async def snapshot(client: HaloClient) -> dict[str, Any]:
    return pick_control(unwrap(await client.request("GET", EP_CONTROL, timeout=60)))


async def post_control(client: HaloClient, control: dict[str, Any]) -> None:
    await client.request("POST", EP_CONTROL, json_body=[control], timeout=120)


async def probe_fields(client: HaloClient, targets: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Sentinel-probe each field: write -> read back -> restore.

    Returns (classification_by_field, residual_differences) where residuals
    are fields still differing from the pre-probe snapshot after restores
    (excluding known server noise).
    """
    snapshot_control = await snapshot(client)
    results: dict[str, Any] = {}
    for field in targets:
        if field not in snapshot_control:
            results[field] = {"verdict": "absent-from-live-object"}
            continue
        original = snapshot_control[field]
        sentinel = sentinel_for(original)
        candidate = dict(snapshot_control)
        candidate[field] = sentinel
        try:
            await post_control(client, candidate)
        except Exception as exc:  # noqa: BLE001
            results[field] = {"verdict": "rejected", "error": str(exc)[:200]}
            continue
        readback = (await snapshot(client)).get(field)
        verdict = classify_readback(original, sentinel, readback)
        results[field] = {
            "verdict": verdict,
            "original": original,
            "sentinel": sentinel,
            "readback": readback,
        }
        if verdict in ("writable", "server-transformed"):
            restore = dict(snapshot_control)
            restore[field] = original
            try:
                await post_control(client, restore)
            except Exception as exc:  # noqa: BLE001
                results[field]["restore_error"] = str(exc)[:200]
        else:
            results[field]["restored"] = "not-needed"

    final = await snapshot(client)
    residuals = sorted(
        key
        for key in set(snapshot_control) | set(final)
        if key not in KNOWN_NOISE_FIELDS and snapshot_control.get(key) != final.get(key)
    )
    for key in residuals:
        # force-restore any residue from the snapshot
        try:
            await post_control(client, dict(snapshot_control))
            break
        except Exception:  # noqa: BLE001
            break
    return results, residuals


async def run(args: argparse.Namespace) -> int:
    props = control_properties()
    census = classify_fields(props)
    evidence: dict[str, Any] = {
        "tool": "control_census",
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "census": census,
        "mode": "static+probe" if args.probe else "static",
        # Findings from OTHER instances (documented provenance, not probed here):
        "known_cross_instance": {
            "prod_teams_chatbot_fields_exist_but_api_refused": [
                "teams_chat_welcome_message",
                "teams_chat_help_message",
                "teams_chat_tenants",
                "teams_chat_tenant_list",
            ],
            "prod_notes": [
                "teams_chat_profile: API-writable on prod (bound a34f0eb2... verified)",
                "any Control save rewrites merakiapplicationsecret ciphertext server-side",
                "UI End-User Chat tab writes fields the API application may not (field-level perms)",
            ],
            "trial_notes": [
                "the four chatbot message/tenant fields are ABSENT from the trial Control object",
                "azure AI toggles + browser push: silently dropped by the API even on trial",
            ],
            "sources": [
                "teams_chatbot_evidence_prod.json",
                "teams_chatbot_evidence.json",
                "this file's probe",
            ],
        },
    }

    if args.probe:
        profile = load_profile(args.profile)
        host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
        if "midtowntg" in host:
            raise SystemExit(
                "refusing: the sentinel probe is trial-only "
                "(field-level writes are not exercised on production)"
            )
        targets = probe_targets(props, args.limit)
        async with HaloClient(profile, profile_name=args.profile) as client:
            verdicts, residuals = await probe_fields(client, targets)
        evidence["probe"] = {
            "profile": args.profile,
            "tenant": host,
            "target_count": len(targets),
            "verdicts": verdicts,
            "residuals_after_restore": residuals,
            "clean": residuals == [],
        }
        tally: dict[str, int] = {}
        for entry in verdicts.values():
            tally[entry["verdict"]] = tally.get(entry["verdict"], 0) + 1
        evidence["probe"]["tally"] = tally

    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, default=str))
    print(f"\nevidence -> {EVIDENCE_PATH}", file=sys.stderr)

    if args.probe and not evidence["probe"]["clean"]:
        print("WARNING: residual differences after restore", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="empirical writability probe (sentinel write/readback/restore; trial only)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=120,
        help="max fields to probe (families: teams/chat/notification/azure)",
    )
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
