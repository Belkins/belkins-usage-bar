"""Tests for ``cc_usage_widget.state`` — the REAL persistence layer.

Added 2026-08-26 after the second pre-ship gate raised its absence to
CRITICAL. Until now no test imported this module at all, yet
``__main__.py:_scan_state_hooks`` wires :class:`ScanStateStore` in as the
production ``state_loader``/``state_saver`` for both indexers, and
:class:`SettingsStore` backs ``settings.json``.

That gap mattered concretely: the double-count incident fixed the same day
turned entirely on ``save_json`` returning ``False`` rather than raising, and
the regression test guarding it passes a hand-written fake saver — so the
*actual* contract it depends on (never raises, reports failure as ``False``,
leaves the previous file intact) was asserted nowhere.

Run directly (pytest is absent from the claude-swap venv)::

    ~/.local/share/uv/tools/claude-swap/bin/python tests/test_state.py
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget import state as state_mod  # noqa: E402
from cc_usage_widget.contracts import SETTINGS_BOUNDS, SETTINGS_DEFAULTS  # noqa: E402


# ---------------------------------------------------------------------------
# ScanStateStore — the contract the double-count fix depends on
# ---------------------------------------------------------------------------


def test_save_json_round_trips_through_load_json() -> None:
    """The raw hand-off the indexer actually uses must survive a round trip."""
    with tempfile.TemporaryDirectory() as tmp:
        store = state_mod.ScanStateStore(Path(tmp) / "scan_state.json")
        payload = {
            "/a/one.jsonl": {"inode": 1, "size": 10, "mtime": time.time(), "offset": 10},
        }
        assert store.save_json(payload) is True
        assert store.load_json() == payload


def test_a_failed_write_returns_false_and_leaves_the_previous_file_intact() -> None:
    """The exact contract the 2026-08-26 double-count fix relies on.

    ``_write`` must NOT raise — the indexer keeps its dirty flag on a ``False``
    and retries next tick — and a failed write must not destroy the last good
    state, or a transient failure would become data loss instead of a retry.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scan_state.json"
        store = state_mod.ScanStateStore(path)
        good = {"/a/one.jsonl": {"inode": 1, "size": 10, "mtime": 1.0, "offset": 10}}
        assert store.save_json(good) is True
        before = path.read_bytes()

        # A payload json.dump cannot serialise: the failure path without
        # depending on filesystem permissions (which root would defeat).
        result = store.save_json({"/a/two.jsonl": {"offset": {1, 2, 3}}})
        assert result is False, "an unserialisable payload must report False, not raise"
        assert path.read_bytes() == before, "a failed write must not clobber good state"
        assert store.last_save_error, "the cause must be recorded for diagnosis"

        # No temp files are left lying around after a failure.
        leftovers = [p.name for p in Path(tmp).iterdir() if ".tmp." in p.name]
        assert not leftovers, f"failed write leaked temp files: {leftovers}"


