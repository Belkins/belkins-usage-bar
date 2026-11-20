"""Tests for Fable reactive limit-hit wakeup, burn-ETA UX, latched-session UI & alerts.

Covers:
  1. `app.py` reactive local `hit_rate_limit` failover wakeup (deduped per
     `(pid, session_id)`, respects `autoswitch_enabled`, prunes on recovery,
     avoids double-switch when pre-fleet autoswitch already switched, degrades
     cleanly when adapter lacks new hooks).
  2. `app.py` cached `fable_burn_eta_minutes` / `fable_burn_rate_pct_per_min`
     on `UiSnapshot` + `copy_text()` guarded by `CC_USAGE_WIDGET_NO_REVEAL`.
  3. `themes/belkins.py` `Active Sessions` `⚡ LATCHED` tag + `Copy Resume` pill
     (including empty/unsafe `session_id` handling and `_LAST_CFG_NOTE`),
     Active Claude Hero burn-ETA tooltip (`<= 240m`) & caption (`<= 45m`), and
     `Hub` top-2 standby + Bento switcher skipping `NO FABLE` seats when
     `belkins_fable_guard` is ON.
  4. `notify.py` deduped latched-session alert (per `(pid, session_id)`,
     surviving restarts, held on fleet read error, re-armed after recovery) and
     deduped rapid Fable burn warning (`< 12m` with `autoswitch_enabled=False`
     only, with 15m hysteresis).
  5. Graceful degradation when `claude_swap` is masked (`sys.modules['claude_swap'] = None`).

Run directly::

    CC_USAGE_WIDGET_NO_REVEAL=1 ~/.local/share/uv/tools/claude-swap/bin/python tests/test_fable_ui_alerts.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import belkins_game  # noqa: E402
from cc_usage_widget import fleet  # noqa: E402
from cc_usage_widget import notify as notify_mod  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    VENDOR_CLAUDE,
    AccountRow,
    normalize_settings,
)
from cc_usage_widget.themes import ThemeActions  # noqa: E402
from cc_usage_widget.themes import belkins as belkins_theme  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _arow(
    slot: int,
    alias: str,
    *,
    is_active: bool = False,
    five_hour_pct: float | None = 10.0,
    seven_day_pct: float | None = 25.0,
    scoped_windows: tuple[tuple[str, float], ...] = (),
    plan_type: str = "claude_max_20x",
    disabled: bool = False,
    **kwargs: Any,
) -> AccountRow:
    email = kwargs.pop("email", f"{alias}@belkins.io")
    return AccountRow(
        slot=slot,
        alias=alias,
        email=email,
        is_active=is_active,
        five_hour_pct=five_hour_pct,
        seven_day_pct=seven_day_pct,
        scoped_windows=scoped_windows,
        plan_type=plan_type,
        disabled=disabled,
        vendor=VENDOR_CLAUDE,
        **kwargs,
    )


class _RecMeasure:
    """Recording Pen stand-in (not a ``_Measure`` subclass so inner-panel hero cards paint into it)."""

    hl = False

    def __init__(self) -> None:
        self._m = belkins_theme._Measure()
        self.texts: list[str] = []
        self.pills: list[str] = []

    def width(self, *a: Any, **k: Any) -> float:
        return self._m.width(*a, **k)

    def fit(self, *a: Any, **k: Any) -> str:
        return self._m.fit(*a, **k)

    def text(self, s: str, *a: Any, **k: Any) -> float:
        if s:
            self.texts.append(s)
        return self._m.text(s, *a, **k)

    def chip(self, text_s: str, *a: Any, **k: Any) -> float:
        if text_s:
            self.texts.append(text_s)
        return self._m.chip(text_s, *a, **k)

    def pill(self, text_s: str, *a: Any, **k: Any) -> float:
        if text_s:
            self.texts.append(text_s)
            self.pills.append(text_s)
        return self._m.pill(text_s, *a, **k)

    def symbol(self, *a: Any, **k: Any) -> float:
        return self._m.symbol(*a, **k)

    def _noop(self, *a: Any, **k: Any) -> None:
        return None

    rrect = panel = bar = circle = line = _noop


def _fake_session(
    pid: int,
    cwd: str,
    *,
    session_id: str = "",
    latched_credits: bool = False,
    hit_rate_limit: bool = False,
    status: str = "idle",
    slot: int | None = 2,
    pinned: bool = False,
    owner_slot: int | None = None,
    org_shifted: bool = False,
) -> Any:
    return SimpleNamespace(
        pid=pid,
        cwd=cwd,
        name="",
        status=status,
        started_at=0.0,
        slot=slot,
        pinned=pinned,
        mapped_slot=None,
        mapped_email="",
        pinned_here=False,
        owner_slot=owner_slot,
        org_shifted=org_shifted,
        session_id=session_id,
        latched_credits=latched_credits,
        hit_rate_limit=hit_rate_limit,
    )


class _FakeAccounts:
    """Minimal AccountSource test double with engine's Fable burn & wakeup hooks."""

    def __init__(
        self,
        rows: list[AccountRow],
        *,
        autoswitch: bool = True,
        eta_min: float | None = 18.0,
        burn_rate: float | None = 1.4,
    ) -> None:
        self._rows = list(rows)
        self._autoswitch = autoswitch
        self._eta_min = eta_min
        self._burn_rate = burn_rate
        self.notify_calls: list[str] = []
        self.eval_calls: int = 0
        self.refresh_forces: list[bool] = []
        self._armed_for_switch = False
        self._pre_fleet_switch = False

    def refresh(self, *, force: bool = False) -> None:
        self.refresh_forces.append(bool(force))

    def rows(self) -> tuple[AccountRow, ...]:
        return tuple(self._rows)

    def active(self) -> AccountRow | None:
        return next((r for r in self._rows if r.is_active), None)

    def autoswitch_enabled(self) -> bool:
        return self._autoswitch

    def notify_local_limit_hit(self, kind: str = "rate_limit") -> None:
        self.notify_calls.append(kind)
        self._armed_for_switch = True

    def evaluate_autoswitch(self) -> str | None:
        self.eval_calls += 1
        if self._pre_fleet_switch:
            self._pre_fleet_switch = False
            self._activate_slot(7)
            return "ana"
        if self._armed_for_switch:
            self._armed_for_switch = False
            self._activate_slot(7)
            return "ana"
        return None

    def _activate_slot(self, target_slot: int) -> None:
        self._rows = [replace(r, is_active=(r.slot == target_slot)) for r in self._rows]

    def fable_burn_eta_minutes(self, slot: int | None = None) -> float | None:
        del slot
        return self._eta_min

    def fable_burn_rate_pct_per_min(self, slot: int | None = None) -> float | None:
        del slot
        return self._burn_rate


