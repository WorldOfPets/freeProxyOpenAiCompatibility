# Agent guide for FreeProxyAPI

## What this repository is

A Python 3.12 / FastAPI proxy that exposes one OpenAI-compatible API over the
GLM, Qwen and DeepSeek web chats. All three are native in-process providers:
DeepSeek (PoW/session/SSE), Qwen (Playwright page context for WAF-safe fetch),
and GLM (direct HTTP signature + Aliyun captcha/device tokens). Browser login
and captcha remain interactive user steps; do not automate captcha bypass.

Read first:

- `README.md` — architecture and quick start.
- `docs/architecture.md` — request flow, streaming lifecycle, limits.
- `docs/native-port.md` — migration from bridges to native providers.
- `docs/rate-limiting.md` and `docs/api.md`.
- `config/providers.json` — the only provider registry.

## Commands

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run freeproxy
uv run freeproxy-auth status
uv run python scripts/check_upstreams.py
uv run python scripts/smoke.py --api-key <key>
```

Browser auth (`uv run freeproxy-auth login ...`) writes to `secrets/`; never
log or commit its contents. Do not run Playwright browser tests in the default
unit suite (CI may not have a browser).

Use `uv add` / `uv remove` for dependencies and commit the updated `uv.lock`.

## Non-negotiable rules

1. **Secrets**: never hardcode or log API keys, upstream tokens, cookies, auth
   files, prompts or request bodies. Do not read `.env`,
   `config/providers.local.json`, `deepseek-auth.json`, `session/` or
   `tokens.sqlite` unless the user explicitly asks for a diagnostic and the
   output is redacted.
2. **OpenAI compatibility**: keep request/response shapes, SSE framing,
   `tool_calls`, `reasoning_content` and usage fields intact. Do not rewrite
   content unless the task requires it.
3. **One upstream attempt = one provider request**: client rate limits count a
   request once; provider limits count every alias attempt. Keep this invariant.
4. **Streaming cleanup**: any slot/semaphore/upstream response acquired for a
   stream must be released on normal completion, client disconnect and errors.
   `ProxyStreamingResponse` is the reference pattern.
5. **No blocking I/O in async code**: use `httpx.AsyncClient`, not `requests`;
   do not call `time.sleep` in handlers.
6. **Header boundary**: never forward the client's `Authorization`/`X-API-Key`
   to upstreams. Only forward the whitelisted headers in
   `ProviderHTTP.chat_headers` and the derived DeepSeek session header.
7. **Admin safety**: admin endpoints must remain key-protected or loopback-only.
   Never expose upstream keys in `/admin/*`.
8. **Configuration**: add new provider options to `ProviderConfig` with safe
   defaults and validation. Use `${ENV_VAR}` for secrets in JSON.
9. **Native providers**: keep them behind `httpx.ASGITransport`; open the
   upstream HTTP session/PoW/captcha **before** returning `StreamingResponse` so
   gateway fallback and HTTP status mapping still work. Close every browser,
   response and client on normal completion and disconnect.
10. **Tests**: add focused tests for routing, limits, auth, streaming and error
    paths. Use `httpx.MockTransport` or fake native transports; tests must not
    call real upstreams.
11. **Docs**: update `README.md` and the relevant `docs/*.md` when behavior,
    endpoints, env vars or limits change.

## Code style

- Python 3.12, full type hints, `from __future__ import annotations`.
- Async FastAPI handlers and dependencies; Pydantic v2 models.
- Keep error responses OpenAI-shaped via `ProxyError`.
- Prefer small service functions over logic in routes.
- Ruff is the formatter/linter; do not reformat unrelated files.

## ECC workflow

Project skills live in `.opencode/skills/` and review subagents in
`.opencode/agents/`. Use them when relevant:

- `fastapi-patterns`, `python-patterns`, `api-design`, `error-handling`;
- `security-review` for auth/headers/SSRF/secrets changes;
- `verification-loop` before finishing a feature;
- `fastapi-reviewer`, `python-reviewer`, `security-reviewer` as read-only
  subagents.

Ask the user before installing the full ECC OpenCode plugin or changing global
OpenCode configuration.

## Definition of done

- `uv run ruff check .` passes.
- `uv run pytest -q` passes.
- New behavior is documented.
- No secrets or real account data are added to the repo.
- Streaming and rate-limit behavior were considered, not just the happy path.
