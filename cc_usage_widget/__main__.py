"""Entry point: ``$PY -m cc_usage_widget`` (SPEC 5).

What this module owns, and nothing else:

* **Explicit wiring.** :func:`build` constructs the five collaborators and
  hands them to :class:`~cc_usage_widget.app.CCUsageWidgetApp`. ``app.py``
  documents this as the path that wins over its own best-effort
  ``build_app()`` autowirer, and it is what lets us give
  ``state.ScanStateStore`` to the indexer and share one
  ``state.SettingsStore`` with ``accounts.py`` (whose autoswitch toggle
  mirrors itself back into it).
* **Degrade, never die.** A seam that cannot be built becomes a ``!`` menu
  line, so a broken ``claude_swap`` install still shows cost and a broken
  transcript root still shows accounts (Rule 12: the failure is visible, not
  swallowed).
* **Signals.** SIGINT/SIGTERM route into the same
  :meth:`~cc_usage_widget.app.CCUsageWidgetApp.shutdown` the Quit item uses,
  so ``launchctl kill`` / Ctrl-C flush and exit like a click rather than
  leaving a half-written cache behind.

Threading (SPEC 2.3): nothing here does I/O after :func:`build` returns. The
transcript walk, the JSON parsing and every save happen on the worker thread
``app.run()`` starts; the first-run index is kicked off there via
:meth:`~cc_usage_widget.app.CCUsageWidgetApp.kickoff` before the AppKit loop
paints its first menu.
"""

from __future__ import annotations

import atexit
import copy
import fcntl
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import accounts as accounts_mod
from . import app as app_mod
from . import codex_indexer as codex_mod
from . import indexer as indexer_mod
from . import pricing as pricing_mod
from . import rollup as rollup_mod
from . import state as state_mod
from .app import CCUsageWidgetApp
from .contracts import (
    CODEX_ACCOUNTS_DIR,
    CODEX_ACCOUNTS_REGISTRY_PATH,
    CODEX_QUOTA_SNAPSHOTS_PATH,
    CODEX_SCAN_STATE_PATH,
    CODEX_SESSIONS_DIR,
    PROJECTS_DIR,
    ROLLUPS_PATH,
    SCAN_STATE_PATH,
    SETTINGS_DEFAULTS,
    SETTINGS_PATH,
    WIDGET_HOME,
)

__all__ = ["build", "main"]

LOCK_PATH = WIDGET_HOME / "widget.lock"
"""Single-instance lock. This process owns three mutable JSON caches and drives
the shared autoswitch engine, so a second copy is data loss, not a duplicate
icon: both load ``scan_state.json`` once and then each rewrites the whole file
and the whole ``rollups.json`` with ``os.replace``. Last writer wins, so one
instance's advanced offsets can land while only the other's rollup survives -
tokens counted by the first are then permanently missing."""

_RIVAL_SUBCOMMANDS = ("auto", "menubar", "tui", "watch")
"""claude-swap entry points that can change the active account behind our back.

``auto``/``menubar`` host an ``AutoSwitchEngine`` outright. ``tui`` (and a bare
``cswap``, which *is* the TUI) switches on a keypress and can start its own
engine from the account view, and ``watch`` polls. Everything else the CLI
offers - ``list``, ``status``, ``config``, a one-shot ``switch`` - either does
not switch or exits immediately, so it is not a standing rival.

Each of these also has a ``--flag`` spelling: ``claude_swap.cli``'s
``_SUBCOMMAND_FLAGS`` rewrites ``tui``/``watch``/``menubar`` into ``--tui``/
``--watch``/``--menubar``, which its own docstring calls the established
interface - so a LaunchAgent or alias written against it runs ``cswap
--menubar``, a real second engine. (``auto`` has no flag spelling upstream; the
pattern's ``(--)?`` covers it uniformly, and a ``cswap --auto`` would exit on
the unknown flag rather than stand around to be matched.)"""

_RIVAL_PATTERN = (
    r"(^|/)cswap([[:space:]]*$"
    r"|[[:space:]]+(--)?(auto|menubar|tui|watch)([[:space:]]|$))"
)
"""Actors that would share ``autoswitch_state.json`` (and the active login)
with us. SPEC 5 says to quit ``cswap menubar`` first; nothing enforced it, and
until 2026-09-01 nothing even saw the bare TUI (pid 15206, up since 19:22,
switching by keypress while our engine switched back).

Anchored on the argv *tail*, because a ``pgrep -fl`` line is
``<pid> <python> <dir>/cswap [args]``: ``(^|/)`` pins the match to the
executable's own name so ``/tmp/cswap.log`` or ``grep cswap`` cannot fire it,
and the trailing ``([[:space:]]|$)`` keeps ``cswap autoswitch`` from matching
``auto`` and ``cswap --watch-foo`` from matching ``--watch``. The bare-TUI arm
is ``[[:space:]]*$`` - argv ends at ``cswap``.

**This is a pre-filter, not the decision.** An ERE cannot say "in argv[0]
position", so the bare-TUI arm also fires on any command line that merely *ends*
in a path named ``cswap`` - ``tail -f ~/logs/cswap``, ``vim ~/notes/cswap``,
``cat ~/.local/bin/cswap``. Reporting one of those would be a fabricated ``!``
row telling the operator to quit a process that cannot switch anything, so
:func:`_rival_actor` re-checks the position and :func:`_detect_rival_engines`
drops what it rejects.

A **POSIX** character class, not ``\\s``: macOS ``pgrep`` compiles its pattern as
POSIX extended regex, where ``\\s`` is not a shorthand and the match silently
never fires. Verified against a live ``cswap menubar`` and a live bare TUI."""

