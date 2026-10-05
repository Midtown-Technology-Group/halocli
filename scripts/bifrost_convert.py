#!/usr/bin/env python3
"""Convert Bifrost artifacts into Halo documents (offline) or apply them.

Offline (no network, safe anywhere):

    python scripts/bifrost_convert.py \\
        --integrations path/to/.bifrost/integrations.yaml --integration NinjaOne \\
        --workflow-file functions/voicemail_routing.py --function inspect_voicemail_customer \\
        --workflows-yaml path/to/.bifrost/workflows.yaml \\
        --methods methods.yaml \\
        --out ./halo_out

Writes ``halo_out/`` with:
- ``integration__<name>.json``   POST /CustomIntegration body (one-element array)
- ``methods__<name>.json``       POST /CustomIntegrationMethod bodies (cascade)
- ``runbook__<name>.json``       POST /Webhook {type:1} body (runbook document)
- ``conversion_report.json``     every note: what stayed human, what moved

``--apply --profile dev`` additionally round-trips every generated payload
against the trial (create -> verify -> [trigger fire] -> delete, self-cleaning,
production-refusing) and writes ``bifrost_conversion_evidence.json``: the
runbook create uses a bounded ladder if the structural steps are rejected,
recording which shape the server accepted.  The runbook is created with
runbook_start_type=1 (public endpoint) and fired once via
``POST /Automation/{id}`` - the external-trigger verification that mirrors
how Bifrost itself would start Halo runbooks.

    python scripts/bifrost_convert.py ... --apply --profile dev
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import yaml  # noqa: E402

from halocli.bifrost_convert import (  # noqa: E402
    Conversion,
    convert_integration,
    convert_methods,
    convert_workflow,
    extract_http_methods,
)

PROBE = "haloclidevprobe"


def _load_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def _integrations_entries(args: argparse.Namespace) -> list[dict]:
    entries: list[dict] = []
    if args.integrations:
        doc = _load_structured(Path(args.integrations))
        entries.extend((doc or {}).get("integrations", {}).values())
    if args.integrations_json:
        doc = _load_structured(Path(args.integrations_json))
        items = doc.get("items", doc) if isinstance(doc, dict) else doc
        entries.extend(items or [])
    if args.integration:
        want = args.integration.lower()
        entries = [
            e
            for e in entries
            if want in str(e.get("name", "")).lower() or str(e.get("id")) == args.integration
        ]
        if not entries:
            raise SystemExit(f"--integration {args.integration!r} matched no entries")
    return entries


def _workflow_args(args: argparse.Namespace) -> tuple[dict, str | None, str | None]:
    """Row + (source, function) for the selected workflow."""
    row: dict = {}
    source = None
    function = args.function

    if args.workflow_file:
        source = Path(args.workflow_file).read_text(encoding="utf-8")
    if args.workflows_yaml and (args.workflow or args.function):
        doc = _load_structured(Path(args.workflows_yaml))
        for e in (doc or {}).get("workflows", {}).values():
            if args.workflow and str(e.get("name", "")).lower() != args.workflow.lower():
                continue
            row = e
            function = function or e.get("function_name")
            if not source and e.get("path"):
                # path is workspace-relative; resolve beside the yaml
                cand = Path(args.workflows_yaml).parent.parent / str(e["path"])
                if cand.exists():
                    source = cand.read_text(encoding="utf-8")
            break
        if not row:
            raise SystemExit(f"--workflow {args.workflow!r} matched no workflows.yaml entry")
    return row, source, function


def build_outputs(args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"integrations": [], "methods": [], "workflows": []}
    files: dict[str, Path] = {}

    # integrations
    for entry in _integrations_entries(args):
        conv = convert_integration(entry)
        name = str(entry.get("name") or "unnamed")
        if conv.ok:
            p = out_dir / f"integration__{_slug(name)}.json"
            p.write_text(json.dumps([conv.payload], indent=2) + "\n", encoding="utf-8")
            files[f"integration:{name}"] = p
        report["integrations"].append({"name": name, "converted": conv.ok, "notes": conv.notes})

    # methods (explicit file, or extracted from a module)
    methods: list[dict] = []
    method_owner = args.methods_integration or (
        _integrations_entries(args)[0].get("name") if _integrations_entries(args) else None
    )
    if args.methods:
        doc = _load_structured(Path(args.methods))
        methods = doc.get("methods", doc) if isinstance(doc, dict) else doc
    elif args.extract_module:
        source = Path(args.extract_module).read_text(encoding="utf-8")
        methods = extract_http_methods(source)
        report["methods"].append(
            {
                "source": str(args.extract_module),
                "extracted": len(methods),
                "note": (
                    'literal client.<verb>("/path") calls only - assembled/f-string URLs '
                    "cannot be extracted; list those methods explicitly via --methods"
                ),
            }
        )
    if methods and method_owner:
        bodies, notes = convert_methods(methods, str(method_owner))
        if bodies:
            p = out_dir / f"methods__{_slug(str(method_owner))}.json"
            p.write_text(json.dumps(bodies, indent=2) + "\n", encoding="utf-8")
            files[f"methods:{method_owner}"] = p
        report["methods"].append(
            {"integration": method_owner, "count": len(bodies), "notes": notes}
        )
    elif methods:
        report["methods"].append(
            {
                "count": 0,
                "notes": [
                    "methods given but no integration owner (--methods-integration or --integrations)"
                ],
            }
        )

    # workflow -> runbook
    if args.workflow_file or args.workflows_yaml:
        row, source, function = _workflow_args(args)
        conv: Conversion = convert_workflow(row, source, function)
        name = str(row.get("name") or (function or "workflow"))
        if conv.ok:
            p = out_dir / f"runbook__{_slug(name)}.json"
            p.write_text(json.dumps(conv.payload, indent=2) + "\n", encoding="utf-8")
            files[f"runbook:{name}"] = p
        report["workflows"].append({"name": name, "converted": conv.ok, "notes": conv.notes})

    (out_dir / "conversion_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return {"files": {k: str(v) for k, v in files.items()}, "report": report}


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-").lower() or "x"


# --- live apply ----------------------------------------------------------


async def apply_outputs(
    files: dict[str, Path], profile_name: str, evidence_path: Path
) -> dict[str, Any]:
    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(profile_name)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")

    ev: dict[str, Any] = {"tenant": host, "applied": {}}
    async with HaloClient(profile, profile_name=profile_name) as client:
        for key, path in sorted(files.items()):
            kind, name = key.split(":", 1)
            body = json.loads(path.read_text(encoding="utf-8"))
            record: dict[str, Any] = {"file": str(path)}
            try:
                if kind == "integration":
                    item = body[0] if isinstance(body, list) else body
                    item = {**item, "name": f"{PROBE}-{name}"[:60]}
                    resp = await client.request(
                        "POST",
                        "/CustomIntegration",
                        json_body=[item],
                        timeout=60,
                    )
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    iid = row.get("id") if isinstance(row, dict) else None
                    record["create"] = {"id": iid}
                    if iid is not None:
                        doc = await client.request("GET", f"/CustomIntegration/{iid}", timeout=30)
                        record["verify"] = isinstance(doc, dict) and doc.get(
                            "authorizationtype"
                        ) == (body[0] if isinstance(body, list) else body).get("authorizationtype")
                        # cascade: methods against this integration
                        mkey = next((k for k in files if k.startswith("methods:")), None)
                        if mkey:
                            methods = json.loads(files[mkey].read_text(encoding="utf-8"))
                            created = []
                            for m in methods:
                                try:
                                    r = await client.request(
                                        "POST",
                                        "/CustomIntegrationMethod",
                                        json_body=[
                                            {k: v for k, v in m.items() if not k.startswith("_")}
                                            | {"integration_id": iid}
                                        ],
                                        timeout=45,
                                    )
                                    mr = r[0] if isinstance(r, list) and r else r
                                    created.append({"id": mr.get("id"), "name": m.get("name")})
                                except Exception as exc:  # noqa: BLE001
                                    created.append({"name": m.get("name"), "error": str(exc)[:200]})
                            record["methods"] = created
                            for c in created:
                                if c.get("id") is not None:
                                    try:
                                        await client.request(
                                            "DELETE",
                                            f"/CustomIntegrationMethod/{c['id']}",
                                            timeout=30,
                                        )
                                    except Exception:  # noqa: BLE001
                                        record.setdefault("cleanup_errors", []).append(
                                            f"method {c['id']}"
                                        )
                        await client.request("DELETE", f"/CustomIntegration/{iid}", timeout=30)
                        record["cleanup"] = "integration deleted"
                elif kind == "runbook":
                    record.update(await _apply_runbook(client, body, name))
                # (methods-only files apply through their integration above)
            except Exception as exc:  # noqa: BLE001
                record["error"] = str(exc)[:400]
            ev["applied"][key] = record

    evidence_path.write_text(json.dumps(ev, indent=2) + "\n", encoding="utf-8")
    return ev


async def _apply_runbook(client: Any, payload: dict, name: str) -> dict[str, Any]:
    """Create with a bounded shape ladder, verify, fire the public trigger, delete."""
    out: dict[str, Any] = {}
    base = {**payload, "name": f"{PROBE}-{name}"[:80], "type": 1, "active": False}
    ladders: list[tuple[str, dict]] = [
        # lead with the public-endpoint shape: Bifrost triggers Halo runbooks
        # via POST /api/automation/{id}. ACTIVE because the probe fires it
        # once and deletes it immediately - the converter itself emits
        # active:false and activation stays a deliberate operator act.
        (
            "converted_public_active",
            {**base, "active": True, "runbook_start_type": 1, "inbound_authentication_type": 0},
        ),
        ("converted_public", {**base, "runbook_start_type": 1, "inbound_authentication_type": 0}),
        ("converted", base),
        (
            "public_no_inputvars",
            {
                k: v
                for k, v in {
                    **base,
                    "runbook_start_type": 1,
                    "inbound_authentication_type": 0,
                }.items()
                if k != "input_variables"
            },
        ),
        (
            "public_empty_steps",
            {
                **{k: v for k, v in base.items() if k != "input_variables"},
                "steps": [],
                "runbook_start_type": 1,
                "inbound_authentication_type": 0,
            },
        ),
        ("bare", {"name": base["name"], "type": 1, "steps": []}),
    ]
    wid = None
    attempts: list[str] = []
    for label, doc in ladders:
        try:
            resp = await client.request("POST", "/Webhook", json_body=[doc], timeout=60)
            row = resp[0] if isinstance(resp, list) and resp else resp
            wid = row.get("id") if isinstance(row, dict) else None
            attempts.append(f"{label}: ok id={wid}")
            out["shape_winner"] = label
            break
        except Exception as exc:  # noqa: BLE001
            attempts.append(f"{label}: {str(exc)[:170]}")
    out["attempts"] = attempts

    if wid is None:
        return out

    # verify
    try:
        doc = await client.request(
            "GET", f"/Webhook/{wid}", params={"includedetails": "true"}, timeout=45
        )
        steps = doc.get("steps") if isinstance(doc, dict) else None
        out["verify"] = {
            "type": doc.get("type") if isinstance(doc, dict) else None,
            "steps": len(steps) if isinstance(steps, list) else steps,
            "expected_steps": len(payload.get("steps") or []),
            "input_variables": len(doc.get("input_variables") or [])
            if isinstance(doc, dict)
            else None,
            "start_type": doc.get("runbook_start_type") if isinstance(doc, dict) else None,
        }
    except Exception as exc:  # noqa: BLE001
        out["verify"] = {"error": str(exc)[:200]}

    # external trigger fire (only when the public start type made it through)
    try:
        check = await client.request("GET", f"/Webhook/{wid}", timeout=30)
        if isinstance(check, dict) and check.get("runbook_start_type") == 1:
            # fire + prove execution with a runlog diff (the runbook was
            # created ACTIVE by the winning ladder rung for exactly this)
            log = await client.request("GET", "/Automation", params={"count": "20"}, timeout=45)
            rows = (
                log
                if isinstance(log, list)
                else next((v for v in log.values() if isinstance(v, list)), [])
            )
            before_ids = {r.get("id") for r in rows}
            resp = await client.request(
                "POST",
                f"/Automation/{wid}",
                json_body={
                    "formCollection": [{"Key": "halocli_probe", "Value": "bifrost-trigger"}]
                },
                timeout=90,
            )
            fired: dict[str, Any] = {"ok": True, "response": str(resp)[:300]}
            for _ in range(6):
                time.sleep(2)
                log = await client.request("GET", "/Automation", params={"count": "20"}, timeout=45)
                rows = (
                    log
                    if isinstance(log, list)
                    else next((v for v in log.values() if isinstance(v, list)), [])
                )
                new = [r for r in rows if r.get("id") not in before_ids]
                if new:
                    fired["runlog_row"] = {
                        k: new[0].get(k)
                        for k in ("id", "runbook_id", "runbook_name", "status", "error")
                        if k in new[0]
                    }
                    break
            out["trigger_fire"] = fired
        else:
            out["trigger_fire"] = "skipped: runbook_start_type != 1 (shape ladder fell back)"
    except Exception as exc:  # noqa: BLE001
        out["trigger_fire"] = f"error: {str(exc)[:300]}"

    # cleanup
    try:
        await client.request("DELETE", f"/Webhook/{wid}", timeout=30)
        try:
            await client.request("GET", f"/Webhook/{wid}", timeout=30)
            out["cleanup"] = "STILL READABLE"
        except Exception:  # noqa: BLE001
            out["cleanup"] = "deleted (clean)"
    except Exception as exc:  # noqa: BLE001
        out["cleanup"] = f"DELETE failed {str(exc)[:200]}"
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--integrations", help=".bifrost/integrations.yaml")
    parser.add_argument("--integrations-json", help="Bifrost GET /integrations JSON")
    parser.add_argument("--integration", help="name/id filter")
    parser.add_argument("--methods", help="explicit methods YAML/JSON list")
    parser.add_argument("--methods-integration", help="owner integration name for --methods")
    parser.add_argument("--extract-module", help="Python module to scan for literal HTTP calls")
    parser.add_argument("--workflow-file", help="Python source with @workflow functions")
    parser.add_argument("--function", help="workflow function name")
    parser.add_argument("--workflows-yaml", help=".bifrost/workflows.yaml")
    parser.add_argument("--workflow", help="workflow name in workflows.yaml")
    parser.add_argument("--out", default="./halo_out", help="output directory")
    parser.add_argument("--apply", action="store_true", help="round-trip payloads on a tenant")
    parser.add_argument("--profile", default="dev", help="profile for --apply (prod refused)")
    args = parser.parse_args()

    if not any(
        (args.integrations, args.integrations_json, args.workflow_file, args.workflows_yaml)
    ):
        parser.error(
            "need at least one source: --integrations/--integrations-json/--workflow-file/--workflows-yaml"
        )

    out_dir = Path(args.out)
    built = build_outputs(args, out_dir)
    print(json.dumps({"files": built["files"]}, indent=2))
    print(json.dumps(built["report"], indent=2))

    if args.apply:
        files = {k: Path(v) for k, v in built["files"].items()}
        ev = asyncio.run(
            apply_outputs(files, args.profile, out_dir / "bifrost_conversion_evidence.json")
        )
        print(json.dumps(ev, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
