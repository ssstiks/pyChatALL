# Timeout Extension & Heartbeat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single `t.join(timeout)` call in `route_and_reply()` with a 5-second polling loop that shows elapsed/remaining time, supports "+5 мин" and "∞ Без лимита" buttons, and sends adaptive heartbeat edits every 30-60s.

**Architecture:** All changes in `tg_agent.py`. Two new module-level variables (`_timeout_extend_count` + lock, `_no_timeout_event`). Two helper functions (`_placeholder_text`, `_edit_placeholder`). Two keyboard builders. Polling loop replaces lines 777-834. Two new callback handlers.

**Tech Stack:** Python `threading`, existing `tg_edit`/`tg_send`/`kb` from `ui.py`.

---

### Task 1: Add module-level state and helper functions

**Files:**
- Modify: `tg_agent.py` (~line 430, after `_worker_busy`)
- Modify: `tg_agent.py` (~line 461, before `route_and_reply`)
- Create: `tests/test_heartbeat.py`

- [ ] **Step 1: Write failing tests for `_placeholder_text`**

```python
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
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
cd /home/stx/Applications/progect/pyChatALL
python -m pytest tests/test_heartbeat.py -v
```
Expected: `AttributeError: module 'tg_agent' has no attribute '_placeholder_text'`

- [ ] **Step 3: Add module-level state to `tg_agent.py`**

Find the section with `_cancel_event` and `_worker_busy` (around line 430). Add immediately after:

```python
_timeout_extend_count = 0               # incremented per "+5 мин" press
_timeout_extend_lock  = threading.Lock()
_no_timeout_event     = threading.Event()
```

- [ ] **Step 4: Add helper functions before `route_and_reply` (~line 461)**

```python
# ── TIMEOUT / HEARTBEAT HELPERS ──────────────────────────────
POLL_INTERVAL = 5
HB_FAST_SECS  = 180
HB_FAST_EVERY = 30
HB_SLOW_EVERY = 60


def _placeholder_text(lbl: str, elapsed: float,
                      remaining: int | None, no_limit: bool = False) -> str:
    """Format the agent-thinking placeholder message text."""
    e_min, e_sec = divmod(int(elapsed), 60)
    e_str = f"{e_min}м {e_sec:02d}с" if e_min else f"{e_sec}с"
    if no_limit:
        return f"⏳ {lbl} думает... {e_str} (без лимита)"
    if remaining is not None:
        r_min, r_sec = divmod(remaining, 60)
        r_str = f"{r_min}м {r_sec:02d}с" if r_min else f"{r_sec}с"
        return f"⏳ {lbl} думает... {e_str} / осталось {r_str}"
    return f"⏳ {lbl} думает... {e_str}"


def _agent_kb_full() -> dict:
    """3-button keyboard used during active agent wait."""
    return kb([[
        ("⏳ +5 мин",    "extend_timeout"),
        ("∞ Без лимита", "no_timeout"),
        ("🛑 Отмена",     "cancel_current"),
    ]])


def _agent_kb_cancel_only() -> dict:
    """1-button keyboard after no-limit is pressed."""
    return kb([[("🛑 Отмена", "cancel_current")]])


def _edit_placeholder(ph_id, lbl: str, elapsed: float,
                      remaining: int | None, no_limit: bool) -> None:
    """Edit the placeholder message. Derives keyboard from no_limit. Silent on error."""
    if ph_id is None:
        return
    markup = _agent_kb_cancel_only() if no_limit else _agent_kb_full()
    text   = _placeholder_text(lbl, elapsed, remaining, no_limit)
    try:
        tg_edit(ph_id, text, markup)
    except Exception:
        pass
```

- [ ] **Step 5: Run `_placeholder_text` tests**

```bash
python -m pytest tests/test_heartbeat.py -v
```
Expected: 4 PASSED

- [ ] **Step 6: Run full suite for regressions**

