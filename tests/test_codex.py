"""Correctness tests for the Codex (OpenAI) vendor path (SPEC-CODEX 2, 3, 3a, 5).

Every case writes **real** ``rollout-*.jsonl`` files into a temporary directory
and runs the real :class:`~cc_usage_widget.codex_indexer.CodexIndexer` over
them - no mocks, no monkeypatched parsers - because the SPEC-CODEX traps live in
the interaction between the reader, the *stateful* extractor and the scan state,
and a mocked extractor would test nothing.

**``~/.codex`` is never touched.** Every indexer here is constructed with an
explicit ``sessions_dir`` and ``state_path`` inside a ``TemporaryDirectory``;
the production roots are only ever read as *values*, in
:func:`test_absent_codex_corpus_is_silent`, which points at a path that does not
exist. Nothing in this module writes outside its own temp dir.

Coverage map:

============================================  =============================================================
SPEC-CODEX 2.1  stateful model attribution    :func:`test_model_attributed_across_turn_context_boundary`
SPEC-CODEX 2.1  resume hazard (trap 1b)       :func:`test_resume_midfile_keeps_the_model_attribution`
SPEC-CODEX 2.2  last_ not total_token_usage   :func:`test_only_last_token_usage_is_summed`
SPEC-CODEX 2.2  total_* resets mid-file       :func:`test_total_token_usage_reset_does_not_corrupt`
SPEC-CODEX 2.3  cached input is a SUBSET      :func:`test_cached_input_is_a_subset_hand_computed_cost`
SPEC-CODEX 3    reasoning is a subset         :func:`test_reasoning_output_is_not_added_twice`
SPEC-CODEX 3    unpriced model                :func:`test_unknown_model_counts_tokens_costs_zero`
SPEC-CODEX 3a   rate_limits.primary, newest   :func:`test_rate_limits_primary_parsed_newest_wins`
SPEC-CODEX 5.5  absent corpus is normal       :func:`test_absent_codex_corpus_is_silent`
SPEC-CODEX 5.2  cost spans both vendors       :func:`test_cost_breakdown_splits_the_two_vendors`
SPEC-CODEX 3a   plan-only record blanks %     :func:`test_plan_only_rate_limits_does_not_blank_the_percentage`
SPEC-CODEX 2.1  marker collision hides model  :func:`test_model_record_carrying_the_usage_marker_is_still_read`
SPEC 3.3 trap 4 day memo vs the UTC offset    :func:`test_day_memo_does_not_collide_across_utc_offsets`
============================================  =============================================================

Run with pytest if it is available, or directly - the module is its own runner::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    $PY tests/test_codex.py
    $PY -m pytest tests -q
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget.codex_indexer import CodexIndexer  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    CODEX_WINDOW_MINUTES_WEEKLY,
    UNKNOWN_MODEL,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    DayRollup,
    IndexProgress,
    ModelUsage,
    TranscriptSource,
    local_day_key,
    unknown_model_key,
)
from cc_usage_widget.pricing import DEFAULT_PRICING  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture helpers - real records, in the shapes SPEC-CODEX 1 measured
# ---------------------------------------------------------------------------

SOL = "gpt-5.6-sol"
"""The corpus's dominant model (15,560 records in 30 days), $5.00 / $0.50 / $30.00."""

MINI = "gpt-5.4-mini"
"""A second priced model, so an attribution test can tell two rates apart."""


def _iso(when: dt.datetime) -> str:
    """UTC ISO-8601 with the trailing ``Z`` a rollout actually writes."""
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _now_local() -> dt.datetime:
    """Local noon today - safely inside the lookback window, and far enough
    from midnight that the record's local day cannot straddle a boundary."""
    today = dt.date.today()
    return dt.datetime(today.year, today.month, today.day, 12, 0, 0).astimezone()


def turn_context(model: str, *, at: dt.datetime | None = None) -> dict[str, Any]:
    """The record that sets the current model (SPEC-CODEX 2.1).

    Note the **top-level** ``type``: the model is not an ``event_msg`` payload
    type, and looking for it under ``payload.type`` finds nothing.
    """
    return {
        "timestamp": _iso(at or _now_local()),
        "type": "turn_context",
        "payload": {"model": model, "cwd": "/tmp", "approval_policy": "on-request"},
    }


