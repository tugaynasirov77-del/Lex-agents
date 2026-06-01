"""FFmpeg-пайплайн: subtitle burn-in + text overlays + background music + 1080x1920.

Требуется ffmpeg в PATH. На Railway добавлен через nixpacks.toml.
"""
from __future__ import annotations

import os
import subprocess
import logging
from typing import Iterable

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


def _escape_drawtext(text: str) -> str:
    """Escape для filter_complex drawtext."""
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def write_srt(srt_text: str, path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(srt_text)
    return path


def render_reel(
    *,
    input_video: str,
    srt_path: str,
    overlays: Iterable[dict],
    music_path: str | None,
    output_path: str,
    music_volume: float = 0.08,
    font_path: str | None = None,
) -> str:
    """Финальный рендер.

    overlays: iterable of {time:int sec, text:str, duration:int sec}
    """
    if not os.path.exists(input_video):
        raise FFmpegError(f"input not found: {input_video}")
    if not os.path.exists(srt_path):
        raise FFmpegError(f"srt not found: {srt_path}")

    # Сценический ratio 9:16 1080x1920. Если HeyGen уже отдал 9:16 — просто scale.
    # subtitles фильтр выжигает SRT в видео. force_style — белый текст, чёрная обводка.
    style = (
        "FontName=DejaVu Sans Bold,FontSize=18,PrimaryColour=&HFFFFFF&,"
        "OutlineColour=&H000000&,BorderStyle=1,Outline=2,Shadow=0,"
        "Alignment=2,MarginV=120"
    )
    # ВАЖНО: путь к srt в фильтре нужно экранировать (: → \:)
    srt_for_filter = srt_path.replace(":", "\\:").replace("'", "\\'")

    vf_parts = [
        "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        f"subtitles='{srt_for_filter}':force_style='{style}'",
    ]

    # text overlays (drawtext) — accent-надписи поверх по timestamp
    font_arg = f":fontfile={font_path}" if font_path else ""
    for ov in overlays:
        try:
            t = int(ov.get("time", 0))
            d = int(ov.get("duration", 3))
            txt = _escape_drawtext(str(ov.get("text", "")))
        except Exception:
            continue
        if not txt:
            continue
        start, end = t, t + d
        vf_parts.append(
            f"drawtext=text='{txt}'{font_arg}:fontcolor=white:fontsize=64:"
            f"borderw=4:bordercolor=black@0.8:"
            f"x=(w-text_w)/2:y=h*0.18:"
            f"enable='between(t,{start},{end})'"
        )

    vf = ",".join(vf_parts)

    cmd: list[str] = [
        "ffmpeg", "-y",
        "-i", input_video,
    ]
    if music_path and os.path.exists(music_path):
        cmd += ["-stream_loop", "-1", "-i", music_path]

    cmd += [
        "-vf", vf,
    ]

    if music_path and os.path.exists(music_path):
        # микс аудио: оригинал 1.0 + фон music_volume
        cmd += [
            "-filter_complex",
            f"[0:a]volume=1.0[a0];[1:a]volume={music_volume}[a1];[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]",
            "-map", "0:v",
            "-map", "[aout]",
        ]
    else:
        cmd += ["-map", "0:v", "-map", "0:a?"]

    cmd += [
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]

    log.info("ffmpeg: %s", " ".join(cmd[:8]) + " …")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed: {proc.stderr[-1200:]}")
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
        raise FFmpegError(f"ffmpeg output too small: {output_path}")
    log.info("FFmpeg done: %s (%d bytes)", output_path, os.path.getsize(output_path))
    return output_path


def extract_cover(input_video: str, output_image: str, *, at_second: float = 1.5) -> str:
    """Один кадр для обложки Reel."""
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(at_second),
        "-i", input_video,
        "-frames:v", "1",
        "-q:v", "2",
        output_image,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"cover extract failed: {proc.stderr[-400:]}")
    return output_image
