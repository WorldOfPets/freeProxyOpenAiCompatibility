from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from freeproxy.native.common import (
    MAX_TOOL_SCHEMA_CHARS,
    NativeError,
    _find_json_object,
    content_text,
    estimate_usage,
    format_prompt,
    looks_like_tool_markup,
    openai_chunk,
    openai_error_event,
    openai_response,
    parse_tool_call,
    retry_after_seconds,
    tool_call_text,
    tool_definitions,
)


def test_native_error_exposes_fields_and_message():
    error = NativeError(429, "slow down", "rate_limited", retry_after=7)
    assert error.status_code == 429
    assert error.message == "slow down"
    assert error.code == "rate_limited"
    assert error.retry_after == 7
    assert str(error) == "slow down"
    assert isinstance(error, RuntimeError)
    assert NativeError(500, "boom", "upstream_error").retry_after is None


def test_retry_after_seconds_reads_numeric_header():
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": "7"})) == 7
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": " 12 "})) == 12


def test_retry_after_seconds_clamps_to_at_least_one():
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": "0"})) == 1
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": "-5"})) == 1


def test_retry_after_seconds_ignores_missing_and_garbage():
    assert retry_after_seconds(httpx.Response(429)) is None
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": ""})) is None
    assert retry_after_seconds(httpx.Response(429, headers={"retry-after": "soon"})) is None


def test_retry_after_seconds_accepts_http_date_in_future():
    retry_at = datetime.now(UTC) + timedelta(seconds=30)
    response = httpx.Response(
        429,
        headers={"retry-after": format_datetime(retry_at, usegmt=True)},
    )
    seconds = retry_after_seconds(response)
    assert seconds is not None
    assert 20 <= seconds <= 30


def test_retry_after_seconds_clamps_past_http_date_to_one():
    retry_at = datetime.now(UTC) - timedelta(seconds=30)
    response = httpx.Response(
        429,
        headers={"retry-after": format_datetime(retry_at, usegmt=True)},
    )
    assert retry_after_seconds(response) == 1


def test_retry_after_seconds_ignores_invalid_http_date():
    response = httpx.Response(429, headers={"retry-after": "Wed, 99 Zzz 2099 99:99:99 GMT"})
    assert retry_after_seconds(response) is None


def test_content_text_handles_strings_and_parts():
    assert content_text("hello") == "hello"
    assert content_text(["a", {"text": "b"}, {"type": "image_url"}]) == "a\nb\n[image]"
    assert content_text([{"type": "input_image", "image_url": {"url": "x"}}]) == "[image]"


def test_content_text_skips_invalid_parts_and_non_list_values():
    assert content_text([{"type": "text", "text": "c"}, None, 3, {"foo": "bar"}, [1]]) == "c"
    assert content_text(None) == ""
    assert content_text({"text": "x"}) == ""
    assert content_text([]) == ""


def test_content_text_prefers_text_over_image_type():
    assert content_text([{"type": "image_url", "text": "caption"}]) == "caption"


def test_tool_call_text_renders_openai_shaped_dict_arguments():
    text = tool_call_text({"function": {"name": "read_file", "arguments": {"path": "a.txt"}}})
    assert json.loads(text) == {"tool_call": {"name": "read_file", "arguments": {"path": "a.txt"}}}
    assert '"arguments":{"path":"a.txt"}' in text


def test_tool_call_text_parses_valid_json_string_arguments():
    text = tool_call_text({"function": {"name": "read_file", "arguments": '{"path": "a.txt"}'}})
    assert json.loads(text)["tool_call"]["arguments"] == {"path": "a.txt"}


def test_tool_call_text_keeps_invalid_json_arguments_as_string():
    text = tool_call_text({"function": {"name": "x", "arguments": "{not json"}})
    assert json.loads(text)["tool_call"]["arguments"] == "{not json"


