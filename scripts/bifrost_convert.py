#!/usr/bin/env python3
"""Convert Bifrost artifacts into Halo documents (offline) or apply them.

Offline (no network, safe anywhere):

    python scripts/bifrost_convert.py ^
        --integrations <bifrost>/.bifrost/integrations.yaml --integration NinjaOne ^
        --methods demo_methods.yaml ^
        --workflow-file functions/voicemail_routing.py --function inspect_voicemail_customer ^
        --phase-bindings bindings.json ^
        --out ./halo_out

Writes ``halo_out/`` with:
- ``integration__<name>.json``   POST /CustomIntegration body (one-element array)
- ``methods__<name>.json``       POST /CustomIntegrationMethod bodies (cascade;
                                 ``_bind_phase`` marks runbook wiring for --apply)
- ``runbook__<name>.json``       POST /Webhook {type:1} body - an EXECUTABLE
                                 graph (steps + action edges, proven by
                                 scripts/runbook_chain_matrix.py); an
                                 ``_phase_bindings`` sidecar maps step ids to
                                 method names when the id is not yet known
- ``conversion_report.json``     every note: what stayed human, what moved

``--apply --profile dev`` round-trips everything against the trial in three
phases so bindings are alive when it matters (production is refused):

1. create: integrations -> methods (ids captured) -> runbooks
   (``_phase_bindings`` resolved to the created method ids and stripped)
2. fire:   POST /Automation/{id} with formCollection from the runbook's own
   input_variables (plus ``--form key=value`` overrides), then a runlog
   capture (list poll, by-id fallback) recording steps_executed /
   runbook_step / status / error
3. cleanup: reverse order - runbooks, methods, integrations (self-cleaning)

    python scripts/bifrost_convert.py ... --apply --profile dev
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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
    match_event,
)

PROBE = "haloclidevprobe"


def _safe_path(p: str | Path) -> Path:
    """Canonicalize a CLI-supplied path before touching the disk (S8707).

    Operator tool: paths are the point, but every read/write resolves
    through here so ``..``/symlink tricks land on a concrete absolute
    path instead of a relative traversal.
    """
    return Path(p).expanduser().resolve()


def _load_structured(path: Path) -> Any:
    path = _safe_path(path)
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
        source = _safe_path(args.workflow_file).read_text(encoding="utf-8")
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

    phase_bindings: dict[str, int | str] = {}
    if args.phase_bindings:
        phase_bindings = json.loads(_safe_path(args.phase_bindings).read_text(encoding="utf-8"))
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
    all_entries = _integrations_entries(args)
    method_owner = args.methods_integration or (
        str(all_entries[0].get("name")) if all_entries else None
    )
    if args.methods:
        doc = _load_structured(_safe_path(args.methods))
        methods = doc.get("methods", doc) if isinstance(doc, dict) else doc
    elif args.extract_module:
        source = _safe_path(args.extract_module).read_text(encoding="utf-8")
        methods = extract_http_methods(source)
        report["methods"].append(
            {
                "source": str(args.extract_module),
                "extracted": len(methods),
                "note": (
                    'literal/assembled client.<verb>("/path") calls only - fully '
                    "dynamic URLs cannot be extracted; list those methods explicitly "
                    "via --methods"
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
        # methods' bind_phase entries auto-wire their phase -> method name;
        # an explicit --phase-bindings file wins over the auto derivation
        auto = {
            str(m["bind_phase"]): str(m["name"])
            for m in methods
            if m.get("bind_phase") and m.get("name")
        }
        auto.update(phase_bindings)
        triggers = [t.strip() for t in (args.triggers or "").split(",") if t.strip()]
        conv: Conversion = convert_workflow(
            row, source, function, phase_bindings=auto or None, triggers=triggers or None
        )
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
    files: dict[str, Path],
    profile_name: str,
    evidence_path: Path,
    form_overrides: dict[str, str],
) -> dict[str, Any]:
    """Three phases: create -> fire -> cleanup (bindings alive at fire time)."""
    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(profile_name)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")

    ev: dict[str, Any] = {"tenant": host, "applied": {}}
    method_ids: dict[str, int] = {}
    async with HaloClient(profile, profile_name=profile_name) as client:
        # ---- phase 1: create (no deletes yet) --------------------------
        for key, path in sorted(files.items()):
            kind, name = key.split(":", 1)
            body = json.loads(path.read_text(encoding="utf-8"))
            record: dict[str, Any] = {"file": str(path)}
            ev["applied"][key] = record
            try:
                if kind == "integration":
                    item = body[0] if isinstance(body, list) else body
                    item = {**item, "name": f"{PROBE}-{name}"[:60]}
                    resp = await client.request(
                        "POST", "/CustomIntegration", json_body=[item], timeout=60
                    )
                    row = resp[0] if isinstance(resp, list) and resp else resp
                    iid = row.get("id") if isinstance(row, dict) else None
                    record["create"] = {"id": iid}
                    if iid is not None:
                        doc = await client.request("GET", f"/CustomIntegration/{iid}", timeout=30)
                        record["verify"] = isinstance(doc, dict) and doc.get(
                            "authorizationtype"
                        ) == item.get("authorizationtype")
                        record["_integration_id"] = iid
                        # cascade: methods against this integration (kept
                        # alive until phase 3; ids feed runbook bindings)
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
                                    mid = mr.get("id") if isinstance(mr, dict) else None
                                    entry = {
                                        "id": mid,
                                        "name": m.get("name"),
                                        "bind_phase": m.get("_bind_phase"),
                                    }
                                    if mid is not None:
                                        # BEHAVIORAL check: _test executes the
                                        # method's real HTTP call (bounded ladder
                                        # for the exact payload shape)
                                        for t_body in (
                                            {"id": mid, "_test": True},
                                            {
                                                "id": mid,
                                                "_test": True,
                                                "_test_runbook_variables": [],
                                            },
                                        ):
                                            try:
                                                tr = await client.request(
                                                    "POST",
                                                    "/CustomIntegrationMethod",
                                                    json_body=[t_body],
                                                    timeout=90,
                                                )
                                                entry["test_response"] = str(tr)[:400]
                                                break
                                            except Exception as texc:  # noqa: BLE001
                                                entry["test_error"] = str(texc)[:250]
                                    created.append(entry)
                                    if mid is not None and m.get("name"):
                                        method_ids[str(m["name"])] = mid
                                except Exception as exc:  # noqa: BLE001
                                    created.append({"name": m.get("name"), "error": str(exc)[:200]})
                            record["methods"] = created
                elif kind == "runbook":
                    record.update(
                        await _create_runbook(client, body, name, method_ids, form_overrides)
                    )
                # methods files apply through their integration above
            except Exception as exc:  # noqa: BLE001
                record["error"] = str(exc)[:400]

        # ---- phase 2: fire (method ids alive) --------------------------
        for key, record in ev["applied"].items():
            if not key.startswith("runbook:"):
                continue
            wid = record.get("id")
            if not wid or record.get("create_error"):
                continue
            _, name = key.split(":", 1)
            inputs = record.get("_inputs") or {}
            record.update(await _fire_runbook(client, str(wid), inputs))
            # branch leg 2 (notmet): the DOCUMENT is authoritative for
            # <<var>>, so the blanked-input path needs a second runbook
            # created fresh (create beats merge-update for input vars).
            raw_payload = json.loads(Path(record["file"]).read_text(encoding="utf-8"))
            # --form overrides apply to the twin too (before array blanking)
            for v in raw_payload.get("input_variables") or []:
                key = str(v.get("key"))
                if key in form_overrides:
                    v["value"] = form_overrides[key]
            blank_inputs: dict[str, str] = {}
            has_array = False
            for v in raw_payload.get("input_variables") or []:
                value = str(v.get("value") or "")
                try:
                    parsed: Any = json.loads(value)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, list) and parsed:
                    v["value"] = "[]"  # valid empty array: "" throws in criteria
                    has_array = True
                blank_inputs[str(v.get("key"))] = str(v.get("value") or "")
            if has_array and isinstance(record.get("runlog"), dict):
                # no _triggers on the twin: one binding set is enough for
                # the proof, and cleanup only tracks the main runbook's
                branch_payload = {k: v for k, v in raw_payload.items() if k != "_triggers"}
                rec2 = await _create_runbook(
                    client, branch_payload, f"{name}-notmet", method_ids, {}
                )
                record["branch_create"] = {
                    k: rec2.get(k)
                    for k in ("id", "shape_winner", "verify", "category_group")
                    if k in rec2
                }
                bid = rec2.get("id")
                if bid:
                    record["branch_runbook_id"] = bid
                    record["branch_fire_notmet"] = await _fire_runbook(
                        client, str(bid), blank_inputs
                    )
            # event-trigger proof: bindings must fire natively on a Halo
            # event (trial-proven pattern: eventno3 + API-created ticket
            # -> runlog status2)
            if record.get("trigger_binding_ids"):
                record["trigger_proof"] = await _event_proof(client, str(wid))

        # ---- phase 3: cleanup (reverse) --------------------------------
        for key in sorted(files, reverse=True):
            record = ev["applied"].get(key)
            if not record:
                continue
            try:
                if key.startswith("runbook:") and record.get("id"):
                    # children first: trigger bindings, then both runbooks
                    for trig_id in record.get("trigger_binding_ids") or []:
                        try:
                            await client.request("DELETE", f"/Notification/{trig_id}", timeout=30)
                        except Exception as exc:  # noqa: BLE001
                            record.setdefault("cleanup_errors", []).append(
                                f"binding {trig_id}: {str(exc)[:130]}"
                            )
                    rids = [record["id"], record.get("branch_runbook_id")]
                    for rid in [r for r in rids if r]:
                        await client.request("DELETE", f"/Webhook/{rid}", timeout=30)
                    record["cleanup"] = await _gone(
                        client, "DELETE check", f"/Webhook/{record['id']}"
                    )
                elif key.startswith("integration:") and record.get("_integration_id"):
                    iid = record.pop("_integration_id")
                    for c in record.get("methods") or []:
                        if c.get("id") is not None:
                            try:
                                await client.request(
                                    "DELETE", f"/CustomIntegrationMethod/{c['id']}", timeout=30
                                )
                            except Exception as exc:  # noqa: BLE001
                                record.setdefault("cleanup_errors", []).append(
                                    f"method {c['id']}: {str(exc)[:120]}"
                                )
                    await client.request("DELETE", f"/CustomIntegration/{iid}", timeout=30)
                    record["cleanup"] = "methods + integration deleted"
            except Exception as exc:  # noqa: BLE001
                record.setdefault("cleanup_errors", []).append(str(exc)[:200])

    _safe_path(evidence_path).write_text(
        json.dumps(ev, indent=2, default=str) + "\n", encoding="utf-8"
    )
    return ev


async def _gone(client: Any, label: str, path: str) -> str:
    try:
        await client.request("GET", path, timeout=30)
        return "STILL READABLE"
    except Exception:  # noqa: BLE001
        return "deleted (clean)"


async def _resolve_runbook_group(client: Any, category: str) -> dict[str, Any]:
    """Match a Bifrost category against Halo runbook groups (lookup -4).

    Fetches unfiltered and filters client-side (the ``lookupid`` query
    param returned0 rows for every value probed). Returns the matched
    group_id or a record of what was available so the evidence shows an
    honest miss - the trial defines no -4 groups (all runbooks carry
    group_id -1), and lookup creation is server-blocked there.
    """
    try:
        body = await client.request("GET", "/Lookup", params={"count": "500"}, timeout=45)
        rows = (
            body
            if isinstance(body, list)
            else next((v for v in body.values() if isinstance(v, list)), [])
        )
    except Exception as exc:  # noqa: BLE001
        return {"category": category, "error": str(exc)[:200]}
    groups = [r for r in rows if str(r.get("lookupid")) == "-4"]
    for r in groups:
        if str(r.get("name") or "").lower() == category.lower():
            return {
                "category": category,
                "matched": r.get("name"),
                "group_id": r.get("id"),
                "row": {k: r.get(k) for k in ("lookupid", "id", "name", "value2")},
            }
    return {
        "category": category,
        "group_id": None,
        "lookup_-4_rows": len(groups),
        "note": "no matching group; runs default to group_id -1"
        if not groups
        else "groups exist but none match",
    }


async def _event_catalog(client: Any) -> list[dict]:
    """lookup64 = Halo's event catalog (205 rows: id=eventno, name/value2)."""
    try:
        body = await client.request("GET", "/Lookup", params={"count": "1000"}, timeout=45)
    except Exception:  # noqa: BLE001
        return []
    rows = (
        body
        if isinstance(body, list)
        else next((v for v in body.values() if isinstance(v, list)), [])
    )
    return [r for r in rows if str(r.get("lookupid")) == "64"]


