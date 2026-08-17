"""Regression tests for the seven blocking findings of the 2026-08-17 review.

Each test here fails on the pre-fix code and passes after it. They are grouped by
the invariant they defend, not by the module they touch, because most of these
bugs live in the *seam* between two modules (offsets vs rollup, adapter vs menu,
worker vs main thread) and a single-module test would not have caught them.

===  =============================================================  =========================================================
 #   invariant                                                       test
===  =============================================================  =========================================================
 1   a lost scan state must not double the rollup                    :func:`test_lost_scan_state_does_not_double_count`
 2   a resumed read must not re-credit a streaming snapshot          :func:`test_resume_across_chunk_boundary_does_not_double_count`
 3   one BaseException must not kill the worker forever              :func:`test_worker_survives_systemexit`
                                                                     :func:`test_dead_worker_is_restarted_and_reported`
 4   a broken account backend must be visible, not an empty menu     :func:`test_broken_account_backend_is_reported`
 5   a failed autoswitch write must not silently revert the click    :func:`test_failed_autoswitch_write_is_reported_and_holds_off`
 6   the rollup must be durable before the offsets                   :func:`test_offsets_are_not_committed_before_the_rollup`
 7   two widgets must not share three mutable caches                 :func:`test_single_instance_lock_refuses_a_second_widget`
===  =============================================================  =========================================================

The 2026-08-17 Codex review added five more, all in the same seam:

===  =============================================================  =========================================================
 8   one vendor's lost cache must not wipe the other's history       :func:`test_lost_codex_scan_state_keeps_claude_history`
 9   a source joining mid-session gets its own doubling check        :func:`test_source_that_joins_later_is_reconciled_before_its_first_merge`
10   a steady tick walks ONE corpus, and starves neither             :func:`test_steady_ticks_alternate_corpora_without_starving_either`
11   an absent corpus is a normal state, not a permanent `!` row     :func:`test_absent_claude_corpus_is_silent_not_an_error_row`
12   no Codex controls on a machine with no Codex                    :func:`test_absent_codex_corpus_offers_no_codex_settings`
===  =============================================================  =========================================================

Plus the non-blocking items that were fixed: ``Today`` double-rounding, a
future-dated bucket retained-and-hidden, an unknown ``cache_creation`` TTL tier
zeroing a record's cache write, ``rows()`` blocking the main thread on a cold
start, and unthrottled traceback spam.

Run with pytest if it is available, or directly::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    $PY tests/test_regressions.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import indexer as indexer_mod  # noqa: E402
from cc_usage_widget.accounts import ACCOUNTS_UNAVAILABLE, SwapAccountSource  # noqa: E402
from cc_usage_widget.app import BackgroundWorker, UiSnapshot  # noqa: E402
from cc_usage_widget.codex_indexer import CodexIndexer  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    IndexProgress,
    ModelUsage,
    local_day_key,
    normalize_settings,
)
from cc_usage_widget.indexer import Indexer  # noqa: E402
from cc_usage_widget.pricing import DEFAULT_PRICING  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402

FABLE = "claude-fable-5-20260514"
COMPLETE = IndexProgress(files_done=1, files_total=1, complete=True)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _record(request_id: str, epoch: float, **usage: int) -> dict[str, Any]:
    """One assistant record shaped like a real Claude Code transcript line."""
    return {
        "type": "assistant",
        "requestId": request_id,
        "timestamp": _iso(epoch),
        "message": {
            "id": f"msg_{request_id}",
            "role": "assistant",
            "model": FABLE,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation", 0),
                "cache_creation": {
                    "ephemeral_5m_input_tokens": usage.get("cache_creation", 0),
                    "ephemeral_1h_input_tokens": 0,
                },
            },
        },
    }


def _write(path: Path, records: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _worker(root: Path, *, indexer: Any, rollups: Any) -> BackgroundWorker:
    """A worker wired for cost only, with the settings the real app would pass."""
    snapshot = UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS)))
    return BackgroundWorker(
        publish=lambda _snapshot: None,
        snapshot=snapshot,
        accounts=None,
        indexer=indexer,
        rollups=rollups,
        pricing=DEFAULT_PRICING,
    )


def _drain_cost(worker: BackgroundWorker, *, limit: int = 40) -> None:
    """Run cost jobs until the first index reports complete."""
    for _ in range(limit):
        worker._run_cost_job()
        progress = worker._indexer.progress()  # type: ignore[union-attr]
        if progress.complete:
            return
    raise AssertionError("index never completed")


def _totals(root: Path, *, keep_days: int = 30) -> tuple[float, float]:
    """``(today, last_30d)`` as a *freshly loaded* store reports them."""
    store = DailyRollupStore(path=root / "rollups.json", keep_days=keep_days)
    store.load()
    breakdown = store.cost_breakdown(
        DEFAULT_PRICING, today=local_day_key(time.time()), progress=COMPLETE
    )
    return breakdown.today.usd, breakdown.last_30d.usd


# ---------------------------------------------------------------------------
# 1. rollups.json and scan_state.json are two halves of one fact
# ---------------------------------------------------------------------------


def test_lost_scan_state_does_not_double_count() -> None:
    """Deleting scan_state.json must not double every day in the window.

    ``RollupStore.merge`` is purely additive, so a lost scan state used to make
    the next pass re-read the whole lookback window and ADD it on top of the
    surviving rollup - measured at exactly 2.000x on the real corpus, permanent,
    and cumulative per loss. The owner must therefore empty the store when the
    offsets come back missing.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        _write(
            root / "projects" / "p" / "a.jsonl",
            [
                _record("req-today", now, input_tokens=1_000, output_tokens=2_000,
                        cache_read_input_tokens=100_000, cache_creation=200_000),
                _record("req-old", now - 2 * 86400, input_tokens=1_000,
                        output_tokens=2_000, cache_creation=200_000),
            ],
        )

        def run() -> tuple[float, float]:
            indexer = Indexer(
                projects_dir=root / "projects",
                state_path=root / "scan_state.json",
                lookback_days=30,
                pricing=DEFAULT_PRICING,
                defer_state_commit=True,
            )
            store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
            worker = _worker(root, indexer=indexer, rollups=store)
            _drain_cost(worker)
            worker._flush()
            return _totals(root)

        first = run()
        assert first[0] > 0 and first[1] > 0, first
        assert (root / "scan_state.json").exists()

        # The exact failure the review reproduced: the state file is gone, the
        # rollup file is untouched.
        (root / "scan_state.json").unlink()
        second = run()
        assert second == first, f"doubled after losing the scan state: {first} -> {second}"

        # And again with a truncated (unparseable) file rather than a missing one.
        (root / "scan_state.json").write_text("{tru", encoding="utf-8")
        third = run()
        assert third == first, f"doubled after a corrupt scan state: {first} -> {third}"


