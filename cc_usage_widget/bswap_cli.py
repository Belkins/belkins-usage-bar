"""Belkins Swap (`bswap` / `belkins`) — Interactive TUI & Unified CLI for Belkins AI Squad, Cyber-Forge, Boss Raids & Spark Vault.

Replaces raw ``cswap`` commands with a Belkins-branded interactive TUI and CLI that:
- Launches an interactive arrow-key selector TUI (`bswap` with no args) with live Companion HUD, Account Switcher, Spark Vault, Boss Raid Arena, Cyber-Form Morphing, and Hall of Fame Trophies
- Shows your live Belkins AI Companion (8 Cyber-Forms, Level, ATK, Crit, Boss Raid HP, SparkCoins, 14 Trophies)
- Includes the 12-item Belkins Spark Vault (`bswap vault`, `bswap buy <item>`, `bswap equip <item>`)
- Includes the Boss Raid Arena (`bswap raid [strikes]`) & Cyber-Form Morphing (`bswap morph <stage|auto>`)
- Unifies Claude account switching, Codex account management, Companion inspection, and menu-bar controls
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    recent_events: tuple[str, ...] = ()
    autoswitch_thresh: float | None = None
    try:
        from . import accounts as _accounts_mod

        src = _accounts_mod.create_account_source()
        src.refresh(force=False)
        claude_rows = list(src.rows())
        recent_events = tuple(src.recent_events())
        autoswitch_thresh = src.autoswitch_threshold()
    except Exception:
        claude_rows = []

    if not claude_rows:
        for swap_state in (
            Path.home() / ".claude-swap-backup" / "sequence.json",
            Path.home() / ".claude" / "claude-swap" / "state.json",
            Path.home() / ".claude-swap" / "state.json",
        ):
            if swap_state.is_file():
                try:
                    st = json.loads(swap_state.read_text(encoding="utf-8"))
                    active_slot = (
                        st.get("activeAccountNumber")
                        or st.get("active_account")
                        or st.get("active")
                    )
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
                                is_active=(str(slot_num) == str(active_slot)),
                                disabled=bool(v.get("disabled", False)),
                                five_hour_pct=float(fh) if fh is not None else None,
                                seven_day_pct=float(sd) if sd is not None else None,
                                vendor=VENDOR_CLAUDE,
                            )
                        )
                    break
                except Exception:
                    pass

    active_codex_id = ""
    try:
        from .contracts import CODEX_AUTH_PATH

        if CODEX_AUTH_PATH.is_file():
            auth_obj = json.loads(CODEX_AUTH_PATH.read_text(encoding="utf-8"))
            if isinstance(auth_obj, dict) and isinstance(auth_obj.get("tokens"), dict):
                active_codex_id = str(auth_obj["tokens"].get("account_id") or "").strip()
    except Exception:
        active_codex_id = ""

    codex_rows: list[AccountRow] = []
    codex_reg_path = WIDGET_DIR / "codex_accounts.json"
    codex_snap_path = WIDGET_DIR / "codex_quota_snapshots.json"
    if codex_reg_path.is_file():
        try:
            reg_data = json.loads(codex_reg_path.read_text(encoding="utf-8"))
            snap_accounts: dict[str, object] = {}
            if codex_snap_path.is_file():
                try:
                    snap_data = json.loads(codex_snap_path.read_text(encoding="utf-8"))
                    snap_accounts = snap_data.get("accounts", {}) or {}
                except Exception:
                    snap_accounts = {}
            for i, entry in enumerate(reg_data.get("accounts", []) or []):
                if not isinstance(entry, dict) or not entry.get("enabled", True):
                    continue
                acc_id = str(entry.get("account_id") or "")
                alias = str(entry.get("alias") or acc_id[:8] or f"codex-{i + 1}")
                sd_pct: float | None = None
                fh_pct: float | None = None
                snap_entry = snap_accounts.get(acc_id)
                if isinstance(snap_entry, dict):
                    quota = snap_entry.get("quota")
                    if isinstance(quota, dict):
                        sd_obj = quota.get("seven_day")
                        if isinstance(sd_obj, dict) and sd_obj.get("used_percent") is not None:
                            sd_pct = float(sd_obj["used_percent"])
                        fh_obj = quota.get("five_hour")
                        if isinstance(fh_obj, dict) and fh_obj.get("used_percent") is not None:
                            fh_pct = float(fh_obj["used_percent"])
                is_cx_active = (acc_id == active_codex_id) if active_codex_id else (i == 0)
                codex_rows.append(
                    AccountRow(
                        slot=-(i + 1),
                        alias=alias,
                        email="",
                        is_active=is_cx_active,
                        five_hour_pct=fh_pct,
                        seven_day_pct=sd_pct,
                        vendor=VENDOR_CODEX,
                        switchable=False,
                    )
                )
        except Exception:
            pass
    if not codex_rows:
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
        recent_switches_count=len(recent_events) if recent_events else (1 if len(claude_rows) > 1 else 0),
        recent_events=recent_events,
        autoswitch_threshold=autoswitch_thresh,
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
        f"{BOLD}{ORANGE}⚡ BELKINS AI SQUAD & CYBER-FORGE{RESET}  "
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


def _build_belkins_tui_app(switcher: object, *, start: str = "dashboard", detected: str | None = None):
    """Construct the BelkinsTuiApp instance (used by _run_belkins_tui and tests)."""
    import claude_swap.tui.app as cswap_app_mod
    from claude_swap.tui.app import CswapApp
    from claude_swap.tui.dashboard import DashboardScreen, MenuEntries, WatchScreen, _BACK
    from claude_swap.tui.theme import CSWAP_DARK, CSWAP_LIGHT, Palette
    from claude_swap.tui.widgets import AccountsPanel, MenuItem
    from rich.text import Text
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.widgets import Footer, ListView, Static

    class BelkinsHudPanel(Static):
        """Top HUD inside the interactive TUI showing Companion, SparkCoins & Boss Raid HP."""

        def render(self) -> Text:
            palette = Palette.from_theme(self.app.current_theme)
            try:
                comp = _load_companion_state()
            except Exception:
                return Text("⚡ BELKINS AI SQUAD & CYBER-FORGE", style="bold #FF5C26")

            st = comp.stage
            boss = comp.boss
            eq_icons = " ".join(it.icon for it in comp.shop_items if it.equipped)
            shiny = "★ " if "shiny_core" in comp.equipped_items else ""

            t = Text()
            t.append("⚡ BELKINS CYBER-FORGE", style="bold #FF5C26")
            t.append("  │  ", style=palette.muted)
            t.append(f"{shiny}#{st.stage} {st.name} ", style="bold")
            t.append(f"Lv.{comp.level}", style="bold #FAC22E")
            t.append(f" ({st.passive})", style=palette.muted)
            t.append("  │  ", style=palette.muted)
            t.append(f"⚔️ {boss.player_atk} ATK ({boss.player_crit_pct}% Crit)", style="bold #F55454")
            t.append("  │  ", style=palette.muted)
            t.append(f"🪙 {comp.coins_available:,} SparkCoins", style="bold #FAC22E")
            if eq_icons:
                t.append("  │  ", style=palette.muted)
                t.append(f"Relics: {eq_icons}", style="bold")

            t.append("\n")
            xp_w = 12
            xp_fill = max(0, min(xp_w, int(round(comp.xp_fraction * xp_w))))
            t.append("XP ", style=palette.muted)
            t.append("━" * xp_fill, style="#FF5C26")
            t.append("─" * (xp_w - xp_fill), style=palette.track)
            t.append(f" {_fmt_tokens(comp.xp_total)} XP", style=palette.muted)

            t.append("  │  ", style=palette.muted)
            hp_w = 10
            hp_fill = max(0, min(hp_w, int(round((boss.hp_pct / 100.0) * hp_w))))
            t.append(f"Boss: {boss.spec.icon} {boss.spec.name} Lv.{boss.level} ", style="bold #F55454")
            t.append("━" * hp_fill, style="#F55454")
            t.append("─" * (hp_w - hp_fill), style=palette.track)
            t.append(f" {boss.hp_current:,}/{boss.hp_max:,} HP (💀 {boss.bosses_slain})", style=palette.muted)

            t.append("  │  ", style=palette.muted)
            t.append(f"🏆 {comp.unlocked_badges_count}/{len(comp.badges)} Trophies", style="bold #FAC22E")
            return t

    class BelkinsDashboardScreen(DashboardScreen):
        """Interactive arrow-key dashboard combining Claude Swap + Belkins Cyber-Forge."""

        BINDINGS = [
            Binding("s", "open_switch", "Switch accounts"),
            Binding("w", "app.open_watch", "Watch"),
            Binding("v", "open_vault", "Spark Vault"),
            Binding("r", "open_raid", "Boss Raid"),
            Binding("m", "open_morph", "Morph Form"),
            Binding("escape,left", "menu_back", "Back", show=False),
            Binding("q", "app.quit", "Quit"),
            Binding("g", "app.open_auto", "Auto view", show=False),
            Binding("f", "app.refresh_full", "Refresh usage", show=False),
            Binding("j", "cursor_down", show=False),
            Binding("k", "cursor_up", show=False),
        ]

        def compose(self) -> ComposeResult:
            yield BelkinsHudPanel(id="belkins-hud")
            yield AccountsPanel(id="accounts-panel")
            yield Static("", id="menu-title")
            yield ListView(id="menu")
            yield Footer()

        def _refresh_hud(self) -> None:
            try:
                self.query_one("#belkins-hud", BelkinsHudPanel).refresh(layout=True)
            except Exception:
                pass

        async def _refresh_current_menu(self, entries: MenuEntries) -> None:
            if not self._menu_stack:
                return
            title, _old = self._menu_stack[-1]
            self._menu_stack[-1] = (title, entries)
            menu = self.query_one("#menu", ListView)
            prev_idx = menu.index if menu.index is not None else 0
            await menu.clear()
            await menu.extend(
                MenuItem(label, action_id, muted=(action_id == "back"))
                for label, action_id in entries
            )
            menu.index = min(prev_idx, max(0, len(entries) - 1))
            self._refresh_hud()

        def _root_entries(self) -> MenuEntries:
            return [
                ("Switch account…", "switch"),
                ("Watch accounts", "watch"),
                ("🎁 Claim Daily Supply Crate (+150 CR & +15K XP)", "belkins-claim"),
                ("⚡ Belkins Spark Vault (Buy / Equip Relics)…", "belkins-vault"),
                ("⚔️ Boss Raid Arena (Strike Raid Boss)…", "belkins-raid"),
                ("🧬 Morph Companion Form (8 Cyber-Forms)…", "belkins-morph"),
                ("🏆 Hall of Fame (14 Trophies)…", "belkins-trophies"),
                ("🌐 Open Belkins HTML Dashboard", "belkins-dashboard"),
                ("🔄 Update Belkins Usage Bar (git pull + restart)", "belkins-update"),
                ("Auto-switch view", "auto"),
                ("Add account…", "add-menu"),
                ("Disable / enable account…", "disable-menu"),
                ("Remove account…", "remove-menu"),
                ("Theme…", "theme-menu"),
                ("Quit", "quit"),
            ]

        def _vault_entries(self) -> MenuEntries:
            comp = _load_companion_state()
            entries: MenuEntries = []
            for idx, it in enumerate(comp.shop_items, start=1):
                if it.slug == "rare_candy":
                    status = f"[INJECT · 🪙 {it.price} (×{it.count})]"
                elif it.equipped:
                    status = "[EQUIPPED ✓ · Enter to unequip]"
                elif it.owned:
                    status = "[OWNED · Enter to equip]"
                elif comp.coins_available >= it.price:
                    status = f"[BUY · 🪙 {it.price}]"
                else:
                    status = f"[🔒 Need 🪙 {it.price} · Have 🪙 {comp.coins_available}]"
                entries.append(
                    (
                        f"{idx:>2}. {it.icon} {it.name:<20} {status:<32}  {it.description}",
                        f"vault-item:{it.slug}",
                    )
                )
            entries.append(_BACK)
            return entries

        def _raid_entries(self) -> MenuEntries:
            comp = _load_companion_state()
            boss = comp.boss
            return [
                (
                    f"⚔️ Strike {boss.spec.icon} {boss.spec.name} Lv.{boss.level} (1× Attack · {boss.player_atk} ATK · {boss.hp_current:,}/{boss.hp_max:,} HP)",
                    "raid-strike:1",
                ),
                (
                    f"🔥 Combo Strike (3× Attacks · Bounty: +{boss.spec.coin_reward} 🪙 & +{boss.spec.xp_reward:,} XP on slay)",
                    "raid-strike:3",
                ),
                (
                    "⚡ All-Out Blitz (5× Attacks)",
                    "raid-strike:5",
                ),
                (
                    "💥 Hyper Barrage (10× Attacks)",
                    "raid-strike:10",
                ),
                _BACK,
            ]

        def _morph_entries(self) -> MenuEntries:
            comp = _load_companion_state()
            auto_mark = "● ACTIVE" if comp.selected_stage == 0 else "  select"
            entries: MenuEntries = [
                (f"{auto_mark:<10}  ⚡ AUTO (Highest Unlocked Stage for Lv.{comp.level})", "morph-stage:0")
            ]
            for st in belkins_game.EVOLUTION_LINE:
                is_cur = st.stage == comp.stage.stage and comp.selected_stage == st.stage
                unlocked = comp.level >= st.min_level
                if is_cur:
                    mark = "● ACTIVE"
                elif unlocked:
                    mark = "✓ UNLOCKED"
                else:
                    mark = f"🔒 Lv.{st.min_level}"
                entries.append(
                    (
                        f"{mark:<10}  #{st.stage} {st.name:<15} ({st.passive:<22} · ATK {st.base_atk:>3}) — {st.lore}",
                        f"morph-stage:{st.stage}",
                    )
                )
            entries.append(_BACK)
            return entries

        def _trophy_entries(self) -> MenuEntries:
            comp = _load_companion_state()
            entries: MenuEntries = []
            for idx, b in enumerate(comp.badges, start=1):
                mark = "✓ UNLOCKED" if b.unlocked else "🔒 LOCKED "
                entries.append(
                    (
                        f"{mark}  {b.icon} {b.name:<20} ({b.progress_text}) — {b.description}",
                        f"trophy-info:{idx - 1}",
                    )
                )
            entries.append(_BACK)
            return entries

        async def action_open_vault(self) -> None:
            while len(self._menu_stack) > 1:
                self._menu_stack.pop()
            await self._push_menu("⚡ spark vault", self._vault_entries())

        async def action_open_raid(self) -> None:
            while len(self._menu_stack) > 1:
                self._menu_stack.pop()
            await self._push_menu("⚔️ boss raid arena", self._raid_entries())

        async def action_open_morph(self) -> None:
            while len(self._menu_stack) > 1:
                self._menu_stack.pop()
            await self._push_menu("🧬 morph cyber-form", self._morph_entries())

        async def _dispatch(self, action_id: str) -> None:
            if action_id == "belkins-vault":
                await self._push_menu("⚡ spark vault", self._vault_entries())
                return
            if action_id.startswith("vault-item:"):
                slug = action_id.split(":", 1)[1]
                comp = _load_companion_state()
                ok, msg = belkins_game.toggle_or_buy_shop_item(slug, comp.coins_earned)
                self.app.notify(msg, severity="information" if ok else "warning")
                await self._refresh_current_menu(self._vault_entries())
                return
            if action_id == "belkins-raid":
                await self._push_menu("⚔️ boss raid arena", self._raid_entries())
                return
            if action_id.startswith("raid-strike:"):
                strikes = int(action_id.split(":", 1)[1])
                last_msg = ""
                for _ in range(strikes):
                    comp = _load_companion_state()
                    _ok, last_msg = belkins_game.strike_raid_boss(comp)
                prefix = f"[{strikes}× Strike] " if strikes > 1 else ""
                self.app.notify(f"{prefix}{last_msg}")
                await self._refresh_current_menu(self._raid_entries())
                return
            if action_id == "belkins-morph":
                await self._push_menu("🧬 morph cyber-form", self._morph_entries())
                return
            if action_id.startswith("morph-stage:"):
                st_num = int(action_id.split(":", 1)[1])
                comp = _load_companion_state()
                ok, msg = belkins_game.select_companion_stage(st_num, comp.level)
                self.app.notify(msg, severity="information" if ok else "warning")
                await self._refresh_current_menu(self._morph_entries())
                return
            if action_id == "belkins-trophies":
                await self._push_menu("🏆 hall of fame", self._trophy_entries())
                return
            if action_id.startswith("trophy-info:"):
                idx = int(action_id.split(":", 1)[1])
                comp = _load_companion_state()
                if 0 <= idx < len(comp.badges):
                    b = comp.badges[idx]
                    state_txt = "UNLOCKED (+250 🪙)" if b.unlocked else f"Progress: {b.progress_text}"
                    self.app.notify(f"{b.icon} {b.name}: {b.description} [{state_txt}]")
                return
            if action_id == "belkins-dashboard":
                _cmd_dashboard()
                self.app.notify("Opened Belkins HTML Dashboard in browser")
                return
            if action_id == "belkins-claim":
                ok, msg = belkins_game.claim_daily_supply_drop()
                self._refresh_hud()
                self.app.notify(msg, severity="information" if ok else "warning")
                return
            if action_id == "belkins-update":
                rc = _cmd_update([])
                self.app.notify(
                    "Belkins Usage Bar updated & restarted!" if rc == 0 else "Update failed — run `bswap update` in terminal",
                    severity="information" if rc == 0 else "warning",
                )
                return

            await super()._dispatch(action_id)

    class BelkinsTuiApp(CswapApp):
        """Belkins Swap interactive Textual application."""

        TITLE = "Belkins Swap (bswap)"
        CSS_PATH = Path(inspect.getfile(cswap_app_mod)).with_name("cswap.tcss")
        CSS = """
        #belkins-hud {
            height: auto;
            padding: 1 3;
            background: $background;
            border-bottom: solid $panel;
        }
        """

        def push_screen(self, screen, *args, **kwargs):  # type: ignore[override]
            if type(screen) is DashboardScreen:
                screen = BelkinsDashboardScreen()
            return super().push_screen(screen, *args, **kwargs)

    return BelkinsTuiApp(switcher, start=start, detected=detected)


def _run_belkins_tui(start: str = "dashboard") -> int:
    """Launch the interactive Belkins Swap arrow-key TUI built on claude_swap.tui."""
    try:
        from claude_swap.appearance import detect_terminal_background, drain_stdin
        from claude_swap.exceptions import ClaudeSwitchError
        from claude_swap.printer import error as print_error
        from claude_swap.switcher import ClaudeAccountSwitcher
    except Exception:
        return _run_cswap(["tui" if start == "dashboard" else "watch"], show_banner=True)

    try:
        switcher = ClaudeAccountSwitcher(debug=False)
    except ClaudeSwitchError as exc:
        print_error(f"Error: {exc}")
        return 1

    try:
        detected = detect_terminal_background()
    except Exception:
        detected = None
    app = _build_belkins_tui_app(switcher, start=start, detected=detected)
    try:
        drain_stdin()
    except Exception:
        pass
    app.run()
    return app.return_code or 0


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

    print(f"{BOLD}{CYAN}━━━ 🧬 8-STAGE CYBER-FORM EVOLUTION TREE (Current: #{comp.stage.stage} {comp.stage.name}) ━━━{RESET}")
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

    print(f"{BOLD}{GOLD}━━━ 🏆 BELKINS HALL OF FAME ({comp.unlocked_badges_count}/{len(comp.badges)} UNLOCKED) ━━━{RESET}")
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
    aliases = {
        "cell": "context_berry",
        "berry": "context_berry",
        "cool": "context_berry",
        "serum": "rare_candy",
        "candy": "rare_candy",
        "overclock": "rare_candy",
        "visor": "deal_shades",
        "shades": "deal_shades",
        "blade": "plasma_blade",
        "sword": "plasma_blade",
        "crown": "sovereign_crown",
        "core": "shiny_core",
        "prismatic": "shiny_core",
        "shiny": "shiny_core",
        "solar": "solar_pad",
        "emerald": "emerald_pad",
        "nebula": "nebula_pad",
        "halo": "cyber_halo",
        "drone": "mini_drone",
        "arcade": "arcade_skin",
    }
    if q in aliases:
        return aliases[q]
    for spec in belkins_game.SHOP_CATALOG:
        if q == spec.slug or q in spec.slug or q in spec.name.lower():
            return spec.slug
    return None


def _cmd_shop(args: list[str]) -> int:
    comp = _load_companion_state()
    if args:
        sub = args[0].lower()
        target = args[1] if len(args) > 1 and sub in {"buy", "equip", "unequip", "use", "feed", "inject"} else args[0]
        slug = _resolve_shop_slug(target)
        if not slug:
            print(f"{RED}Unknown Belkins Spark Vault relic: {target!r}{RESET}")
            print(f"Available items: {', '.join(it.slug for it in belkins_game.SHOP_CATALOG)}")
            return 1
        ok, msg = belkins_game.toggle_or_buy_shop_item(slug, comp.coins_earned)
        color = GREEN if ok else RED
        print(f"{color}{'✓' if ok else '✗'} {msg}{RESET}\n")
        comp = _load_companion_state()

    _print_banner()
    print(
        f"{BOLD}{GOLD}━━━ ⚡ BELKINS SPARK VAULT (Wallet: 🪙 {comp.coins_available:,} SparkCoins) ━━━{RESET}"
    )
    print(f"  {DIM}Earn 🪙 from token burn, Trophies (+250/badge), and Boss Raids (`bswap raid`).{RESET}\n")

    for tab_key, tab_title in (
        ("gear", f"{ORANGE}⚔️ RELICS, WEAPONS & LAB SERUMS{RESET}"),
        ("style", f"{PURPLE}🎨 HOLOPADS, BOTS & CONSOLE SKINS{RESET}"),
    ):
        print(f"  {BOLD}{tab_title}")
        for idx, it in enumerate(comp.shop_items, start=1):
            if it.tab != tab_key:
                continue
            if it.slug == "rare_candy":
                tag = f"{GOLD}[INJECT · 🪙 {it.price} (×{it.count})]{RESET}"
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
        f"  {DIM}Buy / Equip / Inject:{RESET} {ORANGE}bswap buy <number|name>{RESET}  "
        f"(e.g. {ORANGE}bswap buy serum{RESET}, {ORANGE}bswap buy blade{RESET}, {ORANGE}bswap buy core{RESET}, {ORANGE}bswap buy arcade{RESET})"
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
    if not args or args[0] == "status":
        args = ["list", *args[1:]]
    elif args[0] == "switch":
        if len(args) < 2:
            print(f"{RED}Usage: bswap codex switch <alias|account-id>{RESET}", file=sys.stderr)
            return 2
        target = args[1].strip().lower()
        from .codex_accounts import Registry, safe_switch_codex_auth
        from .contracts import CODEX_ACCOUNTS_DIR, CODEX_ACCOUNTS_REGISTRY_PATH, CODEX_AUTH_PATH

        registry = Registry(CODEX_ACCOUNTS_REGISTRY_PATH)
        entries = registry.entries()
        match = None
        for idx, entry in enumerate(entries):
            slot_str = str(idx + 1)
            neg_slot = f"-{idx + 1}"
            if (
                entry.alias.lower() == target
                or entry.account_id.lower() == target
                or entry.account_id.lower().startswith(target)
                or target in {slot_str, neg_slot}
            ):
                match = entry
                break
        if match is None:
            print(f"{RED}✗ No tracked Codex account matching {args[1]!r}{RESET}", file=sys.stderr)
            return 1
        ok, msg = safe_switch_codex_auth(
            match.account_id,
            accounts_dir=CODEX_ACCOUNTS_DIR,
            auth_path=CODEX_AUTH_PATH,
        )
        if not ok:
            print(f"{RED}✗ Failed to switch Codex account: {msg}{RESET}", file=sys.stderr)
            return 1
        label = match.alias or match.account_id[:8]
        print(f"{GREEN}✓ Switched active Codex account to {BOLD}{label}{RESET}{GREEN} ({match.account_id[:8]}){RESET}")
        return 0

    cmd = [sys.executable, "-m", "cc_usage_widget.codex_accounts", *args]
    return subprocess.run(cmd, cwd=str(WIDGET_DIR)).returncode


REPO_GIT_URL = "https://github.com/Belkins/belkins-usage-bar.git"
REPO_TARBALL_URL = "https://github.com/Belkins/belkins-usage-bar/archive/refs/heads/main.tar.gz"


def _cmd_update(args: list[str]) -> int:
    """Pull the latest Belkins Usage Bar from GitHub, byte-compile, and restart the menu-bar agent."""
    import tempfile

    force = "--force" in args or "-f" in args
    print(f"{BOLD}{ORANGE}⚡ Updating Belkins Usage Bar & Cyber-Forge...{RESET}")
    git_dir = WIDGET_DIR / ".git"
    updated_ok = False

    if git_dir.is_dir() and shutil.which("git"):
        remotes = subprocess.run(
            ["git", "-C", str(WIDGET_DIR), "remote"],
            capture_output=True,
            text=True,
        )
        remote_names = set((remotes.stdout or "").split())
        if "origin" not in remote_names:
            subprocess.run(
                ["git", "-C", str(WIDGET_DIR), "remote", "add", "origin", REPO_GIT_URL],
                check=False,
            )

        fetch = subprocess.run(
            ["git", "-C", str(WIDGET_DIR), "fetch", "origin", "main"],
            capture_output=True,
            text=True,
        )
        if fetch.returncode == 0:
            if force:
                pull = subprocess.run(
                    ["git", "-C", str(WIDGET_DIR), "reset", "--hard", "origin/main"],
                    capture_output=True,
                    text=True,
                )
            else:
                pull = subprocess.run(
                    ["git", "-C", str(WIDGET_DIR), "pull", "--ff-only", "origin", "main"],
                    capture_output=True,
                    text=True,
                )
                if pull.returncode != 0:
                    print(
                        f"  {GOLD}! Local git checkout has diverged or has uncommitted changes; "
                        f"skipping destructive reset (re-run with `bswap update --force` to overwrite).{RESET}"
                    )
                    return 1
            if pull.returncode == 0:
                updated_ok = True
                rev = subprocess.run(
                    ["git", "-C", str(WIDGET_DIR), "rev-parse", "--short", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                sha = (rev.stdout or "").strip()
                print(f"  {GREEN}✓ Pulled latest main ({sha}){RESET}")
        elif not force:
            print(
                f"  {RED}✗ Could not fetch from git remote `origin` "
                f"(pass `--force` to download release tarball instead).{RESET}",
                file=sys.stderr,
            )
            return 1

    if not updated_ok:
        print(f"  {DIM}Fetching latest release archive from GitHub...{RESET}")
        with tempfile.TemporaryDirectory() as tmpdir:
            tar_path = Path(tmpdir) / "belkins-usage-bar.tar.gz"
            dl = subprocess.run(
                ["curl", "-fsSL", REPO_TARBALL_URL, "-o", str(tar_path)],
                capture_output=True,
                text=True,
            )
            if dl.returncode != 0:
                print(f"{RED}✗ Could not download update from {REPO_TARBALL_URL}{RESET}", file=sys.stderr)
                return 1
            ex = subprocess.run(
                ["tar", "-xzf", str(tar_path), "-C", tmpdir],
                capture_output=True,
                text=True,
            )
            if ex.returncode != 0:
                print(f"{RED}✗ Failed to unpack update archive.{RESET}", file=sys.stderr)
                return 1
            extracted_dirs = [p for p in Path(tmpdir).iterdir() if p.is_dir()]
            if not extracted_dirs:
                print(f"{RED}✗ Archive was empty.{RESET}", file=sys.stderr)
                return 1
            src_root = extracted_dirs[0]
            for rel in ("cc_usage_widget", "tests", "install.sh", "README.md"):
                s = src_root / rel
                d = WIDGET_DIR / rel
                if s.is_dir():
                    shutil.copytree(s, d, dirs_exist_ok=True)
                elif s.is_file():
                    shutil.copy2(s, d)
            updated_ok = True
            print(f"  {GREEN}✓ Synced latest files from GitHub main archive{RESET}")

    # Byte-compile package
    subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(WIDGET_DIR / "cc_usage_widget")],
        check=False,
    )

    # Ensure optimal Preserved-Thinking autoswitch policy on update
    try:
        from claude_swap import paths as _cswap_paths

        _bdir = Path(_cswap_paths.get_backup_root())
    except Exception:
        _bdir = Path.home() / ".claude-swap-backup"
    _ensure_optimal_autoswitch_policy(_bdir)
    print(f"  {GREEN}✓ Preserved-Thinking Guard active (busy-turn hold 96% · weekly ceiling 98%){RESET}")

    # Restart launchd service
    uid = str(os.getuid())
    res = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{uid}/com.cc-usage-widget"],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        print(f"  {GREEN}✓ Belkins Usage Bar restarted and live in your macOS menu bar!{RESET}")
    else:
        print(f"  {GOLD}! Updated files; run `bswap bar restart` to reload the menu bar.{RESET}")
    return 0


_SAFE_REASON_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _backup_dir() -> Path:
    try:
        from claude_swap import paths

        return Path(paths.get_backup_root())
    except Exception:
        return Path.home() / ".claude-swap-backup"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write JSON to *path*, preserving file permissions (default 0o600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0o600
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _widget_settings_store():
    """Return a loaded SettingsStore pointing at ``WIDGET_DIR / 'settings.json'`` without seeding disk."""
    from .state import SettingsStore

    store = SettingsStore(path=WIDGET_DIR / "settings.json")
    store.load(seed_missing=False)
    return store


def _fable_guard_enabled() -> bool:
    """Return whether ``belkins_fable_guard`` is enabled in widget settings (default True)."""
    try:
        return bool(_widget_settings_store().get("belkins_fable_guard", True))
    except Exception:
        return True


def _claude_config_path(src: Any = None) -> Path:
    """Resolve ``~/.claude.json`` (or a test/switcher-configured Claude config path)."""
    try:
        backend = getattr(src, "_backend", None)
        switcher = getattr(backend, "switcher", None)
        getter = getattr(switcher, "_get_claude_config_path", None)
        if callable(getter):
            return Path(getter())
    except Exception:
        pass
    return Path.home() / ".claude.json"


def _read_stale_credit_reason(cfg_path: Path) -> tuple[bool, str]:
    """Check whether *cfg_path* carries a stale ``cachedExtraUsageDisabledReason`` key.

    Never reads, prints, or returns credentials or any other key from the file.
    """
    if not cfg_path.is_file():
        return False, ""
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return False, ""
    if not isinstance(raw, Mapping) or "cachedExtraUsageDisabledReason" not in raw:
        return False, ""
    val = raw.get("cachedExtraUsageDisabledReason")
    if isinstance(val, str) and _SAFE_REASON_RE.match(val.strip()):
        return True, val.strip()
    return True, "<set>"


def _clear_credit_cache_file(cfg_path: Path) -> bool:
    """Clear stale ``cachedExtraUsageDisabledReason`` from *cfg_path* atomically."""
    try:
        from . import accounts

        clear_fn = getattr(accounts, "_clear_stale_credit_cache", None)
        if callable(clear_fn):
            return bool(clear_fn(SimpleNamespace(_get_claude_config_path=lambda: cfg_path)))
    except Exception:
        pass
    if not cfg_path.is_file():
        return False
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "cachedExtraUsageDisabledReason" not in raw:
            return False
        raw.pop("cachedExtraUsageDisabledReason", None)
        _atomic_write_json(cfg_path, raw)
        return True
    except Exception:
        return False


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _ensure_optimal_autoswitch_policy(backup_dir: Path) -> bool:
    """Ensure claude-swap's settings.json watches all per-model limits (`model='all'`, e.g. Fable)
    with an 88% weekly/Fable buffer, 30s poll interval, and 120s cooldown so multi-agent Fable
    runs rotate proactively before hitting 100% or tripping usage-credits latches."""
    s_path = backup_dir / "settings.json"
    try:
        raw = json.loads(s_path.read_text(encoding="utf-8")) if s_path.is_file() else {}
    except Exception:
        return False
    if not isinstance(raw, dict):
        return False
    auto_raw = raw.get("autoswitch")
    if auto_raw is not None and not isinstance(auto_raw, Mapping):
        return False
    auto = dict(auto_raw or {})
    changed = False

    mdl = auto.get("model")
    if "model" not in auto or mdl is None or (isinstance(mdl, str) and not mdl.strip()):
        auto["model"] = "all"
        changed = True

    wt = _safe_float(auto.get("weeklyThreshold"))
    if wt is None or wt >= 92.0:
        auto["weeklyThreshold"] = 88.0
        changed = True

    hy = _safe_float(auto.get("hysteresisPct"))
    if hy is None:
        auto["hysteresisPct"] = 10.0
        changed = True
    elif hy < 10.0:
        auto["hysteresisPct"] = 10.0
        changed = True

    cd = _safe_float(auto.get("cooldownSeconds"))
    if cd is None or cd >= 300.0:
        auto["cooldownSeconds"] = 120.0
        changed = True

    iv = _safe_float(auto.get("intervalSeconds"))
    if iv is None or iv > 30.0:
        auto["intervalSeconds"] = 30.0
        changed = True

    if changed:
        raw["autoswitch"] = auto
        raw.setdefault("schemaVersion", 1)
        try:
            from claude_swap.settings import atomic_write_json

            atomic_write_json(s_path, raw)
            return True
        except Exception:
            try:
                _atomic_write_json(s_path, raw)
                return True
            except Exception:
                return False
    return False


def _fable_pct_and_reset(row: AccountRow) -> tuple[float | None, str | None]:
    """Return ``(fable_used_pct, fable_reset_label)`` for *row*."""
    fable_pct: float | None = None
    for name, pct in row.scoped_windows or ():
        if pct is not None and str(name).strip().lower().startswith("fable"):
            fable_pct = float(pct)
            break
    fable_reset: str | None = None
    for name, label in row.scoped_resets_at or ():
        if label and str(name).strip().lower().startswith("fable"):
            fable_reset = str(label)
            break
    if fable_reset is None:
        fable_reset = row.seven_day_resets_at
    return fable_pct, fable_reset


def _format_reset_countdown(reset_label: str | None, *, now: float | None = None) -> str:
    """Format a human-readable reset countdown alongside the raw reset clock."""
    if not reset_label:
        return "—"
    hours: float | None = None
    try:
        from . import accounts

        helper = getattr(accounts, "_hours_until_reset_label", None)
        if callable(helper):
            moment = dt.datetime.now().timestamp() if now is None else float(now)
            hours = helper(reset_label, moment)
    except Exception:
        hours = None
    if hours is None or not math.isfinite(hours) or hours <= 0:
        return str(reset_label)
    total_min = max(1, int(round(hours * 60.0)))
    days, rem_min = divmod(total_min, 24 * 60)
    hrs, mins = divmod(rem_min, 60)
    if days > 0:
        cd = f"in {days}d {hrs}h"
    elif hrs > 0:
        cd = f"in {hrs}h {mins}m" if mins else f"in {hrs}h"
    else:
        cd = f"in {mins}m"
    if str(reset_label).strip().startswith("20") and "T" in str(reset_label):
        return cd
    return f"{cd} ({reset_label})"


def _resume_command(cwd: str, session_id: str) -> str:
    """Build a shell-safe, ready-to-paste Claude Code session recovery command."""
    sid = str(session_id or "").strip()
    cwd_clean = str(cwd or "").strip()
    resume_part = f"claude --resume {shlex.quote(sid)}" if sid else "claude --resume"
    if cwd_clean:
        return f"cd {shlex.quote(cwd_clean)} && {resume_part}"
    return resume_part


def _shim_status() -> dict[str, Any]:
    claude_shim = SHIMS_DIR / "claude"
    codex_shim = SHIMS_DIR / "codex"
    c_ok = claude_shim.is_file()
    x_ok = codex_shim.is_file()
    in_path = str(SHIMS_DIR) in (os.environ.get("PATH") or "").split(":")
    return {
        "claude_installed": c_ok,
        "codex_installed": x_ok,
        "installed": c_ok and x_ok,
        "in_path": in_path,
        "shims_dir": SHIMS_DIR,
    }


def _check_launchagent_status() -> tuple[str, str]:
    """Check macOS LaunchAgent ``com.cc-usage-widget`` liveness (graceful off-macOS and in tests)."""
    if sys.platform != "darwin":
        return "skipped", "non-macOS platform"
    if os.environ.get("CC_USAGE_WIDGET_NO_REVEAL") == "1":
        return "skipped", "test/headless mode (CC_USAGE_WIDGET_NO_REVEAL=1)"
    if not shutil.which("launchctl"):
        return "unavailable", "launchctl not found"
    uid = str(os.getuid())
    try:
        proc = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/com.cc-usage-widget"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception:
        return "unavailable", "launchctl check timed out or failed"
    if proc.returncode == 0:
        out = proc.stdout or ""
        pid_m = re.search(r"\bpid\s*=\s*(\d+)", out)
        state_m = re.search(r"\bstate\s*=\s*([A-Za-z0-9_-]+)", out)
        state_txt = state_m.group(1) if state_m else "loaded"
        if pid_m:
            return "running", f"{state_txt} (PID {pid_m.group(1)})"
        return "running", state_txt
    plist = Path.home() / "Library" / "LaunchAgents" / "com.cc-usage-widget.plist"
    if plist.is_file():
        return "stopped", "plist present but agent not running — run `bswap bar restart`"
    return "stopped", "not loaded — run `install.sh` or `bswap bar on`"


def _read_autoswitch_policy(
    backup_dir: Path, src: Any = None
) -> tuple[dict[str, Any], list[str]]:
    """Inspect live ``claude-swap`` autoswitch policy and return ``(summary, warnings)``."""
    s_path = backup_dir / "settings.json"
    raw_auto: dict[str, Any] = {}
    file_exists = s_path.is_file()
    parse_error = False
    if file_exists:
        try:
            parsed = json.loads(s_path.read_text(encoding="utf-8"))
            if isinstance(parsed, Mapping) and isinstance(parsed.get("autoswitch"), Mapping):
                raw_auto = dict(parsed["autoswitch"])
            elif not isinstance(parsed, Mapping):
                parse_error = True
        except Exception:
            parse_error = True

    enabled: bool
    if isinstance(raw_auto.get("enabled"), bool):
        enabled = bool(raw_auto["enabled"])
    elif isinstance(raw_auto.get("enabled"), (int, float)):
        enabled = bool(raw_auto["enabled"])
    else:
        en_fn = getattr(src, "autoswitch_enabled", None)
        try:
            src_enabled = bool(en_fn()) if callable(en_fn) else False
        except Exception:
            src_enabled = False
        try:
            widget_enabled = bool(_widget_settings_store().get("autoswitch_enabled", False))
        except Exception:
            widget_enabled = False
        enabled = src_enabled or widget_enabled

    threshold = _safe_float(raw_auto.get("threshold"))
    eff_threshold = 90.0 if threshold is None else threshold
    weekly_raw = _safe_float(raw_auto.get("weeklyThreshold"))
    model_raw = raw_auto.get("model")
    model_str = str(model_raw).strip() if isinstance(model_raw, str) else ""
    interval_raw = _safe_float(raw_auto.get("intervalSeconds"))
    eff_interval = 60.0 if interval_raw is None else interval_raw
    cooldown_raw = _safe_float(raw_auto.get("cooldownSeconds"))
    eff_cooldown = 300.0 if cooldown_raw is None else cooldown_raw
    hysteresis_raw = _safe_float(raw_auto.get("hysteresisPct"))
    eff_hysteresis = 10.0 if hysteresis_raw is None else hysteresis_raw
    strategy = str(raw_auto.get("strategy") or "best")

    warnings: list[str] = []
    if parse_error:
        warnings.append(f"claude-swap settings.json at {s_path} is unreadable/corrupt")
    if not enabled:
        warnings.append("autoswitch.enabled is OFF — automatic Fable rotation is paused")
    if not model_str:
        warnings.append("autoswitch.model is unset — Fable/Opus/Sonnet weekly limits are NOT watched (recommend 'all')")
    if weekly_raw is None:
        warnings.append("autoswitch.weeklyThreshold is unset — recommend 88% for multi-tab Fable buffer")
    elif weekly_raw >= 92.0:
        warnings.append(
            f"autoswitch.weeklyThreshold={weekly_raw:.0f}% >= 92% — leaves too little buffer for Fable bursts (recommend 88%)"
        )
    if cooldown_raw is None or eff_cooldown >= 300.0:
        warnings.append(
            f"autoswitch.cooldownSeconds={eff_cooldown:.0f}s >= 300s — slows failover under heavy burn (recommend 120s)"
        )
    if interval_raw is None or eff_interval > 30.0:
        warnings.append(
            f"autoswitch.intervalSeconds={eff_interval:.0f}s > 30s — polls too slowly under rapid Fable burn (recommend 30s)"
        )
    if eff_hysteresis < 10.0:
        warnings.append(
            f"autoswitch.hysteresisPct={eff_hysteresis:.0f}% < 10% — risks ping-pong switching (recommend >= 10%)"
        )
    if eff_threshold >= 95.0:
        warnings.append(
            f"autoswitch.threshold={eff_threshold:.0f}% >= 95% — minimal 5h margin before wall"
        )

    summary = {
        "path": str(s_path),
        "file_exists": file_exists,
        "enabled": enabled,
        "threshold": eff_threshold,
        "weeklyThreshold": weekly_raw,
        "model": model_str or None,
        "intervalSeconds": eff_interval,
        "cooldownSeconds": eff_cooldown,
        "hysteresisPct": eff_hysteresis,
        "strategy": strategy,
    }
    return summary, warnings


def _classify_fable_slot(
    row: AccountRow,
    sentinels: Mapping[int, str],
    *,
    fable_guard: bool = True,
    weekly_threshold: float = 88.0,
) -> tuple[str, str, str]:
    """Classify a Claude slot for the Fable capability table.

    Returns ``(status_label, ansi_color, detail_text)``.
    """
    from . import accounts

    fable_pct, fable_reset = _fable_pct_and_reset(row)
    reset_txt = _format_reset_countdown(fable_reset)
    sentinel_note = sentinels.get(row.slot) or row.attention_note or ""

    if row.disabled:
        extra = f" · {sentinel_note}" if sentinel_note else ""
        if fable_pct is not None:
            free_pct = max(0.0, 100.0 - fable_pct)
            return "DISABLED", DIM, f"Fable {free_pct:.0f}% free · resets {reset_txt}{extra}"
        return "DISABLED", DIM, f"excluded from rotation{extra}"

    if sentinel_note or not row.switchable:
        reason = sentinel_note or "missing credentials/backup"
        return "RE-LOGIN NEEDED", RED, reason

    if accounts.row_lacks_fable_quota(row):
        sd_free = max(0.0, 100.0 - float(row.seven_day_pct)) if row.seven_day_pct is not None else None
        sd_str = f"7d {sd_free:.0f}% free" if sd_free is not None else "7d —"
        plan = row.plan_type or "non-Fable"
        if fable_guard:
            return (
                "NO FABLE (Team/Pro — skipped by Fable Guard)",
                GOLD,
                f"{plan} · {sd_str} · resets {_format_reset_countdown(row.seven_day_resets_at)}",
            )
        return (
            "NO FABLE (Team/Pro — Fable Guard OFF!)",
            RED,
            f"{plan} · {sd_str} · risk of usage-credits latch!",
        )

    fh = float(row.five_hour_pct or 0.0)
    sd = float(row.seven_day_pct or 0.0)
    if fable_pct is not None:
        free_pct = max(0.0, 100.0 - fable_pct)
        detail = f"Fable {free_pct:.0f}% free (5h {fh:.0f}% · 7d {sd:.0f}%) · resets {reset_txt}"
        if fable_pct >= weekly_threshold or fh >= 90.0 or sd >= weekly_threshold:
            return "FABLE LOW/CAPPED", RED if fable_pct >= 99.0 or fh >= 99.0 else GOLD, detail
        return "FABLE READY", GREEN, detail

    # Unknown/minimal row (plan_type is None and no scoped_windows reported yet)
    if row.seven_day_pct is None and row.five_hour_pct is None:
        return "FABLE READY", CYAN, "awaiting first usage poll"
    sd_free = max(0.0, 100.0 - sd)
    detail = f"7d {sd_free:.0f}% free (5h {fh:.0f}%) · resets {reset_txt}"
    if fh >= 90.0 or sd >= weekly_threshold:
        return "FABLE LOW/CAPPED", GOLD, detail
    return "FABLE READY", GREEN, detail


def _slot_is_healthy_for_session(
    row: AccountRow | None,
    sentinels: Mapping[int, str],
    *,
    max_5h: float = 90.0,
    max_7d: float = 95.0,
    fleet_rows: Iterable[AccountRow] | None = None,
    fable_guard: bool | None = None,
) -> bool:
    if row is None or row.disabled or not row.switchable:
        return False
    if row.attention_note or row.slot in sentinels:
        return False
    guard_on = _fable_guard_enabled() if fable_guard is None else bool(fable_guard)
    if guard_on:
        from . import accounts

        if accounts.row_lacks_fable_quota(row) and fleet_rows is not None:
            if accounts.fleet_has_fable_accounts(fleet_rows, sentinels=sentinels):
                return False
    fh = float(row.five_hour_pct or 0.0)
    wk = float(row.seven_day_pct or 0.0)
    scoped_max = max((float(pct) for _n, pct in (row.scoped_windows or ()) if pct is not None), default=0.0)
    return fh < max_5h and max(wk, scoped_max) < max_7d


def _cmd_fable_guard(args: list[str]) -> int:
    """Inspect or toggle ``belkins_fable_guard`` in widget settings and display seat classification."""
    from . import accounts

    action = (args[0].strip().lower() if args else "status")
    settings_path = WIDGET_DIR / "settings.json"
    store = _widget_settings_store()

    if action in {"on", "enable", "true", "1"}:
        store.set("belkins_fable_guard", True)
        if not settings_path.is_file():
            store.save(force=True)
        print(f"{GREEN}✓ Fable Guard ENABLED{RESET} {DIM}(non-Fable Team/Pro seats skipped during auto-switch, bswap best & bswap claude){RESET}")
    elif action in {"off", "disable", "false", "0"}:
        store.set("belkins_fable_guard", False)
        if not settings_path.is_file():
            store.save(force=True)
        print(f"{GOLD}! Fable Guard DISABLED{RESET} {DIM}(all healthy seats eligible, including non-Fable Team/Pro seats){RESET}")
    elif action not in {"status", "show", "check"}:
        print(f"{RED}Usage: bswap fable-guard [on|off|status]{RESET}", file=sys.stderr)
        return 2

    enabled = bool(store.get("belkins_fable_guard", True))
    row_list: list[AccountRow] = []
    sentinels: dict[int, str] = {}
    guard_status: dict[str, Any] | None = None
    burn_eta: float | None = None
    burn_rate: float | None = None
    try:
        src = accounts.create_account_source()
        src.refresh(force=False)
        row_list = [r for r in src.rows() if r.slot >= 0 and r.vendor == VENDOR_CLAUDE]
        sentinels = src.sentinels() if callable(getattr(src, "sentinels", None)) else {}
        gs_fn = getattr(src, "fable_guard_status", None)
        if callable(gs_fn):
            raw_gs = gs_fn()
            if isinstance(raw_gs, Mapping):
                guard_status = dict(raw_gs)
        eta_fn = getattr(src, "fable_burn_eta_minutes", None)
        if callable(eta_fn):
            burn_eta = _safe_float(eta_fn())
        rate_fn = getattr(src, "fable_burn_rate_pct_per_min", None)
        if callable(rate_fn):
            burn_rate = _safe_float(rate_fn())
        if guard_status:
            if burn_eta is None:
                burn_eta = _safe_float(guard_status.get("active_eta_min"))
            if burn_rate is None:
                burn_rate = _safe_float(guard_status.get("active_burn_pct_per_min"))
    except Exception:
        row_list = []

    state_badge = f"{GREEN}ON (PROTECTED){RESET}" if enabled else f"{GOLD}OFF{RESET}"
    print(f"\n  {BOLD}{ORANGE}🛡️  BELKINS FABLE GUARD:{RESET} {state_badge}")
    if burn_rate is not None and burn_rate > 0:
        eta_txt = f" (~{burn_eta:.0f}m to wall)" if burn_eta is not None and burn_eta > 0 else ""
        print(f"  {CYAN}⚡ Active Fable burn rate:{RESET} {burn_rate:.2f}%/min{eta_txt}")

    fable_rows = [r for r in row_list if accounts.row_has_fable_quota(r)]
    excluded_rows = [r for r in row_list if accounts.row_lacks_fable_quota(r)]
    other_rows = [r for r in row_list if r not in fable_rows and r not in excluded_rows]

    if not row_list:
        print(f"  {DIM}No live Claude accounts loaded (claude-swap unavailable or pool empty).{RESET}")
    else:
        print(f"\n  {BOLD}{GREEN}Protected Fable Seats ({len(fable_rows)}):{RESET}")
        if fable_rows:
            for r in fable_rows:
                f_pct, f_reset = _fable_pct_and_reset(r)
                free_s = f"{max(0.0, 100.0 - f_pct):.0f}% free" if f_pct is not None else "—"
                mark = f"{GREEN}●{RESET}" if r.is_active else " "
                flags = []
                if r.disabled:
                    flags.append("DISABLED")
                if r.slot in sentinels or r.attention_note:
                    flags.append("RE-LOGIN NEEDED")
                flag_txt = f"  {RED}[{', '.join(flags)}]{RESET}" if flags else ""
                plan = r.plan_type or "claude_max"
                print(
                    f"   {mark} #{r.slot:<2} {BOLD}{r.alias:<18}{RESET} ({plan})  "
                    f"Fable {free_s:<10} · resets {_format_reset_countdown(f_reset)}{flag_txt}"
                )
        else:
            print(f"    {DIM}(none detected — Fable Guard falls back cleanly to standard seats){RESET}")

        print(f"\n  {BOLD}{GOLD}Excluded Non-Fable Seats ({len(excluded_rows)}):{RESET}")
        if excluded_rows:
            fleet_has_fable = accounts.fleet_has_fable_accounts(row_list, sentinels=sentinels)
            for r in excluded_rows:
                mark = f"{GOLD}●{RESET}" if r.is_active else " "
                plan = r.plan_type or "non-Fable"
                sd_free = f"7d {max(0.0, 100.0 - r.seven_day_pct):.0f}% free" if r.seven_day_pct is not None else "7d —"
                if enabled and fleet_has_fable:
                    note = f"{GOLD}skipped by Fable Guard{RESET}"
                elif not enabled:
                    note = f"{RED}NOT skipped (Fable Guard OFF){RESET}"
                else:
                    note = f"{DIM}eligible (no healthy Fable seats in fleet){RESET}"
                print(f"   {mark} #{r.slot:<2} {BOLD}{r.alias:<18}{RESET} ({plan} · {sd_free})  → {note}")
        else:
            print(f"    {DIM}(none — every seat in your pool has Fable quota){RESET}")

        if other_rows:
            print(f"\n  {BOLD}{DIM}Unclassified / Awaiting Usage ({len(other_rows)}):{RESET}")
            for r in other_rows:
                print(f"     #{r.slot:<2} {r.alias:<18} ({r.plan_type or 'unknown plan'})")

    print(
        f"\n  {DIM}Toggle:{RESET} {ORANGE}bswap fable-guard on{RESET}  {DIM}│{RESET}  {ORANGE}bswap fable-guard off{RESET}\n"
    )
    return 0


def _cmd_doctor(args: list[str]) -> int:
    """End-to-end Fable 5.2 & Auto-Switch diagnostic report and `--fix` auto-healer."""
    from . import accounts, fleet

    do_fix = "--fix" in args or "fix" in args
    bdir = _backup_dir()

    src: Any = None
    source_error: str | None = None
    row_list: list[AccountRow] = []
    sentinels: dict[int, str] = {}
    active: AccountRow | None = None
    try:
        src = accounts.create_account_source()
        src.refresh(force=False)
        row_list = [r for r in src.rows() if r.slot >= 0 and r.vendor == VENDOR_CLAUDE]
        sentinels = src.sentinels() if callable(getattr(src, "sentinels", None)) else {}
        active = src.active()
        source_error = getattr(src, "last_error", None)
    except Exception as exc:
        source_error = str(exc)

    cfg_path = _claude_config_path(src)

    # Apply auto-healing fixes BEFORE final diagnostic read so the report shows healed state.
    fixes_applied: list[str] = []
    if do_fix:
        if _ensure_optimal_autoswitch_policy(bdir):
            fixes_applied.append(
                "Updated claude-swap autoswitch policy (model=all, weeklyThreshold=88%, cooldownSeconds=120s, intervalSeconds=30s, hysteresisPct=10%)"
            )
        if _clear_credit_cache_file(cfg_path):
            fixes_applied.append(
                f"Cleared stale cachedExtraUsageDisabledReason from {cfg_path.name}"
            )
        store = _widget_settings_store()
        if not bool(store.get("belkins_fable_guard", True)):
            store.set("belkins_fable_guard", True)
            fixes_applied.append("Enabled belkins_fable_guard=True in widget settings.json")

    fable_guard = _fable_guard_enabled()
    policy, policy_warnings = _read_autoswitch_policy(bdir, src)
    effective_wk = float(policy["weeklyThreshold"]) if policy["weeklyThreshold"] is not None else 88.0

    # Live engine telemetry (safe getattr fallbacks before engine branch merges)
    guard_status: dict[str, Any] | None = None
    burn_eta: float | None = None
    burn_rate: float | None = None
    if src is not None:
        try:
            gs_fn = getattr(src, "fable_guard_status", None)
            if callable(gs_fn):
                raw_gs = gs_fn()
                if isinstance(raw_gs, Mapping):
                    guard_status = dict(raw_gs)
        except Exception:
            guard_status = None
        try:
            eta_fn = getattr(src, "fable_burn_eta_minutes", None)
            if callable(eta_fn):
                burn_eta = _safe_float(eta_fn())
        except Exception:
            burn_eta = None
        try:
            rate_fn = getattr(src, "fable_burn_rate_pct_per_min", None)
            if callable(rate_fn):
                burn_rate = _safe_float(rate_fn())
        except Exception:
            burn_rate = None
        if guard_status:
            if burn_eta is None:
                burn_eta = _safe_float(guard_status.get("active_eta_min"))
            if burn_rate is None:
                burn_rate = _safe_float(guard_status.get("active_burn_pct_per_min"))

    # Collect live Claude sessions
    active_slot = active.slot if active else None
    identities = fleet.account_identities(bdir)
    try:
        snap = fleet.collect(backup_dir=bdir, active_slot=active_slot)
    except Exception:
        snap = fleet.FleetSnapshot()

    stale_credit, stale_reason = _read_stale_credit_reason(cfg_path)
    la_state, la_detail = _check_launchagent_status()
    shims = _shim_status()

    issues: list[str] = list(policy_warnings)
    if not fable_guard:
        issues.append("belkins_fable_guard is OFF — non-Fable Team/Pro seats may be selected during rotation")
    if stale_credit:
        issues.append(f"Stale cachedExtraUsageDisabledReason={stale_reason} in {cfg_path.name}")
    if la_state == "stopped":
        issues.append(f"LaunchAgent com.cc-usage-widget is stopped ({la_detail})")

    _print_banner()
    print(f"{BOLD}{ORANGE}━━━ 🩺 BELKINS FABLE & AUTO-SWITCH DOCTOR ━━━{RESET}\n")

    if do_fix:
        print(f"  {BOLD}{GREEN}Auto-Heal (--fix):{RESET}")
        if fixes_applied:
            for item in fixes_applied:
                print(f"    {GREEN}✓{RESET} {item}")
        else:
            print(f"    {GREEN}✓{RESET} Nothing needed fixing — all auto-healable settings are already optimal.")
        print()

    # Section 1: Autoswitch Policy
    print(f"  {BOLD}{CYAN}1. Auto-Switch Policy ({policy['path']}){RESET}")
    en_txt = f"{GREEN}ON{RESET}" if policy["enabled"] else f"{GOLD}OFF{RESET}"
    mdl_txt = f"{GREEN}{policy['model']}{RESET}" if policy["model"] else f"{RED}unset{RESET}"
    wk_val = policy["weeklyThreshold"]
    wk_txt = (
        f"{RED}unset{RESET}"
        if wk_val is None
        else (f"{RED}{wk_val:.0f}%{RESET}" if wk_val >= 92.0 else f"{GREEN}{wk_val:.0f}%{RESET}")
    )
    iv_val = policy["intervalSeconds"]
    iv_txt = f"{RED}{iv_val:.0f}s{RESET}" if iv_val > 30.0 else f"{GREEN}{iv_val:.0f}s{RESET}"
    cd_val = policy["cooldownSeconds"]
    cd_txt = f"{RED}{cd_val:.0f}s{RESET}" if cd_val >= 300.0 else f"{GREEN}{cd_val:.0f}s{RESET}"
    hy_val = policy["hysteresisPct"]
    hy_txt = f"{RED}{hy_val:.0f}%{RESET}" if hy_val < 10.0 else f"{GREEN}{hy_val:.0f}%{RESET}"
    fg_txt = f"{GREEN}ON{RESET}" if fable_guard else f"{RED}OFF{RESET}"

    print(
        f"     enabled={en_txt}  │  threshold={policy['threshold']:.0f}%  │  weeklyThreshold={wk_txt}  │  model={mdl_txt}"
    )
    print(
        f"     intervalSeconds={iv_txt}  │  cooldownSeconds={cd_txt}  │  hysteresisPct={hy_txt}  │  Fable Guard={fg_txt}"
    )
    if burn_rate is not None and burn_rate > 0:
        eta_s = f" (~{burn_eta:.0f}m to wall)" if burn_eta is not None and burn_eta > 0 else ""
        print(f"     {CYAN}⚡ Live Fable burn:{RESET} {burn_rate:.2f}%/min{eta_s}")
    for warn in policy_warnings:
        print(f"     {GOLD}⚠ {warn}{RESET}")
    if not fable_guard:
        print(f"     {RED}⚠ belkins_fable_guard is OFF — run `bswap doctor --fix` or `bswap fable-guard on`{RESET}")
    if not policy_warnings and fable_guard:
        print(f"     {GREEN}✓ Optimal Fable-safe auto-switch policy active{RESET}")
    print()

    # Section 2: Fleet Fable Capability Table
    print(f"  {BOLD}{CYAN}2. Fleet Fable Capability Table{RESET}")
    if not row_list:
        msg = source_error or "no Claude accounts registered or claude-swap unavailable"
        print(f"     {DIM}No live Claude accounts available ({msg}).{RESET}")
    else:
        ready_cnt = 0
        low_cnt = 0
        no_fable_cnt = 0
        for r in row_list:
            status_lbl, color, detail = _classify_fable_slot(
                r, sentinels, fable_guard=fable_guard, weekly_threshold=effective_wk
            )
            if status_lbl == "FABLE READY":
                ready_cnt += 1
            elif status_lbl == "FABLE LOW/CAPPED":
                low_cnt += 1
            elif status_lbl.startswith("NO FABLE"):
                no_fable_cnt += 1
            active_mark = f"{GREEN}●{RESET}" if r.is_active else " "
            print(
                f"    {active_mark} #{r.slot:<2} {BOLD}{r.alias:<18}{RESET} "
                f"{color}[{status_lbl}]{RESET}  {DIM}{detail}{RESET}"
            )
        print(
            f"     {DIM}Summary: {ready_cnt} FABLE READY · {low_cnt} FABLE LOW/CAPPED · {no_fable_cnt} NO FABLE{RESET}"
        )
    print()

    # Section 3: Live Claude Session Health & Shims
    print(f"  {BOLD}{CYAN}3. Live Claude Session Health & Shims{RESET}")
    latched_count = 0
    shifted_count = 0
    if not snap.sessions:
        print(f"     {DIM}No live interactive Claude Code sessions detected.{RESET}")
    else:
        rows_by_slot = {r.slot: r for r in row_list}
        for s in snap.sessions:
            is_latched = bool(getattr(s, "latched_credits", False))
            is_limited = bool(getattr(s, "hit_rate_limit", False))
            sess_id = str(getattr(s, "session_id", "") or "").strip()
            cwd_p = Path(s.cwd) if s.cwd else Path.cwd()
            short_w = cwd_p.name or s.cwd or "(unknown)"
            org_shifted = bool(getattr(s, "org_shifted", False))
            if (not s.pinned) and (not org_shifted) and s.owner_slot is None and identities and s.cwd:
                try:
                    _owner, org_shifted = fleet.detect_workspace_owner_slot(
                        cwd_p, identities, current_slot=s.slot, pid=s.pid
                    )
                except Exception:
                    org_shifted = False

            cur_row = rows_by_slot.get(s.slot) if s.slot is not None else None
            cur_lbl = f"#{s.slot} ({cur_row.alias})" if cur_row else f"#{s.slot}"
            if is_latched:
                latched_count += 1
                badge = f"{RED}⚡ LATCHED (credits){RESET}"
                issues.append(f"PID {s.pid} ({short_w}) tripped Claude Code usage-credits latch")
            elif is_limited:
                badge = f"{RED}⏱ RATE LIMITED{RESET}"
                issues.append(f"PID {s.pid} ({short_w}) hit rate limit")
            elif s.pinned:
                badge = f"{CYAN}📌 PINNED{RESET}"
            elif org_shifted:
                shifted_count += 1
                badge = f"{GOLD}⚠ ORG SHIFTED{RESET}"
                issues.append(f"PID {s.pid} ({short_w}) is org-shifted")
            else:
                badge = f"{GREEN}● HEALTHY{RESET}"

            print(f"     PID {s.pid:<6} {BOLD}{short_w:<18}{RESET} [{s.status or 'idle':<7}] {badge}  slot={cur_lbl}")
            if is_latched:
                rec_cmd = _resume_command(s.cwd, sess_id)
                print(
                    f"        {GOLD}↳ Recovery (run /exit then paste):{RESET} {ORANGE}{rec_cmd}{RESET}"
                )

    shim_badge = (
        f"{GREEN}INSTALLED{RESET}" if shims["installed"] else f"{DIM}NOT INSTALLED{RESET}"
    )
    path_badge = (
        f"{GREEN}ACTIVE on PATH{RESET}" if shims["in_path"] else f"{GOLD}not on PATH{RESET}"
    )
    print(f"     bswap shim: {shim_badge} ({path_badge})")
    if not shims["installed"]:
        print(f"       {DIM}Tip: run `bswap shim on` for automatic per-session org isolation{RESET}")
    print()

    # Section 4: Config Hygiene & LaunchAgent
    print(f"  {BOLD}{CYAN}4. Credit Cache & LaunchAgent{RESET}")
    if stale_credit:
        print(
            f"     {RED}⚠ ~/.claude.json cachedExtraUsageDisabledReason={stale_reason}{RESET} "
            f"{DIM}(run `bswap doctor --fix` to clear){RESET}"
        )
    else:
        print(f"     {GREEN}✓ ~/.claude.json cachedExtraUsageDisabledReason: clean{RESET}")

    la_color = GREEN if la_state == "running" else (DIM if la_state == "skipped" else GOLD)
    la_icon = "✓" if la_state == "running" else ("·" if la_state == "skipped" else "⚠")
    print(f"     {la_color}{la_icon} LaunchAgent (com.cc-usage-widget): {la_state} — {la_detail}{RESET}")
    print()

    # Final verdict
    if issues:
        fix_hint = "" if do_fix else f"  Run {ORANGE}bswap doctor --fix{RESET} to auto-heal policy, credit cache & Fable Guard."
        print(f"  {GOLD}⚠ Doctor found {len(issues)} item(s) needing attention.{RESET}{fix_hint}\n")
    else:
        print(f"  {GREEN}✓ All Fable 5.2 & Auto-Switch health checks passed!{RESET}\n")
    return 0


def _cmd_best(args: list[str]) -> int:
    """Switch to the healthiest Claude account honoring Fable Guard, same-org affinity & perishable quota."""
    from . import accounts, fleet

    dry_run = "--dry-run" in args or "-n" in args
    bdir = _backup_dir()
    fable_guard = _fable_guard_enabled()

    src: Any = None
    row_list: list[AccountRow] = []
    sentinels: dict[int, str] = {}
    active: AccountRow | None = None
    try:
        src = accounts.create_account_source()
        src.refresh(force=False)
        row_list = [r for r in src.rows() if r.slot >= 0 and r.vendor == VENDOR_CLAUDE]
        sentinels = src.sentinels() if callable(getattr(src, "sentinels", None)) else {}
        active = src.active()
    except Exception:
        row_list = []

    if not row_list:
        accounts.record_cli_switch_marker(bdir, "*")
        return _run_cswap(["switch", "--strategy", "best", *args], show_banner=True)

    identities = fleet.account_identities(bdir)
    active_slot = active.slot if active else None

    # Determine same-org preference: workspace thinking org first, else active seat's org.
    prefer_org: str | None = None
    if identities:
        try:
            owner_slot, _shifted = fleet.detect_workspace_owner_slot(
                Path.cwd(), identities, current_slot=active_slot
            )
            if owner_slot is not None:
                prefer_org = identities.get(owner_slot, ("", ""))[1] or None
        except Exception:
            prefer_org = None
        if not prefer_org and active_slot is not None:
            prefer_org = identities.get(active_slot, ("", ""))[1] or None

    # Parse watched per-model limits
    models: tuple[str, ...] = ("all",)
    try:
        mdl_fn = getattr(src, "autoswitch_model", None)
        raw_mdl = str(mdl_fn() or "").strip() if callable(mdl_fn) else "all"
        if raw_mdl:
            models = tuple(m.strip() for m in raw_mdl.split(",") if m.strip()) or ("all",)
        else:
            models = ()
    except Exception:
        models = ("all",)

    fleet_has_fable = accounts.fleet_has_fable_accounts(row_list, sentinels=sentinels)
    enforce_fable = fable_guard and fleet_has_fable

    candidate_pool = (
        [r for r in row_list if not accounts.row_lacks_fable_quota(r)]
        if enforce_fable
        else list(row_list)
    )
    best_row = accounts.harvest_best_slot(
        candidate_pool,
        sentinels=sentinels,
        identities=identities,
        prefer_org=prefer_org,
        models=models,
        require_fable_if_fleet_has_it=enforce_fable,
    )

    # Identify any healthy non-Fable seat(s) with higher 7d headroom skipped by Fable Guard.
    skipped_non_fable: list[AccountRow] = []
    if enforce_fable:
        best_wk_used = 100.0
        if best_row is not None:
            f_pct, _ = _fable_pct_and_reset(best_row)
            sd_pct = float(best_row.seven_day_pct or 0.0)
            best_wk_used = max(sd_pct, f_pct if f_pct is not None else 0.0)
        unfiltered = accounts.harvest_best_slot(
            row_list,
            sentinels=sentinels,
            identities=identities,
            prefer_org=prefer_org,
            models=models,
            require_fable_if_fleet_has_it=False,
        )
        for r in row_list:
            if not accounts.row_lacks_fable_quota(r):
                continue
            if r.disabled or not r.switchable or r.attention_note or r.slot in sentinels:
                continue
            r_sd = float(r.seven_day_pct or 0.0)
            r_fh = float(r.five_hour_pct or 0.0)
            if r_fh >= 100.0 or r_sd >= 100.0:
                continue
            if best_row is None or r_sd < best_wk_used or (unfiltered is not None and unfiltered.slot == r.slot):
                skipped_non_fable.append(r)

    _print_banner()
    if skipped_non_fable:
        parts = []
        for sr in skipped_non_fable:
            sd_free = max(0.0, 100.0 - float(sr.seven_day_pct or 0.0))
            parts.append(f"#{sr.slot} {sr.alias} ({sr.plan_type or 'non-Fable'} · 7d {sd_free:.0f}% free)")
        print(
            f"  {GOLD}🛡️  Fable Guard skipped higher-7d non-Fable seat(s):{RESET} {', '.join(parts)} "
            f"{DIM}(no included Fable allowance){RESET}"
        )

    if best_row is None:
        if enforce_fable:
            print(
                f"  {RED}✗ No healthy Fable-capable Claude account available.{RESET} "
                f"{DIM}(Non-Fable seats skipped by Fable Guard; run `bswap fable-guard off` or `bswap <slot>` to override.){RESET}",
                file=sys.stderr,
            )
        else:
            print(f"  {RED}✗ No healthy Claude account available to switch to.{RESET}", file=sys.stderr)
        return 1

    f_pct, f_reset = _fable_pct_and_reset(best_row)
    quota_desc = (
        f"Fable {max(0.0, 100.0 - f_pct):.0f}% free · 5h {float(best_row.five_hour_pct or 0.0):.0f}%"
        if f_pct is not None
        else f"7d {max(0.0, 100.0 - float(best_row.seven_day_pct or 0.0)):.0f}% free · 5h {float(best_row.five_hour_pct or 0.0):.0f}%"
    )
    reasons: list[str] = []
    if prefer_org and identities.get(best_row.slot, ("", ""))[1] == prefer_org:
        reasons.append("same-org")
    try:
        hrs_left = accounts._hours_until_reset_label(
            best_row.seven_day_resets_at, dt.datetime.now().timestamp()
        )
        rem_wk = max(0.0, 100.0 - float(best_row.seven_day_pct or 0.0))
        if hrs_left is not None and 0.0 < hrs_left <= 48.0 and rem_wk >= 15.0:
            reasons.append("perishable-quota priority")
    except Exception:
        pass
    reason_suffix = f" ({', '.join(reasons)})" if reasons else ""

    if active is not None and best_row.slot == active.slot:
        print(
            f"  {GREEN}✓ Already on the best {'Fable-capable ' if enforce_fable else ''}account: "
            f"Slot #{best_row.slot} ({best_row.alias}){RESET}  {DIM}[{quota_desc}]{reason_suffix}{RESET}"
        )
        return 0

    if dry_run:
        print(
            f"  {CYAN}→ Would switch to Slot #{best_row.slot} ({best_row.alias}){RESET}  "
            f"{DIM}[{quota_desc}]{reason_suffix}{RESET}"
        )
        return 0

    accounts.record_cli_switch_marker(bdir, str(best_row.slot))
    ok = bool(src.switch_to(str(best_row.slot)))
    if not ok:
        err = getattr(src, "last_error", None) or "switch failed"
        print(f"  {RED}✗ Failed to switch to Slot #{best_row.slot} ({best_row.alias}): {err}{RESET}", file=sys.stderr)
        return 1

    print(
        f"  {GREEN}✓ Switched to Slot #{best_row.slot} ({best_row.alias}){RESET}  "
        f"{DIM}[{quota_desc}]{reason_suffix}{RESET}"
    )
    return 0


def _cmd_sessions(args: list[str]) -> int:
    """Inspect active Claude Code sessions, their Preserved-Thinking organization owner, and optionally auto-fix/pin."""
    from . import accounts, fleet

    do_fix = "--fix" in args or "fix" in args
    bdir = _backup_dir()
    fable_guard = _fable_guard_enabled()

    # Read-only inspection never mutates settings.json; only --fix / fix-thinking writes policy.
    policy_updated = _ensure_optimal_autoswitch_policy(bdir) if do_fix else False
    identities = fleet.account_identities(bdir)
    src = accounts.create_account_source()
    src.refresh(force=False)
    row_list = list(src.rows())
    rows = {r.slot: r for r in row_list}
    sentinels = src.sentinels() if callable(getattr(src, "sentinels", None)) else {}
    active = src.active()
    active_slot = active.slot if active else None
    fleet_has_fable = accounts.fleet_has_fable_accounts(row_list, sentinels=sentinels)
    enforce_fable = fable_guard and fleet_has_fable

    snap = fleet.collect(backup_dir=bdir, active_slot=active_slot)
    _print_banner()
    print(f"  {BOLD}{ORANGE}🛡️  BELKINS PRESERVED-THINKING & SESSION QUALITY DIAGNOSTIC{RESET}")
    if policy_updated:
        print(
            f"  {GREEN}✓ Upgraded autoswitch policy: model → all (Fable/Opus/Sonnet), "
            f"weeklyThreshold → 88.0%, cooldown → 120s, interval → 30s, hysteresis → 10.0%{RESET}"
        )
    else:
        print(f"  {GREEN}✓ Thinking Guard: ACTIVE (concurrency-scaled busy hold · all per-model windows tracked){RESET}")
    print()

    if not snap.sessions:
        print(f"  {DIM}No live interactive Claude Code sessions detected.{RESET}")
        return 0

    org_weights: dict[str, int] = {}
    mismatch_count = 0
    latched_sessions: list[tuple[Any, str]] = []
    skipped_non_fable_owners: set[int] = set()
    pinned_workspaces: set[str] = set()
    for s in snap.sessions:
        cwd_p = Path(s.cwd) if s.cwd else Path.cwd()
        short_w = cwd_p.name or s.cwd
        is_latched = bool(getattr(s, "latched_credits", False))
        is_rate_limited = bool(getattr(s, "hit_rate_limit", False))
        sess_id = str(getattr(s, "session_id", "") or "").strip()

        if s.pinned:
            owner_slot = s.slot
            mismatch = False
        else:
            owner_slot = s.owner_slot
            mismatch = s.org_shifted
            if owner_slot is None and identities:
                owner_slot, mismatch = fleet.detect_workspace_owner_slot(
                    cwd_p, identities, current_slot=s.slot, pid=s.pid
                )
        owner_row = rows.get(owner_slot) if owner_slot is not None else None
        owner_lbl = f"#{owner_slot} ({owner_row.alias})" if owner_row else (f"#{owner_slot}" if owner_slot else "unknown")
        cur_row = rows.get(s.slot) if s.slot is not None else None
        cur_lbl = f"#{s.slot} ({cur_row.alias})" if cur_row else f"#{s.slot}"
        if mismatch:
            mismatch_count += 1
        if (not s.pinned) and owner_slot is not None:
            owner_org = identities.get(owner_slot, ("", ""))[1]
            if owner_org:
                weight = 3 if (s.status or "").lower() == "busy" else 1
                org_weights[owner_org] = org_weights.get(owner_org, 0) + weight

        if is_latched:
            mode_badge = f"{RED}⚡ LATCHED (credits){RESET}"
            rec_cmd = _resume_command(s.cwd, sess_id)
            latched_sessions.append((s, rec_cmd))
        elif is_rate_limited:
            mode_badge = f"{RED}⏱ RATE LIMITED{RESET}"
        elif s.pinned:
            mode_badge = f"{CYAN}📌 PINNED{RESET}"
        elif mismatch:
            mode_badge = f"{GOLD}⚠ ORG SHIFTED{RESET}"
        else:
            mode_badge = f"{GREEN}● MATCHED{RESET}"
        print(f"  PID {s.pid:<6} {BOLD}{short_w:<18}{RESET} [{s.status or 'idle':<7}] {mode_badge}  cur={cur_lbl}  thinking_org={owner_lbl}")
        if is_latched:
            print(f"    {GOLD}↳ Run /exit then resume:{RESET} {ORANGE}{rec_cmd}{RESET}")

        if do_fix and (not s.pinned) and owner_slot is not None and s.cwd and "/private/tmp/" not in s.cwd:
            if enforce_fable and owner_row is not None and accounts.row_lacks_fable_quota(owner_row):
                skipped_non_fable_owners.add(owner_slot)
            else:
                ident = identities.get(owner_slot)
                if ident and s.cwd not in pinned_workspaces:
                    try:
                        fleet.set_mapping(bdir, s.cwd, ident[0], ident[1])
                        pinned_workspaces.add(s.cwd)
                    except Exception:
                        pass

    if pinned_workspaces:
        print(f"\n  {GREEN}✓ Pinned {len(pinned_workspaces)} workspace(s) to their current thinking organization for future `bswap claude` launches.{RESET}")

    best_owner: AccountRow | None = None
    active_org = identities.get(active_slot, ("", ""))[1] if active_slot is not None else ""
    for cand_org, _cnt in sorted(org_weights.items(), key=lambda kv: kv[1], reverse=True):
        if cand_org == active_org and _slot_is_healthy_for_session(
            active, sentinels, fleet_rows=row_list, fable_guard=fable_guard
        ):
            best_owner = active
            break
        same_org_rows = [
            r for r in row_list
            if identities.get(r.slot, ("", ""))[1] == cand_org
            and _slot_is_healthy_for_session(r, sentinels, fleet_rows=row_list, fable_guard=fable_guard)
        ]
        if enforce_fable:
            same_org_rows = [r for r in same_org_rows if not accounts.row_lacks_fable_quota(r)]
            # Track non-Fable seats in this org so we can explain why restore skipped them
            for r in row_list:
                if identities.get(r.slot, ("", ""))[1] == cand_org and accounts.row_lacks_fable_quota(r):
                    skipped_non_fable_owners.add(r.slot)
        cand = accounts.harvest_best_slot(
            same_org_rows,
            sentinels=sentinels,
            identities=identities,
            prefer_org=cand_org,
            require_fable_if_fleet_has_it=enforce_fable,
        )
        if cand is not None and not (enforce_fable and accounts.row_lacks_fable_quota(cand)):
            best_owner = cand
            break

    if skipped_non_fable_owners:
        names = ", ".join(
            f"#{s} ({rows[s].alias})" if s in rows else f"#{s}" for s in sorted(skipped_non_fable_owners)
        )
        print(
            f"\n  {GOLD}🛡️  Fable Guard skipped restoring onto non-Fable seat(s): {names}{RESET} "
            f"{DIM}(no included Fable allowance){RESET}"
        )

    if best_owner is not None and best_owner.slot != active_slot:
        print()
        if do_fix:
            accounts.record_cli_switch_marker(bdir, str(best_owner.slot))
            src.switch_to(str(best_owner.slot))
            print(
                f"  {GREEN}✓ Restored global login to Slot #{best_owner.slot} ({best_owner.alias}) "
                f"so your active sessions regain their Preserved Thinking & Prompt Cache!{RESET}"
            )
        else:
            print(
                f"  {GOLD}💡 Tip:{RESET} Run {ORANGE}bswap fix-thinking{RESET} to restore Slot #{best_owner.slot} ({best_owner.alias}) "
                f"and pin workspaces, or launch future terminals with {ORANGE}bswap claude{RESET} for 100% org isolation."
            )
    elif mismatch_count > 0:
        if skipped_non_fable_owners:
            print(
                f"  {DIM}💡 Keeping Fable-safe global login Slot #{active_slot} ({active.alias if active else ''}); "
                f"launch separate workspaces with `{ORANGE}bswap claude{DIM}` for org isolation.{RESET}"
            )
        else:
            print(
                f"\n  {GREEN}✓ Global login Slot #{active_slot} ({active.alias if active else ''}) already matches your dominant busy sessions!{RESET}\n"
                f"  {DIM}💡 Launch separate workspaces with `{ORANGE}bswap claude{DIM}` so each project stays locked to its own slot.{RESET}"
            )
    elif not latched_sessions:
        print(f"\n  {GREEN}✓ All active sessions match their thinking organization!{RESET}")

    if latched_sessions:
        print(
            f"\n  {RED}⚡ {len(latched_sessions)} session(s) tripped Claude Code's usage-credits latch — "
            f"run `/exit` and the `claude --resume` command above to recover on your active Fable seat.{RESET}"
        )
    return 0


def _cmd_claude_session(args: list[str]) -> int:
    """Launch Claude Code inside an isolated per-account profile (`cswap run --share-history`) locked to the workspace's thinking org."""
    from . import accounts, fleet

    cswap_bin = _find_cswap()
    if cswap_bin is None:
        print(f"{RED}cswap backend not found.{RESET}", file=sys.stderr)
        return 1

    bdir = _backup_dir()
    fable_guard = _fable_guard_enabled()
    identities = fleet.account_identities(bdir)
    src = accounts.create_account_source()
    src.refresh(force=False)
    row_list = list(src.rows())
    rows = {r.slot: r for r in row_list}
    sentinels = src.sentinels() if callable(getattr(src, "sentinels", None)) else {}
    active = src.active()
    active_slot = active.slot if active else None
    fleet_has_fable = accounts.fleet_has_fable_accounts(row_list, sentinels=sentinels)
    enforce_fable = fable_guard and fleet_has_fable

    explicit_target = False
    target_slot: str | None = None
    claude_args = list(args)
    if claude_args and claude_args[0] == "--":
        claude_args = claude_args[1:]
    elif claude_args and not claude_args[0].startswith("-"):
        first = claude_args[0]
        matched_alias = next((r for r in row_list if r.alias.lower() == first.lower()), None)
        if first.isdigit() or matched_alias is not None:
            target_slot = str(matched_alias.slot) if matched_alias is not None else first
            explicit_target = True
            claude_args = claude_args[1:]
            if claude_args and claude_args[0] == "--":
                claude_args = claude_args[1:]

    if explicit_target and target_slot is not None and enforce_fable:
        exp_row = rows.get(int(target_slot)) if target_slot.isdigit() else None
        if exp_row is not None and accounts.row_lacks_fable_quota(exp_row):
            print(
                f"{GOLD}⚠ Warning: Slot #{exp_row.slot} ({exp_row.alias}) lacks included Fable quota "
                f"— proceeding per explicit slot argument.{RESET}"
            )

    cwd = Path.cwd()
    prefer_org: str | None = None
    if target_slot is None:
        # 1. Honour directory pin or transcript organization owner when healthy & Fable-safe
        mappings = fleet.load_mappings(bdir, identities)
        mapped = fleet._mapping_for(str(cwd), mappings)
        if mapped is not None and mapped.slot is not None:
            mapped_row = rows.get(mapped.slot)
            if _slot_is_healthy_for_session(
                mapped_row, sentinels, max_5h=92.0, max_7d=96.0, fleet_rows=row_list, fable_guard=fable_guard
            ):
                r = rows[mapped.slot]
                target_slot = str(r.slot)
                print(
                    f"{GREEN}🛡️  Locked to Pinned Slot #{r.slot} ({r.alias}){RESET} "
                    f"{DIM}(directory mapping){RESET}"
                )
            elif enforce_fable and mapped_row is not None and accounts.row_lacks_fable_quota(mapped_row):
                print(
                    f"{GOLD}🛡️  Fable Guard skipped pinned non-Fable Slot #{mapped_row.slot} ({mapped_row.alias}){RESET} "
                    f"{DIM}(selecting Fable-capable seat instead){RESET}"
                )
        if target_slot is None:
            owner_slot, _shifted = fleet.detect_workspace_owner_slot(
                cwd, identities, current_slot=active_slot
            )
            if owner_slot is not None:
                prefer_org = identities.get(owner_slot, ("", ""))[1] or None
                same_org_candidates = [
                    r for r in row_list
                    if prefer_org and identities.get(r.slot, ("", ""))[1] == prefer_org
                    and _slot_is_healthy_for_session(
                        r, sentinels, max_5h=92.0, max_7d=96.0, fleet_rows=row_list, fable_guard=fable_guard
                    )
                ]
                if enforce_fable:
                    same_org_candidates = [
                        r for r in same_org_candidates if not accounts.row_lacks_fable_quota(r)
                    ]
                chosen_org_row = accounts.harvest_best_slot(
                    same_org_candidates,
                    sentinels=sentinels,
                    identities=identities,
                    prefer_org=prefer_org,
                    require_fable_if_fleet_has_it=enforce_fable,
                )
                if chosen_org_row is not None:
                    target_slot = str(chosen_org_row.slot)
                    print(
                        f"{GREEN}🛡️  Locked to Slot #{chosen_org_row.slot} ({chosen_org_row.alias}){RESET} "
                        f"{DIM}(matches this workspace's Preserved Thinking organization){RESET}"
                    )

    if target_slot is None:
        # 2. Pick healthiest Fable-safe slot via Perishable-Quota & Org-Aware Harvester
        pool = (
            [r for r in row_list if not accounts.row_lacks_fable_quota(r)]
            if enforce_fable
            else list(row_list)
        )
        chosen = accounts.harvest_best_slot(
            pool,
            sentinels=sentinels,
            identities=identities,
            prefer_org=prefer_org,
            require_fable_if_fleet_has_it=enforce_fable,
        )
        if chosen is not None:
            target_slot = str(chosen.slot)
            print(
                f"{GREEN}🛡️  Launching isolated session on Slot #{chosen.slot} ({chosen.alias}){RESET} "
                f"{DIM}(perishable-quota & org-aware selection){RESET}"
            )
        else:
            fallback_slot: int | None = active_slot
            if enforce_fable:
                active_row = rows.get(active_slot) if active_slot is not None else None
                if active_row is None or accounts.row_lacks_fable_quota(active_row):
                    fable_fallbacks = [
                        r for r in row_list
                        if r.slot >= 0
                        and not r.disabled
                        and r.switchable
                        and not r.attention_note
                        and r.slot not in sentinels
                        and accounts.row_has_fable_quota(r)
                    ]
                    if fable_fallbacks:
                        fable_fallbacks.sort(
                            key=lambda r: (
                                _fable_pct_and_reset(r)[0] if _fable_pct_and_reset(r)[0] is not None else 100.0,
                                float(r.five_hour_pct or 0.0),
                            )
                        )
                        fallback_slot = fable_fallbacks[0].slot
            target_slot = str(fallback_slot if fallback_slot is not None else 1)

    # Guarantee true CLAUDE_CONFIG_DIR isolation without creating duplicate refresh tokens:
    # `cswap run N` falls back to unisolated `~/.claude` if N is currently the active default login.
    # When no busy default-profile sessions hold `~/.claude`, rotate the global default login onto
    # another healthy Fable-safe slot first so N runs inside `<backup>/sessions/N-.../` with shared history!
    if active_slot is not None and target_slot == str(active_slot):
        other_healthy = [
            r for r in row_list
            if r.slot != active_slot
            and _slot_is_healthy_for_session(
                r, sentinels, max_5h=95.0, max_7d=99.0, fleet_rows=row_list, fable_guard=fable_guard
            )
        ]
        if enforce_fable:
            other_healthy = [r for r in other_healthy if not accounts.row_lacks_fable_quota(r)]
        try:
            default_recs, _ = fleet.read_registry(fleet.sessions_dir())
            default_busy = sum(1 for rec in default_recs if str(rec.get("status") or "").lower() == "busy")
        except Exception:
            default_busy = 0
        if other_healthy and default_busy == 0:
            standby = accounts.harvest_best_slot(
                other_healthy,
                sentinels=sentinels,
                identities=identities,
                require_fable_if_fleet_has_it=enforce_fable,
            )
            if standby is not None:
                accounts.record_cli_switch_marker(bdir, str(standby.slot))
                src.switch_to(str(standby.slot))
        elif other_healthy and not explicit_target and not prefer_org:
            alt = accounts.harvest_best_slot(
                other_healthy,
                sentinels=sentinels,
                identities=identities,
                require_fable_if_fleet_has_it=enforce_fable,
            )
            if alt is not None:
                target_slot = str(alt.slot)

    run_cmd = [str(cswap_bin), "run", target_slot, "--share-history"]
    if claude_args:
        run_cmd.extend(["--", *claude_args])
    return subprocess.run(run_cmd).returncode


