"""Bifrost -> Halo conversion: pure mapping tests (no network).

The runbook-graph assertions pin the primitives that were fire-proven on
the trial (scripts/runbook_chain_matrix.py, runbook_chain_s6b.py):
edge shapes, terminal wiring, iteration sentinel, method binding.
"""

from __future__ import annotations

import json

from halocli.bifrost_convert import (
    AUTHORIZATION_TYPE,
    DATA_TYPE,
    GRANT_TYPE,
    METHOD_VERB,
    Phase,
    build_runbook_steps,
    classify_phases,
    convert_integration,
    convert_methods,
    convert_workflow,
    extract_http_methods,
    sanitize_for_import,
    workflow_function_names,
)

OAUTH_ENTRY = {
    "id": "11111111-1111-4111-8111-111111111111",
    "name": "NinjaOne",
    "config_schema": [
        {
            "key": "base_url",
            "type": "string",
            "required": True,
            "description": "NinjaOne base URL (e.g. https://app.ninjarmm.com)",
        },
        {"key": "api_key", "type": "secret", "required": True},
    ],
    "oauth_provider": {
        "provider_name": "NinjaOne",
        "oauth_flow_type": "authorization_code",
        "authorization_url": "https://us2.ninjarmm.com/ws/oauth/authorize",
        "token_url": "https://us2.ninjarmm.com/ws/oauth/token",
        "scopes": ["monitoring", "management"],
    },
}


def test_oauth_integration_maps_to_auth_enums() -> None:
    conv = convert_integration(OAUTH_ENTRY)
    assert conv.ok
    p = conv.payload
    assert p["name"] == "NinjaOne"
    assert p["authorizationtype"] == AUTHORIZATION_TYPE["OAuth2"] == 4
    assert p["granttype"] == GRANT_TYPE["AuthorizationCode"] == 2
    assert p["authorizationurl"].startswith("https://us2")
    assert p["tokenurl"].startswith("https://us2")
    assert p["scope"] == "monitoring management"
    # literal URL example extracted from the base_url description
    assert p["resourcebaseurl"] == "https://app.ninjarmm.com"
    # secrets never invent a payload field - they become notes
    assert any("client_secret" in n for n in conv.notes)
    assert any("api_key" in n for n in conv.notes)


def test_client_credentials_flow_maps_to_grant0() -> None:
    entry = {
        "name": "HaloPSA",
        "oauth_provider": {"oauth_flow_type": "client_credentials", "scopes": ["all"]},
    }
    conv = convert_integration(entry)
    assert conv.payload["granttype"] == GRANT_TYPE["ClientCredentials"] == 0
    assert conv.payload["scope"] == "all"


def test_unknown_flow_becomes_note_not_guess() -> None:
    entry = {"name": "X", "oauth_provider": {"oauth_flow_type": "device_code"}}
    conv = convert_integration(entry)
    assert conv.payload.get("granttype") is None
    assert any("device_code" in n for n in conv.notes)


def test_api_row_shape_without_oauth_details() -> None:
    # GET /integrations fixture row: has_oauth_config but no provider block
    conv = convert_integration({"name": "Microsoft Graph", "has_oauth_config": True})
    assert conv.payload["authorizationtype"] == 4
    assert any("has_oauth_config" in n for n in conv.notes)


def test_base_url_placeholder_fallback() -> None:
    entry = {
        "name": "Thing",
        "config_schema": [{"key": "base_url", "description": "no url in here"}],
    }
    conv = convert_integration(entry)
    assert conv.payload["resourcebaseurl"] == "{{base_url}}"


def test_missing_name_is_not_converted() -> None:
    assert not convert_integration({"config_schema": []}).ok


def test_method_verb_mapping_is_non_sequential() -> None:
    assert METHOD_VERB == {"GET": 0, "POST": 1, "PUT": 2, "DELETE": 3, "PATCH": 4}
    bodies, notes = convert_methods(
        [
            {"name": "List", "path": "v1/devices", "method": "GET"},
            {"name": "Patch", "path": "/v1/x", "method": "PATCH"},
            {"name": "Del", "path": "/v1/y", "method": "DELETE"},
            {"name": "Num", "path": "/v1/z", "method": 1},
            {"name": "Odd", "path": "/v1/w", "method": "TRACE"},
        ],
        "NinjaOne",
    )
    assert [b["method"] for b in bodies] == [0, 4, 3, 1, 0]
    assert all(b["path"].startswith("/") for b in bodies)
    assert all(b["_integration_name"] == "NinjaOne" for b in bodies)
    assert any("TRACE" in n for n in notes)


def test_method_bind_phase_survives() -> None:
    bodies, _ = convert_methods(
        [{"name": "Probe", "path": "/probe", "method": "GET", "bind_phase": "fetch_remote"}],
        "Sink",
    )
    assert bodies[0]["_bind_phase"] == "fetch_remote"


def test_method_missing_fields_are_skipped_with_note() -> None:
    bodies, notes = convert_methods([{"name": "NoPath"}], "X")
    assert bodies == []
    assert any("needs both name and path" in n for n in notes)


WORKFLOW_SOURCE = '''
"""Docstring line is the fallback description."""

from bifrost import workflow


@workflow(
    name="Halo Voicemail: Inspect Customer Match",
    description="Read one Halo voicemail and match its caller.",
    category="HaloPSA",
    effects=[{"kind": "integration.read", "target": "halopsa"}],
    enforced_bounds={"max_duration_seconds": 600},
)
async def inspect_voicemail_customer(halo_ticket_id: int, note: str | None = None) -> dict:
    token = await halo_connection()
    return await inspect_ticket(token, halo_ticket_id)
'''


def test_workflow_decorator_and_signature_convert() -> None:
    conv = convert_workflow(
        {"name": "ignored-when-decorator-names"},
        WORKFLOW_SOURCE,
        "inspect_voicemail_customer",
    )
    assert conv.ok
    p = conv.payload
    assert p["type"] == 1
    assert p["active"] is False
    assert p["name"] == "Halo Voicemail: Inspect Customer Match"
    # signature -> input_variables with the SPA data_type enum
    keys = {v["key"]: v for v in p["input_variables"]}
    assert keys["halo_ticket_id"]["data_type"] == DATA_TYPE["int"] == 3
    assert keys["note"]["data_type"] == DATA_TYPE["string"] == 2  # |None unwrapped
    # hop-only graph: [phase hops..., Success] - Fail only exists when an
    # api phase can route to it (matrix: S1/S5 were [hop.., Success])
    names = [s["name"] for s in p["steps"]]
    assert names == ["halo_connection", "inspect_ticket", "Success"]
    assert p["steps"][0]["isstart"] is True
    assert p["steps"][-1]["name"] == "Success"
    assert p["steps"][-1]["isend"] is True and p["steps"][-1]["islaststep"] is True
    # wiring: every hop carries exactly the Sleep Finished edge to the next
    for s in p["steps"][:-1]:
        assert len(s["actions"]) == 1
        a = s["actions"][0]
        assert (a["action_type"], a["action_id"], a["action_name"]) == (32, -32, "Sleep Finished")
        assert a["start_step"] == s["step_id"]
    assert p["steps"][0]["actions"][0]["end_step"] == 2
    # description rides the first step's message (Halo has no description)
    assert "match its caller" in p["steps"][0]["message"]
    # structurally faithful but inert: no actions carry side effects
    assert all(s["actions"] and s.get("auto_action") in (21, None) for s in p["steps"][:-1])
    # everything that did not transfer is a note
    assert any("effects" in n for n in conv.notes)
    assert any("enforced_bounds" in n for n in conv.notes)
    assert any("HaloPSA" in n for n in conv.notes)


