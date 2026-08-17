"""Menu-bar icon + rich attributed menu rendering.

Two jobs, both deliberately cheap (SPEC 2.1):

1. :func:`status_icon` — an SF Symbol **template** image for the status item, so
   the menu bar shows a real icon that follows light/dark automatically instead
   of a text glyph. Costs one NSImage at startup.
2. :func:`account_block` / :func:`apply_attributed` — per-account multi-line
   rows with Unicode block bars and severity colour, mirroring ``cswap watch``.

A second vendor (SPEC-CODEX) adds **no** second bar renderer: a Codex quota
block is the same :func:`window_line` under a :func:`quota_header` instead of an
:func:`account_header`, so the two sections cannot drift apart in bar geometry,
severity thresholds or the ``(!)`` marker. The only genuinely new thing here is
:func:`window_minutes_label`, which turns a reported window *width* into its
name — ``10080`` -> ``weekly`` — so no caller has to hardcode the word.

Why this is not heavy: every figure rendered here is **already computed** by the
worker. This module does string formatting and builds one NSAttributedString per
account per repaint (3 accounts, twice a minute) — microseconds, no I/O, no
allocation that outlives the menu. Bars are plain text, not views, so there is
no per-frame drawing and nothing to invalidate.

Everything degrades: if pyobjc/AppKit is unavailable or a symbol is missing, the
caller falls back to the plain-text labels it already had.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# Bars are drawn with block glyphs so they align in any monospaced font and
# cost nothing to render. Eighth-blocks give sub-cell resolution.
_FULL = "█"  # █
_EMPTY = "░"  # ░
_PARTIALS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")

BAR_WIDTH = 18

# Severity thresholds. Matches the palette in `cswap watch`: green while there
# is room, amber when it is getting close, red at the wall.
WARN_PCT = 70.0
CRIT_PCT = 90.0


def bar(pct: float | None, width: int = BAR_WIDTH) -> str:
    """``████████░░░░░░░░`` for *pct* (0-100). ``None`` renders as all-empty.

    Sub-cell precision via eighth-block glyphs, so 3% on an 18-cell bar is a
    visible sliver rather than nothing.
    """
    if pct is None:
        return _EMPTY * width
    clamped = max(0.0, min(100.0, float(pct)))
    eighths = int(round(clamped / 100.0 * width * 8))
    full, rem = divmod(eighths, 8)
    full = min(full, width)
    out = _FULL * full
    if rem and full < width:
        out += _PARTIALS[rem]
    return out.ljust(width, _EMPTY)


def severity(pct: float | None) -> str:
    """``"ok" | "warn" | "crit"`` — the colour bucket for *pct*."""
    if pct is None:
        return "ok"
    if pct >= CRIT_PCT:
        return "crit"
    if pct >= WARN_PCT:
        return "warn"
    return "ok"


# Window widths that have a name rather than a number. Anything else is
# rendered as a duration, so an unfamiliar width is still legible.
_MINUTES_PER_HOUR = 60
_MINUTES_PER_DAY = 1_440
_MINUTES_PER_WEEK = 10_080
_NAMED_WINDOWS = {
    _MINUTES_PER_HOUR: "hourly",
    _MINUTES_PER_DAY: "daily",
    _MINUTES_PER_WEEK: "weekly",
}


def window_minutes_label(minutes: float | int | None) -> str:
    """Name a quota window from its **width**: ``10080`` -> ``"weekly"``.

    The Codex quota record reports ``rate_limits.primary.window_minutes``, and
    the menu must say what that window *is*. Deriving the word here rather than
    writing ``"weekly"`` at the call site is the difference between a widget
    that keeps telling the truth when OpenAI ships a second window and one that
    silently mislabels a 5-hour bar as a week (SPEC-CODEX 1, and the
    ``CODEX_WINDOW_MINUTES_WEEKLY`` note in ``contracts``).

    ``60`` / ``1440`` / ``10080`` get their names; everything else gets a
    compact duration (``300`` -> ``"5h"``, ``20160`` -> ``"2w"``, ``90`` ->
    ``"90m"``). ``None`` or a non-positive width yields ``""`` — an unreported
    window has no name and the caller must not invent one.
    """
    try:
        total = int(minutes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    named = _NAMED_WINDOWS.get(total)
    if named is not None:
        return named
    for size, suffix in (
        (_MINUTES_PER_WEEK, "w"),
        (_MINUTES_PER_DAY, "d"),
        (_MINUTES_PER_HOUR, "h"),
    ):
        if total % size == 0:
            return f"{total // size}{suffix}"
    return f"{total}m"


# --------------------------------------------------------------------------
# AppKit-dependent parts. Imported lazily and never fatal.
# --------------------------------------------------------------------------


def _appkit() -> Any | None:
    try:
        import AppKit  # noqa: PLC0415
    except Exception:
        return None
    return AppKit


def status_icon(symbols: Sequence[str] = ()) -> Any | None:
    """A template NSImage for the status item, or ``None`` to keep text.

    Tries each SF Symbol in turn — symbol availability is macOS-version
    dependent, so the first that resolves wins. ``setTemplate_(True)`` is what
    makes it invert correctly on a light vs dark menu bar.
    """
    AppKit = _appkit()
    if AppKit is None:
        return None
    # Order matters: EVERY one of these resolves on macOS 15+, so the first
    # entry is what ships. Chosen for legibility at 17 pt in the menu bar —
    # `gauge.with.dots.needle.33percent` resolves fine but renders as an
    # unrecognisable box-with-a-line at that size. Three ascending bars read
    # instantly and match what the dropdown shows.
    candidates = tuple(symbols) or (
        "chart.bar.fill",
        "chart.bar",
        "speedometer",
        "gauge",
        "bolt.horizontal.fill",
    )
    getter = getattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_", None)
    if getter is None:  # pre-11.0; no SF Symbols
        return None
    for name in candidates:
        try:
            image = getter(name, "Claude usage")
        except Exception:
            image = None
        if image is not None:
            try:
                image.setTemplate_(True)
                image.setSize_(AppKit.NSMakeSize(17.0, 17.0))
            except Exception:
                pass
            return image
    return None


def _color(kind: str) -> Any | None:
    AppKit = _appkit()
    if AppKit is None:
        return None
    try:
        if kind == "crit":
            return AppKit.NSColor.systemRedColor()
        if kind == "warn":
            return AppKit.NSColor.systemOrangeColor()
        if kind == "dim":
            return AppKit.NSColor.secondaryLabelColor()
        if kind == "accent":
            return AppKit.NSColor.controlAccentColor()
        return AppKit.NSColor.systemGreenColor()
    except Exception:
        return None


def _mono_font(size: float = 12.0, bold: bool = False) -> Any | None:
    AppKit = _appkit()
    if AppKit is None:
        return None
    try:
        weight = AppKit.NSFontWeightBold if bold else AppKit.NSFontWeightRegular
        return AppKit.NSFont.monospacedSystemFontOfSize_weight_(size, weight)
    except Exception:
        try:
            return AppKit.NSFont.userFixedPitchFontOfSize_(size)
        except Exception:
            return None


def attributed(segments: Iterable[tuple[str, str | None]]) -> Any | None:
    """Build one NSAttributedString from ``(text, colour_kind)`` segments.

    A monospaced font is applied to the whole string so columns line up. Returns
    ``None`` when AppKit is unavailable, so callers keep their plain label.
    """
    AppKit = _appkit()
    if AppKit is None:
        return None
    try:
        out = AppKit.NSMutableAttributedString.alloc().init()
        font = _mono_font()
        # A little leading stops a 4-line block reading as a wall of text; the
        # head indent keeps wrapped/continuation lines under the first column.
        para = None
        try:
            para = AppKit.NSMutableParagraphStyle.alloc().init()
            para.setLineSpacing_(2.0)
            para.setParagraphSpacing_(3.0)
        except Exception:
            para = None
        for text, kind in segments:
            if not text:
                continue
            attrs: dict[Any, Any] = {}
            if font is not None:
                attrs[AppKit.NSFontAttributeName] = font
            if para is not None:
                attrs[AppKit.NSParagraphStyleAttributeName] = para
            col = _color(kind) if kind else None
            if col is not None:
                attrs[AppKit.NSForegroundColorAttributeName] = col
            out.appendAttributedString_(
                AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            )
        return out
    except Exception:
        return None


def apply_attributed(menu_item: Any, segments: Iterable[tuple[str, str | None]]) -> bool:
    """Set an attributed title on a ``rumps.MenuItem``. ``True`` if it took.

    Reaches the underlying NSMenuItem through rumps' ``_menuitem``; if that
    attribute ever goes away the plain title already set on the item stands.
    """
    string = attributed(segments)
    if string is None:
        return False
    native = getattr(menu_item, "_menuitem", None)
    if native is None:
        return False
    try:
        native.setAttributedTitle_(string)
        return True
    except Exception:
        return False


def window_line(
    label: str,
    pct: float | None,
    note: str = "",
    label_width: int = 5,
    note_column: int = 0,
    ahead: bool | None = None,
) -> list[tuple[str, str | None]]:
    """One window row: ``  5h    ████░░░░   27%  resets 54m``.

    The bar and the percentage carry the severity colour; the label and the
    trailing note stay dimmed so the eye lands on the number. An exhausted
    window gets a ``(!)`` so it reads at a glance even in greyscale — colour
    alone is not an accessible signal.

    *label_width* and *note_column* are passed in by the caller so every
    account lines up on one vertical edge, rather than each block aligning only
    with itself.
    """
    kind = severity(pct)
    pct_text = "  --" if pct is None else f"{pct:>3.0f}%"
    marker = "  (!)" if kind == "crit" else ""
    segs: list[tuple[str, str | None]] = [
        (f"   {label:<{label_width}} ", "dim"),
        (bar(pct), kind),
        (f" {pct_text}", kind),
    ]
    if marker:
        segs.append((marker, "crit"))
    if note:
        # Pad so notes start in the same column on every row, with or without
        # a (!) marker ahead of them.
        pad = max(2, note_column - len(marker))
        segs.append((" " * pad + note, "dim"))
    if ahead is True:
        # Amber, not dim: burning faster than the window refills is the one
        # thing on this row that predicts a future problem.
        segs.append(("  (ahead of pace)", "warn"))
    return segs


def account_header(
    slot: int | str,
    name: str,
    email: str,
    is_active: bool,
    age_note: str = "",
) -> list[tuple[str, str | None]]:
    """``1  main (jane@work.com)   · active``"""
    segs: list[tuple[str, str | None]] = [
        (f"{slot}  ", "dim"),
        (name, "accent" if is_active else None),
        (f" ({email})", "dim"),
    ]
    if is_active:
        segs.append(("   ● active", "accent"))
    elif age_note:
        segs.append((f"   · {age_note}", "dim"))
    return segs


def quota_header(label: str, plan: str = "", note: str = "") -> list[tuple[str, str | None]]:
    """``Codex (pro)`` — the heading of a **read-only** quota block.

    Deliberately unlike :func:`account_header`: no slot number, no email, and
    never the ``accent``/``● active`` treatment. A pseudo-account is not an
    account you can switch to (SPEC-CODEX 4), and the one visual promise this
    menu makes is that accent means "this is the account you are on". The block
    below it is drawn with the same :func:`window_line`, so the bars, the
    severity colours and the ``(!)`` marker are shared, not re-implemented.
    """
    segs: list[tuple[str, str | None]] = [(label, None)]
    if plan:
        segs.append((f" ({plan})", "dim"))
    if note:
        segs.append((f"   · {note}", "dim"))
    return segs
