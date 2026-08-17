"""Daily aggregate store plus cost math (SPEC 2.2, 3.4, 4.2, 4.3).

This module owns the *only* persistent aggregate in the program::

    {local_date: {raw_model: {5 counters}}}

bounded by the lookback window (default 30 days), a few kilobytes on disk at
``rollups.json``. It is a **pure cache**: deleting the file costs one re-index
and nothing more, so ``load`` tolerates corruption by starting empty.

Division of labour
------------------

* ``indexer.py`` scans transcripts and hands us :class:`ScanResult.deltas` -
  one :class:`DayRollup` per affected local day, keyed by the **raw**
  ``message.model`` string. We store those keys verbatim (contracts rule 4).
* ``pricing.py`` owns every rate and the cost formula. We never look at a
  rate: we call :meth:`PricingTable.cost_usd` once per ``(day, raw model)``
  pair, passing **that day's** date, so the Sonnet 5 intro -> standard
  rollover on 2026-08-31 leaves historical days correct (SPEC 3.4).
* ``app.py`` renders. It must pair every figure with
  :data:`~cc_usage_widget.contracts.NOTIONAL_LABEL`, and must render
  :meth:`IndexProgress.label` instead of the dollar figures while
  :attr:`CostBreakdown.is_partial` is True (SPEC 4.3).

Money arithmetic
----------------

Summing 30 days x ~6 models of binary floats accumulates drift, so **every
dollar figure published here is summed in :class:`decimal.Decimal`**. Each
``float`` returned by the pricing table is captured exactly once via
``Decimal(str(value))``, accumulated at full precision, and quantised to cents
(``ROUND_HALF_UP``) only at the publish boundary.

Every window - ``today`` included - quantises **once**, from its own un-rounded
Decimal total. Deriving ``today`` from the sum of the already-quantised
``by_model`` rows instead (an earlier convention, chosen to make SPEC 4.2's
column literally add up) double-rounded: two models each truly costing $0.005
each rounded to a cent and made ``Today`` $0.02 - twice the truth, and strictly
greater than the ``Last 7d`` window that contains it. The per-model rows are
display values and can therefore differ from the header by up to half a cent
per row; the header is the one that is right.

Threading
---------

Mutation and publication happen on the background scan thread; ``app.py`` may
read a published :class:`CostBreakdown` from the AppKit main thread. Every
public method takes an :class:`threading.RLock`, and everything handed out is
one of the frozen ``contracts`` shapes, so the main thread can never observe a
half-written aggregate (SPEC 2.3).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .contracts import (
    ROLLUPS_PATH,
    SETTINGS_BOUNDS,
    SETTINGS_DEFAULTS,
    UNKNOWN_MODEL,
    VENDORS,
    CostBreakdown,
    DayKey,
    DayRollup,
    IndexProgress,
    ModelCostRow,
    ModelKey,
    ModelUsage,
    PricingTable,
    Usd,
    Vendor,
    VendorCostRow,
    VendorModelKey,
    WindowCost,
    day_keys_back,
    local_day_key,
    make_vendor_key,
    parse_day_key,
    raw_model_of_key,
    rollups_from_json,
    rollups_to_json,
    unknown_model_key,
    vendor_of_key,
)

__all__ = [
    "DailyRollupStore",
    "RollupStore",
    "open_store",
    "WINDOW_TODAY_DAYS",
    "WINDOW_SHORT_DAYS",
    "WINDOW_LONG_DAYS",
]

# ---------------------------------------------------------------------------
# Window lengths (SPEC 4.2 renders Today / Last 7d / Last 30d)
# ---------------------------------------------------------------------------

WINDOW_TODAY_DAYS: int = 1
"""Length of the ``Today`` window. Present so nothing divides by a literal."""

WINDOW_SHORT_DAYS: int = 7
"""Nominal length of the ``Last 7d`` window."""

WINDOW_LONG_DAYS: int = 30
"""Nominal length of the ``Last 30d`` window.