# ---------------------------------------------------------------------------
# 1. App: reactive hit_rate_limit failover wakeup & burn telemetry
# ---------------------------------------------------------------------------


def test_reactive_hit_rate_limit_immediate_failover_and_dedup() -> None:
    vlad = _arow(2, "vlad", is_active=True, scoped_windows=(("Fable", 96.0),))
    ana = _arow(7, "ana", is_active=False, scoped_windows=(("Fable", 20.0),))
    accounts = _FakeAccounts([vlad, ana], autoswitch=True, eta_min=14.5, burn_rate=1.8)
    published: list[app_mod.UiSnapshot] = []

    worker = app_mod.BackgroundWorker(
        publish=published.append,
        snapshot=app_mod.UiSnapshot(settings=dict(SETTINGS_DEFAULTS)),
        accounts=accounts,  # type: ignore[arg-type]
        sources=(),
    )

    current_sessions: list[Any] = [
        _fake_session(501, "/Users/dev/usagebar", session_id="sess-aaa", hit_rate_limit=True)
    ]
    worker._collect_fleet = lambda _rows, _active: fleet.FleetSnapshot(  # type: ignore[method-assign]
        sessions=tuple(current_sessions),
    )

    # Tick 1: newly observed hit_rate_limit triggers notify_local_limit_hit + immediate failover
    worker._run_accounts_job(force=False)
    assert accounts.notify_calls == ["rate_limit"], accounts.notify_calls
    assert accounts.eval_calls == 2, accounts.eval_calls
    assert published[-1].active is not None and published[-1].active.slot == 7
    assert published[-1].fable_burn_eta_minutes == 14.5
    assert published[-1].fable_burn_rate_pct_per_min == 1.8

    # Tick 2: same (pid, session_id) still has hit_rate_limit=True -> deduped, no second wakeup!
    worker._run_accounts_job(force=False)
    assert accounts.notify_calls == ["rate_limit"], accounts.notify_calls
    assert accounts.eval_calls == 3, accounts.eval_calls  # only the regular pre-fleet eval

    # Tick 3: session recovers -> dedup key pruned
    current_sessions[:] = [
        _fake_session(501, "/Users/dev/usagebar", session_id="sess-aaa", hit_rate_limit=False)
    ]
    worker._run_accounts_job(force=False)
    assert accounts.notify_calls == ["rate_limit"]

    # Tick 4: same session hits rate limit again later -> fires again for the new episode!
    accounts._activate_slot(2)
    current_sessions[:] = [
        _fake_session(501, "/Users/dev/usagebar", session_id="sess-aaa", hit_rate_limit=True)
    ]
    worker._run_accounts_job(force=False)
    assert accounts.notify_calls == ["rate_limit", "rate_limit"], accounts.notify_calls
    assert published[-1].active is not None and published[-1].active.slot == 7

    # Tick 5: autoswitch OFF -> never calls notify_local_limit_hit or evaluate_autoswitch!
    accounts._autoswitch = False
    prev_evals = accounts.eval_calls
    current_sessions[:] = [
        _fake_session(602, "/Users/dev/other", session_id="sess-bbb", hit_rate_limit=True)
    ]
    worker._run_accounts_job(force=False)
    assert accounts.notify_calls == ["rate_limit", "rate_limit"]
    assert accounts.eval_calls == prev_evals


