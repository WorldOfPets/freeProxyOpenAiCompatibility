from __future__ import annotations

from types import SimpleNamespace

import pytest

from freeproxy.auth import capture
from freeproxy.auth.capture import SPECS, ProviderSpec


@pytest.mark.parametrize("provider", ["deepseek", "qwen", "glm"])
def test_specs_are_complete(provider: str):
    spec = SPECS[provider]
    assert isinstance(spec, ProviderSpec)
    assert spec.provider == provider
    assert spec.name
    assert spec.url.startswith("https://")
    assert spec.instructions
    assert spec.cookie_domains and all(spec.cookie_domains)
    assert spec.token_keys and all(spec.token_keys)
    assert spec.grace_seconds > 0


def test_specs_registry_keys_match_provider_ids():
    assert set(SPECS) == {"deepseek", "qwen", "glm"}
    for key, spec in SPECS.items():
        assert spec.provider == key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("plain-token", "plain-token"),
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ('{"value": "abc"}', "abc"),
        ('{"token": "abc"}', "abc"),
        ('{"access_token": "abc"}', "abc"),
        ('{"accessToken": "abc"}', "abc"),
        ('{"value": "Bearer abc"}', "abc"),
        ('{ "value": " abc " }', "abc"),
        ("{not json", "{not json"),
        ('{"unrelated": "x"}', '{"unrelated": "x"}'),
        (12345, "12345"),
    ],
)
def test_normalize_token(raw: object, expected: str | None):
    assert capture._normalize_token(raw) == expected


def test_pick_token_uses_configured_key_order():
    storage = {"token": "second", "userToken": "first"}
    assert capture._pick_token(storage, ("userToken", "token")) == "first"
    assert capture._pick_token(storage, ("token", "userToken")) == "second"


def test_pick_token_falls_back_to_any_token_like_key():
    assert capture._pick_token({"MyAuthToken": "fallback"}, ("token",)) == "fallback"
    assert capture._pick_token({"access_token": "Bearer direct"}, ("missing",)) == "direct"


def test_pick_token_skips_empty_values_and_returns_none():
    assert capture._pick_token({"token": "", "auth_token": "   "}, ("token", "auth_token")) is None
    assert capture._pick_token({}, ("token",)) is None


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        (".qwen.ai", True),
        ("qwen.ai", True),
        ("chat.qwen.ai", True),
        ("sub.chat.qwen.ai", True),
        (".QWEN.AI", True),
        ("evilqwen.ai", False),
        ("qwen.ai.evil.com", False),
        ("", False),
        (None, False),
    ],
)
def test_cookie_matches_uses_domain_suffix_boundaries(domain: object, expected: bool):
    assert capture._cookie_matches({"domain": domain}, ("qwen.ai",)) is expected


def test_cookie_header_joins_and_skips_incomplete_cookies():
    cookies = [
        {"name": "a", "value": "1"},
        {"name": "b", "value": "2", "domain": ".qwen.ai"},
        {"name": "", "value": "3"},
        {"name": "c", "value": ""},
        {"name": "d"},
    ]
    assert capture._cookie_header(cookies) == "a=1; b=2"
    assert capture._cookie_header([]) == ""


def test_clean_headers_deepseek_keeps_only_lowercase_hif_headers():
    headers = {
        "X-HIF-DLIQ": "dliq",
        "x-hif-leim": "leim",
        "Authorization": "Bearer secret",
        "Cookie": "a=b",
        "X-HIF-DLIQ-Extra": "nope",
        "X-Other": "nope",
    }
    assert capture._clean_headers(headers, SPECS["deepseek"]) == {
        "x-hif-dliq": "dliq",
        "x-hif-leim": "leim",
    }


def test_clean_headers_drops_values_with_line_breaks():
    cleaned = capture._clean_headers(
        {"x-hif-dliq": "value\r\nX-Evil: 1", "x-hif-leim": "ok"},
        SPECS["deepseek"],
    )
    assert cleaned == {"x-hif-leim": "ok"}


def test_clean_headers_qwen_keeps_only_baxia_headers():
    headers = {
        "bx-ua": "ua",
        "X-BX-UMIDTOKEN": "umid",
        "Authorization": "Bearer secret",
        "Cookie": "a=b",
        "x-qwen": "nope",
    }
    assert capture._clean_headers(headers, SPECS["qwen"]) == {
        "bx-ua": "ua",
        "x-bx-umidtoken": "umid",
    }


def test_clean_headers_glm_keeps_nothing():
    headers = {"bx-ua": "ua", "x-hif-dliq": "d", "Authorization": "Bearer secret"}
    assert capture._clean_headers(headers, SPECS["glm"]) == {}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", True),
        ("about:blank", True),
        ("https://chat.qwen.ai/", False),
        ("https://chat.qwen.ai/chat/123", False),
        ("https://sub.chat.qwen.ai/x", False),
        ("https://evil.example/", True),
        ("https://notchat.qwen.ai/", True),
    ],
)
def test_page_needs_navigation_compares_hosts(url: str, expected: bool):
    page = SimpleNamespace(url=url)
    assert capture._page_needs_navigation(page, SPECS["qwen"]) is expected


def test_find_browser_prefers_existing_env_override(tmp_path, monkeypatch):
    chrome = tmp_path / "chrome"
    chrome.write_text("", encoding="utf-8")
    monkeypatch.setenv("FP_AUTH_CHROME_PATH", str(chrome))
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(capture.shutil, "which", lambda name: pytest.fail("which called"))
    assert capture._find_browser("chrome") == str(chrome)


def test_find_browser_ignores_missing_override_and_uses_path_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("FP_AUTH_CHROME_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(capture.sys, "platform", "linux")

    def fake_which(name: str) -> str | None:
        return "/usr/bin/google-chrome" if name == "google-chrome" else None

    monkeypatch.setattr(capture.shutil, "which", fake_which)
    assert capture._find_browser("chrome") == "/usr/bin/google-chrome"


def test_find_browser_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.delenv("FP_AUTH_CHROME_PATH", raising=False)
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(capture.sys, "platform", "linux")
    monkeypatch.setattr(capture.shutil, "which", lambda name: None)
    assert capture._find_browser("chrome") is None


def test_find_browser_checks_chromium_first_for_chromium_channel(monkeypatch):
    monkeypatch.delenv("FP_AUTH_CHROME_PATH", raising=False)
    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(capture.sys, "platform", "linux")
    calls: list[str] = []

    def fake_which(name: str) -> str | None:
        calls.append(name)
        return None

    monkeypatch.setattr(capture.shutil, "which", fake_which)
    assert capture._find_browser("chromium") is None
    assert calls == [
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "microsoft-edge",
    ]
