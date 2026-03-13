# Claude Complexity Router Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically select Claude Haiku (cheap) or Sonnet (powerful) per request by classifying task complexity via rules and a concurrent Gemini+Qwen classifier.

**Architecture:** A new `router.py` module exposes a single `classify()` function. Rules handle obvious cases instantly; ambiguous prompts are sent to Gemini and Qwen concurrently via `ThreadPoolExecutor` — first valid response within 3s wins. `ask_claude()` in `agents.py` calls `classify()` and passes the result as `model_override` to `_run_cli()`. Manual model selection always bypasses the router. `router.py` avoids circular imports by using a lazy import of `_run_subprocess` inside the function body that needs it.

**Tech Stack:** Python 3.11+, `concurrent.futures.ThreadPoolExecutor`, existing `_run_subprocess()` infrastructure.

---

## Chunk 1: Config constant + rule classifier

### Task 1: Add `SONNET_MODEL` constant to `config.py`

**Files:**
- Modify: `config.py` (after `DEFAULT_MODELS` block, around line 124)

The constant must be defined after `KNOWN_MODELS` since it references it.

- [ ] **Step 1: Add the constant**

In `config.py`, after the `DEFAULT_MODELS` block, add:

```python
# Named constant — immune to KNOWN_MODELS list reordering
SONNET_MODEL = KNOWN_MODELS["claude"][0]   # "claude-sonnet-4-6"
```

- [ ] **Step 2: Verify import works**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -c "from config import SONNET_MODEL, DEFAULT_MODELS; print(SONNET_MODEL, DEFAULT_MODELS['claude'])"
```

Expected output:
```
claude-sonnet-4-6 claude-haiku-4-5-20251001
```

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add SONNET_MODEL named constant to config"
```

---

### Task 2: Create `router.py` with `_rule_classify`

**Files:**
- Create: `router.py`
- Create: `tests/test_router.py`

The rule classifier is pure logic — no I/O, no subprocess calls. Write tests first.

- [ ] **Step 1: Create `tests/` directory and write the complete test file**

```bash
mkdir -p /home/stx/Applications/progect/pyChatALL/tests
```

Create `tests/test_router.py` with ALL imports at the top (including ones needed by later tasks):

```python
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
    # Deliberately uses no words from _SONNET_KEYWORDS_RU or _SONNET_KEYWORDS_EN
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
    with patch("router._run_subprocess_lazy", side_effect=_fake_subprocess_simple):
        result = _ai_classify("medium length prompt here about basic concepts")
    assert result == _HAIKU_MODEL

def test_ai_classify_complex_response_returns_sonnet():
    with patch("router._run_subprocess_lazy", side_effect=_fake_subprocess_complex):
        result = _ai_classify("medium length prompt here about basic concepts")
    assert result == _SONNET_MODEL

def test_ai_classify_ambiguous_response_returns_sonnet():
    # "SIMPLE but could be COMPLEX" → both words → Sonnet (safety rule)
    with patch("router._run_subprocess_lazy", side_effect=_fake_subprocess_ambiguous):
        result = _ai_classify("medium length prompt here about basic concepts")
    assert result == _SONNET_MODEL

def test_ai_classify_error_returns_sonnet():
    with patch("router._run_subprocess_lazy", side_effect=_fake_subprocess_error):
        result = _ai_classify("medium length prompt here about basic concepts")
    assert result == _SONNET_MODEL


# ── classify() public entry point ─────────────────────────────

def test_classify_manual_model_bypasses_router():
    # Non-default model → return as-is, no classification
    result = classify("реализуй авторизацию", None, "claude-opus-4-6")
    assert result == "claude-opus-4-6"

def test_classify_sonnet_rule_no_ai_call():
    # File attached → Sonnet instantly, _ai_classify never called
    with patch("router._ai_classify") as mock_ai:
        result = classify("посмотри файл", "/tmp/code.py", _HAIKU_MODEL)
    assert result == _SONNET_MODEL
    mock_ai.assert_not_called()

def test_classify_haiku_rule_no_ai_call():
    # Short prompt → Haiku instantly, _ai_classify never called
    with patch("router._ai_classify") as mock_ai:
        result = classify("привет", None, _HAIKU_MODEL)
    assert result == _HAIKU_MODEL
    mock_ai.assert_not_called()

def test_classify_ambiguous_calls_ai_classifier():
    # Medium prompt → routes to AI classifier
    medium_prompt = "объясни разницу между синхронным и асинхронным программированием с примерами использования"
    assert 80 <= len(medium_prompt) <= 300
    with patch("router._ai_classify", return_value=_HAIKU_MODEL) as mock_ai:
        result = classify(medium_prompt, None, _HAIKU_MODEL)
    assert result == _HAIKU_MODEL
    mock_ai.assert_called_once_with(medium_prompt)
```

