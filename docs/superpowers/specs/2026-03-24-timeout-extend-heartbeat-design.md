# Timeout Extension & Heartbeat Design

**Date:** 2026-03-24
**Feature:** Extend/remove timeout button + adaptive heartbeat updates during agent processing

## Overview

Two UX problems solved together:
1. **Timeout kills useful work** — user can extend deadline (+5 min per press) or remove it entirely (∞)
2. **No feedback during long tasks** — placeholder message updated every 30-60s with elapsed/remaining time

## Architecture

All changes are in `tg_agent.py`. No new modules. The existing `t.join(timeout_secs)` call in `route_and_reply()` is replaced by a **5-second polling loop**.

```
route_and_reply() [_queue_worker thread]
    ↓
spawn _worker thread
send placeholder with 3-button keyboard
    ↓
polling loop (5s ticks):
    ├─ thread done?              → exit loop (success)
    ├─ _cancel_event?            → kill + exit
    ├─ _timeout_extend_count>0?  → deadline += 300 * count, update message
    ├─ _no_timeout_event?        → deadline = inf, update message + keyboard
    ├─ now >= deadline?          → kill + exit (timeout)
    └─ heartbeat tick?           → tg_edit placeholder with elapsed/remaining
    ↓
read result_box[0]  (thread is already done)
```

## Components

### New module-level state (`tg_agent.py`)

Added next to existing `_cancel_event` and `_worker_busy`:

```python
_timeout_extend_count = 0               # incremented per "+5 мин" press
_timeout_extend_lock  = threading.Lock()
_no_timeout_event     = threading.Event()
```

**Why a counter instead of Event for extend:** If the user presses "+5 мин" twice before the next 5s tick, both presses are honored. `threading.Event` would collapse them into one.

### Keyboards

```python
def _agent_kb_full() -> dict:
    """3-button keyboard: extend, no-limit, cancel."""
    return kb([[
        ("⏳ +5 мин",    "extend_timeout"),
        ("∞ Без лимита", "no_timeout"),
        ("🛑 Отмена",     "cancel_current"),
    ]])

def _agent_kb_cancel_only() -> dict:
    """1-button keyboard after no-limit pressed."""
    return kb([[("🛑 Отмена", "cancel_current")]])
```

### New callback handlers (`handle_callback`)

```python
elif data == "extend_timeout":
    if _worker_busy.is_set():          # guard: only if request is active
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

### `_placeholder_text(lbl, elapsed, remaining, no_limit) -> str`

Single function used for both the **initial send** and all **heartbeat edits**.

```python
def _placeholder_text(lbl: str, elapsed: float,
                      remaining: int | None, no_limit: bool = False) -> str:
    e_min, e_sec = divmod(int(elapsed), 60)
    e_str = f"{e_min}м {e_sec:02d}с" if e_min else f"{e_sec}с"
    if no_limit:
        return f"⏳ {lbl} думает... {e_str} (без лимита)"
    if remaining is not None:
        r_min, r_sec = divmod(remaining, 60)
        r_str = f"{r_min}м {r_sec:02d}с" if r_min else f"{r_sec}с"
        return f"⏳ {lbl} думает... {e_str} / осталось {r_str}"
    return f"⏳ {lbl} думает... {e_str}"
```

### `_edit_placeholder(ph_id, lbl, elapsed, remaining, no_limit) -> None`

Updates the placeholder message. Derives keyboard from `no_limit`. Silently ignores errors (message deleted, too old, etc.).

```python
def _edit_placeholder(ph_id, lbl, elapsed, remaining, no_limit):
    if ph_id is None:
        return
    markup = _agent_kb_cancel_only() if no_limit else _agent_kb_full()
    text   = _placeholder_text(lbl, elapsed, remaining, no_limit)
    try:
        tg_edit(ph_id, text=text, markup=markup)
    except Exception:
        pass