# ---------------------------------------------------------------------------
# 1b. ... and the cure must be per vendor, because the trigger is
# ---------------------------------------------------------------------------


def _codex_rollout(root: Path, records: list[dict[str, Any]]) -> Path:
    """Write one ``rollout-*.jsonl`` into the dated tree Codex actually uses."""
    today = dt.date.today()
    directory = (
        root / f"{today.year:04d}" / f"{today.month:02d}" / f"{today.day:02d}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{today.isoformat()}T12-00-00-fixture.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _codex_turns(model: str, turns: int, *, tokens: int = 1_000) -> list[dict[str, Any]]:
    """A ``turn_context`` plus *turns* distinct ``token_count`` records."""
    now = time.time()
    out: list[dict[str, Any]] = [
        {
            "timestamp": _iso(now),
            "type": "turn_context",
            "payload": {"model": model, "cwd": "/tmp"},
        }
    ]
    for turn in range(turns):
        out.append(
            {
                "timestamp": _iso(now),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        # `total_token_usage` is cumulative, so it must advance
                        # per turn or the TRAP 4 guard suppresses the repeat.
                        "total_token_usage": {"input_tokens": (turn + 1) * tokens},
                        "last_token_usage": {
                            "input_tokens": tokens,
                            "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0,
                            "output_tokens": 0,
                            "reasoning_output_tokens": 0,
                        },
                    },
                },
            }
        )
    return out


def _two_vendor_worker(
    root: Path, *, codex_on: bool = True
) -> tuple[BackgroundWorker, Any, Any]:
    """A worker wired for BOTH corpora, entirely inside *root*."""
    indexer = Indexer(
        projects_dir=root / "projects",
        state_path=root / "scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
        defer_state_commit=True,
    )
    codex = CodexIndexer(
        sessions_dir=root / "sessions",
        state_path=root / "codex_scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
        defer_state_commit=True,
    )
    store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
    settings = normalize_settings(
        {**SETTINGS_DEFAULTS, "codex_tracking_enabled": codex_on}
    )
    worker = BackgroundWorker(
        publish=lambda _snapshot: None,
        snapshot=UiSnapshot(settings=settings),
        accounts=None,
        indexer=indexer,
        rollups=store,
        pricing=DEFAULT_PRICING,
        sources=(codex,),
    )
    return worker, indexer, codex


def _drain_both(worker: BackgroundWorker, scanners: list[Any], *, limit: int = 40) -> None:
    for _ in range(limit):
        worker._run_cost_job()
        if all(scanner.progress().complete for scanner in scanners):
            worker._flush()
            return
    raise AssertionError("index never completed")


def _by_vendor(root: Path) -> dict[str, dict[str, dict[str, list[int]]]]:
    """``{vendor: {day: {model_key: counters}}}`` straight off disk.

    Read from the JSON rather than through the store so the assertion is about
    the bytes a restart would find, not about anything held in memory.
    """
    raw = json.loads((root / "rollups.json").read_text(encoding="utf-8"))
    days = raw.get("days", raw)
    out: dict[str, dict[str, dict[str, list[int]]]] = {}
    for day, models in days.items():
        if not isinstance(models, dict):
            continue
        for key, counters in models.items():
            vendor = key.split(":", 1)[0] if ":" in key else "claude"
            out.setdefault(vendor, {}).setdefault(day, {})[key] = counters
    return out


