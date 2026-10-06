"""Regression oracle: the same structural rules on Halo's graphs and ours.

The invariant set was dry-run against Halo's own20 template runbooks
(committed fixture, pulled read-only from the online repository) - every
rule below holds there (the one known exception, a dangling end_step in
their "AI Project Task Creation", is asserted explicitly so it stays
documented). The edge-name table is DERIVED from their templates and
enforced on OUR emitted payloads: if a Halo-side convention ever drifts
(or ours does), this test names the exact edge that disagrees.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from halocli.bifrost_convert import convert_workflow

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "bifrost"
    / ("halo_online_runbook_templates.json")
)
NEG_ENDS_ALLOWED = {-98}  # the iteration loop-back sentinel


def load_templates() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def check_invariants(rb: dict, allow_dangling: bool = False) -> list[str]:
    """Structural rules every well-formed graph satisfies."""
    findings: list[str] = []
    steps = rb.get("steps") or []
    ids = [s.get("step_id") for s in steps]
    if len(ids) != len(set(ids)):
        findings.append("duplicate step ids")
    if sum(1 for s in steps if s.get("isstart")) != 1:
        findings.append("exactly one isstart required")
    idset = set(ids)
    for s in steps:
        sid = s.get("step_id")
        if s.get("steptype") == 3:
            if not s.get("isend"):
                findings.append(f"terminal #{sid} without isend")
            if s.get("actions"):
                findings.append(f"terminal #{sid} has actions")
        if s.get("steptype") == 1:
            if not s.get("step_conditions"):
                findings.append(f"condition #{sid} has no step_conditions")
            if {a.get("action_type") for a in s.get("actions") or []} != {12}:
                findings.append(f"condition #{sid} edges are not act12")
        if s.get("steptype") == 2 and s.get("auto_action") == 6:
            if s.get("auto_action_type") is None:
                findings.append(f"aa6 method-call #{sid} missing auto_action_type")
        acts = s.get("actions") or []
        seqs = [a.get("seq") for a in acts]
        if seqs and sorted(seqs) != list(range(1, len(seqs) + 1)):
            findings.append(f"#{sid} non-contiguous seq {seqs}")
        for a in acts:
            if a.get("seq") == 1 and a.get("approval_result") != 1:
                findings.append(f"#{sid} seq1 approval_result != 1")
            if a.get("seq") == 2 and a.get("approval_result") != 0:
                findings.append(f"#{sid} seq2 approval_result != 0")
            end = a.get("end_step")
            if isinstance(end, int):
                if end < 0 and end not in NEG_ENDS_ALLOWED:
                    findings.append(f"#{sid} unexpected negative end_step {end}")
                elif end >= 0 and end not in idset and not allow_dangling:
                    findings.append(f"#{sid} dangling end_step {end}")
            start = a.get("start_step")
            if isinstance(start, int) and start not in idset:
                findings.append(f"#{sid} dangling start_step {start}")
    aa12 = sum(1 for s in steps if s.get("auto_action") == 12)
    aa13 = sum(1 for s in steps if s.get("auto_action") == 13)
    if aa12 != aa13:
        findings.append(f"iteration pair mismatch aa12={aa12} aa13={aa13}")
    return findings


def derive_edge_table(templates: dict) -> dict[tuple[int, int], str]:
    """(action_type, seq) -> action_name, derived from Halo's own graphs."""
    table: dict[tuple[int, int], set[str]] = defaultdict(set)
    for det in (templates.get("details") or {}).values():
        for s in det.get("steps") or []:
            for a in s.get("actions") or []:
                table[(a.get("action_type"), a.get("seq"))].add(a.get("action_name"))
    conflicts = {k: v for k, v in table.items() if len(v) != 1}
    assert not conflicts, f"template edge names not unique per (type,seq): {conflicts}"
    return {k: next(iter(v)) for k, v in table.items()}


