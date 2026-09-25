"""Tests for ``cc_usage_widget.dashboard`` — the single-file local dashboard
(roadmap item 9).

The page is the one artifact of this program a human reads *away* from the
menu bar, on their own, possibly months later, and possibly with someone else
looking over their shoulder. Three properties therefore matter more than any
chart:

* **It loads nothing.** No ``<script src>``, no stylesheet link, no image, no
  ``http`` of any kind. A page describing what someone's Claude and Codex usage
  cost must not announce its own existence to a CDN, and must still render in a
  year with no library available.
* **Every number is a measurement.** There is no sample series anywhere in the
  module, so an empty store renders an empty state rather than a demonstration
  dataset (``rules/10-no-fabrication.md``), unpriced volume stays tokens and
  never becomes a dollar, and a section whose data does not exist yet - the
  per-project table, until roadmap item 6 adds the column - is *absent*, not
  blank. A heading over an empty table reads as "no projects used".
* **It opens nothing during a test run.** ``_open_in_browser`` is the sibling of
  ``_reveal_in_finder`` behind the same ``CC_USAGE_WIDGET_NO_REVEAL`` guard,
  because the export tests opened real Finder windows on this desktop on
  2026-09-10 and a browser tab is the same mistake with a bigger window.

Nothing here touches ``~/.claude``, ``~/.codex`` or the real widget home: every
store is built inside a ``TemporaryDirectory`` and every dashboard is written
beside it.

Run directly (pytest is absent from the claude-swap venv)::

    ~/.local/share/uv/tools/claude-swap/bin/python tests/test_dashboard.py
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open a window from a test

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import dashboard as dash  # noqa: E402
from cc_usage_widget.app import BackgroundWorker, UiSnapshot  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    DayRollup,
    IndexProgress,
    ModelUsage,
    day_keys_back,
    local_day_key,
    make_vendor_key,
    normalize_settings,
)
from cc_usage_widget.dashboard import (  # noqa: E402
    EMPTY_MARKER,
    INDEXING_BANNER,
    WALL_NO_HISTORY_NOTE,
    build_dashboard,
    dashboard_path_for,
    render_dashboard,
    write_dashboard,
)
from cc_usage_widget.history import HistoryStore  # noqa: E402
from cc_usage_widget.indexer import Indexer  # noqa: E402
from cc_usage_widget.pricing import DEFAULT_PRICING  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402

FABLE = "claude-fable-5-20260514"
SOL = make_vendor_key(VENDOR_CODEX, "gpt-5.6-sol")
ASTRA = make_vendor_key(VENDOR_CODEX, "gpt-6-astra")
REVIEW = make_vendor_key(VENDOR_CODEX, "codex-auto-review")

NOW = 1_789_036_000.0  # 2026-09-10 11:26 local — a fixed clock, so the page is stable
TODAY = local_day_key(NOW)


# ---------------------------------------------------------------------------
# Fixtures — real stores in a temp dir, no monkeypatched parsers
# ---------------------------------------------------------------------------


def _store(root: Path, *, keep_days: int = 90) -> DailyRollupStore:
    return DailyRollupStore(path=root / "rollups.json", keep_days=keep_days)


def _history(root: Path) -> HistoryStore:
    return HistoryStore(root / "history.sqlite")


def _day(day: str, **models: ModelUsage) -> DayRollup:
    return DayRollup(day=day, models=dict(models))


def _codex_row(**overrides: Any) -> AccountRow:
    fields: dict[str, Any] = {
        "slot": -1,
        "alias": "vlad",
        "email": "",
        "is_active": True,
        "seven_day_pct": 100.0,
        "seven_day_resets_at": "Sat 09:00",
        "vendor": VENDOR_CODEX,
        "switchable": False,
        "soonest_reset_at": NOW + 4 * 86_400,
        "plan_type": "pro",
    }
    fields.update(overrides)
    return AccountRow(**fields)


def _claude_row(**overrides: Any) -> AccountRow:
    fields: dict[str, Any] = {
        "slot": 1,
        "alias": "podol",
        "email": "someone@example.com",
        "is_active": True,
        "five_hour_pct": 17.0,
        "seven_day_pct": 42.0,
        "five_hour_resets_at": "00:00",
        "scoped_windows": (("Fable", 3.0),),
    }
    fields.update(overrides)
    return AccountRow(**fields)


class _StubRow:
    """A history row that carries a ``project``, as roadmap item 6 will.

    Not a monkeypatched parser: a row object shaped like the one B6's migration
    produces, so this module can prove *today* that the per-project section
    appears the moment the column does — and stays absent until then.
    """

    def __init__(self, day: str, key: str, usage: ModelUsage, usd: float, project: str) -> None:
        self.day = day
        self.key = key
        self.vendor = "claude"
        self.usage = usage
        self.usd_at_record = usd
        self.updated_at = NOW
        self.project = project


class _StubHistory:
    """A history store that returns :class:`_StubRow` objects."""

    def __init__(self, rows: tuple[_StubRow, ...]) -> None:
        self._rows = rows

    def rows(self, *, since: str | None = None) -> tuple[_StubRow, ...]:
        if since is None:
            return self._rows
        return tuple(row for row in self._rows if row.day >= since)


# ---------------------------------------------------------------------------
# 1. What the page is allowed to contain
# ---------------------------------------------------------------------------


def test_an_empty_store_renders_an_honest_empty_state() -> None:
    """No sample data, ever — an unmeasured machine says it has nothing.

    This is the fabrication guard for the whole module: the easiest way to make
    a dashboard look finished is to seed it with a plausible week, and that
    would be a lie printed in a chart.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        page = render_dashboard(_history(root), _store(root), (), (), now=NOW)
    assert EMPTY_MARKER in page, page[:400]
    assert "<h2>Windows</h2>" not in page
    assert "<h2>Tokens per day</h2>" not in page
    assert "<h2>Quota windows</h2>" not in page
    # Nothing that looks like a figure: no dollar amount, no token count.
    assert not re.search(r"\$\d", page), "an empty page must not print a dollar figure"


