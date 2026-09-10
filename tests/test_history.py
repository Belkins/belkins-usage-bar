"""Tests for ``cc_usage_widget.history`` — the long-term mirror and its export
(roadmap item 8).

What is actually at stake here is *irreversibility*. ``rollups.json`` is a
cache: lose it and one re-index rebuilds it. ``history.sqlite`` is not - by the
time a day falls out of the 30-day window, Claude Code has usually pruned the
transcripts it came from, so a row this module drops is gone for good. Three of
the guarantees below therefore exist to prove that nothing here deletes:

* a day the rollup store has PRUNED still reads back out of history;
* a corrupt database is moved aside, not removed, and a fresh one takes over;
* an unwritable state directory reports a ``!`` line, not an exception into the
  cost job that just produced the numbers.

The fourth is the mirror image of the roadmap's opening incident: the store was
add-only and inflated some cells up to 1,650x. A history that only ever
INSERTed would have preserved that inflation for ever, so a rebuild that
*lowers* a cell has to propagate - which is what makes this a REPLACE, and what
``test_a_rebuild_that_lowers_a_cell_propagates`` pins down.

Nothing here touches ``~/.claude``, ``~/.codex``, ``~/Downloads`` or the real
widget home: every path is inside a ``TemporaryDirectory``, and the export
directory is injected rather than defaulted.

Run directly (pytest is absent from the claude-swap venv)::

    ~/.local/share/uv/tools/claude-swap/bin/python tests/test_history.py
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import history as history_mod  # noqa: E402
from cc_usage_widget.app import BackgroundWorker, UiSnapshot  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    HISTORY_DB_PATH,
    NOTIONAL_LABEL,
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    DayRollup,
    ModelUsage,
    local_day_key,
    make_vendor_key,
    normalize_settings,
)
from cc_usage_widget.history import (  # noqa: E402
    COLUMNS,
    EXPORT_FIELDS,
    PROJECT_COLUMNS,
    HistoryStore,
    export_filename,
    export_history,
)
from cc_usage_widget.indexer import Indexer  # noqa: E402
from cc_usage_widget.pricing import DEFAULT_PRICING  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402

FABLE = "claude-fable-5-20260514"
SOL = make_vendor_key(VENDOR_CODEX, "gpt-5.6-sol")


# ---------------------------------------------------------------------------
# Fixture helpers — real files, real sqlite, no monkeypatched parsers
# ---------------------------------------------------------------------------


def _usage(**fields: int) -> ModelUsage:
    return ModelUsage(
        input=fields.get("input", 0),
        output=fields.get("output", 0),
        cache_write_5m=fields.get("cache_write_5m", 0),
        cache_write_1h=fields.get("cache_write_1h", 0),
        cache_read=fields.get("cache_read", 0),
    )


def _day(day: str, **models: ModelUsage) -> DayRollup:
    return DayRollup(day=day, models=dict(models))


def _store(root: Path, **kwargs: Any) -> HistoryStore:
    return HistoryStore(root / "history.sqlite", **kwargs)


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


def _write_transcript(path: Path, records: list[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _worker(root: Path, *, indexer: Any = None, rollups: Any = None) -> BackgroundWorker:
    """A worker wired for cost only, with the settings the real app passes."""
    return BackgroundWorker(
        publish=lambda _snapshot: None,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        indexer=indexer,
        rollups=rollups,
        pricing=DEFAULT_PRICING,
    )


class _Redirect:
    """Send ``Export usage …`` into a temp dir instead of ``~/Downloads``.

    The constant is read at *call* time (``BackgroundWorker._export_history``
    looks it up through the module), which is what lets a test redirect it
    without patching a parser or a class. Restored on exit, always.

    The DATABASE needs no redirect: it follows the rollup store's directory by
    :func:`history_path_for`, and every worker here is built on a store inside
    a ``TemporaryDirectory``.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._saved: Any = None

    def __enter__(self) -> Path:
        self._saved = history_mod.DEFAULT_EXPORT_DIR
        history_mod.DEFAULT_EXPORT_DIR = self._root / "Downloads"
        return self._root

    def __exit__(self, *exc: Any) -> None:
        history_mod.DEFAULT_EXPORT_DIR = self._saved


# ---------------------------------------------------------------------------
# 1. Shape and idempotence
# ---------------------------------------------------------------------------


def test_the_schema_holds_counters_and_keys_and_no_free_text() -> None:
    """The privacy promise, as a column list rather than as prose.

    ``history.sqlite`` is the one artifact that outlives the transcripts, so if
    a free-text column ever appeared here it would be the longest-lived leak in
    the program. Asserting the exact column set makes adding one a test change,
    which is a conversation.
    """
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        store.upsert_day("2026-09-10", _day("2026-09-10", **{FABLE: _usage(input=5)}))
        with sqlite3.connect(str(store.path)) as conn:
            found = tuple(row[1] for row in conn.execute("PRAGMA table_info(daily)"))
        assert found == COLUMNS, found
        assert "project" not in found and "cwd" not in found and "session" not in found


def test_a_day_round_trips_with_its_counters_vendor_and_price() -> None:
    """The basic mirror: what went in comes back out, priced on its own day."""
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        usage = _usage(input=1_000, output=2_000, cache_read=3_000, cache_write_5m=4_000)
        written = store.upsert_day(
            "2026-09-09", _day("2026-09-09", **{FABLE: usage}), DEFAULT_PRICING
        )
        assert written == 1, written

        rows = store.rows()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row.day == "2026-09-09"
        assert row.key == FABLE
        assert row.vendor == "claude"
        assert row.model == FABLE, "the export must see the bare name, not a storage key"
        assert row.usage == usage
        expected = DEFAULT_PRICING.cost_usd(FABLE, usage, dt.date(2026, 9, 9))
        assert abs(row.usd_at_record - expected) < 1e-9, (row.usd_at_record, expected)
        assert row.usd_at_record > 0, "a priced model must not record $0"


