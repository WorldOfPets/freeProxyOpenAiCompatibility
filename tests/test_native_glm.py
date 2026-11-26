from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest

from freeproxy.auth.store import TokenStore
from freeproxy.config import ProxyConfig
from freeproxy.native.glm import (
    GlmHttpTransport,
    GlmStreamParser,
    create_glm_app,
    generate_signature,
)
from freeproxy.native.glm_captcha import device_token_gen
from freeproxy.native.glm_captcha.replay_template import REPLAY_SESSION
from freeproxy.native.glm_captcha.solver import (
    CaptchaError,
    GlmCaptchaSolver,
    _build_fresh_payload,
    _credentials,
    take_device_token,
)

from .conftest import gateway, make_provider, make_settings


def _store(tmp_path: Path) -> Path:
    store = TokenStore(tmp_path / "tokens.json")
    store.upsert("glm", {"token": "glm-token", "user_id": "user-1"})
    return store.path


class FakeGlmCompletion:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.parser = GlmStreamParser()
        self.closed = False

    async def deltas(self):
        for line in self.lines:
            content, reasoning = self.parser.handle_line(line)
            if content or reasoning:
                yield content, reasoning

    async def aclose(self) -> None:
        self.closed = True


class FakeGlmTransport:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.closed = False

    async def open_completion(self, prompt, model, tools):
        assert "User: hi" in prompt
        return FakeGlmCompletion(self.lines)

    async def aclose(self) -> None:
        self.closed = True


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False)


def test_glm_signature_is_stable_and_well_formed():
    signature, query = generate_signature(
        "hello",
        "token",
        "user-1",
        now_ms=1_700_000_000_000,
        request_id="req-1",
    )
    # Independent reconstruction of the two-stage HMAC.
    bucket = 1_700_000_000_000 // 300_000
    w_key = hmac.new(
        b"key-@@@@)))()((9))-xxxx&&&%%%%%",
        str(bucket).encode(),
        hashlib.sha256,
    ).hexdigest()
    prompt_b64 = base64.b64encode(b"hello").decode()
    data = f"requestId,req-1,timestamp,1700000000000,user_id,user-1|{prompt_b64}|1700000000000"
    expected = hmac.new(w_key.encode(), data.encode(), hashlib.sha256).hexdigest()
    assert signature == expected
    assert "signature_timestamp=1700000000000" in query
    assert "token=token" in query


def test_glm_parser_edit_and_details():
    parser = GlmStreamParser()
    parser.handle_line(_sse({"data": {"delta_content": "hello"}}))
    parser.handle_line(_sse({"data": {"edit_content": "HELLO", "edit_index": 5}}))
    parser.handle_line(
        _sse(
            {"data": {"content": parser.raw + "<details><summary>x</summary>think</details>answer"}}
        )
    )
    parser.handle_line("data: [DONE]")
    assert parser.content == "helloHELLOanswer"
    assert parser.reasoning == "think"
    assert parser.finished


def test_glm_parser_phase_done():
    parser = GlmStreamParser()
    parser.handle_line(_sse({"data": {"delta_content": "x" * 100}}))
    parser.handle_line(_sse({"data": {"phase": "done"}}))
    assert parser.content == "x" * 100
    assert parser.finished


def test_glm_parser_error():
    parser = GlmStreamParser()
    parser.handle_line(_sse({"error": {"detail": "blocked"}}))
    assert parser.error is not None
    assert "blocked" in parser.error.message


@pytest.mark.asyncio
async def test_glm_native_app_nonstream(tmp_path):
    lines = [
        _sse({"data": {"delta_content": "hello "}}),
        _sse({"data": {"delta_content": "world"}}),
        "data: [DONE]",
    ]
    transport = FakeGlmTransport(lines)
    app = create_glm_app(_store(tmp_path), transport=transport)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello world"


@pytest.mark.asyncio
async def test_glm_native_stream_tool_call(tmp_path):
    content = '{"tool_call":{"name":"read_file","arguments":{"path":"a.txt"}}}'
    lines = [_sse({"data": {"delta_content": content}}), "data: [DONE]"]
    app = create_glm_app(_store(tmp_path), transport=FakeGlmTransport(lines))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "glm-5.3",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "read_file"}}],
            },
        )
        body = b"".join([chunk async for chunk in response.aiter_bytes()])
    assert response.status_code == 200
    assert b'"tool_calls"' in body
    assert b'"read_file"' in body
    assert b"[DONE]" in body


@pytest.mark.asyncio
async def test_glm_native_requires_auth(tmp_path):
    app = create_glm_app(tmp_path / "missing.json", transport=FakeGlmTransport([]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://native"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 503
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code in {401, 503}


@pytest.mark.asyncio
async def test_gateway_routes_glm_native_in_process(tmp_path):
    transport = FakeGlmTransport([_sse({"data": {"delta_content": "glm native"}}), "data: [DONE]"])
    provider = make_provider(
        "glm",
        base_url="http://glm.native",
        prefix="glm",
        models=["glm-5.3"],
        default_model="glm-5.3",
        auth_mode="none",
        health_url="http://glm.native/health",
    ).model_copy(
        update={
            "native": "glm",
            "auth_required": False,
            "models_path": "/v1/models",
            "chat_path": "/v1/chat/completions",
        }
    )
    config = ProxyConfig(
        providers=[provider],
        aliases={"auto": ["glm/glm-5.3"]},
    )

    async def unused_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"outer transport must not be used: {request.url}")

    settings = make_settings(auth_store_path=_store(tmp_path))
    async with gateway(
        settings,
        config,
        unused_handler,
        native_transports={"glm": transport},
    ) as (_app, client):
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer test-key-0123456789abcdef"},
        )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "glm native"
    assert response.headers["x-freeproxy-provider"] == "glm"


