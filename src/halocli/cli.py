from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
import webbrowser
from datetime import date
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import httpx
import typer

from halocli import __version__
from halocli.auth import DEFAULT_CALLBACK_PORT, build_login_request, wait_for_callback
from halocli.client import HaloClient
from halocli.config import HaloProfile, load_profile, save_profile, update_profile
from halocli.discovery import DiscoveryStatus, discover_auth
from halocli.errors import HaloCLIError, classify_error, diagnose_permission_failure
from halocli.models import TokenPayload
from halocli.output import render, render_error
from halocli.resources import RESOURCES, HaloResource, ResourceOperation
from halocli.schema import validate_request as validate_schema_request
from halocli.token_cache import KeyringTokenCache, TokenCache
from halocli.writes import delete_resource, execute_write
from halocli.todo import (
    GraphMicrosoftTodoRepository,
    HaloTodoRepository,
    JsonMicrosoftTodoRepository,
    import_tasks,
    preview_import,
)
from halocli.utils import DEFAULT_LIST_LIMIT, list_all, normalize_halo_result


app = typer.Typer(help="HaloPSA CLI for safe operator and automation workflows.")
auth_app = typer.Typer(help="Authentication helpers.")
todo_app = typer.Typer(help="Create and preview lightweight Halo Todo tasks.")
app.add_typer(auth_app, name="auth")
app.add_typer(todo_app, name="todo")

def main() -> None:
    app()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"halocli {__version__}")
        raise typer.Exit()


