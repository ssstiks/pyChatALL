# Request Lifecycle: Task Queue + Cancel Button — Design Spec

**Date:** 2026-03-14
**Status:** Approved
**Scope:** Task queue (serialize concurrent requests) + cancel button (kill active request + drain queue). Heartbeat and typing indicator are already implemented; this spec does not change them.

> **Single-user bot assumption:** Cancel is global — any cancel press affects the currently active request regardless of who sent it. This is acceptable for a single-user bot.

---

## Problem

The bot currently spawns a daemon thread per incoming message, so two messages sent while an agent is processing run concurrently and race. There is no way for the user to cancel a running request. This spec adds:

1. **Task queue** — incoming messages are serialized; a second message waits its turn and the user is notified.
2. **Cancel button** — an inline "🛑 Отмена" button on the "thinking..." placeholder kills the active subprocess and clears the queue.

---

## Goals

- One agent request runs at a time; extras queue up in order.
- User sees "📋 В очереди (позиция N)" when their message is queued.
- Cancel kills the current subprocess, discards any in-flight reply, and clears all queued items.
- No change to `/retry`, retry button, `send_to_agent`, Gemini fallback, or rate-limit detection.

---

## Out of Scope

- Typing indicator and heartbeat (already implemented via the 5-second poll loop).
- Per-agent queues or priorities.
- Queue persistence across restarts.
- "Cancel and keep queue" (cancel always clears the full queue per user decision).
- Making `/retry` queue-aware (it bypasses the queue intentionally; concurrent subprocess risk is accepted).

---

## Architecture

### New state in `tg_agent.py` (module level)

| Name | Type | Purpose |
|---|---|---|
| `_request_queue` | `queue.Queue` | Holds `(text, file_path)` tuples waiting to be processed |
| `_cancel_event` | `threading.Event` | Set by cancel handler; checked in while loop and after `t.join()` |
| `_worker_busy` | `threading.Event` | Set while a request is processing; used to detect "busy" on arrival |

### New `_queue_worker()` in `tg_agent.py`

Single persistent daemon thread started in `main()`. Replaces per-request thread spawning in `process_update`.

```python
def _queue_worker() -> None:
    while True:
        try:
            item = _request_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            # Discard items dequeued during a cancel window (cancel fired while
            # worker was blocked in get()). Clear event only on the discard path.
            # On the non-cancel path, route_and_reply clears the event after the
            # placeholder is sent (the commit point).
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

### Start worker thread in `main()`

Add before the polling loop:

```python
threading.Thread(target=_queue_worker, daemon=True, name="queue-worker").start()
```

### Changes to `process_update` in `tg_agent.py`

Replace `threading.Thread(target=route_and_reply, ...).start()` with:

```python
qsize_before = _request_queue.qsize()
_request_queue.put((prompt_text or "", file_path))
if _worker_busy.is_set() or qsize_before > 0:
    pos = qsize_before + 1
    tg_send(f"📋 В очереди (позиция {pos})")
```

`qsize_before` is captured before `put()` to get a stable count. The position shown may be off by one in rare races (worker dequeues between capture and put), which is acceptable — the notification is advisory.

### Changes to `route_and_reply` in `tg_agent.py`

**1. Add cancel button to placeholder; check cancel immediately after (commit point):**
```python
cancel_markup = kb([[("🛑 Отмена", "cancel_current")]])
ph = tg_send(f"⏳ {lbl} думает... (макс {timeout_mins} мин)", cancel_markup)
ph_id = ph["message_id"] if ph else None

# Cancel may have fired between dequeue and placeholder send.
# Edit the NEW placeholder and return. The cancel handler may concurrently
# edit the same placeholder — this produces a benign double-edit (Telegram
# returns 400 "message not modified" for the second call); tg_edit already
# handles 400 errors gracefully by logging them.
if _cancel_event.is_set():
    if ph_id:
        tg_edit(ph_id, "❌ Запрос отменён")
    _cancel_event.clear()
    return
_cancel_event.clear()  # Committed to this request — clear any stale cancel state
```

**2. Check `_cancel_event` in the while loop:**
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

**3. Check `_cancel_event` after `t.join()` and discard reply silently (cancel handler already edited the placeholder):**
```python
t.join()
if _cancel_event.is_set():
    return  # Cancel handler already edited placeholder to "❌ Запрос отменён"