def test_tool_call_text_accepts_flat_call_and_missing_parts():
    flat = json.loads(tool_call_text({"name": "lookup", "arguments": {"q": "x"}}))
    assert flat["tool_call"] == {"name": "lookup", "arguments": {"q": "x"}}

    no_arguments = json.loads(tool_call_text({"function": {"name": "x"}}))
    assert no_arguments["tool_call"] == {"name": "x", "arguments": {}}

    no_name = json.loads(tool_call_text({"function": {"arguments": {"a": 1}}}))
    assert no_name["tool_call"] == {"name": "", "arguments": {"a": 1}}


def test_tool_call_text_tolerates_non_dict_input():
    parsed = json.loads(tool_call_text("nope"))  # type: ignore[arg-type]
    assert parsed == {"tool_call": {"name": "", "arguments": {}}}


def test_tool_definitions_empty_list_returns_empty_string():
    assert tool_definitions([]) == ""


def test_tool_definitions_renders_function_tool_with_markers():
    text = tool_definitions(
        [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read   a\n file\tfast",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            }
        ]
    )
    assert "TOOL REQUEST SYSTEM" in text
    assert "END TOOL REQUEST SYSTEM" in text
    assert "## read_file" in text
    assert "Read a file fast" in text
    assert '"path":{"type":"string"}' in text


def test_tool_definitions_skips_non_function_and_malformed_tools():
    text = tool_definitions(
        [
            {"type": "web_search"},
            {"type": "function", "function": "not-a-dict"},
            {"type": "function", "function": {"name": "ok", "parameters": {}}},
            "not-a-tool",
            None,
        ]
    )
    assert "## ok" in text
    assert "Parameters: {}" in text
    assert "web_search" not in text
    assert "not-a-tool" not in text


def test_tool_definitions_falls_back_for_oversized_schema():
    huge = {"type": "object", "description": "x" * (MAX_TOOL_SCHEMA_CHARS + 1)}
    text = tool_definitions([{"type": "function", "function": {"name": "big", "parameters": huge}}])
    assert 'Parameters: {"type":"object","additionalProperties":true}' in text
    assert "x" * 100 not in text


def test_format_prompt_covers_all_roles_and_tool_call_ids():
    prompt = format_prompt(
        {
            "messages": [
                {"role": "system", "content": "system rule"},
                {"role": "developer", "content": "dev rule"},
                {"role": "user", "content": "read a.txt"},
                {"role": "assistant", "content": "on it"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "read_file", "arguments": {"path": "a.txt"}},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
                {"role": "function", "name": "lookup", "content": "fn result"},
            ],
        }
    )
    assert prompt.startswith("system rule\ndev rule")
    assert "User: read a.txt" in prompt
    assert "Assistant: on it" in prompt
    assert 'Assistant: {"tool_call":{"name":"read_file","arguments":{"path":"a.txt"}}}' in prompt
    assert "[Tool Result: read_file]\ncontents" in prompt
    assert "[Tool Result: lookup]\nfn result" in prompt


def test_format_prompt_tool_result_falls_back_to_generic_label():
    prompt = format_prompt(
        {"messages": [{"role": "tool", "tool_call_id": "missing", "content": "x"}]}
    )
    assert prompt == "[Tool Result]\nx"


def test_format_prompt_ignores_non_dict_messages_and_non_list_tools():
    assert format_prompt({"messages": []}) == ""
    assert format_prompt({}) == ""
    assert format_prompt({"messages": ["nope", {"role": "user", "content": "hi"}]}) == "User: hi"
    assert (
        format_prompt({"messages": [{"role": "user", "content": "hi"}], "tools": "nope"})
        == "User: hi"
    )


def test_format_prompt_truncates_long_prompts():
    prompt = format_prompt(
        {"messages": [{"role": "user", "content": "a" * 30_000}]},
        max_chars=10_000,
    )
    assert "...[context compacted]..." in prompt
    assert len(prompt) <= 10_000
    assert prompt.startswith("User: aaa")
    assert prompt.endswith("a" * 10)