_lock_handle: Any = None
"""Module-global so the ``flock`` lives as long as the process."""

USAGE = """usage: python -m cc_usage_widget [--dry-run] [--help]

  (no flags)  run the menu-bar widget in the foreground
  --dry-run   wire everything, print the diagnostics, exit without entering
              the AppKit loop (no second icon in the menu bar)
"""


def _log(message: str) -> None:
    """Timestamped stderr line, same shape as ``app.py``'s log."""
    sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] cc-usage-widget: {message}\n")
    sys.stderr.flush()


_TMP_ORPHAN_RE = re.compile(
    r"^\.?[\w.-]+\.(?:json|html)(\.tmp\.\w+|\.\w+\.tmp(\.json)?|\.tmp)$"
)
"""Every atomic-write helper's temp name in the widget home, and nothing else.

One pattern for all of them (OPS-1, 2026-09-25): the old per-file glob
``<state>.tmp.*`` could never match ``scan_state_dedup.json.tmp.<pid>``
(indexer.py) - seven of those, Aug 25 .. Sep 23, survived every restart
carrying request ids at mode 644. The arms:

* ``.tmp.<x>``  - ``<name>.tmp.<pid>`` (indexer, codex_indexer), the dot-
  prefixed ``.audit_state.json.tmp.<pid>`` (audit) and ``mkstemp``'s
  ``<name>.tmp.<random>`` (codex_accounts);
* ``.<x>.tmp[.json]`` - ``mkstemp`` with ``prefix=".<name>."`` and
  ``suffix=".tmp"`` (rollup, state) or ``".tmp.json"`` (notify);
* ``.tmp`` - ``attribution.json.tmp`` and ``dashboard.html.tmp``.

A real state file ends in ``.json`` / ``.html`` and cannot match: every arm
needs a ``tmp`` component after the extension."""


def _pid_is_alive(pid: int) -> bool:
    """``kill(pid, 0)``: True unless the pid is certainly gone. A number
    ``pid_t`` cannot hold raises ``OverflowError`` in ``os.kill``; no process
    owns it, so it answers False."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _sweep_tmp_orphans(
    max_age_s: float = 86_400.0,
    home: Path = SCAN_STATE_PATH.parent,
    reprieve_max_age_s: float | None = None,
) -> None:
    """Unlink day-old atomic-write temp orphans in the widget *home*.

    Safe under the single-instance lock: no live writer can own a tmp file
    that old, and every writer creates a fresh name. Belt and braces for the
    ``<name>.tmp.<pid>`` shape: a suffix naming a pid that is still alive is
    left alone - but only while the file is younger than
    *reprieve_max_age_s* (default seven times *max_age_s*, a week). macOS
    reuses pids, and an unrelated long-lived agent that inherits the number
    would otherwise pin the orphan forever (R2-SEC-3: a 23-day-old
    ``scan_state_dedup.json.tmp.1068`` was held by TextInputMenuAgent).
    Past that age, age alone decides. Top level only:
    ``codex-accounts/<id>/`` holds credentials and is not ours to sweep.
    """
    if reprieve_max_age_s is None:
        reprieve_max_age_s = 7 * max_age_s
    try:
        now = time.time()
        for orphan in Path(home).iterdir():
            match = _TMP_ORPHAN_RE.match(orphan.name)
            if match is None:
                continue
            try:
                if not orphan.is_file() or orphan.is_symlink():
                    continue
                age = now - orphan.stat().st_mtime
                if age <= max_age_s:
                    continue
                suffix = (match.group(1) or "").rpartition(".")[2]
                if (
                    age < reprieve_max_age_s
                    and match.group(1).startswith(".tmp.")
                    and suffix.isascii()
                    and suffix.isdigit()
                    and _pid_is_alive(int(suffix))
                ):
                    continue
                orphan.unlink()
                _log(f"swept atomic-write orphan {orphan.name}")
            except (OSError, ValueError, OverflowError):
                continue
    except Exception as exc:  # pragma: no cover - housekeeping must not kill startup
        _log(f"tmp-orphan sweep failed: {exc}")


_MALLOC_NOISE = b"MallocStackLogging: can't turn off"
"""A line every child ``python`` prints at exit on this macOS, carrying no
information: 25,560 of them were 68% of widget.log on 2026-09-25."""


def _bound_widget_log(
    max_bytes: int = 5_000_000, log_path: Path | str | None = None
) -> None:
    """Truncate the launchd-appended log once it passes *max_bytes*.

    launchd appends to ``logs/widget.log`` forever and knows no rotation;
    keep the tail half so recent forensics survive the cut, minus the
    ``MallocStackLogging`` noise lines. Runs at launch and once per local day
    from the worker (OPS-7). Written IN PLACE, never renamed: launchd holds an
    ``O_APPEND`` fd on this inode, so a rename would leave it writing into the
    unlinked old file. *log_path* defaults to the installed widget's log.
    """
    try:
        path = (
            Path(log_path)
            if log_path is not None
            else SCAN_STATE_PATH.parent / "logs" / "widget.log"
        )
        size = path.stat().st_size
        if size <= max_bytes:
            return
        data = path.read_bytes()[-max_bytes // 2 :]
        nl = data.find(b"\n")
        if nl >= 0:
            data = data[nl + 1 :]
        data = b"".join(
            line for line in data.splitlines(keepends=True) if _MALLOC_NOISE not in line
        )
        path.write_bytes(data)
        _log(f"widget.log was {size} bytes — truncated to the recent tail")
    except FileNotFoundError:
        pass
    except Exception as exc:  # pragma: no cover - housekeeping must not kill startup
        _log(f"log bound failed: {exc}")


_RETRY_AFTER_RE = re.compile(r"(retry[-_ ]after\D{0,3})\d+", re.IGNORECASE)

_COLLAPSED_SUMMARY = "_cc_usage_widget_repeat_summary"
"""Marks a summary record the collapser itself emitted, so it passes its own
filter untouched on the way back through the handler."""


def _template(record: logging.LogRecord, message: str) -> logging.LogRecord:
    """What a later summary line is built from: the formatted message, with
    no args, traceback or stack - an hour-long window must not keep a
    traceback's frames alive, and a count line does not repeat one."""
    out = copy.copy(record)
    out.msg, out.args = message, None
    out.exc_info = out.exc_text = out.stack_info = None
    return out


