"""Live per-account Codex quota — one row per tracked login (SPEC-CODEX 6).

What this module is for
=======================

``codex_indexer`` answers "how much of the Codex plan is used?" by reading the
percentage OpenAI already wrote into the rollout transcripts. That answer is
free and offline, but it is **anonymous**: a rollout carries no account id, so
the figure can only describe whichever login happened to write it. Vlad runs
four Codex logins and wants all four visible at once, with the one the CLI is
actually using marked — which the corpus cannot express.

So this module asks the vendor directly: one ``GET`` per tracked account
against :data:`~cc_usage_widget.contracts.CODEX_USAGE_URL`, authorised by that
account's own stored credential, mapped into the same :class:`AccountRow` the
menu already renders. The transcript-derived row is not replaced — it is
*arbitrated* against these by :func:`~cc_usage_widget.contracts.merge_quota_rows`
so one account never shows two different numbers (SPEC 4.3).

The four honesty rules this file exists to keep
===============================================

1. **A window is identified by its WIDTH, never its position.** OpenAI reports
   ``primary_window`` / ``secondary_window``; which one is the week is not
   guaranteed and differs by plan. ``limit_window_seconds == 604800`` is the
   week, ``== 18000`` is the 5-hour bucket, and anything else goes to
   ``scoped_windows`` under a label derived from the width. Nothing is ever
   coerced into a weekly bar because it arrived first.
2. **A sentinel REPLACES a number; it never decorates one.** A row whose
   credential is dead says ``relogin`` and shows no invented figure; a row
   whose last reading is 7 h old withholds its bars and keeps the header. The
   only thing a sentinel may sit beside is a figure that is still *true* — a
   ``capped`` note next to a real 100 %.
3. **Every derived state is answerable.** For each note the module can set,
   the class docstrings below state set-by / cleared-by / ages-out /
   rehydrated-at-cold-start / when-the-producer-is-off. A state nobody can
   clear is a bug that outlives the session that created it.
4. **A token never leaves the credential file.** It is read, put in one
   ``Authorization`` header, and dropped. It is not logged, not persisted, not
   put in an exception message, not in a menu label, and not in ``repr()`` —
   :class:`Credential` overrides ``__repr__`` for exactly that reason. Enforced
   by the canary test in ``tests/test_privacy.py``.

Refreshing a token is OFF, and off means silent
===============================================

The stored ``access_token`` lives ~10 days; this module decodes its ``exp``
locally (base64url payload, **no signature check** — display and scheduling
only) and starts saying ``relogin in 1d 4h`` 48 h out, then ``relogin`` when it
passes.

:class:`TokenRefresher` (roadmap 13) can POST the OAuth refresh grant that
would avoid that relogin, but ``codex_refresh_enabled`` defaults to **False**
and while it is False the class is never called: not one token request is made,
and ``auth.json`` is read and never written — which is the only reason this
module is allowed to hold a write path to a credential file at all. Whether two
``codex login`` sessions under one public OAuth client share a refresh-token
family is still unverified, and a wrong guess logs Vlad out of the account he
is coding in.

*What opens the switch:* ``python -m cc_usage_widget.codex_accounts
probe-refresh <alias>`` on a throwaway account — one grant, persisted, then one
usage GET proving the new token works — followed by a 24 h soak confirming the
other stores and ``~/.codex`` still work (SPEC-CODEX 6.4). Until that result
exists the 10-day relogin is the shipped cost and it is stated on the row
rather than hidden. There is deliberately **no replay path**: a superseded
refresh token is overwritten in place, never kept in a ``.prev`` file, so the
reuse-detection question can only be asked on purpose.

Threading
=========

One daemon poller thread, owned here, not the widget's worker tick: a socket
wait on the worker thread would delay the autoswitch deadline (SPEC 2.3). The
thread body is wrapped exactly like ``app.BackgroundWorker._loop`` — anything
short of the stop event degrades to a visible note and the schedule survives.
:meth:`CodexAccountsSource.quota_rows` is the only method the worker calls and
it does no network I/O: a lock, one ``stat`` of the registry, one ``stat`` of
``~/.codex/auth.json``.

Scheduling is on ``time.time()``, not ``time.monotonic()``: on this Mac
``monotonic`` is ``mach_absolute_time()`` and **pauses while the lid is shut**,
so a monotonic schedule would silently skip the whole sleep. The two clocks are
still compared every cycle — a wall/monotonic divergence larger than two
intervals is a wake, and every account is marked due at once.

CLI
===

``python -m cc_usage_widget.codex_accounts adopt | list | probe <alias|id> |
best | link | probe-refresh <alias|id>`` — its own process, so onboarding never
contends with the widget's flock.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import http.client
import json
import os
import shutil
import stat as stat_module
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Protocol, Sequence

from .contracts import (
    CODEX_ACCOUNTS_DIR,
    CODEX_ACCOUNTS_REGISTRY_PATH,
    CODEX_ACTIVE_GRACE_SECONDS,
    CODEX_AUTH_PATH,
    CODEX_FETCH_EXPIRE_SECONDS,
    CODEX_FETCH_STALE_SECONDS,
    CODEX_QUOTA_SNAPSHOTS_PATH,
    CODEX_RELOGIN_WARN_SECONDS,
    CODEX_USAGE_URL,
    CODEX_WINDOW_SECONDS_FIVE_HOUR,
    CODEX_WINDOW_SECONDS_WEEKLY,
    SETTINGS_BOUNDS,
    SETTINGS_CHOICES,
    SETTINGS_DEFAULTS,
    SETTINGS_PATH,
    VENDOR_CODEX,
    AccountRow,
    Pct,
    Vendor,
    normalize_settings,
)
from .render import (
    # Display formatters live in `render` because BOTH this module and `app`
    # need them and `app` may never import this one: `__main__` imports
    # `codex_accounts` inside a guard on purpose, so that a half-written file
    # here costs one `!` line instead of the whole widget (SPEC-CODEX 6).
    coarse_duration,
    fleet_reset_label,
    title_reset_suffix,
    window_minutes_label,
)

__all__ = [
    "CodexAccountsSource",
    "CodexAccountQuota",
    "WindowSample",
    "Credential",
    "CredentialStore",
    "Registry",
    "RegistryEntry",
    "FetchState",
    "HttpResponse",
    "UsageTransport",
    "UrllibTransport",
    "coarse_duration",
    "decode_jwt_claims",
    "fleet_reset_label",
    "main",
    "pace_note",
    "pricing_tier_note",
    "title_reset_suffix",
]


# ---------------------------------------------------------------------------
# Vocabulary and tuning
# ---------------------------------------------------------------------------

NOTE_RELOGIN = "relogin"
"""The stored credential cannot speak for this account any more.

set-by: ``exp <= now`` before a request is made, or HTTP 401 from the endpoint.
cleared-by: the next 200 (which can only happen after ``codex login`` rewrites
the file). ages-out: never — a dead credential stays dead until a human fixes
it, and a note that expired on its own would show a bygone percentage as live.
rehydrated: yes, from the sidecar, so a restart says ``relogin`` immediately
instead of showing the last good figure for one poll interval.
producer off: the note stops being *refreshed* but stays on the row; nothing
polls, so nothing may claim the account recovered."""

NOTE_NO_ACCESS = "no access"
"""HTTP 403 with a JSON body: the endpoint answered, and the answer is no.

Deliberately not ``relogin`` — logging in again does not grant a permission the
workspace never gave. set-by: JSON 403. cleared-by: a 200. ages-out: never.
rehydrated: yes. producer off: frozen, as above."""

NOTE_RATE_LIMITED = "rate limited"
"""HTTP 429 — us, not the plan. Never confuse this with the plan's own
``limit_reached``: that is Vlad's quota, this is our polling.

set-by: 429. cleared-by: the next 200. ages-out: no, but the retry that clears
it is scheduled from ``Retry-After`` (clamped) or the ladder, so it is always
self-limiting. rehydrated: yes. producer off: frozen."""

NOTE_ENDPOINT_ERROR = "endpoint error"
"""5xx, an HTML challenge behind a 403, a truncated body, or JSON we cannot
parse — the endpoint is there and is not answering the question.

set-by: the third consecutive such failure (one 502 is noise, and a note that
appears on the first blip trains the eye to ignore it). cleared-by: a 200,
which also zeroes the counter. ages-out: no. rehydrated: yes. producer off:
frozen."""

NOTE_OFFLINE = "offline"
"""``OSError`` from the transport: DNS, socket, TLS — we never reached it.

set-by: the third consecutive failure. cleared-by: a 200. ages-out: no.
rehydrated: yes — a laptop that was shut in a tunnel and restarted should not
claim a fresh reading. producer off: frozen."""

NOTE_PENDING = "awaiting first reading"
"""An enabled account with no snapshot at all: onboarded, never polled.

set-by: :meth:`CodexAccountsSource.quota_rows` when the account has no
snapshot and no other note. cleared-by: the first outcome of any kind.
ages-out: n/a. rehydrated: n/a (it *is* the empty state). producer off: shown,
which is the truth — the feature is off, so there is no reading."""

NOTE_NO_CREDENTIAL = "credential unreadable"
"""The account is in the registry but ``auth.json`` is missing, unparseable, or
group/world-readable, so no request may be made with it.

set-by: :meth:`CredentialStore.read` returning ``None``. cleared-by: a
successful read followed by any outcome. ages-out: never. rehydrated: yes.
producer off: frozen. (The poll rules in the plan do not name this case; a
refused credential must still say *why* the row is not moving rather than sit
on ``awaiting first reading`` forever.)"""

NOTE_OUT_OF_CREDITS = "out of credits · Add credits"
"""The plan is blocked on CREDITS, not on a spent window (roadmap 11).

A ``crit`` note that stands in for ``capped <type>`` and, like it, sits beside
the figures rather than replacing them: the week really is at whatever the bar
says, and the reason the account cannot be used is a different one.

set-by: :attr:`CodexAccountQuota.out_of_credits` on a capped 200 —
``rate_limit_reached_type.type == "workspace_owner_credits_depleted"``, or
``credits.has_credits false`` together with a model saying
``credits_would_enable``. cleared-by: the next 200 that is not capped (adding
credits, or the workspace's own reset). ages-out: never on its own; it is a
property of the last reading, so an expired reading withholds it with the
bars. rehydrated: yes, with the snapshot. producer off: frozen with the rest
of the row."""

KIND_INFO = "info"
KIND_WARN = "warn"
KIND_CRIT = "crit"

_BODY_CAP_BYTES = 256 * 1024
"""Ceiling on a response body. The endpoint answers in a few KB; anything
larger is a captive portal or a challenge page, and reading it into the
widget's heap is the only harm it could do."""

_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 15.0

_MIN_ACCOUNT_SPACING_SECONDS = 20.0
"""Minimum gap between two requests inside one cycle. Four accounts polled
back-to-back look exactly like a script to any rate limiter."""

_JITTER_FRACTION = 0.15
"""±15 % on the interval, derived from a hash of the account id — never
``random``. The point is to de-phase four accounts from each other (and from
whatever the other copy of this widget on another Mac is doing) *reproducibly*,
so a scheduling bug is the same bug on every run."""

_ENDPOINT_LADDER_START = 60.0
_RATE_LIMIT_LADDER_START = 120.0
_LADDER_CAP_SECONDS = 1800.0
_NOTE_AFTER_FAILURES = 3
_UNAUTHORIZED_BACKOFF_SECONDS = 1800.0
_FORBIDDEN_BACKOFF_SECONDS = 3600.0
_RETRY_AFTER_CAP_SECONDS = 3600.0

_RESET_GRACE_SECONDS = 120.0
"""How far past a reported reset instant a window is still called live. The
endpoint's clock and ours differ by seconds, and a window that flips to
``overdue`` two seconds early is a lie about a healthy account."""

_CYCLE_SLEEP_MAX_SECONDS = 30.0
"""Longest the poller sleeps between cycles even when nothing is due, so the
wake detector runs soon after the lid opens."""

_SNAPSHOT_VERSION = 1
_REGISTRY_VERSION = 1


def _log(message: str) -> None:
    """Timestamped stderr line, same shape as ``app.py``'s log.

    Callers pass alias/id-prefix, status and milliseconds only. Never a header,
    a token, a URL query or a body — ``widget.log`` is append-forever and world
    readable to anyone who can read the home directory."""
    sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] cc-usage-widget: {message}\n")
    sys.stderr.flush()


def _describe(exc: BaseException) -> str:
    """One-line, secret-free description of *exc* for a diagnostics row.

    The type name plus a truncated message. Transport failures are described by
    **type only** at the call site: a URL or a header must never reach a log
    line by riding inside an exception."""
    text = str(exc).strip().replace("\n", " ")
    if len(text) > 120:
        text = text[:117] + "..."
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# ---------------------------------------------------------------------------
# Small total parsers (a hand-edited or truncated file must never raise)
# ---------------------------------------------------------------------------


def _to_pct(value: Any) -> Pct | None:
    """0-100 float, or ``None``. Mirrors ``codex_indexer._to_pct``: bools are
    not numbers, NaN is not a percentage, and the range is clamped because a
    bar cannot render 130 %."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        if result != result:  # NaN
            return None
        return min(100.0, max(0.0, result))
    return None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (OverflowError, ValueError):  # pragma: no cover - absurd float
            return None
    return None


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return None if result != result else result
    return None


def _to_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _to_number(value: Any) -> float | None:
    """A count the body may send as a number **or** as a decimal string.

    ``credits.balance`` arrives as ``null`` on the captured Business body and
    as the string ``"0"`` on the probed Pro one, so a numbers-only reader would
    silently drop a real figure. Only a plain decimal is accepted — no units,
    no currency symbols, no exponent salad — because anything else is a shape
    we have not seen and a guess about it would end up on a menu row.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return None if result != result else result
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            result = float(text)
        except ValueError:
            return None
        return None if result != result or result in (float("inf"), float("-inf")) else result
    return None


def _parse_iso8601(text: str) -> float | None:
    """``"2026-09-15T18:35:56.051122Z"`` -> epoch, or ``None``.

    The ONE absolute instant this module accepts from a body (``available_at``;
    every rate-limit reset is a duration we anchor ourselves). It is accepted
    because there is no duration form of it and because it is rendered as a
    DATE — ``back Sep 15`` — where a few seconds of server/client clock skew
    cannot change the answer. A naive timestamp is read as UTC, which is what
    the endpoint sends.
    """
    text = text.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    try:
        return when.timestamp()
    except (OSError, OverflowError, ValueError):  # pragma: no cover - absurd year
        return None


def _model_availability(raw: Any) -> tuple[tuple[tuple[str, float], ...], bool]:
    """``model_usage`` -> ``((slug, available_at epoch), …), credits_would_enable``.

    Only models the body says are NOT available yet are listed: an
    ``available_at`` beside ``available: true`` is a bygone instant, and
    printing "back Sep 15" for a model that is already back is exactly the
    kind of stale sentence SPEC 4.3 forbids. Sorted soonest first so the row
    reads in the order the models return.
    """
    if not isinstance(raw, Mapping):
        return (), False
    entries: list[tuple[str, float]] = []
    would_enable = False
    for slug, info in raw.items():
        if not isinstance(slug, str) or not slug or not isinstance(info, Mapping):
            continue
        if _to_bool(info.get("credits_would_enable"), False):
            would_enable = True
        if info.get("available") is True:
            continue
        when = _parse_iso8601(_to_str(info.get("available_at")))
        if when is not None:
            entries.append((slug, when))
    entries.sort(key=lambda pair: (pair[1], pair[0]))
    return tuple(entries), would_enable