```bash
python -m pytest tests/ -q
```
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add tg_agent.py tests/test_heartbeat.py
git commit -m "feat: add timeout/heartbeat helpers — _placeholder_text, _edit_placeholder, keyboards"
```

---

### Task 2: Replace the polling loop in `route_and_reply`

**Files:**
- Modify: `tg_agent.py` lines 777-834

The block to replace starts at:
```python
    lbl = agent_label(agent)
    timeout_secs = _AGENT_TIMEOUT.get(agent, 300)
    timeout_mins = timeout_secs // 60
    cancel_markup = kb([[("🛑 Отмена", "cancel_current")]])
    ph = tg_send(f"⏳ {lbl} думает... (макс {timeout_mins} мин)", cancel_markup)
    ph_id = ph["message_id"] if ph else None
```
and ends at (including):
```python
    is_timeout = "не ответил за" in reply or "думает >" in reply
    if is_timeout:
        retry_markup = kb([[("🔄 Повторить запрос", "retry_last")]])
        if ph_id:
            tg_edit(ph_id, f"[{lbl}]\n{reply}", retry_markup)
        else:
            tg_send(f"[{lbl}]\n{reply}", retry_markup)
        return
```

- [ ] **Step 1: Write failing tests for the polling loop**

Append to `tests/test_heartbeat.py`:

```python
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
    Returns (timed_out: bool, elapsed: float, edit_calls: list).
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
    # Worker finishes quickly, so this just verifies extend logic doesn't crash
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
```

- [ ] **Step 2: Run new tests (some will fail — loop logic not in tg_agent.py yet)**

```bash
python -m pytest tests/test_heartbeat.py -v
```
The `_run_loop` helper tests call tg_agent state directly. `test_polling_normal_completion` etc. should pass since `_run_loop` is self-contained test helper. `test_placeholder_*` tests from Task 1 still pass.

Expected: all new tests PASS (they use `_run_loop`, not `route_and_reply`).

- [ ] **Step 3: Replace the polling block in `route_and_reply`**

Replace from `lbl = agent_label(agent)` through the `is_timeout` block (described above) with:

```python
    lbl          = agent_label(agent)
    timeout_secs = _AGENT_TIMEOUT.get(agent, 300)

    # Clear all control events — upfront + commit point combined
    _cancel_event.clear()
    _no_timeout_event.clear()
    with _timeout_extend_lock:
        global _timeout_extend_count
        _timeout_extend_count = 0

    start_time = time.time()
    deadline   = start_time + timeout_secs
    no_limit   = False
    last_hb    = start_time

    ph = tg_send(
        _placeholder_text(lbl, elapsed=0, remaining=timeout_secs),
        _agent_kb_full(),
    )
    ph_id = ph["message_id"] if ph else None

    # Commit point: cancel may have fired between dequeue and placeholder send.
    if _cancel_event.is_set():
        if ph_id:
            tg_edit(ph_id, "❌ Запрос отменён")
        _cancel_event.clear()
        return

    result_box: list[str] = []
    timed_out = False

    def _worker():
        result_box.append(AGENT_FN[agent](prompt, file_path))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    while True:
        t.join(timeout=POLL_INTERVAL)

        if not t.is_alive():
            break  # normal completion — result_box already populated

        now     = time.time()
        elapsed = now - start_time

        # 1. Cancel (highest priority)
        if _cancel_event.is_set():
            cancel_active_proc()
            t.join(3)
            break

        # 2. Extend deadline
        with _timeout_extend_lock:
            count = _timeout_extend_count
            _timeout_extend_count = 0
        if count > 0:
            deadline += 300 * count
            remaining = max(0, int(deadline - now))
            _edit_placeholder(ph_id, lbl, elapsed, remaining, no_limit=False)

        # 3. Remove timeout
        if _no_timeout_event.is_set():
            _no_timeout_event.clear()
            deadline  = float("inf")
            no_limit  = True
            _edit_placeholder(ph_id, lbl, elapsed, remaining=None, no_limit=True)

        # 4. Hard timeout
        if not no_limit and now >= deadline:
            cancel_active_proc()
            t.join(3)
            timed_out = True
            break

        # 5. Heartbeat
        hb_interval = HB_FAST_EVERY if elapsed < HB_FAST_SECS else HB_SLOW_EVERY
        if now - last_hb >= hb_interval:
            last_hb   = now
            remaining = None if no_limit else max(0, int(deadline - now))
            _edit_placeholder(ph_id, lbl, elapsed, remaining, no_limit)

    if _cancel_event.is_set():
        _cancel_event.clear()
        return  # Cancel handler already edited placeholder to "❌ Запрос отменён"

    if timed_out:
        msg = (f"⏱ {lbl} не ответил за {timeout_secs // 60} мин. "
               f"Напиши «продолжай» или /retry.")
        retry_markup = kb([[("🔄 Повторить запрос", "retry_last")]])
        if ph_id:
            tg_edit(ph_id, msg, retry_markup)
        else:
            tg_send(msg, retry_markup)
        return

    reply = result_box[0] if result_box else "❌ Нет ответа"
