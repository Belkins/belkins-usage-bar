"""State backup + restore around ``Rebuild cost index`` (roadmap item 17).

The rebuild is the one menu action that destroys data, and it is not idempotent
against a shrinking corpus: Claude Code prunes ``~/.claude/projects`` on
``cleanupPeriodDays`` and Codex rotates its rollouts, so a day the store
recorded weeks ago may have no transcript left to re-read. On 2026-09-10 the
recovery was done by hand - copy the four state files aside, rebuild, copy them
back. These tests pin that recovery down as code.

Four invariants, each of which was a real failure mode of the by-hand version:

===  =============================================================  =========================================================
 1   the snapshot exists BEFORE anything is cleared                  :func:`test_the_rebuild_backs_the_state_up_before_it_clears_anything`
 2   a snapshot that cannot be written aborts the rebuild            :func:`test_a_backup_that_cannot_be_written_aborts_the_rebuild`
 3   a restore is not half-applied: the process stops writing        :func:`test_a_restore_reloads_the_store_and_stops_the_widget_writing`
 4   neither destructive action runs without being confirmed         :func:`test_the_two_destructive_items_ask_before_they_act`
===  =============================================================  =========================================================

Invariant 3 is the subtle one. A scanner loads its offsets once and caches
them, so after a restore the FILES are the pre-rebuild ones while this
process's offsets are the post-rebuild ones. Scanning would credit records the
restored rollup already contains (a double count); saving would put the
rebuild's state back on top of the restore (an undercount). Both are exactly
what ``_reconcile_lost_scan_state`` exists to prevent, so the restore freezes
the cost side and says so until the widget is relaunched.

Run with pytest if it is available, or directly::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    $PY tests/test_statebackup.py
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder or a modal

from cc_usage_widget import statebackup  # noqa: E402
from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget.attribution import Attribution, AttributionStore  # noqa: E402
from cc_usage_widget.app import BackgroundWorker, UiSnapshot  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    DayRollup,
    ModelUsage,
    local_day_key,
    normalize_settings,
)
from cc_usage_widget.pricing import DEFAULT_PRICING  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures - real files in a temporary directory, never the widget's own home
# ---------------------------------------------------------------------------


def _seed_home(root: Path, *, tokens: int = 1_000) -> tuple[DailyRollupStore, str]:
    """A home with a real ``rollups.json``, its scan states and its attribution.

    The attribution store is a REAL one writing its own file rather than a
    hand-typed JSON blob, because what the backup has to survive is the file
    that ``_rebuild_index`` clears - and the only thing that knows its shape is
    the store that writes it.
    """
    store = DailyRollupStore(path=root / "rollups.json", keep_days=30)
    day = local_day_key(time.time())
    store.merge([DayRollup(day=day, models={"claude-fable-5": ModelUsage(input=tokens)})])
    store.save(force=True)
    (root / "scan_state.json").write_text('{"/a.jsonl": {"offset": 12}}', encoding="utf-8")
    (root / "scan_state_dedup.json").write_text('{"day": "2026-09-10"}', encoding="utf-8")
    (root / "codex_scan_state.json").write_text('{"/b.jsonl": {"offset": 34}}', encoding="utf-8")
    _seed_attribution(root, day, tokens=tokens)
    return store, day


def _seed_attribution(root: Path, day: str, *, tokens: int = 1_000) -> AttributionStore:
    """A real ``attribution.json`` beside the rollup it decomposes."""
    store = AttributionStore(path=root / "attribution.json", keep_days=30)
    store.merge(
        [
            (
                Attribution(vendor="claude", project="usage-bar", session="5909f788"),
                DayRollup(day=day, models={"claude-fable-5": ModelUsage(input=tokens)}),
            )
        ]
    )
    store.save(force=True)
    return store


class _ForbiddenScanner:
    """A scanner that fails the test on ANY attribute access.

    The negative control for the restore freeze: "the cost job did not run" is
    only worth asserting if using the scanner at all is loud. ``getattr(obj,
    name, default)`` swallows :class:`AttributeError` and nothing else, so this
    cannot be mistaken for an absent attribute.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the frozen cost side touched the scanner: {name}")


def _worker(store: DailyRollupStore, scanner: Any) -> BackgroundWorker:
    worker = BackgroundWorker(
        publish=lambda _s: None,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        rollups=store,
        pricing=DEFAULT_PRICING,
    )
    worker._indexer = scanner
    return worker


