# Voice Messages Design

**Date:** 2026-03-24
**Feature:** #1 Voice message transcription for pyChatALL Telegram bot

## Overview

When a user sends a voice message to the bot, it is automatically transcribed using OpenAI Whisper and forwarded to the active AI agent as text. Dependencies (whisper, ffmpeg) are auto-installed if missing.

## Architecture

A new standalone module `voice.py` handles transcription. It has **no imports from any project module** — only stdlib and optional third-party packages.

Transcription happens in **`_queue_worker`** (not the polling thread), so the polling loop stays responsive while Whisper runs. `process_update()` only downloads the file and queues it as `("", local_path)`. `route_and_reply()` detects a voice file by path suffix and transcribes before routing.

```
process_update() [polling thread]
    ↓
download_tg_file() → local_path (*_voice.ogg)
    ↓
_request_queue.put(("", local_path))   # fast, no blocking

_queue_worker [worker thread]
    ↓
route_and_reply("", local_path)
    ↓
detect voice file → status tg_send → transcribe_voice(local_path)
    ↓
route to active agent with transcribed text
```

**Thread safety:** `_queue_worker` is a single thread — `transcribe_voice()` is never called concurrently. `_whisper_model` needs no lock.

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
4. Call `importlib.invalidate_caches()` so Python discovers the freshly installed package
5. `import whisper`, set `_whisper_mod`, return it

**`transcribe_voice(file_path: str) -> str`**

1. Check `shutil.which("ffmpeg")` — if None, raise `RuntimeError("ffmpeg not found: sudo apt install ffmpeg")`
2. Call `_ensure_whisper()` — raises `RuntimeError` on failure; sets `_whisper_mod`
3. If `_whisper_model is None`: `_whisper_model = _whisper_mod.load_model("base")`
4. `result = _whisper_model.transcribe(file_path, language="ru")`
5. Return `result["text"].strip()` — may be empty string, never raises for empty
6. Any exception from step 4 propagates to caller

**`is_voice_file(file_path: str) -> bool`**

Returns `True` if `os.path.basename(file_path)` matches `*_voice.ogg` pattern. Used by `route_and_reply()` to detect queued voice files.

**Language:** Fixed `"ru"`. Intentional — bot is Russian. Known limitation: non-Russian audio transcribed with degraded accuracy.

### `tg_agent.py` — `process_update()` voice block

**Integration point:** Completely **replaces** the existing `elif voice:` branch (lines ~1180-1183). Must always `return` — never falls through to the generic `_request_queue.put()` at end of `process_update()`.

```python
elif voice:
    local_path = download_tg_file(voice["file_id"], "voice.ogg")
    if not local_path:
        return  # download failed — silent skip
    _request_queue.put(("", local_path))
    if _worker_busy.is_set() or _request_queue.qsize() > 0:
        tg_send("📋 В очереди (голосовое)")
    return
```

`voice.py` is imported at module level:
```python
import voice as _voice_mod   # module-level import
```

### `tg_agent.py` — `route_and_reply()` voice detection

At the **top** of `route_and_reply()`, before routing logic, add voice detection:

```python
def route_and_reply(text: str, file_path: str | None = None) -> None:
    # Voice file transcription (runs in _queue_worker thread)
    if file_path and _voice_mod.is_voice_file(file_path):
        try:
            import importlib.util
            if importlib.util.find_spec("whisper") is None:
                tg_send("📦 Устанавливаю Whisper (первый раз, ~30 сек)...")
            elif _voice_mod._whisper_model is None:
                tg_send("⏳ Загружаю модель Whisper (первый запуск, ~1 мин)...")

            transcribed = _voice_mod.transcribe_voice(file_path)
        except RuntimeError as e:
            tg_send(f"⚠️ {e}")
            return
        except Exception as e:
            log_error("voice transcription failed", e)
            tg_send("🎤 Ошибка транскрипции")
            return
        finally:
            try:
                os.remove(file_path)
            except OSError:
                pass

        if not transcribed:
            tg_send("🎤 Речь не распознана")
            return

        text = f"[Голосовое сообщение]: {transcribed}"
        file_path = None

    # ... existing routing logic continues unchanged ...
```