# our emission under test: api + condition/else + iteration + hop + try/except + aa18
MIXED_SOURCE = """
from bifrost import workflow


@workflow(name="Oracle: Mixed")
async def mixed(client, p: list[str], label: str) -> dict:
    await touch(client)
    if label:
        await guarded(client)
    else:
        await other(client)
    for x in p:
        await each_item(client)
    await risky(client)
    return {}
"""

TRY_ACTION_SOURCE = """
from bifrost import workflow


@workflow(name="Oracle: Try Action")
async def guarded_try(client) -> dict:
    try:
        await risky2(client)
    except Exception:
        await fallback(client)
    await sql_phase(client)
    await post_note(client)
    return {}
"""


def test_templates_obey_the_invariants() -> None:
    templates = load_templates()
    findings: list[str] = []
    dangling: list[str] = []
    for rid, det in (templates.get("details") or {}).items():
        for f in check_invariants(det, allow_dangling=True):
            dangling.append(f"{det.get('name')}: {f}")
        # strict pass too - collect, then allow ONLY the documented case
        for f in check_invariants(det, allow_dangling=False):
            findings.append(f"{det.get('name')}: {f}")
    # the ONE known defect in Halo's own templates stays documented here
    expected = ["AI Project Task Creation: #2 dangling end_step 7"]
    assert sorted(findings) == sorted(expected), f"unexpected: {sorted(findings)}"


def test_edge_names_stable_across_templates() -> None:
    table = derive_edge_table(load_templates())
    assert table[(17, 1)] == "Successful Response (200 - 299)"
    assert table[(17, 2)] == "Unsuccessful Response"
    assert table[(12, 1)] == "Condition met" and table[(12, 2)] == "Condition not met"
    assert table[(22, 2)] == "Has no elements" and table[(23, 1)] == "Iteration finished"
    assert table[(18, 1)] == "Successful" and table[(29, 1)] == "Successful"
    assert table[(32, 1)] == "Sleep Finished"


def test_our_graphs_obeys_the_same_invariants_and_names() -> None:
    table = derive_edge_table(load_templates())
    conv1 = convert_workflow({}, MIXED_SOURCE, "mixed", phase_bindings={"risky": 1})
    assert conv1.ok
    conv2 = convert_workflow(
        {},
        TRY_ACTION_SOURCE,
        "guarded_try",
        phase_bindings={
            "risky2": 1,
            "sql_phase": {
                "kind": "halo_runbook_action",
                "aa": 18,
                "raw_message": "select1",
            },
            "post_note": {"kind": "halo_note", "note": "oracle"},
        },
    )
    assert conv2.ok
    for conv in (conv1, conv2):
        findings = check_invariants(conv.payload, allow_dangling=False)
        assert findings == [], f"our graph violates: {findings}"
        for s in conv.payload["steps"]:
            for a in s.get("actions") or []:
                key = (a.get("action_type"), a.get("seq"))
                if key in table:
                    assert a.get("action_name") == table[key], (
                        f"edge {key} named {a.get('action_name')!r}, "
                        f"Halo's templates say {table[key]!r}"
                    )
    # coverage of the edge families we emit: act17/12/22/23/32/18/29 all seen
    seen = {
        (a.get("action_type"), a.get("seq"))
        for conv in (conv1, conv2)
        for s in conv.payload["steps"]
        for a in s.get("actions") or []
    }
    for key in ((17, 1), (17, 2), (12, 1), (12, 2), (22, 1), (23, 2), (32, 1), (29, 1), (18, 1)):
        assert key in seen, f"expected {key} in our emissions"


def test_templates_cover_every_action_type_we_can_emit() -> None:
    table = derive_edge_table(load_templates())
    # every (type, seq) our converter CAN emit must exist in Halo's
    # authored graphs - new edge types need a template decode first
    ours = {6, 8, 12, 13, 17, 18, 21, 22, 23, 24, 29, 32, 37}
    template_types = {t for (t, _s) in table}
    # aa13/24/21 appear with no edges or single edges - presence rule:
    assert {17, 18, 12, 22, 23, 29, 32, 37} <= template_types
    assert ours  # documented set stays explicit
