"""Correctness tests for the transcript → tokens → cost path (SPEC 3.3, 3.4, 6).

Every case here writes **real** ``.jsonl`` files into a temporary directory and
runs the real :class:`~cc_usage_widget.indexer.Indexer` over them - no mocks, no
monkeypatched parsers - because the traps in SPEC 3.3 live in the interaction
between the reader, the extractor and the dedup map, and a mocked extractor
would test nothing.

Coverage map:

===========================================  ==============================================
SPEC 3.3 trap 1  ``usage.iterations``         :func:`test_iterations_are_not_double_counted`
SPEC 3.3 trap 2  split vs flat cache_creation :func:`test_cache_creation_split_beats_flat_field`
                                              :func:`test_flat_cache_creation_counts_as_5m`
SPEC 3.3 trap 3  duplicate ``requestId``      :func:`test_duplicate_request_id_counted_once`
SPEC 3.3 trap 4  local-time day bucketing     :func:`test_day_bucketing_uses_local_time`
SPEC 3.3 trap 5  unknown model                :func:`test_unknown_model_counts_tokens_costs_zero`
SPEC 3.3 trap 6  missing / partial ``usage``   :func:`test_missing_and_partial_usage_are_skipped`
SPEC 3.2 step 4  truncation guard             :func:`test_truncated_file_is_reindexed`
SPEC 3.2 step 5  trailing partial line        :func:`test_trailing_partial_line_is_not_consumed`
SPEC 3.4         Sonnet 5 intro → standard    :func:`test_sonnet5_rollover_prices_by_record_date`
                                              :func:`test_sonnet5_rollover_through_the_store`
SPEC 6.7         hand-computed total          :func:`test_end_to_end_hand_computed_total`
===========================================  ==============================================

Run with pytest if it is available, or directly - the module is its own runner::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    $PY tests/test_cost_math.py
    $PY -m pytest tests -q
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget.contracts import (  # noqa: E402
    UNKNOWN_MODEL,
    DayRollup,
    IndexProgress,
    ModelUsage,
    day_key_from_date,
    format_usd,
    local_day_key,
)
from cc_usage_widget.indexer import Indexer  # noqa: E402
from cc_usage_widget.pricing import DEFAULT_PRICING  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

FABLE = "claude-fable-5-20260514"
SONNET = "claude-sonnet-5-20260901"
HAIKU = "claude-haiku-4-5"
MYSTERY = "claude-quantum-9-20261231"

COMPLETE_INDEX = IndexProgress(files_done=1, files_total=1, complete=True)


def _epoch(year: int, month: int, day: int, hour: int = 12, minute: int = 0) -> float:
    """Local-time POSIX timestamp - the clock the indexer is given."""
    return dt.datetime(year, month, day, hour, minute).timestamp()


def _iso_utc(epoch: float) -> str:
    """A transcript-style UTC ``...Z`` timestamp for a local epoch."""
    moment = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _record(
    request_id: str | None,
    model: str | None,
    usage: Any,
    *,
    epoch: float | None = None,
    message_id: str | None = None,
    omit_usage: bool = False,
) -> dict[str, Any]:
    """One assistant record shaped like a real Claude Code transcript line."""
    message: dict[str, Any] = {"id": message_id or f"msg_{request_id}", "role": "assistant"}
    if model is not None:
        message["model"] = model
    if not omit_usage:
        message["usage"] = usage
    record: dict[str, Any] = {"type": "assistant", "message": message}
    if request_id is not None:
        record["requestId"] = request_id
    if epoch is not None:
        record["timestamp"] = _iso_utc(epoch)
    return record


def _write(path: Path, records: list[Any], *, trailing_partial: str = "") -> Path:
    """Write records as JSONL, optionally leaving a half-written final line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        if trailing_partial:
            handle.write(trailing_partial)
    return path


def _indexer(root: Path, *, now: float | None = None, lookback_days: int = 30) -> Indexer:
    """An indexer over *root* with its own scan state and an injected clock."""
    return Indexer(
        projects_dir=root / "projects",
        state_path=root / "scan_state.json",
        lookback_days=lookback_days,
        pricing=DEFAULT_PRICING,
        now=(lambda: now) if now is not None else __import__("time").time,
    )


def _by_day(result: Any) -> dict[str, dict[str, ModelUsage]]:
    """``ScanResult.deltas`` as ``{day: {raw model: ModelUsage}}``."""
    return {rollup.day: dict(rollup.models) for rollup in result.deltas}


def _usage(result: Any, day: str, model: str) -> ModelUsage:
    """The counters the scan attributed to one ``(day, model)`` pair."""
    return _by_day(result).get(day, {}).get(model, ModelUsage())


# ---------------------------------------------------------------------------
# SPEC 3.3 trap 1 - usage.iterations is a per-attempt breakdown
# ---------------------------------------------------------------------------


def test_iterations_are_not_double_counted() -> None:
    """``usage.iterations`` repeats the same request; only top level counts.

    The fixture's single iteration carries the *same* numbers as the top-level
    fields, which is what the real corpus does - so a summing bug shows up as
    exactly 2x, and a "sum the iterations instead" bug would still pass a
    weaker test that only checked one field.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-05"
        usage = {
            "input_tokens": 1_100,
            "output_tokens": 2_200,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 3_300,
                "ephemeral_1h_input_tokens": 4_400,
            },
            "cache_read_input_tokens": 5_500,
            # A per-attempt breakdown of this same request. Never summed.
            "iterations": [
                {
                    "input_tokens": 1_100,
                    "output_tokens": 2_200,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 3_300,
                        "ephemeral_1h_input_tokens": 4_400,
                    },
                    "cache_read_input_tokens": 5_500,
                }
            ],
        }
        _write(root / "projects" / "p" / "a.jsonl", [_record("r1", FABLE, usage, epoch=clock)])

        result = _indexer(root, now=clock).scan_once()

        got = _usage(result, day, FABLE)
        assert got == ModelUsage(1_100, 2_200, 3_300, 4_400, 5_500), got
        assert result.records_counted == 1, result.records_counted


# ---------------------------------------------------------------------------
# SPEC 3.3 trap 2 - cache_creation_input_tokens is the SUM of the sub-fields
# ---------------------------------------------------------------------------


def test_cache_creation_split_beats_flat_field() -> None:
    """With both present, the split fields win and the flat sum is ignored.

    ``cache_creation_input_tokens`` here is deliberately the true sum
    (300 + 400 = 700), so adding both would inflate cache writes to 1,400 -
    the exact double-count SPEC 3.3 trap 2 describes.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        usage = {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 300,
                "ephemeral_1h_input_tokens": 400,
            },
            "cache_creation_input_tokens": 700,
            "cache_read_input_tokens": 50,
        }
        _write(root / "projects" / "p" / "a.jsonl", [_record("r1", FABLE, usage, epoch=clock)])

        got = _usage(_indexer(root, now=clock).scan_once(), "2026-09-05", FABLE)

        assert got.cache_write_5m == 300, got
        assert got.cache_write_1h == 400, got
        assert got.cache_write_5m + got.cache_write_1h == 700, got
        assert got == ModelUsage(10, 20, 300, 400, 50), got


def test_flat_cache_creation_counts_as_5m() -> None:
    """Without ``cache_creation``, the flat field is the only signal → 5m."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        usage = {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 700,
            "cache_read_input_tokens": 50,
        }
        _write(root / "projects" / "p" / "a.jsonl", [_record("r1", FABLE, usage, epoch=clock)])

        got = _usage(_indexer(root, now=clock).scan_once(), "2026-09-05", FABLE)

        assert got.cache_write_5m == 700, got
        assert got.cache_write_1h == 0, got
        assert got == ModelUsage(10, 20, 700, 0, 50), got


# ---------------------------------------------------------------------------
# SPEC 3.3 trap 3 - duplicate requestId within one day
# ---------------------------------------------------------------------------


def test_duplicate_request_id_counted_once() -> None:
    """One request repeated - in the same file and in a second file - counts once.

    Per-file offsets cannot see a duplicate that a resumed or copied session
    wrote into a *different* transcript, which is why the dedup map is scoped to
    the day rather than to the file.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        usage = {"input_tokens": 1_000, "output_tokens": 2_000}
        record = _record("dup-1", FABLE, usage, epoch=clock)
        _write(root / "projects" / "p" / "a.jsonl", [record, record])
        _write(root / "projects" / "q" / "b.jsonl", [record])

        result = _indexer(root, now=clock).scan_once()

        got = _usage(result, "2026-09-05", FABLE)
        assert got == ModelUsage(1_000, 2_000, 0, 0, 0), got
        assert result.records_counted == 1, result.records_counted
        assert result.records_duplicate == 2, result.records_duplicate


