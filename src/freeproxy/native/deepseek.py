from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.paths import AuthPaths
from ..auth.store import AuthStoreError, TokenStore
from .pow import POW_DEFAULT_WASM, PowError, PowSolver

logger = logging.getLogger("freeproxy.native.deepseek")

BASE_URL = "https://chat.deepseek.com"
COMPLETION_PATH = "/api/v0/chat/completion"
MAX_PROMPT_CHARS = 80_000
TOOL_CALL_FORMAT = '{"tool_call":{"name":"<function_name>","arguments":{...}}}'


def _model_config(*, thinking: bool = False, search: bool = False) -> dict[str, Any]:
    return {"thinking": thinking, "search": search, "model_type": "default"}


# DeepSeek Web merged its modes; these aliases only toggle DeepThink/search.
MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "deepseek-chat": _model_config(),
    "deepseek-default": _model_config(),
    "deepseek-v4-flash": _model_config(),
    "deepseek-v4.1-flash": _model_config(),
    "deepseek-v3": _model_config(),
    "deepseek-reasoner": _model_config(thinking=True),
    "deepseek-r1": _model_config(thinking=True),
    "deepseek-expert": _model_config(thinking=True),
    "deepseek-v4-pro": _model_config(thinking=True),
    "deepseek-chat-search": _model_config(search=True),
    "deepseek-default-search": _model_config(search=True),
    "deepseek-reasoner-search": _model_config(thinking=True, search=True),
    "deepseek-r1-search": _model_config(thinking=True, search=True),
    "deepseek-expert-search": _model_config(thinking=True, search=True),
}


class NativeError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        message: str,
        code: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.retry_after = retry_after