def token_count(
    *,
    at: dt.datetime | None = None,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_output_tokens: int = 0,
    total: dict[str, int] | None = None,
    rate_limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One usage record, in the live ``event_msg`` / ``token_count`` shape.

    ``total`` fills ``info.total_token_usage``. It defaults to a value that is
    **deliberately wrong** as a per-turn figure - ten times the per-turn numbers
    - so any implementation that sums it instead of ``last_token_usage`` is off
    by an order of magnitude rather than by a rounding error (SPEC-CODEX 2.2).
    """
    last = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if total is None:
        total = {
            "input_tokens": input_tokens * 10,
            "cached_input_tokens": cached_input_tokens * 10,
            "cache_write_input_tokens": cache_write_input_tokens * 10,
            "output_tokens": output_tokens * 10,
        }
    payload: dict[str, Any] = {
        "type": "token_count",
        "info": {"last_token_usage": last, "total_token_usage": total},
    }
    payload["rate_limits"] = rate_limits
    return {"timestamp": _iso(at or _now_local()), "type": "event_msg", "payload": payload}


def rate_limits(
    used_percent: float,
    *,
    window_minutes: int = CODEX_WINDOW_MINUTES_WEEKLY,
    resets_at: float | None = None,
    plan_type: str = "pro",
) -> dict[str, Any]:
    """A ``payload.rate_limits`` block (SPEC-CODEX 3a)."""
    return {
        "primary": {
            "used_percent": used_percent,
            "window_minutes": window_minutes,
            "resets_at": resets_at if resets_at is not None else time.time() + 86_400,
        },
        "plan_type": plan_type,
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    }


def write_rollout(
    root: Path, records: list[dict[str, Any]], *, name: str | None = None, mtime: float | None = None
) -> Path:
    """Write one ``rollout-*.jsonl`` into the dated tree Codex actually uses.

    ``sessions/YYYY/MM/DD/rollout-<iso>-<uuid>.jsonl``. Both halves of the name
    matter: the scanner tests the ``rollout-`` prefix *and* the ``.jsonl``
    suffix, so a fixture named anything else is silently not indexed and the
    test would pass vacuously.
    """
    today = dt.date.today()
    directory = root / f"{today.year:04d}" / f"{today.month:02d}" / f"{today.day:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    if name is None:
        name = f"rollout-{today.isoformat()}T12-00-00-{uuid.uuid4()}.jsonl"
    path = directory / name
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def make_indexer(tmp: Path, root: Path, **kwargs: Any) -> CodexIndexer:
    """A CodexIndexer pointed entirely inside *tmp*. Never at ``~/.codex``."""
    return CodexIndexer(
        sessions_dir=root,
        state_path=tmp / "codex_scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
        **kwargs,
    )


def counted(result: Any) -> dict[str, ModelUsage]:
    """``{vendor_model_key: ModelUsage}`` summed over every day in a result."""
    out: dict[str, ModelUsage] = {}
    for delta in result.deltas:
        for key, usage in delta.models.items():
            out[key] = out.get(key, ModelUsage()) + usage
    return out


def usd(key: str, usage: ModelUsage, day: dt.date | None = None) -> float:
    """Notional USD for *usage* of *key*, priced on *day* (default today)."""
    return DEFAULT_PRICING.cost_usd(key, usage, day or dt.date.today())


# ---------------------------------------------------------------------------
# SPEC-CODEX 2.1 - TRAP 1: the model is not on the usage record
# ---------------------------------------------------------------------------


def test_model_attributed_across_turn_context_boundary() -> None:
    """Turns attribute to the ``turn_context`` that precedes them, and re-attribute
    when a later one changes the model.

    Fails on the naive implementation, which reads the model from the usage
    record (where there is none) and buckets everything at ``codex:unknown``.
    It also fails on a "first model in the file wins" shortcut, because the
    second block belongs to a *different* model at a different rate.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        write_rollout(
            root,
            [
                # Two turns before ANY turn_context. There is no model to be
                # had, and inventing one would be inventing a price - so these
                # stay unknown at $0 rather than borrowing the model below.
                token_count(input_tokens=1_000, output_tokens=100),
                token_count(input_tokens=2_000, output_tokens=200),
                turn_context(SOL),
                token_count(input_tokens=10_000, output_tokens=1_000),
                token_count(input_tokens=20_000, output_tokens=2_000),
                turn_context(MINI),  # <- the boundary
                token_count(input_tokens=100_000, output_tokens=5_000),
            ],
        )
        indexer = make_indexer(tmp, root)
        got = counted(indexer.scan_once())

        assert set(got) == {
            f"{VENDOR_CODEX}:{SOL}",
            f"{VENDOR_CODEX}:{MINI}",
            unknown_model_key(VENDOR_CODEX),
        }, got
        # Everything between the two turn_contexts belongs to SOL...
        assert got[f"{VENDOR_CODEX}:{SOL}"] == ModelUsage(input=30_000, output=3_000)
        # ...and everything after the boundary to MINI, at its own rate.
        assert got[f"{VENDOR_CODEX}:{MINI}"] == ModelUsage(input=100_000, output=5_000)
        # No back-filling: the pre-context turns keep their honest $0.
        assert got[unknown_model_key(VENDOR_CODEX)] == ModelUsage(input=3_000, output=300)
        assert usd(unknown_model_key(VENDOR_CODEX), got[unknown_model_key(VENDOR_CODEX)]) == 0.0

        # And the two priced blocks really are priced differently, which is what
        # makes the boundary observable rather than cosmetic.
        sol_usd = usd(f"{VENDOR_CODEX}:{SOL}", got[f"{VENDOR_CODEX}:{SOL}"])
        mini_usd = usd(f"{VENDOR_CODEX}:{MINI}", got[f"{VENDOR_CODEX}:{MINI}"])
        # SOL: 30,000 * $5/Mtok + 3,000 * $30/Mtok = 0.15 + 0.09 = $0.24
        assert round(sol_usd, 10) == 0.24, sol_usd
        # MINI: 100,000 * $0.75/Mtok + 5,000 * $4.50/Mtok = 0.075 + 0.0225 = $0.0975
        assert round(mini_usd, 10) == 0.0975, mini_usd


def test_resume_midfile_keeps_the_model_attribution() -> None:
    """TRAP 1b: a scan cut short mid-file resumes with the model still known.

    The ``turn_context`` sits *behind* the stored offset and is never re-read,
    so an implementation that keeps ``current_model`` only in a local variable
    sends every post-resume turn to ``codex:unknown`` at $0 - silently, and on
    the routine first-index path. This is the single most expensive Codex bug
    available, and it is invisible without this test.

    Each pass is driven through a **fresh indexer** reading the persisted
    ``codex_scan_state.json``, which is the real restart. An in-process carry
    would pass this test while the on-disk ``last_model`` was missing, so the
    in-memory variable is deliberately thrown away between passes.

    The file has to be longer than the scanner's 4,096-line deadline-check
    interval, or the whole thing is consumed before the first check and there is
    no mid-file cut to resume from - the test would pass vacuously.
    """
    turns = 9_000  # > one deadline-check interval, so the cut lands mid-file
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        records: list[dict[str, Any]] = [turn_context(SOL)]
        records += [
            token_count(input_tokens=1_000, output_tokens=100, total={
                "input_tokens": 1_000 * (i + 1), "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 100 * (i + 1)})
            for i in range(turns)
        ]
        path = write_rollout(root, records)
        size = path.stat().st_size

        # --- pass 1: an already-expired deadline cuts the read mid-file ------
        first = make_indexer(tmp, root, defer_state_commit=True)
        first_result = first.scan_once(deadline=time.monotonic() - 1.0)
        first.commit_state()

        state = json.loads((tmp / "codex_scan_state.json").read_text())
        entry = state[str(path)]
        assert 0 < entry["offset"] < size, (
            f"the read was not cut mid-file, so there is no resume to test: {entry}"
        )
        assert entry.get("last_model") == SOL, (
            "the model current at the cut must be persisted, or every turn after "
            f"the resume attributes to unknown at $0; got {entry!r}"
        )

        # --- passes 2..n: a brand-new indexer each time, resuming from disk ---
        totals = counted(first_result)
        rounds = 0
        while True:
            rounds += 1
            assert rounds < 20, "the resume never finished the file"
            resumed = make_indexer(tmp, root, defer_state_commit=True)
            result = resumed.scan_once(deadline=time.monotonic() + 0.05)
            resumed.commit_state()
            for key, usage in counted(result).items():
                totals[key] = totals.get(key, ModelUsage()) + usage
            if resumed.progress().complete and not result.deltas:
                break

        assert rounds > 1, "the file finished in one pass; no resume was exercised"

        # THE assertion: not one turn fell into the unknown bucket. Without the
        # persisted `last_model`, every turn after the first cut lands here.
        assert unknown_model_key(VENDOR_CODEX) not in totals, (
            f"post-resume turns fell into the unknown bucket: { {k: v for k, v in totals.items()} }"
        )
        assert set(totals) == {f"{VENDOR_CODEX}:{SOL}"}, totals

        # And the file was counted exactly once across every pass - a resume
        # that re-read the overlap would show up here as inflation.
        assert totals[f"{VENDOR_CODEX}:{SOL}"] == ModelUsage(
            input=turns * 1_000, output=turns * 100
        ), totals

        # An idle corpus yields nothing more.
        assert make_indexer(tmp, root).scan_once().deltas == ()


# ---------------------------------------------------------------------------
# SPEC-CODEX 2.2 - TRAP 2: total_token_usage is neither a delta nor a total
# ---------------------------------------------------------------------------


def test_only_last_token_usage_is_summed() -> None:
    """``last_token_usage`` is the per-turn figure; ``total_token_usage`` is not.

    The fixture's ``total_*`` is 10x the per-turn numbers and cumulative, so an
    implementation that sums ``total_*`` (or adds both) reports an order of
    magnitude too much. The exact expected value is hand-computed below.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(
                    input_tokens=1_000, output_tokens=100,
                    total={"input_tokens": 1_000, "cached_input_tokens": 0,
                           "cache_write_input_tokens": 0, "output_tokens": 100},
                ),
                token_count(
                    input_tokens=2_000, output_tokens=200,
                    total={"input_tokens": 3_000, "cached_input_tokens": 0,
                           "cache_write_input_tokens": 0, "output_tokens": 300},
                ),
                token_count(
                    input_tokens=4_000, output_tokens=400,
                    total={"input_tokens": 7_000, "cached_input_tokens": 0,
                           "cache_write_input_tokens": 0, "output_tokens": 700},
                ),
            ],
        )
        got = counted(make_indexer(tmp, root).scan_once())
        usage = got[f"{VENDOR_CODEX}:{SOL}"]
        # sum(last) = 1,000 + 2,000 + 4,000 = 7,000 in / 700 out.
        assert usage == ModelUsage(input=7_000, output=700), usage
        # The trap: the FINAL total_token_usage happens to equal the sum here,
        # so a naive implementation that reads the last total_* looks right...
        assert usage.input != 1_000 + 3_000 + 7_000, "summed total_token_usage"


def test_total_token_usage_reset_does_not_corrupt() -> None:
    """...and here it does not, because ``total_*`` resets mid-session.

    SPEC-CODEX 2.2 measured 252,100,617 summed per-turn against a final
    ``total_token_usage.input`` of 230,324,294 in one real session - the gap is
    a mid-session reset. This fixture reproduces the reset: an implementation
    reading the last ``total_*`` as the session total under-reports, and one
    diffing consecutive ``total_*`` values goes *negative* across the reset.

    Only summing ``last_token_usage`` survives both.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(input_tokens=100_000, output_tokens=1_000,
                            total={"input_tokens": 100_000, "cached_input_tokens": 0,
                                   "cache_write_input_tokens": 0, "output_tokens": 1_000}),
                token_count(input_tokens=100_000, output_tokens=1_000,
                            total={"input_tokens": 200_000, "cached_input_tokens": 0,
                                   "cache_write_input_tokens": 0, "output_tokens": 2_000}),
                # ---- the reset: cumulative counter drops back to a small value
                token_count(input_tokens=100_000, output_tokens=1_000,
                            total={"input_tokens": 5_000, "cached_input_tokens": 0,
                                   "cache_write_input_tokens": 0, "output_tokens": 50}),
                token_count(input_tokens=100_000, output_tokens=1_000,
                            total={"input_tokens": 105_000, "cached_input_tokens": 0,
                                   "cache_write_input_tokens": 0, "output_tokens": 1_050}),
            ],
        )
        usage = counted(make_indexer(tmp, root).scan_once())[f"{VENDOR_CODEX}:{SOL}"]
        assert usage == ModelUsage(input=400_000, output=4_000), usage
        # The two wrong answers this fixture is built to catch:
        assert usage.input != 105_000, "read the final total_token_usage as the total"
        assert usage.input != 205_000, "diffed consecutive total_token_usage values"


# ---------------------------------------------------------------------------
# SPEC-CODEX 2.3 - TRAP 3: cached_input_tokens is a SUBSET of input_tokens
# ---------------------------------------------------------------------------


def test_cached_input_is_a_subset_hand_computed_cost() -> None:
    """Uncached input is ``input - cached``, and the dollar figure is exact.

    OpenAI reports these **overlapping**; Anthropic reports them disjoint. An
    implementation that treats ``cached_input_tokens`` as additive charges the
    cached tokens twice - once at the full input rate and once at the cached
    rate - and every turn is overcharged.

    Hand computation, ``gpt-5.6-sol`` at $5.00 / $0.50 / $30.00 per Mtok, with
    cache writes billed at the standard input rate (SPEC-CODEX 3):

    ==============================  ==================  ==========
    uncached in  1,000,000 - 800,000    200,000 @ $5.00   $1.000000
    cached in                            800,000 @ $0.50   $0.400000
    cache write                          100,000 @ $5.00   $0.500000
    output                                50,000 @ $30.00  $1.500000
    ==============================  ==================  ==========
    **total**                                             **$3.40**
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(
                    input_tokens=1_000_000,
                    cached_input_tokens=800_000,
                    cache_write_input_tokens=100_000,
                    output_tokens=50_000,
                ),
            ],
        )
        usage = counted(make_indexer(tmp, root).scan_once())[f"{VENDOR_CODEX}:{SOL}"]

        # The subtraction itself: `input` holds UNCACHED input, `cache_read` the
        # cached subset. Their sum reconstitutes OpenAI's `input_tokens`.
        assert usage.input == 200_000, f"cached was not subtracted: {usage}"
        assert usage.cache_read == 800_000, usage
        assert usage.input + usage.cache_read == 1_000_000
        assert usage.cache_write_5m == 100_000
        assert usage.cache_write_1h == 0, "OpenAI publishes no 1-hour cache tier"
        assert usage.output == 50_000

        got = usd(f"{VENDOR_CODEX}:{SOL}", usage)
        assert round(got, 10) == 3.40, f"expected exactly $3.40, got ${got}"

        # What the additive (naive) reading would have produced, for the record:
        # 1,000,000 @ $5.00 + 800,000 @ $0.50 + 100,000 @ $5.00 + 50,000 @ $30.00
        # = 5.00 + 0.40 + 0.50 + 1.50 = $7.40 - more than twice the truth.
        assert round(got, 10) != 7.40


def test_cached_greater_than_input_is_clamped_not_negative() -> None:
    """A provider reporting ``cached > input`` must not subtract real money.

    Unclamped, the uncached figure goes negative and *reduces* the day's total -
    a silent credit against every other model in the same bucket.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(input_tokens=1_000, cached_input_tokens=9_000, output_tokens=10),
            ],
        )
        usage = counted(make_indexer(tmp, root).scan_once())[f"{VENDOR_CODEX}:{SOL}"]
        assert usage.input == 0, usage
        assert usd(f"{VENDOR_CODEX}:{SOL}", usage) >= 0.0


