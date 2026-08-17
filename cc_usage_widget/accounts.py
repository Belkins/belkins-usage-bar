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

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final

from .contracts import AccountRow, Pct, normalize_settings

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


def _on_main_thread() -> bool:
    """Whether we are on the AppKit main thread.

    ``rumps`` runs its event loop on the interpreter's main thread, so this is
    the thread a blocking snapshot pass must never stall (SPEC 2.3).
    """
    return threading.current_thread() is threading.main_thread()


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


def _identity_roll(window: Any, _now: float) -> Any:
    """Fallback for the weekly roll-forward: leave the window untouched."""
    return window


def _import_backend(switcher: Any | None) -> _Backend:
    """Import ``claude_swap`` and build the backend handle.

    Raises whatever the import or the switcher's construction raises; the only
    caller wraps this and records the failure.
    """
    from claude_swap.autoswitch import AutoSwitchEngine
    from claude_swap.oauth import fresh_reset_strings
    from claude_swap.settings import (
        SETTINGS_SCHEMA_VERSION,
        atomic_write_json,
        load_settings,
        settings_path,
    )
    from claude_swap.snapshot_source import SnapshotSource
    from claude_swap.switcher import SENTINEL_NOTES, ClaudeAccountSwitcher

    # Reuse upstream's weekly roll-forward: once a weekly window's ``resets_at``
    # has passed we know it rolled over, so the stored pct belongs to a window
    # that no longer exists and must not render as "Fable 100% (!)" for days.
    # It is upstream's own display rule (``claude_swap.menubar``, import-safe
    # without rumps); if it ever disappears we show the raw window instead.
    try:
        from claude_swap.menubar import _rolled_weekly_window as roll_weekly
    except Exception:  # pragma: no cover - upstream refactor
        roll_weekly = _identity_roll

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
        self._switchable: frozenset[int] = frozenset()
        self._alias_by_slot: dict[int, str] = {}

        self._engine: Any | None = None
        self._engine_policy_mtime: float | None = None
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
            self._snapshot = snapshot
            self._snapshot_at = time.monotonic()
            self._last_error = None

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
            note = self._sentinel_note(account, backend)
            if note:
                sentinels[row.slot] = note
            if bool(getattr(account, "switchable", True)):
                switchable.add(row.slot)

        rows.sort(key=lambda row: row.slot)
        with self._lock:
            self._sentinels = sentinels
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
        seven_day = _safe_roll(roll, _window(last_good, "seven_day"), now)

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
                clock = self._reset_clock(window, backend)
                if clock:
                    scoped_resets.append((name, clock))

        return AccountRow(
            slot=slot,
            alias=alias,
            email=email,
            is_active=is_active,
            five_hour_pct=_pct(five_hour),
            seven_day_pct=_pct(seven_day),
            scoped_windows=tuple(scoped_windows),
            five_hour_resets_at=self._reset_clock(five_hour, backend),
            seven_day_resets_at=self._reset_clock(seven_day, backend),
            scoped_resets_at=tuple(scoped_resets),
            usage_age_seconds=_usage_age(entry, now),
            pace_ahead=_pace_ahead(entry, seven_day, scoped_rolled),
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

    def _sentinel_note(self, account: Any, backend: _Backend | None) -> str | None:
        """Human note for a derived usage state (``api key``, ``token
        expired``, ``re-login needed``, ...) or ``None`` for a real measurement.

        :class:`AccountRow` has nowhere to carry this, so it is exposed
        alongside the rows via :meth:`sentinel_for` for the menu to annotate.
        """
        sentinel = getattr(getattr(account, "usage", None), "sentinel", None)
        if not isinstance(sentinel, str) or not sentinel:
            return None
        notes = backend.sentinel_notes if backend is not None else {}
        try:
            return str(notes.get(sentinel, sentinel))
        except Exception:  # pragma: no cover - defensive
            return sentinel

    # -- extras beyond the protocol ---------------------------------------

    def sentinel_for(self, slot: int) -> str | None:
        """Derived-state note for one slot, from the last built rows."""
        with self._lock:
            return self._sentinels.get(int(slot))

    def sentinels(self) -> dict[int, str]:
        """Every slot with a derived usage state, as ``{slot: note}``."""
        with self._lock:
            return dict(self._sentinels)

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

        ok = self._attempt_switch(backend, identifier)
        if not ok:
            fallback = self._slot_identifier_for(identifier)
            if fallback is not None and fallback != identifier:
                ok = self._attempt_switch(backend, fallback)
        if ok:
            # Reflect the new active row now rather than up to a tick later.
            self.refresh(force=True)
        return ok

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
        if not ok:
            # From here on this process trusts its own flag over the file it
            # could not write, so the toggle cannot silently revert.
            with self._lock:
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
            with self._lock:
                self._next_tick_at = time.monotonic() + _BACKEND_RETRY_S
            return None

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
        for event in events:
            kind = str(getattr(event, "kind", "") or "event")
            try:
                line = str(event.human())
            except Exception:  # pragma: no cover - upstream refactor
                line = kind
            lines.append(line)
            if kind == "switch" and not bool(getattr(event, "dry_run", False)):
                alias = self._alias_for_ref(getattr(event, "to_ref", None)) or alias
                self._log.info("accounts: %s", line)
            elif kind in ("account-quarantined", "all-exhausted", "config-warning", "error"):
                self._log.warning("accounts: autoswitch %s: %s", kind, line)
            else:
                self._log.debug("accounts: autoswitch %s: %s", kind, line)

        with self._lock:
            self._event_log.extend(lines)
        if alias is not None:
            # The active slot just changed under us.
            self.refresh(force=True)
        return alias

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
