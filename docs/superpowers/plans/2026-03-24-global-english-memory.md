# Global English Memory Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current text-based shared_context + memory.md system with a structured JSON English memory that injects concise project/user context into every agent prompt, updated in background after each reply.

**Architecture:** `memory_manager.py` owns the JSON memory file with thread-safe read/write. After every bot reply, a daemon thread ("Shadow Librarian") sends the last exchange to Qwen/Gemini-Flash-Lite and updates the JSON. The English memory JSON is injected at the top of every agent prompt via `global_ctx_for_prompt(skip_recent)`, keeping only the last 2-3 raw Russian messages for fresh turns. Claude with an active session uses `skip_recent=True` to avoid context duplication with its own CLI session history.

**Tech Stack:** Python 3.10+, threading.Thread (NOT asyncio — project uses thread model), subprocess CLI (claude/gemini/qwen), JSON, threading.Lock, existing _run_cli in agents.py.

**Out of scope:** OpenRouter `shared_ctx_for_api()` path (uses OpenAI message-list format, separate follow-up task).

---

## Key Project Facts (from codebase analysis)

- `STATE_DIR` in config.py is a plain `str` (`"/tmp/tg_agent"`), NOT a `pathlib.Path` — use `f"{STATE_DIR}/..."` not `/` operator
- `SHARED_CTX_FILE` in context.py is also a plain `str` — use `os.path.exists()`, not `.exists()`
- In `route_and_reply()` (tg_agent.py), the local variable holding the user prompt is `prompt` (not `prompt_text`)
- `agents.py` Claude branch: when session exists, only memory is injected (not shared context) to avoid duplication with CLI session history — must preserve this via `skip_recent=True`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| **CREATE** | `memory_manager.py` | JSON schema, thread-safe load/save, background update, thread-safe singleton |
| **MODIFY** | `config.py` | Add `GLOBAL_MEMORY_FILE` constant (string-safe) |
| **MODIFY** | `context.py` | New `global_ctx_for_prompt(skip_recent)` — inject English JSON + optional last 3 RU msgs |
| **MODIFY** | `agents.py` | Use `global_ctx_for_prompt()` with `skip_recent` for Claude session case |
| **MODIFY** | `tg_agent.py` | Trigger shadow librarian after reply using correct variable `prompt` |
| **CREATE** | `tests/test_memory_manager.py` | Unit tests for MemoryManager |
| **CREATE** | `migrate_memory.py` | One-shot script: convert old memory.md → new JSON |

---

## Task 1: Add config constant

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Read config.py to find STATE_DIR definition and last file constant**

```bash
cd /home/stx/Applications/progect/pyChatALL && grep -n "STATE_DIR\|SETUP_DONE\|ARCHIVE" config.py | head -15
```

- [ ] **Step 2: Add GLOBAL_MEMORY_FILE after SETUP_DONE_FILE using f-string (STATE_DIR is a plain str)**

Find:
```python
SETUP_DONE_FILE = f"{STATE_DIR}/setup_done.txt"
```
Add after it:
```python
GLOBAL_MEMORY_FILE = f"{STATE_DIR}/global_memory.json"
```

If SETUP_DONE_FILE uses a different format (e.g., `os.path.join`), match the same style.

- [ ] **Step 3: Verify — must print path, not raise TypeError**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -c "import config; print(config.GLOBAL_MEMORY_FILE)"
```
Expected: `/tmp/tg_agent/global_memory.json`

- [ ] **Step 4: Commit**

```bash
git add config.py
git commit -m "feat: add GLOBAL_MEMORY_FILE constant to config"
```

---

## Task 2: Create memory_manager.py

**Files:**
- Create: `memory_manager.py`
- Create: `tests/test_memory_manager.py`

- [ ] **Step 1: Write the failing tests first**

```python
# tests/test_memory_manager.py
import json
import re
import threading
import time
import pathlib
import pytest
from unittest.mock import patch