def test_message_id_is_the_dedup_fallback() -> None:
    """With no ``requestId``, the message ``id`` keys the dedup map."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        usage = {"input_tokens": 7, "output_tokens": 9}
        record = _record(None, FABLE, usage, epoch=clock, message_id="msg_abc")
        _write(root / "projects" / "p" / "a.jsonl", [record, record])

        result = _indexer(root, now=clock).scan_once()

        assert _usage(result, "2026-09-05", FABLE) == ModelUsage(7, 9, 0, 0, 0)
        assert result.records_duplicate == 1, result.records_duplicate


# ---------------------------------------------------------------------------
# SPEC 3.3 trap 4 - day buckets are local dates
# ---------------------------------------------------------------------------


def test_day_bucketing_uses_local_time() -> None:
    """A record at 00:30 local buckets to the local day, not the UTC one.

    The fixture picks a local wall-clock time whose UTC date is *different*, so
    a UTC-bucketing bug lands the record on the wrong calendar day. On a machine
    running UTC there is no such time; the case then degenerates and is skipped
    rather than silently asserting nothing.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        for hour in (0, 23):
            local = dt.datetime(2026, 9, 5, hour, 30)
            if local.astimezone(dt.timezone.utc).date() != local.date():
                break
        else:
            print("      (skipped: host clock is UTC, no local/UTC day split exists)")
            return
        clock = local.timestamp()
        expected_local_day = day_key_from_date(local.date())
        utc_day = day_key_from_date(local.astimezone(dt.timezone.utc).date())
        _write(
            root / "projects" / "p" / "a.jsonl",
            [_record("r1", FABLE, {"input_tokens": 5}, epoch=clock)],
        )

        result = _indexer(root, now=clock).scan_once()

        days = _by_day(result)
        assert list(days) == [expected_local_day], (days, utc_day)
        assert days[expected_local_day][FABLE].input == 5


# ---------------------------------------------------------------------------
# SPEC 3.3 trap 5 - unknown model: count tokens, price at $0, show the name
# ---------------------------------------------------------------------------


