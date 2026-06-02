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


def build_user_ass(
    *,
    transcript_words: list[dict],
    key_indices: set[int],
    animation: str,
    out_path: str,
    preset: StyleConfig,
) -> str:
    """Рендер ASS на основе пользовательской выборки.

    Обычные слова: белые, маленький размер (5%), fade-in, по 2-3 в кадре
    Ключевые слова: жёлтые, БОЛЬШОЙ размер (9%), выбранная анимация, по одному

    animation: 'slide_up' | 'pop' | 'fade'
    """
    events: list[str] = []

    regular_size = int(round(CANVAS_H * 0.050))   # ~96px
    key_size = int(round(CANVAS_H * 0.090))       # ~173px
    # Фразы — всегда строго белые
    primary_ass = hex_to_ass("#FFFFFF")
    # Палитра только для КЛЮЧЕВЫХ: жёлтый + белый, без синего и красного
    ACCENT_HEXES = ["#FFD54F", "#FFFFFF"]
    accent_asses = [hex_to_ass(c) for c in ACCENT_HEXES]
    # Тёмный серый для произнесённых слов — заметнее затемнение
    spoken_ass = hex_to_ass("#3A3A3A")
    outline_ass = hex_to_ass("#000000")
    # Полупрозрачная плашка-подложка под фразу (BorderStyle=3 в стиле PhraseBox)
    box_ass = hex_to_ass("#000000", alpha=110)  # alpha 110 ≈ 57% непрозрачность

    CENTER_X = CANVAS_W // 2
    CENTER_Y = 1100  # ближе к низу, чтобы голова в кадре не перекрывалась
    LINE_H = int(regular_size * 1.25)  # вертикальный шаг между словами в фразе
    GAP_MS = 60  # минимальный зазор между событиями, чтобы не было визуального наложения

    def key_anim_tags(x: int, y: int, fs: int, color_ass: str) -> str:
        # shake: микро-дрожание поворотом ±3° в первые 240мс — добавляет "удар"
        shake = "\\t(0,80,\\frz3)\\t(80,160,\\frz-3)\\t(160,240,\\frz0)"
        base = (
            f"\\fs{fs}"
            f"\\1c{color_ass}\\3c{outline_ass}\\bord5"
            f"\\fn{preset.font_name}\\b1"
            f"\\an5"
        )
        if animation == "slide_up":
            return (
                f"\\move({x},{y + 120},{x},{y},0,220)\\fad(180,80){base}"
                f"\\fscx80\\fscy80\\t(0,200,\\fscx110\\fscy110)\\t(200,330,\\fscx100\\fscy100)"
                f"{shake}"
            )
        if animation == "pop":
            return (
                f"\\pos({x},{y})\\fad(80,80){base}"
                f"\\fscx0\\fscy0\\t(0,180,\\fscx118\\fscy118)\\t(180,280,\\fscx100\\fscy100)"
                f"{shake}"
            )
        return (
            f"\\pos({x},{y})\\fad(200,100){base}{shake}"
        )

    def regular_tag(x: int, y: int, fs: int) -> str:
        return (
            f"\\pos({x},{y})"
            f"\\fad(100,80)"
            f"\\fs{fs}"
            f"\\1c{primary_ass}\\3c{outline_ass}\\bord3"
            f"\\fn{preset.font_name}\\b1"
            f"\\an5"
        )

    # Темп речи: разбиваем на фразы по ПАУЗАМ (>400мс между словами) или по ключевому слову.
    # Внутри фразы — слова появляются ПО ОДНОМУ в свой timestamp через \alpha-fade
    # (typewriter-эффект: фраза строится в темп речи)
    PAUSE_MS = 400          # пауза >0.4с разрывает фразу
    MAX_PHRASE_WORDS = 5    # макс слов в одной фразе

    # Сначала собираем сегменты (key или phrase) с их start/end_ms
    # Потом подрезаем end, чтобы соседи не пересекались на экране
    segments: list[dict] = []  # {'type': 'key'|'phrase', 'start': ms, 'end': ms, 'data': ...}

    n = len(transcript_words)
    i = 0
    while i < n:
        word = transcript_words[i]
        idx = word.get("idx", i)
        if idx in key_indices:
            w_start_ms = word["start_ms"]
            # length-aware hold: длинные слова держим дольше
            text_len = len(word["w"])
            hold_min = 600 + text_len * 35
            w_end_ms = max(word["end_ms"] + 250, w_start_ms + hold_min)
            segments.append({
                "type": "key",
                "start": w_start_ms,
                "end": w_end_ms,
                "text": word["w"].upper(),
            })
            i += 1
            continue

        phrase = []
        while i < n and len(phrase) < MAX_PHRASE_WORDS:
            w = transcript_words[i]
            w_idx = w.get("idx", i)
            if w_idx in key_indices:
                break
            if phrase:
                gap = w["start_ms"] - phrase[-1]["end_ms"]
                if gap > PAUSE_MS:
                    break
            phrase.append(w)
            i += 1

        if not phrase:
            continue

        segments.append({
            "type": "phrase",
            "start": phrase[0]["start_ms"],
            "end": phrase[-1]["end_ms"] + 550,
            "phrase": phrase,
        })

    # Подрезаем конец каждого сегмента, чтобы не пересекался со следующим
    for k in range(len(segments) - 1):
        nxt_start = segments[k + 1]["start"]
        if segments[k]["end"] > nxt_start - GAP_MS:
            new_end = nxt_start - GAP_MS
            if new_end <= segments[k]["start"] + 200:
                new_end = segments[k]["start"] + 200
            segments[k]["end"] = new_end

    # Рендер
    key_color_counter = 0
    for seg in segments:
        if seg["type"] == "key":
            # циклическая палитра для ключевых
            color_ass = accent_asses[key_color_counter % len(accent_asses)]
            key_color_counter += 1
            # авто-уменьшение шрифта для длинных слов чтобы не вылезали за края
            text_len = len(seg["text"])
            if text_len <= 8:
                fs_key = key_size
            elif text_len <= 11:
                fs_key = int(key_size * 0.78)
            elif text_len <= 14:
                fs_key = int(key_size * 0.62)
            else:
                fs_key = int(key_size * 0.50)
            tags = key_anim_tags(CENTER_X, CENTER_Y, fs_key, color_ass)
            events.append(
                f"Dialogue: 1,{_ass_time(seg['start'] / 1000)},{_ass_time(seg['end'] / 1000)},KeyWord,,0,0,0,,{{{tags}}}{_escape_ass(seg['text'])}"
            )
            continue

        phrase = seg["phrase"]
        ph_start_ms = seg["start"]
        ph_end_ms = seg["end"]

        # Авто-перенос: если фраза длинная — разбиваем на 2 строки по середине
        words_upper = [w["w"].upper() for w in phrase]
        total_chars = sum(len(w) for w in words_upper) + len(words_upper) - 1
        line_break_idx = -1
        if total_chars > 18 and len(words_upper) >= 2:
            # ищем сплит ближе к середине
            half = total_chars / 2
            cum = 0
            for k, w in enumerate(words_upper):
                cum += len(w) + (1 if k > 0 else 0)
                if cum >= half:
                    line_break_idx = k  # перенос ПЕРЕД этим индексом (k+1-е слово на новой строке)
                    break

        # Karaoke-подсветка: каждое слово окрашено в primary, в свой момент → accent, потом → spoken
        parts = []
        for j, w in enumerate(phrase):
            sep = ""
            if j > 0:
                sep = "\\N" if j == line_break_idx else " "
            parts.append(f"{sep}{_escape_ass(words_upper[j])}")

        inner = "".join(parts)
        # Базовые теги фразы: позиция, pop-in (мягкий), шрифт
        base = (
            f"\\pos({CENTER_X},{CENTER_Y})"
            f"\\fad(120,100)"
            f"\\fs{regular_size}"
            f"\\3c{outline_ass}\\bord5"
            f"\\fn{preset.font_name}\\b1"
            f"\\an5"
            f"\\fscx92\\fscy92\\t(0,180,\\fscx100\\fscy100)"
        )
        events.append(
            f"Dialogue: 0,{_ass_time(ph_start_ms / 1000)},{_ass_time(ph_end_ms / 1000)},PhraseBox,,0,0,0,,{{{base}}}{inner}"
        )

    # Header для произвольного preset — простой Active
    # Делаем headerless (Active style используется как fallback, но мы override через inline теги)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {CANVAS_W}
PlayResY: {CANVAS_H}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Active,{preset.font_name},{regular_size},{primary_ass},&H000000FF,{outline_ass},&H64000000,1,0,0,0,100,100,0,0,1,3,0,5,40,40,40,1
Style: PhraseBox,{preset.font_name},{regular_size},{primary_ass},&H000000FF,{outline_ass},&H64000000,1,0,0,0,100,100,0,0,1,5,0,5,40,40,40,1
Style: KeyWord,{preset.font_name},{key_size},{primary_ass},&H000000FF,{outline_ass},&H00000000,1,0,0,0,100,100,0,0,1,5,0,5,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    body = header + "\n".join(events) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(body)
    log.info("user-ass: %d events, %d key words", len(events), len(key_indices))
    return out_path