def test_re_mirroring_an_unchanged_day_writes_nothing() -> None:
    """Idempotence, and the reason ``updated_at`` is trustworthy.

    The cost job mirrors the whole 30-day window every tick. If an unchanged
    cell were rewritten each time, ``updated_at`` would mean "when we last
    looked" instead of "when this changed", and an idle widget would rewrite
    the same rows 288 times a day.
    """
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        rollup = _day("2026-09-09", **{FABLE: _usage(input=10, output=20)})
        assert store.upsert_day("2026-09-09", rollup, DEFAULT_PRICING) == 1
        first = store.rows()[0].updated_at

        assert store.upsert_day("2026-09-09", rollup, DEFAULT_PRICING) == 0, (
            "an unchanged day must write no rows at all"
        )
        assert store.rows()[0].updated_at == first, (
            "an unchanged cell must keep the timestamp of its last real change"
        )
        assert store.count() == 1, "idempotence must not duplicate the row either"


def test_a_rebuild_that_lowers_a_cell_propagates() -> None:
    """The roadmap's opening incident, mirrored — and then corrected.

    The rollup store was add-only and inflated some day/model cells up to
    1,650x. A history that only ever INSERTed would have frozen that inflation
    into the permanent record: the whole point of REPLACE is that the rebuild
    which fixed the live store fixes the mirror too.
    """
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        inflated = _day("2026-09-09", **{FABLE: _usage(input=1_650_000)})
        store.upsert_day("2026-09-09", inflated, DEFAULT_PRICING)
        before = store.rows()[0]

        truth = _day("2026-09-09", **{FABLE: _usage(input=1_000)})
        assert store.upsert_day("2026-09-09", truth, DEFAULT_PRICING) == 1
        after = store.rows()[0]

        assert store.count() == 1, "a correction must replace the row, not add one"
        assert after.usage.input == 1_000, after.usage
        assert after.usd_at_record < before.usd_at_record, (
            "the recorded dollar figure must be corrected with the counters"
        )
        assert after.updated_at >= before.updated_at


def test_a_day_the_rollup_store_pruned_survives_in_history() -> None:
    """The reason this file exists: the cache is pruned, the record is not.

    ``DailyRollupStore.save`` prunes to ``lookback_days`` on every write and
    Claude Code prunes ``~/.claude/projects`` on its own schedule, so an aged-out
    day is unreconstructible. Once mirrored, it must stay mirrored.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        rollups = DailyRollupStore(path=root / "rollups.json", keep_days=3)
        rollups.add("2026-09-01", FABLE, _usage(input=111))
        rollups.add("2026-09-10", FABLE, _usage(input=222))

        store = _store(root)
        store.upsert_days(
            [rollups.get(day) for day in rollups.days()], DEFAULT_PRICING
        )
        assert store.days() == ("2026-09-01", "2026-09-10"), store.days()

        # The cache ages the old day out; the mirror is then re-run over what
        # the cache still holds, which is exactly what the cost job does.
        rollups.prune(today="2026-09-10", keep_days=3)
        assert rollups.get("2026-09-01") is None, "precondition: the cache pruned it"
        store.upsert_days(
            [rollups.get(day) for day in rollups.days()], DEFAULT_PRICING
        )

        assert store.days() == ("2026-09-01", "2026-09-10"), (
            "history must never lose a day the cache no longer covers"
        )
        aged = [row for row in store.rows() if row.day == "2026-09-01"]
        assert aged and aged[0].usage.input == 111, aged


def test_both_vendors_are_stored_under_their_own_vendor_column() -> None:
    """One day, two vendors, two rows — attributable without parsing the key."""
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        store.upsert_day(
            "2026-09-09",
            _day("2026-09-09", **{FABLE: _usage(input=10), SOL: _usage(input=20)}),
            DEFAULT_PRICING,
        )
        by_vendor = {row.vendor: row for row in store.rows()}
        assert set(by_vendor) == {"claude", "codex"}, by_vendor
        assert by_vendor["codex"].model == "gpt-5.6-sol", (
            "the storage prefix must never reach a user-visible name"
        )
        assert by_vendor["codex"].key == SOL


def test_an_empty_or_zero_day_writes_nothing_and_erases_nothing() -> None:
    """An empty day is not an erasure — this module has no DELETE at all."""
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        store.upsert_day("2026-09-09", _day("2026-09-09", **{FABLE: _usage(input=7)}))
        assert store.upsert_day("2026-09-09", _day("2026-09-09")) == 0
        assert store.upsert_day("2026-09-09", _day("2026-09-09", **{FABLE: _usage()})) == 0
        assert store.count() == 1, "an empty rollup must not wipe a recorded day"
        assert store.rows()[0].usage.input == 7

        source = Path(history_mod.__file__).read_text(encoding="utf-8").lower()
        assert "delete from" not in source, (
            "the append-only promise is structural: no DELETE statement may exist"
        )
        assert "drop table" not in source and "truncate table" not in source, (
            "nor may the table itself be dropped"
        )


def test_the_database_is_created_0600() -> None:
    """It is a record of someone's spend; the other state files are 0600 too."""
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        store.upsert_day("2026-09-09", _day("2026-09-09", **{FABLE: _usage(input=1)}))
        mode = stat.S_IMODE(store.path.stat().st_mode)
        assert mode == 0o600, oct(mode)
        assert not mode & (stat.S_IRGRP | stat.S_IROTH), oct(mode)


# ---------------------------------------------------------------------------
# 2. Failure is a menu line, never an exception
# ---------------------------------------------------------------------------


