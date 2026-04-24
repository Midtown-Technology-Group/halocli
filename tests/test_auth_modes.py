from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from halocli.auth import parse_callback_query
from halocli.config import HaloProfile, load_profile
from halocli.token_cache import FileTokenCacheDisabled, TokenCache


def test_profile_loads_auth_mode(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "profiles": {
                    "jack": {
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

    profile = load_profile("jack", config_file=config_file)

    assert profile.auth_mode == "halo_interactive"


def test_interactive_profile_does_not_require_client_secret(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "profiles": {
                    "jack": {
                        "tenant_url": "https://halo.example.com",
                        "client_id": "id",
                        "auth_mode": "halo_interactive",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile = load_profile("jack", config_file=config_file)

    assert profile.client_secret is None


def test_callback_parser_rejects_state_mismatch() -> None:
    with pytest.raises(ValueError, match="state"):
        parse_callback_query("code=abc&state=wrong", expected_state="expected")


def test_callback_parser_returns_code() -> None:
    assert parse_callback_query("code=abc&state=expected", expected_state="expected") == "abc"


def test_file_token_cache_refuses_without_explicit_allowance(tmp_path: Path) -> None:
    cache = TokenCache(HaloProfile(tenant_url="https://halo.example.com", client_id="id"), tmp_path)

    with pytest.raises(FileTokenCacheDisabled):
        cache.save("jack", {"access_token": "abc"})
