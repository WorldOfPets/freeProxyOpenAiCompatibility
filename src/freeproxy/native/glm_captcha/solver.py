"""Direct Aliyun captcha solver for the native GLM provider.

This is an in-process port of the GLM-Free-API Python reference scripts
(`device_token_gen.py`, `captcha_param_gen.py`, `generate_device_data.py`,
`replay_template.py`, MIT). It mints a fresh device token from a live
InitCaptchaV3 session, builds the signed VerifyCaptchaV3 payload, and returns
the base64 `captcha_verify_param` consumed by chat.z.ai.

No browser, Node or Go process is involved. The only network calls are to the
Aliyun captcha endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import sqlite3
import threading
import time
import urllib.parse
import uuid
import zlib
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import captcha_param_gen
from .device_token_gen import (
    LIVE_APP_KEY,
    LIVE_PREFIX,
    LIVE_REGION,
    LIVE_SCENE_ID,
    _assert_payload_invariants,
    aes_cbc_encrypt,
    decrypt_device_config,
    fetch_device_config,
    generate_device_token,
)
from .replay_template import N_FIELDS, REPLAY_FIELDS

SCENE_ID = LIVE_SCENE_ID
REGION = LIVE_REGION
PREFIX = LIVE_PREFIX
APP_KEY = LIVE_APP_KEY
GATHER_COST_MS = 4058
API_VERSION = "2023-03-05"
VERIFY_ENDPOINT = "https://no8xfe-verify.captcha-open-southeast.aliyuncs.com/"
CAPTCHA_TTL_SECONDS = 75.0
DEFAULT_TOKEN_DB = "../GLM-Free-API/tokens.sqlite"


class CaptchaError(RuntimeError):
    pass


def _credentials(access_key: str | None = None, secret_key: str | None = None) -> tuple[str, str]:
    """Resolve the Aliyun captcha SDK credentials.

    Values may come from the gateway settings, the environment, or a direct
    caller; they are never stored in the repository. They are required for both
    InitCaptchaV3 and VerifyCaptchaV3, including when device tokens come from
    ``FP_GLM_TOKENS_DB``.
    """
    resolved_access = (access_key or os.getenv("FP_ALIYUN_CAPTCHA_ACCESS_KEY", "")).strip()
    resolved_secret = (secret_key or os.getenv("FP_ALIYUN_CAPTCHA_SECRET_KEY", "")).strip()
    if not resolved_access or not resolved_secret:
        raise CaptchaError(
            "GLM captcha is not configured: set FP_ALIYUN_CAPTCHA_ACCESS_KEY and "
            "FP_ALIYUN_CAPTCHA_SECRET_KEY in .env (Aliyun captcha SDK credentials), "
            "then restart freeproxy."
        )
    return resolved_access, resolved_secret


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_signature(params: dict[str, str], secret: str) -> str:
    canonical = "&".join(
        f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(str(value), safe='')}"
        for key, value in sorted(params.items())
    )
    string_to_sign = f"POST&%2F&{urllib.parse.quote(canonical, safe='')}"
    digest = hmac.new(
        (secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _token_db_path() -> Path | None:
    raw = os.getenv("FP_GLM_TOKENS_DB", "").strip()
    candidates = [Path(raw)] if raw else [Path(DEFAULT_TOKEN_DB)]
    candidates.append(Path("secrets/glm-tokens.sqlite"))
    for path in candidates:
        if path.is_file():
            return path
    return None


def take_device_token(path: Path) -> str | None:
    """Pop one harvested device token from the Go collector database."""
    try:
        connection = sqlite3.connect(str(path), timeout=5)
        try:
            with connection:
                row = connection.execute("SELECT token FROM tokens ORDER BY id LIMIT 1").fetchone()
                if not row:
                    return None
                token = str(row[0])
                connection.execute("DELETE FROM tokens WHERE token = ?", (token,))
                return token
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def _build_fresh_payload(session: dict[str, str], now_ms: int, entry94_jitter_ms: int) -> list[str]:
    """Fresh-session payload builder from live_validate.py (verified path)."""
    fields = list(REPLAY_FIELDS)
    if len(fields) != N_FIELDS:
        raise CaptchaError(f"template must have {N_FIELDS} fields")
    server_ts = int(session["serverTs"])
    init_time = int(session.get("initTime", server_ts - 6606))
    if not (server_ts - 7000 <= init_time <= server_ts - 4000):
        raise CaptchaError(f"initTime→serverTs gap is outside 4-7s: {server_ts - init_time}ms")
    session_id = session["sessionId"]
    embedded = int(session_id.split("-h-")[1].split("-")[0])
    if not (server_ts - 3 <= embedded <= server_ts):
        raise CaptchaError("sessionId timestamp is inconsistent with serverTs")
    if not (1 <= entry94_jitter_ms <= 1000):
        raise CaptchaError("entry-94 jitter is outside the sane range")

    fields[72] = str(init_time)
    fields[42] = session["ip"]
    fields[87] = str(server_ts)
    fields[74] = str(now_ms)

    fixed = fields[43].split("|")
    if fixed[-2].split("-")[0] != "93" or fixed[-1].split("-")[0] != "94":
        raise CaptchaError("template field 43 must end with entries 93/94")
    fixed_costs = [int(entry.split("-")[1]) for entry in fixed[:-2]]
    e93 = now_ms - init_time
    if e93 < max(fixed_costs):
        raise CaptchaError(
            f"non-monotonic logs: entry-93 {e93} < last fixed cost {max(fixed_costs)}"
        )
    fields[43] = "|".join(fixed[:-2] + [f"93-{e93}", f"94-{e93 + entry94_jitter_ms}"])
    uuid2 = session_id.split("-h-")[1].split("-", 1)[1]
    fields[21] = aes_cbc_encrypt(session["sessionKey"], uuid2[-8:])
    _assert_payload_invariants(fields, session)
    return fields


def _build_verify_data(certify_id: str) -> str:
    """Build the signed `data` field (live_validate.py / ver_test.py shape)."""
    arg_value = captcha_param_gen.generate_arg(certify_id)
    ct = int(time.time() * 1000)
    track = {
        "TrackList": {"StartTime": ct},
        "TrackStartTime": ct,
        "VerifyTime": ct + 300,
        "Arg": arg_value,
    }
    json_str = json.dumps(track, separators=(",", ":"), ensure_ascii=False)
    digest = captcha_param_gen.ali_hash(json_str, "0000")
    compressed = zlib.compress((digest + json_str).encode("utf-8"))
    return captcha_param_gen.encrypt(base64.b64encode(compressed))


def _verify_captcha(
    client: httpx.Client,
    certify_id: str,
    data_value: str,
    device_token: str,
    access_key: str,
    secret_key: str,
) -> str:
    captcha_verify_param = {
        "certifyId": certify_id,
        "data": data_value,
        "deviceToken": device_token,
        "sceneId": SCENE_ID,
    }
    params: dict[str, str] = {
        "AccessKeyId": access_key,
        "Action": "VerifyCaptchaV3",
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "Timestamp": _timestamp(),
        "Version": API_VERSION,
        "SceneId": SCENE_ID,
        "CertifyId": certify_id,
        "CaptchaVerifyParam": json.dumps(
            captcha_verify_param, separators=(",", ":"), ensure_ascii=False
        ),
        "SignatureNonce": str(uuid.uuid4()),
    }
    params["Signature"] = generate_signature(params, secret_key)
    response = client.post(VERIFY_ENDPOINT, data=params, timeout=40)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("Success"):
        raise CaptchaError(f"VerifyCaptchaV3 rejected the request: {str(payload)[:400]}")
    result = payload.get("Result") or {}
    if not result.get("VerifyResult"):
        raise CaptchaError(f"VerifyCaptchaV3 returned VerifyResult=false: {str(payload)[:400]}")
    security_token = str(result.get("securityToken") or "")
    response_certify_id = str(result.get("certifyId") or "")
    if not security_token or not response_certify_id:
        raise CaptchaError("VerifyCaptchaV3 succeeded without securityToken/certifyId")
    final_payload = {
        "certifyId": response_certify_id,
        "isSign": True,
        "sceneId": SCENE_ID,
        "securityToken": security_token,
    }
    return base64.b64encode(
        json.dumps(final_payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _init_captcha_only(client: httpx.Client, access_key: str, secret_key: str) -> str:
    params: dict[str, str] = {
        "AccessKeyId": access_key,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "Format": "JSON",
        "Timestamp": _timestamp(),
        "Version": API_VERSION,
        "Action": "InitCaptchaV3",
        "SceneId": SCENE_ID,
        "Language": "en",
        "Mode": "popup",
        "UpLang": "true",
        "SignatureNonce": str(uuid.uuid4()),
    }
    params["Signature"] = generate_signature(params, secret_key)
    response = client.post(
        "https://no8xfe.captcha-open-southeast.aliyuncs.com/",
        data=params,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("Success") or not payload.get("CertifyId"):
        raise CaptchaError(f"InitCaptchaV3 (harvested token) failed: {str(payload)[:400]}")
    return str(payload["CertifyId"])


def _captcha_from_device_token(
    client: httpx.Client,
    device_token: str,
    access_key: str,
    secret_key: str,
) -> str:
    certify_id = _init_captcha_only(client, access_key, secret_key)
    data_value = _build_verify_data(certify_id)
    return _verify_captcha(client, certify_id, data_value, device_token, access_key, secret_key)


def generate_captcha_param(access_key: str | None = None, secret_key: str | None = None) -> str:
    """Synchronously mint one accepted `captcha_verify_param` value."""
    access_key, secret_key = _credentials(access_key, secret_key)
    with httpx.Client(timeout=40, follow_redirects=True) as client:
        token_db = _token_db_path()
        if token_db is not None:
            device_token = take_device_token(token_db)
            if device_token:
                return _captcha_from_device_token(client, device_token, access_key, secret_key)
        init = fetch_device_config(
            access_key,
            secret_key,
            SCENE_ID,
            region=REGION,
            prefix=PREFIX,
            app_key=APP_KEY,
        )
        certify_id = str(init.get("CertifyId") or "")
        device_config = str(init.get("DeviceConfig") or "")
        if not certify_id or not device_config:
            raise CaptchaError(
                f"InitCaptchaV3 response is missing CertifyId/DeviceConfig: {str(init)[:400]}"
            )
        session = decrypt_device_config(device_config)
        session["region"] = "SG_WEB"
        # Match the live SDK cadence: mint after initTime+15s (±1.5s). This
        # keeps the log-cost sequence monotonic and f74-serverTs in the normal
        # ~8-10s envelope observed in live traffic.
        init_time = int(session.get("initTime", int(session["serverTs"]) - 6606))
        target_ms = init_time + 15_000 + random.randint(0, 1_500)
        now_ms = int(time.time() * 1000)
        wait_ms = max(0, target_ms - now_ms)
        if wait_ms:
            time.sleep(min(wait_ms / 1000.0, 25.0))
        payload = _build_fresh_payload(session, max(now_ms, target_ms), random.randint(9, 15))
        device_token = generate_device_token(session, GATHER_COST_MS, payload=payload)
        data_value = _build_verify_data(certify_id)
        return _verify_captcha(client, certify_id, data_value, device_token, access_key, secret_key)


class GlmCaptchaSolver:
    """Thread-safe cache around the slow Aliyun captcha generation."""

    def __init__(
        self,
        *,
        ttl_seconds: float = CAPTCHA_TTL_SECONDS,
        access_key: str = "",
        secret_key: str = "",
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._access_key = access_key.strip()
        self._secret_key = secret_key.strip()
        self._cache: deque[tuple[float, str]] = deque()
        self._lock = threading.Lock()
        self._async_lock = asyncio.Lock()
        self._prefetch: asyncio.Task[None] | None = None

    async def _prefetch_one(self) -> None:
        try:
            value = await asyncio.to_thread(self.generate)
            self._store(value)
        except Exception:
            return

    def _schedule_prefetch(self) -> None:
        if self._prefetch is None or self._prefetch.done():
            self._prefetch = asyncio.create_task(self._prefetch_one())

    def _take_cached(self) -> str | None:
        cutoff = time.monotonic() - self.ttl_seconds
        with self._lock:
            while self._cache and self._cache[0][0] < cutoff:
                self._cache.popleft()
            if self._cache:
                return self._cache.popleft()[1]
        return None

    def _store(self, value: str) -> None:
        with self._lock:
            self._cache.append((time.monotonic(), value))

    def generate(self) -> str:
        return generate_captcha_param(self._access_key or None, self._secret_key or None)

    async def get(self) -> str:
        cached = self._take_cached()
        if cached is not None:
            return cached
        async with self._async_lock:
            cached = self._take_cached()
            if cached is not None:
                return cached
            try:
                value = await asyncio.to_thread(self.generate)
            except CaptchaError:
                raise
            except Exception as exc:
                raise CaptchaError(
                    f"Aliyun captcha generation failed: {type(exc).__name__}: {exc}"
                ) from exc
            self._schedule_prefetch()
            return value

    async def warm(self) -> str:
        value = await asyncio.to_thread(self.generate)
        self._store(value)
        return value
