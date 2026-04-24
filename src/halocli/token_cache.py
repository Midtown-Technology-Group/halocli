from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from platformdirs import user_cache_dir

from halocli.config import APP_NAME, HaloProfile


class FileTokenCacheDisabled(RuntimeError):
    pass


class TokenCache:
    def __init__(
        self,
        profile: HaloProfile | None = None,
        cache_dir: Path | None = None,
        *,
        allow_file_cache: bool = False,
    ) -> None:
        self.profile = profile
        self.cache_dir = cache_dir or Path(user_cache_dir(APP_NAME, appauthor=False)) / "tokens"
        self.allow_file_cache = allow_file_cache

    def save(self, profile_name: str, token_data: dict[str, Any]) -> Path:
        if not self.allow_file_cache:
            raise FileTokenCacheDisabled(
                "Refusing to store interactive tokens in a file cache without "
                "--allow-file-token-cache."
            )
        path = self._profile_path(profile_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(token_data, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def load(self, profile_name: str) -> dict[str, Any] | None:
        path = self._profile_path(profile_name)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def delete(self, profile_name: str) -> bool:
        path = self._profile_path(profile_name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def _profile_path(self, profile_name: str) -> Path:
        safe_name = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in profile_name)
        return self.cache_dir / f"{safe_name}.json"


class KeyringTokenCache:
    service_name = APP_NAME

    def save(self, profile_name: str, token_data: dict[str, Any]) -> None:
        keyring = _load_keyring()
        keyring.set_password(self.service_name, profile_name, json.dumps(token_data))

    def load(self, profile_name: str) -> dict[str, Any] | None:
        keyring = _load_keyring()
        payload = keyring.get_password(self.service_name, profile_name)
        if not payload:
            return None
        return json.loads(payload)

    def delete(self, profile_name: str) -> bool:
        keyring = _load_keyring()
        if not keyring.get_password(self.service_name, profile_name):
            return False
        keyring.delete_password(self.service_name, profile_name)
        return True


def _load_keyring():
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - dependency should normally exist
        raise RuntimeError(
            "The keyring package is not installed. Install halocli with dependencies or use "
            "--allow-file-token-cache."
        ) from exc
    return keyring
