"""The "cards" menu theme (design 3, 2026-09-25): provider cards on card_base.

Why these tests exist: the cards theme is a menu the operator acts from, not a
picture. A row that highlights must do something (switch the Claude login, or
start a Codex re-login) and a row that does nothing must not highlight - a
dead click on a card is worse than no card. The honesty rules the glance menu
earned carry over: a noted account never shows a live figure, an ended window
never binds the big number, a stale reading says it is stale, and the Needs
attention lines the app passes in (engine verdict, external switch) are drawn
verbatim. "Add credits" is upstream's wording, never a link we invented.
Last, a card is spread over several menu items; the slices must paint ONE
card, so every slice is sampled for the card fill.

Fixtures are SYNTHETIC (example.com emails, made-up aliases and figures).

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    CC_USAGE_WIDGET_NO_REVEAL=1 $PY tests/test_theme_cards.py
"""

from __future__ import annotations

import datetime as dt
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
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    normalize_settings,
)

UiSnapshot = app_mod.UiSnapshot
NOW = dt.datetime(2026, 9, 25, 10, 52).timestamp()
SKIP = "" if card_base.available() else "AppKit (PyObjC) is not importable"
RELOGIN = "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add"
EXTRA = "⚠ engine: every account is near its limit"


class Skip(Exception):
    pass


def _need_appkit() -> None:
    if SKIP:
        raise Skip(SKIP)


def _claude(slot: int, alias: str, **fields: Any) -> AccountRow:
    return AccountRow(slot=slot, alias=alias, email=f"{alias}@example.com",
                      is_active=fields.pop("active", False), **fields)


def _codex(slot: int, alias: str, **fields: Any) -> AccountRow:
    return AccountRow(slot=slot, alias=alias, email=f"{alias}@example.com",
                      is_active=fields.pop("active", False), vendor=VENDOR_CODEX, switchable=False,
                      usage_age_seconds=fields.pop("usage_age_seconds", 60.0),
                      stale_after_seconds=21_600.0, **fields)


def _snapshot(**over: Any) -> UiSnapshot:
    """Three Claude slots (1 active, 2 spare, 3 noted) and three Codex rows:
    ``work`` active, ``side`` out of credits (figures kept), ``dead`` alarmed."""
    values = normalize_settings(dict(SETTINGS_DEFAULTS))
    values["menu_theme"] = "cards"
    accounts = (
        _claude(1, "main", active=True, five_hour_pct=44.0, seven_day_pct=13.0,
                five_hour_resets_at="14:50", seven_day_resets_at="16:00", usage_age_seconds=30.0),
        _claude(2, "spare", five_hour_pct=5.0, seven_day_pct=51.0, seven_day_resets_at="14:00",
                usage_age_seconds=30.0),
        _claude(3, "noted", five_hour_pct=77.0, seven_day_pct=66.0, usage_age_seconds=3 * 86_400.0),
    )
    quota = (
        _codex(-1, "work", plan_type="pro", active=True, seven_day_pct=36.0,
               seven_day_resets_at="Oct 2 08:46"),
        _codex(-2, "side", plan_type="pro", seven_day_pct=100.0, seven_day_resets_at="Sep 30 10:08",
               attention_note="out of credits · Add credits", attention_kind="crit"),
        _codex(-3, "dead", plan_type="pro", attention_note="re-login needed", attention_kind="warn"),
    )
    fields: dict[str, Any] = dict(
        settings=values, accounts=accounts, active=accounts[0], quota_rows=quota,
        account_notes={3: RELOGIN}, account_note_kinds={3: "re-login needed"},
        recent_events=("08:10 autoswitch → 2",), accounts_at=1.0,
        autoswitch_enabled=True, autoswitch_threshold=85.0,
    )
    fields.update(over)
    return UiSnapshot(**fields)