def test_workflow_row_only_conversion_gets_start_hop() -> None:
    conv = convert_workflow(
        {"name": "create_customer", "description": "Create the customer", "timeout_seconds": 1800}
    )
    assert conv.ok
    assert conv.payload["name"] == "create_customer"
    assert conv.payload["input_variables"] == []
    steps = conv.payload["steps"]
    assert steps[0]["name"] == "create_customer"  # neutral start hop
    assert steps[0]["isstart"] is True
    assert steps[-1]["name"] == "Success"
    assert len(steps) == 2  # no Fail terminal without an api phase


def test_workflow_missing_function_fails_cleanly() -> None:
    conv = convert_workflow({}, WORKFLOW_SOURCE, "nope")
    assert not conv.ok
    assert any("not found" in n for n in conv.notes)


# --- primitives (fire-proven recipes) ------------------------------------

API_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Sync", description="Sync things.")
async def sync_things(client, limit: int = 10, items: list[str] | None = None) -> dict:
    await asyncio.sleep(1)
    await fetch_remote(client)
    for item in items:
        await process_one(item)
    return {}
"""


def test_api_phase_binds_to_method_id_inline() -> None:
    conv = convert_workflow({}, API_SOURCE, "sync_things", phase_bindings={"fetch_remote": 32})
    assert conv.ok
    p = conv.payload
    assert "_phase_bindings" not in p  # id known inline
    api = next(s for s in p["steps"] if s.get("auto_action") == 6)
    assert api["auto_action_type"] == 32  # the CustomIntegrationMethod id
    names = [(a["action_type"], a["action_name"]) for a in api["actions"]]
    assert names == [
        (17, "Successful Response (200 - 299)"),
        (17, "Unsuccessful Response"),
    ]
    # failure edge targets the Fail terminal; Success exists too
    fail = next(s for s in p["steps"] if s["name"] == "Fail")
    assert api["actions"][1]["end_step"] == fail["step_id"]
    assert fail["auto_action"] == 1
    assert any(s["name"] == "Success" for s in p["steps"])


def test_api_phase_sidecar_when_id_unknown() -> None:
    conv = convert_workflow(
        {}, API_SOURCE, "sync_things", phase_bindings={"fetch_remote": "Probe GET"}
    )
    assert conv.ok
    p = conv.payload
    api = next(s for s in p["steps"] if s.get("auto_action") == 6)
    assert "auto_action_type" not in api  # resolved later by --apply
    assert p["_phase_bindings"][str(api["step_id"])] == "Probe GET"


def test_asyncio_sleep_becomes_timed_hop() -> None:
    conv = convert_workflow({}, API_SOURCE, "sync_things")
    steps = conv.payload["steps"]
    sleep_step = steps[0]
    assert sleep_step["auto_action"] == 21
    assert sleep_step["duration"] == 1  # the asyncio.sleep(1) argument


def test_loop_becomes_iteration_pair_with_sentinel() -> None:
    conv = convert_workflow({}, API_SOURCE, "sync_things", phase_bindings={"fetch_remote": 1})
    assert conv.ok
    p = conv.payload
    by_aa = {s.get("auto_action"): s for s in p["steps"]}
    begin = by_aa[12]
    end = by_aa[13]
    # the array source rides the marker messages (proven S6b recipe)
    assert begin["message"] == "<<items>>"
    assert end["message"] == "<<items>>"
    # begin edges: Has elements -> body, Has no elements -> Success
    assert [(a["action_type"], a["action_name"]) for a in begin["actions"]] == [
        (22, "Has elements"),
        (22, "Has no elements"),
    ]
    success = next(s for s in p["steps"] if s["name"] == "Success")
    assert begin["actions"][1]["end_step"] == success["step_id"]
    # end edges: finished -> next, Next iteration -> -98 (loop-back)
    assert end["actions"][1]["end_step"] == -98
    # loop body sits between the markers
    order = [s["step_id"] for s in p["steps"]]
    assert order.index(begin["step_id"]) < order.index(end["step_id"])
    body = next(s for s in p["steps"] if s["step_id"] == begin["actions"][0]["end_step"])
    assert body["name"] == "process_one"


def test_signature_defaults_become_input_values() -> None:
    conv = convert_workflow({}, API_SOURCE, "sync_things")
    values = {v["key"]: v for v in conv.payload["input_variables"]}
    assert values["limit"]["value"] == "10"
    assert values["items"]["value"] == "[]"  # None default -> valid empty array (not "")
    assert values["items"]["data_type"] == DATA_TYPE["Array"] == 1


def test_branches_are_noted_not_faked() -> None:
    # a guard that fits NO translation rule (call test, untyped param)
    src = API_SOURCE.replace(
        "    return {}",
        "    if is_ready(client):\n        await fetch_remote(client)\n    return {}",
    )
    conv = convert_workflow({}, src, "sync_things")
    assert any("branches" in n for n in conv.notes)
    assert not any(p.kind == "condition" for p in classify_phases(_parse_func(src)))


# --- branch translation (else-less array guards) -------------------------

GUARDED_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Guarded", description="Guard the loop.")
async def guarded(client, items: tuple[str, ...] = ()) -> dict:
    if items:
        for item in items:
            await asyncio.sleep(1)
            await process_one(item)
    return {}
"""


GUARD_MID_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Guard mid")
async def guarded_mid(client, items: list[str] | None = None) -> dict:
    if items:
        await fetch_remote(client)
    await asyncio.sleep(1)
    return {}
"""


ELSE_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Else")
async def with_else(client, items: list[str]) -> dict:
    if items:
        await fetch_remote(client)
    else:
        await fallback(client)
    return {}
"""


def _parse_func(src: str):
    import ast

    tree = ast.parse(src)
    return next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))


