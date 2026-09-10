"""Per-project and per-session cost attribution (roadmap item 6).

Three promises are asserted here, in this order:

1. **Scope is read, never guessed.** A project is the basename of the working
   directory the session actually reported; a subagent's cost belongs to the
   session that spawned it. Both vendors, from real files.
2. **The same ledger discipline as the money.** Attribution rides on the two
   events roadmap item 1 made reversible - a file's contribution, and the
   contribution it replaces on a re-read from byte 0 - so a replaced inode is
   not counted twice and a rebuild reproduces the totals exactly. The strongest
   form of this is asserted directly: **every project total sums to the day
   total it decomposes**, which cannot hold if either half drifts.
3. **The privacy promise survives the feature.** The canary method of
   ``test_privacy.py``, aimed at the one new artifact: a canary planted in the
   prompt, the branch, and the *parent directories* of the cwd must not reach
   ``attribution.json``, while the basename - the one component this feature
   exists to show - must. A negative control proves the check can fail.

Everything runs against real files in a ``TemporaryDirectory``; no parser is
monkeypatched and nothing here touches ``~/.claude`` or ``~/.codex``.

Run directly (pytest is not installed)::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    $PY tests/test_attribution.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

from cc_usage_widget import attribution as attr_mod  # noqa: E402
from cc_usage_widget.attribution import (  # noqa: E402
    Attribution,
    AttributionPass,
    AttributionStore,
    UNKNOWN_PROJECT,
    attribution_path_for,
    claude_scope,
    codex_scope,
)
from cc_usage_widget.codex_indexer import CodexIndexer  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    DayRollup,
    ModelUsage,
    local_day_key,
)
from cc_usage_widget.indexer import Indexer  # noqa: E402
from cc_usage_widget.pricing import DEFAULT_PRICING  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402

FABLE = "claude-fable-5-20260514"
SOL = "gpt-5.6-sol"


# ---------------------------------------------------------------------------
# Fixtures - real transcripts, real rollouts
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _claude_record(
    request_id: str,
    epoch: float,
    *,
    cwd: str | None,
    tokens: int = 1_000,
    branch: str = "main",
    prompt: str = "ordinary text",
) -> dict[str, Any]:
    """One assistant record shaped like a real Claude Code transcript line."""
    record: dict[str, Any] = {
        "type": "assistant",
        "requestId": request_id,
        "timestamp": _iso(epoch),
        "gitBranch": branch,
        "message": {
            "id": f"msg_{request_id}",
            "role": "assistant",
            "model": FABLE,
            "content": [{"type": "text", "text": prompt}],
            "usage": {
                "input_tokens": tokens,
                "output_tokens": tokens // 2,
                "cache_read_input_tokens": 0,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 0,
                    "ephemeral_1h_input_tokens": 0,
                },
            },
        },
    }
    if cwd is not None:
        record["cwd"] = cwd
    return record


def _write(path: Path, records: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _codex_rollout(
    root: Path,
    name: str,
    *,
    cwd: str,
    thread_id: str,
    parent_thread_id: str | None = None,
    turns: int = 2,
    tokens: int = 1_000,
) -> Path:
    """One ``rollout-*.jsonl`` with a real ``session_meta`` first line."""
    today = dt.date.today()
    now = time.time()
    meta: dict[str, Any] = {
        "session_id": thread_id,
        "id": thread_id,
        "timestamp": _iso(now),
        "cwd": cwd,
        "originator": "codex_cli_rs",
    }
    if parent_thread_id is not None:
        meta["source"] = {
            "subagent": {"thread_spawn": {"parent_thread_id": parent_thread_id}}
        }
    records: list[dict[str, Any]] = [
        {"timestamp": _iso(now), "ordinal": 0, "type": "session_meta", "payload": meta},
        {
            "timestamp": _iso(now),
            "type": "turn_context",
            "payload": {"model": SOL, "cwd": cwd},
        },
    ]
    for turn in range(turns):
        records.append(
            {
                "timestamp": _iso(now),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
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
    directory = root / f"{today.year:04d}" / f"{today.month:02d}" / f"{today.day:02d}"
    return _write(directory / name, records)


def _drain(scanner: Any, store: DailyRollupStore, *, limit: int = 40) -> None:
    """Scan to completion, applying both dimensions the way ``app.py`` does."""
    for _ in range(limit):
        result = scanner.scan_once()
        retract = getattr(store, "retract_rollups", None)
        if result.retractions and callable(retract):
            retract(result.retractions)
        if result.deltas:
            store.merge(result.deltas)
        if scanner.progress().complete and not result.files_read:
            return
    raise AssertionError("index never completed")


def _drain_attributed(
    scanner: Any, store: DailyRollupStore, attribution: AttributionStore, *, limit: int = 40
) -> None:
    """``_drain`` plus the attribution half, in ``app.py``'s exact order."""
    for _ in range(limit):
        result = scanner.scan_once()
        retract = getattr(store, "retract_rollups", None)
        if result.retractions and callable(retract):
            retract(result.retractions)
        if result.deltas:
            store.merge(result.deltas)
        attribution.apply(scanner.take_attribution())
        if scanner.progress().complete and not result.files_read:
            return
    raise AssertionError("index never completed")


