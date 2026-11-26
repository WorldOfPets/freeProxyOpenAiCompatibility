# ECC-derived skills and agents

This directory contains a curated, adapted subset of
[affaan-m/ECC](https://github.com/affaan-m/ecc), pinned at commit
`4adeee6732a12d1540f49a4d50a38ce8c97f9a53` (MIT).

ECC is an agent harness operating system with planning, review, security,
testing and verification workflows. This repository uses the parts that fit a
Python/FastAPI service without vendoring ECC's Node/TypeScript runtime.

## Skills

Copied from ECC `skills/` and auto-discovered by OpenCode:

- `fastapi-patterns` — app structure, dependencies, async handlers, Pydantic v2.
- `python-patterns` — typing, exceptions, context managers, idioms.
- `api-design` — status codes, errors, rate-limit headers, versioning.
- `security-review` — secrets, auth, input validation, OWASP checks.
- `error-handling` — typed errors, retries, circuit breakers.
- `verification-loop` — build/lint/test/security/diff verification.

## Agents

Adapted from ECC `agents/`:

- `fastapi-reviewer` — async correctness, DI, OpenAPI, production readiness.
- `python-reviewer` — Pythonic code, typing, security, performance.
- `security-reviewer` — auth, headers, secrets, SSRF, rate-limit bypasses.

Changes from the ECC originals:

- removed ECC's `model: sonnet` (OpenCode inherits the parent model);
- removed the V1 `tools:` field and added OpenCode V2 `mode`/`permissions`;
- edit access denied; shell limited to `uv run pytest*` and `uv run ruff*`;
- added a short project-context section.

Use them from OpenCode, for example:

```text
Use the fastapi-reviewer subagent to review the current changes.
Use the security-reviewer subagent to audit src/freeproxy.
```

## License

ECC is MIT-licensed. The original license is reproduced in
`ECC-LICENSE`. Upstream skills and agent prompts remain copyright their
authors; modifications are limited to the frontmatter and project notes
described above.

## Full ECC plugin (optional)

The full ECC OpenCode plugin adds hooks, custom tools and session automation.
It is **not** bundled here because it requires a Node/TypeScript runtime and is
not needed to run this Python service. If you want it, install `ecc-universal`
from its official npm package and follow ECC's `.opencode/README.md` for the
current OpenCode plugin syntax. Do not stack a manual ECC install on top of the
plugin. Official sources: <https://github.com/affaan-m/ECC>,
<https://ecc.tools>.
