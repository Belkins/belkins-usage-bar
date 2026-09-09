#!/usr/bin/env python3
"""Self-reporting menu-bar slot audit. Prints a VERDICT; needs no human eyes.

usage:
  python menubar_slot_audit.py            # audit only (read-only)
  python menubar_slot_audit.py --probe XX # also create a status item titled XX
                                          # and report whether IT got onscreen
"""
import ctypes, sys, objc
from AppKit import NSScreen

cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]


def windows():
    p = cg.CGWindowListCopyWindowInfo(0, 0)   # kCGWindowListOptionAll
    return list(objc.objc_object(c_void_p=p)) if p else []


def statusbar_items():
    out = []
    for w in windows():
        if w.get('kCGWindowLayer') != 25:
            continue
        b = w.get('kCGWindowBounds') or {}
        if b.get('Y') != 0:                   # menu bar strip only
            continue
        out.append(dict(pid=w.get('kCGWindowOwnerPID'),
                        owner=str(w.get('kCGWindowOwnerName')),
                        x=b.get('X'), w=b.get('Width'),
                        onscreen=bool(w.get('kCGWindowIsOnscreen'))))
    return sorted(out, key=lambda d: d['x'])


def report(highlight_pid=None):
    s = NSScreen.mainScreen()
    fr = s.frame()
    scr_w = fr.size.width
    try:
        aux_r = s.auxiliaryTopRightArea()
        notch_end = aux_r.origin.x
        notch_start = s.auxiliaryTopLeftArea().size.width
        notched = True
    except Exception:
        notch_start = notch_end = None
        notched = False

    items = statusbar_items()
    print(f"screen width           : {scr_w}")
    print(f"notched display        : {notched}")
    if notched:
        print(f"notch occupies x       : {notch_start} .. {notch_end}  ({notch_end-notch_start} pt)")
        print(f"status-item region     : x {notch_end} .. {scr_w}  ({scr_w-notch_end} pt usable)")
    print()
    print("layer-25 menu-bar-strip windows (x-sorted):")
    for it in items:
        mark = '  <-- THIS PROCESS' if it['pid'] == highlight_pid else ''
        print(f"  x={it['x']:>7.1f} w={it['w']:>6.1f} onscreen={str(it['onscreen']):<5} "
              f"pid={it['pid']:<6} {it['owner']}{mark}")
    print()

    onscreen = [i for i in items if i['onscreen']]
    if notched and onscreen:
        leftmost = min(i['x'] for i in onscreen)
        free = leftmost - notch_end
        print(f"leftmost ONSCREEN item : x={leftmost}")
        print(f"FREE space right of notch: {free:.1f} pt")
    else:
        free = None

    if highlight_pid is not None:
        mine = [i for i in items if i['pid'] == highlight_pid]
        if not mine:
            print(f"VERDICT: pid {highlight_pid} owns NO menu-bar-layer window at all.")
            return 2
        m = mine[0]
        print(f"our item               : x={m['x']} w={m['w']} onscreen={m['onscreen']}")
        if m['onscreen']:
            print("VERDICT: RENDERED — our status item is onscreen.")
            return 0
        if notched and m['x'] < notch_end:
            print(f"VERDICT: NOT RENDERED — item was laid out at x={m['x']}, which is LEFT of "
                  f"the notch's right edge ({notch_end}); it is clipped by the notch.")
            if free is not None:
                print(f"         needs {m['w']:.0f} pt, only {free:.0f} pt free right of the notch.")
            return 1
        print("VERDICT: NOT RENDERED — item has geometry right of the notch but is not onscreen "
              "(hidden by the app, or a menu-bar manager collapsed it).")
        return 1
    return 0


if __name__ == '__main__':
    pid = None
    if '--probe' in sys.argv:
        import os, threading, time
        title = sys.argv[sys.argv.index('--probe') + 1]
        from AppKit import NSStatusBar, NSApplication
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(1)            # NSApplicationActivationPolicyAccessory
        bar = NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(-1.0)
        item.button().setTitle_(title)
        item.setVisible_(True)
        globals()['_keep'] = item
        pid = os.getpid()
        print(f"probe pid={pid} title={title!r} length={item.length()} isVisible={item.isVisible()}")

        def later():
            time.sleep(2.0)
            code = report(pid)
            print(f"exit={code}")
            os._exit(code)
        threading.Thread(target=later, daemon=True).start()
        app.run()
    else:
        if len(sys.argv) > 1 and sys.argv[1].isdigit():
            pid = int(sys.argv[1])
        sys.exit(report(pid))
