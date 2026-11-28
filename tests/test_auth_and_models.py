from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import ValidationError

from freeproxy.config import Settings

from .conftest import completion, gateway, make_config, make_settings


@pytest.mark.asyncio
async def test_models_require_api_key(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        missing = await client.get("/v1/models")
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "invalid_api_key"
        assert missing.headers["www-authenticate"] == "Bearer"

        wrong = await client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401

        ok = await client.get("/v1/models", headers=auth_headers)
        assert ok.status_code == 200
        assert ok.headers["x-ratelimit-limit"] == "1000"
        ids = {model["id"] for model in ok.json()["data"]}
        assert {"auto", "coding", "glm/glm-4.7", "qwen/qwen3.7-max"} <= ids


@pytest.mark.asyncio
async def test_anonymous_mode_when_no_keys_configured(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    settings = make_settings(api_keys="", allow_anonymous=True)
    async with gateway(settings, make_config(), handler) as (_app, client):
        ok = await client.get("/v1/models")
        assert ok.status_code == 200


def test_auth_cannot_be_disabled_without_keys():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_keys="", allow_anonymous=False)


@pytest.mark.asyncio
async def test_model_discovery_merges_upstream_models(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "qwen.test":
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "qwen-new"}]},
            )
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "glm-discovered"}]},
        )

    async with gateway(
        make_settings(),
        make_config(),
        handler,
        start_discovery=True,
    ) as (_app, client):
        for _ in range(20):
            response = await client.get("/v1/models", headers=auth_headers)
            ids = {model["id"] for model in response.json()["data"]}
            if "glm/glm-discovered" in ids and "qwen/qwen-new" in ids:
                break
            await asyncio.sleep(0.02)
        assert "glm/glm-discovered" in ids
        assert "qwen/qwen-new" in ids


@pytest.mark.asyncio
async def test_models_advertise_only_discovered_ids(auth_headers):
    discovered = ["qwen3.7-plus", "qwen3.8-max", "qwen3.7-max"]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "qwen.test":
            return httpx.Response(
                200,
                json={"object": "list", "data": [{"id": model} for model in discovered]},
            )
        raise AssertionError(f"unexpected upstream call: {request.url}")

    config = make_config()
    async with gateway(make_settings(), config, handler) as (app, client):
        runtime = app.state.runtimes["qwen"]
        assert await runtime.refresh_models(1) == discovered

        configured = next(provider.models for provider in config.providers if provider.id == "qwen")
        assert runtime.advertised_models == sorted(discovered)
        # Routing and alias validation keep accepting configured IDs.
        assert runtime.known_models == sorted(set(configured) | set(discovered))
        assert "qwen3-coder-plus" in runtime.known_models
        assert "qwen3-coder-plus" not in runtime.advertised_models

        response = await client.get("/v1/models", headers=auth_headers)
        ids = {model["id"] for model in response.json()["data"]}
        assert {model_id for model_id in ids if model_id.startswith("qwen/")} == {
            f"qwen/{model}" for model in discovered
        }
        assert "qwen/qwen3-coder-plus" not in ids


@pytest.mark.asyncio
async def test_models_fall_back_to_config_when_discovery_fails(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unavailable", request=request)

    config = make_config()
    configured = next(provider.models for provider in config.providers if provider.id == "qwen")
    async with gateway(make_settings(), config, handler) as (app, client):
        runtime = app.state.runtimes["qwen"]
        assert await runtime.refresh_models(0.5) == []
        assert runtime.advertised_models == sorted(configured)
        assert runtime.known_models == sorted(configured)

        response = await client.get("/v1/models", headers=auth_headers)
        ids = {model["id"] for model in response.json()["data"]}
        assert {f"qwen/{model}" for model in configured} <= ids


@pytest.mark.asyncio
async def test_anonymous_identity_cannot_be_rotated_with_headers():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion()

    settings = make_settings(api_keys="", allow_anonymous=True, client_rpm=1)
    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(settings, make_config(), handler) as (_app, client):
        first = await client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"X-API-Key": "made-up-key-1"},
        )
        assert first.status_code == 200
        second = await client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"X-API-Key": "made-up-key-2"},
        )
        assert second.status_code == 429
        assert calls == 1


@pytest.mark.asyncio
async def test_admin_is_protected_and_redacts_secrets(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        denied = await client.get("/admin/status", headers=auth_headers)
        assert denied.status_code == 403

        allowed = await client.get(
            "/admin/status", headers={"X-Admin-Key": "admin-key-0123456789abcdef"}
        )
        assert allowed.status_code == 200
        body = allowed.text
        assert "glm-upstream-key" not in body
        assert "deepseek-upstream-key" not in body
        assert "test-key-0123456789abcdef" not in body
        assert allowed.json()["providers"]


@pytest.mark.asyncio
async def test_admin_without_key_is_not_opened_by_forwarded_headers(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    settings = make_settings(admin_api_key="")
    async with gateway(settings, make_config(), handler) as (_app, client):
        direct = await client.get("/admin/status", headers=auth_headers)
        assert direct.status_code == 200
        forwarded = await client.get(
            "/admin/status",
            headers={**auth_headers, "X-Forwarded-For": "203.0.113.10"},
        )
        assert forwarded.status_code == 403


@pytest.mark.asyncio
async def test_local_admin_mutations_require_csrf_header(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    settings = make_settings(admin_api_key="")
    async with gateway(settings, make_config(), handler) as (_app, client):
        denied = await client.post("/admin/limits/reset", headers=auth_headers)
        assert denied.status_code == 403
        allowed = await client.post(
            "/admin/limits/reset",
            headers={**auth_headers, "X-FreeProxy-Admin": "true"},
        )
        assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_openapi_exposes_security_schemes(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (app, _client):
        schema = app.openapi()
        schemes = schema["components"]["securitySchemes"]
        assert {"BearerAuth", "ApiKeyAuth", "AdminKeyAuth"} <= set(schemes)
        assert schema["paths"]["/v1/chat/completions"]["post"]["security"]
        assert schema["paths"]["/admin/status"]["get"]["security"]


@pytest.mark.asyncio
async def test_framework_errors_are_openai_shaped(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        not_found = await client.get("/does-not-exist", headers=auth_headers)
        assert not_found.status_code == 404
        assert not_found.json()["error"]["code"] == "not_found"

        wrong_method = await client.get("/v1/chat/completions", headers=auth_headers)
        assert wrong_method.status_code == 405
        assert wrong_method.json()["error"]["code"] == "method_not_allowed"


@pytest.mark.asyncio
async def test_client_rate_limit_headers_and_error(auth_headers):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion()

    settings = make_settings(client_rpm=1)
    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(settings, make_config(), handler) as (app, client):
        first = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert first.status_code == 200
        assert first.headers["x-ratelimit-remaining"] == "0"

        second = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limit_exceeded"
        assert second.headers["x-ratelimit-scope"] == "client_rpm"
        assert int(second.headers["retry-after"]) >= 1
        assert calls == 1
        assert app.state.metrics.client_rate_limited == 1


@pytest.mark.asyncio
async def test_request_body_limit(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    settings = make_settings(max_body_bytes=1024)
    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "x" * 2000}],
    }
    async with gateway(settings, make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"
