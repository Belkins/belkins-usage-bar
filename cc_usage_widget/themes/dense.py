"""The dense card theme (design 3): the power-user compact menu.

A port of the design2 ANGLE C prototype (``evidence/design2/dense/proto.py``)
onto :mod:`card_base`. One column grid runs down the whole menu - window
label, a 44 pt bar (plus one hairline per other window), the figure, and a
``↺`` reset column - so a glance reads every account on one line:

* a header module (faint fill) with the active Claude login and the active
  Codex login, each with its binding window, and a caption line with the
  other windows and the next / best target;
* ``Needs attention``: noted Claude slots, alarmed Codex rows, and the
  non-account lines the glance menu carried (``attention_extras``), verbatim;
* ``Claude`` rows (slot, alias, state tags, binding window) - click switches;
* ``Codex`` rows (alias, plan / reset / verdict tags, plan window).

Wording and figures come from the app's own helpers (``_binding_window``,
``_claude_room``, ``_codex_fleet_parts`` ...), so this module cannot say a
number the text menu would not. Clickable rows: a Claude row (switch), a Codex
row whose login is dead (``Log in again…``); nothing else highlights.
"""

from __future__ import annotations

from typing import Any, Callable

from ..card_base import LEAD, RIGHT, W, Block, Pen, register_role
from . import ThemeActions

# ---------------------------------------------------------------------------
# Grid (points) - the prototype's, with x0 on the native title column (LEAD)
# so the cards line up with the native tail the app appends.
# ---------------------------------------------------------------------------
STATE_C = 12.5  # centre of the native state (checkmark) column
X0 = LEAD
R = RIGHT
RESET_W = 74.0  # "↺ Oct 2 13:19" at 10.5 pt monospaced digits
PCT_R = R - RESET_W - 7.0
BAR_R = PCT_R - 48.0  # room for a 15 pt "100%" in the header
BAR_W = 44.0
BAR_X = BAR_R - BAR_W
WIN_R = BAR_X - 5.0  # window label right edge
# Design 5 (2026-09-25, "more spacing and air"): rows 24 -> 28, section
# headers 22 -> 28 (the extra air goes above the title), the header module
# 42 -> 48 per vendor with 6 pt padding. Separators keep the native 11 pt.
ROW_H = 28.0
ROW_BASE = 18.0      # text baseline inside a row
SECTION_H = 28.0
SECTION_BASE = 20.0
SEP_H = 11.0
HEAD_H = 48.0
HEAD_PAD = 6.0
HEAD_BASE = 19.0     # a vendor line's baseline inside the module
HEAD_CAP = 18.0      # vendor line -> its caption line
TAG_GAP = 6.0
ICON_W = 11.0

SMALL_LABELS = {"weekly": "7d", "daily": "1d", "hourly": "1h"}

# Type ramp: (size, weight, mono)
F_PRI = (13.0, "regular", False)
F_PRI_B = (13.0, "semibold", False)
# Percent figures: SF Pro Rounded, monospaced digits kept (design 4).
F_NUM = (13.0, "semibold", True, True)
F_NUM_BIG = (15.0, "semibold", True, True)
F_SEC = (11.0, "regular", False)
F_CAP = (10.5, "regular", False)
F_CAP_NUM = (10.5, "regular", True)
F_CAP_GOOD = (10.5, "semibold", True)
F_TAG = (10.0, "medium", False)
F_HDR = (11.0, "semibold", False)
F_RESET = (10.5, "regular", True)

# Prototype ink -> card_base roles. The dense bars are quiet: a healthy bar is
# grey (secondary label), only the active header bars take the accent.
TAG_ROLE = {"plan": "secondary", "state": "secondary", "good": "good_text", "crit": "crit_text",
            "warn": "warn_text"}


def _ensure_roles() -> None:
    """The three colours the prototype used that card_base has no role for."""
    try:
        from AppKit import NSColor
    except Exception:  # no AppKit: register_role is a no-op anyway
        return
    # Spelled per appearance, not the system greys: our views are not vibrant
    # and those washed out over a wallpaper-tinted menu (design 4).
    register_role("dense_neutral", (0, 0, 0, 0.50), (255, 255, 255, 0.55), hover=(255, 255, 255, 0.9))
    register_role("dense_track", (0, 0, 0, 0.10), (255, 255, 255, 0.14), hover=(255, 255, 255, 0.28))
    # labelColor at 4.5 %: spelled per appearance, because an alpha applied to
    # a dynamic colour outside a drawing pass freezes it to the light variant.
    register_role("dense_module", (0, 0, 0, 0.045), (255, 255, 255, 0.045), hover="clear")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _w(pen: Pen, s: str, font: tuple) -> float:
    size, weight, mono, *rounded = font
    return pen.width(s, size, weight, mono=mono, rounded=bool(rounded and rounded[0]))