def test_the_document_loads_nothing_from_the_network() -> None:
    """Self-contained is the feature, not a nice-to-have.

    A CDN reference in a page whose subject is someone's spend puts that page's
    existence in a third party's log, and breaks the day the CDN moves.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge([_day(TODAY, **{FABLE: ModelUsage(input=1_000, output=500)})])
        page = render_dashboard(
            _history(root), store, (_codex_row(),), (_claude_row(),),
            now=NOW, pricing=DEFAULT_PRICING,
        )
    for forbidden in ("<script", "http", "<link", "<img", "@import", "url("):
        assert forbidden not in page, f"{forbidden!r} must never appear in the page"


def test_both_themes_are_defined_and_the_light_one_is_not_inside_a_query() -> None:
    """A browser with no colour preference must still get a complete palette.

    The trap this pins: defining the light tokens only under
    ``prefers-color-scheme: light`` leaves the default state with no values at
    all, which renders as unstyled text on whatever the browser paints.
    """
    with tempfile.TemporaryDirectory() as name:
        page = render_dashboard(None, _store(Path(name)), (), (), now=NOW)
    assert "@media (prefers-color-scheme: dark)" in page
    head, _, tail = page.partition("@media (prefers-color-scheme: dark)")
    assert ":root{" in head, "the light palette must be defined on bare :root"
    assert "--bg:" in head and "--ink:" in head and "--s1:" in head
    assert "--bg:" in tail and "--ink:" in tail and "--s1:" in tail


def test_a_model_name_is_escaped_rather_than_interpreted() -> None:
    """Model names come off disk, so they are untrusted text, not markup."""
    hostile = "<b>gpt</b>"
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge([_day(TODAY, **{hostile: ModelUsage(input=4_000)})])
        page = render_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert "<b>gpt</b>" not in page, "an unescaped model name would become markup"
    assert "&lt;b&gt;gpt&lt;/b&gt;" in page


# ---------------------------------------------------------------------------
# 2. The numbers are the stores' numbers
# ---------------------------------------------------------------------------


def test_window_totals_are_the_stores_own_numbers() -> None:
    """Every figure traces to a cell — the exact integer is printed, not only
    the rounded ``1.2M``, so a reader can reconcile the page with the menu."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge(
            [
                _day(TODAY, **{FABLE: ModelUsage(input=1_200_000, output=34_000)}),
                _day(TODAY, **{SOL: ModelUsage(input=5_000_000, output=6_000)}),
            ]
        )
        page = render_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
    claude_tokens = 1_200_000 + 34_000
    codex_tokens = 5_000_000 + 6_000
    assert f"{claude_tokens:,}" in page, "the Claude total must appear exactly"
    assert f"{codex_tokens:,}" in page, "the Codex total must appear exactly"
    assert f"{claude_tokens + codex_tokens:,}" in page, "and so must their sum"


