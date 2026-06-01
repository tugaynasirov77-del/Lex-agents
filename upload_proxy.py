"""Upload-proxy: iOS Telegram WebView блокирует cross-origin PUT в Supabase.

Решение: клиент льёт mp4 на наш HTTPS-эндпойнт (Cloudflare Tunnel → этот сервис),
мы стримим в Supabase Storage с service_role ключом.

Flow:
  1. Mini App → POST /api/projects/[id]/ig/reels/upload-url (Vercel)
     → возвращает {proxy_url, upload_token, storage_path}
  2. Mini App → POST {proxy_url}/upload с file + Headers:
       x-upload-token: HMAC-подписанный токен (WORKER_SECRET)
       x-storage-path: путь в bucket raw-uploads
     → возвращает {source_video_url}
  3. Mini App → POST /api/projects/[id]/ig/reels с source_video_url (Vercel)
     → создаёт draft + job (как раньше)

Запуск: python upload_proxy.py
Слушает на 127.0.0.1:8080, наружу через Cloudflare Tunnel.
"""
from __future__ import annotations

import os
import hmac
import hashlib
import time
import logging
import asyncio
import subprocess
import tempfile

import httpx
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("upload_proxy")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SR_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
BUCKET = "raw-uploads"
MAX_BYTES = 100 * 1024 * 1024     # 100 MB — клиент может слать большой исходник
COMPRESS_THRESHOLD = 40 * 1024 * 1024  # > 40 МБ — пережимаем перед загрузкой в Supabase
SUPABASE_LIMIT = 50 * 1024 * 1024  # лимит free tier


def verify_token(token: str, storage_path: str) -> bool:
    """Token format: <exp>.<hmac>
    hmac = HMAC-SHA256(WORKER_SECRET, f"{storage_path}|{exp}")
    """
    if not token or not WORKER_SECRET:
        return False
    try:
        exp_str, sig = token.split(".", 1)
        exp = int(exp_str)
    except Exception:
        return False
    if exp < int(time.time()):
        return False
    expected = hmac.new(
        WORKER_SECRET.encode(),
        f"{storage_path}|{exp}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


async def upload(request: web.Request) -> web.Response:
    token = request.headers.get("x-upload-token", "")
    storage_path = request.headers.get("x-storage-path", "")
    if not storage_path or ".." in storage_path or storage_path.startswith("/"):
        return web.json_response({"error": "invalid storage_path"}, status=400)
    if not verify_token(token, storage_path):
        return web.json_response({"error": "invalid token"}, status=401)

    # читаем body чанками, чтобы не держать в RAM
    content_type = request.headers.get("content-type", "video/mp4")
    if content_type.startswith("multipart"):
        return web.json_response({"error": "send raw body, not multipart"}, status=400)

    total = 0
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name
        async for chunk in request.content.iter_chunked(256 * 1024):
            total += len(chunk)
            if total > MAX_BYTES:
                os.unlink(tmp_path)
                return web.json_response({"error": "file > 100 MB"}, status=413)
            tmp.write(chunk)

    if total == 0:
        os.unlink(tmp_path)
        return web.json_response({"error": "empty body"}, status=400)

    log.info("received %d bytes → %s", total, storage_path)

    upload_path = tmp_path
    final_size = total

    # Если файл крупнее лимита Supabase (50 МБ) — компрессим через FFmpeg
    if total > COMPRESS_THRESHOLD:
        compressed = tmp_path + ".compressed.mp4"
        log.info("compressing %.1f MB → target ~30 MB", total / 1_048_576)
        # crf 28, preset fast, max bitrate 2M — выходит ~2-3 МБ на 10 сек 1080p
        cmd = [
            "ffmpeg", "-y",
            "-i", tmp_path,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "28",
            "-maxrate", "2500k",
            "-bufsize", "5000k",
            "-vf", "scale=-2:1080",   # макс 1080p по высоте, сохраняем aspect
            "-c:a", "aac",
            "-b:a", "96k",
            "-movflags", "+faststart",
            compressed,
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            os.unlink(tmp_path)
            log.error("ffmpeg failed: %s", stderr[-500:])
            return web.json_response({"error": "compression failed", "detail": stderr[-300:].decode("utf-8", errors="ignore")}, status=500)

        new_size = os.path.getsize(compressed)
        log.info("compressed: %.1f MB → %.1f MB", total / 1_048_576, new_size / 1_048_576)

        if new_size > SUPABASE_LIMIT:
            os.unlink(tmp_path)
            os.unlink(compressed)
            return web.json_response(
                {"error": f"file too large even after compression ({new_size / 1_048_576:.1f} MB)"},
                status=413,
            )

        os.unlink(tmp_path)
        upload_path = compressed
        final_size = new_size

    # Загружаем в Supabase
    with open(upload_path, "rb") as f:
        upload_body = f.read()

    target = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}"
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(
            target,
            content=upload_body,
            headers={
                "Authorization": f"Bearer {SR_KEY}",
                "apikey": SR_KEY,
                "Content-Type": "video/mp4",
                "x-upsert": "true",
            },
        )
    os.unlink(upload_path)

    if r.status_code >= 400:
        log.warning("supabase upload failed %s: %s", r.status_code, r.text[:300])
        return web.json_response({"error": f"supabase: {r.status_code}", "detail": r.text[:300]}, status=502)

    source_video_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}"
    log.info("done %s (received %d → uploaded %d)", storage_path, total, final_size)
    return web.json_response({
        "source_video_url": source_video_url,
        "received_size": total,
        "uploaded_size": final_size,
        "compressed": final_size < total,
    })


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "supabase_configured": bool(SR_KEY), "secret_configured": bool(WORKER_SECRET)})


@web.middleware
async def cors_mw(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, x-upload-token, x-storage-path",
                "Access-Control-Max-Age": "3600",
            },
        )
    resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def make_app() -> web.Application:
    app = web.Application(client_max_size=MAX_BYTES + 4_194_304, middlewares=[cors_mw])
    app.router.add_post("/upload", upload)
    app.router.add_get("/health", health)
    app.router.add_options("/upload", lambda r: web.Response(status=204))
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="127.0.0.1", port=8080, access_log=None)
