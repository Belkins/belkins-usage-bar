"""Transition notifications — macOS Notification Center and optional Telegram.

Roadmap item 7. The widget already knows, twice a minute, everything that has
just changed about every account; until now it kept that to itself and the
operator learned about a wall by looking. This module turns a *change between
two snapshots* into at most one notification, ever.

Three separate jobs, deliberately not entangled
-----------------------------------------------

1. **Detection is pure.** :func:`detect` (and :meth:`Notifier.observe`, which
   is a thin alias of it) takes two :class:`UiSnapshot`-shaped objects and
   returns :class:`Event` objects. It reads no file, holds no state, sends
   nothing, and consults no clock. Every transition rule in this module can
   therefore be asserted with two hand-built snapshots and no fixtures at all.
2. **Once-per-transition is a ledger.** :class:`Notifier.notify` is the impure
   half: it filters the detected events through a small JSON ledger under
   ``WIDGET_HOME`` so a condition that stays true for six hours produces one
   notification rather than 360, and so a restart does not replay everything
   the operator already saw. A key is released from the ledger when its
   condition stops being true — which is what lets a second wall, days later,
   notify again — and a per-key throttle covers a condition that flaps.
3. **Sending is somebody else's thread.** Both senders are injected, and the
   default dispatcher hands the batch to a short-lived daemon thread. A
   Telegram request that hangs for its full timeout must never delay the
   worker's tick (SPEC 2.3 is about the AppKit thread; this is the same
   argument one thread down).

What is deliberately NOT here
-----------------------------

* No polling, no timer, no thread of its own beyond the per-batch dispatcher.
  The worker already has a cadence; this module is called from it.
* No numbers of its own. Every percentage, reset string and note in a message
  is passed through verbatim from the snapshot (SPEC 4.3). A window whose
  reported reset instant has already passed (``expired_windows``) is skipped
  outright rather than notified about, because its percentage describes a
  window that has ENDED.
* No secret anywhere but one file. The Telegram bot token lives only in
  ``WIDGET_HOME/notify.json`` (0600, refused if group- or world-readable), is
  read into one URL, and is never logged, never rendered, and never accepted
  from ``argv`` — ``setup`` takes the NAME of an environment variable, so the
  token cannot land in a shell history or a process listing.

CLI::

    python -m cc_usage_widget.notify setup --telegram-token-from-env TELEGRAM_BOT_TOKEN --chat-id 12345
    python -m cc_usage_widget.notify test
    python -m cc_usage_widget.notify status
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

from .contracts import (
    ATTENTION_PCT,
    ATTENTION_REARM_SECONDS,
    NOTIFY_EXPIRING_SECONDS,
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    WIDGET_HOME,
    format_pct,
    format_tokens,
    vendor_label,
)

__all__ = [
    "Event",
    "Notifier",
    "TelegramCredentials",
    "TelegramSender",
    "MacSender",
    "NOTIFY_STATE_PATH",
    "NOTIFY_CREDENTIALS_PATH",
    "detect",
    "load_telegram_credentials",
    "build_notifier",
    "main",
]


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

NOTIFY_STATE_PATH: Final[Path] = WIDGET_HOME / "notify_state.json"
"""The once-per-transition ledger. Not a source of truth: deleting it costs at
most one duplicate notification per still-standing condition."""

NOTIFY_CREDENTIALS_PATH: Final[Path] = WIDGET_HOME / "notify.json"
"""Telegram bot token + chat id, 0600. Written only by ``notify setup``."""

RECOVERY_PCT: Final[float] = 50.0
"""A window is "back" once it drops below this after having been at or over the
alert threshold. Half the window is a wide enough gap that a percentage
jittering around the threshold cannot produce a wall/back/wall sequence."""

THROTTLE_SECONDS: Final[float] = 60.0
"""Minimum gap between two notifications carrying the same key, even when the
condition genuinely went false and true again in between."""

TELEGRAM_TIMEOUT_SECONDS: Final[float] = 10.0
TELEGRAM_API_ROOT: Final[str] = "https://api.telegram.org"

SEVERITY_INFO: Final[str] = "info"
SEVERITY_WARN: Final[str] = "warn"
SEVERITY_CRIT: Final[str] = "crit"

_LEDGER_VERSION: Final[int] = 1
_CREDENTIALS_VERSION: Final[int] = 1
_FILE_MODE: Final[int] = 0o600

_REDACTED: Final[str] = "<redacted>"

# Window keys as `AccountRow.expired_windows` spells them, paired with the word
# a human reads. Scoped windows carry their own reported name and use it for
# both, so a second scoped window appearing tomorrow needs no change here.
_FIVE_HOUR_KEY: Final[str] = "five_hour"
_SEVEN_DAY_KEY: Final[str] = "seven_day"
_WINDOW_LABELS: Final[Mapping[str, str]] = {
    _FIVE_HOUR_KEY: "5h",
    _SEVEN_DAY_KEY: "weekly",
}

SCOPE_SENTINEL: Final[str] = "sentinel"
SCOPE_AUDIT: Final[str] = "audit"
SCOPE_COST: Final[str] = "cost"
"""Scopes for the conditions that are facts about the SNAPSHOT rather than
about one account row. Every other scope is a row key. ``cost`` is observed
only by a snapshot carrying a COMPLETE cost breakdown: a tick still indexing
(or with cost tracking off) cannot say a model stopped being unpriced."""

SPEND_CAP_PCT: Final[float] = 100.0
"""An extra-usage spend limit at or past this is ``crit``: the next request is
billed past the limit the operator set, or refused."""

LEDGER_MAX_AGE_SECONDS: Final[float] = 7 * 24 * 3600.0
"""Backstop on a held key whose scope has not been observed since — a Codex
account removed from the registry, a claude-swap slot deleted. Without it the
ledger would keep one entry per account that ever existed."""

APP_TITLE: Final[str] = "Usage Bar"


def _log_noop(_message: str) -> None:
    """Default logger: silence. The worker injects the real one."""


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that just became true, ready to be sent.

    ``key`` is the identity the ledger deduplicates on and is stable across
    restarts: it is built from vendor + slot + alias + window + kind, never
    from a wording, so rephrasing a message can never resurrect a notification
    the operator has already seen (the mirror of the ``attention_kind``
    lesson in ``contracts.AccountRow``).

    "Never from a wording" is load-bearing rather than decorative, and the two
    standing conditions are where it is easiest to lose: an ``attention_note``
    reads ``relogin in 1d 4h`` and a claude-swap sentinel note counts down the
    same way, so a key built from either text is a **new key every tick** and
    the ledger — which is doing its job perfectly — suppresses nothing. Their
    keys are therefore ``attention:<row>:<attention_kind>`` and
    ``sentinel:<slot>:<sentinel kind>``; the words ride in :attr:`subtitle`,
    which is the field that is actually rendered.
    """

    key: str
    kind: str
    """``threshold`` | ``capped`` | ``back`` | ``attention`` | ``audit`` |
    ``sentinel`` | ``expiring`` | ``spend`` | ``unpriced`` | ``reset-usable``.
    Machine-stable; the wording is not."""
    severity: str
    title: str
    subtitle: str = ""
    message: str = ""
    scope: str = ""
    """What this key is a fact ABOUT: a row key, ``sentinel`` or ``audit``.

    The ledger releases a key when its condition goes false — but "the source
    errored and published no rows this tick" is not the same fact as "the
    account is fine now". Scope is how the two are told apart: a key whose
    scope was not observed at all is held, not released, so one failed Codex
    poll cannot re-announce a wall the operator has already seen."""

    @property
    def text(self) -> str:
        """One line, for a transport with no subtitle field (Telegram)."""
        return " · ".join(part for part in (self.title, self.subtitle, self.message) if part)