def _claude_indexer(root: Path, *, attribute: bool = True) -> Indexer:
    indexer = Indexer(
        projects_dir=root / "projects",
        state_path=root / "scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
    )
    indexer.set_attribution(attribute)
    return indexer


def _codex_indexer(root: Path, *, attribute: bool = True) -> CodexIndexer:
    scanner = CodexIndexer(
        sessions_dir=root / "sessions",
        state_path=root / "codex_scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
    )
    scanner.set_attribution(attribute)
    return scanner


def _project_totals(store: AttributionStore, day: str) -> dict[str, int]:
    return {row.project: row.total_tokens for row in store.top_projects(day, limit=99)}


def _session_totals(store: AttributionStore, day: str) -> dict[str, int]:
    return {row.session: row.total_tokens for row in store.top_sessions(day, limit=99)}


# ---------------------------------------------------------------------------
# 1. Scope is read, never guessed
# ---------------------------------------------------------------------------


def test_a_claude_transcript_is_attributed_to_its_cwd_basename_and_session() -> None:
    """The project name comes from the record's own ``cwd``.

    Not from the ``~/.claude/projects/<dir>`` segment: that segment is the cwd
    with every ``/`` *and* every space flattened to ``-``, so
    ``-Users-v-Desktop-AI-Products--Claude-Eval-Kit`` cannot be decoded back
    without guessing which hyphens were separators - and a guessed name is a
    fabricated one.
    """
    scope = claude_scope(
        "/x/projects/-Users-v-Desktop-AI-Products--Claude-Eval-Kit/5909f788-7a20.jsonl",
        "/Users/v/Desktop/AI Products /Claude-Eval-Kit",
    )
    assert scope.vendor == VENDOR_CLAUDE
    assert scope.project == "Claude-Eval-Kit", scope
    assert scope.session == "5909f788-7a20", scope


def test_a_transcript_that_never_reported_a_cwd_keeps_the_encoded_directory() -> None:
    """No ``cwd`` seen: the directory name is used verbatim, never decoded.

    Ugly and true beats readable and invented (the no-fabrication rule). The
    row still exists, so the block cannot silently under-count.
    """
    scope = claude_scope("/x/projects/-Users-v--claude/abc-123.jsonl")
    assert scope.project == "-Users-v--claude", scope
    assert scope.session == "abc-123", scope


def test_a_subagent_transcript_rolls_up_into_its_parent_session() -> None:
    """``<project>/<session>/subagents/<agent>.jsonl`` -> the parent session.

    A swarm's cost is the swarm's. One row per agent would push the five
    sessions that matter off a five-row menu on exactly the days the question
    "what did that swarm cost" is worth asking.
    """
    scope = claude_scope(
        "/x/projects/-Users-v-proj/9280af20-7e84/subagents/agent-3.jsonl",
        "/Users/v/proj",
    )
    assert scope.session == "9280af20-7e84", scope
    assert scope.project == "proj", scope


