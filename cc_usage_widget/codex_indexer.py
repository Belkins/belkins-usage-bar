"""Incremental Codex (OpenAI) rollout scanner — SPEC-CODEX 4.

The second :class:`~cc_usage_widget.contracts.TranscriptSource`. It runs the
**same eight-step algorithm** as :mod:`cc_usage_widget.indexer` (SPEC 3.2) —
``scandir`` → ``(size, mtime)`` skip → lookback bound → truncation/inode guard →
``seek(offset)`` → substring pre-filter → parse → atomic state — against a
different corpus, a different record shape and a different set of traps.

Measured corpus on this machine (2026-08-17, probed, not assumed):

============================================  ==========================
``~/.codex/sessions/**/rollout-*.jsonl``      ~15 GB / ~3,000 files
Files touched in 24 h                         ~90 (362 in 7 days)
Files inside the 30-day lookback              1,153 / 12.9 GB
Largest single rollout                        379 MB, 50,951 lines
``token_count`` lines in that rollout         10,260 (8.3 MB of 379 MB)
Lines reaching ``json.loads`` corpus-wide     ~40% of lines, ~5% of bytes
Longest line inside the 30-day lookback       **15.85 MB** (see _MAX_LINE_BYTES)
Longest line anywhere in the corpus           **35.38 MB** (80 d old, outside it)
============================================  ==========================

So the shape of the win is the same as Claude's and the numbers are, if
anything, more favourable: **in steady state almost every one of the ~3,000
files must exit at the ``(size, mtime)`` comparison without ever being opened**,
and inside a file that *did* grow only the appended bytes are read. Of the bytes
that are read, ~95% never reach ``json.loads`` — they fail both substring
pre-filters in place, inside one reusable ``bytearray``, without being copied or
decoded. A 379 MB transcript is never ``read()`` whole; it is streamed 64 KiB at
a time from its stored offset, and no single line is ever held past
:data:`_MAX_LINE_BYTES`.

Measured end to end against the real corpus (state redirected to a temp dir;
``~/.codex`` was only ever read):

* **first index** — ~1,150 files / ~13 GB in 21 chunked passes,
  **21.0 s wall / 18.9 s CPU**, 635,409 turns counted, 21,751 duplicate
  emissions suppressed (trap 4), **0 malformed**, 0 errors;
* **steady-state tick — 8.3 / 8.5 / 9.1 ms CPU**, ~3,000 files walked, **0
  opened**, against SPEC 2.1's < 30 ms budget;
* **peak process RSS 53.8 MB** (17.5 MB interpreter + 6.3 MB imports + ~30 MB
  of scan), against SPEC 2.1's < 70 MB budget. ``tracemalloc`` peak on a
  synthetic file whose longest line is 5 MB: under 4 MiB.

Record shapes this module cares about (both are one JSON object per line)::

    {"timestamp":"2026-08-04T08:11:10.595Z","type":"turn_context",
     "payload":{"model":"gpt-5.6-sol", ...}}

    {"timestamp":"2026-08-04T08:11:25.812Z","type":"event_msg",
     "payload":{"type":"token_count",
                "info":{"total_token_usage":{...},
                        "last_token_usage":{"input_tokens":27512,
                                            "cached_input_tokens":6912,
                                            "cache_write_input_tokens":0,
                                            "output_tokens":635,
                                            "reasoning_output_tokens":516,
                                            "total_tokens":28147}},
                "rate_limits":{"primary":{"used_percent":46.0,
                                          "window_minutes":10080,
                                          "resets_at":1786173359},
                               "plan_type":"pro", ...}}}

Note the asymmetry that drives half of this file: ``turn_context`` is a
**top-level** ``type``, while ``token_count`` is an ``event_msg`` whose
``payload.type`` names it.

The four traps
--------------

1. :ref:`Stateful model attribution <trap-1>` — the model is on a *different
   record* than the usage, so the extractor carries state, and that state must
   survive a mid-file resume.
2. :ref:`total_token_usage is neither a delta nor a total <trap-2>` — only
   ``last_token_usage`` may be summed.
3. :ref:`cached_input_tokens is a SUBSET of input_tokens <trap-3>` — uncached
   input must be derived by subtraction, never by addition.
4. :ref:`Repeated token_count emissions <trap-4>` — **not in the spec; found by
   probing the corpus.** Byte-identical ``token_count`` records repeat within a
   file and would be summed twice or three times. Measured over 60 recent
   rollouts: 5,389 of 126,997 records are re-emissions carrying 748,771,193
   input tokens — a **4.12% over-count** if left unguarded.

Where the 30-day window actually lands, as a sanity check on all four: 2.02 B
input tokens on ``gpt-5.6-sol``, then ``gpt-5.5`` (151 M), ``gpt-5.6-terra``,
``gpt-5.6-luna``, ``gpt-5.4``, ``gpt-5.4-mini``, ``codex-auto-review`` — and
96 M (0.8%) in ``codex:unknown``, the turns that ran before their rollout named
a model. ``codex-auto-review``, ``gpt-5.5`` and ``unknown`` come back in
:attr:`~cc_usage_widget.contracts.ScanResult.unknown_models`: this build's price
table publishes no rate for them, so their tokens are counted, priced at ``$0``
and **named in the menu** rather than borrowed from a neighbouring model
(SPEC 3.3 trap 5).

Threading (SPEC 2.3): every method here does I/O and belongs on the background
thread, except :meth:`CodexIndexer.progress`, :meth:`CodexIndexer.quota_rows`,
:meth:`CodexIndexer.available` and :meth:`CodexIndexer.root`, which read
immutable snapshots published by a single attribute assignment (atomic under the
GIL) and are safe to call from the AppKit main thread on every UI tick.

``~/.codex`` is **read-only** to this module. Nothing here writes, moves or
truncates anything under it; our own state lives beside the Claude state in
:data:`~cc_usage_widget.contracts.WIDGET_HOME`. Its **absence is normal, not an
error** (SPEC-CODEX 5.5): a Claude-only machine simply has no Codex section, and
:meth:`CodexIndexer.available` returns False without raising or logging.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from .contracts import (
    CODEX_SCAN_STATE_PATH,
    CODEX_SESSIONS_DIR,
    CODEX_WINDOW_MINUTES_WEEKLY,
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    DayKey,
    DayRollup,
    FileScanState,
    IndexProgress,
    ModelUsage,
    Pct,
    PricingTable,
    ScanResult,
    Vendor,
    VendorModelKey,
    day_keys_back,
    local_day_key,
    local_day_key_from_iso,
    make_vendor_key,
    parse_day_key,
    raw_model_of_key,
    scan_state_from_json,
    scan_state_to_json,
    unknown_model_key,
    vendor_label,
)

__all__ = [
    "CodexIndexer",
    "CodexTranscriptSource",
    "build_codex_indexer",
    "CodexQuota",
    "CODEX_USAGE_MARKER",
    "CODEX_MODEL_MARKER",
    "ROLLOUT_PREFIX",
    "ROLLOUT_SUFFIX",
    "DEFAULT_CHUNK_FILES",
]

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

CODEX_USAGE_MARKER: Final[bytes] = b'"token_count"'
"""SPEC-CODEX 4 usage pre-filter, the ``bytes`` twin of
:data:`~cc_usage_widget.contracts.CODEX_USAGE_PREFILTER`.

Kept as bytes so the ~80% of lines that fail it are never decoded. It matches
``"type":"token_count"`` and nothing else in this corpus: the two usage blocks
are spelled ``total_token_usage`` / ``last_token_usage`` (``token_usage``, not
``token_count``), so there is no self-match to filter out.
"""

CODEX_MODEL_MARKER: Final[bytes] = b'"model"'
"""SPEC-CODEX 2.1 **model** pre-filter — a deliberate *widening* of
:data:`~cc_usage_widget.contracts.CODEX_MODEL_PREFILTER` (``'"turn_context"'``).

⚠️ Assembly trap first, restated here because this is the module that would have
paid for it: SPEC-CODEX 4 says "prefilter substring is ``token_count``", but the
model is not on the usage record. Pre-filtering on the usage marker alone drops
every record that carries a model, and the whole corpus attributes to
``codex:unknown`` at ``$0`` — a scan that looks like it worked. Both markers are
tested, usage first because it is ~24x more common.

**Why wider than the contract's ``"turn_context"``.** Accepting a *superset* of
what the contract's prefilter accepts can only add records, never drop one, and
it buys a large accuracy gain that ``turn_context`` alone cannot. Probed over
a full 30-day window of a heavily-used corpus (~1,150 rollouts):

* ``turn_context`` only → **3.9%** of input tokens attribute to
  ``codex:unknown`` at ``$0``;
* also honouring ``world_state`` (``payload.state.model``) and
  ``thread_settings_applied`` (``payload.thread_settings.model``) → **0.8%**.

