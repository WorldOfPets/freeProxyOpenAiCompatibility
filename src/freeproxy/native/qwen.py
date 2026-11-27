from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
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
)

logger = logging.getLogger("freeproxy.native.qwen")

BASE_URL = "https://chat.qwen.ai"
CHAT_API_PATH = "/api/v2/chat/completions"
CREATE_CHAT_PATH = "/api/v2/chats/new"
DEFAULT_MODEL = "qwen3.7-max"
MAX_PROMPT_CHARS = 80_000

# Canonical web-chat model IDs. The web API silently falls back when a model
# is unknown, so this list is used for validation and /v1/models.
MODELS: tuple[str, ...] = (
    "qwen3.8-max",
    "qwen3.8-omni-flash",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-plus",
    "qwen3.5-plus",
    "qwen3.5-omni-plus",
    "qwen3-coder-plus",
    "qwen3-vl-plus",
    "qwen3-max",
    "qwen3-235b-a22b",
)

MODEL_ALIASES: dict[str, str] = {
    "qwen-max": "qwen3-max",
    "qwen-max-latest": "qwen3-max",
    "qwen3": "qwen3-235b-a22b",
    "qwen3-coder": "qwen3-coder-plus",
    "qwen-coder-plus": "qwen3-coder-plus",
    "qwen-vl-plus": "qwen3-vl-plus",
    "qwen3.7": "qwen3.7-plus",
    "qwen3.6": "qwen3.6-plus",
    "qwen3.5": "qwen3.5-plus",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

_FILTERED_HEADERS = {
    "cookie",
    "content-length",
    "host",
    "authorization",
    "accept-encoding",
    "connection",
    "origin",
    "referer",
}


def normalize_model(model: str | None) -> str:
    value = (model or DEFAULT_MODEL).strip().lower()
    value = MODEL_ALIASES.get(value, value)
    return value if value in MODELS else DEFAULT_MODEL


def build_create_chat_payload(model: str, title: str = "New chat") -> dict[str, Any]:
    return {
        "title": title,
        "models": [model],
        "chat_mode": "normal",
        "chat_type": "t2t",
        "timestamp": int(time.time() * 1000),
    }


def build_chat_payload(
    prompt: str,
    model: str,
    chat_id: str,
    *,
    parent_id: str | None = None,
) -> dict[str, Any]:
    user_message_id = str(uuid.uuid4())
    assistant_child_id = str(uuid.uuid4())
    timestamp = int(time.time())
    return {
        "stream": True,
        "incremental_output": True,
        "chat_id": chat_id,
        "chat_mode": "normal",
        "messages": [
            {
                "fid": user_message_id,
                "parentId": parent_id,
                "parent_id": parent_id,
                "role": "user",
                "content": prompt,
                "chat_type": "t2t",
                "sub_chat_type": "t2t",
                "timestamp": timestamp,
                "user_action": "chat",
                "models": [model],
                "files": [],
                "childrenIds": [assistant_child_id],
                "extra": {"meta": {"subChatType": "t2t"}},
                "feature_config": {
                    "thinking_enabled": False,
                    "output_schema": "phase",
                },
            }
        ],
        "model": model,
        "parent_id": parent_id,
        "timestamp": timestamp,
    }


class QwenStreamParser:
    """Accumulates Qwen web SSE chunks and exposes provider-agnostic deltas."""

    def __init__(self) -> None:
        self.content = ""
        self.reasoning = ""
        self.response_id: str | None = None
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.error: NativeError | None = None
        self.finished = False

    def handle_payload(self, data: dict[str, Any]) -> tuple[str, str]:
        if not isinstance(data, dict):
            return "", ""

        code = data.get("code")
        if code == "RateLimited" or (code and data.get("detail")):
            self.error = NativeError(
                429,
                str(data.get("detail") or data.get("message") or "Qwen rate limit"),
                "upstream_rate_limited",
            )
            self.finished = True
            return "", ""
        if data.get("error") and not data.get("choices"):
            self.error = NativeError(502, str(data.get("error"))[:500], "upstream_error")
            self.finished = True
            return "", ""
        if data.get("success") is False:
            self.error = NativeError(
                502,
                str(data.get("message") or data.get("detail") or "Qwen error")[:500],
                "upstream_error",
            )
            self.finished = True
            return "", ""

        created = data.get("response.created")
        if isinstance(created, dict) and created.get("response_id"):
            self.response_id = str(created["response_id"])

        content_delta = ""
        reasoning_delta = ""
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    content_delta = content
                    self.content += content
                for key in ("reasoning_content", "thinking_content", "reasoning"):
                    reasoning = delta.get(key)
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_delta += reasoning
                        self.reasoning += reasoning
                if delta.get("status") == "finished":
                    self.finished = True
            finish_reason = choice.get("finish_reason")
            if isinstance(finish_reason, str) and finish_reason:
                self.finish_reason = finish_reason
                self.finished = True

        usage = data.get("usage")
        if isinstance(usage, dict):
            self.usage = usage

        return content_delta, reasoning_delta

    def finalize(self) -> str:
        if self.finish_reason:
            return self.finish_reason
        return "stop"


class QwenCompletion(Protocol):
    parser: QwenStreamParser

    def deltas(self) -> AsyncIterator[tuple[str, str]]: ...

    async def aclose(self) -> None: ...


class QwenTransport(Protocol):
    async def open_completion(
        self, prompt: str, model: str, tools: list[dict[str, Any]]
    ) -> QwenCompletion: ...

    async def aclose(self) -> None: ...


class QwenBrowserCompletion:
    def __init__(
        self,
        transport: QwenBrowserTransport,
        task: asyncio.Task[Any],
        queue: asyncio.Queue[dict[str, Any]],
        request_id: str,
        chat_id: str,
        parser: QwenStreamParser,
    ) -> None:
        self.transport = transport
        self.task = task
        self.queue = queue
        self.request_id = request_id
        self.chat_id = chat_id
        self.parser = parser
        self._closed = False

    async def _next(self) -> dict[str, Any]:
        getter = asyncio.create_task(self.queue.get())
        try:
            done, _ = await asyncio.wait({getter, self.task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                return getter.result()
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await getter
            exc = self.task.exception()
            if exc is not None:
                raise NativeError(
                    503,
                    f"Qwen browser request failed: {type(exc).__name__}",
                    "upstream_unavailable",
                )
            return {"id": self.request_id, "done": True, "status": 200}
        finally:
            if not getter.done():
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter

    async def deltas(self) -> AsyncIterator[tuple[str, str]]:
        while not self._closed:
            item = await self._next()
            if item.get("id") not in {None, self.request_id}:
                continue
            if item.get("done"):
                status = int(item.get("status") or 0)
                if status >= 400:
                    raise NativeError(
                        429 if status == 429 else (502 if status < 500 else 503),
                        f"Qwen completion failed (HTTP {status}): "
                        f"{str(item.get('errorBody') or '')[:300]}",
                        "upstream_error",
                    )
                if item.get("error"):
                    raise NativeError(
                        503,
                        f"Qwen browser error: {str(item['error'])[:300]}",
                        "upstream_unavailable",
                    )
                non_sse = item.get("nonSse")
                if non_sse:
                    raise NativeError(
                        502,
                        f"Qwen returned a non-SSE response: {str(non_sse)[:300]}",
                        "upstream_error",
                    )
                break
            line = item.get("line")
            if not isinstance(line, str):
                continue
            if line == "[DONE]":
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            content_delta, reasoning_delta = self.parser.handle_payload(data)
            if self.parser.error is not None:
                raise self.parser.error
            if content_delta or reasoning_delta:
                yield content_delta, reasoning_delta
            if self.parser.finished:
                break

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.task.done():
            self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(self.task, timeout=10)
        await self.transport.finish_request(self.request_id, self.chat_id)


class QwenBrowserTransport:
    """Runs Qwen web fetches inside a real browser page.

    Qwen's Aliyun WAF blocks plain HTTP and the short-lived token is refreshed
    by the web app itself. One page is kept for the process lifetime and requests
    are serialized; the provider config should therefore use max_concurrency=1.
    """

    def __init__(
        self,
        store_path: Path,
        *,
        headless: bool = True,
        channel: str = "chrome",
        timeout_ms: int = 60_000,
        profile_dir: Path | None = None,
    ) -> None:
        self.store_path = Path(store_path)
        self.headless = headless
        self.channel = channel
        self.timeout_ms = timeout_ms
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._start_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._active_id: str | None = None
        self._closed = False

    def _auth(self) -> dict[str, Any]:
        try:
            return TokenStore(self.store_path).provider("qwen") or {}
        except AuthStoreError as exc:
            raise NativeError(
                503,
                f"Qwen auth store is unreadable: {exc}",
                "provider_not_configured",
            ) from exc

    async def _emit(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        if self._queue is None:
            return
        if payload.get("id") != self._active_id:
            return
        await self._queue.put(payload)

    async def _close_context(self) -> None:
        """Close the current context/browser without stopping Playwright.

        Used when the persistent profile turns out to be unusable so the same
        Playwright instance can drive the isolated-context fallback.
        """
        context, browser = self._context, self._browser
        self._page = None
        self._context = None
        self._browser = None
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        if browser is not None and browser is not context:
            with contextlib.suppress(Exception):
                await browser.close()

    async def _close_browser(self) -> None:
        """Release page/context/browser/Playwright, tolerating partial startup."""
        context, browser, playwright = self._context, self._browser, self._playwright
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        if browser is not None and browser is not context:
            with contextlib.suppress(Exception):
                await browser.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()

    async def _ensure_started(self) -> None:
        if self._closed:
            raise NativeError(503, "Qwen browser transport is closed", "native_closed")
        if self._page is not None and not self._page.is_closed():
            return
        async with self._start_lock:
            if self._page is not None and not self._page.is_closed():
                return
            from playwright.async_api import async_playwright

            try:
                self._playwright = await async_playwright().start()
                launch_args = ["--disable-blink-features=AutomationControlled"]
                auth = self._auth()
                cookies = self._normalize_cookies(auth.get("cookies"))
                token = str(auth.get("token") or "")

                if self.profile_dir is not None and self.profile_dir.is_dir():
                    try:
                        self._context = await self._playwright.chromium.launch_persistent_context(
                            user_data_dir=str(self.profile_dir),
                            channel=self.channel,
                            headless=self.headless,
                            args=launch_args,
                            user_agent=USER_AGENT,
                            locale="en-US",
                            timezone_id="Europe/Paris",
                            viewport={"width": 1920, "height": 1080},
                        )
                        self._browser = self._context.browser
                        if cookies:
                            with contextlib.suppress(Exception):
                                await self._context.add_cookies(cookies)
                        if token:
                            await self._context.add_init_script(
                                "try { localStorage.setItem('token', "
                                + json.dumps(token)
                                + "); } catch (e) {}"
                            )
                        page = (
                            self._context.pages[0]
                            if self._context.pages
                            else await self._context.new_page()
                        )
                        await page.goto(
                            BASE_URL + "/",
                            wait_until="domcontentloaded",
                            timeout=self.timeout_ms,
                        )
                        await page.expose_function("__fp_qwen_emit", self._emit)
                        self._page = page
                        return
                    except Exception:
                        logger.warning(
                            "Qwen: persistent profile %s is unusable; using an isolated context",
                            self.profile_dir,
                            exc_info=True,
                        )
                        await self._close_context()

                try:
                    self._browser = await self._playwright.chromium.launch(
                        channel=self.channel, headless=self.headless, args=launch_args
                    )
                except Exception:
                    logger.warning(
                        "Qwen: Chrome channel %s is unavailable; falling back to Chromium",
                        self.channel,
                    )
                    self._browser = await self._playwright.chromium.launch(
                        headless=self.headless, args=launch_args
                    )
                self._context = await self._browser.new_context(
                    user_agent=USER_AGENT,
                    locale="en-US",
                    timezone_id="Europe/Paris",
                    viewport={"width": 1920, "height": 1080},
                )
                if cookies:
                    with contextlib.suppress(Exception):
                        await self._context.add_cookies(cookies)
                if token:
                    script = (
                        "try { localStorage.setItem('token', "
                        + json.dumps(token)
                        + "); } catch (e) {}"
                    )
                    await self._context.add_init_script(script)
                page = await self._context.new_page()
                await page.goto(
                    BASE_URL + "/",
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                await page.expose_function("__fp_qwen_emit", self._emit)
                self._page = page
            except BaseException:
                await self._close_browser()
                raise

    @staticmethod
    def _normalize_cookies(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if not name or value is None:
                continue
            cookie: dict[str, Any] = {
                "name": str(name),
                "value": str(value),
                "domain": str(item.get("domain") or ".qwen.ai"),
                "path": str(item.get("path") or "/"),
            }
            expires = item.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                cookie["expires"] = float(expires)
            same_site = item.get("sameSite")
            if same_site in {"Strict", "Lax", "None"}:
                cookie["sameSite"] = same_site
            if isinstance(item.get("httpOnly"), bool):
                cookie["httpOnly"] = item["httpOnly"]
            if isinstance(item.get("secure"), bool):
                cookie["secure"] = item["secure"]
            out.append(cookie)
        return out

    async def _token(self) -> str:
        page = self._page
        if page is None:
            raise NativeError(503, "Qwen browser is not started", "upstream_unavailable")
        for attempt in range(30):
            try:
                token = await page.evaluate("() => localStorage.getItem('token')")
            except Exception:
                token = None
            if token:
                return str(token)
            if attempt == 3:
                with contextlib.suppress(Exception):
                    await page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
            await asyncio.sleep(0.5)
        raise NativeError(
            401,
            "Qwen session token is missing or expired; run `uv run freeproxy-auth login qwen`.",
            "upstream_auth_error",
        )

    def _headers(self, token: str, auth: dict[str, Any]) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Authorization": f"Bearer {token}",
            "X-Request-Id": str(uuid.uuid4()),
        }
        baxia = auth.get("baxia_headers")
        if isinstance(baxia, dict):
            for key, value in baxia.items():
                if not isinstance(key, str) or key.lower() in _FILTERED_HEADERS:
                    continue
                if value is None:
                    continue
                headers[key] = str(value)
        return headers

    async def _create_chat(self, model: str, headers: dict[str, str], title: str) -> str:
        page = self._page
        if page is None:
            raise NativeError(503, "Qwen browser is not started", "upstream_unavailable")
        payload = build_create_chat_payload(model, title)
        result = await page.evaluate(
            """async ({url, payload, headers}) => {
                try {
                    const res = await fetch(url, {
                        method: 'POST',
                        credentials: 'include',
                        headers,
                        body: JSON.stringify(payload),
                    });
                    const text = await res.text();
                    let data = null;
                    try { data = JSON.parse(text); } catch (e) {}
                    return {status: res.status, data, text: text.slice(0, 4000)};
                } catch (error) {
                    return {status: 0, error: String(error)};
                }
            }""",
            {
                "url": BASE_URL + CREATE_CHAT_PATH,
                "payload": payload,
                "headers": headers,
            },
        )
        status = int(result.get("status") or 0)
        data = result.get("data")
        if status == 0:
            raise NativeError(
                503,
                f"Qwen create-chat failed: {str(result.get('error'))[:300]}",
                "upstream_unavailable",
            )
        if status >= 400:
            raise NativeError(
                429 if status == 429 else (401 if status in {401, 403} else 502),
                f"Qwen create-chat failed (HTTP {status}): {str(result.get('text') or '')[:300]}",
                "upstream_error",
            )
        if not isinstance(data, dict) or data.get("success") is False:
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("message") or data.get("detail") or data)[:300]
            else:
                detail = str(result.get("text") or "")[:300]
            raise NativeError(502, f"Qwen create-chat rejected: {detail}", "upstream_error")
        chat = data.get("data")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        if not chat_id:
            raise NativeError(502, "Qwen create-chat response has no chat id", "upstream_error")
        return str(chat_id)

    async def _delete_chat(self, chat_id: str, headers: dict[str, str]) -> None:
        page = self._page
        if page is None or not chat_id:
            return
        with contextlib.suppress(Exception):
            await page.evaluate(
                """async ({url, headers}) => {
                    try {
                        await fetch(url, {method: 'DELETE', credentials: 'include', headers});
                    } catch (e) {}
                }""",
                {"url": BASE_URL + f"/api/v2/chats/{chat_id}", "headers": headers},
            )

    async def open_completion(
        self, prompt: str, model: str, tools: list[dict[str, Any]]
    ) -> QwenCompletion:
        await self._ensure_started()
        await self._request_lock.acquire()
        try:
            auth = self._auth()
            token = await self._token()
            headers = self._headers(token, auth)
            chat_id = await self._create_chat(model, headers, "OpenAI request")
            payload = build_chat_payload(prompt, model, chat_id)
            request_id = uuid.uuid4().hex
            page = self._page
            assert page is not None
            started = await page.evaluate(
                """async ({id, url, payload, headers}) => {
                    try {
                        const res = await fetch(url, {
                            method: 'POST',
                            credentials: 'include',
                            headers,
                            body: JSON.stringify(payload),
                        });
                        const contentType = res.headers.get('content-type') || '';
                        if (!res.ok) {
                            const text = await res.text();
                            return {status: res.status, contentType, errorText: text.slice(0, 4000)};
                        }
                        if (!contentType.includes('text/event-stream')) {
                            const text = await res.text();
                            return {status: res.status, contentType, nonSse: text.slice(0, 4000)};
                        }
                        window.__fp_qwen_responses = window.__fp_qwen_responses || {};
                        window.__fp_qwen_responses[id] = res;
                        return {status: res.status, contentType, ok: true};
                    } catch (error) {
                        return {status: 0, error: String(error)};
                    }
                }""",
                {
                    "id": request_id,
                    "url": BASE_URL + f"{CHAT_API_PATH}?chat_id={chat_id}",
                    "payload": payload,
                    "headers": headers,
                },
            )
            status = int(started.get("status") or 0)
            if status == 0:
                raise NativeError(
                    503,
                    f"Qwen browser request failed: {str(started.get('error'))[:300]}",
                    "upstream_unavailable",
                )
            if status >= 400:
                raise NativeError(
                    429 if status == 429 else (401 if status in {401, 403} else 502),
                    f"Qwen completion failed (HTTP {status}): "
                    f"{str(started.get('errorText') or '')[:300]}",
                    "upstream_error",
                )
            if started.get("nonSse") is not None:
                raise NativeError(
                    502,
                    f"Qwen returned a non-SSE response: {str(started.get('nonSse'))[:300]}",
                    "upstream_error",
                )
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            self._queue = queue
            self._active_id = request_id
            task = asyncio.create_task(
                page.evaluate(
                    """async ({id}) => {
                        const emit = (data) => window.__fp_qwen_emit({id, ...data});
                        try {
                            const responses = window.__fp_qwen_responses || {};
                            const res = responses[id];
                            delete responses[id];
                            if (!res) {
                                emit({done: true, error: 'Qwen response handle is missing'});
                                return;
                            }
                            const reader = res.body.getReader();
                            const decoder = new TextDecoder();
                            let buffer = '';
                            while (true) {
                                const {done, value} = await reader.read();
                                if (done) break;
                                buffer += decoder.decode(value, {stream: true});
                                let index;
                                while ((index = buffer.indexOf('\\n')) >= 0) {
                                    let line = buffer.slice(0, index);
                                    buffer = buffer.slice(index + 1);
                                    if (line.endsWith('\\r')) line = line.slice(0, -1);
                                    if (line.startsWith('data:')) {
                                        emit({line: line.slice(5).trimStart()});
                                    }
                                }
                            }
                            if (buffer.startsWith('data:')) {
                                emit({line: buffer.slice(5).trimStart()});
                            }
                            emit({done: true, status: res.status});
                        } catch (error) {
                            emit({done: true, error: String(error)});
                        }
                    }""",
                    {"id": request_id},
                )
            )
            return QwenBrowserCompletion(self, task, queue, request_id, chat_id, QwenStreamParser())
        except BaseException:
            self._queue = None
            self._active_id = None
            self._request_lock.release()
            raise

    async def finish_request(self, request_id: str, chat_id: str) -> None:
        try:
            if self._active_id == request_id:
                self._queue = None
                self._active_id = None
            auth = self._auth()
            token = await self._token()
            await self._delete_chat(chat_id, self._headers(token, auth))
        except Exception:
            logger.debug("Qwen chat cleanup failed", exc_info=True)
        finally:
            if self._request_lock.locked():
                self._request_lock.release()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._close_browser()


class QwenService:
    def __init__(self, store_path: Path, transport: QwenTransport) -> None:
        self.store_path = Path(store_path)
        self.transport = transport

    def configured(self) -> bool:
        try:
            data = TokenStore(self.store_path).provider("qwen") or {}
        except AuthStoreError:
            return False
        return bool(data.get("token"))

    def _model(self, payload: dict[str, Any]) -> str:
        return normalize_model(str(payload.get("model") or DEFAULT_MODEL))

    async def open_stream(self, payload: dict[str, Any]) -> QwenCompletion:
        model = self._model(payload)
        prompt = format_prompt(payload, max_chars=MAX_PROMPT_CHARS)
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        return await self.transport.open_completion(prompt, model, tools)

    async def _read(self, payload: dict[str, Any]) -> tuple[str, QwenStreamParser]:
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
                    "Qwen returned malformed tool-call markup",
                    "malformed_tool_call",
                )
        return openai_response(model, content, parser.reasoning, tool_calls, finish_reason, prompt)

    async def stream_chat(
        self, payload: dict[str, Any], completion: QwenCompletion
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
                f"Qwen network error: {type(exc).__name__}",
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
                        "Qwen returned malformed tool-call markup",
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
            usage=(
                parser.usage
                if parser.usage
                else {
                    "prompt_tokens": max(1, len(prompt) // 4),
                    "completion_tokens": max(0, (len(content) + len(parser.reasoning)) // 4),
                    "total_tokens": max(1, len(prompt) // 4)
                    + max(0, (len(content) + len(parser.reasoning)) // 4),
                }
            ),
        )
        yield b"data: [DONE]\n\n"


def create_qwen_app(
    store_path: Path | str | None = None,
    *,
    transport: QwenTransport | None = None,
    headless: bool = True,
) -> FastAPI:
    paths = AuthPaths.from_env()
    resolved_store = Path(store_path or paths.store)
    if transport is None:
        transport = QwenBrowserTransport(
            resolved_store,
            headless=headless,
            channel=paths.browser_channel,
            timeout_ms=paths.timeout_seconds * 1000,
            profile_dir=paths.browser_profile("qwen"),
        )
    service = QwenService(resolved_store, transport)
    app = FastAPI()
    app.state.close = transport.aclose

    @app.get("/health")
    async def health() -> JSONResponse:
        configured = await asyncio.to_thread(service.configured)
        return JSONResponse(
            status_code=200 if configured else 503,
            content={"healthy": configured, "mode": "native-qwen"},
        )

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "created": 0, "owned_by": "qwen"}
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
                    "Qwen token is not configured. Run `uv run freeproxy-auth login qwen`.",
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
            logger.warning("native Qwen network error: %s", type(exc).__name__)
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": f"Qwen network error: {type(exc).__name__}",
                        "type": "upstream_error",
                        "code": "upstream_unavailable",
                        "param": None,
                    }
                },
            )
        except Exception as exc:
            logger.exception("native Qwen error")
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": f"Native Qwen error: {exc}",
                        "type": "upstream_error",
                        "code": "native_error",
                        "param": None,
                    }
                },
            )

    return app