class _Recorder:
    """ThemeActions whose callables record what a click would enqueue."""

    def __init__(self, relogin_slots: tuple[int, ...] = ()) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.relogin_slots = relogin_slots

    def actions(self, extras: tuple[str, ...] = ()) -> themes.ThemeActions:
        def login_for(row: Any) -> Any:
            if row.slot not in self.relogin_slots:
                return None
            return lambda: self.calls.append(("codex_login", row.slot))

        return themes.ThemeActions(
            switch_to=lambda slot: self.calls.append(("switch_to", slot)),
            codex_login_for=login_for,
            open_url=lambda url: self.calls.append(("open_url", url)) or True,
            attention_extras=extras,
        )


def _build(snap: UiSnapshot | None = None, actions: themes.ThemeActions | None = None) -> list[Any]:
    return themes.build_blocks("cards", snap or _snapshot(), NOW, actions or themes.ThemeActions())


def _by_key(blocks: list[Any]) -> dict[str, Any]:
    return {b.key: b for b in blocks}


def _drawn(block: Any, *, hl: bool = False) -> list[str]:
    """Every string *block* draws (text and chips), painted into a real
    offscreen context so the paint runs exactly as in the menu."""
    from AppKit import NSImage

    seen: list[str] = []

    class Rec(card_base.Pen):
        def text(self, s: str, *a: Any, **k: Any) -> float:
            if s:
                seen.append(s)
            return super().text(s, *a, **k)

    img = NSImage.alloc().initWithSize_((card_base.W, max(1.0, block.height)))
    img.lockFocus()
    try:
        block.paint(Rec(hl), card_base.W, block.height)
    finally:
        img.unlockFocus()
    return seen


def _pixels(path: Path) -> Any:
    from AppKit import NSBitmapImageRep
    from Foundation import NSData

    return NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(str(path)))


def _rgb(rep: Any, x_pt: float, y_pt: float) -> tuple[int, int, int]:
    c = rep.colorAtX_y_(int(x_pt * 2), int(y_pt * 2))
    return (round(c.redComponent() * 255), round(c.greenComponent() * 255), round(c.blueComponent() * 255))


def _top_of(blocks: list[Any], key: str) -> float:
    y = card_base.MATERIAL_PAD
    for b in blocks:
        if b.key == key:
            return y
        y += b.height
    raise KeyError(key)


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_cards_stack_attention_then_claude_then_codex() -> None:
    """The prototype's order: Needs attention, the Claude card, the Codex card;
    every block keyed (renders and tests address blocks by key)."""
    _need_appkit()
    blocks = _build()
    keys = [b.key for b in blocks]
    assert all(keys) and len(set(keys)) == len(keys), keys
    assert all(b.height > 0 for b in blocks)
    first = {prefix: next(i for i, k in enumerate(keys) if k.startswith(prefix))
             for prefix in ("cards-attn-", "cards-claude-", "cards-codex-")}
    assert first["cards-attn-"] < first["cards-claude-"] < first["cards-codex-"], keys
    assert keys[0] == "cards-attn-head" and keys[-1] == "cards-codex-foot", keys


def test_no_attention_card_when_nothing_needs_the_operator() -> None:
    _need_appkit()
    snap = _snapshot(account_notes={}, account_note_kinds={},
                     quota_rows=_snapshot().quota_rows[:1])
    keys = [b.key for b in _build(snap)]
    assert not any(k.startswith("cards-attn") for k in keys), keys


def test_empty_fleet_draws_nothing() -> None:
    """No accounts and no Codex: the app shows classic; the theme invents nothing."""
    _need_appkit()
    snap = _snapshot(accounts=(), active=None, quota_rows=(), account_notes={}, account_note_kinds={})
    assert _build(snap) == []


# ---------------------------------------------------------------------------
# actions: clickable iff something happens
# ---------------------------------------------------------------------------


def test_only_actionable_rows_are_selectable() -> None:
    """With inert actions only the Claude switch target is clickable: the active
    account, the noted account, the Codex rows and every header are static."""
    _need_appkit()
    selectable = sorted(b.key for b in _build() if b.selectable)
    assert selectable == ["cards-claude-2"], selectable


