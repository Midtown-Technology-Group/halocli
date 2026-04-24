from __future__ import annotations

import asyncio
import json
import time
import webbrowser
from pathlib import Path
from typing import Annotated

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
from halocli.token_cache import KeyringTokenCache, TokenCache
from halocli.utils import list_all, normalize_halo_result


app = typer.Typer(help="HaloPSA CLI for safe operator and automation workflows.")
auth_app = typer.Typer(help="Authentication helpers.")
app.add_typer(auth_app, name="auth")

RESOURCE_ALIASES = {
    "tickets": "tickets",
    "ticket": "tickets",
    "clients": "clients",
    "client": "clients",
    "agents": "agents",
    "agent": "agents",
    "teams": "teams",
    "team": "teams",
    "users": "users",
    "user": "users",
    "kb": "kb",
}


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


def _resource_command(resource: str):
    resource_app = typer.Typer(help=f"{resource.title()} commands.")

    @resource_app.command("list")
    def list_command(
        profile: Annotated[str, typer.Option("--profile")] = "default",
        output: Annotated[str, typer.Option("--output", "-o")] = "json",
        open_only: Annotated[bool, typer.Option("--open")] = False,
        page_size: Annotated[int, typer.Option("--page-size")] = 100,
        max_pages: Annotated[int | None, typer.Option("--max-pages")] = None,
        max_records: Annotated[int | None, typer.Option("--max-records")] = None,
        param: Annotated[list[str] | None, typer.Option("--param")] = None,
    ) -> None:
        _run(
            _list_resource(
                resource=RESOURCE_ALIASES[resource],
                profile=profile,
                output=output,
                open_only=open_only,
                page_size=page_size,
                max_pages=max_pages,
                max_records=max_records,
                params=_parse_params(param or []),
            )
        )

    return resource_app


for _resource in ("tickets", "clients", "agents", "teams", "users", "kb"):
    app.add_typer(_resource_command(_resource), name=_resource)


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
) -> None:
    method = method.upper()
    if method in {"POST", "PUT", "PATCH", "DELETE"} and not (apply and yes):
        raise typer.BadParameter(f"Refusing {method} {path} without --apply --yes.")
    body = _load_body(data)
    _run(
        _raw(
            profile=profile,
            output=output,
            method=method,
            path=path,
            params=_parse_params(param or []),
            body=body,
        )
    )


async def _auth_test(*, profile: str, output: str) -> None:
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        result = await client.test_auth()
    render({"ok": True, "profile": profile, "result": normalize_halo_result(result)}, output=output)


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
    resource: str,
    profile: str,
    output: str,
    open_only: bool,
    page_size: int,
    max_pages: int | None,
    max_records: int | None,
    params: dict[str, str],
) -> None:
    if resource == "tickets" and open_only:
        params["open_only"] = "true"
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        rows = await list_all(
            lambda **kwargs: client.list_resource(resource, **kwargs),
            page_size=page_size,
            max_pages=max_pages,
            max_records=max_records,
            **params,
        )
    render({"resource": resource, "count": len(rows), "items": rows}, output=output)


async def _raw(
    *,
    profile: str,
    output: str,
    method: str,
    path: str,
    params: dict[str, str],
    body: object,
) -> None:
    halo_profile = load_profile(profile)
    async with HaloClient(halo_profile, profile_name=profile) as client:
        result = await client.raw(method, path, params=params, body=body)
    render({"ok": True, "body": normalize_halo_result(result)}, output=output)


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


def _run(coro) -> None:
    try:
        asyncio.run(coro)
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
                "error": str(exc),
                "diagnostic": diagnose_permission_failure(err),
            }
        )
        raise typer.Exit(1) from exc
