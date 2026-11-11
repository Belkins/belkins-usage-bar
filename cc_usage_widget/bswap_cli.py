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


def _backup_dir() -> Path:
    try:
        from claude_swap import paths

        return Path(paths.get_backup_root())
    except Exception:
        return Path.home() / ".claude-swap-backup"


def _ensure_optimal_autoswitch_policy(backup_dir: Path) -> bool:
    """Ensure claude-swap's settings.json sets weeklyThreshold=98.0 and hysteresisPct>=12.0 to prevent premature cross-org switches."""
    s_path = backup_dir / "settings.json"
    try:
        raw = json.loads(s_path.read_text(encoding="utf-8")) if s_path.is_file() else {}
    except Exception:
        return False
    if not isinstance(raw, dict):
        return False
    auto = dict(raw.get("autoswitch") or {})
    changed = False
    if auto.get("weeklyThreshold") is None or float(auto.get("weeklyThreshold", 0) or 0) < 95.0:
        auto["weeklyThreshold"] = 98.0
        changed = True
    if float(auto.get("hysteresisPct", 10.0) or 10.0) < 12.0:
        auto["hysteresisPct"] = 12.0
        changed = True
    if changed:
        raw["autoswitch"] = auto
        raw.setdefault("schemaVersion", 1)
        try:
            from claude_swap.settings import atomic_write_json

            atomic_write_json(s_path, raw)
            return True
        except Exception:
            pass
    return False


def _slot_is_healthy_for_session(
    row: AccountRow | None,
    sentinels: dict[int, str],
    *,
    max_5h: float = 90.0,
    max_7d: float = 95.0,
) -> bool:
    if row is None or row.disabled or not row.switchable:
        return False
    if row.attention_note or row.slot in sentinels:
        return False
    fh = float(row.five_hour_pct or 0.0)
    wk = float(row.seven_day_pct or 0.0)
    scoped_max = max((float(pct) for _n, pct in (row.scoped_windows or ()) if pct is not None), default=0.0)
    return fh < max_5h and max(wk, scoped_max) < max_7d


def _cmd_sessions(args: list[str]) -> int:
    """Inspect active Claude Code sessions, their Preserved-Thinking organization owner, and optionally auto-fix/pin."""
    from . import accounts, fleet

    do_fix = "--fix" in args or "fix" in args
    bdir = _backup_dir()

    # Read-only inspection never mutates settings.json; only --fix / fix-thinking writes policy.
    policy_updated = _ensure_optimal_autoswitch_policy(bdir) if do_fix else False
    identities = fleet.account_identities(bdir)
    src = accounts.create_account_source()
    src.refresh(force=False)
    row_list = src.rows()
    rows = {r.slot: r for r in row_list}
    sentinels = src.sentinels()
    active = src.active()
    active_slot = active.slot if active else None

    snap = fleet.collect(backup_dir=bdir, active_slot=active_slot)
    _print_banner()
    print(f"  {BOLD}{ORANGE}🛡️  BELKINS PRESERVED-THINKING & SESSION QUALITY DIAGNOSTIC{RESET}")
    if policy_updated:
        print(f"  {GREEN}✓ Upgraded autoswitch policy: weeklyThreshold → 98.0%, hysteresisPct → 12.0%{RESET}")
    else:
        print(f"  {GREEN}✓ Thinking Guard: ACTIVE (concurrency-scaled busy hold · weekly ceiling 98%){RESET}")
    print()

    if not snap.sessions:
        print(f"  {DIM}No live interactive Claude Code sessions detected.{RESET}")
        return 0

    org_weights: dict[str, int] = {}
    mismatch_count = 0
    pinned_workspaces: set[str] = set()
    for s in snap.sessions:
        cwd_p = Path(s.cwd) if s.cwd else Path.cwd()
        short_w = cwd_p.name or s.cwd
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

        mode_badge = f"{CYAN}📌 PINNED{RESET}" if s.pinned else (f"{GOLD}⚠ ORG SHIFTED{RESET}" if mismatch else f"{GREEN}● MATCHED{RESET}")
        print(f"  PID {s.pid:<6} {BOLD}{short_w:<18}{RESET} [{s.status or 'idle':<7}] {mode_badge}  cur={cur_lbl}  thinking_org={owner_lbl}")

        if do_fix and (not s.pinned) and owner_slot is not None and s.cwd and "/private/tmp/" not in s.cwd:
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
        if cand_org == active_org and _slot_is_healthy_for_session(active, sentinels):
            best_owner = active
            break
        same_org_rows = [
            r for r in row_list
            if identities.get(r.slot, ("", ""))[1] == cand_org
            and _slot_is_healthy_for_session(r, sentinels)
        ]
        cand = accounts.harvest_best_slot(
            same_org_rows,
            sentinels=sentinels,
            identities=identities,
            prefer_org=cand_org,
        )
        if cand is not None:
            best_owner = cand
            break

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
        print(
            f"\n  {GREEN}✓ Global login Slot #{active_slot} ({active.alias if active else ''}) already matches your dominant busy sessions!{RESET}\n"
            f"  {DIM}💡 Launch separate workspaces with `{ORANGE}bswap claude{DIM}` so each project stays locked to its own slot.{RESET}"
        )
    else:
        print(f"\n  {GREEN}✓ All active sessions match their thinking organization!{RESET}")
    return 0