SHIMS_DIR = WIDGET_DIR / "shims"


def _cmd_shim(args: list[str]) -> int:
    """Manage opt-in CLI wrapper shims (`claude` & `codex`) in `<WIDGET_DIR>/shims`."""
    action = (args[0].lower() if args else "status").strip()
    claude_shim = SHIMS_DIR / "claude"
    codex_shim = SHIMS_DIR / "codex"
    path_export = f'export PATH="{SHIMS_DIR}:$PATH"'

    if action in {"off", "remove", "uninstall", "disable"}:
        for p in (claude_shim, codex_shim):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        print(f"{GREEN}✓ Removed Belkins CLI shims from {SHIMS_DIR}{RESET}")
        return 0

    if action in {"on", "install", "enable"}:
        SHIMS_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
        bswap_py = sys.executable
        q_widget = str(WIDGET_DIR)
        claude_body = "\n".join(
            [
                "#!/bin/bash",
                "# Belkins Usage Bar — isolated Fable-safe org-locked Claude wrapper shim",
                f'SHIM_DIR="{SHIMS_DIR}"',
                'find_real() {',
                '  local IFS=":"',
                '  for dir in $PATH; do',
                '    [ -z "$dir" ] || [ "$dir" = "$SHIM_DIR" ] && continue',
                '    if [ -x "$dir/$1" ]; then echo "$dir/$1"; return 0; fi',
                '  done',
                '  return 1',
                '}',
                'REAL_CLAUDE="$(find_real claude)" || { echo "claude binary not found on PATH" >&2; exit 127; }',
                'if [ "${BSWAP_SHIM_ACTIVE:-0}" = "1" ] || [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then',
                '  exec "$REAL_CLAUDE" "$@"',
                'fi',
                'case "${1:-}" in',
                '  auth|login|logout|mcp|update|--version|-v|--help|-h|doctor)',
                '    exec "$REAL_CLAUDE" "$@"',
                '    ;;',
                'esac',
                'export BSWAP_SHIM_ACTIVE=1',
                f'PYTHONPATH="{q_widget}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{bswap_py}" -m cc_usage_widget.bswap_cli claude -- "$@"',
                "",
            ]
        )
        codex_body = "\n".join(
            [
                "#!/bin/bash",
                "# Belkins Usage Bar — quota-aware Codex wrapper shim",
                f'SHIM_DIR="{SHIMS_DIR}"',
                'find_real() {',
                '  local IFS=":"',
                '  for dir in $PATH; do',
                '    [ -z "$dir" ] || [ "$dir" = "$SHIM_DIR" ] && continue',
                '    if [ -x "$dir/$1" ]; then echo "$dir/$1"; return 0; fi',
                '  done',
                '  return 1',
                '}',
                'REAL_CODEX="$(find_real codex)" || { echo "codex binary not found on PATH" >&2; exit 127; }',
                'case "${1:-}" in',
                '  login|logout|--version|-V|--help|-h)',
                '    exec "$REAL_CODEX" "$@"',
                '    ;;',
                'esac',
                'if [ -z "${CODEX_HOME:-}" ]; then',
                f'  BEST_HOME="$(PYTHONPATH="{q_widget}${{PYTHONPATH:+:$PYTHONPATH}}" "{bswap_py}" -m cc_usage_widget.codex_accounts best 2>/dev/null)"',
                '  if [ -n "$BEST_HOME" ] && [ -d "$BEST_HOME" ]; then',
                '    export CODEX_HOME="$BEST_HOME"',
                '  fi',
                'fi',
                'exec "$REAL_CODEX" "$@"',
                "",
            ]
        )
        for path, content in ((claude_shim, claude_body), (codex_shim, codex_body)):
            path.write_text(content, encoding="utf-8")
            os.chmod(path, 0o755)
        print(f"{GREEN}✓ Installed Belkins wrapper shims in {SHIMS_DIR}:{RESET}")
        print(f"  • {BOLD}claude{RESET} → auto-isolates sessions via `bswap claude` (Fable-safe, recursion-safe)")
        print(f"  • {BOLD}codex{RESET}  → routes `CODEX_HOME` to healthiest seat via `codex_accounts best`")
        print(f"\n  {GOLD}Add to ~/.zshrc to activate:{RESET}\n    {ORANGE}{path_export}{RESET}")
        return 0

    st = _shim_status()
    installed = bool(st["installed"])
    in_path = bool(st["in_path"])
    print(
        f"  {BOLD}Belkins Shims:{RESET} "
        f"{GREEN + 'INSTALLED' if installed else DIM + 'NOT INSTALLED'}{RESET}  "
        f"({GREEN + 'ACTIVE on PATH' if in_path else GOLD + 'not prepended to PATH'}{RESET})"
    )
    print(f"  {DIM}Enable:{RESET} {ORANGE}bswap shim on{RESET}   {DIM}│  Disable:{RESET} {ORANGE}bswap shim off{RESET}")
    if installed and not in_path:
        print(f"  {DIM}Shell:{RESET}  {ORANGE}{path_export}{RESET}")
    return 0


