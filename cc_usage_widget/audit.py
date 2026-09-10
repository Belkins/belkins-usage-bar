"""Daily self-audit of the rollup store (roadmap item 2).

Why this module exists
----------------------

``rollups.json`` is an aggregate that nothing ever re-derives. Before the
per-file contribution ledger it was add-only, and a rollout re-read from byte 0
was counted again - live day/model cells were inflated **up to 1,650x** and
nothing in the widget noticed for weeks. The ledger makes the addition
reversible; this module is the part that would have *seen* the problem.

Once per local day, on the cost cadence, it re-indexes the last two days of
every wired corpus into a :class:`tempfile.TemporaryDirectory` using the very
same indexer classes, compares the result with the live store cell by cell, and
repairs the cells that disagree. Two days of the 15 GB Codex corpus is
sub-second (the whole corpus was measured at 14.9 s), and the Claude corpus in
that window is a handful of files.

Design rules it is worth not re-deriving
----------------------------------------

* **Same class, not a second reader.** An audit that agrees with a different
  implementation proves nothing about the one that ships. The twin comes from
  ``indexer.clone_for_audit`` so parser, dedup and clock are identical; only the
  window and the state path differ.
* **Never repair from an incomplete index.** A fresh index cut short by the
  budget reads as "the store has far too much", and repairing from it would
  destroy real usage. An audit that could not finish reports an error and
  changes nothing.
* **Only the vendors that were audited.** A machine where Codex is switched off
  must not have its Codex cells zeroed because no Codex twin ran.
* **Never on the AppKit thread.** It is called from the cost job, on the worker
  thread, like every other scan.
* **The audit thread never mutates the live store.** It reads, it compares, and
  it hands back a repair *plan* - plain data. The worker applies that plan on
  its own thread, inside the cost job, through one atomic
  :meth:`~cc_usage_widget.rollup.DailyRollupStore.replace_day_for_vendor` call
  per ``(day, vendor)``. The earlier design repaired from the daemon thread with
  no coordination at all: a retract-then-merge interleaved with the worker's own
  retract-then-merge can lose a whole tick's deltas, and a reader could see a
  day with its cells taken out and not yet put back. Nothing about the audit is
  worth that.
* **Everything it needs from the live scanners is snapshotted on the worker**
  (:meth:`SelfAudit.snapshot_scanners`) before the thread starts, because
  ``tombstone_rollups`` and ``_ensure_states_loaded`` read the very dict a scan
  is rewriting.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .contracts import (
    AUDIT_STATE_PATH,
    DayKey,
    DayRollup,
    ModelKey,
    ModelUsage,
    Vendor,
    day_keys_back,
    format_tokens,
    local_day_key,
    raw_model_of_key,
    vendor_label,
    vendor_of_key,
)
from .rollup import DailyRollupStore

__all__ = [
    "AuditCell",
    "AuditRepair",
    "AuditResult",
    "AuditSource",
    "SelfAudit",
    "note_with_tail",
    "NOTE_TAIL_SEPARATOR",
    "PENDING_TAIL",
    "AUDIT_DAYS",
    "AUDIT_DRIFT_FRACTION",
    "AUDIT_DRIFT_MIN_TOKENS",
    "AUDIT_BUDGET_SECONDS",
]

_LOG = logging.getLogger(__name__)

NOTE_TAIL_SEPARATOR: str = " — "
"""What separates an audit note's finding from its VERDICT.

The verdict is rewritten by the worker once it knows whether the plan actually
landed, so where it starts has to be stated once rather than parsed twice. An
em dash surrounded by spaces appears nowhere else in the line: cell
descriptions are ``vendor day model ratio`` and caveats are parenthesised.
"""

PENDING_TAIL: str = "repair pending"
"""The verdict of a note whose plan has not been applied yet.

Not ``rebuilt``: at the moment the audit thread writes the note, nothing has
been. The plan goes to the worker, which rewrites the tail on the next cost
tick - to ``rebuilt`` if it landed, or to ``NOT rebuilt - <reason>`` if it did
not (SPEC 3.2a).
"""


def note_with_tail(note: str, tail: str) -> str:
    """Replace an audit note's verdict, keeping everything before it.

    Splits on the LAST :data:`NOTE_TAIL_SEPARATOR`, so a note that has already
    been rewritten can be rewritten again and a note that somehow has no tail
    simply gains one.
    """
    head, sep, _old = str(note).rpartition(NOTE_TAIL_SEPARATOR)
    return f"{head}{sep}{tail}" if sep else f"{note}{NOTE_TAIL_SEPARATOR}{tail}"

AUDIT_DAYS: int = 2
"""How many local days back the audit re-indexes, today included.

