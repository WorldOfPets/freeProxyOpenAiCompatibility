from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigurationError

DEFAULT_AUTH_STORE_PATH = Path("secrets/tokens.json")
# The auth CLI documented `FP_AUTH_STORE` while `Settings.auth_store_path`
# auto-mapped to `FP_AUTH_STORE_PATH`, so the gateway and `freeproxy-auth`
# could read different stores. Both names are accepted everywhere; the
# canonical, more specific `FP_AUTH_STORE_PATH` wins when both are set.
AUTH_STORE_ENV_NAMES: tuple[str, ...] = ("FP_AUTH_STORE_PATH", "FP_AUTH_STORE")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_RESERVED_HEADERS = {
    "authorization",
    "content-length",
    "content-type",
    "host",
    "x-api-key",
    "transfer-encoding",
    "connection",
}
_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:/@+#-]{1,200}$")
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}


def resolve_auth_store_path(
    env: Mapping[str, str] | None = None,
    default: Path = DEFAULT_AUTH_STORE_PATH,
) -> Path:
    """Resolve the token store path, preferring the canonical env name."""
    source = os.environ if env is None else env
    for name in AUTH_STORE_ENV_NAMES:
        value = source.get(name)
        if value:
            return Path(value)
    return default


def is_safe_model_id(value: str) -> bool:
    return bool(MODEL_ID_PATTERN.fullmatch(value))


