"""The ``belkins`` menu theme — Belkins AI Companion, Boss Raid Arena & 12-Item Poké Mart.

Custom flagship theme for Belkins Usage Bar (`bswap` / `belkins`), inspired by
PokeTokenBar. Replaces all ``cswap`` references with ``bswap`` and organizes the
dropdown into 5 clean, in-place tabs (`⚡ Party`, `Claude`, `Codex`, `🐾 Dex`,
`🛒 Mart`):

1. **Belkins Companion Hero Card**: Live 14x14 pixel-art evolving AI companion
   (8 stages: Sparkbit -> Outreachik -> Pipelinex -> Contexton -> Belkinosaur
   -> Chronoflare -> Belkins Prime -> Omega Belkion), dynamic pad/XP themes,
   Shiny palette, Plasma Blade, Mini-Drone sidekick, ATK power, and SparkCoins.
2. **⚡ Party Tab (Default)**: Clean command center showing active Claude and
   Codex champions first, plus a smart grouped Needs-Attention summary card.
3. **Claude & Codex Tabs**: Dedicated fleet cards with spacious Party Stamina
   rows for one-click account switching via ``bswap``.
4. **🐾 Dex Tab**: Interactive **⚔️ Boss Raid Arena** (click to strike Raid
   Bosses for SparkCoins & XP!) + sub-tabs for the **8-Stage Pokedex** (click
   any unlocked stage to morph your Companion!) and **10 Trophy Badges**.
5. **🛒 Mart Tab**: 12-item Belkins Poké Mart with **`⚔️ Gear & Lab`** (Context
   Berry, repeatable Rare Token Candy, Deal Shades, Plasma Blade, Sovereign
   Crown, Shiny Chroma Core) and **`🎨 Skins & Pets`** (Solar/Emerald/Nebula
   Pads, Cyber Halo, Mini-Drone Sidekick, Cyber Arcade UI Skin).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from ..belkins_game import (
    EVOLUTION_LINE,
    SHOP_CATALOG,
    CompanionState,
    EvolutionStage,
    RaidBossSpec,
    claim_daily_supply_drop,
    companion_from_snapshot,
    load_game_save,
    select_companion_stage,
    strike_raid_boss,
    toggle_or_buy_shop_item,
)
from ..card_base import W, Block, Pen, register_role
from ..contracts import format_pct, format_tokens, format_usd
from . import ThemeActions

TABS: tuple[tuple[str, str], ...] = (
    ("party", "Hub"),
    ("claude", "Claude"),
    ("codex", "Codex"),
    ("dex", "Evo"),
    ("shop", "Vault"),
    ("config", "Studio"),
)
_TAB_ICONS: dict[str, str] = {
    "party": "square.grid.2x2.fill",
    "claude": "sparkles",
    "codex": "terminal.fill",
    "dex": "trophy.fill",
    "shop": "shield.lefthalf.filled",
    "config": "slider.horizontal.3",
}
_TAB_ACCENTS: dict[str, str] = {
    "party": "belkins_flame_rule",
    "claude": "belkins_claude_rule",
    "codex": "belkins_codex_rule",
    "dex": "belkins_gold_rule",
    "shop": "belkins_purple_rule",
    "config": "belkins_codex_rule",
}
_ACTIVE_TAB: str = "party"
_DEX_SUBTAB: str = "pokedex"     # "pokedex" | "trophies"
_SHOP_SUBTAB: str = "gear"       # "gear" | "style"
_CFG_SUBTAB: str = "header"      # "header" | "general"
_LAST_SHOP_NOTE: str = ""
_LAST_BOSS_NOTE: str = ""
_LAST_CFG_NOTE: str = ""


def get_active_tab() -> str:
    return _ACTIVE_TAB


def set_active_tab(tab_id: str) -> None:
    global _ACTIVE_TAB
    if any(t[0] == tab_id for t in TABS):
        _ACTIVE_TAB = tab_id


def set_dex_subtab(sub: str) -> None:
    global _DEX_SUBTAB
    if sub in ("pokedex", "trophies"):
        _DEX_SUBTAB = sub


def set_shop_subtab(sub: str) -> None:
    global _SHOP_SUBTAB
    if sub in ("gear", "style"):
        _SHOP_SUBTAB = sub


def set_cfg_subtab(sub: str) -> None:
    global _CFG_SUBTAB
    if sub in ("header", "general"):
        _CFG_SUBTAB = sub


def _fmt_num(n: int) -> str:
    val = int(n)
    if abs(val) >= 1_000_000:
        m = val / 1_000_000.0
        return f"{m:.1f}M" if abs(m) >= 10.0 else f"{m:.2f}M"
    if abs(val) >= 10_000:
        k = val / 1_000.0
        return f"{k:.0f}K" if abs(k) >= 100.0 else f"{k:.1f}K"
    return f"{val:,}"


def _fmt_compact_coins(n: int) -> str:
    val = int(n)
    if val >= 1_000_000:
        m = val / 1_000_000.0
        return f"{m:.1f}M" if m >= 10.0 else f"{m:.2f}M"
    if val >= 10_000:
        k = val / 1_000.0
        return f"{k:.0f}K" if k >= 100.0 else f"{k:.1f}K"
    return f"{val:,}"


# ---------------------------------------------------------------- layout ---
CARD_X = 10.0
PAD = 14.0
CARD_R = 10.0
GAP = 8.0
TOP_GAP = 2.0
TAIL_GAP = 0.0
X0 = CARD_X + PAD                          # 24
X1 = W - CARD_X - PAD                      # 336
W_INNER = X1 - X0                          # 312
HX = X0 + 12
COL_RESET_W = 78.0
COL_PCT_R = X1 - COL_RESET_W - 4           # 254
COL_PCT_W = 40.0
COL_BAR_W = 62.0
COL_BAR_X = COL_PCT_R - COL_PCT_W - 6 - COL_BAR_W
COL_LABEL_R = COL_BAR_X - 6
CAP_STEP = 15.0
HEAD_BASE = 22.0
HEAD_AFTER = 12.0
WIN_LABEL_W = 50.0
WIN_STEP = 22.0
ROW_BASE = 19.0
ROW_CAP = 15.0
ROW_END = 11.0
INK = 4.0
PART_END = 9.0
ACCENT_W = 3.5
ROW_HL = {"hl_inset": CARD_X + 4, "hl_vinset": 1.0, "hl_radius": 6.0}

# ------------------------------------------------------------------ fonts ---
F: dict[str, tuple[float, str, bool, bool, bool]] = {
    "title": (12.5, "bold", False, False, False),
    "hero_name": (13.0, "semibold", False, False, False),
    "hero_big": (20.0, "bold", True, False, True),
    "primary": (12.0, "regular", False, False, False),
    "primary_med": (12.0, "semibold", False, False, False),
    "secondary": (11.0, "regular", False, False, False),
    "secondary_med": (11.0, "medium", False, False, False),
    "caption": (10.5, "regular", False, False, False),
    "caption_med": (10.5, "medium", False, False, False),
    "num": (11.5, "semibold", True, False, True),
    "num_bar": (11.0, "semibold", True, False, True),
    "num_small": (10.5, "regular", True, False, False),
    "slot": (10.5, "semibold", True, False, False),
    "code": (10.0, "medium", False, True, False),
    "tag": (9.5, "semibold", False, False, False),
}

_WHITE = (255, 255, 255)
_ROLES: tuple[tuple[str, tuple, tuple, Any], ...] = (
    ("belkins_cmm_canvas", (244, 242, 252, 0.94), (36, 22, 74, 0.92), None),
    ("belkins_cmm_aqua", (12, 168, 148), (94, 242, 214), _WHITE),
    ("belkins_cmm_icon", (108, 96, 156), (182, 170, 222), _WHITE),
    ("belkins_cmm_sub", (92, 84, 128), (204, 196, 232), _WHITE),
    ("belkins_cmm_coral", (220, 62, 56), (255, 122, 112), _WHITE),
    ("belkins_card", (255, 255, 255, 0.78), (255, 255, 255, 0.08), None),
    ("belkins_card_edge", (0, 0, 0, 0.07), (255, 255, 255, 0.12), None),
    ("belkins_tile_claude", (255, 255, 255, 0.78), (255, 255, 255, 0.08), None),
    ("belkins_tile_claude_edge", (0, 0, 0, 0.07), (255, 255, 255, 0.12), None),
    ("belkins_tile_codex", (255, 255, 255, 0.78), (255, 255, 255, 0.08), None),
    ("belkins_tile_codex_edge", (0, 0, 0, 0.07), (255, 255, 255, 0.12), None),
    ("belkins_glass_btn", (0, 0, 0, 0.06), (255, 255, 255, 0.10), None),
    ("belkins_glass_btn_edge", (0, 0, 0, 0.09), (255, 255, 255, 0.15), None),
    ("belkins_inner_panel", (0, 0, 0, 0.04), (16, 10, 38, 0.42), None),
    ("belkins_inner_edge", (0, 0, 0, 0.07), (255, 255, 255, 0.10), None),
    ("belkins_plan_pill", (0, 0, 0, 0.06), (255, 255, 255, 0.11), (255, 255, 255, 0.18)),
    ("belkins_plan_text", (72, 78, 92), (212, 218, 232), _WHITE),
    ("belkins_arcade_card", (255, 251, 238, 0.80), (42, 28, 82, 0.65), None),
    ("belkins_arcade_edge", (217, 155, 12, 0.42), (250, 204, 21, 0.45), None),
    ("belkins_hero_card", (255, 255, 255, 0.78), (255, 255, 255, 0.08), None),
    ("belkins_hero_edge", (0, 0, 0, 0.07), (255, 255, 255, 0.12), None),
    ("belkins_flame_rule", (242, 82, 34), (255, 102, 51), _WHITE),
    ("belkins_claude_rule", (217, 119, 87), (235, 138, 104), _WHITE),
    ("belkins_codex_rule", (16, 163, 127), (52, 211, 153), _WHITE),
    ("belkins_attn_rule", (255, 149, 0), (255, 159, 10), _WHITE),
    ("belkins_sprite_pad", (22, 24, 34, 0.82), (14, 16, 24, 0.62), None),
    ("belkins_sprite_edge", (242, 82, 34, 0.50), (255, 114, 62, 0.50), None),
    ("belkins_solar_pad", (44, 20, 16, 0.82), (32, 14, 12, 0.62), None),
    ("belkins_solar_edge", (250, 190, 25, 0.78), (250, 204, 21, 0.80), None),
    ("belkins_emerald_pad", (12, 36, 28, 0.82), (10, 26, 20, 0.62), None),
    ("belkins_emerald_edge", (40, 199, 138, 0.75), (52, 211, 153, 0.80), None),
    ("belkins_nebula_pad", (24, 16, 46, 0.82), (16, 11, 34, 0.62), None),
    ("belkins_nebula_edge", (168, 85, 247, 0.78), (192, 132, 252, 0.80), None),
    ("belkins_boss_pad", (40, 14, 20, 0.82), (28, 10, 15, 0.62), None),
    ("belkins_boss_edge", (239, 68, 68, 0.70), (248, 113, 113, 0.75), None),
    ("belkins_gold_rule", (217, 155, 12), (250, 204, 21), _WHITE),
    ("belkins_gold_text", (170, 112, 0), (250, 204, 21), _WHITE),
    ("belkins_gold_pill", (250, 190, 25, 0.16), (250, 204, 21, 0.20), (255, 255, 255, 0.22)),
    ("belkins_purple_rule", (147, 51, 234), (192, 132, 252), _WHITE),
    ("belkins_purple_text", (126, 34, 206), (216, 180, 254), _WHITE),
    ("belkins_purple_pill", (168, 85, 247, 0.16), (192, 132, 252, 0.22), (255, 255, 255, 0.22)),
    ("belkins_tab_bar", (0, 0, 0, 0.05), (10, 12, 20, 0.42), None),
    ("belkins_tab_active", (242, 82, 34, 0.16), (255, 102, 51, 0.28), None),
    ("belkins_tab_active_glass", (255, 255, 255, 0.88), (255, 255, 255, 0.15), None),
    ("belkins_tab_active_edge", (0, 0, 0, 0.12), (255, 255, 255, 0.30), None),
    ("belkins_tab_active_fill", (242, 82, 34), (245, 92, 42), _WHITE),
    ("belkins_tab_active_text", (20, 22, 28), (255, 255, 255), _WHITE),
    ("belkins_tab_inactive_text", (65, 70, 82, 0.90), (215, 222, 235, 0.82), _WHITE),
    ("belkins_subtab_bg", (0, 0, 0, 0.045), (255, 255, 255, 0.06), None),
    ("belkins_subtab_active", (242, 82, 34, 0.15), (255, 108, 58, 0.24), None),
    ("belkins_tab_edge", (242, 82, 34, 0.45), (255, 112, 56, 0.55), None),
    ("belkins_xp_fill", (242, 82, 34), (255, 112, 56), _WHITE),
    ("belkins_xp_solar", (234, 158, 12), (250, 204, 21), _WHITE),
    ("belkins_xp_emerald", (16, 185, 129), (52, 211, 153), _WHITE),
    ("belkins_xp_nebula", (168, 85, 247), (192, 132, 252), _WHITE),
    ("belkins_xp_track", (0, 0, 0, 0.10), (255, 255, 255, 0.14), (255, 255, 255, 0.28)),
    ("belkins_flame_text", (208, 62, 18), (255, 126, 74), _WHITE),
    ("belkins_flame_pill", (242, 82, 34, 0.14), (255, 108, 58, 0.20), (255, 255, 255, 0.22)),
    ("belkins_good_pill", (40, 167, 69, 0.14), (48, 209, 88, 0.18), (255, 255, 255, 0.22)),
    ("belkins_warn_pill", (255, 149, 0, 0.14), (255, 159, 10, 0.20), (255, 255, 255, 0.22)),
    ("belkins_crit_pill", (255, 59, 48, 0.14), (255, 69, 58, 0.20), (255, 255, 255, 0.22)),
    ("belkins_badge_on", (250, 190, 25, 0.16), (250, 204, 21, 0.18), (255, 255, 255, 0.22)),
    ("belkins_badge_on_edge", (217, 155, 12, 0.35), (250, 204, 21, 0.38), None),
    ("belkins_badge_off", (0, 0, 0, 0.04), (255, 255, 255, 0.05), (255, 255, 255, 0.12)),
    ("belkins_badge_off_edge", (0, 0, 0, 0.07), (255, 255, 255, 0.08), None),
    ("belkins_hair", (0, 0, 0, 0.07), (255, 255, 255, 0.08), None),
    ("belkins_track", (0, 0, 0, 0.09), (255, 255, 255, 0.12), (255, 255, 255, 0.28)),
    ("belkins_track_edge", (0, 0, 0, 0.06), (255, 255, 255, 0.08), None),
    ("belkins_warn_fill", (255, 149, 0), (255, 159, 10), _WHITE),
    ("belkins_crit_fill", (255, 59, 48), (255, 69, 58), _WHITE),
    ("belkins_good_fill", (34, 197, 94), (52, 211, 153), _WHITE),
)

SEV_FILL = {
    "ok": "belkins_good_fill",
    "warn": "belkins_warn_fill",
    "crit": "belkins_crit_fill",
    "good": "belkins_good_fill",
    "dim": "dead_fill",
}
SEV_TEXT = {
    "ok": "label",
    "warn": "warn_text",
    "crit": "crit_text",
    "good": "good_text",
    "dim": "secondary",
}
CAP_ROLE = {"dim": "secondary", "accent": "belkins_flame_text", "ok": "secondary", "tag": "belkins_flame_text"}

_roles_done = False


def _ensure_roles() -> None:
    global _roles_done
    if _roles_done:
        return
    for name, light, dark, hover in _ROLES:
        register_role(name, light, dark, hover=hover)
    _roles_done = True


def _rebrand_bswap(s: str) -> str:
    """Replace legacy ``cswap`` references with ``bswap``."""
    return s.replace("cswap ", "bswap ").replace("`cswap`", "`bswap`").replace(": cswap", ": bswap")


# ---------------------------------------------------------- draw helpers ---


class _Measure:
    """A Pen stand-in for the layout pass: measures text, draws nothing."""

    hl = False

    def __init__(self) -> None:
        self._pen = Pen()

    def width(self, *a: Any, **k: Any) -> float:
        return self._pen.width(*a, **k)

    def fit(self, *a: Any, **k: Any) -> str:
        return self._pen.fit(*a, **k)

    def text(
        self,
        s: str,
        x: float,
        y: float,
        size: float,
        weight: Any = "regular",
        role: Any = "label",
        *,
        mono: bool = False,
        right: bool = False,
        center: bool = False,
        maxw: float | None = None,
        code: bool = False,
        kern: float | None = None,
        rounded: bool = False,
    ) -> float:
        if not s:
            return 0.0
        s = self._pen.fit(s, size, weight, maxw, mono=mono, code=code, kern=kern, rounded=rounded)
        return self._pen.width(s, size, weight, mono=mono, code=code, kern=kern, rounded=rounded) if s else 0.0

    def chip(
        self,
        text_s: str,
        x: float,
        y: float,
        size: float = 10.0,
        *,
        role: Any = "secondary",
        bg: Any = "chip",
        maxw: float | None = None,
        weight: Any = "regular",
    ) -> float:
        if not text_s:
            return 0.0
        inner = None if maxw is None else max(0.0, maxw - 6.0)
        return (
            self._pen.width(self._pen.fit(text_s, size, weight, inner, code=True), size, weight, code=True)
            + 6.0
        )

    def pill(
        self,
        text_s: str,
        x: float,
        y: float,
        size: float = 10.0,
        *,
        role: Any = "accent_tag",
        bg: Any = "accent_pill",
        weight: Any = "semibold",
    ) -> float:
        return self._pen.width(text_s, size, weight) + 10.0 if text_s else 0.0

    def symbol(self, name: str, x: float, y: float, size: float, *a: Any, **k: Any) -> float:
        return size

    def _nothing(self, *a: Any, **k: Any) -> None:
        return None

    rrect = panel = bar = circle = line = _nothing


def text(
    pen: Any,
    s: str,
    fk: str,
    role: str,
    x: float,
    baseline: float,
    *,
    maxw: float | None = None,
    right: bool = False,
    center: bool = False,
) -> float:
    if not s or (maxw is not None and maxw <= 0):
        return 0.0
    size, weight, mono, code, rounded = F[fk]
    return pen.text(
        s,
        x,
        baseline,
        size,
        weight,
        role,
        mono=mono,
        code=code,
        right=right,
        center=center,
        maxw=maxw,
        rounded=rounded,
    )


def text_w(pen: Any, s: str, fk: str) -> float:
    size, weight, mono, code, rounded = F[fk]
    return pen.width(s, size, weight, mono=mono, code=code, rounded=rounded)


def segments(pen: Any, segs: list[tuple], x: float, baseline: float, maxw: float) -> float:
    cur, end = x, x + maxw
    for s, fk, role, *style in segs:
        room = end - cur
        if room <= 6:
            break
        if style and style[0] == "code":
            cur += pen.chip(s, cur, baseline, F[fk][0], role=role, maxw=room, weight=F[fk][1])
        else:
            cur += text(pen, s, fk, role, cur, baseline, maxw=room)
    return cur - x


def luxury_bar(
    pen: Any,
    x: float,
    mid_y: float,
    w: float,
    pct: float | None,
    sev: str,
    *,
    h: float = 5.5,
    expired: bool = False,
    fill_override: str | None = None,
) -> None:
    """Luxury continuous precision gauge with quartile micro-ticks, top glass sheen, and specular leading edge."""
    if isinstance(pen, _Measure) or w <= 4.0:
        return
    y = mid_y - h / 2.0
    r = h / 2.0
    fill_role = "dead_fill" if expired else (fill_override or SEV_FILL.get(sev, "ok"))
    frac = 0.0 if (pct is None or expired) else max(0.0, min(1.0, float(pct) / 100.0))

    # 1. Recessed precision capsule track
    pen.rrect(x, y, w, h, r, "belkins_track", stroke="belkins_track_edge", stroke_width=0.5)

    try:
        from AppKit import NSBezierPath, NSColor
        from Foundation import NSMakeRect
    except Exception:
        NSBezierPath = None  # type: ignore[assignment]

    # 2. Subtle quartile micro-ticks (25%, 50%, 75%) inside track
    if NSBezierPath is not None and w >= 42.0:
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.09).setFill()
        for q in (0.25, 0.50, 0.75):
            tx = x + w * q
            NSBezierPath.fillRect_(NSMakeRect(tx, y + 1.0, 0.75, max(1.0, h - 2.0)))

    # 3. Continuous status fill + top specular glass sheen + leading edge indicator
    if frac > 0.005:
        fill_w = max(h, w * frac)
        pen.rrect(x, y, fill_w, h, r, fill_role)
        if NSBezierPath is not None and not expired:
            # Top glass sheen along filled bar
            if fill_w > r * 2.0:
                NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.24).setFill()
                NSBezierPath.fillRect_(NSMakeRect(x + r * 0.6, y + 0.6, max(1.0, fill_w - r * 1.2), max(1.0, h * 0.36)))
            # Specular leading-edge jewel cap
            if 0.03 < frac < 0.995 and fill_w >= h + 1.5:
                NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.72).setFill()
                NSBezierPath.fillRect_(NSMakeRect(x + fill_w - 2.4, y + 0.8, 1.6, max(1.0, h - 1.6)))


def segmented_bar(
    pen: Any,
    x: float,
    mid_y: float,
    w: float,
    pct: float | None,
    sev: str,
    *,
    h: float = 5.5,
    cells: int = 8,
    expired: bool = False,
    fill_override: str | None = None,
) -> None:
    """Delegates to ``luxury_bar`` for a unified, sleek precision gauge experience."""
    del cells
    luxury_bar(pen, x, mid_y, w, pct, sev, h=h, expired=expired, fill_override=fill_override)


def draw_pixel_companion(
    pen: Any,
    stage: EvolutionStage | RaidBossSpec,
    x: float,
    y: float,
    size: float,
    *,
    mood_key: str = "locked_in",
    equipped: set[str] | tuple[str, ...] = (),
    is_boss: bool = False,
    animate: bool = False,
) -> None:
    """Draw a luxury 14x14 pixel-art Belkins companion or Raid Boss inside a double-bezel studio medallion."""
    if isinstance(pen, _Measure):
        return
    eq = set(equipped or ())
    if is_boss:
        pad_role, edge_role = "belkins_boss_pad", "belkins_boss_edge"
        ped_rgb = (248, 113, 113)
    elif "solar_pad" in eq:
        pad_role, edge_role = "belkins_solar_pad", "belkins_solar_edge"
        ped_rgb = (250, 204, 21)
    elif "emerald_pad" in eq:
        pad_role, edge_role = "belkins_emerald_pad", "belkins_emerald_edge"
        ped_rgb = (52, 211, 153)
    elif "nebula_pad" in eq:
        pad_role, edge_role = "belkins_nebula_pad", "belkins_nebula_edge"
        ped_rgb = (192, 132, 252)
    elif "shiny_core" in eq:
        pad_role, edge_role = "belkins_sprite_pad", "belkins_solar_edge"
        ped_rgb = (250, 204, 21)
    else:
        pad_role, edge_role = "belkins_sprite_pad", "belkins_sprite_edge"
        ped_rgb = (255, 114, 62)

    med_r = 10.0 if size >= 40.0 else 6.5
    pen.rrect(x, y, size, size, med_r, pad_role, stroke=edge_role, stroke_width=1.15)
    grid = stage.sprite
    rows = len(grid)
    cols = len(grid[0]) if rows else 14
    pad = max(2.2, size * 0.088)
    cell = (size - 2.0 * pad) / float(max(rows, cols))
    ox = x + pad
    oy = y + pad

    # 4-frame lightweight retro idle phase (~2.2 FPS, zero state retention)
    frame = (int(time.monotonic() * 2.2) + (2 if is_boss else 0)) % 4 if animate else 0
    body_bob_y = (cell * 0.42) if (animate and frame in (1, 2)) else 0.0
    spark_bob_y = (-cell * 0.35) if (animate and frame % 2 == 1) else 0.0
    blink_frame = animate and (frame == 3)

    if not is_boss and "shiny_core" in eq:
        # Royal Aurum, Cyber-Platinum & Imperial Amethyst Mythic Palette
        b_r, b_g, b_b = (245, 196, 48)
        a_r, a_g, a_b = (224, 242, 254)
        c_r, c_g, c_b = (192, 132, 252)
        s_r, s_g, s_b = (56, 189, 248)
        o_rgb = (54, 46, 82)
    else:
        b_r, b_g, b_b = stage.body_rgb
        a_r, a_g, a_b = stage.accent_rgb
        c_r, c_g, c_b = stage.crest_rgb
        s_r, s_g, s_b = stage.spark_rgb
        o_rgb = (46, 52, 74) if not is_boss else (58, 28, 38)
        if not is_boss:
            if mood_key == "overdrive":
                s_r, s_g, s_b = (255, 110, 48)
            elif mood_key == "wall_guard":
                s_r, s_g, s_b = (251, 191, 36)

    palette = {
        "O": (o_rgb[0], o_rgb[1], o_rgb[2], 1.0),
        "B": (b_r, b_g, b_b, 1.0),
        "A": (a_r, a_g, a_b, 1.0),
        "C": (c_r, c_g, c_b, 1.0),
        "E": (255, 255, 255, 1.0),
        "S": (s_r, s_g, s_b, 0.96),
    }
    try:
        from AppKit import NSBezierPath, NSColor
        from Foundation import NSMakeRect
    except Exception:
        return

    # Subtle inner glass bezel & illuminated holo-pedestal platform under the companion's feet
    if size >= 30.0:
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.06).setStroke()
        inner_bp = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x + 1.8, y + 1.8, size - 3.6, size - 3.6), max(2.0, med_r - 1.5), max(2.0, med_r - 1.5)
        )
        inner_bp.setLineWidth_(0.6)
        inner_bp.stroke()

        # Glowing holo-stage ellipse at the base so feet/silhouette stand on a lit pedestal
        ped_w = size * 0.68
        ped_h = max(2.8, cell * 1.15)
        ped_x = x + (size - ped_w) / 2.0
        ped_y = oy + (rows - 1.15) * cell
        NSColor.colorWithSRGBRed_green_blue_alpha_(
            ped_rgb[0] / 255.0, ped_rgb[1] / 255.0, ped_rgb[2] / 255.0, 0.24
        ).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(ped_x, ped_y, ped_w, ped_h)).fill()
        NSColor.colorWithSRGBRed_green_blue_alpha_(
            ped_rgb[0] / 255.0, ped_rgb[1] / 255.0, ped_rgb[2] / 255.0, 0.52
        ).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(ped_x + ped_w * 0.18, ped_y + ped_h * 0.22, ped_w * 0.64, ped_h * 0.52)
        ).fill()

    # Nebula Pad twinkling starfield pixels
    if not is_boss and "nebula_pad" in eq:
        for s_idx, (sc, sr) in enumerate(((1, 3), (12, 2), (2, 11), (11, 12))):
            twinkle_a = 0.92 if (not animate or (s_idx + frame) % 2 == 0) else 0.35
            sz_mul = 0.82 if (animate and (s_idx + frame) % 2 == 0) else 0.62
            NSColor.colorWithSRGBRed_green_blue_alpha_(216 / 255.0, 180 / 255.0, 254 / 255.0, twinkle_a).setFill()
            NSBezierPath.fillRect_(NSMakeRect(ox + sc * cell, oy + sr * cell, cell * sz_mul, cell * sz_mul))

    # Context Berry glowing crimson aura sparks
    if not is_boss and "context_berry" in eq:
        NSColor.colorWithSRGBRed_green_blue_alpha_(244 / 255.0, 63 / 255.0, 94 / 255.0, 0.90).setFill()
        for b_idx, (bc, br) in enumerate(((0, 4), (13, 4), (0, 9), (13, 9))):
            by_off = (-cell * 0.35) if (animate and (b_idx + frame) % 2 == 1) else 0.0
            NSBezierPath.fillRect_(NSMakeRect(ox + bc * cell, oy + br * cell + by_off, cell + 0.1, cell + 0.1))

    # Cyber Halo orbital lightning nodes
    if not is_boss and "cyber_halo" in eq:
        halo_nodes = ((1, 1), (12, 1), (13, 6), (12, 12), (1, 12), (0, 6))
        for h_idx, (hc, hr) in enumerate(halo_nodes):
            is_pulse = animate and (h_idx % 3 == frame % 3)
            if is_pulse:
                NSColor.colorWithSRGBRed_green_blue_alpha_(224 / 255.0, 242 / 255.0, 254 / 255.0, 1.0).setFill()
            else:
                NSColor.colorWithSRGBRed_green_blue_alpha_(56 / 255.0, 189 / 255.0, 248 / 255.0, 0.90).setFill()
            NSBezierPath.fillRect_(NSMakeRect(ox + hc * cell, oy + hr * cell, cell + 0.2, cell + 0.2))

    # Mini-Drone Sidekick hovering in top-left corner
    if not is_boss and "mini_drone" in eq:
        drone_bob = (-cell * 0.45) if (animate and frame in (1, 3)) else 0.0
        beacon_rgb = (250, 204, 21) if frame % 2 == 0 else (52, 211, 153)
        drone_px = (
            (0, 1, (56, 189, 248)),
            (1, 1, beacon_rgb),
            (0, 2, (248, 250, 252)),
            (1, 2, (56, 189, 248)),
        )
        for dc, dr, (rr, gg, bb) in drone_px:
            NSColor.colorWithSRGBRed_green_blue_alpha_(rr / 255.0, gg / 255.0, bb / 255.0, 1.0).setFill()
            NSBezierPath.fillRect_(NSMakeRect(ox + dc * cell, oy + dr * cell + drone_bob, cell + 0.2, cell + 0.2))

    do_bevel = size >= 30.0
    for r_idx, row_str in enumerate(grid):
        # Feet stay planted in bottom 2 rows while upper torso/head gently breathes
        row_bob = body_bob_y if r_idx < (rows - 2) else 0.0
        prev_row = grid[r_idx - 1] if r_idx > 0 else ""
        for c_idx, ch in enumerate(row_str):
            rgba = palette.get(ch)
            if rgba is None:
                continue
            cr, cg, cb_val, alpha = rgba
            py_off = spark_bob_y if ch == "S" else row_bob
            if ch == "E" and blink_frame and (is_boss or "deal_shades" not in eq):
                cr, cg, cb_val = b_r, b_g, b_b
            NSColor.colorWithSRGBRed_green_blue_alpha_(
                cr / 255.0, cg / 255.0, cb_val / 255.0, alpha
            ).setFill()
            px = ox + c_idx * cell
            py = oy + r_idx * cell + py_off
            NSBezierPath.fillRect_(NSMakeRect(px, py, cell + 0.25, cell + 0.25))
            # Subtle 1px top-specular micro-bevel on top-exposed armor/crest cells
            if do_bevel and ch in ("B", "C", "A") and (not prev_row or prev_row[c_idx] != ch):
                NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.24).setFill()
                NSBezierPath.fillRect_(NSMakeRect(px, py, cell + 0.2, max(0.7, cell * 0.28)))
            if not is_boss and ch == "E" and "deal_shades" in eq:
                if blink_frame and c_idx >= (cols // 2):
                    NSColor.colorWithSRGBRed_green_blue_alpha_(224 / 255.0, 242 / 255.0, 254 / 255.0, 1.0).setFill()
                else:
                    NSColor.colorWithSRGBRed_green_blue_alpha_(6 / 255.0, 182 / 255.0, 212 / 255.0, 1.0).setFill()
                NSBezierPath.fillRect_(NSMakeRect(px - cell * 0.2, py, cell * 1.4, cell + 0.25))

    # Sovereign Crown headgear (with dark rim base, gold prongs, and glowing center jewel)
    if not is_boss and "sovereign_crown" in eq:
        crown_px = (
            (5, 0, (254, 240, 138)),
            (7, 0, (254, 249, 195) if (animate and frame == 1) else (250, 204, 21)),
            (9, 0, (254, 240, 138)),
            (5, 1, (245, 158, 11)),
            (6, 1, (250, 204, 21)),
            (7, 1, (244, 63, 94) if "shiny_core" in eq else (56, 189, 248)),  # Center crown jewel!
            (8, 1, (250, 204, 21)),
            (9, 1, (245, 158, 11)),
        )
        for cc, cr_y, (rr, gg, bb) in crown_px:
            NSColor.colorWithSRGBRed_green_blue_alpha_(rr / 255.0, gg / 255.0, bb / 255.0, 1.0).setFill()
            NSBezierPath.fillRect_(NSMakeRect(ox + cc * cell, oy + cr_y * cell + body_bob_y, cell + 0.25, cell + 0.25))

    # Plasma Token Blade weapon on right side
    if not is_boss and "plasma_blade" in eq:
        blade_px = (
            (13, 3, (34, 211, 238)),
            (13, 4, (34, 211, 238)),
            (13, 5, (56, 189, 248)),
            (13, 6, (232, 121, 249)),
            (12, 7, (250, 204, 21)),
            (13, 7, (250, 204, 21)),
            (13, 8, (148, 163, 184)),
        )
        gleam_row = 3 + (frame % 3) if animate else -1
        for bc, br, (rr, gg, bb) in blade_px:
            if br == gleam_row:
                rr, gg, bb = (224, 242, 254)
            NSColor.colorWithSRGBRed_green_blue_alpha_(rr / 255.0, gg / 255.0, bb / 255.0, 1.0).setFill()
            NSBezierPath.fillRect_(NSMakeRect(ox + bc * cell, oy + br * cell + body_bob_y * 0.6, cell + 0.25, cell + 0.25))


def symbol(
    pen: Any, name: str, x: float, mid: float, size: float, role: str, weight: str = "semibold"
) -> float:
    return pen.symbol(name, x, mid, size, role, weight, anchor="leftmid")


def hairline(pen: Any, x0: float, x1: float, y: float) -> None:
    pen.line(x0, y, x1, "belkins_hair", thickness=0.5)


def _pill_w(pen: Any, s: str) -> float:
    return text_w(pen, s, "tag") + 10.0


def flow(pen: Any, caps: list[tuple[str, str]], x: float, baseline: float, maxw: float) -> int:
    lines, cur = 1, x
    sep = "  ·  "
    sep_w = text_w(pen, sep, "caption")
    prev_tag = False
    for i, (cap, kind) in enumerate(caps):
        tag = kind == "tag"
        fk = "caption_med" if kind in ("accent", "good", "crit", "warn") else "caption"
        w = _pill_w(pen, cap) if tag else text_w(pen, cap, fk)
        gap = 6.0 if (tag or prev_tag) else sep_w
        if i and cur + gap + w > x + maxw:
            lines += 1
            baseline += CAP_STEP
            cur = x
        elif i:
            if not (tag or prev_tag):
                text(pen, sep, "caption", "tertiary", cur, baseline)
            cur += gap
        if tag:
            cur += pen.pill(
                cap,
                cur,
                baseline - 0.5,
                F["tag"][0],
                role="belkins_flame_text",
                bg="belkins_flame_pill",
                weight=F["tag"][1],
            )
        else:
            cur += text(
                pen,
                cap,
                fk,
                CAP_ROLE.get(kind, SEV_TEXT.get(kind, "secondary")),
                cur,
                baseline,
                maxw=x + maxw - cur,
            )
        prev_tag = tag
    return lines


def _clip_to(w: float, h: float, draw: Callable[[], None]) -> None:
    from AppKit import NSBezierPath, NSGraphicsContext
    from Foundation import NSMakeRect

    NSGraphicsContext.saveGraphicsState()
    try:
        NSBezierPath.clipRect_(NSMakeRect(0, 0, w, h))
        draw()
    finally:
        NSGraphicsContext.restoreGraphicsState()


def _card_surface_roles(default_fill: str, default_edge: str, default_rule: str | None) -> tuple[str, str, str | None]:
    """If the user equipped `arcade_skin` in the Mart, reskin all cards into the Gold Arcade Console theme!"""
    eq = load_game_save()["equipped"]
    if "arcade_skin" in eq:
        return "belkins_arcade_card", "belkins_arcade_edge", (default_rule or "belkins_gold_rule")
    return default_fill, default_edge, default_rule


def _draw_cmm_backdrop(pen: Any, w: float, h: float, *, top_glow: bool = False) -> None:
    """Paint CleanMyMac's cohesive deep indigo-violet ambient canvas across a block."""
    if isinstance(pen, _Measure) or w <= 0 or h <= 0:
        return
    pen.rrect(0.0, 0.0, w, h, 0.0, "belkins_cmm_canvas")
    if not top_glow:
        return
    try:
        from AppKit import NSBezierPath, NSColor, NSGradient, NSGraphicsContext
        from Foundation import NSMakePoint, NSMakeRect

        NSGraphicsContext.saveGraphicsState()
        try:
            NSBezierPath.clipRect_(NSMakeRect(0.0, 0.0, w, h))
            c0 = NSColor.colorWithSRGBRed_green_blue_alpha_(0.37, 0.95, 0.84, 0.28)
            c1 = NSColor.colorWithSRGBRed_green_blue_alpha_(0.37, 0.95, 0.84, 0.0)
            grad = NSGradient.alloc().initWithStartingColor_endingColor_(c0, c1)
            center = NSMakePoint(w - 39.0, 29.0)
            grad.drawFromCenter_radius_toCenter_radius_options_(center, 2.0, center, 46.0, 0)
        finally:
            NSGraphicsContext.restoreGraphicsState()
    except Exception:
        pass


