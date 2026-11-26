from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

_STORE_VERSION = 1


class AuthStoreError(RuntimeError):
    pass


def decode_jwt_expiry(token: str) -> int | None:
    """Return a JWT exp timestamp without verifying the signature."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    import base64

    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    return int(exp) if isinstance(exp, (int, float)) else None


def secure_write(path: Path, text: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.parent.chmod(0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp_name)


class TokenStore:
    def __init__(self, path: Path | str = Path("secrets/tokens.json")) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": _STORE_VERSION, "providers": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthStoreError(f"Cannot read auth store {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AuthStoreError(f"Auth store {self.path} is not a JSON object")
        providers = data.get("providers")
        if not isinstance(providers, dict):
            providers = {}
        return {"version": _STORE_VERSION, "providers": providers}

    def save(self, data: dict[str, Any]) -> None:
        payload = {
            "version": _STORE_VERSION,
            "providers": data.get("providers", {}),
        }
        secure_write(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def provider(self, provider: str) -> dict[str, Any] | None:
        value = self.load()["providers"].get(provider)
        return value if isinstance(value, dict) else None

    def upsert(self, provider: str, values: dict[str, Any]) -> dict[str, Any]:
        if provider not in {"deepseek", "qwen", "glm"}:
            raise AuthStoreError(f"Unknown provider: {provider}")
        data = self.load()
        provider_data = dict(data["providers"].get(provider) or {})
        provider_data.update(values)
        provider_data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        token = str(provider_data.get("token") or "")
        if token:
            expiry = decode_jwt_expiry(token)
            if expiry is not None:
                provider_data["expires_at"] = expiry
            else:
                provider_data.pop("expires_at", None)
        data["providers"][provider] = provider_data
        self.save(data)
        return provider_data

    def status(self) -> dict[str, Any]:
        data = self.load()["providers"]
        now = int(time.time())
        result: dict[str, Any] = {}
        for provider in ("deepseek", "qwen", "glm"):
            value = data.get(provider)
            if not isinstance(value, dict):
                result[provider] = {"configured": False}
                continue
            token = str(value.get("token") or "")
            expires_at = value.get("expires_at")
            if not isinstance(expires_at, (int, float)):
                expires_at = decode_jwt_expiry(token) if token else None
            expires_int = int(expires_at) if isinstance(expires_at, (int, float)) else None
            result[provider] = {
                "configured": bool(token),
                "token_length": len(token),
                "updated_at": value.get("updated_at"),
                "expires_at": expires_int,
                "expired": bool(expires_int and expires_int <= now),
                "has_cookies": bool(value.get("cookies") or value.get("cookie")),
                "has_baxia": bool(value.get("baxia_headers")),
                "has_deepseek_headers": bool(value.get("hif_dliq") or value.get("hif_leim")),
            }
        return result
