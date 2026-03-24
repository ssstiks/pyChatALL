# Voice Messages Design

**Date:** 2026-03-24
**Feature:** #1 Voice message transcription for pyChatALL Telegram bot

## Overview

When a user sends a voice message to the bot, it is automatically transcribed using OpenAI Whisper and forwarded to the active AI agent as text. Dependencies (whisper, ffmpeg) are auto-installed if missing.

## Architecture

A new standalone module `voice.py` handles transcription. It has **no imports from any project module** — only stdlib and optional third-party packages.

`tg_agent.py` calls `voice.py` inside `process_update()`, which runs in the single polling thread. `_queue_worker` is a separate thread that processes agents — it does **not** call `transcribe_voice()`. Status `tg_send()` calls stay in `tg_agent.py`.

```
process_update() [polling thread]
    ↓
download_tg_file()           # existing
    ↓
[caller sends tg_send status messages]
    ↓
transcribe_voice(path)       # voice.py — always in polling thread
    ↓
_request_queue.put(...)      # existing queue, separate _queue_worker thread
```

**Thread safety:** `transcribe_voice()` is called only from the polling thread (no concurrency). `_whisper_model` needs no lock.

## Components

### `voice.py`

```python
_whisper_mod   = None   # the whisper module, assigned by _ensure_whisper()
_whisper_model = None   # loaded whisper.Model, assigned on first transcription
```

**`_ensure_whisper() -> module`**

1. Try `import whisper` — on success, set `_whisper_mod`, return it
2. On `ImportError`: run `subprocess.run([sys.executable, "-m", "pip", "install", "openai-whisper", "-q"], capture_output=True)`
3. If returncode != 0: raise `RuntimeError(f"pip install openai-whisper failed (rc={proc.returncode}): {proc.stderr.decode()[:200]}")`
4. `import whisper`, set `_whisper_mod`, return it

**`transcribe_voice(file_path: str) -> str`**

1. Check `shutil.which("ffmpeg")` — if None, raise `RuntimeError("ffmpeg not found: sudo apt install ffmpeg")`
2. Call `_ensure_whisper()` — raises `RuntimeError` on failure; sets `_whisper_mod`
3. If `_whisper_model is None`: `_whisper_model = _whisper_mod.load_model("base")`
4. `result = _whisper_model.transcribe(file_path, language="ru")`
5. Return `result["text"].strip()` — may be empty string, never raises for empty
6. Any exception from step 4 propagates to caller

**Language:** Fixed `"ru"`. Intentional — bot is Russian. Known limitation: non-Russian audio transcribed with degraded accuracy.

### `tg_agent.py` — `process_update()` voice block

**Integration point:** The new block completely **replaces** the existing `elif voice:` branch (lines ~1180-1183). It must **always `return`** — it never falls through to the generic `_request_queue.put()` at the end of `process_update()`.

`voice.py` is imported at module level in `tg_agent.py` (not inside the handler):
```python
import voice as _voice_mod   # module-level import, next to other imports
```
If `voice.py` is missing, the bot fails at startup with a clear `ImportError`, not silently at runtime.

**Replacement `elif voice:` block:**
```python
elif voice:
    local_path = download_tg_file(voice["file_id"], "voice.ogg")
    if not local_path:
        return  # download failed — silent skip

    try:
        import importlib.util
        # Status messages before blocking calls
        if importlib.util.find_spec("whisper") is None:
            tg_send("📦 Устанавливаю Whisper (первый раз, ~30 сек)...")
        elif _voice_mod._whisper_model is None:
            tg_send("⏳ Загружаю модель Whisper (первый запуск, ~1 мин)...")

        transcribed = _voice_mod.transcribe_voice(local_path)

        if not transcribed:
            tg_send("🎤 Речь не распознана")
            return

        prompt_text = f"[Голосовое сообщение]: {transcribed}"
        _request_queue.put((prompt_text, None))

    except RuntimeError as e:
        tg_send(f"⚠️ {e}")
        return
    except Exception as e:
        log_error("voice transcription failed", e)
        tg_send("🎤 Ошибка транскрипции — голосовое не обработано")
        return
    finally:
        try:
            os.remove(local_path)
        except OSError:
            pass
    return  # consumed by voice branch — do not fall through
```

**Note:** `_voice_mod._whisper_model` reads the live module attribute (works correctly because `_voice_mod` is a module reference, not a value copy).

## Data Flow

```
voice msg received
    ↓
download_tg_file() → None?       → return (silent)
    ↓
whisper not installed?           → tg_send("📦 Устанавливаю...")
model not loaded?                → tg_send("⏳ Загружаю модель...")
    ↓
transcribe_voice(path)
    ├─ ffmpeg missing            → RuntimeError → tg_send + return
    ├─ pip fails                 → RuntimeError → tg_send + return
    ├─ transcription exception   → Exception   → log + tg_send + return
    ├─ empty result ("")         → tg_send("🎤 Речь не распознана") + return
    └─ success: "текст..."
    ↓
_request_queue.put(("[Голосовое сообщение]: текст...", None))
    ↓
finally: os.remove(local_path)   # always runs
    ↓
return
```

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `download_tg_file` returns None | `return` silently |
| ffmpeg not installed | `tg_send("⚠️ ffmpeg not found: sudo apt install ffmpeg")` + return |
| pip install fails | `tg_send("⚠️ pip install openai-whisper failed (rc=1): ...")` + return |
| Whisper exception (e.g. corrupt OGG) | `log_error` + `tg_send("🎤 Ошибка транскрипции")` + return |
| Empty transcription (silent audio) | `tg_send("🎤 Речь не распознана")` + return |
| Success | `_request_queue.put((prompt_text, None))` + cleanup |
| Temp file cleanup | Always in `finally` block |

## Known Limitations

- **Language:** Russian only (`language="ru"`). Non-Russian audio transcribed with degraded accuracy.
- **`audio` messages:** Only Telegram `voice` type (OGG/Opus PTT recordings) is handled. Regular `audio` messages (music files) are not transcribed — unchanged from current behavior.

## Dependencies

- `openai-whisper` — auto-installed on first voice message (blocks polling thread ~30-120s, user notified)
- `ffmpeg` — system package; checked at runtime with clear error message
- Whisper `base` model — ~140MB, auto-downloaded to `~/.cache/whisper/` on first transcription

## Testing (`tests/test_voice.py`)

- `test_transcribe_success` — mock whisper, returns text → assert correct string returned
- `test_transcribe_empty_result` — mock whisper returns `{"text": "  "}` → assert `""` returned (no exception)
- `test_transcribe_no_ffmpeg` — mock `shutil.which` → None → assert `RuntimeError("ffmpeg")` raised
- `test_transcribe_model_cached` — call twice, assert `load_model` called once
- `test_transcribe_whisper_exception` — mock `transcribe()` raises → assert exception propagates
- `test_ensure_whisper_already_installed` — whisper importable → assert no subprocess call
- `test_ensure_whisper_pip_ok` — `ImportError` then pip succeeds → assert module returned
- `test_ensure_whisper_pip_fails` — pip returns non-zero → assert `RuntimeError` raised
