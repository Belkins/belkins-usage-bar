"""The "cards" menu theme (design 3, 2026-09-25): provider cards, CodexBar-like.

A port of design2 ANGLE A (``evidence/design2/cards/proto.py``) onto
:mod:`card_base`: a slim *Needs attention* card (design 4: the same neutral
panel as the others, marked by an orange leading rule), then one rounded card
per provider - the active account large (name, big binding figure, its
bars), the other accounts as compact rows under a hairline - with the common
native tail appended by the app.

A card spans several menu items (one :class:`Block` each, so a row can be
clicked and highlighted on its own). Every block paints the WHOLE card's
rounded rect shifted to its own offset and clipped to its bounds, so the
slices join into one card with true 10 pt corners at any slice height.

Clickable, and only these (everything else is static text):

- a Claude account row -> ``actions.switch_to(slot)``;
- a Codex row whose login is dead (attention card, Codex card, or the active
  Codex hero) -> ``actions.codex_login_for(row)``.

"Add credits" is shown verbatim where upstream says it, but is NOT
clickable: the codebase has no Add-credits URL and a theme never invents one.

Figures come from the app's own pure helpers (``_binding_window``,
``_claude_room``, ``_codex_fleet_parts`` ...), imported lazily inside
:func:`build` (``app`` imports this package).
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ..card_base import W, Block, Pen, register_role
from . import ThemeActions

# ---------------------------------------------------------------- layout ---
CARD_X = 10.0          # card edge; the native highlight is inset 5, text sits at 20
PAD = 16.0             # card inner padding (design 5: 10 -> 16, more air) -> text x = 26
CARD_R = 10.0
GAP = 10.0             # between cards (design 5: 6 -> 10)
TOP_GAP = 1.0          # first card: 5 pt material pad + 1 = the prototype's 6
TAIL_GAP = 0.0         # after the last card; the app's separator item follows
X0 = CARD_X + PAD                          # 26
X1 = W - CARD_X - PAD                      # 334
W_INNER = X1 - X0                          # 308
HX = X0 + 12                               # hanging indent under a name
COL_RESET_W = 80.0
COL_PCT_R = X1 - COL_RESET_W - 4           # 250: pct right edge
COL_PCT_W = 40.0                                     # "100%" at 13 pt semibold rounded
COL_BAR_W = 48.0
COL_BAR_X = COL_PCT_R - COL_PCT_W - 6 - COL_BAR_W   # 156
COL_LABEL_R = COL_BAR_X - 6                          # 150
CAP_STEP = 14.0                                      # caption line pitch (11 pt captions)
# Design 5 (2026-09-25, "more spacing and air"): a 4-pt-ish vertical rhythm.
HEAD_BASE = 25.0       # card title baseline from the card top: ~16 pt of air above the caps
HEAD_AFTER = 16.0      # title (or its caption line) baseline -> the next part (was 10)
WIN_LABEL_W = 44.0     # hero window rows: label column ("5h", "Fable", "weekly") before the bar
WIN_STEP = 24.0        # hero window-row pitch (was 18)
ROW_BASE = 18.0        # account row: name baseline from the row top (row pitch 30, was 25)
ROW_CAP = 16.0         # account row: name baseline -> first caption baseline (4 pt of air)
ROW_END = 12.0         # last baseline -> the row's bottom
INK = 3.0              # descender allowance: a baseline + INK is the ink bottom
PART_END = 9.0         # ink bottom -> the end of a hero part (the hairline or the foot follows)
ACCENT_W = 3.0                                       # Needs attention's leading orange rule
ROW_HL = {"hl_inset": CARD_X + 4, "hl_vinset": 1.0, "hl_radius": 6.0}

# ------------------------------------------------------------------ fonts ---
# key -> (size, weight, mono digits, SF Mono, SF Pro Rounded)
# Design 4 (2026-09-25): percent figures are SF Pro Rounded with monospaced
# digits; names 14 semibold (hero) / 13 medium (rows); captions 11 regular.
F: dict[str, tuple[float, str, bool, bool, bool]] = {
    "title": (13, "semibold", False, False, False),
    "hero_name": (14, "semibold", False, False, False),
    "hero_big": (26, "semibold", True, False, True),
    "primary": (13, "regular", False, False, False),
    "primary_med": (13, "medium", False, False, False),
    "secondary": (11, "regular", False, False, False),
    "secondary_med": (11, "medium", False, False, False),
    "caption": (11, "regular", False, False, False),
    "caption_med": (11, "medium", False, False, False),
    "num": (13, "semibold", True, False, True),
    "num_bar": (11, "semibold", True, False, True),
    "num_small": (11, "regular", True, False, False),
    "slot": (11, "medium", True, False, False),
    "code": (10.5, "medium", False, True, False),
    "tag": (10, "semibold", False, False, False),
}

# ---------------------------------------------------------------- colours ---
# The prototype's exact values. Text roles come from card_base (label,
# secondary, tertiary, accent, *_text); these are the card surfaces + fills.
_WHITE = (255, 255, 255)
# Design 4: every card is a near-opaque NEUTRAL panel, so the wallpaper tint
# of the menu material never reaches the content (a 0.62 white card let a pink
# wallpaper through and an amber attention tint turned it beige).
_ROLES: tuple[tuple[str, tuple, tuple, Any], ...] = (
    ("cards_card", (255, 255, 255, 0.88), (255, 255, 255, 0.07), None),
    ("cards_card_edge", (0, 0, 0, 0.06), (255, 255, 255, 0.08), None),
    ("cards_attn_rule", (255, 149, 0), (255, 159, 10), None),
    ("cards_good_chip", (40, 167, 69, 0.12), (48, 209, 88, 0.16), "clear"),
    ("cards_hair", (0, 0, 0, 0.08), (255, 255, 255, 0.09), None),
    ("cards_track", (0, 0, 0, 0.10), (255, 255, 255, 0.14), (255, 255, 255, 0.30)),
    ("cards_warn_fill", (255, 149, 0), (255, 159, 10), _WHITE),
    ("cards_crit_fill", (255, 59, 48), (255, 69, 58), _WHITE),
    ("cards_good_fill", (40, 167, 69), (48, 209, 88), _WHITE),
)
# One severity language: bar fills ...  ("ok" = systemBlue, card_base's bar
# blue; the prototype used the accent, identical at the default accent)
SEV_FILL = {"ok": "ok", "warn": "cards_warn_fill", "crit": "cards_crit_fill",
            "good": "cards_good_fill", "dim": "dead_fill"}
# ... and the same hues as TEXT, darkened in light mode for contrast.
SEV_TEXT = {"ok": "label", "warn": "warn_text", "crit": "crit_text", "good": "good_text",
            "dim": "secondary"}
CAP_ROLE = {"dim": "secondary", "accent": "accent", "ok": "secondary", "tag": "accent"}

_roles_done = False


def _ensure_roles() -> None:
    global _roles_done
    if _roles_done:
        return
    for name, light, dark, hover in _ROLES:
        register_role(name, light, dark, hover=hover)
    _roles_done = True


# ---------------------------------------------------------- draw helpers ---


class _Measure:
    """A Pen stand-in for the layout pass: measures text, draws nothing."""

    hl = False

    def __init__(self) -> None:
        self._pen = Pen()

    def width(self, *a: Any, **k: Any) -> float:
        return self._pen.width(*a, **k)

    def fit(self, *a: Any, **k: Any) -> str:
        return self._pen.fit(*a, **k)

    def text(self, s: str, x: float, y: float, size: float, weight: Any = "regular", role: Any = "label",
             *, mono: bool = False, right: bool = False, center: bool = False, maxw: float | None = None,
             code: bool = False, kern: float | None = None, rounded: bool = False) -> float:
        if not s:
            return 0.0
        s = self._pen.fit(s, size, weight, maxw, mono=mono, code=code, kern=kern, rounded=rounded)
        return self._pen.width(s, size, weight, mono=mono, code=code, kern=kern, rounded=rounded) if s else 0.0

    def chip(self, text: str, x: float, y: float, size: float = 10.0, *, role: Any = "secondary",
             bg: Any = "chip", maxw: float | None = None, weight: Any = "regular") -> float:
        if not text:
            return 0.0
        inner = None if maxw is None else max(0.0, maxw - 6.0)
        return self._pen.width(self._pen.fit(text, size, weight, inner, code=True), size, weight,
                               code=True) + 6.0

    def pill(self, text: str, x: float, y: float, size: float = 10.0, *, role: Any = "accent_tag",
             bg: Any = "accent_pill", weight: Any = "semibold") -> float:
        return self._pen.width(text, size, weight) + 10.0 if text else 0.0

    def symbol(self, name: str, x: float, y: float, size: float, *a: Any, **k: Any) -> float:
        return size

    def _nothing(self, *a: Any, **k: Any) -> None:
        return None

    rrect = panel = bar = circle = line = _nothing


def text(pen: Any, s: str, fk: str, role: str, x: float, baseline: float, *,
         maxw: float | None = None, right: bool = False) -> float:
    """The prototype's ``text()``: font by key, baseline-positioned."""
    if not s or (maxw is not None and maxw <= 0):
        return 0.0
    size, weight, mono, code, rounded = F[fk]
    return pen.text(s, x, baseline, size, weight, role, mono=mono, code=code, right=right, maxw=maxw,
                    rounded=rounded)