def test_array_guard_becomes_condition_with_criteria() -> None:
    conv = convert_workflow({}, GUARDED_SOURCE, "guarded")
    assert conv.ok
    steps = conv.payload["steps"]
    cond = next(s for s in steps if s.get("steptype") == 1)
    assert cond["auto_action"] == 6
    assert cond["name"] == "if items:"
    # criteria: the Azure-Onboarding has-elements shape on <<items>>
    (crit,) = cond["step_conditions"]
    assert crit["fieldname"] == "<<items>>"
    assert crit["tablename"] == "runbookvariable"
    assert crit["type"] == 5
    assert crit["value_type"] == "Array"
    assert crit["id"] is None and crit["chatprofile_id"] is None
    # edges: met -> guarded body (the iteration begin), notmet -> Success
    met, notmet = cond["actions"]
    assert (met["action_type"], met["action_name"]) == (12, "Condition met")
    assert (met["approval_result"], notmet["approval_result"]) == (1, 0)
    begin = next(s for s in steps if s.get("auto_action") == 12)
    assert met["end_step"] == begin["step_id"]
    assert (notmet["action_type"], notmet["action_name"]) == (12, "Condition not met")
    success = next(s for s in steps if s["name"] == "Success")
    assert notmet["end_step"] == success["step_id"]  # guard is last: skip to end
    assert any("branch(es) translated" in n for n in conv.notes)


def test_guard_notmet_targets_step_after_guard() -> None:
    conv = convert_workflow({}, GUARD_MID_SOURCE, "guarded_mid")
    assert conv.ok
    steps = conv.payload["steps"]
    cond = next(s for s in steps if s.get("steptype") == 1)
    notmet = cond["actions"][1]
    target = next(s for s in steps if s["step_id"] == notmet["end_step"])
    # the sleep AFTER the guard is the notmet target (not Success)
    assert target["auto_action"] == 21
    assert target["name"] == "sleep 1s"


def test_else_branch_translates_both_arms() -> None:
    conv = convert_workflow({}, ELSE_SOURCE, "with_else")
    assert conv.ok
    steps = conv.payload["steps"]
    cond = next(s for s in steps if s.get("steptype") == 1)
    met, notmet = cond["actions"]
    success = next(s for s in steps if s["name"] == "Success")
    # met -> then-arm (fetch_remote hop), notmet -> else-arm (fallback hop)
    then_step = next(s for s in steps if s["name"] == "fetch_remote")
    else_step = next(s for s in steps if s["name"] == "fallback")
    assert met["end_step"] == then_step["step_id"]
    assert notmet["end_step"] == else_step["step_id"]
    # the then-arm's edge SKIPS the else arm straight to after (Success)
    assert then_step["actions"][0]["end_step"] == success["step_id"]
    # the else-arm falls through to Success naturally
    assert else_step["actions"][0]["end_step"] == success["step_id"]
    assert any("translated" in n for n in conv.notes)


def test_non_array_guard_stays_flattened() -> None:
    # untyped params (client has no annotation) have no criterion mapping
    src = API_SOURCE.replace(
        "    await asyncio.sleep(1)",
        "    if client:\n        await asyncio.sleep(1)",
    )
    conv = convert_workflow({}, src, "sync_things")
    assert conv.ok
    steps = conv.payload["steps"]
    assert not any(s.get("steptype") == 1 for s in steps)
    assert any("did not fit" in n for n in conv.notes)


def test_trigger_filters_become_sidecar_with_note() -> None:
    conv = convert_workflow(
        {},
        API_SOURCE,
        "sync_things",
        triggers=["New Ticket Logged"],
        trigger_filters=[
            {
                "field": "reportedby",
                "op": "eq",
                "value": "noreply@voicemail.goto.com",
                "value_type": "string",
            }
        ],
    )
    assert conv.ok
    assert conv.payload["_trigger_filters"][0]["field"] == "reportedby"
    assert conv.payload["_triggers"] == ["New Ticket Logged"]
    assert any("subscriber-filter" in n and "blocked" in n for n in conv.notes)


def test_bare_int_guard_is_gt0_truthiness() -> None:
    src = COMPARE_SOURCE.replace("if limit > 5:", "if limit:")
    conv = convert_workflow({}, src, "compare_guards")
    assert conv.ok
    conds = [s for s in conv.payload["steps"] if s.get("steptype") == 1]
    assert conds
    (crit,) = conds[0]["step_conditions"]
    # int bare truthiness -> criteria5 (>) value_int 0, trial-proven
    assert (crit["type"], crit["fieldname"], crit["value_int"]) == (5, "<<limit>>", 0)
    assert conds[0]["name"] == "if limit:"
    assert any("misclassify" in n for n in conv.notes)


def test_negated_str_guard_is_no_value() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Not str")
async def not_str_guard(client, label: str = "") -> dict:
    if not label:
        await slow_path(client)
    else:
        await fast_path(client)
    return {}
"""
    conv = convert_workflow({}, src, "not_str_guard")
    assert conv.ok
    cond = next(s for s in conv.payload["steps"] if s.get("steptype") == 1)
    (crit,) = cond["step_conditions"]
    # criteria30 "Does not have a value" - "" -> met (trial run2589)
    assert (crit["type"], crit["fieldname"]) == (30, "<<label>>")
    assert cond["name"] == "if not label:"


ELSE_LOOP_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Loop else")
async def guarded_loop_else(client, items: list[str] | None = None) -> dict:
    if items:
        for item in items:
            await asyncio.sleep(1)
    else:
        await fallback(client)
    return {}
"""


def test_then_arm_ending_in_loop_skips_else_from_iter_end() -> None:
    """The loop-end case: the then-arm's LAST rendered node is iter_end,
    not the last body node - the skip must fire there."""
    conv = convert_workflow({}, ELSE_LOOP_SOURCE, "guarded_loop_else")
    assert conv.ok
    steps = conv.payload["steps"]
    cond = next(s for s in steps if s.get("steptype") == 1)
    iter_end = next(s for s in steps if s.get("auto_action") == 13)
    else_step = next(s for s in steps if s["name"] == "fallback")
    success = next(s for s in steps if s["name"] == "Success")
    assert cond["actions"][1]["end_step"] == else_step["step_id"]  # notmet -> else
    # the loop's "Iteration finished" edge skips the else arm -> Success
    finished = next(a for a in iter_end["actions"] if a["seq"] == 1)
    assert finished["end_step"] == success["step_id"]


def test_triggers_become_sidecar_with_note() -> None:
    conv = convert_workflow({}, API_SOURCE, "sync_things", triggers=["New Ticket Logged", "Closed"])
    assert conv.ok
    assert conv.payload["_triggers"] == ["New Ticket Logged", "Closed"]
    assert any("triggers" in n and "Notification" in n for n in conv.notes)


