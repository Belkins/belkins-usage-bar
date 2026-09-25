"""Fix pass 1 (docs lane): the scoped fleet line gets a Settings switch.

``scoped_fleet_line_enabled`` shipped on 2026-09-25 with the Accounts block's
``Fable 2/5 · next …`` heading, default on, and no control: the only way to
turn it off was a hand-edit of ``settings.json``. A default-on line a user can
only reach by hand-editing is not a switch they have. These tests pin the
Settings item: drawn only when the line can exist, mirroring the setting, and
flipping exactly that key.

Run with pytest if it is available, or directly::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    $PY tests/test_fixdocs.py
"""

from __future__ import annotations

import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder or a modal

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget.app import UiSnapshot  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    AccountRow,
    normalize_settings,
)


def _row(slot: int, alias: str, scoped: tuple[tuple[str, float], ...] = ()) -> AccountRow:
    """One SYNTHETIC claude-swap row."""
    return AccountRow(
        slot=slot,
        alias=alias,
        email=f"{alias}@example.com",
        is_active=slot == 1,
        five_hour_pct=7.0,
        seven_day_pct=3.0,
        scoped_windows=scoped,
    )


def _snapshot(rows: tuple[AccountRow, ...], **overrides: Any) -> UiSnapshot:
    settings = normalize_settings({**SETTINGS_DEFAULTS, **overrides})
    return UiSnapshot(settings=settings, accounts=rows, active=rows[0] if rows else None,
                      accounts_at=1.0)


def _fleet_items(menu: Any) -> dict[str, Any]:
    return {title: menu[title] for title in menu.keys() if "fleet line" in title}


def _without_codex_registry(test: Any) -> Any:
    """Pin the Settings menu's one filesystem probe. The Codex fleet-line
    switch is drawn when the REAL codex_accounts.json exists, so on the
    author's Mac (2026-09-25) these tests saw 'Codex fleet line: ON' too and
    failed in the live checkout while passing in a clean worktree and on CI."""

    def run() -> None:
        original = app_mod._codex_registry_present
        app_mod._codex_registry_present = lambda: False
        try:
            test()
        finally:
            app_mod._codex_registry_present = original

    run.__name__ = test.__name__
    return run


@_without_codex_registry
def test_the_scoped_fleet_line_has_a_settings_switch_that_flips_only_its_key() -> None:
    fable = (_row(1, "main", (("Fable", 1.0),)), _row(3, "podol", (("Fable", 9.0),)))
    app = app_mod.CCUsageWidgetApp()
    submitted: list[Any] = []
    optimistic: list[dict[str, Any]] = []
    try:
        # Never let a click reach the worker: it would write the real settings.json.
        app._worker.submit = lambda command, payload=None: submitted.append((command, payload))
        app._optimistic = lambda **changes: optimistic.append(changes)

        on = _fleet_items(app._settings_submenu(_snapshot(fable)))
        assert list(on) == ["Fable fleet line: ON"], list(on)
        item = on["Fable fleet line: ON"]
        assert item.state == 1, "default on, and the checkmark says so"

        off_snapshot = _snapshot(fable, scoped_fleet_line_enabled=False)
        off = _fleet_items(app._settings_submenu(off_snapshot))
        assert list(off) == ["Fable fleet line: OFF"], list(off)
        assert off["Fable fleet line: OFF"].state == 0

        # The click flips exactly scoped_fleet_line_enabled, from the app's own
        # snapshot, to the opposite of what it holds.
        app._snapshot = off_snapshot
        off["Fable fleet line: OFF"].callback(None)
        assert submitted == [(app_mod._CMD_SET_SETTING, ("scoped_fleet_line_enabled", True))], (
            submitted
        )
        assert optimistic[-1]["settings"]["scoped_fleet_line_enabled"] is True
        assert optimistic[-1]["settings"]["codex_fleet_line_enabled"] is (
            off_snapshot.settings["codex_fleet_line_enabled"]
        ), "no other key moves"
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


@_without_codex_registry
def test_the_switch_is_absent_where_the_line_can_never_appear() -> None:
    """An item that cannot act is not drawn - the menu's standing rule. A
    machine whose slots report no scoped window (or no slots at all) sees the
    Settings menu it had before."""
    app = app_mod.CCUsageWidgetApp()
    try:
        plain = (_row(1, "main"), _row(2, "work"))
        assert _fleet_items(app._settings_submenu(_snapshot(plain))) == {}
        assert _fleet_items(app._settings_submenu(UiSnapshot(
            settings=normalize_settings(dict(SETTINGS_DEFAULTS))))) == {}
        # Two scoped windows: one switch (the setting governs every line),
        # named for both rather than for whichever came first.
        two = (_row(1, "main", (("Fable", 1.0),)), _row(2, "work", (("Opus", 4.0),)))
        assert list(_fleet_items(app._settings_submenu(_snapshot(two)))) == [
            "Model fleet line: ON"
        ]
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def _tests() -> list[tuple[str, Any]]:
    items = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def _uncollected_tests(collected: list[tuple[str, Any]]) -> list[str]:
    """Test names in the SOURCE that never reached ``globals()`` (a test below
    the ``__main__`` guard would vanish from the run with no error)."""
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