def test_unknown_model_counts_tokens_costs_zero() -> None:
    """An unrecognised model keeps its literal name and never borrows a rate."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-05"
        _write(
            root / "projects" / "p" / "a.jsonl",
            [
                _record("r1", MYSTERY, {"input_tokens": 400_000, "output_tokens": 600_000}, epoch=clock),
                _record("r2", FABLE, {"input_tokens": 1_000_000}, epoch=clock),
            ],
        )
        result = _indexer(root, now=clock).scan_once()
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.merge(result.deltas)

        # Tokens are counted under the raw string, verbatim.
        assert _usage(result, day, MYSTERY).total_tokens == 1_000_000
        assert MYSTERY in result.unknown_models, result.unknown_models

        breakdown = store.cost_breakdown(DEFAULT_PRICING, today=day, progress=COMPLETE_INDEX)
        rows = {row.display_name: row for row in breakdown.by_model}
        assert MYSTERY in rows, list(rows)
        mystery = rows[MYSTERY]
        assert mystery.model == UNKNOWN_MODEL, mystery.model
        assert mystery.is_unknown is True
        assert mystery.total_tokens == 1_000_000
        assert mystery.usd == 0.0, mystery.usd
        assert mystery.raw_models == (MYSTERY,), mystery.raw_models
        assert breakdown.unknown_models == (MYSTERY,), breakdown.unknown_models
        # It never displaces a real model, and never borrows Fable's $10/Mtok.
        assert breakdown.by_model[-1].is_unknown is True
        assert breakdown.today.usd == 10.0, breakdown.today.usd


# ---------------------------------------------------------------------------
# SPEC 3.3 trap 6 - missing or partial usage: skip, never zero, never crash
# ---------------------------------------------------------------------------


def test_missing_and_partial_usage_are_skipped() -> None:
    """Six shapes of broken record, one good one. No crash, no phantom buckets."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-05"
        path = root / "projects" / "p" / "a.jsonl"
        _write(
            path,
            [
                _record("bad-1", FABLE, None, epoch=clock, omit_usage=True),  # no usage key
                _record("bad-2", FABLE, None, epoch=clock),                   # usage: null
                _record("bad-3", FABLE, {}, epoch=clock),                     # usage: {}
                _record("bad-4", FABLE, {"iterations": [{"input_tokens": 9}]}, epoch=clock),
                _record("bad-5", FABLE, "usage", epoch=clock),                # usage: a string
                {"type": "user", "message": {"content": 'the word "usage" in prose'}},
                _record("good", FABLE, {"input_tokens": 42}, epoch=clock),
            ],
        )
        # Junk that is not JSON at all must not stop the pass either.
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"message": {"usage": {"input_tokens": 1\n')

        result = _indexer(root, now=clock).scan_once()

        assert result.errors == (), result.errors
        assert _usage(result, day, FABLE) == ModelUsage(42, 0, 0, 0, 0)
        assert result.records_counted == 1, result.records_counted
        # bad-3, bad-4 (usage present, none of the five fields) and the
        # unparseable line are malformed; bad-1/bad-2/bad-5 and the prose line
        # never look like usage at all, so they are not counted as malformed.
        assert result.records_malformed == 3, result.records_malformed
        assert result.deltas[0].models.keys() == {FABLE}, result.deltas[0].models.keys()


# ---------------------------------------------------------------------------
# SPEC 3.2 step 4 - truncation / rotation guard
# ---------------------------------------------------------------------------


def test_truncated_file_is_reindexed() -> None:
    """A file that shrank below its stored offset is re-read from byte 0.

    Without the guard the second pass would seek past the end of the new
    content and count nothing - the file would be silently skipped forever.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-05"
        path = root / "projects" / "p" / "a.jsonl"
        _write(
            path,
            [
                _record(f"r{i}", FABLE, {"input_tokens": 1_000, "output_tokens": 100}, epoch=clock)
                for i in range(6)
            ],
        )
        indexer = _indexer(root, now=clock)

        first = indexer.scan_once()
        assert _usage(first, day, FABLE) == ModelUsage(6_000, 600, 0, 0, 0)
        big_size = path.stat().st_size

        # The session was rotated: same path, far shorter, one new request.
        _write(path, [_record("r-new", FABLE, {"input_tokens": 7}, epoch=clock)])
        assert path.stat().st_size < big_size

        second = indexer.scan_once()

        assert second.files_read == 1, second.files_read
        assert _usage(second, day, FABLE) == ModelUsage(7, 0, 0, 0, 0), _by_day(second)
        # Offsets are reset, not carried: the whole new file was read.
        assert second.bytes_read == path.stat().st_size, second.bytes_read


def test_replaced_file_is_reindexed() -> None:
    """A file swapped for a **larger** one is re-read from 0 (inode guard).

    This is the half of SPEC 3.2 step 4 that the shrink case cannot reach: with
    the replacement bigger than the original, ``offset`` is still below ``size``,
    so nothing but the inode comparison can tell that the stored offset now
    points into an unrelated file. Without it the scan would resume mid-way
    through the new transcript and undercount it.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-05"
        path = root / "projects" / "p" / "a.jsonl"
        _write(
            path,
            [
                _record(f"old{i}", FABLE, {"input_tokens": 1_000}, epoch=clock)
                for i in range(4)
            ],
        )
        indexer = _indexer(root, now=clock)
        first = indexer.scan_once()
        assert _usage(first, day, FABLE).input == 4_000, _by_day(first)
        old_inode = path.stat().st_ino
        old_size = path.stat().st_size

        # A rotated / restored session: same path, new inode, MORE bytes.
        replacement = path.with_suffix(".jsonl.new")
        _write(
            replacement,
            [
                _record(f"new{i}", FABLE, {"input_tokens": 1_000}, epoch=clock)
                for i in range(9)
            ],
        )
        os.replace(replacement, path)
        assert path.stat().st_ino != old_inode
        assert path.stat().st_size > old_size

        second = indexer.scan_once()

        assert second.files_read == 1, second.files_read
        assert _usage(second, day, FABLE).input == 9_000, _by_day(second)
        assert second.bytes_read == path.stat().st_size, second.bytes_read


# ---------------------------------------------------------------------------
# SPEC 3.2 step 5 - a trailing partial line is not consumed
# ---------------------------------------------------------------------------


def test_trailing_partial_line_is_not_consumed() -> None:
    """A half-written final line is discarded and re-read once it completes.

    This is the live-session case: Claude Code is mid-write when we scan. The
    offset must stop before the partial line, and the completed record must
    then be counted exactly once - not zero times, not twice.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-05"
        path = root / "projects" / "p" / "a.jsonl"
        complete = _record("r1", FABLE, {"input_tokens": 100, "output_tokens": 10}, epoch=clock)
        pending = json.dumps(_record("r2", FABLE, {"input_tokens": 500}, epoch=clock))
        head, tail = pending[:40], pending[40:]
        _write(path, [complete], trailing_partial=head)

        indexer = _indexer(root, now=clock)
        first = indexer.scan_once()

        assert _usage(first, day, FABLE) == ModelUsage(100, 10, 0, 0, 0), _by_day(first)
        state = json.loads((root / "scan_state.json").read_text())[str(path)]
        assert state["offset"] == len(json.dumps(complete)) + 1, state
        assert state["offset"] < path.stat().st_size, state

        with path.open("a", encoding="utf-8") as handle:
            handle.write(tail + "\n")
        second = indexer.scan_once()

        assert _usage(second, day, FABLE) == ModelUsage(500, 0, 0, 0, 0), _by_day(second)
        after = json.loads((root / "scan_state.json").read_text())[str(path)]
        assert after["offset"] == path.stat().st_size, after
        # And a third pass, with nothing appended, counts nothing again.
        third = indexer.scan_once()
        assert third.deltas == (), _by_day(third)


# ---------------------------------------------------------------------------
# SPEC 3.4 - Sonnet 5's introductory rate expires 2026-08-31
# ---------------------------------------------------------------------------


def test_sonnet5_rollover_prices_by_record_date() -> None:
    """1M in + 1M out of Sonnet 5: $12 through 2026-08-31, $18 from 09-01.

    Both figures are hand-computed from SPEC 3.4: intro 1e6x$2 + 1e6x$10 = $12,
    standard 1e6x$3 + 1e6x$15 = $18. The boundary days are asserted explicitly
    because inclusive-vs-exclusive is the bug this table invites.
    """
    usage = ModelUsage(input=1_000_000, output=1_000_000)
    price = DEFAULT_PRICING

    assert price.cost_usd(SONNET, usage, dt.date(2026, 8, 30)) == 12.0
    assert price.cost_usd(SONNET, usage, dt.date(2026, 8, 31)) == 12.0
    assert price.cost_usd(SONNET, usage, dt.date(2026, 9, 1)) == 18.0
    assert price.cost_usd(SONNET, usage, dt.date(2026, 9, 5)) == 18.0
    assert price.cost_usd(SONNET, usage, dt.date(2027, 3, 1)) == 18.0

    row = price.price_for(SONNET, dt.date(2026, 9, 5))
    assert row is not None and row.input_usd_per_mtok == 3.0, row
    assert row.output_usd_per_mtok == 15.0, row
    # Cache rates are derived, not stored (SPEC 3.4): 1.25x / 2x / 0.1x.
    assert row.cache_write_5m_usd_per_mtok == 3.75, row
    assert row.cache_write_1h_usd_per_mtok == 6.0, row
    assert row.cache_read_usd_per_mtok == 0.30000000000000004 or round(
        row.cache_read_usd_per_mtok, 10
    ) == 0.3, row
    intro = price.price_for(SONNET, dt.date(2026, 8, 31))
    assert intro is not None and intro.input_usd_per_mtok == 2.0, intro


def test_sonnet5_rollover_through_the_store() -> None:
    """The same usage on either side of the rollover, priced by *its own* day.

    A store holding 2026-08-31 and 2026-09-05 must report $12 for the older day
    and $18 for the newer one in the same breakdown - which is only possible if
    the price is resolved per day rather than from "today".
    """
    with tempfile.TemporaryDirectory() as name:
        store = DailyRollupStore(path=Path(name) / "rollups.json", keep_days=30)
        usage = ModelUsage(input=1_000_000, output=1_000_000)
        store.merge(
            [
                DayRollup(day="2026-08-31", models={SONNET: usage}),
                DayRollup(day="2026-09-05", models={SONNET: usage}),
            ]
        )

        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today="2026-09-05", progress=COMPLETE_INDEX
        )

        assert breakdown.today.usd == 18.0, breakdown.today
        assert breakdown.last_7d.usd == 30.0, breakdown.last_7d  # 18 + 12
        assert breakdown.last_30d.usd == 30.0, breakdown.last_30d
        # And "today" moving on does not rewrite history.
        older = store.cost_breakdown(
            DEFAULT_PRICING, today="2026-08-31", progress=COMPLETE_INDEX
        )
        assert older.today.usd == 12.0, older.today


# ---------------------------------------------------------------------------
# SPEC 6.7 - a hand-computed end-to-end figure, asserted exactly
# ---------------------------------------------------------------------------


def test_end_to_end_hand_computed_total() -> None:
    """Transcripts on disk → indexer → store → cost, against arithmetic by hand.

    Fixture: two transcript files, day **2026-09-05** (so Sonnet 5 is on its
    standard rate) plus one Sonnet record on **2026-08-31** (intro rate). The
    Fable record also carries ``iterations`` and a flat
    ``cache_creation_input_tokens``, so traps 1 and 2 are inside the money path,
    and its exact duplicate lives in the second file, so trap 3 is too.

    Rates (SPEC 3.4, per Mtok; cache = 1.25x / 2.0x / 0.1x of input):

    ============  ======  =======  ======  ======  ======
    model         input   output   w-5m    w-1h    read
    ============  ======  =======  ======  ======  ======
    Fable 5       10.00    50.00   12.50   20.00    1.00
    Sonnet 5 std   3.00    15.00    3.75    6.00    0.30
    Sonnet 5 intro 2.00    10.00    2.50    4.00    0.20
    Haiku 4.5      1.00     5.00    1.25    2.00    0.10
    quantum-9      0.00     0.00    0.00    0.00    0.00  (unknown → $0)
    ============  ======  =======  ======  ======  ======

    **2026-09-05 (today)**

    Fable 5    input   1,000,000 x 10.00/1e6 = $10.00
               output    200,000 x 50.00/1e6 = $10.00
               w-5m      400,000 x 12.50/1e6 =  $5.00
               w-1h      100,000 x 20.00/1e6 =  $2.00
               read    2,000,000 x  1.00/1e6 =  $2.00
                                        Fable = $29.00   (3,700,000 tok)
    Sonnet 5   input     100,000 x  3.00/1e6 =  $0.30
               output     20,000 x 15.00/1e6 =  $0.30
               read    1,000,000 x  0.30/1e6 =  $0.30
                                       Sonnet =  $0.90   (1,120,000 tok)
    Haiku 4.5  input       2,000 x  1.00/1e6 =  $0.002
               output        400 x  5.00/1e6 =  $0.002
                                        Haiku =  $0.004 → renders $0.00 (2,400 tok)
    quantum-9  1,000,000 tok, unrecognised   =  $0.00   (1,000,000 tok)

               Today  = 29.00 + 0.90 + 0.00 + 0.00      = $29.90
               tokens = 3,700,000 + 1,120,000 + 2,400 + 1,000,000 = 5,822,400

    **2026-08-31 (intro rate, 5 days earlier - inside both windows)**

    Sonnet 5   input   1,000,000 x  2.00/1e6 =  $2.00
               output    200,000 x 10.00/1e6 =  $2.00
                                        day  =  $4.00   (1,200,000 tok)

               Last 7d  = 29.904 + 4.00 = 33.904 → $33.90   (7,022,400 tok)
               Last 30d = same window content       $33.90
               7d avg   = 33.90 / 7                = $4.842857… → $4.84
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        older = _epoch(2026, 8, 31)
        today = "2026-09-05"

        fable = _record(
            "e2e-fable",
            FABLE,
            {
                "input_tokens": 1_000_000,
                "output_tokens": 200_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 400_000,
                    "ephemeral_1h_input_tokens": 100_000,
                },
                "cache_creation_input_tokens": 500_000,  # trap 2: the sum; ignored
                "cache_read_input_tokens": 2_000_000,
                "iterations": [  # trap 1: same request, must not be added
                    {"input_tokens": 1_000_000, "output_tokens": 200_000},
                ],
            },
            epoch=clock,
        )
        _write(
            root / "projects" / "alpha" / "session-1.jsonl",
            [
                fable,
                _record(
                    "e2e-sonnet",
                    SONNET,
                    {
                        "input_tokens": 100_000,
                        "output_tokens": 20_000,
                        "cache_read_input_tokens": 1_000_000,
                    },
                    epoch=clock,
                ),
                _record("e2e-haiku", HAIKU, {"input_tokens": 2_000, "output_tokens": 400}, epoch=clock),
                _record("e2e-broken", FABLE, {}, epoch=clock),  # trap 6: skipped
            ],
        )
        _write(
            root / "projects" / "beta" / "session-2.jsonl",
            [
                fable,  # trap 3: the same request again, in another file
                _record(
                    "e2e-mystery",
                    MYSTERY,
                    {"input_tokens": 500_000, "output_tokens": 500_000},
                    epoch=clock,
                ),
                _record(
                    "e2e-sonnet-intro",
                    SONNET,
                    {"input_tokens": 1_000_000, "output_tokens": 200_000},
                    epoch=older,
                ),
            ],
        )

        indexer = _indexer(root, now=clock)
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.load()
        result = indexer.scan_once()
        store.merge(result.deltas)
        store.save()

        assert result.errors == (), result.errors
        assert result.files_read == 2, result.files_read
        # 5 distinct usage-bearing requests: fable, sonnet, haiku, mystery,
        # sonnet-intro. The repeated fable is a duplicate, not a sixth request,
        # and the empty-usage record is malformed.
        assert result.records_counted == 5, result.records_counted
        assert result.records_duplicate == 1, result.records_duplicate
        assert result.records_malformed == 1, result.records_malformed

        breakdown = store.cost_breakdown(DEFAULT_PRICING, today=today, progress=COMPLETE_INDEX)

        # --- the hand-computed figures ------------------------------------
        assert breakdown.today.usd == 29.90, breakdown.today
        assert breakdown.today.total_tokens == 5_822_400, breakdown.today
        assert breakdown.last_7d.usd == 33.90, breakdown.last_7d
        assert breakdown.last_7d.total_tokens == 7_022_400, breakdown.last_7d
        assert breakdown.last_30d.usd == 33.90, breakdown.last_30d
        assert round(breakdown.last_7d_avg_per_day, 2) == 4.84, breakdown.last_7d_avg_per_day
        assert format_usd(breakdown.today.usd) == "$29.90"

        # --- the per-model rows, in menu order (SPEC 4.2) -----------------
        rows = [(r.display_name, r.total_tokens, r.usd) for r in breakdown.by_model]
        assert rows == [
            ("Fable 5", 3_700_000, 29.00),
            ("Sonnet 5", 1_120_000, 0.90),
            ("Haiku 4.5", 2_400, 0.00),
            (MYSTERY, 1_000_000, 0.00),
        ], rows
        # SPEC 4.2's arithmetic must literally hold: the rows sum to Today.
        assert round(sum(r.usd for r in breakdown.by_model), 2) == breakdown.today.usd
        assert breakdown.unknown_models == (MYSTERY,), breakdown.unknown_models
        assert breakdown.is_partial is False

        # --- and it survives a reload of the persisted rollup file --------
        reloaded = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        reloaded.load()
        again = reloaded.cost_breakdown(DEFAULT_PRICING, today=today, progress=COMPLETE_INDEX)
        assert again.today.usd == 29.90, again.today
        assert again.last_7d.usd == 33.90, again.last_7d

        # --- a second scan of an unchanged corpus counts nothing ----------
        assert indexer.scan_once().deltas == ()


# ---------------------------------------------------------------------------
# Runner (pytest is not installed in claude-swap's venv)
# ---------------------------------------------------------------------------


def _tests() -> list[tuple[str, Any]]:
    items = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def main() -> int:
    tests = _tests()
    failures: list[str] = []
    for name, func in tests:
        try:
            func()
        except Exception:
            failures.append(name)
            print(f"FAIL  {name}")
            print(traceback.format_exc().rstrip())
        else:
            print(f"pass  {name}")
    total = len(tests)
    print(f"\n{total - len(failures)} passed, {len(failures)} failed, out of {total}")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# 2026-08-25 incident regressions: unpriced tokens must be a VISIBLE floor
# ---------------------------------------------------------------------------


def test_unpriced_tokens_are_a_visible_floor() -> None:
    """Unknown-model tokens surface in ``WindowCost.unpriced_tokens``.

    The incident: 18.9M codex tokens priced $0 behind a clean-looking
    "Today" figure — correct pricing policy (never borrow a rate), invisible
    magnitude. The windows must now carry the excluded token count so the
    renderer can mark the totals as floors.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-05"
        _write(
            root / "projects" / "p" / "a.jsonl",
            [
                _record("r1", MYSTERY, {"input_tokens": 400_000, "output_tokens": 600_000}, epoch=clock),
                _record("r2", FABLE, {"input_tokens": 1_000_000}, epoch=clock),
            ],
        )
        result = _indexer(root, now=clock).scan_once()
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.merge(result.deltas)

        breakdown = store.cost_breakdown(DEFAULT_PRICING, today=day, progress=COMPLETE_INDEX)
        # Exactly the unknown model's tokens, in every window containing them.
        assert breakdown.today.unpriced_tokens == 1_000_000, breakdown.today
        assert breakdown.last_7d.unpriced_tokens == 1_000_000
        assert breakdown.last_30d.unpriced_tokens == 1_000_000
        # The priced model's tokens never leak into the unpriced counter.
        assert breakdown.today.total_tokens == 2_000_000


def test_priced_only_day_has_zero_unpriced_tokens() -> None:
    """The floor marker must never fire when every model is priced."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-05"
        _write(
            root / "projects" / "p" / "a.jsonl",
            [_record("r1", FABLE, {"input_tokens": 1_000_000}, epoch=clock)],
        )
        result = _indexer(root, now=clock).scan_once()
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.merge(result.deltas)
        breakdown = store.cost_breakdown(DEFAULT_PRICING, today=day, progress=COMPLETE_INDEX)
        assert breakdown.today.unpriced_tokens == 0
        assert breakdown.unknown_models == ()


def test_gpt_6_astra_is_priced() -> None:
    """gpt-6-astra is the corpus's dominant model since 2026-09-03; it must not
    fall through to the $0 unknown path. $10/$1/$50 per Mtok (OpenAI pricing
    page, 2026-09-09): 1M uncached input, 0.5M cached reads, 0.1M output =
    10 + 0.50 + 5 = 15.50 (``ModelUsage.input`` is already uncached,
    SPEC-CODEX 2.3)."""
    usage = ModelUsage(input=1_000_000, output=100_000, cache_read=500_000)
    cost = DEFAULT_PRICING.cost_usd("codex:gpt-6-astra", usage, dt.date(2026, 9, 9))
    assert abs(cost - 15.50) < 1e-9, cost
    assert DEFAULT_PRICING.display_name("codex:gpt-6-astra") == "gpt-6-astra"


def test_gpt_5_6_sol_rate_cut_prices_by_record_date() -> None:
    """Sol's cut ($5/$30 -> $4/$20) resolves by the USAGE record's day at the
    OPENAI_SOL_RATE_CUT_DAY bound, never by today (SPEC 3.4): 1M uncached
    input + 0.1M output = 5+3 = 8.00 before, 4+2 = 6.00 from the cut day."""
    from cc_usage_widget.pricing import OPENAI_SOL_RATE_CUT_DAY
    assert OPENAI_SOL_RATE_CUT_DAY == "2026-09-03"
    usage = ModelUsage(input=1_000_000, output=100_000)
    before = DEFAULT_PRICING.cost_usd("codex:gpt-5.6-sol", usage, dt.date(2026, 9, 2))
    after = DEFAULT_PRICING.cost_usd("codex:gpt-5.6-sol", usage, dt.date(2026, 9, 3))
    assert abs(before - 8.00) < 1e-9, before
    assert abs(after - 6.00) < 1e-9, after


def test_gpt_5_5_is_priced() -> None:
    """gpt-5.5 must never fall through to the $0 unknown path again.

    1.46B live-window tokens priced $0 before the row landed (added
    2026-08-25 from three agreeing secondary sources; $5/$0.50/$30 per Mtok).
    """
    usage = ModelUsage(input=1_000_000, output=1_000_000, cache_read=1_000_000)
    cost = DEFAULT_PRICING.cost_usd("codex:gpt-5.5", usage, dt.date(2026, 8, 25))
    assert abs(cost - (5.00 + 30.00 + 0.50)) < 1e-9, cost


# ---------------------------------------------------------------------------
# 2026-09-25 (drift-2, drift-6): OpenAI's page, read that day and saved at
# ~/.claude/plans/usage-bar-2026-09-25/evidence/drift_openai_pricing.html,
# lists gpt-6-sol and gpt-6-luna and a separate "Cache writes" column. Before
# this pass gpt-6-luna was in use (history 2026-09-23: 184,985 tokens) at $0,
# and every OpenAI cache write billed at the input rate.
# ---------------------------------------------------------------------------

# model -> per-Mtok USD (input, cached input, cache writes, output), the
# page's standard-tier short-context columns, verbatim.
_OPENAI_PAGE_2026_09_25: dict[str, tuple[str, str, str, str]] = {
    "gpt-6-astra": ("10.00", "1.00", "12.50", "50.00"),
    "gpt-6-sol": ("2.00", "0.20", "2.50", "10.00"),
    "gpt-6-luna": ("0.10", "0.01", "0.125", "0.50"),
    "gpt-5.6-sol": ("4.00", "0.40", "5.00", "20.00"),
    "gpt-5.6-terra": ("2.00", "0.20", "2.50", "12.00"),
    "gpt-5.6-luna": ("0.20", "0.02", "0.25", "1.20"),
}


def _mtok_usd(model: str, day: dt.date, **usage: int) -> float:
    """USD for *usage* on *model* through the module-level resolve()."""
    from cc_usage_widget import pricing

    got = pricing.resolve(model, day)
    return got.rates.cost_picos(ModelUsage(**usage)) / 10**12


def test_gpt_6_luna_and_gpt_6_sol_are_priced_at_the_published_rates() -> None:
    """Both new models price at their own rows, never $0 and never at a
    gpt-5.6 sibling's rate: 'gpt-6-sol' is not 'gpt-5.6-sol'."""
    from cc_usage_widget import pricing

    day = dt.date(2026, 9, 23)
    luna = pricing.resolve("codex:gpt-6-luna", day)
    assert luna.is_priced is True, luna
    assert abs(_mtok_usd("codex:gpt-6-luna", day, input=_ONE_MTOK) - 0.10) < 1e-12
    assert pricing.canonical_key("gpt-6-sol") == "codex:gpt-6-sol"
    assert pricing.canonical_key("gpt-6-luna") == "codex:gpt-6-luna"
    # The older siblings still resolve to themselves.
    assert pricing.canonical_key("gpt-5.6-sol") == "codex:gpt-5.6-sol"
    assert pricing.canonical_key("gpt-5.6-luna") == "codex:gpt-5.6-luna"
    for model in ("gpt-6-sol", "gpt-6-luna"):
        key = "codex:" + model
        want_in, want_read, want_write, want_out = (
            float(v) for v in _OPENAI_PAGE_2026_09_25[model]
        )
        assert abs(_mtok_usd(key, day, input=_ONE_MTOK) - want_in) < 1e-12, model
        assert abs(_mtok_usd(key, day, cache_read=_ONE_MTOK) - want_read) < 1e-12, model
        assert abs(_mtok_usd(key, day, cache_write_5m=_ONE_MTOK) - want_write) < 1e-12, model
        assert abs(_mtok_usd(key, day, output=_ONE_MTOK) - want_out) < 1e-12, model
        assert DEFAULT_PRICING.display_name(key) == model


def test_openai_cache_writes_price_at_the_published_cache_write_column() -> None:
    """drift-6: a cache write is billed at the page's 'Cache writes' column
    (1.25x input on every model that prints one), not at the input rate. Rows
    whose page cell is '-' (gpt-5.5, gpt-5.4) or that have no such column
    (gpt-5.4-mini) keep the input rate - no rate is invented for them."""
    day = dt.date(2026, 9, 25)
    astra = _mtok_usd("codex:gpt-6-astra", day, cache_write_5m=_ONE_MTOK)
    assert abs(astra - 12.50) < 1e-12, astra
    # The float contract view carries the same rate (price_for() callers).
    price = DEFAULT_PRICING.price_for("codex:gpt-6-astra", day)
    assert price is not None and abs(price.cache_write_5m_usd_per_mtok - 12.50) < 1e-12, price
    for model, (_, _, write, _) in _OPENAI_PAGE_2026_09_25.items():
        got = _mtok_usd("codex:" + model, day, cache_write_5m=_ONE_MTOK)
        assert abs(got - float(write)) < 1e-12, (model, got, write)
    for model, input_rate in (("gpt-5.5", 5.00), ("gpt-5.4", 2.50), ("gpt-5.4-mini", 0.75)):
        got = _mtok_usd("codex:" + model, day, cache_write_5m=_ONE_MTOK)
        assert abs(got - input_rate) < 1e-12, (model, got)
    # Every OpenAI row states its cache-write rate on the row itself.
    for row in DEFAULT_PRICING.rows:
        if row.vendor == "codex":
            assert row.cache_write_usd_per_mtok is not None, row.model


# ---------------------------------------------------------------------------
# 2026-09-23: Opus 5.5 and Fable 5.1 are this setup's main models, and both
# priced $0 because the suffix rule (correctly) refused to fold them into
# Opus 5 / Fable 5. Each now has its own row. Their cache READ is NOT the
# standard 0.1x: a derived table would bill Opus 5.5 reads at $0.40 (2x) and
# Fable 5.1 reads at $1.00 (4x), which is why each rate is checked on its own.
# ---------------------------------------------------------------------------

_ONE_MTOK = 1_000_000
_NEW_ROW_DAY = dt.date(2026, 9, 23)

# model -> per-Mtok USD for (input, output, cache_write_5m, cache_write_1h,
# cache_read), from the published prices cited beside the rows in pricing.py.
_NEW_ROW_RATES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 8.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 12.50, 20.00, 0.25),
}
_USAGE_FIELDS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")


