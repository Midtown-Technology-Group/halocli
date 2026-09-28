"""Tests for the generated subcommands of nested ``ResourceOperation`` entries."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from typer.main import get_command
from typer.testing import CliRunner

from halocli.cli import app
from halocli.config import HaloProfile, save_profile
from halocli.resources import RESOURCE_BY_COMMAND

runner = CliRunner()

# CI (and FORCE_COLOR runs) render click's rich errors with ANSI styling, which
# interleaves escape codes inside option names ("--apply --yes"). Strip them
# before substring assertions so tests do not depend on the color environment.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# b"%PDF-fake" — the canned binary body several tests assert on.
PDF_BYTES = b"%PDF-fake"


def plain(output: str) -> str:
    return _ANSI_RE.sub("", output)


def save_default_profile() -> None:
    save_profile(
        "default",
        HaloProfile(
            tenant_url="https://x.halopsa.com",
            client_id="id",
            client_secret="secret",
        ),
    )


def fake_client(response: Any) -> tuple[Any, dict[str, Any]]:
    """Build a stand-in for ``halocli.cli.HaloClient`` that records its calls.

    Returns the class (monkeypatch it onto ``halocli.cli.HaloClient``) and a
    ``state`` dict with ``calls`` (one entry per ``request``) and
    ``constructions`` (how often the client was instantiated).
    """
    state: dict[str, Any] = {"calls": [], "constructions": 0}

    class FakeHaloClient:
        def __init__(self, profile: Any, *, profile_name: str = "default", **kwargs: Any) -> None:
            state["constructions"] += 1
            self.profile = profile
            self.profile_name = profile_name

        async def __aenter__(self) -> "FakeHaloClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def request(self, method: str, path: str, **kwargs: Any) -> Any:
            state["calls"].append({"method": method, "path": path, **kwargs})
            return response

    return FakeHaloClient, state


class ExplodingHaloClient:
    """Fails the test if a code path constructs a Halo client at all."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("this code path must not construct a HaloClient")


def test_tickets_help_lists_all_operations() -> None:
    result = runner.invoke(app, ["tickets", "--help"])

    assert result.exit_code == 0
    for name in (
        "create-object",
        "set-billable-project",
        "view",
        "process-children",
        "salesmailbox",
        "vote",
        "zapier",
    ):
        assert name in result.output


def test_invoices_help_lists_operations() -> None:
    result = runner.invoke(app, ["invoices", "--help"])

    assert result.exit_code == 0
    for name in ("pdf", "lines", "void"):
        assert name in result.output


def test_attachments_help_lists_operations() -> None:
    result = runner.invoke(app, ["attachments", "--help"])

    assert result.exit_code == 0
    for name in ("upload-image", "get-document", "delete-image", "list-images"):
        assert name in result.output


def test_registered_help_matches_contract_format() -> None:
    # The raw ``help=`` string is contract-frozen: "METHOD PATH — summary [verification]".
    # (Rich parses help as markup when rendering, which is a display concern; the
    # registered value must still be the exact one-line string.)
    click_app = get_command(app)
    checked = 0
    for resource_name in ("tickets", "invoices", "attachments"):
        group = click_app.commands[resource_name]
        for op in RESOURCE_BY_COMMAND[resource_name].operations:
            assert (
                group.commands[op.name].help
                == f"{op.method} {op.path} — {op.summary} [{op.verification}]"
            )
            checked += 1
    assert checked == 22


def test_operation_help_shows_method_and_summary() -> None:
    result = runner.invoke(app, ["tickets", "zapier", "--help"])

    assert result.exit_code == 0
    help_text = plain(result.output)
    assert "GET /Tickets/zapier" in help_text
    assert "Zapier integration config" in help_text


def test_pdf_rejects_wrong_arity() -> None:
    missing = runner.invoke(app, ["invoices", "pdf"])
    assert missing.exit_code == 2
    assert "pdf expects 1 path argument(s): id" in plain(missing.output)

    extra = runner.invoke(app, ["invoices", "pdf", "1", "2"])
    assert extra.exit_code == 2
    assert "pdf expects 1 path argument(s): id" in plain(extra.output)