def _reached_type(value: Any) -> str | None:
    """``rate_limit_reached_type`` as a string, whichever shape it arrives in.

    The Pro body carries a string or ``null``; a Business workspace was probed
    on 2026-09-10 carrying ``{"type": "workspace_owner_credits_depleted",
    "details": null}``. Read the ``type`` of the object form; anything else
    is ``None`` (a note of ``capped`` with no type, never a dict rendered as
    prose).
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        inner = value.get("type")
        return inner if isinstance(inner, str) and inner else None
    return None


def _to_bool(value: Any, default: bool) -> bool:
    """A missing or junk flag is *unknown*, and unknown means the default —
    never ``False``. Reading a missing ``allowed`` as "not allowed" would put a
    ``capped`` note on a perfectly healthy account."""
    return value if isinstance(value, bool) else default


def _format_reset_clock(epoch: float, now: float) -> str:
    """``"10:59"`` today, ``"Aug 24 14:50"`` otherwise.

    SPEC 4.3 says reset times are shown verbatim and never recomputed. That
    still holds: OpenAI hands us ``reset_after_seconds``, a *duration*, so
    exactly one anchoring-and-formatting step is unavoidable and it happens
    once, here, on the way in. The shape is the twin of
    ``codex_indexer._format_reset_clock`` so the two Codex sections line up in
    the menu; it is duplicated rather than imported because that module is a
    98 KB scanner this one (and its CLI) has no other reason to load.
    """
    try:
        when = dt.datetime.fromtimestamp(epoch)
        today = dt.datetime.fromtimestamp(now).date()
    except (OSError, OverflowError, ValueError):  # pragma: no cover - absurd epoch
        return ""
    if when.date() == today:
        return when.strftime("%H:%M")
    return f"{when:%b} {when.day} {when:%H:%M}"


def _format_duration(seconds: float) -> str:
    """``"1d 4h"`` / ``"3h 20m"`` / ``"12m"`` — the countdown wording.

    Two units at most: the row is a menu line, not a stopwatch, and "1d 4h" is
    the granularity at which a human decides whether to log in today.
    """
    total = int(max(0.0, seconds))
    days, rem = divmod(total, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def pricing_tier_note(settings: Mapping[str, Any] | None = None) -> str:
    """The Codex cost heading's tier suffix (roadmap 11).

    ``"(standard tier)"`` when ``codex_pricing_tier`` is ``standard`` — which
    is what ``pricing.py``'s OpenAI rows actually are — and ``"(fast tier
    rates not loaded)"`` for any other tier.

    The second is the whole point of the setting. A rollout does not say which
    tier ran, and OpenAI's fast/batch multipliers are not in this repo; the
    options were to invent a multiplier, to silently keep charging standard
    rates under a heading that claims otherwise, or to say plainly that the
    figure below is not this tier's. SPEC 4.3 picks the third. Nothing else in
    the widget reads this setting: no dollar figure changes, because no rate
    changed.
    """
    settings = settings if isinstance(settings, Mapping) else {}
    choices = SETTINGS_CHOICES.get("codex_pricing_tier", ("standard",))
    tier = settings.get("codex_pricing_tier", choices[0])
    if not isinstance(tier, str) or tier not in choices:
        tier = choices[0]
    if tier == choices[0]:
        return f"({tier} tier)"
    return f"({tier} tier rates not loaded)"


def _format_reset_date(epoch: float) -> str:
    """``"Sep 15"`` — the date a model comes back, with no clock.

    The clock is deliberately dropped: ``available_at`` is an instant computed
    on the server, and the honest resolution of "when does Astra return" is the
    day. Printing ``18:35`` would invite someone to wait for a minute we did
    not measure.
    """
    try:
        when = dt.datetime.fromtimestamp(epoch)
    except (OSError, OverflowError, ValueError):  # pragma: no cover - absurd epoch
        return ""
    return f"{when:%b} {when.day}"


def _format_count(value: float) -> str:
    """``12`` / ``12.5`` — a count with no unit and no invented precision."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


PACE_RING_SIZE = 12
"""How many ``(fetched_at, weekly used_percent)`` samples the sidecar keeps
per account. Twelve at the 5-minute default is an hour of history — long
enough for a trend, short enough that the sidecar stays a cache."""

PACE_MIN_SAMPLES = 3
"""Two points are a line through noise: ``used_percent`` is reported as a
whole number, so a single 1 % step between two reads five minutes apart
"projects" the wall four hours out and then unprojects it on the next tick."""

PACE_MIN_SPAN_SECONDS = 1_800.0
"""And they must span half an hour. The honest rule from the roadmap: below
this a forecast is an extrapolation of rounding."""


def pace_note(
    samples: Sequence[tuple[float, float]],
    *,
    now: float,
    reset_at: float | None = None,
    capped: bool = False,
) -> str:
    """``"at this pace: wall in 6h"`` / ``"at this pace: resets first"`` / ``""``.

    Pure, and deliberately hard to make speak: it needs
    :data:`PACE_MIN_SAMPLES` readings spanning :data:`PACE_MIN_SPAN_SECONDS`
    with a strictly rising percentage, and it says nothing at all otherwise.
    A forecast is the one figure on this row that is not a fact, so it earns
    its place by being rare and by naming its assumption in its own wording —
    *at this pace*.

    Three silences worth stating:

    * a **capped** row gets none: the wall is not a projection any more;
    * a ring that spans a **reset** is trimmed to the samples after the drop,
      because a week that went 98 % -> 3 % has no trend through the seam;
    * a projection that lands at or beyond ``reset_at`` says ``resets first``
      rather than a countdown, since the window refills before the wall is
      reached and "wall in 5d" would be a number the plan makes impossible.

    (The build contract worded that last case as ``wall before reset``; the
    roadmap's own item 10 words it ``resets first``, which is the one that is
    true in the branch it names. Flagged for the integrator.)
    """
    if capped:
        return ""
    trimmed = _rising_tail(samples)
    if len(trimmed) < PACE_MIN_SAMPLES:
        return ""
    first_at, first_pct = trimmed[0]
    last_at, last_pct = trimmed[-1]
    span = last_at - first_at
    if span < PACE_MIN_SPAN_SECONDS or last_pct <= first_pct or last_pct >= 100.0:
        return ""
    rate = (last_pct - first_pct) / span  # percent per second
    if rate <= 0:  # pragma: no cover - guarded by the comparison above
        return ""
    wall_at = last_at + (100.0 - last_pct) / rate
    if wall_at <= now:
        # The projection has already expired: the newest sample is older than
        # the wall it predicted, so the account either hit the wall (and the
        # capped branch above will say so once the source re-reads) or the
        # trend broke. `coarse_duration` floors at "<1m", so without this the
        # row would advertise "at this pace: wall in <1m" for as long as the
        # ring stayed stale - a countdown to a moment that is already behind us
        # (SPEC 4.3: an age note, never a fabricated instant).
        return ""
    if reset_at is not None and reset_at <= wall_at:
        return "at this pace: resets first"
    return f"at this pace: wall in {coarse_duration(wall_at - now)}"


