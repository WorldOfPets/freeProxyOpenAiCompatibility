from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from freeproxy.retry import parse_retry_after

FIXED_NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_retry_after_empty_values(raw: str | None):
    assert parse_retry_after(raw, now=FIXED_NOW) is None


@pytest.mark.parametrize(("raw", "expected"), [("7", 7), (" 12 ", 12), ("0", 1), ("-5", 1)])
def test_parse_retry_after_delta_seconds(raw: str, expected: int):
    assert parse_retry_after(raw, now=FIXED_NOW) == expected


def test_parse_retry_after_future_http_date():
    retry_at = FIXED_NOW + timedelta(seconds=30)
    raw = format_datetime(retry_at, usegmt=True)
    assert parse_retry_after(raw, now=FIXED_NOW) == 30


def test_parse_retry_after_past_http_date_clamps_to_one():
    retry_at = FIXED_NOW - timedelta(seconds=30)
    raw = format_datetime(retry_at, usegmt=True)
    assert parse_retry_after(raw, now=FIXED_NOW) == 1


def test_parse_retry_after_naive_http_date_is_treated_as_utc():
    # HTTP-dates without a zone parse to a naive datetime.
    raw = "Fri, 27 Nov 2026 12:00:30"
    assert parse_retry_after(raw, now=FIXED_NOW) == 30


def test_parse_retry_after_invalid_http_date():
    assert parse_retry_after("Wed, 99 Zzz 2099 99:99:99 GMT", now=FIXED_NOW) is None
    assert parse_retry_after("soon", now=FIXED_NOW) is None


def test_parse_retry_after_non_utc_aware_http_date():
    retry_at = datetime(2026, 11, 27, 15, 0, 30, tzinfo=timezone(timedelta(hours=3)))
    raw = format_datetime(retry_at, usegmt=False)
    assert parse_retry_after(raw, now=FIXED_NOW) == 30


def test_parse_retry_after_accepts_aware_now_with_offset():
    shifted_now = datetime(2026, 11, 27, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    retry_at = FIXED_NOW + timedelta(seconds=30)
    raw = format_datetime(retry_at, usegmt=True)
    assert parse_retry_after(raw, now=shifted_now) == 30
