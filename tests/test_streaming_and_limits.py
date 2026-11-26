from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from .conftest import (
    BrokenSSEStream,
    SlowSSEStream,
    completion,
    gateway,
    make_config,
    make_settings,
    sse_response,
)


@pytest.mark.asyncio
async def test_streaming_is_passed_through_and_slots_are_released(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        return sse_response()

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    async with gateway(make_settings(), make_config(), handler) as (app, client):
        async with client.stream(
            "POST", "/v1/chat/completions", json=payload, headers=auth_headers
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"
            assert response.headers["x-freeproxy-provider"] == "glm"
            body = b"".join([chunk async for chunk in response.aiter_bytes()])
        assert b"Hel" in body
        assert b"lo" in body
        assert b"[DONE]" in body
        assert app.state.runtimes["glm"].active_requests == 0
        assert app.state.metrics.providers["glm"].successes == 1


@pytest.mark.asyncio
async def test_streaming_alias_falls_back_before_first_byte(auth_headers):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "glm.test":
            return httpx.Response(503, text="not ready")
        return sse_response()

    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    async with gateway(make_settings(), make_config(), handler) as (app, client):
        async with client.stream(
            "POST", "/v1/chat/completions", json=payload, headers=auth_headers
        ) as response:
            assert response.status_code == 200
            assert response.headers["x-freeproxy-provider"] == "qwen"
            body = b"".join([chunk async for chunk in response.aiter_bytes()])
        assert b"[DONE]" in body
        assert calls == ["glm.test", "qwen.test"]
        assert app.state.runtimes["glm"].active_requests == 0
        assert app.state.runtimes["qwen"].active_requests == 0


@pytest.mark.asyncio
async def test_midstream_failure_emits_terminal_error_event(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=BrokenSSEStream(),
        )

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    async with gateway(make_settings(), make_config(), handler) as (app, client):
        async with client.stream(
            "POST", "/v1/chat/completions", json=payload, headers=auth_headers
        ) as response:
            assert response.status_code == 200
            body = b"".join([chunk async for chunk in response.aiter_bytes()])
        assert b"partial" in body
        assert b"event: error" in body
        assert b"stream_interrupted" in body
        assert app.state.runtimes["glm"].active_requests == 0


@pytest.mark.asyncio
async def test_per_client_concurrency_covers_stream_lifetime(auth_headers):
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=SlowSSEStream(started),
            )
        return completion()

    settings = make_settings(client_max_concurrency=1)
    stream_payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    nonstream_payload = {**stream_payload, "stream": False}
    async with gateway(settings, make_config(), handler) as (_app, client):

        async def consume_first() -> None:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json=stream_payload,
                headers=auth_headers,
            ) as response:
                assert response.status_code == 200
                async for _chunk in response.aiter_bytes():
                    pass

        first = asyncio.create_task(consume_first())
        await asyncio.wait_for(started.wait(), timeout=1)
        second = await client.post(
            "/v1/chat/completions", json=stream_payload, headers=auth_headers
        )
        assert second.status_code == 429
        assert second.headers["x-ratelimit-scope"] == "client_concurrency"
        await first
        third = await client.post(
            "/v1/chat/completions", json=nonstream_payload, headers=auth_headers
        )
        assert third.status_code == 200


@pytest.mark.asyncio
async def test_provider_rpm_is_enforced(auth_headers):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion()

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(glm_rpm=1), handler) as (_app, client):
        first = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert first.status_code == 200

        second = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limit_exceeded"
        assert int(second.headers["retry-after"]) >= 1
        assert calls == 1


@pytest.mark.asyncio
async def test_provider_daily_quota_is_enforced(auth_headers):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion()

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(glm_daily=1), handler) as (_app, client):
        first = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert first.status_code == 200
        second = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert second.status_code == 429
        assert calls == 1


@pytest.mark.asyncio
async def test_mixed_rate_limit_and_5xx_is_not_reported_as_all_429(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "glm.test":
            return httpx.Response(429, text="slow down", headers={"Retry-After": "10"})
        return httpx.Response(500, text="boom")

    payload = {
        "model": "auto",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "provider_unavailable"


@pytest.mark.asyncio
async def test_upstream_429_opens_cooldown(auth_headers):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="slow down", headers={"Retry-After": "30"})

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        first = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert first.status_code == 429
        second = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert second.status_code == 429
        assert int(second.headers["retry-after"]) >= 1
        assert calls == 1


@pytest.mark.asyncio
async def test_circuit_breaker_stops_calling_dead_provider(auth_headers):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    payload = {
        "model": "glm/glm-4.7",
        "messages": [{"role": "user", "content": "hello"}],
    }
    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        for _ in range(3):
            response = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
            assert response.status_code == 503
        fourth = await client.post("/v1/chat/completions", json=payload, headers=auth_headers)
        assert fourth.status_code == 503
        assert calls == 3


@pytest.mark.asyncio
async def test_readyz_reports_upstream_health(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "qwen.test":
            return httpx.Response(503, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "healthy": True})

    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["providers"]["glm"]["ok"] is True
        assert body["providers"]["qwen"]["ok"] is False


@pytest.mark.asyncio
async def test_readyz_is_503_when_all_upstreams_are_down(auth_headers):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False})

    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
