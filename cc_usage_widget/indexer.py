"""Incremental transcript scanner — the performance-critical module (SPEC 3.2).

Everything in this file exists to satisfy one number: **< 30 ms CPU per
steady-state tick** over a corpus of 1.38 GB / 3,210 ``*.jsonl`` files of which
only ~40 are touched per hour (SPEC 2.1). The whole design follows from that:

* The tick is dominated by ``os.scandir`` + one ``stat`` per transcript, and
  **every unmodified file exits at the ``(size, mtime)`` comparison without ever
  being opened** (SPEC 3.2 step 2) — measured 3,160 of 3,210, 0 files opened,
  16 ms CPU. That comparison is the hottest predicate in the program; nothing —
  not a ``Path`` construction, not a day-key parse, not a lookback computation —
  happens before it.
* Files that *did* change are read from their stored byte ``offset`` only, so a
  1-hour-old 36 MB transcript costs the few kilobytes that were appended.
* Lines are split out of **one reusable ``bytearray``** and pre-filtered with a
  ``b'"usage"'`` substring test *in place*, before anything is copied and before
  ``json.loads`` (SPEC 3.2 step 6): measured 331,879 lines seen, 168,509 parsed,
  so ~49% of lines are never copied, never decoded and never parsed. ``for raw in
  fh`` was the obvious way to write this and is the wrong one: it allocates a
  fresh multi-kilobyte ``bytes`` per line — 330k of them per full index, which is
  what pushed the allocator high-water past the RSS budget — and on the file
  holding the corpus's largest line (4.13 MB, and it contains no ``"usage"`` at
  all) it peaked at 8.35 MB against 4.70 MB for this loop.
* Nothing is ever accumulated as a parsed record. The scan accumulates into
  ``dict[DayKey, dict[ModelKey, list[int]]]`` — plain mutable counters, per
  contracts rule 5 — and materialises the frozen ``DayRollup`` publish shapes
  exactly once, at the end.
* No transcript is ever ``read()``/``readlines()``-ed: ``seek(offset)`` then
  read 64 KiB at a time (SPEC 2.2). Measured steady-state peak: 2.25 MB traced;
  whole-corpus first-index peak 7.33 MB; the 34.9 MB transcript 3.02 MB — all
  against a 10 MB budget. The floor is the longest single line, since one record
  must be materialised to be parsed, but only the *lines that matter* are ever
  materialised: the largest ``"usage"``-bearing line in this corpus is 176 KB.

Measured first run over the whole corpus: 14 chunked passes, 4.9 s wall / 3.3 s
CPU, 1,417 MB read, 3,165 files opened once each, 73,925 requests counted,
93,811 duplicate records suppressed, 0 malformed, 21.5 MB of RSS growth.

Threading (SPEC 2.3): every method here does I/O and therefore belongs on the
background thread. The one exception is :meth:`Indexer.progress`, which returns
an immutable snapshot published by a single attribute assignment (atomic under
the GIL) and is safe to call from the AppKit main thread on every UI tick. No
lock is ever held across I/O.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Final

from .contracts import (
    PROJECTS_DIR,
    SCAN_STATE_PATH,
    SETTINGS_DEFAULTS,
    UNKNOWN_MODEL,
    DayKey,
    DayRollup,
    FileScanState,
    IndexProgress,
    ModelKey,
    ModelUsage,
    PricingTable,
    ScanResult,
    day_keys_back,
    local_day_key,
    local_day_key_from_iso,
    parse_day_key,
    scan_state_from_json,
    scan_state_to_json,
)

__all__ = [
    "Indexer",
    "TranscriptScanner",
    "build_indexer",
    "USAGE_MARKER",
    "TRANSCRIPT_SUFFIX",
    "DEFAULT_CHUNK_FILES",
]

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

USAGE_MARKER: Final[bytes] = b'"usage"'
"""SPEC 3.2 step 6 pre-filter. Kept as **bytes** so the 61% of lines that fail
it are never decoded — a str prefilter would pay UTF-8 decoding on ~4.2 KB per
line for nothing."""

TRANSCRIPT_SUFFIX: Final[str] = ".jsonl"

DEFAULT_CHUNK_FILES: Final[int] = 250
"""Files read per :meth:`Indexer.scan_once` call. Bounds the work of a single
pass so the first-run index yields between chunks and the caller can publish
partial progress (SPEC 3.2 "First run")."""

_NEWLINE: Final[int] = 0x0A
"""``bytearray.find`` takes an int for a single byte, so the line split needs no
one-byte ``bytes`` object."""

_READ_BUFFER: Final[int] = 1 << 16
"""64 KiB read buffer: big enough that a 36 MB transcript is ~560 reads, small
enough to stay far inside the < 10 MB peak-allocation budget."""

_DEADLINE_LINE_INTERVAL: Final[int] = 4096
"""Lines between ``time.monotonic()`` deadline checks *inside* one file. A
single transcript can be 36 MB, so yielding only between files is not enough."""

_DEDUP_PERSIST_LIMIT: Final[int] = 50_000
"""Cap on the number of request IDs written to the dedup sidecar. One day of
traffic on the measured corpus is ~2.5k entries; the cap only exists so a
pathological day cannot turn the sidecar into a multi-megabyte write."""

_EXCLUDED_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv"}
)

_FORBIDDEN_PATH_PARTS: Final[tuple[str, ...]] = (".claude-swap-backup",)
"""claude-swap keeps **copies** of sessions under
``~/.claude-swap-backup/sessions``. Indexing those would double-count every
token, so the tree is refused even if someone points
``CC_USAGE_WIDGET_PROJECTS_DIR`` at it. The normal root
(``~/.claude/projects``) excludes it structurally; this is the belt to that
braces."""


class _Stats:
    """Mutable per-scan tallies. One instance per scan, not per line."""

    __slots__ = (
        "files_read",
        "bytes_read",
        "lines_seen",
        "lines_parsed",
        "records_counted",
        "records_duplicate",
        "records_malformed",
    )

    def __init__(self) -> None:
        self.files_read = 0
        self.bytes_read = 0
        self.lines_seen = 0
        self.lines_parsed = 0
        self.records_counted = 0
        self.records_duplicate = 0
        self.records_malformed = 0


def _to_int(value: Any) -> int:
    """Coerce a JSON token count to a non-negative ``int``; junk becomes 0.

    Deliberately total: a transcript written by a newer/older Claude Code build
    must never crash the scan (SPEC 3.3 trap 6).
    """
    if type(value) is int:  # noqa: E721 - exact type check is the fast path
        return value if value > 0 else 0
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        return int(value) if value > 0 else 0
    return 0


def _extract_counters(usage: Mapping[str, Any]) -> list[int] | None:
    """Pull the five counters out of one ``message.usage`` object.

    Returns a mutable 5-list in :data:`~cc_usage_widget.contracts.COUNTER_FIELDS`
    order, or ``None`` when the object carries none of the five source fields —
    which the caller must treat as "skip this record".

    TRAP 1 (``usage.iterations``): ``iterations`` is a per-attempt breakdown of
    the *same* request; the real corpus contains records whose single iteration
    repeats the top-level numbers verbatim. We read the **top-level fields
    only** and never touch ``usage["iterations"]``. Summing both double-counts.

    TRAP 2 (``cache_creation_input_tokens``): the flat field is the **sum** of
    ``ephemeral_5m_input_tokens`` + ``ephemeral_1h_input_tokens``. When
    ``cache_creation`` is present we use the split fields and ignore the flat
    one; the flat field is used as ``cache_write_5m`` **only** when
    ``cache_creation`` is absent (older transcripts). Adding both would
    double-count every cache write.

    TRAP 6 (missing/partial ``usage``): a ``usage`` object with none of the five
    fields yields ``None`` — the record is skipped, never counted as five zeros.
    """
    tok_in = usage.get("input_tokens")
    tok_out = usage.get("output_tokens")
    tok_read = usage.get("cache_read_input_tokens")

    creation = usage.get("cache_creation")
    if isinstance(creation, Mapping):
        # TRAP 2: the split fields are authoritative for the part they explain,
        # so the flat sum is never *added* to them.
        write_5m = creation.get("ephemeral_5m_input_tokens")
        write_1h = creation.get("ephemeral_1h_input_tokens")
        # ...but the flat field is still cross-checked, because branching on the
        # mere presence of the mapping meant a new TTL tier
        # (`ephemeral_1d_input_tokens`) or a rename zeroed the record's whole
        # cache write - the largest cost component in this corpus - while a
        # *less* detailed transcript of the same request priced correctly. Any
        # remainder the split fields do not account for is attributed to the 5m
        # bucket, the cheaper of the two write rates.
        flat = _to_int(usage.get("cache_creation_input_tokens"))
        explained = _to_int(write_5m) + _to_int(write_1h)
        if flat > explained:
            write_5m = _to_int(write_5m) + (flat - explained)
    else:
        # TRAP 2 fallback: no cache_creation block, so the flat field is the
        # only cache-write signal available. Attribute it to 5m.
        write_5m = usage.get("cache_creation_input_tokens")
        write_1h = None

    if (
        tok_in is None
        and tok_out is None
        and tok_read is None
        and write_5m is None
        and write_1h is None
    ):
        return None  # TRAP 6: partial usage object -> skip the record

    return [
        _to_int(tok_in),
        _to_int(tok_out),
        _to_int(write_5m),
        _to_int(write_1h),
        _to_int(tok_read),
    ]


def _increment_over(prior: list[int], counts: list[int]) -> list[int] | None:
    """Growth of *counts* over the already-credited *prior*, or ``None``.

    ``None`` means "nothing new" — the ordinary duplicate case, where the
    repeated record carries the same numbers as the one already counted.
    Otherwise returns a fresh 5-list holding only the per-field growth, and
    updates *prior* in place so a third snapshot is measured against the new
    high-water mark. Fields that shrank contribute 0: a later snapshot is never
    allowed to subtract from a day that has already been published.
    """
    delta = [0, 0, 0, 0, 0]
    grew = False
    for i in range(5):
        step = counts[i] - prior[i]
        if step > 0:
            delta[i] = step
            prior[i] = counts[i]
            grew = True
    return delta if grew else None


def _local_midnight_epoch(day: dt.date) -> float:
    """POSIX timestamp of local midnight starting *day*."""
    return dt.datetime.combine(day, dt.time.min).timestamp()


class Indexer:
    """Incremental ``**/*.jsonl`` scanner implementing ``TranscriptIndexer``.

    Construct with no arguments for production defaults; every collaborator is
    injectable so tests can point at fixtures and drive the clock::

        Indexer(projects_dir=tmp, state_path=tmp / "scan_state.json",
                now=lambda: fixed_epoch)

    All methods do I/O and belong on the background thread, except
    :meth:`progress`.
    """

    def __init__(
        self,
        *,
        projects_dir: os.PathLike[str] | str = PROJECTS_DIR,
        state_path: os.PathLike[str] | str = SCAN_STATE_PATH,
        lookback_days: int | None = None,
        settings: Mapping[str, Any] | None = None,
        pricing: PricingTable | None = None,
        chunk_files: int = DEFAULT_CHUNK_FILES,
        now: Callable[[], float] = time.time,
        state_loader: Callable[[], Any] | None = None,
        state_saver: Callable[[Mapping[str, Any]], None] | None = None,
        dedup_within_file: bool = True,
        defer_state_commit: bool = False,
    ) -> None:
        self._projects_dir = os.path.abspath(os.fspath(projects_dir))
        self._state_path = os.fspath(state_path)
        if lookback_days is None:
            source = settings if settings is not None else SETTINGS_DEFAULTS
            raw_lookback = source.get("lookback_days", SETTINGS_DEFAULTS["lookback_days"])
            lookback_days = int(raw_lookback) if isinstance(raw_lookback, (int, float)) else 30
        self._lookback_days = max(1, int(lookback_days))
        self._pricing = pricing
        self._chunk_files = max(1, int(chunk_files))
        self._now = now
        self._state_loader = state_loader
        self._state_saver = state_saver
        self._dedup_within_file = bool(dedup_within_file)
        self._defer_state_commit = bool(defer_state_commit)
        """When True, ``scan_once`` leaves the advanced offsets in memory and the
        owner must call :meth:`commit_state` *after* the matching deltas are
        durable. Persisting an offset before the tokens it consumed have been
        saved turns any crash in that window into a silent, permanent
        undercount, so the wired app (``__main__``) always defers."""

        # --- scan state -----------------------------------------------------
        self._states: dict[str, FileScanState] = {}
        self._states_loaded = False
        self._states_dirty = False
        self._states_at_load = False
        """Whether the persisted scan state held anything when it was loaded.
        Read through :attr:`started_from_empty_state`; see that docstring for why
        the owner must consult it before the first merge."""

        # --- dedup (SPEC 2.2: current local day only) -----------------------
        # Maps requestId -> the five counters already credited to that request.
        # A plain set would be enough for exact repeats, but the real corpus
        # also contains *streaming snapshots*: two records sharing one
        # requestId where output_tokens grew between them (measured: 115 of
        # 5,440 requests in the 12 largest transcripts, e.g. 3 -> 591 output).
        # Holding the counters lets a later, larger snapshot contribute only
        # its increment instead of being dropped whole (undercount) or added
        # whole (double-count).
        self._dedup_day: DayKey | None = None
        self._dedup_usage: dict[str, list[int]] = {}
        self._dedup_loaded = False
        self._dedup_path = f"{os.path.splitext(self._state_path)[0]}_dedup.json"
        """Sidecar holding the *current local day's* dedup counters. Written by
        :meth:`flush_dedup` at shutdown and read once at startup, so a graceful
        restart in the middle of a request's streaming snapshots still credits
        only the growth of the later snapshot. It is deliberately NOT written per
        tick: it is worth nothing to the steady-state budget and everything to a
        clean restart."""

        # Dedup snapshots belonging to a file whose read was cut short. Keyed by
        # path so a RESUMED read sees the requests the earlier chunk already
        # credited; a deadline landing between two streaming snapshots of one
        # request would otherwise credit the second one whole. Entries are
        # dropped the moment a file is read to completion, so at most the
        # in-flight transcript is held (SPEC 2.2).
        self._resume_ids: dict[str, dict[str, list[int]]] = {}

        # --- progress -------------------------------------------------------
        self._complete = False
        self._files_done = 0
        self._files_total = 0
        self._progress = IndexProgress()

        self._scan_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @property
    def lookback_days(self) -> int:
        """Current lookback window in days."""
        return self._lookback_days

    def set_lookback_days(self, days: int) -> None:
        """Change the window. Widening it forces a re-index of the new days.

        Out-of-window files are deliberately *not* persisted in the scan state
        (see :meth:`_collect`), so widening the window simply makes them
        eligible again on the next pass — no cache invalidation needed.
        """
        days = max(1, int(days))
        if days == self._lookback_days:
            return
        self._lookback_days = days
        # Either direction invalidates "the window is fully indexed": widening
        # exposes untracked older files, narrowing changes the n/N denominator.
        self._complete = False
        self._files_done = 0
        self._files_total = 0
        self._publish_progress(scanning=False)

    @property
    def dedup_size(self) -> int:
        """Request IDs held for the current local day (diagnostics)."""
        return len(self._dedup_usage)

    @property
    def state_count(self) -> int:
        """Number of files tracked in the scan state (diagnostics)."""
        self._ensure_states_loaded()
        return len(self._states)

    # ------------------------------------------------------------------
    # Persistence (own atomic JSON; no dependency on state.py)
    # ------------------------------------------------------------------

    def _ensure_states_loaded(self) -> None:
        if self._states_loaded:
            return
        self._states_loaded = True
        raw: Any = None
        try:
            if self._state_loader is not None:
                raw = self._state_loader()
            else:
                with open(self._state_path, "rb") as fh:
                    raw = json.load(fh)
        except FileNotFoundError:
            raw = None
        except (OSError, ValueError):
            # Pure cache: a corrupt scan state costs one re-index, never a crash.
            raw = None
        self._states = scan_state_from_json(raw) if raw is not None else {}
        # `_complete` stays False on a warm start on purpose: a loaded state is
        # a claim about the past, and the first full pass is what verifies it.
        # In steady state that pass opens ~0 files, so verification is free.
        self._states_at_load = bool(self._states)
        if not self._states:
            # No offsets means the whole window is about to be re-read. The
            # dedup sidecar describes requests that were credited under the
            # offsets we just lost, so honouring it would SUPPRESS the re-read
            # and leave today reading $0. The two caches only make sense
            # together (see also `started_from_empty_state`).
            self._dedup_loaded = True

    @property
    def started_from_empty_state(self) -> bool:
        """True when this process began with **no** persisted offsets.

        The owner needs this because ``rollups.json`` and ``scan_state.json`` are
        two halves of one accounting fact and ``RollupStore.merge`` is purely
        additive: if the offsets are gone while the rollup survived, re-reading
        the window ADDS it on top of what is already there and every day in the
        window doubles - permanently, per loss, cumulatively. The owner must
        therefore empty the store before the first merge of such a run (the same
        invariant ``app.py``'s ``Rebuild cost index`` already documents).
        """
        self._ensure_states_loaded()
        return not self._states_at_load

    def commit_state(self) -> None:
        """Persist the advanced offsets (SPEC 3.2 step 8).

        Only needed when the indexer was built with ``defer_state_commit=True``:
        the owner calls this *after* the deltas of the matching
        :meth:`scan_once` are durable, so a crash can only ever cost a re-read
        (harmless) instead of consumed-but-unmerged bytes (a silent, permanent
        undercount that survives restarts).
        """
        with self._scan_lock:
            self._save_states()

    def _save_states(self) -> None:
        """Persist the scan state atomically (SPEC 3.2 step 8).

        ``fsync`` is intentionally skipped: ``os.replace`` already gives readers
        an all-or-nothing view, and the file is a pure cache whose worst-case
        loss is one re-index **provided the owner honours
        :attr:`started_from_empty_state`** and empties the rollup store when the
        offsets come back missing. Paying an fsync here would show up directly in
        the SPEC 2.1 tick budget.
        """
        if not self._states_dirty:
            return
        payload = scan_state_to_json(self._states)
        if self._state_saver is not None:
            self._state_saver(payload)
            self._states_dirty = False
            return
        tmp = f"{self._state_path}.tmp.{os.getpid()}"
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, self._state_path)
            self._states_dirty = False
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- dedup sidecar --------------------------------------------------

    def _load_dedup(self) -> tuple[DayKey | None, dict[str, list[int]]]:
        """Read the dedup sidecar, or ``(None, {})`` when there is nothing usable."""
        try:
            with open(self._dedup_path, "rb") as fh:
                raw = json.load(fh)
        except (FileNotFoundError, OSError, ValueError):
            return None, {}
        if not isinstance(raw, Mapping):
            return None, {}
        day = raw.get("day")
        requests = raw.get("requests")
        if not isinstance(day, str) or not isinstance(requests, Mapping):
            return None, {}
        out: dict[str, list[int]] = {}
        for key, value in requests.items():
            if not isinstance(value, list) or len(value) != 5:
                continue
            try:
                out[str(key)] = [_to_int(item) for item in value]
            except (TypeError, ValueError):  # pragma: no cover - _to_int is total
                continue
        return day, out

    def flush_dedup(self) -> None:
        """Persist the current day's dedup counters (call at shutdown).

        Complements the per-file offsets: the offsets say "these bytes are
        consumed", and this says "and these requests were credited these
        amounts". Without it, a restart landing between two streaming snapshots
        of one request credits the later snapshot **whole** instead of only its
        growth. Never raises - losing this file costs at most one over-credited
        in-flight request.
        """
        day = self._dedup_day
        if day is None:
            return
        usage = self._dedup_usage
        if len(usage) > _DEDUP_PERSIST_LIMIT:
            return
        tmp = f"{self._dedup_path}.tmp.{os.getpid()}"
        try:
            os.makedirs(os.path.dirname(self._dedup_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"day": day, "requests": usage}, fh, separators=(",", ":"))
            os.replace(tmp, self._dedup_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _drop_dedup_file(self) -> None:
        try:
            os.unlink(self._dedup_path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Walking the corpus
    # ------------------------------------------------------------------

    def _root_is_forbidden(self) -> bool:
        parts = self._projects_dir.split(os.sep)
        return any(part in parts for part in _FORBIDDEN_PATH_PARTS)

    def _iter_transcripts(self, errors: list[str]) -> Iterator[tuple[str, int, float, int]]:
        """Yield ``(abs_path, size, mtime, inode)`` for every transcript.

        SPEC 3.2 step 1. Exactly one ``stat`` per ``*.jsonl`` file and **zero**
        stats for anything else: the name test and ``entry.is_dir`` /
        ``entry.is_symlink`` are answered from the dirent's ``d_type``, and
        ``entry.stat()`` is the only stat call — we never follow it with an
        ``os.stat`` of our own.

        Directory symlinks are not followed, which is what keeps a symlinked
        project directory from being counted twice.

        A **missing root is silent**, exactly as ``CodexIndexer._iter_rollouts``
        treats an absent ``~/.codex``: an absent corpus is a normal state, not a
        failure. The widget installs into ``~/.claude``, so ``~/.claude`` exists
        long before ``~/.claude/projects`` does, and a machine that runs Codex
        only would otherwise carry a permanent red
        ``! cost: 1 file(s) unreadable: …/projects: [Errno 2]`` line on every
        300 s tick, with no way to clear it. A root that exists but cannot be
        walked is still reported.
        """
        if self._root_is_forbidden():
            errors.append(
                f"refusing to index {self._projects_dir!r}: contains a claude-swap "
                "backup path (would double-count sessions)"
            )
            return
        if not os.path.isdir(self._projects_dir):
            return
        stack = [self._projects_dir]
        while stack:
            current = stack.pop()
            try:
                scandir_it = os.scandir(current)
            except OSError as exc:
                errors.append(f"{current}: {exc}")
                continue
            with scandir_it:
                while True:
                    try:
                        entry = next(scandir_it)
                    except StopIteration:
                        break
                    except OSError as exc:  # pragma: no cover - dir vanished mid-walk
                        errors.append(f"{current}: {exc}")
                        break
                    name = entry.name
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if name.startswith(".") or name in _EXCLUDED_DIR_NAMES:
                                continue
                            if name in _FORBIDDEN_PATH_PARTS:
                                continue
                            stack.append(entry.path)
                            continue
                        if not name.endswith(TRANSCRIPT_SUFFIX):
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        # File deleted between readdir and stat: nothing to count.
                        continue
                    yield entry.path, st.st_size, st.st_mtime, st.st_ino

    def has_anything_changed(self) -> bool:
        """Cheap pre-check the caller uses to skip the whole scan (SPEC 3.5).

        Returns ``True`` on the first file whose ``(size, mtime)`` differs from
        the stored pair, so a quiet corpus costs one ``scandir`` walk plus one
        ``stat`` and one dict lookup per transcript — no file is opened, no JSON
        is parsed, no state is written.

        Also ``True`` whenever the first index is still incomplete: there is
        pending work regardless of what the filesystem says.

        Cost on the measured corpus: **~16 ms** for 3,210 transcripts. That is
        the same walk :meth:`scan_once` performs, so calling both on one tick
        pays for it twice. Use this when the caller wants to avoid the publish /
        state-write path entirely; a caller that simply calls
        :meth:`scan_once` is already inside the SPEC 2.1 budget, because an
        unchanged corpus opens no files.
        """
        self._ensure_states_loaded()
        if not self._complete:
            return True
        errors: list[str] = []
        states = self._states
        window_start_epoch = self._window_start_epoch(local_day_key(self._now()))
        for path, size, mtime, _inode in self._iter_transcripts(errors):
            prev = states.get(path)
            if prev is not None:
                if not prev.unchanged(size, mtime):
                    return True
                continue
            if mtime >= window_start_epoch:
                return True  # a brand-new in-window transcript
        return bool(errors)

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    def progress(self) -> IndexProgress:
        """Immutable progress snapshot. Main-thread safe, allocation-free."""
        return self._progress

    def _publish_progress(
        self,
        *,
        scanning: bool,
        finished_at: float | None = None,
        duration_ms: float | None = None,
    ) -> None:
        previous = self._progress
        self._progress = IndexProgress(
            files_done=self._files_done,
            files_total=self._files_total,
            complete=self._complete,
            scanning=scanning,
            last_scan_finished_at=(
                finished_at if finished_at is not None else previous.last_scan_finished_at
            ),
            last_scan_duration_ms=(
                duration_ms if duration_ms is not None else previous.last_scan_duration_ms
            ),
        )

    # ------------------------------------------------------------------
    # Window helpers
    # ------------------------------------------------------------------

    def _window_start_key(self, today: DayKey) -> DayKey:
        window = day_keys_back(today, self._lookback_days)
        return window[0] if window else today

    def _window_start_epoch(self, today: DayKey) -> float:
        return _local_midnight_epoch(parse_day_key(self._window_start_key(today)))

    def _roll_dedup_day(self, today: DayKey) -> None:
        """SPEC 3.3 trap 3 / SPEC 2.2: drop the dedup set at day rollover.

        The map only ever holds the *current local day's* request IDs, so its
        size is bounded by one day of traffic and it is discarded — not grown —
        when the local date changes.

        On the very first call of a process the sidecar written by the previous
        run is adopted when (and only when) it is for the same local day, so a
        restart does not re-credit a request whose earlier snapshot is already in
        the rollup. A sidecar is never adopted when the offsets were lost — see
        :meth:`_ensure_states_loaded`.
        """
        if self._dedup_day == today:
            return
        if not self._dedup_loaded:
            self._dedup_loaded = True
            stored_day, stored_usage = self._load_dedup()
            if stored_day == today:
                self._dedup_day = today
                self._dedup_usage = stored_usage
                return
        self._dedup_day = today
        self._dedup_usage = {}

    # ------------------------------------------------------------------
    # The scan
    # ------------------------------------------------------------------

    def scan_once(self, *, deadline: float | None = None) -> ScanResult:
        """Run one incremental pass; see ``TranscriptIndexer.scan_once``.

        Never raises for a per-file problem: those land in
        :attr:`ScanResult.errors` and the pass continues.
        """
        if not self._scan_lock.acquire(blocking=False):
            return ScanResult(
                progress=self._progress,
                errors=("scan already in progress",),
            )
        try:
            return self._scan_locked(deadline=deadline)
        finally:
            self._scan_lock.release()

    def scan_stream(
        self,
        *,
        chunk_deadline_seconds: float | None = None,
        max_passes: int | None = None,
    ) -> Iterator[ScanResult]:
        """First-run mode: yield one :class:`ScanResult` per chunk.

        Each yield hands the caller real deltas to merge and publish, so the
        menu fills in progressively and the AppKit main thread is never waiting
        on the whole 1.38 GB corpus (SPEC 3.2 "First run"). Control returns to
        the caller between chunks — and, with *chunk_deadline_seconds*, between
        files and every few thousand lines inside a file.

        Stops when the index is complete, when a pass finds nothing new, or
        after *max_passes* passes.
        """
        passes = 0
        while True:
            deadline = (
                time.monotonic() + chunk_deadline_seconds
                if chunk_deadline_seconds is not None
                else None
            )
            result = self.scan_once(deadline=deadline)
            passes += 1
            yield result
            if result.progress.complete and not result.changed:
                return
            if result.progress.complete and result.files_read == 0:
                return
            if max_passes is not None and passes >= max_passes:
                return

    def reset(self) -> None:
        """Discard all scan state, forcing a full re-index of the window."""
        self._states = {}
        self._states_loaded = True
        self._states_at_load = False
        self._states_dirty = False
        self._dedup_day = None
        self._dedup_usage = {}
        # A reset re-reads from offset 0, so the sidecar would suppress exactly
        # the records the rebuild is meant to re-credit.
        self._dedup_loaded = True
        self._resume_ids = {}
        self._drop_dedup_file()
        self._complete = False
        self._files_done = 0
        self._files_total = 0
        self._publish_progress(scanning=False)
        if self._state_saver is not None:
            self._state_saver({})
            return
        try:
            os.unlink(self._state_path)
        except OSError:
            pass

    # -- internals ------------------------------------------------------

    def _collect(
        self, errors: list[str], window_start_epoch: float
    ) -> tuple[list[tuple[str, int, float, int]], set[str], int, int, int]:
        """Steps 1-3: walk, then apply the two cheap skips.

        Returns ``(work, seen_paths, files_seen, skipped_unchanged,
        skipped_out_of_window)`` where *work* holds only files that actually
        need to be opened. In steady state *work* has ~40 entries out of ~3,200
        files walked.
        """
        states = self._states
        work: list[tuple[str, int, float, int]] = []
        seen: set[str] = set()
        files_seen = 0
        skipped_unchanged = 0
        skipped_out_of_window = 0
        for record in self._iter_transcripts(errors):
            path, size, mtime, _inode = record
            files_seen += 1
            seen.add(path)
            prev = states.get(path)
            # STEP 2 - the hot exit. 3,160 of 3,210 files stop right here
            # (measured), and nothing above this line touched the file beyond
            # its dirent stat.
            #
            # The inode is deliberately NOT part of this test, even though we
            # have it: contracts states that a file answering `unchanged()` True
            # must not be opened, and SPEC 3.2 puts the inode guard at step 4,
            # i.e. only on files that got past here. The consequence is a known
            # blind spot — a file replaced by one of *identical size* whose
            # mtime was then forced back is invisible. No appending writer can
            # produce that; `os.replace` of a transcript changes size or mtime,
            # so the step-4 inode guard does fire in practice. Closing the hole
            # would cost one int compare here and one line of the contract.
            if prev is not None and prev.unchanged(size, mtime):
                skipped_unchanged += 1
                continue
            # STEP 3 - lookback window. Deliberately AFTER step 2 and computed
            # from the already-read mtime, so it costs one float compare.
            # Out-of-window files are NOT written to the scan state: leaving
            # them untracked is what makes widening `lookback_days` re-read
            # them instead of silently skipping them forever.
            if mtime < window_start_epoch:
                skipped_out_of_window += 1
                continue
            work.append(record)
        return work, seen, files_seen, skipped_unchanged, skipped_out_of_window

    def _scan_locked(self, *, deadline: float | None) -> ScanResult:
        started_monotonic = time.monotonic()
        self._ensure_states_loaded()

        wall_now = self._now()
        today = local_day_key(wall_now)
        self._roll_dedup_day(today)  # TRAP 3 / SPEC 2.2
        window_start_key = self._window_start_key(today)
        window_start_epoch = _local_midnight_epoch(parse_day_key(window_start_key))

        errors: list[str] = []
        stats = _Stats()
        counters: dict[DayKey, dict[ModelKey, list[int]]] = {}

        self._publish_progress(scanning=True)

        work, seen_paths, files_seen, skipped_unchanged, skipped_out = self._collect(
            errors, window_start_epoch
        )
        walk_ok = not errors

        if not self._complete:
            # Monotone n/N for the "indexing… n/N" line: total never shrinks,
            # and newly appearing files raise it rather than rewinding `done`.
            self._files_total = max(self._files_total, self._files_done + len(work))

        interrupted = False
        processed = 0
        for path, size, mtime, inode in work:
            if processed >= self._chunk_files:
                interrupted = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                interrupted = True
                break
            new_state, stopped_early = self._scan_file(
                path,
                size=size,
                mtime=mtime,
                inode=inode,
                today=today,
                window_start_key=window_start_key,
                counters=counters,
                stats=stats,
                errors=errors,
                deadline=deadline,
            )
            if new_state is not None:
                self._states[path] = new_state
                self._states_dirty = True
            processed += 1
            if not self._complete:
                self._files_done += 1
                if self._files_done > self._files_total:
                    self._files_total = self._files_done
                self._publish_progress(scanning=True)
            if stopped_early:
                interrupted = True
                break

        if not interrupted and processed >= len(work):
            # A full pass with no leftovers: the window is fully indexed.
            if walk_ok and len(self._states) != len(seen_paths):
                # Drop state for transcripts that no longer exist. Only ever on
                # a clean full pass — a transient scandir error must not be
                # allowed to evict live entries.
                for gone in [p for p in self._states if p not in seen_paths]:
                    del self._states[gone]
                    self._states_dirty = True
            if not self._complete:
                self._complete = True
                self._files_done = self._files_total

        deltas = tuple(
            DayRollup(
                day=day,
                models={
                    model: ModelUsage.from_counters(counter)
                    for model, counter in sorted(models.items())
                },
            )
            for day, models in sorted(counters.items())
        )
        unknown = self._unknown_models(counters)
        # STEP 8, deliberately AFTER the deltas exist. Persisting "these bytes
        # are consumed" before the caller has the tokens they contained turns any
        # raise between the two into a silent, permanent undercount. With
        # `defer_state_commit` the caller goes further and only calls
        # `commit_state()` once the rollup itself is durable.
        if not self._defer_state_commit:
            self._save_states()
        duration_ms = (time.monotonic() - started_monotonic) * 1000.0
        self._publish_progress(
            scanning=False, finished_at=self._now(), duration_ms=duration_ms
        )
        return ScanResult(
            deltas=deltas,
            progress=self._progress,
            files_seen=files_seen,
            files_skipped_unchanged=skipped_unchanged,
            files_skipped_out_of_window=skipped_out,
            files_read=stats.files_read,
            bytes_read=stats.bytes_read,
            lines_seen=stats.lines_seen,
            lines_parsed=stats.lines_parsed,
            records_counted=stats.records_counted,
            records_duplicate=stats.records_duplicate,
            records_malformed=stats.records_malformed,
            unknown_models=unknown,
            errors=tuple(errors),
            duration_ms=duration_ms,
        )

    def _unknown_models(
        self, counters: Mapping[DayKey, Mapping[ModelKey, list[int]]]
    ) -> tuple[str, ...]:
        """TRAP 5: surface the *literal* unrecognised model strings.

        Derived from the accumulated buckets at publish time — a handful of
        keys — so the hot loop pays nothing and never imports pricing. With no
        price table injected we report nothing rather than guessing.
        """
        if self._pricing is None:
            return ()
        found: set[str] = set()
        for models in counters.values():
            for model in models:
                if model not in found and not self._pricing.is_known(model):
                    found.add(model)
        return tuple(sorted(found))

    def _scan_file(
        self,
        path: str,
        *,
        size: int,
        mtime: float,
        inode: int,
        today: DayKey,
        window_start_key: DayKey,
        counters: dict[DayKey, dict[ModelKey, list[int]]],
        stats: _Stats,
        errors: list[str],
        deadline: float | None,
    ) -> tuple[FileScanState | None, bool]:
        """Steps 4-7 for one transcript.

        Returns ``(new_state, stopped_early)``. ``stopped_early`` is True when a
        deadline cut the read short mid-file; the returned state then records
        ``size == offset`` so the next pass sees a ``(size, mtime)`` mismatch and
        resumes at exactly that byte. Storing the real size there would make the
        file look fully consumed and silently lose its tail.
        """
        prev = self._states.get(path)

        # STEP 4 - truncation / rotation guard.
        offset = 0
        if prev is not None and not prev.needs_reset(size, inode):
            offset = prev.offset
        if offset > size:
            offset = 0

        if offset >= size:
            # mtime moved but nothing was appended (a touch, or a rewrite to the
            # same length caught by the inode check above). Never open the file.
            return FileScanState(inode=inode, size=size, mtime=mtime, offset=offset), False

        # TRAP 4 fallback: a record with no usable timestamp is attributed to
        # the file's local mtime day, not dropped.
        fallback_day = local_day_key(mtime)

        dedup_today = self._dedup_usage
        # Bounded extra safety for days other than today: the real corpus
        # repeats requestIds on historical days too (measured: 5,922 exact
        # repeats in the 12 largest transcripts). Scoped to the file currently
        # open and dropped when it closes, so peak stays ~2 MB even on the
        # 36 MB transcript rather than growing with the whole 1.38 GB corpus.
        file_ids: dict[str, list[int]] | None = None
        if self._dedup_within_file:
            # A RESUMED read must see what the earlier chunk already credited.
            # `scan_once` cuts mid-file on a deadline (the routine first-index
            # path over a 1.4 GB corpus), and a cut landing between two streaming
            # snapshots of one request would otherwise credit the second one
            # whole - input, output and the entire cache_creation again.
            file_ids = self._resume_ids.pop(path, None)
            if file_ids is None:
                file_ids = {}

        pos = offset
        stopped_early = False
        lines_since_check = 0
        check_every = _DEADLINE_LINE_INTERVAL if deadline is not None else 0

        try:
            # `buffering=0` on purpose: we do our own buffering below, and a
            # BufferedReader on top of it would only add a second copy of every
            # chunk.
            with open(path, "rb", buffering=0) as fh:
                stats.files_read += 1
                if offset:
                    fh.seek(offset)  # STEP 5 - never read the whole file
                # One reusable buffer, and a `bytes`-shaped object is
                # materialised ONLY for a line that passed the marker test.
                #
                # `for raw in fh` was measured at a 8.35 MB tracemalloc peak on
                # the file holding the corpus's largest line (4.13 MB): the line
                # object plus BufferedReader's own growth, for a line that does
                # not even contain `"usage"` and is never parsed. The same file
                # through this loop peaks at 4.70 MB. It also removes ~330k
                # short-lived multi-kilobyte allocations per full index, which is
                # what drove the allocator high-water past the RSS budget - the
                # review's own stage-disable attribution put 38.9 MB of the
                # growth on the raw line iteration, not on JSON or dedup.
                buf = bytearray()
                eof = False
                while not eof:
                    chunk = fh.read(_READ_BUFFER)
                    if chunk:
                        buf += chunk
                    else:
                        eof = True
                    start = 0
                    while True:
                        newline = buf.find(_NEWLINE, start)
                        if newline < 0:
                            # STEP 6 - whatever is left is a trailing partial
                            # line from a session being written right now (or the
                            # tail of a chunk). It is neither counted nor allowed
                            # to advance `pos`; the next read completes it.
                            break
                        end = newline + 1
                        pos += end - start
                        stats.lines_seen += 1

                        # STEP 7 - skips ~49% of parses, and does it without
                        # copying the line first.
                        if buf.find(USAGE_MARKER, start, newline) >= 0:
                            self._consume_line(
                                buf[start:end],
                                fallback_day=fallback_day,
                                today=today,
                                window_start_key=window_start_key,
                                counters=counters,
                                stats=stats,
                                dedup_today=dedup_today,
                                file_ids=file_ids,
                            )
                        start = end

                        if check_every:
                            lines_since_check += 1
                            if lines_since_check >= check_every:
                                lines_since_check = 0
                                if time.monotonic() >= deadline:
                                    stopped_early = True
                                    break
                    if start:
                        del buf[:start]
                    if stopped_early:
                        break
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            # Keep whatever we counted; record the offset we actually reached so
            # the next pass resumes instead of re-counting.
            if pos > offset:
                return (
                    FileScanState(inode=inode, size=pos, mtime=mtime, offset=pos),
                    False,
                )
            return None, False

        stats.bytes_read += pos - offset
        if stopped_early:
            if file_ids:
                # Hand the snapshots to the pass that resumes this file. Only
                # ever one entry at a time: `_scan_locked` breaks out of its work
                # list as soon as a file stops early, and the entry is popped
                # (and not re-stored) once the file is read to the end.
                self._resume_ids[path] = file_ids
            return FileScanState(inode=inode, size=pos, mtime=mtime, offset=pos), True
        # Real size retained even when a partial line was discarded: offset <
        # size is legitimate, and the completed line arrives with a new
        # (size, mtime) pair that re-opens the file at this offset.
        return FileScanState(inode=inode, size=size, mtime=mtime, offset=pos), False

    def _consume_line(
        self,
        raw: bytes | bytearray,
        *,
        fallback_day: DayKey,
        today: DayKey,
        window_start_key: DayKey,
        counters: dict[DayKey, dict[ModelKey, list[int]]],
        stats: _Stats,
        dedup_today: dict[str, list[int]],
        file_ids: dict[str, list[int]] | None,
    ) -> None:
        """Parse one candidate line and fold it into the mutable counters.

        Handles traps 1-6; the trap comments live at the exact decision points.
        Nothing parsed is retained: no record objects, no lists of anything.
        """
        stats.lines_parsed += 1
        try:
            record = json.loads(raw)
        except ValueError:
            # JSONDecodeError and UnicodeDecodeError are both ValueError.
            stats.records_malformed += 1
            return
        if not isinstance(record, dict):
            stats.records_malformed += 1
            return

        message = record.get("message")
        if not isinstance(message, dict):
            # The marker matched inside some other payload (a tool result that
            # quotes the word "usage", for instance). Not malformed, not usage.
            return
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return  # TRAP 6 - no usage object: skip, never assume zeros

        counts = _extract_counters(usage)  # TRAPS 1 + 2 + 6 live in there
        if counts is None:
            stats.records_malformed += 1  # TRAP 6 - partial usage object
            return
        if not (counts[0] or counts[1] or counts[2] or counts[3] or counts[4]):
            # All five zero (e.g. a synthetic assistant record). Nothing to add
            # and no bucket to create.
            return

        # TRAP 4 - local-time day bucketing, so "today" is the user's today.
        day = local_day_key_from_iso(record.get("timestamp")) or fallback_day
        if day < window_start_key:
            # In-window file, out-of-window record (day keys sort lexically).
            return

        # TRAP 3 - dedup on requestId, else message id. A resumed or copied
        # session repeats records; per-file offsets cannot see that.
        is_increment = False
        request_id = record.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            request_id = message.get("id")
            if not isinstance(request_id, str) or not request_id:
                request_id = None
        if request_id is not None:
            # The persistent map covers the current local day only and is
            # dropped at rollover, so memory stays bounded (SPEC 2.2). Other
            # days use the file-scoped map.
            seen = dedup_today if day == today else file_ids
            if seen is not None:
                prior = seen.get(request_id)
                if prior is None:
                    seen[request_id] = list(counts)
                else:
                    stats.records_duplicate += 1
                    is_increment = True
                    counts = _increment_over(prior, counts)
                    if counts is None:
                        # An exact repeat of an already-counted request: the
                        # ordinary dedup case. Contribute nothing.
                        return
                    # A larger streaming snapshot of the same request: credit
                    # only the growth, so the day total ends up at the final
                    # figure whether or not the earlier snapshot was seen.

        # TRAP 5 - keep the RAW model string. Canonicalisation and pricing
        # happen later (contracts rule 4), so an unrecognised model keeps its
        # literal name for the menu and still gets its tokens counted.
        model = message.get("model")
        if not isinstance(model, str) or not model:
            model = UNKNOWN_MODEL

        day_bucket = counters.get(day)
        if day_bucket is None:
            day_bucket = counters[day] = {}
        acc = day_bucket.get(model)
        if acc is None:
            day_bucket[model] = counts
        else:
            acc[0] += counts[0]
            acc[1] += counts[1]
            acc[2] += counts[2]
            acc[3] += counts[3]
            acc[4] += counts[4]
        if not is_increment:
            # records_counted counts distinct requests; a snapshot increment
            # belongs to a request already counted (and already tallied in
            # records_duplicate).
            stats.records_counted += 1


TranscriptScanner = Indexer
"""Alias: the concrete scanner, for callers that prefer the noun."""


def build_indexer(
    settings: Mapping[str, Any] | None = None,
    *,
    pricing: PricingTable | None = None,
    projects_dir: os.PathLike[str] | str = PROJECTS_DIR,
    state_path: os.PathLike[str] | str = SCAN_STATE_PATH,
    chunk_files: int = DEFAULT_CHUNK_FILES,
) -> Indexer:
    """Convenience factory: build an :class:`Indexer` from a settings mapping."""
    return Indexer(
        projects_dir=projects_dir,
        state_path=state_path,
        settings=settings,
        pricing=pricing,
        chunk_files=chunk_files,
    )
