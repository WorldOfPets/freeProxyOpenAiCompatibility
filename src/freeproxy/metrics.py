from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ProviderMetrics:
    attempts: int = 0
    successes: int = 0
    errors: int = 0
    client_rate_limited: int = 0
    provider_rate_limited: int = 0
    stream_requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        attempts = data["attempts"]
        data["average_latency_ms"] = (
            round(data["total_latency_ms"] / attempts, 2) if attempts else 0.0
        )
        return data


@dataclass
class Metrics:
    started_at: float
    providers: dict[str, ProviderMetrics] = field(default_factory=dict)
    gateway_requests: int = 0
    gateway_errors: int = 0
    client_rate_limited: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def for_provider(self, provider_id: str) -> ProviderMetrics:
        return self.providers.setdefault(provider_id, ProviderMetrics())

    def record_attempt(self, provider_id: str, *, stream: bool) -> None:
        metrics = self.for_provider(provider_id)
        metrics.attempts += 1
        if stream:
            metrics.stream_requests += 1

    def record_success(self, provider_id: str, latency_ms: float) -> None:
        metrics = self.for_provider(provider_id)
        metrics.successes += 1
        metrics.total_latency_ms += max(0.0, latency_ms)

    def record_error(self, provider_id: str, *, rate_limited: bool = False) -> None:
        metrics = self.for_provider(provider_id)
        metrics.errors += 1
        if rate_limited:
            metrics.provider_rate_limited += 1

    def record_usage(
        self,
        provider_id: str,
        *,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        metrics = self.for_provider(provider_id)
        if prompt_tokens:
            metrics.prompt_tokens += prompt_tokens
            self.prompt_tokens += prompt_tokens
        if completion_tokens:
            metrics.completion_tokens += completion_tokens
            self.completion_tokens += completion_tokens

    def record_client_rate_limit(self) -> None:
        self.client_rate_limited += 1

    def record_provider_rate_limit(self, provider_id: str) -> None:
        self.for_provider(provider_id).provider_rate_limited += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "gateway_requests": self.gateway_requests,
            "gateway_errors": self.gateway_errors,
            "client_rate_limited": self.client_rate_limited,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "providers": {
                provider_id: metrics.as_dict()
                for provider_id, metrics in sorted(self.providers.items())
            },
        }
