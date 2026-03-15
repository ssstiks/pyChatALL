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


# ── _run_cli retry integration ────────────────────────────────

import json
from unittest.mock import patch
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
         patch("agents._save_session"), \
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
         patch("agents._save_session"), \
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
         patch("agents._save_session"), \
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
         patch("agents._save_session"), \
         patch("agents.memory_load", return_value=""), \
         patch("agents.shared_ctx_for_prompt", return_value=""), \
         patch("agents._add_ctx", return_value=0), \
         patch("time.sleep"):
        result = _run_cli(CLAUDE_BIN, CLAUDE_SESSION, CLAUDE_CTX_FILE,
                          "Claude", "hi")

    assert mock_sub.call_count == 2
    assert result  # not empty


def test_sigkill_rc_negative9_is_not_retryable():
    # On Linux, SIGKILL sets returncode = -9.
    # This must NOT be retried — the process was intentionally killed.
    assert _is_transient_error(
        stdout="", stderr="", rc=-9, timed_out=False
    ) is False