def text_w(pen: Any, s: str, fk: str) -> float:
    size, weight, mono, code, rounded = F[fk]
    return pen.width(s, size, weight, mono=mono, code=code, rounded=rounded)


def segments(pen: Any, segs: list[tuple], x: float, baseline: float, maxw: float) -> float:
    """``[(text, font_key, role, style?)]`` left to right; style ``"code"`` is a chip."""
    cur, end = x, x + maxw
    for s, fk, role, *style in segs:
        room = end - cur
        if room <= 6:
            break
        if style and style[0] == "code":
            cur += pen.chip(s, cur, baseline, F[fk][0], role=role, maxw=room, weight=F[fk][1])
        else:
            cur += text(pen, s, fk, role, cur, baseline, maxw=room)
    return cur - x


def bar(pen: Any, x: float, mid: float, w: float, pct: float | None, sev: str, h: float = 5.0,
        expired: bool = False) -> None:
    """A real bar centred on *mid*: quiet track, a fill by severity; None -> track only."""
    pen.bar(x, mid - h / 2, w, h, pct, "dead_fill" if expired else SEV_FILL[sev], "cards_track")


def symbol(pen: Any, name: str, x: float, mid: float, size: float, role: str,
           weight: str = "semibold") -> float:
    return pen.symbol(name, x, mid, size, role, weight, anchor="leftmid")


