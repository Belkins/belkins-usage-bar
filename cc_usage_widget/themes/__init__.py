"""Menu themes (design 3, 2026-09-25): three native card designs + two text layouts.

``menu_theme`` picks one of :data:`MENU_THEMES`. The three CARD themes
(:data:`THEMES`) each live in their own module - ``themes/apple.py``,
``themes/dense.py``, ``themes/cards.py`` - exposing::

    build(snapshot, now, actions) -> list[card_base.Block]

which draws the TOP of the dropdown (status, Needs attention, accounts). The
app then appends the COMMON native tail (:func:`tail_rows`), so every theme
shares Cost / Sessions / Recent switches / Auto-switch / Cost tracking /
Switch account / Switch to best now / All windows & resets / Refresh /
Settings / Quit, with the same behaviour. The two
text layouts (``glance``, ``classic``) are the app's own builders, unchanged.

Themes import app helpers LAZILY inside ``build`` (``from .. import app as A``):
``app`` imports this package, so a module-level import would be circular.
A theme never performs an action itself - it closes over :class:`ThemeActions`.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

__all__ = [
    "THEMES",
    "TEXT_LAYOUTS",
    "MENU_THEMES",
    "THEME_LABELS",
    "ThemeActions",
    "build_blocks",
    "tail_rows",
    "placeholder_blocks",
]

THEMES: tuple[str, ...] = ("apple", "dense", "cards")
"""The native card themes, in Settings ▸ Theme order."""

TEXT_LAYOUTS: tuple[str, ...] = ("glance", "classic")
"""The pre-design-3 text layouts, rendered by app.py byte for byte."""

MENU_THEMES: tuple[str, ...] = THEMES + TEXT_LAYOUTS
"""Every valid ``menu_theme`` value, in Settings ▸ Theme order; the default is
``SETTINGS_DEFAULTS["menu_theme"]`` (cards), not the first entry."""

THEME_LABELS: dict[str, str] = {
    "apple": "Apple",
    "dense": "Dense",
    "cards": "Cards",
    "glance": "Glance (text)",
    "classic": "Classic (text)",
}
"""Settings ▸ Theme item titles."""


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _no_login(_row: Any) -> None:
    return None


@dataclass(frozen=True)
class ThemeActions:
    """What a theme's clickable blocks may do. Every callable runs on the
    AppKit thread and only ENQUEUES work (the worker does the I/O).

    - ``switch_to(slot)``: make Claude slot *slot* the active login.
    - ``switch_best()``: let the engine pick the best target (``Switch to …``).
    - ``codex_login(row)``: start ``Log in again…`` for a Codex quota row;
      returns False (and does nothing) when the row is not a relogin row.
    - ``codex_login_for(row)``: the zero-argument action for that row, or
      ``None`` when the row is not clickable - use it as ``Block(action=…)``.
    - ``open_url(url)``: open an http(s) URL in the default browser. The
      codebase carries no "Add credits" URL today; never invent one.
    - ``refresh()``: the ``Refresh`` command.
    - ``attention_extras``: DATA, not a callable - the non-account lines the
      glance menu's Needs attention carried (the engine verdict, the
      external-switch alert, the switch note), verbatim. A theme shows them
      in its attention section so nothing the glance menu said is lost.
    """

    switch_to: Callable[[int], Any] = _noop
    switch_best: Callable[[], Any] = _noop
    codex_login: Callable[[Any], bool] = lambda _row: False
    codex_login_for: Callable[[Any], Callable[[], Any] | None] = _no_login
    open_url: Callable[[str], bool] = lambda _url: False
    refresh: Callable[[], Any] = _noop
    attention_extras: tuple[str, ...] = field(default_factory=tuple)


def build_blocks(theme: str, snapshot: Any, now: float, actions: ThemeActions) -> list[Any]:
    """``themes/<theme>.py: build(snapshot, now, actions)`` as a list of Blocks.

    Raises ``ValueError`` for a name outside :data:`THEMES` (the text layouts
    are not block themes) and lets a theme's own exception propagate: the app
    catches it and falls back to the glance layout for that rebuild.
    """
    if theme not in THEMES:
        raise ValueError(f"not a card theme: {theme!r}")
    module = importlib.import_module(f"{__name__}.{theme}")
    return list(module.build(snapshot, now, actions))


_LEADING_TIME = re.compile(r"^(\d{1,2}:\d{2})\b")


def tail_rows(snapshot: Any, *, problems: Sequence[str] = ()) -> list[dict[str, Any] | None]:
    """The COMMON native tail under every card theme (design2-spec §7).

    Each row is ``{"id", "title", "detail"?, "check"?, "submenu"?, "key_eq"?, "tooltip"?}``
    and ``None`` is a separator. The app maps ``id`` to the real NSMenuItem;
    :func:`card_base.render_png` draws the same rows as plain text, so the
    rendered composite and the real menu cannot drift apart. *problems* are
    background-failure lines (``! …``) that lead the tail when present.
    """
    from .. import app as A
    from ..card_base import fit_menu_title
    from ..contracts import format_pct

    # A problem line is capped (full text in its tooltip): an uncapped one
    # widened the whole menu past the fixed-width cards (review appkit-1).
    rows: list[dict[str, Any] | None] = [
        {"id": "problem", "title": fit_menu_title(text), "tooltip": text} for text in problems
    ]
    if rows:
        rows.append(None)
    rows.append({"id": "cost", "title": f"Cost ({A.NOTIONAL_LABEL})", "submenu": True})
    if snapshot.sessions or snapshot.fleet_notes:
        rows.append({"id": "sessions", "title": "Sessions", "submenu": True,
                     "detail": str(len(snapshot.sessions)) if snapshot.sessions else ""})
    if snapshot.recent_events:
        newest = A._localize_instants(str(tuple(snapshot.recent_events)[-1]))
        match = _LEADING_TIME.match(newest)
        rows.append({"id": "recent", "title": "Recent switches", "submenu": True,
                     "detail": f"last {match.group(1)}" if match else ""})
    rows.append(None)
    # The two on/off switches stay top level (SPEC 4.2) and say their state
    # in the text, not only in the checkmark: "at 85%" only while it is on.
    threshold = snapshot.autoswitch_threshold
    auto_on = bool(snapshot.autoswitch_enabled)
    rows.append({"id": "autoswitch", "title": "Auto-switch", "check": auto_on,
                 "detail": ("off" if not auto_on else
                            f"at {format_pct(threshold)}" if threshold is not None else "on")})
    cost_on = bool(snapshot.settings.get("cost_tracking_enabled", True))
    rows.append({"id": "cost_tracking", "title": "Cost tracking", "check": cost_on,
                 "detail": "on" if cost_on else "off"})
    rows.append({"id": "switch", "title": "Switch account", "submenu": True})
    # The engine's one-click pick (claude-swap strategy=best), not a theme's
    # own "Switch to <X>" ranking; dim when there is nowhere to go.
    rows.append({"id": "best", "title": "Switch to best now"})
    rows.append({"id": "all", "title": "All windows & resets", "submenu": True})
    rows.append(None)
    rows.append({"id": "refresh", "title": "Refresh", "key_eq": "⌘R"})
    rows.append({"id": "settings", "title": "Settings", "submenu": True})
    rows.append({"id": "quit", "title": "Quit", "key_eq": "⌘Q"})
    return rows


def placeholder_blocks(label: str, snapshot: Any, now: float, actions: ThemeActions) -> list[Any]:
    """A minimal, honest header for a theme whose module is still a stub.

    One line per vendor (the active account and its binding figure - withheld
    when the account is noted, ``Nm old`` when stale) and one line counting
    what needs attention, with the verbatim notes in its tooltip. Everything
    else stays reachable through the common tail (``All windows & resets``).
    """
    from .. import app as A
    from ..card_base import LEAD, RIGHT, Block
    from ..contracts import format_pct

    models = tuple(snapshot.autoswitch_models or ())
    blocks: list[Any] = []

    def line(left: str, right: str, right_role: str = "secondary") -> Callable[..., None]:
        def paint(pen: Any, w: float, h: float) -> None:
            rw = pen.text(right, RIGHT, 16, 11, "regular", right_role, mono=True, right=True) if right else 0.0
            pen.text(left, LEAD, 16, 13, "semibold", "label", maxw=RIGHT - LEAD - rw - 8)
        return paint

    if snapshot.accounts:
        active = snapshot.active
        if active is None:
            left, right, ax = "Claude", "no active account", "Claude: no active account"
        else:
            name = A._display_name(active)
            note = snapshot.account_notes.get(active.slot)
            if note:
                right = "figures withheld"
            else:
                binding = A._binding_window(active, models)
                right = f"{binding[0]} {format_pct(binding[1])}" if binding else "no live window"
                if active.usage_is_stale:
                    right += f" · {A._age_label(active.usage_age_seconds)} old"
            left, ax = f"Claude · {name}", f"Active Claude account {name}: {right}"
        blocks.append(Block(24, line(left, right), ax=ax, tooltip=ax, key="claude-head"))
    live = [r for r in A._visible_quota_rows_of(snapshot) if r.vendor != A.VENDOR_CLAUDE and r.slot < 0]
    if live:
        row = next((r for r in live if r.is_active), None)
        if row is None:
            left, right = "Codex", "active login not identified"
        else:
            window = A._plan_window(row)
            if A._quota_alarm(row):
                right = row.attention_note
            elif window is None:
                right = A._quota_note(row)[0] or "no reading yet"
            else:
                right = f"{window[0]} " + ("—" if window[2] or window[1] is None else format_pct(window[1]))
                if row.usage_is_stale:
                    right += f" · {A._age_label(row.usage_age_seconds)} old"
            left = f"Codex · {A._quota_alias(row)}"
        blocks.append(Block(24, line(left, right), ax=f"{left}: {right}", tooltip=f"{left}: {right}",
                            key="codex-head"))
    notes = [snapshot.account_notes[slot] for slot in sorted(snapshot.account_notes)]
    notes += [row.attention_note for kind, row in A.needs_attention_rows(snapshot) if kind == "codex"]
    notes += list(actions.attention_extras)
    if notes:
        count = f"{len(notes)} need{'s' if len(notes) == 1 else ''} attention"
        blocks.append(Block(22, line(count, "", "warn"), ax=count, tooltip="\n".join(notes),
                            key="attention"))
    if not blocks:
        blocks.append(Block(24, line(label, ""), ax=label, key="empty"))
    return blocks
