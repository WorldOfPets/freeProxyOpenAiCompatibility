# Contributing to FreeProxyAPI

Thanks for taking the time to contribute. This project is a Python 3.12 /
FastAPI proxy that exposes one OpenAI-compatible API over the GLM, Qwen and
DeepSeek web chats. All three providers run natively inside the same process.

## Prerequisites

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/)
- Google Chrome or Chromium **only** for manual Qwen auth
  (`uv run playwright install chromium`). The unit test suite does not need a
  browser and must not launch Playwright.

## Setup

```bash
uv sync
# Optional, only if you plan to run `freeproxy-auth login` for Qwen:
uv run playwright install chromium
```

## Checks before opening a PR

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run pytest -q --cov=freeproxy --cov-report=term-missing --cov-fail-under=60
```

CI runs the same commands.

## Ground rules

- One logical change per PR. Do not mix refactors, formatting churn or
  unrelated fixes with behavior changes.
- Write a failing test first for bug fixes, then the smallest fix that makes
  it pass.
- Update `README.md` and the relevant `docs/*.md` when behavior, environment
  variables, endpoints or limits change.
- Keep the OpenAI contract intact: request/response shapes, SSE framing,
  `tool_calls`, `reasoning_content` and usage fields.
- Do not add dependencies casually; use `uv add` / `uv remove` and commit the
  updated `uv.lock`.

## Secrets

Never commit or log:

- `.env`, `config/providers.local.json`;
- `secrets/`, `tokens.sqlite`, cookies, captured auth files;
- API keys, upstream tokens, prompts or request bodies.

Use dummy values in tests and fixtures. When reporting a bug, redact logs.
For vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a
public issue.

## Tests

- Use `httpx.MockTransport` or fake native transports; tests must never call
  real upstreams or real accounts.
- Cover routing, auth, rate limits, streaming and error paths, not just the
  happy path.
- Do not run Playwright browser tests in the default unit suite (CI may not
  have a browser).

## Project map

```text
src/freeproxy/
├── api/          # chat, models, health, admin routes and dependencies
├── services/     # routing, proxy and streaming
├── providers/    # HTTP adapter, breaker, limits, health
├── native/       # GLM, Qwen, DeepSeek in-process providers
├── auth/         # Playwright capture, token store, native export
├── config.py     # pydantic-settings + providers.json
├── rate_limit.py # sliding window, daily quota, client limiter
└── middleware.py # request id, security headers, access log
tests/            # pytest + mock upstream transports
config/providers.json  # the only provider registry
```

## Streaming and rate-limit invariants

- Any slot, semaphore or upstream response acquired for a stream must be
  released on normal completion, client disconnect and errors.
  `ProxyStreamingResponse` is the reference pattern.
- One client request is counted once by client limits; every alias attempt is
  counted once by provider limits. See [docs/rate-limiting.md](docs/rate-limiting.md).
- Never forward the client's `Authorization` / `X-API-Key` upstream; only the
  whitelisted provider headers are allowed.

## Adding a provider or model

1. Add the provider or model to `config/providers.json` with safe defaults and
   `${ENV_VAR}` placeholders for secrets.
2. Add focused tests for routing, model listing and error mapping using a mock
   transport.
3. Update `README.md` and `docs/providers.md` (plus `docs/api.md` when the
   public surface changes).
4. Keep the OpenAI-compatible request/response and SSE shapes unchanged.

## Pull request checklist

- [ ] Diff is small and scoped to one change.
- [ ] Tests added or updated and passing.
- [ ] `ruff check` and `ruff format --check` pass.
- [ ] `pytest -q` passes (and coverage gate, if affected).
- [ ] No secrets, tokens or unredacted logs in the diff.
- [ ] Streaming and rate-limit cleanup considered.
- [ ] Docs updated when behavior, env vars, endpoints or limits changed.
- [ ] No unrelated formatting or refactors.