def _cmd_roi(args: list[str]) -> int:
    """Print the 30-day Subscription ROI & Prompt-Cache Efficiency Scorecard."""
    from .state import SettingsStore

    settings_path = WIDGET_DIR / "settings.json"
    s_store = SettingsStore(path=settings_path)
    s_store.load()

    if args and args[0].replace(".", "", 1).isdigit():
        val = max(0.0, min(100_000.0, float(args[0])))
        s_store.set("monthly_subscription_usd", int(round(val)))
        s_store.save()
        print(f"{GREEN}✓ Saved monthly subscription cost: ${val:,.0f}/mo{RESET}\n")

    comp = _load_companion_state()
    configured_sub = float(s_store.as_dict().get("monthly_subscription_usd", 0) or 0)
    if configured_sub > 0:
        est_sub, sub_src = configured_sub, "configured monthly_subscription_usd"
    else:
        seat_count = max(0, int(comp.claude_total)) + max(0, int(comp.codex_total))
        est_sub = float(seat_count) * 200.0
        sub_src = f"estimated from {seat_count} tracked seat(s)" if seat_count > 0 else "unconfigured"

    rollups_path = WIDGET_DIR / "rollups.json"
    cache_read_30d = 0
    cache_create_30d = 0
    cache_5m_30d = 0
    input_30d = 0
    today_str = dt.date.today().isoformat()
    if rollups_path.is_file():
        try:
            r_store = DailyRollupStore(path=rollups_path, keep_days=30)
            r_store.load()
            for r in r_store.last_n_days(30, today=today_str):
                t = r.total
                input_30d += int(t.input)
                cache_read_30d += int(t.cache_read)
                w5 = int(t.cache_write_5m)
                w1 = int(t.cache_write_1h)
                cache_create_30d += w5 + w1
                cache_5m_30d += w5
        except Exception:
            pass

    prompt_total = input_30d + cache_read_30d + cache_create_30d
    hit_pct = (cache_read_30d / prompt_total * 100.0) if prompt_total > 0 else 0.0
    thrash_pct = (cache_5m_30d / cache_create_30d * 100.0) if cache_create_30d > 0 else 0.0
    dollar = chr(36)

    _print_banner()
    print(f"{BOLD}{ORANGE}━━━ 💰 BELKINS 30-DAY SUBSCRIPTION ROI & CACHE EFFICIENCY ━━━{RESET}")
    print(f"  30d API-Equivalent Value:  {BOLD}{GREEN}{dollar}{comp.usd_30d:,.2f}{RESET}  ({_fmt_tokens(comp.tokens_30d)} tokens)")
    if est_sub > 0:
        roi_mult = comp.usd_30d / est_sub
        net_val = comp.usd_30d - est_sub
        print(f"  Monthly Seat Cost:         {BOLD}{dollar}{est_sub:,.0f}/mo{RESET}  {DIM}({sub_src}){RESET}")
        print(
            f"  Subscription ROI:          {BOLD}{GOLD}{roi_mult:.1f}×{RESET}  "
            f"({GREEN if net_val >= 0 else RED}{'+' if net_val >= 0 else '-'}{dollar}{abs(net_val):,.0f} net value vs API{RESET})"
        )
    else:
        print(f"  {DIM}Set exact monthly seat cost with:{RESET} {ORANGE}bswap roi <monthly_usd>{RESET}")
    print(
        f"  Prompt-Cache Hit Rate:     {BOLD}{CYAN}{hit_pct:.1f}%{RESET}  "
        f"{DIM}(read {_fmt_tokens(cache_read_30d)} / prompt {_fmt_tokens(prompt_total)}){RESET}"
    )
    print(
        f"  5m Ephemeral Cache Share:  {BOLD}{thrash_pct:.1f}%{RESET} of writes  "
        f"{DIM}({'low thrash ✓' if thrash_pct < 75.0 else 'tip: use `bswap claude` to preserve 1h org cache'}){RESET}"
    )
    print()
    return 0


