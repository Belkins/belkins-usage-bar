"""Comprehensive standalone test suite for ``cc_usage_widget.resets``.

Covers:
- Public API contracts, frozen slotted dataclasses & strict JSON serializability
- ``parse_reset_epoch`` across epoch int/float/str, datetime, ISO-8601 (Z / offset /
  naive), ``HH:MM`` today vs tomorrow with slack, ``Tom HH:MM``, ``Mon HH:MM``,
  ``Nov 27 13:00``, Dec->Jan forward rollover, Jan->Dec past detection, Feb 29
  leap-year rollover, and invalid/overdue inputs
- ``format_countdown`` and ``format_local_reset_label`` across every boundary
- Same-minute deduplication on the same account (``Fable`` vs ``7d``, higher-pct,
  tie-breaks, implicit Fable reset inheritance, multi-account preservation)
- Expired/past reset filtering, disabled accounts, and re-login vs offline states
- Unified Claude + Codex chronological sorting & duck-typed ``QuotaRow`` / ``ExtraLimit``
- Fable Guard capacity exclusion (``fable_guard=True`` vs ``False`` and non-Fable fleets)
- Codex banked reset credits (live vs relogin vs disabled vs usable vs unknown/zero)
- Perishable 48h detection & deduplicated seat-equivalent calculation
- All 5 Smart Fleet Advisor recommendation tiers, priority order, 72-char cap & empty fleet
- Privacy guarantees (zero raw emails or tokens leaked anywhere in radar output)

Run directly (works in BOTH claude-swap venv and bare ``python3``)::

    CC_USAGE_WIDGET_NO_REVEAL=1 python3 tests/test_resets_radar.py
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget.contracts import (  # noqa: E402
    CODEX_PSEUDO_ACCOUNT_SLOT,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    AccountRow,
)
from cc_usage_widget.resets import (  # noqa: E402
    FleetCapacitySummary,
    FleetRecommendation,
    FleetResetRadar,
    ResetCreditAccount,
    ResetCreditsSummary,
    ResetEvent,
    build_fleet_reset_radar,
    format_countdown,
    format_local_reset_label,
    parse_reset_epoch,
)

# Fixed Saturday Nov 21, 2026 10:00:00 local time — completely deterministic
BASE_DT = dt.datetime(2026, 11, 21, 10, 0, 0)
BASE_NOW = BASE_DT.timestamp()


def _local_epoch(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> float:
    return dt.datetime(y, m, d, hh, mm, ss).timestamp()


def _claude_row(
    slot: int,
    alias: str,
    *,
    email: str = "user@example.test",
    is_active: bool = False,
    five_hour_pct: float | None = 10.0,
    seven_day_pct: float | None = 20.0,
    scoped_windows: tuple[tuple[str, float], ...] = (),
    five_hour_resets_at: str | None = None,
    seven_day_resets_at: str | None = None,
    scoped_resets_at: tuple[tuple[str, str], ...] = (),
    plan_type: str | None = "claude_max_20x",
    disabled: bool = False,
    switchable: bool = True,
    expired_windows: tuple[str, ...] = (),
    attention_note: str = "",
    attention_kind: str = "",
) -> AccountRow:
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
        vendor=VENDOR_CLAUDE,
        switchable=switchable,
        disabled=disabled,
        plan_type=plan_type,
        expired_windows=expired_windows,
        attention_note=attention_note,
        attention_kind=attention_kind,
    )


def _codex_row(
    slot: int,
    alias: str,
    *,
    email: str = "codex@example.test",
    is_active: bool = False,
    five_hour_pct: float | None = None,
    seven_day_pct: float | None = 25.0,
    scoped_windows: tuple[tuple[str, float], ...] = (),
    five_hour_resets_at: str | None = None,
    seven_day_resets_at: str | None = None,
    scoped_resets_at: tuple[tuple[str, str], ...] = (),
    plan_type: str | None = "pro",
    disabled: bool = False,
    expired_windows: tuple[str, ...] = (),
    attention_note: str = "",
    attention_kind: str = "",
    soonest_reset_at: float | None = None,
    reset_credits_available: int | None = None,
    reset_credits_usable: int | None = None,
    info_notes: tuple[str, ...] = (),
) -> AccountRow:
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
        vendor=VENDOR_CODEX,
        switchable=False,
        disabled=disabled,
        plan_type=plan_type,
        expired_windows=expired_windows,
        attention_note=attention_note,
        attention_kind=attention_kind,
        soonest_reset_at=soonest_reset_at,
        reset_credits_available=reset_credits_available,
        reset_credits_usable=reset_credits_usable,
        info_notes=info_notes,
    )


@dataclass
class _DuckExtraLimit:
    name: str
    used_pct: float | None
    resets_at: Any
    expired: bool = False


@dataclass
class _DuckQuotaRow:
    slot: int
    alias: str
    email: str = "duck@example.test"
    vendor: str = VENDOR_CODEX
    is_active: bool = False
    disabled: bool = False
    is_live: bool = True
    plan_type: str | None = "self_serve_business_prolite"
    five_hour_pct: float | None = None
    five_hour_resets_at: Any = None
    seven_day_pct: float | None = None
    seven_day_resets_at: Any = None
    extra_limits: tuple[Any, ...] = ()
    reset_credits_available: int | None = None
    reset_credits_usable: int | None = None
    attention_note: str = ""
    attention_kind: str = ""
    expired_windows: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 1. Public API contract & dataclass invariants
# ---------------------------------------------------------------------------


def test_public_dataclasses_contract_fields_and_frozen_slots() -> None:
    expected = {
        ResetEvent: (
            "vendor",
            "slot",
            "alias",
            "window",
            "used_pct",
            "free_pct",
            "resets_at_epoch",
            "seconds_until",
            "reset_label",
            "countdown_label",
            "kind",
            "is_active",
            "disabled",
            "plan_label",
            "no_fable",
        ),
        ResetCreditAccount: (
            "vendor",
            "slot",
            "alias",
            "available",
            "usable",
            "used_pct",
            "is_active",
            "needs_relogin",
        ),
        ResetCreditsSummary: (
            "total_available",
            "live_available",
            "total_usable",
            "accounts",
        ),
        FleetCapacitySummary: (
            "claude_fable_free_seats",
            "claude_fable_total_seats",
            "claude_locked_relogin_seats",
            "claude_next_unlock",
            "codex_free_seats",
            "codex_total_seats",
            "codex_locked_relogin_seats",
            "codex_next_unlock",
            "perishable_seats_48h",
        ),
        FleetRecommendation: (
            "kind",
            "headline",
            "detail",
            "target_vendor",
            "target_slot",
        ),
        FleetResetRadar: (
            "generated_at",
            "events",
            "unlocks",
            "perishable",
            "credits",
            "capacity",
            "recommendation",
        ),
    }
    for cls, names in expected.items():
        actual = tuple(f.name for f in dataclasses.fields(cls))
        assert actual == names, f"{cls.__name__} fields mismatch: {actual} != {names}"
        assert hasattr(cls, "__slots__"), f"{cls.__name__} must use slots=True"
        assert cls.__dataclass_params__.frozen, f"{cls.__name__} must be frozen=True"


def test_radar_is_strictly_json_serializable() -> None:
    c1 = _claude_row(
        1,
        "main",
        is_active=True,
        seven_day_pct=40.0,
        seven_day_resets_at="13:00",
        scoped_windows=(("Fable", 95.0),),
        scoped_resets_at=(("Fable", "13:00"),),
    )
    x1 = _codex_row(
        -1,
        "codex-work",
        seven_day_pct=88.0,
        seven_day_resets_at="Nov 23 14:00",
        reset_credits_available=2,
        reset_credits_usable=1,
    )
    radar = build_fleet_reset_radar((c1,), (x1,), now=BASE_NOW)
    payload = json.dumps(dataclasses.asdict(radar), allow_nan=False)
    decoded = json.loads(payload)
    assert decoded["generated_at"] == BASE_NOW
    assert len(decoded["events"]) == 2
    assert decoded["credits"]["total_usable"] == 1


# ---------------------------------------------------------------------------
# 2. parse_reset_epoch — every supported format & edge case
# ---------------------------------------------------------------------------


def test_parse_reset_epoch_numeric_and_datetime() -> None:
    assert parse_reset_epoch(1763719200, now=BASE_NOW) == 1763719200.0
    assert parse_reset_epoch(1763719200.5, now=BASE_NOW) == 1763719200.5
    assert parse_reset_epoch("1763719200", now=BASE_NOW) == 1763719200.0
    assert parse_reset_epoch("1763719200.25", now=BASE_NOW) == 1763719200.25

    utc_dt = dt.datetime(2026, 11, 27, 13, 0, 0, tzinfo=dt.timezone.utc)
    assert parse_reset_epoch(utc_dt, now=BASE_NOW) == utc_dt.timestamp()


def test_parse_reset_epoch_iso8601() -> None:
    expected = dt.datetime(2026, 11, 27, 13, 0, 0, tzinfo=dt.timezone.utc).timestamp()
    assert parse_reset_epoch("2026-11-27T13:00:00Z", now=BASE_NOW) == expected
    assert parse_reset_epoch("2026-11-27T13:00:00+00:00", now=BASE_NOW) == expected
    assert parse_reset_epoch("2026-11-27T15:00:00+02:00", now=BASE_NOW) == expected
    assert parse_reset_epoch("2026-11-27T13:00:00", now=BASE_NOW) == expected
    assert parse_reset_epoch("2026-11-27 13:00:00Z", now=BASE_NOW) == expected
    frac = parse_reset_epoch("2026-11-27T13:00:00.500000Z", now=BASE_NOW)
    assert frac == expected + 0.5


def test_parse_reset_epoch_hhmm_today_tomorrow_and_slack() -> None:
    # Future clock today (now = 10:00, input = "13:00") -> today 13:00
    assert parse_reset_epoch("13:00", now=BASE_NOW) == _local_epoch(2026, 11, 21, 13, 0)

    # Exact now (10:00 at 10:00:00) -> today 10:00
    assert parse_reset_epoch("10:00", now=BASE_NOW) == _local_epoch(2026, 11, 21, 10, 0)

    # Just passed within 30s slack (10:00 at 10:00:20) -> stays today 10:00 (NOT jumped +24h!)
    assert parse_reset_epoch("10:00", now=BASE_NOW + 20.0) == _local_epoch(2026, 11, 21, 10, 0)

    # Already passed beyond slack (09:00 at 10:00) -> rolls to tomorrow 09:00
    assert parse_reset_epoch("09:00", now=BASE_NOW) == _local_epoch(2026, 11, 22, 9, 0)

    # Late-night rollover (now = 23:00, input = "01:30") -> tomorrow 01:30
    late = _local_epoch(2026, 11, 21, 23, 0)
    assert parse_reset_epoch("01:30", now=late) == _local_epoch(2026, 11, 22, 1, 30)


def test_parse_reset_epoch_tomorrow_and_weekday_clocks() -> None:
    # BASE_DT is Saturday Nov 21, 2026 10:00
    assert parse_reset_epoch("Tom 13:00", now=BASE_NOW) == _local_epoch(2026, 11, 22, 13, 0)
    assert parse_reset_epoch("Tomorrow 08:15", now=BASE_NOW) == _local_epoch(2026, 11, 22, 8, 15)

    # Mon 20:00 -> Monday Nov 23, 2026 20:00
    assert parse_reset_epoch("Mon 20:00", now=BASE_NOW) == _local_epoch(2026, 11, 23, 20, 0)
    assert parse_reset_epoch("Monday 20:00", now=BASE_NOW) == _local_epoch(2026, 11, 23, 20, 0)

    # Same weekday future -> today; same weekday past -> next week (+7d)
    assert parse_reset_epoch("Sat 14:30", now=BASE_NOW) == _local_epoch(2026, 11, 21, 14, 30)
    assert parse_reset_epoch("Sat 08:00", now=BASE_NOW) == _local_epoch(2026, 11, 28, 8, 0)


def test_parse_reset_epoch_month_day_and_year_rollover() -> None:
    assert parse_reset_epoch("Nov 27 13:00", now=BASE_NOW) == _local_epoch(2026, 11, 27, 13, 0)
    assert parse_reset_epoch("Nov 27, 13:00", now=BASE_NOW) == _local_epoch(2026, 11, 27, 13, 0)
    assert parse_reset_epoch("November 27 13:00", now=BASE_NOW) == _local_epoch(2026, 11, 27, 13, 0)

    oct_now = _local_epoch(2026, 10, 20, 10, 0)
    assert parse_reset_epoch("Oct 24 08:10", now=oct_now) == _local_epoch(2026, 10, 24, 8, 10)

    # Dec -> Jan forward year rollover (no crash, resolves into 2027)
    dec_now = _local_epoch(2026, 12, 29, 10, 0)
    assert parse_reset_epoch("Jan 3 09:00", now=dec_now) == _local_epoch(2027, 1, 3, 9, 0)

    # Jan -> Dec past timestamp resolves to Dec 2026 (past), NOT Dec 2027 (+362d!)
    jan_now = _local_epoch(2027, 1, 2, 10, 0)
    parsed_past = parse_reset_epoch("Dec 30 09:00", now=jan_now)
    assert parsed_past == _local_epoch(2026, 12, 30, 9, 0)
    assert parsed_past < jan_now

    # Feb 29 leap-year rollover from late Dec 2027 -> Feb 29, 2028 (never throws!)
    dec_2027 = _local_epoch(2027, 12, 28, 10, 0)
    assert parse_reset_epoch("Feb 29 13:00", now=dec_2027) == _local_epoch(2028, 2, 29, 13, 0)

    # Feb 29 when no adjacent year is leap -> cleanly returns None
    mid_2026 = _local_epoch(2026, 6, 15, 10, 0)
    assert parse_reset_epoch("Feb 29 13:00", now=mid_2026) is None


def test_parse_reset_epoch_rejects_invalid_and_overdue() -> None:
    bad_inputs: list[Any] = [
        None,
        "",
        "   ",
        "—",
        "never",
        "garbage text",
        "overdue (13:00)",
        "overdue (Nov 21 09:00)",
        "25:00",
        "12:60",
        "Feb 30 13:00",
        "Apr 31 12:00",
        "Foo 12 13:00",
        True,
        False,
        0,
        -100,
        float("nan"),
        float("inf"),
        float("-inf"),
        1e25,
        ["13:00"],
        {"reset": "13:00"},
    ]
    for bad in bad_inputs:
        assert parse_reset_epoch(bad, now=BASE_NOW) is None, f"expected None for {bad!r}"


# ---------------------------------------------------------------------------
# 3. format_countdown & format_local_reset_label
# ---------------------------------------------------------------------------


def test_format_countdown_all_ranges() -> None:
    assert format_countdown(-5.0) == "now"
    assert format_countdown(0.0) == "now"
    assert format_countdown(float("nan")) == "now"
    assert format_countdown(0.5) == "<1m"
    assert format_countdown(59.9) == "<1m"
    assert format_countdown(60.0) == "1m"
    assert format_countdown(42 * 60) == "42m"
    assert format_countdown(3599.0) == "59m"
    assert format_countdown(3600.0) == "1h"
    assert format_countdown(2 * 3600 + 54 * 60) == "2h 54m"
    assert format_countdown(23 * 3600 + 59 * 60) == "23h 59m"
    assert format_countdown(86400.0) == "1d"
    assert format_countdown(2 * 86400 + 10 * 3600) == "2d 10h"


def test_format_local_reset_label_all_ranges_and_roundtrip() -> None:
    # Today (Sat Nov 21 13:00)
    today_ep = _local_epoch(2026, 11, 21, 13, 0)
    assert format_local_reset_label(today_ep, now=BASE_NOW) == "13:00"
    assert parse_reset_epoch(format_local_reset_label(today_ep, now=BASE_NOW), now=BASE_NOW) == today_ep

    # Tomorrow (Sun Nov 22 13:00)
    tom_ep = _local_epoch(2026, 11, 22, 13, 0)
    assert format_local_reset_label(tom_ep, now=BASE_NOW) == "Tom 13:00"
    assert parse_reset_epoch(format_local_reset_label(tom_ep, now=BASE_NOW), now=BASE_NOW) == tom_ep

    # Within 6 days (Mon Nov 23 20:00 -> days=2; Fri Nov 27 13:00 -> days=6)
    mon_ep = _local_epoch(2026, 11, 23, 20, 0)
    assert format_local_reset_label(mon_ep, now=BASE_NOW) == "Mon 20:00"
    assert parse_reset_epoch(format_local_reset_label(mon_ep, now=BASE_NOW), now=BASE_NOW) == mon_ep

    fri_ep = _local_epoch(2026, 11, 27, 13, 0)
    assert format_local_reset_label(fri_ep, now=BASE_NOW) == "Fri 13:00"

    # 7+ days out (Sat Nov 28 13:00 -> days=7)
    next_sat = _local_epoch(2026, 11, 28, 13, 0)
    assert format_local_reset_label(next_sat, now=BASE_NOW) == "Nov 28 13:00"
    assert parse_reset_epoch(format_local_reset_label(next_sat, now=BASE_NOW), now=BASE_NOW) == next_sat

    # Invalid / NaN returns ""
    assert format_local_reset_label(float("nan"), now=BASE_NOW) == ""


# ---------------------------------------------------------------------------
# 4. Same-minute deduplication
# ---------------------------------------------------------------------------


def test_same_minute_dedup_prefers_fable_and_higher_pct() -> None:
    # Slot #3: BOTH 7d (45%) and Fable (100%) reset at exact same minute 13:00
    slot3 = _claude_row(
        3,
        "belkins-3",
        five_hour_pct=15.0,
        five_hour_resets_at="11:30",  # different minute -> kept!
        seven_day_pct=45.0,
        seven_day_resets_at="13:00",
        scoped_windows=(("Fable", 100.0),),
        scoped_resets_at=(("Fable", "13:00"),),
    )
    # Slot #4: tie on pct (50% vs 50%) at same minute -> Fable wins over 7d
    slot4 = _claude_row(
        4,
        "belkins-4",
        seven_day_pct=50.0,
        seven_day_resets_at="13:00",
        scoped_windows=(("Fable", 50.0),),
        scoped_resets_at=(("Fable", "13:00"),),
    )
    # Slot #5: higher 7d (95% unlock) vs lower Fable (20%) at same minute -> 7d wins!
    slot5 = _claude_row(
        5,
        "belkins-5",
        seven_day_pct=95.0,
        seven_day_resets_at="14:00",
        scoped_windows=(("Fable", 20.0),),
        scoped_resets_at=(("Fable", "14:00"),),
    )
    # Slot #6: Fable 100% with omitted scoped_resets_at inherits seven_day_resets_at cleanly
    slot6 = _claude_row(
        6,
        "belkins-6",
        seven_day_pct=30.0,
        seven_day_resets_at="15:00",
        scoped_windows=(("Fable", 100.0),),
        scoped_resets_at=(),
    )

    radar = build_fleet_reset_radar((slot3, slot4, slot5, slot6), (), now=BASE_NOW)

    # Slot #3 should have 2 events total: 5h at 11:30, and ONE deduplicated Fable event at 13:00
    s3_events = [e for e in radar.events if e.slot == 3]
    assert len(s3_events) == 2
    assert s3_events[0].window == "5h"
    assert s3_events[1].window == "Fable"
    assert s3_events[1].used_pct == 100.0
    assert s3_events[1].kind == "unlock"

    # Both slot #3 and slot #4 reset at 13:00 -> DIFFERENT accounts both preserved!
    at_1300 = [e for e in radar.events if e.reset_label == "13:00"]
    assert [(e.slot, e.window) for e in at_1300] == [(3, "Fable"), (4, "Fable")]

    # Slot #5 at 14:00 -> 7d (95% unlock) beats Fable (20%)
    s5_events = [e for e in radar.events if e.slot == 5]
    assert len(s5_events) == 1
    assert s5_events[0].window == "7d"
    assert s5_events[0].kind == "unlock"

    # Slot #6 at 15:00 -> Fable (100% unlock) inherited seven_day_resets_at and deduped 7d
    s6_events = [e for e in radar.events if e.slot == 6]
    assert len(s6_events) == 1
    assert s6_events[0].window == "Fable"
    assert s6_events[0].kind == "unlock"


# ---------------------------------------------------------------------------
# 5. Unified Claude + Codex sorting, extra_limits, expired/past/disabled/relogin
# ---------------------------------------------------------------------------


def test_unified_claude_codex_sorting_and_extra_limits() -> None:
    c1 = _claude_row(
        1,
        "claude-a",
        five_hour_pct=92.0,
        five_hour_resets_at="11:00",  # +1h
        seven_day_pct=15.0,
        seven_day_resets_at="Nov 23 10:00",  # +48h
        scoped_windows=(("Fable", 15.0),),
        scoped_resets_at=(("Fable", "Nov 23 10:00"),),
    )
    x1 = _codex_row(
        -1,
        "codex-pro",
        five_hour_pct=30.0,
        five_hour_resets_at="12:30",  # +2h 30m
        seven_day_pct=90.0,
        seven_day_resets_at="Nov 22 08:00",  # +22h
        scoped_windows=(("Spark", 88.0),),
        scoped_resets_at=(("Spark", "14:00"),),  # +4h
    )
    x2 = _DuckQuotaRow(
        slot=-2,
        alias="codex-biz",
        seven_day_pct=20.0,
        seven_day_resets_at=BASE_NOW + 10 * 3600,  # +10h
        extra_limits=(
            _DuckExtraLimit(name="Astra", used_pct=96.0, resets_at=BASE_NOW + 3 * 3600),  # +3h
            {"name": "Reserve", "used_pct": 10.0, "resets_at": BASE_NOW + 30 * 3600},  # +30h
        ),
    )

    radar = build_fleet_reset_radar((c1,), (x1, x2), now=BASE_NOW)
    epochs = [e.resets_at_epoch for e in radar.events]
    assert epochs == sorted(epochs), "events must be strictly chronological"

    order = [(e.vendor, e.slot, e.window) for e in radar.events]
    assert order == [
        ("claude", 1, "5h"),         # +1h
        ("codex", -1, "5h"),         # +2.5h
        ("codex", -2, "Astra"),      # +3h
        ("codex", -1, "Spark"),      # +4h
        ("codex", -2, "7d"),         # +10h
        ("codex", -1, "7d"),         # +22h
        ("codex", -2, "Reserve"),    # +30h
        ("claude", 1, "Fable"),      # +48h
    ], order

    assert radar.events[2].plan_label == "Business"
    assert radar.capacity.claude_next_unlock is not None
    assert radar.capacity.claude_next_unlock.window == "5h"
    assert radar.capacity.codex_next_unlock is not None
    assert (radar.capacity.codex_next_unlock.slot, radar.capacity.codex_next_unlock.window) == (
        -2,
        "Astra",
    )


def test_expired_past_disabled_and_relogin_filtering() -> None:
    past_iso = dt.datetime.fromtimestamp(BASE_NOW - 120, tz=dt.timezone.utc).isoformat()
    within_slack_iso = dt.datetime.fromtimestamp(BASE_NOW - 15, tz=dt.timezone.utc).isoformat()
    future_iso = dt.datetime.fromtimestamp(BASE_NOW + 7200, tz=dt.timezone.utc).isoformat()

    # Past (>30s ago) skipped; within 30s slack kept with seconds_until == 0.0
    c_timing = _claude_row(
        1,
        "timing",
        five_hour_pct=95.0,
        five_hour_resets_at=past_iso,
        seven_day_pct=90.0,
        seven_day_resets_at=within_slack_iso,
        scoped_windows=(("Fable", 90.0),),
        scoped_resets_at=(("Fable", within_slack_iso),),
    )
    # Explicitly expired_windows skipped even with future string
    c_expired = _claude_row(
        2,
        "exp-win",
        seven_day_pct=100.0,
        seven_day_resets_at=future_iso,
        scoped_windows=(("Fable", 100.0),),
        scoped_resets_at=(("Fable", future_iso),),
        expired_windows=("seven_day", "Fable"),
    )
    # Disabled Claude account: visible as disabled upcoming in events, excluded from unlocks/capacity
    c_disabled = _claude_row(
        3,
        "paused",
        disabled=True,
        seven_day_pct=100.0,
        seven_day_resets_at=future_iso,
        scoped_windows=(("Fable", 100.0),),
        scoped_resets_at=(("Fable", future_iso),),
    )
    # Relogin-needed Claude account via account_notes
    c_relogin = _claude_row(
        4,
        "broken-claude",
        seven_day_pct=100.0,
        seven_day_resets_at=future_iso,
        scoped_windows=(("Fable", 100.0),),
        scoped_resets_at=(("Fable", future_iso),),
    )
    # Codex: relogin-locked, offline transient, info relogin countdown (live!), and pseudo slot 0
    x_relogin = _codex_row(
        -1,
        "dead-codex",
        seven_day_pct=None,
        attention_note="relogin",
        attention_kind="warn",
        reset_credits_available=2,
        reset_credits_usable=1,
    )
    x_offline = _codex_row(
        -2,
        "offline-codex",
        seven_day_pct=None,
        attention_note="offline",
        attention_kind="warn",
    )
    x_countdown = _codex_row(
        -3,
        "soon-exp-token",
        seven_day_pct=40.0,
        seven_day_resets_at=future_iso,
        attention_note="relogin in 1d 4h",
        attention_kind="info",
    )
    x_pseudo = _codex_row(
        CODEX_PSEUDO_ACCOUNT_SLOT,
        "Codex",
        seven_day_pct=95.0,
        seven_day_resets_at=future_iso,
    )

    radar = build_fleet_reset_radar(
        (c_timing, c_expired, c_disabled, c_relogin),
        (x_relogin, x_offline, x_countdown, x_pseudo),
        account_notes={4: "⚠ relogin needed"},
        now=BASE_NOW,
    )

    # Past 5h skipped; within-slack Fable kept at seconds_until == 0.0
    t_events = [e for e in radar.events if e.vendor == "claude" and e.slot == 1]
    assert len(t_events) == 1
    assert t_events[0].window == "Fable"
    assert t_events[0].seconds_until == 0.0
    assert t_events[0].countdown_label == "now"

    # Expired-window row emits no events; its rolled window counts as free capacity (1.0)
    assert not any(e.vendor == "claude" and e.slot == 2 for e in radar.events)

    # Disabled row appears in events with disabled=True, kind="upcoming", NOT in unlocks
    d_events = [e for e in radar.events if e.vendor == "claude" and e.slot == 3]
    assert len(d_events) == 1 and d_events[0].disabled is True and d_events[0].kind == "upcoming"
    assert not any(e.slot == 3 for e in radar.unlocks)

    # Relogin Claude #4 emits no events and counts in claude_locked_relogin_seats
    assert not any(e.vendor == "claude" and e.slot == 4 for e in radar.events)
    assert radar.capacity.claude_locked_relogin_seats == 1

    # Codex: x_relogin counts in codex_locked_relogin_seats (offline does NOT!),
    # x_countdown is live (0.60 free of 1 total), pseudo slot 0 excluded from live capacity/unlocks
    assert radar.capacity.codex_locked_relogin_seats == 1
    assert radar.capacity.codex_total_seats == 1
    assert radar.capacity.codex_free_seats == 0.6
    assert not any(e.vendor == "codex" and e.slot == 0 for e in radar.unlocks)

    # Relogin Codex credits counted in total_available, NOT live_available or total_usable
    assert radar.credits.total_available == 2
    assert radar.credits.live_available == 0
    assert radar.credits.total_usable == 0
    assert len(radar.credits.accounts) == 1
    assert radar.credits.accounts[0].needs_relogin is True
    assert radar.credits.accounts[0].usable == 0


# ---------------------------------------------------------------------------
# 6. Fable Guard capacity exclusion
# ---------------------------------------------------------------------------


def test_fable_guard_excludes_non_fable_seats_from_capacity_and_unlocks() -> None:
    fable_ok = _claude_row(
        1,
        "max-1",
        plan_type="claude_max_20x",
        seven_day_pct=20.0,
        seven_day_resets_at="Nov 26 10:00",
        scoped_windows=(("Fable", 30.0),),
        scoped_resets_at=(("Fable", "Nov 26 10:00"),),
    )
    fable_capped = _claude_row(
        2,
        "max-2",
        plan_type="claude_max_20x",
        seven_day_pct=50.0,
        seven_day_resets_at="15:00",
        scoped_windows=(("Fable", 100.0),),
        scoped_resets_at=(("Fable", "15:00"),),
    )
    team_capped = _claude_row(
        3,
        "team-seat",
        plan_type="claude_team",
        seven_day_pct=98.0,
        seven_day_resets_at="11:00",  # sooner than max-2, but non-Fable!
        scoped_windows=(),
    )
    pro_perishable = _claude_row(
        4,
        "pro-seat",
        plan_type="claude_pro",
        seven_day_pct=5.0,
        seven_day_resets_at="Tom 10:00",  # 24h out, 95% free, but non-Fable!
        scoped_windows=(),
    )

    # With fable_guard=True:
    guarded = build_fleet_reset_radar(
        (fable_ok, fable_capped, team_capped, pro_perishable), (), now=BASE_NOW, fable_guard=True
    )
    assert guarded.capacity.claude_fable_total_seats == 2
    assert guarded.capacity.claude_fable_free_seats == 0.7  # max-1 (0.7) + max-2 (0.0)
    assert guarded.capacity.claude_next_unlock is not None
    assert guarded.capacity.claude_next_unlock.slot == 2
    assert guarded.capacity.perishable_seats_48h == 0.0
    assert guarded.perishable == ()

    # Team/Pro 7d resets STILL appear in events with no_fable=True and kind="upcoming"
    team_ev = next(e for e in guarded.events if e.slot == 3)
    pro_ev = next(e for e in guarded.events if e.slot == 4)
    assert team_ev.no_fable is True and team_ev.kind == "upcoming" and team_ev.window == "7d"
    assert pro_ev.no_fable is True and pro_ev.kind == "upcoming" and pro_ev.window == "7d"
    assert not any(e.slot in (3, 4) for e in guarded.unlocks)

    # With fable_guard=False: Team/Pro included!
    unguarded = build_fleet_reset_radar(
        (fable_ok, fable_capped, team_capped, pro_perishable), (), now=BASE_NOW, fable_guard=False
    )
    assert unguarded.capacity.claude_fable_total_seats == 4
    assert unguarded.capacity.claude_next_unlock is not None
    assert unguarded.capacity.claude_next_unlock.slot == 3
    assert any(e.slot == 4 for e in unguarded.perishable)
    assert all(not e.no_fable for e in unguarded.events)

    # Fleet with ZERO Fable seats (Team-only): fable_guard=True does NOT hide them!
    team_only = build_fleet_reset_radar(
        (team_capped, pro_perishable), (), now=BASE_NOW, fable_guard=True
    )
    assert team_only.capacity.claude_fable_total_seats == 2
    assert team_only.capacity.claude_next_unlock is not None
    assert team_only.capacity.claude_next_unlock.slot == 3
    assert len(team_only.perishable) == 1


# ---------------------------------------------------------------------------
# 7. Codex banked reset credits (live vs relogin vs disabled vs usable)
# ---------------------------------------------------------------------------


def test_codex_banked_reset_credits_accounting_and_sorting() -> None:
    rows = (
        _codex_row(
            -1,
            "work-capped",
            is_active=True,
            seven_day_pct=96.0,
            seven_day_resets_at="Nov 24 10:00",
            reset_credits_available=2,
            reset_credits_usable=1,
        ),
        _codex_row(
            -2,
            "personal-banked",
            seven_day_pct=10.0,
            seven_day_resets_at="Nov 25 10:00",
            reset_credits_available=3,
            reset_credits_usable=0,
        ),
        _codex_row(
            -3,
            "relogin-banked",
            seven_day_pct=None,
            attention_note="relogin",
            attention_kind="warn",
            reset_credits_available=4,
            reset_credits_usable=1,
        ),
        _codex_row(
            -4,
            "disabled-banked",
            disabled=True,
            seven_day_pct=50.0,
            reset_credits_available=1,
            reset_credits_usable=1,
        ),
        _codex_row(
            -5,
            "zero-credits",
            seven_day_pct=20.0,
            reset_credits_available=0,
            reset_credits_usable=0,
        ),
        _codex_row(
            -6,
            "unknown-credits",
            seven_day_pct=20.0,
            reset_credits_available=None,
            reset_credits_usable=None,
        ),
    )

    radar = build_fleet_reset_radar((), rows, now=BASE_NOW)
    c = radar.credits
    assert c.total_available == 10   # 2 + 3 + 4 + 1
    assert c.live_available == 5     # 2 + 3 (excludes relogin + disabled)
    assert c.total_usable == 1       # only -1 is live+enabled+usable
    assert len(c.accounts) == 4
    # Sorted: usable first (-1), then live banked (-2) / disabled (-4), then relogin (-3)
    assert c.accounts[0].slot == -1 and c.accounts[0].usable == 1 and c.accounts[0].is_active
    assert c.accounts[1].slot == -2 and c.accounts[1].available == 3 and c.accounts[1].usable == 0
    assert c.accounts[-1].slot == -3 and c.accounts[-1].needs_relogin is True and c.accounts[-1].usable == 0


# ---------------------------------------------------------------------------
# 8. Perishable 48h detection & seat-eq deduplication
# ---------------------------------------------------------------------------


def test_perishable_48h_detection_boundaries() -> None:
    # Exactly at boundary: 25% free (75% used) at exactly 48h -> perishable!
    boundary_ok = _claude_row(
        1,
        "edge-ok",
        seven_day_pct=75.0,
        seven_day_resets_at=dt.datetime.fromtimestamp(BASE_NOW + 48 * 3600, tz=dt.timezone.utc).isoformat(),
        scoped_windows=(("Fable", 75.0),),
        scoped_resets_at=(
            ("Fable", dt.datetime.fromtimestamp(BASE_NOW + 48 * 3600, tz=dt.timezone.utc).isoformat()),
        ),
    )
    # 5h window with 90% free in 2h -> NEVER perishable!
    short_5h = _claude_row(
        2,
        "short-5h",
        five_hour_pct=10.0,
        five_hour_resets_at="12:00",
        seven_day_pct=10.0,
        seven_day_resets_at=dt.datetime.fromtimestamp(BASE_NOW + 49 * 3600, tz=dt.timezone.utc).isoformat(),  # >48h!
        scoped_windows=(("Fable", 10.0),),
        scoped_resets_at=(
            ("Fable", dt.datetime.fromtimestamp(BASE_NOW + 49 * 3600, tz=dt.timezone.utc).isoformat()),
        ),
    )
    # Only 20% free (<25%) in 10h -> upcoming (NOT perishable, NOT unlock)
    low_free = _claude_row(
        3,
        "low-free",
        seven_day_pct=80.0,
        seven_day_resets_at="20:00",
        scoped_windows=(("Fable", 80.0),),
        scoped_resets_at=(("Fable", "20:00"),),
    )
    # Single Codex account with TWO different perishable windows (7d 80% free + Spark 90% free)
    # -> both appear in radar.perishable, but perishable_seats_48h counts at most 1.0 seat-eq (0.80 from 7d)!
    codex_dual = _codex_row(
        -1,
        "codex-expiring",
        seven_day_pct=20.0,
        seven_day_resets_at="Tom 10:00",  # +24h -> 80% free
        scoped_windows=(("Spark", 10.0),),
        scoped_resets_at=(("Spark", "Tom 14:00"),),  # +28h -> 90% free
    )

    radar = build_fleet_reset_radar((boundary_ok, short_5h, low_free), (codex_dual,), now=BASE_NOW)

    p_keys = [(e.vendor, e.slot, e.window) for e in radar.perishable]
    assert p_keys == [
        ("codex", -1, "7d"),
        ("codex", -1, "Spark"),
        ("claude", 1, "Fable"),
    ], p_keys

    # edge-ok (0.25) + codex-expiring primary 7d (0.80) = 1.05 (NOT 0.25 + 0.80 + 0.90 = 1.95!)
    assert radar.capacity.perishable_seats_48h == 1.05


# ---------------------------------------------------------------------------
# 9. All 5 Smart Fleet Advisor recommendation tiers & priority order
# ---------------------------------------------------------------------------


def test_advisor_tier1_use_reset_credit_beats_all_lower_tiers() -> None:
    # Populate triggers for ALL tiers simultaneously; Tier 1 must win!
    claude = (
        _claude_row(
            1,
            "c-perish",
            seven_day_pct=10.0,
            seven_day_resets_at="Tom 10:00",
            scoped_windows=(("Fable", 10.0),),
            scoped_resets_at=(("Fable", "Tom 10:00"),),
        ),
        _claude_row(2, "c-lock-1", scoped_windows=(("Fable", 50.0),)),
        _claude_row(3, "c-lock-2", scoped_windows=(("Fable", 50.0),)),
    )
    codex = (
        _codex_row(
            -1,
            "codex-hot",
            is_active=True,
            seven_day_pct=92.0,
            seven_day_resets_at="14:00",
            reset_credits_available=2,
            reset_credits_usable=1,
        ),
    )
    radar = build_fleet_reset_radar(
        claude, codex, account_notes={2: "relogin", 3: "relogin"}, now=BASE_NOW
    )
    rec = radar.recommendation
    assert rec is not None
    assert rec.kind == "use_reset_credit"
    assert rec.target_vendor == "codex" and rec.target_slot == -1
    assert len(rec.headline) <= 72 and "\n" not in rec.detail
    assert "codex-hot" in rec.headline


def test_advisor_tier2_burn_perishable_standby_and_urgent_active() -> None:
    # A. Standby seat with >=40% free expiring <=36h triggers burn_perishable
    c_active = _claude_row(
        1,
        "active-calm",
        is_active=True,
        seven_day_pct=20.0,
        seven_day_resets_at="Nov 27 10:00",
        scoped_windows=(("Fable", 20.0),),
        scoped_resets_at=(("Fable", "Nov 27 10:00"),),
    )
    c_standby = _claude_row(
        2,
        "standby-exp",
        is_active=False,
        five_hour_pct=10.0,
        seven_day_pct=4.0,
        seven_day_resets_at="Tom 16:00",  # +30h, 96% free
        scoped_windows=(("Fable", 4.0),),
        scoped_resets_at=(("Fable", "Tom 16:00"),),
    )
    r1 = build_fleet_reset_radar((c_active, c_standby), (), now=BASE_NOW)
    assert r1.recommendation is not None
    assert r1.recommendation.kind == "burn_perishable"
    assert r1.recommendation.target_vendor == "claude" and r1.recommendation.target_slot == 2
    assert len(r1.recommendation.headline) <= 72

    # B. Active seat expiring in 24h (NOT urgent <=12h) does NOT nag to switch onto itself
    c_active_24h = _claude_row(
        1,
        "active-24h",
        is_active=True,
        seven_day_pct=20.0,
        seven_day_resets_at="Tom 10:00",  # +24h
        scoped_windows=(("Fable", 20.0),),
        scoped_resets_at=(("Fable", "Tom 10:00"),),
    )
    r2 = build_fleet_reset_radar((c_active_24h,), (), now=BASE_NOW)
    assert r2.recommendation is not None and r2.recommendation.kind == "healthy"

    # C. Active seat expiring in 8h (urgent <=12h) WITH >=40% free DOES trigger burn_perishable!
    c_active_urgent = _claude_row(
        1,
        "active-8h",
        is_active=True,
        seven_day_pct=15.0,
        seven_day_resets_at="18:00",  # +8h, 85% free
        scoped_windows=(("Fable", 15.0),),
        scoped_resets_at=(("Fable", "18:00"),),
    )
    r3 = build_fleet_reset_radar((c_active_urgent,), (), now=BASE_NOW)
    assert r3.recommendation is not None
    assert r3.recommendation.kind == "burn_perishable"
    assert r3.recommendation.target_slot == 1

    # D. Standby perishable seat that is currently capped on 5h (>=95%) is skipped
    c_5h_capped = _claude_row(
        2,
        "blocked-5h",
        is_active=False,
        five_hour_pct=100.0,
        five_hour_resets_at="12:00",
        seven_day_pct=10.0,
        seven_day_resets_at="Tom 10:00",
        scoped_windows=(("Fable", 10.0),),
        scoped_resets_at=(("Fable", "Tom 10:00"),),
    )
    r4 = build_fleet_reset_radar((c_active, c_5h_capped), (), now=BASE_NOW)
    assert r4.recommendation is not None and r4.recommendation.kind != "burn_perishable"


def test_advisor_tier3_unlock_soon_requires_low_capacity() -> None:
    c_capped = _claude_row(
        1,
        "main-cap",
        seven_day_pct=100.0,
        seven_day_resets_at="13:00",  # +3h
        scoped_windows=(("Fable", 100.0),),
        scoped_resets_at=(("Fable", "13:00"),),
    )
    c_half = _claude_row(
        2,
        "backup-half",
        seven_day_pct=50.0,
        seven_day_resets_at="Nov 27 10:00",
        scoped_windows=(("Fable", 50.0),),
        scoped_resets_at=(("Fable", "Nov 27 10:00"),),
    )
    # Free capacity = 0.5 <= 1.5, unlock in 3h <= 6h -> unlock_soon!
    r_low = build_fleet_reset_radar((c_capped, c_half), (), now=BASE_NOW)
    assert r_low.recommendation is not None
    assert r_low.recommendation.kind == "unlock_soon"
    assert r_low.recommendation.target_vendor == "claude" and r_low.recommendation.target_slot == 1
    assert len(r_low.recommendation.headline) <= 72

    # Add 2 full free seats (free capacity = 2.5 > 1.5) -> NOT unlock_soon, becomes healthy!
    c_free1 = _claude_row(3, "free-1", scoped_windows=(("Fable", 0.0),), seven_day_pct=0.0)
    c_free2 = _claude_row(4, "free-2", scoped_windows=(("Fable", 0.0),), seven_day_pct=0.0)
    r_plenty = build_fleet_reset_radar((c_capped, c_half, c_free1, c_free2), (), now=BASE_NOW)
    assert r_plenty.recommendation is not None and r_plenty.recommendation.kind == "healthy"

    # Codex unlock_soon triggers even when Claude has plenty of free seats!
    x_capped = _codex_row(-1, "codex-wall", seven_day_pct=100.0, soonest_reset_at=BASE_NOW + 7200)
    r_codex_unlock = build_fleet_reset_radar((c_free1, c_free2), (x_capped,), now=BASE_NOW)
    assert r_codex_unlock.recommendation is not None
    assert r_codex_unlock.recommendation.kind == "unlock_soon"
    assert r_codex_unlock.recommendation.target_vendor == "codex"
    assert r_codex_unlock.recommendation.target_slot == -1
    assert r_codex_unlock.capacity.codex_next_unlock is not None
    assert r_codex_unlock.capacity.codex_next_unlock.resets_at_epoch == BASE_NOW + 7200


def test_generators_out_of_credits_and_soonest_reset_refinement() -> None:
    # Out-of-credits workspace (40% used, resets in 24h) must have 0.0 free seats & NOT perishable!
    x_ooc = _codex_row(
        -1,
        "biz-ooc",
        plan_type="self_serve_business_prolite",
        seven_day_pct=40.0,
        seven_day_resets_at="Tom 10:00",
        attention_note="out of credits · Add credits",
        attention_kind="crit",
    )
    # Codex row with string clock "13:00" + exact soonest_reset_at 13:00:42 refines to exact second
    exact_ep = _local_epoch(2026, 11, 21, 13, 0, 42)
    x_refined = _codex_row(
        -2,
        "pro-exact",
        seven_day_pct=90.0,
        seven_day_resets_at="13:00",
        soonest_reset_at=exact_ep,
    )
    c_gen = (_claude_row(1, "gen-c", scoped_windows=(("Fable", 25.0),), seven_day_pct=25.0) for _ in range(1))
    x_gen = (row for row in (x_ooc, x_refined))

    radar = build_fleet_reset_radar(c_gen, x_gen, now=BASE_NOW)
    assert radar.capacity.claude_fable_total_seats == 1
    assert radar.capacity.claude_fable_free_seats == 0.75
    assert radar.capacity.codex_total_seats == 2
    assert radar.capacity.codex_free_seats == 0.1  # biz-ooc=0.0, pro-exact=0.1
    assert radar.perishable == (), "out-of-credits row must never be marked perishable"

    ev_refined = next(e for e in radar.events if e.slot == -2)
    assert ev_refined.resets_at_epoch == exact_ep
    ev_ooc = next(e for e in radar.events if e.slot == -1)
    assert ev_ooc.kind == "upcoming"


def test_advisor_tier4_relogin_capacity() -> None:
    c1 = _claude_row(1, "ok", scoped_windows=(("Fable", 20.0),), seven_day_pct=20.0)
    c2 = _claude_row(2, "dead1", scoped_windows=(("Fable", 0.0),))
    c3 = _claude_row(3, "dead2", scoped_windows=(("Fable", 0.0),))
    r_claude = build_fleet_reset_radar(
        (c1, c2, c3), (), account_notes={2: "⚠ relogin", 3: "fetch-failing (HTTP 403)"}, now=BASE_NOW
    )
    assert r_claude.recommendation is not None
    assert r_claude.recommendation.kind == "relogin_capacity"
    assert r_claude.recommendation.target_vendor == "claude"
    assert len(r_claude.recommendation.headline) <= 72

    # 2 locked Codex seats (with banked credits noted in detail)
    x1 = _codex_row(-1, "xdead1", seven_day_pct=None, attention_note="relogin", attention_kind="warn", reset_credits_available=2)
    x2 = _codex_row(-2, "xdead2", seven_day_pct=None, attention_note="relogin", attention_kind="warn", reset_credits_available=1)
    r_codex = build_fleet_reset_radar((c1,), (x1, x2), now=BASE_NOW)
    assert r_codex.recommendation is not None
    assert r_codex.recommendation.kind == "relogin_capacity"
    assert r_codex.recommendation.target_vendor == "codex"
    assert "3×" in r_codex.recommendation.detail

    # Only 1 locked seat does NOT trigger relogin_capacity
    r_one = build_fleet_reset_radar((c1, c2), (), account_notes={2: "relogin"}, now=BASE_NOW)
    assert r_one.recommendation is not None and r_one.recommendation.kind == "healthy"


def test_advisor_tier5_healthy_and_empty_fleet_and_long_alias_cap() -> None:
    # Empty fleet -> recommendation is None, zero counts
    empty = build_fleet_reset_radar((), (), now=BASE_NOW)
    assert empty.recommendation is None
    assert empty.events == ()
    assert empty.unlocks == ()
    assert empty.perishable == ()
    assert empty.credits == ResetCreditsSummary(0, 0, 0, ())
    assert empty.capacity.claude_fable_total_seats == 0
    assert empty.capacity.codex_total_seats == 0

    # Healthy dual-vendor fleet with long alias still obeys <=72 char headline limit
    long_alias = "super-extremely-long-enterprise-workspace-alias-name-for-testing-truncation"
    c1 = _claude_row(1, long_alias, seven_day_pct=20.0, scoped_windows=(("Fable", 20.0),))
    x1 = _codex_row(-1, "codex-main", seven_day_pct=15.0, reset_credits_available=2, reset_credits_usable=0)
    radar = build_fleet_reset_radar((c1,), (x1,), now=BASE_NOW)
    assert radar.recommendation is not None
    assert radar.recommendation.kind == "healthy"
    assert len(radar.recommendation.headline) <= 72
    assert "Fable" in radar.recommendation.headline and "Codex" in radar.recommendation.headline


# ---------------------------------------------------------------------------
# 10. Privacy & canary leak prevention
# ---------------------------------------------------------------------------


def test_privacy_zero_raw_emails_or_tokens_in_radar() -> None:
    secret_email = "secret.exec@private-belkins.test"
    secret_token = "sk-ant-oat01-SECRET_TOKEN_DO_NOT_LEAK"

    # Unaliased Claude row + email-polluted Codex default display_name alias
    c_no_alias = _claude_row(
        1,
        "",
        email=secret_email,
        seven_day_pct=10.0,
        seven_day_resets_at="Tom 10:00",
        scoped_windows=(("Fable", 10.0),),
        scoped_resets_at=(("Fable", "Tom 10:00"),),
    )
    x_email_alias = _codex_row(
        -1,
        f"{secret_email} · deadbeef",
        email=secret_email,
        is_active=True,
        seven_day_pct=96.0,
        seven_day_resets_at="13:00",
        reset_credits_available=2,
        reset_credits_usable=1,
        info_notes=(f"debug {secret_token}",),
    )
    c_note_token = _claude_row(
        2,
        "clean-slot",
        email=secret_email,
        scoped_windows=(("Fable", 50.0),),
    )

    radar = build_fleet_reset_radar(
        (c_no_alias, c_note_token),
        (x_email_alias,),
        account_notes={2: f"auth failed {secret_token}"},
        now=BASE_NOW,
    )
    blob = json.dumps(dataclasses.asdict(radar), allow_nan=False)
    assert secret_email not in blob, f"raw email leaked into radar: {blob}"
    assert "@" not in blob, f"email symbol leaked into radar: {blob}"
    assert secret_token not in blob, f"secret token leaked into radar: {blob}"
    assert radar.events[0].alias in ("Claude #1", "Codex #1")
    assert radar.recommendation is not None
    assert secret_email not in radar.recommendation.headline
    assert secret_email not in radar.recommendation.detail


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
    tests = _tests()
    failures: list[str] = []
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
