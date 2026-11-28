from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from freeproxy.auth.paths import AuthPaths
from freeproxy.config import AUTH_STORE_ENV_NAMES, Settings

DEFAULT_STORE = Path("secrets/tokens.json")


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Keep the repository .env out of these assertions and start from a clean
    # set of auth-store variables.
    monkeypatch.chdir(tmp_path)
    for name in AUTH_STORE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "api_keys": "test-key-0123456789abcdef",
    }
    values.update(overrides)
    return Settings(**values)


def test_settings_auth_store_path_defaults_when_unset() -> None:
    assert _settings().auth_store_path == DEFAULT_STORE


def test_settings_reads_legacy_auth_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_AUTH_STORE", "legacy/tokens.json")
    assert _settings().auth_store_path == Path("legacy/tokens.json")


def test_settings_reads_canonical_auth_store_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_AUTH_STORE_PATH", "canonical/tokens.json")
    assert _settings().auth_store_path == Path("canonical/tokens.json")


def test_settings_prefers_canonical_auth_store_path_when_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FP_AUTH_STORE", "legacy/tokens.json")
    monkeypatch.setenv("FP_AUTH_STORE_PATH", "canonical/tokens.json")
    assert _settings().auth_store_path == Path("canonical/tokens.json")


def test_settings_explicit_auth_store_path_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_AUTH_STORE_PATH", "canonical/tokens.json")
    assert _settings(auth_store_path="explicit.json").auth_store_path == Path("explicit.json")


def test_settings_reads_legacy_auth_store_from_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FP_AUTH_STORE=dotenv/tokens.json\n", encoding="utf-8")
    assert _settings(_env_file=env_file).auth_store_path == Path("dotenv/tokens.json")


def test_cli_auth_paths_read_legacy_store_from_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("FP_AUTH_STORE=dotenv/tokens.json\n", encoding="utf-8")
    assert AuthPaths.from_env().store == Path("dotenv/tokens.json")


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, DEFAULT_STORE),
        ({"FP_AUTH_STORE": "legacy/tokens.json"}, Path("legacy/tokens.json")),
        ({"FP_AUTH_STORE_PATH": "canonical/tokens.json"}, Path("canonical/tokens.json")),
        (
            {"FP_AUTH_STORE": "legacy/tokens.json", "FP_AUTH_STORE_PATH": "canonical/tokens.json"},
            Path("canonical/tokens.json"),
        ),
    ],
)
def test_cli_auth_paths_match_settings(
    env: dict[str, str],
    expected: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert _settings().auth_store_path == AuthPaths.from_env().store == expected


def test_cli_auth_paths_explicit_store_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_AUTH_STORE", "legacy/tokens.json")
    monkeypatch.setenv("FP_AUTH_STORE_PATH", "canonical/tokens.json")
    assert AuthPaths.from_env(store="explicit.json").store == Path("explicit.json")
