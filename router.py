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
    # Увеличиваем порог: 1000 символов вместо 300.
    # Большинство простых вопросов и правок кода влезают в 1000 симв.
    if len(prompt) > 1000:
        return "sonnet"
    # Ищем код: только если это блоки кода ``` (многострочные)
    if "```" in prompt:
        return "sonnet"
    
    prompt_lower = prompt.lower()
    # Более специфичные ключевые слова для сложной разработки
    complex_keywords = (
        "архитектур", "алгоритм", "рефактор", "внедри", "разработай", 
        "architecture", "algorithm", "refactor", "implement", "design"
    )
    if any(kw in prompt_lower for kw in complex_keywords):
        return "sonnet"
    
    # Для всего остального (вопросы, мелкие фиксы) — Haiku
    return "haiku"


def _run_subprocess_lazy(cmd: list, timeout: int, cwd: str, env: dict):
    """
    Ленивая обёртка над agents._run_subprocess.
    Импортируется внутри функции, чтобы избежать кругового импорта:
      agents.py → import router → router imports agents (ERROR).
    После первой загрузки Python кеширует модуль — повторный вызов бесплатен.
    """
    from agents import _run_subprocess
    return _run_subprocess(cmd, timeout, cwd, env)


def _parse_classifier_response(response: str) -> str | None:
    """
    Парсит текстовый ответ классификатора.
    Возвращает модель или None если ответ некорректный.
    Безопасное правило: SIMPLE только если слово есть без COMPLEX.
    """
    result = response.strip().upper()
    if not result:
        return None
    if "SIMPLE" in result and "COMPLEX" not in result:
        return _HAIKU_MODEL
    if "COMPLEX" in result:
        return _SONNET_MODEL
    return None


def _classify_with_binary(binary: str, prompt: str) -> str | None:
    """
    Запускает один классификатор без --output-format json (возвращает plain text).
    Возвращает модель или None при ошибке/таймауте.
    """
    if not os.path.isfile(binary):
        return None
    classifier_prompt = _CLASSIFIER_PROMPT.format(prompt=prompt[:500])
    # Без --output-format json: получаем plain text для простого поиска SIMPLE/COMPLEX
    cmd = [binary, "--yolo", "--print", "--prompt", classifier_prompt]
    env = os.environ.copy()
    for var in ("CLAUDECODE", "GEMINICODE", "QWENCODE"):
        env.pop(var, None)
    try:
        stdout, stderr, rc, timed_out = _run_subprocess_lazy(
            cmd, timeout=8, cwd=WORK_DIR, env=env
        )
        if timed_out or rc != 0:
            return None
        return _parse_classifier_response(stdout)
    except Exception:
        return None


def _ai_classify(prompt: str) -> str:
    """
    Запускает Gemini и Qwen параллельно. Первый валидный ответ за 3с побеждает.
    При неудаче обоих возвращает Sonnet (безопасный дефолт).
    shutdown(cancel_futures=True) предотвращает блокировку после раннего возврата.
    """
    binaries = [GEMINI_BIN, QWEN_BIN]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    futures = {
        executor.submit(_classify_with_binary, binary, prompt): binary
        for binary in binaries
    }
    result = None
    try:
        for future in concurrent.futures.as_completed(futures, timeout=10):
            res = future.result()
            if res is not None:
                log_info(f"Router: AI classifier → {res} (binary={futures[future]})")
                result = res
                break
    except concurrent.futures.TimeoutError:
        log_warn("Router: AI classifier timed out (10s) — defaulting to Sonnet")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if result is None:
        log_warn("Router: both classifiers failed — defaulting to Sonnet")
        return _SONNET_MODEL
    return result


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
        log_info("[router] rule → sonnet")
        return _SONNET_MODEL
    if decision == "haiku":
        log_info("[router] rule → haiku")
        return _HAIKU_MODEL

    # ambiguous — спрашиваем AI
    log_info("[router] rule → ambiguous, calling AI classifier")
    return _ai_classify(prompt)