def hairline(pen: Any, x0: float, x1: float, y: float) -> None:
    pen.line(x0, y, x1, "cards_hair", thickness=0.5)


def _pill_w(pen: Any, s: str) -> float:
    return text_w(pen, s, "tag") + 10.0


def flow(pen: Any, caps: list[tuple[str, str]], x: float, baseline: float, maxw: float) -> int:
    """Captions on as few 11 pt lines as fit, joined by a quiet ' · '; a
    ``tag`` caption (next, best) is a pill and needs no separator. Returns the
    line count."""
    lines, cur = 1, x
    sep = "  ·  "
    sep_w = text_w(pen, sep, "caption")
    prev_tag = False
    for i, (cap, kind) in enumerate(caps):
        tag = kind == "tag"
        fk = "caption_med" if kind in ("accent", "good", "crit", "warn") else "caption"
        w = _pill_w(pen, cap) if tag else text_w(pen, cap, fk)
        gap = 6.0 if (tag or prev_tag) else sep_w
        if i and cur + gap + w > x + maxw:
            lines += 1
            baseline += CAP_STEP
            cur = x
        elif i:
            if not (tag or prev_tag):
                text(pen, sep, "caption", "tertiary", cur, baseline)
            cur += gap
        if tag:
            cur += pen.pill(cap, cur, baseline - 0.5, F["tag"][0], weight=F["tag"][1])
        else:
            cur += text(pen, cap, fk, CAP_ROLE.get(kind, SEV_TEXT.get(kind, "secondary")), cur, baseline,
                        maxw=x + maxw - cur)
        prev_tag = tag
    return lines


def _clip_to(w: float, h: float, draw: Callable[[], None]) -> None:
    """Run *draw* clipped to the block's bounds (views may not clip themselves)."""
    from AppKit import NSBezierPath, NSGraphicsContext
    from Foundation import NSMakeRect

    NSGraphicsContext.saveGraphicsState()
    try:
        NSBezierPath.clipRect_(NSMakeRect(0, 0, w, h))
        draw()
    finally:
        NSGraphicsContext.restoreGraphicsState()


# ------------------------------------------------------------------ cards ---


class _Card:
    """One rounded card spread over consecutive blocks ("parts")."""

    def __init__(self, fill: str, edge: str, rule: str | None = None) -> None:
        self.fill, self.edge, self.rule = fill, edge, rule
        self.parts: list[tuple[float, Callable[[Any, float], None], dict[str, Any]]] = []

    def add(self, draw: Callable[[Any, float], float], **block_kw: Any) -> None:
        """Add a part: ``draw(pen, y0) -> end_y`` lays it out from *y0*; its
        height is measured once now (text widths decide caption wrapping)."""
        height = float(round(draw(_Measure(), 0.0)))
        self.parts.append((height, draw, block_kw))

    def blocks(self, gap_before: float, gap_after: float) -> list[Block]:
        card_h = sum(h for h, _, _ in self.parts)
        out: list[Block] = []
        off = 0.0
        last = len(self.parts) - 1
        for i, (h, draw, kw) in enumerate(self.parts):
            lead = gap_before if i == 0 else 0.0
            trail = gap_after if i == last else 0.0
            out.append(Block(lead + h + trail, self._painter(draw, lead - off, lead, h, card_h, kw), **kw))
            off += h
        return out

    def _painter(self, draw: Callable[[Any, float], float], card_y: float, lead: float, h: float,
                 card_h: float, kw: dict[str, Any]) -> Callable[[Pen, float, float], None]:
        fill, edge, rule = self.fill, self.edge, self.rule
        inset = kw.get("hl_inset", ROW_HL["hl_inset"])
        vinset = kw.get("hl_vinset", ROW_HL["hl_vinset"])
        radius = kw.get("hl_radius", ROW_HL["hl_radius"])

        def paint(pen: Pen, w: float, bh: float) -> None:
            def surface() -> None:
                pen.rrect(CARD_X, card_y, w - 2 * CARD_X, card_h, CARD_R, fill, stroke=edge)
                if rule:  # a leading accent rule, clipped to the card's rounded edge
                    from AppKit import NSBezierPath
                    from Foundation import NSMakeRect

                    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                        NSMakeRect(CARD_X, card_y, w - 2 * CARD_X, card_h), CARD_R, CARD_R).addClip()
                    pen.rrect(CARD_X, card_y, ACCENT_W, card_h, 0.0, rule)
            _clip_to(w, bh, surface)
            if pen.hl:  # the card fill covered BlockView's highlight: draw it again on top
                pen.rrect(inset, lead + vinset, w - 2 * inset, h - 2 * vinset, radius, "hl")
            draw(pen, lead)

        return paint


