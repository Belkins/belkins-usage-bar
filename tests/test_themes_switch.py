"""Menu themes (design 3, 2026-09-25): the ``menu_theme`` setting, its
migration, Settings ▸ Theme, the dispatch to card themes + the common native
tail, and the fall-back to glance.

Why: the operator asked for all three card designs live with a switcher in
Settings. The switch must apply on click, persist through the worker, never
strand an operator who had chosen the classic rollback, and a card theme that
fails to draw must cost the cards - never the menu. The glance and classic
text layouts stay byte-for-byte (their own tests pin them; here: the themed
path does not leak into them).

Fixtures are SYNTHETIC (example.com emails, made-up aliases and figures).

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    CC_USAGE_WIDGET_NO_REVEAL=1 $PY tests/test_themes_switch.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import card_base, themes  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    ALERT_EXTERNAL_SWITCH,
    SETTINGS_CHOICES,
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    normalize_settings,
)
from cc_usage_widget.state import SettingsStore  # noqa: E402

UiSnapshot = app_mod.UiSnapshot
NOW = dt.datetime(2026, 9, 25, 10, 52).timestamp()
SKIP = "" if card_base.available() else "AppKit (PyObjC) is not importable"
RELOGIN = "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add"


class Skip(Exception):
    pass


def _need_appkit() -> None:
    if SKIP:
        raise Skip(SKIP)


def _claude(slot: int, alias: str, **fields: Any) -> AccountRow:
    return AccountRow(slot=slot, alias=alias, email=f"{alias}@example.com",
                      is_active=fields.pop("active", False), **fields)


def _codex(slot: int, alias: str, **fields: Any) -> AccountRow:
    return AccountRow(slot=slot, alias=alias, email="", is_active=fields.pop("active", False),
                      vendor=VENDOR_CODEX, switchable=False, **fields)


def _snapshot(theme: str | None = "apple", **settings: Any) -> UiSnapshot:
    """Two Claude slots (one noted), two Codex rows, one external-switch alert."""
    values = normalize_settings(dict(SETTINGS_DEFAULTS))
    values.update({"title_show_icon": False, "title_show_cost": False})
    if theme is not None:
        values["menu_theme"] = theme
    values.update(settings)
    accounts = (
        _claude(1, "main", active=True, five_hour_pct=44.0, seven_day_pct=13.0,
                five_hour_resets_at="14:50", seven_day_resets_at="16:00"),
        _claude(2, "spare", five_hour_pct=5.0, seven_day_pct=51.0, seven_day_resets_at="14:00"),
        _claude(3, "noted", five_hour_pct=0.0, seven_day_pct=0.0, usage_age_seconds=3 * 86_400.0),
    )
    quota = (
        _codex(-1, "work", plan_type="pro", active=True, seven_day_pct=36.0,
               seven_day_resets_at="Oct 2 08:46", usage_age_seconds=60.0, stale_after_seconds=21_600.0),
        _codex(-2, "side", plan_type="pro", seven_day_pct=97.0, seven_day_resets_at="Sep 30 10:08",
               usage_age_seconds=60.0, stale_after_seconds=21_600.0),
    )
    return UiSnapshot(
        settings=values,
        accounts=accounts,
        active=accounts[0],
        quota_rows=quota,
        account_notes={3: RELOGIN},
        account_note_kinds={3: "re-login needed"},
        alert=(ALERT_EXTERNAL_SWITCH, "09:56 active 2→1 (external)"),
        recent_events=("08:10 autoswitch → 2", "09:40 active 1→2 (external)"),
        accounts_at=1.0,
        autoswitch_enabled=True,
        autoswitch_threshold=85.0,
    )


class _App:
    """A real app whose worker commands are recorded instead of run."""

    def __enter__(self) -> Any:
        self.app = app_mod.CCUsageWidgetApp()
        self.app._now = lambda: NOW
        self.sent: list[tuple[str, Any]] = []
        self.app._worker.submit = lambda name, payload=None: self.sent.append((name, payload))
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.app._running = False
        self.app._worker.stop(timeout=2.0)


def _top(app: Any) -> list[Any]:
    """Top-level items with rumps' separators as ``None``."""
    from rumps.rumps import SeparatorMenuItem

    return [None if isinstance(item, SeparatorMenuItem) else item for item in app.menu.values()]


