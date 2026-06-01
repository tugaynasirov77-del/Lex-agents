"""Загрузка готовых файлов (mp4, jpg) в Supabase Storage.

Используется service_role key (не публичный anon).
Бакет должен быть создан заранее (см. README).
"""
from __future__ import annotations

import os
import logging
import mimetypes

import httpx

log = logging.getLogger(__name__)


class StorageError(RuntimeError):
    pass


def _base() -> str:
    url = os.environ.get("SUPABASE_URL")
    if not url:
        raise StorageError("SUPABASE_URL not set")
    return url.rstrip("/")


def _service_key() -> str:
    k = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not k:
        raise StorageError("SUPABASE_SERVICE_ROLE_KEY not set")
    return k


def _bucket() -> str:
    return os.environ.get("SUPABASE_STORAGE_BUCKET", "reels")


def upload_file(local_path: str, remote_path: str, *, content_type: str | None = None) -> str:
    """Заливает файл, возвращает public URL.

    Бакет ожидается public (или используется signed URL — см. публичный путь).
    """
    if not os.path.exists(local_path):
        raise StorageError(f"file not found: {local_path}")
    ct = content_type or mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    bucket = _bucket()
    url = f"{_base()}/storage/v1/object/{bucket}/{remote_path}"

    with open(local_path, "rb") as f:
        data = f.read()

    with httpx.Client(timeout=180.0) as client:
        r = client.post(
            url,
            content=data,
            headers={
                "Authorization": f"Bearer {_service_key()}",
                "Content-Type": ct,
                "x-upsert": "true",
            },
        )
    if r.status_code >= 400:
        raise StorageError(f"upload failed {r.status_code}: {r.text[:300]}")

    public_url = f"{_base()}/storage/v1/object/public/{bucket}/{remote_path}"
    log.info("Uploaded → %s", public_url)
    return public_url
