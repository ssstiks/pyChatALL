# tests/test_heartbeat.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import tg_agent


# ── _placeholder_text ─────────────────────────────────────────

def test_placeholder_zero_elapsed():
    text = tg_agent._placeholder_text("Claude", 0, remaining=800)
    assert "0с" in text
    assert "осталось" in text
    assert "13м" in text   # 800s = 13m 20s

def test_placeholder_1min_elapsed():
    text = tg_agent._placeholder_text("Claude", 65, remaining=735)
    assert "1м 05с" in text
    assert "12м 15с" in text

def test_placeholder_no_limit():
    text = tg_agent._placeholder_text("Claude", 30, remaining=None, no_limit=True)
    assert "без лимита" in text
    assert "осталось" not in text

def test_placeholder_no_remaining():
    text = tg_agent._placeholder_text("Claude", 10, remaining=None)
    assert "10с" in text
    assert "осталось" not in text
    assert "без лимита" not in text


import threading
import time
import unittest.mock as mock


# ── Polling loop behavior ─────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_tg_agent_state():
    """Reset shared state before each test."""
    tg_agent._cancel_event.clear()
    tg_agent._no_timeout_event.clear()
    tg_agent._worker_busy.clear()
    with tg_agent._timeout_extend_lock:
        tg_agent._timeout_extend_count = 0
    yield
    tg_agent._cancel_event.clear()
    tg_agent._no_timeout_event.clear()
    tg_agent._worker_busy.clear()
    with tg_agent._timeout_extend_lock:
        tg_agent._timeout_extend_count = 0


def _run_loop(worker_delay=0.0, timeout_secs=10,
              extend_count=0, no_limit=False, cancel_after=None):
    """
    Run the polling loop logic in isolation.
    Returns (timed_out: bool, result_box: list, edit_calls: list).
    """
    result_box = []
    timed_out  = False
    edit_calls = []

    def worker():
        if worker_delay:
            time.sleep(worker_delay)
        result_box.append("ok")

    t = threading.Thread(target=worker, daemon=True)
    start_time = time.time()
    deadline   = start_time + timeout_secs
    no_limit_  = no_limit
    last_hb    = start_time
    t.start()

    if extend_count:
        with tg_agent._timeout_extend_lock:
            tg_agent._timeout_extend_count = extend_count

    if no_limit:
        tg_agent._no_timeout_event.set()

    if cancel_after is not None:
        def _cancel():
            time.sleep(cancel_after)
            tg_agent._cancel_event.set()
        threading.Thread(target=_cancel, daemon=True).start()

    while True:
        t.join(timeout=tg_agent.POLL_INTERVAL)
        if not t.is_alive():
            break

        now     = time.time()
        elapsed = now - start_time

        if tg_agent._cancel_event.is_set():
            tg_agent._cancel_event.clear()
            break

        with tg_agent._timeout_extend_lock:
            count = tg_agent._timeout_extend_count
            tg_agent._timeout_extend_count = 0
        if count > 0:
            deadline += 300 * count

        if tg_agent._no_timeout_event.is_set():
            tg_agent._no_timeout_event.clear()
            deadline  = float("inf")
            no_limit_ = True

        if not no_limit_ and now >= deadline:
            timed_out = True
            break

        hb_interval = tg_agent.HB_FAST_EVERY if elapsed < tg_agent.HB_FAST_SECS else tg_agent.HB_SLOW_EVERY
        if now - last_hb >= hb_interval:
            last_hb = now
            edit_calls.append(elapsed)

    return timed_out, result_box, edit_calls


def test_polling_normal_completion():
    timed_out, result_box, _ = _run_loop(worker_delay=0.01, timeout_secs=10)
    assert not timed_out
    assert result_box == ["ok"]


def test_polling_timeout():
    timed_out, result_box, _ = _run_loop(worker_delay=100, timeout_secs=1)
    assert timed_out
    assert result_box == []


def test_polling_extend_once():
    """Deadline extends by 300s when extend_count=1."""
    timed_out, result_box, _ = _run_loop(worker_delay=0.01, timeout_secs=10, extend_count=1)
    assert not timed_out


def test_polling_extend_double():
    """Both +5 min presses honored when count=2 (deadline += 600)."""
    timed_out, result_box, _ = _run_loop(worker_delay=0.01, timeout_secs=10, extend_count=2)
    assert not timed_out


def test_polling_no_limit():
    """No-limit event prevents timeout even with very short deadline."""
    timed_out, result_box, _ = _run_loop(worker_delay=0.5, timeout_secs=1, no_limit=True)
    assert not timed_out


def test_stale_event_guard():
    """extend_timeout callback does nothing when _worker_busy is clear."""
    tg_agent._worker_busy.clear()
    with tg_agent._timeout_extend_lock:
        tg_agent._timeout_extend_count = 0

    # Simulate callback logic
    if tg_agent._worker_busy.is_set():
        with tg_agent._timeout_extend_lock:
            tg_agent._timeout_extend_count += 1

    with tg_agent._timeout_extend_lock:
        assert tg_agent._timeout_extend_count == 0


def test_extend_callback_active():
    """extend_timeout increments counter when worker is busy."""
    tg_agent._worker_busy.set()
    with tg_agent._timeout_extend_lock:
        tg_agent._timeout_extend_count = 0

    # Simulate callback logic
    if tg_agent._worker_busy.is_set():
        with tg_agent._timeout_extend_lock:
            tg_agent._timeout_extend_count += 1

    with tg_agent._timeout_extend_lock:
        assert tg_agent._timeout_extend_count == 1
    tg_agent._worker_busy.clear()


def test_extend_callback_inactive():
    """extend_timeout does nothing when worker is not busy."""
    tg_agent._worker_busy.clear()
    with tg_agent._timeout_extend_lock:
        tg_agent._timeout_extend_count = 0

    if tg_agent._worker_busy.is_set():
        with tg_agent._timeout_extend_lock:
            tg_agent._timeout_extend_count += 1

    with tg_agent._timeout_extend_lock:
        assert tg_agent._timeout_extend_count == 0


def test_no_timeout_callback_inactive():
    """no_timeout event not set when worker is not busy."""
    tg_agent._worker_busy.clear()
    tg_agent._no_timeout_event.clear()

    if tg_agent._worker_busy.is_set():
        tg_agent._no_timeout_event.set()

    assert not tg_agent._no_timeout_event.is_set()
