"""The apple card theme (design 3): an Apple-native minimal dropdown.

A port of the design2 prototype ``evidence/design2/apple/proto.py`` onto
:mod:`card_base`. The top of the menu reads like a system menu extra:

- a HERO per vendor - the active Claude account (slot badge, name, email, the
  binding window's figure large, a 6 pt gauge, "7-day limit · resets Sat
  14:00" with the other windows right-aligned) and a clickable "Switch to …"
  line; then the active Codex login the same way, with at most one fact line
  (a usable reset credit, out of credits, the pace forecast) and a
  "Best: … · next opens …" line;
- Needs attention - one line per noted Claude slot or alarmed Codex row
  (state in the headline, the short remedy under it, the verbatim note in the
  tooltip), then the glance menu's non-account lines
  (``ThemeActions.attention_extras``) verbatim;
- Claude accounts / Codex accounts - list rows with a round badge, the
  caption, and a figure over a 60 pt gauge.

The app appends the common native tail (Cost, Sessions, … Quit); this module
never draws it. Clicks: a Claude row switches to it, the Switch line switches
to the account it names, a Codex relogin row starts "Log in again". Every other
block has no action, so it never highlights.

Honesty (SPEC 4.3): a noted account shows no figures ("figures withheld"),
an expired window never binds and never shows a live figure, stale readings
say "Nm old".
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ..card_base import LEAD, RIGHT, TRAIL, Block, Pen, register_role
from . import ThemeActions

GAUGE_W = 60.0
"""Right-hand gauge column of a list row."""

ICON = 24.0
"""List-row badge diameter."""

HERO_ICON = 30.0
"""Hero badge diameter."""

WINDOW_NAME = {"5h": "5-hour", "7d": "7-day", "weekly": "Weekly"}

_BADGE = "apple_badge"
_ROLES_READY = False


def _ensure_roles() -> None:
    """The list-row badge keeps its quiet fill while its row is highlighted
    (the prototype's badge sits on the selection unchanged)."""
    global _ROLES_READY
    if _ROLES_READY:
        return
    # Spelled per appearance, not tertiarySystemFill: our views are not
    # vibrant and that fill washed to white over a tinted menu (design 4).
    register_role(_BADGE, (0, 0, 0, 0.07), (255, 255, 255, 0.12), hover=None)
    _ROLES_READY = True


# ---------------------------------------------------------------- wording --


def _mark(R: Any, s: str) -> str:
    """'↺ Sat 14:00' -> 'Sat 14:00'; overdue stays a word."""
    s = (s or "").strip()
    if s == R.RESET_MARK_OVERDUE:
        return "overdue"
    return s.replace(R.TITLE_RESET_GLYPH, "").strip()


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _claude_state(A: Any, row: Any, note: str, kind: str) -> tuple[str, str, str]:
    """(headline, short action, age) for a noted Claude slot. The verbatim
    remedy lives in the tooltip."""
    age = A._age_label(row.usage_age_seconds) if row is not None else "?"
    age_txt = f"last seen {age} ago" if age != "?" else ""
    slot = row.slot if row is not None else "?"
    if kind == "fetch-failing":
        code = re.search(r"HTTP (\d{3})", note or "")
        head = "Usage refused" + (f" · HTTP {code.group(1)}" if code else "")
        return head, f"Log in again and cswap add, or cswap remove {slot}", age_txt
    short = A._title_note(note, kind).replace("⚠", "").strip()
    head = {"relogin": "Sign-in expired", "expired": "Token expired"}.get(short, _cap(short))
    act = "Log in with Claude Code, then cswap add" if short == "relogin" else (
        note.split(" — ")[-1] if note else "")
    return head, act, age_txt


def _fact_symbol(kind: str, usable: bool, info_role: str) -> tuple[str, str]:
    if usable:
        return "arrow.counterclockwise.circle.fill", "good"
    if kind == "crit":
        return "xmark.octagon.fill", "crit"
    if kind == "warn":
        return "gauge.with.dots.needle.67percent", "warn"
    return "info.circle", info_role


# ------------------------------------------------------------ primitives --


def _separator(key: str) -> Block:
    def paint(p: Pen, w: float, h: float) -> None:
        p.line(LEAD - 6, 5, w - (TRAIL - 4), "sep")
    return Block(11, paint, key=key)


def _section(title: str, right: str = "", *, key: str, tooltip: str = "") -> Block:
    def paint(p: Pen, w: float, h: float) -> None:
        rw = p.text(right, RIGHT, 16, 11, "regular", "secondary", right=True) if right else 0.0
        p.text(title, LEAD, 16, 11, "semibold", "secondary",
               maxw=RIGHT - LEAD - (rw + 8 if rw else 0))
    return Block(22, paint, ax=f"{title}. {right}".strip(". "), tooltip=tooltip, key=key)


# ---------------------------------------------------------------- heroes ---


def _claude_windows(row: Any) -> list[tuple[str, Any, Any]]:
    windows = [("5h", row.five_hour_pct, row.five_hour_resets_at),
               ("7d", row.seven_day_pct, row.seven_day_resets_at)]
    resets = dict(row.scoped_resets_at)
    windows += [(n, pct, resets.get(n)) for n, pct in row.scoped_windows]
    return windows


def _window_tips(A: Any, row: Any, now: float) -> list[str]:
    """One tooltip / VoiceOver line per window. An expired window says so and
    never reads as a live figure (SPEC 4.3), as in the other two themes."""
    from ..contracts import format_pct

    dead = set(row.expired_windows or ())
    out = []
    for label, pct, reset in _claude_windows(row):
        if pct is None:
            continue
        if A._WINDOW_KEYS.get(label, label) in dead:
            out.append(f"{label} window ended (last {format_pct(pct)})")
        else:
            out.append(f"{label} {format_pct(pct)}  {A._reset_mark_text(reset, now)}".rstrip())
    return out


def _other_windows(A: Any, row: Any, binding: Any) -> list[str]:
    from ..contracts import format_pct

    dead = set(row.expired_windows or ())
    return [f"{label} {format_pct(pct)}" for label, pct, _ in _claude_windows(row)
            if pct is not None and not (binding and label == binding[0])
            and A._WINDOW_KEYS.get(label, label) not in dead]


def _hero_claude(A: Any, R: Any, snap: Any, now: float, models: tuple, actions: ThemeActions) -> list[Block]:
    from ..contracts import format_pct

    a = snap.active
    notes = snap.account_notes
    room = A._claude_room(snap)
    fleet: list[str] = []
    if snap.settings.get("scoped_fleet_line_enabled", True):
        names: list[str] = []
        for r in snap.accounts:
            for n, _ in r.scoped_windows:
                if n not in names:
                    names.append(n)
        fleet = [f for f in (A._scoped_fleet_heading(snap.accounts, name=n, now=now, notes=notes)
                             for n in names) if f]
    right = ([f"{room[0]} of {room[1]} with room"] if room else []) + fleet
    blocks = [_section("Claude", " · ".join(right), key="claude-section")]
    if a is None:
        def paint0(p: Pen, w: float, h: float) -> None:
            p.text("No active account", LEAD, 18, 13, "regular", "secondary")
        blocks.append(Block(28, paint0, ax="Claude: no active account", key="claude-head"))
        return blocks

    note = notes.get(a.slot)
    kind = snap.account_note_kinds.get(a.slot, "")
    binding = None if note else A._binding_window(a, models)
    name = A._display_name(a)
    others = [] if note else _other_windows(A, a, binding)
    tip = [f"{name} ({a.email}) · slot {a.slot}"]
    if not note:
        tip += _window_tips(A, a, now)
    else:
        tip.append(note)
    stale = a.usage_is_stale
    nx = LEAD + HERO_ICON + 10
    # Real-money extra usage at the warn threshold, as glance's Claude card.
    spend = A._spend_pct(a)
    extra = (f"extra {format_pct(spend)}", R.severity(spend)) if (
        spend is not None and spend >= R.WARN_PCT) else None

    def paint(p: Pen, w: float, h: float) -> None:
        p.circle(LEAD, 6, HERO_ICON, "accent")
        p.text(str(a.slot), LEAD + HERO_ICON / 2, 26, 14, "semibold", "white", mono=True, center=True)
        if note:
            head, _act, _age = _claude_state(A, a, note, kind)
            p.text(name, nx, 19, 15, "semibold", "label", maxw=RIGHT - nx)
            p.text(a.email, nx, 35, 11, "regular", "secondary", maxw=RIGHT - nx)
            p.symbol("exclamationmark.triangle.fill", LEAD, 49, 11, "warn", box=14)
            p.text(f"{head} — figures withheld", LEAD + 18, 60, 11, "medium", "label",
                   maxw=RIGHT - LEAD - 18)
            return
        big = format_pct(binding[1]) if binding else "—"
        bw = p.text(big, RIGHT, 31, 26, "semibold", "label", mono=True, rounded=True, right=True)
        p.text(name, nx, 19, 15, "semibold", "label", maxw=RIGHT - bw - 10 - nx)
        p.text(a.email, nx, 35, 11, "regular", "secondary", maxw=RIGHT - bw - 10 - nx)
        s = R.severity(binding[1]) if binding else "ok"
        p.bar(LEAD, 49, RIGHT - LEAD, 6, binding[1] if binding else None, "ok" if s == "ok" else s)
        if binding:
            reset = _mark(R, A._reset_mark_text(A._window_reset(a, binding[0]), now))
            left = f"{WINDOW_NAME.get(binding[0], binding[0])} limit" + (f" · resets {reset}" if reset else "")
        else:
            left = "No live window reported"
        if stale:
            left += f" · {A._age_label(a.usage_age_seconds)} old"
        ow = p.width(" · ".join(others), 11, mono=True) if others else 0.0
        xw = (p.width(f" · {extra[0]}", 11, "medium") if extra else 0.0)
        lw = p.text(left, LEAD, 71, 11, "regular", "secondary",
                    maxw=RIGHT - LEAD - (ow + 10 if ow else 0) - xw)
        if extra:
            p.text(f" · {extra[0]}", LEAD + lw, 71, 11, "medium", extra[1])
        if others:
            p.text(" · ".join(others), RIGHT, 71, 11, "regular", "secondary", mono=True, right=True)

    if note:
        ax = f"Active Claude account {name}: figures withheld"
    elif binding:
        ax = f"Active Claude account {name}: {binding[0]} window {format_pct(binding[1])}"
        if stale:
            ax += f", {A._age_label(a.usage_age_seconds)} old"
    else:
        ax = f"Active Claude account {name}: no live window reported"
    if extra and not note:
        ax += f", {extra[0]}"
        tip.append(f"extra usage {format_pct(spend)}")
    blocks.append(Block(67 if note else 80, paint, tooltip="\n".join(tip), ax=ax, key="claude-head"))

    target = next((r for r in A._switch_targets(snap.accounts)
                   if not getattr(r, "disabled", False) and r.slot not in notes), None)
    if target is not None:
        tw = A._binding_window(target, models)
        tname = A._display_name(target)
        fig = f"{tw[0]} {format_pct(tw[1])}" if tw else ""

        def paint2(p: Pen, w: float, h: float) -> None:
            p.symbol("arrow.left.arrow.right", LEAD, 3, 12, "accent", "medium", box=16)
            fw = p.width(fig, 11, mono=True) if fig else 0.0
            p.text(f"Switch to {tname}", LEAD + 22, 16, 13, "regular", "label",
                   maxw=RIGHT - LEAD - 22 - fw - 12)
            if fig:
                p.text(fig, RIGHT, 16, 11, "regular", "secondary", mono=True, right=True)

        slot = target.slot
        blocks.append(Block(
            22, paint2, key="claude-switch",
            action=lambda: actions.switch_to(slot),
            ax=f"Switch to {tname}" + (f", {fig}" if fig else ""),
            tooltip=f"Most headroom of the accounts you can switch to: {tname}"
                    + (f" ({fig})" if fig else ""),
        ))
    return blocks


def _live_codex(A: Any, snap: Any) -> tuple[Any, ...]:
    return tuple(r for r in A._visible_quota_rows_of(snap) if r.vendor != A.VENDOR_CLAUDE and r.slot < 0)


def _hero_codex(A: Any, R: Any, snap: Any, now: float, live: tuple) -> list[Block]:
    from ..contracts import format_pct

    # "Codex fleet line" OFF hides the room / next / resets facts, as in glance.
    fp = (A._codex_fleet_parts(A._visible_quota_rows_of(snap), now=now)
          if snap.settings.get("codex_fleet_line_enabled", True) else None)
    a = next((r for r in live if r.is_active), None)
    usable = a is not None and (a.reset_credits_usable or 0) > 0
    right = ([f"{fp.room} of {fp.total} with room"] if fp else []) + (
        [fp.resets] if fp and fp.resets and not usable else [])
    blocks = [_section("Codex", " · ".join(right), key="codex-section")]
    if a is None:
        def p0(p: Pen, w: float, h: float) -> None:
            p.text("Active login not identified", LEAD, 18, 13, "regular", "secondary")
        blocks.append(Block(28, p0, ax="Codex: active login not identified", key="codex-head"))
        return blocks
    win = A._plan_window(a)
    alarm = A._quota_alarm(a)
    fact = A._codex_fact_line(a)
    plan = A.plan_label(a.plan_type)
    best = A.best_codex_row(live)
    tip = [A._quota_head(a) + (f" · {a.email}" if a.email else "")] + list(a.info_notes or ())
    if a.attention_note:
        tip.append(a.attention_note)
    expired = bool(win and win[2])
    pct = None if (alarm or win is None or expired) else win[1]
    if alarm:
        left = a.attention_note
    elif win is None:
        left = A._quota_note(a)[0] or "No reading yet"
    else:
        reset = _mark(R, A._quota_reset_mark(a, now))
        left = f"{WINDOW_NAME.get(win[0], win[0])} limit" + (f" · resets {reset}" if reset else "")
        if expired:
            left += " · window ended"
        if a.usage_is_stale:
            left += f" · {A._age_label(a.usage_age_seconds)} old"
    nx = LEAD + HERO_ICON + 10

    def paint(p: Pen, w: float, h: float) -> None:
        p.circle(LEAD, 6, HERO_ICON, "accent")
        p.symbol("terminal.fill", LEAD, 6, 13, "white", "semibold", box=HERO_ICON)
        big = format_pct(pct) if pct is not None else "—"
        bw = p.text(big, RIGHT, 31, 26, "semibold", "label", mono=True, rounded=True, right=True)
        pw = (p.width(plan, 11, "medium") + 6) if plan else 0.0
        nw = p.text(a.alias, nx, 19, 15, "semibold", "label", maxw=RIGHT - bw - 14 - nx - pw)
        if plan:
            p.text(plan, nx + nw + 6, 19, 11, "medium", "secondary")
        if a.email:
            p.text(a.email, nx, 35, 11, "regular", "secondary", maxw=RIGHT - bw - 10 - nx)
        s = R.severity(pct)
        p.bar(LEAD, 49, RIGHT - LEAD, 6, pct, "ok" if s == "ok" else s)
        p.text(left, LEAD, 71, 11, "regular", "secondary", maxw=RIGHT - LEAD)
        if fact:
            text, kind = fact
            sym, col = _fact_symbol(kind, usable, "secondary")
            if usable:
                text = text.replace(R.TITLE_RESET_GLYPH, "").strip()
            p.symbol(sym, LEAD, 88 - 11, 11, col, "medium", box=14)
            p.text(_cap(text), LEAD + 18, 88, 11, "medium" if usable else "regular",
                   "label" if usable else "secondary", maxw=RIGHT - LEAD - 18)

    ax = f"Active Codex account {a.alias}: " + (
        f"{win[0]} {format_pct(pct)}" if pct is not None else left or "no reading")
    blocks.append(Block(78 + (16 if fact else 0), paint, tooltip="\n".join(tip), ax=ax, key="codex-head"))

    bits = []
    if best is not None and best is not a:
        bits.append(f"Best: {A._quota_alias(best)} {format_pct(best.seven_day_pct)}")
    if fp and fp.next_at:
        m = _mark(R, R.reset_mark(fp.next_at, now))
        if m:
            bits.append(f"next opens {m} ({fp.next_alias})")
    if bits:
        line = " · ".join(bits)

        def paint2(p: Pen, w: float, h: float) -> None:
            p.text(line, LEAD, 12, 11, "regular", "secondary", maxw=RIGHT - LEAD)
        blocks.append(Block(20, paint2, ax=line, tooltip=line, key="codex-best"))
    return blocks


# ---------------------------------------------------------- needs attention -


def _attention(A: Any, snap: Any, actions: ThemeActions) -> list[Block]:
    rows = A.needs_attention_rows(snap)
    extra = [x for x in actions.attention_extras if x]
    if not rows and not extra:
        return []
    by_slot = {r.slot: r for r in snap.accounts}
    verbatim: list[str] = []
    body: list[Block] = []
    for i, (kind, ref) in enumerate(rows):
        action: Callable[[], Any] | None = None
        if kind == "claude":
            row = by_slot.get(ref)
            note = snap.account_notes.get(ref, "")
            head, act, age = _claude_state(A, row, note, snap.account_note_kinds.get(ref, ""))
            title = A._display_name(row) if row is not None else f"slot {ref}"
            sym, col = "exclamationmark.triangle.fill", "warn"
            tip = note
            key = f"attention-claude-{ref}"
        else:
            row = ref
            head = row.attention_note
            title = A._quota_alias(row)
            act = A.plan_label(row.plan_type)
            age = ""
            sym, col = (("xmark.octagon.fill", "crit") if row.attention_kind == "crit"
                        else ("exclamationmark.triangle.fill", "warn"))
            tip = row.attention_note
            action = actions.codex_login_for(row)
            key = f"attention-codex-{i}"
        if tip:
            verbatim.append(tip)
        short_age = age.replace("last seen ", "")

        def paint(p: Pen, w: float, h: float, title: str = title, head: str = head, act: str = act,
                  sym: str = sym, col: str = col, short_age: str = short_age) -> None:
            p.symbol(sym, LEAD, 6, 13, col, "medium", box=18)
            x = LEAD + 26
            aw = p.width(short_age, 11, mono=True) if short_age else 0.0
            tw = p.text(title, x, 19, 13, "semibold", "label", maxw=150)
            p.text(head, x + tw + 6, 19, 13, "regular", "label",
                   maxw=RIGHT - (x + tw + 6) - (aw + 8 if aw else 0))
            if short_age:
                p.text(short_age, RIGHT, 19, 11, "regular", "secondary", mono=True, right=True)
            p.text(act, x, 36, 11, "regular", "secondary", maxw=RIGHT - x)

        ax = f"{title}: {head}. {act}".strip(". ")
        if action is not None:
            ax = f"{ax}. Log in again"
        body.append(Block(44, paint, tooltip=tip, ax=ax, action=action, key=key))
    for j, line in enumerate(extra):
        verbatim.append(line)

        def paint_x(p: Pen, w: float, h: float, line: str = line) -> None:
            p.symbol("info.circle", LEAD, 3, 13, "secondary", box=18)
            p.text(line, LEAD + 26, 16, 12, "regular", "label", maxw=RIGHT - LEAD - 26)
        body.append(Block(24, paint_x, ax=line, tooltip=line, key=f"attention-extra-{j}"))
    head_block = _section("Needs attention", str(len(rows) + len(extra)), key="attention",
                          tooltip="\n".join(verbatim))
    return [head_block] + body


# --------------------------------------------------------------- list rows --


def _claude_row(A: Any, R: Any, r: Any, now: float, models: tuple, actions: ThemeActions) -> Block:
    from ..contracts import format_pct

    binding = A._binding_window(r, models)
    caption = []
    if getattr(r, "disabled", False):
        caption.append("Disabled")
    if r.usage_is_stale:
        caption.append(f"{A._age_label(r.usage_age_seconds)} old")
    if binding:
        m = _mark(R, A._reset_mark_text(A._window_reset(r, binding[0]), now))
        if m:
            caption.append(f"resets {m}")
    elif r.expired_windows:
        caption.append("window ended · awaiting a reading")
    else:
        caption.append("no window reported")
    others = _other_windows(A, r, binding)
    caption_text = _cap(" · ".join(caption))
    sub = " · ".join(([caption_text] if caption_text else []) + others)
    name = A._display_name(r)
    tip = [f"{name} ({r.email}) · slot {r.slot}"] + _window_tips(A, r, now)
    pct = binding[1] if binding else None
    slot_txt = str(r.slot)

    def paint(p: Pen, w: float, h: float) -> None:
        p.circle(LEAD, 8.5, ICON, _BADGE)
        p.text(slot_txt, LEAD + ICON / 2, 24.5, 12, "medium", "secondary", mono=True, center=True)
        nx = LEAD + ICON + 10
        gx = RIGHT - GAUGE_W
        fig = format_pct(pct) if pct is not None else "—"
        pw = p.width(fig, 13, "semibold", mono=True, rounded=True)
        lw = (p.width(binding[0], 11) + 4) if binding else 0.0
        p.text(name, nx, 18, 13, "regular", "label", maxw=RIGHT - pw - lw - 8 - nx)
        p.text(sub, nx, 34, 11, "regular", "secondary", maxw=gx - nx - 8)
        p.text(fig, RIGHT, 18, 13, "semibold", "label", mono=True, rounded=True, right=True)
        if binding:
            p.text(binding[0], RIGHT - pw - 4, 18, 11, "regular", "secondary", right=True)
        s = R.severity(pct)
        p.bar(gx, 26.5, GAUGE_W, 4, pct, "secondary" if s == "ok" else s)

    # `switchable` is the only thing that makes a row clickable (AccountRow
    # contract); a `cswap disable`d slot stays a valid explicit target.
    switchable = getattr(r, "switchable", True)
    slot = r.slot
    action = (lambda: actions.switch_to(slot)) if switchable else None
    ax = (f"Switch to {name}; " if action else f"{name}; ") + "; ".join(tip[1:] + ([caption_text] if caption_text else []))
    return Block(41, paint, action=action, tooltip="\n".join(tip), ax=ax.rstrip("; "),
                 key=f"claude-row-{r.slot}")


def _codex_row(A: Any, R: Any, r: Any, now: float, actions: ThemeActions) -> Block:
    from ..contracts import format_pct

    win = A._plan_window(r)
    alarm = A._quota_alarm(r)
    fact = A._codex_fact_line(r)
    plan = A.plan_label(r.plan_type)
    reset = _mark(R, A._quota_reset_mark(r, now))
    caption = []
    if alarm:
        caption.append(r.attention_note)
    elif win is None:
        caption.append(A._quota_note(r)[0] or "No reading yet")
    else:
        if reset:
            caption.append(f"Resets {reset}")
        if win[2]:
            caption.append("window ended")
        if r.usage_is_stale:
            caption.append(f"{A._age_label(r.usage_age_seconds)} old")
    caption_text = _cap(" · ".join(c for c in caption if c))
    tip = [A._quota_head(r) + (f" · {r.email}" if r.email else "")] + list(r.info_notes or ()) + (
        [r.attention_note] if r.attention_note else [])
    fact_line = fact if fact and not (alarm and fact[0] == r.attention_note) else None
    usable = (r.reset_credits_usable or 0) > 0
    pct = None if (alarm or win is None or win[2]) else win[1]
    wlabel = ("7d" if win[0] == "weekly" else win[0]) if win is not None else ""

    def paint(p: Pen, w: float, h: float) -> None:
        p.circle(LEAD, 8.5, ICON, _BADGE)
        p.symbol("terminal", LEAD, 8.5, 10, "secondary", "medium", box=ICON)
        nx = LEAD + ICON + 10
        gx = RIGHT - GAUGE_W
        fig = format_pct(pct) if pct is not None else "—"
        fw = p.width(fig, 13, "semibold", mono=True, rounded=True)
        lw = (p.width(wlabel, 11) + 4) if wlabel else 0.0
        plw = (p.width(plan, 11) + 5) if plan else 0.0
        nw = p.text(r.alias, nx, 18, 13, "regular", "label", maxw=RIGHT - fw - lw - 8 - nx - plw)
        if plan:
            p.text(plan, nx + nw + 5, 18, 11, "regular", "secondary")
        p.text(caption_text, nx, 34, 11, "regular", "secondary", maxw=gx - nx - 8)
        p.text(fig, RIGHT, 18, 13, "semibold", "label", mono=True, rounded=True, right=True)
        if wlabel:
            p.text(wlabel, RIGHT - fw - 4, 18, 11, "regular", "secondary", right=True)
        s = R.severity(pct)
        p.bar(gx, 26.5, GAUGE_W, 4, pct, "secondary" if s == "ok" else s)
        if fact_line:
            text, kind = fact_line
            sym, col = _fact_symbol(kind, usable, "secondary")
            text = _cap(text.replace(R.TITLE_RESET_GLYPH, "").strip())
            p.symbol(sym, nx, 39.5, 10, col, "medium", box=13)
            p.text(text, nx + 17, 50, 11, "regular", "secondary", maxw=RIGHT - nx - 17)

    action = actions.codex_login_for(r)
    ax = f"{A._quota_head(r)}: " + "; ".join(tip[1:] + ([caption_text] if caption_text else []))
    if action is not None:
        ax += ". Log in again"
    return Block(41 + (16 if fact_line else 0), paint, action=action, tooltip="\n".join(tip),
                 ax=ax, key=f"codex-row-{r.alias or r.slot}")


# ------------------------------------------------------------------- build --


def build(snapshot: Any, now: float, actions: ThemeActions) -> list[Block]:
    """Blocks for the top of the dropdown; the app appends the native tail."""
    from .. import app as A
    from .. import render as R

    _ensure_roles()
    snap = snapshot
    models = tuple(snap.autoswitch_models or ())
    live = _live_codex(A, snap)
    sections: list[list[Block]] = []
    if snap.accounts or not live:
        sections.append(_hero_claude(A, R, snap, now, models, actions))
    if live:
        sections.append(_hero_codex(A, R, snap, now, live))
    sections.append(_attention(A, snap, actions))
    claude_rows = [r for r in snap.accounts if not r.is_active and r.slot not in snap.account_notes]
    if claude_rows:
        sections.append([_section("Claude accounts", key="claude-accounts")]
                        + [_claude_row(A, R, r, now, models, actions) for r in claude_rows])
    # An alarmed login is listed once, in Needs attention (with its Log in
    # again action), not a second time here - as glance and dense do.
    codex_rows = [r for r in live if not r.is_active and not A._quota_alarm(r)]
    if codex_rows:
        sections.append([_section("Codex accounts", key="codex-accounts")]
                        + [_codex_row(A, R, r, now, actions) for r in codex_rows])
    blocks: list[Block] = []
    for i, part in enumerate(p for p in sections if p):
        if i:
            blocks.append(_separator(f"sep-{part[0].key}"))
        blocks += part
    return blocks
