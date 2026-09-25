"""The dense card theme (design 3, 2026-09-25): themes/dense.py.

Why these tests exist: the dense menu is a one-line-per-account ledger the
operator acts on without opening a submenu. So what it SAYS must stay honest
(a noted slot never shows live figures; stale readings say so; the
non-account alert lines the glance menu carried are not lost), and what it
DOES must be exact: a Claude row switches to that slot, a dead Codex login
offers ``Log in again…``, and every other row - header, section titles,
Claude notes with no action behind them - never highlights, so nothing
pretends to be a button. The pixel checks guard the one colour the port got
wrong at first: the header module must be visible in dark mode too.

Text assertions use a recording Pen (no drawing context needed); the pixel
checks render offscreen through card_base.render_png. Fixtures are SYNTHETIC
(example.com emails, made-up aliases and figures). AppKit-dependent tests skip
with a printed reason when PyObjC is missing.

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    CC_USAGE_WIDGET_NO_REVEAL=1 $PY tests/test_theme_dense.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import traceback
from pathlib import Path
from dataclasses import replace
from typing import Any

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import card_base, themes  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    normalize_settings,
)
from cc_usage_widget.themes import dense  # noqa: E402

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


def _snapshot(**over: Any) -> UiSnapshot:
    """Active slot 1, a spare (2), a stale disabled slot (4), a noted slot (3);
    an active Codex login, a live one, and a dead one (re-login alarm)."""
    values = normalize_settings(dict(SETTINGS_DEFAULTS))
    accounts = (
        _claude(1, "main", active=True, five_hour_pct=44.0, seven_day_pct=13.0,
                five_hour_resets_at="14:50", seven_day_resets_at="16:00", usage_age_seconds=30.0),
        _claude(2, "spare", five_hour_pct=5.0, seven_day_pct=51.0, seven_day_resets_at="14:00",
                usage_age_seconds=30.0),
        _claude(3, "noted", five_hour_pct=77.0, seven_day_pct=66.0, usage_age_seconds=3 * 86_400.0),
        _claude(4, "idle", five_hour_pct=0.0, seven_day_pct=0.0, disabled=True,
                usage_age_seconds=2 * 3600.0),
    )
    quota = (
        _codex(-1, "work", plan_type="pro", active=True, seven_day_pct=36.0,
               seven_day_resets_at="Oct 2 08:46", usage_age_seconds=60.0),
        _codex(-2, "side", plan_type="pro", seven_day_pct=97.0, seven_day_resets_at="Sep 30 10:08",
               usage_age_seconds=60.0),
        _codex(-3, "dead", attention_note="relogin needed", attention_kind="warn",
               usage_age_seconds=60.0),
    )
    fields: dict[str, Any] = dict(
        settings=values,
        accounts=accounts,
        active=accounts[0],
        quota_rows=quota,
        account_notes={3: RELOGIN},
        account_note_kinds={3: "re-login needed"},
        accounts_at=1.0,
        autoswitch_enabled=True,
        autoswitch_threshold=85.0,
    )
    fields.update(over)
    return UiSnapshot(**fields)


class _Recorder(card_base.Pen):
    """A Pen that records what would be drawn instead of drawing it."""

    def __init__(self) -> None:
        super().__init__(False)
        self.texts: list[tuple[str, str]] = []
        self.symbols: list[str] = []
        self.bars: list[tuple[float | None, str]] = []

    def text(self, s: str, x: float, baseline_y: float, size: float, weight: Any = "regular",
             role: Any = "label", *, mono: bool = False, right: bool = False, center: bool = False,
             maxw: float | None = None, code: bool = False, kern: float | None = None,
             rounded: bool = False) -> float:
        shown = self.fit(s, size, weight, maxw, mono=mono, code=code, kern=kern, rounded=rounded) if s else ""
        if shown:
            self.color(role)  # an unknown role must fail here, as it would on screen
            self.texts.append((shown, role))
        return self.width(shown, size, weight, mono=mono, code=code, kern=kern, rounded=rounded)

    def symbol(self, name: str, *args: Any, **kwargs: Any) -> float:
        self.symbols.append(name)
        return 10.0

    def bar(self, x: float, y: float, w: float, h: float, pct: float | None, role: Any = "ok",
            track: Any = "track") -> None:
        self.color(role), self.color(track)
        self.bars.append((pct, role))

    def rrect(self, *args: Any, **kwargs: Any) -> None:
        pass

    def panel(self, x: float, y: float, w: float, h: float, r: float, role: Any, **kwargs: Any) -> None:
        self.color(role)

    def line(self, *args: Any, **kwargs: Any) -> None:
        pass


def _paint(block: card_base.Block) -> _Recorder:
    pen = _Recorder()
    if block.paint is not None:
        block.paint(pen, card_base.W, block.height)
    return pen


def _words(block: card_base.Block) -> str:
    return " | ".join(t for t, _r in _paint(block).texts)


def _build(snap: UiSnapshot | None = None, **actions: Any) -> list[card_base.Block]:
    return themes.build_blocks("dense", snap or _snapshot(), NOW, themes.ThemeActions(**actions))


def _by_key(blocks: list[card_base.Block]) -> dict[str, card_base.Block]:
    return {b.key: b for b in blocks}


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_every_section_in_prototype_order_and_no_tail_rows() -> None:
    """Header module, Needs attention, Claude, Codex - separated like the
    prototype; the native tail (Cost … Quit) is the app's, never drawn here."""
    _need_appkit()
    keys = [b.key for b in _build()]
    content = [k for k in keys if not k.startswith("sep-")]
    assert content == [
        "claude-head", "codex-head",
        "attention", "att-claude-3", "att-codex-dead",
        "sec-claude", "claude-2", "claude-4",
        "sec-codex", "codex-side",
    ], content
    assert not keys[-1].startswith("sep-"), "the app adds the separator above the tail"
    assert len(set(keys)) == len(keys), "keys must be unique for hover renders"
    drawn = " ".join(_words(b) for b in _build())
    for title in ("Refresh", "Quit", "Auto-switch", "Switch account", "All windows"):
        assert title not in drawn, title