def test_a_corrupt_database_is_moved_aside_and_a_fresh_one_takes_over() -> None:
    """Truncated by a full disk, half-synced by a backup tool, or simply not ours.

    Refusing to record anything until a human notices would lose every day from
    here on, so the bad file is quarantined (never deleted — the bytes stay
    there to look at) and a new one is started. Both facts have to be visible:
    a log line and a ``!`` line.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        path = root / "history.sqlite"
        path.write_bytes(b"this is definitely not a sqlite database" * 8)
        logged: list[str] = []
        store = HistoryStore(path, logger=logged.append)

        written = store.upsert_day(
            "2026-09-09", _day("2026-09-09", **{FABLE: _usage(input=42)}), DEFAULT_PRICING
        )
        assert written == 1, "a fresh database must accept the write"
        assert store.rows()[0].usage.input == 42

        quarantined = path.with_name("history.sqlite.corrupt")
        assert quarantined.exists(), "the unreadable bytes must be kept, not dropped"
        assert quarantined.read_bytes().startswith(b"this is definitely not")
        assert any("unreadable" in line for line in logged), logged
        assert store.errors and "history" in store.errors[0], store.errors
        assert "corrupt" in store.errors[0], store.errors


def test_an_unwritable_state_directory_reports_and_does_not_raise() -> None:
    """A history failure must never cost the cost job the numbers it computed."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name) / "state"
        root.mkdir()
        store = _store(root)
        os.chmod(root, 0o500)
        try:
            written = store.upsert_day(
                "2026-09-09", _day("2026-09-09", **{FABLE: _usage(input=1)}), DEFAULT_PRICING
            )
        finally:
            os.chmod(root, 0o700)
        assert written == 0, written
        assert store.errors, "a silent failure is the one outcome that is not allowed"
        assert store.rows() == (), "a read must be empty, not an exception"
        assert store.count() == 0


def test_a_price_table_that_raises_still_records_the_tokens() -> None:
    """Counters are unreconstructible; a dollar figure is derived. Keep the counters."""

    class _Exploding:
        def cost_usd(self, *_args: Any, **_kwargs: Any) -> float:
            raise RuntimeError("no rates loaded")

    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        assert store.upsert_day(
            "2026-09-09", _day("2026-09-09", **{FABLE: _usage(input=99)}), _Exploding()
        ) == 1
        row = store.rows()[0]
        assert row.usage.input == 99
        assert row.usd_at_record == 0.0, row.usd_at_record


def test_a_transient_lock_is_not_treated_as_corruption() -> None:
    """``database is locked`` and ``not a database`` are both DatabaseError.

    Quarantining on the class alone would move a perfectly good database aside
    the first time two processes overlapped — a data-loss bug dressed as
    resilience — so the classifier keys on the message.
    """
    assert history_mod._is_corruption(sqlite3.DatabaseError("file is not a database"))
    assert history_mod._is_corruption(
        sqlite3.OperationalError("database disk image is malformed")
    )
    assert not history_mod._is_corruption(sqlite3.OperationalError("database is locked"))
    assert not history_mod._is_corruption(OSError("disk full"))


# ---------------------------------------------------------------------------
# 3. Export
# ---------------------------------------------------------------------------


def test_export_writes_into_the_injected_directory_only() -> None:
    """No test may ever write into a real ``~/Downloads``."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.upsert_day(
            "2026-09-09", _day("2026-09-09", **{FABLE: _usage(input=1)}), DEFAULT_PRICING
        )
        out = root / "Downloads"
        path = export_history(
            store, DEFAULT_PRICING, directory=out, fmt="csv", today="2026-09-10"
        )
        assert path == out / "usage-bar-2026-09-10.csv", path
        assert path.parent == out
        assert Path.home() not in path.parents


def test_the_csv_carries_both_dollar_columns_and_says_they_are_notional() -> None:
    """A rate change must read as a difference, never as a rewritten past.

    ``usd_at_record`` is what the day cost at the rates in effect on it;
    the second column reprices the same tokens at today's table. Both column
    names carry ``notional`` because a CSV has nowhere else to put the label
    without breaking every parser that opens it (SPEC 4.3).
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        usage = _usage(input=1_000_000, output=200_000)
        store.upsert_day(
            "2026-09-09", _day("2026-09-09", **{FABLE: usage}), DEFAULT_PRICING
        )
        path = export_history(
            store, DEFAULT_PRICING, directory=root, fmt="csv", today="2026-09-10"
        )
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))
        assert len(rows) == 1, rows
        row = rows[0]
        assert tuple(row.keys()) == EXPORT_FIELDS, tuple(row.keys())
        assert "notional" in "".join(EXPORT_FIELDS)
        assert row["day"] == "2026-09-09"
        assert row["vendor"] == "claude"
        assert row["model"] == FABLE
        assert int(row["input"]) == 1_000_000
        assert int(row["total_tokens"]) == usage.total_tokens
        assert float(row["usd_at_record_notional"]) > 0
        expected_today = DEFAULT_PRICING.cost_usd(FABLE, usage, dt.date(2026, 9, 10))
        assert abs(float(row["usd_at_today_rates_notional"]) - expected_today) < 1e-5


def test_the_two_dollar_columns_differ_when_a_rate_changed() -> None:
    """The whole reason there are two dollar columns.

    Sonnet 5's intro rate ran out on 2026-08-31 ($2 -> $3 per Mtok input). A day
    recorded while the intro rate was live must keep costing what it cost: if
    ``usd_at_record`` were recomputed at export time, every historical figure
    would silently move whenever a vendor changed a price, and the export would
    be a worse record than the menu.
    """
    sonnet = "claude-sonnet-5-20260514"
    tokens = _usage(input=1_000_000)
    intro = DEFAULT_PRICING.cost_usd(sonnet, tokens, dt.date(2026, 8, 20))
    standard = DEFAULT_PRICING.cost_usd(sonnet, tokens, dt.date(2026, 9, 10))
    assert intro != standard, (
        "fixture precondition: this model's published rate must have changed"
    )

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.upsert_day(
            "2026-08-20", _day("2026-08-20", **{sonnet: tokens}), DEFAULT_PRICING
        )
        path = export_history(
            store, DEFAULT_PRICING, directory=root, fmt="csv", today="2026-09-10"
        )
        row = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))[0]
        assert abs(float(row["usd_at_record_notional"]) - intro) < 1e-6, row
        assert abs(float(row["usd_at_today_rates_notional"]) - standard) < 1e-6, row
        assert row["usd_at_record_notional"] != row["usd_at_today_rates_notional"]


