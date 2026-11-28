from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.paths import AuthPaths
from ..auth.store import AuthStoreError, TokenStore
from .common import (
    NativeError,
    format_prompt,
    looks_like_tool_markup,
    openai_chunk,
    openai_error_event,
    openai_response,
    parse_tool_call,
    retry_after_seconds,
)
from .glm_captcha.solver import CaptchaError, GlmCaptchaSolver

logger = logging.getLogger("freeproxy.native.glm")

BASE_URL = "https://chat.z.ai"
CHAT_PATH = "/api/v2/chat/completions"
SALT_KEY = "key-@@@@)))()((9))-xxxx&&&%%%%%"
FE_VERSION = "prod-fe-1.1.93"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
DEFAULT_MODEL = "glm-5.3"
MAX_PROMPT_CHARS = 80_000
MODELS: tuple[str, ...] = (
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5",
    "glm-4.7",
    "glm-4.6",
    "GLM-5v-Turbo",
)


def normalize_model(model: str | None) -> str:
    value = (model or DEFAULT_MODEL).strip()
    for known in MODELS:
        if value.lower() == known.lower():
            return known
    return value or DEFAULT_MODEL


def decode_jwt_user(token: str) -> str:
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return ""
    value = data.get("id") if isinstance(data, dict) else None
    return str(value) if value else ""


