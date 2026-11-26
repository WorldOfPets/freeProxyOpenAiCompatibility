from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

from .paths import AuthPaths
from .store import decode_jwt_expiry

_DEEPSEEK_WASM = "https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm"


class AuthCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    name: str
    url: str
    instructions: str
    cookie_domains: tuple[str, ...]
    token_keys: tuple[str, ...]
    grace_seconds: float


SPECS: dict[str, ProviderSpec] = {
    "deepseek": ProviderSpec(
        provider="deepseek",
        name="DeepSeek",
        url="https://chat.deepseek.com/",
        instructions=(
            "Войдите в DeepSeek, откройте чат и отправьте тестовое сообщение "
            "(например: ok). Это нужно, чтобы страница отправила auth-заголовки."
        ),
        cookie_domains=("deepseek.com",),
        token_keys=("userToken", "token", "auth_token", "access_token", "accessToken"),
        grace_seconds=5.0,
    ),
    "qwen": ProviderSpec(
        provider="qwen",
        name="Qwen",
        url="https://chat.qwen.ai/",
        instructions=(
            "Войдите в Qwen Chat и отправьте тестовое сообщение. "
            "Токен и Baxia-заголовки появляются после загрузки чата/первого запроса."
        ),
        cookie_domains=("qwen.ai", "qwenlm.ai"),
        token_keys=("token", "auth_token", "access_token", "accessToken"),
        grace_seconds=5.0,
    ),
    "glm": ProviderSpec(
        provider="glm",
        name="GLM / Z.AI",
        url="https://chat.z.ai/",
        instructions=(
            "Войдите в chat.z.ai. Для GLM нужен ZAI_TOKEN из localStorage; "
            "отправка сообщения не обязательна, но помогает обновить состояние."
        ),
        cookie_domains=("z.ai",),
        token_keys=("token", "auth_token", "access_token", "accessToken"),
        grace_seconds=2.0,
    ),
}


def _active_page(context: BrowserContext) -> Page:
    pages = [page for page in context.pages if not page.is_closed()]
    if not pages:
        raise AuthCaptureError("Browser window was closed before authorization finished")
    return pages[-1]


def _normalize_token(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("value", "token", "access_token", "accessToken"):
                if parsed.get(key):
                    candidate = str(parsed[key]).strip()
                    return re.sub(r"^Bearer\s+", "", candidate, flags=re.IGNORECASE).strip()
    return re.sub(r"^Bearer\s+", "", value, flags=re.IGNORECASE).strip()


def _pick_token(storage: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        token = _normalize_token(storage.get(key))
        if token:
            return token
    for key, value in storage.items():
        if "token" in key.lower():
            token = _normalize_token(value)
            if token:
                return token
    return None


def _cookie_matches(cookie: dict[str, Any], domains: tuple[str, ...]) -> bool:
    domain = str(cookie.get("domain") or "").lower().lstrip(".")
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in domains)


async def _read_context(context: BrowserContext, spec: ProviderSpec) -> dict[str, Any]:
    pages = [page for page in context.pages if not page.is_closed()]
    if not pages:
        raise AuthCaptureError("Browser window was closed before authorization finished")
    combined: dict[str, Any] = {
        "localStorage": {},
        "sessionStorage": {},
        "resources": [],
    }
    for page in pages:
        try:
            state = await _read_page(page, context, spec)
        except PlaywrightError:
            continue
        storage = state.get("storage") or {}
        combined["localStorage"].update(storage.get("localStorage") or {})
        combined["sessionStorage"].update(storage.get("sessionStorage") or {})
        combined["resources"].extend(storage.get("resources") or [])
    cookies = [
        cookie for cookie in await context.cookies() if _cookie_matches(cookie, spec.cookie_domains)
    ]
    return {"storage": combined, "cookies": cookies}


async def _read_page(page: Page, context: BrowserContext, spec: ProviderSpec) -> dict[str, Any]:
    storage = await page.evaluate(
        """() => {
            const out = {localStorage: {}, sessionStorage: {}, resources: []};
            try {
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    out.localStorage[k] = localStorage.getItem(k);
                }
            } catch (e) {}
            try {
                for (let i = 0; i < sessionStorage.length; i++) {
                    const k = sessionStorage.key(i);
                    out.sessionStorage[k] = sessionStorage.getItem(k);
                }
            } catch (e) {}
            try {
                out.resources = performance.getEntriesByType('resource')
                    .map(r => r.name)
                    .filter(n => /sha3.*\\.wasm|wasm.*sha3/i.test(n))
                    .slice(-20);
            } catch (e) {}
            return out;
        }"""
    )
    cookies = [
        cookie for cookie in await context.cookies() if _cookie_matches(cookie, spec.cookie_domains)
    ]
    return {"storage": storage, "cookies": cookies}


def _cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{cookie.get('name')}={cookie.get('value')}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value")
    )


