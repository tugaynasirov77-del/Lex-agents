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
    subs_path: str,
    overlays: Iterable[dict],
    music_path: str | None,
    output_path: str,
    music_volume: float = 0.08,
    font_path: str | None = None,
    hook_zoom: bool = True,
) -> str:
    """Финальный рендер.

    subs_path: ASS-файл (TikTok karaoke) или SRT — определяется по расширению.
    overlays: iterable of {time:int sec, text:str, duration:int sec}
    hook_zoom: zoom-in эффект первые 2 сек для удержания внимания
    """
    if not os.path.exists(input_video):
        raise FFmpegError(f"input not found: {input_video}")
    if not os.path.exists(subs_path):
        raise FFmpegError(f"subs not found: {subs_path}")

    is_ass = subs_path.lower().endswith(".ass")
    subs_for_filter = subs_path.replace(":", "\\:").replace("'", "\\'")

    # 1080x1920 + лёгкий hook-zoom первые 2 сек
    scale_crop = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if hook_zoom:
        # zoom 1.0 → 1.06 за 2 сек, потом держим 1.0
        # используем expressions в scale+crop: scale up, then crop center с движущимися offsets
        # проще: scale больше, crop центрированно с переменным размером кропа
        zoom_expr = "if(lt(t,2),1+0.03*t,1.06-0.06*(t-2)/0.6)"  # 1→1.06 за 2с, потом плавно обратно за 0.6с
        zoom_expr = "if(lt(t,2),1+0.03*t,if(lt(t,2.6),1.06-(t-2)/0.6*0.06,1))"
        # crop с временной шириной
        scale_crop = (
            "scale=1200:2133:force_original_aspect_ratio=increase,"
            f"crop='1080/({zoom_expr})':'1920/({zoom_expr})':(in_w-out_w)/2:(in_h-out_h)/2,"
            "scale=1080:1920"
        )

    if is_ass:
        subs_filter = f"ass='{subs_for_filter}'"
    else:
        style = "FontName=DejaVu Sans Bold,FontSize=18,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=120"
        subs_filter = f"subtitles='{subs_for_filter}':force_style='{style}'"

    vf_parts = [scale_crop, subs_filter]

    # text overlays (drawtext) с fade-in
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
        # fade-in 0.3 сек, fade-out 0.3 сек через alpha
        alpha_expr = f"if(lt(t,{start}+0.3),(t-{start})/0.3,if(gt(t,{end}-0.3),({end}-t)/0.3,1))"
        vf_parts.append(
            f"drawtext=text='{txt}'{font_arg}:fontcolor=white:fontsize=64:"
            f"borderw=4:bordercolor=black@0.85:"
            f"x=(w-text_w)/2:y=h*0.18:"
            f"alpha='{alpha_expr}':"
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
