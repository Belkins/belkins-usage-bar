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
    ("config", "Settings"),
)
_TAB_ICONS: dict[str, str] = {
    "party": "square.grid.2x2.fill",
    "claude": "sparkles",
    "codex": "terminal.fill",
    "dex": "trophy.fill",
    "shop": "shield.lefthalf.filled",
    "config": "gearshape.fill",
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
_LAST_SHOP_NOTE: str = ""
_LAST_BOSS_NOTE: str = ""
_LAST_CFG_NOTE: str = ""
_LIVE_SETTINGS: dict[str, Any] = {}
_LIVE_AUTOSWITCH: bool = True
_HUB_VIEWS: tuple[str, ...] = ("luxury", "detailed", "bento", "cleanmymac")


def _cur_hub_view(snapshot: Any = None) -> str:
    v = _LIVE_SETTINGS.get("belkins_hub_view")
    if not v and snapshot is not None:
        v = snapshot.settings.get("belkins_hub_view", "luxury")
    v_str = str(v or "luxury")
    return v_str if v_str in _HUB_VIEWS else "luxury"


def _toggle_autoswitch_ui(actions: ThemeActions) -> None:
    global _LIVE_AUTOSWITCH, _LAST_CFG_NOTE
    _LIVE_AUTOSWITCH = not _LIVE_AUTOSWITCH
    _LAST_CFG_NOTE = f"Auto-Switch: {'ON ✓' if _LIVE_AUTOSWITCH else 'OFF'}"
    if callable(actions.toggle_autoswitch):
        actions.toggle_autoswitch()


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
CARD_X = 12.0
PAD = 14.0
CARD_R = 14.0
GAP = 10.0
TOP_GAP = 4.0
TAIL_GAP = 4.0
X0 = CARD_X + PAD                          # 26
X1 = W - CARD_X - PAD                      # 334
W_INNER = X1 - X0                          # 308
HX = X0 + 10
COL_RESET_W = 62.0
COL_PCT_R = X1 - COL_RESET_W - 4           # 268
COL_PCT_W = 40.0
COL_BAR_W = 62.0
COL_BAR_X = COL_PCT_R - COL_PCT_W - 6 - COL_BAR_W
COL_LABEL_R = COL_BAR_X - 6
CAP_STEP = 15.0
HEAD_BASE = 24.0
HEAD_AFTER = 12.0
WIN_LABEL_W = 46.0
WIN_STEP = 21.0
ROW_BASE = 19.0
ROW_CAP = 15.0
ROW_END = 11.0
INK = 4.0
PART_END = 10.0
ACCENT_W = 3.5
ROW_HL = {"hl_inset": CARD_X + 4, "hl_vinset": 1.0, "hl_radius": 7.0}

# ------------------------------------------------------------------ fonts ---
F: dict[str, tuple[float, str, bool, bool, bool]] = {
    "title": (13.0, "bold", False, False, True),
    "hero_name": (13.5, "semibold", False, False, True),
    "hero_big": (20.0, "bold", True, False, True),
    "primary": (12.0, "regular", False, False, False),
    "primary_med": (12.5, "medium", False, False, False),
    "secondary": (11.0, "regular", False, False, False),
    "secondary_med": (11.0, "medium", False, False, False),
    "caption": (10.5, "regular", False, False, False),
    "caption_med": (10.5, "medium", False, False, False),
    "num": (11.5, "semibold", True, False, True),
    "num_bar": (11.0, "semibold", True, False, True),
    "num_small": (10.5, "regular", True, False, False),
    "slot": (10.5, "medium", True, False, False),
    "code": (10.0, "medium", False, True, False),
    "tag": (9.5, "semibold", False, False, False),
}

_WHITE = (255, 255, 255)
_ROLES: tuple[tuple[str, tuple, tuple, Any], ...] = (
    ("belkins_cmm_canvas", (245, 246, 250, 0.97), (15, 18, 27, 0.96), None),
    ("belkins_cmm_aqua", (12, 158, 126), (74, 242, 198), _WHITE),
    ("belkins_cmm_icon", (100, 110, 132), (168, 178, 206), _WHITE),
    ("belkins_cmm_sub", (96, 106, 128), (176, 186, 210), _WHITE),
    ("belkins_cmm_coral", (226, 68, 52), (255, 116, 96), _WHITE),
    ("belkins_card", (255, 255, 255, 0.94), (255, 255, 255, 0.06), None),
    ("belkins_card_edge", (0, 0, 0, 0.06), (255, 255, 255, 0.11), None),
    ("belkins_tile_claude", (255, 255, 255, 0.94), (255, 255, 255, 0.06), None),
    ("belkins_tile_claude_edge", (0, 0, 0, 0.06), (255, 255, 255, 0.11), None),
    ("belkins_tile_codex", (255, 255, 255, 0.94), (255, 255, 255, 0.06), None),
    ("belkins_tile_codex_edge", (0, 0, 0, 0.06), (255, 255, 255, 0.11), None),
    ("belkins_glass_btn", (255, 255, 255, 0.85), (255, 255, 255, 0.08), None),
    ("belkins_glass_btn_edge", (0, 0, 0, 0.08), (255, 255, 255, 0.14), None),
    ("belkins_inner_panel", (247, 249, 253, 0.92), (10, 13, 22, 0.44), None),
    ("belkins_inner_edge", (0, 0, 0, 0.05), (255, 255, 255, 0.08), None),
    ("belkins_plan_pill", (0, 0, 0, 0.06), (255, 255, 255, 0.10), (255, 255, 255, 0.18)),
    ("belkins_plan_text", (72, 78, 92), (212, 218, 232), _WHITE),
    ("belkins_arcade_card", (255, 251, 238, 0.80), (32, 26, 18, 0.68), None),
    ("belkins_arcade_edge", (217, 155, 12, 0.42), (250, 204, 21, 0.45), None),
    ("belkins_hero_card", (255, 255, 255, 0.78), (255, 255, 255, 0.065), None),
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
    """Paint the Belkins luxury dark obsidian-slate game HUD canvas across a block."""
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
            center = NSMakePoint(w - 39.0, 29.0)
            # Outer Cyber-Emerald ambient field behind the Game Companion Mascot
            e0 = NSColor.colorWithSRGBRed_green_blue_alpha_(0.22, 0.90, 0.72, 0.16)
            e1 = NSColor.colorWithSRGBRed_green_blue_alpha_(0.22, 0.90, 0.72, 0.0)
            grad_outer = NSGradient.alloc().initWithStartingColor_endingColor_(e0, e1)
            grad_outer.drawFromCenter_radius_toCenter_radius_options_(center, 4.0, center, 54.0, 0)
            # Inner warm Belkins Ember/Gold core halo
            g0 = NSColor.colorWithSRGBRed_green_blue_alpha_(1.00, 0.68, 0.24, 0.22)
            g1 = NSColor.colorWithSRGBRed_green_blue_alpha_(1.00, 0.42, 0.18, 0.0)
            grad_inner = NSGradient.alloc().initWithStartingColor_endingColor_(g0, g1)
            grad_inner.drawFromCenter_radius_toCenter_radius_options_(center, 1.5, center, 30.0, 0)
        finally:
            NSGraphicsContext.restoreGraphicsState()
    except Exception:
        pass


def _octagon_path(NSBezierPath: Any, NSMakePoint: Any, x: float, y: float, size: float, cut: float) -> Any:
    """Construct a sleek 8-sided chamfered Cyber-Medallion octagon path."""
    p = NSBezierPath.bezierPath()
    p.moveToPoint_(NSMakePoint(x + cut, y))
    p.lineToPoint_(NSMakePoint(x + size - cut, y))
    p.lineToPoint_(NSMakePoint(x + size, y + cut))
    p.lineToPoint_(NSMakePoint(x + size, y + size - cut))
    p.lineToPoint_(NSMakePoint(x + size - cut, y + size))
    p.lineToPoint_(NSMakePoint(x + cut, y + size))
    p.lineToPoint_(NSMakePoint(x, y + size - cut))
    p.lineToPoint_(NSMakePoint(x, y + cut))
    p.closePath()
    return p


def _draw_sparkle_star(NSBezierPath: Any, NSMakePoint: Any, cx: float, cy: float, r: float) -> None:
    """Draw a crisp 4-point RPG vector star sparkle centered at (cx, cy)."""
    inner = r * 0.30
    sp = NSBezierPath.bezierPath()
    sp.moveToPoint_(NSMakePoint(cx, cy - r))
    sp.lineToPoint_(NSMakePoint(cx + inner, cy - inner))
    sp.lineToPoint_(NSMakePoint(cx + r, cy))
    sp.lineToPoint_(NSMakePoint(cx + inner, cy + inner))
    sp.lineToPoint_(NSMakePoint(cx, cy + r))
    sp.lineToPoint_(NSMakePoint(cx - inner, cy + inner))
    sp.lineToPoint_(NSMakePoint(cx - r, cy))
    sp.lineToPoint_(NSMakePoint(cx - inner, cy - inner))
    sp.closePath()
    sp.fill()


def _draw_game_hero_emblem(
    pen: Any,
    stage: EvolutionStage,
    x: float,
    y: float,
    size: float = 46.0,
    equipped: set[str] | tuple[str, ...] = (),
    animate: bool = False,
    *,
    mood_key: str = "locked_in",
) -> None:
    """Draw a high-craft vector + pixel-HD Belkins Cyber-RPG Mascot Emblem on the top-right (`size = 46`)."""
    if isinstance(pen, _Measure):
        return
    try:
        from AppKit import NSBezierPath, NSColor, NSGradient, NSGraphicsContext
        from Foundation import NSMakePoint, NSMakeRect
    except Exception:
        return

    eq = set(equipped or ())
    frame = (int(time.monotonic() * 2.2)) % 4 if animate else 0

    # Accent palette driven by equipped pad/core, defaulting to glowing RPG Gold & Cyber Ember
    if "emerald_pad" in eq:
        rim_rgb = (52, 211, 153)
        glow_rgb = (16, 185, 129)
    elif "nebula_pad" in eq:
        rim_rgb = (192, 132, 252)
        glow_rgb = (168, 85, 247)
    elif "solar_pad" in eq or "shiny_core" in eq:
        rim_rgb = (255, 204, 51)
        glow_rgb = (255, 149, 0)
    else:
        rim_rgb = (255, 184, 77)   # #FFB84D RPG Gold
        glow_rgb = (255, 107, 53)  # #FF6B35 Cyber Ember

    NSGraphicsContext.saveGraphicsState()
    try:
        cx = x + size * 0.5
        cy = y + size * 0.5

        # 1. Soft radial Gold/Ember energy halo (NSGradient) behind the medallion
        halo_c0 = NSColor.colorWithSRGBRed_green_blue_alpha_(
            rim_rgb[0] / 255.0, rim_rgb[1] / 255.0, rim_rgb[2] / 255.0, 0.38
        )
        halo_c1 = NSColor.colorWithSRGBRed_green_blue_alpha_(
            glow_rgb[0] / 255.0, glow_rgb[1] / 255.0, glow_rgb[2] / 255.0, 0.0
        )
        halo_grad = NSGradient.alloc().initWithStartingColor_endingColor_(halo_c0, halo_c1)
        center_pt = NSMakePoint(cx, cy)
        halo_grad.drawFromCenter_radius_toCenter_radius_options_(
            center_pt, size * 0.15, center_pt, size * 0.86, 0
        )

        # 2. Sleek Chamfered Cyber-Medallion / Octagon Shield
        cut = round(size * 0.22, 1)
        outer_oct = _octagon_path(NSBezierPath, NSMakePoint, x, y, size, cut)

        # Recessed deep cyber-obsidian gradient fill
        bg_top = NSColor.colorWithSRGBRed_green_blue_alpha_(0.13, 0.16, 0.26, 0.96)
        bg_bot = NSColor.colorWithSRGBRed_green_blue_alpha_(0.05, 0.06, 0.11, 0.98)
        bg_grad = NSGradient.alloc().initWithStartingColor_endingColor_(bg_top, bg_bot)
        bg_grad.drawInBezierPath_angle_(outer_oct, 90.0)

        # Glowing Gold/Ember outer shield rim
        NSColor.colorWithSRGBRed_green_blue_alpha_(
            rim_rgb[0] / 255.0, rim_rgb[1] / 255.0, rim_rgb[2] / 255.0, 0.88
        ).setStroke()
        outer_oct.setLineWidth_(1.35)
        outer_oct.stroke()

        # Inner specular octagon bezel ring
        in_pad = 2.4
        inner_oct = _octagon_path(
            NSBezierPath,
            NSMakePoint,
            x + in_pad,
            y + in_pad,
            size - 2.0 * in_pad,
            max(2.0, cut - in_pad * 0.7),
        )
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.92, 0.76, 0.18).setStroke()
        inner_oct.setLineWidth_(0.65)
        inner_oct.stroke()

        # 4 cardinal cyber-medallion energy nodes on the rim
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.88, 0.55, 0.85).setFill()
        for nx, ny, nw, nh in (
            (cx - 3.5, y - 0.7, 7.0, 1.6),
            (cx - 3.5, y + size - 0.9, 7.0, 1.6),
            (x - 0.7, cy - 3.5, 1.6, 7.0),
            (x + size - 0.9, cy - 3.5, 1.6, 7.0),
        ):
            NSBezierPath.fillRect_(NSMakeRect(nx, ny, nw, nh))

        # 3. Illuminated Holo-Pedestal inside the medallion base
        ped_w = size * 0.64
        ped_h = 4.2
        ped_x = cx - ped_w * 0.5
        ped_y = y + size - 9.2
        NSColor.colorWithSRGBRed_green_blue_alpha_(
            glow_rgb[0] / 255.0, glow_rgb[1] / 255.0, glow_rgb[2] / 255.0, 0.30
        ).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(ped_x, ped_y, ped_w, ped_h)).fill()
        NSColor.colorWithSRGBRed_green_blue_alpha_(
            rim_rgb[0] / 255.0, rim_rgb[1] / 255.0, rim_rgb[2] / 255.0, 0.62
        ).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(ped_x + ped_w * 0.18, ped_y + 0.9, ped_w * 0.64, ped_h * 0.52)
        ).fill()

        # 4. Pixel-HD Evolved Companion + Crowned Cyber-Core inside the shield
        grid = stage.sprite
        rows = len(grid)
        cols = len(grid[0]) if rows else 14
        sp_pad = 5.2
        cell = (size - 2.0 * sp_pad) / float(max(rows, cols))
        ox = x + sp_pad
        oy = y + sp_pad + 0.8

        body_bob_y = (cell * 0.38) if (animate and frame in (1, 2)) else 0.0
        spark_bob_y = (-cell * 0.32) if (animate and frame % 2 == 1) else 0.0
        blink_frame = animate and (frame == 3)

        if "shiny_core" in eq:
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
            o_rgb = (40, 44, 66)
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

        for r_idx, row_str in enumerate(grid):
            row_bob = body_bob_y if r_idx < (rows - 2) else 0.0
            prev_row = grid[r_idx - 1] if r_idx > 0 else ""
            for c_idx, ch in enumerate(row_str):
                rgba = palette.get(ch)
                if rgba is None:
                    continue
                cr, cg, cb_val, alpha = rgba
                py_off = spark_bob_y if ch == "S" else row_bob
                if ch == "E" and blink_frame and "deal_shades" not in eq:
                    cr, cg, cb_val = b_r, b_g, b_b
                NSColor.colorWithSRGBRed_green_blue_alpha_(
                    cr / 255.0, cg / 255.0, cb_val / 255.0, alpha
                ).setFill()
                px = ox + c_idx * cell
                py = oy + r_idx * cell + py_off
                NSBezierPath.fillRect_(NSMakeRect(px, py, cell + 0.25, cell + 0.25))
                if ch in ("B", "C", "A") and (not prev_row or prev_row[c_idx] != ch):
                    NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.25).setFill()
                    NSBezierPath.fillRect_(NSMakeRect(px, py, cell + 0.2, max(0.65, cell * 0.28)))
                if ch == "E" and "deal_shades" in eq:
                    NSColor.colorWithSRGBRed_green_blue_alpha_(6 / 255.0, 182 / 255.0, 212 / 255.0, 1.0).setFill()
                    NSBezierPath.fillRect_(NSMakeRect(px - cell * 0.2, py, cell * 1.4, cell + 0.25))

        # Sovereign Crown headgear (equipped or subtle golden royal crest on high-tier stages)
        if "sovereign_crown" in eq or stage.stage >= 6:
            crown_px = (
                (5, 0, (254, 240, 138)),
                (7, 0, (255, 214, 70)),
                (9, 0, (254, 240, 138)),
                (5, 1, (245, 158, 11)),
                (6, 1, (250, 204, 21)),
                (7, 1, (56, 189, 248) if "shiny_core" not in eq else (244, 63, 94)),
                (8, 1, (250, 204, 21)),
                (9, 1, (245, 158, 11)),
            )
            for cc, cr_y, (rr, gg, bb) in crown_px:
                NSColor.colorWithSRGBRed_green_blue_alpha_(rr / 255.0, gg / 255.0, bb / 255.0, 1.0).setFill()
                NSBezierPath.fillRect_(
                    NSMakeRect(ox + cc * cell, oy + cr_y * cell + body_bob_y, cell + 0.22, cell + 0.22)
                )

        # Plasma Blade if equipped
        if "plasma_blade" in eq:
            for bc, br, (rr, gg, bb) in (
                (13, 3, (34, 211, 238)),
                (13, 4, (56, 189, 248)),
                (13, 5, (232, 121, 249)),
                (12, 6, (250, 204, 21)),
                (13, 6, (250, 204, 21)),
            ):
                NSColor.colorWithSRGBRed_green_blue_alpha_(rr / 255.0, gg / 255.0, bb / 255.0, 1.0).setFill()
                NSBezierPath.fillRect_(
                    NSMakeRect(ox + bc * cell, oy + br * cell + body_bob_y * 0.6, cell + 0.22, cell + 0.22)
                )

        # 5. Floating Sparkles (`✦`) on top-left and top-right of the medallion
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.84, 0.40, 0.92).setFill()
        _draw_sparkle_star(NSBezierPath, NSMakePoint, x + 3.0, y + 4.5, 3.4)
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.56, 0.28, 0.85).setFill()
        _draw_sparkle_star(NSBezierPath, NSMakePoint, x + size - 3.5, y + 6.5, 2.6)

        # 6. Floating Mini Cyber-Controller Badge on bottom-right corner
        bx, by, bw, bh = x + size - 16.5, y + size - 11.5, 17.5, 11.5
        badge_bp = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bx, by, bw, bh), 4.2, 4.2
        )
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.08, 0.10, 0.18, 0.96).setFill()
        badge_bp.fill()
        NSColor.colorWithSRGBRed_green_blue_alpha_(
            rim_rgb[0] / 255.0, rim_rgb[1] / 255.0, rim_rgb[2] / 255.0, 0.92
        ).setStroke()
        badge_bp.setLineWidth_(1.0)
        badge_bp.stroke()

        # Mini D-Pad cross on left side of controller badge
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.78, 0.35, 0.95).setFill()
        NSBezierPath.fillRect_(NSMakeRect(bx + 3.2, by + 5.0, 4.6, 1.5))
        NSBezierPath.fillRect_(NSMakeRect(bx + 4.75, by + 3.45, 1.5, 4.6))

        # Mini A/B arcade action buttons on right side of controller badge
        NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 0.42, 0.24, 0.98).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(bx + 10.2, by + 5.6, 2.3, 2.3)).fill()
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.29, 0.95, 0.78, 0.98).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(bx + 12.8, by + 3.5, 2.3, 2.3)).fill()
    finally:
        NSGraphicsContext.restoreGraphicsState()


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
                draw_glass_tile(pen, CARD_X, card_y, w - 2 * CARD_X, card_h, CARD_R, fill, edge, rule=rule)

            _clip_to(w, bh, surface)
            if pen.hl:
                pen.rrect(inset, lead + vinset, w - 2 * inset, h - 2 * vinset, radius, "hl")
            draw(pen, lead)

        return paint