def test_claude_row_click_switches_to_that_slot() -> None:
    _need_appkit()
    rec = _Recorder()
    blocks = _by_key(_build(actions=rec.actions()))
    row = blocks["cards-claude-2"]
    assert (row.hl_inset, row.hl_vinset, row.hl_radius) == (14.0, 1.0, 6.0)
    row.action()
    assert rec.calls == [("switch_to", 2)], rec.calls
    assert "click to switch" in row.tooltip


def test_dead_codex_login_is_clickable_in_attention_and_card() -> None:
    """A dead Codex login is ``Log in again…`` in Needs attention and is listed
    only there - not a second time in the Codex card (design2 defect #4). The
    other Codex rows (switching is done in Codex itself) stay static."""
    _need_appkit()
    rec = _Recorder(relogin_slots=(-3,))
    blocks = _by_key(_build(actions=rec.actions()))
    assert blocks["cards-attn-codex--3"].selectable
    assert "cards-codex--3" not in blocks, "listed twice"
    assert not blocks["cards-codex--2"].selectable
    assert not blocks["cards-codex-active"].selectable
    blocks["cards-attn-codex--3"].action()
    assert rec.calls == [("codex_login", -3)], rec.calls
    assert "Log in again…" in _drawn(blocks["cards-attn-codex--3"])


def test_attention_item_without_action_draws_no_chevron_and_no_login_line() -> None:
    """Without a login action the Codex attention item must not promise one."""
    _need_appkit()
    item = _by_key(_build())["cards-attn-codex--3"]
    assert not item.selectable
    assert "Log in again…" not in _drawn(item)


def test_add_credits_is_upstream_wording_not_a_link() -> None:
    """No Add-credits URL exists in the codebase: the words are drawn, the row
    stays static and nothing is opened."""
    _need_appkit()
    rec = _Recorder()
    row = _by_key(_build(actions=rec.actions()))["cards-codex--2"]
    assert not row.selectable
    assert "out of credits · Add credits" in _drawn(row)
    assert not any(name == "open_url" for name, _ in rec.calls)


# ---------------------------------------------------------------------------
# honesty
# ---------------------------------------------------------------------------


def test_noted_account_is_in_attention_only_and_shows_no_figures() -> None:
    _need_appkit()
    blocks = _build()
    keys = [b.key for b in blocks]
    assert "cards-claude-3" not in keys, keys
    item = _by_key(blocks)["cards-attn-claude-3"]
    drawn = _drawn(item)
    assert "noted" in drawn and "re-login needed" in drawn, drawn
    assert "cswap add" in drawn, drawn  # the remedy command, verbatim, as a chip
    assert not any(s.endswith("%") for s in drawn), drawn
    assert RELOGIN in item.tooltip


def test_noted_active_account_withholds_its_big_figure() -> None:
    _need_appkit()
    snap = _snapshot(account_notes={1: RELOGIN}, account_note_kinds={1: "re-login needed"})
    hero = _by_key(_build(snap))["cards-claude-active"]
    drawn = _drawn(hero)
    assert not any("%" in s for s in drawn), drawn
    assert any("relogin" in s for s in drawn), drawn  # the app's own status wording


def test_ended_window_never_binds_the_big_figure() -> None:
    _need_appkit()
    snap = _snapshot()
    active = replace(snap.accounts[0], five_hour_pct=100.0, expired_windows=("five_hour",))
    snap = replace(snap, accounts=(active,) + snap.accounts[1:], active=active)
    drawn = _drawn(_by_key(_build(snap))["cards-claude-active"])
    assert "↺ ended" in drawn, drawn
    big = [s for s in drawn if s.endswith("%")]
    assert big[0] == "13%", drawn  # the 7d window binds; the ended 5h 100% does not


