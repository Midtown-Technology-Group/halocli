from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from platformdirs import user_config_dir
from pydantic import BaseModel, Field


APP_NAME = "halocli"
DEFAULT_PROFILE = "default"


class HaloProfile(BaseModel):
    tenant_url: str
    client_id: str
    client_secret: str | None = None
    scope: str = "all"
    auth_mode: Literal["client_credentials", "halo_interactive", "entra_broker"] = "client_credentials"
    timeout: float = 30.0
    max_retries: int = 3
    interactive_discovered: bool = False
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None

    @property
    def api_base_url(self) -> str:
        base = self.tenant_url.rstrip("/")
        return base if base.lower().endswith("/api") else f"{base}/api"

    @property
    def auth_token_url(self) -> str:
        base = self.tenant_url.rstrip("/")
        if base.lower().endswith("/api"):
            base = base[:-4]
        return f"{base}/auth/token"


@dataclass(frozen=True)
class ConfigOverrides:
    tenant_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None
    auth_mode: str | None = None


class HaloCLIConfig(BaseModel):
    profiles: dict[str, HaloProfile] = Field(default_factory=dict)


def default_config_file() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "config.yaml"


def load_config(config_file: Path | None = None) -> HaloCLIConfig:
    path = config_file or default_config_file()
    if not path.exists():
        return HaloCLIConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return HaloCLIConfig.model_validate(data)


def save_profile(profile_name: str, profile: HaloProfile, config_file: Path | None = None) -> Path:
    path = config_file or default_config_file()
    config = load_config(path)
    config.profiles[profile_name] = profile
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config.model_dump(), sort_keys=True), encoding="utf-8")
    return path


def update_profile(profile_name: str, updates: dict[str, object], config_file: Path | None = None) -> HaloProfile:
    path = config_file or default_config_file()
    config = load_config(path)
    if profile_name not in config.profiles:
        raise ValueError(f"Profile '{profile_name}' does not exist. Run 'halocli configure' first.")
    values = config.profiles[profile_name].model_dump()
    values.update({key: value for key, value in updates.items() if value is not None})
    profile = HaloProfile.model_validate(values)
    save_profile(profile_name, profile, path)
    return profile


def load_profile(
    profile_name: str = DEFAULT_PROFILE,
    *,
    config_file: Path | None = None,
    overrides: ConfigOverrides | None = None,
) -> HaloProfile:
    config = load_config(config_file)
    values = (
        config.profiles.get(profile_name).model_dump()
        if profile_name in config.profiles
        else {}
    )

    env_values = {
        "tenant_url": os.environ.get("HALO_TENANT_URL"),
        "client_id": os.environ.get("HALO_CLIENT_ID"),
        "client_secret": os.environ.get("HALO_CLIENT_SECRET"),
        "scope": os.environ.get("HALO_SCOPE"),
    }
    for key, value in env_values.items():
        if value not in (None, ""):
            values[key] = value

    if overrides is not None:
        for key, value in overrides.__dict__.items():
            if value not in (None, ""):
                values[key] = value

    missing = [key for key in ("tenant_url", "client_id") if not values.get(key)]
    if missing:
        raise ValueError(
            f"Missing HaloCLI profile values: {', '.join(missing)}. "
            "Run 'halocli configure' or set HALO_TENANT_URL and HALO_CLIENT_ID."
        )
    auth_mode = values.get("auth_mode") or "client_credentials"
    if auth_mode == "client_credentials" and not values.get("client_secret"):
        raise ValueError(
            "Missing Halo client secret. Set HALO_CLIENT_SECRET or run "
            "'halocli configure --client-secret ...'."
        )
    return HaloProfile.model_validate(values)
