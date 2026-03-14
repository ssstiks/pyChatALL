# Request Lifecycle: Task Queue + Cancel Button — Design Spec

**Date:** 2026-03-14
**Status:** Approved
**Scope:** Task queue (serialize concurrent requests) + cancel button (kill active request + drain queue). Heartbeat and typing indicator are already implemented; this spec does not change them.

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
        _cancel_event.clear()
        _worker_busy.set()
        text, file_path = item
        try:
            route_and_reply(text, file_path)
        finally:
            _worker_busy.clear()
            _request_queue.task_done()
```

### Changes to `process_update` in `tg_agent.py`

Replace `threading.Thread(target=route_and_reply, ...).start()` with:

```python
_request_queue.put((prompt_text or "", file_path))
if _worker_busy.is_set() or _request_queue.qsize() > 1:
    pos = _request_queue.qsize()
    tg_send(f"📋 В очереди (позиция {pos})")
```

### Changes to `route_and_reply` in `tg_agent.py`

**1. Add cancel button to placeholder:**
```python
cancel_markup = kb([[("🛑 Отмена", "cancel_current")]])
ph = tg_send(f"⏳ {lbl} думает... (макс {timeout_mins} мин)", cancel_markup)
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

**3. Check `_cancel_event` after `t.join()` and discard reply if set:**
```python
t.join()
if _cancel_event.is_set():
    if ph_id:
        tg_edit(ph_id, "❌ Запрос отменён")
    return
reply = result_box[0] if result_box else "❌ Нет ответа"
```

### Cancel handler in `handle_callback` in `tg_agent.py`

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

**3. Fix `_is_transient_error` guard:**

Change `if timed_out or rc == 0:` to `if timed_out or rc <= 0:`.

This prevents a SIGKILL'd process (returncode = -9 on Linux) from being incorrectly classified as a transient error and retried.

---

## Data Flow

```
User message
    │
    ▼
process_update()
    │
    ├─ worker idle? ──yes──► queue.put() [picked up immediately, no notification]
    │
    └─ worker busy? ──yes──► queue.put() + tg_send("📋 В очереди (позиция N)")

_queue_worker() [persistent daemon thread]
    │
    └─ loop: queue.get() → clear cancel_event → set worker_busy
           → route_and_reply(text, file_path)
           → clear worker_busy

route_and_reply()
    │
    ├─ send placeholder with 🛑 button
    ├─ spawn _worker thread → agent function → subprocess
    ├─ while loop: sleep(5) → check cancel_event → edit placeholder
    └─ after join: check cancel_event → discard reply if set

Cancel button pressed
    │
    ├─ agents.cancel_active_proc() → SIGKILL subprocess
    ├─ _cancel_event.set()
    ├─ drain _request_queue
    └─ edit placeholder → "❌ Запрос отменён"
```

---

## Edge Cases

| Scenario | Behaviour |
|---|---|
| Cancel before subprocess starts | `_active_proc` is None → no kill; `_cancel_event` set → while loop breaks, reply discarded |
| Cancel after subprocess finishes but before reply sent | `_cancel_event` check after `t.join()` discards reply |
| Cancel with empty queue | Draining empty queue is a no-op |
| Two cancel presses | Second press: `_active_proc` is None (already killed), event already set → no-op |
| Queue fills while cancelling | Queue is drained by cancel handler before new items can be processed |

---

## Files Changed

| File | Changes |
|---|---|
| `tg_agent.py` | Add `_request_queue`, `_cancel_event`, `_worker_busy`; add `_queue_worker()`; modify `process_update`, `route_and_reply`, `handle_callback`; start worker thread in `main()` |
| `agents.py` | Add `_active_proc`, `cancel_active_proc()`; modify `_run_subprocess`; fix `_is_transient_error` guard |

---

## What Does Not Change

- `/retry` command and retry button — unchanged
- `send_to_agent()` — unchanged, bypasses queue (used by retry)
- Gemini fallback, rate-limit detection, context archiving — unchanged
- Heartbeat while loop interval (5s) — unchanged
- All existing tests — must pass without modification
