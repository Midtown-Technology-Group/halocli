from __future__ import annotations

from types import SimpleNamespace

import halocli.token_cache as token_cache


class WindowsBackend:
    pass


WindowsBackend.__module__ = "keyring.backends.Windows"
WindowsBackend.__name__ = "WinVaultKeyring"


class MacBackend:
    pass


MacBackend.__module__ = "keyring.backends.macOS"
MacBackend.__name__ = "Keyring"


def test_secure_store_label_names_windows_credential_manager(monkeypatch) -> None:
    monkeypatch.setattr(
        token_cache,
        "_load_keyring",
        lambda: SimpleNamespace(get_keyring=lambda: WindowsBackend()),
    )
    monkeypatch.setattr(token_cache.platform, "system", lambda: "Windows")

    assert token_cache.describe_secure_store() == "windows-credential-manager-dpapi"


def test_secure_store_label_names_macos_keychain(monkeypatch) -> None:
    monkeypatch.setattr(
        token_cache,
        "_load_keyring",
        lambda: SimpleNamespace(get_keyring=lambda: MacBackend()),
    )
    monkeypatch.setattr(token_cache.platform, "system", lambda: "Darwin")

    assert token_cache.describe_secure_store() == "macos-keychain"