# ---------------------------------------------------------------------------
# SPEC-CODEX 3 - reasoning_output_tokens is a subset of output_tokens
# ---------------------------------------------------------------------------


def test_reasoning_output_is_not_added_twice() -> None:
    """``reasoning_output_tokens`` is already inside ``output_tokens``.

    Adding it again inflates output - the most expensive counter on every
    OpenAI model ($30.00/Mtok on ``gpt-5.6-sol``) - by however much the model
    reasoned. Here that would be 4,000 of 5,000 output tokens: an 80% overcharge
    on the output line.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(
                    input_tokens=1_000,
                    output_tokens=5_000,
                    reasoning_output_tokens=4_000,  # a SUBSET of the 5,000
                ),
            ],
        )
        usage = counted(make_indexer(tmp, root).scan_once())[f"{VENDOR_CODEX}:{SOL}"]
        assert usage.output == 5_000, f"reasoning was added on top: {usage}"
        assert usage.output != 9_000, "reasoning_output_tokens double-added"
        # 1,000 @ $5.00 + 5,000 @ $30.00 = 0.005 + 0.150 = $0.155
        assert round(usd(f"{VENDOR_CODEX}:{SOL}", usage), 10) == 0.155


# ---------------------------------------------------------------------------
# SPEC-CODEX 3 - an unpriced model: counted, $0, named
# ---------------------------------------------------------------------------


def test_unknown_model_counts_tokens_costs_zero() -> None:
    """``codex-auto-review`` has no published rate: tokens counted, $0, name shown.

    The three things that must all hold, and the three ways this goes wrong:
    dropping the record (tokens vanish), pricing it at a neighbouring model's
    rate (an invented price), or bucketing it with *Claude's* unknown models
    (one ``$0`` row that hides which vendor it came from).
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        write_rollout(
            root,
            [
                turn_context("codex-auto-review"),
                token_count(input_tokens=500_000, output_tokens=20_000),
                turn_context(SOL),
                token_count(input_tokens=1_000, output_tokens=100),
            ],
        )
        result = make_indexer(tmp, root).scan_once()
        got = counted(result)

        key = f"{VENDOR_CODEX}:codex-auto-review"
        assert key in got, f"the unpriced model's tokens were dropped: {got}"
        assert got[key] == ModelUsage(input=500_000, output=20_000)
        assert usd(key, got[key]) == 0.0, "an unpriced model was given a price"
        # Never a neighbour's rate: at gpt-5.6-sol's rates this would be $3.10.
        assert usd(key, got[key]) != usd(f"{VENDOR_CODEX}:{SOL}", got[key])

        # Surfaced by name, RAW - the user must see what OpenAI called it, not
        # our storage key.
        assert "codex-auto-review" in result.unknown_models, result.unknown_models
        assert "codex:codex-auto-review" not in result.unknown_models

        # And it does NOT share Claude's unknown bucket.
        assert unknown_model_key(VENDOR_CLAUDE) not in got
        assert result.vendor == VENDOR_CODEX

        # Through the store, the row is attributed to Codex and priced at $0
        # while the priced model beside it is not.
        store = DailyRollupStore(path=tmp / "rollups.json", keep_days=30)
        store.merge(result.deltas)
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING,
            today=local_day_key(time.time()),
            progress=IndexProgress(complete=True),
        )
        rows = {row.display_name: row for row in breakdown.by_model}
        assert "codex-auto-review" in rows, rows
        row = rows["codex-auto-review"]
        assert row.is_unknown and row.usd == 0.0
        assert row.vendor == VENDOR_CODEX, row
        assert row.total_tokens == 520_000
        assert breakdown.unknown_models == ("codex-auto-review",), breakdown.unknown_models