def _rising_tail(samples: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """The samples after the last percentage DROP, in time order.

    A drop is a window reset (or a correction from the endpoint); everything
    before it belongs to a window that no longer exists. Junk entries — a
    non-numeric pair, a sample from the future of its successor — are dropped
    rather than raising: this reads a hand-editable cache file.
    """
    clean: list[tuple[float, float]] = []
    for pair in samples or ():
        if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)) or len(pair) != 2:
            continue
        when = _to_float(pair[0])
        pct = _to_pct(pair[1])
        if when is None or pct is None:
            continue
        clean.append((when, pct))
    clean.sort(key=lambda pair: pair[0])
    start = 0
    for index in range(1, len(clean)):
        if clean[index][1] < clean[index - 1][1]:
            start = index
    return clean[start:]


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Case-insensitive header lookup. HTTP header names are case-insensitive
    and every transport spells them differently; a case-sensitive ``.get`` here
    once turned a well-behaved ``Retry-After`` into a blind ladder."""
    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == target:
            return value if isinstance(value, str) else str(value)
    return None


def _ladder(start: float, failures: int, cap: float = _LADDER_CAP_SECONDS) -> float:
    """Doubling backoff: ``start`` on the first failure, capped at *cap*."""
    if failures <= 1:
        return min(start, cap)
    return min(start * (2.0 ** (failures - 1)), cap)


def _parse_retry_after(raw: str | None, *, now: float) -> float | None:
    """``Retry-After`` in either legal form -> seconds from now, or ``None``.

    RFC 9110 allows both a delta in seconds and an HTTP-date; servers use both,
    and treating the date form as garbage would ignore an explicit instruction
    from the very endpoint we are trying not to annoy. A date in the past
    yields ``0``, not a negative delay.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(int(text)))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:  # pragma: no cover - defensive, older parsers return None
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    try:
        return max(0.0, when.timestamp() - now)
    except (OSError, OverflowError, ValueError):  # pragma: no cover
        return None


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Read a ChatGPT access/id token's payload. **No signature verification.**

    That is safe and deliberate here because nothing security-relevant is
    decided from the result: the claims are used to show which account a
    credential belongs to and to schedule a ``relogin`` note. The endpoint —
    not this function — decides whether the token is actually good; a forged
    ``exp`` would at worst make us poll an account we then get a 401 from,
    which is already a handled path.

    Returns ``{account_id, email, plan_type, exp}`` with ``None`` for anything
    the token does not carry, or ``{}`` when *token* is not a decodable JWT.
    The raw claim set is deliberately **not** returned: the payload also
    carries session identifiers this widget has no business propagating.
    """
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return {}
    if not isinstance(claims, Mapping):
        return {}
    auth = claims.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, Mapping) else {}
    profile = claims.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, Mapping) else {}
    account_id = _to_str(auth.get("chatgpt_account_id")) or None
    plan_type = _to_str(auth.get("chatgpt_plan_type")) or None
    mail = _to_str(profile.get("email")) or _to_str(claims.get("email")) or None
    return {
        "account_id": account_id,
        "email": mail,
        "plan_type": plan_type,
        "exp": _to_float(claims.get("exp")),
    }


# ---------------------------------------------------------------------------
# 1. The mapped response
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowSample:
    """One rate-limit window as the endpoint reported it.

    ``window_seconds`` is the **identity** of the window, not a decoration:
    every dispatch decision in :meth:`CodexAccountQuota.from_response` is made
    from it. ``reset_at`` is an epoch we computed once by anchoring the
    reported ``reset_after_seconds`` to the instant of the read — the body's
    own ``reset_at`` field is not used, because a duration cannot go stale in
    transit but an absolute instant computed on the server's clock can.
    """

    used_percent: Pct | None = None
    window_seconds: int | None = None
    reset_at: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "used_percent": self.used_percent,
            "window_seconds": self.window_seconds,
            "reset_at": self.reset_at,
        }

    @classmethod
    def from_json(cls, obj: Any) -> WindowSample | None:
        if not isinstance(obj, Mapping):
            return None
        return cls(
            used_percent=_to_pct(obj.get("used_percent")),
            window_seconds=_to_int(obj.get("window_seconds")),
            reset_at=_to_float(obj.get("reset_at")),
        )

    @classmethod
    def from_body(cls, obj: Any, *, observed_at: float) -> WindowSample | None:
        """Build from a ``*_window`` mapping, or ``None`` if it says nothing.

        ``null`` is the normal value of ``secondary_window`` on a Pro plan, so
        "nothing reported" must yield ``None`` rather than a row of zeros — a
        0 % bar for a window the plan does not have is an invented figure.
        """
        if not isinstance(obj, Mapping):
            return None
        pct = _to_pct(obj.get("used_percent"))
        width = _to_int(obj.get("limit_window_seconds"))
        after = _to_float(obj.get("reset_after_seconds"))
        reset_at = observed_at + after if after is not None else None
        if pct is None and width is None and reset_at is None:
            return None
        return cls(used_percent=pct, window_seconds=width, reset_at=reset_at)


@dataclass(frozen=True, slots=True)
class CodexAccountQuota:
    """One account's live quota, as of ``fetched_at``.

    The mirror of ``codex_indexer.CodexQuota`` for the fetched path: same
    ``to_json``/``from_json`` totality (a corrupt sidecar costs at most one
    missing row), same "render as an :class:`AccountRow`" discipline, same
    refusal to carry anything the menu will not honestly show.

    Deliberately **not** carried: ``spend_control`` (rendering
    ``individual_limit`` would imply a unit we have not verified — dollars?
    requests? — and a wrong unit next to a real number is worse than silence),
    and ``model_usage``'s per-model token counts (they already come from the
    corpus, priced by ``pricing.py``; a second, differently-defined source of
    the same quantity is how two numbers for one thing get shipped).

    Two fields of ``model_usage`` and three of ``credits`` ARE carried
    (roadmap 11/12), because each answers a question the bars cannot: *why* a
    workspace is capped when its week is not spent, and *when* a model comes
    back. They are counts and instants the endpoint stated, never a price and
    never a unit we guessed — ``credits.balance`` renders as ``credits 12``
    with no currency, and ``available_at`` as a date.
    """

    account_id: str
    email: str = ""
    plan_type: str | None = None
    five_hour: WindowSample | None = None
    seven_day: WindowSample | None = None
    scoped: tuple[tuple[str, WindowSample], ...] = ()
    limit_reached: bool = False
    allowed: bool = True
    reached_type: str | None = None
    fetched_at: float = 0.0
    credits_balance: float | None = None
    """``credits.balance`` as a number, or ``None`` when the endpoint sent
    ``null`` (the captured Business body) or something unparseable. The unit is
    deliberately unnamed on the row: the body calls them credits and so do we."""
    credits_has: bool | None = None
    """``credits.has_credits``. ``None`` = not reported, which is NOT False —
    a missing flag must never turn a healthy cap into ``out of credits``."""
    credits_would_enable: bool = False
    """True when any ``model_usage`` entry says ``credits_would_enable``: buying
    credits would lift this block. With ``credits_has is False`` it is the
    second, corroborating route to the ``out of credits`` verdict."""
    availability: tuple[tuple[str, float], ...] = ()
    """``(model slug, available_at epoch)`` for every model the body says is
    NOT available yet, soonest first. The slug is the vendor's own
    (``gpt-6-astra``) — the same choice ``pricing.MODEL_DISPLAY_NAMES`` makes
    for OpenAI models, and the name the user sees everywhere else in the menu."""

    # -- construction ------------------------------------------------------

    @classmethod
    def from_response(
        cls,
        body: Mapping[str, Any],
        *,
        credential_account_id: str,
        observed_at: float,
        include_extra: bool,
    ) -> CodexAccountQuota | None:
        """Map one ``/backend-api/wham/usage`` body, or ``None`` to drop it.

        ``None`` means "do not let this near the menu": the body is not a
        mapping, it carries neither rate limits nor a plan, or — the case worth
        the code — it reports an ``account_id`` that is **not** the one whose
        credential we authenticated with. That last one is the difference
        between four honest rows and four copies of whichever account the
        endpoint decided to answer for; the caller keeps the previous snapshot
        and raises a ``!`` diagnostics line rather than overwriting a good row
        with a mislabelled one.

        Windows are dispatched by width (SPEC-CODEX 6): ``604800`` is the week,
        ``18000`` the 5-hour bucket, everything else is scoped under a label
        derived from the width by :func:`render.window_minutes_label`. **First
        writer wins** — if a second window of the same width turns up (an
        ``additional_rate_limits`` pool reporting its own week, say) it goes to
        ``scoped`` under its ``limit_name`` instead of silently displacing the
        plan's own figure.

        ``additional_rate_limits`` is read only when *include_extra*: those are
        per-model pools (a Spark bucket, a reserve) that would triple the row
        height for a number most days is 0. They always render under their own
        ``limit_name`` in ``scoped``, never in the two named slots, even when
        their width matches - see the comment in the dispatch loop.
        """
        if not isinstance(body, Mapping):
            return None
        reported_id = _to_str(body.get("account_id"))
        if reported_id and reported_id != credential_account_id:
            return None

        rate_limit = body.get("rate_limit")
        plan_type = _to_str(body.get("plan_type")) or None
        if not isinstance(rate_limit, Mapping) and plan_type is None:
            return None

        allowed = True
        limit_reached = False
        candidates: list[tuple[str, Any]] = []
        if isinstance(rate_limit, Mapping):
            allowed = _to_bool(rate_limit.get("allowed"), True)
            limit_reached = _to_bool(rate_limit.get("limit_reached"), False)
            candidates.append(("", rate_limit.get("primary_window")))
            candidates.append(("", rate_limit.get("secondary_window")))

        if include_extra:
            extras = body.get("additional_rate_limits")
            if isinstance(extras, Sequence) and not isinstance(extras, (str, bytes)):
                for entry in extras:
                    if not isinstance(entry, Mapping):
                        continue
                    name = _to_str(entry.get("limit_name"))
                    inner = entry.get("rate_limit")
                    if not isinstance(inner, Mapping):
                        continue
                    # The pools use the same window keys as the plan's own
                    # rate_limit (probed 2026-09-09: primary_window /
                    # secondary_window); the bare names are accepted too.
                    candidates.append((name, inner.get("primary_window", inner.get("primary"))))
                    candidates.append((name, inner.get("secondary_window", inner.get("secondary"))))

        five_hour: WindowSample | None = None
        seven_day: WindowSample | None = None
        scoped: list[tuple[str, WindowSample]] = []
        taken: set[str] = set()
        for name, raw in candidates:
            sample = WindowSample.from_body(raw, observed_at=observed_at)
            if sample is None:
                continue
            if not name:
                # Only the plan's OWN windows compete for the two named slots.
                # A pool that arrived with a `limit_name` is a different
                # quantity that merely happens to share a width - putting a
                # Spark bucket's 5-hour figure on the plan's 5-hour bar is the
                # same "two numbers for one thing" failure `merge_quota_rows`
                # exists to prevent, so a named pool always renders as itself.
                if sample.window_seconds == CODEX_WINDOW_SECONDS_WEEKLY and seven_day is None:
                    seven_day = sample
                    continue
                if sample.window_seconds == CODEX_WINDOW_SECONDS_FIVE_HOUR and five_hour is None:
                    five_hour = sample
                    continue
            label = _scoped_label(name, sample.window_seconds, taken)
            if not label:
                continue  # first writer wins; a third same-named window is dropped
            taken.add(label)
            scoped.append((label, sample))

        reached_type = _reached_type(body.get("rate_limit_reached_type"))
        if reached_type is None and isinstance(rate_limit, Mapping):
            reached_type = _reached_type(rate_limit.get("rate_limit_reached_type"))

        credits = body.get("credits")
        credits = credits if isinstance(credits, Mapping) else {}
        raw_has = credits.get("has_credits")
        availability, would_enable = _model_availability(body.get("model_usage"))

        return cls(
            account_id=credential_account_id,
            email=_to_str(body.get("email")),
            plan_type=plan_type,
            five_hour=five_hour,
            seven_day=seven_day,
            scoped=tuple(scoped),
            limit_reached=limit_reached,
            allowed=allowed,
            reached_type=reached_type,
            fetched_at=observed_at,
            credits_balance=_to_number(credits.get("balance")),
            credits_has=raw_has if isinstance(raw_has, bool) else None,
            credits_would_enable=would_enable,
            availability=availability,
        )

    # -- persistence -------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        """Sidecar form. ``None`` fields are kept so the shape is stable and a
        diff of the file reads as a diff of the account, not of the schema."""
        return {
            "account_id": self.account_id,
            "email": self.email,
            "plan_type": self.plan_type,
            "five_hour": self.five_hour.to_json() if self.five_hour else None,
            "seven_day": self.seven_day.to_json() if self.seven_day else None,
            "scoped": [[name, sample.to_json()] for name, sample in self.scoped],
            "limit_reached": self.limit_reached,
            "allowed": self.allowed,
            "reached_type": self.reached_type,
            "fetched_at": self.fetched_at,
            "credits_balance": self.credits_balance,
            "credits_has": self.credits_has,
            "credits_would_enable": self.credits_would_enable,
            "availability": [[slug, when] for slug, when in self.availability],
        }

    @classmethod
    def from_json(cls, obj: Any) -> CodexAccountQuota | None:
        """Inverse of :meth:`to_json`; ``None`` for anything unusable.

        Total by design, like ``CodexQuota.from_json``: the sidecar is a cache
        whose only job is to make a cold start honest, and a corrupt one must
        cost a missing figure for one poll interval, never a crash on the
        AppKit thread."""
        if not isinstance(obj, Mapping):
            return None
        account_id = _to_str(obj.get("account_id"))
        if not account_id:
            return None
        scoped: list[tuple[str, WindowSample]] = []
        raw_scoped = obj.get("scoped")
        if isinstance(raw_scoped, Sequence) and not isinstance(raw_scoped, (str, bytes)):
            for pair in raw_scoped:
                if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)):
                    continue
                if len(pair) != 2:
                    continue
                label = _to_str(pair[0])
                sample = WindowSample.from_json(pair[1])
                if label and sample is not None:
                    scoped.append((label, sample))
        availability: list[tuple[str, float]] = []
        raw_availability = obj.get("availability")
        if isinstance(raw_availability, Sequence) and not isinstance(
            raw_availability, (str, bytes)
        ):
            for pair in raw_availability:
                if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)):
                    continue
                if len(pair) != 2:
                    continue
                slug = _to_str(pair[0])
                when = _to_float(pair[1])
                if slug and when is not None:
                    availability.append((slug, when))
        raw_has = obj.get("credits_has")
        return cls(
            account_id=account_id,
            email=_to_str(obj.get("email")),
            plan_type=_to_str(obj.get("plan_type")) or None,
            five_hour=WindowSample.from_json(obj.get("five_hour")),
            seven_day=WindowSample.from_json(obj.get("seven_day")),
            scoped=tuple(scoped),
            limit_reached=_to_bool(obj.get("limit_reached"), False),
            allowed=_to_bool(obj.get("allowed"), True),
            reached_type=_to_str(obj.get("reached_type")) or None,
            fetched_at=_to_float(obj.get("fetched_at")) or 0.0,
            # A sidecar written before roadmap 11 simply has none of these keys,
            # and reads back as "not reported" rather than as False/0 - the same
            # forward/backward tolerance every other field here has.
            credits_balance=_to_number(obj.get("credits_balance")),
            credits_has=raw_has if isinstance(raw_has, bool) else None,
            credits_would_enable=_to_bool(obj.get("credits_would_enable"), False),
            availability=tuple(availability),
        )

    # -- rendering ---------------------------------------------------------

    @property
    def out_of_credits(self) -> bool:
        """True when the block is a CREDIT block, not a spent window.

        Two routes, both from the captured Business body: the explicit
        ``rate_limit_reached_type.type == "workspace_owner_credits_depleted"``,
        and the corroborating pair ``credits.has_credits is False`` **with**
        some model saying ``credits_would_enable``. The second needs both
        halves: the probed Pro body also reports ``has_credits: false`` and is
        not blocked by anything, so reading that flag alone would print "out of
        credits" on a perfectly healthy plan.
        """
        if self.reached_type == "workspace_owner_credits_depleted":
            return True
        return self.credits_has is False and self.credits_would_enable

    @property
    def capped_note(self) -> str:
        """``"capped"`` / ``"capped <type>"`` when the PLAN is at its limit,
        or ``"out of credits · Add credits"`` when that limit is a credit one.

        Distinct from every transport note: this one sits *beside* the figures
        because they are still true — the account really is at 100 % of that
        window, and hiding the bar would hide the reason.

        The credits wording is not decoration (roadmap 11): a bare ``capped
        workspace_owner_credits_depleted`` reads as "wait for the reset", and
        the reset is not the fix — the week resets in four days and the
        workspace is still blocked until somebody adds credits. The action is
        the sentence.
        """
        if self.allowed and not self.limit_reached:
            return ""
        if self.out_of_credits:
            return NOTE_OUT_OF_CREDITS
        return f"capped {self.reached_type}" if self.reached_type else "capped"

    @property
    def soonest_reset_at(self) -> float | None:
        """The earliest reset instant this account reports, across every window.

        ``None`` when no window reported one. Windows the endpoint did not
        date simply do not compete; nothing is assumed about them.
        """
        candidates = [
            sample.reset_at
            for sample in (self.five_hour, self.seven_day, *(s for _n, s in self.scoped))
            if sample is not None and sample.reset_at is not None
        ]
        return min(candidates) if candidates else None

    def info_notes(self, *, now: float, pace_note: str = "") -> tuple[str, ...]:
        """The dim lines under this row's header (roadmap 10/11/12).

        Facts read at ``fetched_at``, in the order a person asks for them:
        what is left (``credits 12``), when the blocked model returns
        (``gpt-6-astra back Sep 15``), and where this is heading (``at this
        pace: wall in 6h``, computed by the caller from the sample ring —
        this object holds one reading and cannot know a trend).

        A model whose ``available_at`` has PASSED is dropped here rather than
        at mapping time: the snapshot may be hours old, and "back Sep 15"
        printed on Sep 16 is the bygone-reset bug in a new place.
        """
        notes: list[str] = []
        if self.credits_balance is not None and (
            self.credits_balance > 0 or self.credits_has is True
        ):
            # A zero balance beside ``has_credits: false`` is the vendor saying
            # "credits are not part of this plan" - the probed Pro body sends
            # exactly that - and rendering it as ``credits 0`` would read as a
            # resource at zero, i.e. as a problem, on an account with 81 % of
            # its week left. When the zero IS the problem, `capped_note`
            # already says ``out of credits``.
            notes.append(f"credits {_format_count(self.credits_balance)}")
        for slug, when in self.availability:
            if when <= now:
                continue
            notes.append(f"{slug} back {_format_reset_date(when)}")
        if pace_note:
            notes.append(pace_note)
        return tuple(notes)

    def account_row(
        self,
        *,
        now: float,
        slot: int,
        alias: str,
        is_active: bool,
        note: str = "",
        note_kind: str = "",
        pace_note: str = "",
    ) -> AccountRow:
        """Render as the menu row (SPEC-CODEX 6).

        Follows the ``AccountRow`` conventions frozen in ``contracts``:
        ``vendor=VENDOR_CODEX``, ``switchable=False`` (nothing here is a switch
        target — every switch path reads ``snapshot.accounts``), ``plan_type``
        verbatim, no ``pace_ahead`` (pace comes from ``claude_swap.pace``).
        ``is_active`` here means "the login ``~/.codex`` is using right now",
        which is a fact about the CLI, not an invitation to click.

        Two age verdicts, both derived here from one number:

        * past :data:`CODEX_FETCH_STALE_SECONDS` (15 min, three missed polls)
          ``usage_is_stale`` turns on and the renderer shows the age;
        * past :data:`CODEX_FETCH_EXPIRE_SECONDS` (6 h) the **bars are
          withheld entirely** — percentages, resets and scoped windows all go.
          The row itself stays, carrying its header and its note. This is SPEC
          4.3 in its strongest form: a six-hour-old percentage of a five-hour
          window is not a stale figure, it is a wrong one, and the honest thing
          to show is nothing plus the reason;
        * while a ``warn`` sentinel stands (``relogin``, ``no access``, ``rate
          limited``, ``endpoint error``, ``offline``, ``credential unreadable``)
          the bars are withheld too: a sentinel REPLACES the figure. The
          ``capped`` note (kind ``crit``) is the one exception — the plan really
          is at its limit and the percentage is the evidence, so it sits beside
          the bars. ``app._quota_alarm`` relies on exactly this split: it alarms
          the title only for a warn/crit row that carries no figure.

        A window whose reported reset instant has passed (with a 120 s grace
        for clock skew) is listed in ``expired_windows`` and its reset note
        becomes ``overdue (<clock>)``, exactly as the transcript-derived row
        does — the percentage then describes a window that has ended, so the
        renderer must drop the live ``(!)`` treatment rather than shout about a
        cap that expired.
        """
        age = max(0.0, now - self.fetched_at) if self.fetched_at else None
        withheld = (age is not None and age > CODEX_FETCH_EXPIRE_SECONDS) or (
            bool(note) and note_kind == KIND_WARN
        )

        five_pct: Pct | None = None
        seven_pct: Pct | None = None
        scoped_pcts: list[tuple[str, Pct]] = []
        five_reset: str | None = None
        seven_reset: str | None = None
        scoped_resets: list[tuple[str, str]] = []
        expired: list[str] = []

        if not withheld:
            for key, sample in (("five_hour", self.five_hour), ("seven_day", self.seven_day)):
                if sample is None:
                    continue
                text, is_expired = _reset_note(sample.reset_at, now)
                if is_expired:
                    expired.append(key)
                if key == "five_hour":
                    five_pct, five_reset = sample.used_percent, text
                else:
                    seven_pct, seven_reset = sample.used_percent, text
            for label, sample in self.scoped:
                text, is_expired = _reset_note(sample.reset_at, now)
                if sample.used_percent is not None:
                    scoped_pcts.append((label, sample.used_percent))
                if text:
                    scoped_resets.append((label, text))
                if is_expired:
                    expired.append(label)

        return AccountRow(
            slot=slot,
            alias=alias or self.display_name,
            email=self.email,
            is_active=is_active,
            five_hour_pct=five_pct,
            seven_day_pct=seven_pct,
            scoped_windows=tuple(scoped_pcts),
            five_hour_resets_at=five_reset,
            seven_day_resets_at=seven_reset,
            scoped_resets_at=tuple(scoped_resets),
            usage_age_seconds=age,
            pace_ahead=(),
            vendor=VENDOR_CODEX,
            switchable=False,
            plan_type=self.plan_type,
            stale_after_seconds=CODEX_FETCH_STALE_SECONDS,
            expired_windows=tuple(expired),
            attention_note=note,
            attention_kind=note_kind,
            # Withheld with the bars, and for the same reason: a credit
            # balance, a return date and a pace were all read at `fetched_at`,
            # so a reading too old to show a percentage is too old to show
            # these either (roadmap 10/11/12 under SPEC 4.3).
            info_notes=() if withheld else self.info_notes(now=now, pace_note=pace_note),
            soonest_reset_at=None if withheld else self.soonest_reset_at,
        )

    @property
    def display_name(self) -> str:
        """Headline for a row the user never aliased.

        The endpoint's own email plus the first 8 of the account id, because
        two of these four accounts share one email and only the id tells them
        apart. The id prefix is not a secret (it is in every request header the
        ChatGPT app itself sends) and 8 characters is enough to distinguish
        four accounts without turning the menu into a hash dump.
        """
        return _display_name(self.email, self.account_id)


def _display_name(email_address: str, account_id: str) -> str:
    """``jane@x.io · 0a1b2c3d``, or just the id prefix when there is no email."""
    prefix = account_id[:8]
    if email_address and prefix:
        return f"{email_address} · {prefix}"
    return email_address or prefix


def _scoped_label(limit_name: str, width_seconds: int | None, taken: set[str]) -> str:
    """Name a window that is neither the week nor the 5-hour bucket.

    Preference order: the vendor's own ``limit_name`` (it knows what the pool
    is), then the width rendered as a duration by ``render.window_minutes_label``
    so an unfamiliar window is still legible (``3600`` -> ``hourly``), then the
    two combined when a name is reused for two widths. ``""`` means "already
    represented" and the sample is dropped — first writer wins.
    """
    width_label = ""
    if width_seconds and width_seconds > 0:
        width_label = window_minutes_label(width_seconds / 60.0)
    if not width_label and width_seconds:
        width_label = f"{width_seconds}s"
    base = limit_name or width_label
    if not base:
        return ""
    if base not in taken:
        return base
    combined = f"{limit_name} {width_label}".strip()
    if combined and combined not in taken:
        return combined
    # Two unnamed plan windows of one width (reachable on the unverified
    # Business shape), or a pool that reuses a name AND a width: number them
    # rather than draw two bars under one label or drop a real figure.
    for n in range(2, 10):
        numbered = f"{base} ({n})"
        if numbered not in taken:
            return numbered
    return ""


def _reset_note(reset_at: float | None, now: float) -> tuple[str, bool]:
    """``(note, expired)`` for a window's reset instant.

    Returns ``("", False)`` when the endpoint reported no reset — an unreported
    reset has no note and the caller must not invent one.

    The note is the BARE clock (``"15:46"``, ``"Sep 12 14:00"``) or
    ``"overdue (<clock>)"``: ``AccountRow.*_resets_at`` carry the clock only,
    exactly as ``CodexQuota.account_row`` fills them, and both renderers add the
    word ``resets`` themselves. Prefixing it here rendered every live row as
    ``resets resets 15:46`` (review, 2026-09-09).
    """
    if reset_at is None:
        return "", False
    clock = _format_reset_clock(reset_at, now)
    expired = now > reset_at + _RESET_GRACE_SECONDS
    if not clock:
        return "", expired
    return (f"overdue ({clock})" if expired else clock), expired


# ---------------------------------------------------------------------------
# 2. Registry — which accounts exist, in what order, enabled or not
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One tracked account. Holds no token, no percentage, no email — the
    alias is the only free text and the user typed it."""

    account_id: str
    alias: str = ""
    enabled: bool = True
    order: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "alias": self.alias,
            "enabled": self.enabled,
            "order": self.order,
        }

    @classmethod
    def from_json(cls, obj: Any, *, fallback_order: int) -> RegistryEntry | None:
        if not isinstance(obj, Mapping):
            return None
        account_id = _to_str(obj.get("account_id")).strip()
        if not account_id:
            return None
        order = _to_int(obj.get("order"))
        return cls(
            account_id=account_id,
            alias=_to_str(obj.get("alias")).strip(),
            enabled=_to_bool(obj.get("enabled"), True),
            order=fallback_order if order is None else order,
        )


