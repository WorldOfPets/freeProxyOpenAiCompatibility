"""Auth-store reads must not block the event loop.

The auth store does synchronous file I/O, so async callers offload it with
``asyncio.to_thread``. These tests pin that behaviour for the native Qwen/GLM
transports and the admin auth endpoints without touching a real store, browser
or network.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

import freeproxy.api.admin as admin_module
import freeproxy.native.glm as glm_module
import freeproxy.native.qwen as qwen_module
from freeproxy.auth.paths import AuthPaths
from freeproxy.native.common import NativeError
from freeproxy.native.glm import GlmHttpTransport
from freeproxy.native.qwen import QwenBrowserTransport

from .conftest import gateway, make_config, make_settings


def _install_recording_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seen: list[int],
) -> None:
    """Record which thread resolves AuthPaths.from_env (it reads .env)."""

    class FakeAuthPaths:
        @classmethod
        def from_env(cls) -> AuthPaths:
            seen.append(threading.get_ident())
            return AuthPaths(
                store=tmp_path / "tokens.json",
                browser_dir=tmp_path / "browser",
                deepseek_repo=tmp_path / "FreeDeepseekAPI",
                qwen_repo=tmp_path / "FreeQwenApi",
                glm_repo=tmp_path / "GLM-Free-API",
            )

    monkeypatch.setattr(admin_module, "AuthPaths", FakeAuthPaths)


def _install_recording_store(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    result: dict[str, Any],
    seen: list[int],
) -> None:
    class FakeStore:
        def __init__(self, path: Path | str) -> None:
            self.path = Path(path)

        def provider(self, provider: str) -> dict[str, Any]:
            seen.append(threading.get_ident())
            return result

    monkeypatch.setattr(module, "TokenStore", FakeStore)


# --- Qwen / GLM transports -------------------------------------------------


async def test_qwen_auth_async_reads_store_in_worker_thread(tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    seen: list[int] = []
    _install_recording_store(monkeypatch, qwen_module, {"token": "qwen-token"}, seen)
    transport = QwenBrowserTransport(tmp_path / "tokens.json")

    auth = await transport._auth_async()

    assert auth == {"token": "qwen-token"}
    assert len(seen) == 1
    assert seen[0] != main_thread


async def test_glm_auth_async_reads_store_in_worker_thread(tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    seen: list[int] = []
    _install_recording_store(
        monkeypatch,
        glm_module,
        {"token": "glm-token", "user_id": "u1"},
        seen,
    )
    transport = GlmHttpTransport(tmp_path / "tokens.json")

    auth = await transport._auth_async()

    assert auth == ("glm-token", "u1")
    assert len(seen) == 1
    assert seen[0] != main_thread


# --- Qwen startup ordering -------------------------------------------------


class _FakePage:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def expose_function(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeContext:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.browser = browser
        self.pages: list[_FakePage] = []

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def add_cookies(self, cookies: Any) -> None:
        return None

    async def add_init_script(self, script: str) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeBrowser:
    async def new_context(self, **kwargs: Any) -> _FakeContext:
        return _FakeContext(self)

    async def close(self) -> None:
        return None


class _FakeChromium:
    async def launch(self, **kwargs: Any) -> _FakeBrowser:
        return _FakeBrowser()

    async def launch_persistent_context(self, **kwargs: Any) -> _FakeContext:
        raise AssertionError("persistent profile is not used in this test")


class _FakePlaywright:
    def __init__(self, order: list[str]) -> None:
        self.chromium = _FakeChromium()
        self.start_count = 0
        self.stop_count = 0
        self._order = order

    async def start(self) -> _FakePlaywright:
        self._order.append("start")
        self.start_count += 1
        return self

    async def stop(self) -> None:
        self.stop_count += 1


def _install_playwright(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> _FakePlaywright:
    fake = _FakePlaywright(order)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: fake)
    return fake


async def test_qwen_startup_does_not_touch_playwright_without_auth(tmp_path, monkeypatch):
    order: list[str] = []

    async def fake_auth_async() -> dict[str, Any]:
        raise NativeError(503, "no auth", "provider_not_configured")

    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    monkeypatch.setattr(transport, "_auth_async", fake_auth_async)
    fake = _install_playwright(monkeypatch, order)

    with pytest.raises(NativeError) as info:
        await transport._ensure_started()

    assert info.value.code == "provider_not_configured"
    assert order == []
    assert fake.start_count == 0
    assert transport._playwright is None


async def test_qwen_startup_reads_auth_before_playwright(tmp_path, monkeypatch):
    order: list[str] = []

    async def fake_auth_async() -> dict[str, Any]:
        order.append("auth")
        return {"token": "qwen-token"}

    transport = QwenBrowserTransport(tmp_path / "tokens.json")
    monkeypatch.setattr(transport, "_auth_async", fake_auth_async)
    fake = _install_playwright(monkeypatch, order)

    await transport._ensure_started()

    assert order == ["auth", "start"]
    assert fake.start_count == 1
    assert transport._page is not None
    assert transport._closed is False


# --- Admin endpoints -------------------------------------------------------


async def test_admin_auth_offloads_store_status(tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    seen: list[int] = []
    paths_seen: list[int] = []
    _install_recording_paths(monkeypatch, tmp_path, paths_seen)

    class FakeStore:
        def __init__(self, path: Path | str) -> None:
            self.path = Path(path)

        def status(self) -> dict[str, Any]:
            seen.append(threading.get_ident())
            return {"qwen": {"configured": True}}

    monkeypatch.setattr(admin_module, "TokenStore", FakeStore)

    async def handler(request):
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.get(
            "/admin/auth",
            headers={"X-Admin-Key": "admin-key-0123456789abcdef"},
        )

    assert response.status_code == 200
    assert response.json()["providers"] == {"qwen": {"configured": True}}
    assert response.json()["store"].endswith("tokens.json")
    assert len(paths_seen) == 1
    assert paths_seen[0] != main_thread
    assert len(seen) == 1
    assert seen[0] != main_thread


async def test_admin_auth_export_offloads_export_all(tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    seen: list[int] = []
    paths_seen: list[int] = []
    _install_recording_paths(monkeypatch, tmp_path, paths_seen)

    def fake_export_all(store, paths):
        seen.append(threading.get_ident())
        return {"qwen": ["/tmp/x"]}

    monkeypatch.setattr(admin_module, "export_all", fake_export_all)

    async def handler(request):
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.post(
            "/admin/auth/export",
            headers={
                "X-Admin-Key": "admin-key-0123456789abcdef",
                "X-FreeProxy-Admin": "true",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"exported": {"qwen": ["/tmp/x"]}}
    assert len(paths_seen) == 1
    assert paths_seen[0] != main_thread
    assert len(seen) == 1
    assert seen[0] != main_thread
