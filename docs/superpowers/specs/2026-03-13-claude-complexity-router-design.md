# Claude Complexity Router — Design Spec

**Date:** 2026-03-13
**Status:** Approved
**Scope:** Automatic selection between Claude Haiku and Claude Sonnet per request based on task complexity.

---

## Problem

Claude Sonnet is ~12× more expensive than Haiku. Most conversational and simple requests do not require Sonnet's capabilities. The default model should be Haiku, with Sonnet reserved for tasks that genuinely need it — without requiring the user to switch manually.

---

## Goals

- Automatically select Haiku or Sonnet per request.
- Manual model selection (`/claude /model sonnet`) always overrides the router.
- The routed model is used for that request only — never persisted to disk.
- Classification adds ≤3s of overhead in the worst case; both classifiers run concurrently so a single timeout covers both.
- System degrades gracefully if all classifiers are unavailable.

---

## Architecture

### Components

**`router.py`** — new module with two internal functions exposed through one public interface:

- `_rule_classify(prompt: str, file_path: str | None) -> str`
  Returns `"haiku"`, `"sonnet"`, or `"ambiguous"`. Instant, no I/O, no imports from `team_mode`.

- `_ai_classify(prompt: str) -> str`
  Returns `"haiku"` or `"sonnet"`. Submits both Gemini and Qwen to a `ThreadPoolExecutor` concurrently, takes the first valid result within 3s total. Falls back to `"sonnet"` if both fail or time out.

- `classify(prompt: str, file_path: str | None, current_model: str) -> str`
  Public entry point. Returns the model name string to use for this request.

**`agents.py`** — `ask_claude()` calls `router.classify()` and passes the result as `model_override` into `_run_cli()`. `_run_cli()` gains an optional `model_override: str | None = None` parameter.

---

## Decision Flow

```
ask_claude(prompt, file_path)
    │
    ├─ current_model != DEFAULT_MODELS["claude"]?
    │       └─ return current_model  (user override — skip router entirely)
    │
    └─ call router.classify(prompt, file_path, current_model)
            │
            ├─ _rule_classify(prompt, file_path)
            │       ├─ SONNET  → return SONNET_MODEL               # "claude-sonnet-4-6"
            │       ├─ HAIKU   → return DEFAULT_MODELS["claude"]    # "claude-haiku-4-5-20251001"
            │       └─ AMBIGUOUS → _ai_classify(prompt)
            │                           ├─ Gemini + Qwen submitted concurrently
            │                           ├─ first valid result within 3s total wins
            │                           └─ both failed/timed out → SONNET_MODEL (safe default)
            │
            └─ return model name string (never written to disk)
```

Model names are sourced from `config.py`. Add a named constant `SONNET_MODEL = KNOWN_MODELS["claude"][0]` in `config.py` alongside `DEFAULT_MODELS` — this makes the Sonnet reference explicit and immune to list reordering. `router.py` imports `SONNET_MODEL` and `DEFAULT_MODELS`; no hardcoded strings.

---

## Rule Layer

### Sonnet triggers (any one is sufficient)

| Signal | Condition |
|---|---|
| File attached | `file_path is not None` |
| Code block | ` ``` ` or `` ` `` present in prompt |
| Prompt length | `len(prompt) > 300` |
| Programming keywords (RU) | `реализуй`, `напиши`, `исправь`, `отладь`, `рефактор`, `архитектур`, `алгоритм`, `внедри`, `разработай` |
| Programming keywords (EN) | `implement`, `refactor`, `debug`, `architecture`, `algorithm`, `build`, `develop`, `fix`, `review` |

**Note:** Team mode tasks naturally hit the Sonnet path through the prompt-length and keyword rules — long multi-step instructions always exceed 300 characters. No `team_mode` import is needed in `router.py`.

### Haiku triggers (both must be true)

