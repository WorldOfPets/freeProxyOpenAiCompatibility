## Summary

<!-- One or two sentences: what does this PR change and why? -->

## Motivation

<!-- Link the issue or describe the problem. -->

## What changed

<!-- Bullet list of the concrete changes. Keep it scoped to one logical change. -->

## Tests run

<!-- Paste the commands and their result. -->

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

## Docs updated

<!-- Which README/docs files changed, or "not needed" with a reason. -->

## Risk / rollback

<!-- Behavior, streaming, rate-limit or compatibility risk. How to revert? -->

## Checklist

- [ ] Tests added or updated for the change
- [ ] `uv run ruff check .` passes
- [ ] `uv run ruff format --check .` passes
- [ ] `uv run pytest -q` passes
- [ ] No secrets, tokens, cookies or unredacted logs in the diff
- [ ] Streaming cleanup (slots, semaphores, upstream responses) considered
- [ ] Rate-limit invariants considered (client counted once, provider per attempt)
- [ ] Docs updated when behavior, env vars, endpoints or limits changed
- [ ] No unrelated formatting or refactors