def test_write_preview_is_profile_free_and_zero_network(monkeypatch: Any) -> None:
    # No profile exists in the isolated config, and the client class explodes if
    # anything constructs it: a successful preview proves neither is touched.
    monkeypatch.setattr("halocli.cli.HaloClient", ExplodingHaloClient)

    result = runner.invoke(app, ["invoices", "pdf", "42"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["apply"] is False
    assert payload["resource"] == "invoices"
    assert payload["operation"] == "pdf"
    assert payload["method"] == "POST"
    assert payload["endpoint"] == "/Invoice/PDF/42"
    assert payload["params"] == {}
    assert payload["body"] is None
    assert payload["file"] is None
    assert payload["verification"]


def test_write_gating_requires_both_apply_and_yes() -> None:
    preview = runner.invoke(app, ["tickets", "vote", "--data", "{}"])
    assert preview.exit_code == 0, preview.output
    payload = json.loads(preview.output)
    assert payload["apply"] is False
    assert payload["operation"] == "vote"
    assert payload["body"] == {}

    for flags in (["--apply"], ["--yes"]):
        result = runner.invoke(app, ["tickets", "vote", "--data", "{}", *flags])
        assert result.exit_code != 0
        assert "--apply --yes" in plain(result.output)


def test_body_operation_requires_data() -> None:
    result = runner.invoke(app, ["tickets", "vote"])

    assert result.exit_code != 0
    assert "--data" in plain(result.output)


def test_write_execute_sends_json_body(monkeypatch: Any) -> None:
    save_default_profile()
    FakeClient, state = fake_client({"ok": True})
    monkeypatch.setattr("halocli.cli.HaloClient", FakeClient)

    result = runner.invoke(
        app,
        ["tickets", "create-object", "--data", json.dumps({"summary": "x"}), "--apply", "--yes"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is True
    assert payload["result"] == {"ok": True}
    assert state["calls"][0]["method"] == "POST"
    assert state["calls"][0]["path"] == "/Tickets/Object"
    assert state["calls"][0]["json_body"] == {"summary": "x"}


def test_write_execute_accepts_array_bodies(monkeypatch: Any) -> None:
    # Halo takes array bodies on many endpoints; operations must not force a dict.
    save_default_profile()
    FakeClient, state = fake_client({"ok": True})
    monkeypatch.setattr("halocli.cli.HaloClient", FakeClient)

    result = runner.invoke(
        app,
        ["tickets", "create-object", "--data", json.dumps([{"summary": "x"}]), "--apply", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert state["calls"][0]["json_body"] == [{"summary": "x"}]


def test_invalid_data_json_is_a_validation_error() -> None:
    result = runner.invoke(app, ["tickets", "vote", "--data", "{not json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["category"] == "validation"


def test_path_arguments_are_url_encoded() -> None:
    result = runner.invoke(app, ["invoices", "pdf", "a/b c"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["endpoint"] == "/Invoice/PDF/a%2Fb%20c"


def test_get_operation_executes_through_client(monkeypatch: Any) -> None:
    save_default_profile()
    FakeClient, state = fake_client({"zapier_enabled": True})
    monkeypatch.setattr("halocli.cli.HaloClient", FakeClient)

    result = runner.invoke(app, ["tickets", "zapier"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["operation"] == "zapier"
    assert payload["method"] == "GET"
    assert payload["endpoint"] == "/Tickets/zapier"
    assert payload["result"] == {"zapier_enabled": True}
    assert state["constructions"] == 1
    assert len(state["calls"]) == 1
    assert state["calls"][0]["method"] == "GET"
    assert state["calls"][0]["path"] == "/Tickets/zapier"


def test_param_is_passed_through_as_query_string(monkeypatch: Any) -> None:
    save_default_profile()
    FakeClient, state = fake_client({"items": []})
    monkeypatch.setattr("halocli.cli.HaloClient", FakeClient)

    result = runner.invoke(app, ["invoices", "lines", "--param", "id=7"])

    assert result.exit_code == 0, result.output
    assert state["calls"][0]["params"] == {"id": "7"}


def test_multipart_upload_sends_file_bytes(monkeypatch: Any, tmp_path: Path) -> None:
    save_default_profile()
    source = tmp_path / "pixel.png"
    source.write_bytes(b"PNGDATA\x00")
    FakeClient, state = fake_client({"ok": True})
    monkeypatch.setattr("halocli.cli.HaloClient", FakeClient)

    result = runner.invoke(
        app,
        ["attachments", "upload-image", "--file", str(source), "--apply", "--yes"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is True
    assert payload["operation"] == "upload-image"
    assert state["constructions"] == 1
    assert len(state["calls"]) == 1
    call = state["calls"][0]
    assert call["method"] == "POST"
    assert call["path"] == "/Attachment/image"
    assert "file" in call["files"]
    filename, content, *rest = call["files"]["file"]
    assert filename == "pixel.png"
    assert content == b"PNGDATA\x00"
    if rest:
        assert rest[0] == "image/png"


def test_multipart_preview_reports_file_without_client(monkeypatch: Any, tmp_path: Path) -> None:
    source = tmp_path / "pixel.png"
    source.write_bytes(b"PNGDATA\x00")
    FakeClient, state = fake_client({"ok": True})
    monkeypatch.setattr("halocli.cli.HaloClient", FakeClient)

    result = runner.invoke(app, ["attachments", "upload-image", "--file", str(source)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["apply"] is False
    assert payload["endpoint"] == "/Attachment/image"
    assert payload["file"] == {"name": "pixel.png", "size": 8}
    assert state["constructions"] == 0
    assert state["calls"] == []


def test_multipart_apply_requires_file() -> None:
    result = runner.invoke(app, ["attachments", "upload-image", "--apply", "--yes"])

    assert result.exit_code != 0
    assert "--file" in plain(result.output)


def test_binary_response_is_saved_to_file(monkeypatch: Any, tmp_path: Path) -> None:
    save_default_profile()
    FakeClient, _state = fake_client(PDF_BYTES)
    monkeypatch.setattr("halocli.cli.HaloClient", FakeClient)
    out = tmp_path / "nested" / "invoice.pdf"

    result = runner.invoke(
        app,
        ["invoices", "pdf", "42", "--apply", "--yes", "--save", str(out)],
    )

    assert result.exit_code == 0, result.output
    assert out.exists()
    assert out.read_bytes() == PDF_BYTES
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["saved"] == str(out)
    assert payload["bytes"] == len(PDF_BYTES)


def test_binary_response_without_save_is_never_dumped(monkeypatch: Any) -> None:
    save_default_profile()
    FakeClient, _state = fake_client(PDF_BYTES)
    monkeypatch.setattr("halocli.cli.HaloClient", FakeClient)

    result = runner.invoke(app, ["invoices", "pdf", "42", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["binary"] is True
    assert payload["bytes"] == len(PDF_BYTES)
    assert "%PDF" not in result.output


def test_save_rejects_a_directory_before_any_request(monkeypatch: Any, tmp_path: Path) -> None:
    """A directory --save must fail as usage BEFORE the request is dispatched.

    Otherwise the write to disk fails after a successful network call and the
    CLI reports failure for an operation that actually executed.
    """
    save_default_profile()
    FakeClient, state = fake_client(PDF_BYTES)
    monkeypatch.setattr("halocli.cli.HaloClient", FakeClient)

    result = runner.invoke(
        app,
        ["invoices", "pdf", "42", "--apply", "--yes", "--save", str(tmp_path)],
    )

    assert result.exit_code == 2, result.output
    assert "--save must be a file path" in result.output.replace("\x1b", "")
    # zero requests: the guard fires before the client is built
    assert state["calls"] == []


def test_apply_flags_are_rejected_on_read_operations() -> None:
    for flags in (["--apply"], ["--yes"], ["--apply", "--yes"]):
        result = runner.invoke(app, ["tickets", "zapier", *flags])
        assert result.exit_code != 0
        assert "write operations" in plain(result.output)


def test_data_is_rejected_on_read_operations() -> None:
    result = runner.invoke(app, ["tickets", "zapier", "--data", "{}"])

    assert result.exit_code != 0
    assert "--data is only valid for write operations" in plain(result.output)


def test_file_is_rejected_on_non_multipart_operations(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["attachments", "get-document", "5", "--file", str(tmp_path / "x.png")],
    )

    assert result.exit_code != 0
    assert "--file is only valid for multipart operations" in plain(result.output)