def test_event_name_matching_normalizes_catalog_forms() -> None:
    from halocli.bifrost_convert import match_event, normalize_event_name

    catalog = [
        {
            "lookupid": 64,
            "id": 1,
            "name": "New $#request Logged - Assigned to Recipient",
            "value2": "New $#request Logged",
        },
        {
            "lookupid": 64,
            "id": 3,
            "name": "New $#request Logged - All",
            "value2": "New $#request Logged",
        },
        {"lookupid": 64, "id": 39, "name": "Closed - All", "value2": "Closed"},
        {
            "lookupid": 64,
            "id": 76,
            "name": "$#request Status Changed  - All",
            "value2": "$#request Status Changed",
        },
    ]
    assert normalize_event_name("New $#request Logged - All") == "new ticket logged"
    # the bound-row form matches - and the "- All" scope WINS over the
    # earlier Assigned-to-Recipient row that shares value2 (eventno1 vs3:
    # binding1 fired nothing on the trial until this preference landed)
    assert match_event(catalog, "New Ticket Logged")["id"] == 3
    # exact + suffix-stripped forms
    assert match_event(catalog, "Closed")["id"] == 39
    assert match_event(catalog, "closed - all")["id"] == 39
    # placeholder substitution on the other side too
    assert match_event(catalog, "ticket status changed")["id"] == 76
    # honest miss
    assert match_event(catalog, "No Such Event") is None


COMPARE_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Compare")
async def compare_guards(client, limit: int = 10, label: str = "x") -> dict:
    if limit > 5:
        await notify_step(client)
    else:
        await legacy_step(client)
    if label == "x":
        await fast_path(client)
    return {}
"""


def test_comparison_guards_become_typed_criteria() -> None:
    conv = convert_workflow({}, COMPARE_SOURCE, "compare_guards")
    assert conv.ok
    conds = [s for s in conv.payload["steps"] if s.get("steptype") == 1]
    assert len(conds) == 2
    # `limit > 5` -> criteria type5 (Greater than), value_int on int var
    c1 = conds[0]["step_conditions"][0]
    assert (c1["type"], c1["fieldname"], c1["value_type"], c1["value_int"]) == (
        5,
        "<<limit>>",
        "int",
        5,
    )
    assert conds[0]["name"] == "if limit > 5:"
    # `label == "x"` -> type0 (Is equal to) with value_string
    c2 = conds[1]["step_conditions"][0]
    assert (c2["type"], c2["fieldname"], c2["value_type"], c2["value_string"]) == (
        0,
        "<<label>>",
        "string",
        "x",
    )
    # both conditions route with the approval_result rule + have else arms
    for cond in conds[:1]:
        met, notmet = cond["actions"]
        assert (met["approval_result"], notmet["approval_result"]) == (1, 0)
        # notmet -> the else arm (legacy_step), met -> then arm
        legacy = next(s for s in conv.payload["steps"] if s["name"] == "legacy_step")
        notify = next(s for s in conv.payload["steps"] if s["name"] == "notify_step")
        assert met["end_step"] == notify["step_id"]
        assert notmet["end_step"] == legacy["step_id"]


def test_non_literal_comparison_stays_flat() -> None:
    src = COMPARE_SOURCE.replace("if limit > 5:", "if limit > threshold:").replace(
        "client, limit: int = 10", "client, limit: int = 10, threshold: int = 5"
    )
    conv = convert_workflow({}, src, "compare_guards")
    assert conv.ok
    # only the string equality translates; the Name-right comparison flattens
    conds = [s for s in conv.payload["steps"] if s.get("steptype") == 1]
    assert len(conds) == 1
    assert any("flattened" in n or "did not fit" in n for n in conv.notes)


NOTE_SOURCE = """
from bifrost import workflow


@workflow(
    name="Demo: Note",
    description="Writes a note.",
    effects=[{"kind": "integration.write", "target": "halopsa"}],
)
async def writer(client, note_ref: str = "") -> dict:
    await fetch_remote(client)
    await post_note(client)
    return {}
"""


def test_halo_note_binding_emits_aa8_aat3_step() -> None:
    conv = convert_workflow(
        {},
        NOTE_SOURCE,
        "writer",
        phase_bindings={
            "post_note": {"kind": "halo_note", "outcome": "Internal Note", "note": "<b>hi</b>"}
        },
    )
    assert conv.ok
    steps = conv.payload["steps"]
    note = next(s for s in steps if s.get("auto_action") == 8)
    assert note["steptype"] == 2
    assert note["auto_action_type"] == 3  # aat3 = add note (SPA catalog)
    # raw Halo API body with UNQUOTED <<ticket^id>> (working-template style)
    assert '"ticket_id": <<ticket^id>>' in note["message"]
    assert '"outcome": "Internal Note"' in note["message"]
    assert '"note_html": "<b>hi</b>"' in note["message"]
    assert '"who": "Automation"' in note["message"]
    # act18 edges with the approval_result rule + Fail terminal exists
    assert [(a["action_type"], a["approval_result"]) for a in note["actions"]] == [
        (18, 1),
        (18, 0),
    ]
    assert any(s["name"] == "Fail" for s in steps)
    # a halo-note graph still completes (Fail sits last when present)
    assert any(s["name"] == "Success" for s in steps)
    assert steps[-1]["name"] == "Fail"


def test_halopsa_write_without_binding_gets_a_suggestion_note() -> None:
    conv = convert_workflow({}, NOTE_SOURCE, "writer")  # no bindings
    assert conv.ok
    assert any("halo_note" in n and "phase-bindings" in n for n in conv.notes)
    # the suggestion names the aat1/aat2 write kinds too (proven
    # on plain fires - ticket_crud_evidence.json)
    assert any("halo_ticket_create" in n and "halo_ticket_update" in n for n in conv.notes)


def test_halo_ticket_create_binding_emits_aat1() -> None:
    conv = convert_workflow(
        {},
        NOTE_SOURCE,
        "writer",
        phase_bindings={
            "post_note": {
                "kind": "halo_ticket_create",
                "body": {
                    "summary": "Escalated by halocli",
                    "reportedby": "<<request^reporter>>",
                },
            }
        },
    )
    assert conv.ok
    step = next(s for s in conv.payload["steps"] if s.get("auto_action") == 8)
    assert step["auto_action_type"] == 1  # aat1 = create ticket
    body = json.loads(step["message"].replace("<<request^reporter>>", '"x"'))
    assert body["summary"] == "Escalated by halocli"
    # <<vars>> must be UNQUOTED for interpolation (the working-template
    # convention - the probe's literal bodies carried no vars)
    assert '"<<request^reporter>>"' not in step["message"]
    assert "<<request^reporter>>" in step["message"]
    # the act18 pair with the approval_result rule (proven both ways:
    # aat2_bad took Unsuccessful off the Success path)
    assert [(a["action_type"], a["approval_result"]) for a in step["actions"]] == [
        (18, 1),
        (18, 0),
    ]


def test_halo_ticket_update_binding_emits_aat2() -> None:
    conv = convert_workflow(
        {},
        NOTE_SOURCE,
        "writer",
        phase_bindings={
            "post_note": {"kind": "halo_ticket_update", "body": {"id": 42, "summary": "upd"}}
        },
    )
    assert conv.ok
    step = next(s for s in conv.payload["steps"] if s.get("auto_action") == 8)
    assert step["auto_action_type"] == 2  # aat2 = update
    body = json.loads(step["message"])
    assert body == {"id": 42, "summary": "upd"}


def test_ticket_write_binding_without_body_gets_a_note() -> None:
    conv = convert_workflow(
        {},
        NOTE_SOURCE,
        "writer",
        phase_bindings={"post_note": {"kind": "halo_ticket_create"}},
    )
    assert conv.ok
    assert any("has no body" in n for n in conv.notes)


def test_bare_string_guard_uses_has_value_criteria() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Str guard")
async def str_guard(client, label: str = "") -> dict:
    if label:
        await fast_path(client)
    else:
        await slow_path(client)
    return {}
"""
    conv = convert_workflow({}, src, "str_guard")
    assert conv.ok
    cond = next(s for s in conv.payload["steps"] if s.get("steptype") == 1)
    (crit,) = cond["step_conditions"]
    assert (crit["type"], crit["fieldname"], crit["value_string"]) == (29, "<<label>>", "")
    assert cond["name"] == "if label:"
    # both arms route (met -> fast, notmet -> slow)
    met, notmet = cond["actions"]
    fast = next(s for s in conv.payload["steps"] if s["name"] == "fast_path")
    slow = next(s for s in conv.payload["steps"] if s["name"] == "slow_path")
    assert met["end_step"] == fast["step_id"]
    assert notmet["end_step"] == slow["step_id"]


