#!/usr/bin/env python3
"""Read-only evidence for the advanced trigger/side-effect surfaces.

Workflows, automation runbooks, rules: the family that is deliberately
raw (a misconfigured workflow fires notifications and automations against
the whole tenant). GET-only - this records the SHAPES an operator needs
to build payloads safely; execution endpoints (POST /Automation/{runbookId})
are documented but never fired here.

    python scripts/advanced_config_probes.py [--profile dev]

Results land in advanced_config_evidence.json (committed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

EVIDENCE_FILE = REPO_ROOT / "advanced_config_evidence.json"


def shape(value, depth: int = 0) -> object:
    """Compact structural summary: types and nested keys, no values."""
    if isinstance(value, dict):
        if depth >= 2:
            return {k: type(v).__name__ for k, v in list(value.items())[:40]}
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        if not value:
            return []
        return [shape(value[0], depth + 1), f"...{len(value)} rows"]
    return type(value).__name__


async def main() -> int:
    from halocli.client import HaloClient
    from halocli.config import load_profile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    args = parser.parse_args()

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")

    out: dict = {}

    async def first_id(path: str, params: dict | None = None) -> Any:
        """Id of the first row - details probe real rows, never guess ids."""
        try:
            body = await client.request(
                "GET", path, params={"count": "5", **(params or {})}, timeout=45
            )
        except Exception:  # noqa: BLE001
            return None
        rows = (
            body
            if isinstance(body, list)
            else next((v for v in body.values() if isinstance(v, list)), [])
        )
        for row in rows:
            if isinstance(row, dict) and row.get("id") is not None:
                return row.get("id")
        return None

    async def one(key: str, path: str, params: dict | None = None) -> None:
        try:
            body = await client.request("GET", path, params=params or {}, timeout=45)
        except Exception as exc:  # noqa: BLE001
            out[key] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            return
        rows = (
            body
            if isinstance(body, list)
            else next((v for v in body.values() if isinstance(v, list)), None)
        )
        if rows is not None:
            out[key] = {
                "shape": shape(body),
                "rows": len(rows),
                "record_count": body.get("record_count") if isinstance(body, dict) else None,
                "first_row_keys": sorted(rows[0].keys())[:40] if rows else [],
            }
        else:
            out[key] = {
                "shape": shape(body),
                "keys": sorted(body.keys())[:60] if isinstance(body, dict) else [],
            }

    async def detail(key: str, path: str, params: dict) -> None:
        try:
            body = await client.request("GET", path, params=params, timeout=45)
        except Exception as exc:  # noqa: BLE001
            out[key] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            return
        if isinstance(body, dict):
            out[key] = {
                "keys": sorted(body.keys()),
                "shape": shape(body),
            }
        else:
            out[key] = {"type": type(body).__name__}

    async with HaloClient(profile, profile_name=args.profile) as client:
        # workflow family ---------------------------------------------------
        await one("workflows_list", "/Workflow", {"includeinactive": "true"})
        workflow_id = await first_id("/Workflow", {"includeinactive": "true"})
        out["workflow_id_sampled"] = workflow_id
        if workflow_id is not None:
            await detail("workflow_detail", f"/Workflow/{workflow_id}", {"includedetails": "true"})
        await one("workflow_steps", "/workflowstep", {"includecriteriainfo": "true"})
        await one("workflow_targets", "/WorkflowTarget", {})
        # automation family -------------------------------------------------
        await one("automations_list", "/Automation", {})
        automation_id = await first_id("/Automation")
        out["automation_id_sampled"] = automation_id
        if automation_id is not None:
            await detail("automation_detail", f"/Automation/{automation_id}", {})
        # rules -------------------------------------------------------------
        await one(
            "ticket_rules", "/TicketRules", {"includecriteriainfo": "true", "isconfig": "true"}
        )
        rule_id = await first_id("/TicketRules")
        out["ticket_rule_id_sampled"] = rule_id
        if rule_id is not None:
            await detail(
                "ticket_rule_detail", f"/TicketRules/{rule_id}", {"includedetails": "true"}
            )
        await one("event_rules", "/EventRule", {})
        # adjacent advanced families (lighter capture) ----------------------
        await one("screen_layouts", "/ScreenLayout", {})
        await one("view_lists", "/ViewLists", {})

    evidence = {
        "meta": {
            "tenant": host,
            "profile": args.profile,
            "captured_at": __import__("datetime")
            .datetime.now(__import__("datetime").timezone.utc)
            .isoformat(timespec="seconds"),
            "method": "GET-only",
            "spec_sha256_note": "shapes recorded for the deliberately-raw trigger/side-effect family",
        },
        "surfaces": out,
    }
    EVIDENCE_FILE.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for key, value in out.items():
        if not isinstance(value, dict):
            print(f"[info] {key} = {value}")
            continue
        status = "ERROR" if "error" in value else "ok"
        rows = value.get("rows")
        print(f"[{status}] {key}" + (f" ({rows} rows)" if rows is not None else ""))
    print(f"written: {EVIDENCE_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