def test_lost_codex_scan_state_keeps_claude_history() -> None:
    """Losing ONE vendor's scan state must not wipe the other vendor's days.

    ``rollups.json`` is shared (a window total has to span both vendors) but the
    two vendors do not share an accounting fate: only the vendor whose own
    offsets vanished can double. The first implementation cured globally -
    ``rollups.clear()`` plus ``reset()`` on every scanner - so deleting
    ``codex_scan_state.json``, a file both docstrings advertise as a pure
    per-vendor cache, dropped all 29 days of Claude history and forced a full
    1.4 GB Claude re-index. Worse, it is not always recoverable: Claude Code
    prunes ``~/.claude/projects`` on its own ``cleanupPeriodDays``, so a day
    whose transcript has aged off disk is zeroed permanently by a Codex-side
    cache loss.

    The aged-out transcript is what this test turns into an assertion: the
    global cure passes a test where every transcript is still readable, because
    the re-read restores the same numbers.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        old = root / "projects" / "p" / "old.jsonl"
        _write(old, [_record("req-old", now - 3 * 86400, input_tokens=5_000,
                             output_tokens=1_000)])
        _write(
            root / "projects" / "p" / "today.jsonl",
            [_record("req-today", now, input_tokens=1_000, output_tokens=2_000)],
        )
        _codex_rollout(root / "sessions", _codex_turns("gpt-5.6-sol", 3))

        worker, indexer, codex = _two_vendor_worker(root)
        _drain_both(worker, [indexer, codex])
        before = _by_vendor(root)
        assert before["claude"], before
        assert before["codex"], before
        assert len(before["claude"]) == 2, before["claude"]

        # The Claude transcript for the older day ages off disk, exactly as
        # Claude Code's own cleanup does. Its money survives only in rollups.json.
        old.unlink()
        # And the Codex cache is lost - deleted, corrupted, or never written.
        (root / "codex_scan_state.json").unlink()

        worker, indexer, codex = _two_vendor_worker(root)
        _drain_both(worker, [indexer, codex])
        after = _by_vendor(root)

        assert after["claude"] == before["claude"], (
            "a Codex cache loss destroyed Claude history: "
            f"{before['claude']} -> {after['claude']}"
        )
        assert after["codex"] == before["codex"], (
            f"Codex doubled instead of being re-read: {before['codex']} -> {after['codex']}"
        )


def test_source_that_joins_later_is_reconciled_before_its_first_merge() -> None:
    """A scanner that appears mid-session gets its own anti-doubling check.

    The guard used to run exactly once, inside ``if not self._rollups_loaded``,
    and only over the sources enabled AND available at that instant. Switching
    ``Codex tracking`` on afterwards therefore let a Codex indexer with offset 0
    re-read the whole lookback window and ``merge`` it on top of the Codex rows
    already in the store - measured at exactly 2.000x, persisted, permanent. The
    same door opens whenever ``available()`` is False at the first cost job (a
    corpus on a late-mounted volume) and True later.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _write(
            root / "projects" / "p" / "today.jsonl",
            [_record("req-today", time.time(), input_tokens=1_000, output_tokens=2_000)],
        )
        _codex_rollout(root / "sessions", _codex_turns("gpt-5.6-sol", 3))

        worker, indexer, codex = _two_vendor_worker(root, codex_on=True)
        _drain_both(worker, [indexer, codex])
        before = _by_vendor(root)
        assert before["codex"], before

        # The Codex cache is lost while the widget is not looking.
        (root / "codex_scan_state.json").unlink()

        # Relaunch with the toggle OFF: the store loads, Claude reconciles, and
        # Codex is not in `_scanners()` at all - nothing to check yet.
        worker, indexer, codex = _two_vendor_worker(root, codex_on=False)
        worker._run_cost_job()
        assert worker._rollups_loaded is True

        # Now the user clicks "Codex tracking" on. This is the first tick that
        # can double, and it happens long after the one-shot check ran.
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**worker._snapshot.settings, "codex_tracking_enabled": True}
            ),
        )
        _drain_both(worker, [indexer, codex])
        after = _by_vendor(root)

        assert after["codex"] == before["codex"], (
            f"Codex doubled after being switched on mid-session: "
            f"{before['codex']} -> {after['codex']}"
        )
        assert after["claude"] == before["claude"], (
            f"Claude was disturbed by the Codex reconciliation: "
            f"{before['claude']} -> {after['claude']}"
        )


