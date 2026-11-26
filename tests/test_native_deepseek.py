from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from freeproxy.auth.store import TokenStore
from freeproxy.config import ProxyConfig
from freeproxy.native.deepseek import create_deepseek_app
from freeproxy.native.pow import PowSolver

from .conftest import gateway, make_provider, make_settings


def _store(tmp_path: Path) -> Path:
    store = TokenStore(tmp_path / "tokens.json")
    store.upsert(
        "deepseek",
        {
            "token": "deepseek-web-token",
            "cookie": "ds_session_id=abc",
            "hif_dliq": "dliq",
            "hif_leim": "leim",
            "wasm_url": "http://wasm.test/test.wasm",
        },
    )
    return store.path


def _sse(events: list[dict]) -> bytes:
    body = b"".join(
        b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n" for event in events
    )
    return body + b"data: [DONE]\n\n"


def _upstream_handler(events: list[dict], seen: list[httpx.Request]):
    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/chat_session/create"):
            return httpx.Response(
                200,
                json={"data": {"biz_data": {"chat_session": {"id": "session-1"}}}},
            )
        if path.endswith("/create_pow_challenge"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "biz_data": {
                            "challenge": {
                                "algorithm": "sha3",
                                "challenge": "challenge",
                                "salt": "salt",
                                "expire_at": 2_000_000_000,
                                "difficulty": 100,
                                "signature": "sig",
                            }
                        }
                    }
                },
            )
        if path.endswith("/chat/completion"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse(events),
            )
        raise AssertionError(f"unexpected upstream path: {path}")

    return handler


@pytest.mark.asyncio
async def test_native_deepseek_nonstream(tmp_path, monkeypatch):
    async def fake_solve(self, challenge):
        return 42

    monkeypatch.setattr(PowSolver, "solve", fake_solve)
    seen: list[httpx.Request] = []
    events = [
        {
            "v": {
                "response": {
                    "message_id": "m1",
                    "fragments": [
                        {"type": "THINK", "content": "thinking..."},
                        {"type": "RESPONSE", "content": "hello from native"},
                    ],
                    "finish_reason": "stop",
                }
            }
        }
    ]
    app = create_deepseek_app(
        _store(tmp_path),
        transport=httpx.MockTransport(_upstream_handler(events, seen)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://native",
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == "hello from native"
    assert payload["choices"][0]["message"]["reasoning_content"] == "thinking..."
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert len(seen) == 3
    assert seen[0].headers["authorization"] == "Bearer deepseek-web-token"


@pytest.mark.asyncio
async def test_native_deepseek_stream_and_tool_call(tmp_path, monkeypatch):
    async def fake_solve(self, challenge):
        return 42

    monkeypatch.setattr(PowSolver, "solve", fake_solve)
    seen: list[httpx.Request] = []
    tool_content = '{"tool_call":{"name":"read_file","arguments":{"path":"a.txt"}}}'
    events = [
        {
            "v": {
                "response": {
                    "message_id": "m1",
                    "fragments": [{"type": "RESPONSE", "content": tool_content}],
                    "finish_reason": "stop",
                }
            }
        }
    ]
    app = create_deepseek_app(
        _store(tmp_path),
        transport=httpx.MockTransport(_upstream_handler(events, seen)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://native",
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "read a.txt"}],
                "tools": [{"type": "function", "function": {"name": "read_file"}}],
                "stream": True,
            },
        )
        body = b"".join([chunk async for chunk in response.aiter_bytes()])
    assert response.status_code == 200
    assert b'"tool_calls"' in body
    assert b'"read_file"' in body
    assert b"[DONE]" in body


@pytest.mark.asyncio
async def test_native_deepseek_requires_auth(tmp_path):
    app = create_deepseek_app(tmp_path / "missing.json")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://native",
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 503
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"


@pytest.mark.asyncio
async def test_gateway_falls_back_when_native_deepseek_auth_expired(tmp_path, monkeypatch):
    async def fake_solve(self, challenge):
        return 42

    monkeypatch.setattr(PowSolver, "solve", fake_solve)
    store_path = _store(tmp_path)

    async def native_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40003, "data": {"biz_data": None}})

    async def backup_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "id": "backup-1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "backup-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "from backup"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
        raise AssertionError(request.url.path)

    deepseek = make_provider(
        "deepseek",
        base_url="http://deepseek.native",
        prefix="deepseek",
        models=["deepseek-chat"],
        default_model="deepseek-chat",
        auth_mode="none",
        health_url="http://deepseek.native/health",
    ).model_copy(
        update={
            "native": "deepseek",
            "auth_required": False,
            "models_path": "/v1/models",
            "chat_path": "/v1/chat/completions",
            "max_retries": 0,
        }
    )
    backup = make_provider(
        "backup",
        base_url="http://backup.test",
        prefix="backup",
        models=["backup-model"],
        default_model="backup-model",
        auth_mode="none",
        health_url="http://backup.test/health",
    )
    config = ProxyConfig(
        providers=[deepseek, backup],
        aliases={
            "auto": ["deepseek/deepseek-chat", "backup/backup-model"],
        },
    )
    settings = make_settings(auth_store_path=store_path)
    async with gateway(
        settings,
        config,
        backup_handler,
        native_http_transports={
            "deepseek": httpx.MockTransport(native_handler),
        },
    ) as (_app, client):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": "Bearer test-key-0123456789abcdef"},
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "from backup"
    assert response.headers["x-freeproxy-provider"] == "backup"
    assert response.headers["x-freeproxy-fallback"] == "true"


@pytest.mark.asyncio
async def test_gateway_routes_deepseek_native_in_process(tmp_path, monkeypatch):
    async def fake_solve(self, challenge):
        return 42

    monkeypatch.setattr(PowSolver, "solve", fake_solve)
    store_path = _store(tmp_path)
    seen: list[httpx.Request] = []
    events = [
        {
            "v": {
                "response": {
                    "fragments": [{"type": "RESPONSE", "content": "native gateway"}],
                    "finish_reason": "stop",
                }
            }
        }
    ]
    provider = make_provider(
        "deepseek",
        base_url="http://deepseek.native",
        prefix="deepseek",
        models=["deepseek-chat"],
        default_model="deepseek-chat",
        auth_mode="none",
        health_url="http://deepseek.native/health",
    ).model_copy(
        update={
            "native": "deepseek",
            "auth_required": False,
            "models_path": "/v1/models",
            "chat_path": "/v1/chat/completions",
        }
    )
    config = ProxyConfig(
        providers=[provider],
        aliases={"auto": ["deepseek/deepseek-chat"]},
    )
    settings = make_settings(auth_store_path=store_path)
    async with gateway(
        settings,
        config,
        _upstream_handler(events, seen),
        native_http_transports={"deepseek": httpx.MockTransport(_upstream_handler(events, seen))},
    ) as (_app, client):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": "Bearer test-key-0123456789abcdef"},
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "native gateway"
    assert response.headers["x-freeproxy-provider"] == "deepseek"
    assert len(seen) == 3
