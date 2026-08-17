"""The menu-bar surface: title rendering, menu construction, and cadence.

This module owns everything the user sees (SPEC 4) and nothing else. It reads
:mod:`cc_usage_widget.contracts` shapes produced by the four seams
(:class:`~cc_usage_widget.contracts.AccountSource`,
:class:`~cc_usage_widget.contracts.TranscriptIndexer`,
:class:`~cc_usage_widget.contracts.RollupStore`,
:class:`~cc_usage_widget.contracts.PricingTable`) and never parses a transcript,
never touches ``claude_swap``, and never reads a file itself.

Threading (SPEC 2.3) - the whole design in five sentences
---------------------------------------------------------

1. ``rumps.Timer`` callbacks run on the **AppKit main thread**, so the only
   timer this module installs is :meth:`CCUsageWidgetApp._on_sync_tick`, a
   ~1 s tick that does one lock-guarded attribute read and, when a new snapshot
   has arrived, rebuilds ``NSMenu``. No I/O, no JSON, no ``claude_swap`` call
   ever runs on that thread.
2. All work - account reads, transcript scans, rollup merges, cost math,
   settings writes - happens on the single :class:`BackgroundWorker` thread,
   which owns the two independent cadences (``ui_interval_seconds`` and
   ``cost_interval_seconds``) and waits on a :class:`queue.Queue` so a menu
   click wakes it immediately instead of polling.
3. The two threads exchange exactly one thing: an immutable
   :class:`UiSnapshot`, published under a lock. Every value inside it is a
   frozen dataclass from ``contracts``, so the main thread cannot observe a
   half-built state.
4. A background *anything* can never kill the loop or the app: every job body is
   wrapped and the whole loop body is wrapped again in ``except BaseException``
   (``except Exception`` was not enough - a helper calling ``sys.exit()`` raises
   ``SystemExit``, which killed the thread outright), the failure is stringified
   into :attr:`UiSnapshot.accounts_error` / :attr:`UiSnapshot.cost_error`, and it
   surfaces as a ``!`` menu line while the loop keeps its schedule. Should the
   thread die anyway, :meth:`CCUsageWidgetApp._supervise_worker` notices on the
   next repaint tick, says so in the menu, and restarts it - a silently frozen
   widget is the one failure mode with no tell.
5. The first index is chunked: while
   :attr:`~cc_usage_widget.contracts.IndexProgress.complete` is False the cost
   job passes a short ``deadline`` to
   :meth:`~cc_usage_widget.contracts.TranscriptIndexer.scan_once` and
   re-schedules itself in a fraction of a second, so the menu stays live and
   cost fills in behind it (SPEC 3.2 "first run").

Honesty rules this module is responsible for (SPEC 4.3)
-------------------------------------------------------

* Every cost figure sits under a header carrying
  :data:`~cc_usage_widget.contracts.NOTIONAL_LABEL` verbatim.
* While the first index is incomplete the three window rows read
  ``indexing...`` plus an ``n/N`` progress line - never a partial dollar total
  that would look real. The title's cost slot degrades to ``$.../d`` for the
  same reason.
* Reset strings are printed exactly as
  :class:`~cc_usage_widget.contracts.AccountRow` carries them; nothing here
  recomputes a reset time.
* A stale usage read (SPEC's ``usageAgeSeconds``) prints its age on the row
  rather than implying live data.
"""

from __future__ import annotations

import inspect
import json
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

import rumps

from . import render
from .contracts import (
    ATTENTION_PCT,
    CODEX_SCAN_STATE_PATH,
    CODEX_SESSIONS_DIR,
    CODEX_WINDOW_MINUTES_WEEKLY,
    NOTIONAL_LABEL,
    PROJECTS_DIR,
    ROLLUPS_PATH,
    SCAN_STATE_PATH,
    SETTINGS_BOUNDS,
    SETTINGS_DEFAULTS,
    SETTINGS_PATH,
    TITLE_ICON,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    AccountRow,
    AccountSource,
    CostBreakdown,
    IndexProgress,
    PricingTable,
    RollupStore,
    TranscriptIndexer,
    TranscriptSource,
    Vendor,
    format_pct,
    format_tokens,
    format_usd,
    local_day_key,
    normalize_settings,
    vendor_label,
)

try:  # single-sourced wording; a broken accounts.py must not stop app.py importing
    from .accounts import ACCOUNTS_UNAVAILABLE
except Exception:  # pragma: no cover - accounts.py is optional at import time
    ACCOUNTS_UNAVAILABLE = "accounts unavailable"

__all__ = [
    "APP_NAME",
    "UiSnapshot",
    "BackgroundWorker",
    "CCUsageWidgetApp",
    "UsageWidgetApp",
    "App",
    "build_app",
    "main",
]

APP_NAME = "cc-usage-widget"
"""``rumps.App`` name. Also names the Application Support folder, so keep it
stable across releases."""

# --- cadence constants (the two user-visible intervals live in settings.json) -

SYNC_INTERVAL_SECONDS = 1.0
"""Main-thread repaint tick. Does a lock-guarded flag read and returns; the
menu is only rebuilt when the worker published something new."""

INDEX_CHUNK_SECONDS = 0.75
"""``scan_once`` deadline while the first index is still incomplete. Short
enough that the worker yields often, long enough to make real progress."""

INDEX_CHUNK_PAUSE_SECONDS = 0.25
"""Gap between first-run index chunks - the "yields between files" of SPEC 3.2
seen from the scheduler's side."""

STEADY_SCAN_DEADLINE_SECONDS = 30.0
"""Safety net for a steady-state scan, whose budget is < 30 ms (SPEC 2.1). It
exists only so a pathological corpus cannot wedge the worker forever."""

ROLLUP_SAVE_MIN_INTERVAL_SECONDS = 5.0
"""Throttle on ``RollupStore.save()`` during a chunked first index, so a few
thousand files do not mean a few thousand atomic writes."""

MAX_ERROR_CHARS = 140
"""Menu lines are one line. Longer messages are truncated (full traceback goes
to stderr)."""

_ZWSP = "\u200b"
"""Zero-width space. ``rumps`` keys menu items by title and *silently drops* an
item whose title already exists, so duplicate labels get invisible padding
appended rather than disappearing (see :func:`_dedupe_titles`)."""

_LOOKBACK_CHOICES = (7, 14, 30, 60, 90)
_UI_INTERVAL_CHOICES = (30, 60, 120, 300)
_COST_INTERVAL_CHOICES = (60, 300, 600, 1800)

_TITLE_TOGGLES = (
    ("title_show_icon", "Icon"),
    ("title_show_alias", "Account alias"),
    ("title_show_five_hour_pct", "5h percentage"),
    ("title_show_scoped_pct", "Weekly scoped percentage"),
    ("title_show_cost", "Today's cost"),
    ("title_show_codex_pct", "Codex weekly percentage"),
)

FIVE_HOUR_WINDOW_MINUTES = 300
"""Width of Claude's rolling 5-hour window, in minutes.

Only used to *name* a pseudo-account window through
:func:`~cc_usage_widget.render.window_minutes_label`; nothing schedules on it.
"""

# Worker commands.
_CMD_REFRESH = "refresh"
_CMD_SET_AUTOSWITCH = "set_autoswitch"
_CMD_SET_SETTING = "set_setting"
_CMD_SWITCH_TO = "switch_to"
_CMD_REBUILD_INDEX = "rebuild_index"
_CMD_WIRE_SOURCES = "wire_sources"


def _log(message: str) -> None:
    """Timestamped stderr line. The widget runs in the foreground for v1, so
    stderr is the log."""
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] cc-usage-widget: {message}\n")
    sys.stderr.flush()


_SEEN_FAILURES: dict[tuple[str, str, str], int] = {}
"""How many times each failure signature has been described, keyed by
``(scope, exception type, message)``."""

_SEEN_FAILURES_LOCK = threading.Lock()


def _describe(exc: BaseException, scope: str = "") -> str:
    """One-line description of *exc* for a menu row; full traceback to stderr.

    The traceback is written **once per distinct failure**, and the one-line
    recurrence notice only at 2, 4, 8, 16... A persistent cause - ``claude_swap``
    raising on every 60 s refresh, an unreadable transcript root on every 300 s
    scan - would otherwise emit ~1,700 identical multi-line tracebacks a day,
    which buries every other line in the foreground and is unbounded, unrotated
    file growth under the LaunchAgent this widget is headed for (SPEC 5), from a
    widget whose whole premise is a <0.3% idle footprint.

    *scope* names the job, so :func:`_forget_failures` can clear one job's
    signatures when *that* job next succeeds and a failure recurring after a
    recovery is loud again - without one job's success un-muting another's
    still-broken cause.
    """
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    key = (scope, type(exc).__name__, str(exc))
    with _SEEN_FAILURES_LOCK:
        count = _SEEN_FAILURES.get(key, 0) + 1
        _SEEN_FAILURES[key] = count
    if count == 1:
        _log("".join(traceback.format_exception(exc)).rstrip())
    elif count & (count - 1) == 0:  # 2, 4, 8, 16, ...
        _log(f"{text}  (repeated {count}x; traceback logged once)")
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def _forget_failures(scope: str | None = None) -> None:
    """Forget remembered signatures for *scope* (all of them when ``None``)."""
    with _SEEN_FAILURES_LOCK:
        if scope is None:
            _SEEN_FAILURES.clear()
            return
        for key in [key for key in _SEEN_FAILURES if key[0] == scope]:
            del _SEEN_FAILURES[key]


# ---------------------------------------------------------------------------
# The published snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class UiSnapshot:
    """Everything the main thread needs to paint, and nothing it must compute.

    Immutable and swapped wholesale, which is what makes the hand-off to the
    AppKit thread safe (SPEC 2.3). ``eq=False`` keeps identity semantics - the
    snapshot is a message, not a value to compare.
    """

    accounts: tuple[AccountRow, ...] = ()
    quota_rows: tuple[AccountRow, ...] = ()
    """Read-only pseudo-accounts from the non-Claude sources (SPEC-CODEX 4).

    Kept in a **separate** field from ``accounts`` rather than appended to it,
    which is what structurally guarantees requirement "no click-to-switch":
    every switch path in this module - the account block's callback, the
    ``Switch account`` submenu, ``_CMD_SWITCH_TO`` - reads ``accounts``, so a
    pseudo-account cannot reach one by accident. Empty on a machine with no
    Codex corpus, which is what renders no Codex section at all (SPEC-CODEX
    5.5).
    """
    active: AccountRow | None = None
    autoswitch_enabled: bool = False
    cost: CostBreakdown | None = None
    progress: IndexProgress = IndexProgress()
    settings: dict[str, Any] = field(default_factory=lambda: dict(SETTINGS_DEFAULTS))
    accounts_error: str | None = None
    cost_error: str | None = None
    wiring_errors: tuple[str, ...] = ()
    scan_note: str | None = None
    accounts_at: float = 0.0
    cost_at: float = 0.0


# ---------------------------------------------------------------------------
# Small formatters (the menu's own; shared numeric formats come from contracts)
# ---------------------------------------------------------------------------


def _email_local(email: str, limit: int = 14) -> str:
    """``"jane@example.com"`` -> ``"jane"``, truncated."""
    local = (email or "").split("@", 1)[0]
    return local if len(local) <= limit else local[: limit - 1] + "…"


def _display_name(row: AccountRow) -> str:
    """Alias if the account has one, else the email's local part."""
    return row.alias or _email_local(row.email) or f"slot {row.slot}"


def _attention(pct: float | None) -> str:
    """``" (!)"`` at or above :data:`ATTENTION_PCT`, else ``""`` (SPEC 4.2)."""
    return " (!)" if pct is not None and pct >= ATTENTION_PCT else ""


def _title_pct(pct: float | None) -> str:
    """Compact percentage for the menu-bar title: ``17%``, ``100%(!)``.

    Uses :func:`~cc_usage_widget.contracts.format_pct` for the number so the
    title and the menu can never round differently, and the same
    :data:`ATTENTION_PCT` threshold so they cannot disagree about ``(!)``.
    """
    return f"{format_pct(pct)}{_attention(pct).strip()}"


def _title_usd(value: float) -> str:
    """Title-width dollars: ``$12/d``, ``$0.40/d`` (SPEC 4.1).

    Deliberately *not*
    :func:`~cc_usage_widget.contracts.format_usd` - that renders ``$12.40``,
    which is right for a menu row and too wide for the menu bar. Sub-$10 days
    keep one decimal so a quiet morning does not read as ``$0/d``.
    """
    if value >= 10:
        return f"${value:,.0f}/d"
    return f"${value:.2f}/d" if value < 1 else f"${value:.1f}/d"