def _cmd_threshold(args: list[str]) -> int:
    """View or update claude-swap's autoswitch.threshold (5h) and optional weeklyThreshold (7d/Fable)."""
    from . import accounts

    src = accounts.create_account_source()
    if not args:
        cur = src.autoswitch_threshold()
        wk = src.autoswitch_weekly_threshold()
        mdl = src.autoswitch_model() or "5h/7d only"
        lbl = f"{cur:.0f}%" if cur is not None else "default (90%)"
        wk_lbl = f"{wk:.0f}%" if wk is not None else f"auto ({max(float(cur or 90.0), 92.0):.0f}%)"
        print(f"  {BOLD}Auto-switch 5h threshold:{RESET}     {ORANGE}{lbl}{RESET}")
        print(f"  {BOLD}Weekly / Fable threshold:{RESET}    {ORANGE}{wk_lbl}{RESET}")
        print(f"  {BOLD}Scoped model limits:{RESET}         {CYAN}{mdl}{RESET}  {DIM}(`bswap model all|Fable|off`){RESET}")
        return 0
    try:
        pct = float(args[0].rstrip("%"))
        wk_pct = float(args[1].rstrip("%")) if len(args) >= 2 else None
    except ValueError:
        print(f"{RED}Usage: bswap threshold <50..99> [weekly_50..99]{RESET}", file=sys.stderr)
        return 2
    if src.set_autoswitch_threshold(pct):
        clamped = min(99.0, max(50.0, pct))
        if wk_pct is not None:
            src.set_autoswitch_weekly_threshold(wk_pct)
            wk_clamped = min(99.0, max(50.0, wk_pct))
            print(
                f"{GREEN}✓ Auto-switch set to {BOLD}{clamped:.0f}%{RESET}{GREEN} (5h) · "
                f"{BOLD}{wk_clamped:.0f}%{RESET}{GREEN} (weekly/Fable){RESET}"
            )
        else:
            print(f"{GREEN}✓ Auto-switch threshold set to {BOLD}{clamped:.0f}%{RESET}{GREEN} (live in menu bar & claude-swap){RESET}")
        return 0
    print(f"{RED}✗ Failed to update threshold: {src.last_error or 'unknown error'}{RESET}", file=sys.stderr)
    return 1