def test_stale_reading_says_so() -> None:
    _need_appkit()
    snap = _snapshot()
    active = replace(snap.accounts[0], usage_age_seconds=2 * 3600.0, stale_after_seconds=600.0)
    snap = replace(snap, accounts=(active,) + snap.accounts[1:], active=active)
    drawn = _drawn(_by_key(_build(snap))["cards-claude-active"])
    assert any(s.startswith("reading ") and s.endswith(" old") for s in drawn), drawn


def test_attention_extras_are_drawn_verbatim_and_counted() -> None:
    _need_appkit()
    rec = _Recorder()
    blocks = _build(actions=rec.actions(extras=(EXTRA,)))
    by = _by_key(blocks)
    assert EXTRA in _drawn(by["cards-attn-extra-0"])
    # 1 noted Claude slot + 1 alarmed Codex row + 1 extra line
    assert "3" in _drawn(by["cards-attn-head"])
    snap = _snapshot(account_notes={}, account_note_kinds={}, quota_rows=_snapshot().quota_rows[:1])
    only = _by_key(_build(snap, rec.actions(extras=(EXTRA,))))
    assert "cards-attn-extra-0" in only, "the extras alone still open the attention card"


# ---------------------------------------------------------------------------
# pixels
# ---------------------------------------------------------------------------


def test_card_slices_paint_one_card_and_hover_highlights_the_row() -> None:
    """Sampled from a real composite: every slice of the Claude card carries the
    card fill (not the bare material), and hovering a row paints the native
    selection colour inside the card, text white."""
    _need_appkit()
    blocks = _build()
    claude = [b for b in blocks if b.key.startswith("cards-claude-")]
    with tempfile.TemporaryDirectory() as tmp:
        plain, hover = Path(tmp) / "plain.png", Path(tmp) / "hover.png"
        card_base.render_png(blocks, plain, dark=False)
        card_base.render_png(blocks, hover, dark=False, hover_key="cards-claude-2")
        rp, rh = _pixels(plain), _pixels(hover)
        # the 6 pt gap between the attention card and the Claude card: bare material
        material = _rgb(rp, 5, _top_of(blocks, "cards-claude-head") + 3)
        for b in claude:
            top = _top_of(blocks, b.key)
            mid = {"cards-claude-head": top + b.height - 3, "cards-claude-foot": top + 1}.get(
                b.key, top + b.height / 2)
            px = _rgb(rp, 14, mid)
            assert px != material and min(px) >= 240, (b.key, px, material)
        top = _top_of(blocks, "cards-claude-2")
        row = _by_key(blocks)["cards-claude-2"]
        before, after = _rgb(rp, 16, top + row.height / 2), _rgb(rh, 16, top + row.height / 2)
        assert min(before) >= 240, before
        assert after[0] < 80 and after[2] > 180, f"hovered row not in the selection colour: {after}"
        edge = _rgb(rh, 12, top + row.height / 2)  # 2 pt left of the 14 pt highlight inset
        assert min(edge) >= 240, f"the highlight must sit inside the card: {edge}"


PINK = (212, 176, 168)  # a wallpaper-tinted menu material (SYNTHETIC render input)


def _roles(block: Any) -> tuple[list[tuple[str, Any]], list[str]]:
    """(text, role) pairs and pill texts one block draws, painted for real."""
    from AppKit import NSImage

    texts: list[tuple[str, Any]] = []
    pills: list[str] = []

    class Rec(card_base.Pen):
        def text(self, s: str, x: float, y: float, size: float, weight: Any = "regular",
                 role: Any = "label", **k: Any) -> float:
            if s:
                texts.append((s, role))
            return super().text(s, x, y, size, weight, role, **k)

        def pill(self, s: str, *a: Any, **k: Any) -> float:
            pills.append(s)
            return super().pill(s, *a, **k)

    img = NSImage.alloc().initWithSize_((card_base.W, max(1.0, block.height)))
    img.lockFocus()
    try:
        block.paint(Rec(False), card_base.W, block.height)
    finally:
        img.unlockFocus()
    return texts, pills


