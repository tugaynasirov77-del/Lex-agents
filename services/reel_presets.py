"""3 пресета визуального стиля Reels — на базе анализа топовых креаторов 2025-2026.

Каждый пресет описывает ВСЕ параметры рендера:
- ASS-субтитры (шрифт, размер, цвета, анимация, позиция, регистр)
- Hook (zoom, длительность)
- Цветокор (контраст, насыщенность, температура)
- Звук (громкость музыки)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


def hex_to_ass(hex_color: str, alpha: int = 0) -> str:
    """#RRGGBB → &HAABBGGRR для ASS."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


@dataclass
class StyleConfig:
    # Идентификатор
    name: str
    label: str
    description: str

    # Шрифт
    font_name: str = "DejaVu Sans"
    font_size_pct: float = 6.5         # % от высоты кадра (1920)
    bold: bool = True

    # Цвета
    primary_hex: str = "#FFFFFF"
    outline_hex: str = "#0B0F1A"
    outline_px: int = 5
    active_word_hex: str = "#F7D046"   # цвет подсветки текущего слова
    spoken_word_hex: str = "#FFFFFF"   # цвет уже произнесённых

    # Фон под текстом (pill / box)
    text_bg: bool = False
    text_bg_alpha: int = 128           # 0..255

    # Позиция
    margin_v_pct: float = 22           # % снизу
    alignment: int = 2                 # 2 = bottom-center в ASS

    # Слова на экране
    words_per_segment: int = 3         # max слов в одном dialogue
    case: Literal["upper", "title", "sentence"] = "upper"

    # Анимация появления
    animation: Literal["pop", "slide", "fade", "typewriter"] = "pop"
    anim_duration_ms: int = 140

    # Hook (первые секунды)
    hook_zoom_factor: float = 1.08
    hook_zoom_duration_s: float = 1.6

    # Цветокор (FFmpeg eq + curves)
    contrast: float = 1.05
    saturation: float = 1.0
    gamma: float = 1.0
    temperature: Literal["cold", "neutral", "warm"] = "neutral"

    # Звук
    music_volume: float = 0.12         # 0..1, относительно голоса 1.0

    # ──── Cinematic mode (для стиля "наставник") ────
    cinematic_mode: bool = False       # включает специальный one-word-at-time рендер
    accent_font: str = "EB Garamond"   # serif italic для акцентов
    accent_colors: list = field(default_factory=lambda: ["#FFD54F", "#E53935", "#F0E6D2"])
    accent_every_n_word: int = 3       # каждое N-ое слово — акцент (italic serif + цвет)
    accent_size_ratio: float = 0.78    # размер italic относительно основного
    accent_underline: bool = True


# ─────────────────── ПРЕСЕТ 1: МИНИМАЛИЗМ-ЭКСПЕРТ ───────────────────
EXPERT_CLEAN = StyleConfig(
    name="expert_clean",
    label="Минимализм-эксперт",
    description="Чистый кадр, крупные тезисы. Маркетинг, обучение, экспертный контент.",

    font_name="DejaVu Sans",
    font_size_pct=6.5,
    bold=True,

    primary_hex="#FFFFFF",
    outline_hex="#0B0F1A",
    outline_px=5,
    active_word_hex="#F7D046",
    spoken_word_hex="#FFFFFF",

    text_bg=False,

    margin_v_pct=22,
    words_per_segment=3,
    case="upper",

    animation="pop",
    anim_duration_ms=150,

    hook_zoom_factor=1.08,
    hook_zoom_duration_s=1.8,

    contrast=1.06,
    saturation=1.0,
    gamma=1.0,
    temperature="neutral",

    music_volume=0.10,
)


