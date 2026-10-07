#!/usr/bin/env python3
"""Capture Halo's release ladder from the community tracker (usehalo.co).

The community site (not affiliated with Halo) renders the full version
ladder inline: version + status (Stable/Beta/Unreleased) + date. This
parses that server-rendered HTML into halo_release_ladder.json so our
version snapshots can say WHICH track an instance sits on and what the
current stable/beta/unreleased lines are.

Re-run when releases move:

    python scripts/release_ladder.py

Also corrects a narrative trap we fell into: hosted_group names like
USDEMODB2-TRIALS1 say "TRIALS", not the track - usehalo.co shows2.250
went STABLE on28 Sept2026, so our trial runs a GA build ahead of prod
(2.236), not a beta.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _safe_path(p: str | Path) -> Path:
    """Canonicalize a CLI-supplied path before touching the disk (S8707)."""
    return Path(p).expanduser().resolve()


REPO_ROOT = Path(__file__).resolve().parents[1]
LADDER_FILE = REPO_ROOT / "halo_release_ladder.json"
SOURCE = "https://www.usehalo.co/"

ITEM = re.compile(
    r'tabular-nums[^>]*">(?P<version>2\.\d{3})</span>.{0,900}?'
    r"(?P<status>Stable|Beta|Unreleased)",
    re.S,
)
DATE = re.compile(r"\b(\d{1,2} [A-Z][a-z]{2,4} \d{4})\b")


def fetch_home() -> str:
    req = urllib.request.Request(SOURCE, headers={"User-Agent": "halocli-release-ladder/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_ladder(html: str) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    for m in ITEM.finditer(html):
        version = m.group("version")
        if version in seen:
            continue
        seen.add(version)
        window = html[m.start() : m.start() + 500]
        dm = DATE.search(window)
        entries.append(
            {
                "version": version,
                "status": m.group("status"),
                "date": dm.group(1) if dm else None,
            }
        )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(LADDER_FILE))
    args = parser.parse_args()

    html = fetch_home()
    ladder = parse_ladder(html)
    if len(ladder) < 10:
        raise SystemExit(f"suspiciously small ladder ({len(ladder)} entries) - site changed?")

    def vkey(v: str) -> tuple[int, ...]:
        return tuple(int(p) for p in v.split("."))

    statuses = {}
    for e in ladder:
        statuses.setdefault(e["status"], []).append(e["version"])
    current = {
        "latest_stable": max(statuses.get("Stable", []), key=vkey, default=None),
        "latest_beta": max(statuses.get("Beta", []), key=vkey, default=None),
        "latest_unreleased": max(statuses.get("Unreleased", []), key=vkey, default=None),
    }
    out = {
        "source": SOURCE,
        "note": "community-run, not affiliated with Halo; parsed from server-rendered HTML",
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current": current,
        "ladder": ladder,
    }
    _safe_path(args.out).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"{len(ladder)} versions -> {args.out}")
    print("current:", json.dumps(current))
    for e in ladder[:8]:
        print(f"  {e['version']:8} {e['status']:11} {e['date'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