# ------------------------------------------------------------- the specs ---
# Pure data built from the snapshot with the widget's own helpers.


def _short_state(note: str, kind: str) -> tuple[str, str]:
    """(state, cause) from a claude-swap note: 're-login needed' / 'HTTP 429 for 12 days'."""
    head, _, tail = note.partition(" — ")
    if kind == "fetch-failing":
        code = re.search(r"HTTP (\d{3})", note)
        days = re.search(r"for (\d+ days?)", note)
        state = f"HTTP {code.group(1)}" if code else "usage refused"
        if days:
            state += f" for {days.group(1)}"
        polls = re.search(r"(\d[\d,]*) failed polls", note)
        return state, (f"{polls.group(1)} failed polls" if polls else "")
    cause = tail.split(";")[0].strip() if tail else ""
    return head.strip(), cause


def _short_action(note: str, kind: str) -> list[tuple]:
    """The short remedy, as segments; commands come out of the note verbatim."""
    cmds = re.findall(r"`([^`]+)`", note) or re.findall(r"run: (cswap [a-z]+(?: \d+)?)", note)
    if kind == "fetch-failing":
        segs: list[tuple] = [("Log in again, then ", "secondary", "secondary")]
        if cmds:
            segs.append((cmds[0], "code", "secondary", "code"))
        rm = next((c for c in cmds if c.startswith("cswap remove")), None)
        if rm:
            segs += [("  or stop: ", "secondary", "secondary"), (rm, "code", "secondary", "code")]
        return segs
    if "re-login" in (kind + note).lower():
        segs = [("Log in with Claude Code, then ", "secondary", "secondary")]
        if cmds:
            segs.append((cmds[-1], "code", "secondary", "code"))
        return segs
    return [(note.split(" — ")[-1][:80], "secondary", "secondary")]


def _attention_spec(A: Any, snap: Any, actions: ThemeActions) -> dict[str, Any] | None:
    items = []
    by_slot = {r.slot: r for r in snap.accounts}
    for vendor, ref in A.needs_attention_rows(snap):
        if vendor == "claude":
            row = by_slot.get(ref)
            note = snap.account_notes.get(ref, "")
            kind = snap.account_note_kinds.get(ref, "")
            state, cause = _short_state(note, kind)
            age = f"{A._age_label(row.usage_age_seconds)} ago" if row and row.usage_age_seconds is not None else ""
            name = A._display_name(row) if row else f"slot {ref}"
            items.append({
                "key": f"cards-attn-claude-{ref}", "name": name, "provider": "Claude",
                "state": state, "state_role": "warn_text", "cause": cause, "age": age,
                "action_segs": _short_action(note, kind), "action": None,
                "tip": f"{name} (slot {ref}) · {note}",
            })
        else:
            row = ref
            act = actions.codex_login_for(row)
            login = getattr(A, "_codex_login", None)
            detail = login.relogin_detail(row) if login is not None else ""
            if act is not None:
                segs = [("Log in again…", "secondary_med", "accent")]
            else:
                segs = [(detail, "secondary", "secondary")] if detail else []
            items.append({
                "key": f"cards-attn-codex-{row.slot}", "name": A._quota_alias(row), "provider": "Codex",
                "state": row.attention_note,
                "state_role": "crit_text" if row.attention_kind == "crit" else "warn_text",
                "cause": detail,
                "age": f"{A._age_label(row.usage_age_seconds)} ago" if row.usage_age_seconds else "",
                "action_segs": segs, "action": act,
                "tip": "\n".join(x for x in (f"Codex {A._quota_head(row)} · {row.attention_note}", detail) if x),
            })
    extras = [e for e in actions.attention_extras if e]
    if not items and not extras:
        return None
    return {"items": items, "extras": extras}


def _claude_bars(A: Any, row: Any, now: float) -> list[tuple[str, float, str, bool]]:
    dead = set(row.expired_windows or ())
    out = []
    for label, key, pct, raw in (("5h", "five_hour", row.five_hour_pct, row.five_hour_resets_at),
                                 ("7d", "seven_day", row.seven_day_pct, row.seven_day_resets_at)):
        if pct is not None:
            out.append((label, pct, A._reset_mark_text(raw, now), key in dead))
    for name, pct in row.scoped_windows:
        if pct is not None:
            raw = next((r for n, r in row.scoped_resets_at if n == name), None)
            out.append((name, pct, A._reset_mark_text(raw, now), name in dead))
    return out