def test_header_module_is_one_card_over_its_vendor_blocks() -> None:
    """The faint module spans both header blocks: 6 pt padding above the first
    and below the last, 48 pt per vendor (design 5's airier 108 pt total; the
    prototype had 4 + 42 + 42 + 4). The padding belongs to the module's ends,
    never to the middle, so the two vendor lines read as one card."""
    _need_appkit()
    k = _by_key(_build())
    assert k["claude-head"].height + k["codex-head"].height == 2 * 48.0 + 2 * 6.0
    only = _by_key(_build(_snapshot(quota_rows=())))
    assert only["claude-head"].height == 48.0 + 2 * 6.0
    assert "codex-head" not in only and "sec-codex" not in only


def test_the_grid_lines_up_with_the_native_menu() -> None:
    """One column grid down the whole menu, starting on the native title x."""
    assert dense.X0 == card_base.LEAD and dense.R == card_base.RIGHT
    assert dense.PCT_R == card_base.RIGHT - 74.0 - 7.0
    assert dense.BAR_X + dense.BAR_W + 48.0 == dense.PCT_R


# ---------------------------------------------------------------------------
# honesty
# ---------------------------------------------------------------------------


def test_a_noted_slot_never_shows_live_figures() -> None:
    """Slot 3 has 77 % / 66 % on record but a dead token: it appears only in
    Needs attention, with its status, and no figure anywhere."""
    _need_appkit()
    blocks = _build()
    assert "claude-3" not in _by_key(blocks)
    att = _words(_by_key(blocks)["att-claude-3"])
    assert "noted" in att and "Re-login" in att, att
    everywhere = " ".join(_words(b) for b in blocks)
    assert "77%" not in everywhere and "66%" not in everywhere
    assert _by_key(blocks)["att-claude-3"].tooltip == RELOGIN


def test_a_noted_ACTIVE_account_withholds_its_figures_in_the_header() -> None:
    _need_appkit()
    snap = _snapshot()
    snap = _snapshot(account_notes={1: RELOGIN}, account_note_kinds={1: "re-login needed"},
                     active=snap.accounts[0])
    head = _paint(_by_key(_build(snap))["claude-head"])
    words = " | ".join(t for t, _r in head.texts)
    assert "44%" not in words and "13%" not in words, words
    assert not head.bars, "no bar for a noted account"
    # warn_text: the warn hue as TEXT (vivid, never brown on a tinted menu - design 4)
    assert any(role == "warn_text" for _t, role in head.texts), "the status is drawn in warn"


