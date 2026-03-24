# tests/test_heartbeat.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import tg_agent


# ── _placeholder_text ─────────────────────────────────────────

def test_placeholder_zero_elapsed():
    text = tg_agent._placeholder_text("Claude", 0, remaining=800)
    assert "0с" in text
    assert "осталось" in text
    assert "13м" in text   # 800s = 13m 20s

def test_placeholder_1min_elapsed():
    text = tg_agent._placeholder_text("Claude", 65, remaining=735)
    assert "1м 05с" in text
    assert "12м 15с" in text

def test_placeholder_no_limit():
    text = tg_agent._placeholder_text("Claude", 30, remaining=None, no_limit=True)
    assert "без лимита" in text
    assert "осталось" not in text

def test_placeholder_no_remaining():
    text = tg_agent._placeholder_text("Claude", 10, remaining=None)
    assert "10с" in text
    assert "осталось" not in text
    assert "без лимита" not in text
