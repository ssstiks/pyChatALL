"""Voice utilities for STT and TTS support."""
import os
import logging
import subprocess
from typing import Optional

log = logging.getLogger(__name__)


def is_voice_file(file_path: str) -> bool:
    """Check if file is a voice file."""
    voice_extensions = {".ogg", ".mp3", ".wav", ".m4a", ".flac"}
    _, ext = os.path.splitext(file_path.lower())
    return ext in voice_extensions


def transcribe_voice(file_path: str) -> Optional[str]:
    """
    Transcribe voice file to text using Whisper.

    Args:
        file_path: Path to voice file

    Returns:
        Transcribed text or None if failed
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Voice file not found: {file_path}")

    try:
        import whisper
    except ImportError:
        raise RuntimeError("Whisper not installed. Install with: pip install openai-whisper")

    try:
        # Load model (will use GPU if available)
        model = whisper.load_model("base")
        result = model.transcribe(file_path)
        text = result.get("text", "").strip()
        return text if text else None
    except Exception as e:
        log.error(f"Whisper transcription error: {e}")
        raise RuntimeError(f"Failed to transcribe voice: {e}")


def synthesize_speech(text: str, output_path: str, language: str = "ru") -> bool:
    """
    Synthesize speech from text.

    Args:
        text: Text to synthesize
        output_path: Output audio file path
        language: Language code (ru, en, etc.)

    Returns:
        True if successful, False otherwise
    """
    try:
        # Try to use gTTS (Google Text-to-Speech)
        from gtts import gTTS

        tts = gTTS(text=text, lang=language, slow=False)
        tts.save(output_path)
        log.info(f"Speech synthesized: {output_path}")
        return True
    except ImportError:
        log.warning("gTTS not installed. Install with: pip install gtts")
        return False
    except Exception as e:
        log.error(f"Speech synthesis error: {e}")
        return False


def convert_audio_format(input_path: str, output_path: str, format: str = "wav") -> bool:
    """
    Convert audio to specific format using ffmpeg.

    Args:
        input_path: Input audio file
        output_path: Output audio file
        format: Target format (wav, mp3, ogg, etc.)

    Returns:
        True if successful, False otherwise
    """
    try:
        cmd = ["ffmpeg", "-i", input_path, "-y", output_path]
        subprocess.run(cmd, capture_output=True, check=True, timeout=30)
        return True
    except Exception as e:
        log.error(f"Audio conversion error: {e}")
        return False


def get_audio_duration(file_path: str) -> Optional[float]:
    """
    Get duration of audio file in seconds.

    Args:
        file_path: Path to audio file

    Returns:
        Duration in seconds or None if failed
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1:nokey=1",
             file_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        return float(result.stdout.strip()) if result.stdout else None
    except Exception as e:
        log.error(f"Error getting audio duration: {e}")
        return None