**Note:** The test file is written in full upfront with all imports at the top. Tasks 2–4 implement the code these tests exercise. Tests for later tasks (`_ai_classify`, `classify`) will import-error until those functions are added — that is expected.

- [ ] **Step 2: Run tests — expect ImportError (module doesn't exist yet)**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -m pytest tests/test_router.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'router'`

- [ ] **Step 3: Create `router.py` with `_rule_classify` only**

Create `/home/stx/Applications/progect/pyChatALL/router.py`:

```python
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

# Ключевые слова, однозначно указывающие на сложную задачу
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
    """
    Мгновенная классификация по правилам.
    Возвращает 'sonnet', 'haiku' или 'ambiguous'.
    """
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
```

- [ ] **Step 4: Run only `_rule_classify` tests**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -m pytest tests/test_router.py -k "rule_classify or sonnet or haiku or ambiguous" -v
```

Expected: all `_rule_classify` tests **PASS** (the `_ai_classify` and `classify` tests will fail with ImportError — that is expected at this stage).

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py config.py
git commit -m "feat: add _rule_classify and SONNET_MODEL constant"
```

---

## Chunk 2: AI classifier + public `classify()` entry point

### Task 3: Add `_ai_classify` to `router.py`

**Files:**
- Modify: `router.py`

**Key design decisions:**
- `_run_subprocess` is imported lazily inside `_run_subprocess_lazy()` to avoid a circular import (`agents.py` will later import `router`; if `router` imported `agents` at module level, both would fail to load).
- The classifier CLIs are called **without** `--output-format json` so they return plain text. This avoids JSON parsing and lets us search for `SIMPLE`/`COMPLEX` directly.
- `ThreadPoolExecutor` is shut down with `cancel_futures=True` so we don't block waiting for the slower subprocess after the first result is obtained.

- [ ] **Step 1: Run `_ai_classify` tests to verify they fail**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -m pytest tests/test_router.py -k "ai_classify" -v 2>&1 | head -10
```

Expected: `ImportError: cannot import name '_ai_classify' from 'router'`

- [ ] **Step 2: Append `_ai_classify` to `router.py`**

Add after `_rule_classify`:

```python
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
        for future in concurrent.futures.as_completed(futures, timeout=3):
            res = future.result()
            if res is not None:
                log_info(f"Router: AI classifier → {res} (binary={futures[future]})")
                result = res
                break
    except concurrent.futures.TimeoutError:
        log_warn("Router: AI classifier timed out (3s) — defaulting to Sonnet")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if result is None:
        log_warn("Router: both classifiers failed — defaulting to Sonnet")
        return _SONNET_MODEL
    return result
```

**Note on `--print` flag:** Gemini/Qwen CLIs may not support `--print`. If the flag is rejected, remove it from `cmd` — the response is still plain text by default when `--output-format json` is absent.

- [ ] **Step 3: Run `_ai_classify` tests**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -m pytest tests/test_router.py -k "ai_classify" -v
```

Expected: all 4 `_ai_classify` tests **PASS**.

- [ ] **Step 4: Commit**

```bash
git add router.py
git commit -m "feat: add _ai_classify with concurrent Gemini+Qwen and 3s timeout"
```

---

### Task 4: Add public `classify()` entry point to `router.py`

**Files:**
- Modify: `router.py`

- [ ] **Step 1: Run `classify` tests to verify they fail**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -m pytest tests/test_router.py -k "classify" -v 2>&1 | head -10
```

Expected: `ImportError: cannot import name 'classify' from 'router'`

- [ ] **Step 2: Append `classify()` to `router.py`**

```python
def classify(prompt: str, file_path: str | None, current_model: str) -> str:
    """
    Публичный интерфейс роутера.
    Возвращает имя модели Claude для данного запроса.

    Если current_model не является дефолтной (пользователь выбрал вручную) —
    возвращает current_model без классификации.
    """
    if current_model != _HAIKU_MODEL:
        return current_model

    rule = _rule_classify(prompt, file_path)

    if rule == "sonnet":
        log_info(f"Router: rules → Sonnet (len={len(prompt)}, file={file_path is not None})")
        return _SONNET_MODEL

    if rule == "haiku":
        log_info(f"Router: rules → Haiku (len={len(prompt)})")
        return _HAIKU_MODEL

    log_info(f"Router: ambiguous (len={len(prompt)}) — calling AI classifier")
    return _ai_classify(prompt)
```

- [ ] **Step 3: Run all router tests**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -m pytest tests/test_router.py -v
```

Expected: **all tests PASS**. If any fail, check the output carefully — common issues are keyword collisions in ambiguous test prompts.

- [ ] **Step 4: Verify no circular import**

```bash
python3 -c "import router; print('router OK')"
python3 -c "import agents; print('agents OK')"
python3 -c "import tg_agent; print('tg_agent OK')"
```

Expected: each line prints `OK` with no errors.

- [ ] **Step 5: Commit**

```bash
git add router.py
git commit -m "feat: add classify() public entry point to router"
```

---

## Chunk 3: Wire router into `agents.py`

### Task 5: Add `model_override` to `_run_cli()`

**Files:**
- Modify: `agents.py` lines 180–307

Surgical change: add one parameter, replace the `model` variable with `effective_model`.

- [ ] **Step 1: Add `model_override` parameter to `_run_cli()` signature**

Find (line ~180):

```python
def _run_cli(binary: str, session_file: str, ctx_file: str,
             agent_name: str, prompt: str,
             file_path: str | None = None,
             extra_flags: list | None = None) -> str:
```

Replace with:

```python
def _run_cli(binary: str, session_file: str, ctx_file: str,
             agent_name: str, prompt: str,
             file_path: str | None = None,
             extra_flags: list | None = None,
             model_override: str | None = None) -> str:
```

- [ ] **Step 2: Replace `model` with `effective_model` in `_run_cli()` body**

Find (line ~216):

```python
    agent_key = ("claude" if "claude" in binary else
                 "gemini" if "gemini" in binary else "qwen")
    model = get_model(agent_key)

    cmd = [binary]
    if is_claude:
        cmd += ["--print", "--dangerously-skip-permissions"]
    cmd += ["--output-format", "json"]
    if not is_claude:
        cmd += ["--yolo"]
    if model:
        cmd += ["--model", model]
```

Replace with:

```python
    agent_key = ("claude" if "claude" in binary else
                 "gemini" if "gemini" in binary else "qwen")
    effective_model = model_override if model_override is not None else get_model(agent_key)

    cmd = [binary]
    if is_claude:
        cmd += ["--print", "--dangerously-skip-permissions"]
    cmd += ["--output-format", "json"]
    if not is_claude:
        cmd += ["--yolo"]
    if effective_model:
        cmd += ["--model", effective_model]
```

- [ ] **Step 3: Update the log line to use `effective_model`**

Find (line ~244):

```python
    log_info(f"→ {agent_name} [{model}] prompt={len(full_prompt)}ch timeout={timeout_secs}s"
```

Replace `[{model}]` with `[{effective_model}]`:

```python
    log_info(f"→ {agent_name} [{effective_model}] prompt={len(full_prompt)}ch timeout={timeout_secs}s"
```

- [ ] **Step 4: Verify syntax**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -c "import agents; print('agents OK')"
```

Expected: `agents OK`

- [ ] **Step 5: Commit**

```bash
git add agents.py
git commit -m "feat: add model_override param to _run_cli()"
```

---

### Task 6: Wire `classify()` into `ask_claude()`

**Files:**
- Modify: `agents.py` lines 444–452

- [ ] **Step 1: Add `import router` to `agents.py`**

At the top of `agents.py`, after the existing project imports block, add:

```python
import router
```

- [ ] **Step 2: Modify `ask_claude()` to call `classify()`**

Find the current `ask_claude()`:

```python
def ask_claude(prompt: str, file_path: str | None = None) -> str:
    bin_path = _get_effective_bin("claude")
    if not os.path.isfile(bin_path):
        return "❌ Claude CLI не установлен. Используй /setup для установки."
    msg = claude_rate_msg()
    if msg:
        return msg
    return _run_cli(bin_path, CLAUDE_SESSION, CLAUDE_CTX_FILE,
                    "Claude", prompt, file_path)
```

Replace with (binary-check and rate-limit guard preserved unchanged):

```python
def ask_claude(prompt: str, file_path: str | None = None) -> str:
    bin_path = _get_effective_bin("claude")
    if not os.path.isfile(bin_path):
        return "❌ Claude CLI не установлен. Используй /setup для установки."
    msg = claude_rate_msg()
    if msg:
        return msg
    current_model = get_model("claude")
    effective_model = router.classify(prompt, file_path, current_model)
    return _run_cli(bin_path, CLAUDE_SESSION, CLAUDE_CTX_FILE,
                    "Claude", prompt, file_path,
                    model_override=effective_model)
```

- [ ] **Step 3: Verify all imports resolve cleanly**

```bash
cd /home/stx/Applications/progect/pyChatALL
python3 -c "import agents; print('agents OK')"
python3 -c "import tg_agent; print('tg_agent OK')"
python3 -m pytest tests/test_router.py -v
```

Expected:
- `agents OK`
- `tg_agent OK`
- All router tests **PASS**

- [ ] **Step 4: Smoke test the classifier**

```bash
python3 -c "
from router import classify, _HAIKU_MODEL, _SONNET_MODEL

haiku = _HAIKU_MODEL

tests = [
    ('привет', None, haiku, haiku),
    ('реализуй авторизацию через JWT', None, haiku, _SONNET_MODEL),
    ('посмотри файл', '/tmp/x.py', haiku, _SONNET_MODEL),
    ('привет', None, 'claude-opus-4-6', 'claude-opus-4-6'),
]
for prompt, fp, model, expected in tests:
    result = classify(prompt, fp, model)
    status = 'OK' if result == expected else 'FAIL'
    print(f'{status}: classify({prompt!r:.30}, fp={fp is not None}, model={model!r:.20}) -> {result!r:.35}')
"
```

Expected: all lines show `OK`.

- [ ] **Step 5: Final commit**

```bash
git add agents.py
git commit -m "feat: wire complexity router into ask_claude() — auto Haiku/Sonnet selection"
```

---

## Verification

- [ ] Run full test suite: `python3 -m pytest tests/ -v`
- [ ] Restart the bot and check logs: `grep "Router:" /tmp/tg_agent.log | tail -20`
- [ ] Send a simple message ("привет") → expect log: `Router: rules → Haiku`
- [ ] Send a coding request ("реализуй функцию сортировки") → expect log: `Router: rules → Sonnet`
- [ ] Set manual model (`/claude /model opus`) and send any message → expect no `Router:` log line, opus used directly
