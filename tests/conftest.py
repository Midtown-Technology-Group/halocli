from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any

import pytest

import halocli.auth as halocli_auth
import halocli.cli as halocli_cli
import halocli.config as halocli_config
import halocli.token_cache as halocli_token_cache
import platformdirs

# Environment variables that halocli.config.load_profile() reads on top of the profile
# file. Ambient values on a developer machine must never influence test behavior.
PROFILE_ENV_VARS = (
    "HALO_TENANT_URL",
    "HALO_CLIENT_ID",
    "HALO_CLIENT_SECRET",
    "HALO_SCOPE",
)

# The real per-user config location this fixture keeps tests away from.
REAL_CONFIG_FILE = (
    Path(platformdirs.user_config_dir(halocli_config.APP_NAME, appauthor=False)) / "config.yaml"
)


class ForbiddenTestSideEffect(BaseException):
    """Raised when a test reaches an interactive-login side effect.

    Derives from ``BaseException`` (not ``Exception``) on purpose: click's
    ``CliRunner`` only catches ``Exception`` and ``SystemExit``, so this error
    escapes ``runner.invoke()`` and fails the test loudly instead of being
    silently swallowed into a result object.
    """


def _fail_webbrowser_open(*args: Any, **kwargs: Any) -> bool:
    raise ForbiddenTestSideEffect(
        "webbrowser.open() was called during a test. The test suite must never open a "
        "real browser; the interactive Halo login flow is unreachable under test "
        "isolation (see tests/conftest.py). If a test needs the login flow, mock "
        "halocli.cli.webbrowser/wait_for_callback inside that test."
    )


def _fail_wait_for_callback(*args: Any, **kwargs: Any) -> str:
    raise ForbiddenTestSideEffect(
        "wait_for_callback() was called during a test: a test reached the OAuth login "
        "flow far enough to start the local HTTP callback server. Tests must never run "
        "the interactive Halo login flow (see tests/conftest.py)."
    )


class InMemoryKeyring:
    """Dict-backed stand-in for the ``keyring`` package.

    Installed by :func:`isolate_halocli_state` so token-cache code exercised in tests
    can never read from or write to the real Windows Credential Manager (or any other
    OS keyring backend).
    """

    is_halocli_test_double = True

    def __init__(self) -> None:
        self._secrets: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._secrets[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._secrets.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        self._secrets.pop((service_name, username), None)

    def get_keyring(self) -> InMemoryKeyring:
        return self


@pytest.fixture(autouse=True)
def isolate_halocli_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test away from real machine state.

    - ``halocli.config`` resolves its config file through
      ``default_config_file()``/``user_config_dir()``; both (plus the raw
      ``platformdirs`` entry points) are redirected into ``tmp_path`` so no test can
      read or write the real ``<user config>/halocli/config.yaml``.
    - ``halocli.token_cache``'s file-cache directory is redirected into ``tmp_path``.
    - Ambient ``HALO_*`` credential environment variables are removed.
    - ``keyring`` access goes to an in-memory fake, never the OS credential store.
    - ``webbrowser.open()`` and the OAuth ``wait_for_callback()`` HTTP server raise
      loudly (``ForbiddenTestSideEffect``) if any test reaches the interactive login.
    """
    config_file = tmp_path / "config.yaml"
    monkeypatch.setattr(halocli_config, "default_config_file", lambda: config_file)
    monkeypatch.setattr(
        halocli_config, "user_config_dir", lambda *args, **kwargs: str(tmp_path / "config")
    )
    monkeypatch.setattr(
        platformdirs, "user_config_dir", lambda *args, **kwargs: str(tmp_path / "config")
    )
    monkeypatch.setattr(
        halocli_token_cache, "user_cache_dir", lambda *args, **kwargs: str(tmp_path / "cache")
    )
    monkeypatch.setattr(
        platformdirs, "user_cache_dir", lambda *args, **kwargs: str(tmp_path / "cache")
    )

    for name in PROFILE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    # One shared instance per test so save/load/delete round-trip within a test while
    # staying isolated between tests.
    keyring_stub = InMemoryKeyring()
    monkeypatch.setattr(halocli_token_cache, "_load_keyring", lambda: keyring_stub)

    monkeypatch.setattr(webbrowser, "open", _fail_webbrowser_open)
    monkeypatch.setattr(halocli_auth, "wait_for_callback", _fail_wait_for_callback)
    monkeypatch.setattr(halocli_cli, "wait_for_callback", _fail_wait_for_callback)
