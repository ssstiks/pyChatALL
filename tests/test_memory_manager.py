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