def test_cards_are_neutral_panels_the_wallpaper_never_tints() -> None:
    """Design 4: over a pink wallpaper the 0.62-white cards (and the amber
    attention tint) came out pink/beige. Every card - Needs attention too -
    is a near-opaque neutral panel, so its inside stays grey-white."""
    _need_appkit()
    blocks = _build(actions=_Recorder().actions(extras=(EXTRA,)))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pink.png"
        card_base.render_png(blocks, path, dark=False, material=[PINK])
        rep = _pixels(path)
        for key in ("cards-attn-claude-3", "cards-claude-2", "cards-codex--2"):
            b = _by_key(blocks)[key]
            px = _rgb(rep, 15, _top_of(blocks, key) + b.height / 2)
            assert min(px) >= 235 and max(px) - min(px) <= 8, (key, px)


def test_needs_attention_is_marked_by_an_orange_leading_rule() -> None:
    """With the tint gone, severity is a 3 pt orange rule down the card's
    leading edge (and the orange symbol), not a beige wash."""
    _need_appkit()
    blocks = _build()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "attn.png"
        card_base.render_png(blocks, path, dark=False, material=[PINK])
        rep = _pixels(path)
        b = _by_key(blocks)["cards-attn-claude-3"]
        mid = _top_of(blocks, b.key) + b.height / 2
        rule = _rgb(rep, 11.5, mid)
        assert rule[0] > 220 and 100 < rule[1] < 190 and rule[2] < 80, rule
        no_rule = _by_key(blocks)["cards-claude-2"]
        edge = _rgb(rep, 11.5, _top_of(blocks, no_rule.key) + no_rule.height / 2)
        assert min(edge) >= 235, f"only Needs attention carries the rule: {edge}"


def test_hero_figure_is_neutral_and_next_is_a_pill() -> None:
    """The 26 pt figure is drawn in the label colour even when the window is
    critical - the bar carries the severity (a coloured figure read brown on a
    tinted menu). The ``next`` tag is a pill, not loose accent text."""
    _need_appkit()
    snap = _snapshot()
    hot = replace(snap.active, five_hour_pct=92.0)
    accounts = tuple(hot if r.slot == hot.slot else r for r in snap.accounts)
    blocks = _build(replace(snap, accounts=accounts, active=hot))
    texts, _ = _roles(_by_key(blocks)["cards-claude-active"])
    assert ("92%", "label") in texts, texts
    _, pills = _roles(_by_key(blocks)["cards-claude-2"])
    assert pills == ["next"], pills


def test_an_ended_codex_window_draws_a_dimmed_hero_figure() -> None:
    """The neutral 26 pt figure is for LIVE readings. An active Codex login
    whose only window has ended keeps its last figure, but that figure is a
    dead reading: it must not draw in label ink (it would look identical to
    a live 97 %), its bar fills with ``dead_fill`` and the line says ended."""
    _need_appkit()
    snap = _snapshot()
    work = replace(snap.quota_rows[0], seven_day_pct=97.0, expired_windows=("seven_day",))
    ended = replace(snap, quota_rows=(work,) + snap.quota_rows[1:])
    texts, _ = _roles(_by_key(_build(ended))["cards-codex-active"])
    figure = [role for s, role in texts if s == "97%"]
    assert figure and figure[0] != "label", texts
    assert any("↺ ended" in s for s, _r in texts), texts
    live = replace(snap, quota_rows=(replace(work, expired_windows=()),) + snap.quota_rows[1:])
    live_texts, _ = _roles(_by_key(_build(live))["cards-codex-active"])
    assert ("97%", "label") in live_texts, live_texts
    assert not any("↺ ended" in s for s, _r in live_texts), live_texts
    fills: list[Any] = []

    class Pen:
        def bar(self, x: float, y: float, w: float, h: float, pct: Any, role: Any, track: Any) -> None:
            fills.append(role)

    from cc_usage_widget.themes import cards
    cards.bar(Pen(), 0, 0, 100, 97.0, "crit", expired=True)
    assert fills == ["dead_fill"], fills


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def test_codex_header_names_who_opens_next_and_the_banked_resets() -> None:
    """The header carries the fleet line's who-opens-next alias and the
    reset-credit total, as glance, apple and dense do; with Settings ▸ Codex
    fleet line OFF it carries none of the fleet facts."""
    _need_appkit()
    from cc_usage_widget import app as A

    original = A._codex_fleet_parts
    A._codex_fleet_parts = lambda rows, now: A._CodexFleetParts(
        room=1, total=3, next_at=NOW + 3600, next_alias="side", resets="1 reset banked")
    try:
        head = _by_key(_build())["cards-codex-head"].label
        assert "(side)" in head and "1 reset banked" in head, head
        off = _snapshot(settings={**_snapshot().settings, "codex_fleet_line_enabled": False})
        head = _by_key(_build(off))["cards-codex-head"].label
        assert "room" not in head and "side" not in head and "banked" not in head, head
    finally:
        A._codex_fleet_parts = original


