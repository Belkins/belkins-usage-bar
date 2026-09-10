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

SPEC-CODEX 6 (live per-account Codex quota) adds five more, all defending one
promise: a second Codex source must cost the first one nothing.

===  =============================================================  =========================================================
13   no live source: today's menu and title, byte for byte           :func:`test_no_live_codex_source_renders_todays_exact_menu`
14   refresh / vendor toggle / quit all reach the poller             :func:`test_refresh_and_the_vendor_toggle_reach_the_live_source`
15   no registry file: no live-quota controls, no new lines          :func:`test_codex_accounts_submenu_appears_only_with_a_registry`
16   a checkbox writes the registry once, on the worker              :func:`test_a_registry_toggle_is_written_once_on_the_worker_thread`
17   the source's diagnostics reach the menu from a cache            :func:`test_source_diagnostics_reach_the_menu_without_touching_the_disk`
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
import logging
import os
import re
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
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

try:  # an OPTIONAL dependency (README: "account features need claude-swap")
    import claude_swap as _claude_swap  # noqa: E402
except Exception:  # pragma: no cover - a clean machine, and CI
    _claude_swap = None  # type: ignore[assignment]

_CLAUDE_SWAP_PRESENT = _claude_swap is not None
"""Whether upstream is importable. Read by exactly one assertion below, where
the widget's own behaviour legitimately DIFFERS without it."""

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import indexer as indexer_mod  # noqa: E402
from cc_usage_widget.accounts import ACCOUNTS_UNAVAILABLE, SwapAccountSource  # noqa: E402
from cc_usage_widget.app import BackgroundWorker, UiSnapshot  # noqa: E402
from cc_usage_widget.codex_indexer import CodexIndexer  # noqa: E402
from cc_usage_widget import accounts as accounts_mod  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    ALERT_ALL_EXHAUSTED,
    CODEX_PSEUDO_ACCOUNT_SLOT,
    ALERT_EXTERNAL_SWITCH,
    ALERT_KINDS,
    ALERT_NO_TARGET,
    SETTINGS_DEFAULTS,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    DayRollup,
    IndexProgress,
    ModelUsage,
    ScanResult,
    format_tokens,
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


_EMPTY_SCAN = ScanResult()
"""A scan that read nothing - the shape a stalled twin keeps returning."""


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


