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
        "skill_level": "unknown",   # beginner | intermediate | expert | unknown
        "language": "ru",           # primary interaction language
        "interaction_style": "",    # e.g. "terse", "verbose", "command-oriented"
    },
    "project_state": {
        "current_goal": "",
        "milestones": [],
        "last_technical_decision": "",
        "active_projects": [],      # list of project names currently in scope
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
- user_profile.skill_level: infer from behavior —
    "expert" if user uses raw CLI flags, reads tracebacks, references internals or architecture;
    "intermediate" if user writes code but asks for explanations;
    "beginner" if user asks what commands mean or needs step-by-step guidance;
    keep "unknown" if insufficient signal. Never downgrade (expert→intermediate is forbidden).
- user_profile.language: set to the language code the user writes in ("ru", "en", etc.).
- user_profile.interaction_style: one of "terse" (short commands, no prose), \
"verbose" (long descriptions), "command-oriented" (mostly CLI/code), "conversational". \
Infer from message style; update only if clearly different from current.
- project_state.current_goal: update if user's goal changed.
- project_state.milestones: append completed steps as short English phrases.
- project_state.last_technical_decision: last architectural/library choice made.
- project_state.active_projects: list of project names mentioned in scope (e.g. ["pyChatALL", "shadowchat"]).
- Remove conversational filler. Use shorthand: "Refactored X" not "The user has refactored X".
- Output ONLY valid JSON matching the same schema. No prose, no markdown fences.

CURRENT MEMORY:
{current_memory}

NEW EXCHANGE:
{exchange}

OUTPUT (JSON only):"""

# ── Lesson extraction prompt ─────────────────────────────────────────────────
_LESSON_PROMPT_TEMPLATE = """\
You are a technical lessons-learned extractor. Given a short technical summary, \
identify if any bug was FIXED or a technical hurdle was SOLVED.

If yes, output a JSON object with exactly two fields:
  "error_summary": one-line description of the problem (max 150 chars)
  "fix_steps": concise solution (max 200 chars)

If nothing was fixed or solved, output: {{"lesson": null}}

Output ONLY valid JSON. No prose, no markdown.

SUMMARY:
{summary}

OUTPUT (JSON only):"""

# Keywords signalling a completed fix (Russian + English)
_FIX_SIGNALS = (
    "fix", "fixed", "исправ", "решен", "solved", "resolved", "workaround",
    "bug", "баг", "ошибк", "error", "issue", "patch", "debugged",
)


# ── Deep merge helper ───────────────────────────────────────────────────────
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base; override wins on conflicts."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


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
        log.debug("shadow librarian: no cheap updater agent available, SKIPPING to save Claude limits")
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
        self._lock = _get_file_lock(self._path)

    # ── I/O ────────────────────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """Load memory JSON; return schema defaults if file missing or corrupt."""
        with self._lock:
            try:
                if self._path.exists():
                    data = json.loads(self._path.read_text(encoding="utf-8"))
                    merged = _deep_merge(json.loads(json.dumps(_EMPTY)), data)
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
            # Skip trivially short exchanges — not worth a model call
            if len(user_msg) + len(assistant_msg) < 80:
                log.debug("shadow librarian: exchange too short (%d chars), skipping",
                          len(user_msg) + len(assistant_msg))
                return
            current_mem = self.load()
            current_json = json.dumps(current_mem, ensure_ascii=False, indent=2)
            exchange = f"User: {user_msg[:500]}\nAssistant: {assistant_msg[:800]}"
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
                    # After saving, attempt lesson extraction in the same thread
                    ctx = updated.get("short_term_context", "")
                    projects = updated.get("project_state", {}).get("active_projects", [])
                    project = projects[0] if projects else "general"
                    self._try_extract_lesson(ctx, project)
                else:
                    log.warning("shadow librarian: invalid schema in response, skipping")
            else:
                log.debug("shadow librarian: could not parse JSON from response")
        except Exception as e:
            log.error("shadow librarian error: %s", e)

    def _try_extract_lesson(self, short_term_context: str, project_name: str) -> None:
        """Check if the context contains a solved bug; if so, extract and store a lesson."""
        if not short_term_context:
            return
        ctx_lower = short_term_context.lower()
        if not any(sig in ctx_lower for sig in _FIX_SIGNALS):
            return  # No fix signal — skip extraction

        prompt = _LESSON_PROMPT_TEMPLATE.format(summary=short_term_context[:600])
        raw = _run_updater_agent(prompt)
        if not raw:
            return

        parsed = _parse_agent_json_response(raw)
        if not isinstance(parsed, dict):
            return
        if parsed.get("lesson") is None:
            return  # Agent decided nothing to save

        error_summary = str(parsed.get("error_summary", "")).strip()
        fix_steps = str(parsed.get("fix_steps", "")).strip()
        if not error_summary or not fix_steps:
            return

        try:
            from config import DB_PATH
            from db_manager import Database
            db = Database(DB_PATH)
            if not db.lesson_exists(error_summary):
                db.add_lesson(project_name, error_summary, fix_steps)
                log.info("lesson learned: [%s] %s", project_name, error_summary[:60])
        except Exception as e:
            log.warning("lesson store failed: %s", e)


# ── Per-file locks (shared across MemoryManager instances for same path) ────
_file_locks: dict[str, threading.Lock] = {}
_file_locks_meta = threading.Lock()


def _get_file_lock(path: pathlib.Path) -> threading.Lock:
    key = str(path.resolve())
    with _file_locks_meta:
        if key not in _file_locks:
            _file_locks[key] = threading.Lock()
        return _file_locks[key]


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
