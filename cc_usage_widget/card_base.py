"""Native card views for the themed menus (design 3, 2026-09-25).

The three card themes (``themes/apple.py``, ``themes/dense.py``,
``themes/cards.py``) describe the top of the dropdown as a list of
:class:`Block` objects - a height, a ``paint(pen, width, height)`` function,
a tooltip, a VoiceOver label and an optional click action. This module turns
blocks into real menu items (one ``NSMenuItem`` per block, carrying a
:class:`BlockView`) and, for judging a design, into an offscreen PNG composited
over a simulated menu material.

Drawing goes through :class:`Pen`, whose primitives are the superset of what
the three design prototypes used (``evidence/design2/*/proto.py``). Colours are
ROLES, not values: ``label``, ``secondary``, ``warn`` ... resolve to dynamic
colours at draw time (system colours, or our own per-appearance values where
the alpha-thin system greys wash out over a tinted menu - design 4), so one
paint function is right in light and dark and the menu material shows through
(the views never fill a background).

Every AppKit import is guarded: on a machine without PyObjC this module still
imports, :func:`available` says ``False``, and the app renders its text
layouts instead. Nothing here does I/O; painting is allocation-light and runs
on the AppKit thread only.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

try:  # AppKit is optional at import time: the text layouts need none of it.
    import objc
    from AppKit import (
        NSAccessibilityButtonRole,
        NSAccessibilityStaticTextRole,
        NSAppearance,
        NSAppearanceNameAqua,
        NSAppearanceNameDarkAqua,
        NSAttributedString,
        NSBezierPath,
        NSBitmapImageFileTypePNG,
        NSBitmapImageRep,
        NSColor,
        NSCompositingOperationSourceOver,
        NSDeviceRGBColorSpace,
        NSFont,
        NSFontAttributeName,
        NSFontDescriptorSystemDesignRounded,
        NSFontWeightBold,
        NSFontWeightLight,
        NSFontWeightMedium,
        NSFontWeightRegular,
        NSFontWeightSemibold,
        NSForegroundColorAttributeName,
        NSGradient,
        NSImage,
        NSImageSymbolConfiguration,
        NSKernAttributeName,
        NSMutableAttributedString,
        NSView,
    )
    from Foundation import NSMakePoint, NSMakeRect, NSZeroRect

    _APPKIT = True
except Exception:  # pragma: no cover - exercised only without PyObjC
    objc = None  # type: ignore[assignment]
    NSView = object  # type: ignore[assignment,misc]
    _APPKIT = False


__all__ = [
    "W",
    "LEAD",
    "TRAIL",
    "RIGHT",
    "HL_INSET",
    "HL_RADIUS",
    "Block",
    "Pen",
    "BlockView",
    "available",
    "register_role",
    "role_names",
    "menu_items_for",
    "apply_native_title",
    "fit_menu_title",
    "render_png",
]

W = 360.0
"""Fixed menu width in points (design2 brief)."""

LEAD = 20.0
"""x of native menu-item titles in a menu that has a state (checkmark) column."""

TRAIL = 14.0
"""Trailing content margin: the native chevron column."""

RIGHT = W - TRAIL
"""Right content edge (346 pt)."""

MENU_TITLE_W = 270.0
"""Widest native title (menu font, points) that keeps the menu at :data:`W`.
AppKit sizes a menu to about title width + 78 pt (state, submenu-arrow and
key-equivalent columns), measured 2026-09-25; wider titles pushed the whole
menu past the fixed-width cards (review appkit-1)."""

HL_INSET = 5.0
HL_RADIUS = 5.0
"""The native selection: a rounded ``selectedContentBackgroundColor`` inset
5 pt with a 5 pt radius, as macOS draws it behind a highlighted item."""

TAIL_ROW_H = 22.0
TAIL_SEP_H = 11.0
MATERIAL_PAD = 5.0


def available() -> bool:
    """True when PyObjC/AppKit imported, i.e. views can be built."""
    return _APPKIT


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


class Block:
    """One menu item's worth of custom drawing.

    ``paint(pen, width, height)`` draws in flipped coordinates (y grows down)
    inside a ``width`` x ``height`` view; it must be pure drawing - no I/O, no
    snapshot reads beyond what it closed over. A block is *selectable* (hover
    highlight, click, Return, VoiceOver button) iff ``action`` is set;
    ``action()`` takes no arguments. ``key`` names the block for tests and for
    :func:`render_png`'s ``hover_key``. ``hl_inset`` / ``hl_vinset`` /
    ``hl_radius`` shape the highlight (default: the native 5 pt inset, radius
    5); a row inside a drawn card can pull it in to sit within the card.
    """

    def __init__(
        self,
        height: float,
        paint: Callable[["Pen", float, float], None] | None,
        *,
        tooltip: str = "",
        ax: str = "",
        action: Callable[[], Any] | None = None,
        key: str = "",
        hl_inset: float = HL_INSET,
        hl_vinset: float = 0.0,
        hl_radius: float = HL_RADIUS,
    ) -> None:
        self.height = float(height)
        self.paint = paint
        self.tooltip = tooltip or ""
        self.ax = ax or ""
        self.action = action
        self.key = key or ""
        self.hl_inset = float(hl_inset)
        self.hl_vinset = float(hl_vinset)
        self.hl_radius = float(hl_radius)

    def __repr__(self) -> str:
        return f"Block(key={self.key!r}, height={self.height}, selectable={self.selectable})"

    @property
    def selectable(self) -> bool:
        return self.action is not None

    @property
    def label(self) -> str:
        """What VoiceOver reads: the explicit label, else the tooltip."""
        return self.ax or self.tooltip


# ---------------------------------------------------------------------------
# Colour roles
# ---------------------------------------------------------------------------

_WEIGHTS: dict[str, Any] = {}
if _APPKIT:
    _WEIGHTS = {
        "light": NSFontWeightLight,
        "regular": NSFontWeightRegular,
        "medium": NSFontWeightMedium,
        "semibold": NSFontWeightSemibold,
        "bold": NSFontWeightBold,
    }


def _weight(weight: Any) -> Any:
    if isinstance(weight, str):
        return _WEIGHTS.get(weight, _WEIGHTS.get("regular", 0.0))
    return weight if weight is not None else _WEIGHTS.get("regular", 0.0)


def _is_dark(appearance: Any) -> bool:
    try:
        best = appearance.bestMatchFromAppearancesWithNames_(
            [NSAppearanceNameAqua, NSAppearanceNameDarkAqua]
        )
        return best == NSAppearanceNameDarkAqua
    except Exception:
        return False


def _as_color(value: Any) -> Any:
    """An NSColor from an NSColor or an ``(r, g, b[, a])`` tuple in 0-255 (a 0-1)."""
    if isinstance(value, (tuple, list)):
        r, g, b, *rest = value
        a = rest[0] if rest else 1.0
        return NSColor.colorWithSRGBRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, a)
    return value


def _dynamic(light: Any, dark: Any) -> Any:
    light_c, dark_c = _as_color(light), _as_color(dark)
    return NSColor.colorWithName_dynamicProvider_(
        None, lambda appearance: dark_c if _is_dark(appearance) else light_c
    )


_ROLES: dict[str, Callable[[], Any]] = {}
_HOVER: dict[str, Any] = {}
"""role -> hover colour (NSColor), role name, or absent (= unchanged)."""


def _install_system_roles() -> None:
    if not _APPKIT:
        return
    system = {
        "label": NSColor.labelColor,
        "quaternary": NSColor.quaternaryLabelColor,
        "accent": NSColor.controlAccentColor,
        # Bars: ok is systemBlue, NOT the accent (a red accent would read as crit).
        "ok": NSColor.systemBlueColor,
        "warn": NSColor.systemOrangeColor,
        "crit": NSColor.systemRedColor,
        "good": NSColor.systemGreenColor,
        "white": NSColor.whiteColor,
        "fill": NSColor.quaternarySystemFillColor,
        "sep": NSColor.separatorColor,
        "hl": NSColor.selectedContentBackgroundColor,
        "clear": NSColor.clearColor,
    }
    _ROLES.update(system)
    # Our views are NOT vibrant: over a wallpaper-tinted menu the alpha-thin
    # system greys (tertiaryLabel, tertiarySystemFill) wash out - captions went
    # near-invisible and a 0 % track read as a full white bar (design 4,
    # 2026-09-25). So the grey ramp and the track are our own, stronger values.
    # ``tertiary`` is for decoration TEXT (separators, dots) only, never
    # information. ``dead_fill`` is the faint bar fill of a dead reading (an
    # ended window, a disabled account): clearly quieter than a live fill,
    # still visible on the track.
    # Warn/crit/good TEXT stays a vivid hue in light mode (the old darkened
    # hues read brown on a pink tint) yet keeps >= 4.5:1 on white; the fills
    # keep the system hues.
    for name, light, dark in (
        ("secondary", (0, 0, 0, 0.62), (255, 255, 255, 0.68)),
        ("tertiary", (0, 0, 0, 0.45), (255, 255, 255, 0.45)),
        ("dead_fill", (0, 0, 0, 0.22), (255, 255, 255, 0.24)),
        ("track", (0, 0, 0, 0.10), (255, 255, 255, 0.14)),
        ("warn_text", (192, 86, 0), (255, 179, 64)),
        ("crit_text", (208, 40, 40), (255, 110, 100)),
        ("good_text", (20, 130, 54), (80, 220, 110)),
        ("chip", (0, 0, 0, 0.07), (255, 255, 255, 0.10)),
    ):
        color = _dynamic(light, dark)
        _ROLES[name] = (lambda c=color: c)
    # A tag pill's background: the user's accent, thin. Resolved inside the
    # provider so it follows the accent the user picked.
    pill = NSColor.colorWithName_dynamicProvider_(
        None, lambda _appearance: NSColor.controlAccentColor().colorWithAlphaComponent_(0.15))
    _ROLES["accent_pill"] = lambda c=pill: c
    # Its text: the accent, lifted toward white in dark mode so a blue tag on
    # its blue pill stays legible on a dark card.
    tag = NSColor.colorWithName_dynamicProvider_(
        None, lambda appearance: NSColor.controlAccentColor().blendedColorWithFraction_ofColor_(
            0.35, NSColor.whiteColor()) if _is_dark(appearance) else NSColor.controlAccentColor())
    _ROLES["accent_tag"] = lambda c=tag: c
    white = NSColor.whiteColor()
    for name in ("label", "accent", "accent_tag", "ok", "warn", "crit", "good", "white",
                 "warn_text", "crit_text", "good_text"):
        _HOVER[name] = white
    _HOVER["secondary"] = white.colorWithAlphaComponent_(0.85)
    _HOVER["tertiary"] = white.colorWithAlphaComponent_(0.7)
    _HOVER["dead_fill"] = white.colorWithAlphaComponent_(0.45)
    _HOVER["quaternary"] = white.colorWithAlphaComponent_(0.55)
    _HOVER["track"] = white.colorWithAlphaComponent_(0.28)
    _HOVER["fill"] = white.colorWithAlphaComponent_(0.18)
    _HOVER["chip"] = NSColor.clearColor()
    _HOVER["accent_pill"] = white.colorWithAlphaComponent_(0.22)


_install_system_roles()


def register_role(name: str, light: Any, dark: Any = None, *, hover: Any = None) -> None:
    """Add (or replace) a colour role, e.g. a theme's card fill.

    *light* / *dark* are NSColors or ``(r, g, b[, a])`` tuples (0-255, alpha
    0-1); with *dark* omitted *light* is used for both. *hover* is what the
    role becomes inside a highlighted block: a role name, an NSColor/tuple, or
    ``None`` to stay unchanged. No-op without AppKit.
    """
    if not _APPKIT:
        return
    color = _dynamic(light, light if dark is None else dark)
    _ROLES[name] = lambda c=color: c
    if hover is None:
        _HOVER.pop(name, None)
    elif isinstance(hover, str):
        _HOVER[name] = hover
    else:
        _HOVER[name] = _as_color(hover)


def role_names() -> tuple[str, ...]:
    return tuple(sorted(_ROLES))


def _role(name: str) -> Any:
    try:
        return _ROLES[name]()
    except KeyError:
        raise KeyError(f"unknown colour role {name!r}") from None


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_FONTS: dict[tuple, Any] = {}


def _make_font(size: float, weight: Any, *, mono: bool, code: bool, rounded: bool) -> Any:
    w = _weight(weight)
    if code:
        return NSFont.monospacedSystemFontOfSize_weight_(size, w)
    font = (NSFont.monospacedDigitSystemFontOfSize_weight_(size, w) if (mono or rounded)
            else NSFont.systemFontOfSize_weight_(size, w))
    if rounded:
        # The rounded design keeps the monospaced-digit feature of the base
        # descriptor (tests/test_card_base.py measures it); a system without
        # the rounded design keeps SF Pro rather than failing to draw.
        desc = font.fontDescriptor().fontDescriptorWithDesign_(NSFontDescriptorSystemDesignRounded)
        if desc is not None:
            font = NSFont.fontWithDescriptor_size_(desc, size) or font
    return font


# ---------------------------------------------------------------------------
# Pen
# ---------------------------------------------------------------------------


class Pen:
    """The drawing kit a block's ``paint`` receives.

    Coordinates are flipped (y down). Text is positioned by its BASELINE.
    ``hl`` is True while the block is highlighted: text roles turn white (see
    :func:`register_role` for the per-role hover colours) and bar fills too.
    """

    def __init__(self, hl: bool = False) -> None:
        self.hl = bool(hl)

    # -- colours ------------------------------------------------------------

    def color(self, role: Any) -> Any:
        """NSColor for *role* (a role name, an NSColor, or an rgb tuple)."""
        if isinstance(role, str):
            if self.hl and role in _HOVER:
                hover = _HOVER[role]
                return _role(hover) if isinstance(hover, str) else hover
            return _role(role)
        if self.hl:
            return NSColor.whiteColor()
        return _as_color(role)

    # -- fonts / text -------------------------------------------------------

    @staticmethod
    def font(size: float, weight: Any = "regular", *, mono: bool = False, code: bool = False,
             rounded: bool = False) -> Any:
        """SF Pro; ``mono`` = monospaced DIGITS (figures align); ``code`` = SF Mono;
        ``rounded`` = SF Pro Rounded (the percent figures), monospaced digits kept."""
        key = (float(size), str(weight), bool(mono), bool(code), bool(rounded))
        font = _FONTS.get(key)
        if font is None:
            font = _FONTS[key] = _make_font(size, weight, mono=mono, code=code, rounded=rounded)
        return font

    def _astr(self, s: str, font: Any, color: Any, kern: float | None = None) -> Any:
        attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: color}
        if kern is not None:
            attrs[NSKernAttributeName] = kern
        return NSAttributedString.alloc().initWithString_attributes_(s, attrs)

    def width(
        self,
        s: str,
        size: float,
        weight: Any = "regular",
        *,
        mono: bool = False,
        code: bool = False,
        kern: float | None = None,
        rounded: bool = False,
    ) -> float:
        if not s:
            return 0.0
        font = self.font(size, weight, mono=mono, code=code, rounded=rounded)
        return float(self._astr(s, font, NSColor.labelColor(), kern).size().width)

    def fit(
        self,
        s: str,
        size: float,
        weight: Any = "regular",
        maxw: float | None = None,
        *,
        mono: bool = False,
        code: bool = False,
        kern: float | None = None,
        rounded: bool = False,
    ) -> str:
        """*s* tail-truncated with an ellipsis to at most *maxw* points."""
        if maxw is None or not s:
            return s
        if self.width(s, size, weight, mono=mono, code=code, kern=kern, rounded=rounded) <= maxw:
            return s
        lo, hi = 0, len(s)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            cand = s[:mid].rstrip() + "…"
            if self.width(cand, size, weight, mono=mono, code=code, kern=kern, rounded=rounded) <= maxw:
                lo = mid
            else:
                hi = mid - 1
        return (s[:lo].rstrip() + "…") if lo else ("…" if maxw >= self.width("…", size, weight) else "")

    def text(
        self,
        s: str,
        x: float,
        baseline_y: float,
        size: float,
        weight: Any = "regular",
        role: Any = "label",
        *,
        mono: bool = False,
        right: bool = False,
        center: bool = False,
        maxw: float | None = None,
        code: bool = False,
        kern: float | None = None,
        rounded: bool = False,
    ) -> float:
        """Draw *s* with its baseline at *baseline_y*; return the drawn width.

        ``right`` aligns the right edge to *x*, ``center`` the centre;
        ``maxw`` truncates with an ellipsis; ``rounded`` = a percent figure.
        """
        if not s:
            return 0.0
        s = self.fit(s, size, weight, maxw, mono=mono, code=code, kern=kern, rounded=rounded)
        if not s:
            return 0.0
        font = self.font(size, weight, mono=mono, code=code, rounded=rounded)
        a = self._astr(s, font, self.color(role), kern)
        w = float(a.size().width)
        if right:
            x -= w
        elif center:
            x -= w / 2.0
        a.drawAtPoint_(NSMakePoint(x, baseline_y - font.ascender()))
        return w

    # -- shapes ---------------------------------------------------------------

    def rrect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        r: float,
        role: Any,
        *,
        stroke: Any = None,
        stroke_width: float = 0.5,
    ) -> None:
        """Filled rounded rect; *stroke* (a role) adds a hairline border."""
        if w <= 0 or h <= 0:
            return
        if role is not None:
            self.color(role).setFill()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, y, w, h), r, r
            ).fill()
        if stroke is not None:
            half = stroke_width / 2.0
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x + half, y + half, w - stroke_width, h - stroke_width), r, r
            )
            path.setLineWidth_(stroke_width)
            self.color(stroke).setStroke()
            path.stroke()

    def panel(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        r: float,
        role: Any,
        *,
        stroke: Any = None,
        stroke_width: float = 0.5,
        top: bool = True,
        bottom: bool = True,
    ) -> None:
        """A slice of a rounded card spread over several blocks.

        ``top`` / ``bottom`` round (and stroke) that end; a middle slice
        (both False) draws the fill and the two side edges only, so stacked
        slices read as one card.
        """
        if w <= 0 or h <= 0:
            return
        rt = min(r if top else 0.0, h / 2.0, w / 2.0)
        rb = min(r if bottom else 0.0, h / 2.0, w / 2.0)
        x0, y0, x1, y1 = x, y, x + w, y + h
        if role is not None:
            p = NSBezierPath.bezierPath()
            p.moveToPoint_(NSMakePoint(x0, y1 - rb))
            p.lineToPoint_(NSMakePoint(x0, y0 + rt))
            if rt:
                p.appendBezierPathWithArcFromPoint_toPoint_radius_(
                    NSMakePoint(x0, y0), NSMakePoint(x0 + rt, y0), rt)
            p.lineToPoint_(NSMakePoint(x1 - rt, y0))
            if rt:
                p.appendBezierPathWithArcFromPoint_toPoint_radius_(
                    NSMakePoint(x1, y0), NSMakePoint(x1, y0 + rt), rt)
            p.lineToPoint_(NSMakePoint(x1, y1 - rb))
            if rb:
                p.appendBezierPathWithArcFromPoint_toPoint_radius_(
                    NSMakePoint(x1, y1), NSMakePoint(x1 - rb, y1), rb)
            p.lineToPoint_(NSMakePoint(x0 + rb, y1))
            if rb:
                p.appendBezierPathWithArcFromPoint_toPoint_radius_(
                    NSMakePoint(x0, y1), NSMakePoint(x0, y1 - rb), rb)
            p.closePath()
            self.color(role).setFill()
            p.fill()
        if stroke is None:
            return
        i = stroke_width / 2.0
        sx0, sx1 = x0 + i, x1 - i
        sy0 = y0 + i if top else y0
        sy1 = y1 - i if bottom else y1
        srt = max(0.0, rt - i) if top else 0.0
        srb = max(0.0, rb - i) if bottom else 0.0
        p = NSBezierPath.bezierPath()
        # Left edge (bottom -> top), top edge, right edge (top -> bottom), then
        # the bottom edge back to the start. Open ends are simply not drawn.
        p.moveToPoint_(NSMakePoint(sx0, sy1 - srb))
        p.lineToPoint_(NSMakePoint(sx0, sy0 + srt))
        if top:
            if srt:
                p.appendBezierPathWithArcFromPoint_toPoint_radius_(
                    NSMakePoint(sx0, sy0), NSMakePoint(sx0 + srt, sy0), srt)
            p.lineToPoint_(NSMakePoint(sx1 - srt, sy0))
            if srt:
                p.appendBezierPathWithArcFromPoint_toPoint_radius_(
                    NSMakePoint(sx1, sy0), NSMakePoint(sx1, sy0 + srt), srt)
        else:
            p.moveToPoint_(NSMakePoint(sx1, sy0))
        p.lineToPoint_(NSMakePoint(sx1, sy1 - srb))
        if bottom:
            if srb:
                p.appendBezierPathWithArcFromPoint_toPoint_radius_(
                    NSMakePoint(sx1, sy1), NSMakePoint(sx1 - srb, sy1), srb)
            p.lineToPoint_(NSMakePoint(sx0 + srb, sy1))
            if srb:
                p.appendBezierPathWithArcFromPoint_toPoint_radius_(
                    NSMakePoint(sx0, sy1), NSMakePoint(sx0, sy1 - srb), srb)
        p.setLineWidth_(stroke_width)
        self.color(stroke).setStroke()
        p.stroke()

    def bar(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        pct: float | None,
        role: Any = "ok",
        track: Any = "track",
    ) -> None:
        """A rounded gauge: quiet *track*, then a fill of *pct* percent.

        ``None`` (or <= 0) draws the track only - an absent reading is never a
        zero-width fill pretending to be 0 %. A fill never falls below one
        cap (``h``) wide and never passes the track.
        """
        if w <= 0 or h <= 0:
            return
        if track is not None:
            self.rrect(x, y, w, h, h / 2.0, track)
        if pct is None:
            return
        p = max(0.0, min(100.0, float(pct)))
        if p <= 0:
            return
        fw = min(w, max(h, w * p / 100.0))
        self.rrect(x, y, fw, h, h / 2.0, role)

    def circle(self, x: float, y: float, d: float, role: Any) -> None:
        """Filled circle whose bounding box's top-left is (*x*, *y*)."""
        self.color(role).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, y, d, d)).fill()

    def line(self, x1: float, y: float, x2: float, role: Any = "sep", *, thickness: float = 1.0) -> None:
        """Horizontal rule from *x1* to *x2* whose top edge is at *y*."""
        if x2 <= x1:
            return
        self.color(role).setFill()
        NSBezierPath.fillRect_(NSMakeRect(x1, y, x2 - x1, thickness))

    def symbol(
        self,
        name: str,
        x: float,
        y: float,
        size: float,
        role: Any = "secondary",
        weight: Any = "regular",
        box: float | None = None,
        *,
        anchor: str = "topleft",
    ) -> float:
        """Draw SF Symbol *name*; return its width (0 when the name is unknown).

        ``anchor``: ``"topleft"`` - (x, y) is the top-left of the symbol, or of
        a ``box`` x ``box`` square it is centred in; ``"center"`` - (x, y) is
        its centre; ``"leftmid"`` - x is its left edge, y its vertical centre.
        A filled badge (``exclamationmark.*.fill``, ``xmark.*.fill``,
        ``arrow.*.fill``) draws its glyph white on the colour, like Apple's own
        warnings; everything else is one colour.
        """
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if img is None:
            return 0.0
        col = self.color(role)
        cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, _weight(weight))
        two_layer = name.endswith(".fill") and name.split(".")[0] in ("exclamationmark", "xmark", "arrow")
        colors = [NSColor.whiteColor(), col] if two_layer and not self.hl else [col]
        cfg = cfg.configurationByApplyingConfiguration_(
            NSImageSymbolConfiguration.configurationWithPaletteColors_(colors)
        )
        img = img.imageWithSymbolConfiguration_(cfg)
        sz = img.size()
        if anchor == "center":
            ox, oy = x - sz.width / 2.0, y - sz.height / 2.0
        elif anchor == "leftmid":
            ox, oy = x, y - sz.height / 2.0
        elif box:
            ox, oy = x + (box - sz.width) / 2.0, y + (box - sz.height) / 2.0
        else:
            ox, oy = x, y
        img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            NSMakeRect(ox, oy, sz.width, sz.height), NSZeroRect,
            NSCompositingOperationSourceOver, 1.0, True, None,
        )
        return float(sz.width)

    def chip(
        self,
        text: str,
        x: float,
        baseline_y: float,
        size: float = 10.0,
        *,
        role: Any = "secondary",
        bg: Any = "chip",
        maxw: float | None = None,
        weight: Any = "regular",
    ) -> float:
        """A monospace command chip (``cswap add``): SF Mono on a quiet rounded
        background (dropped while highlighted). Returns the chip's width."""
        if not text:
            return 0.0
        pad = 3.0
        inner = None if maxw is None else max(0.0, maxw - 2 * pad)
        shown = self.fit(text, size, weight, inner, code=True)
        tw = self.width(shown, size, weight, code=True)
        font = self.font(size, weight, code=True)
        w = tw + 2 * pad
        if bg is not None and not self.hl:
            self.rrect(
                x, baseline_y - font.ascender() - 1.5, w,
                font.ascender() - font.descender() + 3.0, 3.0, bg,
            )
        self.text(shown, x + pad, baseline_y, size, weight, role, code=True)
        return w

    def pill(
        self,
        text: str,
        x: float,
        baseline_y: float,
        size: float = 10.0,
        *,
        role: Any = "accent_tag",
        bg: Any = "accent_pill",
        weight: Any = "semibold",
    ) -> float:
        """A tag pill (``next``, ``best``): *text* in *role* on a fully rounded
        *bg* (dropped while highlighted). Returns the pill's width."""
        if not text:
            return 0.0
        pad = 5.0
        font = self.font(size, weight)
        tw = self.width(text, size, weight)
        w = tw + 2 * pad
        h = font.ascender() - font.descender() + 2.0
        if bg is not None and not self.hl:
            self.rrect(x, baseline_y - font.ascender() - 1.0, w, h, h / 2.0, bg)
        self.text(text, x + pad, baseline_y, size, weight, role)
        return w


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

