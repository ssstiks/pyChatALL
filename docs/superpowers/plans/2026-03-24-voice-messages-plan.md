# Voice Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transcribe Telegram voice messages using Whisper and forward the text to the active agent, with auto-install of dependencies on first use.

**Architecture:** New standalone `voice.py` module (no project imports) handles detection, auto-install, and transcription. `tg_agent.py` changes in two places: `process_update()` queues voice files without blocking, and `route_and_reply()` detects and transcribes them before routing.

**Tech Stack:** Python stdlib (`subprocess`, `shutil`, `importlib`), `openai-whisper` (auto-installed), `ffmpeg` (system).

**Naming convention note:** `download_tg_file(file_id, "voice.ogg")` saves files as `{unix_ts}_voice.ogg` (see `ui.py:291`). `is_voice_file()` uses `endswith("_voice.ogg")` — this relies on that naming. Do not change the hint name.

---

### Task 1: Create `voice.py` — `is_voice_file` + `_ensure_whisper` + `transcribe_voice`

**Files:**
- Create: `voice.py`
- Create: `tests/test_voice.py`
- Create: `tests/conftest_voice.py` (optional fixture helper, inline instead)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_voice.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import importlib
import unittest.mock as mock
import pytest

import voice


@pytest.fixture(autouse=True)
def reset_voice_globals():
    """Reset module-level caches before each test."""
    voice._whisper_mod   = None
    voice._whisper_model = None
    yield
    voice._whisper_mod   = None
    voice._whisper_model = None


# ── is_voice_file ─────────────────────────────────────────────

def test_is_voice_file_true():
    assert voice.is_voice_file("/tmp/1234567890_voice.ogg") is True

def test_is_voice_file_false_jpg():
    assert voice.is_voice_file("/tmp/photo.jpg") is False

def test_is_voice_file_false_plain_ogg():
    assert voice.is_voice_file("/tmp/audio.ogg") is False

def test_is_voice_file_false_mp3():
    assert voice.is_voice_file("/tmp/1234_voice.mp3") is False


# ── _ensure_whisper ───────────────────────────────────────────

def test_ensure_whisper_already_installed():
    """If whisper is importable, no subprocess is called."""
    fake_whisper = mock.MagicMock()
    with mock.patch.dict("sys.modules", {"whisper": fake_whisper}):
        result = voice._ensure_whisper()
    assert result is fake_whisper


def test_ensure_whisper_pip_ok():
    """If whisper is not installed, pip runs and module is returned."""
    fake_whisper = mock.MagicMock()
    proc = mock.MagicMock()
    proc.returncode = 0

    # Remove whisper from sys.modules to simulate it not being installed
    sys.modules.pop("whisper", None)

    def fake_import(name, *args, **kwargs):
        if name == "whisper":
            return fake_whisper
        raise ImportError(f"No module named '{name}'")

    with mock.patch("subprocess.run", return_value=proc) as mock_run:
        with mock.patch("importlib.invalidate_caches") as mock_cache:
            with mock.patch("builtins.__import__", side_effect=fake_import):
                result = voice._ensure_whisper()

    mock_run.assert_called_once()
    mock_cache.assert_called_once()
    assert result is fake_whisper


def test_ensure_whisper_pip_fails():
    """If pip install fails (non-zero rc), RuntimeError is raised."""
    proc = mock.MagicMock()
    proc.returncode = 1
    proc.stderr = b"ERROR: Could not find a version"

    sys.modules.pop("whisper", None)

    with mock.patch("subprocess.run", return_value=proc):
        with pytest.raises(RuntimeError, match="pip install openai-whisper failed"):
            voice._ensure_whisper()


# ── transcribe_voice ──────────────────────────────────────────

def test_transcribe_no_ffmpeg():
    """RuntimeError if ffmpeg not found."""
    with mock.patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError, match="ffmpeg"):
            voice.transcribe_voice("/tmp/test_voice.ogg")


def test_transcribe_success():
    """Returns stripped text from whisper result."""
    fake_model = mock.MagicMock()
    fake_model.transcribe.return_value = {"text": "  привет мир  "}
    fake_whisper = mock.MagicMock()
    fake_whisper.load_model.return_value = fake_model

    with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        voice._whisper_mod = fake_whisper
        result = voice.transcribe_voice("/tmp/test_voice.ogg")

    assert result == "привет мир"
    fake_whisper.load_model.assert_called_once_with("base")
    fake_model.transcribe.assert_called_once_with("/tmp/test_voice.ogg", language="ru")


def test_transcribe_empty_result():
    """Empty/whitespace result returns empty string, no exception."""
    fake_model = mock.MagicMock()
    fake_model.transcribe.return_value = {"text": "   "}
    fake_whisper = mock.MagicMock()
    fake_whisper.load_model.return_value = fake_model

    with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        voice._whisper_mod = fake_whisper
        result = voice.transcribe_voice("/tmp/test_voice.ogg")

    assert result == ""


def test_transcribe_model_cached():
    """load_model is called only once across two calls."""
    fake_model = mock.MagicMock()
    fake_model.transcribe.return_value = {"text": "текст"}
    fake_whisper = mock.MagicMock()
    fake_whisper.load_model.return_value = fake_model

    with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        voice._whisper_mod = fake_whisper
        voice.transcribe_voice("/tmp/test_voice.ogg")
        voice.transcribe_voice("/tmp/test_voice.ogg")

    fake_whisper.load_model.assert_called_once()


