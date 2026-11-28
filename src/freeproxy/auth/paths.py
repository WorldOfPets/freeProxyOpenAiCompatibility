from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from ..config import DEFAULT_AUTH_STORE_PATH, resolve_auth_store_path

_PROVIDERS = ("deepseek", "qwen", "glm")


def _env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = Path(".env")
    if env_path.is_file():
        for key, value in dotenv_values(env_path).items():
            if value is not None:
                values[str(key)] = str(value)
    values.update(os.environ)
    return values


@dataclass(frozen=True)
class AuthPaths:
    """Filesystem locations for auth capture and native upstream exports."""

    store: Path = DEFAULT_AUTH_STORE_PATH
    browser_dir: Path = Path("secrets/browser")
    deepseek_repo: Path = Path("../FreeDeepseekAPI")
    qwen_repo: Path = Path("../FreeQwenApi")
    glm_repo: Path = Path("../GLM-Free-API")
    browser_channel: str = "chrome"
    timeout_seconds: int = 600

    @classmethod
    def from_env(
        cls,
        *,
        store: str | Path | None = None,
        deepseek_repo: str | Path | None = None,
        qwen_repo: str | Path | None = None,
        glm_repo: str | Path | None = None,
        browser_channel: str | None = None,
        timeout_seconds: int | None = None,
    ) -> AuthPaths:
        env = _env()
        return cls(
            store=Path(store) if store else resolve_auth_store_path(env),
            browser_dir=Path(env.get("FP_AUTH_BROWSER_DIR", "secrets/browser")),
            deepseek_repo=Path(deepseek_repo or env.get("FP_DEEPSEEK_REPO", "../FreeDeepseekAPI")),
            qwen_repo=Path(qwen_repo or env.get("FP_QWEN_REPO", "../FreeQwenApi")),
            glm_repo=Path(glm_repo or env.get("FP_GLM_REPO", "../GLM-Free-API")),
            browser_channel=browser_channel or env.get("FP_AUTH_BROWSER_CHANNEL", "chrome"),
            timeout_seconds=timeout_seconds or int(env.get("FP_AUTH_TIMEOUT_SECONDS", "600")),
        )

    @property
    def secrets_dir(self) -> Path:
        return self.store.parent

    @property
    def export_dir(self) -> Path:
        return self.secrets_dir / "exports"

    @property
    def upstream_env_path(self) -> Path:
        return self.secrets_dir / "upstream.env"

    def browser_profile(self, provider: str) -> Path:
        if provider not in _PROVIDERS:
            raise ValueError(f"unknown provider: {provider}")
        return self.browser_dir / provider

    def repo_for(self, provider: str) -> Path:
        return {
            "deepseek": self.deepseek_repo,
            "qwen": self.qwen_repo,
            "glm": self.glm_repo,
        }[provider]

    @staticmethod
    def providers() -> tuple[str, ...]:
        return _PROVIDERS