# ---------------------------------------------------------------------------
# SPEC-CODEX 3a - the subscription quota is the headline number
# ---------------------------------------------------------------------------


def test_rate_limits_primary_parsed_newest_wins() -> None:
    """``rate_limits.primary`` becomes a read-only pseudo-account; newest wins.

    Newness is the source rollout's **mtime** (rollouts are append-only, so a
    file's mtime tracks its last record), which is what makes files scanned in
    arbitrary order still resolve to the genuinely newest quota. An
    implementation that simply keeps the last one it happened to read reports a
    stale percentage roughly half the time.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        now = time.time()
        reset_epoch = now + 3 * 86_400

        # The NEWER file is written first and named so it sorts first, so a
        # "last read wins" implementation picks the stale one.
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(input_tokens=10, output_tokens=1,
                            rate_limits=rate_limits(41.0, resets_at=reset_epoch)),
            ],
            name="rollout-aaa-newest.jsonl",
            mtime=now,
        )
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(input_tokens=10, output_tokens=1,
                            rate_limits=rate_limits(7.0, resets_at=now + 600)),
            ],
            name="rollout-zzz-stale.jsonl",
            mtime=now - 6 * 3_600,
        )

        indexer = make_indexer(tmp, root)
        indexer.scan_once()
        rows = indexer.quota_rows()

        assert len(rows) == 1, rows
        row = rows[0]
        assert row.seven_day_pct == 41.0, f"the stale quota won: {row.seven_day_pct}"
        assert row.plan_type == "pro"
        assert row.vendor == VENDOR_CODEX
        # A quota row is informational: nothing may offer to switch to it.
        assert row.switchable is False and row.is_pseudo
        assert row.is_active is False
        # No 5-hour window exists for Codex; it must render as an em dash, not 0%.
        assert row.five_hour_pct is None
        # The epoch is formatted once on the way in, then treated as verbatim.
        assert isinstance(row.seven_day_resets_at, str) and row.seven_day_resets_at
        assert str(reset_epoch) not in row.seven_day_resets_at

        # A non-weekly primary is never coerced onto the weekly slot.
        other = tmp / "other"
        write_rollout(
            other / "sessions",
            [
                turn_context(SOL),
                token_count(input_tokens=10, output_tokens=1,
                            rate_limits=rate_limits(88.0, window_minutes=1_440)),
            ],
        )
        daily = CodexIndexer(
            sessions_dir=other / "sessions",
            state_path=other / "state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        daily.scan_once()
        daily_row = daily.quota_rows()[0]
        assert daily_row.seven_day_pct is None, (
            "a 24-hour window was rendered as a weekly percentage: "
            f"{daily_row.seven_day_pct}"
        )
        assert daily_row.scoped_windows and daily_row.scoped_windows[0][1] == 88.0


# ---------------------------------------------------------------------------
# SPEC-CODEX 5.5 - an absent corpus is a normal state, not an error
# ---------------------------------------------------------------------------


def test_absent_codex_corpus_is_silent() -> None:
    """No ``~/.codex`` means no error, no rows, no zeroed cost - and no crash.

    A Claude-only machine is the common case, and the failure mode this guards
    is not an exception: it is a Codex section that renders empty, or a ``$0``
    quota bar that implies a real reading of zero.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        missing = tmp / "definitely" / "not" / "here"
        assert not missing.exists()

        indexer = make_indexer(tmp, missing)
        assert indexer.available() is False
        assert indexer.quota_rows() == ()

        result = indexer.scan_once()
        assert result.deltas == ()
        assert result.errors == (), result.errors
        assert result.records_counted == 0
        assert result.vendor == VENDOR_CODEX

        # An absent corpus must report `complete`, or `IndexProgress.combined`
        # would strand a Claude-only user behind `indexing…` forever.
        assert indexer.progress().complete is True
        assert indexer.progress().vendor == VENDOR_CODEX
        assert IndexProgress.combined([indexer.progress()]).complete is True

        # A store fed nothing renders no Codex section at all.
        store = DailyRollupStore(path=tmp / "rollups.json", keep_days=30)
        store.merge(result.deltas)
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING,
            today=local_day_key(time.time()),
            progress=IndexProgress(complete=True),
        )
        assert breakdown.has_vendor(VENDOR_CODEX) is False
        assert breakdown.by_vendor == ()
        assert breakdown.today.usd == 0.0

        # Nothing was created under the (absent) corpus root.
        assert not missing.exists()

        # And it satisfies the protocol app.py drives every source through.
        assert isinstance(indexer, TranscriptSource)


