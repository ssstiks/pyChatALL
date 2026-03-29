# Request Lifecycle: Task Queue + Cancel Button — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serialize incoming messages through a persistent worker thread and allow the user to cancel the active request via an inline "🛑 Отмена" button.

**Architecture:** Three module-level threading primitives (`_request_queue`, `_cancel_event`, `_worker_busy`) in `tg_agent.py` coordinate a persistent daemon thread (`_queue_worker`). `agents.py` gains `_active_proc` + `cancel_active_proc()` so the cancel handler can SIGKILL the running subprocess. The `_is_transient_error` guard is tightened from `rc == 0` to `rc <= 0` to prevent retrying SIGKILL'd processes (Linux returns rc=-9).

**Tech Stack:** Python stdlib (`queue`, `threading`, `subprocess`), existing `tg_send`/`tg_edit`/`kb` helpers from `ui.py`.

---

## Chunk 1: agents.py changes

### Task 1: Fix `_is_transient_error`, add `_active_proc` + `cancel_active_proc()`, modify `_run_subprocess`

**Files:**
- Modify: `agents.py`

---

- [ ] **Step 1: Add a test for the `rc=-9` (SIGKILL) case**

Append to `tests/test_retry.py` (after the last existing test):

```python
def test_sigkill_rc_negative9_is_not_retryable():
    # On Linux, SIGKILL sets returncode = -9.
    # This must NOT be retried — the process was intentionally killed.
    assert _is_transient_error(
        stdout="", stderr="", rc=-9, timed_out=False
    ) is False
```

- [ ] **Step 2: Run tests to verify it fails (guard not fixed yet)**

```bash
cd /home/stx/Applications/progect/pyChatALL
python -m pytest tests/test_retry.py::test_sigkill_rc_negative9_is_not_retryable -v
```

Expected: FAIL — old guard `rc == 0` does not catch `rc=-9`, so `_is_transient_error` returns `True`.

- [ ] **Step 3: Fix `_is_transient_error` guard in agents.py**

In `agents.py` find (line ~201):

```python
    if timed_out or rc == 0:
```

Change to:

```python
    if timed_out or rc <= 0:
```

- [ ] **Step 4: Run all tests to verify the guard fix**

```bash
python -m pytest tests/test_retry.py -v
```

Expected: all 17 tests pass (16 existing + 1 new).

- [ ] **Step 5: Add `_active_proc` and `cancel_active_proc()` to agents.py**

Add immediately after `_run_subprocess` ends (after line ~178, before `_NON_RETRYABLE_MARKERS`):

```python
_active_proc: "subprocess.Popen | None" = None


def cancel_active_proc() -> None:
    """Kill the currently running subprocess, if any. Thread-safe under CPython GIL."""
    global _active_proc
    if _active_proc is not None and _active_proc.poll() is None:
        _active_proc.kill()
        try:
            _active_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    _active_proc = None
```

- [ ] **Step 6: Modify `_run_subprocess` to set/clear `_active_proc`**

Replace the entire `_run_subprocess` body (keep signature unchanged). The key change is adding `global _active_proc`, assigning `_active_proc = proc` after `Popen`, and clearing via `finally`:

```python
def _run_subprocess(cmd: list, timeout: int, cwd: str, env: dict
                    ) -> tuple[str, str, int, bool]:
    """Запускает процесс; при таймауте убивает его и ждёт завершения.
    Возвращает (stdout, stderr, returncode, timed_out)."""
    global _active_proc
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=cwd, env=env)
    _active_proc = proc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout, stderr, proc.returncode, False
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return "", "", -1, True
    finally:
        _active_proc = None
```

The `finally: _active_proc = None` ensures the reference is cleared regardless of whether the subprocess succeeded, timed out, or was killed externally.

- [ ] **Step 7: Add `cancel_active_proc` to the import in `tg_agent.py`**

In `tg_agent.py`, find the `from agents import (` block (line ~63). It currently looks like:

```python
from agents import (
    _parse_cli_output, _is_gemini_capacity_error, _gemini_fallback_retry,
    _run_subprocess, _run_cli, _run_passthrough,
    _find_binary, check_agents, _get_effective_bin, run_startup_check,
    ask_claude, ask_gemini, ask_qwen,
```

Add `cancel_active_proc,` on its own line after `run_startup_check,`:

```python
from agents import (
    _parse_cli_output, _is_gemini_capacity_error, _gemini_fallback_retry,
    _run_subprocess, _run_cli, _run_passthrough,
    _find_binary, check_agents, _get_effective_bin, run_startup_check,
    cancel_active_proc,
    ask_claude, ask_gemini, ask_qwen,
```

