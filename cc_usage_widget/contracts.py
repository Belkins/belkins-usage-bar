"""Shared data contracts for ``cc_usage_widget``.

This module is the **assembly invariant**: every other module in the package
(``app.py``, ``accounts.py``, ``indexer.py``, ``rollup.py``, ``pricing.py``,
``state.py``) is written against the shapes and protocols defined here, and
against nothing else. It has **no dependencies outside the standard library**
and imports nothing from this package, so it can never introduce an import
cycle and is always safe to import from a test.

Rules that hold across the whole package
----------------------------------------

1. **Percentages are 0-100 floats**, never 0.0-1.0 fractions. ``17.4`` means
   17.4%. ``None`` means "the API did not report this window".
2. **Money is a plain ``float`` of USD** and is always *notional* (API list
   prices against a flat-rate subscription). Any surface that shows it must
   also show :data:`NOTIONAL_LABEL`. See SPEC 4.3.
3. **Day buckets are local-time dates** formatted ``"YYYY-MM-DD"``
   (:data:`DAY_KEY_FORMAT`), so "today" matches what the user sees on their
   clock (SPEC 3.3 trap 4).
4. **Model keys stay raw in the rollup.** ``indexer.py`` buckets usage under
   the *verbatim* ``message.model`` string it read from the transcript; it does
   not know the price table. Canonicalisation happens later, in
   :class:`PricingTable`, when cost is computed. This keeps the hot loop free
   of pricing imports and preserves the exact unknown-model name for the menu
   (SPEC 3.3 trap 5).
5. **These frozen dataclasses are publish/boundary shapes, not hot-loop
   accumulators.** The per-tick scan touches millions of lines; allocating a
   frozen dataclass per line would blow the < 30 ms / < 10 MB budget of
   SPEC 2.1. The indexer must accumulate into plain mutable ``list[int]``
   counters (see :data:`COUNTER_FIELDS` and
   :meth:`ModelUsage.from_counters`) and materialise these objects **once**,
   at publish time.
6. **All I/O and parsing runs off the AppKit main thread** (SPEC 2.3). Every
   object here is immutable, which is what makes handing one to the main
   thread safe.
7. **Every usage figure carries a vendor** (SPEC-CODEX 4). ``"claude"`` and
   ``"codex"`` are separate token economies with separate price tables, and
   they must never be summed into one anonymous number. Storage carries the
   vendor *in the rollup key* (:data:`VendorModelKey`); the display and price
   shapes carry it as an explicit :data:`Vendor` field. See section 0 below
   for why the storage key is a composite string and how a legacy
   ``rollups.json`` full of bare Claude model keys migrates.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, Self, runtime_checkable

__all__ = [
    # type aliases
    "DayKey",
    "ModelKey",
    "Pct",
    "Usd",
    "Vendor",
    "VendorModelKey",
    # vendor dimension
    "VENDOR_CLAUDE",
    "VENDOR_CODEX",
    "VENDORS",
    "VENDOR_LABELS",
    "VENDOR_KEY_SEPARATOR",
    "vendor_label",
    "is_vendor",
    "make_vendor_key",
    "is_vendor_qualified",
    "split_vendor_key",
    "vendor_of_key",
    "raw_model_of_key",
    "normalize_model_key",
    "unknown_model_key",
    # token / rollup shapes
    "ModelUsage",
    "ZERO_USAGE",
    "COUNTER_FIELDS",
    "DayRollup",
    # scan-state shapes
    "FileScanState",
    "IndexProgress",
    "ScanResult",
    # cost shapes
    "ModelPrice",
    "ModelCostRow",
    "VendorCostRow",
    "WindowCost",
    "CostBreakdown",
    # account shapes
    "AccountRow",
    # seams
    "PricingTable",
    "TranscriptIndexer",
    "TranscriptSource",
    "RollupStore",
    "AccountSource",
    # settings
    "SETTINGS_DEFAULTS",
    "SETTINGS_BOUNDS",
    "normalize_settings",
    # constants
    "TITLE_ICON",
    "NOTIONAL_LABEL",
    "UNKNOWN_MODEL",
    "DAY_KEY_FORMAT",
    "CACHE_WRITE_5M_MULTIPLIER",
    "CACHE_WRITE_1H_MULTIPLIER",
    "CACHE_READ_MULTIPLIER",
    "ATTENTION_PCT",
    "STALE_USAGE_SECONDS",
    "WIDGET_HOME",
    "SETTINGS_PATH",
    "SCAN_STATE_PATH",
    "ROLLUPS_PATH",
    "PROJECTS_DIR",
    "CODEX_SCAN_STATE_PATH",
    "CODEX_SESSIONS_DIR",
    "CLAUDE_USAGE_PREFILTER",
    "CODEX_USAGE_PREFILTER",
    "CODEX_MODEL_PREFILTER",
    "CODEX_WINDOW_MINUTES_WEEKLY",
    # json glue
    "scan_state_to_json",
    "scan_state_from_json",
    "rollups_to_json",
    "rollups_from_json",
    # day helpers
    "local_day_key",
    "day_key_from_date",
    "parse_day_key",
    "day_keys_back",
    "local_day_key_from_iso",
    # formatters
    "format_usd",
    "format_tokens",
    "format_pct",
]

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

DayKey = str
"""A local calendar date as ``"YYYY-MM-DD"``. See :data:`DAY_KEY_FORMAT`."""

ModelKey = str
"""A model identifier.

Three flavours exist and must not be confused:

* **raw** - exactly the model string from the transcript, e.g.
  ``"claude-fable-5-20260514"`` or ``"gpt-5.6-sol"``. This is what the user
  sees; :attr:`ModelCostRow.raw_models` holds these.
* **canonical** - the price-table key, e.g. ``"claude-fable-5"``, or
  :data:`UNKNOWN_MODEL`. Produced by :meth:`PricingTable.canonical_model` and
  used as :attr:`ModelCostRow.model`. Bare: the vendor is a separate field.
* **vendor-qualified** (:data:`VendorModelKey`) - a raw or canonical key with
  its vendor prefixed, ``"codex:gpt-5.6-sol"``. This, and only this, is what
  keys :attr:`DayRollup.models` and the on-disk ``rollups.json``.
"""

Pct = float
"""A percentage in the range 0-100 (not 0-1)."""

Usd = float
"""Notional US dollars (API list prices, not a bill)."""

Vendor = Literal["claude", "codex"]
"""Which token economy a figure belongs to (SPEC-CODEX 4).

``"claude"`` - transcripts under :data:`PROJECTS_DIR`, accounts and quota from
``claude_swap``, cache rates *derived* from the input rate.
``"codex"`` - transcripts under :data:`CODEX_SESSIONS_DIR`, a read-only quota
pseudo-account built from the transcript's own ``rate_limits.primary``, cache
rates *published* per model.

Never infer a vendor from a model string. Two vendors can ship a model whose
name collides; the vendor is carried, not guessed.
"""

VendorModelKey = str
"""A rollup key carrying both halves of ``(vendor, model)``.

Canonical spelling, and the only one this module ever *writes*:

* :data:`VENDOR_CLAUDE` -> the **bare** model string, ``"claude-fable-5-20260514"``
* every other vendor -> ``"<vendor>:<raw model>"``, ``"codex:gpt-5.6-sol"``

**Why a composite string rather than a nested ``{vendor: {model: ...}}`` dict**

1. **Backward compatibility is structural, not a migration step.** The live
   ``rollups.json`` is ``{date: {model: {5 counters}}}`` with bare Claude
   keys - which is *already* the canonical form above. Nesting a vendor level
   would change the file's shape, forcing every reader to sniff "is this
   2-deep or 3-deep?" forever, and forcing a rewrite of 30 days of accumulated
   history on first launch. Here the existing file loads as-is and is
   attributed to claude, and :func:`normalize_model_key` is the identity on
   every key in it. Nothing to rewrite means nothing to get wrong.
2. **The default vendor is what the absent prefix encodes.** A key is
   explicitly qualified *iff* it starts with a known vendor literal followed
   by ``":"`` (:func:`is_vendor_qualified`); otherwise it is claude's. That is
   a decidable property of the string, not a heuristic that decays: no
   Anthropic model string can look qualified (they are ``claude-…`` with a
   hyphen, ``us.anthropic.claude-…``, or a Bedrock ARN starting ``arn:`` -
   whose ``arn`` is not a vendor literal), and no OpenAI one can either
   (``gpt-5.6-sol``). Every bare key on disk today was written by the Claude
   indexer, and reads back as claude's.
3. **The hot loop and every existing aggregate stay one dict deep.**
   :attr:`DayRollup.models`, ``merge``, ``prune``, ``total``,
   ``day_rollup_cost_rows`` and the indexer's ``dict[key, list[int]]``
   accumulator are unchanged - one lookup, one level of iteration. Nesting
   would double the loop nesting in the very module the SPEC 2.1 budget is
   measured on, for no gain.
