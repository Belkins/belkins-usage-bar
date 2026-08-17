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

import fcntl
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from . import accounts as accounts_mod
from . import app as app_mod
from . import codex_indexer as codex_mod
from . import indexer as indexer_mod
from . import pricing as pricing_mod
from . import rollup as rollup_mod
from . import state as state_mod
from .app import CCUsageWidgetApp
from .contracts import (
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

_RIVAL_PATTERN = r"cswap[[:space:]]+(auto|menubar)"
"""Upstream engines that would share ``autoswitch_state.json`` with us. SPEC 5
says to quit ``cswap menubar`` first; nothing enforced or even detected it.

A **POSIX** character class, not ``\\s``: macOS ``pgrep`` compiles its pattern as
POSIX extended regex, where ``\\s`` is not a shorthand and the match silently
never fires. Verified against a live ``cswap menubar``."""

_lock_handle: Any = None
"""Module-global so the ``flock`` lives as long as the process."""

USAGE = """usage: python -m cc_usage_widget [--dry-run] [--help]

  (no flags)  run the menu-bar widget in the foreground
  --dry-run   wire everything, print the diagnostics, exit without entering
              the AppKit loop (no second icon in the menu bar)
"""


def _log(message: str) -> None:
    """Timestamped stderr line, same shape as ``app.py``'s log."""
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] cc-usage-widget: {message}\n")
    sys.stderr.flush()


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

    def saver(payload: Mapping[str, Any]) -> None:
        store.save_json(
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


def _detect_rival_engines() -> list[str]:
    """Command lines of running ``cswap auto`` / ``cswap menubar`` processes.

    Two engines against one ``autoswitch_state.json`` both evaluate the same
    threshold and both can issue a switch - double usage-API polling plus switch
    thrash, the exact failure SPEC 5 names. Upstream writes no owner/PID marker,
    so there is nothing in the shared file to check; the process table is.
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
        if pid and pid != mine and rest:
            found.append(f"{rest.strip()} (pid {pid})")
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
        try:
            return "present" if source.available() else "absent — no Codex section"
        except Exception as exc:  # pragma: no cover - available() is total
            return f"probe failed: {type(exc).__name__}: {exc}"
    return "not wired"


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
        f"lookback:     {snapshot.settings['lookback_days']} days",
        f"ui tick:      {snapshot.settings['ui_interval_seconds']} s",
        f"cost tick:    {snapshot.settings['cost_interval_seconds']} s",
    ]
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
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            stream=sys.stderr,
            format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    if not dry_run:
        acquired, detail = acquire_single_instance_lock()
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
    # can issue a switch.
    environment_errors = [
        f"another autoswitch engine is running: {line} — quit it, or turn "
        "Auto-switch off here"
        for line in _detect_rival_engines()
    ]
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