Two, because that is what a day boundary needs: at 00:05 today holds minutes
and yesterday holds the work. Wider windows cost linearly and catch nothing a
daily tick would not have caught the day before.
"""

AUDIT_DRIFT_FRACTION: float = 0.005
"""Relative threshold: a cell must differ by more than 0.5 % to count."""

AUDIT_DRIFT_MIN_TOKENS: int = 100_000
"""Absolute threshold, applied **as well as** the relative one.

Both must be exceeded. A small cell can differ by a large fraction for entirely
honest reasons - a record written between the live scan and the audit scan, a
transcript still being appended to - and a `!` line that fires every morning is
a `!` line the operator stops reading. 100k tokens is ~$1 of Astra and well
under any real inflation, which ran to whole orders of magnitude.
"""

AUDIT_BUDGET_SECONDS: float = 90.0
"""Wall-clock ceiling for one audit, across every vendor.

Not a correctness bound - it is the guard that stops a pathological corpus from
holding the cost job for minutes. Hitting it aborts the audit; it never
produces a partial verdict.
"""

_MAX_PASSES: int = 500
"""Belt to the budget's braces: a scanner that never reports ``complete`` must
not spin forever."""


@dataclass(frozen=True, slots=True)
class AuditCell:
    """One ``(day, key)`` cell where the live store and a fresh index disagree."""

    day: DayKey
    key: ModelKey
    live_tokens: int
    fresh_tokens: int

    @property
    def vendor(self) -> Vendor:
        """Which vendor's cell this is."""
        return vendor_of_key(self.key)

    @property
    def model(self) -> str:
        """The bare model name, without our storage prefix."""
        return raw_model_of_key(self.key)

    @property
    def drift_tokens(self) -> int:
        """Absolute size of the disagreement, in tokens."""
        return abs(self.live_tokens - self.fresh_tokens)

    @property
    def ratio(self) -> float | None:
        """``live / fresh``, or ``None`` when the corpus says zero.

        ``None`` is not "no drift": it is the inflation case with nothing to
        divide by, and the renderer says so in tokens instead of inventing a
        multiplier.
        """
        if self.fresh_tokens <= 0:
            return None
        return self.live_tokens / self.fresh_tokens

    def describe(self) -> str:
        """``codex 2026-09-09 gpt-5.6-sol 12.7x`` - the worst cell, named."""
        ratio = self.ratio
        detail = (
            f"{ratio:.1f}x"
            if ratio is not None
            else f"+{format_tokens(self.drift_tokens)} tok"
        )
        return f"{self.vendor} {self.day} {self.model} {detail}"


@dataclass(frozen=True, slots=True)
class AuditRepair:
    """One ``(day, vendor)`` repair, as plain data the WORKER thread applies.

    The audit computes these and touches nothing. ``fresh`` is what a full
    re-index of that day says the vendor's cells hold; ``observed`` is what the
    live store held at the moment the plan was computed. Both go to
    :meth:`~cc_usage_widget.rollup.DailyRollupStore.replace_day_for_vendor`,
    which applies the difference between them rather than the absolute figure -
    so a delta the worker merged while the audit was running survives the
    repair instead of being overwritten by a stale snapshot. Those deltas are
    bytes the indexer has already consumed and will never re-read, so losing
    them would be permanent.

    Pairs rather than dicts because this crosses a thread boundary and a frozen
    shape that cannot be edited in flight is the whole point (contracts rule 5).
    """

    day: DayKey
    vendor: Vendor
    fresh: tuple[tuple[ModelKey, ModelUsage], ...] = ()
    observed: tuple[tuple[ModelKey, ModelUsage], ...] = ()
    generation: int = 0
    """:attr:`DailyRollupStore.generation` this plan was computed against.

    The plan is a *correction* - ``current - observed + fresh`` - so it is only
    meaningful against the same series of days it was computed from. A
    ``Rebuild cost index`` (or a restore) between the plan and the tick that
    applies it makes ``observed`` describe days nobody holds any more, and
    subtracting it takes the re-index's own work straight back out:
    ``max(0, 1M - 50M + 1M) = 0`` - the day silently zeroed. The pre-existing
    guard only recognised an EMPTY store, which a rebuild stops being after its
    very first delta. ``_apply_audit_repairs`` refuses any plan whose
    generation is not the store's.
    """

    @property
    def fresh_models(self) -> dict[ModelKey, ModelUsage]:
        """``fresh`` as the mapping the store's repair entry point takes."""
        return dict(self.fresh)

    @property
    def observed_models(self) -> dict[ModelKey, ModelUsage]:
        """``observed`` as the mapping the store's repair entry point takes."""
        return dict(self.observed)


