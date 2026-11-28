from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
import uuid
from collections.abc import AsyncIterator, Collection
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
MODELS_PATH = "/api/v2/models"
DEFAULT_MODEL = "qwen3.7-max"
MAX_PROMPT_CHARS = 80_000
WAF_MARKERS = ("_____tmd_____", "punish")
# Discovered models are cached per process. /v1/models never launches a
# browser: it reports the account list only while the page is already warm.
MODEL_CACHE_TTL_SECONDS = 300.0
MODEL_DISCOVERY_RETRY_SECONDS = 30.0
MODEL_NOT_FOUND_CODES = frozenset({"Not_Found", "Model_Not_Found"})
# Cleanup (handle removal, chat deletion) must never hang a request or a
# disconnect path, so every step shares this small hard deadline.
CLEANUP_TIMEOUT_SECONDS = 5.0
# First-response bound. The provider timeout (often 600s) allows long
# generations, but the *first* round-trip (token, create-chat, completion POST
# headers) must fail fast when the WAF challenges us or the page stalls.
FIRST_RESPONSE_TIMEOUT_SECONDS = 90.0
# Default idle window for QwenBrowserCompletion (overridden by open_completion
# with the provider timeout: the idle timeout is per-event, never per-stream).
DEFAULT_STREAM_IDLE_SECONDS = 60.0


def _waf_hint(text: Any) -> str:
    """Return a short hint when a body looks like the Aliyun punish challenge."""
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in WAF_MARKERS):
        return " (Aliyun WAF challenge)"
    return ""


def _error_facts(fragment: Any) -> tuple[str, str]:
    """Extract ``(code, details)`` from a Qwen error body or body fragment."""
    if isinstance(fragment, str):
        try:
            fragment = json.loads(fragment)
        except (json.JSONDecodeError, ValueError):
            return "", fragment
    if not isinstance(fragment, dict):
        return "", str(fragment or "")
    data = fragment.get("data")
    source = data if isinstance(data, dict) else fragment
    code = str(source.get("code") or fragment.get("code") or "")
    details = str(
        source.get("details")
        or source.get("detail")
        or source.get("message")
        or fragment.get("details")
        or fragment.get("detail")
        or fragment.get("message")
        or ""
    )
    return code, details


def _looks_model_not_found(*fragments: Any) -> bool:
    for fragment in fragments:
        if fragment is None:
            continue
        code, details = _error_facts(fragment)
        if code in MODEL_NOT_FOUND_CODES:
            return True
        text = details.lower()
        if isinstance(fragment, str):
            text += " " + fragment.lower()
        if "model not found" in text:
            return True
    return False


def _looks_auth_expired(*fragments: Any) -> bool:
    for fragment in fragments:
        if fragment is None:
            continue
        code, details = _error_facts(fragment)
        if code.lower() == "unauthorized":
            return True
        text = details.lower()
        if isinstance(fragment, str):
            text += " " + fragment.lower()
        if "token has expired" in text:
            return True
    return False


def _model_not_found_error(model: str) -> NativeError:
    # The model name is safe to echo; prompts and tokens are not.
    return NativeError(
        404,
        f"Qwen model '{model}' is not available for this account",
        "model_not_found",
    )


def _auth_expired_error() -> NativeError:
    return NativeError(
        401,
        "Qwen session token is missing or expired; run `uv run freeproxy-auth login qwen`.",
        "upstream_auth_error",
    )


