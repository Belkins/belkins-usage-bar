"""A single-file local dashboard for what the widget already measured
(roadmap item 9).

What this is
------------

The menu answers "how much today, on which model". It cannot answer "was last
week worse than the one before", "when did Astra take over from Sol", or "which
account spent Thursday at the wall" - those are shapes, and a menu is a column
of text. This module renders those shapes ONCE, into one ``dashboard.html``
that opens from ``file://`` with no server, no build step and no network:
inline CSS, inline SVG, and not one ``<script>`` tag.

Why one file with no scripts
----------------------------

Three reasons, in order of how much they cost to get wrong:

1. **It is a spend record.** Every figure on the page is what someone's Claude
   and Codex usage cost. A CDN reference would put that page's existence (and,
   through a referrer, its path) in someone else's log. The page therefore
   loads nothing: :func:`render_dashboard` emits a document whose only external
   dependency is a browser.
2. **It must still open in a year.** The rollup store is a 30-day cache and the
   transcripts behind it are pruned sooner than that; ``history.sqlite`` is the
   only long-term record and this page is how a human reads it. A page that
   needs a library version to render is a page that stops rendering.
3. **The widget is a menu-bar app with a <0.3 % idle budget (SPEC 2.1).**
   Rendering happens on the worker thread, on demand, when the user picks
   ``Open dashboard`` - never on a tick, never in the background. The file is
   regenerated on every open, so it is a snapshot with a timestamp on it rather
   than a cache anyone has to invalidate.

Honesty rules this module inherits (SPEC 4.3)
---------------------------------------------

* **No invented data, ever.** An empty store renders an empty state that says
  so. There is no sample series, no placeholder bar, no "example" account.
* **Unpriced volume is tokens, never dollars.** ``codex-auto-review`` has no
  published rate; it appears with its token magnitude and a ``$0`` that is
  labelled as unpriced rather than folded into a total that would then be
  wrong (roadmap item 3, the same rule the menu follows).
* **Two sources, one precedence, stated on the page.** A day the rollup store
  still covers is read from the store; older days come from the history
  mirror. The store wins where both hold a day, because a rebuild or an audit
  repair lands there first - the mirror can lag by one cost job.
* **What is not measured is not drawn.** The widget records *usage* over time,
  not *quota* over time: nothing anywhere persists "account X was capped from
  14:00 to 22:00". So the "hours at the wall" panel reports the wall each
  account is at NOW and the reset it reported, and says in one line why a
  weekly total is absent, instead of estimating one.

Threading
---------

:func:`render_dashboard` is pure: it reads the stores it is handed and returns
a string. :func:`write_dashboard` does the file I/O. Both belong on the worker
thread; handing the finished file to the browser is the caller's job (see
``app._open_in_browser``, which is the same guarded seam as the Finder reveal).
"""

from __future__ import annotations

import datetime as dt
import html
import math
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .contracts import (
    NOTIONAL_LABEL,
    PROJECTS_DIR,
    VENDORS,
    WIDGET_HOME,
    AccountRow,
    DayKey,
    ModelUsage,
    PricingTable,
    Usd,
    Vendor,
    day_keys_back,
    format_tokens,
    format_usd,
    local_day_key,
    parse_day_key,
    raw_model_of_key,
    vendor_label,
    vendor_of_key,
)
from .render import coarse_duration, fleet_reset_label, plan_label

