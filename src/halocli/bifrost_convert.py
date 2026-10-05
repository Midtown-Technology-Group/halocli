"""Bifrost -> Halo conversion: integrations/methods/workflows -> Halo documents.

Maps Bifrost artifacts onto the first-class Halo surfaces proven on the trial:

- Bifrost integration (``.bifrost/integrations.yaml`` entry or the
  ``GET /integrations`` API row) -> ``POST /CustomIntegration`` payload
  (round-trip proven 2026-10-05, scripts/integration_create_probe.py).
- An explicit method list -> ``POST /CustomIntegrationMethod`` payloads
  (cascade round-trip proven; ``method`` is an int enum).
- Bifrost workflow (``@workflow`` Python source + optional
  ``.bifrost/workflows.yaml``/API row) -> ``POST /Webhook`` ``type:1``
  runbook document with a LIVE-EXECUTABLE step graph.

Enum provenance (decoded from the trial's config SPA and20 working trial
runbooks, 2026-10-05/06; auth pairs empirically confirmed against live
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
- runbook_start_type: {0: Halo-only, 1: Halo + public endpoint};
  inbound_authentication_type 0 = No Authentication.

RUNBOOK STEP PRIMITIVES (every one fire-proven on the trial,
scripts/runbook_chain_matrix.py + runbook_chain_s6b.py, 2026-10-06):

- steps are a directed graph: each working step carries ``actions`` that
  are EDGES ``{action_type, action_id:-action_type, action_name,
  start_step, end_step, seq, use_work_hours, approval_result,
  chat_selection_order}`` - a success edge (seq1) and a failure edge
  (seq2). A chain without edges dies with "Next step not found".
- neutral hop: steptype2, auto_action21 (Sleep), edge action32
  "Sleep Finished" - duration0 completes multi-hop runs (S1/S2/S5:
  steps_executed up to2, status2 completed).
- API call: steptype2, auto_action6, auto_action_type =
  CustomIntegrationMethod id (confirmed: aat32 == method id32 "Trigger
  Automation"), edges action17 "Successful Response (200 - 299)" /
  "Unsuccessful Response".
- ticket action: steptype2, auto_action8, edges action18
  "Successful"/"Unsuccessful" (message carries the ticket payload with
  ``<<var>>`` interpolation).
- condition: steptype1, auto_action6, edges action12 "Condition met" /
  "Condition not met" - with no criteria it evaluates met (S4 completed).
- iteration: auto_action12 "Begin Array Iteration" + auto_action13 "Next
  Iteration"; the ARRAY SOURCE lives in the step ``message`` as
  ``<<var>>`` (proven: input var + message unlocked S6b - completed,
  steps_executed3); edges action22 "Has elements"/"Has no elements" and
  action23 "Iteration finished"/"Next iteration" with end_step -98 as
  Halo's loop-back sentinel (template-proven).
- terminals: steptype3, isend:true, actions [] - auto_action absent =
  Success, auto_action1 = Fail. A failing step without a failure edge
  ends the run with status1.
- condition (branch): steptype1, auto_action6, ``step_conditions`` =
  has-elements criteria (tablename ``runbookvariable``, fieldname
  ``<<var>>``, type5 - Azure Onboarding's GroupSelected? shape), edges
  action12 "Condition met" -> guarded body / "Condition not met" ->
  the else arm (or after the guard when there is none); a then-arm
  ending in a loop skips the else from its iter_end node. BOTH arms
  fire-proven on the trial (met: runlog 2528 status2 3.7s through the
  loop; notmet: runlog 2529 status2 1.6s through the else arm).
  CRITICAL: approval_result drives the persisted action_name (1=
  positive, 0=negative) - the server rewrites names from it on save,
  so every failure/false edge MUST send approval_result:0 or it
  collapses to the positive name and never routes (the same rule
  governs act17/act22/act23 second edges).
- internal triggers: ``triggers=[names]`` becomes a sidecar the apply
  resolves against lookup64 (event catalog; prefer the "- All" scope)
  and binds via POST /Notification {guid: null, eventno, type: -2,
  delivery_method: 6, webhook_id} - NEVER copy a template guid (it
  upserts the template row and hijacks its owner). Trial-proven:
  eventno3 binding + one API-created ticket -> runlog 2532 status2.
  Webhook-create ``events[]`` is a read-joined view (dropped on POST);
  Notification rows are the writable side.

Fidelity stance: the converter emits this executable graph (phases ->
hops or method-bound API-call steps, detected loops -> iteration pairs,
signature -> input variables), and everything that has no Halo field
(effects, enforced_bounds, secrets, branch conditions) lands in
``conversion_report.json`` as notes for a human - never as guesses.
Payloads stay inert (active:false) until an operator or the --apply
probe deliberately fires them.
"""

