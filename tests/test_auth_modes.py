from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import InMemoryKeyring
from halocli import token_cache
from halocli.auth import parse_callback_query
from halocli.config import HaloProfile, load_profile
from halocli.token_cache import FileTokenCacheDisabled, KeyringTokenCache, TokenCache


def test_profile_loads_auth_mode(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "profiles": {
                    "thomas": {
                        "tenant_url": "https://halo.example.com",
                        "client_id": "id",
                        "client_secret": "secret",
                        "auth_mode": "halo_interactive",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile = load_profile("thomas", config_file=config_file)

    assert profile.auth_mode == "halo_interactive"


def test_interactive_profile_does_not_require_client_secret(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "profiles": {
                    "thomas": {
                        "tenant_url": "https://halo.example.com",
                        "client_id": "id",
                        "auth_mode": "halo_interactive",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile = load_profile("thomas", config_file=config_file)

    assert profile.client_secret is None


def test_callback_parser_rejects_state_mismatch() -> None:
    with pytest.raises(ValueError, match="state"):
        parse_callback_query("code=abc&state=wrong", expected_state="expected")


def test_callback_parser_returns_code() -> None:
    assert parse_callback_query("code=abc&state=expected", expected_state="expected") == "abc"


def test_file_token_cache_refuses_without_explicit_allowance(tmp_path: Path) -> None:
    cache = TokenCache(HaloProfile(tenant_url="https://halo.example.com", client_id="id"), tmp_path)

    with pytest.raises(FileTokenCacheDisabled):
        cache.save("thomas", {"access_token": "abc"})


def test_keyring_access_is_isolated_from_windows_credential_manager() -> None:
    # The conftest fixture replaces _load_keyring with an in-memory fake, so these
    # calls must never reach the real Windows Credential Manager (or any OS keyring).
    assert isinstance(token_cache._load_keyring(), InMemoryKeyring)

    secure_cache = KeyringTokenCache()
    secure_cache.save("isolation-probe", {"access_token": "abc"})
    assert secure_cache.load("isolation-probe") == {"access_token": "abc"}
    assert secure_cache.delete("isolation-probe") is True
    assert secure_cache.load("isolation-probe") is None