def test_the_dedup_sidecar_remembers_which_file_credited_what() -> None:
    """A restart must still be able to un-credit ONE file's requests.

    The sidecar is what makes today's dedup survive a restart, and after the
    restart a transcript replaced from a copy has to be able to hand its own ids
    back or its whole contribution to today goes to zero (the retraction removes
    it, the dedup map refuses to let the re-read put it back). So the sidecar
    carries the owning path beside the counters.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        path = root / "projects" / "p" / "a.jsonl"
        first_record = _record("req-a", now, input_tokens=1_000)
        _write(path, [first_record])
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)

        first = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        result = first.scan_once()
        store.retract_rollups(result.retractions)
        store.merge(result.deltas)
        first.flush_dedup()
        assert store.today(today).total.input == 1_000, store.today(today)
        sidecar = json.loads(
            (root / "scan_state_dedup.json").read_text(encoding="utf-8")
        )
        assert sidecar["owners"] == {str(path): ["req-a"]}, sidecar

        # A copy-then-move: same records plus one, brand-new inode.
        spare = path.with_suffix(".jsonl.new")
        _write(spare, [first_record, _record("req-b", now, input_tokens=1_000)])
        os.replace(spare, path)

        second = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        result = second.scan_once()
        assert result.retractions, "the replacement was not retracted"
        store.retract_rollups(result.retractions)
        store.merge(result.deltas)
        assert store.today(today).total.input == 2_000, store.today(today)


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
def test_autoswitch_write_failure_latch_releases_on_a_later_success() -> None:
    """2026-08-26 gate: one transient write failure deafened the widget forever.

    On a failed write to claude-swap's settings.json the adapter starts
    trusting its own in-memory flag over the file — correct, so the toggle
    cannot silently revert. But the latch was never released, so a later
    SUCCESSFUL write left it set: from then on ``autoswitch_enabled()``
    short-circuits to the in-memory value and a ``cswap config set
    autoswitch.enabled`` made in the terminal is never seen again.
    """
    source = SwapAccountSource(settings={})
    writes: list[bool] = []

    source._mirror_local_setting = lambda wanted: None
    source._stop_engine = lambda: None

    # First write fails -> latch engages (and the failure still raises).
    source._write_enabled = lambda wanted: (writes.append(wanted), False)[1]
    try:
        source.set_autoswitch_enabled(True)
    except RuntimeError:
        pass
    assert source._enabled_write_failed is True, "a failed write must engage the latch"

    # A later write succeeds -> the latch must release, so the file is trusted
    # again rather than being ignored for the rest of the process's life.
    source._write_enabled = lambda wanted: (writes.append(wanted), True)[1]
    source.set_autoswitch_enabled(False)
    assert source._enabled_write_failed is False, (
        "a successful write must release the latch, or settings.json is ignored forever"
    )
    assert writes == [True, False], writes


def test_rebuild_surfaces_a_failed_rollup_save_instead_of_clearing_it() -> None:
    """2026-08-26 gate: 'Rebuild cost index' hid a failed save from the user.

    The save was wrapped, logged, and then ``cost_error=None`` was published
    unconditionally — so the one failure that matters here (the emptied store
    never reached disk, while every scanner's offsets HAVE been reset) looked
    like a clean rebuild.
    """

    class _Rollups:
        def __init__(self) -> None:
            self.saved = 0

        def clear(self) -> None:
            return None

        def save(self) -> None:
            self.saved += 1
            raise OSError("disk full")

    class _Scanner:
        def __init__(self) -> None:
            self.reset_calls = 0

        def reset(self) -> None:
            self.reset_calls += 1

    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        rollups=_Rollups(),
    )
    scanner = _Scanner()
    worker._all_scanners = lambda: [("claude", scanner)]
    worker._rebuild_index()

    assert scanner.reset_calls == 1, "the rebuild must still reset the scanners"
    assert published, "nothing published"
    assert published[-1].cost_error, (
        "a failed rollup save must reach the user, not be logged and cleared"
    )
    assert "disk full" in published[-1].cost_error, published[-1].cost_error


def test_engine_unavailable_alert_is_raised_and_later_retracted() -> None:
    """2026-08-26 second gate, MUST: the engine-unavailable alert had no test.

    An engine that will not CONSTRUCT used to reach only ``last_error``, whose
    sole reader is a no-op once any account row exists — the toggle read ON
    while nothing switched. It now raises a standing alert; the untested half
    was the retraction, and an alert that outlives its cause is the exact bug
    this whole feature exists to end. Also pins the deliberate precedence: an
    engine that cannot start outranks a per-account quarantine.
    """
    source = SwapAccountSource(settings={"autoswitch_enabled": True})
    source.autoswitch_enabled = lambda: True
    source._persisted_quarantine_alert = lambda: ("account-quarantined", "Account-3 quarantined")

    source._ensure_engine = lambda: None  # construction fails
    assert source.evaluate_autoswitch() is None
    alert = source.current_alert()
    assert alert is not None and alert[0] == "error", alert
    assert alert[1].startswith("autoswitch engine unavailable: "), alert[1]

    # ...and it outranks the persisted quarantine while it stands.
    assert source.current_alert()[0] == "error"

    # Construction recovers: the alert must be retracted on the spot, not on
    # whichever later tick happens to emit a qualifying event.
    class _Engine:
        def tick(self) -> Any:
            return None

        def stop(self) -> None:
            return None

    source._ensure_engine = lambda: _Engine()
    source._next_tick_at = 0.0
    source.evaluate_autoswitch()
    assert source.current_alert() == ("account-quarantined", "Account-3 quarantined"), (
        "our own engine-unavailable alert must be retracted once it constructs, "
        "revealing the quarantine underneath"
    )


def test_reset_that_fails_to_write_stays_dirty() -> None:
    """2026-08-26 second gate: the durability fix missed its own reset path.

    ``Rebuild cost index`` empties rollups.json BEFORE resetting each vendor,
    so a failed write of the empty scan state leaves stale non-empty offsets
    against an emptied rollup — and ``_reconcile_lost_scan_state`` cannot
    catch it, because that guard keys on ``started_from_empty_state``. The
    result is a silent UNDER-count, the mirror of the double-count fixed in
    ``_save_states``.
    """
    for name, build in (
        (
            "Indexer",
            lambda root, saver: Indexer(
                projects_dir=root / "projects",
                state_path=root / "scan_state.json",
                pricing=DEFAULT_PRICING,
                state_saver=saver,
            ),
        ),
        (
            "CodexIndexer",
            lambda root, saver: CodexIndexer(
                sessions_dir=root / "sessions",
                state_path=root / "codex_scan_state.json",
                pricing=DEFAULT_PRICING,
                state_saver=saver,
            ),
        ),
    ):
        for returned, expect_dirty in ((False, True), (True, False), (None, False)):
            with tempfile.TemporaryDirectory() as tmp:
                indexer = build(Path(tmp), lambda _payload, _r=returned: _r)
                indexer.reset()
                assert indexer._states_dirty is expect_dirty, (
                    f"{name}.reset(): saver returned {returned!r} -> _states_dirty "
                    f"should be {expect_dirty}, got {indexer._states_dirty}"
                )


def test_worker_publishes_the_derived_state_seam_into_the_snapshot() -> None:
    """2026-08-26 pre-ship gate: the seam added by 62466f6 had NO test.

    Every existing test either hand-built a ``UiSnapshot`` or called the
    adapter directly, and the only test driving ``_run_accounts_job`` used a
    double with neither ``sentinels()`` nor ``current_alert()`` — so it covered
    the degrade path exclusively. A rename, a swapped kwarg or a broken
    translation in the wiring would have passed the whole suite.
    """

    class _Row:
        slot, alias, email, is_active = 2, "acme", "v@x.io", True
        five_hour_pct = seven_day_pct = None
        scoped_windows: tuple[Any, ...] = ()
        usage_age_seconds = None
        usage_is_stale = False
        switchable = True
        vendor = "claude"

    class _Source:
        """A source that actually implements the optional accessors."""

        def refresh(self, *, force: bool = False) -> None:
            return None

        def rows(self) -> tuple[Any, ...]:
            return (_Row(),)

        def active(self) -> Any:
            return _Row()

        def autoswitch_enabled(self) -> bool | None:
            return True

        def set_autoswitch_enabled(self, enabled: bool) -> None:
            return None

        def evaluate_autoswitch(self) -> str | None:
            return None

        def switch_to(self, slot_or_alias: str) -> bool:
            return False

        def sentinels(self) -> dict[int, str]:
            return {2: "re-login needed — refresh token dead; run: cswap add"}

        def sentinel_kinds(self) -> dict[int, str]:
            return {2: "re-login needed"}

        def current_alert(self) -> tuple[str, str]:
            return (ALERT_ALL_EXHAUSTED, "all accounts exhausted")

    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=_Source(),
    )
    worker._run_accounts_job(force=False)
    assert published, "nothing published"
    snapshot = published[-1]
    assert snapshot.account_notes == {2: "re-login needed — refresh token dead; run: cswap add"}
    assert snapshot.account_note_kinds == {2: "re-login needed"}
    assert snapshot.alert == (ALERT_ALL_EXHAUSTED, "all accounts exhausted")

    # ...and the renderers consume what the worker actually published, so the
    # adapter->snapshot->title path is covered end to end rather than in halves.
    app = app_mod.CCUsageWidgetApp()
    try:
        settings = dict(snapshot.settings)
        settings.update({"title_show_icon": False, "title_show_cost": False})
        painted = replace(snapshot, settings=settings)
        title = app.render_title(painted)
        assert "relogin" in title and "exhausted" in title, title
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_a_failed_scan_state_write_keeps_the_dirty_flag_set() -> None:
    """2026-08-26 pre-ship gate, CRITICAL: a failed save was marked clean.

    ``ScanStateStore.save_json`` never raises — it reports a failed write as
    ``False`` so the caller "can keep its dirty flag set and retry on the next
    tick". Both indexers' ``_state_saver`` branch discarded that bool and
    cleared ``_states_dirty`` unconditionally, so a transient disk failure left
    STALE offsets on disk while ``rollups.json`` had already committed (the
    rollup is written first and does raise). The next restart then re-read
    those bytes and re-credited their tokens — a silent, permanent double
    count of cost, invisible because ``last_save_error`` is read nowhere.

    Both direct-write fallbacks already gated on success; only the production
    hand-off did not. The bar: a save that returns False must NOT be treated
    as durable, and one that returns None (an older saver) must still work.
    """
    for name, build in (
        (
            "Indexer",
            lambda root, saver: Indexer(
                projects_dir=root / "projects",
                state_path=root / "scan_state.json",
                pricing=DEFAULT_PRICING,
                state_saver=saver,
            ),
        ),
        (
            "CodexIndexer",
            lambda root, saver: CodexIndexer(
                sessions_dir=root / "sessions",
                state_path=root / "codex_scan_state.json",
                pricing=DEFAULT_PRICING,
                state_saver=saver,
            ),
        ),
    ):
        for returned, expect_dirty in ((False, True), (True, False), (None, False)):
            with tempfile.TemporaryDirectory() as tmp:
                calls: list[Any] = []

                def _saver(payload: Any, _r: Any = returned) -> Any:
                    calls.append(payload)
                    return _r

                indexer = build(Path(tmp), _saver)
                indexer._states_dirty = True
                indexer._save_states()
                assert calls, f"{name}: the saver was never called"
                assert indexer._states_dirty is expect_dirty, (
                    f"{name}: saver returned {returned!r} -> _states_dirty should be "
                    f"{expect_dirty}, got {indexer._states_dirty}"
                )


def test_standing_alert_does_not_outlive_the_engine_that_made_it() -> None:
    """2026-08-26: the alert was stored on an event and never cleared, so
    turning Auto-switch OFF froze ``⛔ exhausted`` in the menu bar forever —
    the same "stale state rendered as live" bug the alert was added to end.
    Re-enabling must not resurrect the old verdict either.
    """

    class _Event:
        def __init__(self, kind: str, line: str) -> None:
            self.kind, self._line = kind, line

        def human(self) -> str:
            return self._line

    source = SwapAccountSource(settings={"autoswitch_enabled": True})
    source._persisted_quarantine_alert = lambda: None  # isolate from the real state file
    source.autoswitch_enabled = lambda: True
    source._on_engine_event(_Event("all-exhausted", "all accounts exhausted"))
    source._drain_events()
    assert source.current_alert() == ("all-exhausted", "all accounts exhausted")

    source.autoswitch_enabled = lambda: False
    source.evaluate_autoswitch()  # the worker's per-tick call; stops the engine
    assert source.current_alert() is None, "a stopped engine has no standing verdict"

    source.autoswitch_enabled = lambda: True
    assert source.current_alert() is None, "re-enabling must not resurrect the old verdict"


def test_a_quarantine_that_predates_the_widget_still_reaches_the_menu() -> None:
    """2026-08-26: ``QuarantineEvent`` fires only at the transition, and
    upstream filters an already-quarantined slot out of the candidate path
    before it could fire again — so a quarantine recorded before this process
    started produced no alert at all. It is read from the state file instead,
    which also means releasing it needs no explicit clear.
    """
    source = SwapAccountSource(settings={"autoswitch_enabled": True})
    source.autoswitch_enabled = lambda: True
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "autoswitch_state.json"
        source._autoswitch_state_path = lambda: state

        assert source.current_alert() is None, "no state file is not an alert"

        def _write(payload: dict[str, Any], stamp: float) -> None:
            # Explicit mtimes: the read is mtime-gated, and two writes inside
            # one filesystem timestamp tick would serve a stale cache.
            state.write_text(json.dumps(payload), encoding="utf-8")
            os.utime(state, (stamp, stamp))

        _write({"quarantine": {"3": {"email": "a@b.c", "reason": "invalid_grant"}}}, 1_000_000)
        alert = source.current_alert()
        assert alert is not None, "a persisted quarantine must reach the menu"
        kind, line = alert
        assert kind == "account-quarantined", kind
        assert "Account-3 (a@b.c)" in line and "invalid_grant" in line, line
        assert "--slot 3" in line, "the line must name the recovery command"

        source._alert = ("all-exhausted", "all accounts exhausted")
        assert source.current_alert()[0] == "all-exhausted", "a live verdict outranks the file"
        source._alert = None

        _write({"quarantine": {}}, 1_000_100)
        assert source.current_alert() is None, "release must clear it with no explicit step"

        state.write_text("{not json", encoding="utf-8")
        os.utime(state, (1_000_200, 1_000_200))
        assert source.current_alert() is None, "corrupt state must not take the menu down"


def test_a_test_defined_below_the_main_guard_is_reported_not_silently_dropped() -> None:
    """2026-08-25: a test appended after ``if __name__ == "__main__":`` is never
    defined before ``main()`` runs, so the direct runner dropped it with no
    error — just a smaller total. (Moving ``main()`` to the top does NOT fix
    this: it would run before any test is defined and collect zero.) The runner
    now diffs its collection against the source and fails loud.
    """
    source = "\n".join(
        [
            "def test_alpha() -> None: ...",
            "def main(): ...",
            'if __name__ == "__main__":',
            "    raise SystemExit(main())",
            "def test_orphan() -> None: ...",
        ]
    )
    collected = [("test_alpha", lambda: None)]
    assert _uncollected_tests(collected, source=source) == ["test_orphan"]
    assert _uncollected_tests(collected + [("test_orphan", lambda: None)], source=source) == []


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


def _uncollected_tests(
    collected: list[tuple[str, Any]], source: str | None = None
) -> list[str]:
    """Test names present in the SOURCE but absent from ``globals()``.

    ``main()`` runs from the ``__main__`` guard at the bottom of this file and
    raises ``SystemExit``, so anything defined textually below it never runs —
    a test appended there vanishes from the direct run with no error, only a
    smaller total (happened 2026-08-25). pytest is immune (it imports the
    module instead of executing the guard) and this venv has no pytest, so the
    direct runner has to police itself.
    """
    if source is None:
        try:
            source = Path(__file__).read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - the file is right there
            return []
    declared = re.findall(r"^def (test_\w+)", source, re.MULTILINE)
    found = {name for name, _ in collected}
    return [name for name in declared if name not in found]


def test_format_pct_reserves_100_for_true_limits() -> None:
    """2026-08-25 incident: 99.5-99.99% rendered as a lying "100%".

    The autoswitch engine's at-limit escape may legitimately land on an
    account whose window is at 99.7%; the menu then asserted "100% (!)" for
    the very account it had just switched to. "100%" now means >= 100.0.
    """
    from cc_usage_widget.contracts import format_pct

    assert format_pct(99.5) == "99%"
    assert format_pct(99.99) == "99%"
    assert format_pct(100.0) == "100%"
    assert format_pct(100.4) == "100%"
    assert format_pct(99.4) == "99%"
    assert format_pct(0.4) == "0%"
    assert format_pct(None) == "—"


def test_derived_usage_state_reaches_title_and_menu() -> None:
    """2026-08-25 incident: a quarantined active account rendered ``acme 0%``.

    claude-swap had flagged slot 2 "re-login needed" for 39 hours and the
    adapter computed that note every pass; nothing read it, so the title
    showed a day-old last-good 0% and looked like the healthiest account.
    The note must REPLACE the figure in the title and be printed verbatim in
    the menu (it names the remedy: ``cswap add``); a non-active slot in that
    state still marks the title; the engine's all-exhausted verdict is a
    title-level alert, not a log line.
    """
    from cc_usage_widget.contracts import AccountRow

    note = "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add"
    active = AccountRow(
        slot=2, alias="acme", email="acme@example.com", is_active=True,
        five_hour_pct=0.0, seven_day_pct=18.0, usage_age_seconds=139_000.0,
    )
    idle = AccountRow(
        slot=1, alias="main", email="main@example.com", is_active=False,
        five_hour_pct=47.0, seven_day_pct=78.0,
    )
    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    settings.update({"title_show_icon": False, "title_show_cost": False})
    app = app_mod.CCUsageWidgetApp()
    try:
        plain = UiSnapshot(settings=settings, accounts=(idle, active), active=active)
        assert "0%" in app.render_title(plain)

        flagged = replace(plain, account_notes={2: note})
        title = app.render_title(flagged)
        assert "relogin" in title and "0%" not in title, title
        app.rebuild_menu(flagged)
        texts = [str(getattr(item, "title", "")) for item in app.menu.values()]
        assert any("re-login needed" in t and "acme" in t for t in texts), texts

        other = replace(plain, account_notes={1: note})
        title = app.render_title(other)
        assert "⚠" in title and "0%" in title, title

        exhausted = replace(
            plain,
            alert=("all-exhausted", "all accounts exhausted; earliest reset 2026-08-27T21:00:00Z"),
        )
        assert "exhausted" in app.render_title(exhausted)
        app.rebuild_menu(exhausted)
        texts = [str(getattr(item, "title", "")) for item in app.menu.values()]
        assert any("all accounts exhausted" in t for t in texts), texts
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_autoswitch_alert_tracks_the_engines_latest_verdict() -> None:
    """``current_alert()`` stands while the fleet is dry and clears when the
    engine next finds a way forward — so the bar shows a state, not an echo."""

    class _Event:
        def __init__(self, kind: str, line: str) -> None:
            self.kind, self._line = kind, line

        def human(self) -> str:
            return self._line

    source = SwapAccountSource(settings={})
    # Preconditions made explicit (2026-08-26): a verdict is only readable
    # while the engine is running, and the file fallback is a separate test.
    source.autoswitch_enabled = lambda: True
    source._persisted_quarantine_alert = lambda: None
    source._on_engine_event(_Event("all-exhausted", "all accounts exhausted"))
    source._drain_events()
    assert source.current_alert() == ("all-exhausted", "all accounts exhausted")
    source._on_engine_event(_Event("poll", "poll"))
    source._drain_events()
    assert source.current_alert() is not None, "a poll carries no verdict"
    source._on_engine_event(_Event("no-switch", "below-threshold"))
    source._drain_events()
    assert source.current_alert() is None


def test_all_exhausted_warning_dedupes_across_timestamp_jitter() -> None:
    """One episode must WARN once, not every ~10-minute blocked tick.

    The engine's all-exhausted line embeds a reset instant whose sub-second
    part jitters — and which flipped the whole second/minute/hour between
    emissions of the SAME episode (…T20:59:59.700962Z vs …T21:00:00.132963Z,
    observed live 2026-08-26). Keying repeat detection on the raw line
    therefore never matched and the WARNING fired on every tick.
    """
    from cc_usage_widget.accounts import _episode_key

    a = "all accounts exhausted; earliest reset 2026-08-27T21:00:00.132963Z"
    b = "all accounts exhausted; earliest reset 2026-08-27T20:59:59.700962Z"
    c = "all accounts exhausted; earliest reset 2026-08-27T20:59:59.732573Z"
    assert _episode_key(a) == _episode_key(b) == _episode_key(c)
    # A genuinely different episode (new reset date) stays distinguishable.
    d = "all accounts exhausted; earliest reset 2026-08-28T21:00:00.000001Z"
    assert _episode_key(d) != _episode_key(a)
    # Lines without a timestamp are returned unchanged.
    assert _episode_key("quarantined slot 2") == "quarantined slot 2"


def test_unswitchable_accounts_are_not_clickable() -> None:
    """A slot claude-swap cannot activate must not render as a live row.

    ``AccountRow.switchable`` is the only thing that makes a row clickable,
    and ``_build_row`` never populated it: every account rendered clickable,
    including one whose stored credentials or config backup are missing, so
    the click was guaranteed to fail. Upstream's flag means "activatable
    without re-adding the account" — exactly the click precondition.
    """

    class _Acct:
        def __init__(self, number: int, switchable: object) -> None:
            self.number = number
            self.email = f"a{number}@example.com"
            self.is_active = False
            self.usage = None
            if switchable is not _MISSING:
                self.switchable = switchable

    source = SwapAccountSource(settings=None)
    unswitchable = source._build_row(_Acct(1, False), None, 0.0, None)
    switchable = source._build_row(_Acct(2, True), None, 0.0, None)
    absent = source._build_row(_Acct(3, _MISSING), None, 0.0, None)

    assert unswitchable.switchable is False
    assert switchable.switchable is True
    # Fail OPEN when upstream drops the attribute: a missing field must never
    # silently disable manual switching for every account.
    assert absent.switchable is True



# ---------------------------------------------------------------------------
# W1 forensics: an account switch nobody in this widget made (2026-09-01)
# ---------------------------------------------------------------------------


class _CapturedLog:
    """Collects records from the accounts logger for the duration of a with."""

    def __init__(self, level: int = logging.WARNING) -> None:
        self._level = level
        self.records: list[logging.LogRecord] = []
        self._handler: logging.Handler | None = None

    def __enter__(self) -> "_CapturedLog":
        outer = self

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= outer._level:
                    outer.records.append(record)

        self._handler = _Handler()
        accounts_mod.LOGGER.addHandler(self._handler)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._handler is not None:
            accounts_mod.LOGGER.removeHandler(self._handler)

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


class _FakeSnapshot:
    def __init__(self, active_number: str) -> None:
        self.active_number = active_number
        self.accounts: tuple[Any, ...] = ()


class _FakeSwitcher:
    def __init__(self, snapshot: _FakeSnapshot) -> None:
        self._snapshot = snapshot
        self.calls: list[str] = []
        self.result: dict[str, Any] = {"switched": True}

    def switch_to(self, identifier: str, json_output: bool = False) -> dict[str, Any]:
        self.calls.append(identifier)
        # A real switch moves the live login; that is what the next snapshot
        # pass reads back, and what the detector must NOT call external.
        self._snapshot.active_number = str(identifier)
        return self.result


class _FakeSnapshotSource:
    def __init__(self, snapshot: _FakeSnapshot) -> None:
        self._snapshot = snapshot

    def take(self, full: bool = False, store_only: bool = False) -> _FakeSnapshot:
        return self._snapshot


class _FakeBackend:
    """The two backend attributes ``refresh``/``switch_to`` actually touch."""

    def __init__(self, active_number: str) -> None:
        self.snapshot = _FakeSnapshot(active_number)
        self.snapshot_source = _FakeSnapshotSource(self.snapshot)
        self.switcher = _FakeSwitcher(self.snapshot)


class _EngineEvent:
    def __init__(self, kind: str, line: str, **extra: Any) -> None:
        self.kind, self._line = kind, line
        for name, value in extra.items():
            setattr(self, name, value)

    def human(self) -> str:
        return self._line


def _forensics_source(active: str = "5") -> tuple[Any, _FakeBackend]:
    """A source wired to a fake backend, primed with *active* as the live slot."""
    source = SwapAccountSource(settings={"autoswitch_enabled": True})
    source.autoswitch_enabled = lambda: True
    source._persisted_quarantine_alert = lambda: None
    backend = _FakeBackend(active)
    source._backend_or_none = lambda: backend
    source.refresh(force=True)  # first pass: learn the active slot, say nothing
    return source, backend


def test_a_switch_the_widget_did_not_make_is_detected_and_named() -> None:
    """2026-09-01: three ghost flips, no line anywhere.

    claude-swap re-derives the active login from ``~/.claude.json`` on every
    pass, so an external switch (a rival ``cswap`` actor, a ``/login``, or a
    running session refreshing its own token back into the shared default) IS
    visible to the widget — but nothing compared two passes, so the title
    silently changed alias and the engine re-switched 7 s later. It must now
    produce a WARNING, an event-log line and a standing verdict.

    The first pass must stay silent: with no previous active slot the widget
    cannot say who moved it, and guessing would be the same lie in reverse.
    """
    source, backend = _forensics_source("5")
    assert source.current_alert() is None, "the first pass has nothing to compare"
    assert source.recent_events() == (), source.recent_events()

    backend.snapshot.active_number = "1"
    with _CapturedLog() as captured:
        source.refresh(force=True)

    alert = source.current_alert()
    assert alert is not None, "an external switch must reach the menu bar"
    kind, line = alert
    assert kind == ALERT_EXTERNAL_SWITCH, kind
    assert "active 5→1 (external)" in line, line
    assert any("outside the widget" in m for m in captured.messages()), captured.messages()
    assert source.recent_events()[-1] == line, source.recent_events()

    # The verdict reaches the menu bar as its own glyph, not a generic one.
    assert app_mod._title_alert(kind) == "⚠ ext"

    # It stands while the rival keeps the wheel: a later quiet pass must not
    # clear it (that is the bug this replaces, not a state to reintroduce).
    source.refresh(force=True)
    assert source.current_alert() == alert, "a quiet pass must not retract it"


def test_our_own_engine_and_menu_switches_are_not_reported_as_external() -> None:
    """The detector is only worth having if it never cries wolf.

    Both widget-made paths land the active on a new slot exactly like a rival
    would; each must claim the change first — the engine from the ``switch``
    event's ``to_ref``, the menu click from the target it asked for. A false
    "another actor is driving" would train the operator to ignore the real one.
    """
    # (a) engine switch
    source, backend = _forensics_source("5")
    source._on_engine_event(
        _EngineEvent(
            "switch",
            "Switched Account-5 -> Account-1 (a@b.c) (at-limit)",
            to_ref={"number": "1", "email": "a@b.c"},
        )
    )
    backend.snapshot.active_number = "1"
    source._drain_events()
    source.refresh(force=True)
    assert source.current_alert() is None, source.current_alert()
    assert not any("external" in line for line in source.recent_events()), source.recent_events()
    assert any("Switched Account-5" in line for line in source.recent_events())

    # (b) manual switch from the menu
    source, backend = _forensics_source("5")
    assert source.switch_to("1") is True
    assert backend.switcher.calls == ["1"], backend.switcher.calls
    assert source.current_alert() is None, source.current_alert()
    assert any("manual → 1" in line for line in source.recent_events()), source.recent_events()

    # ...and taking the wheel back retracts a standing external verdict.
    backend.snapshot.active_number = "3"
    source.refresh(force=True)
    assert source.current_alert()[0] == ALERT_EXTERNAL_SWITCH
    source.switch_to("2")
    assert source.current_alert() is None, "a widget switch clears the external verdict"


def test_an_expectation_is_consumed_by_exactly_one_pass() -> None:
    """A claim that outlives its pass would swallow the NEXT external switch.

    A manual switch to slot 1 that then bounces back (the token-refresh race
    that caused tonight's flips) must be reported the second time round.
    """
    source, backend = _forensics_source("5")
    source.switch_to("1")
    assert source.current_alert() is None
    # Same destination, this time nobody asked for it.
    backend.snapshot.active_number = "5"
    source.refresh(force=True)
    backend.snapshot.active_number = "1"
    source.refresh(force=True)
    alert = source.current_alert()
    assert alert is not None and alert[0] == ALERT_EXTERNAL_SWITCH, alert
    # Assert on the journal, not the standing verdict: the 1→5 flip already
    # raised one, so only the LINE COUNT proves the 5→1 flip was not swallowed
    # by a claim that outlived its pass (review 2026-09-01: the old assertion
    # passed with the expectation never consumed).
    external = [line for line in source.recent_events() if "(external)" in line]
    assert len(external) == 2, external
    assert external[-1].endswith("active 5\u21921 (external)"), external


def test_no_viable_target_is_a_standing_verdict_not_a_debug_line() -> None:
    """"Nowhere to go" used to CLEAR the alert and log at DEBUG.

    ``NoSwitchEvent(reason="no-viable-target")`` took the same branch as
    "below-threshold", so the one tick that proves the fleet is stuck also
    erased the ``⛔ exhausted`` line the operator was reading — a C9-class
    invisible state. Every other reason must keep clearing, as before.
    """
    source, _ = _forensics_source("5")
    with _CapturedLog() as captured:
        source._on_engine_event(
            _EngineEvent("no-switch", "no switch: no-viable-target", reason="no-viable-target")
        )
        source._drain_events()
    alert = source.current_alert()
    assert alert is not None and alert[0] == ALERT_NO_TARGET, alert
    assert app_mod._title_alert(alert[0]) == "⚠ no target"
    assert any("no-viable-target" in m for m in captured.messages()), captured.messages()

    # Repeats of the SAME episode stay out of the log (the engine re-emits it
    # every blocked tick) but the verdict keeps standing.
    with _CapturedLog() as repeat:
        source._on_engine_event(
            _EngineEvent("no-switch", "no switch: no-viable-target", reason="no-viable-target")
        )
        source._drain_events()
    assert repeat.messages() == [], repeat.messages()
    assert source.current_alert()[0] == ALERT_NO_TARGET

    # An ordinary no-switch still means "nothing to escalate".
    source._on_engine_event(
        _EngineEvent("no-switch", "no switch: below-threshold", reason="below-threshold")
    )
    source._drain_events()
    assert source.current_alert() is None, source.current_alert()


def test_poll_and_sleep_events_do_not_evict_the_switch_history() -> None:
    """The recent-switches block would otherwise be all polls.

    The engine emits a poll or sleep on nearly every tick and the event log is a
    20-slot deque, so keeping them means a switch line survives ~20 minutes and
    the menu block never shows a switch. They stay in the DEBUG log.
    """
    source, _ = _forensics_source("5")
    source._on_engine_event(
        _EngineEvent("switch", "Switched Account-5 -> Account-1 (a@b.c) (at-limit)")
    )
    source._drain_events()
    for index in range(30):
        source._on_engine_event(_EngineEvent("poll", f"poll {index}"))
        source._on_engine_event(_EngineEvent("sleep", f"sleep {index}"))
    source._drain_events()
    events = source.recent_events()
    assert any("Switched Account-5" in line for line in events), events
    assert not any(line.endswith("poll 29") for line in events), events


def test_the_switch_history_carries_a_local_clock_the_operator_can_read() -> None:
    """Every forensic line is stamped ``HH:MM`` local, and the menu shows it."""
    source, _ = _forensics_source("5")
    source._on_engine_event(
        _EngineEvent("switch", "Switched Account-5 -> Account-1 (a@b.c) (at-limit)")
    )
    source._drain_events()
    line = source.recent_events()[-1]
    assert re.match(r"^\d{2}:\d{2} Switched Account-5", line), line

    app = app_mod.CCUsageWidgetApp()
    try:
        settings = normalize_settings(dict(SETTINGS_DEFAULTS))
        snapshot = UiSnapshot(settings=settings, recent_events=(line,))
        app.rebuild_menu(snapshot)
        texts = [str(getattr(item, "title", "")) for item in app.menu.values()]
        assert any(t.strip() == "Recent switches" for t in texts), texts
        assert any("Switched Account-5" in t for t in texts), texts
        # A machine that has seen nothing renders exactly the old menu.
        app.rebuild_menu(UiSnapshot(settings=settings))
        texts = [str(getattr(item, "title", "")) for item in app.menu.values()]
        assert not any("Recent switches" in t for t in texts), texts
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_exhausted_line_shows_a_time_the_operator_can_act_on() -> None:
    """Upstream writes the earliest reset as an ISO-UTC instant.

    ``earliest reset 2026-08-27T21:00:00Z`` is the one number that matters and
    the one the operator has to convert in their head — seven hours out here.
    It is re-expressed on the local clock; anything unparseable stays verbatim
    rather than becoming a guess.
    """
    raw = "all accounts exhausted; earliest reset 2026-08-27T21:00:00Z"
    local = (
        dt.datetime(2026, 8, 27, 21, 0, tzinfo=dt.timezone.utc).astimezone().strftime("%H:%M")
    )
    assert app_mod._localize_instants(raw).endswith(f"earliest reset {local}")
    assert "2026-08-27T21:00:00Z" not in app_mod._localize_instants(raw)
    assert app_mod._localize_instants("earliest reset 2026-13-99T99:99:99Z") == (
        "earliest reset 2026-13-99T99:99:99Z"
    )

    app = app_mod.CCUsageWidgetApp()
    try:
        settings = normalize_settings(dict(SETTINGS_DEFAULTS))
        app.rebuild_menu(
            UiSnapshot(settings=settings, alert=(ALERT_ALL_EXHAUSTED, raw))
        )
        texts = [str(getattr(item, "title", "")) for item in app.menu.values()]
        assert any(f"earliest reset {local}" in t for t in texts), texts
        assert not any("21:00:00Z" in t for t in texts), texts
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_last_switch_line_is_read_from_shared_state_and_never_guessed() -> None:
    """``last switch HH:MM (trigger) · cooldown Nm left`` — or no line at all.

    The file is claude-swap's, so it also describes a switch made by ``cswap``
    or the TUI. Each part is dropped rather than invented: no ``lastSwitchAt``
    means no line, no ``leftTrigger`` means no reason, and no running engine
    means no cooldown clause (nothing is enforcing one).
    """
    source = SwapAccountSource(settings={"autoswitch_enabled": True})
    source.autoswitch_enabled = lambda: True
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "autoswitch_state.json"
        source._autoswitch_state_path = lambda: state
        assert source.switch_note() is None, "no state file is not a line"

        def _write(payload: dict[str, Any], stamp: float) -> None:
            state.write_text(json.dumps(payload), encoding="utf-8")
            os.utime(state, (stamp, stamp))

        _write({"schemaVersion": 1}, 1_000_000)
        assert source.switch_note() is None, "no lastSwitchAt is not a line"

        when = time.time() - 60.0
        expected = time.strftime("%H:%M", time.localtime(when))
        _write({"lastSwitchAt": when, "lastSwitchTo": "5"}, 1_000_100)
        assert source.switch_note() == f"last switch {expected}", source.switch_note()

        _write({"lastSwitchAt": when, "leftTrigger": "at-limit"}, 1_000_200)
        assert source.switch_note() == f"last switch {expected} (at-limit)"

        class _Policy:
            cooldown_seconds = 300.0

        class _Engine:
            settings = _Policy()

        source._engine = _Engine()
        note = source.switch_note()
        assert note is not None and note.startswith(f"last switch {expected} (at-limit) · cooldown ")
        assert note.endswith("m left"), note

        _write({"lastSwitchAt": "not a number"}, 1_000_300)
        assert source.switch_note() is None, "a corrupt value must not become a guess"

    # ...and it reaches the menu right under the header.
    app = app_mod.CCUsageWidgetApp()
    try:
        settings = normalize_settings(dict(SETTINGS_DEFAULTS))
        app.rebuild_menu(
            UiSnapshot(settings=settings, switch_note="last switch 21:05 (at-limit)")
        )
        texts = [str(getattr(item, "title", "")) for item in app.menu.values()]
        assert any("last switch 21:05 (at-limit)" in t for t in texts), texts
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_worker_publishes_the_forensic_seam_into_the_snapshot() -> None:
    """The adapter computed all of this before; nothing read it (G2).

    A field added to ``UiSnapshot`` and never populated by ``_run_accounts_job``
    is the same silent gap as the deque with no reader, so the wiring gets its
    own test rather than being implied by the renderers'.
    """

    class _Source:
        def refresh(self, *, force: bool = False) -> None:
            return None

        def rows(self) -> tuple[Any, ...]:
            return ()

        def active(self) -> Any:
            return None

        def autoswitch_enabled(self) -> bool | None:
            return False

        def evaluate_autoswitch(self) -> str | None:
            return None

        def recent_events(self) -> tuple[str, ...]:
            return ("21:05 active 5→1 (external)",)

        def switch_note(self) -> str:
            return "last switch 21:05 (at-limit) · cooldown 4m left"

    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=_Source(),
    )
    worker._run_accounts_job(force=False)
    assert published, "nothing published"
    assert published[-1].recent_events == ("21:05 active 5→1 (external)",)
    assert published[-1].switch_note == "last switch 21:05 (at-limit) · cooldown 4m left"

_MISSING = object()


def _fleet_rows(*specs: tuple[int, str, float | None, str | None]) -> tuple[Any, ...]:
    """``(slot, alias, five_hour_pct, five_hour_resets_at)`` -> account rows."""
    from cc_usage_widget.contracts import AccountRow

    return tuple(
        AccountRow(
            slot=slot,
            alias=alias,
            email=f"{alias}@example.com",
            is_active=slot == 1,
            five_hour_pct=pct,
            five_hour_resets_at=resets,
        )
        for slot, alias, pct, resets in specs
    )


def _fleet_settings(**overrides: Any) -> dict[str, Any]:
    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    settings.update({"title_show_icon": False, "title_show_cost": False})
    settings.update(overrides)
    return settings


def test_title_shows_fleet_headroom_only_when_the_room_is_full() -> None:
    """2026-09-01: the wall read ``main 100% ⛔ exhausted`` and stopped there.

    Three other accounts existed and one of them reset at midnight; nothing in
    the title said whether any of them had room, so "exhausted" read as "the
    whole fleet is gone" and the operator had to open the menu to find out. The
    suffix answers exactly that, and ONLY while the answer matters: a healthy
    active account renders the SPEC 4.1 title unchanged, and the component is
    toggleable like every other one.
    """
    rows = _fleet_rows(
        (1, "main", 100.0, "00:00"),
        (2, "podol", 100.0, None),
        (3, "acme", 100.0, "Sep 2 04:00"),
        (4, "ops", 100.0, None),
    )
    settings = _fleet_settings()
    app = app_mod.CCUsageWidgetApp()
    try:
        exhausted = UiSnapshot(
            settings=settings,
            accounts=rows,
            active=rows[0],
            alert=(ALERT_ALL_EXHAUSTED, "all accounts exhausted; earliest reset ..."),
        )
        assert app.render_title(exhausted) == "main 100%(!) ⛔ exhausted 0/4 · next 00:00"

        # Toggled off: the pre-2026-09-01 title, byte for byte.
        off = replace(exhausted, settings=_fleet_settings(title_show_fleet=False))
        assert app.render_title(off) == "main 100%(!) ⛔ exhausted"

        # Healthy fleet: nothing appended at all, no matter how many accounts.
        healthy_rows = _fleet_rows(
            (1, "main", 40.0, "00:00"),
            (2, "podol", 12.0, None),
        )
        healthy = UiSnapshot(
            settings=settings, accounts=healthy_rows, active=healthy_rows[0]
        )
        assert app.render_title(healthy) == "main 40%"

        # At/over the threshold WITHOUT an alert is also a decision point, and
        # the threshold is claude-swap's, not a number of ours: at 90% the
        # default 85 fires and a policy of 95 does not.
        near_rows = _fleet_rows(
            (1, "main", 90.0, "00:00"),
            (2, "podol", 12.0, None),
        )
        near = UiSnapshot(settings=settings, accounts=near_rows, active=near_rows[0])
        assert app.render_title(near) == "main 90% 1/2 · next 00:00"
        assert app.render_title(replace(near, autoswitch_threshold=95.0)) == "main 90%"

        # The threshold is a BOUNDARY, and both sides of it are load-bearing:
        # an active account exactly AT it is at limit (that is when autoswitch
        # fires), and another account exactly AT it is not room (autoswitch
        # would not pick it either).
        at_rows = _fleet_rows((1, "main", 85.0, "01:00"), (2, "podol", 12.0, None))
        at = UiSnapshot(settings=settings, accounts=at_rows, active=at_rows[0])
        assert app.render_title(at) == "main 85% 1/2 · next 01:00"

        peer_at_rows = _fleet_rows(
            (1, "main", 100.0, "00:00"),
            (2, "podol", 85.0, None),
        )
        peer_at = UiSnapshot(
            settings=settings, accounts=peer_at_rows, active=peer_at_rows[0]
        )
        assert app.render_title(peer_at) == "main 100%(!) 0/2 · next 00:00"

        # ...and the fallback really is 85, not "some number below 90". At 60%
        # a fleet is not a decision point and the title must not say anything;
        # this is what pins _TITLE_FLEET_THRESHOLD_DEFAULT to claude-swap's
        # documented default rather than to any value that happens to be low.
        mid_rows = _fleet_rows((1, "main", 60.0, "00:00"), (2, "podol", 12.0, None))
        mid = UiSnapshot(settings=settings, accounts=mid_rows, active=mid_rows[0])
        assert mid.autoswitch_threshold is None
        assert app.render_title(mid) == "main 60%"

        # A slot whose 5h window the API did not report is NOT counted as room.
        unknown_rows = _fleet_rows(
            (1, "main", 100.0, "00:00"),
            (2, "podol", None, None),
        )
        unknown = UiSnapshot(
            settings=settings, accounts=unknown_rows, active=unknown_rows[0]
        )
        assert app.render_title(unknown) == "main 100%(!) 0/2 · next 00:00"

        # A read-only pseudo-account (Codex) is not a room to switch into.
        pseudo = replace(_fleet_rows((0, "Codex", 10.0, None))[0], switchable=False)
        with_pseudo = replace(exhausted, accounts=rows + (pseudo,))
        assert app._title_fleet(with_pseudo, now_minutes=0) == "0/4 · next 00:00"
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_fleet_next_reset_is_verbatim_soonest_and_sheds_under_budget() -> None:
    """The ``next`` half is one row's own reset string, reprinted as-is.

    Ordering is the only computation: "soonest" wraps through midnight, so a
    23:50 reset is ahead of a 00:20 one at 23:40 and behind it at 23:55. A
    reset claude-swap rendered as a DATE is not a next-room candidate (we will
    not invent the day), and when no candidate has a usable clock the ``next``
    half is omitted rather than guessed. Finally the suffix pays for a title
    that has already overshot the menu-bar budget: ``· next`` goes first.
    """
    app = app_mod.CCUsageWidgetApp()
    try:
        rows = _fleet_rows((1, "a", 100.0, "23:50"), (2, "b", 100.0, "00:20"))
        snap = UiSnapshot(settings=_fleet_settings(), accounts=rows, active=rows[0])
        assert app._title_fleet(snap, now_minutes=23 * 60 + 40) == "0/2 · next 23:50"
        assert app._title_fleet(snap, now_minutes=23 * 60 + 55) == "0/2 · next 00:20"

        dated = _fleet_rows((1, "a", 100.0, "Sep 2 04:00"), (2, "b", 100.0, ""))
        no_clock = UiSnapshot(
            settings=_fleet_settings(), accounts=dated, active=dated[0]
        )
        assert app._title_fleet(no_clock, now_minutes=0) == "0/2"

        # Budget: the count survives an overshoot the `next` half cannot.
        assert app._title_fleet(snap, base="x" * 28, now_minutes=0) == "0/2 · next 00:20"
        assert app._title_fleet(snap, base="x" * 34, now_minutes=0) == "0/2"
        assert app._title_fleet(snap, base="x" * 60, now_minutes=0) == ""

        # ...and it survives the SAME overshoot when there was no `next` to
        # shed. Charging the realised tail inverted the order here: with an
        # empty tail any overshoot at all dropped the count, so the title with
        # LESS to show was the one that showed nothing (2026-09-01 review).
        for base_len in (30, 34):
            base = "x" * base_len
            assert app._title_fleet(no_clock, base=base, now_minutes=0) == "0/2"
            assert app._title_fleet(snap, base=base, now_minutes=0) == "0/2"
        assert app._title_fleet(no_clock, base="x" * 60, now_minutes=0) == ""
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_fleet_count_never_offers_a_slot_whose_figures_are_not_trusted() -> None:
    """A slot with a derived-usage note is NOT a free room.

    2026-08-25: a quarantined account showed ``acme 0%`` for 39 hours — the
    stored percentage is a last-good value that can be days old and reads as
    healthy, which is why a note REPLACES that slot's figures everywhere else
    in the title. The first cut of the fleet suffix counted the same forbidden
    number as headroom: ``⛔ exhausted 1/4`` advertised one free room, at the
    moment the operator was deciding whether to keep working, and the room was
    a dead login. That is strictly worse than the old title, which said
    nothing. The note's reset is excluded for the same reason: it comes off
    the same untrusted read.
    """
    app = app_mod.CCUsageWidgetApp()
    try:
        rows = _fleet_rows((1, "main", 100.0, "00:00"), (2, "podol", 0.0, "02:00"))
        note = "re-login needed — refresh token dead; run: cswap add"
        snap = UiSnapshot(
            settings=_fleet_settings(),
            accounts=rows,
            active=rows[0],
            account_notes={2: note},
            alert=(ALERT_ALL_EXHAUSTED, "all accounts exhausted"),
        )
        # 0/2, not 1/2 — and the slot stays in M: it exists, it is not room.
        assert app.render_title(snap) == "main 100%(!) ⛔ exhausted 0/2 · next 00:00"

        # Its reset is not a candidate for `next` either: with the only other
        # row's clock unknown, the half is omitted rather than borrowed.
        borrowed = replace(
            snap, accounts=_fleet_rows((1, "main", 100.0, None), (2, "p", 100.0, "02:00"))
        )
        assert app._title_fleet(borrowed, now_minutes=0) == "0/2"

        # Control: drop the note and the very same rows DO offer the room.
        trusted = replace(snap, account_notes={})
        assert app.render_title(trusted) == "main 100%(!) ⛔ exhausted 1/2 · next 00:00"
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_fleet_alert_kinds_are_the_shared_constants_not_literals() -> None:
    """The no-target trigger crosses a module seam; pin it there.

    Half of :data:`_TITLE_FLEET_ALERT_KINDS` was a raw ``"no-target"``
    literal while the adapter that raises the verdict lives in another module.
    If the two ever spelled it differently the branch would go silently dead —
    no failing test, no log line, the feature quietly down to one of its two
    named triggers. That is the exact failure ``ALERT_KINDS`` was created to
    end (2026-08-26, five literals across two modules).
    """
    kinds = set(app_mod._TITLE_FLEET_ALERT_KINDS)
    assert kinds <= set(ALERT_KINDS), kinds - set(ALERT_KINDS)
    assert ALERT_NO_TARGET in kinds

    app = app_mod.CCUsageWidgetApp()
    try:
        rows = _fleet_rows((1, "main", 12.0, "00:00"), (2, "podol", 40.0, None))
        # Well under the threshold: the suffix is here only because the engine
        # said it has nowhere to go.
        snap = UiSnapshot(
            settings=_fleet_settings(),
            accounts=rows,
            active=rows[0],
            alert=(ALERT_NO_TARGET, "no viable target"),
        )
        title = app.render_title(snap)
        assert title.endswith("2/2"), title
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_fleet_suffix_never_becomes_the_whole_title() -> None:
    """Icon-only stays icon-only: this is a suffix, not a title.

    With every text component off the status item is deliberately image-only
    (RCA 2026-08-17: an ~33pt item fits a saturated menu bar without evicting
    a neighbour). The first cut let the fleet suffix fire anyway, so an
    at-limit account rendered a naked ``1/2 · next 00:00`` — no alias, no
    glyph, unreadable at the wall, and the icon-only path defeated. The alert
    block is the one standing-problem exception and it is a glyph.
    """
    app = app_mod.CCUsageWidgetApp()
    try:
        rows = _fleet_rows((1, "main", 90.0, "00:00"), (2, "podol", 12.0, None))
        silent = _fleet_settings(
            title_show_icon=False,
            title_show_alias=False,
            title_show_five_hour_pct=False,
            title_show_scoped_pct=False,
            title_show_cost=False,
        )
        snap = UiSnapshot(settings=silent, accounts=rows, active=rows[0])
        app._icon_image_set = True
        assert app.render_title(snap) == ""

        # The glyph fallback (no NSImage) is unchanged, and still not a place
        # to hang a room count.
        app._icon_image_set = False
        assert app.render_title(snap) == app_mod.TITLE_ICON

        # Control: give it one text part back and the suffix attaches to it.
        with_alias = replace(snap, settings=_fleet_settings(title_show_alias=True))
        assert app.render_title(with_alias) == "main 90% 1/2 · next 00:00"
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_autoswitch_threshold_reaches_the_snapshot_without_a_per_tick_read() -> None:
    """The count buckets on the engine's own threshold, not on a constant.

    The field existed and nothing assigned it, so the suffix always fell back
    to 85 and its docstring's promise — that "1/4 room" cannot disagree with
    what autoswitch will do — was false: with ``autoswitch.threshold 95`` the
    title claimed a full room the engine considered fine. Wiring it must NOT
    cost a file read per tick (SPEC 2.1): ``autoswitch_threshold()`` calls
    ``load_policy`` unconditionally, so the publish path reads the value the
    mtime-gated ``_ensure_engine`` already cached.
    """

    class _Policy:
        threshold = 95.0
        interval_seconds = 60

    class _Engine:
        def tick(self) -> Any:
            return None

        def stop(self) -> None:
            return None

    class _FakeBackend:
        switcher = object()
        backup_dir = Path("/nonexistent")

        @staticmethod
        def load_policy(_dir: Any) -> Any:
            return _Policy()

        @staticmethod
        def engine_cls(*_args: Any, **_kwargs: Any) -> Any:
            return _Engine()

    source = SwapAccountSource(settings={"autoswitch_enabled": True})
    source._backend_or_none = lambda: _FakeBackend()  # type: ignore[assignment]
    source._policy_path = lambda: None  # type: ignore[assignment]

    assert source.cached_autoswitch_threshold() is None, "guessed before reading"
    assert source._ensure_engine() is not None
    assert source.cached_autoswitch_threshold() == 95.0

    # The publish path uses the cache. A source whose ungated accessor blows up
    # must still publish the threshold — proof the tick does not call it.
    live_rows = _fleet_rows((1, "main", 90.0, "00:00"), (2, "podol", 12.0, None))

    class _Source:
        def refresh(self, *, force: bool = False) -> None:
            return None

        def rows(self) -> tuple[Any, ...]:
            return live_rows

        def active(self) -> Any:
            return live_rows[0]

        def autoswitch_enabled(self) -> bool | None:
            return False

        def set_autoswitch_enabled(self, enabled: bool) -> None:
            return None

        def evaluate_autoswitch(self) -> str | None:
            return None

        def switch_to(self, slot_or_alias: str) -> bool:
            return False

        def cached_autoswitch_threshold(self) -> float:
            return 95.0

        def autoswitch_threshold(self) -> float:
            raise AssertionError("per-tick load_policy read (SPEC 2.1)")

    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=_Source(),
    )
    worker._run_accounts_job(force=False)
    assert published, "nothing published"
    assert published[-1].autoswitch_threshold == 95.0

    # ...and the renderer honours it: 90% is at limit under 85, not under 95.
    app = app_mod.CCUsageWidgetApp()
    try:
        painted = replace(published[-1], settings=_fleet_settings())
        assert app.render_title(painted) == "main 90%"
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# switch-ux: an informed manual switch, and one click that picks for you
# ---------------------------------------------------------------------------


def _switch_row(
    slot: int,
    alias: str,
    *,
    five: float | None,
    seven: float | None = None,
    reset: str | None = None,
    scoped: tuple[tuple[str, float], ...] = (),
    active: bool = False,
    age: float | None = None,
) -> Any:
    from cc_usage_widget.contracts import AccountRow

    return AccountRow(
        slot=slot,
        alias=alias,
        email=f"{alias}@example.com",
        is_active=active,
        five_hour_pct=five,
        seven_day_pct=seven,
        scoped_windows=scoped,
        five_hour_resets_at=reset,
        usage_age_seconds=age,
    )


def test_switch_submenu_carries_what_to_choose_by() -> None:
    """2026-09-01: all four accounts read 5h 100% and the submenu said only
    ``3 podol   5h 100%`` for each of them.

    The staggered resets and the weekly/scoped windows WERE the decision, and
    none of them were on screen. The row now carries the reset and every
    window the API reported, marks an at-limit row, and stays clickable
    (a manual switch is an operator override by design).
    """
    row = _switch_row(
        3, "podol", five=100.0, seven=20.0, reset="00:00", scoped=(("Fable", 40.0),)
    )
    label = app_mod._switch_target_label(row, name_width=5)
    assert label == (
        "3 podol 5h 100% (!) ↺00:00 · 7d 20% · Fable 40% (at limit)"
    ), label

    # A window the API did not report is an em dash, never a fabricated 0%.
    thin = app_mod._switch_target_label(_switch_row(4, "acme", five=None), name_width=4)
    assert "5h —" in thin and "7d —" in thin, thin
    assert "at limit" not in thin, thin

    # An exhausted WEEKLY window is marked too. The Accounts section marks
    # every window `(!)`; a submenu that marked only the 5-hour one presented
    # a 5h 10% / 7d 100% account as a clean target while the block two rows
    # above called the same account out - the two surfaces contradicting each
    # other on the row the operator is about to click.
    weekly = app_mod._switch_target_label(
        _switch_row(6, "week", five=10.0, seven=100.0), name_width=4
    )
    assert "7d 100% (!)" in weekly, weekly
    assert weekly.endswith("(at limit)"), weekly

    # A stale read shows its age here exactly as it does on the account row
    # (SPEC 4.3). This IS the moment of the decision: a three-hour-old read
    # that renders identically to a live one is the dishonest case.
    stale = app_mod._switch_target_label(
        _switch_row(7, "old", five=12.0, age=3 * 3600.0), name_width=4
    )
    assert "(usage 3h old)" in stale, stale
    fresh = app_mod._switch_target_label(
        _switch_row(7, "old", five=12.0, age=5.0), name_width=4
    )
    assert "old)" not in fresh, fresh

    active = _switch_row(1, "main", five=100.0, active=True)
    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    app = app_mod.CCUsageWidgetApp()
    try:
        snapshot = UiSnapshot(settings=settings, accounts=(active, row), active=active)
        submenu = app._switch_account_submenu(snapshot)
        children = [str(getattr(item, "title", "")) for item in submenu.values()]
        assert any("↺00:00" in text and "Fable 40%" in text for text in children), children
        # At limit is a label, not a lock: the click still works.
        clickable = [
            item for item in submenu.values() if getattr(item, "callback", None) is not None
        ]
        assert len(clickable) == 1, children
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_switch_targets_are_ordered_by_headroom() -> None:
    """Most headroom first - the submenu is a ranking, not the order
    claude-swap happened to list the slots in.

    Headroom is the MINIMUM across the windows, so the ranking is on the
    highest one reported. Ranking on the 5-hour window alone put an account at
    5h 10% / 7d 100% at the top of the list while the Accounts section marked
    that same account `7d 100% (!)`.

    A row the API reported no window for sorts LAST: "not reported" is not
    "empty", and guessing it is free headroom would send the operator onto an
    account nobody can vouch for.
    """
    rows = (
        _switch_row(1, "main", five=100.0, active=True),
        _switch_row(5, "unknown", five=None),
        _switch_row(3, "podol", five=100.0, reset="00:00"),
        _switch_row(2, "jan", five=42.0),
        _switch_row(4, "late", five=100.0, reset="02:30"),
        _switch_row(9, "codex", five=0.0),
    )
    codex = replace(rows[-1], switchable=False)  # a read-only pseudo-account
    ordered = app_mod._switch_targets(rows[:-1] + (codex,))
    assert [row.slot for row in ordered] == [2, 3, 4, 5], [r.slot for r in ordered]
    assert all(row.slot != 1 for row in ordered), "the active row is not a target"
    assert all(row.slot != 9 for row in ordered), "a read-only row is not a target"

    # An exhausted weekly window sinks the row even though its 5h is nearly
    # free: that account will refuse work the moment it is switched to.
    weekly = (
        _switch_row(2, "jan", five=10.0, seven=100.0),
        _switch_row(3, "podol", five=60.0, seven=60.0),
    )
    assert [row.slot for row in app_mod._switch_targets(weekly)] == [3, 2]

    # The reset time is displayed, never ranked on: these strings are
    # upstream's clock ("00:30" resets AFTER "23:50" on the same evening) and
    # comparing them as text made the top row the worst target on exactly the
    # late-evening click this feature exists for.
    midnight = (
        _switch_row(2, "early", five=100.0, seven=100.0, reset="00:30"),
        _switch_row(3, "late", five=100.0, seven=100.0, reset="23:50"),
    )
    assert [row.slot for row in app_mod._switch_targets(midnight)] == [2, 3]
    assert [row.slot for row in app_mod._switch_targets(midnight[::-1])] == [2, 3], (
        "slot, not the reset string, is the tie-break"
    )


class _BestSwitchSwitcher:
    """Stands in for ``claude_swap.switcher.ClaudeAccountSwitcher.switch``."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def switch(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


class _BestSwitchSource(SwapAccountSource):
    """A source whose backend is a stub and whose refresh is a no-op."""

    def __init__(self, result: Any, model: str | None = None) -> None:
        super().__init__(settings=None)
        self.switcher = _BestSwitchSwitcher(result)
        self._model = model
        self.refreshed = 0
        self._alias_by_slot = {3: "podol"}

    def _backend_or_none(self) -> Any:
        policy = type("_Policy", (), {"model": self._model})()
        return type(
            "_Stub",
            (),
            {
                "switcher": self.switcher,
                "load_policy": staticmethod(lambda _dir: policy),
                "backup_dir": Path("/nonexistent"),
            },
        )()

    def refresh(self, *, force: bool = False) -> Any:
        self.refreshed += 1
        return None


def _accounts_log() -> tuple[Any, list[str]]:
    """A handler capturing ``cc_usage_widget.accounts`` records."""
    import logging

    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = _Capture()
    logger = logging.getLogger("cc_usage_widget.accounts")
    previous = logger.level
    # INFO is the level the switch lines are logged at; the default NOTSET
    # inherits root's WARNING and would silently capture nothing.
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    def _detach() -> None:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    return _detach, lines


def test_switch_best_delegates_the_pick_to_claude_swap() -> None:
    """One click must ask claude-swap where to go, not rank the rows itself.

    The menu's percentages are a display read that can be minutes old, so a
    second, disagreeing ranking would be an invented number (SPEC 4.3). The
    call therefore goes to ``switcher.switch(strategy="best")`` with the same
    per-model limits the autoswitch engine reads, and every ``warnings`` line
    upstream returns reaches the event log - ``switch_to`` dropped them.
    """
    detach, lines = _accounts_log()
    try:
        source = _BestSwitchSource(
            {
                "switched": True,
                "from": {"number": 1, "email": "main@example.com"},
                "to": {"number": 3, "email": "podol@example.com"},
                "strategy": "best",
                "reason": "switched",
                "message": "Switched to Account-3 (podol@example.com)",
                "warnings": ["Skipped Account-2 (disabled)"],
            },
            model="Fable, Opus ,Fable",
        )
        assert source.switch_best() is True
        assert len(source.switcher.calls) == 1, source.switcher.calls
        call = source.switcher.calls[0]
        assert call["strategy"] == "best"
        assert call["json_output"] is True
        # Deduped, first spelling wins - upstream's own parse of
        # autoswitch.model. Without claude-swap installed there is no parser,
        # and `accounts.py` degrades to "no model limits" with a log line
        # rather than guessing a split - so both answers are pinned here and
        # neither machine gets to skip the assertion.
        expected = ("Fable", "Opus") if _CLAUDE_SWAP_PRESENT else ()
        assert call["models"] == expected, (call["models"], _CLAUDE_SWAP_PRESENT)
        assert source.refreshed == 1, "the new active row was not re-read"
        # W1's external-switch detector calls any active-slot change it cannot
        # explain a ghost flip. `best` picks its own target, so the expectation
        # is recorded from upstream's own answer, BEFORE the refresh that sees
        # it - otherwise the widget's own headline action raises W1's alarm.
        assert getattr(source, "_expect_active", None) == "3", getattr(
            source, "_expect_active", None
        )
        assert any("manual switch (best) -> podol" in line for line in lines), lines
        events = source.recent_events()
        assert any("manual switch (best) -> podol" in line for line in events), events
        assert any("Skipped Account-2 (disabled)" in line for line in events), events

        # A truncated warning list must SAY it was truncated: several disabled
        # slots plus an inert-model-limit line exceed the log cap, and a
        # complete-looking list that is not complete is the dishonest case.
        many = _BestSwitchSource(
            {
                "switched": True,
                "to": {"number": 3, "email": "podol@example.com"},
                "reason": "switched",
                "warnings": [f"Skipped Account-{n} (disabled)" for n in range(2, 9)],
            }
        )
        assert many.switch_best() is True
        capped = [line for line in many.recent_events() if "switch (best):" in line]
        assert len(capped) == 5, capped
        assert capped[-1].endswith("… and 3 more (see the log)"), capped[-1]
        # Every line still reaches the log the summary points at.
        for n in range(2, 9):
            assert any(f"Account-{n} (disabled)" in line for line in lines), n

        # Already on the best account is the right outcome, not a fault.
        already = _BestSwitchSource(
            {
                "switched": False,
                "to": {"number": 3, "email": "podol@example.com"},
                "strategy": "best",
                "reason": "already-best",
                "message": "Already on the account with the most remaining quota",
                "warnings": [],
            }
        )
        assert already.switch_best() is True
        assert already.switcher.calls[0]["models"] == ()
        assert already.refreshed == 0, "nothing moved; nothing to re-read"
        assert already.last_error is None, already.last_error

        # "I cannot tell" is NOT success - it must reach the menu as an error.
        blind = _BestSwitchSource(
            {
                "switched": False,
                "to": {"number": 1, "email": "main@example.com"},
                "strategy": "best",
                "reason": "usage-unavailable",
                "message": "Current account usage is unavailable — staying on Account-1.",
                "warnings": [],
            }
        )
        assert blind.switch_best() is False
        assert "usage is unavailable" in (blind.last_error or ""), blind.last_error
    finally:
        detach()


def test_switch_best_is_one_click_and_dims_with_nowhere_to_go() -> None:
    """The item lives beside the two top-level switches and reaches the worker.

    A menu item that looks live and silently does nothing is worse than a dim
    one, so with no other switchable account it carries no callback at all.
    """
    active = _switch_row(1, "main", five=100.0, active=True)
    other = _switch_row(3, "podol", five=100.0, reset="00:00")
    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    app = app_mod.CCUsageWidgetApp()
    try:
        alone = UiSnapshot(settings=settings, accounts=(active,), active=active)
        item = [
            i for i in app._switch_items(alone)
            if str(getattr(i, "title", "")) == "Switch to best now"
        ]
        assert item, [str(getattr(i, "title", "")) for i in app._switch_items(alone)]
        assert getattr(item[0], "callback", None) is None, "clickable with nowhere to go"

        pair = UiSnapshot(settings=settings, accounts=(active, other), active=active)
        item = [
            i for i in app._switch_items(pair)
            if str(getattr(i, "title", "")) == "Switch to best now"
        ]
        assert getattr(item[0], "callback", None) is not None
        app.rebuild_menu(pair)
        assert "Switch to best now" in [
            str(getattr(i, "title", "")) for i in app.menu.values()
        ]
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)

    # The command reaches the source on the WORKER thread, never the main one,
    # and the REASON the source recorded is what the menu shows. "All accounts
    # are at their limit" and "I could not read the current usage" are
    # different situations; `_accounts_diagnosis` only surfaces `last_error`
    # when `rows()` came back empty, so on a healthy fleet the reason would
    # never have reached the menu and every refusal read the same.
    class _Best:
        def __init__(self, reason: str | None) -> None:
            self.calls = 0
            self.reason = reason
            self.last_error: str | None = None

        def switch_best(self) -> bool:
            self.calls += 1
            self.last_error = self.reason
            return False

        def rows(self, **_kw: Any) -> tuple[Any, ...]:
            return (active,)

    def _publish_refusal(source: Any) -> UiSnapshot:
        published: list[UiSnapshot] = []
        worker = BackgroundWorker(
            publish=published.append,
            snapshot=UiSnapshot(settings=settings),
            accounts=source,
        )
        worker._handle_command((app_mod._CMD_SWITCH_BEST, None))
        return published[-1]

    told = _Best("Current account usage is unavailable — staying on Account-1.")
    snapshot = _publish_refusal(told)
    assert told.calls == 1
    assert "usage is unavailable" in (snapshot.accounts_error or ""), (
        snapshot.accounts_error
    )

    # A source that recorded nothing still gets a visible line (Rule 12).
    silent = _Best(None)
    assert "best account was refused" in (
        _publish_refusal(silent).accounts_error or ""
    )


# ---------------------------------------------------------------------------
# 2026-09-01: a second actor that can switch must not stay invisible
# ---------------------------------------------------------------------------

# Real command lines. The bare one is verbatim (minus the home directory) from
# ``ps -Ao pid,command`` on 2026-09-01 - the TUI that had been switching
# accounts under the widget since 19:22; the rest keep its exact shape:
# interpreter path, then console-script path, then argv.
#
# These are COMMANDS, not ``pgrep -fl`` output lines: ``pgrep -f`` matches the
# pattern against the command alone, and only its *listing* prefixes the pid.
# Asserting against a pid-prefixed string would have tested a shape the pattern
# never sees (and would have hidden that ``^`` can match at all).
_PGREP_PYTHON = "/Users/v/.local/share/uv/tools/claude-swap/bin/python"
_PGREP_CSWAP = "/Users/v/.local/bin/cswap"
_RIVAL_COMMANDS = (
    f"{_PGREP_PYTHON} {_PGREP_CSWAP}",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} tui",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} auto",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} menubar",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} watch",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} tui --refresh 30",
    # The flag spellings of the same actors. `claude_swap.cli`'s
    # `_SUBCOMMAND_FLAGS` rewrites the memorable subcommands into exactly
    # these, and calls the flag form the established interface - so an alias or
    # LaunchAgent written against upstream runs `cswap --menubar`, which is a
    # genuine second engine on the shared autoswitch_state.json.
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} --tui",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} --watch",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} --menubar",
    "cswap",
    "cswap tui",
)
_INNOCENT_COMMANDS = (
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} list",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} status --json",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} switch 3",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} config set autoswitch.threshold 85",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} autoswitch",
    # A flag whose name merely STARTS with a subcommand is not that subcommand.
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} --watch-foo",
    f"{_PGREP_PYTHON} {_PGREP_CSWAP} --no-color",
    "ugrep -G --hidden -I -i cswap",
    "/usr/bin/tail -f /Users/v/logs/cswap.log",
    f"{_PGREP_PYTHON} -m cc_usage_widget",
)
_FILES_NAMED_CSWAP = (
    # These DO match the pattern - an ERE cannot express "argv[0] position", so
    # the bare-TUI arm `[[:space:]]*$` fires on any command line that merely
    # ENDS in a path named `cswap`. Verified against the real /usr/bin/pgrep:
    # `/usr/bin/tail -f <dir>/cswap` came back in the same listing as the
    # genuine TUI. Only `_rival_actor`'s position check separates them, and
    # only the `.log` suffix above is safe by the pattern alone.
    "/usr/bin/tail -f /Users/v/logs/cswap",
    "vim /Users/v/notes/cswap",
    "/bin/cat /Users/v/.local/bin/cswap",
    "/usr/bin/less /var/log/cswap",
)