def _check_each_rate(model: str) -> None:
    """One Mtok in each counter, one counter at a time, so a wrong rate names
    its own field instead of hiding inside a sum; then all five together."""
    expected = _NEW_ROW_RATES[model]
    for field, rate in zip(_USAGE_FIELDS, expected):
        usage = ModelUsage(**{field: _ONE_MTOK})
        cost = DEFAULT_PRICING.cost_usd(model, usage, _NEW_ROW_DAY)
        assert abs(cost - rate) < 1e-12, (model, field, cost, rate)
        # Exact integer path too: $1/Mtok == 1_000_000 pico/token.
        picos = DEFAULT_PRICING.cost_picos(model, usage, _NEW_ROW_DAY)
        assert picos == round(rate * 1_000_000) * _ONE_MTOK, (model, field, picos)
    full = ModelUsage(**{f: _ONE_MTOK for f in _USAGE_FIELDS})
    total = DEFAULT_PRICING.cost_usd(model, full, _NEW_ROW_DAY)
    assert abs(total - sum(expected)) < 1e-9, (model, total)
    # The float contract view must carry the same read override, or anything
    # pricing through price_for() would silently re-derive 0.1x.
    price = DEFAULT_PRICING.price_for(model, _NEW_ROW_DAY)
    assert price is not None, model
    assert abs(price.cache_read_usd_per_mtok - expected[4]) < 1e-12, price
    assert abs(price.cache_write_5m_usd_per_mtok - expected[2]) < 1e-12, price
    assert abs(price.cache_write_1h_usd_per_mtok - expected[3]) < 1e-12, price
    assert abs(price.cost_usd(full) - sum(expected)) < 1e-9, price


