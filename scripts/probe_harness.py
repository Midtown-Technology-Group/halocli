"""Shared harness for the runbook probe scripts (trial-only, self-cleaning).

Holds the pieces every probe repeats so cross-file duplication stays
out of the quality gate: the repo/src bootstrap, the halocli imports
the probes need, the CLI-module loader (fire/_gone helpers), and the
create-side cleanup that deletes every runbook it made.

Probe scripts import this directly (``python scripts/<probe>.py`` puts
scripts/ on sys.path; tests/conftest.py adds it for the test loaders).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from halocli.bifrost_convert import convert_workflow  # noqa: E402,F401
from halocli.client import HaloClient  # noqa: E402,F401
from halocli.config import load_profile  # noqa: E402,F401

_spec = importlib.util.spec_from_file_location("bc_cli", REPO / "scripts" / "bifrost_convert.py")
assert _spec is not None and _spec.loader is not None
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


async def cleanup_runbooks(client: Any, created: list[str], ev: dict) -> None:
    """DELETE every runbook the probe created and record verification."""
    for wid in created:
        try:
            await client.request("DELETE", f"/Webhook/{wid}", timeout=30)
        except Exception as exc:  # noqa: BLE001
            ev.setdefault("cleanup_errors", []).append(f"{wid}: {str(exc)[:150]}")
    ev["cleanup"] = [await bc._gone(client, "DELETE check", f"/Webhook/{w}") for w in created]