@app.callback()
def global_options(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    pass


@app.command()
def configure(
    profile: Annotated[str, typer.Option("--profile")] = "default",
    tenant_url: Annotated[str, typer.Option("--tenant-url", prompt=True)] = "",
    client_id: Annotated[str, typer.Option("--client-id", prompt=True)] = "",
    client_secret: Annotated[
        str | None,
        typer.Option("--client-secret", hide_input=True, confirmation_prompt=False),
    ] = None,
    scope: Annotated[str, typer.Option("--scope")] = "all",
    auth_mode: Annotated[
        str,
        typer.Option(
            "--auth-mode",
            help="Auth mode: client-credentials, halo-interactive, or entra-broker.",
        ),
    ] = "client-credentials",
) -> None:
    normalized_auth_mode = _normalize_auth_mode(auth_mode)
    saved = save_profile(
        profile,
        HaloProfile(
            tenant_url=tenant_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
            auth_mode=normalized_auth_mode,
        ),
    )
    typer.echo(
        f"Saved profile '{profile}' to {saved}. Prefer HALO_CLIENT_SECRET for shared machines."
    )


@auth_app.command("test")
def auth_test(
    profile: Annotated[str, typer.Option("--profile")] = "default",
    output: Annotated[str, typer.Option("--output", "-o")] = "json",
) -> None:
    _run(_auth_test(profile=profile, output=output))


@auth_app.command("whoami")
def auth_whoami(
    profile: Annotated[str, typer.Option("--profile")] = "default",
    output: Annotated[str, typer.Option("--output", "-o")] = "json",
    check: Annotated[
        list[str] | None,
        typer.Option(
            "--check",
            help="Probe an endpoint (GET, read-only) and report whether it is "
            "reachable with the current token. Repeatable.",
        ),
    ] = None,
) -> None:
    _run(_auth_whoami(profile=profile, output=output, check=check or []))


@auth_app.command("discover")
def auth_discover(
    tenant_url: Annotated[str, typer.Option("--tenant-url")] = "",
    profile: Annotated[str | None, typer.Option("--profile")] = None,
    save: Annotated[bool, typer.Option("--save")] = False,
    output: Annotated[str, typer.Option("--output", "-o")] = "json",
) -> None:
    _run(_auth_discover(tenant_url=tenant_url, profile=profile, save=save, output=output))


@auth_app.command("login")
def auth_login(
    profile: Annotated[str, typer.Option("--profile")] = "default",
    allow_file_token_cache: Annotated[bool, typer.Option("--allow-file-token-cache")] = False,
    callback_port: Annotated[int, typer.Option("--callback-port")] = DEFAULT_CALLBACK_PORT,
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds")] = 180,
) -> None:
    try:
        halo_profile = load_profile(profile)
    except ValueError:
        _refuse_unconfirmed_login(profile)
    if halo_profile.auth_mode != "halo_interactive" or not halo_profile.interactive_discovered:
        _refuse_unconfirmed_login(profile)
    login_request = build_login_request(halo_profile, port=callback_port)
    typer.echo("Opening Halo login in your browser...")
    typer.echo(f"If the browser does not open, visit:\n{login_request.authorization_url}")
    webbrowser.open(login_request.authorization_url)
    code = wait_for_callback(login_request, timeout_seconds=timeout_seconds)
    _run(
        _auth_login_exchange(
            profile=profile,
            halo_profile=halo_profile,
            code=code,
            redirect_uri=login_request.redirect_uri,
            code_verifier=login_request.code_verifier,
            allow_file_token_cache=allow_file_token_cache,
        )
    )


@auth_app.command("logout")
def auth_logout(
    profile: Annotated[str, typer.Option("--profile")] = "default",
) -> None:
    deleted = _delete_token(profile)
    render({"ok": True, "profile": profile, "deleted": deleted}, output="json")


def _operation_help(op: ResourceOperation) -> str:
    """One-line help for a nested operation (shown in resource and op listings)."""
    return f"{op.method} {op.path} — {op.summary} [{op.verification}]"


def _operation_command(resource: HaloResource, op: ResourceOperation):
    """Build the callback for one nested operation subcommand.

    `op` arrives as a parameter of this factory rather than as a loop variable
    closed over by a nested function: every generated command therefore binds its
    own operation, which is what keeps the `for op in resource.operations`
    registration loop from late-binding every command to the last operation.
    """

    def command(
        args: Annotated[list[str], typer.Argument()] = [],
        param: Annotated[list[str] | None, typer.Option("--param")] = None,
        data: Annotated[
            str | None,
            typer.Option("--data", help="JSON file path or inline JSON body."),
        ] = None,
        file: Annotated[
            Path | None,
            typer.Option("--file", help="File to upload (multipart operations only)."),
        ] = None,
        save: Annotated[
            Path | None,
            typer.Option("--save", help="Write a binary response to this path."),
        ] = None,
        profile: Annotated[str, typer.Option("--profile")] = "default",
        output: Annotated[str, typer.Option("--output", "-o")] = "json",
        apply: Annotated[bool, typer.Option("--apply", help="Execute the write (requires --yes).")] = False,
        yes: Annotated[bool, typer.Option("--yes", help="Confirm the write (requires --apply).")] = False,
    ) -> None:
        # Usage guards run before anything else: a wrong shape must never reach
        # the network or even load a profile.
        if len(args) != len(op.args):
            expected = ", ".join(op.args)
            expected_part = f": {expected}" if expected else ""
            raise typer.BadParameter(
                f"{op.name} expects {len(op.args)} path argument(s){expected_part}; "
                f"got {len(args)}"
            )
        if not op.write and data is not None:
            raise typer.BadParameter("--data is only valid for write operations")
        if file is not None and not op.multipart:
            raise typer.BadParameter("--file is only valid for multipart operations")
        if file is not None and not file.is_file():
            raise typer.BadParameter(f"--file does not exist or is not a file: {file}")
        if not op.write and (apply or yes):
            raise typer.BadParameter("apply/yes are only valid for write operations")
        if save is not None and save.is_dir():
            raise typer.BadParameter(f"--save must be a file path, not a directory: {save}")

        params = _parse_params(param or [])
        path = _build_operation_path(op, args)

        if op.write:
            execute = _resolve_apply(apply, yes)
        else:
            # Reads never take confirmation flags (rejected above) and always run.
            execute = True
        if op.multipart and execute and file is None:
            raise typer.BadParameter("--file is required to apply this multipart operation")
        if op.write and op.body and data is None:
            raise typer.BadParameter(f"{op.name} requires --data (JSON body).")
        body = _load_data_argument(data) if op.write else None

        if op.write and not execute:
            # Preview: zero network calls, so no profile/client is needed.
            render(
                {
                    "ok": True,
                    "apply": False,
                    "resource": resource.name,
                    "operation": op.name,
                    "method": op.method,
                    "endpoint": path,
                    "params": params,
                    "body": body,
                    "file": _file_preview(file),
                    "verification": op.verification,
                },
                output=output,
            )
            return

        _run(
            _run_operation(
                resource=resource,
                op=op,
                path=path,
                params=params,
                body=body,
                file=file,
                save=save,
                profile=profile,
                output=output,
            )
        )

    return command


# --- Report-specific operations (issue #15) ---------------------------------
#
# `reports run` and `reports clone` are declared like any other ResourceOperation
# -- so the coverage oracle verifies them against the vendored spec and
# `halocli reports --help` lists them with method/path/summary -- but each needs
# behaviour the generic dispatcher cannot express: run shapes a multi-megabyte
# execution response (and must surface Halo's row cap and load_error), clone
# derives its body from another report rather than taking `--data`.

# Fields identifying a stored report rather than describing it. POST /Report
# upserts on `id`, so a clone must drop every one of them or Halo would update
# the source instead of creating a copy. Derived from a hand-built clone whose
# creation was verified live (new id, distinct guid, source unchanged after).
REPORT_IDENTITY_FIELDS = frozenset(
    {
        "_canupdate", "alreadyconverted", "apiquery_id", "builtinid",
        "created_by", "csv_attachment_id", "csv_attachment_link", "date_created",
        "guid", "id", "is_onlinerepository_report", "is_published",
        "json_attachment_id", "json_attachment_link", "last_updated",
        "last_updated_by", "local_library_id", "online_datasource_id", "online_id",
        "pdf_attachment_id", "published_id", "systemreportid",
        "xls_attachment_id", "xls_attachment_link",
    }
)

# Fields compared before/after the clone to prove the source was not modified.
REPORT_SOURCE_FIELDS = (
    "guid", "name", "sql", "description", "availablefields",
    "is_published", "published_id",
)

# Halo's execution wrapper returns at most this many rows (measured: 50,000
# returned from a 135,148-row table). The response carries no cap indicator, so
# a full row count is the only signal available -- warn rather than let a
# truncated set read as complete.
REPORT_ROW_CAP = 50_000


def _report_run_command(resource: HaloResource, op: ResourceOperation):
    """`halocli reports run <id>`: execute a report and shape its result set."""

    def command(
        args: Annotated[list[str], typer.Argument()] = [],
        param: Annotated[list[str] | None, typer.Option("--param")] = None,
        limit: Annotated[
            int,
            typer.Option(
                "--limit",
                help=(
                    "Rows to include in the output. Halo still sends every row it "
                    f"has (up to {REPORT_ROW_CAP:,}), so a small limit keeps the "
                    "payload readable without a second request."
                ),
            ),
        ] = 20,
        timeout: Annotated[
            float,
            typer.Option(
                "--timeout",
                help=(
                    "Seconds to wait for Halo to execute the report. Default 120 "
                    "because execution loads every row (up to 50,000) and Halo's "
                    "own gateway answers 504 at roughly 60s -- a value below that "
                    "just trades a server error for a client one."
                ),
            ),
        ] = 120.0,
        profile: Annotated[str, typer.Option("--profile")] = "default",
        output: Annotated[str, typer.Option("--output", "-o")] = "json",
    ) -> None:
        if len(args) != len(op.args):
            expected = ", ".join(op.args)
            expected_part = f": {expected}" if expected else ""
            raise typer.BadParameter(
                f"{op.name} expects {len(op.args)} path argument(s){expected_part}; "
                f"got {len(args)}"
            )
        if limit < 1:
            raise typer.BadParameter("--limit must be at least 1")
        if timeout <= 0:
            raise typer.BadParameter("--timeout must be greater than 0")
        params = _parse_params(param or [])
        # `run` means "execute": force the load flag last so a stray
        # `--param loadreport=false` cannot turn this into a definition fetch.
        params["loadreport"] = "true"
        path = _build_operation_path(op, args)

        result = _run(
            _run_report(
                resource=resource,
                op=op,
                path=path,
                params=params,
                limit=limit,
                timeout=timeout,
                profile=profile,
            )
        )
        if not result.get("ok"):
            # A failed query still returns HTTP 200 from Halo, so this is the
            # only place it can surface; send it to stderr and exit non-zero
            # rather than rendering a payload that looks successful.
            render_error(result)
            raise typer.Exit(1)
        render(result, output=output, table_fields=result.get("columns"))

    return command


def _execution_failure(
    *,
    resource: HaloResource,
    op: ResourceOperation,
    path: str,
    category: str,
    status_code: int | None,
    error: str,
    detail: str | None = None,
    diagnostic: str = "",
) -> dict[str, Any]:
    """Shape a failed execution so the caller can render it and exit 1.

    Three distinct failures arrive here and all of them would otherwise look
    identical or empty: Halo's 504 (its own gateway gives up at ~60s), a client
    timeout (httpx exceptions stringify to ``""``), and permission denials.
    """
    payload: dict[str, Any] = {
        "ok": False,
        "resource": resource.name,
        "operation": op.name,
        "method": op.method,
        "endpoint": path,
        "category": category,
        "status_code": status_code,
        "error": error,
    }
    if detail:
        payload["hint"] = detail
    if diagnostic:
        payload["diagnostic"] = diagnostic
    return payload


def _execution_hint(status_code: int | None, category: str) -> str | None:
    """Actionable next step for the two slow-execution failure modes."""
    if status_code == 504:
        return (
            "Halo's gateway timed out executing this report (measured: ~60s before "
            f"it answers 504, and results cap at {REPORT_ROW_CAP:,} rows). Narrow "
            "the report by date or filters, or split it into smaller reports."
        )
    if category == "timeout":
        return (
            f"The client stopped waiting for Halo. Execution loads every row Halo "
            f"has (up to {REPORT_ROW_CAP:,}); raise --timeout, or narrow the report."
        )
    return None


async def _run_report(
    *,
    resource: HaloResource,
    op: ResourceOperation,
    path: str,
    params: dict[str, str],
    limit: int,
    timeout: float,
    profile: str,
) -> dict[str, Any]:
    halo_profile = load_profile(profile)
    # Fail fast on execution: Halo's 504 is a gateway deadline on a heavy query,
    # not a transient blip (measured: ~60s to 504, repeatably). Retrying would
    # re-run the same multi-second query up to max_retries times, turning a
    # clear one-minute failure into a four-minute hang.
    exec_profile = halo_profile.model_copy(update={"max_retries": 0})
    try:
        async with HaloClient(exec_profile, profile_name=profile) as client:
            body = await client.request("GET", path, params=params, timeout=timeout)
    except HaloCLIError as exc:
        # Includes Halo's own 504: its gateway gives up at ~60s on a large
        # report, which is a server limit no client timeout can fix.
        return _execution_failure(
            resource=resource,
            op=op,
            path=path,
            category=exc.category,
            status_code=exc.status_code,
            error=str(exc) or type(exc).__name__,
            detail=_execution_hint(exc.status_code, exc.category),
            diagnostic=diagnose_permission_failure(exc),
        )
    except Exception as exc:  # httpx timeouts stringify to ""
        err = classify_error(exc)
        return _execution_failure(
            resource=resource,
            op=op,
            path=path,
            category=err.category,
            status_code=err.status_code,
            error=str(exc) or str(err),
            detail=_execution_hint(err.status_code, err.category),
        )

    payload: dict[str, Any] = {
        "ok": True,
        "resource": resource.name,
        "operation": op.name,
        "method": op.method,
        "endpoint": path,
    }
    if not isinstance(body, dict):
        payload.update(
            ok=False,
            category="validation",
            error=f"Unexpected response shape from Halo: expected an object, got {type(body).__name__}",
        )
        return payload

    report = body.get("report")
    if not isinstance(report, dict):
        payload.update(
            ok=False,
            category="validation",
            error="Response has no 'report' object; Halo did not execute this report",
        )
        return payload
    if report.get("load_error"):
        # Halo reports query failures with HTTP 200, so nothing else would flag it.
        payload.update(
            ok=False,
            category="validation",
            status_code=200,
            error=f"Halo could not execute this report: {report['load_error']}",
        )
        return payload

    rows = report.get("rows")
    if rows is None:
        payload.update(
            ok=False,
            category="validation",
            error="Report executed but returned no 'rows' and no load_error",
        )
        return payload
    if not isinstance(rows, list):
        payload.update(
            ok=False,
            category="validation",
            error=f"Unexpected rows shape: {type(rows).__name__}",
        )
        return payload

    columns = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
    if not columns:
        # An empty result still has a defined shape, and Halo echoes it in the
        # envelope as `availablefields` (verified present in the execution
        # response for reports 5, 143 and 146, matching row keys exactly there).
        # Without this a zero-row run reports no columns at all, so
        # `--output table` renders nothing.
        available = body.get("availablefields")
        if isinstance(available, str):
            columns = [line.strip() for line in available.splitlines() if line.strip()]
    payload.update(
        id=body.get("id"),
        name=body.get("name"),
        row_count=len(rows),
        count=min(limit, len(rows)),
        columns=columns,
        items=rows[:limit],
    )
    if len(rows) >= REPORT_ROW_CAP:
        payload["capped"] = True
        payload["hint"] = (
            f"Halo returned {len(rows):,} rows, which is its maximum: this report "
            "may hold more. Narrow it (date range, filters, fewer columns) to see "
            "everything, and note that a repeated run may return a different "
            "window if the underlying data grows."
        )
    elif len(rows) > limit:
        payload["hint"] = (
            f"Showing {limit} of {len(rows):,} rows; pass --limit {len(rows)} for all."
        )
    return payload


def _report_clone_command(resource: HaloResource, op: ResourceOperation):
    """`halocli reports clone <id> --name ...`: copy a report to a new record."""

    def command(
        source: Annotated[str, typer.Argument(help="ID of the report to copy.")],
        name: Annotated[str, typer.Option("--name", help="Name for the new report.")],
        profile: Annotated[str, typer.Option("--profile")] = "default",
        output: Annotated[str, typer.Option("--output", "-o")] = "json",
        apply: Annotated[bool, typer.Option("--apply", help="Execute the write (requires --yes).")] = False,  # noqa: A002
        yes: Annotated[bool, typer.Option("--yes", help="Confirm the write (requires --apply).")] = False,
    ) -> None:
        if not name.strip():
            raise typer.BadParameter("--name must not be empty")
        path = _build_operation_path(op, [])
        execute = _resolve_apply(apply, yes)
        if not execute:
            # Preview is zero-network by design: the body is derived from the
            # source at apply time, so nothing can be shown yet and no profile
            # is needed to describe the plan.
            render(
                {
                    "ok": True,
                    "apply": False,
                    "resource": resource.name,
                    "operation": op.name,
                    "method": op.method,
                    "endpoint": path,
                    "preview": {
                        "source_id": source,
                        "name": name,
                        "stripped_fields": sorted(REPORT_IDENTITY_FIELDS),
                        "steps": [
                            f"GET {path}/{source} (read the source report)",
                            "drop identity fields so Halo creates a new record",
                            f"set name to {name!r}",
                            f"POST {path} with a one-element array body",
                            "read the new record back to verify it",
                            "re-read the source to verify it is unchanged",
                        ],
                    },
                    "verification": op.verification,
                },
                output=output,
            )
            return

        result = _run(
            _run_report_clone(
                resource=resource,
                op=op,
                path=path,
                source=source,
                name=name,
                profile=profile,
            )
        )
        # Write payloads render to stdout even when they fail, matching
        # _finish_write: the failure above can carry the *new* report id, and
        # losing that to a stderr redirect is what invites a duplicate clone.
        render(result, output=output)
        if not result.get("ok"):
            raise typer.Exit(1)

    return command


async def _run_report_clone(
    *,
    resource: HaloResource,
    op: ResourceOperation,
    path: str,
    source: str,
    name: str,
    profile: str,
) -> dict[str, Any]:
    """GET source -> POST an identity-stripped copy -> verify both sides.

    Verification reads back rather than trusting the POST's status code: a 201
    that silently updated the source would otherwise look identical to a clone.
    """
    halo_profile = load_profile(profile)
    source_path = f"{path}/{quote(source, safe='')}"
    payload: dict[str, Any] = {
        "ok": True,
        "apply": True,
        "resource": resource.name,
        "operation": op.name,
        "method": op.method,
        "endpoint": path,
        "source_id": source,
        "name": name,
    }

    async with HaloClient(halo_profile, profile_name=profile) as client:
        before = await client.request("GET", source_path)
        if not isinstance(before, dict):
            payload.update(
                ok=False, category="validation",
                error=f"GET {source_path} did not return a report object",
            )
            return payload

        copy = {k: v for k, v in before.items() if k not in REPORT_IDENTITY_FIELDS}
        copy["name"] = name
        # Halo deserializes AnalyzerProfile[] here: an object returns 400 and a
        # missing JSON content type returns 415 (both hit while building this).
        created = await client.request("POST", path, json_body=[copy])
        if isinstance(created, list):
            created = created[0] if created else {}
        if not isinstance(created, dict) or created.get("id") is None:
            payload.update(
                ok=False, category="validation",
                error="POST did not return the created report (no id)",
                hint=(
                    "Halo answered without returning the new record, so a copy "
                    "may still exist under a different id. List reports and "
                    "check before retrying -- retrying creates a second copy."
                ),
            )
            return payload

        new_id = str(created["id"])
        # The copy exists from this point on. If a verification read fails, the
        # operator still needs its id: a generic error that omits it invites a
        # retry, and a retry creates a *second* copy. Catch and report instead
        # of letting _run render a bare exception.
        try:
            after_source = await client.request("GET", source_path)
            verified = await client.request("GET", f"{path}/{quote(new_id, safe='')}")
        except Exception as exc:  # noqa: BLE001
            err = exc if isinstance(exc, HaloCLIError) else classify_error(exc)
            payload.update(
                id=created["id"],
                ok=False,
                category=err.category,
                status_code=err.status_code,
                error=str(exc) or str(err),
                verification="skipped",
                hint=(
                    f"The copy WAS created (id {created['id']}), but the "
                    "verification reads failed, so its state is unverified. "
                    "Inspect it before retrying -- retrying creates a second copy."
                ),
            )
            diagnostic = diagnose_permission_failure(err)
            if diagnostic:
                payload["diagnostic"] = diagnostic
            return payload

    payload["id"] = created["id"]
    payload["new_guid_differs"] = (
        isinstance(after_source, dict)
        and isinstance(verified, dict)
        and verified.get("guid") != after_source.get("guid")
    )
    changed = []
    if isinstance(after_source, dict):
        changed = [
            f for f in REPORT_SOURCE_FIELDS
            if after_source.get(f) != before.get(f)
        ]
    else:
        changed = ["<source re-read failed>"]
    payload["source_unchanged"] = not changed
    if changed:
        payload["source_changed_fields"] = changed
    copied_sql = isinstance(verified, dict) and verified.get("sql") == before.get("sql")
    payload["sql_copied"] = copied_sql
    payload["applied_name"] = (
        isinstance(verified, dict) and verified.get("name") == name
    )

    if not payload["source_unchanged"] or not copied_sql or not payload["applied_name"]:
        payload.update(
            ok=False,
            category="validation",
            error="Clone verification failed; inspect the flags before trusting this result",
        )
    return payload


_OPERATION_HANDLERS = {
    "report_run": _report_run_command,
    "report_clone": _report_clone_command,
}


def _resource_command(resource: HaloResource):
    resource_app = typer.Typer(help=f"{resource.name.title()} commands.")

    @resource_app.command("list")
    def list_command(
        profile: Annotated[str, typer.Option("--profile")] = "default",
        output: Annotated[str, typer.Option("--output", "-o")] = "json",
        open_only: Annotated[bool, typer.Option("--open")] = False,
        page_size: Annotated[int, typer.Option("--page-size")] = 100,
        max_pages: Annotated[int | None, typer.Option("--max-pages")] = None,
        max_records: Annotated[int | None, typer.Option("--max-records")] = None,
        fetch_all: Annotated[
            bool,
            typer.Option(
                "--all",
                help="Fetch every record. Without it, results stop at 500 records "
                "(reporting truncation) so a large tenant cannot make `list` hang.",
            ),
        ] = False,
        param: Annotated[list[str] | None, typer.Option("--param")] = None,
    ) -> None:
        if fetch_all and (max_records is not None or max_pages is not None):
            raise typer.BadParameter("--all cannot be combined with --max-records/--max-pages.")
        _run(
            _list_resource(
                resource=resource,
                profile=profile,
                output=output,
                open_only=open_only,
                page_size=page_size,
                max_pages=max_pages,
                max_records=max_records,
                fetch_all=fetch_all,
                params=_parse_params(param or []),
            )
        )

    if resource.supports_get:

        @resource_app.command("get")
        def get_command(
            item_id: str,
            profile: Annotated[str, typer.Option("--profile")] = "default",
            output: Annotated[str, typer.Option("--output", "-o")] = "json",
        ) -> None:
            _run(_get_resource(resource=resource, item_id=item_id, profile=profile, output=output))

    if resource.supports_create:

        @resource_app.command("create")
        def create_command(
            data: Annotated[str, typer.Option("--data", help="JSON file path or inline JSON object.")],
            profile: Annotated[str, typer.Option("--profile")] = "default",
            output: Annotated[str, typer.Option("--output", "-o")] = "json",
            apply: Annotated[bool, typer.Option("--apply", help="Execute the write (requires --yes).")] = False,  # noqa: A002
            yes: Annotated[bool, typer.Option("--yes", help="Confirm the write (requires --apply).")] = False,
        ) -> None:
            execute = _resolve_apply(apply, yes)
            payload = _require_payload(_load_data_argument(data))
            result = _run(
                _write_resource(
                    resource=resource,
                    payload=payload,
                    update=False,
                    profile=profile,
                    apply=execute,
                )
            )
            _finish_write(result, output=output)

    if resource.supports_update:

        @resource_app.command("update")
        def update_command(
            item_id: str,
            data: Annotated[str, typer.Option("--data", help="JSON file path or inline JSON object.")],
            profile: Annotated[str, typer.Option("--profile")] = "default",
            output: Annotated[str, typer.Option("--output", "-o")] = "json",
            apply: Annotated[bool, typer.Option("--apply", help="Execute the write (requires --yes).")] = False,  # noqa: A002
            yes: Annotated[bool, typer.Option("--yes", help="Confirm the write (requires --apply).")] = False,
        ) -> None:
            execute = _resolve_apply(apply, yes)
            payload = dict(_require_payload(_load_data_argument(data)))
            payload["id"] = int(item_id) if item_id.isdigit() else item_id
            result = _run(
                _write_resource(
                    resource=resource,
                    payload=payload,
                    update=True,
                    profile=profile,
                    apply=execute,
                )
            )
            _finish_write(result, output=output)

    if resource.supports_delete:

        @resource_app.command("delete")
        def delete_command(
            item_id: str,
            profile: Annotated[str, typer.Option("--profile")] = "default",
            output: Annotated[str, typer.Option("--output", "-o")] = "json",
            apply: Annotated[bool, typer.Option("--apply", help="Execute the delete (requires --yes).")] = False,  # noqa: A002
            yes: Annotated[bool, typer.Option("--yes", help="Confirm the delete (requires --apply).")] = False,
        ) -> None:
            execute = _resolve_apply(apply, yes)
            result = _run(
                _delete_command(
                    resource=resource,
                    item_id=item_id,
                    profile=profile,
                    apply=execute,
                )
            )
            _finish_write(result, output=output)

    for operation in resource.operations:
        # A declared handler means the generic dispatcher cannot express this
        # operation; an unknown name is a config error, not a licence to fall
        # back to generic behaviour that would silently do the wrong thing.
        handler = _OPERATION_HANDLERS.get(operation.handler)
        if operation.handler and handler is None:
            raise ValueError(
                f"operation {resource.name}.{operation.name} names unknown handler "
                f"{operation.handler!r} (known: {sorted(_OPERATION_HANDLERS)})"
            )
        callback = handler(resource, operation) if handler else _operation_command(resource, operation)
        resource_app.command(operation.name, help=_operation_help(operation))(callback)

    return resource_app


for _resource in RESOURCES:
    app.add_typer(_resource_command(_resource), name=_resource.name)


# The validator prefixes array-element problems with `body[N]: `; that prefix is
# code-generated, so stripping exactly it (and nothing caller-shaped) before the
# startswith check keeps path text containing "warning: " from downgrading an
# unknown-endpoint diagnostic to advisory (CodeRabbit review, PR #29).
_SPEC_BODY_PREFIX = re.compile(r"^body\[\d+\]: ")


def _split_spec_problems(problems: list[str]) -> tuple[list[str], list[str]]:
    """Split validator problems into ``(warnings, fatal)``.

    Array-body problems arrive path-prefixed (``body[0]: warning: ...``) while
    dict-body problems do not, so classification strips the validator's own
    ``body[N]: `` prefix before the ``warning: `` startswith check. A bare
    substring search would let a caller-crafted *path* containing "warning: "
    silence an unknown-endpoint refusal (CodeRabbit review, PR #29); the old
    startswith-only version refused every array-body warning instead, breaking
    the documented spec_warnings flow (issue #28).
    """
    def is_warning(problem: str) -> bool:
        stripped = _SPEC_BODY_PREFIX.sub("", problem, count=1)
        return stripped.startswith("warning: ")

    warnings = [problem for problem in problems if is_warning(problem)]
    fatal = [problem for problem in problems if not is_warning(problem)]
    return warnings, fatal


@app.command()
def raw(
    method: str,
    path: str,
    profile: Annotated[str, typer.Option("--profile")] = "default",
    output: Annotated[str, typer.Option("--output", "-o")] = "json",
    param: Annotated[list[str] | None, typer.Option("--param")] = None,
    data: Annotated[str | None, typer.Option("--data")] = None,
    apply: Annotated[bool, typer.Option("--apply")] = False,  # noqa: A002
    yes: Annotated[bool, typer.Option("--yes")] = False,
    validate: Annotated[
        bool,
        typer.Option(
            "--validate/--no-validate",
            help="Check method/path/body against the vendored Halo OpenAPI spec (default on).",
        ),
    ] = True,
) -> None:
    """Send a spec-validated request to Halo; writes require --apply --yes."""
    method = method.upper()
    if method in {"POST", "PUT", "PATCH", "DELETE"} and not (apply and yes):
        raise typer.BadParameter(f"Refusing {method} {path} without --apply --yes.")
    body = _load_data_argument(data)
    warnings: list[str] = []
    if validate:
        problems = validate_schema_request(method, path, body)
        warnings, fatal = _split_spec_problems(problems)
        if fatal:
            render_error(
                {
                    "ok": False,
                    "category": "validation",
                    "status_code": None,
                    "error": f"Refusing {method} {path}: spec validation failed.",
                    "problems": fatal,
                    "diagnostic": (
                        "Fix the request, or pass --no-validate to bypass the vendored "
                        "OpenAPI spec check."
                    ),
                }
            )
            raise typer.Exit(1)
    _run(
        _raw(
            profile=profile,
            output=output,
            method=method,
            path=path,
            params=_parse_params(param or []),
            body=body,
            spec_warnings=warnings,
        )
    )


@app.command()
def catalog(
    query: str,
    limit: Annotated[int, typer.Option("--limit", min=1, max=50)] = 10,
    output: Annotated[str, typer.Option("--output", "-o")] = "json",
) -> None:
    """Discover Halo resources and OpenAPI operations offline (no tenant calls)."""
    from halocli import mcp_server

    results = mcp_server.search_catalog(query, limit)
    render(
        {
            "ok": True,
            "query": query,
            "count": len(results),
            "results": results,
            "hint": (
                "Pass a result's endpoint to `halocli raw <METHOD> <path>` "
                "(writes need --apply --yes)."
            ),
        },
        output=output,
    )


@app.command()
def search(
    terms: Annotated[
        list[str],
        typer.Argument(
            metavar="TERM",
            help=(
                "One free-text term to find across tickets, articles, clients, "
                "users, assets and services."
            ),
        ),
    ],
    count_per_entity: Annotated[
        int,
        typer.Option(
            "--count-per-entity",
            min=1,
            max=100,
            help="Per-entity cap sent to Halo (the server default is about 5).",
        ),
    ] = 5,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            min=1,
            help=(
                "Rows to include in the output. Halo still returns every "
                "entity's matches (observed up to ~160 KB at 50 per entity), "
                "so a small limit keeps the payload readable."
            ),
        ),
    ] = 20,
    profile: Annotated[str, typer.Option("--profile")] = "default",
    output: Annotated[str, typer.Option("--output", "-o")] = "json",
) -> None:
    """Search the live Halo tenant across tickets, articles, clients, users, assets and services."""
    # 1.0.0 breaking rename: this name used to mean offline catalog discovery.
    # Old multi-word invocations cannot be a live-search term, so fail loudly
    # with the migration path instead of silently querying the tenant.
    if len(terms) != 1:
        raise typer.BadParameter(
            "search now queries the LIVE Halo tenant with a single term; offline "
            "catalog discovery moved to `halocli catalog` (formerly `halocli search`)."
        )
    term = terms[0].strip()
    if not term:
        raise typer.BadParameter("search term must be non-empty")

    result = _run(
        _run_search_live(
            term=term,
            count_per_entity=count_per_entity,
            limit=limit,
            profile=profile,
        )
    )
    if not result.get("ok"):
        render_error(result)
        raise typer.Exit(1)
    render(
        result,
        output=output,
        table_fields=("use", "id", "name", "summary", "client_name"),
    )