def _rival_regex() -> Any:
    """``_RIVAL_PATTERN`` compiled by Python.

    ``pgrep`` compiles POSIX ERE, where ``[[:space:]]`` is the class and
    ``\\s`` is not a shorthand at all; Python's ``re`` is the other way round.
    Translating the one construct that differs is what lets the table below
    assert on the *same* pattern the process table is actually queried with.
    """
    from cc_usage_widget.__main__ import _RIVAL_PATTERN

    return re.compile(_RIVAL_PATTERN.replace("[[:space:]]", r"\s"))


def test_rival_pattern_matches_every_switch_capable_cswap_and_nothing_else() -> None:
    """The startup check saw ``cswap auto|menubar`` only.

    A bare ``cswap`` - which IS the TUI - switches on a keypress and can start
    its own engine from the account view, and that is exactly what was running
    (pid 15206) while the widget's engine kept switching back. The ``--tui``/
    ``--watch``/``--menubar`` spellings are the same four actors through
    upstream's own ``_SUBCOMMAND_FLAGS`` interface. Widening the pattern must
    not make it fire on the read-only subcommands, on a log file whose name ends
    in ``cswap``, on ``cswap autoswitch`` via the ``auto`` arm, or on
    ``--watch-foo`` via the ``watch`` one; a false rival is a permanent ``!``
    row telling the operator to quit a process that is not there.
    """
    pattern = _rival_regex()
    for command in _RIVAL_COMMANDS:
        assert pattern.search(command), f"missed a switch-capable actor: {command}"
    for command in _INNOCENT_COMMANDS:
        assert not pattern.search(command), f"false rival: {command}"


