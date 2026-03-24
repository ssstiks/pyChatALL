#!/usr/bin/env python3
"""
Voice message transcription using OpenAI Whisper.
No imports from project modules — stdlib + optional third-party only.
"""
import importlib
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
        [sys.executable, "-m", "pip", "install", "openai-whisper", "-q",
         "--break-system-packages"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip install openai-whisper failed (rc={proc.returncode}): "
            f"{proc.stderr.decode()[:200]}"
        )
    importlib.invalidate_caches()
    try:
        import whisper as _w
    except ImportError as e:
        raise RuntimeError(f"openai-whisper installed but import failed: {e}") from e
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

    if _whisper_mod is None:
        _ensure_whisper()

    if _whisper_model is None:
        _whisper_model = _whisper_mod.load_model("base")

    result = _whisper_model.transcribe(file_path, language="ru")
    return result["text"].strip()