def test_transcribe_whisper_exception():
    """Exception from transcribe() propagates to caller."""
    fake_model = mock.MagicMock()
    fake_model.transcribe.side_effect = RuntimeError("corrupt file")
    fake_whisper = mock.MagicMock()
    fake_whisper.load_model.return_value = fake_model

    with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
        voice._whisper_mod = fake_whisper
        with pytest.raises(RuntimeError, match="corrupt file"):
            voice.transcribe_voice("/tmp/test_voice.ogg")
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
cd /home/stx/Applications/progect/pyChatALL
python -m pytest tests/test_voice.py -v
```
Expected: `ModuleNotFoundError: No module named 'voice'`

- [ ] **Step 3: Create `voice.py`**

```python
#!/usr/bin/env python3
"""
Voice message transcription using OpenAI Whisper.
No imports from project modules — stdlib + optional third-party only.
"""
import importlib
import importlib.util
import os
import shutil
import subprocess
import sys

_whisper_mod   = None   # the whisper module, assigned by _ensure_whisper()
_whisper_model = None   # loaded whisper.Model, assigned on first transcription


def is_voice_file(file_path: str) -> bool:
    """True if file_path is a voice message downloaded as *_voice.ogg."""
    return os.path.basename(file_path).endswith("_voice.ogg")


def _ensure_whisper():
    """Import whisper, auto-installing via pip if missing. Returns whisper module."""
    global _whisper_mod
    try:
        import whisper as _w
        _whisper_mod = _w
        return _whisper_mod
    except ImportError:
        pass

    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "openai-whisper", "-q"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip install openai-whisper failed (rc={proc.returncode}): "
            f"{proc.stderr.decode()[:200]}"
        )
    importlib.invalidate_caches()
    import whisper as _w
    _whisper_mod = _w
    return _whisper_mod


def transcribe_voice(file_path: str) -> str:
    """
    Transcribe a voice OGG file to Russian text using Whisper.
    Returns stripped text (may be empty string for silent audio).
    Raises RuntimeError if ffmpeg missing or pip install fails.
    Any whisper exception propagates to caller.
    """
    global _whisper_model

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found: sudo apt install ffmpeg")

    _ensure_whisper()

    if _whisper_model is None:
        _whisper_model = _whisper_mod.load_model("base")

    result = _whisper_model.transcribe(file_path, language="ru")
    return result["text"].strip()
```

- [ ] **Step 4: Run all voice tests**

```bash
python -m pytest tests/test_voice.py -v
```
Expected: all tests PASS (except `test_ensure_whisper_pip_ok` may need `whisper` absent from env — skip if needed).

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```
Expected: all existing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add voice.py tests/test_voice.py
git commit -m "feat: add voice.py — Whisper transcription with auto-install"
```

---

### Task 2: Wire `voice.py` into `tg_agent.py`

**Files:**
- Modify: `tg_agent.py` line ~88 (add module import after `import team_mode`)
- Modify: `tg_agent.py` lines 1180-1183 (replace `elif voice:` block in `process_update`)
- Modify: `tg_agent.py` line ~468 (add voice detection block at top of `route_and_reply`, after the log line)

- [ ] **Step 1: Add module-level import**

After `import team_mode` (line ~88), add:

```python
import voice as _voice_mod
```

- [ ] **Step 2: Replace `elif voice:` in `process_update` (lines 1180-1183)**

Replace:
```python
    elif voice:
        file_path = download_tg_file(voice["file_id"], "voice.ogg")
        if not prompt_text:
            prompt_text = "Это голосовое сообщение (ogg). Укажи что можешь с ним сделать."
```

With:
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

- [ ] **Step 3: Add voice detection at top of `route_and_reply` (after the log line, line ~468)**

After:
```python
    log_info(f"MSG: {text[:120]!r}" + (f" + file:{os.path.basename(file_path)}" if file_path else ""))
```

Insert:
```python
    # ── Voice transcription (runs in _queue_worker thread) ────
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
    # ── end voice block ───────────────────────────────────────
```

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest tests/ -q
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tg_agent.py
git commit -m "feat: wire voice transcription into tg_agent — queue + route_and_reply detection"
```

---

### Task 3: Restart and smoke-test

- [ ] **Step 1: Restart bot**

```bash
kill $(cat /tmp/tg_agent.pid 2>/dev/null) 2>/dev/null; sleep 1
python3 /home/stx/Applications/progect/pyChatALL/tg_agent.py >> /tmp/tg_agent.log 2>&1 &
sleep 2 && tail -5 /tmp/tg_agent.log
```
Expected: `=== tg_agent запущен ===`

- [ ] **Step 2: Send a voice message**

First voice ever should show:
```
📦 Устанавливаю Whisper (первый раз, ~30 сек)...
```
Then after model download, active agent receives `[Голосовое сообщение]: текст транскрипции`.

- [ ] **Step 3: Check logs**

```bash
tail -20 /tmp/tg_agent.log
```
Expected: no tracebacks; entry like `[Голосовое сообщение]: ...` visible in prompt log.
