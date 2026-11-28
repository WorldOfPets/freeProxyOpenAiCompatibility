# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security vulnerabilities.

Use GitHub private vulnerability reporting:

<https://github.com/WorldOfPets/freeProxyOpenAiCompatibility/security/advisories/new>

Include a description, affected version/commit, reproduction steps and impact.
Never include real API keys, upstream tokens, cookies, `.env` contents or
captured auth files. Redact logs before attaching them.

## If a token leaked

1. Revoke the leaked credential and log in again:
   `uv run freeproxy-auth login <provider>` (or `login all`), then confirm with
   `uv run freeproxy-auth status`.
2. Rotate client keys in `FP_API_KEYS` and restart the proxy.
3. Check access logs and `/admin/*` state for unexpected usage.
4. If the leak came from this repository, report it through the private
   advisory link above.

## Supported versions

Security fixes are applied to the latest `master` and the current `0.1.x`
release line. Older commits are not supported.