from __future__ import annotations

import ast
import json
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
    "tuple": DATA_TYPE["Array"],
    "set": DATA_TYPE["Array"],
    "datetime": DATA_TYPE["datetime"],
}


def _default_value(literal: object) -> str:
    """Signature default -> the string Halo stores on the input variable.

    Halo parses list-shaped values as JSON (proven: input value
    ``["a","b","c"]`` unlocked the iteration step on the trial), so
    containers and bools serialize via json; strings stay raw.
    """
    if literal is None:
        return ""
    if isinstance(literal, str):
        return literal
    if isinstance(literal, (bool, int, float, list, tuple, dict)):
        return json.dumps(literal)
    return str(literal)


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
    Optional ``bind_phase`` survives in the body (underscore-free) and is
    consumed by the apply probe to wire runbook steps to created ids.
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
        body: dict = {
            "name": name,
            "path": path if path.startswith("/") else "/" + path,
            "method": verb_int,
            "_integration_name": integration_name,
        }
        if m.get("bind_phase"):
            body["_bind_phase"] = str(m["bind_phase"])
        out.append(body)
    return out, notes


def extract_http_methods(module_source: str) -> list[dict]:
    """Heuristic: literal HTTP calls in a Bifrost module.

    Recognizes ``client.get("/path")`` and assembled forms
    ``client.post(base + "/path")`` / f-strings whose first segment is
    literal (the common Bifrost ``VendorAPI`` style). Fully dynamic URLs
    cannot be extracted - list those methods explicitly via ``--methods``.
    """
    tree = ast.parse(module_source)
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def literal_path(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value if node.value[:1] == "/" else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            # base + "/path" -> the right side literal
            right = literal_path(node.right) if isinstance(node.right, ast.BinOp) else None
            if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                return node.right.value if node.right.value[:1] == "/" else None
            return right
        if isinstance(node, ast.JoinedStr):
            # f"/v1/devices/{id}" -> the literal prefix (up to the first hole)
            parts: list[str] = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    break
            prefix = "".join(parts)
            if prefix[:1] == "/":
                return prefix if len(prefix) > 1 else None
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        verb = func.attr.upper()
        if verb not in METHOD_VERB or not node.args:
            continue
        path = literal_path(node.args[0])
        if not path:
            continue
        key = (verb, path)
        if key in seen:
            continue
        seen.add(key)
        found.append({"name": f"{verb.title()} {path}", "path": path, "method": verb})
    return found


# --- workflow: phases ----------------------------------------------------


@dataclass
class Phase:
    """One classified top-level await of a Bifrost workflow function."""

    kind: str  # "sleep" | "api" | "hop" | "condition"
    label: str
    duration: int | None = None
    method_id: int | None = None
    method_name: str | None = None  # unresolved binding -> sidecar
    in_loop: bool = False
    array_var: str | None = None  # loop target (input variable name)
    # condition phases (else-less `if <array param>:` guards AND
    # if/else where both arms carry awaits):
    branch_span: int | None = None  # guarded (then-arm) raw-phase count
    else_span: int | None = None  # else-arm raw-phase count (None = no else)
    notmet_target: int | None = None  # plan-space step id (build side)


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
        if isinstance(base, ast.Name):
            dt = _ANNOTATION_TO_DATA_TYPE.get(base.id.lower())
            if dt is not None:
                # list[str] -> Array, dict -> Object (container wins)
                return dt, (ast.unparse(inner) if hasattr(ast, "unparse") else "complex")
        display = ast.unparse(inner) if hasattr(ast, "unparse") else "complex"
    return DATA_TYPE["string"], display


def _call_label(call: ast.Call) -> str | None:
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def _is_sleep(call: ast.Call) -> int | None:
    """await asyncio.sleep(N) -> N (the neutral-hop primitive with semantics)."""
    fn = call.func
    if (
        isinstance(fn, ast.Attribute)
        and fn.attr == "sleep"
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "asyncio"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, (int, float))
    ):
        return int(call.args[0].value)
    return None