def _age_label(seconds: float | None) -> str:
    """``95`` -> ``"1m"``. Coarse on purpose: this is a staleness hint."""
    if seconds is None:
        return "?"
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _duration_label(seconds: int) -> str:
    """``300`` -> ``"5m"``, ``60`` -> ``"1m"``, ``45`` -> ``"45s"``."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _primary_reset(row: AccountRow) -> str | None:
    """The reset string to print on the row, verbatim (SPEC 4.3).

    The 5-hour window is the one a user acts on, so it wins; the weekly and
    scoped strings are the fallbacks when the API did not report it. Nothing
    here parses or recomputes the value.
    """
    if row.five_hour_resets_at:
        return row.five_hour_resets_at
    if row.seven_day_resets_at:
        return row.seven_day_resets_at
    for _name, reset in row.scoped_resets_at:
        if reset:
            return reset
    return None


def _reset_note(row: AccountRow, window: str) -> str:
    """The reset string for one window, verbatim, prefixed ``resets``.

    *window* is ``"five_hour"``, ``"seven_day"``, or a scoped window's reported
    name (e.g. ``"Fable"``). Empty string when the API reported no reset for it —
    the row simply omits the note rather than inventing a time (SPEC 4.3).
    """
    if window == "five_hour":
        raw = row.five_hour_resets_at
    elif window == "seven_day":
        raw = row.seven_day_resets_at
    else:
        raw = next((r for name, r in row.scoped_resets_at if name == window), None)
    return f"resets {raw}" if raw else ""


def _account_row_label(row: AccountRow, name_width: int = 10) -> str:
    """One account line (SPEC 4.2).

    ``2 vlad       5h  19% - 7d 33% - Fable  65%  resets 14:20``

    Every scoped window the API reported is rendered, in order; nothing
    hardcodes that there is exactly one (contract note on ``scoped_windows``).
    """
    windows = [
        f"5h {format_pct(row.five_hour_pct):>4}{_attention(row.five_hour_pct)}",
        f"7d {format_pct(row.seven_day_pct):>4}{_attention(row.seven_day_pct)}",
    ]
    for name, pct in row.scoped_windows:
        windows.append(f"{name} {format_pct(pct):>4}{_attention(pct)}")
    label = f"{row.slot} {_display_name(row):<{name_width}} " + "  · ".join(windows)
    reset = _primary_reset(row)
    if reset:
        label = f"{label}  resets {reset}"
    if row.usage_is_stale:
        label = f"{label}  (usage {_age_label(row.usage_age_seconds)} old)"
    return label


def _cost_row_label(label: str, value: str, extra: str = "") -> str:
    """``"  Today                     $12.40"`` (SPEC 4.2)."""
    text = f"  {label:<11}{value:>11}"
    return f"{text}   {extra}" if extra else text


def _model_row_label(display_name: str, tokens: int, usd: float) -> str:
    """``"  Fable 5      41.2M tok    $8.90"`` (SPEC 4.2)."""
    return f"  {display_name:<13}{format_tokens(tokens) + ' tok':>11}{format_usd(usd):>10}"


MODEL_ROW_WIDTH = 36
"""Rendered width of :func:`_model_row_label` (2 + 13 + 11 + 10).

