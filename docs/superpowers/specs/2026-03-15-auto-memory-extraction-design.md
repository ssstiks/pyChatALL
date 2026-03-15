# Auto-Memory Extraction — Design Spec

**Date:** 2026-03-15
**Status:** Approved
**Scope:** Automatically extract long-term facts from the shared conversation log when any agent hits `ctx_warn`. Runs in a background thread with zero latency impact on the user.

---

## Problem

Memory is purely manual: users must type `/remember <fact>` to save anything. Important facts from long conversations are silently lost when sessions are archived. Auto-extraction runs at the natural compaction checkpoint (`ctx_warn`) and saves durable facts without user intervention.

---

## Goals

- Extract facts automatically at `ctx_warn` for any agent.
- Run in a background daemon thread — no latency added to the current reply.
- Use Claude Haiku for extraction — cheap, fast, stateless call.
- Prevent duplicate extractions: at most once per `(agent_key, session_id)` pair.
- Prevent concurrent extractions: at most one extraction running at a time.
- Notify the user with a count of facts extracted (silent if zero).

---

## Out of Scope

- Deduplication of extracted facts against existing memory (let memory accumulate; user can `/forget` to clear).
- Extraction on demand (`/extract` command).
- Extraction for the full `shared_ctx` history — only the last 20 messages are sent.
- Changing `/remember`, `/memory`, or `/forget` behavior.

---

## Architecture

### Module-level state in `agents.py`

```python
import threading  # already imported

_extraction_attempted: set[tuple[str, str]] = set()  # (agent_key, session_id)
_extraction_lock = threading.Lock()
```

`_extraction_attempted` prevents re-running extraction on every message after `ctx_warn` is first crossed for a given session. `_extraction_lock` prevents two concurrent extractions if multiple agents hit `ctx_warn` close together — `Lock.acquire(blocking=False)` is atomic under CPython's GIL, avoiding the TOCTOU race that `Event.is_set()` + `Event.set()` would introduce.

### New function: `_extract_memory_async()` in `agents.py`

Called in a background daemon thread. Uses `log_warn`/`log_info` from the module-level `from logger import ...` import already present in `agents.py`. `from ui import tg_send` is used lazily inside the function (same pattern as `_run_cli`).

```python
def _extract_memory_async(ctx_snapshot: list[dict], agent_key: str, session_id: str) -> None:
    """Background thread: extract memorable facts from shared_ctx via Claude Haiku."""
    import json
    from ui import tg_send

    if not _extraction_lock.acquire(blocking=False):
        log_warn("Memory extraction already in progress, skipping")
        return
    try:
        # Format last 20 messages as plain text
        messages = ctx_snapshot[-20:]
        lines = []
        for m in messages:
            role = m.get("role", "")
            agent = m.get("agent", "")
            content = m.get("content", "")
            label = f"[{agent}]" if agent else f"[{role}]"
            lines.append(f"{label}: {content}")
        conversation = "\n".join(lines)

        prompt = (
            "Review this conversation and extract facts worth remembering long-term "
            "(user preferences, project names, key decisions, personal details). "
            "Return one fact per line. No bullets, no headers, no numbering. "
            "If nothing is worth remembering, return nothing.\n\n"
            f"{conversation}"
        )

        cmd = [
            CLAUDE_BIN, "--print", "--dangerously-skip-permissions",
            "--output-format", "json",
            "--model", "claude-haiku-4-5-20251001",
            prompt,
        ]
        stdout, stderr, rc, timed_out = _run_subprocess(cmd, timeout=60, cwd=WORK_DIR, env=os.environ.copy())

        if timed_out or rc != 0:
            log_warn(f"Memory extraction failed: rc={rc} stderr={stderr.strip()[:200]}")
            return

        # Parse JSON directly — do NOT call _parse_cli_output (it requires a real
        # session_file path and would overwrite the Claude agent's session with the
        # Haiku extraction call's session ID).
        try:
            data = json.loads(stdout)
            reply = data.get("result", "") or ""
        except (json.JSONDecodeError, AttributeError):
            log_warn(f"Memory extraction: could not parse Haiku output: {stdout[:200]}")
            return
        facts = [line.strip() for line in reply.splitlines() if line.strip()]

        for fact in facts:
            memory_add(fact)

        if facts:
            log_info(f"Memory extraction: saved {len(facts)} facts")
            tg_send(f"🧠 Память: извлечено {len(facts)} фактов")
        else:
            log_info("Memory extraction: nothing worth saving")
    finally:
        _extraction_lock.release()
```