def _daemon_timer(delay: float, callback: Callable[[], None]) -> None:
    timer = threading.Timer(max(0.0, delay), callback)
    timer.daemon = True
    timer.start()


class _RepeatCollapser(logging.Filter):
    """Collapse a WARNING/ERROR that repeats verbatim into one line + a count.

    OPS-5 (2026-09-25): 645 ``Usage fetch failed for account 6: http-403`` and
    153 ``http-429, retry-after 3600s`` lines were one incident, and they
    buried the switches, keychain timeouts and expiries around them. Keyed on
    ``(logger name, level, message)`` with retry-after digits normalised: the
    first record passes; repeats within *window_s* of the last emitted one are
    counted and dropped; the first repeat after the window is emitted with
    ``" (repeated N times since HH:MM)"`` and opens a new window. A different
    key is its own first. INFO and below always pass.

    A burst that STOPS inside its window still reports its count (R2-SEC-2:
    six 403s in fifty minutes used to leave one line and no count): once
    :meth:`install` has given it the handler, the same summary line is
    emitted when the window closes (a daemon timer), on the next record of
    any key that finds the window already closed, and at :meth:`flush`
    (process exit, SIGTERM) for a window still open.

    Attached to the ``basicConfig`` handler only (a handler filter that
    returns a copy, Python 3.12+), so other handlers - claude-swap's own file
    handler - still see every record unchanged; the widget's own ``_log``
    writes stderr directly and never reaches it.
    """

    def __init__(
        self,
        window_s: float = 3600.0,
        clock: Callable[[], float] = time.time,
        schedule: Callable[[float, Callable[[], None]], None] | None = None,
    ) -> None:
        super().__init__()
        self._window_s = float(window_s)
        self._clock = clock
        self._schedule = _daemon_timer if schedule is None else schedule
        # key -> [emitted_at, repeats, a stripped copy of the emitted record]
        self._seen: dict[tuple[str, int, str], list[Any]] = {}
        self._lock = threading.Lock()
        self._sink: Callable[[logging.LogRecord], Any] | None = None
        self._timer_armed = False

    def install(self, handler: logging.Handler) -> None:
        """Filter *handler*'s records, and emit burst summaries through it."""
        handler.addFilter(self)
        self._sink = handler.handle

    def filter(self, record: logging.LogRecord) -> bool | logging.LogRecord:
        if getattr(record, _COLLAPSED_SUMMARY, False):
            return True
        key = None
        message = ""
        if record.levelno >= logging.WARNING:
            try:
                message = record.getMessage()
                key = (record.name, record.levelno, _RETRY_AFTER_RE.sub(r"\1#", message))
            except Exception:
                key = None
        now = self._clock()
        self._emit(self._take(now, expired_only=True, skip=key))
        if key is None:
            return True
        arm = False
        with self._lock:
            entry = self._seen.get(key)
            if entry is None:
                overflow = self._prune(now) if len(self._seen) >= 512 else []
                self._seen[key] = [now, 0, _template(record, message)]
                result: bool | logging.LogRecord = True
            else:
                overflow = []
                emitted_at, repeats, _kept = entry
                if now - emitted_at < self._window_s:
                    entry[1] = repeats + 1
                    arm = not self._timer_armed and self._sink is not None
                    self._timer_armed = self._timer_armed or arm
                    result = False
                else:
                    self._seen[key] = [now, 0, _template(record, message)]
                    result = (
                        self._summary(record, message, repeats, emitted_at, now)
                        if repeats
                        else True
                    )
        self._emit(overflow)
        if arm:
            self._arm(now)
        return result

    def flush(self) -> None:
        """Emit every pending count now, open window or not (exit paths)."""
        self._emit(self._take(self._clock(), expired_only=False, skip=None))

    # -- internals ---------------------------------------------------------

    def _summary(
        self,
        template: logging.LogRecord,
        message: str,
        repeats: int,
        emitted_at: float,
        now: float,
    ) -> logging.LogRecord:
        since = time.strftime("%H:%M", time.localtime(emitted_at))
        out = copy.copy(template)
        out.msg = f"{message} (repeated {int(repeats)} times since {since})"
        out.args = None
        out.created = now
        out.msecs = (now - int(now)) * 1000.0
        return out

    def _take(
        self, now: float, *, expired_only: bool, skip: tuple[str, int, str] | None
    ) -> list[logging.LogRecord]:
        """Remove and return summaries for pending counts (the lock inside)."""
        out: list[logging.LogRecord] = []
        with self._lock:
            for k, (emitted_at, repeats, template) in list(self._seen.items()):
                if k == skip or not repeats:
                    continue
                if expired_only and now - emitted_at < self._window_s:
                    continue
                del self._seen[k]
                try:
                    message = template.getMessage()
                except Exception:  # pragma: no cover - it formatted once already
                    continue
                summary = self._summary(template, message, repeats, emitted_at, now)
                setattr(summary, _COLLAPSED_SUMMARY, True)
                out.append(summary)
        return out

    def _emit(self, summaries: list[logging.LogRecord]) -> None:
        sink = self._sink
        if sink is None:
            return
        for summary in summaries:
            try:
                sink(summary)
            except Exception:  # pragma: no cover - logging must not raise
                pass

    def _arm(self, now: float) -> None:
        """One timer at a time, due when the earliest pending window closes."""
        with self._lock:
            pending = [at for at, repeats, _t in self._seen.values() if repeats]
        if not pending:
            with self._lock:
                self._timer_armed = False
            return
        delay = min(pending) + self._window_s - now
        try:
            self._schedule(max(0.0, delay), self._on_timer)
        except Exception:  # pragma: no cover - no timer: next record / flush
            with self._lock:
                self._timer_armed = False

    def _on_timer(self) -> None:
        now = self._clock()
        self._emit(self._take(now, expired_only=True, skip=None))
        with self._lock:
            rearm = any(repeats for _at, repeats, _t in self._seen.values())
            self._timer_armed = rearm
        if rearm:
            self._arm(now)

    def _prune(self, now: float) -> list[logging.LogRecord]:
        """Caller holds the lock. Drops closed windows (their counts were
        taken on the way in); at the cap, clears the rest and returns their
        pending counts as summaries rather than losing them."""
        stale = [k for k, (at, _n, _t) in self._seen.items() if now - at >= self._window_s]
        for k in stale:
            del self._seen[k]
        out: list[logging.LogRecord] = []
        if len(self._seen) >= 512:
            for emitted_at, repeats, template in self._seen.values():
                if repeats:
                    try:
                        summary = self._summary(
                            template, template.getMessage(), repeats, emitted_at, now
                        )
                    except Exception:  # pragma: no cover
                        continue
                    setattr(summary, _COLLAPSED_SUMMARY, True)
                    out.append(summary)
            self._seen.clear()
        return out