CHAIN_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Chain")
async def chainer(client) -> dict:
    await setup(client)
    await chain_out(client)
    return {}
"""


def test_chain_binding_emits_aa24_without_edges() -> None:
    conv = convert_workflow(
        {},
        CHAIN_SOURCE,
        "chainer",
        phase_bindings={
            "chain_out": {"kind": "chain_runbook", "target": "11111111-2222-4333-8444-555555555555"}
        },
    )
    assert conv.ok
    steps = conv.payload["steps"]
    chain = next(s for s in steps if s.get("auto_action") == 24)
    assert chain["start_new_runbook_id"] == "11111111-2222-4333-8444-555555555555"
    assert chain["actions"] == []  # no edges - trial-proven (runs2557/2558)
    # chain is LAST (terminate-current): only Success follows
    idx = steps.index(chain)
    assert [s["name"] for s in steps[idx + 1 :]] == ["Success"]
    assert any("chains into" in n or "chain phase" in n for n in conv.notes)


def test_chain_before_trailing_phases_warns() -> None:
    src = CHAIN_SOURCE.replace(
        "    await chain_out(client)\n    return {}",
        "    await chain_out(client)\n    await never_runs(client)\n    return {}",
    )
    conv = convert_workflow(
        {},
        src,
        "chainer",
        phase_bindings={"chain_out": {"kind": "chain_runbook", "target": "g"}},
    )
    assert conv.ok
    assert any("WARNING" in n and "NOT run" in n for n in conv.notes)


TICKET_FILTER_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Filter")
async def filtered(client) -> dict:
    await do_work(client)
    return {}
"""


def test_ticket_guard_prefixes_faults_criteria_with_exit() -> None:
    conv = convert_workflow(
        {},
        TICKET_FILTER_SOURCE,
        "filtered",
        ticket_guards=[
            {
                "field": "reportedby",
                "op": "ne",
                "value": "noreply@voicemail.goto.com",
                "value_type": "string",
            }
        ],
    )
    assert conv.ok
    steps = conv.payload["steps"]
    guard = steps[0]
    # prefix condition, N-Central's faults-table shape
    assert guard["steptype"] == 1 and guard["isstart"] is True
    assert guard["name"] == "if ticket.reportedby != 'noreply@voicemail.goto.com':"
    (crit,) = guard["step_conditions"]
    assert (crit["tablename"], crit["fieldname"], crit["type"]) == (
        "faults",
        "reportedby",
        1,  # Is not equal to
    )
    assert crit["value_string"] == "noreply@voicemail.goto.com"
    met, notmet = guard["actions"]
    # met -> first work phase; notmet EXITS to Success (early-return)
    work = next(s for s in steps if s["name"] == "do_work")
    success = next(s for s in steps if s["name"] == "Success")
    assert met["end_step"] == work["step_id"]
    assert notmet["end_step"] == success["step_id"]
    assert (met["approval_result"], notmet["approval_result"]) == (1, 0)
    assert any("ticket-field guard" in n for n in conv.notes)


HELPER_SOURCE = """
from bifrost import workflow


async def run(client, flag: bool):
    if flag:
        await inspect_path(client)
    else:
        await route_path(client)


@workflow(name="Demo: Helper")
async def caller(client, ticket_id: int) -> dict:
    await setup(client)
    await run(client, flag=True)
    return {}
"""


def test_same_file_helper_inlines_and_folds_true_arm() -> None:
    conv = convert_workflow({}, HELPER_SOURCE, "caller")
    assert conv.ok
    names = [s["name"] for s in conv.payload["steps"]]
    # call-site literal flag=True -> route arm statically gone; `run` is
    # not a hop (its awaits spliced); Success tail
    assert names == ["setup", "inspect_path", "Success"]
    assert any("inlined same-file helper" in n for n in conv.notes)
    assert any("folded" in n and "flag = True" in n for n in conv.notes)


def test_helper_folds_to_else_arm_when_false() -> None:
    conv = convert_workflow({}, HELPER_SOURCE.replace("flag=True", "flag=False"), "caller")
    assert conv.ok
    names = [s["name"] for s in conv.payload["steps"]]
    assert names == ["setup", "route_path", "Success"]


def test_helper_runtime_guard_stays_flattened() -> None:
    src = (
        HELPER_SOURCE.replace(
            "async def run(client, flag: bool):",
            "async def run(client, level: int):",
        )
        .replace("    if flag:", "    if level > 5:")
        .replace("await run(client, flag=True)", "await run(client, level=level)")
        .replace(
            "async def caller(client, ticket_id: int)",
            "async def caller(client, level: int)",
        )
    )
    conv = convert_workflow({}, src, "caller")
    assert conv.ok
    names = [s["name"] for s in conv.payload["steps"]]
    # runtime guard can not fold: both arms extract linearly + noted
    assert names == ["setup", "inspect_path", "route_path", "Success"]
    assert any("RUNTIME conditions" in n and "run" in n for n in conv.notes)
    assert not any("folded" in n for n in conv.notes)