def test_steady_ticks_alternate_corpora_without_starving_either() -> None:
    """One corpus per steady tick, and the tick fires proportionally more often.

    SPEC 2.1's 30 ms is a **per-tick** budget, and a steady tick is ~100%
    directory walk: measured on the live corpora, Claude 19.9 ms + Codex 9.9 ms
    = 30.1 ms combined, over the line, with zero files opened - there is nothing
    in Python that makes a walk cheaper than the walk. Alternating the two trees
    keeps every tick inside the budget (measured after: median 18.7-20.5 ms,
    max 22.8 ms).

    What must NOT change is each vendor's own cadence, so this pins both halves:
    every vendor is scanned exactly once per rotation, and the returned due time
    is divided by the number of corpora so the wall-clock interval per vendor is
    the one the user configured. It also pins that an explicit Refresh reads
    everything - a background optimisation must not make a click do less.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _write(
            root / "projects" / "p" / "today.jsonl",
            [_record("req-today", time.time(), input_tokens=1_000, output_tokens=2_000)],
        )
        _codex_rollout(root / "sessions", _codex_turns("gpt-5.6-sol", 2))

        worker, indexer, codex = _two_vendor_worker(root)
        _drain_both(worker, [indexer, codex])

        seen: list[str] = []
        real = {
            "claude": indexer.scan_once,
            "codex": codex.scan_once,
        }

        def spy(vendor: str) -> Any:
            def wrapped(**kwargs: Any) -> Any:
                seen.append(vendor)
                return real[vendor](**kwargs)

            return wrapped

        indexer.scan_once = spy("claude")  # type: ignore[method-assign]
        codex.scan_once = spy("codex")  # type: ignore[method-assign]

        interval = float(worker._snapshot.settings["cost_interval_seconds"])
        gaps = []
        for _ in range(4):
            start = time.monotonic()
            gaps.append(worker._run_cost_job() - start)
        assert seen == ["claude", "codex", "claude", "codex"], seen
        for gap in gaps:
            assert abs(gap - interval / 2) < 1.0, (gap, interval)

        # An explicit Refresh / cost-touching settings change reads both.
        seen.clear()
        worker._force_all_sources = True
        gap = worker._run_cost_job() - time.monotonic()
        assert sorted(seen) == ["claude", "codex"], seen
        assert abs(gap - interval) < 1.0, (gap, interval)

        # A still-broken vendor's error must not blink off on the other
        # vendor's turn: an error that shows every other tick is an error a
        # user learns to ignore. The break here is real - an unreadable
        # directory inside the Codex tree - so it is genuinely still true on
        # the ticks that do not look at it.
        indexer.scan_once = real["claude"]  # type: ignore[method-assign]
        codex.scan_once = real["codex"]  # type: ignore[method-assign]
        walled = next((root / "sessions").rglob("*/*/*"))
        assert walled.is_dir(), walled
        os.chmod(walled, 0o000)
        try:
            shown = []
            for _ in range(5):
                worker._run_cost_job()
                shown.append(worker._snapshot.cost_error)
        finally:
            os.chmod(walled, 0o700)
        # The rotation resumed on Claude, which cannot see the break yet; from
        # the first Codex tick onwards the line must never disappear, and half
        # of those ticks never look at Codex at all.
        assert shown[0] is None, shown
        assert all(text and "unreadable" in text for text in shown[1:]), shown


def test_absent_claude_corpus_is_silent_not_an_error_row() -> None:
    """An absent ``~/.claude/projects`` is a normal state, like an absent ``~/.codex``.

    The widget installs into ``~/.claude``, so on a machine that runs Codex only
    ``~/.claude`` exists but ``~/.claude/projects`` does not. ``_iter_files``
    used to append the root's own ``scandir`` ``OSError`` on every 300 s tick,
    so ``result.errors`` was never empty, ``_forget_failures('cost')`` was never
    reached, and the menu carried a permanent red
    ``! cost: 1 file(s) unreadable: …/projects: [Errno 2]`` line - while the
    Codex quota and cost rows beside it rendered perfectly. A root that EXISTS
    but cannot be walked is still reported; that is a real failure.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        indexer = Indexer(
            projects_dir=root / "does-not-exist",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        result = indexer.scan_once()
        assert result.errors == (), result.errors
        assert result.files_seen == 0 and result.files_read == 0
        assert indexer.progress().complete is True

        # ... but an unreadable root is still a real, reported failure.
        blocked = root / "blocked"
        blocked.mkdir()
        (blocked / "keep.jsonl").write_text("", encoding="utf-8")
        os.chmod(blocked, 0o000)
        try:
            walled = Indexer(
                projects_dir=blocked,
                state_path=root / "scan_state2.json",
                lookback_days=30,
                pricing=DEFAULT_PRICING,
            )
            assert walled.scan_once().errors, "an unreadable root must still report"
        finally:
            os.chmod(blocked, 0o700)


def test_absent_codex_corpus_offers_no_codex_settings() -> None:
    """A Claude-only machine must not be offered Codex controls or a Codex path.

    ``__main__.build()`` constructs a ``CodexIndexer`` unconditionally, so
    "``extra_sources`` is non-empty" answers "was a source object built", not
    "does this machine have Codex". Gating the Settings menu on it gave a
    machine with no ``~/.codex`` a ``Codex tracking`` switch, a
    ``Codex weekly percentage`` title toggle, and a diagnostics row pointing at
    a directory that does not exist. The corrected gate is availability, probed
    on the worker thread and published as a plain tuple - never ``stat``-ed
    while a menu is being built (SPEC 2.3).
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        (root / "projects").mkdir()

        def worker_for(sessions: Path) -> BackgroundWorker:
            return BackgroundWorker(
                publish=lambda _s: None,
                snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
                accounts=None,
                indexer=Indexer(
                    projects_dir=root / "projects",
                    state_path=root / f"state-{sessions.name}.json",
                    lookback_days=30,
                    pricing=DEFAULT_PRICING,
                ),
                rollups=DailyRollupStore(path=root / "rollups.json", keep_days=30),
                pricing=DEFAULT_PRICING,
                sources=(
                    CodexIndexer(
                        sessions_dir=sessions,
                        state_path=root / f"codex-{sessions.name}.json",
                        lookback_days=30,
                        pricing=DEFAULT_PRICING,
                    ),
                ),
            )

        absent = worker_for(root / "no-codex-here")
        absent._collect_quota_rows()
        assert absent.extra_sources, "the source object IS built - that is the trap"
        assert absent.available_vendors == (), absent.available_vendors
        assert absent.source_roots() == (), absent.source_roots()

        present_dir = root / "sessions"
        _codex_rollout(present_dir, _codex_turns("gpt-5.6-sol", 2))
        present = worker_for(present_dir)
        present._collect_quota_rows()
        assert present.available_vendors == ("codex",), present.available_vendors
        assert present.source_roots(), "a real corpus must still be named"

        # Switching the vendor OFF must not delete the switch that turns it on.
        present._snapshot = replace(
            present._snapshot,
            settings=normalize_settings(
                {**present._snapshot.settings, "codex_tracking_enabled": False}
            ),
        )
        assert present._collect_quota_rows() == ()
        assert present.available_vendors == ("codex",), present.available_vendors


# ---------------------------------------------------------------------------
# 2. dedup must survive a resume boundary
# ---------------------------------------------------------------------------


class _CutClock:
    """``time`` shim whose ``monotonic`` jumps past the deadline after *n* calls.

    Deterministically forces ``_scan_file`` to stop mid-file, which is what a
    0.75 s chunk deadline does to a 1.4 GB corpus on every first index.
    """

    def __init__(self, calls_before_jump: int) -> None:
        self._left = calls_before_jump
        self.time = time.time

    def monotonic(self) -> float:
        if self._left > 0:
            self._left -= 1
            return 0.0
        return 10_000.0


def test_resume_across_chunk_boundary_does_not_double_count() -> None:
    """Two streaming snapshots of one request, split by a deadline cut.

    The pair shares a ``requestId``; the second snapshot repeats the whole
    ``cache_creation`` and grows only ``output_tokens``. Correct behaviour credits
    55,901 tokens (the final snapshot). Before the fix the per-file dedup map was
    allocated inside ``_scan_file`` and thrown away when the file closed, so the
    resuming pass saw no prior and credited the record again: 111,595 tokens.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        # A historical day, so the process-lifetime "today" map is not involved.
        stamp = time.time() - 3 * 86400
        day = local_day_key(stamp)
        path = _write(
            root / "projects" / "p" / "a.jsonl",
            [
                _record("req-stream", stamp, input_tokens=2, output_tokens=1,
                        cache_creation=55_691),
                _record("req-stream", stamp, input_tokens=2, output_tokens=208,
                        cache_creation=55_691),
            ],
        )
        assert path.exists()

        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )

        real_time = indexer_mod.time
        real_interval = indexer_mod._DEADLINE_LINE_INTERVAL
        counters: dict[str, list[int]] = {}
        try:
            # Check the deadline after every line, and make the check fail right
            # after the first record.
            indexer_mod._DEADLINE_LINE_INTERVAL = 1
            # Calls: `_scan_locked`'s start stamp, the pre-file deadline check,
            # then one per line. The third call is the check after line 1.
            indexer_mod.time = _CutClock(2)  # type: ignore[assignment]
            first = indexer.scan_once(deadline=10.0)
        finally:
            indexer_mod.time = real_time
            indexer_mod._DEADLINE_LINE_INTERVAL = real_interval

        assert first.files_read == 1, first
        assert first.progress.complete is False, first.progress
        for rollup in first.deltas:
            for model, usage in rollup.models.items():
                counters.setdefault(model, [0, 0, 0, 0, 0])
                for i, value in enumerate(usage.as_counters()):
                    counters[model][i] += value
        cut_tokens = sum(counters.get(FABLE, [0, 0, 0, 0, 0]))
        assert cut_tokens == 55_694, f"first chunk should hold snapshot 1: {cut_tokens}"

        second = indexer.scan_once()
        for rollup in second.deltas:
            assert rollup.day == day, rollup.day
            for model, usage in rollup.models.items():
                counters.setdefault(model, [0, 0, 0, 0, 0])
                for i, value in enumerate(usage.as_counters()):
                    counters[model][i] += value

        total = sum(counters[FABLE])
        assert total == 55_901, f"resume re-credited the record: {total} (want 55,901)"
        assert counters[FABLE] == [2, 208, 55_691, 0, 0], counters[FABLE]