_COLLAPSER: _RepeatCollapser | None = None
"""The collapser :func:`_configure_logging` installed, for the exit flush."""


def _flush_repeat_counts() -> None:
    collapser = _COLLAPSER
    if collapser is not None:
        try:
            collapser.flush()
        except Exception:  # pragma: no cover - never block an exit
            pass


def _configure_logging(stream: TextIO | None = None) -> logging.Handler | None:
    """``basicConfig`` once, with the repeat collapser on its handler.

    Returns the handler it created, or None when the root logger already had
    one (a host that configured logging keeps its own).
    """
    root = logging.getLogger()
    if root.handlers:
        return None
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr if stream is None else stream,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        # Full date: widget.log is append-forever across restarts, and
        # time-only stamps made multi-day incident forensics guesswork.
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    global _COLLAPSER
    handler = root.handlers[0]
    collapser = _RepeatCollapser()
    collapser.install(handler)
    _COLLAPSER = collapser
    # A normal interpreter exit; the SIGTERM path flushes explicitly.
    atexit.register(_flush_repeat_counts)
    # The menu's Quit ends in NSApp terminate, which exits without atexit;
    # rumps emits before_quit from applicationWillTerminate_ on that path.
    try:
        import rumps.events

        if _flush_repeat_counts not in rumps.events.before_quit.callbacks:
            rumps.events.before_quit.register(_flush_repeat_counts)
    except Exception:  # pragma: no cover - no rumps: no menu Quit to cover
        pass
    return handler


# ---------------------------------------------------------------------------
# Scan-state hand-off: indexer  <->  state.ScanStateStore
# ---------------------------------------------------------------------------


