"""Shared Retry-After parsing for upstream responses."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def parse_retry_after(raw: str | None, *, now: datetime | None = None) -> int | None:
    """Parse Retry-After delta-seconds or HTTP-date; clamp to >= 1.

    ``now`` is only meant for deterministic tests; when supplied it must be
    timezone-aware.
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    try:
        return max(1, int(text))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(1, math.ceil((retry_at - current).total_seconds()))
