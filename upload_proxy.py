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

import httpx
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("upload_proxy")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SR_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
BUCKET = "raw-uploads"
MAX_BYTES = 52 * 1024 * 1024  # 50 MB


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
    chunks: list[bytes] = []
    async for chunk in request.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_BYTES:
            return web.json_response({"error": "file > 50 MB"}, status=413)
        chunks.append(chunk)
    body = b"".join(chunks)
    if total == 0:
        return web.json_response({"error": "empty body"}, status=400)

    log.info("uploading %d bytes → %s", total, storage_path)

    target = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}"
    async with httpx.AsyncClient(timeout=180.0) as client:
        r = await client.post(
            target,
            content=body,
            headers={
                "Authorization": f"Bearer {SR_KEY}",
                "apikey": SR_KEY,
                "Content-Type": content_type if content_type else "video/mp4",
                "x-upsert": "true",
            },
        )
    if r.status_code >= 400:
        log.warning("supabase upload failed %s: %s", r.status_code, r.text[:300])
        return web.json_response({"error": f"supabase: {r.status_code}", "detail": r.text[:300]}, status=502)

    source_video_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{storage_path}"
    log.info("done %s (%d bytes)", storage_path, total)
    return web.json_response({"source_video_url": source_video_url, "size": total})


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
    app = web.Application(client_max_size=MAX_BYTES + 1_048_576, middlewares=[cors_mw])
    app.router.add_post("/upload", upload)
    app.router.add_get("/health", health)
    app.router.add_options("/upload", lambda r: web.Response(status=204))
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="127.0.0.1", port=8080, access_log=None)