_DRAW_ERRORS: set[str] = set()


def _log_once(key: str, message: str) -> None:
    if key in _DRAW_ERRORS:
        return
    _DRAW_ERRORS.add(key)
    import sys
    import time

    sys.stderr.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] cc-usage-widget: {message}\n")
    sys.stderr.flush()


if _APPKIT:

    class BlockView(NSView):
        """One block's custom menu-item view.

        Flipped; draws NO background in the real menu (the material shows
        through). A selectable block draws the native highlight while its menu
        item is highlighted, and a click runs the action once and closes the
        menu. ``force_hover`` exists for offscreen renders only.
        """

        def initWithFrame_(self, frame: Any) -> Any:
            self = objc.super(BlockView, self).initWithFrame_(frame)
            if self is None:
                return None
            self.block = None
            self.force_hover = False
            return self

        @classmethod
        def viewForBlock_width_(cls, block: Block, width: float) -> Any:
            view = cls.alloc().initWithFrame_(NSMakeRect(0, 0, width, block.height))
            view.block = block
            if block.tooltip:
                view.setToolTip_(block.tooltip)
            # A spacer or rule has nothing to say: hidden from VoiceOver, so
            # it does not stop on blank elements between the cards.
            view.setAccessibilityElement_(bool(block.label))
            view.setAccessibilityRole_(
                NSAccessibilityButtonRole if block.selectable else NSAccessibilityStaticTextRole
            )
            if block.label:
                view.setAccessibilityLabel_(block.label)
            return view

        def isFlipped(self) -> bool:
            return True

        def acceptsFirstMouse_(self, _event: Any) -> bool:
            return True

        def isHighlightedNow(self) -> bool:
            block = self.block
            if block is None or not block.selectable:
                return False
            if self.force_hover:
                return True
            item = self.enclosingMenuItem()
            return bool(item is not None and item.isHighlighted())

        def drawRect_(self, _rect: Any) -> None:
            block = self.block
            if block is None:
                return
            b = self.bounds()
            hl = self.isHighlightedNow()
            try:
                if hl:
                    Pen().rrect(block.hl_inset, block.hl_vinset, b.size.width - 2 * block.hl_inset,
                                b.size.height - 2 * block.hl_vinset, block.hl_radius, "hl")
                if block.paint is not None:
                    block.paint(Pen(hl), b.size.width, b.size.height)
            except Exception as exc:  # never raise out of drawRect_
                _log_once(f"draw:{block.key}:{type(exc).__name__}",
                          f"card view {block.key or '?'} failed to draw: {exc!r}")

        def performBlockAction(self) -> bool:
            block = self.block
            if block is None or block.action is None:
                return False
            try:
                block.action()
            except Exception as exc:
                _log_once(f"action:{block.key}:{type(exc).__name__}",
                          f"card action {block.key or '?'} failed: {exc!r}")
            return True

        def performAndClose(self) -> bool:
            """Run the action once, then close the menu - as a click does."""
            if not self.performBlockAction():
                return False
            item = self.enclosingMenuItem()
            menu = item.menu() if item is not None else None
            if menu is not None:
                menu.cancelTracking()
            return True

        def mouseUp_(self, _event: Any) -> None:
            self.performAndClose()

        def accessibilityPerformPress(self) -> bool:
            return self.performAndClose()

    class _MaterialView(NSView):
        """The SIMULATED menu backdrop for offscreen composites (never shipped)."""

        def isFlipped(self) -> bool:
            return True

        stops: Any = None  # [(r, g, b), ...] top -> bottom; None = the plain grey

        def drawRect_(self, _rect: Any) -> None:
            b = self.bounds()
            dark = _is_dark(self.effectiveAppearance())
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(0.5, 0.5, b.size.width - 1, b.size.height - 1), 10, 10
            )
            stops = list(self.stops or ()) or [(38, 38, 40) if dark else (236, 236, 236)]
            if len(stops) == 1:
                _as_color(tuple(stops[0])).setFill()
                path.fill()
            else:  # a wallpaper-tinted material: a vertical gradient (flipped view: top first)
                NSGradient.alloc().initWithColors_([_as_color(tuple(c)) for c in stops]).drawInBezierPath_angle_(
                    path, 90.0)
            (NSColor.colorWithWhite_alpha_(1, 0.16) if dark else NSColor.colorWithWhite_alpha_(0, 0.14)).setStroke()
            path.setLineWidth_(1)
            path.stroke()