__all__ = [
    "Cell",
    "DASHBOARD_FILENAME",
    "DASHBOARD_PATH",
    "EMPTY_MARKER",
    "INDEXING_BANNER",
    "RESET_RADAR_HEADING",
    "WINDOW_DAYS",
    "WALL_NO_HISTORY_NOTE",
    "WORKFLOW_NOTE",
    "build_dashboard",
    "collect_cells",
    "dashboard_path_for",
    "estimate_monthly_seat_usd",
    "render_dashboard",
    "write_dashboard",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DASHBOARD_FILENAME: Final[str] = "dashboard.html"

DASHBOARD_PATH: Final[Path] = WIDGET_HOME / DASHBOARD_FILENAME
"""Where ``Open dashboard`` writes by default.

Beside ``rollups.json`` and ``history.sqlite``, not in ``~/Downloads``: it is
regenerated on every open and is state, not a document the user keeps. 0600
for the same reason ``history.sqlite`` is - it is a record of someone's spend.
"""

FILE_MODE: Final[int] = 0o600

WINDOW_DAYS: Final[tuple[int, ...]] = (30, 90)
"""The two windows the page reports, shortest first.

30 is the rollup store's own ``lookback_days`` default - the window the menu
already talks about - and 90 is the one only the history mirror can answer,
which is the whole reason the mirror exists.
"""

MAX_MODELS: Final[int] = 8
"""Models drawn individually before the tail collapses into ``other``.

A collapsed tail is still counted: the ``other`` row carries its own tokens and
dollars, so the bars sum to the window total rather than to "the top eight".
"""

MAX_PROJECTS: Final[int] = 12

MAX_WORKFLOW_RUNS: Final[int] = 12
"""Rows in the workflow table - the dearest runs, like :data:`MAX_PROJECTS`."""

WORKFLOW_NOTE: Final[str] = (
    "Workflow swarms of today and yesterday (the session window), dearest first. "
    "Tokens are the sum of the agents of each run; phases and agent labels come from "
    "the journal.jsonl of the run, and an agent it does not name is listed as (no phase)."
)
"""The workflow table's caption, named so a test asserts the wording."""

EMPTY_MARKER: Final[str] = "No usage recorded yet"
"""The empty state's exact wording, named so a test asserts the string rather
than a class name that could drift."""

RESET_RADAR_HEADING: Final[str] = "Fleet Reset Timeline & Capacity Forecast"
"""Heading for the unified Claude + Codex Reset Timeline & Capacity Forecast."""

INDEXING_BANNER: Final[str] = (
    "The index is still being built, so every figure below is partial and will "
    "grow as the scan finishes."
)
"""Stamped into the header while a scanner is still on its first pass.

SPEC 4.3's honesty rule, carried onto the page: the menu refuses to print a
dollar figure before the first index completes (``_cost_items`` renders
``indexing…`` instead), and a dashboard opened in that window would otherwise
show the same half-read corpus as finished charts - worse than the menu,
because a page is what gets kept, mailed and read again next week. The page is
still rendered rather than refused: half the corpus is a real measurement of
half the corpus, and saying so is what makes it usable."""

WALL_NO_HISTORY_NOTE: Final[str] = (
    "No quota history is recorded, so hours-at-the-wall per week is not derivable "
    "and is not estimated here - only the wall each account reports right now."
)
"""Item 9 asks for "hours at the wall per account per week". Nothing in this
program stores a quota time series - the sidecar keeps a 12-sample ring (about
an hour) for the pace note, and the history mirror stores usage, not windows -
so the honest answer is this sentence plus the live figures, never a number
derived from data nobody kept."""

_UNPRICED_LABEL: Final[str] = "unpriced at $0"

# A brand-neutral categorical ramp. Hues are spaced far enough apart to stay
# distinguishable side by side, and each has a dark-theme twin lightened rather
# than re-hued, so a series keeps its identity when the theme flips.
_SERIES_LIGHT: Final[tuple[str, ...]] = (
    "#2f6f9f",
    "#c1622a",
    "#3f8b62",
    "#8a55a8",
    "#a8455f",
    "#6b7a8c",
    "#94802a",
    "#3f8698",
)
_SERIES_DARK: Final[tuple[str, ...]] = (
    "#6aa8d8",
    "#e8925a",
    "#6fbd92",
    "#b98cd6",
    "#d97e93",
    "#9aa8b8",
    "#c7b155",
    "#6fb6c8",
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def dashboard_path_for(rollups_path: Path | str | None) -> Path:
    """Where the dashboard belongs for a store kept at *rollups_path*.

    The twin of :func:`~cc_usage_widget.history.history_path_for`, and for the
    same reason: a test (or an operator) that redirects the state directory
    must not have its dashboard written into the INSTALLED widget's home, where
    it would be opened next time and quietly describe the wrong machine.
    ``None`` falls back to :data:`DASHBOARD_PATH`.
    """
    if rollups_path is None:
        return DASHBOARD_PATH
    return Path(rollups_path).expanduser().parent / DASHBOARD_FILENAME


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cell:
    """One ``(day, vendor-qualified model key)`` cell, priced.

    The unit both sources reduce to: a rollup's ``models`` entry and a history
    row carry exactly this much, so every chart below is a fold over a tuple of
    these and nothing downstream needs to know which store a figure came from.
    ``source`` records that anyway, because the page states its own provenance.
    """

    day: DayKey
    key: str
    vendor: Vendor
    model: str
    usage: ModelUsage
    usd: Usd
    source: str
    project: str = ""

    @property
    def tokens(self) -> int:
        return self.usage.total_tokens

    @property
    def unpriced(self) -> bool:
        """Tokens that cost ``$0`` at the rates we hold - a floor, not a total.

        The same rule the menu's ``+`` suffix encodes: a model with no
        published rate is counted in tokens and named, never rolled into a
        dollar figure that would then understate the window silently.
        """
        return self.tokens > 0 and self.usd <= 0.0


def _safe_cost(
    pricing: PricingTable | None, key: str, usage: ModelUsage, day: DayKey
) -> Usd:
    """``pricing.cost_usd`` with every failure flattened to ``0.0``.

    A price table that raises must cost the page a dollar figure, not the whole
    render: the tokens are still true and still drawn.
    """
    if pricing is None:
        return 0.0
    try:
        value = pricing.cost_usd(key, usage, parse_day_key(day))
    except Exception:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return 0.0
    return value


def _history_rows(history: Any, since: DayKey) -> tuple[Any, ...]:
    """Rows from the mirror, or ``()``.

    ``HistoryStore.rows`` already traps its own errors and returns ``()``; the
    guard here is for a ``None`` store (history disabled, or the module failed
    to import) and for any other object the caller wires in.
    """
    if history is None:
        return ()
    try:
        rows = history.rows(since=since)
    except Exception:
        return ()
    return tuple(rows or ())


def _history_project_rows(history: Any, since: DayKey) -> tuple[Any, ...]:
    """Rows from the mirror's PROJECT table, or ``()``.

    ``HistoryStore`` keeps two populations in one file: ``rows()`` is the
    whole-day aggregate, where ``project`` is empty by contract, and
    ``project_rows()`` is the decomposition roadmap item 6 writes. Reading the
    first and filtering it on ``project`` - which is what this page did until
    2026-09-10 - can only ever produce an empty table, so the section a real
    machine has data for never appeared.

    ``rows()`` is still the fallback for a store that predates
    ``project_rows`` (and for any other object a caller wires in): on such a
    store the project column is wherever it is, and asking the only reader it
    has is better than asserting it has none.
    """
    if history is None:
        return ()
    reader = getattr(history, "project_rows", None)
    if not callable(reader):
        reader = getattr(history, "rows", None)
    if not callable(reader):
        return ()
    try:
        rows = reader(since=since)
    except Exception:
        return ()
    return tuple(rows or ())


def collect_cells(
    history: Any,
    rollups: Any,
    *,
    today: DayKey,
    days: int,
    pricing: PricingTable | None = None,
) -> tuple[Cell, ...]:
    """Every priced cell in the *days*-long window ending on *today*.

    Precedence, stated once here and printed on the page: **the rollup store
    wins for any day it covers.** The mirror is written at the end of a cost
    job, so between a rebuild (or an audit repair) and the next mirror pass the
    two disagree, and the store is the one that was just corrected. Days the
    store no longer covers - anything past ``lookback_days`` - come from the
    mirror, which is the only place they still exist.

    Returns cells sorted by ``(day, vendor, key)`` so the page is byte-stable
    for identical inputs; a dashboard that reshuffles between two opens is a
    dashboard nobody trusts.
    """
    window = day_keys_back(today, days)
    if not window:
        return ()
    in_window = set(window)
    cells: list[Cell] = []

    from_store: dict[DayKey, Any] = {}
    try:
        for rollup in rollups.last_n_days(days, today=today) if rollups else ():
            if rollup.models:
                from_store[rollup.day] = rollup
    except Exception:
        from_store = {}

    for day, rollup in from_store.items():
        for key, usage in rollup.models.items():
            if usage.total_tokens <= 0:
                continue
            cells.append(_cell(day, str(key), usage, None, pricing, "store"))

    for row in _history_rows(history, window[0]):
        day = str(getattr(row, "day", ""))
        if day not in in_window or day in from_store:
            continue
        usage = getattr(row, "usage", None)
        if not isinstance(usage, ModelUsage) or usage.total_tokens <= 0:
            continue
        cells.append(
            _cell(
                day,
                str(getattr(row, "key", "")),
                usage,
                getattr(row, "usd_at_record", None),
                pricing,
                "history",
                project=str(getattr(row, "project", "") or ""),
            )
        )

    cells.sort(key=lambda cell: (cell.day, cell.vendor, cell.key))
    return tuple(cells)


def _cell(
    day: DayKey,
    key: str,
    usage: ModelUsage,
    recorded_usd: Any,
    pricing: PricingTable | None,
    source: str,
    *,
    project: str = "",
) -> Cell:
    """Price one cell, preferring today's table and falling back to the record.

    ``usd_at_record`` is what the day cost at the rates in effect then. Today's
    table is preferred because it is the same number the menu is showing right
    now, and a rate that has since been published turns an old ``$0`` into a
    real figure. When the table has nothing (an unknown model, a table that
    raised), the recorded figure is used rather than dropping to ``$0`` - it is
    a measurement, not a guess.
    """
    usd = _safe_cost(pricing, key, usage, day)
    if usd <= 0.0 and isinstance(recorded_usd, (int, float)) and not isinstance(recorded_usd, bool):
        recorded = float(recorded_usd)
        if recorded > 0 and recorded == recorded and recorded != float("inf"):
            usd = recorded
    return Cell(
        day=day,
        key=key,
        vendor=vendor_of_key(key),
        model=raw_model_of_key(key),
        usage=usage,
        usd=usd,
        source=source,
        project=project,
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Series:
    """One drawable series: a label, a token sum and a dollar sum."""

    label: str
    tokens: int
    usd: Usd
    unpriced_tokens: int = 0


def _window_cells(cells: Sequence[Cell], window: Sequence[DayKey]) -> tuple[Cell, ...]:
    days = set(window)
    return tuple(cell for cell in cells if cell.day in days)


def _by(
    cells: Iterable[Cell], attribute: str, labeller: Any = str
) -> tuple[_Series, ...]:
    """Fold cells into series by one attribute, largest tokens first."""
    tokens: dict[str, int] = {}
    usd: dict[str, float] = {}
    unpriced: dict[str, int] = {}
    for cell in cells:
        bucket = str(getattr(cell, attribute))
        tokens[bucket] = tokens.get(bucket, 0) + cell.tokens
        usd[bucket] = usd.get(bucket, 0.0) + cell.usd
        if cell.unpriced:
            unpriced[bucket] = unpriced.get(bucket, 0) + cell.tokens
    series = [
        _Series(labeller(bucket), count, usd.get(bucket, 0.0), unpriced.get(bucket, 0))
        for bucket, count in tokens.items()
    ]
    series.sort(key=lambda item: (-item.tokens, item.label))
    return tuple(series)


def _by_vendor(cells: Iterable[Cell]) -> tuple[_Series, ...]:
    """Vendors in :data:`VENDORS` order, so Claude is always the first colour.

    Ordering by size instead would swap the palette between two opens on a
    machine whose vendors trade places, which reads as a change in the data.
    """
    folded = {series.label: series for series in _by(cells, "vendor")}
    return tuple(
        folded[vendor] for vendor in VENDORS if vendor in folded
    ) + tuple(
        series for label, series in sorted(folded.items()) if label not in VENDORS
    )


def _capped(series: Sequence[_Series], limit: int) -> tuple[_Series, ...]:
    """Top *limit* series, with the tail folded into one ``other`` row.

    The tail is kept rather than dropped: the bars must sum to the window
    total, or the page quietly disagrees with the menu.
    """
    if len(series) <= limit:
        return tuple(series)
    head = tuple(series[: limit - 1])
    tail = series[limit - 1 :]
    other = _Series(
        f"other ({len(tail)} models)",
        sum(item.tokens for item in tail),
        sum(item.usd for item in tail),
        sum(item.unpriced_tokens for item in tail),
    )
    return head + (other,)


# ---------------------------------------------------------------------------
# HTML primitives
# ---------------------------------------------------------------------------


_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"([A-Za-z0-9._%+-]+)@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:Bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:sk-ant-|sk-proj-|sk-live-|ghp_|eyJ)[A-Za-z0-9._~+/=-]{8,})"
)


def _esc(value: Any) -> str:
    """HTML-escape anything on its way into the document.

    Model names and project names reach this page from disk - a transcript's
    ``message.model``, a directory name - so they are untrusted text, not
    literals. One unescaped ``<`` would turn a filename into markup.
    """
    return html.escape(str(value), quote=True)


def _sanitize_text(value: Any) -> str:
    """Strip raw email domains and bearer/API token strings before rendering."""
    text = str(value or "")
    if not text:
        return ""
    text = _EMAIL_RE.sub(r"\1", text)
    text = _TOKEN_RE.sub("[redacted]", text)
    return text


def _safe_alias(raw: Any, *, vendor: str = "", slot: int = 0) -> str:
    """Display-safe account alias — never emits raw email addresses or tokens."""
    cleaned = _sanitize_text(raw).strip()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0].strip()
    if cleaned:
        return cleaned
    if vendor == "codex":
        return f"codex-{abs(slot)}" if slot else "Codex"
    if slot > 0:
        return f"slot-{slot}"
    return vendor_label(vendor) if vendor else "account"


def _clip(text: str, limit: int) -> str:
    """Truncate plain text BEFORE HTML-escaping so entities are never split."""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _int(value: int) -> str:
    """``1234567`` -> ``"1,234,567"``: the exact figure, always present.

    Every compact ``1.2M`` on this page sits beside its exact integer, because
    the point of the dashboard is that the numbers are traceable to the store
    and ``1.2M`` traces to nothing.
    """
    return f"{int(value):,}"


def _pct_of(part: float, whole: float) -> float:
    return (part / whole * 100.0) if whole > 0 else 0.0


def _round(value: float) -> str:
    """Coordinates at two decimals - enough for a 900 px canvas, and stable."""
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def _color(index: int) -> str:
    """The CSS variable for series *index*, wrapping round the ramp."""
    return f"var(--s{index % len(_SERIES_LIGHT) + 1})"


def _stacked_bar(series: Sequence[_Series], *, value: str, label: str) -> str:
    """One horizontal 100 %-stacked bar plus its legend.

    *value* selects ``tokens`` or ``usd``. A zero total renders nothing - an
    empty bar with a legend under it says "we measured zero", which is a
    different claim from "we measured nothing".
    """
    amounts = [float(getattr(item, value)) for item in series]
    total = sum(amounts)
    if total <= 0:
        return ""
    width = 900.0
    parts: list[str] = []
    x = 0.0
    for index, (item, amount) in enumerate(zip(series, amounts)):
        span = width * (amount / total)
        if span <= 0:
            continue
        share = _pct_of(amount, total)
        parts.append(
            f'<rect x="{_round(x)}" y="0" width="{_round(span)}" height="26" '
            f'fill="{_color(index)}"><title>{_esc(item.label)}: '
            f"{_esc(_value_text(item, value))} ({share:.1f}%)</title></rect>"
        )
        x += span
    svg = (
        f'<svg class="bar" viewBox="0 0 900 26" preserveAspectRatio="none" '
        f'role="img" aria-label="{_esc(label)}">' + "".join(parts) + "</svg>"
    )
    legend = "".join(
        f'<li><span class="swatch" style="background:{_color(index)}"></span>'
        f"<span class=\"key\">{_esc(item.label)}</span>"
        f'<span class="num">{_esc(_value_text(item, value))}</span></li>'
        for index, item in enumerate(series)
        if float(getattr(item, value)) > 0
    )
    return f'<div class="chart">{svg}<ul class="legend">{legend}</ul></div>'


def _value_text(item: _Series, value: str) -> str:
    if value == "usd":
        return format_usd(item.usd)
    return f"{format_tokens(item.tokens)} tok ({_int(item.tokens)})"


def _columns(
    days: Sequence[DayKey],
    stacks: Mapping[DayKey, Sequence[tuple[str, float]]],
    order: Sequence[str],
    *,
    label: str,
    normalize: bool = False,
    fmt_value: Any = None,
) -> str:
    """A stacked column chart over *days*: one column per day, in order.

    Days with nothing are drawn as a gap rather than skipped, so the x axis is
    real time and a quiet week looks like a quiet week. *normalize* turns each
    column into a 100 % share (the "who took over from whom" question);
    otherwise the columns share one absolute scale.
    """
    if not days:
        return ""
    height = 150.0
    width = 900.0
    step = width / len(days)
    bar_width = max(1.0, step * 0.8)
    totals = {
        day: sum(amount for _key, amount in stacks.get(day, ())) for day in days
    }
    peak = max(totals.values()) if totals else 0.0
    if peak <= 0:
        return ""
    index_of = {key: position for position, key in enumerate(order)}
    parts: list[str] = []
    for column, day in enumerate(days):
        entries = stacks.get(day, ())
        total = totals[day]
        if total <= 0:
            continue
        scale = height if normalize else height * (total / peak)
        y = height
        x = column * step + (step - bar_width) / 2.0
        for key, amount in sorted(
            entries, key=lambda pair: index_of.get(pair[0], len(order))
        ):
            if amount <= 0:
                continue
            span = scale * (amount / total)
            y -= span
            rendered_val = (
                fmt_value(amount) if callable(fmt_value) else _int(int(amount))
            )
            parts.append(
                f'<rect x="{_round(x)}" y="{_round(y)}" width="{_round(bar_width)}" '
                f'height="{_round(span)}" fill="{_color(index_of.get(key, 0))}">'
                f"<title>{_esc(day)} {_esc(key)}: {_esc(rendered_val)}</title></rect>"
            )
    axis = (
        f'<line class="axis" x1="0" y1="{_round(height)}" x2="{_round(width)}" '
        f'y2="{_round(height)}"></line>'
    )
    first, last = _esc(days[0]), _esc(days[-1])
    return (
        f'<div class="chart"><svg class="cols" viewBox="0 0 900 {_round(height + 1)}" '
        f'preserveAspectRatio="none" role="img" aria-label="{_esc(label)}">'
        + "".join(parts)
        + axis
        + "</svg>"
        + f'<div class="axis-labels"><span>{first}</span><span>{last}</span></div></div>'
    )


def _legend(order: Sequence[str]) -> str:
    return '<ul class="legend">' + "".join(
        f'<li><span class="swatch" style="background:{_color(index)}"></span>'
        f'<span class="key">{_esc(key)}</span></li>'
        for index, key in enumerate(order)
    ) + "</ul>"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _section(title: str, body: str, *, note: str = "", ident: str = "") -> str:
    if not body:
        return ""
    anchor = f' id="{_esc(ident)}"' if ident else ""
    prose = f'<p class="note">{_esc(note)}</p>' if note else ""
    return f"<section{anchor}><h2>{_esc(title)}</h2>{prose}{body}</section>"


def _totals_section(cells: Sequence[Cell], today: DayKey) -> str:
    """30/90-day totals by vendor: tokens and notional dollars, side by side."""
    blocks: list[str] = []
    for days in WINDOW_DAYS:
        window = day_keys_back(today, days)
        scoped = _window_cells(cells, window)
        if not scoped:
            continue
        series = _by_vendor(scoped)
        tokens = sum(item.tokens for item in series)
        usd = sum(item.usd for item in series)
        unpriced = sum(item.unpriced_tokens for item in series)
        floor = "+" if unpriced else ""
        rows = "".join(
            f"<tr><td>{_esc(vendor_label(item.label))}</td>"
            f"<td class=\"num\">{_esc(format_tokens(item.tokens))}</td>"
            f'<td class="num exact">{_int(item.tokens)}</td>'
            f'<td class="num">{_esc(format_usd(item.usd))}</td></tr>'
            for item in series
        )
        blocks.append(
            f'<div class="window"><h3>Last {days} days</h3>'
            f'<p class="headline"><strong>{_esc(format_tokens(tokens))} tok</strong>'
            f'<span class="exact">{_int(tokens)}</span>'
            f"<strong>{_esc(format_usd(usd))}{floor}</strong></p>"
            + _stacked_bar(
                tuple(_Series(vendor_label(i.label), i.tokens, i.usd, i.unpriced_tokens) for i in series),
                value="tokens",
                label=f"tokens by vendor, last {days} days",
            )
            + _stacked_bar(
                tuple(_Series(vendor_label(i.label), i.tokens, i.usd, i.unpriced_tokens) for i in series),
                value="usd",
                label=f"notional dollars by vendor, last {days} days",
            )
            + '<table><thead><tr><th>Vendor</th><th class="num">Tokens</th>'
            '<th class="num exact">exact</th><th class="num">Notional</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    if not blocks:
        return ""
    return f'<div class="windows">{"".join(blocks)}</div>'


def _daily_section(cells: Sequence[Cell], today: DayKey) -> str:
    """Tokens per day over the long window, stacked by vendor."""
    window = day_keys_back(today, max(WINDOW_DAYS))
    scoped = _window_cells(cells, window)
    if not scoped:
        return ""
    order = [item.label for item in _by_vendor(scoped)]
    stacks: dict[DayKey, list[tuple[str, float]]] = {}
    for cell in scoped:
        stacks.setdefault(cell.day, []).append((cell.vendor, float(cell.tokens)))
    folded = {
        day: tuple(_fold(entries).items()) for day, entries in stacks.items()
    }
    chart = _columns(
        window, folded, order, label=f"tokens per day, last {len(window)} days"
    )
    if not chart:
        return ""
    legend = _legend([vendor_label(key) for key in order])
    return chart + legend


def _daily_usd_section(cells: Sequence[Cell], today: DayKey) -> str:
    """Notional dollars per day over the long window, stacked by vendor."""
    window = day_keys_back(today, max(WINDOW_DAYS))
    scoped = _window_cells(cells, window)
    if not scoped:
        return ""
    order = [item.label for item in _by_vendor(scoped)]
    stacks: dict[DayKey, list[tuple[str, float]]] = {}
    for cell in scoped:
        if cell.usd > 0:
            stacks.setdefault(cell.day, []).append((cell.vendor, float(cell.usd)))
    if not stacks:
        return ""
    folded = {
        day: tuple(_fold(entries).items()) for day, entries in stacks.items()
    }
    chart = _columns(
        window,
        folded,
        order,
        label=f"notional dollars per day, last {len(window)} days",
        fmt_value=format_usd,
    )
    if not chart:
        return ""
    legend = _legend([vendor_label(key) for key in order])
    return chart + legend


def _cache_efficiency_section(
    cells: Sequence[Cell], today: DayKey, pricing: PricingTable | None
) -> str:
    """30-day prompt-cache hit ratio, 5m-TTL reuse diagnostics, and dollar savings."""
    scoped = _window_cells(cells, day_keys_back(today, 30))
    if not scoped:
        return ""
    c_read = sum(c.usage.cache_read for c in scoped)
    c_w5 = sum(c.usage.cache_write_5m for c in scoped)
    c_w1h = sum(c.usage.cache_write_1h for c in scoped)
    c_write = c_w5 + c_w1h
    uncached_in = sum(c.usage.input for c in scoped)
    if c_read <= 0 and c_write <= 0:
        return ""
    prompt_total = uncached_in + c_write + c_read
    hit_pct = _pct_of(float(c_read), float(prompt_total))
    reuse = (float(c_read) / float(c_write)) if c_write > 0 else float("inf")
    actual_prompt_usd = 0.0
    uncached_equiv_usd = 0.0
    for cell in scoped:
        if pricing is None or cell.unpriced:
            continue
        p_only = ModelUsage(
            input=cell.usage.input,
            output=0,
            cache_read=cell.usage.cache_read,
            cache_write_5m=cell.usage.cache_write_5m,
            cache_write_1h=cell.usage.cache_write_1h,
        )
        eq_only = ModelUsage(
            input=cell.usage.input + cell.usage.cache_read + cell.usage.cache_write_5m + cell.usage.cache_write_1h,
            output=0,
        )
        actual_prompt_usd += _safe_cost(pricing, cell.key, p_only, cell.day)
        uncached_equiv_usd += _safe_cost(pricing, cell.key, eq_only, cell.day)
    saved_usd = uncached_equiv_usd - actual_prompt_usd
    reuse_str = f"{reuse:.1f}x" if reuse != float("inf") else "direct cache hit"
    verdict_cls = "note"
    if c_write > 0 and reuse < 4.0:
        verdict_cls = "note warn"
        verdict = (
            f"5m TTL cache-thrash warning: each written cache prefix is reused only {reuse:.1f}x "
            "(break-even is ~1.3x; healthy swarm reuse is 8x+). Idle gaps over 5 minutes or "
            "switching global accounts mid-session forces expensive 1.25x cache rebuilds."
        )
    else:
        verdict = (
            f"Healthy prompt-cache reuse ({reuse_str}): {hit_pct:.1f}% of all prompt context "
            "was served from warm cache reads."
        )
    savings_label = (
        f"{format_usd(saved_usd)} saved vs uncached input"
        if saved_usd >= 0
        else f"{format_usd(abs(saved_usd))} net cache-write overhead"
    )
    return (
        f'<p class="{verdict_cls}">{_esc(verdict)}</p>'
        '<table><thead><tr><th>Metric (30d)</th><th class="num">Volume / Ratio</th>'
        '<th class="num exact">exact</th></tr></thead><tbody>'
        f'<tr><td>Cache hit share of prompt tokens</td><td class="num">{hit_pct:.1f}%</td>'
        f'<td class="num exact">{_int(c_read)} / {_int(prompt_total)}</td></tr>'
        f'<tr><td>Cache reuse per write (read / write)</td><td class="num">{_esc(reuse_str)}</td>'
        f'<td class="num exact">5m writes: {_int(c_w5)} &middot; 1h writes: {_int(c_w1h)}</td></tr>'
        f'<tr><td>Net prompt-cache impact</td><td class="num">{_esc(savings_label)}</td>'
        f'<td class="num exact">uncached equiv {format_usd(uncached_equiv_usd)} vs actual {format_usd(actual_prompt_usd)}</td></tr>'
        "</tbody></table>"
    )


def estimate_monthly_seat_usd(
    rows: Sequence[AccountRow], explicit_monthly_usd: float = 0.0
) -> tuple[float, list[tuple[str, str, float]]]:
    """Estimate monthly subscription cost across tracked seats.

    Returns ``(total_monthly_usd, [(label, plan_display, seat_usd), ...])``.
    Pseudo-accounts (``slot == 0``) are skipped whenever real per-account rows
    exist for that vendor so a single active login is never counted twice.
    """
    real_vendors = {r.vendor for r in rows if r.slot != 0}
    breakdown: list[tuple[str, str, float]] = []
    for row in rows:
        if row.slot == 0 and row.vendor in real_vendors:
            continue
        raw = str(row.plan_type or "").strip().lower()
        if not raw:
            continue
        seat = 0.0
        if "20x" in raw:
            seat = 200.0
        elif "5x" in raw or raw in ("max", "claude_max"):
            seat = 100.0
        elif raw == "pro":
            seat = 200.0 if row.vendor == "codex" else 20.0
        elif "plus" in raw or "claude_pro" in raw:
            seat = 20.0
        elif any(tok in raw for tok in ("team", "business", "enterprise")):
            seat = 30.0
        if seat > 0:
            label = _safe_alias(row.alias, vendor=row.vendor, slot=row.slot) if row.alias else row.vendor_label
            breakdown.append((label, plan_label(row.plan_type), seat))
    inferred = sum(item[2] for item in breakdown)
    total = float(explicit_monthly_usd) if explicit_monthly_usd > 0 else inferred
    return total, breakdown


def _roi_section(
    cells: Sequence[Cell],
    rows: Sequence[AccountRow],
    today: DayKey,
    *,
    monthly_subscription_usd: float = 0.0,
) -> str:
    """30-day API notional value vs monthly subscription spend."""
    scoped = _window_cells(cells, day_keys_back(today, 30))
    usd_30d = sum(c.usd for c in scoped)
    if usd_30d <= 0:
        return ""
    monthly_cost, seats = estimate_monthly_seat_usd(rows, monthly_subscription_usd)
    if monthly_cost <= 0:
        return ""
    multiplier = usd_30d / monthly_cost
    net_value = usd_30d - monthly_cost
    source_note = (
        "custom monthly_subscription_usd setting"
        if monthly_subscription_usd > 0
        else f"estimated from {len(seats)} detected plan tier(s)"
    )
    seat_rows = "".join(
        f"<tr><td>{_esc(alias)}</td><td>{_esc(plan)}</td>"
        f'<td class="num">{_esc(format_usd(cost))}/mo</td></tr>'
        for alias, plan, cost in seats
    )
    seat_table = (
        '<table style="margin-top:10px"><thead><tr><th>Account</th><th>Plan tier</th>'
        f'<th class="num">Est. seat</th></tr></thead><tbody>{seat_rows}</tbody></table>'
        if seat_rows and monthly_subscription_usd <= 0
        else ""
    )
    return (
        f'<p class="headline"><strong>{multiplier:.1f}x ROI</strong>'
        f'<span>{_esc(format_usd(usd_30d))} API notional (30d) vs '
        f'{_esc(format_usd(monthly_cost))}/mo ({_esc(source_note)})</span>'
        f'<strong>{_esc(format_usd(max(0.0, net_value)))} net value</strong></p>'
        + seat_table
    )


def _fold(entries: Sequence[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, amount in entries:
        out[key] = out.get(key, 0.0) + amount
    return out


def _model_section(
    cells: Sequence[Cell], today: DayKey, pricing: PricingTable | None = None
) -> str:
    """Per-model tokens and dollars for each window, largest first."""
    blocks: list[str] = []
    for days in WINDOW_DAYS:
        scoped = _window_cells(cells, day_keys_back(today, days))
        if not scoped:
            continue
        series = _capped(_by(scoped, "model"), MAX_MODELS)
        row_parts: list[str] = []
        for item in series:
            friendly = ""
            if pricing is not None and not item.label.startswith("other ("):
                try:
                    cand = pricing.display_name(item.label)
                    if isinstance(cand, str) and cand and cand != item.label:
                        friendly = cand
                except Exception:
                    friendly = ""
            label_html = (
                f'{_esc(friendly)} <span class="exact">({_esc(item.label)})</span>'
                if friendly
                else _esc(item.label)
            )
            row_parts.append(
                f"<tr><td>{label_html}</td>"
                f'<td class="num">{_esc(format_tokens(item.tokens))}</td>'
                f'<td class="num exact">{_int(item.tokens)}</td>'
                f'<td class="num">{_esc(format_usd(item.usd))}'
                f'{"+" if item.unpriced_tokens else ""}</td></tr>'
            )
        rows = "".join(row_parts)
        blocks.append(
            f'<div class="window"><h3>Last {days} days</h3>'
            + _stacked_bar(series, value="tokens", label=f"tokens by model, last {days} days")
            + '<table><thead><tr><th>Model</th><th class="num">Tokens</th>'
            '<th class="num exact">exact</th><th class="num">Notional</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    if not blocks:
        return ""
    return f'<div class="windows">{"".join(blocks)}</div>'


def _mix_section(cells: Sequence[Cell], today: DayKey) -> str:
    """Share of each vendor's tokens per day - who took over from whom.

    Drawn per vendor, and only for a vendor running more than one model in the
    window: a single-model vendor's "mix" is a solid block, which is a chart
    that says nothing. On this machine the Codex panel is the Astra-vs-Sol
    question item 9 names; the code asks it of whatever models are actually
    there rather than hardcoding two names that will age.
    """
    window = day_keys_back(today, max(WINDOW_DAYS))
    scoped = _window_cells(cells, window)
    blocks: list[str] = []
    for vendor in VENDORS:
        mine = tuple(cell for cell in scoped if cell.vendor == vendor)
        models = _by(mine, "model")
        if len(models) < 2:
            continue
        order = [item.label for item in _capped(models, MAX_MODELS)]
        known = set(order)
        stacks: dict[DayKey, list[tuple[str, float]]] = {}
        for cell in mine:
            bucket = cell.model if cell.model in known else order[-1]
            stacks.setdefault(cell.day, []).append((bucket, float(cell.tokens)))
        folded = {day: tuple(_fold(entries).items()) for day, entries in stacks.items()}
        chart = _columns(
            window,
            folded,
            order,
            label=f"{vendor_label(vendor)} model share per day",
            normalize=True,
        )
        if not chart:
            continue
        blocks.append(
            f"<div class=\"window\"><h3>{_esc(vendor_label(vendor))}</h3>"
            + chart
            + _legend(order)
            + "</div>"
        )
    if not blocks:
        return ""
    return f'<div class="windows">{"".join(blocks)}</div>'


def _project_section(
    history: Any, today: DayKey, *, pricing: PricingTable | None = None
) -> str:
    """Per-project totals, from the mirror's project table.

    Absent - not empty, absent - when there is nothing to show: a heading over
    a blank table would read as "no projects used", which is a different claim
    from "this machine has not recorded any".

    The rows come from :func:`_history_project_rows`, which asks
    ``project_rows()`` first. That distinction is the whole of this section's
    history: it was reading ``rows()``, whose ``project`` is empty for every
    row by contract, so the table was unreachable on a machine that had the
    data.
    """
    rows = _history_project_rows(history, day_keys_back(today, max(WINDOW_DAYS))[0])
    days_7d = set(day_keys_back(today, 7))
    days_30d = set(day_keys_back(today, 30))
    tokens: dict[str, int] = {}
    usd: dict[str, float] = {}
    usd_7d: dict[str, float] = {}
    usd_30d: dict[str, float] = {}
    for row in rows:
        project = str(getattr(row, "project", "") or "")
        if not project:
            continue
        usage = getattr(row, "usage", None)
        if not isinstance(usage, ModelUsage) or usage.total_tokens <= 0:
            continue
        row_day = str(getattr(row, "day", today) or today)
        row_key = str(getattr(row, "key", "") or "")
        tokens[project] = tokens.get(project, 0) + usage.total_tokens
        cell_usd = _safe_cost(pricing, row_key, usage, row_day)
        if cell_usd <= 0.0:
            recorded = getattr(row, "usd_at_record", 0.0)
            if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
                cell_usd = max(0.0, float(recorded))
        usd[project] = usd.get(project, 0.0) + cell_usd
        if row_day in days_7d:
            usd_7d[project] = usd_7d.get(project, 0.0) + cell_usd
        if row_day in days_30d:
            usd_30d[project] = usd_30d.get(project, 0.0) + cell_usd
    if not tokens:
        return ""
    ranked = sorted(
        tokens.items(),
        key=lambda pair: (-usd.get(pair[0], 0.0), -pair[1], pair[0]),
    )[:MAX_PROJECTS]
    body = "".join(
        f"<tr><td>{_esc(name)}</td>"
        f'<td class="num">{_esc(format_tokens(count))}</td>'
        f'<td class="num exact">{_int(count)}</td>'
        f'<td class="num">{_esc(format_usd(usd_7d.get(name, 0.0)))}</td>'
        f'<td class="num">{_esc(format_usd(usd_30d.get(name, 0.0)))}</td>'
        f'<td class="num">{_esc(format_usd(usd.get(name, 0.0)))}</td></tr>'
        for name, count in ranked
    )
    return (
        '<table><thead><tr><th>Project</th><th class="num">Tokens (90d)</th>'
        '<th class="num exact">exact</th><th class="num">7d Notional</th>'
        '<th class="num">30d Notional</th><th class="num">90d Notional</th>'
        f"</tr></thead><tbody>{body}</tbody></table>"
    )


def _workflow_money(usd: float, unpriced: int) -> str:
    """Notional dollars, plus the unpriced volume behind a $0 when there is any."""
    text = format_usd(usd)
    if unpriced > 0:
        text += f" + {format_tokens(unpriced)} tok unpriced"
    return text


def _workflow_section(runs: Sequence[Any]) -> str:
    """One row per workflow run, then its phases and models (F1).

    *runs* are ``attribution.WorkflowRun`` values. Absent when there are none,
    for :func:`_project_section`'s reason. Labels and phases reach the page
    from a journal on disk, so they are escaped like every other name here.
    """
    rows: list[str] = []
    for run in list(runs)[:MAX_WORKFLOW_RUNS]:
        usage = getattr(run, "usage", None)
        if not isinstance(usage, ModelUsage) or usage.is_zero:
            continue
        days = getattr(run, "days", ()) or ()
        when = " – ".join(sorted({str(days[0]), str(days[-1])})) if days else ""
        rows.append(
            '<tr class="run">'
            f"<td><strong>{_esc(getattr(run, 'label', ''))}</strong></td>"
            f"<td>{_esc(when)}</td>"
            f'<td class="num">{_int(getattr(run, "agents", 0))}</td>'
            f'<td class="num">{_esc(format_tokens(usage.total_tokens))}</td>'
            f'<td class="num exact">{_int(usage.total_tokens)}</td>'
            f'<td class="num">{_esc(_workflow_money(getattr(run, "usd", 0.0), getattr(run, "unpriced_tokens", 0)))}</td>'
            "</tr>"
        )
        for kind, parts in (
            ("phase", getattr(run, "phases", ()) or ()),
            ("model", getattr(run, "models", ()) or ()),
            ("agent", tuple(getattr(run, "agent_rows", ()) or ())[:3]),
        ):
            for part in parts:
                tokens = getattr(part, "total_tokens", 0)
                rows.append(
                    '<tr class="part">'
                    f'<td>&nbsp;&nbsp;{_esc(kind)}: {_esc(getattr(part, "name", ""))}</td>'
                    "<td></td>"
                    f'<td class="num">{_int(getattr(part, "agents", 0))}</td>'
                    f'<td class="num">{_esc(format_tokens(tokens))}</td>'
                    f'<td class="num exact">{_int(tokens)}</td>'
                    f'<td class="num">{_esc(_workflow_money(getattr(part, "usd", 0.0), getattr(part, "unpriced_tokens", 0)))}</td>'
                    "</tr>"
                )
    if not rows:
        return ""
    return (
        '<table><thead><tr><th>Workflow run</th><th>Days</th><th class="num">Agents</th>'
        '<th class="num">Tokens</th><th class="num exact">exact</th>'
        f'<th class="num">Notional</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )


def _workflow_runs_beside(
    rollups: Any, pricing: PricingTable | None, projects_dir: Path | str | None
) -> tuple[Any, ...]:
    """The runs the attribution cache BESIDE *rollups* holds; ``()`` otherwise.

    Read-only: the cache is loaded and never saved, and a missing file is not
    created. A store with no ``path`` (a test double) reads nothing - never the
    installed widget's cache. Any failure costs the table, not the page.
    """
    where = getattr(rollups, "path", None)
    if where is None:
        return ()
    try:
        from .attribution import AttributionStore, attribution_path_for

        path = attribution_path_for(where)
        if not path.exists():
            return ()
        store = AttributionStore(path=path)
        store.load()
        return store.workflow_runs(
            pricing,
            projects_dir=PROJECTS_DIR if projects_dir is None else projects_dir,
            limit=MAX_WORKFLOW_RUNS,
        )
    except Exception:
        return ()


def _quota_section(rows: Sequence[AccountRow]) -> str:
    """Every reported window as a bar, with its reset and its capped state.

    The bars are percentages because percentages are what the sources report -
    claude-swap hands back a number per window, Codex hands back a used
    percentage and an epoch. A capped window is drawn in the accent colour and
    labelled ``capped``, which is the one distinction that changes what the
    operator does next.
    """
    blocks: list[str] = []
    for row in rows:
        windows = _row_windows(row)
        if not windows:
            continue
        bars = "".join(
            _window_bar(name, pct, reset, expired)
            for name, pct, reset, expired in windows
        )
        note = ""
        if row.attention_note:
            kind = row.attention_kind or "info"
            note = f'<p class="note {_esc(kind)}">{_esc(row.attention_note)}</p>'
        extra = "".join(f'<li>{_esc(line)}</li>' for line in row.info_notes)
        extra = f'<ul class="info">{extra}</ul>' if extra else ""
        title = _safe_alias(row.alias, vendor=row.vendor, slot=row.slot) if row.alias else row.vendor_label
        # The display name (`Max 20x`, `Business`), not the raw slug: Claude
        # rows carry `claude_max_20x` since 2026-09-30.
        plan = f' <span class="plan">{_esc(plan_label(row.plan_type))}</span>' if row.plan_type else ""
        blocks.append(
            f'<div class="account"><h3>{_esc(title)}'
            f'<span class="vendor">{_esc(row.vendor_label)}</span>{plan}</h3>'
            f"{note}{bars}{extra}</div>"
        )
    if not blocks:
        return ""
    return f'<div class="accounts">{"".join(blocks)}</div>'


def _row_windows(row: AccountRow) -> tuple[tuple[str, float, str, bool], ...]:
    """``(name, pct, reset text, expired)`` for every window the row reports.

    A window the source did not report is omitted rather than drawn at zero -
    Codex has no 5-hour window and ``seven_day_opus`` is null on these
    accounts, and a 0 % bar would claim both were measured and unused.
    """
    out: list[tuple[str, float, str, bool]] = []
    if row.five_hour_pct is not None:
        out.append(
            ("5h", float(row.five_hour_pct), row.five_hour_resets_at or "", "five_hour" in row.expired_windows)
        )
    if row.seven_day_pct is not None:
        out.append(
            ("weekly", float(row.seven_day_pct), row.seven_day_resets_at or "", "seven_day" in row.expired_windows)
        )
    scoped_resets = dict(row.scoped_resets_at)
    for name, pct in row.scoped_windows:
        if pct is None:
            continue
        out.append((name, float(pct), scoped_resets.get(name, ""), name in row.expired_windows))
    return tuple(out)


def _window_bar(name: str, pct: float, reset: str, expired: bool) -> str:
    capped = pct >= 100.0
    width = max(0.0, min(100.0, pct))
    css = "fill-capped" if capped else "fill-ok"
    state = " capped" if capped else ""
    if expired:
        css = "fill-expired"
        state = " overdue"
    reset_text = f" · resets {reset}" if reset else ""
    return (
        f'<div class="window-bar"><span class="w-name">{_esc(name)}</span>'
        f'<svg class="meter" viewBox="0 0 100 10" preserveAspectRatio="none" role="img" '
        f'aria-label="{_esc(name)} {pct:.0f} percent">'
        f'<rect class="track" x="0" y="0" width="100" height="10"></rect>'
        f'<rect class="{css}" x="0" y="0" width="{_round(width)}" height="10"></rect>'
        f'<line class="limit" x1="100" y1="0" x2="100" y2="10"></line></svg>'
        f'<span class="w-pct">{pct:.0f}%{state}</span>'
        f'<span class="w-reset">{_esc(reset_text)}</span></div>'
    )


def _wall_section(
    rows: Sequence[AccountRow], now: float, *, history: Any = None
) -> tuple[str, str]:
    """What each account's wall is right now - and measured 7d wall time when recorded."""
    if not rows:
        return "", ""
    measured: dict[tuple[str, str], float] = {}
    reader = getattr(history, "wall_hours_by_account", None) if history is not None else None
    if callable(reader):
        try:
            measured = dict(reader(since_epoch=now - 7 * 86_400.0) or {})
        except Exception:
            measured = {}
    has_measured = bool(measured)
    body: list[str] = []
    for row in rows:
        highest = row.max_pct
        if highest is None:
            state = "no reading"
        elif highest >= 100.0:
            remaining = (
                coarse_duration(row.soonest_reset_at - now)
                if row.soonest_reset_at is not None and row.soonest_reset_at > now
                else ""
            )
            when = fleet_reset_label(row.soonest_reset_at, now)
            tail = f" · reset {when} (in {remaining})" if when and remaining else ""
            state = f"at the wall now{tail}"
        else:
            state = f"{highest:.0f}% of its highest window"
        alias_key = (row.vendor, row.alias or row.vendor_label)
        hrs_col = ""
        if has_measured:
            hrs = measured.get(alias_key, 0.0)
            hrs_col = f'<td class="num">{hrs:.2f} h</td>'
        display_alias = _safe_alias(row.alias, vendor=row.vendor, slot=row.slot) if row.alias else row.vendor_label
        body.append(
            f"<tr><td>{_esc(display_alias)}</td>"
            f"<td>{_esc(row.vendor_label)}</td>"
            f"<td>{_esc(state)}</td>{hrs_col}</tr>"
        )
    hrs_head = '<th class="num">7d wall time</th>' if has_measured else ""
    note = (
        "7-day wall time measured from 15-minute quota samples recorded in history.sqlite."
        if has_measured
        else WALL_NO_HISTORY_NOTE
    )
    return (
        f"<table><thead><tr><th>Account</th><th>Vendor</th><th>Wall</th>{hrs_head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>",
        note,
    )


def _unpriced_section(cells: Sequence[Cell], today: DayKey) -> str:
    """Volume nobody publishes a rate for: tokens and names, never a dollar."""
    scoped = _window_cells(cells, day_keys_back(today, max(WINDOW_DAYS)))
    unpriced = tuple(cell for cell in scoped if cell.unpriced)
    if not unpriced:
        return ""
    per_vendor: dict[Vendor, dict[str, int]] = {}
    for cell in unpriced:
        per_vendor.setdefault(cell.vendor, {})
        bucket = per_vendor[cell.vendor]
        bucket[cell.model] = bucket.get(cell.model, 0) + cell.tokens
    rows = []
    for vendor in VENDORS:
        models = per_vendor.get(vendor)
        if not models:
            continue
        for model, count in sorted(models.items(), key=lambda pair: (-pair[1], pair[0])):
            rows.append(
                f"<tr><td>{_esc(vendor_label(vendor))}</td><td>{_esc(model)}</td>"
                f'<td class="num">{_esc(format_tokens(count))}</td>'
                f'<td class="num exact">{_int(count)}</td></tr>'
            )
    if not rows:
        return ""
    return (
        f'<p class="note">{_esc(_UNPRICED_LABEL)}: these tokens are counted and named, '
        "never folded into a dollar total.</p>"
        '<table><thead><tr><th>Vendor</th><th>Model</th><th class="num">Tokens</th>'
        f'<th class="num exact">exact</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
    )


def _belkins_dex_section(
    cells: Sequence[Cell],
    accounts: Sequence[AccountRow],
    quota_rows: Sequence[AccountRow],
    today: DayKey,
) -> str:
    """Belkins Companion Hero, 5-stage Evolution Dex, and 8-badge Trophy Room."""
    if not cells and not accounts and not quota_rows:
        return ""
    from .belkins_game import EVOLUTION_LINE, compute_companion, render_sprite_svg

    cells_30d = _window_cells(cells, day_keys_back(today, 30))
    cells_today = tuple(c for c in cells_30d if c.day == today)
    tok_30d = sum(c.tokens for c in cells_30d)
    usd_30d = sum(c.usd for c in cells_30d)
    tok_today = sum(c.tokens for c in cells_today)
    usd_today = sum(c.usd for c in cells_today)
    models_count = len({(c.vendor, c.model) for c in cells_30d})

    companion = compute_companion(
        tokens_today=tok_today,
        tokens_30d=tok_30d,
        usd_today=usd_today,
        usd_30d=usd_30d,
        accounts=accounts,
        quota_rows=quota_rows,
        distinct_models_count=models_count,
    )

    hero_svg = render_sprite_svg(companion.stage, size=80, equipped=companion.equipped_items)
    xp_pct = int(round(companion.xp_fraction * 100))
    next_text = (
        f"Next evolution: {companion.next_stage.name} at Lv.{companion.next_stage.min_level}"
        if companion.next_stage is not None
        else "MAX EVOLUTION UNLOCKED — Mythic Sovereign"
    )
    party_bits: list[str] = []
    if companion.claude_total > 0:
        party_bits.append(f"Claude Squad {companion.claude_ready}/{companion.claude_total} ready")
    if companion.codex_total > 0:
        party_bits.append(f"Codex Squad {companion.codex_ready}/{companion.codex_total} ready")
    party_bits.append(f"Trophies {companion.unlocked_badges_count}/{len(companion.badges)}")
    party_bits.append(f"✦ {companion.coins_available:,} CR")

    hero_html = (
        '<div class="bk-hero">'
        f'<div class="bk-sprite-pad">{hero_svg}</div>'
        '<div class="bk-hero-meta">'
        '<div class="bk-hero-top">'
        f'<strong>{_esc(companion.stage.name)}</strong>'
        f'<span class="bk-lv">Lv.{companion.level} · {_esc(companion.stage.title)} ({_esc(companion.stage.passive)})</span>'
        f'<span class="bk-mood">{_esc(companion.mood_label)}</span>'
        "</div>"
        f'<p class="note">{_esc(companion.stage.lore)}</p>'
        '<div class="bk-xp-row">'
        f'<span>{companion.xp_total:,} XP total (+{companion.xp_today:,} XP today)</span>'
        f'<span>{xp_pct}% to Lv.{companion.level + 1} · {_esc(next_text)}</span>'
        "</div>"
        '<div class="bk-xp-track">'
        f'<div class="bk-xp-fill" style="width:{xp_pct}%"></div>'
        "</div>"
        f'<p class="sub" style="margin:6px 0 0">{" · ".join(_esc(b) for b in party_bits)} · CLI: <code>bswap</code> / <code>belkins</code></p>'
        "</div></div>"
    )

    boss = companion.boss
    boss_svg = render_sprite_svg(boss.spec, size=68).replace("HTTP-", "ERR-").replace("http-", "err-")
    boss_name = boss.spec.name.replace("HTTP-", "ERR-").replace("http-", "err-")
    boss_hp_pct = max(0, min(100, int(round(boss.hp_pct))))
    boss_html = (
        '<div class="bk-boss">'
        f'<div class="bk-sprite-pad bk-boss-pad">{boss_svg}</div>'
        '<div class="bk-hero-meta">'
        '<div class="bk-hero-top">'
        f'<strong>BOSS RAID ARENA · {_esc(boss_name)}</strong>'
        f'<span class="bk-lv">Lv.{boss.level} · {_esc(boss.spec.title)}</span>'
        f'<span class="bk-mood">{boss.bosses_slain} Slain · {boss.player_atk} ATK ({boss.player_crit_pct}% Crit)</span>'
        "</div>"
        '<div class="bk-xp-row">'
        f'<span>HP {boss.hp_current:,} / {boss.hp_max:,} ({boss_hp_pct}%)</span>'
        f'<span>Bounty: +{boss.spec.coin_reward} ✦ CR &amp; +{boss.spec.xp_reward:,} XP · Strike via <code>bswap raid</code></span>'
        "</div>"
        '<div class="bk-xp-track">'
        f'<div class="bk-xp-fill" style="width:{boss_hp_pct}%"></div>'
        "</div>"
        "</div></div>"
    )

    evo_cards: list[str] = []
    for st in EVOLUTION_LINE:
        unlocked = companion.level >= st.min_level
        is_current = st.stage == companion.stage.stage
        cls = "bk-stage active" if is_current else ("bk-stage unlocked" if unlocked else "bk-stage locked")
        tag = "ACTIVE" if is_current else ("UNLOCKED" if unlocked else f"Lv.{st.min_level}")
        svg = render_sprite_svg(st, size=44)
        evo_cards.append(
            f'<div class="{cls}">'
            f'<div class="bk-stage-head">{svg}<div>'
            f'<h4>#{st.stage} {_esc(st.name)}</h4>'
            f'<span class="bk-tag">{_esc(tag)} · Lv.{st.min_level}+ · {_esc(st.passive)}</span>'
            "</div></div>"
            f'<p class="note">{_esc(st.lore)}</p>'
            "</div>"
        )

    badge_cards: list[str] = []
    for b in companion.badges:
        cls = "bk-badge unlocked" if b.unlocked else "bk-badge locked"
        status = "UNLOCKED" if b.unlocked else b.progress_text
        badge_cards.append(
            f'<div class="{cls}">'
            f'<div class="bk-badge-head"><span class="bk-badge-icon">{_esc(b.icon)}</span>'
            f'<div><h4>{_esc(b.name)}</h4><span class="bk-tag">{_esc(status)}</span></div></div>'
            f'<p class="note">{_esc(b.description)}</p>'
            "</div>"
        )

    shop_cards: list[str] = []
    for it in companion.shop_items:
        cls = "bk-badge unlocked" if it.owned else "bk-badge locked"
        status = "EQUIPPED ✓" if it.equipped else ("OWNED" if it.owned else f"✦ {it.price:,} CR")
        shop_cards.append(
            f'<div class="{cls}">'
            f'<div class="bk-badge-head"><span class="bk-badge-icon">{_esc(it.icon)}</span>'
            f'<div><h4>{_esc(it.name)}</h4><span class="bk-tag">{_esc(status)}</span></div></div>'
            f'<p class="note">{_esc(it.description)}</p>'
            "</div>"
        )

    return (
        hero_html
        + boss_html
        + "<h3>8-Stage Cyber-Form Evolution Lineage</h3>"
        + f'<div class="bk-evo-grid">{"".join(evo_cards)}</div>'
        + f'<h3 style="margin-top:14px">Belkins Hall of Fame ({companion.unlocked_badges_count}/{len(companion.badges)} Trophies)</h3>'
        + f'<div class="bk-badge-grid">{"".join(badge_cards)}</div>'
        + f'<h3 style="margin-top:14px">Belkins Spark Vault (✦ {companion.coins_available:,} CR available — buy/equip in menu bar or via <code>bswap vault</code>)</h3>'
        + f'<div class="bk-badge-grid">{"".join(shop_cards)}</div>'
    )


# ---------------------------------------------------------------------------
# Fleet Reset Timeline & Capacity Forecast (consumes cc_usage_widget.resets)
# ---------------------------------------------------------------------------

_RR_HORIZON_SECONDS: Final[float] = 7.0 * 86_400.0
_RR_WEEKDAYS: Final[dict[str, int]] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
_RR_MONTHS: Final[dict[str, int]] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_RR_HHMM_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{1,2}):(\d{2})$")
_RR_WD_HHMM_RE: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z]{3})\s+(\d{1,2}):(\d{2})$"
)
_RR_MON_DAY_HHMM_RE: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})$"
)


