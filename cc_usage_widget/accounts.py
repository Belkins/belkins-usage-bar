"""Thin adapter over ``claude_swap`` - accounts, quota windows, autoswitch.

This module implements :class:`~cc_usage_widget.contracts.AccountSource`
(SPEC 3.1). Its whole job is **translation**: it maps claude-swap's shapes onto
our :class:`~cc_usage_widget.contracts.AccountRow` and forwards actions back.
It deliberately contains no account, usage, or autoswitch *logic* of its own:

===========================  =================================================
account list / active slot   ``switcher.accounts_snapshot()`` via
                             ``claude_swap.snapshot_source.SnapshotSource``
per-window percentages       ``UsageEntry.last_good`` - the same dict
                             ``oauth.build_usage_result`` produced
reset strings                ``claude_swap.oauth.fresh_reset_strings``
weekly roll-forward          ``claude_swap.menubar._rolled_weekly_window``
autoswitch                   ``claude_swap.autoswitch.AutoSwitchEngine``
autoswitch policy            ``claude_swap.settings.load_settings``
switching                    ``switcher.switch_to``
===========================  =================================================

Why ``SnapshotSource`` and not the usage API
--------------------------------------------

``SnapshotSource.take()`` runs exactly the pass ``cswap list`` runs; claude-swap's
usage **store** decides whether any account is even eligible for a network fetch
(``SERVE_TTL_S`` = 180 s, plus per-account poll plans and backoff). So calling it
on our 60 s UI tick adds **no API cadence of our own** - it is the paced read
path (SPEC 3.1, SPEC 3.5). While our autoswitch engine is running it already
collects on its own schedule, so the display read drops to ``store_only=True``
(no network eligibility at all) - the same rule upstream's menu bar uses.

One source of truth for the toggle
----------------------------------

Autoswitch *policy* (threshold, interval, cooldown, hysteresis, strategy,
model) is read from claude-swap's ``settings.json`` on every engine (re)build,
so ``cswap config set autoswitch.*`` takes effect without restarting us. The
on/off flag itself has no upstream spec, so it is persisted as
``autoswitch.enabled`` **inside claude-swap's own settings.json** - the same
file and section ``cswap config`` writes, whose reader preserves unknown keys
across a round trip. Our ``settings.json``'s ``autoswitch_enabled`` is only a
first-run default and the fallback when that key is absent.

Failure policy
--------------

Every ``claude_swap`` call is wrapped. An upstream rename, a locked Keychain, a
corrupt state file - anything - degrades to "accounts unavailable"
(:data:`ACCOUNTS_UNAVAILABLE`, with :attr:`SwapAccountSource.last_error` naming
the cause) and never propagates into the widget. A previously good snapshot is
kept and re-rendered rather than blanked.

Threading
---------

Every method here may block (file locks, Keychain subprocesses, network) and so
must be called from the background worker, never the AppKit main thread
(SPEC 2.3). The rows it returns are frozen dataclasses, which is what makes
handing them to the main thread safe. Two concurrent refreshes are collapsed by
an in-flight guard instead of queueing behind each other.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final

from .contracts import (
    ALERT_ACCOUNT_QUARANTINED,
    ALERT_ALL_EXHAUSTED,
    ALERT_ERROR,
    ALERT_EXTERNAL_SWITCH,
    ALERT_GHOST_FLIP,
    ALERT_NO_TARGET,
    FETCH_FAILING_MIN_FAILURES,
    GHOST_FLIP_FIGHT_COUNT,
    GHOST_FLIP_WINDOW_SECONDS,
    AccountRow,
    Pct,
    normalize_settings,
)

__all__ = [
    "LOGGER",
    "ACCOUNTS_UNAVAILABLE",
    "AUTOSWITCH_SECTION",
    "AUTOSWITCH_ENABLED_KEY",
    "SwapAccountSource",
    "create_account_source",
]

LOGGER: Final[logging.Logger] = logging.getLogger("cc_usage_widget.accounts")
"""Module logger. Callers may hand a different one to the constructor."""

ACCOUNTS_UNAVAILABLE: Final[str] = "accounts unavailable"
"""Exact menu text when claude-swap cannot be read at all. Defined here so the
adapter and ``app.py`` cannot disagree about the wording."""

ENGINE_UNAVAILABLE_PREFIX: Final[str] = "autoswitch engine unavailable: "
"""Prefix of the alert this adapter raises when the engine will not construct.

Ours, not upstream's — so a later successful construction can retract exactly
that alert without touching an ``error`` verdict the engine itself emitted."""

AUTOSWITCH_SECTION: Final[str] = "autoswitch"
"""Section of claude-swap's ``settings.json`` that holds autoswitch keys."""

AUTOSWITCH_ENABLED_KEY: Final[str] = "enabled"
"""Our on/off key inside that section - ``autoswitch.enabled``. Upstream has no
spec for it, so ``cswap config set`` will not accept it by name; upstream's
reader does preserve it verbatim across its own writes, which is what makes the
file a safe shared home for the flag."""

_ROWS_MAX_AGE_S: Final[float] = 45.0
"""How old a cached snapshot may be before :meth:`SwapAccountSource.rows`
refreshes on its own. Below the 60 s UI tick, so a caller that only ever calls
``rows()`` still gets fresh data, while ``refresh()`` + ``rows()`` in one tick
costs exactly one snapshot pass."""

_MIN_TAKE_INTERVAL_S: Final[float] = 5.0
"""Floor between unforced snapshot passes. Not a network guard (the store owns
that) - it stops a chatty caller from re-hitting the Keychain."""

_TICK_DELAY_FLOOR_S: Final[float] = 15.0
"""Lower clamp on the autoswitch re-evaluation delay; matches the floor
``autoswitch.intervalSeconds`` itself allows."""

_TICK_DELAY_CEILING_S: Final[float] = 3600.0
"""Upper clamp on the autoswitch re-evaluation delay."""

_BACKEND_RETRY_S: Final[float] = 60.0
"""Wait before retrying a failed ``claude_swap`` import/construction, so a
transient failure is not permanent and a hard failure is not a hot loop."""

_EVENT_LOG_LIMIT: Final[int] = 20
"""How many recent autoswitch event lines :meth:`SwapAccountSource.recent_events`
keeps."""

_NO_TARGET_REASONS: Final[frozenset[str]] = frozenset(
    {"no-viable-target", "no-candidates"}
)
"""``NoSwitchEvent.reason`` values that are a STANDING problem, not a shrug.

Every other reason ("below-threshold", "cooldown", "active-idle", ...) means the
engine looked and had nothing to do. These two mean it wanted to move and could
not - the state behind tonight's ``main 100% ⛔ exhausted`` sitting silent, since
a plain ``no-switch`` also CLEARS whatever alert was standing."""

_ANY_SLOT: Final[str] = "*"
"""``_expect_active`` value meaning "we switched, target slot unknown".

Set when a manual switch names an alias claude-swap owns and we cannot map back
to a number. It suppresses the external-switch verdict for exactly one pass -
mislabelling our own click as a rival actor would be a lie, and the click is
already in the log two lines up."""

_QUIET_EVENT_KINDS: Final[frozenset[str]] = frozenset({"poll", "sleep"})
"""Event kinds kept out of :meth:`SwapAccountSource.recent_events`.

They carry no verdict and the engine emits one of them on nearly every tick, so
including them would push every real line out of the 20-slot deque within ~20
minutes - i.e. the "Recent switches" menu block would show no switches. They are
still DEBUG-logged, which is where a cadence question is answered."""

_TIMESTAMP_TAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"(\d{4}-\d{2}-\d{2})T[\d:.]+Z?"
)
"""Matches an ISO instant and keeps only its date - see :func:`_episode_key`."""

_TESTED_CLAUDE_SWAP_PREFIX: Final[str] = "0.25."
"""claude-swap versions this adapter's private-symbol reach-ins were verified
against. A mismatch WARNS (never refuses): the failure policy already degrades
gracefully, this just makes the degradation datable."""

FETCH_FAILING_KIND: Final[str] = "fetch-failing"
"""Sentinel kind (:meth:`SwapAccountSource.sentinel_kinds`) for a slot whose
usage polls keep being refused with a 4xx other than 401/429. OURS, not an
upstream sentinel key: claude-swap records the failure (``last_error``,
``consecutive_failures``) but names no state for it, so slot 6 rendered
``5h 88% · 7d 0%`` for twelve days of HTTP 403 with no cause (2026-09-25)."""

_HTTP_ERROR_RE: Final[re.Pattern[str]] = re.compile(r"^http-(\d{3})\b")
"""claude-swap's ``UsageEntry.last_error`` for an HTTP failure: ``http-403``,
``http-429, retry-after 3600s (...)``. Anything else (``network``,
``timeout``) is not an answer from the account."""

_COLD_BACKOFF_MIN_AGE_S: Final[float] = 86400.0
"""How long without a good fetch turns upstream's 429 backoff into a standing
refusal on its own (:func:`_cold_backoff_code`). The backoff lasts an hour and
follows every four 403s, so a slot that has not fetched for a DAY is not being
throttled back to health; a fresh process that starts inside the backoff hour
has no 403 of its own to remember (2026-09-25 slot 6: ``http-429``, 803
failures, last good fetch Sep 12)."""

LOGIN_FIGHT_MARK: Final[str] = "login fight:"
"""The words that set the standing login-fight line apart from a per-flip
ghost line; both carry :data:`ALERT_GHOST_FLIP`. The title badges the fight
and not a single benign flip (``app.CCUsageWidgetApp._title_alert_kind``)."""

_SWAP_LOG_NAME: Final[str] = "claude-swap.log"
"""claude-swap's own log inside ``backup_dir`` (``logging_config``), where every
real ``cswap`` switch writes ``Switched from account X to Y``."""

_SWAP_LOG_TAIL_BYTES: Final[int] = 64 * 1024
"""How much of claude-swap.log the flip classifier reads - the tail only."""

_SWAP_LOG_SLACK_S: Final[float] = 15.0
"""Clock slack around the inter-pass interval when matching a switch line."""

_SWAP_LOG_SWITCH_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - \w+ - "
    r"Switched from account (\d+) to (\d+)\s*$",
    re.MULTILINE,
)
"""A ``%(asctime)s - %(levelname)s - %(message)s`` switch line (local time)."""


# ---------------------------------------------------------------------------
# Small pure helpers (no claude_swap involved)
# ---------------------------------------------------------------------------


def _as_mapping(settings: Any) -> Mapping[str, Any] | None:
    """Coerce whatever the caller passed as settings into a mapping.

    Accepts our ``state.SettingsStore`` (anything with ``as_dict()``), a plain
    mapping, or ``None``. Anything else is ignored rather than raising - a bad
    settings object must not stop the widget from listing accounts.
    """
    if settings is None:
        return None
    as_dict = getattr(settings, "as_dict", None)
    if callable(as_dict):
        try:
            value = as_dict()
        except Exception:  # pragma: no cover - defensive
            return None
        return value if isinstance(value, Mapping) else None
    return settings if isinstance(settings, Mapping) else None



def _stamp() -> str:
    """Local ``HH:MM`` for a forensic line.

    Local, not UTC: the operator reads these against the wall clock next to the
    menu bar. Upstream's own lines are UTC ISO instants, which is exactly why
    the all-exhausted line needed rewriting (SPEC 4.3 - show a time a human can
    act on, never a number they must convert).
    """
    return time.strftime("%H:%M")


def _episode_key(line: str) -> str:
    """Jitter-free identity for a repeating engine event line.

    Engine event text carries an ISO reset instant whose sub-second part (and
    therefore sometimes its whole second, minute and hour) moves between
    emissions of the SAME episode. Collapse every timestamp to its date so
    repeat detection is stable; a genuinely new episode almost always carries
    a different reset date, and the hourly re-WARN covers the rest.
    """
    return _TIMESTAMP_TAIL_RE.sub(r"\1", line)