def test_reactive_wakeup_skips_double_switch_and_handles_legacy_adapter() -> None:
    vlad = _arow(2, "vlad", is_active=True, scoped_windows=(("Fable", 99.0),))
    ana = _arow(7, "ana", is_active=False, scoped_windows=(("Fable", 15.0),))
    accounts = _FakeAccounts([vlad, ana], autoswitch=True, eta_min=float("nan"), burn_rate=-3.0)
    accounts._pre_fleet_switch = True
    published: list[app_mod.UiSnapshot] = []

    worker = app_mod.BackgroundWorker(
        publish=published.append,
        snapshot=app_mod.UiSnapshot(settings=dict(SETTINGS_DEFAULTS)),
        accounts=accounts,  # type: ignore[arg-type]
        sources=(),
    )
    worker._collect_fleet = lambda _rows, _active: fleet.FleetSnapshot(  # type: ignore[method-assign]
        sessions=(_fake_session(701, "/Users/dev/app", session_id="sess-pre", hit_rate_limit=True),),
    )

    # Pre-fleet autoswitch already switched -> no redundant second switch on reactive path
    worker._run_accounts_job(force=False)
    assert accounts.notify_calls == [], accounts.notify_calls
    assert accounts.eval_calls == 1, accounts.eval_calls
    assert published[-1].active is not None and published[-1].active.slot == 7
    # NaN ETA and negative rate sanitize cleanly to None
    assert published[-1].fable_burn_eta_minutes is None
    assert published[-1].fable_burn_rate_pct_per_min is None

    # Next tick with the same stale session still marked seen -> still no wakeup
    worker._run_accounts_job(force=False)
    assert accounts.notify_calls == []

    # Legacy adapter without notify_local_limit_hit / fable_burn_* degrades cleanly
    class _LegacyAccounts:
        def refresh(self, *, force: bool = False) -> None:
            pass

        def rows(self) -> tuple[AccountRow, ...]:
            return (vlad, ana)

        def active(self) -> AccountRow:
            return vlad

        def autoswitch_enabled(self) -> bool:
            return True

        def evaluate_autoswitch(self) -> str | None:
            return None

    legacy_pub: list[app_mod.UiSnapshot] = []
    legacy_worker = app_mod.BackgroundWorker(
        publish=legacy_pub.append,
        snapshot=app_mod.UiSnapshot(settings=dict(SETTINGS_DEFAULTS)),
        accounts=_LegacyAccounts(),  # type: ignore[arg-type]
        sources=(),
    )
    legacy_worker._collect_fleet = lambda _r, _a: fleet.FleetSnapshot(  # type: ignore[method-assign]
        sessions=(_fake_session(801, "/Users/dev/app", session_id="s-old", hit_rate_limit=True),),
    )
    legacy_worker._run_accounts_job(force=False)
    assert legacy_pub and legacy_pub[-1].accounts_error is None
    assert legacy_pub[-1].fable_burn_eta_minutes is None
    assert legacy_pub[-1].fable_burn_rate_pct_per_min is None


