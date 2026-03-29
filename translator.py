#!/usr/bin/env python3
"""
RU ↔ EN auto-translation using cheap AI models (Qwen/Gemini-flash-lite).

Public API:
  is_enabled()    -> bool
  toggle()        -> bool   (returns new state)
  translate_to_en(text) -> str
  translate_to_ru(text) -> str

Design goals:
  - No imports from agents.py (circular import avoidance)
  - Subprocess calls directly to binaries
  - Fallback chain: Qwen → Gemini-flash-lite
  - 25s timeout per call; returns original text on any failure
"""

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, Future as _Future

from config import GEMINI_BIN, QWEN_BIN, WORK_DIR
from logger import log_info, log_warn

# ── State ──────────────────────────────────────────────────────
_state_lock = threading.Lock()

_QWEN_MODEL   = "coder-model"          # cheapest Qwen variant
_GEMINI_MODEL = "gemini-2.5-flash-lite"
_TIMEOUT      = 25                      # seconds per subprocess call

_TO_EN_PROMPT = (
    "Translate the following Russian text to English. "
    "Output ONLY the translation — no explanations, no quotes, no prefix.\n\n"
    "{text}"
)
_TO_RU_PROMPT = (
    "Translate the following English text to Russian. "
    "Output ONLY the translation — no explanations, no quotes, no prefix.\n\n"
    "{text}"
)

# ── Module-level executor singleton ───────────────────────────
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="translator")


def _load_state() -> bool:
    """Load translator enabled state from DB on startup."""
    try:
        from config import DB_PATH
        from db_manager import Database
        val = Database(DB_PATH).get_setting('translator_enabled')
        return val == '1'
    except Exception:
        return False

_enabled = _load_state()


def is_enabled() -> bool:
    with _state_lock:
        return _enabled


def toggle() -> bool:
    """Toggle translation on/off. Returns new state."""
    global _enabled
    with _state_lock:
        _enabled = not _enabled
        new_state = _enabled
    # Persist outside lock — no DB calls under threading.Lock
    try:
        from config import DB_PATH
        from db_manager import Database
        Database(DB_PATH).set_setting('translator_enabled', '1' if new_state else '0')
    except Exception:
        pass
    return new_state


def submit_en(text: str) -> _Future:
    """Submit RU→EN translation to the module-level executor. Returns a Future.
    Non-blocking — call .result(timeout=28) in the worker thread to get the value.
    """
    return _executor.submit(translate_to_en, text)


def _run_binary(binary: str, model: str, prompt: str) -> str | None:
    """
    Run a CLI binary with --yolo --print and return stripped stdout.
    Returns None on timeout, non-zero exit, or missing binary.
    """
    if not os.path.isfile(binary):
        return None

    env = os.environ.copy()
    # Strip agent-detection vars so recursive invocation doesn't confuse CLIs
    for var in ("CLAUDECODE", "GEMINICODE", "QWENCODE"):
        env.pop(var, None)

    cmd = [binary, "--yolo", "--model", model, "--print", "--prompt", prompt]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd=WORK_DIR,
            env=env,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None
    except subprocess.TimeoutExpired:
        log_warn(f"[translator] timeout ({_TIMEOUT}s) running {os.path.basename(binary)}")
        return None
    except Exception as e:
        log_warn(f"[translator] error running {os.path.basename(binary)}: {e}")
        return None


def _translate(text: str, prompt_template: str, direction: str) -> str:
    """Internal: try Qwen then Gemini; return original on failure."""
    if not text or not text.strip():
        return text

    prompt = prompt_template.format(text=text)

    # Try Qwen first (free, fast)
    result = _run_binary(QWEN_BIN, _QWEN_MODEL, prompt)
    if result:
        log_info(f"[translator] {direction} via Qwen ({len(text)}→{len(result)} chars)")
        return result

    # Fallback: Gemini flash-lite
    result = _run_binary(GEMINI_BIN, _GEMINI_MODEL, prompt)
    if result:
        log_info(f"[translator] {direction} via Gemini ({len(text)}→{len(result)} chars)")
        return result

    log_warn(f"[translator] {direction} failed — returning original text")
    return text


def translate_to_en(text: str) -> str:
    """Translate RU→EN. Returns original text on failure."""
    return _translate(text, _TO_EN_PROMPT, "RU→EN")


def translate_to_ru(text: str) -> str:
    """Translate EN→RU. Skips if text contains code blocks to preserve code integrity."""
    if "```" in text:
        log_info(f"[translator] EN→RU skipped: response contains code blocks ({len(text)} chars)")
        return text
    return _translate(text, _TO_RU_PROMPT, "EN→RU")