# ---------------------------------------------------------------------------
# The module itself
# ---------------------------------------------------------------------------


def test_a_backup_copies_every_state_file_and_nothing_else() -> None:
    """The five files a rebuild destroys, at 0600, and no third-party junk.

    ``scan_state_dedup.json`` is in the set on purpose: the sidecar describes
    requests credited under the offsets it ships with, so restoring one without
    the other would suppress exactly the records the other says still need
    reading (``indexer._ensure_states_loaded``).

    ``attribution.json`` is in it for the same reason one dimension down:
    ``_rebuild_index`` calls ``attribution.clear()`` right after it empties the
    rollup store, so a backup without it hands back the day totals and leaves
    the per-project store empty beside them.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _seed_home(root)
        (root / "settings.json").write_text("{}", encoding="utf-8")  # not state
        (root / "widget.lock").write_text("", encoding="utf-8")
        (root / "codex_scan_state_quota.json").write_text("{}", encoding="utf-8")

        backup = statebackup.create_backup(root, now=1_757_500_000.0)
        assert backup is not None
        assert backup.name.startswith(statebackup.BACKUP_PREFIX), backup.name
        copied = sorted(path.name for path in backup.iterdir())
        assert copied == [
            "attribution.json",
            "codex_scan_state.json",
            "rollups.json",
            "scan_state.json",
            "scan_state_dedup.json",
        ], copied
        # The live quota sidecar is deliberately NOT copied: it caches someone
        # else's live figures and is re-fetched on the next poll, so a restored
        # one would put an hours-old percentage on a row claiming to be live.
        assert not (backup / "codex_scan_state_quota.json").exists()
        for path in backup.iterdir():
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600, (path.name, oct(mode))
        # ...and the original is still exactly where it was.
        assert (root / "rollups.json").exists()


def test_a_second_backup_in_the_same_second_never_overwrites_the_first() -> None:
    """The first backup can be the only copy of the good state.

    Two rebuilds inside one second share a timestamp, and the SECOND one
    snapshots state the first has already cleared - overwriting in place would
    replace the good copy with the empty one.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _seed_home(root, tokens=1_000)
        first = statebackup.create_backup(root, now=1_757_500_000.0)
        _seed_home(root, tokens=9_999)
        second = statebackup.create_backup(root, now=1_757_500_000.0)
        assert first is not None and second is not None
        assert first != second, "the second backup landed on top of the first"
        assert "1000" in (first / "rollups.json").read_text(encoding="utf-8")
        assert "9999" in (second / "rollups.json").read_text(encoding="utf-8")


def test_a_backup_of_an_empty_home_is_none_not_an_empty_directory() -> None:
    """A first run has nothing to lose, and an empty backup would be a trap:
    ``Restore last backup`` would offer to restore nothing over a live store."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        assert statebackup.create_backup(root) is None
        assert statebackup.list_backups(root) == ()
        assert statebackup.latest_backup(root) is None
        assert list(root.iterdir()) == []


def test_backups_are_listed_oldest_first_and_ignore_strays() -> None:
    """Ordered by NAME, which is the timestamp the user was shown."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _seed_home(root)
        older = statebackup.create_backup(root, now=1_757_400_000.0)
        newer = statebackup.create_backup(root, now=1_757_500_000.0)
        (root / f"{statebackup.BACKUP_PREFIX}notadir").write_text("", encoding="utf-8")
        (root / "backups-elsewhere").mkdir()
        assert statebackup.list_backups(root) == (older, newer)
        assert statebackup.latest_backup(root) == newer


