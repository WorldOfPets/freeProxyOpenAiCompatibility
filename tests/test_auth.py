from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from freeproxy.auth import cli as auth_cli
from freeproxy.auth.exporters import export_all
from freeproxy.auth.paths import AuthPaths
from freeproxy.auth.store import TokenStore, decode_jwt_expiry

from .conftest import gateway, make_config, make_settings


def _jwt(payload: dict) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


def _paths(tmp_path: Path) -> AuthPaths:
    return AuthPaths(
        store=tmp_path / "secrets" / "tokens.json",
        browser_dir=tmp_path / "secrets" / "browser",
        deepseek_repo=tmp_path / "FreeDeepseekAPI",
        qwen_repo=tmp_path / "FreeQwenApi",
        glm_repo=tmp_path / "GLM-Free-API",
    )


def test_jwt_expiry_decode():
    token = _jwt({"exp": 2_000_000_000})
    assert decode_jwt_expiry(token) == 2_000_000_000
    assert decode_jwt_expiry("opaque-token") is None


def test_token_store_status_redacts_tokens(tmp_path):
    paths = _paths(tmp_path)
    store = TokenStore(paths.store)
    token = _jwt({"exp": 2_000_000_000})
    store.upsert("qwen", {"token": token, "cookies": [{"name": "a", "value": "b"}]})
    status = store.status()["qwen"]
    assert status["configured"] is True
    assert status["expired"] is False
    assert token not in json.dumps(status)
    assert status["token_length"] == len(token)


def test_export_all_writes_native_files(tmp_path):
    paths = _paths(tmp_path)
    for repo in (paths.deepseek_repo, paths.qwen_repo, paths.glm_repo):
        repo.mkdir(parents=True)
    (paths.qwen_repo / "session").mkdir()
    (paths.qwen_repo / "session" / "tokens.json").write_text(
        json.dumps([{"id": "acc_existing", "token": "old", "resetAt": None}]),
        encoding="utf-8",
    )

    store = TokenStore(paths.store)
    qwen_token = _jwt({"exp": 2_000_000_000})
    store.upsert(
        "deepseek",
        {
            "token": "deepseek-token",
            "cookie": "ds_session_id=abc; smidV2=def",
            "hif_dliq": "dliq",
            "hif_leim": "leim",
            "wasm_url": "https://example.test/sha3.wasm",
        },
    )
    store.upsert(
        "qwen",
        {
            "token": qwen_token,
            "cookies": [{"name": "qwen-cookie", "value": "yes", "domain": ".qwen.ai"}],
            "baxia_headers": {"bx-ua": "ua", "bx-umidtoken": "umid"},
        },
    )
    store.upsert("glm", {"token": _jwt({"exp": 2_000_000_000})})

    exported = export_all(store, paths)
    assert set(exported) == {"deepseek", "qwen", "glm"}

    deepseek_auth = json.loads(
        (paths.deepseek_repo / "deepseek-auth.json").read_text(encoding="utf-8")
    )
    assert deepseek_auth["token"] == "deepseek-token"
    assert deepseek_auth["cookie"] == "ds_session_id=abc; smidV2=def"
    assert deepseek_auth["hif_dliq"] == "dliq"

    qwen_session = paths.qwen_repo / "session"
    assert (qwen_session / "auth_token.txt").read_text(encoding="utf-8") == qwen_token
    tokens = json.loads((qwen_session / "tokens.json").read_text(encoding="utf-8"))
    assert tokens[0]["id"] == "acc_existing"
    assert any(item["token"] == qwen_token for item in tokens)
    baxia = json.loads((qwen_session / "baxia.json").read_text(encoding="utf-8"))
    assert baxia["headers"]["bx-ua"] == "ua"

    env_file = paths.upstream_env_path.read_text(encoding="utf-8")
    assert "ZAI_TOKEN=" in env_file


@pytest.mark.asyncio
async def test_admin_auth_status_redacts_token(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "tokens.json")
    store.upsert("deepseek", {"token": "super-secret-token", "cookie": "a=b"})
    monkeypatch.setenv("FP_AUTH_STORE", str(store.path))

    async def handler(request):
        raise AssertionError(f"unexpected upstream call: {request.url}")

    async with gateway(make_settings(), make_config(), handler) as (_app, client):
        response = await client.get(
            "/admin/auth",
            headers={"X-Admin-Key": "admin-key-0123456789abcdef"},
        )
        assert response.status_code == 200
        assert response.json()["providers"]["deepseek"]["configured"] is True
        assert "super-secret-token" not in response.text


@pytest.mark.asyncio
async def test_login_confirmation_is_bound_to_provider(tmp_path, monkeypatch):
    seen: dict[str, str] = {}

    async def fake_terminal_confirm(provider: str) -> None:
        seen["provider"] = provider

    async def fake_capture(provider, paths, *, headless=False, confirm=None, cdp_url=None):
        assert confirm is not None
        await confirm()
        return {"token": "captured-token"}

    monkeypatch.setattr(auth_cli, "_terminal_confirm", fake_terminal_confirm)
    monkeypatch.setattr(auth_cli, "capture", fake_capture)
    paths = _paths(tmp_path)
    await auth_cli._login_one(
        "deepseek",
        paths,
        TokenStore(paths.store),
        headless=False,
        export_files=False,
        require_confirmation=True,
    )
    assert seen["provider"] == "deepseek"


def test_export_falls_back_to_secrets_when_repo_missing(tmp_path):
    paths = _paths(tmp_path)
    store = TokenStore(paths.store)
    store.upsert("deepseek", {"token": "token", "cookie": "a=b"})
    files = export_all(store, paths)["deepseek"]
    assert files == [str(paths.export_dir / "deepseek" / "deepseek-auth.json")]
    assert Path(files[0]).is_file()