def _safe_int(value: Any, default: int = 0) -> int:
    """``int(value)`` or *default*. claude-swap slot numbers are strings."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _pct(window: Any) -> Pct | None:
    """The 0-100 utilization of one usage window, or ``None`` if not reported.

    claude-swap stores the API's ``utilization`` verbatim under ``pct``, already
    0-100, so this is a range-preserving read and never a rescale.
    """
    if not isinstance(window, Mapping):
        return None
    raw = window.get("pct")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _window(usage: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    """One named window (``five_hour`` / ``seven_day``) out of a usage dict."""
    if not isinstance(usage, Mapping):
        return None
    window = usage.get(key)
    return window if isinstance(window, Mapping) else None


def _iso_ts(raw: Any) -> float | None:
    """POSIX time of a stored ISO ``resets_at``, parsed the way upstream's
    ``menubar._resets_at_ts`` parses it; ``None`` when missing or bad."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _reset_passed(window: Any, now: float) -> bool:
    """Whether a usage window's stored reset instant is already behind us."""
    if not isinstance(window, Mapping):
        return False
    ts = _iso_ts(window.get("resets_at"))
    return ts is not None and ts <= now


def _fetch_failing_code(entry: Any) -> int | None:
    """The HTTP status of a standing usage-fetch refusal, or ``None``.

    Only a 4xx other than 401 (a credential problem upstream names itself)
    and 429 (throttling, which heals on its own), repeated at least
    :data:`FETCH_FAILING_MIN_FAILURES` times in a row. A network error or a
    timeout says nothing about the account.
    """
    if entry is None:
        return None
    match = _HTTP_ERROR_RE.match(str(getattr(entry, "last_error", None) or ""))
    if match is None:
        return None
    code = int(match.group(1))
    if not 400 <= code < 500 or code in (401, 429):
        return None
    failures = getattr(entry, "consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int):
        return None
    return code if failures >= FETCH_FAILING_MIN_FAILURES else None


def _cold_backoff_code(entry: Any, now: float) -> int | None:
    """``429`` for a throttled slot that has not fetched for a day, or ``None``.

    Upstream's hourly 429 backoff on its own heals, so :func:`_fetch_failing_code`
    ignores it. But a 429 after at least :data:`FETCH_FAILING_MIN_FAILURES`
    consecutive failures with ``fetched_at`` over
    :data:`_COLD_BACKOFF_MIN_AGE_S` old is a refusal that has stood for days;
    without this a widget started inside the backoff hour showed live-looking
    figures for that slot until the next 403.
    """
    if entry is None:
        return None
    match = _HTTP_ERROR_RE.match(str(getattr(entry, "last_error", None) or ""))
    if match is None or int(match.group(1)) != 429:
        return None
    failures = getattr(entry, "consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int):
        return None
    if failures < FETCH_FAILING_MIN_FAILURES:
        return None
    fetched = getattr(entry, "fetched_at", None)
    if isinstance(fetched, bool) or not isinstance(fetched, (int, float)):
        return None
    return 429 if now - float(fetched) >= _COLD_BACKOFF_MIN_AGE_S else None


def _spend(raw: Any) -> tuple[float, float, float, str] | None:
    """``(used, limit, pct, currency)`` from ``lastGood['spend']``, or ``None``.

    All four or nothing: a partial entry must never render as ``$0``.
    ``currency`` is passed through verbatim.
    """
    if not isinstance(raw, Mapping):
        return None
    numbers: list[float] = []
    for key in ("used", "limit", "pct"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)):
            return None
        numbers.append(float(value))
    currency = raw.get("currency")
    if not isinstance(currency, str) or not currency:
        return None
    return numbers[0], numbers[1], numbers[2], currency


def _read_switch_lines(path: Path) -> tuple[tuple[float, str, str], ...]:
    """``(epoch, from, to)`` for every switch line in the log's tail.

    Raises on an unreadable file; the caller turns that into "cannot tell".
    """
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - _SWAP_LOG_TAIL_BYTES)
        handle.seek(start)
        text = handle.read().decode("utf-8", errors="replace")
    if start > 0:
        # The first line of the window is almost certainly cut in half.
        text = text.split("\n", 1)[1] if "\n" in text else ""
    out: list[tuple[float, str, str]] = []
    for match in _SWAP_LOG_SWITCH_RE.finditer(text):
        try:
            when = time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        out.append((when, match.group(2), match.group(3)))
    return tuple(out)


def _parse_policy_models(policy: Any) -> tuple[str, ...]:
    """``autoswitch.model`` of an already-loaded policy, via upstream's parser.

    No file read: *policy* is the object the engine was just built from.
    Any failure is ``()`` ("compare 5h/7d only"), as in ``_policy_models``.
    """
    try:
        from claude_swap.settings import parse_model_names

        return tuple(parse_model_names(getattr(policy, "model", None)))
    except Exception as exc:
        LOGGER.debug("autoswitch model limits unreadable (%r)", exc)
        return ()


def _on_main_thread() -> bool:
    """Whether we are on the AppKit main thread.

    ``rumps`` runs its event loop on the interpreter's main thread, so this is
    the thread a blocking snapshot pass must never stall (SPEC 2.3).
    """
    return threading.current_thread() is threading.main_thread()


_SWITCH_WARNING_LIMIT: Final[int] = 5
"""How many of one switch result's ``warnings`` lines reach the event log.

The log is a 20-line deque shared with the engine's own events; a rotation
that skipped every disabled slot must not evict the switch itself. The log
line count is capped, never the WARNING records - see
:func:`_switch_warnings_for_log`."""


def _switch_warnings(result: Mapping[str, Any]) -> tuple[str, ...]:
    """ALL of a claude-swap switch result's ``warnings``, as clean strings.

    Upstream returns ``{"switched", "from", "to", "strategy", "reason",
    "message", "warnings"}``; the widget dropped ``warnings`` entirely, so
    "Skipped Account-3 (disabled)" never reached the operator. Anything that
    is not a non-empty string is discarded rather than str()-ed into noise.

    Nothing is truncated here: every line is logged. The *menu* view is
    capped separately by :func:`_switch_warnings_for_log`.
    """
    raw = result.get("warnings")
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        line.strip() for line in raw if isinstance(line, str) and line.strip()
    )


def _switch_warnings_for_log(lines: tuple[str, ...]) -> tuple[str, ...]:
    """At most :data:`_SWITCH_WARNING_LIMIT` lines, and honest when it cut.

    Several disabled slots plus an inert-model-limit warning can exceed the
    cap. Silently keeping the first five renders a complete-LOOKING list that
    is not complete, so an over-long run keeps four lines and spends the
    fifth saying how many were dropped and where to read them (SPEC 4.3:
    never imply we showed everything we had).
    """
    if len(lines) <= _SWITCH_WARNING_LIMIT:
        return tuple(lines)
    kept = _SWITCH_WARNING_LIMIT - 1
    return (*lines[:kept], f"… and {len(lines) - kept} more (see the log)")



def _alias_from_email(email: str, slot: int) -> str:
    """Fallback display alias for an account with no ``cswap alias`` set.

    The email's local part (``jane@example.com`` -> ``jane``), else
    ``account-<slot>``. Never empty, because the title renders it.
    """
    local = email.split("@", 1)[0].strip() if isinstance(email, str) else ""
    return local or f"account-{slot}"


# ---------------------------------------------------------------------------
# The claude_swap backend, imported lazily and defensively
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Backend:
    """The handful of ``claude_swap`` entry points this adapter uses.

    Resolved once, lazily, so importing this module never fails when
    ``claude_swap`` is missing (tests, CI) and so an upstream rename surfaces as
    one recorded error instead of an import-time crash.
    """

    switcher: Any
    snapshot_source: Any
    engine_cls: Any
    load_policy: Callable[[Path], Any]
    settings_path: Callable[[Path], Path]
    write_json: Callable[[Path, dict], None]
    fresh_reset_strings: Callable[[Mapping[str, Any]], Any]
    roll_weekly: Callable[[Any, float], Any]
    sentinel_notes: Mapping[str, str]
    schema_version: int
    backup_dir: Path
    state_filename: str
    """Name of claude-swap's autoswitch state file inside ``backup_dir``.

    Taken from upstream rather than hardcoded so a rename lands here as a
    missing quarantine alert, not as a silently wrong path.
    """


def _identity_roll(window: Any, _now: float) -> Any:
    """Fallback for the weekly roll-forward: leave the window untouched."""
    return window


