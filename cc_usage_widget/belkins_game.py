"""Belkins AI Companions, 8-Stage Pokedex, Boss Raid Engine, and 12-Item Poké Mart.

Inspired by PokeTokenBar, tailored for Belkins Usage Bar (`bswap` / `belkins`).
Pure and non-throwing: every computation derives deterministically from the
live :class:`UiSnapshot` or dashboard cells without blocking the AppKit thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Sequence

from .contracts import AccountRow, VENDOR_CLAUDE, VENDOR_CODEX, format_tokens, format_usd


# ---------------------------------------------------------------------------
# 8-Stage Evolution Pokedex ("Belkins Dex")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvolutionStage:
    """One evolution stage in the 8-stage Belkins Companion lineage."""

    stage: int
    slug: str
    name: str
    title: str
    min_level: int
    lore: str
    passive: str
    base_atk: int
    # (R, G, B) in 0..255 for primary body, secondary accent, crest/crown, eye/spark
    body_rgb: tuple[int, int, int]
    accent_rgb: tuple[int, int, int]
    crest_rgb: tuple[int, int, int]
    spark_rgb: tuple[int, int, int]
    sprite: tuple[str, ...]


# 14x14 pixel-art matrices:
# '.' = transparent, 'O' = dark outline, 'B' = primary body,
# 'A' = inner accent/belly, 'C' = crest/horn/crown/flame,
# 'E' = glowing eye, 'S' = ambient spark/energy particle
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
    "....OBBBBO....",
    "...OBO..OBO...",
    "..SOOC..COOS..",
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
    ".OO.OBBBBO.OO.",
    "....OBOOBO....",
    "...OOC..COO...",
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
    ".OO.OBBBBO.OO.",
    "...OBO..OBO...",
    "..OCOO..OOCO..",
    "..............",
)

_SPRITE_CONTEXTON: Final[tuple[str, ...]] = (
    "...S.CCCC.S...",
    "....CCCCCC....",
    "..OOOOOOOOOO..",
    ".OBBCCCCCCBBO.",
    "OBBBOOOOOOBBBO",
    "OBBEEBBBBEEBBO",
    "OBBBBCCCCBBBBO",
    ".OBBAAAAAAABO.",
    ".OBBAAAAAAABO.",
    "..OBBBBBBBBO..",
    ".OO.OBBBBO.OO.",
    "OCO.OBOOBO.OCO",
    "...OOC..COO...",
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
    ".OCOO.OO.OOCO.",
    "..OBO....OBO..",
    "..OCO....OCO..",
)

_SPRITE_CHRONOFLARE: Final[tuple[str, ...]] = (
    "S...CCCCCC...S",
    ".CC.CCCCCC.CC.",
    "CCCOOOOOOOACCC",
    "COOBBBBBBBBOOC",
    ".OBBCCCCCCBBO.",
    "SOBBEEBBEEBBOS",
    "COBBBBBBBBBBOC",
    "COOBBAAAAABOOC",
    ".OOBBAAAAABOO.",
    "..OBBBBBBBBO..",
    "..OO.OBBO.OO..",
    ".OCO.OBBO.OCO.",
    "S...OCO..OCO.S",
    "..............",
)

_SPRITE_BELKINS_PRIME: Final[tuple[str, ...]] = (
    ".S..C.CC.C..S.",
    "..CCOOOOOOCC..",
    ".COOBBBBBBOOC.",
    "COOBBCCCCBBOOC",
    "OBBBOOOOOOBBBO",
    "OBBEEEOOEEEBBO",
    "SOBBBBBBBBBBOS",
    "COOBCCCCCCBOOC",
    "COOBBAAAAABOOC",
    ".OOBBAAAAABOO.",
    "..OBBCCCCBBO..",
    ".OO.OBBBBO.OO.",
    ".OCO.OBBO.OCO.",
    "S..OOC..COO..S",
)

_SPRITE_OMEGA_BELKION: Final[tuple[str, ...]] = (
    "S.CC.CCCC.CC.S",
    ".CCCCCCCCCCCC.",
    "CCCOOOOOOOACCC",
    "COOBCCCCCCBOOC",
    "OBBBBOOOOBBBBO",
    "OBBEEECCEEEBBO",
    "SOBBBBBBBBBBOS",
    "COOBCCCCCCBOOC",
    "COOBAAAAAAAOBC",
    "COOBBAAAAABOOC",
    ".OOBBCCCCBBOO.",
    ".OO.OBBBBO.OO.",
    "OCO.OBOOBO.OCO",
    "S..OOC..COO..S",
)


EVOLUTION_LINE: Final[tuple[EvolutionStage, ...]] = (
    EvolutionStage(
        stage=1,
        slug="sparkbit",
        name="Sparkbit",
        title="Seedling Pulse",
        min_level=1,
        lore="A curious neural spark born in the Belkins outbound forge. Feeds on fresh context windows.",
        passive="+5% XP Gain",
        base_atk=25,
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
        min_level=8,
        lore="An agile token-prowler with twin radar ears. Tracks multi-account headroom and spots rate walls early.",
        passive="+10% Coin Drops",
        base_atk=45,
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
        min_level=18,
        lore="Dual-core striker fusing Claude Fable precision with Codex Astra velocity. Never drops a thread.",
        passive="+15% XP & +10% Crit",
        base_atk=75,
        body_rgb=(249, 115, 22),
        accent_rgb=(253, 224, 71),
        crest_rgb=(168, 85, 247),
        spark_rgb=(56, 189, 248),
        sprite=_SPRITE_PIPELINEX,
    ),
    EvolutionStage(
        stage=4,
        slug="contexton",
        name="Contexton",
        title="Memory Weaver",
        min_level=30,
        lore="Psionicweaver of 1M-token context tapestries. Compresses massive codebases without losing a byte.",
        passive="+20% Crit & +15% Coins",
        base_atk=110,
        body_rgb=(59, 130, 246),
        accent_rgb=(191, 219, 254),
        crest_rgb=(250, 204, 21),
        spark_rgb=(168, 85, 247),
        sprite=_SPRITE_CONTEXTON,
    ),
    EvolutionStage(
        stage=5,
        slug="belkinosaur",
        name="Belkinosaur",
        title="Fleet Titan",
        min_level=45,
        lore="Armored 9-slot autonomous juggernaut. Shrugs off 5-hour rate walls by rotating accounts in milliseconds.",
        passive="+25% Boss Raid DMG",
        base_atk=155,
        body_rgb=(234, 68, 32),
        accent_rgb=(167, 243, 208),
        crest_rgb=(16, 185, 129),
        spark_rgb=(250, 204, 21),
        sprite=_SPRITE_BELKINOSAUR,
    ),
    EvolutionStage(
        stage=6,
        slug="chronoflare",
        name="Chronoflare",
        title="Reset Phoenix",
        min_level=62,
        lore="Solar-winged phoenix that rises whenever a 5-hour or 7-day quota window resets to 0%.",
        passive="+25% XP & Coin Gain",
        base_atk=210,
        body_rgb=(245, 158, 11),
        accent_rgb=(254, 249, 195),
        crest_rgb=(239, 68, 68),
        spark_rgb=(56, 189, 248),
        sprite=_SPRITE_CHRONOFLARE,
    ),
    EvolutionStage(
        stage=7,
        slug="belkins-prime",
        name="Belkins Prime",
        title="Apex Sovereign",
        min_level=80,
        lore="Mythic crowned sovereign of the Belkins AI fleet. Commands billions of tokens across parallel agent swarms.",
        passive="+35% All Stats",
        base_atk=280,
        body_rgb=(255, 59, 29),
        accent_rgb=(254, 249, 195),
        crest_rgb=(250, 204, 21),
        spark_rgb=(34, 211, 238),
        sprite=_SPRITE_BELKINS_PRIME,
    ),
    EvolutionStage(
        stage=8,
        slug="omega-belkion",
        name="Omega Belkion",
        title="Astral Singularity",
        min_level=95,
        lore="Transcendent cosmic singularity forged from 100B+ tokens. Bends rate limits and spacetime to its will.",
        passive="+50% All Stats & Loot",
        base_atk=380,
        body_rgb=(139, 92, 246),
        accent_rgb=(253, 224, 71),
        crest_rgb=(34, 211, 238),
        spark_rgb=(244, 63, 94),
        sprite=_SPRITE_OMEGA_BELKION,
    ),
)


# ---------------------------------------------------------------------------
# Raid Bosses ("Belkins Boss Arena")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RaidBossSpec:
    """One Boss in the Belkins Boss Arena."""

    tier: int
    slug: str
    icon: str
    name: str
    title: str
    base_hp: int
    coin_reward: int
    xp_reward: int
    body_rgb: tuple[int, int, int]
    accent_rgb: tuple[int, int, int]
    crest_rgb: tuple[int, int, int]
    spark_rgb: tuple[int, int, int]
    sprite: tuple[str, ...]


_BOSS_SPRITE_GOLEM: Final[tuple[str, ...]] = (
    "...OOOOOOOO...",
    "..OCCCCCCCCO..",
    ".OBBBBBBBBBBO.",
    "OBBBOOOOOOBBBO",
    "OBBEEBBBBEEBBO",
    "OBBBBBBBBBBBBO",
    "OOBBAAAAABBBOO",
    "OOBBAAAAABBBOO",
    "OBBBBBBBBBBBBO",
    ".OOBBBBBBBBOO.",
    "..OO.OOOO.OO..",
    ".OOO......OOO.",
    ".OOO......OOO.",
    "..............",
)

_BOSS_SPRITE_HYDRA: Final[tuple[str, ...]] = (
    ".CC...CC...CC.",
    "COOC.COOC.COOC",
    "OEEO.OEEO.OEEO",
    ".OO..OOOO..OO.",
    "..OOOOOOOOOO..",
    ".OBBBBBBBBBBO.",
    "OBBBCCCCCCBBBO",
    "OBBBAAAAABBBBO",
    ".OBBAAAAABBBO.",
    "..OBBBBBBBBO..",
    ".OO.OOOOOO.OO.",
    "OO..OO..OO..OO",
    "..............",
    "..............",
)

_BOSS_SPRITE_SINGULARITY: Final[tuple[str, ...]] = (
    "S..CCCCCCCC..S",
    ".CCOOOOOOOOCC.",
    "COOBBBBBBBBOOC",
    "OBBCCCCCCCCBBO",
    "OBBEECCCCEEBBO",
    "OBBCCEEEECCBBO",
    "OBBCCCCCCCCBBO",
    "COOBBAAAAABOOC",
    ".OOBBAAAAABOO.",
    "..OBBBBBBBBO..",
    ".OO.OOOOOO.OO.",
    "OO..OO..OO..OO",
    "S...OO..OO...S",
    "..............",
)

RAID_BOSSES: Final[tuple[RaidBossSpec, ...]] = (
    RaidBossSpec(
        tier=1,
        slug="rate-golem",
        icon="👾",
        name="HTTP-429 Golem",
        title="Rate-Wall Sentinel",
        base_hp=600,
        coin_reward=350,
        xp_reward=45_000,
        body_rgb=(239, 68, 68),
        accent_rgb=(252, 165, 165),
        crest_rgb=(245, 158, 11),
        spark_rgb=(254, 240, 138),
        sprite=_BOSS_SPRITE_GOLEM,
    ),
    RaidBossSpec(
        tier=2,
        slug="context-hydra",
        icon="🐉",
        name="Context Leviathan",
        title="Truncation Hydra",
        base_hp=1_200,
        coin_reward=550,
        xp_reward=75_000,
        body_rgb=(16, 185, 129),
        accent_rgb=(167, 243, 208),
        crest_rgb=(244, 63, 94),
        spark_rgb=(56, 189, 248),
        sprite=_BOSS_SPRITE_HYDRA,
    ),
    RaidBossSpec(
        tier=3,
        slug="latency-lich",
        icon="👁️",
        name="Latency Archlich",
        title="Stale-Token Specter",
        base_hp=2_000,
        coin_reward=800,
        xp_reward=120_000,
        body_rgb=(168, 85, 247),
        accent_rgb=(233, 213, 255),
        crest_rgb=(34, 211, 238),
        spark_rgb=(250, 204, 21),
        sprite=_BOSS_SPRITE_SINGULARITY,
    ),
    RaidBossSpec(
        tier=4,
        slug="quota-behemoth",
        icon="🌋",
        name="Quota Behemoth",
        title="Seven-Day Colossus",
        base_hp=3_200,
        coin_reward=1_200,
        xp_reward=180_000,
        body_rgb=(217, 119, 6),
        accent_rgb=(254, 240, 138),
        crest_rgb=(239, 68, 68),
        spark_rgb=(251, 191, 36),
        sprite=_BOSS_SPRITE_GOLEM,
    ),
    RaidBossSpec(
        tier=5,
        slug="singularity-mech",
        icon="🌌",
        name="Null Singularity",
        title="Eternal Void Core",
        base_hp=5_000,
        coin_reward=1_800,
        xp_reward=260_000,
        body_rgb=(99, 102, 241),
        accent_rgb=(199, 210, 254),
        crest_rgb=(244, 63, 94),
        spark_rgb=(34, 211, 238),
        sprite=_BOSS_SPRITE_SINGULARITY,
    ),
)


@dataclass(frozen=True, slots=True)
class RaidBossState:
    """Live state of the active Raid Boss in the Belkins Arena."""

    spec: RaidBossSpec
    level: int
    hp_current: int
    hp_max: int
    hp_pct: float
    bosses_slain: int
    player_atk: int
    player_crit_pct: int


# ---------------------------------------------------------------------------
# 10 Badges / Trophies
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
    short_progress: str = ""


# ---------------------------------------------------------------------------
# 12-Item Belkins Poké Mart (2 Categories: "gear" & "style")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShopItemSpec:
    """Static definition of an item in the Belkins Mart."""

    slug: str
    icon: str
    name: str
    tab: str            # "gear" | "style"
    category: str       # "berry" | "candy" | "eyewear" | "weapon" | "headgear" | "shiny" | "pad" | "aura" | "pet" | "skin"
    price: int          # cost in SparkCoins (🪙)
    description: str


@dataclass(frozen=True, slots=True)
class ShopItemState:
    """Runtime state of a Belkins Mart item for a CompanionState."""

    slug: str
    icon: str
    name: str
    tab: str
    category: str
    price: int
    description: str
    owned: bool
    equipped: bool
    count: int = 0


SHOP_CATALOG: tuple[ShopItemSpec, ...] = (
    # --- Tab 1: "gear" (Relics, Lab Serums & Weapons) ---
    ShopItemSpec(
        slug="context_berry",
        icon="🔋",
        name="Cool-Down Cell",
        tab="gear",
        category="berry",
        price=150,
        description="Clears Wall Guard · +15% XP & +25 Boss ATK",
    ),
    ShopItemSpec(
        slug="rare_candy",
        icon="🧪",
        name="Overclock Serum",
        tab="gear",
        category="candy",
        price=250,
        description="Repeatable! Injects +60K XP & +20 Permanent ATK",
    ),
    ShopItemSpec(
        slug="deal_shades",
        icon="🕶️",
        name="Deal Closer Visor",
        tab="gear",
        category="eyewear",
        price=400,
        description="Pixel cyan visor · +20% Crit & +40 Boss ATK",
    ),
    ShopItemSpec(
        slug="plasma_blade",
        icon="⚔️",
        name="Plasma Token Blade",
        tab="gear",
        category="weapon",
        price=650,
        description="Equips glowing pixel sword · +85 Boss Raid ATK",
    ),
    ShopItemSpec(
        slug="sovereign_crown",
        icon="👑",
        name="Sovereign Crown",
        tab="gear",
        category="headgear",
        price=900,
        description="Golden pixel crown · +25% XP/Coins & +55 ATK",
    ),
    ShopItemSpec(
        slug="shiny_core",
        icon="✨",
        name="Prismatic Core",
        tab="gear",
        category="shiny",
        price=1500,
        description="Unlocks ★ PRISMATIC sprite · +50% Coins & +90 ATK",
    ),
    # --- Tab 2: "style" (Pads, Auras, Sidekick Drone & Full Console UI Skin) ---
    ShopItemSpec(
        slug="solar_pad",
        icon="🔥",
        name="Solar Forge Pad",
        tab="style",
        category="pad",
        price=450,
        description="Molten gold holopad & Solar Gold XP bar",
    ),
    ShopItemSpec(
        slug="emerald_pad",
        icon="🌿",
        name="Emerald Grove Pad",
        tab="style",
        category="pad",
        price=450,
        description="Matrix grove holopad & Emerald XP bar",
    ),
    ShopItemSpec(
        slug="nebula_pad",
        icon="🌌",
        name="Void Nebula Pad",
        tab="style",
        category="pad",
        price=600,
        description="Cosmic starfield holopad & Nebula Violet XP bar",
    ),
    ShopItemSpec(
        slug="cyber_halo",
        icon="⚡",
        name="Cyber Halo Aura",
        tab="style",
        category="aura",
        price=550,
        description="Orbital cyan lightning ring · +15% Crit & +45 ATK",
    ),
    ShopItemSpec(
        slug="mini_drone",
        icon="🤖",
        name="Mini-Drone Sidekick",
        tab="style",
        category="pet",
        price=950,
        description="Spawns pixel AI Drone bot · +70 Boss ATK",
    ),
    ShopItemSpec(
        slug="arcade_skin",
        icon="🕹️",
        name="Cyber Arcade UI Skin",
        tab="style",
        category="skin",
        price=1200,
        description="Transforms all widget cards into Gold Arcade Console",
    ),
)


def _shop_save_path() -> Any:
    import os
    from pathlib import Path

    override = os.environ.get("BELKINS_SHOP_PATH")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "cc-usage-widget" / "belkins_shop.json"


_GAME_SAVE_CACHE: dict[str, dict[str, Any]] = {}


def _clone_game_save(st: dict[str, Any]) -> dict[str, Any]:
    return {
        "owned": set(st.get("owned") or ()),
        "equipped": set(st.get("equipped") or ()),
        "candies_fed": int(st.get("candies_fed", 0) or 0),
        "boss_tier": int(st.get("boss_tier", 1) or 1),
        "boss_damage": int(st.get("boss_damage", 0) or 0),
        "bosses_slain": int(st.get("bosses_slain", 0) or 0),
        "bonus_coins": int(st.get("bonus_coins", 0) or 0),
        "bonus_xp": int(st.get("bonus_xp", 0) or 0),
        "selected_stage": int(st.get("selected_stage", 0) or 0),
    }


def load_game_save() -> dict[str, Any]:
    """Return full persistent game state from ``belkins_shop.json`` (cached in memory for zero-I/O menu rebuilds)."""
    import json

    path = _shop_save_path()
    key = str(path)
    cached = _GAME_SAVE_CACHE.get(key)
    if cached is not None:
        return _clone_game_save(cached)

    default: dict[str, Any] = {
        "owned": set(),
        "equipped": set(),
        "candies_fed": 0,
        "boss_tier": 1,
        "boss_damage": 0,
        "bosses_slain": 0,
        "bonus_coins": 0,
        "bonus_xp": 0,
        "selected_stage": 0,
    }
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                valid = {it.slug for it in SHOP_CATALOG}
                owned = {str(s) for s in (data.get("owned") or ()) if str(s) in valid}
                equipped = {str(s) for s in (data.get("equipped") or ()) if str(s) in owned}
                parsed = {
                    "owned": owned,
                    "equipped": equipped,
                    "candies_fed": max(0, int(data.get("candies_fed", 0) or 0)),
                    "boss_tier": max(1, int(data.get("boss_tier", 1) or 1)),
                    "boss_damage": max(0, int(data.get("boss_damage", 0) or 0)),
                    "bosses_slain": max(0, int(data.get("bosses_slain", 0) or 0)),
                    "bonus_coins": int(data.get("bonus_coins", 0) or 0),
                    "bonus_xp": max(0, int(data.get("bonus_xp", 0) or 0)),
                    "selected_stage": max(0, int(data.get("selected_stage", 0) or 0)),
                }
                _GAME_SAVE_CACHE[key] = _clone_game_save(parsed)
                return parsed
    except Exception:
        pass
    _GAME_SAVE_CACHE[key] = _clone_game_save(default)
    return default


def save_game_state(state: dict[str, Any]) -> bool:
    """Persist full game state atomically to ``belkins_shop.json``."""
    import json

    path = _shop_save_path()
    key = str(path)
    owned = set(state.get("owned") or ())
    equipped = set(state.get("equipped") or ()) & owned
    clean = {
        "owned": owned,
        "equipped": equipped,
        "candies_fed": max(0, int(state.get("candies_fed", 0) or 0)),
        "boss_tier": max(1, int(state.get("boss_tier", 1) or 1)),
        "boss_damage": max(0, int(state.get("boss_damage", 0) or 0)),
        "bosses_slain": max(0, int(state.get("bosses_slain", 0) or 0)),
        "bonus_coins": int(state.get("bonus_coins", 0) or 0),
        "bonus_xp": max(0, int(state.get("bonus_xp", 0) or 0)),
        "selected_stage": max(0, int(state.get("selected_stage", 0) or 0)),
    }
    _GAME_SAVE_CACHE[key] = _clone_game_save(clean)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {
            **clean,
            "owned": sorted(owned),
            "equipped": sorted(equipped),
        }
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:
        return False


def load_shop_save() -> tuple[set[str], set[str]]:
    """Return ``(owned_slugs, equipped_slugs)`` from persistent storage."""
    st = load_game_save()
    return st["owned"], st["equipped"]


def save_shop_state(owned: set[str], equipped: set[str]) -> bool:
    """Persist ``(owned, equipped)`` while preserving other game fields."""
    st = load_game_save()
    st["owned"] = set(owned)
    st["equipped"] = set(equipped) & set(owned)
    return save_game_state(st)


def _spent_coins(state: dict[str, Any]) -> int:
    owned = state["owned"]
    candies = int(state.get("candies_fed", 0) or 0)
    candy_price = next((it.price for it in SHOP_CATALOG if it.slug == "rare_candy"), 250)
    gear_spent = sum(it.price for it in SHOP_CATALOG if it.slug in owned and it.slug != "rare_candy")
    return gear_spent + candies * candy_price


def toggle_or_buy_shop_item(slug: str, coins_earned: int) -> tuple[bool, str]:
    """Buy *slug* (if unowned and affordable), feed a repeatable candy, or toggle equip."""
    spec = next((it for it in SHOP_CATALOG if it.slug == slug), None)
    if spec is None:
        return False, f"Unknown item: {slug}"

    st = load_game_save()
    owned: set[str] = st["owned"]
    equipped: set[str] = st["equipped"]
    spent = _spent_coins(st)
    available = max(0, int(coins_earned) + int(st.get("bonus_coins", 0)) - spent)

    # Special repeatable item: Overclock Serum!
    if slug == "rare_candy":
        if available < spec.price:
            need = spec.price - available
            return False, f"Need {need:,} more 🪙 for Overclock Serum (costs {spec.price} 🪙)."
        st["candies_fed"] = int(st.get("candies_fed", 0)) + 1
        st["bonus_xp"] = int(st.get("bonus_xp", 0)) + 60_000
        owned.add("rare_candy")
        equipped.add("rare_candy")
        save_game_state(st)
        return True, f"🧪 Injected Overclock Serum #{st['candies_fed']}! +60,000 XP & +20 Permanent ATK!"

    if slug in owned:
        if slug in equipped:
            equipped.discard(slug)
            save_game_state(st)
            return True, f"Unequipped {spec.icon} {spec.name}."
        if spec.category in {"pad", "headgear", "eyewear", "weapon", "skin"}:
            for other in SHOP_CATALOG:
                if other.category == spec.category and other.slug != slug:
                    equipped.discard(other.slug)
        equipped.add(slug)
        save_game_state(st)
        return True, f"Equipped {spec.icon} {spec.name}! ({spec.description})"

    if available < spec.price:
        need = spec.price - available
        return False, f"Need {need:,} more SparkCoins (costs {spec.price:,} 🪙, have {available:,} 🪙)."

    owned.add(slug)
    if spec.category in {"pad", "headgear", "eyewear", "weapon", "skin"}:
        for other in SHOP_CATALOG:
            if other.category == spec.category and other.slug != slug:
                equipped.discard(other.slug)
    equipped.add(slug)
    save_game_state(st)
    return True, f"🎉 Bought & equipped {spec.icon} {spec.name} (-{spec.price} 🪙)!"


def select_companion_stage(stage_num: int, max_unlocked_level: int = 99) -> tuple[bool, str]:
    """Morph the active Companion to *stage_num* (1..8), or 0 to auto-follow highest stage."""
    st = load_game_save()
    if stage_num <= 0:
        st["selected_stage"] = 0
        save_game_state(st)
        return True, "⚡ Companion set to AUTO (highest evolved form)."
    target = next((s for s in EVOLUTION_LINE if s.stage == stage_num), None)
    if target is None:
        return False, f"Unknown stage #{stage_num}."
    if max_unlocked_level < target.min_level:
        return False, f"🔒 {target.name} unlocks at Lv.{target.min_level} (current: Lv.{max_unlocked_level})."
    if int(st.get("selected_stage", 0)) == stage_num:
        st["selected_stage"] = 0
        save_game_state(st)
        return True, f"Switched back from {target.name} to AUTO evolution."
    st["selected_stage"] = stage_num
    save_game_state(st)
    return True, f"🐾 Morphed active Companion to #{target.stage} {target.name} ({target.passive})!"


def strike_raid_boss(companion: CompanionState) -> tuple[bool, str]:
    """Execute one RPG attack against the current Raid Boss using the Companion's ATK & Crit."""
    import time

    st = load_game_save()
    boss = companion.boss
    atk = max(25, boss.player_atk)
    crit_pct = boss.player_crit_pct
    # Deterministic-feel pseudo-random crit based on current timestamp + damage state
    seed = int(time.time() * 1000) ^ ( int(st.get("boss_damage", 0)) * 31 + int(st.get("bosses_slain", 0)) * 97 )
    is_crit = (seed % 100) < crit_pct
    dmg = int(atk * (1.75 if is_crit else 1.0))

    new_dmg = int(st.get("boss_damage", 0)) + dmg
    if new_dmg >= boss.hp_max:
        # Boss Slain!
        st["bosses_slain"] = int(st.get("bosses_slain", 0)) + 1
        st["boss_tier"] = int(st.get("boss_tier", 1)) + 1
        st["boss_damage"] = 0
        coin_mult = 1.50 if "shiny_core" in st["equipped"] else 1.0
        if "sovereign_crown" in st["equipped"]:
            coin_mult += 0.25
        reward_coins = int(boss.spec.coin_reward * coin_mult)
        reward_xp = int(boss.spec.xp_reward)
        st["bonus_coins"] = int(st.get("bonus_coins", 0)) + reward_coins
        st["bonus_xp"] = int(st.get("bonus_xp", 0)) + reward_xp
        save_game_state(st)
        return (
            True,
            f"🏆 SLAIN {boss.spec.icon} {boss.spec.name}! Looted +{reward_coins:,} 🪙 & +{reward_xp:,} XP!",
        )

    st["boss_damage"] = new_dmg
    hit_coins = 25 if is_crit else 12
    st["bonus_coins"] = int(st.get("bonus_coins", 0)) + hit_coins
    st["bonus_xp"] = int(st.get("bonus_xp", 0)) + (4_500 if is_crit else 2_000)
    save_game_state(st)
    rem = max(0, boss.hp_max - new_dmg)
    prefix = "💥 CRIT STRIKE!" if is_crit else "⚔️ Hit!"
    return True, f"{prefix} -{dmg} HP to {boss.spec.name} ({rem}/{boss.hp_max} HP left) · +{hit_coins} 🪙"