**Note:** `_voice_mod._whisper_model` reads the live module attribute correctly because `_voice_mod` is a module reference.

## Data Flow

```
voice msg received [polling thread]
    ↓
download_tg_file() → None?           → return (silent)
    ↓
_request_queue.put(("", local_path)) → return immediately

[_queue_worker thread picks up item]
    ↓
route_and_reply("", local_path)
    ↓
is_voice_file(local_path)?           → yes
    ↓
whisper not installed?               → tg_send("📦 Устанавливаю...")
model not loaded?                    → tg_send("⏳ Загружаю модель...")
    ↓
transcribe_voice(path)
    ├─ ffmpeg missing                → RuntimeError → tg_send + return
    ├─ pip fails                     → RuntimeError → tg_send + return
    ├─ transcription exception       → Exception   → log + tg_send + return
    └─ success / empty
    ↓
finally: os.remove(local_path)       # always
    ↓
empty?                               → tg_send("🎤 Речь не распознана") + return
    ↓
text = "[Голосовое сообщение]: ..."
file_path = None
    ↓
existing routing logic (active agent)
```

## Error Handling

| Condition | Behavior |
|-----------|----------|
| `download_tg_file` returns None | `return` silently from `process_update` |
| ffmpeg not installed | `tg_send("⚠️ ffmpeg not found: sudo apt install ffmpeg")` + return |
| pip install fails | `tg_send("⚠️ pip install openai-whisper failed (rc=N): ...")` + return |
| pip succeeds but package invisible | `importlib.invalidate_caches()` ensures import works without restart |
| Whisper exception (e.g. corrupt OGG) | `log_error` + `tg_send("🎤 Ошибка транскрипции")` + return |
| Empty transcription (silent audio) | `tg_send("🎤 Речь не распознана")` + return |
| Success | Continue to existing routing with transcribed text |
| Temp file cleanup | Always in `finally` block |

## Known Limitations

- **Language:** Russian only (`language="ru"`). Non-Russian audio transcribed with degraded accuracy.
- **`audio` messages:** Only Telegram `voice` type (OGG/Opus PTT recordings) is handled. Regular `audio` messages (music files) are not transcribed — unchanged from current behavior.
- **First-time latency:** pip install (~30-120s) and model download (~1 min) block the queue worker on first voice message. Subsequent messages: ~5-15s transcription. For personal use this is acceptable.

## Dependencies

- `openai-whisper` — auto-installed on first voice message (blocks queue worker, user notified)
- `ffmpeg` — system package; checked at runtime with clear error message
- Whisper `base` model — ~140MB, auto-downloaded to `~/.cache/whisper/` on first transcription

## Testing (`tests/test_voice.py`)

- `test_transcribe_success` — mock whisper, returns text → assert correct string returned
- `test_transcribe_empty_result` — mock whisper returns `{"text": "  "}` → assert `""` returned (no exception)
- `test_transcribe_no_ffmpeg` — mock `shutil.which` → None → assert `RuntimeError("ffmpeg")` raised
- `test_transcribe_model_cached` — call twice, assert `load_model` called once
- `test_transcribe_whisper_exception` — mock `transcribe()` raises → assert exception propagates
- `test_ensure_whisper_already_installed` — whisper importable → assert no subprocess call, caches not invalidated
- `test_ensure_whisper_pip_ok` — `ImportError` then pip succeeds → assert `invalidate_caches()` called, module returned
- `test_ensure_whisper_pip_fails` — pip returns non-zero → assert `RuntimeError` raised
- `test_is_voice_file` — assert True for `*_voice.ogg`, False for other extensions
