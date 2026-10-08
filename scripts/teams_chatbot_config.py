#!/usr/bin/env python3
"""Configure the Microsoft Teams end-user chatbot (usehalo.com/guides/1080).

Guide: "Communicating in Teams using Chatbots > End-User Chat (Chatbot)".
The Halo side of that section is three Control fields (chat profile,
welcome message, help text) plus the optional manifest generator.

    # read current state + candidate chat profiles -> evidence
    python scripts/teams_chatbot_config.py --profile dev

    # apply (trial only): bind profile + messages, full-object save
    python scripts/teams_chatbot_config.py --profile dev --apply \
        --chat-profile <id> [--welcome TEXT] [--help-text TEXT]

Safety: refuses the production host; --apply is a read-modify-write of the
FULL Control object (POST /Control takes the whole array) and verifies on
readback that ONLY the intended fields changed - any other drift triggers a
restore of the original object and a failed exit.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import io
import json
import struct
import sys
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from halocli.client import HaloClient  # noqa: E402
from halocli.config import load_profile  # noqa: E402

EVIDENCE_PATH = REPO_ROOT / "teams_chatbot_evidence.json"

# Endpoint literals (S1192): one definition each.
_EP_CONTROL = "/Control"
_EP_CONTROL_TEAMS = "/Control/Teams"

# The three Control fields behind the guide's End-User Chat general settings.
CHATBOT_FIELDS = (
    "teams_chat_profile",
    "teams_chat_welcome_message",
    "teams_chat_help_message",
)

# Fields the trial's Control endpoint rewrites on ANY save (observed
# 2026-10-07: type coercion and empty-string -> null). Recorded in evidence
# as server normalizations; they never trigger a restore.
KNOWN_SERVER_NORMALIZATIONS = frozenset(
    {
        "stripepaymentmethodoptions",
        "autogenerate_itemaccountsid",
        "world_clock_5_timezone",
        "world_clock_5_label",
        "trophy_agents",
        # re-ciphered by the server on EVERY Control save (prod observation
        # 2026-10-07: the ciphertext changes while the stored secret is
        # server-managed) - recorded as a server rewrite, never a restore
        "merakiapplicationsecret",
    }
)

# Context fields recorded (read-only) so the evidence shows the wider
# Teams integration state the guide's other sections talk about.
CONTEXT_FIELDS = (
    "teams_authorized",
    "show_chat_module",
    "teams_chat_management",
    "allow_live_chat_teams",
    "enableteamsmsg",
    "enableteamscall",
    "teamsbot_disabled",
)

DEFAULT_WELCOME = (
    "Welcome to the Halo service assistant! I can help you raise and track "
    "requests. Type help to see what you can do."
)
DEFAULT_HELP = (
    "Commands: Start a new conversation - begin a new conversation with me; "
    "End the conversation - close this conversation; help - show this message. "
    "If you would rather talk to a person, say: I'd like to speak to someone."
)


def _is_production_host(host: str) -> bool:
    return "midtowntg" in host


def refuse_production(host: str) -> None:
    """Live writes are refused for production without explicit authorization."""
    if _is_production_host(host):
        raise SystemExit(
            "refusing: profile points at PRODUCTION (pass --allow-production to override)"
        )


def unwrap_list(payload: Any) -> list[Any]:
    """Halo list endpoints return either a bare array or an envelope."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "value", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return [payload]
    return []


def chatbot_state(control: dict[str, Any]) -> dict[str, Any]:
    """Chatbot + context fields of one Control object (missing -> None)."""
    state = {name: control.get(name) for name in CHATBOT_FIELDS}
    state["_context"] = {name: control.get(name) for name in CONTEXT_FIELDS}
    return state