| Signal | Condition |
|---|---|
| Short prompt | `len(prompt) < 80` |
| No Sonnet keywords | none of the Sonnet conditions above match |

The conversational-prefix requirement is intentionally omitted: any short prompt with no technical signal is treated as simple, regardless of phrasing.

### Ambiguous

Prompts 80–300 characters with no Sonnet keywords and no file attached. These are routed to the AI classifier.

---

## AI Classifier

### Threading model

`_ai_classify()` submits both Gemini and Qwen as separate futures to a `ThreadPoolExecutor`. It then iterates `concurrent.futures.as_completed(futures, timeout=3)` — whichever responds first with a valid result wins; the other is abandoned. Total wait is at most 3 seconds regardless of how many classifiers are tried. Subprocesses that outlive the future are killed via `_run_subprocess()` kill logic.

### Prompt sent to Gemini/Qwen

```
You are a complexity classifier for a coding assistant.
Reply with exactly one word: SIMPLE or COMPLEX.

SIMPLE: greetings, short questions, explanations, translations,
        "what is X", "explain Y", summarizing text.
COMPLEX: writing code, debugging, implementing features,
         architecture design, code review, refactoring,
         multi-step tasks, technical analysis.

Task: {prompt[:500]}
```

### Response parsing

```python
result = response.strip().upper()
if "SIMPLE" in result and "COMPLEX" not in result:
    return DEFAULT_MODELS["claude"]   # Haiku
return SONNET_MODEL                   # Sonnet — ambiguous, partial, empty, or COMPLEX
```

**Safety rule:** Any response that is not unambiguously `SIMPLE` defaults to Sonnet. Fail toward quality, not savings.

### Fallback chain

```
Gemini + Qwen submitted concurrently to ThreadPoolExecutor
as_completed(futures, timeout=3) — first valid result wins
    ↓ timeout or all results invalid
Both failed                        → SONNET_MODEL  (Sonnet, safe default)
```

---

## Integration Point: `_run_cli()`

`_run_cli()` currently builds the `--model` flag unconditionally from `get_model(agent_key)`:

```python
model = get_model(agent_key)
if model:
    cmd += ["--model", model]
```

After this change, it gains `model_override: str | None = None`. When `model_override` is set, it **replaces** the `get_model()` result entirely — `get_model()` is still called when `model_override` is `None`. The log line must also use the effective model to avoid logging the wrong model name:

```python
effective_model = model_override if model_override is not None else get_model(agent_key)
if effective_model:
    cmd += ["--model", effective_model]
# log uses effective_model, not model
```

`get_model()` is never skipped — `model_override` only substitutes its return value.

### `ask_claude()` after change

The snippet below shows only the lines that change. The existing binary-availability check and rate-limit guard at the top of `ask_claude()` must be preserved unchanged.

```python
# Add to imports at top of agents.py:
from router import classify

# Inside ask_claude(), before calling _run_cli():
current_model = get_model("claude")
effective_model = classify(prompt, file_path, current_model)
# Pass effective_model as model_override into the existing _run_cli() call.
```

---

## What Does Not Change

- User's saved model in `claude_model.txt` — untouched by the router.
- `/claude /model sonnet` — still works; any non-default model bypasses the router.
- All other agents (Gemini, Qwen, OpenRouter) — unaffected.
- `team_mode.py` — no changes; its tasks route to Sonnet naturally via rules.

---

## Files Changed

| File | Change |
|---|---|
| `router.py` | New module: `_rule_classify`, `_ai_classify`, `classify` |
| `agents.py` | `ask_claude()` calls `classify()`; `_run_cli()` gains `model_override` param |
| `config.py` | Add `SONNET_MODEL = KNOWN_MODELS["claude"][0]` named constant |

---

## Out of Scope

- Per-agent routing for Gemini, Qwen, or OpenRouter.
- Learning from user feedback.
- Showing the selected model to the user in Telegram (silent routing).
- Persisting routing decisions.