def test_dedup_sidecar_survives_a_restart() -> None:
    """A new process must still recognise today's already-credited requests.

    The current day's dedup map is process-local; after a restart a later
    snapshot of a request whose earlier snapshot is already in the rollup would
    be credited whole. The sidecar written at shutdown closes that.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        path = root / "projects" / "p" / "a.jsonl"
        _write(path, [_record("req-live", now, input_tokens=2, output_tokens=1,
                              cache_creation=55_691)])

        first = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        first.scan_once()
        first.flush_dedup()  # what the worker's final flush does
        assert (root / "scan_state_dedup.json").exists()

        # A later, larger snapshot of the same request is appended.
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _record("req-live", now, input_tokens=2, output_tokens=208,
                            cache_creation=55_691)
                )
                + "\n"
            )

        second = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        result = second.scan_once()
        total = 0
        for rollup in result.deltas:
            total += rollup.total.total_tokens
        assert total == 207, f"restart re-credited the snapshot: {total} (want 207)"


def test_lost_scan_state_also_drops_the_dedup_sidecar() -> None:
    """A sidecar must never suppress a re-read the lost offsets force.

    Honouring it there would swing the bug from double-counting to reporting
    ``$0`` for today, which is worse: it looks plausible.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        _write(
            root / "projects" / "p" / "a.jsonl",
            [_record("req-a", now, input_tokens=1_000, output_tokens=2_000)],
        )
        first = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        baseline = first.scan_once()
        first.flush_dedup()
        want = sum(r.total.total_tokens for r in baseline.deltas)
        assert want == 3_000, want

        (root / "scan_state.json").unlink()
        second = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        again = second.scan_once()
        got = sum(r.total.total_tokens for r in again.deltas)
        assert got == want, f"sidecar suppressed the forced re-read: {got} != {want}"