def _title(item: Any) -> str:
    return "" if item is None else str(getattr(item, "title", ""))


def _view(item: Any) -> Any:
    native = getattr(item, "_menuitem", None)
    return native.view() if native is not None else None


# ---------------------------------------------------------------------------
# the setting
# ---------------------------------------------------------------------------


def test_menu_theme_is_a_declared_enum_defaulting_to_cards() -> None:
    """Undeclared keys are dropped on save, and a string setting is an enum:
    junk falls back to the default instead of reaching a label."""
    # Cards is the default since design 4 (2026-09-25, the operator asked for it).
    assert SETTINGS_DEFAULTS["menu_theme"] == "cards"
    assert SETTINGS_CHOICES["menu_theme"] == ("apple", "dense", "cards", "glance", "classic")
    assert themes.MENU_THEMES == SETTINGS_CHOICES["menu_theme"], "one list, two owners would drift"
    for theme in SETTINGS_CHOICES["menu_theme"]:
        assert normalize_settings({"menu_theme": theme})["menu_theme"] == theme
    assert normalize_settings({"menu_theme": "neon"})["menu_theme"] == "cards"
    assert normalize_settings({"menu_theme": 3})["menu_theme"] == "cards"


def test_a_classic_operator_keeps_the_classic_menu_after_the_upgrade() -> None:
    """A settings.json from before design 3 with the classic rollback on must
    not wake up in a card theme; one without it gets the new default."""
    assert normalize_settings({"menu_layout_classic": True})["menu_theme"] == "classic"
    assert normalize_settings({"menu_layout_classic": False})["menu_theme"] == "cards"
    assert normalize_settings({})["menu_theme"] == "cards", "a fresh install opens on the Cards theme"
    # An explicit theme is never rewritten by the migration.
    assert normalize_settings({"menu_layout_classic": True, "menu_theme": "dense"})["menu_theme"] == "dense"
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "settings.json"
        path.write_text(json.dumps({"menu_layout_classic": True, "lookback_days": 30}))
        store = SettingsStore(path)
        assert store.load(seed_missing=False)["menu_theme"] == "classic"
        assert store.set("menu_theme", "cards") and store.get("menu_theme") == "cards"
        assert json.loads(path.read_text())["menu_theme"] == "cards", "persisted"
        store.set("menu_theme", "not-a-theme")
        assert store.get("menu_theme") == "cards", "junk coerces to the default, never to a label"


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_layout_follows_the_theme_and_the_classic_switch_still_wins() -> None:
    _need_appkit()
    with _App() as h:
        app = h.app
        for theme in themes.THEMES:
            assert app._menu_layout(_snapshot(theme)) == theme
        assert app._menu_layout(_snapshot(None)) == "cards", "the default is the Cards theme"
        assert app._menu_layout(_snapshot("glance")) == "glance"
        assert app._menu_layout(_snapshot("classic")) == "classic"
        assert app._menu_layout(_snapshot("dense", menu_layout_classic=True)) == "classic"
        empty = replace(_snapshot("apple"), accounts=(), active=None, quota_rows=(),
                        account_notes={}, account_note_kinds={})
        assert app._menu_layout(empty) == "classic", "nothing to summarise: the never-onboarded menu"


def test_no_theme_draws_information_in_the_faint_greys() -> None:
    """Design 4: our views are not vibrant, so over a wallpaper-tinted menu
    anything drawn in ``tertiary`` / ``quaternary`` is barely there. Those
    roles are for decoration (the ' · ' joiners) only: every word, figure,
    age, plan and window label of every card theme uses label or secondary
    (or a severity hue)."""
    _need_appkit()
    from AppKit import NSImage

    for theme in themes.THEMES:
        faint: list[str] = []

        class Rec(card_base.Pen):
            def text(self, s: str, x: float, y: float, size: float, weight: Any = "regular",
                     role: Any = "label", **k: Any) -> float:
                if s and role in ("tertiary", "quaternary") and s.strip(" ·"):
                    faint.append(s)
                return super().text(s, x, y, size, weight, role, **k)

        actions = themes.ThemeActions(attention_extras=("⚠ engine: every account is near its limit",))
        for block in themes.build_blocks(theme, _snapshot(theme), NOW, actions):
            if block.paint is None:
                continue
            img = NSImage.alloc().initWithSize_((card_base.W, max(1.0, block.height)))
            img.lockFocus()
            try:
                block.paint(Rec(False), card_base.W, block.height)
            finally:
                img.unlockFocus()
        assert not faint, (theme, faint)


