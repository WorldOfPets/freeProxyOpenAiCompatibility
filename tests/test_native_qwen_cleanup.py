"""Regression tests for Qwen browser startup cleanup.

Only fake Playwright objects are used: no browser is launched and no network
call is made.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from freeproxy.native.common import NativeError
from freeproxy.native.qwen import QwenBrowserTransport


class FakePage:
    def __init__(
        self,
        *,
        goto_error: BaseException | None = None,
        expose_error: BaseException | None = None,
    ) -> None:
        self.closed = False
        self.goto_count = 0
        self.exposed: list[str] = []
        self._goto_error = goto_error
        self._expose_error = expose_error

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, *args: Any, **kwargs: Any) -> None:
        self.goto_count += 1
        if self._goto_error is not None:
            raise self._goto_error

    async def expose_function(self, name: str, fn: Any) -> None:
        if self._expose_error is not None:
            raise self._expose_error
        self.exposed.append(name)

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(
        self,
        browser: FakeBrowser | None,
        *,
        pages: list[FakePage] | None = None,
        page_error: BaseException | None = None,
        goto_error: BaseException | None = None,
        expose_error: BaseException | None = None,
    ) -> None:
        self.browser = browser
        self.pages: list[FakePage] = list(pages or [])
        self.close_count = 0
        self.new_page_count = 0
        self.added_cookies: list[Any] = []
        self.init_scripts: list[str] = []
        self._page_error = page_error
        self._goto_error = goto_error
        self._expose_error = expose_error

    async def new_page(self) -> FakePage:
        self.new_page_count += 1
        if self._page_error is not None:
            raise self._page_error
        page = FakePage(goto_error=self._goto_error, expose_error=self._expose_error)
        self.pages.append(page)
        return page

    async def add_cookies(self, cookies: Any) -> None:
        self.added_cookies.append(cookies)

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def close(self) -> None:
        self.close_count += 1


class FakeBrowser:
    def __init__(
        self,
        *,
        context_error: BaseException | None = None,
        page_error: BaseException | None = None,
        goto_error: BaseException | None = None,
        expose_error: BaseException | None = None,
    ) -> None:
        self.close_count = 0
        self.contexts: list[FakeContext] = []
        self._context_error = context_error
        self._page_error = page_error
        self._goto_error = goto_error
        self._expose_error = expose_error

    async def new_context(self, **kwargs: Any) -> FakeContext:
        if self._context_error is not None:
            raise self._context_error
        context = FakeContext(
            self,
            page_error=self._page_error,
            goto_error=self._goto_error,
            expose_error=self._expose_error,
        )
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.close_count += 1


class FakeChromium:
    def __init__(self) -> None:
        self.launch_count = 0
        self.launch_kwargs: list[dict[str, Any]] = []
        self.launch_errors: list[BaseException | None] = []
        self.browser_factory: Any = FakeBrowser
        self.persistent_count = 0
        self.persistent_kwargs: list[dict[str, Any]] = []
        self.persistent_error: BaseException | None = None
        self.persistent_context: FakeContext | None = None

    async def launch(self, **kwargs: Any) -> FakeBrowser:
        index = self.launch_count
        self.launch_count += 1
        self.launch_kwargs.append(kwargs)
        if index < len(self.launch_errors) and self.launch_errors[index] is not None:
            raise self.launch_errors[index]
        return self.browser_factory()

    async def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
        self.persistent_count += 1
        self.persistent_kwargs.append(kwargs)
        if self.persistent_error is not None:
            raise self.persistent_error
        if self.persistent_context is None:
            self.persistent_context = FakeContext(FakeBrowser())
        return self.persistent_context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.start_count = 0
        self.stop_count = 0

    async def start(self) -> FakePlaywright:
        self.start_count += 1
        return self

    async def stop(self) -> None:
        self.stop_count += 1


def install_playwright(monkeypatch: pytest.MonkeyPatch, chromium: FakeChromium) -> FakePlaywright:
    fake = FakePlaywright(chromium)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: fake)
    return fake


def make_transport(tmp_path: Path, *, profile_dir: Path | None = None) -> QwenBrowserTransport:
    return QwenBrowserTransport(tmp_path / "tokens.json", profile_dir=profile_dir)


def assert_clean(transport: QwenBrowserTransport) -> None:
    assert transport._playwright is None
    assert transport._browser is None
    assert transport._context is None
    assert transport._page is None
    assert transport._closed is False


async def test_startup_launch_failure_releases_playwright(tmp_path, monkeypatch):
    chromium = FakeChromium()
    chromium.launch_errors = [RuntimeError("channel unavailable"), RuntimeError("no chromium")]
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path)

    with pytest.raises(RuntimeError, match="no chromium"):
        await transport._ensure_started()

    assert chromium.launch_count == 2
    assert fake.stop_count == 1
    assert_clean(transport)


async def test_startup_new_context_failure_closes_browser(tmp_path, monkeypatch):
    browser = FakeBrowser(context_error=RuntimeError("context boom"))
    chromium = FakeChromium()
    chromium.browser_factory = lambda: browser
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path)

    with pytest.raises(RuntimeError, match="context boom"):
        await transport._ensure_started()

    assert browser.close_count == 1
    assert fake.stop_count == 1
    assert_clean(transport)


async def test_startup_new_page_failure_closes_context_and_browser(tmp_path, monkeypatch):
    browser = FakeBrowser(page_error=RuntimeError("page boom"))
    chromium = FakeChromium()
    chromium.browser_factory = lambda: browser
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path)

    with pytest.raises(RuntimeError, match="page boom"):
        await transport._ensure_started()

    assert browser.contexts and browser.contexts[0].close_count == 1
    assert browser.close_count == 1
    assert fake.stop_count == 1
    assert_clean(transport)


async def test_startup_goto_failure_closes_context_and_browser(tmp_path, monkeypatch):
    browser = FakeBrowser(goto_error=RuntimeError("goto boom"))
    chromium = FakeChromium()
    chromium.browser_factory = lambda: browser
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path)

    with pytest.raises(RuntimeError, match="goto boom"):
        await transport._ensure_started()

    assert browser.contexts and browser.contexts[0].close_count == 1
    assert browser.close_count == 1
    assert fake.stop_count == 1
    assert_clean(transport)


async def test_retry_after_failed_startup_succeeds(tmp_path, monkeypatch):
    chromium = FakeChromium()
    chromium.launch_errors = [
        RuntimeError("transient"),
        RuntimeError("transient"),
    ]
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path)

    with pytest.raises(RuntimeError, match="transient"):
        await transport._ensure_started()
    assert_clean(transport)

    await transport._ensure_started()

    assert transport._page is not None
    assert transport._closed is False
    assert fake.start_count == 2
    assert fake.stop_count == 1


async def test_persistent_launch_failure_falls_back_without_stopping_playwright(
    tmp_path, monkeypatch
):
    profile = tmp_path / "profile"
    profile.mkdir()
    chromium = FakeChromium()
    chromium.persistent_error = RuntimeError("profile boom")
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path, profile_dir=profile)

    await transport._ensure_started()

    assert transport._page is not None
    assert chromium.persistent_count == 1
    assert chromium.launch_count == 1
    assert fake.stop_count == 0


async def test_persistent_page_failure_closes_context_and_falls_back(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    persistent = FakeContext(FakeBrowser(), goto_error=RuntimeError("profile page boom"))
    chromium = FakeChromium()
    chromium.persistent_context = persistent
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path, profile_dir=profile)

    await transport._ensure_started()

    assert transport._page is not None
    assert persistent.close_count == 1
    assert chromium.launch_count == 1
    assert fake.stop_count == 0


async def test_aclose_is_idempotent(tmp_path, monkeypatch):
    chromium = FakeChromium()
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path)
    await transport._ensure_started()
    browser = transport._browser
    context = transport._context
    assert browser is not None and context is not None

    await transport.aclose()
    await transport.aclose()

    assert fake.stop_count == 1
    assert browser.close_count == 1
    assert context.close_count == 1
    assert transport._closed is True
    assert transport._page is None


async def test_live_page_does_not_restart_playwright(tmp_path, monkeypatch):
    chromium = FakeChromium()
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path)

    await transport._ensure_started()
    await transport._ensure_started()

    assert fake.start_count == 1
    assert chromium.launch_count == 1


async def test_closed_transport_raises_without_starting(tmp_path, monkeypatch):
    chromium = FakeChromium()
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path)
    transport._closed = True

    with pytest.raises(NativeError) as info:
        await transport._ensure_started()

    assert info.value.code == "native_closed"
    assert fake.start_count == 0
    assert chromium.launch_count == 0


async def test_cancelled_startup_still_cleans_up(tmp_path, monkeypatch):
    browser = FakeBrowser(goto_error=asyncio.CancelledError())
    chromium = FakeChromium()
    chromium.browser_factory = lambda: browser
    fake = install_playwright(monkeypatch, chromium)
    transport = make_transport(tmp_path)

    with pytest.raises(asyncio.CancelledError):
        await transport._ensure_started()

    assert browser.contexts and browser.contexts[0].close_count == 1
    assert browser.close_count == 1
    assert fake.stop_count == 1
    assert_clean(transport)