async def _run_search_live(
    *,
    term: str,
    count_per_entity: int,
    limit: int,
    profile: str,
) -> dict[str, Any]:
    """GET /Search and shape the bare array by the `use` discriminator."""
    halo_profile = load_profile(profile)
    path = "/Search"
    params = {"search": term, "count_per_entity": str(count_per_entity)}
    try:
        async with HaloClient(halo_profile, profile_name=profile) as client:
            body = await client.request("GET", path, params=params)
    except HaloCLIError as exc:
        return {
            "ok": False,
            "command": "search",
            "method": "GET",
            "endpoint": path,
            "category": exc.category,
            "status_code": exc.status_code,
            "error": str(exc) or type(exc).__name__,
            "diagnostic": diagnose_permission_failure(exc),
        }
    except Exception as exc:  # httpx timeouts stringify to ""
        err = classify_error(exc)
        return {
            "ok": False,
            "command": "search",
            "method": "GET",
            "endpoint": path,
            "category": err.category,
            "status_code": err.status_code,
            "error": str(exc) or str(err),
        }

    payload: dict[str, Any] = {
        "ok": True,
        "command": "search",
        "method": "GET",
        "endpoint": path,
        "query": term,
        "count_per_entity": count_per_entity,
    }
    if not isinstance(body, list):
        # The spec declares no response schema for /Search; this is the shape
        # the tenant has always answered with, so a deviation is a failure.
        payload.update(
            ok=False,
            category="validation",
            error=(
                "Unexpected response shape from Halo: expected an array, got "
                f"{type(body).__name__}"
            ),
        )
        return payload

    # `use` is the stable entity key (`table` is absent on service rows).
    entity_counts: dict[str, int] = {}
    for item in body:
        if isinstance(item, dict):
            key = str(item.get("use") or "unknown")
            entity_counts[key] = entity_counts.get(key, 0) + 1
    payload.update(
        row_count=len(body),
        count=min(limit, len(body)),
        entity_counts=entity_counts,
        items=body[:limit],
    )
    if len(body) > limit:
        payload["hint"] = f"Showing {limit} of {len(body)} matches; pass --limit {len(body)} for all."
    elif not body:
        payload["hint"] = "No matches. Broaden the term or check the spelling."
    return payload


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host", help="Bind address for stdio (ignored; reserved).")] = "stdio",
) -> None:
    """Run the code-mode MCP server on stdin/stdout (newline-delimited JSON-RPC 2.0)."""
    from halocli import mcp_server

    del host  # reserved so future transports can be added without breaking callers
    mcp_server.main()


