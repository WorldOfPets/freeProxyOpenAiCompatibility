from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from freeproxy.auth.store import TokenStore
from freeproxy.config import ProxyConfig
from freeproxy.native.qwen import (
    MODELS,
    QwenStreamParser,
    build_chat_payload,
    build_create_chat_payload,
    create_qwen_app,
    normalize_model,
)

from .conftest import gateway, make_provider, make_settings


def _store(tmp_path: Path) -> Path:
    store = TokenStore(tmp_path / "tokens.json")
    store.upsert("qwen", {"token": "qwen-token", "cookies": []})
    return store.path


class FakeQwenCompletion:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.parser = QwenStreamParser()
        self.closed = False

    async def deltas(self):
        for payload in self.payloads:
            content, reasoning = self.parser.handle_payload(payload)
            if content or reasoning:
                yield content, reasoning

    async def aclose(self) -> None:
        self.closed = True


class FakeQwenTransport:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.models: list[str] = []
        self.closed = False

    async def open_completion(self, prompt, model, tools):
        self.prompts.append(prompt)
        self.models.append(model)
        return FakeQwenCompletion(
            [
                {
                    "response.created": {"response_id": "resp-1"},
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "hello ", "reasoning_content": "why"},
                        }
                    ],
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "world", "status": "finished"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                },
            ]
        )

    async def aclose(self) -> None:
        self.closed = True


def test_qwen_payload_builders():
    create = build_create_chat_payload("qwen3.7-max")
    assert create["models"] == ["qwen3.7-max"]
    assert create["chat_type"] == "t2t"
    payload = build_chat_payload("hi", "qwen3.7-max", "chat-1")
    assert payload["chat_id"] == "chat-1"
    assert payload["messages"][0]["content"] == "hi"
    assert payload["messages"][0]["models"] == ["qwen3.7-max"]
    assert payload["stream"] is True


def test_qwen_parser_reasoning_and_finish():
    parser = QwenStreamParser()
    content, reasoning = parser.handle_payload(
        {
            "response.created": {"response_id": "r1"},
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": "answer",
                        "reasoning_content": "think",
                    },
                }
            ],
        }
    )
    assert content == "answer"
    assert reasoning == "think"
    assert parser.response_id == "r1"
    parser.handle_payload({"choices": [{"index": 0, "delta": {"status": "finished"}}]})
    assert parser.finished


def test_qwen_parser_error_chunk():
    parser = QwenStreamParser()
    parser.handle_payload({"code": "RateLimited", "detail": "slow down"})
    assert parser.error is not None
    assert parser.error.status_code == 429


def test_qwen_model_aliases():
    assert normalize_model("qwen3-coder") == "qwen3-coder-plus"
    assert normalize_model("QWEN3.7-PLUS") == "qwen3.7-plus"
    assert normalize_model("unknown") == "qwen3.7-max"
    assert "qwen3.7-max" in MODELS


@pytest.mark.asyncio
async def test_qwen_native_app_nonstream_and_tools(tmp_path):
    transport = FakeQwenTransport()
    app = create_qwen_app(_store(tmp_path), transport=transport)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3.7-max",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == "hello world"
    assert payload["choices"][0]["message"]["reasoning_content"] == "why"
    assert payload["usage"]["total_tokens"] == 5
    assert transport.prompts and "User: hi" in transport.prompts[0]


@pytest.mark.asyncio
async def test_qwen_native_tool_call_stream(tmp_path):
    class ToolTransport(FakeQwenTransport):
        async def open_completion(self, prompt, model, tools):
            return FakeQwenCompletion(
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "content": '{"tool_call":{"name":"read_file","arguments":{"path":"a.txt"}}}',
                                    "status": "finished",
                                },
                            }
                        ]
                    }
                ]
            )

    app = create_qwen_app(_store(tmp_path), transport=ToolTransport())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3.7-max",
                "stream": True,
                "messages": [{"role": "user", "content": "read a.txt"}],
                "tools": [{"type": "function", "function": {"name": "read_file"}}],
            },
        )
        body = b"".join([chunk async for chunk in response.aiter_bytes()])
    assert response.status_code == 200
    assert b'"tool_calls"' in body
    assert b'"read_file"' in body
    assert b"[DONE]" in body


@pytest.mark.asyncio
async def test_qwen_native_requires_auth(tmp_path):
    app = create_qwen_app(tmp_path / "missing.json", transport=FakeQwenTransport())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 503
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen3.7-max",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code in {401, 503}


@pytest.mark.asyncio
async def test_gateway_streams_qwen_native_in_process(tmp_path):
    transport = FakeQwenTransport()
    provider = make_provider(
        "qwen",
        base_url="http://qwen.native",
        prefix="qwen",
        models=["qwen3.7-max"],
        default_model="qwen3.7-max",
        auth_mode="none",
        health_url="http://qwen.native/health",
    ).model_copy(
        update={
            "native": "qwen",
            "auth_required": False,
            "models_path": "/v1/models",
            "chat_path": "/v1/chat/completions",
        }
    )
    config = ProxyConfig(
        providers=[provider],
        aliases={"auto": ["qwen/qwen3.7-max"]},
    )

    async def unused_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"outer transport must not be used: {request.url}")

    settings = make_settings(auth_store_path=_store(tmp_path))
    async with gateway(
        settings,
        config,
        unused_handler,
        native_transports={"qwen": transport},
    ) as (_app, client):
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": "Bearer test-key-0123456789abcdef"},
        )
        body = b"".join([chunk async for chunk in response.aiter_bytes()])
    assert response.status_code == 200
    assert b"hello " in body
    assert b"world" in body
    assert b"[DONE]" in body


@pytest.mark.asyncio
async def test_gateway_routes_qwen_native_in_process(tmp_path):
    transport = FakeQwenTransport()
    provider = make_provider(
        "qwen",
        base_url="http://qwen.native",
        prefix="qwen",
        models=["qwen3.7-max"],
        default_model="qwen3.7-max",
        auth_mode="none",
        health_url="http://qwen.native/health",
    ).model_copy(
        update={
            "native": "qwen",
            "auth_required": False,
            "models_path": "/v1/models",
            "chat_path": "/v1/chat/completions",
        }
    )
    config = ProxyConfig(
        providers=[provider],
        aliases={"auto": ["qwen/qwen3.7-max"]},
    )
    settings = make_settings(auth_store_path=_store(tmp_path))

    async def unused_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"outer transport must not be used: {request.url}")

    async with gateway(
        settings,
        config,
        unused_handler,
        native_transports={"qwen": transport},
    ) as (_app, client):
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer test-key-0123456789abcdef"},
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello world"
    assert response.headers["x-freeproxy-provider"] == "qwen"