def test_app_copy_text_respects_no_reveal_and_uses_pbcopy() -> None:
    # Default in tests: CC_USAGE_WIDGET_NO_REVEAL=1 -> suppressed, never spawns pbcopy
    called: list[Any] = []
    orig_run = app_mod.subprocess.run
    try:
        def _forbid_run(*a: Any, **kw: Any) -> Any:
            called.append((a, kw))
            return SimpleNamespace(returncode=0)

        app_mod.subprocess.run = _forbid_run  # type: ignore[assignment]
        assert os.environ.get("CC_USAGE_WIDGET_NO_REVEAL")
        assert app_mod.copy_text("claude --resume abc-123") is False
        assert called == []

        # When CC_USAGE_WIDGET_NO_REVEAL is unset, pbcopy receives exact UTF-8 payload
        prev_env = os.environ.pop("CC_USAGE_WIDGET_NO_REVEAL", None)
        try:
            assert app_mod.copy_text("") is False
            assert app_mod.copy_text("claude --resume abc-123") is True
            assert len(called) == 1
            args, kwargs = called[0]
            assert args[0] == ["pbcopy"]
            assert kwargs["input"] == b"claude --resume abc-123"
        finally:
            if prev_env is not None:
                os.environ["CC_USAGE_WIDGET_NO_REVEAL"] = prev_env
    finally:
        app_mod.subprocess.run = orig_run


# ---------------------------------------------------------------------------
# 2. Belkins Theme: Latched Session, Burn ETA, Hub Fable Guard
# ---------------------------------------------------------------------------


def test_belkins_latched_session_pill_and_copy_resume() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig_path = belkins_game._shop_save_path
        orig_note = belkins_theme._LAST_CFG_NOTE
        orig_tab = belkins_theme.get_active_tab()
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_theme._LAST_CFG_NOTE = ""

            rows = (
                _arow(2, "vlad", is_active=True, scoped_windows=(("Fable", 40.0),)),
                _arow(7, "ana", is_active=False, scoped_windows=(("Fable", 15.0),)),
            )
            copied: list[str] = []

            # Inject copy_text via a SimpleNamespace wrapper around ThemeActions fields
            base_acts = ThemeActions()
            acts = SimpleNamespace(
                **{k: getattr(base_acts, k) for k in ThemeActions.__dataclass_fields__},
                copy_text=lambda txt: (copied.append(txt), True)[1],
            )

            sid = "550e8400-e29b-41d4-a716-446655440000"
            sess_with_sid = _fake_session(
                1001,
                "/Users/dev/usagebar",
                session_id=sid,
                latched_credits=True,
                org_shifted=True,
                owner_slot=7,
            )
            sess_empty_sid = _fake_session(
                1002,
                "/Users/dev/no-sid-proj",
                session_id="",
                latched_credits=True,
            )
            sess_unsafe_sid = _fake_session(
                1003,
                "/Users/dev/bad-sid-proj",
                session_id="abc; rm -rf /",
                latched_credits=True,
            )
            snap = app_mod.UiSnapshot(
                settings=normalize_settings({**SETTINGS_DEFAULTS, "menu_theme": "belkins"}),
                accounts=rows,
                active=rows[0],
                sessions=(sess_with_sid, sess_empty_sid, sess_unsafe_sid),
            )
            blocks = belkins_theme.build(snap, time.time(), acts)  # type: ignore[arg-type]
            sess_blocks = [
                b for b in blocks if b.key.startswith("belkins-claude-session-dir-")
            ]
            assert len(sess_blocks) == 3, [b.key for b in sess_blocks]

            # Paint session blocks and inspect rendered pills
            rec = _RecMeasure()
            for blk in sess_blocks:
                assert blk.paint is not None
                blk.paint(rec, belkins_theme.W, blk.height)

            assert "⚡ LATCHED" in rec.pills, rec.pills
            assert "Copy Resume" in rec.pills, rec.pills
            assert "⚠ ORG SHIFTED" not in rec.pills, rec.pills

            # Clicking row 0 copies exact `claude --resume <sid>` & updates _LAST_CFG_NOTE
            assert callable(sess_blocks[0].action)
            sess_blocks[0].action()
            assert copied[-1] == f"claude --resume {sid}", copied
            assert (
                belkins_theme._LAST_CFG_NOTE
                == "Copied `claude --resume …` — paste in terminal to clear credits latch ✓"
            ), belkins_theme._LAST_CFG_NOTE

            # Clicking row 1 (empty session_id) copies plain `claude --resume`
            assert callable(sess_blocks[1].action)
            sess_blocks[1].action()
            assert copied[-1] == "claude --resume", copied

            # Clicking row 2 (unsafe session_id) falls back safely to plain `claude --resume`
            assert callable(sess_blocks[2].action)
            sess_blocks[2].action()
            assert copied[-1] == "claude --resume", copied
        finally:
            belkins_theme.set_active_tab(orig_tab)
            belkins_theme._LAST_CFG_NOTE = orig_note
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_game._shop_save_path = orig_path


