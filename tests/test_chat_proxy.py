from __future__ import annotations

import json

import httpx
import pytest

from .conftest import completion, gateway, json_body, make_config, make_settings


@pytest.mark.asyncio
async def test_namespaced_model_is_forwarded_correctly(auth_headers):
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "glm.test"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer glm-upstream-key"
        body = json_body(request)
        assert body["model"] == "glm-4.7"
        assert body["max_tokens"] == 128
        return completion(model="glm-4.7", content="routed")

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 128,
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "routed"
        assert response.headers["x-freeproxy-provider"] == "glm"
        assert response.headers["x-freeproxy-model"] == "glm-4.7"
        assert response.headers["x-request-id"]
        assert len(seen) == 1


@pytest.mark.asyncio
async def test_default_max_tokens_and_client_auth_are_not_forwarded(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json_body(request)
        assert body["max_tokens"] == 4096
        assert request.headers["authorization"] == "Bearer glm-upstream-key"
        assert request.headers["authorization"] != "Bearer test-key-0123456789abcdef"
        return completion()

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_max_tokens_above_gateway_limit_is_rejected(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 99999,
    }
    async with gateway(make_settings(max_output_tokens=8192), make_config(), handler) as (
        _app,
        client,
    ):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "max_tokens_too_large"


@pytest.mark.asyncio
async def test_forced_provider_header(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "qwen.test"
        assert request.url.path == "/api/chat/completions"
        return completion(model="qwen3.7-max")

    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "hello"}],
    }
    headers = {**auth_headers, "X-FreeProxy-Provider": "qwen"}
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.headers["x-freeproxy-provider"] == "qwen"


@pytest.mark.asyncio
async def test_alias_falls_back_to_next_provider(auth_headers):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "glm.test":
            return httpx.Response(500, text="upstream exploded")
        assert request.url.host == "qwen.test"
        return completion(model="qwen3.7-max", content="fallback")

    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "fallback"
        assert response.headers["x-freeproxy-provider"] == "qwen"
        assert response.headers["x-freeproxy-fallback"] == "true"
        assert calls == ["glm.test", "qwen.test"]
        assert app.state.metrics.providers["glm"].errors == 1
        assert app.state.metrics.providers["qwen"].successes == 1


@pytest.mark.asyncio
async def test_deepseek_gets_derived_agent_session(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "deepseek.test"
        assert request.headers["x-agent-session"].startswith("fp-")
        assert json_body(request)["model"] == "deepseek-chat"
        return completion(model="deepseek-chat")

    payload = {
        "model": "deepseek/deepseek-chat",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_upstream_client_error_is_preserved(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "bad request from upstream",
                    "type": "invalid_request_error",
                    "code": "bad_request",
                }
            },
        )

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"
        assert response.headers["x-freeproxy-provider"] == "glm"
        assert response.headers["x-freeproxy-upstream-status"] == "400"


@pytest.mark.asyncio
async def test_strict_models_rejects_unknown_bare_model(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    payload = {
        "model": "not-a-real-model",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(strict_models=True), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_non_json_upstream_success_is_an_error(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "upstream_error"


@pytest.mark.asyncio
async def test_non_ascii_model_is_rejected(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    payload = {
        "model": "glm/модель",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_garbage_200_trips_the_provider_breaker(auth_headers):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="not json")

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        for _ in range(3):
            response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
            assert response.status_code == 502
        fourth = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert fourth.status_code == 503
        assert calls == 3


@pytest.mark.asyncio
async def test_content_type_must_be_json(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post(
            "/v1/chat/completions",
            content=json.dumps(payload).encode(),
            headers={**auth_headers, "Content-Type": "text/plain"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_upstream_string_error_is_wrapped(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "plain upstream error"})

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 400
        assert isinstance(response.json()["error"], dict)
        assert "plain upstream error" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_oversized_upstream_response_is_rejected(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": "x" * 2000})

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(max_upstream_bytes=1024), make_config(), handler) as (
        _app,
        client,
    ):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "upstream_response_too_large"


@pytest.mark.asyncio
async def test_deeply_nested_json_is_rejected_not_500(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    body = b'{"messages":' + b"[" * 3000 + b"]" * 3000 + b"}"
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post(
            "/v1/chat/completions",
            content=body,
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_invalid_payload_is_rejected(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post(
            "/v1/chat/completions",
            content=json.dumps({"messages": []}).encode(),
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request_error"
