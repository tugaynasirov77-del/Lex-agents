"""SRT → ASS karaoke с применением пресета стиля.

3 цветовых состояния слова: not-yet (primary), active (highlight), spoken (faded).
Реализация через \\1c override + \\t timer для смены цвета по ходу проигрывания.
"""
from __future__ import annotations

import re
import logging

from .reel_presets import StyleConfig, hex_to_ass

log = logging.getLogger(__name__)

# 1920 — высота рабочего канваса
CANVAS_W = 1080
CANVAS_H = 1920


SRT_BLOCK = re.compile(
    r"(\d+)\s*\n(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n([\s\S]*?)(?=\n\s*\n|\Z)",
    re.M,
)


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


_PUNCT_RE = re.compile(r"[.,!?;:\"'«»()\[\]…]+")


def _strip_punct(word: str) -> str:
    """Убирает пунктуацию из слова целиком (в Reels знаки не нужны)."""
    return _PUNCT_RE.sub("", word).strip()


def _apply_case(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "title":
        return text.title()
    return text


def _build_header(p: StyleConfig) -> str:
    font_size = int(round(CANVAS_H * p.font_size_pct / 100))
    margin_v = int(round(CANVAS_H * p.margin_v_pct / 100))
    primary = hex_to_ass(p.primary_hex)
    outline = hex_to_ass(p.outline_hex)
    bold = 1 if p.bold else 0

    if p.text_bg:
        back = hex_to_ass(p.outline_hex, alpha=255 - p.text_bg_alpha)
        border_style = 4
    else:
        back = "&H64000000"
        border_style = 1

    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {CANVAS_W}
PlayResY: {CANVAS_H}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Active,{p.font_name},{font_size},{primary},&H000000FF,{outline},{back},{bold},0,0,0,100,100,0,0,{border_style},{p.outline_px},0,{p.alignment},40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _cinematic_ass(srt_text: str, out_path: str, p: StyleConfig) -> str:
    """Cinematic 2-row layout: верхнее слово BOLD CAPS, под ним italic-accent появляется отдельным событием."""
    events: list[str] = []
    color_idx = 0
    accent_colors = p.accent_colors or ["#FFD54F"]

    # Координаты двух строк — компактно, впритык с лёгким зазором
    TOP_X, TOP_Y = CANVAS_W // 2, 900
    BOT_X, BOT_Y = CANVAS_W // 2, 1020
    SOLO_X, SOLO_Y = CANVAS_W // 2, 960

    base_font_size = int(round(CANVAS_H * p.font_size_pct / 100))
    accent_font_size = int(round(CANVAS_H * p.font_size_pct * p.accent_size_ratio / 100))

    # Используем естественные тайминги Whisper БЕЗ растягивания (иначе слова отстают от голоса)
    flat: list[tuple[str, float, float]] = []  # (word, start, end)

    for m in SRT_BLOCK.finditer(srt_text):
        h1, m1, s1, ms1 = int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        h2, m2, s2, ms2 = int(m.group(6)), int(m.group(7)), int(m.group(8)), int(m.group(9))
        start_s = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        end_s = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        text = re.sub(r"\s+", " ", m.group(10).strip().replace("\n", " ")).strip()
        if not text:
            continue
        words = text.split(" ")
        duration = max(0.2, end_s - start_s)
        wt_total = sum(len(w) for w in words) or 1

        block_cursor = start_s
        for w in words:
            cleaned = _strip_punct(w)
            wd = duration * len(w) / wt_total
            w_start = block_cursor
            w_end = w_start + wd
            block_cursor = w_end
            if cleaned:
                flat.append((cleaned, w_start, w_end))

    # Идём парами: 70% времени — пара (main + accent), 30% — соло
    i = 0
    while i < len(flat):
        # Пара? — если есть следующее слово и оба >2 символов
        if i + 1 < len(flat) and len(flat[i][0]) >= 2 and len(flat[i + 1][0]) >= 3:
            main_w, m_start, m_end = flat[i]
            acc_w, a_start, a_end = flat[i + 1]

            # TOP — main word: показываем от его начала до конца accent (висит пока появляется второе)
            top_tags = (
                f"\\pos({TOP_X},{TOP_Y})"
                f"\\fad(100,80)"
                f"\\fn{p.font_name}\\b1"
                f"\\fs{base_font_size}"
                f"\\1c{hex_to_ass(p.primary_hex)}"
                f"\\3c{hex_to_ass(p.outline_hex)}\\bord{p.outline_px}"
                f"\\fscx92\\fscy92\\t(0,140,\\fscx102\\fscy102)"
                f"\\an5"
            )
            top_text = "{" + top_tags + "}" + _escape_ass(main_w.upper())
            events.append(f"Dialogue: 0,{_ass_time(m_start)},{_ass_time(a_end)},Active,,0,0,0,,{top_text}")

            # BOTTOM — italic accent
            color = accent_colors[color_idx % len(accent_colors)]
            color_idx += 1
            color_ass = hex_to_ass(color)
            underline = 1 if p.accent_underline else 0
            bot_tags = (
                f"\\pos({BOT_X},{BOT_Y})"
                f"\\fad(120,80)"
                f"\\fn{p.accent_font}\\i1\\u{underline}"
                f"\\fs{accent_font_size}"
                f"\\1c{color_ass}"
                f"\\3c{hex_to_ass(p.outline_hex)}\\bord2"
                f"\\fscx88\\fscy88\\t(0,170,\\fscx100\\fscy100)"
                f"\\an5"
            )
            bot_text = "{" + bot_tags + "}" + _escape_ass(acc_w.lower())
            events.append(f"Dialogue: 0,{_ass_time(a_start)},{_ass_time(a_end)},Active,,0,0,0,,{bot_text}")

            i += 2
        else:
            # Одиночное слово по центру
            w, w_start, w_end = flat[i]
            solo_tags = (
                f"\\pos({SOLO_X},{SOLO_Y})"
                f"\\fad(100,80)"
                f"\\fn{p.font_name}\\b1"
                f"\\fs{base_font_size}"
                f"\\1c{hex_to_ass(p.primary_hex)}"
                f"\\3c{hex_to_ass(p.outline_hex)}\\bord{p.outline_px}"
                f"\\fscx92\\fscy92\\t(0,140,\\fscx102\\fscy102)"
                f"\\an5"
            )
            solo_text = "{" + solo_tags + "}" + _escape_ass(w.upper())
            events.append(f"Dialogue: 0,{_ass_time(w_start)},{_ass_time(w_end)},Active,,0,0,0,,{solo_text}")
            i += 1

    body = _build_header(p) + "\n".join(events) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)
    log.info("ASS cinematic [%s]: %d events / %d words → %s", p.name, len(events), len(flat), out_path)
    return out_path