- [ ] **Step 8: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all 39 tests pass (22 router + 17 retry).

- [ ] **Step 9: Commit**

```bash
git add agents.py tg_agent.py tests/test_retry.py
git commit -m "feat: add _active_proc + cancel_active_proc(); fix _is_transient_error rc <= 0"
```

---

## Chunk 2: Queue primitives + worker thread

### Task 2: Add `_request_queue`, `_cancel_event`, `_worker_busy`, `_queue_worker()`, start worker in `main()`

**Files:**
- Modify: `tg_agent.py`

---

- [ ] **Step 1: Add `import queue` and module-level primitives**

In `tg_agent.py`, at the top of the file (after the existing `import threading` line), add:

```python
import queue
```

Then after `import team_mode` (and any existing module-level assignments), add:

```python
_request_queue: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
_cancel_event = threading.Event()
_worker_busy = threading.Event()
```

- [ ] **Step 2: Add `_queue_worker()` function before `route_and_reply`**

Add immediately before `def route_and_reply` (before line ~427):

```python
# ── ОЧЕРЕДЬ ЗАПРОСОВ ──────────────────────────────────────────
def _queue_worker() -> None:
    """Persistent daemon thread. Processes one request at a time."""
    while True:
        try:
            item = _request_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            # Discard items dequeued during a cancel window.
            # _cancel_event is cleared HERE on the discard path only.
            # On the non-cancel path, route_and_reply clears it at the
            # commit point (after placeholder is sent).
            if _cancel_event.is_set():
                _cancel_event.clear()
                continue
            _worker_busy.set()
            text, file_path = item
            try:
                route_and_reply(text, file_path)
            finally:
                _worker_busy.clear()
        finally:
            _request_queue.task_done()
```

**Critical detail:** `_cancel_event.clear()` is called **only** on the discard path (`if _cancel_event.is_set(): ...`). On the non-cancel path, `_cancel_event` is **not** cleared here — that is done by `route_and_reply` at the commit point (after sending the placeholder). This asymmetry is intentional: it prevents the worker from racing with the cancel handler.

- [ ] **Step 3: Start the worker thread in `main()`**

In `main()`, add after `threading.Thread(target=run_startup_check, daemon=True).start()`:

```python
    threading.Thread(target=_queue_worker, daemon=True, name="queue-worker").start()
```

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all 39 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tg_agent.py
git commit -m "feat: add queue primitives + _queue_worker() daemon thread"
```

---

## Chunk 3: process_update changes

### Task 3: Replace thread-spawn in `process_update` with queue enqueue

**Files:**
- Modify: `tg_agent.py` (lines ~1135–1139 in `process_update`)

---

- [ ] **Step 1: Replace `threading.Thread(...).start()` in `process_update` with queue put**

Find in `process_update` (lines ~1135–1139):

```python
    threading.Thread(
        target=route_and_reply,
        args=(prompt_text or "", file_path),
        daemon=True,
    ).start()
```

Replace with:

```python
    qsize_before = _request_queue.qsize()
    _request_queue.put((prompt_text or "", file_path))
    if _worker_busy.is_set() or qsize_before > 0:
        pos = qsize_before + 1
        tg_send(f"📋 В очереди (позиция {pos})")
```

`qsize_before` is captured **before** `put()` for a stable count. Position shown may be off by one in rare races (worker dequeues between capture and put) — this is advisory only and accepted.

- [ ] **Step 2: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all 39 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tg_agent.py
git commit -m "feat: enqueue requests via queue.Queue in process_update"
```

---

## Chunk 4: route_and_reply changes

### Task 4: Add cancel button + commit-point check + while-loop check + post-join check in `route_and_reply`

**Files:**
- Modify: `tg_agent.py` (lines ~742–780 in `route_and_reply`)

---

- [ ] **Step 1: Add cancel button to placeholder and commit-point cancel check**

In `route_and_reply`, find (lines ~745–746):

```python
    ph = tg_send(f"⏳ {lbl} думает... (макс {timeout_mins} мин)")
    ph_id = ph["message_id"] if ph else None
```

Replace with:

```python
    cancel_markup = kb([[("🛑 Отмена", "cancel_current")]])
    ph = tg_send(f"⏳ {lbl} думает... (макс {timeout_mins} мин)", cancel_markup)
    ph_id = ph["message_id"] if ph else None

    # Commit point: cancel may have fired between dequeue and placeholder send.
    # Edit the new placeholder and return if cancel is set.
    # The cancel handler may concurrently edit the same placeholder — this is a
    # benign double-edit: tg_edit handles 400 "message not modified" gracefully.
    if _cancel_event.is_set():
        if ph_id:
            tg_edit(ph_id, "❌ Запрос отменён")
        _cancel_event.clear()
        return
    _cancel_event.clear()  # Committed to this request — clear any stale cancel state
```