# ---------------------------------------------------------------------------
# Pure transition detection
# ---------------------------------------------------------------------------


def _row_key(row: Any) -> str:
    """Identity of an account row across snapshots: vendor + slot + alias.

    Slot alone is not enough — Codex's live rows use negative slots assigned by
    registry order, and a Claude slot is only unique within claude-swap. Alias
    alone is not enough either: it can be empty.
    """
    return f"{getattr(row, 'vendor', '?')}:{getattr(row, 'slot', '?')}:{getattr(row, 'alias', '')}"


def _display_name(row: Any) -> str:
    """What a human calls this row: alias, else the email's local part, else the slot."""
    alias = getattr(row, "alias", "") or ""
    if alias:
        return alias
    email = getattr(row, "email", "") or ""
    local = email.split("@", 1)[0]
    return local or f"slot {getattr(row, 'slot', '?')}"


def _windows(row: Any) -> tuple[tuple[str, str, float | None, str | None], ...]:
    """``(window_key, label, pct, resets_at)`` for every window this row reports.

    Windows listed in ``expired_windows`` are dropped: their percentage
    describes a window that has already ended, and notifying "87% weekly" about
    a week that is over would be exactly the invented-currency failure SPEC 4.3
    forbids.
    """
    expired = set(getattr(row, "expired_windows", ()) or ())
    out: list[tuple[str, str, float | None, str | None]] = []
    if _FIVE_HOUR_KEY not in expired:
        out.append(
            (
                _FIVE_HOUR_KEY,
                _WINDOW_LABELS[_FIVE_HOUR_KEY],
                getattr(row, "five_hour_pct", None),
                getattr(row, "five_hour_resets_at", None),
            )
        )
    if _SEVEN_DAY_KEY not in expired:
        out.append(
            (
                _SEVEN_DAY_KEY,
                _WINDOW_LABELS[_SEVEN_DAY_KEY],
                getattr(row, "seven_day_pct", None),
                getattr(row, "seven_day_resets_at", None),
            )
        )
    scoped_resets = dict(getattr(row, "scoped_resets_at", ()) or ())
    for name, pct in getattr(row, "scoped_windows", ()) or ():
        if name in expired:
            continue
        out.append((name, name, pct, scoped_resets.get(name)))
    return tuple(out)


def _rows(snapshot: Any) -> dict[str, Any]:
    """Every account row a snapshot carries, keyed by :func:`_row_key`.

    ``accounts`` and ``quota_rows`` are separate fields on purpose (a
    pseudo-account must never reach a switch path), but for notification
    purposes they are the same thing: a window with a percentage and a reset.
    """
    out: dict[str, Any] = {}
    if snapshot is None:
        return out
    for row in tuple(getattr(snapshot, "accounts", ()) or ()) + tuple(
        getattr(snapshot, "quota_rows", ()) or ()
    ):
        out[_row_key(row)] = row
    return out


def _pct_of(row: Any, window_key: str) -> float | None:
    for key, _label, pct, _resets in _windows(row):
        if key == window_key:
            return pct
    return None


def _reset_suffix(resets_at: str | None) -> str:
    """``"resets 09:00"`` from a verbatim reset string, or ``""``.

    The string is passed through exactly as the source gave it (SPEC 4.3); this
    function only decides whether to mention it at all.
    """
    text = (resets_at or "").strip()
    return f"resets {text}" if text else ""