def test_restore_refuses_anything_that_is_not_a_backup_directory() -> None:
    """This writes over the live store; "which directory" must not be
    answerable by an accident."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _seed_home(root)
        stray = root / "some-other-dir"
        stray.mkdir()
        (stray / "rollups.json").write_text("{}", encoding="utf-8")
        for bad in (stray, root / f"{statebackup.BACKUP_PREFIX}missing"):
            try:
                statebackup.restore_backup(bad, root)
            except ValueError:
                pass
            else:  # pragma: no cover - the failure path
                raise AssertionError(f"restored from {bad}")
        assert "1000" in (root / "rollups.json").read_text(encoding="utf-8")


def test_restore_puts_the_files_back_and_leaves_the_rest_alone() -> None:
    """Every state file round-trips - read back through the store that owns it.

    ``attribution.json`` is asserted through :class:`AttributionStore` rather
    than as bytes: what has to come back is the per-project decomposition the
    menu reads, and "the file exists again" was true of a backup that copied a
    file nobody could load.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _store, day = _seed_home(root, tokens=1_000)
        backup = statebackup.create_backup(root)
        assert backup is not None
        # What a rebuild does to both stores.
        (root / "rollups.json").write_text('{"version": 1, "days": {}}', encoding="utf-8")
        emptied = AttributionStore(path=root / "attribution.json", keep_days=30)
        emptied.load()
        emptied.clear()
        emptied.save(force=True)
        (root / "settings.json").write_text('{"lookback_days": 7}', encoding="utf-8")

        restored = statebackup.restore_backup(backup, root)
        assert restored == (
            "attribution.json",
            "codex_scan_state.json",
            "rollups.json",
            "scan_state.json",
            "scan_state_dedup.json",
        ), restored
        assert "1000" in (root / "rollups.json").read_text(encoding="utf-8")

        back = AttributionStore(path=root / "attribution.json", keep_days=30)
        back.load()
        rollups = back.project_rollups()
        assert [(item[0], item[1]) for item in rollups] == [(day, "usage-bar")], rollups
        assert rollups[0][2].models["claude-fable-5"].input == 1_000, rollups
        # A file the backup does not contain is not in the pair that matters.
        assert (root / "settings.json").read_text(encoding="utf-8") == '{"lookback_days": 7}'


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


def test_the_rebuild_backs_the_state_up_before_it_clears_anything() -> None:
    """Invariant 1: the copy holds the PRE-rebuild numbers.

    A backup taken after ``clear()`` would be a perfect snapshot of nothing,
    which is how a by-hand recovery loses a month.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, day = _seed_home(root, tokens=4_242)

        class _Scanner:
            def __init__(self) -> None:
                self.reset_calls = 0

            def reset(self) -> None:
                self.reset_calls += 1

        scanner = _Scanner()
        worker = _worker(store, scanner)
        worker._all_scanners = lambda: [("claude", scanner)]
        worker._rebuild_index("backup-state-20260910-120000")

        assert scanner.reset_calls == 1
        assert store.days() == (), "the store was not cleared"
        backup = root / "backup-state-20260910-120000"
        assert backup.is_dir(), sorted(p.name for p in root.iterdir())
        assert "4242" in (backup / "rollups.json").read_text(encoding="utf-8")
        assert (backup / "scan_state.json").exists()
        # The name the confirmation dialog showed is the name on disk.
        assert worker.last_state_backup == backup, worker.last_state_backup
        assert day not in store.days()


def test_the_rebuild_snapshots_the_project_store_it_is_about_to_empty() -> None:
    """The inventory bug, as a test: ``attribution.json`` is destroyed too.

    ``_rebuild_index`` clears the attribution cache in the same breath as the
    rollup store - it is a decomposition OF those days, and leaving it standing
    would double-count it on the re-index. So it is destroyed by the button and
    must therefore be IN the copy the button takes first; without it, "Restore
    last backup" gives back the day totals and leaves the per-project store
    empty beside them, which is two answers to one question.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, day = _seed_home(root, tokens=7_000)
        attribution = AttributionStore(path=root / "attribution.json", keep_days=30)
        attribution.load()
        assert attribution.project_rollups(), "the fixture must seed a project"

        class _Scanner:
            def reset(self) -> None:
                pass

        scanner = _Scanner()
        worker = _worker(store, scanner)
        worker._all_scanners = lambda: [("claude", scanner)]
        worker._attribution = attribution
        worker._rebuild_index("backup-state-20260910-130000")

        # Destroyed on disk...
        live = AttributionStore(path=root / "attribution.json", keep_days=30)
        live.load()
        assert live.project_rollups() == (), "the rebuild did not clear the cache"
        # ...and recoverable from the snapshot the rebuild took first.
        saved = AttributionStore(
            path=root / "backup-state-20260910-130000" / "attribution.json", keep_days=30
        )
        saved.load()
        rollups = saved.project_rollups()
        assert [(item[0], item[1]) for item in rollups] == [(day, "usage-bar")], rollups
        assert rollups[0][2].models["claude-fable-5"].input == 7_000, rollups