def test_a_codex_rollout_is_attributed_from_its_session_meta() -> None:
    """Codex states its cwd and its thread id on the first line of the file."""
    scope = codex_scope(
        "/x/rollout-2026-08-24T16-06-26-abc.jsonl",
        meta={"cwd": "/Users/v/Desktop/Obsidian", "id": "thread-9", "session_id": "s-1"},
    )
    assert scope.vendor == VENDOR_CODEX
    assert scope.project == "Obsidian", scope
    assert scope.session == "thread-9", scope


def test_a_codex_subagent_rolls_up_into_its_parent_thread() -> None:
    """``source.subagent.thread_spawn.parent_thread_id`` wins over ``id``."""
    scope = codex_scope(
        "/x/rollout-x.jsonl",
        meta={
            "cwd": "/Users/v/proj",
            "id": "child-thread",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent-1"}}},
        },
    )
    assert scope.session == "parent-1", scope


def test_a_rollout_with_no_session_meta_is_named_not_dropped() -> None:
    """An unreadable scope gets an honest label and keeps its tokens.

    Dropping it would make the block quietly under-count; attributing it to a
    neighbour would be a fabricated number. ``(unknown)`` is neither.
    """
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "rollout-nometa.jsonl"
        path.write_text('{"type":"event_msg","payload":{}}\n', encoding="utf-8")
        scope = codex_scope(path)
        assert scope.project == UNKNOWN_PROJECT, scope
        assert scope.session == "rollout-nometa", scope


def test_a_codex_rollout_scope_is_read_from_the_real_first_line() -> None:
    """The file path, not an injected mapping - the production path."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        path = _codex_rollout(
            root / "sessions",
            "rollout-2026-09-10T09-00-00-a.jsonl",
            cwd="/Users/v/Desktop/Obsidian",
            thread_id="thread-42",
        )
        scope = codex_scope(path)
        assert scope.project == "Obsidian" and scope.session == "thread-42", scope


# ---------------------------------------------------------------------------
# 2. The ledger discipline, end to end
# ---------------------------------------------------------------------------


def test_project_totals_sum_to_the_day_totals_they_decompose() -> None:
    """The invariant that catches any drift in either direction, both vendors.

    A project total that is not a partition of the day total is worse than no
    project total at all: it looks like an answer.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        _write(
            root / "projects" / "-Users-v-alpha" / "sess-a.jsonl",
            [_claude_record("a1", now, cwd="/Users/v/alpha", tokens=4_000)],
        )
        _write(
            root / "projects" / "-Users-v-beta" / "sess-b.jsonl",
            [_claude_record("b1", now, cwd="/Users/v/beta", tokens=2_000)],
        )
        _codex_rollout(
            root / "sessions",
            "rollout-2026-09-10T09-00-00-a.jsonl",
            cwd="/Users/v/gamma",
            thread_id="thread-g",
        )

        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        attribution = AttributionStore(path=attribution_path_for(store.path))
        _drain_attributed(_claude_indexer(root), store, attribution)
        _drain_attributed(_codex_indexer(root), store, attribution)

        day_total = store.today(today).total.total_tokens
        assert day_total > 0, "the fixtures produced nothing to attribute"
        projects = _project_totals(attribution, today)
        assert set(projects) == {"alpha", "beta", "gamma"}, projects
        assert sum(projects.values()) == day_total, (projects, day_total)
        assert sum(_session_totals(attribution, today).values()) == day_total