def _draw_cmm_laptop_emblem(pen: Any, x: float, y: float) -> None:
    """Draw CleanMyMac's glowing Aqua-Mint MacBook illustration on the top-right of the header."""
    if isinstance(pen, _Measure):
        return
    try:
        from AppKit import NSBezierPath, NSColor, NSGraphicsContext
        from Foundation import NSMakeRect

        NSGraphicsContext.saveGraphicsState()
        try:
            # Outer screen lid bezel (34 x 22pt)
            lid_x, lid_y, lid_w, lid_h = x + 4.0, y + 6.0, 34.0, 22.0
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.18, 0.24, 0.38, 0.95).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(lid_x, lid_y, lid_w, lid_h), 3.2, 3.2
            ).fill()
            # Luminous Aqua-Mint inner display (31 x 19pt)
            scr_x, scr_y, scr_w, scr_h = lid_x + 1.5, lid_y + 1.5, lid_w - 3.0, lid_h - 3.0
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.48, 0.97, 0.89, 0.96).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(scr_x, scr_y, scr_w, scr_h), 2.2, 2.2
            ).fill()
            # Top-half bright white-aqua screen sheen
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.84, 1.0, 0.98, 0.55).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(scr_x + 1.0, scr_y + 1.0, scr_w - 2.0, scr_h * 0.48), 1.5, 1.5
            ).fill()
            # Chamfered MacBook aluminum-indigo base deck (42 x 4.5pt)
            base_x, base_y, base_w, base_h = x, lid_y + lid_h + 1.2, 42.0, 4.2
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.56, 0.64, 0.86, 0.92).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(base_x, base_y, base_w, base_h), 2.0, 2.0
            ).fill()
            # Glowing cyan front lip highlight on the base
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.37, 0.95, 0.84, 0.85).setFill()
            NSBezierPath.fillRect_(NSMakeRect(base_x + 3.0, base_y + 0.6, base_w - 6.0, 1.2))
            # Center lid-open notch
            NSColor.colorWithSRGBRed_green_blue_alpha_(0.16, 0.14, 0.32, 0.90).setFill()
            NSBezierPath.fillRect_(NSMakeRect(base_x + 17.0, base_y, 8.0, 1.4))
        finally:
            NSGraphicsContext.restoreGraphicsState()
    except Exception:
        pass