class ProviderConfig(BaseModel):
    """Configuration of one upstream OpenAI-compatible proxy."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    api_key: SecretStr = SecretStr("")
    auth_mode: Literal["bearer", "x-api-key", "none"] = "bearer"
    prefix: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    enabled: bool = True
    models: list[str] = Field(default_factory=list)
    default_model: str = Field(min_length=1, max_length=200)
    models_path: str = "/models"
    chat_path: str = "/chat/completions"
    health_url: str | None = None
    max_rpm: int = Field(default=20, ge=1, le=600)
    max_requests_per_day: int = Field(default=0, ge=0, le=1_000_000)
    max_concurrency: int = Field(default=2, ge=1, le=64)
    queue_timeout_seconds: float = Field(default=8.0, gt=0, le=120)
    timeout_seconds: float = Field(default=300.0, gt=0, le=1800)
    max_retries: int = Field(default=0, ge=0, le=3)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    session_header: bool = False
    supports_tools: bool = True
    inject_max_tokens: bool = True
    auth_required: bool = True
    native: Literal["deepseek", "qwen", "glm"] | None = None

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("base_url must not contain credentials")
        if parts.query or parts.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        return value

    @field_validator("health_url")
    @classmethod
    def _validate_health_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("health_url must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("health_url must not contain credentials")
        if parts.query or parts.fragment:
            raise ValueError("health_url must not contain a query or fragment")
        return value

    @field_validator("models_path", "chat_path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/") or "://" in value:
            raise ValueError("upstream paths must start with '/'")
        return value

    @field_validator("default_model")
    @classmethod
    def _validate_default_model(cls, value: str) -> str:
        value = value.strip()
        if not is_safe_model_id(value):
            raise ValueError("default_model must be a safe ASCII model ID")
        return value

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for model in value:
            model = model.strip()
            if not is_safe_model_id(model):
                raise ValueError("model IDs must be safe ASCII identifiers, 1..200 characters")
            if model not in seen:
                cleaned.append(model)
                seen.add(model)
        return cleaned

    @field_validator("extra_headers")
    @classmethod
    def _validate_extra_headers(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for name, header_value in value.items():
            normalized = name.strip().lower()
            if not _HEADER_NAME_PATTERN.fullmatch(name):
                raise ValueError(f"header '{name}' has an invalid name")
            if normalized in _RESERVED_HEADERS:
                raise ValueError(f"header '{name}' is reserved")
            try:
                header_value.encode("latin-1")
            except UnicodeEncodeError:
                raise ValueError(f"header '{name}' must be latin-1 encodable") from None
            if any(ch in header_value for ch in "\r\n"):
                raise ValueError(f"header '{name}' contains invalid characters")
            cleaned[name] = header_value
        return cleaned

    @model_validator(mode="after")
    def _validate_secrets(self) -> ProviderConfig:
        key = self.api_key.get_secret_value()
        if "${" in key:
            raise ValueError(f"provider '{self.id}': unresolved environment variable in api_key")
        if any(ch in key for ch in "\r\n"):
            raise ValueError(f"provider '{self.id}': api_key contains a newline")
        if self.enabled and self.auth_mode != "none" and self.auth_required and not key.strip():
            raise ValueError(f"provider '{self.id}': auth_required is true but api_key is empty")
        if self.health_url:
            base = urlsplit(self.base_url)
            health = urlsplit(self.health_url)
            base_port = base.port or (443 if base.scheme == "https" else 80)
            health_port = health.port or (443 if health.scheme == "https" else 80)
            if (base.scheme, base.hostname, base_port) != (
                health.scheme,
                health.hostname,
                health_port,
            ):
                raise ValueError(
                    f"provider '{self.id}': health_url must share origin with base_url "
                    "because upstream auth headers are sent to it"
                )
        return self

    @property
    def route_prefix(self) -> str:
        return self.prefix or self.id

    @property
    def models_url(self) -> str:
        return f"{self.base_url}{self.models_path}"

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}{self.chat_path}"

    @property
    def health_check_url(self) -> str:
        return self.health_url or f"{self.base_url}/health"


class ProxyConfig(BaseModel):
    """Validated provider registry and routing aliases."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    providers: list[ProviderConfig]
    aliases: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_registry(self) -> ProxyConfig:
        if not self.providers:
            raise ValueError("at least one provider is required")

        ids = [provider.id for provider in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("provider IDs must be unique")
        prefixes = [provider.route_prefix for provider in self.providers]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("provider prefixes must be unique")

        namespace_owner: dict[str, str] = {}
        for provider in self.providers:
            for namespace in {provider.id, provider.route_prefix}:
                owner = namespace_owner.get(namespace)
                if owner is not None and owner != provider.id:
                    raise ValueError(
                        f"provider namespace '{namespace}' is used by both "
                        f"'{owner}' and '{provider.id}'"
                    )
                namespace_owner[namespace] = provider.id

        provider_map = {provider.id: provider for provider in self.providers}
        for alias, targets in self.aliases.items():
            if alias in namespace_owner:
                raise ValueError(f"alias '{alias}' shadows a provider namespace")
            if not _ALIAS_PATTERN.match(alias):
                raise ValueError(f"invalid alias name '{alias}'")
            if not targets:
                raise ValueError(f"alias '{alias}' has no targets")
            for target in targets:
                provider_id, separator, model = target.partition("/")
                if provider_id not in provider_map:
                    raise ValueError(f"alias '{alias}' references unknown provider '{provider_id}'")
                if separator and not model:
                    raise ValueError(f"alias '{alias}' has an empty model")
                if not separator:
                    model = provider_map[provider_id].default_model
                if not is_safe_model_id(model):
                    raise ValueError(f"alias '{alias}' has an invalid model")
        return self

    @property
    def provider_map(self) -> dict[str, ProviderConfig]:
        return {provider.id: provider for provider in self.providers}


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_prefix="FP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    app_name: str = "FreeProxyAPI"
    version: str = "0.1.0"
    environment: str = "local"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    server_limit_concurrency: int = Field(default=128, ge=10, le=10_000)

    api_keys: SecretStr = SecretStr("")
    allow_anonymous: bool = False
    admin_api_key: SecretStr = SecretStr("")

    providers_file: Path = Path("config/providers.json")
    auth_store_path: Path = Field(
        default=DEFAULT_AUTH_STORE_PATH,
        validation_alias=AliasChoices(*AUTH_STORE_ENV_NAMES),
    )
    qwen_headless: bool = True
    aliyun_captcha_access_key: SecretStr = SecretStr("")
    aliyun_captcha_secret_key: SecretStr = SecretStr("")
    # Optional harvested GLM device-token database. Empty/unset means "fall
    # back to the built-in candidate paths" in the captcha solver.
    glm_tokens_db: Path | None = None
    default_alias: str = "auto"
    strict_models: bool = False

    client_rpm: int = Field(default=30, ge=1, le=6000)
    client_daily_requests: int = Field(default=1000, ge=0, le=1_000_000)
    client_max_concurrency: int = Field(default=2, ge=1, le=64)
    global_rpm: int = Field(default=60, ge=1, le=10_000)
    global_max_concurrency: int = Field(default=8, ge=1, le=256)
    global_queue_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    max_body_bytes: int = Field(default=8_000_000, ge=1024, le=64_000_000)
    body_read_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_upstream_bytes: int = Field(default=16_000_000, ge=1024, le=128_000_000)
    max_output_tokens: int = Field(default=8192, ge=1, le=131_072)
    default_max_tokens: int = Field(default=4096, ge=1, le=131_072)

    request_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    model_discovery_ttl_seconds: int = Field(default=300, ge=10, le=86_400)
    model_discovery_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    shutdown_timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    cors_origins: str = ""
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in _LOG_LEVELS:
            raise ValueError(f"log level must be one of {sorted(_LOG_LEVELS)}")
        return value

    @field_validator("glm_tokens_db", mode="before")
    @classmethod
    def _blank_path_is_none(cls, value: object) -> object:
        # Pydantic turns "" into a relative Path object (`.`) rather than
        # failing, which would silently point the solver at the CWD. Treat
        # unset/blank values as "not configured" instead.
        if isinstance(value, str):
            stripped = value.strip()
            return Path(stripped) if stripped else None
        return value

    @field_validator("default_alias")
    @classmethod
    def _validate_default_alias(cls, value: str) -> str:
        value = value.strip()
        if not _ALIAS_PATTERN.fullmatch(value):
            raise ValueError("default_alias must match ^[a-z][a-z0-9_-]{0,31}$")
        return value

    @model_validator(mode="after")
    def _validate_limits(self) -> Settings:
        if self.default_max_tokens > self.max_output_tokens:
            raise ValueError("FP_DEFAULT_MAX_TOKENS must be <= FP_MAX_OUTPUT_TOKENS")
        if not self.api_key_list and not self.allow_anonymous:
            raise ValueError(
                "Client authentication is not configured: FP_API_KEYS is empty and "
                "FP_ALLOW_ANONYMOUS is false. This is the key OpenCode/curl sends, "
                "not a provider token. Generate one with "
                '`uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`, '
                "set FP_API_KEYS=<key> in .env, or set FP_ALLOW_ANONYMOUS=true for a "
                "loopback-only test instance. Provider tokens are configured separately "
                "with `uv run freeproxy-auth login ...`."
            )
        short_keys = [key for key in self.api_key_list if len(key) < 16]
        if short_keys:
            raise ValueError("client API keys must be at least 16 characters")
        if self.admin_key and len(self.admin_key) < 16:
            raise ValueError("FP_ADMIN_API_KEY must be at least 16 characters")
        if self.admin_key and self.admin_key in self.api_key_list:
            raise ValueError("FP_ADMIN_API_KEY must not equal a client API key")
        return self

    @property
    def api_key_list(self) -> list[str]:
        raw = self.api_keys.get_secret_value()
        keys: list[str] = []
        for item in raw.split(","):
            item = item.strip()
            if item and item not in keys:
                keys.append(item)
        return keys

    @property
    def admin_key(self) -> str:
        return self.admin_api_key.get_secret_value().strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


def _load_env(env_file: Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    path = env_file or Path(".env")
    if path.is_file():
        try:
            for key, value in dotenv_values(path).items():
                if value is not None:
                    values[str(key)] = str(value)
        except OSError as exc:
            raise ConfigurationError(f"Cannot read {path}: {exc.strerror}") from None
    # Real environment variables always win over .env, matching pydantic-settings.
    values.update(os.environ)
    return values


def _expand_env(value: str, env: Mapping[str, str], missing: set[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        resolved = env.get(name)
        if resolved:
            return resolved
        if default is not None:
            return default
        missing.add(name)
        return ""

    return _ENV_PATTERN.sub(replace, value)


def _expand_tree(value: Any, env: Mapping[str, str], missing: set[str]) -> Any:
    if isinstance(value, dict):
        return {key: _expand_tree(item, env, missing) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_tree(item, env, missing) for item in value]
    if isinstance(value, str):
        return _expand_env(value, env, missing)
    return value


def load_proxy_config(
    path: Path | str,
    *,
    env: Mapping[str, str] | None = None,
) -> ProxyConfig:
    config_path = Path(path)
    env_values = dict(env) if env is not None else _load_env()
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigurationError(
            f"Providers config not found: {config_path}. "
            "Copy config/providers.json or set FP_PROVIDERS_FILE."
        ) from None
    except OSError as exc:
        raise ConfigurationError(
            f"Cannot read providers config {config_path}: {exc.strerror}"
        ) from None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"Invalid JSON in {config_path} at line {exc.lineno}, column {exc.colno}"
        ) from None

    missing: set[str] = set()
    data = _expand_tree(parsed, env_values, missing)
    if missing:
        names = ", ".join(sorted(missing))
        raise ConfigurationError(
            f"Missing environment variables referenced by providers config: {names}. "
            "Set them in the environment or in .env."
        )

    try:
        return ProxyConfig.model_validate(data)
    except ValidationError as exc:
        details = []
        for error in exc.errors()[:5]:
            location = ".".join(str(part) for part in error["loc"])
            details.append(f"{location}: {error['msg']}")
        raise ConfigurationError(
            f"Invalid providers config {config_path}: " + "; ".join(details)
        ) from None
