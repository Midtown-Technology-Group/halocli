"""Bifrost -> Halo conversion: integrations/methods/workflows -> Halo documents.

Maps Bifrost artifacts onto the first-class Halo surfaces proven on the trial:

- Bifrost integration (``.bifrost/integrations.yaml`` entry or the
  ``GET /integrations`` API row) -> ``POST /CustomIntegration`` payload
  (round-trip proven 2026-10-05, scripts/integration_create_probe.py).
- An explicit method list -> ``POST /CustomIntegrationMethod`` payloads
  (cascade round-trip proven; ``method`` is an int enum).
- Bifrost workflow (``@workflow`` Python source + optional
  ``.bifrost/workflows.yaml``/API row) -> ``POST /Webhook`` ``type:1``
  runbook document (full CUD + UI-exact import proven
  2026-10-05, scripts/runbook_build_probe.py).

Enum provenance (all decoded from the trial's config SPA, 2026-10-05,
``index-BhEb1Eb1.js``; auth pairs empirically confirmed against live
trial integrations):

- method verbs: ``getIntegrationRequestMethods()`` ->
  {0: GET, 1: POST, 2: PUT, 4: PATCH, 3: DELETE}   (PATCH and DELETE are
  deliberately out of sequence - never assume a natural order)
- authorizationtype: ``np`` freeze -> None:0 APIKey:1 Bearer:2 Basic:3
  OAuth2:4 Certificate:5 SecretKey:6 JWT:7 mTLS:8
- granttype: ``bu`` freeze -> ClientCredentials:0 PasswordCredentials:1
  AuthorizationCode:2 AuthorizationCodeWithPKCE:3
- data types: ``getIntegrationDataTypeValues()`` -> Object:0 Array:1
  string:2 int:3 float:4 bool:5 datetime:6
- runbook_start_type: {0: Halo-only, 1: Halo + public endpoint}
  (the SPA shows the /api/automation/{id} trigger URL iff 1);
  inbound_authentication_type 0 = No Authentication (default).

Fidelity stance (agreed): STRUCTURAL conversion. Python bodies become
named steps + a report of what was seen; semantics, secrets, and auth
enums Bifrost cannot express stay as conversion notes for a human -
the payloads are inert until deliberately triggered and the apply probe
deletes everything it creates.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

# --- enums (provenance above) -------------------------------------------

METHOD_VERB: dict[str, int] = {
    "GET": 0,
    "POST": 1,
    "PUT": 2,
    "DELETE": 3,
    "PATCH": 4,
}
METHOD_VERB_NAME: dict[int, str] = {v: k for k, v in METHOD_VERB.items()}

AUTHORIZATION_TYPE: dict[str, int] = {
    "None": 0,
    "APIKey": 1,
    "Bearer": 2,
    "Basic": 3,
    "OAuth2": 4,
    "Certificate": 5,
    "SecretKey": 6,
    "JWT": 7,
    "mTLS": 8,
}

GRANT_TYPE: dict[str, int] = {
    "ClientCredentials": 0,
    "PasswordCredentials": 1,
    "AuthorizationCode": 2,
    "AuthorizationCodeWithPKCE": 3,
}

DATA_TYPE: dict[str, int] = {
    "Object": 0,
    "Array": 1,
    "string": 2,
    "int": 3,
    "float": 4,
    "bool": 5,
    "datetime": 6,
}

# Bifrost oauth flow name -> Halo granttype (unmapped flows -> note only)
_BIFROST_FLOW_TO_GRANT: dict[str, int] = {
    "client_credentials": GRANT_TYPE["ClientCredentials"],
    "password_credentials": GRANT_TYPE["PasswordCredentials"],
    "password": GRANT_TYPE["PasswordCredentials"],
    "authorization_code": GRANT_TYPE["AuthorizationCode"],
    "authorization_code_pkce": GRANT_TYPE["AuthorizationCodeWithPKCE"],
    "pkce": GRANT_TYPE["AuthorizationCodeWithPKCE"],
}

# Bifrost config key names whose value/description carries a base URL
_BASE_URL_KEYS = ("base_url", "base_uri", "endpoint", "api_url", "api_base_url")
_URL_RE = re.compile(r"https?://[^\s`\"')\],;]+")

# Python annotation -> Halo data_type (name based; best-effort)
_ANNOTATION_TO_DATA_TYPE: dict[str, int] = {
    "int": DATA_TYPE["int"],
    "str": DATA_TYPE["string"],
    "string": DATA_TYPE["string"],
    "bool": DATA_TYPE["bool"],
    "float": DATA_TYPE["float"],
    "dict": DATA_TYPE["Object"],
    "list": DATA_TYPE["Array"],
    "datetime": DATA_TYPE["datetime"],
}


@dataclass
class Conversion:
    """A Halo-ready payload plus everything a human must still decide."""

    payload: dict | None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.payload is not None


# --- integration ---------------------------------------------------------


def convert_integration(entry: dict) -> Conversion:
    """Bifrost integration entry -> POST /CustomIntegration payload.

    Accepts a ``.bifrost/integrations.yaml`` entry or a ``GET
    /integrations`` row (both carry id/name/config_schema; YAML carries
    the full oauth_provider, the API row only ``has_oauth_config``).
    """
    notes: list[str] = []
    name = str(entry.get("name") or "").strip()
    if not name:
        return Conversion(None, ["integration has no name"])

    payload: dict = {"name": name}

    oauth = entry.get("oauth_provider") or {}
    if oauth:
        payload["authorizationtype"] = AUTHORIZATION_TYPE["OAuth2"]
        flow = str(oauth.get("oauth_flow_type") or "authorization_code").lower()
        grant = _BIFROST_FLOW_TO_GRANT.get(flow)
        if grant is None:
            notes.append(
                f"oauth flow {flow!r} has no proven Halo mapping - set Grant Type in the UI "
                f"(0=ClientCredentials 1=Password 2=AuthorizationCode 3=PKCE)"
            )
        else:
            payload["granttype"] = grant
        if oauth.get("authorization_url"):
            payload["authorizationurl"] = oauth["authorization_url"]
        if oauth.get("token_url"):
            payload["tokenurl"] = oauth["token_url"]
        scopes = oauth.get("scopes") or []
        if scopes:
            payload["scope"] = " ".join(str(s) for s in scopes)
        notes.append(
            "oauth client_secret is never exported by Bifrost - register the app and "
            "paste the secret in Halo's integration dialog"
        )
    elif entry.get("has_oauth_config"):
        # API-row shape: oauth exists but details are not in the payload
        payload["authorizationtype"] = AUTHORIZATION_TYPE["OAuth2"]
        notes.append(
            "GET /integrations only reports has_oauth_config - fetch the full row or "
            "the .bifrost/integrations.yaml entry for URLs/scopes/grant flow"
        )
    else:
        payload["authorizationtype"] = AUTHORIZATION_TYPE["None"]

    # config secrets never map to a manifest-level Halo field (they live in
    # header/certificate config or per-method headers) - report them always,
    # regardless of how auth itself mapped
    config_keys = [str(c.get("key") or "") for c in (entry.get("config_schema") or [])]
    secretish = [
        k
        for k in config_keys
        if any(t in k.lower() for t in ("key", "token", "secret", "password"))
    ]
    if secretish:
        notes.append(
            f"config keys {secretish} are header/token secrets with no manifest-level "
            "Halo field - choose APIKey/Bearer (authorizationtype 1/2) and set the "
            "header in Halo, or per-method headers"
        )

    # base URL: literal example from the config description wins; else the
    # {{var}} placeholder style Halo itself uses ({{azureopenai-endpoint}})
    base = _extract_base_url(entry)
    if base:
        payload["resourcebaseurl"] = base

    return Conversion(payload, notes)


def _extract_base_url(entry: dict) -> str | None:
    for c in entry.get("config_schema") or []:
        key = str(c.get("key") or "").lower()
        if key in _BASE_URL_KEYS:
            text = str(c.get("description") or "")
            m = _URL_RE.search(text)
            if m:
                return m.group(0).rstrip(".,")
            notes_url = f"{{{{{c.get('key')}}}}}"
            return notes_url
    return None


# --- methods -------------------------------------------------------------


def convert_methods(methods: list[dict], integration_name: str) -> tuple[list[dict], list[str]]:
    """Explicit method list -> POST /CustomIntegrationMethod payloads.

    Each entry: ``{name, path, method: GET|POST|PUT|DELETE|PATCH|0..4}``
    (verb accepted as name or int; ints validated against the enum).
    """
    notes: list[str] = []
    out: list[dict] = []
    for i, m in enumerate(methods):
        name = str(m.get("name") or "").strip()
        path = str(m.get("path") or "").strip()
        if not name or not path:
            notes.append(f"method #{i}: needs both name and path - skipped")
            continue
        verb = m.get("method", m.get("verb"))
        if isinstance(verb, str) and verb.upper() in METHOD_VERB:
            verb_int = METHOD_VERB[verb.upper()]
        elif isinstance(verb, int) and verb in METHOD_VERB_NAME:
            verb_int = verb
            notes.append(f"method {name!r}: numeric verb {verb} = {METHOD_VERB_NAME[verb]}")
        else:
            notes.append(
                f"method {name!r}: verb {verb!r} unrecognized (GET/POST/PUT/DELETE/PATCH "
                "or 0/1/2/3/4) - defaulted to GET(0)"
            )
            verb_int = METHOD_VERB["GET"]
        out.append(
            {
                "name": name,
                "path": path if path.startswith("/") else "/" + path,
                "method": verb_int,
                "_integration_name": integration_name,
            }
        )
    return out, notes


def extract_http_methods(module_source: str) -> list[dict]:
    """Heuristic: ``client.<verb>("<path>")`` calls in a Bifrost module.

    Best-effort discovery aid - literal strings only; f-strings and
    assembled URLs are reported by the caller as unmapped.
    """
    tree = ast.parse(module_source)
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        verb = func.attr.upper()
        if verb not in METHOD_VERB or not node.args:
            continue
        arg0 = node.args[0]
        if (
            isinstance(arg0, ast.Constant)
            and isinstance(arg0.value, str)
            and arg0.value[:1] in ("/",)
        ):
            key = (verb, arg0.value)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "name": f"{verb.title()} {arg0.value}",
                    "path": arg0.value,
                    "method": verb,
                }
            )
    return found


# --- workflow ------------------------------------------------------------


def _annotation_data_type(node: ast.expr | None) -> tuple[int, str]:
    """AST annotation -> (data_type, display) with |None/Optional unwrap."""
    display = "unknown"
    inner: ast.expr | None = node
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        # int | None -> take the non-None side
        sides = [node.left, node.right]
        names = []
        for s in sides:
            if isinstance(s, ast.Constant) and s.value is None:
                continue
            names.append(s)
        inner = names[0] if len(names) == 1 else node
    if isinstance(inner, ast.Name):
        display = inner.id
        unwrapped = inner.id
        if inner.id.lower() == "optional":
            display = "Optional[?]"
        dt = _ANNOTATION_TO_DATA_TYPE.get(unwrapped.lower())
        if dt is not None:
            return dt, display
    elif isinstance(inner, ast.Subscript):
        base = inner.value
        if isinstance(base, ast.Name) and base.id in ("Optional", "Union"):
            return _annotation_data_type(inner.slice if isinstance(inner.slice, ast.expr) else None)
        display = ast.unparse(inner) if hasattr(ast, "unparse") else "complex"
    return DATA_TYPE["string"], display


def _decorator_kwargs(func: ast.AsyncFunctionDef | ast.FunctionDef) -> dict:
    for dec in func.decorator_list:
        if isinstance(dec, ast.Call):
            fn = dec.func
            if isinstance(fn, ast.Name) and fn.id == "workflow":
                out: dict = {}
                for kw in dec.keywords:
                    if kw.arg is None:
                        continue
                    try:
                        out[kw.arg] = ast.literal_eval(kw.value)
                    except (ValueError, SyntaxError):
                        out[kw.arg] = ast.unparse(kw.value)
                return out
    return {}


def _phase_calls(func: ast.AsyncFunctionDef | ast.FunctionDef) -> list[str]:
    """Top-level await/call names - the v1 'phases' of the runbook."""
    phases: list[str] = []
    for node in ast.walk(func):
        if isinstance(node, (ast.Await,)):
            inner = node.value
            if isinstance(inner, ast.Call):
                fn = inner.func
                if isinstance(fn, ast.Name):
                    label = fn.id
                elif isinstance(fn, ast.Attribute):
                    label = fn.attr
                else:
                    continue
                if label not in phases and label not in ("raise", "ValueError", "RuntimeError"):
                    phases.append(label)
    return phases[:8]  # bounded: a step per phase, max 8


def convert_workflow(
    row: dict,
    source: str | None = None,
    function_name: str | None = None,
) -> Conversion:
    """Bifrost workflow -> POST /Webhook type:1 runbook document.

    ``row``: a ``.bifrost/workflows.yaml`` entry or ``GET /workflows``
    row (name/display_name, description, parameters, timeout...).
    ``source`` + ``function_name``: the Python file and decorated
    function for decorator/signature/phase extraction (optional but
    strongly preferred - the decorator carries the real metadata).
    """
    notes: list[str] = []
    name = ""
    description = str(row.get("description") or "").strip()
    category = str(row.get("category") or "").strip()

    meta: dict = {}
    func: ast.AsyncFunctionDef | ast.FunctionDef | None = None
    if source and function_name:
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return Conversion(None, [f"workflow source does not parse: {exc}"])
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ):
                func = node
                break
        if func is None:
            return Conversion(None, [f"function {function_name!r} not found at module top level"])
        meta = _decorator_kwargs(func)
        # the decorator is the workflow's authored identity - it wins over
        # the row (workflows.yaml mirrors it; API rows can lag a rename)
        name = str(meta.get("name") or "") or str(
            row.get("display_name") or row.get("name") or function_name
        )
        category = str(meta.get("category") or category)
        if not description:
            desc = str(meta.get("description") or "")
            description = desc.split("\n")[0].strip()
    else:
        name = str(row.get("display_name") or row.get("name") or "").strip()

    if not name:
        return Conversion(None, ["workflow has no name (row or @workflow name)"])

    input_variables: list[dict] = []
    phases: list[str] = []
    if func is not None:
        args = func.args
        all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        skip = {"ctx", "self", "cls"}
        for a in all_args:
            if a.arg in skip:
                continue
            dt, display = _annotation_data_type(a.annotation)
            input_variables.append(
                {
                    "id": None,
                    "key": a.arg,
                    "value": "",
                    "data_type": dt,
                    "description": f"from Bifrost signature ({display})",
                }
            )
        phases = _phase_calls(func)

    steps = build_steps(name, description, phases)
    notes.append(
        f"phases detected from Python body: {phases or 'none (no literal await calls at top level)'} - "
        "steps are structural placeholders (steptype/auto_action left to Halo defaults); "
        "the Python logic itself does not transfer"
    )
    if meta.get("effects"):
        notes.append(f"effects recorded, no Halo equivalent: {meta['effects']}")
    if meta.get("enforced_bounds"):
        notes.append(
            f"enforced_bounds recorded, no Halo equivalent (consider infinite_loop_threshold / "
            f"batch settings): {meta['enforced_bounds']}"
        )
    if category:
        notes.append(
            f"category {category!r} -> Halo runbook group (group_id) is a UI lookup (-4); left unset"
        )
    if row.get("timeout_seconds") and not description:
        description = f"Bifrost timeout: {row['timeout_seconds']}s"

    payload: dict = {
        "name": name,
        "type": 1,
        "active": False,  # inert until deliberately activated (house rule)
        "steps": steps,
        "input_variables": input_variables,
    }
    if description:
        # Halo has no runbook description field: the first step's message
        # is the human-readable carrier (documented, honest).
        payload["steps"][0]["message"] = description[:4000]
    return Conversion(payload, notes)


def build_steps(name: str, description: str, phases: list[str]) -> list[dict]:
    """Structural step chain: Start -> one step per phase -> End.

    ``steptype``/``auto_action`` intentionally unset in v1: the enum
    semantics are SPA-only (i18n labels) and the apply probe ladder
    records which minimal shapes the server accepts on create.
    """
    labels = [name, *phases[:8]]  # bounded even when called directly
    steps: list[dict] = []
    total = len(labels)
    for i, label in enumerate(labels, start=1):
        step: dict = {
            "id": None,
            "fdid": None,
            "step_id": i,
            "flow_id": 0,
            "flow_type": 0,
            "name": (label or f"Step {i}")[:200],
            "isstart": i == 1,
            "isend": i == total,
            "islaststep": i == total,
            "stage_number": 0,
            "actions": [],
            "step_conditions": [],
            "runbook_variable_mappings": [],
            "translations": [],
            "allow_all_statuses": True,
            "allowed_statuses": [],
        }
        if i > 1 and description:
            step["message"] = description[:4000]
        steps.append(step)
    return steps


def sanitize_for_import(doc: dict) -> dict:
    """The UI's Import-from-JSON transform (WebhookListParent, verbatim).

    Import order in the apply probe: null step id/fdid/chatprofile_id,
    action and step_condition ids, auto_action_type when
    auto_action==6, then POST {..., _is_new: true}.
    """
    s = dict(doc)
    steps = list(s.get("steps") or [])
    for c in steps:
        c["id"] = None
        c["fdid"] = None
        c["chatprofile_id"] = None
        for u in c.get("actions") or []:
            u["id"] = None
            u["chatprofile_id"] = None
        for u in c.get("step_conditions") or []:
            u["id"] = None
            u["chatprofile_id"] = None
        if c.get("auto_action") == 6 and c.get("auto_action_type_guid"):
            c["auto_action_type"] = None
    s["steps"] = steps
    s["_is_new"] = True
    return s