def classify_phases(
    func: ast.AsyncFunctionDef | ast.FunctionDef,
    phase_bindings: dict[str, int | str] | None = None,
) -> list[Phase]:
    """Top-level awaits in source order, classified into Halo primitives.

    - ``await asyncio.sleep(N)``        -> sleep(N) hop
    - calls bound via ``phase_bindings`` -> API-call step (int id inline,
      str method name -> sidecar for the apply probe to resolve)
    - awaits inside a top-level ``for`` -> loop body (iteration pair
      around them, array var = the iterated name when it is a parameter)
    - an else-less top-level ``if <array param>:`` whose body carries
      awaits -> a Halo condition step (steptype1, criteria on
      ``<<param>>``; translated only for list/tuple-annotated params -
      the shape proven by two working trial runbooks)
    - everything else                   -> neutral hop (sleep0)

    Branches that do not fit (else branches, non-array guards) keep the
    linear flatten + conversion note.
    """
    bindings = phase_bindings or {}
    phases: list[Phase] = []

    # list/tuple-annotated params -> the only guards criteria cover (v1)
    array_params: set[str] = set()
    all_args = [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]
    for a in all_args:
        ann = a.annotation
        while isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            # unwrap list[str] | None
            sides = [ann.left, ann.right]
            non_null = [s for s in sides if not (isinstance(s, ast.Constant) and s.value is None)]
            ann = non_null[0] if len(non_null) == 1 else ann.left
            break
        if isinstance(ann, ast.Subscript) and isinstance(ann.value, ast.Name):
            if ann.value.id in ("list", "tuple", "set"):
                array_params.add(a.arg)
        elif isinstance(ann, ast.Name) and ann.id in ("list", "tuple", "set"):
            array_params.add(a.arg)

    def walk(node: ast.AST, in_loop: bool, array_var: str | None) -> None:
        if isinstance(node, (ast.For, ast.AsyncFor)):
            # the iter expression evaluates once (outside); the target
            # and body run per element (inside)
            loop_var = node.iter.id if isinstance(node.iter, ast.Name) else array_var
            walk(node.iter, in_loop, array_var)
            walk(node.target, True, loop_var)
            for stmt in (*node.body, *node.orelse):
                walk(stmt, True, loop_var)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Await) and isinstance(child.value, ast.Call):
                call = child.value
                label = _call_label(call) or "call"
                seconds = _is_sleep(call)
                bound = bindings.get(label)
                if seconds is not None:
                    phases.append(
                        Phase(
                            "sleep",
                            f"sleep {seconds}s",
                            duration=seconds,
                            in_loop=in_loop,
                            array_var=array_var,
                        )
                    )
                elif bound is not None and label:
                    if isinstance(bound, int):
                        phases.append(
                            Phase(
                                "api", label, method_id=bound, in_loop=in_loop, array_var=array_var
                            )
                        )
                    else:
                        phases.append(
                            Phase(
                                "api",
                                label,
                                method_name=str(bound),
                                in_loop=in_loop,
                                array_var=array_var,
                            )
                        )
                elif label:
                    phases.append(Phase("hop", label, in_loop=in_loop, array_var=array_var))
                # do not descend into the call's own args (nested awaits
                # belong to callee internals, not this workflow's flow)
                continue
            walk(child, in_loop, array_var)

    for stmt in func.body:
        if (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Name)
            and stmt.test.id in array_params
        ):
            # array-param guard: translate whether or not there is an else
            # (both arms must carry awaits to earn steps)
            start = len(phases)
            for sub in stmt.body:
                walk(sub, False, None)
            then_span = len(phases) - start
            else_start = len(phases)
            for sub in stmt.orelse:
                walk(sub, False, None)
            else_span = len(phases) - else_start
            if then_span or else_span:
                phases.insert(
                    start,
                    Phase(
                        "condition",
                        f"if {stmt.test.id}:",
                        array_var=stmt.test.id,
                        branch_span=then_span,
                        else_span=else_span if else_span else None,
                    ),
                )
            continue
        walk(stmt, False, None)
    return phases


# --- workflow: runbook document -----------------------------------------


