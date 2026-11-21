"""Fleet Reset Radar, Banked Reset-Credit Tracker, Capacity Meter & Advisor.

This module is the single canonical, pure, zero-I/O domain layer that computes
unified reset schedules, banked Codex/Claude reset credits, aggregate fleet
capacity, and actionable advisor recommendations across all Claude
(:class:`~cc_usage_widget.contracts.AccountRow`) and Codex quota rows.

Design invariants
-----------------
1. **Zero I/O, zero AppKit, zero ``claude_swap`` dependency.** Imports only
   the Python standard library plus ``.contracts`` and pure helpers from
   ``.accounts``, so it works identically in bare ``python3`` as well as the
   ``claude-swap`` environment and is safe to call from any thread.
2. **Deterministic.** Passing *now* produces repeatable outputs independent of
   the wall clock.
3. **Privacy-first.** Raw email addresses and credentials never reach
   :attr:`ResetEvent.alias`, :attr:`ResetCreditAccount.alias`,
   :attr:`FleetRecommendation.headline`, or :attr:`FleetRecommendation.detail`.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .accounts import (
    fleet_has_fable_accounts,
    row_has_fable_quota,
    row_lacks_fable_quota,
)
from .contracts import (
    CODEX_PSEUDO_ACCOUNT_SLOT,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    vendor_label,
)

__all__ = [
    "ResetEvent",
    "ResetCreditAccount",
    "ResetCreditsSummary",
    "FleetCapacitySummary",
    "FleetRecommendation",
    "FleetResetRadar",
    "parse_reset_epoch",
    "format_countdown",
    "format_local_reset_label",
    "build_fleet_reset_radar",
]

# ---------------------------------------------------------------------------
# Thresholds & constants
# ---------------------------------------------------------------------------

UNLOCK_USED_PCT: Final[float] = 85.0
"""Utilization (%) at or above which an upcoming reset is classified ``unlock``."""

PERISHABLE_MIN_FREE_PCT: Final[float] = 25.0
"""Minimum unused weekly/scoped quota (%) to classify an expiring reset ``perishable``."""

PERISHABLE_HORIZON_SECONDS: Final[float] = 48.0 * 3600.0
"""Maximum seconds until reset for ``perishable`` classification (48 hours)."""

PAST_RESET_SLACK_SECONDS: Final[float] = 30.0
"""Resets at or before ``now - 30s`` are treated as expired/past and skipped."""

ADVISOR_PERISHABLE_MIN_FREE_PCT: Final[float] = 40.0
ADVISOR_PERISHABLE_MAX_SECONDS: Final[float] = 36.0 * 3600.0
ADVISOR_ACTIVE_URGENT_SECONDS: Final[float] = 12.0 * 3600.0
ADVISOR_UNLOCK_SOON_MAX_SECONDS: Final[float] = 6.0 * 3600.0
ADVISOR_LOW_CAPACITY_SEATS: Final[float] = 1.5
ADVISOR_RELOGIN_MIN_SEATS: Final[int] = 2
HEADLINE_MAX_CHARS: Final[int] = 72

_WEEKDAY_NAMES: Final[tuple[str, ...]] = (
    "Mon",
    "Tue",
    "Wed",
    "Thu",
    "Fri",
    "Sat",
    "Sun",
)
_WEEKDAY_MAP: Final[dict[str, int]] = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

_MONTH_NAMES: Final[tuple[str, ...]] = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_MONTH_MAP: Final[dict[str, int]] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_NUM_RE: Final[re.Pattern[str]] = re.compile(r"^\d+(?:\.\d+)?$")
_ISO_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_CLOCK_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_TOMORROW_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:tom|tomorrow)\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$",
    re.IGNORECASE,
)
_WEEKDAY_CLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z]{3,9})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$"
)
_MONTH_DAY_CLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z]{3,9})\s+(\d{1,2})(?:,?\s+(\d{4}))?,?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$"
)
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"[^\s@]+@[^\s@]+")

_PLAN_LABELS: Final[dict[str, str]] = {
    "pro": "Pro",
    "self_serve_business_prolite": "Business",
    "claude_max_20x": "Max 20x",
    "claude_team": "Team",
}
_CLAUDE_PLAN_RE: Final[re.Pattern[str]] = re.compile(
    r"^claude_([a-z]+(?:_[a-z]+)*)(?:_(\d+x))?$"
)

_TRANSIENT_WARN_MARKERS: Final[tuple[str, ...]] = (
    "offline",
    "rate limited",
    "endpoint error",
    "network",
    "timeout",
)
_RELOGIN_NOTE_MARKERS: Final[tuple[str, ...]] = (
    "relogin",
    "re-login",
    "login",
    "expired",
    "revoked",
    "no access",
    "unreadable",
    "quarantin",
    "fetch-failing",
    "auth",
    "401",
    "403",
)
_LOCKED_STATUSES: Final[frozenset[str]] = frozenset(
    {"relogin", "needs_relogin", "expired", "dead", "no_access", "revoked", "quarantined"}
)
_UNAVAILABLE_STATUSES: Final[frozenset[str]] = frozenset(
    {"offline", "rate_limited", "endpoint_error", "error", "stale"}
)

# ---------------------------------------------------------------------------
# Public frozen data contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResetEvent:
    vendor: str                 # "claude" | "codex"
    slot: int                   # Claude slot (1..N) or Codex slot (-1..-N)
    alias: str                  # display alias (never raw email if alias exists)
    window: str                 # "5h" | "7d" | "Fable" | "Opus" | "Sonnet" | extra limit name
    used_pct: float | None      # 0..100
    free_pct: float | None      # 0..100
    resets_at_epoch: float      # POSIX timestamp (UTC)
    seconds_until: float        # >= 0.0
    reset_label: str            # local human label ("13:00", "Tom 13:00", "Mon 20:00", "Nov 27 13:00")
    countdown_label: str        # concise countdown ("<1m", "45m", "2h 54m", "1d 4h")
    kind: str                   # "unlock" (used>=85%) | "perishable" (free>=25% & <=48h & weekly/scoped) | "upcoming"
    is_active: bool = False
    disabled: bool = False
    plan_label: str = ""
    no_fable: bool = False


@dataclass(frozen=True, slots=True)
class ResetCreditAccount:
    vendor: str                 # "codex" | "claude"
    slot: int
    alias: str
    available: int              # total banked credits on account
    usable: int                 # usable right now
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
    kind: str                   # "use_reset_credit" | "burn_perishable" | "unlock_soon" | "relogin_capacity" | "healthy"
    headline: str               # concise <= 72 chars
    detail: str                 # 1-line explanation
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


# ---------------------------------------------------------------------------
# Pure time parsing & formatting
# ---------------------------------------------------------------------------


def _valid_epoch(val: float) -> float | None:
    if not math.isfinite(val) or val <= 0.0:
        return None
    try:
        dt.datetime.fromtimestamp(val)
    except (OSError, OverflowError, ValueError):
        return None
    return val


def _valid_hms(hh: int, mm: int, ss: int) -> bool:
    return 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59


def parse_reset_epoch(value: Any, *, now: float | None = None) -> float | None:
    """Parse any reset timestamp representation into a UTC POSIX epoch float.

    Supports:
    - POSIX epoch ``int`` / ``float`` (or numeric string)
    - :class:`datetime.datetime`
    - ISO-8601 strings (``Z``, ``+00:00``, explicit offsets, or naive UTC)
    - Local wall-clock ``"HH:MM"`` (today, or tomorrow if already passed beyond slack)
    - ``"Tom HH:MM"`` / ``"Tomorrow HH:MM"``
    - Weekday + clock ``"Mon 20:00"`` / ``"Monday 20:00"``
    - Month + day + clock ``"Nov 27 13:00"`` / ``"Oct 24 08:10"`` (with safe
      Dec/Jan year rollover and leap-day handling)

    Returns ``None`` on empty, overdue, or unparseable input.
    """
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return _valid_epoch(float(value))

    if isinstance(value, dt.datetime):
        try:
            return _valid_epoch(value.timestamp())
        except (OSError, OverflowError, ValueError):
            return None

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    low = text.lower()
    if low.startswith("overdue"):
        return None

    # Numeric epoch string
    if _NUM_RE.match(text):
        try:
            return _valid_epoch(float(text))
        except ValueError:
            return None

    # ISO-8601 timestamp
    if _ISO_PREFIX_RE.match(text):
        iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = dt.datetime.fromisoformat(iso_text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        try:
            return _valid_epoch(parsed.timestamp())
        except (OSError, OverflowError, ValueError):
            return None

    moment = time.time() if now is None else float(now)
    try:
        base_dt = dt.datetime.fromtimestamp(moment)
    except (OSError, OverflowError, ValueError):
        return None

    # Bare local clock "HH:MM"[:SS]
    m_clock = _CLOCK_RE.match(text)
    if m_clock is not None:
        hh, mm = int(m_clock.group(1)), int(m_clock.group(2))
        ss = int(m_clock.group(3) or 0)
        if not _valid_hms(hh, mm, ss):
            return None
        cand = dt.datetime(base_dt.year, base_dt.month, base_dt.day, hh, mm, ss)
        if cand.timestamp() < moment - PAST_RESET_SLACK_SECONDS:
            next_day = base_dt.date() + dt.timedelta(days=1)
            cand = dt.datetime(next_day.year, next_day.month, next_day.day, hh, mm, ss)
        return _valid_epoch(cand.timestamp())

    # "Tom HH:MM" / "Tomorrow HH:MM"
    m_tom = _TOMORROW_RE.match(text)
    if m_tom is not None:
        hh, mm = int(m_tom.group(1)), int(m_tom.group(2))
        ss = int(m_tom.group(3) or 0)
        if not _valid_hms(hh, mm, ss):
            return None
        tom_date = base_dt.date() + dt.timedelta(days=1)
        cand = dt.datetime(tom_date.year, tom_date.month, tom_date.day, hh, mm, ss)
        return _valid_epoch(cand.timestamp())

    # Weekday + clock "Mon 20:00"
    m_wd = _WEEKDAY_CLOCK_RE.match(text)
    if m_wd is not None:
        wd = _WEEKDAY_MAP.get(m_wd.group(1).lower())
        if wd is not None:
            hh, mm = int(m_wd.group(2)), int(m_wd.group(3))
            ss = int(m_wd.group(4) or 0)
            if not _valid_hms(hh, mm, ss):
                return None
            delta_days = (wd - base_dt.weekday()) % 7
            target_date = base_dt.date() + dt.timedelta(days=delta_days)
            cand = dt.datetime(target_date.year, target_date.month, target_date.day, hh, mm, ss)
            if delta_days == 0 and cand.timestamp() < moment - PAST_RESET_SLACK_SECONDS:
                target_date = base_dt.date() + dt.timedelta(days=7)
                cand = dt.datetime(target_date.year, target_date.month, target_date.day, hh, mm, ss)
            return _valid_epoch(cand.timestamp())

    # Month + day + [year] + clock "Nov 27 13:00" / "Oct 24 08:10"
    m_md = _MONTH_DAY_CLOCK_RE.match(text)
    if m_md is not None:
        month = _MONTH_MAP.get(m_md.group(1).lower())
        if month is None:
            return None
        day = int(m_md.group(2))
        explicit_year = int(m_md.group(3)) if m_md.group(3) else None
        hh, mm = int(m_md.group(4)), int(m_md.group(5))
        ss = int(m_md.group(6) or 0)
        if not _valid_hms(hh, mm, ss):
            return None
        if explicit_year is not None:
            try:
                cand = dt.datetime(explicit_year, month, day, hh, mm, ss)
            except ValueError:
                return None
            return _valid_epoch(cand.timestamp())
        # Evaluate adjacent years so Dec->Jan forward rollovers, Jan->Dec past
        # timestamps, and leap-day (Feb 29) boundaries resolve cleanly without throwing.
        candidates: list[dt.datetime] = []
        for year in (base_dt.year - 1, base_dt.year, base_dt.year + 1):
            try:
                candidates.append(dt.datetime(year, month, day, hh, mm, ss))
            except ValueError:
                continue
        if not candidates:
            return None
        best = min(candidates, key=lambda c: abs((c - base_dt).total_seconds()))
        return _valid_epoch(best.timestamp())

    return None


def format_countdown(seconds: float) -> str:
    """Format a remaining duration concisely.

    - ``"now"`` if ``<= 0``
    - ``"<1m"`` if ``< 60``
    - ``"42m"`` if ``< 3600``
    - ``"2h 54m"`` (or ``"2h"`` on exact hours) if ``< 86400``
    - ``"2d 10h"`` (or ``"2d"`` on exact days) if ``>= 86400``
    """
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        return "now"
    if not math.isfinite(secs) or secs <= 0.0:
        return "now"
    if secs < 60.0:
        return "<1m"
    total = int(secs)
    if total < 3_600:
        return f"{total // 60}m"
    if total < 86_400:
        hours, rem = divmod(total, 3_600)
        minutes = rem // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, rem = divmod(total, 86_400)
    hours = rem // 3_600
    return f"{days}d {hours}h" if hours else f"{days}d"


def format_local_reset_label(epoch: float, *, now: float | None = None) -> str:
    """Format *epoch* as a concise local human label relative to *now*.

    - ``"13:00"`` if today
    - ``"Tom 13:00"`` if tomorrow
    - ``"Mon 20:00"`` if 2..6 days out
    - ``"Nov 27 13:00"`` otherwise
    """
    try:
        target = float(epoch)
        moment = time.time() if now is None else float(now)
        if not math.isfinite(target) or not math.isfinite(moment):
            return ""
        when = dt.datetime.fromtimestamp(target)
        today = dt.datetime.fromtimestamp(moment).date()
    except (OSError, OverflowError, TypeError, ValueError):
        return ""
    days = (when.date() - today).days
    clock = when.strftime("%H:%M")
    if days == 0:
        return clock
    if days == 1:
        return f"Tom {clock}"
    if 2 <= days <= 6:
        return f"{_WEEKDAY_NAMES[when.weekday()]} {clock}"
    return f"{_MONTH_NAMES[when.month]} {when.day} {clock}"


# ---------------------------------------------------------------------------
# Internal normalization & classification helpers
# ---------------------------------------------------------------------------


def _format_plan_label(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    mapped = _PLAN_LABELS.get(text)
    if mapped is not None:
        return mapped
    match = _CLAUDE_PLAN_RE.match(text)
    if match is not None:
        words = " ".join(part.capitalize() for part in match.group(1).split("_"))
        return f"{words} {match.group(2)}" if match.group(2) else words
    return text


def _safe_pct(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    val = float(value)
    if not math.isfinite(val):
        return None
    return round(max(0.0, min(100.0, val)), 4)


def _safe_nonneg_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


def _safe_slot(row: Any, default: int = 0) -> int:
    raw = getattr(row, "slot", default)
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _safe_alias(row: Any, vendor: str, slot: int) -> str:
    """Return a clean display alias that never leaks raw email addresses."""
    raw = str(getattr(row, "alias", "") or "").strip()
    raw = " ".join(raw.split())
    v_label = vendor_label(vendor)  # type: ignore[arg-type]
    fallback = f"{v_label} #{abs(slot)}" if slot != 0 else v_label
    if not raw:
        return fallback
    if _EMAIL_RE.search(raw):
        cleaned = _EMAIL_RE.sub("", raw).strip(" ·-|()[]")
        # If only an opaque hex id prefix remains (like Codex's default
        # "jane@x.io · 0a1b2c3d"), use the clean vendor slot label instead.
        if not cleaned or re.fullmatch(r"[0-9a-fA-F]{6,16}", cleaned):
            return fallback
        return cleaned
    return raw


def _fit_headline(text: str, limit: int = HEADLINE_MAX_CHARS) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)].rstrip() + "…"


def _short_alias(alias: str, max_len: int = 20) -> str:
    if len(alias) <= max_len:
        return alias
    return alias[: max_len - 1].rstrip() + "…"


def _is_short_rolling_window(window: str) -> bool:
    low = window.strip().lower()
    if low in ("5h", "five_hour", "1h", "hourly"):
        return True
    return low.endswith(" (5h)") or low.endswith(" 5h")


def _window_specificity(window: str, vendor: str = VENDOR_CLAUDE) -> int:
    low = window.strip().lower()
    if low.startswith("fable"):
        return 3
    if _is_short_rolling_window(low):
        return 0
    if low in ("7d", "seven_day", "weekly"):
        return 2 if vendor == VENDOR_CODEX else 1
    return 1 if vendor == VENDOR_CODEX else 2


def _is_live_codex_row(row: Any, slot: int) -> bool:
    explicit = getattr(row, "is_live", None)
    if explicit is False:
        return False
    if explicit is True:
        return True
    return slot != CODEX_PSEUDO_ACCOUNT_SLOT


def _row_health_state(
    row: Any,
    vendor: str,
    slot: int,
    notes: Mapping[int, str],
) -> tuple[bool, bool]:
    """Return ``(needs_relogin, is_unavailable)`` for *row*.

    - ``needs_relogin``: auth/credential/sentinel lock requiring operator action
      (counted in ``*_locked_relogin_seats``, excluded from live capacity & events).
    - ``is_unavailable``: transient transport/offline/rate-limit block (excluded
      from live capacity & events, but NOT mislabelled as needing re-login).
    """
    if bool(getattr(row, "needs_relogin", False)):
        return True, True

    status = str(getattr(row, "status", "") or "").strip().lower()
    if status in _LOCKED_STATUSES:
        return True, True
    if status in _UNAVAILABLE_STATUSES:
        return False, True

    note_from_map = str(notes.get(slot, "") or "").strip() if slot in notes else ""
    attn_note = str(getattr(row, "attention_note", "") or "").strip()
    attn_kind = str(getattr(row, "attention_kind", "") or "").strip().lower()

    if note_from_map:
        low_map = note_from_map.lower()
        if any(tok in low_map for tok in _TRANSIENT_WARN_MARKERS) and not any(
            tok in low_map for tok in _RELOGIN_NOTE_MARKERS
        ):
            return False, True
        return True, True

    if attn_kind == "warn":
        low_attn = attn_note.lower()
        if low_attn and any(tok in low_attn for tok in _TRANSIENT_WARN_MARKERS) and not any(
            tok in low_attn for tok in _RELOGIN_NOTE_MARKERS
        ):
            return False, True
        return True, True

    if attn_note and attn_kind not in ("info", "crit"):
        low_attn = attn_note.lower()
        if low_attn.startswith("relogin in "):
            return False, False
        if any(tok in low_attn for tok in _RELOGIN_NOTE_MARKERS):
            return True, True
        if any(tok in low_attn for tok in _TRANSIENT_WARN_MARKERS):
            return False, True

    return False, False


def _expired_set(row: Any) -> set[str]:
    raw = getattr(row, "expired_windows", ()) or ()
    out: set[str] = set()
    for item in raw:
        key = str(item or "").strip().lower()
        if key:
            out.add(key)
    return out


def _is_window_expired(expired: set[str], canonical_key: str, display_label: str) -> bool:
    return canonical_key.lower() in expired or display_label.lower() in expired


def _extract_extra_limits(row: Any) -> list[tuple[str, float | None, Any, bool]]:
    """Extract duck-typed ``extra_limits`` entries as ``(name, pct, reset_raw, expired)``."""
    raw_extras = getattr(row, "extra_limits", None)
    if not raw_extras or isinstance(raw_extras, (str, bytes)):
        return []
    out: list[tuple[str, float | None, Any, bool]] = []
    for item in raw_extras:
        if isinstance(item, Mapping):
            name = str(
                item.get("name")
                or item.get("limit_name")
                or item.get("label")
                or item.get("window")
                or ""
            ).strip()
            pct = _safe_pct(
                item.get("used_pct")
                if "used_pct" in item
                else item.get("used_percent", item.get("pct"))
            )
            reset_raw = (
                item.get("resets_at")
                if item.get("resets_at") is not None
                else item.get("reset_at", item.get("resets_at_epoch"))
            )
            exp = bool(item.get("expired", False))
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 3:
            name = str(item[0] or "").strip()
            pct = _safe_pct(item[1])
            reset_raw = item[2]
            exp = bool(item[3]) if len(item) >= 4 else False
        else:
            name = str(
                getattr(item, "name", None)
                or getattr(item, "limit_name", None)
                or getattr(item, "label", None)
                or getattr(item, "window", None)
                or ""
            ).strip()
            raw_pct = getattr(item, "used_pct", None)
            if raw_pct is None:
                raw_pct = getattr(item, "used_percent", None)
            if raw_pct is None:
                raw_pct = getattr(item, "pct", None)
            pct = _safe_pct(raw_pct)
            reset_raw = getattr(item, "resets_at", None)
            if reset_raw is None:
                reset_raw = getattr(item, "reset_at", None)
            if reset_raw is None:
                reset_raw = getattr(item, "resets_at_epoch", None)
            exp = bool(getattr(item, "expired", False))
        if name:
            out.append((name, pct, reset_raw, exp))
    return out


def _classify_event_kind(
    *,
    window: str,
    used_pct: float | None,
    free_pct: float | None,
    seconds_until: float,
    disabled: bool,
    no_fable: bool,
    non_live: bool,
    out_of_credits: bool = False,
) -> str:
    if disabled or no_fable or non_live:
        return "upcoming"
    if used_pct is not None and used_pct >= UNLOCK_USED_PCT:
        return "unlock"
    if (
        not out_of_credits
        and free_pct is not None
        and free_pct >= PERISHABLE_MIN_FREE_PCT
        and seconds_until <= PERISHABLE_HORIZON_SECONDS
        and not _is_short_rolling_window(window)
    ):
        return "perishable"
    return "upcoming"


def _event_dedup_priority(event: ResetEvent) -> tuple[int, int, float, int]:
    """Higher tuple wins when two windows on the same account reset in the same minute."""
    is_unlock = 1 if event.kind == "unlock" else 0
    is_perishable = 1 if event.kind == "perishable" else 0
    pct_rank = event.used_pct if event.used_pct is not None else -1.0
    spec_rank = _window_specificity(event.window, event.vendor)
    return (is_unlock, is_perishable, pct_rank, spec_rank)


def _extract_row_events(
    row: Any,
    *,
    vendor: str,
    slot: int,
    alias: str,
    plan_lbl: str,
    is_active: bool,
    disabled: bool,
    no_fable: bool,
    non_live: bool,
    moment: float,
) -> tuple[list[ResetEvent], float | None]:
    """Build deduplicated future ``ResetEvent``s for one account row.

    Returns ``(events, live_five_hour_pct)`` so the advisor knows if a
    perishable weekly seat is currently blocked on a 5h burst cap.
    """
    expired = _expired_set(row)
    soonest_epoch = parse_reset_epoch(getattr(row, "soonest_reset_at", None), now=moment)
    attn_kind = str(getattr(row, "attention_kind", "") or "").strip().lower()
    attn_note = str(getattr(row, "attention_note", "") or "").strip().lower()
    out_of_credits = attn_kind == "crit" and "out of credits" in attn_note

    fh_pct = _safe_pct(getattr(row, "five_hour_pct", None))
    sd_pct = _safe_pct(getattr(row, "seven_day_pct", None))
    fh_raw = getattr(row, "five_hour_resets_at", None)
    sd_raw = getattr(row, "seven_day_resets_at", None)

    fh_expired = _is_window_expired(expired, "five_hour", "5h")
    sd_expired = _is_window_expired(expired, "seven_day", "7d")

    live_fh_pct: float | None = None
    if fh_pct is not None and not fh_expired:
        fh_ep = parse_reset_epoch(fh_raw, now=moment)
        if fh_ep is None or fh_ep > moment - PAST_RESET_SLACK_SECONDS:
            live_fh_pct = fh_pct

    # Collect raw (window_label, used_pct, reset_raw, is_expired) candidates
    raw_windows: list[tuple[str, float | None, Any, bool]] = []
    if fh_raw is not None:
        raw_windows.append(("5h", fh_pct, fh_raw, fh_expired))

    # For Codex rows that carry only soonest_reset_at without seven_day_resets_at
    effective_sd_raw = sd_raw
    if (
        effective_sd_raw is None
        and soonest_epoch is not None
        and fh_raw is None
        and sd_pct is not None
        and not sd_expired
    ):
        effective_sd_raw = soonest_epoch
    if effective_sd_raw is not None:
        raw_windows.append(("7d", sd_pct, effective_sd_raw, sd_expired))

    scoped_pct_map: dict[str, float | None] = {}
    for entry in getattr(row, "scoped_windows", ()) or ():
        if isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)) and len(entry) >= 2:
            s_name = str(entry[0] or "").strip()
            if s_name:
                scoped_pct_map[s_name] = _safe_pct(entry[1])

    seen_scoped_names: set[str] = set()
    for entry in getattr(row, "scoped_resets_at", ()) or ():
        if isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)) and len(entry) >= 2:
            s_name = str(entry[0] or "").strip()
            s_reset = entry[1]
            if not s_name or s_reset is None:
                continue
            seen_scoped_names.add(s_name.lower())
            raw_windows.append(
                (
                    s_name,
                    scoped_pct_map.get(s_name),
                    s_reset,
                    _is_window_expired(expired, s_name, s_name),
                )
            )

    # If a Claude row reports a Fable scoped percentage without an explicit
    # scoped_resets_at entry, inherit seven_day_resets_at (same-minute dedup
    # ensures the account is never duplicated at that minute).
    if vendor == VENDOR_CLAUDE and effective_sd_raw is not None and not sd_expired:
        for s_name, s_pct in scoped_pct_map.items():
            if s_name.lower().startswith("fable") and s_name.lower() not in seen_scoped_names:
                if s_pct is not None and not _is_window_expired(expired, s_name, s_name):
                    raw_windows.append((s_name, s_pct, effective_sd_raw, False))
                    seen_scoped_names.add(s_name.lower())

    for e_name, e_pct, e_reset, e_exp in _extract_extra_limits(row):
        if e_name.lower() in seen_scoped_names:
            continue
        if e_reset is None:
            continue
        raw_windows.append(
            (
                e_name,
                e_pct,
                e_reset,
                e_exp or _is_window_expired(expired, e_name, e_name),
            )
        )

    # Parse, filter expired/past, and deduplicate same-minute resets on this account
    by_minute: dict[int, ResetEvent] = {}
    for win_label, used_pct, reset_raw, is_exp in raw_windows:
        if is_exp:
            continue
        epoch = parse_reset_epoch(reset_raw, now=moment)
        if epoch is None:
            continue
        # Refine string-clock minute precision with exact soonest_reset_at when matching
        if soonest_epoch is not None and abs(epoch - soonest_epoch) <= 90.0:
            epoch = soonest_epoch
        if epoch <= moment - PAST_RESET_SLACK_SECONDS:
            continue

        secs = max(0.0, epoch - moment)
        free_pct = round(max(0.0, min(100.0, 100.0 - used_pct)), 4) if used_pct is not None else None
        kind = _classify_event_kind(
            window=win_label,
            used_pct=used_pct,
            free_pct=free_pct,
            seconds_until=secs,
            disabled=disabled,
            no_fable=no_fable,
            non_live=non_live,
            out_of_credits=out_of_credits,
        )
        ev = ResetEvent(
            vendor=vendor,
            slot=slot,
            alias=alias,
            window=win_label,
            used_pct=used_pct,
            free_pct=free_pct,
            resets_at_epoch=epoch,
            seconds_until=secs,
            reset_label=format_local_reset_label(epoch, now=moment),
            countdown_label=format_countdown(secs),
            kind=kind,
            is_active=is_active,
            disabled=disabled,
            plan_label=plan_lbl,
            no_fable=no_fable,
        )
        minute_bucket = int(epoch // 60)
        existing = by_minute.get(minute_bucket)
        if existing is None or _event_dedup_priority(ev) > _event_dedup_priority(existing):
            by_minute[minute_bucket] = ev

    return list(by_minute.values()), live_fh_pct


def _claude_effective_fable_used_pct(row: Any, moment: float) -> float | None:
    """Effective weekly/Fable utilization (0..100) for Claude seat capacity."""
    expired = _expired_set(row)
    scoped_resets = {
        str(n).strip().lower(): r
        for n, r in (getattr(row, "scoped_resets_at", ()) or ())
        if n
    }

    def _is_rolled(key: str, label: str, reset_raw: Any) -> bool:
        if _is_window_expired(expired, key, label):
            return True
        ep = parse_reset_epoch(reset_raw, now=moment)
        return ep is not None and ep <= moment - PAST_RESET_SLACK_SECONDS

    fable_pct: float | None = None
    fable_reported = False
    for name, pct in getattr(row, "scoped_windows", ()) or ():
        s_name = str(name or "").strip()
        if not s_name.lower().startswith("fable"):
            continue
        val = _safe_pct(pct)
        if val is None:
            continue
        fable_reported = True
        fable_reset = scoped_resets.get(s_name.lower())
        if fable_reset is None:
            fable_reset = getattr(row, "seven_day_resets_at", None)
        if _is_rolled(s_name, s_name, fable_reset):
            fable_pct = 0.0
        else:
            fable_pct = val
        break

    sd_pct = _safe_pct(getattr(row, "seven_day_pct", None))
    sd_reported = sd_pct is not None
    if sd_reported and _is_rolled("seven_day", "7d", getattr(row, "seven_day_resets_at", None)):
        sd_pct = 0.0

    if fable_reported and sd_reported:
        assert fable_pct is not None and sd_pct is not None
        return max(fable_pct, sd_pct)
    if fable_reported:
        return fable_pct
    if sd_reported:
        return sd_pct

    fh_pct = _safe_pct(getattr(row, "five_hour_pct", None))
    if fh_pct is not None:
        if _is_rolled("five_hour", "5h", getattr(row, "five_hour_resets_at", None)):
            return 0.0
        return fh_pct
    return None


def _codex_effective_used_pct(row: Any, moment: float) -> float | None:
    """Effective weekly utilization (0..100) for live Codex seat capacity."""
    expired = _expired_set(row)
    attn_kind = str(getattr(row, "attention_kind", "") or "").strip().lower()
    attn_note = str(getattr(row, "attention_note", "") or "").strip().lower()

    sd_pct = _safe_pct(getattr(row, "seven_day_pct", None))
    fh_pct = _safe_pct(getattr(row, "five_hour_pct", None))

    if sd_pct is None and fh_pct is None:
        return None

    # A credit-depleted workspace is blocked even if weekly pct < 100%
    if attn_kind == "crit" and "out of credits" in attn_note:
        return 100.0

    if sd_pct is not None:
        sd_reset = getattr(row, "seven_day_resets_at", None)
        ep = parse_reset_epoch(sd_reset, now=moment)
        if _is_window_expired(expired, "seven_day", "7d") or (
            ep is not None and ep <= moment - PAST_RESET_SLACK_SECONDS
        ):
            return 0.0
        return sd_pct

    assert fh_pct is not None
    fh_reset = getattr(row, "five_hour_resets_at", None)
    ep = parse_reset_epoch(fh_reset, now=moment)
    if _is_window_expired(expired, "five_hour", "5h") or (
        ep is not None and ep <= moment - PAST_RESET_SLACK_SECONDS
    ):
        return 0.0
    return fh_pct


def _credit_used_pct(row: Any) -> float | None:
    sd = _safe_pct(getattr(row, "seven_day_pct", None))
    if sd is not None:
        return sd
    vals = [
        p
        for p in (
            _safe_pct(getattr(row, "five_hour_pct", None)),
            *(_safe_pct(pct) for _n, pct in (getattr(row, "scoped_windows", ()) or ())),
        )
        if p is not None
    ]
    return max(vals) if vals else None


def _build_recommendation(
    *,
    events: tuple[ResetEvent, ...],
    unlocks: tuple[ResetEvent, ...],
    perishable: tuple[ResetEvent, ...],
    credits: ResetCreditsSummary,
    capacity: FleetCapacitySummary,
    live_fh_by_key: Mapping[tuple[str, int], float | None],
    has_any_account: bool,
) -> FleetRecommendation | None:
    """Produce a deterministic, single-best recommendation across the 5 tiers."""
    # Tier 1: use_reset_credit — active or near-capped (>=85%) seat with usable > 0
    credit_candidates = [
        acct
        for acct in credits.accounts
        if acct.usable > 0
        and not acct.needs_relogin
        and (acct.is_active or (acct.used_pct is not None and acct.used_pct >= UNLOCK_USED_PCT))
    ]
    if credit_candidates:
        best_credit = min(
            credit_candidates,
            key=lambda a: (
                0 if a.is_active else 1,
                -(a.used_pct if a.used_pct is not None else -1.0),
                -a.usable,
                a.vendor,
                a.slot,
            ),
        )
        alias_s = _short_alias(best_credit.alias)
        pct_part = (
            f" ({best_credit.used_pct:.0f}%)"
            if best_credit.used_pct is not None
            else ""
        )
        noun = "credit" if best_credit.usable == 1 else "credits"
        v_name = vendor_label(best_credit.vendor)  # type: ignore[arg-type]
        headline = _fit_headline(
            f"Use {best_credit.usable}× reset {noun} on {alias_s}{pct_part}"
        )
        detail = (
            f"{v_name} seat {best_credit.alias} has {best_credit.usable} usable reset "
            f"{noun} right now ({best_credit.available} banked total)."
        )
        return FleetRecommendation(
            kind="use_reset_credit",
            headline=headline,
            detail=detail,
            target_vendor=best_credit.vendor,
            target_slot=best_credit.slot,
        )

    # Tier 2: burn_perishable — healthy seat with >=40% free expiring <=36h
    perishable_candidates: list[ResetEvent] = []
    for ev in perishable:
        if ev.free_pct is None or ev.free_pct < ADVISOR_PERISHABLE_MIN_FREE_PCT:
            continue
        if ev.seconds_until > ADVISOR_PERISHABLE_MAX_SECONDS:
            continue
        # Skip seats currently rate-limited on their 5h burst window
        fh = live_fh_by_key.get((ev.vendor, ev.slot))
        if fh is not None and fh >= 95.0:
            continue
        if not ev.is_active or ev.seconds_until <= ADVISOR_ACTIVE_URGENT_SECONDS:
            perishable_candidates.append(ev)

    if perishable_candidates:
        # Prefer standby seats (actionable switch) or urgently expiring seats, soonest first
        best_perish = min(
            perishable_candidates,
            key=lambda e: (
                0 if not e.is_active else 1,
                e.seconds_until,
                -(e.free_pct or 0.0),
                e.vendor,
                e.slot,
            ),
        )
        alias_s = _short_alias(best_perish.alias)
        free_int = int(round(best_perish.free_pct or 0.0))
        v_name = vendor_label(best_perish.vendor)  # type: ignore[arg-type]
        if best_perish.is_active:
            headline = _fit_headline(
                f"Burn {free_int}% free {best_perish.window} on {alias_s} ({best_perish.countdown_label} left)"
            )
        else:
            headline = _fit_headline(
                f"Use {alias_s}: {free_int}% {best_perish.window} expires in {best_perish.countdown_label}"
            )
        detail = (
            f"{v_name} seat {best_perish.alias} has {free_int}% unused "
            f"{best_perish.window} quota resetting {best_perish.reset_label} "
            f"(in {best_perish.countdown_label})."
        )
        return FleetRecommendation(
            kind="burn_perishable",
            headline=headline,
            detail=detail,
            target_vendor=best_perish.vendor,
            target_slot=best_perish.slot,
        )

    # Tier 3: unlock_soon — capped Fable/Codex seat unlocking <=6h while low free capacity (<=1.5)
    unlock_candidates: list[ResetEvent] = []
    for ev in unlocks:
        if ev.seconds_until > ADVISOR_UNLOCK_SOON_MAX_SECONDS:
            continue
        if ev.vendor == VENDOR_CLAUDE and capacity.claude_fable_free_seats <= ADVISOR_LOW_CAPACITY_SEATS:
            unlock_candidates.append(ev)
        elif ev.vendor == VENDOR_CODEX and capacity.codex_free_seats <= ADVISOR_LOW_CAPACITY_SEATS:
            unlock_candidates.append(ev)

    if unlock_candidates:
        best_unlock = min(
            unlock_candidates,
            key=lambda e: (e.resets_at_epoch, e.vendor, e.slot, e.window),
        )
        alias_s = _short_alias(best_unlock.alias)
        v_name = vendor_label(best_unlock.vendor)  # type: ignore[arg-type]
        if best_unlock.vendor == VENDOR_CLAUDE:
            free_s, total_s = capacity.claude_fable_free_seats, capacity.claude_fable_total_seats
        else:
            free_s, total_s = capacity.codex_free_seats, capacity.codex_total_seats
        headline = _fit_headline(
            f"{alias_s} ({best_unlock.window}) unlocks in {best_unlock.countdown_label} ({best_unlock.reset_label})"
        )
        detail = (
            f"{v_name} headroom is low ({free_s:.1f}/{total_s} seats); "
            f"{best_unlock.alias} resets {best_unlock.reset_label} to restore capacity."
        )
        return FleetRecommendation(
            kind="unlock_soon",
            headline=headline,
            detail=detail,
            target_vendor=best_unlock.vendor,
            target_slot=best_unlock.slot,
        )

    # Tier 4: relogin_capacity — >=2 Claude or Codex seats locked behind re-login
    c_locked = capacity.claude_locked_relogin_seats
    x_locked = capacity.codex_locked_relogin_seats
    total_locked = c_locked + x_locked
    if total_locked >= ADVISOR_RELOGIN_MIN_SEATS:
        if c_locked > 0 and x_locked > 0:
            target_vendor = (
                VENDOR_CLAUDE
                if c_locked > x_locked
                else (VENDOR_CODEX if x_locked > c_locked else None)
            )
            headline = _fit_headline(
                f"Re-login {total_locked} locked seats ({c_locked} Claude, {x_locked} Codex)"
            )
            detail = (
                f"{c_locked} Claude and {x_locked} Codex seats are locked behind "
                f"re-login; refresh them via bswap add / codex login to recover capacity."
            )
        elif c_locked >= ADVISOR_RELOGIN_MIN_SEATS:
            target_vendor = VENDOR_CLAUDE
            headline = _fit_headline(
                f"Re-login {c_locked} locked Claude seats to recover capacity"
            )
            detail = (
                f"{c_locked} Claude seats need re-authentication; run bswap add to "
                f"bring their weekly quota back online."
            )
        else:
            target_vendor = VENDOR_CODEX
            locked_credits = credits.total_available - credits.live_available
            credit_clause = (
                f" (holding {locked_credits}× banked reset credits)"
                if locked_credits > 0
                else ""
            )
            headline = _fit_headline(
                f"Re-login {x_locked} locked Codex seats to recover capacity"
            )
            detail = (
                f"{x_locked} Codex seats need re-authentication{credit_clause}; "
                f"run codex login to bring them back online."
            )
        return FleetRecommendation(
            kind="relogin_capacity",
            headline=headline,
            detail=detail,
            target_vendor=target_vendor,
            target_slot=None,
        )

    # Empty fleet: no recommendation
    if not has_any_account and not events and credits.total_available == 0:
        return None

    # Tier 5: healthy — concise summary of ready Fable + Codex seats and next unlock
    summary_parts: list[str] = []
    detail_parts: list[str] = []
    if capacity.claude_fable_total_seats > 0:
        summary_parts.append(
            f"{capacity.claude_fable_free_seats:.1f}/{capacity.claude_fable_total_seats} Fable"
        )
        detail_parts.append(
            f"Claude Fable {capacity.claude_fable_free_seats:.2f}/{capacity.claude_fable_total_seats} seats ready"
        )
    if capacity.codex_total_seats > 0:
        summary_parts.append(
            f"{capacity.codex_free_seats:.1f}/{capacity.codex_total_seats} Codex"
        )
        detail_parts.append(
            f"Codex {capacity.codex_free_seats:.2f}/{capacity.codex_total_seats} seats ready"
        )

    next_unlock = unlocks[0] if unlocks else None
    if summary_parts:
        base_head = "Fleet healthy: " + " · ".join(summary_parts)
        if next_unlock is not None:
            candidate_head = f"{base_head} · next {next_unlock.countdown_label}"
            headline = _fit_headline(
                candidate_head if len(candidate_head) <= HEADLINE_MAX_CHARS else base_head
            )
        else:
            headline = _fit_headline(base_head)
    elif total_locked == 1:
        which = "Claude" if c_locked else "Codex"
        headline = _fit_headline(f"1 {which} seat needs re-login; no live seats ready")
        detail_parts.append(f"1 {which} seat is waiting on re-authentication")
    else:
        headline = "Fleet healthy"
        detail_parts.append("All monitored seats are within healthy limits")

    if next_unlock is not None:
        detail_parts.append(
            f"next unlock is {next_unlock.alias} ({next_unlock.window}) in "
            f"{next_unlock.countdown_label} ({next_unlock.reset_label})"
        )
    if credits.live_available > 0:
        detail_parts.append(
            f"{credits.live_available}× live reset credits banked"
        )

    detail = "; ".join(detail_parts) + "."
    return FleetRecommendation(
        kind="healthy",
        headline=headline,
        detail=detail,
        target_vendor=None,
        target_slot=None,
    )


# ---------------------------------------------------------------------------
# Public radar builder
# ---------------------------------------------------------------------------


def build_fleet_reset_radar(
    accounts: Iterable[Any],
    quota_rows: Iterable[Any],
    *,
    account_notes: Mapping[int, str] | None = None,
    now: float | None = None,
    fable_guard: bool = True,
) -> FleetResetRadar:
    """Build the unified :class:`FleetResetRadar` across Claude and Codex seats.

    Parameters
    ----------
    accounts:
        Claude :class:`~cc_usage_widget.contracts.AccountRow` items (or duck-typed rows).
    quota_rows:
        Codex quota rows (:class:`~cc_usage_widget.contracts.AccountRow` with
        ``vendor="codex"`` or duck-typed ``QuotaRow`` objects).
    account_notes:
        Optional mapping of ``slot -> standing note`` (e.g. ``relogin``,
        ``fetch-failing``) for Claude slots.
    now:
        Optional POSIX timestamp override for deterministic evaluation.
    fable_guard:
        When ``True`` and the Claude fleet contains Fable-capable seats,
        non-Fable Team/Pro seats are flagged ``no_fable=True`` and excluded from
        Fable capacity / unlocks / perishable metrics while keeping their ``7d``
        resets visible in ``events`` as ``upcoming``.
    """
    moment = time.time() if now is None else float(now)
    notes: Mapping[int, str] = account_notes if isinstance(account_notes, Mapping) else {}

    # Partition & deduplicate incoming rows by (vendor, slot)
    claude_rows: list[Any] = []
    codex_rows: list[Any] = []
    seen_keys: set[tuple[str, int]] = set()

    for default_vendor, iterable in ((VENDOR_CLAUDE, accounts or ()), (VENDOR_CODEX, quota_rows or ())):
        for row in iterable:
            if row is None:
                continue
            vendor = str(getattr(row, "vendor", default_vendor) or default_vendor).strip().lower()
            if vendor not in (VENDOR_CLAUDE, VENDOR_CODEX):
                vendor = default_vendor
            slot = _safe_slot(row, default=0)
            key = (vendor, slot)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            if vendor == VENDOR_CLAUDE:
                claude_rows.append(row)
            else:
                codex_rows.append(row)

    has_fable_fleet = bool(fable_guard) and fleet_has_fable_accounts(claude_rows, sentinels=notes)

    all_events: list[ResetEvent] = []
    credit_accounts: list[ResetCreditAccount] = []
    live_fh_by_key: dict[tuple[str, int], float | None] = {}

    claude_fable_free = 0.0
    claude_fable_total = 0
    claude_locked_relogin = 0

    codex_free = 0.0
    codex_total = 0
    codex_locked_relogin = 0

    total_credits_available = 0
    live_credits_available = 0
    total_credits_usable = 0

    # --- Process Claude rows ---
    for row in claude_rows:
        slot = _safe_slot(row, default=0)
        alias = _safe_alias(row, VENDOR_CLAUDE, slot)
        is_active = bool(getattr(row, "is_active", False))
        disabled = bool(getattr(row, "disabled", False))
        switchable = bool(getattr(row, "switchable", True))
        raw_plan = getattr(row, "plan_type", None)
        plan_lbl = _format_plan_label(raw_plan) or str(getattr(row, "plan_label", "") or "").strip()

        needs_relogin, is_unavailable = _row_health_state(row, VENDOR_CLAUDE, slot, notes)
        if needs_relogin:
            claude_locked_relogin += 1

        # Optional banked reset credits on Claude rows (if ever reported)
        raw_avail = _safe_nonneg_int(getattr(row, "reset_credits_available", None))
        raw_usable = _safe_nonneg_int(getattr(row, "reset_credits_usable", None))
        if (raw_avail or 0) > 0 or (raw_usable or 0) > 0:
            eff_avail = max(raw_avail or 0, raw_usable or 0)
            is_live_credit = not disabled and not needs_relogin and not is_unavailable and switchable
            eff_usable = min(eff_avail, raw_usable or 0) if is_live_credit else 0
            total_credits_available += eff_avail
            if is_live_credit:
                live_credits_available += eff_avail
                total_credits_usable += eff_usable
            credit_accounts.append(
                ResetCreditAccount(
                    vendor=VENDOR_CLAUDE,
                    slot=slot,
                    alias=alias,
                    available=eff_avail,
                    usable=eff_usable,
                    used_pct=_credit_used_pct(row),
                    is_active=is_active,
                    needs_relogin=needs_relogin,
                )
            )

        if needs_relogin or is_unavailable:
            continue

        no_fable = bool(has_fable_fleet and not row_has_fable_quota(row) and row_lacks_fable_quota(row))

        row_events, live_fh = _extract_row_events(
            row,
            vendor=VENDOR_CLAUDE,
            slot=slot,
            alias=alias,
            plan_lbl=plan_lbl,
            is_active=is_active,
            disabled=disabled,
            no_fable=no_fable,
            non_live=not switchable,
            moment=moment,
        )
        all_events.extend(row_events)
        live_fh_by_key[(VENDOR_CLAUDE, slot)] = live_fh

        if not disabled and switchable and not no_fable:
            eff_used = _claude_effective_fable_used_pct(row, moment)
            if eff_used is not None:
                claude_fable_total += 1
                claude_fable_free += max(0.0, min(1.0, (100.0 - eff_used) / 100.0))

    # --- Process Codex rows ---
    for row in codex_rows:
        slot = _safe_slot(row, default=0)
        alias = _safe_alias(row, VENDOR_CODEX, slot)
        is_active = bool(getattr(row, "is_active", False))
        disabled = bool(getattr(row, "disabled", False))
        is_live = _is_live_codex_row(row, slot)
        raw_plan = getattr(row, "plan_type", None)
        plan_lbl = _format_plan_label(raw_plan) or str(getattr(row, "plan_label", "") or "").strip()

        needs_relogin, is_unavailable = _row_health_state(row, VENDOR_CODEX, slot, notes)
        if needs_relogin and is_live:
            codex_locked_relogin += 1

        # Banked reset credits (live vs relogin/disabled/non-live)
        raw_avail = _safe_nonneg_int(getattr(row, "reset_credits_available", None))
        raw_usable = _safe_nonneg_int(getattr(row, "reset_credits_usable", None))
        if (raw_avail or 0) > 0 or (raw_usable or 0) > 0:
            eff_avail = max(raw_avail or 0, raw_usable or 0)
            is_live_credit = is_live and not disabled and not needs_relogin and not is_unavailable
            eff_usable = min(eff_avail, raw_usable or 0) if is_live_credit else 0
            total_credits_available += eff_avail
            if is_live_credit:
                live_credits_available += eff_avail
                total_credits_usable += eff_usable
            credit_accounts.append(
                ResetCreditAccount(
                    vendor=VENDOR_CODEX,
                    slot=slot,
                    alias=alias,
                    available=eff_avail,
                    usable=eff_usable,
                    used_pct=_credit_used_pct(row),
                    is_active=is_active,
                    needs_relogin=needs_relogin,
                )
            )

        if needs_relogin or is_unavailable:
            continue

        row_events, live_fh = _extract_row_events(
            row,
            vendor=VENDOR_CODEX,
            slot=slot,
            alias=alias,
            plan_lbl=plan_lbl,
            is_active=is_active,
            disabled=disabled,
            no_fable=False,
            non_live=not is_live,
            moment=moment,
        )
        all_events.extend(row_events)
        live_fh_by_key[(VENDOR_CODEX, slot)] = live_fh

        if is_live and not disabled:
            eff_used = _codex_effective_used_pct(row, moment)
            if eff_used is not None:
                codex_total += 1
                codex_free += max(0.0, min(1.0, (100.0 - eff_used) / 100.0))

    # Chronological unified sorting across Claude + Codex
    all_events.sort(key=lambda e: (e.resets_at_epoch, e.vendor, e.slot, e.window))
    events_tuple = tuple(all_events)
    unlocks_tuple = tuple(
        e for e in events_tuple if e.kind == "unlock" and not e.disabled and not e.no_fable
    )
    perishable_tuple = tuple(
        e for e in events_tuple if e.kind == "perishable" and not e.disabled and not e.no_fable
    )

    # Sort banked reset-credit accounts: usable first, then live banked, then relogin
    credit_accounts.sort(
        key=lambda a: (-a.usable, 1 if a.needs_relogin else 0, -a.available, a.vendor, a.slot)
    )
    credits_summary = ResetCreditsSummary(
        total_available=total_credits_available,
        live_available=live_credits_available,
        total_usable=total_credits_usable,
        accounts=tuple(credit_accounts),
    )

    # Perishable seats <48h (at most 1.0 seat-eq per distinct account)
    perishable_by_acct: dict[tuple[str, int], ResetEvent] = {}
    for ev in perishable_tuple:
        acct_key = (ev.vendor, ev.slot)
        prev = perishable_by_acct.get(acct_key)
        if prev is None or _window_specificity(ev.window, ev.vendor) > _window_specificity(
            prev.window, prev.vendor
        ):
            perishable_by_acct[acct_key] = ev
    perishable_seats = sum(
        max(0.0, min(1.0, (ev.free_pct or 0.0) / 100.0))
        for ev in perishable_by_acct.values()
    )

    claude_next_unlock = next((e for e in unlocks_tuple if e.vendor == VENDOR_CLAUDE), None)
    codex_next_unlock = next((e for e in unlocks_tuple if e.vendor == VENDOR_CODEX), None)

    capacity_summary = FleetCapacitySummary(
        claude_fable_free_seats=round(claude_fable_free, 4),
        claude_fable_total_seats=claude_fable_total,
        claude_locked_relogin_seats=claude_locked_relogin,
        claude_next_unlock=claude_next_unlock,
        codex_free_seats=round(codex_free, 4),
        codex_total_seats=codex_total,
        codex_locked_relogin_seats=codex_locked_relogin,
        codex_next_unlock=codex_next_unlock,
        perishable_seats_48h=round(perishable_seats, 4),
    )

    has_any_account = bool(claude_rows or codex_rows)
    recommendation = _build_recommendation(
        events=events_tuple,
        unlocks=unlocks_tuple,
        perishable=perishable_tuple,
        credits=credits_summary,
        capacity=capacity_summary,
        live_fh_by_key=live_fh_by_key,
        has_any_account=has_any_account,
    )

    return FleetResetRadar(
        generated_at=moment,
        events=events_tuple,
        unlocks=unlocks_tuple,
        perishable=perishable_tuple,
        credits=credits_summary,
        capacity=capacity_summary,
        recommendation=recommendation,
    )