def test_belkins_fable_burn_eta_tooltip_and_caption() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig_path = belkins_game._shop_save_path
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            belkins_theme._LIVE_SETTINGS.clear()

            vlad = _arow(
                2,
                "vlad",
                is_active=True,
                five_hour_pct=30.0,
                seven_day_pct=55.0,
                scoped_windows=(("Fable", 73.0),),
            )
            ana = _arow(7, "ana", is_active=False, scoped_windows=(("Fable", 20.0),))
            now_ts = time.time()

            # 1. Rapid burn (18m <= 45m): tooltip AND subtle caption on both Hub & Claude hero
            snap_hot = app_mod.UiSnapshot(
                settings=normalize_settings({**SETTINGS_DEFAULTS, "menu_theme": "belkins"}),
                accounts=(vlad, ana),
                active=vlad,
                fable_burn_eta_minutes=18.0,
                fable_burn_rate_pct_per_min=1.5,
            )
            blocks_hot = belkins_theme.build(snap_hot, now_ts, ThemeActions())
            claude_hero = next(b for b in blocks_hot if b.key == "belkins-claude-active")
            hub_hero = next(b for b in blocks_hot if b.key == "belkins-hub-claude-active")
            assert "~18m to wall" in claude_hero.tooltip, claude_hero.tooltip
            assert "1.5%/min" in claude_hero.tooltip, claude_hero.tooltip

            rec_claude = _RecMeasure()
            claude_hero.paint(rec_claude, belkins_theme.W, claude_hero.height)
            assert any("~18m to wall" in t for t in rec_claude.texts), rec_claude.texts

            rec_hub = _RecMeasure()
            hub_hero.paint(rec_hub, belkins_theme.W, hub_hero.height)
            assert any("~18m to wall" in t for t in rec_hub.texts), rec_hub.texts

            # 2. Moderate burn (120m: > 45m, <= 240m): tooltip shows ETA, hero caption does NOT
            snap_mod = replace(
                snap_hot,
                fable_burn_eta_minutes=120.0,
                fable_burn_rate_pct_per_min=0.4,
            )
            blocks_mod = belkins_theme.build(snap_mod, now_ts, ThemeActions())
            claude_hero_mod = next(b for b in blocks_mod if b.key == "belkins-claude-active")
            assert "~2h to wall" in claude_hero_mod.tooltip, claude_hero_mod.tooltip
            rec_mod = _RecMeasure()
            claude_hero_mod.paint(rec_mod, belkins_theme.W, claude_hero_mod.height)
            assert not any("to wall" in t for t in rec_mod.texts), rec_mod.texts

            # 3. Very slow burn (300m > 240m) or None: neither tooltip nor caption shows ETA
            for eta_val in (300.0, None, float("nan"), -5.0):
                snap_cold = replace(snap_hot, fable_burn_eta_minutes=eta_val)
                blocks_cold = belkins_theme.build(snap_cold, now_ts, ThemeActions())
                hero_cold = next(b for b in blocks_cold if b.key == "belkins-claude-active")
                assert "to wall" not in hero_cold.tooltip, (eta_val, hero_cold.tooltip)
        finally:
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_game._shop_save_path = orig_path