Named so the vendor group heading can right-align its subtotal on the same
edge as the model rows underneath it, instead of two constants drifting apart.
"""


def _vendor_group_label(name: str, usd: float) -> str:
    """``"  ── Codex ──────────────────     $3.30"`` (SPEC-CODEX 5.2).

    Replaces the single ``── by model ──`` rule when more than one vendor used
    tokens today. It costs one line per vendor and makes every model row below
    it attributable at a glance without widening the (already dense) rows
    themselves — which is why the vendor is a *heading* and not a per-row tag.
    """
    left = f"  ── {name} ".ljust(MODEL_ROW_WIDTH - 10, "─")
    return f"{left}{format_usd(usd):>10}"


def _quota_windows(row: AccountRow) -> list[tuple[str, float | None, str]]:
    """``(label, pct, reset note)`` for every window a quota row reports.

    The label is **derived from the window's width**
    (:func:`~cc_usage_widget.render.window_minutes_label`), never hardcoded:
    Codex's ``rate_limits.primary`` is a 10,080-minute window and therefore
    reads ``weekly``, and a future window of some other width would name itself
    correctly instead of being mislabelled (SPEC-CODEX 1).

    ``window_minutes`` is read off the row when a source chose to carry it, so
    a richer row wins; otherwise the width is the one the contract fixes for
    the slot the figure arrived in — ``seven_day_pct`` is documented as the
    landing slot for the weekly primary window, and a source is forbidden from
    putting any other width there.

    A window the source did not report is **omitted**, not rendered as a bar at
    ``--``: a Codex block has no 5-hour window at all, and a placeholder row
    for it would imply a quota that does not exist.
    """
    minutes = getattr(row, "window_minutes", None)
    windows: list[tuple[str, float | None, str]] = []
    if row.five_hour_pct is not None:
        label = render.window_minutes_label(FIVE_HOUR_WINDOW_MINUTES) or "5h"
        windows.append((label, row.five_hour_pct, _reset_note(row, "five_hour")))
    if row.seven_day_pct is not None:
        width = minutes if minutes else CODEX_WINDOW_MINUTES_WEEKLY
        label = render.window_minutes_label(width) or "7d"
        windows.append((label, row.seven_day_pct, _reset_note(row, "seven_day")))
    for name, pct in row.scoped_windows:
        if pct is not None:
            windows.append((name, pct, _reset_note(row, name)))
    return windows


def _quota_row_label(row: AccountRow) -> str:
    """One-line plain fallback for a quota block.

    ``Codex (pro)   weekly  12%  resets Aug 21 14:00``

    This is what VoiceOver reads and what shows if attributed rendering is
    unavailable, so it must carry every figure the bar block does.
    """
    head = row.alias or vendor_label(row.vendor)
    if row.plan_type:
        head = f"{head} ({row.plan_type})"
    parts = [
        f"{label} {format_pct(pct):>4}{_attention(pct)}" for label, pct, _note in _quota_windows(row)
    ]
    text = f"{head}   " + "  · ".join(parts) if parts else head
    reset = _primary_reset(row)
    if reset:
        text = f"{text}  resets {reset}"
    return text


def _window_label_width(rows: Sequence[AccountRow], quota_rows: Sequence[AccountRow]) -> int:
    """Widest window label across **every** block in the menu.

    One vertical edge for the whole surface: without this the Claude bars would
    align with each other and the Codex bar with itself, two columns apart.
    Claude-only menus are unaffected — ``weekly`` only enters the maximum when
    a Codex block is actually present.
    """
    widths = [len(name) for row in rows for name, _pct in row.scoped_windows]
    widths += [len(label) for row in quota_rows for label, _pct, _note in _quota_windows(row)]
    widths.append(2)  # "5h" / "7d"
    return max(widths)


def _progress_label(progress: IndexProgress) -> str:
    """``"indexing... 1,204/~3,200"``, or a bare ``indexing...`` before the
    file count is known. Empty once the index is complete."""
    if progress.complete:
        return ""
    return progress.label() if progress.files_total > 0 else "indexing…"


# ---------------------------------------------------------------------------
# The background worker - every byte of I/O in this module happens here
# ---------------------------------------------------------------------------


class BackgroundWorker:
    """One daemon thread owning both cadences and all I/O (SPEC 2.3, 3.5).

    It waits on a command queue rather than sleeping in a loop, so a click
    ("Refresh now", a toggle) is serviced immediately while an idle widget
    wakes only twice a minute-ish. Nothing here imports ``rumps`` or touches a
    menu; the only outbound edge is *publish*, which hands the main thread an
    immutable :class:`UiSnapshot`.

    **Several vendors, one loop** (SPEC-CODEX 4). ``indexer`` remains the
    Claude scanner it always was; ``sources`` is a list of
    :class:`~cc_usage_widget.contracts.TranscriptSource` (today: Codex), and
    every cost-side operation - scan, lookback, offset commit, reset - iterates
    over both together. Nothing in this class knows what a vendor *is*; adding
    a third means adding a source, which is the point of the protocol.
    """

    def __init__(
        self,
        *,
        publish: Callable[[UiSnapshot], None],
        snapshot: UiSnapshot,
        accounts: AccountSource | None = None,
        indexer: TranscriptIndexer | None = None,
        rollups: RollupStore | None = None,
        pricing: PricingTable | None = None,
        persist_settings: Callable[[dict[str, Any]], None] | None = None,
        sources: Sequence[TranscriptSource] | None = None,
        source_factory: Callable[[dict[str, Any], Any], Sequence[Any]] | None = None,
    ) -> None:
        self._publish_cb = publish
        self._snapshot = snapshot
        self._accounts = accounts
        self._indexer = indexer
        self._rollups = rollups
        self._pricing = pricing
        self._persist_settings = persist_settings
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rollups_loaded = False
        self._reconciled: set[Vendor] = set()
        """Vendors already checked by ``_reconcile_lost_scan_state``. A source
        can join the tick long after the store was loaded - the Codex toggle
        flipped on, or a corpus that only became ``available()`` later - and its
        first merge is just as capable of doubling as the first one ever was."""
        self._last_rollup_save = 0.0
        self._scan_cursor = 0
        """Which corpus gets walked on the next steady tick. With more than one
        vendor the walks are alternated so no single tick pays for both trees
        (SPEC 2.1's 30 ms is a per-tick budget); the tick fires proportionally
        more often, so each vendor's own cadence is unchanged."""
        self._force_all_sources = False
        """Set when a command forces a cost job (Refresh now, a cost-touching
        settings change). Such a job answers for every vendor at once - the
        rotation is a background-cadence optimisation, not something a user's
        explicit click should be subject to."""
        self._scan_errors: dict[Vendor, tuple[str, ...]] = {}
        """Last-known unreadable-file errors per vendor. Held across ticks
        because a steady tick reads only one corpus: the other vendor's error
        is still true, it just was not re-observed this second."""
        self._lookback_applied: int | None = None
        # Extra vendors. `sources=None` means "nobody has decided yet", which
        # is what lets `_CMD_WIRE_SOURCES` autowire Codex on *this* thread; an
        # explicit sequence (including an empty one) is a decision and is never
        # second-guessed, so a test or a caller that wants Claude only gets
        # exactly that and no ~/.codex is ever touched.
        self._sources: tuple[Any, ...] = tuple(sources) if sources is not None else ()
        self._sources_wired = sources is not None
        self._source_factory = source_factory
        self._source_errors: tuple[str, ...] = ()
        self._available_vendors: tuple[Vendor, ...] = ()
        """Extra vendors whose corpus actually exists, as of the last
        ``_collect_quota_rows``. Written on the worker thread, read on the main
        one, and the reason a Claude-only machine gets the pre-Codex menu."""

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cc-usage-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        """Ask the worker to finish and wait briefly. Safe to call twice.

        Returns True when the thread is actually gone, so the caller can tell a
        clean exit from a join that merely timed out - the difference between
        "the final flush happened" and "it did not".
        """
        self._stop.set()
        self._commands.put(("__stop__", None))
        thread, self._thread = self._thread, None
        if thread is None:
            return True
        if timeout > 0 and thread.is_alive():
            thread.join(timeout=timeout)
        return not thread.is_alive()

    def signal_stop(self) -> None:
        """Ask the worker to finish **without** waiting for it.

        The quit path uses this so the AppKit main thread is never blocked on a
        Keychain read or an HTTPS request that the worker happens to be inside
        (SPEC 2.3); liveness is then polled from the repaint tick.
        """
        self._stop.set()
        self._commands.put(("__stop__", None))

    @property
    def alive(self) -> bool:
        """Whether the worker thread is running.

        The main thread polls this: a dead worker publishes nothing, so without
        the check the menu would keep painting its last title forever with no
        error line and no way for the user to tell (Rule 12).
        """
        thread = self._thread
        return thread is not None and thread.is_alive()

    def submit(self, name: str, payload: Any = None) -> None:
        """Queue a command from the main thread; wakes the worker at once."""
        self._commands.put((name, payload))

    @property
    def cost_available(self) -> bool:
        """True when the cost side is wired (a scanner + store + prices).

        "A scanner" rather than "the Claude indexer": a machine with only a
        Codex corpus still has real cost to show, and gating on the Claude
        indexer would have made it read ``unavailable — cost modules are not
        wired`` (SPEC-CODEX 5.5).
        """
        if self._rollups is None or self._pricing is None:
            return False
        return self._indexer is not None or bool(self._sources)

    def _scanners(self) -> list[tuple[Vendor, Any]]:
        """``(vendor, scanner)`` for every corpus this widget should read.

        The Claude indexer comes first and is always included - it predates
        :class:`~cc_usage_widget.contracts.TranscriptSource` and has no
        ``available()``; the extra sources are filtered by their own
        ``available()``, because an absent ``~/.codex`` is a normal state and
        must produce no error, no empty section and no zeroed cost.
        """
        out: list[tuple[Vendor, Any]] = []
        if self._indexer is not None:
            out.append((getattr(self._indexer, "vendor", VENDOR_CLAUDE), self._indexer))
        codex_on = bool(self._settings().get("codex_tracking_enabled", True))
        for source in self._sources:
            vendor = getattr(source, "vendor", VENDOR_CODEX)
            if vendor != VENDOR_CLAUDE and not codex_on:
                continue
            available = getattr(source, "available", None)
            if callable(available):
                try:
                    if not available():
                        continue
                except Exception as exc:  # a probe must never break the tick
                    _log(f"{vendor} source availability check failed: {exc!r}")
                    continue
            out.append((vendor, source))
        return out

    @property
    def extra_sources(self) -> tuple[Any, ...]:
        """The non-Claude sources actually wired (may be unavailable)."""
        return self._sources

    @property
    def source_errors(self) -> tuple[str, ...]:
        """Wiring failures of the extra sources, for the ``!`` menu lines."""
        return self._source_errors

    def source_roots(self) -> tuple[tuple[str, str], ...]:
        """``(label, path)`` per extra source **that has a corpus**.

        Filtered by :attr:`available_vendors` rather than by existence of the
        source object: a Claude-only machine must not be shown a
        ``Codex: /Users/…/.codex/sessions`` diagnostics line pointing at a
        directory that is not there. No ``stat`` happens here - this is called
        from the main thread while a menu is being built.
        """
        out: list[tuple[str, str]] = []
        present = self._available_vendors
        for source in self._sources:
            vendor = getattr(source, "vendor", VENDOR_CODEX)
            if vendor not in present:
                continue
            root = getattr(source, "root", None)
            if root is not None:
                out.append((vendor_label(vendor), str(root)))
        return tuple(out)

    def _wire_sources(self) -> None:
        """Build the extra vendor sources. **Worker thread only.**

        Deferred here rather than done in the constructor because building a
        source stats a corpus root, and the constructor runs on the AppKit main
        thread (SPEC 2.3). Runs exactly once; a missing ``codex_indexer.py`` is
        silent (a Claude-only install must see today's exact menu), while a
        module that imports but yields no usable source is surfaced - that is a
        broken feature, not an absent one.
        """
        if self._sources_wired or self._source_factory is None:
            return
        self._sources_wired = True
        try:
            built = tuple(self._source_factory(self._settings(), self._pricing) or ())
        except Exception as exc:
            self._source_errors = (f"codex source unavailable: {_describe(exc)}",)
            built = ()
        self._sources = built
        if built:
            _log(f"wired {len(built)} extra transcript source(s)")
        self._publish(replace(self._snapshot, quota_rows=self._collect_quota_rows()))

    def _collect_quota_rows(self) -> tuple[AccountRow, ...]:
        """Read-only quota rows from the extra sources (SPEC-CODEX 4).

        Cheap by contract: a source reports what its **last scan** learned and
        must not scan here. Still worker-thread only, because "cheap" is not
        "guaranteed non-blocking" and the AppKit thread gets no I/O at all.

        Also records which extra vendors actually have a corpus
        (:attr:`available_vendors`). That answer costs a ``stat``, so it can
        only be produced here, on this thread - and the main thread needs it:
        "does this machine have Codex at all" is a question about the
        *filesystem*, not about whether a source object was constructed
        (``__main__.build()`` constructs one unconditionally). Availability is
        recorded independently of ``codex_tracking_enabled``, or switching the
        vendor off would delete the switch that turns it back on.
        """
        rows: list[AccountRow] = []
        present: list[Vendor] = []
        codex_on = bool(self._settings().get("codex_tracking_enabled", True))
        for source in self._sources:
            vendor = getattr(source, "vendor", VENDOR_CODEX)
            available = getattr(source, "available", None)
            if callable(available):
                try:
                    if not available():
                        continue
                except Exception as exc:  # a probe must never break the tick
                    _log(f"{vendor} source availability check failed: {exc!r}")
                    continue
            present.append(vendor)
            if vendor != VENDOR_CLAUDE and not codex_on:
                continue
            getter = getattr(source, "quota_rows", None)
            if not callable(getter):
                continue
            try:
                rows.extend(getter() or ())
            except Exception as exc:
                _log(f"quota_rows failed: {_describe(exc, 'quota')}")
        self._available_vendors = tuple(present)
        return tuple(rows)

    @property
    def available_vendors(self) -> tuple[Vendor, ...]:
        """Extra vendors whose corpus existed at the last :meth:`_collect_quota_rows`.

        A plain tuple read, so the main thread may have it: the ``stat`` that
        produced it already happened on the worker.
        """
        return self._available_vendors

    def supports_index_rebuild(self) -> bool:
        """True when the rollup store can be emptied.

        A rebuild re-reads transcripts from offset 0, and ``merge`` *adds*, so
        rebuilding without first emptying the store would double-count every
        historical day. The menu item therefore only appears when the store
        exposes a clearing method - it is never faked with ``prune``.
        """
        store = self._rollups
        if store is None or (self._indexer is None and not self._sources):
            return False
        return any(callable(getattr(store, name, None)) for name in ("clear", "reset", "drop_all"))

    # -- publishing --------------------------------------------------------

    def _publish(self, snapshot: UiSnapshot) -> None:
        self._snapshot = snapshot
        try:
            self._publish_cb(snapshot)
        except Exception as exc:  # never let the UI hand-off kill the loop
            _log(f"publish failed: {exc!r}")

    def _settings(self) -> dict[str, Any]:
        return self._snapshot.settings

    # -- the loop ----------------------------------------------------------

    def _loop(self) -> None:
        next_ui = 0.0
        next_cost = 0.0
        try:
            while not self._stop.is_set():
                try:
                    stop_now, next_ui, next_cost = self._loop_once(next_ui, next_cost)
                except BaseException as exc:  # noqa: BLE001 - see below
                    # `except Exception` was not enough. A CLI-shaped helper
                    # inside claude_swap calling `sys.exit()` on a fatal config
                    # error raises SystemExit, which is a BaseException: it
                    # escaped the loop, skipped the flush, and killed the thread
                    # for the rest of the process's life. The menu then painted
                    # its last title forever with no error line. Anything short
                    # of the stop event now degrades to a visible `!` row and the
                    # loop keeps its schedule.
                    if isinstance(exc, KeyboardInterrupt):
                        raise
                    self._publish(
                        replace(self._snapshot, accounts_error=_describe(exc))
                    )
                    next_ui = time.monotonic() + self._ui_interval()
                    next_cost = time.monotonic() + self._cost_interval()
                    continue
                if stop_now:
                    break
        finally:
            # Reached on every exit path, including a BaseException the guard
            # above re-raises: the flush is the only thing that closes the
            # merged-but-unsaved rollup window.
            self._flush()

    def _loop_once(self, next_ui: float, next_cost: float) -> tuple[bool, float, float]:
        """One iteration of :meth:`_loop`. Returns ``(stop, next_ui, next_cost)``."""
        now = time.monotonic()
        due = min(next_ui, next_cost)
        timeout = due - now
        command: tuple[str, Any] | None = None
        if timeout > 0:
            try:
                command = self._commands.get(timeout=timeout)
            except queue.Empty:
                command = None
        else:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                command = None

        if command is not None:
            if command[0] == "__stop__":
                return True, next_ui, next_cost
            force_ui, force_cost = self._handle_command(command)
            if force_ui:
                next_ui = 0.0
            if force_cost:
                next_cost = 0.0
                # An explicit click reads every corpus, not the one whose turn
                # it happens to be in the steady-state rotation.
                self._force_all_sources = True
            return False, next_ui, next_cost

        now = time.monotonic()
        if now >= next_ui:
            # Always re-arm, even if the job raised, so one broken read
            # cannot turn into a hot loop.
            next_ui = now + self._ui_interval()
            self._run_accounts_job(force=False)
        if not self._stop.is_set() and time.monotonic() >= next_cost:
            next_cost = self._run_cost_job()
        return False, next_ui, next_cost

    def _flush(self) -> None:
        """Last save before the thread exits - still off the main thread.

        The indexer persists an advanced offset as soon as it reads bytes, so a
        delta that was merged but never saved would be counted as consumed and
        lost. Saving here closes that window at quit; ``save`` is a no-op when
        the store is clean.
        """
        store = self._rollups
        if store is not None and self._rollups_loaded:
            try:
                store.save()
            except Exception as exc:
                _log(f"final rollup save failed: {_describe(exc)}")
        # Offsets last, and only now that the tokens they consumed are durable.
        self._commit_scan_state()
        for _vendor, scanner in self._all_scanners():
            flush_dedup = getattr(scanner, "flush_dedup", None)
            if callable(flush_dedup):
                try:
                    flush_dedup()
                except Exception as exc:
                    _log(f"dedup flush failed: {_describe(exc)}")

    def _all_scanners(self) -> list[tuple[Vendor, Any]]:
        """Every scanner ever wired, available or not.

        ``_scanners`` answers "what should this tick read"; this answers "whose
        state do we own". A source that has gone unavailable since its last
        scan - a removed volume, a toggled-off vendor - still has offsets that
        must be committed and flushed, or the tokens it already counted are
        lost.
        """
        out: list[tuple[Vendor, Any]] = []
        if self._indexer is not None:
            out.append((getattr(self._indexer, "vendor", VENDOR_CLAUDE), self._indexer))
        for source in self._sources:
            out.append((getattr(source, "vendor", VENDOR_CODEX), source))
        return out

    def _commit_scan_state(self) -> None:
        """Persist every scanner's offsets, for those that defer it to us.

        Ordering is the whole point: the rollup must be on disk *before* the
        offsets that say those bytes were consumed. The reverse order turns any
        crash in the window into a permanent, invisible undercount that survives
        restarts and is only curable via ``Rebuild cost index``. With two
        vendors the rule is unchanged and now applies to both - one shared
        ``rollups.json``, one scan-state file per vendor (SPEC-CODEX 4).
        """
        for _vendor, scanner in self._all_scanners():
            commit = getattr(scanner, "commit_state", None)
            if not callable(commit):
                continue
            try:
                commit()
            except Exception as exc:
                _log(f"scan-state commit failed: {_describe(exc)}")

    def _ui_interval(self) -> float:
        low, high = SETTINGS_BOUNDS["ui_interval_seconds"]
        try:
            value = int(self._settings().get("ui_interval_seconds", 60))
        except (TypeError, ValueError):
            value = int(SETTINGS_DEFAULTS["ui_interval_seconds"])
        return float(min(max(value, low), high))

    def _cost_interval(self) -> float:
        low, high = SETTINGS_BOUNDS["cost_interval_seconds"]
        try:
            value = int(self._settings().get("cost_interval_seconds", 300))
        except (TypeError, ValueError):
            value = int(SETTINGS_DEFAULTS["cost_interval_seconds"])
        return float(min(max(value, low), high))

    # -- commands ----------------------------------------------------------

    def _handle_command(self, command: tuple[str, Any]) -> tuple[bool, bool]:
        """Run one command. Returns ``(force_ui, force_cost)``."""
        name, payload = command
        try:
            if name == _CMD_REFRESH:
                self._run_accounts_job(force=True)
                return False, True
            if name == _CMD_SET_AUTOSWITCH:
                self._set_autoswitch(bool(payload))
                return True, False
            if name == _CMD_SET_SETTING:
                key, value = payload
                cost_touching = key in (
                    "cost_tracking_enabled",
                    "lookback_days",
                    "codex_tracking_enabled",
                )
                self._set_setting(key, value)
                return True, cost_touching
            if name == _CMD_SWITCH_TO:
                self._switch_to(str(payload))
                return True, False
            if name == _CMD_REBUILD_INDEX:
                self._rebuild_index()
                return False, True
            if name == _CMD_WIRE_SOURCES:
                self._wire_sources()
                return True, True
        except Exception as exc:
            self._publish(replace(self._snapshot, accounts_error=_describe(exc)))
        else:
            _log(f"ignoring unknown command {name!r}")
        return False, False

    def _set_autoswitch(self, enabled: bool) -> None:
        """Write through to claude-swap, then publish *its* answer.

        ``claude_swap``'s ``autoswitch.*`` settings are the single source of
        truth (SPEC 3.1); our ``settings.json`` key is a mirror so the choice
        survives a restart even if claude-swap reports nothing.
        """
        error: str | None = None
        if self._accounts is None:
            error = "no account source wired"
        else:
            try:
                self._accounts.set_autoswitch_enabled(enabled)
            except Exception as exc:
                error = _describe(exc)
        settings = self._merge_settings("autoswitch_enabled", enabled)
        truth = self._read_autoswitch(default=bool(settings["autoswitch_enabled"]))
        self._publish(
            replace(
                self._snapshot,
                settings=settings,
                autoswitch_enabled=truth,
                accounts_error=error,
            )
        )

    def _set_setting(self, key: str, value: Any) -> None:
        settings = self._merge_settings(key, value)
        snapshot = replace(self._snapshot, settings=settings)
        if not settings.get("cost_tracking_enabled", True):
            snapshot = replace(snapshot, cost=None, cost_error=None)
        if not settings.get("codex_tracking_enabled", True):
            # Drop the pseudo-accounts now rather than leaving a frozen quota
            # bar on screen: they are only ever as fresh as the last scan of
            # the corpus we just stopped scanning.
            snapshot = replace(snapshot, quota_rows=())
        self._publish(snapshot)

    def _merge_settings(self, key: str, value: Any) -> dict[str, Any]:
        """Normalise, persist, and return the new settings dict.

        :func:`~cc_usage_widget.contracts.normalize_settings` clamps and
        type-checks, so an out-of-range interval from a stale menu can never
        reach the loop.
        """
        settings = normalize_settings({**self._snapshot.settings, key: value})
        if self._persist_settings is not None:
            try:
                self._persist_settings(settings)
            except Exception as exc:
                _log(f"could not persist settings: {_describe(exc)}")
        return settings

    def _switch_to(self, slot_or_alias: str) -> None:
        if self._accounts is None:
            return
        error: str | None = None
        try:
            if not self._accounts.switch_to(slot_or_alias):
                error = f"switch to {slot_or_alias} was refused"
        except Exception as exc:
            error = _describe(exc)
        self._run_accounts_job(force=True, error_override=error)

    def _rebuild_index(self) -> None:
        """Empty the rollup store and reset **every** scanner, then re-index.

        All-or-nothing across vendors on purpose: the store is shared, so
        emptying it while resetting only one vendor would drop the other
        vendor's days without re-reading them (its offsets still say
        "consumed") - a permanent, invisible undercount.
        """
        scanners = self._all_scanners()
        if not scanners or self._rollups is None:
            return
        try:
            for name in ("clear", "reset", "drop_all"):
                method = getattr(self._rollups, name, None)
                if callable(method):
                    method()
                    break
            else:
                raise RuntimeError("rollup store cannot be emptied; refusing to re-index")
            for _vendor, scanner in scanners:
                scanner.reset()
            # Deliberately emptied, so no vendor may later be diagnosed as
            # having "lost" its offsets and have its rows dropped a second time.
            self._reconciled.update(vendor for vendor, _scanner in scanners)
            try:
                self._rollups.save()
            except Exception as exc:
                _log(f"rollup save after reset failed: {_describe(exc)}")
            self._publish(
                replace(self._snapshot, cost=None, cost_error=None, progress=IndexProgress())
            )
        except Exception as exc:
            self._publish(replace(self._snapshot, cost_error=_describe(exc)))

    def _apply_lookback(self, days: int) -> None:
        """Push a changed ``lookback_days`` into the seams that cache it.

        ``prune`` takes the window as an argument, but the indexer's scan
        window and the store's own window (which bounds ``save`` and clamps the
        breakdown's ``Last Nd`` labels) are constructor state. Without this a
        Settings change would be only half-applied. Both setters are optional -
        neither is in the protocol - so a seam that does not expose one keeps
        whatever it was built with.
        """
        if self._lookback_applied == days:
            return
        self._lookback_applied = days
        targets: list[tuple[Any, str]] = [(self._rollups, "set_keep_days")]
        targets += [(scanner, "set_lookback_days") for _v, scanner in self._all_scanners()]
        for target, name in targets:
            method = getattr(target, name, None)
            if callable(method):
                try:
                    method(days)
                except Exception as exc:
                    _log(f"{name}({days}) failed: {_describe(exc)}")

    # -- jobs --------------------------------------------------------------

    def _read_autoswitch(self, default: bool) -> bool:
        """claude-swap's answer, falling back to our mirrored setting."""
        if self._accounts is None:
            return default
        try:
            value = self._accounts.autoswitch_enabled()
        except Exception as exc:
            _log(f"autoswitch_enabled failed: {_describe(exc)}")
            return default
        return default if value is None else bool(value)

    def _accounts_diagnosis(self, rows: tuple[AccountRow, ...]) -> str | None:
        """Turn an empty, uncomplaining account read into a visible error.

        ``SwapAccountSource`` never raises: a missing ``claude_swap``, a locked
        Keychain or a renamed upstream symbol all end up as ``last_error`` plus
        an empty ``rows()``. Without this, a completely broken backend rendered
        as a normal, *empty* menu (``Accounts`` -> ``none found``) and the only
        trace was a log line on a stderr the user never sees (Rule 12).
        """
        if rows:
            return None
        source = self._accounts
        cause = getattr(source, "last_error", None)
        unavailable = getattr(source, "available", True) is False
        if not cause and not unavailable:
            return None
        return f"{ACCOUNTS_UNAVAILABLE}: {cause or 'no snapshot yet'}"[:MAX_ERROR_CHARS]

    def _run_accounts_job(self, *, force: bool, error_override: str | None = None) -> None:
        """Read claude-swap's usage store and (if enabled) tick autoswitch.

        ``refresh(force=False)`` respects claude-swap's pacing, so this makes
        no API call of our own (SPEC 3.5).

        It also re-reads the vendor pseudo-accounts, which is what keeps the
        Codex quota block on the 60 s cadence with the Claude bars rather than
        the 300 s cost cadence - and what makes it appear on a machine that has
        no ``claude_swap`` at all (SPEC-CODEX 5.5).
        """
        if self._accounts is None:
            quota = self._collect_quota_rows()
            if quota != self._snapshot.quota_rows:
                self._publish(replace(self._snapshot, quota_rows=quota))
            return
        try:
            self._accounts.refresh(force=force)
            rows = tuple(self._accounts.rows())
            active = self._accounts.active()
            autoswitch = self._read_autoswitch(
                default=bool(self._settings().get("autoswitch_enabled", False))
            )
            if autoswitch:
                # The adapter enforces claude-swap's own cadence and is a no-op
                # when the toggle is off; the check above is belt-and-braces so
                # a stale adapter cannot switch accounts behind an OFF label.
                switched = self._accounts.evaluate_autoswitch()
                if switched:
                    _log(f"autoswitch -> {switched}")
                    self._accounts.refresh(force=True)
                    rows = tuple(self._accounts.rows())
                    active = self._accounts.active()
            if not error_override:
                error_override = self._accounts_diagnosis(rows)
            if not error_override:
                _forget_failures("accounts")
            self._publish(
                replace(
                    self._snapshot,
                    accounts=rows,
                    quota_rows=self._collect_quota_rows(),
                    active=active,
                    autoswitch_enabled=autoswitch,
                    accounts_error=error_override,
                    accounts_at=time.time(),
                )
            )
        except Exception as exc:
            self._publish(
                replace(
                    self._snapshot,
                    accounts_error=error_override or _describe(exc, "accounts"),
                    accounts_at=time.time(),
                )
            )

    @staticmethod
    def _store_has_vendor(rollups: RollupStore, vendor: Vendor) -> bool:
        """Whether the store already holds days containing *vendor*'s usage.

        This is what makes the doubling guard below **per vendor**: adding
        Codex to an existing install means Codex starts with no scan state
        while the store is full of Claude days, and that is not a doubling
        risk - there is nothing of Codex's in there to double. Only a vendor
        whose own rows survived while its own offsets vanished can double.

        Errs towards ``True`` on any surprise: over-clearing costs one
        re-index, under-clearing corrupts the figures permanently.
        """
        try:
            for day in rollups.days():
                rollup = rollups.get(day)
                if rollup is None:
                    continue
                vendors = getattr(rollup, "vendors", None)
                if vendors is None:
                    return bool(getattr(rollup, "models", None)) and vendor == VENDOR_CLAUDE
                if vendor in vendors:
                    return True
        except Exception as exc:
            _log(f"could not inspect the rollup store for {vendor}: {exc!r}")
            return True
        return False

    def _reconcile_lost_scan_state(
        self, scanners: Sequence[tuple[Vendor, Any]], rollups: RollupStore
    ) -> None:
        """Drop a vendor's cached rows when *its* scan state came back missing.

        ``rollups.json`` and ``scan_state.json`` are two independently-persisted
        halves of one accounting fact, and ``merge`` is purely additive. If the
        offsets are gone while the rollup survived - the file deleted, truncated,
        or unparseable - the next pass re-reads the whole lookback window and
        ADDS it on top of what is already there: every day in the window doubles,
        permanently, once per loss (measured: ``Last 3d`` $1,568.52 -> $3,137.03,
        exactly 2.000x). This is the same invariant ``_rebuild_index`` documents;
        the only difference is that nobody clicked anything.

        **Trigger and cure are both per vendor.** The two vendors share one
        store because a window total has to span both, but they do not share an
        accounting fate: only a vendor that lost its own offsets while its own
        rows survived can double (:meth:`_store_has_vendor`), so only that
        vendor's rows are dropped (``RollupStore.drop_vendors``) and only its
        scanner is reset. Curing globally would delete the other vendor's
        history to fix a fault it did not have - and that history is not always
        reconstructible: Claude Code prunes ``~/.claude/projects`` on its own
        ``cleanupPeriodDays``, so a day that has aged off disk would be zeroed
        for good by a Codex-side cache loss. It also contradicts what both
        modules advertise (``contracts.CODEX_SCAN_STATE_PATH``: "deleting one
        vendor's cache re-indexes only that vendor").

        A store too old to know about vendors falls back to the global
        ``clear()`` + reset-everything cure, because for such a store the only
        vendor that can be in it is claude.

        **Called for every scanner the first time it appears**, not once per
        process: ``codex_tracking_enabled`` can be switched on mid-session, and
        an unavailable corpus (a late-mounted volume) can become available, so
        a source can join the tick after the store was loaded. Its first
        ``scan_once`` is still its first merge, and that is what has to be
        reconciled. :attr:`started_from_empty_state` is fixed at load, so
        re-asking is stable; ``_reconciled`` is what keeps the cure to once per
        vendor.
        """
        lost = [
            vendor
            for vendor, scanner in scanners
            if vendor not in self._reconciled
            and getattr(scanner, "started_from_empty_state", None) is True
            and self._store_has_vendor(rollups, vendor)
        ]
        self._reconciled.update(vendor for vendor, _scanner in scanners)
        if not lost:
            return
        days = len(rollups) if hasattr(rollups, "__len__") else 0
        if not days:
            return  # ordinary first run: nothing to double
        drop_vendors = getattr(rollups, "drop_vendors", None)
        if callable(drop_vendors):
            removed = drop_vendors(lost)
            # Only the lost vendors are re-read, so only their offsets may be
            # thrown away. The others' rows are still in the store and their
            # offsets still describe them correctly.
            doomed = set(lost)
            reset = [pair for pair in scanners if pair[0] in doomed]
            note = (
                f"scan state was missing for {', '.join(lost)}; dropped "
                f"{removed} cached row(s) for that vendor and re-read only its "
                "corpus, leaving every other vendor's history intact"
            )
        else:
            for name in ("clear", "reset", "drop_all"):
                method = getattr(rollups, name, None)
                if callable(method):
                    method()
                    break
            else:
                self._publish(
                    replace(
                        self._snapshot,
                        cost_error=(
                            "scan state lost and the rollup store cannot be emptied; "
                            "figures would double — use Rebuild cost index"
                        ),
                    )
                )
                return
            # A vendor-blind store was emptied wholesale, so every scanner's
            # offsets now say "consumed" for days that are gone; without
            # resetting them all, their history would vanish instead of being
            # re-read.
            reset = list(scanners)
            note = (
                f"scan state was missing for {', '.join(lost)}; dropped {days} "
                "cached day(s) and reset every source so the window is not "
                "counted twice"
            )
        for _vendor, scanner in reset:
            try:
                scanner.reset()
            except Exception as exc:
                _log(f"reset after reconciliation failed: {_describe(exc)}")
        _log(note)
        try:
            rollups.save()
        except Exception as exc:
            _log(f"rollup save after reconciliation failed: {_describe(exc)}")

    @staticmethod
    def _scan_note(vendor: Vendor, result: Any, *, labelled: bool) -> str:
        """The SPEC 2.1 evidence line for one scanner.

        Unlabelled and byte-identical to the pre-Codex wording when there is
        only one corpus, so a Claude-only install's diagnostics do not change.
        """
        prefix = f"{vendor_label(vendor)}: " if labelled else ""
        return (
            f"{prefix}{result.duration_ms:.0f} ms · {result.files_read} of "
            f"{result.files_seen} file(s) read · {result.bytes_read / 1024:,.0f} KB"
            f" · {result.records_counted:,} records"
        )

    def _run_cost_job(self) -> float:
        """One incremental scan per vendor + cost recompute. Returns the next due time.

        The shape is unchanged from the single-vendor version and every
        ordering guarantee still holds; the only difference is that the scan
        step is a loop, and that the merged deltas of both vendors land in one
        store keyed by ``(vendor, model)`` so the window totals span both
        (SPEC-CODEX 5.2).
        """
        interval = self._cost_interval()
        settings = self._settings()
        if not settings.get("cost_tracking_enabled", True):
            if self._snapshot.cost is not None:
                self._publish(replace(self._snapshot, cost=None))
            return time.monotonic() + interval
        if not self.cost_available:
            return time.monotonic() + interval

        rollups = self._rollups
        pricing = self._pricing
        assert rollups is not None and pricing is not None
        scanners = self._scanners()
        if not scanners:
            # Every corpus is absent or switched off: nothing to read, and an
            # absent corpus is a normal state, not an error (SPEC-CODEX 5.5).
            return time.monotonic() + interval

        try:
            if not self._rollups_loaded:
                # Load before the first merge, never after: a load replaces the
                # in-memory store and would discard freshly merged deltas.
                rollups.load()
                self._rollups_loaded = True
            # Every tick, not only the first: a scanner that joins later (the
            # Codex toggle switched on, a corpus that became available) has its
            # own first merge to reconcile. Cheap - a no-op set membership test
            # once every vendor has been seen.
            self._reconcile_lost_scan_state(scanners, rollups)

            keep_days = int(
                settings.get("lookback_days", SETTINGS_DEFAULTS["lookback_days"])
            )
            self._apply_lookback(keep_days)

            before = [scanner.progress() for _v, scanner in scanners]
            was_partial = any(not item.complete for item in before)
            if was_partial:
                # Chunked mode re-runs about once a second. Only the corpora
                # that are actually still indexing take part: re-walking a
                # finished ~3,200-file tree every second to satisfy another
                # vendor's first index would burn the SPEC 2.1 idle budget for
                # no new data, and that vendor's own 300 s cadence has not come
                # due anyway.
                due = [pair for pair, item in zip(scanners, before) if not item.complete]
            elif len(scanners) > 1 and not self._force_all_sources:
                # SPEC 2.1: the 30 ms budget is PER TICK, and a steady tick is
                # ~100% directory walk - 6,270 `DirEntry.stat()` calls across
                # the two trees, zero files opened. Measured here: Claude 19.9 ms
                # (a bare hand-written walk of the same tree costs 19.8) + Codex
                # 9.9 ms = 30.1 ms combined, over the line, and nothing in
                # Python can make a walk cheaper than the walk.
                #
                # So the two corpora are walked on ALTERNATE ticks and the tick
                # fires proportionally more often: each vendor is still scanned
                # once per `cost_interval_seconds`, its data is exactly as fresh,
                # the time-averaged CPU is unchanged - and no single tick pays
                # for both trees. A vendor joining or leaving just shifts the
                # rotation; the worst case is one extra interval before its turn.
                index = self._scan_cursor % len(scanners)
                self._scan_cursor = index + 1
                due = [scanners[index]]
                interval = interval / len(scanners)
            else:
                # One corpus, or an explicit Refresh / settings change, which
                # must answer for every vendor at once rather than a slice.
                due = scanners
            self._force_all_sources = False
            budget = INDEX_CHUNK_SECONDS if was_partial else STEADY_SCAN_DEADLINE_SECONDS
            # The budget is split, not handed to each in turn: during a first
            # index one 15 GB corpus would otherwise eat every chunk and the
            # other vendor's figures would stay empty for as long as it ran.
            share = budget / len(due)
            results = []
            for _vendor, scanner in due:
                result = scanner.scan_once(deadline=time.monotonic() + share)
                results.append(result)
                if result.deltas:
                    rollups.merge(result.deltas)

            today = local_day_key(time.time())
            rollups.prune(today=today, keep_days=keep_days)

            # Combined over EVERY scanner, not just the ones that ran: one
            # vendor still indexing must keep the dollar figures behind the
            # `indexing…` label (SPEC 4.3, SPEC-CODEX 5.2).
            progress = IndexProgress.combined(scanner.progress() for _v, scanner in scanners)
            any_deltas = any(result.deltas for result in results)
            if any_deltas:
                # Unthrottled on purpose. The old 5 s throttle left up to five
                # seconds of merged deltas in memory while the indexer's offsets
                # were already durable, so a force-quit in that window silently
                # dropped them for good. There are only ~14 chunks in a whole
                # first index, so writing an 8 KB file per delta-producing tick
                # costs nothing measurable.
                rollups.save()
                self._last_rollup_save = time.monotonic()
            # ONLY NOW may the offsets be persisted: "these bytes are consumed"
            # must never reach disk before the tokens they contained. One save
            # covers both vendors, so neither may commit before it.
            self._commit_scan_state()

            breakdown = rollups.cost_breakdown(pricing, today=today, progress=progress)
            note = " | ".join(
                self._scan_note(vendor, result, labelled=len(scanners) > 1)
                for (vendor, _scanner), result in zip(due, results)
            )
            # Per vendor, and remembered: a steady tick reads ONE corpus, so
            # taking the error line from this tick alone would clear a still-
            # broken vendor's `!` row every time the other vendor's turn came
            # round and then bring it back - an error that blinks is an error a
            # user learns to ignore (Rule 12). Each vendor's line stands until
            # that vendor is read again and reports differently.
            for (scanned, _s), result in zip(due, results):
                self._scan_errors[scanned] = tuple(result.errors)
            errors = tuple(
                error
                for vendor, _scanner in scanners
                for error in self._scan_errors.get(vendor, ())
            )
            warning: str | None = None
            if not errors:
                _forget_failures("cost")
            else:
                warning = f"{len(errors)} file(s) unreadable: {errors[0]}"[:MAX_ERROR_CHARS]
            self._publish(
                replace(
                    self._snapshot,
                    cost=breakdown,
                    progress=progress,
                    quota_rows=self._collect_quota_rows(),
                    cost_error=warning,
                    scan_note=note,
                    cost_at=time.time(),
                )
            )
            if not progress.complete:
                made_progress = any(
                    result.files_read > 0 or result.deltas for result in results
                )
                # No progress while incomplete means the indexer is stalled or
                # idle; back off to the normal interval instead of spinning.
                delay = INDEX_CHUNK_PAUSE_SECONDS if made_progress else interval
                return time.monotonic() + delay
            return time.monotonic() + interval
        except Exception as exc:
            self._publish(
                replace(
                    self._snapshot,
                    cost_error=_describe(exc, "cost"),
                    cost_at=time.time(),
                )
            )
            return time.monotonic() + interval


# ---------------------------------------------------------------------------
# Menu helpers
# ---------------------------------------------------------------------------


def _dedupe_titles(items: Sequence[Any]) -> list[Any]:
    """Make every title in one menu level unique.

    ``rumps.Menu`` keys items by title and ignores a key it already holds, so
    two rows that happen to render identically would silently collapse into
    one. Padding the duplicate with zero-width spaces keeps the visible text
    identical while giving it a distinct key.
    """
    seen: set[str] = set()
    out: list[Any] = []
    for item in items:
        if item is None:
            out.append(item)
            continue
        if isinstance(item, str):
            item = rumps.MenuItem(item)
        title = getattr(item, "title", None)
        if isinstance(title, str):
            unique = title
            while unique in seen:
                unique += _ZWSP
            if unique != title:
                item.title = unique
            seen.add(unique)
        out.append(item)
    return out


def _submenu(title: str, children: Sequence[Any]) -> rumps.MenuItem:
    """A parent item with *children* attached, titles de-duplicated."""
    parent = rumps.MenuItem(title)
    for child in _dedupe_titles(children):
        parent.add(child)
    return parent


def _info(text: str) -> rumps.MenuItem:
    """A non-clickable line (no callback = greyed out in AppKit)."""
    return rumps.MenuItem(text, callback=None)


def _check(item: rumps.MenuItem, on: bool) -> rumps.MenuItem:
    """Set the native checkmark. Used *in addition* to an ON/OFF label."""
    item.state = 1 if on else 0
    return item


def _apply_activation_policy() -> None:
    """``NSApplicationActivationPolicyAccessory`` - no Dock icon, no Cmd-Tab.

    ``rumps`` never sets a policy, so under a framework Python the process
    would park a "Python" icon in the Dock for as long as the widget runs
    (SPEC 5). Failure is non-fatal: a Dock icon is ugly, not broken.
    """
    try:
        import AppKit

        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory
        )
    except Exception as exc:  # pragma: no cover - depends on the host AppKit
        _log(f"could not set accessory activation policy: {exc!r}")


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