else:  # pragma: no cover - no AppKit

    class BlockView:  # type: ignore[no-redef]
        """Placeholder so ``card_base.BlockView`` always resolves."""

        @classmethod
        def viewForBlock_width_(cls, block: Block, width: float) -> Any:
            raise RuntimeError("AppKit is not available")


# ---------------------------------------------------------------------------
# Menu items
# ---------------------------------------------------------------------------


def menu_items_for(blocks: Sequence[Block], *, width: float = W) -> list[Any]:
    """One ``rumps.MenuItem`` per block, each carrying a :class:`BlockView`.

    A selectable block's item gets a callback that runs the same action, so
    Return on a keyboard-highlighted row does what a click does (a click is
    handled by the view itself and never reaches the item). The plain title is
    never drawn; it keys the item in rumps and is the label SHORTENED to
    :data:`MENU_TITLE_W` - AppKit sizes the menu to its items' titles
    even when a view replaces them, and the full labels (``Switch to spare;
    5h 5%; 7d 51% ↺ 14:00; …``) widened the menu to 423 pt past the 360 pt
    cards (review appkit-1). VoiceOver reads the view's own full label.
    Raises when AppKit is missing: the caller falls back to a text layout.
    """
    if not _APPKIT:
        raise RuntimeError("AppKit is not available")
    import rumps

    items: list[Any] = []
    for block in blocks:
        callback = None
        if block.action is not None:
            def callback(_sender: Any, _action: Callable[[], Any] = block.action) -> None:
                _action()
        item = rumps.MenuItem(fit_menu_title(block.label or " "), callback=callback)
        view = BlockView.viewForBlock_width_(block, width)
        item._menuitem.setView_(view)
        if block.tooltip:
            item._menuitem.setToolTip_(block.tooltip)
        items.append(item)
    return items