def test_belkins_hub_top2_standby_skips_no_fable_seats() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig_path = belkins_game._shop_save_path
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            belkins_theme._LIVE_SETTINGS.clear()

            vlad = _arow(
                2,
                "vlad",
                is_active=True,
                five_hour_pct=10.0,
                seven_day_pct=53.0,
                scoped_windows=(("Fable", 92.0),),
                plan_type="claude_max_20x",
            )
            work1_team = _arow(
                4,
                "work1",
                is_active=False,
                five_hour_pct=16.0,
                seven_day_pct=21.0,
                scoped_windows=(),
                plan_type="claude_team",
            )
            ana = _arow(
                7,
                "sm.anastasiia.g",
                is_active=False,
                five_hour_pct=0.0,
                seven_day_pct=24.0,
                scoped_windows=(("Fable", 45.0),),
                plan_type="claude_max_20x",
            )
            d_capped = _arow(
                9,
                "d.yagmurova2",
                is_active=False,
                five_hour_pct=0.0,
                seven_day_pct=23.0,
                scoped_windows=(("Fable", 100.0),),
                plan_type="claude_max_20x",
            )

            now_ts = time.time()
            snap_guarded = app_mod.UiSnapshot(
                settings=normalize_settings(
                    {
                        **SETTINGS_DEFAULTS,
                        "menu_theme": "belkins",
                        "belkins_fable_guard": True,
                        "belkins_hub_view": "luxury",
                    }
                ),
                accounts=(vlad, work1_team, ana, d_capped),
                active=vlad,
                autoswitch_models=("all",),
            )
            blocks_guarded = belkins_theme.build(snap_guarded, now_ts, ThemeActions())
            hub_row_keys = [
                b.key
                for b in blocks_guarded
                if b.key.startswith("belkins-hub-claude-")
                and b.key not in (
                    "belkins-hub-claude-head",
                    "belkins-hub-claude-active",
                    "belkins-hub-claude-rule",
                    "belkins-hub-claude-attn-rule",
                    "belkins-hub-claude-attn-grouped",
                    "belkins-hub-claude-foot",
                )
            ]
            # Only Fable seat #7 appears in Hub top-2 standby; #4 work1 (NO FABLE) is NEVER shown!
            assert hub_row_keys == ["belkins-hub-claude-7"], hub_row_keys

            # Full Claude tab still lists #4 work1 with NO FABLE badge for manual Sonnet/Opus use
            claude_tab_row4 = next(
                b for b in blocks_guarded if b.key == "belkins-claude-4"
            )
            rec_r4 = _RecMeasure()
            claude_tab_row4.paint(rec_r4, belkins_theme.W, claude_tab_row4.height)
            assert "NO FABLE" in rec_r4.pills, rec_r4.pills

            # Bento helper never falls back to a NO FABLE row even when all Fable standbys are disabled
            snap_only_team_enabled = replace(
                snap_guarded,
                accounts=(vlad, work1_team, replace(ana, disabled=True)),
            )
            claude_spec_only_team = belkins_theme._claude_spec(
                app_mod,
                __import__("cc_usage_widget.render", fromlist=["render"]),
                snap_only_team_enabled,
                now_ts,
                ThemeActions(),
            )
            assert belkins_theme._best_bento_switch_row(claude_spec_only_team) is None

            # Turning belkins_fable_guard OFF makes #4 work1 eligible again on Hub
            snap_unguarded = replace(
                snap_guarded,
                settings=normalize_settings(
                    {
                        **SETTINGS_DEFAULTS,
                        "menu_theme": "belkins",
                        "belkins_fable_guard": False,
                        "belkins_hub_view": "luxury",
                    }
                ),
            )
            blocks_unguarded = belkins_theme.build(snap_unguarded, now_ts, ThemeActions())
            hub_keys_off = [
                b.key
                for b in blocks_unguarded
                if b.key.startswith("belkins-hub-claude-")
                and b.key.rsplit("-", 1)[-1].isdigit()
            ]
            assert "belkins-hub-claude-4" in hub_keys_off, hub_keys_off
        finally:
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_game._shop_save_path = orig_path


# ---------------------------------------------------------------------------
# 3. Notify: Latched Session Alert & Rapid Fable Burn Warning
# ---------------------------------------------------------------------------


