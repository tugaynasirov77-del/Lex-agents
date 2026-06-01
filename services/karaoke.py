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

    base_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {CANVAS_W}
PlayResY: {CANVAS_H}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Active,{p.font_name},{font_size},{primary},&H000000FF,{outline},{back},{bold},0,0,0,100,100,0,0,{border_style},{p.outline_px},0,{p.alignment},40,40,{margin_v},1
"""

    # Дополнительные стили для cinematic_mode (4 цветные плашки)
    if p.cinematic_mode:
        pill_size = int(round(CANVAS_H * p.font_size_pct / 100))
        text_dark = "&H001A0F0B"   # #0B0F1A → BGR
        text_light = "&H00FFFFFF"  # белый

        # Цветные фоны плашек (BGR в ASS)
        pill_cyan = "&H00FFC800"      # #00C8FF
        pill_yellow = "&H004FD5FF"    # #FFD54F
        pill_orange = "&H002D7AFF"    # #FF7A2D
        pill_white = "&H00F0F0F0"     # #F0F0F0

        # BorderStyle=4 = opaque box. Outline=N = padding в N px вокруг текста.
        pad = 28
        pills = [
            ("PillCyan", text_dark, pill_cyan),
            ("PillYellow", text_dark, pill_yellow),
            ("PillOrange", text_light, pill_orange),
            ("PillWhite", text_dark, pill_white),
        ]
        for name, txt_col, bg_col in pills:
            base_header += (
                f"Style: {name},{p.font_name},{pill_size},{txt_col},&H000000FF,"
                f"{txt_col},{bg_col},1,0,0,0,100,100,0,0,4,{pad},0,2,40,40,{margin_v},1\n"
            )

    base_header += "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    return base_header


def _cinematic_ass(srt_text: str, out_path: str, p: StyleConfig) -> str:
    """Klap "construction worker" стиль: ALL CAPS короткие фразы в цветных плашках,
    цвет циклится cyan → yellow → orange → white. По центру внизу, pop-in.
    """
    events: list[str] = []

    pill_styles = ["PillCyan", "PillYellow", "PillOrange", "PillWhite"]

    font_size = int(round(CANVAS_H * p.font_size_pct / 100))
    PHRASE_MAX_CHARS = 18              # макс длина фразы в плашке (вкл. пробелы)
    MIN_PHRASE_DUR = 0.6               # плашка не короче 0.6 сек на экране
    INTER_GAP = 0.04                   # 40мс между плашками — глаз не путается

    # Сбор плоского списка слов с whisper-таймингами (естественными, без растяжки)
    flat: list[tuple[str, float, float]] = []
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
        cur = start_s
        for w in words:
            cleaned = _strip_punct(w)
            wd = duration * len(w) / wt_total
            w_start, w_end = cur, cur + wd
            cur = w_end
            # Игнорируем слова короче 2 символов (артефакты whisper) и пустые
            if cleaned and len(cleaned) >= 2:
                flat.append((cleaned, w_start, w_end))

    # Группируем слова в "фразы" — максимум 18 символов или 3 слова, что наступит раньше
    phrases: list[tuple[str, float, float]] = []
    cur_words: list[tuple[str, float, float]] = []
    cur_chars = 0

    def flush():
        if not cur_words:
            return
        text = " ".join(w for w, _, _ in cur_words).upper()
        ph_start = cur_words[0][1]
        ph_end = cur_words[-1][2]
        phrases.append((text, ph_start, ph_end))

    for w, w_start, w_end in flat:
        upper_w = w.upper()
        added_chars = len(upper_w) + (1 if cur_words else 0)
        if (cur_chars + added_chars > PHRASE_MAX_CHARS or len(cur_words) >= 3) and cur_words:
            flush()
            cur_words = []
            cur_chars = 0
        cur_words.append((w, w_start, w_end))
        cur_chars += added_chars
    flush()

    # Минимальная длительность + gap между плашками
    fixed: list[tuple[str, float, float]] = []
    for idx, (text, ph_start, ph_end) in enumerate(phrases):
        if ph_end - ph_start < MIN_PHRASE_DUR:
            ph_end = ph_start + MIN_PHRASE_DUR
        # подрезаем конец, если следующая фраза близко — оставляем gap
        if idx + 1 < len(phrases):
            next_start = phrases[idx + 1][1]
            if ph_end > next_start - INTER_GAP:
                ph_end = next_start - INTER_GAP
                if ph_end - ph_start < 0.3:
                    ph_end = ph_start + 0.3
        fixed.append((text, ph_start, ph_end))

    # Рендер: плашка циклически меняет цвет
    for idx, (text, ph_start, ph_end) in enumerate(fixed):
        style = pill_styles[idx % len(pill_styles)]
        # Pop-in анимация: scale 80→100 за 150ms + fade
        tags = "{\\fad(120,80)\\fscx82\\fscy82\\t(0,160,\\fscx100\\fscy100)}"
        events.append(
            f"Dialogue: 0,{_ass_time(ph_start)},{_ass_time(ph_end)},{style},,0,0,0,,{tags}{_escape_ass(text)}"
        )

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