def test_find_json_object_handles_nesting_and_string_content():
    assert _find_json_object('prefix {"a": {"b": 1}} suffix') == {"a": {"b": 1}}
    assert _find_json_object('{"a": "{not a key}"}') == {"a": "{not a key}"}
    assert _find_json_object('{"a": "he said \\"hi\\"", "b": 1}') == {
        "a": 'he said "hi"',
        "b": 1,
    }


def test_find_json_object_skips_invalid_object_and_finds_next():
    assert _find_json_object('{bad} then {"ok": 1}') == {"ok": 1}
    assert _find_json_object('[1, 2] "text" {"ok": 2}') == {"ok": 2}


def test_find_json_object_returns_none_without_object():
    assert _find_json_object("no json here") is None
    assert _find_json_object("") is None
    assert _find_json_object('{"unterminated": 1') is None


def test_looks_like_tool_markup_rejects_plain_text_and_empty():
    assert looks_like_tool_markup("") is False
    assert looks_like_tool_markup("   ") is False
    assert looks_like_tool_markup("```\n\n```") is False
    assert looks_like_tool_markup("hello") is False
    assert looks_like_tool_markup("hello {not a tool}") is False


def test_looks_like_tool_markup_detects_markers():
    assert looks_like_tool_markup('```json\n{"tool_call":{"name":"x"}}\n```')
    assert looks_like_tool_markup("TOOL_CALL: read_file")
    assert looks_like_tool_markup("<tool_call>")
    assert looks_like_tool_markup("<|DSML|tool_calls>")
    assert looks_like_tool_markup('"function_call": {}')


def test_parse_tool_call_extracts_envelope_and_fenced_json():
    content = '{"tool_call":{"name":"read_file","arguments":{"path":"a.txt"}}}'
    parsed = parse_tool_call(content)
    assert parsed is not None
    assert parsed[0] == content
    tool_call = parsed[1][0]
    assert tool_call["function"] == {"name": "read_file", "arguments": '{"path":"a.txt"}'}
    assert tool_call["type"] == "function"
    assert re.fullmatch(r"call_[0-9a-f]{24}", tool_call["id"])

    fenced = parse_tool_call(
        'Sure!\n```json\n{"tool_call":{"name":"read_file","arguments":"{\\"a\\": 1}"}}\n```\nDone'
    )
    assert fenced is not None
    assert fenced[1][0]["function"]["arguments"] == '{"a": 1}'


def test_parse_tool_call_accepts_function_call_and_single_tool_calls_list():
    function_call = parse_tool_call('{"function_call":{"name":"f","arguments":"{not json"}}')
    assert function_call is not None
    assert function_call[1][0]["function"] == {"name": "f", "arguments": "{}"}

    listed = parse_tool_call(
        '{"tool_calls":[{"type":"function","function":{"name":"f","arguments":{"a":1}}}]}'
    )
    assert listed is not None
    assert len(listed[1]) == 1
    assert listed[1][0]["function"]["arguments"] == '{"a":1}'


def test_parse_tool_call_ignores_surrounding_text():
    parsed = parse_tool_call('here you go: {"tool_call":{"name":"f","arguments":{}}} done')
    assert parsed is not None
    assert parsed[1][0]["function"]["name"] == "f"

    trailing = parse_tool_call('{"tool_call":{"name":"f"}} trailing {')
    assert trailing is not None
    assert trailing[1][0]["function"]["name"] == "f"


def test_parse_tool_call_normalizes_argument_types():
    as_dict = parse_tool_call('{"tool_call":{"name":"f","arguments":{"a":1}}}')
    assert as_dict is not None
    assert as_dict[1][0]["function"]["arguments"] == '{"a":1}'

    as_json = parse_tool_call('{"tool_call":{"name":"f","arguments":"{\\"a\\": 1}"}}')
    assert as_json is not None
    assert as_json[1][0]["function"]["arguments"] == '{"a": 1}'

    invalid = parse_tool_call('{"tool_call":{"name":"f","arguments":"not-json"}}')
    assert invalid is not None
    assert invalid[1][0]["function"]["arguments"] == "{}"


