"""Diff the trial's own swagger against our vendored spec (beta/GA-ahead surface)."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

REPO = Path(r"C:\Users\ThomasBray\src\Midtown-Technology-Group\halocli")
OUT = REPO / "trial_spec_diff.json"


def fetch(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "halocli-spec-diff/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def opset(doc: dict) -> set[tuple[str, str]]:
    methods = {"get", "post", "put", "patch", "delete"}
    return {
        (p, m)
        for p, item in doc.get("paths", {}).items()
        if isinstance(item, dict)
        for m in item
        if m in methods
    }


def main() -> int:
    trial_url = "https://clidev.trial.usehalo.com/api/swagger/v2/swagger.json"
    try:
        raw = fetch(trial_url)
    except Exception as exc:  # noqa: BLE001
        print(f"trial swagger fetch failed: {type(exc).__name__}: {exc}")
        return 1
    trial = json.loads(raw)
    ours = json.loads((REPO / "src/halocli/spec/halo_openapi.json").read_text(encoding="utf-8"))

    t_ops = opset(trial)
    o_ops = opset(ours)
    new_ops = sorted(t_ops - o_ops)
    gone_ops = sorted(o_ops - t_ops)

    # params added to shared paths (the subtle surface)
    param_drift = {}
    for path in set(trial.get("paths", {})) & set(ours.get("paths", {})):
        for method in ("get", "post"):
            t_params = {
                p.get("name")
                for p in (trial["paths"][path].get(method) or {}).get("parameters", [])
            }
            o_params = {
                p.get("name") for p in (ours["paths"][path].get(method) or {}).get("parameters", [])
            }
            added = t_params - o_params
            if added:
                param_drift[f"{method.upper()} {path}"] = sorted(added)

    result = {
        "trial_url": trial_url,
        "trial_info": trial.get("info", {}),
        "trial_paths": len(trial.get("paths", {})),
        "trial_ops": len(t_ops),
        "vendored_paths": len(ours.get("paths", {})),
        "vendored_ops": len(o_ops),
        "trial_sha256": hashlib.sha256(raw).hexdigest()[:16],
        "vendored_sha256": hashlib.sha256(
            (REPO / "src/halocli/spec/halo_openapi.json").read_bytes()
        ).hexdigest()[:16],
        "new_ops_in_trial": [{"path": p, "method": m} for p, m in new_ops],
        "missing_from_trial": [{"path": p, "method": m} for p, m in gone_ops],
        "param_drift": dict(sorted(param_drift.items())),
    }
    OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"trial: {result['trial_paths']} paths / {result['trial_ops']} ops")
    print(f"vendored: {result['vendored_paths']} paths / {result['vendored_ops']} ops")
    print(f"NEW in trial (not in our spec): {len(new_ops)}")
    for op in new_ops[:40]:
        print(f"  + {op[1].upper():6} {op[0]}")
    print(f"gone from trial: {len(gone_ops)}")
    for op in gone_ops[:15]:
        print(f"  - {op[1].upper():6} {op[0]}")
    print(f"param drift on shared paths: {len(param_drift)}")
    for k, v in list(param_drift.items())[:15]:
        print(f"  {k}: +{v}")
    print(f"written: {OUT.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
