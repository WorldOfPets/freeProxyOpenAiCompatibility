# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/tmp \
    FP_HOST=0.0.0.0 \
    FP_PORT=8080 \
    FP_QWEN_HEADLESS=true

WORKDIR /app
RUN groupadd --system app \
    && useradd --system --gid app --home /app app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY config /app/config
COPY README.md LICENSE /app/
# Qwen native needs a real browser context for its WAF-protected endpoints.
RUN /app/.venv/bin/playwright install --with-deps chromium \
    && chown -R app:app /app /ms-playwright

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

CMD ["freeproxy"]