def test_parse_tool_call_returns_none_for_unusable_content():
    assert parse_tool_call("") is None
    assert parse_tool_call("hello") is None
    assert parse_tool_call("not json") is None
    assert parse_tool_call("[1, 2]") is None
    assert parse_tool_call('{"tool_call":{"arguments":{}}}') is None
    assert (
        parse_tool_call('{"tool_calls":[{"function":{"name":"f"}},{"function":{"name":"g"}}]}')
        is None
    )


def test_estimate_usage_counts_prompt_and_completion():
    assert estimate_usage("", "") == {
        "prompt_tokens": 1,
        "completion_tokens": 0,
        "total_tokens": 1,
    }
    usage = estimate_usage("a" * 8, "b" * 8, "c" * 4)
    assert usage["prompt_tokens"] == 2
    assert usage["completion_tokens"] == 3
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_openai_chunk_serializes_openai_delta():
    chunk = openai_chunk("chatcmpl-1", 123, "glm-4.7", {"content": "привет"}, "stop")
    assert chunk.startswith(b"data: ")
    assert chunk.endswith(b"\n\n")
    assert "привет".encode() in chunk  # ensure_ascii=False keeps raw UTF-8

    payload = json.loads(chunk.removeprefix(b"data: "))
    assert payload["id"] == "chatcmpl-1"
    assert payload["object"] == "chat.completion.chunk"
    assert payload["created"] == 123
    assert payload["model"] == "glm-4.7"
    assert payload["choices"] == [
        {"index": 0, "delta": {"content": "привет"}, "finish_reason": "stop"}
    ]
    assert "usage" not in payload


def test_openai_chunk_includes_usage_only_when_provided():
    usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    payload = json.loads(
        openai_chunk("chatcmpl-1", 0, "m", {}, None, usage).removeprefix(b"data: ")
    )
    assert payload["choices"][0]["finish_reason"] is None
    assert payload["usage"] == usage


def test_openai_error_event_is_openai_shaped():
    payload = json.loads(openai_error_event("boom", "upstream_error").removeprefix(b"data: "))
    assert payload == {
        "error": {
            "message": "boom",
            "type": "upstream_error",
            "code": "upstream_error",
            "param": None,
        }
    }


def test_openai_response_builds_completion_payload():
    payload = openai_response("glm-4.7", "hi", "", None, "stop", "hello!")
    assert payload["object"] == "chat.completion"
    assert payload["id"].startswith("chatcmpl-")
    assert payload["model"] == "glm-4.7"
    assert payload["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hi"},
            "finish_reason": "stop",
        }
    ]
    assert payload["usage"] == estimate_usage("hello!", "hi")


def test_openai_response_includes_reasoning_and_tool_calls():
    tool_calls = [
        {"id": "call_x", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    ]
    payload = openai_response("m", "", "think", tool_calls, "tool_calls", "prompt")
    message = payload["choices"][0]["message"]
    assert message["content"] is None
    assert message["reasoning_content"] == "think"
    assert message["tool_calls"] == tool_calls
    assert payload["choices"][0]["finish_reason"] == "tool_calls"
    assert payload["usage"] == estimate_usage("prompt", "", "think")


def test_openai_response_omits_reasoning_when_absent():
    payload = openai_response("m", "answer", "", None, "stop", "prompt")
    assert "reasoning_content" not in payload["choices"][0]["message"]
    assert "tool_calls" not in payload["choices"][0]["message"]


@pytest.mark.parametrize("content", ["", {}, []])
def test_openai_response_empty_content_becomes_none(content):
    payload = openai_response("m", content, "", None, "stop", "prompt")
    assert payload["choices"][0]["message"]["content"] is None
