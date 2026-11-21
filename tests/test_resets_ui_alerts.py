"""Comprehensive tests for Reset Radar UI, Menu-Bar Next-Unlock Chip & Smart Reset Alerts.

Covers:
  1. `contracts.py`: `title_show_next_unlock` (default False) & `reset_imminent_alert_enabled`
     (default True) in `SETTINGS_DEFAULTS` + `normalize_settings`.
  2. `app.py`: default-off byte-identical title guarantee, opt-in `🔓3h`/`🔓35m` next-unlock
     token (`<= 24h`, `"good"`/`"dim"` color, excluded in compact & icon-only modes, safe
     fallback when `cc_usage_widget.resets` is absent), and Settings submenu entries.
  3. `themes/belkins.py`:
     - Unified `↺ Reset Radar` card on `Hub` tab (compact top-3 vs expanded top-8 via
       `_RESET_RADAR_EXPANDED`, `keep_open=True`, live banked credit badge in emerald/aqua,
       2-pill Fleet Runway Strip, unified `✦`/`◈` timeline with `🔓 UNLOCK` / `⏳ USE` / `↺`
       pills, clickable perishable Claude & Codex switch rows, 1-line Smart Fleet Advisor
       strip with full detail tooltip, zero raw email leaks).
     - All Hub view modes (`luxury`, `detailed`, `bento`, `cleanmymac`) + real AppKit
       Dark (DarkAqua) and Light (Aqua) appearance rendering.
     - `Codex` hero & standby rows: banked reset credit badges (`↺ 3×` aqua, `↺ USE 1×`
       emerald) + concise reset clocks (`↺ 4d`, `↺ 3h`).
     - `Claude` standby rows: concise reset countdown (`↺ 3h`) & capped badge (`Capped · 3h`).
     - `Settings` tab `Unlock: ON/OFF` & `Reset Bell: ON/OFF` toggles (`keep_open=True`).
     - Graceful fallback when `cc_usage_widget.resets` is absent.
  4. `notify.py`:
     - Imminent unlock alert (`unlock_soon:<vendor>:<slot>:<window>:<reset_bucket>`) for
       capped (`>= 95%`) Claude Fable and Codex seats `<= 15m` before reset.
     - Codex reset-credit ready alert (`reset_credit:<slot>:<reset_bucket>`) for Codex seats
       `>= 90%` with `reset_credits_usable > 0`.
     - Ledger dedup across ticks, API clock jitter, and restarts; re-fires on next reset
       window; gated by both `notifications_enabled` and `reset_imminent_alert_enabled`.

Uses the REAL `cc_usage_widget.resets` module when present; installs a faithful in-test
stub ONLY if `cc_usage_widget.resets` is not yet merged into the workspace.

Run directly::

    CC_USAGE_WIDGET_NO_REVEAL=1 ~/.local/share/uv/tools/claude-swap/bin/python tests/test_resets_ui_alerts.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import math
import os
import sys
import tempfile
import traceback
import types
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

import cc_usage_widget  # noqa: E402
from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import belkins_game  # noqa: E402
from cc_usage_widget import card_base  # noqa: E402
from cc_usage_widget import notify as notify_mod  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    AccountRow,
    normalize_settings,
)
from cc_usage_widget.themes import ThemeActions  # noqa: E402
from cc_usage_widget.themes import belkins as belkins_theme  # noqa: E402

try:
    from cc_usage_widget import resets as _REAL_RESETS  # type: ignore[attr-defined]
except ImportError:
    _REAL_RESETS = None


# ---------------------------------------------------------------------------
# Faithful in-test stub for cc_usage_widget.resets (USED ONLY BEFORE MERGE)
# ---------------------------------------------------------------------------


def _build_resets_stub() -> types.ModuleType:
    """Build a spec-exact stub matching `resets_core`'s public contract."""
    mod = types.ModuleType("cc_usage_widget.resets")

    @dataclass(frozen=True, slots=True)
    class ResetEvent:
        vendor: str
        slot: int
        alias: str
        window: str
        used_pct: float | None
        free_pct: float | None
        resets_at_epoch: float
        seconds_until: float
        reset_label: str
        countdown_label: str
        kind: str
        is_active: bool = False
        disabled: bool = False
        plan_label: str = ""
        no_fable: bool = False

    @dataclass(frozen=True, slots=True)
    class ResetCreditAccount:
        vendor: str
        slot: int
        alias: str
        available: int
        usable: int
        used_pct: float | None
        is_active: bool = False
        needs_relogin: bool = False

    @dataclass(frozen=True, slots=True)
    class ResetCreditsSummary:
        total_available: int
        live_available: int
        total_usable: int
        accounts: tuple[ResetCreditAccount, ...]

    @dataclass(frozen=True, slots=True)
    class FleetCapacitySummary:
        claude_fable_free_seats: float
        claude_fable_total_seats: int
        claude_locked_relogin_seats: int
        claude_next_unlock: ResetEvent | None
        codex_free_seats: float
        codex_total_seats: int
        codex_locked_relogin_seats: int
        codex_next_unlock: ResetEvent | None
        perishable_seats_48h: float

    @dataclass(frozen=True, slots=True)
    class FleetRecommendation:
        kind: str
        headline: str
        detail: str
        target_vendor: str | None = None
        target_slot: int | None = None

    @dataclass(frozen=True, slots=True)
    class FleetResetRadar:
        generated_at: float
        events: tuple[ResetEvent, ...]
        unlocks: tuple[ResetEvent, ...]
        perishable: tuple[ResetEvent, ...]
        credits: ResetCreditsSummary
        capacity: FleetCapacitySummary
        recommendation: FleetRecommendation | None

    def parse_reset_epoch(value: Any, *, now: float | None = None) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            v = float(value)
            return v if math.isfinite(v) and v > 0.0 else None
        raw = str(value or "").strip()
        if not raw:
            return None
        if "T" in raw:
            iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
            try:
                parsed = dt.datetime.fromisoformat(iso)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return parsed.timestamp()
            except ValueError:
                return None
        if now is None:
            return None
        base = dt.datetime.fromtimestamp(float(now))
        if ":" in raw and " " not in raw:
            hh, _, mm = raw.partition(":")
            if hh.isdigit() and mm.isdigit():
                cand = base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if cand.timestamp() < float(now) - 30.0:
                    cand = cand + dt.timedelta(days=1)
                return cand.timestamp()
        try:
            cand = dt.datetime.strptime(f"{base.year} {raw}", "%Y %b %d %H:%M")
            if cand < base - dt.timedelta(days=183):
                cand = cand.replace(year=base.year + 1)
            return cand.timestamp()
        except ValueError:
            return None

    def format_countdown(seconds: float) -> str:
        s = float(seconds)
        if s <= 0.0:
            return "now"
        if s < 60.0:
            return "<1m"
        if s < 3_600.0:
            return f"{int(round(s / 60.0))}m"
        if s < 86_400.0:
            hrs = int(s // 3_600)
            mins = int((s % 3_600) // 60)
            return f"{hrs}h {mins}m" if mins else f"{hrs}h"
        days = int(s // 86_400)
        hrs = int((s % 86_400) // 3_600)
        return f"{days}d {hrs}h" if hrs else f"{days}d"

    def format_local_reset_label(epoch: float, *, now: float | None = None) -> str:
        when = dt.datetime.fromtimestamp(float(epoch))
        base = dt.datetime.fromtimestamp(float(now if now is not None else epoch))
        delta_days = (when.date() - base.date()).days
        if delta_days == 0:
            return when.strftime("%H:%M")
        if delta_days == 1:
            return f"Tom {when.strftime('%H:%M')}"
        if 1 < delta_days <= 6:
            return f"{when.strftime('%a')} {when.strftime('%H:%M')}"
        return f"{when.strftime('%b')} {when.day} {when.strftime('%H:%M')}"

    def _row_alias(row: Any) -> str:
        al = str(getattr(row, "alias", "") or "").strip()
        if al:
            return al
        return f"slot {getattr(row, 'slot', '?')}"

    def build_fleet_reset_radar(
        accounts: Any,
        quota_rows: Any,
        *,
        account_notes: Any = None,
        now: float | None = None,
        fable_guard: bool = True,
    ) -> FleetResetRadar:
        ts = float(now if now is not None else dt.datetime.now().timestamp())
        notes = dict(account_notes or {})
        claude_rows = tuple(accounts or ())
        codex_rows = tuple(
            r for r in (quota_rows or ()) if getattr(r, "vendor", "") == VENDOR_CODEX and getattr(r, "slot", 0) < 0
        )

        has_fable_fleet = any(
            any(str(n).lower().startswith("fable") for n, _ in (getattr(r, "scoped_windows", ()) or ()))
            for r in claude_rows
            if not getattr(r, "disabled", False) and r.slot not in notes
        )

        events: list[ResetEvent] = []
        fable_free = 0.0
        fable_total = 0
        claude_locked = 0

        for r in claude_rows:
            if r.slot in notes:
                claude_locked += 1
                continue
            if getattr(r, "disabled", False):
                continue
            scoped = dict(getattr(r, "scoped_windows", ()) or ())
            scoped_res = dict(getattr(r, "scoped_resets_at", ()) or ())
            fable_key = next((k for k in scoped if str(k).lower().startswith("fable")), None)
            no_fable = bool(fable_guard and has_fable_fleet and fable_key is None)
            if not no_fable and fable_key is not None and scoped[fable_key] is not None:
                fable_total += 1
                fable_free += max(0.0, 1.0 - float(scoped[fable_key]) / 100.0)
            expired = set(getattr(r, "expired_windows", ()) or ())
            raw_candidates: list[tuple[str, float | None, Any, bool]] = []
            if fable_key is not None and fable_key not in expired:
                raw_candidates.append((fable_key, scoped.get(fable_key), scoped_res.get(fable_key), True))
            if "seven_day" not in expired and getattr(r, "seven_day_pct", None) is not None:
                raw_candidates.append(("7d", r.seven_day_pct, getattr(r, "seven_day_resets_at", None), True))
            if "five_hour" not in expired and getattr(r, "five_hour_pct", None) is not None:
                raw_candidates.append(("5h", r.five_hour_pct, getattr(r, "five_hour_resets_at", None), False))
            by_min: dict[int, ResetEvent] = {}
            for wname, pct, raw_rst, is_weekly in raw_candidates:
                ep = parse_reset_epoch(raw_rst, now=ts)
                if ep is None or ep <= ts - 30.0:
                    continue
                secs = max(0.0, ep - ts)
                used = float(pct) if pct is not None else None
                free = (max(0.0, 100.0 - used) if used is not None else None)
                if no_fable:
                    kind = "upcoming"
                elif used is not None and used >= 85.0:
                    kind = "unlock"
                elif is_weekly and free is not None and free >= 25.0 and secs <= 48.0 * 3600.0:
                    kind = "perishable"
                else:
                    kind = "upcoming"
                ev = ResetEvent(
                    vendor="claude",
                    slot=int(r.slot),
                    alias=_row_alias(r),
                    window=wname,
                    used_pct=used,
                    free_pct=free,
                    resets_at_epoch=ep,
                    seconds_until=secs,
                    reset_label=format_local_reset_label(ep, now=ts),
                    countdown_label=format_countdown(secs),
                    kind=kind,
                    is_active=bool(r.is_active),
                    disabled=bool(r.disabled),
                    plan_label=str(getattr(r, "plan_type", "") or ""),
                    no_fable=no_fable,
                )
                mkey = int(ep // 60)
                prev = by_min.get(mkey)
                if prev is None or wname.lower().startswith("fable") or (used or 0.0) > (prev.used_pct or 0.0):
                    by_min[mkey] = ev
            events.extend(by_min.values())

        codex_free = 0.0
        codex_total = 0
        codex_locked = 0
        credit_accts: list[ResetCreditAccount] = []
        total_avail = 0
        live_avail = 0
        total_usable = 0

        for r in codex_rows:
            relogin = getattr(r, "attention_kind", "") == "warn"
            avail = int(getattr(r, "reset_credits_available", None) or 0)
            usable = int(getattr(r, "reset_credits_usable", None) or 0)
            if avail > 0 or usable > 0:
                total_avail += avail
                if not relogin and not getattr(r, "disabled", False):
                    live_avail += avail
                    total_usable += usable
                credit_accts.append(
                    ResetCreditAccount(
                        vendor="codex",
                        slot=int(r.slot),
                        alias=_row_alias(r),
                        available=avail,
                        usable=usable,
                        used_pct=r.seven_day_pct,
                        is_active=bool(r.is_active),
                        needs_relogin=relogin,
                    )
                )
            if relogin:
                codex_locked += 1
                continue
            if getattr(r, "disabled", False):
                continue
            if r.seven_day_pct is not None:
                codex_total += 1
                codex_free += max(0.0, 1.0 - float(r.seven_day_pct) / 100.0)
            ep = parse_reset_epoch(getattr(r, "seven_day_resets_at", None), now=ts)
            if ep is None:
                ep = parse_reset_epoch(getattr(r, "soonest_reset_at", None), now=ts)
            if ep is not None and ep > ts - 30.0:
                secs = max(0.0, ep - ts)
                used = float(r.seven_day_pct) if r.seven_day_pct is not None else None
                free = max(0.0, 100.0 - used) if used is not None else None
                if used is not None and used >= 85.0:
                    kind = "unlock"
                elif free is not None and free >= 25.0 and secs <= 48.0 * 3600.0:
                    kind = "perishable"
                else:
                    kind = "upcoming"
                events.append(
                    ResetEvent(
                        vendor="codex",
                        slot=int(r.slot),
                        alias=_row_alias(r),
                        window="7d",
                        used_pct=used,
                        free_pct=free,
                        resets_at_epoch=ep,
                        seconds_until=secs,
                        reset_label=format_local_reset_label(ep, now=ts),
                        countdown_label=format_countdown(secs),
                        kind=kind,
                        is_active=bool(r.is_active),
                        disabled=bool(r.disabled),
                        plan_label=str(getattr(r, "plan_type", "") or ""),
                        no_fable=False,
                    )
                )

        events.sort(key=lambda e: (e.seconds_until, e.vendor, e.slot))
        unlocks = tuple(e for e in events if e.kind == "unlock" and not e.disabled and not e.no_fable)
        perishable = tuple(e for e in events if e.kind == "perishable" and not e.disabled and not e.no_fable)
        claude_next = next((e for e in unlocks if e.vendor == "claude"), None)
        codex_next = next((e for e in unlocks if e.vendor == "codex"), None)
        perish_seats = sum((e.free_pct or 0.0) / 100.0 for e in perishable)

        credits_summary = ResetCreditsSummary(
            total_available=total_avail,
            live_available=live_avail,
            total_usable=total_usable,
            accounts=tuple(credit_accts),
        )
        capacity_summary = FleetCapacitySummary(
            claude_fable_free_seats=round(fable_free, 2),
            claude_fable_total_seats=fable_total,
            claude_locked_relogin_seats=claude_locked,
            claude_next_unlock=claude_next,
            codex_free_seats=round(codex_free, 2),
            codex_total_seats=codex_total,
            codex_locked_relogin_seats=codex_locked,
            codex_next_unlock=codex_next,
            perishable_seats_48h=round(perish_seats, 2),
        )

        rec: FleetRecommendation | None = None
        credit_target = next(
            (c for c in credit_accts if not c.needs_relogin and c.usable > 0 and (c.used_pct or 0.0) >= 85.0),
            None,
        )
        perish_target = next(
            (e for e in perishable if (e.free_pct or 0.0) >= 40.0 and e.seconds_until <= 36.0 * 3600.0),
            None,
        )
        if credit_target is not None:
            rec = FleetRecommendation(
                kind="use_reset_credit",
                headline=f"Use {credit_target.usable}× reset credit on {credit_target.alias}",
                detail=f"{credit_target.alias} is at {credit_target.used_pct:.0f}% with {credit_target.usable} usable Codex reset credit(s).",
                target_vendor="codex",
                target_slot=credit_target.slot,
            )
        elif perish_target is not None:
            rec = FleetRecommendation(
                kind="burn_perishable",
                headline=f"Burn {perish_target.alias} ({perish_target.free_pct:.0f}% free · {perish_target.countdown_label})",
                detail=f"{perish_target.alias} has {perish_target.free_pct:.0f}% {perish_target.window} quota expiring in {perish_target.countdown_label}.",
                target_vendor=perish_target.vendor,
                target_slot=perish_target.slot,
            )
        elif unlocks and unlocks[0].seconds_until <= 6.0 * 3600.0 and (fable_free + codex_free) <= 1.5:
            u = unlocks[0]
            rec = FleetRecommendation(
                kind="unlock_soon",
                headline=f"{u.alias} unlocks in {u.countdown_label}",
                detail=f"Fresh {u.window} capacity arrives on {u.alias} at {u.reset_label}.",
                target_vendor=u.vendor,
                target_slot=u.slot,
            )
        elif (claude_locked + codex_locked) >= 2:
            rec = FleetRecommendation(
                kind="relogin_capacity",
                headline=f"Re-login {claude_locked + codex_locked} locked seats to recover capacity",
                detail=f"{claude_locked} Claude and {codex_locked} Codex seats need re-login.",
                target_vendor="claude" if claude_locked >= codex_locked else "codex",
                target_slot=None,
            )
        else:
            rec = FleetRecommendation(
                kind="healthy",
                headline=f"Fleet healthy: {fable_free:.1f} Fable · {codex_free:.1f} Codex free",
                detail="No urgent expirations or locked capacity.",
            )

        return FleetResetRadar(
            generated_at=ts,
            events=tuple(events),
            unlocks=unlocks,
            perishable=perishable,
            credits=credits_summary,
            capacity=capacity_summary,
            recommendation=rec,
        )

    mod.ResetEvent = ResetEvent  # type: ignore[attr-defined]
    mod.ResetCreditAccount = ResetCreditAccount  # type: ignore[attr-defined]
    mod.ResetCreditsSummary = ResetCreditsSummary  # type: ignore[attr-defined]
    mod.FleetCapacitySummary = FleetCapacitySummary  # type: ignore[attr-defined]
    mod.FleetRecommendation = FleetRecommendation  # type: ignore[attr-defined]
    mod.FleetResetRadar = FleetResetRadar  # type: ignore[attr-defined]
    mod.parse_reset_epoch = parse_reset_epoch  # type: ignore[attr-defined]
    mod.format_countdown = format_countdown  # type: ignore[attr-defined]
    mod.format_local_reset_label = format_local_reset_label  # type: ignore[attr-defined]
    mod.build_fleet_reset_radar = build_fleet_reset_radar  # type: ignore[attr-defined]
    return mod


@contextlib.contextmanager
def _ensure_resets() -> Iterator[Any]:
    """Use the REAL `cc_usage_widget.resets` if merged, else inject the in-test stub."""
    if _REAL_RESETS is not None:
        yield _REAL_RESETS
        return
    stub = _build_resets_stub()
    prev_mod = sys.modules.get("cc_usage_widget.resets")
    had_attr = hasattr(cc_usage_widget, "resets")
    prev_attr = getattr(cc_usage_widget, "resets", None)
    sys.modules["cc_usage_widget.resets"] = stub
    setattr(cc_usage_widget, "resets", stub)
    try:
        yield stub
    finally:
        if prev_mod is None:
            sys.modules.pop("cc_usage_widget.resets", None)
        else:
            sys.modules["cc_usage_widget.resets"] = prev_mod
        if had_attr:
            setattr(cc_usage_widget, "resets", prev_attr)
        elif hasattr(cc_usage_widget, "resets"):
            delattr(cc_usage_widget, "resets")


@contextlib.contextmanager
def _mask_resets_absent() -> Iterator[None]:
    """Temporarily make `from cc_usage_widget import resets` fail with ImportError."""
    prev_mod = sys.modules.get("cc_usage_widget.resets")
    had_attr = hasattr(cc_usage_widget, "resets")
    prev_attr = getattr(cc_usage_widget, "resets", None)
    sys.modules["cc_usage_widget.resets"] = None  # type: ignore[assignment]
    if had_attr:
        delattr(cc_usage_widget, "resets")
    try:
        yield
    finally:
        if prev_mod is None:
            sys.modules.pop("cc_usage_widget.resets", None)
        else:
            sys.modules["cc_usage_widget.resets"] = prev_mod
        if had_attr:
            setattr(cc_usage_widget, "resets", prev_attr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = dt.datetime(2026, 11, 21, 10, 0, 0).timestamp()


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claude(
    slot: int,
    alias: str,
    *,
    is_active: bool = False,
    five_hour_pct: float | None = 15.0,
    seven_day_pct: float | None = 30.0,
    scoped_windows: tuple[tuple[str, float], ...] = (),
    five_hour_resets_at: str | None = None,
    seven_day_resets_at: str | None = None,
    scoped_resets_at: tuple[tuple[str, str], ...] = (),
    plan_type: str = "claude_max_20x",
    disabled: bool = False,
    **kwargs: Any,
) -> AccountRow:
    email = kwargs.pop("email", f"secret-{alias}@private-org.example")
    return AccountRow(
        slot=slot,
        alias=alias,
        email=email,
        is_active=is_active,
        five_hour_pct=five_hour_pct,
        seven_day_pct=seven_day_pct,
        scoped_windows=scoped_windows,
        five_hour_resets_at=five_hour_resets_at,
        seven_day_resets_at=seven_day_resets_at,
        scoped_resets_at=scoped_resets_at,
        plan_type=plan_type,
        disabled=disabled,
        vendor=VENDOR_CLAUDE,
        switchable=True,
        **kwargs,
    )


def _codex(
    slot: int,
    alias: str,
    *,
    is_active: bool = False,
    seven_day_pct: float | None = 25.0,
    five_hour_pct: float | None = None,
    seven_day_resets_at: str | None = None,
    five_hour_resets_at: str | None = None,
    soonest_reset_at: float | None = None,
    reset_credits_available: int | None = None,
    reset_credits_usable: int | None = None,
    plan_type: str = "pro",
    disabled: bool = False,
    attention_note: str = "",
    attention_kind: str = "",
    **kwargs: Any,
) -> AccountRow:
    email = kwargs.pop("email", f"secret-{alias}@codex-private.example")
    return AccountRow(
        slot=slot,
        alias=alias,
        email=email,
        is_active=is_active,
        five_hour_pct=five_hour_pct,
        seven_day_pct=seven_day_pct,
        five_hour_resets_at=five_hour_resets_at,
        seven_day_resets_at=seven_day_resets_at,
        soonest_reset_at=soonest_reset_at,
        reset_credits_available=reset_credits_available,
        reset_credits_usable=reset_credits_usable,
        plan_type=plan_type,
        disabled=disabled,
        attention_note=attention_note,
        attention_kind=attention_kind,
        vendor=VENDOR_CODEX,
        switchable=False,
        usage_age_seconds=kwargs.pop("usage_age_seconds", 60.0),
        stale_after_seconds=21_600.0,
        **kwargs,
    )


class _RecPen:
    """Recording Pen stand-in capturing every text, pill, and role drawn."""

    hl = False

    def __init__(self) -> None:
        self._m = belkins_theme._Measure()
        self.texts: list[str] = []
        self.pills: list[tuple[str, Any, Any]] = []

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
            self.pills.append((text_s, k.get("role"), k.get("bg")))
        return self._m.pill(text_s, *a, **k)

    def symbol(self, *a: Any, **k: Any) -> float:
        return self._m.symbol(*a, **k)

    def _noop(self, *a: Any, **k: Any) -> None:
        return None

    rrect = panel = bar = circle = line = _noop


def _paint_blocks(blocks: list[Any]) -> _RecPen:
    rec = _RecPen()
    for blk in blocks:
        if blk.paint is not None:
            blk.paint(rec, belkins_theme.W, blk.height)
    return rec


def _mixed_fleet_snapshot(
    *,
    usable_credits: int = 1,
    hub_view: str = "luxury",
    extra_settings: dict[str, Any] | None = None,
) -> app_mod.UiSnapshot:
    """Rich synthetic Claude + Codex fleet with unlocks, perishables, upcoming resets & credits."""
    claude_rows = (
        _claude(
            1,
            "vlad",
            is_active=True,
            five_hour_pct=20.0,
            seven_day_pct=45.0,
            scoped_windows=(("Fable", 50.0),),
            five_hour_resets_at=_iso(NOW + 4 * 3600),
            seven_day_resets_at=_iso(NOW + 4 * 86400),
            scoped_resets_at=(("Fable", _iso(NOW + 4 * 86400)),),
        ),
        _claude(
            2,
            "dina",
            is_active=False,
            five_hour_pct=0.0,
            seven_day_pct=35.0,
            scoped_windows=(("Fable", 100.0),),
            seven_day_resets_at=_iso(NOW + 3 * 3600),
            scoped_resets_at=(("Fable", _iso(NOW + 3 * 3600)),),
        ),
        _claude(
            3,
            "ana",
            is_active=False,
            five_hour_pct=5.0,
            seven_day_pct=10.0,
            scoped_windows=(("Fable", 10.0),),
            seven_day_resets_at=_iso(NOW + 20 * 3600),
            scoped_resets_at=(("Fable", _iso(NOW + 20 * 3600)),),
        ),
        _claude(
            4,
            "team_only",
            is_active=False,
            five_hour_pct=12.0,
            seven_day_pct=20.0,
            scoped_windows=(),
            seven_day_resets_at=_iso(NOW + 72 * 3600),
            plan_type="claude_team",
        ),
    )
    codex_rows = (
        _codex(
            -1,
            "codex_main",
            is_active=True,
            seven_day_pct=92.0,
            seven_day_resets_at=_iso(NOW + 50 * 3600),
            soonest_reset_at=NOW + 50 * 3600,
            reset_credits_available=1,
            reset_credits_usable=usable_credits,
        ),
        _codex(
            -2,
            "codex_perish",
            is_active=False,
            seven_day_pct=15.0,
            seven_day_resets_at=_iso(NOW + 18 * 3600),
            soonest_reset_at=NOW + 18 * 3600,
            reset_credits_available=3,
            reset_credits_usable=0,
        ),
        _codex(
            -3,
            "codex_cap",
            is_active=False,
            seven_day_pct=100.0,
            seven_day_resets_at=_iso(NOW + 5 * 3600),
            soonest_reset_at=NOW + 5 * 3600,
            reset_credits_available=0,
            reset_credits_usable=0,
        ),
        _codex(
            -4,
            "codex_dead",
            is_active=False,
            seven_day_pct=None,
            attention_note="relogin",
            attention_kind="warn",
            reset_credits_available=2,
            reset_credits_usable=0,
        ),
    )
    settings = normalize_settings(
        {
            **SETTINGS_DEFAULTS,
            "menu_theme": "belkins",
            "belkins_hub_view": hub_view,
            "belkins_fable_guard": True,
            **(extra_settings or {}),
        }
    )
    return app_mod.UiSnapshot(
        settings=settings,
        accounts=claude_rows,
        active=claude_rows[0],
        quota_rows=codex_rows,
        autoswitch_enabled=True,
        autoswitch_threshold=85.0,
        autoswitch_models=("all",),
    )


# ---------------------------------------------------------------------------
# 1. Contracts & Settings Defaults
# ---------------------------------------------------------------------------


def test_settings_defaults_and_normalization() -> None:
    assert SETTINGS_DEFAULTS["title_show_next_unlock"] is False
    assert SETTINGS_DEFAULTS["reset_imminent_alert_enabled"] is True

    defaulted = normalize_settings(None)
    assert defaulted["title_show_next_unlock"] is False
    assert defaulted["reset_imminent_alert_enabled"] is True

    flipped = normalize_settings(
        {"title_show_next_unlock": True, "reset_imminent_alert_enabled": False}
    )
    assert flipped["title_show_next_unlock"] is True
    assert flipped["reset_imminent_alert_enabled"] is False

    # Invalid types fall back cleanly to defaults
    bad = normalize_settings(
        {"title_show_next_unlock": "yes", "reset_imminent_alert_enabled": "nope"}
    )
    assert bad["title_show_next_unlock"] is False
    assert bad["reset_imminent_alert_enabled"] is True


# ---------------------------------------------------------------------------
# 2. App: Menu-Bar Next-Unlock Chip & Settings Submenu Wiring
# ---------------------------------------------------------------------------


def test_title_next_unlock_default_off_byte_identical_and_opt_in() -> None:
    with _ensure_resets():
        app = app_mod.CCUsageWidgetApp()
        try:
            app._now = lambda: NOW  # type: ignore[method-assign]
            base_settings = normalize_settings(
                {
                    **SETTINGS_DEFAULTS,
                    "title_show_icon": False,
                    "title_show_cost": False,
                    "title_show_next_unlock": False,
                }
            )
            snap_3h = _mixed_fleet_snapshot(extra_settings=base_settings)

            # Default OFF: 100% byte-identical (includes `⚠` from codex_dead, zero unlock glyph)
            title_off = app.render_title(snap_3h)
            assert title_off == "vlad 20% F50% ⚠", title_off
            assert "🔓" not in title_off

            # Opt-in ON: soonest capped Fable seat unlocks in 3h -> `🔓3h` in `"dim"`
            snap_on = replace(
                snap_3h,
                settings={**base_settings, "title_show_next_unlock": True},
            )
            tokens_3h = app.title_tokens(snap_on)
            title_3h = app.render_title(snap_on)
            assert "🔓3h" in title_3h, title_3h
            assert any(t == [("🔓3h", "dim")] for t in tokens_3h), tokens_3h

            # Imminent unlock <= 1h (Codex seat unlocking in 35m) -> `"🔓35m"` in `"good"`
            soon_codex = _codex(
                -5,
                "codex_soon",
                is_active=False,
                seven_day_pct=100.0,
                seven_day_resets_at=_iso(NOW + 35 * 60),
                soonest_reset_at=NOW + 35 * 60,
            )
            snap_35m = replace(snap_on, quota_rows=snap_on.quota_rows + (soon_codex,))
            tokens_35m = app.title_tokens(snap_35m)
            assert any(t == [("🔓35m", "good")] for t in tokens_35m), tokens_35m

            # Beyond 24h (> 86400s): no `🔓` token emitted
            far_claude = (
                _claude(
                    1,
                    "vlad",
                    is_active=True,
                    five_hour_pct=20.0,
                    scoped_windows=(("Fable", 50.0),),
                ),
                _claude(
                    2,
                    "dina",
                    is_active=False,
                    scoped_windows=(("Fable", 100.0),),
                    scoped_resets_at=(("Fable", _iso(NOW + 30 * 3600)),),
                ),
            )
            snap_far = replace(snap_on, accounts=far_claude, active=far_claude[0], quota_rows=())
            assert "🔓" not in app.render_title(snap_far), app.render_title(snap_far)

            # Compact and icon-only modes NEVER show `🔓` even when title_show_next_unlock=True
            snap_compact = replace(
                snap_on,
                settings={**snap_on.settings, "title_compact": True},
            )
            assert "🔓" not in app.render_title(snap_compact), app.render_title(snap_compact)
            snap_icon_only = replace(
                snap_on,
                settings={**snap_on.settings, "title_view": "icon_only"},
            )
            assert app.title_tokens(snap_icon_only) == []

            # Settings submenus expose both new toggles cleanly
            settings_menu = app._settings_submenu(snap_on)
            title_items = [str(k) for k in settings_menu["Title"].keys()]
            notif_items = [str(k) for k in settings_menu["Notifications"].keys()]
            assert any("Next unlock countdown" in t for t in title_items), title_items
            assert any("Imminent unlock" in t for t in notif_items), notif_items

            # Graceful fallback when resets module is absent
            with _mask_resets_absent():
                assert app.render_title(snap_on) == title_off
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# 3. Belkins UI: Reset Radar Card (Compact/Expanded, Perishable Click, Views)
# ---------------------------------------------------------------------------


def test_belkins_reset_radar_card_compact_expanded_and_perishable_click() -> None:
    with tempfile.TemporaryDirectory() as tmp, _ensure_resets():
        orig_path = belkins_game._shop_save_path
        orig_tab = belkins_theme.get_active_tab()
        orig_exp = belkins_theme.get_reset_radar_expanded()
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_theme.set_active_tab("party")
            belkins_theme.set_reset_radar_expanded(False)

            switched_claude: list[int] = []
            switched_codex: list[str] = []
            actions = ThemeActions(
                switch_to=lambda slot: switched_claude.append(slot),
                switch_codex=lambda alias: switched_codex.append(alias),
            )

            snap = _mixed_fleet_snapshot(usable_credits=1, hub_view="luxury")
            blocks = belkins_theme.build(snap, NOW, actions)

            # Unique block keys across entire build
            keys = [b.key for b in blocks]
            assert len(keys) == len(set(keys)), f"duplicate keys: {keys}"

            # Compact Reset Radar blocks visible, expanded blocks hidden
            compact_blocks = [
                b for b in blocks if b.key.startswith("belkins-hub-radar-") and not b.key.startswith("belkins-hub-radar-exp-")
            ]
            exp_blocks = [b for b in blocks if b.key.startswith("belkins-hub-radar-exp-")]
            assert compact_blocks, "expected compact Reset Radar blocks"
            assert exp_blocks, "expected expandable Reset Radar blocks (> 3 events)"
            assert all(b.is_visible() for b in compact_blocks if b.is_visible is not None)
            assert all(not b.is_visible() for b in exp_blocks if b.is_visible is not None)

            compact_ev_blocks = [b for b in compact_blocks if "-ev-" in b.key]
            exp_ev_blocks = [b for b in exp_blocks if "-ev-" in b.key]
            assert len(compact_ev_blocks) == 3, [b.key for b in compact_ev_blocks]
            assert 4 <= len(exp_ev_blocks) <= 8, [b.key for b in exp_ev_blocks]

            # Paint compact card & check header, credits badge, runway, timeline, advisor
            rec = _paint_blocks(compact_blocks)
            assert "↺ Reset Radar" in rec.texts, rec.texts
            assert any(p[0] == "Show all" for p in rec.pills), rec.pills
            # Emerald banked credit pill when total_usable > 0
            banked_pills = [p for p in rec.pills if "4× banked" in p[0]]
            assert banked_pills and banked_pills[0][1] == "good_text" and banked_pills[0][2] == "belkins_good_pill", banked_pills
            assert any("✦ Fable:" in t and "seats" in t for t in rec.texts), rec.texts
            assert any("◈ Codex:" in t and "seats" in t for t in rec.texts), rec.texts
            assert any("Next 🔓 dina" in t for t in rec.texts), rec.texts
            assert any(p[0] == "🔓 UNLOCK" and p[1] == "good_text" for p in rec.pills), rec.pills

            # Advisor strip present with tooltip detail
            adv_blk = next(b for b in compact_blocks if b.key == "belkins-hub-radar-advisor")
            assert adv_blk.tooltip and "\n" in adv_blk.tooltip, adv_blk.tooltip

            # Privacy check: zero raw email strings anywhere in Reset Radar card!
            for b in compact_blocks + exp_blocks:
                assert "@private-org.example" not in b.tooltip and "@codex-private.example" not in b.tooltip
                assert "@private-org.example" not in b.ax and "@codex-private.example" not in b.ax
            assert not any("@" in t for t in rec.texts), rec.texts

            # Click header toggle (`keep_open=True`) -> expands in-menu!
            head_blk = next(b for b in compact_blocks if b.key == "belkins-hub-radar-head")
            assert head_blk.keep_open is True and callable(head_blk.action)
            head_blk.action()
            assert belkins_theme.get_reset_radar_expanded() is True
            assert all(not b.is_visible() for b in compact_blocks if b.is_visible is not None)
            assert all(b.is_visible() for b in exp_blocks if b.is_visible is not None)

            rec_exp = _paint_blocks(exp_blocks)
            assert any(p[0] == "Compact" for p in rec_exp.pills), rec_exp.pills
            assert any(p[0] == "⏳ USE" and p[1] == "warn_text" for p in rec_exp.pills), rec_exp.pills

            # Click healthy perishable Claude & Codex rows -> switches straight to those seats!
            perish_codex_blk = next(
                b for b in exp_ev_blocks if "codex_perish" in b.tooltip and "⏳ USE" in b.ax
            )
            assert callable(perish_codex_blk.action)
            perish_codex_blk.action()
            assert switched_codex == ["codex_perish"], switched_codex

            perish_claude_blk = next(
                b for b in exp_ev_blocks if "ana" in b.tooltip and "⏳ USE" in b.ax
            )
            assert callable(perish_claude_blk.action)
            perish_claude_blk.action()
            assert switched_claude == [3], switched_claude

            # Non-perishable unlock row is NOT clickable (no dead action!)
            unlock_blk = next(b for b in exp_ev_blocks if "dina" in b.tooltip and "🔓 UNLOCK" in b.ax)
            assert unlock_blk.action is None

            # Toggle back to compact via expanded header
            exp_head = next(b for b in exp_blocks if b.key == "belkins-hub-radar-exp-head")
            exp_head.action()
            assert belkins_theme.get_reset_radar_expanded() is False

            # When usable_credits == 0 but live_available > 0 -> aqua pill (`belkins_cmm_aqua`)!
            snap_aqua = _mixed_fleet_snapshot(usable_credits=0, hub_view="luxury")
            blocks_aqua = belkins_theme.build(snap_aqua, NOW, actions)
            compact_aqua = [
                b for b in blocks_aqua if b.key.startswith("belkins-hub-radar-") and not b.key.startswith("belkins-hub-radar-exp-")
            ]
            rec_aqua = _paint_blocks(compact_aqua)
            aqua_pills = [p for p in rec_aqua.pills if "4× banked" in p[0]]
            assert aqua_pills and aqua_pills[0][1] == "belkins_cmm_aqua", aqua_pills
        finally:
            belkins_theme.set_reset_radar_expanded(orig_exp)
            belkins_theme.set_active_tab(orig_tab)
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_game._shop_save_path = orig_path


def test_belkins_reset_radar_all_hub_views_and_dark_light_render() -> None:
    with tempfile.TemporaryDirectory() as tmp, _ensure_resets():
        orig_path = belkins_game._shop_save_path
        orig_tab = belkins_theme.get_active_tab()
        orig_exp = belkins_theme.get_reset_radar_expanded()
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_theme.set_active_tab("party")
            belkins_theme.set_reset_radar_expanded(False)

            for view_mode in ("luxury", "bento", "detailed", "cleanmymac"):
                snap = _mixed_fleet_snapshot(hub_view=view_mode)
                blocks = belkins_theme.build(snap, NOW, ThemeActions())
                visible_radar = [
                    b
                    for b in blocks
                    if "-radar-" in b.key and (b.is_visible is None or b.is_visible())
                ]
                assert visible_radar, f"expected visible radar blocks in {view_mode}"
                rec = _paint_blocks(visible_radar)
                assert "↺ Reset Radar" in rec.texts, (view_mode, rec.texts)

            # Real AppKit Dark & Light appearance paint smoke test
            if card_base.available():
                from AppKit import (
                    NSAppearance,
                    NSAppearanceNameAqua,
                    NSAppearanceNameDarkAqua,
                    NSImage,
                )

                snap_lux = _mixed_fleet_snapshot(hub_view="luxury")
                blocks_lux = belkins_theme.build(snap_lux, NOW, ThemeActions())
                radar_blocks = [
                    b for b in blocks_lux if b.key.startswith("belkins-hub-radar-") and not b.key.startswith("belkins-hub-radar-exp-")
                ]
                prev_appearance = NSAppearance.currentAppearance()
                try:
                    for app_name in (NSAppearanceNameDarkAqua, NSAppearanceNameAqua):
                        appearance = NSAppearance.appearanceNamed_(app_name)
                        NSAppearance.setCurrentAppearance_(appearance)
                        for blk in radar_blocks:
                            img = NSImage.alloc().initWithSize_((card_base.W, max(1.0, blk.height)))
                            img.lockFocus()
                            try:
                                blk.paint(card_base.Pen(False), card_base.W, blk.height)
                            finally:
                                img.unlockFocus()
                finally:
                    NSAppearance.setCurrentAppearance_(prev_appearance)
        finally:
            belkins_theme.set_reset_radar_expanded(orig_exp)
            belkins_theme.set_active_tab(orig_tab)
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_game._shop_save_path = orig_path


def test_belkins_party_tabs_codex_credits_and_reset_clocks() -> None:
    with tempfile.TemporaryDirectory() as tmp, _ensure_resets():
        orig_path = belkins_game._shop_save_path
        orig_tab = belkins_theme.get_active_tab()
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            belkins_theme._LIVE_SETTINGS.clear()

            snap = _mixed_fleet_snapshot(usable_credits=1)
            blocks = belkins_theme.build(snap, NOW, ThemeActions())

            # Codex tab: active hero shows emerald `↺ USE 1×` pill
            codex_hero = next(b for b in blocks if b.key == "belkins-codex-active")
            rec_ch = _paint_blocks([codex_hero])
            assert any(p[0] == "↺ USE 1×" and p[1] == "good_text" for p in rec_ch.pills), rec_ch.pills

            # Codex tab: standby with banked credits (usable=0, avail=3) shows aqua `↺ 3×` AND reset clock
            codex_stby = next(b for b in blocks if b.key == "belkins-codex--2")
            rec_cs = _paint_blocks([codex_stby])
            assert any(p[0] == "↺ 3×" and p[1] == "belkins_cmm_aqua" for p in rec_cs.pills), rec_cs.pills
            assert any(t.startswith("↺ ") and ("h" in t or "d" in t or ":" in t) for t in rec_cs.texts), rec_cs.texts

            # Codex tab: capped standby shows `Capped · 5h`
            codex_cap = next(b for b in blocks if b.key == "belkins-codex--3")
            rec_cc = _paint_blocks([codex_cap])
            assert any("Capped · 5h" in t for t in rec_cc.texts), rec_cc.texts

            # Codex active hero with usable=0, avail=3 -> aqua `↺ 3×`
            snap_banked_only = replace(
                snap,
                quota_rows=(
                    replace(snap.quota_rows[0], reset_credits_available=3, reset_credits_usable=0),
                )
                + snap.quota_rows[1:],
            )
            blocks_bo = belkins_theme.build(snap_banked_only, NOW, ThemeActions())
            rec_bo = _paint_blocks([next(b for b in blocks_bo if b.key == "belkins-codex-active")])
            assert any(p[0] == "↺ 3×" and p[1] == "belkins_cmm_aqua" for p in rec_bo.pills), rec_bo.pills

            # Claude tab: capped standby row (#2 dina) shows `Capped · 3h`
            claude_cap = next(b for b in blocks if b.key == "belkins-claude-2")
            rec_cl_cap = _paint_blocks([claude_cap])
            assert any("Capped · 3h" in t for t in rec_cl_cap.texts), rec_cl_cap.texts

            # Claude tab: healthy standby row (#3 ana) shows concise reset countdown (`↺ 20h`)
            claude_stby = next(b for b in blocks if b.key == "belkins-claude-3")
            rec_cl_stby = _paint_blocks([claude_stby])
            assert any("↺ 20h" in t for t in rec_cl_stby.texts), rec_cl_stby.texts
        finally:
            belkins_theme.set_active_tab(orig_tab)
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_game._shop_save_path = orig_path


def test_belkins_settings_tab_radar_toggles() -> None:
    with tempfile.TemporaryDirectory() as tmp, _ensure_resets():
        orig_path = belkins_game._shop_save_path
        orig_tab = belkins_theme.get_active_tab()
        orig_note = belkins_theme._LAST_CFG_NOTE
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_theme._LAST_CFG_NOTE = ""
            belkins_theme.set_active_tab("config")

            saved: list[tuple[str, Any]] = []
            actions = ThemeActions(set_setting=lambda k, v: saved.append((k, v)))
            snap = _mixed_fleet_snapshot()
            blocks = belkins_theme.build(snap, NOW, actions)

            radar_cfg = next(b for b in blocks if b.key == "belkins-cfg-radar-bar")
            assert radar_cfg.keep_open is True
            assert callable(radar_cfg.on_click_x)

            rec_before = _paint_blocks([radar_cfg])
            assert "Unlock: OFF" in rec_before.texts, rec_before.texts
            assert "Reset Bell: ON" in rec_before.texts, rec_before.texts

            # Click left segment (`Unlock: OFF` -> `Unlock: ON`)
            radar_cfg.on_click_x(belkins_theme.X0 + 20.0)
            assert saved[-1] == ("title_show_next_unlock", True), saved
            rec_mid = _paint_blocks([radar_cfg])
            assert "Unlock: ON" in rec_mid.texts, rec_mid.texts

            # Click right segment (`Reset Bell: ON` -> `Reset Bell: OFF`)
            radar_cfg.on_click_x(belkins_theme.X1 - 20.0)
            assert saved[-1] == ("reset_imminent_alert_enabled", False), saved
            rec_after = _paint_blocks([radar_cfg])
            assert "Reset Bell: OFF" in rec_after.texts, rec_after.texts
        finally:
            belkins_theme.set_active_tab(orig_tab)
            belkins_theme._LAST_CFG_NOTE = orig_note
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_game._shop_save_path = orig_path


def test_belkins_graceful_fallback_when_resets_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp, _mask_resets_absent():
        orig_path = belkins_game._shop_save_path
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            belkins_theme._LIVE_SETTINGS.clear()
            snap = _mixed_fleet_snapshot()
            blocks = belkins_theme.build(snap, NOW, ThemeActions())
            assert blocks, "Belkins theme should render cleanly even when resets.py is absent"
            assert not any("-hub-" in b.key and "-radar-" in b.key for b in blocks)
            _paint_blocks(blocks)
        finally:
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_game._shop_save_path = orig_path


# ---------------------------------------------------------------------------
# 4. Notify: Imminent Unlock & Codex Reset-Credit Deduped Alerts
# ---------------------------------------------------------------------------


def test_notify_imminent_unlock_alert_dedup_restart_and_gating() -> None:
    with _ensure_resets():
        # Anchor reset right on an hour boundary so +/- 30s jitter tests bucket stability
        reset_ep = float(int(NOW // 3600) * 3600 + 3600)
        t_minus_20m = reset_ep - 20 * 60
        t_minus_10m = reset_ep - 10 * 60
        t_minus_5m = reset_ep - 5 * 60

        claude_capped = _claude(
            3,
            "dina",
            scoped_windows=(("Fable", 97.0),),
            scoped_resets_at=(("Fable", _iso(reset_ep)),),
        )
        codex_capped = _codex(
            -2,
            "codex_cap",
            seven_day_pct=100.0,
            seven_day_resets_at=_iso(reset_ep),
            soonest_reset_at=reset_ep,
        )
        claude_low = _claude(
            4,
            "low_seat",
            scoped_windows=(("Fable", 80.0),),
            scoped_resets_at=(("Fable", _iso(reset_ep)),),
        )
        claude_disabled = _claude(
            5,
            "paused_seat",
            scoped_windows=(("Fable", 100.0),),
            scoped_resets_at=(("Fable", _iso(reset_ep)),),
            disabled=True,
        )
        claude_noted = _claude(
            6,
            "noted_seat",
            scoped_windows=(("Fable", 100.0),),
            scoped_resets_at=(("Fable", _iso(reset_ep)),),
        )

        snap = app_mod.UiSnapshot(
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "notifications_enabled": True, "reset_imminent_alert_enabled": True}
            ),
            accounts=(claude_capped, claude_low, claude_disabled, claude_noted),
            quota_rows=(codex_capped,),
            account_notes={6: "re-login needed"},
            account_note_kinds={6: "re-login needed"},
        )

        # At T-20m (> 15m): no unlock_soon events
        ev_20 = [e for e in notify_mod.detect(None, snap, now=t_minus_20m) if e.kind == "unlock_soon"]
        assert ev_20 == [], ev_20

        # At T-10m (<= 15m): fires ONCE each for dina (Fable) and codex_cap (7d), skipping low/disabled/noted
        ev_10 = [e for e in notify_mod.detect(None, snap, now=t_minus_10m) if e.kind == "unlock_soon"]
        assert len(ev_10) == 2, ev_10
        by_key = {e.key: e for e in ev_10}
        claude_ev = next(e for k, e in by_key.items() if k.startswith("unlock_soon:claude:3:Fable:"))
        codex_ev = next(e for k, e in by_key.items() if k.startswith("unlock_soon:codex:-2:7d:"))
        assert claude_ev.message == "🔓 dina (Fable) unlocks in 10m — fresh capacity incoming", claude_ev.message
        assert codex_ev.message == "🔓 codex_cap (7d) unlocks in 10m — fresh capacity incoming", codex_ev.message

        # Disabled via reset_imminent_alert_enabled=False -> silent
        snap_off = replace(
            snap,
            settings={**snap.settings, "reset_imminent_alert_enabled": False},
        )
        assert [e for e in notify_mod.detect(None, snap_off, now=t_minus_10m) if e.kind == "unlock_soon"] == []

        # Stateful ledger: dedup across ticks, API +/-30s jitter across hour boundary, and restarts!
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "notify_state.json"
            clock = [t_minus_10m]
            sent: list[notify_mod.Event] = []
            cfg = dict(snap.settings)

            def _mk(settings_map: dict[str, Any]) -> notify_mod.Notifier:
                return notify_mod.Notifier(
                    settings=lambda: settings_map,
                    ledger_path=ledger,
                    credentials_path=Path(tmp) / "notify.json",
                    mac_sender=SimpleNamespace(send=lambda e: (sent.append(e), True)[1]),
                    clock=lambda: clock[0],
                    dispatch=lambda work: work(),
                )

            # Master notifications_enabled=False -> fires nothing and writes no ledger
            n_muted = _mk({**cfg, "notifications_enabled": False})
            assert n_muted.notify(None, snap) == []
            assert not ledger.exists()

            # Tick 1 at T-10m -> fires both imminent unlocks once
            n1 = _mk(cfg)
            fired1 = [e for e in n1.notify(None, snap) if e.kind == "unlock_soon"]
            assert len(fired1) == 2, fired1

            # Tick 2 at T-5m with reset_ep jittered -25s across boundary -> SAME bucket -> 0 new sends!
            clock[0] = t_minus_5m
            snap_jitter = replace(
                snap,
                accounts=(
                    replace(claude_capped, scoped_resets_at=(("Fable", _iso(reset_ep - 25.0)),)),
                ),
                quota_rows=(
                    replace(
                        codex_capped,
                        seven_day_resets_at=_iso(reset_ep + 25.0),
                        soonest_reset_at=reset_ep + 25.0,
                    ),
                ),
            )
            assert [e for e in n1.notify(snap, snap_jitter) if e.kind == "unlock_soon"] == []

            # Restart with previous=None -> still suppressed by persisted ledger!
            n2 = _mk(cfg)
            assert [e for e in n2.notify(None, snap_jitter) if e.kind == "unlock_soon"] == []

            # Next reset cycle (+7 days) -> new bucket -> fires ONCE again!
            next_reset = reset_ep + 7 * 86400
            clock[0] = next_reset - 8 * 60
            snap_next = replace(
                snap,
                accounts=(
                    replace(claude_capped, scoped_resets_at=(("Fable", _iso(next_reset)),)),
                ),
                quota_rows=(
                    replace(
                        codex_capped,
                        seven_day_resets_at=_iso(next_reset),
                        soonest_reset_at=next_reset,
                    ),
                ),
            )
            fired_next = [e for e in n2.notify(snap_jitter, snap_next) if e.kind == "unlock_soon"]
            assert len(fired_next) == 2, fired_next


def test_notify_codex_reset_credit_alert_dedup_restart_and_gating() -> None:
    with _ensure_resets():
        reset_ep = float(int(NOW // 3600) * 3600 + 48 * 3600)
        cfg_on = normalize_settings(
            {**SETTINGS_DEFAULTS, "notifications_enabled": True, "reset_imminent_alert_enabled": True}
        )

        low_row = _codex(
            -1,
            "belkins_work",
            seven_day_pct=85.0,
            seven_day_resets_at=_iso(reset_ep),
            soonest_reset_at=reset_ep,
            reset_credits_available=2,
            reset_credits_usable=1,
        )
        crossed_row = replace(low_row, seven_day_pct=92.0)
        higher_row = replace(
            low_row,
            seven_day_pct=98.0,
            seven_day_resets_at=_iso(reset_ep + 30.0),
            soonest_reset_at=reset_ep + 30.0,
        )
        plural_row = _codex(
            -2,
            "belkins_ops",
            seven_day_pct=95.0,
            seven_day_resets_at=_iso(reset_ep),
            soonest_reset_at=reset_ep,
            reset_credits_available=3,
            reset_credits_usable=2,
        )
        no_usable_row = _codex(
            -3,
            "no_usable",
            seven_day_pct=96.0,
            seven_day_resets_at=_iso(reset_ep),
            soonest_reset_at=reset_ep,
            reset_credits_available=3,
            reset_credits_usable=0,
        )

        prev_snap = app_mod.UiSnapshot(settings=cfg_on, quota_rows=(low_row, no_usable_row))
        curr_snap = app_mod.UiSnapshot(settings=cfg_on, quota_rows=(crossed_row, plural_row, no_usable_row))
        higher_snap = app_mod.UiSnapshot(settings=cfg_on, quota_rows=(higher_row, plural_row, no_usable_row))

        # Below 90% -> does NOT fire reset_credit
        assert [e for e in notify_mod.detect(None, prev_snap, now=NOW) if e.kind == "reset_credit"] == []

        # Crossing >= 90% with usable > 0 -> fires exact messages (singular & plural)
        evs = [e for e in notify_mod.detect(prev_snap, curr_snap, now=NOW) if e.kind == "reset_credit"]
        assert len(evs) == 2, evs
        ev1 = next(e for e in evs if e.key.startswith("reset_credit:-1:"))
        ev2 = next(e for e in evs if e.key.startswith("reset_credit:-2:"))
        assert ev1.message == "⚡ belkins_work (Codex 92%) has 1 usable reset credit available", ev1.message
        assert ev2.message == "⚡ belkins_ops (Codex 95%) has 2 usable reset credits available", ev2.message

        # Disabled via reset_imminent_alert_enabled=False -> silent
        snap_off = replace(curr_snap, settings={**cfg_on, "reset_imminent_alert_enabled": False})
        assert [e for e in notify_mod.detect(prev_snap, snap_off, now=NOW) if e.kind == "reset_credit"] == []

        # Stateful ledger: survives restarts, never fires twice for same reset window, re-fires next cycle
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "notify_state.json"
            clock = [NOW]
            sent: list[notify_mod.Event] = []

            def _mk(settings_map: dict[str, Any]) -> notify_mod.Notifier:
                return notify_mod.Notifier(
                    settings=lambda: settings_map,
                    ledger_path=ledger,
                    credentials_path=Path(tmp) / "notify.json",
                    mac_sender=SimpleNamespace(send=lambda e: (sent.append(e), True)[1]),
                    clock=lambda: clock[0],
                    dispatch=lambda work: work(),
                )

            n1 = _mk(cfg_on)
            fired1 = [e for e in n1.notify(prev_snap, curr_snap) if e.kind == "reset_credit"]
            assert len(fired1) == 2, fired1

            # 92% -> 98% with +30s reset jitter in same reset window -> 0 new sends!
            clock[0] += 120.0
            assert [e for e in n1.notify(curr_snap, higher_snap) if e.kind == "reset_credit"] == []

            # Restart with previous=None -> suppressed by persisted ledger!
            n2 = _mk(cfg_on)
            assert [e for e in n2.notify(None, higher_snap) if e.kind == "reset_credit"] == []

            # Next reset window (+7 days) -> new reset_bucket -> fires ONCE again!
            next_reset = reset_ep + 7 * 86400
            clock[0] = next_reset - 12 * 3600
            next_row = replace(
                crossed_row,
                seven_day_resets_at=_iso(next_reset),
                soonest_reset_at=next_reset,
            )
            next_snap = app_mod.UiSnapshot(settings=cfg_on, quota_rows=(next_row,))
            fired_next = [e for e in n2.notify(higher_snap, next_snap) if e.kind == "reset_credit"]
            assert len(fired_next) == 1 and fired_next[0].key.startswith("reset_credit:-1:"), fired_next

            # Master notifications_enabled=False -> fires nothing
            n_off = _mk({**cfg_on, "notifications_enabled": False})
            assert n_off.notify(None, next_snap) == []


# ---------------------------------------------------------------------------
# Runner
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
    mode = "real resets.py" if _REAL_RESETS is not None else "in-test resets stub"
    print(f"[test_resets_ui_alerts] mode={mode}")
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
