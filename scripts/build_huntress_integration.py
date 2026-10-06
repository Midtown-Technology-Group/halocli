#!/usr/bin/env python3
"""PERSISTENT builder: install the spec-rendered Huntress integration into a tenant.

Unlike ``scripts/bifrost_convert.py --apply`` (round-trip: create -> fire ->
DELETE), this LEAVES the integration + full method catalog in the tenant -
the ask is to build the integration out. Safety: refuses production hosts,
defaults to the ``dev`` profile, refuses to double-create (same name),
and ``--cleanup`` removes it again (methods cascade with the integration
but are deleted explicitly first, mirroring the apply probe).

Usage:
    python scripts/build_huntress_integration.py --profile dev
    python scripts/build_huntress_integration.py --profile dev --cleanup

Inputs default to the repo-root artifacts rendered by openapi_methods.py:
    huntress_integration.yaml + huntress_integration_methods.yaml
Evidence: huntress_integration_evidence.json (repo root).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import probe_harness as ph
from halocli.bifrost_convert import convert_integration, convert_methods

METHOD_EP = "/CustomIntegrationMethod"


def _method_docs(rows: list[dict], integration_id: int) -> list[dict]:
    """convert_methods output -> POST bodies (strip _-keys, pin owner)."""
    bodies, notes = convert_methods(rows, "Huntress")
    if notes:
        print("method notes:", notes)
    return [
        {k: v for k, v in m.items() if not k.startswith("_")} | {"integration_id": integration_id}
        for m in bodies
    ]


async def _find_by_name(client: Any, name: str) -> int | None:
    rows = await client.request("GET", "/CustomIntegration", params={"showall": "true"}, timeout=60)
    if isinstance(rows, dict):
        rows = next((v for v in rows.values() if isinstance(v, list)), [])
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and str(r.get("name") or "") == name:
            return int(r.get("id"))
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default="dev")
    ap.add_argument("--integration", default="huntress_integration.yaml")
    ap.add_argument("--methods", default="huntress_integration_methods.yaml")
    ap.add_argument("--name", default="Huntress")
    ap.add_argument("--cleanup", action="store_true", help="delete the integration instead")
    args = ap.parse_args()

    import yaml

    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")

    ev: dict[str, Any] = {"tenant": host, "action": "cleanup" if args.cleanup else "build"}
    async with HaloClient(profile, profile_name=args.profile) as client:
        existing = await _find_by_name(client, args.name)
        if args.cleanup:
            if existing is None:
                print(f"nothing to delete: no integration named {args.name!r}")
                return 0
            methods = await client.request(
                "GET",
                METHOD_EP,
                params={"integration_id": existing, "showall": "true"},
                timeout=60,
            )
            mlist = methods if isinstance(methods, list) else []
            deleted = 0
            for m in mlist:
                try:
                    await client.request(
                        "DELETE", f"/CustomIntegrationMethod/{m.get('id')}", timeout=30
                    )
                    deleted += 1
                except Exception as exc:  # noqa: BLE001
                    ev.setdefault("cleanup_errors", []).append(str(exc)[:120])
            await client.request("DELETE", f"/CustomIntegration/{existing}", timeout=30)
            gone = True
            try:
                await client.request("GET", f"/CustomIntegration/{existing}", timeout=30)
                gone = False
            except Exception:  # noqa: BLE001
                gone = True
            ev.update({"deleted_id": existing, "deleted_methods": deleted, "verified_gone": gone})
            print(f"deleted integration {existing} ({deleted} methods), verified gone: {gone}")
        else:
            if existing is not None:
                raise SystemExit(
                    f"an integration named {args.name!r} already exists (id {existing}) - "
                    "use --cleanup first if you want to rebuild it"
                )
            entry = yaml.safe_load(_ph_path(args.integration).read_text(encoding="utf-8"))
            doc = entry.get("integrations") or {}
            integ = next(iter(doc.values())) if isinstance(doc, dict) else None
            if not integ:
                raise SystemExit(f"{args.integration}: no integrations entry")
            conv = convert_integration(integ)
            if not conv.ok or conv.payload is None:
                raise SystemExit(f"integration payload failed: {conv.notes}")
            payload, notes = conv.payload, conv.notes
            rows_doc = yaml.safe_load(_ph_path(args.methods).read_text(encoding="utf-8"))
            rows = rows_doc.get("methods") or []
            resp = await client.request(
                "POST", "/CustomIntegration", json_body=[payload], timeout=90
            )
            row = resp[0] if isinstance(resp, list) and resp else resp
            iid = int(row.get("id"))
            ev["integration"] = {"id": iid, "name": payload.get("name"), "notes": notes}
            created, failed = [], []
            for body in _method_docs(rows, iid):
                try:
                    r = await client.request("POST", METHOD_EP, json_body=[body], timeout=45)
                    mr = r[0] if isinstance(r, list) and r else r
                    created.append({"id": mr.get("id"), "name": body.get("name")})
                except Exception as exc:  # noqa: BLE001
                    failed.append({"name": body.get("name"), "error": str(exc)[:180]})
            ev["methods"] = {"created": len(created), "failed": len(failed), "failures": failed}
            # verify: read back the catalog
            back = await client.request(
                "GET",
                METHOD_EP,
                params={"integration_id": iid, "showall": "true"},
                timeout=60,
            )
            blist = back if isinstance(back, list) else []
            ev["verify"] = {
                "integration_id": iid,
                "methods_on_readback": len(blist),
                "expected": len(rows),
            }
            print(
                f"built integration {iid} {payload.get('name')!r}: "
                f"{len(created)} methods created, {len(failed)} failed, "
                f"{len(blist)}/{len(rows)} read back"
            )
            for f in failed[:8]:
                print("   FAILED:", f["name"], f["error"][:100])

    out = ph.REPO / "huntress_integration_evidence.json"
    out.write_text(json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8")
    print("evidence:", out)
    return 0


def _ph_path(p: str):
    from pathlib import Path

    base = ph.REPO / p if not str(p).startswith(("/", "\\")) and ":" not in str(p) else Path(p)
    # canonicalize a CLI-supplied path before touching the disk (S8707) -
    # the same house pattern as scripts/bifrost_convert.py::_safe_path
    return base.expanduser().resolve()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