@dataclass(frozen=True, slots=True)
class _FallbackResetEvent:
    vendor: str
    slot: int
    alias: str
    window: str
    used_pct: float | None
    free_pct: float | None
    resets_at_epoch: float
    seconds_until: float
    reset_label: str
    countdown_label: str
    kind: str
    is_active: bool = False
    disabled: bool = False
    plan_label: str = ""
    no_fable: bool = False


@dataclass(frozen=True, slots=True)
class _FallbackResetCreditAccount:
    vendor: str
    slot: int
    alias: str
    available: int
    usable: int
    used_pct: float | None
    is_active: bool = False
    needs_relogin: bool = False


@dataclass(frozen=True, slots=True)
class _FallbackResetCreditsSummary:
    total_available: int
    live_available: int
    total_usable: int
    accounts: tuple[_FallbackResetCreditAccount, ...]


@dataclass(frozen=True, slots=True)
class _FallbackFleetCapacitySummary:
    claude_fable_free_seats: float
    claude_fable_total_seats: int
    claude_locked_relogin_seats: int
    claude_next_unlock: _FallbackResetEvent | None
    codex_free_seats: float
    codex_total_seats: int
    codex_locked_relogin_seats: int
    codex_next_unlock: _FallbackResetEvent | None
    perishable_seats_48h: float


