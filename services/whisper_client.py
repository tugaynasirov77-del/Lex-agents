"""OpenAI Whisper для генерации SRT-субтитров из видео/аудио.

Docs: https://platform.openai.com/docs/api-reference/audio
"""
from __future__ import annotations

import os
import logging

import httpx

log = logging.getLogger(__name__)

OPENAI_BASE = "https://api.openai.com/v1"


class WhisperError(RuntimeError):
    pass


def _api_key() -> str:
    k = os.environ.get("OPENAI_API_KEY")
    if not k:
        raise WhisperError("OPENAI_API_KEY not set")
    return k


def transcribe_to_srt(audio_or_video_path: str, *, language: str = "ru") -> str:
    """Принимает путь к mp4/mp3/wav, возвращает SRT-текст."""
    with open(audio_or_video_path, "rb") as f:
        files = {"file": (os.path.basename(audio_or_video_path), f, "application/octet-stream")}
        data = {
            "model": "whisper-1",
            "response_format": "srt",
            "language": language,
        }
        with httpx.Client(timeout=180.0) as client:
            r = client.post(
                f"{OPENAI_BASE}/audio/transcriptions",
                headers={"Authorization": f"Bearer {_api_key()}"},
                data=data,
                files=files,
            )
    if r.status_code >= 400:
        raise WhisperError(f"Whisper failed {r.status_code}: {r.text[:300]}")
    srt = r.text
    if not srt.strip():
        raise WhisperError("Whisper returned empty SRT")
    log.info("Whisper SRT: %d bytes", len(srt))
    return srt
