"""Append-only history mirror of the rollup store, plus CSV/JSON export
(roadmap item 8).

Why this exists
---------------

``rollups.json`` is a **pure cache** bounded by ``lookback_days`` (30 by
default): :meth:`~cc_usage_widget.rollup.DailyRollupStore.save` prunes on every
write, and Claude Code prunes ``~/.claude/projects`` on its own
``cleanupPeriodDays`` schedule, so a day that falls out of the window is gone
from *both* the aggregate and the corpus it was derived from. Nothing in the
program remembers July.

This module is the long-term record. It mirrors every day the rollup store
currently holds into ``history.sqlite`` and then **never deletes a row**, so
the mirror keeps growing past the window while the cache stays small. It is a
mirror, not a source: deleting it costs history, not correctness, and the
widget renders exactly the same menu without it.

Guarantees
----------

* **Append-or-replace, never delete.** :meth:`HistoryStore.upsert_day` writes
  one row per ``(day, key)`` present in the rollup it is handed, replacing the
  stored counters when they differ. A rebuild or a self-audit repair that
  *lowers* a cell therefore propagates - the reason this is a REPLACE and not
  an ``INSERT OR IGNORE`` (the 1,650x inflation this roadmap opens with would
  otherwise have been mirrored into permanent history). There is no ``DELETE``
  statement in this file. The one consequence, stated rather than hidden: a
  ``(day, key)`` cell that disappears from the store *entirely* keeps its last
  recorded value here, because a day the store simply no longer covers and a
  day whose usage was retracted look identical from this side.
* **Idempotent.** Re-mirroring an unchanged day writes nothing at all, so
  ``updated_at`` means "when this cell last changed", not "when we last
  looked", and an idle widget does not rewrite the same rows every tick.
* **Never raises into the worker tick.** Every public method traps its own
  errors, records them on :attr:`HistoryStore.errors` (which the menu renders
  as a ``!`` line) and returns a neutral value. A history failure must never be
  able to stop the cost job that produced the numbers.
* **0600, and no free text.** The file holds day keys, vendor-qualified model
  keys, integer token counters and a notional dollar figure - nothing from a
  prompt, a completion, a path or a branch name. It is created 0600 anyway,
  because it is a record of someone's spend.

Money
-----

``usd_at_record`` is what the day cost *at the rates in effect on that day*,
captured when the row was written. The export puts it beside a second column
priced at **today's** table, so a rate change is visible as a difference rather
than silently rewriting history. Both are :data:`NOTIONAL_LABEL` figures and
the export column names say so.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .contracts import (
    HISTORY_DB_PATH,
    NOTIONAL_LABEL,
    DayKey,
    DayRollup,
    ModelUsage,
    PricingTable,
    Usd,
    Vendor,
    local_day_key,
    parse_day_key,
    raw_model_of_key,
    vendor_of_key,
)

__all__ = [
    "HistoryStore",
    "history_path_for",
    "HistoryRow",
    "EXPORT_FORMATS",
    "DEFAULT_EXPORT_DIR",
    "export_filename",
    "export_history",
    "open_history",
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION: Final[int] = 2
"""Bumped to 2 by the per-project table (roadmap item 6).

A v1 database is migrated by :func:`_migrate_project_table`, which only ever
*creates* something: not one existing row is read, rewritten or moved. That is
deliberate and is the whole reason the project decomposition is a second table
rather than a column on ``daily``. Widening ``daily`` would have meant changing
its primary key from ``(day, key)`` to ``(day, key, project)`` - and sqlite
cannot widen a primary key in place, so it would have meant rebuilding the one
file in this program whose contents are unreconstructible (SPEC: a day that has
aged out of ``rollups.json`` and out of ``~/.claude/projects`` exists nowhere
else). A migration that touches no row cannot lose one.
"""

_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS daily (
    day             TEXT    NOT NULL,
    key             TEXT    NOT NULL,
    vendor          TEXT    NOT NULL,
    input           INTEGER NOT NULL,
    output          INTEGER NOT NULL,
    cache_read      INTEGER NOT NULL,
    cache_write_5m  INTEGER NOT NULL,
    cache_write_1h  INTEGER NOT NULL,
    usd_at_record   REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    PRIMARY KEY (day, key)
)
"""

_INDEX_SQL: Final[str] = "CREATE INDEX IF NOT EXISTS daily_day ON daily (day)"