def test_an_unwritable_directory_reports_false_rather_than_raising() -> None:
    """The disk-full / permission shape, which is what actually happens live."""
    if os.geteuid() == 0:  # pragma: no cover - root defeats mode bits
        print("  (skipped: running as root)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        locked = Path(tmp) / "locked"
        locked.mkdir()
        store = state_mod.ScanStateStore(locked / "scan_state.json")
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x: cannot create files
        try:
            assert store.save_json({"/a.jsonl": {"offset": 1}}) is False
            assert store.last_save_error
        finally:
            locked.chmod(stat.S_IRWXU)


def test_a_corrupt_or_missing_scan_state_loads_as_empty_not_an_exception() -> None:
    """A missing file is a normal cold start; corrupt bytes must not crash."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "scan_state.json"
        assert state_mod.ScanStateStore(path).load_json() in (None, {}, [])
        path.write_text("{not json", encoding="utf-8")
        assert state_mod.ScanStateStore(path).load_json() in (None, {}, [])


def test_save_json_prunes_entries_outside_the_lookback_window() -> None:
    """The only rule this hand-off applies — old transcripts must fall out."""
    with tempfile.TemporaryDirectory() as tmp:
        store = state_mod.ScanStateStore(Path(tmp) / "scan_state.json")
        now = time.time()
        payload = {
            "/a/fresh.jsonl": {"inode": 1, "size": 1, "mtime": now, "offset": 1},
            "/a/ancient.jsonl": {
                "inode": 2,
                "size": 1,
                "mtime": now - 400 * 86400,
                "offset": 1,
            },
        }
        assert store.save_json(payload, lookback_days=30, now=now) is True
        kept = store.load_json() or {}
        assert "/a/fresh.jsonl" in kept
        assert "/a/ancient.jsonl" not in kept, "a 400-day-old entry must be pruned"


# ---------------------------------------------------------------------------
# SettingsStore
# ---------------------------------------------------------------------------


def test_first_run_seeds_the_file_with_defaults() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        loaded = state_mod.SettingsStore(path).load()
        assert path.exists(), "first run must seed a hand-editable file"
        for key, value in SETTINGS_DEFAULTS.items():
            assert loaded[key] == value


def test_a_corrupt_settings_file_is_not_overwritten_on_load() -> None:
    """Documented behaviour: fall back in memory, leave the bad bytes to fix."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text("{ this is not json", encoding="utf-8")
        loaded = state_mod.SettingsStore(path).load()
        assert loaded["lookback_days"] == SETTINGS_DEFAULTS["lookback_days"]
        assert path.read_text(encoding="utf-8") == "{ this is not json"


def test_unknown_keys_survive_a_write() -> None:
    """An older build must not strip a newer build's settings."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(
            json.dumps({**SETTINGS_DEFAULTS, "a_future_key": "keep me"}),
            encoding="utf-8",
        )
        store = state_mod.SettingsStore(path)
        store.load()
        store.save(force=True)
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk.get("a_future_key") == "keep me"


def test_out_of_range_and_wrongly_typed_values_cannot_brick_the_widget() -> None:
    """A hand-edit must be clamped/dropped, never honoured into a busy loop."""
    key = "ui_interval_seconds"
    if key not in SETTINGS_BOUNDS:  # pragma: no cover - contract changed
        print(f"  (skipped: {key} has no declared bounds)")
        return
    low, high = SETTINGS_BOUNDS[key]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        path.write_text(
            json.dumps({**SETTINGS_DEFAULTS, key: 0, "lookback_days": "thirty"}),
            encoding="utf-8",
        )
        loaded = state_mod.SettingsStore(path).load()
        assert low <= loaded[key] <= high, f"{key} must be clamped into {low}..{high}"
        assert loaded["lookback_days"] == SETTINGS_DEFAULTS["lookback_days"], (
            "a wrongly-typed value must fall back to its default"
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


# ---------------------------------------------------------------------------
# Roadmap item 2 - the self-audit's off switch has to survive the round trip
# ---------------------------------------------------------------------------


def test_self_audit_setting_defaults_on_and_survives_normalisation() -> None:
    """Every feature needs an off switch, and an undeclared key is DROPPED.

    ``normalize_settings`` keeps only what ``SETTINGS_DEFAULTS`` declares, so a
    toggle added to the menu but not to the defaults would silently revert on
    every save - the switch would appear to work and then not.
    """
    from cc_usage_widget.contracts import normalize_settings

    assert SETTINGS_DEFAULTS["self_audit_enabled"] is True
    assert normalize_settings({})["self_audit_enabled"] is True
    off = normalize_settings({**SETTINGS_DEFAULTS, "self_audit_enabled": False})
    assert off["self_audit_enabled"] is False
    # And it round-trips through the real settings file, not a dict.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "settings.json"
        store = state_mod.SettingsStore(path)
        store.load()
        assert store.get("self_audit_enabled") is True
        assert store.set("self_audit_enabled", False) is True
        assert state_mod.SettingsStore(path).load()["self_audit_enabled"] is False


def test_the_scan_state_ledger_round_trips_through_the_real_store() -> None:
    """The ledger rides in the same file the double-count fix guards.

    A ledger that did not survive ``ScanStateStore`` would leave every entry
    unable to retract, which is the add-only behaviour it exists to end - and
    it would fail silently, because a missing ledger is a legal legacy entry.
    """
    from cc_usage_widget.contracts import (
        FileScanState,
        LedgerEntry,
        scan_state_from_json,
        scan_state_to_json,
    )

    with tempfile.TemporaryDirectory() as tmp:
        store = state_mod.ScanStateStore(Path(tmp) / "scan_state.json")
        entry = FileScanState(
            inode=7,
            size=100,
            mtime=1_780_000_000.0,
            offset=100,
            last_model="codex:gpt-5.6-sol",
            ledger=(
                LedgerEntry(
                    day="2026-09-09",
                    model="codex:gpt-5.6-sol",
                    counters=(1, 2, 3, 4, 5),
                ),
            ),
        )
        assert store.save_json(scan_state_to_json({"/a/r.jsonl": entry})) is True
        back = scan_state_from_json(store.load_json())
        assert back["/a/r.jsonl"] == entry, back

        # A legacy entry - the four SPEC 3.2 keys and nothing else - still
        # loads, keeps its offset, and simply has nothing to retract.
        legacy = {"/a/old.jsonl": {"inode": 1, "size": 2, "mtime": 3.0, "offset": 2}}
        assert store.save_json(legacy) is True
        loaded = scan_state_from_json(store.load_json())["/a/old.jsonl"]
        assert loaded.offset == 2 and loaded.ledger == ()
        assert loaded.to_json() == legacy["/a/old.jsonl"], loaded.to_json()


if __name__ == "__main__":
    raise SystemExit(main())
