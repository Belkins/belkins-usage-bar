"""Belkins AI Companions, XP progression, mood engine, and Trophy Dex.

Inspired by PokeTokenBar, tailored for Belkins Usage Bar (`bswap` / `belkins`).
Pure and non-throwing: every computation derives deterministically from the
live :class:`UiSnapshot` or dashboard cells without touching disk or network on
the AppKit thread (SPEC 2.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Sequence

from .contracts import AccountRow, VENDOR_CLAUDE, VENDOR_CODEX, format_tokens, format_usd


# ---------------------------------------------------------------------------
# Evolution Stages ("Belkins Dex")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvolutionStage:
    """One evolution stage in the Belkins Companion lineage."""

    stage: int
    slug: str
    name: str
    title: str
    min_level: int
    lore: str
    # (R, G, B) in 0..255 for primary body, secondary accent, crest/crown, eye/spark
    body_rgb: tuple[int, int, int]
    accent_rgb: tuple[int, int, int]
    crest_rgb: tuple[int, int, int]
    spark_rgb: tuple[int, int, int]
    sprite: tuple[str, ...]


# 14x14 pixel-art matrices:
# '.' = transparent
# 'O' = dark outline
# 'B' = primary body
# 'A' = inner accent / belly / visor
# 'C' = crest / horn / crown / flame
# 'E' = eye (glowing)
# 'S' = ambient spark / energy particle
_SPRITE_SPARKBIT: Final[tuple[str, ...]] = (
    "......CC......",
    ".....CCCC.....",
    "..S..OOOO..S..",
    "....OBBBBO....",
    "...OBBBBBBO...",
    "..OBBEEEEBBO..",
    "..OBBAAAAABO..",
    "..OBBAAAAABO..",
    "...OBBBBBBO...",
    "....OOOOOO....",
    "...OO....OO...",
    "..S..........S",
    "..............",
    "..............",
)

_SPRITE_OUTREACHIK: Final[tuple[str, ...]] = (
    "..CC......CC..",
    ".COOC....COOC.",
    ".COOOOOOOOOOC.",
    "..OBBBBBBBBO..",
    ".OBBBOOOOBBBO.",
    ".OBBEBBBBEEBO.",
    ".OBBBBBBBBBBO.",
    "..OBBAAAAABO..",
    "..OBBAAAAABO..",
    ".OOBBBBBBBBOO.",
    ".OO.OOOOOO.OO.",
    "....OO..OO....",
    "...OO....OO...",
    "..............",
)

_SPRITE_PIPELINEX: Final[tuple[str, ...]] = (
    "....CCCCCC....",
    "...CCCCCCCC...",
    ".CCOOOOOOOOCC.",
    "COOBBBBBBBBOOC",
    ".OBBBOOOOBBBO.",
    ".OBBEEBBEEBBO.",
    "SOBBBBBBBBBBOS",
    ".OOBBAAAAABOO.",
    "COOBBAAAAABOOC",
    "COOBBBBBBBBOOC",
    ".OO.OOOOOO.OO.",
    "...OO....OO...",
    "..OOO....OOO..",
    "..............",
)

_SPRITE_BELKINOSAUR: Final[tuple[str, ...]] = (
    "..CC..CC..CC..",
    ".CCCCCCCCCCCC.",
    ".COOOOOOOOOOC.",
    "OOOBBBBBBBBOOO",
    "OBBBBOOOOBBBBO",
    "OBBEEEBBEEEBBO",
    "OBBBBBBBBBBBBO",
    "OOBBCCCCCCBBOO",
    "OOBBAAAAABBBOO",
    "OBBBAAAAABBBBO",
    "OOOBBBBBBBBOOO",
    ".OOOO.OO.OOOO.",
    "..OOO....OOO..",
    "..OOO....OOO..",
)

_SPRITE_BELKINS_PRIME: Final[tuple[str, ...]] = (
    ".S..CCCCCC..S.",
    "..CCCCCCCCCC..",
    ".CCOOOOOOOOCC.",
    "COOBBBBBBBBOOC",
    "OBBBCCCCCCBBBO",
    "OBBEEEBBEEEBBO",
    "SOBBBBBBBBBBOS",
    "COOBCCCCCCBOOC",
    "COOBBAAAAABOOC",
    ".OOBBAAAAABOO.",
    "..OBBBBBBBBO..",
    ".OO.OOOOOO.OO.",
    "OO..OO..OO..OO",
    "S...OO..OO...S",
)


EVOLUTION_LINE: Final[tuple[EvolutionStage, ...]] = (
    EvolutionStage(
        stage=1,
        slug="sparkbit",
        name="Sparkbit",
        title="Seedling Pulse",
        min_level=1,
        lore="A curious neural spark born in the Belkins outbound forge. Feeds on fresh context windows.",
        body_rgb=(255, 98, 56),
        accent_rgb=(255, 214, 165),
        crest_rgb=(56, 189, 248),
        spark_rgb=(125, 211, 252),
        sprite=_SPRITE_SPARKBIT,
    ),
    EvolutionStage(
        stage=2,
        slug="outreachik",
        name="Outreachik",
        title="Signal Scout",
        min_level=5,
        lore="An agile token-prowler with twin radar ears. Tracks multi-account headroom and spots rate walls early.",
        body_rgb=(255, 85, 42),
        accent_rgb=(254, 240, 138),
        crest_rgb=(251, 191, 36),
        spark_rgb=(52, 211, 153),
        sprite=_SPRITE_OUTREACHIK,
    ),
    EvolutionStage(
        stage=3,
        slug="pipelinex",
        name="Pipelinex",
        title="Deal Vanguard",
        min_level=12,
        lore="Dual-core striker fusing Claude Fable precision with Codex Astra velocity. Never drops a thread.",
        body_rgb=(249, 115, 22),
        accent_rgb=(253, 224, 71),
        crest_rgb=(168, 85, 247),
        spark_rgb=(56, 189, 248),
        sprite=_SPRITE_PIPELINEX,
    ),
    EvolutionStage(
        stage=4,
        slug="belkinosaur",
        name="Belkinosaur",
        title="Fleet Titan",
        min_level=25,
        lore="Armored 9-slot autonomous juggernaut. Shrugs off 5-hour rate walls by rotating accounts in milliseconds.",
        body_rgb=(234, 68, 32),
        accent_rgb=(167, 243, 208),
        crest_rgb=(16, 185, 129),
        spark_rgb=(250, 204, 21),
        sprite=_SPRITE_BELKINOSAUR,
    ),
    EvolutionStage(
        stage=5,
        slug="belkins-prime",
        name="Belkins Prime",
        title="Apex Sovereign",
        min_level=50,
        lore="Mythic crowned sovereign of the Belkins AI fleet. Commands billions of tokens across parallel agent swarms.",
        body_rgb=(255, 59, 29),
        accent_rgb=(254, 249, 195),
        crest_rgb=(250, 204, 21),
        spark_rgb=(34, 211, 238),
        sprite=_SPRITE_BELKINS_PRIME,
    ),
)


# ---------------------------------------------------------------------------
# Badges / Trophies
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BadgeState:
    """One Belkins Trophy / Achievement badge."""

    slug: str
    icon: str
    name: str
    description: str
    unlocked: bool
    progress_text: str


# ---------------------------------------------------------------------------
# Belkins Poké Mart (Shop & Cosmetics)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShopItemSpec:
    """Static definition of an item in the Belkins Mart."""

    slug: str
    icon: str
    name: str
    category: str       # "berry" | "eyewear" | "headgear" | "aura" | "pad"
    price: int          # cost in SparkCoins (🪙)
    description: str


@dataclass(frozen=True, slots=True)
class ShopItemState:
    """Runtime state of a Belkins Mart item for a CompanionState."""

    slug: str
    icon: str
    name: str
    category: str
    price: int
    description: str
    owned: bool
    equipped: bool


SHOP_CATALOG: tuple[ShopItemSpec, ...] = (
    ShopItemSpec(
        slug="context_berry",
        icon="🍒",
        name="Context Berry",
        category="berry",
        price=150,
        description="Sweet neural berry. Soothes Wall Guard & boosts mood to Overdrive.",
    ),
    ShopItemSpec(
        slug="deal_shades",
        icon="🕶️",
        name="Deal Closer Shades",
        category="eyewear",
        price=400,
        description="Equips cyber-cyan pixel sunglasses onto your Companion sprite.",
    ),
    ShopItemSpec(
        slug="solar_pad",
        icon="🔥",
        name="Solar Forge Pad",
        category="pad",
        price=500,
        description="Transforms your Companion showcase into a crimson-gold forge.",
    ),
    ShopItemSpec(
        slug="emerald_pad",
        icon="🌿",
        name="Emerald Grove Pad",
        category="pad",
        price=500,
        description="Transforms your Companion showcase into a bioluminescent cyber-grove.",
    ),
    ShopItemSpec(
        slug="cyber_halo",
        icon="⚡",
        name="Cyber Halo Aura",
        category="aura",
        price=600,
        description="Surrounds your Companion sprite with a pulsing golden-cyan halo.",
    ),
    ShopItemSpec(
        slug="sovereign_crown",
        icon="👑",
        name="Sovereign Crown",
        category="headgear",
        price=800,
        description="Equips a golden pixel crown on your Companion + 25% XP boost.",
    ),
)


def _shop_save_path() -> Any:
    import os
    from pathlib import Path

    override = os.environ.get("BELKINS_SHOP_PATH")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "cc-usage-widget" / "belkins_shop.json"


def load_shop_save() -> tuple[set[str], set[str]]:
    """Return ``(owned_slugs, equipped_slugs)`` from persistent storage."""
    import json

    path = _shop_save_path()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                valid = {it.slug for it in SHOP_CATALOG}
                owned = {str(s) for s in (data.get("owned") or ()) if str(s) in valid}
                equipped = {str(s) for s in (data.get("equipped") or ()) if str(s) in owned}
                return owned, equipped
    except Exception:
        pass
    return set(), set()


def save_shop_state(owned: set[str], equipped: set[str]) -> bool:
    """Persist ``(owned, equipped)`` atomically to ``belkins_shop.json``."""
    import json

    path = _shop_save_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {
            "owned": sorted(owned),
            "equipped": sorted(equipped & owned),
        }
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        return False


def toggle_or_buy_shop_item(slug: str, coins_earned: int) -> tuple[bool, str]:
    """Buy *slug* (if unowned and affordable) or toggle its equipped state.

    Returns ``(changed, status_message)``.
    """
    spec = next((it for it in SHOP_CATALOG if it.slug == slug), None)
    if spec is None:
        return False, f"Unknown item: {slug}"

    owned, equipped = load_shop_save()
    spent = sum(it.price for it in SHOP_CATALOG if it.slug in owned)
    available = max(0, int(coins_earned) - spent)

    if slug in owned:
        if slug in equipped:
            equipped.discard(slug)
            save_shop_state(owned, equipped)
            return True, f"Unequipped {spec.icon} {spec.name}."
        # Unequip any other item in the same exclusive slot category (e.g. pad)
        if spec.category in {"pad", "headgear", "eyewear"}:
            for other in SHOP_CATALOG:
                if other.category == spec.category and other.slug != slug:
                    equipped.discard(other.slug)
        equipped.add(slug)
        save_shop_state(owned, equipped)
        return True, f"Equipped {spec.icon} {spec.name}!"

    if available < spec.price:
        need = spec.price - available
        return False, f"Need {need:,} more SparkCoins (costs {spec.price:,}, have {available:,})."

    owned.add(slug)
    if spec.category in {"pad", "headgear", "eyewear"}:
        for other in SHOP_CATALOG:
            if other.category == spec.category and other.slug != slug:
                equipped.discard(other.slug)
    equipped.add(slug)
    save_shop_state(owned, equipped)
    return True, f"Purchased & equipped {spec.icon} {spec.name} for {spec.price:,} SparkCoins!"


# ---------------------------------------------------------------------------
# Companion Snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompanionState:
    """Complete gamified state rendered by the Belkins theme, CLI, and Dashboard."""

    stage: EvolutionStage
    next_stage: EvolutionStage | None
    level: int
    xp_total: int
    xp_today: int
    xp_into_level: int
    xp_for_next_level: int
    xp_fraction: float
    mood_key: str        # "overdrive" | "wall_guard" | "locked_in" | "recharging"
    mood_label: str      # "🔥 Overdrive" | "🛡️ Wall Guard" | "⚡ Locked In" | "💤 Recharging"
    mood_role: str       # card_base color role for the mood badge
    tokens_today: int
    tokens_30d: int
    usd_today: float
    usd_30d: float
    claude_ready: int
    claude_total: int
    codex_ready: int
    codex_total: int
    active_sessions: int
    badges: tuple[BadgeState, ...]
    coins_earned: int = 300
    coins_available: int = 300
    shop_items: tuple[ShopItemState, ...] = ()
    equipped_items: tuple[str, ...] = ()

    @property
    def unlocked_badges_count(self) -> int:
        return sum(1 for b in self.badges if b.unlocked)


# ---------------------------------------------------------------------------
# Pure XP & Level Math
# ---------------------------------------------------------------------------


def xp_for_level(level: int) -> int:
    """Cumulative XP required to reach *level* (Lv 1 = 0 XP)."""
    if level <= 1:
        return 0
    return int(90 * ((level - 1) ** 2))


def level_from_xp(xp: int) -> tuple[int, int, int, float]:
    """Return ``(level, xp_into_level, xp_needed_for_band, fraction)``."""
    safe_xp = max(0, int(xp))
    level = 1
    while level < 99 and safe_xp >= xp_for_level(level + 1):
        level += 1
    floor_xp = xp_for_level(level)
    ceil_xp = xp_for_level(level + 1)
    band = max(1, ceil_xp - floor_xp)
    into = max(0, safe_xp - floor_xp)
    fraction = min(1.0, max(0.0, into / float(band)))
    return level, into, band, fraction


def stage_for_level(level: int) -> tuple[EvolutionStage, EvolutionStage | None]:
    """Return ``(current_stage, next_stage)`` for *level*."""
    current = EVOLUTION_LINE[0]
    next_st: EvolutionStage | None = None
    for idx, stage in enumerate(EVOLUTION_LINE):
        if level >= stage.min_level:
            current = stage
            next_st = EVOLUTION_LINE[idx + 1] if idx + 1 < len(EVOLUTION_LINE) else None
    return current, next_st


def compute_companion(
    *,
    tokens_today: int = 0,
    tokens_30d: int = 0,
    usd_today: float = 0.0,
    usd_30d: float = 0.0,
    accounts: Sequence[AccountRow] = (),
    quota_rows: Sequence[AccountRow] = (),
    account_notes: dict[int, str] | None = None,
    active_sessions: int = 0,
    recent_switches_count: int = 0,
    distinct_models_count: int = 0,
    threshold_pct: float = 85.0,
) -> CompanionState:
    """Compute the full :class:`CompanionState` from raw usage and fleet metrics."""
    notes = account_notes or {}
    claude_slots = [
        r for r in accounts
        if r.vendor == VENDOR_CLAUDE and getattr(r, "switchable", True)
    ]
    claude_enabled = [r for r in claude_slots if not getattr(r, "disabled", False)]
    claude_ready = 0
    active_binding_pct = 0.0
    for row in claude_enabled:
        pcts = [
            p for p in (row.five_hour_pct, row.seven_day_pct, *(w[1] for w in row.scoped_windows))
            if p is not None
        ]
        max_p = max(pcts) if pcts else None
        if row.is_active and max_p is not None:
            active_binding_pct = float(max_p)
        if row.slot not in notes and max_p is not None and max_p < threshold_pct:
            claude_ready += 1

    live_codex = [
        r for r in quota_rows
        if r.vendor == VENDOR_CODEX and r.slot < 0
    ]
    if not live_codex:
        live_codex = [r for r in quota_rows if r.vendor == VENDOR_CODEX]
    codex_ready = sum(
        1 for r in live_codex
        if not getattr(r, "attention_note", "")
        and (r.seven_day_pct is None or r.seven_day_pct < 100.0)
    )

    # XP Calculation:
    # - 25 XP per 1M tokens in 30d
    # - 40 XP per $1 notional spend in 30d
    # - 250 XP per switchable Claude slot
    # - 300 XP per Codex slot
    # - 200 XP per recorded switch
    tok_30d = max(0, int(tokens_30d), int(tokens_today))
    tok_today = max(0, int(tokens_today))
    spend_30d = max(0.0, float(usd_30d), float(usd_today))
    spend_today = max(0.0, float(usd_today))

    owned_slugs, equipped_slugs = load_shop_save()
    xp_mult = 1.25 if "sovereign_crown" in equipped_slugs else (1.10 if "context_berry" in equipped_slugs else 1.0)

    xp_from_tokens = int((tok_30d / 1_000_000.0) * 25.0)
    xp_from_spend = int(spend_30d * 40.0)
    xp_from_fleet = len(claude_slots) * 250 + len(live_codex) * 300 + recent_switches_count * 200
    xp_total = int(max(100, xp_from_tokens + xp_from_spend + xp_from_fleet) * xp_mult)
    xp_today = int(((tok_today / 1_000_000.0) * 25.0 + spend_today * 40.0) * xp_mult)

    level, xp_into, xp_band, xp_frac = level_from_xp(xp_total)
    stage, next_stage = stage_for_level(level)

    # Mood determination (Context Berry soothes Wall Guard into Overdrive)
    if "context_berry" in equipped_slugs:
        mood_key = "overdrive"
        mood_label = "🍒 Berry Overdrive"
        mood_role = "Claude"
    elif active_binding_pct >= threshold_pct:
        mood_key = "wall_guard"
        mood_label = "🛡️ Wall Guard"
        mood_role = "amber"
    elif spend_today >= 45.0 or active_binding_pct >= 55.0 or (active_sessions >= 2 and tok_today >= 15_000_000):
        mood_key = "overdrive"
        mood_label = "🔥 Overdrive"
        mood_role = "Claude"
    elif active_sessions > 0 or active_binding_pct >= 8.0 or tok_today >= 1_000_000:
        mood_key = "locked_in"
        mood_label = "⚡ Locked In"
        mood_role = "green"
    else:
        mood_key = "recharging"
        mood_label = "💤 Recharging"
        mood_role = "Codex"

    # 8 Belkins Badges
    badges = (
        BadgeState(
            slug="first_spark",
            icon="⚡",
            name="First Spark",
            description="Process 1M+ tokens in a single day.",
            unlocked=tok_today >= 1_000_000 or tok_30d >= 1_000_000,
            progress_text=f"{format_tokens(max(tok_today, min(tok_30d, 1_000_000)))} / 1.0M tok",
        ),
        BadgeState(
            slug="fleet_commander",
            icon="🎖️",
            name="Fleet Commander",
            description="Command 5+ switchable Claude accounts in your bswap pool.",
            unlocked=len(claude_slots) >= 5,
            progress_text=f"{len(claude_slots)} / 5 accounts",
        ),
        BadgeState(
            slug="dual_engine",
            icon="🔀",
            name="Dual Wielder",
            description="Operate both Claude and Codex accounts simultaneously.",
            unlocked=bool(claude_slots and live_codex),
            progress_text="Claude + Codex active" if (claude_slots and live_codex) else "Single vendor",
        ),
        BadgeState(
            slug="wall_breaker",
            icon="🛡️",
            name="Wall Breaker",
            description="Rotate accounts with bswap to bypass rate walls.",
            unlocked=recent_switches_count > 0 or len(claude_slots) >= 2,
            progress_text=f"{max(1, recent_switches_count)} rotations logged" if (recent_switches_count > 0 or len(claude_slots) >= 2) else "0 rotations",
        ),
        BadgeState(
            slug="centurion",
            icon="🔥",
            name="Centurion Burn",
            description="Reach 100 USD notional spend in a day or 500 USD across 30 days.",
            unlocked=spend_today >= 100.0 or spend_30d >= 500.0,
            progress_text=f"{spend_30d:,.0f} / 500 USD",
        ),
        BadgeState(
            slug="billion_club",
            icon="💎",
            name="Billion Token Club",
            description="Surpass 1B tokens in the 30-day window.",
            unlocked=tok_30d >= 1_000_000_000,
            progress_text=f"{format_tokens(tok_30d)} / 1.0B tok",
        ),
        BadgeState(
            slug="swarm_architect",
            icon="🐝",
            name="Swarm Architect",
            description="Run parallel sessions or 4+ distinct AI models.",
            unlocked=active_sessions >= 2 or distinct_models_count >= 4,
            progress_text=f"{max(distinct_models_count, active_sessions)} active streams",
        ),
        BadgeState(
            slug="apex_sovereign",
            icon="👑",
            name="Apex Sovereign",
            description="Evolve to Lv.50 Belkins Prime.",
            unlocked=level >= 50,
            progress_text=f"Lv.{level} / Lv.50",
        ),
    )

    unlocked_count = sum(1 for b in badges if b.unlocked)
    coins_earned = (
        300
        + int((tok_30d / 1_000_000.0) * 8.0)
        + int(spend_30d * 15.0)
        + unlocked_count * 200
    )
    coins_spent = sum(it.price for it in SHOP_CATALOG if it.slug in owned_slugs)
    coins_available = max(0, coins_earned - coins_spent)
    shop_items = tuple(
        ShopItemState(
            slug=it.slug,
            icon=it.icon,
            name=it.name,
            category=it.category,
            price=it.price,
            description=it.description,
            owned=(it.slug in owned_slugs),
            equipped=(it.slug in equipped_slugs),
        )
        for it in SHOP_CATALOG
    )

    return CompanionState(
        stage=stage,
        next_stage=next_stage,
        level=level,
        xp_total=xp_total,
        xp_today=xp_today,
        xp_into_level=xp_into,
        xp_for_next_level=xp_band,
        xp_fraction=xp_frac,
        mood_key=mood_key,
        mood_label=mood_label,
        mood_role=mood_role,
        tokens_today=tok_today,
        tokens_30d=tok_30d,
        usd_today=spend_today,
        usd_30d=spend_30d,
        claude_ready=claude_ready,
        claude_total=len(claude_enabled),
        codex_ready=codex_ready,
        codex_total=len(live_codex),
        active_sessions=active_sessions,
        badges=badges,
        coins_earned=coins_earned,
        coins_available=coins_available,
        shop_items=shop_items,
        equipped_items=tuple(sorted(equipped_slugs)),
    )


def companion_from_snapshot(snapshot: Any) -> CompanionState:
    """Build :class:`CompanionState` purely from a :class:`UiSnapshot`."""
    cost = getattr(snapshot, "cost", None)
    tokens_today = 0
    tokens_30d = 0
    usd_today = 0.0
    usd_30d = 0.0
    distinct_models = 0
    if cost is not None and not getattr(cost, "is_partial", False):
        today_w = getattr(cost, "today", None)
        last30_w = getattr(cost, "last_30d", None)
        if today_w is not None:
            tokens_today = int(getattr(today_w, "total_tokens", 0) or 0)
            usd_today = float(getattr(today_w, "usd", 0.0) or 0.0)
        if last30_w is not None:
            tokens_30d = int(getattr(last30_w, "total_tokens", 0) or 0)
            usd_30d = float(getattr(last30_w, "usd", 0.0) or 0.0)
        by_model = getattr(cost, "by_model", ()) or ()
        distinct_models = len(by_model)

    accounts = getattr(snapshot, "accounts", ()) or ()
    quota_rows = getattr(snapshot, "quota_rows", ()) or ()
    notes = dict(getattr(snapshot, "account_notes", {}) or {})
    sessions = getattr(snapshot, "sessions", ()) or ()
    recent = getattr(snapshot, "recent_switches", ()) or ()
    threshold = float(getattr(snapshot, "autoswitch_threshold_pct", 85.0) or 85.0)

    return compute_companion(
        tokens_today=tokens_today,
        tokens_30d=tokens_30d,
        usd_today=usd_today,
        usd_30d=usd_30d,
        accounts=accounts,
        quota_rows=quota_rows,
        account_notes=notes,
        active_sessions=len(sessions),
        recent_switches_count=len(recent),
        distinct_models_count=distinct_models,
        threshold_pct=threshold,
    )


def render_sprite_svg(
    stage: EvolutionStage,
    *,
    size: int = 84,
    equipped: Sequence[str] = (),
) -> str:
    """Render *stage*'s 14x14 pixel-art sprite as a crisp inline SVG (with equipped cosmetics)."""
    b_r, b_g, b_b = stage.body_rgb
    a_r, a_g, a_b = stage.accent_rgb
    c_r, c_g, c_b = stage.crest_rgb
    s_r, s_g, s_b = stage.spark_rgb
    palette = {
        "O": "#18181b",
        "B": f"rgb({b_r},{b_g},{b_b})",
        "A": f"rgb({a_r},{a_g},{a_b})",
        "C": f"rgb({c_r},{c_g},{c_b})",
        "E": "#ffffff",
        "S": f"rgb({s_r},{s_g},{s_b})",
    }
    eq = set(equipped or ())
    rects: list[str] = []
    if "cyber_halo" in eq:
        for hx, hy in ((1, 1), (12, 1), (0, 6), (13, 6), (1, 12), (12, 12)):
            rects.append(f'<rect x="{hx}" y="{hy}" width="1" height="1" fill="#38bdf8"/>')
    for y, row in enumerate(stage.sprite):
        for x, ch in enumerate(row):
            color = palette.get(ch)
            if not color:
                continue
            rects.append(f'<rect x="{x}" y="{y}" width="1" height="1" fill="{color}"/>')
            if ch == "E" and "deal_shades" in eq:
                rects.append(f'<rect x="{x}" y="{y}" width="1" height="1" fill="#06b6d4"/>')
    if "sovereign_crown" in eq:
        for cx, cy in ((5, 0), (7, 0), (9, 0), (5, 1), (6, 1), (7, 1), (8, 1), (9, 1)):
            rects.append(f'<rect x="{cx}" y="{cy}" width="1" height="1" fill="#facc15"/>')
    return (
        f'<svg class="belkins-sprite" width="{size}" height="{size}" '
        f'viewBox="0 0 14 14" shape-rendering="crispEdges" '
        f'role="img" aria-label="{stage.name} sprite">'
        + "".join(rects)
        + "</svg>"
    )