4. **SPEC-CODEX 2.1 writes the key this way verbatim** ("bucket as
   ``codex:unknown``"), and :func:`unknown_model_key` reproduces both spellings
   exactly: ``"unknown"`` for claude, ``"codex:unknown"`` for codex.

The asymmetry is the point, and it is contained: :func:`make_vendor_key` is the
only way to build a key and :func:`split_vendor_key` the only way to read one,
so no call site ever writes the prefix by hand. Both are total and idempotent;
``normalize_model_key`` additionally collapses a redundant ``"claude:"``
spelling onto the bare one, so even a hand-edited file cannot produce two rows
for one model.

The residual cost is that the vendor must be split back out before display -
the menu shows ``gpt-5.6-sol``, never ``codex:gpt-5.6-sol``.
:func:`raw_model_of_key` is that call, and :attr:`ModelCostRow.raw_models` is
where it lands.
"""


# ---------------------------------------------------------------------------
# 0. The vendor dimension
# ---------------------------------------------------------------------------

VENDOR_CLAUDE: Final[Vendor] = "claude"
"""Claude Code: ``~/.claude/projects/**/*.jsonl``, ``claude_swap`` accounts."""

VENDOR_CODEX: Final[Vendor] = "codex"
"""Codex (OpenAI): ``~/.codex/sessions/**/rollout-*.jsonl``, no switching."""

VENDORS: Final[tuple[Vendor, ...]] = (VENDOR_CLAUDE, VENDOR_CODEX)
"""Every vendor, in menu order. Claude first - it owns the accounts section."""

VENDOR_LABELS: Final[Mapping[Vendor, str]] = {
    VENDOR_CLAUDE: "Claude",
    VENDOR_CODEX: "Codex",
}
"""Display labels. One definition so the title, the accounts section and the
cost section cannot disagree about capitalisation."""

VENDOR_KEY_SEPARATOR: Final[str] = ":"
"""Separator in a :data:`VendorModelKey`. Not configurable: it is baked into
the on-disk rollup keys, so changing it would orphan every stored day."""

_VENDOR_PREFIXES: Final[tuple[tuple[str, Vendor], ...]] = tuple(
    (f"{v}{VENDOR_KEY_SEPARATOR}", v) for v in VENDORS
)


def is_vendor(value: Any) -> bool:
    """True when *value* is one of the :data:`VENDORS` literals."""
    return isinstance(value, str) and value in VENDORS


def vendor_label(vendor: Vendor) -> str:
    """Display label for a vendor, e.g. ``"Codex"``.

    Falls back to the raw string for a vendor this build does not know, so a
    newer rollup file never renders a blank section.
    """
    return VENDOR_LABELS.get(vendor, str(vendor))


def make_vendor_key(vendor: Vendor, raw_model: str) -> VendorModelKey:
    """Build the canonical rollup key for ``(vendor, raw_model)``.

    ``("codex", "gpt-5.6-sol")`` -> ``"codex:gpt-5.6-sol"``.
    ``("claude", "claude-fable-5")`` -> ``"claude-fable-5"`` - claude is the
    default vendor and its keys stay bare, which is what makes the existing
    ``rollups.json`` already canonical (see :data:`VendorModelKey`).

    Idempotent in both directions: an already-qualified *raw_model* keeps its
    own prefix (its vendor wins over the *vendor* argument, since the string
    states it explicitly), and a redundant ``"claude:"`` prefix is collapsed
    away. So every ingress point can call this without tracking whether some
    earlier layer already did.

    Only the **first** separator is significant, so an exotic model string
    containing colons (a Bedrock ARN) round-trips through
    :func:`split_vendor_key` unharmed.
    """
    if is_vendor_qualified(raw_model):
        stated, bare = split_vendor_key(raw_model)
        return raw_model if stated != VENDOR_CLAUDE else bare
    if vendor == VENDOR_CLAUDE:
        return raw_model
    return f"{vendor}{VENDOR_KEY_SEPARATOR}{raw_model}"


def is_vendor_qualified(key: str) -> bool:
    """True when *key* already carries a known vendor prefix.

    Deliberately checks against the closed :data:`VENDORS` set rather than
    "contains a colon": a Bedrock ARN (``arn:aws:bedrock:…``) contains colons
    and is **not** vendor-qualified.
    """
    if not isinstance(key, str):
        return False
    return any(key.startswith(prefix) for prefix, _ in _VENDOR_PREFIXES)


def split_vendor_key(key: VendorModelKey) -> tuple[Vendor, str]:
    """Split into ``(vendor, raw_model)``.

    An unqualified key is attributed to :data:`VENDOR_CLAUDE` - the migration
    rule for every model key written before Codex support existed. Total:
    never raises.
    """
    if isinstance(key, str):
        for prefix, vendor in _VENDOR_PREFIXES:
            if key.startswith(prefix):
                return vendor, key[len(prefix) :]
    return VENDOR_CLAUDE, str(key)


def vendor_of_key(key: VendorModelKey) -> Vendor:
    """Vendor half of a rollup key; :data:`VENDOR_CLAUDE` when unqualified."""
    return split_vendor_key(key)[0]


def raw_model_of_key(key: VendorModelKey) -> str:
    """Model half of a rollup key - what the menu actually shows.

    ``"codex:gpt-5.6-sol"`` -> ``"gpt-5.6-sol"``. Use this anywhere a raw model
    name reaches the user or :attr:`CostBreakdown.unknown_models`; a user must
    never be shown the storage key.
    """
    return split_vendor_key(key)[1]


def normalize_model_key(key: str, *, default_vendor: Vendor = VENDOR_CLAUDE) -> VendorModelKey:
    """Canonicalise one model key. Total and idempotent.

    This is **the** migration function, and on a pre-Codex ``rollups.json`` it
    is the *identity*: a bare ``"claude-fable-5-20260514"`` is already the
    canonical spelling for claude, so the whole existing file loads unchanged
    and is attributed to :data:`VENDOR_CLAUDE` with nothing rewritten.

    What it does change is the redundant spelling: ``"claude:claude-fable-5"``
    collapses onto ``"claude-fable-5"``, so a key written by hand, or by a
    build that qualified everything, merges with the bare entry instead of
    becoming a second row for the same model. Non-claude keys pass through
    untouched.

    *default_vendor* applies only to an unqualified key, and exists so a
    non-claude producer can normalise its own bare output.
    """
    return make_vendor_key(default_vendor, key)


def unknown_model_key(vendor: Vendor = VENDOR_CLAUDE) -> VendorModelKey:
    """The per-vendor unknown bucket: ``"unknown"``, ``"codex:unknown"``.

    Per-vendor rather than global so a Codex model with no published rate
    (``codex-auto-review``) never shares a menu row - or a ``$0`` explanation -
    with an unrecognised Claude model (SPEC 3.3 trap 5, SPEC-CODEX 2.1/3).
    Claude's bucket is bare :data:`UNKNOWN_MODEL`, unchanged from before Codex
    existed.
    """
    return make_vendor_key(vendor, UNKNOWN_MODEL)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TITLE_ICON: Final[str] = "⇄"
"""The menu-bar glyph (SPEC 4.1). Keep it stable - the user finds the widget
by this glyph, so it must not change between releases or states."""

NOTIONAL_LABEL: Final[str] = "notional, API list prices"
"""Exact wording required next to any cost figure (SPEC 4.3 honesty rule)."""

UNKNOWN_MODEL: Final[ModelKey] = "unknown"
"""Canonical bucket for a model string the price table does not recognise.
Tokens are still counted; the price is ``$0``; the raw name is surfaced in the
menu via :attr:`CostBreakdown.unknown_models` (SPEC 3.3 trap 5)."""

DAY_KEY_FORMAT: Final[str] = "%Y-%m-%d"
"""``strftime``/``strptime`` format for a :data:`DayKey`."""

CACHE_WRITE_5M_MULTIPLIER: Final[float] = 1.25
"""5-minute cache-write price = base input price x this (SPEC 3.4)."""

CACHE_WRITE_1H_MULTIPLIER: Final[float] = 2.0
"""1-hour cache-write price = base input price x this (SPEC 3.4)."""

CACHE_READ_MULTIPLIER: Final[float] = 0.1
"""Cache-read price = base input price x this (SPEC 3.4)."""

ATTENTION_PCT: Final[Pct] = 100.0
"""At or above this percentage the UI appends ``(!)`` to a window
(SPEC 4.2 shows ``Fable 100% (!)``). Single definition so the title renderer
and the menu renderer cannot disagree."""

STALE_USAGE_SECONDS: Final[float] = 300.0
"""Display threshold only. When :attr:`AccountRow.usage_age_seconds` exceeds
this, the menu must show the age rather than implying live data
(SPEC 4.3). It never triggers an API call of our own."""

COUNTER_FIELDS: Final[tuple[str, ...]] = (
    "input",
    "output",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
)
"""Canonical order of the five token counters.

This order is the wire order of :meth:`ModelUsage.as_counters` /
:meth:`ModelUsage.from_counters` and therefore the layout of the mutable
``list[int]`` the indexer accumulates into. Do not reorder.
"""


def _env_path(var: str, default: Path) -> Path:
    """Return ``$var`` as a path if set and non-empty, else *default*.

    Exists so tests can redirect state and transcript roots without writing
    into the installed package.
    """
    raw = os.environ.get(var)
    return Path(raw).expanduser() if raw else default


WIDGET_HOME: Final[Path] = _env_path(
    # Project root, NOT the package dir: runtime state must not live inside the
    # importable package, where a reinstall would clobber the accumulated
    # rollup/scan state. SPEC 3 places both stores at the project root.
    "CC_USAGE_WIDGET_HOME", Path(__file__).resolve().parent.parent
)
"""Directory holding our persisted state. Defaults to the package directory,
matching SPEC 3's tree (``cc_usage_widget/settings.json``). Override with
``CC_USAGE_WIDGET_HOME``."""

SETTINGS_PATH: Final[Path] = WIDGET_HOME / "settings.json"
"""User preferences (SPEC 3). Created on first run from
:data:`SETTINGS_DEFAULTS`."""

SCAN_STATE_PATH: Final[Path] = WIDGET_HOME / "scan_state.json"
"""Per-file scan state (SPEC 3.2). Pure cache - safe to delete, costs one
re-index."""

ROLLUPS_PATH: Final[Path] = WIDGET_HOME / "rollups.json"
"""Daily token rollups (SPEC 2.2). Pure cache - safe to delete, costs one
re-index."""

PROJECTS_DIR: Final[Path] = _env_path(
    "CC_USAGE_WIDGET_PROJECTS_DIR", Path.home() / ".claude" / "projects"
)
"""Root of the transcript corpus scanned for ``**/*.jsonl`` (SPEC 1).
Override with ``CC_USAGE_WIDGET_PROJECTS_DIR`` to point tests at fixtures."""

CODEX_SESSIONS_DIR: Final[Path] = _env_path(
    "CC_USAGE_WIDGET_CODEX_SESSIONS_DIR", Path.home() / ".codex" / "sessions"
)
"""Root of the Codex corpus, scanned for ``**/rollout-*.jsonl``
(SPEC-CODEX 1: 15 GB / ~3,000 files, 90 touched in 24 h).

**Read-only.** Nothing in this package may write, move or truncate anything
under ``~/.codex``; our own state lives in :data:`WIDGET_HOME`. Its absence is
normal, not an error - a Claude-only user simply gets no Codex section
(SPEC-CODEX 5.5). Override with ``CC_USAGE_WIDGET_CODEX_SESSIONS_DIR`` to point
tests at a fixture tree.
"""

CODEX_SCAN_STATE_PATH: Final[Path] = WIDGET_HOME / "codex_scan_state.json"
"""Per-file scan state for the Codex corpus (SPEC-CODEX 4).

A **separate file** from :data:`SCAN_STATE_PATH`, deliberately:

* the live Claude ``scan_state.json`` (≈860 KB, ~3,200 entries) keeps its exact
  existing shape, so shipping Codex support cannot cost a Claude re-index;
* two indexers on two background threads never contend on one atomic
  temp-file + ``os.replace`` write;
* deleting one vendor's cache re-indexes only that vendor.

The two vendors still share **one** :data:`ROLLUPS_PATH`, because the rollup is
keyed by :data:`VendorModelKey` and the cost windows must span both vendors
(SPEC-CODEX 5.2).
"""

CLAUDE_USAGE_PREFILTER: Final[str] = '"usage"'
"""Substring every Claude usage line contains (SPEC 3.2 step 6).

Tested with ``in line`` *before* ``json.loads``; skips ~61% of parses.
"""

CODEX_USAGE_PREFILTER: Final[str] = '"token_count"'
"""Substring a Codex usage line contains (SPEC-CODEX 4)."""

CODEX_MODEL_PREFILTER: Final[str] = '"turn_context"'
"""Substring a Codex **model** line contains (SPEC-CODEX 2.1).

⚠️ Assembly trap. SPEC-CODEX 4 says "prefilter substring is ``token_count``",
but the model is *not* on the usage record - it is on a separate
``turn_context`` record. Prefiltering on ``token_count`` alone therefore drops
every record that carries the model, and the whole corpus attributes to
:func:`unknown_model_key` at ``$0``. The Codex line prefilter must accept
**either** substring::

    if CODEX_USAGE_PREFILTER not in line and CODEX_MODEL_PREFILTER not in line:
        continue

Two ``in`` tests on a line still cost far less than one ``json.loads``, so the
SPEC 2.1 budget is unaffected.
"""

CODEX_WINDOW_MINUTES_WEEKLY: Final[int] = 10_080
"""``rate_limits.primary.window_minutes`` for the weekly Codex window
(SPEC-CODEX 1). A primary window reporting this maps onto
:attr:`AccountRow.seven_day_pct`; any other width must **not** be silently
rendered as a weekly figure - surface it or drop it."""


# ---------------------------------------------------------------------------
# 1. ModelUsage - the five token counters
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """The five token counters for one model (SPEC 3.3).

    Field -> transcript source (top-level fields of ``message.usage`` only):

    ==================  =============================================================
    ``input``           ``usage.input_tokens``
    ``output``          ``usage.output_tokens``
    ``cache_write_5m``  ``usage.cache_creation.ephemeral_5m_input_tokens``
    ``cache_write_1h``  ``usage.cache_creation.ephemeral_1h_input_tokens``
    ``cache_read``      ``usage.cache_read_input_tokens``
    ==================  =============================================================

    Extraction traps the indexer must respect when filling these in:

    * ``usage.iterations`` is a per-attempt breakdown of the *same* request.
      Use the top-level fields only; summing both double-counts.
    * ``usage.cache_creation_input_tokens`` is the **sum** of the 5m and 1h
      sub-fields. Prefer the split fields; fall back to the flat field as
      ``cache_write_5m`` **only** when ``cache_creation`` is absent.
    * A missing or partial ``usage`` object means *skip the record* - never
      construct a zeroed ``ModelUsage`` to stand in for it.

    Immutable and hashable, so it is safe to pass to the AppKit main thread.
    """

    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0

    def __add__(self, other: ModelUsage) -> ModelUsage:
        """Field-wise sum, for accumulating two published snapshots."""
        if not isinstance(other, ModelUsage):
            return NotImplemented
        return ModelUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_write_5m=self.cache_write_5m + other.cache_write_5m,
            cache_write_1h=self.cache_write_1h + other.cache_write_1h,
            cache_read=self.cache_read + other.cache_read,
        )

    def __radd__(self, other: object) -> ModelUsage:
        """Support ``sum(iterable_of_usage)``, whose start value is ``0``."""
        if other is None or (isinstance(other, int) and not isinstance(other, bool) and other == 0):
            return self
        if isinstance(other, ModelUsage):
            return other.__add__(self)
        return NotImplemented

    @property
    def total_tokens(self) -> int:
        """Sum of all five counters - the ``N tok`` figure in the menu."""
        return (
            self.input
            + self.output
            + self.cache_write_5m
            + self.cache_write_1h
            + self.cache_read
        )

    @property
    def is_zero(self) -> bool:
        """True when every counter is zero (row should not be rendered)."""
        return self.total_tokens == 0

    def as_counters(self) -> tuple[int, int, int, int, int]:
        """Return the counters in :data:`COUNTER_FIELDS` order."""
        return (
            self.input,
            self.output,
            self.cache_write_5m,
            self.cache_write_1h,
            self.cache_read,
        )

    @classmethod
    def from_counters(cls, counters: Sequence[int]) -> Self:
        """Build from a 5-element sequence in :data:`COUNTER_FIELDS` order.

        This is the hot-loop bridge: the indexer keeps
        ``dict[ModelKey, list[int]]`` while scanning and calls this once per
        model at publish time.

        Raises:
            ValueError: if *counters* does not have exactly five elements.
        """
        if len(counters) != len(COUNTER_FIELDS):
            raise ValueError(
                f"expected {len(COUNTER_FIELDS)} counters, got {len(counters)}"
            )
        return cls(
            input=int(counters[0]),
            output=int(counters[1]),
            cache_write_5m=int(counters[2]),
            cache_write_1h=int(counters[3]),
            cache_read=int(counters[4]),
        )

    @classmethod
    def from_codex_last_token_usage(cls, usage: Any) -> ModelUsage | None:
        """Map one Codex turn onto the five counters, or ``None`` to skip.

        **This method is the assembly invariant for Codex cost.** The five
        counters were named for Anthropic's disjoint reporting; OpenAI reports
        an overlapping shape. If the indexer and the price table disagreed
        about which slot holds cached input, every Codex dollar would be
        silently wrong and no test in either module would notice. So the
        mapping is defined once, here, and both sides read it from here.

        Pass ``payload.info.last_token_usage`` (the **per-turn** block), never
        ``total_token_usage`` - SPEC-CODEX 2.2: ``total_*`` is cumulative *and*
        resets mid-session (measured: 252,100,617 summed per-turn vs a final
        ``total`` of 230,324,294), so it is neither a delta nor a total.

        Slot mapping, and why:

        ==================  ==========================================================
        ``input``           ``input_tokens - cached_input_tokens`` - **uncached**
                            input. SPEC-CODEX 2.3: OpenAI's ``cached_input_tokens``
                            is a *subset* of ``input_tokens``, so subtracting here
                            is what keeps the five counters disjoint, exactly as
                            Anthropic already reports them. Clamped at 0.
        ``cache_read``      ``cached_input_tokens`` - priced at the model's
                            **published** cached rate, not a derived multiple
                            (:attr:`ModelPrice.cached_input_usd_per_mtok`).
        ``cache_write_5m``  ``cache_write_input_tokens`` - priced at the standard
                            input rate (SPEC-CODEX 3), which is why a Codex
                            :class:`ModelPrice` sets ``cache_write_usd_per_mtok``
                            to its input rate rather than Claude's ``1.25x``.
        ``cache_write_1h``  Always 0 - OpenAI publishes no 1-hour cache tier.
        ``output``          ``output_tokens``. ``reasoning_output_tokens`` is a
                            *subset* of it and is **not** added (SPEC-CODEX 3).
        ==================  ==========================================================

        Consequence for display: for a Codex row, ``input`` reads as *uncached*
        input and ``input + cache_read`` reconstitutes OpenAI's
        ``input_tokens``. :attr:`total_tokens` is therefore already free of
        double counting and is directly comparable with a Claude row.

        Returns ``None`` when *usage* is not a mapping or carries none of the
        four fields - a malformed record is skipped, never counted as zeros
        (SPEC 3.3 trap 6).
        """
        if not isinstance(usage, Mapping):
            return None
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
        )
        if not any(key in usage for key in fields):
            return None
        try:
            raw_input = _as_int(usage.get("input_tokens", 0) or 0, "input_tokens")
            cached = _as_int(usage.get("cached_input_tokens", 0) or 0, "cached_input_tokens")
            written = _as_int(
                usage.get("cache_write_input_tokens", 0) or 0, "cache_write_input_tokens"
            )
            output = _as_int(usage.get("output_tokens", 0) or 0, "output_tokens")
        except (TypeError, ValueError):
            return None
        if raw_input < 0 or cached < 0 or written < 0 or output < 0:
            return None
        # Clamp rather than trust: a negative uncached figure would mean the
        # provider reported cached > input, and silently negative counters
        # would subtract real money from the day's total.
        return cls(
            input=max(0, raw_input - cached),
            output=output,
            cache_write_5m=written,
            cache_write_1h=0,
            cache_read=cached,
        )

    def to_json(self) -> dict[str, int]:
        """On-disk form: ``{"input": .., "output": .., ...}``."""
        return {
            "input": self.input,
            "output": self.output,
            "cache_write_5m": self.cache_write_5m,
            "cache_write_1h": self.cache_write_1h,
            "cache_read": self.cache_read,
        }

    @classmethod
    def from_json(cls, obj: Mapping[str, Any]) -> Self:
        """Inverse of :meth:`to_json`.

        Missing counters default to 0 (forward-compatible with a future sixth
        counter being added by a newer build).

        Raises:
            TypeError: if *obj* is not a mapping.
            ValueError: if a present counter is not an integer value.
        """
        if not isinstance(obj, Mapping):
            raise TypeError(f"ModelUsage.from_json expects a mapping, got {type(obj)!r}")
        return cls(
            input=_as_int(obj.get("input", 0), "input"),
            output=_as_int(obj.get("output", 0), "output"),
            cache_write_5m=_as_int(obj.get("cache_write_5m", 0), "cache_write_5m"),
            cache_write_1h=_as_int(obj.get("cache_write_1h", 0), "cache_write_1h"),
            cache_read=_as_int(obj.get("cache_read", 0), "cache_read"),
        )


ZERO_USAGE: Final[ModelUsage] = ModelUsage()
"""Shared all-zero instance. Use as an identity element; never as a stand-in
for a record whose ``usage`` was missing."""


def _as_int(value: Any, field_name: str) -> int:
    """Coerce a JSON value to ``int``, raising ``ValueError`` on junk."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name}: expected an int, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(f"{field_name}: expected an int, got {value!r}")


# ---------------------------------------------------------------------------
# 2. DayRollup - one local day's usage, keyed by raw model string
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayRollup:
    """All token usage attributed to one local calendar day.

    ``models`` is keyed by :data:`VendorModelKey`: a bare model string for
    claude (``"claude-fable-5-20260514"``), prefixed for anything else
    (``"codex:gpt-5.6-sol"``).

    **A bare key means claude**, so the pre-existing Claude indexer - which
    emits raw ``message.model`` strings and knows nothing about vendors - is
    already emitting canonical keys, and needs no change. The constructor
    stores what it is given verbatim; canonicalisation happens at every
    *ingress to storage*, where it is idempotent and cannot double-count:

    * :meth:`from_json` - disk load of a pre-Codex ``rollups.json`` (identity
      on every bare key in it);
    * :meth:`merged` - both sides, so two spellings of one model can never end
      up as two rows;
    * :meth:`RollupStore.merge` - must call :meth:`with_normalized_keys` on
      every incoming delta.

    Anything reading ``models`` must treat keys as :data:`VendorModelKey` -
    :func:`vendor_of_key` to attribute, :func:`raw_model_of_key` before showing
    a name to the user. Never render the storage key, and never assume a key is
    a bare model name.

    On-disk shape (SPEC 2.2) is unchanged at ``{date: {model: {5 counters}}}``.
    :meth:`to_json` serialises the model map *only* - the date is the key in
    the enclosing dict. Use :func:`rollups_to_json` / :func:`rollups_from_json`
    for the whole file.
    """

    day: DayKey
    models: Mapping[ModelKey, ModelUsage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Defensively copy *models* so callers cannot mutate a frozen value."""
        object.__setattr__(self, "models", dict(self.models))

    def __hash__(self) -> int:
        """Hash over the day plus the sorted model map.

        Defined explicitly because the auto-generated frozen ``__hash__``
        would raise on the ``dict`` field.
        """
        return hash((self.day, tuple(sorted(self.models.items()))))

    @property
    def total(self) -> ModelUsage:
        """Field-wise sum across every model in this day."""
        acc = [0, 0, 0, 0, 0]
        for usage in self.models.values():
            for i, value in enumerate(usage.as_counters()):
                acc[i] += value
        return ModelUsage.from_counters(acc)

    @property
    def total_tokens(self) -> int:
        """Total tokens across every model in this day."""
        return sum(u.total_tokens for u in self.models.values())

    @property
    def vendors(self) -> tuple[Vendor, ...]:
        """Vendors present in this day, in :data:`VENDORS` order.

        Drives the "Claude-only users see no Codex section" rule
        (SPEC-CODEX 5.5): an absent vendor is simply not listed.
        """
        seen = {vendor_of_key(key) for key in self.models}
        return tuple(v for v in VENDORS if v in seen)

    def with_normalized_keys(self, *, default_vendor: Vendor = VENDOR_CLAUDE) -> DayRollup:
        """Return this rollup with every key in canonical form. Idempotent.

        Unqualified keys are attributed to *default_vendor* (claude, per the
        migration rule) and a redundant ``"claude:"`` prefix is collapsed. If
        both spellings of the same model are present, their counters are
        **summed** rather than one shadowing the other - that collision is
        exactly the double-count this method exists to prevent.

        Returns ``self`` unchanged when every key is already canonical, which
        is the case for every all-Claude rollup, so the steady-state merge path
        allocates nothing.
        """
        if all(
            normalize_model_key(key, default_vendor=default_vendor) == key
            for key in self.models
        ):
            return self
        models: dict[VendorModelKey, ModelUsage] = {}
        for key, usage in self.models.items():
            normalized = normalize_model_key(str(key), default_vendor=default_vendor)
            existing = models.get(normalized)
            models[normalized] = usage if existing is None else existing + usage
        return DayRollup(day=self.day, models=models)

    def usage_for(self, model: VendorModelKey) -> ModelUsage:
        """Usage for one model key, or :data:`ZERO_USAGE` if absent.

        Accepts either spelling: an exact hit wins, otherwise the key is
        normalised and retried, so a caller holding a bare
        ``"claude-fable-5-…"`` still finds the migrated entry.
        """
        found = self.models.get(model)
        if found is not None:
            return found
        return self.models.get(normalize_model_key(str(model)), ZERO_USAGE)

    def usage_for_vendor(self, vendor: Vendor) -> ModelUsage:
        """Field-wise sum across every model belonging to *vendor*."""
        acc = [0, 0, 0, 0, 0]
        for key, usage in self.models.items():
            if vendor_of_key(key) != vendor:
                continue
            for i, value in enumerate(usage.as_counters()):
                acc[i] += value
        return ModelUsage.from_counters(acc)

    def merged(self, other: DayRollup) -> DayRollup:
        """Return a new rollup summing *self* and *other* per model.

        Both sides are key-normalised first, so merging a bare-keyed delta into
        a migrated on-disk day sums into one entry instead of producing two
        rows for the same model.

        Raises:
            ValueError: if the two rollups are for different days - silently
                merging across days would corrupt the day buckets.
        """
        if self.day != other.day:
            raise ValueError(f"cannot merge day {other.day!r} into day {self.day!r}")
        models: dict[VendorModelKey, ModelUsage] = dict(
            self.with_normalized_keys().models
        )
        for model, usage in other.with_normalized_keys().models.items():
            existing = models.get(model)
            models[model] = usage if existing is None else existing + usage
        return DayRollup(day=self.day, models=models)

    def to_json(self) -> dict[str, dict[str, int]]:
        """Serialise the model map only; the date is the enclosing key.

        Keys are written canonical, so a Claude-only file this build writes is
        byte-identical to what previous builds wrote, and the next load is a
        no-op either way.
        """
        return {
            model: usage.to_json()
            for model, usage in self.with_normalized_keys().models.items()
        }

    @classmethod
    def from_json(cls, day: DayKey, obj: Mapping[str, Any]) -> Self:
        """Inverse of :meth:`to_json`, given the *day* from the enclosing key.

        **This is the ``rollups.json`` migration**, and for a pre-Codex file it
        is a no-op by construction: every key is passed through
        :func:`normalize_model_key`, and a bare Claude model string is already
        the canonical form, so such a file loads with its usage attributed to
        :data:`VENDOR_CLAUDE` with nothing rewritten, no re-index and no data
        loss. The only key that actually changes is a redundant
        ``"claude:"``-prefixed one, which collapses onto its bare form and is
        summed into it rather than becoming a duplicate row.

        Raises:
            TypeError: if *obj* is not a mapping.
            ValueError: if *day* is not a valid :data:`DayKey`, or a counter
                block is malformed.
        """
        parse_day_key(day)  # validates, raises ValueError on junk
        if not isinstance(obj, Mapping):
            raise TypeError(f"DayRollup.from_json expects a mapping, got {type(obj)!r}")
        models: dict[VendorModelKey, ModelUsage] = {}
        for model, raw in obj.items():
            key = normalize_model_key(str(model))
            usage = ModelUsage.from_json(raw)
            existing = models.get(key)
            models[key] = usage if existing is None else existing + usage
        return cls(day=day, models=models)


# ---------------------------------------------------------------------------
# 3. FileScanState - per-transcript incremental scan bookkeeping
# ---------------------------------------------------------------------------


class _Keep:
    """Sentinel type for "leave this field as it was" (see
    :meth:`FileScanState.advanced`)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<KEEP>"


_KEEP: Final[_Keep] = _Keep()


@dataclass(frozen=True, slots=True)
class FileScanState:
    """Where the scanner left off in one transcript file (SPEC 3.2).

    On-disk, one entry per absolute path::

        {"<abs path>": {"inode": 12345, "size": 7364512,
                        "mtime": 1786956130.1, "offset": 7364512}}

    ``offset`` is the byte position **just past the last complete line** that
    was counted. It can legitimately be smaller than ``size`` when the file
    ended with a partial line that must not be advanced past (SPEC 3.2 step 5).

    ``last_model`` is the fifth, **optional** key, and exists solely for
    SPEC-CODEX 2.1: in a Codex rollout the model is not on the usage record but
    on a preceding ``turn_context`` record, so the extractor is stateful. When
    the next scan resumes mid-file at ``offset``, the ``turn_context`` that set
    the current model is *behind* the offset and will never be re-read; without
    persisting it, every turn after a resume attributes to
    :func:`unknown_model_key` at ``$0``. Claude never sets it (model and usage
    share one record there), and :meth:`to_json` **omits the key when it is
    ``None``**, so the live ~3,200-entry Claude ``scan_state.json`` keeps its
    exact current byte shape and shipping Codex costs no re-index.
    """

    inode: int
    size: int
    mtime: float
    offset: int
    last_model: str | None = None

    def unchanged(self, size: int, mtime: float) -> bool:
        """True when ``(size, mtime)`` match - the 3,162-of-~3,200 fast path.

        This is the single hottest predicate in the program (SPEC 3.2 step 2);
        a file answering True must not be opened.
        """
        return self.size == size and self.mtime == mtime

    def needs_reset(self, size: int, inode: int) -> bool:
        """True when the file was truncated or replaced.

        Either ``size`` fell below our stored ``offset`` or the inode changed,
        meaning the offset is meaningless and the file must be re-read from 0
        (SPEC 3.2 step 4).
        """
        return size < self.offset or inode != self.inode

    def advanced(
        self,
        *,
        inode: int,
        size: int,
        mtime: float,
        offset: int,
        last_model: str | None | _Keep = _KEEP,
    ) -> FileScanState:
        """Return a new state after reading up to *offset*.

        ``last_model`` defaults to **keeping** the stored value, so the
        pre-existing Claude call sites - which pass only the four original
        keyword arguments - cannot accidentally erase a Codex resume model.
        Pass it explicitly (including ``None``) to change it.
        """
        resolved = self.last_model if isinstance(last_model, _Keep) else last_model
        return FileScanState(
            inode=inode, size=size, mtime=mtime, offset=offset, last_model=resolved
        )

    def to_json(self) -> dict[str, float | int | str]:
        """On-disk form: the four keys from SPEC 3.2, plus ``last_model``
        **only when it is set**, so a Claude entry is byte-identical to what
        previous builds wrote."""
        out: dict[str, float | int | str] = {
            "inode": self.inode,
            "size": self.size,
            "mtime": self.mtime,
            "offset": self.offset,
        }
        if self.last_model is not None:
            out["last_model"] = self.last_model
        return out

    @classmethod
    def from_json(cls, obj: Mapping[str, Any]) -> Self:
        """Inverse of :meth:`to_json`.

        Raises on anything malformed. Callers (``state.py``) must catch and
        treat a bad entry as **absent**, which is correct-by-construction:
        the file simply gets re-read from offset 0.

        Raises:
            TypeError: if *obj* is not a mapping.
            ValueError: if a required key is missing or not numeric.
        """
        if not isinstance(obj, Mapping):
            raise TypeError(
                f"FileScanState.from_json expects a mapping, got {type(obj)!r}"
            )
        for key in ("inode", "size", "mtime", "offset"):
            if key not in obj:
                raise ValueError(f"FileScanState.from_json: missing key {key!r}")
        mtime = obj["mtime"]
        if isinstance(mtime, bool) or not isinstance(mtime, (int, float)):
            raise ValueError(f"FileScanState.from_json: bad mtime {mtime!r}")
        # Absent (every Claude entry, and every entry written before Codex
        # support) means "no remembered model", which is the correct starting
        # state for a file whose first turn_context has not been read yet.
        raw_model = obj.get("last_model")
        if raw_model is not None and not isinstance(raw_model, str):
            raise ValueError(f"FileScanState.from_json: bad last_model {raw_model!r}")
        return cls(
            inode=_as_int(obj["inode"], "inode"),
            size=_as_int(obj["size"], "size"),
            mtime=float(mtime),
            offset=_as_int(obj["offset"], "offset"),
            last_model=raw_model,
        )


@dataclass(frozen=True, slots=True)
class IndexProgress:
    """First-run / current-scan progress, for the honesty rule in SPEC 4.3.

    While ``complete`` is False the cost rows must render :meth:`label`
    (``indexing... n/N``) instead of a partial dollar figure that would read
    as a real total.
    """

    files_done: int = 0
    files_total: int = 0
    complete: bool = False
    scanning: bool = False
    last_scan_finished_at: float | None = None
    last_scan_duration_ms: float | None = None
    vendor: Vendor | None = None
    """Which source this progress describes; ``None`` means an aggregate over
    several sources (see :meth:`combined`). Defaults to ``None`` so every
    existing construction keeps its meaning."""

    def label(self) -> str:
        """``"indexing... 1,204/~3,200"`` while indexing, else ``""``."""
        if self.complete:
            return ""
        return f"indexing… {self.files_done:,}/{self.files_total:,}"

    def vendor_label(self) -> str:
        """:meth:`label` prefixed with the vendor when this is a single
        source, e.g. ``"Codex indexing… 90/~3,000"``.

        Returns ``""`` when complete, so a caller can render it unconditionally.
        """
        text = self.label()
        if not text or self.vendor is None:
            return text
        return f"{vendor_label(self.vendor)} {text}"

    @classmethod
    def combined(cls, parts: Iterable[IndexProgress]) -> IndexProgress:
        """Aggregate progress across N sources - the value app.py shows.

        ``complete`` is **all** of them (one still-indexing source keeps the
        cost figures behind SPEC 4.3's ``indexing…`` label, because a partial
        Codex index makes the *combined* total wrong just as surely as a
        partial Claude one). ``scanning`` is any of them. The file counts sum.
        ``vendor`` is ``None`` - the result describes no single source.

        An empty iterable yields a complete, idle progress, which is the right
        answer for a machine with no transcript corpora at all.
        """
        items = tuple(parts)
        if not items:
            return cls(complete=True)
        finished = [p.last_scan_finished_at for p in items if p.last_scan_finished_at]
        durations = [p.last_scan_duration_ms for p in items if p.last_scan_duration_ms]
        return cls(
            files_done=sum(p.files_done for p in items),
            files_total=sum(p.files_total for p in items),
            complete=all(p.complete for p in items),
            scanning=any(p.scanning for p in items),
            last_scan_finished_at=max(finished) if finished else None,
            last_scan_duration_ms=sum(durations) if durations else None,
            vendor=None,
        )

    @property
    def fraction(self) -> float:
        """Progress in 0.0-1.0; 1.0 when complete or when nothing to do."""
        if self.complete or self.files_total <= 0:
            return 1.0
        return min(1.0, self.files_done / self.files_total)


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What one incremental scan pass produced (SPEC 3.2).

    ``deltas`` holds **only newly counted usage**, one :class:`DayRollup` per
    affected day, to be handed to :meth:`RollupStore.merge`. It is a tuple
    rather than a mapping so the whole result stays hashable and ordered.

    The counters are not decoration: they are the evidence for the SPEC 2.1
    budget (``duration_ms``, ``files_read``, ``bytes_read``) and for the
    correctness traps (``records_duplicate``, ``records_malformed``).

    A scan must never raise because of one unreadable file; per-file failures
    are recorded in ``errors`` and the pass continues.
    """

    deltas: tuple[DayRollup, ...] = ()
    progress: IndexProgress = IndexProgress()
    files_seen: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_out_of_window: int = 0
    files_read: int = 0
    bytes_read: int = 0
    lines_seen: int = 0
    lines_parsed: int = 0
    records_counted: int = 0
    records_duplicate: int = 0
    records_malformed: int = 0
    unknown_models: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    duration_ms: float = 0.0
    vendor: Vendor = VENDOR_CLAUDE
    """Which source produced this pass. Defaults to :data:`VENDOR_CLAUDE` so
    the pre-existing indexer needs no change.

    ``unknown_models`` stays **raw** (``"codex-auto-review"``, not
    ``"codex:codex-auto-review"``) - a per-source result already carries its
    vendor here, and the strings are shown to the user verbatim."""

    @property
    def changed(self) -> bool:
        """True when anything new was counted - gates a UI refresh."""
        return bool(self.deltas)


# ---------------------------------------------------------------------------
# 4. Cost shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """One dated row of the price table (SPEC 3.4, SPEC-CODEX 3).

    Base input/output rates are per million tokens. The three cache rates come
    from **one of two shapes**, chosen by the vendor - the whole reason this
    type carries a vendor at all:

    *Derived* (Anthropic, :data:`VENDOR_CLAUDE`)
        Anthropic publishes no cache rates; they are documented multiples of
        the input rate: write-5m ``1.25x``, write-1h ``2.0x``, read ``0.1x``
        (:data:`CACHE_WRITE_5M_MULTIPLIER` and friends). Leave the two
        override fields ``None`` and the multipliers apply.

    *Published* (OpenAI, :data:`VENDOR_CODEX`)
        OpenAI publishes a real cached-input price per model - ``$0.50`` for
        ``gpt-5.6-sol`` against a ``$5.00`` input rate. That is ``0.1x`` for
        *this* model by coincidence and **must not be treated as a rule**: set
        :attr:`cached_input_usd_per_mtok` to the published number.
        ``cache_write_input_tokens`` is billed at the standard input rate
        (SPEC-CODEX 3), which :attr:`cache_write_usd_per_mtok` defaults to for
        a Codex row.

    Applying Claude's multipliers to an OpenAI model is not a rounding error,
    it is a fabricated price. A Codex row constructed without a published
    cached rate therefore **raises** rather than silently deriving one.

    ``effective_from`` / ``effective_until`` are **inclusive** bounds; ``None``
    means unbounded on that side. Sonnet 5's introductory row ends
    ``2026-08-31`` and its standard row begins ``2026-09-01``, so a record
    dated 2026-08-31 prices at the intro rate and 2026-09-01 at the standard
    rate. Resolution is by the **date of the usage record**, never by today.

    ``model`` is the **bare** canonical key (``"claude-fable-5"``,
    ``"gpt-5.6-sol"``), not a :data:`VendorModelKey`: the vendor lives in its
    own field. :attr:`vendor_key` composes the two when a storage key is
    needed.
    """

    model: ModelKey
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    effective_from: dt.date | None = None
    effective_until: dt.date | None = None
    vendor: Vendor = VENDOR_CLAUDE
    cached_input_usd_per_mtok: float | None = None
    """Published cache-**read** rate per Mtok. ``None`` = derive from input by
    :data:`CACHE_READ_MULTIPLIER` (Anthropic). Required for a Codex row."""
    cache_write_usd_per_mtok: float | None = None
    """Published cache-**write** rate per Mtok, applying to both write slots.
    ``None`` = derive by the 1.25x / 2.0x multipliers (Anthropic). Defaults to
    the input rate for a Codex row (SPEC-CODEX 3)."""

    def __post_init__(self) -> None:
        """Reject a Codex row that would fall back to Claude's multipliers.

        Also fills the Codex cache-write rate with the input rate, so the one
        published-but-unstated rule in SPEC-CODEX 3 cannot be forgotten at a
        call site.
        """
        if self.vendor != VENDOR_CODEX:
            return
        if self.cached_input_usd_per_mtok is None:
            raise ValueError(
                f"{self.model!r}: a {VENDOR_CODEX} price row must carry its published "
                "cached_input_usd_per_mtok; deriving it from the input rate with "
                "Claude's multiplier would invent a price"
            )
        if self.cache_write_usd_per_mtok is None:
            object.__setattr__(
                self, "cache_write_usd_per_mtok", self.input_usd_per_mtok
            )

    @property
    def vendor_key(self) -> VendorModelKey:
        """This row's canonical key as stored in a rollup."""
        return make_vendor_key(self.vendor, self.model)

    @property
    def has_published_cache_rates(self) -> bool:
        """True when the cache rates are published rather than derived."""
        return self.cached_input_usd_per_mtok is not None

    def covers(self, day: dt.date) -> bool:
        """True when this row is in effect on *day* (bounds inclusive)."""
        if self.effective_from is not None and day < self.effective_from:
            return False
        if self.effective_until is not None and day > self.effective_until:
            return False
        return True

    @property
    def cache_write_5m_usd_per_mtok(self) -> float:
        """5-minute cache-write rate: published override, else ``1.25x`` input."""
        if self.cache_write_usd_per_mtok is not None:
            return self.cache_write_usd_per_mtok
        return self.input_usd_per_mtok * CACHE_WRITE_5M_MULTIPLIER

    @property
    def cache_write_1h_usd_per_mtok(self) -> float:
        """1-hour cache-write rate: published override, else ``2.0x`` input.

        OpenAI has no 1-hour tier, and
        :meth:`ModelUsage.from_codex_last_token_usage` always leaves that
        counter at 0, so for a Codex row this rate is never actually applied.
        """
        if self.cache_write_usd_per_mtok is not None:
            return self.cache_write_usd_per_mtok
        return self.input_usd_per_mtok * CACHE_WRITE_1H_MULTIPLIER

    @property
    def cache_read_usd_per_mtok(self) -> float:
        """Cache-read rate: published cached-input price, else ``0.1x`` input."""
        if self.cached_input_usd_per_mtok is not None:
            return self.cached_input_usd_per_mtok
        return self.input_usd_per_mtok * CACHE_READ_MULTIPLIER

    def cost_usd(self, usage: ModelUsage) -> Usd:
        """Notional USD for *usage* at this row's rates.

        The one and only place the cost formula lives, so the hand-computed
        fixture in SPEC 6.7 has a single implementation to check.

        It is also, unchanged, the Codex formula from SPEC-CODEX 3 - because
        :meth:`ModelUsage.from_codex_last_token_usage` has already moved
        uncached input into ``input``, cached input into ``cache_read`` and
        cache writes into ``cache_write_5m``::

            (input_tokens - cached_input_tokens) * input_rate
            + cached_input_tokens               * cached_rate
            + cache_write_input_tokens          * input_rate
            + output_tokens                     * output_rate
        """
        return (
            usage.input * self.input_usd_per_mtok
            + usage.output * self.output_usd_per_mtok
            + usage.cache_write_5m * self.cache_write_5m_usd_per_mtok
            + usage.cache_write_1h * self.cache_write_1h_usd_per_mtok
            + usage.cache_read * self.cache_read_usd_per_mtok
        ) / 1_000_000.0


@dataclass(frozen=True, slots=True)
class ModelCostRow:
    """One ``by model`` line in the menu (SPEC 4.2, SPEC-CODEX 5.2).

    ``model`` is the **bare** canonical key (``"claude-fable-5"``,
    ``"gpt-5.6-sol"``, or :data:`UNKNOWN_MODEL`) and ``vendor`` is its own
    field, so the UI can label and group rows without ever parsing a string.
    ``vendor`` is appended with a :data:`VENDOR_CLAUDE` default rather than put
    first, so every existing construction of this row keeps working unchanged.

    ``raw_models`` lists every raw transcript string that collapsed into this
    row, and holds **bare** names (``"gpt-5.6-sol"``, not
    ``"codex:gpt-5.6-sol"``) - it is user-visible text, and it is what the menu
    shows for an unknown model so the user sees the actual unrecognised name.
    A producer reading vendor-qualified rollup keys must pass each through
    :func:`raw_model_of_key` first.

    Two vendors may legitimately produce two rows with the same ``model``;
    ``(vendor, model)`` is the identity, which :attr:`vendor_key` composes.
    """

    model: ModelKey
    display_name: str
    usage: ModelUsage
    usd: Usd
    is_unknown: bool = False
    raw_models: tuple[str, ...] = ()
    vendor: Vendor = VENDOR_CLAUDE

    @property
    def total_tokens(self) -> int:
        """Total tokens for this model."""
        return self.usage.total_tokens

    @property
    def vendor_key(self) -> VendorModelKey:
        """``(vendor, model)`` as the single rollup key it came from."""
        return make_vendor_key(self.vendor, self.model)

    @property
    def vendor_label(self) -> str:
        """Display label for this row's vendor, e.g. ``"Codex"``."""
        return vendor_label(self.vendor)


@dataclass(frozen=True, slots=True)
class WindowCost:
    """A ``Today`` / ``Last 7d`` / ``Last 30d`` aggregate row (SPEC 4.2).

    ``days_counted`` is the number of days in the window that actually had
    data. It is **informational only** and must not be used as an average
    divisor: SPEC 4.2's ``$86.10 / $12.30 per day`` divides by the fixed
    window length (7), so a quiet weekend still lowers the average.
    """

    label: str
    usd: Usd
    total_tokens: int
    window_days: int
    days_counted: int
    vendor_usd: tuple[tuple[Vendor, Usd], ...] = ()
    """Optional per-vendor split of :attr:`usd`, in :data:`VENDORS` order.

    ``usd`` always remains the **total across every vendor** (SPEC-CODEX 5.2:
    "cost totals span both vendors"); this is the breakdown behind it, so a
    consumer can show ``$12.40 (Claude $9.10 · Codex $3.30)`` without
    recomputing. Empty means "not split", never "zero" - a renderer must fall
    back to the single total rather than printing $0 for a vendor.
    """

    @property
    def usd_per_day(self) -> Usd:
        """USD divided by the fixed window length (not by ``days_counted``)."""
        if self.window_days <= 0:
            return 0.0
        return self.usd / self.window_days

    def usd_for_vendor(self, vendor: Vendor) -> Usd | None:
        """This window's USD for one vendor, or ``None`` when not split."""
        for name, amount in self.vendor_usd:
            if name == vendor:
                return amount
        return None


@dataclass(frozen=True, slots=True)
class VendorCostRow:
    """One vendor's subtotal for **today**, with its per-model rows.

    This is the grouping unit for SPEC-CODEX 5.1/5.2: the menu renders a
    Claude block and a Codex block, each with its own subtotal and its own
    model rows, under one cross-vendor ``Today`` figure.

    A vendor with no usage today must simply be **absent** from
    :attr:`CostBreakdown.by_vendor`, never present with zeros - that is what
    gives a Claude-only user no Codex section and a Codex-only user no Claude
    section (SPEC-CODEX 5.5) with no special-casing in the renderer.
    """

    vendor: Vendor
    usd: Usd
    usage: ModelUsage
    rows: tuple[ModelCostRow, ...] = ()

    @property
    def label(self) -> str:
        """Section heading, e.g. ``"Codex"``."""
        return vendor_label(self.vendor)

    @property
    def total_tokens(self) -> int:
        """Total tokens this vendor accounts for today."""
        return self.usage.total_tokens

    @property
    def unknown_models(self) -> tuple[str, ...]:
        """Raw names of this vendor's unpriced models, sorted and unique."""
        names: set[str] = set()
        for row in self.rows:
            if row.is_unknown:
                names.update(row.raw_models)
        return tuple(sorted(names))


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Everything the Cost section of the menu renders (SPEC 4.2).

    ``by_model`` describes **today** - in SPEC 4.2 the per-model rows sum
    exactly to the ``Today`` figure ($8.90 + $2.60 + $0.90 + $0.00 = $12.40) -
    and is ordered by descending ``usd``, with the unknown bucket last. It is
    the **flat, cross-vendor** list; every row carries its own
    :attr:`ModelCostRow.vendor`.

    ``by_vendor`` is the same rows grouped, one :class:`VendorCostRow` per
    vendor that actually used tokens today, in :data:`VENDORS` order. The two
    views must agree: the flat rows and the grouped rows hold the same usage,
    and both sum to ``today.usd`` (SPEC-CODEX 5.2). Only vendors present
    contribute a group, so an absent vendor renders no section at all
    (SPEC-CODEX 5.5).

    ``unknown_models`` stays flat and **raw** - the names the user must see,
    across both vendors. Use :attr:`VendorCostRow.unknown_models` when they
    need attributing to a section.

    Every consumer must pair these figures with :data:`NOTIONAL_LABEL`, and
    must render :meth:`IndexProgress.label` instead of the dollar figures
    while :attr:`is_partial` is True - which, with several sources, means
    while **any** source is still indexing (:meth:`IndexProgress.combined`).
    """

    today: WindowCost
    last_7d: WindowCost
    last_30d: WindowCost
    by_model: tuple[ModelCostRow, ...] = ()
    unknown_models: tuple[str, ...] = ()
    progress: IndexProgress = IndexProgress()
    generated_at: float = 0.0
    by_vendor: tuple[VendorCostRow, ...] = ()

    @property
    def last_7d_avg_per_day(self) -> Usd:
        """``Last 7d`` divided by 7 - the ``($12.30/day avg)`` figure."""
        return self.last_7d.usd_per_day

    @property
    def is_partial(self) -> bool:
        """True while the first index is still filling in (SPEC 4.3)."""
        return not self.progress.complete

    @property
    def vendors(self) -> tuple[Vendor, ...]:
        """Vendors with usage today, in :data:`VENDORS` order.

        Derived from ``by_vendor`` when it is populated, else from the flat
        rows, so it is correct for a producer that has only filled in one view.
        """
        if self.by_vendor:
            present = {row.vendor for row in self.by_vendor}
        else:
            present = {row.vendor for row in self.by_model}
        return tuple(v for v in VENDORS if v in present)

    def has_vendor(self, vendor: Vendor) -> bool:
        """True when *vendor* has any usage today - gates its menu section."""
        return vendor in self.vendors

    def rows_for_vendor(self, vendor: Vendor) -> tuple[ModelCostRow, ...]:
        """Today's per-model rows for one vendor, in the same menu order."""
        for group in self.by_vendor:
            if group.vendor == vendor:
                return group.rows
        return tuple(row for row in self.by_model if row.vendor == vendor)

    def vendor_row(self, vendor: Vendor) -> VendorCostRow | None:
        """The :class:`VendorCostRow` for *vendor*, or ``None`` if absent."""
        for group in self.by_vendor:
            if group.vendor == vendor:
                return group
        return None


# ---------------------------------------------------------------------------
# 5. AccountRow
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountRow:
    """One account line in the menu (SPEC 4.2), sourced from ``claude_swap``.

    All percentages are 0-100 floats; ``None`` means the API did not report
    that window. In particular ``seven_day_opus`` is ``null`` on these
    accounts, so there is **no** Opus percentage anywhere - Opus consumption
    is reported as tokens and cost only (SPEC 1).

    ``scoped_windows`` is a **list** of ``(name, pct)`` pairs. These accounts
    currently report exactly one (``("Fable", 3.0)``), but nothing may
    hardcode that: the title renderer abbreviates the **first** entry by its
    initial (``F3%``) and the menu renders them all.

    Reset strings are passed through **verbatim** from the API and are never
    recomputed by us (SPEC 4.3).

    **Pseudo-accounts (SPEC-CODEX 4).** Codex has no ``claude_swap``
    equivalent: no switching, no autoswitch, no aliases. Its quota is still a
    row in this menu, built from the newest ``rate_limits.primary`` seen in the
    transcripts, so it is modelled as an :class:`AccountRow` with
    ``vendor=VENDOR_CODEX`` and ``switchable=False`` rather than as a second,
    parallel row type - the renderer then has one shape to lay out.

    ``switchable`` is the *only* thing that makes a row clickable. A renderer
    must gate the switch action on it, not on the vendor: "Codex" is a fact
    about where the numbers came from, "read-only" is a fact about what the
    user may do, and a future vendor could be either. Likewise
    ``AccountSource.switch_to`` must refuse a non-switchable row.

    Conventions for a Codex pseudo-account:

    * ``slot`` ``0`` - real claude-swap slots start at 1, so a pseudo-account
      sorts before or after them deterministically without colliding;
    * ``alias`` the vendor label, ``email`` ``""`` (there is no account);
    * ``is_active`` ``False`` - "active" means "the account claude-swap would
      route to", which is meaningless here and must not render the ``>`` marker;
    * the weekly ``primary`` window (``window_minutes`` =
      :data:`CODEX_WINDOW_MINUTES_WEEKLY`) maps onto ``seven_day_pct`` /
      ``seven_day_resets_at``; there is no 5-hour window, so ``five_hour_pct``
      stays ``None`` and renders as an em dash, never ``0%``;
    * ``resets_at`` arrives as an **epoch number**, not the API's formatted
      string, so it must be formatted once on the way in and then treated as
      verbatim like every other reset string;
    * ``pace_ahead`` stays empty - pace comes from ``claude_swap.pace``, and we
      do not derive burn rates ourselves (SPEC 3.1).
    """

    slot: int
    alias: str
    email: str
    is_active: bool
    five_hour_pct: Pct | None = None
    seven_day_pct: Pct | None = None
    scoped_windows: tuple[tuple[str, Pct], ...] = ()
    five_hour_resets_at: str | None = None
    seven_day_resets_at: str | None = None
    scoped_resets_at: tuple[tuple[str, str], ...] = ()
    usage_age_seconds: float | None = None
    pace_ahead: tuple[tuple[str, bool], ...] = ()
    """``(window_key, is_ahead_of_pace)`` for the WEEKLY windows only.

    Key is ``"seven_day"`` or a scoped window's reported name (e.g. ``"Fable"``).
    Pace is meaningless for the 5-hour rolling window, so it is never present
    for it. Computed by ``claude_swap.pace.compute_pace`` — we do not derive
    burn rates ourselves. A window absent from this tuple simply has no pace
    verdict (too soon after a reset, or not computable), and renders no note.
    """
    vendor: Vendor = VENDOR_CLAUDE
    """Which vendor this row's quota belongs to."""
    switchable: bool = True
    """False = a **read-only pseudo-account**: render it, never offer to switch
    to it. Defaults to True so every existing claude-swap row is unchanged."""
    plan_type: str | None = None
    """Vendor-reported plan, e.g. Codex's ``"pro"`` (SPEC-CODEX 1). Passed
    through verbatim; ``None`` when the source does not report one."""

    @property
    def is_pseudo(self) -> bool:
        """True when this row is informational only (nothing to switch to)."""
        return not self.switchable

    @property
    def vendor_label(self) -> str:
        """Display label for this row's vendor, e.g. ``"Codex"``."""
        return vendor_label(self.vendor)

    @property
    def primary_scoped_window(self) -> tuple[str, Pct] | None:
        """First scoped window, or ``None``. What the title renders."""
        return self.scoped_windows[0] if self.scoped_windows else None

    def scoped_pct(self, name: str) -> Pct | None:
        """Percentage for a named scoped window, or ``None`` if not reported."""
        for window_name, pct in self.scoped_windows:
            if window_name == name:
                return pct
        return None

    def scoped_abbrev(self, name: str) -> str:
        """Title abbreviation for a scoped window: its first character.

        ``"Fable"`` -> ``"F"``. Derived, never hardcoded, so a second scoped
        window appearing tomorrow needs no code change.
        """
        return name[:1].upper()

    @property
    def max_pct(self) -> Pct | None:
        """Highest reported percentage across all windows, or ``None``."""
        values = [
            pct
            for pct in (
                self.five_hour_pct,
                self.seven_day_pct,
                *(p for _, p in self.scoped_windows),
            )
            if pct is not None
        ]
        return max(values) if values else None

    @property
    def needs_attention(self) -> bool:
        """True when any window is at or above :data:`ATTENTION_PCT`."""
        highest = self.max_pct
        return highest is not None and highest >= ATTENTION_PCT

    @property
    def usage_is_stale(self) -> bool:
        """True when the usage read is old enough that its age must be shown."""
        return (
            self.usage_age_seconds is not None
            and self.usage_age_seconds > STALE_USAGE_SECONDS
        )


# ---------------------------------------------------------------------------
# 6. The four seams
# ---------------------------------------------------------------------------


@runtime_checkable
class PricingTable(Protocol):
    """``pricing.py`` - dated model prices and the cost formula (SPEC 3.4).

    Implementations must be **pure and thread-safe**: called from the
    background scan thread and from tests, never mutated after construction.
    They must resolve a price by the *record's* date, never by today.

    **Vendor rules** (SPEC-CODEX 3), which keep the signatures below unchanged:

    * Every ``raw_model`` argument accepts **either** spelling - a bare
      ``"gpt-5.6-sol"`` or a rollup key ``"codex:gpt-5.6-sol"``. An
      implementation splits it with :func:`split_vendor_key` and looks the
      model up *within that vendor*, so an unqualified string keeps resolving
      against the Claude table exactly as before.
    * A model is only ever priced by **its own vendor's** rows. Two vendors
      shipping a same-named model must not cross-resolve; neither may an
      OpenAI model inherit Claude's derived cache multipliers - see
      :class:`ModelPrice`.
    * Unknown stays unknown: :data:`UNKNOWN_MODEL` bare, or
      :func:`unknown_model_key` when a storage key is wanted. ``codex-auto-review``
      has no published rate and takes this path - tokens counted, ``$0``, name
      surfaced (SPEC-CODEX 3).
    * Anything the table hands back for display - ``display_name``,
      ``unknown_models`` - carries the **raw** model name, never the storage
      key. Run keys through :func:`raw_model_of_key` on the way out.
    """

    def canonical_model(self, raw_model: str) -> ModelKey:
        """Map a raw ``message.model`` string to a price-table key.

        Accepts a bare or vendor-qualified string; returns the **bare**
        canonical key (the vendor is carried alongside, in
        :attr:`ModelCostRow.vendor` / :attr:`ModelPrice.vendor`).

        Returns :data:`UNKNOWN_MODEL` when the string is not recognised.
        Must be total: never raises, never returns an empty string.
        """
        ...

    def is_known(self, raw_model: str) -> bool:
        """True when :meth:`canonical_model` resolves to a priced model."""
        ...

    def display_name(self, raw_model: str) -> str:
        """Human label for the menu, e.g. ``"Fable 5"``.

        For an unknown model, returns the raw string itself so the user sees
        the actual unrecognised name (SPEC 3.3 trap 5).
        """
        ...

    def price_for(self, raw_model: str, day: dt.date) -> ModelPrice | None:
        """Price row in effect for *raw_model* on *day*, or ``None``.

        ``None`` means unknown model or no row covering that date; callers
        must then price at ``$0`` and surface the model name - never fall back
        to another model's rate.
        """
        ...

    def cost_usd(self, raw_model: str, usage: ModelUsage, day: dt.date) -> Usd:
        """Notional USD for *usage* of *raw_model* incurred on *day*.

        Returns ``0.0`` for an unknown or unpriced model.
        """
        ...


@runtime_checkable
class TranscriptIndexer(Protocol):
    """``indexer.py`` - the performance-critical incremental scanner (SPEC 3.2).

    Contract obligations:

    * **Background thread only.** Never called from the AppKit main thread.
    * **Never** ``read()``/``readlines()`` a transcript; ``seek(offset)`` then
      iterate lines, discarding a trailing partial line.
    * Pre-filter on ``(size, mtime)`` and on the lookback window before
      opening anything; a tick where nothing changed must open no files.
    * Substring-test ``'"usage"' in line`` before ``json.loads``.
    * Dedup on ``requestId`` (else message ``id``) for the **current local
      day only**; drop the set at day rollover.
    * Bucket by **raw** model string and by **local** date.
    * Survive per-file errors: record them in :attr:`ScanResult.errors`.
    """

    def scan_once(self, *, deadline: float | None = None) -> ScanResult:
        """Run one incremental pass and return the newly counted usage.

        Args:
            deadline: optional ``time.monotonic()`` value at which to stop
                early and return partial progress. This is how the first-run
                index stays chunked and yields between files (SPEC 3.2); the
                next call resumes where this one stopped.
        """
        ...

    def progress(self) -> IndexProgress:
        """Current progress snapshot. Cheap; safe to call every UI tick."""
        ...

    def reset(self) -> None:
        """Discard all scan state, forcing a full re-index of the window."""
        ...


@runtime_checkable
class TranscriptSource(Protocol):
    """One vendor's corpus, as ``app.py`` sees it (SPEC-CODEX 4).

    :class:`TranscriptIndexer` plus the identity and quota bits needed to drive
    **N sources in a loop** instead of special-casing Codex::

        for source in self.sources:                 # claude, codex, …
            if not source.available():
                continue                            # absent ~/.codex is normal
            result = source.scan_once(deadline=deadline)
            store.merge(result.deltas)              # keys carry the vendor
        progress = IndexProgress.combined(s.progress() for s in self.sources)
        rows = [*account_source.rows(), *(r for s in self.sources
                                          for r in s.quota_rows())]

    Every existing obligation of :class:`TranscriptIndexer` still holds -
    background thread only, never ``read()`` a transcript, ``(size, mtime)``
    pre-filter, substring pre-filter before ``json.loads``, dedup, survive
    per-file errors. Adding a third vendor later means adding a source to that
    list and a price table, and touching nothing else.

    Additional obligations:

    * :meth:`scan_once` must stamp :attr:`ScanResult.vendor` with
      :attr:`vendor`, and emit deltas keyed by :data:`VendorModelKey` for that
      vendor (a bare key is tolerated and read as claude, so only the Codex
      source is actually obliged to qualify - but qualifying is idempotent and
      preferred).
    * :meth:`progress` must stamp :attr:`IndexProgress.vendor` likewise.
    * :meth:`available` must be **cheap and non-throwing**; it is called every
      tick and a missing corpus is a normal state, not an error.
    * A source that reads a vendor's quota from its own transcripts returns it
      from :meth:`quota_rows`; one that has no quota of its own (Claude, whose
      quota comes from :class:`AccountSource`) returns ``()``.

    Note: this is a *data* protocol - ``isinstance`` works, ``issubclass``
    raises ``TypeError``, as for any protocol with non-method members.
    """

    @property
    def vendor(self) -> Vendor:
        """Which vendor this source produces. Constant for the object's life."""
        ...

    @property
    def root(self) -> Path:
        """Corpus root, e.g. :data:`PROJECTS_DIR` or
        :data:`CODEX_SESSIONS_DIR`. Shown in Settings; never written to."""
        ...

    def available(self) -> bool:
        """True when this vendor's corpus exists and is readable.

        False is a normal state - a Claude-only machine has no ``~/.codex``,
        and a Codex-only machine has no ``~/.claude/projects`` (SPEC-CODEX
        5.5). Callers must skip an unavailable source silently: no error row,
        no empty section, no zeroed cost.
        """
        ...

    def scan_once(self, *, deadline: float | None = None) -> ScanResult:
        """One incremental pass; see :meth:`TranscriptIndexer.scan_once`."""
        ...

    def progress(self) -> IndexProgress:
        """Progress snapshot stamped with :attr:`vendor`. Cheap."""
        ...

    def reset(self) -> None:
        """Discard this source's scan state only, leaving other vendors' alone."""
        ...

    def quota_rows(self) -> tuple[AccountRow, ...]:
        """Read-only quota rows this source can build from its own corpus.

        Codex returns one pseudo-account from the newest
        ``payload.rate_limits.primary`` it has seen - ``used_percent`` onto
        :attr:`AccountRow.seven_day_pct`, ``resets_at`` (epoch) formatted onto
        ``seven_day_resets_at``, ``plan_type`` passed through - with
        ``switchable=False``. Claude returns ``()``: its accounts come from
        :class:`AccountSource`, which is the single source of truth for them
        and must not be duplicated here.

        Must not do a scan of its own; it reports what the last scan learned.
        """
        ...


@runtime_checkable
class RollupStore(Protocol):
    """``rollup.py`` - the daily aggregate store plus cost math (SPEC 2.2).

    The only persistent aggregate in the program:
    ``{date: {model: {5 counters}}}``, bounded by the lookback window, a few
    kilobytes in total. Writes are atomic (temp file + ``os.replace``).

    Two *optional* capabilities are discovered by name rather than required
    here, so a minimal store still satisfies the protocol:

    ``clear()``
        Drop every day. Its presence is what makes ``Rebuild cost index``
        appear at all - rebuilding without emptying would double-count.
    ``drop_vendors(vendors) -> int``
        Drop only those vendors' rows (matched with :func:`vendor_of_key`),
        deleting any day left with no models, and return how many rows went.
        This is the per-vendor half of the lost-scan-state cure: the two
        vendors share one store but not one accounting fate, so losing
        :data:`CODEX_SCAN_STATE_PATH` must re-index Codex only. Without it the
        owner falls back to ``clear()`` and re-reads everything, which is
        correct but destroys any day whose transcripts have since aged off
        disk.
    """

    def load(self) -> None:
        """Load from disk. A missing or corrupt file means "start empty" -
        the store is a pure cache and is rebuildable by re-indexing."""
        ...

    def save(self) -> None:
        """Persist atomically (temp file + ``os.replace``)."""
        ...

    def get(self, day: DayKey) -> DayRollup | None:
        """Rollup for one local day, or ``None`` if that day has no data."""
        ...

    def days(self) -> tuple[DayKey, ...]:
        """Every day held, sorted ascending."""
        ...

    def merge(self, rollups: Iterable[DayRollup]) -> None:
        """Add scanned deltas (:attr:`ScanResult.deltas`) into the store.

        Per-model, per-day field-wise addition. Must be idempotent with
        respect to the indexer's dedup guarantee: the indexer never emits the
        same request twice, so ``merge`` simply adds.

        **Must call** :meth:`DayRollup.with_normalized_keys` on every incoming
        delta before adding it. Deltas may still arrive with bare model keys
        while the on-disk days are vendor-qualified; without normalising here,
        the same model would accumulate under two keys and the day would
        double-count. The call is idempotent and allocation-free when the keys
        are already qualified.
        """
        ...

    def prune(self, *, today: DayKey, keep_days: int) -> None:
        """Drop days older than the ``keep_days``-long window ending *today*."""
        ...

    def cost_breakdown(
        self,
        pricing: PricingTable,
        *,
        today: DayKey,
        progress: IndexProgress,
    ) -> CostBreakdown:
        """Compute every figure the Cost menu section renders.

        Each day is priced with the row in effect **on that day**, so the
        Sonnet 5 intro->standard rollover leaves historical days correct.
        ``by_model`` covers *today* only, ordered by descending USD with the
        unknown bucket last. *progress* is threaded through verbatim so the
        UI can honour the partial-index rule in SPEC 4.3 - with several
        sources it is :meth:`IndexProgress.combined` over all of them.

        Window totals span **every** vendor in the store (SPEC-CODEX 5.2),
        with the per-vendor split in :attr:`WindowCost.vendor_usd` and the
        grouped rows in :attr:`CostBreakdown.by_vendor`. A vendor with no
        usage contributes no group and no split entry, so it renders no
        section (SPEC-CODEX 5.5).
        """
        ...


@runtime_checkable
class AccountSource(Protocol):
    """``accounts.py`` - a thin adapter over ``claude_swap`` (SPEC 3.1).

    Reuse, do not reimplement: rows come from ``switcher.accounts_snapshot()``
    (backed by claude-swap's paced usage store, so **no API calls of our
    own**), autoswitch from ``claude_swap.autoswitch.AutoSwitchEngine``
    sharing ``~/.claude-swap-backup/autoswitch_state.json``, switching from
    ``claude_swap.switcher``.

    ``claude_swap``'s ``autoswitch.*`` settings remain the single source of
    truth for the toggle; our ``settings.json`` key is only a first-run
    default and a fallback when claude-swap reports nothing.

    Every method here may do I/O and therefore runs on the background thread.
    """

    def rows(self) -> tuple[AccountRow, ...]:
        """All accounts, ordered by :attr:`AccountRow.slot` ascending."""
        ...

    def active(self) -> AccountRow | None:
        """The active account, or ``None`` if none is active."""
        ...

    def refresh(self, *, force: bool = False) -> None:
        """Re-read claude-swap's usage store.

        ``force=False`` respects claude-swap's own pacing. ``force=True``
        backs the explicit ``Refresh now`` menu item.
        """
        ...

    def switch_to(self, slot_or_alias: str) -> bool:
        """Switch the active account. Returns True on success.

        Must return ``False`` for anything that is not a switchable
        claude-swap account - a Codex pseudo-account
        (:attr:`AccountRow.switchable` False) is read-only and there is
        nothing to switch to (SPEC-CODEX 4).
        """
        ...

    def autoswitch_enabled(self) -> bool:
        """Whether the autoswitch engine is enabled (claude-swap's answer)."""
        ...

    def set_autoswitch_enabled(self, enabled: bool) -> None:
        """Enable/disable autoswitch, writing through to claude-swap so
        ``cswap config set autoswitch.*`` and our toggle stay in agreement."""
        ...

    def evaluate_autoswitch(self) -> str | None:
        """Run one autoswitch evaluation at claude-swap's cadence.

        Returns the alias switched to, or ``None`` if no switch happened.
        Must be a no-op returning ``None`` when the toggle is off.
        """
        ...


# ---------------------------------------------------------------------------
# 7. settings.json schema
# ---------------------------------------------------------------------------

SETTINGS_DEFAULTS: dict[str, Any] = {
    # --- feature switches (top-level menu items, per SPEC 4.2) -------------
    "autoswitch_enabled": False,
    "cost_tracking_enabled": True,
    # --- vendors (SPEC-CODEX) ---------------------------------------------
    "codex_tracking_enabled": True,
    # --- scan / cadence (SPEC 3.5); all intervals live here ---------------
    "lookback_days": 30,
    "ui_interval_seconds": 60,
    "cost_interval_seconds": 300,
    # --- title components (SPEC 4.1: "<icon> podol 17% F3% $12/d") --------
    "title_show_icon": True,
    "title_show_alias": True,
    "title_show_five_hour_pct": True,
    "title_show_scoped_pct": True,
    "title_show_cost": True,
    "title_show_codex_pct": False,
}
"""Full ``settings.json`` schema with defaults.

Treat as read-only; :func:`normalize_settings` returns a fresh dict.

The ``title_show_*`` keys are exactly the components of SPEC 4.1's title, each
individually toggleable. ``autoswitch_enabled`` defaults to ``False`` so a
fresh install never starts switching accounts unasked; :class:`AccountSource`
remains the runtime source of truth for it.

Vendor keys (SPEC-CODEX):

``codex_tracking_enabled``
    Whether to construct and scan the Codex :class:`TranscriptSource` at all.
    Defaults ``True``, which costs nothing on a machine without ``~/.codex``:
    :meth:`TranscriptSource.available` returns False and the source is skipped.
``title_show_codex_pct``
    Defaults ``False``. The title is already five components wide and the menu
    bar is finite; the Codex weekly percentage is opt-in, and the Codex section
    in the menu is *not* gated on it.

**These keys must be declared here to exist.** :func:`normalize_settings`
drops unknown keys, so a vendor setting added only in ``app.py`` would be
silently discarded on the next save.
"""

SETTINGS_BOUNDS: dict[str, tuple[int, int]] = {
    "lookback_days": (1, 365),
    "ui_interval_seconds": (15, 3600),
    "cost_interval_seconds": (30, 86_400),
}
"""Inclusive clamps for the integer settings.

Floors exist to protect the SPEC 2.1 idle-CPU budget: a 1-second cost tick
would defeat the whole design.
"""


def normalize_settings(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a complete, type-correct settings dict.

    Fills missing keys from :data:`SETTINGS_DEFAULTS`, drops unknown keys,
    coerces bools and ints, and clamps ints to :data:`SETTINGS_BOUNDS`. A junk
    value falls back to its default rather than raising - a hand-edited
    ``settings.json`` must never prevent the widget from starting.

    Args:
        raw: parsed ``settings.json`` contents, or ``None`` on first run.

    Returns:
        A fresh dict with exactly the keys of :data:`SETTINGS_DEFAULTS`.
    """
    out: dict[str, Any] = dict(SETTINGS_DEFAULTS)
    if not raw or not isinstance(raw, Mapping):
        return out
    for key, default in SETTINGS_DEFAULTS.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(default, bool):
            if isinstance(value, bool):
                out[key] = value
            elif isinstance(value, int):
                out[key] = bool(value)
            continue
        if isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            low, high = SETTINGS_BOUNDS[key]
            out[key] = min(max(int(value), low), high)
    return out


# ---------------------------------------------------------------------------
# 8. Whole-file JSON glue
# ---------------------------------------------------------------------------


def scan_state_to_json(
    states: Mapping[str, FileScanState],
) -> dict[str, dict[str, float | int]]:
    """Serialise the scan-state file: ``{abs path: entry}`` (SPEC 3.2)."""
    return {path: state.to_json() for path, state in states.items()}


def scan_state_from_json(obj: Any) -> dict[str, FileScanState]:
    """Parse the scan-state file, **skipping** malformed entries.

    Skipping is safe by construction: a dropped entry re-reads that file from
    offset 0, which is exactly the truncation path. A non-mapping top level
    yields an empty dict (full re-index).
    """
    if not isinstance(obj, Mapping):
        return {}
    out: dict[str, FileScanState] = {}
    for path, raw in obj.items():
        try:
            out[str(path)] = FileScanState.from_json(raw)
        except (TypeError, ValueError):
            continue
    return out


def rollups_to_json(
    rollups: Mapping[DayKey, DayRollup] | Iterable[DayRollup],
) -> dict[str, dict[str, dict[str, int]]]:
    """Serialise the rollup file: ``{date: {model: {5 counters}}}`` (SPEC 2.2).

    Accepts either a day-keyed mapping or an iterable of :class:`DayRollup`.
    Keys are emitted sorted so the file is diff-stable.
    """
    if isinstance(rollups, Mapping):
        items = [rollups[day] for day in sorted(rollups)]
    else:
        items = sorted(rollups, key=lambda r: r.day)
    return {rollup.day: rollup.to_json() for rollup in items}


def rollups_from_json(obj: Any) -> dict[DayKey, DayRollup]:
    """Parse the rollup file, **skipping** malformed days.

    The rollup file is a pure cache, so tolerating partial corruption (rather
    than crashing at launch) is correct; the affected days simply read as
    empty until re-indexed.
    """
    if not isinstance(obj, Mapping):
        return {}
    out: dict[DayKey, DayRollup] = {}
    for day, raw in obj.items():
        try:
            rollup = DayRollup.from_json(str(day), raw)
        except (TypeError, ValueError):
            continue
        out[rollup.day] = rollup
    return out


# ---------------------------------------------------------------------------
# 9. Day-key helpers (local time everywhere)
# ---------------------------------------------------------------------------


def local_day_key(epoch_seconds: float) -> DayKey:
    """Local-time :data:`DayKey` for a POSIX timestamp.

    Used for the file-mtime fallback when a record has no usable timestamp.
    """
    return dt.datetime.fromtimestamp(epoch_seconds).strftime(DAY_KEY_FORMAT)


def day_key_from_date(day: dt.date) -> DayKey:
    """Format a ``date`` as a :data:`DayKey`."""
    return day.strftime(DAY_KEY_FORMAT)


def parse_day_key(key: DayKey) -> dt.date:
    """Parse a :data:`DayKey` into a ``date``.

    Raises:
        ValueError: if *key* is not a ``YYYY-MM-DD`` string.
    """
    if not isinstance(key, str):
        raise ValueError(f"bad day key {key!r}")
    return dt.datetime.strptime(key, DAY_KEY_FORMAT).date()


def day_keys_back(today: DayKey, days: int) -> tuple[DayKey, ...]:
    """The *days*-long window **ending on and including** *today*.

    ``day_keys_back("2026-08-17", 7)`` returns 2026-08-11 .. 2026-08-17.
    Inclusive-of-today is what makes SPEC 4.2 consistent: ``Last 7d $86.10``
    with a ``$12.30/day`` average divides by 7.

    Returns keys in ascending order. ``days <= 0`` returns ``()``.
    """
    if days <= 0:
        return ()
    end = parse_day_key(today)
    start = end - dt.timedelta(days=days - 1)
    return tuple(
        day_key_from_date(start + dt.timedelta(days=i)) for i in range(days)
    )


def local_day_key_from_iso(timestamp: str | None) -> DayKey | None:
    """Convert a transcript ISO-8601 ``timestamp`` to a **local** day key.

    Transcripts write UTC with a trailing ``Z``; a value without offset info
    is treated as UTC. The result is the local date, so day buckets match the
    user's clock (SPEC 3.3 trap 4).

    Returns ``None`` when *timestamp* is missing or unparseable; the caller
    must then fall back to :func:`local_day_key` on the file's mtime rather
    than dropping the record.
    """
    if not timestamp or not isinstance(timestamp, str):
        return None
    text = timestamp.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone().strftime(DAY_KEY_FORMAT)


# ---------------------------------------------------------------------------
# 10. Shared formatters (one definition, so title and menu cannot disagree)
# ---------------------------------------------------------------------------


def format_usd(value: Usd) -> str:
    """``12.4`` -> ``"$12.40"``. Always two decimals, thousands separated."""
    return f"${value:,.2f}"


def format_tokens(count: int) -> str:
    """``41_200_000`` -> ``"41.2M"``. The menu appends ``" tok"``."""
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:.1f}K"
    if count < 1_000_000_000:
        return f"{count / 1_000_000:.1f}M"
    return f"{count / 1_000_000_000:.1f}B"


def format_pct(value: Pct | None) -> str:
    """``17.4`` -> ``"17%"``; ``None`` -> ``"—"`` (an em dash).

    ``None`` renders as a dash rather than ``0%`` because "not reported" is
    not the same as "none used" - notably ``seven_day_opus`` (SPEC 1).
    """
    if value is None:
        return "—"
    return f"{round(value):d}%"