def _cmd_model(args: list[str]) -> int:
    """View or update claude-swap's autoswitch.model (`all`, `Fable`, `Fable,Opus`, or `off`)."""
    from . import accounts

    src = accounts.create_account_source()
    if not args:
        cur = src.autoswitch_model() or "5h/7d only"
        print(f"  {BOLD}Scoped model limits:{RESET} {ORANGE}{cur}{RESET}  {DIM}(set with `bswap model all`, `bswap model Fable`, or `bswap model off`){RESET}")
        return 0
    wanted = ",".join(args).strip()
    if src.set_autoswitch_model(wanted):
        shown = "5h/7d only" if wanted.lower() in {"off", "none", "5h7d", ""} else ("all" if wanted.lower() == "all" else wanted)
        print(f"{GREEN}✓ Auto-switch model limits set to {BOLD}{shown}{RESET}")
        return 0
    print(f"{RED}✗ Failed to update model limits: {src.last_error or 'unknown error'}{RESET}", file=sys.stderr)
    return 1


def _cmd_strategy(args: list[str]) -> int:
    """View or update claude-swap's autoswitch.strategy (`best` | `consume-first`)."""
    from . import accounts

    src = accounts.create_account_source()
    if not args:
        cur = src.autoswitch_strategy()
        print(f"  {BOLD}Auto-switch strategy:{RESET} {ORANGE}{cur}{RESET}  {DIM}(options: `best`, `consume-first`){RESET}")
        return 0
    wanted = args[0].strip().lower()
    if wanted in {"harvest", "consume", "expiring"}:
        wanted = "consume-first"
    if src.set_autoswitch_strategy(wanted):
        print(f"{GREEN}✓ Auto-switch strategy set to {BOLD}{wanted}{RESET}")
        return 0
    print(f"{RED}✗ Strategy must be `best` or `consume-first`{RESET}", file=sys.stderr)
    return 2