def test_a_day_only_the_mirror_still_holds_is_included() -> None:
    """The whole point of the 90-day window: days the cache has dropped.

    ``rollups.json`` is pruned to ``lookback_days``; ``history.sqlite`` is not.
    A dashboard that read only the live store would answer exactly the question
    the menu already answers.
    """
    old_day = day_keys_back(TODAY, 60)[0]
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        history = _history(root)
        history.upsert_day(
            old_day, _day(old_day, **{FABLE: ModelUsage(input=7_654_321)}), DEFAULT_PRICING
        )
        store = _store(root)  # empty: the cache no longer covers that day
        page = render_dashboard(history, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert "7,654,321" in page, "a history-only day must still be counted"


def test_the_live_store_wins_where_both_hold_the_same_day() -> None:
    """A rebuild that LOWERED a cell must not be re-inflated by a stale mirror.

    The mirror is written at the end of a cost job, so between a rebuild (or an
    audit repair) and the next mirror pass the two disagree — and the store is
    the one that was just corrected. This is the roadmap's opening incident
    (cells inflated up to 1,650x) pointed at the dashboard.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        history = _history(root)
        history.upsert_day(
            TODAY, _day(TODAY, **{FABLE: ModelUsage(input=999_888_777)}), DEFAULT_PRICING
        )
        store = _store(root)
        store.merge([_day(TODAY, **{FABLE: ModelUsage(input=1_234)})])
        page = render_dashboard(history, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert "1,234" in page, "the corrected figure must be the one drawn"
    assert "999,888,777" not in page, "the stale mirror must not survive a rebuild"


def test_days_outside_the_long_window_are_left_out() -> None:
    """90 days means 90 days. A mirror that keeps for ever must not widen it
    silently, or "last 90 days" stops meaning anything."""
    ancient = day_keys_back(TODAY, 200)[0]
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        history = _history(root)
        history.upsert_day(
            ancient, _day(ancient, **{FABLE: ModelUsage(input=5_555_555)}), DEFAULT_PRICING
        )
        page = render_dashboard(history, _store(root), (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert "5,555,555" not in page
    assert EMPTY_MARKER in page, "nothing inside the window means an empty page"


def test_the_thirty_day_block_excludes_what_only_the_ninety_day_one_holds() -> None:
    """Two windows on one page means two folds, not one drawn twice.

    The bound that matters here is the INNER one: ``collect_cells`` already
    stops at 90 days, so a 30-day block that forgot to re-filter would print
    the 90-day figure under a "Last 30 days" heading and nothing else on the
    page would look wrong.
    """
    older = day_keys_back(TODAY, 45)[0]
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge(
            [
                # Different vendors, so each day's figure survives the fold
                # and the two windows are distinguishable at a glance.
                _day(older, **{SOL: ModelUsage(input=88_888_888)}),
                _day(TODAY, **{FABLE: ModelUsage(input=1_111)}),
            ]
        )
        page = render_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
    windows = page.split("<h2>Windows</h2>", 1)[1].split("</section>", 1)[0]
    thirty, _, ninety = windows.partition("<h3>Last 90 days</h3>")
    assert "1,111" in thirty and "88,888,888" not in thirty, thirty[:300]
    assert "88,888,888" in ninety and f"{88_888_888 + 1_111:,}" in ninety


def test_unpriced_volume_is_tokens_and_names_never_a_dollar() -> None:
    """``codex-auto-review`` has no published rate (roadmap item 3).

    It is 0.9 B tokens a week at $0; folded into a total it would silently
    understate the window, so the page names it and prints its magnitude while
    the window totals carry a ``+`` to say they are a floor.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge(
            [
                _day(TODAY, **{SOL: ModelUsage(input=200_000, output=9_000)}),
                _day(TODAY, **{REVIEW: ModelUsage(input=916_000_000)}),
            ]
        )
        page = render_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert "<h2>Unpriced volume</h2>" in page
    assert "codex-auto-review" in page
    assert "916,000,000" in page
    body = page.split("<h2>Unpriced volume</h2>", 1)[1]
    section = body.split("</section>", 1)[0]
    assert "unpriced at $0" in section, "the section says $0 in words, once"
    assert not re.search(r"\$[\d,]*[1-9][\d,]*", section), (
        "unpriced volume must never carry a dollar AMOUNT"
    )
    assert re.search(r"\$[\d,]+\.\d\d\+", page), "the window total must be marked a floor"


def test_the_render_is_byte_stable_for_identical_inputs() -> None:
    """Two opens a second apart must not reshuffle the page.

    Dict iteration order and set ordering both leak into HTML if the folds are
    not sorted, and a dashboard whose legend changes colour between opens is a
    dashboard nobody reconciles against anything.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge(
            [
                _day(TODAY, **{FABLE: ModelUsage(input=3_000), SOL: ModelUsage(input=4_000)}),
                _day(
                    day_keys_back(TODAY, 3)[0],
                    **{ASTRA: ModelUsage(input=5_000), FABLE: ModelUsage(input=6_000)},
                ),
            ]
        )
        first = render_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
        second = render_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert first == second


def test_a_vendor_running_two_models_gets_a_mix_chart_and_one_running_one_does_not() -> None:
    """"Astra vs Sol over time", asked of whatever models are actually there.

    A single-model vendor's "mix" is a solid block — a chart that says nothing —
    so the panel is drawn only where there is a share to see.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        solo = _store(root / "a")
        solo.merge([_day(TODAY, **{FABLE: ModelUsage(input=1_000)})])
        assert "<h2>Model mix over time</h2>" not in render_dashboard(
            None, solo, (), (), now=NOW, pricing=DEFAULT_PRICING
        )

        mixed = _store(root / "b")
        mixed.merge(
            [
                _day(day_keys_back(TODAY, 5)[0], **{SOL: ModelUsage(input=90_000)}),
                _day(TODAY, **{ASTRA: ModelUsage(input=120_000)}),
            ]
        )
        page = render_dashboard(None, mixed, (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert "<h2>Model mix over time</h2>" in page
    assert "gpt-6-astra" in page and "gpt-5.6-sol" in page


# ---------------------------------------------------------------------------
# 3. Sections that appear only when their data does
# ---------------------------------------------------------------------------


def test_the_project_section_is_absent_until_history_carries_projects() -> None:
    """Roadmap item 6 lands in a sibling branch; until then, no heading.

    Absent, not empty: a "By project" heading over a blank table reads as "no
    projects used", which is a claim this build cannot make.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        history = _history(root)
        history.upsert_day(
            TODAY, _day(TODAY, **{FABLE: ModelUsage(input=8_000)}), DEFAULT_PRICING
        )
        page = render_dashboard(history, _store(root), (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert "<h2>By project</h2>" not in page


def test_the_project_section_reads_the_real_store_s_project_rows() -> None:
    """Through a REAL ``HistoryStore``, populated the way the widget populates it.

    This is the test that failed on 2026-09-10: the section read ``rows()``,
    whose ``project`` is empty for every row by contract (the aggregate table),
    and never called ``project_rows()``, which is where ``upsert_projects``
    puts the decomposition. Every stub in this module answers ``rows()``, so
    the bug was invisible to all of them - a machine with months of project
    data got no "By project" section at all.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        history = _history(root)
        # The aggregate the widget always writes...
        history.upsert_day(
            TODAY,
            _day(TODAY, **{FABLE: ModelUsage(input=5_500_000)}),
            DEFAULT_PRICING,
        )
        # ...and its decomposition, exactly as `_mirror_attribution` writes it.
        history.upsert_projects(
            [
                (TODAY, "linkagent", _day(TODAY, **{FABLE: ModelUsage(input=4_400_000)})),
                (TODAY, "usage-bar", _day(TODAY, **{FABLE: ModelUsage(input=1_100_000)})),
            ],
            DEFAULT_PRICING,
        )
        assert all(row.project == "" for row in history.rows()), (
            "the aggregate table carries no project - that is why rows() cannot answer"
        )
        page = render_dashboard(history, _store(root), (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert "<h2>By project</h2>" in page
    assert "linkagent" in page and "usage-bar" in page
    assert "4,400,000" in page
    # Ranked by tokens, largest first.
    assert page.index("linkagent") < page.index("usage-bar")


def test_a_store_without_project_rows_is_still_read_through_rows() -> None:
    """The fallback: an object that only has ``rows()`` keeps working.

    ``project_rows`` is a ``HistoryStore`` method, not a protocol anyone else
    implements, so the reader asks for it and falls back rather than assuming
    it. A v1 database (no ``daily_project`` table) answers ``project_rows()``
    with ``()`` and gets no section, which is the honest result; an object with
    no such method at all gets read the only way it can be.
    """
    rows = (
        _StubRow(TODAY, FABLE, ModelUsage(input=4_400_000), 12.50, "linkagent"),
        _StubRow(TODAY, FABLE, ModelUsage(input=1_100_000), 3.25, "usage-bar"),
    )
    with tempfile.TemporaryDirectory() as name:
        page = render_dashboard(
            _StubHistory(rows), _store(Path(name)), (), (), now=NOW, pricing=DEFAULT_PRICING
        )
    assert "<h2>By project</h2>" in page
    assert "linkagent" in page and "usage-bar" in page


def test_a_page_rendered_mid_index_says_its_figures_are_partial() -> None:
    """SPEC 4.3 in the artifact that gets kept.

    The menu refuses to print a dollar figure while the first index is running;
    a page opened in the same minute drew the same half-read corpus as finished
    charts with nothing to say so. The banner carries the caller's own progress
    label, so the page and the menu say the same thing.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge([_day(TODAY, **{FABLE: ModelUsage(input=1_000_000)})])
        mid = render_dashboard(
            None, store, (), (), now=NOW, pricing=DEFAULT_PRICING,
            indexing="indexing… 1,204/3,200",
        )
        done = render_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert INDEXING_BANNER in mid
    assert "1,204/3,200" in mid
    assert 'class="banner"' in mid
    # And a finished index says nothing at all - no empty box, no reassurance.
    assert INDEXING_BANNER not in done
    assert 'class="banner"' not in done


def test_an_empty_progress_label_is_not_an_empty_red_box() -> None:
    """``""`` means "nothing to say", exactly as ``None`` does."""
    with tempfile.TemporaryDirectory() as name:
        page = render_dashboard(
            None, _store(Path(name)), (), (), now=NOW, pricing=DEFAULT_PRICING, indexing="  "
        )
    assert 'class="banner"' not in page


def test_quota_windows_render_only_the_windows_a_row_reported() -> None:
    """A window the source did not report is omitted, never drawn at zero.

    Codex has no 5-hour window and ``seven_day_opus`` is null on these
    accounts; a 0 % bar would claim both were measured and unused (SPEC 1).
    """
    with tempfile.TemporaryDirectory() as name:
        page = render_dashboard(
            None, _store(Path(name)), (_codex_row(),), (), now=NOW
        )
    section = page.split("<h2>Quota windows</h2>", 1)[1].split("</section>", 1)[0]
    assert ">weekly<" in section
    assert ">5h<" not in section, "Codex reports no 5-hour window"


def test_a_capped_window_is_marked_capped_and_a_healthy_one_is_not() -> None:
    """The one distinction that changes what the operator does next."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        capped = render_dashboard(None, _store(root), (_codex_row(),), (), now=NOW)
        healthy = render_dashboard(None, _store(root), (), (_claude_row(),), now=NOW)
    def _accounts(page: str) -> str:
        # The stylesheet names every class; only the account markup counts.
        return page.split("<h2>Quota windows</h2>", 1)[1].split("</section>", 1)[0]

    assert 'class="fill-capped"' in _accounts(capped) and "100% capped" in capped
    assert 'class="fill-capped"' not in _accounts(healthy), "42% is not a wall"
    assert 'class="fill-ok"' in _accounts(healthy)


def test_the_wall_section_says_why_there_is_no_weekly_figure() -> None:
    """Item 9 asks for hours-at-the-wall per week; nothing stores a quota time
    series, so the page says so instead of estimating one (SPEC 4.3)."""
    with tempfile.TemporaryDirectory() as name:
        page = render_dashboard(
            None, _store(Path(name)), (_codex_row(),), (_claude_row(),), now=NOW
        )
    assert "<h2>Hours at the wall</h2>" in page
    assert WALL_NO_HISTORY_NOTE in page
    section = page.split("<h2>Hours at the wall</h2>", 1)[1].split("</section>", 1)[0]
    assert "at the wall now" in section
    assert not re.search(r"\d+(\.\d+)?\s*hours", section), "no invented hours figure"


def test_no_accounts_means_no_quota_sections_at_all() -> None:
    """A Claude-only machine with the live Codex source off draws neither."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge([_day(TODAY, **{FABLE: ModelUsage(input=2_000)})])
        page = render_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
    assert "<h2>Quota windows</h2>" not in page
    assert "<h2>Hours at the wall</h2>" not in page


# ---------------------------------------------------------------------------
# 4. The file on disk
# ---------------------------------------------------------------------------


def test_the_file_is_written_0600_and_replaced_atomically() -> None:
    """It is a spend record, so it gets the mode ``history.sqlite`` gets — and
    a rename rather than a truncate, because a reload may be reading it."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        target = root / "dashboard.html"
        write_dashboard("<!doctype html><html><body>one</body></html>", target)
        assert oct(target.stat().st_mode & 0o777) == "0o600"
        write_dashboard("<!doctype html><html><body>two</body></html>", target)
        assert "two" in target.read_text(encoding="utf-8")
        assert oct(target.stat().st_mode & 0o777) == "0o600"
        assert not (root / "dashboard.html.tmp").exists(), "the temp file must be renamed away"


def test_the_dashboard_is_written_beside_the_store_it_describes() -> None:
    """A redirected cache must not overwrite the installed widget's page —
    which would then be opened next time and describe the wrong machine."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        assert dashboard_path_for(root / "rollups.json") == root / "dashboard.html"
        assert dashboard_path_for(None) == dash.DASHBOARD_PATH
        store = _store(root)
        store.merge([_day(TODAY, **{FABLE: ModelUsage(input=9_000)})])
        written = build_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
        assert written == root / "dashboard.html"
        assert written.exists()


# ---------------------------------------------------------------------------
# F1 (2026-09-25): the workflow table. Every name below is SYNTHETIC.
# ---------------------------------------------------------------------------

_WF_SESSION = "5e55a0a1-synthetic"
_WF_RUN = "wf_0badc0de-syn"


def _swarm_cache(root: Path) -> DailyRollupStore:
    """A store plus the attribution cache beside it holding one two-agent run,
    and that run's journal under ``root/projects``."""
    import json

    from cc_usage_widget.attribution import Attribution, AttributionStore, attribution_path_for
    from cc_usage_widget.contracts import VENDOR_CLAUDE

    store = _store(root)
    store.merge([_day(TODAY, **{FABLE: ModelUsage(input=3_000_000)})])
    cache = AttributionStore(path=attribution_path_for(store.path))
    for agent, tokens in (("a1", 1_000_000), ("a2", 2_000_000)):
        scope = Attribution(
            vendor=VENDOR_CLAUDE, project="SYNTHETIC_proj", session=_WF_SESSION,
            workflow=_WF_RUN, agent=agent,
        )
        cache.merge([(scope, _day(TODAY, **{FABLE: ModelUsage(input=tokens)}))])
    cache.save(force=True)
    run = root / "projects" / "-SYNTHETIC-proj" / _WF_SESSION / "subagents" / "workflows" / _WF_RUN
    run.mkdir(parents=True)
    (run / "journal.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"type": "launched"},
                {"type": "started", "agentId": "a1", "label": "<b>SYNTHETIC</b>", "phase": "Review"},
                {"type": "started", "agentId": "a2", "label": "build:SYNTHETIC", "phase": "Build"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return store


def test_the_workflow_table_is_absent_without_a_run() -> None:
    """No run, no heading - the page a machine without swarms drew before F1."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge([_day(TODAY, **{FABLE: ModelUsage(input=9_000)})])
        page = render_dashboard(None, store, now=NOW, pricing=DEFAULT_PRICING)
        assert "Workflow runs" not in page
        assert dash.WORKFLOW_NOTE not in page
        # build_dashboard with no cache beside the store neither shows the
        # table nor creates the cache file.
        written = build_dashboard(
            None, store, (), (), now=NOW, pricing=DEFAULT_PRICING,
            projects_dir=root / "projects",
        )
        assert "Workflow runs" not in written.read_text(encoding="utf-8")
        assert not (root / "attribution.json").exists(), "the dashboard created the cache"


def test_the_workflow_table_shows_each_run_with_its_phases_and_models() -> None:
    """One row per run whose tokens are its agents' sum, split per phase (from
    the journal) and per model; journal text is escaped; the cache and the
    journal are read, never written."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _swarm_cache(root)
        cache = root / "attribution.json"
        before = cache.read_bytes()
        written = build_dashboard(
            None, store, (), (), now=NOW, pricing=DEFAULT_PRICING,
            projects_dir=root / "projects",
        )
        page = written.read_text(encoding="utf-8")
        assert "Workflow runs" in page and dash.WORKFLOW_NOTE in page
        assert "wf_0badc0de · SYNTHETIC_proj" in page, page
        assert ">3,000,000<" in page, "the run's exact token sum"
        assert "phase: Review" in page and "phase: Build" in page
        assert "model: Fable 5" in page
        assert "&lt;b&gt;SYNTHETIC&lt;/b&gt;" in page and "<b>SYNTHETIC" not in page
        assert cache.read_bytes() == before, "the cache is read-only here"
        # Rendering straight from the runs is the same table (pure seam).
        from cc_usage_widget.attribution import AttributionStore

        loaded = AttributionStore(path=cache)
        loaded.load()
        runs = loaded.workflow_runs(DEFAULT_PRICING, projects_dir=root / "projects")
        assert "phase: Review" in render_dashboard(
            None, store, now=NOW, pricing=DEFAULT_PRICING, workflow_runs=runs
        )


# ---------------------------------------------------------------------------
# 5. The wiring in app.py
# ---------------------------------------------------------------------------


def _wired(root: Path) -> tuple[Any, DailyRollupStore]:
    """An app whose worker holds a real store, prices and a scanner."""
    store = _store(root)
    app = app_mod.CCUsageWidgetApp()
    app._worker._rollups = store
    app._worker._pricing = DEFAULT_PRICING
    app._worker._indexer = Indexer(
        projects_dir=root / "projects",
        state_path=root / "scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
    )
    return app, store


def test_open_dashboard_sits_in_the_cost_section_and_vanishes_when_off() -> None:
    """The off switch means the menu is byte-for-byte the pre-roadmap layout."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        app, store = _wired(root)
        try:
            store.merge([_day(TODAY, **{FABLE: ModelUsage(input=1_000)})])
            breakdown = store.cost_breakdown(
                DEFAULT_PRICING, today=TODAY, progress=IndexProgress(complete=True)
            )
            settings = normalize_settings(dict(SETTINGS_DEFAULTS))
            # The exports are only offered once the mirror holds something
            # (roadmap 8 / integration fix 1); this test is about where "Open
            # dashboard" SITS relative to them, so say the mirror has rows.
            app._worker._history_rows = 3
            on = [
                str(item.title)
                for item in app._cost_items(UiSnapshot(settings=settings, cost=breakdown))
            ]
            assert "Open dashboard" in on, on
            assert on.index("Open dashboard") < on.index("Export usage (CSV)…"), on

            off_settings = normalize_settings({**SETTINGS_DEFAULTS, "dashboard_enabled": False})
            off = [
                str(item.title)
                for item in app._cost_items(UiSnapshot(settings=off_settings, cost=breakdown))
            ]
            assert off == [title for title in on if title != "Open dashboard"], off
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_the_menu_item_carries_a_callback_that_only_enqueues() -> None:
    """SPEC 2.3: the AppKit thread does no I/O — it submits and returns."""
    app = app_mod.CCUsageWidgetApp()
    try:
        settings = normalize_settings(dict(SETTINGS_DEFAULTS))
        items = app._dashboard_items(UiSnapshot(settings=settings))
        assert [str(item.title) for item in items] == ["Open dashboard"]
        assert items[0].callback is not None
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_browser_hand_off_is_suppressed_and_says_so() -> None:
    """``_open_in_browser`` is ``_reveal_in_finder``'s sibling, same guard.

    Asserting the LOG line, not only the ``False``: ``NSWorkspace`` also returns
    False for a path that does not exist, so a return value alone would go on
    passing if the guard were deleted - and the next test, which hands it a
    file that DOES exist, would then open a browser tab on someone's desktop.
    The suppression line is the only evidence that the guard, rather than the
    missing file, is what stopped it.
    """
    missing = Path(tempfile.gettempdir()) / "cc-usage-widget-no-such-dashboard.html"
    assert not missing.exists(), "this test must hand over a path that cannot open"
    captured = io.StringIO()
    saved, sys.stderr = sys.stderr, captured
    try:
        result = app_mod._open_in_browser(missing)
    finally:
        sys.stderr = saved
    assert result is False
    assert "open suppressed (CC_USAGE_WIDGET_NO_REVEAL)" in captured.getvalue(), (
        captured.getvalue()
    )


def test_the_worker_writes_the_file_and_opens_no_window_under_the_guard() -> None:
    """The command runs end to end on the worker, and the desktop stays shut.

    ``CC_USAGE_WIDGET_NO_REVEAL`` is set at import for exactly this: the
    claude-swap venv HAS PyObjC, so without the guard this test would open a
    browser tab on whatever desktop the suite runs on.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        # The worker renders on the WALL clock, not the fixture's, so the day
        # seeded here has to be the real one or it falls outside the window.
        store.merge([_day(local_day_key(time.time()), **{FABLE: ModelUsage(input=42_000)})])
        worker = BackgroundWorker(
            publish=lambda _snapshot: None,
            snapshot=UiSnapshot(
                settings=normalize_settings(dict(SETTINGS_DEFAULTS)),
                quota_rows=(_codex_row(),),
                accounts=(_claude_row(),),
            ),
            accounts=None,
            indexer=None,
            rollups=store,
            pricing=DEFAULT_PRICING,
        )
        assert app_mod._open_in_browser(root / "anything.html") is False, (
            "the guard must suppress the hand-off"
        )
        handled = worker._handle_command((app_mod._CMD_OPEN_DASHBOARD, None))
        assert handled == (True, False), handled
        written = root / "dashboard.html"
        assert written.exists(), "the worker must write the page it was asked for"
        assert oct(written.stat().st_mode & 0o777) == "0o600"
        assert "42,000" in written.read_text(encoding="utf-8")
        assert worker.dashboard_note == f"Dashboard: {written}"
        assert worker.dashboard_error is None


def test_the_worker_stamps_the_banner_while_its_scanners_are_still_indexing() -> None:
    """The guard the menu had and the dashboard did not (2026-09-10).

    ``_export_history`` refuses to write a file that would read as a record of
    a month in which nothing was spent; ``_open_dashboard`` had no equivalent,
    so the first thing a new install did - press "Open dashboard" while the
    first scan was still walking a 1.4 GB corpus - produced a page of partial
    totals presented as the whole. The condition is the menu's own: no
    breakdown yet, or one whose ``is_partial`` is True.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge([_day(TODAY, **{FABLE: ModelUsage(input=42_000)})])
        settings = normalize_settings(dict(SETTINGS_DEFAULTS))

        scanning = BackgroundWorker(
            publish=lambda _snapshot: None,
            snapshot=UiSnapshot(
                settings=settings,
                progress=IndexProgress(files_done=1_204, files_total=3_200),
            ),
            accounts=None,
            indexer=None,
            rollups=store,
            pricing=DEFAULT_PRICING,
        )
        scanning._handle_command((app_mod._CMD_OPEN_DASHBOARD, None))
        page = (root / "dashboard.html").read_text(encoding="utf-8")
        assert INDEXING_BANNER in page, "a mid-index page must say so"
        assert "1,204/3,200" in page, page[:600]

        # The same store, once the scan is done: the page carries no banner.
        breakdown = store.cost_breakdown(
            DEFAULT_PRICING, today=TODAY, progress=IndexProgress(complete=True)
        )
        finished = BackgroundWorker(
            publish=lambda _snapshot: None,
            snapshot=UiSnapshot(settings=settings, cost=breakdown),
            accounts=None,
            indexer=None,
            rollups=store,
            pricing=DEFAULT_PRICING,
        )
        finished._handle_command((app_mod._CMD_OPEN_DASHBOARD, None))
        done = (root / "dashboard.html").read_text(encoding="utf-8")
        assert INDEXING_BANNER not in done, "a finished index must not claim to be running"


def test_the_dashboard_open_is_parked_for_the_appkit_thread_not_called_by_the_worker() -> None:
    """The page is built on the worker; ``openURL:`` happens on the AppKit one.

    ``NSWorkspace`` is AppKit and AppKit is main-thread-only. That the call
    happened to return without crashing from the worker is today's luck, not a
    contract, and ``CC_USAGE_WIDGET_NO_REVEAL`` makes a wrong-thread call look
    identical to no call at all - so the structural half of this assertion (no
    method of ``BackgroundWorker`` names the helper) is the half that can fail.
    """
    import inspect

    source = inspect.getsource(BackgroundWorker)
    assert "_open_in_browser(" not in source, "the worker must not open a browser"

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge([_day(local_day_key(time.time()), **{FABLE: ModelUsage(input=42_000)})])
        worker = BackgroundWorker(
            publish=lambda _snapshot: None,
            snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
            accounts=None,
            indexer=None,
            rollups=store,
            pricing=DEFAULT_PRICING,
        )
        worker._handle_command((app_mod._CMD_OPEN_DASHBOARD, None))
        parked = worker.take_desktop_requests()
        assert len(parked) == 1, parked
        action, path = parked[0]
        assert action == app_mod._DESKTOP_OPEN, action
        assert Path(path) == root / "dashboard.html", path
        # Once. Two repaints must not open two tabs on one page.
        assert worker.take_desktop_requests() == ()

        # The AppKit side performs it, through a seam a test can record.
        app = app_mod.CCUsageWidgetApp()
        try:
            seen: list[tuple[str, str]] = []
            app._desktop_handoff = lambda action, path: (  # type: ignore[assignment]
                seen.append((action, str(path))) or True
            )
            app._worker._ask_desktop(app_mod._DESKTOP_OPEN, path)
            assert app._drain_desktop_handoffs() == 1
            assert seen == [(app_mod._DESKTOP_OPEN, str(path))], seen
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_the_repaint_tick_is_what_performs_the_parked_hand_offs() -> None:
    """Parking is only half of it: something on the AppKit thread has to drain.

    ``_on_sync_tick`` is the widget's only main-thread heartbeat, so it is the
    one place a hand-off can be performed without a second timer to keep alive.
    Without this test the parking could be wired perfectly and the export would
    simply never be revealed - a silent regression, because the file is written
    either way and only the window is missing.
    """
    app = app_mod.CCUsageWidgetApp()
    try:
        seen: list[tuple[str, str]] = []
        app._desktop_handoff = lambda action, path: (  # type: ignore[assignment]
            seen.append((action, str(path))) or True
        )
        app._worker._ask_desktop(app_mod._DESKTOP_OPEN, Path("/tmp/some-dashboard.html"))
        app._on_sync_tick(None)
        assert seen == [(app_mod._DESKTOP_OPEN, "/tmp/some-dashboard.html")], seen
        # A second tick with nothing parked does nothing at all.
        app._on_sync_tick(None)
        assert len(seen) == 1, seen
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_off_switch_stops_the_worker_writing_anything() -> None:
    """Off means off from every direction: no file is created at all."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        store = _store(root)
        store.merge([_day(local_day_key(time.time()), **{FABLE: ModelUsage(input=42_000)})])
        worker = BackgroundWorker(
            publish=lambda _snapshot: None,
            snapshot=UiSnapshot(
                settings=normalize_settings({**SETTINGS_DEFAULTS, "dashboard_enabled": False})
            ),
            accounts=None,
            indexer=None,
            rollups=store,
            pricing=DEFAULT_PRICING,
        )
        worker._handle_command((app_mod._CMD_OPEN_DASHBOARD, None))
        assert not (root / "dashboard.html").exists()
        assert worker.dashboard_note is None


def test_a_dashboard_failure_shows_as_a_bang_line_and_never_raises() -> None:
    """Rule 12: a feature that broke says so rather than going quiet — and it
    does not take the worker's command loop down with it."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)

        class _Exploding:
            path = root / "rollups.json"

            def last_n_days(self, *_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("store is on fire")

        app = app_mod.CCUsageWidgetApp()
        try:
            worker = app._worker
            worker._rollups = _Exploding()
            worker._pricing = DEFAULT_PRICING
            settings = normalize_settings(dict(SETTINGS_DEFAULTS))
            assert not any(
                "dashboard" in str(item.title)
                for item in app._problem_items(UiSnapshot(settings=settings))
            ), "a healthy dashboard adds no line at all"

            worker._dashboard_error = "dashboard failed: RuntimeError: store is on fire"
            titles = [
                str(item.title) for item in app._problem_items(UiSnapshot(settings=settings))
            ]
            assert any(t.startswith("! dashboard failed:") for t in titles), titles
        finally:
            app._running = False
            app._worker.stop(timeout=2.0)


def test_a_store_that_raises_does_not_take_the_render_down() -> None:
    """``collect_cells`` traps its own reads: a broken store costs the page its
    charts, not its existence."""

    class _Exploding:
        def last_n_days(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("nope")

    class _ExplodingHistory:
        def rows(self, **_kwargs: Any) -> Any:
            raise RuntimeError("also nope")

    page = render_dashboard(_ExplodingHistory(), _Exploding(), (), (), now=NOW)
    assert EMPTY_MARKER in page


# ---------------------------------------------------------------------------
# Runner (pytest is not installed in claude-swap's venv)
# ---------------------------------------------------------------------------


def _tests() -> list[tuple[str, Any]]:
    items = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def main() -> int:
    failures: list[str] = []
    tests = _tests()
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