def test_recursive_helper_cycle_safe() -> None:
    src = """
from bifrost import workflow


async def countdown(n: int):
    if n > 0:
        await countdown(n - 1)
    await tick()


@workflow(name="Demo: Rec")
async def rec_caller(client) -> dict:
    await countdown(3)
    return {}
"""
    conv = convert_workflow({}, src, "rec_caller")
    assert conv.ok
    names = [s["name"] for s in conv.payload["steps"]]
    # n=3 folds the guard True; the INNER countdown call is in the helper
    # stack -> hop fallback (no infinite recursion); tick splices
    assert names == ["countdown", "tick", "Success"]
    assert any("folded" in n and "n > 0 = True" in n for n in conv.notes)


def test_unreachable_after_folded_return_is_pruned() -> None:
    src = """
from bifrost import workflow


async def run(client, flag: bool):
    if flag:
        await yes(client)
        return
    await no(client)


@workflow(name="Demo: Unreach")
async def caller(client) -> dict:
    await run(client, flag=True)
    return {}
"""
    conv = convert_workflow({}, src, "caller")
    assert conv.ok
    names = [s["name"] for s in conv.payload["steps"]]
    # `no(client)` sits after the folded arm's return - dead, pruned
    assert names == ["yes", "Success"]
    assert any("unreachable" in n and "run: 1" in n for n in conv.notes)


def test_classify_does_not_descend_into_callee_args() -> None:
    phases = classify_phases  # imported for the guard below
    conv = convert_workflow({}, WORKFLOW_SOURCE, "inspect_voicemail_customer")
    labels = [s["name"] for s in conv.payload["steps"][:-1]]
    assert labels == ["halo_connection", "inspect_ticket"]
    assert phases is not None


# --- extraction ----------------------------------------------------------


def test_extract_http_methods_literal_and_assembled() -> None:
    src = """
async def go(client, base):
    await client.get("/v1/devices")
    await client.post("/v1/devices", json={})
    await client.put(base + "/v1/devices/1")
    await client.get(f"/v1/devices/{dev_id}")
    await client.delete(dyn_url)               # fully dynamic - not extractable
"""
    found = extract_http_methods(src)
    assert [(m["method"], m["path"]) for m in found] == [
        ("GET", "/v1/devices"),
        ("POST", "/v1/devices"),
        ("PUT", "/v1/devices/1"),
        ("GET", "/v1/devices/"),  # literal prefix of the f-string
    ]


def test_sanitize_for_import_matches_ui_transform() -> None:
    doc = {
        "name": "RB",
        "type": 1,
        "steps": [
            {
                "id": 9,
                "fdid": 7,
                "chatprofile_id": "x",
                "auto_action": 6,
                "auto_action_type_guid": "g",
                "actions": [{"id": 3, "chatprofile_id": "x"}],
                "step_conditions": [{"id": 4}],
            }
        ],
    }
    out = sanitize_for_import(doc)
    s = out["steps"][0]
    assert s["id"] is None and s["fdid"] is None and s["chatprofile_id"] is None
    assert s["actions"][0]["id"] is None and s["actions"][0]["chatprofile_id"] is None
    assert s["step_conditions"][0]["id"] is None
    assert s["auto_action_type"] is None
    assert out["_is_new"] is True


MULTI_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Child", description="Child runbook.")
async def child_wf(client, tag: str) -> dict:
    await child_work(client)
    return {"tag": tag}


@workflow(name="Demo: Parent", description="Parent runbook.")
async def parent_wf(client, tag: str) -> dict:
    await prepare(client)
    await child_wf(client, tag=tag)
    return {"done": True}