def test_opus_5_5_prices_each_token_kind_at_its_published_rate() -> None:
    """$4 in, $20 out, $5 5m write, $8 1h write, $0.20 read (0.05x) = $37.20."""
    _check_each_rate("claude-opus-5-5")
    full = ModelUsage(**{f: _ONE_MTOK for f in _USAGE_FIELDS})
    assert abs(DEFAULT_PRICING.cost_usd("claude-opus-5-5", full, _NEW_ROW_DAY) - 37.20) < 1e-9


def test_fable_5_1_prices_each_token_kind_at_its_published_rate() -> None:
    """$10 in, $50 out, $12.50 5m write, $20 1h write, $0.25 read (0.025x) = $92.75."""
    _check_each_rate("claude-fable-5-1")
    full = ModelUsage(**{f: _ONE_MTOK for f in _USAGE_FIELDS})
    assert abs(DEFAULT_PRICING.cost_usd("claude-fable-5-1", full, _NEW_ROW_DAY) - 92.75) < 1e-9


def test_opus_5_5_and_fable_5_1_spellings_resolve_to_their_own_rows() -> None:
    """Every spelling Claude Code writes (plain, [1m], dated) lands on the
    point release's own row, with its own menu label."""
    cases = {
        "claude-opus-5-5": "claude-opus-5-5",
        "claude-opus-5-5[1m]": "claude-opus-5-5",
        "claude-opus-5-5-20260922": "claude-opus-5-5",
        "claude-fable-5-1": "claude-fable-5-1",
        "claude-fable-5-1[1m]": "claude-fable-5-1",
    }
    for raw, want in cases.items():
        got = DEFAULT_PRICING.canonical_model(raw)
        assert got == want, (raw, got)
        assert DEFAULT_PRICING.canonical_key(raw) == want, raw
        assert DEFAULT_PRICING.is_known(raw), raw
    assert DEFAULT_PRICING.display_name("claude-opus-5-5[1m]") == "Opus 5.5"
    assert DEFAULT_PRICING.display_name("claude-fable-5-1") == "Fable 5.1"


def test_a_point_release_never_borrows_its_predecessors_rate() -> None:
    """Opus 5.5 is not Opus 5 and Fable 5.1 is not Fable 5 - and a point
    release with no row of its own stays unknown ($0, raw name surfaced)
    rather than borrowing the nearest neighbour's rate (SPEC 3.3 trap 5)."""
    full = ModelUsage(**{f: _ONE_MTOK for f in _USAGE_FIELDS})
    # Cache reads dominate this setup's token mix, and reads are where the two
    # pairs differ most (Fable 5.1 matches Fable 5 on every other rate): 10
    # Mtok of reads is $2.00 vs $5.00 for Opus, $2.50 vs $10.00 for Fable.
    reads = ModelUsage(cache_read=10 * _ONE_MTOK)
    for new, old, new_usd, old_usd in (
        ("claude-opus-5-5", "claude-opus-5", 2.00, 5.00),
        ("claude-fable-5-1", "claude-fable-5", 2.50, 10.00),
    ):
        assert DEFAULT_PRICING.canonical_model(new) == new
        assert DEFAULT_PRICING.canonical_model(new) != old
        got_new = DEFAULT_PRICING.cost_usd(new, reads, _NEW_ROW_DAY)
        got_old = DEFAULT_PRICING.cost_usd(old, reads, _NEW_ROW_DAY)
        assert abs(got_new - new_usd) < 1e-9, (new, got_new)
        assert abs(got_old - old_usd) < 1e-9, (old, got_old)
    # The predecessors still resolve to themselves, dated and bracketed.
    assert DEFAULT_PRICING.canonical_model("claude-opus-5[1m]") == "claude-opus-5"
    assert DEFAULT_PRICING.canonical_model("claude-fable-5-20260514") == "claude-fable-5"
    for future in (
        "claude-opus-5-7",
        "claude-opus-5-7[1m]",
        "claude-opus-5-5-1",
        "claude-fable-5-2",
        "claude-fable-5-1-1",
    ):
        assert DEFAULT_PRICING.canonical_model(future) == UNKNOWN_MODEL, future
        assert DEFAULT_PRICING.cost_usd(future, full, _NEW_ROW_DAY) == 0.0, future
        assert DEFAULT_PRICING.display_name(future) == future, future