def test_a_backup_that_cannot_be_written_aborts_the_rebuild() -> None:
    """Invariant 2: no snapshot, no rebuild.

    The snapshot is the only reason it is safe to press the button, so a home
    the widget cannot write must leave the store intact and say so - not
    proceed and destroy the thing it failed to copy.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, day = _seed_home(root, tokens=7_777)

        class _Scanner:
            def __init__(self) -> None:
                self.reset_calls = 0

            def reset(self) -> None:
                self.reset_calls += 1

        scanner = _Scanner()
        published: list[UiSnapshot] = []
        worker = _worker(store, scanner)
        worker._publish_cb = published.append
        worker._all_scanners = lambda: [("claude", scanner)]

        os.chmod(root, 0o500)  # readable, not writable: mkdir raises OSError
        try:
            worker._rebuild_index("backup-state-20260910-120000")
        finally:
            os.chmod(root, 0o700)

        assert scanner.reset_calls == 0, "the scanners were reset without a backup"
        assert day in store.days(), "the store was cleared without a backup"
        assert published and published[-1].cost_error, "the failure was silent"
        assert statebackup.list_backups(root) == ()


def test_a_restore_reloads_the_store_and_stops_the_widget_writing() -> None:
    """Invariant 3: a restore is whole or it is nothing.

    The rollup is re-read (public ``load()``), and the cost side then stops:
    no scan, no save, no offset commit, until a relaunch reads the restored
    offsets the normal way.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, day = _seed_home(root, tokens=5_500)

        class _Scanner:
            def reset(self) -> None:
                return None

        scanner = _Scanner()
        worker = _worker(store, scanner)
        worker._all_scanners = lambda: [("claude", scanner)]
        worker._rebuild_index("backup-state-20260910-120000")
        assert store.days() == ()
        assert "5500" not in (root / "rollups.json").read_text(encoding="utf-8")

        worker._restore_backup("backup-state-20260910-120000")

        # The file is back...
        assert "5500" in (root / "rollups.json").read_text(encoding="utf-8")
        # ...and so is the live store: a restore nobody can see is not a restore.
        assert day in store.days(), store.days()
        assert store.today(day).models["claude-fable-5"].input == 5_500
        assert worker.restore_note and "relaunch" in worker.restore_note.lower()
        # ...and the Cost section shows the restored money rather than
        # `indexing…`, which on a frozen widget would claim work that is not
        # happening (SPEC 4.3).
        published = worker._snapshot
        assert published.cost is not None and not published.cost.is_partial
        assert published.cost.today.usd > 0, published.cost.today

        # Frozen: the cost job does not reach the scanner, and the flush does
        # not write this process's memory over the restored file.
        worker._indexer = _ForbiddenScanner()
        worker._all_scanners = lambda: [("claude", worker._indexer)]
        before = (root / "rollups.json").read_bytes()
        worker._run_cost_job()
        store.add(day, "claude-fable-5", ModelUsage(input=999_999))
        worker._flush()
        assert (root / "rollups.json").read_bytes() == before, (
            "a frozen widget wrote its own memory over the restored file"
        )