# ---------------------------------------------------------------------------
# 3. one BaseException must not kill the worker forever
# ---------------------------------------------------------------------------


class _ExitingAccounts:
    """An account source that does what a CLI-shaped helper does on a fatal
    config error: ``sys.exit()``, i.e. raise ``SystemExit``."""

    def __init__(self) -> None:
        self.calls = 0

    def refresh(self, *, force: bool = False) -> None:
        self.calls += 1
        raise SystemExit("claude_swap called sys.exit()")

    def rows(self) -> tuple[Any, ...]:
        return ()

    def active(self) -> Any:
        return None

    def autoswitch_enabled(self) -> bool | None:
        return None

    def set_autoswitch_enabled(self, enabled: bool) -> None:
        return None

    def evaluate_autoswitch(self) -> str | None:
        return None

    def switch_to(self, slot_or_alias: str) -> bool:
        return False


def test_worker_survives_systemexit() -> None:
    """``SystemExit`` from a seam degrades to a menu line; the loop lives on."""
    published: list[UiSnapshot] = []
    accounts = _ExitingAccounts()
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=accounts,
    )
    worker.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not published:
            time.sleep(0.02)
        assert published, "worker published nothing"
        assert worker.alive, "worker thread died on SystemExit"
        assert accounts.calls >= 1, accounts.calls
        errors = [s.accounts_error for s in published if s.accounts_error]
        assert errors and "SystemExit" in errors[0], errors
    finally:
        worker.stop(timeout=2.0)


def test_dead_worker_is_restarted_and_reported() -> None:
    """A dead worker is noticed by the repaint tick, reported, and restarted."""
    app = app_mod.CCUsageWidgetApp()
    try:
        app._running = True
        assert app._worker.alive is False
        app._supervise_worker()
        assert app._worker.alive is True
        texts = app.snapshot().wiring_errors
        assert any("background worker stopped" in text for text in texts), texts
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# 4. a broken account backend must be visible
# ---------------------------------------------------------------------------


class _SilentlyBrokenAccounts(_ExitingAccounts):
    """What ``SwapAccountSource`` looks like with ``claude_swap`` missing: no
    exception, no rows, the cause only in ``last_error``."""

    last_error = "claude-swap unavailable: ImportError: No module named 'claude_swap'"
    available = False

    def refresh(self, *, force: bool = False) -> None:
        self.calls += 1


def test_broken_account_backend_is_reported() -> None:
    """An empty, uncomplaining read must not render as ``none found``."""
    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=_SilentlyBrokenAccounts(),
    )
    worker._run_accounts_job(force=False)
    assert published, "nothing published"
    snapshot = published[-1]
    assert snapshot.accounts == ()
    assert snapshot.accounts_error, "a totally broken backend reported no error"
    assert ACCOUNTS_UNAVAILABLE in snapshot.accounts_error
    assert "claude_swap" in snapshot.accounts_error


# ---------------------------------------------------------------------------
# 5. a failed autoswitch write must not silently revert the click
# ---------------------------------------------------------------------------


class _UnwritableSource(SwapAccountSource):
    """claude-swap's settings.json is readable and says ON, but not writable."""

    def __init__(self, path: Path) -> None:
        super().__init__(settings=None)
        self._path = path

    def _policy_path(self) -> Path:
        return self._path

    def _write_enabled(self, enabled: bool) -> bool:
        self._record_error(f"refusing to overwrite unreadable {self._path}")
        return False


