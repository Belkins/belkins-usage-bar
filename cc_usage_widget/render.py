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

import datetime as dt
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


TITLE_RESET_GLYPH = "↺"
"""Marks a countdown to a reset in the menu bar. One glyph, no word: the bar
has no room for "resets in", and the arrow is the shape the account rows
already use for the same fact."""

TITLE_RESET_SUFFIX_MAX = 4
"""Hard ceiling on the width :func:`title_reset_suffix` may add to the title.

The title measures 187 pt today and every component competes for the same bar.
Four characters is the whole budget for "and when does it reopen", which is why
the duration below is ONE unit and the glyph carries no space."""


COMPACT_TITLE_SEPARATOR = "\u00b7"
"""Joins the vendor initials in the compact title: ``V\u00b7C``.

A middle dot, not a slash: the slash already separates the two *figures*, and
one glyph doing both jobs made ``V/C 100/100`` read as four numbers."""

COMPACT_TITLE_MAX = 12
"""Hard ceiling on :func:`compact_title`'s output (roadmap 16).

The full title runs to five components and 187 pt; compact exists for a menu
bar that has no room for it. Worst case here is ``V\u00b7C 100/100`` \u2014 11
characters \u2014 and the budget is checked by a test rather than trusted,
because every previous title widening was also "just two more characters"."""


def compact_title(figures: Sequence[tuple[str, str]]) -> str:
    """``"V\u00b7C 100/100"`` \u2014 initials, then their figures, in one order.

    *figures* is ``(initial, figure)`` per vendor, already formatted by the
    caller: this module has no opinion about rounding, and reusing the caller's
    ``format_pct`` is what keeps the compact title from disagreeing with the
    menu about 99.5%.

    The initials come first as a group so the reader learns the column order
    once (``V\u00b7C`` \u2192 Claude then Codex) and then reads two bare
    numbers, instead of scanning ``V100 C100`` for where one figure ends and
    the next letter begins. One vendor renders ``"V 100"``; none renders ``""``,
    and the caller decides what an empty title means (an icon-only status item,
    per ``render_title``).

    A *figure* may be any short string, not only a percentage \u2014 ``"\u26a0"``
    is what the caller passes when a standing verdict has REPLACED the number
    (SPEC 4.3): the compact title must never be the one surface that keeps
    showing a percentage for an account that cannot report one.
    """
    pairs = [(initial, figure) for initial, figure in figures if initial and figure]
    if not pairs:
        return ""
    initials = COMPACT_TITLE_SEPARATOR.join(initial for initial, _figure in pairs)
    values = "/".join(figure for _initial, figure in pairs)
    return f"{initials} {values}"


def coarse_duration(seconds: float) -> str:
    """``"4d"`` / ``"18h"`` / ``"35m"`` — one unit, at most three characters.

    The menu-bar twin of the two-unit countdown the menu rows use (``"1d 4h"``,
    ``codex_accounts._format_duration``), which can afford a second unit
    because a menu row is wider than a status item. Rounding is DOWN, so a
    countdown never claims more time than was reported; ``"<1m"`` is the floor
    and ``"99d"`` the ceiling — no rate-limit window is remotely that wide, so
    the clamp exists only to stop a junk epoch widening the bar.
    """
    total = int(max(0.0, seconds))
    if total >= 86_400:
        return f"{min(99, total // 86_400)}d"
    if total >= 3_600:
        return f"{total // 3_600}h"
    if total >= 60:
        return f"{total // 60}m"
    return "<1m"


def title_reset_suffix(reset_at: float | None, now: float) -> str:
    """``"↺4d"`` for the menu bar, or ``""`` when there is no reported reset.

    Never longer than :data:`TITLE_RESET_SUFFIX_MAX`, and never a lone glyph:
    an arrow with no duration would say "something resets" without saying when,
    which is decoration rather than information (SPEC 4.3). A reset already in
    the past yields ``""`` too — the caller's window is then overdue, and the
    menu row is where that is explained.
    """
    if reset_at is None:
        return ""
    remaining = reset_at - now
    if remaining <= 0:
        return ""
    suffix = f"{TITLE_RESET_GLYPH}{coarse_duration(remaining)}"
    return suffix if len(suffix) <= TITLE_RESET_SUFFIX_MAX else ""