def draw_glass_tile(
    pen: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    r: float,
    fill: str,
    edge: str,
    *,
    rule: str | None = None,
    stroke_width: float = 0.75,
) -> None:
    """Draw a CleanMyMac-style soft frosted glass tile over the deep indigo-violet canvas."""
    del rule
    if isinstance(pen, _Measure) or w <= 0 or h <= 0:
        return
    pen.rrect(x, y, w, h, r, fill, stroke=edge, stroke_width=stroke_width)


class _Card:
    """One rounded Liquid Glass card spread over consecutive blocks ("parts")."""

    def __init__(self, fill: str, edge: str, rule: str | None = None) -> None:
        self.fill, self.edge, self.rule = fill, edge, rule
        self.parts: list[tuple[float, Callable[[Any, float], None], dict[str, Any]]] = []

    def add(self, draw: Callable[[Any, float], float], **block_kw: Any) -> None:
        block_kw.setdefault("no_hover_bg", True)
        height = float(round(draw(_Measure(), 0.0)))
        self.parts.append((height, draw, block_kw))

    def blocks(self, gap_before: float, gap_after: float) -> list[Block]:
        card_h = sum(h for h, _, _ in self.parts)
        out: list[Block] = []
        off = 0.0
        last = len(self.parts) - 1
        for i, (h, draw, kw) in enumerate(self.parts):
            lead = gap_before if i == 0 else 0.0
            trail = gap_after if i == last else 0.0
            out.append(
                Block(lead + h + trail, self._painter(draw, lead - off, lead, h, card_h, kw), **kw)
            )
            off += h
        return out

    def _painter(
        self,
        draw: Callable[[Any, float], float],
        card_y: float,
        lead: float,
        h: float,
        card_h: float,
        kw: dict[str, Any],
    ) -> Callable[[Pen, float, float], None]:
        base_fill, base_edge, base_rule = self.fill, self.edge, self.rule
        inset = kw.get("hl_inset", ROW_HL["hl_inset"])
        vinset = kw.get("hl_vinset", ROW_HL["hl_vinset"])
        radius = kw.get("hl_radius", ROW_HL["hl_radius"])

        def paint(pen: Pen, w: float, bh: float) -> None:
            _draw_cmm_backdrop(pen, w, bh)
            fill, edge, rule = _card_surface_roles(base_fill, base_edge, base_rule)

            def surface() -> None:
                draw_glass_tile(pen, CARD_X, card_y, w - 2 * CARD_X, card_h, 12.0, fill, edge, rule=rule)

            _clip_to(w, bh, surface)
            if pen.hl:
                pen.rrect(inset, lead + vinset, w - 2 * inset, h - 2 * vinset, radius, "hl")
            draw(pen, lead)

        return paint


# ------------------------------------------------ CleanMyMac Header ---


def _companion_card(snapshot: Any, companion: CompanionState, actions: ThemeActions) -> _Card:
    """CleanMyMac-style borderless top header (`Mac Health: Good` / `MacBook Pro` + glowing laptop on top-right)."""
    open_dash = getattr(actions, "open_dashboard", None)
    dash_enabled = bool(snapshot.settings.get("dashboard_enabled", True))
    act = open_dash if (callable(open_dash) and dash_enabled) else None

    def paint_header(pen: Pen, w: float, bh: float) -> None:
        _draw_cmm_backdrop(pen, w, bh, top_glow=True)
        live = companion_from_snapshot(snapshot)
        coins_live = live.coins_available

        # Determine overall health label ("Good" vs "Attention")
        notes = getattr(snapshot, "account_notes", {}) or {}
        active_acc = getattr(snapshot, "active", None)
        active_pct = max(
            float(getattr(active_acc, "five_hour_pct", 0.0) or 0.0),
            float(getattr(active_acc, "seven_day_pct", 0.0) or 0.0),
        ) if active_acc else 0.0
        health_word = "Good" if active_pct < 85.0 else "High Load"
        health_role = "belkins_cmm_aqua" if active_pct < 85.0 else "belkins_cmm_coral"

        lx = 18.0
        b1 = 27.0
        w_prefix = pen.text("AI Health: ", lx, b1, 17.0, "semibold", "label")
        pen.text(health_word, lx + w_prefix, b1, 17.0, "bold", health_role)

        b2 = 45.5
        sub_parts = [live.stage.name, f"Lv.{live.level}"]
        if coins_live > 0:
            sub_parts.append(f"✦ {_fmt_compact_coins(coins_live)} CR")
        elif notes:
            sub_parts.append(f"{len(notes)} slot{'s' if len(notes) != 1 else ''} flagged")
        pen.text("  ·  ".join(sub_parts), lx, b2, 11.5, "regular", "belkins_cmm_sub", maxw=w - 94.0)

        # Glowing Aqua-Mint MacBook illustration on the top-right
        _draw_cmm_laptop_emblem(pen, w - 60.0, 12.0)

    tip_lines = [
        f"Belkins Companion: {companion.stage.name} (Lv.{companion.level} · {companion.stage.title})",
        f"Passive: {companion.stage.passive} · ATK: {companion.boss.player_atk} · Crit: {companion.boss.player_crit_pct}%",
        f"Mood: {companion.mood_label} · XP: {companion.xp_total:,} (+{companion.xp_today:,} today)",
        f"Trophies: {companion.unlocked_badges_count}/{len(companion.badges)} · Bosses Slain: {companion.boss.bosses_slain}",
        "Click to open Belkins Dex & Usage Dashboard",
    ]
    tip = "\n".join(tip_lines)

    class _HeaderWrap:
        def blocks(self, _before: float, _after: float) -> list[Block]:
            return [
                Block(
                    58.0,
                    paint_header,
                    key="belkins-companion-hero",
                    tooltip=tip,
                    ax=tip.replace("\n", ". "),
                    action=act,
                    no_hover_bg=True,
                )
            ]

    return _HeaderWrap()  # type: ignore[return-value]


# --------------------------------------------------------- Specs & Cards ---


def _short_state(note: str, kind: str) -> tuple[str, str]:
    clean = _rebrand_bswap(note)
    head, _, tail = clean.partition(" — ")
    if kind == "fetch-failing":
        code = re.search(r"HTTP (\d{3})", clean)
        days = re.search(r"for (\d+ days?)", clean)
        state = f"HTTP {code.group(1)}" if code else "usage refused"
        if days:
            state += f" for {days.group(1)}"
        polls = re.search(r"(\d[\d,]*) failed polls", clean)
        return state, (f"{polls.group(1)} failed polls" if polls else "")
    cause = tail.split(";")[0].strip() if tail else ""
    return head.strip(), cause


def _short_action(note: str, kind: str) -> list[tuple]:
    clean = _rebrand_bswap(note)
    cmds = re.findall(r"`([^`]+)`", clean) or re.findall(r"run: (bswap [a-z]+(?: \d+)?)", clean)
    if kind == "fetch-failing":
        segs: list[tuple] = [("Log in again, then ", "secondary", "secondary")]
        if cmds:
            segs.append((cmds[0], "code", "secondary", "code"))
        rm = next((c for c in cmds if c.startswith("bswap remove")), None)
        if rm:
            segs += [("  or stop: ", "secondary", "secondary"), (rm, "code", "secondary", "code")]
        return segs
    if "re-login" in (kind + clean).lower():
        segs = [("Log in with Claude Code, then ", "secondary", "secondary")]
        if cmds:
            segs.append((cmds[-1], "code", "secondary", "code"))
        return segs
    return [(clean.split(" — ")[-1][:80], "secondary", "secondary")]


def _attention_spec(A: Any, snap: Any, actions: ThemeActions) -> dict[str, Any] | None:
    items = []
    by_slot = {r.slot: r for r in snap.accounts}
    for vendor, ref in A.needs_attention_rows(snap):
        if vendor == "claude":
            row = by_slot.get(ref)
            raw_note = snap.account_notes.get(ref, "")
            note = _rebrand_bswap(raw_note)
            kind = snap.account_note_kinds.get(ref, "")
            state, cause = _short_state(note, kind)
            age = (
                f"{A._age_label(row.usage_age_seconds)} ago"
                if row and row.usage_age_seconds is not None
                else ""
            )
            name = A._display_name(row) if row else f"slot {ref}"
            tip_text = f"{name} (slot {ref}) · {note}"
            if raw_note and raw_note != note:
                tip_text += f"\n{raw_note}"
            items.append(
                {
                    "key": f"belkins-attn-claude-{ref}",
                    "name": name,
                    "provider": "Claude",
                    "state": state,
                    "state_role": "warn_text",
                    "cause": cause,
                    "age": age,
                    "action_segs": _short_action(note, kind),
                    "action": None,
                    "tip": tip_text,
                }
            )
        else:
            row = ref
            act = actions.codex_login_for(row)
            login = getattr(A, "_codex_login", None)
            detail = _rebrand_bswap(login.relogin_detail(row)) if login is not None else ""
            if act is not None:
                segs = [("Log in again…", "secondary_med", "belkins_flame_text")]
            else:
                segs = [(detail, "secondary", "secondary")] if detail else []
            items.append(
                {
                    "key": f"belkins-attn-codex-{row.slot}",
                    "name": A._quota_alias(row),
                    "provider": "Codex",
                    "state": row.attention_note,
                    "state_role": "crit_text" if row.attention_kind == "crit" else "warn_text",
                    "cause": detail,
                    "age": f"{A._age_label(row.usage_age_seconds)} ago" if row.usage_age_seconds else "",
                    "action_segs": segs,
                    "action": act,
                    "tip": "\n".join(
                        x for x in (f"Codex {A._quota_head(row)} · {row.attention_note}", detail) if x
                    ),
                }
            )
    extras = [_rebrand_bswap(e) for e in actions.attention_extras if e]
    if not items and not extras:
        return None
    return {"items": items, "extras": extras}


def _claude_bars(A: Any, row: Any, now: float) -> list[tuple[str, float, str, bool]]:
    dead = set(row.expired_windows or ())
    out = []
    for label, key, pct, raw in (
        ("5h", "five_hour", row.five_hour_pct, row.five_hour_resets_at),
        ("7d", "seven_day", row.seven_day_pct, row.seven_day_resets_at),
    ):
        if pct is not None:
            out.append((label, pct, A._reset_mark_text(raw, now), key in dead))
    for name, pct in row.scoped_windows:
        if pct is not None:
            raw = next((r for n, r in row.scoped_resets_at if n == name), None)
            out.append((name, pct, A._reset_mark_text(raw, now), name in dead))
    return out


def _claude_tip(A: Any, row: Any, now: float, note: str = "", accounts: tuple = ()) -> str:
    parts = [f"{A._display_name(row)} · slot #{row.slot} · {row.email}"]
    plan = " · ".join(
        x
        for x in (
            A._claude_plan_text(accounts, row),
            row.org_name,
            A._same_login_note(accounts, row),
        )
        if x
    )
    if plan:
        parts.append(plan)
    if note:
        parts.append(_rebrand_bswap(note))
    else:
        for label, pct, mark, expired in _claude_bars(A, row, now):
            parts.append(
                f"{label} {format_pct(pct)}{' (window ended)' if expired else ''} {mark}".strip()
            )
    if row.usage_is_stale:
        parts.append(f"reading {A._age_label(row.usage_age_seconds)} old")
    if row.disabled:
        parts.append("disabled (bswap disable): never auto-selected, still polled")
    parts.append("active login" if row.is_active else f"click to switch (bswap {row.slot})")
    return "\n".join(parts)


def _claude_spec(A: Any, R: Any, snap: Any, now: float, actions: ThemeActions) -> dict[str, Any] | None:
    if not snap.accounts:
        return None
    models = tuple(snap.autoswitch_models or ())
    notes = snap.account_notes
    active = snap.active
    room = A._claude_room(snap)
    right = []
    if room:
        right.append(f"{room[0]}/{room[1]} room")
    if snap.autoswitch_enabled:
        right.append("AUTO")
    if snap.settings.get("scoped_fleet_line_enabled", True):
        scoped = dict.fromkeys(n for r in snap.accounts for n, _ in r.scoped_windows)
        right += [
            h
            for h in (
                A._scoped_fleet_heading(snap.accounts, name=n, now=now, notes=notes) for n in scoped
            )
            if h
        ]
    hero = None
    accounts = tuple(snap.accounts)
    if active is not None:
        note = _rebrand_bswap(notes.get(active.slot, ""))
        binding = None if note else A._binding_window(active, models)
        captions = []
        if active.usage_is_stale and not note:
            captions.append((f"reading {A._age_label(active.usage_age_seconds)} old", "dim"))
        spend = A._spend_of(active)
        if spend:
            used, limit, cur = spend
            sp = A._spend_pct(active)
            captions.append(
                (
                    f"extra usage {A._money(used, cur)} of {A._money(limit, cur)}",
                    R.severity(sp) if sp else "dim",
                )
            )
        hero = {
            "name": A._display_name(active),
            "slot": active.slot,
            "plan": A._claude_plan_text(accounts, active) or A.plan_label(active.plan_type),
            "sub": active.email,
            "raw_pct": binding[1] if binding else None,
            "big": (format_pct(binding[1]), binding[0], R.severity(binding[1])) if binding else None,
            "status": (
                _rebrand_bswap(
                    A._account_status_note(
                        active, note, snap.account_note_kinds.get(active.slot, "")
                    )
                ),
                "warn",
            )
            if note
            else None,
            "bars": [] if note else _claude_bars(A, active, now),
            "captions": captions,
            "tip": _claude_tip(A, active, now, note, accounts),
            "action": None,
        }
    targets = [r for r in A._switch_targets(snap.accounts) if r.slot not in notes]
    nxt = next((r for r in targets if not r.disabled), None)
    rows = []
    for r in targets:
        binding = A._binding_window(r, models)
        caps = []
        if r is nxt:
            caps.append(("NEXT", "tag"))
        sub = A._subscription_caption(accounts, r)
        if sub:
            caps.append((sub, "dim"))
        if r.disabled:
            caps.append(("disabled · bswap enable", "dim"))
        if r.usage_is_stale:
            caps.append((f"{A._age_label(r.usage_age_seconds)} old", "dim"))
        rows.append(
            {
                "key": f"belkins-claude-{r.slot}",
                "slot": f"#{r.slot}",
                "name": A._display_name(r),
                "plan": A.plan_label(r.plan_type),
                "label": binding[0] if binding else "",
                "pct": binding[1] if binding else None,
                "sev": R.severity(binding[1]) if binding else "dim",
                "reset": A._reset_mark_text(A._window_reset(r, binding[0]), now) if binding else "",
                "withheld": None,
                "captions": caps,
                "tip": _claude_tip(A, r, now, accounts=accounts),
                "action": (lambda slot=r.slot: actions.switch_to(slot)),
            }
        )
    return {
        "title": "Claude",
        "icon": "sparkles",
        "rule": "belkins_claude_rule",
        "right": " · ".join(right),
        "right_parts": right,
        "hero": hero,
        "rows": rows,
        "total_count": len(accounts),
        "key": "claude",
    }


