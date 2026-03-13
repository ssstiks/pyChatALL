#!/usr/bin/env python3
"""
Маршрутизатор сложности задач для Claude.
Выбирает между Haiku (дешёвый) и Sonnet (мощный) на основе анализа промпта.

Публичный интерфейс: classify(prompt, file_path, current_model) -> str

Импорты: router.py НЕ импортирует agents.py на уровне модуля — только
ленивый импорт внутри функции, чтобы избежать кругового импорта.
"""

import concurrent.futures
import os

from config import (
    DEFAULT_MODELS, SONNET_MODEL,
    GEMINI_BIN, QWEN_BIN,
    WORK_DIR,
)
from logger import log_info, log_warn

_HAIKU_MODEL  = DEFAULT_MODELS["claude"]
_SONNET_MODEL = SONNET_MODEL

_SONNET_KEYWORDS_RU = (
    "реализуй", "напиши", "исправь", "отладь",
    "рефактор", "архитектур", "алгоритм", "внедри", "разработай",
)
_SONNET_KEYWORDS_EN = (
    "implement", "refactor", "debug", "architecture",
    "algorithm", "build", "develop", "fix", "review",
)

_CLASSIFIER_PROMPT = """\
You are a complexity classifier for a coding assistant.
Reply with exactly one word: SIMPLE or COMPLEX.

SIMPLE: greetings, short questions, explanations, translations,
        "what is X", "explain Y", summarizing text.
COMPLEX: writing code, debugging, implementing features,
         architecture design, code review, refactoring,
         multi-step tasks, technical analysis.

Task: {prompt}"""


def _rule_classify(prompt: str, file_path: str | None) -> str:
    if file_path is not None:
        return "sonnet"
    if "```" in prompt or "`" in prompt:
        return "sonnet"
    if len(prompt) > 300:
        return "sonnet"
    prompt_lower = prompt.lower()
    if any(kw in prompt_lower for kw in _SONNET_KEYWORDS_RU):
        return "sonnet"
    if any(kw in prompt_lower for kw in _SONNET_KEYWORDS_EN):
        return "sonnet"
    if len(prompt) < 80:
        return "haiku"
    return "ambiguous"


def _run_subprocess_lazy(cmd, timeout, cwd, env):
    """Запускает подпроцесс и возвращает (stdout, stderr, returncode, timed_out)."""
    import subprocess
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        return (result.stdout.strip(), result.stderr.strip(), result.returncode, False)
    except subprocess.TimeoutExpired:
        return ("", "timeout", -1, True)
    except Exception as exc:
        return ("", str(exc), -1, False)


def _ai_classify(prompt: str) -> str:
    """Использует Haiku для классификации сложности промпта.

    Возвращает _HAIKU_MODEL если SIMPLE, иначе _SONNET_MODEL.
    При любой ошибке возвращает _SONNET_MODEL (safe default).
    """
    classifier_input = _CLASSIFIER_PROMPT.format(prompt=prompt[:500])
    cmd = [
        "/home/stx/.local/bin/claude",
        "--model", _HAIKU_MODEL,
        "--print",
        classifier_input,
    ]
    env = {**os.environ}
    stdout, stderr, returncode, timed_out = _run_subprocess_lazy(
        cmd, timeout=15, cwd=WORK_DIR, env=env
    )
    if returncode != 0 or not stdout:
        log_warn(f"[router] _ai_classify error: {stderr!r}")
        return _SONNET_MODEL
    answer = stdout.strip().upper()
    if answer == "SIMPLE":
        log_info(f"[router] AI → SIMPLE → haiku")
        return _HAIKU_MODEL
    log_info(f"[router] AI → {answer!r} → sonnet")
    return _SONNET_MODEL


def classify(prompt: str, file_path: str | None, current_model: str) -> str:
    """Публичный интерфейс маршрутизатора.

    Если current_model — не дефолтная haiku модель, возвращает её без изменений
    (пользователь явно выбрал модель).
    """
    # Если пользователь вручную выбрал не-haiku модель — не переопределяем.
    if current_model != _HAIKU_MODEL:
        return current_model

    decision = _rule_classify(prompt, file_path)

    if decision == "sonnet":
        log_info(f"[router] rule → sonnet")
        return _SONNET_MODEL
    if decision == "haiku":
        log_info(f"[router] rule → haiku")
        return _HAIKU_MODEL

    # ambiguous — спрашиваем AI
    log_info(f"[router] rule → ambiguous, calling AI classifier")
    return _ai_classify(prompt)
