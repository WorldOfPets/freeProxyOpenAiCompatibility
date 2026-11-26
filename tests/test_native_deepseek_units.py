from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from freeproxy.auth.store import TokenStore
from freeproxy.native.deepseek import (
    MODEL_CONFIGS,
    DeepSeekStreamParser,
    create_deepseek_app,
    format_prompt,
    looks_like_tool_markup,
    parse_tool_call,
)
from freeproxy.native.pow import PowSolver


def _store(tmp_path: Path) -> Path:
    store = TokenStore(tmp_path / "tokens.json")
    store.upsert("deepseek", {"token": "token", "cookie": "c=1"})
    return store.path


def test_parser_applies_patch_operations_and_content_appends():
    parser = DeepSeekStreamParser()
    parser.handle_line(
        "data: "
        + json.dumps(
            {
                "p": "response",
                "v": {
                    "response": {
                        "message_id": "m1",
                        "fragments": [{"type": "RESPONSE", "content": "hel"}],
                        "status": "RUNNING",
                    }
                },
            }
        )
    )
    parser.handle_line(
        "data: "
        + json.dumps(
            {
                "p": "response",
                "v": [
                    {"p": "fragments", "o": "APPEND", "v": {"type": "RESPONSE", "content": "lo"}}
                ],
            }
        )
    )
    assert parser.content == "hello"

    parser.handle_line("data: " + json.dumps({"p": "response/fragments/-1/content", "v": "!"}))
    assert parser.content == "hello!"

    parser.handle_line("data: " + json.dumps({"p": "response/status", "v": "INCOMPLETE"}))
    assert parser.finalize() == "length"


def test_parser_delta_is_incremental():
    parser = DeepSeekStreamParser()
    parser.handle_line(
        "data: "
        + json.dumps(
            {
                "v": {
                    "response": {
                        "fragments": [{"type": "THINK", "content": "why"}],
                        "finish_reason": None,
                    }
                }
            }
        )
    )
    assert parser.delta() == ("", "why")
    parser.handle_line(
        "data: "
        + json.dumps({"p": "response/fragments", "v": {"type": "RESPONSE", "content": "ok"}})
    )
    assert parser.delta() == ("ok", "")


def test_parse_tool_call_accepts_envelope_and_fenced_json():
    content = '```json\n{"tool_call":{"name":"read_file","arguments":{"path":"a.txt"}}}\n```'
    parsed = parse_tool_call(content)
    assert parsed is not None
    _, tool_calls = parsed
    assert tool_calls[0]["function"]["name"] == "read_file"
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"path": "a.txt"}


def test_parse_tool_call_ignores_plain_text():
    assert parse_tool_call("hello") is None
    assert not looks_like_tool_markup("hello {not a tool}")
    assert looks_like_tool_markup('{"tool_call":')
    assert looks_like_tool_markup("<tool_call>")


def test_format_prompt_includes_tools_and_tool_results():
    prompt = format_prompt(
        {
            "messages": [
                {"role": "system", "content": "be useful"},
                {"role": "user", "content": "read a.txt"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        }
    )
    assert "be useful" in prompt
    assert "TOOL REQUEST SYSTEM" in prompt
    assert "read_file" in prompt
    assert "[Tool Result: read_file]" in prompt
    assert "contents" in prompt


def test_pow_header_round_trips_challenge_fields():
    challenge = {
        "algorithm": "DeepSeekHashV1",
        "challenge": "abc",
        "salt": "salt",
        "signature": "sig",
        "target_path": "/api/v0/chat/completion",
    }
    header = PowSolver.build_header(challenge, 42, "/api/v0/chat/completion")
    decoded = json.loads(base64.b64decode(header))
    assert decoded == {
        "algorithm": "DeepSeekHashV1",
        "challenge": "abc",
        "salt": "salt",
        "answer": 42,
        "signature": "sig",
        "target_path": "/api/v0/chat/completion",
    }


def test_all_deepseek_alias_models_are_known():
    for model in (
        "deepseek-chat",
        "deepseek-default",
        "deepseek-v4-flash",
        "deepseek-v4.1-flash",
        "deepseek-reasoner",
        "deepseek-chat-search",
    ):
        assert model in MODEL_CONFIGS


@pytest.mark.asyncio
async def test_native_health_with_token(tmp_path):
    app = create_deepseek_app(_store(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["mode"] == "native-deepseek"


@pytest.mark.asyncio
async def test_native_stream_requires_token_before_sse(tmp_path):
    app = create_deepseek_app(tmp_path / "missing.json")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"


@pytest.mark.asyncio
async def test_native_expired_session_returns_401(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40003, "data": {"biz_data": None}})

    app = create_deepseek_app(_store(tmp_path), transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "upstream_auth_error"


@pytest.mark.asyncio
async def test_native_stream_returns_completion_http_error_not_sse(tmp_path, monkeypatch):
    async def fake_solve(self, challenge):
        return 42

    monkeypatch.setattr(PowSolver, "solve", fake_solve)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat_session/create"):
            return httpx.Response(200, json={"data": {"biz_data": {"chat_session": {"id": "s1"}}}})
        if request.url.path.endswith("/create_pow_challenge"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "biz_data": {
                            "challenge": {
                                "algorithm": "DeepSeekHashV1",
                                "challenge": "abc",
                                "salt": "salt",
                                "expire_at": 1,
                                "difficulty": 100,
                                "signature": "sig",
                            }
                        }
                    }
                },
            )
        return httpx.Response(429, headers={"retry-after": "7"}, json={"message": "slow"})

    app = create_deepseek_app(_store(tmp_path), transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "upstream_error"


@pytest.mark.asyncio
async def test_native_upstream_401_is_mapped(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "expired"})

    app = create_deepseek_app(_store(tmp_path), transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "upstream_session_error"
