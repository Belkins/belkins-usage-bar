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


if __name__ == "__main__":
    raise SystemExit(main())
