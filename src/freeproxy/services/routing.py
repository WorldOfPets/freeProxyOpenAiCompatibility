from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import ProxyConfig
from ..errors import InvalidRequest, ModelNotFound, ProviderUnavailable
from ..providers.base import ProviderRuntime


@dataclass(frozen=True)
class ModelTarget:
    provider_id: str
    model: str
    requested_model: str
    alias: str | None = None

    @property
    def route_label(self) -> str:
        return f"{self.provider_id}/{self.model}"


class ModelRouter:
    """Resolves aliases, namespaced IDs and bare model IDs to providers."""

    def __init__(
        self,
        config: ProxyConfig,
        runtimes: dict[str, ProviderRuntime],
        *,
        default_alias: str = "auto",
        strict_models: bool = False,
    ) -> None:
        self.config = config
        self.runtimes = runtimes
        self.default_alias = default_alias
        self.strict_models = strict_models
        self._prefixes: dict[str, str] = {}
        for provider in config.providers:
            self._prefixes[provider.id] = provider.id
            self._prefixes[provider.route_prefix] = provider.id

    def _enabled_runtimes(self) -> list[ProviderRuntime]:
        return [runtime for runtime in self.runtimes.values() if runtime.config.enabled]

    def _target(
        self, provider_id: str, model: str | None, requested: str, alias: str | None
    ) -> ModelTarget:
        runtime = self.runtimes.get(provider_id)
        if runtime is None or not runtime.config.enabled:
            raise ModelNotFound(requested)
        return ModelTarget(
            provider_id=provider_id,
            model=model or runtime.config.default_model,
            requested_model=requested,
            alias=alias,
        )

    def _alias_targets(self, alias: str, requested: str) -> list[ModelTarget]:
        targets: list[ModelTarget] = []
        for raw_target in self.config.aliases.get(alias, []):
            provider_id, separator, model = raw_target.partition("/")
            runtime = self.runtimes.get(provider_id)
            if runtime is None or not runtime.config.enabled:
                continue
            targets.append(
                ModelTarget(
                    provider_id=provider_id,
                    model=model if separator and model else runtime.config.default_model,
                    requested_model=requested,
                    alias=alias,
                )
            )
        return targets

    def _default_targets(self, requested: str) -> list[ModelTarget]:
        if self.default_alias in self.config.aliases:
            targets = self._alias_targets(self.default_alias, requested)
            if targets:
                return targets
        enabled = self._enabled_runtimes()
        if not enabled:
            raise ProviderUnavailable("No providers are enabled", provider="gateway")
        runtime = enabled[0]
        return [
            ModelTarget(
                provider_id=runtime.id,
                model=runtime.config.default_model,
                requested_model=requested,
            )
        ]

    def resolve(self, model: str | None, forced_provider: str | None = None) -> list[ModelTarget]:
        requested = (model or "").strip()
        forced_id: str | None = None
        if forced_provider:
            forced_provider = forced_provider.strip()
            if len(forced_provider) > 64:
                raise InvalidRequest("X-FreeProxy-Provider value is too long")
            forced_id = self._prefixes.get(forced_provider)
            if forced_id is None:
                raise InvalidRequest(f"Unknown provider header value '{forced_provider}'")
            runtime = self.runtimes.get(forced_id)
            if runtime is None or not runtime.config.enabled:
                raise ModelNotFound(forced_provider)

        if not requested:
            targets = self._default_targets(self.default_alias)
            return self._filter_forced(targets, forced_id, requested)

        if requested in self.config.aliases:
            targets = self._alias_targets(requested, requested)
            if not targets:
                raise ModelNotFound(requested)
            return self._filter_forced(targets, forced_id, requested)

        if "/" in requested:
            namespace, _, inner = requested.partition("/")
            provider_id = self._prefixes.get(namespace)
            if provider_id is None:
                raise ModelNotFound(requested)
            if forced_id and provider_id != forced_id:
                raise InvalidRequest(
                    "X-FreeProxy-Provider conflicts with the provider in the model ID"
                )
            return [self._target(provider_id, inner or None, requested, None)]

        if forced_id:
            return [self._target(forced_id, requested, requested, None)]

        matches = [
            runtime for runtime in self._enabled_runtimes() if requested in runtime.known_models
        ]
        if matches:
            runtime = matches[0]
            return [self._target(runtime.id, requested, requested, None)]

        if self.strict_models:
            raise ModelNotFound(requested)

        enabled = self._enabled_runtimes()
        if not enabled:
            raise ProviderUnavailable("No providers are enabled", provider="gateway")
        runtime = enabled[0]
        return [self._target(runtime.id, requested, requested, None)]

    @staticmethod
    def _filter_forced(
        targets: list[ModelTarget],
        forced_id: str | None,
        requested: str,
    ) -> list[ModelTarget]:
        if not forced_id:
            return targets
        filtered = [target for target in targets if target.provider_id == forced_id]
        if not filtered:
            raise ModelNotFound(requested)
        return filtered

    def available_models(self) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []

        for alias in sorted(self.config.aliases):
            targets = self._alias_targets(alias, alias)
            if not targets:
                continue
            models.append(
                {
                    "id": alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": "freeproxy",
                    "provider": "auto",
                    "routing": [target.route_label for target in targets],
                }
            )

        for runtime in sorted(self._enabled_runtimes(), key=lambda item: item.id):
            for model in runtime.advertised_models:
                models.append(
                    {
                        "id": f"{runtime.config.route_prefix}/{model}",
                        "object": "model",
                        "created": 0,
                        "owned_by": runtime.config.id,
                        "provider": runtime.config.id,
                        "root": model,
                    }
                )
        return models