def test_a_file_merely_named_cswap_is_not_reported_as_a_rival() -> None:
    """The pattern is a pre-filter; executable position is the decision.

    ``/usr/bin/tail -f ~/logs/cswap`` satisfies the bare-TUI arm, and the real
    ``pgrep`` returns it next to the genuine TUI. Reported, it would read as
    ``another switch actor: cswap (pid N) — it can switch …``: a fabricated
    claim about a process that cannot switch anything, indistinguishable from
    the true row. The detector must drop it before the menu ever sees it.
    """
    from cc_usage_widget.__main__ import _detect_rival_engines, _rival_actor

    pattern = _rival_regex()
    for command in _FILES_NAMED_CSWAP:
        # Asserting on the regex here would be asserting the hole, not the fix.
        assert pattern.search(command), f"sample no longer exercises the arm: {command}"
        assert _rival_actor(command) is None, f"false rival: {command}"
    for command in _RIVAL_COMMANDS:
        assert _rival_actor(command) is not None, f"missed a real actor: {command}"

    # …and the drop happens in the detector, not just in the helper. The whole
    # `pgrep` listing is these four lines, exactly as the real one returned
    # them (pid, then the command); the detector must return nothing at all.
    listing = "\n".join(
        f"{pid} {command}" for pid, command in enumerate(_FILES_NAMED_CSWAP, start=900)
    )

    class _FakeSubprocess:
        """Stands in for the ``subprocess`` module inside ``__main__`` only -
        patching ``subprocess.run`` itself would patch it for every module."""

        SubprocessError = subprocess.SubprocessError

        @staticmethod
        def run(*args: Any, **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=listing + "\n", stderr=""
            )

    main_mod = sys.modules["cc_usage_widget.__main__"]
    real_subprocess = main_mod.subprocess
    main_mod.subprocess = _FakeSubprocess  # type: ignore[assignment]
    try:
        assert _detect_rival_engines() == [], "a file named cswap reached the menu"
    finally:
        main_mod.subprocess = real_subprocess  # type: ignore[assignment]


def test_rival_pattern_is_valid_posix_ere_for_pgrep() -> None:
    """A pattern Python likes and ``pgrep`` rejects fails silently in prod.

    ``pgrep`` exits 0 (matched) or 1 (no match) on a good pattern and 2 on a
    bad one - and the widget's caller only reads stdout, so a 2 would have
    looked exactly like "no rivals are running", forever.
    """
    from cc_usage_widget.__main__ import _RIVAL_PATTERN

    if not Path("/usr/bin/pgrep").exists():  # pragma: no cover - macOS only
        return
    proc = subprocess.run(
        ["/usr/bin/pgrep", "-fl", _RIVAL_PATTERN],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    assert proc.returncode in (0, 1), (proc.returncode, proc.stderr.strip())


def test_rival_actor_names_the_actor_not_its_install_path() -> None:
    """``!`` rows are one menu line; two absolute paths do not fit in one.

    The label is also what keeps the row inside ``MAX_ERROR_CHARS``: the longest
    it can be is ``cswap menubar`` plus a 7-digit pid, and the half of the line
    that tells the operator what to expect is at the END, which is the half a
    truncation removes.
    """
    from cc_usage_widget.__main__ import _rival_actor

    assert _rival_actor(f"{_PGREP_PYTHON} {_PGREP_CSWAP}") == "cswap"
    assert _rival_actor(f"{_PGREP_PYTHON} {_PGREP_CSWAP} tui") == "cswap tui"
    assert _rival_actor(f"{_PGREP_PYTHON} {_PGREP_CSWAP} tui --refresh 30") == "cswap tui"
    assert _rival_actor(f"{_PGREP_PYTHON} {_PGREP_CSWAP} menubar") == "cswap menubar"
    # The flag spelling is the same actor, so it must read as the same row.
    assert _rival_actor(f"{_PGREP_PYTHON} {_PGREP_CSWAP} --menubar") == "cswap menubar"
    assert _rival_actor(f"{_PGREP_PYTHON} {_PGREP_CSWAP} --watch") == "cswap watch"
    # A flag is not a subcommand: a bare TUI stays "cswap".
    assert _rival_actor(f"{_PGREP_PYTHON} {_PGREP_CSWAP} --no-color") == "cswap"
    # Exec'd directly, with no interpreter in front of it.
    assert _rival_actor("cswap tui") == "cswap tui"
    # Every label is short enough that the actionable tail survives the menu.
    for command in _RIVAL_COMMANDS:
        actor = _rival_actor(command)
        assert actor is not None and len(actor) <= 20, (command, actor)


class _CountingDetector:
    """Stand-in for ``pgrep``: counts calls, returns whatever is set on it."""

    def __init__(self, findings: list[str]) -> None:
        self.findings = findings
        self.calls = 0

    def __call__(self) -> list[str]:
        self.calls += 1
        return list(self.findings)


def test_rival_rescan_keeps_its_own_cadence_and_replaces_its_last_finding() -> None:
    """Periodic, but never per tick - and never additive.

    Before this, ``_detect_rival_engines`` ran once in ``main()``: a TUI opened
    after launch was invisible for the life of the process, and one that quit
    was reported forever. The rescan therefore (a) is gated to its own
    interval, not the 60 s UI tick, because it is the only subprocess the
    steady state runs, and (b) REPLACES its previous lines so the menu shows
    who is running now, while wiring errors from other seams survive untouched.
    """
    other = "indexer.py wiring failed: RuntimeError: boom"
    stale = 'another switch actor: cswap auto (pid 9) — it can switch and can run its own engine; switches it makes show here as "external"'
    fresh = 'another switch actor: cswap tui (pid 15206) — it can switch and can run its own engine; switches it makes show here as "external"'
    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(
            settings=normalize_settings(dict(SETTINGS_DEFAULTS)),
            wiring_errors=(other, stale),
        ),
    )
    detector = _CountingDetector([fresh])
    worker._rival_detector = detector

    start = 10_000.0
    worker._next_rival_scan = start
    # One second short of due: no subprocess, whatever else the loop is doing.
    assert worker._maybe_rescan_rivals(start - 1.0) is False
    assert detector.calls == 0, "rescan ran early"
    assert not published

    assert worker._maybe_rescan_rivals(start) is True
    assert detector.calls == 1
    assert published, "a changed rival set published nothing"
    assert published[-1].wiring_errors == (other, fresh), published[-1].wiring_errors

    # The whole interval must pass, not one UI tick.
    for elapsed in (60.0, 120.0, app_mod.RIVAL_RESCAN_SECONDS - 1.0):
        assert worker._maybe_rescan_rivals(start + elapsed) is False
    assert detector.calls == 1

    # Due again, same finding: re-scanned, but nothing republished (the menu
    # must not be rebuilt every five minutes for an unchanged state).
    published_before = len(published)
    assert worker._maybe_rescan_rivals(start + app_mod.RIVAL_RESCAN_SECONDS) is True
    assert detector.calls == 2
    assert len(published) == published_before

    # The rival quit: its line goes, the unrelated wiring error stays.
    detector.findings = []
    assert worker._maybe_rescan_rivals(start + 2 * app_mod.RIVAL_RESCAN_SECONDS) is True
    assert published[-1].wiring_errors == (other,), published[-1].wiring_errors


def test_the_worker_loop_actually_reaches_the_rival_gate() -> None:
    """The gate is worthless if nothing calls it.

    ``_loop_once`` holds the only call site, and W6 (`cadence`) rewrites exactly
    that region. Every other test here drives ``_maybe_rescan_rivals`` directly,
    so all of them would still pass with the three-line insert merged away - and
    the widget would silently be back to startup-only detection, the very bug
    this work exists to fix. This one exercises the loop body instead.

    The due times are in the PAST on purpose: a future one makes the loop block
    on ``self._commands.get(timeout=…)`` for that long, and the rival gate sits
    after the queue read.
    """
    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
    )
    detector = _CountingDetector([])
    worker._rival_detector = detector
    worker._next_rival_scan = 0.0
    ran: list[str] = []

    # The two jobs are other seams' work; this test is about the gate alone.
    def fake_accounts_job(**kwargs: Any) -> None:
        ran.append("accounts")

    def fake_cost_job() -> float:
        ran.append("cost")
        return time.monotonic() + 1e6

    worker._run_accounts_job = fake_accounts_job  # type: ignore[method-assign]
    worker._run_cost_job = fake_cost_job  # type: ignore[method-assign]

    stop, _next_ui, _next_cost, _next_engine = worker._loop_once(0.0, 0.0)

    assert stop is False
    assert detector.calls == 1, "the loop never reached the rival gate"
    assert ran == ["accounts", "cost"], ran


def test_a_failed_rival_rescan_is_logged_and_the_loop_survives() -> None:
    """``pgrep`` dying must not take the worker's schedule with it."""

    def explode() -> list[str]:
        raise OSError("pgrep: no such file")

    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
    )
    worker._rival_detector = explode
    worker._next_rival_scan = 0.0
    assert worker._maybe_rescan_rivals(1.0) is True
    assert not published, "a failed scan must not publish a snapshot"
    # Re-armed, so a transient failure is retried rather than latched.
    assert worker._next_rival_scan == 1.0 + app_mod.RIVAL_RESCAN_SECONDS


def test_rival_finding_reaches_the_menu_whole() -> None:
    """The row has to survive the menu's truncation to be actionable.

    The half that tells the operator what to expect - that the switches show up
    here as ``external`` - is at the END of the line, which is precisely the
    half a ``MAX_ERROR_CHARS`` cut removes.
    """
    from cc_usage_widget.__main__ import _rival_actor

    label = _rival_actor(f"{_PGREP_PYTHON} {_PGREP_CSWAP} tui")
    line = (
        f"another switch actor: {label} (pid 15206) — it can switch and can "
        'run its own engine; switches it makes show here as "external"'
    )
    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
    )
    worker._rival_detector = _CountingDetector([line])
    worker._next_rival_scan = 0.0
    worker._maybe_rescan_rivals(1.0)
    assert published, "nothing published"

    app = app_mod.CCUsageWidgetApp()
    try:
        app.rebuild_menu(published[-1])
        texts = [str(getattr(item, "title", "")) for item in app.menu.values()]
        assert any(text == f"! {line}" for text in texts), texts
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# W6: the autoswitch engine keeps claude-swap's cadence, not the 60 s UI tick
# ---------------------------------------------------------------------------

_Empty = app_mod.queue.Empty
"""``queue.Empty``. Taken from ``app_mod`` rather than a new import here so the
test file's import block stays untouched."""


