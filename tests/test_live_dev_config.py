"""Unit tests for live-dev harness profile/env resolution.

No tenant contact: these exercise _active_mode / dev_profile /
_refuse_prod with monkeypatched inputs. They live in their own file
because test_live_dev's module-level pytestmark would skip them in CI
exactly where they matter most.
"""

from __future__ import annotations

import pytest

import test_live_dev as lvd


def _clear(monkeypatch) -> None:
    for key in (*lvd.ENV_KEYS, lvd.PROFILE_ENV):
        monkeypatch.delenv(key, raising=False)


def _set_env_trio(monkeypatch, url: str = "https://clidev.trial.usehalo.com") -> None:
    monkeypatch.setenv("HALO_DEV_TENANT_URL", url)
    monkeypatch.setenv("HALO_DEV_CLIENT_ID", "id")
    monkeypatch.setenv("HALO_DEV_CLIENT_SECRET", "secret")


def test_no_config_means_inactive(monkeypatch) -> None:
    _clear(monkeypatch)
    assert lvd._active_mode() is None


def test_env_trio_activates_env_mode(monkeypatch) -> None:
    _clear(monkeypatch)
    _set_env_trio(monkeypatch)
    assert lvd._active_mode() == "env"


def test_partial_env_stays_inactive(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("HALO_DEV_TENANT_URL", "https://x.example.com")
    assert lvd._active_mode() is None  # id/secret missing


def test_profile_mode_wins_when_both_configured(monkeypatch) -> None:
    _clear(monkeypatch)
    _set_env_trio(monkeypatch)
    monkeypatch.setenv(lvd.PROFILE_ENV, "dev")
    assert lvd._active_mode() == "profile"


def test_profile_mode_loads_the_named_native_profile(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(lvd.PROFILE_ENV, "dev")
    seen: list[str] = []

    def fake_load(name: str) -> lvd.HaloProfile:
        seen.append(name)
        return lvd.HaloProfile(
            tenant_url="https://clidev.trial.usehalo.com",
            client_id="app-id",
            auth_mode="halo_interactive",
        )

    monkeypatch.setattr(lvd, "load_profile", fake_load)
    profile, name = lvd.dev_profile()

    assert seen == ["dev"]
    assert name == "dev"  # token-cache key follows the profile name
    assert profile.auth_mode == "halo_interactive"  # native default, secretless
    assert profile.client_secret is None


def test_profile_mode_refuses_production(monkeypatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv(lvd.PROFILE_ENV, "prod-like")
    monkeypatch.setattr(
        lvd,
        "load_profile",
        lambda name: lvd.HaloProfile(
            tenant_url="https://midtowntg.halopsa.com", client_id="app-id"
        ),
    )

    with pytest.raises(AssertionError, match="PRODUCTION"):
        lvd.dev_profile()


def test_env_mode_refuses_production(monkeypatch) -> None:
    _clear(monkeypatch)
    _set_env_trio(monkeypatch, url="https://midtowntg.halopsa.com/")

    with pytest.raises(AssertionError, match="PRODUCTION"):
        lvd.dev_profile()


def test_env_mode_builds_client_credentials_profile(monkeypatch) -> None:
    _clear(monkeypatch)
    _set_env_trio(monkeypatch)

    profile, name = lvd.dev_profile()

    assert profile.auth_mode == "client_credentials"
    assert profile.client_secret == "secret"
    assert name == "dev-verification"


def test_live_module_opts_out_of_isolation() -> None:
    """The live module must carry the real_state marker: profile mode needs
    the real config + real credential store, which the autouse isolation
    fixture otherwise redirects to tmp (this exact failure sent the first
    campaign run looking for 'configured: none')."""
    marks = lvd.pytestmark
    if not isinstance(marks, list):
        marks = [marks]
    names = {m.name for m in marks}
    assert "real_state" in names
    assert "skipif" in names


def test_skip_isolation_predicate_only_for_marked_nodes() -> None:
    from conftest import skip_isolation

    class _Node:
        def __init__(self, marker: object | None) -> None:
            self._marker = marker

        def get_closest_marker(self, name: str):  # noqa: ANN001
            return self._marker if name == "real_state" else None

    sentinel = object()
    assert skip_isolation(_Node(sentinel)) is True
    assert skip_isolation(_Node(None)) is False
