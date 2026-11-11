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


def _is_strip_bounds(bounds: dict[str, Any]) -> bool:
    """True for menu-bar status-item strip windows (excludes tall layer-25 popovers)."""
    try:
        h = float(bounds.get("Height", 0) or 0)
        w = float(bounds.get("Width", 0) or 0)
    except Exception:
        return False
    return 10.0 <= h <= 60.0 and w > 0.0


def status_item_windows() -> list[tuple[int, bool]]:
    """``(owner pid, onscreen)`` for every menu-bar status item window.

    Empty on any failure: no verdict is always the safe answer.
    Note: global CoreGraphics ``X`` coordinates are negative on secondary
    displays arranged to the left of the primary monitor, so only degenerate
    zero-width / sub-10pt auxiliary windows and >60pt popovers are filtered out.
    """
    try:
        rows = _window_list()(0, 0)  # kCGWindowListOptionAll, kCGNullWindowID
        out = []
        for row in rows or ():
            if row.get("kCGWindowLayer") != STATUS_ITEM_LAYER:
                continue
            bounds = row.get("kCGWindowBounds") or {}
            if not _is_strip_bounds(bounds):
                continue
            out.append((int(row.get("kCGWindowOwnerPID", 0)), bool(row.get("kCGWindowIsOnscreen", False))))
        return out
    except Exception:
        return []


def onscreen_other_count(window_number: int | None = None, *, win: Any = None) -> int | None:
    """Count onscreen status-item windows other than *window_number* (or *win* / ``os.getpid()``).

    Returns ``None`` when the menu bar itself is hidden (fewer than
    :data:`MIN_OTHERS_ONSCREEN` items onscreen) or when CoreGraphics is unavailable.
    """
    try:
        pid = os.getpid()
        wx: float | None = None
        ww: float | None = None
        if win is not None:
            try:
                fr = win.frame()
                wx = float(fr.origin.x)
                ww = float(fr.size.width)
            except Exception:
                wx = ww = None
        onscreen_rows = _window_list()(1, 0)
        others = 0
        skipped_self = False
        for row in onscreen_rows or ():
            if row.get("kCGWindowLayer") != STATUS_ITEM_LAYER:
                continue
            bounds = row.get("kCGWindowBounds") or {}
            if not _is_strip_bounds(bounds):
                continue
            wnum = int(row.get("kCGWindowNumber", 0))
            owner = int(row.get("kCGWindowOwnerPID", 0))
            if window_number and 0 < window_number < 0x100000000:
                if wnum == window_number:
                    continue
            elif (
                not skipped_self
                and wx is not None
                and ww is not None
                and abs(float(bounds.get("X", -99999)) - wx) <= 4.0
                and abs(float(bounds.get("Width", -99999)) - ww) <= 4.0
            ):
                skipped_self = True
                continue
            elif owner == pid:
                continue
            others += 1
        return others if others >= MIN_OTHERS_ONSCREEN else None
    except Exception:
        return None


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


def own_window_hidden(window_number: int | None, *, win: Any = None) -> bool | None:
    """Detect whether *window_number* (or *win*, from ``nsstatusitem.button().window()``)
    is hidden by the MacBook camera notch or menu-bar overflow on macOS 15 / 26+.

    On macOS 26 (Tahoe), status-item windows on layer 25 report ``Control Centre``
    as ``kCGWindowOwnerPID`` rather than ``os.getpid()``, and ``win.windowNumber()``
    returns the out-of-process proxy sentinel ``0x100000000`` (``4294967296``) whose
    ``occlusionState()`` does NOT set ``NSWindowOcclusionStateVisible`` (returning
    ``8192`` even when drawn). Therefore we match ``win.frame()`` directly against
    all layer-25 strip windows from ``CGWindowListCopyWindowInfo(0, 0)`` and read
    WindowServer's ``kCGWindowIsOnscreen`` flag, returning ``None`` whenever layout
    is still in flight or the target display's menu bar is hidden (fullscreen/auto-hide).
    """
    try:
        all_rows = _window_list()(0, 0)  # kCGWindowListOptionAll = 0
        strip: list[tuple[int, int, float, float, float, bool]] = []
        for row in all_rows or ():
            if row.get("kCGWindowLayer") != STATUS_ITEM_LAYER:
                continue
            bounds = row.get("kCGWindowBounds") or {}
            if not _is_strip_bounds(bounds):
                continue
            strip.append(
                (
                    int(row.get("kCGWindowNumber", 0)),
                    int(row.get("kCGWindowOwnerPID", 0)),
                    float(bounds.get("X", -99999)),
                    float(bounds.get("Y", 0)),
                    float(bounds.get("Width", 0)),
                    bool(row.get("kCGWindowIsOnscreen", False)),
                )
            )

        matched_idx: int | None = None
        if win is not None:
            try:
                fr = win.frame()
                wx = float(fr.origin.x)
                ww = float(fr.size.width)
                wh = float(fr.size.height)
                if ww <= 0 or wh <= 0:
                    return None
                best_dist = 999.0
                for idx, (_wnum, _owner, bx, _by, bw, _on) in enumerate(strip):
                    if abs(bx - wx) <= 4.0 and abs(bw - ww) <= 4.0:
                        dist = abs(bx - wx) + abs(bw - ww)
                        if dist < best_dist:
                            best_dist = dist
                            matched_idx = idx
            except Exception:
                matched_idx = None

        if matched_idx is None and window_number and 0 < window_number < 0x100000000:
            for idx, (wnum, _owner, _bx, _by, _bw, _on) in enumerate(strip):
                if wnum == window_number:
                    matched_idx = idx
                    break

        others_onscreen = sum(
            1 for idx, row in enumerate(strip) if idx != matched_idx and row[5]
        )
        if others_onscreen < MIN_OTHERS_ONSCREEN:
            return None

        if matched_idx is not None:
            _wnum, _owner, mbx, mby, _mbw, is_onscreen = strip[matched_idx]
            if is_onscreen:
                return False
            # Confirm that THIS display's menu-bar strip is actually onscreen
            # (not a fullscreen Space while a second monitor has >=3 items) and
            # that no onscreen item sits to our left on the same strip (since
            # notch/overflow eviction always clips from the left).
            same_strip_onscreen = [
                bx
                for idx, (_w, _o, bx, by, _bw, on) in enumerate(strip)
                if idx != matched_idx and on and abs(by - mby) <= 10.0 and abs(bx - mbx) <= 2500.0
            ]
            if not same_strip_onscreen:
                return None
            if any(ox + 2.0 <= mbx for ox in same_strip_onscreen):
                return None
            return True

        # Layout in flight after a title width change on a macOS 26 proxy window,
        # or window not yet composited: wait for the next check tick rather than
        # trusting proxy occlusionState() (which reports 8192 = non-visible).
        if win is not None or not window_number or window_number <= 0 or window_number >= 0x100000000:
            return own_item_hidden()
        return None
    except Exception:
        return own_item_hidden()