def test_a_card_theme_renders_views_then_the_common_native_tail() -> None:
    _need_appkit()
    with _App() as h:
        app = h.app
        for theme in themes.THEMES:
            snap = _snapshot(theme)
            app.rebuild_menu(snap)
            items = _top(app)
            first_native = next(i for i, item in enumerate(items) if item is None)
            views = [_view(item) for item in items[:first_native]]
            assert views and all(isinstance(v, card_base.BlockView) for v in views), (theme, views)
            assert all(v.frame().size.width == card_base.W for v in views)
            tail = items[first_native + 1:]
            expected = [row and row["title"] for row in themes.tail_rows(snap)]
            got = [None if item is None else _title(item) for item in tail]
            assert len(got) == len(expected), (got, expected)
            for want, have in zip(expected, got):
                assert (want is None and have is None) or (have or "").startswith(want), (want, have)
            assert not any(_view(item) for item in tail if item is not None), "the tail stays native"


def test_the_tail_carries_everything_the_glance_menu_did() -> None:
    """Nothing lost: Cost, both on/off switches TOP LEVEL with their state in
    the text (SPEC 4.2), the engine's one-click Switch to best now, recent
    switches with the newest time, every Claude and Codex bar, Settings."""
    _need_appkit()
    with _App() as h:
        app = h.app
        snap = _snapshot("apple")
        app.rebuild_menu(snap)
        menu = app.menu
        assert f"Cost ({app_mod.NOTIONAL_LABEL})" in menu
        cost_switch = next(item for item in _top(app) if _title(item).startswith("Cost tracking"))
        assert cost_switch.state == 1 and _title(cost_switch) == "Cost tracking  on", _title(cost_switch)
        assert cost_switch.callback == app._on_toggle_cost_tracking
        best = next(item for item in _top(app) if _title(item) == "Switch to best now")
        assert best.callback is not None, "spare is a target: the item is live"
        best.callback(best)
        assert ("switch_best", None) in h.sent, h.sent
        recent = next(item for item in _top(app) if _title(item).startswith("Recent switches"))
        assert _title(recent) == "Recent switches  last 09:40", _title(recent)
        assert any("09:40 active 1→2" in _title(i) for i in recent.values()), "the lines are one level down"
        auto = next(item for item in _top(app) if _title(item).startswith("Auto-switch"))
        assert auto.state == 1 and _title(auto) == "Auto-switch  at 85%", (_title(auto), auto.state)
        every = [_title(i) for i in menu["All windows & resets"].values() if i is not None]
        joined = "\n".join(every)
        for alias in ("main", "spare", "noted", "work", "side"):
            assert alias in joined, (alias, every)
        assert "Theme" in [_title(i) for i in menu["Settings"].values() if i is not None]
        switch = [_title(i) for i in menu["Switch account"].values() if i is not None]
        assert any("spare" in t for t in switch), switch


def test_switch_state_is_in_the_text_and_best_dims_with_nowhere_to_go() -> None:
    """Off reads "off", not a stale "at 85%"; with no other account the best
    switch is dim (no callback), as the glance item is."""
    _need_appkit()
    with _App() as h:
        app = h.app
        snap = replace(_snapshot("cards", cost_tracking_enabled=False), autoswitch_enabled=False,
                       accounts=_snapshot().accounts[:1], account_notes={}, account_note_kinds={})
        app.rebuild_menu(snap)
        titles = {_title(i): i for i in _top(app) if i is not None}
        assert "Auto-switch  off" in titles and titles["Auto-switch  off"].state == 0, list(titles)
        assert "Cost tracking  off" in titles and titles["Cost tracking  off"].state == 0, list(titles)
        assert titles["Switch to best now"].callback is None, "clickable with nowhere to go"


