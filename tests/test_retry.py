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