def _scan_state_hooks(
    store: state_mod.ScanStateStore, settings: state_mod.SettingsStore
) -> tuple[Any, Any]:
    """Return ``(loader, saver)`` for :class:`~cc_usage_widget.indexer.Indexer`.

    SPEC 3 gives ``state.py`` the atomic-JSON persistence for the scan state,
    and the indexer exposes exactly these two hooks so it does not have to
    import it. Both run on the worker thread:

    * the loader is called once, lazily, on the indexer's first pass - so the
      ~850 KB read never happens on the AppKit thread at launch;
    * the saver prunes entries that fell out of the lookback window (which is
      what keeps the file from carrying transcripts the scanner will never open
      again) and writes atomically. It is only called when the indexer actually
      advanced an offset, so a tick that changed nothing writes nothing.

    Both hooks move the payload in its **on-disk shape**. The earlier version
    round-tripped it through ``FileScanState`` objects on the way in *and* out
    (json -> objects -> json -> objects on load, and the mirror on save), and
    let ``prune`` re-derive the live path set with one ``os.path.exists`` per
    tracked file. Together that was ~12 ms of the first tick after every launch,
    all of it spent re-deriving what the caller already knew.
    """

    def loader() -> Any:
        return store.load_json()

    def saver(payload: Mapping[str, Any]) -> bool:
        # MUST propagate the result. ``save_json`` never raises — it reports a
        # failed write as False precisely "so the caller can keep its dirty
        # flag set and retry on the next tick" (state.py:_write). Discarding
        # it told the indexer a failed write was durable, which is a silent
        # permanent double-count after the next restart (2026-08-26).
        return store.save_json(
            payload,
            lookback_days=int(
                settings.get("lookback_days", SETTINGS_DEFAULTS["lookback_days"])
            ),
        )

    return loader, saver


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build(extra_errors: Sequence[str] = ()) -> CCUsageWidgetApp:
    """Construct the app with real collaborators. Never raises.

    Each seam is built inside its own guard: whatever fails becomes a wiring
    error the menu shows, and everything else still runs. *extra_errors* are
    environment problems the caller found before wiring (a rival autoswitch
    engine, say) and get the same ``!`` treatment.
    """
    errors: list[str] = [str(text) for text in extra_errors]

    def fail(what: str, exc: BaseException) -> None:
        text = f"{what}: {type(exc).__name__}: {exc}"
        errors.append(text)
        _log(text)

    # --- settings (shared with accounts.py, so one instance) --------------
    settings_store: state_mod.SettingsStore | None = None
    settings: dict[str, Any] = dict(SETTINGS_DEFAULTS)
    persist = None
    try:
        settings_store = state_mod.settings_store()
        settings = state_mod.load_settings()
        persist = state_mod.save_settings
    except Exception as exc:
        fail(f"settings unavailable ({SETTINGS_PATH})", exc)

    lookback = int(settings.get("lookback_days", SETTINGS_DEFAULTS["lookback_days"]))

    def live_settings() -> Mapping[str, Any]:
        """The settings as they are NOW, for sources that outlive one tick.

        ``state.load_settings`` reads the process-wide store's in-memory copy
        (the same store ``save_settings`` writes through on every menu click),
        so this is cheap and current. Falls back to the boot snapshot if the
        store could not be built at all - a source must never crash on a
        settings read.
        """
        try:
            return state_mod.load_settings()
        except Exception:
            return settings

    # --- pricing (pure, no I/O) -------------------------------------------
    pricing: Any = None
    try:
        pricing = pricing_mod.DEFAULT_PRICING
    except Exception as exc:  # pragma: no cover - a table this broken is a bug
        fail("price table unavailable", exc)

    # --- rollup store (loaded by the worker, before its first merge) ------
    rollups: Any = None
    try:
        rollups = rollup_mod.DailyRollupStore(path=ROLLUPS_PATH, keep_days=lookback)
    except Exception as exc:
        fail(f"rollup store unavailable ({ROLLUPS_PATH})", exc)

    # --- indexer, persisting through state.ScanStateStore -----------------
    indexer: Any = None
    try:
        loader = saver = None
        if settings_store is not None:
            loader, saver = _scan_state_hooks(
                state_mod.ScanStateStore(SCAN_STATE_PATH), settings_store
            )
        indexer = indexer_mod.Indexer(
            projects_dir=PROJECTS_DIR,
            state_path=SCAN_STATE_PATH,
            lookback_days=lookback,
            pricing=pricing,
            state_loader=loader,
            state_saver=saver,
            # The worker commits the offsets itself, AFTER the rollup they
            # belong to is on disk. The reverse order (the indexer persisting
            # "consumed" before the caller has the tokens) turns any crash in
            # that window into a permanent, invisible undercount.
            defer_state_commit=True,
        )
    except Exception as exc:
        fail(f"transcript indexer unavailable ({PROJECTS_DIR})", exc)

    # --- Codex, as one more TranscriptSource (SPEC-CODEX 4) ---------------
    # Not special-cased anywhere downstream: the worker drives Claude and this
    # through the same loop, so it already obeys `cost_tracking_enabled`, the
    # lookback window and the rollup-before-offsets ordering. Everything that
    # differs about Codex - no account switching, a quota read from its own
    # transcripts, published cache rates - lives behind the protocol.
    #
    # Built unconditionally rather than gated on `codex_tracking_enabled`,
    # because the worker re-reads that setting on **every** tick
    # (`BackgroundWorker._scanners`). Constructing here therefore makes the
    # toggle live, where deciding here would have made it need a restart. The
    # constructor does no I/O; an absent ~/.codex is answered by `available()`
    # and costs one `stat` per tick.
    sources: list[Any] = []
    try:
        codex_loader = codex_saver = None
        if settings_store is not None:
            codex_loader, codex_saver = _scan_state_hooks(
                state_mod.ScanStateStore(CODEX_SCAN_STATE_PATH), settings_store
            )
        # `CodexIndexer` rather than its `build_codex_indexer` factory: the
        # factory takes neither the persistence hooks nor an explicit
        # `lookback_days`, so going through it would silently drop the
        # `state.ScanStateStore` wiring and leave the source writing its own
        # scan-state file behind state.py's back.
        sources.append(
            codex_mod.CodexIndexer(
                sessions_dir=CODEX_SESSIONS_DIR,
                state_path=CODEX_SCAN_STATE_PATH,
                lookback_days=lookback,
                pricing=pricing,
                state_loader=codex_loader,
                state_saver=codex_saver,
                # Same invariant as the Claude indexer: the worker commits the
                # offsets only once the rollup they belong to is on disk.
                defer_state_commit=True,
            )
        )
    except Exception as exc:
        # A broken vendor must not cost the user their Claude figures, but it
        # is a failure and says so (Rule 12). An *absent* corpus is not this
        # path - that is `available()` returning False, silently.
        fail(f"codex source unavailable ({CODEX_SESSIONS_DIR})", exc)

    # --- live per-account Codex quota (SPEC-CODEX 6) ----------------------
    # A SECOND Codex source, deliberately: the log-derived one above answers
    # "what did the corpus see" with no identity, this one answers "what does
    # each account's plan say" over HTTP. They meet in
    # `contracts.merge_quota_rows`, never here.
    #
    # The module is imported INSIDE the guard, not at the top of this file, so
    # that a `codex_accounts.py` which is missing, half-written or raising on
    # import degrades to one `!` line - it must never cost the user their
    # Claude figures, which a module-level `from . import codex_accounts` would
    # do by killing the whole entry point.
    try:
        from . import codex_accounts as codex_accounts_mod  # noqa: PLC0415

        sources.append(
            codex_accounts_mod.CodexAccountsSource(
                registry=codex_accounts_mod.Registry(CODEX_ACCOUNTS_REGISTRY_PATH),
                credentials=codex_accounts_mod.CredentialStore(CODEX_ACCOUNTS_DIR),
                transport=codex_accounts_mod.UrllibTransport(),
                snapshots_path=CODEX_QUOTA_SNAPSHOTS_PATH,
                # The LIVE settings, not the dict loaded above: the source reads
                # `codex_live_quota_enabled` and the poll interval on every
                # cycle, and a snapshot taken at boot would make both switches
                # need a restart. `state.load_settings` returns the
                # process-wide store's in-memory copy, which `save_settings`
                # (the app's persist hook) updates on every menu click - so
                # this is a read of shared state, not a file read per cycle.
                settings=live_settings,
            )
        )
    except Exception as exc:
        # Same rule as every other seam: no credentials, no registry and no
        # network is a normal state answered by `available()`; only a source
        # that cannot be BUILT lands here, and it lands visibly.
        fail("codex accounts source unavailable", exc)

    # --- accounts (claude_swap is imported lazily, on the worker thread) --
    accounts: Any = None
    try:
        accounts = accounts_mod.create_account_source(settings=settings_store)
    except Exception as exc:
        fail("account source unavailable", exc)

    return CCUsageWidgetApp(
        accounts=accounts,
        indexer=indexer,
        rollups=rollups,
        pricing=pricing,
        settings=settings,
        persist_settings=persist,
        wiring_errors=tuple(errors),
        # An explicit sequence - including an empty one - is a decision the
        # worker never second-guesses, so its `_build_extra_sources` autowire
        # (the fallback for a bare `app.build_app()`) does not also run and
        # give us two Codex scanners racing one scan-state file.
        sources=tuple(sources),
    )