def _clean_headers(headers: dict[str, str], spec: ProviderSpec) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if any(ch in value for ch in "\r\n"):
            continue
        if spec.provider == "deepseek" and lower in {"x-hif-dliq", "x-hif-leim"}:
            cleaned[lower] = value
        if spec.provider == "qwen" and (lower.startswith("bx-") or lower.startswith("x-bx-")):
            cleaned[lower] = value
    return cleaned


def _find_browser(channel: str) -> str | None:
    override = os.environ.get("FP_AUTH_CHROME_PATH") or os.environ.get("CHROME_PATH")
    if override and Path(override).exists():
        return override

    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates = []
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate

    names = (
        ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge")
        if channel.lower() == "chromium"
        else (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
        )
    )
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch_chrome(
    browser_path: str, profile_dir: Path, port: int, url: str
) -> subprocess.Popen[bytes]:
    args = [
        browser_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        url,
    ]
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _terminate_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


async def _wait_for_cdp(port: int, process: subprocess.Popen[bytes]) -> None:
    async with httpx.AsyncClient(timeout=1.0) as client:
        for _ in range(120):
            if process.poll() is not None:
                raise AuthCaptureError(
                    "Chrome завершился при запуске. Проверьте FP_AUTH_CHROME_PATH."
                )
            try:
                response = await client.get(f"http://127.0.0.1:{port}/json/version")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    raise AuthCaptureError("Chrome DevTools endpoint did not start")


def _page_needs_navigation(page: Page, spec: ProviderSpec) -> bool:
    if page.url in {"", "about:blank"}:
        return True
    host = urlsplit(page.url).hostname or ""
    expected = urlsplit(spec.url).hostname or ""
    return not (host == expected or host.endswith("." + expected))


async def _select_page(context: BrowserContext, spec: ProviderSpec) -> Page:
    pages = [page for page in context.pages if not page.is_closed()]
    expected = urlsplit(spec.url).hostname or ""
    for page in pages:
        host = urlsplit(page.url).hostname or ""
        if expected and (host == expected or host.endswith("." + expected)):
            return page
    return pages[-1] if pages else await context.new_page()


