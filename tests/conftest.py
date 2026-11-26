from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from freeproxy.config import ProviderConfig, ProxyConfig, Settings
from freeproxy.main import create_app

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def make_provider(
    provider_id: str,
    *,
    base_url: str,
    prefix: str,
    models: list[str],
    default_model: str,
    api_key: str = "",
    auth_mode: str = "bearer",
    health_url: str | None = None,
    max_rpm: int = 100,
    max_requests_per_day: int = 0,
    max_concurrency: int = 4,
    timeout_seconds: float = 30,
    max_retries: int = 0,
    session_header: bool = False,
) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        name=provider_id.title(),
        base_url=base_url,
        api_key=api_key,
        auth_mode=auth_mode,
        prefix=prefix,
        models=models,
        default_model=default_model,
        health_url=health_url or f"{base_url.rsplit('/', 1)[0]}/health",
        max_rpm=max_rpm,
        max_requests_per_day=max_requests_per_day,
        max_concurrency=max_concurrency,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        session_header=session_header,
    )


def make_config(
    *,
    glm_rpm: int = 100,
    qwen_rpm: int = 100,
    deepseek_rpm: int = 100,
    glm_daily: int = 0,
    qwen_daily: int = 0,
    deepseek_daily: int = 0,
    aliases: dict[str, list[str]] | None = None,
) -> ProxyConfig:
    providers = [
        make_provider(
            "glm",
            base_url="http://glm.test/v1",
            prefix="glm",
            models=["glm-4.7", "glm-5.3-flash"],
            default_model="glm-4.7",
            api_key="glm-upstream-key",
            max_rpm=glm_rpm,
            max_requests_per_day=glm_daily,
        ),
        make_provider(
            "qwen",
            base_url="http://qwen.test/api",
            prefix="qwen",
            models=["qwen3.7-max", "qwen3-coder-plus"],
            default_model="qwen3.7-max",
            auth_mode="none",
            max_rpm=qwen_rpm,
            max_requests_per_day=qwen_daily,
        ),
        make_provider(
            "deepseek",
            base_url="http://deepseek.test/v1",
            prefix="deepseek",
            models=["deepseek-chat", "deepseek-reasoner"],
            default_model="deepseek-chat",
            api_key="deepseek-upstream-key",
            max_rpm=deepseek_rpm,
            max_requests_per_day=deepseek_daily,
            session_header=True,
        ),
    ]
    return ProxyConfig(
        providers=providers,
        aliases=aliases
        or {
            "auto": ["glm/glm-4.7", "qwen/qwen3.7-max", "deepseek/deepseek-chat"],
            "coding": ["qwen/qwen3-coder-plus", "glm/glm-4.7"],
        },
    )


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "api_keys": "test-key-0123456789abcdef",
        "allow_anonymous": False,
        "admin_api_key": "admin-key-0123456789abcdef",
        "client_rpm": 1000,
        "client_daily_requests": 10_000,
        "global_rpm": 10_000,
        "global_max_concurrency": 10,
        "max_body_bytes": 1_000_000,
        "max_output_tokens": 8192,
        "default_max_tokens": 4096,
        "model_discovery_ttl_seconds": 3600,
        "model_discovery_timeout_seconds": 1,
        "log_level": "WARNING",
        "log_format": "text",
    }
    values.update(overrides)
    return Settings(**values)


def completion(
    *,
    model: str = "glm-4.7",
    content: str = "hello",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    usage: dict[str, int] | None = None,
) -> httpx.Response:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    return httpx.Response(status_code, json=payload, headers=headers)


class SlowSSEStream(httpx.AsyncByteStream):
    def __init__(self, started: Any, delay: float = 0.3) -> None:
        self.started = started
        self.delay = delay

    async def __aiter__(self):
        self.started.set()
        yield b'data: {"choices":[{"index":0,"delta":{"content":"first"},"finish_reason":null}]}\n\n'
        await asyncio.sleep(self.delay)
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"


class BrokenSSEStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
        raise httpx.ReadError("simulated upstream reset")


def sse_response() -> httpx.Response:
    chunks = [
        b'data: {"id":"chatcmpl-stream","choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n\n',
        b'data: {"id":"chatcmpl-stream","choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    return httpx.Response(
        200,
        content=b"".join(chunks),
        headers={"content-type": "text/event-stream; charset=utf-8"},
    )


def json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


@asynccontextmanager
async def gateway(
    settings: Settings,
    config: ProxyConfig,
    handler: Handler,
    *,
    start_discovery: bool = False,
    native_http_transports: dict[str, httpx.AsyncBaseTransport] | None = None,
    native_transports: dict[str, Any] | None = None,
) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    transport = httpx.MockTransport(handler)
    app = create_app(
        settings,
        config,
        http_transport=transport,
        native_http_transports=native_http_transports,
        native_transports=native_transports,
        start_discovery=start_discovery,
    )
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client,
    ):
        yield app, client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-key-0123456789abcdef"}