def _import_backend(switcher: Any | None) -> _Backend:
    """Import ``claude_swap`` and build the backend handle.

    Raises whatever the import or the switcher's construction raises; the only
    caller wraps this and records the failure.
    """
    from claude_swap.autoswitch import STATE_FILENAME, AutoSwitchEngine
    from claude_swap.oauth import fresh_reset_strings
    from claude_swap.settings import (
        SETTINGS_SCHEMA_VERSION,
        atomic_write_json,
        load_settings,
        settings_path,
    )
    from claude_swap.snapshot_source import SnapshotSource
    from claude_swap.switcher import SENTINEL_NOTES, ClaudeAccountSwitcher

    # This adapter reaches into 9 upstream symbols, one of them PRIVATE
    # (menubar._rolled_weekly_window). Warn — never refuse — when running
    # against an untested claude-swap major/minor, so a silent
    # good-but-degraded state after `uv tool upgrade` has a log line.
    try:
        from importlib.metadata import version as _dist_version

        _cs_version = _dist_version("claude-swap")
        if not _cs_version.startswith(_TESTED_CLAUDE_SWAP_PREFIX):
            LOGGER.warning(
                "claude-swap %s is outside the tested %sx range — the "
                "accounts adapter may silently degrade; re-verify the menu "
                "after upstream upgrades",
                _cs_version,
                _TESTED_CLAUDE_SWAP_PREFIX,
            )
    except Exception:  # pragma: no cover - metadata is best-effort
        pass

    # Reuse upstream's weekly roll-forward: once a weekly window's ``resets_at``
    # has passed we know it rolled over, so the stored pct belongs to a window
    # that no longer exists and must not render as "Fable 100% (!)" for days.
    # It is upstream's own display rule (``claude_swap.menubar``, import-safe
    # without rumps); if it ever disappears we show the raw window instead.
    try:
        from claude_swap.menubar import _rolled_weekly_window as roll_weekly
    except Exception:  # pragma: no cover - upstream refactor
        roll_weekly = _identity_roll
        LOGGER.warning(
            "claude_swap.menubar._rolled_weekly_window is gone (upstream "
            "refactor?) — weekly windows render RAW, so an already-rolled "
            "week may display as a stale 100%% until its next real fetch"
        )

    live = switcher if switcher is not None else ClaudeAccountSwitcher()
    return _Backend(
        switcher=live,
        snapshot_source=SnapshotSource(live),
        engine_cls=AutoSwitchEngine,
        load_policy=load_settings,
        settings_path=settings_path,
        write_json=atomic_write_json,
        fresh_reset_strings=fresh_reset_strings,
        roll_weekly=roll_weekly,
        sentinel_notes=dict(SENTINEL_NOTES),
        schema_version=int(SETTINGS_SCHEMA_VERSION),
        backup_dir=Path(live.backup_dir),
        state_filename=str(STATE_FILENAME),
    )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class SwapAccountSource:
    """:class:`~cc_usage_widget.contracts.AccountSource` over ``claude_swap``.

    Args:
        settings: our widget settings - a mapping or a ``state.SettingsStore``.
            Only ``autoswitch_enabled`` is read, as the first-run default for
            the toggle. When a store is passed, the toggle is mirrored back
            into it so our file and claude-swap's stay in agreement.
        switcher: an existing ``ClaudeAccountSwitcher`` to adopt. Tests inject a
            fake here; production leaves it ``None`` so one is constructed.
        logger: destination for degradation warnings.
    """

    def __init__(
        self,
        *,
        settings: Any = None,
        switcher: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._log = logger or LOGGER
        self._settings_obj = settings
        self._injected_switcher = switcher

        # One lock guards every piece of adapter state below. Held only for
        # bookkeeping, never across a blocking claude-swap call.
        self._lock = threading.RLock()
        # Non-reentrant: the in-flight guard for a snapshot pass.
        self._take_lock = threading.Lock()

        defaults = normalize_settings(_as_mapping(settings))
        self._fallback_enabled: bool = bool(defaults["autoswitch_enabled"])

        self._backend: _Backend | None = None
        self._backend_retry_at: float = 0.0
        self._last_error: str | None = None

        self._snapshot: Any | None = None
        self._snapshot_at: float = 0.0

        # Derived per snapshot, for the extras below the protocol.
        self._sentinels: dict[int, str] = {}
        # The STRUCTURED key behind each note ("re-login needed", "api key",
        # ...). Kept beside the prose so the menu classifies on a stable
        # identifier instead of substring-matching upstream's wording.
        self._sentinel_kinds: dict[int, str] = {}
        # All-exhausted WARNING dedupe: holds an _episode_key(), NOT a
        # raw line (the line's timestamp jitters). See _drain_events.
        self._last_exhausted_key: str | None = None
        self._last_exhausted_warn_at: float = 0.0
        # The engine's standing verdict for the menu bar (see _drain_events).
        self._alert: tuple[str, str] | None = None
        # no-viable-target WARNING dedupe, same shape as _last_exhausted_key:
        # the engine re-emits the event every blocked tick.
        self._last_no_target_key: str | None = None
        # (mtime, parsed) of the last read of claude-swap's autoswitch state, so
        # every reader of that file costs one stat in the steady state.
        self._state_cache: tuple[float | None, Mapping[str, Any] | None] = (None, None)
        # Previous pass's active slot, and the slot a switch WE made is expected
        # to land on (consumed by the next pass). Together these are the whole
        # external-switch detector: claude-swap re-derives the active login from
        # ~/.claude.json every pass, so a switch another actor made is visible -
        # but only if something compares two passes (2026-09-01: three ghost
        # flips tonight left no line anywhere).
        self._last_active_number: str | None = None
        self._expect_active: str | None = None
        # The widget's OWN verdict, kept apart from the engine's `_alert`: an
        # ordinary no-switch tick clears `_alert` (correctly - the engine moved
        # on), and in the first cut that took this verdict with it within 60 s
        # (review 2026-09-01). Only a widget-made switch retracts it.
        self._external_alert: tuple[str, str] | None = None
        # swap-3 flip forensics, all in memory. Wall clock of the previous
        # completed pass (the window a flip happened in); the parsed switch
        # lines of claude-swap.log's tail keyed on its mtime; ghost flips per
        # target slot, aged out by GHOST_FLIP_WINDOW_SECONDS; and the standing
        # "login fight" as (slot, line), re-validated against that window on
        # every read. `_wall` is the clock for all of it (tests swap it).
        self._wall: Callable[[], float] = time.time
        self._last_pass_wall: float | None = None
        self._swap_log_cache: tuple[float, tuple[tuple[float, str, str], ...]] | None = None
        self._ghost_flips: dict[str, deque[float]] = {}
        self._login_fight: tuple[str, str] | None = None
        # swap-1: slot -> (HTTP status, fetched_at) of the last qualifying
        # refusal seen, so the hourly 429 upstream's backoff interleaves with
        # the 403s (slot 6: four 403s, then an hour of 429) does not blink the
        # note off. Holds only while the slot is still failing and has had no
        # good fetch since; in memory only.
        self._fetch_failing_seen: dict[int, tuple[int, Any]] = {}
        self._switchable: frozenset[int] = frozenset()
        self._alias_by_slot: dict[int, str] = {}

        self._engine: Any | None = None
        self._engine_policy_mtime: float | None = None
        self._policy_threshold: float | None = None  # W4
        self._policy_models_cache: tuple[str, ...] = ()  # UX-1a
        self._next_tick_at: float = 0.0

        self._warned_main_thread = False

        self._events: list[Any] = []
        self._event_lock = threading.Lock()
        self._event_log: deque[str] = deque(maxlen=_EVENT_LOG_LIMIT)

        # (settings.json mtime, enabled|None) - one stat per read in the
        # steady state instead of a parse.
        self._enabled_cache: tuple[float | None, bool | None] | None = None

        # Set once a write to claude-swap's settings.json has failed; from then
        # on `autoswitch_enabled()` trusts `_fallback_enabled` instead of the
        # file it could not update.
        self._enabled_write_failed = False

    # -- diagnostics --------------------------------------------------------

    @property
    def last_error(self) -> str | None:
        """Why the last claude-swap interaction degraded, or ``None``.

        Cleared by the next successful snapshot pass. ``app.py`` renders this
        under :data:`ACCOUNTS_UNAVAILABLE` so a failure is visible rather than
        silent (Rule 12).
        """
        with self._lock:
            return self._last_error

    @property
    def available(self) -> bool:
        """Whether we have usable account data.

        False means the menu must render :data:`ACCOUNTS_UNAVAILABLE`: either
        ``claude_swap`` could not be reached at all, or no snapshot has ever
        succeeded. A *stale* snapshot still counts as available - it is
        age-annotated per row instead (SPEC 4.3).
        """
        with self._lock:
            return self._snapshot is not None

    def _record_error(self, what: str, exc: BaseException | None = None) -> None:
        """Store and log one degradation reason. Never raises."""
        message = f"{what}: {type(exc).__name__}: {exc}" if exc is not None else what
        with self._lock:
            first = self._last_error != message
            self._last_error = message
        if first:
            self._log.warning("accounts: %s", message)
        else:
            self._log.debug("accounts: %s", message)

    # -- backend ------------------------------------------------------------

    def _backend_or_none(self) -> _Backend | None:
        """The backend handle, importing it on first use.

        A failure is recorded and retried no sooner than
        :data:`_BACKEND_RETRY_S` later, so a locked Keychain at launch heals
        itself and a genuinely broken install does not spin.
        """
        with self._lock:
            if self._backend is not None:
                return self._backend
            if time.monotonic() < self._backend_retry_at:
                return None
            self._backend_retry_at = time.monotonic() + _BACKEND_RETRY_S
        try:
            backend = _import_backend(self._injected_switcher)
        except ImportError:
            # The overwhelmingly common case on someone else's machine:
            # claude-swap simply is not installed. That is a normal
            # configuration, not a fault, so say so in plain words instead of
            # showing a raw Python exception in the menu. Cost tracking works
            # without it; only the account section needs it.
            self._record_error(
                "claude-swap not installed - account features are off "
                "(cost tracking still works)"
            )
            return None
        except Exception as exc:
            # A real failure: claude-swap IS present but unusable (locked
            # Keychain, corrupt state, upstream rename). Keep the cause.
            self._record_error("claude-swap unavailable", exc)
            return None
        with self._lock:
            self._backend = backend
        self._log.debug("accounts: claude-swap backend ready (%s)", backend.backup_dir)
        return backend

    # -- AccountSource: reads ----------------------------------------------

    def refresh(self, *, force: bool = False) -> None:
        """Re-read claude-swap's usage store.

        ``force=False`` respects claude-swap's own pacing (the store decides
        network eligibility; we only add a small floor between passes).
        ``force=True`` backs the ``Refresh now`` menu item and asks the store
        for a full pass - which is still capped by its serve TTL, so it is
        honest about not being able to conjure fresher data.

        Blocking. Never raises: a failed pass leaves the previous snapshot in
        place and records :attr:`last_error`.
        """
        backend = self._backend_or_none()
        if backend is None:
            return

        now = time.monotonic()
        with self._lock:
            if (
                not force
                and self._snapshot is not None
                and (now - self._snapshot_at) < _MIN_TAKE_INTERVAL_S
            ):
                return
            store_only = self._engine is not None

        if not self._take_lock.acquire(blocking=False):
            # Another worker is already taking a snapshot; its result lands in
            # the same cache. Queueing here would only serialise Keychain hits.
            return
        try:
            snapshot = backend.snapshot_source.take(full=force, store_only=store_only)
        except Exception as exc:
            self._record_error("usage snapshot failed", exc)
            return
        finally:
            self._take_lock.release()

        with self._lock:
            previous = self._last_active_number
            # Consumed once, whether or not the active actually moved: an
            # expectation that outlives the pass it was made for would swallow
            # the NEXT external switch to the same slot.
            expected, self._expect_active = self._expect_active, None
            current = getattr(snapshot, "active_number", None)
            current = str(current) if isinstance(current, str) and current else None
            self._last_active_number = current
            self._snapshot = snapshot
            self._snapshot_at = time.monotonic()
            self._last_error = None
            since, self._last_pass_wall = self._last_pass_wall, self._wall()
        if not self._note_reverted_switch(expected, current, since=since):
            self._note_external_switch(previous, current, expected, since=since)

    def _note_reverted_switch(
        self, expected: str | None, current: str | None, *, since: float | None = None
    ) -> bool:
        """Report a switch of ours that did not survive to this pass (swap-2).

        ``expected`` is the slot an engine or menu switch just landed on. If a
        completed pass reads a DIFFERENT active slot, something put the old
        login back in between - on 2026-09-24 twice within a minute, while the
        recent-events block claimed the switch had happened. The one-shot
        expectation used to be consumed here without a word. Returns whether
        it reported, so the external-switch check does not report it twice.

        The revert is itself a flip, classified like any other (swap-3): with
        no ``Switched from account {expected} to {current}`` line in
        claude-swap.log it is a config-only rewrite and counts toward the
        login fight - on 2026-09-24 the engine's 1 -> 5 at 20:24:38 was put
        back to 1 by 20:25:37, before any pass saw slot 5, and a revert that
        skipped the ghost counter could hold the fight alert back.
        """
        if expected is None or expected == _ANY_SLOT or current is None:
            return False
        if current == expected:
            return False
        LOGGER.warning(
            "accounts: switch to %s did not stick: active is %s (reverted "
            "before the next pass)",
            expected,
            current,
        )
        if self._classify_flip(expected, current, since) == "ghost":
            self._note_ghost_flip(expected, current, reverted=True)
            return True
        line = f"{_stamp()} switch →{expected} reverted to {current}"
        with self._lock:
            self._event_log.append(line)
            self._external_alert = (ALERT_EXTERNAL_SWITCH, line)
        return True

    def _note_external_switch(
        self,
        previous: str | None,
        current: str | None,
        expected: str | None,
        *,
        since: float | None = None,
    ) -> None:
        """Report an active-account change the widget did not cause.

        Called once per completed snapshot pass. Silent unless *all* of these
        hold, because a false "someone else switched" is worse than none:

        * both passes knew which slot was active (the first pass after start
          has no ``previous`` - we cannot say who moved it, so we do not);
        * the slot actually changed;
        * no switch of ours is outstanding - an engine ``switch`` event drained
          in this pass, or a manual switch recorded by :meth:`switch_to`, both
          of which set ``_expect_active`` to the slot they aimed at.

        The alert stands until the widget itself switches again (an engine
        switch or a menu click clears it); it is deliberately NOT cleared by a
        later quiet tick, because "another actor is driving this login" stays
        true until we take the wheel back.
        """
        if previous is None or current is None or current == previous:
            return
        if expected is not None and expected in (current, _ANY_SLOT):
            return
        # swap-3: WHO moved it decides the remedy. A real `cswap` switch logs
        # "Switched from account X to Y" in claude-swap.log; a running Claude
        # Code session rewriting ~/.claude.json to its own login logs nothing
        # (2026-09-24: six flips to account 1, none logged). Unknown (log
        # unreadable) keeps the generic verdict - never a guess.
        verdict = self._classify_flip(previous, current, since)
        if verdict == "ghost":
            self._note_ghost_flip(previous, current)
            return
        LOGGER.warning(
            "accounts: active changed outside the widget: %s -> %s (%s)",
            previous,
            current,
            "claude-swap.log records it: another cswap actor"
            if verdict == "cswap"
            else "no engine or manual switch; a running session's token "
            "refresh or another cswap actor",
        )
        line = f"{_stamp()} active {previous}\u2192{current} (external)"
        with self._lock:
            self._event_log.append(line)
            self._external_alert = (ALERT_EXTERNAL_SWITCH, line)

    def _alias_or_slot(self, slot: str) -> str:
        """Display alias of *slot* from the last built rows, else ``account-N``."""
        with self._lock:
            alias = self._alias_by_slot.get(_safe_int(slot, default=-1))
        return alias or f"account-{slot}"

    def _note_ghost_flip(
        self, previous: str, current: str, *, reverted: bool = False
    ) -> None:
        """Report a config-only flip; at the fight count, a standing alert.

        Never pauses the engine: the flip is reported, the engine's repair is
        still its own call. The per-slot deque is in memory only and ages out
        by :data:`GHOST_FLIP_WINDOW_SECONDS`. ``reverted`` marks a flip that
        undid a switch of ours before a pass saw it (``previous`` is then the
        slot we switched to); the per-flip line says so.
        """
        now = self._wall()
        target, holder = self._alias_or_slot(current), self._alias_or_slot(previous)
        with self._lock:
            flips = self._ghost_flips.setdefault(current, deque())
            while flips and now - flips[0] > GHOST_FLIP_WINDOW_SECONDS:
                flips.popleft()
            flips.append(now)
            count = len(flips)
        LOGGER.warning(
            "accounts: active changed outside the widget: %s -> %s (config-only: "
            "no cswap switch in claude-swap.log; a running Claude Code session "
            "rewrote ~/.claude.json) [%d in the last hour onto %s]",
            previous,
            current,
            count,
            current,
        )
        undone = f"switch \u2192{previous} reverted: " if reverted else ""
        line = (
            f"{_stamp()} {undone}~/.claude.json rewritten to {target} by a running "
            f"Claude Code session; keychain still holds {holder}"
        )
        fight: tuple[str, str] | None = None
        if count >= GHOST_FLIP_FIGHT_COUNT:
            line = (
                f"{_stamp()} {LOGIN_FIGHT_MARK} running Claude Code sessions restored "
                f"{target} (slot {current}) {count}\u00d7 in the last hour "
                f"(~/.claude.json rewritten; keychain still holds {holder}). Fix: "
                f"per-terminal profiles (`cswap map <N> <dir>` + `cswap run <N> "
                f"--share-history`), or restart sessions started before the last "
                f"switch"
            )
            fight = (current, line)
        with self._lock:
            self._event_log.append(line)
            self._external_alert = (ALERT_GHOST_FLIP, line)
            if fight is not None:
                self._login_fight = fight

    def _standing_login_fight(self) -> tuple[str, str] | None:
        """The login-fight alert while its window still holds the count.

        It must outlive the engine's own repair switch (which retracts the
        per-flip alert within a minute), and must end on its own once the
        flips age out - so it is re-validated on every read, not remembered.
        """
        now = self._wall()
        with self._lock:
            fight = self._login_fight
            if fight is None:
                return None
            flips = self._ghost_flips.get(fight[0], deque())
            while flips and now - flips[0] > GHOST_FLIP_WINDOW_SECONDS:
                flips.popleft()
            if len(flips) >= GHOST_FLIP_FIGHT_COUNT:
                return (ALERT_GHOST_FLIP, fight[1])
            self._login_fight = None
            return None

    def _classify_flip(
        self, previous: str, current: str, since: float | None
    ) -> str | None:
        """``"cswap"``, ``"ghost"``, or ``None`` when the log cannot say.

        Reads only the last :data:`_SWAP_LOG_TAIL_BYTES` of claude-swap.log,
        and only when its mtime changed since the last read; the parsed switch
        lines are cached against that mtime. A line ``Switched from account
        {previous} to {current}`` stamped inside the interval since the last
        completed pass (\u00b1 :data:`_SWAP_LOG_SLACK_S`) means a cswap actor.
        """
        try:
            backend = self._backend_or_none()
            if backend is None:
                return None
            path = Path(backend.backup_dir) / _SWAP_LOG_NAME
            mtime = path.stat().st_mtime
            with self._lock:
                cached = self._swap_log_cache
            if cached is not None and cached[0] == mtime:
                switches = cached[1]
            else:
                switches = _read_switch_lines(path)
                with self._lock:
                    self._swap_log_cache = (mtime, switches)
        except Exception as exc:
            self._log.debug("accounts: claude-swap.log unreadable: %r", exc)
            return None
        now = self._wall()
        low = (since if since is not None else now) - _SWAP_LOG_SLACK_S
        high = now + _SWAP_LOG_SLACK_S
        for when, source, target in switches:
            if source == previous and target == current and low <= when <= high:
                return "cswap"
        return "ghost"

    def rows(self) -> tuple[AccountRow, ...]:
        """All accounts, ordered by :attr:`AccountRow.slot` ascending.

        Refreshes first when there is no snapshot yet or the cached one is
        older than :data:`_ROWS_MAX_AGE_S`, so a caller that only ticks
        ``rows()`` still sees live data. Returns ``()`` when claude-swap is
        unavailable - the caller renders :data:`ACCOUNTS_UNAVAILABLE`.

        **It will not stall the AppKit main thread.** Called from there with a
        cached snapshot in hand, it serves that snapshot however old it is
        rather than taking a blocking pass - the rows carry
        :attr:`AccountRow.usage_age_seconds`, so ageing data is visible in the
        menu (SPEC 4.3) instead of freezing the menu bar (SPEC 2.3). A caller
        on the main thread must therefore drive :meth:`refresh` from its
        background worker; the first such call is logged as a warning.
        """
        now = time.monotonic()
        with self._lock:
            snapshot = self._snapshot
            age = now - self._snapshot_at
        if snapshot is None or age > _ROWS_MAX_AGE_S:
            # The thread test dominates, and deliberately so. Testing
            # `snapshot is not None and _on_main_thread()` inverted the guard for
            # the case it most needs to cover: with no snapshot yet - i.e. the
            # first call, at launch - a main-thread caller fell through to the
            # blocking `refresh()` and froze the menu bar for a Keychain
            # subprocess plus up to three usage-API requests. Never refresh from
            # the main thread; an empty tuple that fills in a second later is the
            # correct answer there (SPEC 2.3).
            if _on_main_thread():
                self._warn_main_thread_rows(age)
            else:
                self.refresh()
                with self._lock:
                    snapshot = self._snapshot
        if snapshot is None:
            return ()
        return self._build_rows(snapshot)

    def _warn_main_thread_rows(self, age: float) -> None:
        """Warn once that a stale snapshot was served to the main thread."""
        with self._lock:
            first = not self._warned_main_thread
            self._warned_main_thread = True
        if first:
            with self._lock:
                have = self._snapshot is not None
            self._log.warning(
                "accounts: rows() called on the AppKit main thread (%s); refusing "
                "to take a blocking snapshot pass there. Call refresh() from the "
                "background worker.",
                f"serving a {age:.0f}s-old snapshot" if have else "no snapshot yet",
            )

    def active(self) -> AccountRow | None:
        """The active account, or ``None`` if none is active."""
        for row in self.rows():
            if row.is_active:
                return row
        return None

    # -- row construction --------------------------------------------------

    def _build_rows(self, snapshot: Any) -> tuple[AccountRow, ...]:
        """Map an ``AccountsSnapshot`` onto our frozen rows.

        Rebuilt on each call rather than cached with the snapshot, because two
        derived values must be current at *render* time, not at fetch time: the
        measurement's age (SPEC 4.3) and the reset clock strings (a string
        formatted an hour ago says the wrong thing). Both come from the same
        stored ``resets_at``/``fetched_at``, so nothing is invented.
        """
        backend = self._backend
        now = time.time()
        accounts = getattr(snapshot, "accounts", None) or ()
        active_number = getattr(snapshot, "active_number", None)

        rows: list[AccountRow] = []
        sentinels: dict[int, str] = {}
        sentinel_kinds: dict[int, str] = {}
        switchable: set[int] = set()
        aliases: dict[int, str] = {}
        for account in accounts:
            try:
                row = self._build_row(account, active_number, now, backend)
            except Exception as exc:  # one bad account must not blank the menu
                self._log.debug("accounts: skipped an account row: %r", exc)
                continue
            rows.append(row)
            aliases[row.slot] = row.alias
            derived = self._sentinel_note(account, backend)
            if derived is None:
                # swap-1: no upstream word for it, so the refusal is named here.
                derived = self._fetch_failure_note(account)
            if derived is not None:
                kind, note = derived
                sentinels[row.slot] = note
                sentinel_kinds[row.slot] = kind
            if row.switchable:
                switchable.add(row.slot)

        rows.sort(key=lambda row: row.slot)
        with self._lock:
            self._sentinels = sentinels
            self._sentinel_kinds = sentinel_kinds
            self._switchable = frozenset(switchable)
            self._alias_by_slot = aliases
        return tuple(rows)

    def _build_row(
        self,
        account: Any,
        active_number: Any,
        now: float,
        backend: _Backend | None,
    ) -> AccountRow:
        """One :class:`AccountRow` from one ``AccountSnapshot``."""
        slot = _safe_int(getattr(account, "number", ""))
        email = str(getattr(account, "email", "") or "")
        alias = str(getattr(account, "alias", "") or "").strip() or _alias_from_email(
            email, slot
        )
        is_active = bool(getattr(account, "is_active", False)) or (
            isinstance(active_number, str)
            and active_number != ""
            and str(getattr(account, "number", "")) == active_number
        )

        entry = getattr(account, "usage", None)
        last_good = getattr(entry, "last_good", None)
        if not isinstance(last_good, Mapping):
            last_good = None

        roll = backend.roll_weekly if backend is not None else _identity_roll
        five_hour = _window(last_good, "five_hour")
        # Weekly windows only: the 5-hour window has no fixed weekly cadence to
        # roll forward from, so upstream never rolls it either.
        raw_seven_day = _window(last_good, "seven_day")
        seven_day = _safe_roll(roll, raw_seven_day, now)

        # swap-1: a window whose reset already passed describes a window that
        # has ENDED. Judged on what is shown (post-roll): upstream's roll turns
        # a passed weekly reset into an honest 0% for a slot that is being
        # polled - but for a slot whose fetches keep failing that 0% is as
        # unverified as the old figure, so the RAW reset counts there too.
        failing = self._fetch_failing(slot, entry) is not None

        def ended(shown: Any, raw: Any) -> bool:
            return _reset_passed(shown, now) or (failing and _reset_passed(raw, now))

        expired: list[str] = []
        if ended(five_hour, five_hour):
            expired.append("five_hour")
        if ended(seven_day, raw_seven_day):
            expired.append("seven_day")

        scoped_windows: list[tuple[str, Pct]] = []
        scoped_resets: list[tuple[str, str]] = []
        # The ROLLED scoped windows, kept for pace: compute_pace needs resets_at
        # and pct from the same post-roll view the percentages came from.
        scoped_rolled: list[tuple[str, Any]] = []
        raw_scoped = last_good.get("scoped") if last_good is not None else None
        if isinstance(raw_scoped, (list, tuple)):
            for raw in raw_scoped:
                if not isinstance(raw, Mapping):
                    continue
                window = _safe_roll(roll, raw, now)
                name = window.get("name") if isinstance(window, Mapping) else None
                pct = _pct(window)
                if not isinstance(name, str) or not name or pct is None:
                    continue
                scoped_windows.append((name, pct))
                scoped_rolled.append((name, window))
                if ended(window, raw) and name not in expired:
                    expired.append(name)
                clock = self._reset_clock(window, backend)
                if clock:
                    scoped_resets.append((name, clock))

        # F2a: extra-usage spend, read-only from the same cached lastGood.
        raw_spend = last_good.get("spend") if last_good is not None else None
        spend = _spend(raw_spend)
        spend_resets_at = (
            self._reset_clock(raw_spend, backend)
            if spend is not None and _iso_ts(raw_spend.get("resets_at")) is not None
            else None
        )

        return AccountRow(
            slot=slot,
            alias=alias,
            email=email,
            is_active=is_active,
            # Upstream's `switchable` means "this slot has BOTH stored
            # credentials and a config backup", i.e. it can be activated
            # without re-adding the account (switcher._account_is_switchable).
            # AccountRow.switchable is the ONLY thing that makes a row
            # clickable, and it was never populated here: a slot claude-swap
            # cannot activate still rendered as a live menu item whose click
            # was guaranteed to fail. Defaults to True when the attribute is
            # absent, matching this adapter's fail-open policy (a missing
            # upstream field must not disable manual switching).
            switchable=bool(getattr(account, "switchable", True)),
            # Upstream's `cswap disable` flag, carried so the title's room count
            # can skip a slot the engine would never rotate onto.
            disabled=bool(getattr(account, "disabled", False)),
            five_hour_pct=_pct(five_hour),
            seven_day_pct=_pct(seven_day),
            scoped_windows=tuple(scoped_windows),
            five_hour_resets_at=self._reset_clock(five_hour, backend),
            seven_day_resets_at=self._reset_clock(seven_day, backend),
            scoped_resets_at=tuple(scoped_resets),
            usage_age_seconds=_usage_age(entry, now),
            pace_ahead=_pace_ahead(entry, seven_day, scoped_rolled),
            expired_windows=tuple(expired),
            spend_used=spend[0] if spend is not None else None,
            spend_limit=spend[1] if spend is not None else None,
            spend_pct=spend[2] if spend is not None else None,
            spend_currency=spend[3] if spend is not None else None,
            spend_resets_at=spend_resets_at,
        )

    def _reset_clock(
        self, window: Any, backend: _Backend | None
    ) -> str | None:
        """The reset string for one window, rendered by claude-swap.

        ``oauth.fresh_reset_strings`` turns the API's stored ``resets_at`` into
        ``(countdown, clock)``; we surface the clock (``"10:59"``, or
        ``"Aug 24 14:50"`` when it is not today). We never compute a reset time
        ourselves (SPEC 4.3) - if upstream's formatter is unavailable we fall
        back to the raw ``resets_at`` string exactly as the API sent it.
        """
        if not isinstance(window, Mapping):
            return None
        if backend is not None:
            try:
                cell = backend.fresh_reset_strings(window)
            except Exception as exc:  # pragma: no cover - upstream refactor
                self._log.debug("accounts: reset formatting failed: %r", exc)
                cell = None
            if cell:
                try:
                    return str(cell[1])
                except (IndexError, TypeError):  # pragma: no cover
                    pass
        raw = window.get("resets_at")
        return str(raw) if isinstance(raw, str) and raw else None

    def _sentinel_note(
        self, account: Any, backend: _Backend | None
    ) -> tuple[str, str] | None:
        """``(kind, human note)`` for a derived usage state, or ``None``.

        The *kind* is upstream's own sentinel key — a short stable identifier
        (``"re-login needed"``, ``"api key"``, ``"token expired"``); the note is
        the long prose it maps to. Both are exposed (:meth:`sentinel_for`,
        :meth:`sentinel_kinds`) because :class:`AccountRow` has nowhere to
        carry them, and because the menu must classify on the key: matching the
        prose meant a wording change upstream silently demoted a re-login
        warning to a generic bucket (2026-08-26).
        """
        sentinel = getattr(getattr(account, "usage", None), "sentinel", None)
        if not isinstance(sentinel, str) or not sentinel:
            return None
        notes = backend.sentinel_notes if backend is not None else {}
        try:
            return sentinel, str(notes.get(sentinel, sentinel))
        except Exception:  # pragma: no cover - defensive
            return sentinel, sentinel

    def _fetch_failure_note(self, account: Any) -> tuple[str, str] | None:
        """``(FETCH_FAILING_KIND, note)`` for a slot whose polls keep being
        refused, or ``None``. Same shape as :meth:`_sentinel_note`.

        Derived on every build from claude-swap's cache entry (``last_error``,
        ``consecutive_failures``, ``fetched_at``) and never stored, so it
        clears on the first build after the slot recovers. The note spells
        out that ``cswap disable`` does NOT stop the polling (only ``remove``
        does), because that is the remedy an operator reaches for first.
        """
        try:
            entry = getattr(account, "usage", None)
            code = self._fetch_failing(_safe_int(getattr(account, "number", "")), entry)
            if code is None:
                return None
            failures = int(getattr(entry, "consecutive_failures", 0))
            number = str(getattr(account, "number", "") or "").strip() or "N"
            email = str(getattr(account, "email", "") or "").strip() or "that account"
            what = "forbidden" if code == 403 else "refused"
            since = ""
            fetched = getattr(entry, "fetched_at", None)
            if isinstance(fetched, (int, float)) and not isinstance(fetched, bool):
                local = time.localtime(float(fetched))
                since = f" since {time.strftime('%b', local)} {local.tm_mday}"
            head = f"usage {what} (HTTP {code}){since}"
            if code == 429 and since:
                # Only the backoff is visible - say what is known, no more.
                days = int((time.time() - float(fetched)) // 86400)
                head = (
                    f"usage refused for {days} days{since} (last error HTTP 429, "
                    f"upstream backoff)"
                )
            note = (
                f"{head} · {failures} failed polls "
                f"— plan lapsed or access revoked? log in as {email} and run "
                f"`cswap add`, or `cswap remove {number}` to stop polling "
                f"(`cswap disable` only takes it out of rotation; it is still polled)"
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._log.debug("accounts: fetch-failure note failed: %r", exc)
            return None
        return FETCH_FAILING_KIND, note

    def _fetch_failing(self, slot: int, entry: Any) -> int | None:
        """HTTP status of *slot*'s standing fetch refusal, or ``None``.

        A qualifying refusal (:func:`_fetch_failing_code`) is remembered with
        the entry's ``fetched_at``. While the slot keeps failing with anything
        else - upstream's own 429 budget backoff, a network blip - and has had
        no good fetch since (``fetched_at`` unchanged), the refusal still
        stands: on 2026-09-25 slot 6's ``lastError`` read ``http-429`` for an
        hour after every four 403s. A 429 starts it only when the entry has
        not fetched for a day (:func:`_cold_backoff_code`) - the cold-start
        case, where no 403 was ever seen in this process.
        Idempotent for one entry, so the row and the note agree.
        """
        code = _fetch_failing_code(entry)
        fetched = getattr(entry, "fetched_at", None) if entry is not None else None
        with self._lock:
            if code is not None:
                self._fetch_failing_seen[slot] = (code, fetched)
                return code
            seen = self._fetch_failing_seen.get(slot)
            still_failing = bool(getattr(entry, "last_error", None)) if entry is not None else False
            if seen is not None and still_failing and seen[1] == fetched:
                return seen[0]
            # Nothing seen in this process (a cold start inside the backoff
            # hour): the persisted entry alone can still prove a standing
            # refusal - a day and more of failures with no good fetch.
            cold = _cold_backoff_code(entry, time.time())
            if cold is not None:
                self._fetch_failing_seen[slot] = (cold, fetched)
                return cold
            self._fetch_failing_seen.pop(slot, None)
            return None

    # -- extras beyond the protocol ---------------------------------------

    def sentinel_for(self, slot: int) -> str | None:
        """Derived-state note for one slot, from the last built rows."""
        with self._lock:
            return self._sentinels.get(int(slot))

    def sentinels(self) -> dict[int, str]:
        """Every slot with a derived usage state, as ``{slot: note}``."""
        with self._lock:
            return dict(self._sentinels)

    def sentinel_kinds(self) -> dict[int, str]:
        """The stable key behind each note, as ``{slot: kind}``.

        Keys are upstream's own (``"re-login needed"``, ``"token expired"``,
        ``"api key"``, ...). Classify on these, never on the prose.
        """
        with self._lock:
            return dict(self._sentinel_kinds)

    def switchable_slots(self) -> frozenset[int]:
        """Slots claude-swap reports as switchable (they have a usable backup).

        The ``Switch account`` submenu should disable the others rather than
        offering a switch that will fail.
        """
        with self._lock:
            return self._switchable

    def recent_events(self) -> tuple[str, ...]:
        """Recent autoswitch event lines (newest last), for the menu/log."""
        with self._lock:
            return tuple(self._event_log)

    def current_alert(self) -> tuple[str, str] | None:
        """The engine's standing verdict as ``(kind, line)``, or ``None``.

        Set by an ``all-exhausted`` / ``account-quarantined`` / ``error`` event
        and cleared by the next switch or no-switch tick, so the menu bar shows
        what the engine currently believes rather than a one-off log line.
        Before 2026-08-25 these events reached only the log: the fleet sat at
        "all accounts exhausted" for hours while the title read ``vlad 0%``.

        Two things here are DERIVED rather than remembered, because a verdict
        that outlives what produced it is exactly the bug this feature exists
        to end (both shipped in the first cut and were fixed 2026-08-26):

        * with autoswitch off there is no engine and therefore no ENGINE
          verdict — checked on every read, so a toggle written by another
          process (``cswap config set``) is honoured without waiting for a
          tick. An ``external-switch`` verdict is ours, not the engine's, and
          is the one thing that survives the toggle;
        * a quarantine that predates this process never fires an event, so it
          falls back to the state file (see
          :meth:`_persisted_quarantine_alert`).
        """
        with self._lock:
            alert = self._alert
            external = self._external_alert
        if not self.autoswitch_enabled():
            # An external switch is the widget's OWN observation, not an engine
            # verdict, so it survives the toggle - and autoswitch is OFF by
            # default, which is precisely when a rival actor owns the login.
            # Every other verdict belongs to the engine and dies with it.
            return external or self._standing_login_fight()
        # Precedence is deliberate: a live verdict outranks the state file.
        # An engine that will not start is the most urgent of all — nothing is
        # switching for ANY account — so it legitimately hides a per-account
        # quarantine, which is still spelled out on that account's own row.
        # A login fight is the widget's own observation too, and stands past
        # the engine's repair switch that retracts `external` (swap-3).
        return (
            alert
            or external
            or self._standing_login_fight()
            or self._persisted_quarantine_alert()
        )

    def _autoswitch_state_path(self) -> Path | None:
        """claude-swap's ``autoswitch_state.json``, or ``None`` if unavailable."""
        backend = self._backend_or_none()
        if backend is None:
            return None
        try:
            return Path(backend.backup_dir) / backend.state_filename
        except Exception as exc:  # pragma: no cover - upstream refactor
            self._log.debug("accounts: autoswitch state path unknown: %r", exc)
            return None

    def _persisted_quarantine_alert(self) -> tuple[str, str] | None:
        """A quarantine already recorded in ``autoswitch_state.json``.

        ``QuarantineEvent`` fires only at the TRANSITION, and upstream filters
        an already-quarantined slot out of the candidate path before it could
        fire again — so a quarantine that predates this process (a restart, or
        the widget being started after the fact) reaches the menu only if we
        read the file. Re-reading it also means the alert disappears on its own
        once upstream releases the quarantine, instead of needing a clear.

        mtime-gated: one ``stat`` in the steady state. Unreadable or corrupt
        state is "no alert", never an exception — the menu must not go down
        because a file upstream owns is mid-write.
        """
        data = self._autoswitch_state()
        alert: tuple[str, str] | None = None
        try:
            entries = data.get("quarantine") if isinstance(data, Mapping) else None
            if isinstance(entries, Mapping) and entries:
                # Deterministic pick so the line does not flap between slots;
                # the count carries the rest rather than dropping them.
                slot = sorted(entries, key=lambda k: (len(str(k)), str(k)))[0]
                entry = entries.get(slot)
                entry = entry if isinstance(entry, Mapping) else {}
                email = str(entry.get("email") or "")
                reason = str(entry.get("reason") or "unknown reason")
                extra = len(entries) - 1
                more = f" (+{extra} more)" if extra > 0 else ""
                # Word-for-word upstream's QuarantineEvent.human(), so the two
                # paths to this line cannot disagree in the menu.
                alert = (
                    ALERT_ACCOUNT_QUARANTINED,
                    f"Account-{slot} ({email}) quarantined: {reason}. "
                    f"Log in with it and run 'cswap --add-account --slot {slot}' "
                    f"to recover.{more}",
                )
        except (AttributeError, TypeError, ValueError) as exc:  # pragma: no cover
            self._log.debug("accounts: autoswitch quarantine unreadable: %r", exc)
            return None
        return alert

    def _autoswitch_state(self) -> Mapping[str, Any] | None:
        """claude-swap's parsed ``autoswitch_state.json``, or ``None``.

        mtime-gated: one ``stat`` in the steady state, which is what lets three
        readers (quarantine, last-switch, cooldown) share the file without three
        parses per tick. Unreadable, corrupt or non-object state is ``None``,
        never an exception - the menu must not go down because a file upstream
        owns is mid-write.
        """
        path = self._autoswitch_state_path()
        if path is None:
            return None
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            with self._lock:
                self._state_cache = (None, None)
            return None

        with self._lock:
            cached_mtime, cached = self._state_cache
            if cached_mtime == mtime:
                return cached

        data: Mapping[str, Any] | None = None
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._log.debug("accounts: autoswitch state unreadable: %r", exc)
            return None
        if isinstance(parsed, Mapping):
            data = parsed

        with self._lock:
            self._state_cache = (mtime, data)
        return data

    def switch_note(self) -> str | None:
        """One line about the last recorded switch, or ``None``.

        ``last switch 21:05 (at-limit) - cooldown 3m left`` - answers "why is
        the widget sitting still?" from claude-swap's own shared state rather
        than from anything we remember, so a switch made by ``cswap`` or the TUI
        is described too.

        Every part is omitted rather than guessed (SPEC 4.3): no
        ``lastSwitchAt`` -> no line at all; no ``leftTrigger`` -> no reason in
        parentheses; no running engine -> no cooldown clause, because the
        cooldown only binds an engine and reading the policy file per tick just
        to print a number nothing is waiting on is not worth the syscall.
        """
        state = self._autoswitch_state()
        if not state:
            return None
        try:
            when = float(state.get("lastSwitchAt"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if when <= 0:
            return None
        try:
            local = time.localtime(when)
            today = time.localtime()
            recent = (local.tm_year, local.tm_yday) == (today.tm_year, today.tm_yday)
            # A bare clock reads as "today". The state file persists for weeks
            # and autoswitch is off by default, so a switch from another day
            # carries its date rather than implying one this evening (SPEC 4.3).
            stamp = time.strftime("%H:%M" if recent else "%b %d %H:%M", local)
        except (OSError, OverflowError, ValueError):
            return None

        line = f"last switch {stamp}"
        trigger = state.get("leftTrigger")
        if isinstance(trigger, str) and trigger:
            line = f"{line} ({trigger})"

        cooldown = self._engine_cooldown_seconds()
        if cooldown is not None:
            left = cooldown - (time.time() - when)
            if left > 0:
                minutes = int(left // 60) + (1 if left % 60 else 0)
                line = f"{line} \u00b7 cooldown {minutes}m left"
        return line

    def _engine_cooldown_seconds(self) -> float | None:
        """``autoswitch.cooldownSeconds`` as the RUNNING engine sees it.

        Read off the live engine only - no engine means no cooldown is being
        enforced, and inventing one from the settings file would describe a gate
        that is not there.
        """
        with self._lock:
            engine = self._engine
        value = getattr(getattr(engine, "settings", None), "cooldown_seconds", None)
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def autoswitch_threshold(self) -> float | None:
        """claude-swap's current ``autoswitch.threshold``, or ``None``.

        Read-only: the widget shows it, it never sets policy.
        """
        backend = self._backend_or_none()
        if backend is None:
            return None
        try:
            return float(backend.load_policy(backend.backup_dir).threshold)
        except Exception as exc:
            self._record_error("autoswitch policy unreadable", exc)
            return None

    def cached_autoswitch_threshold(self) -> float | None:  # W4
        """The threshold the live engine was built with, or ``None``.

        The cheap sibling of :meth:`autoswitch_threshold`: no file read at
        all, just the value captured in :meth:`_ensure_engine` from the same
        policy object the engine snapshotted, refreshed behind the same
        ``settings.json`` mtime gate. That is what makes it safe to publish on
        every UI tick, and what guarantees a renderer bucketing on it cannot
        disagree with what autoswitch will actually do.

        ``None`` until an engine has been built once — with autoswitch off,
        that is for the whole session. Callers must have their own answer for
        "unknown"; this never guesses one.
        """
        with self._lock:
            return self._policy_threshold

    def cached_autoswitch_models(self) -> tuple[str, ...]:  # UX-1a
        """The per-model weekly windows the live engine binds on, or ``()``.

        ``autoswitch.model`` (e.g. ``("Fable",)``), split by upstream's own
        parser and captured in :meth:`_ensure_engine` from the same policy
        object as :meth:`cached_autoswitch_threshold` - no file read here, so
        it is safe on every UI tick. ``()`` until an engine has been built
        (autoswitch off) or when the policy names no model: "compare 5h/7d
        only", never a guess at which models matter.
        """
        with self._lock:
            return self._policy_models_cache

    def autoswitch_state_path(self) -> Path | None:
        """The shared ``autoswitch_state.json`` this adapter's engine uses.

        Not hardcoded: claude-swap resolves its backup root per platform
        (``~/.claude-swap-backup`` on macOS), and hardcoding would silently
        stop sharing state with ``cswap auto``.
        """
        backend = self._backend_or_none()
        if backend is None:
            return None
        try:
            from claude_swap.autoswitch import STATE_FILENAME

            return backend.backup_dir / STATE_FILENAME
        except Exception as exc:  # pragma: no cover - upstream refactor
            self._record_error("autoswitch state path unknown", exc)
            return None

    # -- AccountSource: switching -----------------------------------------

    def switch_to(self, slot_or_alias: str) -> bool:
        """Switch the active account. Returns True on success.

        Delegates to ``switcher.switch_to`` in JSON mode, which is the
        non-interactive path (the human path can prompt on an ambiguous email -
        fatal on a background thread with no tty). Already being on the target
        counts as success. A fabricated alias (our email-local-part fallback,
        which claude-swap has never heard of) is retried as the slot number.
        """
        backend = self._backend_or_none()
        if backend is None:
            return False
        identifier = str(slot_or_alias).strip()
        if not identifier:
            self._record_error("switch requested with an empty identifier")
            return False

        # Manual switches bypass every engine gate (usage, quarantine, token
        # freshness) BY DESIGN — the click is an operator override. Make the
        # override informed and forensically visible: the 2026-08-25 15:06
        # manual switch landed on a token-dead account and left no log line
        # at all, which cost real diagnosis time.
        target_slot = self._resolve_slot_for_logging(identifier)
        sentinel = None
        if target_slot is not None:
            with self._lock:
                sentinel = self._sentinels.get(target_slot)
        if sentinel:
            LOGGER.warning(
                "manual switch -> %s requested although the target reports "
                "%r — the engine would not have chosen it; proceeding "
                "(operator override)",
                identifier,
                sentinel,
            )
        else:
            LOGGER.info("manual switch -> %s requested", identifier)

        ok = self._attempt_switch(backend, identifier)
        if not ok:
            fallback = self._slot_identifier_for(identifier)
            if fallback is not None and fallback != identifier:
                ok = self._attempt_switch(backend, fallback)
        if ok:
            LOGGER.info("manual switch -> %s succeeded", identifier)
            with self._lock:
                # Claim the change the refresh below is about to see, so our
                # own click is never reported as an external switch. The slot
                # is best-effort; `_ANY_SLOT` still suppresses exactly one pass.
                self._expect_active = (
                    str(target_slot) if target_slot is not None else _ANY_SLOT
                )
                self._event_log.append(f"{_stamp()} manual \u2192 {identifier}")
                # Taking the wheel back retracts "another actor is driving".
                self._external_alert = None
            # Reflect the new active row now rather than up to a tick later.
            self.refresh(force=True)
        return ok

    def _resolve_slot_for_logging(self, identifier: str) -> int | None:
        """Best-effort slot number for *identifier*; never raises.

        Only used to annotate manual-switch log lines with the target's
        sentinel state — resolution failures must not affect the switch.
        """
        try:
            return int(identifier)
        except ValueError:
            pass
        try:
            fallback = self._slot_identifier_for(identifier)
            if fallback is not None:
                return int(fallback)
        except Exception:  # pragma: no cover - defensive
            pass
        return None

    def _attempt_switch(self, backend: _Backend, identifier: str) -> bool:
        """One ``switcher.switch_to`` attempt, fully wrapped."""
        try:
            result = backend.switcher.switch_to(identifier, json_output=True)
        except Exception as exc:
            self._record_error(f"switch to {identifier!r} failed", exc)
            return False
        if not isinstance(result, Mapping):
            # Only the interactive path returns None; treat anything unexpected
            # as a failure rather than reporting a switch that may not have
            # happened.
            self._record_error(f"switch to {identifier!r} returned no result")
            return False
        if result.get("switched") is True:
            return True
        # "already-active" / "activated" are successful end states: the target
        # is the live account, which is all the caller asked for.
        if result.get("reason") in ("already-active", "activated"):
            return True
        self._record_error(
            f"switch to {identifier!r} did not take: "
            f"{result.get('message') or result.get('reason') or 'unknown reason'}"
        )
        return False

    def _slot_identifier_for(self, identifier: str) -> str | None:
        """``str(slot)`` for a cached alias, or ``None`` when it is not ours."""
        with self._lock:
            aliases = dict(self._alias_by_slot)
        wanted = identifier.strip().lower()
        for slot, alias in aliases.items():
            if alias.strip().lower() == wanted:
                return str(slot)
        return None

    def switch_best(self) -> bool:
        """Switch to the switchable account with the most remaining quota.

        Delegates to ``switcher.switch(strategy="best", json_output=True)`` -
        claude-swap's OWN usage-aware pick, folding in the per-model weekly
        windows the autoswitch policy names, so one click lands where the
        engine would have gone. We do not rank the rows ourselves: the menu's
        percentages are a display read that may be minutes old, and a
        second, disagreeing ranking is exactly the kind of invented number
        SPEC 4.3 forbids.

        Unlike :meth:`switch_to` this is **not** an operator override.
        Upstream stays put unless it can prove another account has more
        headroom; that verdict (``already-best``, ``candidates-exhausted``,
        ``usage-unavailable``) is reported rather than swallowed - staying on
        the best account is a success, being unable to tell is not.

        Every ``warnings`` line upstream returns (skipped disabled slots,
        inert model limits) is appended to the event log; ``switch_to`` used
        to drop them on the floor.
        """
        backend = self._backend_or_none()
        if backend is None:
            return False
        models = self._policy_models(backend)
        LOGGER.info(
            "manual switch (best) requested%s",
            f" (model limits: {', '.join(models)})" if models else "",
        )
        try:
            result = backend.switcher.switch(
                strategy="best", json_output=True, models=models
            )
        except Exception as exc:
            self._record_error("switch to the best account failed", exc)
            return False
        if not isinstance(result, Mapping):
            # Only the interactive path returns None; anything else is a
            # switch we cannot confirm, and an unconfirmed switch is a failure.
            self._record_error("switch to the best account returned no result")
            return False

        warnings = _switch_warnings(result)
        # Log EVERY line, then cap only the menu view - the log is the "see
        # the log" the summary line points at, so it cannot be the capped one.
        for warning in warnings:
            LOGGER.warning("switch (best): %s", warning)
        for warning in _switch_warnings_for_log(warnings):
            self._append_event(f"switch (best): {warning}")

        target = self._alias_for_ref(result.get("to")) or "the best account"
        reason = result.get("reason")
        if result.get("switched") is True or reason in ("already-active", "activated"):
            LOGGER.info("manual switch (best) -> %s succeeded", target)
            self._append_event(f"manual switch (best) -> {target}")
            with self._lock:
                # Claim the landing slot so the refresh below does not report
                # our own click as an external switch; taking the wheel back
                # also retracts a standing "another actor is driving" verdict.
                self._expect_active = self._ref_number(result.get("to")) or _ANY_SLOT
                self._external_alert = None
            # Reflect the new active row now rather than up to a tick later.
            self.refresh(force=True)
            return True
        message = result.get("message") or reason or "unknown reason"
        if reason == "already-best":
            # Nothing to do IS the right outcome; do not report it as a fault.
            LOGGER.info("manual switch (best): %s", message)
            self._append_event(f"switch (best): {message}")
            return True
        self._record_error(f"switch to the best account did not take: {message}")
        return False

    def _policy_models(self, backend: _Backend) -> tuple[str, ...]:
        """Per-model weekly windows the autoswitch policy names, or ``()``.

        Same key the engine reads (``autoswitch.model``), split by upstream's
        own parser so a one-click best switch weighs exactly what the engine
        weighs. Any failure means "compare 5h/7d only" - never a guess at
        which models matter.
        """
        try:
            from claude_swap.settings import parse_model_names

            policy = backend.load_policy(backend.backup_dir)
            return tuple(parse_model_names(getattr(policy, "model", None)))
        except Exception as exc:
            LOGGER.debug(
                "autoswitch model limits unreadable (%r); comparing 5h/7d only", exc
            )
            return ()

    def _append_event(self, line: str) -> None:
        """Append one line to the event log read by :meth:`recent_events`.

        Stamped with the local ``HH:MM`` like every other journal line, so the
        "Recent switches" block reads as one timeline whichever path wrote it.
        """
        with self._lock:
            self._event_log.append(f"{_stamp()} {line}")

    # -- AccountSource: autoswitch toggle ---------------------------------

    def autoswitch_enabled(self) -> bool:
        """Whether autoswitch is enabled, as claude-swap's file reports it.

        Reads ``autoswitch.enabled`` from claude-swap's ``settings.json``,
        mtime-gated so the steady state costs one ``stat``. Falls back to our
        ``settings.json``'s ``autoswitch_enabled`` when the key is absent
        (first run) or the file cannot be read.

        Once a write to that file has failed in this session the in-memory flag
        wins outright: re-reading a file we were unable to update would report
        the user's rejected value back at them and re-enable the engine they
        just turned off.
        """
        with self._lock:
            if self._enabled_write_failed:
                return self._fallback_enabled
        path = self._policy_path()
        if path is None:
            with self._lock:
                return self._fallback_enabled
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None

        with self._lock:
            cached = self._enabled_cache
            if cached is not None and cached[0] == mtime:
                value = cached[1]
                return self._fallback_enabled if value is None else value

        value = self._read_enabled(path)
        with self._lock:
            self._enabled_cache = (mtime, value)
            return self._fallback_enabled if value is None else value

    def set_autoswitch_enabled(self, enabled: bool) -> None:
        """Enable/disable autoswitch, writing through to claude-swap.

        The flag lands in claude-swap's own ``settings.json`` under
        ``autoswitch.enabled`` (see :data:`AUTOSWITCH_ENABLED_KEY`), so our
        toggle and ``cswap config set autoswitch.*`` describe one state in one
        file. It is also mirrored into our settings store, when one was handed
        to us, so the first-run default matches next launch.

        Disabling stops the engine immediately - "off" must mean the engine
        does not run (SPEC 6.2).

        Raises:
            RuntimeError: when the write to claude-swap's ``settings.json``
                failed. Swallowing it meant an OFF click looked like it worked,
                the label snapped back to ON within a second (``autoswitch_enabled``
                re-read the unchanged file), and accounts kept being switched -
                with the cause only in :attr:`last_error`, which nothing rendered.
                The in-memory flag still holds for this session, so OFF is
                honoured here even though claude-swap's file disagrees.
        """
        wanted = bool(enabled)
        with self._lock:
            self._fallback_enabled = wanted
            self._enabled_cache = None
            if wanted:
                # Evaluate on the next call rather than waiting out a delay
                # computed before the toggle flipped.
                self._next_tick_at = 0.0

        self._mirror_local_setting(wanted)
        ok = self._write_enabled(wanted)
        with self._lock:
            if ok:
                # Release the latch. It was set on a previous failed write and
                # never cleared, so ONE transient failure made this process
                # ignore claude-swap's settings.json for the rest of its life —
                # a `cswap config set autoswitch.enabled` from the terminal
                # would then never be seen again, with the widget silently
                # disagreeing with the file it claims to mirror (2026-08-26).
                self._enabled_write_failed = False
            else:
                # From here on this process trusts its own flag over the file it
                # could not write, so the toggle cannot silently revert.
                self._enabled_write_failed = True
        if not wanted:
            self._stop_engine()
        if not ok:
            raise RuntimeError(
                f"could not persist autoswitch={'on' if wanted else 'off'} to "
                f"claude-swap: {self.last_error or 'unknown reason'}"
            )

    def _policy_path(self) -> Path | None:
        """claude-swap's ``settings.json`` path, or ``None`` if unavailable."""
        backend = self._backend_or_none()
        if backend is None:
            return None
        try:
            return Path(backend.settings_path(backend.backup_dir))
        except Exception as exc:  # pragma: no cover - upstream refactor
            self._record_error("claude-swap settings path unknown", exc)
            return None

    def _read_enabled(self, path: Path) -> bool | None:
        """``autoswitch.enabled`` from *path*, or ``None`` when not present.

        ``None`` (missing key, missing file, junk value) means "claude-swap
        reports nothing", which is exactly when the contract says to fall back
        to our own default. A corrupt file is reported once, not fatal.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self._record_error(f"could not read {path}", exc)
            return None
        if not isinstance(raw, Mapping):
            return None
        section = raw.get(AUTOSWITCH_SECTION)
        if not isinstance(section, Mapping) or AUTOSWITCH_ENABLED_KEY not in section:
            return None
        value = section[AUTOSWITCH_ENABLED_KEY]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return None

    def _write_enabled(self, enabled: bool) -> bool:
        """Persist ``autoswitch.enabled`` into claude-swap's settings.json.

        Read-modify-write through claude-swap's own ``atomic_write_json`` so we
        inherit its temp-file+replace, its 0600/0700 modes, and its
        write-through-a-symlink handling. Every other key and section survives.

        A file that exists but is not a JSON object is left **untouched**: a
        hand-editing user's broken file is theirs to fix, and replacing it would
        also wipe their autoswitch policy.
        """
        backend = self._backend_or_none()
        path = self._policy_path()
        if backend is None or path is None:
            self._record_error("cannot persist autoswitch toggle: claude-swap unavailable")
            return False

        raw: Any
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            self._record_error(f"refusing to overwrite unreadable {path}", exc)
            return False
        if not isinstance(raw, dict):
            self._record_error(f"refusing to overwrite non-object {path}")
            return False

        section = raw.get(AUTOSWITCH_SECTION)
        section = dict(section) if isinstance(section, Mapping) else {}
        section[AUTOSWITCH_ENABLED_KEY] = enabled
        raw[AUTOSWITCH_SECTION] = section
        raw.setdefault("schemaVersion", backend.schema_version)
        try:
            backend.write_json(path, raw)
        except Exception as exc:
            self._record_error(f"could not write {path}", exc)
            return False
        return True

    def _mirror_local_setting(self, enabled: bool) -> None:
        """Best-effort write-back of the toggle into our own settings store."""
        setter = getattr(self._settings_obj, "set", None)
        if not callable(setter):
            return
        try:
            setter("autoswitch_enabled", enabled)
        except Exception as exc:
            self._log.debug("accounts: local settings mirror failed: %r", exc)

    # -- AccountSource: autoswitch evaluation -----------------------------

    def next_tick_in(self) -> float | None:
        """Seconds until the next autoswitch evaluation is due, or ``None``.

        Read-only: it starts no engine, calls no claude-swap code, and moves no
        schedule - it only reports the deadline :meth:`evaluate_autoswitch`
        already set. The worker uses it to size its sleep, so the engine keeps
        claude-swap's cadence (SPEC 3.5) instead of being rounded up to the
        60 s UI tick: with ``autoswitch.intervalSeconds`` at 15 s the engine
        used to be evaluated once a minute, four times later than configured.

        ``None`` means "no schedule of ours to honour" - autoswitch is off (the
        file is the source of truth, so a ``cswap config set`` is seen without
        a restart) or the toggle could not be read. ``0.0`` means due now.
        """
        try:
            if not self.autoswitch_enabled():
                return None
        except Exception as exc:  # pragma: no cover - the toggle read is guarded
            self._log.debug("accounts: autoswitch toggle unreadable: %r", exc)
            return None
        with self._lock:
            due = self._next_tick_at
        return max(0.0, due - time.monotonic())

    def evaluate_autoswitch(self) -> str | None:
        """Run one autoswitch evaluation at claude-swap's cadence.

        One ``AutoSwitchEngine.tick()`` per due call - the same tick
        ``cswap auto`` runs, sharing ``autoswitch_state.json`` and the
        ``autoswitch.*`` policy. We drive the ticks ourselves instead of
        hosting ``run_loop()`` so the widget owns exactly one scheduler and can
        stop instantly when the toggle flips.

        The next-due time comes from the engine's own delay computation, so
        cooldowns, reset-parking and the store's poll plan are honoured rather
        than re-derived here.

        Returns:
            The alias of the account switched to, or ``None`` when nothing
            happened - including when the toggle is off, when the tick is not
            yet due, and when claude-swap is unavailable.
        """
        if not self.autoswitch_enabled():
            self._stop_engine()
            return None

        now = time.monotonic()
        with self._lock:
            if now < self._next_tick_at:
                return None

        engine = self._ensure_engine()
        if engine is None:
            # Autoswitch is ON but the engine will not start (a malformed
            # `autoswitch` section, an incompatible upstream). Every OTHER
            # autoswitch failure reaches the menu bar as an alert; this one
            # reached only `last_error`, whose sole reader is a no-op once any
            # account row exists — so the toggle read ON while nothing was
            # switching, indefinitely and silently (2026-08-26).
            with self._lock:
                self._next_tick_at = time.monotonic() + _BACKEND_RETRY_S
                self._alert = (
                    ALERT_ERROR,
                    f"{ENGINE_UNAVAILABLE_PREFIX}"
                    f"{self._last_error or 'unknown reason'}",
                )
            return None

        # Construction succeeded: retract our own engine-unavailable alert
        # rather than waiting for a tick that happens to emit a qualifying
        # event. Without this the widget could sit on "⚠ autoswitch" long
        # after autoswitch recovered — the same stale-verdict bug this
        # feature exists to end, reintroduced by its own error path
        # (2026-08-26, second pre-ship gate pass). Matched on OUR prefix so an
        # `error` verdict the engine itself emitted is never swallowed.
        with self._lock:
            standing = self._alert
            if (
                standing is not None
                and standing[0] == ALERT_ERROR
                and standing[1].startswith(ENGINE_UNAVAILABLE_PREFIX)
            ):
                self._alert = None

        outcome: Any = None
        try:
            # tick() documents that it never raises; wrapped anyway, because a
            # raising tick must not kill the widget's worker thread.
            outcome = engine.tick()
        except Exception as exc:
            self._record_error("autoswitch tick failed", exc)

        delay = self._tick_delay(engine, outcome)
        with self._lock:
            self._next_tick_at = time.monotonic() + delay
        return self._drain_events()

    def _ensure_engine(self) -> Any | None:
        """The live engine, (re)built when policy changed. ``None`` on failure.

        Rebuilding on a changed ``settings.json`` mtime is what makes
        ``cswap config set autoswitch.threshold 95`` take effect without
        restarting the widget - the engine snapshots its settings at
        construction.
        """
        backend = self._backend_or_none()
        if backend is None:
            return None

        path = self._policy_path()
        mtime: float | None = None
        if path is not None:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = None

        with self._lock:
            engine = self._engine
            if engine is not None and mtime == self._engine_policy_mtime:
                return engine
        if engine is not None:
            self._stop_engine()

        try:
            policy = backend.load_policy(backend.backup_dir)
            engine = backend.engine_cls(
                backend.switcher,
                policy,
                self._on_engine_event,
                dry_run=False,
            )
        except Exception as exc:
            self._record_error("autoswitch engine failed to start", exc)
            return None

        with self._lock:
            self._engine = engine
            self._engine_policy_mtime = mtime
            # W4: cache the number the engine was just built with, so a
            # renderer can bucket accounts on the SAME threshold without
            # re-reading the policy file on every tick (SPEC 2.1).
            try:
                self._policy_threshold = float(policy.threshold)
            except (AttributeError, TypeError, ValueError):
                self._policy_threshold = None
            self._policy_models_cache = _parse_policy_models(policy)
        self._log.info(
            "accounts: autoswitch engine started (threshold %s, interval %ss)",
            getattr(policy, "threshold", "?"),
            getattr(policy, "interval_seconds", "?"),
        )
        return engine

    def _stop_engine(self) -> None:
        """Stop the engine if one is running. Idempotent, never raises."""
        with self._lock:
            engine = self._engine
            self._engine = None
            self._engine_policy_mtime = None
            # The alert is the RUNNING engine's verdict. Keeping it past the
            # engine that produced it is the same "stale state rendered as
            # live" bug this alert was added to end (2026-08-26). Anything
            # still true is re-derived: all-exhausted re-emits every blocked
            # tick, a quarantine is re-read from the state file. The external
            # switch verdict lives in its own slot and is untouched here: it is
            # a fact about the world, not a verdict of the engine being stopped.
            self._alert = None
        if engine is None:
            return
        try:
            engine.stop()
        except Exception as exc:  # pragma: no cover - defensive
            self._log.debug("accounts: engine stop failed: %r", exc)
        backend = self._backend
        if backend is not None:
            try:
                # Drop the engine's pinned poll-planning inputs so cadence goes
                # back to the settings file, as upstream expects when an engine
                # screen closes.
                backend.switcher.clear_poll_policy_inputs()
            except Exception as exc:  # pragma: no cover - upstream refactor
                self._log.debug("accounts: clearing poll inputs failed: %r", exc)
        self._log.info("accounts: autoswitch engine stopped")

    def _tick_delay(self, engine: Any, outcome: Any) -> float:
        """Seconds until the next evaluation, per claude-swap's own policy.

        Prefers the engine's ``_next_delay`` (cooldown/reset-aware, jittered,
        and it shortens to the usage store's next-poll time) and falls back to
        the plain configured interval. Clamped so a surprising value can
        neither hot-loop nor park us for a day.
        """
        delay: float | None = None
        if outcome is not None:
            next_delay = getattr(engine, "_next_delay", None)
            if callable(next_delay):
                try:
                    delay = float(next_delay(outcome))
                except Exception as exc:  # pragma: no cover - upstream refactor
                    self._log.debug("accounts: _next_delay unavailable: %r", exc)
                    delay = None
        if delay is None:
            try:
                delay = float(engine.settings.interval_seconds)
            except Exception:  # pragma: no cover - defensive
                delay = 60.0
        return min(max(delay, _TICK_DELAY_FLOOR_S), _TICK_DELAY_CEILING_S)

    # -- engine events ------------------------------------------------------

    def _on_engine_event(self, event: Any) -> None:
        """Engine-thread callback. Must never raise (upstream does not catch).

        Queues the event for the caller's thread; ``tick()`` runs on our worker
        so the queue is drained in the same call, but keeping the handoff
        explicit means a future hosted loop needs no change here.
        """
        try:
            with self._event_lock:
                self._events.append(event)
        except Exception:  # pragma: no cover - defensive
            pass

    def _drain_events(self) -> str | None:
        """Drain queued events; return the alias of the last real switch.

        Non-switch events (quarantine, all-exhausted, config warnings, errors)
        are logged rather than dropped: a silently inert autoswitch config is
        exactly the failure the user would never notice.
        """
        with self._event_lock:
            events, self._events = self._events, []
        if not events:
            return None

        alias: str | None = None
        lines: list[str] = []
        expect: str | None = None
        took_wheel = False
        # ``False`` = no verdict-bearing event in this batch (leave _alert as
        # is); ``None`` = the engine moved on (clear); a tuple = a new alert.
        alert: tuple[str, str] | None | bool = False
        for event in events:
            kind = str(getattr(event, "kind", "") or "event")
            try:
                line = str(event.human())
            except Exception:  # pragma: no cover - upstream refactor
                line = kind
            # Journal only what an operator would call an event: a plain
            # "no switch: below-threshold" arrives every idle tick and would
            # evict the one external line the block exists for within ~20 min
            # (review 2026-09-01). Still DEBUG-logged below.
            quiet = kind in _QUIET_EVENT_KINDS or (
                kind == "no-switch"
                and str(getattr(event, "reason", "") or "") not in _NO_TARGET_REASONS
            )
            if not quiet:
                lines.append(f"{_stamp()} {line}")
            if kind == "switch" and not bool(getattr(event, "dry_run", False)):
                alias = self._alias_for_ref(getattr(event, "to_ref", None)) or alias
                # Claim the landing slot so the refresh below does not read our
                # own engine's switch as somebody else's.
                expect = self._ref_number(getattr(event, "to_ref", None)) or _ANY_SLOT
                alert = None
                took_wheel = True
                self._last_no_target_key = None
                self._log.info("accounts: %s", line)
            elif kind == ALERT_ALL_EXHAUSTED:
                alert = (kind, line)
                # The engine re-emits this every blocked tick (~10 min, capped
                # 600s) for as long as the fleet is dry — potentially days.
                # WARN once per episode (keyed on the message, which carries
                # earliest-reset) and once per hour thereafter; every
                # occurrence still reaches the menu via _event_log below.
                now = time.monotonic()
                # Key on the reset DATE, never the raw line: the line embeds a
                # sub-second-jittering timestamp that also flips the whole
                # second (…T20:59:59.700962Z vs …T21:00:00.132963Z for one
                # episode), so a raw-line comparison never matches and this
                # WARNed every tick anyway — the exact noise it was added to
                # remove (observed live 2026-08-26 12:31/12:41/12:45/12:55).
                key = _episode_key(line)
                if key != self._last_exhausted_key or (
                    now - self._last_exhausted_warn_at > 3600.0
                ):
                    self._last_exhausted_key = key
                    self._last_exhausted_warn_at = now
                    self._log.warning("accounts: autoswitch %s: %s", kind, line)
                else:
                    self._log.debug("accounts: autoswitch %s: %s", kind, line)
            elif kind in (ALERT_ACCOUNT_QUARANTINED, ALERT_ERROR):
                alert = (kind, line)
                self._log.warning("accounts: autoswitch %s: %s", kind, line)
            elif kind == "config-warning":
                self._log.warning("accounts: autoswitch %s: %s", kind, line)
            elif kind == "no-switch" and (
                str(getattr(event, "reason", "") or "") in _NO_TARGET_REASONS
            ):
                # "I wanted to move and there was nowhere to go" is a standing
                # state, not a shrug: before 2026-09-01 it took the same branch
                # as every other no-switch and therefore CLEARED the alert the
                # operator needed, at DEBUG level (invisible in the default log).
                alert = (ALERT_NO_TARGET, line)
                key = _episode_key(line)
                if key != self._last_no_target_key:
                    self._last_no_target_key = key
                    self._log.warning("accounts: autoswitch %s: %s", kind, line)
                else:
                    self._log.debug("accounts: autoswitch %s: %s", kind, line)
            elif kind in ("no-switch", "account-unquarantined"):
                # The engine evaluated the fleet and found nothing to escalate:
                # whatever verdict was standing (all-exhausted, quarantine) is
                # over. A poll/sleep event carries no verdict and changes nothing.
                alert = None
                self._last_no_target_key = None
                self._log.debug("accounts: autoswitch %s: %s", kind, line)
            else:
                self._log.debug("accounts: autoswitch %s: %s", kind, line)

        with self._lock:
            self._event_log.extend(lines)
            if expect is not None:
                self._expect_active = expect
            if took_wheel:
                # Our own engine switched: "another actor is driving" is over.
                self._external_alert = None
            if alert is not False:
                self._alert = alert
        if alias is not None:
            # The active slot just changed under us.
            self.refresh(force=True)
        return alias

    @staticmethod
    def _ref_number(ref: Any) -> str | None:
        """Slot number of a ``{"number": .., "email": ..}`` event ref, as str."""
        if not isinstance(ref, Mapping):
            return None
        number = ref.get("number")
        if number is None or isinstance(number, bool):
            return None
        text = str(number).strip()
        return text or None

    def _alias_for_ref(self, ref: Any) -> str | None:
        """Alias for a ``{"number": .., "email": ..}`` event ref."""
        if not isinstance(ref, Mapping):
            return None
        slot = _safe_int(ref.get("number"), default=-1)
        with self._lock:
            alias = self._alias_by_slot.get(slot)
        if alias:
            return alias
        email = ref.get("email")
        if isinstance(email, str) and email:
            return _alias_from_email(email, slot)
        return str(slot) if slot >= 0 else None

    # -- lifecycle ----------------------------------------------------------

    def shutdown(self) -> None:
        """Stop the autoswitch engine. Call from the app's quit handler."""
        self._stop_engine()


def _safe_roll(
    roll: Callable[[Any, float], Any], window: Any, now: float
) -> Any:
    """Apply the weekly roll-forward, falling back to the raw window."""
    if window is None:
        return None
    try:
        rolled = roll(window, now)
    except Exception:  # pragma: no cover - upstream refactor
        return window
    return rolled if isinstance(rolled, Mapping) else window


def _usage_age(entry: Any, now: float) -> float | None:
    """Age in seconds of the measurement behind a row, or ``None``.

    Computed from the store's ``fetched_at`` at render time so it keeps
    counting up between snapshot passes; ``age_s`` (frozen at the snapshot's
    take) is the fallback when no fetch stamp is recorded.
    """
    if entry is None:
        return None
    fetched = getattr(entry, "fetched_at", None)
    if isinstance(fetched, (int, float)) and not isinstance(fetched, bool):
        return max(0.0, now - float(fetched))
    age = getattr(entry, "age_s", None)
    if isinstance(age, (int, float)) and not isinstance(age, bool):
        return max(0.0, float(age))
    return None


def create_account_source(
    *,
    settings: Any = None,
    switcher: Any | None = None,
    logger: logging.Logger | None = None,
) -> SwapAccountSource:
    """Build the app's :class:`~cc_usage_widget.contracts.AccountSource`.

    A named factory so ``__main__``/``app`` do not have to know the concrete
    class. Construction does no I/O: ``claude_swap`` is imported on first use,
    from the background thread.
    """
    return SwapAccountSource(settings=settings, switcher=switcher, logger=logger)


def _pace_ahead(
    entry: Any, seven_day: Any, scoped_rolled: "list[tuple[str, Any]]"
) -> tuple[tuple[str, bool], ...]:
    """Pace verdicts for the weekly windows, via ``claude_swap.pace``.

    We do NOT compute burn rates ourselves — upstream already decides what
    "ahead of pace" means (it suppresses the verdict for the first 24h after a
    reset, and applies a 15-point threshold), and two implementations would
    drift apart and disagree with ``cswap watch``.

    Returns only the windows with a real verdict; anything not computable is
    omitted rather than defaulted, so the UI renders no note instead of a
    misleading "on pace". Never raises: pace is decoration, and an upstream
    change here must not cost us the account rows.
    """
    fetched_at = getattr(entry, "fetched_at", None)
    if not isinstance(fetched_at, (int, float)):
        return ()
    try:
        from claude_swap import pace as _pace  # noqa: PLC0415 - lazy, worker thread
    except Exception:
        return ()

    out: list[tuple[str, bool]] = []
    for key, window in (("seven_day", seven_day), *scoped_rolled):
        if not isinstance(window, Mapping):
            continue
        try:
            result = _pace.compute_pace(window, fetched_at=float(fetched_at))
        except Exception:
            result = None
        if result is not None:
            out.append((key, bool(getattr(result, "ahead", False))))
    return tuple(out)