async def capture(
    provider: str,
    paths: AuthPaths,
    *,
    headless: bool = False,
    confirm: Callable[[], Awaitable[None]] | None = None,
    cdp_url: str | None = None,
) -> dict[str, Any]:
    spec = SPECS.get(provider)
    if spec is None:
        raise AuthCaptureError(f"Unknown provider: {provider}")

    print(f"\n[{spec.name}] Открываю браузер: {spec.url}")
    print(f"[{spec.name}] {spec.instructions}")
    if confirm is not None:
        print(
            f"[{spec.name}] После входа и тестового сообщения вернитесь в терминал "
            "и подтвердите авторизацию."
        )
    else:
        print(f"[{spec.name}] Ожидаю токен автоматически (Ctrl+C для отмены).\n")

    captured_headers: dict[str, str] = {}
    profile_dir = paths.browser_profile(provider).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    browser: Browser | None = None
    context: BrowserContext | None = None
    chrome_process: subprocess.Popen[bytes] | None = None
    owns_browser = False
    attached_cdp = cdp_url is not None

    print(
        f"[{spec.name}] Если появится Access Verification — пройдите слайдер вручную. "
        "Окно должно быть обычным Chrome без automation-флагов."
    )

    try:
        async with async_playwright() as playwright:
            page: Page | None = None

            if cdp_url:
                try:
                    browser = await playwright.chromium.connect_over_cdp(cdp_url)
                    context = (
                        browser.contexts[0] if browser.contexts else await browser.new_context()
                    )
                    page = await _select_page(context, spec)
                    print(f"[{spec.name}] Подключился к существующему Chrome: {cdp_url}")
                except PlaywrightError as exc:
                    raise AuthCaptureError(
                        f"{spec.name}: не удалось подключиться к CDP {cdp_url}: {exc}"
                    ) from exc
            elif not headless:
                chrome_path = _find_browser(paths.browser_channel)
                if chrome_path:
                    port = _free_port()
                    print(f"[{spec.name}] Запускаю обычный Chrome: {chrome_path}")
                    chrome_process = _launch_chrome(chrome_path, profile_dir, port, spec.url)
                    try:
                        await _wait_for_cdp(port, chrome_process)
                        browser = await playwright.chromium.connect_over_cdp(
                            f"http://127.0.0.1:{port}"
                        )
                        context = (
                            browser.contexts[0] if browser.contexts else await browser.new_context()
                        )
                        page = await _select_page(context, spec)
                        owns_browser = True
                    except Exception as exc:
                        print(
                            f"[{spec.name}] Обычный Chrome не подключился ({exc}); "
                            "пробую Playwright Chromium."
                        )
                        _terminate_process(chrome_process)
                        chrome_process = None
                        browser = None
                        context = None
                        page = None
                else:
                    print(
                        f"[{spec.name}] Chrome не найден; использую Playwright Chromium. "
                        "Капча в нём может не пройти."
                    )

            if context is None:
                fallback_channel = paths.browser_channel
                if _find_browser(paths.browser_channel) is None:
                    fallback_channel = ""
                channel = (
                    None
                    if fallback_channel.lower() in {"", "chromium", "bundled"}
                    else fallback_channel
                )
                kwargs: dict[str, Any] = {
                    "user_data_dir": str(profile_dir),
                    "headless": headless,
                    "viewport": {"width": 1440, "height": 900},
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        channel=channel, **kwargs
                    )
                except PlaywrightError:
                    if not channel:
                        raise AuthCaptureError(
                            "Не удалось запустить Chromium. Выполните "
                            "`uv run playwright install chromium` или укажите "
                            "FP_AUTH_BROWSER_CHANNEL=chrome."
                        ) from None
                    print(f"[{spec.name}] Канал {channel!r} недоступен, пробую bundled Chromium...")
                    context = await playwright.chromium.launch_persistent_context(**kwargs)
                page = await _select_page(context, spec)

            try:
                if page is None:
                    page = _active_page(context)

                def attach(page_: Page) -> None:
                    def on_request(request: Any) -> None:
                        for key, value in request.headers.items():
                            captured_headers[key.lower()] = value

                    page_.on("request", on_request)

                context.on("page", attach)
                attach(page)
                if _page_needs_navigation(page, spec):
                    await page.goto(spec.url, wait_until="domcontentloaded", timeout=60_000)

                if confirm is not None:
                    await confirm()
                    print(f"[{spec.name}] Подтверждение получено; проверяю токен...")

                deadline = time.monotonic() + paths.timeout_seconds
                last_notice = 0.0
                token: str | None = None
                state: dict[str, Any] = {}
                expired_notice = False
                while time.monotonic() < deadline:
                    _active_page(context)
                    try:
                        state = await _read_context(context, spec)
                    except PlaywrightError:
                        await asyncio.sleep(1)
                        continue

                    storage = {
                        **state["storage"].get("localStorage", {}),
                        **state["storage"].get("sessionStorage", {}),
                    }
                    candidate = _pick_token(storage, spec.token_keys)
                    if not candidate:
                        for cookie in state.get("cookies") or []:
                            name = str(cookie.get("name") or "").lower()
                            if "token" in name or "auth" in name:
                                candidate = _normalize_token(cookie.get("value"))
                                if candidate:
                                    break
                    if candidate:
                        expiry = decode_jwt_expiry(candidate)
                        if expiry is not None and expiry <= int(time.time()):
                            if not expired_notice:
                                print(
                                    f"[{spec.name}] Найден просроченный токен; "
                                    "войдите заново, ожидаю свежий."
                                )
                                expired_notice = True
                        else:
                            token = candidate
                            break

                    now = time.monotonic()
                    if now - last_notice > 15:
                        print(f"[{spec.name}] Жду входа/токен...")
                        last_notice = now
                    await asyncio.sleep(1)

                if not token:
                    raise AuthCaptureError(
                        f"{spec.name}: токен не появился за {paths.timeout_seconds} секунд. "
                        "Проверьте вход в браузере и повторите."
                    )

                print(f"[{spec.name}] Токен найден. Собираю cookies/заголовки...")
                await asyncio.sleep(spec.grace_seconds)
                _active_page(context)
                state = await _read_context(context, spec)
                storage = {
                    **state["storage"].get("localStorage", {}),
                    **state["storage"].get("sessionStorage", {}),
                }
                token = _pick_token(storage, spec.token_keys) or token
                cookies = state["cookies"]
                headers = _clean_headers(captured_headers, spec)
                expiry = decode_jwt_expiry(token)
                resources = state["storage"].get("resources") or []

                data: dict[str, Any] = {
                    "token": token,
                    "cookies": cookies,
                    "expires_at": expiry,
                }
                if spec.provider == "deepseek":
                    wasm = next(
                        (url for url in resources if "sha3" in url and ".wasm" in url),
                        _DEEPSEEK_WASM,
                    )
                    data.update(
                        {
                            "cookie": _cookie_header(cookies),
                            "hif_dliq": headers.get("x-hif-dliq", ""),
                            "hif_leim": headers.get("x-hif-leim", ""),
                            "wasm_url": wasm,
                        }
                    )
                elif spec.provider == "qwen":
                    data["baxia_headers"] = headers
                elif spec.provider == "glm":
                    data["zai_token"] = token
                return data
            finally:
                if owns_browser:
                    if browser is not None:
                        with contextlib.suppress(PlaywrightError):
                            await browser.close()
                    _terminate_process(chrome_process)
                elif attached_cdp:
                    pass
                elif context is not None:
                    with contextlib.suppress(PlaywrightError):
                        await context.close()
    except PlaywrightError as exc:
        raise AuthCaptureError(f"{spec.name}: ошибка Playwright: {exc}") from exc