def _retry_after(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif part.get("type") in {"image_url", "input_image"}:
                    parts.append("[image]")
        return "\n".join(parts)
    return ""


def _tool_call_text(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function") if isinstance(tool_call, dict) else None
    if not isinstance(function, dict):
        function = tool_call if isinstance(tool_call, dict) else {}
    name = str(function.get("name") or "")
    arguments = function.get("arguments") or {}
    if isinstance(arguments, str):
        with contextlib.suppress(json.JSONDecodeError):
            arguments = json.loads(arguments or "{}")
    return json.dumps(
        {"tool_call": {"name": name, "arguments": arguments}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _tool_definitions(tools: list[dict[str, Any]]) -> str:
    if not tools:
        return ""
    text = (
        "\n\n--- TOOL REQUEST SYSTEM ---\n"
        "You can request exactly one local tool execution. "
        "Reply with ONLY this JSON envelope and no Markdown fences:\n"
        f"{TOOL_CALL_FORMAT}\n"
        "The local gateway executes the tool and sends the result back as "
        "[Tool Result: name]. Never invent a tool result.\n"
        "When no tool is needed, answer normally without tool_call JSON.\n"
        "Available functions:\n"
    )
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        description = re.sub(r"\s+", " ", str(function.get("description") or ""))[:500]
        parameters = function.get("parameters") or {}
        text += f"\n## {name}\n{description}\nParameters: {json.dumps(parameters, ensure_ascii=False)}\n"
    text += "\n--- END TOOL REQUEST SYSTEM ---\n"
    return text


def format_prompt(payload: dict[str, Any]) -> str:
    messages = [message for message in payload.get("messages", []) if isinstance(message, dict)]
    tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    system_parts: list[str] = []
    conversation: list[str] = []
    tool_names: dict[str, str] = {}

    for message in messages:
        if message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list):
            for tool_call in message["tool_calls"]:
                function = tool_call.get("function") if isinstance(tool_call, dict) else None
                if isinstance(function, dict) and tool_call.get("id"):
                    tool_names[str(tool_call["id"])] = str(function.get("name") or "")

    for message in messages:
        role = message.get("role")
        text = _content_text(message.get("content"))
        if role in {"system", "developer"}:
            if text:
                system_parts.append(text)
        elif role == "user":
            if text:
                conversation.append(f"User: {text}")
        elif role == "assistant":
            if text:
                conversation.append(f"Assistant: {text}")
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if isinstance(tool_call, dict):
                        conversation.append(f"Assistant: {_tool_call_text(tool_call)}")
        elif role in {"tool", "function"}:
            name = message.get("name") or tool_names.get(str(message.get("tool_call_id") or ""), "")
            label = f"[Tool Result: {name}]" if name else "[Tool Result]"
            conversation.append(f"{label}\n{text}")

    full = "\n".join(system_parts).strip() + _tool_definitions(tools)
    if conversation:
        full += "\n\n" + "\n\n".join(conversation)
    full = full.strip()
    if len(full) > MAX_PROMPT_CHARS:
        head = full[:4_000]
        tail = full[-(MAX_PROMPT_CHARS - 4_100) :]
        full = head + "\n...[context compacted]...\n" + tail
    return full


def _find_json_object(text: str) -> dict[str, Any] | None:
    starts = [index for index, char in enumerate(text) if char == "{"]
    for start in starts[:32]:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, min(len(text), start + 256 * 1024)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
    return None


def looks_like_tool_markup(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.startswith("```"):
        match = re.match(r"^```[\w.+-]*\s*(.*?)\s*```$", stripped, re.DOTALL)
        if match:
            stripped = match.group(1).strip()
    if not stripped:
        return False
    if stripped[0] not in '{"<[' and not stripped.upper().startswith("TOOL_CALL"):
        return False
    return bool(
        re.search(
            r"TOOL_CALL:\s*[\w-]+|<\s*tool_call\b|[|]+\s*DSML\s*[|]+|"
            r"[<<]\s*/?\s*(?:DSML)?(?:[\w.-]+:)?"
            r"(?:tool[\s_-]*calls|function[\s_-]*calls|invoke)\b|"
            r"[\"'](?:tool_call|tool_calls|function_call)[\"']\s*:",
            stripped,
            re.IGNORECASE,
        )
    )


def parse_tool_call(content: str) -> tuple[str, list[dict[str, Any]]] | None:
    if not content:
        return None
    cleaned = re.sub(r"```(?:json)?", "", content).replace("```", "").strip()
    candidates = [cleaned]
    extracted = _find_json_object(cleaned)
    if extracted is not None:
        candidates.append(json.dumps(extracted))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        raw = value.get("tool_call") or value.get("function_call")
        if (
            raw is None
            and isinstance(value.get("tool_calls"), list)
            and len(value["tool_calls"]) == 1
        ):
            raw = value["tool_calls"][0]
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = str(function.get("name") or "")
        if not name:
            continue
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                json.loads(arguments)
                argument_text = arguments
            except json.JSONDecodeError:
                argument_text = "{}"
        else:
            argument_text = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        return content, [
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": name, "arguments": argument_text},
            }
        ]
    return None


class DeepSeekStreamParser:
    def __init__(self) -> None:
        self.fragments: list[dict[str, Any]] = []
        self.content = ""
        self.reasoning = ""
        self.finish_reason: str | None = None
        self.status = ""
        self.message_id: str | None = None
        self.model_error = ""
        self.last_path: str | None = None
        self._emitted_content = ""
        self._emitted_reasoning = ""

    def _rebuild(self) -> None:
        self.content = "".join(
            str(fragment.get("content") or "")
            for fragment in self.fragments
            if fragment.get("type") in {"RESPONSE", "SEARCH"}
        )
        self.reasoning = "".join(
            str(fragment.get("content") or "")
            for fragment in self.fragments
            if fragment.get("type") in {"THINK", "REASONING"}
        )

    def _append_fragments(self, value: Any) -> None:
        incoming = value if isinstance(value, list) else [value]
        for fragment in incoming:
            if isinstance(fragment, dict):
                self.fragments.append(dict(fragment))
        self._rebuild()

    def handle_line(self, raw_line: str) -> None:
        line = raw_line.rstrip("\r")
        if not line.startswith("data:"):
            return
        payload = line[5:].lstrip()
        if not payload or payload == "[DONE]":
            return
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        if event.get("response_message_id") and not self.message_id:
            self.message_id = str(event["response_message_id"])
        if event.get("type") == "error":
            self.model_error = str(event.get("content") or event.get("message") or "DeepSeek error")
        if event.get("finish_reason"):
            self.finish_reason = str(event["finish_reason"])
        if event.get("p") is not None:
            self.last_path = str(event["p"])
        value = event.get("v")

        if isinstance(value, dict) and isinstance(value.get("response"), dict):
            response = value["response"]
            if response.get("message_id") is not None:
                self.message_id = str(response["message_id"])
            if isinstance(response.get("content"), str):
                self.content = response["content"]
            if isinstance(response.get("fragments"), list):
                self.fragments = [
                    dict(fragment)
                    for fragment in response["fragments"]
                    if isinstance(fragment, dict)
                ]
                self._rebuild()
            if response.get("finish_reason") is not None:
                self.finish_reason = str(response["finish_reason"])
            if isinstance(response.get("status"), str):
                self.status = response["status"]

        if self.last_path == "response" and isinstance(value, list):
            for operation in value:
                if (
                    isinstance(operation, dict)
                    and operation.get("p") == "fragments"
                    and operation.get("o") == "APPEND"
                    and operation.get("v") is not None
                ):
                    self._append_fragments(operation["v"])
        elif self.last_path == "response/fragments" and value is not None:
            self._append_fragments(value)
        elif (
            self.last_path == "response/fragments/-1/content"
            and value is not None
            and not isinstance(value, dict)
        ):
            if self.fragments:
                last = self.fragments[-1]
                last["content"] = str(last.get("content") or "") + str(value)
                self._rebuild()
        elif (
            self.last_path == "response/content"
            and value is not None
            and not isinstance(value, dict)
        ):
            self.content += str(value)
        elif self.last_path == "response/finish_reason" and value is not None:
            self.finish_reason = str(value)
        elif self.last_path == "response/status" and isinstance(value, str):
            self.status = value

    def delta(self) -> tuple[str, str]:
        content = self.content
        reasoning = self.reasoning
        if content.startswith(self._emitted_content):
            new_content = content[len(self._emitted_content) :]
        else:
            new_content = content
        if reasoning.startswith(self._emitted_reasoning):
            new_reasoning = reasoning[len(self._emitted_reasoning) :]
        else:
            new_reasoning = reasoning
        self._emitted_content = content
        self._emitted_reasoning = reasoning
        return new_content, new_reasoning

    def finalize(self) -> str:
        if not self.finish_reason:
            if self.status.upper() == "INCOMPLETE":
                self.finish_reason = "length"
            else:
                self.finish_reason = "stop"
        return self.finish_reason


class DeepSeekCompletion:
    """An already-opened upstream SSE response and its parser."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        response: httpx.Response,
        parser: DeepSeekStreamParser,
    ) -> None:
        self.client = client
        self.response = response
        self.parser = parser
        self._closed = False

    async def deltas(self) -> AsyncIterator[tuple[str, str]]:
        async for line in self.response.aiter_lines():
            self.parser.handle_line(line)
            delta = self.parser.delta()
            if delta != ("", ""):
                yield delta
        if self.parser.model_error:
            raise NativeError(502, self.parser.model_error, "upstream_error")
        tail = self.parser.delta()
        if tail != ("", ""):
            yield tail

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self.response.aclose()
        finally:
            await self.client.aclose()


class DeepSeekService:
    def __init__(
        self,
        store_path: Path,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store_path = Path(store_path)
        self.transport = transport

    def _auth(self) -> dict[str, Any]:
        try:
            data = TokenStore(self.store_path).provider("deepseek")
        except AuthStoreError as exc:
            raise NativeError(
                503,
                f"DeepSeek auth store is unreadable: {exc}",
                "provider_not_configured",
            ) from exc
        if not data or not data.get("token"):
            raise NativeError(
                503,
                "DeepSeek token is not configured. Run `uv run freeproxy-auth login deepseek`.",
                "provider_not_configured",
            )
        return data

    def ensure_configured(self) -> None:
        self._auth()

    async def check_configured(self) -> None:
        await asyncio.to_thread(self.ensure_configured)

    def _model(self, payload: dict[str, Any]) -> str:
        model = str(payload.get("model") or "deepseek-chat").lower()
        if model not in MODEL_CONFIGS:
            raise NativeError(400, f"Unknown DeepSeek model: {model}", "invalid_model")
        return model

    @staticmethod
    def _headers(auth: dict[str, Any]) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            ),
            "x-client-platform": "web",
            "x-client-version": "2.0.0",
            "x-client-locale": "ru",
            "x-client-timezone-offset": "14400",
            "x-app-version": "2.0.0",
            "Authorization": f"Bearer {auth.get('token', '')}",
            "x-hif-dliq": str(auth.get("hif_dliq") or ""),
            "x-hif-leim": str(auth.get("hif_leim") or ""),
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
            "Cookie": str(auth.get("cookie") or ""),
            "Content-Type": "application/json",
        }

    @staticmethod
    async def _create_session(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
        response = await client.post(
            BASE_URL + "/api/v0/chat_session/create",
            headers=headers,
            json={},
        )
        if response.status_code >= 400:
            raise NativeError(
                502 if response.status_code not in {401, 403, 429} else response.status_code,
                f"DeepSeek chat session failed (HTTP {response.status_code}). "
                "The web token may be expired; run freeproxy-auth login deepseek.",
                "upstream_session_error",
                retry_after=_retry_after(response),
            )
        try:
            data = response.json()
        except ValueError:
            raise NativeError(
                502, "DeepSeek returned a non-JSON session response", "upstream_error"
            ) from None
        body = data.get("data") if isinstance(data, dict) else None
        biz_data = body.get("biz_data") if isinstance(body, dict) else None
        if not isinstance(biz_data, dict):
            raise NativeError(
                401,
                "DeepSeek web session is invalid or expired; "
                "run `uv run freeproxy-auth login deepseek`.",
                "upstream_auth_error",
            )
        chat_session = biz_data.get("chat_session")
        session_id = (
            chat_session.get("id") if isinstance(chat_session, dict) else None
        ) or biz_data.get("id")
        if not session_id:
            raise NativeError(
                502, "DeepSeek session response has no chat_session.id", "upstream_error"
            )
        return str(session_id)

    async def _pow_header(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        auth: dict[str, Any],
    ) -> str:
        response = await client.post(
            BASE_URL + "/api/v0/chat/create_pow_challenge",
            headers=headers,
            json={"target_path": COMPLETION_PATH},
        )
        if response.status_code >= 400:
            raise NativeError(
                502 if response.status_code not in {401, 403, 429} else response.status_code,
                f"DeepSeek PoW challenge failed (HTTP {response.status_code}). "
                "Auth may be expired; run freeproxy-auth login deepseek.",
                "upstream_pow_error",
                retry_after=_retry_after(response),
            )
        try:
            data = response.json()
        except ValueError:
            raise NativeError(
                502, "DeepSeek PoW response is not JSON", "upstream_pow_error"
            ) from None
        body = data.get("data") if isinstance(data, dict) else None
        biz_data = body.get("biz_data") if isinstance(body, dict) else None
        challenge = biz_data.get("challenge") if isinstance(biz_data, dict) else None
        if not isinstance(challenge, dict):
            raise NativeError(
                401,
                "DeepSeek web session is invalid or expired; "
                "run `uv run freeproxy-auth login deepseek`.",
                "upstream_auth_error",
            )
        solver = PowSolver(
            client,
            str(auth.get("wasm_url") or POW_DEFAULT_WASM),
        )
        try:
            answer = await solver.solve(challenge)
        except (PowError, httpx.HTTPError) as exc:
            raise NativeError(502, f"DeepSeek PoW failed: {exc}", "upstream_pow_error") from exc
        return PowSolver.build_header(challenge, answer, COMPLETION_PATH)

    def _completion_payload(
        self,
        prompt: str,
        session_id: str,
        model: str,
    ) -> dict[str, Any]:
        config = MODEL_CONFIGS[model]
        payload = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "prompt": prompt,
            "ref_file_ids": [],
            "thinking_enabled": bool(config["thinking"]),
            "search_enabled": bool(config["search"]),
            "action": None,
            "preempt": False,
        }
        if config.get("model_type"):
            payload["model_type"] = config["model_type"]
        return payload

    async def open_completion(self, prompt: str, model: str) -> DeepSeekCompletion:
        """Open the upstream SSE response, surfacing HTTP errors before streaming."""
        auth = await asyncio.to_thread(self._auth)
        headers = self._headers(auth)
        client = httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(600.0, connect=30.0),
        )
        try:
            session_id = await self._create_session(client, headers)
            pow_header = await self._pow_header(client, headers, auth)
            payload = self._completion_payload(prompt, session_id, model)
            upstream_headers = {
                **headers,
                "Accept": "text/event-stream",
                "X-DS-PoW-Response": pow_header,
            }
            request = client.build_request(
                "POST",
                BASE_URL + COMPLETION_PATH,
                headers=upstream_headers,
                json=payload,
            )
            response = await client.send(request, stream=True)
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")[:500]
                status = response.status_code
                await response.aclose()
                raise NativeError(
                    429 if status == 429 else (502 if status < 500 else 503),
                    f"DeepSeek completion failed (HTTP {status}): {body}",
                    "upstream_error",
                    retry_after=_retry_after(response),
                )
            return DeepSeekCompletion(client, response, DeepSeekStreamParser())
        except BaseException:
            await client.aclose()
            raise

    async def open_stream(self, payload: dict[str, Any]) -> DeepSeekCompletion:
        model = self._model(payload)
        prompt = format_prompt(payload)
        return await self.open_completion(prompt, model)

    @staticmethod
    def _usage(prompt: str, content: str, reasoning: str) -> dict[str, int]:
        prompt_tokens = max(1, len(prompt) // 4)
        completion_tokens = max(0, (len(content) + len(reasoning)) // 4)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    @staticmethod
    def _response_object(
        model: str,
        content: str,
        reasoning: str,
        tool_calls: list[dict[str, Any]] | None,
        finish_reason: str,
        prompt: str,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if reasoning:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": DeepSeekService._usage(prompt, content, reasoning),
        }

    @staticmethod
    def _chunk(
        response_id: str,
        created: int,
        model: str,
        delta: dict[str, Any],
        finish_reason: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> bytes:
        payload: dict[str, Any] = {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            payload["usage"] = usage
        return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"

    async def _read_completion(self, prompt: str, model: str) -> DeepSeekStreamParser:
        completion = await self.open_completion(prompt, model)
        try:
            async for _delta in completion.deltas():
                pass
        finally:
            await completion.aclose()
        return completion.parser

    @staticmethod
    def _error_event(message: str, code: str) -> bytes:
        return (
            b"data: "
            + json.dumps(
                {
                    "error": {
                        "message": message,
                        "type": "upstream_error",
                        "code": code,
                        "param": None,
                    }
                },
                ensure_ascii=False,
            ).encode()
            + b"\n\n"
        )

    @staticmethod
    def _tool_error(message: str) -> bytes:
        return DeepSeekService._error_event(message, "malformed_tool_call")

    async def stream_chat(
        self, payload: dict[str, Any], completion: DeepSeekCompletion
    ) -> AsyncIterator[bytes]:
        try:
            model = self._model(payload)
        except NativeError as exc:
            await completion.aclose()
            yield self._error_event(exc.message, exc.code)
            yield b"data: [DONE]\n\n"
            return
        prompt = format_prompt(payload)
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        response_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        parser = completion.parser

        yield self._chunk(response_id, created, model, {"role": "assistant"})

        buffered: list[str] = []
        failure: NativeError | None = None
        try:
            async for content_delta, reasoning_delta in completion.deltas():
                if reasoning_delta:
                    yield self._chunk(
                        response_id,
                        created,
                        model,
                        {"reasoning_content": reasoning_delta},
                    )
                if content_delta:
                    if tools:
                        buffered.append(content_delta)
                    else:
                        yield self._chunk(
                            response_id,
                            created,
                            model,
                            {"content": content_delta},
                        )
        except NativeError as exc:
            failure = exc
        except httpx.HTTPError as exc:
            failure = NativeError(
                503,
                f"DeepSeek network error: {type(exc).__name__}",
                "upstream_unavailable",
            )
        finally:
            await completion.aclose()

        if failure is not None:
            yield self._error_event(failure.message, failure.code)
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
                    yield self._tool_error("DeepSeek returned malformed tool-call markup")
                    yield b"data: [DONE]\n\n"
                    return
            for start in range(0, len(content), 120):
                yield self._chunk(
                    response_id,
                    created,
                    model,
                    {"content": content[start : start + 120]},
                )
        if tool_calls:
            yield self._chunk(
                response_id,
                created,
                model,
                {"tool_calls": [{"index": 0, **tool_calls[0]}]},
            )
        usage = self._usage(prompt, content, parser.reasoning)
        yield self._chunk(
            response_id,
            created,
            model,
            {},
            finish_reason=finish_reason,
            usage=usage,
        )
        yield b"data: [DONE]\n\n"

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = self._model(payload)
        prompt = format_prompt(payload)
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        parser = await self._read_completion(prompt, model)
        tool_calls: list[dict[str, Any]] | None = None
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
                    "DeepSeek returned malformed tool-call markup",
                    "malformed_tool_call",
                )
        return self._response_object(
            model,
            content,
            parser.reasoning,
            tool_calls,
            finish_reason,
            prompt,
        )


def create_deepseek_app(
    store_path: Path | str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    paths = AuthPaths.from_env()
    service = DeepSeekService(Path(store_path or paths.store), transport=transport)
    app = FastAPI()

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            await service.check_configured()
        except NativeError:
            return JSONResponse(
                status_code=503,
                content={"healthy": False, "mode": "native-deepseek"},
            )
        return JSONResponse(
            status_code=200,
            content={"healthy": True, "mode": "native-deepseek"},
        )

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "deepseek",
                }
                for model in MODEL_CONFIGS
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
        model = str(payload.get("model") or "deepseek-chat").lower()
        if model not in MODEL_CONFIGS:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": f"Unknown DeepSeek model: {model}",
                        "type": "invalid_model",
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
            logger.warning("native DeepSeek network error: %s", type(exc).__name__)
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": f"DeepSeek network error: {type(exc).__name__}",
                        "type": "upstream_error",
                        "code": "upstream_unavailable",
                        "param": None,
                    }
                },
            )
        except Exception as exc:
            logger.exception("native DeepSeek error")
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": f"Native DeepSeek error: {exc}",
                        "type": "upstream_error",
                        "code": "native_error",
                        "param": None,
                    }
                },
            )

    return app