The gap is a whole class of session — sub-agent and forked rollouts — that emits
hundreds of ``token_count`` records before its first ``turn_context``. Those two
extra record types are *statements of the model by the same producer*, not
inferences: across the window, a ``turn_context`` agreed with the model a
preceding ``world_state`` / ``thread_settings_applied`` had stated **3,896 times
and disagreed 0 times**. ``turn_context`` remains the authoritative record; the
other two only fill in where it has not spoken yet.

One substring test covers all three (every one of them spells the field
``"model"``) and future-proofs against a fourth record type, at a measured cost
of 1.64% of lines / 2.65% of bytes reaching ``json.loads`` — 88.8 MB of a 3.36 GB
sample. The record type is then checked in :meth:`CodexIndexer._consume_model_line`,
so a line that merely mentions a model (a ``session_meta`` instruction blob)
sets nothing.
"""

ROLLOUT_PREFIX: Final[str] = "rollout-"
ROLLOUT_SUFFIX: Final[str] = ".jsonl"
"""Codex names every transcript ``rollout-<iso>-<session uuid>.jsonl`` under
``sessions/YYYY/MM/DD/``. Both halves are tested so a stray ``.jsonl`` dropped
into the tree is not indexed as usage."""

DEFAULT_CHUNK_FILES: Final[int] = 64
"""Files read per :meth:`CodexIndexer.scan_once` call.

Deliberately a quarter of the Claude indexer's 250: a Codex rollout averages
11 MB inside the lookback window against Claude's 0.4 MB, and the largest is
379 MB. The wall-clock bound that actually matters is the *deadline* (checked
between files and every :data:`_DEADLINE_LINE_INTERVAL` lines inside one), but a
smaller file chunk keeps the ``indexing… n/N`` line moving on a corpus whose
individual files are this large.
"""

_NEWLINE: Final[int] = 0x0A

_READ_BUFFER: Final[int] = 1 << 16
"""64 KiB, same as the Claude scanner. A 379 MB rollout is ~5,800 reads and the
peak allocation stays far inside the SPEC 2.1 < 10 MB budget; the floor is the
longest single line, and the longest line that survives a pre-filter here is a
~3.3 KB ``turn_context``."""

_DEADLINE_LINE_INTERVAL: Final[int] = 4096
"""Lines between ``time.monotonic()`` deadline checks *inside* one file.
Yielding only between files is not enough when one file can be 379 MB."""

_MAX_LINE_BYTES: Final[int] = 2 << 20
"""Hard cap on how much of a single line is ever held in memory (2 MiB).

**This is a memory-budget guard, and it is not theoretical.** A line must be
buffered whole before its terminating newline can be found, so without a cap the
peak allocation is the longest line the scan actually reaches. Inside the
default 30-day window that is **15.85 MB** (a single ``response_item`` holding a
pasted blob). Measured without this cap: ``tracemalloc`` peak **17.63 MB**
against SPEC 2.1's < 10 MB budget, and **85 MB of RSS growth** against a < 70 MB
whole-process budget, because a bytearray growing to 16 MB by repeated
``realloc`` leaves a high-water mark the allocator does not give back.

The window is **not** what makes this safe, which is why the cap is not sized
from it: the longest line in the whole corpus is **35.38 MB** (2026-05-27, 80
days old, in a 240 MB rollout), 2.2x the in-window worst case, and raising
``lookback_days`` past ~80 admits it. The drain holds regardless — an isolated
pass over that file peaks at **2.45 MB** traced allocation, i.e. the 35 MB line
is never materialised.

Nothing we need is anywhere near this size: over the 30-day window the longest
line containing :data:`CODEX_MODEL_MARKER` is **76 KB** and the longest
``token_count`` line is **0.9 KB**, so 2 MiB is ~26x headroom over the largest
record this module has ever had to parse. A line past the cap is *drained* — its
bytes are counted towards the file offset and discarded without being retained
— and if it did contain a marker, that is reported in
:attr:`~cc_usage_widget.contracts.ScanResult.errors` and counted as malformed,
so an oversized record can never be dropped silently.
"""

_MARKER_OVERLAP: Final[int] = 16
"""Bytes retained across a drain boundary so a marker split between two
discarded chunks is still found. Larger than the longest marker."""

_MAX_OVERSIZE_ERRORS: Final[int] = 4
"""Cap on oversized-line reports per file, so a pathological transcript cannot
turn :attr:`~cc_usage_widget.contracts.ScanResult.errors` into a memory leak."""

_EXCLUDED_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv"}
)

_QUOTA_SIDECAR_SUFFIX: Final[str] = "_quota.json"
"""The quota snapshot lives in a sidecar beside the scan state rather than
inside it, so :data:`~cc_usage_widget.contracts.CODEX_SCAN_STATE_PATH` keeps
exactly the ``{path: entry}`` shape
:func:`~cc_usage_widget.contracts.scan_state_from_json` expects. Mirrors the
Claude indexer's ``*_dedup.json`` sidecar."""

CODEX_PSEUDO_ACCOUNT_SLOT: Final[int] = 0
"""Slot for the Codex pseudo-account. Real claude-swap slots start at 1, so 0
sorts deterministically without ever colliding with one (contracts,
``AccountRow`` "Conventions for a Codex pseudo-account")."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


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
        "models_seen",
    )

    def __init__(self) -> None:
        self.files_read = 0
        self.bytes_read = 0
        self.lines_seen = 0
        self.lines_parsed = 0
        self.records_counted = 0
        self.records_duplicate = 0
        self.records_malformed = 0
        self.models_seen = 0


def _to_int(value: Any) -> int:
    """Coerce a JSON number to a non-negative ``int``; junk becomes 0.

    Deliberately total: a rollout written by a newer or older Codex build must
    never crash the scan (SPEC 3.3 trap 6).
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