_UPSERT_DAILY_SQL: Final[str] = (
    "INSERT OR REPLACE INTO daily "
    "(day, key, vendor, input, output, cache_read, "
    " cache_write_5m, cache_write_1h, usd_at_record, updated_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?)"
)
"""The one statement that writes a ``daily`` cell.

Shared by :meth:`HistoryStore.upsert_days` and :meth:`HistoryStore.replace_day`
because the two differ only in WHICH cells they write: a second copy of the
column list is a second place for a migration to be forgotten."""

_PROJECT_TABLE_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS daily_project (
    day             TEXT    NOT NULL,
    key             TEXT    NOT NULL,
    project         TEXT    NOT NULL,
    vendor          TEXT    NOT NULL,
    input           INTEGER NOT NULL,
    output          INTEGER NOT NULL,
    cache_read      INTEGER NOT NULL,
    cache_write_5m  INTEGER NOT NULL,
    cache_write_1h  INTEGER NOT NULL,
    usd_at_record   REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    PRIMARY KEY (day, key, project)
)
"""

_PROJECT_INDEX_SQL: Final[str] = (
    "CREATE INDEX IF NOT EXISTS daily_project_day ON daily_project (day)"
)

PROJECT_COLUMNS: Final[tuple[str, ...]] = (
    "day",
    "key",
    "project",
    "vendor",
    "input",
    "output",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "usd_at_record",
    "updated_at",
)
"""Every column of ``daily_project``, in declaration order.

Identical to :data:`COLUMNS` plus one: ``project``. That column is the single
free-ish string this database has ever held, and its contents are bounded by
contract - **one path component**, the basename of the working directory a
session ran in, capped at 64 characters by ``attribution.py``. Never a full
path, never a git branch, never a session id, never a file name inside the
project. ``tests/test_history.py`` asserts this column list for the same reason
it asserts :data:`COLUMNS`: adding a second string here should be a test change,
and therefore a conversation.
"""

COLUMNS: Final[tuple[str, ...]] = (
    "day",
    "key",
    "vendor",
    "input",
    "output",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "usd_at_record",
    "updated_at",
)
"""Every column of ``daily``, in declaration order.

Named here so a test can assert the shape rather than describe it, and so the
privacy promise (no free text, ever) is checkable by reading one tuple.
"""

_CORRUPTION_MARKERS: Final[tuple[str, ...]] = (
    "not a database",
    "malformed",
    "encrypted",
    "corrupt",
)
"""Substrings that mean *this file is unusable*, as opposed to *try again*.

``sqlite3.OperationalError`` covers both "database disk image is malformed"
(quarantine and start over) and "database is locked" (a transient another
process is holding), so the class alone cannot tell them apart and the message
has to.
"""


def _is_corruption(exc: BaseException) -> bool:
    """True when *exc* means the database file itself is unusable."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _CORRUPTION_MARKERS)


# ---------------------------------------------------------------------------
# Export destination
# ---------------------------------------------------------------------------


