"""Smoke test for a running FreeProxyAPI gateway.

Usage:
    uv run python scripts/smoke.py
    uv run python scripts/smoke.py --base-url http://127.0.0.1:8080 --model auto
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def _print_json(label: str, payload: Any) -> None:
    print(f"\n{label}:")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])


def main() -> None:
    parser = argparse.ArgumentParser(description="Check health, models, chat and streaming")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("FREEPROXY_BASE_URL", "http://127.0.0.1:8080"),
        help="Gateway base URL without /v1",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("FREEPROXY_API_KEY", ""),
        help="Client API key (or set FREEPROXY_API_KEY)",
    )
    parser.add_argument("--model", default="auto", help="Model alias or namespaced ID")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    headers = _headers(args.api_key)

    with httpx.Client(timeout=120.0) as client:
        health = client.get(f"{base_url}/health")
        if health.status_code != 200:
            _fail(f"/health returned {health.status_code}: {health.text[:500]}")
        _print_json("health", health.json())

        models = client.get(f"{base_url}/v1/models", headers=headers)
        if models.status_code != 200:
            _fail(f"/v1/models returned {models.status_code}: {models.text[:500]}")
        model_ids = [item["id"] for item in models.json().get("data", [])]
        print(f"\nmodels ({len(model_ids)}): {', '.join(model_ids[:20])}")
        if args.model not in model_ids:
            print(f"WARN: requested model '{args.model}' is not in /v1/models")

        payload = {
            "model": args.model,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly: gateway-ok",
                }
            ],
            "stream": False,
            "max_tokens": 64,
        }
        chat = client.post(f"{base_url}/v1/chat/completions", headers=headers, json=payload)
        if chat.status_code != 200:
            _fail(f"chat returned {chat.status_code}: {chat.text[:1000]}")
        data = chat.json()
        choices = data.get("choices") or [{}] if isinstance(data, dict) else [{}]
        content = (choices[0].get("message") or {}).get("content", "")
        print(
            "\nchat:",
            f"provider={chat.headers.get('x-freeproxy-provider')}",
            f"model={chat.headers.get('x-freeproxy-model')}",
            f"content={content!r}",
        )

        stream_payload = {**payload, "stream": True}
        with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            headers=headers,
            json=stream_payload,
        ) as stream:
            if stream.status_code != 200:
                _fail(f"stream returned {stream.status_code}: {stream.read()[:1000]!r}")
            print(
                "\nstream:",
                f"status={stream.status_code}",
                f"provider={stream.headers.get('x-freeproxy-provider')}",
                f"content-type={stream.headers.get('content-type')}",
            )
            preview = b""
            for chunk in stream.iter_bytes():
                preview += chunk
                if len(preview) >= 800:
                    break
            print(preview[:800].decode("utf-8", errors="replace"))

    print("\nOK")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        print(f"FAIL: HTTP error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
