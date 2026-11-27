"""Shared OpenAI-compatible plumbing for native providers."""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid
from typing import Any

from ..retry import parse_retry_after

MAX_TOOL_SCHEMA_CHARS = 24_000
MAX_PROMPT_CHARS = 80_000
TOOL_CALL_FORMAT = '{"tool_call":{"name":"<function_name>","arguments":{...}}}'


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


def retry_after_seconds(response: Any) -> int | None:
    return parse_retry_after(response.headers.get("retry-after"))


def content_text(content: Any) -> str:
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


def tool_call_text(tool_call: dict[str, Any]) -> str:
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


def tool_definitions(tools: list[dict[str, Any]]) -> str:
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
    schema_budget = MAX_TOOL_SCHEMA_CHARS
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "")
        description = re.sub(r"\s+", " ", str(function.get("description") or ""))[:500]
        parameters = function.get("parameters") or {}
        schema_text = json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
        if len(schema_text) > schema_budget:
            schema_text = '{"type":"object","additionalProperties":true}'
        schema_budget = max(0, schema_budget - len(schema_text))
        text += f"\n## {name}\n{description}\nParameters: {schema_text}\n"
    text += "\n--- END TOOL REQUEST SYSTEM ---\n"
    return text


def format_prompt(payload: dict[str, Any], *, max_chars: int = MAX_PROMPT_CHARS) -> str:
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
        text = content_text(message.get("content"))
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
                        conversation.append(f"Assistant: {tool_call_text(tool_call)}")
        elif role in {"tool", "function"}:
            name = message.get("name") or tool_names.get(str(message.get("tool_call_id") or ""), "")
            label = f"[Tool Result: {name}]" if name else "[Tool Result]"
            conversation.append(f"{label}\n{text}")

    full = "\n".join(system_parts).strip() + tool_definitions(tools)
    if conversation:
        full += "\n\n" + "\n\n".join(conversation)
    full = full.strip()
    if len(full) > max_chars:
        head = full[:4_000]
        tail = full[-(max_chars - 4_100) :]
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


def estimate_usage(prompt: str, content: str, reasoning: str = "") -> dict[str, int]:
    prompt_tokens = max(1, len(prompt) // 4)
    completion_tokens = max(0, (len(content) + len(reasoning)) // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def openai_chunk(
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


def openai_error_event(message: str, code: str) -> bytes:
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


def openai_response(
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
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": estimate_usage(prompt, content, reasoning),
    }