# ------------------------------------------------ CleanMyMac Header ---


def _companion_card(snapshot: Any, companion: CompanionState, actions: ThemeActions) -> _Card:
    """Minimalist Luxury RPG Hero Header with spacious breathing room and Belkins Game Emblem."""
    register_role("belkins_rpg_gold", (214, 118, 12), (255, 184, 77), hover=_WHITE)
    open_dash = getattr(actions, "open_dashboard", None)
    dash_enabled = bool(snapshot.settings.get("dashboard_enabled", True))
    act = open_dash if (callable(open_dash) and dash_enabled) else None

    def paint_header(pen: Pen, w: float, bh: float) -> None:
        _draw_cmm_backdrop(pen, w, bh, top_glow=True)
        live = companion_from_snapshot(snapshot)
        coins_live = live.coins_available
        eq = set(live.equipped_items or ())
        animate = bool(snapshot.settings.get("belkins_animations_enabled", True))

        lx = 18.0
        max_left_w = w - 88.0

        # Line 1 (17.5pt Bold): `Belkins Prime` + `Lv.89` in RPG Gold
        b1 = 25.0
        lv_role = "belkins_flame_rule" if live.mood_key == "overdrive" else "belkins_rpg_gold"
        w_name = pen.text(f"{live.stage.name}  ", lx, b1, 17.5, "bold", "label", rounded=True)
        pen.text(f"Lv.{live.level}", lx + w_name, b1, 17.5, "bold", lv_role, rounded=True)

        # Line 2 (11.5pt Minimalist Subtitle): `Apex Sovereign  ·  ✦ 12.7K`
        b2 = 42.5
        sub_line = f"{live.stage.title}   ·   ✦ {_fmt_compact_coins(coins_live)}"
        pen.text(sub_line, lx, b2, 11.5, "regular", "belkins_cmm_sub", maxw=max_left_w)

        # Line 3 (y = 55pt): Uninterrupted, sculptural 4.0pt XP bar in clean open space
        bar_y = 55.5
        bar_w = 178.0
        xp_pct = max(4.0, min(100.0, float(live.xp_fraction) * 100.0))
        if "solar_pad" in eq or "shiny_core" in eq:
            xp_role = "belkins_xp_solar"
        elif "emerald_pad" in eq:
            xp_role = "belkins_xp_emerald"
        elif "nebula_pad" in eq:
            xp_role = "belkins_xp_nebula"
        else:
            xp_role = "belkins_xp_fill"

        luxury_bar(pen, lx, bar_y, bar_w, xp_pct, "ok", h=4.0, fill_override=xp_role)

        # Top-right glowing Belkins Cyber-RPG Mascot Emblem (x = w - 64, y = 11, size = 46)
        _draw_game_hero_emblem(
            pen,
            live.stage,
            w - 64.0,
            11.0,
            46.0,
            live.equipped_items,
            animate,
            mood_key=live.mood_key,
        )

    tip_lines = [
        f"Belkins Companion: {companion.stage.name} (Lv.{companion.level} · {companion.stage.title})",
        f"Passive: {companion.stage.passive} · ATK: {companion.boss.player_atk} · Crit: {companion.boss.player_crit_pct}%",
        f"Mood: {companion.mood_label} · XP: {companion.xp_total:,} (+{companion.xp_today:,} today)",
        f"Trophies: {companion.unlocked_badges_count}/{len(companion.badges)} · Streak: {max(1, companion.streak_days)}d · Bosses Slain: {companion.boss.bosses_slain}",
        "Click to open Belkins Dex & Usage Dashboard",
    ]
    tip = "\n".join(tip_lines)

    class _HeaderWrap:
        def blocks(self, _before: float, _after: float) -> list[Block]:
            return [
                Block(
                    68.0,
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


def _nice_plan_label(raw_plan: str | None, fallback: str = "") -> str:
    raw = str(raw_plan or "").strip()
    low = raw.lower().replace("_", "").replace("-", "").replace(" ", "")
    mapped = {
        "promax": "Pro Max",
        "max20x": "Max 20x",
        "max5x": "Max 5x",
        "pro": "Pro",
        "plus": "Plus",
        "max": "Max",
        "team": "Team",
        "business": "Business",
        "enterprise": "Enterprise",
        "free": "Free",
    }.get(low)
    return mapped or (fallback.replace("_", " ") if fallback else "") or raw.replace("_", " ")


def _claude_spec(A: Any, R: Any, snap: Any, now: float, actions: ThemeActions) -> dict[str, Any] | None:
    if not snap.accounts:
        return None
    models = tuple(snap.autoswitch_models or ())
    notes = snap.account_notes
    active = snap.active or next((r for r in snap.accounts if getattr(r, "is_active", False)), None)
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
        raw_plan = A._claude_plan_text(accounts, active) or A.plan_label(active.plan_type)
        hero = {
            "name": A._display_name(active),
            "slot": active.slot,
            "plan": _nice_plan_label(active.plan_type, raw_plan),
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
    toggle_fn = getattr(actions, "toggle_slot_disabled", None)
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
        if r.five_hour_pct is not None and r.seven_day_pct is not None:
            dual_win = f"5h {format_pct(r.five_hour_pct)}  ·  7d {format_pct(r.seven_day_pct)}"
        elif binding and binding[1] is not None:
            dual_win = f"{binding[0]} {format_pct(binding[1])}"
        else:
            dual_win = r.email or ""
        free_pct = (
            max(0, 100 - int(round(float(binding[1]))))
            if (binding and binding[1] is not None)
            else None
        )
        rows.append(
            {
                "key": f"belkins-claude-{r.slot}",
                "slot": f"#{r.slot}",
                "slot_num": r.slot,
                "name": A._display_name(r),
                "plan": _nice_plan_label(r.plan_type, A.plan_label(r.plan_type)),
                "email": r.email or "",
                "dual_win": dual_win,
                "free_pct": free_pct,
                "disabled": bool(r.disabled),
                "is_next": bool(r is nxt),
                "needs_relogin": False,
                "label": binding[0] if binding else "",
                "pct": binding[1] if binding else None,
                "sev": R.severity(binding[1]) if binding else "dim",
                "reset": A._reset_mark_text(A._window_reset(r, binding[0]), now) if binding else "",
                "withheld": None,
                "captions": caps,
                "tip": _claude_tip(A, r, now, accounts=accounts),
                "action": (lambda slot=r.slot: actions.switch_to(slot)),
                "toggle_disable_action": (
                    (lambda slot=r.slot: toggle_fn(slot)) if callable(toggle_fn) else None
                ),
            }
        )

    thr = getattr(snap, "autoswitch_threshold", 85.0)
    thr_int = int(round(float(thr if thr is not None else 85.0)))
    auto_on = bool(getattr(snap, "autoswitch_enabled", True))
    header_pills: list[tuple[Any, Any, Any, Callable[[], Any]]] = [
        (
            lambda t_default=thr_int: (
                f"⚡ Auto {int(_LIVE_SETTINGS.get('notification_threshold_pct', t_default) or t_default)}%"
                if _LIVE_AUTOSWITCH
                else "Auto OFF"
            ),
            lambda: "good_text" if _LIVE_AUTOSWITCH else "belkins_cmm_sub",
            lambda: "belkins_good_pill" if _LIVE_AUTOSWITCH else "belkins_plan_pill",
            lambda: _toggle_autoswitch_ui(actions),
        ),
    ]
    if nxt is not None and callable(actions.switch_best):
        header_pills.append(
            ("⚡ Best", "belkins_cmm_aqua", "belkins_tab_active_glass", actions.switch_best)
        )
    relogin_fn = getattr(actions, "claude_relogin", None)
    header_pills.append(
        (
            "+ Add",
            "belkins_flame_text",
            "belkins_flame_pill",
            relogin_fn if callable(relogin_fn) else (lambda: _copy_cmd_to_pasteboard("bswap add")),
        )
    )
    return {
        "title": "Claude Party",
        "icon": "sparkles",
        "rule": "belkins_claude_rule",
        "right": " · ".join(right),
        "right_parts": right,
        "room_badge": f"{room[0]}/{len(accounts)} ready" if room else f"{len(accounts)} slots",
        "header_pills": header_pills,
        "relogin_action": relogin_fn if callable(relogin_fn) else (lambda: _copy_cmd_to_pasteboard("bswap add")),
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
            "plan": _nice_plan_label(active.plan_type, A.plan_label(active.plan_type)),
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
        relogin_act = actions.codex_login_for(r)
        act = relogin_act
        row_alias = A._quota_alias(r)
        if act is None and row_alias:
            act = (lambda target=row_alias: actions.switch_codex(target))
        caps = _codex_caps(A, r, compact=True)
        if r is best:
            caps.insert(0, ("BEST", "tag"))
        if relogin_act is not None:
            caps.append(("Log in again…", "accent"))
        plan_nice = _nice_plan_label(r.plan_type, A.plan_label(r.plan_type))
        sub_parts = [p for p in (plan_nice, r.email) if p]
        free_pct = (
            max(0, 100 - int(round(float(win[1]))))
            if (win and win[1] is not None and not win[2])
            else None
        )
        rows.append(
            {
                "key": f"belkins-codex-{r.slot}",
                "slot": "",
                "slot_num": r.slot,
                "name": row_alias,
                "plan": plan_nice,
                "email": r.email or "",
                "dual_win": " · ".join(sub_parts),
                "free_pct": free_pct,
                "disabled": bool(r.disabled),
                "is_next": bool(r is best),
                "needs_relogin": bool(relogin_act is not None),
                "label": "",
                "pct": win[1] if win else None,
                "sev": "dim" if (not win or win[2]) else R.severity(win[1]),
                "reset": A._quota_reset_mark(r, now) if win else "",
                "withheld": None,
                "captions": caps,
                "tip": _codex_tip(A, r, now),
                "action": act,
                "toggle_disable_action": None,
            }
        )

    header_pills: list[tuple[str, str, str, Callable[[], Any]]] = []
    best_alias = A._quota_alias(best) if (best is not None and not best.is_active) else ""
    if best_alias and callable(actions.switch_codex):
        header_pills.append(
            (
                "⚡ Best",
                "belkins_cmm_aqua",
                "belkins_tab_active_glass",
                lambda target=best_alias: actions.switch_codex(target),
            )
        )
    new_login_fn = getattr(actions, "codex_new_login", None)
    if callable(new_login_fn):
        header_pills.append(
            ("+ Login", "good_text", "belkins_good_pill", new_login_fn)
        )
    return {
        "title": "Codex Party",
        "icon": "terminal.fill",
        "rule": "belkins_codex_rule",
        "right": " · ".join(right),
        "right_parts": right,
        "room_badge": f"{parts.room}/{parts.total} ready" if parts else "",
        "header_pills": header_pills,
        "hero": hero,
        "rows": rows,
        "total_count": len(live),
        "key": "codex",
    }


# ---------------------------------------------------------------- painters ---


def _short_reset_mark(mark: str, *, ultra_short: bool = False) -> str:
    """Trim redundant trailing ` HH:MM` from multi-day calendar reset marks (`↺ Nov 2 00:59` -> `↺ Nov 2`) so reset labels never truncate."""
    s = str(mark or "").strip()
    if not s:
        return ""
    # e.g. `↺ Nov 2 00:59` -> `↺ Nov 2`
    s = re.sub(
        r"^(↺\s+[A-Z][a-z]{2}\s+\d{1,2})\s+\d{1,2}:\d{2}$",
        r"\1",
        s,
    )
    if ultra_short:
        # e.g. `↺ Sat 12:59` -> `↺ Sat`
        s = re.sub(
            r"^(↺\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))\s+\d{1,2}:\d{2}$",
            r"\1",
            s,
        )
    return s


def _attn_item(it: dict[str, Any]) -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        b1 = y0 + 16.0
        cmd_chip = next(
            (seg[0] for seg in (it.get("action_segs") or ()) if len(seg) >= 4 and seg[3] == "code"),
            "bswap add" if it.get("provider") == "Claude" else "Re-login",
        )
        chip_w = text_w(pen, cmd_chip, "code") + 8.0
        pen.chip(
            cmd_chip,
            X1 - chip_w,
            b1,
            F["code"][0],
            role="belkins_flame_text",
            bg="belkins_flame_pill",
            weight=F["code"][1],
        )
        right_lim = X1 - chip_w - 8.0
        wname = text(pen, it["name"], "primary_med", "label", X0, b1, maxw=110.0)
        sx = X0 + wname + 6.0
        st_s = f"· {it['state']}" + (f" ({it['age']})" if it.get("age") else "")
        text(pen, st_s, "caption_med", it["state_role"], sx, b1, maxw=max(40.0, right_lim - sx))
        return b1 + 8.0

    return draw


def _gap(h: float) -> Callable[[Any, float], float]:
    return lambda pen, y0: y0 + h


def _hair() -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        hairline(pen, X0, X1, y0 + 2)
        return y0 + 3

    return draw


def _select_header_pills(spec: dict[str, Any], *, one_line: bool = False) -> list[tuple]:
    all_pills = list(spec.get("header_pills") or ())
    if not all_pills:
        return []
    if one_line:
        return all_pills[:1]
    if len(all_pills) >= 3:
        return [all_pills[0], all_pills[-1]]
    return all_pills[:2]


def _card_head(spec: dict[str, Any], *, one_line: bool = False, show_chevron: bool = False) -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        base = y0 + HEAD_BASE
        if spec.get("icon"):
            symbol(pen, spec["icon"], X0, base - 4.2, 11.5, spec.get("rule", "belkins_flame_text"), "semibold")
        tx = X0 + (18.0 if spec.get("icon") else 0.0)
        disp_title = spec["title"].replace(" Party", "") if one_line else spec["title"]
        tw = text(pen, disp_title, "title", "label", tx, base)
        right_edge = X1
        if show_chevron:
            symbol(pen, "chevron.right", X1 - 7.0, base - 3.8, 8.5, "belkins_cmm_sub", "semibold")
            right_edge = X1 - 12.0

        pills = _select_header_pills(spec, one_line=one_line)
        cur_r = right_edge
        for raw_lbl, raw_role, raw_bg, _fn in reversed(pills):
            lbl = str(raw_lbl() if callable(raw_lbl) else raw_lbl)
            role = str(raw_role() if callable(raw_role) else raw_role)
            bg = str(raw_bg() if callable(raw_bg) else raw_bg)
            pw = _pill_w(pen, lbl)
            if cur_r - pw < tx + tw + 10.0:
                break
            pen.pill(lbl, cur_r - pw, base - 0.5, F["tag"][0], role=role, bg=bg, weight="semibold")
            cur_r -= pw + 5.0

        room_badge = (spec.get("room_badge") or "").replace(" ready", "").strip()
        if room_badge and (tx + tw + 8.0 + text_w(pen, room_badge, "caption_med") <= cur_r - 6.0):
            text(pen, f"·  {room_badge}", "caption_med", "belkins_cmm_sub", tx + tw + 7.5, base - 0.5)
        return base + HEAD_AFTER

    return draw


def _card_head_click_x(spec: dict[str, Any], *, one_line: bool = False) -> Callable[[float], None] | None:
    pills = _select_header_pills(spec, one_line=one_line)
    if not pills:
        return None

    def on_click_x(click_x: float) -> None:
        m = _Measure()
        cur_r = X1
        for raw_lbl, _role, _bg, fn in reversed(pills):
            lbl = str(raw_lbl() if callable(raw_lbl) else raw_lbl)
            pw = _pill_w(m, lbl)
            x_start = cur_r - pw - 2.0
            x_end = cur_r + 2.0
            if x_start <= click_x <= x_end and callable(fn):
                fn()
                return
            cur_r -= pw + 5.0

    return on_click_x


def _hero(hero: dict[str, Any], *, compact: bool = False) -> Callable[[Any, float], float]:
    from .. import render as R

    def _draw_inner(pen: Any, y0: float) -> float:
        px0 = X0 + 6.0
        px1 = X1 - 6.0
        b1 = y0 + 19.0
        big = hero["big"]
        bw = 0.0
        sev_dot = "belkins_good_fill"
        master_sev = "ok"
        raw_val = hero.get("raw_pct")
        if hero["status"]:
            sev_dot = "belkins_warn_fill"
            master_sev = "warn"
        elif big:
            pct_str, win_lbl, sev = big
            master_sev = sev
            if sev == "crit":
                sev_dot = "belkins_crit_fill"
                badge_role = "crit_text"
            elif sev == "warn":
                sev_dot = "belkins_warn_fill"
                badge_role = "warn_text"
            elif sev == "dim":
                sev_dot = "dead_fill"
                badge_role = "secondary"
            else:
                sev_dot = "belkins_good_fill"
                badge_role = "good_text"
            if raw_val is not None:
                free_pct = max(0, 100 - int(round(float(raw_val))))
                badge_txt = f"{free_pct}% free"
            else:
                badge_txt = f"{pct_str} · {win_lbl}"
            bw = text_w(pen, badge_txt, "primary_med")
            text(pen, badge_txt, "primary_med", badge_role, px1, b1, right=True)

        # Live status dot + Account Name (Plan label shown only in expanded tab view)
        pen.circle(px0, b1 - 7.5, 6.4, sev_dot)
        nx = px0 + 11.5
        name_max = max(60.0, px1 - bw - 12.0 - nx)
        plan_raw = (hero.get("plan") or "").split("·")[0].strip() if not compact else ""
        nw = text(
            pen,
            hero["name"],
            "hero_name",
            "label",
            nx,
            b1,
            maxw=name_max * (0.68 if plan_raw else 1.0),
        )
        if plan_raw:
            plan_lbl = f"·  {plan_raw}"
            if nw + 7.5 + text_w(pen, plan_lbl, "caption_med") <= name_max:
                text(pen, plan_lbl, "caption_med", "belkins_cmm_sub", nx + nw + 7.0, b1 - 0.5)

        # Sculptural Minimalist Mode for `Hub`: 1 Master Stamina Bar + 1 Whisper-Clean Telemetry Line
        if compact and not hero["status"]:
            bar_y = b1 + 10.5
            bar_pct = float(raw_val) if raw_val is not None else 0.0
            luxury_bar(
                pen,
                px0,
                bar_y,
                px1 - px0,
                bar_pct,
                master_sev,
                h=5.5,
                expired=(master_sev == "dim"),
            )
            b_meta = bar_y + 18.0
            meta_parts: list[str] = []
            first_rst = ""
            for label, pct, mark, expired in hero["bars"][:2]:
                short_m = "ended" if expired else _short_reset_mark(mark, ultra_short=True)
                if short_m and not first_rst:
                    first_rst = short_m
                if label == "weekly":
                    meta_parts.append(f"Weekly {format_pct(pct)}")
                else:
                    meta_parts.append(f"{label} {format_pct(pct)}")
            if first_rst:
                meta_parts.append(first_rst)
            meta_line = "   ·   ".join(meta_parts)
            text(pen, meta_line, "caption", "belkins_cmm_sub", px0, b_meta, maxw=px1 - px0)
            return b_meta + 12.0

        ink = b1 + 6.0
        if hero["status"]:
            s, kind = hero["status"]
            b = ink + 16.0
            text(pen, s, "primary_med", SEV_TEXT.get(kind, kind), nx, b, maxw=px1 - nx)
            ink = b + INK

        bx = HX + WIN_LABEL_W
        b = ink + 17.5 - WIN_STEP
        for label, pct, mark, expired in hero["bars"]:
            b += WIN_STEP
            sev = R.severity(pct)
            text(pen, label, "secondary_med", "belkins_cmm_sub", px0 + 2.0, b, maxw=WIN_LABEL_W - 4.0)
            luxury_bar(
                pen,
                bx,
                b - 3.8,
                COL_PCT_R - COL_PCT_W - 6.0 - bx,
                pct,
                sev,
                h=5.4,
                expired=expired,
            )
            pct_role = "belkins_cmm_sub" if expired else (SEV_TEXT[sev] if sev != "ok" else "label")
            text(pen, format_pct(pct), "num_bar", pct_role, COL_PCT_R, b, right=True)
            short_mark = "↺ ended" if expired else _short_reset_mark(mark, ultra_short=True)
            text(
                pen,
                short_mark,
                "num_small",
                "belkins_cmm_sub",
                px1,
                b,
                right=True,
                maxw=max(32.0, px1 - COL_PCT_R - 6.0),
            )
            ink = b + INK

        caps_source = hero.get("captions_compact") if compact and "captions_compact" in hero else hero["captions"]
        banners = [
            c
            for c in caps_source
            if (c[1] == "good" and c[0].startswith("↺")) or (c[1] == "warn" and "pace" in c[0].lower())
        ][:1]
        for cap, kind in banners:
            y = ink + 7.0
            is_warn = kind == "warn"
            bg_r = "belkins_warn_pill" if is_warn else "belkins_good_pill"
            txt_r = "warn_text" if is_warn else "good_text"
            ic_name = "bolt.fill" if is_warn else "arrow.counterclockwise.circle.fill"
            pen.rrect(px0, y, px1 - px0, 21.0, 6.0, bg_r)
            clean_cap = cap.lstrip("↺ ").strip()
            if is_warn and clean_cap[:1].islower():
                clean_cap = clean_cap[:1].upper() + clean_cap[1:]
            symbol(pen, ic_name, px0 + 6.5, y + 10.5, 9.5, txt_r, "semibold")
            text(pen, clean_cap, "caption_med", txt_r, px0 + 19.5, y + 14.2, maxw=px1 - px0 - 25.0)
            ink = y + 21.0

        return ink + 11.5

    def draw(pen: Any, y0: float) -> float:
        end_y = _draw_inner(_Measure(), y0)
        if not isinstance(pen, _Measure):
            pen.rrect(
                X0 - 2.0,
                y0 + 1.5,
                W_INNER + 4.0,
                max(24.0, end_y - y0 - 5.5),
                10.0,
                "belkins_inner_panel",
                stroke="belkins_inner_edge",
                stroke_width=0.75,
            )
            _draw_inner(pen, y0)
        return end_y

    return draw


def _row_hub(r: dict[str, Any]) -> Callable[[Any, float], float]:
    """Minimalist 27pt Party Roster row for `Hub` — clean typography, zero extra progress bars, 1-click switch."""
    def draw(pen: Any, y0: float) -> float:
        b = y0 + 17.0
        free_pct = r.get("free_pct")
        dot_col = "belkins_good_fill" if (free_pct is None or free_pct >= 50) else ("belkins_warn_fill" if free_pct >= 20 else "belkins_crit_fill")
        pen.circle(X0 + 4.0, b - 6.8, 5.4, dot_col)
        x = X0 + 16.0

        # Right side: clean percentage readout (`100%` or `43%`) without duplicate progress bars
        if free_pct is not None:
            free_s = f"{free_pct}% free"
            free_role = "good_text" if free_pct >= 50 else ("warn_text" if free_pct >= 20 else "crit_text")
        elif r["pct"] is not None:
            free_s = format_pct(r["pct"])
            free_role = "label"
        else:
            free_s = "Ready"
            free_role = "belkins_cmm_sub"

        rw = text_w(pen, free_s, "caption_med")
        text(pen, free_s, "caption_med", free_role, X1 - 2.0, b, right=True)

        # Left side: Account Name + quiet `NEXT` / `BEST` indicator only
        avail_w = max(60.0, (X1 - rw - 14.0) - x)
        tag_cap = next((c[0] for c in r["captions"] if c[1] == "tag"), "")
        sub_w = (text_w(pen, tag_cap, "tag") + 10.0) if tag_cap in ("NEXT", "BEST") else 0.0
        nw = text(pen, r["name"], "primary_med", "label", x, b, maxw=max(50.0, avail_w - sub_w))
        if tag_cap in ("NEXT", "BEST") and (nw + sub_w <= avail_w + 4.0):
            text(pen, tag_cap, "tag", "belkins_cmm_aqua", x + nw + 7.0, b - 0.5)

        return b + 9.5

    return draw


def _row(r: dict[str, Any]) -> Callable[[Any, float], float]:
    """Spacious 1-line 30pt Standby Account row for `Claude` & `Codex` tabs — zero 2nd-line clutter."""
    def draw(pen: Any, y0: float) -> float:
        x = X0 + 20.0 if r["slot"] else X0 + 15.0
        b = y0 + 18.5

        right_edge = X1
        disabled = bool(r.get("disabled", False))
        needs_relogin = bool(r.get("needs_relogin", False))
        has_toggle = callable(r.get("toggle_disable_action"))
        free_pct = r.get("free_pct")

        if disabled:
            btn_lbl = "▶ Enable"
            btn_w = _pill_w(pen, btn_lbl)
            pen.pill(
                btn_lbl,
                right_edge - btn_w,
                b - 0.5,
                F["tag"][0],
                role="warn_text",
                bg="belkins_warn_pill",
                weight="semibold",
            )
            right_edge -= btn_w + 8.0
        elif needs_relogin:
            btn_lbl = "Re-login"
            btn_w = _pill_w(pen, btn_lbl)
            pen.pill(
                btn_lbl,
                right_edge - btn_w,
                b - 0.5,
                F["tag"][0],
                role="belkins_flame_text",
                bg="belkins_flame_pill",
                weight="semibold",
            )
            right_edge -= btn_w + 8.0
        else:
            if has_toggle:
                pause_lbl = "⏸"
                pw_pause = _pill_w(pen, pause_lbl)
                pen.pill(
                    pause_lbl,
                    right_edge - pw_pause,
                    b - 0.5,
                    F["tag"][0],
                    role="belkins_cmm_sub",
                    bg="belkins_plan_pill",
                    weight="semibold",
                )
                right_edge -= pw_pause + 6.0

            if free_pct is not None:
                if free_pct <= 0:
                    short_rst = _short_reset_mark(r.get("reset") or "", ultra_short=True)
                    sw_lbl = f"Capped {short_rst}".strip() if short_rst else "Capped"
                    sw_role = "crit_text"
                    bar_val: float | None = 0.0
                    bar_sev = "crit"
                elif free_pct <= 35:
                    sw_lbl = f"{free_pct}%"
                    sw_role = "warn_text"
                    bar_val = float(free_pct)
                    bar_sev = "warn"
                else:
                    sw_lbl = f"{free_pct}%"
                    sw_role = "good_text"
                    bar_val = float(free_pct)
                    bar_sev = "ok"
                sw_w = max(34.0, text_w(pen, sw_lbl, "caption_med"))
                text(pen, sw_lbl, "caption_med", sw_role, right_edge, b, right=True)
                right_edge -= sw_w + 8.0
                if free_pct > 0:
                    bar_w = 46.0
                    luxury_bar(pen, right_edge - bar_w, b - 3.6, bar_w, bar_val, bar_sev, h=4.2)
                    right_edge -= bar_w + 10.0

        if r["slot"]:
            text(pen, r["slot"], "slot", "belkins_cmm_sub", X0 + 15.0, b, right=True)
        else:
            dot_c = "dead_fill" if disabled else ("belkins_good_fill" if (free_pct or 0) > 35 else "belkins_warn_fill")
            pen.circle(X0 + 3.0, b - 6.8, 5.4, dot_c)

        tag_cap = "PAUSED" if disabled else next((c[0] for c in r["captions"] if c[1] == "tag"), "")
        show_tag = tag_cap in ("NEXT", "BEST", "PAUSED")
        tag_w = (_pill_w(pen, tag_cap) + 6.0) if show_tag else 0.0
        avail_w = max(48.0, right_edge - x)
        name_role = "belkins_cmm_sub" if disabled else "label"
        nw = text(pen, r["name"], "primary_med", name_role, x, b, maxw=max(42.0, avail_w - tag_w))
        if show_tag and (x + nw + tag_w <= right_edge + 2.0):
            t_role = "warn_text" if disabled else "belkins_cmm_aqua"
            t_bg = "belkins_warn_pill" if disabled else "belkins_tab_active_glass"
            pen.pill(
                tag_cap,
                x + nw + 5.5,
                b - 0.5,
                F["tag"][0],
                role=t_role,
                bg=t_bg,
                weight="semibold",
            )

        return b + 11.0

    return draw


def _row_click_x(r: dict[str, Any]) -> Callable[[float], None] | None:
    toggle_fn = r.get("toggle_disable_action")
    act_fn = r.get("action")
    if not callable(toggle_fn):
        return None
    disabled = bool(r.get("disabled", False))

    def on_click_x(click_x: float) -> None:
        if disabled and click_x >= (X1 - 70.0):
            toggle_fn()
            return
        if (not disabled) and click_x >= (X1 - 26.0):
            toggle_fn()
            return
        if callable(act_fn):
            act_fn()

    return on_click_x


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


def _provider_card(
    spec: dict[str, Any],
    attn_items: list[dict[str, Any]] | None = None,
    *,
    key_prefix: str = "belkins",
    expand_attn: bool = False,
    extra_notes: list[str] | None = None,
) -> _Card:
    card = _Card("belkins_card", "belkins_card_edge", rule=spec.get("rule"))
    key = spec["key"]
    is_hub = not expand_attn
    head_click_x = _card_head_click_x(spec, one_line=is_hub)
    relogin_fn = spec.get("relogin_action") or (lambda: _copy_cmd_to_pasteboard("bswap add"))
    head_tip_parts = [spec["right"] or spec["title"]] + list(extra_notes or ())
    head_ax_parts = [f"{spec['title']}. {spec['right']}".strip(". ")] + list(extra_notes or ())
    head_pills = _select_header_pills(spec, one_line=is_hub)
    card.add(
        _card_head(spec, one_line=is_hub),
        key=f"{key_prefix}-{key}-head",
        ax=". ".join(p for p in head_ax_parts if p),
        tooltip="\n".join(p for p in head_tip_parts if p),
        action=head_pills[0][3] if head_pills else None,
        on_click_x=head_click_x,
        keep_open=True,
        no_hover_bg=True,
    )
    hero = spec["hero"]
    if hero:
        card.add(
            _hero(hero, compact=is_hub),
            key=f"{key_prefix}-{key}-active",
            tooltip=hero["tip"],
            ax=_ax(hero["tip"]),
            action=hero["action"],
            **ROW_HL,
        )

    all_rows = spec["rows"] or []
    if is_hub:
        # On Hub: show at most 2 ready standby accounts (`free > 0%` and not paused) in clean minimalist rows
        ready_rows = [
            r
            for r in all_rows
            if not r.get("disabled") and (r.get("free_pct") is None or r.get("free_pct", 0) > 0)
        ]
        shown_ready = ready_rows[:2]
        hidden_ready = max(0, len(ready_rows) - len(shown_ready))
        capped_cnt = sum(1 for r in all_rows if not r.get("disabled") and r.get("free_pct") == 0)
        paused_cnt = sum(1 for r in all_rows if r.get("disabled"))
        relogin_cnt = len(attn_items or ())

        if shown_ready:
            card.add(_hair(), key=f"{key_prefix}-{key}-rule")
            for r in shown_ready:
                row_key = r["key"] if key_prefix == "belkins" else r["key"].replace("belkins-", f"{key_prefix}-", 1)
                card.add(
                    _row_hub(r),
                    key=row_key,
                    tooltip=r["tip"],
                    ax=_ax(r["tip"]),
                    action=r["action"],
                    **ROW_HL,
                )

        other_cnt = hidden_ready + capped_cnt + paused_cnt
        if relogin_cnt or other_cnt:
            card.add(_hair(), key=f"{key_prefix}-{key}-attn-rule")
            if relogin_cnt:
                sum_str = f"{relogin_cnt} need re-login" + (f"  ·  +{other_cnt} more" if other_cnt else "")
            else:
                sum_str = f"+{other_cnt} more in party"
            all_attn_tips = [it["tip"] for it in (attn_items or ())]
            footer_tip = "\n".join([sum_str] + all_attn_tips + [f"Click to manage all {spec['title']} accounts"])

            def draw_hub_footer(pen: Any, y0: float, s_txt: str = sum_str) -> float:
                b = y0 + 16.5
                text(pen, s_txt, "caption", "belkins_cmm_sub", X0 + 4.0, b, maxw=W_INNER - 74.0)
                text(pen, "All ▸", "caption_med", "belkins_cmm_aqua", X1, b, right=True)
                return b + 8.0

            card.add(
                draw_hub_footer,
                key=f"{key_prefix}-{key}-attn-grouped",
                tooltip=footer_tip,
                ax=_ax(footer_tip),
                action=lambda t=key: set_active_tab(t),
                keep_open=True,
                **ROW_HL,
            )
    else:
        if all_rows:
            card.add(_hair(), key=f"{key_prefix}-{key}-rule")
            for r in all_rows:
                row_key = r["key"] if key_prefix == "belkins" else r["key"].replace("belkins-", f"{key_prefix}-", 1)
                row_cx = _row_click_x(r)
                card.add(
                    _row(r),
                    key=row_key,
                    tooltip=r["tip"],
                    ax=_ax(r["tip"]),
                    action=r["action"],
                    on_click_x=row_cx,
                    **ROW_HL,
                )
        if attn_items:
            card.add(_hair(), key=f"{key_prefix}-{key}-attn-rule")

            def _trigger_relogin() -> None:
                _copy_cmd_to_pasteboard("bswap add")
                if callable(relogin_fn):
                    relogin_fn()

            if len(attn_items) >= 2:
                # Collapse multiple expired accounts into ONE calm, minimalist banner!
                names_short = [it["name"].split(".")[0][:12] for it in attn_items]
                shown_n = ", ".join(names_short[:3]) + (f" +{len(names_short) - 3}" if len(names_short) > 3 else "")
                cmd_chip = "bswap add" if key == "claude" else "Re-login"
                all_tips = "\n".join(it["tip"] for it in attn_items)

                def draw_collapsed_attn(pen: Any, y0: float) -> float:
                    b = y0 + 18.0
                    cw = text_w(pen, cmd_chip, "code") + 8.0
                    pen.chip(
                        cmd_chip,
                        X1 - cw,
                        b,
                        F["code"][0],
                        role="belkins_flame_text",
                        bg="belkins_flame_pill",
                        weight=F["code"][1],
                    )
                    lbl_s = f"{len(attn_items)} need re-login"
                    lw = text(pen, lbl_s, "primary_med", "warn_text", X0 + 4.0, b)
                    rem_w = max(30.0, (X1 - cw - 10.0) - (X0 + 4.0 + lw + 8.0))
                    text(pen, f"·  {shown_n}", "caption", "belkins_cmm_sub", X0 + 4.0 + lw + 7.0, b, maxw=rem_w)
                    return b + 10.5

                card.add(
                    draw_collapsed_attn,
                    key=f"{key_prefix}-{key}-attn-grouped",
                    tooltip=all_tips,
                    ax=_ax(all_tips),
                    action=attn_items[0]["action"] or _trigger_relogin,
                    **ROW_HL,
                )
            else:
                it = attn_items[0]
                act = it["action"] or _trigger_relogin
                card.add(
                    _attn_item(it),
                    key=f"{key_prefix}-{key}-attn-single",
                    tooltip=it["tip"],
                    ax=_ax(it["tip"]),
                    action=act,
                    **ROW_HL,
                )
    card.add(_gap(8.0), key=f"{key_prefix}-{key}-foot")
    return card


def _claude_sessions_card(snapshot: Any) -> _Card | None:
    """Minimalist Active CLI Sessions card on the dedicated `✨ Claude` tab (workspace name + count, no path clutter)."""
    sessions = getattr(snapshot, "sessions", ()) or ()
    if not sessions:
        return None
    card = _Card("belkins_card", "belkins_card_edge", rule="belkins_claude_rule")
    total_sess = len(sessions)

    by_dir: dict[str, list[Any]] = {}
    for s in sessions:
        cwd = str(getattr(s, "cwd", "") or "").strip()
        short_dir = cwd.rstrip("/").split("/")[-1] if cwd else "Workspace"
        by_dir.setdefault(short_dir, []).append((cwd, s))

    def draw_head(pen: Any, y0: float) -> float:
        base = y0 + HEAD_BASE
        symbol(pen, "terminal.fill", X0, base - 4.0, 11.5, "belkins_claude_rule", "semibold")
        text(pen, "Active Sessions", "title", "label", X0 + 18.0, base)
        badge = f"{total_sess} active"
        bw = _pill_w(pen, badge)
        pen.pill(badge, X1 - bw, base - 0.5, F["tag"][0], role="good_text", bg="belkins_good_pill", weight="semibold")
        return base + HEAD_AFTER

    card.add(
        draw_head,
        key="belkins-claude-sessions-head",
        ax=f"Active Claude CLI Sessions: {total_sess} running",
        tooltip=f"{total_sess} active Claude Code CLI processes detected across {len(by_dir)} workspaces",
    )
    card.add(_hair(), key="belkins-claude-sessions-rule")

    for idx, (short_dir, items) in enumerate(by_dir.items()):
        full_cwd = items[0][0] or short_dir
        cnt = len(items)

        def draw_sess_row(
            pen: Any,
            y0: float,
            name_s: str = short_dir,
            n_cnt: int = cnt,
        ) -> float:
            b = y0 + 17.5
            pen.circle(X0 + 4.0, b - 6.8, 5.6, "belkins_good_fill")
            cnt_s = f"{n_cnt}" if n_cnt > 1 else "1"
            text(pen, cnt_s, "caption_med", "belkins_cmm_sub", X1 - 2.0, b, right=True)
            text(pen, name_s, "primary_med", "label", X0 + 16.0, b, maxw=W_INNER - 48.0)
            return b + 9.5

        card.add(
            draw_sess_row,
            key=f"belkins-claude-session-dir-{idx}",
            ax=f"{short_dir}: {cnt} active Claude sessions ({full_cwd})",
            tooltip=f"{full_cwd}\n{cnt} active Claude Code CLI session(s)",
        )

    card.add(_gap(8.0), key="belkins-claude-sessions-foot")
    return card


# ------------------------------------------ Interactive Tabs, Dex & Shop ---


def _tab_bar_block(claude_count: int, codex_count: int) -> Block:
    """Segmented RPG glass pill strip (`Hub | Claude | Codex | Evo | Vault | Studio`) with bottom energy bar."""
    del claude_count, codex_count
    bar_w = W - 2 * CARD_X
    seg_w = bar_w / float(len(TABS))
    bar_h = 30.0
    total_h = 35.0
    game_icons = {
        "party": "square.grid.2x2.fill",
        "claude": "sparkles",
        "codex": "terminal.fill",
        "dex": "trophy.fill",
        "shop": "shield.lefthalf.filled",
        "config": "slider.horizontal.3",
    }

    def paint(pen: Pen, w: float, h: float) -> None:
        _draw_cmm_backdrop(pen, w, h)
        y = 2.5
        fill, edge, _ = _card_surface_roles("belkins_card", "belkins_card_edge", None)
        draw_glass_tile(pen, CARD_X, y, bar_w, bar_h, 9.5, fill, edge)
        pen.rrect(CARD_X + 1.0, y + 1.0, bar_w - 2.0, bar_h - 2.0, 8.5, "belkins_tab_bar")

        try:
            from AppKit import NSBezierPath, NSColor
            from Foundation import NSMakeRect
        except Exception:
            NSBezierPath = None  # type: ignore[assignment]

        active_idx = next((i for i, (tid, _) in enumerate(TABS) if tid == _ACTIVE_TAB), 0)
        if not isinstance(pen, _Measure) and NSBezierPath is not None:
            NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.07).setFill()
            for div_i in range(1, len(TABS)):
                if div_i in (active_idx, active_idx + 1):
                    continue
                dx = CARD_X + div_i * seg_w
                NSBezierPath.fillRect_(NSMakeRect(dx - 0.25, y + 7.0, 0.5, bar_h - 14.0))

        for idx, (tab_id, label) in enumerate(TABS):
            tx = CARD_X + idx * seg_w
            is_act = _ACTIVE_TAB == tab_id
            accent_role = _TAB_ACCENTS.get(tab_id, "belkins_cmm_aqua")
            if is_act:
                px = tx + 2.0
                py = y + 2.0
                pw = seg_w - 4.0
                ph = bar_h - 4.0
                draw_glass_tile(
                    pen,
                    px,
                    py,
                    pw,
                    ph,
                    7.5,
                    "belkins_tab_active_glass",
                    "belkins_tab_active_edge",
                    stroke_width=0.85,
                )
                if not isinstance(pen, _Measure) and NSBezierPath is not None:
                    NSColor.colorWithSRGBRed_green_blue_alpha_(1.0, 1.0, 1.0, 0.16).setFill()
                    NSBezierPath.fillRect_(NSMakeRect(px + 5.0, py + 0.8, max(2.0, pw - 10.0), 0.9))
                eb_w = max(14.0, pw - 16.0)
                eb_x = px + (pw - eb_w) / 2.0
                eb_y = py + ph - 3.2
                pen.rrect(eb_x, eb_y, eb_w, 2.0, 1.0, accent_role)

            txt_role = "label" if is_act else "belkins_cmm_sub"
            ic_role = "belkins_cmm_aqua" if is_act else "belkins_cmm_icon"
            ic_name = _TAB_ICONS.get(tab_id, game_icons.get(tab_id, "sparkles"))
            fk_lbl = "tag"
            tw_lbl = text_w(pen, label, fk_lbl)
            ic_sz = 7.2
            ic_adv = 10.2
            tot_w = ic_adv + tw_lbl
            start_x = tx + max(2.2, (seg_w - tot_w) / 2.0)
            mid_y = y + 13.8
            sw = symbol(pen, ic_name, start_x, mid_y, ic_sz, ic_role, "semibold")
            txt_x = start_x + max(ic_adv - 0.5, (sw + 1.8) if sw > 0 else ic_adv)
            avail_w = max(10.0, (tx + seg_w - 1.5) - txt_x)
            text(
                pen,
                label,
                fk_lbl,
                txt_role,
                txt_x,
                y + 17.2,
                maxw=avail_w,
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
        tooltip="Switch Belkins Command Center tabs (Hub, Claude, Codex, Evo, Vault, Studio)",
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


def _best_bento_switch_row(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the optimal switch candidate row (`NEXT` / `BEST` tagged first, else first actionable row)."""
    if not spec:
        return None
    rows = spec.get("rows") or []
    for r in rows:
        if callable(r.get("action")) and any(
            tag in ("NEXT", "BEST") for tag, _ in (r.get("captions") or ())
        ):
            return r
    return next((r for r in rows if callable(r.get("action"))), None)


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
    """Render a spacious 164x88pt Game HUD Bento tile with 4 zero-clutter elements and 1-click rotation."""
    fill, edge, _ = _card_surface_roles(tile_fill, tile_edge, None)
    draw_glass_tile(pen, tx, ty, tw, th, 12.0, fill, edge)

    pad_x = 12.5
    ix_left = tx + pad_x
    ix_right = tx + tw - pad_x
    inner_w = max(40.0, ix_right - ix_left)

    # 1. Top-Left: Provider vector icon (warm ember for Claude, cyber-emerald for Codex) + bold title
    b1 = ty + 21.0
    raw_title = spec["title"] if spec else fallback_title
    title = raw_title.replace(" Party", "").replace(" Quota", "") if tw < 190.0 else raw_title
    icon_name = spec["icon"] if spec else fallback_icon
    icon_role = (spec.get("rule") if spec else None) or accent_role or "belkins_cmm_icon"
    symbol(pen, icon_name, ix_left, b1 - 4.0, 13.0, icon_role, "semibold")
    ix_title = ix_left + 18.5
    text(pen, title, "hero_name", "label", ix_title, b1, maxw=78.0)

    if not spec:
        text(pen, "No accounts linked", "caption", "belkins_cmm_sub", ix_left, ty + 42.0, maxw=inner_w)
        luxury_bar(pen, ix_left, ty + 53.0, inner_w, 0.0, "dim", h=3.5, expired=True)
        return

    right_parts = spec.get("right_parts") or []
    room_part = next((p for p in right_parts if "room" in p), "")
    room_short = room_part.replace(" room", "").strip() if room_part else ""

    # 2. Top-Right: Crisp `% free` readout in emerald (>50%), amber (16-50%), or coral (<=15%)
    hero = spec.get("hero")
    raw_pct = hero.get("raw_pct") if hero else None
    has_status = bool(hero and hero.get("status"))
    if raw_pct is not None and not has_status:
        used_pct = max(0.0, min(100.0, float(raw_pct)))
        free_pct = max(0, min(100, int(round(100.0 - used_pct))))
        top_right_s = f"{free_pct}% free"
        if free_pct <= 15:
            top_right_role = "belkins_cmm_coral"
            bar_sev = "crit"
        elif free_pct <= 50:
            top_right_role = "warn_text"
            bar_sev = "warn"
        else:
            top_right_role = "good_text"
            bar_sev = "good"
        bar_pct: float | None = float(free_pct)
        bar_expired = False
    else:
        top_right_s = f"{room_short} ok" if room_short else "—"
        top_right_role = "belkins_cmm_coral" if has_status else "belkins_cmm_sub"
        bar_pct = 0.0
        bar_sev = "crit" if has_status else "dim"
        bar_expired = not has_status

    if top_right_s:
        text(pen, top_right_s, "caption_med", top_right_role, ix_right, b1, right=True)

    # 3. Middle Row: Full-width active account + fleet status (`podol · 3/7 ok`) + 3.5pt progress bar
    b2 = ty + 41.5
    if hero:
        if has_status:
            st_txt, _ = hero["status"]
            mid_s = f"{hero.get('name') or 'Active'} · {st_txt}"
            mid_role = "belkins_cmm_coral"
        else:
            hero_nm = str(hero.get("name") or "Active")
            mid_s = f"{hero_nm} · {room_short} ok" if room_short else hero_nm
            mid_role = "belkins_cmm_sub"
    else:
        mid_s = f"No active · {room_short} ok" if room_short else "No active session"
        mid_role = "belkins_cmm_sub"
    text(pen, mid_s, "caption_med", mid_role, ix_left, b2, maxw=inner_w)

    luxury_bar(
        pen,
        ix_left,
        ty + 53.0,
        inner_w,
        bar_pct,
        bar_sev,
        h=3.5,
        expired=bar_expired,
    )

    # 4. Bottom-Right: Clean borderless 1-click rotation link (`⚡ Switch → calloway`)
    b3 = ty + th - 12.5
    best_r = _best_bento_switch_row(spec)
    if best_r is not None:
        nm = str(best_r.get("name") or "next")
        act_txt = f"⚡ Switch → {nm}"
    else:
        total_c = int(spec.get("total_count") or 1)
        act_txt = f"Fleet ({total_c}) →"
    text(pen, act_txt, "caption_med", "belkins_cmm_aqua", ix_right, b3, right=True, maxw=inner_w)


def _hub_bento_blocks(
    claude_spec: dict[str, Any] | None,
    codex_spec: dict[str, Any] | None,
    snapshot: Any,
    attn: dict[str, Any] | None,
    actions: ThemeActions,
    *,
    view_mode: str = "luxury",
    key_prefix: str = "belkins-hub",
) -> list[Block]:
    """Build Unified Dual-Party Command Center supporting all 4 Hub View Styles (`luxury`, `detailed`, `bento`, `cleanmymac`)."""
    out: list[Block] = []
    tile_gap = 8.0
    tile_w = (W - 2 * CARD_X - tile_gap) / 2.0
    lx0 = CARD_X
    rx0 = CARD_X + tile_w + tile_gap
    companion = companion_from_snapshot(snapshot)

    items = (attn.get("items") or []) if attn else []
    extras = (attn.get("extras") or []) if attn else []
    claude_attn = [it for it in items if it.get("provider") == "Claude"]
    codex_attn = [it for it in items if it.get("provider") == "Codex"]

    urgent_extras = [e for e in extras if not str(e).lower().startswith("last switch")]

    if view_mode == "bento":
        # Mode 3 (`bento`): Side-by-side 2×2 Game Bento Tiles + compact Quest Alert Banner
        if claude_spec or codex_spec:
            best_c = _best_bento_switch_row(claude_spec)
            best_x = _best_bento_switch_row(codex_spec)

            def paint_bento_pair(pen: Pen, w: float, h: float) -> None:
                _draw_cmm_backdrop(pen, w, h)
                _draw_provider_bento_tile(
                    pen,
                    claude_spec,
                    lx0,
                    2.0,
                    tile_w,
                    88.0,
                    "belkins_tile_claude",
                    "belkins_tile_claude_edge",
                    "Claude Party",
                    "sparkles",
                    "belkins_claude_rule",
                )
                _draw_provider_bento_tile(
                    pen,
                    codex_spec,
                    rx0,
                    2.0,
                    tile_w,
                    88.0,
                    "belkins_tile_codex",
                    "belkins_tile_codex_edge",
                    "Codex Party",
                    "terminal.fill",
                    "belkins_codex_rule",
                )

            bento_blk: Block | None = None

            def on_bento_click(click_x: float) -> None:
                if click_x < W / 2.0:
                    if best_c and callable(best_c.get("action")):
                        if bento_blk is not None:
                            bento_blk.keep_open = False
                        best_c["action"]()
                    else:
                        if bento_blk is not None:
                            bento_blk.keep_open = True
                        set_active_tab("claude")
                else:
                    if best_x and callable(best_x.get("action")):
                        if bento_blk is not None:
                            bento_blk.keep_open = False
                        best_x["action"]()
                    else:
                        if bento_blk is not None:
                            bento_blk.keep_open = True
                        set_active_tab("codex")

            ax_parts = []
            if claude_spec:
                ax_parts.append(claude_spec["title"])
            if codex_spec:
                ax_parts.append(f"{codex_spec['title']}: {codex_spec['right']}".strip(": "))
            ax_parts += list(extras)
            bento_blk = Block(
                92.0,
                paint_bento_pair,
                key=f"{key_prefix}-bento-providers",
                ax=". ".join(p for p in ax_parts if p),
                tooltip="\n".join(p for p in ax_parts if p),
                action=lambda: on_bento_click(lx0 + 10.0),
                on_click_x=on_bento_click,
                keep_open=True,
                no_hover_bg=True,
            )
            out.append(bento_blk)

        if items or urgent_extras:
            q_cnt = len(items)
            q_txt = f"{q_cnt} {'account needs' if q_cnt == 1 else 'accounts need'} re-login" if q_cnt else str(urgent_extras[0])
            q_tips = [it["tip"] for it in items] + list(urgent_extras)

            def paint_quest_banner(pen: Pen, w: float, h: float, msg: str = q_txt) -> None:
                _draw_cmm_backdrop(pen, w, h)
                fill, edge, _ = _card_surface_roles("belkins_card", "belkins_card_edge", None)
                draw_glass_tile(pen, CARD_X, 2.0, W - 2 * CARD_X, 32.0, 9.0, fill, edge)
                b = 22.0
                symbol(pen, "exclamationmark.triangle.fill", X0, b - 4.0, 11.0, "belkins_warn_fill", "semibold")
                text(pen, msg, "caption_med", "warn_text", X0 + 16.5, b, maxw=W_INNER - 64.0)
                text(pen, "Fix ▸", "caption_med", "belkins_cmm_aqua", X1, b, right=True)

            out.append(
                Block(
                    36.0,
                    paint_quest_banner,
                    key=f"{key_prefix}-bento-quest",
                    ax=f"Quest Alert: {q_txt}. Click to fix.",
                    tooltip="\n".join(q_tips),
                    action=lambda: set_active_tab("claude" if claude_attn else "codex"),
                    keep_open=True,
                    no_hover_bg=True,
                )
            )
    elif view_mode == "cleanmymac":
        # Mode 4 (`cleanmymac`): CleanMyMac Indigo Glass Protection Banner + Side-by-Side Cyan Action Tiles
        q_cnt = len(items)
        q_tips = [it["tip"] for it in items] + list(extras)

        def paint_cmm_banner(pen: Pen, w: float, h: float) -> None:
            _draw_cmm_backdrop(pen, w, h)
            fill, edge, _ = _card_surface_roles("belkins_card", "belkins_card_edge", None)
            draw_glass_tile(pen, CARD_X, 2.0, W - 2 * CARD_X, 44.0, 11.0, fill, edge)
            b1 = 20.0
            b2 = 35.0
            if q_cnt > 0:
                ic_nm, ic_rl = "exclamationmark.shield.fill", "belkins_cmm_coral"
                hdr_s = f"Action Needed: {q_cnt} {'Account' if q_cnt == 1 else 'Accounts'}"
                sub_s = "Expired or capped credentials in party pool"
                pill_s, pill_r, pill_bg = "Fix Now ▸", "crit_text", "belkins_crit_pill"
            else:
                ic_nm, ic_rl = "checkmark.shield.fill", "belkins_cmm_aqua"
                hdr_s = "Fleet Protected & Ready"
                sub_s = "Auto-Switch active · All active quotas healthy"
                pill_s, pill_r, pill_bg = "Protected ✓", "good_text", "belkins_good_pill"
            symbol(pen, ic_nm, X0, b1 + 3.0, 16.0, ic_rl, "semibold")
            pw = _pill_w(pen, pill_s)
            pen.pill(pill_s, X1 - pw, b1 + 4.5, F["tag"][0], role=pill_r, bg=pill_bg, weight="semibold")
            max_tx = max(60.0, (X1 - pw - 8.0) - (X0 + 24.0))
            text(pen, hdr_s, "primary_med", "label", X0 + 24.0, b1, maxw=max_tx)
            text(pen, sub_s, "caption", "belkins_cmm_sub", X0 + 24.0, b2, maxw=max_tx)

        out.append(
            Block(
                48.0,
                paint_cmm_banner,
                key=f"{key_prefix}-cmm-banner",
                ax="Fleet Protection Status. " + (" ".join(q_tips) if q_tips else "All active quotas healthy."),
                tooltip="\n".join(q_tips) if q_tips else "Fleet Protected & Ready",
                action=lambda: set_active_tab("claude" if claude_attn else "codex"),
                keep_open=True,
                no_hover_bg=True,
            )
        )

        if claude_spec or codex_spec:
            def paint_cmm_pair(pen: Pen, w: float, h: float) -> None:
                _draw_cmm_backdrop(pen, w, h)
                _draw_provider_bento_tile(
                    pen,
                    claude_spec,
                    lx0,
                    2.0,
                    tile_w,
                    88.0,
                    "belkins_tile_claude",
                    "belkins_tile_claude_edge",
                    "Claude Quota",
                    "sparkles",
                    "belkins_cmm_aqua",
                )
                _draw_provider_bento_tile(
                    pen,
                    codex_spec,
                    rx0,
                    2.0,
                    tile_w,
                    88.0,
                    "belkins_tile_codex",
                    "belkins_tile_codex_edge",
                    "Codex Quota",
                    "terminal.fill",
                    "belkins_cmm_aqua",
                )

            ax_cmm = []
            if claude_spec:
                ax_cmm.append(claude_spec["title"])
            if codex_spec:
                ax_cmm.append(f"{codex_spec['title']}: {codex_spec['right']}".strip(": "))
            out.append(
                Block(
                    92.0,
                    paint_cmm_pair,
                    key=f"{key_prefix}-cmm-providers",
                    ax=". ".join(p for p in ax_cmm if p),
                    tooltip="\n".join(p for p in ax_cmm if p),
                    action=lambda: set_active_tab("claude"),
                    on_click_x=lambda cx: set_active_tab("claude" if cx < W / 2.0 else "codex"),
                    keep_open=True,
                    no_hover_bg=True,
                )
            )
    else:
        # Mode 1 (`luxury`, Default) & Mode 2 (`detailed`, Expanded 2-Line Party Command Center)
        expand_all = view_mode == "detailed"
        if urgent_extras and not claude_spec and not codex_spec:
            extra_card = _Card("belkins_card", "belkins_card_edge", rule="belkins_attn_rule")
            for i, line in enumerate(urgent_extras):
                extra_card.add(_attn_extra(line), key=f"{key_prefix}-attn-extra-{i}", tooltip=line, ax=line)
            extra_card.add(_gap(4.0), key=f"{key_prefix}-attn-extra-foot")
            out += extra_card.blocks(4.0, 4.0)

        if claude_spec:
            out += _provider_card(
                claude_spec,
                claude_attn,
                key_prefix=key_prefix,
                expand_attn=expand_all,
                extra_notes=list(extras),
            ).blocks(4.0, 4.0)

        if codex_spec:
            out += _provider_card(
                codex_spec,
                codex_attn,
                key_prefix=key_prefix,
                expand_attn=expand_all,
                extra_notes=list(extras) if not claude_spec else None,
            ).blocks(4.0, 4.0)

    # 4. Minimalist Side-by-Side Single-Line Telemetry Pills (`Today` & `30d`)
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
                xp_today = int(getattr(companion, "xp_today", 0) or 0)
                if xp_today >= 1_000_000:
                    xp_str = f"+{xp_today / 1_000_000.0:.1f}M XP"
                elif xp_today >= 1_000:
                    xp_str = f"+{int(round(xp_today / 1000.0))}K XP"
                elif xp_today > 0:
                    xp_str = f"+{xp_today} XP"
                else:
                    xp_str = "+0 XP"

                open_dash = getattr(actions, "open_dashboard", None)
                dash_on = bool(snapshot.settings.get("dashboard_enabled", True))
                dash_act = open_dash if (callable(open_dash) and dash_on) else None

                def paint_telemetry(pen: Pen, w: float, h: float) -> None:
                    _draw_cmm_backdrop(pen, w, h)
                    ty = 3.0
                    th = 38.0
                    b = ty + 23.5
                    fill, edge, _ = _card_surface_roles("belkins_card", "belkins_card_edge", None)

                    # Left Pill: Today (`⚡ Today` · `$388`)
                    draw_glass_tile(pen, lx0, ty, tile_w, th, 12.0, fill, edge)
                    symbol(pen, "bolt.fill", lx0 + 13.0, b - 3.8, 11.0, "belkins_flame_rule", "semibold")
                    text(pen, "Today", "primary_med", "belkins_cmm_sub", lx0 + 28.0, b)
                    text(pen, _compact_usd_str(usd_today), "hero_name", "label", lx0 + tile_w - 14.0, b, right=True)

                    # Right Pill: 30d (`◈ 30d` · `$58.1K`)
                    draw_glass_tile(pen, rx0, ty, tile_w, th, 12.0, fill, edge)
                    symbol(pen, "sparkles", rx0 + 13.0, b - 3.8, 11.0, "belkins_cmm_icon", "semibold")
                    text(pen, "30d", "primary_med", "belkins_cmm_sub", rx0 + 28.0, b)
                    text(pen, _compact_usd_str(usd_30d), "hero_name", "label", rx0 + tile_w - 14.0, b, right=True)

                burn_key = "belkins-party-burn-velocity" if key_prefix == "belkins-hub" else f"{key_prefix}-burn-velocity"
                out.append(
                    Block(
                        45.0,
                        paint_telemetry,
                        key=burn_key,
                        ax=f"Today Burn {format_usd(usd_today)} ({xp_str}), 30d Spend {format_usd(usd_30d)}, 7d average {format_usd(avg_7d)} per day.",
                        tooltip=f"Today: {format_usd(usd_today)} · {format_tokens(tok_today)} tokens ({xp_str})\n30d: {format_usd(usd_30d)} · {format_usd(avg_7d)}/d avg · {sess_cnt} active CLI sessions\nClick to open the full HTML Usage Dashboard.",
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
        b = y0 + 17.5
        avail = live.daily_crate_available
        ic_role = "belkins_gold_rule" if avail else "good_text"
        symbol(pen, "gift.fill" if avail else "checkmark.seal.fill", X0 + 4.0, b - 4.0, 10.5, ic_role, "semibold")
        pill_txt = "✦ CLAIM +150 CR" if avail else "✓ CLAIMED TODAY"
        pill_role = "belkins_gold_text" if avail else "good_text"
        pill_bg = "belkins_gold_pill" if avail else "belkins_good_pill"
        pw = _pill_w(pen, pill_txt)
        pen.pill(pill_txt, X1 - pw, b - 0.5, F["tag"][0], role=pill_role, bg=pill_bg, weight="semibold")
        lbl = f"Daily Supply Crate · Day {live.streak_days} Streak"
        text(
            pen,
            lbl,
            "caption_med",
            "label" if avail else "belkins_cmm_sub",
            X0 + 19.5,
            b,
            maxw=max(60.0, X1 - pw - (X0 + 19.5) - 8.0),
        )
        return b + 8.5

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
        pen.rrect(X0, bar_y, W_INNER, bar_h, 6.5, "belkins_subtab_bg", stroke="belkins_inner_edge", stroke_width=0.6)
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
        return bar_y + bar_h + 7.5

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

                box_y = y0 + 2.0
                box_h = 34.0
                if cur:
                    bg_r, edge_r = "belkins_badge_on", "belkins_badge_on_edge"
                elif unl:
                    bg_r, edge_r = "belkins_inner_panel", "belkins_inner_edge"
                else:
                    bg_r, edge_r = "belkins_badge_off", "belkins_badge_off_edge"
                pen.rrect(X0, box_y, W_INNER, box_h, 7.0, bg_r, stroke=edge_r)

                draw_pixel_companion(
                    pen,
                    stage,
                    X0 + 5.0,
                    box_y + 4.0,
                    26.0,
                    mood_key="overdrive" if cur else "locked_in",
                    equipped=eq_live if cur else (),
                )
                bx = X0 + 37.0
                b1 = box_y + 14.0
                b2 = box_y + 27.0
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
                pen.pill(tag_txt, X1 - 6.0 - tw_pill, b1 - 0.5, F["tag"][0], role=tag_role, bg=tag_bg, weight="semibold")
                name_w = text(pen, f"{stage.stage}. {stage.name}", "primary_med", "label" if unl else "belkins_cmm_sub", bx, b1)
                text(
                    pen,
                    f"·  {stage.title}",
                    "caption",
                    "belkins_cmm_sub" if unl else "secondary",
                    bx + name_w + 5.0,
                    b1,
                    maxw=max(20.0, X1 - 6.0 - tw_pill - bx - name_w - 10.0),
                )
                sub_role = "belkins_gold_text" if cur else ("belkins_cmm_aqua" if unl else "secondary")
                text(
                    pen,
                    f"{stage.passive}  ·  {stage.base_atk} ATK",
                    "tag",
                    sub_role,
                    bx,
                    b2,
                    maxw=W_INNER - 45.0,
                )
                return box_y + box_h + 4.0

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
                box_h = 36.0
                b1 = box_y + 14.0
                b2 = box_y + 27.5
                for c_idx, b_item in enumerate(items):
                    cx = X0 + c_idx * (col_w + col_gap)
                    bg_role = "belkins_badge_on" if b_item.unlocked else "belkins_inner_panel"
                    edge_role = "belkins_badge_on_edge" if b_item.unlocked else "belkins_inner_edge"
                    pen.rrect(cx, box_y, col_w, box_h, 6.0, bg_role, stroke=edge_role)
                    mark_ic = "checkmark.seal.fill" if b_item.unlocked else "lock.fill"
                    mark_role = "good_text" if b_item.unlocked else "secondary"
                    symbol(pen, mark_ic, cx + col_w - 15.5, b1 - 4.0, 8.5, mark_role, "semibold")
                    text(
                        pen,
                        f"{b_item.icon} {b_item.name}",
                        "caption_med",
                        "label" if b_item.unlocked else "belkins_cmm_sub",
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
                return box_y + box_h + 5.0

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
        pen.rrect(X0, bar_y, W_INNER, bar_h, 6.5, "belkins_subtab_bg", stroke="belkins_inner_edge", stroke_width=0.75)
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
    """`⚙️ Settings` Tab: Unified, Single-Screen Control Center for Views, Themes, Menu-Bar Header, Icon, Thresholds & Engine."""
    del subtab
    card = _Card("belkins_card", "belkins_card_edge", rule="belkins_flame_rule")

    def _get_s(k: str, default: Any = None) -> Any:
        if k in _LIVE_SETTINGS:
            return _LIVE_SETTINGS[k]
        return snapshot.settings.get(k, default)

    def _put_s(k: str, val: Any) -> None:
        _LIVE_SETTINGS[k] = val
        if isinstance(getattr(snapshot, "settings", None), dict):
            snapshot.settings[k] = val

    def _cur_view() -> str:
        v = str(_get_s("title_view", "standard") or "standard")
        if bool(_get_s("title_compact", False)) and v == "standard":
            return "compact"
        return v

    def _cur_icon() -> str:
        if not bool(_get_s("title_show_icon", True)):
            return "none"
        return str(_get_s("title_icon_style", "bolt") or "bolt")

    def _cur_theme() -> str:
        if bool(_get_s("menu_layout_classic", False)):
            return "classic"
        return str(_get_s("menu_theme", "belkins") or "belkins")

    def _set_hub_view(mode: str, label: str) -> None:
        global _LAST_CFG_NOTE
        _put_s("belkins_hub_view", mode)
        actions.set_setting("belkins_hub_view", mode)
        _LAST_CFG_NOTE = f"Hub View → {label} ✓"

    def _set_theme(th: str, label: str) -> None:
        global _LAST_CFG_NOTE
        _put_s("menu_theme", th)
        _put_s("menu_layout_classic", th == "classic")
        _LAST_CFG_NOTE = f"Theme → {label} ✓"
        actions.set_setting("menu_theme", th)

    def _set_view(v: str, label: str) -> None:
        global _LAST_CFG_NOTE
        _put_s("title_view", v)
        _put_s("title_compact", v == "compact")
        if v == "full":
            for ck, cv in (
                ("title_show_alias", True),
                ("title_show_five_hour_pct", True),
                ("title_show_seven_day_pct", True),
                ("title_show_scoped_pct", True),
                ("title_show_codex_pct", True),
                ("title_show_cost", True),
                ("title_show_tokens", False),
                ("title_show_companion", False),
                ("title_show_coins", False),
                ("title_show_boss", False),
            ):
                _put_s(ck, cv)
        elif v == "companion":
            for ck, cv in (
                ("title_show_alias", False),
                ("title_show_five_hour_pct", True),
                ("title_show_seven_day_pct", False),
                ("title_show_scoped_pct", False),
                ("title_show_codex_pct", False),
                ("title_show_cost", False),
                ("title_show_tokens", False),
                ("title_show_companion", True),
                ("title_show_coins", True),
                ("title_show_boss", False),
            ):
                _put_s(ck, cv)
        actions.set_setting("title_view", v)
        _LAST_CFG_NOTE = f"Layout → {label} ✓"

    def _set_icon(st: str, label: str) -> None:
        global _LAST_CFG_NOTE
        _put_s("title_icon_style", st)
        _put_s("title_show_icon", st != "none")
        actions.set_setting("title_icon_style", st)
        _LAST_CFG_NOTE = f"Icon → {label} ✓"

    def _toggle_key(k: str, label: str, default: bool = True) -> None:
        global _LAST_CFG_NOTE
        cur = bool(_get_s(k, default))
        new_val = not cur
        _put_s(k, new_val)
        if k.startswith("title_show_"):
            _put_s("title_view", "standard")
            _put_s("title_compact", False)
        actions.set_setting(k, new_val)
        _LAST_CFG_NOTE = f"{label}: {'ON ✓' if new_val else 'OFF'}"

    def _set_val(k: str, val: Any, label: str) -> None:
        global _LAST_CFG_NOTE
        _put_s(k, val)
        actions.set_setting(k, val)
        _LAST_CFG_NOTE = label

    _ICON_SF_MAP: dict[str, str] = {
        "bolt": "bolt.fill",
        "companion": "sparkles",
        "crown": "crown.fill",
        "flame": "flame.fill",
        "cpu": "gamecontroller.fill",
        "bars": "chart.bar.fill",
        "shield": "bolt.shield.fill",
    }

    # 1. Header + Live Menu-Bar Preview Strip
    def draw_cfg_head(pen: Any, y0: float) -> float:
        base = y0 + HEAD_BASE
        symbol(pen, "gearshape.fill", X0, base - 4.0, 11.5, "belkins_flame_rule", "semibold")
        text(pen, "Settings & View Studio", "title", "label", X0 + 18.0, base)
        note_tag = _LAST_CFG_NOTE if _LAST_CFG_NOTE else f"VIEW: {_cur_hub_view(snapshot).upper()}"
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
        if live_str.startswith("⚡"):
            live_str = live_str.lstrip("⚡").strip()

        ic_style = _cur_icon()
        ic_sf = _ICON_SF_MAP.get(ic_style)
        tw_live = text_w(pen, live_str, "caption_med")
        ic_w = 16.5 if ic_sf else 0.0
        pill_pad = 16.0
        mock_w = min(W_INNER - 115.0, max(54.0, tw_live + ic_w + pill_pad))
        mock_x = X1 - mock_w - 6.0
        pen.rrect(mock_x, strip_y + 3.0, mock_w, strip_h - 6.0, 5.0, "belkins_badge_off", stroke="belkins_badge_on_edge")
        tx = mock_x + 8.0
        if ic_sf:
            sw_live = symbol(pen, ic_sf, tx, strip_y + 13.0, 9.5, "belkins_flame_text", "semibold")
            tx += max(15.5, sw_live + 3.5)
        text(pen, live_str or "Icon Only", "caption_med", "label", tx, sb, maxw=mock_x + mock_w - tx - 5.0)
        return strip_y + strip_h + 4.0

    card.add(
        draw_cfg_head,
        key="belkins-cfg-head",
        ax="Settings and View Studio with Live Menu-Bar Preview",
        tooltip="Every change below updates your macOS menu-bar status item and Hub view immediately.",
    )

    # Helper: Horizontal Segmented Control Bar (supports dynamic callable labels for ON/OFF state!)
    def _add_segmented_bar(
        row_key: str,
        sec_label: str,
        items: list[tuple[Any, str | None, Callable[[], bool], Callable[[], None], str]],
    ) -> None:
        n = len(items)
        gap_s = 4.0
        seg_w = (W_INNER - gap_s * (n - 1)) / float(n)

        def draw_seg(pen: Any, y0: float, seg_items: list = items) -> float:
            lbl_b = y0 + 14.0
            text(pen, sec_label, "tag", "belkins_cmm_sub", X0, lbl_b)
            bar_y = lbl_b + 4.5
            bar_h = 26.0
            b_txt = bar_y + 17.0
            for idx, (raw_lbl, sf_ic, is_on_fn, _act_fn, _tip) in enumerate(seg_items):
                lbl = str(raw_lbl() if callable(raw_lbl) else raw_lbl)
                sx = X0 + idx * (seg_w + gap_s)
                on = bool(is_on_fn())
                bg = "belkins_tab_active_fill" if on else "belkins_badge_off"
                edge = "belkins_flame_rule" if on else "belkins_badge_off_edge"
                pen.rrect(sx, bar_y, seg_w, bar_h, 6.0, bg, stroke=edge)
                txt_r = "belkins_tab_active_text" if on else "belkins_tab_inactive_text"
                if sf_ic:
                    fk_seg = "tag" if len(seg_items) >= 4 else "caption_med"
                    tw = text_w(pen, lbl, fk_seg)
                    ic_s = 6.6 if len(seg_items) >= 6 else (7.2 if len(seg_items) == 5 else 7.8)
                    ic_adv = 9.0 if len(seg_items) >= 6 else (10.0 if len(seg_items) == 5 else 11.0)
                    tot = ic_adv + tw
                    st_x = sx + max(1.5, (seg_w - tot) / 2.0)
                    sw = symbol(pen, sf_ic, st_x, bar_y + 13.0, ic_s, txt_r, "semibold")
                    gap_ic = 1.2 if len(seg_items) >= 5 else 2.0
                    txt_x = st_x + max(ic_adv - 1.0, (sw + gap_ic) if sw > 0 else ic_adv)
                    text(pen, lbl, fk_seg, txt_r, txt_x, b_txt, maxw=max(12.0, (sx + seg_w - 1.2) - txt_x))
                else:
                    text(pen, lbl, "caption_med", txt_r, sx + seg_w / 2.0, b_txt, center=True, maxw=seg_w - 4.0)
            return bar_y + bar_h + 3.5

        def on_seg_click(click_x: float, seg_items: list = items) -> None:
            rel = max(0.0, min(W_INNER - 1.0, click_x - X0))
            idx = min(len(seg_items) - 1, max(0, int(rel // (seg_w + gap_s))))
            seg_items[idx][3]()

        ax_labels = [str(i[0]() if callable(i[0]) else i[0]) for i in items]
        card.add(
            draw_seg,
            key=row_key,
            ax=f"{sec_label}: " + ", ".join(ax_labels),
            tooltip="\n".join(i[4] for i in items if i[4]),
            action=lambda seg_items=items: seg_items[0][3](),
            on_click_x=on_seg_click,
            keep_open=True,
            no_hover_bg=True,
        )

    # 1. Hub View Style (Switch between New Luxury View & Previous Views!)
    _add_segmented_bar(
        "belkins-cfg-hub-view-bar",
        "HUB VIEW",
        [
            ("Luxury", "sparkles", lambda: _cur_hub_view(snapshot) == "luxury", lambda: _set_hub_view("luxury", "Luxury Minimal"), "Sculptural Master Stamina Hero + 1-line ready standby rows (Default)"),
            ("Full Party", "bolt.fill", lambda: _cur_hub_view(snapshot) == "detailed", lambda: _set_hub_view("detailed", "Full Party"), "Expanded standby slots with Switch/Pause pills & multi-bar hero"),
            ("2×2 Bento", "square.grid.2x2.fill", lambda: _cur_hub_view(snapshot) == "bento", lambda: _set_hub_view("bento", "2×2 Bento Tiles"), "Compact side-by-side 2×2 Game Bento tiles + 1-line Quest Banner"),
            ("CleanMac", "shield.fill", lambda: _cur_hub_view(snapshot) == "cleanmymac", lambda: _set_hub_view("cleanmymac", "CleanMyMac Glass"), "CleanMyMac Fleet Protection Banner + cyan quota tiles"),
        ],
    )

    # 2. App Theme (Switch between Belkins & Classic / Legacy Themes!)
    _add_segmented_bar(
        "belkins-cfg-theme-bar",
        "THEME",
        [
            ("Belkins", "bolt.fill", lambda: _cur_theme() == "belkins", lambda: _set_theme("belkins", "Belkins ⚡"), "Belkins Companion, Boss Raid & Party Command Center"),
            ("Cards", "rectangle.stack.fill", lambda: _cur_theme() == "cards", lambda: _set_theme("cards", "Cards"), "Classic native Cards theme"),
            ("Apple", "apple.logo", lambda: _cur_theme() == "apple", lambda: _set_theme("apple", "Apple"), "Classic Apple native bars theme"),
            ("Dense", "list.bullet", lambda: _cur_theme() == "dense", lambda: _set_theme("dense", "Dense"), "Compact Dense card theme"),
            ("Glance", "doc.text", lambda: _cur_theme() == "glance", lambda: _set_theme("glance", "Glance"), "Glance text menu layout"),
            ("Classic", "list.dash", lambda: _cur_theme() == "classic", lambda: _set_theme("classic", "Classic"), "Original Classic text menu layout"),
        ],
    )

    # 3. Menu-Bar Icon (6-Segment Horizontal Picker)
    _add_segmented_bar(
        "belkins-cfg-icon-bar",
        "HEADER ICON",
        [
            ("Game", "gamecontroller.fill", lambda: _cur_icon() == "cpu", lambda: _set_icon("cpu", "Game Crest"), "Crowned Cyber-Gamepad menu-bar icon"),
            ("Sprite", "sparkles", lambda: _cur_icon() == "companion", lambda: _set_icon("companion", "Companion Sprite"), "Live 12x12 color pixel sprite of your active Companion"),
            ("Bolt", "bolt.fill", lambda: _cur_icon() == "bolt", lambda: _set_icon("bolt", "Belkins Bolt"), "Belkins Lightning Bolt icon"),
            ("Crown", "crown.fill", lambda: _cur_icon() == "crown", lambda: _set_icon("crown", "Crown"), "Sovereign Crown icon"),
            ("Bars", "chart.bar.fill", lambda: _cur_icon() == "bars", lambda: _set_icon("bars", "Bars"), "Classic Usage Bars icon"),
            ("None", "nosign", lambda: _cur_icon() == "none", lambda: _set_icon("none", "No Icon"), "Text only (hide menu-bar icon)"),
        ],
    )

    # 4. Menu-Bar View Preset (4-Segment Horizontal Picker)
    _add_segmented_bar(
        "belkins-cfg-view-bar",
        "HEADER PRESET",
        [
            ("Custom", "slider.horizontal.3", lambda: _cur_view() == "standard", lambda: _set_view("standard", "Custom Chips"), "Show the exact stats toggled below"),
            ("Full Info", "chart.bar.fill", lambda: _cur_view() == "full", lambda: _set_view("full", "Full Info"), "Show Account + 5h% + 7d% + Codex% + Today Cost"),
            ("Companion", "sparkles", lambda: _cur_view() == "companion", lambda: _set_view("companion", "Companion HUD"), "Show Companion Level/Form + 5h% + SparkCoins"),
            ("Compact", "rectangle.compress.vertical", lambda: _cur_view() == "compact", lambda: _set_view("compact", "Compact V·C"), "Ultra-narrow V·C 100/100 view for crowded menu bars"),
        ],
    )

    # 5. Visible Menu-Bar Stats (Compact 4-Column × 2-Row Chip Grid with explicit ON / OFF state!)
    card.add(
        lambda pen, y0: text(pen, "HEADER STATS", "tag", "belkins_cmm_sub", X0, y0 + 14.0) and (y0 + 17.5),
        key="belkins-cfg-chips-hdr",
    )
    chip_rows = [
        [
            ("Alias", "person.fill", lambda: bool(_get_s("title_show_alias", True)), lambda: _toggle_key("title_show_alias", "Account Alias", True), "Show active Claude account alias"),
            ("5h %", "bolt.fill", lambda: bool(_get_s("title_show_five_hour_pct", True)), lambda: _toggle_key("title_show_five_hour_pct", "5h Quota %", True), "Show active 5-hour quota %"),
            ("7d %", "calendar", lambda: bool(_get_s("title_show_seven_day_pct", False)), lambda: _toggle_key("title_show_seven_day_pct", "7d Weekly %", False), "Show 7-day weekly quota %"),
            ("Codex", "terminal.fill", lambda: bool(_get_s("title_show_codex_pct", False)), lambda: _toggle_key("title_show_codex_pct", "Codex %", False), "Show active Codex weekly %"),
        ],
        [
            ("$ Cost", "dollarsign.circle.fill", lambda: bool(_get_s("title_show_cost", True)), lambda: _toggle_key("title_show_cost", "Today Cost", True), "Show today's notional USD spend ($/d)"),
            ("Tokens", "number.square.fill", lambda: bool(_get_s("title_show_tokens", False)), lambda: _toggle_key("title_show_tokens", "Today Tokens", False), "Show today's token burn (e.g. 42M)"),
            ("Level", "sparkles", lambda: bool(_get_s("title_show_companion", False)), lambda: _toggle_key("title_show_companion", "Companion Lv", False), "Show Companion Level & Stage in header"),
            ("Boss", "flame.fill", lambda: bool(_get_s("title_show_boss", False)), lambda: _toggle_key("title_show_boss", "Raid Boss HP", False), "Show active Raid Boss HP% in header"),
        ],
    ]
    chip_gap = 5.0
    chip_w = (W_INNER - chip_gap * 3.0) / 4.0
    for r_idx, c_row in enumerate(chip_rows):
        def draw_chip_row(pen: Any, y0: float, row_items: list = c_row) -> float:
            by = y0 + 2.0
            bh = 25.0
            bt = by + 16.5
            for c_i, (clbl, _c_sf, on_fn, _act, _tip) in enumerate(row_items):
                cx = X0 + c_i * (chip_w + chip_gap)
                on = bool(on_fn())
                bg = "belkins_good_pill" if on else "belkins_badge_off"
                edge = "belkins_emerald_edge" if on else "belkins_badge_off_edge"
                pen.rrect(cx, by, chip_w, bh, 5.5, bg, stroke=edge)
                dot_role = "belkins_good_fill" if on else "dead_fill"
                pen.circle(cx + 5.5, by + 9.8, 5.4, dot_role)
                state_s = "ON" if on else "OFF"
                state_r = "good_text" if on else "secondary"
                text(pen, state_s, "tag", state_r, cx + chip_w - 5.0, bt, right=True)
                txt_r = "label" if on else "belkins_tab_inactive_text"
                tx_c = cx + 13.5
                text(pen, clbl, "tag", txt_r, tx_c, bt, maxw=max(18.0, (cx + chip_w - 23.0) - tx_c))
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

    # 6. Auto-Switch & Quota Alert Threshold (5-Segment Bar)
    def _cur_thresh() -> int:
        return int(_get_s("notification_threshold_pct", 85) or 85)

    _add_segmented_bar(
        "belkins-cfg-thresh-bar",
        "AUTO-SWITCH",
        [
            (
                lambda: "Auto: ON" if _LIVE_AUTOSWITCH else "Auto: OFF",
                "bolt.fill",
                lambda: bool(_LIVE_AUTOSWITCH),
                lambda: _toggle_autoswitch_ui(actions),
                "Toggle automatic Claude account rotation before hitting the wall",
            ),
            ("80%", None, lambda: _cur_thresh() == 80, lambda: _set_val("notification_threshold_pct", 80, "Alert Threshold → 80% ✓"), "Warn at 80% quota utilization"),
            ("85%", None, lambda: _cur_thresh() == 85, lambda: _set_val("notification_threshold_pct", 85, "Alert Threshold → 85% ✓"), "Warn at 85% quota utilization (default)"),
            ("90%", None, lambda: _cur_thresh() == 90, lambda: _set_val("notification_threshold_pct", 90, "Alert Threshold → 90% ✓"), "Warn at 90% quota utilization"),
            ("95%", None, lambda: _cur_thresh() == 95, lambda: _set_val("notification_threshold_pct", 95, "Alert Threshold → 95% ✓"), "Warn at 95% quota utilization"),
        ],
    )

    # 7. Desktop Alerts & Codex Poll Speed (5-Segment Bar)
    def _cur_poll() -> int:
        return int(_get_s("codex_quota_interval_seconds", 300) or 300)

    _add_segmented_bar(
        "belkins-cfg-poll-bar",
        "ALERTS & POLLING",
        [
            (
                lambda: "Bell: ON" if bool(_get_s("notifications_enabled", True)) else "Bell: OFF",
                "bell.fill",
                lambda: bool(_get_s("notifications_enabled", True)),
                lambda: _toggle_key("notifications_enabled", "Desktop Alerts", True),
                "Toggle macOS Notification Center quota alerts",
            ),
            ("1m", None, lambda: _cur_poll() == 60, lambda: _set_val("codex_quota_interval_seconds", 60, "Codex Poll → 1 min ✓"), "Poll live Codex quotas every 1 minute"),
            ("5m", None, lambda: _cur_poll() == 300, lambda: _set_val("codex_quota_interval_seconds", 300, "Codex Poll → 5 min ✓"), "Poll live Codex quotas every 5 minutes (recommended)"),
            ("10m", None, lambda: _cur_poll() == 600, lambda: _set_val("codex_quota_interval_seconds", 600, "Codex Poll → 10 min ✓"), "Poll live Codex quotas every 10 minutes"),
            ("15m", None, lambda: _cur_poll() == 900, lambda: _set_val("codex_quota_interval_seconds", 900, "Codex Poll → 15 min ✓"), "Poll live Codex quotas every 15 minutes"),
        ],
    )

    # 8. Core Engine Switches & Classic Menu Tail (4-Segment Bar with explicit :ON / :OFF labels!)
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
        "MODULES",
        [
            (
                lambda: f"Codex: {'ON' if bool(_get_s('codex_live_quota_enabled', False)) else 'OFF'}",
                "antenna.radiowaves.left.and.right",
                lambda: bool(_get_s("codex_live_quota_enabled", False)),
                lambda: _toggle_key("codex_live_quota_enabled", "Codex Live Quota", False),
                "Enable live per-account Codex OAuth quota polling",
            ),
            (
                lambda: f"Cost: {'ON' if bool(_get_s('cost_tracking_enabled', True)) else 'OFF'}",
                "dollarsign.circle.fill",
                lambda: bool(_get_s("cost_tracking_enabled", True)),
                lambda: _toggle_key("cost_tracking_enabled", "Cost Tracking", True),
                "Track local transcript token & USD spend",
            ),
            (
                lambda: f"Anim: {'ON' if bool(_get_s('belkins_animations_enabled', True)) else 'OFF'}",
                "sparkles",
                lambda: bool(_get_s("belkins_animations_enabled", True)),
                lambda: _toggle_key("belkins_animations_enabled", "Idle Animations", True),
                "Toggle retro pixel breathing/bobbing animations while menu is open",
            ),
            (
                lambda: f"Tail: {'ON' if bool(_get_s('belkins_native_tail', False)) else 'OFF'}",
                "list.bullet.rectangle",
                lambda: bool(_get_s("belkins_native_tail", False)),
                lambda: _toggle_key("belkins_native_tail", "Classic Menu Tail", False),
                "Show classic macOS menu tail (Cost, Sessions, Resets, Settings) at bottom of dropdown",
            ),
        ],
    )

    _add_segmented_bar(
        "belkins-cfg-actions-bar",
        "TOOLS",
        [
            ("Update App", "arrow.triangle.2.circlepath", lambda: False, _trig_update, "Pull latest Belkins Usage Bar from GitHub & restart (`bswap update`)"),
            ("Web Dashboard", "safari.fill", lambda: False, _trig_dash, "Open the local Belkins HTML Usage & Companion Dashboard"),
            ("Export CSV", "square.and.arrow.down.fill", lambda: False, _trig_csv, "Export daily token & USD history to ~/Downloads"),
        ],
    )

    card.add(_gap(8.0), key="belkins-cfg-foot")
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
    """CleanMyMac-style bottom utility dock (`[🎮]   Open Command Center   [⊞] [↻] [⚙️] [⏻]`)."""
    btn_sz = 26.0
    btn_gap = 4.5
    dock_blk: Block | None = None

    def paint(pen: Pen, w: float, h: float) -> None:
        _draw_cmm_backdrop(pen, w, h)
        pen.line(0.0, 1.0, w, "belkins_hair", thickness=0.5)
        y = 7.0
        mid_y = y + btn_sz / 2.0
        fill, edge, _ = _card_surface_roles("belkins_glass_btn", "belkins_glass_btn_edge", None)

        # Left square Game / Command Center button (`gamecontroller.fill` -> `trophy.fill` fallback)
        lx = CARD_X
        draw_glass_tile(pen, lx, y, btn_sz, btn_sz, 6.5, fill, edge)
        sw_left = pen.symbol(
            "gamecontroller.fill",
            lx + btn_sz / 2.0,
            mid_y,
            11.0,
            "belkins_cmm_aqua",
            "semibold",
            anchor="center",
        )
        if sw_left <= 0.0:
            pen.symbol(
                "trophy.fill",
                lx + btn_sz / 2.0,
                mid_y,
                10.5,
                "belkins_cmm_aqua",
                "semibold",
                anchor="center",
            )

        # Right square icon buttons (`Cycle View`, `Refresh`, `Settings`, `Quit`)
        right_icons = [
            ("square.grid.2x2.fill", "belkins_cmm_aqua" if _cur_hub_view(snapshot) != "luxury" else "belkins_cmm_sub"),
            ("arrow.clockwise", "belkins_cmm_sub"),
            ("gearshape.fill", "belkins_cmm_aqua" if _ACTIVE_TAB == "config" else "belkins_cmm_sub"),
            ("power", "belkins_cmm_coral"),
        ]
        r_start = w - CARD_X - len(right_icons) * btn_sz - (len(right_icons) - 1) * btn_gap
        for idx, (ic_nm, ic_rl) in enumerate(right_icons):
            rx = r_start + idx * (btn_sz + btn_gap)
            draw_glass_tile(pen, rx, y, btn_sz, btn_sz, 6.5, fill, edge)
            pen.symbol(ic_nm, rx + btn_sz / 2.0, mid_y, 10.2, ic_rl, "semibold", anchor="center")

        # Center action text (`Open Command Center`) symmetrically centered between left & right docks
        left_end = lx + btn_sz
        center_x = (left_end + r_start) / 2.0
        text(
            pen,
            "Open Command Center",
            "primary_med",
            "label",
            center_x,
            y + 17.5,
            center=True,
            maxw=max(60.0, r_start - left_end - 10.0),
        )

    def on_click_x(click_x: float) -> None:
        global _LAST_CFG_NOTE
        r_start = W - CARD_X - 4 * btn_sz - 3 * btn_gap
        if click_x >= r_start:
            rel = click_x - r_start
            slot = int(rel // (btn_sz + btn_gap))
            if slot <= 0:
                if dock_blk is not None:
                    dock_blk.keep_open = True
                cur_v = _cur_hub_view(snapshot)
                nxt_v = _HUB_VIEWS[(_HUB_VIEWS.index(cur_v) + 1) % len(_HUB_VIEWS)]
                _LIVE_SETTINGS["belkins_hub_view"] = nxt_v
                if isinstance(getattr(snapshot, "settings", None), dict):
                    snapshot.settings["belkins_hub_view"] = nxt_v
                actions.set_setting("belkins_hub_view", nxt_v)
                _LAST_CFG_NOTE = f"Hub View → {nxt_v.title()} ✓"
                set_active_tab("party")
            elif slot == 1:
                if dock_blk is not None:
                    dock_blk.keep_open = True
                if callable(actions.refresh):
                    actions.refresh()
            elif slot == 2:
                if dock_blk is not None:
                    dock_blk.keep_open = True
                set_active_tab("party" if _ACTIVE_TAB == "config" else "config")
            else:
                if dock_blk is not None:
                    dock_blk.keep_open = False
                if callable(actions.quit_app):
                    actions.quit_app()
            return
        if dock_blk is not None:
            dock_blk.keep_open = False
        if callable(actions.open_dashboard):
            actions.open_dashboard()

    dock_blk = Block(
        40.0,
        paint,
        key="belkins-footer-dock",
        tooltip="Open Command Center Dashboard, Cycle Hub View (Luxury / Full Party / 2×2 Bento / CleanMyMac), Refresh, Settings, or Quit",
        ax="Open Command Center, Cycle Hub View, Refresh, Settings, Quit",
        action=actions.open_dashboard,
        on_click_x=on_click_x,
        keep_open=False,
        no_hover_bg=True,
    )
    return dock_blk


# ------------------------------------------------------------------ build ---


def build(snapshot: Any, now: float, actions: ThemeActions | None = None) -> list[Block]:
    """Blocks for the top of the Belkins dropdown; the app appends the native tail."""
    from .. import app as A
    from .. import render as R

    global _LIVE_AUTOSWITCH
    _ensure_roles()
    acts = actions or ThemeActions()
    _LIVE_SETTINGS.clear()
    if isinstance(getattr(snapshot, "settings", None), dict):
        _LIVE_SETTINGS.update(snapshot.settings)
    _LIVE_AUTOSWITCH = bool(getattr(snapshot, "autoswitch_enabled", False))
    companion = companion_from_snapshot(snapshot)

    # 1. Always-visible Companion Hero + Segmented 6-Tab Bar
    hero_card = _companion_card(snapshot, companion, acts)
    blocks: list[Block] = hero_card.blocks(TOP_GAP, 2.0)

    claude_sp = _claude_spec(A, R, snapshot, now, acts)
    codex_sp = _codex_spec(A, R, snapshot, now, acts)
    claude_cnt = len(getattr(snapshot, "accounts", ()) or ())
    codex_cnt = len([r for r in A._visible_quota_rows_of(snapshot) if r.vendor != A.VENDOR_CLAUDE and r.slot < 0])
    blocks.append(_tab_bar_block(claude_cnt, codex_cnt))

    # 2. Tab "party" ("Hub"): Pre-build all 4 Hub View Styles (`luxury`, `detailed`, `bento`, `cleanmymac`) for instant in-menu switching!
    attn = _attention_spec(A, snapshot, acts)
    initial_hub = _cur_hub_view(snapshot)
    for hub_mode in _HUB_VIEWS:
        kp = "belkins-hub" if hub_mode == initial_hub else f"belkins-hub-{hub_mode}"
        blocks += _tag_blocks(
            _hub_bento_blocks(
                claude_sp,
                codex_sp,
                snapshot,
                attn,
                acts,
                view_mode=hub_mode,
                key_prefix=kp,
            ),
            "party",
            extra_pred=lambda m=hub_mode: _cur_hub_view(snapshot) == m,
        )

    # 3. Tab "claude": Full Claude Party Manager + Individual Re-Login Slots + Active Claude CLI Sessions
    if claude_sp:
        claude_attn = [it for it in (attn["items"] if attn else ()) if it["provider"] == "Claude"]
        blocks += _tag_blocks(
            _provider_card(claude_sp, claude_attn, key_prefix="belkins", expand_attn=True).blocks(4.0, 4.0),
            "claude",
        )
        sess_card = _claude_sessions_card(snapshot)
        if sess_card is not None:
            blocks += _tag_blocks(sess_card.blocks(4.0, TAIL_GAP), "claude")

    # 4. Tab "codex": Full Codex Party Manager + Detailed Fleet Line
    if codex_sp:
        codex_attn = [it for it in (attn["items"] if attn else ()) if it["provider"] == "Codex"]
        blocks += _tag_blocks(
            _provider_card(codex_sp, codex_attn, key_prefix="belkins", expand_attn=True).blocks(4.0, TAIL_GAP),
            "codex",
        )

    # 5. Tab "dex": Interactive Boss Raid Arena + Daily Crate + 8-Stage Pokedex / 14-Trophy Bento Grid
    blocks += _tag_blocks(
        _dex_card(snapshot, companion, "pokedex").blocks(4.0, TAIL_GAP),
        "dex",
        extra_pred=lambda: _DEX_SUBTAB == "pokedex",
    )
    blocks += _tag_blocks(
        _dex_card(snapshot, companion, "trophies").blocks(4.0, TAIL_GAP),
        "dex",
        extra_pred=lambda: _DEX_SUBTAB == "trophies",
    )

    # 6. Tab "shop": 12-Item Belkins Spark Vault ("gear" sub-tab & "style" sub-tab)
    blocks += _tag_blocks(
        _shop_card(snapshot, companion, "gear").blocks(4.0, TAIL_GAP),
        "shop",
        extra_pred=lambda: _SHOP_SUBTAB == "gear",
    )
    blocks += _tag_blocks(
        _shop_card(snapshot, companion, "style").blocks(4.0, TAIL_GAP),
        "shop",
        extra_pred=lambda: _SHOP_SUBTAB == "style",
    )

    # 7. Tab "config": Unified Single-Screen Settings & View Studio
    blocks += _tag_blocks(
        _config_card(snapshot, acts).blocks(4.0, 4.0),
        "config",
    )

    # 8. Always-visible Liquid Glass Utility Dock
    blocks.append(_footer_dock_block(snapshot, acts))
    return blocks
