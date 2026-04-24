from __future__ import annotations

from pathlib import Path

import yaml

from halocli.config import ConfigOverrides, load_profile


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