def _codex_caps(A: Any, row: Any, *, compact: bool = False) -> list[tuple[str, str]]:
    caps = []
    if row.attention_note and row.attention_kind == "crit":
        caps.append((row.attention_note, "crit"))
    for line in row.info_notes or ():
        if (line.startswith("↺") and "usable now" in line) or re.search(r"\bback\b", line):
            caps.append((line, "good"))
        elif line.startswith(A._PACE_NOTE_PREFIX):
            caps.append((line, "warn"))
        elif not compact:
            caps.append((line, "dim"))
    if row.usage_is_stale:
        caps.append((f"{A._age_label(row.usage_age_seconds)} old", "dim"))
    if row.disabled:
        caps.append(("disabled", "dim"))
    return caps


def _codex_tip(A: Any, row: Any, now: float) -> str:
    lines = [f"Codex {A._quota_head(row)} · {row.email}"]
    win = A._plan_window(row)
    if win and win[1] is not None:
        lines.append(f"{win[0]} {format_pct(win[1])} {A._quota_reset_mark(row, now)}")
    note, _ = A._quota_note(row)
    if note:
        lines.append(note)
    lines += list(row.info_notes or ())
    lines.append("active in ~/.codex" if row.is_active else "Click to switch active ~/.codex account (or bswap codex switch)")
    return "\n".join(lines)


def _codex_spec(A: Any, R: Any, snap: Any, now: float, actions: ThemeActions) -> dict[str, Any] | None:
    rows_all = A._visible_quota_rows_of(snap)
    live = [r for r in rows_all if r.vendor != A.VENDOR_CLAUDE and r.slot < 0]
    if not live:
        return None
    parts = (
        A._codex_fleet_parts(rows_all, now=now)
        if snap.settings.get("codex_fleet_line_enabled", True)
        else None
    )
    right = []
    if parts:
        right.append(f"{parts.room}/{parts.total} room")
        mark = R.reset_mark(parts.next_at, now)
        if mark:
            right.append(f"next {mark}" + (f" ({parts.next_alias})" if parts.next_alias else ""))
        if parts.resets:
            right.append(parts.resets)
    active = next((r for r in live if r.is_active), None)
    best_of = getattr(A, "best_codex_row", None)
    best = best_of(live) if best_of is not None else None
    hero = None
    if active is not None:
        win = A._plan_window(active)
        alarm = A._quota_alarm(active)
        known = bool(win) and win[1] is not None and not alarm
        act = actions.codex_login_for(active)
        caps = _codex_caps(A, active, compact=False)
        caps_compact = _codex_caps(A, active, compact=True)
        if act is not None:
            caps.append(("Log in again…", "accent"))
            caps_compact.append(("Log in again…", "accent"))
        hero = {
            "name": A._quota_alias(active),
            "plan": A.plan_label(active.plan_type),
            "slot": None,
            "sub": active.email,
            "raw_pct": win[1] if known else None,
            "big": (format_pct(win[1]), win[0], "dim" if win[2] else R.severity(win[1]))
            if known
            else None,
            "status": (active.attention_note, "warn") if alarm else None,
            "bars": [(win[0], win[1], A._quota_reset_mark(active, now), win[2])] if known else [],
            "captions": caps,
            "captions_compact": caps_compact,
            "tip": _codex_tip(A, active, now),
            "action": act,
        }
    rows = []
    for r in live:
        if r is active or A._quota_alarm(r):
            continue
        win = A._plan_window(r)
        act = actions.codex_login_for(r)
        row_alias = A._quota_alias(r)
        if act is None and row_alias:
            act = (lambda target=row_alias: actions.switch_codex(target))
        caps = _codex_caps(A, r, compact=True)
        if r is best:
            caps.insert(0, ("BEST", "tag"))
        if actions.codex_login_for(r) is not None:
            caps.append(("Log in again…", "accent"))
        rows.append(
            {
                "key": f"belkins-codex-{r.slot}",
                "slot": "",
                "name": row_alias,
                "plan": A.plan_label(r.plan_type),
                "label": "",
                "pct": win[1] if win else None,
                "sev": "dim" if (not win or win[2]) else R.severity(win[1]),
                "reset": A._quota_reset_mark(r, now) if win else "",
                "withheld": None,
                "captions": caps,
                "tip": _codex_tip(A, r, now),
                "action": act,
            }
        )
    return {
        "title": "Codex",
        "icon": "terminal.fill",
        "rule": "belkins_codex_rule",
        "right": " · ".join(right),
        "right_parts": right,
        "hero": hero,
        "rows": rows,
        "total_count": len(live),
        "key": "codex",
    }


# ---------------------------------------------------------------- painters ---


def _attn_head(count: int) -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        base = y0 + HEAD_BASE
        symbol(pen, "exclamationmark.triangle.fill", X0, base - 4.5, 11.5, "belkins_warn_fill")
        text(pen, "Needs attention", "title", "label", X0 + 18, base)
        cnt_s = str(count)
        pw = _pill_w(pen, cnt_s)
        pen.pill(cnt_s, X1 - pw, base - 0.5, F["tag"][0], role="warn_text", bg="belkins_warn_pill", weight="semibold")
        return base + 8
    return draw


def _attn_item(it: dict[str, Any]) -> Callable[[Any, float], float]:
    chevron = it["action"] is not None

    def draw(pen: Any, y0: float) -> float:
        b1 = y0 + 16
        wname = text(pen, it["name"], "primary_med", "label", X0, b1, maxw=125)
        age_w = text_w(pen, it["age"], "secondary") if it["age"] else 0.0
        if chevron:
            chev_x = X1 - 6
            age_r = chev_x - 8
            symbol(pen, "chevron.right", chev_x, b1 - 4, 9, "secondary")
        else:
            age_r = X1
        text(pen, it["age"], "secondary", "secondary", age_r, b1, right=True)
        sx = X0 + wname + 6
        text(pen, it["state"], "primary", it["state_role"], sx, b1, maxw=age_r - age_w - 8 - sx)
        if not it["action_segs"]:
            return b1 + ROW_END
        b2 = b1 + 17
        segments(pen, it["action_segs"], X0, b2, W_INNER - (14 if chevron else 0))
        return b2 + ROW_END

    return draw


def _attn_grouped_item(group: list[dict[str, Any]], *, show_tab_hint: bool = True) -> Callable[[Any, float], float]:
    """Compact 2-line summary for multiple accounts sharing the same alert state — guarantees `bswap add` is never truncated."""
    first = group[0]
    n = len(group)
    provider = first["provider"]
    state = first["state"]
    state_role = first["state_role"]

    def _short_alias(s: str) -> str:
        return (s[:10] + "…") if len(s) > 11 else s

    names = [_short_alias(g["name"]) for g in group]
    if len(names) <= 3:
        names_s = ", ".join(names)
    else:
        names_s = f"{', '.join(names[:3])} +{len(names) - 3} more"

    cmd_chip = next(
        (seg[0] for seg in (first.get("action_segs") or ()) if len(seg) >= 4 and seg[3] == "code"),
        "bswap add",
    )

    def draw(pen: Any, y0: float) -> float:
        b1 = y0 + 16.0
        head_s = f"{n} {provider} slots"
        whead = text(pen, head_s, "primary_med", "label", X0, b1)
        hint_w = 54.0 if show_tab_hint else 0.0
        text(pen, state, "primary", state_role, X0 + whead + 7.0, b1, maxw=X1 - (X0 + whead + 7.0 + hint_w))
        if show_tab_hint:
            text(pen, f"{provider} ▸", "caption_med", "secondary", X1, b1, right=True)

        b2 = b1 + 17.5
        # Draw right-aligned `Run [bswap add]` FIRST so the command chip is 100% immune to clipping!
        chip_w = text_w(pen, cmd_chip, "code") + 6.0
        run_lbl = "Run "
        run_w = text_w(pen, run_lbl, "caption_med")
        pen.chip(cmd_chip, X1 - chip_w, b2, F["code"][0], role="belkins_flame_text", bg="belkins_flame_pill", weight=F["code"][1])
        text(pen, run_lbl, "caption_med", "secondary", X1 - chip_w - run_w - 2.0, b2)

        left_maxw = max(40.0, X1 - chip_w - run_w - 10.0 - X0)
        text(pen, names_s, "caption", "secondary", X0, b2, maxw=left_maxw)
        return b2 + ROW_END

    return draw


def _word_lines(pen: Any, s: str, fk: str, maxw: float, most: int) -> list[str]:
    if most <= 1:
        return [s]
    words, lines, cur = s.split(), [], ""
    for i, word in enumerate(words):
        trial = f"{cur} {word}" if cur else word
        if cur and text_w(pen, trial, fk) > maxw:
            lines.append(cur)
            if len(lines) == most - 1:
                return lines + [" ".join(words[i:])]
            cur = word
        else:
            cur = trial
    return lines + [cur] if cur else lines


def _attn_extra(line: str) -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        b = y0 + 16
        for i, part in enumerate(_word_lines(pen, line.split("\n")[0], "primary", W_INNER, 3)):
            if i:
                b += 16.0
            text(pen, part, "primary", "label", X0, b, maxw=W_INNER)
        return b + ROW_END

    return draw


def _gap(h: float) -> Callable[[Any, float], float]:
    return lambda pen, y0: y0 + h


def _hair() -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        hairline(pen, X0, X1, y0 + 2)
        return y0 + 3

    return draw


def _card_head(spec: dict[str, Any], *, one_line: bool = False, show_chevron: bool = False) -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        base = y0 + HEAD_BASE
        if spec.get("icon"):
            symbol(pen, spec["icon"], X0, base - 4.0, 11.5, spec.get("rule", "belkins_flame_text"), "semibold")
        tx = X0 + (17.5 if spec.get("icon") else 0.0)
        tw = text(pen, spec["title"], "title", "label", tx, base)
        right_edge = X1
        if show_chevron:
            symbol(pen, "chevron.right", X1 - 7.0, base - 3.8, 8.5, "tertiary", "semibold")
            right_edge = X1 - 12.0
        room = right_edge - (tx + tw + 10.0)
        parts = spec.get("right_parts") or []
        full = spec["right"]
        if one_line or len(parts) < 2 or text_w(pen, full, "secondary_med") <= room:
            shown_r = " · ".join(parts[:2]) if (one_line and len(parts) > 2) else full
            text(pen, shown_r, "secondary_med", "secondary", right_edge, base, maxw=room, right=True)
            return base + HEAD_AFTER
        text(pen, parts[0], "secondary_med", "secondary", right_edge, base, maxw=room, right=True)
        cap = base + 15.0
        text(pen, " · ".join(parts[1:]), "caption", "secondary", tx, cap, maxw=X1 - tx)
        return cap + HEAD_AFTER

    return draw


def _hero(hero: dict[str, Any], *, compact: bool = False) -> Callable[[Any, float], float]:
    from .. import render as R

    def _draw_inner(pen: Any, y0: float) -> float:
        px0 = X0 + 4.0
        px1 = X1 - 4.0
        b1 = y0 + 17.5
        b2 = b1 + 15.5
        big = hero["big"]
        bw = 0.0
        sev_dot = "belkins_good_fill"
        if hero["status"]:
            sev_dot = "belkins_warn_fill"
        elif big:
            pct_str, win_lbl, sev = big
            if sev == "crit":
                sev_dot = "belkins_crit_fill"
                badge_role, badge_bg = "crit_text", "belkins_crit_pill"
            elif sev == "warn":
                sev_dot = "belkins_warn_fill"
                badge_role, badge_bg = "warn_text", "belkins_warn_pill"
            elif sev == "dim":
                sev_dot = "dead_fill"
                badge_role, badge_bg = "secondary", "belkins_plan_pill"
            else:
                sev_dot = "belkins_good_fill"
                badge_role, badge_bg = "good_text", "belkins_good_pill"
            raw_val = hero.get("raw_pct")
            if raw_val is not None:
                free_pct = max(0, 100 - int(round(float(raw_val))))
                badge_txt = f"{free_pct}% FREE"
            else:
                badge_txt = f"{pct_str} · {win_lbl}"
            bw = _pill_w(pen, badge_txt)
            pen.pill(
                badge_txt,
                px1 - bw,
                b1 - 0.5,
                F["tag"][0],
                role=badge_role,
                bg=badge_bg,
                weight="semibold",
            )

        # Live status dot + Account Name + Plan Capsule Pill
        pen.circle(px0, b1 - 7.5, 6.8, sev_dot)
        nx = px0 + 11.5
        name_max = max(50.0, px1 - bw - 10.0 - nx)
        plan_raw = (hero.get("plan") or "").strip()
        plan_tag = plan_raw.upper().replace("_", " ") if plan_raw else ""
        pw = (_pill_w(pen, plan_tag) + 6.0) if plan_tag else 0.0
        nw = text(
            pen,
            hero["name"],
            "hero_name",
            "label",
            nx,
            b1,
            maxw=max(name_max - pw, name_max * 0.52),
        )
        if plan_tag and (nw + pw <= name_max + 4.0):
            pen.pill(
                plan_tag,
                nx + nw + 6.0,
                b1 - 0.5,
                F["tag"][0],
                role="belkins_plan_text",
                bg="belkins_plan_pill",
                weight="semibold",
            )

        sub = hero["sub"]
        parts_sub = []
        if hero.get("slot") is not None:
            parts_sub.append(f"Slot #{hero['slot']}")
        else:
            parts_sub.append("Active")
        if sub:
            parts_sub.append(sub)
        sub_line = "  ·  ".join(parts_sub)
        text(pen, sub_line, "caption", "secondary", nx, b2, maxw=px1 - nx)
        ink = b2 + 5.0

        if hero["status"]:
            s, kind = hero["status"]
            b = ink + 16.0
            text(pen, s, "primary_med", SEV_TEXT.get(kind, kind), nx, b, maxw=px1 - nx)
            ink = b + INK

        bx = HX + WIN_LABEL_W
        b = ink + 16.5 - WIN_STEP
        for label, pct, mark, expired in hero["bars"]:
            b += WIN_STEP
            sev = R.severity(pct)
            text(pen, label, "secondary_med", "secondary", px0 + 4.0, b, maxw=WIN_LABEL_W - 4.0)
            luxury_bar(
                pen,
                bx,
                b - 3.8,
                COL_PCT_R - COL_PCT_W - 6.0 - bx,
                pct,
                sev,
                h=5.6,
                expired=expired,
            )
            pct_role = "secondary" if expired else (SEV_TEXT[sev] if sev != "ok" else "label")
            text(pen, format_pct(pct), "num_bar", pct_role, COL_PCT_R, b, right=True)
            text(pen, "↺ ended" if expired else mark, "num_small", "secondary", px1, b, right=True)
            ink = b + INK

        caps_source = hero.get("captions_compact") if compact and "captions_compact" in hero else hero["captions"]
        # Render both `good` reset-credit notes and `warn` pace notes inside sleek structured micro-banners!
        banners = [
            c
            for c in caps_source
            if (c[1] == "good" and c[0].startswith("↺")) or (c[1] == "warn" and "pace" in c[0].lower())
        ]
        rest = [c for c in caps_source if c not in banners]
        for cap, kind in banners:
            y = ink + 6.5
            is_warn = kind == "warn"
            bg_r = "belkins_warn_pill" if is_warn else "belkins_good_pill"
            txt_r = "warn_text" if is_warn else "good_text"
            ic_name = "bolt.fill" if is_warn else "arrow.counterclockwise.circle.fill"
            pen.rrect(px0, y, px1 - px0, 21.0, 5.5, bg_r)
            clean_cap = cap.lstrip("↺ ").strip()
            if is_warn and clean_cap[:1].islower():
                clean_cap = clean_cap[:1].upper() + clean_cap[1:]
            symbol(pen, ic_name, px0 + 6.5, y + 10.5, 9.5, txt_r, "semibold")
            text(pen, clean_cap, "caption_med", txt_r, px0 + 19.5, y + 14.2, maxw=px1 - px0 - 25.0)
            ink = y + 21.0

        if rest:
            cb_y = ink + 13.5
            ink = cb_y + CAP_STEP * (flow(pen, rest, px0 + 4.0, cb_y, px1 - px0 - 4.0) - 1) + INK
        return ink + 10.0

    def draw(pen: Any, y0: float) -> float:
        end_y = _draw_inner(_Measure(), y0)
        if not isinstance(pen, _Measure):
            pen.rrect(
                X0 - 4.0,
                y0 + 1.5,
                W_INNER + 8.0,
                max(24.0, end_y - y0 - 5.5),
                8.5,
                "belkins_inner_panel",
                stroke="belkins_inner_edge",
                stroke_width=0.75,
            )
            _draw_inner(pen, y0)
        return end_y

    return draw


