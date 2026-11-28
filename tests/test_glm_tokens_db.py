from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from freeproxy import main as main_module
from freeproxy.config import ProviderConfig, ProxyConfig, Settings
from freeproxy.native import glm as glm_module
from freeproxy.native.glm import create_glm_app
from freeproxy.native.glm_captcha import solver as solver_module
from freeproxy.native.glm_captcha.solver import (
    GlmCaptchaSolver,
    _token_db_path,
    generate_captcha_param,
    take_device_token,
)

from .conftest import gateway, make_settings


@pytest.fixture(autouse=True)
def _clean_glm_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FP_GLM_TOKENS_DB", raising=False)
    yield


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "api_keys": "test-key-0123456789abcdef",
    }
    values.update(overrides)
    return Settings(**values)


def _token_db(path: Path, *tokens: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE tokens (id INTEGER PRIMARY KEY, token TEXT)")
        connection.executemany("INSERT INTO tokens (token) VALUES (?)", [(t,) for t in tokens])
        connection.commit()
    finally:
        connection.close()
    return path


# --- Settings ---------------------------------------------------------------


def test_settings_glm_tokens_db_unset_is_none() -> None:
    assert _settings().glm_tokens_db is None


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_settings_glm_tokens_db_blank_is_none(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FP_GLM_TOKENS_DB", value)
    assert _settings().glm_tokens_db is None


def test_settings_glm_tokens_db_path_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_GLM_TOKENS_DB", "  data/glm-tokens.sqlite  ")
    assert _settings().glm_tokens_db == Path("data/glm-tokens.sqlite")


def test_settings_glm_tokens_db_reads_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("FP_GLM_TOKENS_DB=harvested/tokens.sqlite\n", encoding="utf-8")
    assert _settings(_env_file=env_file).glm_tokens_db == Path("harvested/tokens.sqlite")


# --- _token_db_path precedence ---------------------------------------------


def test_token_db_explicit_existing_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = _token_db(tmp_path / "explicit.sqlite", "a")
    env_db = _token_db(tmp_path / "env.sqlite", "b")
    monkeypatch.setenv("FP_GLM_TOKENS_DB", str(env_db))
    assert _token_db_path(explicit) == explicit


def test_token_db_missing_explicit_falls_back_to_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_db = _token_db(tmp_path / "env.sqlite", "b")
    monkeypatch.setenv("FP_GLM_TOKENS_DB", str(env_db))
    assert _token_db_path(tmp_path / "missing.sqlite") == env_db


def test_token_db_env_fallback_when_explicit_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_db = _token_db(tmp_path / "env.sqlite", "b")
    monkeypatch.setenv("FP_GLM_TOKENS_DB", str(env_db))
    assert _token_db_path(None) == env_db


def test_token_db_falls_back_to_default_then_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_db = _token_db(tmp_path / "legacy" / "tokens.sqlite", "d")
    monkeypatch.setattr(solver_module, "DEFAULT_TOKEN_DB", str(default_db))
    assert _token_db_path(tmp_path / "missing.sqlite") == default_db

    default_db.unlink()
    secrets_db = _token_db(tmp_path / "secrets" / "glm-tokens.sqlite", "s")
    resolved = _token_db_path(tmp_path / "missing.sqlite")
    assert resolved == Path("secrets/glm-tokens.sqlite")
    assert resolved is not None and resolved.resolve() == secrets_db


# --- solver consumes the explicit DB ----------------------------------------


def test_explicit_tokens_db_is_consumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = _token_db(tmp_path / "explicit.sqlite", "tok-explicit", "tok-solver")
    monkeypatch.setenv("FP_ALIYUN_CAPTCHA_ACCESS_KEY", "test-access")
    monkeypatch.setenv("FP_ALIYUN_CAPTCHA_SECRET_KEY", "test-secret")
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        solver_module,
        "_captcha_from_device_token",
        lambda client, token, access_key, secret_key: (
            seen.append((token, access_key, secret_key)) or "db-param"
        ),
    )
    assert generate_captcha_param(tokens_db=explicit) == "db-param"
    assert seen == [("tok-explicit", "test-access", "test-secret")]

    solver = GlmCaptchaSolver(tokens_db=explicit)
    assert solver.generate() == "db-param"
    assert seen[-1][0] == "tok-solver"
    assert take_device_token(explicit) is None


# --- wiring -----------------------------------------------------------------


def test_create_glm_app_passes_tokens_db_to_solver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []

    class FakeSolver:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)

    monkeypatch.setattr(glm_module, "GlmCaptchaSolver", FakeSolver)
    tokens_db = _token_db(tmp_path / "tokens.sqlite", "tok")

    create_glm_app(
        tmp_path / "tokens.json",
        tokens_db=tokens_db,
        captcha_access_key="access",
        captcha_secret_key="secret",
    )

    assert captured[0]["tokens_db"] == tokens_db
    assert captured[0]["access_key"] == "access"
    assert captured[0]["secret_key"] == "secret"


async def test_gateway_passes_glm_tokens_db_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens_db = Path("harvested/glm-tokens.sqlite")
    captured: list[dict[str, object]] = []

    def fake_create_glm_app(store_path: object, **kwargs: object) -> FastAPI:
        captured.append(kwargs)
        return FastAPI()

    monkeypatch.setattr(main_module, "create_glm_app", fake_create_glm_app)
    provider = ProviderConfig(
        id="glm",
        name="GLM",
        base_url="http://glm.test/v1",
        auth_mode="none",
        models=["glm-4.7"],
        default_model="glm-4.7",
        native="glm",
    )
    config = ProxyConfig(providers=[provider], aliases={"auto": ["glm/glm-4.7"]})
    settings = make_settings(glm_tokens_db=tokens_db)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with gateway(settings, config, handler):
        pass

    assert captured[0]["tokens_db"] == tokens_db