def _claude_tip(A: Any, row: Any, now: float, note: str = "") -> str:
    from ..contracts import format_pct

    parts = [f"{A._display_name(row)} · slot {row.slot} · {row.email}"]
    if note:
        parts.append(note)
    else:
        for label, pct, mark, expired in _claude_bars(A, row, now):
            parts.append(f"{label} {format_pct(pct)}{' (window ended)' if expired else ''} {mark}".strip())
    if row.usage_is_stale:
        parts.append(f"reading {A._age_label(row.usage_age_seconds)} old")
    if row.disabled:
        parts.append("disabled (cswap disable): never auto-selected, still polled")
    parts.append("active login" if row.is_active else "click to switch to this account")
    return "\n".join(parts)


def _claude_spec(A: Any, R: Any, snap: Any, now: float, actions: ThemeActions) -> dict[str, Any] | None:
    from ..contracts import format_pct

    if not snap.accounts:
        return None
    models = tuple(snap.autoswitch_models or ())
    notes = snap.account_notes
    active = snap.active
    room = A._claude_room(snap)
    right = []
    if room:
        right.append(f"{room[0]}/{room[1]} room")
    if snap.settings.get("scoped_fleet_line_enabled", True):
        scoped = dict.fromkeys(n for r in snap.accounts for n, _ in r.scoped_windows)
        right += [h for h in (A._scoped_fleet_heading(snap.accounts, name=n, now=now, notes=notes)
                              for n in scoped) if h]
    hero = None
    if active is not None:
        note = notes.get(active.slot, "")
        binding = None if note else A._binding_window(active, models)
        captions = []
        if active.usage_is_stale and not note:
            captions.append((f"reading {A._age_label(active.usage_age_seconds)} old", "dim"))
        spend = A._spend_of(active)
        if spend:
            used, limit, cur = spend
            sp = A._spend_pct(active)
            captions.append((f"extra usage {A._money(used, cur)} of {A._money(limit, cur)}",
                             R.severity(sp) if sp else "dim"))
        hero = {
            "name": A._display_name(active), "slot": active.slot, "plan": "", "sub": active.email,
            "big": (format_pct(binding[1]), binding[0], R.severity(binding[1])) if binding else None,
            "status": (A._account_status_note(active, note, snap.account_note_kinds.get(active.slot, "")), "warn")
            if note else None,
            "bars": [] if note else _claude_bars(A, active, now),
            "captions": captions,
            "tip": _claude_tip(A, active, now, note),
            "action": None,
        }
    targets = [r for r in A._switch_targets(snap.accounts) if r.slot not in notes]
    nxt = next((r for r in targets if not r.disabled), None)
    rows = []
    for r in targets:
        binding = A._binding_window(r, models)
        caps = []
        if r is nxt:
            caps.append(("next", "tag"))
        if r.disabled:
            caps.append(("disabled · not in rotation", "dim"))
        if r.usage_is_stale:
            caps.append((f"{A._age_label(r.usage_age_seconds)} old", "dim"))
        rows.append({
            "key": f"cards-claude-{r.slot}", "slot": str(r.slot), "name": A._display_name(r), "plan": "",
            "label": binding[0] if binding else "",
            "pct": binding[1] if binding else None,
            "sev": R.severity(binding[1]) if binding else "dim",
            "reset": A._reset_mark_text(A._window_reset(r, binding[0]), now) if binding else "",
            "withheld": None, "captions": caps, "tip": _claude_tip(A, r, now),
            "action": (lambda slot=r.slot: actions.switch_to(slot)),
        })
    return {"title": "Claude", "icon": "sparkle", "right": " · ".join(right), "right_parts": right,
            "hero": hero, "rows": rows, "key": "claude"}


def _codex_caps(A: Any, row: Any) -> list[tuple[str, str]]:
    caps = []
    if row.attention_note and row.attention_kind == "crit":
        caps.append((row.attention_note, "crit"))
    for line in row.info_notes or ():
        if (line.startswith("↺") and "usable now" in line) or re.search(r"\bback\b", line):
            caps.append((line, "good"))
        elif line.startswith(A._PACE_NOTE_PREFIX):
            caps.append((line, "warn"))
        else:
            caps.append((line, "dim"))
    if row.usage_is_stale:
        caps.append((f"{A._age_label(row.usage_age_seconds)} old", "dim"))
    if row.disabled:
        caps.append(("disabled", "dim"))
    return caps


def _codex_tip(A: Any, row: Any, now: float) -> str:
    from ..contracts import format_pct

    lines = [f"Codex {A._quota_head(row)} · {row.email}"]
    win = A._plan_window(row)
    if win and win[1] is not None:
        lines.append(f"{win[0]} {format_pct(win[1])} {A._quota_reset_mark(row, now)}")
    note, _ = A._quota_note(row)
    if note:
        lines.append(note)
    lines += list(row.info_notes or ())
    lines.append("active in ~/.codex" if row.is_active else "switch in Codex: Switch account ▸")
    return "\n".join(lines)


