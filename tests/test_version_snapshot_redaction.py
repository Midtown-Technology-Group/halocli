"""Pin the version snapshot's public redaction.

`halo_version_snapshot.json` is committed to a public repository, so the
internal infrastructure identifiers in the /InstanceInfo payload (the instance
dbid and the hosted_group name) must never reach it. A regression here leaks
them silently, which is exactly the kind of thing a test should fail on.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "halo_version_snapshot", REPO / "scripts" / "halo_version_snapshot.py"
)
assert _spec is not None and _spec.loader is not None
snapshot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snapshot)

_FAKE_DBID = "00000000-0000-0000-0000-000000000000"


def test_public_view_redacts_internal_identifiers() -> None:
    info = {
        "app_version": "2.250.48",
        "dbid": _FAKE_DBID,
        "hosted_group": "SOME-Stable8",
        "tenant_id": "example",
    }
    view = snapshot._public_view(info)
    assert view["dbid"] == "[redacted]"
    assert view["hosted_group"] == "[redacted]"
    # build-bearing fields survive redaction
    assert view["app_version"] == "2.250.48"
    # the source payload is not mutated (console output keeps real values)
    assert info["dbid"] == _FAKE_DBID


def test_public_view_tolerates_absent_fields() -> None:
    assert snapshot._public_view({"app_version": "2.236.134"}) == {"app_version": "2.236.134"}