def acquire_single_instance_lock() -> tuple[bool, str]:
    """Take an exclusive ``flock`` on :data:`LOCK_PATH`.

    Returns ``(acquired, detail)``. ``detail`` names the holding PID when the
    lock is already held, so the user is told what to quit instead of silently
    getting a second widget that fights the first one for three cache files.
    """
    global _lock_handle
    if _lock_handle is not None:
        return True, "already held by this process"
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        handle = open(LOCK_PATH, "a+", encoding="utf-8")
    except OSError as exc:
        # An unlockable directory must not stop the widget from running; the
        # guard is a safety net, not a dependency.
        _log(f"could not open {LOCK_PATH}: {exc!r} — running without the lock")
        return True, "lock unavailable"
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            handle.seek(0)
            holder = handle.read().strip() or "unknown"
        except OSError:  # pragma: no cover - defensive
            holder = "unknown"
        handle.close()
        return False, f"pid {holder}"
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:  # pragma: no cover - the lock itself is what matters
        pass
    _lock_handle = handle
    return True, f"pid {os.getpid()}"


_EXECUTABLE_POSITIONS = (0, 1)
"""argv indices a console script can occupy: it is either exec'd directly
(``cswap tui``) or run by its interpreter (``<python> <dir>/cswap tui``).
Anything further right is an *argument* - a file being read, edited or tailed."""


def _rival_actor(command: str) -> str | None:
    """``"/x/python /y/bin/cswap --tui --foo"`` -> ``"cswap tui"``.

    ``None`` when no token in *executable position* is ``cswap``. That is the
    half :data:`_RIVAL_PATTERN` cannot do: ``/usr/bin/tail -f ~/logs/cswap`` and
    ``vim ~/notes/cswap`` both satisfy the bare-TUI arm, and both are processes
    that cannot switch a thing. Verified with the real ``pgrep``: a ``tail -f``
    on a file named ``cswap`` came back in the same listing as the genuine TUI
    and was, before this check, textually indistinguishable in the menu.

    The label itself is the actor, not its install: the raw ``pgrep -fl`` line
    is two absolute paths and every flag, which no one can read in one menu row.
    A leading ``--`` is stripped so the ``--menubar`` spelling of ``menubar``
    reads as the same actor.
    """
    parts = command.split()
    for index in _EXECUTABLE_POSITIONS:
        if index >= len(parts):
            break
        if index == 1 and not parts[0].rsplit("/", 1)[-1].startswith("python"):
            # argv[1] is only an executable when argv[0] is the interpreter.
            break
        if parts[index].rsplit("/", 1)[-1] != "cswap":
            continue
        following = parts[index + 1] if index + 1 < len(parts) else ""
        subcommand = following[2:] if following.startswith("--") else following
        if subcommand in _RIVAL_SUBCOMMANDS:
            return f"cswap {subcommand}"
        # A bare ``cswap`` IS the TUI; other flags are not a subcommand.
        return "cswap"
    return None


