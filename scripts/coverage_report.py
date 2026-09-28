#!/usr/bin/env python3
"""Coverage oracle: diff the vendored HaloPSA spec against the HaloCLI registry.

Prints a JSON report:

* how many spec operations are reachable first-class (`halocli <res> list/get`)
* uncurated roots ranked as curation candidates (curated roots first — extending
  an existing resource is cheaper than adopting a new subsystem)
* `write_mismatches`: registry write metadata the spec does NOT support
  (the `/Contract` bug class — non-empty means a confirmed write could fail)

Usage:
    python scripts/coverage_report.py
    python scripts/coverage_report.py --top 50
    python scripts/coverage_report.py --check   # exit 1 on write mismatches (CI gate)
"""

from __future__ import annotations

import argparse
import json
import sys

from halocli.coverage import build_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=25, help="candidate roots to show")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if write metadata is unsupported by the spec (CI gate)",
    )
    args = parser.parse_args()

    report = build_report(top=args.top)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))

    if not report.get("ok"):
        return 1
    if args.check and report["write_mismatches"]:
        print(
            f"FAIL: {len(report['write_mismatches'])} write mismatch(es)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