@todo_app.command("import-ms")
def todo_import_ms(
    source_json: Annotated[str | None, typer.Option("--source-json")] = None,
    list_name: Annotated[str | None, typer.Option("--list")] = None,
    include_completed: Annotated[bool, typer.Option("--include-completed")] = False,
    max_records: Annotated[int | None, typer.Option("--max-records")] = 50,
    apply: Annotated[bool, typer.Option("--apply", help="Create Halo Todo records.")] = False,  # noqa: A002
    complete_source: Annotated[
        bool,
        typer.Option("--complete-source", help="Mark each Microsoft To Do task complete after a successful Halo import."),
    ] = False,
    profile: Annotated[str, typer.Option("--profile", help="Halo profile used when --apply is set.")] = "default",
    output: Annotated[str, typer.Option("--output", "-o")] = "json",
) -> None:
    if complete_source and not apply:
        raise typer.BadParameter("--complete-source requires --apply.")
    if apply and source_json:
        raise typer.BadParameter("--apply cannot complete or mutate a --source-json import.")
    repository = (
        JsonMicrosoftTodoRepository(source_json)
        if source_json
        else GraphMicrosoftTodoRepository.from_shared_auth(
            scopes=["Tasks.Read", "Tasks.ReadWrite"] if complete_source else ["Tasks.Read"]
        )
    )
    if apply:
        _run(
            _todo_import_ms_apply(
                repository=repository,
                profile=profile,
                list_name=list_name,
                include_completed=include_completed,
                max_records=max_records,
                complete_source=complete_source,
                output=output,
            )
        )
        return
    previews = preview_import(
        repository,
        list_name=list_name,
        include_completed=include_completed,
        max_records=max_records,
    )
    render({"source": "microsoft.todo", "count": len(previews), "items": previews}, output=output)


