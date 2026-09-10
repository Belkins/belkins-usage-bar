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

Phase 1 never refreshes a token
===============================

The stored ``access_token`` lives ~10 days; this module decodes its ``exp``
locally (base64url payload, **no signature check** — display and scheduling
only) and starts saying ``relogin in 1d 4h`` 48 h out, then ``relogin`` when it
passes. It does **not** POST a refresh grant, because whether two ``codex
login`` sessions under one public OAuth client share a refresh-token family is
unverified, and a wrong guess logs Vlad out of the account he is coding in.

*Revisit trigger:* the phase-2 probe in the plan (a throwaway ``CODEX_HOME``,
one refresh grant, 24 h soak, then a deliberate replay of the superseded token
to see whether reuse detection revokes the family). Only a clean result opens
``codex_refresh_enabled``. Until then a 10-day relogin is the shipped cost and
it is stated on the row rather than hidden.

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

``python -m cc_usage_widget.codex_accounts adopt | list | probe <alias|id>`` —
its own process, so onboarding never contends with the widget's flock.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import http.client
import json
import os
import stat as stat_module
import sys
import tempfile
import threading
import time
import urllib.error
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
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    Pct,
    Vendor,
)
from .render import window_minutes_label

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
    "decode_jwt_claims",
    "main",
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

    Deliberately **not** carried: ``credits`` and ``spend_control`` (rendering
    ``individual_limit`` would imply a unit we have not verified — dollars?
    requests? — and a wrong unit next to a real number is worse than silence),
    and ``model_usage`` (per-model token counts already come from the corpus,
    priced by ``pricing.py``; a second, differently-defined source of the same
    quantity is how two numbers for one thing get shipped).
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
        )

    # -- rendering ---------------------------------------------------------

    @property
    def capped_note(self) -> str:
        """``"capped"`` / ``"capped <type>"`` when the PLAN is at its limit.

        Distinct from every transport note: this one sits *beside* the figures
        because they are still true — the account really is at 100 % of that
        window, and hiding the bar would hide the reason.
        """
        if self.allowed and not self.limit_reached:
            return ""
        return f"capped {self.reached_type}" if self.reached_type else "capped"

    def account_row(
        self,
        *,
        now: float,
        slot: int,
        alias: str,
        is_active: bool,
        note: str = "",
        note_kind: str = "",
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

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._paused = False
        self._loaded = False
        self._snapshots: dict[str, CodexAccountQuota] = {}
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

    def _poll_account(self, entry: RegistryEntry, *, interval: float, include_extra: bool) -> None:
        """Read one account: credential checks, one request, one verdict.

        Order matters. The credential is read and its ``exp`` checked
        **before** the transport is touched, so an expired token costs zero
        requests — the endpoint would only answer 401 and we already know it.
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
            return
        finally:
            del headers, credential  # the token's lifetime ends here
        elapsed_ms = int(max(0.0, (self._monotonic() - started)) * 1000)
        self._log(f"codex {label}: {response.status} in {elapsed_ms} ms")
        with self._lock:
            self._requests += 1

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
            for account_id in (set(self._snapshots) | set(self._states)) & known:
                quota = self._snapshots.get(account_id)
                state = self._states.get(account_id)
                accounts[account_id] = {
                    "quota": quota.to_json() if quota is not None else None,
                    "note": state.note if state else "",
                    "note_kind": state.note_kind if state else "",
                }
            payload = {"version": _SNAPSHOT_VERSION, "accounts": accounts}
        return _atomic_write_json(self._snapshots_path, payload)

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
"""


def main(
    argv: Sequence[str],
    *,
    accounts_dir: os.PathLike[str] | str | None = None,
    registry_path: os.PathLike[str] | str | None = None,
    auth_path: os.PathLike[str] | str | None = None,
    transport: UsageTransport | None = None,
    out: Any = None,
) -> int:
    """``adopt | list | probe`` — onboarding, in its own process.

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
    if command == "probe":
        if len(args) < 2:
            write("probe needs an alias or account id\n")
            return 2
        return _cmd_probe(args[1], registry, credentials, transport, write)
    write(_USAGE)
    return 2


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


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main(sys.argv[1:]))