class _RecordingQueue:
    """A command queue that never has a command, but remembers how long the
    worker was willing to wait for one - i.e. the loop's sleep."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.polls = 0

    def get(self, timeout: float | None = None) -> Any:
        self.timeouts.append(float(timeout if timeout is not None else -1.0))
        raise _Empty

    def get_nowait(self) -> Any:
        self.polls += 1
        raise _Empty


class _CadenceAccounts:
    """A fake adapter shaped like ``SwapAccountSource`` for the loop's sake.

    ``next_tick_in`` is the seam under test: the real adapter answers with the
    deadline its own ``AutoSwitchEngine`` set (cooldown, reset-parking and the
    usage store's poll plan all folded in), and the worker must honour it.
    """

    def __init__(self, due_in: float | None, switched: str | None = None) -> None:
        self._due_in = due_in
        self._switched = switched
        self.asked = 0
        self.evaluated = 0
        self.refreshed = 0
        self.alert: tuple[str, str] | None = None
        self.row = object()

    # -- the seam ------------------------------------------------------
    def next_tick_in(self) -> float | None:
        self.asked += 1
        return self._due_in

    # -- the rest of the AccountSource surface -------------------------
    def refresh(self, *, force: bool = False) -> None:
        self.refreshed += 1

    def rows(self) -> tuple[Any, ...]:
        return (self.row,)

    def active(self) -> Any:
        return self.row

    def autoswitch_enabled(self) -> bool | None:
        return True

    def set_autoswitch_enabled(self, enabled: bool) -> None:
        return None

    def evaluate_autoswitch(self) -> str | None:
        self.evaluated += 1
        return self._switched

    def switch_to(self, slot_or_alias: str) -> bool:
        return False

    def current_alert(self) -> tuple[str, str] | None:
        return self.alert


class _LegacyAccounts(_CadenceAccounts):
    """An adapter from before ``next_tick_in`` existed. The worker must fall
    back to its own cadence rather than inventing a due time."""

    next_tick_in = None  # not callable -> the getattr guard must reject it


def _cadence_worker(accounts: Any) -> tuple[Any, _RecordingQueue, list[UiSnapshot]]:
    """A worker with slow UI/cost cadences, so only the engine can wake it."""
    published: list[UiSnapshot] = []
    settings = normalize_settings(
        dict(SETTINGS_DEFAULTS, ui_interval_seconds=300, cost_interval_seconds=1800)
    )
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=settings),
        accounts=accounts,
    )
    commands = _RecordingQueue()
    worker._commands = commands  # type: ignore[assignment]
    return worker, commands, published


def test_engine_due_time_shortens_the_worker_sleep() -> None:
    """An engine due in 20 s must not wait for the 300 s UI tick (G4).

    Before this, the worker slept ``min(next_ui, next_cost)`` and evaluated
    autoswitch only inside the UI job, so ``autoswitch.intervalSeconds`` was
    silently rounded up to ``ui_interval_seconds`` - a 15 s interval was served
    once a minute, and every "why did it not switch for another minute" was
    invisible in the log.
    """
    accounts = _CadenceAccounts(due_in=20.0)
    worker, commands, _published = _cadence_worker(accounts)

    now = time.monotonic()
    stop, next_ui, next_cost, next_engine = worker._loop_once(
        now + 300.0, now + 1800.0, float("inf")
    )

    assert stop is False
    assert commands.timeouts, "the worker never waited on its command queue"
    slept = commands.timeouts[0]
    assert 19.0 <= slept <= 20.5, f"engine due in 20 s but the worker slept {slept:.1f} s"
    assert accounts.asked >= 1, "the loop never asked the adapter for its due time"
    # The other two deadlines are untouched: this is an extra wake-up, not a
    # faster UI.
    assert next_ui == now + 300.0
    assert next_cost == now + 1800.0
    assert next_engine <= now + 20.5


def test_engine_due_tick_runs_without_a_ui_tick() -> None:
    """When only the engine is due, evaluate autoswitch - and nothing else.

    The point of the extra wake-up is the engine's cadence, not a UI refresh
    four times as often, so the accounts job (and its snapshot pass) must stay
    on its own 300 s schedule.
    """
    accounts = _CadenceAccounts(due_in=0.0)
    worker, _commands, published = _cadence_worker(accounts)

    now = time.monotonic()
    stop, next_ui, next_cost, next_engine = worker._loop_once(
        now + 300.0, now + 1800.0, now - 1.0
    )

    assert stop is False
    assert accounts.evaluated == 1, f"{accounts.evaluated} autoswitch evaluations"
    assert accounts.refreshed == 0, "the engine tick dragged the whole UI job with it"
    assert next_ui == now + 300.0, "the UI tick must keep its own schedule"
    assert not published, "an idle tick published a snapshot"
    assert next_engine >= now + 14.0, f"engine deadline re-armed to {next_engine - now:.1f} s"


def test_engine_tick_publishes_a_switch_it_made() -> None:
    """A switch between UI ticks must reach the menu bar immediately.

    An account change the widget itself made, sitting unpublished until the
    next 300 s tick, is the same stale-state-rendered-as-live bug the alert
    plumbing exists to end.
    """
    accounts = _CadenceAccounts(due_in=0.0, switched="podol")
    accounts.alert = (ALERT_ALL_EXHAUSTED, "all accounts exhausted")
    worker, _commands, published = _cadence_worker(accounts)

    now = time.monotonic()
    worker._loop_once(now + 300.0, now + 1800.0, now - 1.0)

    assert accounts.evaluated == 1
    assert accounts.refreshed == 1, "a switch must be followed by a forced re-read"
    assert published, "the switch was never published"
    snapshot = published[-1]
    assert snapshot.accounts == (accounts.row,)
    assert snapshot.active is accounts.row
    assert snapshot.alert == (ALERT_ALL_EXHAUSTED, "all accounts exhausted")


def test_a_due_now_adapter_cannot_spin_the_worker() -> None:
    """An adapter stuck on "due now" must cost one tick, not a hot loop.

    ``evaluate_autoswitch`` always pushes its own deadline forward, so this is
    defence in depth - but the loop is the last place that may busy-wait, and a
    spin here would burn the < 0.3% idle-CPU budget (SPEC 2.1) invisibly.
    """
    accounts = _CadenceAccounts(due_in=0.0)
    worker, commands, _published = _cadence_worker(accounts)

    now = time.monotonic()
    deadline = now - 1.0
    for _ in range(5):
        _stop, _next_ui, _next_cost, deadline = worker._loop_once(
            now + 300.0, now + 1800.0, deadline
        )

    assert accounts.evaluated == 1, f"{accounts.evaluated} engine ticks in five iterations"
    assert deadline >= now + 14.0, f"deadline only moved to +{deadline - now:.1f} s"


def test_adapter_without_next_tick_in_keeps_the_ui_cadence() -> None:
    """No due time on offer means no extra wake-up - never a guessed one."""
    accounts = _LegacyAccounts(due_in=None)
    worker, commands, _published = _cadence_worker(accounts)

    now = time.monotonic()
    stop, _next_ui, _next_cost, next_engine = worker._loop_once(
        now + 300.0, now + 1800.0, float("inf")
    )

    assert stop is False
    assert commands.timeouts and 299.0 <= commands.timeouts[0] <= 300.5, commands.timeouts
    assert accounts.evaluated == 0, "the engine was ticked with no due time to justify it"
    assert next_engine == float("inf")


def test_next_tick_in_reports_the_engines_own_due_time() -> None:
    """The adapter's answer is a read of the deadline a tick already set.

    It must not start an engine, must clamp a passed deadline at 0 rather than
    going negative, and must answer ``None`` (not 0) when autoswitch is off -
    "no schedule of ours" is not "due now".
    """
    source = SwapAccountSource(settings={"autoswitch_enabled": True})
    source.autoswitch_enabled = lambda: True  # type: ignore[method-assign]

    source._next_tick_at = time.monotonic() + 42.0
    delay = source.next_tick_in()
    assert delay is not None and 41.0 <= delay <= 42.0, delay

    source._next_tick_at = time.monotonic() - 10.0
    assert source.next_tick_in() == 0.0, "an overdue tick must read as 0, not negative"

    # Read-only: no engine constructed, no schedule moved.
    assert source._engine is None, "next_tick_in started an engine"
    before = source._next_tick_at
    source.next_tick_in()
    assert source._next_tick_at == before, "next_tick_in moved the schedule"

    source.autoswitch_enabled = lambda: False  # type: ignore[method-assign]
    assert source.next_tick_in() is None, "autoswitch off must read as no schedule"



# ---------------------------------------------------------------------------
# integration seams between the 2026-09-01 branches (title ↔ worker, switch-ux
# ↔ forensics, cadence ↔ forensics)
# ---------------------------------------------------------------------------


def test_threshold_and_journal_reach_the_snapshot_from_both_publish_paths() -> None:
    """The title branch added ``UiSnapshot.autoswitch_threshold`` and nothing
    populated it (it fell back to 85 silently); the cadence branch's between-
    tick publish carried neither the journal nor the switch note, so a switch it
    made showed a new active account above a "Recent switches" block that still
    ended on the previous one. Both seams are wired here and pinned here.
    """

    class _Row:
        slot, alias, email, is_active = 1, "main", "m@x.io", True
        five_hour_pct = seven_day_pct = None
        scoped_windows: tuple[Any, ...] = ()
        usage_age_seconds = None
        usage_is_stale = False
        switchable = True
        vendor = "claude"

    class _Source:
        def __init__(self) -> None:
            self.events: tuple[str, ...] = ("21:05 no switch: no-viable-target",)
            self.switched: str | None = None

        def refresh(self, *, force: bool = False) -> None:
            return None

        def rows(self) -> tuple[Any, ...]:
            return (_Row(),)

        def active(self) -> Any:
            return _Row()

        def autoswitch_enabled(self) -> bool:
            return True

        def set_autoswitch_enabled(self, enabled: bool) -> None:
            return None

        def evaluate_autoswitch(self) -> str | None:
            return self.switched

        def switch_to(self, slot_or_alias: str) -> bool:
            return False

        def current_alert(self) -> tuple[str, str] | None:
            return None

        def cached_autoswitch_threshold(self) -> float:
            return 90.0

        def recent_events(self) -> tuple[str, ...]:
            return self.events

        def switch_note(self) -> str:
            return "last switch 21:05 (at-limit)"

    source = _Source()
    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=source,
    )
    worker._read_autoswitch = lambda default: True  # type: ignore[method-assign]
    worker._run_accounts_job(force=False)
    snapshot = published[-1]
    assert snapshot.autoswitch_threshold == 90.0, snapshot.autoswitch_threshold
    assert snapshot.recent_events == source.events
    assert snapshot.switch_note == "last switch 21:05 (at-limit)"

    # Between UI ticks: a journal line without a verdict change still publishes.
    worker._snapshot = snapshot
    source.events = source.events + ("21:06 active 5→1 (external)",)
    worker._run_autoswitch_tick()
    assert published[-1].recent_events[-1] == "21:06 active 5→1 (external)"

    # ...and a switch made between ticks carries the journal with the rows.
    worker._snapshot = published[-1]
    source.events = source.events + ("21:07 Switched Account-1 -> Account-3",)
    source.switched = "podol"
    worker._run_autoswitch_tick()
    assert published[-1].recent_events[-1] == "21:07 Switched Account-1 -> Account-3"
    assert published[-1].autoswitch_threshold == 90.0


def test_one_click_best_switch_is_not_reported_as_an_external_switch() -> None:
    """``switch_best`` lands on a slot the next refresh will see as "changed";
    without claiming it (as ``switch_to`` does) the forensics detector would
    log our own click as another actor and raise ``⚠ ext`` — the exact false
    alarm the detector's docstring promises never to raise. It also retracts a
    standing external-switch verdict, and its journal line is stamped like
    every other one.
    """
    import re as _re
    from cc_usage_widget.contracts import ALERT_EXTERNAL_SWITCH

    class _Switcher:
        def switch(self, strategy: str, json_output: bool, models: tuple[str, ...]) -> dict[str, Any]:
            assert strategy == "best" and json_output is True
            return {"switched": True, "from": {"number": "1"}, "to": {"number": "3", "email": "p@x.io"}, "warnings": ["Skipped Account-4 (disabled)"]}

    class _Backend:
        switcher = _Switcher()
        backup_dir = "/nonexistent"

        @staticmethod
        def load_policy(_dir: str) -> Any:
            raise RuntimeError("no policy in this test")

    source = SwapAccountSource(settings={})
    source._backend_or_none = lambda: _Backend()  # type: ignore[method-assign]
    source._external_alert = (ALERT_EXTERNAL_SWITCH, "21:05 active 5→1 (external)")
    seen: dict[str, Any] = {}

    def _refresh(*, force: bool = False) -> None:
        seen["expect"] = source._expect_active
        seen["alert"] = source._external_alert

    source.refresh = _refresh  # type: ignore[method-assign]
    assert source.switch_best() is True
    assert seen["expect"] == "3", seen
    assert seen["alert"] is None, "taking the wheel back must retract the external verdict"
    lines = source.recent_events()
    assert lines and _re.match(r"^\d\d:\d\d manual switch \(best\) -> ", lines[-1]), lines
    assert any("Skipped Account-4" in line for line in lines), lines


def test_disabled_flag_is_carried_from_claude_swap_into_the_row() -> None:
    """``cswap disable`` holds a slot out of rotation while it stays clickable.
    ``AccountRow`` had no field for it, so every fleet count treated slot 4
    (work1, disabled, 0 %) as a free room — a room the engine would never use.
    """
    import types

    source = SwapAccountSource(settings={})
    account = types.SimpleNamespace(
        number="4", email="w@x.io", alias="work1", is_active=False,
        switchable=True, disabled=True, usage=None,
    )
    row = source._build_row(account, "1", time.time(), None)
    assert row.disabled is True and row.switchable is True, row
    plain = types.SimpleNamespace(
        number="3", email="p@x.io", alias="podol", is_active=False,
        switchable=True, usage=None,
    )
    assert source._build_row(plain, "1", time.time(), None).disabled is False


# ---------------------------------------------------------------------------
# review fixes 2026-09-01 (forensics + cadence findings the fixers never landed)
# ---------------------------------------------------------------------------


class _RevEvent:
    """An engine event double: kind, human line, and the optional fields
    ``_drain_events`` reads (``reason``, ``to_ref``, ``dry_run``)."""

    dry_run = False

    def __init__(self, kind: str, line: str, *, reason: str = "", to_ref: Any = None) -> None:
        self.kind, self._line, self.reason, self.to_ref = kind, line, reason, to_ref

    def human(self) -> str:
        return self._line


def test_an_external_verdict_survives_idle_engine_ticks_until_the_widget_switches() -> None:
    """The engine emits ``no switch: below-threshold`` on nearly every idle
    tick, and a no-switch clears the engine's verdict — so the first cut lost
    the ``⚠ ext`` glyph within 60 s of raising it (review blocker). It lives
    in its own slot now: idle ticks and an all-exhausted verdict leave it,
    and only a switch the widget makes retracts it.
    """
    from cc_usage_widget.contracts import ALERT_ALL_EXHAUSTED as _EXH

    source, backend = _forensics_source("5")
    backend.snapshot.active_number = "1"
    source.refresh(force=True)
    assert source.current_alert()[0] == ALERT_EXTERNAL_SWITCH

    source._on_engine_event(_RevEvent("no-switch", "no switch: below-threshold", reason="below-threshold"))
    source._drain_events()
    assert source.current_alert()[0] == ALERT_EXTERNAL_SWITCH, "an idle tick must not retract it"

    # An engine verdict outranks it while both stand, but does not destroy it.
    source._on_engine_event(_RevEvent("all-exhausted", "all accounts exhausted"))
    source._drain_events()
    assert source.current_alert()[0] == _EXH
    source._on_engine_event(_RevEvent("no-switch", "no switch: below-threshold", reason="below-threshold"))
    source._drain_events()
    assert source.current_alert()[0] == ALERT_EXTERNAL_SWITCH, "clearing the engine verdict reveals it again"

    # Our own engine switching is "taking the wheel back".
    backend.snapshot.active_number = "3"
    source._on_engine_event(
        _RevEvent("switch", "Switched Account-1 -> Account-3", to_ref={"number": "3", "email": "p@x.io"})
    )
    source._drain_events()
    assert source.current_alert() is None, source.current_alert()


def test_an_external_switch_is_visible_with_autoswitch_off() -> None:
    """OFF is the default configuration, and the one in which a rival actor
    most plausibly owns the login; the verdict must not die with the toggle,
    and an engine verdict left in the slot must not be shown instead."""
    source, backend = _forensics_source("5")
    source.autoswitch_enabled = lambda: False
    source._alert = ("all-exhausted", "stale engine verdict")
    backend.snapshot.active_number = "1"
    source.refresh(force=True)
    alert = source.current_alert()
    assert alert is not None and alert[0] == ALERT_EXTERNAL_SWITCH, alert


def test_stopping_the_engine_keeps_an_external_verdict_but_drops_the_engines() -> None:
    source, _backend = _forensics_source("5")
    source._alert = ("all-exhausted", "engine verdict")
    source._external_alert = (ALERT_EXTERNAL_SWITCH, "21:05 active 5→1 (external)")
    source._stop_engine()
    assert source._alert is None
    assert source.current_alert() == (ALERT_EXTERNAL_SWITCH, "21:05 active 5→1 (external)")


def test_idle_no_switch_ticks_do_not_flood_the_journal() -> None:
    """One switch line plus thirty idle ticks: the switch must still be in the
    20-slot deque, or "Recent switches" shows five identical non-events."""
    source, backend = _forensics_source("5")
    backend.snapshot.active_number = "3"
    source._on_engine_event(
        _RevEvent("switch", "Switched Account-5 -> Account-3", to_ref={"number": "3", "email": "p@x.io"})
    )
    source._drain_events()
    for _ in range(30):
        source._on_engine_event(_RevEvent("no-switch", "no switch: below-threshold (17% < 85%)", reason="below-threshold"))
        source._drain_events()
    lines = source.recent_events()
    assert any("Switched Account-5" in line for line in lines), lines
    assert not any("below-threshold" in line for line in lines), lines
    # ...while a no-target verdict IS an event worth a line.
    source._on_engine_event(_RevEvent("no-switch", "no switch: no-viable-target", reason="no-viable-target"))
    source._drain_events()
    assert "no-viable-target" in source.recent_events()[-1]


def test_a_days_old_last_switch_carries_its_date() -> None:
    """``last switch 21:05`` read from a file that persists for weeks implied
    "today"; a switch from another day must say which day."""
    source = SwapAccountSource(settings={"autoswitch_enabled": True})
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "autoswitch_state.json"
        source._autoswitch_state_path = lambda: state
        three_days = time.time() - 3 * 86_400
        state.write_text(json.dumps({"lastSwitchAt": three_days, "leftTrigger": "at-limit"}), encoding="utf-8")
        note = source.switch_note()
        assert note is not None and note.startswith("last switch "), note
        expected = time.strftime("%b %d %H:%M", time.localtime(three_days))
        assert expected in note, (note, expected)
        os.utime(state, (three_days + 1, three_days + 1))
        state.write_text(json.dumps({"lastSwitchAt": time.time() - 60, "leftTrigger": "proactive"}), encoding="utf-8")
        note = source.switch_note()
        assert note is not None and re.search(r"last switch \d\d:\d\d \(proactive\)", note), note


def test_an_overdue_engine_is_evaluated_at_once() -> None:
    """``reported <= now`` is exactly the case that needs an immediate wake;
    the first cut refused to shorten the sleep for it and stalled a due engine
    for a whole UI interval after any worker exception."""
    accounts = _CadenceAccounts(due_in=0.0)
    worker, _commands, _published = _cadence_worker(accounts)
    now = time.monotonic()
    worker._loop_once(now + 300.0, now + 1800.0, float("inf"))
    assert accounts.evaluated == 1, accounts.evaluated


def test_a_sub_floor_adapter_is_bounded_by_the_floor() -> None:
    """A positive answer under the 15 s floor used to undercut the re-arm on
    the very next iteration (58 ticks in 3 s measured in review)."""

    class _SleepingQueue(_RecordingQueue):
        """The real queue blocks for its timeout; the recording one returns
        at once, which is why the reviewer's measurement (58 ticks in 3 s)
        was invisible to the shipped test."""

        def get(self, timeout: float | None = None) -> Any:
            time.sleep(min(float(timeout or 0.0), 0.2))
            return super().get(timeout=timeout)

    accounts = _CadenceAccounts(due_in=0.05)
    worker, _commands, _published = _cadence_worker(accounts)
    worker._commands = _SleepingQueue()  # type: ignore[assignment]
    start = time.monotonic()
    deadline = float("inf")
    while time.monotonic() - start < 0.6:
        _stop, _ui, _cost, deadline = worker._loop_once(start + 300.0, start + 1800.0, deadline)
    assert accounts.evaluated == 1, f"{accounts.evaluated} ticks in 0.6 s"
    assert deadline >= start + 14.0, deadline - start


def test_autoswitch_off_guard_holds_on_the_engine_only_path() -> None:
    """A stale past deadline survives the toggle going off; the engine-only
    path must still refuse to tick (SPEC 6.2)."""

    class _Off(_CadenceAccounts):
        def autoswitch_enabled(self) -> bool | None:
            return False

    accounts = _Off(due_in=0.0)
    worker, _commands, published = _cadence_worker(accounts)
    now = time.monotonic()
    worker._loop_once(now + 300.0, now + 1800.0, now - 1.0)
    assert accounts.evaluated == 0, accounts.evaluated
    assert not published, "nothing to publish when the guard holds"


# ---------------------------------------------------------------------------
# 13. SPEC-CODEX 6: a second Codex source must cost the old one NOTHING
# ---------------------------------------------------------------------------


class _FakeRegistryEntry:
    """One ``codex_accounts.RegistryEntry``, duck-typed."""

    def __init__(self, account_id: str, alias: str, enabled: bool) -> None:
        self.account_id, self.alias, self.enabled = account_id, alias, enabled
        self.order = 0


class _FakeRegistry:
    def __init__(self, entries: tuple[_FakeRegistryEntry, ...]) -> None:
        self._entries = entries
        self.set_calls: list[tuple[str, bool]] = []

    def entries(self) -> tuple[_FakeRegistryEntry, ...]:
        return self._entries

    def set_enabled(self, account_id: str, flag: bool) -> bool:
        self.set_calls.append((account_id, flag))
        return True


class _FakeLiveCodexSource:
    """A stand-in for ``codex_accounts.CodexAccountsSource``.

    Hand-built rather than imported on purpose: this module owns the WIRING
    (start/stop/pause/force_due, the merge, the menu), and the wiring's whole
    promise is that it is duck-typed — it must hold for any object satisfying
    the SPEC-CODEX 6 contract, including one that does not exist yet on a
    machine where ``codex_accounts.py`` failed to import. Importing the real
    source here would also put a poller thread and a credential-directory
    stat inside a unit test.
    """

    vendor = VENDOR_CODEX

    def __init__(
        self,
        rows: tuple[Any, ...] = (),
        *,
        available: bool = True,
        entries: tuple[_FakeRegistryEntry, ...] = (),
    ) -> None:
        self.rows = rows
        self._available = available
        self._registry = _FakeRegistry(entries)
        self.started = 0
        self.stopped = 0
        self.forced = 0
        self.paused: list[bool] = []

    def available(self) -> bool:
        return self._available

    def quota_rows(self) -> tuple[Any, ...]:
        return self.rows

    def start(self) -> None:
        self.started += 1

    def stop(self, timeout: float = 2.0) -> bool:
        self.stopped += 1
        return True

    def pause(self, flag: bool) -> None:
        self.paused.append(bool(flag))

    def force_due(self) -> None:
        self.forced += 1

    def diagnostics(self) -> tuple[str, ...]:
        return ("codex accounts: 2 tracked, 2 enabled, live quota on",)


def _scanned_codex_row() -> Any:
    """The transcript-derived Codex row, exactly as ``CodexIndexer`` emits it.

    Built by hand with a VERBATIM reset string rather than scanned out of a
    fixture corpus, because this test pins byte-for-byte output and a
    clock-derived reset would make the expected strings move every run.
    """
    from cc_usage_widget.contracts import AccountRow

    return AccountRow(
        slot=CODEX_PSEUDO_ACCOUNT_SLOT,
        alias="Codex",
        email="",
        is_active=False,
        seven_day_pct=19.0,
        seven_day_resets_at="Sep 12 14:00",
        vendor=VENDOR_CODEX,
        switchable=False,
        plan_type="pro",
        usage_age_seconds=60.0,
        stale_after_seconds=7_200.0,
    )


def test_no_live_codex_source_renders_todays_exact_menu() -> None:
    """With no live source — or one that is not available — nothing moves.

    The rollback switch (SPEC-CODEX 6): zero credentials, ``codex_accounts.py``
    missing, or ``codex_live_quota_enabled`` off must all leave the widget
    byte-for-byte as it shipped. The expected strings below were captured from
    the pre-SPEC-CODEX-6 code at ``HEAD`` before any of this was written, so
    they are a pin against the old build and not a photograph of the new one:
    a change to the header, the bar geometry, the note column, the label width
    or the title fails here rather than being discovered in the menu bar.
    """
    from cc_usage_widget.app import (
        _quota_row_label,
        _window_label_width,
        _quota_windows,
    )

    row = _scanned_codex_row()
    plain = "Codex (pro)   weekly  19%  resets Sep 12 14:00"
    drawn = "Codex (pro)\n   weekly ███▍░░░░░░░░░░░░░░  19%     resets Sep 12 14:00"

    assert _quota_row_label(row) == plain, _quota_row_label(row)
    assert _quota_windows(row) == [("weekly", 19.0, "resets Sep 12 14:00", False)]
    assert _window_label_width((), (row,)) == 6

    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    settings.update({"title_show_icon": False, "title_show_cost": False})
    app = app_mod.CCUsageWidgetApp()
    try:
        snapshot = UiSnapshot(settings=settings, quota_rows=(row,))
        assert app._visible_quota_rows(snapshot) == (row,)
        items = app._quota_items(snapshot)
        assert len(items) == 1 and str(items[0].title) == drawn, [
            str(item.title) for item in items
        ]
        # The title component is off by default and unchanged when switched on:
        # the transcript row still speaks for the active login when no
        # identified row can (there is none here).
        assert app.render_title(snapshot) == "⇄", app.render_title(snapshot)
        on = replace(snapshot, settings={**settings, "title_show_codex_pct": True})
        assert app.render_title(on) == "C19%", app.render_title(on)
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)

    # ...and the same through the worker, with a live source wired but not
    # available: the merge must return the collection untouched, not empty it.
    with tempfile.TemporaryDirectory() as name:
        base = Path(name)
        (base / "projects").mkdir()

        class _Scanned:
            vendor = VENDOR_CODEX

            def available(self) -> bool:
                return True

            def quota_rows(self) -> tuple[Any, ...]:
                return (row,)

        for live in (None, _FakeLiveCodexSource(available=False)):
            sources: tuple[Any, ...] = (_Scanned(),) if live is None else (_Scanned(), live)
            worker = BackgroundWorker(
                publish=lambda _s: None,
                snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
                accounts=None,
                indexer=None,
                rollups=None,
                pricing=None,
                sources=sources,
            )
            assert worker._collect_quota_rows() == (row,), live
            # An unavailable source is never asked for rows, and never counted
            # as a present vendor by itself.
            assert worker.available_vendors == (VENDOR_CODEX,), worker.available_vendors


def test_refresh_and_the_vendor_toggle_reach_the_live_source() -> None:
    """``Refresh now``, ``Codex tracking`` and quitting all reach the poller.

    Each of the three is a promise the menu makes that only the source can
    keep: "answer me now" must not wait out a 300 s period; switching the
    vendor OFF must stop the REQUESTS, not merely hide the rows (a poller
    running behind an OFF switch is the widget lying about what it is doing on
    someone else's endpoint); and a quit must end the thread rather than leave
    it fetching for a menu bar item that is gone.
    """
    from cc_usage_widget.app import _CMD_REFRESH, _CMD_SET_SETTING

    live = _FakeLiveCodexSource(rows=(_scanned_codex_row(),))
    worker = BackgroundWorker(
        publish=lambda _s: None,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        indexer=None,
        rollups=None,
        pricing=None,
        sources=(live,),
    )

    worker.start()
    try:
        assert live.started == 1, live.started
        worker.start()  # the liveness watchdog restarts the worker
        assert live.started == 1, "a second poller would double the request rate"
        # Starting applies the current vendor switch, which is ON by default -
        # once, not once per restart.
        assert live.paused == [False], live.paused

        worker._handle_command((_CMD_REFRESH, None))
        assert live.forced == 1, live.forced

        worker._handle_command((_CMD_SET_SETTING, ("codex_tracking_enabled", False)))
        assert live.paused[-1] is True, live.paused
        worker._handle_command((_CMD_SET_SETTING, ("codex_tracking_enabled", True)))
        assert live.paused[-1] is False, live.paused

        # An unrelated setting must not touch the poller at all.
        before = list(live.paused)
        worker._handle_command((_CMD_SET_SETTING, ("lookback_days", 14)))
        assert live.paused == before, live.paused
    finally:
        assert worker.stop(timeout=2.0)
    assert live.stopped == 1, live.stopped

    # The quit path signals rather than joins (SPEC 2.3) and must still reach it.
    other = _FakeLiveCodexSource()
    quitting = BackgroundWorker(
        publish=lambda _s: None,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        sources=(other,),
    )
    quitting.signal_stop()
    assert other.stopped == 1, other.stopped


def test_an_unavailable_source_gets_no_thread_until_it_is_available() -> None:
    """No poller on a Claude-only machine (review, 2026-09-09): the worker
    starts a source only once ``available()`` says so, re-checked every tick,
    and never twice."""
    live = _FakeLiveCodexSource(available=False)
    worker = BackgroundWorker(
        publish=lambda _s: None,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        indexer=None,
        rollups=None,
        pricing=None,
        sources=(live,),
    )
    worker.start()
    try:
        assert live.started == 0, "started while unavailable"
        worker._collect_quota_rows()
        assert live.started == 0
        live._available = True
        worker._collect_quota_rows()
        assert live.started == 1, "started on the tick after it became available"
        assert live.paused == [False], live.paused
        worker._collect_quota_rows()
        assert live.started == 1, "never twice"
    finally:
        assert worker.stop(timeout=2.0)


def test_a_quota_only_source_is_never_handed_to_the_cost_job() -> None:
    """The live poller has no corpus: it must not appear in _scanners().

    On 2026-09-10 the first tick after onboarding killed the cost job with
    ``AttributeError: 'CodexAccountsSource' object has no attribute
    'progress'`` because _scanners() returned every available source.
    """
    live = _FakeLiveCodexSource(rows=(_scanned_codex_row(),))
    assert not hasattr(live, "scan_once")
    worker = BackgroundWorker(
        publish=lambda _s: None,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        indexer=None,
        rollups=None,
        pricing=None,
        sources=(live,),
    )
    assert worker._scanners() == [], worker._scanners()
    worker._run_cost_job()  # must not raise
    assert worker._collect_quota_rows() == (_scanned_codex_row(),), "still collected as quota"


def test_a_reveal_is_suppressed_by_the_env_guard_and_never_reaches_finder() -> None:
    """The claude-swap venv has PyObjC, so a reveal from a test really opens
    Finder on the desktop (peer report 2026-09-10). Every test module sets the
    guard at import; this pins that the guard is honoured."""
    from cc_usage_widget.app import _NO_REVEAL_ENV, _reveal_in_finder

    assert os.environ.get(_NO_REVEAL_ENV), "the test module must set the guard at import"
    with tempfile.TemporaryDirectory() as name:
        assert _reveal_in_finder(Path(name) / "export.csv") is False


def test_codex_accounts_submenu_appears_only_with_a_registry() -> None:
    """No ``codex_accounts.json``, no live-quota controls anywhere in Settings.

    The same rule ``test_absent_codex_corpus_offers_no_codex_settings`` defends
    one layer down: a control that cannot change anything is worse than no
    control, because it reads as a feature that is broken. Before onboarding
    there is no registry, no credential and nothing to enable — so the Settings
    menu is the pre-SPEC-CODEX-6 one, item for item.

    The registry path is read through the module global (never captured), so
    pointing it at a temporary directory is enough to exercise both states
    without touching the real widget home.
    """
    row = _scanned_codex_row()
    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    entries = (
        _FakeRegistryEntry("acct-aaaa1111", "acme", True),
        _FakeRegistryEntry("acct-bbbb2222", "", False),
    )
    live = _FakeLiveCodexSource(rows=(), entries=entries)

    original = app_mod.CODEX_ACCOUNTS_REGISTRY_PATH
    app = app_mod.CCUsageWidgetApp()
    try:
        app._worker._sources = (live,)
        app._worker._collect_quota_rows()  # populates the main thread's cache
        assert app._worker.codex_accounts == (
            ("acct-aaaa1111", "acme", True),
            ("acct-bbbb2222", "", False),
        ), app._worker.codex_accounts

        snapshot = UiSnapshot(settings=settings, quota_rows=(row,))
        with tempfile.TemporaryDirectory() as name:
            registry = Path(name) / "codex_accounts.json"
            app_mod.CODEX_ACCOUNTS_REGISTRY_PATH = registry

            absent = list(app._settings_submenu(snapshot).keys())
            assert not any(t.startswith("Codex live quota") for t in absent), absent
            assert "Codex accounts" not in absent, absent
            # Nor a diagnostics line about a credential directory that is not
            # there - the source object exists on every machine.
            assert not any(t.startswith("codex accounts:") for t in absent), absent
            # The pre-SPEC-CODEX-6 vendor switch is still there.
            assert any(t.startswith("Codex tracking") for t in absent), absent

            registry.write_text('{"version": 1, "accounts": []}', encoding="utf-8")
            present = list(app._settings_submenu(snapshot).keys())
            assert any(t.startswith("Codex live quota") for t in present), present
            assert "Codex accounts" in present, present

            children = list(app._settings_submenu(snapshot)["Codex accounts"].keys())
            # One row per registry entry, the alias-less one named by the first
            # 8 characters of its own id - not by an invented name.
            assert "acme" in children, children
            assert "acct-bbb" in children, children
            assert any(title.startswith("Poll every") for title in children), children
            assert "Reveal codex_accounts.json" in children, children

            # The switch must not be its own precondition: with credentials
            # but no corpus and no rows yet (live quota is OFF by default, so
            # the source reports nothing), the only way to turn it on would
            # otherwise be to hand-edit settings.json.
            bare = list(app._settings_submenu(replace(snapshot, quota_rows=())).keys())
            assert any(t.startswith("Codex live quota") for t in bare), bare
            assert "Codex accounts" in bare, bare

            # The checkbox reflects the registry, and a click goes to the
            # worker rather than writing the file on the AppKit thread.
            item = app._settings_submenu(snapshot)["Codex accounts"]["acme"]
            assert item.state == 1, item.state
            disabled = app._settings_submenu(snapshot)["Codex accounts"]["acct-bbb"]
            assert disabled.state == 0, disabled.state
            recorded: list[tuple[str, Any]] = []
            app._worker.submit = lambda name, payload=None: recorded.append((name, payload))
            item.callback(item)
            assert recorded == [("set_codex_account", ("acct-aaaa1111", False))], recorded
    finally:
        app_mod.CODEX_ACCOUNTS_REGISTRY_PATH = original
        app._running = False
        app._worker.stop(timeout=2.0)


def test_a_registry_toggle_is_written_once_on_the_worker_thread() -> None:
    """The checkbox click lands on the registry, through the source that owns it.

    Two writers over one atomic file take turns dropping each other's edits
    (the trap ``state.settings_store()`` exists to avoid), so the menu never
    opens ``codex_accounts.json`` itself: it enqueues a command and the worker
    calls ``set_enabled`` on the source's own registry.
    """
    from cc_usage_widget.app import _CMD_SET_CODEX_ACCOUNT

    entries = (_FakeRegistryEntry("acct-aaaa1111", "acme", True),)
    live = _FakeLiveCodexSource(entries=entries)
    published: list[UiSnapshot] = []
    worker = BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        sources=(live,),
    )
    worker._handle_command((_CMD_SET_CODEX_ACCOUNT, ("acct-aaaa1111", False)))
    assert live._registry.set_calls == [("acct-aaaa1111", False)], live._registry.set_calls
    assert published, "the rows must be republished so the checkmark can move"


def test_source_diagnostics_reach_the_menu_without_touching_the_disk() -> None:
    """The source's own diagnostics lines show up under Settings.

    Read from the worker's cache, produced on the worker thread: a diagnostics
    line that stats a credential directory while the menu is being assembled
    would put I/O back on the AppKit thread (SPEC 2.3), which is the one thing
    the whole snapshot design exists to prevent.
    """
    live = _FakeLiveCodexSource(rows=(_scanned_codex_row(),))
    original = app_mod.CODEX_ACCOUNTS_REGISTRY_PATH
    app = app_mod.CCUsageWidgetApp()
    try:
        snapshot = UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS)))
        app._worker._sources = (live,)
        with tempfile.TemporaryDirectory() as name:
            registry = Path(name) / "codex_accounts.json"
            app_mod.CODEX_ACCOUNTS_REGISTRY_PATH = registry

            # No registry: the feature is not in use and says nothing at all.
            app._worker._collect_quota_rows()
            assert not any(
                "codex accounts" in str(item.title)
                for item in app._diagnostic_items(snapshot)
            ), "a Claude-only machine must gain no diagnostics line"

            registry.write_text('{"version": 1, "accounts": []}', encoding="utf-8")
            app._worker._collect_quota_rows()
            titles = [str(item.title) for item in app._diagnostic_items(snapshot)]
            assert any("codex accounts: 2 tracked" in title for title in titles), titles
    finally:
        app_mod.CODEX_ACCOUNTS_REGISTRY_PATH = original
        app._running = False
        app._worker.stop(timeout=2.0)


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
    orphans = _uncollected_tests(tests)
    if orphans:
        # Fail loud: an all-green run that quietly skipped a test is the one
        # outcome worse than a red one.
        print(
            f"ERROR: {len(orphans)} test(s) are defined in this file but were never "
            f"collected — they sit below the `if __name__` guard: " + ", ".join(orphans)
        )
    return 1 if (failures or orphans) else 0


# ---------------------------------------------------------------------------
# Roadmap item 3 - unpriced volume, attributed
# ---------------------------------------------------------------------------


def _breakdown_with_unpriced(root: Path, *, claude: bool) -> Any:
    """A finished breakdown holding 500k unpriced Codex tokens, +/- Claude."""
    today = local_day_key(time.time())
    models: dict[str, ModelUsage] = {
        f"{VENDOR_CODEX}:gpt-5.6-sol": ModelUsage(input=1_000_000, output=100_000),
        f"{VENDOR_CODEX}:codex-auto-review": ModelUsage(input=500_000),
    }
    if claude:
        models["claude-fable-5"] = ModelUsage(input=1_000_000, output=100_000)
    store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
    store.merge([DayRollup(day=today, models=models)])
    return store.cost_breakdown(
        DEFAULT_PRICING, today=today, progress=IndexProgress(complete=True)
    )


def test_unpriced_volume_is_attributed_to_its_vendor_when_both_are_present() -> None:
    """``codex-auto-review`` is 916M tokens a week that OpenAI publishes no rate
    for. Priced at $0 and unattributed it read as "someone's" - on a two-vendor
    machine the line must say whose, and must still never print a dollar.

    A one-vendor machine keeps the pre-existing line byte for byte: there is
    nothing to attribute and a prefix would be noise.
    """
    scale = f" ({format_tokens(500_000)} tok/30d)"
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        app = app_mod.CCUsageWidgetApp()
        try:
            both = app._unpriced_items(_breakdown_with_unpriced(root, claude=True))
            lines = [str(item.title) for item in both]
            assert lines == [
                f"  Codex unpriced at $0{scale}: codex-auto-review"
            ], lines

            codex_only = app._unpriced_items(
                _breakdown_with_unpriced(root / "solo", claude=False)
            )
            solo = [str(item.title) for item in codex_only]
            assert solo == [f"  unpriced at $0{scale}: codex-auto-review"], solo
            for line in lines + solo:
                assert "$0" in line and "$0." not in line, line
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_a_fully_priced_window_renders_no_unpriced_line_at_all() -> None:
    """The line is a floor marker, not a permanent row: nothing unpriced,
    nothing rendered - on one vendor or on two."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        today = local_day_key(time.time())
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.merge(
            [
                DayRollup(
                    day=today,
                    models={
                        "claude-fable-5": ModelUsage(input=1_000_000),
                        f"{VENDOR_CODEX}:gpt-5.6-sol": ModelUsage(input=1_000_000),
                    },
                )
            ]
        )
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today=today, progress=IndexProgress(complete=True)
        )
        assert breakdown.last_30d.vendor_unpriced == ()
        assert breakdown.unknown_models_by_vendor == ()
        app = app_mod.CCUsageWidgetApp()
        try:
            assert app._unpriced_items(breakdown) == []
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_unpriced_tokens_split_per_vendor_and_per_window() -> None:
    """The split is per window, not one number reused: a model unpriced only
    yesterday must not show up in ``Today``."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        today = local_day_key(time.time())
        yesterday = local_day_key(time.time() - 86_400)
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.merge(
            [
                DayRollup(
                    day=yesterday,
                    models={
                        f"{VENDOR_CODEX}:codex-auto-review": ModelUsage(input=500_000)
                    },
                ),
                DayRollup(
                    day=today,
                    models={"claude-quantum-9": ModelUsage(input=7_000)},
                ),
            ]
        )
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today=today, progress=IndexProgress(complete=True)
        )
        assert breakdown.today.vendor_unpriced == ((VENDOR_CLAUDE, 7_000),)
        assert breakdown.last_30d.vendor_unpriced == (
            (VENDOR_CLAUDE, 7_000),
            (VENDOR_CODEX, 500_000),
        ), breakdown.last_30d.vendor_unpriced
        assert breakdown.today.unpriced_for_vendor(VENDOR_CODEX) == 0
        assert breakdown.last_30d.unpriced_for_vendor(VENDOR_CODEX) == 500_000
        assert breakdown.unknown_models_for_vendor(VENDOR_CODEX) == (
            "codex-auto-review",
        )
        assert breakdown.unknown_models_for_vendor(VENDOR_CLAUDE) == ("claude-quantum-9",)


# ---------------------------------------------------------------------------
# Roadmap item 2 - the daily self-audit tick
#
# The store is an aggregate nothing re-derives. Before the ledger it was
# add-only and a re-read doubled a cell; live figures were inflated up to
# 1,650x for WEEKS with nothing in the widget noticing. This is the part that
# notices: once a day, re-index the last two days of every corpus into a
# TemporaryDirectory with the same indexer classes, compare cell by cell, and
# repair what disagrees.
#
# Every case here builds a REAL corpus - the record shapes the cost and Codex
# suites already use - and runs the real indexers over it.
# ---------------------------------------------------------------------------


def _audit_fixture(root: Path) -> tuple[Any, list[tuple[str, Any]], str]:
    """A both-vendor corpus, indexed once. Returns (store, scanners, today)."""
    now = time.time()
    _write(
        root / "projects" / "p" / "session.jsonl",
        [
            _record("req-a", now, input_tokens=400_000, output_tokens=20_000),
            _record("req-b", now, input_tokens=100_000, output_tokens=5_000),
        ],
    )
    _codex_rollout(root / "sessions", _codex_turns("gpt-5.6-sol", 40, tokens=20_000))
    indexer = Indexer(
        projects_dir=root / "projects",
        state_path=root / "scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
    )
    codex = CodexIndexer(
        sessions_dir=root / "sessions",
        state_path=root / "codex_scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
    )
    store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
    scanners: list[tuple[str, Any]] = [("claude", indexer), (VENDOR_CODEX, codex)]
    for _vendor, scanner in scanners:
        for _ in range(20):
            result = scanner.scan_once()
            store.retract_rollups(result.retractions)
            store.merge(result.deltas)
            if scanner.progress().complete:
                break
    return store, scanners, local_day_key(time.time())


def test_a_clean_store_audits_to_no_note_at_all() -> None:
    """A store that matches its corpus must say nothing.

    A `!` line that is always on is a `!` line nobody reads, so "no drift" has
    to be silent - and it has to be silent over a REAL two-vendor corpus, not
    an empty one, or the test proves only that zero equals zero.
    """
    from cc_usage_widget.audit import SelfAudit

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, scanners, today = _audit_fixture(root)
        assert store.today(today).total.total_tokens > 0, "the fixture indexed nothing"
        audit = SelfAudit.beside(store.path)
        result = audit.run(store, scanners, today=today)
        assert result.error is None, result.error
        assert result.cells_compared > 0, result
        assert result.drifted == (), result.drifted
        assert result.note is None, result.note
        assert result.log_line.startswith("audit: 0 drift"), result.log_line


def test_an_inflated_cell_is_named_and_repaired() -> None:
    """The 1,650x incident, in miniature: a cell the corpus does not support is
    detected, named in the note, and put back to what the corpus says."""
    from cc_usage_widget.audit import SelfAudit

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, scanners, today = _audit_fixture(root)
        key = f"{VENDOR_CODEX}:gpt-5.6-sol"
        truth = store.today(today).models[key]
        assert truth.total_tokens > 0, truth
        # Double it, exactly as a re-read from byte 0 used to.
        store.add(today, key, truth)
        assert store.today(today).models[key].total_tokens == truth.total_tokens * 2

        audit = SelfAudit.beside(store.path)
        result = audit.run(store, scanners, today=today)
        assert result.error is None, result.error
        assert len(result.drifted) == 1, result.drifted
        cell = result.drifted[0]
        assert cell.key == key and cell.day == today, cell
        assert cell.ratio is not None and abs(cell.ratio - 2.0) < 0.01, cell.ratio
        assert result.note is not None and "gpt-5.6-sol" in result.note, result.note
        # NOT "rebuilt": nothing has been. The audit thread only produces a
        # plan, and the worker may still drop it (a rebuild in between, a store
        # that cannot repair a day, a repair that raised). The verdict is
        # rewritten by `_apply_audit_repairs` once it knows (SPEC 3.2a).
        assert result.note.endswith("— repair pending"), result.note

        # The audit itself changed NOTHING: it runs on a daemon thread of its
        # own and the store belongs to the worker (roadmap item 2, hardened
        # 2026-09-10). What it produces is a plan.
        assert store.today(today).models[key].total_tokens == truth.total_tokens * 2
        assert len(result.repairs) == 1, result.repairs
        repair = result.repairs[0]
        assert (repair.day, repair.vendor) == (today, VENDOR_CODEX), repair
        assert repair.fresh_models[key] == truth, repair.fresh_models

        # Applied the way the worker applies it: one atomic store call.
        store.replace_day_for_vendor(
            repair.day,
            repair.vendor,
            repair.fresh_models,
            observed=repair.observed_models,
        )
        # Repaired to the corpus, and the OTHER vendor left untouched.
        assert store.today(today).models[key] == truth, store.today(today)
        claude = [k for k in store.today(today).models if not k.startswith(f"{VENDOR_CODEX}:")]
        assert claude, store.today(today).models


def test_small_and_proportionally_tiny_differences_are_not_drift() -> None:
    """Both gates, on a real corpus. A cell must differ by MORE than 100k
    tokens AND by more than 0.5 % before it is called drift.

    Neither alone is enough. A record written between the live scan and the
    audit scan moves a cell honestly, and an audit that fires on that is an
    audit whose `!` line the operator learns to ignore - which is how the
    1,650x inflation survived weeks of daily looking in the first place.
    """
    from cc_usage_widget.audit import AUDIT_DRIFT_MIN_TOKENS, SelfAudit

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        # A deliberately LARGE codex cell, so 0.5 % of it exceeds the absolute
        # floor and the two gates can be told apart.
        _codex_rollout(
            root / "sessions",
            _codex_turns("gpt-5.6-sol", 40, tokens=700_000)
            # ...and a SMALL cell beside it, where 0.5 % is a few hundred
            # tokens. Without the absolute floor this one would drift on any
            # rounding; without the relative gate the large one would not drift
            # on a real doubling. Both gates need a cell that isolates them.
            + _codex_turns("gpt-5.4-mini", 4, tokens=10_000),
        )
        codex = CodexIndexer(
            sessions_dir=root / "sessions",
            state_path=root / "codex_scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        scanners = [(VENDOR_CODEX, codex)]
        for _ in range(20):
            result = codex.scan_once()
            store.merge(result.deltas)
            if codex.progress().complete:
                break
        today = local_day_key(time.time())
        key = f"{VENDOR_CODEX}:gpt-5.6-sol"
        truth = store.today(today).models[key]
        assert truth.total_tokens == 40 * 700_000, truth
        relative_floor = truth.total_tokens * 0.005
        assert relative_floor > AUDIT_DRIFT_MIN_TOKENS, relative_floor

        # Under the absolute floor: too small to be worth a word.
        store.add(today, key, ModelUsage(input=AUDIT_DRIFT_MIN_TOKENS - 1))
        result = SelfAudit.beside(store.path).run(store, scanners, today=today)
        assert result.drifted == (), result.drifted
        assert result.note is None

        # Over the absolute floor but under 0.5 % of this cell: still not drift,
        # and the store is left exactly as it was.
        store.add(today, key, ModelUsage(input=20_001))
        inflated = store.today(today).models[key]
        assert AUDIT_DRIFT_MIN_TOKENS < inflated.total_tokens - truth.total_tokens
        assert inflated.total_tokens - truth.total_tokens < relative_floor
        result = SelfAudit.beside(store.path).run(store, scanners, today=today)
        assert result.drifted == (), result.drifted
        assert store.today(today).models[key] == inflated, "a non-drift was repaired"

        # The small cell: 50k tokens is a LOT of it proportionally (0.5 % is a
        # few hundred) and still under the absolute floor, so it is not drift.
        small_key = f"{VENDOR_CODEX}:gpt-5.4-mini"
        small = store.today(today).models[small_key]
        assert small.total_tokens == 4 * 10_000, small
        store.add(today, small_key, ModelUsage(input=AUDIT_DRIFT_MIN_TOKENS // 2))
        grown = store.today(today).models[small_key]
        assert grown.total_tokens - small.total_tokens > small.total_tokens * 0.005
        result = SelfAudit.beside(store.path).run(store, scanners, today=today)
        assert result.drifted == (), result.drifted
        assert store.today(today).models[small_key] == grown, "a non-drift was repaired"


def test_the_audit_never_repairs_from_an_unfinished_reindex() -> None:
    """A twin that could not finish reads as "the store has far too much".
    Repairing from it would destroy real usage, so it must change nothing."""
    from cc_usage_widget.audit import SelfAudit

    class _NeverFinishes:
        """A scanner whose twin reports progress it never completes."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def clone_for_audit(self, state_dir: Any, *, lookback_days: int) -> Any:
            return self

        def scan_once(self, *, deadline: float | None = None) -> Any:
            return self._inner.__class__.__mro__ and _EMPTY_SCAN

        def progress(self) -> Any:
            return IndexProgress(files_done=1, files_total=9, complete=False)

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, scanners, today = _audit_fixture(root)
        before = store.today(today)
        audit = SelfAudit.beside(store.path, budget_seconds=1.0)
        result = audit.run(store, [("claude", _NeverFinishes(scanners[0][1]))], today=today)
        assert result.error is not None, result
        assert "did not finish" in result.error, result.error
        assert result.drifted == (), result.drifted
        assert store.today(today) == before, "an unfinished audit changed the store"
        assert result.note is not None and "could not complete" in result.note


def test_a_deleted_transcripts_day_is_not_audited_away() -> None:
    """A file deleted today is exactly what a naive audit destroys.

    The live store keeps a pruned file's tokens on purpose - they exist nowhere
    else - so a fresh index of what is on disk NOW legitimately holds less. The
    audit reconciles with the scanners' tombstones before it compares; without
    that it would read the gap as drift and "repair" real, unrecoverable usage.
    """
    from cc_usage_widget.audit import SelfAudit

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        doomed = _write(
            root / "projects" / "p" / "doomed.jsonl",
            [_record("req-gone", now, input_tokens=900_000, output_tokens=40_000)],
        )
        store, scanners, today = _audit_fixture(root)
        before = store.today(today)
        assert before.total.input >= 900_000, before

        doomed.unlink()
        # One more pass turns the entry into a tombstone.
        indexer = scanners[0][1]
        for _ in range(5):
            result = indexer.scan_once()
            store.retract_rollups(result.retractions)
            store.merge(result.deltas)
            if indexer.progress().complete:
                break
        assert store.today(today) == before, "the pruned file was retracted"

        result = SelfAudit.beside(store.path).run(store, scanners, today=today)
        assert result.error is None, result.error
        assert result.drifted == (), result.drifted
        assert store.today(today) == before, "the audit deleted a pruned day"


def test_the_audit_runs_once_a_day_and_says_when_it_last_ran() -> None:
    """The sidecar is what stops a 300 s cost cadence auditing 288 times a day,
    and what the Settings line reads."""
    from cc_usage_widget.audit import AuditResult, SelfAudit

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        audit = SelfAudit(state_path=root / "audit_state.json")
        assert audit.due(today="2026-09-10")
        assert audit.status_label() is None, "no audit has run yet"

        audit.mark_ran(AuditResult(ran_at=time.time()), today="2026-09-10")
        assert not audit.due(today="2026-09-10")
        assert audit.due(today="2026-09-11"), "a new local day is a new audit"
        label = audit.status_label()
        assert label is not None and label.endswith("· 0 drift"), label

        # A restart re-reads the sidecar rather than auditing again.
        again = SelfAudit(state_path=root / "audit_state.json")
        assert not again.due(today="2026-09-10")
        assert again.last_drift_cells == 0

        again.mark_ran(
            AuditResult(ran_at=time.time(), error="codex re-index did not finish"),
            today="2026-09-11",
        )
        third = SelfAudit(state_path=root / "audit_state.json")
        assert third.status_label().endswith("· incomplete"), third.status_label()


def test_the_self_audit_off_switch_reads_no_corpus_at_all() -> None:
    """Off means off: no thread, no second read of the corpus, no sidecar, and
    no Settings line claiming an audit that never ran.

    Every feature here has an off switch, and a switch that only stops the
    *note* while the re-index still runs would be the expensive half left on.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", time.time(), input_tokens=200_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "self_audit_enabled": False}
            ),
        )
        try:
            _drain_cost(worker)
            assert indexer.progress().complete
            assert worker._audit_thread is None, "the off switch did not stop the audit"
            assert worker._audit is None, "an audit was constructed anyway"
            assert not (root / "audit_state.json").exists()
            assert worker.audit_status_label() is None
            assert worker._snapshot.audit_note is None
        finally:
            worker.stop(timeout=5.0)


def test_a_partial_first_index_is_never_audited() -> None:
    """While the first index is still filling in, the live store is KNOWN to be
    incomplete. Auditing it would call every unread file's day drift and
    "repair" it by deleting what the indexer had not reached yet - the audit
    fighting the indexer, on the one path where the widget is already slow.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        for index in range(4):
            _write(
                root / "projects" / "p" / f"s{index}.jsonl",
                [_record(f"req-{index}", now, input_tokens=200_000)],
            )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
            chunk_files=1,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        try:
            worker._run_cost_job()
            assert not indexer.progress().complete, "the fixture indexed in one pass"
            assert worker._audit_thread is None, "a partial index was audited"

            for _ in range(20):
                worker._run_cost_job()
                if indexer.progress().complete:
                    break
            assert indexer.progress().complete
            assert worker._audit_thread is not None, "a finished index was not audited"
            worker._audit_thread.join(timeout=60.0)
        finally:
            worker.stop(timeout=5.0)


def test_the_cost_job_actually_fires_the_audit_and_publishes_its_verdict() -> None:
    """The wiring, not the module. An audit nobody calls is the failure mode
    this whole item exists to end, so the assertion goes through
    ``_run_cost_job`` on a real two-vendor worker rather than calling
    ``SelfAudit.run`` directly.

    The verdict is published on the NEXT tick on purpose: the audit runs on its
    own daemon thread so a slow corpus cannot starve the 60 s accounts tick, and
    a read-modify-write of the snapshot from that thread would race the worker's
    own publishes.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        _write(
            root / "projects" / "p" / "session.jsonl",
            [_record("req-a", now, input_tokens=400_000, output_tokens=20_000)],
        )
        _codex_rollout(root / "sessions", _codex_turns("gpt-5.6-sol", 40, tokens=20_000))
        worker, indexer, codex = _two_vendor_worker(root)
        try:
            _drain_both(worker, [indexer, codex])
            store = worker._rollups
            today = local_day_key(time.time())
            key = f"{VENDOR_CODEX}:gpt-5.6-sol"
            truth = store.today(today).models[key]
            assert truth.total_tokens > 0, truth

            # Today's audit already ran while draining and found nothing. Put
            # the day back on the clock and inflate a cell the way a re-read
            # from byte 0 used to.
            (root / "audit_state.json").unlink(missing_ok=True)
            worker._audit = None
            store.add(today, key, truth)

            worker._run_cost_job()
            thread = worker._audit_thread
            assert thread is not None, "the cost job never started an audit"
            thread.join(timeout=60.0)
            assert not thread.is_alive(), "the audit did not finish"
            # The audit thread computes; it must NOT have touched the store.
            assert store.today(today).models[key].total_tokens == truth.total_tokens * 2
            assert worker._audit_repairs, "the audit produced no repair plan"

            worker._run_cost_job()
            assert store.today(today).models[key] == truth, "the cell was not repaired"
            assert worker._audit_repairs == (), "the plan was applied twice"
            note = worker._snapshot.audit_note
            assert note is not None and "gpt-5.6-sol" in note, note
            # Only NOW may it say "rebuilt" - the plan has landed. The suffix
            # is roadmap item 6's half of the repair: the per-project split of
            # a rebuilt day cannot be reconstructed from a per-(day, model)
            # plan, so it is reset and the note says why.
            assert note.startswith("audit: 1 cell drifted"), note
            assert "— rebuilt" in note, note
            assert f"project split for {today} reset" in note, note
            assert worker.audit_status_label() is not None
            # And it does not audit twice in one local day.
            worker._run_cost_job()
            assert worker._audit_thread is thread or not worker._audit_thread.is_alive()
            assert not worker._audit.due(today=today)
        finally:
            worker.stop(timeout=5.0)


def test_the_audit_note_reaches_the_cost_section_and_a_clean_one_does_not() -> None:
    """The verdict is money, so it renders with the money - and only when there
    is one."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        today = local_day_key(time.time())
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.merge(
            [DayRollup(day=today, models={"claude-fable-5": ModelUsage(input=1_000)})]
        )
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today=today, progress=IndexProgress(complete=True)
        )
        settings = normalize_settings(dict(SETTINGS_DEFAULTS))
        note = "audit: 1 cell drifted (codex 2026-09-09 gpt-5.6-sol 12.7x) — rebuilt"
        app = app_mod.CCUsageWidgetApp()
        try:
            # Real wiring, not a stand-in: `_cost_items` renders "unavailable"
            # until the worker actually holds a scanner, a store and prices.
            app._worker._rollups = store
            app._worker._pricing = DEFAULT_PRICING
            app._worker._indexer = Indexer(
                projects_dir=root / "projects",
                state_path=root / "scan_state.json",
                lookback_days=30,
                pricing=DEFAULT_PRICING,
            )
            noisy = app._cost_items(
                UiSnapshot(settings=settings, cost=breakdown, audit_note=note)
            )
            assert any(str(item.title) == f"! {note}" for item in noisy), [
                str(item.title) for item in noisy
            ]
            quiet = app._cost_items(UiSnapshot(settings=settings, cost=breakdown))
            assert not any("audit:" in str(item.title) for item in quiet), [
                str(item.title) for item in quiet
            ]
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Attribution: two menu blocks that must be absent unless asked for (item 6)
# ---------------------------------------------------------------------------