class CCUsageWidgetApp(rumps.App):
    """The status-bar app (SPEC 4).

    Collaborators are injected - this class constructs none of them, which is
    what lets ``__main__`` wire real implementations and a test wire fakes.
    Every method on this class runs on the AppKit main thread and does no I/O;
    clicks are turned into worker commands.
    """

    def __init__(
        self,
        *,
        accounts: AccountSource | None = None,
        indexer: TranscriptIndexer | None = None,
        rollups: RollupStore | None = None,
        pricing: PricingTable | None = None,
        settings: dict[str, Any] | None = None,
        persist_settings: Callable[[dict[str, Any]], None] | None = None,
        wiring_errors: Sequence[str] = (),
        sources: Sequence[TranscriptSource] | None = None,
        name: str = APP_NAME,
    ) -> None:
        super().__init__(name, title=TITLE_ICON, quit_button=None)
        normalized = normalize_settings(settings)
        self._lock = threading.Lock()
        self._snapshot = UiSnapshot(
            settings=normalized,
            autoswitch_enabled=bool(normalized["autoswitch_enabled"]),
            wiring_errors=tuple(wiring_errors),
        )
        self._dirty = False
        self._worker = BackgroundWorker(
            publish=self._publish,
            snapshot=self._snapshot,
            accounts=accounts,
            indexer=indexer,
            rollups=rollups,
            pricing=pricing,
            persist_settings=persist_settings,
            sources=sources,
            source_factory=_build_extra_sources,
        )
        self._sync_timer = rumps.Timer(self._on_sync_tick, SYNC_INTERVAL_SECONDS)
        self._quit_timer: Any = None
        self._quit_deadline = 0.0
        self._worker_restarts = 0
        self._running = False
        self.rebuild_menu()

    # -- lifecycle ---------------------------------------------------------

    def run(self, **options: Any) -> None:
        """Start the worker and the repaint tick, then enter the AppKit loop.

        The extra vendor sources are wired from **here**, as a queued worker
        command, for two reasons: building one stats a corpus root and the
        constructor runs on the AppKit main thread (SPEC 2.3), and a widget
        that is constructed but never run - every test in ``tests/`` - must not
        touch ``~/.codex`` at all.
        """
        _apply_activation_policy()
        self._running = True
        self._worker.submit(_CMD_WIRE_SOURCES, None)
        self._worker.start()
        self._sync_timer.start()
        try:
            super().run(**options)
        finally:
            self._running = False
            self._worker.stop()

    QUIT_GRACE_SECONDS = 5.0
    """How long the worker gets to reach its final flush after a Quit."""

    def shutdown(self) -> None:
        """Ask the worker to finish, then leave the AppKit loop once it has.

        The single quit path: the Quit menu item and ``__main__``'s SIGINT /
        SIGTERM handlers all call this, so a signal exits exactly as cleanly as
        a click. Idempotent.

        It **signals** rather than joins. Joining here blocked the AppKit main
        thread for as long as the worker was inside a Keychain read or an HTTPS
        request (SPEC 2.3), and a join that timed out was indistinguishable from
        a clean exit - so the final flush was skipped silently. Liveness is
        polled instead, and a deadline that expires is logged loudly.
        """
        if self._quit_timer is not None:
            return  # a quit is already in flight
        try:
            self._sync_timer.stop()
        except Exception:  # pragma: no cover - a timer that never started
            pass
        if not self._worker.alive:
            self._worker.stop(timeout=0.0)
            rumps.quit_application()
            return
        self._worker.signal_stop()
        self._quit_deadline = time.monotonic() + self.QUIT_GRACE_SECONDS
        try:
            self._quit_timer = rumps.Timer(self._on_quit_tick, 0.2)
            self._quit_timer.start()
        except Exception:  # pragma: no cover - no runloop to hang a timer on
            self._worker.stop(timeout=self.QUIT_GRACE_SECONDS)
            rumps.quit_application()

    def _on_quit_tick(self, _timer: Any) -> None:
        """Poll the worker after a Quit; leave the loop when it is gone."""
        try:
            if self._worker.alive and time.monotonic() < self._quit_deadline:
                return
            if self._worker.alive:
                _log(
                    "worker did not finish within "
                    f"{self.QUIT_GRACE_SECONDS:.0f}s; quitting anyway "
                    "(a merged-but-unsaved rollup delta may be lost)"
                )
            try:
                self._quit_timer.stop()
            except Exception:  # pragma: no cover - defensive
                pass
            self._worker.stop(timeout=0.0)
        finally:
            rumps.quit_application()

    def kickoff(self) -> None:
        """Queue the first account read and cost scan on the worker thread.

        :meth:`run` already starts the worker with both jobs due, so this is
        belt-and-braces for an explicit ``__main__``: it makes the first pass
        queued work rather than a schedule the loop happens to notice, and it is
        what starts the first-run index off the AppKit thread before the menu is
        ever painted (SPEC 3.2 "First run"). Safe before or after
        :meth:`run`; the queue is drained as soon as the thread exists.
        """
        self._worker.submit(_CMD_REFRESH, None)

    # -- thread hand-off ---------------------------------------------------

    def _publish(self, snapshot: UiSnapshot) -> None:
        """Called on the worker thread. Stores and flags; paints nothing."""
        with self._lock:
            self._snapshot = snapshot
            self._dirty = True

    def snapshot(self) -> UiSnapshot:
        """Current snapshot (main thread reads, worker writes, under a lock)."""
        with self._lock:
            return self._snapshot

    @property
    def extra_sources(self) -> tuple[Any, ...]:
        """The non-Claude transcript sources the worker is driving.

        Read-only, for diagnostics (``--dry-run``). The tuple itself is safe to
        read from any thread; calling into a source is **not** - only
        :meth:`TranscriptSource.available` is documented cheap and non-throwing,
        and that is all a diagnostic should touch.
        """
        return self._worker.extra_sources

    def _on_sync_tick(self, _timer: Any) -> None:
        """Main-thread repaint tick: cheap when nothing changed.

        Wrapped end to end - an exception escaping a ``rumps.Timer`` callback
        would take the tick (and with it every future repaint) down.
        """
        try:
            self._supervise_worker()
            with self._lock:
                dirty, self._dirty = self._dirty, False
                snapshot = self._snapshot
            if dirty:
                self.rebuild_menu(snapshot)
        except Exception as exc:  # pragma: no cover - defensive
            _log(f"repaint failed: {_describe(exc)}")

    MAX_WORKER_RESTARTS = 5
    """Restarts attempted before the widget stops trying and just says so."""

    def _supervise_worker(self) -> None:
        """Restart the worker if it died, and say so in the menu.

        Nothing else notices a dead worker: it publishes nothing, so the icon
        keeps painting the last title with no ``!`` row and every figure freezes
        at its launch value. This is the only liveness check in the program
        (Rule 12: a skipped job must be visible, not silent).
        """
        if not self._running or self._quit_timer is not None:
            return
        if self._worker.alive:
            return
        if self._worker_restarts >= self.MAX_WORKER_RESTARTS:
            return
        self._worker_restarts += 1
        message = (
            f"background worker stopped — restarting "
            f"({self._worker_restarts}/{self.MAX_WORKER_RESTARTS})"
        )
        _log(message)
        marker = "background worker stopped"
        with self._lock:
            kept = tuple(
                text for text in self._snapshot.wiring_errors if marker not in text
            )
            self._snapshot = replace(self._snapshot, wiring_errors=kept + (message,))
            self._dirty = True
        self._worker.start()  # idempotent

    # -- title (SPEC 4.1) --------------------------------------------------

    def render_title(self, snapshot: UiSnapshot | None = None) -> str:
        """``"* podol 17% F3% $12/d"`` with each component toggleable.

        Falls back to the icon alone when every component is off or nothing has
        loaded yet, because a status item with an empty title is invisible and
        the user finds this widget by its glyph.
        """
        snapshot = snapshot or self.snapshot()
        settings = snapshot.settings
        parts: list[str] = []
        if settings.get("title_show_icon", True):
            parts.append(TITLE_ICON)
        active = snapshot.active
        if active is not None:
            if settings.get("title_show_alias", True):
                parts.append(_display_name(active))
            if settings.get("title_show_five_hour_pct", True) and active.five_hour_pct is not None:
                parts.append(_title_pct(active.five_hour_pct))
            if settings.get("title_show_scoped_pct", True):
                window = active.primary_scoped_window
                if window is not None:
                    name, pct = window
                    parts.append(f"{active.scoped_abbrev(name)}{_title_pct(pct)}")
        if settings.get("title_show_codex_pct", False):
            codex = self._title_vendor_pct(snapshot)
            if codex:
                parts.append(codex)
        if settings.get("title_show_cost", True) and settings.get("cost_tracking_enabled", True):
            cost_part = self._title_cost(snapshot)
            if cost_part:
                parts.append(cost_part)
        if parts:
            return " ".join(parts)
        # Every text component off: with a real NSImage on the status item the
        # right answer is NO text (an icon-only ~33pt item that fits a saturated
        # bar without evicting a neighbour — RCA 2026-08-17). The glyph fallback
        # only guards the case where the image could not be installed, because
        # an item with neither image nor title is zero-width and invisible.
        return "" if getattr(self, "_icon_image_set", False) else TITLE_ICON

    def _title_vendor_pct(self, snapshot: UiSnapshot) -> str:
        """``"C12%"`` — a pseudo-account's window in the menu bar.

        Off by default (``title_show_codex_pct``): the title is already five
        components wide and the menu bar is finite. The abbreviation is the
        vendor's initial, derived exactly as ``AccountRow.scoped_abbrev``
        derives a scoped window's, so no second naming rule exists. The menu's
        Codex section is **not** gated on this setting.
        """
        for row in self._visible_quota_rows(snapshot):
            windows = _quota_windows(row)
            if not windows:
                continue
            # The plan window (Codex's weekly ``primary``) is the figure that
            # answers "how much of my subscription have I used"; anything else
            # this row happens to report is a fallback, never a substitute.
            pct = row.seven_day_pct if row.seven_day_pct is not None else windows[0][1]
            initial = (row.alias or vendor_label(row.vendor))[:1].upper()
            return f"{initial}{_title_pct(pct)}"
        return ""

    def _title_cost(self, snapshot: UiSnapshot) -> str | None:
        """Today's notional cost, or ``$.../d`` while the index is partial.

        Never a partial figure: an in-progress total in the menu bar is the
        exact dishonesty SPEC 4.3 forbids.
        """
        if not self._worker.cost_available:
            return None
        cost = snapshot.cost
        if cost is None or cost.is_partial:
            return "$…/d"
        return _title_usd(cost.today.usd)

    # -- menu (SPEC 4.2) ---------------------------------------------------

    def rebuild_menu(self, snapshot: UiSnapshot | None = None) -> None:
        """Rebuild the whole menu from *snapshot*. Main thread only."""
        snapshot = snapshot or self.snapshot()
        self.title = self.render_title(snapshot)
        self._install_icon_once()

        items: list[Any] = [self._header_item(snapshot), None]
        items.extend(self._switch_items(snapshot))
        # Sections, each preceded by its own separator and each free to be
        # empty. A Claude-only machine yields exactly the pre-Codex layout
        # (accounts, cost); a Codex-only one drops the accounts section rather
        # than showing an empty one (SPEC-CODEX 5.5).
        for section in (
            self._account_items(snapshot),
            self._quota_items(snapshot),
            self._cost_items(snapshot),
        ):
            if section:
                items.append(None)
                items.extend(section)
        problems = self._problem_items(snapshot)
        if problems:
            items.append(None)
            items.extend(problems)
        items.append(None)
        items.append(self._switch_account_submenu(snapshot))
        items.append(rumps.MenuItem("Refresh now", callback=self._on_refresh_now))
        items.append(self._settings_submenu(snapshot))
        items.append(rumps.MenuItem("Quit", callback=self._on_quit))

        self.menu.clear()
        self.menu = _dedupe_titles(items)

    def _header_item(self, snapshot: UiSnapshot) -> rumps.MenuItem:
        active = snapshot.active
        if active is None:
            return _info("No active account")
        text = f"{_display_name(active)} ({active.email}) — active"
        if active.usage_is_stale:
            text = f"{text} · usage {_age_label(active.usage_age_seconds)} old"
        return _info(text)

    def _switch_items(self, snapshot: UiSnapshot) -> list[rumps.MenuItem]:
        """The two top-level on/off switches - one click each (SPEC 4.2).

        These are deliberately *not* inside Settings: it is an explicit product
        requirement that both are reachable in a single click.
        """
        autoswitch_on = bool(snapshot.autoswitch_enabled)
        cost_on = bool(snapshot.settings.get("cost_tracking_enabled", True))
        return [
            _check(
                rumps.MenuItem(
                    self._switch_label("Auto-switch", autoswitch_on),
                    callback=self._on_toggle_autoswitch,
                ),
                autoswitch_on,
            ),
            _check(
                rumps.MenuItem(
                    self._switch_label("Cost tracking", cost_on),
                    callback=self._on_toggle_cost_tracking,
                ),
                cost_on,
            ),
        ]

    @staticmethod
    def _switch_label(name: str, on: bool) -> str:
        """``"Auto-switch:      ON"`` - state is in the text, not only in the
        checkmark, so it reads correctly at a glance."""
        return f"{name + ':':<18}{'ON' if on else 'OFF'}"

    def _install_icon_once(self) -> None:
        """Put an SF Symbol template image on the status item (once).

        Deferred to the first repaint because rumps only creates the
        NSStatusItem in ``applicationDidFinishLaunching_``; before that there is
        nothing to set an image on. Purely cosmetic — any failure leaves the
        text title in place, so this can never stop the widget from working.
        """
        if getattr(self, "_icon_installed", False):
            return
        self._icon_installed = True  # one attempt, whatever happens
        try:
            item = getattr(getattr(self, "_nsapp", None), "nsstatusitem", None)
            if item is None:
                self._icon_installed = False  # retry on the next repaint
                return
            image = render.status_icon()
            if image is not None:
                item.setImage_(image)
                self._icon_image_set = True
                # The title may have been rendered before the image existed;
                # re-render so the ⇄ fallback drops off an icon-only item.
                self.title = self.render_title()
        except Exception:
            pass

    def _account_items(self, snapshot: UiSnapshot) -> list[rumps.MenuItem]:
        """The claude-swap accounts section, or ``[]`` when there is none.

        Empty only in the one case that is not a failure: no claude-swap
        accounts *and* another vendor did report a quota. A Codex-only machine
        then shows no Accounts heading at all rather than an empty one
        (SPEC-CODEX 5.5), while a Claude machine whose backend broke keeps
        today's ``none found`` line plus its ``!`` diagnosis.
        """
        if not snapshot.accounts and self._visible_quota_rows(snapshot):
            return []
        items: list[rumps.MenuItem] = [_info("Accounts")]
        if not snapshot.accounts:
            items.append(_info("  " + ("loading…" if snapshot.accounts_at == 0 else "none found")))
            return items
        width = max((len(_display_name(row)) for row in snapshot.accounts), default=8)
        # One vertical edge for the whole menu: the widest window name and the
        # widest (!)-adjusted gap across EVERY block, accounts and quota alike,
        # not per block.
        label_width = _window_label_width(snapshot.accounts, self._visible_quota_rows(snapshot))
        for row in snapshot.accounts:
            # Plain label first: it is what shows if attributed rendering is
            # unavailable, and it is what VoiceOver reads.
            label = "  " + _account_row_label(row, name_width=min(width, 16))
            item = rumps.MenuItem(
                label,
                # `switchable` gates the click, not the vendor: a read-only row
                # must never acquire a switch callback (contract, AccountRow).
                callback=(
                    None
                    if row.is_active or not getattr(row, "switchable", True)
                    else self._make_switch_callback(row)
                ),
            )
            self._decorate_account_item(item, row, label_width=label_width)
            _check(item, row.is_active)
            items.append(item)
        return items

    def _visible_quota_rows(self, snapshot: UiSnapshot) -> tuple[AccountRow, ...]:
        """Pseudo-accounts worth drawing (SPEC-CODEX 5.1, 5.5).

        Dropped: a vendor the user switched off, and a row that reports no
        percentage at all. The second is the "no data" half of requirement 3 -
        a source that exists but has learned nothing yet renders **nothing**,
        not a heading over an empty bar.
        """
        if not snapshot.settings.get("codex_tracking_enabled", True):
            rows = tuple(row for row in snapshot.quota_rows if row.vendor == VENDOR_CLAUDE)
        else:
            rows = tuple(snapshot.quota_rows)
        return tuple(row for row in rows if _quota_windows(row))

    def _quota_items(self, snapshot: UiSnapshot) -> list[rumps.MenuItem]:
        """The Codex (and any future read-only vendor) quota section.

        One item per vendor carrying a heading plus one bar line per reported
        window - the same object shape as an account block, minus the callback.
        Absent vendor, absent section: no heading, no placeholder, no error
        (SPEC-CODEX 5.5).
        """
        rows = self._visible_quota_rows(snapshot)
        if not rows:
            return []
        label_width = _window_label_width(snapshot.accounts, rows)
        items: list[rumps.MenuItem] = []
        for row in rows:
            # `callback=None` is what makes AppKit render it disabled, which is
            # the requirement: a Codex quota must not look clickable, because
            # there is nothing to switch to.
            item = rumps.MenuItem("  " + _quota_row_label(row), callback=None)
            self._decorate_quota_item(item, row, label_width=label_width)
            items.append(item)
        return items

    def _decorate_quota_item(
        self, item: rumps.MenuItem, row: AccountRow, *, label_width: int = 6
    ) -> None:
        """Upgrade a quota row to the same bar block the accounts use.

        Deliberately built from :func:`render.window_line` - the identical bar
        geometry, the identical 70/90 severity palette and the identical
        ``(!)`` marker as a Claude window. A second bar renderer here would be
        two things to keep in sync and one of them would eventually be wrong.
        """
        try:
            segments = render.quota_header(
                row.alias or vendor_label(row.vendor),
                plan=row.plan_type or "",
                note=(
                    f"{_age_label(row.usage_age_seconds)} old" if row.usage_is_stale else ""
                ),
            )
            pace = dict(getattr(row, "pace_ahead", ()) or ())
            for label, pct, note in _quota_windows(row):
                segments.append(("\n", None))
                segments.extend(
                    render.window_line(
                        label,
                        pct,
                        note,
                        label_width=label_width,
                        note_column=5,
                        ahead=pace.get(label),
                    )
                )
            render.apply_attributed(item, segments)
        except Exception:
            pass  # plain label stands

    def _decorate_account_item(
        self, item: rumps.MenuItem, row: AccountRow, *, label_width: int = 5
    ) -> None:
        """Upgrade one account row to a multi-line bar block (`cswap watch` look).

        One NSMenuItem carrying a 4-line attributed title, rather than four
        items: fewer objects, and clicking anywhere in the block switches to
        that account. Falls back silently to the plain label already set.
        """
        try:
            segments = render.account_header(
                row.slot,
                _display_name(row),
                row.email,
                row.is_active,
                age_note=(
                    f"{_age_label(row.usage_age_seconds)} old" if row.usage_is_stale else ""
                ),
            )
            windows: list[tuple[str, float | None, str]] = [
                ("5h", row.five_hour_pct, _reset_note(row, "five_hour")),
                ("7d", row.seven_day_pct, _reset_note(row, "seven_day")),
            ]
            for name, pct in row.scoped_windows:
                windows.append((name, pct, _reset_note(row, name)))
            # 5 == len("  (!)"): reserve the marker's width on every row so the
            # reset notes form one column whether or not a window is exhausted.
            pace = dict(getattr(row, "pace_ahead", ()) or ())
            for name, pct, note in windows:
                segments.append(("\n", None))
                segments.extend(
                    render.window_line(
                        name,
                        pct,
                        note,
                        label_width=label_width,
                        note_column=5,
                        # Keyed "five_hour"/"seven_day"/scoped-name; the 5h row
                        # never has a verdict, so it never gets a note.
                        ahead=pace.get(
                            {"5h": "five_hour", "7d": "seven_day"}.get(name, name)
                        ),
                    )
                )
            render.apply_attributed(item, segments)
        except Exception:
            pass  # plain label stands

    def _cost_items(self, snapshot: UiSnapshot) -> list[rumps.MenuItem]:
        """The Cost section. Always carries the notional label (SPEC 4.3)."""
        header = _info(f"Cost ({NOTIONAL_LABEL})")
        if not snapshot.settings.get("cost_tracking_enabled", True):
            return [header, _info("  tracking is off")]
        if not self._worker.cost_available:
            return [header, _info("  unavailable — cost modules are not wired")]

        cost = snapshot.cost
        progress = cost.progress if cost is not None else snapshot.progress
        if cost is None or cost.is_partial:
            # Honesty rule: no dollar figures until the first index finishes.
            rows = [_info(_cost_row_label(label, "indexing…")) for label in ("Today", "Last 7d", "Last 30d")]
            return [header, *rows, _info(f"  {_progress_label(progress) or 'indexing…'}")]

        items = [
            header,
            _info(_cost_row_label(cost.today.label or "Today", format_usd(cost.today.usd))),
            _info(
                _cost_row_label(
                    cost.last_7d.label or "Last 7d",
                    format_usd(cost.last_7d.usd),
                    extra=f"({format_usd(cost.last_7d_avg_per_day)}/day avg)",
                )
            ),
            _info(_cost_row_label(cost.last_30d.label or "Last 30d", format_usd(cost.last_30d.usd))),
        ]
        items.extend(self._model_items(cost))
        if cost.unknown_models:
            # SPEC 3.3 trap 5: show the actual unrecognised name, priced at $0.
            names = ", ".join(cost.unknown_models[:3])
            if len(cost.unknown_models) > 3:
                names = f"{names}, +{len(cost.unknown_models) - 3} more"
            items.append(_info(f"  unpriced model(s) at $0: {names}"))
        return items

    def _model_items(self, cost: CostBreakdown) -> list[rumps.MenuItem]:
        """Today's per-model rows, grouped by vendor when there is more than one.

        With one vendor this is byte-for-byte the pre-Codex section: a single
        ``── by model ──`` rule and the rows under it. With two, the rule
        becomes one rule *per vendor* carrying that vendor's subtotal, which is
        how ``Opus 5`` and ``gpt-5.6-sol`` become attributable at a glance
        (SPEC-CODEX 5.2) for the cost of one extra line rather than a wider tag
        on every row - the dropdown is already dense.

        The window totals above are untouched and still span both vendors: the
        groups are a breakdown *of* today's figure, never a replacement for it.
        """
        if not cost.by_model:
            return [_info("  no usage today")]
        vendors = cost.vendors
        if len(vendors) <= 1:
            items = [_info("  ── by model ──────────")]
            items.extend(
                _info(_model_row_label(row.display_name, row.total_tokens, row.usd))
                for row in cost.by_model
            )
            return items
        items = []
        for vendor in vendors:
            rows = cost.rows_for_vendor(vendor)
            if not rows:
                continue
            group = cost.vendor_row(vendor)
            subtotal = group.usd if group is not None else sum(row.usd for row in rows)
            items.append(_info(_vendor_group_label(vendor_label(vendor), subtotal)))
            items.extend(
                _info(_model_row_label(row.display_name, row.total_tokens, row.usd))
                for row in rows
            )
        return items

    def _problem_items(self, snapshot: UiSnapshot) -> list[rumps.MenuItem]:
        """Background failures as menu lines - never a crash, never silence."""
        items: list[rumps.MenuItem] = []
        for text in snapshot.wiring_errors:
            items.append(_info(f"! {text[:MAX_ERROR_CHARS]}"))
        # A vendor module that is *absent* is silent (a Claude-only install is
        # not broken); one that imported and then failed to yield a source is
        # a broken feature and says so.
        for text in self._worker.source_errors:
            items.append(_info(f"! {text[:MAX_ERROR_CHARS]}"))
        if snapshot.accounts_error:
            items.append(_info(f"! accounts: {snapshot.accounts_error}"))
        if snapshot.cost_error:
            items.append(_info(f"! cost: {snapshot.cost_error}"))
        return items

    def _switch_account_submenu(self, snapshot: UiSnapshot) -> rumps.MenuItem:
        children: list[Any] = []
        for row in snapshot.accounts:
            # Pseudo-accounts live in `quota_rows` and never reach here; the
            # `switchable` test is the belt to that structural braces.
            if row.is_active or not getattr(row, "switchable", True):
                continue
            children.append(
                rumps.MenuItem(
                    f"{row.slot} {_display_name(row)}   5h {format_pct(row.five_hour_pct)}",
                    callback=self._make_switch_callback(row),
                )
            )
        if not children:
            children.append(_info("No other accounts"))
        return _submenu("Switch account", children)

    def _settings_submenu(self, snapshot: UiSnapshot) -> rumps.MenuItem:
        settings = snapshot.settings
        # A vendor with nothing to show contributes no settings either: a
        # Claude-only machine sees the pre-Codex Settings menu exactly.
        # Availability, NOT existence of the source object - `__main__.build()`
        # constructs a CodexIndexer unconditionally, so `extra_sources` is
        # non-empty even where there is no ~/.codex, and the machine that has
        # no Codex was being offered a "Codex tracking" switch, a "Codex weekly
        # percentage" title toggle and a diagnostics path that does not exist.
        has_vendors = bool(snapshot.quota_rows) or bool(self._worker.available_vendors)
        title_children = [
            _check(
                rumps.MenuItem(label, callback=self._make_setting_toggle(key)),
                bool(settings.get(key, SETTINGS_DEFAULTS.get(key, True))),
            )
            for key, label in _TITLE_TOGGLES
            if has_vendors or key != "title_show_codex_pct"
        ]
        children: list[Any] = [
            _submenu("Title", title_children),
            None,
            _submenu(
                f"Lookback: {settings['lookback_days']} days",
                [
                    _check(
                        rumps.MenuItem(
                            f"{days} days", callback=self._make_setting_value("lookback_days", days)
                        ),
                        settings["lookback_days"] == days,
                    )
                    for days in _LOOKBACK_CHOICES
                ],
            ),
            _submenu(
                f"Account refresh: {_duration_label(int(settings['ui_interval_seconds']))}",
                [
                    _check(
                        rumps.MenuItem(
                            _duration_label(secs),
                            callback=self._make_setting_value("ui_interval_seconds", secs),
                        ),
                        int(settings["ui_interval_seconds"]) == secs,
                    )
                    for secs in _UI_INTERVAL_CHOICES
                ],
            ),
            _submenu(
                f"Cost scan: {_duration_label(int(settings['cost_interval_seconds']))}",
                [
                    _check(
                        rumps.MenuItem(
                            _duration_label(secs),
                            callback=self._make_setting_value("cost_interval_seconds", secs),
                        ),
                        int(settings["cost_interval_seconds"]) == secs,
                    )
                    for secs in _COST_INTERVAL_CHOICES
                ],
            ),
            None,
        ]
        if has_vendors:
            codex_on = bool(settings.get("codex_tracking_enabled", True))
            children.insert(
                1,
                _check(
                    rumps.MenuItem(
                        self._switch_label(f"{vendor_label(VENDOR_CODEX)} tracking", codex_on),
                        callback=self._make_setting_toggle("codex_tracking_enabled"),
                    ),
                    codex_on,
                ),
            )
        if self._worker.supports_index_rebuild():
            children.append(rumps.MenuItem("Rebuild cost index", callback=self._on_rebuild_index))
        children.append(rumps.MenuItem("Reveal settings.json", callback=self._on_reveal_settings))
        children.append(None)
        children.extend(self._diagnostic_items(snapshot))
        return _submenu("Settings", children)

    def _diagnostic_items(self, snapshot: UiSnapshot) -> list[rumps.MenuItem]:
        """Evidence for the SPEC 2.1 budget, visible without a debugger."""
        items = [_info(f"Last scan: {snapshot.scan_note}" if snapshot.scan_note else "Last scan: —")]
        if snapshot.accounts_at:
            items.append(_info(f"Accounts read: {_age_label(time.time() - snapshot.accounts_at)} ago"))
        if snapshot.cost_at:
            items.append(_info(f"Cost read: {_age_label(time.time() - snapshot.cost_at)} ago"))
        items.append(_info(f"Transcripts: {PROJECTS_DIR}"))
        # Extra corpora, named by their own source rather than by a constant,
        # so the line cannot claim a root the source is not actually reading.
        for label, root in self._worker.source_roots():
            items.append(_info(f"{label}: {root}"))
        items.append(_info(f"State: {SCAN_STATE_PATH.parent}"))
        return items

    # -- callbacks (main thread; they only enqueue work) -------------------

    def _optimistic(self, **changes: Any) -> None:
        """Repaint immediately with an assumed result.

        The worker publishes the truth a moment later - including on failure,
        where it republishes the real value - so an optimistic label can never
        stick around as a lie.
        """
        with self._lock:
            self._snapshot = replace(self._snapshot, **changes)
            snapshot = self._snapshot
            self._dirty = False
        self.rebuild_menu(snapshot)

    def _on_toggle_autoswitch(self, _sender: Any) -> None:
        target = not bool(self.snapshot().autoswitch_enabled)
        self._optimistic(autoswitch_enabled=target)
        self._worker.submit(_CMD_SET_AUTOSWITCH, target)

    def _on_toggle_cost_tracking(self, _sender: Any) -> None:
        snapshot = self.snapshot()
        target = not bool(snapshot.settings.get("cost_tracking_enabled", True))
        settings = normalize_settings({**snapshot.settings, "cost_tracking_enabled": target})
        self._optimistic(settings=settings, cost=None if not target else snapshot.cost)
        self._worker.submit(_CMD_SET_SETTING, ("cost_tracking_enabled", target))

    def _make_setting_toggle(self, key: str) -> Callable[[Any], None]:
        def callback(_sender: Any) -> None:
            snapshot = self.snapshot()
            target = not bool(snapshot.settings.get(key, True))
            self._optimistic(settings=normalize_settings({**snapshot.settings, key: target}))
            self._worker.submit(_CMD_SET_SETTING, (key, target))

        return callback

    def _make_setting_value(self, key: str, value: Any) -> Callable[[Any], None]:
        def callback(_sender: Any) -> None:
            snapshot = self.snapshot()
            self._optimistic(settings=normalize_settings({**snapshot.settings, key: value}))
            self._worker.submit(_CMD_SET_SETTING, (key, value))

        return callback

    def _make_switch_callback(self, row: AccountRow) -> Callable[[Any], None]:
        target = row.alias or str(row.slot)

        def callback(_sender: Any) -> None:
            self._worker.submit(_CMD_SWITCH_TO, target)

        return callback

    def _on_refresh_now(self, _sender: Any) -> None:
        self._worker.submit(_CMD_REFRESH, None)

    def _on_rebuild_index(self, _sender: Any) -> None:
        self._worker.submit(_CMD_REBUILD_INDEX, None)

    def _on_reveal_settings(self, _sender: Any) -> None:
        """Reveal settings.json in Finder. Hands off to NSWorkspace, so the
        main thread is not doing the work."""
        try:
            import AppKit

            AppKit.NSWorkspace.sharedWorkspace().selectFile_inFileViewerRootedAtPath_(
                str(SETTINGS_PATH), str(SETTINGS_PATH.parent)
            )
        except Exception as exc:
            _log(f"could not reveal {SETTINGS_PATH}: {exc!r}")

    def _on_quit(self, _sender: Any) -> None:
        self.shutdown()