def test_the_json_export_names_the_notional_label_in_full() -> None:
    """JSON has room for the words, so it must carry them (SPEC 4.3)."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.upsert_day(
            "2026-09-09",
            _day("2026-09-09", **{FABLE: _usage(input=10), SOL: _usage(output=20)}),
            DEFAULT_PRICING,
        )
        path = export_history(
            store, DEFAULT_PRICING, directory=root, fmt="json", today="2026-09-10"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["notional_label"] == NOTIONAL_LABEL
        assert payload["row_count"] == 2 == len(payload["rows"])
        assert payload["generated_day"] == "2026-09-10"
        assert set(payload["rows"][0]) == set(EXPORT_FIELDS)
        assert {row["vendor"] for row in payload["rows"]} == {"claude", "codex"}


def test_an_empty_history_exports_a_header_and_no_rows() -> None:
    """An honest empty file beats a silent no-op that looks like success."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        csv_path = export_history(store, DEFAULT_PRICING, directory=root, fmt="csv",
                                  today="2026-09-10")
        text = csv_path.read_text(encoding="utf-8")
        assert text.splitlines()[0] == ",".join(EXPORT_FIELDS)
        assert len(text.splitlines()) == 1, text
        json_path = export_history(store, DEFAULT_PRICING, directory=root, fmt="json",
                                   today="2026-09-10")
        assert json.loads(json_path.read_text(encoding="utf-8"))["rows"] == []


def test_exporting_with_history_off_creates_no_database() -> None:
    """The off switch means off from every direction.

    ``Export usage …`` still works on a machine whose mirror is disabled - it
    exports whatever was recorded before the switch was flipped - but it must
    not be the thing that brings the file into existence.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        assert store.rows() == ()
        assert store.days() == ()
        assert store.count() == 0
        assert not store.path.exists(), "a read must not create the database"

        path = export_history(
            store, DEFAULT_PRICING, directory=root, fmt="json", today="2026-09-10"
        )
        assert json.loads(path.read_text(encoding="utf-8"))["rows"] == []
        assert not store.path.exists(), "an export must not create the database either"
        assert store.errors == (), store.errors


def test_an_unknown_export_format_is_refused_by_name() -> None:
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        for bad in ("xlsx", "", "CSV.", "sqlite"):
            try:
                export_filename(bad)
            except ValueError as exc:
                assert bad in str(exc) or "expected" in str(exc), str(exc)
            else:
                raise AssertionError(f"{bad!r} was accepted as an export format")
        assert export_filename("JSON", today="2026-09-10") == "usage-bar-2026-09-10.json"


def test_the_export_refuses_to_guess_a_destination() -> None:
    """No default directory, so no silent fallback into a real Downloads.

    The first version of this module defaulted to :data:`DEFAULT_EXPORT_DIR`
    when the caller passed nothing. A single mutated line then wrote two files
    into the operator's actual ``~/Downloads`` while every test still reported
    green - the destination has to be the caller's decision, and an omitted one
    has to be an error rather than a guess.
    """
    import inspect

    signature = inspect.signature(export_history)
    assert signature.parameters["directory"].default is inspect.Parameter.empty, (
        "an export destination must never have a default"
    )
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        try:
            export_history(store, DEFAULT_PRICING, directory=None, fmt="csv")
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError("an absent directory was accepted")


def test_the_export_file_is_0600() -> None:
    """It is a spend record landing in a folder other things can read."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.upsert_day("2026-09-09", _day("2026-09-09", **{FABLE: _usage(input=1)}))
        path = export_history(store, DEFAULT_PRICING, directory=root, fmt="csv",
                              today="2026-09-10")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, oct(mode)


# ---------------------------------------------------------------------------
# 4. Wiring: the cost job, the settings switch, the menu
# ---------------------------------------------------------------------------


def test_the_mirror_lives_beside_the_rollup_store_it_mirrors() -> None:
    """A redirected cache must never append fixture days to the real record.

    The cost-job regression tests drive the real ``_run_cost_job`` against a
    rollup store in a ``TemporaryDirectory``. Resolving the mirror to the
    installed ``WIDGET_HOME`` regardless would have made every one of those runs
    write into the operator's own long-term history — silently, since the file
    is git-ignored and nothing ever reads it back in a test.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        rollups = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        assert history_mod.history_path_for(rollups.path) == root / "history.sqlite"

        # No store at all (nothing wired yet) falls back to the widget home.
        assert history_mod.history_path_for(None) == HISTORY_DB_PATH

        # In production the two rules agree, which is why nothing moves.
        assert history_mod.history_path_for(HISTORY_DB_PATH.parent / "rollups.json") == (
            HISTORY_DB_PATH
        )

        # An operator who names a path means it - and the pin is read at
        # IMPORT, because `HISTORY_DB_PATH` is a module constant built from the
        # environment (contracts). So the pin is proved where an operator
        # actually sets it: in the environment of a fresh process. Setting it
        # in THIS one after the import has happened cannot move the constant,
        # and a test that set it here and asserted the default path back was
        # asserting the opposite of the guarantee it was labelled with.
        # In a directory of its own, and NOT named `history.sqlite`: the
        # co-location rule builds `<rollups dir>/HISTORY_DB_PATH.name`, and
        # HISTORY_DB_PATH itself is built from this very variable - so a pin
        # that differs only in stem is reproduced by the fallback and the
        # assertion passes without the pin ever being honoured.
        pinned = root / "pins" / "elsewhere.sqlite"
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from cc_usage_widget.history import history_path_for;"
                " print(history_path_for(sys.argv[1]))",
                str(rollups.path),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env={**os.environ, history_mod.HISTORY_DB_ENV: str(pinned)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == str(pinned), (probe.stdout, probe.stderr)

        # ... and with no pin in the environment, the same call co-locates.
        clean = {k: v for k, v in os.environ.items() if k != history_mod.HISTORY_DB_ENV}
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from cc_usage_widget.history import history_path_for;"
                " print(history_path_for(sys.argv[1]))",
                str(rollups.path),
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=clean,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == str(root / "history.sqlite"), probe.stdout


def test_a_real_cost_job_mirrors_the_window_into_history() -> None:
    """End to end: a real transcript, the real indexer, the real cost job.

    No fake scanner and no monkeypatched parser — the only thing redirected is
    where the mirror lives, so what this asserts is that the hook is wired into
    ``_run_cost_job`` at all.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        _write_transcript(
            root / "projects" / "p" / "a.jsonl",
            [
                _record("req-today", now, input_tokens=1_000, output_tokens=2_000,
                        cache_read_input_tokens=100_000, cache_creation=200_000),
                _record("req-old", now - 2 * 86_400, input_tokens=1_000,
                        output_tokens=2_000, cache_creation=200_000),
            ],
        )
        with _Redirect(root):
            indexer = Indexer(
                projects_dir=root / "projects",
                state_path=root / "scan_state.json",
                lookback_days=30,
                pricing=DEFAULT_PRICING,
                defer_state_commit=True,
            )
            rollups = DailyRollupStore(path=root / "rollups.json", keep_days=30)
            worker = _worker(root, indexer=indexer, rollups=rollups)
            for _ in range(40):
                worker._run_cost_job()
                if indexer.progress().complete:
                    break
            worker._flush()

            assert worker.history_errors == (), worker.history_errors
            store = HistoryStore(root / "history.sqlite")
            today = local_day_key(now)
            older = local_day_key(now - 2 * 86_400)
            assert set(store.days()) == {older, today}, store.days()
            rows = {row.day: row for row in store.rows()}
            assert rows[today].usage.input == 1_000, rows[today].usage
            assert rows[today].usage.cache_read == 100_000
            assert rows[older].usage.output == 2_000
            assert rows[today].usd_at_record > 0