def fleet_reset_label(reset_at: float | None, now: float) -> str:
    """``"09:00"`` today, ``"Sat 09:00"`` inside the week, ``"Sep 15 09:00"``
    beyond it — the fleet line's "when does the next room open".

    A weekday is the right unit for a weekly window: "Sat 09:00" is how a
    person plans around a reset four days out, while a bare clock would read as
    today and a full date is noise inside the same week. Past seven days the
    date returns, because "Sat" would then name the wrong Saturday. ``None`` —
    a window whose reset the source did not report — renders ``""``; the caller
    must not substitute a guess.

    A reset **already in the past** renders ``""`` too, exactly as
    :func:`title_reset_suffix` does. A capped row whose epoch has passed and
    whose source has not yet re-read is a window that is overdue, not one that
    opens on Saturday: printing the stale instant would put "next Sat 09:00"
    over a Saturday that has been and gone, which is the one thing a "when does
    the next room open" line must never say (SPEC 4.3 — no invented number, and
    a stale one is invented by omission).
    """
    if reset_at is None:
        return ""
    if reset_at <= now:
        return ""
    try:
        when = dt.datetime.fromtimestamp(reset_at)
        today = dt.datetime.fromtimestamp(now).date()
    except (OSError, OverflowError, ValueError):  # pragma: no cover - absurd epoch
        return ""
    days = (when.date() - today).days
    if days == 0:
        return when.strftime("%H:%M")
    if 0 < days < 7:
        return f"{when:%a} {when:%H:%M}"
    return f"{when:%b} {when.day} {when:%H:%M}"


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
    expired: bool = False,
) -> list[tuple[str, str | None]]:
    """One window row: ``  5h    ████░░░░   27%  resets 54m``.

    The bar and the percentage carry the severity colour; the label and the
    trailing note stay dimmed so the eye lands on the number. An exhausted
    window gets a ``(!)`` so it reads at a glance even in greyscale — colour
    alone is not an accessible signal.

    *expired* means the window's reset instant has already passed
    (``AccountRow.expired_windows``): the figure describes a window that has
    ENDED, so the whole line renders dimmed and the live ``(!)``/severity
    treatment is withheld — a dead window must not shout "you are capped now"
    (its note carries the ``resets overdue (…)`` explanation instead).

    *label_width* and *note_column* are passed in by the caller so every
    account lines up on one vertical edge, rather than each block aligning only
    with itself.
    """
    kind = "dim" if expired else severity(pct)
    # "100%" is reserved for pct >= 100 (same boundary-honesty rule as
    # contracts.format_pct): the engine's at-limit escape can land on a
    # 99.x% account, and a rounded-up 100% here contradicted that switch.
    if pct is None:
        pct_text = "  --"
    elif pct < 100 and round(pct) >= 100:
        pct_text = f"{99:>3d}%"
    else:
        pct_text = f"{pct:>3.0f}%"
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


NOTE_KIND_COLORS: dict[str, str] = {"info": "dim", "warn": "warn", "crit": "crit"}
"""``AccountRow.attention_kind`` -> the colour kind :func:`_color` understands.

The mapping is the whole reason ``attention_kind`` exists beside
``attention_note`` (SPEC-CODEX 6): a reworded note once silently lost its
colour, so nothing here may classify on the prose. An unknown or empty kind
falls back to ``dim``, which is exactly how the pre-SPEC-CODEX-6 age note was
already drawn — so a header that carries no kind renders byte-for-byte as
before."""


def quota_header(
    label: str,
    plan: str = "",
    note: str = "",
    *,
    active: bool = False,
    note_kind: str = "",
) -> list[tuple[str, str | None]]:
    """``Codex (pro) · active   · relogin in 1d 4h`` — a read-only quota heading.

    Still deliberately unlike :func:`account_header`: no slot number, no email,
    and never the ``accent``/``● active`` treatment. A quota row is not a
    switch target (SPEC-CODEX 4/6 — ``switchable`` is False on every one of
    them), and the one visual promise this menu makes is that accent means
    "this is the account you are on". The block below it is drawn with the same
    :func:`window_line`, so the bars, the severity colours and the ``(!)``
    marker are shared, not re-implemented.

    *active* marks the login the Codex CLI is using right now (SPEC-CODEX 6,
    ``~/.codex/auth.json`` ``tokens.account_id``). It renders **dim**, not
    accent, precisely because it is a statement of fact about another process
    and not an invitation to click: four Codex rows are live at once and only
    one of them is the one Codex would spend against.

    *note* is either a staleness age or a standing sentinel that has REPLACED
    the figures (SPEC 4.3); *note_kind* colours it through
    :data:`NOTE_KIND_COLORS` and nothing else reads the wording. Defaults keep
    every existing call byte-for-byte: no marker, dim note.
    """
    segs: list[tuple[str, str | None]] = [(label, None)]
    if plan:
        segs.append((f" ({plan})", "dim"))
    if active:
        segs.append((" · active", "dim"))
    if note:
        segs.append((f"   · {note}", NOTE_KIND_COLORS.get(note_kind, "dim")))
    return segs