# Aliases: ``__main__`` is written separately, so accept the obvious names.
UsageWidgetApp = CCUsageWidgetApp
App = CCUsageWidgetApp


# ---------------------------------------------------------------------------
# Best-effort wiring, for a bare ``python -m cc_usage_widget``
# ---------------------------------------------------------------------------

_CANDIDATES: dict[str, tuple[str, ...]] = {
    "pricing": (
        "default_pricing", "load_pricing", "build_pricing", "pricing_table", "default_table",
        "PricingTable", "PriceTable", "Pricing", "ModelPricing", "ModelPriceTable",
        "DEFAULT_PRICING", "PRICING", "PRICE_TABLE", "DEFAULT_TABLE",
    ),
    "rollups": (
        "RollupStore", "DailyRollupStore", "DailyRollups", "RollupFile", "Rollups", "Store",
        "default_store", "load_store", "open_store",
    ),
    "indexer": (
        "TranscriptIndexer", "IncrementalIndexer", "Indexer", "TranscriptScanner", "Scanner",
        "default_indexer", "build_indexer",
    ),
    "accounts": (
        "AccountSource", "ClaudeSwapAccounts", "ClaudeSwapAccountSource", "AccountsAdapter",
        "Accounts", "SwapAccounts", "default_source", "build_source",
    ),
    "codex": (
        "CodexSource", "CodexIndexer", "CodexTranscriptSource", "CodexTranscriptIndexer",
        "CodexScanner", "Codex", "default_source", "build_source", "default_codex_source",
        "build_codex_source",
    ),
}

