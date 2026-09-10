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
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .contracts import (
    NOTIONAL_LABEL,
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
from .render import coarse_duration, fleet_reset_label

__all__ = [
    "Cell",
    "DASHBOARD_FILENAME",
    "DASHBOARD_PATH",
    "EMPTY_MARKER",
    "INDEXING_BANNER",
    "WINDOW_DAYS",
    "WALL_NO_HISTORY_NOTE",
    "build_dashboard",
    "collect_cells",
    "dashboard_path_for",
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

EMPTY_MARKER: Final[str] = "No usage recorded yet"
"""The empty state's exact wording, named so a test asserts the string rather
than a class name that could drift."""

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


def _esc(value: Any) -> str:
    """HTML-escape anything on its way into the document.

    Model names and project names reach this page from disk - a transcript's
    ``message.model``, a directory name - so they are untrusted text, not
    literals. One unescaped ``<`` would turn a filename into markup.
    """
    return html.escape(str(value), quote=True)


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
            parts.append(
                f'<rect x="{_round(x)}" y="{_round(y)}" width="{_round(bar_width)}" '
                f'height="{_round(span)}" fill="{_color(index_of.get(key, 0))}">'
                f"<title>{_esc(day)} {_esc(key)}: {_esc(_int(int(amount)))}</title></rect>"
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


def _fold(entries: Sequence[tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, amount in entries:
        out[key] = out.get(key, 0.0) + amount
    return out


def _model_section(cells: Sequence[Cell], today: DayKey) -> str:
    """Per-model tokens and dollars for each window, largest first."""
    blocks: list[str] = []
    for days in WINDOW_DAYS:
        scoped = _window_cells(cells, day_keys_back(today, days))
        if not scoped:
            continue
        series = _capped(_by(scoped, "model"), MAX_MODELS)
        rows = "".join(
            f"<tr><td>{_esc(item.label)}</td>"
            f'<td class="num">{_esc(format_tokens(item.tokens))}</td>'
            f'<td class="num exact">{_int(item.tokens)}</td>'
            f'<td class="num">{_esc(format_usd(item.usd))}'
            f'{"+" if item.unpriced_tokens else ""}</td></tr>'
            for item in series
        )
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


def _project_section(history: Any, today: DayKey) -> str:
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
    tokens: dict[str, int] = {}
    usd: dict[str, float] = {}
    for row in rows:
        project = str(getattr(row, "project", "") or "")
        if not project:
            continue
        usage = getattr(row, "usage", None)
        if not isinstance(usage, ModelUsage):
            continue
        tokens[project] = tokens.get(project, 0) + usage.total_tokens
        recorded = getattr(row, "usd_at_record", 0.0)
        if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
            usd[project] = usd.get(project, 0.0) + max(0.0, float(recorded))
    if not tokens:
        return ""
    ranked = sorted(tokens.items(), key=lambda pair: (-pair[1], pair[0]))[:MAX_PROJECTS]
    body = "".join(
        f"<tr><td>{_esc(name)}</td>"
        f'<td class="num">{_esc(format_tokens(count))}</td>'
        f'<td class="num exact">{_int(count)}</td>'
        f'<td class="num">{_esc(format_usd(usd.get(name, 0.0)))}</td></tr>'
        for name, count in ranked
    )
    return (
        '<table><thead><tr><th>Project</th><th class="num">Tokens</th>'
        '<th class="num exact">exact</th><th class="num">Notional at record</th>'
        f"</tr></thead><tbody>{body}</tbody></table>"
    )


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
        title = row.alias or row.vendor_label
        plan = f' <span class="plan">{_esc(row.plan_type)}</span>' if row.plan_type else ""
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


def _wall_section(rows: Sequence[AccountRow], now: float) -> str:
    """What each account's wall is right now - and why there is no weekly total.

    See :data:`WALL_NO_HISTORY_NOTE`. Everything here is read off the row: the
    percentage the source reported, and a countdown computed from the epoch it
    reported. Nothing is reconstructed.
    """
    if not rows:
        return ""
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
        body.append(
            f"<tr><td>{_esc(row.alias or row.vendor_label)}</td>"
            f"<td>{_esc(row.vendor_label)}</td>"
            f"<td>{_esc(state)}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Account</th><th>Vendor</th><th>Wall</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
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
        + light
        + "}"
        "@media (prefers-color-scheme: dark){:root{--bg:#16171a;--panel:#1e2024;"
        "--ink:#e9eaec;--dim:#9aa0a8;--rule:#2c2f35;--accent:#e0715c;--ok:#6fbd92;"
        "--track:#2c2f35;" + dark + "}}"
        "*{box-sizing:border-box}"
        "body{margin:0;background:var(--bg);color:var(--ink);"
        "font:14px/1.5 -apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif}"
        "header,main,footer{max-width:1000px;margin:0 auto;padding:0 20px}"
        "header{padding-top:28px}"
        "h1{font-size:20px;margin:0 0 4px}"
        "h2{font-size:15px;margin:0 0 10px;letter-spacing:.02em;text-transform:uppercase;color:var(--dim)}"
        "h3{font-size:14px;margin:0 0 8px}"
        "section{background:var(--panel);border:1px solid var(--rule);border-radius:8px;"
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
        ".account{flex:1 1 320px;min-width:0;border:1px solid var(--rule);border-radius:6px;padding:12px}"
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
    """
    stamp = time.time() if now is None else float(now)
    today = local_day_key(stamp)
    cells = collect_cells(
        history, rollups, today=today, days=max(WINDOW_DAYS), pricing=pricing
    )
    rows = tuple(accounts) + tuple(quota_rows)

    sections = [
        _section("Windows", _totals_section(cells, today)),
        _section("Tokens per day", _daily_section(cells, today)),
        _section("By model", _model_section(cells, today)),
        _section(
            "Model mix over time",
            _mix_section(cells, today),
            note="Share of each day's tokens, per vendor. Absolute volume is the chart above.",
        ),
        _section("By project", _project_section(history, today)),
        _section("Quota windows", _quota_section(rows)),
        _section("Hours at the wall", _wall_section(rows, stamp), note=WALL_NO_HISTORY_NOTE),
        _section("Unpriced volume", _unpriced_section(cells, today)),
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
) -> Path:
    """Render and write in one call. Returns the file written.

    The seam ``app`` uses: everything up to and including the disk write
    belongs on the worker thread, and only the hand-off to the browser is left
    to the caller.
    """
    document = render_dashboard(
        history, rollups, quota_rows, accounts, now=now, pricing=pricing, indexing=indexing
    )
    destination = (
        Path(path)
        if path is not None
        else dashboard_path_for(getattr(rollups, "path", None))
    )
    return write_dashboard(document, destination)