def _row(r: dict[str, Any]) -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        x = X0 + 18.0 if r["slot"] else X0 + 6.0
        label_w = text_w(pen, r["label"], "caption") if r["label"] else 0.0
        name_max = (COL_LABEL_R - label_w - 8.0 - x) if r["label"] else (COL_BAR_X - 6.0 - x)
        caps = list(r["captions"])
        plan_inline = bool(r["plan"]) and (
            text_w(pen, r["name"], "primary_med") + 6.0 + text_w(pen, r["plan"], "caption") <= name_max
        )
        if r["plan"] and not plan_inline:
            caps.insert(0, (r["plan"], "dim"))
        b = y0 + ROW_BASE
        if r["slot"]:
            text(pen, r["slot"], "slot", "belkins_flame_text", X0 + 14.0, b, right=True)
        nw = text(pen, r["name"], "primary_med", "label", x, b, maxw=name_max)
        if plan_inline:
            text(pen, r["plan"], "caption", "secondary", x + nw + 5.5, b)
        if r.get("withheld"):
            note, kind = r["withheld"]
            text(
                pen,
                "⚠ " + note,
                "secondary_med",
                "crit_text" if kind == "crit" else "warn_text",
                X1,
                b,
                right=True,
                maxw=X1 - COL_BAR_X,
            )
        else:
            if r["label"]:
                text(pen, r["label"], "caption", "secondary", COL_LABEL_R, b, right=True)
            luxury_bar(pen, COL_BAR_X, b - 3.8, COL_BAR_W, r["pct"], r["sev"], h=5.0)
            if r["pct"] is not None:
                text(
                    pen,
                    format_pct(r["pct"]),
                    "num",
                    SEV_TEXT[r["sev"]] if r["sev"] != "ok" else "label",
                    COL_PCT_R,
                    b,
                    right=True,
                )
            text(pen, r["reset"], "num_small", "secondary", X1, b, right=True)
        if not caps:
            return b + ROW_END
        lines = flow(pen, caps, x, b + ROW_CAP, X1 - x)
        return b + ROW_CAP + CAP_STEP * (lines - 1) + ROW_END

    return draw


def _ax(tip: str) -> str:
    return tip.replace("\n", ". ")


def _copy_cmd_to_pasteboard(cmd: str = "bswap add") -> None:
    """Copy a CLI remedy (`bswap add`) to the macOS pasteboard on click."""
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString

        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(cmd, NSPasteboardTypeString)
    except Exception:
        pass


def _provider_card(spec: dict[str, Any], attn_items: list[dict[str, Any]] | None = None) -> _Card:
    card = _Card("belkins_card", "belkins_card_edge", rule=spec.get("rule"))
    key = spec["key"]
    card.add(
        _card_head(spec),
        key=f"belkins-{key}-head",
        ax=f"{spec['title']}. {spec['right']}".strip(". "),
        tooltip=spec["right"],
    )
    hero = spec["hero"]
    if hero:
        card.add(
            _hero(hero, compact=False),
            key=f"belkins-{key}-active",
            tooltip=hero["tip"],
            ax=_ax(hero["tip"]),
            action=hero["action"],
            **ROW_HL,
        )
    if spec["rows"]:
        card.add(_hair(), key=f"belkins-{key}-rule")
        for r in spec["rows"]:
            card.add(
                _row(r),
                key=r["key"],
                tooltip=r["tip"],
                ax=_ax(r["tip"]),
                action=r["action"],
                **ROW_HL,
            )
    if attn_items:
        card.add(_hair(), key=f"belkins-{key}-attn-rule")
        if len(attn_items) >= 2:
            tips = "\n".join(it["tip"] for it in attn_items) + "\nClick to copy `bswap add` to clipboard"
            card.add(
                _attn_grouped_item(attn_items, show_tab_hint=False),
                key=f"belkins-{key}-attn-grouped",
                tooltip=tips,
                ax=_ax(tips),
                action=lambda: _copy_cmd_to_pasteboard("bswap add"),
                keep_open=True,
                **ROW_HL,
            )
        else:
            it = attn_items[0]
            act = it["action"] or (lambda: _copy_cmd_to_pasteboard("bswap add"))
            card.add(
                _attn_item(it),
                key=f"belkins-{key}-attn-single",
                tooltip=it["tip"],
                ax=_ax(it["tip"]),
                action=act,
                **ROW_HL,
            )
    card.add(_gap(6), key=f"belkins-{key}-foot")
    return card


def _attention_card(spec: dict[str, Any]) -> _Card:
    """Smart Needs-Attention card: collapses 2+ identical re-login slots into one clean summary row."""
    card = _Card("belkins_card", "belkins_card_edge", rule="belkins_attn_rule")
    count = len(spec["items"]) + len(spec["extras"])
    card.add(_attn_head(count), key="belkins-attn-head", ax=f"Needs attention: {count}")

    items = spec["items"]
    if len(items) > 1:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        standalone: list[dict[str, Any]] = []
        for it in items:
            if it["action"] is None:
                grouped.setdefault((it["provider"], it["state"]), []).append(it)
            else:
                standalone.append(it)

        for (prov, st_name), grp in grouped.items():
            if len(grp) >= 2:
                combined_tip = "\n".join(g["tip"] for g in grp)
                combined_ax = " ".join(f"{g['provider']} {g['name']}: {g['state']}." for g in grp)
                target_tab = "claude" if prov.lower() == "claude" else "codex"
                card.add(
                    _attn_grouped_item(grp),
                    key=f"belkins-attn-grouped-{prov.lower()}-{st_name[:12]}",
                    tooltip=combined_tip,
                    ax=combined_ax,
                    action=lambda t=target_tab: set_active_tab(t),
                    keep_open=True,
                    **ROW_HL,
                )
            else:
                standalone.extend(grp)

        for it in standalone:
            opens = "Logs in again." if it["action"] is not None else ""
            label = " ".join(
                x
                for x in (
                    f"{it['provider']} {it['name']}: {it['state']}.",
                    f"{it['cause']}." if it["cause"] else "",
                    f"Last seen {it['age']}." if it["age"] else "",
                    opens,
                )
                if x
            )
            card.add(
                _attn_item(it),
                key=it["key"],
                tooltip=it["tip"],
                ax=label,
                action=it["action"],
                **ROW_HL,
            )
    else:
        for it in items:
            opens = "Logs in again." if it["action"] is not None else ""
            label = " ".join(
                x
                for x in (
                    f"{it['provider']} {it['name']}: {it['state']}.",
                    f"{it['cause']}." if it["cause"] else "",
                    f"Last seen {it['age']}." if it["age"] else "",
                    opens,
                )
                if x
            )
            card.add(
                _attn_item(it),
                key=it["key"],
                tooltip=it["tip"],
                ax=label,
                action=it["action"],
                **ROW_HL,
            )

    for i, line in enumerate(spec["extras"]):
        card.add(_attn_extra(line), key=f"belkins-attn-extra-{i}", tooltip=line, ax=line)
    card.add(_gap(6), key="belkins-attn-foot")
    return card


# ------------------------------------------ Interactive Tabs, Dex & Shop ---