"""


def test_decorated_callee_becomes_chain_not_inline() -> None:
    """A @workflow callee keeps its own runbook -> aa24 chain by NAME."""
    conv = convert_workflow({}, MULTI_SOURCE, "parent_wf")
    assert conv.ok
    chain_steps = [s for s in conv.payload["steps"] if s.get("auto_action") == 24]
    assert len(chain_steps) == 1
    chain = chain_steps[0]
    # name target: the same --apply resolves it to the created id
    assert chain["start_new_runbook_id"] is None
    assert conv.payload.get("_chains") == {str(chain["step_id"]): "child_wf"}
    assert chain["actions"] == []  # aa24 has no edges (trial-proven)
    # NOT inlined: the callee's phases must not splice into the parent
    names = [s["name"] for s in conv.payload["steps"]]
    assert "child_work" not in names
    assert "prepare" in names
    assert any("chain target(s) by NAME" in n for n in conv.notes)


def test_explicit_chain_guid_binding_wins_without_sidecar() -> None:
    conv = convert_workflow(
        {},
        MULTI_SOURCE,
        "parent_wf",
        phase_bindings={
            "child_wf": {
                "kind": "chain_runbook",
                "target": "11111111-2222-4333-8444-555555555555",
            }
        },
    )
    assert conv.ok
    chain = next(s for s in conv.payload["steps"] if s.get("auto_action") == 24)
    assert chain["start_new_runbook_id"] == "11111111-2222-4333-8444-555555555555"
    assert "_chains" not in conv.payload  # guid needs no same-apply resolution


def test_plain_helper_still_inlines_not_chains() -> None:
    conv = convert_workflow(
        {},
        MULTI_SOURCE.replace('@workflow(name="Demo: Child", description="Child runbook.")\n', ""),
        "parent_wf",
    )
    assert conv.ok
    assert not any(s.get("auto_action") == 24 for s in conv.payload["steps"])
    assert "_chains" not in conv.payload


def test_workflow_function_names() -> None:
    assert workflow_function_names(MULTI_SOURCE) == ["child_wf", "parent_wf"]
    assert workflow_function_names("def plain(x):\n    return x\n") == []
    assert workflow_function_names("def broken(:\n") == []  # unparseable -> []


def test_var_vs_var_guard_flattens_with_evidence_note() -> None:
    """if a == b must NOT become a criterion (value side doesn't substitute)."""
    src = """
from bifrost import workflow


@workflow(name="Demo: Var Guard")
async def compare_vars(a: str, b: str) -> dict:
    if a == b:
        await matched()
    else:
        await different()
    return {}
"""
    conv = convert_workflow({}, src, "compare_vars")
    assert conv.ok
    # honest flatten: no condition step would be created (it would
    # always route notmet - the probe proved <<b>> compares literally)
    assert not any(s.get("step_conditions") for s in conv.payload["steps"])
    assert any("two runbook variables" in n and "var_compare_evidence" in n for n in conv.notes)
    assert any("if a == b:" in n for n in conv.notes)  # names the guard


def test_literal_guard_still_translates_after_var_detection() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Lit Guard")
async def compare_literal(a: str) -> dict:
    if a == "x":
        await matched()
    return {}
"""
    conv = convert_workflow({}, src, "compare_literal")
    assert conv.ok
    conds = [s for s in conv.payload["steps"] if s.get("step_conditions")]
    assert conds  # literal side unaffected by the var-vs-var detector
    assert not any("two runbook variables" in n for n in conv.notes)


MEMBERSHIP_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Membership")
async def region_gate(region: str) -> dict:
    if region in ["eu", "us"]:
        await allowed()
    else:
        await blocked()
    return {}
"""


def test_membership_guard_translates_to_type23() -> None:
    """str param in literal set -> type23 comma-set, both arms wired."""
    conv = convert_workflow({}, MEMBERSHIP_SOURCE, "region_gate")
    assert conv.ok
    conds = [s for s in conv.payload["steps"] if s.get("step_conditions")]
    assert len(conds) == 1
    c = conds[0]["step_conditions"][0]
    assert c["type"] == 23  # Includes (membership_evidence.json)
    assert c["fieldname"] == "<<region>>"
    assert c["value_string"] == "eu,us"  # STRICT comma-set
    names = [s["name"] for s in conv.payload["steps"]]
    assert "allowed" in names and "blocked" in names  # both arms survive
    assert any("set membership" in n for n in conv.notes)


def test_not_in_translates_to_type24() -> None:
    src = MEMBERSHIP_SOURCE.replace('if region in ["eu", "us"]:', 'if region not in ["eu", "us"]:')
    conv = convert_workflow({}, src, "region_gate")
    assert conv.ok
    c = next(s for s in conv.payload["steps"] if s.get("step_conditions"))["step_conditions"][0]
    assert c["type"] == 24  # Does not include (both legs proven)
    assert c["value_string"] == "eu,us"


def test_array_element_membership_flattens_with_evidence_note() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Arr Membership")
async def elem_gate(items: list[str]) -> dict:
    if "x" in items:
        await allowed()
    return {}
"""
    conv = convert_workflow({}, src, "elem_gate")
    assert conv.ok
    assert not any(s.get("step_conditions") for s in conv.payload["steps"])
    assert any("array-element membership" in n and "membership_evidence" in n for n in conv.notes)


def test_param_in_array_param_flattens_with_evidence_note() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Dyn Membership")
async def dyn_gate(items: list[str], region: str) -> dict:
    if region in items:
        await allowed()
    return {}
"""
    conv = convert_workflow({}, src, "dyn_gate")
    assert conv.ok
    assert not any(s.get("step_conditions") for s in conv.payload["steps"])
    assert any("array-element membership" in n for n in conv.notes)


def test_int_set_membership_translates_with_string_value_type() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Int Set")
async def level_gate(level: int) -> dict:
    if level in [1, 2]:
        await allowed()
    else:
        await blocked()
    return {}
"""
    conv = convert_workflow({}, src, "level_gate")
    assert conv.ok
    c = next(s for s in conv.payload["steps"] if s.get("step_conditions"))["step_conditions"][0]
    assert c["type"] == 23
    assert c["fieldname"] == "<<level>>"
    assert c["value_string"] == "1,2"
    assert c["value_type"] == "string"  # the proven row (int accepts it too)


def test_float_set_membership_requires_string_value_type() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Float Set")
async def limit_gate(limit: float) -> dict:
    if limit in [1.5, 2.5]:
        await allowed()
    return {}
"""
    conv = convert_workflow({}, src, "limit_gate")
    assert conv.ok
    c = next(s for s in conv.payload["steps"] if s.get("step_conditions"))["step_conditions"][0]
    assert c["type"] == 23
    assert c["value_string"] == "1.5,2.5"
    # value_type "float" FAILS on the trial (fltA legs) - string is proven
    assert c["value_type"] == "string"


def test_bool_bare_truthiness_translates_to_gt_zero() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Bool")
async def flag_gate(flag: bool) -> dict:
    if flag:
        await allowed()
    else:
        await blocked()
    return {}
"""
    conv = convert_workflow({}, src, "flag_gate")
    assert conv.ok
    c = next(s for s in conv.payload["steps"] if s.get("step_conditions"))["step_conditions"][0]
    assert c["type"] == 5 and c["value_int"] == 0  # the ">0" idiom
    assert c["fieldname"] == "<<flag>>"
    assert c["value_type"] == "int"  # the proven criterion-row spelling
    assert any("truthiness" in n for n in conv.notes)


def test_bool_literal_compare_translates_to_eq_one() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Bool Lit")
async def flag_eq(flag: bool) -> dict:
    if flag == True:  # noqa: E712
        await allowed()
    else:
        await blocked()
    return {}
"""
    conv = convert_workflow({}, src, "flag_eq")
    assert conv.ok
    c = next(s for s in conv.payload["steps"] if s.get("step_conditions"))["step_conditions"][0]
    # both legs proven: flag=1 met / flag=0 notmet on eq value_int1
    # (interpolation_evidence.json) - data_type5's canonical spelling
    assert c["type"] == 0 and c["value_int"] == 1
    assert c["fieldname"] == "<<flag>>"
    assert c["value_type"] == "int"


def test_bool_set_membership_encodes_one_zero() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Bool Set")
async def flag_set(flag: bool) -> dict:
    if flag in [True, False]:
        await allowed()
    else:
        await blocked()
    return {}
"""
    conv = convert_workflow({}, src, "flag_set")
    assert conv.ok
    c = next(s for s in conv.payload["steps"] if s.get("step_conditions"))["step_conditions"][0]
    assert c["type"] == 23
    # canonical1/0 encoding - the "True" spelling fails on the trial
    # (interpolation_evidence.json boolset_capital notmet)
    assert c["value_string"] == "1,0"
    assert c["value_type"] == "string"


def test_int_constant_on_bool_param_stays_flat() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Bool Int")
async def flag_int(flag: bool) -> dict:
    if flag == 1:
        await allowed()
    return {}
"""
    conv = convert_workflow({}, src, "flag_int")
    assert conv.ok
    # int-vs-bool was never probed - honest flatten
    assert not any(s.get("step_conditions") for s in conv.payload["steps"])


def test_bool_default_serializes_to_proven_spelling() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Bool Default")
async def flag_default(flag: bool = True, other: bool = False) -> dict:
    await work()
    return {}
"""
    conv = convert_workflow({}, src, "flag_default")
    assert conv.ok
    values = {v["key"]: v["value"] for v in conv.payload["input_variables"]}
    # "1"/"0" are the only data_type5 spellings the trial accepts
    # ("True" never matched - intbool_guard_evidence.json)
    assert values == {"flag": "1", "other": "0"}


TRY_SOURCE = """
from bifrost import workflow


@workflow(name="Demo: Try")
async def guarded(client) -> dict:
    try:
        await risky(client)
    except Exception:
        await fallback(client)
    await afterwards(client)
    return {}
"""


def test_try_except_routes_failure_edges_and_skips_handler() -> None:
    conv = convert_workflow({}, TRY_SOURCE, "guarded", phase_bindings={"risky": 1})
    assert conv.ok
    steps = {s["name"]: s for s in conv.payload["steps"]}
    risky, fallback, afterwards = steps["risky"], steps["fallback"], steps["afterwards"]
    # success path SKIPS the handler; Unsuccessful routes INTO it
    assert [(a["action_name"], a["end_step"]) for a in risky["actions"]] == [
        ("Successful Response (200 - 299)", afterwards["step_id"]),
        ("Unsuccessful Response", fallback["step_id"]),
    ]
    # handler converges after the try
    assert fallback["actions"][0]["end_step"] == afterwards["step_id"]
    assert any("try/except block(s) mapped" in n for n in conv.notes)


def test_try_except_maps_every_failable_phase() -> None:
    src = TRY_SOURCE.replace(
        "        await risky(client)\n",
        "        await risky_a(client)\n        await risky_b(client)\n",
    )
    conv = convert_workflow({}, src, "guarded", phase_bindings={"risky_a": 1, "risky_b": 2})
    assert conv.ok
    steps = {s["name"]: s for s in conv.payload["steps"]}
    a, b, fallback, after = (
        steps["risky_a"],
        steps["risky_b"],
        steps["fallback"],
        steps["afterwards"],
    )
    # BOTH failable phases route Unsuccessful to the handler...
    assert a["actions"][1]["end_step"] == fallback["step_id"]
    assert b["actions"][1]["end_step"] == fallback["step_id"]
    # ...and the LAST body phase's success edge skips the handler
    assert b["actions"][0]["end_step"] == after["step_id"]
    # the non-last body phase flows into the body normally
    assert a["actions"][0]["end_step"] == b["step_id"]


def test_halo_note_in_try_routes_to_handler() -> None:
    conv = convert_workflow(
        {},
        TRY_SOURCE,
        "guarded",
        phase_bindings={"risky": {"kind": "halo_note", "note": "x"}},
    )
    assert conv.ok
    steps = {s["name"]: s for s in conv.payload["steps"]}
    note, fallback = steps["risky"], steps["fallback"]
    assert note["auto_action"] == 8
    # act18 Unsuccessful -> handler (not the Fail terminal)
    assert note["actions"][1]["end_step"] == fallback["step_id"]
    assert note["actions"][0]["end_step"] == steps["afterwards"]["step_id"]


def test_condition_at_end_of_try_retargets_notmet_after() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Try Guard")
async def guarded_try(client, label: str) -> dict:
    try:
        if label:
            await guarded_work(client)
    except Exception:
        await fallback(client)
    await afterwards(client)
    return {}
"""
    conv = convert_workflow({}, src, "guarded_try")
    assert conv.ok
    steps = {s["name"]: s for s in conv.payload["steps"]}
    cond = next(s for s in conv.payload["steps"] if s.get("step_conditions"))
    work, fallback, after = steps["guarded_work"], steps["fallback"], steps["afterwards"]
    # notmet would land INSIDE the handler - retargeted after the try
    notmet = next(a for a in cond["actions"] if a["action_name"] == "Condition not met")
    assert notmet["end_step"] == after["step_id"]
    # the guarded body's success edge skips the handler too
    assert work["actions"][0]["end_step"] == after["step_id"]
    assert work is not fallback


def test_try_with_finally_stays_flat_with_note() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Try Finally")
async def try_fin(client) -> dict:
    try:
        await risky(client)
    finally:
        await cleanup_hop(client)
    return {}
"""
    conv = convert_workflow({}, src, "try_fin", phase_bindings={"risky": 1})
    assert conv.ok
    names = [s["name"] for s in conv.payload["steps"]]
    assert "risky" in names and "cleanup_hop" in names  # flattened honestly
    assert any("else/finally or multiple handlers" in n for n in conv.notes)
    # no failure-edge routing: Unsuccessful still targets Fail
    risky = next(s for s in conv.payload["steps"] if s["name"] == "risky")
    fail = next(s for s in conv.payload["steps"] if s.get("auto_action") == 1)
    assert risky["actions"][1]["end_step"] == fail["step_id"]


def test_multi_handler_try_stays_flat_with_note() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Multi Handler")
async def multi(client) -> dict:
    try:
        await risky(client)
    except ValueError:
        await handler_a(client)
    except Exception:
        await handler_b(client)
    return {}
"""
    conv = convert_workflow({}, src, "multi", phase_bindings={"risky": 1})
    assert conv.ok
    assert any("else/finally or multiple handlers" in n for n in conv.notes)


def test_typed_handler_maps_with_caveat_note() -> None:
    src = TRY_SOURCE.replace("except Exception:", "except ValueError:")
    conv = convert_workflow({}, src, "guarded", phase_bindings={"risky": 1})
    assert conv.ok
    steps = {s["name"]: s for s in conv.payload["steps"]}
    # still routes (edges carry no exception type) + the caveat is named
    assert steps["risky"]["actions"][1]["end_step"] == steps["fallback"]["step_id"]
    assert any("routes ALL" in n and "ValueError" in n for n in conv.notes)


def test_nested_try_gets_unsupported_note() -> None:
    src = """
from bifrost import workflow


@workflow(name="Demo: Nested Try")
async def nested_try(client, flag: bool) -> dict:
    if flag:
        try:
            await risky(client)
        except Exception:
            await fallback(client)
    await afterwards(client)
    return {}
"""
    conv = convert_workflow({}, src, "nested_try", phase_bindings={"risky": 1})
    assert conv.ok
    assert any("nested inside control flow" in n for n in conv.notes)


def test_build_runbook_steps_direct_calls() -> None:
    """Direct primitive construction: edge fields exactly as templates."""
    steps, sidecar, chains = build_runbook_steps(
        "WB",
        "desc",
        [Phase("sleep", "rest", duration=0), Phase("api", "call", method_id=7)],
    )
    assert sidecar == {}
    assert chains == {}
    hop, api, success, fail = steps
    # canonical edge fields (copied from working trial runbooks)
    assert set(api["actions"][0]) == {
        "action_type",
        "action_id",
        "action_name",
        "start_step",
        "end_step",
        "seq",
        "use_work_hours",
        "approval_result",
        "chat_selection_order",
    }
    # approval_result drives the persisted name (server rewrites on save):
    # 1 = positive edge, 0 = failure/false edge
    assert [a["approval_result"] for a in api["actions"]] == [1, 0]
    assert api["actions"][0]["end_step"] == success["step_id"]
    assert api["actions"][1]["end_step"] == fail["step_id"]
    assert hop["isstart"] is True