def _cost_app(root: Path, store: DailyRollupStore) -> Any:
    """A real app whose worker holds a real store, scanner and price table.

    ``_cost_items`` renders "unavailable" until all three are present, so a
    byte-for-byte comparison against a stand-in would compare two error lines.
    """
    app = app_mod.CCUsageWidgetApp()
    app._worker._rollups = store
    app._worker._pricing = DEFAULT_PRICING
    app._worker._indexer = Indexer(
        projects_dir=root / "projects",
        state_path=root / "scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
    )
    return app


def test_the_cost_section_is_byte_for_byte_unchanged_when_attribution_is_off() -> None:
    """The off switch, measured where a user would notice it.

    Not "roughly the same": the same list of strings. A machine with
    ``cost_by_project_enabled`` false must draw the Cost section it drew before
    roadmap item 6 existed, even with rows sitting on the worker.
    """
    from cc_usage_widget.attribution import AttributionRow

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        today = local_day_key(time.time())
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.merge([DayRollup(day=today, models={FABLE: ModelUsage(input=1_000)})])
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today=today, progress=COMPLETE
        )
        app = _cost_app(root, store)
        try:
            rows = (
                AttributionRow(
                    vendor=VENDOR_CLAUDE,
                    project="cc-usage-widget",
                    session="",
                    usage=ModelUsage(input=1_000),
                    usd=8.9,
                    unpriced_tokens=0,
                ),
            )
            app._worker._cost_project_rows = rows
            app._worker._cost_session_rows = rows

            off = normalize_settings(
                {**SETTINGS_DEFAULTS, "cost_by_project_enabled": False}
            )
            on = normalize_settings({**SETTINGS_DEFAULTS, "cost_by_project_enabled": True})
            drawn_off = [
                str(item.title)
                for item in app._cost_items(UiSnapshot(settings=off, cost=breakdown))
            ]
            drawn_on = [
                str(item.title)
                for item in app._cost_items(UiSnapshot(settings=on, cost=breakdown))
            ]
            assert not any("by project" in line for line in drawn_off), drawn_off
            assert any("by project" in line for line in drawn_on), drawn_on
            # ... and the difference is EXACTLY the new block, in place.
            assert [line for line in drawn_on if line in drawn_off] == drawn_off

            # The same section with nothing to attribute: also byte for byte.
            app._worker._cost_project_rows = ()
            app._worker._cost_session_rows = ()
            empty = [
                str(item.title)
                for item in app._cost_items(UiSnapshot(settings=on, cost=breakdown))
            ]
            assert empty == drawn_off, (empty, drawn_off)
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_both_attribution_blocks_render_their_rows_with_a_heading() -> None:
    """Present when there is data: a heading carrying the subtotal, then rows."""
    from cc_usage_widget.attribution import AttributionRow

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        today = local_day_key(time.time())
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.merge([DayRollup(day=today, models={FABLE: ModelUsage(input=1_000)})])
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today=today, progress=COMPLETE
        )
        app = _cost_app(root, store)
        try:
            # The Export items are gated on the mirror holding rows (roadmap 8
            # / integration fix 1) and this test asserts the blocks sit ABOVE
            # them, so give it a mirror with something in it.
            app._worker._history_rows = 1
            app._worker._cost_project_rows = (
                AttributionRow(
                    vendor=VENDOR_CLAUDE,
                    project="cc-usage-widget",
                    session="",
                    usage=ModelUsage(input=41_200_000),
                    usd=8.9,
                    unpriced_tokens=0,
                ),
            )
            app._worker._cost_session_rows = (
                AttributionRow(
                    vendor=VENDOR_CLAUDE,
                    project="cc-usage-widget",
                    session="5909f788-7a20-41b8",
                    usage=ModelUsage(input=2_100_000),
                    usd=6.54,
                    unpriced_tokens=0,
                ),
            )
            drawn = [
                str(item.title)
                for item in app._cost_items(
                    UiSnapshot(
                        settings=normalize_settings(dict(SETTINGS_DEFAULTS)),
                        cost=breakdown,
                    )
                )
            ]
            assert any("── today by project" in line and "$8.90" in line for line in drawn), drawn
            assert any("cc-usage-widget" in line and "41.2M tok" in line for line in drawn), drawn
            assert any("── today's top sessions" in line for line in drawn), drawn
            # The session row shows the id AND the project, not one or the other.
            assert any("5909f788" in line and "cc-usage-wid" in line for line in drawn), drawn
            # The blocks sit under the money, above the exports.
            first_block = next(i for i, line in enumerate(drawn) if "by project" in line)
            export = next(i for i, line in enumerate(drawn) if line.startswith("Export"))
            assert first_block < export, drawn
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_a_real_cost_job_fills_the_attribution_store_and_the_menu_rows() -> None:
    """The whole wiring, through ``_run_cost_job``: files in, two blocks out."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        record = _record("attr-1", now, input_tokens=4_000, output_tokens=1_000)
        record["cwd"] = "/Users/v/Desktop/Obsidian"
        _write(root / "projects" / "-Users-v-Desktop-Obsidian" / "sess-a.jsonl", [record])
        _codex_rollout(root / "sessions", _codex_turns("gpt-5.6-sol", 3, tokens=2_000))
        worker, indexer, codex = _two_vendor_worker(root)
        try:
            _drain_both(worker, [indexer, codex])
            assert (root / "attribution.json").exists(), (
                "the cache must live beside rollups.json, not in the real widget home"
            )
            projects = {row.project for row in worker.cost_project_rows}
            assert "Obsidian" in projects, projects
            assert worker.cost_session_rows, "no session rows were published"
            # The partition invariant, through the real job.
            total = worker._rollups.today(today).total.total_tokens
            assert sum(row.total_tokens for row in worker.cost_project_rows) == total

            # And a rebuild does not double it.
            worker._rebuild_index()
            assert worker.cost_project_rows == (), "the rebuild must clear the rows"
            _drain_both(worker, [indexer, codex])
            assert sum(row.total_tokens for row in worker.cost_project_rows) == (
                worker._rollups.today(today).total.total_tokens
            )
        finally:
            worker.stop(timeout=5.0)


def test_attribution_off_writes_no_cache_file_at_all() -> None:
    """Off means off from every direction, including the disk."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        record = _record("attr-2", now, input_tokens=4_000)
        record["cwd"] = "/Users/v/Desktop/Obsidian"
        _write(root / "projects" / "-Users-v-Desktop-Obsidian" / "sess-a.jsonl", [record])
        worker, indexer, codex = _two_vendor_worker(root, codex_on=False)
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "cost_by_project_enabled": False}
            ),
        )
        try:
            for _ in range(40):
                worker._run_cost_job()
                if indexer.progress().complete:
                    break
            assert not (root / "attribution.json").exists(), "the off switch wrote a file"
            assert worker.cost_project_rows == ()
            assert not indexer.attribution_enabled
        finally:
            worker.stop(timeout=5.0)


