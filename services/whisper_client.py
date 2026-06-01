"""Whisper-транскрипция через Vercel-proxy.

OpenAI блокирует российский IP Timeweb → ходим через Vercel
(/api/ig/transcribe, хост US/EU).

Сначала FFmpeg извлекает аудио в mp3 (~500 КБ для 90 сек),
потом multipart-POST на Vercel.
"""
from __future__ import annotations

import os
import logging
import subprocess
import tempfile

import httpx

log = logging.getLogger(__name__)


class WhisperError(RuntimeError):
    pass


def _proxy_base() -> str:
    return os.environ.get("LEXAI_API_BASE", "https://lex-ai-miniapp.vercel.app").rstrip("/")


def _worker_secret() -> str:
    s = os.environ.get("WORKER_SECRET")
    if not s:
        raise WhisperError("WORKER_SECRET not set")
    return s


def _extract_audio(video_path: str, out_mp3: str) -> str:
    """FFmpeg: вытаскиваем аудио-дорожку в mp3 64kbps mono — ~500 КБ за 90 сек."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "64k",
        "-ac", "1",
        "-ar", "16000",
        out_mp3,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WhisperError(f"audio extract failed: {proc.stderr[-400:]}")
    if not os.path.exists(out_mp3) or os.path.getsize(out_mp3) < 100:
        raise WhisperError("extracted audio too small")
    return out_mp3


def transcribe_to_srt(audio_or_video_path: str, *, language: str = "ru") -> str:
    """Возвращает SRT-текст. Принимает mp4 (извлечёт аудио) или готовый mp3/wav."""
    is_audio = audio_or_video_path.lower().endswith((".mp3", ".wav", ".m4a", ".ogg"))

    with tempfile.TemporaryDirectory(prefix="whisper-") as tmp:
        if is_audio:
            audio_path = audio_or_video_path
        else:
            audio_path = _extract_audio(audio_or_video_path, os.path.join(tmp, "audio.mp3"))
            log.info("extracted audio: %.1f KB", os.path.getsize(audio_path) / 1024)

        with open(audio_path, "rb") as f:
            files = {"file": ("audio.mp3", f, "audio/mpeg")}
            data = {"language": language}
            with httpx.Client(timeout=120.0) as client:
                r = client.post(
                    f"{_proxy_base()}/api/ig/transcribe",
                    headers={"x-worker-secret": _worker_secret()},
                    data=data,
                    files=files,
                )
    if r.status_code >= 400:
        raise WhisperError(f"Whisper proxy {r.status_code}: {r.text[:300]}")
    srt = r.text
    if not srt.strip():
        raise WhisperError("Whisper returned empty SRT")
    log.info("Whisper SRT: %d bytes", len(srt))
    return srt