# Integer pico-per-token rates of every row that existed before the Opus 5.5 /
# Fable 5.1 rows, captured from the pre-change table (HEAD ee05da2):
# (vendor, model, from, until) -> (input, output, write_5m, write_1h, read).
_PRE_EXISTING_RATES: dict[tuple[str, str, str | None, str | None], tuple[int, int, int, int, int]] = {
    ("claude", "claude-fable-5", None, None): (10_000_000, 50_000_000, 12_500_000, 20_000_000, 1_000_000),
    ("claude", "claude-mythos-5", None, None): (10_000_000, 50_000_000, 12_500_000, 20_000_000, 1_000_000),
    ("claude", "claude-opus-5", None, None): (5_000_000, 25_000_000, 6_250_000, 10_000_000, 500_000),
    ("claude", "claude-opus-4-8", None, None): (5_000_000, 25_000_000, 6_250_000, 10_000_000, 500_000),
    ("claude", "claude-sonnet-5", None, "2026-08-31"): (2_000_000, 10_000_000, 2_500_000, 4_000_000, 200_000),
    ("claude", "claude-sonnet-5", "2026-09-01", None): (3_000_000, 15_000_000, 3_750_000, 6_000_000, 300_000),
    ("claude", "claude-sonnet-4-6", None, None): (3_000_000, 15_000_000, 3_750_000, 6_000_000, 300_000),
    ("claude", "claude-haiku-4-5", None, None): (1_000_000, 5_000_000, 1_250_000, 2_000_000, 100_000),
    # 2026-09-25 (drift-6): these four OpenAI rows' cache-write slots moved on
    # purpose from the input rate to the page's "Cache writes" column; every
    # other slot of theirs is unchanged. Nothing else in this table moved.
    ("codex", "gpt-6-astra", None, None): (10_000_000, 50_000_000, 12_500_000, 12_500_000, 1_000_000),
    ("codex", "gpt-5.6-sol", None, "2026-09-02"): (5_000_000, 30_000_000, 5_000_000, 5_000_000, 500_000),
    ("codex", "gpt-5.6-sol", "2026-09-03", None): (4_000_000, 20_000_000, 5_000_000, 5_000_000, 400_000),
    ("codex", "gpt-5.6-terra", None, None): (2_000_000, 12_000_000, 2_500_000, 2_500_000, 200_000),
    ("codex", "gpt-5.6-luna", None, None): (200_000, 1_200_000, 250_000, 250_000, 20_000),
    ("codex", "gpt-5.5", None, None): (5_000_000, 30_000_000, 5_000_000, 5_000_000, 500_000),
    ("codex", "gpt-5.4", None, None): (2_500_000, 15_000_000, 2_500_000, 2_500_000, 250_000),
    ("codex", "gpt-5.4-mini", None, None): (750_000, 4_500_000, 750_000, 750_000, 75_000),
}


def test_the_cache_read_override_moves_no_pre_existing_rate() -> None:
    """Adding a per-row cache-read override must leave every older row exactly
    as it was: same integer rates, and (for Claude) still no override, so its
    float view still derives 0.1x / 1.25x / 2x."""
    rows = {
        (
            r.vendor,
            r.model,
            r.effective_from.isoformat() if r.effective_from else None,
            r.effective_until.isoformat() if r.effective_until else None,
        ): r
        for r in DEFAULT_PRICING.rows
    }
    for key, want in _PRE_EXISTING_RATES.items():
        assert key in rows, key
        rates = rows[key].rates
        got = (rates.input, rates.output, rates.cache_write_5m, rates.cache_write_1h, rates.cache_read)
        assert got == want, (key, got, want)
        if key[0] == "claude":
            assert rows[key].cached_input_usd_per_mtok is None, key
            assert rows[key].cache_write_usd_per_mtok is None, key
    # The only rows added are the two point releases and (2026-09-25) the two
    # GPT-6 models OpenAI's page lists beside Astra.
    added = sorted(k[1] for k in rows if k not in _PRE_EXISTING_RATES)
    assert added == ["claude-fable-5-1", "claude-opus-5-5", "gpt-6-luna", "gpt-6-sol"], added


# ---------------------------------------------------------------------------
# Roadmap item 1: the per-file contribution ledger makes `merge` reversible
#
# The incident these encode: `DailyRollupStore.merge` was a plain addition and
# the scan state remembered only HOW FAR a file had been read. Any re-read from
# byte 0 - a new inode, a truncation, a lost entry - added that file's whole
# history a second time, and live day/model cells were inflated up to 1,650x.
#
# Every case below re-scans with a FRESH `Indexer` over the same state file,
# which is what a widget restart does. It also matters for the test itself: the
# same instance holds today's requestIds in its dedup map and would suppress a
# double count that a restarted process cannot see. The records are dated
# YESTERDAY for the same reason - today's dedup map is persistent, every other
# day's is file-scoped and dies with the file handle.
# ---------------------------------------------------------------------------


def _apply(store: DailyRollupStore, result: Any) -> None:
    """Apply one scan the way the cost job must: retract, THEN merge.

    The order is not cosmetic. Merging first and retracting after would take
    the freshly re-added contribution straight back out again.
    """
    store.retract_rollups(result.retractions)
    store.merge(result.deltas)


def _replace_file(path: Path, records: list[Any]) -> None:
    """Swap *path* for a brand-new file holding *records* (a NEW inode).

    `os.replace` is what a copy-then-move, a restore, or a sync agent does, and
    it is the exact shape of the "re-read from zero" that inflated the store.
    """
    spare = path.with_suffix(path.suffix + ".new")
    _write(spare, records)
    os.replace(spare, path)