@dataclass(frozen=True, slots=True)
class AuditSource:
    """Everything the audit needs from one live scanner, snapshotted.

    Built on the WORKER thread by :meth:`SelfAudit.snapshot_scanners`, because
    ``tombstone_rollups`` reads (and can load from disk) the same scan state a
    scan is rewriting. The audit thread then works from this frozen shape and
    calls back into the live object exactly once, for ``clone_for_audit``,
    which only reads immutable configuration.
    """

    vendor: Vendor
    clone: Callable[..., Any] | None = None
    tombstones: tuple[DayRollup, ...] = ()
    legacy_days: tuple[DayKey, ...] = ()
    offsets: tuple[tuple[str, int, int, tuple[DayKey, ...]], ...] = ()
    """``(path, inode, offset, in-window ledger days)`` per live scan-state entry.

    The bound the twin reads to. Without it the twin re-indexed every file to
    its CURRENT end while the live scanner had consumed only part of it, so the
    audit's "fresh" total included bytes the store does not hold yet: the
    repair installed them, and the same cost tick merged the same tail again
    (probe: 10M indexed, 5M appended, audit and apply gave 15M, the scan 20M -
    truth 15M). With it, "fresh" describes exactly the bytes the store holds.
    """
    bounded: bool = False
    """True when the twin can actually be held to :attr:`offsets`.

    False for a scanner that offers no ``audit_offsets``, or whose
    ``clone_for_audit`` does not take ``limits`` - a test double, an older
    build. Such a vendor is compared and REPORTED but never repaired: its
    fresh figures describe a different set of bytes from the store's, and a
    plan built on that is the very bug this field exists to stop.
    """
    error: str | None = None

    @property
    def limits(self) -> dict[str, tuple[int, int]]:
        """:attr:`offsets` as the mapping ``clone_for_audit`` takes."""
        return {path: (inode, offset) for path, inode, offset, _days in self.offsets}

    def days_of(self, paths: Iterable[str]) -> tuple[DayKey, ...]:
        """In-window ledger days of *paths*, from the snapshot.

        Which days become "not comparable" when the twin could not read those
        files: precisely the days the live entry says it contributed to, which
        are the cells of the store that the fresh index can no longer account
        for. A path with an empty ledger costs nothing - it put nothing in.
        """
        wanted = {str(path) for path in paths}
        days: set[DayKey] = set()
        for path, _inode, _offset, entries in self.offsets:
            if path in wanted:
                days.update(entries)
        return tuple(sorted(days))

    @property
    def has_legacy(self) -> bool:
        """True when this vendor still holds a pre-ledger tombstone in window.

        The repair veto: some of this vendor's usage is in ``rollups.json`` with
        nothing on the scanner side able to describe it, so a fresh index cannot
        reproduce it and "drift" cannot be told apart from it.
        """
        return bool(self.legacy_days)


