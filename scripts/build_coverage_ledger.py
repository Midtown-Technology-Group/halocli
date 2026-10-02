#!/usr/bin/env python3
"""Build (or check) the coverage ledger: every spec operation, explicitly classified.

The ledger is the moonshot's spine: 1,455+ spec operations each carry a
disposition so CI can enforce that nothing is silently unclassified.

    python scripts/build_coverage_ledger.py          # regenerate coverage_ledger.json
    python scripts/build_coverage_ledger.py --check  # exit 1 if stale

Inputs (all repo-owned):
  - src/halocli/spec/halo_openapi.json   the spec (source of truth for ops)
  - src/halocli/resources.py             the registry (source of truth for first-class)
  - coverage_policy.json                 segment-level human classification

Dispositions:
  first-class     reachable as a first-class command today (registry-derived)
  backlog         not yet promoted; note carries candidate/probe rationale
  deliberately-raw  argued to stay raw; reason required
  dormant         known-dead on our tenant; reason required (live evidence)
  junk            vestigial/duplicate in the spec; reason required

An op whose top-level segment is missing from coverage_policy.json is emitted
with note "UNCLASSIFIED" and fails the test gates - that is how a spec re-vendor
(or a genuinely new endpoint family) forces a human decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "src" / "halocli" / "spec" / "halo_openapi.json"
POLICY_PATH = REPO_ROOT / "coverage_policy.json"
LEDGER_PATH = REPO_ROOT / "coverage_ledger.json"
RESULTS_PATH = REPO_ROOT / "sweep_results.json"

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
REASON_REQUIRED = {"deliberately-raw", "dormant", "junk"}
DISPOSITIONS = {"first-class", "backlog", "deliberately-raw", "dormant", "junk"}

# policy disposition -> ledger disposition (candidates and probes are both backlog;
# the note keeps which kind it was).
_POLICY_MAP = {
    "covered": "first-class",          # must agree with the registry (validated below)
    "first-class-candidate": "backlog",
    "needs-live-probe": "backlog",
    "deliberately-raw": "deliberately-raw",
    "junk-or-dead": "junk",
}


def http_methods() -> set[str]:
    return HTTP_METHODS


def registry_map() -> dict[tuple[str, str], str]:
    """(path, method) -> first-class command string, derived from RESOURCES."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from halocli.resources import RESOURCES  # noqa: E402

    mapping: dict[tuple[str, str], str] = {}
    for resource in RESOURCES:
        name = resource.name
        mapping[(resource.endpoint, "get")] = f"halocli {name} list"
        if resource.supports_get:
            mapping[(f"{resource.endpoint}/{{id}}", "get")] = f"halocli {name} get"
        if resource.supports_create or resource.supports_update:
            verb = "create/update" if (
                resource.supports_create and resource.supports_update
                and resource.create_endpoint == resource.update_endpoint
            ) else ("create" if resource.supports_create else "update")
            if resource.create_endpoint:
                mapping[(resource.create_endpoint, "post")] = f"halocli {name} {verb}"
            if resource.update_endpoint and resource.update_endpoint != resource.create_endpoint:
                mapping[(resource.update_endpoint, "post")] = f"halocli {name} {verb}"
        if resource.supports_delete:
            mapping[(f"{resource.endpoint}/{{id}}", "delete")] = f"halocli {name} delete"
        for op in resource.operations:
            mapping[(op.path, op.method.lower())] = f"halocli {name} {op.name}"
    return mapping


def policy_segments() -> tuple[dict, dict, dict]:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    return policy.get("segments", {}), policy.get("overrides", {}), policy.get("op_overrides", {})


def classify(
    path: str,
    method: str,
    reg: dict,
    segments: dict,
    overrides: dict,
    op_overrides: dict,
):
    key = (path, method)
    if key in reg:
        return {"disposition": "first-class", "via": reg[key], "note": ""}
    segment = path.strip("/").split("/")[0] if path.strip("/") else ""
    op_key = f"{method.upper()} {path}"
    if op_key in op_overrides:
        entry = dict(op_overrides[op_key])
        disp = entry.get("disposition", "")
        if disp not in DISPOSITIONS:
            raise SystemExit(
                f"coverage_policy.json: op_override {op_key!r} has unknown "
                f"disposition {disp!r}"
            )
        if disp in REASON_REQUIRED and not entry.get("reason", "").strip():
            raise SystemExit(
                f"coverage_policy.json: op_override {op_key!r} disposition "
                f"{disp!r} needs a reason"
            )
        return {"disposition": disp, "via": "", "note": entry.get("reason", "")}
    seg_policy = dict(segments.get(segment) or {})
    seg_policy.update(overrides.get(segment) or {})
    if not seg_policy:
        return {
            "disposition": "backlog",
            "via": "",
            "note": "UNCLASSIFIED: segment missing from coverage_policy.json",
        }
    # Segments speak the scout's vocabulary; overrides may already speak the
    # ledger's (e.g. "dormant" from live evidence) - accept either.
    raw_disp = seg_policy.get("disposition", "")
    # A "covered" segment only guarantees SOME of its ops are registry-mapped
    # (the mapping already missed or we would not be here); an unmapped op in
    # a covered segment is a promotion candidate, not first-class.
    if raw_disp == "covered":
        return {
            "disposition": "backlog",
            "via": "",
            "note": (
                "segment is registry-covered but this operation is not "
                "registry-mapped - promote it or argue it"
            ),
        }
    disp = raw_disp if raw_disp in DISPOSITIONS else _POLICY_MAP.get(raw_disp)
    if disp is None:
        raise SystemExit(
            f"coverage_policy.json: segment {segment!r} has unknown "
            f"disposition {seg_policy.get('disposition')!r}"
        )
    reason = seg_policy.get("reason", "")
    if disp in REASON_REQUIRED and not reason:
        raise SystemExit(
            f"coverage_policy.json: segment {segment!r} disposition {disp!r} needs a reason"
        )
    return {"disposition": disp, "via": "", "note": reason}