def test_stale_and_disabled_rows_say_so() -> None:
    _need_appkit()
    idle = _paint(_by_key(_build())["claude-4"])
    words = [t for t, _r in idle.texts]
    assert "disabled" in words and "2h" in words, words
    assert "clock" in idle.symbols
    assert idle.bars[0][1] == "dead_fill", "a disabled account's bar is quiet, never a severity colour"


def test_a_disabled_bar_never_looks_like_a_live_healthy_bar() -> None:
    """A disabled account at 40 % must not draw the same bar as a live one:
    its fill (``dead_fill``) is clearly fainter than ``dense_neutral`` in both
    appearances. (With the disabled fill at tertiary 0.45 the two sat 0.05
    apart and read identical.)"""
    _need_appkit()
    from AppKit import NSAppearance, NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSColorSpace

    snap = _snapshot()
    idle = replace(snap.accounts[3], five_hour_pct=40.0, seven_day_pct=40.0)
    live = replace(snap.accounts[1], five_hour_pct=40.0, seven_day_pct=40.0)
    accounts = tuple(idle if r.slot == 4 else live if r.slot == 2 else r for r in snap.accounts)
    by = _by_key(_build(replace(snap, accounts=accounts)))
    dead_role = _paint(by["claude-4"]).bars[0][1]
    live_role = _paint(by["claude-2"]).bars[0][1]
    assert (dead_role, live_role) == ("dead_fill", "dense_neutral"), (dead_role, live_role)
    for name in (NSAppearanceNameAqua, NSAppearanceNameDarkAqua):
        alphas: list[float] = []
        NSAppearance.appearanceNamed_(name).performAsCurrentDrawingAppearance_(lambda: alphas.extend(
            card_base.Pen().color(r).colorUsingColorSpace_(NSColorSpace.sRGBColorSpace()).alphaComponent()
            for r in (dead_role, live_role)))
        assert alphas[1] - alphas[0] >= 0.2, (name, alphas)


def test_attention_extras_are_shown_verbatim_in_tooltips() -> None:
    """The engine verdict / external-switch alert / switch note the glance menu
    carried must not be lost when a card theme is on."""
    _need_appkit()
    extras = ("⚠ 09:56 active 2→1 (external)", "⛔ autoswitch: every account is exhausted",
              "holding: cooldown 4m")
    blocks = _build(attention_extras=extras)
    rows = [b for b in blocks if b.key.startswith("att-extra-")]
    assert [b.tooltip for b in rows] == list(extras)
    # The section title's tooltip gathers every line, account notes first.
    assert _by_key(blocks)["attention"].tooltip.split("\n") == [RELOGIN, "relogin needed", *extras]
    assert not any(b.selectable for b in rows)
    painted = [_paint(b) for b in rows]
    assert painted[0].symbols == ["exclamationmark.triangle.fill"]
    assert painted[1].symbols == ["xmark.octagon.fill"]
    assert painted[2].symbols == ["info.circle"]
    assert painted[2].texts[0][0] == "holding: cooldown 4m"
    # Only extras, no account notes: the section still appears.
    calm = _snapshot(account_notes={}, account_note_kinds={}, quota_rows=())
    keys = [b.key for b in _build(calm, attention_extras=extras[:1])]
    assert "attention" in keys and "att-extra-0" in keys


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


def test_a_claude_row_switches_to_its_own_slot_once() -> None:
    _need_appkit()
    calls: list[int] = []
    k = _by_key(_build(switch_to=calls.append))
    k["claude-4"].action()
    k["claude-2"].action()
    assert calls == [4, 2], calls
    assert "Click to switch" in k["claude-2"].label


def test_a_dead_codex_login_offers_log_in_again() -> None:
    _need_appkit()
    started: list[str] = []

    def login_for(row: Any) -> Any:
        return (lambda: started.append(row.alias)) if row.alias == "dead" else None

    k = _by_key(_build(codex_login_for=login_for))
    att = k["att-codex-dead"]
    assert att.selectable
    att.action()
    assert started == ["dead"]
    painted = _paint(att)
    assert "Log in again" in [t for t, _r in painted.texts]
    assert "chevron.right" in painted.symbols
    assert not k["codex-side"].selectable and not k["codex-head"].selectable