@dataclass(frozen=True, slots=True)
class _FallbackFleetRecommendation:
    kind: str
    headline: str
    detail: str
    target_vendor: str | None = None
    target_slot: int | None = None


@dataclass(frozen=True, slots=True)
class _FallbackFleetResetRadar:
    generated_at: float
    events: tuple[_FallbackResetEvent, ...]
    unlocks: tuple[_FallbackResetEvent, ...]
    perishable: tuple[_FallbackResetEvent, ...]
    credits: _FallbackResetCreditsSummary
    capacity: _FallbackFleetCapacitySummary
    recommendation: _FallbackFleetRecommendation | None


def _fallback_parse_reset_epoch(value: Any, *, now: float) -> float | None:
    """Best-effort reset timestamp parser used only when ``resets`` is absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        val = float(value)
        return val if math.isfinite(val) and val > 1_000_000_000.0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        num = float(text)
        if math.isfinite(num) and num > 1_000_000_000.0:
            return num
    except ValueError:
        pass
    iso_cand = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed_iso = dt.datetime.fromisoformat(iso_cand)
        return parsed_iso.timestamp()
    except ValueError:
        pass
    now_dt = dt.datetime.fromtimestamp(now)
    m = _RR_HHMM_RE.match(text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            cand = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand.timestamp() <= now - 30.0:
                cand += dt.timedelta(days=1)
            return cand.timestamp()
        return None
    m = _RR_WD_HHMM_RE.match(text)
    if m:
        wd = _RR_WEEKDAYS.get(m.group(1).lower())
        hh, mm = int(m.group(2)), int(m.group(3))
        if wd is not None and 0 <= hh <= 23 and 0 <= mm <= 59:
            delta = (wd - now_dt.weekday()) % 7
            cand = (now_dt + dt.timedelta(days=delta)).replace(
                hour=hh, minute=mm, second=0, microsecond=0
            )
            if cand.timestamp() <= now - 30.0:
                cand += dt.timedelta(days=7)
            return cand.timestamp()
        return None
    m = _RR_MON_DAY_HHMM_RE.match(text)
    if m:
        month = _RR_MONTHS.get(m.group(1).lower())
        day = int(m.group(2))
        hh, mm = int(m.group(3)), int(m.group(4))
        if month is not None and 1 <= day <= 31 and 0 <= hh <= 23 and 0 <= mm <= 59:
            for year in (now_dt.year, now_dt.year + 1, now_dt.year - 1):
                try:
                    cand = dt.datetime(year, month, day, hh, mm, 0)
                except ValueError:
                    continue
                ts = cand.timestamp()
                if now - 180.0 * 86_400.0 <= ts <= now + 185.0 * 86_400.0:
                    return ts
    return None


def _fallback_format_countdown(seconds: float) -> str:
    if seconds <= 0.0:
        return "now"
    if seconds < 60.0:
        return "<1m"
    total_min = int(seconds // 60)
    if total_min < 60:
        return f"{total_min}m"
    hours, mins = divmod(total_min, 60)
    if hours < 24:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    days, rem_h = divmod(hours, 24)
    return f"{days}d {rem_h}h" if rem_h else f"{days}d"


def _fallback_format_local_reset_label(epoch: float, *, now: float) -> str:
    now_dt = dt.datetime.fromtimestamp(now)
    target = dt.datetime.fromtimestamp(epoch)
    clock = target.strftime("%H:%M")
    diff_days = (target.date() - now_dt.date()).days
    if diff_days == 0:
        return clock
    if diff_days == 1:
        return f"Tom {clock}"
    if 2 <= diff_days <= 6:
        return f"{target.strftime('%a')} {clock}"
    return f"{target.strftime('%b')} {target.day} {clock}"


def _fallback_row_fable_pct(row: Any) -> float | None:
    for name, pct in getattr(row, "scoped_windows", ()) or ():
        if pct is not None and str(name).strip().lower().startswith("fable"):
            try:
                val = float(pct)
            except (TypeError, ValueError):
                continue
            if math.isfinite(val):
                return val
    return None


def _fallback_row_lacks_fable(row: Any) -> bool:
    if _fallback_row_fable_pct(row) is not None:
        return False
    plan = str(getattr(row, "plan_type", None) or "").strip().lower()
    if not plan:
        return False
    if not plan.startswith("claude_max"):
        return True
    return (
        getattr(row, "five_hour_pct", None) is not None
        or getattr(row, "seven_day_pct", None) is not None
    )


def _fallback_is_locked(row: Any, notes: Mapping[int, str]) -> bool:
    vendor = str(getattr(row, "vendor", "claude") or "claude")
    slot = int(getattr(row, "slot", 0) or 0)
    kind = str(getattr(row, "attention_kind", "") or "").lower()
    note = str(getattr(row, "attention_note", "") or "").lower()
    if vendor == "claude" and slot in notes:
        return True
    if kind == "warn":
        return True
    return any(
        tok in note
        for tok in ("relogin", "re-login", "expired", "401", "403", "quarantine", "no access")
    )


def _fallback_fleet_reset_radar(
    accounts: Sequence[AccountRow],
    quota_rows: Sequence[AccountRow],
    *,
    account_notes: Mapping[int, str] | None = None,
    now: float,
    fable_guard: bool = True,
) -> _FallbackFleetResetRadar:
    """Contract-compatible fallback used only when ``cc_usage_widget.resets`` is absent."""
    notes = dict(account_notes or {})
    claude_rows = [r for r in accounts if getattr(r, "vendor", "claude") == "claude"]
    raw_codex = [
        r for r in list(accounts) + list(quota_rows) if getattr(r, "vendor", "") == "codex"
    ]
    has_live_codex = any(int(getattr(r, "slot", 0) or 0) < 0 for r in raw_codex)
    codex_rows = [
        r for r in raw_codex if not (has_live_codex and int(getattr(r, "slot", 0) or 0) == 0)
    ]

    fleet_has_fable = any(
        not getattr(r, "disabled", False)
        and not _fallback_is_locked(r, notes)
        and _fallback_row_fable_pct(r) is not None
        for r in claude_rows
    )

    events: list[_FallbackResetEvent] = []
    credit_accounts: list[_FallbackResetCreditAccount] = []
    claude_free = 0.0
    claude_total = 0
    claude_locked = 0
    codex_free = 0.0
    codex_total = 0
    codex_locked = 0

    def _collect_row_events(
        row: Any,
        *,
        vendor: str,
        no_fable: bool,
        disabled: bool,
    ) -> None:
        slot = int(getattr(row, "slot", 0) or 0)
        alias = _safe_alias(getattr(row, "alias", ""), vendor=vendor, slot=slot)
        expired_set = {str(w) for w in (getattr(row, "expired_windows", ()) or ())}
        candidates: list[tuple[str, float | None, float]] = []

        fh_pct = getattr(row, "five_hour_pct", None)
        fh_ep = _fallback_parse_reset_epoch(getattr(row, "five_hour_resets_at", None), now=now)
        if fh_ep is not None and "five_hour" not in expired_set and fh_ep > now - 30.0:
            candidates.append(("5h", float(fh_pct) if fh_pct is not None else None, fh_ep))

        sd_pct = getattr(row, "seven_day_pct", None)
        sd_ep = _fallback_parse_reset_epoch(getattr(row, "seven_day_resets_at", None), now=now)
        if sd_ep is None and vendor == "codex" and fh_pct is None:
            sd_ep = _fallback_parse_reset_epoch(getattr(row, "soonest_reset_at", None), now=now)
        if sd_ep is not None and "seven_day" not in expired_set and sd_ep > now - 30.0:
            candidates.append(("7d", float(sd_pct) if sd_pct is not None else None, sd_ep))

        scoped_resets = dict(getattr(row, "scoped_resets_at", ()) or ())
        scoped_pcts = dict(getattr(row, "scoped_windows", ()) or ())
        for w_name in sorted(set(scoped_resets) | set(scoped_pcts)):
            if w_name in expired_set:
                continue
            w_ep = _fallback_parse_reset_epoch(scoped_resets.get(w_name), now=now)
            if w_ep is None or w_ep <= now - 30.0:
                continue
            raw_pct = scoped_pcts.get(w_name)
            candidates.append((str(w_name), float(raw_pct) if raw_pct is not None else None, w_ep))

        # Deduplicate identical-minute resets on the same account (prefer Fable / higher pct).
        by_minute: dict[int, tuple[str, float | None, float]] = {}
        for win, pct, ep in candidates:
            bucket = int(ep // 60)
            prev = by_minute.get(bucket)
            if prev is None:
                by_minute[bucket] = (win, pct, ep)
                continue
            p_win, p_pct, _p_ep = prev
            score_new = (1 if win.lower().startswith("fable") else 0, pct if pct is not None else -1.0)
            score_old = (1 if p_win.lower().startswith("fable") else 0, p_pct if p_pct is not None else -1.0)
            if score_new > score_old:
                by_minute[bucket] = (win, pct, ep)

        p_label = plan_label(getattr(row, "plan_type", None)) if getattr(row, "plan_type", None) else ""
        is_active = bool(getattr(row, "is_active", False))
        for win, pct, ep in by_minute.values():
            secs = max(0.0, ep - now)
            used = max(0.0, min(100.0, pct)) if pct is not None else None
            free = (100.0 - used) if used is not None else None
            if disabled or no_fable:
                ev_kind = "upcoming"
            elif used is not None and used >= 85.0:
                ev_kind = "unlock"
            elif free is not None and free >= 25.0 and secs <= 48.0 * 3600.0 and win != "5h":
                ev_kind = "perishable"
            else:
                ev_kind = "upcoming"
            events.append(
                _FallbackResetEvent(
                    vendor=vendor,
                    slot=slot,
                    alias=alias,
                    window=win,
                    used_pct=used,
                    free_pct=free,
                    resets_at_epoch=ep,
                    seconds_until=secs,
                    reset_label=_fallback_format_local_reset_label(ep, now=now),
                    countdown_label=_fallback_format_countdown(secs),
                    kind=ev_kind,
                    is_active=is_active,
                    disabled=disabled,
                    plan_label=p_label,
                    no_fable=no_fable,
                )
            )

    for row in claude_rows:
        if _fallback_is_locked(row, notes):
            claude_locked += 1
            continue
        disabled = bool(getattr(row, "disabled", False))
        no_fable = bool(fable_guard and fleet_has_fable and _fallback_row_lacks_fable(row))
        if not disabled and not no_fable:
            fable_pct = _fallback_row_fable_pct(row)
            eff_pct = (
                fable_pct
                if fable_pct is not None
                else getattr(row, "seven_day_pct", None)
            )
            if eff_pct is not None:
                claude_total += 1
                claude_free += max(0.0, min(1.0, (100.0 - float(eff_pct)) / 100.0))
        _collect_row_events(row, vendor="claude", no_fable=no_fable, disabled=disabled)

    for row in codex_rows:
        slot = int(getattr(row, "slot", 0) or 0)
        alias = _safe_alias(getattr(row, "alias", ""), vendor="codex", slot=slot)
        locked = _fallback_is_locked(row, notes)
        disabled = bool(getattr(row, "disabled", False))
        avail = int(getattr(row, "reset_credits_available", None) or 0)
        usable = int(getattr(row, "reset_credits_usable", None) or 0)
        if avail > 0 or usable > 0:
            credit_accounts.append(
                _FallbackResetCreditAccount(
                    vendor="codex",
                    slot=slot,
                    alias=alias,
                    available=avail,
                    usable=0 if locked else usable,
                    used_pct=float(row.seven_day_pct) if row.seven_day_pct is not None else None,
                    is_active=bool(getattr(row, "is_active", False)),
                    needs_relogin=locked,
                )
            )
        if locked:
            codex_locked += 1
            continue
        if not disabled and getattr(row, "seven_day_pct", None) is not None:
            codex_total += 1
            codex_free += max(0.0, min(1.0, (100.0 - float(row.seven_day_pct)) / 100.0))
        _collect_row_events(row, vendor="codex", no_fable=False, disabled=disabled)

    events.sort(key=lambda e: (e.resets_at_epoch, e.vendor, e.slot, e.window))
    ev_tuple = tuple(events)
    unlocks = tuple(e for e in ev_tuple if e.kind == "unlock")
    perishable = tuple(e for e in ev_tuple if e.kind == "perishable")

    claude_next = next(
        (e for e in unlocks if e.vendor == "claude" and not e.no_fable and not e.disabled),
        None,
    )
    codex_next = next(
        (e for e in unlocks if e.vendor == "codex" and not e.disabled),
        None,
    )

    perish_by_acct: dict[tuple[str, int], float] = {}
    for ev in perishable:
        if ev.free_pct is not None:
            key = (ev.vendor, ev.slot)
            perish_by_acct[key] = max(perish_by_acct.get(key, 0.0), ev.free_pct / 100.0)
    perishable_seats = sum(perish_by_acct.values())

    credit_accounts.sort(key=lambda c: (-c.usable, -c.available, c.alias, c.slot))
    credits_summary = _FallbackResetCreditsSummary(
        total_available=sum(c.available for c in credit_accounts),
        live_available=sum(c.available for c in credit_accounts if not c.needs_relogin),
        total_usable=sum(c.usable for c in credit_accounts if not c.needs_relogin),
        accounts=tuple(credit_accounts),
    )

    capacity = _FallbackFleetCapacitySummary(
        claude_fable_free_seats=round(claude_free, 4),
        claude_fable_total_seats=claude_total,
        claude_locked_relogin_seats=claude_locked,
        claude_next_unlock=claude_next,
        codex_free_seats=round(codex_free, 4),
        codex_total_seats=codex_total,
        codex_locked_relogin_seats=codex_locked,
        codex_next_unlock=codex_next,
        perishable_seats_48h=round(perishable_seats, 4),
    )

    # Smart Fleet Advisor (5-tier priority matching resets.py contract).
    rec: _FallbackFleetRecommendation | None = None
    usable_hot = [
        c
        for c in credit_accounts
        if not c.needs_relogin
        and c.usable > 0
        and (c.is_active or (c.used_pct is not None and c.used_pct >= 85.0))
    ]
    urgent_perish = [
        e
        for e in perishable
        if e.free_pct is not None and e.free_pct >= 40.0 and e.seconds_until <= 36.0 * 3600.0
    ]
    soon_unlock = [
        e
        for e in unlocks
        if e.seconds_until <= 6.0 * 3600.0
        and (
            (e.vendor == "claude" and claude_free <= 1.5)
            or (e.vendor == "codex" and codex_free <= 1.5)
        )
    ]
    if usable_hot:
        pick = usable_hot[0]
        rec = _FallbackFleetRecommendation(
            kind="use_reset_credit",
            headline=f"Redeem reset credit on {pick.alias} ({pick.usable}× usable)",
            detail=f"{pick.alias} holds {pick.available}× banked credit(s) with {pick.usable}× usable now.",
            target_vendor=pick.vendor,
            target_slot=pick.slot,
        )
    elif urgent_perish:
        pick = urgent_perish[0]
        rec = _FallbackFleetRecommendation(
            kind="burn_perishable",
            headline=f"Burn {pick.alias} ({pick.free_pct:.0f}% free, expires in {pick.countdown_label})",
            detail=f"{pick.window} quota on {pick.alias} resets at {pick.reset_label} — use before it expires.",
            target_vendor=pick.vendor,
            target_slot=pick.slot,
        )
    elif soon_unlock:
        pick = soon_unlock[0]
        rec = _FallbackFleetRecommendation(
            kind="unlock_soon",
            headline=f"{pick.alias} ({pick.window}) unlocks in {pick.countdown_label}",
            detail=f"Fresh capacity arrives at {pick.reset_label} while fleet headroom is tight.",
            target_vendor=pick.vendor,
            target_slot=pick.slot,
        )
    elif claude_locked + codex_locked >= 2:
        total_locked = claude_locked + codex_locked
        rec = _FallbackFleetRecommendation(
            kind="relogin_capacity",
            headline=f"Re-login {total_locked} locked seat(s) to restore fleet capacity",
            detail=f"{claude_locked} Claude and {codex_locked} Codex account(s) need fresh auth.",
        )
    elif claude_total > 0 or codex_total > 0 or ev_tuple:
        next_hint = ""
        if claude_next is not None:
            next_hint = f" · next Claude unlock {claude_next.alias} in {claude_next.countdown_label}"
        elif codex_next is not None:
            next_hint = f" · next Codex unlock {codex_next.alias} in {codex_next.countdown_label}"
        rec = _FallbackFleetRecommendation(
            kind="healthy",
            headline=f"Fleet healthy: {claude_free:.2f}/{claude_total} Fable · {codex_free:.2f}/{codex_total} Codex seats",
            detail=f"Ready capacity across active Claude and Codex pools{next_hint}.",
        )

    return _FallbackFleetResetRadar(
        generated_at=now,
        events=ev_tuple,
        unlocks=unlocks,
        perishable=perishable,
        credits=credits_summary,
        capacity=capacity,
        recommendation=rec,
    )


def _build_reset_radar(
    accounts: Sequence[AccountRow],
    quota_rows: Sequence[AccountRow],
    *,
    now: float,
    account_notes: Mapping[int, str] | None = None,
    fable_guard: bool = True,
) -> Any:
    """Lazily invoke ``cc_usage_widget.resets.build_fleet_reset_radar`` with clean fallback."""
    if not accounts and not quota_rows:
        return None
    try:
        from . import resets as resets_mod
    except Exception:
        resets_mod = None
    if resets_mod is not None:
        builder = getattr(resets_mod, "build_fleet_reset_radar", None)
        if callable(builder):
            try:
                return builder(
                    accounts,
                    quota_rows,
                    account_notes=account_notes,
                    now=now,
                    fable_guard=fable_guard,
                )
            except Exception:
                return None
    return _fallback_fleet_reset_radar(
        accounts,
        quota_rows,
        account_notes=account_notes,
        now=now,
        fable_guard=fable_guard,
    )


def _vendor_glyph(vendor: str) -> str:
    return "✦" if vendor == "claude" else ("◈" if vendor == "codex" else "•")


def _status_bucket(kind: str) -> str:
    low = str(kind or "").strip().lower()
    if low == "unlock":
        return "unlock"
    if low == "perishable":
        return "perish"
    return "upcoming"


def _credit_lookup(radar: Any) -> dict[tuple[str, int], Any]:
    credits = getattr(radar, "credits", None)
    accounts = getattr(credits, "accounts", ()) if credits is not None else ()
    out: dict[tuple[str, int], Any] = {}
    for acct in accounts or ():
        key = (str(getattr(acct, "vendor", "")), int(getattr(acct, "slot", 0) or 0))
        out[key] = acct
    return out


def _reset_kpi_strip(radar: Any) -> str:
    """A. Fleet Capacity & Banked Resets KPI Strip (4 responsive cards)."""
    cap = getattr(radar, "capacity", None)
    credits = getattr(radar, "credits", None)
    perish_events = tuple(getattr(radar, "perishable", ()) or ())
    if cap is None:
        return ""

    c_free = float(getattr(cap, "claude_fable_free_seats", 0.0) or 0.0)
    c_total = int(getattr(cap, "claude_fable_total_seats", 0) or 0)
    c_next = getattr(cap, "claude_next_unlock", None)
    c_pct = max(0.0, min(100.0, (c_free / c_total * 100.0) if c_total > 0 else 0.0))
    if c_next is not None:
        c_alias = _safe_alias(
            getattr(c_next, "alias", ""),
            vendor=str(getattr(c_next, "vendor", "claude")),
            slot=int(getattr(c_next, "slot", 0) or 0),
        )
        c_cd = _sanitize_text(getattr(c_next, "countdown_label", ""))
        c_lbl = _sanitize_text(getattr(c_next, "reset_label", ""))
        c_sub = (
            f'Next unlock: <strong>{_esc(c_alias)} in {_esc(c_cd)}</strong>'
            + (f' <span class="exact">({_esc(c_lbl)})</span>' if c_lbl else "")
        )
    elif c_total > 0:
        c_sub = "All Fable seats clear &middot; no active cap"
    else:
        c_sub = "No Fable seats tracked"

    x_free = float(getattr(cap, "codex_free_seats", 0.0) or 0.0)
    x_total = int(getattr(cap, "codex_total_seats", 0) or 0)
    x_next = getattr(cap, "codex_next_unlock", None)
    x_pct = max(0.0, min(100.0, (x_free / x_total * 100.0) if x_total > 0 else 0.0))
    if x_next is not None:
        x_alias = _safe_alias(
            getattr(x_next, "alias", ""),
            vendor=str(getattr(x_next, "vendor", "codex")),
            slot=int(getattr(x_next, "slot", 0) or 0),
        )
        x_cd = _sanitize_text(getattr(x_next, "countdown_label", ""))
        x_lbl = _sanitize_text(getattr(x_next, "reset_label", ""))
        x_sub = (
            f'Next unlock: <strong>{_esc(x_alias)} in {_esc(x_cd)}</strong>'
            + (f' <span class="exact">({_esc(x_lbl)})</span>' if x_lbl else "")
        )
    elif x_total > 0:
        x_sub = "All live Codex seats clear"
    else:
        x_sub = "No live Codex seats tracked"

    live_cr = int(getattr(credits, "live_available", 0) or 0) if credits is not None else 0
    total_cr = int(getattr(credits, "total_available", 0) or 0) if credits is not None else 0
    usable_cr = int(getattr(credits, "total_usable", 0) or 0) if credits is not None else 0
    cr_accts = tuple(getattr(credits, "accounts", ()) or ()) if credits is not None else ()
    usable_badge = (
        f'<span class="rr-pill rr-pill-unlock">{usable_cr}× usable now</span>'
        if usable_cr > 0
        else ""
    )
    if cr_accts:
        top_bits: list[str] = []
        for ca in cr_accts[:3]:
            ca_alias = _safe_alias(
                getattr(ca, "alias", ""),
                vendor=str(getattr(ca, "vendor", "codex")),
                slot=int(getattr(ca, "slot", 0) or 0),
            )
            ca_av = int(getattr(ca, "available", 0) or 0)
            ca_us = int(getattr(ca, "usable", 0) or 0)
            ca_rel = bool(getattr(ca, "needs_relogin", False))
            tag = " relogin" if ca_rel else (" usable" if ca_us > 0 else "")
            top_bits.append(f"{ca_alias} {ca_av}×{tag}")
        cr_sub = _esc(" · ".join(top_bits))
    else:
        cr_sub = "No banked reset credits reported"

    perish_eq = float(getattr(cap, "perishable_seats_48h", 0.0) or 0.0)
    c_locked = int(getattr(cap, "claude_locked_relogin_seats", 0) or 0)
    x_locked = int(getattr(cap, "codex_locked_relogin_seats", 0) or 0)
    total_locked = c_locked + x_locked
    locked_detail = (
        f" ({c_locked} Claude · {x_locked} Codex)" if total_locked > 0 else ""
    )
    perish_cls = " rr-kpi-warn" if perish_eq > 0.0 else ""
    perish_count_note = (
        f"{len(perish_events)} window(s) expiring &lt;48h · "
        if perish_events
        else ""
    )

    return (
        '<div class="rr-kpi-grid">'
        '<div class="rr-kpi">'
        '<div class="rr-kpi-label"><span class="rr-v-claude">✦</span> Claude Fable Ready</div>'
        f'<div class="rr-kpi-val">{c_free:.2f} / {c_total} seats</div>'
        f'<div class="rr-mini-track"><div class="rr-mini-fill rr-fill-unlock" style="width:{c_pct:.1f}%"></div></div>'
        f'<div class="rr-kpi-sub">{c_sub}</div>'
        "</div>"
        '<div class="rr-kpi">'
        '<div class="rr-kpi-label"><span class="rr-v-codex">◈</span> Codex Weekly Ready</div>'
        f'<div class="rr-kpi-val">{x_free:.2f} / {x_total} seats</div>'
        f'<div class="rr-mini-track"><div class="rr-mini-fill rr-fill-credit" style="width:{x_pct:.1f}%"></div></div>'
        f'<div class="rr-kpi-sub">{x_sub}</div>'
        "</div>"
        '<div class="rr-kpi">'
        '<div class="rr-kpi-label">↺ Banked Reset Credits</div>'
        f'<div class="rr-kpi-val">{live_cr}× live / {total_cr}× total {usable_badge}</div>'
        f'<div class="rr-kpi-sub">{cr_sub}</div>'
        "</div>"
        f'<div class="rr-kpi{perish_cls}">'
        '<div class="rr-kpi-label">⏳ Perishable &lt; 48h &amp; Relogin</div>'
        f'<div class="rr-kpi-val">{perish_eq:.2f} seat-eq at risk</div>'
        f'<div class="rr-kpi-sub">{perish_count_note}Locked relogin: '
        f"<strong>{total_locked} seats</strong>{_esc(locked_detail)}</div>"
        "</div>"
        "</div>"
    )


def _reset_advisor_callout(radar: Any) -> str:
    """Smart Fleet Advisor callout (uses ``.rr-advisor``, never ``.banner``)."""
    rec = getattr(radar, "recommendation", None)
    if rec is None:
        return ""
    headline = _sanitize_text(getattr(rec, "headline", "")).strip()
    detail = _sanitize_text(getattr(rec, "detail", "")).strip()
    if not headline and not detail:
        return ""
    raw_kind = str(getattr(rec, "kind", "healthy") or "healthy").strip().lower()
    safe_kind = re.sub(r"[^a-z0-9_-]", "", raw_kind) or "healthy"
    meta: dict[str, tuple[str, str]] = {
        "use_reset_credit": ("⚡", "RESET CREDIT READY"),
        "burn_perishable": ("⏳", "BURN EXPIRING FIRST"),
        "unlock_soon": ("🔓", "UNLOCK INCOMING"),
        "relogin_capacity": ("🔑", "RELOGIN CAPACITY"),
        "healthy": ("✓", "FLEET HEALTHY"),
    }
    icon, badge = meta.get(safe_kind, ("•", "FLEET ADVISOR"))
    detail_html = f'<span class="rr-advisor-detail">{_esc(detail)}</span>' if detail else ""
    return (
        f'<div class="rr-advisor rr-advisor-{_esc(safe_kind)}" role="status">'
        f'<span class="rr-advisor-badge">{_esc(icon)} {_esc(badge)}</span>'
        '<div class="rr-advisor-body">'
        f'<strong>{_esc(headline)}</strong>{detail_html}'
        "</div></div>"
    )


def _reset_gantt_svg(radar: Any) -> str:
    """B. Pure SVG 7-Day Reset Gantt / Swimlane Chart (zero scripts, zero url())."""
    raw_events = tuple(getattr(radar, "events", ()) or ())
    if not raw_events:
        return ""
    ordered = sorted(
        raw_events,
        key=lambda e: (
            float(getattr(e, "resets_at_epoch", 0.0) or 0.0),
            str(getattr(e, "vendor", "")),
            int(getattr(e, "slot", 0) or 0),
            str(getattr(e, "window", "")),
        ),
    )

    # Group into one swimlane per account, ordered by soonest reset first.
    lanes: list[tuple[tuple[str, int, str], list[Any]]] = []
    lane_index: dict[tuple[str, int, str], int] = {}
    for ev in ordered:
        vendor = str(getattr(ev, "vendor", "claude") or "claude")
        slot = int(getattr(ev, "slot", 0) or 0)
        alias = _safe_alias(getattr(ev, "alias", ""), vendor=vendor, slot=slot)
        key = (vendor, slot, alias)
        idx = lane_index.get(key)
        if idx is None:
            lane_index[key] = len(lanes)
            lanes.append((key, [ev]))
        else:
            lanes[idx][1].append(ev)

    credits_map = _credit_lookup(radar)
    width = 960.0
    label_w = 216.0
    plot_x = label_w
    plot_w = 588.0  # 84 px per day * 7 days
    day_w = plot_w / 7.0
    top_h = 34.0
    row_h = 36.0
    bot_h = 12.0
    plot_h = len(lanes) * row_h
    total_h = top_h + plot_h + bot_h

    parts: list[str] = []

    # 24h highlight band
    parts.append(
        f'<rect class="rr-band-24h" x="{_round(plot_x)}" y="{_round(top_h - 8.0)}" '
        f'width="{_round(day_w)}" height="{_round(plot_h + 8.0)}" rx="3">'
        "<title>Next 24 hours</title></rect>"
    )
    parts.append(
        f'<text class="rr-band-label" x="{_round(plot_x + day_w / 2.0)}" y="12" '
        'text-anchor="middle">24h</text>'
    )

    # Vertical day gridlines & X-axis labels (Now, +1d .. +7d)
    for day_i in range(8):
        gx = plot_x + day_i * day_w
        grid_cls = "rr-grid rr-grid-now" if day_i == 0 else "rr-grid"
        parts.append(
            f'<line class="{grid_cls}" x1="{_round(gx)}" y1="{_round(top_h - 6.0)}" '
            f'x2="{_round(gx)}" y2="{_round(top_h + plot_h)}"></line>'
        )
        tick = "Now" if day_i == 0 else f"+{day_i}d"
        anchor = "start" if day_i == 0 else ("end" if day_i == 7 else "middle")
        parts.append(
            f'<text class="rr-axis-txt" x="{_round(gx)}" y="24" '
            f'text-anchor="{anchor}">{tick}</text>'
        )

    # Swimlanes
    for row_idx, ((vendor, slot, alias), lane_events) in enumerate(lanes):
        row_y = top_h + row_idx * row_h
        cy = row_y + row_h / 2.0
        primary = lane_events[0]
        is_active = any(bool(getattr(e, "is_active", False)) for e in lane_events)
        is_muted = all(
            bool(getattr(e, "no_fable", False)) or bool(getattr(e, "disabled", False))
            for e in lane_events
        )
        alt_cls = " rr-lane-alt" if row_idx % 2 == 1 else ""
        glyph = _vendor_glyph(vendor)
        v_cls = "rr-svg-v-claude" if vendor == "claude" else "rr-svg-v-codex"
        name_cls = (
            "rr-lane-name rr-lane-muted"
            if is_muted
            else ("rr-lane-name rr-lane-active" if is_active else "rr-lane-name")
        )
        shown_alias = _clip(alias, 15) + (" ●" if is_active else "")
        win_names: list[str] = []
        for ev in lane_events:
            wn = _sanitize_text(getattr(ev, "window", "")).strip()
            if wn and wn not in win_names:
                win_names.append(wn)
        win_tag = _clip("·".join(win_names[:2]), 11)
        if all(bool(getattr(e, "no_fable", False)) for e in lane_events):
            win_tag = _clip(f"{win_tag} noF" if win_tag else "no-Fable", 12)

        parts.append(f'<g class="rr-lane">')
        parts.append(
            f'<rect class="rr-lane-bg{alt_cls}" x="0" y="{_round(row_y)}" '
            f'width="{_round(width)}" height="{_round(row_h)}" rx="4"></rect>'
        )
        parts.append(
            f'<line class="rr-track-line" x1="{_round(plot_x)}" y1="{_round(cy)}" '
            f'x2="{_round(plot_x + plot_w)}" y2="{_round(cy)}"></line>'
        )
        parts.append(
            f'<text class="{v_cls}" x="10" y="{_round(cy + 4.0)}">{_esc(glyph)}</text>'
        )
        parts.append(
            f'<text class="{name_cls}" x="26" y="{_round(cy + 4.0)}">{_esc(shown_alias)}'
            f"<title>{_esc(vendor_label(vendor))}: {_esc(alias)}</title></text>"
        )
        parts.append(
            f'<text class="rr-lane-win" x="{_round(plot_x - 10.0)}" y="{_round(cy + 3.5)}" '
            f'text-anchor="end">{_esc(win_tag)}</text>'
        )

        # Draw farthest bars first so the soonest bar & marker sit clearly on top.
        for ev in reversed(lane_events):
            secs = max(0.0, float(getattr(ev, "seconds_until", 0.0) or 0.0))
            beyond = secs > _RR_HORIZON_SECONDS
            frac = min(1.0, secs / _RR_HORIZON_SECONDS)
            bar_w = max(5.0, frac * plot_w)
            mx = min(plot_x + plot_w, plot_x + bar_w)
            bucket = _status_bucket(str(getattr(ev, "kind", "upcoming")))
            used_pct = getattr(ev, "used_pct", None)
            pct_str = f"{float(used_pct):.0f}% used" if used_pct is not None else "usage n/a"
            cd_lbl = _sanitize_text(getattr(ev, "countdown_label", ""))
            rst_lbl = _sanitize_text(getattr(ev, "reset_label", ""))
            ev_win = _sanitize_text(getattr(ev, "window", ""))
            tip = (
                f"{alias} · {ev_win} · {pct_str} · resets {rst_lbl} "
                f"(in {cd_lbl}{' · >7d' if beyond else ''})"
            )
            parts.append(
                f'<rect class="rr-bar rr-bar-{bucket}" x="{_round(plot_x)}" '
                f'y="{_round(cy - 4.0)}" width="{_round(bar_w)}" height="8" rx="4">'
                f"<title>{_esc(tip)}</title></rect>"
            )
            parts.append(
                f'<circle class="rr-halo rr-halo-{bucket}" cx="{_round(mx)}" '
                f'cy="{_round(cy)}" r="8.5"></circle>'
            )
            parts.append(
                f'<circle class="rr-mk rr-mk-{bucket}" cx="{_round(mx)}" '
                f'cy="{_round(cy)}" r="4.8"><title>{_esc(tip)}</title></circle>'
            )

        # Right annotation: primary countdown + utilization + Codex reset-credit pill
        credit = credits_map.get((vendor, slot))
        has_credit = credit is not None and int(getattr(credit, "available", 0) or 0) > 0
        p_cd = _sanitize_text(getattr(primary, "countdown_label", ""))
        p_used = getattr(primary, "used_pct", None)
        right_txt = (
            f"{p_cd} · {float(p_used):.0f}%"
            if p_used is not None
            else p_cd
        )
        right_txt = _clip(right_txt, 13 if has_credit else 20)
        parts.append(
            f'<text class="rr-right-txt" x="{_round(plot_x + plot_w + 10.0)}" '
            f'y="{_round(cy + 3.5)}">{_esc(right_txt)}</text>'
        )

        if has_credit:
            av = int(getattr(credit, "available", 0) or 0)
            us = int(getattr(credit, "usable", 0) or 0)
            pill_cls = "rr-svg-pill-usable" if us > 0 else "rr-svg-pill-credit"
            pill_txt_cls = "rr-svg-pill-txt-usable" if us > 0 else "rr-svg-pill-txt-credit"
            pill_tip = f"{alias}: {av}× banked reset credit(s), {us}× usable now"
            parts.append(
                f'<rect class="{pill_cls}" x="902" y="{_round(cy - 9.0)}" '
                f'width="50" height="18" rx="9"><title>{_esc(pill_tip)}</title></rect>'
            )
            parts.append(
                f'<text class="{pill_txt_cls}" x="927" y="{_round(cy + 3.5)}" '
                f'text-anchor="middle">↺ {av}×<title>{_esc(pill_tip)}</title></text>'
            )

        parts.append("</g>")

    svg = (
        f'<svg class="rr-gantt" viewBox="0 0 {_round(width)} {_round(total_h)}" '
        'role="img" aria-label="7-day fleet reset timeline">'
        + "".join(parts)
        + "</svg>"
    )
    legend = (
        '<ul class="legend rr-legend">'
        '<li><span class="swatch rr-sw-unlock"></span><span>Unlock (≥85% used → fresh capacity)</span></li>'
        '<li><span class="swatch rr-sw-perish"></span><span>Perishable (≥25% free expiring ≤48h → burn first)</span></li>'
        '<li><span class="swatch rr-sw-upcoming"></span><span>Upcoming reset</span></li>'
        '<li><span class="swatch rr-sw-credit"></span><span>↺ N× Banked Codex reset credit (emerald = usable now)</span></li>'
        "</ul>"
    )
    return f'<div class="rr-gantt-wrap">{svg}{legend}</div>'


def _reset_schedule_table(radar: Any) -> str:
    """C. Unified Reset Schedule & Banked Reset Credits Table across all accounts."""
    raw_events = tuple(getattr(radar, "events", ()) or ())
    ordered = sorted(
        raw_events,
        key=lambda e: (
            float(getattr(e, "resets_at_epoch", 0.0) or 0.0),
            str(getattr(e, "vendor", "")),
            int(getattr(e, "slot", 0) or 0),
            str(getattr(e, "window", "")),
        ),
    )
    credits_map = _credit_lookup(radar)
    seen_credit_keys: set[tuple[str, int]] = set()
    rows_html: list[str] = []

    for ev in ordered:
        vendor = str(getattr(ev, "vendor", "claude") or "claude")
        slot = int(getattr(ev, "slot", 0) or 0)
        alias = _safe_alias(getattr(ev, "alias", ""), vendor=vendor, slot=slot)
        v_cls = "rr-v-claude" if vendor == "claude" else "rr-v-codex"
        v_cell = f'<span class="{v_cls}">{_esc(_vendor_glyph(vendor))}</span> {_esc(vendor_label(vendor))}'

        tags: list[str] = []
        if bool(getattr(ev, "is_active", False)):
            tags.append('<span class="rr-tag rr-tag-active">active</span>')
        p_lbl = _sanitize_text(getattr(ev, "plan_label", "")).strip()
        if p_lbl:
            tags.append(f'<span class="rr-tag">{_esc(p_lbl)}</span>')
        if bool(getattr(ev, "no_fable", False)):
            tags.append('<span class="rr-tag rr-tag-muted">no Fable</span>')
        if bool(getattr(ev, "disabled", False)):
            tags.append('<span class="rr-tag rr-tag-muted">disabled</span>')
        acct_cell = f"<strong>{_esc(alias)}</strong>" + (" " + " ".join(tags) if tags else "")

        win = _sanitize_text(getattr(ev, "window", ""))
        used_pct = getattr(ev, "used_pct", None)
        free_pct = getattr(ev, "free_pct", None)
        if used_pct is not None:
            free_note = (
                f' <span class="exact">({float(free_pct):.0f}% free)</span>'
                if free_pct is not None
                else ""
            )
            util_cell = f"{float(used_pct):.0f}%{free_note}"
        else:
            util_cell = "—"

        rst_lbl = _sanitize_text(getattr(ev, "reset_label", ""))
        cd_lbl = _sanitize_text(getattr(ev, "countdown_label", ""))

        bucket = _status_bucket(str(getattr(ev, "kind", "upcoming")))
        status_labels = {
            "unlock": ("rr-pill-unlock", "Unlock"),
            "perish": ("rr-pill-perish", "Perishable"),
            "upcoming": ("rr-pill-upcoming", "Upcoming"),
        }
        p_cls, p_txt = status_labels[bucket]
        pills = [f'<span class="rr-pill {p_cls}">{p_txt}</span>']

        credit = credits_map.get((vendor, slot))
        if credit is not None:
            seen_credit_keys.add((vendor, slot))
            av = int(getattr(credit, "available", 0) or 0)
            us = int(getattr(credit, "usable", 0) or 0)
            if us > 0:
                pills.append(
                    f'<span class="rr-pill rr-pill-unlock">↺ {av}× ({us}× usable)</span>'
                )
            elif av > 0:
                pills.append(
                    f'<span class="rr-pill rr-pill-credit">↺ {av}× banked</span>'
                )

        rows_html.append(
            "<tr>"
            f"<td>{v_cell}</td>"
            f"<td>{acct_cell}</td>"
            f"<td><code>{_esc(win)}</code></td>"
            f'<td class="num">{util_cell}</td>'
            f"<td>{_esc(rst_lbl)}</td>"
            f'<td class="num"><strong>{_esc(cd_lbl)}</strong></td>'
            f'<td>{" ".join(pills)}</td>'
            "</tr>"
        )

    # Surface credit-holding accounts with no upcoming timed reset (e.g. relogin seats).
    credits = getattr(radar, "credits", None)
    credit_accounts = tuple(getattr(credits, "accounts", ()) or ()) if credits is not None else ()
    extra_credits = sorted(
        (
            ca
            for ca in credit_accounts
            if (str(getattr(ca, "vendor", "")), int(getattr(ca, "slot", 0) or 0))
            not in seen_credit_keys
            and int(getattr(ca, "available", 0) or 0) > 0
        ),
        key=lambda c: (
            str(getattr(c, "vendor", "")),
            int(getattr(c, "slot", 0) or 0),
            str(getattr(c, "alias", "")),
        ),
    )
    for ca in extra_credits:
        vendor = str(getattr(ca, "vendor", "codex") or "codex")
        slot = int(getattr(ca, "slot", 0) or 0)
        alias = _safe_alias(getattr(ca, "alias", ""), vendor=vendor, slot=slot)
        v_cls = "rr-v-claude" if vendor == "claude" else "rr-v-codex"
        v_cell = f'<span class="{v_cls}">{_esc(_vendor_glyph(vendor))}</span> {_esc(vendor_label(vendor))}'
        av = int(getattr(ca, "available", 0) or 0)
        us = int(getattr(ca, "usable", 0) or 0)
        relogin = bool(getattr(ca, "needs_relogin", False))
        used_pct = getattr(ca, "used_pct", None)
        util_cell = f"{float(used_pct):.0f}%" if used_pct is not None else "—"
        pills: list[str] = []
        if relogin:
            pills.append('<span class="rr-pill rr-pill-warn">Relogin</span>')
        if us > 0:
            pills.append(
                f'<span class="rr-pill rr-pill-unlock">↺ {av}× ({us}× usable)</span>'
            )
        else:
            pills.append(f'<span class="rr-pill rr-pill-credit">↺ {av}× banked</span>')
        rows_html.append(
            "<tr>"
            f"<td>{v_cell}</td>"
            f"<td><strong>{_esc(alias)}</strong></td>"
            "<td><code>7d</code></td>"
            f'<td class="num">{util_cell}</td>'
            "<td>—</td>"
            f'<td class="num">{"relogin" if relogin else "—"}</td>'
            f'<td>{" ".join(pills)}</td>'
            "</tr>"
        )

    if not rows_html:
        return ""
    return (
        '<div class="rr-table-wrap">'
        "<table><thead><tr>"
        "<th>Vendor</th><th>Account</th><th>Window</th>"
        '<th class="num">Utilization</th><th>Reset Time</th>'
        '<th class="num">Countdown</th><th>Status / Banked Credits</th>'
        f"</tr></thead><tbody>{''.join(rows_html)}</tbody></table></div>"
    )


def _reset_radar_section(radar: Any) -> str:
    """Visual Fleet Reset Timeline & Capacity Forecast section, or ``""`` when empty."""
    if radar is None:
        return ""
    events = tuple(getattr(radar, "events", ()) or ())
    cap = getattr(radar, "capacity", None)
    credits = getattr(radar, "credits", None)
    has_capacity = cap is not None and any(
        int(getattr(cap, field, 0) or 0) > 0
        for field in (
            "claude_fable_total_seats",
            "claude_locked_relogin_seats",
            "codex_total_seats",
            "codex_locked_relogin_seats",
        )
    )
    has_credits = credits is not None and (
        int(getattr(credits, "total_available", 0) or 0) > 0
        or bool(getattr(credits, "accounts", ()))
    )
    if not events and not has_capacity and not has_credits:
        return ""

    parts: list[str] = []
    kpi_html = _reset_kpi_strip(radar)
    if kpi_html:
        parts.append(kpi_html)
    advisor_html = _reset_advisor_callout(radar)
    if advisor_html:
        parts.append(advisor_html)
    gantt_html = _reset_gantt_svg(radar)
    if gantt_html:
        parts.append('<h3 class="rr-subhead">7-Day Reset Swimlanes</h3>' + gantt_html)
    table_html = _reset_schedule_table(radar)
    if table_html:
        parts.append(
            '<h3 class="rr-subhead">Unified Reset Schedule &amp; Banked Credits</h3>'
            + table_html
        )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------


def _banner(indexing: str | None) -> str:
    """The header's partial-figures banner, or ``""``.

    A caller that passes an empty string means "nothing to say" exactly as
    ``None`` does, so a progress label that has not been computed yet cannot
    produce an empty red box.
    """
    text = str(indexing or "").strip()
    if not text:
        return ""
    return f'<p class="banner">{_esc(INDEXING_BANNER)} {_esc(text)}</p>'


def _style() -> str:
    """The whole stylesheet, inline. Light tokens on ``:root``, dark overridden.

    Every colour is a token defined once in the light block and redefined in
    the ``prefers-color-scheme: dark`` block, so no rule anywhere has its only
    definition inside a media query and a browser with no preference still gets
    a complete palette.
    """
    light = "".join(
        f"--s{index + 1}:{value};" for index, value in enumerate(_SERIES_LIGHT)
    )
    dark = "".join(
        f"--s{index + 1}:{value};" for index, value in enumerate(_SERIES_DARK)
    )
    return (
        ":root{color-scheme:light dark;--bg:#f7f7f5;--panel:#ffffff;--ink:#1b1c1e;"
        "--dim:#5d6169;--rule:#e0e0dc;--accent:#b3402f;--ok:#3f8b62;--track:#e8e8e4;"
        "--rr-unlock:#15803d;--rr-unlock-bg:#dcfce7;--rr-perish:#b45309;--rr-perish-bg:#fef3c7;"
        "--rr-upcoming:#4f46e5;--rr-upcoming-bg:#e0e7ff;--rr-credit:#0e7490;--rr-credit-bg:#cffafe;"
        "--rr-warn-bg:#fee2e2;--rr-band24:#f0f9ff;--rr-lane-alt:#f8f9fb;--rr-card:#fafaf8;"
        "--rr-claude:#b3402f;--rr-codex:#0e7490;"
        + light
        + "}"
        "@media (prefers-color-scheme: dark){:root{--bg:#0d0e12;--panel:#15171e;"
        "--ink:#f4f5f7;--dim:#9499a6;--rule:#242733;--accent:#ff6b2c;--ok:#30d175;"
        "--track:#242733;"
        "--rr-unlock:#34d399;--rr-unlock-bg:#0c2c20;--rr-perish:#fbbf24;--rr-perish-bg:#2e220b;"
        "--rr-upcoming:#818cf8;--rr-upcoming-bg:#1e2140;--rr-credit:#38bdf8;--rr-credit-bg:#0f293a;"
        "--rr-warn-bg:#331515;--rr-band24:#182032;--rr-lane-alt:#191c26;--rr-card:#12141c;"
        "--rr-claude:#ff6b2c;--rr-codex:#38bdf8;" + dark + "}}"
        "*{box-sizing:border-box}"
        "body{margin:0;background:var(--bg);color:var(--ink);"
        "font:14px/1.5 -apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif}"
        "header,main,footer{max-width:1000px;margin:0 auto;padding:0 20px}"
        "header{padding-top:28px}"
        "h1{font-size:20px;margin:0 0 4px}"
        "h2{font-size:15px;margin:0 0 10px;letter-spacing:.02em;text-transform:uppercase;color:var(--dim)}"
        "h3{font-size:14px;margin:0 0 8px}"
        "section{background:var(--panel);border:1px solid var(--rule);border-radius:10px;"
        "padding:16px 18px;margin:16px 0}"
        ".sub,.note{color:var(--dim);margin:0 0 10px;font-size:12px}"
        ".banner{color:var(--accent);border:1px solid var(--accent);border-radius:6px;"
        "padding:8px 12px;margin:0 0 10px;font-size:12px;font-weight:600}"
        ".note.warn{color:var(--accent)}.note.crit{color:var(--accent);font-weight:600}"
        ".windows{display:flex;flex-wrap:wrap;gap:20px}"
        ".window{flex:1 1 420px;min-width:0}"
        ".headline{display:flex;gap:12px;align-items:baseline;margin:0 0 10px}"
        ".headline strong{font-size:18px}"
        ".exact{color:var(--dim);font-variant-numeric:tabular-nums;font-size:11px}"
        ".chart{margin:0 0 10px}"
        "svg.bar{width:100%;height:26px;display:block;border-radius:3px}"
        "svg.cols{width:100%;height:150px;display:block}"
        "line.axis{stroke:var(--rule);stroke-width:1}"
        ".axis-labels{display:flex;justify-content:space-between;color:var(--dim);font-size:11px}"
        "ul.legend{list-style:none;display:flex;flex-wrap:wrap;gap:4px 16px;margin:6px 0 0;padding:0;font-size:12px}"
        "ul.legend li{display:flex;align-items:center;gap:6px}"
        ".swatch{width:10px;height:10px;border-radius:2px;display:inline-block}"
        "ul.info{margin:6px 0 0;padding-left:18px;color:var(--dim);font-size:12px}"
        "table{width:100%;border-collapse:collapse;font-size:13px}"
        "th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--rule)}"
        "th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase}"
        ".num{text-align:right;font-variant-numeric:tabular-nums}"
        ".accounts{display:flex;flex-wrap:wrap;gap:18px}"
        ".account{flex:1 1 320px;min-width:0;border:1px solid var(--rule);border-radius:8px;padding:12px}"
        ".account h3{display:flex;gap:8px;align-items:baseline}"
        ".vendor,.plan{color:var(--dim);font-weight:400;font-size:11px;text-transform:uppercase}"
        ".window-bar{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:12px}"
        ".w-name{width:56px;color:var(--dim)}"
        ".w-pct{width:110px;font-variant-numeric:tabular-nums}"
        ".w-reset{color:var(--dim);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
        "svg.meter{flex:0 0 140px;height:10px;border-radius:2px}"
        "rect.track{fill:var(--track)}rect.fill-ok{fill:var(--ok)}"
        "rect.fill-capped{fill:var(--accent)}rect.fill-expired{fill:var(--dim)}"
        "line.limit{stroke:var(--rule);stroke-width:2}"
        ".empty{padding:28px 0;text-align:center;color:var(--dim)}"
        ".bk-hero,.bk-boss{display:flex;gap:16px;align-items:center;border:1px solid var(--rule);border-radius:10px;padding:14px;margin-bottom:14px}"
        ".bk-boss{border-color:var(--accent)}"
        ".bk-sprite-pad{flex:0 0 92px;height:92px;border-radius:8px;background:#121318;border:1px solid var(--rule);display:flex;align-items:center;justify-content:center}"
        ".bk-boss-pad{flex:0 0 78px;height:78px;background:#1a1014}"
        ".bk-hero-meta{flex:1;min-width:0}"
        ".bk-hero-top{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;margin-bottom:4px}"
        ".bk-hero-top strong{font-size:18px}"
        ".bk-lv{color:var(--accent);font-weight:600;font-size:13px}"
        ".bk-mood{margin-left:auto;border:1px solid var(--rule);border-radius:99px;padding:2px 10px;font-size:12px;font-weight:600}"
        ".bk-xp-row{display:flex;justify-content:space-between;font-size:12px;color:var(--dim);margin:6px 0 4px}"
        ".bk-xp-track{height:8px;background:var(--track);border-radius:4px;overflow:hidden}"
        ".bk-xp-fill{height:100%;background:var(--accent);border-radius:4px}"
        ".bk-evo-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}"
        ".bk-badge-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}"
        ".bk-stage,.bk-badge{border:1px solid var(--rule);border-radius:8px;padding:10px}"
        ".bk-stage.active,.bk-badge.unlocked{border-color:var(--accent)}"
        ".bk-stage.locked,.bk-badge.locked{opacity:.65}"
        ".bk-stage-head,.bk-badge-head{display:flex;gap:10px;align-items:center;margin-bottom:6px}"
        ".bk-stage-head h4,.bk-badge-head h4{margin:0;font-size:13px}"
        ".bk-badge-icon{font-size:20px}"
        ".bk-tag{font-size:11px;color:var(--dim);font-weight:600}"
        ".rr-kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:12px;margin-bottom:12px}"
        ".rr-kpi{background:var(--rr-card);border:1px solid var(--rule);border-radius:8px;padding:11px 12px}"
        ".rr-kpi-warn{border-color:var(--rr-perish)}"
        ".rr-kpi-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.03em;color:var(--dim);margin-bottom:4px}"
        ".rr-kpi-val{font-size:17px;font-weight:700;font-variant-numeric:tabular-nums;display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px}"
        ".rr-kpi-sub{font-size:12px;color:var(--dim);line-height:1.4}"
        ".rr-mini-track{height:5px;background:var(--track);border-radius:99px;overflow:hidden;margin:0 0 6px}"
        ".rr-mini-fill{height:100%;border-radius:99px}"
        ".rr-fill-unlock{background:var(--rr-unlock)}.rr-fill-credit{background:var(--rr-credit)}"
        ".rr-v-claude{color:var(--rr-claude);font-weight:700}.rr-v-codex{color:var(--rr-codex);font-weight:700}"
        ".rr-advisor{display:flex;flex-wrap:wrap;gap:10px;align-items:center;border:1px solid var(--rule);border-left:4px solid var(--rr-upcoming);border-radius:8px;background:var(--rr-card);padding:10px 12px;margin-bottom:14px}"
        ".rr-advisor-use_reset_credit,.rr-advisor-unlock_soon{border-left-color:var(--rr-unlock)}"
        ".rr-advisor-burn_perishable{border-left-color:var(--rr-perish)}"
        ".rr-advisor-relogin_capacity{border-left-color:var(--accent)}"
        ".rr-advisor-badge{font-size:10px;font-weight:700;letter-spacing:.04em;padding:3px 8px;border-radius:99px;background:var(--rr-upcoming-bg);color:var(--rr-upcoming);white-space:nowrap}"
        ".rr-advisor-use_reset_credit .rr-advisor-badge,.rr-advisor-unlock_soon .rr-advisor-badge{background:var(--rr-unlock-bg);color:var(--rr-unlock)}"
        ".rr-advisor-burn_perishable .rr-advisor-badge{background:var(--rr-perish-bg);color:var(--rr-perish)}"
        ".rr-advisor-relogin_capacity .rr-advisor-badge{background:var(--rr-warn-bg);color:var(--accent)}"
        ".rr-advisor-body{flex:1;min-width:200px;display:flex;flex-direction:column;gap:2px;font-size:13px}"
        ".rr-advisor-detail{font-size:12px;color:var(--dim)}"
        ".rr-subhead{margin:14px 0 8px;font-size:13px;color:var(--ink)}"
        ".rr-gantt-wrap{border:1px solid var(--rule);border-radius:8px;padding:10px;background:var(--rr-card);overflow-x:auto}"
        "svg.rr-gantt{width:100%;min-width:680px;height:auto;display:block;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif}"
        "rect.rr-band-24h{fill:var(--rr-band24)}"
        "text.rr-band-label{fill:var(--rr-credit);font-size:10px;font-weight:700;letter-spacing:.04em}"
        "line.rr-grid{stroke:var(--rule);stroke-width:1;stroke-dasharray:2 3}"
        "line.rr-grid-now{stroke:var(--ink);stroke-width:1.4;stroke-dasharray:none;opacity:.55}"
        "text.rr-axis-txt{fill:var(--dim);font-size:10.5px;font-weight:600}"
        "rect.rr-lane-bg{fill:transparent;transition:fill .12s ease}"
        "rect.rr-lane-alt{fill:var(--rr-lane-alt)}"
        "g.rr-lane:hover rect.rr-lane-bg{fill:var(--rr-band24)}"
        "line.rr-track-line{stroke:var(--track);stroke-width:2}"
        "text.rr-svg-v-claude{fill:var(--rr-claude);font-size:12px;font-weight:700}"
        "text.rr-svg-v-codex{fill:var(--rr-codex);font-size:12px;font-weight:700}"
        "text.rr-lane-name{fill:var(--ink);font-size:12px;font-weight:500}"
        "text.rr-lane-active{font-weight:700}"
        "text.rr-lane-muted{fill:var(--dim)}"
        "text.rr-lane-win{fill:var(--dim);font-size:10px;font-weight:600}"
        "rect.rr-bar-unlock{fill:var(--rr-unlock);opacity:.78}"
        "rect.rr-bar-perish{fill:var(--rr-perish);opacity:.78}"
        "rect.rr-bar-upcoming{fill:var(--rr-upcoming);opacity:.55}"
        "circle.rr-halo-unlock{fill:var(--rr-unlock);opacity:.22}"
        "circle.rr-halo-perish{fill:var(--rr-perish);opacity:.22}"
        "circle.rr-halo-upcoming{fill:var(--rr-upcoming);opacity:.18}"
        "circle.rr-mk{stroke:var(--panel);stroke-width:1.6}"
        "circle.rr-mk-unlock{fill:var(--rr-unlock)}"
        "circle.rr-mk-perish{fill:var(--rr-perish)}"
        "circle.rr-mk-upcoming{fill:var(--rr-upcoming)}"
        "text.rr-right-txt{fill:var(--dim);font-size:10.5px;font-variant-numeric:tabular-nums}"
        "rect.rr-svg-pill-usable{fill:var(--rr-unlock-bg);stroke:var(--rr-unlock);stroke-width:1}"
        "rect.rr-svg-pill-credit{fill:var(--rr-credit-bg);stroke:var(--rr-credit);stroke-width:1}"
        "text.rr-svg-pill-txt-usable{fill:var(--rr-unlock);font-size:10px;font-weight:700}"
        "text.rr-svg-pill-txt-credit{fill:var(--rr-credit);font-size:10px;font-weight:700}"
        ".rr-sw-unlock{background:var(--rr-unlock)}.rr-sw-perish{background:var(--rr-perish)}"
        ".rr-sw-upcoming{background:var(--rr-upcoming)}.rr-sw-credit{background:var(--rr-credit)}"
        ".rr-legend{margin-top:8px;font-size:11px;color:var(--dim)}"
        ".rr-table-wrap{overflow-x:auto}"
        ".rr-pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:600;white-space:nowrap;margin:1px 2px 1px 0}"
        ".rr-pill-unlock{background:var(--rr-unlock-bg);color:var(--rr-unlock)}"
        ".rr-pill-perish{background:var(--rr-perish-bg);color:var(--rr-perish)}"
        ".rr-pill-upcoming{background:var(--rr-upcoming-bg);color:var(--rr-upcoming)}"
        ".rr-pill-credit{background:var(--rr-credit-bg);color:var(--rr-credit)}"
        ".rr-pill-warn{background:var(--rr-warn-bg);color:var(--accent)}"
        ".rr-tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;background:var(--track);color:var(--dim);vertical-align:middle}"
        ".rr-tag-active{background:var(--rr-unlock-bg);color:var(--rr-unlock)}"
        ".rr-tag-muted{opacity:.8}"
        "footer{padding:8px 20px 32px;color:var(--dim);font-size:11px}"
        "footer p{margin:2px 0}"
    )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def render_dashboard(
    history: Any,
    rollups: Any,
    quota_rows: Sequence[AccountRow] = (),
    accounts: Sequence[AccountRow] = (),
    *,
    now: float | None = None,
    pricing: PricingTable | None = None,
    indexing: str | None = None,
    workflow_runs: Sequence[Any] = (),
    monthly_subscription_usd: float = 0.0,
    account_notes: Mapping[int, str] | None = None,
    fable_guard: bool = True,
    reset_radar: Any = None,
) -> str:
    """One self-contained HTML document describing what the stores hold.

    Pure: reads *history* and *rollups*, touches no file, opens no socket and
    never raises for want of data - a missing store is a missing section, and a
    machine with nothing recorded gets :data:`EMPTY_MARKER` rather than a
    demonstration dataset.

    Args:
        history: a :class:`~cc_usage_widget.history.HistoryStore`, or ``None``
            when the mirror is off or unavailable. Supplies days the rollup
            store has already pruned, and the ``project`` column once roadmap
            item 6 adds one.
        rollups: the live :class:`~cc_usage_widget.rollup.DailyRollupStore`.
            Wins for every day it covers (see :func:`collect_cells`).
        quota_rows: read-only vendor quota rows (Codex pseudo-accounts).
        accounts: switchable claude-swap rows.
        now: epoch to render "as of"; defaults to the wall clock. Injected so a
            test gets a stable page.
        pricing: the price table. Absent, the page shows tokens and whatever
            dollar figures history recorded, and no cell is re-priced.
        indexing: the caller's progress label (``"indexing… 1,204/3,200"``)
            when a first index is still running, else ``None``. Present, it
            stamps :data:`INDEXING_BANNER` plus that label into the header, so
            a page rendered mid-scan says its figures are partial in the same
            words the menu uses. The page never derives this: only the widget
            knows whether a scanner has finished.
        workflow_runs: ``attribution.WorkflowRun`` values for the workflow
            table (F1); empty, the section is absent. :func:`build_dashboard`
            reads them from the attribution cache beside *rollups*.
    """
    stamp = time.time() if now is None else float(now)
    today = local_day_key(stamp)
    cells = collect_cells(
        history, rollups, today=today, days=max(WINDOW_DAYS), pricing=pricing
    )
    rows = tuple(accounts) + tuple(quota_rows)
    radar = (
        reset_radar
        if reset_radar is not None
        else _build_reset_radar(
            accounts,
            quota_rows,
            now=stamp,
            account_notes=account_notes,
            fable_guard=fable_guard,
        )
    )
    wall_body, wall_note = _wall_section(rows, stamp, history=history)

    sections = [
        _section("Windows", _totals_section(cells, today)),
        _section(
            "Subscription ROI",
            _roi_section(
                cells,
                rows,
                today,
                monthly_subscription_usd=monthly_subscription_usd,
            ),
            ident="roi",
        ),
        _section("Tokens per day", _daily_section(cells, today)),
        _section("Notional spend per day", _daily_usd_section(cells, today)),
        _section(
            "Prompt-cache efficiency",
            _cache_efficiency_section(cells, today, pricing),
            ident="cache-efficiency",
        ),
        _section("By model", _model_section(cells, today, pricing=pricing)),
        _section(
            "Model mix over time",
            _mix_section(cells, today),
            note="Share of each day's tokens, per vendor. Absolute volume is the chart above.",
        ),
        _section("By project", _project_section(history, today, pricing=pricing)),
        _section("Workflow runs", _workflow_section(workflow_runs), note=WORKFLOW_NOTE),
        _section(
            RESET_RADAR_HEADING,
            _reset_radar_section(radar),
            ident="fleet-reset-radar",
        ),
        _section("Quota windows", _quota_section(rows)),
        _section("Hours at the wall", wall_body, note=wall_note),
        _section("Unpriced volume", _unpriced_section(cells, today)),
        _section(
            "Belkins Companion & Trophy Dex",
            _belkins_dex_section(cells, accounts, quota_rows, today),
            ident="belkins-dex",
        ),
    ]
    body = "".join(section for section in sections if section)
    if not body:
        body = (
            f'<section><div class="empty"><p><strong>{_esc(EMPTY_MARKER)}</strong></p>'
            "<p>Nothing has been indexed into the rollup store or the history mirror "
            "yet, and no account reported a quota window. This page shows measurements "
            "only - there is no sample data to fall back on.</p></div></section>"
        )

    when = dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
    counted = len(cells)
    sourced = sorted({cell.source for cell in cells})
    provenance = ", ".join(sourced) if sourced else "no source read"
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Usage Bar dashboard</title>"
        f"<style>{_style()}</style></head><body>"
        f"<header><h1>Usage Bar dashboard</h1>"
        f'<p class="sub">Generated {_esc(when)} local &middot; {_esc(NOTIONAL_LABEL)} '
        f"&middot; {_esc(_int(counted))} cells read ({_esc(provenance)})</p>"
        + _banner(indexing)
        + "</header>"
        f"<main>{body}</main>"
        "<footer>"
        f"<p>Dollar figures are {_esc(NOTIONAL_LABEL)}; no invoice is involved.</p>"
        "<p>Days still inside the rollup window are read from the live store; "
        "older days come from the history mirror. Nothing on this page is "
        "estimated, sampled or generated.</p>"
        "</footer></body></html>"
    )


def write_dashboard(document: str, path: Path | str) -> Path:
    """Write *document* to *path* 0600, atomically. Returns the path.

    Atomic because the browser may still be reading the previous file when the
    next open regenerates it: a truncate-then-write would hand a half-page to a
    reload. The mode is set on the temporary file BEFORE the rename, so the
    document is never briefly world-readable - the same rule the credential
    files follow, applied to a page that is nothing but someone's spend.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    data = document.encode("utf-8")
    handle = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)
    os.chmod(temp, FILE_MODE)
    os.replace(temp, target)
    return target