def _bound_name(raw: str) -> str:
    """Catalog -> bound-row name: strip '- All', substitute i18n tokens."""
    s = raw
    if s.lower().endswith(" - all"):
        s = s[: -len(" - all")]
    for ph, word in (("$#request", "Ticket"), ("$#technician", "Agent")):
        s = s.replace(ph, word)
    return s


async def _event_proof(client: Any, wid: str) -> dict[str, Any]:
    """Trigger proof: one probe ticket -> native Halo event -> our run.

    Request-economical (list-poll + ONE bounded by-id sweep) and always
    deletes the probe ticket it created.
    """
    out: dict[str, Any] = {}
    tid = None
    try:
        tr = await client.request(
            "POST",
            "/Tickets",
            json_body=[{"summary": f"{PROBE} trigger proof"}],
            timeout=60,
        )
        trow = tr[0] if isinstance(tr, list) and tr else tr
        tid = trow.get("id") if isinstance(trow, dict) else None
        out["ticket"] = tid
    except Exception as exc:  # noqa: BLE001
        out["ticket_error"] = str(exc)[:220]
        return out

    async def runlog_list() -> list[dict]:
        log = await client.request("GET", "/Automation", params={"count": "1000"}, timeout=45)
        return (
            log
            if isinstance(log, list)
            else next((v for v in log.values() if isinstance(v, list)), [])
        )

    rows = await runlog_list()
    before = {r.get("id") for r in rows}
    hit = None
    for attempt in range(34):  # ~100s: observed event latency ~79s
        await asyncio.sleep(3)
        rows = await runlog_list()
        hit = next(
            (r for r in rows if r.get("id") not in before and r.get("runbook_id") == wid),
            None,
        )
        if hit:
            break
        if attempt % 3 == 2:
            # periodic by-id sweep over the full-list top (the row can
            # out-run an end-only sweep by seconds)
            top = max((r.get("id") or 0) for r in rows) if rows else 0
            for cand in range(top, max(top - 6, 0), -1):
                try:
                    d2 = await client.request("GET", f"/Automation/{cand}", timeout=20)
                except Exception:  # noqa: BLE001
                    continue
                if (
                    isinstance(d2, dict)
                    and d2.get("runbook_id") == wid
                    and d2.get("id") not in before
                ):
                    hit = d2
                    break
        if hit:
            break
    # follow the found row to a terminal state (list-matches can read
    # mid-flight status1 with an empty error - observed at ~1.6s)
    if isinstance(hit, dict) and hit.get("id") is not None:
        rid = hit.get("id")
        for _ in range(15):
            if (hit.get("status") or 1) != 1 or (hit.get("error") or ""):
                break
            await asyncio.sleep(2)
            try:
                nxt = await client.request("GET", f"/Automation/{rid}", timeout=20)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(nxt, dict):
                hit = nxt
    out["runlog"] = (
        {
            k: hit.get(k)
            for k in ("id", "status", "error", "steps_executed", "runbook_step", "execution_time")
        }
        if isinstance(hit, dict)
        else "no event-driven run observed (~100s)"
    )
    if tid is not None:
        try:
            await client.request("DELETE", f"/Tickets/{tid}", timeout=30)
            out["ticket_cleanup"] = "deleted"
        except Exception as exc:  # noqa: BLE001
            out["ticket_cleanup"] = str(exc)[:160]
    return out


