# Retry on Transient Errors — Design Spec

**Date:** 2026-03-14
**Status:** Approved
**Scope:** Automatic single retry for transient subprocess/API errors in `_run_cli()`, covering all agents (Claude, Gemini, Qwen, OpenRouter).

---

## Problem

When a CLI agent subprocess fails due to a transient condition (brief network blip, temporary 5xx from upstream API, subprocess crash with empty output), the bot immediately surfaces the error to the user. The user must manually hit `/retry`. A single automatic retry with a short backoff would silently recover from these transient failures without any user intervention.

---

## Goals

- Automatically retry once on transient errors, transparently to the user.
- Never retry on non-transient errors (auth failures, rate limits, timeouts).
- Single implementation point covering all agents — no per-agent duplication.
- Degrade gracefully: if the retry also fails, the user sees the error as before.

---

## Out of Scope

- Multiple retries or exponential backoff.
- Retrying timeouts (`timed_out=True`).
- Changing the existing manual `/retry` command or the Gemini model fallback logic.
- Per-agent retry configuration.

---

## Architecture

### Files Changed

| File | Change |
|---|---|
| `agents.py` | Add `_is_transient_error()` helper; add retry block in `_run_cli()` |
| `tests/test_retry.py` | New test file: unit tests for `_is_transient_error` and retry behavior in `_run_cli` |

No new modules. No config changes.

---

## `_is_transient_error(stdout, stderr, rc, timed_out) -> bool`

Pure function. Returns `True` if the error is worth retrying automatically.

### Retryable conditions (any one sufficient)

| Condition | Rationale |
|---|---|
| `rc != 0` AND not `timed_out` AND output contains a 5xx pattern | Upstream API returned a server error |
| `rc != 0` AND not `timed_out` AND `stdout` is empty | Subprocess crashed before producing output |

### 5xx patterns checked (case-insensitive, in `stdout + stderr`)

`"500"`, `"502"`, `"503"`, `"504"`, `"overloaded"`, `"temporarily unavailable"`, `"service unavailable"`, `"internal server error"`

### Not retryable

| Condition | Reason |
|---|---|
| `timed_out=True` | Already waited up to 10 minutes; retry wastes another full timeout |
| `rc == 0` | Success — nothing to retry |
| `rc != 0` with `"403"` or `"forbidden"` in output | Auth/permission problem, not transient |
| `rc != 0` with rate-limit patterns | Already handled by existing `claude_rate_set` path |

---

## Retry Block in `_run_cli()`

Inserted immediately after the first `_run_subprocess` call, before any result processing:

```python
if _is_transient_error(stdout, stderr, rc, timed_out):
    log_warn(f"{agent_name}: transient error (rc={rc}), retrying in 2s…")
    time.sleep(2)
    stdout, stderr, rc, timed_out = _run_subprocess(cmd, timeout_secs, WORK_DIR, env)
```

- One retry only.
- If the retry also fails, execution continues into the normal error-handling path — the user sees the error as before.
- The log line makes the retry visible in `/tmp/tg_agent.log` for debugging.

---

## Tests (`tests/test_retry.py`)

### `_is_transient_error` unit tests

| Test | Input | Expected |
|---|---|---|
| 503 in stdout | `rc=1, timed_out=False, stdout="503"` | `True` |
| "overloaded" in stderr | `rc=1, timed_out=False, stderr="overloaded"` | `True` |
| empty stdout, rc≠0 | `rc=1, timed_out=False, stdout=""` | `True` |
| timed out | `rc=-1, timed_out=True` | `False` |
| rc=0 | `rc=0, timed_out=False` | `False` |
| 403 in output | `rc=1, stdout="403 forbidden"` | `False` |

### `_run_cli` retry integration tests

Mock `_run_subprocess` via `unittest.mock.patch`:

| Test | Mock behavior | Expected |
|---|---|---|
| Retry fires and succeeds | First call: `rc=1, stdout=""`. Second: `rc=0, stdout=valid_json` | Returns successful reply; `_run_subprocess` called twice |
| Retry fires, second also fails | Both calls: `rc=1, stdout=""` | Returns error reply; `_run_subprocess` called twice |
| No retry on timeout | First call: `timed_out=True` | `_run_subprocess` called once |
| No retry on 403 | First call: `rc=1, stdout="403 forbidden"` | `_run_subprocess` called once |

---

## Decision Flow

```
_run_subprocess() → (stdout, stderr, rc, timed_out)
        │
        ├─ _is_transient_error()? ──yes──► log_warn + sleep(2) + _run_subprocess() again
        │                                          │
        │                                          └─► (stdout, stderr, rc, timed_out)  [continue normally]
        │
        └─ no ──► continue into normal result processing
```

---

## What Does Not Change

- `/retry` command and the retry button on timeout — unchanged.
- Gemini model fallback logic (`_gemini_fallback_retry`) — unchanged; it runs after this retry block.
- Claude rate-limit detection and `claude_rate_set` — unchanged.
- All agent timeouts (`_AGENT_TIMEOUT`) — unchanged.