def test_a_replaced_transcript_is_not_counted_twice() -> None:
    """A new inode replaces a project's contribution; it does not add to it.

    The 1,650x inflation of roadmap item 1, one dimension lower: without the
    retraction the re-read would put the whole file into the project a second
    time while the day total - which IS reversible - stayed correct, and the
    two would disagree forever.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        path = root / "projects" / "-Users-v-alpha" / "sess-a.jsonl"
        _write(path, [_claude_record("a1", now, cwd="/Users/v/alpha", tokens=1_000)])

        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        attribution = AttributionStore(path=attribution_path_for(store.path))
        indexer = _claude_indexer(root)
        _drain_attributed(indexer, store, attribution)
        before = _project_totals(attribution, today)
        assert before == {"alpha": 1_500}, before

        # A longer copy at a NEW inode - a restore, a sync agent, an editor
        # that writes through a temp file. Fresh request ids, so the same-day
        # dedup cannot mask the re-read and the arithmetic is unambiguous:
        # 1,500 comes out, 4,500 goes in.
        replacement = path.with_suffix(".new")
        _write(
            replacement,
            [
                _claude_record("a3", now, cwd="/Users/v/alpha", tokens=1_000),
                _claude_record("a4", now, cwd="/Users/v/alpha", tokens=2_000),
            ],
        )
        os.replace(replacement, path)
        _drain_attributed(indexer, store, attribution)

        after = _project_totals(attribution, today)
        assert after == {"alpha": 4_500}, (
            f"the replaced file was counted twice: {before} -> {after} "
            "(6,000 is the double count this retraction exists to prevent)"
        )
        assert sum(after.values()) == store.today(today).total.total_tokens


def test_a_rebuild_reproduces_identical_project_totals() -> None:
    """Clear + reset + re-index lands on exactly the same rows.

    This is the property a display feature must have before it may be believed:
    if the second pass disagreed with the first, neither is a number.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        _write(
            root / "projects" / "-Users-v-alpha" / "sess-a.jsonl",
            [
                _claude_record("a1", now, cwd="/Users/v/alpha", tokens=1_000),
                _claude_record("a2", now - 86_400, cwd="/Users/v/alpha", tokens=3_000),
            ],
        )
        _write(
            root / "projects" / "-Users-v-beta" / "sess-b.jsonl",
            [_claude_record("b1", now, cwd="/Users/v/beta", tokens=500)],
        )

        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        attribution = AttributionStore(path=attribution_path_for(store.path))
        indexer = _claude_indexer(root)
        _drain_attributed(indexer, store, attribution)
        first = _project_totals(attribution, today)
        assert first, first

        # Exactly what `_rebuild_index` does.
        store.clear()
        attribution.clear()
        indexer.reset()
        _drain_attributed(indexer, store, attribution)

        assert _project_totals(attribution, today) == first
        assert sum(first.values()) == store.today(today).total.total_tokens


