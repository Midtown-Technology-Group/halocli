#!/usr/bin/env python3
"""Prove the vendored spec is still operation-for-operation current upstream.

Downloads the upstream swagger (read-only) and diffs the (path, method) set
plus operationIds against src/halocli/spec/halo_openapi.json. Exit 0 when
identical, exit 1 on drift (re-run scripts/vendor_halo_spec.py then).

    python scripts/check_spec_currency.py [--url URL]
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def _safe_url(url: str) -> str:
    """Validate a CLI-supplied fetch URL before requesting it (S8703).

    Spec downloaders are https-only: no scheme tricks, no userinfo.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or "@" in parts.netloc:
        raise ValueError(f"refusing non-https spec URL: {url!r}")
    return url


REPO_ROOT = Path(__file__).resolve().parents[1]
VENDORED = REPO_ROOT / "src" / "halocli" / "spec" / "halo_openapi.json"
DEFAULT_URL = "https://dtcdev.halopsa.com/api/swagger/v2/swagger.json"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def opset(doc: dict) -> set[tuple[str, str]]:
    return {
        (path, m)
        for path, item in doc.get("paths", {}).items()
        if isinstance(item, dict)
        for m in item
        if m in METHODS
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()
    try:
        url = _safe_url(args.url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    with urllib.request.urlopen(url, timeout=60) as resp:
        upstream = json.loads(resp.read().decode("utf-8"))
    vendored = json.loads(VENDORED.read_text(encoding="utf-8"))
    up, ven = opset(upstream), opset(vendored)
    added, removed = sorted(up - ven), sorted(ven - up)
    print(f"upstream ops={len(up)} vendored ops={len(ven)}")
    for path, m in added:
        print(f"  + {m.upper()} {path}")
    for path, m in removed:
        print(f"  - {m.upper()} {path}")
    if added or removed:
        print(
            "DRIFT: re-run scripts/vendor_halo_spec.py, reconcile new ops "
            "in coverage_policy.json, regenerate the ledger and overlay prose"
        )
        return 1
    print("NO DRIFT: vendored spec is operation-for-operation current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