class Registry:
    """``codex_accounts.json`` — the user's list, read on every menu repaint.

    Why not ``settings.json``: ``normalize_settings`` keeps flat scalars only
    and drops unknown keys, so per-account state cannot live there without
    weakening that guarantee for everything else.

    Reads are **mtime-gated**: the file is ``stat``ed on every call and parsed
    only when ``(mtime_ns, size)`` moved, so ``quota_rows`` on the worker tick
    costs one ``stat``. Parsing is forgiving — an entry without an id is
    skipped, a junk ``order`` falls back to file order, a corrupt file yields
    ``()`` rather than an exception — because this file is meant to be
    hand-editable and a typo must cost at most a missing row.

    Writes are atomic (``mkstemp`` + ``os.replace``) and 0600.

    Guarded by an ``RLock`` because two threads share one instance: the worker
    calls ``available()`` and ``quota_rows()`` while the poller is inside a
    cycle. The contention is a cache-key comparison, not I/O, and without it a
    torn read of the ``(mtime_ns, size)`` gate would silently re-parse or serve
    a half-updated tuple.
    """

    def __init__(self, path: os.PathLike[str] | str = CODEX_ACCOUNTS_REGISTRY_PATH) -> None:
        self._path = Path(path)
        self._entries: tuple[RegistryEntry, ...] = ()
        self._stat_key: tuple[int, int] | None = None
        self._loaded = False
        self._lock = threading.RLock()
        self.last_error: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        try:
            return self._path.is_file()
        except OSError:  # pragma: no cover - unreadable parent
            return False

    def entries(self) -> tuple[RegistryEntry, ...]:
        """Every tracked account, sorted by ``order`` then id. Never raises."""
        with self._lock:
            try:
                st = self._path.stat()
                key = (st.st_mtime_ns, st.st_size)
            except OSError:
                self._entries = ()
                self._stat_key = None
                self._loaded = True
                return ()
            if self._loaded and key == self._stat_key:
                return self._entries
            self._entries = self._parse()
            self._stat_key = key
            self._loaded = True
            return self._entries

    def enabled_entries(self) -> tuple[RegistryEntry, ...]:
        return tuple(entry for entry in self.entries() if entry.enabled)

    def _parse(self) -> tuple[RegistryEntry, ...]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            self.last_error = f"registry unreadable: {_describe(exc)}"
            return ()
        self.last_error = None
        accounts = raw.get("accounts") if isinstance(raw, Mapping) else None
        if not isinstance(accounts, Sequence) or isinstance(accounts, (str, bytes)):
            return ()
        out: list[RegistryEntry] = []
        seen: set[str] = set()
        for index, item in enumerate(accounts):
            entry = RegistryEntry.from_json(item, fallback_order=index)
            if entry is None or entry.account_id in seen:
                continue  # a duplicated id would render the same account twice
            seen.add(entry.account_id)
            out.append(entry)
        return tuple(sorted(out, key=lambda e: (e.order, e.account_id)))

    def upsert(self, entry: RegistryEntry) -> bool:
        """Add *entry*, or replace the one with the same id. Returns success."""
        with self._lock:
            current = [e for e in self._read_for_write() if e.account_id != entry.account_id]
            current.append(entry)
            return self._write(current)

    def set_enabled(self, account_id: str, flag: bool) -> bool:
        """Flip one account's checkbox. Returns whether anything was written.

        Disabling hides the row and stops the polling; it does **not** delete
        the snapshot sidecar, so re-enabling shows the last reading with its
        true age instead of an empty row pretending to be new.
        """
        with self._lock:
            current = self._read_for_write()
            if not any(entry.account_id == account_id for entry in current):
                return False
            updated = [
                RegistryEntry(e.account_id, e.alias, flag, e.order)
                if e.account_id == account_id
                else e
                for e in current
            ]
            return self._write(updated)

    def _read_for_write(self) -> list[RegistryEntry]:
        """Re-read from disk, ignoring the mtime gate: another process (the
        ``adopt`` CLI, a hand edit) may have written since our last read, and a
        read-modify-write from a stale cache would silently drop its work."""
        self._loaded = False
        self._stat_key = None
        return list(self.entries())

    def _write(self, entries: Iterable[RegistryEntry]) -> bool:
        ordered = sorted(entries, key=lambda e: (e.order, e.account_id))
        payload = {
            "version": _REGISTRY_VERSION,
            "accounts": [entry.to_json() for entry in ordered],
        }
        ok = _atomic_write_json(self._path, payload)
        if ok:
            # Force the next entries() to re-read: our own write changed the
            # file, and trusting the cache here would hide it from ourselves.
            self._loaded = False
            self._stat_key = None
        else:
            self.last_error = f"registry not written: {self._path.name}"
        return ok


def _atomic_write_json(path: Path, payload: Any) -> bool:
    """Temp file + ``os.replace``, 0600. Never raises; returns success.

    ``mkstemp`` already creates the file 0600 and in the destination directory
    so the replace is atomic on the same filesystem; the explicit ``chmod``
    states the intent for the next reader of this code. ``fsync`` is skipped
    for the same reason the indexers skip it: ``os.replace`` gives readers
    all-or-nothing, and a power cut costs one poll interval.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.tmp.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover - already gone
                pass
            raise
        return True
    except (OSError, TypeError, ValueError) as exc:
        _log(f"codex accounts: write failed for {path.name}: {_describe(exc)}")
        return False


# ---------------------------------------------------------------------------
# 3. Credentials — read-only, refused when they leak
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Credential:
    """One account's stored login, held only for the length of one request.

    ``__repr__`` is overridden: the default dataclass repr would print the
    bearer token, and a repr reaches a log the moment anybody writes
    ``_log(f"{cred}")`` or an exception carries the object. The one place the
    token is allowed to appear is the ``Authorization`` header of the request
    it authorises.
    """

    account_id: str
    access_token: str
    exp: float | None = None
    email: str | None = None
    plan_type: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - exercised via the canary test
        return (
            f"Credential(account_id={self.account_id[:8]!r}, access_token=<redacted>, "
            f"exp={self.exp!r}, plan_type={self.plan_type!r})"
        )

    def expired(self, now: float) -> bool:
        """True when the token cannot be used any more. Unknown ``exp`` is NOT
        expired: a token whose claims we could not decode still deserves its
        one request, and the endpoint's 401 is the authoritative answer."""
        return self.exp is not None and self.exp <= now

    def relogin_seconds(self, now: float) -> float | None:
        """Seconds until ``exp``, or ``None`` when it is unknown or passed."""
        if self.exp is None:
            return None
        remaining = self.exp - now
        return remaining if remaining > 0 else None