def test_a_subagent_transcripts_tokens_land_on_its_parent_session() -> None:
    """End to end, from files: parent + subagent are one session row."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        project = root / "projects" / "-Users-v-alpha"
        _write(
            project / "9280af20.jsonl",
            [_claude_record("p1", now, cwd="/Users/v/alpha", tokens=1_000)],
        )
        _write(
            project / "9280af20" / "subagents" / "agent-1.jsonl",
            [_claude_record("s1", now, cwd="/Users/v/alpha", tokens=2_000)],
        )

        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        attribution = AttributionStore(path=attribution_path_for(store.path))
        _drain_attributed(_claude_indexer(root), store, attribution)

        sessions = _session_totals(attribution, today)
        assert sessions == {"9280af20": 4_500}, sessions
        assert _project_totals(attribution, today) == {"alpha": 4_500}


def test_attribution_is_off_unless_the_owner_switches_it_on() -> None:
    """The off switch, at the seam that matters: no scope is ever resolved.

    A scanner built directly - which is what ``test_privacy.py`` does - must
    behave exactly as it did before this feature existed.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        _write(
            root / "projects" / "-Users-v-alpha" / "sess-a.jsonl",
            [_claude_record("a1", now, cwd="/Users/v/alpha", tokens=1_000)],
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        indexer = Indexer(
            projects_dir=root / "projects",
            state_path=root / "scan_state.json",
            lookback_days=30,
            pricing=DEFAULT_PRICING,
        )
        assert not indexer.attribution_enabled, "attribution must be opt-in"
        _drain(indexer, store)
        assert store.today(local_day_key(now)).total.total_tokens == 1_500
        assert not indexer.take_attribution().changed, (
            "a scanner with attribution off must attribute nothing"
        )


# ---------------------------------------------------------------------------
# 3. The store: persistence, bounds, clamping
# ---------------------------------------------------------------------------


def _scope(project: str, session: str) -> Attribution:
    return Attribution(vendor=VENDOR_CLAUDE, project=project, session=session)


def _rollup(day: str, tokens: int) -> DayRollup:
    return DayRollup(day=day, models={FABLE: ModelUsage(input=tokens)})


def test_the_store_round_trips_through_disk() -> None:
    """What was applied comes back, both dimensions, after a reload."""
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "attribution.json"
        store = AttributionStore(path=path)
        store.merge([(_scope("alpha", "s-1"), _rollup("2026-09-10", 100))])
        store.save()
        assert path.exists()

        reloaded = AttributionStore(path=path)
        reloaded.load()
        assert _project_totals(reloaded, "2026-09-10") == {"alpha": 100}
        assert _session_totals(reloaded, "2026-09-10") == {"s-1": 100}


def test_a_corrupt_cache_file_starts_empty_instead_of_raising() -> None:
    """It is a cache. A truncated file costs the two blocks, never the widget."""
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "attribution.json"
        path.write_text('{"projects": {"2026-09-10', encoding="utf-8")
        store = AttributionStore(path=path)
        store.load()
        assert len(store) == 0
        assert store.top_projects("2026-09-10") == ()


def test_the_store_is_written_0600() -> None:
    """It names the directories somebody works in; the other state files are 0600."""
    import stat

    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "attribution.json"
        store = AttributionStore(path=path)
        store.merge([(_scope("alpha", "s-1"), _rollup("2026-09-10", 100))])
        store.save()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_session_rows_are_bounded_to_two_days_and_projects_to_the_window() -> None:
    """A swarm day is hundreds of sessions; the menu shows five of today's.

    Projects keep the 30-day window because a project count is bounded by how
    many directories a person works in.
    """
    store = AttributionStore(path=None, keep_days=30)
    for offset, day in enumerate(("2026-09-10", "2026-09-09", "2026-09-08", "2026-07-01")):
        store.merge([(_scope(f"p{offset}", f"s{offset}"), _rollup(day, 100))])
    store.prune(today="2026-09-10")
    assert _project_totals(store, "2026-09-08") == {"p2": 100}, "30-day window"
    assert _project_totals(store, "2026-07-01") == {}, "outside the window"
    assert _session_totals(store, "2026-09-09") == {"s1": 100}, "yesterday survives"
    assert _session_totals(store, "2026-09-08") == {}, "older sessions are dropped"


def test_a_retraction_clamps_at_zero_and_says_so() -> None:
    """The ledger can describe more than this cache holds; it must not go negative."""
    logs: list[str] = []
    store = AttributionStore(path=None, logger=logs.append)
    scope = _scope("alpha", "s-1")
    store.merge([(scope, _rollup("2026-09-10", 100))])
    clamped = store.retract([(scope, _rollup("2026-09-10", 500))])
    assert clamped == 2, clamped  # one project cell, one session cell
    assert _project_totals(store, "2026-09-10") == {}
    assert any("clamped" in line for line in logs), logs


def test_apply_retracts_before_it_adds() -> None:
    """The order is the feature, and it is only visible when a cell clamps.

    Deliberately the clamping case: with the cache holding 100 and a pass that
    takes back 500 and puts in 400, the right order lands on 400 (the clamp
    eats the shortfall, which is what "the ledger described more than we hold"
    has to mean) and the wrong one lands on 0 - the re-added contribution taken
    straight back out. With no clamp in play both orders agree, so a test that
    did not force one would pass against either.
    """
    store = AttributionStore(path=None)
    scope = _scope("alpha", "s-1")
    store.merge([(scope, _rollup("2026-09-10", 100))])
    store.apply(
        AttributionPass(
            adds=((scope, _rollup("2026-09-10", 400)),),
            retractions=((scope, _rollup("2026-09-10", 500)),),
        )
    )
    assert _project_totals(store, "2026-09-10") == {"alpha": 400}, (
        "0 means the add ran first and the retraction took it back out"
    )


def test_rows_are_ordered_by_money_then_volume() -> None:
    """"Top 5" has to mean the dearest five, not the first five inserted."""
    store = AttributionStore(path=None)
    day = local_day_key(time.time())
    store.merge(
        [
            (_scope("cheap", "s-1"), DayRollup(day=day, models={FABLE: ModelUsage(input=10)})),
            (_scope("dear", "s-2"), DayRollup(day=day, models={FABLE: ModelUsage(input=10_000_000)})),
        ]
    )
    rows = store.top_projects(day, DEFAULT_PRICING, limit=5)
    assert [row.project for row in rows] == ["dear", "cheap"], rows
    assert rows[0].usd > rows[1].usd, rows


def test_an_unpriced_model_shows_volume_and_never_a_dollar() -> None:
    """``codex-auto-review`` is $0 with 916M tokens behind it (roadmap item 3)."""
    store = AttributionStore(path=None)
    day = local_day_key(time.time())
    store.merge(
        [
            (
                Attribution(vendor=VENDOR_CODEX, project="alpha", session="s-1"),
                DayRollup(
                    day=day,
                    models={f"{VENDOR_CODEX}:codex-auto-review": ModelUsage(input=900_000)},
                ),
            )
        ]
    )
    row = store.top_projects(day, DEFAULT_PRICING)[0]
    assert row.usd == 0.0 and row.unpriced_tokens == 900_000, row


def test_dropping_a_vendor_leaves_the_other_untouched() -> None:
    """The twin of ``DailyRollupStore.drop_vendors`` - one vendor's loss is its own."""
    store = AttributionStore(path=None)
    day = "2026-09-10"
    store.merge(
        [
            (_scope("alpha", "s-1"), _rollup(day, 100)),
            (
                Attribution(vendor=VENDOR_CODEX, project="beta", session="s-2"),
                DayRollup(day=day, models={f"{VENDOR_CODEX}:{SOL}": ModelUsage(input=200)}),
            ),
        ]
    )
    assert store.drop_vendors([VENDOR_CODEX]) > 0
    assert _project_totals(store, day) == {"alpha": 100}


# ---------------------------------------------------------------------------
# 4. The privacy promise, with a negative control
# ---------------------------------------------------------------------------

CANARY = "SECRET_CANARY_7C4D1_do_not_leak"
"""Planted in the prompt, the git branch, and the PARENT directories of the
working directory - every free-text position this feature comes near except the
one component it is allowed to keep."""

PROJECT_NAME = "widget-fixtures"
"""The basename, and the only string that may survive into the cache."""


def _poisoned_corpus(root: Path) -> tuple[DailyRollupStore, AttributionStore]:
    """Index a Claude transcript and a Codex rollout whose paths carry a canary."""
    now = time.time()
    cwd = f"/Users/someone/{CANARY}/{PROJECT_NAME}"
    # The canary is deliberately NOT in the directory NAME: `scan_state.json`
    # is keyed by absolute path and always has been, so planting it there would
    # test the scan state's pre-existing shape rather than this feature's.
    _write(
        root / "projects" / f"-Users-someone-hidden-{PROJECT_NAME}" / "sess-a.jsonl",
        [
            _claude_record(
                "a1",
                now,
                cwd=cwd,
                tokens=1_000,
                branch=f"feature/{CANARY}",
                prompt=f"here is my api key: {CANARY}",
            )
        ],
    )
    _codex_rollout(
        root / "sessions",
        "rollout-2026-09-10T09-00-00-a.jsonl",
        cwd=cwd,
        thread_id="thread-1",
    )
    store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
    attribution = AttributionStore(path=attribution_path_for(store.path))
    _drain_attributed(_claude_indexer(root), store, attribution)
    _drain_attributed(_codex_indexer(root), store, attribution)
    attribution.save(force=True)
    store.save(force=True)
    return store, attribution


def test_only_the_basename_reaches_the_attribution_cache() -> None:
    """Prompt, branch and every parent directory stop at the reader.

    ``attribution.json`` is the one new artifact this feature writes, and the
    project name is the one string in it that came out of a transcript. This
    asserts the exact boundary: the basename survives, the path it sat in does
    not.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _store, attribution = _poisoned_corpus(root)

        written = [p for p in root.rglob("*") if p.is_file() and p.suffix in {".json"}]
        assert any(p.name == "attribution.json" for p in written), written

        leaked: list[str] = []
        for path in written:
            body = path.read_text(errors="replace")
            if CANARY in body:
                leaked.append(f"{path.name}: content")
            if CANARY in str(path):
                leaked.append(f"{path.name}: filename")
        assert not leaked, "transcript content escaped into: " + ", ".join(leaked)

        cache = (root / "attribution.json").read_text(encoding="utf-8")
        assert PROJECT_NAME in cache, (
            "the basename did NOT survive - this test would pass vacuously"
        )
        rows = attribution.top_projects(local_day_key(time.time()), limit=9)
        assert {row.project for row in rows} == {PROJECT_NAME}, rows


def test_the_canary_check_can_actually_fail() -> None:
    """The negative control: a scope resolver that keeps the whole path.

    Without this, the test above would pass just as happily against a feature
    that had quietly stopped attributing anything.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        cwd = f"/Users/someone/{CANARY}/{PROJECT_NAME}"
        _write(
            root / "projects" / "-Users-someone-x" / "sess-a.jsonl",
            [_claude_record("a1", now, cwd=cwd, tokens=1_000)],
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        attribution = AttributionStore(path=attribution_path_for(store.path))
        indexer = _claude_indexer(root)
        # The careless version: the full path as the project name.
        indexer._attribution = attr_mod.AttributionCollector(
            lambda path, hint: Attribution(
                vendor=VENDOR_CLAUDE, project=str(hint or ""), session="s"
            )
        )
        _drain_attributed(indexer, store, attribution)
        attribution.save(force=True)

        body = (root / "attribution.json").read_text(encoding="utf-8")
        assert CANARY in body, (
            "a resolver that stores the whole path went UNDETECTED - the check "
            "above is not testing anything"
        )


def test_the_project_name_is_bounded_in_length() -> None:
    """A pathological basename must not grow the cache file without bound."""
    scope = claude_scope("/x/projects/p/s.jsonl", "/Users/v/" + "n" * 500)
    assert len(scope.project) == 64, len(scope.project)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. The scope has to survive a restart (2026-09-10)
#
# `AttributionCollector` caches the scope of a path so a retraction goes back
# to the project the tokens went INTO. That cache dies with the process, and
# the first thing a replaced transcript does after a relaunch is retract its
# old ledger - resolved, with nothing remembered, from the file that is on disk
# NOW. So the scope is persisted on the file's scan-state entry, beside the
# ledger it has to agree with.
# ---------------------------------------------------------------------------


def test_a_retraction_after_a_restart_goes_to_the_project_it_came_from() -> None:
    """A session file rewritten in a different working directory.

    ``scope_for`` pins a path's project for the life of the collector precisely
    so a retraction goes back where the tokens went. Across a RESTART there was
    nothing to pin it to: the retraction was resolved from the file on disk
    now, so it landed on the new project - which clamps at zero, having nothing
    to give back - while the old project kept tokens the file no longer holds.
    Two wrong rows, and a split that no longer sums to the day it decomposes.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        path = root / "projects" / "-Users-v-alpha" / "sess-a.jsonl"
        _write(path, [_claude_record("a1", now, cwd="/Users/v/alpha", tokens=1_000)])

        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        cache_path = attribution_path_for(store.path)
        attribution = AttributionStore(path=cache_path)
        indexer = _claude_indexer(root)
        _drain_attributed(indexer, store, attribution)
        assert _project_totals(attribution, today) == {"alpha": 1_500}
        attribution.save(force=True)

        # The widget is relaunched: a new indexer over the SAME scan state, a
        # new store loaded from the same file, and nothing at all in memory.
        restarted = _claude_indexer(root)
        reloaded = AttributionStore(path=cache_path)
        reloaded.load()
        assert _project_totals(reloaded, today) == {"alpha": 1_500}

        # ...and the transcript is replaced by one that reports a DIFFERENT
        # working directory. A new inode, so the old contribution is retracted
        # and the new one added.
        replacement = path.with_suffix(".new")
        _write(
            replacement, [_claude_record("a2", now, cwd="/Users/v/beta", tokens=2_000)]
        )
        os.replace(replacement, path)
        _drain_attributed(restarted, store, reloaded)

        totals = _project_totals(reloaded, today)
        day_total = store.today(today).total.total_tokens
        assert day_total == 3_000, store.today(today)
        # One row, holding the whole day: the file's scope is what it was when
        # its tokens went in, and both halves of the replacement - the
        # retraction of 1,500 and the addition of 3,000 - landed on it.
        assert totals == {"alpha": 3_000}, (
            "the retraction was attributed to the wrong project: expected one "
            f"row of {day_total}, got {totals}"
        )
        assert sum(totals.values()) == day_total, (totals, day_total)


def test_the_scope_is_written_beside_the_ledger_and_only_when_attributing() -> None:
    """The state entry carries it, and a machine with the feature OFF keeps its
    scan state byte-identical."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        _write(
            root / "projects" / "-Users-v-alpha" / "sess-a.jsonl",
            [_claude_record("a1", now, cwd="/Users/v/alpha", tokens=1_000)],
        )
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        attribution = AttributionStore(path=attribution_path_for(store.path))
        indexer = _claude_indexer(root)
        _drain_attributed(indexer, store, attribution)
        state = json.loads((root / "scan_state.json").read_text(encoding="utf-8"))
        entry = next(iter(state.values()))
        assert "sc" in entry, entry
        assert entry["sc"].split("\x1f")[1] == "alpha", entry["sc"]

        off_root = root / "off"
        _write(
            off_root / "projects" / "-Users-v-alpha" / "sess-a.jsonl",
            [_claude_record("a1", now, cwd="/Users/v/alpha", tokens=1_000)],
        )
        off_store = DailyRollupStore(path=off_root / "rollups.json", keep_days=30)
        off_indexer = _claude_indexer(off_root, attribute=False)
        off_attr = AttributionStore(path=attribution_path_for(off_store.path))
        _drain_attributed(off_indexer, off_store, off_attr)
        off_state = json.loads(
            (off_root / "scan_state.json").read_text(encoding="utf-8")
        )
        off_entry = next(iter(off_state.values()))
        assert "sc" not in off_entry, off_entry


def test_a_vanished_transcript_keeps_its_scope_on_the_tombstone() -> None:
    """The cache is bounded by dropping gone paths, so the tombstone is the
    only thing left that can attribute a returning file's retraction."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        today = local_day_key(now)
        path = root / "projects" / "-Users-v-alpha" / "sess-a.jsonl"
        _write(path, [_claude_record("a1", now, cwd="/Users/v/alpha", tokens=1_000)])
        store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        attribution = AttributionStore(path=attribution_path_for(store.path))
        indexer = _claude_indexer(root)
        _drain_attributed(indexer, store, attribution)

        path.unlink()
        _drain_attributed(indexer, store, attribution)
        state = json.loads((root / "scan_state.json").read_text(encoding="utf-8"))
        entry = state[str(path)]
        assert entry["inode"] == 0, entry
        assert entry["sc"].split("\x1f")[1] == "alpha", entry

        # The path comes back reporting a different project. Its old
        # contribution must come back out of alpha - the project it went into -
        # not out of the one it has just joined, which has nothing to give.
        _write(path, [_claude_record("a9", now, cwd="/Users/v/beta", tokens=2_000)])
        _drain_attributed(indexer, store, attribution)
        totals = _project_totals(attribution, today)
        day_total = store.today(today).total.total_tokens
        assert day_total == 3_000, store.today(today)
        assert totals == {"alpha": 3_000}, (totals, day_total)


def _tests() -> list[tuple[str, object]]:
    items = [
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
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
