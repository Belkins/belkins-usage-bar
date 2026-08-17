"""Atomic JSON persistence for ``cc_usage_widget`` (SPEC 3, SPEC 3.2).

Two stores live here, both under :data:`~cc_usage_widget.contracts.WIDGET_HOME`:

* :class:`ScanStateStore` - ``scan_state.json``, one
  :class:`~cc_usage_widget.contracts.FileScanState` per transcript
  (~3,200 entries on this machine). Owned by ``indexer.py``.
* :class:`SettingsStore` - ``settings.json``, user preferences seeded from
  :data:`~cc_usage_widget.contracts.SETTINGS_DEFAULTS` on first run. Owned by
  ``app.py``.

Design rules this module holds to
--------------------------------

1. **Both files are caches or preferences, never sources of truth.** Losing
   ``scan_state.json`` costs exactly one re-index; losing ``settings.json``
   costs the user's toggles. So *nothing here raises at the caller*: a missing,
   unreadable, or corrupt file is logged **once** and falls back to
   empty/defaults. A hand-edited or half-written file must never stop the
   widget from launching.
2. **Every write is atomic**: temp file in the *same directory*, ``write`` ->
   ``flush`` -> ``fsync`` -> ``os.replace``. ``os.replace`` is atomic within a
   filesystem, so a crash (or a concurrent reader) sees either the whole old
   file or the whole new one - never a truncated one. The temp file is removed
   on any failure path, and the target's existing permission bits are
   preserved.
3. **No global mutable singleton.** ``__main__.py`` constructs one of each
   store and passes it to the modules that need it. The only module-level
   mutable object is the logger.
4. **Thread-safe.** ``indexer.py`` saves scan state from the background thread
   while ``app.py`` may write settings from the AppKit main thread, so each
   store guards its own state with an ``RLock``. Settings writes are sub-
   millisecond (<1 KB), which is why a main-thread toggle is tolerable; the
   scan-state write (a few hundred KB) is background-thread only.
5. **Writes are skipped when nothing changed.** Each store tracks a dirty
   flag, so the 300 s cost tick that finds no changed transcript performs no
   I/O here either (SPEC 2.1 idle budget).

Reading these files whole is fine and is *not* a violation of SPEC 2.2's
"never ``read()`` a whole file": that rule is about the 1.41 GB transcript
corpus. ``scan_state.json`` is bounded by the lookback window and pruning to a
few hundred KB.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import stat
import tempfile
import threading
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any, Final

from .contracts import (
    SCAN_STATE_PATH,
    SETTINGS_DEFAULTS,
    SETTINGS_PATH,
    FileScanState,
    normalize_settings,
    scan_state_from_json,
    scan_state_to_json,
)

__all__ = [
    "LOGGER",
    "ScanStateStore",
    "SettingsStore",
    "lookback_cutoff_epoch",
    "settings_store",
    "load_settings",
    "save_settings",
]

LOGGER: Final[logging.Logger] = logging.getLogger("cc_usage_widget.state")
"""Module logger. The default logger is used unless a store is handed one."""

_FALLBACK_FILE_MODE: Final[int] = 0o644
"""Permissions for a file we are creating for the first time. An existing
file's own mode is preserved instead."""


# ---------------------------------------------------------------------------
# Lookback-window helper (shared with the indexer's mtime pre-filter)
# ---------------------------------------------------------------------------


def lookback_cutoff_epoch(lookback_days: int, *, now: float | None = None) -> float:
    """POSIX timestamp of the first instant inside the lookback window.

    The window is the ``lookback_days``-long span of **local** calendar days
    ending on and including today, matching
    :func:`~cc_usage_widget.contracts.day_keys_back`. A file whose ``mtime``
    is below this value cannot contribute to any day the UI shows, so the
    indexer skips it (SPEC 3.2 step 3) and :meth:`ScanStateStore.prune` drops
    its entry.

    Args:
        lookback_days: window length in days; values below 1 are treated as 1.
        now: POSIX timestamp to treat as "now" (tests); defaults to the clock.

    Returns:
        Local midnight at the start of the window, as a POSIX timestamp.
    """
    days = max(1, int(lookback_days))
    today = dt.date.fromtimestamp(now) if now is not None else dt.date.today()
    start = today - dt.timedelta(days=days - 1)
    return dt.datetime.combine(start, dt.time.min).timestamp()