Both nominal lengths are clamped down to the configured lookback window: with
``lookback_days = 10`` there is no honest 30-day figure, so the row is labelled
``Last 10d`` and divides by 10. At the default ``lookback_days = 30`` the rows
read exactly as SPEC 4.2 shows them.
"""

_CENTS = Decimal("0.01")
_ZERO = Decimal("0")


def _dec(value: object) -> Decimal:
    """Capture a pricing ``float`` as an exact :class:`Decimal`.

    A non-finite or non-numeric value becomes ``0`` rather than poisoning
    every aggregate downstream with ``NaN``: the pricing contract forbids it,
    but one bad rate must not blank the whole Cost menu.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _ZERO
    if isinstance(value, float) and not math.isfinite(value):
        return _ZERO
    return Decimal(str(value))


def _to_cents(value: Decimal) -> Usd:
    """Quantise an exact Decimal total to cents and hand back a float."""
    return float(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _clamp_keep_days(value: object) -> int:
    """Coerce and clamp a lookback length to :data:`SETTINGS_BOUNDS`."""
    low, high = SETTINGS_BOUNDS["lookback_days"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return int(SETTINGS_DEFAULTS["lookback_days"])
    return min(max(int(value), low), high)


def _atomic_write_json(path: Path, payload: object) -> None:
    """Serialise *payload* to *path* atomically (temp file + ``os.replace``).

    The temp file is created in the destination directory so ``os.replace`` is
    a same-filesystem rename, and is fsynced before the swap: a power loss can
    leave the old file or the new file, never a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _context_marker(raws: tuple[str, ...]) -> str | None:
    """The long-context marker carried by any raw string in a group, or ``None``.

    Imported lazily so ``rollup.py`` keeps working against a
    :class:`~cc_usage_widget.contracts.PricingTable` that is not
    ``pricing.ModelPricing`` (a test double, say).
    """
    try:
        from .pricing import context_window_marker
    except Exception:  # pragma: no cover - pricing is a sibling module
        return None
    for raw in raws:
        marker = context_window_marker(raw)
        if marker:
            return marker
    return None


def _group_key(pricing: PricingTable, raw_model: str) -> VendorModelKey:
    """The ``(vendor, model)`` identity a stored key collapses onto.

    Grouping by :meth:`PricingTable.canonical_model` alone is a **vendor
    collision**: it returns the *bare* key, so two vendors shipping a
    same-named model land on one menu row, and every unpriced model - Claude's
    and Codex's alike - shares the single ``unknown`` bucket that
    :func:`~cc_usage_widget.contracts.unknown_model_key` exists to keep apart.

    ``canonical_key`` is the vendor-safe method and is what we use when the
    table has it. The fallback reconstructs the same value from the two methods
    the :class:`~cc_usage_widget.contracts.PricingTable` protocol actually
    mandates, so a test double implementing only the protocol still groups
    correctly rather than silently merging vendors.
    """
    method = getattr(pricing, "canonical_key", None)
    if callable(method):
        try:
            key = method(raw_model)
        except Exception:  # pragma: no cover - a table this broken is a bug
            key = None
        if isinstance(key, str) and key:
            return key
    # Protocol-only fallback. The stored key already states its vendor
    # (unqualified means claude, per the migration rule), and that statement
    # wins - a bare `canonical_model` cannot tell us who owns the name.
    vendor = vendor_of_key(raw_model)
    canonical = pricing.canonical_model(raw_model) or UNKNOWN_MODEL
    if canonical == UNKNOWN_MODEL:
        return unknown_model_key(vendor)
    return make_vendor_key(vendor, canonical)


class _ModelAcc:
    """Mutable per-model accumulator used only while building a breakdown."""

    __slots__ = ("usd", "counters", "raws")

    def __init__(self) -> None:
        self.usd: Decimal = _ZERO
        self.counters: list[int] = [0, 0, 0, 0, 0]
        self.raws: set[str] = set()

    def add(self, *, usd: Decimal, usage: ModelUsage, raw_model: str) -> None:
        self.usd += usd
        for i, value in enumerate(usage.as_counters()):
            self.counters[i] += value
        self.raws.add(raw_model)


def _vendor_split(totals: dict[Vendor, Decimal]) -> tuple[tuple[Vendor, Usd], ...]:
    """``WindowCost.vendor_usd`` for one window, in :data:`VENDORS` order.

    A vendor with no usage in the window contributes **no entry** rather than a
    zero, so an absent vendor renders no section (SPEC-CODEX 5.5). Returns
    ``()`` when nothing was counted at all, which the contract reads as "not
    split" - the renderer then falls back to the single total.
    """
    if not totals:
        return ()
    known = [(v, totals[v]) for v in VENDORS if v in totals]
    extra = sorted((v, amount) for v, amount in totals.items() if v not in VENDORS)
    return tuple((vendor, _to_cents(amount)) for vendor, amount in known + extra)


class DailyRollupStore:
    """The daily token aggregate, satisfying ``contracts.RollupStore``.

    Internally each day is a ``dict[raw model, list[int]]`` of the five
    counters in :data:`~cc_usage_widget.contracts.COUNTER_FIELDS` order -
    mutable, so accumulation allocates nothing per merge - and the frozen
    ``contracts`` shapes are materialised only when something is published
    (contracts rule 5).
    """

    def __init__(
        self,
        *,
        path: Path | str | None = None,
        keep_days: int | None = None,
    ) -> None:
        """Create an empty store. Call :meth:`load` to read the cache file.

        Args:
            path: override for ``rollups.json``; defaults to
                :data:`~cc_usage_widget.contracts.ROLLUPS_PATH`.
            keep_days: lookback window; defaults to
                ``SETTINGS_DEFAULTS["lookback_days"]`` and is clamped to
                ``SETTINGS_BOUNDS["lookback_days"]``.
        """
        self._path: Path = Path(path) if path is not None else ROLLUPS_PATH
        self._keep_days: int = _clamp_keep_days(
            SETTINGS_DEFAULTS["lookback_days"] if keep_days is None else keep_days
        )
        self._days: dict[DayKey, dict[ModelKey, list[int]]] = {}
        self._lock = threading.RLock()
        self._progress = IndexProgress()
        self._dirty = False
        self._revision = 0
        self._cache: tuple[tuple[object, ...], CostBreakdown] | None = None

    # -- introspection ----------------------------------------------------

    @property
    def path(self) -> Path:
        """Where this store persists."""
        return self._path

    @property
    def keep_days(self) -> int:
        """The lookback window, in days."""
        return self._keep_days

    def set_keep_days(self, keep_days: int) -> None:
        """Change the lookback window (e.g. the user edited settings).

        Does not prune immediately; the next :meth:`save` enforces it.
        """
        with self._lock:
            clamped = _clamp_keep_days(keep_days)
            if clamped == self._keep_days:
                return
            self._keep_days = clamped
            self._invalidate()

    def __len__(self) -> int:
        """Number of days held."""
        with self._lock:
            return len(self._days)

    # -- progress (SPEC 4.3 honesty rule) ---------------------------------

    def progress(self) -> IndexProgress:
        """The last progress snapshot handed to :meth:`set_progress`."""
        with self._lock:
            return self._progress

    def set_progress(self, progress: IndexProgress) -> None:
        """Record the indexer's progress so breakdowns can carry it.

        While ``progress.complete`` is False every :class:`CostBreakdown` this
        store produces reports :attr:`CostBreakdown.is_partial` True, and the
        UI must render ``indexing... n/N`` instead of a dollar figure that
        would read as a real total (SPEC 4.3).
        """
        if not isinstance(progress, IndexProgress):
            raise TypeError(
                f"set_progress expects IndexProgress, got {type(progress)!r}"
            )
        with self._lock:
            if progress == self._progress:
                return
            self._progress = progress
            # Progress is not persisted, so this is not a `dirty` change; the
            # breakdown cache keys on it and drops itself.
            self._cache = None

    @property
    def is_complete(self) -> bool:
        """True once the index has finished at least one full pass.

        The single flag ``app.py`` needs to decide between a dollar figure and
        ``indexing... n/N``.
        """
        with self._lock:
            return self._progress.complete

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        """Load from disk; a missing or corrupt file means "start empty".

        The store is a pure cache and is rebuildable by re-indexing, so
        crashing at launch over a truncated JSON file would be strictly worse
        than losing it. Individually malformed days are skipped by
        :func:`~cc_usage_widget.contracts.rollups_from_json`.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            parsed: object = None
        else:
            try:
                parsed = json.loads(raw)
            except (ValueError, RecursionError):
                parsed = None

        days: dict[DayKey, dict[ModelKey, list[int]]] = {}
        for day, rollup in rollups_from_json(parsed).items():
            days[day] = {
                model: list(usage.as_counters())
                for model, usage in rollup.models.items()
            }
        with self._lock:
            self._days = days
            self._dirty = False
            self._invalidate()

    def save(self, *, force: bool = False) -> None:
        """Prune to the window, then persist atomically.

        Pruning on write is what bounds the file: it cannot grow past
        ``keep_days`` days regardless of how long the widget runs (SPEC 2.2).

        A clean store with an existing file is skipped, so an idle widget does
        not rewrite the same bytes every 300 s tick. Pass ``force=True`` to
        write unconditionally.
        """
        with self._lock:
            self.prune(
                today=local_day_key(time.time()), keep_days=self._keep_days
            )
            if not (force or self._dirty or not self._path.exists()):
                return
            payload = rollups_to_json(
                {
                    day: DayRollup(
                        day=day,
                        models={
                            model: ModelUsage.from_counters(counters)
                            for model, counters in models.items()
                        },
                    )
                    for day, models in self._days.items()
                }
            )
            _atomic_write_json(self._path, payload)
            self._dirty = False

    # -- mutation ---------------------------------------------------------

    def add(self, day: DayKey, model: ModelKey, usage: ModelUsage) -> None:
        """Accumulate *usage* into ``(day, model)``, field-wise.

        *model* is stored verbatim: bucketing by the raw ``message.model``
        string is the indexer's contract and canonicalisation happens later,
        in the price table (contracts rule 4).

        Raises:
            ValueError: if *day* is not a ``YYYY-MM-DD`` local day key.
            TypeError: if *usage* is not a :class:`ModelUsage`.
        """
        parse_day_key(day)
        if not isinstance(usage, ModelUsage):
            raise TypeError(f"add expects ModelUsage, got {type(usage)!r}")
        with self._lock:
            self._add_locked(day, str(model), usage)
            self._invalidate()

    def merge(self, rollups: Iterable[DayRollup]) -> None:
        """Add the indexer's deltas into the store, per day and per model.

        The indexer's dedup guarantee means a request is never emitted twice,
        so this is a plain addition. Days outside the current window are
        accepted and dropped by the next :meth:`prune`.
        """
        with self._lock:
            changed = False
            for rollup in rollups:
                if not isinstance(rollup, DayRollup):
                    raise TypeError(
                        f"merge expects DayRollup values, got {type(rollup)!r}"
                    )
                parse_day_key(rollup.day)
                for model, usage in rollup.models.items():
                    self._add_locked(rollup.day, str(model), usage)
                    changed = True
            if changed:
                self._invalidate()

    def prune(self, *, today: DayKey, keep_days: int) -> None:
        """Drop days outside the ``keep_days`` window ending on *today*.

        Days *after* *today* are kept up to ``keep_days`` ahead: a corrected
        clock or a transcript synced from a further-east host can leave a
        future-dated bucket, and silently deleting real usage is worse than
        carrying a stray day until it ages out. :meth:`_breakdown` folds those
        days into ``Last Nd`` so they are visible rather than retained-and-hidden,
        and the symmetric bound is what keeps the key set finite.

        Raises:
            ValueError: if *today* is not a day key, or ``keep_days < 1``
                (a zero-length window would wipe the whole store, which is
                never what a caller means).
        """
        if not isinstance(keep_days, int) or isinstance(keep_days, bool):
            raise ValueError(f"keep_days must be an int, got {keep_days!r}")
        if keep_days < 1:
            raise ValueError(f"keep_days must be >= 1, got {keep_days!r}")
        today_date = parse_day_key(today)
        start = today_date - dt.timedelta(days=keep_days - 1)
        end = today_date + dt.timedelta(days=keep_days)
        with self._lock:
            doomed: list[DayKey] = []
            for day in self._days:
                try:
                    day_date = parse_day_key(day)
                except ValueError:
                    doomed.append(day)
                    continue
                if day_date < start or day_date > end:
                    doomed.append(day)
            if not doomed:
                return
            for day in doomed:
                del self._days[day]
            self._invalidate()

    def clear(self) -> None:
        """Drop every day. Pairs with ``TranscriptIndexer.reset()``."""
        with self._lock:
            if not self._days:
                return
            self._days = {}
            self._invalidate()

    def drop_vendors(self, vendors: Iterable[Vendor]) -> int:
        """Drop every row belonging to *vendors*, leaving the others untouched.

        The per-vendor counterpart of :meth:`clear`, and the reason a lost
        ``codex_scan_state.json`` costs one Codex re-index rather than the whole
        store: the two vendors share one ``rollups.json`` (a window total has to
        span both), but they do **not** share an accounting fate. Only the vendor
        whose own offsets vanished can double, so only its rows may be dropped -
        ``clear()`` here would delete Claude days that no longer exist on disk
        (Claude Code prunes ``~/.claude/projects`` on its own schedule) and are
        therefore not reconstructible by re-indexing.

        Keys are matched with :func:`vendor_of_key`, so a bare pre-Codex key
        counts as claude exactly as it does everywhere else. A day left with no
        models is removed rather than kept as an empty bucket.

        Returns:
            The number of ``(day, model)`` rows removed - 0 when nothing
            matched, which is the common case and does not dirty the store.
        """
        wanted = {v for v in vendors}
        if not wanted:
            return 0
        removed = 0
        with self._lock:
            for day in list(self._days):
                models = self._days[day]
                doomed = [key for key in models if vendor_of_key(key) in wanted]
                if not doomed:
                    continue
                for key in doomed:
                    del models[key]
                removed += len(doomed)
                if not models:
                    del self._days[day]
            if removed:
                self._invalidate()
        return removed

    # -- queries ----------------------------------------------------------

    def get(self, day: DayKey) -> DayRollup | None:
        """Rollup for one local day, or ``None`` when that day has no data."""
        with self._lock:
            models = self._days.get(day)
            if models is None:
                return None
            return DayRollup(
                day=day,
                models={
                    model: ModelUsage.from_counters(counters)
                    for model, counters in models.items()
                },
            )

    def days(self) -> tuple[DayKey, ...]:
        """Every day held, sorted ascending."""
        with self._lock:
            return tuple(sorted(self._days))

    def today(self, day: DayKey | None = None) -> DayRollup:
        """Today's rollup, or an **empty** rollup when today has no data.

        Unlike :meth:`get` this never returns ``None`` - the title renderer
        wants a number, and "no usage yet today" is legitimately zero, not
        missing. *day* defaults to the current local date.
        """
        key = day if day is not None else local_day_key(time.time())
        parse_day_key(key)
        return self.get(key) or DayRollup(day=key)

    def last_n_days(
        self, n: int, *, today: DayKey | None = None
    ) -> tuple[DayRollup, ...]:
        """The *n*-day window ending on and **including** *today*, ascending.

        Always exactly *n* entries: days with no data come back as empty
        rollups rather than being omitted, so the result indexes as a dense
        time series. ``n <= 0`` returns ``()``.
        """
        end = today if today is not None else local_day_key(time.time())
        return tuple(self.today(key) for key in day_keys_back(end, n))

    def window_total(self, n: int, *, today: DayKey | None = None) -> ModelUsage:
        """Field-wise token total across the *n*-day window ending today."""
        acc = [0, 0, 0, 0, 0]
        for rollup in self.last_n_days(n, today=today):
            for i, value in enumerate(rollup.total.as_counters()):
                acc[i] += value
        return ModelUsage.from_counters(acc)

    # -- cost -------------------------------------------------------------

    def cost_breakdown(
        self,
        pricing: PricingTable,
        *,
        today: DayKey,
        progress: IndexProgress,
    ) -> CostBreakdown:
        """``contracts.RollupStore`` entry point. See :meth:`build_breakdown`."""
        return self._breakdown(pricing, today=today, progress=progress)

    def build_breakdown(
        self,
        pricing: PricingTable,
        today: DayKey | None = None,
        *,
        progress: IndexProgress | None = None,
    ) -> CostBreakdown:
        """Compute every figure the Cost menu section renders (SPEC 4.2).

        Convenience form of :meth:`cost_breakdown`: *today* defaults to the
        current local date and *progress* to :meth:`progress`.

        Each day is priced with the row in effect **on that day**, by handing
        that day's ``date`` to the price table, so the Sonnet 5
        intro -> standard rollover leaves historical days correct.
        ``by_model`` covers *today* only, ordered by descending USD with the
        unknown bucket last, and its rows sum to the ``Today`` figure exactly.
        """
        day = today if today is not None else local_day_key(time.time())
        return self._breakdown(
            pricing,
            today=day,
            progress=self.progress() if progress is None else progress,
        )

    # -- internals --------------------------------------------------------

    def _add_locked(self, day: DayKey, model: ModelKey, usage: ModelUsage) -> None:
        """Accumulate one ``(day, model)`` pair. Caller holds the lock."""
        models = self._days.get(day)
        if models is None:
            models = {}
            self._days[day] = models
        counters = models.get(model)
        if counters is None:
            models[model] = list(usage.as_counters())
            return
        for i, value in enumerate(usage.as_counters()):
            counters[i] += value

    def _invalidate(self) -> None:
        """Mark the aggregate changed. Caller holds the lock."""
        self._revision += 1
        self._dirty = True
        self._cache = None

    def _breakdown(
        self,
        pricing: PricingTable,
        *,
        today: DayKey,
        progress: IndexProgress,
    ) -> CostBreakdown:
        parse_day_key(today)
        if not isinstance(progress, IndexProgress):
            raise TypeError(
                f"progress must be IndexProgress, got {type(progress)!r}"
            )

        with self._lock:
            long_days = max(1, min(WINDOW_LONG_DAYS, self._keep_days))
            short_days = max(1, min(WINDOW_SHORT_DAYS, self._keep_days))
            cache_key = (
                today,
                self._revision,
                id(pricing),
                progress,
                short_days,
                long_days,
            )
            cached = self._cache
            if cached is not None and cached[0] == cache_key:
                return cached[1]

            window = day_keys_back(today, long_days)
            short_window = frozenset(day_keys_back(today, short_days))
            # `prune` deliberately RETAINS a day dated after today (a corrected
            # clock, or a transcript synced from a further-east host), on the
            # grounds that deleting real usage is worse than carrying a stray
            # day. `day_keys_back` only ever looks backwards, so such a bucket
            # was retained *and* hidden from every figure the UI renders - the
            # one outcome that satisfies neither goal. Fold it into the two
            # windows so the money is visible; `prune` now bounds how far ahead
            # a day may be, so this stays a handful of keys.
            future = sorted(day for day in self._days if day > today)
            if future:
                window = window + tuple(future)
                short_window = short_window | frozenset(future)

            long_usd = _ZERO
            long_tokens = 0
            long_days_counted = 0
            short_usd = _ZERO
            short_tokens = 0
            short_days_counted = 0
            today_tokens = 0
            today_usd_exact = _ZERO
            today_rows: dict[VendorModelKey, _ModelAcc] = {}
            unknown_raw: set[str] = set()
            # Per-vendor USD per window (SPEC-CODEX 5.2). The window headers
            # stay cross-vendor totals; these are the breakdown *behind* them.
            long_by_vendor: dict[Vendor, Decimal] = {}
            short_by_vendor: dict[Vendor, Decimal] = {}

            for day_key in window:
                models = self._days.get(day_key)
                if not models:
                    continue
                day_date = parse_day_key(day_key)
                is_today = day_key == today
                day_usd = _ZERO
                day_tokens = 0

                for raw_model, counters in models.items():
                    usage = ModelUsage.from_counters(counters)
                    if usage.is_zero:
                        continue
                    cost = _dec(pricing.cost_usd(raw_model, usage, day_date))
                    group = _group_key(pricing, raw_model)
                    vendor = vendor_of_key(group)
                    if raw_model_of_key(group) == UNKNOWN_MODEL:
                        # RAW, never the storage key: SPEC 3.3 trap 5 requires
                        # the user to see the actual unrecognised name, and
                        # `codex:codex-auto-review` is our spelling, not theirs.
                        name = raw_model_of_key(str(raw_model))
                        # ...but the sentinel is not a name. A record whose model
                        # was absent entirely (85.5M Codex tokens in the live
                        # 30-day window: turns emitted before any turn_context)
                        # is already bucketed and shown as an `unknown` ROW with
                        # its tokens at $0. Listing it again under "unpriced
                        # model(s)" would print `unknown` as if it were a model
                        # string the user could look up.
                        if name != UNKNOWN_MODEL:
                            unknown_raw.add(name)
                    day_usd += cost
                    day_tokens += usage.total_tokens
                    long_by_vendor[vendor] = long_by_vendor.get(vendor, _ZERO) + cost
                    if day_key in short_window:
                        short_by_vendor[vendor] = (
                            short_by_vendor.get(vendor, _ZERO) + cost
                        )
                    if is_today:
                        acc = today_rows.get(group)
                        if acc is None:
                            acc = _ModelAcc()
                            today_rows[group] = acc
                        acc.add(usd=cost, usage=usage, raw_model=raw_model)

                long_usd += day_usd
                long_tokens += day_tokens
                if day_tokens:
                    long_days_counted += 1
                if day_key in short_window:
                    short_usd += day_usd
                    short_tokens += day_tokens
                    if day_tokens:
                        short_days_counted += 1
                if is_today:
                    today_tokens = day_tokens
                    today_usd_exact = day_usd

            rows = self._build_rows(pricing, today_rows)
            # Today's split comes from the SAME Decimals the rows were built
            # from, so `by_vendor` subtotals and `today.vendor_usd` can never
            # disagree, and both sum to the un-rounded `today` header.
            today_by_vendor: dict[Vendor, Decimal] = {}
            for group_key, acc in today_rows.items():
                vendor = vendor_of_key(group_key)
                today_by_vendor[vendor] = today_by_vendor.get(vendor, _ZERO) + acc.usd
            by_vendor = self._build_vendor_groups(rows, today_by_vendor)
            # Quantised ONCE, from the same un-rounded Decimal the 7d/30d windows
            # use. Summing the already-quantised per-model rows instead (the
            # previous convention) double-rounded: two models each truly costing
            # $0.005 rounded to $0.01 apiece and made Today $0.02 - twice the
            # truth, and strictly greater than the 7-day window containing it.
            today_usd = today_usd_exact

            breakdown = CostBreakdown(
                today=WindowCost(
                    label="Today",
                    usd=_to_cents(today_usd),
                    total_tokens=today_tokens,
                    window_days=WINDOW_TODAY_DAYS,
                    days_counted=1 if today_tokens else 0,
                    vendor_usd=_vendor_split(today_by_vendor),
                ),
                last_7d=WindowCost(
                    label=f"Last {short_days}d",
                    usd=_to_cents(short_usd),
                    total_tokens=short_tokens,
                    window_days=short_days,
                    days_counted=short_days_counted,
                    vendor_usd=_vendor_split(short_by_vendor),
                ),
                last_30d=WindowCost(
                    label=f"Last {long_days}d",
                    usd=_to_cents(long_usd),
                    total_tokens=long_tokens,
                    window_days=long_days,
                    days_counted=long_days_counted,
                    vendor_usd=_vendor_split(long_by_vendor),
                ),
                by_model=rows,
                unknown_models=tuple(sorted(unknown_raw)),
                progress=progress,
                generated_at=time.time(),
                by_vendor=by_vendor,
            )
            self._cache = (cache_key, breakdown)
            return breakdown

    @staticmethod
    def _build_rows(
        pricing: PricingTable, accs: dict[VendorModelKey, _ModelAcc]
    ) -> tuple[ModelCostRow, ...]:
        """Materialise today's per-model rows, ordered for the menu.

        Descending USD, then descending tokens, then model name for a stable
        order between ticks; the unknown bucket always sorts last so a
        ``$0`` mystery model never displaces a real one (SPEC 4.2).

        *accs* is keyed by :data:`~cc_usage_widget.contracts.VendorModelKey`;
        the vendor is split back out onto :attr:`ModelCostRow.vendor` and the
        row's ``model`` stays **bare**, per the contract. ``raw_models`` is
        stripped to bare names too - it is user-visible text and must never
        show our storage spelling.
        """
        rows: list[ModelCostRow] = []
        for group_key, acc in accs.items():
            usage = ModelUsage.from_counters(acc.counters)
            if usage.is_zero:
                continue
            vendor = vendor_of_key(group_key)
            canonical = raw_model_of_key(group_key)
            raws = tuple(sorted(raw_model_of_key(str(raw)) for raw in acc.raws))
            is_unknown = canonical == UNKNOWN_MODEL
            rows.append(
                ModelCostRow(
                    model=canonical,
                    display_name=DailyRollupStore._display_name(
                        pricing, raws, is_unknown=is_unknown, lookup=group_key
                    ),
                    usage=usage,
                    usd=_to_cents(acc.usd),
                    is_unknown=is_unknown,
                    raw_models=raws,
                    vendor=vendor,
                )
            )
        rows.sort(
            key=lambda row: (
                row.is_unknown,
                -row.usd,
                -row.total_tokens,
                row.model,
                row.vendor,
            )
        )
        return tuple(rows)

    @staticmethod
    def _build_vendor_groups(
        rows: tuple[ModelCostRow, ...], totals: dict[Vendor, Decimal]
    ) -> tuple[VendorCostRow, ...]:
        """Group today's rows by vendor, in :data:`VENDORS` order.

        A vendor with no rows today is simply **absent** - never present with
        zeros - which is what gives a Claude-only user no Codex section and a
        Codex-only user no Claude section with no special-casing in the
        renderer (SPEC-CODEX 5.5). Row order inside a group is the menu order
        established by :meth:`_build_rows`.

        The subtotal comes from *totals* (the un-rounded Decimals), not from
        summing the already-quantised row floats, for the same reason the
        ``Today`` header does: summing rounded rows double-rounds.
        """
        grouped: dict[Vendor, list[ModelCostRow]] = {}
        for row in rows:
            grouped.setdefault(row.vendor, []).append(row)
        if not grouped:
            return ()
        order = [v for v in VENDORS if v in grouped]
        order += sorted(v for v in grouped if v not in VENDORS)
        out: list[VendorCostRow] = []
        for vendor in order:
            members = tuple(grouped[vendor])
            usage = sum(
                (row.usage for row in members), ModelUsage()
            )
            out.append(
                VendorCostRow(
                    vendor=vendor,
                    usd=_to_cents(totals.get(vendor, _ZERO)),
                    usage=usage,
                    rows=members,
                )
            )
        return tuple(out)

    @staticmethod
    def _display_name(
        pricing: PricingTable,
        raws: tuple[str, ...],
        *,
        is_unknown: bool,
        lookup: str | None = None,
    ) -> str:
        """Menu label for a canonical group of raw model strings.

        *lookup* is the vendor-qualified key to price the label against, when
        the caller has one. Looking a **known** model up by its bare name would
        route it through the price table's cross-vendor fallback, which is
        deliberately refused when two vendors claim the name - the label would
        then silently degrade to the raw string. ``raws`` stays bare either
        way: it is what the user is shown.

        A known model collapses several dated raw strings
        (``claude-fable-5-20260514``) into one label (``Fable 5``), so the
        first raw string's label is the group's label. For the unknown bucket
        the price table returns the raw string itself, which is exactly what
        SPEC 3.3 trap 5 requires the user to see; if several unrecognised
        strings landed in the bucket the extras are counted in the label so no
        name is hidden.
        """
        if not raws:
            return UNKNOWN_MODEL
        probe = raws[0] if (is_unknown or not lookup) else lookup
        label = pricing.display_name(probe) or raws[0]
        if is_unknown and len(raws) > 1:
            return f"{label} (+{len(raws) - 1} more)"
        if not is_unknown:
            # A long-context tier (`claude-opus-5[1m]`, `claude-opus-5-1m`) folds
            # onto the base key and is therefore priced at the base rate. That is
            # deliberate - the spec's table has no long-context row and inventing
            # a multiplier is forbidden - but it must not be invisible (SPEC 4.3).
            marker = _context_marker(raws)
            if marker:
                return f"{label} ({marker} ctx · base rate)"
        return label


RollupStore = DailyRollupStore
"""Alias for :class:`DailyRollupStore`.

``contracts.RollupStore`` is the *protocol*; this is the concrete class under
the name a caller is most likely to reach for. Same object, so
``isinstance``/identity checks behave.
"""


def open_store(
    *, path: Path | str | None = None, keep_days: int | None = None
) -> DailyRollupStore:
    """Construct a store and :meth:`~DailyRollupStore.load` it in one step.

    Never raises on a missing or corrupt cache file - the store simply starts
    empty and the next index refills it.
    """
    store = DailyRollupStore(path=path, keep_days=keep_days)
    store.load()
    return store