def _codex_spec(A: Any, R: Any, snap: Any, now: float, actions: ThemeActions) -> dict[str, Any] | None:
    from ..contracts import format_pct

    rows_all = A._visible_quota_rows_of(snap)
    live = [r for r in rows_all if r.vendor != A.VENDOR_CLAUDE and r.slot < 0]
    if not live:
        return None
    # "Codex fleet line" OFF: no room / next / resets facts, as in glance.
    parts = (A._codex_fleet_parts(rows_all, now=now)
             if snap.settings.get("codex_fleet_line_enabled", True) else None)
    right = []
    if parts:
        right.append(f"{parts.room}/{parts.total} room")
        mark = R.reset_mark(parts.next_at, now)
        if mark:
            right.append(f"next {mark}" + (f" ({parts.next_alias})" if parts.next_alias else ""))
        if parts.resets:
            right.append(parts.resets)
    active = next((r for r in live if r.is_active), None)
    best_of = getattr(A, "best_codex_row", None)
    best = best_of(live) if best_of is not None else None
    hero = None
    if active is not None:
        win = A._plan_window(active)
        alarm = A._quota_alarm(active)
        known = bool(win) and win[1] is not None and not alarm
        act = actions.codex_login_for(active)
        caps = _codex_caps(A, active)
        if act is not None:
            caps.append(("Log in again…", "accent"))
        hero = {
            "name": A._quota_alias(active), "plan": A.plan_label(active.plan_type), "slot": None,
            "sub": active.email,
            "big": (format_pct(win[1]), win[0], "dim" if win[2] else R.severity(win[1])) if known else None,
            "status": (active.attention_note, "warn") if alarm else None,
            "bars": [(win[0], win[1], A._quota_reset_mark(active, now), win[2])] if known else [],
            "captions": caps,
            "tip": _codex_tip(A, active, now),
            "action": act,
        }
    rows = []
    for r in live:
        # An alarmed login is listed once, in Needs attention (with its Log
        # in again action), not again in this card - as glance and dense do.
        if r is active or A._quota_alarm(r):
            continue
        win = A._plan_window(r)
        act = actions.codex_login_for(r)
        caps = _codex_caps(A, r)
        if r is best:
            caps.insert(0, ("best", "tag"))
        if act is not None:
            caps.append(("Log in again…", "accent"))
        rows.append({
            "key": f"cards-codex-{r.slot}", "slot": "", "name": A._quota_alias(r),
            "plan": A.plan_label(r.plan_type),
            "label": "", "pct": win[1] if win else None,
            "sev": "dim" if (not win or win[2]) else R.severity(win[1]),
            "reset": A._quota_reset_mark(r, now) if win else "",
            "withheld": None,
            "captions": caps,
            "tip": _codex_tip(A, r, now),
            "action": act,
        })
    return {"title": "Codex", "icon": "terminal", "right": " · ".join(right), "right_parts": right,
            "hero": hero, "rows": rows, "key": "codex"}


# ---------------------------------------------------------------- painters ---
# Each ``draw(pen, y0) -> end_y`` lays one part out from y0 (block coords).


def _attn_head(count: int) -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        base = y0 + HEAD_BASE
        symbol(pen, "exclamationmark.triangle.fill", X0, base - 4.5, 12, "cards_warn_fill")
        text(pen, "Needs attention", "title", "label", X0 + 19, base)
        text(pen, str(count), "num_bar", "secondary", X1, base, right=True)
        return base + 10
    return draw


def _attn_item(it: dict[str, Any]) -> Callable[[Any, float], float]:
    chevron = it["action"] is not None

    def draw(pen: Any, y0: float) -> float:
        b1 = y0 + 17
        wname = text(pen, it["name"], "primary_med", "label", X0, b1, maxw=120)
        age_w = text_w(pen, it["age"], "secondary") if it["age"] else 0.0
        if chevron:
            chev_x = X1 - 6
            age_r = chev_x - 8
            symbol(pen, "chevron.right", chev_x, b1 - 4, 9, "secondary")
        else:  # no action -> no chevron: nothing here opens anything
            age_r = X1
        text(pen, it["age"], "secondary", "secondary", age_r, b1, right=True)
        sx = X0 + wname + 6
        text(pen, it["state"], "primary", it["state_role"], sx, b1, maxw=age_r - age_w - 8 - sx)
        if not it["action_segs"]:
            return b1 + ROW_END
        b2 = b1 + 18  # the remedy line: ~3 pt under the name line (its chips are taller)
        # the chevron column is only reserved when there is a chevron
        segments(pen, it["action_segs"], X0, b2, W_INNER - (14 if chevron else 0))
        return b2 + ROW_END
    return draw


def _attn_extra(line: str) -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        b = y0 + 17
        text(pen, line.split("\n")[0], "primary", "label", X0, b, maxw=W_INNER)
        return b + ROW_END
    return draw


