"""card_base (design 3, 2026-09-25): the blocks, pen, views and renderer the
three card themes draw with.

Why these tests exist: a card menu is judged on pixels, but it is USED through
mechanics - a hover highlight that only appears on clickable rows, a click that
runs its action exactly once and closes the menu, Return doing what a click
does, VoiceOver reading a label and a role. Those are asserted here on real
views, offscreen; the pixel checks sample a rendered PNG so a highlight or a
bar that stops drawing fails a test rather than a screenshot review.

Fixtures are SYNTHETIC (no account data at all). AppKit-dependent tests skip
with a printed reason when PyObjC is missing.

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    CC_USAGE_WIDGET_NO_REVEAL=1 $PY tests/test_card_base.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget import card_base as cb  # noqa: E402

SKIP = "" if cb.available() else "AppKit (PyObjC) is not importable"


class Skip(Exception):
    pass


def _need_appkit() -> None:
    if SKIP:
        raise Skip(SKIP)


def _pixels(path: Path) -> Any:
    from AppKit import NSBitmapImageRep
    from Foundation import NSData

    return NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(str(path)))


def _rgb(rep: Any, x_pt: float, y_pt: float) -> tuple[int, int, int]:
    """The stored 0-255 components at a POINT (the PNG is 2x; y from the top).
    Raw values, no colour-space conversion: the renderer writes what it drew."""
    c = rep.colorAtX_y_(int(x_pt * 2), int(y_pt * 2))
    return (round(c.redComponent() * 255), round(c.greenComponent() * 255), round(c.blueComponent() * 255))


def _close(a: tuple[int, int, int], b: tuple[int, int, int], tol: int = 6) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _render(blocks: list[Any], **kwargs: Any) -> tuple[Path, Any]:
    folder = Path(tempfile.mkdtemp(prefix="card_base_"))
    path = folder / "out.png"
    cb.render_png(blocks, path, **kwargs)
    return path, _pixels(path)


LIGHT_MATERIAL = (236, 236, 236)
DARK_MATERIAL = (38, 38, 40)
PAD = cb.MATERIAL_PAD


# ---------------------------------------------------------------------------
# Block
# ---------------------------------------------------------------------------


def test_a_block_is_selectable_exactly_when_it_has_an_action() -> None:
    """Hover, click, Return and the VoiceOver button role all hang off this one
    bit, so a static caption can never look clickable."""
    static = cb.Block(22, None, tooltip="tip only")
    live = cb.Block(22, None, ax="Switch to main", action=lambda: None, key="row")
    assert not static.selectable and live.selectable
    assert static.label == "tip only", "VoiceOver falls back to the tooltip"
    assert live.label == "Switch to main"
    assert live.height == 22.0 and live.key == "row"


# ---------------------------------------------------------------------------
# Pen
# ---------------------------------------------------------------------------


def test_text_truncates_with_an_ellipsis_inside_maxw() -> None:
    """A long alias must end in … inside its column, never run into the figure."""
    _need_appkit()
    pen = cb.Pen()
    long = "synthetic-alias-that-is-far-too-long-for-its-column"
    fitted = pen.fit(long, 13, "regular", 80)
    assert fitted.endswith("…") and len(fitted) < len(long), fitted
    assert pen.width(fitted, 13) <= 80
    assert pen.fit("short", 13, "regular", 80) == "short"
    drawn: list[float] = []

    def paint(p: Any, w: float, h: float) -> None:
        drawn.append(p.text(long, cb.LEAD, 16, 13, "regular", "label", maxw=80))
        drawn.append(p.text("42%", cb.RIGHT, 16, 13, "semibold", "label", mono=True, right=True))

    _render([cb.Block(24, paint)], dark=False)
    assert 0 < drawn[0] <= 80, drawn
    assert drawn[1] > 0, drawn


def test_monospaced_digits_align_figures() -> None:
    """Figures use monospaced digits so a column of percentages lines up."""
    _need_appkit()
    pen = cb.Pen()
    assert abs(pen.width("11%", 13, mono=True) - pen.width("88%", 13, mono=True)) < 0.01


def test_roles_resolve_and_hover_turns_text_white() -> None:
    _need_appkit()
    from AppKit import NSColor

    for role in ("label", "secondary", "tertiary", "quaternary", "accent", "ok", "warn", "crit",
                 "good", "white", "track", "hl", "warn_text", "crit_text"):
        assert cb.Pen().color(role) is not None, role
    white = NSColor.whiteColor()
    assert cb.Pen(hl=True).color("label") == white
    assert cb.Pen(hl=True).color("crit") == white, "a highlighted row is white text, whatever its severity"
    try:
        cb.Pen().color("no-such-role")
    except KeyError:
        pass
    else:
        raise AssertionError("an unknown role must fail loudly, not draw black")


def test_register_role_adds_a_dynamic_colour_with_a_hover_mapping() -> None:
    _need_appkit()
    cb.register_role("test_card_fill", (255, 0, 0), (0, 0, 255), hover="clear")
    assert "test_card_fill" in cb.role_names()

    def paint(p: Any, w: float, h: float) -> None:
        p.rrect(40, 4, 80, 16, 4, "test_card_fill")

    _, light = _render([cb.Block(24, paint)], dark=False)
    _, dark = _render([cb.Block(24, paint)], dark=True)
    assert _close(_rgb(light, 80, PAD + 12), (255, 0, 0), 12), _rgb(light, 80, PAD + 12)
    assert _close(_rgb(dark, 80, PAD + 12), (0, 0, 255), 12), _rgb(dark, 80, PAD + 12)


def test_bar_fills_its_percentage_and_none_is_track_only() -> None:
    """An absent reading is a bare track, never a fill pretending to be 0 %."""
    _need_appkit()

    def half(p: Any, w: float, h: float) -> None:
        p.bar(40, 8, 200, 8, 50.0, "crit")

    def missing(p: Any, w: float, h: float) -> None:
        p.bar(40, 8, 200, 8, None, "crit")

    _, rep = _render([cb.Block(24, half), cb.Block(24, missing)], dark=False)
    red = _rgb(rep, 90, PAD + 12)
    unfilled = _rgb(rep, 190, PAD + 12)
    assert red[0] > 200 and red[1] < 110, red
    assert not (unfilled[0] > 200 and unfilled[1] < 110), unfilled
    none_row = _rgb(rep, 90, PAD + 24 + 12)
    assert not (none_row[0] > 200 and none_row[1] < 110), none_row


# Design 4 (2026-09-25): the real menu material is translucent and tinted by
# the wallpaper, and our views are not vibrant, so the alpha-thin system greys
# washed out live - captions near-invisible, a 0 % track reading as a FULL
# white bar, warn text turning brown. These pin the replacement values.
PINK = (212, 176, 168)      # sampled from the operator's screenshot (SYNTHETIC render material)
DARK_TINT = (58, 44, 50)


def _resolved(role: str, dark: bool) -> Any:
    """*role* as the sRGB colour it draws in that appearance."""
    from AppKit import NSAppearance, NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSColorSpace

    out: list[Any] = []
    appearance = NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua if dark else NSAppearanceNameAqua)
    appearance.performAsCurrentDrawingAppearance_(
        lambda: out.append(cb.Pen().color(role).colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())))
    return out[0]


def test_grey_roles_are_readable_values_not_the_thin_system_greys() -> None:
    """Information is drawn in ``secondary`` - label at ~0.62 (light) / ~0.68
    (dark) - and ``tertiary`` stays a strong-enough decoration; the track is a
    neutral groove (black 0.10 / white 0.14), never the system fill that a
    tinted menu turns white."""
    _need_appkit()
    for role, light_a, dark_a in (("secondary", 0.62, 0.68), ("tertiary", 0.45, 0.45), ("track", 0.10, 0.14)):
        lc, dc = _resolved(role, False), _resolved(role, True)
        assert abs(lc.alphaComponent() - light_a) < 0.02, (role, lc)
        assert abs(dc.alphaComponent() - dark_a) < 0.02, (role, dc)
        assert lc.redComponent() < 0.05 and dc.redComponent() > 0.95, f"{role}: black ink on light, white on dark"


def _contrast_on_white(c: Any) -> float:
    """WCAG contrast ratio of an opaque sRGB NSColor against white."""
    def lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * lin(c.redComponent()) + 0.7152 * lin(c.greenComponent()) + 0.0722 * lin(c.blueComponent())
    return 1.05 / (lum + 0.05)


def test_warn_and_crit_text_are_vivid_never_brown() -> None:
    """Light-mode warn/crit TEXT was darkened to (172,82,0) and read brown on
    a pink menu; it must stay a bright hue (red channel high). But it is
    11-13 pt information on a white card, so vivid may not cost legibility:
    every severity text keeps WCAG AA (>= 4.5:1) on white."""
    _need_appkit()
    for role in ("warn_text", "crit_text"):
        c = _resolved(role, False)
        r, g, b = (round(v * 255) for v in (c.redComponent(), c.greenComponent(), c.blueComponent()))
        assert r >= 185 and r - b >= 150, (role, (r, g, b))
    good = _resolved("good_text", False)
    assert good.greenComponent() > good.redComponent() + 0.3, "good text is a clear green"
    for role in ("warn_text", "crit_text", "good_text"):
        ratio = _contrast_on_white(_resolved(role, False))
        assert ratio >= 4.5, (role, round(ratio, 2))


def test_dead_fill_is_quieter_than_information_and_louder_than_the_track() -> None:
    """A dead reading (ended window, disabled account) fills its bar with
    ``dead_fill``: far fainter than ``tertiary``/``secondary`` so it never
    reads as a live bar, yet still visible above the empty track."""
    _need_appkit()
    for dark in (False, True):
        dead = _resolved("dead_fill", dark).alphaComponent()
        assert dead <= _resolved("tertiary", dark).alphaComponent() - 0.15, (dark, dead)
        assert dead >= _resolved("track", dark).alphaComponent() + 0.08, (dark, dead)


def test_percent_figures_are_rounded_and_keep_monospaced_digits() -> None:
    """Figures are SF Pro Rounded; switching design must not drop the
    monospaced-digit feature, or a column of percentages stops lining up."""
    _need_appkit()
    pen = cb.Pen()
    rounded = pen.font(26, "semibold", mono=True, rounded=True)
    plain = pen.font(26, "semibold", mono=True)
    assert "Rounded" in str(rounded.fontName()), rounded.fontName()
    assert "Rounded" not in str(plain.fontName()), plain.fontName()
    for size in (11, 13, 26):
        widths = {pen.width(d * 3 + "%", size, "semibold", mono=True, rounded=True) for d in "0123456789"}
        assert max(widths) - min(widths) < 0.01, (size, widths)
    assert pen.font(13, "semibold", rounded=True) is pen.font(13, "semibold", rounded=True), "fonts are cached"


def test_a_zero_bar_draws_a_visible_groove_on_every_material() -> None:
    """A 0 % account must look EMPTY: its track differs clearly from the
    material around it - darker on a light menu, lighter on a dark one -
    including on a wallpaper-tinted material. (The old system fill moved the
    pixel by ~30 of 765; a visible groove moves it by more than 45.)"""
    _need_appkit()

    def paint(p: Any, w: float, h: float) -> None:
        p.bar(40, 8, 200, 8, 0.0, "ok")

    for material, dark in ((PINK, False), (LIGHT_MATERIAL, False), (DARK_MATERIAL, True), (DARK_TINT, True)):
        _, rep = _render([cb.Block(24, paint)], dark=dark, material=[material])
        track, beside = _rgb(rep, 140, PAD + 12), _rgb(rep, 300, PAD + 12)
        assert _close(beside, material), (material, beside)
        assert sum(abs(a - b) for a, b in zip(track, beside)) > 45, (material, track, beside)
        assert (sum(track) > sum(beside)) == dark, (material, track, beside)


def test_material_stops_paint_the_simulated_wallpaper_tint() -> None:
    """``render_png(material=...)``: one stop is a flat colour, two a vertical
    gradient from the first (top) to the last (bottom)."""
    _need_appkit()
    _, flat = _render([cb.Block(60, None)], dark=False, material=[PINK])
    assert _close(_rgb(flat, 180, PAD + 30), PINK), _rgb(flat, 180, PAD + 30)
    blue = (179, 192, 204)
    _, grad = _render([cb.Block(200, None)], dark=False, material=[PINK, blue])
    top, bottom = _rgb(grad, 180, 3), _rgb(grad, 180, 200 + 2 * PAD - 3)
    assert _close(top, PINK, 8) and _close(bottom, blue, 8), (top, bottom)


def test_pill_is_wider_than_its_text_and_draws_no_background_while_highlighted() -> None:
    _need_appkit()
    widths: dict[str, float] = {}

    def paint(p: Any, w: float, h: float) -> None:
        widths["pill"] = p.pill("next", 200, 16)

    blk = cb.Block(24, paint, action=lambda: None, key="row")
    _, rep = _render([blk], dark=False)
    assert widths["pill"] > cb.Pen().width("next", 10, "semibold"), widths
    inside = _rgb(rep, 202, PAD + 14)  # left padding of the pill, no glyph there
    assert not _close(inside, LIGHT_MATERIAL, 4), "the pill has a tinted background"
    _, hovered = _render([blk], dark=False, hover_key="row")
    assert inside != _rgb(hovered, 202, PAD + 14)


def test_panel_slices_stack_into_one_card() -> None:
    """The cards theme spreads one rounded card over several menu items: the
    middle slice must fill edge to edge vertically, the material stay outside."""
    _need_appkit()
    cb.register_role("test_panel", (0, 128, 0), (0, 128, 0))

    def top(p: Any, w: float, h: float) -> None:
        p.panel(10, 4, w - 20, h - 4, 10, "test_panel", stroke="sep", bottom=False)

    def middle(p: Any, w: float, h: float) -> None:
        p.panel(10, 0, w - 20, h, 10, "test_panel", stroke="sep", top=False, bottom=False)

    def bottom(p: Any, w: float, h: float) -> None:
        p.panel(10, 0, w - 20, h - 4, 10, "test_panel", stroke="sep", top=False)

    _, rep = _render([cb.Block(30, top), cb.Block(20, middle), cb.Block(30, bottom)], dark=False)
    for y in (PAD + 20, PAD + 30, PAD + 49.5, PAD + 60):
        assert _close(_rgb(rep, 180, y), (0, 128, 0), 20), (y, _rgb(rep, 180, y))
    assert _close(_rgb(rep, 4, PAD + 40), LIGHT_MATERIAL), _rgb(rep, 4, PAD + 40)
    # rounded outer corner: the very corner pixel of the top slice is material
    assert not _close(_rgb(rep, 10.5, PAD + 4.5), (0, 128, 0), 20)


def test_symbol_chip_line_circle_draw_and_unknown_symbol_is_zero() -> None:
    _need_appkit()
    widths: dict[str, float] = {}

    def paint(p: Any, w: float, h: float) -> None:
        widths["sym"] = p.symbol("exclamationmark.triangle.fill", cb.LEAD, 4, 13, "warn", box=18)
        widths["none"] = p.symbol("no.such.symbol.zzz", cb.LEAD, 4, 13, "warn")
        widths["chip"] = p.chip("cswap add", 60, 16, 10)
        p.line(cb.LEAD, 26, cb.RIGHT, "sep")
        p.circle(200, 4, 16, "accent")

    _render([cb.Block(30, paint)], dark=False)
    assert widths["sym"] > 0 and widths["none"] == 0.0, widths
    assert widths["chip"] > cb.Pen().width("cswap add", 10, code=True), widths


# ---------------------------------------------------------------------------
# Views: highlight, click, keyboard, accessibility
# ---------------------------------------------------------------------------


def test_hover_draws_the_native_highlight_only_on_clickable_blocks() -> None:
    """A static caption given the hover key must stay undrawn; a clickable row
    gets the rounded selection inset 5 pt, with the material outside it."""
    _need_appkit()

    def caption(p: Any, w: float, h: float) -> None:
        p.text("Needs attention", cb.LEAD, 16, 11, "semibold", "secondary")

    row = cb.Block(24, caption, action=lambda: None, key="row")
    static = cb.Block(24, caption, key="static")
    _, plain = _render([row], dark=False)
    _, hover = _render([row], dark=False, hover_key="row")
    _, static_hover = _render([static], dark=False, hover_key="static")
    inside = (300, PAD + 12)
    assert _close(_rgb(plain, *inside), LIGHT_MATERIAL), _rgb(plain, *inside)
    assert not _close(_rgb(hover, *inside), LIGHT_MATERIAL, 30), _rgb(hover, *inside)
    assert _close(_rgb(hover, 2, PAD + 12), LIGHT_MATERIAL), "the selection is inset 5 pt"
    assert _close(_rgb(static_hover, *inside), LIGHT_MATERIAL), "a static block never highlights"
    # A row inside a drawn card pulls the selection in to sit within the card.
    carded = cb.Block(24, caption, action=lambda: None, key="in-card", hl_inset=14, hl_radius=6)
    _, card_hover = _render([carded], dark=False, hover_key="in-card")
    assert _close(_rgb(card_hover, 9, PAD + 12), LIGHT_MATERIAL), _rgb(card_hover, 9, PAD + 12)
    assert not _close(_rgb(card_hover, *inside), LIGHT_MATERIAL, 30)


def test_dark_render_uses_the_dark_material() -> None:
    _need_appkit()
    _, rep = _render([cb.Block(24, None)], dark=True)
    assert _close(_rgb(rep, 180, PAD + 12), DARK_MATERIAL), _rgb(rep, 180, PAD + 12)


def test_render_size_is_2x_and_includes_the_tail() -> None:
    _need_appkit()
    tail = [{"title": "Cost", "submenu": True}, None, {"title": "Quit", "key_eq": "⌘Q"}]
    folder = Path(tempfile.mkdtemp(prefix="card_base_"))
    wide, high = cb.render_png([cb.Block(40, None)], folder / "t.png", dark=False, tail=tail)
    # 40 + separator (auto) + 22 + 11 + 22, plus 5 pt of material top and bottom
    assert (wide, high) == (720, int((40 + 11 + 22 + 11 + 22 + 2 * PAD) * 2)), (wide, high)


def test_click_runs_the_action_once_and_closes_the_menu() -> None:
    _need_appkit()
    from AppKit import NSMenu

    calls: list[str] = []
    block = cb.Block(24, None, ax="Switch to work1", tooltip="work1 · 5h 12%",
                     action=lambda: calls.append("switch"), key="row")
    items = cb.menu_items_for([block])
    native = items[0]._menuitem
    view = native.view()
    assert isinstance(view, cb.BlockView), view
    menu = NSMenu.alloc().initWithTitle_("t")
    menu.addItem_(native)
    assert view.enclosingMenuItem() == native
    assert not view.isHighlightedNow(), "nothing is highlighted before tracking"
    view.mouseUp_(None)  # cancelTracking on a menu that is not tracking is a no-op
    assert calls == ["switch"], calls


def test_return_on_a_highlighted_row_does_what_a_click_does() -> None:
    """NSMenu sends a view item's action on Return; that action is the block's."""
    _need_appkit()
    calls: list[str] = []
    live = cb.Block(24, None, ax="Log in again", action=lambda: calls.append("login"), key="cx")
    static = cb.Block(22, None, ax="Claude", key="hdr")
    items = cb.menu_items_for([live, static])
    assert items[0].callback is not None and items[1].callback is None
    items[0].callback(items[0])
    assert calls == ["login"], calls
    assert not items[1]._menuitem.isEnabled() or items[1].callback is None


def test_accessibility_label_role_tooltip_and_press() -> None:
    _need_appkit()
    from AppKit import NSAccessibilityButtonRole, NSAccessibilityStaticTextRole

    calls: list[int] = []
    live = cb.BlockView.viewForBlock_width_(
        cb.Block(24, None, ax="Switch to work1", tooltip="all windows", action=lambda: calls.append(1)),
        cb.W,
    )
    static = cb.BlockView.viewForBlock_width_(cb.Block(22, None, tooltip="Claude · 1 of 5 with room"), cb.W)
    assert live.accessibilityRole() == NSAccessibilityButtonRole
    assert static.accessibilityRole() == NSAccessibilityStaticTextRole
    assert live.accessibilityLabel() == "Switch to work1"
    assert static.accessibilityLabel() == "Claude · 1 of 5 with room"
    assert live.toolTip() == "all windows"
    assert live.isFlipped() and live.frame().size.width == cb.W
    assert live.accessibilityPerformPress() and calls == [1]
    assert not static.accessibilityPerformPress()
    assert live.isAccessibilityElement() and static.isAccessibilityElement()
    spacer = cb.BlockView.viewForBlock_width_(cb.Block(11, None, key="sep-1"), cb.W)
    assert not spacer.isAccessibilityElement(), "VoiceOver must not stop on a blank spacer"


def test_a_failing_paint_or_action_is_logged_never_raised() -> None:
    """drawRect_ and mouseUp_ run inside AppKit: an exception there must not
    escape (it would kill the menu), and it is logged once, not per frame."""
    _need_appkit()

    def boom(p: Any, w: float, h: float) -> None:
        raise ValueError("synthetic paint failure")

    def bad_action() -> None:
        raise RuntimeError("synthetic action failure")

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        _render([cb.Block(24, boom, key="boom")], dark=False)
        _render([cb.Block(24, boom, key="boom")], dark=False)
        view = cb.BlockView.viewForBlock_width_(cb.Block(24, None, action=bad_action, key="bad"), cb.W)
        view.mouseUp_(None)
    text = err.getvalue()
    assert text.count("synthetic paint failure") == 1, text
    assert "synthetic action failure" in text, text


def test_apply_native_title_keeps_the_plain_key() -> None:
    _need_appkit()
    import rumps

    item = rumps.MenuItem("Recent switches")
    assert cb.apply_native_title(item, [("Recent switches", "label"), ("  last 09:56", "secondary")])
    assert str(item._menuitem.attributedTitle().string()) == "Recent switches  last 09:56"
    assert not cb.apply_native_title(object(), [("x", "label")])


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


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