def test_history_disabled_never_creates_the_file() -> None:
    """The off switch is real: no database, no sidecars, no directory entry."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        with _Redirect(root):
            rollups = DailyRollupStore(path=root / "rollups.json", keep_days=30)
            rollups.add("2026-09-10", FABLE, _usage(input=1_000))
            worker = _worker(root, rollups=rollups)
            worker._snapshot = worker._snapshot.__class__(
                **{
                    **{
                        field: getattr(worker._snapshot, field)
                        for field in worker._snapshot.__dataclass_fields__
                    },
                    "settings": normalize_settings(
                        {**SETTINGS_DEFAULTS, "history_enabled": False}
                    ),
                }
            )
            assert worker._settings()["history_enabled"] is False
            worker._mirror_history(rollups, 30, "2026-09-10")

            assert not (root / "history.sqlite").exists(), (
                "an off feature must not create its state file"
            )
            assert worker.history_errors == ()

            # And back on again: the switch is reversible without a restart.
            worker._snapshot = worker._snapshot.__class__(
                **{
                    **{
                        field: getattr(worker._snapshot, field)
                        for field in worker._snapshot.__dataclass_fields__
                    },
                    "settings": normalize_settings(dict(SETTINGS_DEFAULTS)),
                }
            )
            worker._mirror_history(rollups, 30, "2026-09-10")
            assert (root / "history.sqlite").exists()
            assert HistoryStore(root / "history.sqlite").count() == 1


def test_history_enabled_is_declared_so_normalize_settings_keeps_it() -> None:
    """A key only ``app.py`` knows about is dropped on the next save."""
    assert "history_enabled" in SETTINGS_DEFAULTS
    assert SETTINGS_DEFAULTS["history_enabled"] is True
    assert normalize_settings({"history_enabled": False})["history_enabled"] is False
    assert normalize_settings({})["history_enabled"] is True
    assert HISTORY_DB_PATH.name == "history.sqlite", HISTORY_DB_PATH


def test_the_export_command_writes_the_file_and_records_where() -> None:
    """The menu callback enqueues; the worker does the sqlite read and the write."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        with _Redirect(root):
            rollups = DailyRollupStore(path=root / "rollups.json", keep_days=30)
            rollups.add("2026-09-10", FABLE, _usage(input=1_000, output=2_000))
            worker = _worker(root, rollups=rollups)
            worker._mirror_history(rollups, 30, "2026-09-10")

            for fmt in ("csv", "json"):
                worker._handle_command((app_mod._CMD_EXPORT_HISTORY, fmt))
                expected = root / "Downloads" / f"usage-bar-{local_day_key(time.time())}.{fmt}"
                assert expected.exists(), (fmt, sorted((root / "Downloads").iterdir()))
                assert worker.history_errors == (), worker.history_errors
                assert worker.history_note and str(expected) in worker.history_note

            rows = list(
                csv.DictReader(
                    io.StringIO(
                        (root / "Downloads" / f"usage-bar-{local_day_key(time.time())}.csv")
                        .read_text(encoding="utf-8")
                    )
                )
            )
            assert len(rows) == 1 and int(rows[0]["input"]) == 1_000, rows