def _to_pct(value: Any) -> Pct | None:
    """Coerce ``used_percent`` to a 0-100 float, or ``None`` when unusable.

    ``None`` is not 0: "not reported" renders as an em dash, never as ``0%``
    (:func:`~cc_usage_widget.contracts.format_pct`).
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        if result != result:  # NaN
            return None
        return min(100.0, max(0.0, result))
    return None


def _local_midnight_epoch(day: dt.date) -> float:
    """POSIX timestamp of local midnight starting *day*."""
    return dt.datetime.combine(day, dt.time.min).timestamp()


def _window_label(window_minutes: int) -> str:
    """Short human label for a rate-limit window width.

    ``10080`` -> ``"7d"``, ``300`` -> ``"5h"``, ``1440`` -> ``"24h"``. Used only
    for a window that is **not** the weekly one, so that an unexpected width is
    *surfaced* under its own name rather than silently rendered as a weekly
    figure (contracts, :data:`CODEX_WINDOW_MINUTES_WEEKLY`).
    """
    if window_minutes % 10080 == 0:
        weeks = window_minutes // 10080
        return f"{weeks * 7}d"
    if window_minutes % 1440 == 0:
        return f"{window_minutes // 1440}d"
    if window_minutes % 60 == 0:
        return f"{window_minutes // 60}h"
    return f"{window_minutes}m"


def _format_reset_clock(epoch: float, now: float) -> str:
    """Render an epoch reset instant the way the Claude account rows read.

    ``"10:59"`` when it falls today, ``"Aug 24 14:50"`` otherwise — the exact
    shapes ``accounts._reset_clock`` surfaces from claude-swap's own formatter,
    so the two vendors' rows line up in the menu.

    SPEC 4.3 says reset times are shown verbatim and never recomputed by us.
    That still holds: OpenAI hands us an **epoch number** rather than a string
    (contracts, ``AccountRow`` conventions), so exactly one formatting step is
    unavoidable. It happens here, once, on the way in — nothing downstream ever
    re-derives or adjusts it.
    """
    try:
        when = dt.datetime.fromtimestamp(epoch)
        today = dt.datetime.fromtimestamp(now).date()
    except (OSError, OverflowError, ValueError):  # pragma: no cover - absurd epoch
        return ""
    if when.date() == today:
        return when.strftime("%H:%M")
    return f"{when:%b} {when.day} {when:%H:%M}"


@dataclass(frozen=True, slots=True)
class CodexQuota:
    """The newest ``payload.rate_limits`` seen anywhere in the corpus.

    This is the **headline** Codex number (SPEC-CODEX 3a): Codex runs on a
    ChatGPT Pro subscription, so "how much have I used?" is answered by the
    plan's own ``used_percent``, not by the notional per-model dollars. It is a
    real figure reported by OpenAI, and it is the one element of the Codex
    section that is not labelled notional.

    ``observed_at`` is the **mtime of the rollout it came from**, not the time
    we read it. Rollouts are append-only, so a file's mtime tracks its last
    record, which makes it a correct and free ordering key across files scanned
    in arbitrary order — no ISO timestamp parsing in the hot loop.

    Deliberately *not* carried (SPEC-CODEX 3a, "Two things deliberately NOT
    built"): ``credits`` and ``spend_control_reached``. On this account they are
    inert (``has_credits: false``, ``balance: "0"``) and rendering a $0 credit
    balance would imply metered billing that is not happening.
    """

    used_percent: Pct | None = None
    window_minutes: int | None = None
    resets_at: float | None = None
    plan_type: str | None = None
    observed_at: float = 0.0

    @property
    def is_weekly(self) -> bool:
        """True when ``primary`` is the weekly window SPEC-CODEX 1 measured."""
        return self.window_minutes == CODEX_WINDOW_MINUTES_WEEKLY

    @property
    def is_empty(self) -> bool:
        """True when nothing usable was reported (render no row at all)."""
        return self.used_percent is None and not self.plan_type

    def to_json(self) -> dict[str, Any]:
        """Sidecar form; ``None`` fields are kept so the shape is stable."""
        return {
            "used_percent": self.used_percent,
            "window_minutes": self.window_minutes,
            "resets_at": self.resets_at,
            "plan_type": self.plan_type,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_json(cls, obj: Any) -> CodexQuota | None:
        """Inverse of :meth:`to_json`; ``None`` for anything unusable.

        Total by design — the sidecar is a pure cache, and a corrupt one must
        cost at most a missing quota row until the next rollout is appended to.
        """
        if not isinstance(obj, Mapping):
            return None
        window = obj.get("window_minutes")
        resets = obj.get("resets_at")
        plan = obj.get("plan_type")
        observed = obj.get("observed_at")
        return cls(
            used_percent=_to_pct(obj.get("used_percent")),
            window_minutes=(
                int(window)
                if isinstance(window, (int, float)) and not isinstance(window, bool)
                else None
            ),
            resets_at=(
                float(resets)
                if isinstance(resets, (int, float)) and not isinstance(resets, bool)
                else None
            ),
            plan_type=plan if isinstance(plan, str) and plan else None,
            observed_at=(
                float(observed)
                if isinstance(observed, (int, float)) and not isinstance(observed, bool)
                else 0.0
            ),
        )

    @classmethod
    def from_rate_limits(cls, rate_limits: Any, *, observed_at: float) -> CodexQuota | None:
        """Build from one record's ``payload.rate_limits``, or ``None``.

        ``rate_limits`` is legitimately ``null`` on a small minority of records
        (measured: 10 of 95,593), and ``primary`` can itself be ``null`` — both
        yield ``None`` rather than a row of zeros.
        """
        if not isinstance(rate_limits, Mapping):
            return None
        primary = rate_limits.get("primary")
        plan = rate_limits.get("plan_type")
        plan = plan if isinstance(plan, str) and plan else None
        if not isinstance(primary, Mapping):
            if plan is None:
                return None
            return cls(plan_type=plan, observed_at=observed_at)
        window = primary.get("window_minutes")
        resets = primary.get("resets_at")
        return cls(
            used_percent=_to_pct(primary.get("used_percent")),
            window_minutes=(
                int(window)
                if isinstance(window, (int, float)) and not isinstance(window, bool)
                else None
            ),
            resets_at=(
                float(resets)
                if isinstance(resets, (int, float)) and not isinstance(resets, bool)
                else None
            ),
            plan_type=plan,
            observed_at=observed_at,
        )

    def account_row(self, *, now: float) -> AccountRow:
        """Render as the read-only pseudo-account the menu shows.

        Follows the conventions frozen in ``contracts.AccountRow``: slot 0,
        ``alias`` the vendor label, no email, never active, ``switchable=False``
        so no renderer can offer to switch to it, and no ``pace_ahead`` (pace
        comes from ``claude_swap.pace``; we do not derive burn rates, SPEC 3.1).

        The weekly ``primary`` window maps onto ``seven_day_pct`` /
        ``seven_day_resets_at``. There is no 5-hour window, so
        ``five_hour_pct`` stays ``None`` and renders as an em dash, never
        ``0%``. A ``primary`` of some *other* width is **not** silently rendered
        as a weekly figure: it goes into ``scoped_windows`` under its own label
        (``"24h"``), which the menu already knows how to render.
        """
        reset_text = (
            _format_reset_clock(self.resets_at, now) if self.resets_at is not None else None
        )
        seven_day_pct: Pct | None = None
        seven_day_resets: str | None = None
        scoped: tuple[tuple[str, Pct], ...] = ()
        scoped_resets: tuple[tuple[str, str], ...] = ()
        if self.is_weekly or self.window_minutes is None:
            # window_minutes absent is treated as the weekly window because that
            # is the only width this corpus has ever reported (95,580 of 95,583
            # records) - but a *different* stated width is never coerced.
            seven_day_pct = self.used_percent
            seven_day_resets = reset_text or None
        else:
            label = _window_label(self.window_minutes)
            if self.used_percent is not None:
                scoped = ((label, self.used_percent),)
            if reset_text:
                scoped_resets = ((label, reset_text),)
        age = now - self.observed_at if self.observed_at else None
        return AccountRow(
            slot=CODEX_PSEUDO_ACCOUNT_SLOT,
            alias=vendor_label(VENDOR_CODEX),
            email="",
            is_active=False,
            five_hour_pct=None,
            seven_day_pct=seven_day_pct,
            scoped_windows=scoped,
            five_hour_resets_at=None,
            seven_day_resets_at=seven_day_resets,
            scoped_resets_at=scoped_resets,
            usage_age_seconds=age if age is not None and age >= 0 else None,
            pace_ahead=(),
            vendor=VENDOR_CODEX,
            switchable=False,
            plan_type=self.plan_type,
        )


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------


class CodexIndexer:
    """Incremental ``rollout-*.jsonl`` scanner implementing ``TranscriptSource``.

    Construct with no arguments for production defaults; every collaborator is
    injectable so tests can point at a fixture tree and drive the clock, and so
    **no test ever needs to touch the read-only** ``~/.codex``::

        CodexIndexer(sessions_dir=tmp / "sessions",
                     state_path=tmp / "codex_scan_state.json",
                     now=lambda: fixed_epoch)

    All methods do I/O and belong on the background thread, except
    :meth:`progress`, :meth:`quota_rows`, :meth:`available` and :attr:`root`.
    """

    def __init__(
        self,
        *,
        sessions_dir: os.PathLike[str] | str = CODEX_SESSIONS_DIR,
        state_path: os.PathLike[str] | str = CODEX_SCAN_STATE_PATH,
        lookback_days: int | None = None,
        settings: Mapping[str, Any] | None = None,
        pricing: PricingTable | None = None,
        chunk_files: int = DEFAULT_CHUNK_FILES,
        now: Callable[[], float] = time.time,
        state_loader: Callable[[], Any] | None = None,
        state_saver: Callable[[Mapping[str, Any]], None] | None = None,
        defer_state_commit: bool = False,
    ) -> None:
        self._sessions_dir = os.path.abspath(os.fspath(sessions_dir))
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
        self._defer_state_commit = bool(defer_state_commit)
        """When True, ``scan_once`` leaves the advanced offsets in memory and the
        owner must call :meth:`commit_state` *after* the matching deltas are
        durable. Persisting an offset before the tokens it consumed have been
        saved turns any crash in that window into a silent, permanent
        undercount."""

        # --- scan state -----------------------------------------------------
        self._states: dict[str, FileScanState] = {}
        self._states_loaded = False
        self._states_dirty = False
        self._states_at_load = False

        # --- quota (SPEC-CODEX 3a) -----------------------------------------
        self._quota: CodexQuota | None = None
        self._quota_dirty = False
        self._quota_loaded = False
        self._completed_while_absent = False
        """Set when :meth:`scan_once` declared the index complete only because
        the corpus was missing. If ``~/.codex`` later appears (a first Codex run
        on this machine), the claim is withdrawn so the first real index shows
        its ``indexing… n/N`` progress instead of presenting a partial total as
        final (SPEC 4.3)."""
        self._quota_path = (
            f"{os.path.splitext(self._state_path)[0]}{_QUOTA_SIDECAR_SUFFIX}"
        )

        # --- TRAP 4 resume carry -------------------------------------------
        # Fingerprint of the last token_count record counted in a file whose
        # read was cut short by a deadline, so the pass that RESUMES that file
        # can still recognise a repeated emission across the cut. Keyed by path
        # and popped the moment the file is read to completion, so at most the
        # single in-flight transcript is held (SPEC 2.2).
        self._resume_fingerprint: dict[str, tuple[Any, ...]] = {}

        # --- day-key memo ---------------------------------------------------
        # Keyed on the ISO timestamp truncated to the MINUTE. Every real UTC
        # offset is a whole number of minutes, so the local calendar date cannot
        # change inside one UTC minute - which makes this memo exact, not
        # approximate. Hit rate on the corpus is ~99%; it removes ~95k
        # fromisoformat + astimezone conversions from a full index.
        self._day_memo_key: str | None = None
        self._day_memo_value: DayKey | None = None

        # --- model-key memo -------------------------------------------------
        # raw model string -> vendor-qualified rollup key. A handful of entries
        # (6 distinct models in 30 days), so this is a bounded dict that removes
        # one f-string per counted record.
        self._key_memo: dict[str, VendorModelKey] = {}
        self._unknown_key: VendorModelKey = unknown_model_key(VENDOR_CODEX)

        # --- progress -------------------------------------------------------
        self._complete = False
        self._files_done = 0
        self._files_total = 0
        self._progress = IndexProgress(vendor=VENDOR_CODEX)

        self._scan_lock = threading.Lock()

    # ------------------------------------------------------------------
    # TranscriptSource identity
    # ------------------------------------------------------------------

    @property
    def vendor(self) -> Vendor:
        """Always :data:`~cc_usage_widget.contracts.VENDOR_CODEX`."""
        return VENDOR_CODEX

    @property
    def root(self) -> Path:
        """Corpus root. Shown in Settings; **never written to**."""
        return Path(self._sessions_dir)

    def available(self) -> bool:
        """True when ``~/.codex/sessions`` exists and is readable.

        False is a **normal state**, not an error (SPEC-CODEX 5.5): a
        Claude-only machine has no ``~/.codex`` and must see no Codex section,
        no error row and no zeroed cost. Cheap (one ``stat``) and non-throwing,
        because the owner calls it on every tick.
        """
        try:
            return os.path.isdir(self._sessions_dir) and os.access(
                self._sessions_dir, os.R_OK | os.X_OK
            )
        except OSError:  # pragma: no cover - isdir/access do not normally raise
            return False

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
        eligible again on the next pass — no cache invalidation needed. That
        matters more here than for Claude: the window is what stands between the
        first index and ~15 GB.
        """
        days = max(1, int(days))
        if days == self._lookback_days:
            return
        self._lookback_days = days
        self._complete = False
        self._files_done = 0
        self._files_total = 0
        self._publish_progress(scanning=False)

    @property
    def state_count(self) -> int:
        """Number of rollouts tracked in the scan state (diagnostics)."""
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
        # `_complete` stays False on a warm start on purpose: a loaded state is a
        # claim about the past, and the first full pass is what verifies it. In
        # steady state that pass opens ~0 files, so verification is free.
        self._states_at_load = bool(self._states)

    @property
    def started_from_empty_state(self) -> bool:
        """True when this process began with **no** persisted offsets.

        The owner needs this because ``rollups.json`` and the scan state are two
        halves of one accounting fact and ``RollupStore.merge`` is purely
        additive: if the offsets are gone while the rollup survived, re-reading
        the window ADDS it on top of what is already there and every Codex day
        in the window doubles. The owner must therefore drop this vendor's
        contribution before the first merge of such a run.
        """
        self._ensure_states_loaded()
        return not self._states_at_load

    def commit_state(self) -> None:
        """Persist the advanced offsets and the quota snapshot (SPEC 3.2 step 8).

        Only needed when built with ``defer_state_commit=True``: the owner calls
        this *after* the deltas of the matching :meth:`scan_once` are durable, so
        a crash can only ever cost a re-read (harmless) instead of
        consumed-but-unmerged bytes (a silent, permanent undercount).
        """
        with self._scan_lock:
            self._save_states()
            self._save_quota()

    def _save_states(self) -> None:
        """Persist the scan state atomically (SPEC 3.2 step 8).

        ``fsync`` is intentionally skipped: ``os.replace`` already gives readers
        an all-or-nothing view, and the file is a pure cache whose worst case is
        one re-index **provided the owner honours**
        :attr:`started_from_empty_state`. Paying an fsync here would show up
        directly in the SPEC 2.1 tick budget.

        A separate file from the Claude ``scan_state.json`` by contract: two
        indexers on two background threads never contend on one atomic
        temp-file + ``os.replace`` write, and deleting one vendor's cache
        re-indexes only that vendor.
        """
        if not self._states_dirty:
            return
        payload = scan_state_to_json(self._states)
        if self._state_saver is not None:
            self._state_saver(payload)
            self._states_dirty = False
            return
        if self._atomic_write_json(self._state_path, payload):
            self._states_dirty = False

    def _atomic_write_json(self, path: str, payload: Any) -> bool:
        """Temp file + ``os.replace``. Never raises; returns success."""
        tmp = f"{path}.tmp.{os.getpid()}"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, path)
            return True
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    # -- quota sidecar --------------------------------------------------

    def _ensure_quota_loaded(self) -> None:
        """Adopt the persisted quota snapshot exactly once.

        Without this, a restart on a quiet machine shows **no** Codex quota row:
        the scan state says every rollout is fully consumed, so nothing is
        opened, so no ``rate_limits`` record is seen, so the headline
        subscription figure (SPEC-CODEX 3a) silently disappears until the next
        Codex turn. The snapshot is small, immutable and self-dating —
        :attr:`AccountRow.usage_age_seconds` carries its age so a stale one
        shows its age rather than implying live data (SPEC 4.3).
        """
        if self._quota_loaded:
            return
        self._quota_loaded = True
        try:
            with open(self._quota_path, "rb") as fh:
                raw = json.load(fh)
        except (FileNotFoundError, OSError, ValueError):
            return
        stored = CodexQuota.from_json(raw)
        if stored is not None and not stored.is_empty:
            self._quota = stored

    def _save_quota(self) -> None:
        if not self._quota_dirty or self._quota is None:
            return
        if self._atomic_write_json(self._quota_path, self._quota.to_json()):
            self._quota_dirty = False

    def _drop_quota_file(self) -> None:
        try:
            os.unlink(self._quota_path)
        except OSError:
            pass

    def _offer_quota(self, candidate: CodexQuota | None) -> None:
        """Keep *candidate* when it is newer than what we hold.

        Newness is the source rollout's mtime, which for an append-only file
        tracks its last record — so files scanned in arbitrary order still
        resolve to the genuinely newest quota.

        **A percent-less candidate never displaces a percentage.** A
        ``rate_limits`` block whose ``primary`` is null but whose ``plan_type``
        is set yields a row with ``used_percent=None``
        (:meth:`CodexQuota.from_rate_limits`), and such records are real: the
        live corpus has files whose last two ``rate_limits`` records are a
        complete one followed by a plan-only one. Letting the later one win
        blanks ``used_percent``, and because ``is_empty`` is False (the plan is
        set) the row survives every emptiness check while ``_quota_windows()``
        has nothing to render — the headline subscription bar (SPEC-CODEX 3a),
        the one Codex figure that is not notional, silently disappears and the
        blanked snapshot is persisted to the sidecar. A null ``primary`` is a
        *missing report*, not a report of "no quota", so a percent-less
        candidate may only ever contribute a ``plan_type``, and only while we
        hold no percentage at all. Symmetrically, a percentage always beats no
        percentage regardless of mtime order — otherwise one plan-only newest
        file suppresses every percentage in the corpus behind it.

        ``observed_at`` therefore always dates the **percentage being shown**,
        which is exactly what ``AccountRow.usage_age_seconds`` claims about it.
        """
        if candidate is None or candidate.is_empty:
            return
        current = self._quota
        if current is None:
            self._quota = candidate
            self._quota_dirty = True
            return
        if candidate.used_percent is None:
            # A missing report. It may only contribute a plan name, and only
            # when we have no percentage of our own to protect.
            if current.used_percent is not None:
                return
            if candidate.observed_at < current.observed_at:
                return
            winner = candidate
        elif current.used_percent is None:
            # Any real percentage beats none, whatever the mtime order in which
            # the two files happened to be walked: without this, one plan-only
            # newest file suppresses every percentage in the whole corpus.
            winner = replace(
                candidate, plan_type=candidate.plan_type or current.plan_type
            )
        elif candidate.observed_at < current.observed_at:
            return
        else:
            winner = candidate
        if winner == current:
            return
        self._quota = winner
        self._quota_dirty = True

    # ------------------------------------------------------------------
    # Quota (TranscriptSource.quota_rows)
    # ------------------------------------------------------------------

    def quota(self) -> CodexQuota | None:
        """The newest quota snapshot, or ``None`` (diagnostics + tests)."""
        self._ensure_quota_loaded()
        return self._quota

    def quota_rows(self) -> tuple[AccountRow, ...]:
        """The read-only Codex pseudo-account, or ``()`` (SPEC-CODEX 4/5.1).

        Reports what the last scan learned; it never scans of its own accord, so
        it is cheap enough for the main thread on every UI tick. The one caveat
        is the **very first** call in a process, which reads the small quota
        sidecar (a few hundred bytes) — the background thread's first
        :meth:`scan_once` normally gets there first and primes it.

        Returns ``()`` — not a row of zeros — when nothing has been observed, so
        a Claude-only machine renders no Codex quota block at all
        (SPEC-CODEX 5.5).
        """
        self._ensure_quota_loaded()
        snapshot = self._quota
        if snapshot is None or snapshot.is_empty:
            return ()
        return (snapshot.account_row(now=self._now()),)

    # ------------------------------------------------------------------
    # Walking the corpus
    # ------------------------------------------------------------------

    def _iter_rollouts(self, errors: list[str]) -> Iterator[tuple[str, int, float, int]]:
        """Yield ``(abs_path, size, mtime, inode)`` for every rollout.

        SPEC 3.2 step 1. Exactly one ``stat`` per ``rollout-*.jsonl`` and
        **zero** stats for anything else: the name test and ``entry.is_dir`` /
        ``entry.is_symlink`` are answered from the dirent's ``d_type``, and
        ``entry.stat()`` is the only stat call.

        A **missing root is silent** — no error is appended (SPEC-CODEX 5.5).
        Only a root that exists but cannot be walked is reported.
        """
        if not os.path.isdir(self._sessions_dir):
            return
        stack = [self._sessions_dir]
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
                            # Not followed: a symlinked session directory would
                            # otherwise be counted twice.
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if name.startswith(".") or name in _EXCLUDED_DIR_NAMES:
                                continue
                            stack.append(entry.path)
                            continue
                        if not (
                            name.startswith(ROLLOUT_PREFIX) and name.endswith(ROLLOUT_SUFFIX)
                        ):
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        # Deleted between readdir and stat: nothing to count.
                        continue
                    yield entry.path, st.st_size, st.st_mtime, st.st_ino

    def has_anything_changed(self) -> bool:
        """Cheap pre-check the caller uses to skip the whole scan (SPEC 3.5).

        Returns ``True`` on the first rollout whose ``(size, mtime)`` differs
        from the stored pair, so a quiet corpus costs one ``scandir`` walk plus
        one ``stat`` and one dict lookup per rollout — no file is opened, no JSON
        is parsed, no state is written. Also ``True`` while the first index is
        incomplete: there is pending work regardless of the filesystem.

        Returns ``False`` immediately when the corpus is absent, which is the
        normal Claude-only case.
        """
        if not self.available():
            return False
        self._ensure_states_loaded()
        if not self._complete:
            return True
        errors: list[str] = []
        states = self._states
        window_start_epoch = self._window_start_epoch(local_day_key(self._now()))
        for path, size, mtime, _inode in self._iter_rollouts(errors):
            prev = states.get(path)
            if prev is not None:
                if not prev.unchanged(size, mtime):
                    return True
                continue
            if mtime >= window_start_epoch:
                return True  # a brand-new in-window rollout
        return bool(errors)

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    def progress(self) -> IndexProgress:
        """Immutable progress snapshot, stamped with this source's vendor.

        Main-thread safe and allocation-free.
        """
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
            vendor=VENDOR_CODEX,
        )

    # ------------------------------------------------------------------
    # Window helpers
    # ------------------------------------------------------------------

    def _window_start_key(self, today: DayKey) -> DayKey:
        window = day_keys_back(today, self._lookback_days)
        return window[0] if window else today

    def _window_start_epoch(self, today: DayKey) -> float:
        return _local_midnight_epoch(parse_day_key(self._window_start_key(today)))

    def _day_key_for(self, timestamp: Any, fallback_day: DayKey) -> DayKey:
        """Local-time day bucket for one record (SPEC 3.3 trap 4).

        Rollouts write UTC with a trailing ``Z``; the bucket is the **local**
        date so "today" matches the user's clock. A record with no usable
        timestamp falls back to the file's local mtime day rather than being
        dropped.

        Memoised on the minute **plus the UTC-offset designator**, never on the
        minute alone. "Every real UTC offset is a whole number of minutes, so
        the local date cannot change within one UTC minute" is only true when
        every timestamp carries the *same* offset:
        ``2026-08-17T01:30:00+05:00`` and ``2026-08-17T01:30:00-05:00`` are ten
        hours apart yet share that 16-character prefix, and they fall on
        different local days (2026-08-16 vs 2026-08-17). With a prefix-only memo
        the second record silently inherits the first's day — money moves
        between Today / 7d / 30d, and a record pushed before
        ``window_start_key`` is dropped for good. The memo lives on the indexer,
        so the collision spans files within one scan, not just one file.

        Fractional seconds are excluded so the memo still hits for every record
        in the same minute (that hit rate is the whole point of it). Today's
        corpus is uniformly ``Z`` — 110,475 of 110,475 timestamps in the 200
        most recent rollouts — so this is latent, not live; it becomes live the
        moment Codex, or a rollout synced from another host, writes an offset.
        """
        if not isinstance(timestamp, str) or not timestamp:
            return fallback_day
        # Walk back over the offset's own digits and colon to its sign, or stop
        # on the trailing `Z`. Bounded by "YYYY-MM-DDTHH:MM:SS" so a naive
        # timestamp (no designator at all) yields "" and memoises on the minute,
        # which is correct for it: naive timestamps all mean local time.
        i = len(timestamp) - 1
        while i >= 19 and (timestamp[i].isdigit() or timestamp[i] == ":"):
            i -= 1
        zone = timestamp[i:] if i >= 19 and timestamp[i] in "Z+-" else ""
        memo_key = timestamp[:16] + zone
        if memo_key == self._day_memo_key and self._day_memo_value is not None:
            return self._day_memo_value
        resolved = local_day_key_from_iso(timestamp)
        if resolved is None:
            return fallback_day
        self._day_memo_key = memo_key
        self._day_memo_value = resolved
        return resolved

    def _rollup_key(self, model: str | None) -> VendorModelKey:
        """Vendor-qualified rollup key for a raw model string.

        ``"gpt-5.6-sol"`` -> ``"codex:gpt-5.6-sol"``; an unknown or absent model
        -> ``"codex:unknown"``. The unknown bucket is **per vendor** so a Codex
        model with no published rate never shares a menu row — or a ``$0``
        explanation — with an unrecognised Claude model (SPEC-CODEX 2.1/3).
        """
        if not model:
            return self._unknown_key
        cached = self._key_memo.get(model)
        if cached is not None:
            return cached
        key = make_vendor_key(VENDOR_CODEX, model)
        self._key_memo[model] = key
        return key

    # ------------------------------------------------------------------
    # The scan
    # ------------------------------------------------------------------

    def scan_once(self, *, deadline: float | None = None) -> ScanResult:
        """Run one incremental pass; see ``TranscriptSource.scan_once``.

        Never raises for a per-file problem: those land in
        :attr:`ScanResult.errors` and the pass continues. An absent corpus
        returns an empty, **complete** result — a Claude-only machine must not
        be held behind an ``indexing…`` label forever
        (:meth:`IndexProgress.combined` makes ``complete`` the AND of every
        source).
        """
        if not self._scan_lock.acquire(blocking=False):
            return ScanResult(
                progress=self._progress,
                errors=("scan already in progress",),
                vendor=VENDOR_CODEX,
            )
        try:
            if not self.available():
                # SPEC-CODEX 5.5: absent ~/.codex is normal, not an error. It is
                # reported COMPLETE rather than perpetually indexing, because
                # `IndexProgress.combined` ANDs completeness across sources and a
                # Claude-only machine must not sit behind an `indexing…` label
                # forever waiting on a corpus that will never exist.
                if not self._complete:
                    self._complete = True
                    self._completed_while_absent = True
                    self._publish_progress(scanning=False)
                return ScanResult(progress=self._progress, vendor=VENDOR_CODEX)
            if self._completed_while_absent:
                # The corpus appeared after we declared completeness on its
                # absence. Withdraw the claim so the first real index shows its
                # progress instead of presenting a partial total as final.
                self._completed_while_absent = False
                self._complete = False
                self._files_done = 0
                self._files_total = 0
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

        Each yield hands the caller real deltas to merge and publish, so the menu
        fills in progressively and the AppKit main thread is never waiting on the
        12.9 GB the lookback window admits. Control returns to the caller between
        chunks — and, with *chunk_deadline_seconds*, between files and every few
        thousand lines inside a file.

        Stops when the index is complete, when a pass finds nothing new, or after
        *max_passes* passes.
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
        """Discard **this vendor's** scan state only, forcing a re-index.

        Claude's ``scan_state.json`` is a different file and is untouched, which
        is precisely why the two vendors do not share one (contracts,
        :data:`CODEX_SCAN_STATE_PATH`).

        The quota snapshot is deliberately kept: it is an observation about the
        subscription, not an accumulation, so re-reading cannot double-count it
        and dropping it would blank the headline figure until the next Codex
        turn.
        """
        self._states = {}
        self._states_loaded = True
        self._states_at_load = False
        self._states_dirty = False
        self._resume_fingerprint = {}
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

    def reset_all(self) -> None:
        """:meth:`reset` and additionally forget the quota snapshot."""
        self.reset()
        self._quota = None
        self._quota_dirty = False
        self._quota_loaded = True
        self._drop_quota_file()

    # -- internals ------------------------------------------------------

    def _collect(
        self, errors: list[str], window_start_epoch: float
    ) -> tuple[list[tuple[str, int, float, int]], set[str], int, int, int]:
        """Steps 1-3: walk, then apply the two cheap skips.

        Returns ``(work, seen_paths, files_seen, skipped_unchanged,
        skipped_out_of_window)`` where *work* holds only rollouts that actually
        need to be opened. **In steady state *work* has ~90 entries out of ~3,000
        walked** — and after the first tick of a session, ~0.
        """
        states = self._states
        work: list[tuple[str, int, float, int]] = []
        seen: set[str] = set()
        files_seen = 0
        skipped_unchanged = 0
        skipped_out_of_window = 0
        for record in self._iter_rollouts(errors):
            path, size, mtime, _inode = record
            files_seen += 1
            seen.add(path)
            prev = states.get(path)
            # STEP 2 - the hot exit. This is the single predicate that keeps a
            # ~15 GB corpus inside a 30 ms tick, and nothing above this line
            # touched the file beyond its dirent stat.
            #
            # The inode is deliberately NOT part of this test, matching the
            # Claude indexer and SPEC 3.2, which puts the inode guard at step 4
            # - i.e. only on files that got past here.
            if prev is not None and prev.unchanged(size, mtime):
                skipped_unchanged += 1
                continue
            # STEP 3 - lookback window. Deliberately AFTER step 2 and computed
            # from the already-read mtime, so it costs one float compare.
            # Out-of-window rollouts are NOT written to the scan state: leaving
            # them untracked is what makes widening `lookback_days` re-read them
            # instead of skipping them forever. On this corpus the window is
            # what stands between the first index and ~15 GB (12.9 GB in, 2.7
            # out at 30 days).
            if mtime < window_start_epoch:
                skipped_out_of_window += 1
                continue
            work.append(record)
        return work, seen, files_seen, skipped_unchanged, skipped_out_of_window

    def _scan_locked(self, *, deadline: float | None) -> ScanResult:
        started_monotonic = time.monotonic()
        self._ensure_states_loaded()
        self._ensure_quota_loaded()

        wall_now = self._now()
        today = local_day_key(wall_now)
        window_start_key = self._window_start_key(today)
        window_start_epoch = _local_midnight_epoch(parse_day_key(window_start_key))

        errors: list[str] = []
        stats = _Stats()
        counters: dict[DayKey, dict[VendorModelKey, list[int]]] = {}

        self._publish_progress(scanning=True)

        work, seen_paths, files_seen, skipped_unchanged, skipped_out = self._collect(
            errors, window_start_epoch
        )
        walk_ok = not errors

        if not self._complete:
            # Monotone n/N for the "Codex indexing… n/N" line: the total never
            # shrinks, and newly appearing files raise it rather than rewinding.
            self._files_total = max(self._files_total, self._files_done + len(work))

        interrupted = False
        processed = 0
        for path, size, mtime, inode in work:
            if processed >= self._chunk_files:
                interrupted = True
                break
            # `processed` guard: **always attempt at least one file per pass.**
            # An already-expired deadline would otherwise return an empty,
            # never-complete result forever, and `scan_stream` - whose stop
            # condition is "complete and nothing read" - would spin without ever
            # advancing a byte. One file is still bounded: the in-file check
            # below cuts after `_DEADLINE_LINE_INTERVAL` lines and the offset it
            # stores is real progress.
            if processed and deadline is not None and time.monotonic() >= deadline:
                interrupted = True
                break
            new_state, stopped_early = self._scan_file(
                path,
                size=size,
                mtime=mtime,
                inode=inode,
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
                # Drop state for rollouts that no longer exist. Only ever on a
                # clean full pass - a transient scandir error must not be allowed
                # to evict live entries.
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
        # `defer_state_commit` the owner goes further and only calls
        # `commit_state()` once the rollup itself is durable.
        if not self._defer_state_commit:
            self._save_states()
            self._save_quota()
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
            vendor=VENDOR_CODEX,
        )

    def _unknown_models(
        self, counters: Mapping[DayKey, Mapping[VendorModelKey, list[int]]]
    ) -> tuple[str, ...]:
        """Surface the *literal* unrecognised model strings (SPEC 3.3 trap 5).

        ``codex-auto-review`` is the live example: OpenAI publishes no rate for
        it, so it must be counted in tokens, priced at ``$0`` and **named** —
        never priced at ``gpt-5.6-sol``'s rate because it looks similar.

        The price table is queried with the **vendor-qualified** key, so a Codex
        model can only ever resolve against Codex rows; the strings returned are
        **raw** (``"codex-auto-review"``, not ``"codex:codex-auto-review"``)
        because :attr:`ScanResult.unknown_models` is user-visible text and the
        result already carries its vendor.

        Derived from the accumulated buckets at publish time — a handful of keys
        — so the hot loop pays nothing and never imports pricing. With no price
        table injected we report nothing rather than guessing.
        """
        if self._pricing is None:
            return ()
        found: set[str] = set()
        checked: set[VendorModelKey] = set()
        for models in counters.values():
            for key in models:
                if key in checked:
                    continue
                checked.add(key)
                if not self._pricing.is_known(key):
                    found.add(raw_model_of_key(key))
        return tuple(sorted(found))

    def _scan_file(
        self,
        path: str,
        *,
        size: int,
        mtime: float,
        inode: int,
        window_start_key: DayKey,
        counters: dict[DayKey, dict[VendorModelKey, list[int]]],
        stats: _Stats,
        errors: list[str],
        deadline: float | None,
    ) -> tuple[FileScanState | None, bool]:
        """Steps 4-7 for one rollout.

        Returns ``(new_state, stopped_early)``. ``stopped_early`` is True when a
        deadline cut the read short mid-file; the returned state then records
        ``size == offset`` so the next pass sees a ``(size, mtime)`` mismatch and
        resumes at exactly that byte. Storing the real size there would make the
        file look fully consumed and silently lose its tail.

        .. _trap-1:

        **TRAP 1 — STATEFUL MODEL ATTRIBUTION.** Unlike a Claude transcript,
        where ``message.model`` and ``message.usage`` share one record, a Codex
        rollout puts the model on a separate ``type: "turn_context"`` record
        (``payload.model``) and every following ``token_count`` belongs to it.
        The extractor therefore carries ``current_model`` across lines.

        **TRAP 1b — RESUME HAZARD.** When a scan resumes mid-file at a stored
        offset, the ``turn_context`` that set the current model is *behind* that
        offset and will never be re-read. Without persisting it, every turn after
        a resume attributes to ``codex:unknown`` at ``$0`` — silently, and on the
        routine first-index path where a 379 MB rollout takes many chunks. So
        ``current_model`` is seeded from :attr:`FileScanState.last_model` and
        written back into every state this method returns.

        When the model is *genuinely* unknown it stays unknown: 6 of the 40 most
        recent rollouts emit ``token_count`` records before their first
        ``turn_context`` (measured: 5,246 records), and no earlier record in
        those files carries a model. Those tokens are counted and priced at
        ``$0`` under ``codex:unknown``. **We never back-fill them from a model
        discovered later in the file, and never guess the file's dominant
        model** — an invented attribution is an invented price.
        """
        prev = self._states.get(path)

        # STEP 4 - truncation / rotation guard.
        offset = 0
        # TRAP 1b: restore the model the previous pass left off on. `needs_reset`
        # means the file was truncated or replaced, so we re-read from 0 and the
        # remembered model is meaningless - drop it with the offset.
        current_model: str | None = None
        if prev is not None and not prev.needs_reset(size, inode):
            offset = prev.offset
            current_model = prev.last_model
        if offset > size:
            offset = 0
            current_model = None

        if offset >= size:
            # mtime moved but nothing was appended (a touch, or a rewrite to the
            # same length caught by the inode check above). Never open the file.
            return (
                FileScanState(
                    inode=inode,
                    size=size,
                    mtime=mtime,
                    offset=offset,
                    last_model=current_model,
                ),
                False,
            )

        # SPEC 3.3 trap 4 fallback: a record with no usable timestamp is
        # attributed to the file's local mtime day, not dropped.
        fallback_day = local_day_key(mtime)

        # TRAP 4 carry across a mid-file cut (see `_consume_line`).
        last_fingerprint: tuple[Any, ...] | None = self._resume_fingerprint.pop(path, None)

        # SPEC-CODEX 3a: hold a reference to the most recent `rate_limits`
        # mapping seen in this file. It is a ~9-key dict, so retaining the
        # reference is cheaper than extracting four scalars 10,000 times, and the
        # enclosing record is still discarded immediately.
        newest_rate_limits: Any = None
        newest_has_primary = False
        """Whether `newest_rate_limits` carries a usable `primary` block, so a
        later percent-less record cannot blank the one we already have."""

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
                # One reusable buffer; a `bytes`-shaped object is materialised
                # ONLY for a line that passed a marker test. On the measured
                # 379 MB rollout that is 8.5 MB of 379 MB.
                buf = bytearray()
                dropped = 0
                """Bytes of the CURRENT line already discarded by the oversize
                drain. They still have to be added to `pos` when the line
                finally ends, or the stored offset would rewind the file."""
                oversize_hit = False
                oversize_errors = 0
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
                            # STEP 6 - whatever is left is a trailing partial line
                            # from a session being written right now (or the tail
                            # of a chunk). It is neither counted nor allowed to
                            # advance `pos`; the next read completes it.
                            break
                        end = newline + 1
                        pos += end - start + dropped
                        stats.lines_seen += 1

                        if dropped:
                            # Tail of a line whose head exceeded _MAX_LINE_BYTES
                            # and was discarded. It cannot be reassembled, so it
                            # is reported rather than silently skipped - but only
                            # when it actually looked like a record we wanted.
                            if oversize_hit or (
                                buf.find(CODEX_USAGE_MARKER, start, newline) >= 0
                                or buf.find(CODEX_MODEL_MARKER, start, newline) >= 0
                            ):
                                stats.records_malformed += 1
                                if oversize_errors < _MAX_OVERSIZE_ERRORS:
                                    oversize_errors += 1
                                    errors.append(
                                        f"{path}: skipped a line over "
                                        f"{_MAX_LINE_BYTES} bytes that carried a "
                                        "usage or model marker"
                                    )
                            dropped = 0
                            oversize_hit = False
                        # STEP 7 - the two-marker pre-filter, in place, before
                        # anything is copied or decoded. Usage first: it is ~24x
                        # more common than a model-bearing record. Testing only
                        # the usage marker would drop every model and send the
                        # whole corpus to `codex:unknown` at $0 (see
                        # CODEX_MODEL_MARKER).
                        else:
                            # The two tests are NOT mutually exclusive. A
                            # model-bearing record whose bytes merely CONTAIN
                            # b'"token_count"' - a turn_context with that string
                            # in `cwd`, say - matches the usage marker first;
                            # with an `elif` it would be handed to
                            # `_consume_usage_line`, correctly rejected there as
                            # a non-usage record, and then dropped, so the model
                            # it states is never read and every following turn is
                            # priced at the PREVIOUS model's rate (measured on a
                            # fixture: 10M sol tokens billed at luna's rate, a
                            # 22.8x under-report, silently). So the usage handler
                            # reports whether it actually consumed the line, and
                            # a false-positive marker match falls through to the
                            # model branch. The extra `find` only runs on lines
                            # the usage handler rejected - 0 of 280,305 marker
                            # matches in the 400 most recent live rollouts.
                            consumed = False
                            if buf.find(CODEX_USAGE_MARKER, start, newline) >= 0:
                                (
                                    rate_limits,
                                    fingerprint,
                                    consumed,
                                ) = self._consume_usage_line(
                                    buf[start:end],
                                    model=current_model,
                                    fallback_day=fallback_day,
                                    window_start_key=window_start_key,
                                    counters=counters,
                                    stats=stats,
                                    last_fingerprint=last_fingerprint,
                                )
                                if rate_limits is not None:
                                    # "Newest" means newest USABLE. A later
                                    # record whose `primary` is null is a
                                    # missing report, not a report of "no
                                    # quota", and letting it overwrite an
                                    # earlier complete one in the same file
                                    # blanks the headline percentage
                                    # (SPEC-CODEX 3a). Live example: a rollout
                                    # whose last two of 3,971 rate_limits
                                    # records are `used_percent: 100.0` then
                                    # `primary: null, plan_type: "pro"`.
                                    has_primary = isinstance(
                                        rate_limits.get("primary"), Mapping
                                    )
                                    if has_primary or not newest_has_primary:
                                        newest_rate_limits = rate_limits
                                        newest_has_primary = has_primary
                                if fingerprint is not None:
                                    last_fingerprint = fingerprint
                            if not consumed and (
                                buf.find(CODEX_MODEL_MARKER, start, newline) >= 0
                            ):
                                # TRAP 1: a turn_context (or a world_state /
                                # thread_settings_applied that states the same
                                # thing) sets the model for every token_count
                                # that follows.
                                found = self._consume_model_line(
                                    buf[start:end], stats=stats
                                )
                                if found is not None:
                                    current_model = found
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
                    if len(buf) > _MAX_LINE_BYTES:
                        # An incomplete line past the cap. Remember whether it
                        # looked interesting, count its bytes, and throw them
                        # away - keeping only enough tail that a marker straddling
                        # the boundary is still found. This is what holds the peak
                        # allocation at ~2 MiB against a longest line of 15.85 MB
                        # inside the default window, and a measured 2.45 MB
                        # against the 35.38 MB longest line in the whole corpus
                        # (see _MAX_LINE_BYTES).
                        if not oversize_hit and (
                            buf.find(CODEX_USAGE_MARKER) >= 0
                            or buf.find(CODEX_MODEL_MARKER) >= 0
                        ):
                            oversize_hit = True
                        keep = _MARKER_OVERLAP
                        dropped += len(buf) - keep
                        del buf[: len(buf) - keep]
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            self._offer_quota(
                CodexQuota.from_rate_limits(newest_rate_limits, observed_at=mtime)
            )
            # Keep whatever we counted; record the offset we actually reached so
            # the next pass resumes instead of re-counting.
            if pos > offset:
                return (
                    FileScanState(
                        inode=inode,
                        size=pos,
                        mtime=mtime,
                        offset=pos,
                        last_model=current_model,
                    ),
                    False,
                )
            return None, False

        stats.bytes_read += pos - offset
        self._offer_quota(
            CodexQuota.from_rate_limits(newest_rate_limits, observed_at=mtime)
        )
        if stopped_early:
            if last_fingerprint is not None:
                # Hand the TRAP 4 fingerprint to the pass that resumes this file,
                # so a cut landing between two identical emissions still
                # suppresses the second. Only ever one entry at a time:
                # `_scan_locked` breaks out of its work list as soon as a file
                # stops early, and the entry is popped (and not re-stored) once
                # the file is read to the end.
                self._resume_fingerprint[path] = last_fingerprint
            return (
                FileScanState(
                    inode=inode,
                    size=pos,
                    mtime=mtime,
                    offset=pos,
                    last_model=current_model,
                ),
                True,
            )
        # Real size retained even when a partial line was discarded: offset <
        # size is legitimate, and the completed line arrives with a new
        # (size, mtime) pair that re-opens the file at this offset.
        return (
            FileScanState(
                inode=inode,
                size=size,
                mtime=mtime,
                offset=pos,
                last_model=current_model,
            ),
            False,
        )

    # -- line handlers ---------------------------------------------------

    def _consume_model_line(
        self, raw: bytes | bytearray, *, stats: _Stats
    ) -> str | None:
        """TRAP 1: read the model a record states, for the turns that follow it.

        Returns the new current model, or ``None`` to leave it unchanged — a
        line that merely *mentions* a model (a ``session_meta`` instruction
        blob), or a record with no usable model, must never erase a model we
        already have.

        Three record shapes are honoured, in the order the corpus produces them:

        ``type: "turn_context"`` -> ``payload.model``
            The authoritative one, and the only one SPEC-CODEX 2.1 names. Note
            it is a **top-level** ``type``, not an ``event_msg`` payload type;
            looking for it under ``payload.type`` finds nothing.
        ``type: "world_state"`` -> ``payload.state.model``
        ``payload.type: "thread_settings_applied"`` -> ``payload.thread_settings.model``
            Same producer, same fact, stated earlier in the sub-agent and forked
            rollouts that emit hundreds of ``token_count`` records before their
            first ``turn_context``. Honouring them takes the corpus-wide share of
            input tokens stranded at ``codex:unknown``/``$0`` from **3.9% to
            0.8%**, and they never contradict a ``turn_context`` (measured:
            3,896 agreements, 0 disagreements). See :data:`CODEX_MODEL_MARKER`.

        What is deliberately *not* done: back-filling. The turns that ran before
        any of these three records spoke stay ``codex:unknown`` at ``$0`` even
        though a model is discovered later in the same file, and the file's
        dominant model is never assumed. An invented attribution is an invented
        price (SPEC 3.3 trap 5).
        """
        stats.lines_parsed += 1
        try:
            record = json.loads(raw)
        except ValueError:
            # JSONDecodeError and UnicodeDecodeError are both ValueError.
            stats.records_malformed += 1
            return None
        if not isinstance(record, dict):
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        record_type = record.get("type")
        if record_type == "turn_context":
            holder: Any = payload
        elif record_type == "world_state":
            holder = payload.get("state")
        elif payload.get("type") == "thread_settings_applied":
            holder = payload.get("thread_settings")
        else:
            return None
        if not isinstance(holder, Mapping):
            return None
        model = holder.get("model")
        if not isinstance(model, str) or not model:
            return None
        stats.models_seen += 1
        return model

    def _consume_usage_line(
        self,
        raw: bytes | bytearray,
        *,
        model: str | None,
        fallback_day: DayKey,
        window_start_key: DayKey,
        counters: dict[DayKey, dict[VendorModelKey, list[int]]],
        stats: _Stats,
        last_fingerprint: tuple[Any, ...] | None,
    ) -> tuple[Any, tuple[Any, ...] | None, bool]:
        """Parse one ``token_count`` record and fold it into the counters.

        Returns ``(rate_limits, fingerprint, consumed)``: the record's
        ``rate_limits`` mapping for the quota snapshot (or ``None``), the TRAP 4
        fingerprint of this record (or ``None`` when the record was not
        counted), and whether this line was ours at all.

        ``consumed`` is False for exactly one case: the pre-filter marker
        matched inside some *other* record's bytes. The caller must then try the
        model branch on the same line - a ``turn_context`` carrying the literal
        string ``"token_count"`` in a path or an argument is otherwise swallowed
        here and the model it states is lost, which silently prices every
        following turn at the previous model's rate. A line that failed to parse
        is ``consumed`` (it was already counted malformed here; re-parsing it as
        a model line would count it twice and find nothing).

        Nothing parsed is retained beyond those values: no record objects, no
        lists of anything.

        .. _trap-2:

        **TRAP 2 — ``total_token_usage`` IS NEITHER A DELTA NOR A TOTAL.** It is
        cumulative *and* it resets mid-session. Measured in one session: the sum
        of per-turn ``last_token_usage.input`` was **252,100,617** against a
        final ``total_token_usage.input`` of **230,324,294**. So it can be used
        neither as a per-turn delta nor as a session total, and only
        ``payload.info.last_token_usage`` is ever summed. It is read here for
        exactly one purpose — the duplicate fingerprint below — and never added
        to any counter.

        .. _trap-3:

        **TRAP 3 — ``cached_input_tokens`` IS A SUBSET OF ``input_tokens``.**
        OpenAI reports them overlapping where Anthropic reports them disjoint, so
        uncached input is ``input_tokens - cached_input_tokens``, clamped at 0.
        That subtraction, and the rest of the five-counter mapping, is **not
        performed here**: it lives in
        :meth:`ModelUsage.from_codex_last_token_usage`, because the price table
        reads the same method's contract to decide which slot carries the
        published cached rate. If this module and ``pricing`` disagreed about
        which slot holds cached input, every Codex dollar would be quietly wrong
        and no test in either module would notice.
        ``reasoning_output_tokens`` is likewise a subset of ``output_tokens`` and
        is never added again — also enforced there.

        .. _trap-4:

        **TRAP 4 — REPEATED ``token_count`` EMISSIONS.** Not in the spec; found
        by probing the corpus. A rollout re-emits byte-identical ``token_count``
        records, including an *unchanged* cumulative ``total_token_usage``::

            line 140  last_in=169204  tot_in=115452511
            line 141  last_in=169204  tot_in=115452511   <- same event again
            line 152  last_in=169204  tot_in=115452511   <- and again

        Summing all three triples that turn. Because ``total_token_usage`` is
        cumulative, a genuine new turn **must** move it (a turn that moved
        nothing has all-zero counters and is skipped anyway), so a record whose
        ``(total, last)`` pair equals the previously counted record's is a
        re-emission, not a new turn. Comparing against the immediately preceding
        counted record — rather than a set of every record seen — is what keeps
        this compatible with TRAP 2's mid-session resets: a reset changes the
        pair, so it can never be mistaken for a repeat.

        Measured over 60 recent rollouts: 5,389 of 126,997 records suppressed,
        carrying 748,771,193 input tokens — a **4.12% over-count** avoided.

        Idempotence, and why per-file offsets are the right mechanism here:
        a Codex ``token_count`` record carries **no request id**, so there is no
        Claude-style ``requestId`` to dedup on across files. It does not need
        one. Probed over 60 recent rollouts / 126,997 records: **zero**
        duplicates across files, and every one of the ~3,000 session UUIDs appears
        in exactly one file — Codex writes one append-only rollout per session
        and never replays a parent session's ``event_msg`` records into a
        resumed one, which is exactly the case Claude's cross-file dedup exists
        for. So "each line is consumed exactly once" is a *sufficient* idempotence
        guarantee, and that is what the byte offset gives. What offsets cannot
        see is the *within-file* repetition above, which is why TRAP 4 exists.

        Residual, stated rather than hidden: the TRAP 4 fingerprint is carried
        across a mid-file resume **in memory** (``_resume_fingerprint``) because
        the frozen ``FileScanState`` has no field for it. A process restart that
        lands exactly between two identical emissions therefore over-counts that
        one turn. Worst case is one turn per restart, against 4.12% of the corpus
        if the guard were absent.
        """
        stats.lines_parsed += 1
        try:
            record = json.loads(raw)
        except ValueError:
            stats.records_malformed += 1
            return None, None, True
        if not isinstance(record, dict):
            stats.records_malformed += 1
            return None, None, True
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            # The marker matched inside some other payload. Not malformed, not
            # usage - and possibly a model-bearing record, so the caller is told
            # this line was NOT consumed and re-tries it against the model
            # marker.
            return None, None, False

        # SPEC-CODEX 3a: the subscription quota rides along on every usage
        # record. Captured even when the usage half is unusable - the quota is
        # the headline number and does not depend on the token counts.
        rate_limits = payload.get("rate_limits")
        rate_limits = rate_limits if isinstance(rate_limits, Mapping) else None

        info = payload.get("info")
        if not isinstance(info, dict):
            return rate_limits, None, True

        # TRAP 2: `last_token_usage` only. `total_token_usage` is read solely for
        # the TRAP 4 fingerprint below and never enters a counter.
        last_usage = info.get("last_token_usage")
        usage = ModelUsage.from_codex_last_token_usage(last_usage)  # TRAP 3 lives there
        if usage is None:
            # A malformed or field-less usage block is skipped, never counted as
            # five zeros (SPEC 3.3 trap 6).
            stats.records_malformed += 1
            return rate_limits, None, True
        if usage.is_zero:
            # A real turn that consumed nothing (measured: 77 of 9,708 in one
            # rollout). Nothing to add and no bucket to create - and no
            # fingerprint either, since it was not counted.
            return rate_limits, None, True

        # TRAP 4 - the fingerprint of this accounting event.
        total_usage = info.get("total_token_usage")
        fingerprint: tuple[Any, ...] | None = None
        if isinstance(total_usage, Mapping):
            fingerprint = (
                _to_int(total_usage.get("input_tokens")),
                _to_int(total_usage.get("cached_input_tokens")),
                _to_int(total_usage.get("cache_write_input_tokens")),
                _to_int(total_usage.get("output_tokens")),
                usage.input,
                usage.output,
                usage.cache_write_5m,
                usage.cache_read,
            )
            if fingerprint == last_fingerprint:
                stats.records_duplicate += 1
                return rate_limits, fingerprint, True

        # SPEC 3.3 trap 4 - local-time day bucketing, so "today" is the user's.
        day = self._day_key_for(record.get("timestamp"), fallback_day)
        if day < window_start_key:
            # In-window file, out-of-window record (day keys sort lexically).
            return rate_limits, fingerprint, True

        # TRAP 1 - attribute to the model the last turn_context set. Keep the RAW
        # string: canonicalisation and pricing happen later, so an unrecognised
        # model keeps its literal name for the menu and still gets its tokens
        # counted at $0 (SPEC 3.3 trap 5, SPEC-CODEX 3).
        key = self._rollup_key(model)

        counts = usage.as_counters()
        day_bucket = counters.get(day)
        if day_bucket is None:
            day_bucket = counters[day] = {}
        acc = day_bucket.get(key)
        if acc is None:
            day_bucket[key] = list(counts)
        else:
            acc[0] += counts[0]
            acc[1] += counts[1]
            acc[2] += counts[2]
            acc[3] += counts[3]
            acc[4] += counts[4]
        stats.records_counted += 1
        return rate_limits, fingerprint, True


CodexTranscriptSource = CodexIndexer
"""Alias: the concrete Codex :class:`~cc_usage_widget.contracts.TranscriptSource`,
for callers that prefer the protocol's noun."""


def build_codex_indexer(
    settings: Mapping[str, Any] | None = None,
    *,
    pricing: PricingTable | None = None,
    sessions_dir: os.PathLike[str] | str = CODEX_SESSIONS_DIR,
    state_path: os.PathLike[str] | str = CODEX_SCAN_STATE_PATH,
    chunk_files: int = DEFAULT_CHUNK_FILES,
    defer_state_commit: bool = False,
) -> CodexIndexer:
    """Convenience factory: build a :class:`CodexIndexer` from a settings mapping.

    Always returns an indexer, even on a machine with no ``~/.codex``:
    constructing one is free (no I/O happens until the first scan) and
    :meth:`CodexIndexer.available` is the single place that decides whether it
    contributes anything. Gating on ``settings["codex_tracking_enabled"]`` is the
    owner's call, not this factory's.
    """
    return CodexIndexer(
        sessions_dir=sessions_dir,
        state_path=state_path,
        settings=settings,
        pricing=pricing,
        chunk_files=chunk_files,
        defer_state_commit=defer_state_commit,
    )