def _threshold_of(settings: Mapping[str, Any] | None) -> float:
    raw = (settings or {}).get(
        "notification_threshold_pct", SETTINGS_DEFAULTS["notification_threshold_pct"]
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(SETTINGS_DEFAULTS["notification_threshold_pct"])
    return min(max(value, RECOVERY_PCT), ATTENTION_PCT)


def _unpriced_floor_of(settings: Mapping[str, Any] | None) -> int:
    """``unpriced_alert_min_tokens`` as a non-negative int; 0 means off."""
    default = int(SETTINGS_DEFAULTS["unpriced_alert_min_tokens"])
    raw = (settings or {}).get("unpriced_alert_min_tokens", default)
    if isinstance(raw, bool):
        return default
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return default


def _pct_events(
    previous: Any, current: Any, *, threshold: float
) -> list[Event]:
    """Threshold crossings, walls, and recoveries — the percentage rules.

    Every one of them is an EDGE: it needs both a previous and a current
    reading. A row seen for the first time (a fresh start, a newly enabled
    account) therefore produces nothing, which is deliberate — replaying the
    current state of four already-capped accounts at every launch is how a
    notification feature gets muted forever.
    """
    events: list[Event] = []
    old_rows = _rows(previous)
    for key, row in _rows(current).items():
        old = old_rows.get(key)
        name = _display_name(row)
        vendor = vendor_label(getattr(row, "vendor", "claude"))
        for window_key, label, pct, resets_at in _windows(row):
            if pct is None:
                continue
            # ONE guard, deliberately: "there is no previous reading for this
            # window" is a single fact, whether the row is new, the window is
            # new, or this is the first snapshot ever. Every percentage rule
            # below is an edge and cannot be evaluated without it.
            was = _pct_of(old, window_key) if old is not None else None
            if was is None:
                continue
            title = f"{vendor} {name}"
            if was < ATTENTION_PCT <= pct:
                events.append(
                    Event(
                        key=f"capped:{key}:{window_key}",
                        kind="capped",
                        severity=SEVERITY_CRIT,
                        scope=key,
                        title=title,
                        subtitle=f"{label} {format_pct(pct)}",
                        message=_reset_suffix(resets_at) or "window exhausted",
                    )
                )
            elif was < threshold <= pct:
                events.append(
                    Event(
                        key=f"threshold:{key}:{window_key}",
                        kind="threshold",
                        severity=SEVERITY_WARN,
                        scope=key,
                        title=title,
                        subtitle=f"{label} {format_pct(pct)}",
                        message=f"crossed {threshold:g}%",
                    )
                )
            elif was >= threshold and pct < RECOVERY_PCT:
                events.append(
                    Event(
                        key=f"back:{key}:{window_key}",
                        kind="back",
                        severity=SEVERITY_INFO,
                        scope=key,
                        title=title,
                        subtitle=f"{label} {format_pct(pct)}",
                        message=f"back: {name} {format_pct(pct)} {label}",
                    )
                )
    return events


def _standing_conditions(
    snapshot: Any,
    *,
    now: float | None = None,
    threshold: float | None = None,
    unpriced_min_tokens: int | None = None,
) -> dict[str, Event]:
    """Conditions that are simply TRUE of one snapshot, keyed by ledger key.

    Unlike the percentage rules these need no history: a warn-level
    ``attention_note`` is a fact about the row in front of us. The ledger, not
    the previous snapshot, is what makes them fire once — which is also what
    makes them survive a restart without re-announcing a standing fault.

    The keyword arguments switch on the rules that need an input the snapshot
    does not carry: *now* the pre-expiry rule, *threshold* the spend rule,
    *unpriced_min_tokens* (> 0) the unpriced-model rule. Left ``None`` each rule
    is simply absent, so this stays pure and clock-free for a caller that does
    not ask.
    """
    out: dict[str, Event] = {}
    if snapshot is None:
        return out
    if now is not None:
        out.update(_expiring_conditions(snapshot, now=now))
    if threshold is not None:
        out.update(_spend_conditions(snapshot, threshold=threshold))
    if unpriced_min_tokens:
        out.update(_unpriced_conditions(snapshot, min_tokens=unpriced_min_tokens))
    out.update(_reset_usable_conditions(snapshot))

    for key, row in _rows(snapshot).items():
        note = (getattr(row, "attention_note", "") or "").strip()
        kind = (getattr(row, "attention_kind", "") or "").strip()
        if not note or kind not in (SEVERITY_WARN, SEVERITY_CRIT):
            continue
        vendor = vendor_label(getattr(row, "vendor", "claude"))
        # Keyed on the row and the VERDICT's kind, never on the note's words.
        # `attention_note` carries a live countdown ("relogin in 1d 4h"), so a
        # key built from it is a different key on every tick and the operator
        # is re-notified about one standing fault every 60 seconds. The wording
        # still travels - in `subtitle`, which is what gets rendered.
        ledger_key = f"attention:{key}:{kind}"
        out[ledger_key] = Event(
            key=ledger_key,
            kind="attention",
            severity=kind,
            scope=key,
            title=f"{vendor} {_display_name(row)}",
            subtitle=note,
            message="needs attention",
        )

    notes = getattr(snapshot, "account_notes", None) or {}
    kinds = getattr(snapshot, "account_note_kinds", None) or {}
    if isinstance(notes, Mapping):
        for slot, note in notes.items():
            text = str(note or "").strip()
            if not text:
                continue
            sentinel = str(kinds.get(slot, "") or "").strip() if isinstance(kinds, Mapping) else ""
            # Same rule as the attention keys above: the slot plus the sentinel
            # KIND. A claude-swap sentinel note is rephrased as its countdown
            # ticks down, and falling back to the text (as this once did) turns
            # one standing fault into a new notification per tick. A slot whose
            # source reported no kind collapses to one key for that slot, which
            # is the conservative half of the trade: one announcement, not one
            # per wording.
            key = f"sentinel:{slot}:{sentinel}"
            out[key] = Event(
                key=key,
                kind="sentinel",
                severity=SEVERITY_WARN,
                scope=SCOPE_SENTINEL,
                title=f"{APP_TITLE} · slot {slot}",
                subtitle=text,
                message="",
            )

    # B1 adds ``UiSnapshot.audit_note``; read it defensively so this module
    # works on a build that does not have it yet.
    audit = (getattr(snapshot, "audit_note", None) or "").strip()
    if audit:
        key = f"audit:{audit}"
        out[key] = Event(
            key=key,
            kind="audit",
            severity=SEVERITY_WARN,
            scope=SCOPE_AUDIT,
            title=f"{APP_TITLE} · self-audit",
            subtitle=audit,
            message="",
        )
    return out


def _format_expiry(epoch: float) -> str:
    """``"Sep 20 11:13"`` (local time), or ``""`` for an absurd epoch."""
    try:
        when = dt.datetime.fromtimestamp(float(epoch))
    except (OSError, OverflowError, TypeError, ValueError):
        return ""
    return f"{when:%b} {when.day} {when:%H:%M}"


def _expiring_conditions(snapshot: Any, *, now: float) -> dict[str, Event]:
    """``expiring:<row>`` — a Codex login inside :data:`NOTIFY_EXPIRING_SECONDS`.

    Lifecycle of the ledger key (CX-6):

    * **set by** the first tick on which ``0 < credential_expires_at - now <
      NOTIFY_EXPIRING_SECONDS`` while the row carries an ``info`` attention (the
      dim ``relogin in …`` countdown) and no ``warn``/``crit`` one;
    * **cleared by** the condition going false: ``credential_expires_at`` moves
      later (a refresh or a relogin wrote a new token), the token actually
      expires (the row then carries the ``warn`` ``relogin`` verdict, whose own
      ``attention:`` key takes over), or any ``warn``/``crit`` note appears;
    * **persisted** in ``notify_state.json`` like every other key, so a restart
      inside the window does not announce it twice; **aged** by the ledger's
      unobserved-scope backstop only;
    * **producer off** (no ``credential_expires_at`` on the row — the field is
      read with ``getattr`` because it lands from lane codex-credentials):
      nothing fires.

    Why the ``info`` gate rather than "no note at all": the live source
    rotates a token itself inside ``REFRESH_PROACTIVE_SECONDS`` (24 h, the same
    width as this window) and suppresses its countdown whenever it expects to
    manage that (a healthy refresh, or the desktop app's own login). A row
    with no countdown is therefore one the source is about to renew, and
    announcing it would notify once per account per token lifetime about a
    login nobody has to touch. Kind-based, never wording-based.
    """
    out: dict[str, Event] = {}
    for key, row in _rows(snapshot).items():
        if getattr(row, "vendor", "") != VENDOR_CODEX:
            continue
        expires = getattr(row, "credential_expires_at", None)
        if expires is None:
            continue
        try:
            left = float(expires) - float(now)
        except (TypeError, ValueError):
            continue
        if not 0.0 < left < NOTIFY_EXPIRING_SECONDS:
            continue
        kind = (getattr(row, "attention_kind", "") or "").strip()
        if kind != SEVERITY_INFO:
            continue
        ledger_key = f"expiring:{key}"
        when = _format_expiry(float(expires))
        out[ledger_key] = Event(
            key=ledger_key,
            kind="expiring",
            severity=SEVERITY_WARN,
            scope=key,
            title=f"{vendor_label(VENDOR_CODEX)} {_display_name(row)}",
            subtitle=f"login expires {when}" if when else "login expires within a day",
            message="Log in again from the menu",
        )
    return out


def _money(amount: float, currency: str) -> str:
    """``480.00`` + ``USD`` -> ``"$480.00"``; any other code rides verbatim
    (``"480.00 EUR"``). The figure itself is the source's, never recomputed."""
    if currency.upper() == "USD":
        return f"${amount:,.2f}"
    return f"{amount:,.2f} {currency}".rstrip()


def _spend_conditions(snapshot: Any, *, threshold: float) -> dict[str, Event]:
    """``spend:<row>:threshold`` / ``spend:<row>:capped`` — extra-usage spend.

    Reads ``AccountRow.spend_*`` (lane swap-forensics; ``getattr`` because the
    fields land from that branch). A row with no ``spend_limit`` or no
    ``spend_pct`` is a row with no spend limit, and says nothing. At or past
    :data:`SPEND_CAP_PCT` only the ``capped`` key stands, so a jump straight to
    100 % is one ``crit`` rather than a warn and a crit in the same batch;
    :func:`retained_keys` holds the threshold key too while capped. Released
    when the percentage drops back under (a new billing period), which re-arms
    both.
    """
    out: dict[str, Event] = {}
    for key, row in _rows(snapshot).items():
        limit = getattr(row, "spend_limit", None)
        pct = getattr(row, "spend_pct", None)
        used = getattr(row, "spend_used", None)
        if limit is None or pct is None:
            continue
        try:
            pct_value = float(pct)
        except (TypeError, ValueError):
            continue
        if pct_value >= SPEND_CAP_PCT:
            level, severity, message = "capped", SEVERITY_CRIT, "extra-usage limit reached"
        elif pct_value >= threshold:
            level, severity, message = "threshold", SEVERITY_WARN, f"crossed {threshold:g}% of the extra-usage limit"
        else:
            continue
        currency = str(getattr(row, "spend_currency", "") or "")
        try:
            figures = (
                f"{_money(float(used), currency)} / {_money(float(limit), currency)}"
                if used is not None
                else f"limit {_money(float(limit), currency)}"
            )
        except (TypeError, ValueError):
            continue
        ledger_key = f"spend:{key}:{level}"
        out[ledger_key] = Event(
            key=ledger_key,
            kind="spend",
            severity=severity,
            scope=key,
            title=f"{vendor_label(getattr(row, 'vendor', 'claude'))} {_display_name(row)}",
            subtitle=f"extra usage {figures} ({format_pct(pct_value)})",
            message=message,
        )
    return out


def _unpriced_buckets(snapshot: Any) -> list[tuple[str, tuple[str, ...], int]]:
    """``(vendor, raw model names, today's tokens)`` per unpriced bucket.

    Empty unless the snapshot carries a COMPLETE breakdown: while the first
    index is still filling in, today's figure is a partial count and the
    alert would fire on a number that is still growing for a reason other
    than use.
    """
    cost = getattr(snapshot, "cost", None)
    if cost is None or getattr(cost, "is_partial", True):
        return []
    out: list[tuple[str, tuple[str, ...], int]] = []
    for model_row in getattr(cost, "by_model", ()) or ():
        if not getattr(model_row, "is_unknown", False):
            continue
        names = tuple(name for name in (getattr(model_row, "raw_models", ()) or ()) if name)
        if not names:
            continue
        try:
            tokens = int(getattr(model_row, "total_tokens", 0) or 0)
        except (TypeError, ValueError):
            continue
        out.append((str(getattr(model_row, "vendor", "") or ""), names, tokens))
    return out


def _unpriced_conditions(snapshot: Any, *, min_tokens: int) -> dict[str, Event]:
    """``unpriced:<model>`` — a model with no published rate, over the floor.

    ``by_model`` is today, and the rollup folds every unrecognised model of one
    vendor into ONE bucket, so a bucket with two names cannot say how its
    tokens split. The message therefore quotes the bucket's figure as the
    bucket's ("between them"), never as one model's, and every name in it gets
    its own key so a NEW unpriced model joining an old one still announces
    itself. Held (see :func:`retained_keys`) while the name is in the 30-day
    ``unknown_models`` list, so a model that stays unpriced notifies once, not
    once a day. No network: the names and counts are the rollup's.
    """
    out: dict[str, Event] = {}
    for vendor, names, tokens in _unpriced_buckets(snapshot):
        if tokens < min_tokens:
            continue
        figure = f"{format_tokens(tokens)} tokens today"
        if len(names) > 1:
            figure = f"{figure} between {', '.join(names)}"
        for name in names:
            ledger_key = f"unpriced:{name}"
            out[ledger_key] = Event(
                key=ledger_key,
                kind="unpriced",
                severity=SEVERITY_WARN,
                scope=SCOPE_COST,
                title=f"{APP_TITLE} · unpriced model",
                subtitle=f"{vendor_label(vendor) if vendor else ''} {name}".strip(),
                message=f"{figure}, counted at $0 (no published rate)",
            )
    return out


def _reset_usable_conditions(snapshot: Any) -> dict[str, Event]:
    """``reset-usable:<row>`` — a Codex reset credit that can be spent now.

    Reads ``AccountRow.reset_credits_usable`` (lane codex-resets) with
    ``getattr``: ``None`` means "not reported" and never fires. Standing while
    the count is >= 1; released when it returns to 0, which re-arms it. The
    ledger makes it once per transition and once across a restart.
    """
    out: dict[str, Event] = {}
    for key, row in _rows(snapshot).items():
        if getattr(row, "vendor", "") != VENDOR_CODEX:
            continue
        usable = getattr(row, "reset_credits_usable", None)
        if usable is None or isinstance(usable, bool):
            continue
        try:
            count = int(usable)
        except (TypeError, ValueError):
            continue
        if count < 1:
            continue
        noun = "reset credit" if count == 1 else "reset credits"
        message = f"{_display_name(row)}: {count} {noun} usable now"
        weekly = _pct_of(row, _SEVEN_DAY_KEY)
        if weekly is not None:
            message = f"{message} — weekly {format_pct(weekly)}"
        ledger_key = f"reset-usable:{key}"
        out[ledger_key] = Event(
            key=ledger_key,
            kind="reset-usable",
            severity=SEVERITY_INFO,
            scope=key,
            title="Codex reset available",
            subtitle="",
            message=message,
        )
    return out


def rearms(key: str) -> bool:
    """Whether a held *key* is sent again after :data:`ATTENTION_REARM_SECONDS`.

    Codex ``warn`` attention keys only (``attention:codex:<slot>:<alias>:warn``):
    a dead login, no access, offline. Claude sentinel keys (``sentinel:*``) are
    deliberately NOT re-armed - claude-swap owns those faults and their
    remedies, and the same constant is not applied to them unless someone
    decides to on purpose. ``crit`` (a capped plan) is a wall with its own
    ``back`` event, not a thing to be reminded of.
    """
    return key.startswith(f"attention:{VENDOR_CODEX}:") and key.endswith(f":{SEVERITY_WARN}")


def detect(
    previous: Any,
    current: Any,
    *,
    threshold: float | None = None,
    now: float | None = None,
    unpriced_min_tokens: int | None = None,
) -> list[Event]:
    """Everything that became true between *previous* and *current*. PURE.

    No I/O, no clock, no ledger, no sending. ``previous`` may be ``None`` (the
    very first publish), in which case only the standing conditions of
    *current* are reported — a percentage rule needs two readings.

    Args:
        previous: the snapshot last published, or ``None``.
        current: the snapshot being published now.
        threshold: alert percentage; defaults to the settings default.
        now: the instant to judge a login's expiry against. ``None`` (no
            clock) leaves the pre-expiry rule out, which keeps this pure.
        unpriced_min_tokens: the unpriced-model floor; ``None`` means the
            settings default, 0 turns the rule off.

    Returns:
        Events in a stable order: percentage transitions first, then standing
        conditions sorted by key, so two runs over the same pair of snapshots
        produce the same list.
    """
    limit = _threshold_of({"notification_threshold_pct": threshold} if threshold is not None else None)
    floor = _unpriced_floor_of(
        {"unpriced_alert_min_tokens": unpriced_min_tokens} if unpriced_min_tokens is not None else None
    )
    events = _pct_events(previous, current, threshold=limit)
    options = {"now": now, "threshold": limit, "unpriced_min_tokens": floor}
    before = _standing_conditions(previous, **options)
    standing = _standing_conditions(current, **options)
    for key in sorted(standing):
        if key not in before:
            events.append(standing[key])
    return events


def retained_keys(
    snapshot: Any,
    *,
    threshold: float,
    now: float | None = None,
    unpriced_min_tokens: int | None = None,
) -> set[str]:
    """Ledger keys whose condition is still true of *snapshot*.

    The ledger releases everything else — subject to the throttle, and to
    :func:`observed_scopes`, which is what stops a tick that saw nothing from
    counting as a tick that saw the problem go away.
    """
    keys: set[str] = set(
        _standing_conditions(
            snapshot, now=now, threshold=threshold, unpriced_min_tokens=unpriced_min_tokens
        )
    )
    for key in list(keys):
        if key.startswith("spend:") and key.endswith(":capped"):
            # A capped spend limit still stands past the threshold: dropping
            # the threshold key here would let it fire the moment the
            # percentage slips back from 100 to 99.
            keys.add(key[: -len("capped")] + "threshold")
    cost = getattr(snapshot, "cost", None) if snapshot is not None else None
    if cost is not None and not getattr(cost, "is_partial", True):
        # Once per model name, not once per day: the key is held for as long
        # as the name is still in the 30-day unpriced list.
        for name in getattr(cost, "unknown_models", ()) or ():
            if name:
                keys.add(f"unpriced:{name}")
    rows = _rows(snapshot)
    for key, row in rows.items():
        for window_key, _label, pct, _resets in _windows(row):
            if pct is None:
                continue
            if pct >= ATTENTION_PCT:
                keys.add(f"capped:{key}:{window_key}")
            if pct >= threshold:
                keys.add(f"threshold:{key}:{window_key}")
            if pct < RECOVERY_PCT:
                keys.add(f"back:{key}:{window_key}")
    return keys


def observed_scopes(snapshot: Any) -> set[str]:
    """Which :attr:`Event.scope` values this snapshot can speak for.

    A row absent from the snapshot was not *observed to be fine*: the source
    may simply have failed this tick (``quota_rows`` goes empty on a Codex poll
    error). Its keys are therefore held rather than released, and the operator
    is not told twice about the same wall because a poll blipped.
    """
    scopes: set[str] = set(_rows(snapshot))
    scopes.add(SCOPE_AUDIT)  # a snapshot always answers "is there drift"
    cost = getattr(snapshot, "cost", None) if snapshot is not None else None
    if cost is not None and not getattr(cost, "is_partial", True):
        scopes.add(SCOPE_COST)
    notes = getattr(snapshot, "account_notes", None)
    if (notes and isinstance(notes, Mapping)) or (getattr(snapshot, "accounts", ()) or ()):
        scopes.add(SCOPE_SENTINEL)
    return scopes


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, payload: Any, *, log: Callable[[str], None]) -> bool:
    """Write *payload* to *path* atomically at 0600. Never raises.

    Same contract as ``state._JsonStore._write`` — temp file in the same
    directory, fsync, ``os.replace`` — but local, because this file holds a
    credential in one of its two uses and must never inherit a mode from an
    existing file the way the settings store deliberately does.
    """
    tmp_path: Path | None = None
    try:
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        # ``.tmp.json`` rather than ``.tmp``: the widget home IS this
        # repository on a source install, and the tree's ignore convention for
        # a half-written file is ``*.tmp.*``. A crash between ``mkstemp`` and
        # ``os.replace`` leaves this file behind, and an orphan holding a
        # Telegram token must not be able to show up as an untracked file that
        # someone commits (the notify.json / notify_state.json ignore lines
        # cover only the final names).
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp.json"
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, _FILE_MODE)
        os.replace(tmp_path, path)
        tmp_path = None
    except (OSError, TypeError, ValueError) as exc:
        log(f"notify: cannot write {path.name}: {type(exc).__name__}: {exc}")
        return False
    else:
        return True
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