_CODEX_MODULES = ("codex_indexer", "codex_source", "codex")
"""Where the Codex source might live, most-likely first (SPEC-CODEX 4 names
``codex_indexer.py``). Tried in order; the first module that imports wins."""


def _build_extra_sources(
    settings: dict[str, Any], pricing: Any = None
) -> tuple[TranscriptSource, ...]:
    """Best-effort construction of the non-Claude transcript sources.

    **Worker thread only** - it imports a module and constructs a scanner,
    which stats a corpus root.

    Absence is silence, by design and by requirement: on a machine with no
    Codex support built in (no ``codex_indexer.py``), or with the vendor
    switched off, this returns ``()`` and the widget renders precisely the menu
    it rendered before Codex existed. A module that *is* present but yields
    nothing usable raises, so :meth:`BackgroundWorker._wire_sources` can put a
    ``!`` line in the menu - that is a broken feature, not an absent one
    (Rule 12).
    """
    if not settings.get("codex_tracking_enabled", True):
        return ()
    module = None
    for name in _CODEX_MODULES:
        try:
            module = __import__(f"{__package__}.{name}", fromlist=["*"])
            break
        except ModuleNotFoundError as exc:
            # Only *this* module being absent is normal. A module that exists
            # and fails on its own missing import is a real failure and must
            # not be swallowed as "Codex support is not built in".
            if exc.name and exc.name.split(".")[-1] != name:
                raise
            continue
    if module is None:
        return ()
    lookback = int(settings.get("lookback_days", SETTINGS_DEFAULTS["lookback_days"]))
    pool: dict[str, Any] = {
        "root": CODEX_SESSIONS_DIR,
        "sessions_dir": CODEX_SESSIONS_DIR,
        "sessions_root": CODEX_SESSIONS_DIR,
        "codex_dir": CODEX_SESSIONS_DIR,
        "projects_dir": CODEX_SESSIONS_DIR,
        "path": CODEX_SCAN_STATE_PATH,
        "state_path": CODEX_SCAN_STATE_PATH,
        "scan_state_path": CODEX_SCAN_STATE_PATH,
        "lookback_days": lookback,
        "keep_days": lookback,
        "pricing": pricing,
        "settings": settings,
    }
    source = _resolve(module, TranscriptSource, "codex", pool)
    if source is None:
        raise RuntimeError(
            f"no TranscriptSource implementation found in {module.__name__}"
        )
    return (source,)


