"""Михаил Reels-maker — главный воркер.

Этап 2 контент-фабрики LEX AI.

Loop:
  1. Pulls 1 pending job из Mini App API (/api/ig/reel-jobs/next)
  2. HeyGen: создаёт avatar-видео из script
  3. Polling до completion → download mp4
  4. Whisper: транскрипция → SRT
  5. FFmpeg: burn SRT + overlays + bg music → 1080×1920 MP4
  6. Upload финального mp4 + cover в Supabase Storage
  7. Report success → Mini App пишет video_url в content_draft
  8. На ошибке: report_failure (с авто-ретраем до 3 раз)

Запуск: python reels_worker.py
Procfile: reels: python reels_worker.py
"""
from __future__ import annotations

import os
import sys
import time
import socket
import tempfile
import logging
import traceback

from services import heygen_client, whisper_client, ffmpeg_processor, supabase_storage, lexai_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("reels_worker")

WORKER_ID = f"reels-{socket.gethostname()}-{os.getpid()}"
POLL_INTERVAL = int(os.environ.get("REEL_POLL_INTERVAL", "20"))  # сек между пуллами
BG_MUSIC_PATH = os.environ.get("BACKGROUND_MUSIC_PATH")  # опц. локальный mp3


def process_job(job: dict) -> None:
    job_id = job["id"]
    draft_id = job["draft_id"]
    script: str = job["script"]
    overlays = job.get("overlays") or []
    log.info("=== job %s (draft %s) ===", job_id, draft_id)

    with tempfile.TemporaryDirectory(prefix=f"reel-{job_id[:8]}-") as tmp:
        raw_mp4 = os.path.join(tmp, "raw.mp4")
        srt_path = os.path.join(tmp, "subs.srt")
        out_mp4 = os.path.join(tmp, "out.mp4")
        cover_jpg = os.path.join(tmp, "cover.jpg")

        # 1) HeyGen
        lexai_client.report_progress(job_id, status="rendering")
        video_id = heygen_client.create_video(script)
        lexai_client.report_progress(job_id, heygen_video_id=video_id)
        result = heygen_client.poll_until_ready(video_id)
        if not result.video_url:
            raise RuntimeError("HeyGen returned empty video_url")
        heygen_client.download_video(result.video_url, raw_mp4)

        # 2) Whisper
        srt = whisper_client.transcribe_to_srt(raw_mp4, language="ru")
        ffmpeg_processor.write_srt(srt, srt_path)
        lexai_client.report_progress(job_id, srt_text=srt[:8000])

        # 3) FFmpeg
        ffmpeg_processor.render_reel(
            input_video=raw_mp4,
            srt_path=srt_path,
            overlays=overlays,
            music_path=BG_MUSIC_PATH if BG_MUSIC_PATH and os.path.exists(BG_MUSIC_PATH) else None,
            output_path=out_mp4,
        )
        ffmpeg_processor.extract_cover(out_mp4, cover_jpg, at_second=1.5)

        # 4) Upload
        remote_video = f"{draft_id}/reel.mp4"
        remote_cover = f"{draft_id}/cover.jpg"
        video_url = supabase_storage.upload_file(out_mp4, remote_video, content_type="video/mp4")
        cover_url = supabase_storage.upload_file(cover_jpg, remote_cover, content_type="image/jpeg")

        # 5) Report success
        lexai_client.report_success(job_id, video_url=video_url, cover_url=cover_url)
        log.info("=== job %s DONE → %s ===", job_id, video_url)


def main() -> int:
    log.info("Reels worker starting as %s", WORKER_ID)
    log.info("Poll interval: %ds. LEXAI_API_BASE=%s", POLL_INTERVAL, os.environ.get("LEXAI_API_BASE", "default"))

    while True:
        try:
            job = lexai_client.claim_next_job(WORKER_ID)
        except Exception as e:
            log.warning("claim failed: %s", e)
            time.sleep(POLL_INTERVAL)
            continue

        if not job:
            time.sleep(POLL_INTERVAL)
            continue

        job_id = job.get("id", "?")
        try:
            process_job(job)
        except Exception as e:
            tb = traceback.format_exc()
            log.error("job %s failed: %s\n%s", job_id, e, tb)
            try:
                lexai_client.report_failure(job_id, error=f"{type(e).__name__}: {e}")
            except Exception as e2:
                log.error("could not report failure: %s", e2)

        # короткая пауза между задачами
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main() or 0)