def test_problems_lead_the_tail_when_present() -> None:
    _need_appkit()
    with _App() as h:
        app = h.app
        snap = replace(_snapshot("dense"), accounts_error="synthetic accounts failure")
        app.rebuild_menu(snap)
        titles = [_title(i) for i in _top(app)]
        assert "! accounts: synthetic accounts failure" in titles, titles


def test_glance_and_classic_do_not_change_under_the_theme_dispatch() -> None:
    """The text layouts are their builders' output, item for item."""
    _need_appkit()
    with _App() as h:
        app = h.app
        for theme, builder in (("glance", app._rebuild_glance), ("classic", app._rebuild_classic)):
            snap = _snapshot(theme)
            want = [_title(i) for i in app_mod._dedupe_titles(builder(snap))]
            app.rebuild_menu(snap)
            got = [_title(i) for i in _top(app)]
            assert "" in want and got.count("") == want.count(""), "separators compared too"
            assert got == want, (theme, got, want)
            assert not any(isinstance(_view(i), card_base.BlockView) for i in _top(app) if i is not None)


def test_a_failing_theme_falls_back_to_glance_and_logs_once() -> None:
    _need_appkit()
    original = themes.build_blocks

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("synthetic theme failure")

    with _App() as h:
        app = h.app
        glance = [_title(i) for i in app_mod._dedupe_titles(app._rebuild_glance(_snapshot("cards")))]
        err = io.StringIO()
        themes.build_blocks = broken
        try:
            with contextlib.redirect_stderr(err):
                app.rebuild_menu(_snapshot("cards"))
                first = [_title(i) for i in _top(app)]
                app.rebuild_menu(_snapshot("cards"))
        finally:
            themes.build_blocks = original
        assert first == glance, "the menu survives as glance"
        assert err.getvalue().count("menu theme 'cards' failed") == 1, err.getvalue()
        app.rebuild_menu(_snapshot("cards"))
        assert isinstance(_view(_top(app)[0]), card_base.BlockView), "recovers on the next rebuild"