async def _create_runbook(
    client: Any,
    payload: dict,
    name: str,
    method_ids: dict[str, int],
    input_overrides: dict[str, str],
) -> dict[str, Any]:
    """Resolve bindings, apply input overrides, create, verify the graph.

    ``input_overrides`` (--form) patch the DOCUMENT's input variable
    values before create: ``<<var>>`` expressions resolve from the
    document, not from formCollection (trial-observed - formCollection
    is the ``<<request>>`` payload).
    """
    out: dict[str, Any] = {}
    doc: dict[str, Any] = {k: v for k, v in payload.items() if not k.startswith("_")}
    sidecar = payload.get("_phase_bindings") or {}
    warnings = []
    for step in doc.get("steps") or []:
        mname = sidecar.get(str(step.get("step_id")))
        if mname:
            if mname in method_ids:
                step["auto_action_type"] = method_ids[mname]
            else:
                warnings.append(
                    f"step {step.get('step_id')} wanted method {mname!r} - not created; "
                    "left unbound (aa6 without a method fails at fire time)"
                )
    for v in doc.get("input_variables") or []:
        if str(v.get("key")) in input_overrides:
            v["value"] = input_overrides[str(v["key"])]
    out["_inputs"] = {
        str(v.get("key")): str(v.get("value") or "") for v in doc.get("input_variables") or []
    }
    # category sidecar -> Halo runbook group (lookup -4, matched live)
    category = payload.get("_category")
    if category:
        resolved = await _resolve_runbook_group(client, str(category))
        out["category_group"] = resolved
        if resolved.get("group_id") is not None:
            doc["group_id"] = resolved["group_id"]
    if warnings:
        out["binding_warnings"] = warnings
    base = {**doc, "name": f"{PROBE}-{name}"[:80], "type": 1, "active": False}
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
        ("bare", {"name": base["name"], "type": 1, "steps": []}),
    ]
    wid = None
    attempts: list[str] = []
    for label, doc2 in ladders:
        try:
            resp = await client.request("POST", "/Webhook", json_body=[doc2], timeout=60)
            row = resp[0] if isinstance(resp, list) and resp else resp
            wid = row.get("id") if isinstance(row, dict) else None
            attempts.append(f"{label}: ok id={wid}")
            out["shape_winner"] = label
            break
        except Exception as exc:  # noqa: BLE001
            attempts.append(f"{label}: {str(exc)[:170]}")
    out["attempts"] = attempts
    if wid is None:
        out["create_error"] = "all ladder rungs rejected"
        return out
    out["id"] = wid

    # trigger bindings: _triggers names -> lookup64 catalog -> fresh
    # POST /Notification rows (guid ALWAYS null: a copied template guid
    # upserts the template row and hijacks its owner - trial-lesson)
    triggers = payload.get("_triggers") or []
    if triggers:
        catalog = await _event_catalog(client)
        resolved: list[dict[str, Any]] = []
        unresolved: list[str] = []
        binding_ids: list[int] = []
        for t in triggers:
            m = match_event(catalog, t)
            if not m or m.get("id") is None:
                unresolved.append(t)
                continue
            clean = _bound_name(str(m.get("name") or t))
            try:
                nr = await client.request(
                    "POST",
                    "/Notification",
                    json_body=[
                        {
                            "guid": None,
                            "eventno": m["id"],
                            "name": clean,
                            "type": -2,
                            "delivery_method": 6,
                            "agent_id": 0,
                            "webhook_id": wid,
                        }
                    ],
                    timeout=60,
                )
                nrow = nr[0] if isinstance(nr, list) and nr else nr
                bid = nrow.get("id") if isinstance(nrow, dict) else None
                if bid is not None:
                    binding_ids.append(bid)
                resolved.append({"eventno": m["id"], "name": clean, "binding_id": bid})
            except Exception as exc:  # noqa: BLE001
                unresolved.append(f"{t}: {str(exc)[:180]}")
        out["triggers"] = {"resolved": resolved, "unresolved": unresolved}
        if binding_ids:
            out["trigger_binding_ids"] = binding_ids

    # verify the graph as it persisted (steps + edge counts + bindings)
    try:
        full = await client.request(
            "GET", f"/Webhook/{wid}", params={"includedetails": "true"}, timeout=45
        )
        steps = full.get("steps") if isinstance(full, dict) else None
        edges = sum(len(s.get("actions") or []) for s in (steps or []))
        edge_names = sorted(
            {
                a.get("action_name")
                for s in steps or []
                for a in s.get("actions") or []
                if a.get("action_name")
            }
        )
        bound = [s.get("step_id") for s in (steps or []) if s.get("auto_action_type") is not None]
        out["verify"] = {
            "type": full.get("type") if isinstance(full, dict) else None,
            "steps": len(steps) if isinstance(steps, list) else steps,
            "expected_steps": len(doc.get("steps") or []),
            "edges": edges,
            "edge_names": edge_names,
            "bound_steps": bound,
            "input_variables": len(full.get("input_variables") or [])
            if isinstance(full, dict)
            else None,
            "start_type": full.get("runbook_start_type") if isinstance(full, dict) else None,
            "group_id": full.get("group_id") if isinstance(full, dict) else None,
        }
    except Exception as exc:  # noqa: BLE001
        out["verify"] = {"error": str(exc)[:200]}
    return out


