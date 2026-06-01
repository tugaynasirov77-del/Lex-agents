"""Конвертация SRT → ASS с TikTok/Reels-style анимацией.

Каждая фраза:
- Большой жирный белый текст с чёрной обводкой
- Bottom-center
- Bouncy fade-in (scale 100% → 108% за 150ms + fade)
- Подсветка каждого слова отдельно через karaoke \\k теги
"""
from __future__ import annotations

import re
import logging

log = logging.getLogger(__name__)

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Active,DejaVu Sans,84,&H0000FFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,0,2,40,40,420,1
Style: Default,DejaVu Sans,84,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,0,2,40,40,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# regex: блоки SRT — индекс \n start --> end \n text(могут быть многострочные)
SRT_BLOCK = re.compile(
    r"(\d+)\s*\n(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n([\s\S]*?)(?=\n\s*\n|\Z)",
    re.M,
)


def _ass_time(seconds: float) -> str:
    """ASS time format: H:MM:SS.cs (centiseconds)"""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def srt_to_ass_karaoke(srt_text: str, out_path: str, *, max_chars: int = 24) -> str:
    """SRT → ASS karaoke.

    Каждый SRT-блок разбиваем на фразы ≤ max_chars символов,
    время распределяем пропорционально длине слов.
    """
    events: list[str] = []

    for m in SRT_BLOCK.finditer(srt_text):
        h1, m1, s1, ms1 = int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        h2, m2, s2, ms2 = int(m.group(6)), int(m.group(7)), int(m.group(8)), int(m.group(9))
        start_s = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        end_s = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        text = m.group(10).strip().replace("\n", " ")
        if not text:
            continue

        # очистка whisper-артефактов
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        words = text.split(" ")
        total_chars = sum(len(w) for w in words) or 1
        duration = max(0.3, end_s - start_s)

        # Группируем слова в "фразы" — короткие куски ≤ max_chars
        phrases: list[list[str]] = []
        cur: list[str] = []
        cur_len = 0
        for w in words:
            add = len(w) + (1 if cur else 0)
            if cur_len + add > max_chars and cur:
                phrases.append(cur)
                cur = [w]
                cur_len = len(w)
            else:
                cur.append(w)
                cur_len += add
        if cur:
            phrases.append(cur)

        # Распределяем время между фразами пропорционально длине
        phrase_lens = [sum(len(w) for w in p) for p in phrases]
        ph_total = sum(phrase_lens) or 1
        cursor = start_s
        for p, plen in zip(phrases, phrase_lens):
            ph_dur = duration * plen / ph_total
            ph_start = cursor
            ph_end = ph_start + ph_dur
            cursor = ph_end

            # Внутри фразы — \k теги для каждого слова (центисекунды)
            word_lens = [len(w) for w in p]
            wt_total = sum(word_lens) or 1
            parts = []
            remaining_cs = max(1, int(round(ph_dur * 100)))
            for i, (w, wl) in enumerate(zip(p, word_lens)):
                if i == len(p) - 1:
                    k_cs = remaining_cs
                else:
                    k_cs = max(1, int(round(remaining_cs * wl / sum(word_lens[i:]))))
                    remaining_cs -= k_cs
                parts.append(f"{{\\kf{k_cs}}}{_escape_ass(w)}")
            inner = " ".join(parts)

            # bouncy fade-in: \fad(120,80) + scale 96→102 за 180ms
            text_block = "{\\fad(120,80)\\fscx96\\fscy96\\t(0,180,\\fscx102\\fscy102)}" + inner

            events.append(
                f"Dialogue: 0,{_ass_time(ph_start)},{_ass_time(ph_end)},Active,,0,0,0,,{text_block}"
            )

    body = ASS_HEADER + "\n".join(events) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)
    log.info("ASS: %d events → %s", len(events), out_path)
    return out_path
