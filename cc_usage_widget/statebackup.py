"""Snapshot and restore the widget's cost-state files (roadmap item 17).

``Rebuild cost index`` is the one menu action that destroys data: it empties
``rollups.json`` and resets every scanner's offsets, and what comes back is
whatever the corpus can still produce *today*. Claude Code prunes
``~/.claude/projects`` on ``cleanupPeriodDays`` and Codex rotates its rollouts,
so a rebuild run a month after a day was recorded cannot re-create that day —
the rebuild is not idempotent against a shrinking corpus, and on 2026-09-10 the
recovery was done by hand: copy the four state files aside, rebuild, copy them
back when the result looked worse.

This module is that recovery, written down. It does exactly two things and
neither of them is clever:

* :func:`create_backup` copies the state files into
  ``<home>/backup-state-<ts>/`` before anything is cleared.
* :func:`restore_backup` copies one of those directories back.

**No deletion, ever.** Nothing here prunes old backups: the whole point is that
the user reaches for this after a rebuild went badly, and a retention policy is
one more thing that can eat the copy they needed. They are small (``rollups.json``
is a 30-day window of counters) and ``WIDGET_HOME`` is the user's own directory.

**Which files.** :data:`STATE_PATTERNS` — the rollup itself, one scan state per
vendor, and the per-project store. The ``scan_state*`` glob is deliberate: it
catches the dedup sidecar (``scan_state_dedup.json``), which is only meaningful
*together* with the offsets it belongs to (``indexer._ensure_states_loaded``).
Restoring one without the other would suppress exactly the records the other
says still need reading. ``attribution.json`` is in the set for the same reason
one level down: ``_rebuild_index`` clears it in the same breath as the rollup
store (it is a decomposition OF those days), so leaving it out meant the one
destructive button destroyed the per-project history with no copy, and a restore
put the day totals back beside a project store that had been emptied — two
answers to the same question, disagreeing, with no way back.
The Codex quota sidecar (``codex_scan_state_quota.json``) is NOT copied: it is a
cache of someone else's live figures, it is re-fetched on the next poll, and a
restored one would put an hours-old percentage on a row that claims to be live.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Iterable

__all__ = [
    "BACKUP_PREFIX",
    "STATE_PATTERNS",
    "backup_dir_name",
    "create_backup",
    "list_backups",
    "latest_backup",
    "restore_backup",
    "state_files",
]

BACKUP_PREFIX = "backup-state-"
"""Directory-name prefix inside ``WIDGET_HOME``.

Load-bearing in two directions: :func:`list_backups` finds backups by it, and
:func:`restore_backup` refuses any directory that does not carry it, so a
mistyped path can never copy an arbitrary tree over the user's live state."""

BACKUP_TIMESTAMP = "%Y%m%d-%H%M%S"
"""Local time, to the second, and lexically sortable — which is why
:func:`list_backups` can order by name without stat-ing anything."""

STATE_PATTERNS: tuple[str, ...] = (
    "rollups.json",
    "scan_state*.json",
    "codex_scan_state.json",
    "attribution.json",
)
"""Globs, relative to ``WIDGET_HOME``, of everything a rebuild destroys."""

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def backup_dir_name(now: float | None = None) -> str:
    """``"backup-state-20260910-142530"`` for *now* (local time)."""
    stamp = time.strftime(BACKUP_TIMESTAMP, time.localtime(time.time() if now is None else now))
    return f"{BACKUP_PREFIX}{stamp}"


def state_files(home: Path | str) -> tuple[Path, ...]:
    """Every state file that exists in *home*, sorted by name.

    A missing file is a normal state (a Claude-only machine has no
    ``codex_scan_state.json``; a first run has nothing at all), so this returns
    what is there rather than what should be.
    """
    root = Path(home)
    found: dict[str, Path] = {}
    for pattern in STATE_PATTERNS:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                found[path.name] = path
    return tuple(found[name] for name in sorted(found))


def create_backup(home: Path | str, *, name: str | None = None, now: float | None = None) -> Path | None:
    """Copy *home*'s state files into a new backup directory. Returns it.

    ``None`` when there is nothing to copy — a first run, or a machine whose
    state has already been cleared. An empty backup directory would be worse
    than none: ``Restore last backup`` would offer to restore nothing over a
    working store.

    *name* is the directory name the caller has already shown the user (the
    confirmation alert names the path before the destructive action runs, and
    the two must agree). A name that is already taken — two rebuilds inside one
    second — gets a ``-2``, ``-3``… suffix rather than overwriting a backup
    that may be the only copy of the good state.

    Raises ``OSError`` if the copy fails: the caller must NOT proceed to clear
    anything it could not snapshot first.
    """
    root = Path(home)
    sources = state_files(root)
    if not sources:
        return None
    base = name or backup_dir_name(now)
    dest = root / base
    suffix = 1
    while dest.exists():
        suffix += 1
        dest = root / f"{base}-{suffix}"
    dest.mkdir(mode=_DIR_MODE, parents=True)
    try:
        os.chmod(dest, _DIR_MODE)
    except OSError:  # pragma: no cover - a filesystem without modes
        pass
    for path in sources:
        target = dest / path.name
        shutil.copy2(path, target)
        try:
            os.chmod(target, _FILE_MODE)
        except OSError:  # pragma: no cover
            pass
    return dest


def list_backups(home: Path | str) -> tuple[Path, ...]:
    """Every backup directory in *home*, oldest first.

    Ordered by NAME, which is the timestamp: an mtime sort would reorder the
    list the moment a backup directory is touched by anything (a copy, a
    Finder preview, a backup tool), and the name is the thing the user was
    shown in the confirmation.
    """
    root = Path(home)
    try:
        entries = sorted(root.glob(f"{BACKUP_PREFIX}*"))
    except OSError:  # pragma: no cover - an unreadable home
        return ()
    return tuple(path for path in entries if path.is_dir())


def latest_backup(home: Path | str) -> Path | None:
    """The newest backup directory in *home*, or ``None``."""
    backups = list_backups(home)
    return backups[-1] if backups else None


def restore_backup(backup: Path | str, home: Path | str) -> tuple[str, ...]:
    """Copy every file of *backup* back into *home*. Returns the names copied.

    Refuses anything that is not a directory named with :data:`BACKUP_PREFIX`
    (``ValueError``) — this writes over the live store, and "which directory"
    must not be answerable by an accident.

    The restore is a plain overwrite, not a merge: the files in one backup were
    captured together and are only consistent together (offsets describe bytes
    already folded into that rollup). Files the backup does not contain are
    left alone rather than deleted, because the pair that matters is the one
    that was copied.
    """
    source = Path(backup)
    root = Path(home)
    if not source.name.startswith(BACKUP_PREFIX) or not source.is_dir():
        raise ValueError(f"not a state backup directory: {source}")
    restored: list[str] = []
    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        target = root / path.name
        shutil.copy2(path, target)
        try:
            os.chmod(target, _FILE_MODE)
        except OSError:  # pragma: no cover
            pass
        restored.append(path.name)
    return tuple(restored)


def describe(paths: Iterable[str]) -> str:
    """``"rollups.json, scan_state.json"`` — for a log line or an alert."""
    return ", ".join(sorted(paths))