# ─────────────────── ПРЕСЕТ 2: ДИНАМИЧНЫЙ ЛИЧНЫЙ БРЕНД ───────────────────
PERSONAL_BRAND_ENERGY = StyleConfig(
    name="personal_brand_energy",
    label="Динамичный личный бренд",
    description="Много эмоций, плотный темп. Эксперты, коучи, личные бренды.",

    font_name="DejaVu Sans",
    font_size_pct=7.0,
    bold=True,

    primary_hex="#FFFFFF",
    outline_hex="#111111",
    outline_px=4,
    active_word_hex="#FFCC00",
    spoken_word_hex="#F5F5F5",

    text_bg=False,

    margin_v_pct=20,
    words_per_segment=2,
    case="upper",

    animation="pop",
    anim_duration_ms=120,

    hook_zoom_factor=1.13,
    hook_zoom_duration_s=1.5,

    contrast=1.10,
    saturation=1.12,
    gamma=0.98,
    temperature="warm",

    music_volume=0.15,
)


# ─────────────────── ПРЕСЕТ 3: AI-TECH ЭНЕРГИЧНЫЙ ───────────────────
AI_TECH_FAST = StyleConfig(
    name="ai_tech_fast",
    label="AI-tech энергичный",
    description="Холодная палитра, плотный текст. AI, стартапы, технологии.",

    font_name="DejaVu Sans",
    font_size_pct=7.5,
    bold=True,

    primary_hex="#FFFFFF",
    outline_hex="#000000",
    outline_px=5,
    active_word_hex="#00D1FF",
    spoken_word_hex="#FFFFFF",

    text_bg=True,
    text_bg_alpha=140,

    margin_v_pct=18,
    words_per_segment=2,
    case="upper",

    animation="pop",
    anim_duration_ms=100,

    hook_zoom_factor=1.10,
    hook_zoom_duration_s=1.4,

    contrast=1.15,
    saturation=0.92,
    gamma=0.97,
    temperature="cold",

    music_volume=0.18,
)


# ─────────────────── ПРЕСЕТ 4: CINEMATIC MENTOR ───────────────────
# Стиль "наставника": одно слово в кадре по центру, ОЧЕНЬ крупно,
# смесь Bold sans-caps + italic serif с подчёркиванием на акцентных словах.
CINEMATIC_MENTOR = StyleConfig(
    name="cinematic_mentor",
    label="Cinematic Mentor",
    description="Крупный текст по центру, italic-акценты, jump-cuts. Премиум-видеомонтаж.",

    font_name="Montserrat Black",
    font_size_pct=5.5,                 # 1920*0.055 ≈ 106px — для плашек оптимально
    bold=True,

    primary_hex="#0B0F1A",             # тёмный текст внутри плашки (для светлых плашек)
    outline_hex="#000000",
    outline_px=28,                     # это padding внутри opaque box (BorderStyle=4)
    active_word_hex="#FFD54F",
    spoken_word_hex="#FFFFFF",

    text_bg=True,

    margin_v_pct=22,                   # 22% снизу — нижняя четверть кадра
    alignment=2,                       # 2 = bottom-center
    words_per_segment=3,
    case="upper",

    animation="pop",
    anim_duration_ms=160,

    hook_zoom_factor=1.06,
    hook_zoom_duration_s=1.4,

    contrast=1.08,
    saturation=1.05,
    gamma=0.99,
    temperature="neutral",

    music_volume=0.14,

    cinematic_mode=True,
    accent_font="DejaVu Serif",       # 100% кириллица; Garamond пропускал буквы
    accent_colors=["#FFD54F", "#E53935", "#F0E6D2"],
    accent_every_n_word=4,             # реже акценты — каждое 4-е слово
    accent_size_ratio=0.85,
    accent_underline=True,
)


PRESETS = {
    "expert_clean": EXPERT_CLEAN,
    "personal_brand_energy": PERSONAL_BRAND_ENERGY,
    "ai_tech_fast": AI_TECH_FAST,
    "cinematic_mentor": CINEMATIC_MENTOR,
}


def get_preset(name: str | None) -> StyleConfig:
    return PRESETS.get(name or "expert_clean", EXPERT_CLEAN)