def _t(pen: Pen, s: str, x: float, base: float, font: tuple, role: str, *,
       right: bool = False, maxw: float | None = None) -> float:
    size, weight, mono, *rounded = font
    return pen.text(s, x, base, size, weight, role, mono=mono, right=right, maxw=maxw,
                    rounded=bool(rounded and rounded[0]))


def _sev(render: Any, pct: float | None) -> str:
    return render.severity(pct)


# ---------------------------------------------------------------------------
# The theme
# ---------------------------------------------------------------------------


def build(snapshot: Any, now: float, actions: ThemeActions) -> list[Block]:
    """Blocks for the top of the dropdown; the app appends the native tail."""
    from .. import app as A
    from .. import render as RN
    from ..contracts import format_pct as fmt_pct

    _ensure_roles()
    snap = snapshot
    models = tuple(snap.autoswitch_models or ())
    notes, kinds = snap.account_notes, snap.account_note_kinds
    blocks: list[Block] = []
    seps = [0]

    # -- model ------------------------------------------------------------

    def claude_windows(row: Any) -> list[tuple[str, float, str, bool]]:
        """[(label, pct, reset_mark, expired)] in canonical order 5h, 7d, scoped."""
        dead = set(row.expired_windows or ())
        out = []
        for label, pct, key in (("5h", row.five_hour_pct, "five_hour"), ("7d", row.seven_day_pct, "seven_day")):
            if pct is not None:
                out.append((label, pct, A._reset_mark_text(A._window_reset(row, label), now), key in dead))
        for name, pct in row.scoped_windows:
            if pct is not None:
                out.append((name, pct, A._reset_mark_text(A._window_reset(row, name), now), name in dead))
        return out

    def tags_claude(row: Any) -> list[tuple[str, str, str | None]]:
        tags: list[tuple[str, str, str | None]] = []
        if row.disabled:
            tags.append(("disabled", "state", None))
        if row.usage_is_stale:
            tags.append((A._age_label(row.usage_age_seconds), "state", "clock"))
        return tags

    def tags_codex(row: Any) -> list[tuple[str, str, str | None]]:
        tags: list[tuple[str, str, str | None]] = []
        usable = row.reset_credits_usable or 0
        held = row.reset_credits_available or 0
        kind = row.attention_kind or ""
        if usable > 0:
            tags.append((f"{usable} reset usable", "good", None))
        if row.attention_note and kind == "crit":
            tags.append((row.attention_note.split(" · ")[0], "crit", None))
        elif row.attention_note and kind == "info":
            tags.append((row.attention_note, "state", None))
        if usable == 0 and held > 0:
            tags.append((f"{held} reset", "state", None))
        if row.usage_is_stale and not row.attention_note:
            tags.append((A._age_label(row.usage_age_seconds), "state", "clock"))
        plan = A.plan_label(row.plan_type)
        if plan:
            tags.append((plan, "plan", None))
        return tags

    def codex_window(row: Any) -> tuple[str, float | None, str, bool] | None:
        w = A._plan_window(row)
        if w is None:
            return None
        label, pct, expired = w
        return SMALL_LABELS.get(label, label), pct, A._quota_reset_mark(row, now), expired

    # -- drawing primitives ----------------------------------------------------

    def caption(pen: Pen, segs: list[tuple[str, str]], x: float, base: float, maxw: float) -> None:
        """Caption as coloured segments joined by a quiet middle dot."""
        for i, (text, kind) in enumerate(segs):
            if i:
                x += _t(pen, "  ·  ", x, base, F_CAP_NUM, "tertiary")
            room = X0 + maxw - x
            if room < 20:
                break
            font = F_CAP_GOOD if kind == "good" else F_CAP_NUM  # the one good-news line gets weight
            x += _t(pen, text, x, base, font, TAG_ROLE.get(kind, kind), maxw=room)

    def tag_w(pen: Pen, tag: tuple[str, str, str | None]) -> float:
        text, _kind, icon = tag
        return _w(pen, text, F_TAG) + (ICON_W if icon else 0.0)

    def alias_budget(pen: Pen, label: str) -> float:
        """Alias+tags width: up to the window label that row actually draws."""
        return WIN_R - (_w(pen, label, F_CAP) if label else 0.0) - 8.0 - X0

    def alias_and_tags(pen: Pen, alias: str, tags: list[tuple[str, str, str | None]], base: float,
                       font: tuple[float, str, bool], maxw: float) -> None:
        """Alias, then tags. State tags always show (the alias truncates, down to a
        floor); a plan tag is decoration and only shows when everything fits."""
        full = _w(pen, alias, font)
        floor = min(full, 64.0)
        shown, used = [], 0.0
        for t in (t for t in tags if t[1] != "plan"):
            w = tag_w(pen, t) + TAG_GAP
            if floor + used + w <= maxw:
                shown.append(t)
                used += w
        for t in (t for t in tags if t[1] == "plan"):
            w = tag_w(pen, t) + TAG_GAP
            if full + used + w + 10.0 <= maxw:  # never butt against the window label
                shown.append(t)
                used += w
        x = X0 + _t(pen, alias, X0, base, font, "label", maxw=maxw - used)
        for text, kind, icon in shown:
            role = TAG_ROLE[kind]
            x += TAG_GAP
            if icon:
                pen.symbol(icon, x + 4.5, base - 3.6, 8.5, role, "medium", anchor="center")
                x += ICON_W
            x += _t(pen, text, x, base, F_TAG, role)

    def metric(pen: Pen, base: float, y_mid: float, label: str, pct: float | None, reset: str, *,
               head: bool = False, active: bool = False, secondaries: tuple = (),
               disabled: bool = False, expired: bool = False) -> None:
        """Window label, bar (+ one hairline per other window), %, reset."""
        if label:
            _t(pen, label, WIN_R, base, F_CAP, "secondary", right=True)
        s = _sev(RN, pct)
        if disabled:
            fill = "dead_fill"
        elif active and s == "ok":
            fill = "accent"
        else:
            fill = "dense_neutral" if s == "ok" else s
        bh = 6.0 if head else 5.0
        hair, hgap = 1.5, 2.0
        top = y_mid - (bh + len(secondaries) * (hair + hgap)) / 2.0
        pen.bar(BAR_X, top, BAR_W, bh, None if expired else pct, fill, "dense_track")
        y = top + bh
        for _l, p in secondaries:
            y += hgap
            ss = _sev(RN, p)
            pen.bar(BAR_X, y, BAR_W, hair, p, "dead_fill" if (ss == "ok" or disabled) else ss, "dense_track")
            y += hair
        if expired or pct is None:
            _t(pen, "—", PCT_R, base, F_NUM, "secondary", right=True)
        else:
            role = "secondary" if disabled else ("crit_text" if s == "crit" else "label")
            _t(pen, fmt_pct(pct), PCT_R, base, F_NUM_BIG if head else F_NUM, role, right=True)
        if reset:
            # left-aligned so the ↺ glyphs form one column down the menu
            _t(pen, reset, R - RESET_W, base, F_RESET, "secondary", maxw=RESET_W)

    # -- block builders ----------------------------------------------------------

    def sep() -> None:
        seps[0] += 1

        def paint(pen: Pen, w: float, h: float) -> None:
            # the geometry of the native separators the app puts around the cards
            pen.line(LEAD - 6, 5, w - 10, "sep")

        blocks.append(Block(SEP_H, paint, key=f"sep-{seps[0]}"))

    def section(title: str, right: str, key: str, tooltip: str = "") -> None:
        def paint(pen: Pen, w: float, h: float) -> None:
            _t(pen, title, X0, SECTION_BASE, F_HDR, "secondary")
            if right:
                _t(pen, right, R, SECTION_BASE, F_CAP_NUM, "secondary", right=True,
                   maxw=R - X0 - _w(pen, title, F_HDR) - 12)

        ax = f"{title}. {right}".strip(". ")
        blocks.append(Block(SECTION_H, paint, ax=ax, tooltip=tooltip or right, key=key))

    # ---- header module ---------------------------------------------------------
    active = snap.active
    vis = A._visible_quota_rows_of(snap)
    live_cx = [r for r in vis if r.vendor != A.VENDOR_CLAUDE and r.slot < 0]
    cx_active = next((r for r in live_cx if r.is_active), None)
    heads: list[tuple[Callable[[Pen, float], None], str, str, Callable[[], Any] | None]] = []

    if snap.accounts:
        target = next((r for r in A._switch_targets(snap.accounts)
                       if not r.disabled and r.slot not in notes), None)

        def p_claude(pen: Pen, oy: float) -> None:
            base = oy + HEAD_BASE
            pen.symbol("sparkle", STATE_C, base - 4.5, 10, "accent", "semibold", anchor="center")
            if active is None:
                _t(pen, "No active Claude account", X0, base, F_PRI_B, "secondary")
                return
            note = notes.get(active.slot)
            hb = A._binding_window(active, models) if not note else None
            alias_and_tags(pen, A._display_name(active), tags_claude(active), base, F_PRI_B,
                           alias_budget(pen, hb[0] if hb else "") if not note else 150)
            cap = [("Claude", "secondary")]
            if note:
                st = A._account_status_note(active, note, kinds.get(active.slot, "")).replace("⚠ ", "")
                _t(pen, st, R, base, F_SEC, "warn_text", right=True, maxw=R - X0 - 160)
            elif hb is None:
                _t(pen, "no window reported", R, base, F_SEC, "secondary", right=True)
            else:
                lab, pct = hb
                mark = A._reset_mark_text(A._window_reset(active, lab), now)
                metric(pen, base, base - 4.5, lab, pct, mark, head=True, active=True)
                for l, p, _m, e in claude_windows(active):
                    if l != lab:
                        cap.append((f"{l} {'overdue' if e else fmt_pct(p)}", "secondary"))
            spend = None if note else A._spend_pct(active)
            if spend is not None and spend >= RN.WARN_PCT:
                # real-money extra usage, in its severity colour (glance);
                # right after "Claude" so a long caption never truncates it
                cap.insert(1, (f"extra {fmt_pct(spend)}", _sev(RN, spend)))
            if target is not None:
                tb = A._binding_window(target, models)
                t = f"next → {A._display_name(target)}"
                if tb:
                    t += f" {fmt_pct(tb[1])}"
                cap.append((t, "secondary"))
            caption(pen, cap, X0, base + HEAD_CAP, R - X0)

        ax = "Claude, active " + (A._display_name(active) if active else "none")
        if active is not None:
            note = notes.get(active.slot)
            if note:
                ax += ". figures withheld: " + note
            else:
                ax += ". " + ", ".join(f"{l} {fmt_pct(p)}{' ended' if e else ''} {m}".strip()
                                       for l, p, m, e in claude_windows(active))
                if active.usage_is_stale:
                    ax += f". usage {A._age_label(active.usage_age_seconds)} old"
                spend = A._spend_pct(active)
                if spend is not None and spend >= RN.WARN_PCT:
                    ax += f". extra usage {fmt_pct(spend)}"
        heads.append((p_claude, ax, "claude-head", None))

    if live_cx:
        best = A.best_codex_row(live_cx) if A.best_codex_row else None

        def p_codex(pen: Pen, oy: float) -> None:
            base = oy + HEAD_BASE
            row = cx_active
            pen.symbol("terminal", STATE_C, base - 4.5, 10, "accent", "semibold", anchor="center")
            if row is None:
                _t(pen, "Codex login not identified", X0, base, F_PRI_B, "secondary")
                return
            hw = codex_window(row)
            alias_and_tags(pen, A._quota_alias(row), tags_codex(row), base, F_PRI_B,
                           alias_budget(pen, hw[0] if hw else ""))
            cap = [("Codex", "secondary")]
            if A._quota_alarm(row):
                kind = "crit_text" if row.attention_kind == "crit" else "warn_text"
                _t(pen, row.attention_note, R, base, F_SEC, kind, right=True, maxw=R - X0 - 160)
            elif hw is None:
                note, _k = A._quota_note(row)
                _t(pen, note or "no reading", R, base, F_SEC, "secondary", right=True)
            else:
                lab, pct, mark, exp = hw
                metric(pen, base, base - 4.5, lab, pct, mark, head=True, active=True, expired=exp)
            fact = A._codex_fact_line(row)
            if fact and fact[1] == "good":
                cap.append((fact[0].split(" — ")[0], "good"))
            if best is not None and best is not row:
                t = f"best → {A._quota_alias(best)}"
                if best.seven_day_pct is not None:
                    t += f" {fmt_pct(best.seven_day_pct)}"
                cap.append((t, "secondary"))
            caption(pen, cap, X0, base + HEAD_CAP, R - X0)

        if cx_active is None:
            ax = "Codex login not identified"
        else:
            ax = "Codex active " + A._quota_row_label(cx_active, reset_style="mark", plan_style="label", now=now)
        login = actions.codex_login_for(cx_active) if cx_active is not None else None
        heads.append((p_codex, ax, "codex-head", login))

    for i, (painter, ax, key, action) in enumerate(heads):
        first, last = i == 0, i == len(heads) - 1
        height = HEAD_H + (HEAD_PAD if first else 0.0) + (HEAD_PAD if last else 0.0)

        def p_head(pen: Pen, w: float, h: float, painter: Any = painter, first: bool = first,
                   last: bool = last) -> None:
            pen.panel(6, 0, W - 12, h, 8, "dense_module", top=first, bottom=last)
            painter(pen, HEAD_PAD if first else 0.0)

        tip = ax + (". Click to log in again." if action else "")
        blocks.append(Block(height, p_head, ax=tip, tooltip=tip, key=key, action=action))

    # ---- needs attention -----------------------------------------------------
    att = A.needs_attention_rows(snap)
    extras = tuple(x for x in actions.attention_extras if x)
    if att or extras:
        if blocks:
            sep()
        every = [notes[k] for vendor, k in att if vendor == "claude"]
        every += [k.attention_note for vendor, k in att if vendor != "claude"]
        section("Needs attention", "", "attention", "\n".join(every + list(extras)))
        by_slot = {r.slot: r for r in snap.accounts}
        for vendor, key in att:
            action: Callable[[], Any] | None = None
            if vendor == "claude":
                row = by_slot.get(key)
                note = notes[key]
                kind = kinds.get(key, "")
                vname = A._display_name(row) if row else f"slot {key}"
                status = (A._account_status_note(row, note, kind) if row
                          else A._title_note(note, kind)).replace("⚠ ", "")
                verb = "Re-login" if "relogin" in status else "Fix"
                color = "warn"
                bkey = f"att-claude-{key}"
            else:
                row = key
                note = status = row.attention_note
                verb = "Log in again" if "relogin" in status else "Fix"
                color = "crit" if row.attention_kind == "crit" else "warn"
                vname = A._quota_alias(row)
                action = actions.codex_login_for(row)
                bkey = f"att-codex-{vname}"

            def p_att(pen: Pen, w: float, h: float, vname: str = vname, status: str = status,
                      verb: str = verb, color: str = color, vendor: str = vendor,
                      clickable: bool = action is not None) -> None:
                base = ROW_BASE
                pen.symbol("exclamationmark.triangle.fill", STATE_C, base - 4.5, 10, color, "semibold",
                           anchor="center")
                x = X0 + _t(pen, vname, X0, base, F_PRI, "label", maxw=110)
                vend = "Claude" if vendor == "claude" else "Codex"
                aw = _w(pen, verb, F_SEC) + 16
                _t(pen, f"{vend} · {status}", x + 8, base, F_SEC, "secondary", maxw=R - aw - x - 14)
                if clickable:
                    _t(pen, verb, R - 12, base, F_SEC, "label", right=True)
                    pen.symbol("chevron.right", R - 3, base - 4, 9, "secondary", "semibold", anchor="center")
                else:
                    # the remedy is named, but nothing here can perform it: no chevron
                    _t(pen, verb, R, base, F_SEC, "label", right=True)

            ax = f"Needs attention: {vname}, {vendor}, {status}"
            if action is not None:
                ax += f". Click to {verb.lower()}."
            blocks.append(Block(ROW_H, p_att, ax=ax, tooltip=note, key=bkey, action=action))

        for i, line in enumerate(extras):
            text = line
            icon, role = "info.circle", "secondary"
            if text.startswith("⛔"):
                icon, role, text = "xmark.octagon.fill", "crit", text[1:].strip()
            elif text.startswith("⚠"):
                icon, role, text = "exclamationmark.triangle.fill", "warn", text[1:].strip()

            def p_extra(pen: Pen, w: float, h: float, text: str = text, icon: str = icon,
                        role: str = role) -> None:
                base = ROW_BASE
                pen.symbol(icon, STATE_C, base - 4.5, 10, role, "semibold", anchor="center")
                _t(pen, text, X0, base, F_SEC, "secondary", maxw=R - X0)

            blocks.append(Block(ROW_H, p_extra, ax=f"Needs attention: {line}", tooltip=line,
                                key=f"att-extra-{i}"))

    # ---- Claude accounts -----------------------------------------------------
    rows = [r for r in A._switch_targets(snap.accounts) if r.slot not in notes]
    if rows:
        if blocks:
            sep()
        room = A._claude_room(snap)
        right = []
        if room:
            right.append(f"{room[0]}/{room[1]} room")
        if snap.settings.get("scoped_fleet_line_enabled", True):
            names: list[str] = []
            for r in snap.accounts:
                for n, _p in r.scoped_windows:
                    if n not in names:
                        names.append(n)
            for n in names:
                heading = A._scoped_fleet_heading(snap.accounts, name=n, now=now, notes=notes)
                if heading:
                    right.append(heading)
        section("Claude", "  ·  ".join(right), "sec-claude")
        for row in rows:
            def p_row(pen: Pen, w: float, h: float, row: Any = row) -> None:
                base = ROW_BASE
                _t(pen, str(row.slot), STATE_C + 3, base, F_CAP_NUM, "secondary", right=True)
                b = A._binding_window(row, models)
                alias_and_tags(pen, A._display_name(row), tags_claude(row), base, F_PRI,
                               alias_budget(pen, b[0] if b else ""))
                if b is None:
                    _t(pen, "no live window", R, base, F_SEC, "secondary", right=True)
                    return
                lab, pct = b
                secs = tuple((l, p) for l, p, _m, e in claude_windows(row) if l != lab and not e)
                mark = A._reset_mark_text(A._window_reset(row, lab), now)
                metric(pen, base, base - 4.5, lab, pct, mark, secondaries=secs, disabled=row.disabled)

            ax = f"Claude {A._display_name(row)} slot {row.slot}. " + ", ".join(
                f"{l} {fmt_pct(p)}{' ended' if e else ''} {m}".strip() for l, p, m, e in claude_windows(row))
            if row.usage_is_stale:
                ax += f". usage {A._age_label(row.usage_age_seconds)} old"
            if row.disabled:
                ax += ". disabled"
            blocks.append(Block(ROW_H, p_row, ax=ax + ". Click to switch.", tooltip=ax,
                                key=f"claude-{row.slot}",
                                action=(lambda slot=row.slot: actions.switch_to(slot))))

    # ---- Codex accounts --------------------------------------------------------
    cx_rows = [r for r in live_cx if not r.is_active and not A._quota_alarm(r)]
    if cx_rows:
        if blocks:
            sep()
        # "Codex fleet line" OFF: the section is the rows alone, as in glance.
        parts = (A._codex_fleet_parts(vis, now=now)
                 if snap.settings.get("codex_fleet_line_enabled", True) else None)
        right = []
        if parts is not None:
            right.append(f"{parts.room}/{parts.total} room")
            mark = RN.reset_mark(parts.next_at, now)
            if mark:
                right.append(f"next {mark} {parts.next_alias}")
            if parts.resets:
                right.append(parts.resets)
        section("Codex", "  ·  ".join(right), "sec-codex")
        for row in cx_rows:
            def p_cx(pen: Pen, w: float, h: float, row: Any = row) -> None:
                base = ROW_BASE
                cw = codex_window(row)
                alias_and_tags(pen, A._quota_alias(row), tags_codex(row), base, F_PRI,
                               alias_budget(pen, cw[0] if cw else ""))
                if cw is None:
                    note, _k = A._quota_note(row)
                    _t(pen, note or "no reading", R, base, F_SEC, "secondary", right=True)
                    return
                lab, pct, mark, exp = cw
                metric(pen, base, base - 4.5, lab, pct, mark, expired=exp, disabled=row.disabled)

            tip = A._quota_row_label(row, reset_style="mark", plan_style="label", now=now)
            login = actions.codex_login_for(row)
            blocks.append(Block(ROW_H, p_cx, ax="Codex " + tip + (". Click to log in again." if login else ""),
                                tooltip=tip, key=f"codex-{row.alias}", action=login))

    if not blocks:
        def p_empty(pen: Pen, w: float, h: float) -> None:
            _t(pen, "No accounts reported", X0, ROW_BASE, F_PRI, "secondary")

        blocks.append(Block(ROW_H, p_empty, ax="No accounts reported", key="empty"))
    return blocks
