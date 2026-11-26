from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from pydantic import ValidationError

from freeproxy.config import (
    ConfigurationError,
    ProviderConfig,
    ProxyConfig,
    Settings,
    load_proxy_config,
)
from freeproxy.errors import InvalidRequest, ModelNotFound, RateLimitExceeded
from freeproxy.providers.http import ProviderHTTP
from freeproxy.rate_limit import ClientLimiter, DailyQuota, SlidingWindowLimiter
from freeproxy.services.proxy import derive_session_id

from .conftest import gateway, make_config, make_provider, make_settings


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def wall_clock(self) -> float:
        return 1_700_000_000.0 + self.now


def test_sliding_window_limit_and_reset():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(
        2,
        60,
        monotonic=clock.monotonic,
        wall_clock=clock.wall_clock,
    )
    first = limiter.consume("client")
    second = limiter.consume("client")
    assert first.allowed and first.remaining == 1
    assert second.allowed and second.remaining == 0

    denied = limiter.consume("client")
    assert not denied.allowed
    assert denied.retry_after == 60

    clock.now = 30
    assert limiter.preview("client").retry_after == 30
    clock.now = 61
    assert limiter.consume("client").allowed


def test_daily_quota_resets_on_next_utc_day():
    current = [date(2026, 1, 1)]
    limiter = DailyQuota(
        2,
        wall_clock=lambda: 1_700_000_000.0,
        today=lambda: current[0],
    )
    assert limiter.consume("a").allowed
    assert limiter.consume("a").allowed
    assert not limiter.consume("a").allowed
    current[0] = current[0] + timedelta(days=1)
    assert limiter.consume("a").allowed


def test_client_limiter_does_not_consume_when_rejected():
    limiter = ClientLimiter(client_rpm=1, daily_requests=10, global_rpm=10)
    first = limiter.check("client")
    assert first.rpm.remaining == 0
    with pytest.raises(RateLimitExceeded) as exc_info:
        limiter.check("client")
    assert exc_info.value.scope == "client_rpm"
    assert exc_info.value.headers["X-RateLimit-Scope"] == "client_rpm"
    # The daily slot must not be consumed by the rejected request.
    assert limiter.daily.preview("client").remaining == 9


def test_default_provider_config_is_native_and_valid():
    for path in ("config/providers.json", "config/providers.docker.json"):
        config = load_proxy_config(path)
        assert {provider.id for provider in config.providers} == {
            "glm",
            "qwen",
            "deepseek",
        }
        assert {provider.native for provider in config.providers} == {
            "glm",
            "qwen",
            "deepseek",
        }
        assert all(provider.base_url.endswith(".native") for provider in config.providers)
        assert set(config.aliases) == {
            "auto",
            "fast",
            "coding",
            "reasoning",
            "vision",
        }
        for alias, targets in config.aliases.items():
            assert targets, alias
            for target in targets:
                provider_id = target.partition("/")[0]
                assert provider_id in config.provider_map


def test_env_expansion_and_missing_variable(tmp_path, monkeypatch):
    config_path = tmp_path / "providers.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "glm",
                        "name": "GLM",
                        "base_url": "http://127.0.0.1:3001/v1",
                        "api_key": "${TEST_UPSTREAM_KEY}",
                        "models": ["glm-4.7"],
                        "default_model": "glm-4.7",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_UPSTREAM_KEY", "top-secret-value")
    config = load_proxy_config(config_path)
    assert config.providers[0].api_key.get_secret_value() == "top-secret-value"

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "${TEST_UPSTREAM_KEY}", "${TEST_MISSING_KEY}"
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("TEST_MISSING_KEY", raising=False)
    with pytest.raises(ConfigurationError) as exc_info:
        load_proxy_config(config_path)
    assert "TEST_MISSING_KEY" in str(exc_info.value)


def test_required_provider_auth_fails_closed():
    with pytest.raises(ValidationError):
        ProviderConfig(
            id="glm",
            name="GLM",
            base_url="http://127.0.0.1:3001/v1",
            api_key="",
            auth_mode="bearer",
            auth_required=True,
            models=["glm-4.7"],
            default_model="glm-4.7",
        )
    optional = ProviderConfig(
        id="deepseek",
        name="DeepSeek",
        base_url="http://127.0.0.1:9655/v1",
        api_key="",
        auth_mode="bearer",
        auth_required=False,
        models=["deepseek-chat"],
        default_model="deepseek-chat",
    )
    assert optional.auth_required is False


def test_retry_after_http_date_is_supported():
    retry_at = datetime.now(UTC) + timedelta(seconds=60)
    response = httpx.Response(
        429,
        headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
    )
    seconds = ProviderHTTP.retry_after_seconds(response)
    assert seconds is not None
    assert 55 <= seconds <= 60