def _has_elements_criterion(var: str, step_id: int) -> dict:
    """'Has elements' criterion on a runbook variable.

    Shape copied from two working trial runbooks (Azure Onboarding's
    `GroupSelected?` / `LicenseSelected?` steps): tablename
    ``runbookvariable``, fieldname ``<<var>>``, type 5 (has-elements),
    ids/linkage nulled the way the UI import sanitize does.
    """
    return {
        "id": None,
        "rule_id": 0,
        "qualification_criteria_id": 0,
        "fieldname": f"<<{var}>>",
        "value_type": "Array",
        "value_type_id": -1,
        "value_int": 0,
        "value_string": "",
        "partialmatch": False,
        "matchseparatedvalues": False,
        "tablename": "runbookvariable",
        "type": 5,
        "flowsubdetails_criteria_id": 0,
        "use": 0,
        "chatprofile_id": None,
        "chatprofile_flow_seq": step_id,
        "timezonestring": "",
        "match_after_start": False,
        "match_after_target": False,
        "eventrule_id": 0,
        "flow_id": 0,
        "flow_type": 0,
        "flow_seq": 0,
    }


def build_runbook_steps(
    name: str,
    description: str,
    phases: list[Phase],
) -> tuple[list[dict], dict[str, str]]:
    """Executable step graph: phases -> primitives -> wired edges.

    Returns (steps, sidecar) where sidecar maps step_id -> method name
    for bindings whose id is not yet known (the apply probe resolves and
    strips it). Wiring rules (all fire-proven, see module docstring):

    - hop/sleep: one edge (action32 "Sleep Finished") -> next
    - api: two edges (action17 "Successful Response (200 - 299)" -> next,
      "Unsuccessful Response" -> Fail terminal)
    - iteration begin: action22 "Has elements" -> body,
      "Has no elements" -> Success terminal
    - iteration end: action23 "Iteration finished" -> next,
      "Next iteration" -> end_step -98 (Halo's loop-back sentinel)
    - terminals carry no edges: Success (auto_action absent) and
      Fail (auto_action1), both isend.
    """
    sidecar: dict[str, str] = {}

    # flatten loop markers: a loop with body phases becomes
    # [iter_begin, *body, iter_end]; loop-less phases stay as-is.
    # raw_to_plan maps each RAW phase index to the plan node that
    # executes it (a loop start targets its iter_begin), so condition
    # phases can point their "Condition not met" edge at the first plan
    # node AFTER the guarded raw span - resolved after the pass.
    plan: list[tuple[str, Phase | None]] = []
    raw_to_plan: dict[int, int] = {}
    pending_conditions: list[tuple[Phase, int]] = []
    pending_skips: list[tuple[int, int]] = []  # (then-last raw, after-else raw)
    loop_end_pos: dict[int, int] = {}  # last body raw -> its iter_end pos
    raw = 0
    i = 0
    while i < len(phases):
        p = phases[i]
        if p.in_loop:
            body: list[Phase] = []
            loop_var = p.array_var
            while i < len(phases) and phases[i].in_loop:
                body.append(phases[i])
                loop_var = loop_var or phases[i].array_var
                i += 1
            raw_start = raw
            plan.append(("iter_begin", body[0] if body else None))
            begin_pos = len(plan) - 1
            for b in body:
                plan.append(("body", b))
                raw_to_plan.setdefault(raw, len(plan) - 1)
                raw += 1
            plan.append(("iter_end", body[-1] if body else None))
            # targeting the loop's first raw phase lands on iter_begin
            raw_to_plan[raw_start] = begin_pos
            if body:
                # a skip whose then-arm ENDS with this loop must jump from
                # the iter_end node, not from the last body node
                loop_end_pos[raw - 1] = len(plan) - 1
            # share the array var with the marker phases (they render the
            # <<var>> message); body phases ignore array_var when hopping
            for kind, ph in plan:
                if kind in ("iter_begin", "iter_end") and ph is not None and loop_var:
                    ph.array_var = loop_var
            continue
        plan.append(("plain", p))
        if p.kind == "condition":
            pending_conditions.append((p, raw + 1 + (p.branch_span or 0)))
            if p.else_span:
                # the then-arm's last node must skip the else arm entirely
                # (with an empty then-arm the condition node itself skips)
                pending_skips.append(
                    (
                        raw + (p.branch_span or 0),
                        raw + (p.branch_span or 0) + p.else_span + 1,
                    )
                )
        raw_to_plan[raw] = len(plan) - 1
        raw += 1
        i += 1
    total_raw = raw
    for cond, notmet_raw in pending_conditions:
        if notmet_raw >= total_raw:
            cond.notmet_target = None  # falls to the Success terminal
        else:
            pos = raw_to_plan.get(notmet_raw)
            cond.notmet_target = pos + 1 if pos is not None else None
    if not plan:
        # no awaits at all: one neutral start hop so the graph has an entry
        plan.append(("plain", Phase("hop", name)))

    n = len(plan)
    success_id = n + 1
    # the Fail terminal exists only when something can route to it (an api
    # phase's "Unsuccessful Response" edge); pure-hop graphs match the
    # matrix recipes [hop.., Success] exactly
    has_fail = any(
        kind in ("plain", "body") and ph is not None and ph.kind == "api" for kind, ph in plan
    )
    fail_id = success_id + 1 if has_fail else None

    # then-arm skips: the last node of a then-arm that HAS an else must
    # jump over the else arm (a then-arm ending in a loop skips from its
    # iter_end node; an empty then-arm makes the condition itself skip)
    skip_map: dict[int, int] = {}
    for then_last_raw, target_raw in pending_skips:
        pos = loop_end_pos.get(then_last_raw)
        if pos is None:
            pos = raw_to_plan.get(then_last_raw)
        if pos is None:
            continue
        tpos = raw_to_plan.get(target_raw)
        skip_map[pos + 1] = tpos + 1 if tpos is not None else success_id

    def edge(action_type: int, action_name: str, start: int, end: int, seq: int) -> dict:
        # approval_result DRIVES the persisted name: the server rewrites
        # action_name from it on save (SPA pairs them explicitly:
        # approval_result 1="Condition met"/"Successful Response"/"Has
        # elements", 0="not met"/"Unsuccessful"/"Has no elements") -
        # sending 1 on both edges collapsed every second edge to the
        # positive name and broke all failure/false routing (trial-proven
        # 2026-10-06: sent-vs-persisted diffs on act12 AND act17).
        return {
            "action_type": action_type,
            "action_id": -action_type,
            "action_name": action_name,
            "start_step": start,
            "end_step": end,
            "seq": seq,
            "use_work_hours": True,
            "approval_result": 1 if seq == 1 else 0,
            "chat_selection_order": 1,
        }

    # ordered emission (single pass, honest and readable)
    steps: list[dict] = []
    for idx, (kind, ph) in enumerate(plan, start=1):
        is_last_plan = idx == n
        next_id = idx + 1 if not is_last_plan else success_id
        skip = skip_map.get(idx)
        if skip is not None:
            next_id = skip  # then-arm end jumps over the else arm
        if kind == "iter_begin":
            msg = f"<<{ph.array_var}>>" if ph and ph.array_var else None
            steps.append(
                {
                    "step_id": idx,
                    "name": f"Begin iteration: {ph.label if ph else ''}".strip(),
                    "steptype": 2,
                    "auto_action": 12,
                    **({"message": msg} if msg else {}),
                    "isstart": idx == 1,
                    "allow_all_statuses": True,
                    "actions": [
                        edge(22, "Has elements", idx, next_id, 1),
                        edge(22, "Has no elements", idx, success_id, 2),
                    ],
                }
            )
            continue
        if kind == "iter_end":
            # closes the most recent loop: finished -> next after the pair,
            # next -> -98 (loop-back sentinel; template-proven)
            msg = f"<<{ph.array_var}>>" if ph and ph.array_var else None
            steps.append(
                {
                    "step_id": idx,
                    "name": f"Next iteration ({ph.label if ph else 'loop'})",
                    "steptype": 2,
                    "auto_action": 13,
                    **({"message": msg} if msg else {}),
                    "allow_all_statuses": True,
                    "actions": [
                        edge(23, "Iteration finished", idx, next_id, 1),
                        edge(23, "Next iteration", idx, -98, 2),
                    ],
                }
            )
            continue
        assert ph is not None  # kind == "plain" or "body"
        if ph.kind == "condition":
            notmet = ph.notmet_target if ph.notmet_target is not None else success_id
            steps.append(
                {
                    "step_id": idx,
                    "name": ph.label[:200],
                    "steptype": 1,
                    "auto_action": 6,
                    "isstart": idx == 1,
                    "allow_all_statuses": True,
                    "step_conditions": [_has_elements_criterion(ph.array_var or "items", idx)],
                    "actions": [
                        edge(12, "Condition met", idx, next_id, 1),
                        edge(12, "Condition not met", idx, notmet, 2),
                    ],
                }
            )
            continue
        if ph.kind == "api":
            fail_target = fail_id if fail_id is not None else success_id
            step: dict = {
                "step_id": idx,
                "name": ph.label[:200],
                "steptype": 2,
                "auto_action": 6,
                "isstart": idx == 1,
                "allow_all_statuses": True,
                "actions": [
                    edge(17, "Successful Response (200 - 299)", idx, next_id, 1),
                    edge(17, "Unsuccessful Response", idx, fail_target, 2),
                ],
            }
            if ph.method_id is not None:
                step["auto_action_type"] = ph.method_id
            elif ph.method_name is not None:
                sidecar[str(idx)] = ph.method_name
            steps.append(step)
            continue
        # neutral hop (sleep or classified sleep)
        duration = ph.duration if ph.kind == "sleep" else 0
        hop: dict = {
            "step_id": idx,
            "name": ph.label[:200],
            "steptype": 2,
            "auto_action": 21,
            "duration": duration,
            "isstart": idx == 1,
            "allow_all_statuses": True,
            "actions": [edge(32, "Sleep Finished", idx, next_id, 1)],
        }
        steps.append(hop)

    # description rides the first step (Halo has no description field)
    if steps and description:
        steps[0]["message"] = description[:4000]

    steps.append(
        {
            "step_id": success_id,
            "name": "Success",
            "steptype": 3,
            "isend": True,
            "islaststep": True,
            "allow_all_statuses": True,
            "actions": [],
        }
    )
    if fail_id is not None:
        steps.append(
            {
                "step_id": fail_id,
                "name": "Fail",
                "steptype": 3,
                "auto_action": 1,
                "isend": True,
                "allow_all_statuses": True,
                "actions": [],
            }
        )
    return steps, sidecar