def test_rows_without_an_action_never_highlight() -> None:
    """Header, section titles, separators, Claude notes (no action exists for
    them) and the healthy Codex rows are static text, and draw no chevron."""
    _need_appkit()
    k = _by_key(_build())
    for key, block in k.items():
        if key.startswith("claude-") and key.split("-", 1)[1].isdigit():
            assert block.selectable, key
        else:
            assert not block.selectable, key
    assert "chevron.right" not in _paint(k["att-claude-3"]).symbols
    assert "chevron.right" not in _paint(k["att-codex-dead"]).symbols  # inert ThemeActions()


def test_nothing_to_show_is_one_honest_line() -> None:
    _need_appkit()
    empty = _snapshot(accounts=(), active=None, quota_rows=(), account_notes={}, account_note_kinds={})
    blocks = _build(empty)
    assert [b.key for b in blocks] == ["empty"]
    assert _words(blocks[0]) == "No accounts reported"


# ---------------------------------------------------------------------------
# pixels
# ---------------------------------------------------------------------------


def _pixels(path: Path) -> Any:
    from AppKit import NSBitmapImageRep
    from Foundation import NSData

    return NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(str(path)))


def _grey(rep: Any, x_pt: float, y_pt: float) -> float:
    c = rep.colorAtX_y_(int(x_pt * 2), int(y_pt * 2))
    return (c.redComponent() + c.greenComponent() + c.blueComponent()) * 255 / 3


def _top_of(blocks: list[card_base.Block], key: str) -> float:
    y = card_base.MATERIAL_PAD
    for b in blocks:
        if b.key == key:
            return y
        y += b.height
    raise KeyError(key)


def test_the_header_module_shows_in_light_and_dark() -> None:
    """The module is labelColor at 4.5 %: darker than the material in light,
    LIGHTER in dark (a frozen light variant made it vanish in dark)."""
    _need_appkit()
    blocks = _build()
    y_sep = _top_of(blocks, "sep-1") + 2  # empty material under the module
    with tempfile.TemporaryDirectory() as tmp:
        for dark in (False, True):
            path = Path(tmp) / f"{dark}.png"
            card_base.render_png(blocks, path, dark=dark)
            rep = _pixels(path)
            inside, outside = _grey(rep, 180, card_base.MATERIAL_PAD + 2), _grey(rep, 180, y_sep)
            if dark:
                assert inside - outside >= 4, (inside, outside)
            else:
                assert outside - inside >= 4, (inside, outside)


def test_a_hovered_claude_row_draws_the_highlight_and_a_header_cannot() -> None:
    _need_appkit()
    blocks = _build()
    y_row = _top_of(blocks, "claude-2") + 12
    y_head = card_base.MATERIAL_PAD + 30
    with tempfile.TemporaryDirectory() as tmp:
        plain, row, head = Path(tmp) / "p.png", Path(tmp) / "r.png", Path(tmp) / "h.png"
        card_base.render_png(blocks, plain, dark=False)
        card_base.render_png(blocks, row, dark=False, hover_key="claude-2")
        card_base.render_png(blocks, head, dark=False, hover_key="claude-head")
        p, r, h = _pixels(plain), _pixels(row), _pixels(head)
        assert abs(_grey(p, 8, y_row) - _grey(r, 8, y_row)) > 30, "no highlight on the hovered row"
        assert abs(_grey(p, 8, y_head) - _grey(h, 8, y_head)) < 1, "a static header must not highlight"


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def test_extra_usage_spend_at_warn_shows_in_the_header() -> None:
    """Real-money extra usage at the warn threshold is top level, in its
    severity colour, as on glance's Claude card."""
    _need_appkit()
    snap = _snapshot()
    hot = replace(snap.active, spend_used=40.0, spend_limit=50.0, spend_pct=80.0)
    accounts = tuple(hot if r.slot == hot.slot else r for r in snap.accounts)
    head = _by_key(_build(replace(snap, accounts=accounts, active=hot)))["claude-head"]
    assert ("extra 80%", "warn_text") in _paint(head).texts, _paint(head).texts
    assert "extra usage 80%" in head.ax
    assert "extra" not in _words(_by_key(_build())["claude-head"])


def test_the_codex_fleet_line_setting_hides_the_section_facts() -> None:
    _need_appkit()
    on = _by_key(_build())["sec-codex"].ax
    off = _by_key(_build(replace(_snapshot(), settings={**_snapshot().settings,
                                                        "codex_fleet_line_enabled": False})))
    assert "room" in on and off["sec-codex"].ax == "Codex", (on, off["sec-codex"].ax)


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