def test_notify_latched_session_alert_dedup_restart_and_rearm() -> None:
    d_row = _arow(9, "d.yagmurova2", is_active=True, scoped_windows=(("Fable", 35.0),))
    unlatched = app_mod.UiSnapshot(
        accounts=(d_row,),
        active=d_row,
        sessions=(_fake_session(101, "/Users/dev/usagebar", session_id="sess-1", latched_credits=False),),
    )
    latched = replace(
        unlatched,
        sessions=(_fake_session(101, "/Users/dev/usagebar", session_id="sess-1", latched_credits=True),),
    )

    # Pure transition detection
    ev1 = notify_mod.detect(unlatched, latched)
    assert [e.kind for e in ev1] == ["latched"], ev1
    assert ev1[0].key == "latched:101:sess-1"
    assert "usagebar" in ev1[0].message
    assert "/exit && claude --resume" in ev1[0].message
    assert "d.yagmurova2" in ev1[0].message
    assert "/Users/dev/" not in ev1[0].message  # basename only, never full path

    # Still latched on next tick -> 0 events
    assert notify_mod.detect(latched, latched) == []

    # Stateful Notifier + ledger dedup across restarts, fleet errors, and re-arm after recovery
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "notify_state.json"
        clock = [1_000.0]
        sent: list[notify_mod.Event] = []
        settings_on = normalize_settings({**SETTINGS_DEFAULTS, "notifications_enabled": True})

        def _make_notifier() -> notify_mod.Notifier:
            return notify_mod.Notifier(
                settings=lambda: settings_on,
                ledger_path=ledger_path,
                credentials_path=Path(tmp) / "notify.json",
                mac_sender=SimpleNamespace(send=lambda e: (sent.append(e), True)[1]),
                clock=lambda: clock[0],
                dispatch=lambda work: work(),
            )

        n1 = _make_notifier()
        assert len(n1.notify(unlatched, latched)) == 1
        assert len(sent) == 1

        # Simulated restart with previous=None -> suppressed by persisted ledger!
        n2 = _make_notifier()
        assert n2.notify(None, latched) == []
        assert len(sent) == 1

        # A second distinct session latching notifies once for that second session
        two_latched = replace(
            latched,
            sessions=(
                _fake_session(101, "/Users/dev/usagebar", session_id="sess-1", latched_credits=True),
                _fake_session(202, "/Users/dev/crm", session_id="sess-2", latched_credits=True),
            ),
        )
        assert [e.key for e in n2.notify(latched, two_latched)] == ["latched:202:sess-2"]
        assert len(sent) == 2

        # Transient fleet-unreadable tick does NOT release held keys
        clock[0] += notify_mod.THROTTLE_SECONDS + 10.0
        fleet_err = replace(unlatched, sessions=(), fleet_notes=("fleet unreadable: OSError",))
        assert n2.notify(two_latched, fleet_err) == []
        assert n2.notify(fleet_err, latched) == []  # still suppressed!

        # Clean recovery past throttle releases the key so a future latch episode notifies again
        clock[0] += notify_mod.THROTTLE_SECONDS + 10.0
        assert n2.notify(latched, unlatched) == []
        clock[0] += notify_mod.THROTTLE_SECONDS + 10.0
        assert [e.key for e in n2.notify(unlatched, latched)] == ["latched:101:sess-1"]
        assert len(sent) == 3