def fit_menu_title(text: str, maxw: float = MENU_TITLE_W) -> str:
    """*text* tail-truncated with an ellipsis to *maxw* points in the menu
    font, so a native or hidden item title cannot widen the menu past the
    cards. Without AppKit it caps at 32 characters."""
    if not _APPKIT:
        return text if len(text) <= 32 else text[:31].rstrip() + "…"
    font = NSFont.menuFontOfSize_(0)

    def width(s: str) -> float:
        return float(NSAttributedString.alloc().initWithString_attributes_(
            s, {NSFontAttributeName: font}).size().width)

    if width(text) <= maxw:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if width(text[:mid].rstrip() + "…") <= maxw:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def apply_native_title(menu_item: Any, segments: Iterable[tuple[str, str]]) -> bool:
    """Give a NATIVE item an attributed title in the menu font, run by run.

    ``segments`` are ``(text, role)``; e.g. ``[("Recent switches", "label"),
    ("  last 09:56", "secondary")]``. The plain title stays what it was (it is
    what rumps keys on and what VoiceOver falls back to). ``False`` without
    AppKit or on any failure.
    """
    if not _APPKIT:
        return False
    native = getattr(menu_item, "_menuitem", None)
    if native is None:
        return False
    try:
        font = NSFont.menuFontOfSize_(0)
        out = NSMutableAttributedString.alloc().init()
        for text, role in segments:
            if not text:
                continue
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    text, {NSFontAttributeName: font, NSForegroundColorAttributeName: _role(role)}
                )
            )
        native.setAttributedTitle_(out)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Offscreen composite