class _Ledger:
    """``{key: (last_fired_epoch, scope)}``, persisted, never authoritative.

    Loaded lazily and only when there is something to decide: a tick with no
    transition and no release performs no write at all, which is what keeps
    this feature inside the SPEC 2.1 idle budget.
    """

    def __init__(self, path: Path, *, log: Callable[[str], None] = _log_noop) -> None:
        self._path = Path(path)
        self._log = log
        self._fired: dict[str, tuple[float, str]] | None = None

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> dict[str, tuple[float, str]]:
        if self._fired is not None:
            return self._fired
        fired: dict[str, tuple[float, str]] = {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = None
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self._log(f"notify: unreadable ledger ({type(exc).__name__}); starting empty")
            raw = None
        if isinstance(raw, Mapping):
            entries = raw.get("fired")
            if isinstance(entries, Mapping):
                for key, entry in entries.items():
                    when, scope = entry, ""
                    if isinstance(entry, Mapping):
                        when, scope = entry.get("at"), str(entry.get("scope", "") or "")
                    try:
                        fired[str(key)] = (float(when), scope)
                    except (TypeError, ValueError):
                        continue
        self._fired = fired
        return fired

    def suppressed(self, key: str) -> bool:
        """True when *key* has already been announced and not yet released."""
        return key in self._load()

    def age(self, key: str, now: float) -> float | None:
        """Seconds since *key* was last sent, or ``None`` when it is not held.

        What the per-kind re-arm reads (:func:`rearms`): a held key is released
        only when its condition goes false, so a condition that never goes
        false needs its age, not its presence, to be sent again.
        """
        entry = self._load().get(key)
        return None if entry is None else float(now) - entry[0]

    def commit(
        self,
        events: Sequence[Event],
        retained: set[str],
        observed: set[str],
        *,
        now: float,
    ) -> bool:
        """Record what was just sent and release what is no longer true.

        Called on EVERY publish, not only on a publish that notified: a key is
        released when its condition goes false, and a tick where nothing fires
        is exactly the tick where that usually happens. The write is what costs,
        so it happens only when the map actually changed — an idle widget with
        nothing standing performs no I/O here at all (SPEC 2.1).

        A key is released only when its scope was actually *observed*: a source
        that failed this tick published no rows, and "I did not see it" must not
        be read as "it is fixed". :data:`LEDGER_MAX_AGE_SECONDS` bounds the
        entries that are held that way forever (an account that was deleted).

        Returns whether anything changed.
        """
        fired = self._load()
        before = dict(fired)
        for event in events:
            fired[event.key] = (now, event.scope)
        for key, (when, scope) in list(fired.items()):
            if key in retained:
                continue
            age = now - when
            if scope and scope not in observed:
                if age > LEDGER_MAX_AGE_SECONDS:
                    del fired[key]
                continue
            if age > THROTTLE_SECONDS:
                del fired[key]
        if fired == before:
            return False
        payload = {
            "version": _LEDGER_VERSION,
            "fired": {key: {"at": when, "scope": scope} for key, (when, scope) in fired.items()},
        }
        _atomic_write_json(self._path, payload, log=self._log)
        return True

    def entry_count(self) -> int:
        return len(self._load())


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class TelegramCredentials:
    """A bot token and a chat id. Its ``repr`` never contains the token.

    ``repr`` is overridden rather than trusted to be unused: a credential
    object reaches a log line the moment somebody writes ``f"{creds}"`` in a
    debug print, and the privacy test in ``tests/test_privacy.py`` treats a
    plain dataclass repr as a leak.
    """

    token: str
    chat_id: str

    def __repr__(self) -> str:  # pragma: no cover - trivial, asserted in tests
        return f"TelegramCredentials(chat_id={self.chat_id!r}, token={_REDACTED})"

    def redact(self, text: str) -> str:
        """*text* with the token replaced. Applied to anything that may be logged."""
        return text.replace(self.token, _REDACTED) if self.token else text


def load_telegram_credentials(
    path: Path | str = NOTIFY_CREDENTIALS_PATH,
) -> tuple[TelegramCredentials | None, str]:
    """Read ``notify.json``. Returns ``(credentials, reason)``.

    ``reason`` is a human line for ``status`` and the Settings menu; it never
    contains any part of the token. A file that anyone but the owner can read
    is REFUSED rather than used — a bot token is a password, and the widget
    would otherwise happily send from a credential the whole machine can see.
    """
    target = Path(path)
    try:
        info = target.stat()
    except FileNotFoundError:
        return None, f"no {target.name}"
    except OSError as exc:
        return None, f"cannot stat {target.name}: {type(exc).__name__}"
    if stat.S_IMODE(info.st_mode) & 0o077:
        return None, f"{target.name} is group/world readable; chmod 600 it"
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return None, f"{target.name} is not readable JSON ({type(exc).__name__})"
    if not isinstance(raw, Mapping):
        return None, f"{target.name} is not an object"
    block = raw.get("telegram")
    block = block if isinstance(block, Mapping) else raw
    token = str(block.get("token", "") or "")
    chat_id = str(block.get("chat_id", "") or "")
    if not token:
        return None, f"{target.name} carries no telegram token"
    if not chat_id:
        return None, f"{target.name} carries no chat_id"
    return TelegramCredentials(token=token, chat_id=chat_id), "ok"


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------


class MacSender:
    """Notification Center, via ``rumps`` with an ``osascript`` fallback.

    ``rumps.notification`` only works from inside a running, bundled rumps app;
    from the CLI (``notify test``) there is no app, so the fallback is not
    defensive decoration — it is the path that CLI takes every time.
    """

    def __init__(
        self,
        *,
        notifier: Callable[[str, str, str], Any] | None = None,
        runner: Callable[[Sequence[str]], Any] | None = None,
        log: Callable[[str], None] = _log_noop,
    ) -> None:
        self._notifier = notifier
        self._runner = runner
        self._log = log

    def send(self, event: Event) -> bool:
        if self._try_rumps(event):
            return True
        return self._try_osascript(event)

    def _try_rumps(self, event: Event) -> bool:
        notifier = self._notifier
        if notifier is None:
            try:
                import rumps  # noqa: PLC0415 - optional, and only on macOS
            except Exception:
                return False
            notifier = rumps.notification
        try:
            notifier(event.title, event.subtitle, event.message)
        except Exception as exc:
            self._log(f"notify: rumps notification failed ({type(exc).__name__}); using osascript")
            return False
        return True

    def _try_osascript(self, event: Event) -> bool:
        runner = self._runner
        if runner is None:
            import subprocess  # noqa: PLC0415 - only on the fallback path

            def runner(argv: Sequence[str]) -> Any:  # type: ignore[misc]
                return subprocess.run(list(argv), check=True, capture_output=True, timeout=10)

        body = event.message or event.subtitle
        subtitle = event.subtitle if event.message else ""
        script = f"display notification {json.dumps(body)} with title {json.dumps(event.title)}"
        if subtitle:
            script += f" subtitle {json.dumps(subtitle)}"
        try:
            runner(["osascript", "-e", script])
        except Exception as exc:
            self._log(f"notify: osascript failed ({type(exc).__name__})")
            return False
        return True


class TelegramSender:
    """``POST /bot<token>/sendMessage``, with the token confined to the URL.

    The URL is never logged, never returned and never stored: the only thing
    that leaves this class is a boolean and, on failure, an exception TYPE plus
    a message run through :meth:`TelegramCredentials.redact`.
    """

    def __init__(
        self,
        credentials: TelegramCredentials,
        *,
        opener: Callable[..., Any] | None = None,
        timeout: float = TELEGRAM_TIMEOUT_SECONDS,
        log: Callable[[str], None] = _log_noop,
        api_root: str = TELEGRAM_API_ROOT,
    ) -> None:
        self._credentials = credentials
        self._opener = opener if opener is not None else urllib.request.urlopen
        self._timeout = float(timeout)
        self._log = log
        self._api_root = api_root.rstrip("/")

    def send(self, event: Event) -> bool:
        creds = self._credentials
        url = f"{self._api_root}/bot{creds.token}/sendMessage"
        payload = urllib.parse.urlencode(
            {
                "chat_id": creds.chat_id,
                "text": event.text,
                "disable_notification": "false",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            response = self._opener(request, timeout=self._timeout)
        except Exception as exc:
            self._log(
                "notify: telegram send failed "
                f"({type(exc).__name__}: {creds.redact(str(exc))})"
            )
            return False
        try:
            status = int(getattr(response, "status", 0) or getattr(response, "code", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        closer = getattr(response, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover - a fake with a broken close
                pass
        if status and status >= 400:
            self._log(f"notify: telegram refused the message (HTTP {status})")
            return False
        return True


# ---------------------------------------------------------------------------
# The notifier
# ---------------------------------------------------------------------------


def _dispatch_on_daemon_thread(work: Callable[[], None]) -> None:
    """Run *work* on a short-lived daemon thread.

    The worker thread owns a cadence; a Telegram request owns up to ten seconds
    of timeout. Those two facts must not meet.
    """
    threading.Thread(target=work, name="cc-usage-notify", daemon=True).start()


class Notifier:
    """Detection + ledger + dispatch, wired into the worker's publish path."""

    def __init__(
        self,
        *,
        settings: Callable[[], Mapping[str, Any]] | None = None,
        ledger_path: Path | str = NOTIFY_STATE_PATH,
        credentials_path: Path | str = NOTIFY_CREDENTIALS_PATH,
        mac_sender: Any | None = None,
        telegram_sender: Any | None = None,
        clock: Callable[[], float] = time.time,
        dispatch: Callable[[Callable[[], None]], None] = _dispatch_on_daemon_thread,
        log: Callable[[str], None] = _log_noop,
    ) -> None:
        self._settings = settings if settings is not None else (lambda: dict(SETTINGS_DEFAULTS))
        self._ledger = _Ledger(Path(ledger_path), log=log)
        self._credentials_path = Path(credentials_path)
        self._mac_sender = mac_sender
        self._telegram_sender = telegram_sender
        self._telegram_resolved = telegram_sender is not None
        self._clock = clock
        self._dispatch = dispatch
        self._log = log

    # -- detection (pure) --------------------------------------------------

    def observe(self, previous: Any, current: Any, *, now: float | None = None) -> list[Event]:
        """:func:`detect` at the configured threshold and unpriced floor, judged
        at *now* (default: the injected clock). No I/O, no sends."""
        return detect(
            previous,
            current,
            threshold=self.threshold(),
            now=float(self._clock()) if now is None else float(now),
            unpriced_min_tokens=self.unpriced_floor(),
        )

    def unpriced_floor(self) -> int:
        return _unpriced_floor_of(self._current_settings())

    def threshold(self) -> float:
        return _threshold_of(self._current_settings())

    def _current_settings(self) -> Mapping[str, Any]:
        try:
            settings = self._settings()
        except Exception:
            return dict(SETTINGS_DEFAULTS)
        return settings if isinstance(settings, Mapping) else dict(SETTINGS_DEFAULTS)

    # -- the impure half ---------------------------------------------------

    def notify(self, previous: Any, current: Any) -> list[Event]:
        """Detect, deduplicate, persist, dispatch. Returns what was sent.

        Called on the worker thread from ``BackgroundWorker._publish``. It is
        allowed to touch the disk (a sub-kilobyte ledger, only when something
        happened) but never to send: that is handed to ``dispatch``.
        """
        settings = self._current_settings()
        if not bool(settings.get("notifications_enabled", SETTINGS_DEFAULTS["notifications_enabled"])):
            return []
        threshold = self.threshold()
        floor = self.unpriced_floor()
        now = float(self._clock())
        events = self.observe(previous, current, now=now)
        fresh = [event for event in events if not self._ledger.suppressed(event.key)]
        # Per-kind re-arm (CX-6): a standing Codex warn that has been held for
        # ATTENTION_REARM_SECONDS is sent again. `detect` cannot see it - the
        # condition was already true of `previous` - so it is read from the
        # standing set directly, and committing it below restamps its age.
        sent = {event.key for event in fresh}
        standing = _standing_conditions(
            current, now=now, threshold=threshold, unpriced_min_tokens=floor
        )
        for key in sorted(standing):
            if key in sent or not rearms(key):
                continue
            age = self._ledger.age(key, now)
            if age is not None and age >= ATTENTION_REARM_SECONDS:
                fresh.append(standing[key])
        # Reconcile on every publish, fired or not: releasing a key whose
        # condition just went false is what lets the same account notify again
        # the next time it walls, and that release happens on a quiet tick.
        self._ledger.commit(
            fresh,
            retained_keys(current, threshold=threshold, now=now, unpriced_min_tokens=floor),
            observed_scopes(current),
            now=now,
        )
        if not fresh:
            return []
        telegram_on = bool(
            settings.get(
                "telegram_notifications_enabled",
                SETTINGS_DEFAULTS["telegram_notifications_enabled"],
            )
        )
        batch = tuple(fresh)
        self._dispatch(lambda: self._send_all(batch, telegram=telegram_on))
        return fresh

    def _send_all(self, events: Sequence[Event], *, telegram: bool) -> None:
        """Runs on the dispatcher's thread. Never raises into it."""
        mac = self._mac_sender
        if mac is None:
            mac = self._mac_sender = MacSender(log=self._log)
        for event in events:
            try:
                ok = mac.send(event)
            except Exception as exc:  # a sender must not kill the batch
                self._log(f"notify: macOS send raised {type(exc).__name__}")
                continue
            if ok:
                # The KEY, never the wording: it is machine-stable, carries no
                # figure that could be stale in a log, and is how a later
                # reader proves an announcement happened (0 such lines existed
                # before CX-6, so nothing proved Sep 20 was ever sent).
                self._log(f"notify: sent {event.key}")
        if not telegram:
            return
        sender = self._telegram()
        if sender is None:
            return
        for event in events:
            try:
                ok = sender.send(event)
            except Exception:
                self._log("notify: telegram send raised")
                continue
            if ok:
                self._log(f"notify: sent {event.key} via telegram")

    def _telegram(self) -> Any | None:
        if self._telegram_resolved:
            return self._telegram_sender
        self._telegram_resolved = True
        credentials, reason = load_telegram_credentials(self._credentials_path)
        if credentials is None:
            self._log(f"notify: telegram disabled ({reason})")
            self._telegram_sender = None
        else:
            self._telegram_sender = TelegramSender(credentials, log=self._log)
        return self._telegram_sender

    # -- introspection for the menu ---------------------------------------

    def telegram_configured(self) -> bool:
        """Whether ``notify.json`` holds usable credentials right now.

        Read live rather than cached: the Settings item that greys out the
        Telegram toggle must go live the moment ``notify setup`` has run, and
        the answer costs one ``stat`` plus a sub-kilobyte read.
        """
        credentials, _reason = load_telegram_credentials(self._credentials_path)
        return credentials is not None


def build_notifier(
    *,
    settings: Callable[[], Mapping[str, Any]],
    log: Callable[[str], None] = _log_noop,
) -> Notifier:
    """The production notifier: real paths, real senders, daemon dispatch."""
    return Notifier(settings=settings, log=log)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = """usage: python -m cc_usage_widget.notify <command>

  setup --telegram-token-from-env NAME --chat-id N
                 write notify.json (0600) with the bot token read from the
                 NAMED ENVIRONMENT VARIABLE. The token is never taken from
                 the command line, so it cannot reach a shell history or a
                 process listing.
  test           send one macOS notification and, when configured, one
                 Telegram message. Nothing is written.
  status         where the files are, whether they are usable, how many
                 transitions are currently held. Prints no secret.
"""


def _parse_flags(args: Sequence[str]) -> tuple[dict[str, str], str]:
    """``--key value`` pairs. Returns ``(flags, error)``."""
    flags: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            return flags, f"unexpected argument {token!r}"
        if "=" in token:
            name, value = token[2:].split("=", 1)
            flags[name] = value
            index += 1
            continue
        if index + 1 >= len(args):
            return flags, f"{token} needs a value"
        flags[token[2:]] = args[index + 1]
        index += 2
    return flags, ""


def _cmd_setup(
    args: Sequence[str],
    *,
    credentials_path: Path,
    env: Mapping[str, str],
    write: Callable[[str], Any],
    log: Callable[[str], None],
) -> int:
    flags, error = _parse_flags(args)
    if error:
        write(error + "\n")
        return 2
    if "telegram-token" in flags or "token" in flags:
        write(
            "refusing a token on the command line: it would be visible in the "
            "process list and the shell history. Use "
            "--telegram-token-from-env NAME.\n"
        )
        return 2
    env_name = flags.get("telegram-token-from-env", "TELEGRAM_BOT_TOKEN")
    chat_id = flags.get("chat-id", "").strip()
    if not chat_id:
        write("setup needs --chat-id\n")
        return 2
    token = (env.get(env_name) or "").strip()
    if not token:
        write(f"${env_name} is empty or unset; export it and run setup again\n")
        return 2
    payload = {
        "version": _CREDENTIALS_VERSION,
        "telegram": {"token": token, "chat_id": chat_id},
    }
    if not _atomic_write_json(credentials_path, payload, log=log):
        write(f"could not write {credentials_path}\n")
        return 1
    mode = stat.S_IMODE(os.stat(credentials_path).st_mode)
    write(f"wrote {credentials_path} (mode {mode:04o}) for chat {chat_id}\n")
    write("token read from $" + env_name + "; it is not echoed anywhere\n")
    return 0


def _cmd_test(
    *,
    credentials_path: Path,
    write: Callable[[str], Any],
    log: Callable[[str], None],
    mac_sender: Any | None,
    telegram_sender: Any | None,
) -> int:
    event = Event(
        key="test",
        kind="test",
        severity=SEVERITY_INFO,
        title=APP_TITLE,
        subtitle="test notification",
        message="if you can read this, notifications work",
    )
    mac = mac_sender if mac_sender is not None else MacSender(log=log)
    ok_mac = bool(mac.send(event))
    write(f"macOS: {'sent' if ok_mac else 'FAILED'}\n")
    sender = telegram_sender
    if sender is None:
        credentials, reason = load_telegram_credentials(credentials_path)
        if credentials is None:
            write(f"telegram: skipped ({reason})\n")
            return 0 if ok_mac else 1
        sender = TelegramSender(credentials, log=log)
    ok_telegram = bool(sender.send(event))
    write(f"telegram: {'sent' if ok_telegram else 'FAILED'}\n")
    return 0 if (ok_mac and ok_telegram) else 1


def _cmd_status(
    *,
    credentials_path: Path,
    ledger_path: Path,
    write: Callable[[str], Any],
) -> int:
    credentials, reason = load_telegram_credentials(credentials_path)
    write(f"credentials: {credentials_path}\n")
    write(f"  telegram: {reason}\n")
    if credentials is not None:
        write(f"  chat_id: {credentials.chat_id}\n")
        write("  token: present (never printed)\n")
    write(f"ledger: {ledger_path}\n")
    ledger = _Ledger(ledger_path)
    write(f"  transitions held: {ledger.entry_count()}\n")
    return 0


def main(
    argv: Sequence[str],
    *,
    credentials_path: os.PathLike[str] | str | None = None,
    ledger_path: os.PathLike[str] | str | None = None,
    env: Mapping[str, str] | None = None,
    out: Any = None,
    mac_sender: Any | None = None,
    telegram_sender: Any | None = None,
) -> int:
    """``setup | test | status``. Keyword arguments are test seams only."""
    write = (out or sys.stdout).write
    args = list(argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        write(_USAGE)
        return 0 if args else 2
    credentials = Path(credentials_path) if credentials_path is not None else NOTIFY_CREDENTIALS_PATH
    ledger = Path(ledger_path) if ledger_path is not None else NOTIFY_STATE_PATH
    environ = env if env is not None else os.environ

    def log(message: str) -> None:
        write(message + "\n")

    command = args[0]
    if command == "setup":
        return _cmd_setup(
            args[1:], credentials_path=credentials, env=environ, write=write, log=log
        )
    if command == "test":
        return _cmd_test(
            credentials_path=credentials,
            write=write,
            log=log,
            mac_sender=mac_sender,
            telegram_sender=telegram_sender,
        )
    if command == "status":
        return _cmd_status(credentials_path=credentials, ledger_path=ledger, write=write)
    write(_USAGE)
    return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main(sys.argv[1:]))