def build_dashboard(
    history: Any,
    rollups: Any,
    quota_rows: Sequence[AccountRow] = (),
    accounts: Sequence[AccountRow] = (),
    *,
    now: float | None = None,
    pricing: PricingTable | None = None,
    path: Path | str | None = None,
    indexing: str | None = None,
    projects_dir: Path | str | None = None,
    monthly_subscription_usd: float = 0.0,
    account_notes: Mapping[int, str] | None = None,
    fable_guard: bool = True,
    reset_radar: Any = None,
) -> Path:
    """Render and write in one call. Returns the file written.

    The seam ``app`` uses: everything up to and including the disk write
    belongs on the worker thread, and only the hand-off to the browser is left
    to the caller. It is also where the workflow table's I/O happens: the
    attribution cache beside *rollups* and each run's journal under
    *projects_dir* (default :data:`~contracts.PROJECTS_DIR`), both read-only.
    """
    document = render_dashboard(
        history,
        rollups,
        quota_rows,
        accounts,
        now=now,
        pricing=pricing,
        indexing=indexing,
        workflow_runs=_workflow_runs_beside(rollups, pricing, projects_dir),
        monthly_subscription_usd=monthly_subscription_usd,
        account_notes=account_notes,
        fable_guard=fable_guard,
        reset_radar=reset_radar,
    )
    destination = (
        Path(path)
        if path is not None
        else dashboard_path_for(getattr(rollups, "path", None))
    )
    return write_dashboard(document, destination)
