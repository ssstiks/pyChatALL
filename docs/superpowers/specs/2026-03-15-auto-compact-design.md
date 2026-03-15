# Auto-Compact Claude Session — Design Spec

**Date:** 2026-03-15
**Status:** Approved
**Scope:** Automatically compact Claude's conversation context at the warning threshold, avoiding full session resets. Gemini and Qwen are unaffected.

---

## Problem

When a Claude session exceeds `ctx_archive` chars, the bot hard-resets: deletes the session file and starts fresh. All conversation context is lost. This is jarring for long conversations.

Claude CLI supports `/compact` non-interactively (`claude --print --resume <session_id> /compact`), which compresses the session in-place without losing the session ID.

---

## Goals

- Compact Claude's session automatically when it hits `ctx_warn`, before it reaches `ctx_archive`.
- On compact success: reset the ctx counter to 0 and notify the user.
- On compact failure: log a warning, suppress retries for this session, continue — the archive threshold will handle it later.
- Gemini and Qwen: no change to existing behavior.

---

## Out of Scope

- Compact for Gemini or Qwen (no CLI equivalent).
- Changing the hard-reset behavior at `ctx_archive`.
- User-triggered `/compact` command.

---

## Architecture

### Module-level guard: `_compact_attempted` in `agents.py`

```python
_compact_attempted: set[str] = set()
```

A set of session IDs for which a compact was attempted in this process lifetime. Prevents retrying compact on every message after a failure. Added once, near other module-level state.

### New helper: `_compact_claude_session()` in `agents.py`

Placed after `_run_subprocess` / `cancel_active_proc`, before `_NON_RETRYABLE_MARKERS`. Uses `log_warn` and `log_info` from the module-level `from logger import ...` import already present in `agents.py`.

```python
def _compact_claude_session(binary: str, session_file: str, env: dict) -> bool:
    """Run /compact on the active Claude session. Returns True on success.

    Parses compact output via _parse_cli_output to refresh the session ID
    (Claude may rotate the session ID after compaction).
    """
    global _compact_attempted
    sid = _load_session(session_file)
    if not sid:
        return False
    cmd = [binary, "--print", "--dangerously-skip-permissions",
           "--output-format", "json", "--resume", sid, "/compact"]
    stdout, stderr, rc, timed_out = _run_subprocess(cmd, timeout=120, cwd=WORK_DIR, env=env)
    _compact_attempted.add(sid)
    if timed_out or rc != 0:
        log_warn(f"Claude compact failed: rc={rc} stderr={stderr.strip()[:200]}")
        return False
    # Call _parse_cli_output purely to persist any updated session_id Claude returns
    # after compaction. rc=0 is the authoritative success signal per Claude CLI contract.
    # The return value is intentionally ignored.
    _parse_cli_output(stdout, session_file)
    log_info("Claude session compacted successfully")
    return True
```

### Changes to ctx check block in `_run_cli()` in `agents.py`

The existing ctx check block is at the bottom of the `try:` block in `_run_cli()`, after `reply = _parse_cli_output(...)` and `_add_ctx(...)`. The `env` dict, `binary`, `session_file`, and `ctx_file` are all in scope at that point. `from ui import tg_send` is the lazy import already at the top of `_run_cli()` and covers the new calls.

Replace the existing `elif total >= ctx_warn:` branch:

```python
total = _add_ctx(ctx_file, len(full_prompt) + len(reply))
ctx_warn, ctx_archive = CTX_LIMITS.get(agent_key, (150_000, 400_000))
if total >= ctx_archive:
    # hard reset — unchanged
    ts = time.strftime("%Y%m%d_%H%M%S")
    with open(f"{ARCHIVE_DIR}/{agent_name.lower()}_{ts}.txt", "w") as f:
        f.write(f"session={_load_session(session_file)}\ntotal={total}\n")
    _reset_session(session_file, ctx_file)
    log_warn(f"{agent_name} context archived at {total // 1000}k chars")
    tg_send(f"🗄 {agent_name}: контекст {total // 1000}k символов архивирован. Новая сессия.")
elif total >= ctx_warn:
    log_warn(f"{agent_name} context warning: {total // 1000}k/{ctx_archive // 1000}k")
    if is_claude:
        sid = _load_session(session_file)
        if sid and sid not in _compact_attempted:
            if _compact_claude_session(binary, session_file, env):
                # Reset ctx counter to 0 after successful compact
                with open(ctx_file, "w") as f:
                    f.write("0")
                tg_send("🗜 Claude: контекст сжат, продолжаем")
            else:
                tg_send(f"⚠️ Claude: контекст {total // 1000}k/{ctx_archive // 1000}k симв. (сжатие не удалось)")
        # If sid is None or already attempted, fall through silently — archive will handle it
    else:
        tg_send(f"⚠️ {agent_name}: контекст {total // 1000}k/{ctx_archive // 1000}k симв.")
```

**Counter reset:** After successful compact, write `"0"` directly to `ctx_file`. Do not use `_add_ctx` for this — writing directly avoids the read-then-write race that `_add_ctx` subtraction would introduce.

**Retry suppression:** The `sid not in _compact_attempted` guard prevents re-attempting compact on every subsequent message after a failure. `_compact_attempted.add(sid)` is called inside `_compact_claude_session()` on every attempt (success or failure). On session reset (`_reset_session`), the old session ID is naturally invalidated — the next session gets a new ID not in the set.

---

## Data Flow

```
_run_cli() after each reply
    │
    ├─ total >= ctx_archive? ──► hard reset (all agents, unchanged)
    │
    └─ total >= ctx_warn?
           │
           ├─ is_claude?
           │      │
           │      └─ sid not in _compact_attempted?
           │              yes → _compact_claude_session()
           │                        success → write "0" to ctx_file
           │                                → tg_send "🗜 Claude: контекст сжат"
           │                        failure → tg_send "⚠️ сжатие не удалось"
           │              no  → (silent, archive threshold handles it)
           │
           └─ not claude ──► tg_send "⚠️ {agent}: контекст Xk" (unchanged)
```

---

## Files Changed

| File | Changes |
|---|---|
| `agents.py` | Add `_compact_attempted` set; add `_compact_claude_session()`; modify `elif total >= ctx_warn:` in `_run_cli()` |

---

## What Does Not Change

- `_reset_session()` — unchanged
- `ctx_archive` hard-reset path — unchanged for all agents
- Gemini/Qwen ctx warning behavior — unchanged
- All existing tests — must pass without modification