def _gap(h: float) -> Callable[[Any, float], float]:
    return lambda pen, y0: y0 + h


def _hair() -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        hairline(pen, X0, X1, y0 + 2)
        return y0 + 3
    return draw


def _card_head(spec: dict[str, Any]) -> Callable[[Any, float], float]:
    def draw(pen: Any, y0: float) -> float:
        base = y0 + HEAD_BASE
        if spec.get("icon"):
            symbol(pen, spec["icon"], X0, base - 4.5, 12, "label", "medium")
        tx = X0 + (18 if spec.get("icon") else 0)
        tw = text(pen, spec["title"], "title", "label", tx, base)
        room = X1 - (tx + tw + 12)
        parts = spec.get("right_parts") or []
        full = spec["right"]
        if len(parts) < 2 or text_w(pen, full, "secondary") <= room:
            text(pen, full, "secondary", "secondary", X1, base, maxw=room, right=True)
            return base + HEAD_AFTER
        # Design 5: facts that do not fit beside the title are never truncated
        # there - the first (room) stays in the header, the rest get their own
        # caption line under the title.
        text(pen, parts[0], "secondary", "secondary", X1, base, maxw=room, right=True)
        cap = base + 17
        text(pen, " · ".join(parts[1:]), "secondary", "secondary", tx, cap, maxw=X1 - tx)
        return cap + HEAD_AFTER
    return draw


def _hero(hero: dict[str, Any]) -> Callable[[Any, float], float]:
    from ..contracts import format_pct
    from .. import render as R

    def draw(pen: Any, y0: float) -> float:
        b1 = y0 + 14          # name baseline
        b2 = b1 + 20          # caption baseline (design 5: +4 pt from the name)
        big = hero["big"]
        bw = 0.0
        if big:
            pct, _label, sev = big
            # Neutral: severity lives in the bar (a coloured 26 pt figure read
            # brown on a tinted menu, design 4). An ended window's figure is a
            # dead reading and stays dimmed, never label ink. Design 5: the
            # figure is centred on the name + caption pair; its window and
            # reset are no longer repeated under it - the window rows below
            # carry them (label, figure, reset or "ended").
            bw = text(pen, pct, "hero_big", "secondary" if sev == "dim" else "label", X1, b1 + 14, right=True)
        name_max = X1 - bw - 12 - X0 - 12
        symbol(pen, "circle.fill", X0, b1 - 5, 7, "accent")
        nw = text(pen, hero["name"], "hero_name", "label", X0 + 12, b1, maxw=name_max)
        if hero.get("plan"):
            text(pen, hero["plan"], "secondary_med", "secondary", X0 + 12 + nw + 6, b1, maxw=name_max - nw - 6)
        sub = hero["sub"]
        sub = f"Active · slot {hero['slot']} · {sub}" if hero.get("slot") is not None else f"Active · {sub}"
        text(pen, sub, "secondary", "secondary", X0 + 12, b2, maxw=name_max)
        ink = b2 + INK
        if hero["status"]:
            s, kind = hero["status"]
            b = ink + 18
            text(pen, s, "primary_med", SEV_TEXT.get(kind, kind), X0 + 12, b, maxw=X1 - X0 - 12)
            ink = b + INK
        # Window rows share the account rows' columns: bar end, % right edge
        # and reset right edge line up down the whole card.
        bx = HX + WIN_LABEL_W
        b = ink + 19 - WIN_STEP  # the first row sits 19 below the ink above (hero -> rows +8)
        for label, pct, mark, expired in hero["bars"]:
            b += WIN_STEP
            sev = R.severity(pct)
            text(pen, label, "secondary", "secondary", HX, b, maxw=WIN_LABEL_W - 6)
            bar(pen, bx, b - 4, COL_PCT_R - COL_PCT_W - 6 - bx, pct, sev, h=6, expired=expired)
            pct_role = "secondary" if expired else (SEV_TEXT[sev] if sev != "ok" else "label")
            text(pen, format_pct(pct), "num_bar", pct_role, COL_PCT_R, b, right=True)
            text(pen, "↺ ended" if expired else mark, "num_small", "secondary", X1, b, right=True)
            ink = b + INK
        chips = [c for c in hero["captions"] if c[1] == "good" and c[0].startswith("↺")]
        rest = [c for c in hero["captions"] if c not in chips]
        for cap, _kind in chips:  # a usable reset credit: the one positive call to action
            y = ink + 10
            pen.rrect(HX - 4, y, X1 - HX + 4, 24, 6, "cards_good_chip")
            text(pen, cap, "secondary_med", "good_text", HX + 4, y + 16, maxw=X1 - HX - 8)
            ink = y + 24
        if rest:
            cb = ink + 15
            ink = cb + CAP_STEP * (flow(pen, rest, HX, cb, X1 - HX) - 1) + INK
        return ink + PART_END
    return draw