class CredentialStore:
    """The four widget-owned ``CODEX_HOME`` dirs, plus ``~/.codex`` as a mirror.

    Two very different jobs, deliberately in one class because they share the
    "never write, never trust" discipline:

    * :meth:`discover` / :meth:`read` — our own credential dirs, each named by
      the ``chatgpt_account_id`` the file itself claims. **The directory name
      must equal ``tokens.account_id``**: that is the whole identity check, and
      it is what makes a renamed or copied dir fail loudly instead of showing
      one account's quota under another's alias.
    * :meth:`active_account_id` — ``~/.codex/auth.json``, opened read-only, for
      exactly one field. The ChatGPT desktop app owns that file and rewrites it
      on its own schedule; the widget marks a row from it and never touches it.

    A credential file with any group or world bit set is **refused**, not used:
    a token readable by another user is a token to rotate, and quietly polling
    with it would hide that.
    """

    def __init__(
        self,
        accounts_dir: os.PathLike[str] | str = CODEX_ACCOUNTS_DIR,
        *,
        auth_path: os.PathLike[str] | str = CODEX_AUTH_PATH,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir = Path(accounts_dir)
        self._auth_path = Path(auth_path)
        self._clock = clock
        self.last_error: str | None = None
        self._active_id: str | None = None
        self._active_seen_at: float = 0.0
        self._active_key: tuple[int, int] | None = None

    @property
    def accounts_dir(self) -> Path:
        return self._dir

    @property
    def auth_path(self) -> Path:
        return self._auth_path

    # -- our own credential dirs ------------------------------------------

    def discover(self) -> dict[str, Path]:
        """``{account_id: auth.json}`` for every dir that proves its own name.

        A dir whose ``auth.json`` claims a different id is skipped with a
        recorded reason rather than adopted under the name on the box — see the
        class docstring.
        """
        found: dict[str, Path] = {}
        problems: list[str] = []
        try:
            children = sorted(self._dir.iterdir())
        except OSError:
            return {}
        for child in children:
            if not child.is_dir():
                continue
            auth = child / "auth.json"
            if not auth.is_file():
                continue
            claimed = _account_id_in(auth)
            if claimed is None:
                problems.append(f"{child.name}: no tokens.account_id")
                continue
            if claimed != child.name:
                problems.append(f"{child.name}: auth.json claims {claimed[:8]}")
                continue
            found[claimed] = auth
        self.last_error = "; ".join(problems) if problems else None
        return found

    def read(self, account_id: str) -> Credential | None:
        """The credential for *account_id*, or ``None`` with a reason.

        ``None`` for: no such dir, no ``auth.json``, unparseable JSON, no
        access token, an id that does not match the dir, or a mode with any
        group/world bit. The reason lands in :attr:`last_error` and from there
        in ``diagnostics()`` — a refused credential must say why, or the row
        just sits still forever.
        """
        auth = self._dir / account_id / "auth.json"
        try:
            st = auth.stat()
        except OSError as exc:
            self.last_error = f"{account_id[:8]}: auth.json unreadable ({type(exc).__name__})"
            return None
        mode = stat_module.S_IMODE(st.st_mode)
        if mode & 0o077:
            self.last_error = (
                f"{account_id[:8]}: auth.json is {mode:04o}, refusing to use a "
                "credential other users can read (chmod 600)"
            )
            return None
        try:
            raw = json.loads(auth.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            self.last_error = f"{account_id[:8]}: auth.json unparseable ({type(exc).__name__})"
            return None
        tokens = raw.get("tokens") if isinstance(raw, Mapping) else None
        tokens = tokens if isinstance(tokens, Mapping) else {}
        claimed = _to_str(tokens.get("account_id"))
        access = _to_str(tokens.get("access_token"))
        if not access:
            self.last_error = f"{account_id[:8]}: auth.json has no access token"
            return None
        if claimed and claimed != account_id:
            self.last_error = f"{account_id[:8]}: auth.json claims {claimed[:8]}"
            return None
        claims = decode_jwt_claims(access)
        self.last_error = None
        return Credential(
            account_id=account_id,
            access_token=access,
            exp=claims.get("exp"),
            email=claims.get("email"),
            plan_type=claims.get("plan_type"),
        )

    # -- the read-only ~/.codex mirror ------------------------------------

    def active_account_id(self) -> str | None:
        """Which of the tracked accounts the Codex CLI is logged in as.

        One ``stat`` per call, gated on ``(mtime_ns, size)`` so the 60 s
        accounts tick re-parses only when the desktop app actually rewrote the
        file. Exactly one field is read (``tokens.account_id``); the token in
        that file is never touched, and the file is never opened for writing.

        set-by: a successful read. cleared-by: a read that returns a different
        id, or the grace expiring. ages-out: yes — an unreadable or corrupt
        file keeps the previous answer for
        :data:`CODEX_ACTIVE_GRACE_SECONDS` (the app rewrites it
        non-atomically and a mid-write read is normal), then ``None``, because
        "I saw this ten minutes ago" is not "this is true now".
        rehydrated: no, deliberately — the active login is cheap to re-read and
        persisting it would let a stale marker survive a reboot.
        producer off: unaffected; this is one ``stat``, and it is what tells
        the merge rule whether a live row may speak for the corpus.
        """
        now = self._clock()
        try:
            st = self._auth_path.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return self._active_within_grace(now)
        if key == self._active_key and self._active_id is not None:
            self._active_seen_at = now
            return self._active_id
        claimed = _account_id_in(self._auth_path)
        if claimed is None:
            return self._active_within_grace(now)
        self._active_key = key
        self._active_id = claimed
        self._active_seen_at = now
        return claimed

    def _active_within_grace(self, now: float) -> str | None:
        if self._active_id is None:
            return None
        if now - self._active_seen_at <= CODEX_ACTIVE_GRACE_SECONDS:
            return self._active_id
        self._active_id = None
        self._active_key = None
        return None


def _account_id_in(auth_path: Path) -> str | None:
    """``tokens.account_id`` from an ``auth.json``, or ``None``. Read-only.

    The single place any ``auth.json`` is parsed for identity. It reads the
    whole file (there is no way to read one JSON field without it) and returns
    exactly one string — the token in the same object is never returned to a
    caller that did not ask :meth:`CredentialStore.read` for it.
    """
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    tokens = raw.get("tokens") if isinstance(raw, Mapping) else None
    if not isinstance(tokens, Mapping):
        return None
    return _to_str(tokens.get("account_id")) or None


# ---------------------------------------------------------------------------
# 4. Transport
# ---------------------------------------------------------------------------


class HttpResponse(NamedTuple):
    """What a transport returns. A 4xx/5xx is a *response*, not an exception —
    only a failure to reach the endpoint at all raises (``OSError``), which is
    what lets the poller tell ``offline`` from ``endpoint error``."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class UsageTransport(Protocol):
    """The one seam between this module and the network.

    Everything above is pure or filesystem-only, so every poll rule, ladder and
    note in this file is tested against a ``FakeTransport`` with a fake clock
    and real files — no monkeypatched ``urllib``, no live request in the suite.
    """

    def get(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:  # pragma: no cover - protocol
        ...


class RefreshTransport(Protocol):
    """The *second*, optional half of the seam — one form POST (roadmap 13).

    Deliberately a separate protocol rather than a method on
    :class:`UsageTransport`: a quota reader needs only ``get``, and every
    existing double in the suite is a legitimate transport that cannot post.
    :class:`TokenRefresher` therefore probes for ``post_form`` with ``getattr``
    and reports ``unsupported`` when it is absent, instead of raising an
    ``AttributeError`` from inside a poll cycle.
    """

    def post_form(
        self, url: str, data: Mapping[str, str], timeout: float
    ) -> HttpResponse:  # pragma: no cover - protocol
        ...


class UrllibTransport:
    """Production transport: stdlib only, no redirects, bounded body.

    * **No redirects.** A 3xx from an authenticated API is either a captive
      portal or a login wall; following it would send the bearer token to
      wherever the redirect points. The handler refuses, urllib raises, and the
      3xx surfaces as an ``endpoint error``.
    * **Two-phase timeout.** ``urllib`` exposes one socket timeout, so the
      connection class re-arms the socket with the read timeout after connect:
      10 s to get a connection, 15 s to get an answer. If that subclassing ever
      stops matching the stdlib the opener falls back to a single timeout —
      degraded, never broken.
    * **256 KB cap.** A body larger than that is not this endpoint answering.
    """

    def __init__(
        self,
        *,
        connect_timeout: float = _CONNECT_TIMEOUT,
        read_timeout: float = _READ_TIMEOUT,
        body_cap: int = _BODY_CAP_BYTES,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._body_cap = body_cap
        self._opener = self._build_opener()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = [_NoRedirect()]
        try:
            handlers.append(_TwoPhaseHTTPSHandler(self._read_timeout))
        except Exception as exc:  # pragma: no cover - stdlib shape changed
            _log(f"codex accounts: single-timeout transport ({_describe(exc)})")
        return urllib.request.build_opener(*handlers)

    def get(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        request = urllib.request.Request(url, method="GET")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with self._opener.open(request, timeout=timeout or self._connect_timeout) as resp:
                return HttpResponse(
                    status=int(getattr(resp, "status", 0) or 0),
                    headers=dict(resp.headers.items()),
                    body=resp.read(self._body_cap + 1)[: self._body_cap],
                )
        except urllib.error.HTTPError as exc:
            # An HTTP status is an answer; only a failure to reach the endpoint
            # is an exception. Reading the error body is what tells a JSON 403
            # ("no access") from an HTML one (a challenge = endpoint error).
            try:
                body = exc.read(self._body_cap + 1)[: self._body_cap]
            except Exception:  # pragma: no cover - body already consumed
                body = b""
            return HttpResponse(status=int(exc.code), headers=dict(exc.headers.items()), body=body)
        except http.client.HTTPException as exc:
            # Not an OSError subclass, but it means exactly what one means here.
            raise OSError(type(exc).__name__) from exc

    def post_form(
        self, url: str, data: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        """One ``application/x-www-form-urlencoded`` POST — the refresh grant.

        Same three rules as :meth:`get` and for the same reasons: no redirect
        (a 3xx here would forward a *refresh* token, which is worse than
        forwarding an access token), an HTTP status is a response and only an
        unreachable endpoint raises, and the body is capped. The encoded body
        holds the refresh token, so it is bound to a local, passed once and
        dropped in ``finally`` — it is never put in a log line, an exception
        message or a retry buffer.
        """
        payload = urllib.parse.urlencode(dict(data)).encode("ascii")
        request = urllib.request.Request(url, data=payload, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("Accept", "application/json")
        try:
            with self._opener.open(request, timeout=timeout or self._connect_timeout) as resp:
                return HttpResponse(
                    status=int(getattr(resp, "status", 0) or 0),
                    headers=dict(resp.headers.items()),
                    body=resp.read(self._body_cap + 1)[: self._body_cap],
                )
        except urllib.error.HTTPError as exc:
            # The error body is the whole point: ``invalid_grant`` there is the
            # difference between "log in again" and "the network wobbled".
            try:
                body = exc.read(self._body_cap + 1)[: self._body_cap]
            except Exception:  # pragma: no cover - body already consumed
                body = b""
            return HttpResponse(status=int(exc.code), headers=dict(exc.headers.items()), body=body)
        except http.client.HTTPException as exc:
            raise OSError(type(exc).__name__) from exc
        finally:
            del payload, request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect; urllib then raises the 3xx as an ``HTTPError``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


def _TwoPhaseHTTPSHandler(read_timeout: float) -> urllib.request.HTTPSHandler:  # noqa: N802
    """An ``HTTPSHandler`` whose sockets get *read_timeout* after connecting.

    Factory rather than a class so the per-instance read timeout can be closed
    over: ``do_open`` constructs the connection itself and passes only
    ``req.timeout``.
    """

    class _Connection(http.client.HTTPSConnection):
        def connect(self) -> None:
            super().connect()
            try:
                self.sock.settimeout(read_timeout)
            except (AttributeError, OSError):  # pragma: no cover - defensive
                pass

    class _Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):  # noqa: ANN001, D102
            return self.do_open(_Connection, req, context=self._context)

    return _Handler()


# ---------------------------------------------------------------------------
# 4a. Token refresh — roadmap 13, DEFAULT OFF (SPEC-CODEX 6.4)
# ---------------------------------------------------------------------------

CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
"""OpenAI's OAuth token endpoint. Only ever reached from
:meth:`TokenRefresher.refresh`, which is only ever called when
``codex_refresh_enabled`` is on."""

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
"""The Codex CLI's **public** OAuth client id — the same string that already
sits in every ``auth.json`` this module reads. A public client id is not a
secret (it identifies the app, it does not authorise anything), so unlike a
token it may appear in source, in a log line and in this docstring."""

REFRESH_PROACTIVE_SECONDS = 24 * 3600.0
"""How far ahead of ``exp`` a healthy poll may rotate the token.

24 h, not 48 h (the countdown threshold) and not 1 h: the countdown exists to
tell a human to act, so a refresh that fired at the same moment would make the
sentinel a liar; and an hour would leave no room for a machine that is asleep
between the two polls. One day is roughly two hundred poll opportunities."""

REFRESH_OK = "ok"
REFRESH_RELOGIN = "relogin"
REFRESH_FAILED = "failed"
REFRESH_UNSUPPORTED = "unsupported"
"""The four outcomes. ``relogin`` is reserved for ``invalid_grant`` — the one
answer that means the family is gone and no retry can help; everything else
(offline, 5xx, a body that is not JSON, an unwritable file) is ``failed``, and
a failed refresh changes nothing: the old access token is still valid until
``exp`` and the row keeps counting down to the manual relogin."""


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """What one grant attempt produced. ``credential`` is set only on ``ok``,
    and it is re-read *from disk* — so holding one is proof the rotation was
    persisted, not merely received."""

    status: str
    detail: str = ""
    credential: Credential | None = None


def refresh_due(credential: Credential, now: float) -> bool:
    """Whether *credential* is inside the proactive window.

    An unknown ``exp`` is **not** due: we would be guessing at a schedule, and
    the 401 path already covers a token that turns out to be dead. An ``exp``
    that has already passed IS due — a refresh token outlives the access token
    it mints, so the last chance to avoid a browser login is exactly here.
    """
    if credential.exp is None:
        return False
    return (credential.exp - now) < REFRESH_PROACTIVE_SECONDS


def _iso_utc(epoch: float) -> str:
    """``2026-09-10T11:40:00Z`` — the shape ``codex login`` writes."""
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_auth_for_rotation(auth: Path) -> tuple[dict[str, Any] | None, str]:
    """The whole ``auth.json`` as a dict, or ``(None, reason)``.

    The whole file, because the rotation must write back every key the Codex
    CLI put there (``OPENAI_API_KEY``, ``auth_mode``, anything a future version
    adds) — a refresh that silently dropped a field would break the CLI for the
    account it was trying to help. The same mode check as
    :meth:`CredentialStore.read`: a credential another user can read is one to
    rotate by hand, not one to rotate in place.
    """
    try:
        st = auth.stat()
    except OSError as exc:
        return None, f"auth.json unreadable ({type(exc).__name__})"
    if stat_module.S_IMODE(st.st_mode) & 0o077:
        return None, f"auth.json is {stat_module.S_IMODE(st.st_mode):04o}, refusing to rotate it"
    try:
        raw = json.loads(auth.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        return None, f"auth.json unparseable ({type(exc).__name__})"
    if not isinstance(raw, Mapping):
        return None, "auth.json is not an object"
    return dict(raw), ""


class TokenRefresher:
    """One OAuth refresh grant, persisted before it is used (roadmap 13).

    The whole safety story is four rules, and each one is a test:

    1. **Persist before use.** The rotated ``refresh_token`` /
       ``access_token`` / ``id_token`` are written to the account's
       ``auth.json`` (temp file + ``os.replace``, 0600) *before* this method
       returns, and the :class:`Credential` it returns is then **re-read from
       that file**. There is no code path in which a token that only exists in
       memory authorises a request: a crash between the POST and the write
       costs one grant, while the reverse order would cost the account.
    2. **No replay, ever.** The superseded refresh token is overwritten in
       place. No ``.prev`` file, no in-memory copy kept for a retry, no
       "try the old one if the new one fails". If OpenAI's reuse detection is
       armed on this client, the only way to trip it is to ask on purpose.
    3. **``invalid_grant`` is terminal.** It means the family is gone; a retry
       can only make it worse, so the outcome is ``relogin`` and the caller
       shows the sentinel it would have shown anyway a day later.
    4. **Nothing about a token is logged.** The refresh token goes from the
       file into one form body and out of scope; log lines carry the alias, the
       outcome and how much life the new token has — never a value, a length or
       a prefix.

    Constructed unconditionally by :class:`CodexAccountsSource` (it is three
    references and no I/O) and called only while ``codex_refresh_enabled`` is
    on. :attr:`posts` counts every POST it has made, which is how "off means
    zero requests" is asserted rather than described.
    """

    def __init__(
        self,
        transport: Any,
        credentials: CredentialStore,
        *,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = _log,
        url: str = CODEX_TOKEN_URL,
        client_id: str = CODEX_OAUTH_CLIENT_ID,
        timeout: float = _CONNECT_TIMEOUT,
    ) -> None:
        self._transport = transport
        self._credentials = credentials
        self._clock = clock
        self._log = log
        self._url = url
        self._client_id = client_id
        self._timeout = timeout
        self.posts = 0
        """POSTs attempted, including ones that raised. Never reset."""

    def refresh(self, account_id: str, *, label: str = "") -> RefreshOutcome:
        """Rotate one account's tokens. Never raises; returns the verdict."""
        label = label or account_id[:8]
        post = getattr(self._transport, "post_form", None)
        if not callable(post):
            return self._failed(label, "transport cannot post a form")

        auth = Path(self._credentials.accounts_dir) / account_id / "auth.json"
        raw, problem = _read_auth_for_rotation(auth)
        if raw is None:
            return self._failed(label, problem)
        tokens = raw.get("tokens")
        tokens = dict(tokens) if isinstance(tokens, Mapping) else {}
        if not _to_str(tokens.get("refresh_token")):
            return self._failed(label, "auth.json has no refresh token")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": _to_str(tokens.get("refresh_token")),
            "client_id": self._client_id,
        }
        self.posts += 1
        try:
            response = post(self._url, data, self._timeout)
        except OSError as exc:
            # Type name only, exactly as the usage GET does: a URLError message
            # can carry the host and, behind a proxy, the URL.
            return self._failed(label, f"unreachable ({type(exc).__name__})")
        finally:
            del data

        body = _decode_json(response.body)
        if response.status != 200:
            error = _to_str(body.get("error")) if isinstance(body, Mapping) else ""
            if error == "invalid_grant":
                self._log(f"codex {label}: refresh refused (invalid_grant) - relogin")
                return RefreshOutcome(REFRESH_RELOGIN, "invalid_grant")
            return self._failed(label, f"HTTP {response.status}" + (f" {error}" if error else ""))
        if not isinstance(body, Mapping):
            return self._failed(label, "grant body was not JSON")
        if not _to_str(body.get("access_token")):
            return self._failed(label, "grant carried no access_token")

        tokens["access_token"] = _to_str(body.get("access_token"))
        # A grant that rotates the refresh token replaces it; one that does not
        # leaves the existing one in place. Never blank it: an empty
        # refresh_token would turn the next proactive tick into a hard relogin.
        if _to_str(body.get("refresh_token")):
            tokens["refresh_token"] = _to_str(body.get("refresh_token"))
        if _to_str(body.get("id_token")):
            tokens["id_token"] = _to_str(body.get("id_token"))
        payload = dict(raw)
        payload["tokens"] = tokens
        payload["last_refresh"] = _iso_utc(self._clock())
        written = _atomic_write_json(auth, payload)
        del payload, tokens, raw, body, response
        if not written:
            # The old refresh token is already spent and the new one could not
            # be stored. Say so loudly: this is the one failure a person must
            # fix by hand (a full disk, a read-only home), and pretending the
            # refresh worked would use a token no restart could ever find.
            return self._failed(label, "rotated tokens could not be persisted")

        credential = self._credentials.read(account_id)
        if credential is None:
            return self._failed(
                label, self._credentials.last_error or "auth.json unreadable after rotation"
            )
        remaining = credential.relogin_seconds(self._clock())
        life = _format_duration(remaining) if remaining is not None else "unknown"
        self._log(f"codex {label}: token refreshed, {life} of life")
        return RefreshOutcome(REFRESH_OK, "", credential)

    def _failed(self, label: str, detail: str) -> RefreshOutcome:
        self._log(f"codex {label}: refresh failed ({detail})")
        return RefreshOutcome(REFRESH_FAILED, detail)


# ---------------------------------------------------------------------------
# 5. Per-account fetch state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FetchState:
    """The poller's memory of one account. Mutable, guarded by the source lock.

    ``consecutive_failures`` is deliberately **not** persisted: a ladder is a
    statement about the last few minutes of network, and restoring "you have
    failed 5 times" across a restart would put a fresh process into a 30-minute
    backoff over a problem that ended yesterday. The *note* is persisted,
    because a dead credential is still dead after a restart.
    """

    consecutive_failures: int = 0
    next_due_wall: float = 0.0
    last_status: int | None = None
    note: str = ""
    note_kind: str = ""
    failure_class: str = ""
    """Which ladder the counter belongs to (``"rate"`` for 429, ``"endpoint"``
    for 5xx/garbage/offline). The count restarts when the class changes, so
    two 429s cannot make the very first 502 print ``endpoint error``."""
    credential_sig: tuple[int, int] | None = None
    """``(mtime_ns, size)`` of the account's ``auth.json`` at the last poll. A
    standing warn sentinel is cleared the moment this moves: a fresh ``codex
    login`` must show within one tick, not after a 30-minute backoff."""

    def clear_note(self) -> None:
        self.note = ""
        self.note_kind = ""

    def set_note(self, note: str, kind: str) -> None:
        self.note = note
        self.note_kind = kind


# ---------------------------------------------------------------------------
# 6. The source
# ---------------------------------------------------------------------------


class CodexAccountsSource:
    """The duck-typed quota source the worker collects rows from.

    Satisfies the same informal contract as ``CodexIndexer``
    (``vendor`` / ``available()`` / ``quota_rows()``), plus the lifecycle the
    app drives: :meth:`start` from the worker, :meth:`stop` from
    ``BackgroundWorker.stop`` and ``_on_quit``, :meth:`pause` when
    ``codex_tracking_enabled`` goes off, :meth:`force_due` on *Refresh now*.

    Every collaborator is injected, so the whole poll machine is tested with
    real files in a ``TemporaryDirectory``, a fake clock and a fake transport::

        CodexAccountsSource(registry=Registry(tmp / "codex_accounts.json"),
                            credentials=CredentialStore(tmp / "codex-accounts",
                                                        auth_path=tmp / "auth.json"),
                            transport=FakeTransport(...),
                            snapshots_path=tmp / "codex_quota_snapshots.json",
                            settings=lambda: settings, clock=clock.time,
                            monotonic=clock.monotonic, sleeper=clock.sleep)
    """

    vendor: Vendor = VENDOR_CODEX

    def __init__(
        self,
        *,
        registry: Registry,
        credentials: CredentialStore,
        transport: UsageTransport,
        snapshots_path: os.PathLike[str] | str = CODEX_QUOTA_SNAPSHOTS_PATH,
        settings: Callable[[], Mapping[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Any] | None = None,
        log: Callable[[str], None] = _log,
        refresher: TokenRefresher | None = None,
    ) -> None:
        self._registry = registry
        self._credentials = credentials
        self._transport = transport
        self._snapshots_path = Path(snapshots_path)
        self._settings = settings or (lambda: SETTINGS_DEFAULTS)
        self._clock = clock
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._log = log
        self._refresher = refresher or TokenRefresher(
            transport, credentials, clock=clock, log=log
        )
        """Built unconditionally (three references, no I/O) and called only
        while ``codex_refresh_enabled`` is on — see :meth:`_refresh_enabled`.
        Constructing it here rather than on demand is what lets a test inject
        one and count its POSTs, including the count that must stay zero."""

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._paused = False
        self._loaded = False
        self._snapshots: dict[str, CodexAccountQuota] = {}
        self._samples: dict[str, list[tuple[float, float]]] = {}
        """Per-account ring of ``(fetched_at, weekly used_percent)``, oldest
        first, at most :data:`PACE_RING_SIZE` long (roadmap 10). Recorded on
        every healthy 200 whatever ``codex_pace_forecast_enabled`` says — the
        setting gates the NOTE, not the memory, so switching the forecast on
        does not start an hour of silence."""
        self._states: dict[str, FetchState] = {}
        self._last_wall: float | None = None
        self._last_monotonic: float | None = None
        self._cycles = 0
        self._requests = 0
        self._notes: list[str] = []
        """Transient diagnostics lines (id mismatch, wake, untracked login).
        Bounded, newest last; never a token, header or body."""
        self.last_error: str | None = None

    # -- the source contract ----------------------------------------------

    def available(self) -> bool:
        """True when this source has something to say AND is switched on.

        Both halves matter: ``codex_live_quota_enabled`` off is the rollback
        switch (no thread, no request, menu byte-for-byte as before), and a
        registry with no enabled account has nothing to render even when the
        setting is on.
        """
        try:
            if not bool(self._settings().get("codex_live_quota_enabled", False)):
                return False
            return bool(self._registry.enabled_entries())
        except Exception as exc:  # a probe must never break the worker tick
            self.last_error = _describe(exc)
            return False

    def quota_rows(self) -> tuple[AccountRow, ...]:
        """One row per enabled account, in registry order. Cheap by contract.

        No network, no credential read: a lock, one ``stat`` of the registry
        and one of ``~/.codex/auth.json``. Slots are **negative** (``-1, -2,
        ...``) so they can never collide with a claude-swap slot (1+) or with
        the transcript-derived Codex pseudo-account (0) that
        ``merge_quota_rows`` arbitrates against.

        An enabled account with no snapshot renders as a real row carrying
        ``awaiting first reading`` rather than being hidden: the user enabled
        it, and a row that silently does not exist is indistinguishable from a
        row that is broken.
        """
        with self._lock:
            self._ensure_loaded()
            now = self._clock()
            active = self._active_id_locked()
            rows: list[AccountRow] = []
            for index, entry in enumerate(self._registry.enabled_entries()):
                slot = -(index + 1)
                state = self._states.get(entry.account_id)
                snapshot = self._snapshots.get(entry.account_id)
                note = state.note if state else ""
                kind = state.note_kind if state else ""
                if not note and snapshot is None:
                    note, kind = NOTE_PENDING, KIND_INFO
                is_active = bool(active) and entry.account_id == active
                if snapshot is None:
                    rows.append(
                        AccountRow(
                            slot=slot,
                            alias=entry.alias or _display_name("", entry.account_id),
                            email="",
                            is_active=is_active,
                            vendor=VENDOR_CODEX,
                            switchable=False,
                            stale_after_seconds=CODEX_FETCH_STALE_SECONDS,
                            attention_note=note,
                            attention_kind=kind,
                        )
                    )
                else:
                    rows.append(
                        snapshot.account_row(
                            now=now,
                            slot=slot,
                            alias=entry.alias,
                            is_active=is_active,
                            note=note,
                            note_kind=kind,
                            pace_note=self._pace_note_locked(entry.account_id, snapshot, now),
                        )
                    )
            return tuple(rows)

    def active_account_id(self) -> str | None:
        """Which tracked account ``~/.codex`` is logged in as, or ``None``.

        Delegates to the store, whose ``(mtime_ns, size)`` gate *is* the cache
        — a second call in the same tick costs one ``stat`` and no parse.
        """
        with self._lock:
            return self._active_id_locked()

    def _active_id_locked(self) -> str | None:
        try:
            active = self._credentials.active_account_id()
        except Exception as exc:  # pragma: no cover - stat only
            self.last_error = _describe(exc)
            return None
        if active and not any(
            entry.account_id == active for entry in self._registry.entries()
        ):
            self._note(f"active login ({active[:8]}) is not tracked")
            return None
        return active

    def diagnostics(self) -> tuple[str, ...]:
        """Lines for ``_diagnostic_items`` and ``--dry-run``.

        Aliases, id prefixes, statuses and schedules only — the same discipline
        as the log: no token, no header, no email, no body.
        """
        with self._lock:
            entries = self._registry.entries()
            enabled = [entry for entry in entries if entry.enabled]
            now = self._clock()
            live = bool(self._settings().get("codex_live_quota_enabled", False))
            lines = [
                f"codex accounts: {len(entries)} tracked, {len(enabled)} enabled, "
                f"live quota {'on' if live else 'off'}"
                f"{', paused' if self._paused else ''}"
                f"{', polling' if self._thread is not None and self._thread.is_alive() else ''}",
                f"credential dir: {self._credentials.accounts_dir}",
                f"registry: {self._registry.path}",
            ]
            for entry in enabled:
                state = self._states.get(entry.account_id)
                snapshot = self._snapshots.get(entry.account_id)
                label = entry.alias or entry.account_id[:8]
                status = "never polled" if state is None or state.last_status is None else str(
                    state.last_status
                )
                if state is None:
                    due = "due now"
                else:
                    delta = state.next_due_wall - now
                    due = "due now" if delta <= 0 else f"due in {_format_duration(delta)}"
                age = (
                    f", read {_format_duration(max(0.0, now - snapshot.fetched_at))} ago"
                    if snapshot is not None and snapshot.fetched_at
                    else ", no reading"
                )
                note = f", {state.note}" if state is not None and state.note else ""
                lines.append(f"  {label}: {status}, {due}{age}{note}")
            for line in self._notes[-4:]:
                lines.append(f"  ! {line}")
            if self._credentials.last_error:
                lines.append(f"  ! {self._credentials.last_error}")
            if self.last_error:
                lines.append(f"  ! {self.last_error}")
            return tuple(lines)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the poller. Idempotent, daemon, named for ``ps``.

        Gated on :meth:`available`: a machine with zero enabled credentials, or
        with the feature switched off, grows NO thread — a Claude-only install
        must be byte-for-byte and thread-for-thread what it was. The worker
        retries on every tick, so the thread appears the moment the source
        becomes available and never twice.
        """
        if not self.available():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="cc-usage-codex-poll", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        """Ask the poller to finish and wait briefly. Returns whether it is gone.

        The default sleeper is the stop event's ``wait``, so a cycle sitting in
        the 20 s inter-account gap returns immediately instead of holding up
        quit for the rest of the gap.
        """
        self._stop.set()
        with self._lock:
            thread, self._thread = self._thread, None
        if thread is None:
            return True
        if timeout > 0 and thread.is_alive():
            thread.join(timeout=timeout)
        return not thread.is_alive()

    def pause(self, flag: bool) -> None:
        """Stop making requests without losing state (``codex_tracking_enabled``).

        The thread stays, the sidecar stays, the rows stay with their true age.
        Un-pausing does not force a poll: the existing schedule resumes, so
        flipping the switch twice cannot be used to hammer the endpoint.
        """
        with self._lock:
            self._paused = bool(flag)

    def force_due(self) -> None:
        """Mark every account due now (*Refresh now*). Still ≥20 s apart."""
        with self._lock:
            for state in self._states.values():
                state.next_due_wall = 0.0

    # -- the loop ----------------------------------------------------------

    def _loop(self) -> None:
        """Poller thread body, wrapped like ``app.BackgroundWorker._loop``.

        ``except BaseException`` short of the stop event, and for the same
        reason it is written that way there: a ``SystemExit`` raised deep in a
        library once killed the worker thread for the life of the process and
        the menu painted its last title forever. Anything short of the stop
        event degrades to a diagnostics line and the schedule survives.
        """
        while not self._stop.is_set():
            try:
                self.run_cycle_once()
            except BaseException as exc:  # noqa: BLE001 - see docstring
                if isinstance(exc, KeyboardInterrupt):
                    raise
                self.last_error = _describe(exc)
                self._log(f"codex poll cycle failed: {type(exc).__name__}")
            if self._stop.wait(self._sleep_seconds()):
                break

    def _sleep_seconds(self) -> float:
        """How long until the next cycle: the earliest due time, bounded.

        Capped at 30 s even when nothing is due so the wake detector runs soon
        after the lid opens; floored at 1 s so a due-in-the-past account cannot
        turn the loop into a spin.
        """
        with self._lock:
            now = self._clock()
            due = [state.next_due_wall for state in self._states.values()]
        if not due:
            return _CYCLE_SLEEP_MAX_SECONDS
        return max(1.0, min(_CYCLE_SLEEP_MAX_SECONDS, min(due) - now))

    def run_cycle_once(self) -> None:
        """One synchronous poll cycle: every due account, in registry order.

        Public because the tests drive it directly — the thread adds nothing to
        the logic and a test that has to sleep to observe a schedule is a test
        that will flake on a loaded machine.
        """
        if self._stop.is_set():
            return
        with self._lock:
            self._ensure_loaded()
            paused = self._paused
        if paused or not self.available():
            return

        now = self._clock()
        self._detect_wake(now)
        interval = self._interval_seconds()
        include_extra = bool(self._settings().get("codex_show_extra_limits", False))
        entries = self._registry.enabled_entries()

        polled = 0
        for entry in entries:
            if self._stop.is_set():
                return
            with self._lock:
                state = self._states.setdefault(entry.account_id, FetchState())
                if state.note and state.note_kind == KIND_WARN:
                    sig = self._credential_sig(entry.account_id)
                    if sig is not None and sig != state.credential_sig:
                        # The credential file moved under a standing sentinel:
                        # a fresh login landed. Forget the verdict, read now.
                        state.clear_note()
                        state.credential_sig = sig
                        state.next_due_wall = 0.0
                due = state.next_due_wall <= self._clock()
            if not due:
                continue
            if polled and self._pause_between_accounts():
                return  # stop asked for while spacing requests out
            self._poll_account(entry, interval=interval, include_extra=include_extra)
            polled += 1

        with self._lock:
            self._cycles += 1

    def _pause_between_accounts(self) -> bool:
        """Space requests ≥20 s apart. Returns True when asked to stop."""
        sleeper = self._sleeper
        if sleeper is None:
            return bool(self._stop.wait(_MIN_ACCOUNT_SPACING_SECONDS))
        return bool(sleeper(_MIN_ACCOUNT_SPACING_SECONDS))

    def _credential_sig(self, account_id: str) -> tuple[int, int] | None:
        """``(mtime_ns, size)`` of the account's ``auth.json``, or ``None``."""
        try:
            st = os.stat(self._credentials.accounts_dir / account_id / "auth.json")
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _detect_wake(self, now: float) -> None:
        """Mark everything due when the Mac slept through the schedule.

        ``time.monotonic()`` on this interpreter is ``mach_absolute_time()``,
        which **pauses while the lid is shut**; ``time.time()`` does not. A
        wall-clock delta that outruns the monotonic delta by more than two
        intervals is therefore a sleep, not a slow cycle — and every account's
        next-due instant, computed before the lid closed, is now meaninglessly
        far in the past or the future. This is also why the *schedule* is on
        wall time: a monotonic schedule would simply have skipped the sleep.
        """
        mono = self._monotonic()
        with self._lock:
            last_wall, last_mono = self._last_wall, self._last_monotonic
            self._last_wall, self._last_monotonic = now, mono
            if last_wall is None or last_mono is None:
                return
            interval = self._interval_seconds()
            if (now - last_wall) - (mono - last_mono) <= 2.0 * interval:
                return
            for state in self._states.values():
                state.next_due_wall = 0.0
            self._note(f"woke after {_format_duration(now - last_wall)}; all accounts due")

    def _interval_seconds(self) -> float:
        low, high = SETTINGS_BOUNDS.get("codex_quota_interval_seconds", (60, 3600))
        raw = self._settings().get("codex_quota_interval_seconds", 300)
        value = _to_float(raw)
        if value is None:
            value = float(SETTINGS_DEFAULTS["codex_quota_interval_seconds"])
        return float(min(max(value, low), high))

    def _jittered(self, account_id: str, interval: float) -> float:
        """``interval`` ±15 %, deterministic per account.

        A hash, never ``random``: four accounts must be de-phased from each
        other reproducibly, so a scheduling bug reproduces instead of appearing
        once a week.
        """
        digest = hashlib.sha256(account_id.encode("utf-8")).digest()
        fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # 0..1
        return interval * (1.0 + _JITTER_FRACTION * (2.0 * fraction - 1.0))

    # -- one account -------------------------------------------------------

    def _refresh_enabled(self) -> bool:
        """``codex_refresh_enabled`` (roadmap 13), default False.

        Read on every poll rather than cached, for the same reason the live
        switch is: turning it off in Settings must stop the next request, not
        the next restart. While it answers False :class:`TokenRefresher` is
        never called and no token request of any kind is made.
        """
        try:
            return bool(self._settings().get("codex_refresh_enabled", False))
        except Exception as exc:  # a settings read must never break a poll
            self.last_error = _describe(exc)
            return False

    def _try_refresh(
        self, state: FetchState, account_id: str, label: str, *, reason: str
    ) -> RefreshOutcome:
        """One grant attempt, plus the bookkeeping a rotation implies.

        A successful rotation rewrites ``auth.json``, which moves the
        ``(mtime_ns, size)`` signature the cycle uses to spot a fresh ``codex
        login``. Without re-stamping it here, our own write would look like a
        human logging in again and would clear a standing sentinel and cancel
        its backoff on the very next cycle.
        """
        outcome = self._refresher.refresh(account_id, label=label)
        if outcome.status == REFRESH_OK:
            with self._lock:
                state.credential_sig = self._credential_sig(account_id)
        elif outcome.status != REFRESH_RELOGIN:
            self._note(f"{label}: {reason} refresh failed ({outcome.detail})")
        return outcome

    def _usage_request(
        self,
        credential: Credential,
        *,
        state: FetchState,
        account_id: str,
        label: str,
        now: float,
    ) -> HttpResponse | None:
        """One authorised GET. ``None`` means the endpoint was unreachable and
        the offline verdict has already been committed."""
        headers = {
            "Authorization": f"Bearer {credential.access_token}",
            "ChatGPT-Account-Id": account_id,
            "Accept": "application/json",
        }
        started = self._monotonic()
        try:
            # The connect budget; UrllibTransport re-arms the socket to the
            # read budget once connected (two-phase timeout).
            response = self._transport.get(CODEX_USAGE_URL, headers, _CONNECT_TIMEOUT)
        except OSError as exc:
            # Type name only: a URLError's message can carry the host and,
            # through a proxy, the URL. Neither belongs in widget.log.
            self._log(f"codex {label}: unreachable ({type(exc).__name__})")
            self._on_failure(
                state,
                account_id,
                note=NOTE_OFFLINE,
                start=_ENDPOINT_LADDER_START,
                now=now,
                status=None,
            )
            return None
        finally:
            del headers, credential  # the token's lifetime ends here
        elapsed_ms = int(max(0.0, (self._monotonic() - started)) * 1000)
        self._log(f"codex {label}: {response.status} in {elapsed_ms} ms")
        with self._lock:
            self._requests += 1
        return response

    def _poll_account(self, entry: RegistryEntry, *, interval: float, include_extra: bool) -> None:
        """Read one account: credential checks, one request, one verdict.

        Order matters. The credential is read and its ``exp`` checked
        **before** the transport is touched, so an expired token costs zero
        requests — the endpoint would only answer 401 and we already know it.

        With ``codex_refresh_enabled`` on (roadmap 13) two rotation points sit
        inside that order and nowhere else: **proactive**, before the expiry
        check, when the token is inside :data:`REFRESH_PROACTIVE_SECONDS` of
        ``exp``; and **reactive exactly once**, when the endpoint answers 401.
        A 403 never refreshes — it is an answer about permissions, and the
        token in hand is the one it refused. ``attempted_refresh`` is one flag
        for both points, so a poll can never make two grant requests.
        """
        account_id = entry.account_id
        label = entry.alias or account_id[:8]
        now = self._clock()
        with self._lock:
            state = self._states.setdefault(account_id, FetchState())
            state.credential_sig = self._credential_sig(account_id)

        credential = self._credentials.read(account_id)
        if credential is None:
            self._finish(
                state,
                account_id,
                note=NOTE_NO_CREDENTIAL,
                kind=KIND_WARN,
                next_due=now + interval,
            )
            return

        attempted_refresh = False
        if self._refresh_enabled() and refresh_due(credential, now):
            attempted_refresh = True
            outcome = self._try_refresh(state, account_id, label, reason="proactive")
            if outcome.status == REFRESH_RELOGIN:
                self._finish(
                    state, account_id, note=NOTE_RELOGIN, kind=KIND_WARN,
                    next_due=now + interval,
                )
                return
            if outcome.status == REFRESH_OK and outcome.credential is not None:
                credential = outcome.credential

        if credential.expired(now):
            # No request: an expired token has exactly one outcome and making
            # the call would only teach the endpoint our polling schedule.
            self._finish(
                state,
                account_id,
                note=NOTE_RELOGIN,
                kind=KIND_WARN,
                next_due=now + interval,
            )
            return

        remaining = credential.relogin_seconds(now)
        countdown = ""
        if remaining is not None and remaining <= CODEX_RELOGIN_WARN_SECONDS:
            countdown = f"{NOTE_RELOGIN} in {_format_duration(remaining)}"

        response = self._usage_request(
            credential, state=state, account_id=account_id, label=label, now=now
        )
        if response is None:
            return

        if response.status == 401 and self._refresh_enabled() and not attempted_refresh:
            outcome = self._try_refresh(state, account_id, label, reason="401")
            if outcome.status == REFRESH_RELOGIN:
                with self._lock:
                    state.consecutive_failures = 0
                self._finish(
                    state, account_id, note=NOTE_RELOGIN, kind=KIND_WARN,
                    next_due=now + _UNAUTHORIZED_BACKOFF_SECONDS, status=401,
                )
                return
            if outcome.status == REFRESH_OK and outcome.credential is not None:
                credential = outcome.credential
                remaining = credential.relogin_seconds(now)
                countdown = ""
                if remaining is not None and remaining <= CODEX_RELOGIN_WARN_SECONDS:
                    countdown = f"{NOTE_RELOGIN} in {_format_duration(remaining)}"
                response = self._usage_request(
                    credential, state=state, account_id=account_id, label=label, now=now
                )
                if response is None:
                    return
        del credential  # the token's lifetime ends here

        self._handle_response(
            response,
            state=state,
            entry=entry,
            now=now,
            interval=interval,
            include_extra=include_extra,
            countdown=countdown,
            label=label,
        )

    def _handle_response(
        self,
        response: HttpResponse,
        *,
        state: FetchState,
        entry: RegistryEntry,
        now: float,
        interval: float,
        include_extra: bool,
        countdown: str,
        label: str,
    ) -> None:
        """Turn one status into a note, a schedule and maybe a snapshot."""
        account_id = entry.account_id
        status = response.status

        if status == 200:
            body = _decode_json(response.body)
            if body is None:
                self._on_failure(
                    state, account_id, note=NOTE_ENDPOINT_ERROR,
                    start=_ENDPOINT_LADDER_START, now=now, status=status,
                )
                return
            reported_id = _to_str(body.get("account_id")) if isinstance(body, Mapping) else ""
            if reported_id and reported_id != account_id:
                # An answer about a DIFFERENT account. Keep the old snapshot: a
                # mislabelled figure is worse than an old one, and this is the
                # case worth a diagnostics line rather than a note.
                self._note(f"{label}: response did not match this account; kept previous reading")
                self._finish(
                    state, account_id, note=state.note, kind=state.note_kind,
                    next_due=now + self._jittered(account_id, interval), status=status,
                )
                return
            quota = CodexAccountQuota.from_response(
                body,
                credential_account_id=account_id,
                observed_at=now,
                include_extra=include_extra,
            )
            if quota is None:
                # A 200 for THIS account that carries nothing usable (no
                # rate_limit, no plan_type) is an endpoint fault, not an
                # identity problem: the ladder, and a note after three strikes.
                self._on_failure(
                    state, account_id, note=NOTE_ENDPOINT_ERROR,
                    start=_ENDPOINT_LADDER_START, now=now, status=status,
                )
                return
            with self._lock:
                self._snapshots[account_id] = quota
                self._record_sample(account_id, quota)
                state.consecutive_failures = 0
            note, kind = (quota.capped_note, KIND_CRIT) if quota.capped_note else (countdown, KIND_INFO)
            self._finish(
                state, account_id,
                note=note, kind=kind if note else "",
                next_due=now + self._jittered(account_id, interval), status=status,
            )
            return

        if status == 401:
            # Definitive, not flaky: reset the ladder so a later transport blip
            # is judged on its own evidence.
            with self._lock:
                state.consecutive_failures = 0
            self._finish(
                state, account_id, note=NOTE_RELOGIN, kind=KIND_WARN,
                next_due=now + _UNAUTHORIZED_BACKOFF_SECONDS, status=status,
            )
            return

        if status == 403:
            if _decode_json(response.body) is None:
                # An HTML body behind a 403 is a challenge page, not an answer
                # about permissions — that is an endpoint problem and it may
                # clear on its own, so it gets the ladder, not the hour.
                self._on_failure(
                    state, account_id, note=NOTE_ENDPOINT_ERROR,
                    start=_ENDPOINT_LADDER_START, now=now, status=status,
                )
                return
            with self._lock:
                state.consecutive_failures = 0
            self._finish(
                state, account_id, note=NOTE_NO_ACCESS, kind=KIND_WARN,
                next_due=now + _FORBIDDEN_BACKOFF_SECONDS, status=status,
            )
            return

        if status == 429:
            with self._lock:
                if state.failure_class != "rate":
                    state.consecutive_failures = 0
                state.failure_class = "rate"
                state.consecutive_failures += 1
                failures = state.consecutive_failures
            retry_after = _parse_retry_after(_header(response.headers, "Retry-After"), now=now)
            if retry_after is None:
                delay = _ladder(_RATE_LIMIT_LADDER_START, failures)
            else:
                # Clamped both ways: below the interval it would be a licence
                # to hammer, above an hour it would hide a recovered account.
                delay = min(max(retry_after, interval), _RETRY_AFTER_CAP_SECONDS)
            self._finish(
                state, account_id, note=NOTE_RATE_LIMITED, kind=KIND_WARN,
                next_due=now + delay, status=status,
            )
            return

        # 5xx, an unexpected 4xx, anything else: the endpoint is there and is
        # not answering the question.
        self._on_failure(
            state, account_id, note=NOTE_ENDPOINT_ERROR,
            start=_ENDPOINT_LADDER_START, now=now, status=status,
        )

    def _on_failure(
        self,
        state: FetchState,
        account_id: str,
        *,
        note: str,
        start: float,
        now: float,
        status: int | None,
    ) -> None:
        """Ladder + the three-strike rule.

        One 502 or one dropped Wi-Fi packet is noise. A note that appears on
        the first blip is a note the eye learns to ignore, and then the real
        one is invisible too — so the row keeps showing its last reading (with
        its honest age) until the **third** consecutive failure.
        """
        with self._lock:
            if state.failure_class != "endpoint":
                state.consecutive_failures = 0
            state.failure_class = "endpoint"
            state.consecutive_failures += 1
            failures = state.consecutive_failures
        delay = _ladder(start, failures)
        if failures >= _NOTE_AFTER_FAILURES:
            self._finish(state, account_id, note=note, kind=KIND_WARN,
                         next_due=now + delay, status=status)
        else:
            self._finish(state, account_id, note=state.note, kind=state.note_kind,
                         next_due=now + delay, status=status)

    def _finish(
        self,
        state: FetchState,
        account_id: str,
        *,
        note: str,
        kind: str,
        next_due: float,
        status: int | None = None,
    ) -> None:
        """Commit one account's outcome and persist the sidecar."""
        with self._lock:
            state.set_note(note, kind if note else "")
            state.next_due_wall = next_due
            if status is not None:
                state.last_status = status
        self._save()

    # -- the sidecar -------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Rehydrate snapshots and notes once, at the first read.

        Mirrors ``CodexIndexer._ensure_quota_loaded``: a restart must show the
        last reading with its TRUE age, and a standing ``relogin`` must survive
        — otherwise every restart briefly claims a dead account is merely
        waiting for its first poll. Counters are NOT restored (see
        :class:`FetchState`).
        """
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._snapshots_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return
        accounts = raw.get("accounts") if isinstance(raw, Mapping) else None
        if not isinstance(accounts, Mapping):
            return
        # Only accounts the registry still knows (disabled ones included - a
        # disabled account keeps its reading). A removed or renamed account
        # must not resurrect a stale figure or a standing note from the file.
        known = {entry.account_id for entry in self._registry.entries()}
        for account_id, record in accounts.items():
            if not isinstance(account_id, str) or not isinstance(record, Mapping):
                continue
            if account_id not in known:
                continue
            quota = CodexAccountQuota.from_json(record.get("quota"))
            if quota is not None:
                self._snapshots[account_id] = quota
            # The ring survives a restart on purpose: a forecast that had to
            # rebuild an hour of history after every launch would never speak
            # on a laptop. `_rising_tail` is what keeps it honest across the
            # gap - a window that reset while we were closed shows as a drop.
            ring = _rising_tail(record.get("samples") or ())
            if ring:
                self._samples[account_id] = ring[-PACE_RING_SIZE:]
            note = _to_str(record.get("note"))
            kind = _to_str(record.get("note_kind"))
            if note:
                state = self._states.setdefault(account_id, FetchState())
                state.set_note(note, kind or KIND_WARN)

    def _save(self) -> bool:
        """Persist snapshots + notes (never counters, never a token). 0600."""
        with self._lock:
            accounts: dict[str, Any] = {}
            known = {entry.account_id for entry in self._registry.entries()}
            for account_id in (
                set(self._snapshots) | set(self._states) | set(self._samples)
            ) & known:
                quota = self._snapshots.get(account_id)
                state = self._states.get(account_id)
                accounts[account_id] = {
                    "quota": quota.to_json() if quota is not None else None,
                    "note": state.note if state else "",
                    "note_kind": state.note_kind if state else "",
                    # The pace ring (roadmap 10). Percentages and instants
                    # only - the same class of content the snapshot already
                    # holds, so the sidecar's privacy story is unchanged.
                    "samples": [[when, pct] for when, pct in self._samples.get(account_id, ())],
                }
            payload = {"version": _SNAPSHOT_VERSION, "accounts": accounts}
        return _atomic_write_json(self._snapshots_path, payload)

    def _record_sample(self, account_id: str, quota: CodexAccountQuota) -> None:
        """Append this reading's weekly percentage to the account's ring.

        Caller holds the lock. A reading with no weekly window (Pro before the
        week starts reporting, a sentinel path) records nothing — a ring with
        holes in it would forecast across a gap it cannot see. A repeat of the
        same instant replaces rather than appends, so a forced refresh cannot
        stack duplicates and shrink the ring's span to zero.
        """
        sample = quota.seven_day
        if sample is None or sample.used_percent is None or not quota.fetched_at:
            return
        ring = self._samples.setdefault(account_id, [])
        if ring and ring[-1][0] == quota.fetched_at:
            ring[-1] = (quota.fetched_at, float(sample.used_percent))
        else:
            ring.append((quota.fetched_at, float(sample.used_percent)))
        del ring[:-PACE_RING_SIZE]

    def _pace_note_locked(self, account_id: str, quota: CodexAccountQuota, now: float) -> str:
        """The forecast note for one account, or ``""``. Caller holds the lock."""
        try:
            if not bool(self._settings().get("codex_pace_forecast_enabled", True)):
                return ""
        except Exception as exc:  # pragma: no cover - a settings probe must not break a repaint
            self.last_error = _describe(exc)
            return ""
        return pace_note(
            self._samples.get(account_id, ()),
            now=now,
            reset_at=quota.seven_day.reset_at if quota.seven_day else None,
            capped=bool(quota.capped_note),
        )

    def _note(self, line: str) -> None:
        """Record a diagnostics line, newest last, bounded at 8."""
        with self._lock:
            if line in self._notes:
                return
            self._notes.append(line)
            del self._notes[:-8]


def _decode_json(body: bytes) -> Mapping[str, Any] | None:
    """Parse a response body, or ``None`` — which is itself a verdict.

    A non-JSON 200 or 403 means an HTML challenge, a captive portal or a
    truncated read, and those get the endpoint ladder rather than being
    mistaken for a permissions answer.
    """
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


# ---------------------------------------------------------------------------
# 7. Onboarding CLI
# ---------------------------------------------------------------------------

_USAGE = """usage: python -m cc_usage_widget.codex_accounts <command>

  adopt          adopt every codex-accounts/new-*/auth.json written by
                 `CODEX_HOME=<dir> codex login`: decode its claims offline,
                 rename the dir to the account id, fix modes, add it to the
                 registry. Makes no network request.
  list           the registry, plus which account ~/.codex is logged in as.
  probe <who>    one usage request for an alias or id; prints the plan and the
                 raw window widths so a new plan shape can be read before any
                 mapping code trusts it.
  best [--json]  print the CODEX_HOME of the enabled account with the most
                 weekly headroom (soonest reset when all are capped), read
                 from the widget's own sidecar. No network request. Exit 2
                 with a one-line reason when nothing is usable.
  link [--unlink]
                 give each account home your ~/.codex configuration: the names
                 Codex WRITES to (config.toml, memories) are COPIED once, the
                 read-mostly ones (AGENTS.md, skills, plugins, agents, rules)
                 are symlinked. Idempotent; never overwrites a real file; never
                 writes to ~/.codex - which is why the writable names are not
                 symlinks. --unlink removes only the links it made; the copies
                 are that home's own files and stay.
  probe-refresh <who>
                 ONE OAuth refresh grant on that account, persisted to its
                 auth.json before use, then one usage request to prove the new
                 access token works. Prints plan/email/expiry before and
                 after; never a token. This is the SPEC-CODEX 6.4 probe that
                 must run clean (and soak 24 h) before codex_refresh_enabled
                 is worth turning on - it works whether or not it is on.
"""

COPY_TARGETS: tuple[str, ...] = (
    "config.toml",
    "memories",
)
"""What ``link`` COPIES from ``~/.codex`` into each per-account home.

These are the names Codex itself writes to: ``codex config set`` rewrites
``config.toml``, and the memory tooling appends under ``memories/``. A symlink
would make those writes land in ``~/.codex`` through the link, which is the one
thing this program promises never to do (SPEC-CODEX 6.3: the widget reads
``~/.codex`` and writes nothing into it) — and the promise would be broken by a
process the widget does not even run, which is worse than breaking it directly
because nothing here would ever log it.

Copied ONCE, and only when the name is absent from the home: a copy the user
has since edited is that home's configuration now, and re-copying over it on
the next ``link`` would silently discard the per-account settings the copy
exists to make possible."""

LINK_TARGETS: tuple[str, ...] = (
    "AGENTS.md",
    "skills",
    "plugins",
    "agents",
    "rules",
)
"""What ``link`` SYMLINKS from ``~/.codex`` into each per-account home.

The per-account homes exist to hold ONE file — ``auth.json`` — so a session
started with ``CODEX_HOME=<home>`` sees none of the user's configuration. These
names are the read-mostly half of that configuration: you edit them in
``~/.codex`` and every account sees the edit, which is the reason to link
rather than copy. Each is linked only when it exists upstream, so a machine
without ``skills/`` gets four links and no error. Deliberately NOT here:
``auth.json`` (the whole point is that each home has its own),
``sessions``/``history.jsonl``/``log`` (Codex writes those, and a symlink would
braid four accounts' history into one file), and :data:`COPY_TARGETS`."""


def _cmd_best(
    accounts_dir: Path,
    registry: Registry,
    credentials: CredentialStore,
    snapshots_path: Path,
    settings: Mapping[str, Any],
    *,
    as_json: bool,
    write: Callable[[str], Any],
    now: float,
) -> int:
    """Print the ``CODEX_HOME`` to run the next Codex session in (roadmap 5).

    Reads the registry and the widget's sidecar: no request, no credential
    parse, no write. (One more read comes with reusing the source: the same
    ``stat`` + ``tokens.account_id`` peek at ``~/.codex/auth.json`` the menu
    makes to mark the active login. Read-only, as everywhere else.) The
    ranking is the one a person makes by
    eye from the menu — **lowest weekly percentage among the accounts that
    still have room**, and when none has room, the one whose window reopens
    first — so the CLI and the menu can never disagree about which account is
    the good one.

    Rows come from :meth:`CodexAccountsSource.quota_rows`, the same call the
    worker makes, which is why a stale reading is not silently treated as
    fresh: a row past ``CODEX_FETCH_EXPIRE_SECONDS`` has withheld its figures
    upstream and lands here as "no reading", and a row carrying a ``warn``
    sentinel (dead credential, no access, offline) is not a place to send a
    session at all.

    Exit 2, never 1, when there is nothing to print: the shell function in the
    README propagates it, so ``codexb`` fails loudly instead of launching Codex
    against ``CODEX_HOME=`` (which would silently use ``~/.codex``).
    """

    def refuse(reason: str) -> int:
        if as_json:
            write(json.dumps({"home": None, "reason": reason}) + "\n")
        else:
            write(reason + "\n")
        return 2

    if not bool(settings.get("codex_live_quota_enabled", False)):
        return refuse(
            "codex live quota is off (codex_live_quota_enabled); no account has been read"
        )
    if not registry.exists():
        return refuse(f"no account registry at {registry.path}")
    entries = registry.enabled_entries()
    if not entries:
        return refuse(f"no enabled accounts in {registry.path}")

    source = CodexAccountsSource(
        registry=registry,
        credentials=credentials,
        transport=_OfflineTransport(),
        snapshots_path=snapshots_path,
        settings=lambda: settings,
        clock=lambda: now,
    )
    rows = source.quota_rows()
    if len(rows) != len(entries):  # pragma: no cover - the contract of quota_rows
        return refuse("registry changed while reading; try again")

    candidates: list[tuple[RegistryEntry, AccountRow, Path]] = []
    for entry, row in zip(entries, rows):
        home = accounts_dir / entry.account_id
        if not home.is_dir():
            continue  # a registered account with no credential dir is not a home
        if row.attention_kind == KIND_WARN:
            continue  # relogin / no access / offline: not somewhere to send work
        candidates.append((entry, row, home))
    if not candidates:
        return refuse("no usable account: every enabled one is missing a home or needs attention")

    def emit(entry: RegistryEntry, row: AccountRow, home: Path, reason: str) -> int:
        if as_json:
            write(
                json.dumps(
                    {
                        "home": str(home),
                        "account_id": entry.account_id,
                        "alias": entry.alias,
                        "weekly_used_percent": row.seven_day_pct,
                        "reset_at": row.soonest_reset_at,
                        "usage_age_seconds": row.usage_age_seconds,
                        "reason": reason,
                    }
                )
                + "\n"
            )
        else:
            write(f"{home}\n")
        return 0

    with_room = [
        item
        for item in candidates
        if item[1].seven_day_pct is not None
        and item[1].seven_day_pct < 100.0
        and item[1].attention_kind != KIND_CRIT
    ]
    if with_room:
        entry, row, home = min(
            with_room, key=lambda item: (item[1].seven_day_pct, item[0].order, item[0].account_id)
        )
        return emit(entry, row, home, "lowest weekly usage with headroom")

    resets = [item for item in candidates if item[1].soonest_reset_at is not None]
    if resets:
        entry, row, home = min(resets, key=lambda item: (item[1].soonest_reset_at, item[0].order))
        return emit(entry, row, home, "all capped; soonest reset")

    return refuse("no usable reading for any enabled account; is the widget running?")


class _OfflineTransport:
    """The transport ``best`` is built with: it refuses to be used.

    ``best`` reuses the whole source so its ranking cannot drift from the
    menu's, and a source needs a transport. Injecting one that raises is how
    "this command makes no network request" is enforced by construction rather
    than by reading the code — nothing here calls ``start()``, and if a future
    edit did, the first cycle would fail loudly instead of quietly reaching the
    endpoint from a CLI the user ran for a local answer.
    """

    def get(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        raise OSError("codex_accounts best makes no network request")

    def post_form(self, url: str, data: Mapping[str, str], timeout: float) -> HttpResponse:
        raise OSError("codex_accounts best makes no network request")


def _cmd_link(
    accounts_dir: Path,
    registry: Registry,
    codex_home: Path,
    *,
    unlink: bool,
    write: Callable[[str], Any],
) -> int:
    """Mirror ``~/.codex``'s configuration into each account home (roadmap 5).

    Four rules, and the whole safety story is in them:

    1. **Never overwrite.** A name that already exists in the home — real file,
       real directory, or somebody else's symlink — is left exactly as it is
       and counted as ``kept``. This command can therefore be run any number of
       times, and cannot lose a file a user put there on purpose.
    2. **Never write to ``~/.codex``.** Every path created is inside
       ``accounts_dir``; the upstream tree is only read. (The widget's one hard
       promise about ``~/.codex`` — SPEC-CODEX 6.3.)
    3. **What Codex writes to is copied, not linked** (:data:`COPY_TARGETS`).
       A symlink at ``config.toml`` means the next ``codex config set`` in that
       home rewrites ``~/.codex/config.toml`` through the link: rule 2 broken by
       a process this program does not run and cannot log. A home whose
       ``config.toml`` is already such a symlink — made by an earlier version of
       this command — is CONVERTED: the link is replaced by a copy of the file
       it pointed at, which changes no content and closes the hole on a machine
       that is already linked.
    4. **``--unlink`` removes only symlinks it made**: one whose target is the
       matching name under ``~/.codex``. A real file of the same name — every
       copy rule 3 made, and anything the user put there — or a link pointing
       somewhere else, is left alone. Un-linking is not un-configuring.
    """
    entries = registry.entries()
    if not entries:
        write(f"no accounts in {registry.path}\n")
        return 1
    touched = 0
    for entry in entries:
        home = accounts_dir / entry.account_id
        if not home.is_dir():
            write(f"{entry.alias or entry.account_id[:8]}: no home at {home}\n")
            continue
        made: list[str] = []
        copied: list[str] = []
        kept: list[str] = []
        missing: list[str] = []
        for name in COPY_TARGETS + LINK_TARGETS:
            target = codex_home / name
            link = home / name
            is_copy = name in COPY_TARGETS
            try:
                if unlink:
                    if link.is_symlink() and os.readlink(link) == str(target):
                        link.unlink()
                        made.append(name)
                    elif link.exists() or link.is_symlink():
                        kept.append(name)
                    continue
                if is_copy and link.is_symlink() and os.readlink(link) == str(target):
                    # Rule 3's migration: our own write-through link, replaced
                    # by a copy of exactly what it pointed at.
                    link.unlink()
                elif link.exists() or link.is_symlink():
                    kept.append(name)
                    continue
                if not target.exists():
                    missing.append(name)
                    continue
                if is_copy:
                    _copy_into_home(target, link)
                    copied.append(name)
                    continue
                os.symlink(target, link)
                made.append(name)
            except OSError as exc:
                write(f"{entry.alias or entry.account_id[:8]}: {name}: {type(exc).__name__}\n")
                return 1
        touched += len(made) + len(copied)
        verb = "unlinked" if unlink else "linked"
        parts = [f"{verb} {len(made)}"]
        if copied:
            parts.append(f"copied {len(copied)}")
        if kept:
            parts.append(f"kept {len(kept)}")
        if missing:
            parts.append(f"absent upstream {len(missing)}")
        write(f"{entry.alias or entry.account_id[:8]}: {', '.join(parts)}\n")
    write(f"{'unlinked' if unlink else 'linked/copied'} {touched} path(s) under {accounts_dir}\n")
    return 0


def _copy_into_home(target: Path, destination: Path) -> None:
    """Copy one ``~/.codex`` name into an account home. File or directory.

    The destination is always a REAL file or directory, never a link: a copy
    that turned out to be a symlink would put the writes straight back where
    they must not go. Links found *inside* a copied tree are copied as links
    (``symlinks=True``) — that is the tree the user has, and rewriting it into
    a deep copy would be this command inventing a layout.
    """
    if target.is_dir():
        shutil.copytree(target, destination, symlinks=True)
    else:
        shutil.copy2(target, destination)


def main(
    argv: Sequence[str],
    *,
    accounts_dir: os.PathLike[str] | str | None = None,
    registry_path: os.PathLike[str] | str | None = None,
    auth_path: os.PathLike[str] | str | None = None,
    transport: UsageTransport | None = None,
    snapshots_path: os.PathLike[str] | str | None = None,
    settings_path: os.PathLike[str] | str | None = None,
    clock: Callable[[], float] = time.time,
    out: Any = None,
) -> int:
    """``adopt | list | probe | best | link`` — onboarding and launching, in
    its own process.

    Its own entry point rather than a flag on the widget so it never contends
    with the single-instance flock: onboarding happens while the widget runs.

    The keyword arguments are test seams only; production passes ``argv``
    alone and every path comes from ``contracts``.
    """
    write = (out or sys.stdout).write
    args = list(argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        write(_USAGE)
        return 0 if args else 2
    command = args[0]
    store_dir = Path(accounts_dir) if accounts_dir is not None else CODEX_ACCOUNTS_DIR
    registry = Registry(registry_path if registry_path is not None else CODEX_ACCOUNTS_REGISTRY_PATH)
    credentials = CredentialStore(
        store_dir, auth_path=auth_path if auth_path is not None else CODEX_AUTH_PATH
    )

    if command == "adopt":
        return _cmd_adopt(store_dir, registry, write)
    if command == "list":
        return _cmd_list(registry, credentials, write)
    if command == "best":
        return _cmd_best(
            store_dir,
            registry,
            credentials,
            Path(snapshots_path) if snapshots_path is not None else CODEX_QUOTA_SNAPSHOTS_PATH,
            _read_settings(
                Path(settings_path) if settings_path is not None else SETTINGS_PATH
            ),
            as_json="--json" in args[1:],
            write=write,
            now=clock(),
        )
    if command == "link":
        return _cmd_link(
            store_dir,
            registry,
            credentials.auth_path.parent,
            unlink="--unlink" in args[1:],
            write=write,
        )
    if command == "probe":
        if len(args) < 2:
            write("probe needs an alias or account id\n")
            return 2
        return _cmd_probe(args[1], registry, credentials, transport, write)
    if command == "probe-refresh":
        if len(args) < 2:
            write("probe-refresh needs an alias or account id\n")
            return 2
        return _cmd_probe_refresh(args[1], registry, credentials, transport, write, clock=clock)
    write(_USAGE)
    return 2


def _read_settings(path: Path) -> dict[str, Any]:
    """``settings.json`` as the widget reads it, or the defaults.

    Through :func:`normalize_settings` rather than raw, so the CLI and the
    running widget agree about a hand-edited or half-written file: a junk value
    is the default in both, not a crash in one of them.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return dict(SETTINGS_DEFAULTS)
    return normalize_settings(raw if isinstance(raw, Mapping) else None)


def _cmd_adopt(accounts_dir: Path, registry: Registry, write: Callable[[str], Any]) -> int:
    """Rename every ``new-*`` dir to the account id its own token claims.

    Zero network: the id, email, plan and expiry all come from the token's
    payload, decoded locally. A duplicate id is **refused with both paths**
    rather than merged — two dirs claiming one account means the workspace
    picker was not used as intended, and silently keeping one would hide a
    login that is not being tracked.
    """
    try:
        candidates = sorted(p for p in accounts_dir.iterdir() if p.is_dir())
    except OSError as exc:
        write(f"no credential dir at {accounts_dir} ({type(exc).__name__})\n")
        return 1
    pending = [p for p in candidates if p.name.startswith("new-") and (p / "auth.json").is_file()]
    if not pending:
        write(f"nothing to adopt in {accounts_dir} (expected new-*/auth.json)\n")
        return 0

    existing = {p.name: p for p in candidates if not p.name.startswith("new-")}
    claimed: dict[str, Path] = {}
    plan: list[tuple[Path, str, dict[str, Any]]] = []
    for source in pending:
        auth = source / "auth.json"
        try:
            raw = json.loads(auth.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            write(f"{source.name}: unreadable auth.json ({type(exc).__name__})\n")
            return 1
        tokens = raw.get("tokens") if isinstance(raw, Mapping) else None
        tokens = tokens if isinstance(tokens, Mapping) else {}
        account_id = _to_str(tokens.get("account_id"))
        claims = decode_jwt_claims(_to_str(tokens.get("access_token")))
        account_id = account_id or _to_str(claims.get("account_id"))
        if not account_id:
            write(f"{source.name}: no account id in auth.json or its token\n")
            return 1
        other = claimed.get(account_id) or existing.get(account_id)
        if other is not None:
            write(
                f"refusing to adopt {source.name}: account {account_id[:8]} is already "
                f"at {other}\n"
                f"  both credential dirs claim the same account; delete one and log in "
                f"again choosing the other workspace\n"
            )
            return 1
        claimed[account_id] = source
        plan.append((source, account_id, claims))

    order = len(registry.entries())
    for source, account_id, claims in plan:
        target = accounts_dir / account_id
        try:
            source.rename(target)
            os.chmod(target, 0o700)
            os.chmod(target / "auth.json", 0o600)
        except OSError as exc:
            write(f"{source.name}: could not adopt ({type(exc).__name__})\n")
            return 1
        registry.upsert(RegistryEntry(account_id=account_id, alias="", enabled=True, order=order))
        order += 1
        exp = claims.get("exp")
        when = _format_reset_clock(exp, time.time()) if exp else "unknown"
        write(
            f"{account_id} · {claims.get('email') or '(no email in token)'} · "
            f"{claims.get('plan_type') or '(no plan in token)'} · expires {when}\n"
        )
    write(f"adopted {len(plan)}; set aliases in {registry.path}\n")
    return 0


def _cmd_list(registry: Registry, credentials: CredentialStore, write: Callable[[str], Any]) -> int:
    """The registry as the widget reads it, plus the active login."""
    entries = registry.entries()
    if not entries:
        write(f"no accounts in {registry.path}\n")
    active = credentials.active_account_id()
    found = credentials.discover()
    for index, entry in enumerate(entries):
        marks = []
        if entry.account_id == active:
            marks.append("active")
        if not entry.enabled:
            marks.append("disabled")
        if entry.account_id not in found:
            marks.append("no credential")
        suffix = f"  ({', '.join(marks)})" if marks else ""
        write(f"-{index + 1}  {entry.alias or '(no alias)'}  {entry.account_id}{suffix}\n")
    if active and not any(entry.account_id == active for entry in entries):
        write(f"~/.codex is logged in as {active[:8]}, which is not tracked\n")
    elif not active:
        write("~/.codex has no readable account id\n")
    if credentials.last_error:
        write(f"! {credentials.last_error}\n")
    return 0


def _cmd_probe(
    who: str,
    registry: Registry,
    credentials: CredentialStore,
    transport: UsageTransport | None,
    write: Callable[[str], Any],
) -> int:
    """One request, printed as raw widths — the tool for a new plan shape.

    Prints ``limit_window_seconds`` verbatim rather than the mapped row on
    purpose: this is what you run *before* trusting the mapper on a plan
    nobody has seen, so it must not launder the answer through the mapping it
    is meant to check.
    """
    match = None
    for entry in registry.entries():
        if who in (entry.account_id, entry.alias) or entry.account_id.startswith(who):
            match = entry
            break
    if match is None:
        write(f"no registry entry matching {who!r}\n")
        return 1
    credential = credentials.read(match.account_id)
    if credential is None:
        write(f"{credentials.last_error or 'credential unreadable'}\n")
        return 1
    client = transport or UrllibTransport()
    headers = {
        "Authorization": f"Bearer {credential.access_token}",
        "ChatGPT-Account-Id": match.account_id,
        "Accept": "application/json",
    }
    try:
        response = client.get(CODEX_USAGE_URL, headers, _CONNECT_TIMEOUT)
    except OSError as exc:
        write(f"unreachable ({type(exc).__name__})\n")
        return 1
    write(f"HTTP {response.status}\n")
    body = _decode_json(response.body)
    if body is None:
        write("body was not JSON\n")
        return 1
    write(f"plan_type: {body.get('plan_type')!r}\n")
    rate_limit = body.get("rate_limit")
    rate_limit = rate_limit if isinstance(rate_limit, Mapping) else {}
    for name in ("primary_window", "secondary_window"):
        window = rate_limit.get(name)
        if isinstance(window, Mapping):
            write(
                f"{name}: limit_window_seconds={window.get('limit_window_seconds')!r} "
                f"used_percent={window.get('used_percent')!r} "
                f"reset_after_seconds={window.get('reset_after_seconds')!r}\n"
            )
        else:
            write(f"{name}: null\n")
    extras = body.get("additional_rate_limits")
    if isinstance(extras, Sequence) and not isinstance(extras, (str, bytes)):
        for entry in extras:
            if not isinstance(entry, Mapping):
                continue
            inner = entry.get("rate_limit")
            inner = inner if isinstance(inner, Mapping) else {}
            widths = [
                (inner.get(key) or {}).get("limit_window_seconds")
                if isinstance(inner.get(key), Mapping)
                else None
                for key in ("primary_window", "secondary_window")
            ]
            write(f"extra {entry.get('limit_name')!r}: widths={widths}\n")
    write(f"allowed={rate_limit.get('allowed')!r} limit_reached={rate_limit.get('limit_reached')!r}\n")
    return 0


def _identity_line(label: str, credential: Credential, now: float) -> str:
    """``gmail: plan pro, who@example.test, expires 2026-09-19 08:46 (in 9d)``.

    Everything a person needs to see that a rotation happened, and nothing that
    could be replayed: the plan and email come from the token's own claims, and
    the expiry is a date. No token, no length, no prefix, no ``last_refresh``
    string (which would say when — not what)."""
    if credential.exp is None:
        life = "expiry unknown"
    else:
        remaining = credential.relogin_seconds(now)
        when = dt.datetime.fromtimestamp(credential.exp).strftime("%Y-%m-%d %H:%M")
        life = f"expires {when}" + (f" (in {_format_duration(remaining)})" if remaining else " (passed)")
    return (
        f"{label}: plan {credential.plan_type or 'unknown'}, "
        f"{credential.email or 'no email in claims'}, {life}"
    )


def _cmd_probe_refresh(
    who: str,
    registry: Registry,
    credentials: CredentialStore,
    transport: UsageTransport | None,
    write: Callable[[str], Any],
    *,
    clock: Callable[[], float] = time.time,
) -> int:
    """The SPEC-CODEX 6.4 probe: one grant, then one GET that proves it.

    Deliberately **not** gated on ``codex_refresh_enabled``: this command is
    what produces the evidence that opens that switch, so requiring the switch
    would be circular. It is also deliberately one-shot — no loop, no second
    account, no retry — because the question it answers ("does rotating this
    login break the others?") is answered by looking at the *other* stores 24 h
    later, and every extra grant muddies that reading.

    Exit 0 only when the rotation persisted **and** the new access token was
    accepted by the usage endpoint. Anything else is 1 with one line saying
    which half failed.
    """
    match = None
    for entry in registry.entries():
        if who in (entry.account_id, entry.alias) or entry.account_id.startswith(who):
            match = entry
            break
    if match is None:
        write(f"no registry entry matching {who!r}\n")
        return 1
    label = match.alias or match.account_id[:8]

    before = credentials.read(match.account_id)
    if before is None:
        write(f"{credentials.last_error or 'credential unreadable'}\n")
        return 1
    now = clock()
    write("before  " + _identity_line(label, before, now) + "\n")
    del before

    client = transport or UrllibTransport()
    refresher = TokenRefresher(client, credentials, clock=clock, log=lambda line: None)
    outcome = refresher.refresh(match.account_id, label=label)
    if outcome.status != REFRESH_OK or outcome.credential is None:
        write(f"refresh {outcome.status}: {outcome.detail or 'no detail'}\n")
        return 1
    after = outcome.credential
    write("after   " + _identity_line(label, after, clock()) + "\n")
    write(f"        rotated tokens persisted to {credentials.accounts_dir / match.account_id}/auth.json\n")

    headers = {
        "Authorization": f"Bearer {after.access_token}",
        "ChatGPT-Account-Id": match.account_id,
        "Accept": "application/json",
    }
    del after, outcome
    try:
        response = client.get(CODEX_USAGE_URL, headers, _CONNECT_TIMEOUT)
    except OSError as exc:
        write(f"usage request unreachable ({type(exc).__name__})\n")
        return 1
    finally:
        del headers
    body = _decode_json(response.body)
    if response.status != 200 or not isinstance(body, Mapping):
        write(f"usage   HTTP {response.status} - the new access token was NOT accepted\n")
        return 1
    quota = CodexAccountQuota.from_response(
        body, credential_account_id=match.account_id, observed_at=clock(), include_extra=False
    )
    weekly = "no weekly window"
    if quota is not None and quota.seven_day is not None:
        window = quota.seven_day
        weekly = (
            f"weekly {window.used_percent:.1f}%"
            if window.used_percent is not None
            else "weekly limit reached"
        )
    write(f"usage   HTTP 200, plan {body.get('plan_type')!r}, {weekly}\n")
    write(
        "soak    leave the other stores alone for 24 h, then run `list` and check\n"
        "        ~/.codex still works before turning codex_refresh_enabled on\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main(sys.argv[1:]))