def test_replacing_a_transcript_with_a_longer_copy_adds_only_the_new_records() -> None:
    """A new inode holding the old records plus more must move the day by the
    APPENDED records only - not by the whole file a second time."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-04"
        stamp = _epoch(2026, 9, 4, 10)
        first = [
            _record(f"r{i}", FABLE, {"input_tokens": 1_000}, epoch=stamp)
            for i in range(3)
        ]
        target = root / "projects" / "p" / "a.jsonl"
        _write(target, first)
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        _apply(store, _indexer(root, now=clock).scan_once())
        assert store.today(day).total.input == 3_000, store.today(day)

        _replace_file(
            target,
            first + [_record("r3", FABLE, {"input_tokens": 1_000}, epoch=stamp)],
        )
        result = _indexer(root, now=clock).scan_once()
        assert result.retractions, "a re-read from zero must retract what it replaces"
        _apply(store, result)
        assert store.today(day).total.input == 4_000, store.today(day)


def test_a_shrunken_transcript_gives_its_old_contribution_back() -> None:
    """Truncating a file to fewer records leaves the day holding ONLY what the
    file still says - the retraction covers the records that went away."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-04"
        stamp = _epoch(2026, 9, 4, 10)
        target = root / "projects" / "p" / "a.jsonl"
        _write(
            target,
            [
                _record(f"r{i}", FABLE, {"input_tokens": 1_000}, epoch=stamp)
                for i in range(4)
            ],
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        _apply(store, _indexer(root, now=clock).scan_once())
        assert store.today(day).total.input == 4_000

        # Same inode, smaller than the stored offset: SPEC 3.2 step 4's
        # truncation guard, which re-reads from 0.
        _write(target, [_record("r0", FABLE, {"input_tokens": 1_000}, epoch=stamp)])
        result = _indexer(root, now=clock).scan_once()
        _apply(store, result)
        assert store.today(day).total.input == 1_000, store.today(day)


def test_a_vanished_transcript_keeps_its_day_and_cannot_double_on_return() -> None:
    """A deleted transcript keeps its money, and a returning one does not double it.

    Both halves matter and they pull in opposite directions. Claude Code prunes
    ``~/.claude/projects`` on its own ``cleanupPeriodDays``, so an aged-out
    transcript's tokens exist ONLY in ``rollups.json`` - retracting them on the
    vanish would be permanent, unrecoverable loss (the same incident
    ``test_lost_codex_scan_state_keeps_claude_history`` guards). But a path that
    comes back - a restore, a sync agent, a re-created session id - is read from
    byte 0 and would be added on top of a contribution nothing had taken out.

    The tombstone entry answers both: it holds the ledger, so the day survives,
    and it can never match a real file's ``(inode, size, mtime)``, so the return
    reads as a reset and replaces the contribution.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-04"
        stamp = _epoch(2026, 9, 4, 10)
        keep = root / "projects" / "p" / "keep.jsonl"
        doomed = root / "projects" / "p" / "doomed.jsonl"
        gone_records = [_record("d0", FABLE, {"input_tokens": 5_000}, epoch=stamp)]
        _write(keep, [_record("k0", FABLE, {"input_tokens": 1_000}, epoch=stamp)])
        _write(doomed, gone_records)
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        _apply(store, _indexer(root, now=clock).scan_once())
        assert store.today(day).total.input == 6_000

        doomed.unlink()
        result = _indexer(root, now=clock).scan_once()
        assert result.retractions == (), "a pruned transcript must not be retracted"
        _apply(store, result)
        assert store.today(day).total.input == 6_000, store.today(day)

        _write(doomed, gone_records)
        result = _indexer(root, now=clock).scan_once()
        assert result.retractions, "a returning path must retract before it re-adds"
        _apply(store, result)
        assert store.today(day).total.input == 6_000, store.today(day)


def test_a_tombstone_is_dropped_once_its_ledger_ages_out_of_the_window() -> None:
    """The tombstone is bounded: it exists to protect a day, and goes when the
    day does. Without this the scan state would grow by one entry per file the
    corpus has ever held."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        stamp = _epoch(2026, 9, 4, 10)
        keep = root / "projects" / "p" / "keep.jsonl"
        doomed = root / "projects" / "p" / "doomed.jsonl"
        _write(keep, [_record("k0", FABLE, {"input_tokens": 1_000}, epoch=stamp)])
        _write(doomed, [_record("d0", FABLE, {"input_tokens": 5_000}, epoch=stamp)])
        _indexer(root, now=_epoch(2026, 9, 5)).scan_once()
        doomed.unlink()
        _indexer(root, now=_epoch(2026, 9, 5)).scan_once()
        state = json.loads((root / "scan_state.json").read_text(encoding="utf-8"))
        assert str(doomed) in state, state
        assert state[str(doomed)]["inode"] == 0, state[str(doomed)]

        # Forty days on, the day it protected is outside every window.
        _indexer(root, now=_epoch(2026, 10, 15)).scan_once()
        state = json.loads((root / "scan_state.json").read_text(encoding="utf-8"))
        assert str(doomed) not in state, state


def test_widening_the_lookback_does_not_double_count() -> None:
    """Widening the window admits older FILES without re-adding the ones the
    narrow window already counted."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        recent_day = "2026-09-04"
        old_day = "2026-08-20"
        recent = root / "projects" / "p" / "recent.jsonl"
        old = root / "projects" / "p" / "old.jsonl"
        _write(
            recent,
            [
                _record(
                    "r0", FABLE, {"input_tokens": 1_000}, epoch=_epoch(2026, 9, 4, 10)
                )
            ],
        )
        _write(
            old,
            [
                _record(
                    "o0", FABLE, {"input_tokens": 7_000}, epoch=_epoch(2026, 8, 20, 10)
                )
            ],
        )
        # The old FILE is out of a 3-day window and therefore untracked, which
        # is what makes widening pick it up at all (`Indexer._collect`).
        os.utime(old, (_epoch(2026, 8, 20, 10), _epoch(2026, 8, 20, 10)))
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)

        _apply(store, _indexer(root, now=clock, lookback_days=3).scan_once())
        assert store.today(recent_day).total.input == 1_000
        assert store.today(old_day).total.input == 0

        _apply(store, _indexer(root, now=clock, lookback_days=30).scan_once())
        assert store.today(recent_day).total.input == 1_000, store.today(recent_day)
        assert store.today(old_day).total.input == 7_000, store.today(old_day)

        # And again, to prove the widened pass is itself idempotent.
        _apply(store, _indexer(root, now=clock, lookback_days=30).scan_once())
        assert store.today(recent_day).total.input == 1_000
        assert store.today(old_day).total.input == 7_000


def test_a_legacy_scan_state_entry_keeps_its_offset_and_starts_a_ledger() -> None:
    """A pre-ledger ``scan_state.json`` loads, keeps its offset, and begins
    recording from its next appended bytes.

    The documented cost: the ledger cannot retract what it never saw, so a
    legacy entry whose file is then replaced re-adds its history ONCE. That is
    strictly better than re-reading every 36 MB transcript at upgrade time, and
    it is self-healing - the entry is correct from the first append onwards.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5)
        day = "2026-09-04"
        stamp = _epoch(2026, 9, 4, 10)
        target = root / "projects" / "p" / "a.jsonl"
        _write(target, [_record("r0", FABLE, {"input_tokens": 1_000}, epoch=stamp)])
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        _apply(store, _indexer(root, now=clock).scan_once())

        # Rewrite the state file in the pre-ledger shape: the four SPEC 3.2
        # keys and nothing else.
        state_path = root / "scan_state.json"
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        legacy = {
            path: {k: entry[k] for k in ("inode", "size", "mtime", "offset")}
            for path, entry in raw.items()
        }
        assert legacy, "the scan wrote no state to downgrade"
        state_path.write_text(json.dumps(legacy), encoding="utf-8")

        with target.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _record("r1", FABLE, {"input_tokens": 500}, epoch=stamp)
                )
                + "\n"
            )
        result = _indexer(root, now=clock).scan_once()
        assert result.retractions == (), "a legacy entry has nothing to retract"
        _apply(store, result)
        # The offset survived: only the appended record was counted.
        assert store.today(day).total.input == 1_500, store.today(day)

        upgraded = json.loads(state_path.read_text(encoding="utf-8"))
        entry = next(iter(upgraded.values()))
        assert entry["ledger"] == {day: {FABLE: [500, 0, 0, 0, 0]}}, entry


def test_retract_clamps_at_zero_and_reports_the_shortfall() -> None:
    """A ledger can describe more than the store holds (a pruned or partially
    lost store). The clamp must hold and must be reported, not swallowed."""
    with tempfile.TemporaryDirectory() as name:
        store = DailyRollupStore(path=Path(name) / "rollups.json", keep_days=30)
        store.add("2026-09-04", FABLE, ModelUsage(input=100, output=10))
        removed = store.retract("2026-09-04", FABLE, ModelUsage(input=250, output=4))
        assert removed == ModelUsage(input=100, output=4), removed
        day = store.get("2026-09-04")
        assert day is not None and day.models[FABLE] == ModelUsage(output=6), day

        clamped = store.retract_rollups(
            [DayRollup(day="2026-09-04", models={FABLE: ModelUsage(output=99)})]
        )
        assert clamped == 1, clamped
        # A cell driven to zero is deleted, not kept as a row of zeros.
        assert store.get("2026-09-04") is None, store.get("2026-09-04")


def test_retracting_a_day_the_store_never_had_is_a_no_op() -> None:
    """Retraction must never invent a negative cell or a phantom day."""
    with tempfile.TemporaryDirectory() as name:
        store = DailyRollupStore(path=Path(name) / "rollups.json", keep_days=30)
        clamped = store.retract_rollups(
            [DayRollup(day="2026-09-04", models={FABLE: ModelUsage(input=5)})]
        )
        assert clamped == 1, clamped
        assert store.days() == (), store.days()


# ---------------------------------------------------------------------------
# Roadmap item 1, second half (2026-09-10): a retraction has to give the DEDUP
# map back what it gives the store back.
#
# The ledger tests above all date their records to *yesterday*, where dedup is
# file-scoped and dies with the file handle. On TODAY the map is persistent and
# process-wide: a file re-read from byte 0 had its contribution retracted and
# then found every one of its requests already credited, contributed nothing,
# and silently took the day to zero. These cases keep ONE indexer across both
# scans, which is what makes them today's-map tests rather than yesterday's.
# ---------------------------------------------------------------------------


def test_replacing_a_todays_transcript_keeps_the_records_it_still_holds() -> None:
    """A new inode holding today's records plus more must move today by the
    APPENDED records - not to zero, and not by the whole file again."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5, 18)
        day = "2026-09-05"
        stamp = _epoch(2026, 9, 5, 10)
        first = [
            _record(f"t{i}", FABLE, {"input_tokens": 1_000}, epoch=stamp)
            for i in range(3)
        ]
        target = root / "projects" / "p" / "a.jsonl"
        _write(target, first)
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        indexer = _indexer(root, now=clock)
        _apply(store, indexer.scan_once())
        assert store.today(day).total.input == 3_000, store.today(day)

        _replace_file(
            target,
            first + [_record("t3", FABLE, {"input_tokens": 1_000}, epoch=stamp)],
        )
        result = indexer.scan_once()
        assert result.retractions, "a re-read from zero must retract what it replaces"
        _apply(store, result)
        # Before the fix this was 1_000: the retraction took 3_000 back out and
        # the dedup map refused to let the re-read put any of it back.
        assert store.today(day).total.input == 4_000, store.today(day)


def test_shrinking_a_todays_transcript_leaves_what_it_still_holds() -> None:
    """The truncation half of the same fix, and the negative control beside it:
    the OTHER transcript's contribution to today must not move at all."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5, 18)
        day = "2026-09-05"
        stamp = _epoch(2026, 9, 5, 10)
        target = root / "projects" / "p" / "a.jsonl"
        keep = root / "projects" / "p" / "keep.jsonl"
        _write(
            target,
            [
                _record(f"t{i}", FABLE, {"input_tokens": 1_000}, epoch=stamp)
                for i in range(4)
            ],
        )
        _write(keep, [_record("k0", FABLE, {"input_tokens": 500}, epoch=stamp)])
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        indexer = _indexer(root, now=clock)
        _apply(store, indexer.scan_once())
        assert store.today(day).total.input == 4_500, store.today(day)

        # Same inode, smaller than the stored offset: SPEC 3.2 step 4.
        _write(target, [_record("t0", FABLE, {"input_tokens": 1_000}, epoch=stamp)])
        _apply(store, indexer.scan_once())
        # 1_000 of the shrunken file + the untouched 500 of the other one.
        assert store.today(day).total.input == 1_500, store.today(day)