# ── helpers ────────────────────────────────────────────────────────────────
def _tmp_mm(tmp_path):
    from memory_manager import MemoryManager
    return MemoryManager(tmp_path / "global_memory.json")

# ── schema tests ───────────────────────────────────────────────────────────
def test_load_returns_defaults_when_file_missing(tmp_path):
    mm = _tmp_mm(tmp_path)
    mem = mm.load()
    assert "user_profile" in mem
    assert "project_state" in mem
    assert "short_term_context" in mem

def test_save_and_reload(tmp_path):
    mm = _tmp_mm(tmp_path)
    mm.save({
        "user_profile": {"os": "Arch", "tools": [], "preferences": []},
        "project_state": {"current_goal": "build bot", "milestones": [], "last_technical_decision": ""},
        "short_term_context": "User builds Telegram bot with multiple AI agents."
    })
    mem = mm.load()
    assert mem["user_profile"]["os"] == "Arch"
    assert mem["short_term_context"] == "User builds Telegram bot with multiple AI agents."

def test_to_prompt_block(tmp_path):
    mm = _tmp_mm(tmp_path)
    block = mm.to_prompt_block()
    assert block.startswith("[MEMORY:")
    assert "user_profile" in block

def test_thread_safety(tmp_path):
    mm = _tmp_mm(tmp_path)
    errors = []

    def worker(i):
        try:
            mem = mm.load()
            mem["short_term_context"] = f"Thread {i} ran."
            mm.save(mem)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == [], f"Thread safety errors: {errors}"

def test_update_background_is_nonblocking_and_calls_agent(tmp_path):
    """update_background must return immediately AND the agent must be called."""
    mm = _tmp_mm(tmp_path)
    called = threading.Event()

    def fake_agent(prompt: str) -> str:
        called.set()
        return '{"user_profile":{"os":"","tools":[],"preferences":[]},"project_state":{"current_goal":"","milestones":[],"last_technical_decision":""},"short_term_context":"Test ran."}'

    # patch must stay active until called.wait() returns — thread may not have
    # reached _run_updater_agent by the time update_background() returns
    with patch("memory_manager._run_updater_agent", side_effect=fake_agent):
        t0 = time.monotonic()
        mm.update_background("user: test", "assistant: ok")
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"update_background blocked for {elapsed:.3f}s"
        # Wait up to 3s for background thread to call the agent (inside patch ctx)
        assert called.wait(timeout=3.0), "Shadow Librarian never called _run_updater_agent"
    # Memory assertion outside patch — file was already written
    assert mm.load()["short_term_context"] == "Test ran."