def convert_workflow(
    row: dict,
    source: str | None = None,
    function_name: str | None = None,
    phase_bindings: dict[str, int | str] | None = None,
    triggers: list[str] | None = None,
) -> Conversion:
    """Bifrost workflow -> POST /Webhook type:1 runbook document.

    ``row``: a ``.bifrost/workflows.yaml`` entry or ``GET /workflows``
    row (name/display_name, description, parameters, timeout...).
    ``source`` + ``function_name``: the Python file and decorated
    function (preferred - the decorator carries the real metadata).
    ``phase_bindings``: phase name -> CustomIntegrationMethod id (int,
    inline) or method name (str, sidecar for the apply probe).
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
    phases: list[Phase] = []
    if func is not None:
        args = func.args
        all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        defaults = list(args.defaults) + list(args.kw_defaults or [])
        # align defaults to the tail of the positional list
        default_map: dict[str, ast.expr] = {}
        if defaults:
            positional = [a.arg for a in all_args[: len(all_args) - len(args.kwonlyargs)]]
            pos_defaults = list(args.defaults)
            for arg_name, default in zip(
                positional[len(positional) - len(pos_defaults) :], pos_defaults
            ):
                default_map[arg_name] = default
            for a in args.kwonlyargs:
                idx = list(args.kwonlyargs).index(a)
                kwdefaults = list(args.kw_defaults or [])
                if idx < len(kwdefaults):
                    kwdef = kwdefaults[idx]
                    if kwdef is not None:
                        default_map[a.arg] = kwdef

        skip = {"ctx", "self", "cls"}
        for a in all_args:
            if a.arg in skip:
                continue
            dt, display = _annotation_data_type(a.annotation)
            value = ""
            if a.arg in default_map:
                try:
                    value = _default_value(ast.literal_eval(default_map[a.arg]))
                except (ValueError, SyntaxError):
                    value = ""
            if not value and dt == DATA_TYPE["Array"]:
                # Array variables must hold a valid JSON array: an empty
                # string makes has-elements criteria/iteration THROW
                # instead of evaluating false (trial-observed)
                value = "[]"
            input_variables.append(
                {
                    "id": None,
                    "key": a.arg,
                    "value": value,
                    "data_type": dt,
                    "description": f"from Bifrost signature ({display})",
                }
            )
        phases = classify_phases(func, phase_bindings)

    steps, sidecar = build_runbook_steps(name, description, phases)
    loop_phases = [p for p in phases if p.in_loop]
    notes.append(
        f"phases -> {len(phases)} executable step(s): "
        f"{[f'{p.kind}:{p.label}' for p in phases] or 'none - single neutral start'}; "
        "wired with template-proven edges (Sleep/Successful Response/iteration)"
    )
    if loop_phases:
        notes.append(
            f"detected loop over {loop_phases[0].array_var or '?'} -> Halo iteration pair "
            "(aa12/aa13, -98 back-edge); loop multiplicity executes in Halo - "
            "runbook log 'iteration' counter observed on the trial"
        )
    if sidecar:
        notes.append(
            f"steps {sorted(sidecar, key=int)} bind to methods {list(sidecar.values())} - "
            "resolved to created method ids by --apply (sidecar stripped before POST)"
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
            f"category {category!r} -> Halo runbook group via lookup -4 "
            "(--apply matches it live when the tenant defines groups)"
        )
    if func is not None and _has_branches(func):
        translated = sum(1 for p in phases if p.kind == "condition")
        if translated:
            notes.append(
                f"{translated} branch(es) translated to Halo condition steps "
                "(steptype1 + action12 'Condition met/not met', has-elements criteria "
                "on <<array params>>); else-branches and non-array guards stay "
                "flattened to the linear success path"
            )
        else:
            notes.append(
                "Python branches (if/else) are flattened to the linear success path - "
                "Halo conditions exist (steptype1 + action12 'Condition met/not met') "
                "and translate for else-less `if <array param>:` guards; this function's "
                "guards did not fit, so review in the flow editor"
            )

    payload: dict = {
        "name": name,
        "type": 1,
        "active": False,  # inert until deliberately activated (house rule)
        "steps": steps,
        "input_variables": input_variables,
    }
    if sidecar:
        payload["_phase_bindings"] = sidecar  # apply-only; stripped before POST
    if category:
        payload["_category"] = category  # apply resolves group_id via lookup -4
    if triggers:
        payload["_triggers"] = list(triggers)  # apply binds via POST /Notification
        notes.append(
            f"triggers {list(triggers)} -> Halo event bindings via POST /Notification "
            "(resolved against lookup64 at apply; the runbook then runs natively on "
            "those events - trial-proven: eventno3 binding + one API-created ticket "
            "-> runlog status2)"
        )
    return Conversion(payload, notes)


def _has_branches(func: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if isinstance(node, (ast.If, ast.IfExp)):
            return True
    return False


# --- trigger bindings (Bifrost event subscribers -> Halo events) ----------

_EVENT_PLACEHOLDERS = (
    ("$#request", "ticket"),
    ("$#technician", "agent"),
)


def normalize_event_name(name: str) -> str:
    """Normalize a Halo event name for matching (catalog vs bound forms).

    The lookup64 catalog carries i18n placeholders and an "- All" suffix
    ("New $#request Logged - All") while bound notifications carry the
    substituted base ("New Ticket Logged") - lowercase, strip the suffix,
    and substitute the observed placeholders.
    """
    s = str(name).lower().strip()
    if s.endswith(" - all"):
        s = s[: -len(" - all")].strip()
    for ph, word in _EVENT_PLACEHOLDERS:
        s = s.replace(ph.lower(), word)
    return " ".join(s.split())


def match_event(catalog: list[dict], wanted: str) -> dict | None:
    """Resolve an event name against lookup64 rows -> {id, name}.

    Scope matters: several rows share the same value2 base with
    different scopes ("... - All" vs "... - Assigned to Recipient") and
    the working bindings all use the ``- All`` variant (trial: eventno3
    fires for every new ticket). Preference: exact name -> normalized
    match on an ``- All`` row -> any normalized match. None = honest
    miss (the caller records it with the catalog sample).
    """
    target = normalize_event_name(wanted)
    exact = all_match = any_match = None
    for row in catalog:
        name = str(row.get("name") or "")
        values = [name] + ([str(row["value2"])] if row.get("value2") else [])
        is_all = name.lower().endswith(" - all")
        for cand in values:
            if cand.lower().strip() == str(wanted).lower().strip() and exact is None:
                exact = {"id": row.get("id"), "name": cand}
            if normalize_event_name(cand) == target:
                hit = {"id": row.get("id"), "name": cand}
                if is_all and all_match is None:
                    all_match = hit
                if any_match is None:
                    any_match = hit
    return exact or all_match or any_match


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