def test_long_codex_fleet_facts_get_their_own_line_untruncated() -> None:
    """Design 5: "3/4 room · next ↺ … (alias) · 1 reset banked" outgrew the
    header and was cut to "1 reset…". The room figure stays beside the title;
    the rest moves to a caption line under it, drawn in full."""
    _need_appkit()
    from cc_usage_widget import app as A

    original = A._codex_fleet_parts
    A._codex_fleet_parts = lambda rows, now: A._CodexFleetParts(
        room=3, total=4, next_at=NOW + 3600, next_alias="a-long-alias", resets="2 resets banked")
    try:
        texts, _ = _roles(_by_key(_build())["cards-codex-head"])
    finally:
        A._codex_fleet_parts = original
    drawn = [s for s, _r in texts]
    assert "3/4 room" in drawn, drawn
    rest = [s for s in drawn if "a-long-alias" in s]
    assert rest and rest[0].endswith("(a-long-alias) · 2 resets banked"), drawn
    from cc_usage_widget.themes import cards  # the line has the room to be drawn whole (no fit "…")
    assert cards.text_w(card_base.Pen(), rest[0], "secondary") <= cards.X1 - cards.X0 - 18, rest


def test_window_rows_and_account_rows_share_columns() -> None:
    """More air must not break the grid: the hero's window bars end where the
    account rows' bars end, and every figure shares one right edge, as does
    every reset mark."""
    _need_appkit()
    from AppKit import NSImage

    bars: list[float] = []
    rights: dict[str, set[float]] = {"pct": set(), "reset": set()}

    class Rec(card_base.Pen):
        def bar(self, x: float, y: float, w: float, h: float, *a: Any, **k: Any) -> None:
            bars.append(round(x + w, 1))
            return super().bar(x, y, w, h, *a, **k)

        def text(self, s: str, x: float, y: float, *a: Any, **k: Any) -> float:
            if k.get("right") and s.endswith("%") and a and a[0] < 20:
                rights["pct"].add(round(x, 1))
            elif k.get("right") and s.startswith("↺"):
                rights["reset"].add(round(x, 1))
            return super().text(s, x, y, *a, **k)

    blocks = _by_key(_build())
    for key in ("cards-claude-active", "cards-claude-2", "cards-codex-active", "cards-codex--2"):
        b = blocks[key]
        img = NSImage.alloc().initWithSize_((card_base.W, max(1.0, b.height)))
        img.lockFocus()
        try:
            b.paint(Rec(False), card_base.W, b.height)
        finally:
            img.unlockFocus()
    assert len(bars) >= 4 and len(set(bars)) == 1, bars
    assert len(rights["pct"]) == 1 and len(rights["reset"]) == 1, rights


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
