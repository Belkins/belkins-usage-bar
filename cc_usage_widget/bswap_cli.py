"""Belkins Swap (`bswap` / `belkins`) — Unified CLI for Belkins AI Fleet, Codex, 8-Stage Dex, Boss Raids & 12-Item Poké Mart.

Replaces raw ``cswap`` commands with a Belkins-branded CLI that:
- Shows your live Belkins AI Companion (8 Stages, Level, ATK, Crit, Boss Raid HP, SparkCoins, 10 Trophies)
- Includes the 12-item Belkins Poké Mart (`bswap shop`, `bswap buy <item>`, `bswap equip <item>`)
- Includes the Boss Raid Arena (`bswap raid [strikes]`) & Pokedex Morphing (`bswap morph <stage|auto>`)
- Never leaves a persistent interactive ``cswap`` TUI process running by accident (avoiding rival-actor warnings)
- Unifies Claude account switching, Codex account management, Companion Dex inspection, and menu-bar controls
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import belkins_game
from .contracts import VENDOR_CLAUDE, VENDOR_CODEX, AccountRow
from .pricing import DEFAULT_PRICING
from .rollup import DailyRollupStore

WIDGET_DIR = Path(__file__).resolve().parents[1]

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ORANGE = "\033[38;2;255;92;38m"
GOLD = "\033[38;2;250;194;46m"
GREEN = "\033[38;2;56;217;122m"
CYAN = "\033[38;2;77;184;255m"
RED = "\033[38;2;245;84;84m"
PURPLE = "\033[38;2;192;132;252m"


def _find_cswap() -> Path | None:
    local = Path.home() / ".local" / "bin" / "cswap"
    if local.is_file():
        return local
    which = shutil.which("cswap")
    return Path(which) if which else None


def _rebrand_text(text: str) -> str:
    """Replace cswap/claude-swap branding with Belkins bswap branding."""
    out = re.sub(r"\bclaude-swap\b", "Belkins Swap (bswap)", text)
    out = re.sub(r"\bcswap\b", "bswap", out)
    return out


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _load_companion_state() -> belkins_game.CompanionState:
    """Compute live CompanionState from rollups.json, game save, and account state files."""
    rollups_path = WIDGET_DIR / "rollups.json"
    tokens_30d = 0
    tokens_today = 0
    usd_30d = 0.0
    usd_today = 0.0
    models_seen: set[str] = set()
    today_str = dt.date.today().isoformat()

    if rollups_path.is_file():
        try:
            store = DailyRollupStore(path=rollups_path, keep_days=30)
            store.load()
            for rollup in store.last_n_days(30, today=today_str):
                for key, usage in (rollup.models or {}).items():
                    tok = int(usage.total_tokens)
                    if tok <= 0:
                        continue
                    models_seen.add(str(key))
                    tokens_30d += tok
                    cost = 0.0
                    try:
                        cost = float(DEFAULT_PRICING.cost_usd(str(key), usage, rollup.day))
                    except Exception:
                        cost = 0.0
                    usd_30d += cost
                    if rollup.day == today_str:
                        tokens_today += tok
                        usd_today += cost
        except Exception:
            pass

    claude_rows: list[AccountRow] = []
    for swap_state in (
        Path.home() / ".claude" / "claude-swap" / "state.json",
        Path.home() / ".claude-swap" / "state.json",
    ):
        if swap_state.is_file():
            try:
                st = json.loads(swap_state.read_text(encoding="utf-8"))
                active_slot = st.get("active_account") or st.get("active")
                for k, v in (st.get("accounts", {}) or {}).items():
                    slot_num = int(k)
                    usage = v.get("usage") or {}
                    fh = (usage.get("five_hour") or {}).get("utilization")
                    sd = (usage.get("seven_day") or {}).get("utilization")
                    claude_rows.append(
                        AccountRow(
                            slot=slot_num,
                            alias=str(v.get("alias") or f"slot-{slot_num}"),
                            email=str(v.get("email") or ""),
                            is_active=(slot_num == active_slot),
                            disabled=bool(v.get("disabled", False)),
                            five_hour_pct=float(fh) if fh is not None else None,
                            seven_day_pct=float(sd) if sd is not None else None,
                            vendor=VENDOR_CLAUDE,
                        )
                    )
                break
            except Exception:
                pass

    codex_rows: list[AccountRow] = []
    codex_dir = Path.home() / ".codex-accounts"
    if codex_dir.is_dir():
        try:
            idx = -1
            for p in sorted(codex_dir.glob("*.json")):
                if p.name.startswith(".") or p.name == "state.json":
                    continue
                codex_rows.append(
                    AccountRow(
                        slot=idx,
                        alias=p.stem,
                        email="",
                        is_active=(idx == -1),
                        vendor=VENDOR_CODEX,
                        switchable=False,
                    )
                )
                idx -= 1
        except Exception:
            pass

    return belkins_game.compute_companion(
        tokens_today=tokens_today,
        tokens_30d=tokens_30d,
        usd_today=usd_today,
        usd_30d=usd_30d,
        accounts=tuple(claude_rows),
        quota_rows=tuple(codex_rows),
        distinct_models_count=len(models_seen),
        recent_switches_count=1 if len(claude_rows) > 1 else 0,
    )


def _xp_bar(frac: float, width: int = 16, color: str = ORANGE) -> str:
    filled = max(0, min(width, int(round(frac * width))))
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def _print_banner() -> None:
    try:
        comp = _load_companion_state()
    except Exception:
        return

    st = comp.stage
    boss = comp.boss
    unlocked = [b for b in comp.badges if b.unlocked]
    badge_icons = " ".join(b.icon for b in unlocked) if unlocked else "none yet"
    eq_icons = " ".join(it.icon for it in comp.shop_items if it.equipped)
    gear_str = f"  {DIM}│{RESET}  Gear: {eq_icons}" if eq_icons else ""
    shiny_prefix = f"{GOLD}★ {RESET}" if "shiny_core" in comp.equipped_items else ""
    dollar = chr(36)

    print(
        f"{BOLD}{ORANGE}⚡ BELKINS AI FLEET & COMPANION DEX{RESET}  "
        f"{DIM}│{RESET}  {shiny_prefix}{BOLD}{st.name}{RESET} {GOLD}Lv.{comp.level}{RESET} ({st.passive})  "
        f"{DIM}│{RESET}  {RED}⚔️ {boss.player_atk} ATK ({boss.player_crit_pct}% Crit){RESET}  "
        f"{DIM}│{RESET}  {BOLD}{comp.mood_label}{RESET}  "
        f"{DIM}│{RESET}  {GOLD}🪙 {comp.coins_available:,} Coins{RESET}{gear_str}"
    )
    print(
        f"   {DIM}XP{RESET} [{_xp_bar(comp.xp_fraction)}] "
        f"{BOLD}{comp.xp_total:,}{RESET} XP (+{comp.xp_today:,}/d)  "
        f"{DIM}│{RESET}  Today: {BOLD}{_fmt_tokens(comp.tokens_today)}{RESET} tok ({dollar}{comp.usd_today:,.0f})  "
        f"{DIM}│{RESET}  Boss: {RED}{boss.spec.icon} {boss.spec.name} ({boss.hp_current:,}/{boss.hp_max:,} HP · 💀 {boss.bosses_slain}){RESET}  "
        f"{DIM}│{RESET}  🏆 {comp.unlocked_badges_count}/{len(comp.badges)}: {badge_icons}"
    )
    print()


def _cmd_dex() -> int:
    comp = _load_companion_state()
    boss = comp.boss
    _print_banner()

    print(
        f"{BOLD}{RED}━━━ ⚔️ BOSS RAID ARENA (Bosses Slain: 💀 {boss.bosses_slain} · Your ATK: {boss.player_atk} · Crit: {boss.player_crit_pct}%) ━━━{RESET}"
    )
    print(
        f"  {boss.spec.icon} {BOLD}{boss.spec.name} · Lv.{boss.level}{RESET} ({boss.spec.title})  "
        f"[{_xp_bar(boss.hp_pct / 100.0, width=18, color=RED)}] "
        f"{BOLD}{boss.hp_current:,} / {boss.hp_max:,} HP{RESET}  "
        f"{DIM}(Bounty: +{boss.spec.coin_reward} 🪙 & +{boss.spec.xp_reward:,} XP){RESET}"
    )
    print(f"  {DIM}Attack from CLI:{RESET} {ORANGE}bswap raid [strikes]{RESET}   (e.g. {ORANGE}bswap raid 3{RESET})\n")

    print(f"{BOLD}{CYAN}━━━ 📖 8-STAGE EVOLUTION POKEDEX (Current: #{comp.stage.stage} {comp.stage.name}) ━━━{RESET}")
    for st in belkins_game.EVOLUTION_LINE:
        is_current = st.stage == comp.stage.stage
        unlocked = comp.level >= st.min_level
        marker = (
            f"{ORANGE}▶ ACTIVE  {RESET}"
            if is_current
            else (f"{GREEN}✓ UNLOCKED{RESET}" if unlocked else f"{DIM}🔒 Lv.{st.min_level:<2}  {RESET}")
        )
        print(
            f"  {marker}  #{st.stage} {BOLD}{st.name:<14}{RESET} "
            f"({CYAN}{st.passive:<22}{RESET} · ATK {st.base_atk:>3}) — {DIM}{st.lore}{RESET}"
        )
    print(f"  {DIM}Morph active Companion:{RESET} {ORANGE}bswap morph <1..8|auto>{RESET}\n")

    print(f"{BOLD}{GOLD}━━━ 🏆 BELKINS TROPHY ROOM ({comp.unlocked_badges_count}/{len(comp.badges)} UNLOCKED) ━━━{RESET}")
    for b in comp.badges:
        status = f"{GREEN}✓{RESET}" if b.unlocked else f"{DIM}·{RESET}"
        name_col = f"{BOLD}{b.name:<20}{RESET}" if b.unlocked else f"{DIM}{b.name:<20}{RESET}"
        print(f"  {status} {b.icon}  {name_col} {DIM}{b.description}{RESET}  ({b.progress_text})")
    print()
    return 0


def _cmd_raid(args: list[str]) -> int:
    strikes = 1
    if args and args[0].isdigit():
        strikes = max(1, min(25, int(args[0])))
    for i in range(strikes):
        comp = _load_companion_state()
        _ok, msg = belkins_game.strike_raid_boss(comp)
        print(f"  {BOLD}[Strike {i + 1}/{strikes}]{RESET} {msg}")
    print()
    _print_banner()
    return 0


def _cmd_morph(args: list[str]) -> int:
    comp = _load_companion_state()
    if not args or args[0].lower() in {"auto", "0", "reset"}:
        _ok, msg = belkins_game.select_companion_stage(0, comp.level)
        print(f"{GREEN}✓ {msg}{RESET}\n")
        _print_banner()
        return 0
    q = args[0].strip().lower()
    st_num = 0
    if q.isdigit():
        st_num = int(q)
    else:
        for s in belkins_game.EVOLUTION_LINE:
            if q in s.slug or q in s.name.lower():
                st_num = s.stage
                break
    ok, msg = belkins_game.select_companion_stage(st_num, comp.level)
    print(f"{GREEN if ok else RED}{'✓' if ok else '✗'} {msg}{RESET}\n")
    _print_banner()
    return 0 if ok else 1


def _resolve_shop_slug(query: str) -> str | None:
    q = query.strip().lower().replace("-", "_")
    if q.isdigit():
        idx = int(q) - 1
        if 0 <= idx < len(belkins_game.SHOP_CATALOG):
            return belkins_game.SHOP_CATALOG[idx].slug
    for spec in belkins_game.SHOP_CATALOG:
        if q == spec.slug or q in spec.slug or q in spec.name.lower():
            return spec.slug
    return None


def _cmd_shop(args: list[str]) -> int:
    comp = _load_companion_state()
    if args:
        sub = args[0].lower()
        target = args[1] if len(args) > 1 and sub in {"buy", "equip", "unequip", "use", "feed"} else args[0]
        slug = _resolve_shop_slug(target)
        if not slug:
            print(f"{RED}Unknown Belkins Mart item: {target!r}{RESET}")
            print(f"Available items: {', '.join(it.slug for it in belkins_game.SHOP_CATALOG)}")
            return 1
        ok, msg = belkins_game.toggle_or_buy_shop_item(slug, comp.coins_earned)
        color = GREEN if ok else RED
        print(f"{color}{'✓' if ok else '✗'} {msg}{RESET}\n")
        comp = _load_companion_state()

    _print_banner()
    print(
        f"{BOLD}{GOLD}━━━ 🛒 BELKINS POKÉ MART (Wallet: 🪙 {comp.coins_available:,} SparkCoins) ━━━{RESET}"
    )
    print(f"  {DIM}Earn 🪙 from token burn, Trophies (+250/badge), and Boss Raids (`bswap raid`).{RESET}\n")

    for tab_key, tab_title in (
        ("gear", f"{ORANGE}⚔️ GEAR, WEAPONS & LAB UPGRADES{RESET}"),
        ("style", f"{PURPLE}🎨 WORLD PADS, PETS & CONSOLE SKINS{RESET}"),
    ):
        print(f"  {BOLD}{tab_title}")
        for idx, it in enumerate(comp.shop_items, start=1):
            if it.tab != tab_key:
                continue
            if it.slug == "rare_candy":
                tag = f"{GOLD}[FEED · 🪙 {it.price} (×{it.count})]{RESET}"
            elif it.equipped:
                tag = f"{GREEN}[EQUIPPED ✓]{RESET}"
            elif it.owned:
                tag = f"{ORANGE}[OWNED · equip]{RESET}"
            elif comp.coins_available >= it.price:
                tag = f"{GOLD}[BUY · 🪙 {it.price:>4}]{RESET}"
            else:
                tag = f"{DIM}[🔒 🪙 {it.price:>4}]{RESET}"
            print(
                f"    {BOLD}{idx:>2}.{RESET} {it.icon}  {BOLD}{it.name:<20}{RESET} {tag:<28} "
                f"{DIM}{it.description}{RESET}"
            )
        print()

    print(
        f"  {DIM}Buy / Equip / Feed:{RESET} {ORANGE}bswap buy <number|name>{RESET}  "
        f"(e.g. {ORANGE}bswap buy candy{RESET}, {ORANGE}bswap buy blade{RESET}, {ORANGE}bswap buy shiny{RESET}, {ORANGE}bswap buy arcade{RESET})"
    )
    print()
    return 0


def _cmd_bar(args: list[str]) -> int:
    action = args[0] if args else "status"
    uid = str(os.getuid())
    if action == "restart":
        res = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.cc-usage-widget"])
        if res.returncode == 0:
            print(f"{GREEN}✓ Belkins Usage Bar restarted.{RESET}")
        return res.returncode
    if action == "logs":
        log_path = WIDGET_DIR / "logs" / "widget.log"
        return subprocess.run(["tail", "-n", "40", str(log_path)]).returncode
    usagebar_bin = Path.home() / ".local" / "bin" / "usagebar"
    if usagebar_bin.is_file():
        return subprocess.run([str(usagebar_bin), action, *args[1:]]).returncode
    print(f"{RED}usagebar script not found at {usagebar_bin}{RESET}", file=sys.stderr)
    return 1


def _cmd_dashboard() -> int:
    dash_path = WIDGET_DIR / "dashboard.html"
    if dash_path.is_file():
        subprocess.run(["open", str(dash_path)])
        print(f"{GREEN}✓ Opened Belkins Companion & Usage Dashboard:{RESET} {dash_path}")
        return 0
    print(f"{RED}Dashboard file not found at {dash_path}. Click 'Open Usage Dashboard' in the menu bar first.{RESET}")
    return 1


def _cmd_codex(args: list[str]) -> int:
    cmd = [sys.executable, "-m", "cc_usage_widget.codex_accounts", *args]
    return subprocess.run(cmd, cwd=str(WIDGET_DIR)).returncode


def _cmd_help() -> int:
    _print_banner()
    print(f"{BOLD}Usage:{RESET} {ORANGE}bswap{RESET} [command]   (or {ORANGE}belkins{RESET} [command])")
    print()
    print(f"{BOLD}Belkins Companion, Boss Arena, Mart & Bar Commands:{RESET}")
    print(f"  {ORANGE}bswap{RESET} / {ORANGE}bswap status{RESET}       Show Companion HUD + Claude & Codex fleet status")
    print(f"  {ORANGE}bswap dex{RESET}                  Inspect 8-Stage Pokedex, Boss Raid Arena & 10 Trophy Badges")
    print(f"  {ORANGE}bswap raid [strikes]{RESET}       Attack the active Raid Boss for SparkCoins & Bonus XP")
    print(f"  {ORANGE}bswap morph <1..8|auto>{RESET}    Morph your active Companion into any unlocked Pokedex stage")
    print(f"  {ORANGE}bswap shop{RESET} / {ORANGE}bswap mart{RESET}    Open the 12-item Belkins Poké Mart (Gear, Skins, Pets & Candies)")
    print(f"  {ORANGE}bswap buy <item>{RESET}           Buy, equip, or feed a Mart item (e.g. candy, blade, shiny, arcade)")
    print(f"  {ORANGE}bswap dashboard{RESET}            Open the interactive Belkins HTML Dashboard in browser")
    print(f"  {ORANGE}bswap bar{RESET} [on|off|restart|status|logs]  Control the Belkins menu-bar app")
    print(f"  {ORANGE}bswap codex{RESET} [status|switch|add|relogin] Manage Codex accounts")
    print()
    print(f"{BOLD}Claude Fleet Commands:{RESET}")
    print(f"  {ORANGE}bswap <slot|alias>{RESET}         Switch active Claude account to slot number or alias")
    print(f"  {ORANGE}bswap best{RESET}                 Switch to the healthiest Claude account with most room")
    print(f"  {ORANGE}bswap list{RESET}                 List all registered Claude accounts")
    print(f"  {ORANGE}bswap add{RESET}                  Register current Claude credentials into a slot")
    print(f"  {ORANGE}bswap remove <slot>{RESET}        Remove a slot from the rotation")
    print(f"  {ORANGE}bswap enable|disable <s|a>{RESET} Enable or disable a slot for auto-switch")
    print(f"  {ORANGE}bswap tui{RESET}                  Launch interactive terminal UI (note: pauses menu-bar switch while open)")
    return 0


def _run_cswap(args: list[str], *, show_banner: bool = False) -> int:
    if show_banner:
        _print_banner()
    cswap_bin = _find_cswap()
    if cswap_bin is None:
        print(f"{RED}cswap backend not found. Install with: uv tool install claude-swap{RESET}", file=sys.stderr)
        return 1

    interactive = {"tui", "watch", "add", "run"}
    if args and args[0] in interactive:
        real_args = [] if args[0] == "tui" else args
        return subprocess.run([str(cswap_bin), *real_args]).returncode

    proc = subprocess.run(
        [str(cswap_bin), *args],
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        sys.stdout.write(_rebrand_text(proc.stdout))
    if proc.stderr:
        sys.stderr.write(_rebrand_text(proc.stderr))
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] == "status":
        rest = args[1:] if args else []
        return _run_cswap(["status", *rest], show_banner=True)

    cmd = args[0].lower()
    if cmd in {"-h", "--help", "help"}:
        return _cmd_help()
    if cmd in {"dex", "trophies", "companion", "badges", "pokedex"}:
        return _cmd_dex()
    if cmd in {"raid", "attack", "strike", "boss", "battle"}:
        return _cmd_raid(args[1:])
    if cmd in {"morph", "evolve", "stage"}:
        return _cmd_morph(args[1:])
    if cmd in {"shop", "mart", "store"}:
        return _cmd_shop(args[1:])
    if cmd in {"buy", "equip", "unequip", "feed", "use"}:
        return _cmd_shop(args)
    if cmd in {"dashboard", "dash"}:
        return _cmd_dashboard()
    if cmd in {"bar", "widget", "menubar"}:
        return _cmd_bar(args[1:])
    if cmd == "codex":
        return _cmd_codex(args[1:])

    return _run_cswap(args, show_banner=False)


if __name__ == "__main__":
    raise SystemExit(main())