def test_glm_fresh_payload_round_trips():
    init_time = int(REPLAY_SESSION["serverTs"]) - 6606
    now_ms = init_time + 15_000
    payload = _build_fresh_payload(REPLAY_SESSION, now_ms, 12)
    assert len(payload) == 140
    assert payload[74] == str(now_ms)
    token = device_token_gen.generate_device_token(REPLAY_SESSION, 4058, payload=payload)
    verified = device_token_gen.verify_token(token, REPLAY_SESSION["sessionKey"])
    assert verified["md5_ok"] is True
    assert verified["fields"] == payload


@pytest.mark.asyncio
async def test_glm_http_transport_builds_signed_request(tmp_path):
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(b'data: {"data":{"delta_content":"native"}}\n\ndata: [DONE]\n\n'),
            )
        if request.url.path.startswith("/api/v1/chats/"):
            return httpx.Response(200, text="true")
        raise AssertionError(f"unexpected path: {request.url.path}")

    class FakeSolver:
        async def get(self) -> str:
            return "captcha-param"

        async def warm(self) -> str:
            return await self.get()

    transport = GlmHttpTransport(
        _store(tmp_path),
        captcha_solver=FakeSolver(),  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )
    completion = await transport.open_completion("hello", "glm-5.3", [])
    try:
        chunks = [chunk async for chunk in completion.deltas()]
    finally:
        await completion.aclose()
    assert "".join(content for content, _ in chunks) == "native"
    request = seen[0]
    assert request.headers["authorization"] == "Bearer glm-token"
    assert request.headers["x-signature"]
    assert "signature_timestamp" in request.url.query.decode()
    body = json.loads(request.content)
    assert body["signature_prompt"] == "hello"
    assert body["captcha_verify_param"] == "captcha-param"
    assert body["features"]["enable_thinking"] is True


@pytest.mark.asyncio
async def test_captcha_solver_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "freeproxy.native.glm_captcha.solver.generate_captcha_param",
        lambda *args, **kwargs: calls.append(1) or "captcha-param",
    )
    solver = GlmCaptchaSolver()
    first = await solver.warm()
    second = await solver.get()
    assert first == second == "captcha-param"
    assert len(calls) == 1


def test_device_token_db_pop(tmp_path):
    import sqlite3

    path = tmp_path / "tokens.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE tokens (id INTEGER PRIMARY KEY, token TEXT)")
    connection.execute("INSERT INTO tokens (token) VALUES ('tok-1')")
    connection.commit()
    connection.close()
    assert take_device_token(path) == "tok-1"
    assert take_device_token(path) is None


def test_captcha_uses_device_token_db(tmp_path, monkeypatch):
    import sqlite3

    from freeproxy.native.glm_captcha import solver as solver_module

    path = tmp_path / "tokens.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE tokens (id INTEGER PRIMARY KEY, token TEXT)")
    connection.execute("INSERT INTO tokens (token) VALUES ('tok-1')")
    connection.commit()
    connection.close()
    monkeypatch.setenv("FP_GLM_TOKENS_DB", str(path))
    monkeypatch.setenv("FP_ALIYUN_CAPTCHA_ACCESS_KEY", "test-access")
    monkeypatch.setenv("FP_ALIYUN_CAPTCHA_SECRET_KEY", "test-secret")
    seen = []
    monkeypatch.setattr(
        solver_module,
        "_captcha_from_device_token",
        lambda client, token, access_key, secret_key: (
            seen.append((token, access_key, secret_key)) or "db-param"
        ),
    )
    assert solver_module.generate_captcha_param() == "db-param"
    assert seen == [("tok-1", "test-access", "test-secret")]
    assert take_device_token(path) is None


def test_captcha_requires_aliyun_credentials(monkeypatch):
    monkeypatch.delenv("FP_ALIYUN_CAPTCHA_ACCESS_KEY", raising=False)
    monkeypatch.delenv("FP_ALIYUN_CAPTCHA_SECRET_KEY", raising=False)
    with pytest.raises(CaptchaError, match="FP_ALIYUN_CAPTCHA_ACCESS_KEY"):
        _credentials()


def test_captcha_credentials_from_env(monkeypatch):
    monkeypatch.setenv("FP_ALIYUN_CAPTCHA_ACCESS_KEY", "env-access")
    monkeypatch.setenv("FP_ALIYUN_CAPTCHA_SECRET_KEY", "env-secret")
    assert _credentials() == ("env-access", "env-secret")
    # Explicit gateway settings take precedence over the environment.
    assert _credentials("setting-access", "setting-secret") == (
        "setting-access",
        "setting-secret",
    )