def test_a_view_failure_also_falls_back() -> None:
    _need_appkit()
    original = card_base.menu_items_for

    def broken(_blocks: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("synthetic view failure")

    with _App() as h:
        card_base.menu_items_for = broken
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                h.app.rebuild_menu(_snapshot("apple"))
        finally:
            card_base.menu_items_for = original
        assert not any(isinstance(_view(i), card_base.BlockView) for i in _top(h.app) if i is not None)
        assert any(_title(i) == "Settings" for i in _top(h.app))


# ---------------------------------------------------------------------------
# Settings ▸ Theme
# ---------------------------------------------------------------------------


def test_theme_menu_checks_the_current_choice_and_applies_on_click() -> None:
    _need_appkit()
    with _App() as h:
        app = h.app
        app._snapshot = _snapshot("apple")
        app.rebuild_menu(app._snapshot)
        menu = app.menu["Settings"]["Theme"]
        states = {_title(i): i.state for i in menu.values() if i is not None}
        assert states == {"Apple": 1, "Dense": 0, "Cards": 0, "Glance (text)": 0, "Classic (text)": 0}, states
        menu["Dense"].callback(menu["Dense"])
        assert app.snapshot().settings["menu_theme"] == "dense", "optimistic: applied before the worker"
        assert app._menu_layout(app.snapshot()) == "dense"
        assert isinstance(_view(_top(app)[0]), card_base.BlockView), "the menu was rebuilt"
        assert h.sent == [("set_setting", ("menu_theme", "dense")),
                          ("set_setting", ("menu_layout_classic", False))], h.sent
        checked = {_title(i): i.state for i in app.menu["Settings"]["Theme"].values() if i is not None}
        assert checked["Dense"] == 1 and checked["Apple"] == 0, checked


def test_choosing_classic_or_leaving_it_keeps_both_keys_in_step() -> None:
    """The older switch means classic on its own, so leaving classic must
    clear it too - otherwise the new choice would be silently overridden."""
    _need_appkit()
    with _App() as h:
        app = h.app
        app._snapshot = _snapshot("apple")
        app.rebuild_menu(app._snapshot)
        item = app.menu["Settings"]["Theme"]["Classic (text)"]
        item.callback(item)
        assert app.snapshot().settings["menu_layout_classic"] is True
        assert app._menu_layout(app.snapshot()) == "classic"
        item = app.menu["Settings"]["Theme"]["Cards"]
        item.callback(item)
        settings = app.snapshot().settings
        assert settings["menu_theme"] == "cards" and settings["menu_layout_classic"] is False, settings
        assert app._menu_layout(app.snapshot()) == "cards"
        assert h.sent[-2:] == [("set_setting", ("menu_theme", "cards")),
                               ("set_setting", ("menu_layout_classic", False))], h.sent


def test_the_worker_persists_a_theme_choice() -> None:
    """The worker's set_setting path merges and persists the key (it is
    declared, so normalize_settings keeps it)."""
    worker = app_mod.BackgroundWorker.__new__(app_mod.BackgroundWorker)
    saved: list[dict[str, Any]] = []
    worker._snapshot = _snapshot("apple")
    worker._persist_settings = lambda values: saved.append(dict(values)) or True
    merged = app_mod.BackgroundWorker._merge_settings(worker, "menu_theme", "cards")
    assert merged["menu_theme"] == "cards" and saved and saved[-1]["menu_theme"] == "cards", saved


# ---------------------------------------------------------------------------
# ThemeActions and the stub header
# ---------------------------------------------------------------------------


def test_theme_actions_enqueue_and_never_act_on_the_ui_thread() -> None:
    _need_appkit()
    with _App() as h:
        app = h.app
        snap = _snapshot("apple")
        actions = app._theme_actions(snap)
        actions.switch_to(2)
        actions.switch_best()
        actions.refresh()
        assert h.sent == [("switch_to", "spare"), ("switch_best", None), ("refresh", None)], h.sent
        assert actions.codex_login_for(snap.quota_rows[1]) is None, "a live Codex row is not a login row"
        assert actions.codex_login(snap.quota_rows[1]) is False
        assert actions.open_url("file:///etc/hosts") is False, "only http(s)"
        assert actions.open_url("https://example.com") is False, "suppressed under NO_REVEAL in tests"
        glance_extra = [_title(i) for i in app._alert_items(snap)[len(snap.account_notes):]]
        assert list(actions.attention_extras) == glance_extra and glance_extra, glance_extra


# The block that describes the active Claude account, per theme. The lanes
# chose their own keys; the honesty rule below holds for every one of them.
ACTIVE_CLAUDE_KEY = {"apple": "claude-head", "dense": "claude-head", "cards": "cards-claude-active"}


def test_every_theme_header_is_honest() -> None:
    """Every card theme hides a noted active account's figures, names the note
    and keeps the full remedy reachable, and says how old a stale reading is."""
    _need_appkit()
    actions = themes.ThemeActions()
    snap = _snapshot("apple")
    assert set(ACTIVE_CLAUDE_KEY) == set(themes.THEMES), "a new theme needs its active-account key here"
    noted_active = replace(snap, active=snap.accounts[2],
                           accounts=(replace(snap.accounts[0], is_active=False), snap.accounts[1],
                                     replace(snap.accounts[2], is_active=True)))
    stale = replace(snap.accounts[0], usage_age_seconds=3_600.0, stale_after_seconds=600.0)
    stale_snap = replace(snap, active=stale, accounts=(stale,) + snap.accounts[1:])
    for theme, key in ACTIVE_CLAUDE_KEY.items():
        blocks = themes.build_blocks(theme, noted_active, NOW, actions)
        head = next(b for b in blocks if b.key == key)
        assert "%" not in head.ax, (theme, head.ax)
        assert "figures withheld" in head.ax or "re-login needed" in head.ax, (theme, head.ax)
        assert any(RELOGIN in (b.tooltip or "") for b in blocks), (theme, "remedy not reachable")
        head = next(b for b in themes.build_blocks(theme, stale_snap, NOW, actions) if b.key == key)
        assert "1h old" in head.ax, (theme, head.ax)
    try:
        themes.build_blocks("glance", snap, NOW, actions)
    except ValueError:
        pass
    else:
        raise AssertionError("glance is a text layout, not a block theme")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def test_an_external_switch_alert_stays_on_card_themes_when_it_is_the_newest_event() -> None:
    """Glance drops the alert while it IS the newest journal line, because its
    inline Recent-switches line already says it. No card theme draws that
    line, so the alert must stay in the themes' attention section."""
    _need_appkit()
    line = "09:56 active 2→1 (external)"
    with _App() as h:
        app = h.app
        for theme in themes.THEMES:
            snap = replace(_snapshot(theme), recent_events=("08:10 autoswitch → 2", line))
            actions = app._theme_actions(snap)
            assert f"⚠ {line}" in actions.attention_extras, (theme, actions.attention_extras)
            blocks = themes.build_blocks(theme, snap, NOW, actions)
            assert any(line in (b.label or "") or line in (b.tooltip or "") for b in blocks), theme
        glance = [_title(i) for i in app._alert_items(snap)]
        assert f"⚠ {line}" not in glance, "glance keeps its inline-line dedupe"


def test_the_fleet_line_settings_reach_every_card_theme() -> None:
    """Settings ▸ Codex fleet line OFF hides the room / next / resets facts
    on every theme, as it does on glance - a switch that does nothing on the
    default theme is a broken switch."""
    _need_appkit()
    actions = themes.ThemeActions()
    for theme in themes.THEMES:
        def room(**settings: Any) -> list[str]:
            blocks = themes.build_blocks(theme, _snapshot(theme, **settings), NOW, actions)
            return [b.key for b in blocks if "room" in (b.label or "") and "Codex" in (b.label or "")]
        assert room(), (theme, "the fleet line is on by default")
        assert not room(codex_fleet_line_enabled=False), (theme, room(codex_fleet_line_enabled=False))


def test_the_menu_stays_as_wide_as_the_cards() -> None:
    """AppKit sizes a menu to its widest item TITLE, even a view item's hidden
    one: long hidden labels and an uncapped problem line made the menu 423 pt
    (up to ~920 pt) while the cards stay 360 pt, leaving an empty strip."""
    _need_appkit()
    with _App() as h:
        app = h.app
        for theme in themes.THEMES:
            snap = replace(_snapshot(theme), accounts_error="W" * 300)
            app.rebuild_menu(snap)
            width = app.menu._menu.size().width
            assert width == card_base.W, (theme, width)
            problem = next(i for i in _top(app) if _title(i).startswith("! accounts"))
            assert problem._menuitem.toolTip() == "! accounts: " + "W" * 300, "full text in the tooltip"


def test_a_rebuild_releases_the_menu_before_last() -> None:
    """rumps never forgets a MenuItem; each rebuild kept the whole previous
    menu (card views, their closures, the snapshot) alive for the process's
    life. The previous generation is kept one rebuild (an open submenu still
    works), the one before is released, and a late click on it is a no-op."""
    _need_appkit()
    from rumps.rumps import NSApp

    with _App() as h:
        app = h.app
        snap = _snapshot("apple")
        app.rebuild_menu(snap)
        first = list(app.menu.values())
        views = [i._menuitem.view() for i in first if i is not None and i._menuitem.view() is not None]
        assert views
        app.rebuild_menu(snap)
        registry = NSApp._ns_to_py_and_callback
        size = len(registry)
        for _ in range(20):
            app.rebuild_menu(snap)
        assert len(registry) == size, (size, len(registry))
        assert all(v.block is None for v in views), "the old views let go of their blocks"
        stale = next(i for i in first if i is not None and _title(i) == "Refresh")
        assert registry[stale._menuitem][1](None) is None, "a late click is a no-op"
        live = next(i for i in _top(app) if _title(i) == "Refresh")
        assert live.callback is not None


def _tests() -> list[tuple[str, Any]]:
    items = [(n, o) for n, o in globals().items() if n.startswith("test_") and callable(o)]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def main() -> int:
    tests = _tests()
    failures: list[str] = []
    skipped = 0
    for name, func in tests:
        try:
            func()
        except Skip as why:
            skipped += 1
            print(f"skip  {name}: {why}")
        except Exception:
            failures.append(name)
            print(f"FAIL  {name}")
            print(traceback.format_exc().rstrip())
        else:
            print(f"pass  {name}")
    total = len(tests)
    passed = total - len(failures) - skipped
    print(f"\n{passed} passed, {len(failures)} failed, {skipped} skipped, out of {total}")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
