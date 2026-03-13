"""Tests for router: _rule_classify, _ai_classify, classify."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import patch

from config import DEFAULT_MODELS, SONNET_MODEL
from router import _rule_classify, _ai_classify, classify, _HAIKU_MODEL, _SONNET_MODEL


# ── _rule_classify: Sonnet triggers ───────────────────────────

def test_file_attached_is_sonnet():
    assert _rule_classify("привет", "/tmp/file.py") == "sonnet"

def test_code_block_backticks_is_sonnet():
    assert _rule_classify("что делает ```python\nprint(1)\n```", None) == "sonnet"

def test_prompt_over_300_chars_is_sonnet():
    long_prompt = "а" * 301
    assert _rule_classify(long_prompt, None) == "sonnet"

def test_ru_keyword_реализуй_is_sonnet():
    assert _rule_classify("реализуй класс авторизации", None) == "sonnet"

def test_ru_keyword_отладь_is_sonnet():
    assert _rule_classify("отладь этот код", None) == "sonnet"

def test_en_keyword_implement_is_sonnet():
    assert _rule_classify("implement OAuth2 flow", None) == "sonnet"

def test_en_keyword_refactor_is_sonnet():
    assert _rule_classify("refactor this function", None) == "sonnet"

def test_en_keyword_debug_is_sonnet():
    assert _rule_classify("debug the login handler", None) == "sonnet"


# ── _rule_classify: Haiku triggers ────────────────────────────

def test_short_no_keywords_is_haiku():
    assert _rule_classify("привет", None) == "haiku"

def test_short_what_is_question_is_haiku():
    assert _rule_classify("что такое TCP?", None) == "haiku"

def test_exactly_79_chars_no_keywords_is_haiku():
    prompt = "х" * 79
    assert _rule_classify(prompt, None) == "haiku"


# ── _rule_classify: Ambiguous ─────────────────────────────────

def test_medium_no_keywords_is_ambiguous():
    # 80-300 chars, no Sonnet keywords, no file
    prompt = "объясни разницу между синхронным и асинхронным программированием с примерами использования"
    assert 80 <= len(prompt) <= 300, f"prompt length {len(prompt)} out of ambiguous range"
    assert _rule_classify(prompt, None) == "ambiguous"

def test_exactly_80_chars_no_keywords_is_ambiguous():
    prompt = "х" * 80
    assert _rule_classify(prompt, None) == "ambiguous"

def test_exactly_300_chars_no_keywords_is_ambiguous():
    prompt = "х" * 300
    assert _rule_classify(prompt, None) == "ambiguous"


# ── _ai_classify ──────────────────────────────────────────────

def _fake_subprocess_simple(cmd, timeout, cwd, env):
    return ("SIMPLE", "", 0, False)

def _fake_subprocess_complex(cmd, timeout, cwd, env):
    return ("COMPLEX", "", 0, False)

def _fake_subprocess_error(cmd, timeout, cwd, env):
    return ("", "error", 1, False)

def _fake_subprocess_ambiguous(cmd, timeout, cwd, env):
    return ("I think this is SIMPLE but could be COMPLEX", "", 0, False)


def test_ai_classify_simple_response_returns_haiku():
    with patch("os.path.isfile", return_value=True), \
         patch("router._run_subprocess_lazy", side_effect=_fake_subprocess_simple):
        result = _ai_classify("medium length prompt here about basic concepts")
    assert result == _HAIKU_MODEL

def test_ai_classify_complex_response_returns_sonnet():
    with patch("os.path.isfile", return_value=True), \
         patch("router._run_subprocess_lazy", side_effect=_fake_subprocess_complex):
        result = _ai_classify("medium length prompt here about basic concepts")
    assert result == _SONNET_MODEL

def test_ai_classify_ambiguous_response_returns_sonnet():
    with patch("os.path.isfile", return_value=True), \
         patch("router._run_subprocess_lazy", side_effect=_fake_subprocess_ambiguous):
        result = _ai_classify("medium length prompt here about basic concepts")
    assert result == _SONNET_MODEL

def test_ai_classify_error_returns_sonnet():
    with patch("os.path.isfile", return_value=True), \
         patch("router._run_subprocess_lazy", side_effect=_fake_subprocess_error):
        result = _ai_classify("medium length prompt here about basic concepts")
    assert result == _SONNET_MODEL


# ── classify() public entry point ─────────────────────────────

def test_classify_manual_model_bypasses_router():
    result = classify("реализуй авторизацию", None, "claude-opus-4-6")
    assert result == "claude-opus-4-6"

def test_classify_sonnet_rule_no_ai_call():
    with patch("router._ai_classify") as mock_ai:
        result = classify("посмотри файл", "/tmp/code.py", _HAIKU_MODEL)
    assert result == _SONNET_MODEL
    mock_ai.assert_not_called()

def test_classify_haiku_rule_no_ai_call():
    with patch("router._ai_classify") as mock_ai:
        result = classify("привет", None, _HAIKU_MODEL)
    assert result == _HAIKU_MODEL
    mock_ai.assert_not_called()

def test_classify_ambiguous_calls_ai_classifier():
    medium_prompt = "объясни разницу между синхронным и асинхронным программированием с примерами использования"
    assert 80 <= len(medium_prompt) <= 300
    with patch("router._ai_classify", return_value=_HAIKU_MODEL) as mock_ai:
        result = classify(medium_prompt, None, _HAIKU_MODEL)
    assert result == _HAIKU_MODEL
    mock_ai.assert_called_once_with(medium_prompt)
