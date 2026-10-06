#!/usr/bin/env python3
"""Message-template catalog probe: the unexecuted aat4/5/6 variants.

The SPA decode reads auto_action_type3 = add note,1 create ticket,
2 update, and4-8 client/site/user CRUD - but only1/2/3 ever ran.
This probe fires each candidate with a marker-name body and scans the
entity endpoints to pin WHICH aat creates WHICH entity:

  aat1_control  proven create-ticket shape (in-run control)
  aat4          {"name": <marker>}  -> does a Client/Site/User appear?
  aat5          {"name": <marker>}
  aat6          {"name": <marker>}

Readback: GET /Clients + /Sites + /Users scanned for the markers;
whatever appears pins the pair (aat -> endpoint). Self-cleaning:
created entities + runbooks deleted in finally. Evidence:
aat48_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import probe_harness as ph

AAT1_MARKER = "halocli-aat48 ticket marker"
AAT4_MARKER = "halocli-aat48 aat4 entity"
AAT5_MARKER = "halocli-aat48 aat5 entity"
AAT6_MARKER = "halocli-aat48 aat6 entity"

# leg -> (aat, message body, marker)
LEGS: dict[str, tuple[int, dict, str]] = {
    "aat1_control": (1, {"summary": AAT1_MARKER, "reportedby": "probe@example.com"}, AAT1_MARKER),
    "aat4": (4, {"name": AAT4_MARKER}, AAT4_MARKER),
    "aat5": (5, {"name": AAT5_MARKER}, AAT5_MARKER),
    "aat6": (6, {"name": AAT6_MARKER}, AAT6_MARKER),
}

ENDPOINTS = ("/Tickets", "/Clients", "/Sites", "/Users")


async def scan_markers(client: Any, markers: list[str]) -> dict[str, dict]:
    """endpoint:id for any created entity carrying one of our markers."""
    hits: dict[str, dict] = {}
    for ep in ENDPOINTS:
        try:
            rows = await client.request("GET", ep, params={"count": "1000"}, timeout=60)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(rows, dict):
            rows = next((v for v in rows.values() if isinstance(v, list)), [])
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            blob = json.dumps(r, default=str)
            for m in markers:
                if m in blob:
                    hits[m] = {"endpoint": ep, "id": r.get("id")}
    return hits


def verdict(ev: dict[str, Any]) -> str:
    legs = ev["legs"]
    findings: list[str] = []
    hits: dict[str, dict] = ev.get("marker_hits") or {}
    if AAT1_MARKER in hits:
        findings.append("control: aat1 created the marker ticket (harness valid)")
    else:
        findings.append("control FAILED - interpret the rest with care")
    for leg, (_aat, _body, marker) in LEGS.items():
        if leg == "aat1_control":
            continue
        hit = hits.get(marker)
        if hit:
            findings.append(f"SHIP: {leg} -> {hit['endpoint']} id={hit['id']}")
        else:
            rl = legs.get(leg, {}).get("runlog") or {}
            findings.append(
                f"{leg} created no matching entity (run status={rl.get('status')}, "
                f"error={(rl.get('error') or '')[:60]!r})"
            )
    return " | ".join(findings)


async def main() -> int:
    profile = ph.load_profile("dev")
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")
    ev: dict[str, Any] = {"tenant": host, "question": "aat4/5/6 catalog pin", "legs": {}}
    created_runbooks: list[str] = []
    created_entities: list[str] = []
    try:
        async with ph.HaloClient(profile, profile_name="dev") as client:
            try:
                all_markers = [m for (_a, _b, m) in LEGS.values()]
                pre = await scan_markers(client, all_markers)
                for leg, (aat, body, _marker) in LEGS.items():
                    doc = ph.aa8_runbook(f"aat48-{leg}", aat, json.dumps(body))
                    resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    wid = str(row.get("id"))
                    created_runbooks.append(wid)
                    fire = await ph.bc._fire_runbook(client, wid, {})
                    ev["legs"][leg] = {
                        "id": wid,
                        "aat": aat,
                        "runlog": fire.get("runlog"),
                        "trigger_fire": fire.get("trigger_fire"),
                    }
                    rl = fire.get("runlog") or {}
                    print(
                        f"{leg:14s} aat={aat} status={rl.get('status')} exec={rl.get('steps_executed')}"
                    )

                markers = [m for (_a, _b, m) in LEGS.values()]
                hits = await scan_markers(client, markers)
                for m, hit in hits.items():
                    if m not in pre:  # created by THIS run (incl. control ticket)
                        created_entities.append(f"{hit['endpoint']}:{hit['id']}")
                ev["marker_hits"] = hits
            finally:
                await ph.cleanup_runbooks(client, created_runbooks, ev)
                for ref in created_entities:
                    ep, eid = ref.split(":", 1)
                    try:
                        await client.request("DELETE", f"{ep}/{eid}", timeout=30)
                    except Exception as exc:  # noqa: BLE001
                        ev.setdefault("cleanup_errors", []).append(f"{ref}: {exc}")
                ev["entity_cleanup"] = f"deleted {len(created_entities)} created entit(ies)"
    finally:
        ev["verdict"] = verdict(ev)
        out = ph.REPO / "aat48_evidence.json"
        out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
        print("verdict:", ev["verdict"])
        print("cleanup:", ev.get("cleanup"), "|", ev.get("entity_cleanup"))
        print("evidence:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
