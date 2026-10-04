#!/usr/bin/env python3
"""Actually make a workflow: round-trip a sanitized in-box workflow on the trial.

The promotion experiment for POST /Workflow, schooled by Halo's in-box
examples (10 shipped workflows with full includedetails documents):

1. fetch KB Draft Workflow (id18 - the smallest:1 stage,3 steps) fresh;
2. SANITIZE for create (Halo's POST-with-id = UPDATE convention, so all
   identity must go): strip top id/guid, stage id/guid/translations, step
   guid/fdid and repoint flow_id to0, action id/flow_id - keep the
   instructional content (name/stages/steps/actions/flow_chart);
3. force active=FALSE so the new workflow is inert (nothing references
   it; it can never fire on live traffic);
4. POST /Workflow as a one-element array (house wire contract), fall
   back to a bare object on shape rejection (bounded, evidence-recorded);
5. GET verify (name landed, active false, steps carried over), then
   DELETE and GET-verify gone.

SAFETY: trial only (prod refusal), the created workflow is inactive by
construction, everything probe-named for manual sweeps.

    python scripts/workflow_create_probe.py [--profile dev] [--keep]
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

PROBE_NAME = "halocli-dev-probe-workflow"
TEMPLATE_WORKFLOW_ID = 18  # KB Draft Workflow: smallest in-box example


def sanitize_for_create(doc: dict[str, Any]) -> dict[str, Any]:
    """Remove every server-owned identity; keep the instructional content."""
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if key in {"id", "guid", "in_use", "notinuse"}:
            continue
        out[key] = value
    out["name"] = PROBE_NAME
    out["active"] = False  # inert: no ticket type will ever reference it
    stages = []
    for stage in out.get("stages") or []:
        stage = dict(stage)
        stage.pop("id", None)
        stage.pop("guid", None)
        stage.pop("translations", None)  # entity_id points at the old stage id
        stage["flow_id"] = 0
        stages.append(stage)
    if stages:
        out["stages"] = stages
    steps = []
    for step in out.get("steps") or []:
        step = dict(step)
        step.pop("guid", None)
        step.pop("fdid", None)  # flow-detail id: server-owned
        step["flow_id"] = 0
        actions = []
        for action in step.get("actions") or []:
            action = dict(action)
            action.pop("id", None)
            action["flow_id"] = 0
            actions.append(action)
        if actions:
            step["actions"] = actions
        steps.append(step)
    if steps:
        out["steps"] = steps
    return out


def summarize(doc: dict[str, Any]) -> dict[str, Any]:
    steps = doc.get("steps") or []
    return {
        "name": doc.get("name"),
        "active": doc.get("active"),
        "stages": len(doc.get("stages") or []),
        "steps": len(steps),
        "actions": sum(len(s.get("actions") or []) for s in steps if isinstance(s, dict)),
        "flow_chart": bool(doc.get("flow_chart_json")),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    parser.add_argument("--keep", action="store_true", help="skip the delete+verify leg")
    args = parser.parse_args()

    from halocli.client import HaloClient
    from halocli.config import load_profile

    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if "midtowntg" in host:
        raise SystemExit("refusing: profile points at PRODUCTION")

    evidence: dict[str, Any] = {"tenant": host, "template_workflow_id": TEMPLATE_WORKFLOW_ID}
    async with HaloClient(profile, profile_name=args.profile) as client:
        # 1. template -----------------------------------------------------
        template = await client.request(
            "GET",
            f"/Workflow/{TEMPLATE_WORKFLOW_ID}",
            params={"includedetails": "true"},
            timeout=45,
        )
        evidence["template"] = summarize(template)

        # 2. sanitize + preview -------------------------------------------
        payload = sanitize_for_create(template)
        evidence["sanitized"] = summarize(payload)
        stripped = sorted(
            set(template) - set(payload)
            | {f"stages[].{k}" for k in ("id", "guid", "translations")}
            | {f"steps[].{k}" for k in ("guid", "fdid")}
            | {"steps[].actions[].id", "steps[].actions[].flow_id"}
        )
        evidence["stripped_identity_fields"] = stripped
        print("preview:", json.dumps(evidence["sanitized"]))

        # 3. create (array first - house wire contract) --------------------
        created: Any = None
        attempts = []
        for mode, body in (("array", [payload]), ("object", payload)):
            try:
                created = await client.request("POST", "/Workflow", json_body=body, timeout=60)
                attempts.append(f"{mode}:ok")
                break
            except Exception as exc:  # noqa: BLE001
                attempts.append(f"{mode}:{str(exc)[:220]}")
        evidence["create_attempts"] = attempts
        if created is None:
            evidence["outcome"] = "create failed (both shapes)"
            print(json.dumps(evidence, indent=2))
            return 1
        if isinstance(created, list):
            created = created[0] if created else {}
        new_id = created.get("id")
        evidence["created_id"] = new_id
        evidence["created_response_summary"] = (
            summarize(created) if isinstance(created, dict) else str(type(created))
        )
        if new_id is None:
            evidence["outcome"] = "created but no id in response"
            print(json.dumps(evidence, indent=2))
            return 1

        # 4. verify ---------------------------------------------------------
        try:
            verify = await client.request(
                "GET", f"/Workflow/{new_id}", params={"includedetails": "true"}, timeout=45
            )
            evidence["verify"] = summarize(verify)
            evidence["verify_ok"] = (
                isinstance(verify, dict)
                and verify.get("name") == PROBE_NAME
                and verify.get("active") is False
            )
        except Exception as exc:  # noqa: BLE001
            evidence["verify"] = {"error": str(exc)[:200]}
            evidence["verify_ok"] = False

        # 5. cleanup ---------------------------------------------------------
        if args.keep:
            evidence["outcome"] = "kept (--keep): LEFT ON TRIAL"
        else:
            try:
                await client.request("DELETE", f"/Workflow/{new_id}", timeout=45)
                gone = False
                try:
                    await client.request(
                        "GET", f"/Workflow/{new_id}", params={"includedetails": "true"}, timeout=45
                    )
                    gone = False  # still readable
                except Exception:  # noqa: BLE001
                    gone = True
                evidence["delete_verified_gone"] = gone
                evidence["outcome"] = (
                    "full round-trip proven (create -> verify -> delete -> gone)"
                    if evidence.get("verify_ok") and gone
                    else "partial (see legs)"
                )
            except Exception as exc:  # noqa: BLE001
                evidence["delete_error"] = str(exc)[:300]
                evidence["outcome"] = "DELETE failed - probe workflow LEFT ON TRIAL (inactive)"

    print(json.dumps(evidence, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
