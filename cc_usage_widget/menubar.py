"""Is our status item actually on screen? — the full-bar fallback.

On a notched MacBook with many menu-bar items, macOS silently drops an item
that does not fit to the right of the notch: the process runs, the item exists,
``isVisible()`` is ``True``, and nothing is drawn. The full title grows with
live alerts (288 pt on 2026-09-30, after a reboot, against 165 pt on
2026-09-25), so a widget that fitted yesterday can vanish today.

The only honest witness is the window server: a composited status item owns an
onscreen layer-25 window. This module reads that, and :func:`item_hidden`
refuses to answer while the menu bar itself is hidden (a fullscreen app or
auto-hide), because every status item reads offscreen then — the trap that
produced false "hidden" readings in the original investigation.

CoreGraphics is reached through ``objc.loadBundleFunctions`` because the
runtime venv (claude-swap's) ships pyobjc Cocoa but not the Quartz wrapper.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

STATUS_ITEM_LAYER = 25
"""``kCGStatusWindowLevel``: the layer every menu-bar status item lives on."""

MIN_OTHERS_ONSCREEN = 3
"""How many OTHER status items must be onscreen before a verdict is given.

Zero onscreen means the bar is hidden (fullscreen, auto-hide) and says nothing
about us. Control Centre alone owns several items (clock, battery, Wi-Fi), so
a visible bar clears this on any Mac."""

_cg_window_list: Any = None


def _window_list() -> Any:
    global _cg_window_list
    if _cg_window_list is None:
        import objc
        from Foundation import NSBundle

        found: dict[str, Any] = {}
        bundle = NSBundle.bundleWithIdentifier_("com.apple.CoreGraphics")
        objc.loadBundleFunctions(
            bundle, found, [("CGWindowListCopyWindowInfo", b"^{__CFArray=}II")]
        )
        _cg_window_list = found["CGWindowListCopyWindowInfo"]
    return _cg_window_list


def status_item_windows() -> list[tuple[int, bool]]:
    """``(owner pid, onscreen)`` for every menu-bar status item window.

    Empty on any failure: no verdict is always the safe answer.
    """
    try:
        rows = _window_list()(0, 0)  # kCGWindowListOptionAll, kCGNullWindowID
        out = []
        for row in rows or ():
            if row.get("kCGWindowLayer") != STATUS_ITEM_LAYER:
                continue
            bounds = row.get("kCGWindowBounds") or {}
            if bounds.get("Height", 0) < 10 or bounds.get("X", -1) < 0:
                continue
            out.append((int(row.get("kCGWindowOwnerPID", 0)), bool(row.get("kCGWindowIsOnscreen", False))))
        return out
    except Exception:
        return []


def item_hidden(windows: Iterable[tuple[int, bool]], pid: int) -> bool | None:
    """``True`` hidden, ``False`` drawn, ``None`` no verdict.

    No verdict when *pid* owns no status item window yet (startup) or when
    fewer than :data:`MIN_OTHERS_ONSCREEN` other items are onscreen (the bar
    itself is hidden).
    """
    mine: list[bool] = []
    others = 0
    for owner, onscreen in windows:
        if owner == pid:
            mine.append(onscreen)
        elif onscreen:
            others += 1
    if not mine or others < MIN_OTHERS_ONSCREEN:
        return None
    return not any(mine)


def own_item_hidden() -> bool | None:
    """:func:`item_hidden` for this process, read from the window server."""
    return item_hidden(status_item_windows(), os.getpid())