```

### Polling loop (replaces `t.join(timeout_secs)` in `route_and_reply`)

**Initial send** uses `_placeholder_text` from the start (consistent format):

```python
start_time = time.time()
remaining0 = timeout_secs
ph = tg_send(
    _placeholder_text(lbl, elapsed=0, remaining=remaining0),
    _agent_kb_full()
)
ph_id    = ph["message_id"] if ph else None
no_limit = False
last_hb  = start_time
deadline = start_time + timeout_secs
```

**`tg_typing()` is removed from the loop** — the heartbeat text edits serve the same purpose (user sees the message timestamp update). This is intentional.

**Loop:**

```python
POLL_INTERVAL = 5
HB_FAST_SECS  = 180
HB_FAST_EVERY = 30
HB_SLOW_EVERY = 60

while True:
    t.join(timeout=POLL_INTERVAL)

    if not t.is_alive():
        break  # normal completion — result_box already populated, no join needed

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
        break

    # 5. Heartbeat
    hb_interval = HB_FAST_EVERY if elapsed < HB_FAST_SECS else HB_SLOW_EVERY
    if now - last_hb >= hb_interval:
        last_hb   = now
        remaining = None if no_limit else max(0, int(deadline - now))
        _edit_placeholder(ph_id, lbl, elapsed, remaining, no_limit)
```

**After the loop**, the existing code reads `result_box[0]` — this is safe because:
- Normal completion: `t.is_alive()` was False at loop exit, `result_box` is fully written
- Cancel/timeout: `cancel_active_proc()` + `t.join(3)` ensures thread has stopped

### Event Cleanup

At the **start** of `route_and_reply()`, before spawning the worker:

```python
_cancel_event.clear()
_no_timeout_event.clear()
with _timeout_extend_lock:
    _timeout_extend_count = 0
```

**Stale event behavior:** If user presses "+5 мин" or "∞" after the request completes but before the next one starts, `handle_callback` guards via `_worker_busy.is_set()` — the counter/event is not modified. No stale state leaks to the next request.

## Data Flow Example

```
User sends "Объясни весь код"
    ↓
placeholder: "⏳ Claude думает... 0с / осталось 13м 20с"
             [⏳ +5 мин] [∞ Без лимита] [🛑 Отмена]
    ↓
30s: edit → "⏳ Claude думает... 30с / осталось 12м 50с"
60s: edit → "⏳ Claude думает... 1м 00с / осталось 12м 20с"
    ↓
User presses "+5 мин" at 2м:
    edit → "⏳ Claude думает... 2м 00с / осталось 16м 20с"
    ↓
User presses "+5 мин" again (2 presses, 1 tick):
    edit → "⏳ Claude думает... 2м 05с / осталось 21м 15с"
    ↓
User presses "∞" at 3м:
    edit → "⏳ Claude думает... 3м 00с (без лимита)"
           [🛑 Отмена]
    ↓
Claude replies at 4м 30с → normal reply shown
```

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `tg_edit` fails (message deleted) | Caught silently, polling continues |
| "+5 мин" pressed after completion | `_worker_busy` guard prevents increment |
| "∞" pressed after completion | `_worker_busy` guard prevents set |
| Multiple "+5 мин" in one tick | Counter accumulates, all honored |
| Cancel + extend in same tick | Cancel checked first, takes priority |
| Worker finishes during `t.join(POLL_INTERVAL)` | `t.is_alive()` is False, loop exits immediately |

## Testing

- `test_polling_normal_completion` — worker finishes before deadline → no timeout, result returned
- `test_polling_timeout` — deadline reached → `cancel_active_proc` called, timeout message
- `test_polling_extend_once` — `_timeout_extend_count = 1` → deadline += 300
- `test_polling_extend_double` — `_timeout_extend_count = 2` → deadline += 600
- `test_polling_no_limit` — `_no_timeout_event` set → deadline = inf, keyboard changes
- `test_polling_cancel_priority` — cancel + extend both set → cancel wins
- `test_placeholder_text_formats` — assert correct strings at 0s, 30s, 90s, no_limit
- `test_heartbeat_adaptive` — verify tg_edit called at 30s intervals before 180s, 60s after
- `test_stale_event_guard` — `_worker_busy` clear → callback does not modify state