@dataclass(frozen=True, slots=True)
class AuditResult:
    """What one audit pass found, and what it asked the worker to do."""

    ran_at: float = 0.0
    days: tuple[DayKey, ...] = ()
    vendors: tuple[Vendor, ...] = ()
    cells_compared: int = 0
    drifted: tuple[AuditCell, ...] = ()
    repaired_days: tuple[DayKey, ...] = ()
    """Days the plan covers. Named for what the note says about them; the
    repair itself lands on the worker's next tick, before the note is
    published."""
    repairs: tuple[AuditRepair, ...] = ()
    """The plan. Applied by the worker, never by the audit thread."""
    legacy_vendors: tuple[Vendor, ...] = ()
    """Vendors whose drift was NOT repaired because their scanner still holds a
    legacy tombstone in the audited window."""
    not_comparable: tuple[tuple[DayKey, Vendor], ...] = ()
    """``(day, vendor)`` pairs the fresh index could not describe.

    A file the live scanner had an offset into was replaced, truncated or
    deleted between the snapshot and the audit, so the store holds a
    contribution this index cannot reproduce and the difference between the two
    is not drift. Reported in the note, never repaired - or, when a vendor's
    twin could not be bounded at all (:attr:`AuditSource.bounded` False), the
    whole audited window for that vendor.
    """
    error: str | None = None

    @property
    def clean(self) -> bool:
        """True when the store agreed with the corpus everywhere it looked."""
        return self.error is None and not self.drifted

    @property
    def note(self) -> str | None:
        """The line for :attr:`UiSnapshot.audit_note`, or ``None``.

        ``None`` on a clean audit: a `!` line that is always on says nothing.
        An audit that could not run says so rather than staying quiet - a
        verifier whose failure is invisible is not a verifier (Rule 12). A
        refused repair says why, in the same line: the operator needs to know
        the number was left alone deliberately, not that the audit failed.

        The tail is :data:`PENDING_TAIL` while a plan exists, **never**
        ``rebuilt``. Nothing has been rebuilt at this point: the audit thread
        only produces data, and the worker may still drop the plan on the next
        tick (a rebuild in between, a store that cannot repair a day, a repair
        that raised). Claiming the repair in advance is what made the note say
        "rebuilt" about days that were left exactly as they were; the worker
        rewrites this tail with :func:`note_with_tail` once it knows.
        """
        return self.note_with_tail(
            PENDING_TAIL if self.repairs else "NOT rebuilt - no repairable cell"
        )

    def note_with_tail(self, tail: str) -> str | None:
        """This audit's note with *tail* as its verdict, or ``None`` if silent.

        The one place the line is built, so the worker's rewritten verdict
        cannot drift from the audit's own wording.
        """
        if self.error is not None:
            return f"audit: could not complete — {self.error}"
        if not self.drifted:
            return None
        worst = max(self.drifted, key=lambda cell: cell.drift_tokens)
        count = len(self.drifted)
        plural = "cell" if count == 1 else "cells"
        caveats: list[str] = []
        if self.legacy_vendors:
            names = ", ".join(vendor_label(v) for v in self.legacy_vendors)
            caveats.append(f"pre-ledger history: {names}")
        if self.not_comparable:
            days = len({day for day, _vendor in self.not_comparable})
            caveats.append(f"{days} day(s) not comparable")
        detail = f" ({'; '.join(caveats)})" if caveats else ""
        return (
            f"audit: {count} {plural} drifted ({worst.describe()}){detail}"
            f"{NOTE_TAIL_SEPARATOR}{tail}"
        )

    @property
    def log_line(self) -> str:
        """The one line this audit writes to the log, drift or no drift."""
        if self.error is not None:
            return f"audit: could not complete — {self.error}"
        if not self.drifted:
            return f"audit: 0 drift ({self.cells_compared} cells)"
        return self.note or ""


def _accepts_limits(clone: Any) -> bool:
    """Whether *clone* is a ``clone_for_audit`` that takes ``limits=``.

    A scanner without it cannot be held to the live offsets, so its twin reads
    the corpus as it is NOW - which is the double count roadmap item 2's fix
    exists to stop. Rather than pass the argument and hope, the audit asks, and
    reports such a vendor instead of repairing it.
    """
    if not callable(clone):
        return False
    try:
        return "limits" in inspect.signature(clone).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False


def _cells(store: Any, day: DayKey) -> dict[ModelKey, int]:
    """``{key: total tokens}`` for one day of a store, zero cells dropped."""
    rollup = store.get(day)
    if rollup is None:
        return {}
    return {
        key: usage.total_tokens
        for key, usage in rollup.models.items()
        if not usage.is_zero
    }