- [ ] **Step 2: Add cancel check inside while loop**

Find the while loop (lines ~757–764):

```python
    while t.is_alive():
        time.sleep(5)
        if not t.is_alive():
            break
        elapsed = int(time.time() - t_start)
        if ph_id:
            tg_edit(ph_id, f"⏳ {lbl} думает... {elapsed}с / {timeout_secs}с")
        tg_typing()
```

Replace with:

```python
    while t.is_alive():
        time.sleep(5)
        if _cancel_event.is_set():
            break
        if not t.is_alive():
            break
        elapsed = int(time.time() - t_start)
        if ph_id:
            tg_edit(ph_id, f"⏳ {lbl} думает... {elapsed}с / {timeout_secs}с", cancel_markup)
        tg_typing()
```

Note: cancel check (`_cancel_event.is_set()`) comes **before** the `if not t.is_alive(): break` check — this matches the spec's ordering.

- [ ] **Step 3: Add post-join cancel check**

Find (line ~766–767):

```python
    t.join()
    reply = result_box[0] if result_box else "❌ Нет ответа"
```

Replace with:

```python
    t.join()
    if _cancel_event.is_set():
        return  # Cancel handler already edited placeholder to "❌ Запрос отменён"
    reply = result_box[0] if result_box else "❌ Нет ответа"
```

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all 39 tests pass.

- [ ] **Step 5: Commit**

```bash
git add tg_agent.py
git commit -m "feat: add cancel button + cancel checks in route_and_reply"
```

---

## Chunk 5: cancel handler

### Task 5: Add `cancel_current` handler in `handle_callback`

**Files:**
- Modify: `tg_agent.py` (inside `handle_callback`)

---

- [ ] **Step 1: Add cancel handler at the top of the if/elif chain**

In `handle_callback`, find (line ~812):

```python
    if data == "retry_last":
```

Replace that line (and leave the rest of the `retry_last` block untouched) so that the cancel handler appears first:

```python
    if data == "cancel_current":
        tg_answer_cb(cb_id, "❌ Отменяю...")
        cancel_active_proc()
        _cancel_event.set()
        while not _request_queue.empty():
            try:
                _request_queue.get_nowait()
                _request_queue.task_done()
            except queue.Empty:
                break
        if msg_id:
            tg_edit(msg_id, "❌ Запрос отменён")
        return

    elif data == "retry_last":
```

**Safety note:** The drain loop calls `task_done()` only for items it retrieves via `get_nowait()`. This is safe because `queue.get()` atomically removes an item before any `get_nowait()` can return the same item — the worker and the drain loop cannot hold the same item simultaneously.

The cancel handler is the **only** place that edits the placeholder to "❌ Запрос отменён". `route_and_reply` returns silently (without editing) when it detects `_cancel_event.is_set()` after `t.join()`.

- [ ] **Step 2: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all 39 tests pass.

- [ ] **Step 3: Manual smoke test**

Start the bot:
```bash
nohup python3 tg_agent.py >> /tmp/tg_agent.log 2>&1 &
```

Verify:
1. Send a message → placeholder shows "⏳ думает... (макс N мин)" with "🛑 Отмена" button.
2. Send a second message while first is processing → "📋 В очереди (позиция 1)".
3. Press "🛑 Отмена" → placeholder changes to "❌ Запрос отменён", queue drains.
4. Send another message → processes normally (no stale cancel state).

- [ ] **Step 4: Commit**

```bash
git add tg_agent.py
git commit -m "feat: add cancel_current handler — SIGKILL + drain queue + edit placeholder"
```

---

## Edge Cases Reference

| Scenario | Behaviour |
|---|---|
| Cancel before subprocess starts | `_active_proc` is None → `cancel_active_proc()` is no-op. Subprocess runs to completion; output discarded by post-join check. Known limitation: subprocess consumes resources until timeout. |
| Cancel after subprocess finishes but before reply sent | `_cancel_event` set → `route_and_reply` returns silently after join |
| Cancel with empty queue | Drain loop is a no-op |
| Two cancel presses | Second press: `_active_proc` is None, queue empty → no-op |
| Queue position off by one | Race between `qsize_before` capture and worker dequeue; advisory only |
| Benign double-edit | Cancel handler and commit-point check may both call `tg_edit` on same placeholder; `tg_edit` handles 400 "message not modified" gracefully |
