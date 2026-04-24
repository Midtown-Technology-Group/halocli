from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlparse
from urllib.parse import parse_qs

from halocli.config import HaloProfile


@dataclass(frozen=True)
class OAuthLoginRequest:
    authorization_url: str
    redirect_uri: str
    state: str
    code_verifier: str


def parse_callback_query(query: str, *, expected_state: str) -> str:
    params = parse_qs(query.lstrip("?"), keep_blank_values=True)
    actual_state = _first(params.get("state"))
    if actual_state != expected_state:
        raise ValueError("OAuth callback state mismatch.")
    error = _first(params.get("error"))
    if error:
        description = _first(params.get("error_description")) or error
        raise ValueError(f"OAuth callback returned an error: {description}")
    code = _first(params.get("code"))
    if not code:
        raise ValueError("OAuth callback did not include an authorization code.")
    return code


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0]


DEFAULT_CALLBACK_PORT = 8765


def build_login_request(profile: HaloProfile, *, port: int = DEFAULT_CALLBACK_PORT) -> OAuthLoginRequest:
    if not profile.authorization_endpoint:
        raise ValueError("Profile does not include a discovered authorization endpoint.")
    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _pkce_challenge(code_verifier)
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    query = urlencode(
        {
            "response_type": "code",
            "client_id": profile.client_id,
            "redirect_uri": redirect_uri,
            "scope": profile.scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return OAuthLoginRequest(
        authorization_url=f"{profile.authorization_endpoint}?{query}",
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )


def wait_for_callback(login_request: OAuthLoginRequest, *, timeout_seconds: int = 180) -> str:
    parsed = urlparse(login_request.redirect_uri)
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/callback"):
                try:
                    result["code"] = parse_callback_query(
                        urlparse(self.path).query,
                        expected_state=login_request.state,
                    )
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"HaloCLI login complete. You can close this tab.")
                except ValueError as exc:
                    result["error"] = str(exc)
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(f"HaloCLI login failed: {exc}".encode("utf-8"))
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = ThreadingHTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 0), CallbackHandler)
    server.timeout = timeout_seconds
    server.handle_request()
    server.server_close()
    if "error" in result:
        raise ValueError(result["error"])
    if "code" not in result:
        raise TimeoutError("Timed out waiting for the Halo OAuth browser callback.")
    return result["code"]


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