def _cmd_claude_session(args: list[str]) -> int:
    """Launch Claude Code inside an isolated per-account profile (`cswap run --share-history`) locked to the workspace's thinking org."""
    from . import accounts, fleet

    cswap_bin = _find_cswap()
    if cswap_bin is None:
        print(f"{RED}cswap backend not found.{RESET}", file=sys.stderr)
        return 1

    bdir = _backup_dir()
    identities = fleet.account_identities(bdir)
    src = accounts.create_account_source()
    src.refresh(force=False)
    row_list = src.rows()
    rows = {r.slot: r for r in row_list}
    sentinels = src.sentinels()
    active = src.active()
    active_slot = active.slot if active else None

    explicit_target = False
    target_slot: str | None = None
    claude_args = list(args)
    if claude_args and not claude_args[0].startswith("-"):
        first = claude_args[0]
        matched_alias = next((r for r in row_list if r.alias.lower() == first.lower()), None)
        if first.isdigit() or matched_alias is not None:
            target_slot = str(matched_alias.slot) if matched_alias is not None else first
            explicit_target = True
            claude_args = claude_args[1:]
            if claude_args and claude_args[0] == "--":
                claude_args = claude_args[1:]

    cwd = Path.cwd()
    prefer_org: str | None = None
    if target_slot is None:
        # 1. Honour directory pin or transcript organization owner when healthy
        mappings = fleet.load_mappings(bdir, identities)
        mapped = fleet._mapping_for(str(cwd), mappings)
        if mapped is not None and mapped.slot is not None and _slot_is_healthy_for_session(
            rows.get(mapped.slot), sentinels, max_5h=92.0, max_7d=96.0
        ):
            r = rows[mapped.slot]
            target_slot = str(r.slot)
            print(
                f"{GREEN}🛡️  Locked to Pinned Slot #{r.slot} ({r.alias}){RESET} "
                f"{DIM}(directory mapping){RESET}"
            )
        else:
            owner_slot, _shifted = fleet.detect_workspace_owner_slot(
                cwd, identities, current_slot=active_slot
            )
            if owner_slot is not None:
                prefer_org = identities.get(owner_slot, ("", ""))[1] or None
                same_org_candidates = [
                    r for r in row_list
                    if prefer_org and identities.get(r.slot, ("", ""))[1] == prefer_org
                    and _slot_is_healthy_for_session(r, sentinels, max_5h=92.0, max_7d=96.0)
                ]
                chosen_org_row = accounts.harvest_best_slot(
                    same_org_candidates,
                    sentinels=sentinels,
                    identities=identities,
                    prefer_org=prefer_org,
                )
                if chosen_org_row is not None:
                    target_slot = str(chosen_org_row.slot)
                    print(
                        f"{GREEN}🛡️  Locked to Slot #{chosen_org_row.slot} ({chosen_org_row.alias}){RESET} "
                        f"{DIM}(matches this workspace's Preserved Thinking organization){RESET}"
                    )

    if target_slot is None:
        # 2. Pick healthiest slot via Perishable-Quota & Org-Aware Harvester
        chosen = accounts.harvest_best_slot(
            row_list,
            sentinels=sentinels,
            identities=identities,
            prefer_org=prefer_org,
        )
        if chosen is not None:
            target_slot = str(chosen.slot)
            print(
                f"{GREEN}🛡️  Launching isolated session on Slot #{chosen.slot} ({chosen.alias}){RESET} "
                f"{DIM}(perishable-quota & org-aware selection){RESET}"
            )
        else:
            target_slot = str(active_slot if active_slot is not None else 1)

    # Guarantee true CLAUDE_CONFIG_DIR isolation without creating duplicate refresh tokens:
    # `cswap run N` falls back to unisolated `~/.claude` if N is currently the active default login.
    # When no busy default-profile sessions hold `~/.claude`, rotate the global default login onto
    # another healthy slot first so N runs inside `<backup>/sessions/N-.../` with shared history!
    if active_slot is not None and target_slot == str(active_slot):
        other_healthy = [
            r for r in row_list
            if r.slot != active_slot and _slot_is_healthy_for_session(r, sentinels, max_5h=95.0, max_7d=99.0)
        ]
        try:
            default_recs, _ = fleet.read_registry(fleet.sessions_dir())
            default_busy = sum(1 for rec in default_recs if str(rec.get("status") or "").lower() == "busy")
        except Exception:
            default_busy = 0
        if other_healthy and default_busy == 0:
            standby = accounts.harvest_best_slot(other_healthy, sentinels=sentinels, identities=identities)
            if standby is not None:
                accounts.record_cli_switch_marker(bdir, str(standby.slot))
                src.switch_to(str(standby.slot))
        elif other_healthy and not explicit_target and not prefer_org:
            alt = accounts.harvest_best_slot(other_healthy, sentinels=sentinels, identities=identities)
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
                "# Belkins Usage Bar — isolated org-locked Claude wrapper shim",
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
        print(f"  • {BOLD}claude{RESET} → auto-isolates sessions via `bswap claude` (recursion-safe)")
        print(f"  • {BOLD}codex{RESET}  → routes `CODEX_HOME` to healthiest seat via `codex_accounts best`")
        print(f"\n  {GOLD}Add to ~/.zshrc to activate:{RESET}\n    {ORANGE}{path_export}{RESET}")
        return 0

    installed = claude_shim.is_file() and codex_shim.is_file()
    in_path = str(SHIMS_DIR) in (os.environ.get("PATH") or "").split(":")
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
    """View or update claude-swap's autoswitch.threshold (50..99%)."""
    from . import accounts

    src = accounts.create_account_source()
    if not args:
        cur = src.autoswitch_threshold()
        lbl = f"{cur:.0f}%" if cur is not None else "default (90%)"
        print(f"  {BOLD}Auto-switch threshold:{RESET} {ORANGE}{lbl}{RESET}  {DIM}(change with `bswap threshold <50..99>`){RESET}")
        return 0
    try:
        pct = float(args[0].rstrip("%"))
    except ValueError:
        print(f"{RED}Usage: bswap threshold <50..99>{RESET}", file=sys.stderr)
        return 2
    if src.set_autoswitch_threshold(pct):
        clamped = min(99.0, max(50.0, pct))
        print(f"{GREEN}✓ Auto-switch threshold set to {BOLD}{clamped:.0f}%{RESET}{GREEN} (live in menu bar & claude-swap){RESET}")
        return 0
    print(f"{RED}✗ Failed to update threshold: {src.last_error or 'unknown error'}{RESET}", file=sys.stderr)
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
    print(f"  {ORANGE}bswap claude [slot] [args]{RESET} Launch Claude Code in an org-locked profile (never drops thinking!)")
    print(f"  {ORANGE}bswap shim [on|off|status]{RESET} Install/remove auto-isolating `claude` & `codex` CLI shims")
    print(f"  {ORANGE}bswap fix-thinking{RESET}         Diagnose & restore session orgs + set 98% weekly switch guard")
    print(f"  {ORANGE}bswap sessions{RESET}             Inspect live Claude sessions & their Preserved-Thinking org owners")
    print(f"  {ORANGE}bswap unpin [dir]{RESET}          Remove workspace directory pin (defaults to current directory)")
    print(f"  {ORANGE}bswap threshold [50..99]{RESET}   View or set live Claude auto-switch threshold %")
    print(f"  {ORANGE}bswap strategy [best|consume-first]{RESET} Set auto-switch rotation strategy")
    print(f"  {ORANGE}bswap <slot|alias>{RESET}         Switch active Claude account to slot number or alias")
    print(f"  {ORANGE}bswap best{RESET}                 Switch to the healthiest Claude account with most room")
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
        from . import accounts

        accounts.record_cli_switch_marker(_backup_dir(), "*")
        return _run_cswap(["switch", "--strategy", "best", *args[1:]], show_banner=True)
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
