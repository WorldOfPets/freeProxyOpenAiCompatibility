"""Check native provider token stores and external HTTP provider health.

Native providers are checked through the auth store; external providers are
probed over HTTP.

Usage:
    uv run python scripts/check_upstreams.py
    uv run python scripts/check_upstreams.py --config config/providers.json
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

from freeproxy.auth.store import TokenStore
from freeproxy.config import Settings, load_proxy_config
from freeproxy.providers.http import build_auth_headers


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe configured providers")
    parser.add_argument("--config", default="config/providers.json")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    config = load_proxy_config(Path(args.config))
    settings = Settings()
    auth = TokenStore(settings.auth_store_path)
    healthy = 0
    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        for provider in config.providers:
            if not provider.enabled:
                print(f"SKIP {provider.id}: disabled")
                continue
            if provider.native:
                data = auth.provider(provider.native) or {}
                ok = bool(data.get("token") or data.get("zai_token"))
                expires_at = data.get("expires_at")
                expired = bool(isinstance(expires_at, (int, float)) and expires_at <= time.time())
                if expired:
                    ok = False
                detail = "auth store ok" if ok else "token missing or expired"
                print(f"{'OK  ' if ok else 'FAIL'} {provider.id}: native ({detail})")
                healthy += int(ok)
                continue
            try:
                response = client.get(
                    provider.health_check_url,
                    headers=build_auth_headers(provider),
                )
                ok = 200 <= response.status_code < 300
                if ok:
                    try:
                        payload = response.json()
                        if isinstance(payload, dict):
                            ok = not (payload.get("ok") is False or payload.get("healthy") is False)
                    except ValueError:
                        pass
                print(
                    f"{'OK  ' if ok else 'FAIL'} {provider.id}: "
                    f"{response.status_code} {provider.health_check_url}"
                )
                healthy += int(ok)
            except httpx.HTTPError as exc:
                print(f"FAIL {provider.id}: {type(exc).__name__} {provider.health_check_url}")

    enabled = sum(1 for provider in config.providers if provider.enabled)
    print(f"\n{healthy}/{enabled} enabled providers healthy")
    if enabled and healthy == 0:
        raise SystemExit(1)
    if healthy == 0:
        print("No enabled providers; the gateway will not serve chat requests.", file=sys.stderr)


if __name__ == "__main__":
    main()
