# Retry on Transient Errors — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically retry agent subprocess calls once on transient errors (5xx, empty crash output) without user intervention.

**Architecture:** Add `_is_transient_error()` pure helper to `agents.py`; insert a single retry block in `_run_cli()` immediately after `_run_subprocess()` returns, before any result processing. Non-retryable errors (auth, rate-limit, timeout) are excluded by checking explicit marker strings first.

**Tech Stack:** Python stdlib (`time`, `unittest.mock`), pytest.

---

## Chunk 1: `_is_transient_error()` + unit tests

### Task 1: Add `_is_transient_error()` to agents.py and unit-test it

**Files:**
- Modify: `agents.py` (after `_run_subprocess`, before `_run_cli`)
- Create: `tests/test_retry.py`

---

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_retry.py`:

```python
"""Tests for _is_transient_error in agents.py."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents import _is_transient_error


# ── retryable ────────────────────────────────────────────────

def test_503_in_stdout_is_retryable():
    assert _is_transient_error(
        stdout="503 service unavailable", stderr="", rc=1, timed_out=False
    ) is True

def test_overloaded_in_stderr_non_empty_stdout_is_retryable():
    # Exercises the 5xx-pattern branch via stderr, not the empty-stdout branch
    assert _is_transient_error(
        stdout="partial output", stderr="overloaded", rc=1, timed_out=False
    ) is True

def test_empty_stdout_clean_stderr_is_retryable():
    assert _is_transient_error(
        stdout="", stderr="", rc=1, timed_out=False
    ) is True

def test_internal_server_error_is_retryable():
    assert _is_transient_error(
        stdout="internal server error", stderr="", rc=1, timed_out=False
    ) is True

def test_502_is_retryable():
    assert _is_transient_error(
        stdout="502 bad gateway", stderr="", rc=1, timed_out=False
    ) is True


# ── not retryable ────────────────────────────────────────────

def test_timed_out_is_not_retryable():
    assert _is_transient_error(
        stdout="", stderr="", rc=-1, timed_out=True
    ) is False

def test_rc_zero_with_error_text_is_not_retryable():
    # rc=0 means success even if output looks like an error
    assert _is_transient_error(
        stdout="503", stderr="", rc=0, timed_out=False
    ) is False

def test_403_in_stdout_is_not_retryable():
    assert _is_transient_error(
        stdout="403 forbidden", stderr="", rc=1, timed_out=False
    ) is False

def test_request_not_allowed_in_stdout_is_not_retryable():
    assert _is_transient_error(
        stdout='{"error":"request not allowed"}', stderr="", rc=1, timed_out=False
    ) is False

def test_empty_stdout_403_in_stderr_is_not_retryable():
    # Empty stdout must NOT fire when stderr has a non-retryable marker
    assert _is_transient_error(
        stdout="", stderr="403 forbidden", rc=1, timed_out=False
    ) is False

def test_rate_limit_429_is_not_retryable():
    assert _is_transient_error(
        stdout="429 rate limit exceeded", stderr="", rc=1, timed_out=False
    ) is False

def test_quota_exceeded_is_not_retryable():
    assert _is_transient_error(
        stdout="", stderr="quota exceeded", rc=1, timed_out=False
    ) is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/stx/Applications/progect/pyChatALL
python -m pytest tests/test_retry.py -v
```

Expected: `ImportError` or `AttributeError` — `_is_transient_error` does not exist yet.

- [ ] **Step 3: Implement `_is_transient_error` in agents.py**

Add these two constants and the function immediately after `_run_subprocess` (around line 179), before the `# ── УНИВЕРСАЛЬНЫЙ ВЫЗОВ CLI ──` comment banner that precedes `_run_cli`. Keep the banner above `_run_cli`.

```python
_NON_RETRYABLE_MARKERS = (
    "403", "forbidden", "request not allowed",       # auth / permission
    "429", "rate limit", "quota exceeded", "too many requests",  # rate limit
)

_TRANSIENT_PATTERNS = (
    "500", "502", "503", "504",
    "overloaded", "temporarily unavailable",
    "service unavailable", "internal server error",
)


def _is_transient_error(stdout: str, stderr: str, rc: int, timed_out: bool) -> bool:
    """Returns True if the subprocess failure is transient and worth retrying once.

    Non-retryable check runs first: any auth or rate-limit marker → False.
    Then: 5xx pattern in combined output → True.
    Then: empty stdout (subprocess crash with no output) → True.
    rc=0 and timed_out=True are always False.
    """
    if timed_out or rc == 0:
        return False
    combined = (stdout + stderr).lower()
    if any(m in combined for m in _NON_RETRYABLE_MARKERS):
        return False
    if any(p in combined for p in _TRANSIENT_PATTERNS):
        return True
    if not stdout.strip():
        return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_retry.py -v
```

Expected: 12 tests pass.

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -v
```

Expected: all 34 tests pass (22 router + 12 retry).

- [ ] **Step 6: Commit**

```bash
git add agents.py tests/test_retry.py
git commit -m "feat: add _is_transient_error() with unit tests"
```

> Note: `_run_cli` integration tests (retry fires/no-retry-on-timeout/no-retry-on-403) are in Chunk 2, alongside the actual retry block in `_run_cli`. They are deferred because they test behaviour that doesn't exist yet.

---

## Chunk 2: Retry block in `_run_cli()` + integration tests

### Task 2: Insert retry block in `_run_cli()` and add integration tests

**Files:**
- Modify: `agents.py` (inside `_run_cli`, lines ~259-260)
- Modify: `tests/test_retry.py` (append integration tests)

---

- [ ] **Step 1: Write the failing integration tests**

Append to `tests/test_retry.py`:

```python
# ── _run_cli retry integration ────────────────────────────────

import json
from unittest.mock import patch, MagicMock
from agents import _run_cli
from config import CLAUDE_BIN, CLAUDE_SESSION, CLAUDE_CTX_FILE


def _claude_success_json(text="ok"):
    """Minimal valid Claude CLI JSON response."""
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": text, "session_id": "test-session-1",
        "total_cost_usd": 0.001,
        "usage": {
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard",
            "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
            "inference_geo": "", "iterations": [], "speed": "standard",
        },
        "modelUsage": {},
        "permission_denials": [], "fast_mode_state": "off",
        "uuid": "test-uuid",
    })


def test_run_cli_retries_on_transient_error_and_succeeds():
    """First _run_subprocess returns empty/rc=1; second returns success.
    _run_subprocess must be called exactly twice."""
    fail = ("", "", 1, False)
    success = (_claude_success_json("hello"), "", 0, False)

    with patch("agents._run_subprocess", side_effect=[fail, success]) as mock_sub, \
         patch("ui.tg_send"), \
         patch("agents._load_session", return_value=None), \
         patch("agents.memory_load", return_value=""), \
         patch("agents.shared_ctx_for_prompt", return_value=""), \
         patch("agents._add_ctx", return_value=0), \
         patch("time.sleep"):
        result = _run_cli(CLAUDE_BIN, CLAUDE_SESSION, CLAUDE_CTX_FILE,
                          "Claude", "hi")

    assert mock_sub.call_count == 2
    assert "hello" in result


def test_run_cli_no_retry_on_timeout():
    """timed_out=True must NOT trigger a retry."""
    timed_out_result = ("", "", -1, True)

    with patch("agents._run_subprocess", return_value=timed_out_result) as mock_sub, \
         patch("ui.tg_send"), \
         patch("agents._load_session", return_value=None), \
         patch("agents.memory_load", return_value=""), \
         patch("agents.shared_ctx_for_prompt", return_value=""), \
         patch("agents._add_ctx", return_value=0), \
         patch("time.sleep"):
        result = _run_cli(CLAUDE_BIN, CLAUDE_SESSION, CLAUDE_CTX_FILE,
                          "Claude", "hi")

    assert mock_sub.call_count == 1
    assert "не ответил" in result  # timeout message


def test_run_cli_no_retry_on_403():
    """403 forbidden must NOT trigger a retry."""
    # Plain text as emitted by real CLI auth failures — "403" appears in raw stdout
    auth_error = ("403 forbidden", "", 1, False)

    with patch("agents._run_subprocess", return_value=auth_error) as mock_sub, \
         patch("ui.tg_send"), \
         patch("agents._load_session", return_value=None), \
         patch("agents.memory_load", return_value=""), \
         patch("agents.shared_ctx_for_prompt", return_value=""), \
         patch("agents._add_ctx", return_value=0), \
         patch("time.sleep"):
        result = _run_cli(CLAUDE_BIN, CLAUDE_SESSION, CLAUDE_CTX_FILE,
                          "Claude", "hi")

    assert mock_sub.call_count == 1


def test_run_cli_retry_fires_both_fail_returns_error():
    """Both attempts fail → _run_subprocess called twice, error returned to user."""
    fail = ("", "", 1, False)

    with patch("agents._run_subprocess", return_value=fail) as mock_sub, \
         patch("ui.tg_send"), \
         patch("agents._load_session", return_value=None), \
         patch("agents.memory_load", return_value=""), \
         patch("agents.shared_ctx_for_prompt", return_value=""), \
         patch("agents._add_ctx", return_value=0), \
         patch("time.sleep"):
        result = _run_cli(CLAUDE_BIN, CLAUDE_SESSION, CLAUDE_CTX_FILE,
                          "Claude", "hi")

    assert mock_sub.call_count == 2
    # Should return some non-empty response (empty reply fallback)
    assert result  # not empty
```

- [ ] **Step 2: Run integration tests to verify they fail**

```bash
python -m pytest tests/test_retry.py -k "run_cli" -v
```

Expected: tests fail (retry block not yet added to `_run_cli`). The `test_run_cli_retries_on_transient_error_and_succeeds` test will fail because `mock_sub.call_count == 1`, not 2.

- [ ] **Step 3: Add retry block to `_run_cli` in agents.py**

In `agents.py`, find these two lines (around line 259):

```python
        stdout, stderr, rc, timed_out = _run_subprocess(cmd, timeout_secs, WORK_DIR, env)
        elapsed = time.time() - t_start
```

Replace with:

```python
        stdout, stderr, rc, timed_out = _run_subprocess(cmd, timeout_secs, WORK_DIR, env)

        if _is_transient_error(stdout, stderr, rc, timed_out):
            log_warn(f"{agent_name}: transient error (rc={rc}), retrying in 2s…")
            time.sleep(2)
            stdout, stderr, rc, timed_out = _run_subprocess(cmd, timeout_secs, WORK_DIR, env)

        elapsed = time.time() - t_start
```

- [ ] **Step 4: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all 38 tests pass (22 router + 12 unit + 4 integration).

- [ ] **Step 5: Commit**

```bash
git add agents.py tests/test_retry.py
git commit -m "feat: auto-retry transient errors in _run_cli()"
```
