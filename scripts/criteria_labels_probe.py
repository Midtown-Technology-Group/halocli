"""Read-only: sweep trial runbooks for membership-style criteria shapes.

Collects every step_conditions row whose type is outside the already-
decoded set (0,1,2,3,5,6,7,8,29,30) or whose value side looks like a
list (value_lookup, array value_int, multiselect value_type).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from halocli.client import HaloClient  # noqa: E402
from halocli.config import load_profile  # noqa: E402

KNOWN = {0, 1, 2, 3, 5, 6, 7, 8, 29, 30, -10}


def interesting(c: dict) -> bool:
    if c.get("type") not in KNOWN:
        return True
    if c.get("value_type") == "multiselect":
        return True
    if c.get("value_lookup") is not None:
        return True
    if isinstance(c.get("value_int"), list):
        return True
    return False


async def main() -> int:
    profile = load_profile("dev")
    hits: list[dict[str, Any]] = []
    scanned = 0
    async with HaloClient(profile, profile_name="dev") as client:
        listing = await client.request("GET", "/Webhook", params={"count": "1000"}, timeout=60)
        rows = listing if isinstance(listing, list) else []
        for row in rows:
            wid = row.get("id")
            if not wid:
                continue
            try:
                doc = await client.request(
                    "GET", f"/Webhook/{wid}", params={"includedetails": "true"}, timeout=45
                )
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict):
                continue
            scanned += 1
            for s in doc.get("steps") or []:
                for c in s.get("step_conditions") or []:
                    if interesting(c):
                        hits.append(
                            {
                                "runbook": doc.get("name"),
                                "step": s.get("name"),
                                "criterion": {
                                    k: c.get(k)
                                    for k in (
                                        "type",
                                        "fieldname",
                                        "tablename",
                                        "value_type",
                                        "value_type_id",
                                        "value_int",
                                        "value_string",
                                        "value_lookup",
                                        "partialmatch",
                                    )
                                    if c.get(k) is not None
                                },
                            }
                        )
    print(f"scanned {scanned} runbooks, {len(hits)} interesting criteria")
    for h in hits[:40]:
        print(json.dumps(h, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