def test_namespace_and_alias_collisions_are_rejected():
    first = make_provider(
        "glm",
        base_url="http://glm.test/v1",
        prefix="shared",
        models=["glm-4.7"],
        default_model="glm-4.7",
        api_key="upstream-key",
    )
    second = make_provider(
        "shared",
        base_url="http://other.test/v1",
        prefix="other",
        models=["other-model"],
        default_model="other-model",
        auth_mode="none",
    )
    with pytest.raises(ValidationError):
        ProxyConfig(providers=[first, second], aliases={})

    third = make_provider(
        "glm",
        base_url="http://glm.test/v1",
        prefix="glm",
        models=["glm-4.7"],
        default_model="glm-4.7",
        api_key="upstream-key",
    )
    with pytest.raises(ValidationError):
        ProxyConfig(providers=[third], aliases={"glm": ["glm/glm-4.7"]})


def test_default_alias_validation():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            api_keys="key-0123456789abcdef",
            default_alias="not a valid alias",
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            api_keys="same-key-0123456789",
            admin_api_key="same-key-0123456789",
        )


def test_dotenv_values_expand_provider_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TEST_DOTENV_KEY", raising=False)
    config_path = tmp_path / "providers.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "glm",
                        "name": "GLM",
                        "base_url": "http://127.0.0.1:3001/v1",
                        "api_key": "${TEST_DOTENV_KEY}",
                        "models": ["glm-4.7"],
                        "default_model": "glm-4.7",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("TEST_DOTENV_KEY=from-dotenv\n", encoding="utf-8")

    config = load_proxy_config(config_path)
    assert config.providers[0].api_key.get_secret_value() == "from-dotenv"

    monkeypatch.setenv("TEST_DOTENV_KEY", "from-real-env")
    config = load_proxy_config(config_path)
    assert config.providers[0].api_key.get_secret_value() == "from-real-env"


def test_provider_config_rejects_unknown_keys(tmp_path):
    config_path = tmp_path / "providers.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "glm",
                        "name": "GLM",
                        "base_url": "http://127.0.0.1:3001/v1",
                        "api_key": "x",
                        "models": ["glm-4.7"],
                        "default_model": "glm-4.7",
                        "typo_key": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError) as exc_info:
        load_proxy_config(config_path)
    assert "typo_key" in str(exc_info.value)


@pytest.mark.asyncio
async def test_router_aliases_and_forced_provider():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (app, _client):
        router = app.state.router
        auto = router.resolve("auto")
        assert [target.provider_id for target in auto] == ["glm", "qwen", "deepseek"]
        coding = router.resolve("coding")
        assert [target.provider_id for target in coding] == ["qwen", "glm"]
        namespaced = router.resolve("qwen/qwen3.7-max")
        assert namespaced[0].provider_id == "qwen"
        assert namespaced[0].model == "qwen3.7-max"
        default_namespaced = router.resolve("glm/")
        assert default_namespaced[0].model == "glm-4.7"
        forced = router.resolve("auto", "qwen")
        assert [target.provider_id for target in forced] == ["qwen"]
        with pytest.raises(InvalidRequest):
            router.resolve("glm/glm-4.7", "qwen")
        with pytest.raises(ModelNotFound):
            router.resolve("missing/model")


@pytest.mark.asyncio
async def test_available_models_include_aliases_and_namespaces():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (app, _client):
        models = app.state.router.available_models()
        by_id = {model["id"]: model for model in models}
        assert by_id["auto"]["routing"] == [
            "glm/glm-4.7",
            "qwen/qwen3.7-max",
            "deepseek/deepseek-chat",
        ]
        assert by_id["glm/glm-4.7"]["root"] == "glm-4.7"
        assert by_id["glm/glm-4.7"]["provider"] == "glm"


def test_session_id_is_stable_and_supports_explicit_header():
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    first = derive_session_id("client", payload, httpx.Headers(), "auto")
    second = derive_session_id("client", payload, httpx.Headers(), "auto")
    other = derive_session_id("client", payload, httpx.Headers(), "coding")
    assert first == second
    assert first != other
    explicit = derive_session_id(
        "client", payload, httpx.Headers({"x-agent-session": "my-session"}), "auto"
    )
    assert explicit.startswith("fp-")
    other_client = derive_session_id(
        "other-client", payload, httpx.Headers({"x-agent-session": "my-session"}), "auto"
    )
    assert other_client != explicit
    sanitized = derive_session_id(
        "client",
        payload,
        {"x-agent-session": "safe\r\nМодель"},
        "auto",
    )
    assert sanitized.startswith("fp-")
    assert sanitized != explicit