def test_the_cost_section_offers_both_exports_only_when_there_is_something_to_export() -> None:
    """Three states, and only one of them is clickable.

    The bug this pins down shipped: the two items were offered unconditionally,
    so on a machine that had never mirrored a day - ``history_enabled`` off, or
    simply a first run - clicking one wrote a header-only CSV into
    ``~/Downloads`` and opened a Finder window on it. A file with a header and
    no rows is not "no answer"; read a week later it is the claim that nothing
    was spent (SPEC 4.3, and the reason the menu would rather say
    "nothing recorded yet" than hand over a document).
    """
    app = app_mod.CCUsageWidgetApp()
    try:
        on = normalize_settings(dict(SETTINGS_DEFAULTS))
        snapshot = UiSnapshot(settings=on)

        # 1. Nothing mirrored: one honest line, and NOTHING clickable.
        app._worker._history_rows = 0
        empty = app._export_items(snapshot)
        assert [str(item.title) for item in empty] == [
            "! history: nothing recorded yet"
        ], [str(item.title) for item in empty]
        assert all(item.callback is None for item in empty), "not a click target"

        # 2. The mirror holds cells: the two items, both clickable.
        app._worker._history_rows = 12
        items = app._export_items(snapshot)
        titles = [str(item.title) for item in items]
        assert titles == ["Export usage (CSV)…", "Export usage (JSON)…"], titles
        assert all(item.callback is not None for item in items), titles

        # 3. The feature is off: not one line, whatever the worker holds.
        off = normalize_settings({**SETTINGS_DEFAULTS, "history_enabled": False})
        assert app._export_items(UiSnapshot(settings=off)) == []
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_cost_section_is_byte_for_byte_the_pre_roadmap_one_with_history_off() -> None:
    """The off switch, measured where a user would notice it.

    Not "roughly the same": the same list of strings, with a real store, a real
    price table and a real breakdown behind it. ``history_enabled`` false must
    draw the Cost section this widget drew before roadmap item 8 existed - no
    Export items, no ``!`` line about a mirror that is deliberately not there,
    no blank line where a heading used to be.
    """
    from cc_usage_widget.contracts import IndexProgress

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        today = local_day_key(time.time())
        rollups = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        rollups.merge([_day(today, **{FABLE: ModelUsage(input=1_000, output=500)})])
        breakdown = rollups.cost_breakdown(
            DEFAULT_PRICING, today=today, progress=IndexProgress(complete=True)
        )
        app = app_mod.CCUsageWidgetApp()
        try:
            app._worker._rollups = rollups
            app._worker._pricing = DEFAULT_PRICING
            app._worker._indexer = Indexer(
                projects_dir=root / "projects",
                state_path=root / "scan_state.json",
                lookback_days=30,
                pricing=DEFAULT_PRICING,
            )
            # A worker that HAS mirrored days: the off switch must win anyway.
            app._worker._history_rows = 40

            off = normalize_settings(
                {**SETTINGS_DEFAULTS, "history_enabled": False, "dashboard_enabled": False}
            )
            on = normalize_settings({**SETTINGS_DEFAULTS, "dashboard_enabled": False})
            drawn_off = [
                str(item.title)
                for item in app._cost_items(UiSnapshot(settings=off, cost=breakdown))
            ]
            drawn_on = [
                str(item.title)
                for item in app._cost_items(UiSnapshot(settings=on, cost=breakdown))
            ]
            assert not any("Export" in line for line in drawn_off), drawn_off
            assert not any("history" in line for line in drawn_off), drawn_off
            # ... and the difference is EXACTLY the two export items, in place.
            assert drawn_on == drawn_off + [
                "Export usage (CSV)…",
                "Export usage (JSON)…",
            ], (drawn_on, drawn_off)
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_an_export_with_an_empty_mirror_writes_no_file_and_asks_for_no_reveal() -> None:
    """The belt to the menu's braces, on the worker where the file would land.

    A command can be enqueued from a menu painted a minute ago, so the refusal
    cannot live only in ``_export_items``. What must be true afterwards: no file
    in the export directory, no reveal parked for the AppKit thread, and a
    ``!`` line saying which of the two reasons it was.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        with _Redirect(root):
            rollups = DailyRollupStore(path=root / "rollups.json", keep_days=30)
            worker = _worker(root, rollups=rollups)

            # 1. On, but nothing has ever been mirrored.
            worker._handle_command((app_mod._CMD_EXPORT_HISTORY, "csv"))
            assert not (root / "Downloads").exists(), sorted(root.iterdir())
            assert worker.take_desktop_requests() == ()
            assert worker.history_errors == ("history: nothing recorded yet",), (
                worker.history_errors
            )
            assert worker.history_note is None

            # 2. Off: the same refusal, in its own words, and still no file.
            worker._snapshot = UiSnapshot(
                settings=normalize_settings(
                    {**SETTINGS_DEFAULTS, "history_enabled": False}
                )
            )
            worker._handle_command((app_mod._CMD_EXPORT_HISTORY, "json"))
            assert not (root / "Downloads").exists(), sorted(root.iterdir())
            assert worker.take_desktop_requests() == ()
            assert worker.history_errors == (
                "history: history is off - nothing is being recorded",
            ), worker.history_errors
            # The database was never even opened: off means off from every
            # direction, including this one.
            assert not (root / "history.sqlite").exists(), sorted(root.iterdir())


def test_the_export_reveal_is_parked_for_the_appkit_thread_never_called_by_the_worker() -> None:
    """``NSWorkspace`` is AppKit; the worker is not the AppKit thread.

    Two halves, and the first is structural: no method of ``BackgroundWorker``
    may name either desktop helper. A behavioural assertion alone would pass
    for ever once the tests set ``CC_USAGE_WIDGET_NO_REVEAL`` - the guard makes
    a wrong-thread call look exactly like no call at all, which is precisely
    how this shipped.
    """
    import inspect

    source = inspect.getsource(BackgroundWorker)
    assert "_reveal_in_finder(" not in source, "the worker must not reveal"
    assert "_open_in_browser(" not in source, "the worker must not open"

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        with _Redirect(root):
            rollups = DailyRollupStore(path=root / "rollups.json", keep_days=30)
            rollups.add("2026-09-10", FABLE, _usage(input=1_000, output=2_000))
            worker = _worker(root, rollups=rollups)
            worker._mirror_history(rollups, 30, "2026-09-10")
            worker._handle_command((app_mod._CMD_EXPORT_HISTORY, "csv"))

            parked = worker.take_desktop_requests()
            assert len(parked) == 1, parked
            action, path = parked[0]
            assert action == app_mod._DESKTOP_REVEAL, action
            assert Path(path).exists(), path
            # Drained exactly once: a second repaint must not open a second
            # Finder window on the same file.
            assert worker.take_desktop_requests() == ()

            # And the AppKit side really performs it. The seam is on the app
            # instance, so nothing module-wide is patched and the settings
            # reveal callbacks keep their own behaviour.
            app = app_mod.CCUsageWidgetApp()
            try:
                seen: list[tuple[str, str]] = []
                app._desktop_handoff = lambda action, path: (  # type: ignore[assignment]
                    seen.append((action, str(path))) or True
                )
                app._worker._ask_desktop(app_mod._DESKTOP_REVEAL, path)
                assert app._drain_desktop_handoffs() == 1
                assert seen == [(app_mod._DESKTOP_REVEAL, str(path))], seen
            finally:
                app._running = False
                app._worker.stop(timeout=2.0)


def test_replace_day_zeroes_a_phantom_key_rather_than_leaving_or_deleting_it() -> None:
    """The audit-repair path (roadmap item 2), at the mirror.

    ``upsert_day`` is a union and cannot lower a cell the fresh rollup does not
    mention at all - which is the shape of the incident that started this
    roadmap: an inflated ``gpt-5.6-sol`` row for a day the corpus says had no
    Codex traffic. ``replace_day`` makes the day EXACTLY the rollup, and the
    phantom becomes a zero row rather than a deletion, because this module's
    whole promise is that it never deletes.
    """
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        day = "2026-09-09"
        store.upsert_day(
            day,
            _day(day, **{FABLE: _usage(input=1_000), SOL: _usage(input=99_000_000)}),
            DEFAULT_PRICING,
        )
        assert {row.key: row.total_tokens for row in store.rows()} == {
            FABLE: 1_000,
            SOL: 99_000_000,
        }

        # The corpus, re-indexed: that day had no Codex traffic at all.
        written = store.replace_day(
            day, _day(day, **{FABLE: _usage(input=1_000)}), DEFAULT_PRICING
        )
        assert written == 1, "only the phantom moved"
        after = {row.key: row for row in store.rows()}
        assert set(after) == {FABLE, SOL}, "a row is corrected, never removed"
        assert after[SOL].total_tokens == 0, after[SOL]
        assert after[SOL].usd_at_record == 0.0, after[SOL]
        assert after[SOL].vendor == VENDOR_CODEX, "the zeroed row keeps its vendor"
        assert after[FABLE].total_tokens == 1_000, "the true cell is untouched"

        # Idempotent: repairing a repaired day writes nothing.
        assert store.replace_day(
            day, _day(day, **{FABLE: _usage(input=1_000)}), DEFAULT_PRICING
        ) == 0

        # And it is one DAY that is replaced, not the table.
        store.upsert_day(
            "2026-09-08", _day("2026-09-08", **{SOL: _usage(input=7)}), DEFAULT_PRICING
        )
        store.replace_day(day, _day(day), DEFAULT_PRICING)
        others = {
            row.key: row.total_tokens for row in store.rows() if row.day == "2026-09-08"
        }
        assert others == {SOL: 7}, others
        assert store.errors == (), store.errors


def test_a_history_failure_shows_as_a_bang_line_in_the_menu() -> None:
    """Rule 12: a feature that broke says so, rather than going quiet."""
    app = app_mod.CCUsageWidgetApp()
    try:
        snapshot = UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS)))
        assert not any(
            "history" in str(item.title) for item in app._problem_items(snapshot)
        ), "a healthy mirror must add no line at all"

        app._worker._history_errors = ("history: database was unreadable",)
        titles = [str(item.title) for item in app._problem_items(snapshot)]
        assert any(t.startswith("! history:") for t in titles), titles
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Runner (pytest is not installed in claude-swap's venv)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The project dimension (roadmap item 6)
# ---------------------------------------------------------------------------


def test_the_project_table_holds_one_path_component_and_no_other_free_text() -> None:
    """The privacy promise for the second table, as a column list.

    ``daily_project`` is the only place in this database where a string from a
    transcript is stored, and it is bounded by contract to ONE path component
    (``attribution.py`` caps it at 64 characters). Asserting the exact column
    set makes adding a second such string a test change, and therefore a
    conversation - the same rule ``daily`` has lived under since it shipped.
    """
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        store.upsert_projects(
            [("2026-09-10", "widget", _day("2026-09-10", **{FABLE: _usage(input=5)}))],
            DEFAULT_PRICING,
        )
        with sqlite3.connect(str(store.path)) as conn:
            found = tuple(row[1] for row in conn.execute("PRAGMA table_info(daily_project)"))
        assert found == PROJECT_COLUMNS, found
        assert set(found) - set(COLUMNS) == {"project"}, (
            "the project table may differ from the aggregate one by exactly one column"
        )
        assert "cwd" not in found and "session" not in found and "branch" not in found


def test_a_project_day_round_trips_and_stays_out_of_the_aggregate_rows() -> None:
    """Two populations, one file. ``rows()`` must keep meaning what it meant.

    Every existing caller - the export above all - asks for the whole-day
    figures. A project row leaking into that answer would double the export.
    """
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        day = _day("2026-09-10", **{FABLE: _usage(input=100), SOL: _usage(input=40)})
        store.upsert_day("2026-09-10", day, DEFAULT_PRICING)
        store.upsert_projects(
            [
                ("2026-09-10", "widget", _day("2026-09-10", **{FABLE: _usage(input=60)})),
                ("2026-09-10", "obsidian", _day("2026-09-10", **{FABLE: _usage(input=40)})),
            ],
            DEFAULT_PRICING,
        )
        assert {row.key for row in store.rows()} == {FABLE, SOL}
        assert all(row.project == "" for row in store.rows()), store.rows()

        by_project = {row.project: row for row in store.project_rows()}
        assert set(by_project) == {"widget", "obsidian"}, by_project
        assert by_project["widget"].usage.input == 60
        assert by_project["widget"].vendor == "claude"
        assert by_project["widget"].usd_at_record > 0, "priced on its own day"


def test_a_project_row_is_replaced_not_accumulated() -> None:
    """A rebuild or an audit repair must propagate, exactly as it does for a day."""
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        rows = [("2026-09-10", "widget", _day("2026-09-10", **{FABLE: _usage(input=100)}))]
        assert store.upsert_projects(rows, DEFAULT_PRICING) == 1
        assert store.upsert_projects(rows, DEFAULT_PRICING) == 0, "idempotent"
        store.upsert_projects(
            [("2026-09-10", "widget", _day("2026-09-10", **{FABLE: _usage(input=40)}))],
            DEFAULT_PRICING,
        )
        got = store.project_rows()
        assert len(got) == 1 and got[0].usage.input == 40, got


def test_an_unnamed_project_is_skipped_rather_than_stored_as_a_phantom() -> None:
    """An empty name is a bug upstream; storing it would invent a project."""
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        assert store.upsert_projects(
            [("2026-09-10", "", _day("2026-09-10", **{FABLE: _usage(input=100)}))]
        ) == 0
        assert store.project_rows() == ()


def test_a_v1_database_gains_the_project_table_without_touching_a_row() -> None:
    """The migration is additive: every v1 row is byte-identical afterwards.

    ``history.sqlite`` is the only record of a day that has aged out of both
    ``rollups.json`` and ``~/.claude/projects``, so the bar for a schema change
    here is not "no row was lost" but "no row was read".
    """
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "history.sqlite"
        # A genuine v1 file: the shipped schema, written by hand.
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                "CREATE TABLE daily ("
                " day TEXT NOT NULL, key TEXT NOT NULL, vendor TEXT NOT NULL,"
                " input INTEGER NOT NULL, output INTEGER NOT NULL,"
                " cache_read INTEGER NOT NULL, cache_write_5m INTEGER NOT NULL,"
                " cache_write_1h INTEGER NOT NULL, usd_at_record REAL NOT NULL,"
                " updated_at REAL NOT NULL, PRIMARY KEY (day, key))"
            )
            conn.execute(
                "INSERT INTO daily VALUES ('2026-08-01', ?, 'claude', 7, 0, 0, 0, 0, 0.5, 1.0)",
                (FABLE,),
            )
            conn.execute("PRAGMA user_version=1")
            conn.commit()
            before = conn.execute("SELECT * FROM daily").fetchall()

        store = HistoryStore(path)
        assert store.project_rows() == (), "a v1 file simply has no project rows"
        store.upsert_projects(
            [("2026-09-10", "widget", _day("2026-09-10", **{FABLE: _usage(input=9)}))],
            DEFAULT_PRICING,
        )

        with sqlite3.connect(str(path)) as conn:
            after = conn.execute("SELECT * FROM daily").fetchall()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert after == before, ("the migration rewrote a v1 row", before, after)
        assert version == 2, version
        # And the aged-out day is still readable through the normal accessor.
        assert [row.day for row in store.rows()] == ["2026-08-01"]
        assert [row.project for row in store.project_rows()] == ["widget"]


def test_a_repaired_days_project_rows_are_zeroed_for_that_vendor_only() -> None:
    """``drop_project_day`` - the per-project half of an audit repair.

    The audit rebuilds a day's AGGREGATE from the corpus, and nothing in its
    plan says how to decompose it. The split of that day therefore has to go,
    or the dashboard keeps quoting a decomposition of a figure the aggregate
    has already disowned. Zeroed rather than deleted, exactly as
    ``replace_day`` does it: the row is the record that this key was once
    believed to have spent something here, and the honest correction is "and
    the correction is nothing".
    """
    with tempfile.TemporaryDirectory() as name:
        store = _store(Path(name))
        store.upsert_projects(
            [
                ("2026-09-10", "widget", _day("2026-09-10", **{FABLE: _usage(input=100)})),
                ("2026-09-10", "widget", _day("2026-09-10", **{SOL: _usage(input=70)})),
                ("2026-09-10", "obsidian", _day("2026-09-10", **{FABLE: _usage(input=40)})),
                ("2026-09-09", "widget", _day("2026-09-09", **{FABLE: _usage(input=25)})),
            ],
            DEFAULT_PRICING,
        )
        assert len(store.project_rows()) == 4

        written = store.drop_project_day("2026-09-10", "claude")
        assert written == 2, written

        rows = {(row.day, row.project, row.key): row for row in store.project_rows()}
        assert len(rows) == 4, "a row was deleted rather than zeroed"
        assert rows[("2026-09-10", "widget", FABLE)].total_tokens == 0
        assert rows[("2026-09-10", "widget", FABLE)].usd_at_record == 0.0
        assert rows[("2026-09-10", "obsidian", FABLE)].total_tokens == 0
        # The other vendor's decomposition of that day is not what was rebuilt.
        assert rows[("2026-09-10", "widget", SOL)].usage.input == 70
        # ...and neither is another day.
        assert rows[("2026-09-09", "widget", FABLE)].usage.input == 25

        assert store.drop_project_day("2026-09-10", "claude") == 0, (
            "a second drop rewrote rows that already hold nothing"
        )
        assert store.errors == (), store.errors


def test_the_project_rows_this_mirror_stores_reach_the_dashboard() -> None:
    """The consumer side of ``upsert_projects``, end to end.

    ``daily_project`` exists to be read by something, and the only reader is
    the dashboard's "By project" table. That table asked ``rows()`` - the
    aggregate, whose ``project`` is empty by contract - until 2026-09-10, so
    every project this mirror stored was written and never displayed. Asserting
    the page from this side means a future change to either name (the method or
    the column) breaks a test rather than the section.
    """
    from cc_usage_widget.dashboard import render_dashboard

    today = local_day_key(time.time())
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.upsert_day(today, _day(today, **{FABLE: _usage(input=3_300_000)}), DEFAULT_PRICING)
        store.upsert_projects(
            [
                (today, "obsidian", _day(today, **{FABLE: _usage(input=2_200_000)})),
                (today, "widget", _day(today, **{FABLE: _usage(input=1_100_000)})),
            ],
            DEFAULT_PRICING,
        )
        page = render_dashboard(
            store,
            DailyRollupStore(path=root / "rollups.json", keep_days=30),
            (),
            (),
            now=time.time(),
            pricing=DEFAULT_PRICING,
        )
    assert "<h2>By project</h2>" in page
    assert "obsidian" in page and "widget" in page
    assert "2,200,000" in page, "the page must print the tokens this store holds"


def test_the_export_is_unchanged_by_the_project_dimension() -> None:
    """A machine with the feature off writes exactly the file it used to.

    The export reads the aggregate rows only, so a database that also holds a
    project decomposition exports the same bytes as one that does not - the
    off-switch guarantee, in the one artifact a user keeps.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        plain = HistoryStore(root / "plain.sqlite")
        both = HistoryStore(root / "both.sqlite")
        day = _day("2026-09-10", **{FABLE: _usage(input=100)})
        for store in (plain, both):
            store.upsert_day("2026-09-10", day, DEFAULT_PRICING, now=1.0)
        both.upsert_projects(
            [("2026-09-10", "widget", _day("2026-09-10", **{FABLE: _usage(input=100)}))],
            DEFAULT_PRICING,
            now=1.0,
        )
        out = root / "out"
        first = export_history(
            plain, DEFAULT_PRICING, directory=out / "a", fmt="csv", today="2026-09-10"
        )
        second = export_history(
            both, DEFAULT_PRICING, directory=out / "b", fmt="csv", today="2026-09-10"
        )
        assert first.read_text() == second.read_text(), (
            "the project rows changed the export"
        )
        assert "project" not in first.read_text()


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