async def _fire_runbook(
    client: Any,
    wid: str,
    inputs: dict[str, str],
) -> dict[str, Any]:
    """Fire the public trigger, capture the runlog.

    ``inputs`` are the DOCUMENT's final input variable values (they
    drive ``<<var>>``); they ride formCollection too so the ``<<request>>``
    view carries the same data (trial-observed: formCollection alone does
    NOT populate ``<<var>>`` - the document values are authoritative).
    """
    out: dict[str, Any] = {}
    try:
        check = await client.request("GET", f"/Webhook/{wid}", timeout=30)
        if not (isinstance(check, dict) and check.get("runbook_start_type") == 1):
            out["trigger_fire"] = "skipped: runbook_start_type != 1 (create ladder fell back)"
            return out
    except Exception as exc:  # noqa: BLE001
        out["trigger_fire"] = f"pre-check failed: {str(exc)[:200]}"
        return out

    pairs = [{"Key": k, "Value": v} for k, v in inputs.items()]

    async def runlog_list() -> list[dict]:
        log = await client.request("GET", "/Automation", params={"count": "1000"}, timeout=45)
        return (
            log
            if isinstance(log, list)
            else next((v for v in log.values() if isinstance(v, list)), [])
        )

    try:
        before = {r.get("id") for r in await runlog_list()}
        resp = await client.request(
            "POST",
            f"/Automation/{wid}",
            json_body={"formCollection": pairs},
            timeout=120,
        )
        out["trigger_fire"] = {
            "ok": True,
            "response": str(resp)[:200],
            "formCollection": pairs,
        }
    except Exception as exc:  # noqa: BLE001
        out["trigger_fire"] = f"error: {str(exc)[:300]}"
        return out

    # capture the run: find the new row once, then FOLLOW it to a
    # terminal state (a mid-run row reads status1 with an empty error -
    # observed capturing step5 mid-flight at ~2.6s). Snapshots record
    # the evolution so running-vs-failed is never a guess.
    evolution: list[dict[str, Any]] = []
    found: dict | None = None
    row_id = None
    for attempt in range(34):  # ~100s: trial latency observed up to 79s
        await asyncio.sleep(3)
        if row_id is None:
            all_rows = await runlog_list()
            rows = [r for r in all_rows if r.get("id") not in before]
            mine = [r for r in rows if r.get("runbook_id") == wid]
            if mine:
                row_id = mine[0].get("id")
            elif attempt % 3 == 2:
                # periodic by-id sweep anchored on the FULL list (the
                # new-only filter reads top=0 when no other runs land,
                # which silently skipped this scan - trial bug9)
                top = max((r.get("id") or 0) for r in all_rows) if all_rows else 0
                for cand in range(top, max(top - 6, 0), -1):
                    try:
                        d2 = await client.request("GET", f"/Automation/{cand}", timeout=20)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(d2, dict) and d2.get("runbook_id") == wid:
                        row_id = d2.get("id")
                        break
        if row_id is None:
            continue
        try:
            snap = await client.request("GET", f"/Automation/{row_id}", timeout=20)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(snap, dict):
            found = snap
            evolution.append(
                {
                    "t_s": (attempt + 1) * 2,
                    "status": snap.get("status"),
                    "step": snap.get("runbook_step"),
                    "step_name": snap.get("runbook_step_name"),
                    "exec": snap.get("steps_executed"),
                    "error": (snap.get("error") or "")[:80],
                }
            )
            # status1 = running (or failed-with-error); leave only when a
            # terminal signature shows: status !=1, or an error landed
            if (snap.get("status") or 1) != 1 or (snap.get("error") or ""):
                break
    if found:
        out["runlog"] = {
            k: found.get(k)
            for k in (
                "id",
                "status",
                "error",
                "steps_executed",
                "runbook_step",
                "runbook_step_name",
                "iteration",
                "execution_time",
                "timestamp",
            )
        }
        out["evolution"] = evolution
    else:
        out["runlog"] = "row not found (list window + by-id scan)"
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
    parser.add_argument(
        "--phase-bindings",
        help='JSON {phase: method_id | "Method Name"} for aa6 step wiring',
    )
    parser.add_argument(
        "--triggers",
        default="",
        help="comma-separated Halo event names to bind as runbook triggers "
        '(e.g. "New Ticket Logged,Closed") - resolved against lookup64 and '
        "written as POST /Notification bindings at apply",
    )
    parser.add_argument("--out", default="./halo_out", help="output directory")
    parser.add_argument("--apply", action="store_true", help="round-trip payloads on a tenant")
    parser.add_argument("--profile", default="dev", help="profile for --apply (prod refused)")
    parser.add_argument(
        "--form",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a runbook input variable in the document before create "
        "(<<var>> reads the document; repeatable)",
    )
    args = parser.parse_args()

    if not any(
        (args.integrations, args.integrations_json, args.workflow_file, args.workflows_yaml)
    ):
        parser.error(
            "need at least one source: --integrations/--integrations-json/"
            "--workflow-file/--workflows-yaml"
        )

    form_overrides: dict[str, str] = {}
    for pair in args.form:
        if "=" not in pair:
            parser.error(f"--form expects KEY=VALUE, got {pair!r}")
        k, v = pair.split("=", 1)
        form_overrides[k] = v

    out_dir = _safe_path(args.out)
    built = build_outputs(args, out_dir)
    print(json.dumps({"files": built["files"]}, indent=2))
    print(json.dumps(built["report"], indent=2))

    if args.apply:
        files = {k: Path(v) for k, v in built["files"].items()}
        ev = asyncio.run(
            apply_outputs(
                files, args.profile, out_dir / "bifrost_conversion_evidence.json", form_overrides
            )
        )
        print(json.dumps(ev, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