def generate_signature(
    prompt: str,
    token: str,
    user_id: str,
    *,
    now_ms: int | None = None,
    request_id: str | None = None,
) -> tuple[str, str]:
    """Return (x-signature, encoded URL query string)."""
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    timestamp = str(ts_ms)
    request_id = request_id or str(uuid.uuid4())
    bucket = ts_ms // 300_000

    w_key = hmac.new(
        SALT_KEY.encode("utf-8"), str(bucket).encode("ascii"), hashlib.sha256
    ).hexdigest()
    payload_parts = [
        ("requestId", request_id),
        ("timestamp", timestamp),
        ("user_id", user_id),
    ]
    payload_parts.sort(key=lambda item: item[0])
    sorted_payload = ",".join(f"{key},{value}" for key, value in payload_parts)
    prompt_b64 = base64.b64encode(prompt.strip().encode("utf-8")).decode("ascii")
    data_to_sign = f"{sorted_payload}|{prompt_b64}|{timestamp}"
    signature = hmac.new(
        w_key.encode("utf-8"), data_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    params = {
        "timestamp": timestamp,
        "requestId": request_id,
        "user_id": user_id,
        "version": "0.0.1",
        "platform": "web",
        "token": token,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        "language": "en-US",
        "screen_resolution": "1920x1080",
        "viewport_size": "1920x1080",
        "timezone": "Europe/Paris",
        "timezone_offset": "-60",
        "signature_timestamp": timestamp,
    }
    return signature, urllib.parse.urlencode(sorted(params.items()))


class GlmStreamParser:
    """Parses Z.AI's edit-based SSE stream into reasoning/content deltas."""

    def __init__(self) -> None:
        self.raw = ""
        self.content = ""
        self.reasoning = ""
        self.finish_reason: str | None = None
        self.error: NativeError | None = None
        self.finished = False
        self._content_emitter = _Emitter()
        self._reasoning_emitter = _Emitter()

    def handle_line(self, raw_line: str) -> tuple[str, str]:
        line = raw_line.rstrip("\r")
        if not line.startswith("data:"):
            return "", ""
        payload = line[5:].lstrip()
        if not payload:
            return "", ""
        if payload == "[DONE]":
            self.finished = True
            return self._flush(final=True)
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return "", ""
        if not isinstance(event, dict):
            return "", ""

        error = _extract_error(event)
        if error:
            self.error = NativeError(502, error[:500], "upstream_error")
            self.finished = True
            return "", ""

        data = event.get("data")
        if isinstance(data, dict):
            if data.get("phase") == "done":
                self.finished = True
                return self._flush(final=True)
            edit_content = data.get("edit_content")
            if isinstance(edit_content, str) and edit_content:
                index = data.get("edit_index")
                self._apply_edit(int(index) if isinstance(index, (int, float)) else 0, edit_content)
            else:
                content = data.get("content")
                if isinstance(content, str) and content:
                    self.raw = content
                else:
                    delta = data.get("delta_content")
                    if isinstance(delta, str) and delta:
                        self.raw += delta
        return self._flush(final=False)

    def _apply_edit(self, utf16_index: int, text: str) -> None:
        prefix = self.raw.encode("utf-16-le")[: max(0, utf16_index) * 2]
        before = prefix.decode("utf-16-le", "ignore")
        self.raw = before + text

    def _flush(self, *, final: bool) -> tuple[str, str]:
        reasoning, content, open_block = _split_details(self.raw)
        reasoning = _strip_details_tags(reasoning)
        if not final:
            content = _hold_back(content, 64)
            if open_block:
                reasoning = _hold_back(reasoning, 64)
        reasoning_delta = self._reasoning_emitter.delta(reasoning)
        content_delta = self._content_emitter.delta(content)
        self.content = self._content_emitter.view
        self.reasoning = self._reasoning_emitter.view
        return content_delta, reasoning_delta

    def finalize(self) -> str:
        if self.finish_reason:
            return self.finish_reason
        return "stop"


class _Emitter:
    def __init__(self) -> None:
        self.view = ""

    def delta(self, target: str) -> str:
        if target == self.view:
            return ""
        if target.startswith(self.view):
            delta = target[len(self.view) :]
            self.view = target
            return delta
        common = 0
        limit = min(len(target), len(self.view))
        while common < limit and target[common] == self.view[common]:
            common += 1
        if common == len(target):
            return ""
        delta = target[common:]
        self.view = target
        return delta


_DETAILS_RE = re.compile(r"<details\b[^>]*>(.*?)</details>", re.IGNORECASE | re.DOTALL)
_DETAILS_OPEN_RE = re.compile(r"<details\b[^>]*>", re.IGNORECASE)


def _split_details(raw: str) -> tuple[str, str, bool]:
    reasoning_parts: list[str] = []
    cursor = 0
    content_parts: list[str] = []
    for match in _DETAILS_RE.finditer(raw):
        content_parts.append(raw[cursor : match.start()])
        reasoning_parts.append(match.group(1))
        cursor = match.end()
    content_parts.append(raw[cursor:])
    content = "".join(content_parts)
    reasoning = "\n".join(reasoning_parts)

    open_block = False
    opener = _DETAILS_OPEN_RE.search(content)
    if opener is not None:
        open_block = True
        reasoning += content[opener.end() :]
        content = content[: opener.start()]
    return reasoning, content, open_block


def _strip_details_tags(text: str) -> str:
    text = _DETAILS_OPEN_RE.sub("", text)
    text = re.sub(r"<summary\b[^>]*>.*?</summary>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"</details>|</?summary\b[^>]*>", "", text, flags=re.IGNORECASE)
    lines = [re.sub(r"^\s*>\s?", "", line) for line in text.splitlines()]
    return "\n".join(lines).strip()


def _hold_back(text: str, count: int) -> str:
    if len(text) <= count:
        return ""
    return text[:-count]


def _extract_error(event: dict[str, Any]) -> str:
    for key in ("error", "detail", "message"):
        value = event.get(key)
        if isinstance(value, dict):
            detail = value.get("detail") or value.get("message")
            if detail:
                return str(detail)
        elif isinstance(value, str) and value:
            if key == "message" and event.get("data"):
                continue
            return value
    data = event.get("data")
    if isinstance(data, dict):
        value = data.get("error")
        if isinstance(value, dict):
            return str(value.get("detail") or value.get("message") or value)
        if isinstance(value, str) and value:
            return value
        if data.get("code") and data.get("detail"):
            return str(data["detail"])
    return ""


class GlmCompletion:
    def __init__(
        self,
        client: httpx.AsyncClient,
        response: httpx.Response,
        parser: GlmStreamParser,
        chat_id: str,
        headers: dict[str, str],
    ) -> None:
        self.client = client
        self.response = response
        self.parser = parser
        self.chat_id = chat_id
        self.headers = headers
        self._closed = False

    async def deltas(self) -> AsyncIterator[tuple[str, str]]:
        async for line in self.response.aiter_lines():
            content_delta, reasoning_delta = self.parser.handle_line(line)
            if self.parser.error is not None:
                raise self.parser.error
            if content_delta or reasoning_delta:
                yield content_delta, reasoning_delta
            if self.parser.finished:
                break
        if not self.parser.finished and self.parser.error is None:
            content_delta, reasoning_delta = self.parser.handle_line("data: [DONE]")
            if content_delta or reasoning_delta:
                yield content_delta, reasoning_delta

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self.response.aclose()
        if self.chat_id:
            with contextlib.suppress(Exception):
                await self.client.delete(
                    BASE_URL + f"/api/v1/chats/{self.chat_id}",
                    headers=self.headers,
                    timeout=10,
                )
        with contextlib.suppress(Exception):
            await self.client.aclose()


class GlmTransport(Protocol):
    async def open_completion(
        self, prompt: str, model: str, tools: list[dict[str, Any]]
    ) -> GlmCompletion: ...

    async def aclose(self) -> None: ...


class GlmHttpTransport:
    def __init__(
        self,
        store_path: Path,
        *,
        captcha_solver: GlmCaptchaSolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.store_path = Path(store_path)
        self.captcha_solver = captcha_solver or GlmCaptchaSolver()
        self.transport = transport
        self.timeout_seconds = timeout_seconds

    def _auth(self) -> tuple[str, str]:
        try:
            data = TokenStore(self.store_path).provider("glm") or {}
        except AuthStoreError as exc:
            raise NativeError(
                503,
                f"GLM auth store is unreadable: {exc}",
                "provider_not_configured",
            ) from exc
        token = str(data.get("token") or data.get("zai_token") or "")
        if not token:
            raise NativeError(
                503,
                "GLM token is not configured. Run `uv run freeproxy-auth login glm`.",
                "provider_not_configured",
            )
        user_id = str(data.get("user_id") or "") or decode_jwt_user(token)
        if not user_id:
            raise NativeError(
                401,
                "GLM token has no user id; run `uv run freeproxy-auth login glm`.",
                "upstream_auth_error",
            )
        return token, user_id

    async def _auth_async(self) -> tuple[str, str]:
        """Read the auth store off the event loop (it does blocking file I/O)."""
        return await asyncio.to_thread(self._auth)

    @staticmethod
    def _headers(token: str, signature: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "content-type": "application/json",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
            "x-fe-Version": FE_VERSION,
            "x-region": "overseas",
            "x-signature": signature,
        }

    async def open_completion(
        self, prompt: str, model: str, tools: list[dict[str, Any]]
    ) -> GlmCompletion:
        token, user_id = await self._auth_async()
        try:
            captcha_param = await self.captcha_solver.get()
        except CaptchaError as exc:
            raise NativeError(503, str(exc), "captcha_failed") from exc
        signature, query = generate_signature(prompt, token, user_id)
        headers = self._headers(token, signature)
        chat_id = str(uuid.uuid4())
        body = {
            "model": model,
            "chat_id": chat_id,
            "messages": [{"role": "user", "content": prompt}],
            "signature_prompt": prompt,
            "stream": True,
            "captcha_verify_param": captcha_param,
            "features": {"enable_thinking": True, "image_generation": False},
        }
        client = httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(self.timeout_seconds, connect=30.0),
        )
        try:
            request = client.build_request(
                "POST",
                BASE_URL + CHAT_PATH + "?" + query,
                headers=headers,
                json=body,
            )
            response = await client.send(request, stream=True)
            if response.status_code >= 400:
                raw = (await response.aread()).decode("utf-8", "replace")
                await response.aclose()
                await client.aclose()
                snippet = re.sub(r"\s+", " ", raw)[:500]
                status = response.status_code
                if status in {401, 403} or "not authenticated" in snippet.lower():
                    raise NativeError(
                        401,
                        "GLM web session is invalid or expired; "
                        "run `uv run freeproxy-auth login glm`.",
                        "upstream_auth_error",
                    )
                if status == 429:
                    raise NativeError(
                        429,
                        f"GLM rate limit: {snippet}",
                        "upstream_rate_limited",
                        retry_after=retry_after_seconds(response),
                    )
                if status == 405 or "blocked as it may cause potential" in snippet:
                    raise NativeError(
                        503,
                        "chat.z.ai WAF blocked this IP; wait and retry later.",
                        "upstream_waf_blocked",
                    )
                raise NativeError(
                    502, f"GLM completion failed (HTTP {status}): {snippet}", "upstream_error"
                )
            return GlmCompletion(client, response, GlmStreamParser(), chat_id, headers)
        except BaseException:
            await client.aclose()
            raise

    async def aclose(self) -> None:
        return None


class GlmService:
    def __init__(self, store_path: Path, transport: GlmTransport) -> None:
        self.store_path = Path(store_path)
        self.transport = transport

    def configured(self) -> bool:
        try:
            data = TokenStore(self.store_path).provider("glm") or {}
        except AuthStoreError:
            return False
        return bool(data.get("token") or data.get("zai_token"))

    def _model(self, payload: dict[str, Any]) -> str:
        return normalize_model(str(payload.get("model") or DEFAULT_MODEL))

    async def open_stream(self, payload: dict[str, Any]) -> GlmCompletion:
        model = self._model(payload)
        prompt = format_prompt(payload, max_chars=MAX_PROMPT_CHARS)
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        return await self.transport.open_completion(prompt, model, tools)

    async def _read(self, payload: dict[str, Any]) -> tuple[str, GlmStreamParser]:
        model = self._model(payload)
        prompt = format_prompt(payload, max_chars=MAX_PROMPT_CHARS)
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        completion = await self.transport.open_completion(prompt, model, tools)
        try:
            async for _content, _reasoning in completion.deltas():
                pass
        finally:
            await completion.aclose()
        return prompt, completion.parser

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = self._model(payload)
        prompt, parser = await self._read(payload)
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        tool_calls = None
        content = parser.content
        finish_reason = parser.finalize()
        if tools:
            parsed = parse_tool_call(content)
            tool_calls = parsed[1] if parsed else None
            if tool_calls:
                content = ""
                finish_reason = "tool_calls"
            elif looks_like_tool_markup(content):
                raise NativeError(
                    502,
                    "GLM returned malformed tool-call markup",
                    "malformed_tool_call",
                )
        return openai_response(model, content, parser.reasoning, tool_calls, finish_reason, prompt)

    async def stream_chat(
        self, payload: dict[str, Any], completion: GlmCompletion
    ) -> AsyncIterator[bytes]:
        model = self._model(payload)
        prompt = format_prompt(payload, max_chars=MAX_PROMPT_CHARS)
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        parser = completion.parser
        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        yield openai_chunk(response_id, created, model, {"role": "assistant"})

        buffered: list[str] = []
        failure: NativeError | None = None
        try:
            async for content_delta, reasoning_delta in completion.deltas():
                if reasoning_delta:
                    yield openai_chunk(
                        response_id, created, model, {"reasoning_content": reasoning_delta}
                    )
                if content_delta:
                    if tools:
                        buffered.append(content_delta)
                    else:
                        yield openai_chunk(response_id, created, model, {"content": content_delta})
        except NativeError as exc:
            failure = exc
        except httpx.HTTPError as exc:
            failure = NativeError(
                503,
                f"GLM network error: {type(exc).__name__}",
                "upstream_unavailable",
            )
        finally:
            await completion.aclose()

        if failure is not None:
            yield openai_error_event(failure.message, failure.code)
            yield b"data: [DONE]\n\n"
            return

        finish_reason = parser.finalize()
        tool_calls: list[dict[str, Any]] | None = None
        content = parser.content
        if tools:
            parsed = parse_tool_call(content)
            tool_calls = parsed[1] if parsed else None
            if tool_calls:
                content = ""
                finish_reason = "tool_calls"
            else:
                content = "".join(buffered)
                if looks_like_tool_markup(content):
                    yield openai_error_event(
                        "GLM returned malformed tool-call markup",
                        "malformed_tool_call",
                    )
                    yield b"data: [DONE]\n\n"
                    return
            for start in range(0, len(content), 120):
                yield openai_chunk(
                    response_id, created, model, {"content": content[start : start + 120]}
                )
        if tool_calls:
            yield openai_chunk(
                response_id,
                created,
                model,
                {"tool_calls": [{"index": 0, **tool_calls[0]}]},
            )
        yield openai_chunk(
            response_id,
            created,
            model,
            {},
            finish_reason=finish_reason,
            usage={
                "prompt_tokens": max(1, len(prompt) // 4),
                "completion_tokens": max(0, (len(content) + len(parser.reasoning)) // 4),
                "total_tokens": max(1, len(prompt) // 4)
                + max(0, (len(content) + len(parser.reasoning)) // 4),
            },
        )
        yield b"data: [DONE]\n\n"


def create_glm_app(
    store_path: Path | str | None = None,
    *,
    transport: GlmTransport | None = None,
    captcha_solver: GlmCaptchaSolver | None = None,
    captcha_access_key: str = "",
    captcha_secret_key: str = "",
    tokens_db: Path | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
    timeout_seconds: float = 600.0,
) -> FastAPI:
    paths = AuthPaths.from_env()
    resolved_store = Path(store_path or paths.store)
    if transport is None:
        if captcha_solver is None:
            captcha_solver = GlmCaptchaSolver(
                access_key=captcha_access_key,
                secret_key=captcha_secret_key,
                tokens_db=tokens_db,
            )
        transport = GlmHttpTransport(
            resolved_store,
            captcha_solver=captcha_solver,
            transport=http_transport,
            timeout_seconds=timeout_seconds,
        )
    service = GlmService(resolved_store, transport)
    app = FastAPI()
    app.state.close = transport.aclose

    @app.get("/health")
    async def health() -> JSONResponse:
        configured = await asyncio.to_thread(service.configured)
        return JSONResponse(
            status_code=200 if configured else 503,
            content={"healthy": configured, "mode": "native-glm"},
        )

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "created": 0, "owned_by": "glm"}
                for model in MODELS
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "Invalid JSON", "type": "invalid_request_error"}},
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "Request body must be an object",
                        "type": "invalid_request_error",
                    }
                },
            )
        if not isinstance(payload.get("messages"), list) or not payload["messages"]:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": "messages must be a non-empty array",
                        "type": "invalid_request_error",
                    }
                },
            )
        try:
            if not await asyncio.to_thread(service.configured):
                raise NativeError(
                    503,
                    "GLM token is not configured. Run `uv run freeproxy-auth login glm`.",
                    "provider_not_configured",
                )
            if payload.get("stream") is True:
                completion = await service.open_stream(payload)
                return StreamingResponse(
                    service.stream_chat(payload, completion),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            return await service.chat(payload)
        except NativeError as exc:
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "message": exc.message,
                        "type": "upstream_error",
                        "code": exc.code,
                        "param": None,
                    }
                },
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("native GLM network error: %s", type(exc).__name__)
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": f"GLM network error: {type(exc).__name__}",
                        "type": "upstream_error",
                        "code": "upstream_unavailable",
                        "param": None,
                    }
                },
            )
        except Exception as exc:
            logger.exception("native GLM error")
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": f"Native GLM error: {exc}",
                        "type": "upstream_error",
                        "code": "native_error",
                        "param": None,
                    }
                },
            )

    return app