def test_the_settings_menu_carries_the_cost_by_project_switch() -> None:
    """The off switch has to be reachable without editing settings.json.

    It is the only setting that decides whether a name taken out of a
    transcript reaches the disk, so "the key exists in SETTINGS_DEFAULTS" is
    not enough: a control a user cannot find is a control they do not have.
    """
    app = app_mod.CCUsageWidgetApp()
    try:
        for value in (True, False):
            snapshot = UiSnapshot(
                settings=normalize_settings(
                    {**SETTINGS_DEFAULTS, "cost_by_project_enabled": value}
                )
            )
            titles = list(app._settings_submenu(snapshot).keys())
            # The exact label, not a prefix: `_switch_label` pads the name to a
            # fixed column, so a prefix match would accept a renamed control
            # and this test exists to pin the name a user reads.
            wanted = app._switch_label("Cost by project", value)
            assert wanted in titles, (wanted, titles)
            assert ("ON" in wanted) is value, wanted
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Roadmap item 16 - the compact title
# ---------------------------------------------------------------------------


def test_the_compact_title_is_two_figures_and_never_wider_than_its_budget() -> None:
    """``V·C 100/100`` for a narrow menu bar (roadmap item 16).

    A different title, not another component: with ``title_compact`` on the
    ``title_show_*`` toggles are inert, because none of those components is
    rendered. What it must NOT lose in the shrink is the honesty rules - a
    standing note replaces the figure it invalidates, an unreported window
    contributes nothing rather than a zero, and 99.5% is still 99.
    """
    from cc_usage_widget import render

    settings = _fleet_settings(title_compact=True)
    claude = _fleet_rows((1, "vlad", 100.0, None))[0]
    codex = replace(_scanned_codex_row(), seven_day_pct=100.0)
    app = app_mod.CCUsageWidgetApp()
    try:
        both = UiSnapshot(
            settings=settings, accounts=(claude,), active=claude, quota_rows=(codex,)
        )
        title = app.render_title(both)
        assert title == "V·C 100/100", title
        # The budget is measured, not assumed: every previous title widening
        # was also "just two more characters".
        assert len(title) <= render.COMPACT_TITLE_MAX, (title, len(title))

        # One vendor each way - initials and figures stay in step.
        claude_only = UiSnapshot(settings=settings, accounts=(claude,), active=claude)
        assert app.render_title(claude_only) == "V 100"
        assert app.render_title(UiSnapshot(settings=settings, quota_rows=(codex,))) == "C 100"

        # Neither: the same fallback the full title makes, never an invented
        # glyph or a zero.
        assert app.render_title(UiSnapshot(settings=settings)) == app_mod.TITLE_ICON

        # A derived state REPLACES the figure it invalidates (SPEC 4.3); the
        # other vendor's number is untouched.
        noted = replace(both, account_notes={1: "re-login required"})
        assert app.render_title(noted) == "V·C ⚠/100", app.render_title(noted)

        # The engine's standing verdict keeps its glyph - the one exception the
        # full title makes too - and only the glyph, never the word.
        alerted = replace(both, alert=(ALERT_ALL_EXHAUSTED, "all accounts exhausted"))
        assert app.render_title(alerted) == "V·C 100/100 ⛔", app.render_title(alerted)
        assert len(app.render_title(alerted)) <= render.COMPACT_TITLE_MAX + 2

        # 99.x floors to 99 here exactly as it does in the menu, and an
        # unreported window is absent rather than 0.
        near = replace(
            both,
            accounts=(replace(claude, five_hour_pct=99.6),),
            active=replace(claude, five_hour_pct=99.6),
        )
        assert app.render_title(near) == "V·C 99/100", app.render_title(near)
        blank = replace(both, accounts=(replace(claude, five_hour_pct=None),),
                        active=replace(claude, five_hour_pct=None))
        assert app.render_title(blank) == "C 100", app.render_title(blank)

        # Off (the default): the SPEC 4.1 title, byte for byte.
        assert SETTINGS_DEFAULTS["title_compact"] is False
        full = replace(both, settings=_fleet_settings())
        assert app.render_title(full) == "vlad 100%(!) 0/1", app.render_title(full)
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_compact_switch_is_in_the_title_submenu_and_writes_the_setting() -> None:
    """It lives with the components it replaces, and a click goes to the worker
    like every other setting - the AppKit thread writes no file."""
    settings = _fleet_settings()
    app = app_mod.CCUsageWidgetApp()
    try:
        snapshot = UiSnapshot(settings=settings)
        title_menu = app._settings_submenu(snapshot)["Title"]
        assert app_mod._COMPACT_TITLE_LABEL in list(title_menu.keys()), list(title_menu.keys())
        item = title_menu[app_mod._COMPACT_TITLE_LABEL]
        assert item.state == 0, "compact is off by default"
        recorded: list[tuple[str, Any]] = []
        app._worker.submit = lambda name, payload=None: recorded.append((name, payload))
        item.callback(item)
        assert recorded == [("set_setting", ("title_compact", True))], recorded
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Wave-1 leftovers: the settings that had no control, and the tier note
# ---------------------------------------------------------------------------


def test_every_wave_one_setting_has_a_control_and_a_claude_only_machine_sees_none() -> None:
    """Roadmap 8/10/11's switches reach the menu - behind their own vendor.

    ``history_enabled`` is cost-side and appears wherever the cost section
    does; the three Codex keys appear only where Codex does, so a Claude-only
    machine's Settings menu is byte-for-byte the one it had before wave 1.
    """
    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    row = _scanned_codex_row()
    original = app_mod.CODEX_ACCOUNTS_REGISTRY_PATH
    app = app_mod.CCUsageWidgetApp()
    try:
        # A Claude-only machine: no quota rows, no vendors, no registry.
        with tempfile.TemporaryDirectory() as name:
            app_mod.CODEX_ACCOUNTS_REGISTRY_PATH = Path(name) / "codex_accounts.json"
            bare = UiSnapshot(settings=settings)
            titles = list(app._settings_submenu(bare).keys())
            for unexpected in ("Codex fleet line", "Codex pace forecast", "Pricing tier"):
                assert not any(t.startswith(unexpected) for t in titles), (unexpected, titles)
            # No cost modules wired either, so not even the history switch.
            assert not any(t.startswith("History") for t in titles), titles

            # With Codex present the tier chooser appears; the two live-source
            # switches wait for a registry, because neither draws anything
            # without a live row.
            withcodex = UiSnapshot(settings=settings, quota_rows=(row,))
            titles = list(app._settings_submenu(withcodex).keys())
            assert any(t.startswith("Pricing tier: standard") for t in titles), titles
            assert not any(t.startswith("Codex fleet line") for t in titles), titles

            (Path(name) / "codex_accounts.json").write_text(
                '{"version": 1, "accounts": []}', encoding="utf-8"
            )
            titles = list(app._settings_submenu(withcodex).keys())
            assert any(t.startswith("Codex fleet line") for t in titles), titles
            assert any(t.startswith("Codex pace forecast") for t in titles), titles

            # The tier is an enum, and picking one goes to the worker.
            tier_menu = app._settings_submenu(withcodex)["Pricing tier: standard"]
            assert list(tier_menu.keys()) == ["standard", "fast", "batch"], list(tier_menu.keys())
            recorded: list[tuple[str, Any]] = []
            app._worker.submit = lambda n, payload=None: recorded.append((n, payload))
            tier_menu["fast"].callback(tier_menu["fast"])
            assert recorded == [("set_setting", ("codex_pricing_tier", "fast"))], recorded

        # The history switch appears once the cost side is actually wired.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "projects").mkdir()
            app._worker._rollups = DailyRollupStore(path=root / "rollups.json", keep_days=30)
            app._worker._pricing = DEFAULT_PRICING
            app._worker._indexer = Indexer(
                projects_dir=root / "projects",
                state_path=root / "scan_state.json",
                lookback_days=30,
                pricing=DEFAULT_PRICING,
            )
            titles = list(app._settings_submenu(UiSnapshot(settings=settings)).keys())
            assert any(t.startswith("History") for t in titles), titles
    finally:
        app_mod.CODEX_ACCOUNTS_REGISTRY_PATH = original
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_codex_cost_heading_names_the_tier_and_never_rescales_a_rate() -> None:
    """Roadmap 11, wired: the tier is a LABEL on the Codex group heading.

    ``pricing.py`` carries OpenAI's standard rates only and a rollout does not
    record which tier ran, so a non-standard tier must say the rows below are
    not that tier's rather than multiply them by a number nobody measured. The
    figures are identical either way - that is the assertion that matters.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        today = local_day_key(time.time())
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        store.merge(
            [
                DayRollup(
                    day=today,
                    models={
                        "claude-fable-5": ModelUsage(input=1_000_000),
                        "codex:gpt-5.6-sol": ModelUsage(input=2_000_000),
                    },
                )
            ]
        )
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today=today, progress=IndexProgress(complete=True)
        )
        settings = normalize_settings(dict(SETTINGS_DEFAULTS))
        app = app_mod.CCUsageWidgetApp()
        try:
            standard = [str(item.title) for item in app._model_items(breakdown, settings)]
            codex_heading = [line for line in standard if "Codex" in line]
            assert len(codex_heading) == 1, standard
            assert "(standard tier)" in codex_heading[0], codex_heading[0]

            fast = [
                str(item.title)
                for item in app._model_items(breakdown, {**settings, "codex_pricing_tier": "fast"})
            ]
            fast_heading = [line for line in fast if "Codex" in line]
            assert "(fast tier rates not loaded)" in fast_heading[0], fast_heading[0]
            # Same money, different wording: no rate was re-scaled.
            assert [line for line in fast if "Codex" not in line] == [
                line for line in standard if "Codex" not in line
            ]

            # A Claude-only day has no vendor groups at all, so it carries no
            # tier claim either - byte-for-byte the pre-Codex section.
            claude_only = DailyRollupStore(path=root / "claude.json", keep_days=30)
            claude_only.merge(
                [DayRollup(day=today, models={"claude-fable-5": ModelUsage(input=1_000_000)})]
            )
            solo = [
                str(item.title)
                for item in app._model_items(
                    claude_only.cost_breakdown(
                        DEFAULT_PRICING, today=today, progress=IndexProgress(complete=True)
                    ),
                    settings,
                )
            ]
            assert solo[0] == "  ── by model ──────────", solo[0]
            assert not any("tier" in line for line in solo), solo
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Roadmap item 2, hardened (2026-09-10). Three defects the reviewers found in
# the shipped audit, each with the failure it caused:
#
#  * the audit repaired the LIVE store from its own daemon thread, with no
#    coordination with the worker's retract-then-merge;
#  * it called `tombstone_rollups` (and through it `_ensure_states_loaded`) on
#    the live scanners without their scan lock;
#  * a vanished PRE-LEDGER entry left no tombstone at all, so the first audit
#    read that file's real contribution as drift and repaired it away.
# ---------------------------------------------------------------------------


class _RecordingHistory:
    """A history mirror that records what the worker asked it to replace."""

    def __init__(self) -> None:
        self.replaced: list[tuple[str, int]] = []
        self.errors: tuple[str, ...] = ()

    def upsert_days(self, days: Any, pricing: Any) -> None:
        return None

    def replace_day(self, day: str, rollup: Any, pricing: Any) -> None:
        self.replaced.append((day, rollup.total.total_tokens))


def test_the_audit_thread_never_touches_the_live_store() -> None:
    """The audit computes; the WORKER applies. Nothing else is safe.

    ``_repair`` used to run retract-then-merge on the daemon thread while the
    worker was doing its own retract-then-merge on the same store: the two
    interleave, and a whole tick of deltas - bytes whose offsets are already
    durable, so they are never re-read - can end up subtracted back out. The
    thread now produces plain data and mutates nothing.
    """
    from cc_usage_widget.audit import SelfAudit

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, scanners, today = _audit_fixture(root)
        key = f"{VENDOR_CODEX}:gpt-5.6-sol"
        truth = store.today(today).models[key]
        store.add(today, key, truth)
        before = store.today(today)

        result = SelfAudit.beside(store.path).run(store, scanners, today=today)
        assert result.drifted, "the fixture did not drift"
        assert result.repairs, "no repair plan was produced"
        assert store.today(today) == before, "the audit thread mutated the store"
        # The plan is data, not a live handle on anything.
        repair = result.repairs[0]
        assert isinstance(repair.fresh, tuple) and isinstance(repair.observed, tuple)
        assert repair.observed_models[key] == before.models[key], repair.observed


def test_a_repair_applied_after_a_merge_keeps_the_merged_deltas() -> None:
    """The reason the plan carries what the audit OBSERVED.

    The audit reads the store at 09:00 and the worker merges a tick's deltas at
    09:00:05, before the plan is applied. Installing the audit's absolute figure
    would silently delete those deltas - and the indexer has already advanced
    past the bytes that produced them, so they never come back. The repair is
    therefore applied as a correction: current - observed + fresh.
    """
    from cc_usage_widget.audit import AuditRepair

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        transcript = _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", now, input_tokens=1_000_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "self_audit_enabled": False}
            ),
        )
        try:
            _drain_cost(worker)
            truth = store.today(today).models[FABLE]
            assert truth.input == 1_000_000, truth
            # The store is inflated the way a re-read from byte 0 used to
            # inflate it. This is what the audit sees.
            store.add(today, FABLE, truth)
            observed = store.today(today).models[FABLE]
            assert observed.input == 2_000_000, observed

            # ...and while the audit is still running, the worker merges a new
            # tick's deltas.
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(_record("req-b", now, input_tokens=500_000)) + "\n"
                )
            worker._run_cost_job()
            assert store.today(today).models[FABLE].input == 2_500_000

            # Only now does the plan land.
            worker._audit_repairs = (
                AuditRepair(
                    day=today,
                    vendor=VENDOR_CLAUDE,
                    fresh=((FABLE, truth),),
                    observed=((FABLE, observed),),
                ),
            )
            worker._run_cost_job()
            after = store.today(today).models[FABLE]
            assert after.input == 1_500_000, (
                "the repair overwrote deltas merged while it was being computed"
            )
            assert worker._audit_repairs == (), "the plan was left to run again"
        finally:
            worker.stop(timeout=5.0)


def test_a_repaired_day_is_replaced_in_the_long_term_record() -> None:
    """history.sqlite mirrors the store's days, so a rebuilt day has to be
    REPLACED there too - otherwise the dashboard reads the inflated figure for
    ever, from a table nothing else ever corrects."""
    from cc_usage_widget.audit import AuditRepair

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", now, input_tokens=1_000_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "self_audit_enabled": False}
            ),
        )
        history = _RecordingHistory()
        worker._history = history
        try:
            _drain_cost(worker)
            truth = store.today(today).models[FABLE]
            store.add(today, FABLE, truth)
            observed = store.today(today).models[FABLE]
            worker._audit_repairs = (
                AuditRepair(
                    day=today,
                    vendor=VENDOR_CLAUDE,
                    fresh=((FABLE, truth),),
                    observed=((FABLE, observed),),
                ),
            )
            worker._run_cost_job()
            assert history.replaced, "the repaired day never reached history"
            day, tokens = history.replaced[0]
            assert day == today, history.replaced
            assert tokens == truth.total_tokens, history.replaced
        finally:
            worker.stop(timeout=5.0)


def test_a_rebuild_between_the_plan_and_the_tick_drops_the_plan() -> None:
    """A plan describes a store that existed. After `Rebuild cost index` it does
    not: applying the correction to an emptied day could add tokens nothing has
    read, and the rebuild's own re-index is the authority anyway."""
    from cc_usage_widget.audit import AuditRepair

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", now, input_tokens=1_000_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "self_audit_enabled": False}
            ),
        )
        try:
            _drain_cost(worker)
            truth = store.today(today).models[FABLE]
            worker._audit_repairs = (
                AuditRepair(
                    day=today,
                    vendor=VENDOR_CLAUDE,
                    fresh=((FABLE, truth),),
                    observed=(),
                ),
            )
            # What `Rebuild cost index` does before the next tick.
            store.clear()
            indexer.reset()
            worker._apply_audit_repairs(store)
            assert worker._audit_repairs == (), "the stale plan was kept"
            assert store.days() == (), "a plan repopulated an emptied store"
        finally:
            worker.stop(timeout=5.0)