def _animation_prefix(p: StyleConfig) -> str:
    """ASS-теги анимации появления."""
    d = p.anim_duration_ms
    if p.animation == "pop":
        return f"{{\\fad(120,80)\\fscx94\\fscy94\\t(0,{d},\\fscx104\\fscy104)}}"
    if p.animation == "slide":
        return f"{{\\fad(80,80)\\move({CANVAS_W // 2},{CANVAS_H},{CANVAS_W // 2},{CANVAS_H - 250},0,{d})}}"
    if p.animation == "fade":
        return f"{{\\fad({d},80)}}"
    if p.animation == "typewriter":
        return "{\\fad(40,40)}"
    return "{\\fad(120,80)}"


def srt_to_ass_styled(srt_text: str, out_path: str, preset: StyleConfig) -> str:
    """Конвертирует SRT в ASS. Если preset.cinematic_mode — спец-рендер."""
    if preset.cinematic_mode:
        return _cinematic_ass(srt_text, out_path, preset)

    primary_c = hex_to_ass(preset.primary_hex)
    active_c = hex_to_ass(preset.active_word_hex)
    spoken_c = hex_to_ass(preset.spoken_word_hex)

    events: list[str] = []

    for m in SRT_BLOCK.finditer(srt_text):
        h1, m1, s1, ms1 = int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        h2, m2, s2, ms2 = int(m.group(6)), int(m.group(7)), int(m.group(8)), int(m.group(9))
        start_s = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        end_s = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        text = m.group(10).strip().replace("\n", " ")
        if not text:
            continue
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        words = [_apply_case(w, preset.case) for w in text.split(" ")]
        duration = max(0.3, end_s - start_s)

        # Группируем слова по preset.words_per_segment
        groups: list[list[str]] = []
        for i in range(0, len(words), preset.words_per_segment):
            groups.append(words[i : i + preset.words_per_segment])

        # Распределяем время между группами пропорционально длине
        group_lens = [sum(len(w) for w in g) for g in groups]
        total_len = sum(group_lens) or 1
        cursor = start_s

        for g, glen in zip(groups, group_lens):
            gdur = duration * glen / total_len
            g_start = cursor
            g_end = g_start + gdur
            cursor = g_end

            # внутри группы — окрашиваем каждое слово по таймингу
            word_lens = [len(w) for w in g]
            wt_total = sum(word_lens) or 1
            word_durations = [gdur * wl / wt_total for wl in word_lens]

            # для каждого слова: цвет меняется от primary → active в момент его старта,
            # и от active → spoken когда оно "прозвучало"
            parts = []
            cum = 0.0
            for i, w in enumerate(g):
                wd = word_durations[i]
                t_active_start_ms = int(cum * 1000)
                t_active_end_ms = int((cum + wd) * 1000)
                cum += wd

                # стартовый цвет: primary
                # в момент t_active_start меняется на active
                # в момент t_active_end меняется на spoken
                # \t(start,end,style) — плавный переход; если хотим мгновенно — start=end
                word_block = (
                    f"{{\\1c{primary_c}"
                    f"\\t({t_active_start_ms},{t_active_start_ms + 30},\\1c{active_c})"
                    f"\\t({t_active_end_ms},{t_active_end_ms + 30},\\1c{spoken_c})}}"
                    f"{_escape_ass(w)}"
                )
                parts.append(word_block)

            inner = " ".join(parts)
            anim = _animation_prefix(preset)
            text_block = anim + inner

            events.append(
                f"Dialogue: 0,{_ass_time(g_start)},{_ass_time(g_end)},Active,,0,0,0,,{text_block}"
            )

    body = _build_header(preset) + "\n".join(events) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)
    log.info("ASS [%s]: %d events → %s", preset.name, len(events), out_path)
    return out_path


# legacy alias
def srt_to_ass_karaoke(srt_text: str, out_path: str, max_chars: int = 24) -> str:
    from .reel_presets import EXPERT_CLEAN
    return srt_to_ass_styled(srt_text, out_path, EXPERT_CLEAN)