reply = result_box[0] if result_box else "❌ Нет ответа"
```

### Cancel handler in `handle_callback` in `tg_agent.py`

The cancel handler is the single point responsible for editing the placeholder to "❌". `route_and_reply` just returns silently after `t.join()` if the cancel event is set.

```python
if data == "cancel_current":
    tg_answer_cb(cb_id, "❌ Отменяю...")
    agents.cancel_active_proc()
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
```

This avoids the double-edit problem: only the cancel handler edits the placeholder to "❌"; `route_and_reply` does not edit it on the cancel path.

### Changes to `agents.py`

**1. Add `_active_proc` and `cancel_active_proc()`:**

```python
_active_proc: "subprocess.Popen | None" = None

def cancel_active_proc() -> None:
    global _active_proc
    if _active_proc is not None and _active_proc.poll() is None:
        _active_proc.kill()
        try:
            _active_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    _active_proc = None
```

**2. Set/clear in `_run_subprocess`:**

```python
def _run_subprocess(cmd, timeout, cwd, env):
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

**3. Fix `_is_transient_error` guard (Linux only):**

Change `if timed_out or rc == 0:` to `if timed_out or rc <= 0:`.

On Linux, `proc.kill()` (SIGKILL) results in `returncode = -9`. The existing guard `rc == 0` would not catch this, allowing the killed subprocess to be incorrectly classified as a transient error and retried. The timeout path (`rc=-1, timed_out=True`) is already caught by the `timed_out` check and unaffected. This fix is Linux-specific; on Windows, killed subprocesses return a large positive exit code and would not be affected by this guard either way.

**4. Note on `_active_proc` thread safety:**

`_active_proc` is written by `_run_subprocess` (queue-worker thread) and read+written by `cancel_active_proc()` (callback-handler thread). Simple Python object assignments are atomic under the GIL, so no additional lock is required. This assumption holds for CPython and is acceptable for this use case.

---

## Data Flow

```
User message
    │
    ▼
process_update()
    │
    ├─ worker idle, queue empty? ──► queue.put() [no notification — picked up immediately]
    │
    └─ worker busy or queue non-empty? ──► queue.put() + tg_send("📋 В очереди (позиция N)")

_queue_worker() [persistent daemon thread]
    │
    └─ loop: queue.get()
           → if cancel_event set: clear + discard item + continue
           → else: set worker_busy (do NOT clear event yet)
           → route_and_reply(text, file_path)
           → clear worker_busy

route_and_reply()
    │
    ├─ send placeholder with 🛑 button
    ├─ check cancel_event (commit point): if set → edit placeholder "❌" + clear + return
    ├─ _cancel_event.clear()  ← cleared here, after commit
    ├─ spawn _worker thread → agent function → subprocess
    ├─ while loop: sleep(5) → check cancel_event → edit placeholder
    └─ after join: if cancel_event set → return silently (handler already edited)

Cancel button pressed
    │
    ├─ agents.cancel_active_proc() → SIGKILL subprocess
    ├─ _cancel_event.set()
    ├─ drain _request_queue
    └─ tg_edit(placeholder, "❌ Запрос отменён")  ← only edit on cancel path
```

---

## Edge Cases

| Scenario | Behaviour |
|---|---|
| Cancel before subprocess starts | `_active_proc` is None → `cancel_active_proc()` is a no-op. The subprocess starts, runs to completion (up to the full agent timeout), and its output is discarded by the post-join check. Known limitation: subprocess consumes resources until timeout. |
| Cancel after subprocess finishes but before reply sent | `_cancel_event` set → `route_and_reply` returns silently after join; handler already edited placeholder |
| Cancel with empty queue | Draining empty queue is a no-op |
| Two cancel presses | Second press: `_active_proc` is None, queue empty → no-op. Known limitation: if first discard-cycle clears the event before the second cancel sets it, a legitimate message arriving next may be dequeued and discarded by the worker's event check. This is an accepted edge case. |
| Item dequeued by worker mid-cancel | Worker checks `_cancel_event` after dequeue; if set, discards item and clears event |
| Queue position shown is off by one | Race between `qsize_before` capture and worker dequeue is accepted; position is advisory |

---

## Files Changed

| File | Changes |
|---|---|
| `tg_agent.py` | Add `_request_queue`, `_cancel_event`, `_worker_busy`; add `_queue_worker()`; modify `process_update`, `route_and_reply`, `handle_callback`; start worker thread in `main()` |
| `agents.py` | Add `_active_proc`, `cancel_active_proc()`; modify `_run_subprocess`; fix `_is_transient_error` guard (`rc <= 0`) |

---

## What Does Not Change

- `/retry` command and retry button — unchanged
- `send_to_agent()` — unchanged, bypasses queue (used by retry)
- Gemini fallback, rate-limit detection, context archiving — unchanged
- Heartbeat while loop interval (5s) — unchanged
- All existing tests — must pass without modification