class _Lazy:
    """A pool value built only if some constructor actually asks for it."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._value: Any = None
        self._built = False

    def get(self) -> Any:
        if not self._built:
            self._value = self._factory()
            self._built = True
        return self._value


def _construct(target: Any, pool: dict[str, Any]) -> Any:
    """Instantiate *target*, filling parameters by name from *pool*.

    Returns ``None`` when a required parameter cannot be satisfied, which is
    how a wrong candidate is rejected without guessing at positional order.
    """
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target()
    kwargs: dict[str, Any] = {}
    for param in signature.parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.name in pool:
            if param.kind is param.POSITIONAL_ONLY:
                if param.default is param.empty:
                    return None
                continue
            value = pool[param.name]
            kwargs[param.name] = value.get() if isinstance(value, _Lazy) else value
        elif param.default is param.empty:
            return None
    return target(**kwargs)


def _resolve(module: Any, protocol: type, kind: str, pool: dict[str, Any]) -> Any:
    """Find something in *module* satisfying *protocol*.

    Preferred names first, then anything else public in the module. Protocol
    conformance is checked on the *instance*, because a class object also
    "has" the methods and would pass an ``isinstance`` check by accident.
    """
    names = [name for name in _CANDIDATES[kind] if hasattr(module, name)]
    names += [name for name in vars(module) if not name.startswith("_") and name not in names]
    for name in names:
        obj = getattr(module, name, None)
        if obj is None:
            continue
        if not isinstance(obj, type) and not callable(obj):
            try:
                if isinstance(obj, protocol):
                    return obj
            except TypeError:
                pass
            continue
        if getattr(obj, "__module__", None) != module.__name__:
            continue  # re-exported from elsewhere (often the protocol itself)
        try:
            instance = _construct(obj, pool)
        except Exception:
            continue
        if instance is None:
            continue
        try:
            if isinstance(instance, protocol):
                return instance
        except TypeError:
            continue
    return None


def _load_settings() -> tuple[dict[str, Any], Callable[[dict[str, Any]], None] | None, list[str]]:
    """Settings dict + a persist callable, preferring ``state.py``."""
    errors: list[str] = []
    settings: dict[str, Any] | None = None
    persist: Callable[[dict[str, Any]], None] | None = None
    try:
        from . import state  # type: ignore[attr-defined]
    except Exception as exc:
        errors.append(f"state module unavailable: {type(exc).__name__}: {exc}")
        state = None  # type: ignore[assignment]
    if state is not None:
        for name in ("load_settings", "read_settings", "load"):
            loader = getattr(state, name, None)
            if callable(loader):
                try:
                    settings = normalize_settings(loader())
                    break
                except Exception as exc:
                    errors.append(f"{name}() failed: {type(exc).__name__}: {exc}")
        for name in ("save_settings", "write_settings", "store_settings", "save"):
            saver = getattr(state, name, None)
            if callable(saver):
                persist = saver
                break
    if settings is None:
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = None
        except Exception as exc:
            errors.append(f"settings.json unreadable: {type(exc).__name__}: {exc}")
            raw = None
        settings = normalize_settings(raw)
    return settings, persist, errors


def build_app() -> CCUsageWidgetApp:
    """Construct the app with real collaborators, degrading rather than dying.

    ``__main__`` may instead build the four seams itself and pass them to
    :class:`CCUsageWidgetApp` - that is the explicit path and it wins. This
    function is the fallback for a bare ``python -m cc_usage_widget``: it
    resolves each seam from its sibling module by protocol conformance, and any
    seam it cannot wire becomes a ``!`` menu line instead of a traceback, so
    the widget still shows whatever it does have.
    """
    settings, persist, errors = _load_settings()
    lookback = int(settings["lookback_days"])
    pricing = rollups = indexer = accounts = None

    def _try(kind: str, module_name: str, protocol: type, pool: dict[str, Any]) -> Any:
        try:
            module = __import__(f"{__package__}.{module_name}", fromlist=["*"])
        except Exception as exc:
            errors.append(f"{module_name}.py unavailable: {type(exc).__name__}: {exc}")
            return None
        try:
            resolved = _resolve(module, protocol, kind, pool)
        except Exception as exc:
            errors.append(f"{module_name}.py wiring failed: {type(exc).__name__}: {exc}")
            return None
        if resolved is None:
            errors.append(f"no {protocol.__name__} implementation found in {module_name}.py")
        return resolved

    pricing = _try("pricing", "pricing", PricingTable, {})
    rollups = _try(
        "rollups",
        "rollup",
        RollupStore,
        {
            "pricing": pricing, "pricing_table": pricing,
            "path": ROLLUPS_PATH, "rollups_path": ROLLUPS_PATH, "state_path": ROLLUPS_PATH,
            "lookback_days": lookback, "keep_days": lookback, "settings": settings,
        },
    )
    indexer = _try(
        "indexer",
        "indexer",
        TranscriptIndexer,
        {
            "rollups": rollups, "rollup_store": rollups, "store": rollups, "rollup": rollups,
            "pricing": pricing, "settings": settings,
            "lookback_days": lookback, "keep_days": lookback,
            "projects_dir": PROJECTS_DIR, "projects_root": PROJECTS_DIR, "root": PROJECTS_DIR,
            "path": SCAN_STATE_PATH, "state_path": SCAN_STATE_PATH,
            "scan_state_path": SCAN_STATE_PATH,
        },
    )
    # No ``switcher`` in the pool on purpose: the adapter imports and builds
    # claude-swap lazily, on the background thread. Handing it a switcher here
    # would put a Keychain read on the AppKit thread at launch (SPEC 2.3) and
    # would make a transient claude-swap failure look like "no AccountSource
    # implementation found" instead of a reported, retried degradation.
    accounts = _try("accounts", "accounts", AccountSource, {"settings": settings})
    for text in errors:
        _log(text)
    return CCUsageWidgetApp(
        accounts=accounts,
        indexer=indexer,
        rollups=rollups,
        pricing=pricing,
        settings=settings,
        persist_settings=persist,
        wiring_errors=tuple(errors),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point helper. ``__main__.py`` may call this or wire its own app."""
    build_app().run()
    return 0
