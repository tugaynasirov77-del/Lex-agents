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

from services import (
    heygen_client,
    whisper_client,
    ffmpeg_processor,
    supabase_storage,
    lexai_client,
    source_downloader,
    karaoke,
    reel_presets,
)

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
    project_id = job["project_id"]
    mode = job.get("mode", "avatar")
    overlays = job.get("overlays") or []
    preset = reel_presets.get_preset(job.get("preset"))
    user_selections = job.get("user_selections")
    transcript_words = job.get("transcript_words")

    has_selections = bool(user_selections and isinstance(user_selections, dict))
    log.info("=== job %s mode=%s preset=%s has_selections=%s (draft %s) ===",
             job_id, mode, preset.name, has_selections, draft_id)

    with tempfile.TemporaryDirectory(prefix=f"reel-{job_id[:8]}-") as tmp:
        raw_mp4 = os.path.join(tmp, "raw.mp4")
        ass_path = os.path.join(tmp, "subs.ass")
        out_mp4 = os.path.join(tmp, "out.mp4")
        cover_jpg = os.path.join(tmp, "cover.jpg")

        # ============== PHASE 1: транскрипция (если ещё не делали) ==============
        if not has_selections:
            lexai_client.report_progress(job_id, status="rendering", phase="download")
            if mode == "from_upload":
                src_url = job.get("source_video_url")
                if not src_url:
                    raise RuntimeError("from_upload job has no source_video_url")
                source_downloader.download_source_video(src_url, raw_mp4)
            else:
                script: str = job.get("script") or ""
                if not script.strip():
                    raise RuntimeError("avatar job has empty script")
                video_id = heygen_client.create_video(script)
                lexai_client.report_progress(job_id, heygen_video_id=video_id)
                result = heygen_client.poll_until_ready(video_id)
                if not result.video_url:
                    raise RuntimeError("HeyGen returned empty video_url")
                heygen_client.download_video(result.video_url, raw_mp4)

            lexai_client.report_progress(job_id, phase="transcribe")
            words = whisper_client.transcribe_to_words(raw_mp4, language="ru")
            if not words:
                raise RuntimeError("whisper returned no words")

            # Сохраняем word-level транскрипт и ждём юзера
            lexai_client.report_progress(
                job_id,
                status="awaiting_approval",
                phase="awaiting_approval",
                transcript_words=words,
            )
            log.info("=== job %s PHASE 1 DONE — awaiting user approval ===", job_id)
            return

        # ============== PHASE 2: рендер по выборке юзера ==============
        if not transcript_words:
            raise RuntimeError("phase 2 but transcript_words missing")

        # скачиваем raw mp4 заново (Phase 1 уже скачивал, но за tmp не сохранили)
        lexai_client.report_progress(job_id, status="rendering", phase="download")
        if mode == "from_upload":
            source_downloader.download_source_video(job["source_video_url"], raw_mp4)
        else:
            # для avatar Phase 2 потребует кэширования HeyGen-видео где-то.
            # Пока поддерживаем только from_upload в новом 2-фазном workflow.
            raise RuntimeError("Phase 2 supports only from_upload mode for now")

        # Строим ASS на основе пользовательской выборки
        lexai_client.report_progress(job_id, phase="render")
        key_indices = set(user_selections.get("key_indices") or [])
        animation = user_selections.get("animation", "slide_up")
        karaoke.build_user_ass(
            transcript_words=transcript_words,
            key_indices=key_indices,
            animation=animation,
            out_path=ass_path,
            preset=preset,
        )

        ffmpeg_processor.render_reel(
            input_video=raw_mp4,
            subs_path=ass_path,
            overlays=overlays,
            music_path=BG_MUSIC_PATH if BG_MUSIC_PATH and os.path.exists(BG_MUSIC_PATH) else None,
            output_path=out_mp4,
            hook_zoom=True,
            preset=preset,
            sfx_events=[],
        )
        ffmpeg_processor.extract_cover(out_mp4, cover_jpg, at_second=1.5)

        # Upload
        lexai_client.report_progress(job_id, phase="upload")
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