def test_get_memory_manager_singleton_is_thread_safe():
    """Multiple threads calling get_memory_manager() must all get the same instance."""
    from memory_manager import get_memory_manager
    instances = []
    def grab():
        instances.append(get_memory_manager())
    threads = [threading.Thread(target=grab) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(set(id(i) for i in instances)) == 1, "Singleton created multiple instances"
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -m pytest tests/test_memory_manager.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'memory_manager'`

- [ ] **Step 3: Create memory_manager.py**

```python
"""
memory_manager.py — Global English Memory Layer
Thread-safe JSON memory that tracks user profile, project state, and short-term context.
Updated in background after every bot reply (Shadow Librarian pattern).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
import threading
from typing import Any

log = logging.getLogger(__name__)

# ── Default schema ──────────────────────────────────────────────────────────
_EMPTY: dict[str, Any] = {
    "user_profile": {
        "os": "",
        "tools": [],
        "preferences": [],
    },
    "project_state": {
        "current_goal": "",
        "milestones": [],
        "last_technical_decision": "",
    },
    "short_term_context": "",
}

# ── Shadow Librarian prompt ─────────────────────────────────────────────────
_UPDATE_PROMPT_TEMPLATE = """\
You are a technical memory curator. Given the CURRENT MEMORY JSON and a new \
[User/Assistant] exchange (in Russian), update the JSON fields:

Rules:
- short_term_context: 3-5 sentence ENGLISH technical summary of the LATEST exchange. \
Dense, no filler. Example: "User debugged auth.py. Fixed JWT expiry bug. Switched to HS256."
- user_profile.os / tools / preferences: update ONLY if new fact found. Keep existing.
- project_state.current_goal: update if user's goal changed.
- project_state.milestones: append completed steps as short English phrases.
- project_state.last_technical_decision: last architectural/library choice made.
- Remove conversational filler. Use shorthand: "Refactored X" not "The user has refactored X".
- Output ONLY valid JSON matching the same schema. No prose, no markdown fences.

CURRENT MEMORY:
{current_memory}

NEW EXCHANGE:
{exchange}

OUTPUT (JSON only):"""


# ── Updater agent call ──────────────────────────────────────────────────────
def _run_updater_agent(prompt: str) -> str:
    """
    Calls a cheap CLI agent (qwen or gemini flash) to update the memory JSON.
    Checks binary exists on disk before queuing. Returns stdout or "".
    """
    try:
        from config import QWEN_BIN, GEMINI_BIN
        qwen = str(QWEN_BIN) if QWEN_BIN and os.path.isfile(str(QWEN_BIN)) else None
        gemini = str(GEMINI_BIN) if GEMINI_BIN and os.path.isfile(str(GEMINI_BIN)) else None
    except ImportError:
        qwen = None
        gemini = None

    # Prefer qwen (fastest), fall back to gemini flash lite
    candidates: list[tuple[str, list[str]]] = []
    if qwen:
        candidates.append((qwen, ["--yolo", "--output-format", "json", "--model", "coder-model"]))
    if gemini:
        candidates.append((gemini, ["--yolo", "--output-format", "json",
                                     "--model", "gemini-2.5-flash-lite"]))

    if not candidates:
        log.debug("shadow librarian: no updater agent available")
        return ""

    for binary, flags in candidates:
        try:
            result = subprocess.run(
                [binary] + flags + [prompt],
                capture_output=True, text=True,
                timeout=30,
                start_new_session=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            log.debug("shadow librarian: agent %s rc=%d stderr=%s",
                      binary, result.returncode, result.stderr[:100])
        except Exception as e:
            log.debug("shadow librarian: agent %s error: %s", binary, e)
            continue

    return ""


def _parse_agent_json_response(raw: str) -> dict[str, Any] | None:
    """Extract JSON from agent CLI output (handles wrapped JSON format from CLI)."""
    # Try direct parse first
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Try to extract from CLI wrapper: {"type":"result","result":"..."}
    try:
        outer = json.loads(raw)
        if isinstance(outer, dict):
            content = outer.get("result") or outer.get("response") or ""
            if content:
                return json.loads(content)
        if isinstance(outer, list):
            for item in outer:
                if isinstance(item, dict):
                    content = item.get("result") or item.get("response") or ""
                    if content:
                        try:
                            return json.loads(content)
                        except Exception:
                            pass
    except Exception:
        pass
    # Fallback: regex extract JSON block
    match = re.search(r'\{[\s\S]+\}', raw)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return None


# ── Main class ──────────────────────────────────────────────────────────────
class MemoryManager:
    """
    Thread-safe global English memory for pyChatALL.

    Usage:
        mm = MemoryManager(GLOBAL_MEMORY_FILE)
        block = mm.to_prompt_block()               # inject into prompts
        mm.update_background(user_msg, asst_msg)   # call after reply
    """

    def __init__(self, path: pathlib.Path | str) -> None:
        self._path = pathlib.Path(path)
        self._lock = threading.Lock()

    # ── I/O ────────────────────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """Load memory JSON; return schema defaults if file missing or corrupt."""
        with self._lock:
            try:
                if self._path.exists():
                    data = json.loads(self._path.read_text(encoding="utf-8"))
                    merged = json.loads(json.dumps(_EMPTY))  # deep copy defaults
                    merged.update(data)
                    return merged
            except (json.JSONDecodeError, OSError) as e:
                log.warning("global_memory load error: %s — using defaults", e)
            return json.loads(json.dumps(_EMPTY))

    def save(self, mem: dict[str, Any]) -> None:
        """Atomically write memory JSON (write to .tmp, then rename for crash safety)."""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(self._path)
            except OSError as e:
                log.error("global_memory save error: %s", e)

    # ── Prompt injection ───────────────────────────────────────────────────

    def to_prompt_block(self) -> str:
        """
        Returns a compact English memory block for injection at top of agent prompts.
        Example: [MEMORY: {"user_profile":{...},"project_state":{...},"short_term_context":"..."}]
        """
        mem = self.load()
        compact = json.dumps(mem, ensure_ascii=False, separators=(",", ":"))
        return f"[MEMORY: {compact}]"

    # ── Shadow Librarian ───────────────────────────────────────────────────

    def update_background(self, user_msg: str, assistant_msg: str) -> None:
        """
        Spawn a daemon thread to update memory after a bot reply.
        Returns immediately — MUST NOT block the calling thread.
        """
        t = threading.Thread(
            target=self._do_update,
            args=(user_msg, assistant_msg),
            daemon=True,
            name="shadow-librarian",
        )
        t.start()

    def _do_update(self, user_msg: str, assistant_msg: str) -> None:
        """Background worker: call cheap agent, parse JSON, save updated memory."""
        try:
            current_mem = self.load()
            current_json = json.dumps(current_mem, ensure_ascii=False, indent=2)
            exchange = f"User: {user_msg}\nAssistant: {assistant_msg}"
            prompt = _UPDATE_PROMPT_TEMPLATE.format(
                current_memory=current_json,
                exchange=exchange,
            )
            raw = _run_updater_agent(prompt)
            if not raw:
                log.debug("shadow librarian: empty response, skipping update")
                return
            updated = _parse_agent_json_response(raw)
            if updated and isinstance(updated, dict):
                if all(k in updated for k in ("user_profile", "project_state", "short_term_context")):
                    self.save(updated)
                    log.info("shadow librarian: memory updated — %s",
                             updated.get("short_term_context", "")[:80])
                else:
                    log.warning("shadow librarian: invalid schema in response, skipping")
            else:
                log.debug("shadow librarian: could not parse JSON from response")
        except Exception as e:
            log.error("shadow librarian error: %s", e)


# ── Thread-safe singleton ───────────────────────────────────────────────────
_mm: MemoryManager | None = None
_mm_lock = threading.Lock()


def get_memory_manager() -> MemoryManager:
    """Return the module-level singleton MemoryManager. Thread-safe lazy init."""
    global _mm
    if _mm is None:
        with _mm_lock:
            if _mm is None:  # double-checked locking
                try:
                    from config import GLOBAL_MEMORY_FILE
                    _mm = MemoryManager(GLOBAL_MEMORY_FILE)
                except ImportError:
                    _mm = MemoryManager("/tmp/tg_agent/global_memory.json")
    return _mm
```

- [ ] **Step 4: Run tests — all 6 must pass**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -m pytest tests/test_memory_manager.py -v
```
Expected:
```
PASSED test_load_returns_defaults_when_file_missing
PASSED test_save_and_reload
PASSED test_to_prompt_block
PASSED test_thread_safety
PASSED test_update_background_is_nonblocking_and_calls_agent
PASSED test_get_memory_manager_singleton_is_thread_safe
```

- [ ] **Step 5: Syntax check**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -c "from memory_manager import get_memory_manager; mm = get_memory_manager(); print(mm.to_prompt_block()[:80])"
```
Expected: `[MEMORY: {"user_profile":...`

- [ ] **Step 6: Commit**

```bash
git add memory_manager.py tests/test_memory_manager.py
git commit -m "feat: add MemoryManager with thread-safe singleton and Shadow Librarian"
```

---

## Task 3: Add global_ctx_for_prompt() to context.py

**Files:**
- Modify: `context.py` (add one new function + one import)

**IMPORTANT:** `SHARED_CTX_FILE` in context.py is a plain `str` — use `os.path.exists()`, NOT `.exists()`.

New function accepts `skip_recent: bool = False` to support Claude's session dedup case.

- [ ] **Step 1: Check exact type of SHARED_CTX_FILE and imports in context.py**

```bash
cd /home/stx/Applications/progect/pyChatALL && grep -n "SHARED_CTX_FILE\|^import\|^from" context.py | head -20
```
Confirm `SHARED_CTX_FILE` is a `str`, and check whether `import os` and `import json` are already present.

- [ ] **Step 2: Add missing imports if needed (os, json)**

If `import os` is missing from context.py, add it with the other stdlib imports at the top.
`json` should already be present.

- [ ] **Step 3: Add import for MemoryManager after existing imports**

```python
from memory_manager import get_memory_manager
```

- [ ] **Step 4: Add global_ctx_for_prompt() after shared_ctx_for_prompt()**

```python
def global_ctx_for_prompt(skip_recent: bool = False) -> str:
    """
    Token-efficient context injection:
      [MEMORY: {JSON}]          ← English summary (~200-400 tokens)
      [Recent context (RU): ...]  ← Last 3 raw RU messages (skipped when skip_recent=True)

    skip_recent=True: use for Claude with active session (CLI already has session history,
    injecting recent messages would duplicate context and waste tokens).

    Replaces verbose shared_ctx_for_prompt() for CLI agents (Claude/Gemini/Qwen).
    OpenRouter API path continues to use shared_ctx_for_api() — out of scope.
    """
    parts: list[str] = []

    # 1. English memory JSON block (always included)
    mm = get_memory_manager()
    parts.append(mm.to_prompt_block())

    # 2. Last 3 raw Russian messages — skip for Claude with active session
    if not skip_recent:
        try:
            if os.path.exists(SHARED_CTX_FILE):
                with open(SHARED_CTX_FILE, encoding="utf-8") as f:
                    msgs = json.load(f)
                if isinstance(msgs, list) and msgs:
                    recent = msgs[-3:]
                    lines = []
                    for m in recent:
                        role = m.get("role", "?")
                        agent = m.get("agent", "")
                        content = str(m.get("content", ""))[:500]  # hard cap per message
                        label = f"{agent}({role})" if agent else role
                        lines.append(f"[{label}]: {content}")
                    if lines:
                        parts.append("[Recent context (RU):\n" + "\n".join(lines) + "]")
        except (json.JSONDecodeError, OSError):
            pass

    return "\n\n".join(parts)
```

- [ ] **Step 5: Verify syntax**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -c "from context import global_ctx_for_prompt; print(global_ctx_for_prompt()[:120])"
```
Expected: starts with `[MEMORY: {`

Also test skip_recent:
```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -c "
from context import global_ctx_for_prompt
full = global_ctx_for_prompt(skip_recent=False)
slim = global_ctx_for_prompt(skip_recent=True)
print('Full len:', len(full))
print('Slim len:', len(slim))
print('Slim has no Recent context:', 'Recent context' not in slim)
"
```
Expected: slim is shorter and does not contain "Recent context"

- [ ] **Step 6: Commit**

```bash
git add context.py
git commit -m "feat: add global_ctx_for_prompt(skip_recent) — English memory + optional RU turn context"
```

---

## Task 4: Wire global_ctx_for_prompt() into agents.py

**Files:**
- Modify: `agents.py` — the `_run_cli` function prompt-building block

**Claude session dedup rule:** When Claude has an active session (`sid` exists), use `skip_recent=True` because the CLI already carries full session history — injecting shared context would duplicate it.

- [ ] **Step 1: Find exact lines of the if-sid context block**

```bash
cd /home/stx/Applications/progect/pyChatALL && grep -n "sid\|ctx_text\|shared_ctx_for_prompt\|memory_load" agents.py | head -20
```
Find the block that looks like:
```python
if sid:
    ctx_text = memory_load() or ""
else:
    ctx_text = shared_ctx_for_prompt()
```

- [ ] **Step 2: Add global_ctx_for_prompt to the context.py import block in agents.py**

Find existing:
```python
from context import (
    ...
    shared_ctx_for_prompt,
    ...
)
```
Add `global_ctx_for_prompt` to this import.

- [ ] **Step 3: Replace the if-sid ctx block**

Old:
```python
        if sid:
            ctx_text = memory_load() or ""
        else:
            ctx_text = shared_ctx_for_prompt()
```

New:
```python
        # skip_recent=True when Claude has active session (CLI already has history)
        ctx_text = global_ctx_for_prompt(skip_recent=bool(sid))
```

- [ ] **Step 4: Verify syntax**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -c "import agents; print('OK')"
```
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add agents.py
git commit -m "feat: agents use global_ctx_for_prompt — skip_recent=True for Claude sessions"
```

---

## Task 5: Trigger Shadow Librarian after every reply in tg_agent.py

**Files:**
- Modify: `tg_agent.py`

**IMPORTANT:** At the `shared_ctx_add("assistant", ...)` call site in `route_and_reply()`, the user prompt variable is named `prompt` (not `prompt_text`). Verify before editing.

- [ ] **Step 1: Find the exact line and confirm variable name**

```bash
cd /home/stx/Applications/progect/pyChatALL && grep -n "shared_ctx_add.*assistant" tg_agent.py
```
Note the line number. Then check 5 lines above it:
```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -c "
lines = open('tg_agent.py').read().splitlines()
# find shared_ctx_add('assistant') line
for i, l in enumerate(lines):
    if 'shared_ctx_add' in l and 'assistant' in l:
        print('Line', i+1, ':', l)
        print('Context above:')
        for j in range(max(0,i-8), i):
            print('  ', j+1, ':', lines[j])
"
```
Confirm `prompt` is the correct variable name at that scope.

- [ ] **Step 2: Add import at top of tg_agent.py**

```python
from memory_manager import get_memory_manager
```

- [ ] **Step 3: Add shadow librarian trigger after shared_ctx_add line**

Find:
```python
    shared_ctx_add("assistant", reply, AGENT_NAMES[agent])
```
Add immediately after (use `prompt`, confirmed from Step 1):
```python
    shared_ctx_add("assistant", reply, AGENT_NAMES[agent])
    # Shadow Librarian: update English memory in background (non-blocking daemon thread)
    get_memory_manager().update_background(prompt, reply)
```

- [ ] **Step 4: Verify syntax**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -c "import tg_agent; print('OK')"
```
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tg_agent.py
git commit -m "feat: trigger shadow librarian after each reply in route_and_reply"
```

---

## Task 6: Migration script (old memory.md → new JSON)

**Files:**
- Create: `migrate_memory.py`

One-shot script to promote old `memory.md` bullet facts into `user_profile.preferences`.

- [ ] **Step 1: Create migrate_memory.py**

```python
#!/usr/bin/env python3
"""
migrate_memory.py — One-shot migration: memory.md → global_memory.json

Run once after deploying the new memory layer:
    python3 migrate_memory.py

Reads:  /tmp/tg_agent/memory.md  (old format: "- [date] fact" bullet points)
Writes: /tmp/tg_agent/global_memory.json
Safe: never overwrites existing global_memory.json data, only appends missing facts.
"""

import json
import re
import pathlib

OLD_FILE = pathlib.Path("/tmp/tg_agent/memory.md")
NEW_FILE = pathlib.Path("/tmp/tg_agent/global_memory.json")


def main() -> None:
    if not OLD_FILE.exists():
        print("No old memory.md found — nothing to migrate.")
        return

    old_facts: list[str] = []
    for line in OLD_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cleaned = re.sub(r'^[-*]\s*\[.*?\]\s*', '', line).strip()
            if cleaned:
                old_facts.append(cleaned)

    if not old_facts:
        print("memory.md is empty — nothing to migrate.")
        return

    # Load or init new JSON
    mem: dict = {}
    if NEW_FILE.exists():
        try:
            mem = json.loads(NEW_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            corrupt_backup = NEW_FILE.with_suffix(".corrupt.json")
            NEW_FILE.rename(corrupt_backup)
            print(f"Warning: existing global_memory.json was corrupt — saved as {corrupt_backup.name}, reinitializing.")

    # Merge: append facts not already present
    prefs: list = mem.get("user_profile", {}).get("preferences", [])
    added = 0
    for fact in old_facts:
        if fact not in prefs:
            prefs.append(fact)
            added += 1

    # Ensure full schema
    mem.setdefault("user_profile", {})
    mem["user_profile"]["preferences"] = prefs
    mem["user_profile"].setdefault("os", "")
    mem["user_profile"].setdefault("tools", [])
    mem.setdefault("project_state", {
        "current_goal": "",
        "milestones": [],
        "last_technical_decision": "",
    })
    mem.setdefault("short_term_context", "")

    NEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    NEW_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Migration complete: {added} new facts written to {NEW_FILE}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run migration**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 migrate_memory.py
```
Expected: `Migration complete: N new facts written to /tmp/tg_agent/global_memory.json`

- [ ] **Step 3: Verify output is valid JSON with facts**

```bash
python3 -c "import json; d=json.load(open('/tmp/tg_agent/global_memory.json')); print(json.dumps(d, ensure_ascii=False, indent=2)[:400])"
```
Expected: valid JSON, `user_profile.preferences` contains facts from old memory.md.

- [ ] **Step 4: Commit**

```bash
git add migrate_memory.py
git commit -m "feat: add migrate_memory.py — promote memory.md facts to global_memory.json"
```

---

## Task 7: End-to-end smoke test

- [ ] **Step 1: Run all tests**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -m pytest tests/ -v --tb=short
```
Expected: all tests PASS

- [ ] **Step 2: Full import sanity check**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -c "
from memory_manager import get_memory_manager
from context import global_ctx_for_prompt
import agents
import tg_agent
print('All imports OK')
mm = get_memory_manager()
print('Memory path:', mm._path)
block = mm.to_prompt_block()
print('Block preview:', block[:100])
print('Block length:', len(block))
"
```
Expected: `All imports OK` with no errors

- [ ] **Step 3: Compare prompt sizes — verify token reduction**

```bash
cd /home/stx/Applications/progect/pyChatALL && python3 -c "
from context import global_ctx_for_prompt, shared_ctx_for_prompt
old = shared_ctx_for_prompt()
new_full = global_ctx_for_prompt(skip_recent=False)
new_slim = global_ctx_for_prompt(skip_recent=True)
print(f'Old shared_ctx_for_prompt: {len(old)} chars')
print(f'New global_ctx (full):     {len(new_full)} chars')
print(f'New global_ctx (slim):     {len(new_slim)} chars')
"
```
Expected: `new_slim` ≤ `new_full` ≤ `old` after memory is populated. Initially new may be larger (empty memory defaults), which is expected until Shadow Librarian runs.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: global English memory layer complete — Shadow Librarian + token-efficient prompts"
```

---

## Rollback

If anything breaks at any point:

```bash
cd /home/stx/Applications/progect && tar -xzf pyChatALL_backup_20260324_013251.tar.gz
```
