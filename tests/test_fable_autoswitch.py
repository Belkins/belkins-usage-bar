"""Standalone test suite for Fable 5.2 predictive auto-switching & interruption detection.

Covers:
  1. EWMA Fable burn velocity, ETA, stale-repeat immunity, weekly resets, idle gaps,
     clamping, and zero-I/O ``fable_guard_status()``.
  2. Predictive pre-switching in ``_thinking_guard_policy`` under fast burn vs steady burn,
     proportional busy hold cap & duration shrinking, 75% floor, and >=3% jump release.
  3. ``notify_local_limit_hit`` bypassing tick delay, manual grace, and Thinking Guard hold.
  4. ``_FableGuardSwitcher`` and ``SwapAccountSource.switch_best`` store pre-refresh,
     same-org affinity (``prefer_org``), perishable quota (``harvest_best_slot``), and
     hard exclusion of sentinel-flagged, disabled, and non-Fable seats.
  5. ``_clear_stale_credit_cache`` atomic key stripping preserving ``0o600`` permissions.
  6. ``fleet.detect_session_interruptions`` & ``fleet.collect`` integration: all latch and
     rate-limit signatures, recovery after subsequent assistant success, zero false positives
     on user/tool content, ``max_age_s`` gating, 16 KiB tail-only bound on multi-MB files,
     and strict privacy (zero transcript content leaked to return values or logs).
  7. CI compatibility when ``claude_swap`` is absent.

Run directly::

    CC_USAGE_WIDGET_NO_REVEAL=1 python3 tests/test_fable_autoswitch.py
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

from cc_usage_widget import accounts as accounts_mod  # noqa: E402
from cc_usage_widget import fleet  # noqa: E402
from cc_usage_widget.contracts import AccountRow  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(
    slot: int,
    alias: str,
    *,
    is_active: bool = False,
    five_hour_pct: float | None = 10.0,
    seven_day_pct: float | None = 20.0,
    scoped_windows: tuple[tuple[str, float], ...] = (),
    seven_day_resets_at: str | None = None,
    plan_type: str | None = "claude_max_20x",
    disabled: bool = False,
    switchable: bool = True,
    attention_note: str = "",
) -> AccountRow:
    return AccountRow(
        slot=slot,
        alias=alias,
        email=f"{alias}@example.com",
        is_active=is_active,
        five_hour_pct=five_hour_pct,
        seven_day_pct=seven_day_pct,
        scoped_windows=scoped_windows,
        seven_day_resets_at=seven_day_resets_at,
        plan_type=plan_type,
        disabled=disabled,
        switchable=switchable,
        attention_note=attention_note,
    )


def _snap_account(
    slot: int,
    alias: str,
    *,
    is_active: bool = False,
    five_hour_pct: float | None = 10.0,
    seven_day_pct: float | None = 20.0,
    fable_pct: float | None = None,
    seven_day_resets_at: str | None = None,
    fetched_at: float | None = None,
    disabled: bool = False,
    switchable: bool = True,
    sentinel: str | None = None,
) -> SimpleNamespace:
    last_good: dict[str, Any] = {}
    if five_hour_pct is not None:
        last_good["five_hour"] = {"pct": five_hour_pct}
    if seven_day_pct is not None:
        w7: dict[str, Any] = {"pct": seven_day_pct}
        if seven_day_resets_at is not None:
            w7["resets_at"] = seven_day_resets_at
        last_good["seven_day"] = w7
    if fable_pct is not None:
        last_good["scoped"] = [{"name": "Fable", "pct": fable_pct}]
    entry = SimpleNamespace(
        last_good=last_good if last_good else None,
        fetched_at=fetched_at,
        age_s=0.0 if fetched_at is not None else None,
        sentinel=sentinel,
        last_error=None,
        consecutive_failures=0,
    )
    return SimpleNamespace(
        number=str(slot),
        email=f"{alias}@example.com",
        alias=alias,
        is_active=is_active,
        disabled=disabled,
        switchable=switchable,
        usage=entry,
        org_name="",
    )


@contextmanager
def _sessions_dir(path: Path) -> Iterator[None]:
    prev = os.environ.get(fleet.SESSIONS_DIR_ENV)
    os.environ[fleet.SESSIONS_DIR_ENV] = str(path)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(fleet.SESSIONS_DIR_ENV, None)
        else:
            os.environ[fleet.SESSIONS_DIR_ENV] = prev


def _write_session_record(
    sessions_dir: Path,
    pid: int,
    *,
    session_id: str,
    cwd: str,
    name: str = "tab-1",
    status: str = "busy",
    stem: str | None = None,
) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{stem or pid}.json"
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": session_id,
                "cwd": cwd,
                "name": name,
                "status": status,
                "kind": "interactive",
                "startedAt": int(time.time() * 1000),
            }
        ),
        encoding="utf-8",
    )
    return path


def _make_real_cs_switcher_cls() -> type:
    """Build a test-double switcher class whose ``__module__`` starts with ``claude_swap``
    so live-switcher paths can be exercised in isolation without real Keychain or ``claude_swap``."""

    class _FakeClaudeSwapSwitcher:
        def __init__(
            self,
            *,
            configs_dir: Path,
            claude_cfg: Path,
            switchable_nums: list[str],
        ) -> None:
            self.configs_dir = configs_dir
            self._claude_cfg = claude_cfg
            self._switchable_nums = list(switchable_nums)
            self.switched_to: list[str] = []

        def _get_claude_config_path(self) -> Path:
            return self._claude_cfg

        def switchable_account_numbers(self) -> list[str]:
            return list(self._switchable_nums)

        def switch_to(self, identifier: str, json_output: bool = True) -> dict[str, Any]:
            self.switched_to.append(str(identifier))
            return {"switched": True, "to": {"number": int(identifier)}}

        def has_live_login(self) -> bool:
            return True

    _FakeClaudeSwapSwitcher.__module__ = "claude_swap.switcher"
    return _FakeClaudeSwapSwitcher


def _write_plan_config(configs_dir: Path, slot: int, email: str, org_type: str, tier: str = "") -> None:
    configs_dir.mkdir(parents=True, exist_ok=True)
    oauth: dict[str, Any] = {"emailAddress": email, "organizationType": org_type}
    if tier:
        oauth["organizationRateLimitTier"] = tier
    (configs_dir / f".claude-config-{slot}-{email}.json").write_text(
        json.dumps({"oauthAccount": oauth}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. EWMA Fable Burn Velocity & Telemetry
# ---------------------------------------------------------------------------


def test_ewma_fable_burn_velocity_eta_resets_and_stale_repeats() -> None:
    now_t = [1_760_000_000.0]
    snapshots: list[Any] = []

    class _FakeSnapSource:
        def take(self, *, full: bool = False, store_only: bool = False) -> Any:
            return snapshots.pop(0)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg_dir = root / "configs"
        _write_plan_config(cfg_dir, 2, "vlad@example.com", "claude_max", "default_claude_max_20x")
        _write_plan_config(cfg_dir, 4, "team@example.com", "claude_team")
        _write_plan_config(cfg_dir, 7, "ana@example.com", "claude_max", "default_claude_max_20x")

        sw = SimpleNamespace(configs_dir=cfg_dir, has_live_login=lambda: True)
        backend = SimpleNamespace(
            switcher=sw,
            snapshot_source=_FakeSnapSource(),
            backup_dir=root,
            sentinel_notes={},
            fresh_reset_strings=lambda _w: None,
            roll_weekly=lambda w, _now: w,
        )

        src = accounts_mod.SwapAccountSource(settings={"belkins_fable_guard": True})
        src._backend = backend  # type: ignore[assignment]
        src._wall = lambda: now_t[0]

        def _make_snap(fable_v: float, fetched: float) -> SimpleNamespace:
            return SimpleNamespace(
                active_number="2",
                accounts=[
                    _snap_account(2, "vlad", is_active=True, fable_pct=fable_v, fetched_at=fetched),
                    _snap_account(4, "team", is_active=False, five_hour_pct=15.0, seven_day_pct=20.0, fetched_at=fetched),
                    _snap_account(7, "ana", is_active=False, fable_pct=30.0, fetched_at=fetched),
                ],
            )

        # Sample 1: baseline only -> no burn rate or ETA yet
        snapshots.append(_make_snap(70.0, now_t[0]))
        src.refresh(force=True)
        src.rows()
        assert src.fable_burn_rate_pct_per_min() is None
        assert src.fable_burn_eta_minutes() is None

        # Sample 2: +2.0% in 60s -> 2.0 %/min, 28% remaining -> ETA = 14.0m
        now_t[0] += 60.0
        snapshots.append(_make_snap(72.0, now_t[0]))
        src.refresh(force=True)
        assert src.fable_burn_rate_pct_per_min() == 2.0
        assert src.fable_burn_eta_minutes() == 14.0
        # Reading rows() on the same snapshot must NOT alter EWMA
        src.rows()
        assert src.fable_burn_rate_pct_per_min() == 2.0

        # Stale repeat: new snapshot object carrying the SAME fetched_at -> ignored
        snapshots.append(_make_snap(72.0, now_t[0]))
        src.refresh(force=True)
        assert src.fable_burn_rate_pct_per_min() == 2.0
        assert src.fable_burn_eta_minutes() == 14.0

        # Sample 3: +2.5% in 60s -> EWMA = 0.45 * 2.5 + 0.55 * 2.0 = 2.225 %/min
        now_t[0] += 60.0
        snapshots.append(_make_snap(74.5, now_t[0]))
        src.refresh(force=True)
        rate3 = src.fable_burn_rate_pct_per_min()
        assert rate3 is not None and abs(rate3 - 2.225) < 1e-4, rate3
        # ETA uses fresh sample.last_pct (74.5%) even before rows() rebuilds _rows_cache
        eta3 = src.fable_burn_eta_minutes()
        expected_eta3 = round((100.0 - 74.5) / 2.225, 1)
        assert eta3 == expected_eta3, (eta3, expected_eta3)

        # fable_guard_status() exposes exact required schema with zero I/O
        src.rows()
        status = src.fable_guard_status()
        assert set(status.keys()) == {
            "enabled",
            "fable_slots",
            "excluded_slots",
            "active_eta_min",
            "active_burn_pct_per_min",
        }, status
        assert status["enabled"] is True
        assert status["fable_slots"] == [2, 7], status
        assert status["excluded_slots"] == [4], status
        assert status["active_burn_pct_per_min"] == rate3
        assert status["active_eta_min"] == expected_eta3

        # Disabling Fable Guard empties excluded_slots immediately
        src._settings_obj = {"belkins_fable_guard": False}
        assert src.fable_guard_status()["excluded_slots"] == []
        src._settings_obj = {"belkins_fable_guard": True}

        # Extreme jump clamped at _FABLE_BURN_MAX_RATE (25.0 %/min)
        src._record_fable_sample(99, 10.0, 1000.0)
        src._record_fable_sample(99, 90.0, 1010.0)  # +80% in 10s = 480%/min -> clamped to 25.0
        assert src.fable_burn_rate_pct_per_min(99) == 25.0

        # Weekly reset (80% -> 0%) resets velocity to 0.0 and clears ETA to None
        now_t[0] += 60.0
        snapshots.append(_make_snap(0.0, now_t[0]))
        src.refresh(force=True)
        assert src.fable_burn_rate_pct_per_min() == 0.0
        assert src.fable_burn_eta_minutes() is None

        # Long gap (> 30 min) resets baseline rather than smearing an old burst
        src._record_fable_sample(77, 40.0, 5000.0)
        src._record_fable_sample(77, 42.0, 5060.0)
        assert src.fable_burn_rate_pct_per_min(77) == 2.0
        src._record_fable_sample(77, 45.0, 5060.0 + 3600.0)
        assert src.fable_burn_rate_pct_per_min(77) is None


# ---------------------------------------------------------------------------
# 2. Predictive Pre-Switching & Jump-Spike Immediate Release
# ---------------------------------------------------------------------------


def test_predictive_threshold_lowering_fast_burn_vs_steady_burn_and_jump_release() -> None:
    @dataclasses.dataclass
    class _Policy:
        threshold: float = 85.0
        weekly_threshold: float | None = 88.0

    pol = _Policy(threshold=85.0, weekly_threshold=88.0)
    eng = SimpleNamespace(_policy=pol)

    with tempfile.TemporaryDirectory() as sess_tmp:
        orig_read = fleet.read_registry
        with _sessions_dir(Path(sess_tmp)):
            try:
                # A. Steady slow burn (0.4%/min < 0.8%/min): keeps 88% line & 91.5% busy hold
                src_steady = accounts_mod.SwapAccountSource(settings={"belkins_thinking_guard": True})
                src_steady._rows_cache = (
                    _row(1, "vlad", is_active=True, five_hour_pct=10.0, seven_day_pct=50.0, scoped_windows=(("Fable", 89.0),)),
                    _row(2, "ana", is_active=False, scoped_windows=(("Fable", 20.0),)),
                )
                src_steady._record_fable_sample(1, 88.6, 1000.0)
                src_steady._record_fable_sample(1, 89.0, 1060.0)  # 0.4%/min, jump 0.4%
                fleet.read_registry = lambda _d: ([{"status": "busy"}], 0)  # type: ignore[assignment]
                g_steady = src_steady._thinking_guard_policy(eng, pol)
                assert g_steady is not None and g_steady.weekly_threshold == 91.5, g_steady
                assert src_steady.has_armed_handoff() is True

                # B. Fast burn (2.0%/min >= 0.8%/min) under 6 busy tabs:
                #    burn_shift = min(10.0, 2.0 * 4.0) = 8.0 -> effective_weekly = 80.0%
                #    busy_7d_cap = max(75.0, (93.0 - 9.0) - 8.0) = 76.0% -> releases immediately at 81%!
                src_fast = accounts_mod.SwapAccountSource(settings={"belkins_thinking_guard": True})
                src_fast._rows_cache = (
                    _row(1, "vlad", is_active=True, five_hour_pct=15.0, seven_day_pct=40.0, scoped_windows=(("Fable", 81.0),)),
                    _row(2, "ana", is_active=False, scoped_windows=(("Fable", 15.0),)),
                )
                src_fast._record_fable_sample(1, 79.0, 2000.0)
                src_fast._record_fable_sample(1, 81.0, 2060.0)  # 2.0%/min, jump 2.0% (< 3.0%)
                fleet.read_registry = lambda _d: ([{"status": "busy"} for _ in range(6)], 0)  # type: ignore[assignment]
                g_fast_6 = src_fast._thinking_guard_policy(eng, pol)
                assert g_fast_6 is not None and g_fast_6.weekly_threshold == 80.0, g_fast_6
                assert src_fast.has_armed_handoff() is False

                # C. Same fast burn (2.0%/min) under 1 busy tab:
                #    busy_7d_cap shrinks to 91.5 - 8.0 = 83.5%, max_hold_s shrinks from 90s -> 30s!
                fleet.read_registry = lambda _d: ([{"status": "busy"}], 0)  # type: ignore[assignment]
                g_fast_1 = src_fast._thinking_guard_policy(eng, pol)
                assert g_fast_1 is not None and g_fast_1.weekly_threshold == 83.5, g_fast_1
                assert src_fast.has_armed_handoff() is True
                # Simulate 35s elapsed (> 30s shrunk hold cap, well under 90s normal cap) -> releases!
                src_fast._guard_hold_started_at = time.monotonic() - 35.0
                g_fast_released = src_fast._thinking_guard_policy(eng, pol)
                assert g_fast_released is not None and g_fast_released.weekly_threshold == 80.0, g_fast_released
                assert src_fast.has_armed_handoff() is False

                # D. Floor at 75.0% for extreme burn (and never raises user policy already < 75%)
                src_floor = accounts_mod.SwapAccountSource(settings={"belkins_thinking_guard": True})
                src_floor._rows_cache = (
                    _row(1, "vlad", is_active=True, scoped_windows=(("Fable", 76.0),)),
                )
                src_floor._record_fable_sample(1, 73.0, 3000.0)
                src_floor._record_fable_sample(1, 76.0, 3060.0)  # 3.0%/min -> shift 10.0
                fleet.read_registry = lambda _d: ((), 0)  # type: ignore[assignment]
                g_floor = src_floor._thinking_guard_policy(eng, _Policy(threshold=80.0, weekly_threshold=82.0))
                assert g_floor is not None and g_floor.weekly_threshold == 75.0, g_floor
                g_low_user = src_floor._thinking_guard_policy(eng, _Policy(threshold=70.0, weekly_threshold=70.0))
                assert g_low_user is None, "must never raise user weekly_threshold already below 75%"

                # E. Single-interval jump >= 3.0% while >= 82% -> immediate release (no busy hold!)
                src_jump = accounts_mod.SwapAccountSource(settings={"belkins_thinking_guard": True})
                src_jump._rows_cache = (
                    _row(1, "vlad", is_active=True, five_hour_pct=10.0, seven_day_pct=50.0, scoped_windows=(("Fable", 89.0),)),
                )
                # +3.5% over 5 minutes (0.7%/min < 0.8%/min) so jump_spike alone triggers release!
                src_jump._record_fable_sample(1, 85.5, 4000.0)
                src_jump._record_fable_sample(1, 89.0, 4300.0)
                fleet.read_registry = lambda _d: ([{"status": "busy"}], 0)  # type: ignore[assignment]
                g_jump = src_jump._thinking_guard_policy(eng, pol)
                assert g_jump is None, f"expected immediate release without busy hold, got {g_jump}"
                assert src_jump.has_armed_handoff() is False
            finally:
                fleet.read_registry = orig_read


# ---------------------------------------------------------------------------
# 3. notify_local_limit_hit Immediate Wakeup
# ---------------------------------------------------------------------------


def test_notify_local_limit_hit_bypasses_manual_grace_tick_delay_and_guard_hold() -> None:
    @dataclasses.dataclass
    class _Policy:
        threshold: float = 85.0
        weekly_threshold: float | None = 88.0

    ticks_seen: list[Any] = []
    pol = _Policy()

    class _FakeEngine:
        def __init__(self) -> None:
            self.settings = pol

        def tick(self) -> str:
            ticks_seen.append(self.settings)
            return "ok"

    src = accounts_mod.SwapAccountSource(settings={"belkins_thinking_guard": True})
    src._rows_cache = (
        _row(1, "vlad", is_active=True, five_hour_pct=10.0, seven_day_pct=50.0, scoped_windows=(("Fable", 89.0),)),
        _row(2, "ana", is_active=False, scoped_windows=(("Fable", 10.0),)),
    )
    src._snapshot = SimpleNamespace(
        active_number="1",
        accounts=[_snap_account(1, "vlad", is_active=True, fable_pct=89.0)],
    )
    src.autoswitch_enabled = lambda: True  # type: ignore[method-assign]
    engine = _FakeEngine()
    src._ensure_engine = lambda: engine  # type: ignore[method-assign]
    src._drain_events = lambda: None  # type: ignore[method-assign]
    src._tick_delay = lambda _e, _o: 30.0  # type: ignore[method-assign]

    now = time.monotonic()
    src._next_tick_at = now + 300.0
    src._manual_hold_until = now + 180.0
    src._guard_hold_started_at = now - 5.0
    src._handoff_armed = True

    # Before local hit: blocked by tick delay + manual grace
    assert src.evaluate_autoswitch() is None
    assert len(ticks_seen) == 0

    with tempfile.TemporaryDirectory() as sess_tmp:
        orig_read = fleet.read_registry
        with _sessions_dir(Path(sess_tmp)):
            try:
                fleet.read_registry = lambda _d: ([{"status": "busy"}], 0)  # type: ignore[assignment]
                src.notify_local_limit_hit("rate_limit")
                assert src._next_tick_at == 0.0
                assert src._manual_hold_until == 0.0
                assert src._guard_hold_started_at == 0.0
                assert src.has_armed_handoff() is False
                assert any("local rate_limit hit" in e for e in src.recent_events())

                # Runs immediately AND bypasses busy-session Thinking Guard hold on this pass!
                src.evaluate_autoswitch()
                assert len(ticks_seen) == 1
                assert ticks_seen[0].weekly_threshold == 88.0, ticks_seen[0]
                assert src.has_armed_handoff() is False

                # Kind sanitization prevents arbitrary log injection
                src.notify_local_limit_hit("usage_credits!! \n bad")
                assert any("local usage_creditsbad hit" in e for e in src.recent_events())
            finally:
                fleet.read_registry = orig_read


# ---------------------------------------------------------------------------
# 4. _FableGuardSwitcher & switch_best (Refresh, Same-Org, Perishable, Guard)
# ---------------------------------------------------------------------------


def test_fable_guard_switcher_and_switch_best_store_refresh_org_and_perishable() -> None:
    now = 1_760_000_000.0
    soon_iso = _iso(now + 6 * 3600)
    far_iso = _iso(now + 6 * 86400)
    FakeSwitcherCls = _make_real_cs_switcher_cls()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        configs_dir = root / "configs"
        claude_cfg = root / ".claude.json"
        claude_cfg.write_text(
            json.dumps({"cachedExtraUsageDisabledReason": "out_of_credits", "theme": "dark"}),
            encoding="utf-8",
        )
        os.chmod(claude_cfg, 0o600)

        (root / "sequence.json").write_text(
            json.dumps(
                {
                    "activeAccountNumber": "1",
                    "accounts": {
                        "1": {"email": "active@belkins.io", "organizationUuid": "org-belkins"},
                        "2": {"email": "sameorg@belkins.io", "organizationUuid": "org-belkins"},
                        "3": {"email": "other@gmail.com", "organizationUuid": "org-other"},
                        "4": {"email": "team@belkins.io", "organizationUuid": "org-belkins"},
                        "5": {"email": "expiring@gmail.com", "organizationUuid": "org-third"},
                        "6": {"email": "broken@belkins.io", "organizationUuid": "org-belkins"},
                    },
                }
            ),
            encoding="utf-8",
        )
        _write_plan_config(configs_dir, 1, "active@example.com", "claude_max", "default_claude_max_20x")
        _write_plan_config(configs_dir, 2, "sameorg@example.com", "claude_max", "default_claude_max_20x")
        _write_plan_config(configs_dir, 3, "other@example.com", "claude_max", "default_claude_max_20x")
        _write_plan_config(configs_dir, 4, "team@example.com", "claude_team")
        _write_plan_config(configs_dir, 5, "expiring@example.com", "claude_max", "default_claude_max_20x")
        _write_plan_config(configs_dir, 6, "broken@example.com", "claude_max", "default_claude_max_20x")

        sw = FakeSwitcherCls(
            configs_dir=configs_dir,
            claude_cfg=claude_cfg,
            switchable_nums=["1", "2", "3", "4", "5", "6"],
        )

        # Store starts with stale rows (slot 2 appears 95% full), while live snapshot_source
        # returns fresh store data (slot 2 is actually 45% -> same-org wins over slot 3 at 35%!).
        take_calls: list[dict[str, bool]] = []
        current_snap: list[Any] = [
            SimpleNamespace(
                active_number="1",
                accounts=[
                    _snap_account(1, "active", is_active=True, fable_pct=92.0, seven_day_resets_at=far_iso, fetched_at=now),
                    _snap_account(2, "sameorg", is_active=False, fable_pct=45.0, seven_day_resets_at=far_iso, fetched_at=now),
                    _snap_account(3, "other", is_active=False, fable_pct=35.0, seven_day_resets_at=far_iso, fetched_at=now),
                    _snap_account(4, "team", is_active=False, five_hour_pct=5.0, seven_day_pct=10.0, fetched_at=now),
                    _snap_account(5, "expiring", is_active=False, fable_pct=100.0, seven_day_resets_at=soon_iso, fetched_at=now),
                    _snap_account(6, "broken", is_active=False, fable_pct=0.0, sentinel="re-login needed", fetched_at=now),
                ],
            )
        ]

        orig_switch_to = sw.switch_to

        def _stick_switch_to(identifier: str, json_output: bool = True) -> dict[str, Any]:
            res = orig_switch_to(identifier, json_output=json_output)
            snap = current_snap[0]
            snap.active_number = str(identifier)
            for acc in snap.accounts:
                acc.is_active = str(acc.number) == str(identifier)
            return res

        sw.switch_to = _stick_switch_to  # type: ignore[method-assign]

        class _StoreSnapSource:
            def take(self, *, full: bool = False, store_only: bool = False) -> Any:
                take_calls.append({"full": full, "store_only": store_only})
                return current_snap[0]

        backend = SimpleNamespace(
            switcher=sw,
            snapshot_source=_StoreSnapSource(),
            backup_dir=root,
            sentinel_notes={"re-login needed": "Re-login needed"},
            fresh_reset_strings=lambda w: ("", w.get("resets_at")) if w.get("resets_at") else None,
            roll_weekly=lambda w, _now: w,
            load_policy=lambda _dir: SimpleNamespace(model="all"),
        )

        src = accounts_mod.SwapAccountSource(settings={"belkins_fable_guard": True})
        src._backend = backend  # type: ignore[assignment]
        src._wall = lambda: now
        # Seed deliberately stale _rows_cache before switch_best to verify pre-refresh!
        src._rows_cache = (
            _row(1, "active", is_active=True, scoped_windows=(("Fable", 92.0),)),
            _row(2, "sameorg", is_active=False, scoped_windows=(("Fable", 98.0),)),
            _row(3, "other", is_active=False, scoped_windows=(("Fable", 35.0),)),
        )

        # 1. switch_best refreshes from store, skips Team #4 & sentinel #6, prefers same-org #2 over #3!
        assert src.switch_best() is True
        assert len(take_calls) >= 2, "must refresh from store BEFORE ranking and after switching"
        assert sw.switched_to == ["2"], sw.switched_to
        # Stale credit cache cleared with 0o600 preserved
        cfg_after = json.loads(claude_cfg.read_text(encoding="utf-8"))
        assert "cachedExtraUsageDisabledReason" not in cfg_after
        assert cfg_after["theme"] == "dark"
        assert stat.S_IMODE(claude_cfg.stat().st_mode) == 0o600

        # 2. _FableGuardSwitcher filters out Team #4 and sentinel #6, and reorders same-org #2 ahead of #3!
        sw_order = FakeSwitcherCls(
            configs_dir=configs_dir,
            claude_cfg=claude_cfg,
            switchable_nums=["1", "3", "2", "4", "5", "6"],
        )
        src._rows_cache = (
            _row(1, "active", is_active=True, scoped_windows=(("Fable", 92.0),), seven_day_resets_at=far_iso),
            _row(2, "sameorg", is_active=False, scoped_windows=(("Fable", 45.0),), seven_day_resets_at=far_iso),
            _row(3, "other", is_active=False, scoped_windows=(("Fable", 45.0),), seven_day_resets_at=far_iso),
            _row(4, "team", is_active=False, five_hour_pct=5.0, seven_day_pct=10.0, plan_type="claude_team"),
            _row(5, "expiring", is_active=False, scoped_windows=(("Fable", 45.0),), seven_day_resets_at=soon_iso),
            _row(6, "broken", is_active=False, scoped_windows=(("Fable", 0.0),)),
        )
        src._sentinels = {6: "Re-login needed"}
        proxy = accounts_mod._FableGuardSwitcher(sw_order, src)
        cand_order = proxy.switchable_account_numbers()
        assert "4" not in cand_order, f"Team seat #4 must be excluded: {cand_order}"
        assert "6" not in cand_order, f"Sentinel seat #6 must be excluded: {cand_order}"
        # Perishable #5 (expiring 6h) and same-org #2 both beat distant other-org #3!
        assert cand_order.index("5") < cand_order.index("3"), cand_order
        assert cand_order.index("2") < cand_order.index("3"), f"same-org #2 should precede #3: {cand_order}"

        # 3. Perishable-quota urgency: slot #5 expiring in 6h with 60% Fable free beats far same-org #2!
        sw.switched_to.clear()
        current_snap[0] = SimpleNamespace(
            active_number="1",
            accounts=[
                _snap_account(1, "active", is_active=True, fable_pct=92.0, seven_day_resets_at=far_iso, fetched_at=now),
                _snap_account(2, "sameorg", is_active=False, fable_pct=45.0, seven_day_resets_at=far_iso, fetched_at=now),
                _snap_account(4, "team", is_active=False, five_hour_pct=5.0, seven_day_pct=10.0, fetched_at=now),
                _snap_account(5, "expiring", is_active=False, fable_pct=40.0, seven_day_resets_at=soon_iso, fetched_at=now),
            ],
        )
        assert src.switch_best() is True
        assert sw.switched_to == ["5"], sw.switched_to

        # 4. Already on the best (and sole healthy) Fable seat -> returns True without switching onto Team #4!
        sw.switched_to.clear()
        current_snap[0] = SimpleNamespace(
            active_number="2",
            accounts=[
                _snap_account(2, "sameorg", is_active=True, fable_pct=25.0, seven_day_resets_at=far_iso, fetched_at=now),
                _snap_account(3, "other", is_active=False, fable_pct=100.0, seven_day_resets_at=far_iso, fetched_at=now),
                _snap_account(4, "team", is_active=False, five_hour_pct=5.0, seven_day_pct=10.0, fetched_at=now),
            ],
        )
        assert src.switch_best() is True
        assert sw.switched_to == [], sw.switched_to

        # 5. All Fable seats at 100%, only Team #4 free -> Fable Guard refuses to poison onto Team #4!
        current_snap[0] = SimpleNamespace(
            active_number="2",
            accounts=[
                _snap_account(2, "sameorg", is_active=True, fable_pct=100.0, fetched_at=now),
                _snap_account(3, "other", is_active=False, fable_pct=100.0, fetched_at=now),
                _snap_account(4, "team", is_active=False, five_hour_pct=5.0, seven_day_pct=10.0, fetched_at=now),
            ],
        )
        assert src.switch_best() is False
        assert sw.switched_to == []
        assert "Fable" in (src.last_error or ""), src.last_error


# ---------------------------------------------------------------------------
# 5. _clear_stale_credit_cache Permissions & Key Preservation
# ---------------------------------------------------------------------------


def test_clear_stale_credit_cache_preserves_keys_and_0600_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg_file = Path(tmp) / ".claude.json"
        original = {
            "cachedExtraUsageDisabledReason": "org_level_disabled",
            "oauthAccount": {"emailAddress": "dev@belkins.io", "organizationUuid": "org-1"},
            "theme": "dark",
            "mcpServers": {"local": {"command": "echo"}},
        }
        cfg_file.write_text(json.dumps(original, indent=2), encoding="utf-8")
        os.chmod(cfg_file, 0o600)

        fake_sw = SimpleNamespace(_get_claude_config_path=lambda: cfg_file)
        assert accounts_mod._clear_stale_credit_cache(fake_sw) is True
        assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600

        after = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "cachedExtraUsageDisabledReason" not in after
        assert after["oauthAccount"] == original["oauthAccount"]
        assert after["theme"] == "dark"
        assert after["mcpServers"] == original["mcpServers"]

        # No stray temp files left behind & second call is idempotent False
        leftover = [p for p in Path(tmp).iterdir() if ".tmp." in p.name]
        assert not leftover, leftover
        assert accounts_mod._clear_stale_credit_cache(fake_sw) is False


# ---------------------------------------------------------------------------
# 6. detect_session_interruptions & fleet.collect Integration
# ---------------------------------------------------------------------------


def test_detect_session_interruptions_latch_recovery_rate_limit_and_collect() -> None:
    assert fleet.SessionInfo is fleet.SessionRow
    live_pid = os.getpid()
    now = time.time()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sess_dir = root / "sessions"
        proj_root = root / "projects"
        cwd = "/Users/dev/belkins-app"
        slug = re.sub(r"[^a-zA-Z0-9-]", "-", cwd)
        pdir = proj_root / slug
        pdir.mkdir(parents=True)

        # A. Every latch signature is detected
        latch_samples = [
            {"type": "assistant", "isApiErrorMessage": True, "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Fable 5.2 now requires usage credits"}]}},
            {"type": "error", "error": "model_requires_usage_credits"},
            {"type": "system", "subtype": "api_error", "reason": "consent_unanswered"},
            {"type": "assistant", "isApiErrorMessage": True, "message": {"content": [{"type": "text", "text": "You are out of usage credits"}]}},
            {"type": "assistant", "isApiErrorMessage": True, "message": {"content": [{"type": "text", "text": "You\u2019re out of usage credits"}]}},
        ]
        for idx, rec in enumerate(latch_samples):
            sid = f"latch-sig-{idx}"
            (pdir / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
            _write_session_record(sess_dir, live_pid, session_id=sid, cwd=cwd)
            res = fleet.detect_session_interruptions(sess_dir, [proj_root], max_age_s=180.0, now=now)
            assert live_pid in res and res[live_pid]["latched_credits"] is True, (idx, res)
            assert res[live_pid]["session_id"] == sid

        # B. Trailing system turn_duration does NOT clear latch; subsequent assistant success DOES!
        sid_rec = "latch-then-recover"
        jsonl_rec = pdir / f"{sid_rec}.jsonl"
        jsonl_rec.write_text(
            "\n".join(
                [
                    json.dumps(latch_samples[0]),
                    json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 412}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        _write_session_record(sess_dir, live_pid, session_id=sid_rec, cwd=cwd)
        res_still = fleet.detect_session_interruptions(sess_dir, [proj_root], max_age_s=180.0, now=now)
        assert res_still.get(live_pid, {}).get("latched_credits") is True, res_still

        # Append a successful assistant response -> clears latch!
        with jsonl_rec.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "model": "claude-fable-5-2",
                            "content": [{"type": "text", "text": "Resumed cleanly!"}],
                            "usage": {"input_tokens": 200, "output_tokens": 40},
                        },
                    }
                )
                + "\n"
            )
        res_cleared = fleet.detect_session_interruptions(sess_dir, [proj_root], max_age_s=180.0, now=now)
        assert live_pid not in res_cleared, res_cleared

        # C. Rate-limit signatures detected, cleared by subsequent assistant success
        rl_samples = [
            {"type": "assistant", "isApiErrorMessage": True, "message": {"content": [{"type": "text", "text": "You've hit your Fable limit · resets 5pm"}]}},
            {"type": "assistant", "isApiErrorMessage": True, "message": {"content": [{"type": "text", "text": "You've hit your limit"}]}},
            {"type": "system", "subtype": "api_error", "error": {"type": "usage_limit_reached"}},
        ]
        for idx, rec in enumerate(rl_samples):
            sid = f"rl-sig-{idx}"
            (pdir / f"{sid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
            _write_session_record(sess_dir, live_pid, session_id=sid, cwd=cwd)
            res = fleet.detect_session_interruptions(sess_dir, [proj_root], max_age_s=180.0, now=now)
            assert res.get(live_pid, {}).get("hit_rate_limit") is True, (idx, res)
            assert res[live_pid]["latched_credits"] is False

        # D. User prompts, tool results, and normal assistant code replies NEVER false-positive!
        sid_fp = "no-false-positive"
        (pdir / f"{sid_fp}.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": "Fix detection for model_requires_usage_credits and hit your Fable limit",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "model": "claude-fable-5-2",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Added consent_unanswered and usage_limit_reached checks.",
                                    }
                                ],
                                "usage": {"input_tokens": 500, "output_tokens": 120},
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        _write_session_record(sess_dir, live_pid, session_id=sid_fp, cwd=cwd)
        assert fleet.detect_session_interruptions(sess_dir, [proj_root], max_age_s=180.0, now=now) == {}

        # E. Stale mtime (> max_age_s) is skipped without triggering
        sid_stale = "stale-latch"
        stale_file = pdir / f"{sid_stale}.jsonl"
        stale_file.write_text(json.dumps(latch_samples[0]) + "\n", encoding="utf-8")
        os.utime(stale_file, (now - 500.0, now - 500.0))
        _write_session_record(sess_dir, live_pid, session_id=sid_stale, cwd=cwd)
        assert fleet.detect_session_interruptions(sess_dir, [proj_root], max_age_s=180.0, now=now) == {}

        # F. fleet.collect wires session_id, latched_credits, hit_rate_limit cleanly
        #    even when org_shifted is ALSO True (tests row reconstruction preserves fields!)
        backup = root / "backup"
        backup.mkdir()
        (backup / "sequence.json").write_text(
            json.dumps(
                {
                    "activeAccountNumber": "2",
                    "accounts": {
                        "1": {"email": "a@belkins.io", "organizationUuid": "org-alpha"},
                        "2": {"email": "b@other.io", "organizationUuid": "org-beta"},
                    },
                }
            ),
            encoding="utf-8",
        )
        sid_live = "collect-latched-rl"
        (pdir / f"{sid_live}.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"type": "system", "organizationUuid": "org-alpha"}),
                    json.dumps(rl_samples[0]),
                    json.dumps(latch_samples[0]),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        _write_session_record(sess_dir, live_pid, session_id=sid_live, cwd=cwd)
        with _sessions_dir(sess_dir):
            snap = fleet.collect(backup_dir=backup, active_slot=2)
        assert len(snap.sessions) == 1, snap.sessions
        row = snap.sessions[0]
        assert row.session_id == sid_live
        assert row.org_shifted is True
        assert row.latched_credits is True
        assert row.hit_rate_limit is True


# ---------------------------------------------------------------------------
# 7. Tail-Only 16 KiB Bound on Multi-MB Files & Strict Privacy Canary
# ---------------------------------------------------------------------------


def test_detect_session_interruptions_tail_only_and_privacy() -> None:
    live_pid = os.getpid()
    now = time.time()
    canary = "SECRET_TRANSCRIPT_CANARY_99142_DO_NOT_LEAK"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sess_dir = root / "sessions"
        proj_root = root / "projects"
        cwd = "/Users/dev/giant-repo"
        slug = re.sub(r"[^a-zA-Z0-9-]", "-", cwd)
        pdir = proj_root / slug
        pdir.mkdir(parents=True)

        # Case 1: 2.5 MiB transcript with error buried at byte 0 (outside last 16 KiB)
        sid_head = "huge-head-error"
        head_file = pdir / f"{sid_head}.jsonl"
        filler_line = (
            json.dumps({"type": "user", "message": {"role": "user", "content": f"pad {canary} " + ("x" * 900)}})
            + "\n"
        )
        with head_file.open("w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "isApiErrorMessage": True,
                        "message": {"content": [{"type": "text", "text": "Fable 5.2 now requires usage credits"}]},
                    }
                )
                + "\n"
            )
            # Write ~2.5 MiB of filler after the old error
            for _ in range(2600):
                fh.write(filler_line)
        assert head_file.stat().st_size > 2 * 1024 * 1024

        _write_session_record(sess_dir, live_pid, session_id=sid_head, cwd=cwd)
        assert fleet.detect_session_interruptions(sess_dir, [proj_root], max_age_s=180.0, now=now) == {}

        # Case 2: 2.5 MiB transcript with error + privacy canary in the trailing 16 KiB
        sid_tail = "huge-tail-error"
        tail_file = pdir / f"{sid_tail}.jsonl"
        with tail_file.open("w", encoding="utf-8") as fh:
            for _ in range(2600):
                fh.write(filler_line)
            fh.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "isApiErrorMessage": True,
                        "message": {
                            "model": "<synthetic>",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"Fable 5.2 now requires usage credits ({canary})",
                                }
                            ],
                        },
                    }
                )
                + "\n"
            )
        assert tail_file.stat().st_size > 2 * 1024 * 1024
        _write_session_record(sess_dir, live_pid, session_id=sid_tail, cwd=cwd)

        # Instrument Path.open to verify at most 16 KiB is read from the 2.5 MiB transcript
        bytes_read_from_jsonl: list[int] = []
        orig_open = Path.open

        class _CountingReader:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def seek(self, offset: int, whence: int = 0) -> int:
                return self._inner.seek(offset, whence)

            def tell(self) -> int:
                return self._inner.tell()

            def read(self, n: int = -1) -> bytes:
                chunk = self._inner.read(n)
                bytes_read_from_jsonl.append(len(chunk))
                return chunk

            def __enter__(self) -> "_CountingReader":
                return self

            def __exit__(self, *exc: Any) -> Any:
                return self._inner.__exit__(*exc)

        def _wrapped_open(self_path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            fh = orig_open(self_path, mode, *args, **kwargs)
            if self_path == tail_file and "b" in mode:
                return _CountingReader(fh)
            return fh

        captured_logs: list[str] = []

        class _LogSink(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_logs.append(self.format(record))

        sink = _LogSink()
        prev_level = fleet.LOGGER.level
        fleet.LOGGER.setLevel(logging.DEBUG)
        fleet.LOGGER.addHandler(sink)
        Path.open = _wrapped_open  # type: ignore[assignment]
        try:
            res = fleet.detect_session_interruptions(sess_dir, [proj_root], max_age_s=180.0, now=now)
            with _sessions_dir(sess_dir):
                snap = fleet.collect(backup_dir=None, active_slot=1)
        finally:
            Path.open = orig_open  # type: ignore[assignment]
            fleet.LOGGER.removeHandler(sink)
            fleet.LOGGER.setLevel(prev_level)

        assert res.get(live_pid, {}).get("latched_credits") is True, res
        assert bytes_read_from_jsonl and all(n <= 16 * 1024 for n in bytes_read_from_jsonl), bytes_read_from_jsonl
        # Strict privacy: canary never appears in result, SessionRow, or logs!
        assert canary not in json.dumps(res)
        assert canary not in repr(snap)
        assert all(canary not in line for line in captured_logs), captured_logs


# ---------------------------------------------------------------------------
# 8. CI Compatibility (Works With claude_swap Absent)
# ---------------------------------------------------------------------------


def test_ci_compatibility_without_claude_swap() -> None:
    repo_root = str(Path(__file__).resolve().parents[1])
    probe = r"""