def _cmd_unpin(args: list[str]) -> int:
    """Remove directory pin for target path (default: current directory)."""
    from . import fleet

    target_dir = Path(args[0]).expanduser().resolve() if args else Path.cwd().resolve()
    bdir = _backup_dir()
    try:
        removed = fleet.clear_mapping(bdir, target_dir)
    except Exception as exc:
        print(f"{RED}✗ Could not unpin {target_dir}: {exc}{RESET}", file=sys.stderr)
        return 1
    if removed:
        print(f"{GREEN}✓ Unpinned workspace {target_dir}{RESET}")
    else:
        print(f"{DIM}No exact pin recorded for {target_dir}{RESET}")
    return 0


def _cmd_help() -> int:
    _print_banner()
    print(f"{BOLD}Usage:{RESET} {ORANGE}bswap{RESET} [command]   (or {ORANGE}belkins{RESET} [command])")
    print()
    print(f"{BOLD}Interactive TUI & Companion Commands:{RESET}")
    print(f"  {ORANGE}bswap{RESET} / {ORANGE}bswap tui{RESET}          Launch interactive arrow-key TUI (Switch, Watch, Spark Vault, Boss Raid)")
    print(f"  {ORANGE}bswap update{RESET}               Pull latest Belkins Usage Bar from GitHub & restart menu bar")
    print(f"  {ORANGE}bswap watch{RESET}                Launch live multi-account Watch monitor in TUI")
    print(f"  {ORANGE}bswap status{RESET}               Print Companion HUD + Claude & Codex fleet status")
    print(f"  {ORANGE}bswap evo{RESET} / {ORANGE}bswap dex{RESET}      Inspect 8-Stage Cyber-Forms, Boss Raid Arena & 14 Hall of Fame Trophies")
    print(f"  {ORANGE}bswap claim{RESET}                Claim your Daily Supply Crate (+150..300 CR & +15K..45K XP)")
    print(f"  {ORANGE}bswap raid [strikes]{RESET}       Attack the active Raid Boss for SparkCoins & Bonus XP")
    print(f"  {ORANGE}bswap morph <1..8|auto>{RESET}    Morph your active Companion into any unlocked Cyber-Form")
    print(f"  {ORANGE}bswap vault{RESET} / {ORANGE}bswap shop{RESET}   Open the 12-item Belkins Spark Vault (Relics, Serums, Skins & Bots)")
    print(f"  {ORANGE}bswap buy <item>{RESET}           Buy, equip, or inject a Vault relic (e.g. serum, blade, core, arcade)")
    print(f"  {ORANGE}bswap dashboard{RESET}            Open the interactive Belkins HTML Dashboard in browser")
    print(f"  {ORANGE}bswap roi [monthly_usd]{RESET}    Show 30-day Subscription ROI & Prompt-Cache Scorecard")
    print(f"  {ORANGE}bswap bar{RESET} [on|off|restart|status|logs]  Control the Belkins menu-bar app")
    print(f"  {ORANGE}bswap codex{RESET} [list|switch|best|adopt|probe] Manage & switch Codex accounts")
    print()
    print(f"{BOLD}Claude Fleet & Preserved-Thinking Commands:{RESET}")
    print(f"  {ORANGE}bswap doctor [--fix]{RESET}       Diagnose & auto-heal Fable autoswitch, seats, latched sessions & LaunchAgent")
    print(f"  {ORANGE}bswap fable-guard [on|off|status]{RESET} Inspect or toggle Fable Guard (skip non-Fable Team/Pro seats)")
    print(f"  {ORANGE}bswap claude [slot] [args]{RESET} Launch Claude Code in a Fable-safe org-locked profile (never drops thinking!)")
    print(f"  {ORANGE}bswap shim [on|off|status]{RESET} Install/remove auto-isolating `claude` & `codex` CLI shims")
    print(f"  {ORANGE}bswap fix-thinking{RESET}         Diagnose & restore session orgs + set 88% weekly/Fable guard")
    print(f"  {ORANGE}bswap sessions{RESET}             Inspect live Claude sessions, latched credits & Preserved-Thinking orgs")
    print(f"  {ORANGE}bswap unpin [dir]{RESET}          Remove workspace directory pin (defaults to current directory)")
    print(f"  {ORANGE}bswap threshold [50..99]{RESET}   View or set live Claude 5h & weekly/Fable auto-switch threshold %")
    print(f"  {ORANGE}bswap model [all|Fable|off]{RESET} Configure per-model weekly auto-switch limits (default: `all`)")
    print(f"  {ORANGE}bswap strategy [best|consume-first]{RESET} Set auto-switch rotation strategy")
    print(f"  {ORANGE}bswap <slot|alias>{RESET}         Switch active Claude account to slot number or alias")
    print(f"  {ORANGE}bswap best [--dry-run]{RESET}     Switch to the healthiest Fable-safe, same-org & perishable-quota account")
    print(f"  {ORANGE}bswap list{RESET}                 List all registered Claude accounts")
    print(f"  {ORANGE}bswap add{RESET}                  Register current Claude credentials into a slot")
    print(f"  {ORANGE}bswap remove <slot>{RESET}        Remove a slot from the rotation")
    print(f"  {ORANGE}bswap enable|disable <s|a>{RESET} Enable or disable a slot for auto-switch")
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

    # Bare `bswap` in an interactive terminal launches the full arrow-key TUI
    # (just like `cswap`), with both Claude account switching and Belkins submenus.
    if not args:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return _run_belkins_tui(start="dashboard")
        return _run_cswap(["status"], show_banner=True)

    cmd = args[0].lower()
    if cmd == "tui":
        return _run_belkins_tui(start="dashboard")
    if cmd == "watch" and len(args) == 1 and sys.stdin.isatty() and sys.stdout.isatty():
        return _run_belkins_tui(start="watch")
    if cmd == "status":
        return _run_cswap(["status", *args[1:]], show_banner=True)
    if cmd == "best":
        return _cmd_best(args[1:])
    if cmd in {"doctor", "diag", "diagnose", "health"}:
        return _cmd_doctor(args[1:])
    if cmd in {"fable-guard", "fable_guard", "fableguard", "fable"}:
        return _cmd_fable_guard(args[1:])
    if cmd.isdigit():
        from . import accounts

        accounts.record_cli_switch_marker(_backup_dir(), args[0])
        return _run_cswap(["switch", args[0], *args[1:]], show_banner=True)
    if cmd == "switch":
        from . import accounts

        target_hint = args[1] if len(args) > 1 and args[1].isdigit() else "*"
        accounts.record_cli_switch_marker(_backup_dir(), target_hint)
        return _run_cswap(["switch", *args[1:]], show_banner=True)
    if cmd in {"-h", "--help", "help"}:
        return _cmd_help()
    if cmd in {"evo", "dex", "trophies", "companion", "badges", "pokedex", "forms"}:
        return _cmd_dex()
    if cmd in {"claim", "daily", "crate", "drop"}:
        ok, msg = belkins_game.claim_daily_supply_drop()
        print(f"{GREEN if ok else GOLD}{msg}{RESET}\n")
        _print_banner()
        return 0
    if cmd in {"raid", "attack", "strike", "boss", "battle"}:
        return _cmd_raid(args[1:])
    if cmd in {"morph", "evolve", "stage"}:
        return _cmd_morph(args[1:])
    if cmd in {"vault", "shop", "mart", "store", "armory"}:
        return _cmd_shop(args[1:])
    if cmd in {"buy", "equip", "unequip", "feed", "inject", "use"}:
        return _cmd_shop(args)
    if cmd in {"dashboard", "dash"}:
        return _cmd_dashboard()
    if cmd in {"bar", "widget", "menubar"}:
        return _cmd_bar(args[1:])
    if cmd in {"update", "upgrade", "self-update"}:
        return _cmd_update(args[1:])
    if cmd == "codex":
        return _cmd_codex(args[1:])
    if cmd in {"claude", "isolate", "locked"}:
        return _cmd_claude_session(args[1:])
    if cmd in {"shim", "shims"}:
        return _cmd_shim(args[1:])
    if cmd in {"roi", "value", "savings"}:
        return _cmd_roi(args[1:])
    if cmd in {"threshold", "thresh"}:
        return _cmd_threshold(args[1:])
    if cmd in {"model", "models"}:
        return _cmd_model(args[1:])
    if cmd in {"strategy", "strat"}:
        return _cmd_strategy(args[1:])
    if cmd in {"unpin", "unmap"}:
        return _cmd_unpin(args[1:])
    if cmd in {"sessions", "session", "thinking"}:
        return _cmd_sessions(args[1:])
    if cmd in {"fix-thinking", "sync-org", "restore-org"}:
        return _cmd_sessions(["--fix", *args[1:]])

    return _run_cswap(args, show_banner=False)


if __name__ == "__main__":
    raise SystemExit(main())
