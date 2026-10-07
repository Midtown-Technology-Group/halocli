#!/usr/bin/env python3
"""Read-only evidence for the offline mirror (sync/sql/standup/triage).

Settles the open design questions with live facts before the mirror ships:

1. ordering   - does `order=dateoccurred&orderdesc=true` give newest-first
                tickets? (decides sync ordering for the ops-relevant resources)
2. statuses   - which status_id values are closed-ish, and their live names
                (the ops' open/closed predicate)
3. dates      - Servosity's claim: tickets.datecreated is blank and
                dateoccurred is the real creation timestamp
4. agent_name - their claim: ticket payloads carry agent_id but no agent name
                (our sync-time label hydration should bake the name instead)
5. feed       - their claim: /Feed is a window that always refills, so a
                page-walk never terminates (why sync must not page-walk feed)
6. actions    - duplicate action ids across parents (why the mirror stores
                rows with a sequence instead of keying by id alone)

SAFETY: GET-only through HaloClient; no write path exists here.
Each probe is isolated: a failing one records its error and the rest still run.
Results print as JSON and land in mirror_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


_EP_Feed = "/Feed"
_EP_Tickets = "/Tickets"
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def rows_of(body: Any) -> list[dict]:
    if isinstance(body, list):
        return [r for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        for value in body.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


async def probe(out: dict, name: str, fn: Callable[[], Awaitable[dict] | dict]) -> None:
    try:
        result = fn()
        out[name] = await result if isinstance(result, Awaitable) else result
    except Exception as exc:  # noqa: BLE001 - evidence records failures verbatim
        out[name] = {"error": f"{type(exc).__name__}: {exc}"}


async def main() -> int:
    from halocli.client import HaloClient
    from halocli.config import load_profile
    from halocli.resources import get_resource

    profile = load_profile("thomas")
    out: dict[str, dict] = {}
    async with HaloClient(profile, profile_name="thomas") as client:

        def dates(rows: list[dict]) -> list[str]:
            return [str(r.get("dateoccurred") or "")[:19] for r in rows]

        async def ordering_probe() -> dict:
            attempts = []
            for orderdesc in ("true", "1"):
                body = await client.request(
                    "GET",
                    _EP_Tickets,
                    params={
                        "pageinate": "true",
                        "page_no": "1",
                        "page_size": "5",
                        "order": "dateoccurred",
                        "orderdesc": orderdesc,
                    },
                    timeout=60,
                )
                attempts.append({"orderdesc": orderdesc, "dates": dates(rows_of(body))})
            base = await client.request(
                "GET",
                _EP_Tickets,
                params={"pageinate": "true", "page_no": "1", "page_size": "5"},
                timeout=60,
            )
            newest_first = any(
                len(a["dates"]) > 1
                and all(a["dates"][i] >= a["dates"][i + 1] for i in range(len(a["dates"]) - 1))
                for a in attempts
            )
            return {
                "attempts": attempts,
                "baseline_no_order": dates(rows_of(base)),
                "newest_first_achieved": newest_first,
            }

        async def statuses_probe(newest_first: bool) -> dict:
            status_resource = get_resource("statuses")
            status_rows = rows_of(
                await client.list_resource("statuses", pageinate=True, page_no=1, page_size=100)
            )
            id_to_name = {
                str(r.get("id")): r.get("name") for r in status_rows if r.get("id") is not None
            }
            recent = rows_of(
                await client.request(
                    "GET",
                    _EP_Tickets,
                    params={
                        "pageinate": "true",
                        "page_no": "1",
                        "page_size": "50",
                        **({"order": "dateoccurred", "orderdesc": "true"} if newest_first else {}),
                    },
                    timeout=60,
                )
            )
            seen = sorted({str(r.get("status_id")) for r in recent})
            return {
                "status_endpoint": status_resource.endpoint,
                "map": id_to_name,
                "seen_in_recent_50": {sid: id_to_name.get(sid) for sid in seen},
                "closed_candidates": {
                    sid: name
                    for sid, name in id_to_name.items()
                    if name and "close" in str(name).lower()
                },
                "_recent": recent,  # reused by the date/agent probes
            }

        def dates_probe(recent: list[dict]) -> dict:
            return {
                "sampled": len(recent),
                "datecreated_blank": sum(1 for r in recent if r.get("datecreated") in (None, "")),
                "dateoccurred_present": sum(
                    1 for r in recent if r.get("dateoccurred") not in (None, "")
                ),
                "sample_pair": [
                    {
                        "datecreated": r.get("datecreated"),
                        "dateoccurred": r.get("dateoccurred"),
                        "datemodified": r.get("datemodified"),
                    }
                    for r in recent[:3]
                ],
            }

        def agent_probe(recent: list[dict]) -> dict:
            return {
                "sampled": len(recent),
                "agent_id_present": sum(
                    1 for r in recent if r.get("agent_id") not in (None, "", 0)
                ),
                "agent_name_present": sum(
                    1 for r in recent if r.get("agent_name") not in (None, "")
                ),
            }

        async def feed_probe() -> dict:
            f1 = rows_of(
                await client.request(
                    "GET",
                    _EP_Feed,
                    params={"pageinate": "true", "page_no": "1", "page_size": "10"},
                    timeout=60,
                )
            )
            f2 = rows_of(
                await client.request(
                    "GET",
                    _EP_Feed,
                    params={"pageinate": "true", "page_no": "2", "page_size": "10"},
                    timeout=60,
                )
            )
            ids1 = [str(r.get("id")) for r in f1]
            ids2 = [str(r.get("id")) for r in f2]
            return {
                "page1_ids": ids1,
                "page2_ids": ids2,
                "identical_pages": ids1 == ids2,
                "overlap": len(set(ids1) & set(ids2)),
                "page_size": len(f1),
                "window_stayed_full": len(f1) == 10,
            }

        async def actions_probe() -> dict:
            a1 = rows_of(
                await client.request(
                    "GET",
                    "/Actions",
                    params={"pageinate": "true", "page_no": "1", "page_size": "100"},
                    timeout=20,
                )
            )
            a2 = rows_of(
                await client.request(
                    "GET",
                    "/Actions",
                    params={"pageinate": "true", "page_no": "2", "page_size": "100"},
                    timeout=20,
                )
            )
            ids1 = [str(r.get("id")) for r in a1]
            ids2 = [str(r.get("id")) for r in a2]
            return {
                "page1_rows": len(a1),
                "page1_distinct_ids": len(set(ids1)),
                "within_page_dups": len(ids1) - len(set(ids1)),
                "cross_page_id_overlap": len(set(ids1) & set(ids2)),
                "sample_overlap_ids": sorted(set(ids1) & set(ids2))[:5],
            }

        async def ticket_flags_probe() -> dict:
            """Correlate ticket-side completion flags with status (standup
            'closed in window' needs dateclosed; triage needs current status)."""
            rows = rows_of(
                await client.request(
                    "GET",
                    _EP_Tickets,
                    params={"pageinate": "true", "page_no": "1", "page_size": "50"},
                    timeout=60,
                )
            )
            cross: dict[str, dict[str, int]] = {}
            for r in rows:
                sid = str(r.get("status_id"))
                hbc = r.get("hasbeenclosed")
                if hbc is True:
                    closed = "true"
                elif hbc is None:
                    closed = "null"
                else:
                    closed = str(hbc)
                cross.setdefault(sid, {}).setdefault(closed, 0)
                cross[sid][closed] += 1
            closed_dates = [r.get("dateclosed") for r in rows if r.get("dateclosed")]
            return {
                "hasbeenclosed_by_status": cross,
                "dateclosed_samples": closed_dates[:5],
                "date_fully_closed_samples": [
                    r.get("date_fully_closed") for r in rows if r.get("date_fully_closed")
                ][:5],
                "open_by_flag": sum(1 for r in rows if r.get("hasbeenclosed") is None),
                "closed_by_flag": sum(1 for r in rows if r.get("hasbeenclosed") is True),
            }

        await probe(out, "tickets_ordering", ordering_probe)
        newest = bool(out["tickets_ordering"].get("newest_first_achieved"))
        await probe(out, "statuses", lambda: statuses_probe(newest))
        recent = out.get("statuses", {}).pop("_recent", None)
        if recent is None:
            recent = []
            out["dates_claim"] = {"error": "skipped: statuses probe failed"}
            out["agent_name_claim"] = {"error": "skipped: statuses probe failed"}
        else:
            await probe(out, "dates_claim", lambda: dates_probe(recent))
            await probe(out, "agent_name_claim", lambda: agent_probe(recent))

        async def feed_cursor_probe() -> dict:
            """Spec claims for /Feed (count + newer/older_than_id) - live check
            before list_all walks a cursor instead of page numbers."""
            r1 = rows_of(await client.request("GET", _EP_Feed, params={"count": "5"}, timeout=30))
            ids1 = [int(r["id"]) for r in r1 if str(r.get("id", "")).isdigit()]
            result: dict[str, Any] = {"count_5_rows": len(r1), "count_5_ids": ids1}
            if ids1:
                older = rows_of(
                    await client.request(
                        "GET",
                        _EP_Feed,
                        params={"count": "5", "older_than_id": str(min(ids1))},
                        timeout=30,
                    )
                )
                oids = [int(r["id"]) for r in older if str(r.get("id", "")).isdigit()]
                result["older_than_id"] = {
                    "request": min(ids1),
                    "ids": oids,
                    "strictly_older": bool(oids) and max(oids) < min(ids1),
                    "disjoint": not (set(oids) & set(ids1)),
                }
                newer = rows_of(
                    await client.request(
                        "GET",
                        _EP_Feed,
                        params={"count": "5", "newer_than_id": str(max(ids1))},
                        timeout=30,
                    )
                )
                nids = [int(r["id"]) for r in newer if str(r.get("id", "")).isdigit()]
                result["newer_than_id"] = {
                    "request": max(ids1),
                    "ids": nids,
                    "strictly_newer": bool(nids) and min(nids) > max(ids1),
                    "note": "empty at the top of the feed is correct",
                }
            big = rows_of(
                await client.request("GET", _EP_Feed, params={"count": "500"}, timeout=60)
            )
            result["count_500_rows"] = len(big)
            duo = rows_of(
                await client.request(
                    "GET",
                    _EP_Feed,
                    params={
                        "count": "5",
                        "pageinate": "true",
                        "page_no": "2",
                        "page_size": "5",
                    },
                    timeout=30,
                )
            )
            result["paging_trio_alongside_count"] = {
                "rows": len(duo),
                "ids": [r.get("id") for r in duo[:6]],
                "still_ignored": [r.get("id") for r in duo[:6]] == ids1[:6],
            }
            return result

        await probe(out, "feed_claim", feed_probe)
        await probe(out, "feed_cursor_proof", feed_cursor_probe)
        await probe(out, "actions_dup_ids", actions_probe)
        await probe(out, "ticket_flags", ticket_flags_probe)

    text = json.dumps(out, indent=2, sort_keys=True, default=str)
    (REPO_ROOT / "mirror_evidence.json").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