**Note on `CLAUDE_BIN`:** Already a module-level constant in `agents.py` imported from `config`. Haiku model ID `claude-haiku-4-5-20251001` is the current Haiku model per project conventions.

### Trigger in `_run_cli()` in `agents.py`

Added inside the `elif total >= ctx_warn:` block, for all agents. Must run **after** the compact logic (so compact can reset the ctx counter first if it fires).

```python
elif total >= ctx_warn:
    log_warn(f"{agent_name} context warning: {total // 1000}k/{ctx_archive // 1000}k")
    if is_claude:
        # ... compact logic (Sub-project C) ...
        pass

    # Auto-memory extraction — all agents
    sid = _load_session(session_file) or ""
    extract_key = (agent_key, sid)
    if extract_key not in _extraction_attempted:
        _extraction_attempted.add(extract_key)
        ctx_snapshot = shared_ctx_load()
        threading.Thread(
            target=_extract_memory_async,
            args=(ctx_snapshot, agent_key, sid),
            daemon=True,
            name="memory-extractor",
        ).start()

    # Warning message for non-Claude (Claude sends compact success/fail message instead)
    if not is_claude:
        tg_send(f"⚠️ {agent_name}: контекст {total // 1000}k/{ctx_archive // 1000}k симв.")
```

**Import:** `threading` is already imported in `agents.py`. `shared_ctx_load` and `memory_add` are defined in `context.py` but are **not** currently imported in `agents.py` — the implementer must add them to the `from context import (...)` block in `agents.py`.

---

## Data Flow

```
_run_cli() after each reply
    │
    └─ total >= ctx_warn?
           │
           ├─ is_claude? ──► compact logic (Sub-project C)
           │
           └─ (agent_key, sid) not in _extraction_attempted?
                  yes → _extraction_attempted.add(key)
                      → threading.Thread(_extract_memory_async, ctx_snapshot, ...)
                  no  → skip (already extracted for this session)
```

```
_extract_memory_async() [daemon thread]
    │
    ├─ _extraction_lock.acquire(blocking=False)? no ──► return (concurrent guard)
    ├─ (lock acquired)
    ├─ format last 20 messages
    ├─ build extraction prompt
    ├─ _run_subprocess(claude haiku, no --resume)
    ├─ rc != 0 or timed_out? ──► log_warn + return
    ├─ json.loads(stdout) → data.get("result") → reply
    ├─ split reply into facts
    ├─ memory_add(fact) for each
    ├─ tg_send("🧠 Память: извлечено N фактов") if facts
    └─ _extraction_lock.release()
```

---

## Edge Cases

| Scenario | Behaviour |
|---|---|
| Two agents hit ctx_warn simultaneously | `_extraction_lock.acquire(blocking=False)` is atomic; second agent's thread fails to acquire lock and returns immediately |
| Haiku call fails | Log warn, `_extraction_lock` cleared in `finally`, no retry |
| Extraction returns empty reply | No facts added, no tg_send, log info |
| Session is reset after extraction | New session gets a new session_id; `(agent_key, new_sid)` is not in `_extraction_attempted` → extraction fires again next time ctx_warn is hit |
| Haiku JSON parsing fails | `json.JSONDecodeError` caught, logged, lock released — no crash |

---

## Files Changed

| File | Changes |
|---|---|
| `agents.py` | Add `_extraction_attempted`, `_extraction_lock`; add `_extract_memory_async()`; add trigger in `elif total >= ctx_warn:` block in `_run_cli()` |

---

## What Does Not Change

- `memory_add()`, `memory_load()`, `memory_clear()` — unchanged
- `/remember`, `/memory`, `/forget` commands — unchanged
- All existing tests — must pass without modification