# ---------------------------------------------------------------------------
# Companion Snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompanionState:
    """Complete gamified state rendered by the Belkins theme, CLI, and Dashboard."""

    stage: EvolutionStage
    highest_stage: EvolutionStage
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
    boss: RaidBossState
    coins_earned: int = 300
    coins_available: int = 300
    candies_fed: int = 0
    selected_stage: int = 0
    shop_items: tuple[ShopItemState, ...] = ()
    equipped_items: tuple[str, ...] = ()

    @property
    def unlocked_badges_count(self) -> int:
        return sum(1 for b in self.badges if b.unlocked)


# ---------------------------------------------------------------------------
# Pure XP, Economy & Level Math
# ---------------------------------------------------------------------------


def xp_for_level(level: int) -> int:
    """Cumulative XP required to reach *level* (Lv 1 = 0 XP, Lv 80 = ~4.2M XP, Lv 95 = ~6.2M XP)."""
    if level <= 1:
        return 0
    return int(480 * ((level - 1) ** 2.08))


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
    """Compute the full :class:`CompanionState` from raw usage, game save, and fleet metrics."""
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

    tok_30d = max(0, int(tokens_30d), int(tokens_today))
    tok_today = max(0, int(tokens_today))
    spend_30d = max(0.0, float(usd_30d), float(usd_today))
    spend_today = max(0.0, float(usd_today))

    game_st = load_game_save()
    owned_slugs: set[str] = game_st["owned"]
    equipped_slugs: set[str] = game_st["equipped"]
    candies_fed = int(game_st.get("candies_fed", 0) or 0)
    bonus_xp = int(game_st.get("bonus_xp", 0) or 0)
    bonus_coins = int(game_st.get("bonus_coins", 0) or 0)
    bosses_slain = int(game_st.get("bosses_slain", 0) or 0)
    boss_tier = max(1, int(game_st.get("boss_tier", 1) or 1))
    boss_damage = max(0, int(game_st.get("boss_damage", 0) or 0))
    selected_stage_num = int(game_st.get("selected_stage", 0) or 0)

    xp_mult = 1.0
    if "sovereign_crown" in equipped_slugs:
        xp_mult += 0.25
    if "context_berry" in equipped_slugs:
        xp_mult += 0.15

    xp_from_tokens = int((tok_30d / 1_000_000.0) * 18.0)
    xp_from_spend = int(spend_30d * 28.0)
    xp_from_fleet = len(claude_slots) * 350 + len(live_codex) * 400 + recent_switches_count * 250
    xp_total = int((max(150, xp_from_tokens + xp_from_spend + xp_from_fleet) + bonus_xp) * xp_mult)
    xp_today = int(((tok_today / 1_000_000.0) * 18.0 + spend_today * 28.0) * xp_mult)

    level, xp_into, xp_band, xp_frac = level_from_xp(xp_total)
    highest_stage, next_stage = stage_for_level(level)

    # Allow morphing into any unlocked stage from the Pokedex
    stage = highest_stage
    if selected_stage_num > 0:
        chosen = next((s for s in EVOLUTION_LINE if s.stage == selected_stage_num), None)
        if chosen is not None and level >= chosen.min_level:
            stage = chosen

    # Mood determination
    if "context_berry" in equipped_slugs:
        mood_key = "overdrive"
        mood_label = "🔋 Cell Overdrive"
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

    # 14 Belkins Trophy Badges (from Core Milestones to Hard-to-Achieve Endgame Trials)
    healthy_claude = sum(1 for r in claude_enabled if r.slot not in notes)
    healthy_total = healthy_claude + codex_ready
    triad_eq = sum(1 for k in ("shiny_core", "sovereign_crown", "plasma_blade") if k in equipped_slugs)

    badges = (
        BadgeState(
            slug="first_spark",
            icon="⚡",
            name="First Spark",
            description="Process 10M+ tokens in a single day.",
            unlocked=tok_today >= 10_000_000 or tok_30d >= 10_000_000,
            progress_text=f"{format_tokens(max(tok_today, min(tok_30d, 10_000_000)))} / 10M tok",
            short_progress=f"{format_tokens(tok_today)}/10M",
        ),
        BadgeState(
            slug="kilowatt_day",
            icon="🔥",
            name="Kilowatt Day",
            description="Burn 1,500 USD notional spend or 2.5B tokens in a single day.",
            unlocked=spend_today >= 1_500.0 or tok_today >= 2_500_000_000,
            progress_text=f"${spend_today:,.0f} / $1.5K today ({format_tokens(tok_today)} / 2.5B)",
            short_progress=f"${spend_today:,.0f}/$1.5K",
        ),
        BadgeState(
            slug="fleet_admiral",
            icon="🎖️",
            name="Fleet Admiral",
            description="Maintain 8+ healthy, logged-in accounts across Claude & Codex with 0 errors.",
            unlocked=healthy_total >= 8,
            progress_text=f"{healthy_total} / 8 healthy accounts",
            short_progress=f"{healthy_total}/8 ok",
        ),
        BadgeState(
            slug="zero_wall",
            icon="🛡️",
            name="Flawless Rotator",
            description="Log 15+ account rotations with 0 Claude accounts stuck in re-login.",
            unlocked=recent_switches_count >= 15 and len(notes) == 0,
            progress_text=f"{recent_switches_count}/15 rotations · {len(notes)} alerts",
            short_progress=f"{recent_switches_count}/15" if len(notes) == 0 else f"{len(notes)} err",
        ),
        BadgeState(
            slug="hundred_billion",
            icon="💎",
            name="100B Token Club",
            description="Surpass 100B tokens in the 30-day window.",
            unlocked=tok_30d >= 100_000_000_000,
            progress_text=f"{format_tokens(tok_30d)} / 100B tok",
            short_progress=f"{format_tokens(tok_30d)}/100B",
        ),
        BadgeState(
            slug="quarter_trillion",
            icon="🌌",
            name="Quarter-Trillion",
            description="Surpass 250B tokens or 100,000 USD notional spend in 30 days.",
            unlocked=tok_30d >= 250_000_000_000 or spend_30d >= 100_000.0,
            progress_text=f"{format_tokens(tok_30d)} / 250B tok ({spend_30d:,.0f} / 100K USD)",
            short_progress=f"{format_tokens(tok_30d)}/250B",
        ),
        BadgeState(
            slug="swarm_overlord",
            icon="🐝",
            name="Swarm Overlord",
            description="Run 6+ distinct AI models across Claude & Codex in 30 days.",
            unlocked=distinct_models_count >= 6,
            progress_text=f"{distinct_models_count} / 6 models active",
            short_progress=f"{distinct_models_count}/6 mdl",
        ),
        BadgeState(
            slug="leviathan_hunter",
            icon="🐉",
            name="Leviathan Hunter",
            description="Slay 10+ Raid Bosses in the Belkins Boss Arena.",
            unlocked=bosses_slain >= 10,
            progress_text=f"{bosses_slain} / 10 bosses slain",
            short_progress=f"{bosses_slain}/10 💀",
        ),
        BadgeState(
            slug="godslayer",
            icon="☠️",
            name="Raid Godslayer",
            description="Slay 50+ Raid Bosses (Prestige Cycle 10+ in the Boss Arena).",
            unlocked=bosses_slain >= 50,
            progress_text=f"{bosses_slain} / 50 bosses slain",
            short_progress=f"{bosses_slain}/50 💀",
        ),
        BadgeState(
            slug="candy_alchemist",
            icon="🧪",
            name="Serum Alchemist",
            description="Inject 25+ Overclock Serums into your Companion in the Spark Vault.",
            unlocked=candies_fed >= 25,
            progress_text=f"{candies_fed} / 25 serums injected",
            short_progress=f"{candies_fed}/25 🧪",
        ),
        BadgeState(
            slug="mart_tycoon",
            icon="⚡",
            name="Vault Tycoon",
            description="Own all 12/12 relics and cyber-mods in the Belkins Spark Vault.",
            unlocked=len(owned_slugs) >= len(SHOP_CATALOG),
            progress_text=f"{len(owned_slugs)} / {len(SHOP_CATALOG)} relics owned",
            short_progress=f"{len(owned_slugs)}/{len(SHOP_CATALOG)}",
        ),
        BadgeState(
            slug="chromatic_triad",
            icon="✨",
            name="Prismatic Triad",
            description="Equip Prismatic Core, Sovereign Crown & Plasma Blade simultaneously.",
            unlocked=triad_eq >= 3,
            progress_text=f"{triad_eq} / 3 mythic relics equipped",
            short_progress=f"{triad_eq}/3 eq",
        ),
        BadgeState(
            slug="omega_ascendant",
            icon="🔮",
            name="Omega Ascendant",
            description="Evolve your Companion to Lv.95 Omega Belkion (Stage 8/8).",
            unlocked=level >= 95,
            progress_text=f"Lv.{level} / Lv.95",
            short_progress=f"Lv.{level}/95",
        ),
        BadgeState(
            slug="immortal_99",
            icon="♾️",
            name="Lv.99 Immortal",
            description="Reach the absolute maximum Level 99 (7.1M+ XP).",
            unlocked=level >= 99,
            progress_text=f"Lv.{level} / Lv.99",
            short_progress=f"Lv.{level}/99",
        ),
    )

    # Balanced RPG Coin Economy:
    # Base 500 + power-scaled token/spend coins + 250 per unlocked trophy + Boss loot
    unlocked_count = sum(1 for b in badges if b.unlocked)
    token_m = max(0.0, tok_30d / 1_000_000.0)
    token_coins = int((token_m ** 0.58) * 14.0)
    spend_coins = int((max(0.0, spend_30d) ** 0.65) * 3.2)
    coins_earned = 500 + token_coins + spend_coins + unlocked_count * 250 + bonus_coins
    spent_total = _spent_coins(game_st)
    coins_available = max(0, coins_earned - spent_total)

    # Compute Companion Battle Stats (ATK & Crit) from Stage + Level + Equipped Mart Gear
    atk = stage.base_atk + level * 2 + candies_fed * 20
    crit_pct = 15 + (10 if stage.stage >= 3 else 0) + (15 if stage.stage >= 4 else 0)
    if "context_berry" in equipped_slugs:
        atk += 25
    if "deal_shades" in equipped_slugs:
        atk += 40
        crit_pct += 20
    if "plasma_blade" in equipped_slugs:
        atk += 85
    if "sovereign_crown" in equipped_slugs:
        atk += 55
    if "shiny_core" in equipped_slugs:
        atk += 90
        crit_pct += 15
    if "cyber_halo" in equipped_slugs:
        atk += 45
        crit_pct += 15
    if "mini_drone" in equipped_slugs:
        atk += 70
    crit_pct = min(95, crit_pct)

    # Active Raid Boss state
    boss_idx = (boss_tier - 1) % len(RAID_BOSSES)
    cycle = (boss_tier - 1) // len(RAID_BOSSES)
    boss_spec = RAID_BOSSES[boss_idx]
    hp_max = int(boss_spec.base_hp * (1.0 + cycle * 0.65))
    hp_cur = max(1, hp_max - min(hp_max - 1, boss_damage))
    boss_state = RaidBossState(
        spec=boss_spec,
        level=boss_tier * 12,
        hp_current=hp_cur,
        hp_max=hp_max,
        hp_pct=(hp_cur / float(hp_max)) * 100.0,
        bosses_slain=bosses_slain,
        player_atk=atk,
        player_crit_pct=crit_pct,
    )

    shop_items = tuple(
        ShopItemState(
            slug=it.slug,
            icon=it.icon,
            name=it.name,
            tab=it.tab,
            category=it.category,
            price=it.price,
            description=it.description,
            owned=(it.slug in owned_slugs),
            equipped=(it.slug in equipped_slugs),
            count=candies_fed if it.slug == "rare_candy" else (1 if it.slug in owned_slugs else 0),
        )
        for it in SHOP_CATALOG
    )

    return CompanionState(
        stage=stage,
        highest_stage=highest_stage,
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
        boss=boss_state,
        coins_earned=coins_earned,
        coins_available=coins_available,
        candies_fed=candies_fed,
        selected_stage=selected_stage_num,
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
    eq = set(equipped or ())
    if "shiny_core" in eq:
        b_r, b_g, b_b = (250, 204, 21)
        a_r, a_g, a_b = (254, 249, 195)
        c_r, c_g, c_b = (168, 85, 247)
        s_r, s_g, s_b = (34, 211, 238)
    else:
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
    rects: list[str] = []
    if "cyber_halo" in eq:
        for hx, hy in ((1, 1), (12, 1), (0, 6), (13, 6), (1, 12), (12, 12)):
            rects.append(f'<rect x="{hx}" y="{hy}" width="1" height="1" fill="#38bdf8"/>')
    if "mini_drone" in eq:
        for dx, dy, dc in ((0, 1, "#38bdf8"), (1, 1, "#facc15"), (0, 2, "#f8fafc"), (1, 2, "#38bdf8")):
            rects.append(f'<rect x="{dx}" y="{dy}" width="1" height="1" fill="{dc}"/>')
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
    if "plasma_blade" in eq:
        for bx, by, bc in (
            (13, 3, "#22d3ee"),
            (13, 4, "#22d3ee"),
            (13, 5, "#38bdf8"),
            (13, 6, "#e879f9"),
            (12, 7, "#facc15"),
            (13, 7, "#facc15"),
            (13, 8, "#94a3b8"),
        ):
            rects.append(f'<rect x="{bx}" y="{by}" width="1" height="1" fill="{bc}"/>')
    return (
        f'<svg class="belkins-sprite" width="{size}" height="{size}" '
        f'viewBox="0 0 14 14" shape-rendering="crispEdges" '
        f'role="img" aria-label="{stage.name} sprite">'
        + "".join(rects)
        + "</svg>"
    )