def apply_chatbot_fields(
    control: dict[str, Any], profile_id: str, welcome: str, help_text: str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Pure read-modify-write step: set the three fields on a copy.

    Returns (new_object, {field: {before, after}}) so callers can verify and
    evidence exactly what changed.
    """
    updated = copy.deepcopy(control)
    requested = {
        "teams_chat_profile": profile_id,
        "teams_chat_welcome_message": welcome,
        "teams_chat_help_message": help_text,
    }
    changed: dict[str, dict[str, Any]] = {}
    for field, value in requested.items():
        before = updated.get(field)
        if before != value:
            updated[field] = value
            changed[field] = {"before": before, "after": value}
    return updated, changed


def diff_other_fields(
    before: dict[str, Any],
    after: dict[str, Any],
    intended: object = (),
) -> dict[str, Any]:
    """Fields that differ outside the intended write set (drift check)."""
    skip = set(CHATBOT_FIELDS) | set(intended)  # type: ignore[arg-type]
    drift: dict[str, Any] = {}
    for key in set(before) | set(after):
        if key in skip or key in KNOWN_SERVER_NORMALIZATIONS:
            continue
        if before.get(key) != after.get(key):
            drift[key] = {"before": before.get(key), "after": after.get(key)}
    return drift


def server_normalizations(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """The known server-side rewrites (evidence only, never a restore)."""
    return {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in KNOWN_SERVER_NORMALIZATIONS
        if before.get(key) != after.get(key)
    }


async def read_state(client: HaloClient) -> dict[str, Any]:
    """Read-only snapshot: Teams tab, Control chatbot fields, chat profiles."""
    out: dict[str, Any] = {}
    try:
        out["teams_tab"] = await client.request("GET", _EP_CONTROL_TEAMS, timeout=60)
    except Exception as exc:  # noqa: BLE001 - tab may be absent pre-enablement
        out["teams_tab_error"] = f"{type(exc).__name__}: {exc}"[:300]

    controls = unwrap_list(await client.request("GET", _EP_CONTROL, timeout=60))
    control = next(
        (c for c in controls if isinstance(c, dict) and "teams_chat_profile" in c),
        controls[0] if controls else None,
    )
    if not isinstance(control, dict):
        raise SystemExit("GET /Control returned no Control object")
    out["chatbot"] = chatbot_state(control)

    profiles = unwrap_list(await client.request("GET", "/ChatProfile", timeout=60))
    out["chat_profiles"] = [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "access_type": p.get("access_type"),
            "access_control_level": p.get("access_control_level"),
            "access_control_count": len(p.get("access_control") or []),
            "validate_lifetime": p.get("validate_lifetime"),
            "anon_token_method": p.get("anon_token_method"),
            "cors_whitelist_count": len(p.get("cors_whitelist_list") or []),
            "has_tenant_id": bool(p.get("_tenantid")),
            # presence only: never record the secret itself
            "has_hmac_secret": bool(p.get("new_hmac_secret")),
            "logticket": p.get("logticket"),
            "tickettype_name": p.get("tickettype_name"),
            "in_use": p.get("in_use"),
            "chat_available": p.get("_chat_available"),
            "chat_mode": p.get("_chat_mode"),
        }
        for p in profiles
        if isinstance(p, dict)
    ]
    return out


async def apply_state(
    client: HaloClient,
    profile_id: str,
    welcome: str,
    help_text: str,
    only: str = "all",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full-object save, one field at a time, with per-field readback.

    Sequential field saves give exact persistence evidence: Halo accepted
    some fields silently before, so each write is proven on its own before
    the next one is attempted. Restore-on-drift stays armed throughout.
    """
    controls = unwrap_list(await client.request("GET", _EP_CONTROL, timeout=60))
    original = next(
        (c for c in controls if isinstance(c, dict) and "teams_chat_profile" in c),
        controls[0] if controls else None,
    )
    if not isinstance(original, dict):
        raise SystemExit("GET /Control returned no Control object")

    requested = {
        "teams_chat_profile": profile_id,
        "teams_chat_welcome_message": welcome,
        "teams_chat_help_message": help_text,
    }
    key_by_choice = {
        "profile": "teams_chat_profile",
        "welcome": "teams_chat_welcome_message",
        "help": "teams_chat_help_message",
    }
    if only != "all":
        requested = {key_by_choice[only]: requested[key_by_choice[only]]}
    if extra:
        requested.update(extra)

    current = copy.deepcopy(original)
    persistence: dict[str, Any] = {}
    for field, value in requested.items():
        candidate = copy.deepcopy(current)
        candidate[field] = value
        await client.request("POST", _EP_CONTROL, json_body=[candidate], timeout=120)
        readback_controls = unwrap_list(await client.request("GET", _EP_CONTROL, timeout=60))
        readback = next(
            (c for c in readback_controls if isinstance(c, dict) and "teams_chat_profile" in c),
            None,
        )
        if not isinstance(readback, dict):
            raise SystemExit("readback: Control object not found after save")
        persistence[field] = {
            "requested": value,
            "readback": readback.get(field),
            "persisted": readback.get(field) == value,
        }
        current = readback

    result: dict[str, Any] = {
        "persistence": persistence,
        "readback_chatbot": chatbot_state(current),
        "server_normalizations": server_normalizations(original, current),
        "drift": diff_other_fields(original, current, set(requested)),
    }
    result["not_persisted"] = [
        field for field, entry in persistence.items() if not entry["persisted"]
    ]

    if result["drift"]:
        # restore the exact original object and report failure
        try:
            await client.request("POST", _EP_CONTROL, json_body=[original], timeout=120)
            result["restored"] = True
        except Exception as exc:  # noqa: BLE001
            result["restore_error"] = f"{type(exc).__name__}: {exc}"[:300]
    return result


async def tab_post_state(
    client: HaloClient, profile_id: str, welcome: str, help_text: str
) -> dict[str, Any]:
    """Experimental write path: POST the Teams tab object with chatbot fields.

    The guide's General settings live on the Teams integration page; the
    spec only lists GET /Control/Teams, but trimmed specs hide write verbs.
    The full GET object is sent back plus the three chatbot fields so a
    full-replace semantics cannot lose tab state.
    """
    result: dict[str, Any] = {}
    tab = await client.request("GET", _EP_CONTROL_TEAMS, timeout=60)
    if not isinstance(tab, dict):
        raise SystemExit("GET /Control/Teams returned no object")
    payload = dict(tab)
    payload["teams_chat_profile"] = profile_id
    payload["teams_chat_welcome_message"] = welcome
    payload["teams_chat_help_message"] = help_text
    try:
        await client.request("POST", _EP_CONTROL_TEAMS, json_body=payload, timeout=60)
        result["post"] = "accepted"
    except Exception as exc:  # noqa: BLE001
        result["post"] = f"{type(exc).__name__}: {exc}"[:300]

    readback = unwrap_list(await client.request("GET", _EP_CONTROL, timeout=60))
    control = next(
        (c for c in readback if isinstance(c, dict) and "teams_chat_profile" in c),
        None,
    )
    result["readback_chatbot"] = chatbot_state(control) if isinstance(control, dict) else None
    return result


def _png(width: int, height: int, pixel: bytes, border: int = 0) -> bytes:
    """Minimal RGBA PNG (stdlib only) - placeholder Teams icons."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter: none
        for x in range(width):
            on_border = border and (
                x < border or y < border or x >= width - border or y >= height - border
            )
            rows += b"\xff\xff\xff\xff" if on_border else pixel
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(rows), 9)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _placeholder_icons(hex_color: str) -> tuple[bytes, bytes]:
    """192x192 color + 32x32 outline placeholder icons (Teams' requirements)."""
    h = hex_color.lstrip("#")
    try:
        rgb = bytes(int(h[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        rgb = bytes((0, 120, 212))
    pixel = rgb + b"\xff"
    return _png(192, 192, pixel), _png(32, 32, pixel, border=6)


def _ensure_icons(data: bytes, hex_color: str) -> tuple[bytes, list[str]]:
    """Guarantee the manifest zip carries the icon files it declares."""
    with zipfile.ZipFile(io.BytesIO(data)) as src:
        names = src.namelist()
        manifest = json.loads(src.read("manifest.json"))
        declared = [manifest["icons"]["color"], manifest["icons"]["outline"]]
        missing = [n for n in declared if n not in names]
        if not missing:
            return data, []
        color_png, outline_png = _placeholder_icons(hex_color)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for name in names:
                out.writestr(name, src.read(name))
            for name in declared:
                if name in missing:
                    out.writestr(name, color_png if "color" in name else outline_png)
        return buf.getvalue(), missing


async def generate_manifest(
    client: HaloClient, args: argparse.Namespace, out_path: Path
) -> dict[str, Any]:
    """POST the manifest generator (guide Fig 26-27) and record the artifact.

    Teams rejects packages without their declared icons. The generator only
    embeds images when they arrive as data URIs (the UI's format - plain hex
    or bare base64 yields an icon-less zip, both verified live), so generated
    placeholder icons are sent as data URIs and _ensure_icons still guarantees
    the zip contents as a fallback.
    """
    color_png, outline_png = _placeholder_icons(args.manifest_icon_color)
    body = {
        "name": args.manifest_name,
        "shortDescription": args.manifest_short,
        "longDescription": args.manifest_long,
        "iconColor": "data:image/png;base64," + base64.b64encode(color_png).decode(),
        "iconOutline": "data:image/png;base64," + base64.b64encode(outline_png).decode(),
    }
    raw = await client.request(
        "POST",
        "/IntegrationData/MicrosoftTeams/Manifest",
        json_body=body,
        timeout=60,
        as_bytes=True,
    )
    result: dict[str, Any] = {
        # metadata only: evidence must not carry multi-KB data URIs
        "requested": {
            "name": args.manifest_name,
            "shortDescription": args.manifest_short,
            "longDescription": args.manifest_long,
            "icon_color": args.manifest_icon_color,
            "icons": "data-URI placeholder PNGs (192 color / 32 outline)",
        }
    }
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
        result["looks_like_zip"] = data[:2] == b"PK"
        if data[:2] == b"PK":
            data, injected = _ensure_icons(data, args.manifest_icon_color)
            result["icons_injected"] = injected
            out_path.write_bytes(data)
            result["artifact"] = str(out_path)
            result["bytes"] = len(data)
            result["sha256"] = hashlib.sha256(data).hexdigest()
            with zipfile.ZipFile(out_path) as zf:
                result["entries"] = zf.namelist()
        else:
            result["bytes"] = len(data)
            result["sha256"] = hashlib.sha256(data).hexdigest()
            try:
                result["json"] = json.loads(data.decode("utf-8-sig"))
            except ValueError:
                result["head"] = data[:200].decode("utf-8", "replace")
    else:
        result["json"] = raw
    return result


async def run(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    host = profile.tenant_url.split("//", 1)[-1].split("/", 1)[0]
    if not args.allow_production:
        refuse_production(host)
    else:
        print(f"WARNING: --allow-production set for {host}", file=sys.stderr)

    evidence: dict[str, Any] = {
        "tool": "teams_chatbot_config",
        "guide": "https://www.usehalo.com/guides/1080",
        "profile": args.profile,
        "tenant": host,
        "production": _is_production_host(host),
        "mode": "apply" if (args.apply or args.tab_post) else "read",
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    async with HaloClient(profile, profile_name=args.profile) as client:
        evidence["read"] = await read_state(client)

        if args.apply:
            if not args.chat_profile:
                print(
                    "error: --apply requires --chat-profile (run the read phase "
                    "first and pick from chat_profiles)",
                    file=sys.stderr,
                )
                return 2
            extra: dict[str, Any] = {}
            for raw in args.set_fields:
                key, sep, value = raw.partition("=")
                if not sep or not key:
                    print(f"error: --set expects KEY=VALUE, got {raw!r}", file=sys.stderr)
                    return 2
                try:
                    extra[key] = json.loads(value)
                except ValueError:
                    extra[key] = value
            evidence["apply"] = await apply_state(
                client,
                args.chat_profile,
                args.welcome,
                args.help_text,
                args.only,
                extra,
            )

        if args.tab_post:
            if not args.chat_profile:
                print("error: --tab-post requires --chat-profile", file=sys.stderr)
                return 2
            evidence["tab_post"] = await tab_post_state(
                client, args.chat_profile, args.welcome, args.help_text
            )

        if args.manifest:
            manifest_out = REPO_ROOT / (
                "teams_chatbot_manifest_prod.zip"
                if _is_production_host(host)
                else "teams_chatbot_manifest.zip"
            )
            evidence["manifest"] = await generate_manifest(client, args, manifest_out)

    evidence_path = EVIDENCE_PATH
    if _is_production_host(host):
        evidence_path = EVIDENCE_PATH.with_name("teams_chatbot_evidence_prod.json")
    evidence_path.write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, default=str))
    print(f"\nevidence -> {evidence_path}", file=sys.stderr)

    apply_result = evidence.get("apply") or {}
    if apply_result.get("drift") or apply_result.get("not_persisted"):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="dev")
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="explicit authorization to write to the production host (midtowntg)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write to Control (production needs the override flag)"
    )
    parser.add_argument("--chat-profile", default=None, help="ChatProfile id to bind")
    parser.add_argument(
        "--only",
        choices=("all", "profile", "welcome", "help"),
        default="all",
        help="apply a single field (persistence diagnostics)",
    )
    parser.add_argument(
        "--set",
        dest="set_fields",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="narrow extra Control write (VALUE parsed as JSON when valid); repeatable",
    )
    parser.add_argument(
        "--tab-post",
        action="store_true",
        help="experimental: POST the Teams tab object with the chatbot fields",
    )
    parser.add_argument("--welcome", default=DEFAULT_WELCOME)
    parser.add_argument("--help-text", default=DEFAULT_HELP)
    parser.add_argument(
        "--manifest", action="store_true", help="generate the Teams app manifest (guide Fig 26)"
    )
    parser.add_argument("--manifest-name", default="Halo Service Chatbot")
    parser.add_argument(
        "--manifest-short",
        default="Raise and track requests with the Halo service assistant.",
    )
    parser.add_argument(
        "--manifest-long",
        default=(
            "Chat with the Halo service assistant to raise and track requests, "
            "start conversations, or ask to speak to a human agent."
        ),
    )
    parser.add_argument("--manifest-icon-color", default="0078D4")
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