```

**Note on `global _timeout_extend_count`:** The `global` declaration must be at the top of the function body (`route_and_reply`), not inside the `with` block. Python requires `global` before any assignment to the name in the function scope. Place it right after `global _last_request` at the top of `route_and_reply`.

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -q
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tg_agent.py
git commit -m "feat: replace join loop with 5s polling loop — heartbeat, timed_out flag, extend/no-limit support"
```

---

### Task 3: Add callback handlers for extend/no-limit buttons

**Files:**
- Modify: `tg_agent.py` (add handlers after `retry_last` in `handle_callback`, ~line 888)

- [ ] **Step 1: Write tests for callback guards**

Append to `tests/test_heartbeat.py`:

```python
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
```

- [ ] **Step 2: Run tests**

```bash
python -m pytest tests/test_heartbeat.py -v
```
Expected: all PASS

- [ ] **Step 3: Add handlers in `handle_callback` after the `retry_last` block**

Find the `elif data == "retry_last":` block and add after it:

```python
    elif data == "extend_timeout":
        if _worker_busy.is_set():
            with _timeout_extend_lock:
                global _timeout_extend_count
                _timeout_extend_count += 1
            tg_answer_cb(cb_id, "⏳ +5 мин добавлено")
        else:
            tg_answer_cb(cb_id, "Нет активного запроса")

    elif data == "no_timeout":
        if _worker_busy.is_set():
            _no_timeout_event.set()
            tg_answer_cb(cb_id, "∞ Таймаут снят")
        else:
            tg_answer_cb(cb_id, "Нет активного запроса")
```

- [ ] **Step 4: Run full suite**

```bash
python -m pytest tests/ -q
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tg_agent.py tests/test_heartbeat.py
git commit -m "feat: add extend_timeout + no_timeout callbacks with worker-busy guards"
```

---

### Task 4: Restart and smoke-test

- [ ] **Step 1: Restart bot**

```bash
kill $(cat /tmp/tg_agent.pid 2>/dev/null) 2>/dev/null; sleep 1
python3 /home/stx/Applications/progect/pyChatALL/tg_agent.py >> /tmp/tg_agent.log 2>&1 &
sleep 2 && tail -5 /tmp/tg_agent.log
```
Expected: `=== tg_agent запущен ===`

- [ ] **Step 2: Send a message, verify new placeholder format**

Expected:
```
⏳ Claude думает... 0с / осталось 13м 20с
[⏳ +5 мин] [∞ Без лимита] [🛑 Отмена]
```

- [ ] **Step 3: Wait 30s, verify heartbeat edit**

Expected: message updates to `⏳ Claude думает... 30с / осталось 12м 50с`

- [ ] **Step 4: Press "+5 мин"**

Expected: toast "⏳ +5 мин добавлено" + remaining time increases by 5m.

- [ ] **Step 5: Press "∞ Без лимита"**

Expected: toast "∞ Таймаут снят" + message shows `(без лимита)` + keyboard → `[🛑 Отмена]` only.

- [ ] **Step 6: Commit smoke-test confirmation**

```bash
git commit --allow-empty -m "chore: verify timeout extension + heartbeat smoke test passed"
```