def test_failed_autoswitch_write_is_reported_and_holds_off() -> None:
    """A failed write raises, and OFF still holds for this session."""
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "settings.json"
        path.write_text(json.dumps({"autoswitch": {"enabled": True}}), encoding="utf-8")
        source = _UnwritableSource(path)
        assert source.autoswitch_enabled() is True

        raised: BaseException | None = None
        try:
            source.set_autoswitch_enabled(False)
        except Exception as exc:  # noqa: BLE001 - the point of the test
            raised = exc
        assert raised is not None, "a failed write reported success"
        assert "autoswitch" in str(raised)

        # The file still says True; the session must not snap back to ON.
        assert json.loads(path.read_text(encoding="utf-8"))["autoswitch"]["enabled"] is True
        assert source.autoswitch_enabled() is False, "the OFF click reverted itself"

        # And the worker turns that raise into a visible menu line.
        published: list[UiSnapshot] = []
        worker = BackgroundWorker(
            publish=published.append,
            snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
            accounts=source,
        )
        worker._set_autoswitch(False)
        assert published[-1].accounts_error, "no error surfaced for the failed write"
        assert published[-1].autoswitch_enabled is False


# ---------------------------------------------------------------------------
# 6. the rollup must be durable before the offsets
# ---------------------------------------------------------------------------


class _UnsaveableStore(DailyRollupStore):
    """A store whose ``save`` fails, standing in for a crash in that window."""

    def save(self, *, force: bool = False) -> None:
        raise OSError("disk full")


def test_offsets_are_not_committed_before_the_rollup() -> None:
    """If the rollup cannot be saved, the offsets must not say "consumed".

    Otherwise the tokens are lost permanently and invisibly: the next pass starts
    past them, and only ``Rebuild cost index`` recovers.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        _write(
            root / "projects" / "p" / "a.jsonl",
            [_record("req-a", now, input_tokens=1_000, output_tokens=2_000)],
        )
        state = root / "scan_state.json"

        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=state,
            lookback_days=30,
            pricing=DEFAULT_PRICING,
            defer_state_commit=True,
        )
        broken = _UnsaveableStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=broken)
        worker._run_cost_job()
        assert not state.exists(), "offsets were persisted although the rollup was not"

        # A healthy run over the same corpus still sees the tokens.
        indexer2 = Indexer(
            projects_dir=root / "projects",
            state_path=state,
            lookback_days=30,
            pricing=DEFAULT_PRICING,
            defer_state_commit=True,
        )
        good = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker2 = _worker(root, indexer=indexer2, rollups=good)
        _drain_cost(worker2)
        assert state.exists(), "offsets were never committed on the healthy path"
        today, _long = _totals(root)
        assert today > 0, "the tokens were lost"


# ---------------------------------------------------------------------------
# 7. two widgets must not share three mutable caches
# ---------------------------------------------------------------------------


def test_single_instance_lock_refuses_a_second_widget() -> None:
    """The second instance is refused and told which PID holds the lock."""
    from cc_usage_widget import __main__ as main_mod

    with tempfile.TemporaryDirectory() as name:
        home = Path(name)
        original = main_mod.LOCK_PATH
        main_mod.LOCK_PATH = home / "widget.lock"
        try:
            acquired, detail = main_mod.acquire_single_instance_lock()
            assert acquired is True, detail
            assert str(os.getpid()) in detail

            env = dict(os.environ, CC_USAGE_WIDGET_HOME=str(home))
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from cc_usage_widget.__main__ import acquire_single_instance_lock as a;"
                    "print(a())",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            assert probe.returncode == 0, probe.stderr
            assert "False" in probe.stdout, probe.stdout
            assert str(os.getpid()) in probe.stdout, probe.stdout
        finally:
            handle = main_mod._lock_handle
            main_mod._lock_handle = None
            if handle is not None:
                handle.close()
            main_mod.LOCK_PATH = original


def test_rival_autoswitch_engine_is_detected() -> None:
    """A running ``cswap auto`` / ``cswap menubar`` must be seen and reported.

    Two engines against one ``autoswitch_state.json`` both evaluate the same
    threshold and both can switch - the double-poll SPEC 5 warns about. Upstream
    writes no owner marker, so the process table is the only evidence.

    This also pins the regex *flavour*: macOS ``pgrep`` compiles POSIX extended
    regex, where ``\\s`` is not a shorthand, so the first version of this check
    matched nothing at all while looking perfectly correct.
    """
    from cc_usage_widget import __main__ as main_mod

    fake = subprocess.Popen(["/bin/sh", "-c", 'exec -a "cswap auto" sleep 20'])
    try:
        deadline = time.monotonic() + 5.0
        found: list[str] = []
        while time.monotonic() < deadline and not found:
            found = main_mod._detect_rival_engines()
            if not found:
                time.sleep(0.1)
        assert found, "a running `cswap auto` was not detected"
        assert any("cswap" in line for line in found), found
    finally:
        fake.terminate()
        fake.wait(timeout=10)


# ---------------------------------------------------------------------------
# Non-blocking items that were fixed
# ---------------------------------------------------------------------------


def test_today_is_not_double_rounded() -> None:
    """Two half-cent models must not make Today twice the truth.

    ``Today`` used to be the sum of the already-quantised per-model rows, so
    $0.005 + $0.005 became $0.01 + $0.01 = $0.02 - and Today was then strictly
    greater than the 7-day window containing it.
    """
    with tempfile.TemporaryDirectory() as name:
        today = local_day_key(time.time())
        store = DailyRollupStore(path=Path(name) / "rollups.json", keep_days=30)
        store.add(today, "claude-haiku-4-5", ModelUsage(input=5_000))  # $1/Mtok
        store.add(today, "claude-opus-5", ModelUsage(input=1_000))  # $5/Mtok
        breakdown = store.cost_breakdown(DEFAULT_PRICING, today=today, progress=COMPLETE)
        assert breakdown.today.usd == 0.01, breakdown.today
        assert breakdown.last_7d.usd == 0.01, breakdown.last_7d
        assert breakdown.today.usd <= breakdown.last_7d.usd <= breakdown.last_30d.usd


def test_future_dated_day_is_visible_and_bounded() -> None:
    """A future-dated bucket must show up in a window, and must age out."""
    with tempfile.TemporaryDirectory() as name:
        today_date = dt.date.today()
        today = today_date.isoformat()
        tomorrow = (today_date + dt.timedelta(days=1)).isoformat()
        far = (today_date + dt.timedelta(days=90)).isoformat()
        store = DailyRollupStore(path=Path(name) / "rollups.json", keep_days=30)
        store.add(tomorrow, "claude-fable-5", ModelUsage(input=10_000_000))  # $100
        store.add(far, "claude-fable-5", ModelUsage(input=10_000_000))
        store.prune(today=today, keep_days=30)

        assert tomorrow in store.days(), store.days()
        assert far not in store.days(), "an absurdly future day was retained forever"
        breakdown = store.cost_breakdown(DEFAULT_PRICING, today=today, progress=COMPLETE)
        assert breakdown.last_30d.usd == 100.0, breakdown.last_30d
        assert breakdown.last_7d.usd == 100.0, breakdown.last_7d


def test_unknown_cache_ttl_tier_is_not_priced_at_zero() -> None:
    """A new ``cache_creation`` sub-field must not zero the record's cache write.

    ``{"ephemeral_1d_input_tokens": 55691}`` carries none of the two known keys,
    so the split fields were both ``None``, the flat sum was discarded, and the
    largest cost component in this corpus priced at $0 - while *deleting* the
    ``cache_creation`` key from the same record priced it correctly.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        record = {
            "type": "assistant",
            "requestId": "req-newtier",
            "timestamp": _iso(now),
            "message": {
                "id": "msg_req-newtier",
                "role": "assistant",
                "model": FABLE,
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 55_691,
                    "cache_creation": {"ephemeral_1d_input_tokens": 55_691},
                },
            },
        }
        _write(root / "projects" / "p" / "a.jsonl", [record])
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        result = indexer.scan_once()
        usage = result.deltas[0].models[FABLE]
        assert usage.cache_write_5m == 55_691, usage
        assert usage.cache_write_1h == 0, usage
        cost = DEFAULT_PRICING.cost_usd(FABLE, usage, dt.date.today())
        assert round(cost, 4) == 0.7012, cost


