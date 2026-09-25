"""The apple card theme (design 3): ``themes/apple.py``.

Why: the operator chose three live designs; this one must read like a native
macOS menu extra AND stay honest (SPEC 4.3) - a noted account never shows a
figure, an expired window never shows a live one, a stale reading says how old
it is - and every click must do exactly what its row says: a Claude row
switches to THAT slot, a Codex relogin row starts Log in again, and a row with
nothing to do never highlights. Nothing the glance menu carried may be lost
(the non-account Needs attention lines ride on ``ThemeActions.attention_extras``).

The paint functions are exercised through a recording Pen, so the tests read
what the menu would actually draw, not just the labels.

Fixtures are SYNTHETIC (example.com emails, made-up aliases and figures).

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    CC_USAGE_WIDGET_NO_REVEAL=1 $PY tests/test_theme_apple.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import card_base, themes  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    normalize_settings,
)

UiSnapshot = app_mod.UiSnapshot
NOW = dt.datetime(2026, 9, 25, 10, 52).timestamp()
SKIP = "" if card_base.available() else "AppKit (PyObjC) is not importable"
RELOGIN = "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add"
VERDICT = "Engine: all rooms above 85% - holding on slot 1"


class Skip(Exception):
    pass


def _need_appkit() -> None:
    if SKIP:
        raise Skip(SKIP)


def _claude(slot: int, alias: str, **fields: Any) -> AccountRow:
    return AccountRow(slot=slot, alias=alias, email=f"{alias}@example.com",
                      is_active=fields.pop("active", False), **fields)


def _codex(slot: int, alias: str, **fields: Any) -> AccountRow:
    return AccountRow(slot=slot, alias=alias, email=f"{alias}@example.com",
                      is_active=fields.pop("active", False), vendor=VENDOR_CODEX, switchable=False,
                      **fields)


def _snapshot(**overrides: Any) -> UiSnapshot:
    """Three Claude slots (1 active, 2 spare, 3 noted) and three Codex rows
    (work active, side capped, dead with a warn alarm and no figures)."""
    values = normalize_settings(dict(SETTINGS_DEFAULTS))
    values.update({"menu_theme": "apple"})
    accounts = (
        _claude(1, "main", active=True, five_hour_pct=44.0, seven_day_pct=13.0,
                five_hour_resets_at="14:50", seven_day_resets_at="16:00", usage_age_seconds=30.0),
        _claude(2, "spare", five_hour_pct=5.0, seven_day_pct=51.0, seven_day_resets_at="14:00",
                usage_age_seconds=30.0),
        _claude(3, "noted", five_hour_pct=77.0, seven_day_pct=66.0, usage_age_seconds=3 * 86_400.0),
    )
    quota = (
        _codex(-1, "work", plan_type="pro", active=True, seven_day_pct=36.0,
               seven_day_resets_at="Oct 2 08:46", usage_age_seconds=60.0, stale_after_seconds=21_600.0),
        _codex(-2, "side", plan_type="pro", seven_day_pct=97.0, seven_day_resets_at="Sep 30 10:08",
               usage_age_seconds=60.0, stale_after_seconds=21_600.0),
        _codex(-3, "dead", plan_type="plus", attention_note="sign-in expired · Log in again",
               attention_kind="warn", usage_age_seconds=60.0, stale_after_seconds=21_600.0),
    )
    base = dict(
        settings=values,
        accounts=accounts,
        active=accounts[0],
        quota_rows=quota,
        account_notes={3: RELOGIN},
        account_note_kinds={3: "re-login needed"},
        accounts_at=1.0,
        autoswitch_enabled=True,
        autoswitch_threshold=85.0,
    )
    base.update(overrides)
    return UiSnapshot(**base)


if card_base.available():

    class RecPen(card_base.Pen):
        """Records what a paint function draws instead of drawing it."""

        def __init__(self, hl: bool = False) -> None:
            super().__init__(hl)
            self.texts: list[tuple[str, Any]] = []
            self.bars: list[tuple[Any, Any]] = []
            self.symbols: list[str] = []

        def text(self, s: str, x: float, baseline_y: float, size: float, weight: Any = "regular",
                 role: Any = "label", **kw: Any) -> float:
            self.color(role)  # an unknown role must fail here, as it would on screen
            if s:
                self.texts.append((s, role))
            return self.width(s, size, weight, mono=kw.get("mono", False))

        def bar(self, x: float, y: float, w: float, h: float, pct: Any, role: Any = "ok",
                track: Any = "track") -> None:
            self.color(role)
            self.bars.append((pct, role))

        def symbol(self, name: str, *args: Any, **kw: Any) -> float:
            self.symbols.append(name)
            return 10.0

        def circle(self, x: float, y: float, d: float, role: Any) -> None:
            self.color(role)

        def rrect(self, *args: Any, **kw: Any) -> None:
            pass

        def line(self, *args: Any, **kw: Any) -> None:
            pass


def _paint(block: Any, hl: bool = False) -> Any:
    pen = RecPen(hl)
    block.paint(pen, card_base.W, block.height)
    return pen


def _drawn(block: Any) -> str:
    return " | ".join(s for s, _ in _paint(block).texts)


def _build(snap: UiSnapshot | None = None, actions: Any = None) -> list[Any]:
    return themes.build_blocks("apple", snap or _snapshot(), NOW, actions or themes.ThemeActions())


def _key(blocks: list[Any], key: str) -> Any:
    found = [b for b in blocks if b.key == key]
    assert found, f"no block {key!r} in {[b.key for b in blocks]}"
    return found[0]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_layout_follows_the_prototype_order_and_leaves_the_tail_to_the_app() -> None:
    """Claude hero, Codex hero, Needs attention, Claude accounts, Codex
    accounts - one separator between sections, none at either end - and never
    a native tail row (the app appends those as real NSMenuItems)."""
    _need_appkit()
    blocks = _build(actions=themes.ThemeActions(attention_extras=(VERDICT,)))
    keys = [b.key for b in blocks]
    assert len(keys) == len(set(keys)), keys
    sections = [k for k in keys if k in ("claude-section", "codex-section", "attention",
                                         "claude-accounts", "codex-accounts")]
    assert sections == ["claude-section", "codex-section", "attention", "claude-accounts",
                        "codex-accounts"], sections
    assert not keys[0].startswith("sep-") and not keys[-1].startswith("sep-"), keys
    for i, k in enumerate(keys):
        if k.startswith("sep-"):
            assert keys[i + 1] in sections, (k, keys[i + 1])
    everything = " ".join(b.label + " " + _drawn(b) for b in blocks)
    for tail in ("Quit", "Refresh", "Auto-switch", "Settings"):
        assert tail not in everything, tail


def test_every_block_draws_in_light_and_dark_and_hovered() -> None:
    """A paint error is swallowed on screen (a blank row), so catch it here:
    every block paints through the real Pen in both appearances, plain and
    highlighted, with no logged draw failure."""
    _need_appkit()
    blocks = _build(actions=themes.ThemeActions(attention_extras=(VERDICT,)))
    before = set(card_base._DRAW_ERRORS)
    with tempfile.TemporaryDirectory() as tmp:
        for dark in (False, True):
            for block in blocks:
                _paint(block, hl=block.selectable)
            hover = next(b.key for b in blocks if b.selectable)
            w, h = card_base.render_png(blocks, Path(tmp) / f"apple-{dark}.png", dark=dark, hover_key=hover)
            assert w == 720 and h > 400, (w, h)
    assert set(card_base._DRAW_ERRORS) == before, set(card_base._DRAW_ERRORS) - before


def test_active_hero_shows_the_binding_window_big_with_its_reset() -> None:
    _need_appkit()
    head = _key(_build(), "claude-head")
    drawn = _drawn(head)
    assert "44%" in drawn and "main" in drawn and "main@example.com" in drawn, drawn
    assert "5-hour limit · resets 14:50" in drawn, drawn
    assert "7d 13%" in drawn, drawn  # the non-binding window, right-aligned
    assert not head.selectable
    assert "5h window 44%" in head.ax, head.ax


def test_a_noted_active_account_never_shows_a_figure() -> None:
    """SPEC 4.3: a relogin'd slot's last numbers are not live - withhold them
    everywhere the hero could leak them (drawing, VoiceOver, tooltip)."""
    _need_appkit()
    snap = _snapshot()
    accounts = (replace(snap.accounts[0], is_active=False), snap.accounts[1],
                replace(snap.accounts[2], is_active=True))
    blocks = _build(replace(snap, accounts=accounts, active=accounts[2]))
    head = _key(blocks, "claude-head")
    pen = _paint(head)
    drawn = " | ".join(s for s, _ in pen.texts)
    assert "Sign-in expired — figures withheld" in drawn, drawn
    for leak in ("77", "66", "%"):
        assert leak not in drawn and leak not in head.ax and leak not in head.tooltip, (leak, drawn)
    assert pen.bars == [], pen.bars
    assert RELOGIN in head.tooltip
    assert "claude-row-3" not in [b.key for b in blocks]  # nor as a list row


def test_noted_slots_live_in_needs_attention_not_in_the_list() -> None:
    _need_appkit()
    blocks = _build()
    keys = [b.key for b in blocks]
    assert "claude-row-3" not in keys and "claude-row-1" not in keys, keys  # noted / active
    row = _key(blocks, "attention-claude-3")
    drawn = _drawn(row)
    assert "noted" in drawn and "Sign-in expired" in drawn, drawn
    assert "Log in with Claude Code, then cswap add" in drawn, drawn
    assert "3d ago" in drawn, drawn
    assert "%" not in drawn, drawn
    assert row.tooltip == RELOGIN
    assert not row.selectable  # nothing a click could do for it


def test_a_refused_fetch_names_the_http_code_and_both_remedies() -> None:
    _need_appkit()
    note = "usage refused (HTTP 429) — log in again and cswap add, or cswap remove 3"
    snap = _snapshot(account_notes={3: note}, account_note_kinds={3: "fetch-failing"})
    drawn = _drawn(_key(_build(snap), "attention-claude-3"))
    assert "Usage refused · HTTP 429" in drawn, drawn
    assert "Log in again and cswap add, or cswap remove 3" in drawn, drawn


def test_attention_extras_are_shown_verbatim_and_counted() -> None:
    """The glance menu's engine verdict / external-switch alert must not be
    lost in the apple theme."""
    _need_appkit()
    blocks = _build(actions=themes.ThemeActions(attention_extras=(VERDICT,)))
    head = _key(blocks, "attention")
    extra = _key(blocks, "attention-extra-0")
    assert VERDICT in _drawn(extra) and extra.tooltip == VERDICT and not extra.selectable
    # 1 noted slot + 1 alarmed Codex row + 1 extra
    assert "3" in _drawn(head), _drawn(head)
    assert RELOGIN in head.tooltip and VERDICT in head.tooltip, head.tooltip


def test_needs_attention_is_absent_when_nothing_needs_the_operator() -> None:
    _need_appkit()
    snap = _snapshot(account_notes={}, account_note_kinds={},
                     quota_rows=_snapshot().quota_rows[:2])
    keys = [b.key for b in _build(snap)]
    assert not any(k.startswith("attention") for k in keys), keys


def test_a_claude_row_click_switches_to_that_slot() -> None:
    _need_appkit()
    calls: list[Any] = []
    actions = themes.ThemeActions(switch_to=calls.append)
    blocks = _build(actions=actions)
    row = _key(blocks, "claude-row-2")
    assert row.selectable
    row.action()
    assert calls == [2], calls
    drawn = _drawn(row)
    assert "spare" in drawn and "7d" in drawn and "51%" in drawn, drawn  # binds on its worst window


def test_the_switch_line_switches_to_the_account_it_names() -> None:
    """The label says "Switch to spare": the click must go to spare, not to
    whatever the engine might pick later."""
    _need_appkit()
    calls: list[Any] = []
    blocks = _build(actions=themes.ThemeActions(switch_to=calls.append))
    line = _key(blocks, "claude-switch")
    assert "Switch to spare" in _drawn(line), _drawn(line)
    line.action()
    assert calls == [2], calls


def test_a_non_switchable_row_does_not_highlight() -> None:
    _need_appkit()
    snap = _snapshot()
    accounts = snap.accounts[:1] + (replace(snap.accounts[1], switchable=False),) + snap.accounts[2:]
    blocks = _build(replace(snap, accounts=accounts, active=accounts[0]))
    assert not _key(blocks, "claude-row-2").selectable
    assert "claude-switch" not in [b.key for b in blocks]  # no target left to name


def test_a_disabled_slot_stays_an_explicit_target() -> None:
    _need_appkit()
    snap = _snapshot()
    accounts = snap.accounts[:1] + (replace(snap.accounts[1], disabled=True),) + snap.accounts[2:]
    row = _key(_build(replace(snap, accounts=accounts, active=accounts[0])), "claude-row-2")
    assert row.selectable and _drawn(row).count("Disabled") == 1, _drawn(row)


def test_codex_rows_click_only_when_they_need_a_login() -> None:
    """CX-5: a dead Codex login's click is Log in again; a healthy one is not
    clickable at all (Codex rows are never switch targets). The dead login is
    listed ONCE, in Needs attention - not again under Codex accounts (design2
    defect #4, "everything twice")."""
    _need_appkit()
    started: list[str] = []

    def login_for(row: AccountRow) -> Any:
        return (lambda: started.append(row.alias)) if row.alias == "dead" else None

    blocks = _build(actions=themes.ThemeActions(codex_login_for=login_for))
    assert not _key(blocks, "codex-row-side").selectable
    assert "codex-row-dead" not in [b.key for b in blocks], "listed twice"
    dead_attention = _key(blocks, "attention-codex-1")
    assert dead_attention.selectable and "Log in again" in dead_attention.ax, dead_attention.ax
    dead_attention.action()
    assert started == ["dead"], started
    assert not _key(blocks, "codex-head").selectable


def test_headers_and_facts_never_highlight() -> None:
    _need_appkit()
    blocks = _build(actions=themes.ThemeActions(attention_extras=(VERDICT,)))
    clickable = {b.key for b in blocks if b.selectable}
    assert clickable == {"claude-switch", "claude-row-2"}, clickable


def test_codex_hero_shows_the_weekly_figure_and_the_best_other_login() -> None:
    _need_appkit()
    blocks = _build()
    drawn = _drawn(_key(blocks, "codex-head"))
    assert "work" in drawn and "Pro" in drawn and "36%" in drawn, drawn
    assert "Weekly limit" in drawn, drawn
    side = _drawn(_key(blocks, "codex-row-side"))
    assert "97%" in side and "7d" in side, side


def test_an_expired_codex_window_shows_no_live_figure() -> None:
    """SPEC 4.3: once a window's reset has passed its old percentage is not a
    reading - neither the hero nor a list row may print or fill it."""
    _need_appkit()
    snap = _snapshot()
    quota = tuple(replace(r, expired_windows=("seven_day",)) for r in snap.quota_rows)
    blocks = _build(replace(snap, quota_rows=quota))
    for key, figure in (("codex-head", "36%"), ("codex-row-side", "97%")):
        pen = _paint(_key(blocks, key))
        drawn = " | ".join(s for s, _ in pen.texts)
        assert figure not in drawn and "—" in drawn, (key, drawn)
        assert all(pct is None for pct, _ in pen.bars), (key, pen.bars)


def test_a_stale_reading_says_how_old_it_is() -> None:
    _need_appkit()
    snap = _snapshot()
    stale = replace(snap.accounts[0], usage_age_seconds=3_600.0, stale_after_seconds=600.0)
    spare = replace(snap.accounts[1], usage_age_seconds=3_600.0, stale_after_seconds=600.0)
    blocks = _build(replace(snap, accounts=(stale, spare, snap.accounts[2]), active=stale))
    head = _key(blocks, "claude-head")
    assert "1h old" in _drawn(head) and "1h old" in head.ax, (_drawn(head), head.ax)
    assert "1h old" in _drawn(_key(blocks, "claude-row-2"))


def test_a_usable_reset_credit_is_the_one_green_fact() -> None:
    _need_appkit()
    snap = _snapshot()
    capped = replace(snap.quota_rows[0], seven_day_pct=100.0, reset_credits_available=1,
                     reset_credits_usable=1, info_notes=("↺ 1 reset credit usable now — use it in Codex",))
    blocks = _build(replace(snap, quota_rows=(capped,) + snap.quota_rows[1:]))
    head = _key(blocks, "codex-head")
    pen = _paint(head)
    drawn = [s for s, _ in pen.texts]
    assert "1 reset credit usable now — use it in Codex" in drawn, drawn  # glyph dropped, symbol instead
    assert "arrow.counterclockwise.circle.fill" in pen.symbols, pen.symbols
    assert pen.bars == [(100.0, "crit")], pen.bars
    assert head.height == 94  # design 5: 78 pt hero (was 74) + the 16 pt fact line


def test_bars_speak_one_colour_language() -> None:
    """Hero gauge: systemBlue when ok (never the accent - a red accent would
    read as crit). List gauges: quiet grey when ok, warn/crit by severity."""
    _need_appkit()
    blocks = _build()
    assert _paint(_key(blocks, "claude-head")).bars == [(44.0, "ok")]
    assert _paint(_key(blocks, "claude-row-2")).bars == [(51.0, "secondary")]
    assert _paint(_key(blocks, "codex-row-side")).bars == [(97.0, "crit")]


def test_codex_only_setup_has_no_empty_claude_hero() -> None:
    _need_appkit()
    snap = _snapshot(accounts=(), active=None, account_notes={}, account_note_kinds={})
    keys = [b.key for b in _build(snap)]
    assert keys[0] == "codex-section" and "claude-head" not in keys, keys


def test_no_active_claude_account_says_so() -> None:
    _need_appkit()
    snap = _snapshot()
    accounts = (replace(snap.accounts[0], is_active=False),) + snap.accounts[1:]
    blocks = _build(replace(snap, accounts=accounts, active=None))
    head = _key(blocks, "claude-head")
    assert "No active account" in _drawn(head) and "no active account" in head.ax


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def test_a_row_with_no_windows_does_not_claim_one_ended() -> None:
    """"Window ended" only when a window did end; a never-read slot says no
    window was reported, as glance does."""
    _need_appkit()
    snap = _snapshot()
    fresh = _claude(4, "fresh")
    ended = _claude(5, "ended", five_hour_pct=100.0, expired_windows=("five_hour",))
    blocks = _build(replace(snap, accounts=snap.accounts + (fresh, ended)))
    assert "no window reported" in _key(blocks, "claude-row-4").ax.lower()
    assert "ended" not in _key(blocks, "claude-row-4").ax.lower()
    assert "window ended" in _key(blocks, "claude-row-5").ax.lower()


def test_an_expired_window_never_reads_as_live_in_tooltips_or_voiceover() -> None:
    _need_appkit()
    snap = _snapshot()
    acc = list(snap.accounts)
    acc[0] = replace(acc[0], five_hour_pct=100.0, expired_windows=("five_hour",))
    acc[1] = replace(acc[1], five_hour_pct=100.0, expired_windows=("five_hour",))
    blocks = _build(replace(snap, accounts=tuple(acc), active=acc[0]))
    for key in ("claude-head", "claude-row-2"):
        block = _key(blocks, key)
        for text in (block.tooltip, block.ax):
            assert "5h 100%" not in text and "5h window ended (last 100%)" in block.tooltip, (key, text)


def test_extra_usage_spend_at_warn_shows_in_the_hero() -> None:
    """Real-money extra usage at the warn threshold is top level, in its
    severity colour, as on glance's Claude card."""
    _need_appkit()
    snap = _snapshot()
    hot = replace(snap.accounts[0], spend_used=40.0, spend_limit=50.0, spend_pct=80.0)
    head = _key(_build(replace(snap, accounts=(hot,) + snap.accounts[1:], active=hot)), "claude-head")
    pen = _paint(head)
    assert ("· extra 80%", "warn") in [(s.strip(), r) for s, r in pen.texts], pen.texts
    assert "extra 80%" in head.ax
    calm = _key(_build(), "claude-head")
    assert "extra" not in _drawn(calm)


def _tests() -> list[tuple[str, Any]]:
    items = [(n, o) for n, o in globals().items() if n.startswith("test_") and callable(o)]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def main() -> int:
    tests = _tests()
    failures: list[str] = []
    skipped = 0
    for name, func in tests:
        try:
            func()
        except Skip as why:
            skipped += 1
            print(f"skip  {name}: {why}")
        except Exception:
            failures.append(name)
            print(f"FAIL  {name}")
            print(traceback.format_exc().rstrip())
        else:
            print(f"pass  {name}")
    total = len(tests)
    passed = total - len(failures) - skipped
    print(f"\n{passed} passed, {len(failures)} failed, {skipped} skipped, out of {total}")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