# ---------------------------------------------------------------------------
# SPEC-CODEX 5.2 - the cost section spans both vendors
# ---------------------------------------------------------------------------


def test_cost_breakdown_splits_the_two_vendors() -> None:
    """One store, two vendors: totals span both, rows are attributed to each.

    Fails on a rollup that groups by the **bare** canonical model name: the two
    vendors' same-named models collapse onto one row, both unknown buckets
    merge, and every Codex row is reported as Claude's - which is exactly what
    ``CostBreakdown.by_vendor`` exists to prevent.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        today = local_day_key(time.time())
        store = DailyRollupStore(path=tmp / "rollups.json", keep_days=30)
        store.merge(
            [
                DayRollup(
                    day=today,
                    models={
                        # Bare key = claude, exactly as every pre-Codex
                        # rollups.json spells it.
                        "claude-fable-5": ModelUsage(input=1_000_000, output=100_000),
                        f"{VENDOR_CODEX}:{SOL}": ModelUsage(input=1_000_000, output=100_000),
                        f"{VENDOR_CODEX}:codex-auto-review": ModelUsage(input=500_000),
                        UNKNOWN_MODEL: ModelUsage(input=1_000),
                    },
                )
            ]
        )
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today=today, progress=IndexProgress(complete=True)
        )

        assert breakdown.vendors == (VENDOR_CLAUDE, VENDOR_CODEX), breakdown.vendors
        by_vendor = {group.vendor: group for group in breakdown.by_vendor}
        assert set(by_vendor) == {VENDOR_CLAUDE, VENDOR_CODEX}

        # Claude: 1,000,000 @ $10.00 + 100,000 @ $50.00 = 10.00 + 5.00 = $15.00
        assert by_vendor[VENDOR_CLAUDE].usd == 15.00, by_vendor[VENDOR_CLAUDE].usd
        # Codex:  1,000,000 @ $5.00  + 100,000 @ $30.00 =  5.00 + 3.00 =  $8.00
        assert by_vendor[VENDOR_CODEX].usd == 8.00, by_vendor[VENDOR_CODEX].usd
        # The header spans both, and the split behind it agrees.
        assert breakdown.today.usd == 23.00
        assert breakdown.today.vendor_usd == ((VENDOR_CLAUDE, 15.00), (VENDOR_CODEX, 8.00))
        assert breakdown.today.usd_for_vendor(VENDOR_CODEX) == 8.00

        # The two unknown buckets stay apart - one row per vendor, both $0.
        unknown_rows = [row for row in breakdown.by_model if row.is_unknown]
        assert len(unknown_rows) == 2, unknown_rows
        assert {row.vendor for row in unknown_rows} == {VENDOR_CLAUDE, VENDOR_CODEX}
        assert all(row.usd == 0.0 for row in unknown_rows)

        # Every row is attributed, and no row leaks the storage key.
        for row in breakdown.by_model:
            assert row.vendor in (VENDOR_CLAUDE, VENDOR_CODEX)
            for raw in row.raw_models:
                assert not raw.startswith(f"{VENDOR_CODEX}:"), raw

        # Codex's rows are the Codex ones, and only those.
        codex_models = {row.model for row in breakdown.rows_for_vendor(VENDOR_CODEX)}
        assert codex_models == {SOL, UNKNOWN_MODEL}, codex_models


def test_claude_only_store_renders_no_codex_section() -> None:
    """The other half of SPEC-CODEX 5.5, through the real cost path.

    A store holding only bare Claude keys - which is byte-for-byte what the live
    ``rollups.json`` holds - must produce exactly one vendor group, no Codex
    row, and the same single-vendor menu shape as before Codex existed.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        today = local_day_key(time.time())
        store = DailyRollupStore(path=tmp / "rollups.json", keep_days=30)
        store.merge(
            [
                DayRollup(
                    day=today,
                    models={
                        "claude-fable-5": ModelUsage(input=1_000_000, output=100_000),
                        "claude-sonnet-5": ModelUsage(input=2_000_000),
                    },
                )
            ]
        )
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today=today, progress=IndexProgress(complete=True)
        )
        assert breakdown.vendors == (VENDOR_CLAUDE,)
        assert breakdown.has_vendor(VENDOR_CODEX) is False
        assert breakdown.rows_for_vendor(VENDOR_CODEX) == ()
        assert all(row.vendor == VENDOR_CLAUDE for row in breakdown.by_model)
        assert [group.vendor for group in breakdown.by_vendor] == [VENDOR_CLAUDE]
        assert breakdown.today.usd_for_vendor(VENDOR_CLAUDE) == breakdown.today.usd


