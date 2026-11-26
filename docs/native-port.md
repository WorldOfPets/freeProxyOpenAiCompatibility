# Native Python port (one process, no upstream services)

This document tracks the migration from the current gateway architecture to a
single FastAPI process that speaks directly to DeepSeek, Qwen and GLM.

## Goal

One `uv run freeproxy` process, one port, no separate `:3001`, `:3264` or
`:9655` services. The public OpenAI-compatible API, auth onboarding, rate
limits, aliases and OpenCode config stay the same.

## What is already reusable

- `freeproxy-auth`: captures browser tokens/cookies into `secrets/tokens.json`.
- Client API keys, rate limits, aliases, model routing and OpenCode config.
- SSE/JSON response plumbing and admin/status endpoints.
- `wasmtime` for the DeepSeek PoW WASM solver.

## Stage 1 — DeepSeek native

Port `FreeDeepseekAPI` core:

- auth headers from `secrets/tokens.json` (`token`, `cookie`, `hif_*`, `wasm_url`);
- PoW WASM solver (`wasm_solve` via `wasmtime`);
- `/api/v0/chat_session/create`;
- `/api/v0/chat/completion` SSE parser (fragments, reasoning, finish reason);
- prompt formatter and JSON tool-call shim;
- `deepseek-chat`, `deepseek-reasoner`, `deepseek-chat-search`;
- OpenAI JSON and SSE output.

Remove the external DeepSeek base URL after this stage.

## Stage 2 — Qwen native

Port the Qwen request path into Python:

- persistent Playwright profile from `freeproxy-auth` (token + cookies + Baxia);
- `POST /api/v2/chats/new`;
- `POST /api/v2/chat/completions?chat_id=...` SSE;
- token refresh from the page when it expires;
- tool-call and reasoning passthrough;
- OpenAI JSON/SSE output.

No Node.js process. Browser is internal to the Python service and only used for
Qwen/GLM sessions that need the web WAF context.

## Stage 3 — GLM native

Hardest stage:

- port token/device-session handling from the Go token collector and Python
  reference scripts (`captcha_param_gen.py`, `device_token_gen.py`,
  `signature_gen.py`);
- Aliyun captcha param generation and request signature;
- `/api/v2/chat/completions` SSE;
- agent mode/tool-call prompt shim;
- session pool and WAF cooldown.

No Go process at runtime. Token collection may remain a one-time browser step,
but the collected data is consumed by the Python service.

## Definition of done

- `config/providers.json` has only native providers; no `base_url` to external
  proxies.
- Stopping the three upstream projects does not affect `uv run freeproxy`.
- DeepSeek/Qwen/GLM chat and streaming work through `/v1/chat/completions`.
- Tool calls work from OpenCode for at least `coding`/`auto` aliases.
- Tests mock upstream protocols; real-account checks are documented separately.
- README/docs no longer instruct users to run three proxy servers.

## Current status

- [x] Browser auth capture for all three providers.
- [x] `wasmtime` dependency for DeepSeek PoW.
- [x] DeepSeek native adapter (auth store, PoW, session create, SSE parser,
      tool-call shim, OpenAI JSON/SSE, in-process ASGI provider).
- [x] Qwen native adapter (in-process Playwright page context, WAF-safe fetch,
      chat creation, SSE parser, tool-call shim).
- [x] GLM native adapter (direct HTTP signature, Aliyun captcha, device-token
      pool fallback, SSE parser, tool-call shim).
- [x] Remove upstream gateway configuration and docs.

### Stage 2 notes (Qwen)

- `src/freeproxy/native/qwen.py` keeps one Playwright page for the process and
  serializes requests (`max_concurrency: 1`).
- The page loads `chat.qwen.ai`, gets the short-lived `localStorage.token`, and
  performs `/api/v2/chats/new` + `/api/v2/chat/completions` via `page.evaluate`.
  This is required because Qwen's Aliyun Baxia WAF rejects plain HTTP and
  Node-side streaming. Captured `baxia_headers` and cookies are injected.
- The response is streamed back through an exposed page function so Python can
  emit OpenAI SSE deltas in real time.

### Stage 3 notes (GLM)

- `src/freeproxy/native/glm.py` implements the HMAC-SHA256 request signature,
  direct `/api/v2/chat/completions` calls and the Z.AI edit-based SSE parser.
- `src/freeproxy/native/glm_captcha/` is an MIT-attributed Python port of the
  reference device-token/captcha scripts. It can mint a device token from a
  fresh `InitCaptchaV3` session or consume a `tokens.sqlite` harvested by the
  legacy collector (`FP_GLM_TOKENS_DB`).
- Single-use chat IDs are generated per request; there is no pre-warmed session
  pool. WAF/HTTP failures feed the existing provider circuit breaker instead of
  a dedicated GLM cooldown loop.
- Aliyun's risk engine can still answer `F001` for synthetic/foreign device
  sessions. That is a risk decision, not a crypto error; using a token pool
  harvested from the same account/IP is the reliable path.

### Known gaps

- Vision image upload is not ported for native Qwen/GLM; image parts are folded
  to `[image]` text in the prompt.
- Qwen uses the single account in `secrets/tokens.json`; the multi-account
  round-robin pool from FreeQwenApi is not ported.
- GLM has no agent ultra/LoRA tool parser; the shared JSON tool shim is used.

Tests mock all provider protocols with `httpx.MockTransport` / fake transports;
no real account is used in CI.