def test_notify_rapid_fable_burn_warning_only_when_autoswitch_off_and_hysteresis() -> None:
    vlad = _arow(2, "vlad", is_active=True, scoped_windows=(("Fable", 86.0),))
    base = app_mod.UiSnapshot(
        accounts=(vlad,),
        active=vlad,
        autoswitch_enabled=False,
        fable_burn_eta_minutes=20.0,
        fable_burn_rate_pct_per_min=0.7,
    )
    hot = replace(base, fable_burn_eta_minutes=9.0, fable_burn_rate_pct_per_min=1.6)
    hotter = replace(base, fable_burn_eta_minutes=6.0, fable_burn_rate_pct_per_min=2.1)
    auto_on_hot = replace(hot, autoswitch_enabled=True)

    # Auto-switch ON -> silent
    assert notify_mod.detect(replace(base, autoswitch_enabled=True), auto_on_hot) == []

    # Auto-switch OFF & ETA < 12m -> fires once
    ev = notify_mod.detect(base, hot)
    assert [e.kind for e in ev] == ["fable-burn"], ev
    assert "~9m to wall" in ev[0].subtitle
    assert "1.6%/min" in ev[0].message

    # Shrinking ETA (9m -> 6m) -> deduped (stable row key, not countdown wording)
    assert notify_mod.detect(hot, hotter) == []

    # Non-Fable seat or already-100% seat -> silent
    team = _arow(4, "work1", is_active=True, scoped_windows=(), plan_type="claude_team")
    assert (
        notify_mod.detect(
            None,
            app_mod.UiSnapshot(
                accounts=(team,),
                active=team,
                autoswitch_enabled=False,
                fable_burn_eta_minutes=5.0,
            ),
        )
        == []
    )
    walled = _arow(2, "vlad", is_active=True, scoped_windows=(("Fable", 100.0),))
    assert (
        notify_mod.detect(
            None,
            app_mod.UiSnapshot(
                accounts=(walled,),
                active=walled,
                autoswitch_enabled=False,
                fable_burn_eta_minutes=1.0,
            ),
        )
        == []
    )

    # Ledger hysteresis: 9m -> 13m (< 15m recover) -> 10m stays suppressed; 20m clears & re-arms
    with tempfile.TemporaryDirectory() as tmp:
        clock = [2_000.0]
        sent: list[notify_mod.Event] = []
        settings_on = normalize_settings({**SETTINGS_DEFAULTS, "notifications_enabled": True})
        notifier = notify_mod.Notifier(
            settings=lambda: settings_on,
            ledger_path=Path(tmp) / "notify_state.json",
            credentials_path=Path(tmp) / "notify.json",
            mac_sender=SimpleNamespace(send=lambda e: (sent.append(e), True)[1]),
            clock=lambda: clock[0],
            dispatch=lambda work: work(),
        )
        assert len(notifier.notify(base, hot)) == 1
        clock[0] += notify_mod.THROTTLE_SECONDS + 10.0
        jitter_13 = replace(base, fable_burn_eta_minutes=13.0)
        assert notifier.notify(hot, jitter_13) == []
        clock[0] += notify_mod.THROTTLE_SECONDS + 10.0
        assert notifier.notify(jitter_13, hot) == []  # held across hysteresis band!

        # Cool down to 20m (>= 15m) past throttle -> releases & re-arms
        clock[0] += notify_mod.THROTTLE_SECONDS + 10.0
        assert notifier.notify(hot, base) == []
        clock[0] += notify_mod.THROTTLE_SECONDS + 10.0
        assert len(notifier.notify(base, hot)) == 1
        assert len(sent) == 2


# ---------------------------------------------------------------------------
# 4. Graceful degradation when claude_swap is masked
# ---------------------------------------------------------------------------


def test_graceful_degradation_when_claude_swap_masked() -> None:
    saved = {k: v for k, v in sys.modules.items() if k == "claude_swap" or k.startswith("claude_swap.")}
    with tempfile.TemporaryDirectory() as tmp:
        orig_path = belkins_game._shop_save_path
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            for k in list(saved):
                sys.modules.pop(k, None)
            sys.modules["claude_swap"] = None  # type: ignore[assignment]

            vlad = _arow(2, "vlad", is_active=True, scoped_windows=(("Fable", 85.0),))
            work1 = _arow(4, "work1", is_active=False, scoped_windows=(), plan_type="claude_team")
            ana = _arow(7, "ana", is_active=False, scoped_windows=(("Fable", 30.0),))
            snap = app_mod.UiSnapshot(
                settings=normalize_settings({**SETTINGS_DEFAULTS, "menu_theme": "belkins"}),
                accounts=(vlad, work1, ana),
                active=vlad,
                sessions=(_fake_session(99, "/Users/dev/usagebar", session_id="sid-1", latched_credits=True),),
                fable_burn_eta_minutes=10.0,
                fable_burn_rate_pct_per_min=1.5,
            )
            blocks = belkins_theme.build(snap, time.time(), ThemeActions())
            assert blocks
            events = notify_mod.detect(None, snap)
            kinds = {e.kind for e in events}
            assert "latched" in kinds and "fable-burn" in kinds, kinds
            assert app_mod.copy_text("claude --resume sid-1") is False
        finally:
            sys.modules.pop("claude_swap", None)
            sys.modules.update(saved)
            belkins_game._shop_save_path = orig_path


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    tests = [
        fn
        for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