# ---------------------------------------------------------------------------
# SPEC-CODEX 3a - a percent-less rate_limits record must not blank the headline
# ---------------------------------------------------------------------------


def test_plan_only_rate_limits_does_not_blank_the_percentage() -> None:
    """A later ``primary: null`` record must not erase ``used_percent``.

    ``rate_limits`` legitimately arrives with a null ``primary`` and a non-null
    ``plan_type``. ``CodexQuota.from_rate_limits`` then returns a row with
    ``used_percent=None`` whose ``is_empty`` is False (the plan is set), so it
    passes every emptiness check, wins on ``observed_at`` - and leaves
    ``_quota_windows()`` with nothing to render. The headline subscription bar,
    the one Codex figure that is NOT notional (SPEC-CODEX 3a), then disappears
    from the menu with no error, and the blanked snapshot is persisted to the
    sidecar so it survives restarts.

    Found live: ``.../2026/08/12/rollout-…019ff5bb….jsonl`` has 3,971
    ``rate_limits`` records whose last two are ``used_percent: 100.0`` followed
    by ``primary: null, plan_type: "pro"``. Both halves are exercised here:
    within one file (the ``newest_rate_limits`` choice) and across files (the
    ``_offer_quota`` comparison).
    """
    plan_only = {"primary": None, "plan_type": "pro"}

    # (a) within one file: the complete record comes first, the plan-only one last.
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(input_tokens=10, output_tokens=1,
                            rate_limits=rate_limits(12.0)),
                token_count(input_tokens=10, output_tokens=1,
                            rate_limits=plan_only),
            ],
        )
        indexer = make_indexer(tmp, root)
        indexer.scan_once()
        quota = indexer.quota()
        assert quota is not None
        assert quota.used_percent == 12.0, f"the plan-only record blanked it: {quota}"
        assert quota.plan_type == "pro"
        rows = indexer.quota_rows()
        assert len(rows) == 1 and rows[0].seven_day_pct == 12.0, rows

    # (b) across files: the newest file's only record is plan-only.
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        now = time.time()
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(input_tokens=10, output_tokens=1,
                            rate_limits=rate_limits(37.0)),
            ],
            name="rollout-aaa-older.jsonl",
            mtime=now - 3_600,
        )
        write_rollout(
            root,
            [
                turn_context(SOL),
                token_count(input_tokens=10, output_tokens=1, rate_limits=plan_only),
            ],
            name="rollout-zzz-newest.jsonl",
            mtime=now,
        )
        indexer = make_indexer(tmp, root)
        indexer.scan_once()
        quota = indexer.quota()
        assert quota is not None
        assert quota.used_percent == 37.0, f"the newer plan-only file blanked it: {quota}"
        assert quota.plan_type == "pro"
        # And the sidecar must not persist a blanked snapshot.
        indexer.commit_state()
        reloaded = make_indexer(tmp, root)
        assert reloaded.quota() is not None
        assert reloaded.quota().used_percent == 37.0  # type: ignore[union-attr]

    # A genuinely newer COMPLETE record still wins - the guard is about missing
    # percentages, not about freezing the first one ever seen.
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        now = time.time()
        write_rollout(
            root,
            [turn_context(SOL),
             token_count(input_tokens=10, rate_limits=rate_limits(5.0))],
            name="rollout-aaa-older.jsonl",
            mtime=now - 3_600,
        )
        write_rollout(
            root,
            [turn_context(SOL),
             token_count(input_tokens=10, rate_limits=rate_limits(66.0))],
            name="rollout-zzz-newest.jsonl",
            mtime=now,
        )
        indexer = make_indexer(tmp, root)
        indexer.scan_once()
        assert indexer.quota().used_percent == 66.0  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# SPEC-CODEX 2.1 - the two pre-filters are not mutually exclusive