import importlib.abc, os, sys, tempfile, time, json
from pathlib import Path

class _BlockClaudeSwap(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "claude_swap" or fullname.startswith("claude_swap."):
            raise ImportError("blocked for CI compatibility test")
        return None

sys.meta_path.insert(0, _BlockClaudeSwap())
sys.path.insert(0, %r)
os.environ["CC_USAGE_WIDGET_NO_REVEAL"] = "1"

from cc_usage_widget import accounts, fleet
from cc_usage_widget.contracts import AccountRow

src = accounts.SwapAccountSource(settings={"belkins_fable_guard": True})
src._rows_cache = (
    AccountRow(slot=1, alias="max1", email="a@x.com", is_active=True,
               five_hour_pct=5.0, seven_day_pct=20.0,
               scoped_windows=(("Fable", 80.0),), plan_type="claude_max_20x"),
    AccountRow(slot=2, alias="team", email="b@x.com", is_active=False,
               five_hour_pct=5.0, seven_day_pct=10.0,
               scoped_windows=(), plan_type="claude_team"),
)
src._record_fable_sample(1, 78.0, 1000.0)
src._record_fable_sample(1, 80.0, 1060.0)
assert src.fable_burn_rate_pct_per_min() == 2.0
assert src.fable_burn_eta_minutes() == 10.0
st = src.fable_guard_status()
assert st["fable_slots"] == [1] and st["excluded_slots"] == [2]
src.notify_local_limit_hit("rate_limit")
assert src._next_tick_at == 0.0

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    sdir = root / "sessions"
    pdir = root / "projects" / "-tmp-ci"
    sdir.mkdir(parents=True)
    pdir.mkdir(parents=True)
    pid = os.getpid()
    (sdir / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": "ci-sess", "cwd": "/tmp/ci", "kind": "interactive", "status": "busy"
    }))
    (pdir / "ci-sess.jsonl").write_text(json.dumps({
        "type": "assistant", "isApiErrorMessage": True,
        "message": {"content": [{"type": "text", "text": "You've hit your Fable limit"}]}
    }) + "\n")
    os.environ[fleet.SESSIONS_DIR_ENV] = str(sdir)
    intr = fleet.detect_session_interruptions(sdir, [root / "projects"])
    assert intr[pid]["hit_rate_limit"] is True
    snap = fleet.collect(backup_dir=None, active_slot=1)
    assert snap.sessions[0].hit_rate_limit is True
print("CI_OK")
""" % (repo_root,)
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0 and "CI_OK" in proc.stdout, (proc.returncode, proc.stdout, proc.stderr)


# ---------------------------------------------------------------------------
# Standalone Test Runner
# ---------------------------------------------------------------------------


def _tests() -> list[tuple[str, Any]]:
    items = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def main() -> int:
    failures: list[str] = []
    tests = _tests()
    for name, func in tests:
        try:
            func()
        except Exception:
            failures.append(name)
            print(f"FAIL  {name}")
            print(traceback.format_exc().rstrip())
        else:
            print(f"pass  {name}")
    total = len(tests)
    print(f"\n{total - len(failures)} passed, {len(failures)} failed, out of {total}")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
