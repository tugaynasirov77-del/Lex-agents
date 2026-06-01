"""Whisper-транскрипция локально через whisper.cpp.

Бинарник: /opt/whisper.cpp/build/bin/whisper-cli
Модель:   /opt/whisper.cpp/models/ggml-small.bin (multilingual, ru)

Бесплатно, без квот, без блокировок IP.
"""
from __future__ import annotations

import os
import logging
import subprocess
import tempfile

log = logging.getLogger(__name__)

WHISPER_BIN = os.environ.get("WHISPER_BIN", "/opt/whisper.cpp/build/bin/whisper-cli")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "/opt/whisper.cpp/models/ggml-small.bin")


class WhisperError(RuntimeError):
    pass


def _extract_audio(video_path: str, out_wav: str) -> str:
    """whisper.cpp ест 16kHz mono WAV. FFmpeg конвертирует."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        out_wav,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise WhisperError(f"audio extract failed: {proc.stderr[-400:]}")
    if not os.path.exists(out_wav) or os.path.getsize(out_wav) < 100:
        raise WhisperError("extracted audio too small")
    return out_wav


def transcribe_to_srt(audio_or_video_path: str, *, language: str = "ru") -> str:
    """Возвращает SRT-текст (для legacy-сценариев). Word-level — отдельная функция."""
    if not os.path.exists(WHISPER_BIN):
        raise WhisperError(f"whisper-cli not found at {WHISPER_BIN}")
    if not os.path.exists(WHISPER_MODEL):
        raise WhisperError(f"model not found at {WHISPER_MODEL}")

    with tempfile.TemporaryDirectory(prefix="whisper-") as tmp:
        wav_path = os.path.join(tmp, "audio.wav")
        srt_base = os.path.join(tmp, "out")

        _extract_audio(audio_or_video_path, wav_path)
        log.info("wav: %.1f KB", os.path.getsize(wav_path) / 1024)

        cmd = [
            WHISPER_BIN,
            "-m", WHISPER_MODEL,
            "-f", wav_path,
            "-l", language,
            "-osrt",
            "-of", srt_base,
            "-t", "2",
            "-ml", "18",
            "--no-prints",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise WhisperError(f"whisper-cli failed: {proc.stderr[-500:]}")

        srt_path = f"{srt_base}.srt"
        if not os.path.exists(srt_path):
            raise WhisperError(f"whisper did not produce SRT (stderr: {proc.stderr[-300:]})")
        with open(srt_path, "r", encoding="utf-8") as f:
            srt = f.read()

    if not srt.strip():
        raise WhisperError("whisper returned empty SRT")
    log.info("SRT: %d bytes", len(srt))
    return srt


def transcribe_to_words(audio_or_video_path: str, *, language: str = "ru") -> list[dict]:
    """Word-level транскрипт. Возвращает [{idx, w, start_ms, end_ms}]."""
    import re as _re
    if not os.path.exists(WHISPER_BIN):
        raise WhisperError(f"whisper-cli not found at {WHISPER_BIN}")

    with tempfile.TemporaryDirectory(prefix="whisper-w-") as tmp:
        wav_path = os.path.join(tmp, "audio.wav")
        srt_base = os.path.join(tmp, "out")
        _extract_audio(audio_or_video_path, wav_path)

        # -ml 1 + --split-on-word — каждый сегмент = одно слово
        cmd = [
            WHISPER_BIN,
            "-m", WHISPER_MODEL,
            "-f", wav_path,
            "-l", language,
            "-osrt",
            "-of", srt_base,
            "-t", "2",
            "-ml", "1",
            "--split-on-word",
            "--no-prints",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise WhisperError(f"whisper-cli failed: {proc.stderr[-500:]}")

        srt_path = f"{srt_base}.srt"
        with open(srt_path, "r", encoding="utf-8") as f:
            srt = f.read()

    # парсим SRT в [{idx,w,start_ms,end_ms}]
    pattern = _re.compile(
        r"(\d+)\s*\n(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n([\s\S]*?)(?=\n\s*\n|\Z)",
        _re.M,
    )
    punct_re = _re.compile(r"[.,!?;:\"'«»()\[\]…]+")

    words: list[dict] = []
    idx = 0
    for m in pattern.finditer(srt):
        h1, m1, s1, ms1 = int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        h2, m2, s2, ms2 = int(m.group(6)), int(m.group(7)), int(m.group(8)), int(m.group(9))
        start_ms = (h1 * 3600 + m1 * 60 + s1) * 1000 + ms1
        end_ms = (h2 * 3600 + m2 * 60 + s2) * 1000 + ms2
        raw = m.group(10).strip().replace("\n", " ")
        # whisper -ml1 иногда даёт пробельный или пунктуационный сегмент — пропускаем
        clean = punct_re.sub("", raw).strip()
        if not clean or len(clean) < 2:
            continue
        words.append({
            "idx": idx,
            "w": clean,
            "start_ms": start_ms,
            "end_ms": end_ms,
        })
        idx += 1

    log.info("word-level: %d words", len(words))
    return words
