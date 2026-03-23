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