# ---------------------------------------------------------------------------


def test_model_record_carrying_the_usage_marker_is_still_read() -> None:
    """A ``turn_context`` whose bytes contain ``"token_count"`` still sets the model.

    The usage pre-filter is a substring test over raw bytes, so any
    model-bearing record that happens to contain the literal ``"token_count"``
    - in a ``cwd``, an argument, a path - matches it first. Chained with
    ``elif`` the line went to the usage handler, was correctly rejected there as
    a non-``token_count`` record, and was then dropped: the model it stated was
    never read, and every following turn was priced at the PREVIOUS model's
    rate. Silent - no error, no ``records_malformed``, no unknown bucket.

    Here the second ``turn_context`` switches luna -> sol and the ten turns
    after it are 1,000,000 input tokens each. Mis-attributed they are billed at
    luna's $0.20/Mtok instead of sol's $5.00/Mtok: $2.20 instead of $50.20, a
    22.8x under-report, and the sol row vanishes from the menu.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        poisoned = turn_context("gpt-5.6-sol")
        poisoned["payload"]["cwd"] = "token_count"  # the 13 marker bytes, verbatim
        # `total_token_usage` is cumulative, so it must advance per turn - two
        # turns sharing one total are a TRAP 4 re-emission and are suppressed.
        records: list[dict[str, Any]] = [
            turn_context("gpt-5.6-luna"),
            token_count(input_tokens=1_000_000, total={"input_tokens": 1_000_000}),
            poisoned,
        ]
        records += [
            token_count(
                input_tokens=1_000_000,
                total={"input_tokens": (turn + 2) * 1_000_000},
            )
            for turn in range(10)
        ]
        path = write_rollout(root, records)
        assert b'"token_count"' in path.read_bytes(), "fixture does not trip the filter"

        indexer = make_indexer(tmp, root)
        got = counted(indexer.scan_once())
        assert got.get("codex:gpt-5.6-sol", ModelUsage()).input == 10_000_000, got
        assert got.get("codex:gpt-5.6-luna", ModelUsage()).input == 1_000_000, got


# ---------------------------------------------------------------------------
# SPEC 3.3 trap 4 - the day memo must not collapse two UTC offsets
# ---------------------------------------------------------------------------


def test_day_memo_does_not_collide_across_utc_offsets() -> None:
    """Two timestamps with the same wall clock and different offsets are
    different instants and may fall on different local days.

    The memo used to key on ``timestamp[:16]``, which truncates the offset away,
    so ``…T01:30:00+05:00`` and ``…T01:30:00-05:00`` - ten hours apart - shared
    one memo entry and the second inherited the first's day. The memo lives on
    the indexer, so the collision spans files within a scan. A record put in the
    wrong bucket moves money between Today / 7d / 30d and can be pushed out of
    the window entirely, where it is dropped for good.
    """
    with tempfile.TemporaryDirectory() as name:
        tmp = Path(name)
        root = tmp / "sessions"
        root.mkdir(parents=True)
        indexer = make_indexer(tmp, root)
        fallback = local_day_key(time.time())

        east = indexer._day_key_for("2026-08-17T01:30:00+05:00", fallback)
        west = indexer._day_key_for("2026-08-17T01:30:00-05:00", fallback)
        fresh = make_indexer(tmp, root)._day_key_for("2026-08-17T01:30:00-05:00", fallback)
        assert west == fresh, f"memo collision: {west} via the memo, {fresh} without it"
        assert east != west or fresh == east, (east, west, fresh)

        # The memo must still HIT within one minute+offset, which is its point.
        first = indexer._day_key_for("2026-08-17T09:15:00.100Z", fallback)
        indexer._day_memo_value = "1999-01-01"  # only a memo hit can return this
        assert indexer._day_key_for("2026-08-17T09:15:00.900Z", fallback) == "1999-01-01"
        assert first == make_indexer(tmp, root)._day_key_for(
            "2026-08-17T09:15:00.100Z", fallback
        )


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