def test_rows_never_refreshes_on_the_main_thread() -> None:
    """A cold-start ``rows()`` on the AppKit thread must not block."""
    source = SwapAccountSource(settings=None)
    calls: list[bool] = []
    source.refresh = lambda *, force=False: calls.append(force)  # type: ignore[method-assign]
    assert threading.current_thread() is threading.main_thread()
    assert source.rows() == ()
    assert calls == [], "rows() blocked the main thread with no snapshot in hand"


def _raised(message: str) -> BaseException:
    """A ValueError that has actually been raised, so it carries a traceback."""
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


def test_repeated_failures_log_one_traceback() -> None:
    """A persistent cause must not emit a traceback on every tick.

    ~1,700 identical multi-line tracebacks a day buries every other line in the
    foreground and is unbounded, unrotated growth under a LaunchAgent. One
    traceback, then a counted one-liner at 2/4/8/16.
    """
    app_mod._forget_failures()
    lines: list[str] = []
    real = app_mod._log
    app_mod._log = lines.append  # type: ignore[assignment]
    try:
        for _ in range(16):
            app_mod._describe(_raised("same cause"), "probe")
    finally:
        app_mod._log = real  # type: ignore[assignment]
        app_mod._forget_failures()
    tracebacks = [line for line in lines if "Traceback" in line]
    assert len(tracebacks) == 1, f"{len(tracebacks)} tracebacks for one cause"
    assert len(lines) < 16, f"{len(lines)} log lines for 16 identical failures"
    assert "repeated 16x" in lines[-1], lines[-1]


def test_one_jobs_success_does_not_unmute_anothers_failure() -> None:
    """Clearing is per-job, so a still-broken cause stays muted."""
    app_mod._forget_failures()
    lines: list[str] = []
    real = app_mod._log
    app_mod._log = lines.append  # type: ignore[assignment]
    try:
        app_mod._describe(_raised("accounts down"), "accounts")
        app_mod._forget_failures("cost")  # the *other* job succeeded
        app_mod._describe(_raised("accounts down"), "accounts")
        muted = len([line for line in lines if "Traceback" in line])
        app_mod._forget_failures("accounts")  # now this job recovered
        app_mod._describe(_raised("accounts down"), "accounts")
    finally:
        app_mod._log = real  # type: ignore[assignment]
        app_mod._forget_failures()
    assert muted == 1, lines
    assert len([line for line in lines if "Traceback" in line]) == 2, lines


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
    failures: list[str] = []
    tests = _tests()
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