def _detect_rival_engines() -> list[str]:
    """One operator line per running actor that can switch out from under us.

    Two engines against one ``autoswitch_state.json`` both evaluate the same
    threshold and both can issue a switch - double usage-API polling plus switch
    thrash, the exact failure SPEC 5 names. Upstream writes no owner/PID marker,
    so there is nothing in the shared file to check; the process table is.

    A hand switch in the TUI is the same hazard with a person behind it: the
    widget's engine sees a login it did not choose and switches back. The line
    therefore says what the operator will otherwise see and not understand -
    the "external" marker the account reader raises on such a change.

    Callable from the worker thread (``BackgroundWorker._rescan_rivals``) as
    well as from :func:`main`; it holds no module state beyond the pattern.
    """
    try:
        proc = subprocess.run(
            ["/usr/bin/pgrep", "-fl", _RIVAL_PATTERN],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"could not check for a rival autoswitch engine: {exc!r}")
        return []
    mine = str(os.getpid())
    found: list[str] = []
    for line in proc.stdout.splitlines():
        pid, _, rest = line.strip().partition(" ")
        if not pid or pid == mine or not rest:
            continue
        # The pattern is a pre-filter; the position check is the decision.
        # A hit that is not in executable position is a file NAMED cswap, and
        # reporting it would be a `!` row about a process that cannot switch.
        actor = _rival_actor(rest)
        if actor is None:
            continue
        found.append(
            f"another switch actor: {actor} (pid {pid}) — it "
            "can switch and can run its own engine; switches it makes show "
            'here as "external"'
        )
    return found


def _install_signal_handlers(app: CCUsageWidgetApp) -> None:
    """Route SIGINT/SIGTERM into :meth:`CCUsageWidgetApp.shutdown`.

    A Python signal handler only runs on the main thread at the next bytecode
    boundary, and the AppKit loop is Objective-C - so what actually delivers
    the signal is the 1 s ``rumps.Timer`` repaint tick, giving us a worst-case
    ~1 s exit. ``shutdown`` then stops the worker, which flushes the rollup
    store from the worker thread before the process goes away.
    """

    def handler(signum: int, _frame: Any) -> None:
        _log(f"signal {signal.Signals(signum).name}: shutting down")
        # AppKit's terminate ends the process with C exit(), past atexit: the
        # collapser's pending repeat counts are written here (R2-SEC-2).
        _flush_repeat_counts()
        try:
            app.shutdown()
        except Exception as exc:  # pragma: no cover - defensive
            _log(f"shutdown failed: {exc!r}")
            raise SystemExit(1) from exc

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError) as exc:  # not the main thread / unsupported
            _log(f"could not install {sig!r} handler: {exc!r}")


def _codex_state(app: CCUsageWidgetApp) -> str:
    """One word for what the Codex source is doing, for ``--dry-run``.

    Reads the wired source rather than re-statting the path, so the line
    describes the object the worker will actually drive. Never raises: a
    diagnostic must not be the thing that breaks the diagnostic.
    """
    settings = app.snapshot().settings
    if not settings.get("codex_tracking_enabled", True):
        return "tracking off"
    for source in getattr(app, "extra_sources", ()):
        # The corpus scanner, named by the fact that it HAS a corpus: with a
        # second Codex source wired (SPEC-CODEX 6) "the first source" is no
        # longer a safe way to mean "the indexer", and reporting the live
        # quota source's availability under a line about ~/.codex/sessions
        # would be a wrong answer wearing the right label.
        if getattr(source, "root", None) is None:
            continue
        try:
            return "present" if source.available() else "absent — no Codex section"
        except Exception as exc:  # pragma: no cover - available() is total
            return f"probe failed: {type(exc).__name__}: {exc}"
    return "not wired"


def _codex_accounts_lines(app: CCUsageWidgetApp) -> list[str]:
    """``--dry-run`` block for the live per-account Codex quota (SPEC-CODEX 6).

    Asks the source itself rather than re-deriving anything: it owns the
    registry, the credential directory and the poll schedule, and a diagnostic
    that re-implements them can disagree with the thing it describes. Blocking
    I/O is fine here and only here - ``--dry-run`` is a CLI that exits, not the
    AppKit thread (which reads the worker's cached copy instead).
    """
    lines: list[str] = []
    for source in getattr(app, "extra_sources", ()):
        diagnostics = getattr(source, "diagnostics", None)
        if not callable(diagnostics):
            continue
        try:
            lines.extend(f"  {line}" for line in (diagnostics() or ()))
        except Exception as exc:  # pragma: no cover - diagnostics is total
            lines.append(f"  diagnostics unavailable: {type(exc).__name__}: {exc}")
    return lines