# ---------------------------------------------------------------------------


def _tail_painter(row: Any) -> Callable[[Pen, float, float], None]:
    """Paint one simulated NATIVE row: ``{title, detail, check, submenu, key_eq}``."""

    def paint(pen: Pen, w: float, h: float) -> None:
        font = NSFont.menuFontOfSize_(0)
        size = float(font.pointSize())
        base = 16.0
        if row.get("check"):
            pen.symbol("checkmark", 3, 3, 11, "label", "semibold", box=16)
        tw = pen.text(row.get("title", ""), LEAD, base, size, "regular", "label")
        if row.get("detail"):
            pen.text(row["detail"], LEAD + tw + 6, base, size, "regular", "secondary")
        if row.get("submenu"):
            pen.symbol("chevron.right", RIGHT - 9, 4, 11, "secondary", "semibold", box=14)
        if row.get("key_eq"):
            pen.text(row["key_eq"], RIGHT, base, size, "regular", "tertiary", right=True)

    return paint


def _tail_blocks(tail: Sequence[Any] | None, width: float) -> list[Block]:
    out: list[Block] = []
    for row in tail or ():
        if row is None:
            def sep(pen: Pen, w: float, h: float) -> None:
                pen.line(LEAD - 6, 5, w - (TRAIL - 4), "sep")
            out.append(Block(TAIL_SEP_H, sep, key="tail-sep"))
        else:
            out.append(Block(TAIL_ROW_H, _tail_painter(row), ax=row.get("title", ""),
                             key=f"tail-{row.get('title', '')}"))
    return out