# ---------------------------------------------------------------------------
# Shared atomic-JSON plumbing
# ---------------------------------------------------------------------------


class _JsonStore:
    """Read/write plumbing shared by the two stores.

    Subclasses own the in-memory shape and the dirty flag; this class owns the
    filesystem contract: tolerant reads, atomic writes, log-once diagnostics.
    """

    def __init__(self, path: Path | str, *, logger: logging.Logger | None = None) -> None:
        self._path = Path(path)
        self._log = logger if logger is not None else LOGGER
        self._lock = threading.RLock()
        self._logged: set[str] = set()
        self.last_save_error: str | None = None
        """Human-readable reason the most recent :meth:`save` failed, else
        ``None``. Exposed so the UI can surface a persistent failure instead of
        silently retrying forever."""

    @property
    def path(self) -> Path:
        """Absolute path of the backing file."""
        return self._path

    # -- diagnostics --------------------------------------------------------

    def _log_once(self, key: str, message: str, *args: object) -> None:
        """Log *message* at WARNING the first time *key* is seen.

        Corrupt state is a once-per-process event, not a once-per-tick event;
        without this a bad file would spam the log every 300 s.
        """
        if key in self._logged:
            return
        self._logged.add(key)
        self._log.warning(message, *args)

    # -- read ---------------------------------------------------------------

    def _read(self) -> tuple[Any | None, bool]:
        """Parse the backing file.

        Returns:
            ``(parsed_json, file_existed)``. ``parsed_json`` is ``None`` when
            the file is absent, unreadable, or not valid JSON - all three are
            "start from scratch" for our callers, and the second element lets
            :class:`SettingsStore` tell "first run" (seed it) apart from
            "corrupt" (leave the user's file alone).
        """
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, False
        except OSError as exc:
            self._log_once(
                f"read-oserror:{self._path}",
                "cannot read %s (%s); continuing with defaults",
                self._path,
                exc,
            )
            return None, True
        if not text.strip():
            self._log_once(
                f"read-empty:{self._path}",
                "%s is empty; continuing with defaults",
                self._path,
            )
            return None, True
        try:
            return json.loads(text), True
        except (ValueError, UnicodeDecodeError) as exc:
            self._log_once(
                f"read-corrupt:{self._path}",
                "%s is not valid JSON (%s); continuing with defaults",
                self._path,
                exc,
            )
            return None, True

    # -- write --------------------------------------------------------------

    def _target_mode(self) -> int:
        """Permission bits to give the replacement file."""
        try:
            return stat.S_IMODE(os.stat(self._path).st_mode)
        except OSError:
            return _FALLBACK_FILE_MODE

    def _write(self, payload: Any, *, indent: int | None, sort_keys: bool = True) -> bool:
        """Serialise *payload* and atomically replace the backing file.

        Never raises: a failure is logged, recorded in
        :attr:`last_save_error`, and reported as ``False`` so the caller can
        keep its dirty flag set and retry on the next tick.

        Args:
            payload: JSON-serialisable object.
            indent: ``2`` for human-editable files, ``None`` for the compact
                machine-written scan state.
            sort_keys: keep the file diff-stable. Worth 0.59 ms of the SPEC 2.1
                tick budget on the ~3,200-key scan state, so the raw-JSON
                hand-off (:meth:`ScanStateStore.save_json`) turns it off - a
                machine-written cache nobody diffs does not need it. Every
                human-editable file keeps it on.

        Returns:
            True when the file now contains *payload*.
        """
        tmp_path: Path | None = None
        try:
            separators = None if indent is not None else (",", ":")
            data = json.dumps(
                payload, indent=indent, separators=separators, sort_keys=sort_keys
            ).encode("utf-8")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            mode = self._target_mode()
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, self._path)
            tmp_path = None
        except (OSError, TypeError, ValueError) as exc:
            self.last_save_error = f"{type(exc).__name__}: {exc}"
            self._log.warning("failed to write %s: %s", self._path, exc)
            return False
        else:
            self.last_save_error = None
            return True
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _delete_file(self) -> bool:
        """Remove the backing file if present. Never raises."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return True
        except OSError as exc:
            self.last_save_error = f"{type(exc).__name__}: {exc}"
            self._log.warning("failed to delete %s: %s", self._path, exc)
            return False
        return True


# ---------------------------------------------------------------------------
# scan_state.json
# ---------------------------------------------------------------------------


class ScanStateStore(_JsonStore):
    """Per-transcript scan bookkeeping, ``{abs path: FileScanState}`` (SPEC 3.2).

    On-disk shape is exactly SPEC 3.2's, produced by
    :func:`~cc_usage_widget.contracts.scan_state_to_json`::

        {"/abs/path.jsonl": {"inode": 1, "size": 2, "mtime": 3.0, "offset": 2}}

    Written with no indent and ``(",", ":")`` separators, keys sorted, so the
    ~3,200-entry file stays a few hundred KB and is diff-stable. Malformed
    individual entries are dropped on load, which is correct by construction:
    a missing entry re-reads that transcript from offset 0.

    Typical lifecycle, all on the indexer's background thread::

        store = ScanStateStore()
        store.load()
        ...                                  # per tick
        store.set(path, state.advanced(...))
        store.prune(lookback_days=30, known_paths=seen_paths)
        store.save()
    """

    def __init__(
        self,
        path: Path | str = SCAN_STATE_PATH,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(path, logger=logger)
        self._states: dict[str, FileScanState] = {}
        self._dirty = False
        self._loaded = False

    # -- lifecycle ----------------------------------------------------------

    def load(self) -> dict[str, FileScanState]:
        """Load from disk, tolerating absence and corruption.

        Returns:
            A copy of the loaded mapping (empty on a fresh or unusable file).
        """
        parsed, _existed = self._read()
        states = scan_state_from_json(parsed) if parsed is not None else {}
        if parsed is not None and not isinstance(parsed, Mapping):
            self._log_once(
                f"scan-not-object:{self._path}",
                "%s top level is %s, not an object; re-indexing from scratch",
                self._path,
                type(parsed).__name__,
            )
        elif isinstance(parsed, Mapping) and len(states) != len(parsed):
            self._log_once(
                f"scan-skipped:{self._path}",
                "%s: skipped %d malformed entries; those files will be re-read",
                self._path,
                len(parsed) - len(states),
            )
        with self._lock:
            self._states = states
            self._dirty = False
            self._loaded = True
            return dict(self._states)

    def save(self, *, force: bool = False) -> bool:
        """Persist atomically, skipping the write when nothing changed.

        Args:
            force: write even when the store is not dirty.

        Returns:
            True when the file is up to date (including the skipped-write
            case); False when the write failed - the dirty flag is kept so the
            next tick retries.
        """
        with self._lock:
            if not self._dirty and not force:
                return True
            payload = scan_state_to_json(self._states)
            ok = self._write(payload, indent=None)
            if ok:
                self._dirty = False
            return ok

    # -- raw-JSON hand-off (the indexer's loader/saver hooks) ---------------

    def load_json(self) -> Any:
        """The parsed file, or ``None`` when there is nothing usable.

        The indexer's ``state_loader`` hook wants exactly the on-disk payload and
        does its own :func:`~cc_usage_widget.contracts.scan_state_from_json`.
        Routing it through :meth:`load` instead cost two redundant conversions of
        a ~3,200-entry mapping on every launch (json -> ``FileScanState`` ->
        json -> ``FileScanState``), measured at ~2.5 ms of the first tick. The
        tolerant read, and its logging, are shared with :meth:`load`.
        """
        parsed, _existed = self._read()
        with self._lock:
            self._loaded = True
        return parsed

    def save_json(
        self,
        payload: Mapping[str, Any],
        *,
        lookback_days: int | None = None,
        now: float | None = None,
    ) -> bool:
        """Write an on-disk-shaped *payload*, dropping out-of-window entries.

        The mirror of :meth:`load_json`, for the indexer's ``state_saver`` hook.
        *payload* is already the authoritative tracked set - the indexer evicts
        vanished transcripts itself on every clean full pass - so the only rule
        applied here is the lookback cutoff (:meth:`prune`'s mtime rule), which
        is what keeps the file from carrying transcripts the scanner will never
        open again. No existence check, no object graph, one serialisation.

        The in-memory mapping is **not** updated: this store's only remaining
        reader is :meth:`load_json`, and rebuilding 3,200 dataclasses to keep a
        mapping nobody reads in sync is the cost this method exists to avoid.
        """
        cutoff = (
            lookback_cutoff_epoch(lookback_days, now=now)
            if lookback_days is not None
            else None
        )
        if cutoff is None:
            kept = dict(payload)
        else:
            kept = {}
            for key, entry in payload.items():
                mtime = entry.get("mtime") if isinstance(entry, Mapping) else None
                if isinstance(mtime, (int, float)) and not isinstance(mtime, bool):
                    if float(mtime) < cutoff:
                        continue
                kept[str(key)] = entry
        ok = self._write(kept, indent=None, sort_keys=False)
        if ok:
            with self._lock:
                self._dirty = False
        return ok

    def reset(self) -> None:
        """Drop every entry and delete the file (backs ``TranscriptIndexer.reset``)."""
        with self._lock:
            self._states = {}
            self._dirty = False
            self._delete_file()

    # -- accessors ----------------------------------------------------------

    @property
    def loaded(self) -> bool:
        """True once :meth:`load` has run."""
        return self._loaded

    @property
    def dirty(self) -> bool:
        """True when in-memory state differs from the file."""
        return self._dirty

    def __len__(self) -> int:
        return len(self._states)

    def __contains__(self, path: object) -> bool:
        return str(path) in self._states

    def get(self, path: Path | str) -> FileScanState | None:
        """State for one transcript, or ``None`` if we have never read it."""
        return self._states.get(str(path))

    def snapshot(self) -> dict[str, FileScanState]:
        """A copy of the whole mapping - safe to hand to another thread."""
        with self._lock:
            return dict(self._states)

    def paths(self) -> tuple[str, ...]:
        """Every path we hold state for, sorted."""
        with self._lock:
            return tuple(sorted(self._states))

    # -- mutation -----------------------------------------------------------

    def set(self, path: Path | str, state: FileScanState) -> None:
        """Record *state* for *path*, marking the store dirty if it changed."""
        key = str(path)
        with self._lock:
            if self._states.get(key) == state:
                return
            self._states[key] = state
            self._dirty = True

    def update(self, states: Mapping[str, FileScanState]) -> None:
        """Merge a batch of entries in one lock acquisition."""
        with self._lock:
            for key, state in states.items():
                text_key = str(key)
                if self._states.get(text_key) == state:
                    continue
                self._states[text_key] = state
                self._dirty = True

    def discard(self, path: Path | str) -> bool:
        """Forget one entry. Returns True if it was present."""
        key = str(path)
        with self._lock:
            if self._states.pop(key, None) is None:
                return False
            self._dirty = True
            return True

    def clear(self) -> None:
        """Forget every entry in memory (the file is rewritten on next save)."""
        with self._lock:
            if not self._states:
                return
            self._states = {}
            self._dirty = True

    def prune(
        self,
        *,
        lookback_days: int | None = None,
        now: float | None = None,
        known_paths: Collection[str | Path] | None = None,
        drop_missing: bool = True,
    ) -> int:
        """Drop entries that can no longer contribute, keeping the file bounded.

        An entry is dropped when either:

        * **its file is gone** - detected without any syscall when the caller
          passes *known_paths* (the set of transcripts its ``os.scandir`` walk
          just saw, the cheap path used by the indexer); otherwise, and only
          when *drop_missing* is True, by an ``os.path.exists`` per entry; or
        * **it fell out of the lookback window** - its recorded ``mtime`` is
          older than :func:`lookback_cutoff_epoch`, so the indexer would skip
          the file anyway (SPEC 3.2 step 3).

        Args:
            lookback_days: window length; ``None`` disables the mtime rule.
            now: POSIX timestamp to treat as "now" (tests).
            known_paths: paths that exist right now. When given, the existence
                rule uses this set and performs no filesystem calls.
            drop_missing: apply the existence rule at all. Ignored (treated as
                True) when *known_paths* is supplied.

        Returns:
            Number of entries removed.
        """
        cutoff = (
            lookback_cutoff_epoch(lookback_days, now=now)
            if lookback_days is not None
            else None
        )
        known = {str(p) for p in known_paths} if known_paths is not None else None
        removed = 0
        with self._lock:
            for key, state in list(self._states.items()):
                if known is not None:
                    gone = key not in known
                elif drop_missing:
                    gone = not os.path.exists(key)
                else:
                    gone = False
                if not gone and cutoff is not None and state.mtime < cutoff:
                    gone = True
                if gone:
                    del self._states[key]
                    removed += 1
            if removed:
                self._dirty = True
        return removed


# ---------------------------------------------------------------------------
# settings.json
# ---------------------------------------------------------------------------


class SettingsStore(_JsonStore):
    """User preferences, validated through
    :func:`~cc_usage_widget.contracts.normalize_settings`.

    Behaviour that matters:

    * **First run seeds the file.** If ``settings.json`` does not exist,
      :meth:`load` writes the full :data:`SETTINGS_DEFAULTS` so the user has
      something to hand-edit.
    * **A corrupt file is not overwritten on load.** We fall back to defaults
      in memory and leave the bad bytes on disk for the user to fix; the first
      explicit :meth:`set` is what replaces it. (A corrupt file is reported
      once at WARNING.)
    * **Unknown keys survive a write.** Keys we do not recognise are held
      aside and re-emitted verbatim on :meth:`save`, so an older build cannot
      strip a newer build's settings. Recognised keys always win over an
      unknown key of the same name (impossible by construction, but explicit).
    * **A value of the wrong type is dropped in favour of its default**, and
      out-of-range integers are clamped to :data:`SETTINGS_BOUNDS` - both by
      :func:`normalize_settings`, so a hand-edit cannot brick the widget or
      defeat the idle-CPU floor. Rejections are logged once.

    Written with ``indent=2`` and sorted keys, because this file is meant to
    be opened by a human.
    """

    def __init__(
        self,
        path: Path | str = SETTINGS_PATH,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(path, logger=logger)
        self._values: dict[str, Any] = dict(SETTINGS_DEFAULTS)
        self._extras: dict[str, Any] = {}
        self._dirty = False
        self._loaded = False

    # -- lifecycle ----------------------------------------------------------

    def load(self, *, seed_missing: bool = True) -> dict[str, Any]:
        """Load, normalise, and (on first run) seed the file.

        Args:
            seed_missing: write :data:`SETTINGS_DEFAULTS` when the file is
                absent. Pass False in tests that must not touch the disk.

        Returns:
            A copy of the effective settings - always complete and
            type-correct, whatever was on disk.
        """
        parsed, existed = self._read()
        with self._lock:
            if isinstance(parsed, Mapping):
                self._values = normalize_settings(parsed)
                self._extras = {
                    str(key): value
                    for key, value in parsed.items()
                    if str(key) not in SETTINGS_DEFAULTS
                }
                rejected = sorted(
                    str(key)
                    for key in parsed
                    if str(key) in SETTINGS_DEFAULTS
                    and parsed[key] != self._values[str(key)]
                )
                if rejected:
                    self._log_once(
                        f"settings-rejected:{self._path}",
                        "%s: ignoring out-of-range or wrongly typed values for %s",
                        self._path,
                        ", ".join(rejected),
                    )
            else:
                if parsed is not None:
                    self._log_once(
                        f"settings-not-object:{self._path}",
                        "%s top level is %s, not an object; using defaults",
                        self._path,
                        type(parsed).__name__,
                    )
                self._values = dict(SETTINGS_DEFAULTS)
                self._extras = {}
            self._dirty = False
            self._loaded = True
            snapshot = dict(self._values)
        if seed_missing and not existed:
            self.save(force=True)
        return snapshot

    def save(self, *, force: bool = False) -> bool:
        """Persist atomically, skipping the write when nothing changed.

        Unknown keys are merged back in first, so forward compatibility
        survives every write.
        """
        with self._lock:
            if not self._dirty and not force:
                return True
            payload: dict[str, Any] = dict(self._extras)
            payload.update(self._values)
            ok = self._write(payload, indent=2)
            if ok:
                self._dirty = False
            return ok

    def reset_to_defaults(self, *, persist: bool = True) -> dict[str, Any]:
        """Restore every recognised key to its default.

        Unknown keys are left untouched - they are not ours to discard.
        """
        with self._lock:
            self._values = dict(SETTINGS_DEFAULTS)
            self._dirty = True
            snapshot = dict(self._values)
        if persist:
            self.save()
        return snapshot

    # -- accessors ----------------------------------------------------------

    @property
    def loaded(self) -> bool:
        """True once :meth:`load` has run."""
        return self._loaded

    @property
    def dirty(self) -> bool:
        """True when in-memory settings differ from the file."""
        return self._dirty

    def as_dict(self) -> dict[str, Any]:
        """A copy of the recognised settings, complete and type-correct."""
        with self._lock:
            return dict(self._values)

    def extras(self) -> dict[str, Any]:
        """A copy of the unrecognised keys preserved for forward compatibility."""
        with self._lock:
            return dict(self._extras)

    def get(self, key: str, default: Any = None) -> Any:
        """Value for *key*.

        Recognised keys always resolve (to their default if the file omitted
        them); an unrecognised key falls back to the preserved raw value, then
        to *default*.
        """
        with self._lock:
            if key in SETTINGS_DEFAULTS:
                return self._values[key]
            return self._extras.get(key, default)

    # -- mutation -----------------------------------------------------------

    def set(self, key: str, value: Any, *, persist: bool = True) -> bool:
        """Set one recognised setting, validating through ``normalize_settings``.

        A wrongly typed value is refused (the previous value stays) and an
        out-of-range integer is clamped; both are logged at DEBUG, because the
        callers here are our own code and a mismatch is a bug, not user input.

        Args:
            key: a key of :data:`SETTINGS_DEFAULTS`.
            value: the new value.
            persist: write the file immediately (default). Pass False to batch
                several changes and call :meth:`save` once.

        Returns:
            True when the store is consistent with disk afterwards - i.e. the
            write succeeded, was skipped as unnecessary, or was deferred.

        Raises:
            KeyError: if *key* is not a recognised setting. Silently inventing
                a new key would hide a typo in a menu handler.
        """
        if key not in SETTINGS_DEFAULTS:
            raise KeyError(f"unknown setting {key!r}")
        with self._lock:
            candidate = dict(self._values)
            candidate[key] = value
            normalized = normalize_settings(candidate)
            effective = normalized[key]
            if effective != value:
                self._log.debug(
                    "setting %s: %r coerced to %r", key, value, effective
                )
            if effective == self._values[key]:
                return True
            self._values[key] = effective
            self._dirty = True
        return self.save() if persist else True

    def update(self, values: Mapping[str, Any], *, persist: bool = True) -> bool:
        """Set several recognised settings with a single write.

        Raises:
            KeyError: if any key is unrecognised. Nothing is applied in that
                case - the check runs before the first mutation.
        """
        unknown = sorted(str(key) for key in values if key not in SETTINGS_DEFAULTS)
        if unknown:
            raise KeyError(f"unknown setting(s) {', '.join(unknown)}")
        changed = False
        with self._lock:
            candidate = dict(self._values)
            candidate.update(values)
            normalized = normalize_settings(candidate)
            for key in values:
                effective = normalized[key]
                if effective != values[key]:
                    self._log.debug(
                        "setting %s: %r coerced to %r", key, values[key], effective
                    )
                if effective != self._values[key]:
                    self._values[key] = effective
                    changed = True
            if changed:
                self._dirty = True
        if not changed:
            return True
        return self.save() if persist else True

    def toggle(self, key: str, *, persist: bool = True) -> bool:
        """Flip a boolean setting and return its new value.

        Backs SPEC 4.2's two top-level one-click switches.

        Raises:
            KeyError: if *key* is not a recognised setting.
            TypeError: if *key* is not a boolean setting.
        """
        if key not in SETTINGS_DEFAULTS:
            raise KeyError(f"unknown setting {key!r}")
        if not isinstance(SETTINGS_DEFAULTS[key], bool):
            raise TypeError(f"setting {key!r} is not a boolean")
        with self._lock:
            new_value = not bool(self._values[key])
        self.set(key, new_value, persist=persist)
        return new_value


# ---------------------------------------------------------------------------
# Module-level settings facade
# ---------------------------------------------------------------------------
#
# ``app.build_app()`` looks for ``state.load_settings()`` / ``state.save_settings()``
# when it wires itself for a bare ``python -m cc_usage_widget``; without them it
# falls back to reading settings.json directly and hands the app **no** persist
# callable, so every toggle would be forgotten at restart (SPEC 6.2). These three
# functions are that seam. ``__main__.py`` instead owns a :class:`SettingsStore`
# explicitly, which is the better path because the same instance can be handed to
# ``accounts.py`` for its toggle mirror.

_SETTINGS_STORE: SettingsStore | None = None
_SETTINGS_STORE_LOCK: Final[threading.Lock] = threading.Lock()


def settings_store() -> SettingsStore:
    """The process-wide :class:`SettingsStore`, created on first use.

    One instance matters: the store holds the unknown keys read from disk and
    re-emits them on every write, so two stores over one file would take turns
    dropping each other's forward-compatible keys.
    """
    global _SETTINGS_STORE
    with _SETTINGS_STORE_LOCK:
        if _SETTINGS_STORE is None:
            _SETTINGS_STORE = SettingsStore()
        return _SETTINGS_STORE


def load_settings() -> dict[str, Any]:
    """Effective settings, seeding ``settings.json`` on first run."""
    store = settings_store()
    return store.load() if not store.loaded else store.as_dict()


def save_settings(values: Mapping[str, Any]) -> bool:
    """Persist recognised settings, ignoring keys we do not own.

    Unrecognised keys are dropped rather than raising, because this is the
    callback form handed to ``app.py``: a stale key from a newer build must not
    turn a menu click into a traceback. :meth:`SettingsStore.update` keeps the
    strict, raising behaviour for our own code.
    """
    store = settings_store()
    if not store.loaded:
        store.load()
    known = {
        str(key): value
        for key, value in values.items()
        if str(key) in SETTINGS_DEFAULTS
    }
    if not known:
        return True
    return store.update(known)
