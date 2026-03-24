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


@pytest.mark.skip(reason="requires whisper absent from sys.modules; builtins.__import__ patch unreliable in Python 3.14")
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
