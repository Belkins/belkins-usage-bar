"""Menu redesign wave 2 (2026-09-25) — the glance layout's pure builders.

The judge spec (``~/.claude/plans/usage-bar-2026-09-25/lanes/design-spec.md``)
picked the glance layout: two cards that answer the four at-a-glance questions
(which Claude account and how close to its binding window; is Codex usable and
on which account; does anything need me; is a reset usable now), then Needs
attention, Claude, Codex, activity and tools. Today's menu stays byte-for-byte
behind ``menu_layout_classic`` and is chosen automatically where the glance
layout has nothing to summarise — ``tests/test_regressions.py`` pins that path.

Everything here runs without a window server: titles, section order, strings,
the reset formatter, the plan map and the title grammar are pure; the few
native calls are smoke-tested only where the selector exists.

Fixtures are SYNTHETIC rows shaped like the 2026-09-25 integrate preview
(``evidence/integrate-menu-preview.txt``): aliases as in the existing tests,
``example.com`` emails, no real account data.

Run directly, or with pytest if it is installed::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    CC_USAGE_WIDGET_NO_REVEAL=1 $PY tests/test_design.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import codex_accounts, render  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    ALERT_EXTERNAL_SWITCH,
    CODEX_PSEUDO_ACCOUNT_SLOT,
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    normalize_settings,
)

UiSnapshot = app_mod.UiSnapshot

NOW = dt.datetime(2026, 9, 25, 10, 52).timestamp()
"""Friday 2026-09-25 10:52 local — the integrate preview's instant. Every
reset below is placed relative to it, so no assertion reads the real clock."""


def _at(month: int, day: int, hour: int, minute: int) -> float:
    return dt.datetime(2026, month, day, hour, minute).timestamp()


RELOGIN = "re-login needed — refresh token dead; log in with Claude Code, then run: cswap add"


def _settings(**overrides: Any) -> dict[str, Any]:
    """Today's live title settings (spec §2.1): icon off, scoped off, Codex % on,
    cost off; everything else at its shipped default - except the menu, pinned
    to the glance layout these tests describe (design 3 made a card theme the
    default; tests/test_themes_switch.py covers the themes)."""
    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    settings.update(
        {
            "title_show_icon": False,
            "title_show_cost": False,
            "title_show_scoped_pct": False,
            "title_show_codex_pct": True,
            "menu_theme": "glance",
        }
    )
    settings.update(overrides)
    return settings


def _claude(slot: int, alias: str, **fields: Any) -> AccountRow:
    return AccountRow(slot=slot, alias=alias, email=f"{alias}@example.com",
                      is_active=fields.pop("active", False), **fields)


def _codex(slot: int, alias: str, **fields: Any) -> AccountRow:
    return AccountRow(slot=slot, alias=alias, email="", is_active=fields.pop("active", False),
                      vendor=VENDOR_CODEX, switchable=False, **fields)


def _claude_rows() -> tuple[AccountRow, ...]:
    return (
        _claude(1, "main", active=True, five_hour_pct=44.0, seven_day_pct=13.0,
                scoped_windows=(("Fable", 2.0),), five_hour_resets_at="14:50",
                seven_day_resets_at="16:00", scoped_resets_at=(("Fable", "16:00"),),
                spend_used=480.00, spend_limit=500.00, spend_pct=96.0,
                spend_currency="USD"),
        _claude(2, "vlad", five_hour_pct=0.0, seven_day_pct=51.0,
                scoped_windows=(("Fable", 90.0),), seven_day_resets_at="14:00",
                scoped_resets_at=(("Fable", "14:00"),), usage_age_seconds=3 * 86_400.0,
                pace_ahead=(("Fable", True),)),
        _claude(3, "podol", five_hour_pct=1.0, seven_day_pct=25.0,
                scoped_windows=(("Fable", 9.0),), five_hour_resets_at="12:59",
                seven_day_resets_at="Sep 26 13:59", scoped_resets_at=(("Fable", "Sep 26 13:59"),),
                usage_age_seconds=330.0),
        _claude(4, "work1", five_hour_pct=0.0, seven_day_pct=0.0,
                seven_day_resets_at="Sep 27 14:00", disabled=True, usage_age_seconds=3_360.0),
        _claude(5, "synthetic-slot", five_hour_pct=5.0, seven_day_pct=11.0,
                scoped_windows=(("Fable", 7.0),), five_hour_resets_at="13:39",
                seven_day_resets_at="Oct 1 21:59", scoped_resets_at=(("Fable", "Oct 1 21:59"),)),
        _claude(6, "forbid06", five_hour_pct=88.0, seven_day_pct=0.0,
                scoped_windows=(("Fable", 0.0),), five_hour_resets_at="Sep 12 18:10",
                seven_day_resets_at="12:00", scoped_resets_at=(("Fable", "12:00"),),
                usage_age_seconds=12 * 86_400.0, expired_windows=("five_hour",)),
    )


def _codex_rows() -> tuple[AccountRow, ...]:
    return (
        _codex(-1, "vlad", plan_type="pro", seven_day_pct=97.0,
               seven_day_resets_at="Sep 30 10:08", soonest_reset_at=_at(9, 30, 10, 8),
               info_notes=("at this pace: wall in 3h",), usage_age_seconds=60.0,
               stale_after_seconds=21_600.0),
        _codex(-2, "belkins personal", plan_type="pro", active=True,
               attention_note="relogin", attention_kind="warn",
               usage_age_seconds=4 * 86_400.0, stale_after_seconds=21_600.0),
        _codex(-3, "belkins work", plan_type="self_serve_business_prolite", seven_day_pct=0.0,
               seven_day_resets_at="Oct 2 10:49", soonest_reset_at=_at(10, 2, 10, 49),
               reset_credits_available=1, reset_credits_usable=0,
               info_notes=("reset credits: 1 (not usable now)",), usage_age_seconds=60.0,
               stale_after_seconds=21_600.0),
        _codex(-4, "gmail", plan_type="pro", seven_day_pct=100.0,
               seven_day_resets_at="Oct 1 20:25", soonest_reset_at=_at(10, 1, 20, 25),
               attention_note="out of credits · Add credits", attention_kind="crit",
               info_notes=("gpt-6-astra back Oct 1",), usage_age_seconds=60.0,
               stale_after_seconds=21_600.0),
    )


def real_snapshot(**settings: Any) -> UiSnapshot:
    """The REAL state (spec §5.2): vlad noted, belkins personal dead and active,
    gmail capped, belkins work holding one banked reset credit."""
    accounts = _claude_rows()
    return UiSnapshot(
        settings=_settings(**settings),
        accounts=accounts,
        active=accounts[0],
        quota_rows=_codex_rows(),
        account_notes={2: RELOGIN},
        account_note_kinds={2: "re-login needed"},
        alert=(ALERT_EXTERNAL_SWITCH, "09:56 active 5→1 (external)"),
        recent_events=("09:56 active 5→1 (external)",),
        accounts_at=1.0,
    )


def claude_only_snapshot(**settings: Any) -> UiSnapshot:
    """Claude-only (spec §5.3): vlad and forbid06 both noted."""
    snap = real_snapshot(**settings)
    return replace(
        snap,
        quota_rows=(),
        account_notes={2: RELOGIN, 6: "fetch failing — HTTP 429 from the usage endpoint"},
        account_note_kinds={2: "re-login needed", 6: "fetch-failing"},
    )


def codex_only_snapshot(**settings: Any) -> UiSnapshot:
    """Codex-only with live quota on: no claude-swap at all."""
    snap = real_snapshot(**settings)
    return replace(snap, accounts=(), active=None, account_notes={}, account_note_kinds={},
                   alert=None, recent_events=())


def healthy_snapshot(**settings: Any) -> UiSnapshot:
    """Everything healthy (derived): the problem rows removed, belkins work active."""
    snap = real_snapshot(**settings)
    accounts = tuple(row for row in snap.accounts if row.slot != 2)
    codex = tuple(
        replace(row, is_active=row.slot == -3) for row in snap.quota_rows if row.slot != -2
    )
    return replace(snap, accounts=accounts, quota_rows=codex, account_notes={},
                   account_note_kinds={})


def credit_snapshot(**settings: Any) -> UiSnapshot:
    """SYNTHETIC: gmail active, capped, with one usable reset credit."""
    snap = real_snapshot(**settings)
    rows = []
    for row in snap.quota_rows:
        if row.slot == -4:
            row = replace(row, is_active=True, reset_credits_available=1, reset_credits_usable=1,
                          info_notes=("↺ 1 reset credit usable now — use it in Codex",
                                      "gpt-6-astra back Oct 1"))
        elif row.is_active:
            row = replace(row, is_active=False)
        rows.append(row)
    return replace(snap, quota_rows=tuple(rows))


def features_off_snapshot() -> UiSnapshot:
    """Codex-only, features off: the transcript row and nothing else."""
    codex = AccountRow(slot=CODEX_PSEUDO_ACCOUNT_SLOT, alias="Codex", email="", is_active=False,
                       seven_day_pct=42.0, seven_day_resets_at="Oct 1 20:25",
                       vendor=VENDOR_CODEX, switchable=False, plan_type="pro")
    return UiSnapshot(settings=_settings(), quota_rows=(codex,), accounts_at=1.0)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _new_app() -> Any:
    app = app_mod.CCUsageWidgetApp()
    app._now = lambda: NOW  # the one clock seam the menu builders read
    return app


def _close(app: Any) -> None:
    app._running = False
    app._worker.stop(timeout=2.0)


def _capture(build: Any) -> list[list[tuple[str, Any]]]:
    """Run *build* with ``render.apply_attributed`` recording its segments."""
    captured: list[list[tuple[str, Any]]] = []
    original = app_mod.render.apply_attributed

    def _record(_item: Any, segments: Any) -> bool:
        captured.append([(str(text), kind) for text, kind in segments])
        return True

    app_mod.render.apply_attributed = _record
    try:
        build()
    finally:
        app_mod.render.apply_attributed = original
    return captured


def _lines(segments: list[tuple[str, Any]]) -> list[str]:
    return "".join(text for text, _kind in segments).split("\n")


def _title(item: Any) -> str:
    return str(getattr(item, "title", ""))


def _sections(app: Any, snap: UiSnapshot) -> dict[str, list[Any]]:
    """The glance groups with PLAIN titles: ``apply_attributed`` is recorded,
    not applied (an applied attributed title is what ``title()`` returns)."""
    out: dict[str, list[Any]] = {}
    _capture(lambda: out.update(app._glance_sections(snap)))
    return out


def _section_segments(app: Any, snap: UiSnapshot, name: str) -> list[list[tuple[str, Any]]]:
    builders = {
        "cards": app._glance_cards,
        "needs": app._needs_attention_items,
        "claude": app._claude_section,
        "codex": app._codex_section,
    }
    return _capture(lambda: builders[name](snap))


# ---------------------------------------------------------------------------
# 1-3. formatters
# ---------------------------------------------------------------------------


def test_reset_mark_is_one_format_for_every_distance() -> None:
    """Spec §3: ``↺ 14:50`` today, ``↺ Sun 13:59`` inside the week, ``↺ Oct 2
    10:49`` beyond it (time kept), ``↺ overdue`` once passed, ``""`` unknown."""
    assert render.reset_mark(_at(9, 25, 14, 50), NOW) == "↺ 14:50"
    assert render.reset_mark(_at(9, 27, 13, 59), NOW) == "↺ Sun 13:59"
    assert render.reset_mark(_at(10, 2, 10, 49), NOW) == "↺ Oct 2 10:49"
    assert render.reset_mark(_at(9, 25, 9, 0), NOW) == "↺ overdue"
    assert render.reset_mark(None, NOW) == ""


def test_reset_mark_text_reads_claude_swap_strings_back() -> None:
    """The instant is upstream's; only the spelling changes. Unparseable text
    is printed verbatim after the glyph, never guessed at (SPEC 4.3)."""
    assert app_mod._reset_mark_text("14:50", NOW) == "↺ 14:50"
    assert app_mod._reset_mark_text("Sep 26 13:59", NOW) == "↺ Sat 13:59"
    assert app_mod._reset_mark_text("Oct 2 10:49", NOW) == "↺ Oct 2 10:49"
    assert app_mod._reset_mark_text("soon", NOW) == "↺ soon"
    assert app_mod._reset_mark_text("", NOW) == ""
    assert app_mod._reset_mark_text(None, NOW) == ""


def test_plan_label_maps_only_observed_plans() -> None:
    """UX-9: two observed plans get display names; anything else verbatim."""
    assert app_mod.plan_label("pro") == "Pro"
    assert app_mod.plan_label("self_serve_business_prolite") == "Business"
    assert app_mod.plan_label("team_x") == "team_x"
    assert app_mod.plan_label("") == "" and app_mod.plan_label(None) == ""


# ---------------------------------------------------------------------------
# 4-7. title
# ---------------------------------------------------------------------------


def test_title_tokens_join_to_render_title_in_every_setting() -> None:
    """The colour pass reads the tokens the plain title is joined from, so it
    can never change a character (spec §2.3)."""
    app = _new_app()
    try:
        builders = (real_snapshot, claude_only_snapshot, codex_only_snapshot,
                    healthy_snapshot, credit_snapshot)
        for build in builders:
            for compact in (False, True):
                for merge in (False, True):
                    for credit in (False, True):
                        snap = build(title_compact=compact, title_merge_alerts=merge,
                                     title_show_reset_credit=credit)
                        tokens = app.title_tokens(snap)
                        joined = render.join_title_tokens(tokens)
                        assert joined == app.render_title(snap), (build.__name__, joined)
        # The icon-only fallback agrees too.
        empty = replace(features_off_snapshot(), settings=_settings(title_show_codex_pct=False))
        assert app.render_title(empty) == app_mod.TITLE_ICON
        assert render.join_title_tokens(app.title_tokens(empty)) == ""
    finally:
        _close(app)


def test_title_variants_and_widths_in_characters() -> None:
    """Spec §2.2. The default title is byte-identical to today's and the
    opt-in merge NARROWS it. (Where the spec's table printed ``C100%`` for a
    capped row, today's ``_title_pct`` prints ``C100%(!)``; the text stays
    today's and only ``↺now`` is new.)"""
    app = _new_app()
    try:
        real = app.render_title(real_snapshot())
        assert real == "main 44% C⚠ ⚠" and len(real) == 13, real
        n = len(app_mod.needs_attention_rows(real_snapshot()))
        merged = app.render_title(real_snapshot(title_merge_alerts=True))
        assert merged == f"main 44% ⚠{n}" and len(merged) == 11 and len(merged) < len(real), merged
        assert app.render_title(real_snapshot(title_compact=True)) == "M·C 44/⚠"

        assert app.render_title(claude_only_snapshot()) == "main 44% ⚠"
        assert app.render_title(claude_only_snapshot(title_merge_alerts=True)) == "main 44% ⚠2"
        assert app.render_title(claude_only_snapshot(title_compact=True)) == "M 44"

        assert app.render_title(codex_only_snapshot()) == "C⚠"
        assert app.render_title(codex_only_snapshot(title_merge_alerts=True)) == "⚠1"
        assert app.render_title(codex_only_snapshot(title_compact=True)) == "C ⚠"

        assert app.render_title(healthy_snapshot()) == "main 44% C0%"
        assert app.render_title(healthy_snapshot(title_compact=True)) == "M·C 44/0"

        credit = app.render_title(credit_snapshot())
        assert credit == "main 44% C100%(!)↺now ⚠", credit
        off = app.render_title(credit_snapshot(title_show_reset_credit=False))
        assert off.startswith("main 44% C100%(!)↺") and not off.startswith("main 44% C100%(!)↺now")
        assert len(off) <= len(credit), (off, credit)  # ↺now is the whole 4-char budget
        compact = app.render_title(credit_snapshot(title_compact=True))
        assert compact == "M·C 44/100↺", compact
        assert len(compact) <= render.COMPACT_TITLE_MAX
        assert len(app.render_title(real_snapshot(title_compact=True))) <= render.COMPACT_TITLE_MAX
    finally:
        _close(app)


def test_title_token_kinds() -> None:
    """Spec §2.3: alarm glyphs warn (crit when the row is crit), ↺now good,
    fleet dim, a healthy 44% uncoloured, 100% crit."""
    app = _new_app()
    try:
        def kinds(snap: UiSnapshot) -> dict[str, Any]:
            return {text: kind for token in app.title_tokens(snap) for text, kind in token}

        real = kinds(real_snapshot())
        assert real["44%"] == "ok" and real["C⚠"] == "warn" and real["⚠"] == "warn", real
        assert real["main"] is None
        assert kinds(real_snapshot(title_merge_alerts=True))["⚠2"] == "warn"
        credit = kinds(credit_snapshot())
        assert credit["↺now"] == "good" and credit["100%(!)"] == "crit", credit
        # The alarmed row's kind carries into the glyph.
        crit = real_snapshot()
        crit = replace(crit, quota_rows=tuple(
            replace(row, attention_kind="crit") if row.slot == -2 else row for row in crit.quota_rows
        ))
        assert kinds(crit)["C⚠"] == "crit"
        # The fleet suffix is dim.
        hot = real_snapshot()
        main = replace(hot.accounts[0], five_hour_pct=90.0, five_hour_resets_at="12:00")
        hot = replace(hot, accounts=(main,) + hot.accounts[1:], active=main)
        tokens = app.title_tokens(hot)
        assert tokens[-1][0][1] == "dim" and "/" in tokens[-1][0][0], tokens
    finally:
        _close(app)


def test_reset_credit_marker_gating() -> None:
    """``↺now`` only for the ACTIVE, CAPPED row with usable > 0 and the setting
    on. Held-only, a non-active holder, unknown counters, the setting off and
    an expired window each keep today's behaviour."""
    app = _new_app()
    try:
        assert "↺now" in app.render_title(credit_snapshot())
        base = credit_snapshot()

        def with_gmail(**fields: Any) -> UiSnapshot:
            return replace(base, quota_rows=tuple(
                replace(row, **fields) if row.slot == -4 else row for row in base.quota_rows
            ))

        held = app.render_title(with_gmail(reset_credits_usable=0))
        assert "↺now" not in held and "C100%(!)↺" in held, held
        assert "↺now" not in app.render_title(with_gmail(reset_credits_usable=None))
        assert "↺now" not in app.render_title(credit_snapshot(title_show_reset_credit=False))
        expired = app.render_title(with_gmail(expired_windows=("seven_day",)))
        assert "↺" not in expired, expired
        # Usable on a row that is NOT the active login: no marker.
        real = real_snapshot()
        other = replace(real, quota_rows=tuple(
            replace(row, reset_credits_usable=1) if row.slot == -4 else row for row in real.quota_rows
        ))
        assert "↺now" not in app.render_title(other)
        # Not capped: nothing at all, as today.
        roomy = with_gmail(seven_day_pct=40.0, attention_note="", attention_kind="")
        assert "↺" not in app.render_title(roomy), app.render_title(roomy)
        assert "↺" not in app.render_title(replace(roomy, settings={**roomy.settings, "title_compact": True}))
    finally:
        _close(app)


def test_the_title_reset_reads_the_pinned_clock_not_the_wall_clock() -> None:
    """R2-DATE-1. ``_title_reset`` read ``time.time()`` while every menu reset
    mark read ``self._now()``; once the wall clock passed the gmail row's
    Oct 1 20:25 reset, the pinned-NOW title tests went red with no code
    change. The title reads the same seam, so the wall clock can be anywhere
    and the title and the menu agree on whether a reset has passed."""
    app = _new_app()
    real_time = time.time
    base = credit_snapshot()
    held = replace(base, quota_rows=tuple(
        replace(row, reset_credits_usable=0) if row.slot == -4 else row for row in base.quota_rows
    ))
    try:
        pinned = app.render_title(held)
        assert "C100%(!)↺" in pinned and "↺now" not in pinned, pinned
        for days in (7, 365):
            time.time = lambda days=days: real_time() + days * 86_400.0
            assert app.render_title(held) == pinned, (days, app.render_title(held))
    finally:
        time.time = real_time
        _close(app)


# ---------------------------------------------------------------------------
# 8-9. shared definitions
# ---------------------------------------------------------------------------


def test_needs_attention_rows_is_the_one_count() -> None:
    """Noted Claude slots (active included) + alarmed Codex rows; the capped
    gmail row is a limit, not a fault. N = L1 badge + L2 badge = T2's digit."""
    snap = real_snapshot()
    rows = app_mod.needs_attention_rows(snap)
    assert [(v, k if v == "claude" else k.alias) for v, k in rows] == [
        ("claude", 2), ("codex", "belkins personal")
    ], rows
    noted_active = replace(snap, account_notes={1: RELOGIN, 2: RELOGIN})
    assert ("claude", 1) in app_mod.needs_attention_rows(noted_active)

    app = _new_app()
    try:
        cards = _sections(app, snap)["cards"]
        l1, l2 = _title(cards[0]), _title(cards[1])
        claude_n = int(l1.rsplit(" · ", 1)[1].split()[0])
        codex_n = int(l2.rsplit(" · ", 1)[1].split()[0])
        merged = app.render_title(real_snapshot(title_merge_alerts=True))
        assert claude_n + codex_n == len(rows) == int(merged.rsplit("⚠", 1)[1]), (l1, l2, merged)
    finally:
        _close(app)


def test_best_codex_row_ranks_like_the_cli() -> None:
    """UX-5: lowest weekly with room; warn, crit and disabled rows skipped;
    tie -> row order; all capped -> None."""
    rows = _codex_rows()
    assert codex_accounts.best_codex_row(rows).alias == "belkins work"
    tie = (replace(rows[2], alias="first"), replace(rows[2], slot=-5, alias="second"))
    assert codex_accounts.best_codex_row(tie).alias == "first"
    assert codex_accounts.best_codex_row(
        (replace(rows[2], disabled=True), rows[0])
    ).alias == "vlad"
    crit_low = replace(rows[0], seven_day_pct=3.0, attention_kind="crit", attention_note="x")
    assert codex_accounts.best_codex_row((crit_low, rows[2])).alias == "belkins work"
    warn_low = replace(rows[0], seven_day_pct=3.0, attention_kind="warn", attention_note="x")
    assert codex_accounts.best_codex_row((warn_low, rows[2])).alias == "belkins work"
    assert codex_accounts.best_codex_row((rows[3], replace(rows[0], seven_day_pct=100.0))) is None
    # The transcript row (slot 0) is not a login to send work to.
    transcript = features_off_snapshot().quota_rows[0]
    assert codex_accounts.best_codex_row((transcript,)) is None


def test_best_codex_row_equals_the_best_command() -> None:
    """The menu's ``best:`` and ``codex_accounts best --json`` are one ranking."""
    import test_codex_accounts as tca  # the command's own harness

    accounts = [("acct-a", "vlad"), ("acct-b", "work"), ("acct-c", "gmail")]
    for percentages in (
        {"acct-a": 80.0, "acct-b": 12.0, "acct-c": 44.0},
        {"acct-a": 20.0, "acct-b": 20.0, "acct-c": 44.0},  # tie -> registry order
    ):
        with tca.temp_harness(accounts=accounts) as h:
            tca.seed_three_accounts(h, percentages)
            code, text = tca.run_cli(h, "best", "--json")
            assert code == 0, text
            picked = json.loads(text)["alias"]
            best = codex_accounts.best_codex_row(h.source.quota_rows())
            assert best is not None and best.alias == picked, (best, picked)


# ---------------------------------------------------------------------------
# 10. section order per state
# ---------------------------------------------------------------------------


def test_glance_sections_order_per_state() -> None:
    app = _new_app()
    try:
        order = lambda snap: [name for name, _items in app._glance_sections(snap)]  # noqa: E731
        assert order(real_snapshot()) == ["cards", "needs", "claude", "codex", "activity", "tools"]
        assert "codex" not in order(claude_only_snapshot())
        codex_only = order(codex_only_snapshot())
        assert "claude" not in codex_only and codex_only[0] == "cards", codex_only
        assert len(_sections(app, codex_only_snapshot())["cards"]) == 1
        healthy = order(healthy_snapshot())
        assert "needs" not in healthy, healthy  # absent, not empty
        # Nothing to summarise -> the classic layout, automatically.
        assert app._menu_layout(features_off_snapshot()) == "classic"
        never = UiSnapshot(settings=_settings(), accounts_at=0.0)
        assert app._menu_layout(never) == "classic"
        assert app._menu_layout(real_snapshot()) == "glance"
        assert app._menu_layout(real_snapshot(menu_layout_classic=True)) == "classic"
        # rebuild_menu really takes the classic path for them.
        called: list[str] = []
        original = app._rebuild_classic
        app._rebuild_classic = lambda snap: called.append("classic") or original(snap)
        try:
            app.rebuild_menu(features_off_snapshot())
            app.rebuild_menu(never)
        finally:
            app._rebuild_classic = original
        assert called == ["classic", "classic"], called
    finally:
        _close(app)


# ---------------------------------------------------------------------------
# 11-15. card, row and header contents
# ---------------------------------------------------------------------------


def test_cards_answer_the_four_questions() -> None:
    app = _new_app()
    try:
        cards = _section_segments(app, real_snapshot(), "cards")
        l1, l2 = _lines(cards[0]), _lines(cards[1])
        assert l1[0].startswith("Claude · main  5h 44%  ↺ 14:50"), l1
        assert l1[1] == "  next: synthetic-slot 7d 11% · 4/5 room · Fable 4/4 · extra 96%", l1
        extra = [kind for text, kind in cards[0] if text == "extra 96%"]
        assert extra == ["crit"], cards[0]
        assert l2[0].startswith("Codex · belkins personal (Pro)  ⚠ relogin"), l2
        assert "%" not in l2[0], "a withheld active row shows no figure"
        best = codex_accounts.best_codex_row(real_snapshot().quota_rows)
        assert l2[1] == f"  best: {best.alias} 0% · 2/4 room · next ↺ Thu 20:25 (gmail) · 1 reset banked", l2

        # A noted active slot: its status, no percentage.
        noted = real_snapshot()
        noted = replace(noted, account_notes={1: RELOGIN}, account_note_kinds={1: "re-login needed"})
        first = _lines(_section_segments(app, noted, "cards")[0])[0]
        assert first.startswith("Claude · main  ⚠ relogin") and "%" not in first, first

        # `next:` skips disabled and noted rows (vlad is noted, work1 disabled).
        low = real_snapshot()
        accounts = tuple(
            replace(row, five_hour_pct=0.0, seven_day_pct=0.0) if row.slot in (2, 4) else row
            for row in low.accounts
        )
        low = replace(low, accounts=accounts, active=accounts[0])
        assert "next: synthetic-slot" in _lines(_section_segments(app, low, "cards")[0])[1]

        # `best:` is omitted when the best row IS the active row.
        healthy = _lines(_section_segments(app, healthy_snapshot(), "cards")[1])
        assert "best:" not in healthy[1], healthy
        assert healthy[0].startswith("Codex · belkins work (Business)  0%  ↺ Oct 2 10:49"), healthy
    finally:
        _close(app)


def test_credit_state_puts_the_green_line_first() -> None:
    """Spec §5.3 synthetic: the usable-credit note leads L2 line 2 in green and
    the card badge says ``1 reset usable``."""
    app = _new_app()
    try:
        snap = credit_snapshot()
        segs = _section_segments(app, snap, "cards")[1]
        l2 = _lines(segs)
        assert l2[0].startswith("Codex · gmail (Pro)  100%  (!)  ↺ Thu 20:25"), l2
        assert l2[1].startswith("  ↺ 1 reset credit usable now — use it in Codex · best: belkins work 0%"), l2
        assert ("↺ 1 reset credit usable now — use it in Codex", "good") in segs, segs
        card = _sections(app, snap)["cards"][1]
        assert _title(card).endswith(" · 1 reset usable"), _title(card)
        header = _title(_sections(app, snap)["codex"][0])
        assert header.endswith(" · 1 reset usable"), header
        # Green appears nowhere else in the menu.
        greens = [
            text
            for name in ("cards", "needs", "claude", "codex")
            for block in _section_segments(app, snap, name)
            for text, kind in block
            if kind == "good"
        ]
        assert {text.strip() for text in greens} == {
            "↺ 1 reset credit usable now — use it in Codex"
        }, greens
    finally:
        _close(app)


def test_one_line_claude_rows() -> None:
    """No noted slot among them; _switch_targets order (disabled last); disabled
    rows dim and never ``(!)``; an expired window never binds; the plain title
    is the mark-style row label."""
    app = _new_app()
    try:
        snap = real_snapshot()
        items = _sections(app, snap)["claude"]
        assert _title(items[0]) == "Claude · 4/5 room", _title(items[0])
        blocks = _section_segments(app, snap, "claude")
        # blocks[0] is the active 4-line block; the one-line rows follow.
        active = _lines(blocks[0])
        assert active[1].rstrip().endswith("↺ 14:50") and "resets" not in "".join(active), active
        rows = blocks[1:5]  # then All Claude bars' own blocks follow
        firsts = [_lines(block)[0] for block in rows]
        slots = [line.split()[0] for line in firsts]
        assert slots == ["5", "3", "6", "4"], firsts
        assert "2" not in slots  # vlad is noted: Needs attention, not here
        assert firsts[0].startswith("5  synthetic-slot  7d    █▏░░░░░░░░  11%  ↺ Thu 21:59"), firsts
        assert firsts[1].endswith("↺ Sat 13:59 · 5m old"), firsts
        assert firsts[2].split()[2] == "7d", "forbid06's ended 5h 88% never binds"
        disabled = rows[3]
        assert all(kind == "dim" for _text, kind in disabled), disabled
        assert "(!)" not in firsts[3] and firsts[3].endswith(" · disabled"), firsts
        # Plain titles.
        by_slot = {row.slot: row for row in snap.accounts}
        one_line = [item for item in items if _title(item)[:2] in ("5 ", "3 ", "6 ", "4 ")]
        for item in one_line:
            row = by_slot[int(_title(item).split()[0])]
            expected = app_mod._account_row_label(
                row, status=app_mod._account_status_note(row), reset_style="mark", now=NOW
            )
            assert _title(item) == expected, (_title(item), expected)
        assert len(one_line) == 4 and _title(one_line[0]).startswith("5 synthetic-slot")
        # A disabled row, pushed to the wall, still never shows (!).
        wall = replace(by_slot[4], seven_day_pct=100.0)
        segs = render.account_line(4, "work1", name_width=8, label="7d", pct=100.0,
                                   disabled=True)
        assert not any("(!)" in text for text, _kind in segs), (wall, segs)
    finally:
        _close(app)


def test_codex_rows_fact_priority_and_badges() -> None:
    app = _new_app()
    try:
        snap = real_snapshot()
        blocks = _section_segments(app, snap, "codex")[:3]  # then All Codex bars' blocks
        lines = {tuple(_lines(block))[0].split("(")[0].strip(): _lines(block) for block in blocks}
        assert set(lines) == {"belkins work", "vlad", "gmail"}, lines  # alarmed row skipped
        assert lines["belkins work"][1] == "  reset credits: 1 (not usable now)"
        assert lines["vlad"][1] == "  at this pace: wall in 3h"
        assert lines["gmail"][1] == "  out of credits · Add credits"
        assert "weekly" in lines["vlad"][0] and "↺ Wed 10:08" in lines["vlad"][0], lines["vlad"]
        assert "(!)" in lines["gmail"][0] and "(!)" not in lines["vlad"][0]
        kinds = {
            _lines(block)[0].split("(")[0].strip(): [k for t, k in block if t.startswith("  ") and "\n" not in t][-1]
            for block in blocks
        }
        assert kinds == {"belkins work": "dim", "vlad": "warn", "gmail": "crit"}, kinds
        # Priority: usable > crit > pace > info.
        row = snap.quota_rows[3]
        both = replace(row, reset_credits_usable=1,
                       info_notes=("↺ 1 reset credit usable now — use it in Codex", "at this pace: wall in 1h"))
        assert app_mod._codex_fact_line(both) == ("↺ 1 reset credit usable now — use it in Codex", "good")
        crit_pace = replace(row, info_notes=("gpt-6-astra back Oct 1", "at this pace: wall in 1h"))
        assert app_mod._codex_fact_line(crit_pace) == ("out of credits · Add credits", "crit")
        pace = replace(crit_pace, attention_note="", attention_kind="")
        assert app_mod._codex_fact_line(pace) == ("at this pace: wall in 1h", "warn")
        info = replace(pace, info_notes=("gpt-6-astra back Oct 1",))
        assert app_mod._codex_fact_line(info) == ("gpt-6-astra back Oct 1", "dim")
        assert app_mod._codex_fact_line(replace(info, info_notes=())) is None
        # Badges.
        items = _sections(app, snap)["codex"]
        work = next(item for item in items if _title(item).startswith("belkins work"))
        assert _title(work).endswith(" · 1 reset"), _title(work)
        vlad = next(item for item in items if _title(item).startswith("vlad"))
        assert "reset" not in _title(vlad).rsplit(" · ", 1)[-1], _title(vlad)
        unknown = replace(snap, quota_rows=tuple(
            replace(r, reset_credits_available=None, reset_credits_usable=None) for r in snap.quota_rows
        ))
        work = next(i for i in _sections(app, unknown)["codex"] if _title(i).startswith("belkins work"))
        assert not _title(work).endswith("reset"), _title(work)
        # A stale live row draws its age and no bar.
        stale = replace(snap, quota_rows=tuple(
            replace(r, seven_day_pct=None, soonest_reset_at=None, usage_age_seconds=8 * 3600.0,
                    info_notes=()) if r.slot == -1 else r
            for r in snap.quota_rows
        ))
        stale_block = _section_segments(app, stale, "codex")[0]
        assert _lines(stale_block)[0].startswith("vlad"), stale_block
        assert "8h old" in _lines(stale_block)[0] and "░" not in "".join(t for t, _ in stale_block)
    finally:
        _close(app)


def test_section_headers_and_the_fleet_parts_split() -> None:
    """The Codex header, the card and the classic heading compose from one
    source; the classic heading is byte-identical to its pre-split strings."""
    app = _new_app()
    try:
        items = _sections(app, real_snapshot())
        assert _title(items["codex"][0]) == (
            "Codex · 2/4 room · next ↺ Thu 20:25 (gmail) · 1 reset banked"
        ), _title(items["codex"][0])
        assert _title(items["needs"][0]) == "Needs attention"
        assert app_mod._codex_fleet_heading(_codex_rows(), now=NOW) == (
            "Codex 2/4 · next Thu 20:25 (gmail) · 1 reset banked"
        )
        # The literal strings the pre-split tests pinned (test_codex_resets.py,
        # test_regressions.py): unchanged.
        plain = tuple(replace(r, reset_credits_available=None, reset_credits_usable=None)
                      for r in _codex_rows())
        assert app_mod._codex_fleet_heading(plain, now=NOW) == "Codex 2/4 · next Thu 20:25 (gmail)"
        usable = (replace(plain[0], reset_credits_usable=2),) + plain[1:]
        assert app_mod._codex_fleet_heading(usable, now=NOW).endswith(" · 2 resets usable")
        assert app_mod._codex_fleet_heading(features_off_snapshot().quota_rows, now=NOW) == ""
        parts = app_mod._codex_fleet_parts(_codex_rows(), now=NOW)
        assert (parts.room, parts.total, parts.next_alias, parts.resets) == (2, 4, "gmail", "1 reset banked")
        assert app_mod._claude_room(real_snapshot()) == (4, 5)
    finally:
        _close(app)


def test_plain_titles_carry_the_badge_text() -> None:
    """VoiceOver reads the plain title, so it carries the badge's words."""
    app = _new_app()
    try:
        cards = _sections(app, real_snapshot())["cards"]
        assert _title(cards[0]).endswith(" · 1 alert"), _title(cards[0])
        assert _title(cards[1]).endswith(" · 1 alert"), _title(cards[1])
        healthy = _sections(app, healthy_snapshot())["cards"]
        assert "alert" not in _title(healthy[0]) and "alert" not in _title(healthy[1])
    finally:
        _close(app)


# ---------------------------------------------------------------------------
# 16. classic and glance twins of the layout tests
# ---------------------------------------------------------------------------


def _top(app: Any, snap: UiSnapshot) -> list[Any]:
    """Top-level plain titles after a full rebuild (as ``_ux_render`` reads them)."""
    _capture(lambda: app.rebuild_menu(snap))
    return [None if item is None else _title(item) for item in app.menu.values()]


def test_classic_layout_is_todays_menu() -> None:
    app = _new_app()
    try:
        titles = _top(app, real_snapshot(menu_layout_classic=True))
        assert titles[0] == "main (main@example.com) — active", titles
        assert titles[1].startswith("⚠ vlad (2): re-login needed"), titles
        at = titles.index("Accounts")
        assert titles[at + 1] == "Fable 4/4", titles
        assert any(t and t.startswith("  1 main") and "resets 14:50" in t for t in titles), titles
        assert not any(t and "↺ 14:50" in t for t in titles), titles
        assert "Codex 2/4 · next Thu 20:25 (gmail) · 1 reset banked" in titles
        assert any(t and "(self_serve_business_prolite)" in t for t in titles)
        # The glance layout is the default and speaks the one reset format.
        glance = _top(app, real_snapshot())
        assert "Accounts" not in glance and not any(t and "resets 14:50" in t for t in glance)
    finally:
        _close(app)


def test_glance_twin_noted_slot_lives_in_needs_attention() -> None:
    """Twin of test_a_noted_claude_slot_draws_dim_bars_with_its_age_and_no_pace."""
    app = _new_app()
    try:
        snap = real_snapshot()
        titles = _top(app, snap)
        assert sum(1 for t in titles if t and "cswap add" in t) == 1, titles
        needs = titles.index("Needs attention")
        claude = next(i for i, t in enumerate(titles) if t and t.startswith("Claude · 4/5"))
        vlad = next(i for i, t in enumerate(titles) if t and t.startswith("⚠ vlad (2) · Claude · relogin · last seen 3d ago"))
        assert needs < vlad < claude, titles
        blocks = _section_segments(app, snap, "needs")
        assert _lines(blocks[0]) == ["⚠ vlad (2) · Claude · relogin · last seen 3d ago", f"  {RELOGIN}"]
        codex = _lines(blocks[1])
        assert codex[0] == "⚠ belkins personal (Pro) · Codex · relogin · active", codex
        # vlad's dim bars live only inside All Claude bars, with no pace verdict.
        bars = _capture(lambda: app._account_items(snap, reset_style="mark"))
        vlad_block = next(b for b in bars if "vlad" in "".join(t for t, _ in b))
        assert not any(k == "crit" for _t, k in vlad_block)
        assert not any("ahead of pace" in t for t, _k in vlad_block)
        _capture(lambda: app.rebuild_menu(snap))
        sub = [_title(i) for i in app.menu["All Claude bars"].values()]
        assert any(t.startswith("  2 vlad") for t in sub), sub
        assert "Accounts" not in sub
    finally:
        _close(app)


COLD_429 = (
    "usage refused for 12 days since Sep 12 (last error HTTP 429, upstream backoff) · "
    "803 failed polls — plan lapsed or access revoked? log in as forbid06@example.com "
    "and run `cswap add`, or `cswap remove 6` to stop polling (`cswap disable` only "
    "takes it out of rotation; it is still polled)"
)
"""The SMC-4 cold-429 note in ``accounts._fetch_failure_note``'s exact shape."""


def test_a_long_alert_note_is_wrapped_not_cut() -> None:
    """Review 2 (UX): the cold-429 note is longer than ``MAX_ERROR_CHARS`` and
    both layouts sliced it there - ``... log in as`` - so the email and the
    ``cswap add`` / ``cswap remove 6`` remedy never showed. It is wrapped at
    word boundaries now, the statement of the problem whole on the first line,
    still ONE item per slot (``_needs_attention_items`` slices by that count),
    and a note that fits is the plain line it always was."""
    width = app_mod.MAX_ERROR_CHARS
    assert len(COLD_429) > width
    tooltips: list[str] = []
    original_tooltip = app_mod.render.set_tooltip
    app_mod.render.set_tooltip = lambda _item, text: tooltips.append(text) or True
    app = _new_app()
    try:
        snap = replace(
            claude_only_snapshot(menu_layout_classic=True),
            account_notes={2: RELOGIN, 6: COLD_429},
        )
        built: list[Any] = []
        blocks = _capture(lambda: built.extend(app._alert_items(snap)))
        assert _title(built[0]) == f"⚠ vlad (2): {RELOGIN}", "a note that fits is unchanged"
        extra = len(app._alert_items(replace(snap, account_notes={})))
        assert len(built) == len(snap.account_notes) + extra, [_title(i) for i in built]
        (block,) = blocks  # only the long note needed more than one line
        lines = _lines(block)
        whole = f"⚠ forbid06 (6): {COLD_429}"
        assert lines[0] == "⚠ forbid06 (6): usage refused for 12 days since Sep 12 (last error HTTP 429, upstream backoff) · 803 failed polls", lines
        assert len(lines) > 1 and all(len(line) <= width + 2 for line in lines), lines
        assert " ".join(line.strip() for line in lines) == whole, lines
        assert _title(built[1]) == whole and COLD_429 in tooltips

        # Glance: the same note on the dim lines under the Needs attention row.
        tooltips.clear()
        glance = replace(snap, settings={**snap.settings, "menu_layout_classic": False})
        needs = _section_segments(app, glance, "needs")
        dino = next(b for b in needs if _lines(b)[0].startswith("⚠ forbid06 (6) · Claude"))
        detail = _lines(dino)[1:]
        assert all(line.startswith("  ") and len(line) <= width + 2 for line in detail), detail
        assert " ".join(line.strip() for line in detail) == COLD_429, detail
        assert all(k == "dim" for t, k in dino if t.strip() and "forbid06" not in t), dino
        assert COLD_429 in tooltips, tooltips
        vlad = next(b for b in needs if _lines(b)[0].startswith("⚠ vlad (2) · Claude"))
        assert _lines(vlad)[1:] == [f"  {RELOGIN}"], "a note that fits is one dim line"
    finally:
        app_mod.render.set_tooltip = original_tooltip
        _close(app)


def test_glance_twin_recent_switches_sit_below_codex() -> None:
    """Twin of test_recent_switches_are_one_line_and_a_submenu_below_the_accounts."""
    events = tuple(f"20:{m:02d} autoswitch → 5" for m in range(10, 17))
    app = _new_app()
    try:
        snap = replace(real_snapshot(), recent_events=events, alert=None)
        titles = _top(app, snap)
        codex = next(i for i, t in enumerate(titles) if t and t.startswith("Codex · 2/4 room"))
        newest = titles.index("20:16 autoswitch → 5")
        sub = titles.index("Recent switches")
        assert codex < newest < sub, titles
        assert titles.index("All Codex bars") < newest
        children = [_title(i) for i in app.menu["Recent switches"].values()]
        assert len(children) == app_mod.RECENT_SWITCH_LINES and children[0].strip() == "20:16 autoswitch → 5"
    finally:
        _close(app)


def test_glance_twin_scoped_fleet_line_on_the_claude_card() -> None:
    """Twin of test_scoped_fleet_line_renders_under_accounts_behind_its_setting."""
    app = _new_app()
    try:
        on = _lines(_section_segments(app, real_snapshot(), "cards")[0])[1]
        assert "Fable 4/4" in on, on
        off = _lines(_section_segments(app, real_snapshot(scoped_fleet_line_enabled=False), "cards")[0])[1]
        assert "Fable" not in off, off
    finally:
        _close(app)


def test_settings_offer_the_three_new_switches() -> None:
    app = _new_app()
    try:
        app.rebuild_menu(real_snapshot())
        settings = app.menu["Settings"]
        top = [_title(i) for i in settings.values() if i is not None]
        # Design 3: the classic toggle became one of Theme's five choices.
        assert top[0] == "Title" and top[1] == "Theme", top
        themes = [_title(i) for i in settings["Theme"].values() if i is not None]
        assert themes == ["Apple", "Dense", "Cards", "Glance (text)", "Classic (text)"], themes
        title = [_title(i) for i in settings["Title"].values() if i is not None]
        assert "Merge alerts into ⚠N" in title and "Reset-credit marker (↺now)" in title, title
        app.rebuild_menu(claude_only_snapshot())
        title = [_title(i) for i in app.menu["Settings"]["Title"].values() if i is not None]
        assert "Merge alerts into ⚠N" in title and "Reset-credit marker (↺now)" not in title, title
        for key, default in (("menu_layout_classic", False), ("title_merge_alerts", False),
                             ("title_show_reset_credit", True)):
            assert SETTINGS_DEFAULTS[key] is default
            assert normalize_settings({key: not default})[key] is (not default)
    finally:
        _close(app)


# ---------------------------------------------------------------------------
# 17. native smoke (skipped without AppKit / the selector)
# ---------------------------------------------------------------------------


def test_colour_map_and_native_smoke() -> None:
    AppKit = render._appkit()
    if AppKit is None:
        print("  skip: no AppKit")
        return
    assert render._color("ok") == AppKit.NSColor.labelColor()
    assert render._color("good") == AppKit.NSColor.systemGreenColor()
    assert render._color("anything-else") == AppKit.NSColor.labelColor()
    if getattr(AppKit.NSMenuItem, "sectionHeaderWithTitle_", None) is None:
        print("  skip: no sectionHeaderWithTitle_ (macOS < 14)")
        return
    header = render.section_header("Claude")
    assert header._menuitem.isSectionHeader() and _title(header) == "Claude"
    import rumps

    item = rumps.MenuItem("x")
    assert render.set_badge(item, "1 reset") and item._menuitem.badge().stringValue() == "1 reset"
    assert render.set_alert_badge(item, 3) and "3" in item._menuitem.badge().stringValue()
    string = render.title_attributed([[("main", None)], [("44%", "ok")], [("↺now", "good")]])
    assert string is not None and string.string() == "main 44% ↺now"


def test_the_coloured_title_is_exactly_as_wide_as_the_plain_one() -> None:
    """Live 2026-09-25: the status bar draws a plain title at 13 pt, and a title
    rebuilt with menuBarFontOfSize_(0) (14 pt) grew the item from 158 to 173 pt.
    Recolouring the button's own plain title must keep its font on every
    character and change only the colour of the warn runs."""
    AppKit = render._appkit()
    if AppKit is None:
        print("  skip: no AppKit")
        return
    tokens = [[("podol", None)], [("22%", "ok")], [("C", None), ("29%", "ok")], [("⚠", "warn")]]
    text = render.join_title_tokens(tokens)
    font = AppKit.NSFont.systemFontOfSize_(13.0)
    plain = AppKit.NSAttributedString.alloc().initWithString_attributes_(
        text, {AppKit.NSFontAttributeName: font}
    )
    out = render.title_recolored(plain, tokens)
    assert out is not None and out.string() == text
    for index in range(len(text.encode("utf-16-le")) // 2):
        attrs, _ = out.attributesAtIndex_effectiveRange_(index, None)
        assert attrs[AppKit.NSFontAttributeName] == font, (index, attrs)
    warn_at = len(text) - 1
    attrs, _ = out.attributesAtIndex_effectiveRange_(warn_at, None)
    assert attrs.get(AppKit.NSForegroundColorAttributeName) == render._color("warn")
    attrs, _ = out.attributesAtIndex_effectiveRange_(0, None)
    assert AppKit.NSForegroundColorAttributeName not in attrs, "healthy runs keep the bar colour"
    assert out.size().width == plain.size().width
    # A plain title that does not spell the tokens is never recoloured.
    assert render.title_recolored(plain, [[("main", None)]]) is None


# ---------------------------------------------------------------------------
# 18. rebuild cost guard
# ---------------------------------------------------------------------------


def test_glance_rebuild_does_no_io_and_is_fast() -> None:
    """SPEC 2.3: the menu is built on the AppKit thread and does no I/O. The
    two pre-existing per-build probes in Settings (`_codex_registry_present`,
    a stat; `_telegram_credentials_present`, a sub-KB read) are this module's
    documented exceptions and are pinned to constants here; anything else
    that opens a file or a socket fails the test."""
    import builtins
    import io
    import socket

    app = _new_app()
    snap = real_snapshot()
    touched: list[str] = []
    originals = (builtins.open, io.open, os.open, socket.socket,
                 app_mod._codex_registry_present, app_mod._telegram_credentials_present)

    def _deny(name: str) -> Any:
        def _raise(*args: Any, **kwargs: Any) -> Any:
            touched.append(f"{name}{args[:1]}")
            raise OSError(f"{name} during a menu rebuild")
        return _raise

    try:
        app._rebuild_glance(snap)  # warm the symbol cache and imports first
        builtins.open = io.open = _deny("open")  # type: ignore[assignment]
        os.open = _deny("os.open")  # type: ignore[assignment]
        socket.socket = _deny("socket")  # type: ignore[assignment, misc]
        app_mod._codex_registry_present = lambda: True
        app_mod._telegram_credentials_present = lambda: False
        # Best of three: one collector pause or scheduler hiccup on a shared CI
        # runner is not what this guards; a rebuild that does real work is.
        elapsed = float("inf")
        for _attempt in range(3):
            started = time.perf_counter()
            items = app._rebuild_glance(snap)
            elapsed = min(elapsed, time.perf_counter() - started)
    finally:
        (builtins.open, io.open, os.open, socket.socket,
         app_mod._codex_registry_present, app_mod._telegram_credentials_present) = originals
        _close(app)
    assert items, "the glance layout built nothing"
    assert touched == [], touched
    assert elapsed < 0.05, f"glance rebuild took {elapsed * 1000:.1f} ms"


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def _tests() -> list[tuple[str, Any]]:
    items = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def main() -> int:
    tests = _tests()
    failures: list[str] = []
    for name, func in tests:
        try:
            func()
        except Exception:
            failures.append(name)
            print(f"FAIL  {name}")
            print(traceback.format_exc().rstrip())
        else:
            print(f"pass  {name}")
    total = len(tests)
    print(f"\n{total - len(failures)} passed, {len(failures)} failed, out of {total}")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