def _env_dir(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else default


DEFAULT_EXPORT_DIR: Final[Path] = _env_dir(
    "CC_USAGE_WIDGET_EXPORT_DIR", Path.home() / "Downloads"
)
"""Where ``Export usage …`` writes. ``~/Downloads`` in production.

Overridable so a test never writes into the real Downloads folder - and so is
the *caller*: :func:`export_history` takes an explicit ``directory``, which is
what the tests actually inject. The environment variable is for an operator
who wants exports somewhere else.
"""

EXPORT_FORMATS: Final[tuple[str, ...]] = ("csv", "json")


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HistoryRow:
    """One ``(day, key)`` cell as it is stored.

    ``key`` is the storage spelling (``codex:gpt-5.6-sol``); ``model`` is the
    bare name the user may be shown. Both are carried because the export needs
    the readable one and a caller reconciling against the rollup store needs
    the exact one.
    """

    day: DayKey
    key: str
    vendor: Vendor
    usage: ModelUsage
    usd_at_record: Usd
    updated_at: float
    project: str = ""
    """Which project's share of ``(day, key)`` this row is (roadmap item 6).

    Empty for every row of ``daily``, which is the whole-day aggregate and
    has no project. Non-empty only on a :meth:`HistoryStore.project_rows`
    result, where it is one path component - see :data:`PROJECT_COLUMNS`.
    """

    @property
    def model(self) -> str:
        """The bare model name - never the storage key (contracts rule 4)."""
        return raw_model_of_key(self.key)

    @property
    def total_tokens(self) -> int:
        return self.usage.total_tokens


def _row_from_sql(row: sqlite3.Row | Sequence[Any]) -> HistoryRow:
    return HistoryRow(
        day=str(row[0]),
        key=str(row[1]),
        vendor=str(row[2]),
        usage=ModelUsage(
            input=int(row[3]),
            output=int(row[4]),
            cache_read=int(row[5]),
            cache_write_5m=int(row[6]),
            cache_write_1h=int(row[7]),
        ),
        usd_at_record=float(row[8]),
        updated_at=float(row[9]),
    )


def _project_row_from_sql(row: sqlite3.Row | Sequence[Any]) -> HistoryRow:
    """The :data:`PROJECT_COLUMNS` shape - ``project`` sits third."""
    return HistoryRow(
        day=str(row[0]),
        key=str(row[1]),
        project=str(row[2]),
        vendor=str(row[3]),
        usage=ModelUsage(
            input=int(row[4]),
            output=int(row[5]),
            cache_read=int(row[6]),
            cache_write_5m=int(row[7]),
            cache_write_1h=int(row[8]),
        ),
        usd_at_record=float(row[9]),
        updated_at=float(row[10]),
    )


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class HistoryStore:
    """The append-only mirror at ``history.sqlite``.

    A connection is opened **per call** and closed again: the worker thread
    must not hold a sqlite handle across a 300 s idle period (a handle is a
    file descriptor and a WAL reader that would keep a checkpoint pending), and
    nothing here is hot enough to care about the ~0.1 ms open.

    Every method is safe to call from the background worker thread. Our own
    calls are serialised by an :class:`threading.RLock` so two ticks cannot
    interleave a read-then-write; sqlite's own locking covers anything else on
    the machine.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        logger: Any = None,
    ) -> None:
        self._path = Path(path) if path is not None else HISTORY_DB_PATH
        self._lock = threading.RLock()
        self._errors: tuple[str, ...] = ()
        self._logger = logger
        self._quarantined: Path | None = None

    # -- introspection ----------------------------------------------------

    @property
    def path(self) -> Path:
        """Where the mirror lives."""
        return self._path

    @property
    def errors(self) -> tuple[str, ...]:
        """The last failure, as a one-line string, or ``()``.

        The menu renders this as a ``!`` line. It is a *tuple* rather than a
        string so a caller can splice several sources of diagnostics together
        without special-casing ``None``.
        """
        with self._lock:
            return self._errors

    @property
    def quarantined_path(self) -> Path | None:
        """Where a corrupt database was moved, if one was, this session."""
        with self._lock:
            return self._quarantined

    # -- writing ----------------------------------------------------------

    def upsert_day(
        self,
        day: DayKey,
        rollup: DayRollup,
        pricing: PricingTable | None = None,
        *,
        now: float | None = None,
    ) -> int:
        """Mirror one day. Returns the number of rows actually written.

        A cell whose five counters and recorded dollar figure are already what
        we would write is left alone, so re-mirroring an unchanged day is a
        no-op and ``updated_at`` keeps meaning "when this changed".
        """
        return self.upsert_days([(day, rollup)], pricing, now=now)

    def upsert_days(
        self,
        days: Iterable[DayRollup | tuple[DayKey, DayRollup]],
        pricing: PricingTable | None = None,
        *,
        now: float | None = None,
    ) -> int:
        """Mirror several days in one transaction. Returns rows written.

        Accepts :class:`DayRollup` values (which carry their own ``day``) or
        ``(day, rollup)`` pairs, because the caller sometimes has the key in
        hand and sometimes only the rollup. Days with no models contribute
        nothing - an empty day is not an erasure, and this module never
        deletes.

        Never raises: a failure is logged, recorded on :attr:`errors` and
        reported as ``0`` rows written.
        """
        stamp = time.time() if now is None else float(now)
        try:
            pending = self._prepare(days, pricing, stamp)
        except Exception as exc:  # a malformed rollup must not kill the tick
            self._fail("history: could not read the rollups", exc)
            return 0
        if not pending:
            self._clear_error()
            return 0

        written = 0
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return 0
                try:
                    existing = self._existing(conn, sorted({d for d, _k, *_ in pending}))
                    changed = [
                        row
                        for row in pending
                        if existing.get((row[0], row[1])) != row[2:9]
                    ]
                    if changed:
                        conn.executemany(_UPSERT_DAILY_SQL, changed)
                        conn.commit()
                    written = len(changed)
                finally:
                    conn.close()
        except Exception as exc:
            self._fail("history: write failed", exc)
            return 0
        self._clear_error()
        return written

    def replace_day(
        self,
        day: DayKey,
        rollup: DayRollup,
        pricing: PricingTable | None = None,
        *,
        now: float | None = None,
    ) -> int:
        """Make one day EXACTLY the rollup. Returns the rows actually written.

        The repair path for the self-audit (roadmap item 2). :meth:`upsert_day`
        is a *union*: it writes the keys it is given and leaves the rest alone,
        which is right for a mirror that is fed the same days over and over and
        wrong for the one case the audit exists to handle — a **phantom cell**.
        The opening incident of this roadmap was a store that had inflated some
        cells up to 1,650x; when the audit rebuilds a day from the corpus and
        the corpus has no ``gpt-5.6-sol`` at all that day, ``upsert_day``
        leaves the inflated ``gpt-5.6-sol`` row standing for ever and the
        mirror keeps quoting a number the aggregate has already disowned.

        So keys present in history for *day* but absent from *rollup* are
        rewritten with **zero** counters and ``usd_at_record`` 0.0 — never
        deleted. That distinction is the whole point of this module (see the
        class docstring): a row is the record that this key was once believed
        to have spent something on this day, and the honest correction is
        "and the correction is nothing", not the erasure of the belief. A
        deleted row and a row that was never written are indistinguishable
        afterwards; a zeroed row still says the audit looked.

        Cells that already hold what we would write are skipped, exactly as
        :meth:`upsert_days` skips them, so re-repairing a repaired day writes
        nothing and ``updated_at`` keeps meaning "when this changed".

        Never raises: a failure is logged, recorded on :attr:`errors` and
        reported as ``0`` rows written. Callers reach it defensively
        (``getattr(store, "replace_day", None)``) so a build without it degrades
        to no repair rather than to a broken cost job.
        """
        stamp = time.time() if now is None else float(now)
        key_of_day = str(day)
        try:
            pending = self._prepare([(key_of_day, rollup)], pricing, stamp)
        except Exception as exc:  # a malformed rollup must not kill the tick
            self._fail("history: could not read the rollups", exc)
            return 0

        written = 0
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return 0
                try:
                    existing = self._existing(conn, [key_of_day])
                    fresh = {row[1] for row in pending}
                    rows = list(pending)
                    for (stored_day, stored_key), payload in existing.items():
                        if stored_day != key_of_day or stored_key in fresh:
                            continue
                        # The vendor is carried over from the row being
                        # zeroed: it is a fact about the KEY, and re-deriving
                        # it here would be a second parser of the same string.
                        rows.append(
                            (
                                key_of_day,
                                stored_key,
                                payload[0],
                                0,
                                0,
                                0,
                                0,
                                0,
                                0.0,
                                stamp,
                            )
                        )
                    changed = [
                        row for row in rows if existing.get((row[0], row[1])) != row[2:9]
                    ]
                    if changed:
                        conn.executemany(_UPSERT_DAILY_SQL, changed)
                        conn.commit()
                    written = len(changed)
                finally:
                    conn.close()
        except Exception as exc:
            self._fail("history: write failed", exc)
            return 0
        self._clear_error()
        return written

    def upsert_projects(
        self,
        items: Iterable[tuple[DayKey, str, DayRollup]],
        pricing: PricingTable | None = None,
        *,
        now: float | None = None,
    ) -> int:
        """Mirror the per-project decomposition of several days (roadmap item 6).

        One transaction for the lot, and the same replace-not-append rule the
        aggregate rows follow, so an audit repair or a rebuild propagates into
        the project rows instead of leaving a second, stale copy beside them.

        A row whose *project* is empty is skipped rather than stored: an
        unattributable transcript already has a name of its own
        (``attribution.UNKNOWN_PROJECT``), so an empty string here means a bug
        upstream, and storing it would create a phantom project.

        Never raises: a failure is logged, recorded on :attr:`errors` and
        reported as ``0`` rows written.
        """
        stamp = time.time() if now is None else float(now)
        pending: list[tuple[Any, ...]] = []
        try:
            for day, project, rollup in items:
                name = str(project)
                if not name:
                    continue
                for row in self._prepare([(day, rollup)], pricing, stamp):
                    pending.append((row[0], row[1], name, *row[2:]))
        except Exception as exc:
            self._fail("history: could not read the project rollups", exc)
            return 0
        if not pending:
            self._clear_error()
            return 0

        written = 0
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return 0
                try:
                    existing = self._existing_projects(
                        conn, sorted({row[0] for row in pending})
                    )
                    changed = [
                        row
                        for row in pending
                        if existing.get((row[0], row[1], row[2])) != row[3:10]
                    ]
                    if changed:
                        conn.executemany(
                            "INSERT OR REPLACE INTO daily_project "
                            "(day, key, project, vendor, input, output, cache_read, "
                            " cache_write_5m, cache_write_1h, usd_at_record, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            changed,
                        )
                        conn.commit()
                    written = len(changed)
                finally:
                    conn.close()
        except Exception as exc:
            self._fail("history: project write failed", exc)
            return 0
        self._clear_error()
        return written

    def drop_project_day(
        self,
        day: DayKey,
        vendor: Vendor,
        *,
        now: float | None = None,
    ) -> int:
        """Zero one vendor's project rows for one day. Returns rows written.

        The per-project half of an audit repair (roadmap item 2, 2026-09-10).
        :meth:`replace_day` rebuilds the day's AGGREGATE from the corpus, but
        the decomposition of that day - which project the tokens belonged to -
        cannot be rebuilt from the audit's plan: the plan is per
        ``(day, model)``, and nothing in it says which project each cell came
        from. Leaving the old rows standing would let the dashboard keep
        quoting a per-project split of a figure the aggregate has already
        disowned, and the two would never agree again for that day.

        So the rows are zeroed, in the spelling :meth:`replace_day` uses and
        for the same reason: never deleted, because a row is the record that
        this key was once believed to have spent something here, and the honest
        correction is "and the correction is nothing". The live attribution
        cache drops the same ``(day, vendor)`` on the same tick, so the next
        mirror re-populates whatever the re-index attributes.

        Only *vendor*'s rows are touched - the other vendor's decomposition of
        that day is not what the audit rebuilt.

        Never raises: a failure is logged, recorded on :attr:`errors` and
        reported as ``0``.
        """
        stamp = time.time() if now is None else float(now)
        key_of_day = str(day)
        wanted = str(vendor)
        written = 0
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return 0
                try:
                    existing = self._existing_projects(conn, [key_of_day])
                    rows = [
                        (
                            stored_day,
                            stored_key,
                            project,
                            payload[0],
                            0,
                            0,
                            0,
                            0,
                            0,
                            0.0,
                            stamp,
                        )
                        for (stored_day, stored_key, project), payload in existing.items()
                        if stored_day == key_of_day
                        and payload[0] == wanted
                        and any(payload[1:])
                    ]
                    if rows:
                        conn.executemany(
                            "INSERT OR REPLACE INTO daily_project "
                            "(day, key, project, vendor, input, output, cache_read, "
                            " cache_write_5m, cache_write_1h, usd_at_record, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            rows,
                        )
                        conn.commit()
                    written = len(rows)
                finally:
                    conn.close()
        except Exception as exc:
            self._fail("history: project drop failed", exc)
            return 0
        self._clear_error()
        return written

    # -- reading ----------------------------------------------------------

    def project_rows(self, *, since: DayKey | None = None) -> tuple[HistoryRow, ...]:
        """Every stored per-project cell, ascending by day then project then key.

        ``()`` on a database that has none - a machine that has never run with
        ``cost_by_project_enabled`` on, or one still on the v1 schema. Like
        :meth:`rows`, a read never creates the file.
        """
        if not self._path.exists():
            return ()
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return ()
                try:
                    sql = f"SELECT {', '.join(PROJECT_COLUMNS)} FROM daily_project"
                    args: tuple[Any, ...] = ()
                    if since is not None:
                        parse_day_key(since)
                        sql += " WHERE day >= ?"
                        args = (since,)
                    sql += " ORDER BY day ASC, project ASC, vendor ASC, key ASC"
                    raw = conn.execute(sql, args).fetchall()
                finally:
                    conn.close()
        except Exception as exc:
            self._fail("history: project read failed", exc)
            return ()
        self._clear_error()
        return tuple(_project_row_from_sql(item) for item in raw)

    def rows(self, *, since: DayKey | None = None) -> tuple[HistoryRow, ...]:
        """Every stored cell, ascending by day then vendor then key.

        Returns ``()`` rather than raising when the database is missing or
        unreadable - an export of nothing is an honest empty file, and the
        ``!`` line says why. A read never CREATES the file either, so exporting
        on a machine with ``history_enabled`` off leaves the disk as it found
        it (the off switch has to mean off from every direction).
        """
        if not self._path.exists():
            return ()
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return ()
                try:
                    sql = f"SELECT {', '.join(COLUMNS)} FROM daily"
                    args: tuple[Any, ...] = ()
                    if since is not None:
                        parse_day_key(since)
                        sql += " WHERE day >= ?"
                        args = (since,)
                    sql += " ORDER BY day ASC, vendor ASC, key ASC"
                    raw = conn.execute(sql, args).fetchall()
                finally:
                    conn.close()
        except Exception as exc:
            self._fail("history: read failed", exc)
            return ()
        self._clear_error()
        return tuple(_row_from_sql(item) for item in raw)

    def days(self) -> tuple[DayKey, ...]:
        """Every day held, ascending. ``()`` when there is nothing.

        Like :meth:`rows`, a read of a database that does not exist is empty
        rather than a database that now does.
        """
        if not self._path.exists():
            return ()
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return ()
                try:
                    raw = conn.execute(
                        "SELECT DISTINCT day FROM daily ORDER BY day ASC"
                    ).fetchall()
                finally:
                    conn.close()
        except Exception as exc:
            self._fail("history: read failed", exc)
            return ()
        self._clear_error()
        return tuple(str(item[0]) for item in raw)

    def count(self) -> int:
        """Number of ``(day, key)`` cells held; ``0`` on any failure.

        Reads do not create the file - see :meth:`rows`.
        """
        if not self._path.exists():
            return 0
        try:
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return 0
                try:
                    raw = conn.execute("SELECT COUNT(*) FROM daily").fetchone()
                finally:
                    conn.close()
        except Exception as exc:
            self._fail("history: read failed", exc)
            return 0
        self._clear_error()
        return int(raw[0]) if raw else 0

    # -- internals --------------------------------------------------------

    def _prepare(
        self,
        days: Iterable[DayRollup | tuple[DayKey, DayRollup]],
        pricing: PricingTable | None,
        stamp: float,
    ) -> list[tuple[Any, ...]]:
        """Flatten the caller's days into insertable tuples.

        Pricing is resolved **here**, against each day's own date, so the
        stored ``usd_at_record`` is what that day cost at the rates in effect
        on it (SPEC 3.4) rather than at whatever the table says today. A price
        table that raises contributes ``0.0`` for that cell rather than losing
        the token counts, which are the part that cannot be recomputed.
        """
        out: list[tuple[Any, ...]] = []
        for item in days:
            if isinstance(item, DayRollup):
                day, rollup = item.day, item
            else:
                day, rollup = item
                if not isinstance(rollup, DayRollup):
                    raise TypeError(
                        f"expected DayRollup, got {type(rollup)!r}"
                    )
            day = str(day)
            day_date = parse_day_key(day)
            for key, usage in rollup.models.items():
                if not isinstance(usage, ModelUsage) or usage.is_zero:
                    continue
                out.append(
                    (
                        day,
                        str(key),
                        vendor_of_key(str(key)),
                        int(usage.input),
                        int(usage.output),
                        int(usage.cache_read),
                        int(usage.cache_write_5m),
                        int(usage.cache_write_1h),
                        _price(pricing, str(key), usage, day_date),
                        stamp,
                    )
                )
        return out

    @staticmethod
    def _existing(
        conn: sqlite3.Connection, days: Sequence[DayKey]
    ) -> dict[tuple[str, str], tuple[Any, ...]]:
        """The already-stored payload of every cell in *days*.

        Read in one statement per 500 days so the idempotence check costs one
        round trip rather than one per row; the comparison tuple deliberately
        excludes ``updated_at`` (which is the thing being decided) and includes
        ``usd_at_record`` (a re-priced day IS a change worth recording).
        """
        out: dict[tuple[str, str], tuple[Any, ...]] = {}
        chunk = 500
        for start in range(0, len(days), chunk):
            batch = days[start : start + chunk]
            marks = ",".join("?" * len(batch))
            sql = (
                "SELECT day, key, vendor, input, output, cache_read, "
                "cache_write_5m, cache_write_1h, usd_at_record "
                f"FROM daily WHERE day IN ({marks})"
            )
            for row in conn.execute(sql, tuple(batch)):
                out[(str(row[0]), str(row[1]))] = (
                    str(row[2]),
                    int(row[3]),
                    int(row[4]),
                    int(row[5]),
                    int(row[6]),
                    int(row[7]),
                    float(row[8]),
                )
        return out

    @staticmethod
    def _existing_projects(
        conn: sqlite3.Connection, days: Sequence[DayKey]
    ) -> dict[tuple[str, str, str], tuple[Any, ...]]:
        """:meth:`_existing`, at the project grain."""
        out: dict[tuple[str, str, str], tuple[Any, ...]] = {}
        chunk = 500
        for start in range(0, len(days), chunk):
            batch = days[start : start + chunk]
            marks = ",".join("?" * len(batch))
            sql = (
                "SELECT day, key, project, vendor, input, output, cache_read, "
                "cache_write_5m, cache_write_1h, usd_at_record "
                f"FROM daily_project WHERE day IN ({marks})"
            )
            for row in conn.execute(sql, tuple(batch)):
                out[(str(row[0]), str(row[1]), str(row[2]))] = (
                    str(row[3]),
                    int(row[4]),
                    int(row[5]),
                    int(row[6]),
                    int(row[7]),
                    int(row[8]),
                    float(row[9]),
                )
        return out

    def _connect(self) -> sqlite3.Connection | None:
        """Open (creating and migrating on the way) or ``None`` on failure.

        A file that is not a database - truncated by a full disk, half-synced
        by a backup tool, or simply something else with our name - is moved
        aside to ``history.sqlite.corrupt`` and a fresh one is created, once.
        That is not a deletion: the bytes are still there to look at, and the
        alternative (refusing to record anything until a human notices) loses
        every day from here on. A ``!`` line names it either way.
        """
        for attempt in (0, 1):
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fresh = not self._path.exists()
                conn = sqlite3.connect(str(self._path), timeout=5.0)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:
                    # A filesystem that cannot do WAL (some network mounts)
                    # still does the default rollback journal perfectly well.
                    pass
                conn.execute(_TABLE_SQL)
                conn.execute(_INDEX_SQL)
                _migrate_project_table(conn)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.commit()
                if fresh:
                    self._harden()
                return conn
            except Exception as exc:
                try:
                    conn.close()  # type: ignore[possibly-undefined]
                except Exception:
                    pass
                if attempt == 0 and _is_corruption(exc) and self._path.exists():
                    self._quarantine(exc)
                    continue
                self._fail(f"history: cannot open {self._path.name}", exc)
                return None
        return None

    def _quarantine(self, exc: BaseException) -> None:
        """Move an unusable database aside so a fresh one can be created."""
        target = self._path.with_name(self._path.name + ".corrupt")
        try:
            os.replace(self._path, target)
        except OSError as move_exc:
            self._fail("history: corrupt database could not be moved", move_exc)
            return
        for suffix in ("-wal", "-shm"):
            side = self._path.with_name(self._path.name + suffix)
            try:
                side.unlink()
            except OSError:
                pass
        with self._lock:
            self._quarantined = target
        self._log(
            f"history: {self._path.name} was unreadable ({exc}); "
            f"moved to {target.name} and started a new one"
        )
        with self._lock:
            self._errors = (
                f"history: database was unreadable, moved to {target.name}",
            )

    def _harden(self) -> None:
        """0600 the database and its sidecars. Best effort, never fatal."""
        for name in ("", "-wal", "-shm"):
            side = self._path.with_name(self._path.name + name)
            try:
                if side.exists():
                    os.chmod(side, 0o600)
            except OSError:
                pass

    def _fail(self, summary: str, exc: BaseException) -> None:
        message = f"{summary}: {exc.__class__.__name__}: {exc}"[:200]
        self._log(message)
        with self._lock:
            self._errors = (message,)

    def _clear_error(self) -> None:
        with self._lock:
            if self._errors and self._quarantined is None:
                self._errors = ()

    def _log(self, message: str) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            logger(message)
        except Exception:
            pass


def _migrate_project_table(conn: sqlite3.Connection) -> None:
    """v1 -> v2: create ``daily_project`` beside ``daily`` (roadmap item 6).

    Additive by construction. ``CREATE TABLE IF NOT EXISTS`` reads no row of
    ``daily``, rewrites none, and is a no-op on a database that already has the
    table - which is what makes it safe to run on every connect rather than
    trusting ``PRAGMA user_version`` (a version written by a build that never
    finished its work would otherwise skip the step forever).

    Downgrade is equally uneventful: a v1 binary opening a v2 file simply never
    reads the extra table.
    """
    conn.execute(_PROJECT_TABLE_SQL)
    conn.execute(_PROJECT_INDEX_SQL)


HISTORY_DB_ENV: Final[str] = "CC_USAGE_WIDGET_HISTORY_DB"
"""Environment override for the mirror's location. An explicit pin wins over
the co-location rule below."""


def history_path_for(rollups_path: Path | str | None) -> Path:
    """Where the mirror for a given rollup store lives.

    **Beside the aggregate it mirrors.** In production ``rollups.json`` sits in
    :data:`~cc_usage_widget.contracts.WIDGET_HOME`, so this is
    :data:`~cc_usage_widget.contracts.HISTORY_DB_PATH` and nothing moves. The
    rule earns its keep everywhere else: a test (or a second widget home, or a
    ``--rollups`` override) that redirects the cache into a temporary directory
    gets its mirror redirected with it, rather than quietly appending fixture
    days to the operator's real long-term record. That is not a hypothetical —
    the existing cost-job regression tests drive the real ``_run_cost_job``, and
    without this they wrote into the installed widget's history.

    An explicit ``CC_USAGE_WIDGET_HISTORY_DB`` wins over the co-location: an
    operator who names a path means it.
    """
    if os.environ.get(HISTORY_DB_ENV):
        return HISTORY_DB_PATH
    if rollups_path is None:
        return HISTORY_DB_PATH
    return Path(rollups_path).parent / HISTORY_DB_PATH.name


def open_history(
    path: Path | str | None = None, *, logger: Any = None
) -> HistoryStore:
    """Construct a :class:`HistoryStore`. Never touches the disk by itself."""
    return HistoryStore(path, logger=logger)


def _price(
    pricing: PricingTable | None, key: str, usage: ModelUsage, day: dt.date
) -> Usd:
    """``pricing.cost_usd`` with every failure mode flattened to ``0.0``."""
    if pricing is None:
        return 0.0
    try:
        value = pricing.cost_usd(key, usage, day)
    except Exception:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    return value


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

EXPORT_FIELDS: Final[tuple[str, ...]] = (
    "day",
    "vendor",
    "model",
    "input",
    "output",
    "cache_read",
    "cache_write_5m",
    "cache_write_1h",
    "total_tokens",
    "usd_at_record_notional",
    "usd_at_today_rates_notional",
)
"""Export columns. The two dollar columns carry ``notional`` in their *names*
because a CSV has nowhere else to put :data:`NOTIONAL_LABEL` - a comment line
would break every parser that opens it (SPEC 4.3 honesty, in a file format
that has no room for prose)."""


def export_filename(fmt: str, *, today: DayKey | None = None) -> str:
    """``usage-bar-2026-09-10.csv``. Raises ``ValueError`` on a bad format."""
    key = str(fmt).lower()
    if key not in EXPORT_FORMATS:
        raise ValueError(f"unknown export format {fmt!r}; expected one of {EXPORT_FORMATS}")
    day = today if today is not None else local_day_key(time.time())
    parse_day_key(day)
    return f"usage-bar-{day}.{key}"


def export_history(
    store: HistoryStore,
    pricing: PricingTable | None = None,
    *,
    directory: Path | str,
    fmt: str = "csv",
    today: DayKey | None = None,
) -> Path:
    """Write every stored row to ``<directory>/usage-bar-<day>.<fmt>``.

    *directory* is **required**, deliberately. This module never decides where
    a user's file lands: ``app.py`` passes :data:`DEFAULT_EXPORT_DIR`
    (``~/Downloads``) and a test passes a
    :class:`~tempfile.TemporaryDirectory`. A default here would be a fallback
    a bug could reach silently - and the one time it existed, a mutation
    experiment wrote two files into the real Downloads folder while every test
    still reported green.

    Each row carries two dollar figures - what it cost at the rates recorded on
    the day, and what the same tokens would cost at today's table - so a price
    change reads as a difference instead of quietly rewriting the past. Both
    are notional.

    Unlike everything else in this module this DOES raise on a write failure:
    it is a user-initiated action with a menu behind it, and a silent no-op
    would look exactly like a successful export of nothing.
    """
    key = str(fmt).lower()
    name = export_filename(key, today=today)
    day = today if today is not None else local_day_key(time.time())
    day_date = parse_day_key(day)
    if directory is None:
        raise ValueError("export_history requires an explicit directory")
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / name

    rows = store.rows()
    records = [_export_record(row, pricing, day_date) for row in rows]

    if key == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_FIELDS))
        writer.writeheader()
        for record in records:
            writer.writerow(record)
        text = buffer.getvalue()
    else:
        text = json.dumps(
            {
                "version": SCHEMA_VERSION,
                "generated_at": time.time(),
                "generated_day": day,
                "notional_label": NOTIONAL_LABEL,
                "row_count": len(records),
                "rows": records,
            },
            indent=2,
            sort_keys=False,
        )
        text += "\n"

    # Written whole, then 0600'd: this is a record of someone's spend sitting
    # in a shared-ish folder, and the widget's own state files are 0600 for the
    # same reason.
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _export_record(
    row: HistoryRow, pricing: PricingTable | None, day: dt.date
) -> dict[str, Any]:
    return {
        "day": row.day,
        "vendor": row.vendor,
        "model": row.model,
        "input": row.usage.input,
        "output": row.usage.output,
        "cache_read": row.usage.cache_read,
        "cache_write_5m": row.usage.cache_write_5m,
        "cache_write_1h": row.usage.cache_write_1h,
        "total_tokens": row.usage.total_tokens,
        "usd_at_record_notional": round(float(row.usd_at_record), 6),
        "usd_at_today_rates_notional": round(
            _price(pricing, row.key, row.usage, day), 6
        ),
    }
