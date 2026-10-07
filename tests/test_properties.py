"""Property tests: invariants that hand-picked examples keep missing.

These pin the safety contracts of the pure helpers with hostile/random
inputs: path arguments can never inject URL structure, the overlay never
overwrites upstream prose (and is idempotent), Halo payload normalisation
is idempotent, and create-validation always refuses a concrete id.
"""

from __future__ import annotations

import importlib.util
import json
import string
import sys
from pathlib import Path
from urllib.parse import quote

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from halocli.cli import _build_operation_path
from halocli.resources import HaloResource, ResourceOperation
from halocli.utils import normalize_halo_result, parse_page_result
from halocli.writes import validate_write

REPO_ROOT = Path(__file__).resolve().parents[1]

# No deadline: CI matrix runners vary in speed; the invariants matter, not ms.
PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# Hostile URL material: separators, query/fragment markers, traversal,
# percent-escapes, whitespace - but no lone surrogates (no JSON round-trip)
# and no empty strings (an empty path argument is an arity/usage question,
# not an injection one).
HOSTILE = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=40,
)


@given(st.lists(HOSTILE, min_size=1, max_size=4))
@PROFILE
def test_build_operation_path_never_injects_url_structure(values: list[str]) -> None:
    """Filled arguments can never add path segments or query/fragment text."""
    n = len(values)
    template = "/".join(f"{{arg{i}}}" for i in range(n))
    op = ResourceOperation(
        name="probe",
        method="GET",
        path=f"/Root/{template}",
        args=tuple(f"arg{i}" for i in range(n)),
    )
    result = _build_operation_path(op, list(values))

    # Exactly the template's structure: no argument may introduce a segment
    # (percent-encoding with an empty safe set is what makes this true).
    assert len(result.split("/")) == n + 2, (values, result)
    assert result.startswith("/Root/")
    for value in values:
        assert quote(value, safe="") in result, (value, result)


@given(st.text(alphabet=string.printable, min_size=1, max_size=30))
@PROFILE
def test_build_operation_path_never_leaves_placeholders(value: str) -> None:
    """One filled template: no braces survive, and the value appears encoded."""
    op = ResourceOperation(name="one", method="POST", path="/X/{id}", args=("id",))
    result = _build_operation_path(op, [value])
    assert "{" not in result
    assert "}" not in result
    assert result == "/X/" + quote(value, safe="")


# ----------------------------------------------------------------- overlay


def _vendor_module():
    spec = importlib.util.spec_from_file_location(
        "vendor_halo_spec_props", REPO_ROOT / "scripts" / "vendor_halo_spec.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["vendor_halo_spec_props"] = module
    spec.loader.exec_module(module)
    return module


_VENDOR = None


def vendor():
    global _VENDOR
    if _VENDOR is None:
        _VENDOR = _vendor_module()
    return _VENDOR


actions_strategy = st.lists(
    st.tuples(
        st.sampled_from(["/A", "/B", "/C"]),
        st.sampled_from(["get", "post"]),
        st.sampled_from(["summary", "description"]),
        # non-space, non-empty: the vendor rejects strip()-empty updates
        # (str.strip() also treats Cc/Zs whitespace such as \x85 as blank),
        # and empty-target filling is a separate edge from this invariant
        st.text(
            alphabet=st.characters(
                min_codepoint=33,
                blacklist_categories=("Cc", "Cs", "Zs", "Zl", "Zp"),
            ),
            min_size=1,
            max_size=40,
        ),
    ),
    max_size=12,
    unique_by=lambda a: a[:3],
)


@given(actions_strategy)
@PROFILE
def test_overlay_fill_if_missing_never_overwrites_and_is_idempotent(
    actions: list[tuple[str, str, str, str]],
) -> None:
    """Pre-existing prose survives; every empty field is filled; 2nd apply is a no-op."""
    doc: dict = {"paths": {}}
    for path in ("/A", "/B", "/C"):
        for method in ("get", "post"):
            doc["paths"].setdefault(path, {})[method] = {}
    # upstream prose on two fields (never targeted by our action list below)
    doc["paths"]["/A"]["get"]["summary"] = "UPSTREAM SUMMARY"
    doc["paths"]["/B"]["post"]["description"] = "UPSTREAM DESC"

    targets = [
        (p, m, f, t)
        for (p, m, f, t) in actions
        if not (p == "/A" and m == "get" and f == "summary")
        and not (p == "/B" and m == "post" and f == "description")
    ]
    overlay = {
        "overlay": "1.0.0",
        "info": {"title": "prop"},
        "actions": [{"target": f"$.paths['{p}'].{m}.{f}", "update": t} for p, m, f, t in targets],
    }

    filled, already = vendor().apply_overlay(doc, overlay)
    # upstream prose untouched
    assert doc["paths"]["/A"]["get"]["summary"] == "UPSTREAM SUMMARY"
    assert doc["paths"]["/B"]["post"]["description"] == "UPSTREAM DESC"
    assert filled + already == len(targets)
    # every action's target now holds exactly its update text
    for path, method, field, text in targets:
        assert doc["paths"][path][method].get(field) == text, (path, method, field)

    # idempotence: second application fills nothing and changes nothing
    snapshot = json.dumps(doc, sort_keys=True)
    filled2, already2 = vendor().apply_overlay(doc, overlay)
    assert filled2 == 0
    assert filled2 + already2 == len(targets)
    assert json.dumps(doc, sort_keys=True) == snapshot


# ----------------------------------------------------------------- payloads


@given(
    st.recursive(
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(min_value=-(10**9), max_value=10**9),
            st.text(max_size=20),
        ),
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.text(max_size=10), children, max_size=6),
        ),
        max_leaves=15,
    )
)
@PROFILE
def test_normalize_halo_result_is_idempotent(payload) -> None:
    """Normalising twice equals normalising once (safe to chain)."""
    once = normalize_halo_result(payload)
    assert normalize_halo_result(once) == once


@given(
    st.one_of(
        st.none(),
        st.integers(),
        st.text(max_size=20),
        st.floats(allow_nan=False),
    ),
    st.dictionaries(st.text(min_size=1, max_size=8), st.integers(), max_size=5),
)
@PROFILE
def test_create_always_refuses_a_concrete_id(concrete_id, extra: dict) -> None:
    """Halo treats POST-with-id as an update; validation refuses every id type."""
    resource = HaloResource(
        "prop",
        "/Prop",
        create_endpoint="/Prop",
        required_create_fields=("name",),
    )
    payload = dict(extra)
    payload["id"] = concrete_id
    problems = validate_write(resource, payload, update=False)
    if concrete_id is None or concrete_id == "":
        # None/"" count as absent: no id complaint (other problems allowed)
        assert not any("must not include 'id'" in p for p in problems)
    else:
        assert any("must not include 'id'" in p for p in problems), problems


@given(st.dictionaries(st.text(min_size=1, max_size=8), st.integers(min_value=0), max_size=6))
@PROFILE
def test_parse_page_result_invariants(payload: dict) -> None:
    """PageResult shape holds for arbitrary dict payloads."""
    page = parse_page_result(payload)
    assert isinstance(page.items, list)
    assert isinstance(page.record_count, int)
    assert page.record_count >= 0