def content_sha256(path: Path) -> str:
    """EOL-insensitive content hash.

    Windows runners check out with core.autocrlf=true, so byte-hashing the
    inputs would make the ledger "stale" on Windows checkouts only (observed
    on PR #34: spec_sha differed while Linux/mac passed). Normalize line
    endings before hashing so freshness means same content everywhere.
    """
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def build() -> dict:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    reg = registry_map()
    segments, overrides, op_overrides = policy_segments()

    # Policy must describe reality: covered markers exactly match the registry's
    # segments, and every spec segment must be classified somewhere.
    registry_segments = {p.strip("/").split("/")[0] for p, _ in reg if p.strip("/")}
    policy_covered = {s for s, v in segments.items() if v.get("disposition") == "covered"}
    if policy_covered != registry_segments:
        missing = sorted(registry_segments - policy_covered)
        extra = sorted(policy_covered - registry_segments)
        raise SystemExit(
            f"coverage_policy.json 'covered' markers disagree with the registry "
            f"(missing={missing}, stale={extra})"
        )
    spec_segments = {
        p.strip("/").split("/")[0] for p in spec["paths"] if p.strip("/")
    }
    unclassified = sorted(spec_segments - set(segments) - set(overrides))
    if unclassified:
        raise SystemExit(
            f"coverage_policy.json is missing segments present in the spec: {unclassified}"
        )

    ops = []
    for path, item in spec["paths"].items():
        if not isinstance(item, dict):
            continue
        for method in sorted(item):
            if method not in http_methods():
                continue
            entry = {"path": path, "method": method}
            entry.update(classify(path, method, reg, segments, overrides, op_overrides))
            ops.append(entry)
    ops.sort(key=lambda e: (e["path"], e["method"]))

    counts: dict[str, int] = {}
    for entry in ops:
        counts[entry["disposition"]] = counts.get(entry["disposition"], 0) + 1

    # Reference the live sweep evidence when it exists: the ledger links to
    # it, and --check stays coupled to it (re-running the sweep changes
    # totals -> ledger must be regenerated).
    meta: dict = {
        "generated_by": "scripts/build_coverage_ledger.py",
        "spec_sha256": content_sha256(SPEC_PATH),
        "policy_sha256": content_sha256(POLICY_PATH),
        "operation_count": len(ops),
        "disposition_counts": dict(sorted(counts.items())),
    }
    if RESULTS_PATH.exists():
        sweep = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        statuses: dict[str, int] = {}
        for entry in sweep.values():
            status = str(entry.get("status"))
            statuses[status] = statuses.get(status, 0) + 1
        meta["sweep"] = {
            "file": "sweep_results.json",
            "entries": len(sweep),
            "statuses": dict(sorted(statuses.items())),
        }

    return {
        "_meta": meta,
        "operations": ops,
    }


def render(ledger: dict) -> str:
    return json.dumps(ledger, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed ledger is stale")
    args = parser.parse_args()

    ledger = build()
    text = render(ledger)
    counts = ledger["_meta"]["disposition_counts"]

    if args.check:
        if not LEDGER_PATH.exists():
            print("coverage_ledger.json is missing; run scripts/build_coverage_ledger.py")
            return 1
        committed_text = LEDGER_PATH.read_text(encoding="utf-8")
        if committed_text != text:
            print(
                "coverage_ledger.json is stale (spec, policy or registry changed); "
                "run: python scripts/build_coverage_ledger.py"
            )
            committed = json.loads(committed_text)
            committed_ops = {
                (e["path"], e["method"]): e for e in committed.get("operations", [])
            }
            generated_ops = {
                (e["path"], e["method"]): e for e in ledger["operations"]
            }
            for key in sorted(set(committed_ops) | set(generated_ops)):
                old, new = committed_ops.get(key), generated_ops.get(key)
                if old != new:
                    print(f"  {key[1].upper()} {key[0]}: committed={old} generated={new}")
            old_meta = committed.get("_meta", {})
            for field in ("spec_sha256", "policy_sha256"):
                if old_meta.get(field) != ledger["_meta"].get(field):
                    print(
                        f"  meta {field}: committed={old_meta.get(field)} "
                        f"generated={ledger['_meta'].get(field)}"
                    )
            return 1
        unclassified = [e for e in ledger["operations"] if "UNCLASSIFIED" in e["note"]]
        if unclassified:
            print(f"{len(unclassified)} operations UNCLASSIFIED in coverage_policy.json")
            for entry in unclassified[:10]:
                print(f"  {entry['method'].upper()} {entry['path']}")
            return 1
        print(f"coverage ledger fresh: {counts}")
        return 0

    LEDGER_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {LEDGER_PATH} ({ledger['_meta']['operation_count']} operations)")
    print(f"dispositions: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