def render_png(
    blocks: Sequence[Block],
    path: Any,
    *,
    dark: bool,
    width: float = W,
    hover_key: str | None = None,
    tail: Sequence[Any] | None = None,
    material: Sequence[Sequence[float]] | None = None,
) -> tuple[int, int]:
    """Composite *blocks* (+ the simulated native *tail*) over a simulated menu
    material and write a 2x PNG to *path*. Returns ``(pixels_wide, pixels_high)``.

    ``hover_key`` draws that block highlighted, exactly as a hovered menu item
    would (only when it is selectable). ``tail`` rows are dicts with ``title``
    and optional ``detail``/``check``/``submenu``/``key_eq``, ``None`` for a
    separator (see ``themes.tail_rows``). ``material`` simulates a
    wallpaper-tinted menu: ``(r, g, b)`` stops (0-255) painted top to bottom
    (one stop = a flat colour); ``None`` = the plain grey. Offscreen only:
    builds views, never a window.
    """
    if not _APPKIT:
        raise RuntimeError("AppKit is not available")
    rows = list(tail or ())
    if rows and blocks and rows[0] is not None:
        rows = [None] + rows  # the separator the app puts between blocks and tail
    every = list(blocks) + _tail_blocks(rows, width)
    total = sum(b.height for b in every) + 2 * MATERIAL_PAD
    container = _MaterialView.alloc().initWithFrame_(NSMakeRect(0, 0, width, total))
    container.stops = [tuple(c) for c in material] if material else None
    y = MATERIAL_PAD
    for block in every:
        view = BlockView.viewForBlock_width_(block, width)
        view.setFrame_(NSMakeRect(0, y, width, block.height))
        view.force_hover = bool(hover_key) and block.key == hover_key
        container.addSubview_(view)
        y += block.height
    appearance = NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua if dark else NSAppearanceNameAqua)
    container.setAppearance_(appearance)
    rect = container.bounds()
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, int(width * 2), int(round(total * 2)), 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
    )
    rep.setSize_((width, total))

    def draw() -> None:
        container.cacheDisplayInRect_toBitmapImageRep_(rect, rep)

    appearance.performAsCurrentDrawingAppearance_(draw)
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    if not data.writeToFile_atomically_(str(path), True):
        raise OSError(f"could not write {path}")
    return int(rep.pixelsWide()), int(rep.pixelsHigh())
