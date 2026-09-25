from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import halocli.config as halocli_config
from conftest import REAL_CONFIG_FILE
from halocli.config import (
    ConfigOverrides,
    HaloProfile,
    load_config,
    load_profile,
    save_profile,
)


def test_env_values_override_profile_file(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "profiles": {
                    "default": {
                        "tenant_url": "https://profile.example.com",
                        "client_id": "profile-id",
                        "client_secret": "profile-secret",
                        "scope": "all",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HALO_TENANT_URL", "https://env.example.com")
    monkeypatch.setenv("HALO_CLIENT_ID", "env-id")
    monkeypatch.setenv("HALO_CLIENT_SECRET", "env-secret")
    monkeypatch.setenv("HALO_SCOPE", "read")

    profile = load_profile("default", config_file=config_file)

    assert profile.tenant_url == "https://env.example.com"
    assert profile.client_id == "env-id"
    assert profile.client_secret == "env-secret"
    assert profile.scope == "read"


def test_explicit_overrides_win_over_env_and_profile(monkeypatch, tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "profiles": {
                    "default": {
                        "tenant_url": "https://profile.example.com",
                        "client_id": "profile-id",
                        "client_secret": "profile-secret",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HALO_CLIENT_ID", "env-id")

    profile = load_profile(
        "default",
        config_file=config_file,
        overrides=ConfigOverrides(client_id="override-id"),
    )

    assert profile.client_id == "override-id"
    assert profile.tenant_url == "https://profile.example.com"


def test_profile_save_stays_inside_isolated_config_dir(tmp_path: Path) -> None:
    # Dedicated isolation guarantee: save_profile() with no explicit path must land
    # under this test's tmp_path, never at the developer's real config location.
    saved = save_profile(
        "isolation-probe",
        HaloProfile(
            tenant_url="https://isolation.example.com",
            client_id="probe-id",
            client_secret="probe-secret",
        ),
    )

    # save_profile() only writes to the path it returns, so a tmp_path result proves
    # the real config file was not touched by this call.
    assert saved == tmp_path / "config.yaml"
    assert saved != REAL_CONFIG_FILE
    assert saved.is_relative_to(tmp_path)

    written = load_config()
    assert set(written.profiles) == {"isolation-probe"}


def test_default_config_file_is_redirected_away_from_real_location(tmp_path: Path) -> None:
    # The autouse fixture patches halocli.config.default_config_file; access it via
    # the module so the patched attribute (not an import-time binding) is used.
    resolved = halocli_config.default_config_file()

    assert resolved == tmp_path / "config.yaml"
    assert resolved != REAL_CONFIG_FILE
    # The isolated default config starts empty: the real config's profiles (e.g. a
    # developer's discovered 'thomas' profile) are invisible to tests.
    assert load_config().profiles == {}


def test_ambient_halo_env_vars_are_cleared(monkeypatch) -> None:
    # The fixture strips ambient HALO_* vars; a test that never sets them must see
    # load_profile() fail on missing values rather than inherit the machine's secrets.
    monkeypatch.delenv("HALO_TENANT_URL", raising=False)
    monkeypatch.delenv("HALO_CLIENT_ID", raising=False)

    with pytest.raises(ValueError, match="Missing HaloCLI profile values"):
        load_profile("does-not-exist")