def _row(r: dict[str, Any]) -> Callable[[Any, float], float]:
    from ..contracts import format_pct

    def draw(pen: Any, y0: float) -> float:
        x = X0 + 12  # names share the hero name's column
        label_w = text_w(pen, r["label"], "caption") if r["label"] else 0.0
        name_max = (COL_LABEL_R - label_w - 8 - x) if r["label"] else (COL_BAR_X - 12 - x)
        caps = list(r["captions"])
        plan_inline = bool(r["plan"]) and (
            text_w(pen, r["name"], "primary_med") + 5 + text_w(pen, r["plan"], "caption") <= name_max)
        if r["plan"] and not plan_inline:  # never silently dropped: it leads the caption line
            caps.insert(0, (r["plan"], "dim"))
        b = y0 + ROW_BASE
        if r["slot"]:
            text(pen, r["slot"], "slot", "secondary", X0 + 7, b, right=True)
        nw = text(pen, r["name"], "primary_med", "label", x, b, maxw=name_max)
        if plan_inline:
            text(pen, r["plan"], "caption", "secondary", x + nw + 5, b)
        if r.get("withheld"):
            note, kind = r["withheld"]
            text(pen, "⚠ " + note, "secondary_med", "crit_text" if kind == "crit" else "warn_text", X1, b,
                 right=True, maxw=X1 - COL_BAR_X)
        else:
            if r["label"]:
                text(pen, r["label"], "caption", "secondary", COL_LABEL_R, b, right=True)
            bar(pen, COL_BAR_X, b - 4, COL_BAR_W, r["pct"], r["sev"])
            if r["pct"] is not None:
                text(pen, format_pct(r["pct"]), "num", SEV_TEXT[r["sev"]] if r["sev"] != "ok" else "label",
                     COL_PCT_R, b, right=True)
            text(pen, r["reset"], "num_small", "secondary", X1, b, right=True)
        if not caps:
            return b + ROW_END
        lines = flow(pen, caps, x, b + ROW_CAP, X1 - x)
        return b + ROW_CAP + CAP_STEP * (lines - 1) + ROW_END
    return draw


def _ax(tip: str) -> str:
    return tip.replace("\n", ". ")


def _provider_card(spec: dict[str, Any]) -> _Card:
    card = _Card("cards_card", "cards_card_edge")
    key = spec["key"]
    # The fleet facts can outgrow the header; the full line is the tooltip.
    card.add(_card_head(spec), key=f"cards-{key}-head", ax=f"{spec['title']} accounts. {spec['right']}".strip(". "),
             tooltip=spec["right"])
    hero = spec["hero"]
    if hero:
        card.add(_hero(hero), key=f"cards-{key}-active", tooltip=hero["tip"], ax=_ax(hero["tip"]),
                 action=hero["action"], **ROW_HL)
    if spec["rows"]:
        card.add(_hair(), key=f"cards-{key}-rule")
        for r in spec["rows"]:
            card.add(_row(r), key=r["key"], tooltip=r["tip"], ax=_ax(r["tip"]), action=r["action"], **ROW_HL)
    card.add(_gap(7), key=f"cards-{key}-foot")  # + the part's own end: ~16 pt under the last ink
    return card


def _attention_card(spec: dict[str, Any]) -> _Card:
    card = _Card("cards_card", "cards_card_edge", rule="cards_attn_rule")
    count = len(spec["items"]) + len(spec["extras"])
    card.add(_attn_head(count), key="cards-attn-head", ax=f"Needs attention: {count}")
    for it in spec["items"]:
        opens = "Logs in again." if it["action"] is not None else ""
        label = " ".join(x for x in (
            f"{it['provider']} {it['name']}: {it['state']}.",
            f"{it['cause']}." if it["cause"] else "",
            f"Last seen {it['age']}." if it["age"] else "", opens) if x)
        card.add(_attn_item(it), key=it["key"], tooltip=it["tip"], ax=label, action=it["action"], **ROW_HL)
    for i, line in enumerate(spec["extras"]):
        card.add(_attn_extra(line), key=f"cards-attn-extra-{i}", tooltip=line, ax=line)
    card.add(_gap(6), key="cards-attn-foot")
    return card


# ------------------------------------------------------------------ build ---


def build(snapshot: Any, now: float, actions: ThemeActions) -> list[Block]:
    """Blocks for the top of the dropdown; the app appends the native tail."""
    from .. import app as A
    from .. import render as R

    _ensure_roles()
    cards: list[_Card] = []
    attn = _attention_spec(A, snapshot, actions)
    if attn:
        cards.append(_attention_card(attn))
    for spec in (_claude_spec(A, R, snapshot, now, actions), _codex_spec(A, R, snapshot, now, actions)):
        if spec:
            cards.append(_provider_card(spec))
    blocks: list[Block] = []
    for i, card in enumerate(cards):
        blocks += card.blocks(TOP_GAP if i == 0 else GAP, TAIL_GAP if i == len(cards) - 1 else 0.0)
    return blocks