def _parse_models_payload(payload: Any) -> list[str] | None:
    """Parse both observed ``/api/v2/models`` shapes, preserving order.

    - ``{"data": {"data": [{"id": ...}, ...]}}`` (v2 nested envelope, verified live)
    - ``{"data": [{"id": ...}, ...]}`` (top-level list shape)
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict):
        entries = data.get("data")
    elif isinstance(data, list):
        entries = data
    else:
        return None
    if not isinstance(entries, list):
        return None
    models: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        value = entry.get("id") if isinstance(entry, dict) else entry
        if not isinstance(value, str):
            continue
        model = value.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models or None


# Static fallback for ``/v1/models`` while the browser page is cold. The
# account advertises its real list via ``/api/v2/models``; ``QwenService``
# prefers that discovered list and only falls back to this curated set of
# confirmed-working models otherwise.
MODELS: tuple[str, ...] = (
    "qwen3.8-max",
    "qwen3.7-max",
    "qwen3.7-plus",
)

# Every alias target must stay inside ``MODELS``: the account no longer
# accepts the old coder/VL/omni IDs, so those aliases resolve to the closest
# working model instead of a dead one. ``qwen-vl-plus`` is intentionally absent
# because no vision model is currently available; it falls back to
# ``DEFAULT_MODEL`` like any other unknown name.
MODEL_ALIASES: dict[str, str] = {
    "qwen-max": "qwen3.8-max",
    "qwen-max-latest": "qwen3.8-max",
    "qwen3": "qwen3.7-plus",
    "qwen3-coder": "qwen3.7-plus",
    "qwen-coder-plus": "qwen3.7-plus",
    "qwen3.7": "qwen3.7-plus",
    "qwen3.6": "qwen3.7-plus",
    "qwen3.5": "qwen3.7-plus",
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


def normalize_model(model: str | None, *, known: Collection[str] | None = None) -> str:
    """Resolve aliases and keep explicitly requested known models intact.

    ``known`` extends the static list with the account's discovered models.
    Unknown names still fall back to ``DEFAULT_MODEL`` as before; the backend
    reports ``model_not_found`` when a known-but-unavailable model is selected.
    """
    value = (model or DEFAULT_MODEL).strip().lower()
    value = MODEL_ALIASES.get(value, value)
    allowed = MODELS if not known else (*MODELS, *known)
    return value if value in allowed else DEFAULT_MODEL


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
        *,
        model: str = DEFAULT_MODEL,
        idle_seconds: float = DEFAULT_STREAM_IDLE_SECONDS,
    ) -> None:
        self.transport = transport
        self.task = task
        self.queue = queue
        self.request_id = request_id
        self.chat_id = chat_id
        self.parser = parser
        self.model = model
        # Applied per event, not to the whole stream: a long completion that
        # keeps emitting chunks is fine, a silent one is not.
        self.idle_seconds = max(idle_seconds, 0.001)
        self._closed = False
        self._aborted = False

    async def _abort_stalled(self) -> None:
        """Cancel the reader, drop the in-page handle and release the lock."""
        self._aborted = True
        await self.transport.abort_request(self.request_id, self.task)

    async def _next(self) -> dict[str, Any]:
        getter = asyncio.create_task(self.queue.get())
        try:
            done, _ = await asyncio.wait(
                {getter, self.task},
                timeout=self.idle_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done:
                return getter.result()
            if not done:
                getter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await getter
                await self._abort_stalled()
                seconds = max(1, round(self.idle_seconds))
                raise NativeError(
                    503,
                    f"Qwen stream stalled: no events for {seconds}s "
                    "(WAF challenge or stalled upstream)",
                    "upstream_unavailable",
                )
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await getter
            if self.task.cancelled():
                raise NativeError(
                    503,
                    "Qwen browser request was cancelled",
                    "upstream_unavailable",
                )
            exc = self.task.exception()
            if isinstance(exc, NativeError):
                raise exc
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
                fragments = (item.get("errorBody"), item.get("nonSse"))
                if _looks_model_not_found(*fragments):
                    raise _model_not_found_error(self.model)
                if _looks_auth_expired(*fragments) or status in {401, 403}:
                    raise _auth_expired_error()
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
                    hint = _waf_hint(non_sse)
                    raise NativeError(
                        502,
                        f"Qwen returned a non-SSE response{hint}: {str(non_sse)[:300]}",
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
            await asyncio.wait_for(self.task, timeout=CLEANUP_TIMEOUT_SECONDS)
        if not self._aborted:
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

    async def _auth_async(self) -> dict[str, Any]:
        """Read the auth store off the event loop (it does blocking file I/O)."""
        return await asyncio.to_thread(self._auth)

    @property
    def _timeout_seconds(self) -> float:
        return max(self.timeout_ms / 1000.0, 0.001)

    @property
    def _cleanup_timeout_seconds(self) -> float:
        return min(self._timeout_seconds, CLEANUP_TIMEOUT_SECONDS)

    @property
    def _first_response_timeout_seconds(self) -> float:
        """Hard bound for the first round-trip, independent of long-gen timeout."""
        return min(self._timeout_seconds, FIRST_RESPONSE_TIMEOUT_SECONDS)

    def _timeout_error(self, action: str, seconds: float | None = None) -> NativeError:
        effective = self._timeout_seconds if seconds is None else max(seconds, 0.001)
        rounded = max(1, round(effective))
        return NativeError(
            503,
            f"Qwen {action} timed out after {rounded}s (WAF challenge or stalled upstream)",
            "upstream_unavailable",
        )

    async def _bounded_evaluate(
        self,
        page: Any,
        script: str,
        arg: dict[str, Any],
        *,
        action: str,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Run one page.evaluate call with a hard deadline."""
        effective = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            return await asyncio.wait_for(
                page.evaluate(script, arg),
                timeout=effective,
            )
        except TimeoutError as exc:
            raise self._timeout_error(action, effective) from exc

    async def _drop_response_handle(self, request_id: str) -> None:
        """Best-effort removal of the in-page response handle.

        Bounded and fully suppressed: it runs on error paths where the page is
        already suspected of being stuck.
        """
        page = self._page
        if page is None or not request_id:
            return
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(
                page.evaluate(
                    """({id}) => {
                        try {
                            const responses = window.__fp_qwen_responses || {};
                            delete responses[id];
                        } catch (e) {}
                    }""",
                    {"id": request_id},
                ),
                timeout=self._cleanup_timeout_seconds,
            )

    async def abort_request(self, request_id: str, task: asyncio.Task[Any]) -> None:
        """Tear down a stalled stream: cancel the reader and free the slot.

        Every step is best effort and bounded so a wedged page cannot hang the
        error path. The lock is released only when this request still owns it,
        so a later ``aclose`` cannot release a newer request's slot.
        """
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=self._cleanup_timeout_seconds)
        if self._active_id != request_id:
            return
        self._queue = None
        self._active_id = None
        await self._drop_response_handle(request_id)
        if self._request_lock.locked():
            self._request_lock.release()

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
            # Read the auth store before acquiring Playwright: the blocking read
            # must not happen after browser resources exist, so a slow or
            # cancelled read never leaves a half-started browser behind.
            auth = await self._auth_async()
            cookies = self._normalize_cookies(auth.get("cookies"))
            token = str(auth.get("token") or "")
            from playwright.async_api import async_playwright

            try:
                self._playwright = await async_playwright().start()
                launch_args = ["--disable-blink-features=AutomationControlled"]

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
        # Resolving the short-lived token is a first round-trip: bound it so a
        # wedged page cannot sit on the provider timeout.
        effective = self._first_response_timeout_seconds
        for attempt in range(30):
            try:
                token = await asyncio.wait_for(
                    page.evaluate("() => localStorage.getItem('token')"),
                    timeout=effective,
                )
            except TimeoutError as exc:
                raise self._timeout_error("token lookup", effective) from exc
            except Exception:
                token = None
            if token:
                return str(token)
            if attempt == 3:
                with contextlib.suppress(Exception):
                    await page.reload(
                        wait_until="domcontentloaded",
                        timeout=max(1, round(effective * 1000)),
                    )
            await asyncio.sleep(0.5)
        raise NativeError(
            401,
            "Qwen session token is missing or expired; run `uv run freeproxy-auth login qwen`.",
            "upstream_auth_error",
        )

    async def list_models(self) -> list[str] | None:
        """Read the account's model list from an already-started page.

        Never launches a browser: ``/v1/models`` must not start Playwright.
        Any failure (page gone, WAF, timeout, malformed body) returns ``None``
        so the caller can keep the static fallback.
        """
        page = self._page
        if page is None:
            return None
        try:
            result = await asyncio.wait_for(
                page.evaluate(
                    """async ({url}) => {
                        try {
                            const res = await fetch(url, {
                                method: 'GET',
                                credentials: 'include',
                                headers: {Accept: 'application/json'},
                            });
                            const text = await res.text();
                            let data = null;
                            try { data = JSON.parse(text); } catch (e) {}
                            return {status: res.status, data, text: text.slice(0, 4000)};
                        } catch (error) {
                            return {status: 0, error: String(error)};
                        }
                    }""",
                    {"url": BASE_URL + MODELS_PATH},
                ),
                timeout=self._first_response_timeout_seconds,
            )
        except Exception:
            logger.debug("Qwen model discovery failed", exc_info=True)
            return None
        if not isinstance(result, dict):
            return None
        status = int(result.get("status") or 0)
        body_text = result.get("text")
        if _waf_hint(body_text):
            logger.debug("Qwen model discovery hit an Aliyun WAF challenge")
            return None
        if status != 200:
            logger.debug("Qwen model discovery returned HTTP %s", status)
            return None
        data = result.get("data")
        if isinstance(data, dict) and data.get("success") is False:
            logger.debug("Qwen model discovery was rejected upstream")
            return None
        models = _parse_models_payload(data)
        if models is None:
            logger.debug("Qwen model discovery returned no model ids")
        return models

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
        result = await self._bounded_evaluate(
            page,
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
            action="create-chat",
            timeout_seconds=self._first_response_timeout_seconds,
        )
        status = int(result.get("status") or 0)
        data = result.get("data")
        if status == 0:
            raise NativeError(
                503,
                f"Qwen create-chat failed: {str(result.get('error'))[:300]}",
                "upstream_unavailable",
            )
        fragments = (data, result.get("text"))
        if _looks_model_not_found(*fragments):
            raise _model_not_found_error(model)
        if _looks_auth_expired(*fragments) or status in {401, 403}:
            raise _auth_expired_error()
        if status >= 400:
            raise NativeError(
                429 if status == 429 else 502,
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
            await asyncio.wait_for(
                page.evaluate(
                    """async ({url, headers}) => {
                        try {
                            await fetch(url, {method: 'DELETE', credentials: 'include', headers});
                        } catch (e) {}
                    }""",
                    {"url": BASE_URL + f"/api/v2/chats/{chat_id}", "headers": headers},
                ),
                timeout=self._cleanup_timeout_seconds,
            )

    async def open_completion(
        self, prompt: str, model: str, tools: list[dict[str, Any]]
    ) -> QwenCompletion:
        await self._ensure_started()
        await self._request_lock.acquire()
        committed = False
        request_id: str | None = None
        try:
            auth = await self._auth_async()
            token = await self._token()
            headers = self._headers(token, auth)
            chat_id = await self._create_chat(model, headers, "OpenAI request")
            payload = build_chat_payload(prompt, model, chat_id)
            request_id = uuid.uuid4().hex
            page = self._page
            assert page is not None
            started = await self._bounded_evaluate(
                page,
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
                action="completion",
                timeout_seconds=self._first_response_timeout_seconds,
            )
            status = int(started.get("status") or 0)
            if status == 0:
                raise NativeError(
                    503,
                    f"Qwen browser request failed: {str(started.get('error'))[:300]}",
                    "upstream_unavailable",
                )
            fragments = (started.get("errorText"), started.get("nonSse"))
            if _looks_model_not_found(*fragments):
                raise _model_not_found_error(model)
            if _looks_auth_expired(*fragments) or status in {401, 403}:
                raise _auth_expired_error()
            if status >= 400:
                hint = _waf_hint(started.get("errorText"))
                raise NativeError(
                    429 if status == 429 else 502,
                    f"Qwen completion failed (HTTP {status}){hint}: "
                    f"{str(started.get('errorText') or '')[:300]}",
                    "upstream_error",
                )
            if started.get("nonSse") is not None:
                hint = _waf_hint(started.get("nonSse"))
                raise NativeError(
                    502,
                    f"Qwen returned a non-SSE response{hint}: {str(started.get('nonSse'))[:300]}",
                    "upstream_error",
                )
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            self._queue = queue
            self._active_id = request_id
            task = asyncio.create_task(self._read_stream(page, request_id))
            completion = QwenBrowserCompletion(
                self,
                task,
                queue,
                request_id,
                chat_id,
                QwenStreamParser(),
                model=model,
                idle_seconds=self._timeout_seconds,
            )
            committed = True
            return completion
        finally:
            if not committed:
                self._queue = None
                self._active_id = None
                if request_id is not None:
                    await self._drop_response_handle(request_id)
                if self._request_lock.locked():
                    self._request_lock.release()

    async def _read_stream(self, page: Any, request_id: str) -> None:
        """Drain the in-page SSE body.

        No total deadline here: legitimate completions can stream for minutes.
        ``QwenBrowserCompletion`` enforces an idle timeout per event instead.
        """
        await page.evaluate(
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

    async def _bounded_cleanup(self, awaitable: Any, label: str) -> Any:
        """Await one cleanup step under the cleanup deadline, swallowing errors.

        Cleanup runs on normal completion, error paths and client disconnects.
        A wedged page or slow auth store must never turn teardown into a hang.
        """
        try:
            return await asyncio.wait_for(awaitable, timeout=self._cleanup_timeout_seconds)
        except Exception:
            logger.debug("Qwen cleanup step '%s' failed", label, exc_info=True)
            return None

    async def finish_request(self, request_id: str, chat_id: str) -> None:
        owned = self._active_id == request_id
        try:
            if owned:
                self._queue = None
                self._active_id = None
            await self._drop_response_handle(request_id)
            auth = await self._bounded_cleanup(self._auth_async(), "auth store")
            token = await self._bounded_cleanup(self._token(), "token lookup")
            if token and chat_id:
                headers = self._headers(str(token), auth if isinstance(auth, dict) else {})
                await self._bounded_cleanup(self._delete_chat(chat_id, headers), "chat delete")
        finally:
            # Only release the slot we still own; a stalled request may have
            # freed it already and a newer request may hold it now.
            if owned and self._request_lock.locked():
                self._request_lock.release()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._close_browser()


class QwenService:
    def __init__(
        self,
        store_path: Path,
        transport: QwenTransport,
        *,
        model_ttl_seconds: float = MODEL_CACHE_TTL_SECONDS,
    ) -> None:
        self.store_path = Path(store_path)
        self.transport = transport
        ttl = float(model_ttl_seconds)
        self.model_ttl_seconds = ttl if math.isfinite(ttl) and ttl > 0 else MODEL_CACHE_TTL_SECONDS
        self._models: list[str] | None = None
        self._models_expires_at = 0.0
        self._models_retry_at = 0.0
        self._models_lock = asyncio.Lock()
        self._models_task: asyncio.Task[None] | None = None

    def configured(self) -> bool:
        try:
            data = TokenStore(self.store_path).provider("qwen") or {}
        except AuthStoreError:
            return False
        return bool(data.get("token"))

    async def models(self) -> list[str]:
        """Return the account's discovered models, or the static fallback.

        Discovery is best-effort and bounded by the transport's first-response
        timeout. Failures are cached for a short retry window so a stalled page
        cannot add that latency to every ``/v1/models`` call.
        """
        now = time.monotonic()
        if self._models is not None and now < self._models_expires_at:
            return list(self._models)
        if now < self._models_retry_at:
            return list(self._models or MODELS)
        async with self._models_lock:
            now = time.monotonic()
            if self._models is not None and now < self._models_expires_at:
                return list(self._models)
            if now < self._models_retry_at:
                return list(self._models or MODELS)
            discovered = await self._discover_models()
            now = time.monotonic()
            if discovered:
                self._models = discovered
                self._models_expires_at = now + self.model_ttl_seconds
                self._models_retry_at = 0.0
                return list(discovered)
            self._models_retry_at = now + MODEL_DISCOVERY_RETRY_SECONDS
            return list(self._models or MODELS)

    async def _discover_models(self) -> list[str] | None:
        discover = getattr(self.transport, "list_models", None)
        if not callable(discover):
            return None
        try:
            discovered = await discover()
        except Exception:
            logger.debug("Qwen model discovery failed", exc_info=True)
            return None
        if not isinstance(discovered, list):
            return None
        models: list[str] = []
        seen: set[str] = set()
        for item in discovered:
            if not isinstance(item, str):
                continue
            model = item.strip()
            if not model or model in seen:
                continue
            seen.add(model)
            models.append(model)
        return models or None

    def _schedule_models_refresh(self) -> None:
        """Warm the cache after a chat made the browser page ready.

        Only runs against a started browser transport; the task is owned by
        the service and cancelled in :meth:`aclose` (app shutdown).
        """
        if getattr(self.transport, "_page", None) is None:
            return
        if not callable(getattr(self.transport, "list_models", None)):
            return
        now = time.monotonic()
        if self._models is not None and now < self._models_expires_at:
            return
        if self._models_task is not None and not self._models_task.done():
            return
        self._models_task = asyncio.create_task(self._refresh_models())

    async def _refresh_models(self) -> None:
        try:
            await self.models()
        except Exception:
            logger.debug("Qwen background model discovery failed", exc_info=True)

    async def aclose(self) -> None:
        task, self._models_task = self._models_task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def _model(self, payload: dict[str, Any]) -> str:
        return normalize_model(str(payload.get("model") or DEFAULT_MODEL), known=self._models)

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
        response = openai_response(
            model, content, parser.reasoning, tool_calls, finish_reason, prompt
        )
        self._schedule_models_refresh()
        return response

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
        self._schedule_models_refresh()


def create_qwen_app(
    store_path: Path | str | None = None,
    *,
    transport: QwenTransport | None = None,
    headless: bool = True,
    model_ttl_seconds: float = MODEL_CACHE_TTL_SECONDS,
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
    service = QwenService(resolved_store, transport, model_ttl_seconds=model_ttl_seconds)
    app = FastAPI()

    async def close() -> None:
        await service.aclose()
        await transport.aclose()

    app.state.service = service
    app.state.close = close

    @app.get("/health")
    async def health() -> JSONResponse:
        configured = await asyncio.to_thread(service.configured)
        return JSONResponse(
            status_code=200 if configured else 503,
            content={"healthy": configured, "mode": "native-qwen"},
        )

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        available = await service.models()
        return {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "created": 0, "owned_by": "qwen"}
                for model in available
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
