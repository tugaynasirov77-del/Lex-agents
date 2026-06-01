"""HeyGen API клиент. Генерация avatar-видео из текстового скрипта.

Docs: https://docs.heygen.com/reference/create-an-avatar-video-v2
"""
from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)

HEYGEN_BASE = "https://api.heygen.com"


@dataclass
class HeyGenResult:
    video_id: str
    video_url: str
    duration_seconds: float | None = None


class HeyGenError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("HEYGEN_API_KEY")
    if not key:
        raise HeyGenError("HEYGEN_API_KEY not set")
    return key


def _avatar_id() -> str:
    av = os.environ.get("HEYGEN_AVATAR_ID")
    if not av:
        raise HeyGenError("HEYGEN_AVATAR_ID not set")
    return av


def _voice_id() -> str:
    v = os.environ.get("HEYGEN_VOICE_ID")
    if not v:
        raise HeyGenError("HEYGEN_VOICE_ID not set")
    return v


def create_video(script: str, *, ratio: str = "9:16") -> str:
    """Создаёт задачу на генерацию. Возвращает video_id."""
    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "avatar",
                    "avatar_id": _avatar_id(),
                    "avatar_style": "normal",
                },
                "voice": {
                    "type": "text",
                    "input_text": script,
                    "voice_id": _voice_id(),
                },
                "background": {"type": "color", "value": "#0F1117"},
            }
        ],
        "dimension": {"width": 1080, "height": 1920} if ratio == "9:16" else {"width": 1920, "height": 1080},
    }
    with httpx.Client(timeout=60.0) as client:
        r = client.post(
            f"{HEYGEN_BASE}/v2/video/generate",
            json=payload,
            headers={"X-Api-Key": _api_key(), "Content-Type": "application/json"},
        )
    if r.status_code >= 400:
        raise HeyGenError(f"HeyGen create failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    vid = (data.get("data") or {}).get("video_id")
    if not vid:
        raise HeyGenError(f"HeyGen create returned no video_id: {data}")
    log.info("HeyGen video task created: %s", vid)
    return vid


def poll_until_ready(video_id: str, *, timeout_seconds: int = 600, interval: int = 15) -> HeyGenResult:
    """Polling каждые `interval` секунд до status=completed или failed."""
    deadline = time.time() + timeout_seconds
    last_status = None
    while time.time() < deadline:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                f"{HEYGEN_BASE}/v1/video_status.get",
                params={"video_id": video_id},
                headers={"X-Api-Key": _api_key()},
            )
        if r.status_code >= 400:
            raise HeyGenError(f"HeyGen status failed {r.status_code}: {r.text[:300]}")
        body = r.json().get("data") or {}
        status = body.get("status")
        if status != last_status:
            log.info("HeyGen %s: %s", video_id, status)
            last_status = status
        if status == "completed":
            return HeyGenResult(
                video_id=video_id,
                video_url=body.get("video_url") or "",
                duration_seconds=body.get("duration"),
            )
        if status in ("failed", "error"):
            raise HeyGenError(f"HeyGen render failed: {body}")
        time.sleep(interval)
    raise HeyGenError(f"HeyGen timeout after {timeout_seconds}s")


def download_video(url: str, dest_path: str) -> None:
    """Стримим видео в файл."""
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
    log.info("Downloaded HeyGen video → %s", dest_path)
