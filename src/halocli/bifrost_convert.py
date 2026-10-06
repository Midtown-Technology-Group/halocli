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
- ticket guards (``--ticket-guard "field op value"``) -> PREFIX
  condition steps with criteria on the FAULTS table (N-Central's
  production shape: tablename ``faults``, plain fieldname, operator id)
  and ``notmet_exit``: notmet jumps to Success - the Bifrost early-
  return (no-op complete) semantics. Requires ticket context (the
  event/trigger path; faults criteria cannot evaluate on a bare
  formCollection fire). Trial-proven BOTH legs: full path exec3
  status2 (run2567) vs early exit exec1 status2 (run2570).
- bare guards (all trial-proven): array -> has-elements (type5 +
  value_int0), str -> criteria29 Has a value, ``not str`` ->
  criteria30 Does not have a value, int/float/bool -> criteria5 ">0"
  (int/float runs2589-2592; bool flag inputs "1"/"0" with the
  "True" spelling disproven - intbool_guard_evidence.json; bool
  defaults serialize as "1"/"0"; negative numbers misclassify -
  noted). FIELDNAME
  RULE: runbookvariable criteria MUST wrap as ``<<var>>`` - plain
  names are for the faults table (unwrapped = every leg routes to
  Fail, the first probe attempt's exact failure).
- var-vs-var guards (``if a == b``, two signature params) do NOT
  transfer: the criteria VALUE side compares ``<<b>>`` as literal
  text - trial-proven with a control leg (run2609 control exec3 vs
  mutated met/notmet runs2610/2611 exec1,
  var_compare_evidence.json). Emitting that criterion would invert
  routing (always notmet), so these flatten WITH a precise
  per-guard note naming the evidence - never a guessed criterion.
- membership guards (``if x in ["a","b"]`` / ``not in``) on str, int
  and float params -> type23/type24 (Includes / Does not include)
  with value_string = comma-set, value_type "string" for all three
  (floats FAIL with value_type "float" - intbool_guard_evidence) -
  STRICT set semantics proven by the16-leg membership_evidence.json
  probe (both legs for in and not-in,
  the overlap leg value "xy" + field "x" rules out substring, eq rows
  are AND'd so sets cannot be built from eq rows). Element membership
  on ARRAY params does NOT transfer (type23 on an Array field stays
  notmet both ways - noted per conversion); bool sets and bool
  literal comparisons (``flag == True``) stay unpinned (flat).
- trigger filters (``_trigger_filters`` sidecar from
  ``--trigger-filter``) -> inline conditions on each binding
  (faults table, filter_type2 - AI Triage's production shape): the
  run does NOT START for non-matching events (blocked leg no-run +
  matching leg status2, filter_evidence.json) - the Bifrost
  event.body subscriber guard ported in-Halo.
- chain binding (``{"kind": "chain_runbook", "target": <guid>}``) ->
  aa24 StartNewRunbookTerminateCurrentRunbook + start_new_runbook_id,
  NO outgoing edges (trial-proven, runs2561/2562: the current run
  terminates (-9999) and the TARGET runs to Success with zero edge
  config - the SPA has no default-edge catalog entry for aa24 because
  none is needed). The chain step TERMINATES this run: trailing
  phases get a WARNING note. Target = an existing runbook's guid, or
  a same-file ``@workflow`` callee NAME: awaiting a decorated
  workflow converts to the chain automatically (the callee keeps its
  own runbook identity - inputs, triggers, runlogs) and ``--apply``
  creates targets FIRST (dependency order), patching
  ``start_new_runbook_id`` with the created ids - multi-runbook
  orchestration in one apply. An explicit ``--phase-bindings`` chain
  always wins over the automatic one. Fire-proven as one apply
  (chain_orchestration_evidence.json): child created first, parent
  patched with the child id, parent run2607 status2 step -9999 ->
  chain-started child run2608 status2 (newer than the child's own
  direct-fire run2606); both deleted clean.
- runbook_start_type0 correctly answers401 to a public POST (Halo-only
  enforcement, observed) - fire-able probes need start_type1; runbook
  names are server-UNIQUE (colliding creates400 "Name must be
  unique").

- internal triggers: ``triggers=[names]`` becomes a sidecar the apply
  resolves against lookup64 (event catalog; prefer the "- All" scope)
  and binds via POST /Notification {guid: null, eventno, type: -2,
  delivery_method: 6, webhook_id} - NEVER copy a template guid (it
  upserts the template row and hijacks its owner). Trial-proven:
  eventno3 binding + one API-created ticket -> runlog 2532 status2.
  Webhook-create ``events[]`` is a read-joined view (dropped on POST);
  Notification rows are the writable side.
- halo_note binding (``{"kind": "halo_note", outcome, who, note}``) ->
  Halo API Action step: auto_action8 (label "Halo API Action") +
  auto_action_type3 (SPA message-template catalog:1 create ticket,
  2 update, 3 add note, 4-8 client/site/user CRUD), message = raw Halo
  API body with UNQUOTED ``<<ticket^id>>``, edges act18 Successful /
  Unsuccessful. REQUIRES TICKET CONTEXT: trigger path proven (runlog
  2552 status2 + note read back as an Action row with our marker);
  ticket-less formCollection fires stop at the note step ("Failed
  result reached") - correct behavior, not a defect. Unbound
  halopsa-write effects get a conversion_report suggestion, never a
  guessed write.

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
import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any

# --- enums (provenance above) -------------------------------------------

# Criteria operators - decoded from GET /Languages/1 label pack
# (agentweb_<id> keys; ids from the SPA's getCriteriaTypeText switch,
# labels from the pack,2026-10-06):0 Is equal to,1 Is not equal to,
# 2 Contains,3 Does not contain,5 Greater than,6 Greater than or equal
# to,7 Less than,8 Less than or equal to,29 Has a value,30 Does not
# have a value,-10 To any value. type5 "Greater than" with
# value_int:0 on an Array variable IS Halo's has-elements idiom (the
# shape two working trial runbooks use). type29 "Has a value" proven
# BOTH legs for bare str truthiness on the trial (label "x" -> met
# status2, "" -> notmet to the Fail terminal; runs2563/2564).
# type23/24 = the Includes / Does not include pair (label-pack order
# agentweb756/757) PROVEN as set membership by the16-leg
# membership_evidence.json probe: value_string = comma-set with
# STRICT set semantics (value "xy" + field "x" NOT matched - the
# overlap leg rules out substring), both legs for in (23) and not-in
# (24), multiple eq rows are AND'd (eq rows cannot express a set),
# and type23 on an Array field does NOT match element membership
# (t23_arr_true notmet).
# Other decoded labels from the
# same pack: steptype1=Condition,2=Action,3=End (SPA
# getChatFlowStepTypeDisplay ids5268/5269/455); aa6 label5635="Execute
# an Integration Method" (confirms the method-binding primitive);
# start_type0="Can only be started from Halo" (5651),1="...and from a
# public endpoint" (5652); grant labels5577-5580 confirm the bu-freeze
# mapping; method authorizationtype -1="Inherit from integration
# settings",0="None".
CRITERIA_TYPE: dict[str, int] = {
    "eq": 0,  # Is equal to
    "ne": 1,  # Is not equal to
    "contains": 2,
    "not_contains": 3,
    "gt": 5,  # Greater than (Array + value_int0 = has elements)
    "ge": 6,  # Greater than or equal to
    "lt": 7,  # Less than
    "le": 8,  # Less than or equal to
    "in_set": 23,  # Includes - value side = comma-set (membership, proven)
    "not_in_set": 24,  # Does not include - the `not in` primitive
    "has_value": 29,
    "no_value": 30,
}

# Python compare operator -> criteria key (only exact-semantics maps;
# bare truthiness on non-arrays stays flattened + noted)
_COMPARE_OPS: dict[type, str] = {
    ast.Gt: "gt",
    ast.GtE: "ge",
    ast.Lt: "lt",
    ast.LtE: "le",
    ast.Eq: "eq",
    ast.NotEq: "ne",
}
_COMPARE_SYMBOLS = {
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
    "eq": "==",
    "ne": "!=",
    "in_set": "in",
    "not_in_set": "not in",
}

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
_GUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

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
    containers serialize via json; strings stay raw. bools canonicalize
    to ``1``/``0`` - the ONLY input spellings the trial accepts for
    data_type5 (``"True"`` never matched: intbool_guard_evidence.json
    bool_t_TF notmet; json's ``"true"`` is equally unpinned).
    """
    if literal is None:
        return ""
    if isinstance(literal, bool):
        return "1" if literal else "0"
    if isinstance(literal, str):
        return literal
    if isinstance(literal, (int, float, list, tuple, dict)):
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
    # halo_note phases (bound Halo API Action, aa8/aat3 add-note):
    halo_note: dict | None = None  # {outcome, who, note} payload spec
    # chain phases (aa24 StartNewRunbookTerminateCurrentRunbook):
    chain_target: str | None = None  # target runbook guid
    # condition phases (array truthiness guards AND if/else comparisons):
    branch_span: int | None = None  # guarded (then-arm) raw-phase count
    else_span: int | None = None  # else-arm raw-phase count (None = no else)
    notmet_target: int | None = None  # plan-space step id (build side)
    notmet_exit: bool = False  # notmet jumps straight to Success (early-return semantics)
    criterion_spec: dict | None = None  # typed comparison criteria (None = array has-elements)


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


def _is_workflow_func(func: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """True when the function carries a ``@workflow(...)`` decorator.

    A @workflow callee is its OWN runbook (separate trigger surface,
    inputs, runlogs) - awaiting it is a cross-workflow CALL, not a
    helper splice: it converts to an aa24 chain phase instead.
    """
    for dec in func.decorator_list:
        if isinstance(dec, ast.Call):
            fn = dec.func
            if isinstance(fn, ast.Name) and fn.id == "workflow":
                return True
    return False


def workflow_function_names(source: str) -> list[str]:
    """Names of every top-level ``@workflow`` function in ``source``.

    The CLI uses this to convert a whole multi-workflow file in one
    pass (``--workflow-file`` without ``--function``).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_workflow_func(n)
    ]


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


def _literal_bindings(
    helper: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call
) -> dict[str, Any]:
    """Call-site CONSTANT arguments bound to the helper's parameter names.

    Only ast.Constant values bind (runtime args pass through untouched) -
    this is what makes ``run(x, inspect_only=True)`` fold its True arm.
    """
    binds: dict[str, Any] = {}
    h = helper.args
    pos = [a.arg for a in (*h.posonlyargs, *h.args)]
    for name, node in zip(pos, call.args):
        if isinstance(node, ast.Constant):
            binds[name] = node.value
    for kw in call.keywords:
        if kw.arg and isinstance(kw.value, ast.Constant):
            binds[kw.arg] = kw.value.value  # the VALUE, not the AST node
    return binds


class _LiteralFolder(ast.NodeTransformer):
    """Substitute literal-bound names and statically fold ``if`` guards.

    A guard whose test becomes fully literal evaluates to bool and the
    UNTAKEN arm disappears (constant propagation from the call site);
    runtime guards remain for the linear walk (recorded by the caller).
    """

    def __init__(self, binds: dict[str, Any]) -> None:
        self.binds = binds
        self.folds: list[tuple[str, Any]] = []

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.binds:
            value = self.binds[node.id]
            if isinstance(value, ast.Constant):  # defensive unwrap
                value = value.value
            return ast.copy_location(ast.Constant(value=value), node)
        return node

    def visit_If(self, node: ast.If) -> list[ast.stmt] | ast.If:
        src = ast.unparse(node.test)
        node.test = self.visit(node.test)
        try:
            value = _eval_literal(node.test)
        except (ValueError, SyntaxError):
            # runtime guard: keep (caller records it as flattened)
            self.generic_visit(node)
            return node
        self.folds.append((src, value))
        taken = node.body if bool(value) else node.orelse
        out: list[ast.stmt] = []
        for stmt in taken:
            res = self.visit(stmt)
            if isinstance(res, list):
                out.extend(res)
            else:
                out.append(res)
        return out


_CMP = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b,
}


def _eval_literal(node: ast.expr) -> Any:
    """literal_eval + the guard forms it lacks: single comparisons and ``not``.

    Raises ValueError for anything runtime - the folder keeps those Ifs
    for the honest flatten path.
    """
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        pass
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_literal(node.operand)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        left = _eval_literal(node.left)
        right = _eval_literal(node.comparators[0])
        fn = _CMP.get(type(node.ops[0]))
        if fn is None:
            raise ValueError(f"unsupported comparison {node.ops[0]!r}")
        return fn(left, right)
    raise ValueError(f"not a literal guard: {type(node).__name__}")


_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


def _prune_unreachable(node: Any) -> int:
    """Drop statements that follow a control-flow terminator in the SAME block.

    The constant-fold turns ``if inspect_only: ... return`` into those
    statements directly, leaving the fall-through ``return await route(...)``
    after them - statically dead, so it must not become a phase. Recursive
    over every statement list (body/orelse/finalbody); returns the pruned
    count.
    """
    pruned = 0
    for field_name in ("body", "orelse", "finalbody"):
        stmts = getattr(node, field_name, None)
        if not isinstance(stmts, list) or not stmts:
            continue
        keep: list = []
        terminated = False
        for stmt in stmts:
            if terminated:
                pruned += 1
                continue
            pruned += _prune_unreachable(stmt)
            keep.append(stmt)
            if isinstance(stmt, _TERMINATORS):
                terminated = True
        if len(keep) != len(stmts):
            setattr(node, field_name, keep)
    return pruned


def classify_phases(
    func: ast.AsyncFunctionDef | ast.FunctionDef,
    phase_bindings: dict[str, int | str] | None = None,
    module_defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] | None = None,
    report: dict[str, Any] | None = None,
) -> list[Phase]:
    """Top-level awaits in source order, classified into Halo primitives.

    - ``await asyncio.sleep(N)``        -> sleep(N) hop
    - calls bound via ``phase_bindings`` -> API-call step (int id inline,
      str method name -> sidecar for the apply probe to resolve)
    - awaits inside a top-level ``for`` -> loop body (iteration pair
      around them, array var = the iterated name when it is a parameter)
    - ``await <same-file helper>(...)`` -> the helper's awaits SPLICE
      into this flow (explicit bindings win over inlining); call-site
      CONSTANT arguments fold the helper's ``if`` guards statically -
      untaken arms vanish (e.g. run(x, inspect_only=True) drops the
      route arm); statements after a return/raise that become
      unreachable are PRUNED (the voicemail fall-through case);
      cycle-safe (recursive calls fall back to a hop)
    - an else-less top-level ``if <array param>:`` whose body carries
      awaits -> a Halo condition step (steptype1, criteria on
      ``<<param>>``; translated only for list/tuple-annotated params -
      the shape proven by two working trial runbooks)
    - everything else                   -> neutral hop (sleep0)

    Branches that do not fit (else branches, non-array guards) keep the
    linear flatten + conversion note.
    """
    bindings = phase_bindings or {}
    module_defs = module_defs or {}
    if report is None:
        report = {}
    report.setdefault("inlined", set())
    report.setdefault("folds", [])
    report.setdefault("helper_branches", set())
    report.setdefault("sync_helpers", set())
    helper_stack: set[str] = {func.name}
    phases: list[Phase] = []

    # list/tuple-annotated params -> bare-truthiness guards; all annotated
    # params -> typed comparison guards (int/float/str/bool)
    array_params: set[str] = set()
    param_types: dict[str, str] = {}
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
                param_types[a.arg] = "array"
        elif isinstance(ann, ast.Name):
            if ann.id in ("list", "tuple", "set"):
                array_params.add(a.arg)
                param_types[a.arg] = "array"
            elif ann.id.lower() in ("int", "float", "str", "bool"):
                param_types[a.arg] = ann.id.lower()

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
                    if isinstance(bound, dict) and bound.get("kind") == "chain_runbook":
                        # chain into an existing runbook: aa24 + guid,
                        # NO edges (trial-proven: the step terminates the
                        # current run and starts the target)
                        phases.append(
                            Phase(
                                "chain",
                                label,
                                chain_target=str(bound.get("target") or ""),
                                in_loop=in_loop,
                                array_var=array_var,
                            )
                        )
                    elif isinstance(bound, dict):
                        # explicit action binding: {"kind": "halo_note", ...}
                        # -> Halo API Action aa8/aat3 (add note) step
                        phases.append(
                            Phase(
                                "halo_note",
                                label,
                                halo_note=bound,
                                in_loop=in_loop,
                                array_var=array_var,
                            )
                        )
                    elif isinstance(bound, int):
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
                elif label in module_defs and label not in helper_stack:
                    callee = module_defs[label]
                    if _is_workflow_func(callee) and callee.name != func.name:
                        # cross-workflow CALL: a @workflow callee keeps
                        # its OWN runbook identity (inputs, triggers,
                        # runlogs) - chain phase (aa24) with a NAME
                        # target that the same --apply resolves to the
                        # callee's freshly-created guid (dependency-
                        # ordered create). Explicit bindings above win;
                        # a self-call falls through to the inline path.
                        phases.append(
                            Phase(
                                "chain",
                                label,
                                chain_target=label,
                                in_loop=in_loop,
                                array_var=array_var,
                            )
                        )
                        continue
                    # same-file helper: splice its awaits into this flow
                    # (bindings win over inlining - explicit beats static)
                    helper = module_defs[label]
                    binds = _literal_bindings(helper, call)
                    folder = _LiteralFolder(binds)
                    folded_body: list[ast.stmt] = []
                    for raw_stmt in copy.deepcopy(helper.body):
                        res = folder.visit(raw_stmt)
                        if isinstance(res, list):
                            folded_body.extend(res)
                        else:
                            folded_body.append(res)
                    # post-fold fall-through code after a return/raise is
                    # statically dead (voicemail's `return route_ticket`
                    # after the inspect_only return) - prune it or it
                    # would become a phantom phase
                    wrapped = ast.Module(body=folded_body, type_ignores=[])
                    pruned = _prune_unreachable(wrapped)
                    if pruned:
                        report.setdefault("pruned", {})
                        report["pruned"][label] = report["pruned"].get(label, 0) + pruned
                    helper_stack.add(label)
                    started = len(phases)
                    for stmt in wrapped.body:  # the PRUNED statement list
                        if isinstance(stmt, ast.If):
                            report["helper_branches"].add(label)
                        walk(stmt, in_loop, array_var)
                    helper_stack.remove(label)
                    if folder.folds:
                        report["folds"].extend(
                            f"{label}: {test} = {value!r}" for test, value in folder.folds
                        )
                    if len(phases) > started:
                        report["inlined"].add(label)
                    else:
                        # sync-only helper: the call itself stays the hop
                        phases.append(Phase("hop", label, in_loop=in_loop, array_var=array_var))
                        report["sync_helpers"].add(label)
                    continue
                elif label:
                    phases.append(Phase("hop", label, in_loop=in_loop, array_var=array_var))
                # do not descend into the call's own args (nested awaits
                # belong to callee internals, not this workflow's flow)
                continue
            walk(child, in_loop, array_var)

    def _guard_spec(test: ast.expr) -> dict | None:
        """`<param> <op> <constant>` on int/float/str params -> criteria spec.

        Only exact-semantics maps: one comparison, signature param on the
        left, literal constant on the right, types compatible (int/int,
        float/int, float/float, str/str). Everything else stays flat.
        """
        if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
            return None
        op = _COMPARE_OPS.get(type(test.ops[0]))
        left, right = test.left, test.comparators[0]
        # `param in [..]` / `param not in [..]` -> type23/24 membership
        # (STRICT comma-set proven for str AND int/float params - the
        # membership_evidence.json and intbool_guard_evidence.json
        # probes; floats REQUIRE value_type "string" on the criterion
        # row, ints accept it, so all sets emit "string". bool sets and
        # array-element membership stay flat - the latter proven
        # non-matching; int/float sets were unpinned until v14)
        if isinstance(test.ops[0], (ast.In, ast.NotIn)):
            if not isinstance(left, ast.Name):
                return None
            _elem_pred = {
                "str": lambda v: isinstance(v, str),
                "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
                "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            }
            pred = _elem_pred.get(param_types.get(left.id, "") or "")
            if (
                pred is not None
                and isinstance(right, (ast.List, ast.Tuple))
                and right.elts
                and all(isinstance(e, ast.Constant) and pred(e.value) for e in right.elts)
            ):
                literals = [e.value for e in right.elts if isinstance(e, ast.Constant)]
                return {
                    "param": left.id,
                    "op": "in_set" if isinstance(test.ops[0], ast.In) else "not_in_set",
                    "value": ",".join(str(v) for v in literals),
                    "values": literals,
                    "value_type": "string",  # proven for str/int/float alike
                }
            return None
        if op is None or not isinstance(left, ast.Name):
            return None
        ptype = param_types.get(left.id)
        if ptype in (None, "array", "bool"):
            return None
        if not isinstance(right, ast.Constant) or isinstance(right.value, bool):
            return None
        value = right.value
        if ptype == "int" and not isinstance(value, int):
            return None
        if ptype == "float" and not isinstance(value, (int, float)):
            return None
        if ptype == "str" and not isinstance(value, str):
            return None
        return {"param": left.id, "op": op, "value": value, "value_type": ptype}

    for stmt in func.body:
        guard_label = None
        spec: dict | None = None
        array_var: str | None = None
        if isinstance(stmt, ast.If):
            test = stmt.test
            bare = test if isinstance(test, ast.Name) else None
            negated = False
            operand: ast.Name | None = None
            if (
                isinstance(test, ast.UnaryOp)
                and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name)
            ):
                negated = True
                operand = test.operand
            if bare is not None and bare.id in array_params:
                guard_label = f"if {bare.id}:"
                array_var = bare.id
            elif negated and operand is not None and param_types.get(operand.id) == "str":
                # `if not <str>:` -> criteria30 "Does not have a value"
                # (trial-proven: "" -> met run2589, "x" -> Fail run2590)
                guard_label = f"if not {operand.id}:"
                spec = {
                    "param": operand.id,
                    "op": "no_value",
                    "value": "",
                    "value_type": "str",
                }
            elif bare is not None and param_types.get(bare.id) == "str":
                # bare truthiness on a str param -> criteria29 "Has a
                # value" (trial-proven both legs: "x" -> met/status2,
                # "" -> notmet/Fail terminal)
                guard_label = f"if {bare.id}:"
                spec = {
                    "param": bare.id,
                    "op": "has_value",
                    "value": "",
                    "value_type": "str",
                }
            elif bare is not None and param_types.get(bare.id) in ("int", "float", "bool"):
                # bare numeric/bool truthiness as ">0" (trial-proven
                # both legs: limit=10 -> met run2591, limit=0 -> Fail
                # run2592; bool flag inputs "1" -> met / "0" -> notmet
                # with the "True" spelling disproven -
                # intbool_guard_evidence.json; negative values
                # misclassify - flagged in notes)
                guard_label = f"if {bare.id}:"
                ptype_bare = param_types[bare.id]
                spec = {
                    "param": bare.id,
                    "op": "gt",
                    "value": 0,
                    # bool proven with a value_type "int" criterion row
                    "value_type": "int" if ptype_bare == "bool" else ptype_bare,
                    "truthiness": True,
                }
            else:
                spec = _guard_spec(stmt.test)
                if spec:
                    rhs = spec["values"] if "values" in spec else spec["value"]
                    guard_label = f"if {spec['param']} {_COMPARE_SYMBOLS[spec['op']]} {rhs!r}:"
                elif (
                    isinstance(test, ast.Compare)
                    and len(test.ops) == 1
                    and len(test.comparators) == 1
                    and isinstance(test.left, ast.Name)
                    and isinstance(test.comparators[0], ast.Name)
                    and test.left.id in param_types
                    and test.comparators[0].id in param_types
                    and _COMPARE_OPS.get(type(test.ops[0])) is not None
                ):
                    # var-vs-var: PROVEN non-transferable - the criteria
                    # value side compares <<b>> as the literal text (trial:
                    # control exec3 vs mutated met/notmet exec1,
                    # var_compare_evidence.json). Emitting the criterion
                    # would INVERT routing (always notmet), so this stays
                    # flattened - but the report says exactly why.
                    sym = _COMPARE_SYMBOLS[_COMPARE_OPS[type(test.ops[0])]]
                    report.setdefault("var_guards", []).append(
                        f"if {test.left.id} {sym} {test.comparators[0].id}:"
                    )
                elif (
                    isinstance(test, ast.Compare)
                    and len(test.ops) == 1
                    and isinstance(test.ops[0], (ast.In, ast.NotIn))
                    and isinstance(test.comparators[0], ast.Name)
                    and test.comparators[0].id in array_params
                ):
                    # element membership on an Array param: PROVEN not to
                    # execute (type23 on an Array field stays notmet both
                    # ways - membership_evidence.json t23_arr_true/false)
                    report.setdefault("flat_membership", []).append(
                        f"{ast.unparse(stmt.test)} (array-element membership does "
                        "not execute: membership_evidence.json t23_arr_true notmet)"
                    )
        if guard_label and isinstance(stmt, ast.If):
            # guard with optional else: translate whether or not there is
            # one (both arms must carry awaits to earn steps)
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
                        guard_label,
                        array_var=array_var,
                        criterion_spec=spec,
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


def _compare_criterion(spec: dict, step_id: int) -> dict:
    """Typed comparison criterion (criteria operator table, /Languages/1).

    ``{param, op, value, value_type}`` -> criterion with the operator id
    from CRITERIA_TYPE (eq0/gt5/ge6/lt7/le8/ne1...) and the value in the
    field matching the variable's type (value_int/value_float/
    value_string), same base shape as the has-elements criterion.
    """
    value = spec["value"] if spec.get("values") is None else ""
    if spec.get("values") is not None:
        # membership: the value side is a STRICT comma-set; value_type
        # "string" is the proven row for str/int/float params alike
        # (intbool_guard_evidence.json: floats FAIL with value_type
        # "float", pass with "string"; ints pass with both)
        value = ",".join(str(v) for v in spec["values"])
    vt = spec.get("value_type") or "string"
    # spec forms: {param...} -> <<param>> on runbookvariable, or
    # {table, field...} -> a raw table field (N-Central's faults-style
    # ticket-field criteria)
    fieldname = str(spec["field"]) if spec.get("field") else f"<<{spec.get('param')}>>"
    crit: dict = {
        "id": None,
        "rule_id": 0,
        "qualification_criteria_id": 0,
        "fieldname": fieldname,
        "value_type": {"int": "int", "float": "float", "str": "string"}.get(vt, "string"),
        "value_type_id": -1,
        "value_int": 0,
        "value_string": "",
        "partialmatch": False,
        "matchseparatedvalues": False,
        "tablename": str(spec.get("table") or "runbookvariable"),
        "type": CRITERIA_TYPE[spec["op"]],
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
    if isinstance(value, str):
        crit["value_string"] = value
    elif isinstance(value, float):
        crit["value_float"] = value
        crit["value_int"] = int(value)
    else:
        crit["value_int"] = int(value)
    return crit


def build_runbook_steps(
    name: str,
    description: str,
    phases: list[Phase],
) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    """Executable step graph: phases -> primitives -> wired edges.

    Returns (steps, sidecar, chains): sidecar maps step_id -> method
    name for bindings whose id is not yet known (the apply probe
    resolves and strips it); chains maps step_id -> runbook NAME for
    aa24 targets that are not guids yet (the same-apply create resolves
    them to ids). Wiring rules (all fire-proven, see module docstring):

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
    chains: dict[str, str] = {}

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
        kind in ("plain", "body") and ph is not None and ph.kind in ("api", "halo_note")
        for kind, ph in plan
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
            if ph.notmet_exit:
                notmet = success_id  # early-return semantics: skip the rest
            else:
                notmet = ph.notmet_target if ph.notmet_target is not None else success_id
            steps.append(
                {
                    "step_id": idx,
                    "name": ph.label[:200],
                    "steptype": 1,
                    "auto_action": 6,
                    "isstart": idx == 1,
                    "allow_all_statuses": True,
                    "step_conditions": [
                        _compare_criterion(ph.criterion_spec, idx)
                        if ph.criterion_spec
                        else _has_elements_criterion(ph.array_var or "items", idx)
                    ],
                    "actions": [
                        edge(12, "Condition met", idx, next_id, 1),
                        edge(12, "Condition not met", idx, notmet, 2),
                    ],
                }
            )
            continue
        if ph.kind == "chain":
            # aa24 StartNewRunbookTerminateCurrentRunbook: starts the
            # target and ENDS this run - no outgoing edges (trial-proven:
            # run2557 current completed/-9999, run2558 target ran to
            # Success with zero edge config). The target is either an
            # explicit guid (bindable today) or a runbook NAME that the
            # same --apply resolves after creating the target first
            # (dependency-ordered create).
            target = (ph.chain_target or "").strip()
            is_guid = bool(_GUID_RE.fullmatch(target))
            if target and not is_guid:
                chains[str(idx)] = target
            steps.append(
                {
                    "step_id": idx,
                    "name": ph.label[:200],
                    "steptype": 2,
                    "auto_action": 24,
                    "start_new_runbook_id": target if is_guid else None,
                    "isstart": idx == 1,
                    "allow_all_statuses": True,
                    "actions": [],
                }
            )
            continue
        if ph.kind == "halo_note":
            # Halo API Action (aa8) + auto_action_type3 = add note; the
            # message is a raw Halo API request body (SPA template for
            # aat3), with <<ticket^id>> interpolated UNQUOTED like the
            # working runbooks' payloads. Edges: act18 Successful /
            # Unsuccessful (sem-table pair for aa8).
            spec = ph.halo_note or {}
            note_body = json.dumps(
                {
                    "ticket_id": "<<ticket^id>>",
                    "outcome": spec.get("outcome") or "Internal Note",
                    "who": spec.get("who") or "Automation",
                    "hiddenfromuser": True,
                    "note_html": spec.get("note") or "Converted from Bifrost workflow by halocli",
                },
                indent=2,
            ).replace('"<<ticket^id>>"', "<<ticket^id>>")
            steps.append(
                {
                    "step_id": idx,
                    "name": ph.label[:200],
                    "steptype": 2,
                    "auto_action": 8,
                    "auto_action_type": 3,
                    "isstart": idx == 1,
                    "allow_all_statuses": True,
                    "message": note_body,
                    "actions": [
                        edge(18, "Successful", idx, next_id, 1),
                        edge(18, "Unsuccessful", idx, fail_id or success_id, 2),
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
    return steps, sidecar, chains


def convert_workflow(
    row: dict,
    source: str | None = None,
    function_name: str | None = None,
    phase_bindings: dict[str, int | str] | None = None,
    triggers: list[str] | None = None,
    trigger_filters: list[dict] | None = None,
    ticket_guards: list[dict] | None = None,
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
    module_defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    if source and function_name:
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return Conversion(None, [f"workflow source does not parse: {exc}"])
        module_defs = {
            n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
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
    classify_report: dict = {}
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
        phases = classify_phases(
            func, phase_bindings, module_defs=module_defs, report=classify_report
        )

    if ticket_guards:
        # prefix ticket-field guards (N-Central's faults-style criteria):
        # notmet = early return -> Success (the Bifrost payload-filter
        # semantics: wrong sender/event -> no-op complete, skip writes)
        guard_phases = [
            Phase(
                "condition",
                f"if ticket.{g['field']} "
                f"{_COMPARE_SYMBOLS.get(str(g['op']), str(g['op']))} {g['value']!r}:",
                criterion_spec={
                    "field": str(g["field"]),
                    "table": "faults",
                    "op": str(g["op"]),
                    "value": g["value"],
                    "value_type": str(g.get("value_type") or "string"),
                },
                notmet_exit=True,
            )
            for g in ticket_guards
        ]
        phases = guard_phases + phases
        notes.append(
            f"{len(guard_phases)} ticket-field guard(s) prepended (criteria on the "
            "faults table, N-Central's production shape): notmet exits to Success "
            "(early-return semantics - the Bifrost payload-filter pattern)"
        )

    steps, sidecar, chains = build_runbook_steps(name, description, phases)
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
    chain_phases = [i for i, p in enumerate(phases) if p.kind == "chain"]
    if classify_report.get("inlined"):
        notes.append(
            f"inlined same-file helper(s): {sorted(classify_report['inlined'])} - "
            "their awaits splice into this flow (bind phases by the inner call labels)"
        )
    if classify_report.get("folds"):
        notes.append(
            f"folded {len(classify_report['folds'])} call-site literal guard(s) "
            f"({'; '.join(classify_report['folds'])}) - untaken arms skipped statically "
            "(constant propagation from the call site)"
        )
    if classify_report.get("helper_branches"):
        notes.append(
            f"helper branch(es) with RUNTIME conditions in "
            f"{sorted(classify_report['helper_branches'])} stay flattened to linear "
            "phases - review in the flow editor"
        )
    if classify_report.get("var_guards"):
        vg = classify_report["var_guards"]
        notes.append(
            f"{len(vg)} guard(s) compare two runbook variables ({', '.join(vg)}) - "
            "Halo criteria do NOT substitute <<var>> on the value side "
            "(trial-proven: var_compare_evidence.json - control ran 3 steps, both "
            "mutated legs ran 1, i.e. '<<b>>' compared as literal text), so "
            "emitting the criterion would invert routing - flattened to the linear "
            "success path; review in the flow editor"
        )
    if classify_report.get("flat_membership"):
        fm = classify_report["flat_membership"]
        notes.append(
            f"{len(fm)} membership guard(s) flattened ({'; '.join(fm)}) - element "
            "membership on Array variables does not execute in Halo "
            "(trial-proven both legs: membership_evidence.json) - flattened to "
            "the linear success path; review in the flow editor"
        )
    if classify_report.get("sync_helpers"):
        notes.append(
            f"sync-only helper(s) kept as hops: {sorted(classify_report['sync_helpers'])} "
            "(no awaits to splice)"
        )
    if classify_report.get("pruned"):
        pruned = classify_report["pruned"]
        total = sum(pruned.values())
        notes.append(
            f"dropped {total} statically-unreachable statement(s) after "
            f"return/raise ({', '.join(f'{k}: {v}' for k, v in pruned.items())}) - "
            "dead code after a constant-folded early return (never executes)"
        )
    if chain_phases:
        notes.append(
            "chain phase -> aa24 StartNewRunbookTerminateCurrentRunbook "
            f"(target {phases[chain_phases[0]].chain_target}): starts the target runbook "
            "and TERMINATES this run (trial-proven: run2557 current + run2558 target, "
            "zero edge config); steps after a chain phase never execute"
        )
        if chain_phases[0] != len(phases) - 1:
            notes.append(
                f"WARNING: {len(phases) - 1 - chain_phases[0]} phase(s) follow the chain "
                "phase and will NOT run - move the chain last or drop the trailing phases"
            )
    if sidecar:
        notes.append(
            f"steps {sorted(sidecar, key=int)} bind to methods {list(sidecar.values())} - "
            "resolved to created method ids by --apply (sidecar stripped before POST)"
        )
    if chains:
        notes.append(
            f"chain target(s) by NAME {sorted(set(chains.values()))} -> the same --apply "
            "creates the target runbook FIRST (dependency order) and patches this step's "
            "start_new_runbook_id with its id; a lone --apply of this file leaves the "
            "target unbound (chain fires nothing) - bind an explicit guid via "
            "--phase-bindings to chain into a pre-existing runbook instead"
        )
    if meta.get("effects"):
        notes.append(f"effects recorded, no Halo equivalent: {meta['effects']}")
        effects = meta.get("effects") or []
        writes_halo = any(
            isinstance(e, dict)
            and e.get("kind") == "integration.write"
            and str(e.get("target", "")).lower() == "halopsa"
            for e in effects
        )
        if writes_halo and not any(p.kind == "halo_note" for p in phases):
            notes.append(
                "this workflow writes to Halo but no phase is bound to a halo_note "
                'action - bind one via --phase-bindings {"<phase>": {"kind": '
                '"halo_note", "outcome": "...", "note": "..."}} to emit a '
                "Halo API Action (aa8/aat3 add-note) step in ticket context"
            )
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
                "(steptype1 + action12 'Condition met/not met'; criteria on "
                "<<params>>: array has-elements, str has/no-value, numeric/bool >0, "
                "literal comparisons, set membership (str/int/float, in/not in)); "
                "else-branches, bool literal comparisons and "
                "comparisons against other names stay flattened to the linear "
                "success path"
            )
        else:
            notes.append(
                "Python branches (if/else) are flattened to the linear success path - "
                "Halo conditions exist (steptype1 + action12 'Condition met/not met') "
                "and translate for array/str/numeric guards with literal comparisons; "
                "this function's guards did not fit, so review in the flow editor"
            )
    if any(
        p.kind == "condition" and p.criterion_spec and p.criterion_spec.get("truthiness")
        for p in phases
    ):
        notes.append(
            "bare-truthiness (numeric/bool) mapped to '>0' (criteria5, trial-proven "
            "limit=10/limit=0 legs + bool flag1/flag0 legs): NEGATIVE values "
            "misclassify - use an explicit comparison if the value can go below zero"
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
    if chains:
        payload["_chains"] = chains  # step_id -> runbook name; apply resolves to ids
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
    if trigger_filters:
        payload["_trigger_filters"] = list(trigger_filters)  # binding conditions
        notes.append(
            f"trigger filters {list(trigger_filters)} -> conditions on each binding "
            "(faults table, filter_type 2) - THE Bifrost subscriber-filter port: the "
            "run does NOT START for a non-matching event (trial-proven: blocked leg "
            "no-run + matching leg run status2)"
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
