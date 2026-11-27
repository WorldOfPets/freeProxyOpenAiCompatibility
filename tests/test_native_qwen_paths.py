"""Error, cleanup and streaming path tests for the native Qwen provider.

Everything browser-shaped is faked: no Playwright, Chrome or network access.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

import freeproxy.native.qwen as qwen_module
from freeproxy.auth.store import TokenStore
from freeproxy.native.common import NativeError
from freeproxy.native.qwen import (
    MODELS,
    QwenBrowserCompletion,
    QwenBrowserTransport,
    QwenService,
    QwenStreamParser,
    create_qwen_app,
)

HEX_ID = re.compile(r"^[0-9a-f]{32}$")
UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# --- fakes -----------------------------------------------------------------


class RecordingTransport:
    def __init__(self) -> None:
        self.finished: list[tuple[str, str]] = []

    async def finish_request(self, request_id: str, chat_id: str) -> None:
        self.finished.append((request_id, chat_id))


class FakePage:
    """Minimal page double: results are returned by successive evaluate calls."""

    def __init__(
        self,
        results: list[Any] | None = None,
        *,
        error: BaseException | None = None,
        token: str | None = None,
        token_after_reload: bool = False,
    ) -> None:
        self.results = list(results or [])
        self.error = error
        self.token = token
        self.token_after_reload = token_after_reload
        self.reloads = 0
        self.evaluations = 0
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.evaluations += 1
        if self.error is not None:
            raise self.error
        if self.token is not None:
            if self.token_after_reload and not self.reloads:
                return None
            return self.token
        return self.results.pop(0) if self.results else None

    async def reload(self, **kwargs: Any) -> None:
        self.reloads += 1


async def _done_task(value: Any = None) -> asyncio.Task[Any]:
    async def runner() -> Any:
        return value

    task = asyncio.create_task(runner())
    await task
    return task


async def _failed_task() -> asyncio.Task[Any]:
    async def runner() -> Any:
        raise RuntimeError("browser crashed")

    task = asyncio.create_task(runner())
    with pytest.raises(RuntimeError):
        await task
    return task


def _pending_task() -> asyncio.Task[Any]:
    return asyncio.create_task(asyncio.sleep(3600))


async def _drop_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _queue_completion(
    transport: Any,
    items: list[dict[str, Any]],
    *,
    request_id: str = "req-1",
    chat_id: str = "chat-1",
) -> QwenBrowserCompletion:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)
    return QwenBrowserCompletion(
        transport,
        await _done_task(),
        queue,
        request_id,
        chat_id,
        QwenStreamParser(),
    )


async def _collect(completion: QwenBrowserCompletion) -> list[tuple[str, str]]:
    return [pair async for pair in completion.deltas()]


# --- stream parser ---------------------------------------------------------


def test_parser_ignores_non_dict_payload():
    parser = QwenStreamParser()
    assert parser.handle_payload(["not", "a", "dict"]) == ("", "")  # type: ignore[arg-type]
    assert parser.error is None
    assert parser.finished is False


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"error": "boom"}, "boom"),
        ({"success": False, "message": "nope"}, "nope"),
        ({"success": False, "detail": "detail-no"}, "detail-no"),
    ],
)
def test_parser_turns_error_payloads_into_native_errors(payload, message):
    parser = QwenStreamParser()
    assert parser.handle_payload(payload) == ("", "")
    assert parser.error is not None
    assert parser.error.status_code == 502
    assert parser.error.code == "upstream_error"
    assert parser.error.message == message
    assert parser.finished is True


def test_parser_records_finish_reason_and_finalize():
    parser = QwenStreamParser()
    parser.handle_payload({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
    assert parser.finish_reason == "length"
    assert parser.finished is True
    assert parser.finalize() == "length"


def test_parser_finalize_defaults_to_stop():
    assert QwenStreamParser().finalize() == "stop"


# --- browser completion ----------------------------------------------------


async def test_completion_next_returns_queued_item():
    transport = RecordingTransport()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    queue.put_nowait({"id": "req-1", "line": "data"})
    task = _pending_task()
    try:
        completion = QwenBrowserCompletion(
            transport, task, queue, "req-1", "chat-1", QwenStreamParser()
        )
        assert await completion._next() == {"id": "req-1", "line": "data"}
    finally:
        await _drop_task(task)


async def test_completion_next_maps_task_exception_to_503():
    transport = RecordingTransport()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    completion = QwenBrowserCompletion(
        transport, await _failed_task(), queue, "req-1", "chat-1", QwenStreamParser()
    )
    with pytest.raises(NativeError) as info:
        await completion._next()
    assert info.value.status_code == 503
    assert info.value.code == "upstream_unavailable"


async def test_completion_next_synthesises_done_when_task_is_finished():
    transport = RecordingTransport()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    completion = QwenBrowserCompletion(
        transport, await _done_task(), queue, "req-1", "chat-1", QwenStreamParser()
    )
    assert await completion._next() == {"id": "req-1", "done": True, "status": 200}


async def test_completion_deltas_skips_foreign_ids_and_bad_lines():
    completion = await _queue_completion(
        RecordingTransport(),
        [
            {"id": "someone-else", "line": '{"choices":[{"delta":{"content":"nope"}}]}'},
            {"id": "req-1", "line": 42},
            {"id": "req-1", "line": "not-json"},
            {
                "id": "req-1",
                "line": json.dumps(
                    {
                        "choices": [
                            {"index": 0, "delta": {"content": "hi", "reasoning_content": "why"}}
                        ]
                    }
                ),
            },
            {"id": "req-1", "line": "[DONE]"},
        ],
    )
    assert await _collect(completion) == [("hi", "why")]


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, 429), (401, 502), (400, 502), (500, 503), (503, 503)],
)
async def test_completion_deltas_maps_done_status(status, expected):
    completion = await _queue_completion(
        RecordingTransport(),
        [{"id": "req-1", "done": True, "status": status, "errorBody": "upstream said no"}],
    )
    with pytest.raises(NativeError) as info:
        await _collect(completion)
    assert info.value.status_code == expected
    assert info.value.code == "upstream_error"


async def test_completion_deltas_done_error_and_non_sse():
    error_completion = await _queue_completion(
        RecordingTransport(),
        [{"id": "req-1", "done": True, "status": 200, "error": "reader blew up"}],
    )
    with pytest.raises(NativeError) as info:
        await _collect(error_completion)
    assert (info.value.status_code, info.value.code) == (503, "upstream_unavailable")

    non_sse_completion = await _queue_completion(
        RecordingTransport(),
        [{"id": "req-1", "done": True, "status": 200, "nonSse": "<html>login</html>"}],
    )
    with pytest.raises(NativeError) as info:
        await _collect(non_sse_completion)
    assert (info.value.status_code, info.value.code) == (502, "upstream_error")


async def test_completion_deltas_stops_on_successful_done_item():
    completion = await _queue_completion(
        RecordingTransport(),
        [
            {"id": "req-1", "done": True, "status": 200},
            {"id": "req-1", "line": json.dumps({"choices": [{"delta": {"content": "late"}}]})},
        ],
    )
    assert await _collect(completion) == []


async def test_completion_deltas_stops_on_parser_error():
    completion = await _queue_completion(
        RecordingTransport(),
        [{"id": "req-1", "line": json.dumps({"error": "upstream boom"})}],
    )
    with pytest.raises(NativeError) as info:
        await _collect(completion)
    assert (info.value.status_code, info.value.code) == (502, "upstream_error")


async def test_completion_deltas_stops_when_parser_finished():
    completion = await _queue_completion(
        RecordingTransport(),
        [
            {
                "id": "req-1",
                "line": json.dumps(
                    {"choices": [{"index": 0, "delta": {"content": "once", "status": "finished"}}]}
                ),
            },
            {"id": "req-1", "line": json.dumps({"choices": [{"delta": {"content": "twice"}}]})},
        ],
    )
    assert await _collect(completion) == [("once", "")]


async def test_completion_aclose_cancels_task_and_finishes_request():
    transport = RecordingTransport()
    task = _pending_task()
    completion = QwenBrowserCompletion(
        transport,
        task,
        asyncio.Queue(),
        "req-1",
        "chat-1",
        QwenStreamParser(),
    )

    await completion.aclose()
    await completion.aclose()

    assert task.cancelled() is True
    assert transport.finished == [("req-1", "chat-1")]


async def test_completion_aclose_tolerates_finished_task():
    transport = RecordingTransport()
    completion = QwenBrowserCompletion(
        transport,
        await _done_task(3),
        asyncio.Queue(),
        "req-1",
        "chat-1",
        QwenStreamParser(),
    )

    await completion.aclose()

    assert transport.finished == [("req-1", "chat-1")]


# --- browser transport helpers --------------------------------------------


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(qwen_module.asyncio, "sleep", no_sleep)


async def test_token_requires_a_started_page(tmp_path):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    with pytest.raises(NativeError) as info:
        await transport._token()
    assert (info.value.status_code, info.value.code) == (503, "upstream_unavailable")


async def test_token_returns_local_storage_value(tmp_path):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._page = FakePage(token="qwen-token")
    assert await transport._token() == "qwen-token"


async def test_token_reloads_then_succeeds(tmp_path, fast_sleep):
    page = FakePage(token="qwen-token", token_after_reload=True)
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._page = page

    assert await transport._token() == "qwen-token"
    assert page.reloads == 1


async def test_token_reports_missing_session_after_attempts(tmp_path, fast_sleep):
    page = FakePage()
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._page = page

    with pytest.raises(NativeError) as info:
        await transport._token()
    assert (info.value.status_code, info.value.code) == (401, "upstream_auth_error")
    assert page.evaluations == 30
    assert page.reloads == 1


async def test_token_treats_evaluate_failures_as_missing(tmp_path, fast_sleep):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._page = FakePage(error=RuntimeError("page detached"))

    with pytest.raises(NativeError) as info:
        await transport._token()
    assert info.value.status_code == 401


def test_headers_build_base_headers(tmp_path):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    headers = transport._headers("qwen-token", {})
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "*/*"
    assert headers["Authorization"] == "Bearer qwen-token"
    assert UUID_TEXT.match(headers["X-Request-Id"])


def test_headers_merge_baxia_and_filter_unsafe_entries(tmp_path):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    headers = transport._headers(
        "qwen-token",
        {
            "baxia_headers": {
                "bx-ua": "42",
                "cookie": "session=1",
                "Authorization": "Bearer attacker",
                "host": "evil.test",
                "content-length": "10",
                "accept-encoding": "br",
                "connection": "close",
                "origin": "https://evil.test",
                "referer": "https://evil.test",
                "empty": None,
                7: "non-str-key",
            }
        },
    )
    assert headers["bx-ua"] == "42"
    assert headers["Authorization"] == "Bearer qwen-token"
    lowered = {key.lower() for key in headers}
    assert {"cookie", "host", "content-length", "accept-encoding", "connection"} & lowered == set()
    assert "origin" not in lowered and "referer" not in lowered
    assert "empty" not in headers
    assert "7" not in headers


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        ({"status": 0, "error": "TypeError: fetch failed"}, 503, "upstream_unavailable"),
        ({"status": 429, "text": "slow down"}, 429, "upstream_error"),
        ({"status": 401, "text": "unauthorized"}, 401, "upstream_error"),
        ({"status": 403, "text": "forbidden"}, 401, "upstream_error"),
        ({"status": 500, "text": "boom"}, 502, "upstream_error"),
        (
            {"status": 200, "data": {"success": False, "message": "denied"}},
            502,
            "upstream_error",
        ),
        ({"status": 200, "data": {"data": {}}}, 502, "upstream_error"),
        ({"status": 200, "data": "plain text", "text": "plain text"}, 502, "upstream_error"),
    ],
)
async def test_create_chat_maps_failures(tmp_path, result, status, code):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._page = FakePage([result])

    with pytest.raises(NativeError) as info:
        await transport._create_chat("qwen3.7-max", {}, "OpenAI request")
    assert (info.value.status_code, info.value.code) == (status, code)


async def test_create_chat_requires_started_page(tmp_path):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    with pytest.raises(NativeError) as info:
        await transport._create_chat("qwen3.7-max", {}, "OpenAI request")
    assert info.value.status_code == 503


async def test_create_chat_returns_chat_id(tmp_path):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._page = FakePage([{"status": 200, "data": {"data": {"id": "chat-42"}}}])
    assert await transport._create_chat("qwen3.7-max", {}, "title") == "chat-42"


async def test_delete_chat_is_a_noop_without_page_or_id(tmp_path):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    await transport._delete_chat("chat-1", {})

    page = FakePage()
    transport._page = page
    await transport._delete_chat("", {})
    await transport._delete_chat("chat-1", {"Authorization": "Bearer t"})
    assert page.evaluations == 1


async def test_delete_chat_suppresses_page_errors(tmp_path):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._page = FakePage(error=RuntimeError("detached"))
    await transport._delete_chat("chat-1", {})


def test_normalize_cookies_applies_defaults_and_skips_invalid_entries():
    assert QwenBrowserTransport._normalize_cookies("not-a-list") == []
    cookies = QwenBrowserTransport._normalize_cookies(
        [
            {"name": "session", "value": "abc"},
            {
                "name": "rich",
                "value": "def",
                "domain": ".example.test",
                "path": "/chat",
                "expires": 1_700_000_000.5,
                "sameSite": "Lax",
                "httpOnly": True,
                "secure": False,
            },
            {"name": "", "value": "skipped"},
            {"name": "no-value"},
            "junk",
        ]
    )
    assert [cookie["name"] for cookie in cookies] == ["session", "rich"]
    assert cookies[0]["domain"] == ".qwen.ai"
    assert cookies[0]["path"] == "/"
    assert "expires" not in cookies[0]
    assert cookies[1]["expires"] == 1_700_000_000.5
    assert cookies[1]["sameSite"] == "Lax"
    assert cookies[1]["httpOnly"] is True
    assert cookies[1]["secure"] is False


async def test_emit_requires_queue_and_matching_id(tmp_path):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    await transport._emit({"id": "req-1"})
    await transport._emit(["not", "a", "dict"])  # type: ignore[arg-type]

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    transport._queue = queue
    transport._active_id = "req-1"
    await transport._emit({"id": "other", "line": "dropped"})
    assert queue.empty()

    await transport._emit({"id": "req-1", "line": "kept"})
    assert queue.get_nowait() == {"id": "req-1", "line": "kept"}


def test_auth_wraps_unreadable_store(tmp_path):
    store = tmp_path / "tokens.json"
    store.write_text("{not json", encoding="utf-8")
    transport = QwenBrowserTransport(store)

    with pytest.raises(NativeError) as info:
        transport._auth()
    assert (info.value.status_code, info.value.code) == (503, "provider_not_configured")


# --- open_completion / finish_request -------------------------------------


def _patch_open_completion(
    monkeypatch: pytest.MonkeyPatch, transport: QwenBrowserTransport
) -> None:
    async def ensure_started() -> None:
        return None

    async def auth() -> dict[str, Any]:
        return {"baxia_headers": {"bx-ua": "42"}}

    async def token() -> str:
        return "qwen-token"

    async def create_chat(model: str, headers: dict[str, str], title: str) -> str:
        return "chat-1"

    async def delete_chat(chat_id: str, headers: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(transport, "_ensure_started", ensure_started)
    monkeypatch.setattr(transport, "_auth_async", auth)
    monkeypatch.setattr(transport, "_token", token)
    monkeypatch.setattr(transport, "_create_chat", create_chat)
    monkeypatch.setattr(transport, "_delete_chat", delete_chat)


@pytest.mark.parametrize(
    ("started", "status", "code"),
    [
        ({"status": 0, "error": "TypeError: fetch failed"}, 503, "upstream_unavailable"),
        ({"status": 429, "errorText": "slow down"}, 429, "upstream_error"),
        ({"status": 401, "errorText": "unauthorized"}, 401, "upstream_error"),
        ({"status": 403, "errorText": "forbidden"}, 401, "upstream_error"),
        ({"status": 500, "errorText": "boom"}, 502, "upstream_error"),
        ({"status": 200, "contentType": "text/html", "nonSse": "<html>"}, 502, "upstream_error"),
    ],
)
async def test_open_completion_maps_start_failures_and_resets_state(
    tmp_path, monkeypatch, started, status, code
):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._page = FakePage([started])  # type: ignore[assignment]
    _patch_open_completion(monkeypatch, transport)

    with pytest.raises(NativeError) as info:
        await transport.open_completion("hi", "qwen3.7-max", [])

    assert (info.value.status_code, info.value.code) == (status, code)
    assert transport._request_lock.locked() is False
    assert transport._queue is None
    assert transport._active_id is None


async def test_open_completion_success_and_aclose_release_lock(tmp_path, monkeypatch):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._page = FakePage(  # type: ignore[assignment]
        [{"status": 200, "contentType": "text/event-stream", "ok": True}, None]
    )
    _patch_open_completion(monkeypatch, transport)

    completion = await transport.open_completion("hi", "qwen3.7-max", [])

    assert HEX_ID.match(completion.request_id)
    assert completion.chat_id == "chat-1"
    assert transport._request_lock.locked() is True
    assert transport._queue is completion.queue
    assert transport._active_id == completion.request_id

    await completion.aclose()

    assert transport._request_lock.locked() is False
    assert transport._queue is None
    assert transport._active_id is None


async def test_finish_request_clears_state_and_releases_lock(tmp_path, monkeypatch):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._queue = asyncio.Queue()
    transport._active_id = "req-1"
    await transport._request_lock.acquire()
    calls: list[tuple[str, str | None]] = []

    async def auth() -> dict[str, Any]:
        return {"baxia_headers": {}}

    async def token() -> str:
        return "qwen-token"

    async def delete_chat(chat_id: str, headers: dict[str, str]) -> None:
        calls.append((chat_id, headers.get("Authorization")))

    monkeypatch.setattr(transport, "_auth_async", auth)
    monkeypatch.setattr(transport, "_token", token)
    monkeypatch.setattr(transport, "_delete_chat", delete_chat)

    await transport.finish_request("req-1", "chat-1")

    assert calls == [("chat-1", "Bearer qwen-token")]
    assert transport._queue is None
    assert transport._active_id is None
    assert transport._request_lock.locked() is False


@pytest.mark.parametrize("failing", ["auth", "token", "delete"])
async def test_finish_request_releases_lock_when_cleanup_fails(tmp_path, monkeypatch, failing):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    transport._queue = asyncio.Queue()
    transport._active_id = "req-1"
    await transport._request_lock.acquire()

    async def auth() -> dict[str, Any]:
        if failing == "auth":
            raise RuntimeError("store unreadable")
        return {}

    async def token() -> str:
        if failing == "token":
            raise RuntimeError("token missing")
        return "qwen-token"

    async def delete_chat(chat_id: str, headers: dict[str, str]) -> None:
        if failing == "delete":
            raise RuntimeError("delete failed")

    monkeypatch.setattr(transport, "_auth_async", auth)
    monkeypatch.setattr(transport, "_token", token)
    monkeypatch.setattr(transport, "_delete_chat", delete_chat)

    await transport.finish_request("req-1", "chat-1")

    assert transport._request_lock.locked() is False


async def test_finish_request_keeps_state_for_foreign_request(tmp_path, monkeypatch):
    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    transport._queue = queue
    transport._active_id = "other-request"
    await transport._request_lock.acquire()

    async def auth() -> dict[str, Any]:
        return {}

    async def token() -> str:
        return "qwen-token"

    async def delete_chat(chat_id: str, headers: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(transport, "_auth_async", auth)
    monkeypatch.setattr(transport, "_token", token)
    monkeypatch.setattr(transport, "_delete_chat", delete_chat)

    await transport.finish_request("req-1", "chat-1")

    assert transport._queue is queue
    assert transport._active_id == "other-request"


# --- service ---------------------------------------------------------------


class ScriptedCompletion:
    def __init__(
        self,
        payloads: list[dict[str, Any]] | None = None,
        *,
        error: BaseException | None = None,
        http_error: BaseException | None = None,
    ) -> None:
        self.parser = QwenStreamParser()
        self.payloads = list(payloads or [])
        self.error = error
        self.http_error = http_error
        self.closed = False

    async def deltas(self):
        for payload in self.payloads:
            content, reasoning = self.parser.handle_payload(payload)
            if self.parser.error is not None:
                raise self.parser.error
            if content or reasoning:
                yield content, reasoning
            if self.parser.finished:
                return
        if self.error is not None:
            raise self.error
        if self.http_error is not None:
            raise self.http_error

    async def aclose(self) -> None:
        self.closed = True


class ScriptedTransport:
    def __init__(self, completion: ScriptedCompletion) -> None:
        self.completion = completion
        self.prompts: list[str] = []
        self.models: list[str] = []
        self.tools: list[list[dict[str, Any]]] = []
        self.closed = False

    async def open_completion(self, prompt, model, tools):
        self.prompts.append(prompt)
        self.models.append(model)
        self.tools.append(tools)
        return self.completion

    async def aclose(self) -> None:
        self.closed = True


def _payload(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "qwen3.7-max",
        "messages": [{"role": "user", "content": "hi"}],
    }
    payload.update(extra)
    return payload


def _content_payload(content: str, *, finished: bool = True, usage: bool = False):
    delta: dict[str, Any] = {"content": content}
    if finished:
        delta["status"] = "finished"
    payload: dict[str, Any] = {"choices": [{"index": 0, "delta": delta}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}
    return payload


def _sse_events(chunks: list[bytes]) -> list[dict[str, Any] | None]:
    events: list[dict[str, Any] | None] = []
    for line in b"".join(chunks).decode().splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[len("data: ") :]
        events.append(None if raw == "[DONE]" else json.loads(raw))
    return events


def test_service_configured_reads_token_store(tmp_path):
    configured_store = TokenStore(tmp_path / "configured.json")
    configured_store.upsert("qwen", {"token": "qwen-token"})
    empty_store = TokenStore(tmp_path / "empty.json")
    empty_store.upsert("qwen", {"cookies": []})

    assert QwenService(configured_store.path, ScriptedTransport(ScriptedCompletion())).configured()
    assert not QwenService(empty_store.path, ScriptedTransport(ScriptedCompletion())).configured()
    assert not QwenService(
        tmp_path / "missing.json", ScriptedTransport(ScriptedCompletion())
    ).configured()


def test_service_configured_treats_unreadable_store_as_missing(tmp_path):
    broken = tmp_path / "tokens.json"
    broken.write_text("[", encoding="utf-8")

    assert not QwenService(broken, ScriptedTransport(ScriptedCompletion())).configured()


async def test_service_chat_returns_content_without_tools(tmp_path):
    completion = ScriptedCompletion(
        [{"choices": [{"index": 0, "delta": {"content": "answer", "reasoning_content": "why"}}]}]
    )
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    response = await service.chat(_payload())

    message = response["choices"][0]["message"]
    assert message["content"] == "answer"
    assert message["reasoning_content"] == "why"
    assert response["choices"][0]["finish_reason"] == "stop"


async def test_service_chat_parses_tool_calls_and_clears_content(tmp_path):
    content = '{"tool_call":{"name":"read_file","arguments":{"path":"a.txt"}}}'
    completion = ScriptedCompletion([_content_payload(content)])
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    response = await service.chat(_payload(tools=tools))

    message = response["choices"][0]["message"]
    assert message["content"] is None
    assert message["tool_calls"][0]["function"]["name"] == "read_file"
    assert response["choices"][0]["finish_reason"] == "tool_calls"


async def test_service_chat_rejects_malformed_tool_markup(tmp_path):
    completion = ScriptedCompletion([_content_payload('{"tool_call": {"name": ""}}')])
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    with pytest.raises(NativeError) as info:
        await service.chat(_payload(tools=tools))
    assert (info.value.status_code, info.value.code) == (502, "malformed_tool_call")


async def test_service_stream_chat_emits_content_reasoning_and_usage(tmp_path):
    completion = ScriptedCompletion(
        [
            {"choices": [{"index": 0, "delta": {"content": "Hello", "reasoning_content": "hmm"}}]},
            _content_payload(" world"),
        ]
    )
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    chunks = [chunk async for chunk in service.stream_chat(_payload(), completion)]
    events = _sse_events(chunks)

    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    deltas = [event["choices"][0]["delta"] for event in events if event and event["choices"]]
    assert {"reasoning_content": "hmm"} in deltas
    assert {"content": "Hello"} in deltas
    assert {"content": " world"} in deltas
    final = events[-2]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"]["total_tokens"] == (
        final["usage"]["prompt_tokens"] + final["usage"]["completion_tokens"]
    )
    assert final["usage"]["prompt_tokens"] >= 1
    assert events[-1] is None
    assert completion.closed is True


async def test_service_stream_chat_prefers_parser_usage(tmp_path):
    completion = ScriptedCompletion([_content_payload("abcd", usage=True)])
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    chunks = [chunk async for chunk in service.stream_chat(_payload(), completion)]
    final = _sse_events(chunks)[-2]

    assert final["usage"] == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}


async def test_service_stream_chat_buffers_tool_call_markup(tmp_path):
    content = '{"tool_call":{"name":"read_file","arguments":{"path":"a.txt"}}}'
    completion = ScriptedCompletion([_content_payload(content)])
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    chunks = [chunk async for chunk in service.stream_chat(_payload(tools=tools), completion)]
    events = [event for event in _sse_events(chunks) if event is not None]

    tool_chunks = [
        event["choices"][0]["delta"]["tool_calls"]
        for event in events
        if "tool_calls" in event["choices"][0]["delta"]
    ]
    assert tool_chunks and tool_chunks[0][0]["function"]["name"] == "read_file"
    final = _sse_events(chunks)[-2]
    assert final["choices"][0]["finish_reason"] == "tool_calls"


async def test_service_stream_chat_emits_plain_content_with_tools_configured(tmp_path):
    completion = ScriptedCompletion([_content_payload("plain answer")])
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    chunks = [chunk async for chunk in service.stream_chat(_payload(tools=tools), completion)]
    events = [event for event in _sse_events(chunks) if event is not None and "choices" in event]

    assert {"content": "plain answer"} in [event["choices"][0]["delta"] for event in events]
    final = _sse_events(chunks)[-2]
    assert final["choices"][0]["finish_reason"] == "stop"


async def test_service_stream_chat_reports_malformed_tool_markup(tmp_path):
    completion = ScriptedCompletion([_content_payload('{"tool_call": {"name": ""}}')])
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    chunks = [chunk async for chunk in service.stream_chat(_payload(tools=tools), completion)]
    events = _sse_events(chunks)

    error_event = events[-2]
    assert error_event["error"]["code"] == "malformed_tool_call"
    assert events[-1] is None
    assert completion.closed is True


async def test_service_stream_chat_reports_native_error_mid_stream(tmp_path):
    completion = ScriptedCompletion(
        [_content_payload("partial", finished=False)],
        error=NativeError(502, "upstream exploded", "upstream_error"),
    )
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    chunks = [chunk async for chunk in service.stream_chat(_payload(), completion)]
    events = _sse_events(chunks)

    assert {"content": "partial"} in [
        event["choices"][0]["delta"] for event in events if event is not None and "choices" in event
    ]
    assert events[-2]["error"] == {
        "message": "upstream exploded",
        "type": "upstream_error",
        "code": "upstream_error",
        "param": None,
    }
    assert events[-1] is None
    assert completion.closed is True


async def test_service_stream_chat_maps_httpx_errors_to_unavailable(tmp_path):
    completion = ScriptedCompletion([], http_error=httpx.ReadError("reset"))
    service = QwenService(tmp_path / "tokens.json", ScriptedTransport(completion))

    chunks = [chunk async for chunk in service.stream_chat(_payload(), completion)]
    events = _sse_events(chunks)

    assert events[-2]["error"]["code"] == "upstream_unavailable"
    assert "ReadError" in events[-2]["error"]["message"]
    assert events[-1] is None


# --- app -------------------------------------------------------------------


class AppTransport:
    def __init__(
        self,
        completion: ScriptedCompletion | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.completion = completion
        self.error = error
        self.closed = False

    async def open_completion(self, prompt, model, tools):
        if self.error is not None:
            raise self.error
        assert self.completion is not None
        return self.completion

    async def aclose(self) -> None:
        self.closed = True


def _store_with_token(tmp_path: Path) -> Path:
    store = TokenStore(tmp_path / "tokens.json")
    store.upsert("qwen", {"token": "qwen-token", "cookies": []})
    return store.path


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://native")


async def test_app_rejects_invalid_json(tmp_path):
    app = create_qwen_app(_store_with_token(tmp_path), transport=AppTransport())
    async with _client(app) as client:
        response = await client.post(
            "/v1/chat/completions",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Invalid JSON"


async def test_app_rejects_non_object_body(tmp_path):
    app = create_qwen_app(_store_with_token(tmp_path), transport=AppTransport())
    async with _client(app) as client:
        response = await client.post("/v1/chat/completions", json=["not", "an", "object"])
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Request body must be an object"


@pytest.mark.parametrize("payload", [{}, {"messages": []}, {"messages": "hi"}])
async def test_app_rejects_empty_messages(tmp_path, payload):
    app = create_qwen_app(_store_with_token(tmp_path), transport=AppTransport())
    async with _client(app) as client:
        response = await client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "messages must be a non-empty array"


async def test_app_reports_missing_configuration(tmp_path):
    app = create_qwen_app(tmp_path / "missing.json", transport=AppTransport())
    async with _client(app) as client:
        response = await client.post("/v1/chat/completions", json=_payload())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"


async def test_app_maps_native_error_and_retry_after(tmp_path):
    error = NativeError(429, "slow down", "upstream_rate_limited", retry_after=9)
    app = create_qwen_app(_store_with_token(tmp_path), transport=AppTransport(error=error))
    async with _client(app) as client:
        response = await client.post("/v1/chat/completions", json=_payload())
    assert response.status_code == 429
    assert response.headers["retry-after"] == "9"
    assert response.json()["error"]["code"] == "upstream_rate_limited"


async def test_app_maps_httpx_error_to_upstream_unavailable(tmp_path):
    app = create_qwen_app(
        _store_with_token(tmp_path),
        transport=AppTransport(error=httpx.ConnectError("no route")),
    )
    async with _client(app) as client:
        response = await client.post("/v1/chat/completions", json=_payload())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_unavailable"


async def test_app_maps_unexpected_error_to_native_error(tmp_path):
    app = create_qwen_app(
        _store_with_token(tmp_path),
        transport=AppTransport(error=ValueError("weird state")),
    )
    async with _client(app) as client:
        response = await client.post("/v1/chat/completions", json=_payload())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "native_error"
    assert "weird state" in response.json()["error"]["message"]


async def test_app_health_and_models(tmp_path):
    app = create_qwen_app(_store_with_token(tmp_path), transport=AppTransport())
    async with _client(app) as client:
        health = await client.get("/health")
        models = await client.get("/v1/models")

    assert health.status_code == 200
    assert health.json() == {"healthy": True, "mode": "native-qwen"}
    assert models.status_code == 200
    assert [entry["id"] for entry in models.json()["data"]] == list(MODELS)


async def test_app_health_reports_unconfigured(tmp_path):
    app = create_qwen_app(tmp_path / "missing.json", transport=AppTransport())
    async with _client(app) as client:
        health = await client.get("/health")
    assert health.status_code == 503
    assert health.json()["healthy"] is False


async def test_app_builds_default_transport_lazily(tmp_path):
    app = create_qwen_app(tmp_path / "tokens.json")
    assert callable(app.state.close)
    await app.state.close()