def test_the_audit_refuses_to_repair_a_vendor_with_a_pre_ledger_tombstone() -> None:
    """The legacy hole, end to end.

    A transcript indexed by an older build has no ledger. When it is pruned its
    entry becomes a LEGACY tombstone: its tokens are in the store, no ledger
    describes them and no re-index can find the file. The audit therefore sees
    drift it cannot explain - and must leave it alone and say so, because
    "repairing" it deletes real money that exists nowhere else.
    """
    from cc_usage_widget.audit import SelfAudit

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        doomed = _write(
            root / "projects" / "p" / "doomed.jsonl",
            [_record("req-gone", now, input_tokens=9_000_000)],
        )
        store, scanners, today = _audit_fixture(root)
        before = store.today(today)
        assert before.total.input >= 9_000_000, before

        # Age the scan state back to what a pre-ledger build wrote.
        state_path = root / "scan_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for entry in state.values():
            entry.pop("ledger", None)
            entry.pop("lv", None)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        doomed.unlink()

        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=state_path,
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        for _ in range(5):
            result = indexer.scan_once()
            store.retract_rollups(result.retractions)
            store.merge(result.deltas)
            if indexer.progress().complete:
                break
        assert indexer.legacy_tombstone_days(since="2026-01-01"), "no legacy tombstone"

        pairs = [(VENDOR_CLAUDE, indexer), scanners[1]]
        result = SelfAudit.beside(store.path).run(store, pairs, today=today)
        assert result.error is None, result.error
        assert result.drifted, "the pruned legacy file did not read as drift"
        assert result.repairs == (), "a pre-ledger vendor was repaired anyway"
        assert result.legacy_vendors == (VENDOR_CLAUDE,), result.legacy_vendors
        assert result.note is not None and "NOT rebuilt" in result.note, result.note
        assert "pre-ledger history" in result.note, result.note
        assert store.today(today) == before, "the audit destroyed a pruned day"


def test_tombstone_rollups_waits_for_the_scan_lock() -> None:
    """Both scanners' tombstone readers take the scan lock.

    The audit calls them from its own thread; ``_states`` is the dict a scan
    rewrites entry by entry, and on a cold indexer the call also *loads* it from
    disk. Reading it unlocked was a race with every tick.
    """

    def _blocks(scanner: Any) -> None:
        done = threading.Event()

        def _read() -> None:
            scanner.tombstone_rollups(since="2026-01-01")
            done.set()

        scanner._scan_lock.acquire()
        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        try:
            assert not done.wait(0.25), (
                f"{type(scanner).__name__}.tombstone_rollups read the live scan "
                "state without the scan lock"
            )
        finally:
            scanner._scan_lock.release()
        assert done.wait(5.0), "the reader never finished after the lock was free"
        thread.join(timeout=5.0)

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _blocks(
            Indexer(
                projects_dir=root / "projects",
                state_path=root / "scan_state.json",
                lookback_days=30,
                pricing=DEFAULT_PRICING,
            )
        )
        _blocks(
            CodexIndexer(
                sessions_dir=root / "sessions",
                state_path=root / "codex_scan_state.json",
                lookback_days=30,
                pricing=DEFAULT_PRICING,
            )
        )


def test_switching_the_audit_off_clears_the_note_it_left_behind() -> None:
    """Lifecycle: set / cleared / aged / rehydrated / DISABLED.

    The `!` line survived the off switch - nothing cleared ``_audit_note``, so a
    verdict from this morning stayed on the Cost section for a feature that was
    no longer running and could never contradict it.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", time.time(), input_tokens=200_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        try:
            _drain_cost(worker)
            worker._audit_note = "audit: 1 cell drifted (claude 2026-09-09 x 2.0x) — rebuilt"
            worker._run_cost_job()
            assert worker._snapshot.audit_note is not None, "the note never published"

            worker._snapshot = replace(
                worker._snapshot,
                settings=normalize_settings(
                    {**SETTINGS_DEFAULTS, "self_audit_enabled": False}
                ),
            )
            worker._run_cost_job()
            assert worker._audit_note is None, "the note survived the off switch"
            assert worker._snapshot.audit_note is None, worker._snapshot.audit_note
        finally:
            worker.stop(timeout=5.0)


def test_the_audit_status_label_is_served_from_the_worker_cache() -> None:
    """The Settings line is read on the AppKit thread, so it must be a string
    the worker already computed - ``SelfAudit.status_label`` reads a file."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", time.time(), input_tokens=200_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        try:
            assert worker.audit_status_label() is None, "a label before any audit"
            _drain_cost(worker)
            thread = worker._audit_thread
            assert thread is not None
            thread.join(timeout=60.0)
            worker._run_cost_job()
            label = worker.audit_status_label()
            assert label is not None and label.startswith("Last audit:"), label

            # Nothing is asked of the audit object at read time.
            worker._audit = object()
            assert worker.audit_status_label() == label
        finally:
            worker.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# Fix pass 2 - the audit twin reads the STORE's bytes, not the corpus's
#
# The audit re-indexes into a TemporaryDirectory and compares. It used to read
# every file from byte 0 to its CURRENT end, while the live scanner had
# consumed only part of it: the "fresh" total then included bytes the store
# does not hold yet, the repair installed them, and the same cost tick merged
# the very same tail again. Probed at 10M indexed + 5M appended -> 20M, where
# the truth is 15M.
# ---------------------------------------------------------------------------


def test_the_audit_never_counts_bytes_the_live_scanner_has_not_read() -> None:
    """The 20M probe. A tail appended after the live scan must not be in
    "fresh", or the repair installs it and the next scan merges it again."""
    from cc_usage_widget.audit import SelfAudit

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        transcript = _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", now, input_tokens=10_000_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        for _ in range(20):
            result = indexer.scan_once()
            store.retract_rollups(result.retractions)
            store.merge(result.deltas)
            if indexer.progress().complete:
                break
        assert store.today(today).models[FABLE].input == 10_000_000

        # ...and now the session writes 5M more, which the live scanner has
        # NOT read: those bytes are past its offset and are nobody's tokens yet.
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_record("req-b", now, input_tokens=5_000_000)) + "\n")

        audit = SelfAudit.beside(store.path)
        verdict = audit.run(store, [(VENDOR_CLAUDE, indexer)], today=today)
        assert verdict.error is None, verdict.error
        # An unbounded twin reads 15M against the store's 10M and calls the
        # difference drift - a 50% "inflation" that is nothing of the sort.
        assert verdict.drifted == (), verdict.drifted
        assert verdict.repairs == (), verdict.repairs

        # Apply whatever the audit asked for (nothing), then let the live
        # scanner do its job: the tail is merged exactly once.
        for repair in verdict.repairs:
            store.replace_day_for_vendor(
                repair.day, repair.vendor, repair.fresh_models,
                observed=repair.observed_models,
            )
        for _ in range(20):
            result = indexer.scan_once()
            store.retract_rollups(result.retractions)
            store.merge(result.deltas)
            if indexer.progress().complete:
                break
        assert store.today(today).models[FABLE].input == 15_000_000, (
            "the audit counted bytes the store did not hold; the tail was "
            "merged twice"
        )


def test_a_file_replaced_under_the_live_offset_is_reported_not_repaired() -> None:
    """A transcript whose inode changed since the snapshot cannot be compared.

    The store holds what the OLD file contributed; no index of the new one can
    reproduce it, so the difference is not drift and "repairing" it would
    delete real usage. The day is named as not comparable instead.
    """
    from cc_usage_widget.audit import SelfAudit

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        path = root / "projects" / "p" / "s.jsonl"
        _write(path, [_record("req-a", now, input_tokens=4_000_000)])
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        for _ in range(20):
            result = indexer.scan_once()
            store.retract_rollups(result.retractions)
            store.merge(result.deltas)
            if indexer.progress().complete:
                break
        held = store.today(today).models[FABLE]
        assert held.input == 4_000_000, held

        # The file is replaced wholesale - a restore, a sync agent, a session
        # id reused. Same path, new inode, and the live offsets still describe
        # the old one.
        replacement = _write(
            root / "projects" / "p" / "other.jsonl",
            [_record("req-b", now, input_tokens=1_000)],
        )
        os.replace(replacement, path)

        verdict = SelfAudit.beside(store.path).run(
            store, [(VENDOR_CLAUDE, indexer)], today=today
        )
        assert verdict.error is None, verdict.error
        assert verdict.drifted, "the disagreement was not even noticed"
        assert (today, VENDOR_CLAUDE) in verdict.not_comparable, verdict.not_comparable
        assert verdict.repairs == (), "a day nothing can describe was repaired"
        assert verdict.note is not None and "not comparable" in verdict.note, verdict.note
        assert store.today(today).models[FABLE] == held, "the store was touched"


def test_a_twin_that_cannot_be_bounded_is_never_repaired_from() -> None:
    """A scanner whose ``clone_for_audit`` takes no ``limits`` reads the corpus
    as it is NOW. Comparing that with the store is the bug itself, so such a
    vendor is reported and never repaired."""
    from cc_usage_widget.audit import SelfAudit

    class _Unbounded:
        """A scanner of the older shape: no limits, no offsets."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def clone_for_audit(self, state_dir: Any, *, lookback_days: int) -> Any:
            return self._inner.clone_for_audit(state_dir, lookback_days=lookback_days)

        def tombstone_rollups(self, *, since: str) -> tuple[Any, ...]:
            return ()

        def legacy_tombstone_days(self, *, since: str) -> tuple[str, ...]:
            return ()

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", now, input_tokens=4_000_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        for _ in range(20):
            result = indexer.scan_once()
            store.merge(result.deltas)
            if indexer.progress().complete:
                break
        truth = store.today(today).models[FABLE]
        store.add(today, FABLE, truth)  # double it, as a re-read used to

        verdict = SelfAudit.beside(store.path).run(
            store, [(VENDOR_CLAUDE, _Unbounded(indexer))], today=today
        )
        assert verdict.drifted, "the doubling was not noticed"
        assert verdict.repairs == (), "an unbounded twin produced a repair plan"
        assert (today, VENDOR_CLAUDE) in verdict.not_comparable, verdict.not_comparable


def test_a_plan_that_lands_after_a_rebuild_and_a_reindex_is_refused() -> None:
    """The generation guard.

    The old guard only recognised an EMPTY store, and a rebuild stops being
    empty on its very first delta. A plan landing one tick later was applied as
    a correction against days that no longer exist:
    ``max(0, 1M - 50M + 1M) == 0`` - the freshly re-indexed day silently zeroed.
    """
    from cc_usage_widget.audit import AuditRepair

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", now, input_tokens=1_000_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "self_audit_enabled": False}
            ),
        )
        try:
            _drain_cost(worker)
            truth = store.today(today).models[FABLE]
            assert truth.input == 1_000_000, truth
            # What the audit saw before the rebuild: an inflated day.
            stale = AuditRepair(
                day=today,
                vendor=VENDOR_CLAUDE,
                fresh=((FABLE, truth),),
                observed=((FABLE, ModelUsage(input=50_000_000)),),
                generation=store.generation,
            )

            # `Rebuild cost index`, and then a tick that re-indexes the day.
            store.clear()
            indexer.reset()
            worker._run_cost_job()
            assert store.today(today).models[FABLE] == truth, "the rebuild lost the day"
            assert store.days(), "the store is empty; this is not the case under test"

            # ...and only NOW does the audit thread publish what it computed
            # before the rebuild.
            worker._audit_repairs = (stale,)
            worker._run_cost_job()

            assert store.today(today).models[FABLE] == truth, (
                "a plan from before the rebuild zeroed the re-indexed day"
            )
            assert worker._audit_repairs == (), "the stale plan was kept"

            # ...and the verdict says so. (Published straight into
            # `_apply_audit_repairs` because the tick above ends by clearing
            # the note: the self-audit is switched off in this fixture, and off
            # means no `!` line at all.)
            worker._audit_note = (
                "audit: 1 cell drifted (claude 2026-09-09 x 50.0x) — repair pending"
            )
            worker._audit_repairs = (stale,)
            worker._apply_audit_repairs(store)
            note = worker._audit_note or ""
            assert note.endswith("NOT rebuilt - the cost index was rebuilt since"), note
        finally:
            worker.stop(timeout=5.0)


def test_a_rebuild_during_the_audit_drain_invalidates_the_plan() -> None:
    """The generation must be sampled BEFORE the twin drains.

    Sampled afterwards, a `Rebuild cost index` that lands while the twin is
    reading stamps the plan with the NEW generation, the worker's mismatch
    check passes, and the re-indexed day is counted twice (verifier,
    2026-09-10). Here the store moves its generation on the first read the
    comparison makes - i.e. after the drain - and the plan must still be the
    old generation's, which the worker then refuses.
    """
    from cc_usage_widget.audit import SelfAudit

    class _RebuildingStore(DailyRollupStore):
        """Bumps its generation once, on the first read after construction."""

        _bumped = False

        def _bump_once(self) -> None:
            if not self._bumped:
                self._bumped = True
                self.bump_generation()

        def get(self, day):  # type: ignore[override]
            self._bump_once()
            return super().get(day)

        def today(self, day=None):  # type: ignore[override]
            self._bump_once()
            return super().today(day)

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, scanners, today = _audit_fixture(root)
        key = f"{VENDOR_CODEX}:gpt-5.6-sol"
        truth = store.today(today).models[key]
        store.add(today, key, truth)  # the inflated cell the audit will want to repair
        store.save(force=True)
        racing = _RebuildingStore(path=store.path, keep_days=30)
        racing.load()
        before = racing.generation
        assert racing._bumped is False  # noqa: SLF001

        result = SelfAudit.beside(racing.path).run(racing, scanners, today=today)
        assert result.error is None, result.error
        assert result.repairs, "the fixture must produce a repair plan"
        assert racing._bumped is True, "the comparison never read the store"  # noqa: SLF001
        assert racing.generation == before + 1
        assert all(r.generation == before for r in result.repairs), (
            "the plan carries the generation read AFTER the rebuild"
        )

        indexer = next(scanner for vendor, scanner in scanners if vendor == "claude")
        worker = _worker(root, indexer=indexer, rollups=racing)
        try:
            inflated = racing.today(today).models[key]
            worker._audit_note = result.note
            worker._audit_repairs = tuple(result.repairs)
            worker._apply_audit_repairs(racing)
            assert racing.today(today).models[key] == inflated, "the stale plan was applied"
            note = worker._audit_note or ""
            assert note.endswith("NOT rebuilt - the cost index was rebuilt since"), note
        finally:
            worker.stop(timeout=5.0)


def test_a_dropped_plan_never_leaves_the_note_claiming_a_rebuild() -> None:
    """SPEC 3.2a: "rebuilt" is published only after the plan applied.

    Every other exit rewrites the tail to ``NOT rebuilt - <reason>``. A note
    that claims a repair that never happened is worse than no note: it is the
    one line the operator would use to decide the number can be trusted.
    """
    from cc_usage_widget.audit import AuditRepair

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", now, input_tokens=1_000_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "self_audit_enabled": False}
            ),
        )
        try:
            _drain_cost(worker)
            truth = store.today(today).models[FABLE]
            pending = (
                "audit: 1 cell drifted (claude 2026-09-09 x 2.0x) — repair pending"
            )

            # 1. every repair raises.
            class _Refuses:
                def __getattr__(self, item: str) -> Any:
                    return getattr(store, item)

                def replace_day_for_vendor(self, *a: Any, **k: Any) -> None:
                    raise RuntimeError("no")

            worker._audit_note = pending
            worker._audit_repairs = (
                AuditRepair(
                    day=today,
                    vendor=VENDOR_CLAUDE,
                    fresh=((FABLE, truth),),
                    observed=((FABLE, truth),),
                    generation=store.generation,
                ),
            )
            worker._apply_audit_repairs(_Refuses())
            assert worker._audit_note == (
                "audit: 1 cell drifted (claude 2026-09-09 x 2.0x) "
                "— NOT rebuilt - every repair failed"
            ), worker._audit_note

            # 2. a store that cannot repair a day at all.
            class _CannotRepair:
                def days(self) -> tuple[str, ...]:
                    return (today,)

                generation = store.generation

            worker._audit_note = pending
            worker._audit_repairs = (
                AuditRepair(day=today, vendor=VENDOR_CLAUDE, generation=store.generation),
            )
            worker._apply_audit_repairs(_CannotRepair())
            assert (worker._audit_note or "").endswith(
                "NOT rebuilt - this store cannot repair a day"
            ), worker._audit_note

            # 3. `Rebuild cost index` while a plan is outstanding.
            worker._audit_note = pending
            worker._audit_repairs = (
                AuditRepair(day=today, vendor=VENDOR_CLAUDE, generation=store.generation),
            )
            worker._rebuild_index()
            assert worker._audit_repairs == (), "the rebuild kept the plan"
            assert (worker._audit_note or "").endswith(
                "NOT rebuilt - the cost index was rebuilt"
            ), worker._audit_note
            assert store.generation > 0, "the rebuild did not bump the generation"
        finally:
            worker.stop(timeout=5.0)


def test_a_repaired_day_resets_its_per_project_split_and_says_so() -> None:
    """A repair rebuilds a day's AGGREGATE. The per-project decomposition of
    that day cannot be rebuilt from a per-(day, model) plan, so it is dropped -
    from ``attribution.json`` and from ``history.daily_project`` - and the note
    says why, or the menu block just looks broken."""
    from cc_usage_widget.attribution import Attribution, AttributionStore
    from cc_usage_widget.audit import AuditRepair
    from cc_usage_widget.history import HistoryStore

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", now, input_tokens=1_000_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "self_audit_enabled": False}
            ),
        )
        history = HistoryStore(path=root / "history.sqlite")
        worker._history = history
        try:
            _drain_cost(worker)
            truth = store.today(today).models[FABLE]

            # The split as it stood before the repair, in both places.
            attribution = AttributionStore(path=root / "attribution.json")
            scope = Attribution(vendor=VENDOR_CLAUDE, project="alpha", session="s")
            inflated = DayRollup(day=today, models={FABLE: ModelUsage(input=9_000_000)})
            attribution.merge([(scope, inflated)])
            attribution.save(force=True)
            worker._attribution = attribution
            history.upsert_projects(attribution.project_rollups(), DEFAULT_PRICING)
            assert history.project_rows(), "the fixture wrote no project rows"

            worker._audit_note = (
                "audit: 1 cell drifted (claude 2026-09-09 x 9.0x) — repair pending"
            )
            worker._audit_repairs = (
                AuditRepair(
                    day=today,
                    vendor=VENDOR_CLAUDE,
                    fresh=((FABLE, truth),),
                    observed=((FABLE, truth),),
                    generation=store.generation,
                ),
            )
            worker._apply_audit_repairs(store)

            assert attribution.top_projects(today, DEFAULT_PRICING) == (), (
                "the repaired day kept a project split of a figure the "
                "aggregate has disowned"
            )
            rows = [row for row in history.project_rows() if row.day == today]
            assert rows, "the rows were deleted rather than zeroed"
            assert all(row.total_tokens == 0 for row in rows), rows
            note = worker._audit_note or ""
            assert note.endswith(f"— rebuilt (project split for {today} reset)"), note

            # And the file on disk agrees with the store in memory.
            reloaded = AttributionStore(path=root / "attribution.json")
            reloaded.load()
            assert reloaded.top_projects(today, DEFAULT_PRICING) == ()
        finally:
            worker.stop(timeout=5.0)


def test_the_audit_plan_changes_hands_under_a_lock() -> None:
    """``plan, self._audit_repairs = self._audit_repairs, ()`` is a read and
    then a write, and the audit thread publishes into the gap between them: the
    plan it just computed is overwritten by the ``()`` and lost, with the audit
    already marked done for the day.

    Both sides therefore take ``_audit_lock``. The proof is that a publish
    cannot complete while the lock is held - with the guard removed the thread
    below finishes immediately.
    """
    from cc_usage_widget.audit import AuditRepair

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        try:
            today = local_day_key(time.time())
            plan = (AuditRepair(day=today, vendor=VENDOR_CLAUDE),)

            # The audit thread cannot publish while the worker holds the lock.
            published = threading.Thread(
                target=worker._publish_audit_plan, args=(plan, "note")
            )
            with worker._audit_lock:
                published.start()
                published.join(timeout=0.3)
                assert published.is_alive(), (
                    "a plan was published without taking the hand-over lock"
                )
            published.join(timeout=5.0)
            assert not published.is_alive()
            assert worker._audit_repairs == plan

            # ...and the worker cannot take one while the audit thread holds it.
            taken: list[Any] = []
            taker = threading.Thread(
                target=lambda: taken.append(worker._take_audit_plan()[0])
            )
            with worker._audit_lock:
                taker.start()
                taker.join(timeout=0.3)
                assert taker.is_alive(), (
                    "a plan was taken without the hand-over lock"
                )
            taker.join(timeout=5.0)
            assert taken == [plan], taken
            assert worker._audit_repairs == ()
        finally:
            worker.stop(timeout=5.0)


def test_switching_cost_by_project_off_empties_the_cache_it_leaves_behind() -> None:
    """Off must leave nothing that would be wrong when it comes back on.

    The scanners keep indexing while the feature is off, so a cache left
    standing describes a window with a hole in it: re-enabling would show a
    project split missing everything read in between, beside day totals that
    are complete. ``_rebuild_index`` already clears it for exactly this reason.
    """
    from cc_usage_widget.attribution import Attribution, AttributionStore

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        today = local_day_key(time.time())
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        try:
            path = root / "attribution.json"
            cache = AttributionStore(path=path)
            cache.merge(
                [
                    (
                        Attribution(vendor=VENDOR_CLAUDE, project="alpha", session="s"),
                        DayRollup(day=today, models={FABLE: ModelUsage(input=1_000)}),
                    )
                ]
            )
            cache.save(force=True)
            assert path.exists()

            worker._attribution = cache
            worker._attribution_on = True
            worker._set_attribution(False)

            reloaded = AttributionStore(path=path)
            reloaded.load()
            assert reloaded.top_projects(today, DEFAULT_PRICING) == (), (
                "the stale split survived the off switch"
            )
            assert worker._attribution is None

            # ...and a machine that has never had the feature on gets no file.
            path.unlink()
            worker._attribution_on = True
            worker._set_attribution(False)
            assert not path.exists(), "the off switch created the cache file"
        finally:
            worker.stop(timeout=5.0)


def test_a_restore_drops_the_plan_and_moves_the_store_on_a_generation() -> None:
    """``Restore last backup`` replaces the days wholesale with other bytes.

    That is exactly as fatal to an outstanding repair plan as a rebuild -
    ``observed`` describes figures nobody holds any more - and ``load()`` does
    not bump the generation, because it is a read. So the restore does.
    """
    from cc_usage_widget.audit import AuditRepair

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        _write(
            root / "projects" / "p" / "s.jsonl",
            [_record("req-a", now, input_tokens=1_000_000)],
        )
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(root, indexer=indexer, rollups=store)
        worker._snapshot = replace(
            worker._snapshot,
            settings=normalize_settings(
                {**SETTINGS_DEFAULTS, "self_audit_enabled": False}
            ),
        )
        try:
            _drain_cost(worker)
            truth = store.today(today).models[FABLE]
            assert truth.input == 1_000_000, truth

            worker._rebuild_index("backup-state-20260910-120000")
            assert store.days() == (), "the rebuild kept the days"
            after_rebuild = store.generation

            # A plan computed against the REBUILT store, still in flight when
            # the operator puts the old files back.
            worker._audit_note = (
                "audit: 1 cell drifted (claude 2026-09-09 x 2.0x) — repair pending"
            )
            worker._audit_repairs = (
                AuditRepair(
                    day=today,
                    vendor=VENDOR_CLAUDE,
                    fresh=((FABLE, truth),),
                    observed=((FABLE, truth),),
                    generation=after_rebuild,
                ),
            )
            worker._restore_backup("backup-state-20260910-120000")

            assert store.today(today).models[FABLE] == truth, "the restore lost the day"
            assert worker._audit_repairs == (), "the restore kept the plan"
            assert store.generation > after_rebuild, (
                "a plan computed before the restore would still look current"
            )
            assert (worker._audit_note or "").endswith(
                "NOT rebuilt - the cost index was restored"
            ), worker._audit_note
        finally:
            worker.stop(timeout=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
