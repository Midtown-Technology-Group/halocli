#!/usr/bin/env python3
"""Live write-verification sweep over every first-class write-enabled resource.

The dev-tenant campaign's main event (GET nothing here - every phase that
says apply=True really writes, against the trial tenant only):

    create (probe-prefixed) -> read-back verify -> update -> delete -> verify gone

Round-2 synthesis, learned from round-1's server errors (the spec's POST
schemas are shells - required: [] everywhere - so real field knowledge
comes from live sample rows in the dev mirror):

- COPY resources: payload = a sample row copied (minus id/guid/readonly
  counts), identity fields replaced by the probe marker, sequence+1,
  bare `date` moved to now - this satisfied every "column does not allow
  nulls" DB-level rejection in round 1 in one move.
- NEEDLE borrows: a single field lifted from a sample row by key
  (assets -> assettype, actions -> actoutcome, field-infos -> table).
- EXTRAS: known shapes the errors named (appointments need an agents
  array; SLAs need priorities; certificates need a password; CSPs need
  a date; workdays-valid ids where samples carry a0 sentinel).
- CASCADE: ids created during the sweep feed later FKs and are kept in
  an id HISTORY (survives the delete phase), so contact-group-contacts
  finds contact-groups even though it was cleaned up first.
- FULL-OBJECT UPDATES: Halo rejects partial updates on some resources
  (round 1: to-dos "Ticket ID must be specified"), so updates resend
  the create fields with id and a -v2 marker.
- SELF-CLEANING + HONEST: delete runs even when update failed; every
  phase records ok/errors; evidence lands in dev_write_results.json.
- --dry-run validates every payload offline (zero network).

    python scripts/dev_write_sweep.py [--profile dev] [--only a,b] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from halocli.client import HaloClient  # noqa: E402
from halocli.config import load_profile  # noqa: E402
from halocli.mirror import default_mirror_path  # noqa: E402
from halocli.resources import RESOURCES  # noqa: E402
from halocli.utils import normalize_halo_result  # noqa: E402
from halocli.writes import delete_resource, execute_write  # noqa: E402

PROBE = "halocli-dev-probe-"
# run-unique token: names must be unique per table (round 2 caught
# Client/Outcome rejecting marker collisions)
RUN_TOKEN = __import__("uuid").uuid4().hex[:6]
EVIDENCE_FILE = REPO_ROOT / "dev_write_results.json"

TEXTY = {
    "name",
    "text",
    "summary",
    "subject",
    "description",
    "title",
    "note",
    "value",
    "variable",
    "buttonname",
    "line1",
    "letter",
    "details",
    "body",
    "template",
    "comment",
    "firstname",
    "surname",
    "outcome",
}
EMAIL_FIELDS = {"email", "emailaddress"}
DATETIME_FIELDS = {"start_date", "end_date", "due_date", "dateoccurred"}
SKIP_COPY = {"id", "guid"}
# identity columns the copy must never resend (their values differ from id)
SKIP_COPY_EXTRA: dict[str, set[str]] = {
    "holidays": {"holid"},
}

FK_MAP: dict[str, str] = {
    "client_id": "clients",
    "site_id": "sites",
    "ticket_id": "tickets",
    "fault_id": "tickets",
    "supplier_id": "suppliers",
    "item_id": "items",
    "contract_id": "contracts",
    "ccgid": "contact-groups",
    "user_id": "users",
    "status_id": "statuses",
    "priority_id": "priorities",
    "area_id": "ticket-areas",
}

# Config masters whose full sample shape is the safest payload.
COPY_LIST = {
    "priorities",
    "workdays",
    "ticket-types",
    "ticket-areas",
    "categories",
    "pdf-templates",
    "asset-groups",
    "asset-types",
    "slas",
    "stock-bins",
    "item-stocks",
    "fields",
    "custom-tables",
    "lookups",
    "holidays",
    "outcomes",
    "services",
    "service-categories",
}

# Needle borrows: resource -> [(field to send, key needles in sample)].
NEEDLES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "assets": [("assettype_id", ("assettype",))],
    "actions": [("actoutcome", ("outcome",))],
    "field-infos": [("customtable_id", ("customtable", "table"))],
    "certificates": [("password", ("password",))],
    "timesheet-events": [("ticket_id", ("ticket",))],
}


def marker(field: str) -> str:
    return f"{PROBE}{RUN_TOKEN}-{field}"[:60]


def alnum_marker(field: str) -> str:
    return "".join(ch for ch in marker(field) if ch.isalnum())


class Mirror:
    """Local read model: per-resource rows from the dev mirror DB."""

    def __init__(self, path: Path) -> None:
        import sqlite3

        if not path.exists():
            raise SystemExit(
                f"dev mirror not found at {path}; run: halocli sync --profile dev --db {path}"
            )
        self.conn = sqlite3.connect(str(path))
        self._cache: dict[str, list[dict]] = {}

    def rows(self, resource: str) -> list[dict]:
        if resource not in self._cache:
            self._cache[resource] = [
                json.loads(r[0])
                for r in self.conn.execute(
                    "SELECT data FROM mirror_rows WHERE resource = ? ORDER BY row_key",
                    (resource,),
                )
            ]
        return self._cache[resource]

    def sample(self, resource: str) -> dict | None:
        rows = self.rows(resource)
        return rows[0] if rows else None

    def first_id(self, resource: str) -> Any:
        rows = self.rows(resource)
        return rows[0].get("id") if rows else None


class Sweeper:
    def __init__(self, client: Any, mirror: Mirror, ids: dict[str, Any]) -> None:
        self.client = client
        self.mirror = mirror
        self.ids = ids  # live ids from this run (history: survives deletes)
        self.leftovers: dict[str, Any] = {}  # created but not yet deleted

    async def raw_ids(self, path: str) -> list[Any]:
        try:
            body = await self.client.request("GET", path, params={"count": "1"}, timeout=30)
        except Exception:  # noqa: BLE001
            return []
        rows = (
            body
            if isinstance(body, list)
            else next((v for v in body.values() if isinstance(v, list)), [])
        )
        return [r.get("id") for r in rows if isinstance(r, dict) and r.get("id") is not None]

    def fk_value(self, field: str) -> Any:
        f = field.lower()
        if f == "agent_id":
            for row in self.mirror.rows("agents"):
                if "thomas" in str(row.get("name", "")).lower():
                    return row.get("id")
            return self.mirror.first_id("agents")
        if f == "workday_id":
            wid = self.mirror.first_id("workdays")
            return wid if wid not in (None, 0) else 1
        if f in FK_MAP:
            return self.ids.get(FK_MAP[f]) or self.mirror.first_id(FK_MAP[f])
        return None

    async def value_for(self, field: str, resource_name: str) -> Any:
        f = field.lower()
        if f in EMAIL_FIELDS:
            return f"{PROBE}example.invalid"
        if f in TEXTY:
            if resource_name == "custom-tables":
                return alnum_marker(f)
            return marker(f)
        if f in DATETIME_FIELDS:
            base = datetime.now().replace(microsecond=0)
            if f == "end_date":
                base += timedelta(hours=1)
            return base.isoformat()
        if f == "cuid":
            ids = await self.raw_ids("/Contact")
            if ids:
                return ids[0]
            raise ValueError(f"unresolved FK {f}: no /Contact rows readable")
        if f == "ccgid":
            value = self.ids.get("contact-groups") or self.mirror.first_id("contact-groups")
            if value is None:
                raise ValueError(f"unresolved FK {f}: no contact-groups id")
            return value
        fk = self.fk_value(f)
        if fk is not None:
            return fk
        if f in FK_MAP:
            raise ValueError(f"unresolved FK {f}: no mirror rows for {FK_MAP[f]}")
        if f in {"type", "category", "state", "use", "allday", "priority_level"}:
            return 0
        return marker(f)

    def copy_sample(self, resource_name: str) -> dict[str, Any]:
        """Full sample shape: satisfies server-side NOT NULL columns wholesale."""
        sample = self.mirror.sample(resource_name)
        if not sample:
            return {}
        own_id = sample.get("id")
        skip = SKIP_COPY | SKIP_COPY_EXTRA.get(resource_name, set())
        payload: dict[str, Any] = {}
        for key, value in sample.items():
            lk = key.lower()
            if lk in skip or lk.endswith("_count") or lk.startswith("_"):
                continue
            if value == own_id and lk.endswith("id") and lk != "id":
                continue  # identity columns (holid, ...) must not be resent
            if lk in TEXTY:
                if resource_name == "custom-tables":
                    payload[key] = alnum_marker(lk)
                else:
                    payload[key] = marker(lk)
            elif lk == "sequence" and isinstance(value, int):
                payload[key] = value + 1
            elif lk == "date" and isinstance(value, str):
                payload[key] = datetime.now().replace(microsecond=0).isoformat()
            else:
                payload[key] = value
        return payload

    def needle(self, resource_name: str) -> dict[str, Any]:
        sample = self.mirror.sample(resource_name) or {}
        out: dict[str, Any] = {}
        for field, needles in NEEDLES.get(resource_name, []):
            for key, value in sample.items():
                if not any(n in key.lower() for n in needles):
                    continue
                if value in (None, "", 0, -1):
                    continue
                if field.endswith("_id") and not isinstance(value, (int, str)):
                    continue  # a name column must not be sent where an id goes
                try:
                    if field.endswith("_id"):
                        int(str(value))
                except ValueError:
                    continue
                out[field] = value
                break
        return out

    async def extras(self, resource_name: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if resource_name == "appointments":
            agent = self.fk_value("agent_id")
            if agent is not None:
                out["agents"] = [{"id": agent, "use": "agent"}]
        if resource_name == "actions":
            outcome = self.mirror.first_id("outcomes")
            if outcome is not None:
                out.setdefault("actoutcome", outcome)
                out.setdefault("outcomeid", outcome)
        if resource_name == "field-infos":
            table = self.mirror.first_id("custom-tables")
            if table is not None:
                out.setdefault("customextratableid", table)
        if resource_name == "slas":
            # priority rows are keyed by GUID while SLA wants the INT
            # (the house JOIN_FIELDS quirk, biting our own sweep)
            priority_row = self.mirror.sample("priorities") or {}
            priority_int = priority_row.get("priorityid")
            if isinstance(priority_int, int):
                out["priorities"] = [
                    {"priorityid": priority_int, "description": marker("priority")}
                ]
        if resource_name == "priorities":
            out.setdefault("endofday", False)
        if resource_name == "contract-schedule-plans":
            now = datetime.now().replace(microsecond=0).isoformat()
            out.setdefault("date", now)
            out.setdefault("hoursallocated", 0)
        if resource_name == "certificates":
            for key in ("password", "pass", "pwd"):
                out.setdefault(key, marker("password"))
        if resource_name == "asset-groups":
            out.setdefault("use", 1)
        if resource_name == "ticket-types":
            status = self.fk_value("status_id")
            if status is not None:
                for key in ("initialstatus", "initial_status", "status"):
                    out.setdefault(key, status)
        if resource_name == "timesheet-events":
            event = self.ids.get("actions") or self.mirror.first_id("actions")
            if event is not None:
                for key in ("eventid", "event_id", "actionid", "action_id"):
                    out.setdefault(key, event)
        if resource_name == "custom-tables":
            out.setdefault("name", alnum_marker("name"))
        return out

    async def build(self, resource, update: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        # custom-tables UPDATE: the server DERIVES db_name ("CT"+name) and
        # rejects a resent stale db_name with "Invalid Name" (matrix-proven
        # 2026-10-05): updates use the minimal proven shape {id, name} only.
        minimal_update = update and resource.name == "custom-tables"
        if not minimal_update:
            if resource.name in COPY_LIST:
                payload.update(self.copy_sample(resource.name))
            payload.update(self.needle(resource.name))
            payload.update(await self.extras(resource.name))
        fields = list(resource.required_create_fields)
        for field in fields:
            if field.lower() == "id":
                continue
            if field not in payload:
                payload[field] = await self.value_for(field, resource.name)
        if update:
            primary = next((f for f in fields if f.lower() in TEXTY | {"note"}), None)
            if primary:
                if minimal_update:
                    payload = {primary: alnum_marker(primary) + "v2"}
                else:
                    payload[primary] = marker(primary) + "-v2"
            payload["id"] = self.leftovers.get(resource.name) or self.ids.get(resource.name)
        return payload


def extract_id(result: Any) -> Any:
    if isinstance(result, dict):
        for key in ("id", "Id", "ID"):
            if result.get(key) not in (None, ""):
                return result.get(key)
    return None


async def verify_exists(client, resource, item_id) -> tuple[bool, str]:
    if not resource.supports_get:
        return True, "no get route (assumed; create response carried the id)"
    try:
        await client.request("GET", f"{resource.endpoint}/{item_id}", timeout=30)
        return True, "get by id ok"
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "404" in message or "not_found" in message:
            return False, f"get by id failed: {message[:160]}"
        # Halo's detail route itself can be broken (round 2: GET /Field/{id}
        # and GET /Lookup/{id} answer 400 with CREATE validation text).
        # Fall back to a bounded list search for this run's marker.
        try:
            from halocli.utils import parse_page_result

            body = await client.list_resource(resource.name, count="500")
            rows = parse_page_result(body, list_key=resource.list_key).items
            hit = any(RUN_TOKEN in json.dumps(row, default=str) for row in rows)
            return hit, (
                "list-search fallback hit (detail route errors on this endpoint)"
                if hit
                else "detail route errors and marker not found in list"
            )
        except Exception as fallback:  # noqa: BLE001
            return False, f"get failed ({message[:80]}); list fallback failed: {str(fallback)[:80]}"


async def verify_gone(client, resource, item_id) -> tuple[bool, str]:
    if not resource.supports_get:
        return True, "no get route (delete response accepted)"
    try:
        await client.request("GET", f"{resource.endpoint}/{item_id}", timeout=30)
        return False, "record STILL EXISTS after delete"
    except Exception:  # noqa: BLE001
        return True, "get after delete confirms gone"


async def sweep_resource(sweeper: Sweeper, resource, *, apply: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {"resource": resource.name, "cud": []}

    # ---- create -----------------------------------------------------------
    phase: dict[str, Any] = {"phase": "create", "update": False}
    created_id: Any = None
    try:
        payload = await sweeper.build(resource, update=False)
        outcome = await execute_write(
            sweeper.client if apply else None, resource, payload, apply=apply
        )
        phase.update(ok=bool(outcome.get("ok")), payload=outcome.get("payload"))
        if outcome.get("errors"):
            phase["errors"] = outcome["errors"]
        if outcome.get("warnings"):
            phase["warnings"] = outcome["warnings"]
        if apply and outcome.get("ok"):
            created_id = extract_id(outcome.get("result"))
            phase["id"] = created_id
    except Exception as exc:  # noqa: BLE001
        phase.update(ok=False, errors=[f"{type(exc).__name__}: {str(exc)[:300]}"])
    entry["cud"].append(phase)
    if not phase.get("ok"):
        entry["outcome"] = "create-failed"
        return entry
    if created_id is None:
        entry["outcome"] = "created-unverifiable (no id in response)"
        return entry
    sweeper.ids[resource.name] = created_id
    sweeper.leftovers[resource.name] = created_id

    # ---- verify create ----------------------------------------------------
    exists, note = await verify_exists(sweeper.client, resource, created_id)
    entry["cud"].append({"phase": "verify-create", "ok": exists, "note": note})
    if not exists:
        entry["outcome"] = "create-unverifiable"
        return entry

    # ---- update -----------------------------------------------------------
    if resource.supports_update and apply:
        phase = {"phase": "update", "update": True}
        try:
            payload = await sweeper.build(resource, update=True)
            outcome = await execute_write(
                sweeper.client, resource, payload, update=True, apply=True
            )
            phase.update(ok=bool(outcome.get("ok")))
            if outcome.get("errors"):
                phase["errors"] = outcome["errors"]
        except Exception as exc:  # noqa: BLE001
            phase.update(ok=False, errors=[f"{type(exc).__name__}: {str(exc)[:300]}"])
        entry["cud"].append(phase)

    # ---- delete: DEFERRED (see finalize_deletes). Parents must stay alive
    # until every child has consumed their id (round 2 lesson: actions and
    # to-dos referenced a ticket the sweep had already deleted).
    if resource.supports_delete:
        entry["cleanup"] = "deferred"
    else:
        entry["left_in_place"] = "POST-only resource: no delete route by contract"

    phases_ok = all(p.get("ok") for p in entry["cud"] if p.get("ok") is not None)
    entry["outcome"] = "proven-pending-cleanup" if phases_ok else "partial"
    return entry


async def finalize_deletes(client, sweeper: Sweeper, results: dict[str, Any]) -> None:
    """Phase B: delete everything we created, children before parents.

    Reverse creation order gives that for free (contact-group-contacts
    created after contact-groups, so it deletes first). Special cases:
    Halo refuses to delete a Priority that has tickets until it is marked
    inactive - update, then retry once.
    """
    for name in reversed(list(sweeper.leftovers)):
        resource = next((r for r in RESOURCES if r.name == name), None)
        if resource is None or not resource.supports_delete:
            continue  # POST-only: stays by contract, reported as left_in_place
        item_id = sweeper.leftovers[name]
        phase: dict[str, Any] = {"phase": "delete"}
        try:
            try:
                outcome = await delete_resource(client, resource, item_id, apply=True)
                ok = bool(outcome.get("ok"))
            except Exception as first:  # noqa: BLE001 - Halo RAISES on 400
                if name != "priorities":
                    raise
                # server text: "...give these tickets another Priority before
                # deleting or mark the Priority as inactive"
                phase["inactive_retry"] = True
                phase["first_error"] = str(first)[:200]
                await execute_write(
                    client,
                    resource,
                    {"id": item_id, "ishidden": True},
                    update=True,
                    apply=True,
                )
                outcome = await delete_resource(client, resource, item_id, apply=True)
                ok = bool(outcome.get("ok"))
            if not ok and name == "priorities" and not phase.get("inactive_retry"):
                phase["inactive_retry"] = True
                await execute_write(
                    client,
                    resource,
                    {"id": item_id, "ishidden": True},
                    update=True,
                    apply=True,
                )
                outcome = await delete_resource(client, resource, item_id, apply=True)
                ok = bool(outcome.get("ok"))
            phase["ok"] = ok
            if outcome.get("errors"):
                phase["errors"] = outcome["errors"]
            if ok:
                gone, gnote = await verify_gone(client, resource, item_id)
                phase["verify_gone"] = gone
                phase["note"] = gnote
                if gone:
                    sweeper.leftovers.pop(name, None)
        except Exception as exc:  # noqa: BLE001
            phase.update(ok=False, errors=[f"{type(exc).__name__}: {str(exc)[:300]}"])
        entry = results.get(name)
        if entry is not None:
            entry.setdefault("cud", []).append(phase)
            entry.pop("cleanup", None)
            ok_all = all(p.get("ok") for p in entry["cud"] if p.get("ok") is not None)
            entry["outcome"] = "proven" if ok_all else "partial"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--db", default=None)
    parser.add_argument("--only", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--nested",
        action="store_true",
        help="phase2: the nested write ops (requires the main evidence file)",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else default_mirror_path().with_name("mirror_dev.db")
    mirror = Mirror(db_path)

    targets = [r for r in RESOURCES if r.supports_write]
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = wanted - {r.name for r in targets}
        if unknown:
            raise SystemExit(f"not write-enabled resources: {sorted(unknown)}")
        targets = [r for r in targets if r.name in wanted]

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")

    if args.nested:
        # max_retries=0: report executions must never be repeated (house rule)
        exec_profile = profile.model_copy(update={"max_retries": 0})
        async with HaloClient(exec_profile, profile_name=args.profile) as client:
            nested = await nested_phase(client, mirror)
        _merge_nested(nested, host)
        print(json.dumps(nested["summary"], indent=2, sort_keys=True))
        return 0

    ids: dict[str, Any] = {}
    results: dict[str, Any] = {}
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.dry_run:
        sweeper = Sweeper(client=None, mirror=mirror, ids=ids)
        for resource in targets:
            entry: dict[str, Any] = {"resource": resource.name, "cud": []}
            for update in (False, True):
                if update and not resource.supports_update:
                    continue
                try:
                    payload = await sweeper.build(resource, update=update)
                except ValueError as exc:
                    # offline dry-run has no live cascade (e.g. ccgid needs
                    # contact-groups created first); record, don't crash
                    entry["cud"].append(
                        {
                            "phase": "update" if update else "create",
                            "ok": False,
                            "errors": [f"dry-run-unresolved: {exc}"],
                        }
                    )
                    continue
                if update:
                    payload["id"] = payload.get("id") or 0
                outcome = await execute_write(None, resource, payload, update=update, apply=False)
                entry["cud"].append(
                    {
                        "phase": "update" if update else "create",
                        "ok": bool(outcome.get("ok")),
                        **({"errors": outcome["errors"]} if outcome.get("errors") else {}),
                    }
                )
            entry["outcome"] = (
                "dry-run-clean"
                if all(p["ok"] for p in entry["cud"])
                else "dry-run-validation-errors"
            )
            results[resource.name] = entry
        summary = {
            "dry_run": True,
            "resources": len(results),
            "clean": sum(1 for e in results.values() if e["outcome"] == "dry-run-clean"),
            "invalid": sum(1 for e in results.values() if e["outcome"] != "dry-run-clean"),
        }
        print(json.dumps({"summary": summary, "results": results}, indent=2, sort_keys=True))
        return 0

    async with HaloClient(profile, profile_name=args.profile) as client:
        sweeper = Sweeper(client, mirror, ids)
        for resource in targets:
            print(f"[sweep] {resource.name} ...", file=sys.stderr)
            try:
                results[resource.name] = await sweep_resource(sweeper, resource, apply=True)
            except Exception as exc:  # noqa: BLE001
                results[resource.name] = {
                    "resource": resource.name,
                    "outcome": "error",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
            _write_evidence(results, started, host, partial=True)

        await finalize_deletes(client, sweeper, results)
        leftovers = sorted(sweeper.leftovers)

    evidence = _write_evidence(results, started, host, partial=False, leftovers=leftovers)
    print(json.dumps(evidence["summary"], indent=2, sort_keys=True))
    return 0


async def nested_phase(client, mirror: Mirror) -> dict[str, Any]:
    """Phase 2: the30 nested write ops, each with a recorded judgment.

    Target principle: act on SAMPLE rows (mirror ids) or this run's own
    probe records - never on anything that matters, even though this is a
    throwaway trial. Body principle: id-only bodies for line/approval ops
    so a validation rejection gives free evidence WITHOUT mutating
    (the server cannot apply lines we never sent).
    """
    results: list[dict[str, Any]] = []
    created_nested: dict[str, Any] = {}

    judgment = {
        ("agents", "clear-cache"): "idempotent cache clear",
        ("lookups", "clear-cache"): "idempotent cache clear (also proven by test_live_dev)",
        ("users", "prefs"): "empty-body probe; expected to read prefs, not mutate",
        ("tickets", "view"): "view counter on a sample ticket (trial)",
        ("tickets", "vote"): "vote marker on a sample ticket (trial)",
        (
            "tickets",
            "process-children",
        ): "sample ticket has no children: no-op or validation evidence",
        ("tickets", "create-object"): "empty-body semantics probe",
        ("tickets", "set-billable-project"): "points a sample ticket at a sample project (trial)",
        ("kb", "view"): "view counter on a sample KB article (trial)",
        ("kb", "vote"): "vote marker on a sample KB article (trial)",
        ("contracts", "next-ref"): "returns the next reference number; read-only effect",
        ("invoices", "pdf"): "renders a PDF for a sample invoice (trial artifact)",
        ("invoices", "view"): "view marker on a sample invoice (trial)",
        ("invoices", "update-lines"): "id-only body CANNOT mutate lines; validation evidence only",
        (
            "invoices",
            "void",
        ): "DESTRUCTIVE: voids a trial SAMPLE invoice - the dev tenant exists for exactly this",
        ("quotations", "lines"): "id-only body CANNOT add lines; validation evidence only",
        (
            "quotations",
            "approval",
        ): "expected token/signature rejection = evidence for the argued-raw stance",
        ("quotations", "view"): "view marker on a sample quotation (trial)",
        ("reports", "clone"): "clones a sample report; the clone is deleted immediately after",
        ("reports", "bookmark"): "toggles a bookmark flag on a sample report (trial)",
        (
            "reports",
            "create-pdf",
        ): "executes a sample report ONCE (house rule: never retry report execution); PDF artifact",
        (
            "reports",
            "print",
        ): "executes a sample report ONCE; print dispatch on a printer-less trial",
        ("canned-text", "favourite"): "toggles favourite on a sample canned text (trial)",
        ("email-templates", "preview"): "renders a preview; read-only effect",
        ("attachments", "presign-url"): "presign flow probe (returns a URL)",
        (
            "attachments",
            "presign-complete",
        ): "presign completion probe without prior upload; validation evidence",
        (
            "attachments",
            "create-document",
        ): "uploads a tiny probe text file; deleted immediately after",
        ("attachments", "upload-image"): "uploads a 1px probe PNG; deleted immediately after",
        ("attachments", "delete-document"): "deletes this run's own probe document",
        ("attachments", "delete-image"): "deletes this run's own probe image",
    }
    target_resource = {
        "tickets": "tickets",
        "kb": "kb",
        "contracts": "contracts",
        "invoices": "invoices",
        "quotations": "quotations",
        "reports": "reports",
        "canned-text": "canned-text",
        "email-templates": "email-templates",
    }
    exec_ops = {"create-pdf", "print"}

    pairs = [
        (r, op) for r in RESOURCES for op in r.operations if op.method.upper() in ("POST", "DELETE")
    ]
    # creations before deletes so this run's uploads have ids to delete
    pairs.sort(key=lambda p: p[1].method.upper() == "DELETE")

    png_1px = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000a49444154789c6360000002000100ffff03000006000557bfabd40000000049454e44ae426082"
    )

    for resource, op in pairs:
        entry: dict[str, Any] = {
            "resource": resource.name,
            "operation": op.name,
            "method": op.method,
            "endpoint": op.path,
            "judgment": judgment.get((resource.name, op.name), "standard probe"),
        }

        # target resolution --------------------------------------------------
        target: Any = None
        if resource.name in target_resource:
            target = mirror.first_id(target_resource[resource.name])
            if target is None:
                entry["outcome"] = "skipped (no sample rows to target)"
                results.append(entry)
                continue
        if op.method.upper() == "DELETE":
            key = "document_id" if "document" in op.name else "image_id"
            target = created_nested.get(key)
            if target is None:
                entry["outcome"] = "skipped (nothing uploaded this run)"
                results.append(entry)
                continue

        # path + body --------------------------------------------------------
        path = op.path
        if "{id}" in path:
            if target is None:
                entry["outcome"] = "skipped (path needs an id, none resolvable)"
                results.append(entry)
                continue
            path = path.replace("{id}", str(target))
        entry["target"] = target

        body: Any = None
        files = None
        if resource.name == "attachments" and op.name == "create-document":
            if op.multipart:
                files = {"file": ("halocli-dev-probe.txt", b"halocli dev probe", "text/plain")}
            else:
                body = {"filename": "halocli-dev-probe.txt", "content": "halocli dev probe"}
        elif resource.name == "attachments" and op.name == "upload-image":
            if op.multipart:
                files = {"file": ("halocli-dev-probe.png", png_1px, "image/png")}
            else:
                body = {"filename": "halocli-dev-probe.png"}
        elif resource.name == "attachments" and op.name in {"presign-url", "presign-complete"}:
            body = {}
        elif resource.name == "invoices" and op.name in {"pdf", "void"}:
            body = None  # id travels in the path
        elif resource.name == "agents" and op.name == "clear-cache":
            body = {}
        elif resource.name == "lookups" and op.name == "clear-cache":
            body = {}
        elif resource.name == "users" and op.name == "prefs":
            body = {}
        elif resource.name == "tickets" and op.name == "set-billable-project":
            body = {"id": target}
            project = mirror.first_id("projects")
            if project is not None:
                body["project_id"] = project
        else:
            body = {"id": target} if target is not None else {}

        # execute ------------------------------------------------------------
        timeout = 120.0 if op.name in exec_ops else 30.0
        attempts: list[str] = []
        try:
            kwargs: dict[str, Any] = {"timeout": timeout}
            if body is not None:
                kwargs["json_body"] = body
            if files is not None:
                kwargs["files"] = files
            try:
                result = await client.request(op.method, path, **kwargs)
            except Exception as first:  # noqa: BLE001
                # Round-1 of phase 2 proved the wire shape: most of these
                # endpoints deserialize into MODEL ARRAYS (Faults[],
                # Viewers[], "POST object length is less than 1") - one
                # bounded retry with the body wrapped as an array.
                message = str(first)
                wants_array = "[]" in message or "object length is less" in message
                if not wants_array or body is None or isinstance(body, list):
                    raise
                attempts.append("array-retry")
                kwargs["json_body"] = [body]
                result = await client.request(op.method, path, **kwargs)
            if isinstance(result, bytes):
                entry.update(ok=True, outcome="ok", response=f"<{len(result)} bytes>")
            else:
                normalized = normalize_halo_result(result)
                text = json.dumps(normalized, default=str)
                entry.update(ok=True, outcome="ok", response=text[:300])
                if resource.name == "attachments" and op.name == "create-document":
                    created_nested["document_id"] = extract_id(normalized)
                if resource.name == "attachments" and op.name == "upload-image":
                    created_nested["image_id"] = extract_id(normalized)
                if resource.name == "reports" and op.name == "clone":
                    clone_id = extract_id(normalized)
                    if clone_id is not None and resource.supports_delete:
                        try:
                            await delete_resource(client, resource, clone_id, apply=True)
                            entry["clone_cleanup"] = "deleted"
                        except Exception as cleanup:  # noqa: BLE001
                            entry["clone_cleanup"] = f"FAILED: {str(cleanup)[:160]}"
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            entry.update(ok=False, outcome="rejected", error=str(exc)[:400])
        if attempts:
            entry["attempts"] = attempts
        results.append(entry)

    summary = {
        "attempted": sum(1 for e in results if e.get("ok") is not None),
        "ok": sum(1 for e in results if e.get("ok") is True),
        "rejected": sum(1 for e in results if e.get("ok") is False),
        "skipped": sum(1 for e in results if "skipped" in str(e.get("outcome", ""))),
        "total": len(results),
    }
    return {"results": results, "summary": summary}


def _merge_nested(nested: dict[str, Any], host: str) -> None:
    if not EVIDENCE_FILE.exists():
        raise SystemExit("dev_write_results.json missing; run the main sweep first")
    evidence = json.loads(EVIDENCE_FILE.read_text(encoding="utf-8"))
    from importlib import metadata

    nested["meta"] = {
        "tenant": host,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "halocli": metadata.version("halocli"),
        "run_token": RUN_TOKEN,
    }
    evidence["nested"] = nested
    evidence["meta"]["nested_updated_at"] = nested["meta"]["captured_at"]
    EVIDENCE_FILE.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_evidence(
    results: dict[str, Any],
    started: str,
    host: str,
    *,
    partial: bool,
    leftovers: list[str] | None = None,
) -> dict[str, Any]:
    from importlib import metadata

    outcomes: dict[str, int] = {}
    for entry in results.values():
        key = str(entry.get("outcome", "?"))
        outcomes[key] = outcomes.get(key, 0) + 1
    post_only = sorted(name for name, entry in results.items() if entry.get("left_in_place"))
    evidence = {
        "meta": {
            "tenant": host,
            "profile": "dev",
            "started_at": started,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "halocli": metadata.version("halocli"),
            "run_token": RUN_TOKEN,
            "partial": partial,
        },
        "summary": {
            "resources": len(results),
            "outcomes": dict(sorted(outcomes.items())),
            "leftovers": leftovers if leftovers is not None else [],
            "left_post_only": post_only,
        },
        "results": dict(sorted(results.items())),
    }
    EVIDENCE_FILE.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return evidence


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