@todo_app.command("add")
def todo_add(
    title: str,
    profile: Annotated[str, typer.Option("--profile")] = "default",
    output: Annotated[str, typer.Option("--output", "-o")] = "json",
    description: Annotated[str, typer.Option("--description", "--body")] = "",
    owner: Annotated[int | None, typer.Option("--owner")] = None,
    due: Annotated[str | None, typer.Option("--due")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag")] = None,
) -> None:
    _run(
        _todo_add(
            title=title,
            profile=profile,
            output=output,
            description=description,
            owner=owner,
            due=date.fromisoformat(due) if due else None,
            tags=tag or [],
        )
    )


@todo_app.command("web")
def todo_web(
    profile: Annotated[str, typer.Option("--profile")] = "default",
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8766,
    reload: Annotated[bool, typer.Option("--reload")] = False,
) -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise typer.BadParameter("Install halocli with the web extra: pip install 'halocli[web]'.") from exc

    from halocli.todo_web import create_halo_todo_api

    typer.echo(f"Starting Halo Todo API at http://{host}:{port} (docs at http://{host}:{port}/docs)")
    uvicorn.run(create_halo_todo_api(profile=profile), host=host, port=port, reload=reload)


async def _auth_test(*, profile: str, output: str) -> None:
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        result = await client.test_auth()
    render({"ok": True, "profile": profile, "result": normalize_halo_result(result)}, output=output)


def _granted_scope(profile: str) -> dict[str, Any]:
    """Read the scope Halo actually granted, from whichever cache holds the token.

    Scope is fixed at token issuance (a refresh grant never widens it), so this
    is the authoritative answer for the token in use -- not the profile's
    requested scope.
    """
    payload: dict[str, Any] | None = None
    source = "keyring"
    try:
        payload = KeyringTokenCache().load(profile)
    except Exception:  # noqa: BLE001 - store unavailable: fall through to file cache
        payload = None
    if not payload:
        source = "file"
        try:
            payload = TokenCache(allow_file_cache=True).load(profile)
        except Exception:  # noqa: BLE001
            payload = None
    if not payload:
        return {"present": False, "source": None, "scope": [], "scope_text": None}
    raw = payload.get("scope")
    if isinstance(raw, str):
        scopes = sorted(set(raw.split()))
    elif isinstance(raw, list):
        scopes = sorted({str(item) for item in raw})
    else:
        scopes = []
    return {
        "present": True,
        "source": source,
        "scope": scopes,
        "scope_text": " ".join(scopes) or None,
        "expires_at": payload.get("expires_at"),
        "token_type": payload.get("token_type"),
    }


async def _auth_whoami(*, profile: str, output: str, check: list[str]) -> None:
    halo_profile = load_profile(profile)
    scope_info = _granted_scope(profile)

    identity: dict[str, Any] | None = None
    probes: list[dict[str, Any]] = []
    async with HaloClient(halo_profile, profile_name=profile) as client:
        try:
            me = normalize_halo_result(await client.test_auth())
            if isinstance(me, dict):
                identity = {
                    "id": me.get("id"),
                    "name": me.get("name"),
                    "email": me.get("email"),
                    "team": me.get("team"),
                    "jobtitle": me.get("jobtitle"),
                    "is_agent": me.get("is_agent"),
                }
        except Exception as exc:  # noqa: BLE001 - identity is best-effort below
            identity = {"error": str(exc)}

        for path in check:
            clean = path.strip()
            if not clean.startswith("/"):
                clean = f"/{clean}"
            entry: dict[str, Any] = {"endpoint": clean, "method": "GET"}
            try:
                # Read-only probe with a one-record page; never a write.
                result = await client.raw("GET", clean, params={"take": 1})
                entry["reachable"] = True
                entry["sample"] = normalize_halo_result(result)
            except HaloCLIError as exc:
                entry["reachable"] = False
                entry["category"] = exc.category
                entry["status_code"] = exc.status_code
                entry["error"] = str(exc)
                if exc.category == "permission":
                    entry["diagnostic"] = diagnose_permission_failure(exc)
            except Exception as exc:  # noqa: BLE001
                entry["reachable"] = False
                entry["category"] = classify_error(exc).category
                entry["error"] = str(exc)
            probes.append(entry)

    requested = halo_profile.scope
    payload: dict[str, Any] = {
        "ok": True,
        "profile": profile,
        "tenant_url": halo_profile.tenant_url,
        "auth_mode": halo_profile.auth_mode,
        "client_id": halo_profile.client_id,
        "requested_scope": requested,
        "granted_scope": scope_info,
        "identity": identity,
    }
    if scope_info.get("present") and requested and scope_info.get("scope_text") != requested:
        payload["scope_note"] = (
            "Granted scope differs from the profile's requested scope. Halo narrows "
            "the grant to what the API application's Permissions tab allows; a 403 "
            "means an endpoint needs a scope that was not granted. Probe one with "
            "`halocli auth whoami --check /Invoice` (substitute the endpoint you "
            "called)."
        )
    if probes:
        payload["checks"] = probes
        payload["checks_all_reachable"] = all(
            entry.get("reachable") for entry in probes
        )
    render(payload, output=output)


async def _auth_discover(*, tenant_url: str, profile: str | None, save: bool, output: str) -> None:
    result = await discover_auth(tenant_url)
    saved = False
    if save:
        if not profile:
            raise typer.BadParameter("--save requires --profile.")
        if result.status != DiscoveryStatus.INTERACTIVE_SUPPORTED:
            raise typer.BadParameter("Discovery did not confirm interactive support; profile was not updated.")
        update_profile(
            profile,
            {
                "tenant_url": result.tenant_url,
                "auth_mode": "halo_interactive",
                "interactive_discovered": True,
                "authorization_endpoint": result.authorization_endpoint,
                "token_endpoint": result.token_endpoint,
            },
        )
        saved = True
    payload = result.model_dump(mode="json")
    payload["saved_profile"] = profile if saved else None
    render(payload, output=output)


async def _auth_login_exchange(
    *,
    profile: str,
    halo_profile: HaloProfile,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    allow_file_token_cache: bool,
) -> None:
    data = {
        "grant_type": "authorization_code",
        "client_id": halo_profile.client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    if halo_profile.client_secret:
        data["client_secret"] = halo_profile.client_secret
    async with httpx.AsyncClient(timeout=halo_profile.timeout) as http:
        response = await http.post(halo_profile.token_endpoint or halo_profile.auth_token_url, data=data)
    if response.status_code >= 300:
        err = RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
        err.response = response  # type: ignore[attr-defined]
        raise classify_error(err, endpoint="/auth/token")
    token = TokenPayload.model_validate(response.json())
    token_data = token.model_dump(exclude_none=True)
    token_data["expires_at"] = time.time() + token.expires_in
    if allow_file_token_cache:
        TokenCache(halo_profile, allow_file_cache=True).save(profile, token_data)
        store = "file"
    else:
        secure_cache = KeyringTokenCache()
        secure_cache.save(profile, token_data)
        store = secure_cache.store_label()
    render(
        {
            "ok": True,
            "profile": profile,
            "token_store": store,
            "expires_in": token.expires_in,
            "has_refresh_token": bool(token.refresh_token),
        },
        output="json",
    )


async def _list_resource(
    *,
    resource: HaloResource,
    profile: str,
    output: str,
    open_only: bool,
    page_size: int,
    max_pages: int | None,
    max_records: int | None,
    fetch_all: bool,
    params: dict[str, str],
) -> None:
    if resource.name == "tickets" and open_only:
        params["open_only"] = "true"
    # --all opts out of the default record ceiling; explicit --max-* always wins.
    effective_max_records = max_records
    if not fetch_all and effective_max_records is None and max_pages is None:
        effective_max_records = DEFAULT_LIST_LIMIT
    halo_profile = load_profile(profile)
    stats: dict[str, Any] = {}
    async with HaloClient(halo_profile, profile_name=profile) as client:
        rows = await list_all(
            lambda **kwargs: client.list_resource(resource.name, **kwargs),
            page_size=page_size,
            max_pages=max_pages,
            max_records=effective_max_records,
            list_key=resource.list_key,
            stats=stats,
            **params,
        )
    payload: dict[str, Any] = {"resource": resource.name, "count": len(rows), "items": rows}
    if stats.get("paging_ignored"):
        # Halo answered page 2 with page 1 verbatim (issue #24): the endpoint
        # ignores paging. The duplicate was never appended, and one
        # single-page recovery (count=<total>) was already attempted.
        payload["paging_ignored"] = True
    if stats.get("truncated"):
        # Say so plainly: a truncated payload must never read as a complete one.
        payload["truncated"] = True
        payload["total_available"] = stats.get("record_count")
        if stats.get("paging_ignored"):
            payload["hint"] = (
                f"Halo ignores paging on this endpoint (page 2 repeats page 1) "
                f"and returned {len(rows)} of {stats.get('record_count') or 'unknown'} "
                "records in a single page."
            )
        else:
            payload["hint"] = (
                f"Stopped at {len(rows)} of "
                f"{stats.get('record_count') or 'unknown'} records. "
                "Pass --all to fetch every record."
            )
    render(payload, output=output, table_fields=resource.table_fields)


async def _get_resource(
    *,
    resource: HaloResource,
    item_id: str,
    profile: str,
    output: str,
) -> None:
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        item = await client.get_resource(resource.name, item_id)
    render(
        {"resource": resource.name, "id": item_id, "item": normalize_halo_result(item)},
        output=output,
        table_fields=resource.table_fields,
    )


def _resolve_apply(apply: bool, yes: bool) -> bool:
    """Normalize the `--apply --yes` pair into a single execute decision.

    Both flags are required to write (matching `halocli raw`); preview is the default
    when neither is given. Passing exactly one is a usage error, not a silent preview —
    a half-confirmed write flag deserves a loud refusal.
    """
    if apply and yes:
        return True
    if apply or yes:
        raise typer.BadParameter(
            "Writes require both --apply and --yes (preview is the default; "
            "pass --apply --yes to execute)."
        )
    return False


def _require_payload(body: object) -> dict:
    if not isinstance(body, dict):
        raise typer.BadParameter("--data must decode to a JSON object.")
    return body


async def _write_resource(
    *,
    resource: HaloResource,
    payload: dict,
    update: bool,
    profile: str,
    apply: bool,
) -> dict:
    if not apply:
        # Preview: zero network calls, so no profile/client is needed.
        return await execute_write(None, resource, payload, update=update, apply=False)
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        return await execute_write(client, resource, payload, update=update, apply=True)


async def _delete_command(
    *,
    resource: HaloResource,
    item_id: str,
    profile: str,
    apply: bool,
) -> dict:
    if not apply:
        return await delete_resource(None, resource, item_id, apply=False)
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        return await delete_resource(client, resource, item_id, apply=True)


def _mask_sensitive(value: Any) -> Any:
    """Deep-copy ``value`` with credential-shaped fields masked for display.

    Rendered write payloads are echoed to stdout (preview and post-apply), and
    `users update --data '{"new_password": ...}'` would print the submitted
    credential into terminals and CI logs (CodeRabbit Medium, PR #29). Only
    the display copy is masked - the wire payload is untouched. Keys are
    masked when they look credential-shaped (password/secret/verifier/token);
    values keep their JSON type so the preview shape stays readable.
    """
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            if re.search(r"password|secret|code_verifier|_token$|^token$", str(key), re.I):
                masked[key] = "***"
            else:
                masked[key] = _mask_sensitive(item)
        return masked
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    return value


def _finish_write(result: dict, *, output: str) -> None:
    """Render a resource write result (preview or apply) and exit 1 on failure.

    Credentials in the echoed payload are masked on the way to stdout
    (_mask_sensitive); nothing about the request actually sent changes.
    """
    render(_mask_sensitive(result), output=output)
    if not result.get("ok"):
        raise typer.Exit(1)


def _build_operation_path(op: ResourceOperation, values: list[str]) -> str:
    """Fill the operation's `{name}` placeholders with `values`, in template order.

    Each value is percent-encoded with an empty safe set so a path argument can
    never inject `/`, `?` or `#` into the request path. The leftover-placeholder
    check is defensive: a template that still contains braces means arity and
    template drifted apart, which must be a usage error, not a broken request.
    """
    path = op.path
    for name, value in zip(op.args, values):
        path = path.replace("{" + name + "}", quote(value, safe=""))
    if "{" in path or "}" in path:
        raise typer.BadParameter(
            f"Could not substitute every path placeholder in {op.path!r} "
            f"from {len(values)} path argument(s)."
        )
    return path


def _file_preview(file: Path | None) -> dict[str, Any] | None:
    """Preview metadata for `--file`: name and size, without loading the bytes."""
    if file is None:
        return None
    return {"name": file.name, "size": file.stat().st_size}


def _multipart_files(file: Path) -> dict[str, Any]:
    """Read `file` once and shape it as Halo's `file` multipart form field."""
    content = file.read_bytes()
    content_type = mimetypes.guess_type(file.name)[0]
    if content_type:
        return {"file": (file.name, content, content_type)}
    return {"file": (file.name, content)}


def _load_data_argument(data: str | None) -> object:
    """Parse `--data` for a command (dict, list, or any other JSON value).

    A file path is read as-is; anything else must decode as JSON. A body that
    fails to decode is a pre-network validation failure: it renders through
    `render_error` like `raw`'s spec-validation refusals and exits 1 instead
    of raising a raw JSONDecodeError traceback (which is what the resource
    create/update paths did before this was unified).
    """
    if data is None:
        return None
    try:
        return _load_body(data)
    except (ValueError, OSError) as exc:
        render_error(
            {
                "ok": False,
                "category": "validation",
                "status_code": None,
                "error": f"Invalid --data: {exc}",
            }
        )
        raise typer.Exit(1) from exc


async def _run_operation(
    *,
    resource: HaloResource,
    op: ResourceOperation,
    path: str,
    params: dict[str, str],
    body: object | None,
    file: Path | None,
    save: Path | None,
    profile: str,
    output: str,
) -> None:
    """Execute one nested operation for real and render its result.

    Previews never reach here (they render from the command itself with zero
    network calls), so this helper always loads a profile and performs a request.
    Halo/transport failures surface as `HaloCLIError` and are rendered by `_run`,
    which exits 1. A `bytes` response is either written to `--save` or reported by
    length — raw bytes are never dumped into the JSON output.
    """
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        kwargs: dict[str, Any] = {"params": params}
        if body is not None:
            kwargs["json_body"] = body
        if op.multipart:
            assert file is not None  # guarded in _operation_command before this call
            kwargs["files"] = _multipart_files(file)
        result = await client.request(op.method, path, **kwargs)
    payload: dict[str, Any] = {
        "ok": True,
        "resource": resource.name,
        "operation": op.name,
        "method": op.method,
        "endpoint": path,
    }
    if isinstance(result, bytes):
        if save is not None:
            save.parent.mkdir(parents=True, exist_ok=True)
            save.write_bytes(result)
            payload["saved"] = str(save)
            payload["bytes"] = len(result)
        else:
            payload["binary"] = True
            payload["bytes"] = len(result)
            payload["hint"] = "pass --save PATH to write the bytes to a file"
    else:
        if op.write:
            payload["apply"] = True
        payload["result"] = normalize_halo_result(result)
    render(payload, output=output)


async def _raw(
    *,
    profile: str,
    output: str,
    method: str,
    path: str,
    params: dict[str, str],
    body: object,
    spec_warnings: list[str] | None = None,
) -> dict:
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        result = await client.raw(method, path, params=params, body=body)
    payload = {"ok": True, "body": normalize_halo_result(result)}
    if spec_warnings:
        payload["spec_warnings"] = spec_warnings
    render(payload, output=output)
    return payload


async def _todo_add(
    *,
    title: str,
    profile: str,
    output: str,
    description: str,
    owner: int | None,
    due: date | None,
    tags: list[str],
) -> None:
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        todo = await HaloTodoRepository(client).create(
            title=title,
            description=description,
            owner=owner,
            due=due,
            tags=tags,
        )
    render({"ok": True, "todo": normalize_halo_result(todo)}, output=output)


async def _todo_import_ms_apply(
    *,
    repository,
    profile: str,
    list_name: str | None,
    include_completed: bool,
    max_records: int | None,
    complete_source: bool,
    output: str,
) -> None:
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        results = await import_tasks(
            repository,
            HaloTodoRepository(client),
            list_name=list_name,
            include_completed=include_completed,
            max_records=max_records,
            complete_source=complete_source,
        )
    imported = [item for item in results if item.get("imported")]
    failed = [item for item in results if item.get("error")]
    completed = [item for item in results if item.get("source_completed")]
    render(
        {
            "source": "microsoft.todo",
            "apply": True,
            "complete_source": complete_source,
            "count": len(results),
            "imported_count": len(imported),
            "source_completed_count": len(completed),
            "failed_count": len(failed),
            "items": normalize_halo_result(results),
        },
        output=output,
    )


def _parse_params(values: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise typer.BadParameter(f"Invalid --param {value!r}; expected key=value.")
        key, item = value.split("=", 1)
        params[key] = item
    return params


def _normalize_auth_mode(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    allowed = {"client_credentials", "halo_interactive", "entra_broker"}
    if normalized not in allowed:
        raise typer.BadParameter(
            "Invalid auth mode. Expected client-credentials, halo-interactive, or entra-broker."
        )
    return normalized


def _refuse_unconfirmed_login(profile: str) -> None:
    raise typer.BadParameter(
        f"Interactive Halo auth is not confirmed for profile '{profile}'. "
        "Run 'halocli auth discover --tenant-url ...' first and keep using "
        "client-credentials until discovery reports interactive support."
    )


def _delete_token(profile: str) -> bool:
    deleted = False
    try:
        deleted = KeyringTokenCache().delete(profile) or deleted
    except Exception:
        pass
    return TokenCache(allow_file_cache=True).delete(profile) or deleted


def _load_body(value: str | None) -> object:
    if value is None:
        return None
    path = Path(value)
    if path.exists():
        value = path.read_text(encoding="utf-8")
    return json.loads(value)


def _run(coro):
    try:
        return asyncio.run(coro)
    except HaloCLIError as exc:
        render_error(
            {
                "ok": False,
                "category": exc.category,
                "status_code": exc.status_code,
                "error": str(exc),
                "diagnostic": diagnose_permission_failure(exc),
            }
        )
        raise typer.Exit(1) from exc
    except Exception as exc:
        err = classify_error(exc)
        render_error(
            {
                "ok": False,
                "category": err.category,
                "status_code": err.status_code,
                # A message-less exception (httpx.ReadTimeout()) must not render
                # as an empty string: fall back to the classified message.
                "error": str(exc) or str(err),
                "diagnostic": diagnose_permission_failure(err),
            }
        )
        raise typer.Exit(1) from exc