def test_a_restart_does_not_uncredit_another_transcripts_requests() -> None:
    """Only the restarting file's own request ids may be handed back.

    Two transcripts holding the same requestId is the copied-session case
    cross-file dedup exists for: the second file contributed nothing for it, so
    its ledger has nothing to retract and dropping the id would let the request
    be counted a second time.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5, 18)
        day = "2026-09-05"
        stamp = _epoch(2026, 9, 5, 10)
        shared = _record("shared", FABLE, {"input_tokens": 1_000}, epoch=stamp)
        original = root / "projects" / "p" / "a.jsonl"
        copy = root / "projects" / "p" / "b.jsonl"
        _write(original, [shared])
        _write(copy, [shared])
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        indexer = _indexer(root, now=clock)
        _apply(store, indexer.scan_once())
        assert store.today(day).total.input == 1_000, "the copy was double counted"

        _replace_file(
            copy,
            [shared, _record("fresh", FABLE, {"input_tokens": 1_000}, epoch=stamp)],
        )
        _apply(store, indexer.scan_once())
        assert store.today(day).total.input == 2_000, store.today(day)


# ---------------------------------------------------------------------------
# Roadmap item 2, second half (2026-09-10): a vanished LEGACY entry must leave
# a tombstone the audit can see.
#
# A pre-ledger entry keeps its offset and has no ledger, so when its file is
# pruned there is nothing to carry - and the entry was simply deleted. The
# tokens it credited are still in rollups.json, invisible to `tombstone_rollups`
# and unreachable by any re-index, so the first self-audit read them as drift
# and repaired them away. The tombstone has to stand even when it is empty.
# ---------------------------------------------------------------------------


def _strip_ledger(state_path: Path) -> dict[str, Any]:
    """Rewrite a scan state as a pre-ledger build would have written it."""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for entry in state.values():
        entry.pop("ledger", None)
        entry.pop("lv", None)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state


def test_a_vanished_legacy_entry_leaves_a_legacy_tombstone() -> None:
    """It is tombstoned, flagged, reported - and bounded by the window."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5, 18)
        stamp = _epoch(2026, 9, 5, 10)
        keep = root / "projects" / "p" / "keep.jsonl"
        doomed = root / "projects" / "p" / "doomed.jsonl"
        _write(keep, [_record("k0", FABLE, {"input_tokens": 1_000}, epoch=stamp)])
        _write(doomed, [_record("d0", FABLE, {"input_tokens": 5_000}, epoch=stamp)])
        # Pin the write day: with the real mtime (today) the final scan below
        # kept the tombstone inside its 30-day lookback once the calendar
        # passed its fixed clock. 09-04, not the clock's 09-05, so the test
        # still proves the flag comes from the mtime, not the injected clock.
        written_at = _epoch(2026, 9, 4, 10)
        os.utime(doomed, (written_at, written_at))
        state_path = root / "scan_state.json"
        _indexer(root, now=clock).scan_once()
        _strip_ledger(state_path)
        # The day the entry is flagged with is the file's last WRITE day - the
        # honest upper bound on the days its records can belong to - which is
        # the real mtime, not the injected clock.
        written = local_day_key(doomed.stat().st_mtime)

        doomed.unlink()
        indexer = _indexer(root, now=clock)
        indexer.scan_once()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        entry = state.get(str(doomed))
        assert entry is not None, "a legacy entry was dropped, not tombstoned"
        assert entry["inode"] == 0, entry
        assert entry["legacy_day"] == written, entry
        assert "lv" not in entry, entry
        assert indexer.legacy_tombstone_days(since="2026-09-04") == (
            written,
        ), indexer.legacy_tombstone_days(since="2026-09-04")
        # It is bounded like every other tombstone: once the day it protects is
        # out of the window there is nothing left to protect.
        assert indexer.legacy_tombstone_days(since="2099-01-01") == ()

        _indexer(root, now=_epoch(2026, 10, 15)).scan_once()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert str(doomed) not in state, state


def test_an_entry_this_build_wrote_is_never_mistaken_for_a_legacy_one() -> None:
    """The flag has to be precise or the audit stops repairing anything.

    A transcript with no usage at all also has an empty ledger, and there are
    plenty of those. Only an entry written before the ledger existed - no
    ``lv`` marker - counts as legacy.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5, 18)
        stamp = _epoch(2026, 9, 5, 10)
        keep = root / "projects" / "p" / "keep.jsonl"
        empty = root / "projects" / "p" / "empty.jsonl"
        _write(keep, [_record("k0", FABLE, {"input_tokens": 1_000}, epoch=stamp)])
        _write(empty, [{"type": "user", "message": {"role": "user"}}])
        indexer = _indexer(root, now=clock)
        indexer.scan_once()
        entry = json.loads(
            (root / "scan_state.json").read_text(encoding="utf-8")
        )[str(empty)]
        assert "ledger" not in entry, entry
        assert entry["lv"] == 1, entry

        empty.unlink()
        indexer.scan_once()
        assert indexer.legacy_tombstone_days(since="2026-09-04") == ()
        state = json.loads((root / "scan_state.json").read_text(encoding="utf-8"))
        assert str(empty) not in state, "a contribution-free entry was tombstoned"


# ---------------------------------------------------------------------------
# The dedup sidecar and the owners map are ONE cache (2026-09-10)
#
# `owners` arrived after `requests` did. A sidecar written before it was read
# for the parts we recognised - requests, no owners - and that is worse than
# not reading it at all: `_forget_dedup_for_path` un-credits exactly the ids a
# path owns, so a file re-read from byte 0 whose ids have no owner keeps every
# one of them suppressed and its whole contribution to TODAY reads as zero.
# ---------------------------------------------------------------------------


def test_a_pre_owners_dedup_sidecar_is_discarded_whole() -> None:
    """An unversioned sidecar must not suppress a re-read it cannot undo."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5, 18)
        day = "2026-09-05"
        stamp = _epoch(2026, 9, 5, 10)  # TODAY on this clock: the persistent map
        target = root / "projects" / "p" / "a.jsonl"
        first = [
            _record(f"t{i}", FABLE, {"input_tokens": 1_000}, epoch=stamp)
            for i in range(2)
        ]
        _write(target, first)
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        indexer = _indexer(root, now=clock)
        _apply(store, indexer.scan_once())
        assert store.today(day).total.input == 2_000, store.today(day)
        indexer.flush_dedup()

        sidecar = root / "scan_state_dedup.json"
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        assert raw["requests"], "the fixture wrote no dedup sidecar"
        assert raw["owners"], "this build writes owners; the test is stale"
        # ...as a build from before the owners map would have written it.
        raw.pop("owners")
        raw.pop("v")
        sidecar.write_text(json.dumps(raw), encoding="utf-8")

        # A restart, and the transcript is replaced by a longer copy - a new
        # inode, so its whole contribution is retracted and re-read.
        restarted = _indexer(root, now=clock)
        _replace_file(
            target,
            first + [_record("t2", FABLE, {"input_tokens": 1_000}, epoch=stamp)],
        )
        _apply(store, restarted.scan_once())
        # Before the fix: 1_000. The retraction took 2_000 out and the adopted
        # requests map refused to let the re-read put any of it back, because
        # nothing said which path owned those ids.
        assert store.today(day).total.input == 3_000, store.today(day)


def test_a_versioned_dedup_sidecar_is_still_honoured() -> None:
    """The negative control: this build's own sidecar must keep working, or
    the fix above would be "discard everything" wearing a version number."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = _epoch(2026, 9, 5, 18)
        day = "2026-09-05"
        stamp = _epoch(2026, 9, 5, 10)
        target = root / "projects" / "p" / "a.jsonl"
        _write(target, [_record("t0", FABLE, {"input_tokens": 1_000}, epoch=stamp)])
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        indexer = _indexer(root, now=clock)
        _apply(store, indexer.scan_once())
        indexer.flush_dedup()

        # A restart that APPENDS a larger streaming snapshot of the same
        # request: only its growth may be credited, which is what the sidecar
        # is for.
        restarted = _indexer(root, now=clock)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _record("t0", FABLE, {"input_tokens": 1_500}, epoch=stamp)
                )
                + "\n"
            )
        _apply(store, restarted.scan_once())
        assert store.today(day).total.input == 1_500, store.today(day)


if __name__ == "__main__":
    raise SystemExit(main())
