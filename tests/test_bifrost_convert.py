"""Bifrost -> Halo conversion: pure mapping tests (no network)."""

from __future__ import annotations

from halocli.bifrost_convert import (
    AUTHORIZATION_TYPE,
    DATA_TYPE,
    GRANT_TYPE,
    METHOD_VERB,
    build_steps,
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
    # phases -> step chain: start + one per awaited call + end markers
    names = [s["name"] for s in p["steps"]]
    assert names[0] == p["name"]
    assert p["steps"][0]["isstart"] is True
    assert p["steps"][-1]["isend"] is True and p["steps"][-1]["islaststep"] is True
    assert "halo_connection" in names and "inspect_ticket" in names
    # description rides the first step's message (Halo has no description field)
    assert "match its caller" in p["steps"][0]["message"]
    # structurally faithful but inert: no actions carry side effects
    assert all(s["actions"] == [] for s in p["steps"])
    # everything that did not transfer is a note
    assert any("effects" in n for n in conv.notes)
    assert any("enforced_bounds" in n for n in conv.notes)
    assert any("HaloPSA" in n for n in conv.notes)


def test_workflow_row_only_conversion() -> None:
    # API row without source: name/description/timeout from the row alone
    conv = convert_workflow(
        {"name": "create_customer", "description": "Create the customer", "timeout_seconds": 1800}
    )
    assert conv.ok
    assert conv.payload["name"] == "create_customer"
    assert conv.payload["input_variables"] == []
    assert conv.payload["steps"][0]["isstart"] is True


def test_workflow_missing_function_fails_cleanly() -> None:
    conv = convert_workflow({}, WORKFLOW_SOURCE, "nope")
    assert not conv.ok
    assert any("not found" in n for n in conv.notes)


def test_extract_http_methods_literal_calls_only() -> None:
    src = """
async def go(client):
    await client.get("/v1/devices")
    await client.post("/v1/devices", json={})
    await client.delete(f"/v1/devices/{dev_id}")   # assembled - not extractable
"""
    found = extract_http_methods(src)
    assert [(m["method"], m["path"]) for m in found] == [
        ("GET", "/v1/devices"),
        ("POST", "/v1/devices"),
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


def test_build_steps_bounds_phases() -> None:
    steps = build_steps("WB", "desc", [f"call{i}" for i in range(20)])
    assert len(steps) <= 9  # name + at most 8 phases
    assert steps[0]["isstart"] is True
