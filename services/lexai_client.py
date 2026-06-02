"""Клиент к Mini App API: пулл задачи + отчёт о результате/ошибке."""
from __future__ import annotations

import os
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LexAIError(RuntimeError):
    pass


def _base() -> str:
    return os.environ.get("LEXAI_API_BASE", "https://lex-ai-miniapp.vercel.app").rstrip("/")


def _secret() -> str:
    s = os.environ.get("WORKER_SECRET")
    if not s:
        raise LexAIError("WORKER_SECRET not set")
    return s


def _headers() -> dict[str, str]:
    return {
        "x-worker-secret": _secret(),
        "Content-Type": "application/json",
    }


def claim_next_job(worker_id: str) -> dict | None:
    """Возвращает job dict или None если пусто."""
    with httpx.Client(timeout=30.0) as client:
        r = client.post(
            f"{_base()}/api/ig/reel-jobs/next",
            json={"worker_id": worker_id},
            headers=_headers(),
        )
    if r.status_code >= 400:
        raise LexAIError(f"claim failed {r.status_code}: {r.text[:200]}")
    return r.json().get("job")


def report_progress(job_id: str, **patch: Any) -> None:
    with httpx.Client(timeout=15.0) as client:
        r = client.patch(
            f"{_base()}/api/ig/reel-jobs/{job_id}/status",
            json=patch,
            headers=_headers(),
        )
    if r.status_code >= 400:
        log.warning("progress report failed %s: %s", r.status_code, r.text[:200])


def report_success(job_id: str, *, video_url: str, cover_url: str | None = None) -> None:
    with httpx.Client(timeout=15.0) as client:
        r = client.post(
            f"{_base()}/api/ig/reel-jobs/{job_id}/status",
            json={"video_url": video_url, "cover_url": cover_url},
            headers=_headers(),
        )
    if r.status_code >= 400:
        raise LexAIError(f"success report failed {r.status_code}: {r.text[:200]}")


def caption_from_transcript(*, project_id: str, draft_id: str, transcript: str) -> dict:
    """Зовёт Алину через Mini App API. Возвращает {caption, overlays}."""
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            f"{_base()}/api/ig/caption-from-transcript",
            json={"project_id": project_id, "draft_id": draft_id, "transcript": transcript[:8000]},
            headers=_headers(),
        )
    if r.status_code >= 400:
        raise LexAIError(f"caption-from-transcript failed {r.status_code}: {r.text[:200]}")
    return r.json()


def emoji_for_words(words: list[str], context: str = "") -> dict[str, str]:
    """Зовёт Claude haiku через Mini App API. Возвращает {WORD: "🚀" или ""}."""
    if not words:
        return {}
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{_base()}/api/ig/emoji-for-words",
                json={"words": words, "context": context[:2000]},
                headers=_headers(),
            )
        if r.status_code >= 400:
            log.warning("emoji-for-words failed %s: %s", r.status_code, r.text[:200])
            return {}
        return r.json().get("emojis", {}) or {}
    except Exception as e:
        log.warning("emoji-for-words error: %s", e)
        return {}


def report_failure(job_id: str, *, error: str) -> None:
    with httpx.Client(timeout=15.0) as client:
        r = client.post(
            f"{_base()}/api/ig/reel-jobs/{job_id}/status",
            json={"error": error[:500]},
            headers=_headers(),
        )
    if r.status_code >= 400:
        log.warning("failure report failed %s: %s", r.status_code, r.text[:200])
