"""Скачивает сырое видео клиента из приватного бакета Supabase raw-uploads."""
from __future__ import annotations

import os
import logging

import httpx

log = logging.getLogger(__name__)


def download_source_video(url: str, dest_path: str) -> None:
    """url имеет формат https://<project>.supabase.co/storage/v1/object/raw-uploads/<path>.

    Приватный bucket — авторизация через service_role.
    """
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["apikey"] = key

    with httpx.Client(timeout=300.0, follow_redirects=True) as client:
        with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=128 * 1024):
                    f.write(chunk)
    size = os.path.getsize(dest_path)
    log.info("Downloaded source video: %s (%.1f MB)", dest_path, size / 1_048_576)
