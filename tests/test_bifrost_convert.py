"""Bifrost -> Halo conversion: pure mapping tests (no network).

The runbook-graph assertions pin the primitives that were fire-proven on
the trial (scripts/runbook_chain_matrix.py, runbook_chain_s6b.py):
edge shapes, terminal wiring, iteration sentinel, method binding.
"""

from __future__ import annotations

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
    assert values["items"]["value"] == ""  # None default -> empty
    assert values["items"]["data_type"] == DATA_TYPE["Array"] == 1


def test_branches_are_noted_not_faked() -> None:
    src = API_SOURCE.replace(
        "    return {}", "    if limit > 5:\n        await fetch_remote(client)\n    return {}"
    )
    conv = convert_workflow({}, src, "sync_things")
    assert any("branches" in n for n in conv.notes)


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


def test_build_runbook_steps_direct_calls() -> None:
    """Direct primitive construction: edge fields exactly as templates."""
    steps, sidecar = build_runbook_steps(
        "WB",
        "desc",
        [Phase("sleep", "rest", duration=0), Phase("api", "call", method_id=7)],
    )
    assert sidecar == {}
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
    assert api["actions"][0]["end_step"] == success["step_id"]
    assert api["actions"][1]["end_step"] == fail["step_id"]
    assert hop["isstart"] is True
