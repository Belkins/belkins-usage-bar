#!/usr/bin/env python3
"""Self-reporting: is the cc_usage_widget status item ACTUALLY composited?

Needs no human eyes. Reads kCGWindowIsOnscreen from the window server.
Refuses to answer while the menu bar is hidden (fullscreen app / auto-hide),
because every status item reads onscreen=False then -- the trap that produced
false 'NOT RENDERED' readings during the original investigation.

exit 0 = RENDERED    1 = HIDDEN    2 = no status item    3 = NO VERDICT (gate)
Run with a python that has pyobjc Quartz + Cocoa.
"""
import pathlib, sys, time
from Quartz import (CGWindowListCopyWindowInfo, kCGWindowListOptionAll,
                    kCGNullWindowID)
from AppKit import NSScreen

LOCK = pathlib.Path.home() / ".claude/cc-usage-widget/widget.lock"
CONTROL_MIN = 12
GATE_SECONDS = 90


def items():
    rows = []
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID):
        if w.get("kCGWindowLayer") != 25:
            continue
        b = w["kCGWindowBounds"]
        if b["Height"] < 10 or b["X"] < 0:
            continue
        rows.append((b["X"], b["Width"], bool(w.get("kCGWindowIsOnscreen", False)),
                     w.get("kCGWindowOwnerPID"), w.get("kCGWindowOwnerName")))
    return rows


def main():
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else int(LOCK.read_text().strip())
    scr = NSScreen.mainScreen()
    right = scr.auxiliaryTopRightArea()
    rx = right.origin.x
    print(f"status-item region: x {rx:.0f} .. {rx + right.size.width:.0f}   target pid {pid}")

    deadline = time.time() + GATE_SECONDS
    while time.time() < deadline:
        rows = items()
        others = [r for r in rows if r[2] and r[3] != pid]
        if len(others) >= CONTROL_MIN:
            occupied = sum(r[1] for r in others)
            leftmost = min(r[0] for r in others)
            print(f"control group: {len(others)} items onscreen, {occupied:.0f}pt occupied, "
                  f"leftmost x={leftmost:.0f}, {leftmost - rx:.0f}pt free")
            mine = [r for r in rows if r[3] == pid]
            if not mine:
                print("VERDICT: NO STATUS ITEM — the process owns no menu-bar window")
                return 2
            x, w, on, _, _ = mine[0]
            print(f"our item: x={x:.0f}..{x + w:.0f} w={w:.0f}")
            if on:
                print("VERDICT: RENDERED — the window server is compositing it")
                return 0
            print("VERDICT: HIDDEN — item exists with real geometry but is not composited")
            print("  fix: defaults write com.claude-swap.menubar "
                  "'NSStatusItem Preferred Position Item-0' -float 2000  then restart the widget")
            return 1
        time.sleep(1.0)

    print(f"VERDICT: NO VERDICT — fewer than {CONTROL_MIN} control items were onscreen for "
          f"{GATE_SECONDS}s. The menu bar is hidden (fullscreen app or auto-hide); "
          "every status item reads offscreen. Leave fullscreen and re-run.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