def test_a_rebuild_after_a_restore_lifts_the_freeze() -> None:
    """The freeze is not a dead end: a rebuild is a deliberate fresh start
    that re-reads everything from offset 0, which is precisely the question
    the freeze was asking."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, _day = _seed_home(root)

        class _Scanner:
            def reset(self) -> None:
                return None

        scanner = _Scanner()
        worker = _worker(store, scanner)
        worker._all_scanners = lambda: [("claude", scanner)]
        worker._rebuild_index("backup-state-20260910-120000")
        worker._restore_backup("backup-state-20260910-120000")
        assert worker._restore_frozen == "backup-state-20260910-120000"

        worker._rebuild_index("backup-state-20260910-130000")
        assert worker._restore_frozen is None
        assert (root / "backup-state-20260910-130000").is_dir()


def test_restoring_with_no_backup_at_all_is_reported_not_ignored() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, _day = _seed_home(root)
        published: list[UiSnapshot] = []
        worker = _worker(store, _ForbiddenScanner())
        worker._publish_cb = published.append
        worker._restore_backup(None)
        assert published and published[-1].cost_error, "a no-op restore said nothing"
        assert worker._restore_frozen is None


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------


def test_the_two_destructive_items_ask_before_they_act() -> None:
    """Invariant 4. Cancel enqueues nothing; OK enqueues the exact name the
    dialog just promised, so the directory named on screen is the one on disk.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, _day = _seed_home(root)
        asked: list[tuple[str, str]] = []
        answer = [False]

        def confirm(title: str, message: str) -> bool:
            asked.append((title, message))
            return answer[0]

        app = app_mod.CCUsageWidgetApp(confirm=confirm)
        try:
            app._worker._rollups = store
            app._worker._pricing = DEFAULT_PRICING
            recorded: list[tuple[str, Any]] = []
            app._worker.submit = lambda name, payload=None: recorded.append((name, payload))

            app._on_rebuild_index(None)
            assert recorded == [], "a cancelled rebuild still ran"
            assert asked and asked[-1][0] == "Rebuild cost index?", asked[-1][0]
            # The dialog names the directory it will create, in the home the
            # store actually lives in - not the installed widget's.
            assert str(root) in asked[-1][1], asked[-1][1]
            promised = re.search(r"backup-state-\d{8}-\d{6}", asked[-1][1])
            assert promised is not None, asked[-1][1]

            answer[0] = True
            app._on_rebuild_index(None)
            assert len(recorded) == 1, recorded
            command, payload = recorded[0]
            assert command == "rebuild_index"
            assert payload and payload.startswith(statebackup.BACKUP_PREFIX), payload

            # ...and the same for the restore.
            backup = statebackup.create_backup(root, now=1_757_500_000.0)
            assert backup is not None
            app._worker._refresh_state_backups()
            answer[0] = False
            app._on_restore_backup(None)
            assert len(recorded) == 1, "a cancelled restore still ran"
            assert asked[-1][0] == "Restore last backup?", asked[-1][0]
            assert backup.name in asked[-1][1], asked[-1][1]

            answer[0] = True
            app._on_restore_backup(None)
            assert recorded[-1] == ("restore_backup", backup.name), recorded[-1]
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_restore_last_backup_appears_only_when_there_is_one() -> None:
    """An item that cannot act is not drawn - the menu's standing rule - and
    the answer comes from the worker's cache, never from a directory listing on
    the AppKit thread (SPEC 2.3)."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, _day = _seed_home(root)
        snapshot = UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS)))
        app = app_mod.CCUsageWidgetApp()
        try:
            app._worker._rollups = store
            app._worker._refresh_state_backups()
            absent = list(app._settings_submenu(snapshot).keys())
            assert app_mod._RESTORE_BACKUP_LABEL not in absent, absent

            statebackup.create_backup(root)
            app._worker._refresh_state_backups()
            present = list(app._settings_submenu(snapshot).keys())
            assert app_mod._RESTORE_BACKUP_LABEL in present, present
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_a_frozen_widget_says_so_in_the_diagnostics() -> None:
    """A widget that has stopped indexing must say so where the rest of the
    evidence is: a silent freeze reads as "nothing is happening"."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store, _day = _seed_home(root)
        snapshot = UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS)))
        app = app_mod.CCUsageWidgetApp()
        try:
            app._worker._rollups = store
            quiet = [str(item.title) for item in app._diagnostic_items(snapshot)]
            assert not any("Restored" in line for line in quiet), quiet

            class _Scanner:
                def reset(self) -> None:
                    return None

            app._worker._all_scanners = lambda: [("claude", _Scanner())]
            app._worker._rebuild_index("backup-state-20260910-120000")
            app._worker._restore_backup("backup-state-20260910-120000")
            noisy = [str(item.title) for item in app._diagnostic_items(snapshot)]
            assert any("Restored" in line and "relaunch" in line for line in noisy), noisy
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


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


def _uncollected_tests(collected: list[tuple[str, Any]]) -> list[str]:
    """Test names in the SOURCE that never reached ``globals()``.

    ``main()`` runs from the guard at the bottom and raises ``SystemExit``, so
    a test appended below it would vanish from the run with no error, only a
    smaller total.
    """
    try:
        source = Path(__file__).read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - the file is right there
        return []
    declared = re.findall(r"^def (test_\w+)", source, re.MULTILINE)
    found = {name for name, _ in collected}
    return [name for name in declared if name not in found]


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
        print(
            f"ERROR: {len(orphans)} test(s) are defined in this file but were never "
            f"collected — they sit below the `if __name__` guard: " + ", ".join(orphans)
        )
    return 1 if (failures or orphans) else 0


if __name__ == "__main__":
    raise SystemExit(main())