@dataclass
class SelfAudit:
    """The once-a-day integrity tick.

    Args:
        state_path: the sidecar remembering the last audit. Default
            :data:`~cc_usage_widget.contracts.AUDIT_STATE_PATH`.
        days: how many days back to re-index (:data:`AUDIT_DAYS`).
        budget_seconds: wall-clock ceiling (:data:`AUDIT_BUDGET_SECONDS`).
        now: injected clock, so a test can drive the day boundary.
    """

    state_path: Path = AUDIT_STATE_PATH

    @classmethod
    def beside(cls, store_path: Any, **kwargs: Any) -> SelfAudit:
        """A :class:`SelfAudit` whose sidecar sits next to *store_path*.

        The sidecar belongs with the thing it describes: production puts
        ``rollups.json`` in ``WIDGET_HOME`` and gets ``audit_state.json``
        beside it, and a test pointing its store at a ``TemporaryDirectory``
        gets one there instead of writing into the installed package.
        """
        try:
            path = Path(store_path).with_name("audit_state.json")
        except (TypeError, ValueError):
            path = AUDIT_STATE_PATH
        return cls(state_path=path, **kwargs)
    days: int = AUDIT_DAYS
    budget_seconds: float = AUDIT_BUDGET_SECONDS
    now: Callable[[], float] = time.time
    _state: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)

    # -- sidecar ---------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            raw = None
        self._state = dict(raw) if isinstance(raw, dict) else {}

    def _save(self) -> None:
        """Persist the sidecar. A failure is logged, never raised - the audit
        is a check, and a check that can crash the tick is worse than one that
        occasionally runs twice."""
        path = Path(self.state_path)
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(self._state, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            _LOG.warning("audit state not saved: %s", exc)
            try:
                tmp.unlink()
            except OSError:
                pass

    @property
    def last_audit_day(self) -> DayKey | None:
        """The local day the last audit ran on, or ``None``."""
        self._load()
        value = self._state.get("last_audit_day")
        return value if isinstance(value, str) and value else None

    @property
    def last_audit_at(self) -> float | None:
        """When the last audit ran, as a POSIX timestamp, or ``None``."""
        self._load()
        value = self._state.get("last_audit_at")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    @property
    def last_drift_cells(self) -> int:
        """Cells that drifted on the last audit (``0`` when it was clean)."""
        self._load()
        value = self._state.get("last_drift_cells")
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, value)

    @property
    def last_error(self) -> str | None:
        """Why the last audit could not complete, or ``None``."""
        self._load()
        value = self._state.get("last_error")
        return value if isinstance(value, str) and value else None

    def status_label(self) -> str | None:
        """``Last audit: 11:40 · 0 drift`` for the Settings menu, or ``None``.

        ``None`` before the first audit has run: a line claiming an audit that
        never happened is worse than no line.
        """
        at = self.last_audit_at
        if at is None:
            return None
        clock = time.strftime("%H:%M", time.localtime(at))
        if self.last_error:
            return f"Last audit: {clock} · incomplete"
        cells = self.last_drift_cells
        return f"Last audit: {clock} · {cells} drift"

    def due(self, *, today: DayKey | None = None) -> bool:
        """True when no audit has run on *today* yet."""
        day = today if today is not None else local_day_key(self.now())
        return self.last_audit_day != day

    def mark_ran(self, result: AuditResult, *, today: DayKey | None = None) -> None:
        """Record that an audit ran, so it does not run again today."""
        self._load()
        day = today if today is not None else local_day_key(self.now())
        self._state["last_audit_day"] = day
        self._state["last_audit_at"] = result.ran_at or self.now()
        self._state["last_drift_cells"] = len(result.drifted)
        if result.error:
            self._state["last_error"] = result.error[:200]
        else:
            self._state.pop("last_error", None)
        self._save()

    # -- the audit itself -------------------------------------------------

    def snapshot_scanners(
        self,
        scanners: Sequence[tuple[Vendor, Any]],
        *,
        today: DayKey | None = None,
    ) -> tuple[AuditSource, ...]:
        """Freeze what the audit needs from each live scanner. **Worker thread.**

        Call this before starting the audit thread and hand the result to
        :meth:`run`. It is the whole of the audit's contact with the live
        scanners' mutable state: ``tombstone_rollups`` reads the scan-state dict
        a scan rewrites entry by entry and can load it from disk on the way, so
        calling it from the audit's own thread raced every tick. A scanner that
        raises here does not kill the tick - the failure rides along in
        :attr:`AuditSource.error` and becomes the audit's error instead.
        """
        day = today if today is not None else local_day_key(self.now())
        since = min(day_keys_back(day, max(1, int(self.days))))
        out: list[AuditSource] = []
        for vendor, scanner in scanners:
            clone = getattr(scanner, "clone_for_audit", None)
            tombstones: tuple[DayRollup, ...] = ()
            legacy: tuple[DayKey, ...] = ()
            offsets: tuple[tuple[str, int, int, tuple[DayKey, ...]], ...] = ()
            error: str | None = None
            reader = getattr(scanner, "tombstone_rollups", None)
            if callable(reader):
                try:
                    tombstones = tuple(reader(since=since))
                except Exception as exc:
                    error = f"{vendor} tombstones unreadable: {exc!r}"[:200]
            legacy_reader = getattr(scanner, "legacy_tombstone_days", None)
            if error is None and callable(legacy_reader):
                try:
                    legacy = tuple(legacy_reader(since=since))
                except Exception as exc:
                    error = f"{vendor} tombstones unreadable: {exc!r}"[:200]
            # The offsets, read here for the same reason the tombstones are:
            # `_states` is the dict a scan rewrites entry by entry, and this is
            # the only thread allowed to look at it.
            offset_reader = getattr(scanner, "audit_offsets", None)
            if error is None and callable(offset_reader):
                try:
                    offsets = tuple(offset_reader(since=since))
                except Exception as exc:
                    error = f"{vendor} offsets unreadable: {exc!r}"[:200]
            out.append(
                AuditSource(
                    vendor=vendor,
                    clone=clone if callable(clone) else None,
                    tombstones=tombstones,
                    legacy_days=legacy,
                    offsets=offsets,
                    bounded=bool(offsets) and _accepts_limits(clone),
                    error=error,
                )
            )
        return tuple(out)

    def run(
        self,
        store: Any,
        scanners: Sequence[AuditSource] | Sequence[tuple[Vendor, Any]],
        *,
        today: DayKey | None = None,
        repair: bool = True,
    ) -> AuditResult:
        """Re-index the recent window, compare with *store*, plan the repair.

        *scanners* is either the :class:`AuditSource` tuple
        :meth:`snapshot_scanners` produced on the worker thread (what the wired
        app passes) or the raw ``(vendor, scanner)`` sequence, which is
        snapshotted here - convenient for a caller that is already on the only
        thread touching those scanners. A scanner without ``clone_for_audit`` is
        skipped and its vendor is simply not audited - never treated as "this
        vendor has no usage".

        **Reads the store, never writes it.** With ``repair`` on, the disagreeing
        cells come back as :attr:`AuditResult.repairs` for the worker to apply.
        """
        started = time.monotonic()
        ran_at = self.now()
        # Sampled BEFORE the twin drains (up to budget_seconds): a rebuild that
        # lands during the audit bumps the store's generation, and a plan
        # stamped with the value read afterwards would pass the worker's
        # mismatch check and count the day twice (verifier, 2026-09-10).
        generation = int(getattr(store, "generation", 0) or 0)
        day = today if today is not None else local_day_key(ran_at)
        window = tuple(sorted(day_keys_back(day, max(1, int(self.days)))))
        deadline = started + max(1.0, float(self.budget_seconds))

        sources = self._as_sources(scanners, today=day)
        for source in sources:
            if source.error:
                return AuditResult(ran_at=ran_at, days=window, error=source.error)

        twins: list[tuple[Vendor, Any]] = []
        with tempfile.TemporaryDirectory(prefix="usage-bar-audit-") as name:
            root = Path(name)
            for source in sources:
                if source.clone is None:
                    continue
                try:
                    if source.bounded:
                        # The whole of the fix: the twin may read each file
                        # only as far as the LIVE scanner had, so `fresh`
                        # describes the bytes the store actually holds.
                        twin = source.clone(
                            root,
                            lookback_days=max(1, int(self.days)),
                            limits=source.limits,
                        )
                    else:
                        twin = source.clone(root, lookback_days=max(1, int(self.days)))
                except Exception as exc:  # a broken twin must not kill the tick
                    return AuditResult(
                        ran_at=ran_at,
                        days=window,
                        error=f"{source.vendor} twin failed: {exc!r}"[:200],
                    )
                twins.append((source.vendor, twin))
            twins_by_vendor = dict(twins)
            if not twins:
                return AuditResult(
                    ran_at=ran_at, days=window, error="no auditable scanner"
                )

            fresh = DailyRollupStore(
                path=root / "audit_rollups.json", keep_days=max(1, int(self.days))
            )
            audited: list[Vendor] = []
            # Reconciliation term, applied BEFORE the comparison. A fresh index
            # reads only what is on disk now; the live store also holds what
            # files that have since been deleted contributed, on purpose (their
            # scan-state entries are tombstones, not deletions - a pruned
            # transcript's money exists only in `rollups.json`). Without this
            # the audit would read every deleted-file day as drift and "repair"
            # it by destroying real, unrecoverable usage.
            for source in sources:
                if source.tombstones:
                    fresh.merge(source.tombstones)
            for vendor, twin in twins:
                try:
                    if not self._drain(twin, fresh, deadline=deadline):
                        return AuditResult(
                            ran_at=ran_at,
                            days=window,
                            error=f"{vendor} re-index did not finish in budget",
                        )
                except Exception as exc:
                    return AuditResult(
                        ran_at=ran_at,
                        days=window,
                        error=f"{vendor} re-index failed: {exc!r}"[:200],
                    )
                audited.append(vendor)

            wanted = frozenset(audited)
            drifted, compared = self._compare(store, fresh, window, wanted)
            # Days the fresh index cannot account for, because a file the live
            # offsets point into moved (or vanished) under it. Their difference
            # is not drift and repairing it would delete real usage, so they are
            # named in the note and left exactly as they are.
            incomparable = self._incomparable(sources, twins_by_vendor, window)
            # A vendor whose scanner still holds a pre-ledger tombstone in this
            # window has usage in the store that NOTHING here can account for:
            # the file is gone, its ledger never described what it contributed,
            # and the fresh index cannot read a file that no longer exists. Its
            # drift is therefore indistinguishable from that real usage, and
            # "repairing" it would delete money for good. Say so instead.
            vetoed = frozenset(
                source.vendor for source in sources if source.has_legacy
            )
            repairs: tuple[AuditRepair, ...] = ()
            if drifted and repair:
                repairs = self._plan(
                    store,
                    fresh,
                    drifted,
                    wanted - vetoed,
                    blocked=incomparable,
                    generation=generation,
                )
            refused = tuple(
                sorted({cell.vendor for cell in drifted if cell.vendor in vetoed})
            )
            return AuditResult(
                ran_at=ran_at,
                days=window,
                vendors=tuple(audited),
                cells_compared=compared,
                drifted=drifted,
                repaired_days=tuple(sorted({item.day for item in repairs})),
                repairs=repairs,
                legacy_vendors=refused,
                not_comparable=tuple(
                    sorted(
                        pair
                        for pair in incomparable
                        if any(cell.day == pair[0] and cell.vendor == pair[1]
                               for cell in drifted)
                    )
                ),
            )

    def _as_sources(
        self,
        scanners: Sequence[AuditSource] | Sequence[tuple[Vendor, Any]],
        *,
        today: DayKey,
    ) -> tuple[AuditSource, ...]:
        """Accept either already-snapshotted sources or raw scanner pairs."""
        items = list(scanners)
        if all(isinstance(item, AuditSource) for item in items):
            return tuple(items)  # type: ignore[arg-type]
        return self.snapshot_scanners(items, today=today)  # type: ignore[arg-type]

    def _drain(self, scanner: Any, fresh: DailyRollupStore, *, deadline: float) -> bool:
        """Run *scanner* to completion into *fresh*. False when the budget ran out.

        Retract-then-merge, exactly as the cost job does: the twin starts with
        no offsets, so it never retracts in practice, but an audit that applied
        a scan differently from production would be auditing a different program.
        """
        for _ in range(_MAX_PASSES):
            if time.monotonic() >= deadline:
                return False
            result = scanner.scan_once(deadline=deadline)
            retractions = getattr(result, "retractions", ())
            if retractions:
                fresh.retract_rollups(retractions)
            if result.deltas:
                fresh.merge(result.deltas)
            if scanner.progress().complete:
                return True
        return False

    @staticmethod
    def _compare(
        store: Any,
        fresh: DailyRollupStore,
        window: Sequence[DayKey],
        vendors: frozenset[Vendor],
    ) -> tuple[tuple[AuditCell, ...], int]:
        """Cell-by-cell comparison over *window*, restricted to *vendors*."""
        drifted: list[AuditCell] = []
        compared = 0
        for day in window:
            live_cells = _cells(store, day)
            fresh_cells = _cells(fresh, day)
            keys = {
                key
                for key in set(live_cells) | set(fresh_cells)
                if vendor_of_key(key) in vendors
            }
            for key in sorted(keys):
                live = live_cells.get(key, 0)
                got = fresh_cells.get(key, 0)
                compared += 1
                drift = abs(live - got)
                if drift <= AUDIT_DRIFT_MIN_TOKENS:
                    continue
                # Relative to the LARGER side, so a phantom cell (fresh = 0)
                # is 100 % drift rather than a division by zero.
                scale = max(live, got)
                if scale <= 0 or drift <= scale * AUDIT_DRIFT_FRACTION:
                    continue
                drifted.append(
                    AuditCell(day=day, key=key, live_tokens=live, fresh_tokens=got)
                )
        return tuple(drifted), compared

    @staticmethod
    def _incomparable(
        sources: Sequence[AuditSource],
        twins: dict[Vendor, Any],
        window: Sequence[DayKey],
    ) -> frozenset[tuple[DayKey, Vendor]]:
        """``(day, vendor)`` pairs whose fresh figure describes other bytes.

        Two ways in. A vendor whose twin could not be BOUNDED at all reads its
        whole corpus as it is now, so every day of the window is suspect. A
        bounded twin reports the snapshotted paths it could not honour - the
        inode moved, the file shrank below the offset, the file is gone - and
        the days those entries' ledgers name are the cells of the store that
        this index cannot account for.
        """
        out: set[tuple[DayKey, Vendor]] = set()
        for source in sources:
            twin = twins.get(source.vendor)
            if twin is None:
                continue
            if not source.bounded:
                out.update((day, source.vendor) for day in window)
                continue
            reader = getattr(twin, "audit_unusable_paths", None)
            if not callable(reader):
                continue
            try:
                unusable = tuple(reader())
            except Exception:  # pragma: no cover - a report must not raise
                unusable = ()
            if not unusable:
                continue
            for day in source.days_of(unusable):
                if day in window:
                    out.add((day, source.vendor))
        return frozenset(out)

    @staticmethod
    def _plan(
        store: Any,
        fresh: DailyRollupStore,
        drifted: Iterable[AuditCell],
        vendors: frozenset[Vendor],
        *,
        blocked: frozenset[tuple[DayKey, Vendor]] = frozenset(),
        generation: int = 0,
    ) -> tuple[AuditRepair, ...]:
        """The repair, as data: what each drifted ``(day, vendor)`` should hold.

        Per ``(day, vendor)`` rather than per cell: an inflated day usually has
        a matching *missing* cell, and repairing one cell at a time would leave
        the other half of the disagreement in place. Vendors that were not
        audited - or that were vetoed - are never planned for.

        Both sides are recorded. ``observed`` is what the live store holds right
        now, so the worker can apply the *difference* and keep whatever it
        merges between this computation and the next tick; ``fresh`` is what the
        re-index says. Nothing here mutates anything.
        """
        pairs = sorted({(cell.day, cell.vendor) for cell in drifted})
        out: list[AuditRepair] = []
        for day, vendor in pairs:
            if vendor not in vendors or (day, vendor) in blocked:
                continue
            live = store.get(day)
            observed = (
                {
                    key: usage
                    for key, usage in live.models.items()
                    if vendor_of_key(key) == vendor and not usage.is_zero
                }
                if live is not None
                else {}
            )
            replacement = fresh.get(day)
            wanted: dict[ModelKey, ModelUsage] = (
                {
                    key: usage
                    for key, usage in replacement.models.items()
                    if vendor_of_key(key) == vendor and not usage.is_zero
                }
                if replacement is not None
                else {}
            )
            out.append(
                AuditRepair(
                    day=day,
                    vendor=vendor,
                    fresh=tuple(sorted(wanted.items())),
                    observed=tuple(sorted(observed.items())),
                    generation=generation,
                )
            )
        return tuple(out)