def _tab_bar_block(claude_count: int, codex_count: int) -> Block:
    """CleanMyMac-inspired low-profile glass pill strip (`Hub | Claude | Codex | Evo | Vault | Studio`)."""
    del claude_count, codex_count
    bar_w = W - 2 * CARD_X
    seg_w = bar_w / float(len(TABS))
    bar_h = 28.0
    total_h = 34.0

    def paint(pen: Pen, w: float, h: float) -> None:
        _draw_cmm_backdrop(pen, w, h)
        y = 2.0
        fill, edge, _ = _card_surface_roles("belkins_card", "belkins_card_edge", None)
        draw_glass_tile(pen, CARD_X, y, bar_w, bar_h, 9.0, fill, edge)
        for idx, (tab_id, label) in enumerate(TABS):
            tx = CARD_X + idx * seg_w
            is_act = _ACTIVE_TAB == tab_id
            if is_act:
                draw_glass_tile(
                    pen,
                    tx + 2.0,
                    y + 2.0,
                    seg_w - 4.0,
                    bar_h - 4.0,
                    7.0,
                    "belkins_tab_active_glass",
                    "belkins_tab_active_edge",
                )

            txt_role = "label" if is_act else "belkins_cmm_sub"
            ic_role = "belkins_cmm_aqua" if is_act else "belkins_cmm_icon"
            ic_name = _TAB_ICONS.get(tab_id, "sparkles")
            tw_lbl = text_w(pen, label, "caption_med")
            ic_sz = 8.0
            ic_adv = 12.0
            tot_w = ic_adv + tw_lbl
            start_x = tx + (seg_w - tot_w) / 2.0
            symbol(pen, ic_name, start_x, y + 14.0, ic_sz, ic_role, "semibold")
            text(
                pen,
                label,
                "caption_med",
                txt_role,
                start_x + ic_adv,
                y + 18.0,
                maxw=seg_w - ic_adv - 2.0,
            )

    def on_click_x(click_x: float) -> None:
        rel = max(0.0, min(bar_w - 1.0, click_x - CARD_X))
        idx = int(rel // seg_w)
        if 0 <= idx < len(TABS):
            set_active_tab(TABS[idx][0])

    def cycle_tab() -> None:
        ids = [t[0] for t in TABS]
        cur = ids.index(_ACTIVE_TAB) if _ACTIVE_TAB in ids else 0
        set_active_tab(ids[(cur + 1) % len(ids)])

    return Block(
        total_h,
        paint,
        key="belkins-tab-bar",
        tooltip="",
        ax="Belkins Tabs: Hub, Claude, Codex, Evo, Vault, Studio",
        action=cycle_tab,
        on_click_x=on_click_x,
        keep_open=True,
        no_hover_bg=True,
    )


def _compact_usd_str(val: float) -> str:
    v = float(val or 0.0)
    if v >= 10_000.0:
        return f"${v / 1000.0:.1f}K"
    if v >= 1_000.0:
        return f"${v:,.0f}"
    if v >= 100.0:
        return f"${v:.0f}"
    return format_usd(v)


def _draw_provider_bento_tile(
    pen: Pen,
    spec: dict[str, Any] | None,
    tx: float,
    ty: float,
    tw: float,
    th: float,
    tile_fill: str,
    tile_edge: str,
    fallback_title: str,
    fallback_icon: str,
    accent_role: str,
) -> None:
    """Render one CleanMyMac-style tile (`Macintosh HD` / `Memory` twin) with left icon, title, subtitle & borderless cyan action."""
    del accent_role
    fill, edge, _ = _card_surface_roles(tile_fill, tile_edge, None)
    draw_glass_tile(pen, tx, ty, tw, th, 12.0, fill, edge)

    ix_icon = tx + 13.0
    ix_text = tx + 36.0
    ix_right = tx + tw - 13.0
    text_maxw = max(40.0, ix_right - ix_text)

    b1 = ty + 23.5
    title = spec["title"] if spec else fallback_title
    icon_name = spec["icon"] if spec else fallback_icon
    symbol(pen, icon_name, ix_icon, b1 - 2.0, 14.5, "belkins_cmm_icon", "medium")
    text(pen, title, "hero_name", "label", ix_text, b1)

    if not spec:
        text(pen, "No accounts linked", "secondary", "belkins_cmm_sub", ix_text, ty + 42.0, maxw=text_maxw)
        return

    right_parts = spec.get("right_parts") or []
    room_part = next((p for p in right_parts if "room" in p), "")
    room_short = room_part.replace(" room", "") if room_part else ""

    hero = spec.get("hero")
    raw_pct = hero.get("raw_pct") if hero else None
    if raw_pct is not None and not (hero and hero.get("status")):
        free_pct = max(0, min(100, int(round(100.0 - float(raw_pct)))))
        top_right_s = f"{free_pct}% free"
        top_right_role = "belkins_cmm_coral" if free_pct <= 15 else "label"
    else:
        top_right_s = f"{room_short} ok" if room_short else ""
        top_right_role = "belkins_cmm_sub"
    if top_right_s:
        text(pen, top_right_s, "caption_med", top_right_role, ix_right, b1, right=True)

    b2 = ty + 41.5
    if hero:
        if hero.get("status"):
            st_txt, _ = hero["status"]
            text(pen, st_txt, "caption", "belkins_cmm_coral", ix_text, b2, maxw=text_maxw)
        else:
            hero_nm = str(hero.get("name") or "Active")
            sub_str = f"{hero_nm} ({room_short})" if (room_short and len(hero_nm) <= 10) else hero_nm
            text(pen, sub_str, "caption", "belkins_cmm_sub", ix_text, b2, maxw=text_maxw)
    else:
        text(pen, "No active session", "caption", "belkins_cmm_sub", ix_text, b2, maxw=text_maxw)

    # Bottom-right borderless Aqua-Mint action link (just like CleanMyMac's `Free Up` / `Test Speed`)
    b3 = ty + th - 13.0
    rows = spec.get("rows") or []
    best_r = rows[0] if rows else None
    if best_r and callable(best_r.get("action")):
        nm = str(best_r.get("name") or "")
        act_txt = f"Switch → {nm}"
    else:
        total_c = int(spec.get("total_count") or 1)
        act_txt = f"Fleet ({total_c})"
    text(pen, act_txt, "primary_med", "belkins_cmm_aqua", ix_right, b3, right=True, maxw=tw - 24.0)


def _hub_bento_blocks(
    claude_spec: dict[str, Any] | None,
    codex_spec: dict[str, Any] | None,
    snapshot: Any,
    attn: dict[str, Any] | None,
    actions: ThemeActions,
) -> list[Block]:
    """Build CleanMyMac-style Protection Banner + 2x2 Bento Grid (`Claude | Codex` and `Today Burn | 30d Spend`)."""
    from .. import card_base as _cb

    out: list[Block] = []
    tile_gap = 8.0
    tile_w = (W - 2 * CARD_X - tile_gap) / 2.0
    lx0 = CARD_X
    rx0 = CARD_X + tile_w + tile_gap

    # 1. Full-Width Protection / Engine Status Card (twin of CleanMyMac's `Protection by Moonlock · ✓ Protected`)
    auto_on = bool(getattr(snapshot, "autoswitch_enabled", True))
    thr = getattr(snapshot, "autoswitch_threshold", 85.0)
    thr_int = int(round(float(thr if thr is not None else 85.0)))
    items = (attn.get("items") or []) if attn else []
    extras = (attn.get("extras") or []) if attn else []
    claude_n = sum(1 for it in items if it.get("provider") == "Claude")
    codex_n = sum(1 for it in items if it.get("provider") == "Codex")

    if claude_n:
        scan_line = f"{claude_n} Claude slot{'s' if claude_n != 1 else ''} {'need' if claude_n != 1 else 'needs'} re-login · Run bswap add"
        target_banner_tab = "claude"
    elif codex_n:
        scan_line = f"{codex_n} Codex account{'s' if codex_n != 1 else ''} {'need' if codex_n != 1 else 'needs'} attention"
        target_banner_tab = "codex"
    elif extras:
        scan_line = str(extras[0]).lstrip("⚠ ").strip()
        target_banner_tab = "claude"
    else:
        c_nm = claude_spec["hero"]["name"] if (claude_spec and claude_spec.get("hero")) else "none"
        x_nm = codex_spec["hero"]["name"] if (codex_spec and codex_spec.get("hero")) else "none"
        scan_line = f"Active: {c_nm} (Claude) · {x_nm} (Codex)"
        target_banner_tab = "claude"

    all_tips = [it.get("tip", "") for it in items if it.get("tip")] + list(extras)
    full_tip = "\n".join(all_tips) if all_tips else "Auto-Switch Engine active"
    full_ax = ". ".join([scan_line] + list(extras))

    def paint_protection_banner(pen: Pen, w: float, h: float) -> None:
        _draw_cmm_backdrop(pen, w, h)
        ty = 4.0
        th = 74.0
        fill, edge, _ = _card_surface_roles("belkins_card", "belkins_card_edge", None)
        draw_glass_tile(pen, CARD_X, ty, w - 2 * CARD_X, th, 12.0, fill, edge)

        ix0 = CARD_X + 14.0
        ix1 = w - CARD_X - 14.0
        b1 = ty + 22.0
        symbol(pen, "checkmark.shield.fill", ix0, b1 - 4.0, 12.5, "good_text", "semibold")
        w_prot = text(pen, "Protection by ", "primary_med", "label", ix0 + 19.0, b1)
        symbol(pen, "bolt.fill", ix0 + 19.0 + w_prot + 1.0, b1 - 3.8, 10.0, "belkins_cmm_aqua", "semibold")
        text(pen, "Belkins", "primary_med", "label", ix0 + 19.0 + w_prot + 13.0, b1)
        status_right = f"✓ Active ({thr_int}%)" if auto_on else "Paused"
        text(pen, status_right, "caption_med", "belkins_cmm_sub", ix1, b1, right=True)

        b2 = ty + 44.5
        mon_txt = "Real-time quota & burn monitor ON" if auto_on else "Auto-switch monitor paused"
        text(pen, mon_txt, "primary_med", "label", ix0, b2, maxw=ix1 - ix0)

        b3 = ty + 62.5
        sub_role = "belkins_cmm_coral" if (claude_n or codex_n) else "belkins_cmm_sub"
        text(pen, scan_line, "secondary", sub_role, ix0, b3, maxw=ix1 - ix0)

    out.append(
        Block(
            82.0,
            paint_protection_banner,
            key="belkins-hub-alert-pill",
            ax=full_ax,
            tooltip=full_tip,
            action=lambda t=target_banner_tab: set_active_tab(t),
            keep_open=True,
            no_hover_bg=True,
        )
    )

    # 2. Bento Row 1: Side-by-Side Claude & Codex Tiles (`Macintosh HD` & `Memory` twins)
    if claude_spec or codex_spec:
        row1_h = 90.0

        def paint_providers(pen: Pen, w: float, h: float) -> None:
            _draw_cmm_backdrop(pen, w, h)
            _draw_provider_bento_tile(
                pen,
                claude_spec,
                lx0,
                3.0,
                tile_w,
                82.0,
                "belkins_tile_claude",
                "belkins_card_edge",
                "Claude",
                "sparkles",
                "belkins_claude_rule",
            )
            _draw_provider_bento_tile(
                pen,
                codex_spec,
                rx0,
                3.0,
                tile_w,
                82.0,
                "belkins_tile_codex",
                "belkins_card_edge",
                "Codex",
                "terminal.fill",
                "belkins_codex_rule",
            )

        def on_provider_bento_click(click_x: float) -> None:
            is_right = click_x >= (W / 2.0)
            target_sp = codex_spec if is_right else claude_spec
            target_tab = "codex" if is_right else "claude"
            if target_sp is None:
                target_sp = claude_spec or codex_spec
                target_tab = "claude" if claude_spec else "codex"
            click_y = getattr(_cb, "LAST_CLICK_Y", None)
            if click_y is not None and click_y >= 54.0 and target_sp:
                rows = target_sp.get("rows") or []
                if rows and callable(rows[0].get("action")):
                    rows[0]["action"]()
                    return
            set_active_tab(target_tab)

        c_tip = claude_spec["hero"]["tip"] if (claude_spec and claude_spec.get("hero")) else "Claude Fleet"
        x_tip = codex_spec["hero"]["tip"] if (codex_spec and codex_spec.get("hero")) else "Codex Fleet"
        bento_ax_parts = []
        if claude_spec:
            bento_ax_parts.append(f"Claude {claude_spec.get('right', '').replace(' room', ' ok')}")
        if codex_spec:
            bento_ax_parts.append(f"Codex {codex_spec.get('right', '').replace(' room', ' ok')}")
        out.append(
            Block(
                row1_h,
                paint_providers,
                key="belkins-hub-providers-bento",
                ax=". ".join(bento_ax_parts) or "Claude and Codex Bento Tiles",
                tooltip=f"{c_tip}\n\n{x_tip}",
                action=lambda: set_active_tab("claude" if claude_spec else "codex"),
                on_click_x=on_provider_bento_click,
                keep_open=True,
                no_hover_bg=True,
            )
        )

    # 3. Bento Row 2: Side-by-Side Today Burn & 30d Spend Tiles (`Battery` & `CPU` twins)
    cost = getattr(snapshot, "cost", None)
    if cost is not None and not getattr(cost, "is_partial", False):
        today_w = getattr(cost, "today", None)
        last7_w = getattr(cost, "last_7d", None)
        last30_w = getattr(cost, "last_30d", None)
        if today_w is not None and last7_w is not None and last30_w is not None:
            usd_today = float(getattr(today_w, "usd", 0.0) or 0.0)
            tok_today = int(getattr(today_w, "total_tokens", 0) or 0)
            usd_7d = float(getattr(last7_w, "usd", 0.0) or 0.0)
            usd_30d = float(getattr(last30_w, "usd", 0.0) or 0.0)
            if usd_30d > 0.0 or tok_today > 0:
                avg_7d = usd_7d / 7.0
                sess_cnt = len(getattr(snapshot, "sessions", ()) or ())
                open_dash = getattr(actions, "open_dashboard", None)
                dash_on = bool(snapshot.settings.get("dashboard_enabled", True))
                dash_act = open_dash if (callable(open_dash) and dash_on) else None

                def paint_telemetry(pen: Pen, w: float, h: float) -> None:
                    _draw_cmm_backdrop(pen, w, h)
                    ty = 2.0
                    th = 60.0
                    fill, edge, _ = _card_surface_roles("belkins_card", "belkins_card_edge", None)

                    # Left Tile: Today Burn (`Battery` twin)
                    draw_glass_tile(pen, lx0, ty, tile_w, th, 12.0, fill, edge)
                    b1 = ty + 23.5
                    b2 = ty + 42.0
                    symbol(pen, "bolt.horizontal.fill", lx0 + 13.0, b1 - 2.0, 14.0, "belkins_cmm_icon", "medium")
                    text(pen, "Today Burn", "hero_name", "label", lx0 + 36.0, b1)
                    text(pen, _compact_usd_str(usd_today), "primary_med", "label", lx0 + tile_w - 13.0, b1, right=True)
                    tok_s = f"{format_tokens(tok_today)} tokens" if tok_today > 0 else "Idle today"
                    text(pen, tok_s, "secondary", "belkins_cmm_sub", lx0 + 36.0, b2, maxw=tile_w - 48.0)

                    # Right Tile: 30d Spend (`CPU` twin)
                    draw_glass_tile(pen, rx0, ty, tile_w, th, 12.0, fill, edge)
                    symbol(pen, "cpu", rx0 + 13.0, b1 - 2.0, 14.0, "belkins_cmm_icon", "medium")
                    text(pen, "30d Spend", "hero_name", "label", rx0 + 36.0, b1)
                    text(pen, _compact_usd_str(usd_30d), "primary_med", "belkins_cmm_coral", rx0 + tile_w - 13.0, b1, right=True)
                    sub_30 = f"{_compact_usd_str(avg_7d)}/d · {sess_cnt} sess" if sess_cnt > 0 else f"Avg {_compact_usd_str(avg_7d)}/day"
                    text(pen, sub_30, "secondary", "belkins_cmm_sub", rx0 + 36.0, b2, maxw=tile_w - 48.0)

                out.append(
                    Block(
                        66.0,
                        paint_telemetry,
                        key="belkins-party-burn-velocity",
                        ax=f"Today Burn {format_usd(usd_today)}, 30d Spend {format_usd(usd_30d)}, 7d average {format_usd(avg_7d)} per day.",
                        tooltip="Click to open the full interactive HTML Usage & Cost Dashboard.",
                        action=dash_act,
                        no_hover_bg=True,
                    )
                )

    return out


def _dex_card(snapshot: Any, companion: CompanionState, subtab: str) -> _Card:
    """`✨ Evo` Tab: Interactive Boss Raid Arena + 8-Stage Cyber-Forms / 20-Trophy Bento Grid."""
    card = _Card("belkins_card", "belkins_card_edge", rule="belkins_flame_rule")

    # 1. Interactive Boss Raid Arena at the top of `✨ Evo` inside a recessed glass inner panel
    def _draw_boss_inner(pen: Any, y0: float) -> float:
        live = companion if isinstance(pen, _Measure) else companion_from_snapshot(snapshot)
        boss = live.boss
        px0 = X0 + 4.0
        px1 = X1 - 4.0
        base = y0 + 18.5
        symbol(pen, "flame.fill", px0, base - 4.0, 11.5, "belkins_crit_fill", "semibold")
        text(pen, "Boss Raid Arena", "title", "label", px0 + 16.5, base)
        right_hdr = f"{boss.bosses_slain} Slain  ·  {boss.player_atk} ATK ({boss.player_crit_pct}% Crit)"
        text(pen, right_hdr, "caption_med", "secondary", px1, base, right=True)

        box_top = base + 8.5
        sprite_sz = 40.0
        anim_on = bool(snapshot.settings.get("belkins_animations_enabled", True))
        draw_pixel_companion(pen, boss.spec, px0, box_top, sprite_sz, is_boss=True, animate=anim_on)

        rx = px0 + sprite_sz + 10.5
        rw = px1 - rx
        b1 = box_top + 12.5
        btn_txt = f"✦ STRIKE (-{boss.player_atk})"
        bw = _pill_w(pen, btn_txt)
        pen.pill(
            btn_txt,
            px1 - bw,
            b1 - 0.5,
            F["tag"][0],
            role="crit_text",
            bg="belkins_crit_pill",
            weight="semibold",
        )
        bname = boss.spec.name
        nw_boss = text(pen, bname, "primary_med", "label", rx, b1, maxw=rw - bw - 34.0)
        text(pen, f"Lv.{boss.level}", "caption_med", "crit_text", rx + nw_boss + 5.0, b1, maxw=max(0.0, rw - bw - nw_boss - 6.0))

        b2 = box_top + 27.5
        hp_txt = f"{boss.hp_current:,}/{boss.hp_max:,} HP"
        hw = text(pen, hp_txt, "num_small", "crit_text", px1, b2, right=True)
        luxury_bar(
            pen,
            rx,
            b2 - 3.8,
            max(60.0, rw - hw - 8.0),
            boss.hp_pct,
            "crit",
            h=5.6,
        )

        b3 = box_top + sprite_sz + 13.5
        note_s = _LAST_BOSS_NOTE or f"Bounty: +{boss.spec.coin_reward} ✦ CR & +{boss.spec.xp_reward // 1000}K XP · Click to strike"
        text(
            pen,
            note_s,
            "caption_med" if _LAST_BOSS_NOTE else "caption",
            "belkins_flame_text" if _LAST_BOSS_NOTE else "secondary",
            px0,
            b3,
            maxw=px1 - px0,
        )
        return b3 + 10.0

    def draw_boss_arena(pen: Any, y0: float) -> float:
        end_y = _draw_boss_inner(_Measure(), y0)
        if not isinstance(pen, _Measure):
            pen.rrect(
                X0 - 4.0,
                y0 + 3.5,
                W_INNER + 8.0,
                max(28.0, end_y - y0 - 7.5),
                8.5,
                "belkins_inner_panel",
                stroke="belkins_inner_edge",
                stroke_width=0.75,
            )
            _draw_boss_inner(pen, y0)
        return end_y

    def on_strike_boss() -> None:
        global _LAST_BOSS_NOTE
        live = companion_from_snapshot(snapshot)
        _ok, msg = strike_raid_boss(live)
        _LAST_BOSS_NOTE = msg

    card.add(
        draw_boss_arena,
        key=f"belkins-dex-boss-arena-{subtab}",
        ax=f"Boss Raid Arena: {companion.boss.spec.name} ({companion.boss.hp_current} of {companion.boss.hp_max} HP). Click to strike.",
        tooltip="Click to strike the Raid Boss with your Companion and equipped Vault Relics! Defeating bosses drops huge SparkCoins and Bonus XP.",
        action=on_strike_boss,
        keep_open=True,
        animated=True,
        **ROW_HL,
    )

    # 1b. Daily Streak & Supply Crate Row
    def draw_daily_crate(pen: Any, y0: float) -> float:
        live = companion if isinstance(pen, _Measure) else companion_from_snapshot(snapshot)
        b = y0 + 16.5
        avail = live.daily_crate_available
        ic_role = "belkins_xp_fill" if avail else "good_text"
        symbol(pen, "gift.fill" if avail else "checkmark.seal.fill", X0 + 4.0, b - 4.0, 10.5, ic_role, "semibold")
        pill_txt = "✦ CLAIM +150 CR" if avail else "✓ CLAIMED TODAY"
        pill_role = "belkins_xp_fill" if avail else "good_text"
        pill_bg = "belkins_gold_pill" if avail else "belkins_good_pill"
        pw = _pill_w(pen, pill_txt)
        pen.pill(pill_txt, X1 - pw, b - 0.5, F["tag"][0], role=pill_role, bg=pill_bg, weight="semibold")
        lbl = f"Daily Supply Crate · Day {live.streak_days} Streak"
        text(
            pen,
            lbl,
            "caption_med",
            "label" if avail else "secondary",
            X0 + 19.5,
            b,
            maxw=max(60.0, X1 - pw - (X0 + 19.5) - 8.0),
        )
        return b + 7.5

    def on_claim_crate() -> None:
        global _LAST_BOSS_NOTE
        _ok, msg = claim_daily_supply_drop()
        _LAST_BOSS_NOTE = msg

    card.add(
        draw_daily_crate,
        key=f"belkins-dex-daily-crate-{subtab}",
        ax="Daily Supply Crate. Click to claim daily SparkCoins and Bonus XP.",
        tooltip="Claim your daily Belkins Supply Crate (+150..300 CR & +15K..45K XP, scaling with your active daily streak)!",
        action=on_claim_crate,
        keep_open=True,
        **ROW_HL,
    )

    # 2. Sub-Tab Switcher inside `✨ Evo`: `[ Cyber-Forms | Hall of Fame ]` with vector SF Symbols
    def draw_dex_subtabs(pen: Any, y0: float) -> float:
        live = companion if isinstance(pen, _Measure) else companion_from_snapshot(snapshot)
        bar_y = y0 + 4.0
        bar_h = 25.0
        half_w = (W_INNER - 6.0) / 2.0
        pen.rrect(X0, bar_y, W_INNER, bar_h, 6.5, "belkins_subtab_bg")
        tabs_sub = (
            ("pokedex", "sparkles", f"Cyber-Forms ({live.highest_stage.stage}/8)"),
            ("trophies", "trophy.fill", f"Hall of Fame ({live.unlocked_badges_count}/{len(live.badges)})"),
        )
        for idx, (sid, ic_name, slabel) in enumerate(tabs_sub):
            sx = X0 + 2.5 + idx * (half_w + 1.0)
            is_on = _DEX_SUBTAB == sid
            if is_on:
                pen.rrect(sx, bar_y + 2.0, half_w, bar_h - 4.0, 5.0, "belkins_tab_active_fill")
            txt_role = "belkins_tab_active_text" if is_on else "belkins_tab_inactive_text"
            tw_lbl = text_w(pen, slabel, "caption_med")
            ic_sz = 9.0
            tot_w = ic_sz + 4.5 + tw_lbl
            start_x = sx + (half_w - tot_w) / 2.0
            symbol(pen, ic_name, start_x, bar_y + 12.5, ic_sz, txt_role, "semibold")
            text(
                pen,
                slabel,
                "caption_med",
                txt_role,
                start_x + ic_sz + 4.5,
                bar_y + 16.2,
                maxw=half_w - ic_sz - 10.0,
            )
        return bar_y + bar_h + 6.0

    def on_dex_subtab_click(click_x: float) -> None:
        if click_x < W / 2.0:
            set_dex_subtab("pokedex")
        else:
            set_dex_subtab("trophies")

    card.add(
        draw_dex_subtabs,
        key=f"belkins-dex-subtabs-{subtab}",
        ax="Switch between Pokedex and Trophy Room",
        tooltip="",
        action=lambda: set_dex_subtab("trophies" if _DEX_SUBTAB == "pokedex" else "pokedex"),
        on_click_x=on_dex_subtab_click,
        keep_open=True,
        no_hover_bg=True,
    )

    if subtab == "pokedex":
        for st in EVOLUTION_LINE:
            def draw_stage(
                pen: Any,
                y0: float,
                stage: EvolutionStage = st,
            ) -> float:
                live = companion if isinstance(pen, _Measure) else companion_from_snapshot(snapshot)
                cur = stage.stage == live.stage.stage
                unl = live.level >= stage.min_level
                eq_live = set(live.equipped_items)

                top = y0 + 2.5
                draw_pixel_companion(
                    pen,
                    stage,
                    X0,
                    top,
                    23.0,
                    mood_key="overdrive" if cur else "locked_in",
                    equipped=eq_live if cur else (),
                )
                bx = X0 + 30.0
                b1 = top + 15.5
                if cur:
                    tag_txt = "ACTIVE ✓"
                    tag_role, tag_bg = "belkins_flame_text", "belkins_flame_pill"
                elif unl:
                    tag_txt = "MORPH"
                    tag_role, tag_bg = "good_text", "belkins_good_pill"
                else:
                    tag_txt = f"Lv.{stage.min_level}"
                    tag_role, tag_bg = "secondary", "belkins_badge_off"
                tw_pill = _pill_w(pen, tag_txt)
                pen.pill(tag_txt, X1 - tw_pill, b1 - 0.5, F["tag"][0], role=tag_role, bg=tag_bg, weight="semibold")
                name_w = text(pen, f"{stage.stage}. {stage.name}", "primary_med", "label" if unl else "secondary", bx, b1)
                text(
                    pen,
                    f"·  {stage.passive}",
                    "caption",
                    "secondary",
                    bx + name_w + 5.0,
                    b1,
                    maxw=X1 - tw_pill - bx - name_w - 10.0,
                )
                return top + 26.0

            def on_morph(st_num: int = st.stage) -> None:
                global _LAST_BOSS_NOTE
                live = companion_from_snapshot(snapshot)
                _ok, msg = select_companion_stage(st_num, live.level)
                _LAST_BOSS_NOTE = msg

            card.add(
                draw_stage,
                key=f"belkins-dex-stage-{st.stage}",
                ax=f"Stage {st.stage} {st.name}: {st.title} ({st.passive}). {st.lore}",
                tooltip=f"#{st.stage} {st.name} (Lv.{st.min_level} · {st.title})\nPassive: {st.passive} · Base ATK: {st.base_atk}\n{st.lore}\nClick to morph your active Companion!",
                action=on_morph,
                keep_open=True,
                **ROW_HL,
            )
    else:
        badges = companion.badges
        col_gap = 8.0
        col_w = (W_INNER - col_gap) / 2.0
        for row_idx in range(0, len(badges), 2):
            pair = badges[row_idx : row_idx + 2]

            def draw_badge_pair(pen: Any, y0: float, r_idx: int = row_idx, default_pair: tuple = pair) -> float:
                live = companion if isinstance(pen, _Measure) else companion_from_snapshot(snapshot)
                items = live.badges[r_idx : r_idx + 2] if r_idx < len(live.badges) else default_pair
                box_y = y0 + 2.0
                box_h = 34.0
                b1 = box_y + 13.5
                b2 = box_y + 26.5
                for c_idx, b_item in enumerate(items):
                    cx = X0 + c_idx * (col_w + col_gap)
                    bg_role = "belkins_badge_on" if b_item.unlocked else "belkins_badge_off"
                    edge_role = "belkins_badge_on_edge" if b_item.unlocked else "belkins_badge_off_edge"
                    pen.rrect(cx, box_y, col_w, box_h, 6.0, bg_role, stroke=edge_role)
                    mark_ic = "checkmark.seal.fill" if b_item.unlocked else "lock.fill"
                    mark_role = "good_text" if b_item.unlocked else "secondary"
                    symbol(pen, mark_ic, cx + col_w - 15.5, b1 - 4.0, 8.5, mark_role, "semibold")
                    text(
                        pen,
                        f"{b_item.icon} {b_item.name}",
                        "caption_med",
                        "label" if b_item.unlocked else "secondary",
                        cx + 7.5,
                        b1,
                        maxw=col_w - 25.0,
                    )
                    sub_txt = b_item.description if b_item.unlocked else b_item.progress_text
                    sub_role = "belkins_gold_text" if b_item.unlocked else "secondary"
                    text(
                        pen,
                        sub_txt,
                        "tag",
                        sub_role,
                        cx + 7.5,
                        b2,
                        maxw=col_w - 15.0,
                    )
                return box_y + box_h + 4.0

            tips = "\n\n".join(
                f"{b.icon} {b.name} ({'UNLOCKED ✓' if b.unlocked else b.progress_text})\n{b.description}"
                for b in pair
            )
            axs = " · ".join(f"{b.name}: {b.progress_text}" for b in pair)
            card.add(
                draw_badge_pair,
                key=f"belkins-dex-badge-row-{row_idx // 2}",
                ax=axs,
                tooltip=tips,
            )

    card.add(_gap(8.0), key=f"belkins-dex-foot-{subtab}")
    return card


def _shop_card(snapshot: Any, companion: CompanionState, subtab: str) -> _Card:
    """`🛡️ Vault` Tab Card: 12-Item Belkins Spark Vault with `Relics & Lab` and `Skins & Bots` sub-tabs."""
    card = _Card("belkins_card", "belkins_card_edge", rule="belkins_gold_rule")

    def draw_shop_head(pen: Any, y0: float) -> float:
        live = companion if isinstance(pen, _Measure) else companion_from_snapshot(snapshot)
        coins_live = live.coins_available
        base = y0 + HEAD_BASE
        symbol(pen, "bolt.shield.fill", X0, base - 4.0, 11.5, "belkins_gold_rule", "semibold")
        tx = X0 + 18.0
        text(pen, "Belkins Spark Vault", "title", "label", tx, base)
        wallet = f"✦ {_fmt_compact_coins(coins_live)} CR"
        pw = _pill_w(pen, wallet)
        pen.pill(wallet, X1 - pw, base - 0.5, F["tag"][0], role="belkins_gold_text", bg="belkins_gold_pill", weight="semibold")
        b2 = base + 16.0
        sub = _LAST_SHOP_NOTE or "Every relic & mod upgrades your Companion, Boss ATK, or UI:"
        text(pen, sub, "caption_med" if _LAST_SHOP_NOTE else "caption", "belkins_flame_text" if _LAST_SHOP_NOTE else "secondary", X0, b2, maxw=W_INNER)
        return b2 + 6.0

    card.add(
        draw_shop_head,
        key=f"belkins-shop-head-{subtab}",
        ax=f"Belkins Spark Vault. {companion.coins_available} Coins available.",
        tooltip="Earn SparkCoins from token burn, Trophies (+250/badge), and Boss Raids in the Evo tab!",
    )

    # Sub-Tab Switcher inside `🛡️ Vault`: `[ Relics & Lab (6) | Skins & Bots (6) ]` with vector SF Symbols
    def draw_shop_subtabs(pen: Any, y0: float) -> float:
        bar_y = y0 + 2.0
        bar_h = 25.0
        half_w = (W_INNER - 6.0) / 2.0
        pen.rrect(X0, bar_y, W_INNER, bar_h, 6.5, "belkins_subtab_bg")
        tabs_sub = (
            ("gear", "bolt.shield.fill", "Relics & Lab (6)"),
            ("style", "paintbrush.pointed.fill", "Skins & Bots (6)"),
        )
        for idx, (sid, ic_name, slabel) in enumerate(tabs_sub):
            sx = X0 + 2.5 + idx * (half_w + 1.0)
            is_on = _SHOP_SUBTAB == sid
            if is_on:
                pen.rrect(sx, bar_y + 2.0, half_w, bar_h - 4.0, 5.0, "belkins_tab_active_fill")
            txt_role = "belkins_tab_active_text" if is_on else "belkins_tab_inactive_text"
            tw_lbl = text_w(pen, slabel, "caption_med")
            ic_sz = 9.0
            tot_w = ic_sz + 4.5 + tw_lbl
            start_x = sx + (half_w - tot_w) / 2.0
            symbol(pen, ic_name, start_x, bar_y + 12.5, ic_sz, txt_role, "semibold")
            text(
                pen,
                slabel,
                "caption_med",
                txt_role,
                start_x + ic_sz + 4.5,
                bar_y + 16.2,
                maxw=half_w - ic_sz - 10.0,
            )
        return bar_y + bar_h + 5.0

    def on_shop_subtab_click(click_x: float) -> None:
        if click_x < W / 2.0:
            set_shop_subtab("gear")
        else:
            set_shop_subtab("style")

    card.add(
        draw_shop_subtabs,
        key=f"belkins-shop-subtabs-{subtab}",
        ax="Switch between Gear & Lab and Skins & Pets",
        tooltip="",
        action=lambda: set_shop_subtab("style" if _SHOP_SUBTAB == "gear" else "gear"),
        on_click_x=on_shop_subtab_click,
        keep_open=True,
        no_hover_bg=True,
    )
    card.add(_hair(), key=f"belkins-shop-rule-{subtab}")

    items_for_sub = [it for it in SHOP_CATALOG if it.tab == subtab]
    for spec in items_for_sub:
        def draw_item(pen: Any, y0: float, item_spec: Any = spec) -> float:
            live = companion if isinstance(pen, _Measure) else companion_from_snapshot(snapshot)
            coins_live = live.coins_available
            owned_live = {it.slug for it in live.shop_items if it.owned}
            eq_live = set(live.equipped_items)
            is_owned = item_spec.slug in owned_live
            is_eq = item_spec.slug in eq_live

            b1 = y0 + 16.0
            b2 = y0 + 30.0
            if item_spec.slug == "rare_candy":
                fed = live.candies_fed
                if coins_live >= item_spec.price:
                    btn_txt = f"INJECT · ✦ {item_spec.price} CR" + (f" (×{fed})" if fed else "")
                    btn_role, btn_bg = "belkins_gold_text", "belkins_gold_pill"
                else:
                    btn_txt = f"LOCKED · ✦ {item_spec.price} CR" + (f" (×{fed})" if fed else "")
                    btn_role, btn_bg = "secondary", "belkins_badge_off"
            elif is_eq:
                btn_txt, btn_role, btn_bg = "EQUIPPED ✓", "good_text", "belkins_good_pill"
            elif is_owned:
                btn_txt, btn_role, btn_bg = "EQUIP", "belkins_flame_text", "belkins_flame_pill"
            elif coins_live >= item_spec.price:
                btn_txt, btn_role, btn_bg = f"BUY · ✦ {item_spec.price} CR", "belkins_gold_text", "belkins_gold_pill"
            else:
                btn_txt, btn_role, btn_bg = f"LOCKED · ✦ {item_spec.price} CR", "secondary", "belkins_badge_off"

            bw = _pill_w(pen, btn_txt)
            pen.pill(btn_txt, X1 - bw, b1 - 0.5, F["tag"][0], role=btn_role, bg=btn_bg, weight="semibold")
            text(
                pen,
                f"{item_spec.icon}  {item_spec.name}",
                "primary_med",
                "label",
                X0,
                b1,
                maxw=X1 - X0 - bw - 8.0,
            )
            text(pen, item_spec.description, "caption", "secondary", X0 + 20.0, b2, maxw=W_INNER - 20.0)
            return b2 + 8.0

        def on_click_item(slug: str = spec.slug) -> None:
            global _LAST_SHOP_NOTE
            live = companion_from_snapshot(snapshot)
            _ok, msg = toggle_or_buy_shop_item(slug, live.coins_earned)
            _LAST_SHOP_NOTE = msg

        card.add(
            draw_item,
            key=f"belkins-shop-item-{spec.slug}",
            ax=f"{spec.name} ({spec.price} Coins): {spec.description}",
            tooltip=f"{spec.icon} {spec.name} ({spec.price} SparkCoins)\n{spec.description}\nClick to buy or toggle equip",
            action=on_click_item,
            keep_open=True,
            **ROW_HL,
        )

    card.add(_gap(8.0), key=f"belkins-shop-foot-{subtab}")
    return card


def _config_card(snapshot: Any, actions: ThemeActions, subtab: str = "header") -> _Card:
    """`🎛️ Studio` Tab: Unified, Single-Screen Control Center for Menu-Bar Header, Icon, Thresholds & Engine."""
    del subtab
    card = _Card("belkins_card", "belkins_card_edge", rule="belkins_flame_rule")

    def _cur_view() -> str:
        v = str(snapshot.settings.get("title_view", "standard") or "standard")
        if snapshot.settings.get("title_compact", False) and v == "standard":
            return "compact"
        return v

    def _cur_icon() -> str:
        if not snapshot.settings.get("title_show_icon", True):
            return "none"
        return str(snapshot.settings.get("title_icon_style", "bolt") or "bolt")

    def _set_view(v: str, label: str) -> None:
        global _LAST_CFG_NOTE
        actions.set_setting("title_view", v)
        _LAST_CFG_NOTE = f"Layout → {label}"

    def _set_icon(st: str, label: str) -> None:
        global _LAST_CFG_NOTE
        actions.set_setting("title_icon_style", st)
        _LAST_CFG_NOTE = f"Icon → {label}"

    def _toggle_key(k: str, label: str, default: bool = True) -> None:
        global _LAST_CFG_NOTE
        cur = bool(snapshot.settings.get(k, default))
        actions.set_setting(k, not cur)
        _LAST_CFG_NOTE = f"{label}: {'ON ✓' if not cur else 'OFF'}"

    def _set_val(k: str, val: Any, label: str) -> None:
        global _LAST_CFG_NOTE
        actions.set_setting(k, val)
        _LAST_CFG_NOTE = label

    _ICON_SF_MAP: dict[str, str] = {
        "bolt": "bolt.fill",
        "companion": "sparkles",
        "crown": "crown.fill",
        "flame": "flame.fill",
        "cpu": "display",
        "bars": "chart.bar.fill",
        "shield": "bolt.shield.fill",
    }

    # 1. Header + Live Menu-Bar Preview Strip
    def draw_cfg_head(pen: Any, y0: float) -> float:
        base = y0 + HEAD_BASE
        symbol(pen, "slider.horizontal.3", X0, base - 4.0, 11.5, "belkins_flame_rule", "semibold")
        text(pen, "Settings & Control Studio", "title", "label", X0 + 18.0, base)
        note_tag = _LAST_CFG_NOTE if _LAST_CFG_NOTE else f"PRESET: {_cur_view().upper()}"
        pw = _pill_w(pen, note_tag)
        pen.pill(
            note_tag,
            X1 - pw,
            base - 0.5,
            F["tag"][0],
            role="good_text" if _LAST_CFG_NOTE else "belkins_flame_text",
            bg="belkins_good_pill" if _LAST_CFG_NOTE else "belkins_flame_pill",
            weight="semibold",
        )

        # Recessed Live Menu-Bar Preview Strip
        strip_y = base + 8.0
        strip_h = 26.0
        pen.rrect(
            X0,
            strip_y,
            W_INNER,
            strip_h,
            6.5,
            "belkins_inner_panel",
            stroke="belkins_inner_edge",
            stroke_width=0.75,
        )
        sb = strip_y + 17.0
        text(pen, "LIVE MENU BAR", "tag", "secondary", X0 + 9.0, sb)

        live_fn = getattr(actions, "render_live_title", None)
        live_str = str(live_fn() or "").strip() if callable(live_fn) else ""
        if not live_str:
            act_row = getattr(snapshot, "active", None)
            alias_s = str(getattr(act_row, "alias", "main") or "main")[:8]
            pct_s = f"{int(round(getattr(act_row, 'five_hour_pct', 0) or 0))}%" if act_row else "0%"
            live_str = f"{alias_s} {pct_s}"
        # Strip leading fallback glyph if icon is rendered as SF Symbol
        if live_str.startswith("⚡"):
            live_str = live_str.lstrip("⚡").strip()

        ic_style = _cur_icon()
        ic_sf = _ICON_SF_MAP.get(ic_style)
        tw_live = text_w(pen, live_str, "caption_med")
        ic_w = 13.5 if ic_sf else 0.0
        pill_pad = 16.0
        mock_w = min(W_INNER - 115.0, max(54.0, tw_live + ic_w + pill_pad))
        mock_x = X1 - mock_w - 6.0
        pen.rrect(mock_x, strip_y + 3.0, mock_w, strip_h - 6.0, 5.0, "belkins_badge_off", stroke="belkins_badge_on_edge")
        tx = mock_x + 8.0
        if ic_sf:
            symbol(pen, ic_sf, tx, strip_y + 13.0, 9.5, "belkins_flame_text", "semibold")
            tx += 13.5
        text(pen, live_str or "Icon Only", "caption_med", "label", tx, sb, maxw=mock_x + mock_w - tx - 6.0)
        return strip_y + strip_h + 4.0

    card.add(
        draw_cfg_head,
        key="belkins-cfg-head",
        ax="Settings and Control Studio with Live Menu-Bar Preview",
        tooltip="Every change below updates your macOS menu-bar status item immediately.",
    )

    # Helper: Horizontal Segmented Control Bar
    def _add_segmented_bar(
        row_key: str,
        sec_label: str,
        items: list[tuple[str, str | None, Callable[[], bool], Callable[[], None], str]],
    ) -> None:
        n = len(items)
        gap_s = 4.0
        seg_w = (W_INNER - gap_s * (n - 1)) / float(n)

        def draw_seg(pen: Any, y0: float, seg_items: list = items) -> float:
            lbl_b = y0 + 12.5
            text(pen, sec_label, "tag", "belkins_flame_text", X0, lbl_b)
            bar_y = lbl_b + 4.5
            bar_h = 26.0
            b_txt = bar_y + 17.0
            for idx, (lbl, sf_ic, is_on_fn, _act_fn, _tip) in enumerate(seg_items):
                sx = X0 + idx * (seg_w + gap_s)
                on = bool(is_on_fn())
                bg = "belkins_tab_active_fill" if on else "belkins_badge_off"
                edge = "belkins_flame_rule" if on else "belkins_badge_off_edge"
                pen.rrect(sx, bar_y, seg_w, bar_h, 5.5, bg, stroke=edge)
                txt_r = "belkins_tab_active_text" if on else "secondary"
                if sf_ic:
                    tw = text_w(pen, lbl, "caption_med")
                    ic_s = 8.0
                    tot = ic_s + 2.5 + tw
                    st_x = sx + max(2.5, (seg_w - tot) / 2.0)
                    symbol(pen, sf_ic, st_x, bar_y + 13.0, ic_s, txt_r, "semibold")
                    text(pen, lbl, "caption_med", txt_r, st_x + ic_s + 2.5, b_txt, maxw=seg_w - ic_s - 3.5)
                else:
                    text(pen, lbl, "caption_med", txt_r, sx + seg_w / 2.0, b_txt, center=True, maxw=seg_w - 4.0)
            return bar_y + bar_h + 3.0

        def on_seg_click(click_x: float, seg_items: list = items) -> None:
            rel = max(0.0, min(W_INNER - 1.0, click_x - X0))
            idx = min(len(seg_items) - 1, max(0, int(rel // (seg_w + gap_s))))
            seg_items[idx][3]()

        card.add(
            draw_seg,
            key=row_key,
            ax=f"{sec_label}: " + ", ".join(i[0] for i in items),
            tooltip="\n".join(i[4] for i in items if i[4]),
            action=lambda seg_items=items: seg_items[0][3](),
            on_click_x=on_seg_click,
            keep_open=True,
            no_hover_bg=True,
        )

    # 2. Menu-Bar Icon (6-Segment Horizontal Picker)
    _add_segmented_bar(
        "belkins-cfg-icon-bar",
        "1. MENU-BAR ICON STYLE",
        [
            ("Mac", "display", lambda: _cur_icon() == "cpu", lambda: _set_icon("cpu", "Mac Display"), "CleanMyMac-style Mac Display icon"),
            ("Bolt", "bolt.fill", lambda: _cur_icon() == "bolt", lambda: _set_icon("bolt", "Belkins Bolt"), "Belkins Lightning Bolt icon"),
            ("Sprite", "sparkles", lambda: _cur_icon() == "companion", lambda: _set_icon("companion", "Companion Sprite"), "Live 12x12 color pixel sprite of your active Companion"),
            ("Crown", "crown.fill", lambda: _cur_icon() == "crown", lambda: _set_icon("crown", "Crown"), "Sovereign Crown icon"),
            ("Bars", "chart.bar.fill", lambda: _cur_icon() == "bars", lambda: _set_icon("bars", "Bars"), "Classic Usage Bars icon"),
            ("None", "nosign", lambda: _cur_icon() == "none", lambda: _set_icon("none", "No Icon"), "Text only (hide menu-bar icon)"),
        ],
    )

    # 3. Menu-Bar View Preset (4-Segment Horizontal Picker)
    _add_segmented_bar(
        "belkins-cfg-view-bar",
        "2. MENU-BAR LAYOUT PRESET",
        [
            ("Custom", "slider.horizontal.3", lambda: _cur_view() == "standard", lambda: _set_view("standard", "Custom Chips"), "Show the exact stats toggled in Section 3 below"),
            ("Full Info", "chart.bar.fill", lambda: _cur_view() == "full", lambda: _set_view("full", "Full Info"), "Show Account + 5h% + 7d% + Codex% + Today Cost"),
            ("Companion", "sparkles", lambda: _cur_view() == "companion", lambda: _set_view("companion", "Companion HUD"), "Show Companion Level/Form + 5h% + SparkCoins"),
            ("Compact", "rectangle.compress.vertical", lambda: _cur_view() == "compact", lambda: _set_view("compact", "Compact V·C"), "Ultra-narrow V·C 100/100 view for crowded menu bars"),
        ],
    )

    # 4. Visible Menu-Bar Stats (Compact 4-Column × 2-Row Chip Grid)
    card.add(
        lambda pen, y0: text(pen, "3. VISIBLE MENU-BAR DATA CHIPS (CLICK TO TOGGLE)", "tag", "belkins_flame_text", X0, y0 + 12.5) and (y0 + 15.5),
        key="belkins-cfg-chips-hdr",
    )
    chip_rows = [
        [
            ("Alias", "person.fill", lambda: bool(snapshot.settings.get("title_show_alias", True)), lambda: _toggle_key("title_show_alias", "Account Alias", True), "Show active Claude account alias"),
            ("5h %", "bolt.fill", lambda: bool(snapshot.settings.get("title_show_five_hour_pct", True)), lambda: _toggle_key("title_show_five_hour_pct", "5h Quota %", True), "Show active 5-hour quota %"),
            ("7d %", "calendar", lambda: bool(snapshot.settings.get("title_show_seven_day_pct", False)), lambda: _toggle_key("title_show_seven_day_pct", "7d Weekly %", False), "Show 7-day weekly quota %"),
            ("Codex", "terminal.fill", lambda: bool(snapshot.settings.get("title_show_codex_pct", False)), lambda: _toggle_key("title_show_codex_pct", "Codex %", False), "Show active Codex weekly %"),
        ],
        [
            ("$ Cost", "dollarsign.circle.fill", lambda: bool(snapshot.settings.get("title_show_cost", True)), lambda: _toggle_key("title_show_cost", "Today Cost", True), "Show today's notional USD spend ($/d)"),
            ("Tokens", "number.square.fill", lambda: bool(snapshot.settings.get("title_show_tokens", False)), lambda: _toggle_key("title_show_tokens", "Today Tokens", False), "Show today's token burn (e.g. 42M)"),
            ("Lv/Form", "sparkles", lambda: bool(snapshot.settings.get("title_show_companion", False)), lambda: _toggle_key("title_show_companion", "Companion Lv", False), "Show Companion Level & Stage in header"),
            ("Boss HP", "flame.fill", lambda: bool(snapshot.settings.get("title_show_boss", False)), lambda: _toggle_key("title_show_boss", "Raid Boss HP", False), "Show active Raid Boss HP% in header"),
        ],
    ]
    chip_gap = 5.0
    chip_w = (W_INNER - chip_gap * 3.0) / 4.0
    for r_idx, c_row in enumerate(chip_rows):
        def draw_chip_row(pen: Any, y0: float, row_items: list = c_row) -> float:
            by = y0 + 2.0
            bh = 26.0
            bt = by + 17.0
            for c_i, (clbl, c_sf, on_fn, _act, _tip) in enumerate(row_items):
                cx = X0 + c_i * (chip_w + chip_gap)
                on = bool(on_fn())
                bg = "belkins_badge_on" if on else "belkins_badge_off"
                edge = "belkins_badge_on_edge" if on else "belkins_badge_off_edge"
                pen.rrect(cx, by, chip_w, bh, 5.5, bg, stroke=edge)
                ic_r = "good_text" if on else "secondary"
                txt_r = "label" if on else "secondary"
                symbol(pen, c_sf, cx + 6.5, by + 13.0, 8.5, ic_r, "semibold")
                dot_s = "✓" if on else ""
                if dot_s:
                    text(pen, dot_s, "tag", "good_text", cx + chip_w - 6.0, bt, right=True)
                text(pen, clbl, "caption_med", txt_r, cx + 17.5, bt, maxw=chip_w - (29.0 if on else 20.0))
            return by + bh + 3.0

        def on_chip_row_click(click_x: float, row_items: list = c_row) -> None:
            rel = max(0.0, min(W_INNER - 1.0, click_x - X0))
            idx = min(3, max(0, int(rel // (chip_w + chip_gap))))
            row_items[idx][3]()

        card.add(
            draw_chip_row,
            key=f"belkins-cfg-chip-row-{r_idx}",
            ax="Menu-bar data chips: " + ", ".join(c[0] for c in c_row),
            tooltip="\n".join(c[4] for c in c_row),
            action=lambda row_items=c_row: row_items[0][3](),
            on_click_x=on_chip_row_click,
            keep_open=True,
            no_hover_bg=True,
        )

    # 5. Auto-Switch & Quota Alert Threshold (5-Segment Bar)
    def _cur_thresh() -> int:
        return int(snapshot.settings.get("notification_threshold_pct", 85) or 85)

    def _toggle_autosw() -> None:
        global _LAST_CFG_NOTE
        actions.toggle_autoswitch()
        _LAST_CFG_NOTE = f"Auto-Switch → {'OFF' if snapshot.autoswitch_enabled else 'ON ✓'}"

    _add_segmented_bar(
        "belkins-cfg-thresh-bar",
        "4. AUTO-SWITCH & QUOTA ALERT THRESHOLD",
        [
            (
                "Auto ON" if snapshot.autoswitch_enabled else "Auto OFF",
                "bolt.fill",
                lambda: bool(snapshot.autoswitch_enabled),
                _toggle_autosw,
                "Toggle automatic Claude account rotation before hitting the wall",
            ),
            ("80%", None, lambda: _cur_thresh() == 80, lambda: _set_val("notification_threshold_pct", 80, "Alert Threshold → 80%"), "Warn at 80% quota utilization"),
            ("85%", None, lambda: _cur_thresh() == 85, lambda: _set_val("notification_threshold_pct", 85, "Alert Threshold → 85%"), "Warn at 85% quota utilization (default)"),
            ("90%", None, lambda: _cur_thresh() == 90, lambda: _set_val("notification_threshold_pct", 90, "Alert Threshold → 90%"), "Warn at 90% quota utilization"),
            ("95%", None, lambda: _cur_thresh() == 95, lambda: _set_val("notification_threshold_pct", 95, "Alert Threshold → 95%"), "Warn at 95% quota utilization"),
        ],
    )

    # 6. Desktop Alerts & Codex Poll Speed (5-Segment Bar)
    def _cur_poll() -> int:
        return int(snapshot.settings.get("codex_quota_interval_seconds", 300) or 300)

    _add_segmented_bar(
        "belkins-cfg-poll-bar",
        "5. DESKTOP NOTIFICATIONS & CODEX POLL INTERVAL",
        [
            (
                "Bell ON" if snapshot.settings.get("notifications_enabled", True) else "Bell OFF",
                "bell.fill",
                lambda: bool(snapshot.settings.get("notifications_enabled", True)),
                lambda: _toggle_key("notifications_enabled", "Desktop Alerts", True),
                "Toggle macOS Notification Center quota alerts",
            ),
            ("1m", None, lambda: _cur_poll() == 60, lambda: _set_val("codex_quota_interval_seconds", 60, "Codex Poll → 1 min"), "Poll live Codex quotas every 1 minute"),
            ("5m", None, lambda: _cur_poll() == 300, lambda: _set_val("codex_quota_interval_seconds", 300, "Codex Poll → 5 min"), "Poll live Codex quotas every 5 minutes (recommended)"),
            ("10m", None, lambda: _cur_poll() == 600, lambda: _set_val("codex_quota_interval_seconds", 600, "Codex Poll → 10 min"), "Poll live Codex quotas every 10 minutes"),
            ("15m", None, lambda: _cur_poll() == 900, lambda: _set_val("codex_quota_interval_seconds", 900, "Codex Poll → 15 min"), "Poll live Codex quotas every 15 minutes"),
        ],
    )

    # 7. Core Engine Switches & Quick Actions (2 Rows of 3 Pills)
    def _trig_update() -> None:
        global _LAST_CFG_NOTE
        fn = getattr(actions, "update_app", None)
        if callable(fn):
            fn()
        _LAST_CFG_NOTE = "Running bswap update in background…"

    def _trig_dash() -> None:
        global _LAST_CFG_NOTE
        fn = getattr(actions, "open_dashboard", None)
        if callable(fn):
            fn()
        _LAST_CFG_NOTE = "Opened HTML Dashboard in browser"

    def _trig_csv() -> None:
        global _LAST_CFG_NOTE
        fn = getattr(actions, "export_csv", None)
        if callable(fn):
            fn()
        _LAST_CFG_NOTE = "Exported Usage CSV to ~/Downloads"

    _add_segmented_bar(
        "belkins-cfg-engine-bar",
        "6. ENGINE MODULES & QUICK ACTIONS",
        [
            (
                "Codex Live",
                "antenna.radiowaves.left.and.right",
                lambda: bool(snapshot.settings.get("codex_live_quota_enabled", False)),
                lambda: _toggle_key("codex_live_quota_enabled", "Codex Live Quota", False),
                "Enable live per-account Codex OAuth quota polling",
            ),
            (
                "Cost Track",
                "dollarsign.circle.fill",
                lambda: bool(snapshot.settings.get("cost_tracking_enabled", True)),
                lambda: _toggle_key("cost_tracking_enabled", "Cost Tracking", True),
                "Track local transcript token & USD spend",
            ),
            (
                "Idle Anim",
                "sparkles",
                lambda: bool(snapshot.settings.get("belkins_animations_enabled", True)),
                lambda: _toggle_key("belkins_animations_enabled", "Idle Animations", True),
                "Toggle retro pixel breathing/bobbing animations while menu is open",
            ),
        ],
    )

    _add_segmented_bar(
        "belkins-cfg-actions-bar",
        "QUICK TOOLS",
        [
            ("Update App", "arrow.triangle.2.circlepath", lambda: False, _trig_update, "Pull latest Belkins Usage Bar from GitHub & restart (`bswap update`)"),
            ("Web Dashboard", "safari.fill", lambda: False, _trig_dash, "Open the local Belkins HTML Usage & Companion Dashboard"),
            ("Export CSV", "square.and.arrow.down.fill", lambda: False, _trig_csv, "Export daily token & USD history to ~/Downloads"),
        ],
    )

    card.add(_gap(6.0), key="belkins-cfg-foot")
    return card


def _tag_blocks(
    blocks: list[Block],
    tab_id: str,
    extra_pred: Callable[[], bool] | None = None,
) -> list[Block]:
    for b in blocks:
        b.tab = tab_id
        if extra_pred is None:
            b.is_visible = lambda t=tab_id: _ACTIVE_TAB == t
        else:
            b.is_visible = lambda t=tab_id, p=extra_pred: (_ACTIVE_TAB == t) and p()
    return blocks


def _footer_dock_block(snapshot: Any, actions: ThemeActions) -> Block:
    """CleanMyMac-style bottom bar (`[🖥]   Open Belkins Dashboard   [↻] [⚙️] [⏻]`)."""
    del snapshot
    btn_sz = 26.0

    def paint(pen: Pen, w: float, h: float) -> None:
        _draw_cmm_backdrop(pen, w, h)
        pen.line(0.0, 1.0, w, "belkins_hair", thickness=0.5)
        y = 7.0
        fill, edge, _ = _card_surface_roles("belkins_glass_btn", "belkins_glass_btn_edge", None)

        # Left square icon button (`display`)
        lx = CARD_X
        draw_glass_tile(pen, lx, y, btn_sz, btn_sz, 6.5, fill, edge)
        symbol(pen, "display", lx + 6.5, y + 13.0, 11.0, "belkins_cmm_sub", "medium")

        # Center action text (`Open Belkins Dashboard`)
        text(pen, "Open Belkins Dashboard", "primary_med", "label", w / 2.0 - 12.0, y + 17.5, center=True)

        # Right square icon buttons (`Refresh`, `Settings`, `Quit`)
        right_icons = [
            ("arrow.clockwise", "belkins_cmm_sub"),
            ("gearshape.fill", "belkins_cmm_sub"),
            ("power", "belkins_cmm_coral"),
        ]
        for idx, (ic_nm, ic_rl) in enumerate(right_icons):
            rx = w - CARD_X - (len(right_icons) - idx) * btn_sz - (len(right_icons) - 1 - idx) * 5.0
            draw_glass_tile(pen, rx, y, btn_sz, btn_sz, 6.5, fill, edge)
            symbol(pen, ic_nm, rx + 7.2, y + 13.0, 10.5, ic_rl, "semibold")

    def on_click_x(click_x: float) -> None:
        r_start = W - CARD_X - 3 * btn_sz - 10.0
        if click_x >= r_start:
            rel = click_x - r_start
            slot = int(rel // (btn_sz + 5.0))
            if slot <= 0:
                if callable(actions.refresh):
                    actions.refresh()
            elif slot == 1:
                set_active_tab("config")
            else:
                if callable(actions.quit_app):
                    actions.quit_app()
            return
        if callable(actions.open_dashboard):
            actions.open_dashboard()

    return Block(
        40.0,
        paint,
        key="belkins-footer-dock",
        ax="Open Belkins Dashboard, Refresh, Settings, Quit",
        action=actions.open_dashboard,
        on_click_x=on_click_x,
        keep_open=False,
        no_hover_bg=True,
    )


# ------------------------------------------------------------------ build ---


def build(snapshot: Any, now: float, actions: ThemeActions | None = None) -> list[Block]:
    """Blocks for the top of the Belkins dropdown; the app appends the native tail."""
    from .. import app as A
    from .. import render as R

    _ensure_roles()
    acts = actions or ThemeActions()
    companion = companion_from_snapshot(snapshot)

    # 1. Always-visible Companion Hero + Segmented 6-Tab Bar
    hero_card = _companion_card(snapshot, companion, acts)
    blocks: list[Block] = hero_card.blocks(TOP_GAP, 2.0)

    claude_sp = _claude_spec(A, R, snapshot, now, acts)
    codex_sp = _codex_spec(A, R, snapshot, now, acts)
    claude_cnt = len(getattr(snapshot, "accounts", ()) or ())
    codex_cnt = len([r for r in A._visible_quota_rows_of(snapshot) if r.vendor != A.VENDOR_CLAUDE and r.slot < 0])
    blocks.append(_tab_bar_block(claude_cnt, codex_cnt))

    # 2. Tab "party" ("Hub"): 2×2 Liquid Glass Bento Grid (Claude + Codex tiles, Burn + 30d tiles, Alert capsule)
    attn = _attention_spec(A, snapshot, acts)
    blocks += _tag_blocks(
        _hub_bento_blocks(claude_sp, codex_sp, snapshot, attn, acts),
        "party",
    )

    # 3. Tab "claude": Full Claude Fleet + Compact Claude Attention Summary
    if claude_sp:
        claude_attn = [it for it in (attn["items"] if attn else ()) if it["provider"] == "Claude"]
        blocks += _tag_blocks(_provider_card(claude_sp, claude_attn).blocks(2.0, TAIL_GAP), "claude")

    # 4. Tab "codex": Full Codex Fleet + Compact Codex Attention Summary
    if codex_sp:
        codex_attn = [it for it in (attn["items"] if attn else ()) if it["provider"] == "Codex"]
        blocks += _tag_blocks(_provider_card(codex_sp, codex_attn).blocks(2.0, TAIL_GAP), "codex")

    # 5. Tab "dex": Interactive Boss Raid Arena + Daily Crate + 8-Stage Pokedex / 14-Trophy Bento Grid
    blocks += _tag_blocks(
        _dex_card(snapshot, companion, "pokedex").blocks(2.0, TAIL_GAP),
        "dex",
        extra_pred=lambda: _DEX_SUBTAB == "pokedex",
    )
    blocks += _tag_blocks(
        _dex_card(snapshot, companion, "trophies").blocks(2.0, TAIL_GAP),
        "dex",
        extra_pred=lambda: _DEX_SUBTAB == "trophies",
    )

    # 6. Tab "shop": 12-Item Belkins Spark Vault ("gear" sub-tab & "style" sub-tab)
    blocks += _tag_blocks(
        _shop_card(snapshot, companion, "gear").blocks(2.0, TAIL_GAP),
        "shop",
        extra_pred=lambda: _SHOP_SUBTAB == "gear",
    )
    blocks += _tag_blocks(
        _shop_card(snapshot, companion, "style").blocks(2.0, TAIL_GAP),
        "shop",
        extra_pred=lambda: _SHOP_SUBTAB == "style",
    )

    # 7. Tab "config": Unified Single-Screen Settings & Control Studio
    blocks += _tag_blocks(
        _config_card(snapshot, acts).blocks(2.0, 2.0),
        "config",
    )

    # 8. Always-visible Liquid Glass Utility Dock (replaces clunky native NSMenuItem tail)
    blocks.append(_footer_dock_block(snapshot, acts))
    return blocks
