"""Dump ALL trial workflows with includedetails (full documents) for study."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def shape(value, depth: int = 0):
    if isinstance(value, dict):
        if depth >= 3:
            return {k: type(v).__name__ for k, v in list(value.items())[:50]}
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:80]}
    if isinstance(value, list):
        if not value:
            return []
        return [shape(value[0], depth + 1), f"...{len(value)} items"]
    if isinstance(value, str) and len(value) > 60:
        return f"str({len(value)})"
    return value


async def main() -> None:
    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile("dev")
    out: dict = {}
    async with HaloClient(profile, profile_name="dev") as client:
        body = await client.request(
            "GET", "/Workflow", params={"includeinactive": "true"}, timeout=45
        )
        rows = (
            body
            if isinstance(body, list)
            else next((v for v in body.values() if isinstance(v, list)), [])
        )
        out["list"] = rows
        details = {}
        for row in rows:
            wid = row.get("id")
            try:
                doc = await client.request(
                    "GET", f"/Workflow/{wid}", params={"includedetails": "true"}, timeout=45
                )
            except Exception as exc:  # noqa: BLE001
                details[str(wid)] = {"error": str(exc)[:150]}
                continue
            details[str(wid)] = doc
        out["details"] = details

    dest = Path(r"C:\Users\ThomasBray\AppData\Local\Temp\opencode\inbox_workflows.json")
    dest.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"{len(rows)} workflows, {len(details)} details -> {dest}")
    # compact shape summary per workflow
    for wid, doc in details.items():
        if not isinstance(doc, dict) or "error" in doc:
            print(f"  {wid}: {doc if isinstance(doc, dict) else '?'}")
            continue
        steps = doc.get("steps") or []
        stages = doc.get("stages") or []
        targets = doc.get("targets") or []
        actions = sum(len(s.get("actions") or []) for s in steps if isinstance(s, dict))
        fchart = doc.get("flow_chart_json")
        print(
            f"  {wid}: {doc.get('name')!r} active={doc.get('active')} "
            f"stages={len(stages)} steps={len(steps)} actions={actions} "
            f"targets={len(targets)} flow_chart={'yes' if fchart else 'no'}"
        )


asyncio.run(main())