def _diagnostics(app: CCUsageWidgetApp) -> str:
    """One-screen summary of what got wired, for ``--dry-run``."""
    snapshot = app.snapshot()
    lines = [
        f"title:        {app.render_title(snapshot)}",
        f"settings:     {SETTINGS_PATH}",
        f"scan state:   {SCAN_STATE_PATH}",
        f"rollups:      {ROLLUPS_PATH}",
        f"transcripts:  {PROJECTS_DIR}",
        f"codex state:  {CODEX_SCAN_STATE_PATH}",
        # `available()` is the whole SPEC-CODEX 5.5 answer: absent is a normal
        # state, so say which it is rather than leaving the reader to guess
        # from an empty Codex section.
        f"codex corpus: {CODEX_SESSIONS_DIR} ({_codex_state(app)})",
        # The live per-account quota (SPEC-CODEX 6) is a separate seam with a
        # separate off switch, so it gets its own two lines rather than being
        # folded into the corpus one - "the corpus is there" and "four
        # credentials are polling" are different facts about different files.
        f"codex accts:  {CODEX_ACCOUNTS_REGISTRY_PATH} "
        f"(live quota {'on' if snapshot.settings.get('codex_live_quota_enabled') else 'off'}, "
        f"{snapshot.settings.get('codex_quota_interval_seconds')} s)",
        f"lookback:     {snapshot.settings['lookback_days']} days",
        f"ui tick:      {snapshot.settings['ui_interval_seconds']} s",
        f"cost tick:    {snapshot.settings['cost_interval_seconds']} s",
    ]
    lines.extend(_codex_accounts_lines(app))
    for error in snapshot.wiring_errors:
        lines.append(f"! {error}")
    lines.append("menu:")
    lines.extend(f"  {key}" for key in app.menu.keys())
    return "\n".join(lines)


def _seed_status_item_position() -> None:
    """Self-heal the one thing that made the widget invisible (RCA 2026-08-17).

    On a saturated, notched menu bar macOS arbitrates overflow by each item's
    persisted ``NSStatusItem Preferred Position``; an item with NO stored
    position sorts last and is silently never composited (created, isVisible
    True, kCGWindowIsOnscreen False — proven by controlled toggle, same width
    hidden→rendered on this exact key). rumps never sets an autosave name, so
    seed the key ourselves when absent. 2000 = "rightmost priority"; macOS
    clamps it into the bar. Never overwrite an existing value — that would
    stomp the position the user chose by Cmd-dragging.

    Domain hazard, documented: upstream ``cswap menubar`` wrote an Info.plist
    beside the interpreter (bundle id ``com.claude-swap.menubar``), so every
    Python from this venv shares one defaults domain and one ``Item-0`` slot.
    Acceptable while this widget replaces upstream's; isolating via
    ``setAutosaveName_`` is the follow-up if both must ever coexist.
    """
    try:
        from Foundation import NSUserDefaults  # noqa: PLC0415

        defaults = NSUserDefaults.standardUserDefaults()
        key = "NSStatusItem Preferred Position Item-0"
        if defaults.objectForKey_(key) is None:
            defaults.setFloat_forKey_(2000.0, key)
    except Exception:  # pragma: no cover - cosmetic self-heal must never block launch
        pass


def main(argv: Sequence[str] | None = None) -> int:
    """Build, install signal handlers, kick off the index, run the loop."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--help" in args or "-h" in args:
        sys.stdout.write(USAGE)
        return 0
    dry_run = "--dry-run" in args
    unknown = [a for a in args if a not in ("--dry-run",)]
    if unknown:
        sys.stderr.write(f"unknown argument(s): {' '.join(unknown)}\n{USAGE}")
        return 2

    # Only a real run may touch global state. Seeding the status-item position
    # writes a user-defaults key in a domain shared with every other plain-Python
    # app, so `--help` and `--dry-run` -- both documented as read-only -- must
    # return before this point rather than mutating the user's machine.
    if not dry_run:
        _seed_status_item_position()

    # The adapter's diagnostics (engine started/stopped, switches, degradations)
    # go through `logging`; without a handler the default root level discards
    # every INFO line, so a widget that silently stopped switching left no trace.
    _configure_logging()

    if not dry_run:
        acquired, detail = acquire_single_instance_lock()
        if acquired:
            # Housekeeping that is only safe under the single-instance lock:
            # (a) atomic-write tmp orphans — a SIGKILL between mkstemp and
            # os.replace leaks `<state>.tmp.<pid>` files forever (six were
            # found on 2026-08-25, some carrying request IDs); (b) the
            # append-forever widget.log launchd redirects into — truncate a
            # runaway file so multi-month logs cannot grow unbounded.
            _sweep_tmp_orphans()
            _bound_widget_log()
        if not acquired:
            sys.stderr.write(
                f"cc-usage-widget is already running ({detail}). Quit that "
                f"instance first — two copies share {SCAN_STATE_PATH.name} and "
                f"{ROLLUPS_PATH.name}, and the last writer wins.\n"
            )
            return 0

    # Not fatal: cost tracking and the account rows are unaffected. But the user
    # has to be told, because two engines sharing autoswitch_state.json is
    # exactly what SPEC 5 warns about — both evaluate the same threshold and both
    # can issue a switch. This is the startup pass only; the worker re-runs the
    # same check every RIVAL_RESCAN_SECONDS, so an actor started later (or one
    # that has since quit) is not missed for the life of the process.
    environment_errors = _detect_rival_engines()
    for text in environment_errors:
        _log(f"! {text}")

    app = build(environment_errors)
    if dry_run:
        sys.stdout.write(_diagnostics(app) + "\n")
        return 0

    _install_signal_handlers(app)
    # Queue the first account read + transcript scan before the AppKit loop
    # starts, so the first-run index is already running on the worker thread by
    # the time the menu bar paints (SPEC 3.2).
    app.kickoff()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
