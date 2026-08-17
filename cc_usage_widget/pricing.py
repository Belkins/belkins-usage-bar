"""Dated, **vendor-aware** model price table and exact cost math (SPEC 3.4,
SPEC-CODEX 3).

This module owns two things and nothing else:

1. **What a model costs, on a given day.** Prices are stored as *base
   input/output rates per million tokens*, each row carrying inclusive
   ``effective_from`` / ``effective_until`` bounds. The three cache rates come
   from one of two shapes, chosen by the row's **vendor** - see
   `Two cache-rate shapes`_.

2. **Exact money arithmetic.** Every figure is computed in **integer
   picodollars** (1 USD = 10**12 pico) and only converted to ``Decimal`` /
   ``float`` at the boundary. A 30-day sum is therefore an integer sum: no
   float error accumulates across days or models. See `Money units`_.

.. _Two cache-rate shapes:

Two cache-rate shapes - derived (Anthropic) vs published (OpenAI)
-----------------------------------------------------------------

*Derived* (:data:`~contracts.VENDOR_CLAUDE`)
    Anthropic publishes no cache prices, so the three cache rates are
    **derived** from the base input rate by the multipliers frozen in
    ``contracts`` (5m write ``1.25x``, 1h write ``2.0x``, read ``0.1x``). A
    cache rate can therefore never drift out of step with its base rate.

*Published* (:data:`~contracts.VENDOR_CODEX`)
    OpenAI publishes a real cached-input price per model. For
    ``gpt-5.6-sol`` that is ``$0.50`` against a ``$5.00`` input rate. It is
    ``0.1x`` for that model *by coincidence*, and treating the coincidence as
    a rule would be inventing a price - so an OpenAI row stores its published
    cached rate verbatim and Claude's multipliers are never applied to it.
    ``cache_write_input_tokens`` bills at the standard input rate
    (SPEC-CODEX 3), which is what an OpenAI row's cache-write rate is set to.

:meth:`TokenRates.from_usd_per_mtok` is the single place that choice is made,
and :func:`_openai_row` cannot be called without a published cached rate, so
there is no path by which an OpenAI model acquires a Claude multiplier.

The **vendor is carried, never inferred from the model string.** A rollup key
is a :data:`~contracts.VendorModelKey` (bare for claude, ``"codex:<model>"``
otherwise) and a model is only ever priced against its **own vendor's** rows -
see `Cross-vendor resolution`_.

.. _OpenAI rate history:

OpenAI rate history - why only current rates ship
--------------------------------------------------

Public reporting says OpenAI cut the **standard** rates for ``gpt-5.6-terra``
and ``gpt-5.6-luna`` on **2026-07-30**. :data:`OPENAI_PRICE_ROWS` nonetheless
ships **only the current published rates**, with no ``effective_from`` bound:
we have an authoritative table for what those models cost *today*
(developers.openai.com/api/docs/pricing) and **no authoritative dated table for
what they cost before the cut**. Inventing the pre-cut numbers to fill a dated
row would be exactly the fabricated price this module exists to prevent, and it
would silently mis-state every historical day in the 30-day window.

The consequence is stated plainly rather than hidden: usage of Terra or Luna
from **before 2026-07-30 is priced at the post-cut rate**, so those historical
days read low. The machinery to fix it is already here and needs no code
change - when OpenAI publishes its rate history, add a bounded row per rate::

    _openai_row("gpt-5.6-terra", "<pre-cut in>", "<pre-cut cached>",
                "<pre-cut out>", effective_until="2026-07-29"),
    _openai_row("gpt-5.6-terra", "2.00", "0.20", "12.00",
                effective_from="2026-07-30"),

which is the identical shape to the Sonnet 5 pair below, and is resolved by the
same dated lookup.

Resolution is **by the date of the usage record**, never by today
-------------------------------------------------------------------

Sonnet 5 ships an introductory rate ($2/$10 per Mtok) through **2026-08-31**
and its standard rate ($3/$15) from **2026-09-01**. Both bounds are inclusive,
so a record dated 2026-08-31 prices at intro and one dated 2026-09-01 prices at
standard - regardless of when the widget process started or what "today" is.
Every entry point here takes the record's day explicitly; there is deliberately
no "current price" accessor to reach for by accident. That single bug is the
reason this module exists as its own file (SPEC 3.4).

Unknown models are never priced at another model's rate
-------------------------------------------------------

:meth:`ModelPricing.canonical_model` is total: an unrecognised
``message.model`` string resolves to :data:`~contracts.UNKNOWN_MODEL`, prices
at ``$0``, and keeps its **raw** name available for the menu via
:meth:`ModelPricing.display_name`, :meth:`ModelPricing.resolve` (whose
:class:`PriceResolution` always carries a zero-rate :class:`ModelPrice`, never
``None``) and :meth:`ModelPricing.unknown_raw_models`. Prefix matching is
deliberately strict - the remainder after a canonical key must look like a
snapshot date, an ``@date``, a ``vN[:N]`` provider suffix, a context-window
marker or nothing at all - so a future ``claude-fable-5-1`` is reported as
unknown rather than silently billed at Fable 5's rate (SPEC 3.3 trap 5).

``codex-auto-review`` takes exactly this path (SPEC-CODEX 3): OpenAI publishes
no rate for it, so it is **not** priced at ``gpt-5.6-sol``'s rate or any other
sibling's. Its tokens are counted, it costs ``$0``, and the literal string
``codex-auto-review`` reaches the menu. Note it does *not* collide with the
``codex:`` vendor prefix - :func:`~contracts.is_vendor_qualified` tests for the
separator, and ``codex-auto-review`` has a hyphen there.

.. _Cross-vendor resolution:

Cross-vendor resolution - the rule that keeps vendors from bleeding
--------------------------------------------------------------------

Every lookup accepts either spelling of a model string, and resolves in at most
two steps:

1. **Within the string's own vendor.** :func:`~contracts.split_vendor_key`
   names it - ``"codex:gpt-5.6-sol"`` is codex's, and an unqualified string is
   claude's, which is why every pre-Codex call site keeps resolving against the
   Claude table byte-for-byte as before.
2. **Only for an unqualified string that missed**, a fallback to another
   vendor - and only when **exactly one** other vendor claims it. So a bare
   ``"gpt-5.6-sol"`` (say, from a hand-written test or a rollup key that
   predates qualification) still prices correctly, while a name two vendors
   both ship stays unresolved rather than being priced at a coin-flip.

An **explicitly qualified** string never falls back. ``"codex:claude-fable-5"``
is unknown and costs ``$0``; it does not borrow Fable 5's rate. That asymmetry
is the whole safety property: the prefix is an assertion about which economy
the tokens were spent in, and honouring it is what makes two same-named models
from two vendors impossible to confuse.

Purity / threading
------------------

:class:`ModelPricing` satisfies the :class:`contracts.PricingTable` protocol and
honours its contract: it is immutable after construction (the only mutable
member is a bounded memo dict whose entries are pure functions of their key),
does no I/O, and is safe to share between the background scan thread and the
AppKit main thread (SPEC 2.3).

.. _Money units:

Money units
-----------

======================================  ==============================================
``cost_picos*`` / ``*_cost_picos``      exact ``int`` picodollars (1 USD = 10**12)
``cost_for`` / ``*_cost``               exact ``Decimal`` USD (no rounding applied)
``cost_usd`` / ``*_cost_usd``           ``float`` USD - the ``contracts`` boundary type
``picos_to_cents``                      ``int`` cents, ROUND_HALF_UP (display only)
======================================  ==============================================

Aggregate in picodollars, convert once at the end. The float accessors exist
because :class:`contracts.PricingTable`, :class:`contracts.ModelCostRow` and
:class:`contracts.WindowCost` are typed in ``float``; they are computed from the
integer path, so they are as accurate as a float can be.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from functools import lru_cache
from typing import Final

from .contracts import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    UNKNOWN_MODEL,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    VENDORS,
    DayKey,
    DayRollup,
    ModelCostRow,
    ModelKey,
    ModelPrice,
    ModelUsage,
    Usd,
    Vendor,
    VendorModelKey,
    is_vendor_qualified,
    make_vendor_key,
    parse_day_key,
    raw_model_of_key,
    split_vendor_key,
    unknown_model_key,
    vendor_label,
)

__all__ = [
    # units
    "PICOS_PER_USD",
    "PICOS_PER_TOKEN_PER_USD_PER_MTOK",
    "picos_to_usd",
    "picos_to_float",
    "picos_to_cents",
    # table shapes
    "TokenRates",
    "PriceRow",
    "PriceResolution",
    "ModelPricing",
    # aliases some callers may reach for
    "PriceTable",
    "Pricing",
    # data
    "PRICE_ROWS",
    "CLAUDE_PRICE_ROWS",
    "OPENAI_PRICE_ROWS",
    "MODEL_DISPLAY_NAMES",
    "SONNET_5_INTRO_LAST_DAY",
    "SONNET_5_STANDARD_FIRST_DAY",
    "OPENAI_TERRA_LUNA_RATE_CUT_DAY",
    # default instance + module-level convenience wrappers
    "DEFAULT_PRICING",
    "PRICING",
    "default_pricing",
    "canonical_model",
    "canonical_key",
    "vendor_for",
    "is_known",
    "display_name",
    "context_window_marker",
    "price_for",
    "resolve",
    "cost_for",
    "cost_usd",
    "cost_picos",
    "day_rollup_cost_picos",
    "day_rollup_cost",
    "day_rollup_cost_usd",
    "day_rollup_cost_rows",
    "day_rollup_unknown_models",
    "window_cost_picos",
    "window_cost",
    "window_cost_usd",
]


# ---------------------------------------------------------------------------
# 1. Money units
# ---------------------------------------------------------------------------

PICOS_PER_USD: Final[int] = 1_000_000_000_000
"""Picodollars in one US dollar. The internal accumulator unit."""

PICOS_PER_TOKEN_PER_USD_PER_MTOK: Final[int] = PICOS_PER_USD // 1_000_000
"""Scale factor: ``$1.00`` per million tokens == ``1_000_000`` pico per token.

Chosen so that every rate in :data:`PRICE_ROWS`, and every cache rate derived
from it, is an **exact integer** number of picodollars per token:
``$2.00/Mtok`` -> ``2_000_000`` pico/token; ``x1.25`` -> ``2_500_000``;
``x0.1`` -> ``200_000``. Six decimal places of headroom on the stored rate,
which is five more than any published price uses.
"""

_CENT: Final[Decimal] = Decimal("0.01")


def picos_to_usd(picos: int) -> Decimal:
    """Exact ``Decimal`` USD for an integer picodollar amount.

    Exact because it is a decimal point shift, not a division:
    ``1_234_500_000_000`` -> ``Decimal("1.234500000000")``.
    """
    return Decimal(int(picos)).scaleb(-12)


def picos_to_float(picos: int) -> Usd:
    """``float`` USD for an integer picodollar amount (boundary conversion)."""
    return float(picos_to_usd(picos))


def picos_to_cents(picos: int) -> int:
    """Whole cents, ROUND_HALF_UP - for display only, never for accumulation."""
    return int(picos_to_usd(picos).quantize(_CENT, rounding=ROUND_HALF_UP) * 100)


def _rate_to_picos_per_token(usd_per_mtok: Decimal) -> int:
    """Convert a USD-per-million-tokens rate to integer pico-per-token.

    Raises:
        ValueError: if the rate has more precision than the pico scale can hold
            exactly (i.e. more than six decimal places). Loud by design: a
            silently rounded price table is exactly the class of bug this
            module exists to prevent.
    """
    scaled = usd_per_mtok * PICOS_PER_TOKEN_PER_USD_PER_MTOK
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"rate {usd_per_mtok} USD/Mtok is not representable in picodollars "
            "per token without rounding"
        )
    return int(scaled)


def _derive_picos(base_picos: int, multiplier: float) -> int:
    """Derive a cache rate from a base rate by one of the frozen multipliers.

    ``Decimal(str(multiplier))`` is used so ``0.1`` means one tenth, not the
    binary double nearest to it. The documented multipliers (1.25, 2.0, 0.1)
    are exact at the pico scale, so ROUND_HALF_EVEN never actually fires here;
    it is present only so a hypothetical future multiplier cannot raise inside
    the price table at import time.
    """
    scaled = Decimal(base_picos) * Decimal(str(multiplier))
    return int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))


# ---------------------------------------------------------------------------
# 2. Rates + rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenRates:
    """The five per-token rates, in **integer picodollars per token**.

    Built once per :class:`PriceRow`. For an Anthropic row the three cache
    rates are derived from ``input`` and are never stored independently, so
    they cannot disagree with the base rate (SPEC 3.4). For an OpenAI row they
    are the **published** figures, passed in explicitly (SPEC-CODEX 3).
    """

    input: int
    output: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int

    @classmethod
    def from_usd_per_mtok(
        cls,
        input_usd: Decimal,
        output_usd: Decimal,
        *,
        cached_input_usd: Decimal | None = None,
        cache_write_usd: Decimal | None = None,
    ) -> TokenRates:
        """Build from the stored base rates.

        **This is the one place the derived-vs-published choice is made**, so
        there is a single line to audit for "did an OpenAI model just get
        Claude's multiplier?".

        With both keyword rates left ``None`` this is the pre-Codex behaviour
        exactly: the three cache rates derive from ``input_usd`` by
        :data:`~contracts.CACHE_WRITE_5M_MULTIPLIER` and friends (Anthropic
        publishes no cache prices, so a multiple of the base rate is the only
        honest figure available).

        Args:
            cached_input_usd: **published** cache-read rate per Mtok. Supplying
                it suppresses the ``0.1x`` derivation entirely - it is not a
                default to be adjusted, it is the vendor's own number. OpenAI's
                ``$0.50`` against a ``$5.00`` input rate happens to be ``0.1x``
                for ``gpt-5.6-sol``; ``gpt-5.4-mini``'s ``$0.075`` against
                ``$0.75`` happens to be too. Neither makes it a rule, and the
                moment OpenAI ships a model where it is not, a derived table
                would be quietly wrong with nothing to notice it.
            cache_write_usd: **published** cache-write rate per Mtok, applied
                to both write slots. For OpenAI this is the standard input rate
                (SPEC-CODEX 3), which is emphatically not Claude's ``1.25x`` /
                ``2.0x``. OpenAI publishes no 1-hour tier at all, and
                :meth:`~contracts.ModelUsage.from_codex_last_token_usage`
                always leaves that counter at 0, so the 1h slot is priced here
                only so the field is never undefined - it is multiplied by zero
                in practice.
        """
        base = _rate_to_picos_per_token(input_usd)
        if cache_write_usd is None:
            write_5m = _derive_picos(base, CACHE_WRITE_5M_MULTIPLIER)
            write_1h = _derive_picos(base, CACHE_WRITE_1H_MULTIPLIER)
        else:
            write_5m = write_1h = _rate_to_picos_per_token(cache_write_usd)
        if cached_input_usd is None:
            read = _derive_picos(base, CACHE_READ_MULTIPLIER)
        else:
            read = _rate_to_picos_per_token(cached_input_usd)
        return cls(
            input=base,
            output=_rate_to_picos_per_token(output_usd),
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
            cache_read=read,
        )

    def cost_picos(self, usage: ModelUsage) -> int:
        """Exact integer picodollars for *usage* at these rates.

        Pure integer arithmetic - the one multiplication-and-sum that all cost
        figures in the program flow through.
        """
        return (
            usage.input * self.input
            + usage.output * self.output
            + usage.cache_write_5m * self.cache_write_5m
            + usage.cache_write_1h * self.cache_write_1h
            + usage.cache_read * self.cache_read
        )


ZERO_RATES: Final[TokenRates] = TokenRates(0, 0, 0, 0, 0)
"""All-zero rates - what an unknown model prices at (never another model's)."""


def _zero_price(vendor: Vendor = VENDOR_CLAUDE) -> ModelPrice:
    """The zero-rate :class:`contracts.ModelPrice` an unknown model resolves to.

    Vendor-aware for a reason that would otherwise be a crash rather than a
    wrong number: :meth:`contracts.ModelPrice.__post_init__` **rejects** a
    ``codex`` row whose ``cached_input_usd_per_mtok`` is ``None``, since a
    missing published rate would mean falling back to Claude's ``0.1x``. So a
    non-Claude zero price must state its zero cache rates explicitly. They are
    genuinely zero, not derived-from-zero, which is the honest reading anyway.

    The Claude branch is left constructing exactly what it did before Codex
    existed, so nothing about the pre-existing unknown path shifts.
    """
    if vendor == VENDOR_CLAUDE:
        return ModelPrice(
            model=UNKNOWN_MODEL,
            input_usd_per_mtok=0.0,
            output_usd_per_mtok=0.0,
        )
    return ModelPrice(
        model=UNKNOWN_MODEL,
        input_usd_per_mtok=0.0,
        output_usd_per_mtok=0.0,
        vendor=vendor,
        cached_input_usd_per_mtok=0.0,
        cache_write_usd_per_mtok=0.0,
    )


@dataclass(frozen=True, slots=True)
class PriceRow:
    """One dated row of the price table, for one ``(vendor, model)``.

    Mirrors :class:`contracts.ModelPrice` but stores the base rates as
    ``Decimal`` and carries the precomputed integer :class:`TokenRates`, so the
    exact-money path never touches a float. Use :meth:`as_model_price` to hand
    the contract-typed (float) view to anything outside this module.

    ``model`` is the **bare** canonical key (``"claude-fable-5"``,
    ``"gpt-5.6-sol"``); the vendor is its own field, never parsed back out of
    the name. :attr:`vendor_key` composes the two into the storage key.

    ``effective_from`` / ``effective_until`` are **inclusive**; ``None`` means
    unbounded on that side.

    The two published-rate fields are ``None`` for an Anthropic row (cache
    rates derive from ``input_usd_per_mtok``) and set for an OpenAI one. They
    are appended last so every pre-Codex positional construction is unchanged.
    """

    model: ModelKey
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    effective_from: dt.date | None = None
    effective_until: dt.date | None = None
    rates: TokenRates = ZERO_RATES
    vendor: Vendor = VENDOR_CLAUDE
    cached_input_usd_per_mtok: Decimal | None = None
    cache_write_usd_per_mtok: Decimal | None = None

    @property
    def vendor_key(self) -> VendorModelKey:
        """This row's canonical storage key, e.g. ``"codex:gpt-5.6-sol"``."""
        return make_vendor_key(self.vendor, self.model)

    @property
    def has_published_cache_rates(self) -> bool:
        """True when the cache rates are published rather than derived."""
        return self.cached_input_usd_per_mtok is not None

    def covers(self, day: dt.date) -> bool:
        """True when this row is in effect on *day* (both bounds inclusive)."""
        if self.effective_from is not None and day < self.effective_from:
            return False
        if self.effective_until is not None and day > self.effective_until:
            return False
        return True

    def as_model_price(self) -> ModelPrice:
        """The :class:`contracts.ModelPrice` (float) view of this row.

        The published cache rates cross over explicitly. That matters for more
        than fidelity: :meth:`contracts.ModelPrice.__post_init__` **raises** for
        a ``codex`` row whose ``cached_input_usd_per_mtok`` is ``None``, so
        dropping them here would not quietly mis-price - it would fail loudly
        at the first Codex lookup.
        """
        return ModelPrice(
            model=self.model,
            input_usd_per_mtok=float(self.input_usd_per_mtok),
            output_usd_per_mtok=float(self.output_usd_per_mtok),
            effective_from=self.effective_from,
            effective_until=self.effective_until,
            vendor=self.vendor,
            cached_input_usd_per_mtok=(
                None
                if self.cached_input_usd_per_mtok is None
                else float(self.cached_input_usd_per_mtok)
            ),
            cache_write_usd_per_mtok=(
                None
                if self.cache_write_usd_per_mtok is None
                else float(self.cache_write_usd_per_mtok)
            ),
        )


def _row(
    model: str,
    input_usd: str,
    output_usd: str,
    *,
    effective_from: str | None = None,
    effective_until: str | None = None,
) -> PriceRow:
    """Build an **Anthropic** :class:`PriceRow` from string literals.

    ``Decimal`` from a string literal, never a float, so ``"0.075"`` is
    seven-and-a-half hundredths and not the binary double nearest to it. Cache
    rates derive from the input rate. Use :func:`_openai_row` for a vendor that
    publishes its own.
    """
    input_dec = Decimal(input_usd)
    output_dec = Decimal(output_usd)
    return PriceRow(
        model=model,
        input_usd_per_mtok=input_dec,
        output_usd_per_mtok=output_dec,
        effective_from=parse_day_key(effective_from) if effective_from else None,
        effective_until=parse_day_key(effective_until) if effective_until else None,
        rates=TokenRates.from_usd_per_mtok(input_dec, output_dec),
        vendor=VENDOR_CLAUDE,
    )


def _openai_row(
    model: str,
    input_usd: str,
    cached_input_usd: str,
    output_usd: str,
    *,
    effective_from: str | None = None,
    effective_until: str | None = None,
    cache_write_usd: str | None = None,
) -> PriceRow:
    """Build an **OpenAI** :class:`PriceRow` with its *published* cache rates.

    The cached-input rate is a required positional argument, sitting between
    input and output exactly as it does in OpenAI's published table and in
    SPEC-CODEX 3. That is deliberate: there is no way to add an OpenAI model to
    this table without stating its cached rate, and therefore no way for one to
    silently inherit Claude's ``0.1x`` derivation. Forgetting it is a
    ``TypeError`` at import, not a wrong number at runtime.

    ``cache_write_usd`` defaults to *input_usd* per SPEC-CODEX 3
    ("``cache_write_input_tokens`` is separate and priced at the standard input
    rate"). It is a parameter only so a future model that prices cache writes
    differently has somewhere to say so.
    """
    input_dec = Decimal(input_usd)
    output_dec = Decimal(output_usd)
    cached_dec = Decimal(cached_input_usd)
    write_dec = Decimal(cache_write_usd) if cache_write_usd is not None else input_dec
    return PriceRow(
        model=model,
        input_usd_per_mtok=input_dec,
        output_usd_per_mtok=output_dec,
        effective_from=parse_day_key(effective_from) if effective_from else None,
        effective_until=parse_day_key(effective_until) if effective_until else None,
        rates=TokenRates.from_usd_per_mtok(
            input_dec,
            output_dec,
            cached_input_usd=cached_dec,
            cache_write_usd=write_dec,
        ),
        vendor=VENDOR_CODEX,
        cached_input_usd_per_mtok=cached_dec,
        cache_write_usd_per_mtok=write_dec,
    )


SONNET_5_INTRO_LAST_DAY: Final[dt.date] = dt.date(2026, 8, 31)
"""Last day Sonnet 5's introductory rate applies (inclusive) - SPEC 3.4."""

SONNET_5_STANDARD_FIRST_DAY: Final[dt.date] = dt.date(2026, 9, 1)
"""First day Sonnet 5's standard rate applies (inclusive) - SPEC 3.4."""


OPENAI_TERRA_LUNA_RATE_CUT_DAY: Final[dt.date] = dt.date(2026, 7, 30)
"""Day public reporting says OpenAI cut Terra/Luna **standard** rates.

Recorded as a date, not as a price row, and deliberately **not** wired into
:data:`OPENAI_PRICE_ROWS`. We have no authoritative dated table for the pre-cut
rates, and a made-up ``effective_until`` row would be a fabricated price - see
`OpenAI rate history`_ in the module docstring for the exact two-row shape to
add if OpenAI ever publishes its history. Until then this constant exists so
the caveat is greppable from code rather than living only in prose.
"""


CLAUDE_PRICE_ROWS: Final[tuple[PriceRow, ...]] = (
    # model              input   output   from          until
    _row("claude-fable-5", "10.00", "50.00"),
    _row("claude-mythos-5", "10.00", "50.00"),
    _row("claude-opus-5", "5.00", "25.00"),
    _row("claude-opus-4-8", "5.00", "25.00"),
    # Sonnet 5: introductory rate, then standard. Inclusive bounds, adjacent
    # days - no gap, no overlap. This pair is the reason resolution is dated.
    _row("claude-sonnet-5", "2.00", "10.00", effective_until="2026-08-31"),
    _row("claude-sonnet-5", "3.00", "15.00", effective_from="2026-09-01"),
    _row("claude-sonnet-4-6", "3.00", "15.00"),
    _row("claude-haiku-4-5", "1.00", "5.00"),
)
"""Anthropic rows, USD per million tokens (SPEC 3.4).

Only base input/output rates live here; cache rates are **derived** by the
frozen multipliers. Rows are resolved against the **usage record's** date.
"""


OPENAI_PRICE_ROWS: Final[tuple[PriceRow, ...]] = (
    # model                input   cached   output
    _openai_row("gpt-5.6-sol", "5.00", "0.50", "30.00"),
    _openai_row("gpt-5.6-terra", "2.00", "0.20", "12.00"),
    _openai_row("gpt-5.6-luna", "0.20", "0.02", "1.20"),
    _openai_row("gpt-5.4", "2.50", "0.25", "15.00"),
    _openai_row("gpt-5.4-mini", "0.75", "0.075", "4.50"),
)
"""OpenAI rows, USD per million tokens - verified 2026-08-17 against OpenAI's
official pricing docs (developers.openai.com/api/docs/pricing), SPEC-CODEX 3.

The middle column is the **published** cached-input rate, stored verbatim and
never derived; see `Two cache-rate shapes`_. Every rate here lands on an exact
integer number of picodollars per token (``$0.075/Mtok`` -> ``75_000``), so no
figure in this table is rounded on the way in - :func:`_rate_to_picos_per_token`
raises rather than accept one that would be.

Unbounded on both sides, with the Terra/Luna caveat recorded at
:data:`OPENAI_TERRA_LUNA_RATE_CUT_DAY`.

Not listed, therefore not priced: ``codex-auto-review``. OpenAI publishes no
rate for it, so it takes the unknown path - tokens counted, ``$0``, name
surfaced - rather than borrowing a sibling's rate (SPEC-CODEX 3).
"""


PRICE_ROWS: Final[tuple[PriceRow, ...]] = CLAUDE_PRICE_ROWS + OPENAI_PRICE_ROWS
"""The whole price table, both vendors, in menu order (Claude first).

Rows are indexed by ``(vendor, model)``, so two vendors shipping a same-named
model would occupy two independent entries rather than colliding.
"""


MODEL_DISPLAY_NAMES: Final[Mapping[VendorModelKey, str]] = {
    # Claude keys stay BARE - this map is public and pre-Codex callers index it
    # with a plain model string.
    "claude-fable-5": "Fable 5",
    "claude-mythos-5": "Mythos 5",
    "claude-opus-5": "Opus 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-sonnet-5": "Sonnet 5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-haiku-4-5": "Haiku 4.5",
    # OpenAI keys are vendor-qualified, so a future Anthropic model that
    # happened to share a name could not overwrite one of these.
    "codex:gpt-5.6-sol": "gpt-5.6-sol",
    "codex:gpt-5.6-terra": "gpt-5.6-terra",
    "codex:gpt-5.6-luna": "gpt-5.6-luna",
    "codex:gpt-5.4": "gpt-5.4",
    "codex:gpt-5.4-mini": "gpt-5.4-mini",
}
"""Menu labels (SPEC 4.2: ``Fable 5``, ``Opus 5``, ``Sonnet 5``, ``Haiku 4.5``).

Keyed by :data:`~contracts.VendorModelKey`, so lookups are per-vendor;
:meth:`ModelPricing.display_name` falls back to the bare key so a caller
passing its own ``{bare: label}`` map still works.

The OpenAI models map to **their own names**. That is a decision, not a gap:
:func:`_derive_display_name` would render ``gpt-5.6-sol`` as ``"Gpt 5.6.sol"``,
and the name OpenAI ships is the name the user recognises. The vendor is shown
separately via :attr:`contracts.ModelCostRow.vendor_label`, so the row reads
``gpt-5.6-sol`` under a ``Codex`` heading rather than repeating itself.

A canonical model missing from this map falls back to
:func:`_derive_display_name`, so adding a price row is enough to get a sane
label for a Claude-shaped name.
"""


def _derive_display_name(model: ModelKey) -> str:
    """``"claude-opus-4-8"`` -> ``"Opus 4.8"``; family title-cased, version dotted."""
    parts = [p for p in model.split("-") if p]
    if parts and parts[0] == "claude":
        parts = parts[1:]
    if not parts:
        return model
    family = parts[0].capitalize()
    version = ".".join(parts[1:])
    return f"{family} {version}".strip()


# ---------------------------------------------------------------------------
# 3. Raw model string -> canonical key
# ---------------------------------------------------------------------------

_PROVIDER_DOT_PREFIXES: Final[tuple[str, ...]] = (
    "us.",
    "use.",
    "usw.",
    "eu.",
    "apac.",
    "us-gov.",
    "anthropic.",
)
"""Bedrock-style region / vendor prefixes, stripped left-to-right."""

_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:[-@:](?:\d{8}|latest|preview|v\d+(?::\d+)?|\d+[km]))*$"
)
"""What may follow a canonical key for it to still be *that* model.

Accepted: nothing, a snapshot date (``-20260514``, ``@20260514``), a provider
version (``-v1:0``), a context-window marker (``-1m``), ``-latest``,
``-preview``, and combinations. Anything else - notably a further version
segment like ``-1`` in a hypothetical ``claude-fable-5-1`` - is **rejected**,
so the model reports as unknown instead of borrowing Fable 5's rate.
"""

_BRACKET_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"\[[^\]]*\]\s*$")
"""Trailing context-window marker, e.g. ``claude-opus-5[1m]``."""

_CONTEXT_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\[\s*(\d+[km])\s*\]|[-@:](\d+[km]))\s*$", re.IGNORECASE
)
"""The context-window marker :data:`_BRACKET_SUFFIX_RE` / :data:`_SUFFIX_RE`
strip, captured so it can be *reported* instead of silently vanishing.

Both ``claude-opus-5[1m]`` and ``claude-opus-5-1m`` canonicalise to
``claude-opus-5`` and therefore price at the standard rate. That is a deliberate
fold, not an oversight: SPEC 3.4's table has no long-context row, and inventing a
multiplier would be exactly the "silently price an unrecognised model at another
model's rate" this module forbids. The honest position is to price at the base
rate **and say so** - so :func:`context_window_marker` exists and the menu label
carries it. If a real long-context row is ever added, drop these two forms from
the suffix patterns so they cannot fold at all."""


def context_window_marker(raw_model: str) -> str | None:
    """``"claude-opus-5[1m]"`` -> ``"1m"``; ``None`` when there is no marker.

    Lets a caller tell the user that a long-context request was priced at the
    short-context rate rather than leaving the tier collapsed in silence
    (SPEC 4.3).
    """
    if not isinstance(raw_model, str):
        return None
    # Accepts a vendor-qualified key too: the prefix comes off first so the
    # ``:`` in "codex:..." can never be read as an "@/:"-style marker separator.
    bare = raw_model_of_key(raw_model.strip())
    match = _CONTEXT_MARKER_RE.search(bare.strip().lower())
    if match is None:
        return None
    return (match.group(1) or match.group(2)) or None

_MEMO_LIMIT: Final[int] = 4096
"""Cap on the raw->canonical memo, so junk model strings cannot grow it forever."""


@lru_cache(maxsize=1024)
def _normalize(raw_model: str) -> str:
    """Lower-case, de-prefix and de-bracket a raw ``message.model`` string.

    Pure and cached (``lru_cache`` is thread-safe). Returns ``""`` for input
    that cannot be a model id.
    """
    key = raw_model.strip().lower()
    if not key:
        return ""
    # openrouter/anthropic/claude-..., vertex_ai/claude-..., bedrock/...
    if "/" in key:
        key = key.rsplit("/", 1)[-1]
    key = _BRACKET_SUFFIX_RE.sub("", key).strip()
    changed = True
    while changed:
        changed = False
        for prefix in _PROVIDER_DOT_PREFIXES:
            if key.startswith(prefix):
                key = key[len(prefix) :]
                changed = True
    return key


# ---------------------------------------------------------------------------
# 4. PriceResolution - what a lookup answers with
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceResolution:
    """The full answer to "what does this raw model cost on this day?".

    Unlike :meth:`ModelPricing.price_for` (which returns ``None`` for an
    unknown model, per :class:`contracts.PricingTable`), this always carries a
    usable :attr:`price`: a **zero-rate** :class:`ModelPrice` when the model is
    unknown or no row covers *day*. The raw string is kept in
    :attr:`raw_model` / :attr:`display_name` so the UI can surface the actual
    unrecognised name (SPEC 3.3 trap 5).

    :attr:`model` is the **bare** canonical key and :attr:`vendor` is its own
    field - the pair, not the string, is the identity. ``vendor`` is appended
    last with a :data:`~contracts.VENDOR_CLAUDE` default so every pre-Codex
    construction of this class is unchanged.
    """

    raw_model: str
    model: ModelKey
    display_name: str
    price: ModelPrice
    rates: TokenRates
    day: dt.date
    is_known: bool
    is_priced: bool
    vendor: Vendor = VENDOR_CLAUDE

    @property
    def vendor_key(self) -> VendorModelKey:
        """The rollup key this resolution belongs to.

        :func:`~contracts.unknown_model_key` for an unknown model, so a Codex
        model with no published rate buckets as ``"codex:unknown"`` and never
        shares a ``$0`` menu row with an unrecognised Claude one.
        """
        if self.model == UNKNOWN_MODEL:
            return unknown_model_key(self.vendor)
        return make_vendor_key(self.vendor, self.model)

    @property
    def vendor_label(self) -> str:
        """Display label for this resolution's vendor, e.g. ``"Codex"``."""
        return vendor_label(self.vendor)

    def cost_picos(self, usage: ModelUsage) -> int:
        """Exact integer picodollars for *usage* at this resolution's rates."""
        return self.rates.cost_picos(usage)

    def cost(self, usage: ModelUsage) -> Decimal:
        """Exact ``Decimal`` USD for *usage*."""
        return picos_to_usd(self.cost_picos(usage))

    def cost_usd(self, usage: ModelUsage) -> Usd:
        """``float`` USD for *usage* (contract boundary type)."""
        return picos_to_float(self.cost_picos(usage))


def _as_date(day: dt.date | DayKey) -> dt.date:
    """Accept either a ``date`` or a ``"YYYY-MM-DD"`` :data:`contracts.DayKey`.

    ``datetime`` is narrowed to its ``date`` so a caller passing a timestamp
    cannot accidentally compare a ``datetime`` against the table's ``date``
    bounds.
    """
    if isinstance(day, dt.datetime):
        return day.date()
    if isinstance(day, dt.date):
        return day
    return parse_day_key(day)


# ---------------------------------------------------------------------------
# 5. ModelPricing - the contracts.PricingTable implementation
# ---------------------------------------------------------------------------


class ModelPricing:
    """Dated price table implementing :class:`contracts.PricingTable`.

    Construct with no arguments for the shipped table (:data:`PRICE_ROWS`), or
    pass *rows* to price against a fixture table in a test::

        pricing = ModelPricing()
        pricing.cost_for(usage, "claude-sonnet-5-20260514", dt.date(2026, 9, 5))

    Immutable and thread-safe after construction (SPEC 2.3): the only mutable
    member is a bounded memo of raw-string -> canonical-key, whose values are a
    pure function of the key.
    """

    __slots__ = (
        "_rows",
        "_by_key",
        "_prefixes",
        "_owner",
        "_vendors",
        "_display",
        "_memo",
    )

    def __init__(
        self,
        rows: Sequence[PriceRow] | None = None,
        *,
        display_names: Mapping[VendorModelKey, str] | None = None,
    ) -> None:
        self._rows: tuple[PriceRow, ...] = tuple(PRICE_ROWS if rows is None else rows)
        by_key: dict[VendorModelKey, list[PriceRow]] = {}
        # bare model key -> the vendors that price it. A bare (unqualified)
        # lookup may only cross vendors when exactly one owns the name.
        owners: dict[ModelKey, set[Vendor]] = {}
        for row in self._rows:
            by_key.setdefault(row.vendor_key, []).append(row)
            owners.setdefault(row.model, set()).add(row.vendor)
        # Ascending by effective_from with the unbounded row first, so that
        # scanning in reverse finds the latest-starting covering row: the
        # Sonnet 5 standard row wins from 2026-09-01, intro before it.
        self._by_key: Mapping[VendorModelKey, tuple[PriceRow, ...]] = {
            key: tuple(
                sorted(
                    key_rows,
                    key=lambda r: (
                        r.effective_from is not None,
                        r.effective_from or dt.date.min,
                    ),
                )
            )
            for key, key_rows in by_key.items()
        }
        self._owner: Mapping[ModelKey, frozenset[Vendor]] = {
            model: frozenset(vendors) for model, vendors in owners.items()
        }
        # Per vendor, its bare model keys longest first, so "claude-opus-4-8"
        # is tried before any shorter key that happens to be a prefix of it -
        # and, on the OpenAI side, "gpt-5.4-mini" before "gpt-5.4".
        prefixes: dict[Vendor, list[ModelKey]] = {}
        for row in self._rows:
            bucket = prefixes.setdefault(row.vendor, [])
            if row.model not in bucket:
                bucket.append(row.model)
        self._prefixes: Mapping[Vendor, tuple[ModelKey, ...]] = {
            vendor: tuple(sorted(models, key=len, reverse=True))
            for vendor, models in prefixes.items()
        }
        # Known vendors in menu order, plus any exotic one a fixture table
        # introduced, so a test row for a third vendor still resolves.
        extra = [v for v in prefixes if v not in VENDORS]
        self._vendors: tuple[Vendor, ...] = tuple(
            [v for v in VENDORS if v in prefixes] + sorted(extra)
        )
        merged: dict[VendorModelKey, str] = dict(MODEL_DISPLAY_NAMES)
        if display_names:
            merged.update(display_names)
        self._display: Mapping[VendorModelKey, str] = merged
        self._memo: dict[str, tuple[Vendor, ModelKey]] = {}

    # -- introspection ----------------------------------------------------

    @property
    def rows(self) -> tuple[PriceRow, ...]:
        """Every row in this table, in declaration order."""
        return self._rows

    @property
    def vendors(self) -> tuple[Vendor, ...]:
        """Every vendor this table prices, in :data:`~contracts.VENDORS` order."""
        return self._vendors

    def models(self) -> tuple[VendorModelKey, ...]:
        """Every canonical key this table prices, sorted.

        Keys are :data:`~contracts.VendorModelKey` - bare for Claude,
        ``"codex:..."`` otherwise - so two vendors' same-named models are two
        distinct entries. Use :meth:`models_for_vendor` for bare names.
        """
        return tuple(sorted(self._by_key))

    def models_for_vendor(self, vendor: Vendor) -> tuple[ModelKey, ...]:
        """Every **bare** canonical model key one vendor prices, sorted."""
        return tuple(sorted(self._prefixes.get(vendor, ())))

    def rows_for(
        self, model: ModelKey, vendor: Vendor = VENDOR_CLAUDE
    ) -> tuple[PriceRow, ...]:
        """Every row for one model, oldest first. ``()`` if unknown.

        *model* may be bare (resolved within *vendor*) or already
        vendor-qualified, in which case its own prefix wins.
        """
        return self._by_key.get(make_vendor_key(vendor, str(model)), ())

    # -- contracts.PricingTable ------------------------------------------

    def resolve_vendor_model(self, raw_model: str) -> tuple[Vendor, ModelKey]:
        """Map a raw or vendor-qualified string to ``(vendor, bare key)``.

        The single resolution primitive; :meth:`canonical_model`,
        :meth:`vendor_for`, :meth:`canonical_key` and every price lookup are
        thin readings of its two halves, so they cannot disagree about which
        vendor a string belongs to.

        Total: never raises. Junk resolves to
        ``(VENDOR_CLAUDE, UNKNOWN_MODEL)``.
        """
        if not isinstance(raw_model, str):
            return (VENDOR_CLAUDE, UNKNOWN_MODEL)
        cached = self._memo.get(raw_model)
        if cached is not None:
            return cached
        resolved = self._resolve_key(raw_model)
        if len(self._memo) < _MEMO_LIMIT:
            # Benign race: concurrent writers can only store the same value.
            self._memo[raw_model] = resolved
        return resolved

    def canonical_model(self, raw_model: str) -> ModelKey:
        """Map a raw ``message.model`` string to a price-table key.

        Returns the **bare** canonical key, per
        :class:`contracts.PricingTable` - the vendor travels alongside it in
        :meth:`vendor_for` / :attr:`PriceResolution.vendor`, and
        :meth:`canonical_key` composes the two when a storage key is wanted.

        Total by contract: never raises, never returns ``""``. Returns
        :data:`contracts.UNKNOWN_MODEL` for anything the table does not
        recognise, including ``None`` and non-string junk.
        """
        return self.resolve_vendor_model(raw_model)[1]

    def vendor_for(self, raw_model: str) -> Vendor:
        """Which vendor's economy *raw_model* belongs to.

        A vendor-qualified string reports its own prefix, an unqualified one
        that resolves cross-vendor reports where it resolved, and anything else
        reports :data:`~contracts.VENDOR_CLAUDE` - the migration default for
        every key written before Codex support existed.
        """
        return self.resolve_vendor_model(raw_model)[0]

    def canonical_key(self, raw_model: str) -> VendorModelKey:
        """Canonical **storage** key for *raw_model* (SPEC-CODEX 2.1).

        ``"gpt-5.6-sol"`` and ``"codex:gpt-5.6-sol"`` both give
        ``"codex:gpt-5.6-sol"``; ``"claude-fable-5-20260514"`` gives the bare
        ``"claude-fable-5"``. An unknown model gives its vendor's own unknown
        bucket - ``"unknown"`` or ``"codex:unknown"`` - so an unpriced Codex
        model never merges into the Claude ``$0`` row.

        This is the method a rollup should group by: unlike
        :meth:`canonical_model` it cannot collide two vendors' same-named
        models onto one row.
        """
        vendor, model = self.resolve_vendor_model(raw_model)
        if model == UNKNOWN_MODEL:
            return unknown_model_key(vendor)
        return make_vendor_key(vendor, model)

    def _match_within(self, vendor: Vendor, key: str) -> ModelKey | None:
        """Exact-then-prefix match of a normalised *key* inside one vendor.

        See :data:`_SUFFIX_RE` for why prefix matching is strict. Longest-first
        candidate order is what stops ``"gpt-5.4-mini"`` resolving to
        ``"gpt-5.4"``; the ``-mini`` remainder would be rejected by
        :data:`_SUFFIX_RE` anyway, so the two guards are independent.
        """
        if make_vendor_key(vendor, key) in self._by_key:
            return key
        for candidate in self._prefixes.get(vendor, ()):
            if key.startswith(candidate) and _SUFFIX_RE.match(key[len(candidate) :]):
                return candidate
        return None

    def _resolve_key(self, raw_model: str) -> tuple[Vendor, ModelKey]:
        """Uncached canonicalisation. See `Cross-vendor resolution`_.

        Two steps, and the second one never runs for an explicitly qualified
        string - that is what makes ``"codex:claude-fable-5"`` cost ``$0``
        instead of borrowing Fable 5's rate.
        """
        stated, bare = split_vendor_key(raw_model)
        explicit = is_vendor_qualified(raw_model)
        key = _normalize(bare)
        if not key:
            return (stated, UNKNOWN_MODEL)

        hit = self._match_within(stated, key)
        if hit is not None:
            return (stated, hit)
        if explicit:
            # The string asserted its vendor and that vendor does not price it.
            # Honour the assertion: unknown, $0, name surfaced.
            return (stated, UNKNOWN_MODEL)

        # Unqualified and unresolved against the default vendor. Fall back only
        # to an UNAMBIGUOUS owner elsewhere: collect every vendor that claims
        # it, and refuse if more than one does (or if the name it matched is
        # itself priced by several vendors). A coin-flip between two vendors'
        # rates is exactly the fabricated price this module forbids.
        matches: list[tuple[Vendor, ModelKey]] = []
        for vendor in self._vendors:
            if vendor == stated:
                continue
            found = self._match_within(vendor, key)
            if found is not None:
                matches.append((vendor, found))
        if len(matches) == 1:
            vendor, found = matches[0]
            if len(self._owner.get(found, ())) == 1:
                return (vendor, found)
        return (stated, UNKNOWN_MODEL)

    def is_known(self, raw_model: str) -> bool:
        """True when :meth:`canonical_model` resolves to a priced model."""
        return self.canonical_model(raw_model) != UNKNOWN_MODEL

    def display_name(self, raw_model: str) -> str:
        """Human label for the menu, e.g. ``"Fable 5"`` or ``"gpt-5.6-sol"``.

        For an unknown model this returns the **raw** string itself (stripped),
        so the user sees the actual unrecognised name (SPEC 3.3 trap 5) - with
        any vendor prefix removed, because ``"codex:codex-auto-review"`` is a
        storage key and the user must be shown ``"codex-auto-review"``.
        """
        vendor, canonical = self.resolve_vendor_model(raw_model)
        if canonical == UNKNOWN_MODEL:
            if isinstance(raw_model, str) and raw_model.strip():
                bare = raw_model_of_key(raw_model.strip()).strip()
                return bare if bare else UNKNOWN_MODEL
            return UNKNOWN_MODEL
        label = self._display.get(make_vendor_key(vendor, canonical))
        if not label:
            # Fall back to the bare key, so a caller-supplied {bare: label} map
            # still works for a vendor whose keys are qualified.
            label = self._display.get(canonical)
        return label if label else _derive_display_name(canonical)

    def price_for(self, raw_model: str, day: dt.date) -> ModelPrice | None:
        """Price row in effect for *raw_model* on *day*, or ``None``.

        ``None`` means unknown model, or a known model with no row covering
        that date. Per :class:`contracts.PricingTable`, callers must then price
        at ``$0`` and surface the model name - never fall back to another
        model's rate. :meth:`resolve` is the ``None``-free variant.
        """
        row = self._row_for(raw_model, _as_date(day))
        return row.as_model_price() if row is not None else None

    def cost_usd(self, raw_model: str, usage: ModelUsage, day: dt.date) -> Usd:
        """``float`` notional USD for *usage* of *raw_model* incurred on *day*.

        ``0.0`` for an unknown or unpriced model. Computed through the integer
        picodollar path, so it is the closest float to the exact figure. Prefer
        :meth:`cost_picos` when the result will be summed.
        """
        return picos_to_float(self.cost_picos(raw_model, usage, day))

    # -- exact-money API --------------------------------------------------

    def cost_picos(
        self, raw_model: str, usage: ModelUsage, day: dt.date | DayKey
    ) -> int:
        """Exact integer picodollars for *usage* of *raw_model* on *day*.

        The accumulation-safe primitive: sum these across models and days, then
        convert once. ``0`` for an unknown or unpriced model.
        """
        row = self._row_for(raw_model, _as_date(day))
        if row is None:
            return 0
        return row.rates.cost_picos(usage)

    def cost_for(
        self, usage: ModelUsage, model: str, on_date: dt.date | DayKey
    ) -> Decimal:
        """Exact ``Decimal`` USD for *usage* of *model* incurred on *on_date*.

        Argument order matches the module's stated public helper
        (``usage, model, on_date``); :meth:`cost_usd` keeps the
        ``contracts.PricingTable`` order (``raw_model, usage, day``). Both go
        through the same integer path, so they never disagree beyond float
        representation.

        *on_date* accepts a ``date``, a ``datetime`` (narrowed to its date) or a
        ``"YYYY-MM-DD"`` :data:`contracts.DayKey`. Returns ``Decimal(0)`` for an
        unknown or unpriced model.
        """
        return picos_to_usd(self.cost_picos(model, usage, on_date))

    def resolve(self, raw_model: str, day: dt.date | DayKey) -> PriceResolution:
        """Full, ``None``-free resolution of *raw_model* on *day*.

        Always returns a :class:`PriceResolution` carrying a usable
        :attr:`~PriceResolution.price`; unknown or unpriced models get a
        zero-rate price whose ``model`` is :data:`contracts.UNKNOWN_MODEL`,
        while the raw name stays visible for the menu.
        """
        as_date = _as_date(day)
        vendor, canonical = self.resolve_vendor_model(raw_model)
        row = self._row_for(raw_model, as_date)
        if row is None:
            return PriceResolution(
                raw_model=raw_model if isinstance(raw_model, str) else str(raw_model),
                model=canonical,
                display_name=self.display_name(raw_model),
                price=_zero_price(vendor),
                rates=ZERO_RATES,
                day=as_date,
                is_known=canonical != UNKNOWN_MODEL,
                is_priced=False,
                vendor=vendor,
            )
        return PriceResolution(
            raw_model=raw_model,
            model=row.model,
            display_name=self.display_name(raw_model),
            price=row.as_model_price(),
            rates=row.rates,
            day=as_date,
            is_known=True,
            is_priced=True,
            vendor=row.vendor,
        )

    def _row_for(self, raw_model: str, day: dt.date) -> PriceRow | None:
        """The covering row for *raw_model* on *day*, resolved **by that date**.

        Looked up by ``(vendor, model)``, so a model is only ever priced
        against its own vendor's rows.
        """
        vendor, canonical = self.resolve_vendor_model(raw_model)
        if canonical == UNKNOWN_MODEL:
            return None
        rows = self._by_key.get(make_vendor_key(vendor, canonical))
        if not rows:
            return None
        # Reverse order: the latest-starting row that covers `day` wins, so the
        # 2026-09-01 standard row beats the unbounded-start intro row.
        for row in reversed(rows):
            if row.covers(day):
                return row
        return None

    # -- DayRollup helpers -------------------------------------------------

    def day_rollup_cost_picos(
        self, rollup: DayRollup, *, day: dt.date | DayKey | None = None
    ) -> int:
        """Exact integer picodollars for a whole :class:`contracts.DayRollup`.

        Prices every raw model in the rollup with the row in effect on the
        rollup's **own** day (``rollup.day``), which is what keeps historical
        days correct across the Sonnet 5 rollover. Pass *day* only to override
        that, e.g. when pricing a synthetic fixture.

        Integer-summed, so a 30-day total accumulates no float error.
        """
        as_date = _as_date(rollup.day if day is None else day)
        total = 0
        for raw_model, usage in rollup.models.items():
            row = self._row_for(raw_model, as_date)
            if row is not None:
                total += row.rates.cost_picos(usage)
        return total

    def day_rollup_cost(
        self, rollup: DayRollup, *, day: dt.date | DayKey | None = None
    ) -> Decimal:
        """Exact ``Decimal`` USD for a whole day."""
        return picos_to_usd(self.day_rollup_cost_picos(rollup, day=day))

    def day_rollup_cost_usd(
        self, rollup: DayRollup, *, day: dt.date | DayKey | None = None
    ) -> Usd:
        """``float`` USD for a whole day (contract boundary type)."""
        return picos_to_float(self.day_rollup_cost_picos(rollup, day=day))

    def day_rollup_unknown_models(self, rollup: DayRollup) -> tuple[str, ...]:
        """Raw model names in *rollup* that this table does not price.

        Sorted and de-duplicated - ready for
        :attr:`contracts.CostBreakdown.unknown_models`.

        Names are **bare**: a key stored as ``"codex:codex-auto-review"`` is
        reported as ``"codex-auto-review"``. This list is rendered verbatim in
        the menu, and a user must be shown the model name OpenAI uses, never
        our storage key.
        """
        return tuple(
            sorted(
                {
                    raw_model_of_key(str(raw))
                    for raw in rollup.models
                    if self.canonical_model(raw) == UNKNOWN_MODEL
                }
            )
        )

    def day_rollup_cost_rows(
        self,
        rollup: DayRollup,
        *,
        day: dt.date | DayKey | None = None,
        include_zero_usage: bool = False,
    ) -> tuple[ModelCostRow, ...]:
        """Per-model :class:`contracts.ModelCostRow` tuple for one day.

        Raw model strings are collapsed onto their canonical **(vendor, key)**
        pair - so ``claude-opus-5`` and ``claude-opus-5-20260514`` become one
        row with both names in :attr:`~contracts.ModelCostRow.raw_models`,
        while two vendors' same-named models stay two rows. Unknown strings
        collapse into **one unknown row per vendor**, each priced at ``$0``,
        so an unpriced ``codex-auto-review`` never shares a row - or a ``$0``
        explanation - with an unrecognised Claude model (SPEC-CODEX 2.1). The
        result is ordered by descending USD with the unknown rows **last** -
        exactly the ``by model`` ordering SPEC 4.2 renders.

        The rows are **flat and cross-vendor**, matching
        :attr:`contracts.CostBreakdown.by_model`; each carries its own
        :attr:`~contracts.ModelCostRow.vendor` for the caller to group by when
        building :class:`contracts.VendorCostRow` sections.

        Rows are costed in picodollars and converted once, so the rows sum to
        the same figure as :meth:`day_rollup_cost` (SPEC 4.2's per-model rows
        summing exactly to ``Today``).

        Args:
            include_zero_usage: keep models whose five counters are all zero.
                Off by default, since a zero-token row must not be rendered.
                A zero-*cost* row with real tokens (Haiku at ``$0.00``) is
                always kept.
        """
        as_date = _as_date(rollup.day if day is None else day)
        # Grouped by (vendor, canonical model) - NOT by the bare model key.
        # Two vendors may legitimately ship a same-named model, and collapsing
        # them onto one row would sum tokens across two economies and price the
        # lot at whichever table won the lookup.
        counters: dict[tuple[Vendor, ModelKey], list[int]] = {}
        picos: dict[tuple[Vendor, ModelKey], int] = {}
        raws: dict[tuple[Vendor, ModelKey], set[str]] = {}
        for raw_model, usage in rollup.models.items():
            vendor, canonical = self.resolve_vendor_model(raw_model)
            group = (vendor, canonical)
            row = self._row_for(raw_model, as_date)
            acc = counters.get(group)
            if acc is None:
                acc = [0, 0, 0, 0, 0]
                counters[group] = acc
                picos[group] = 0
                raws[group] = set()
            for i, value in enumerate(usage.as_counters()):
                acc[i] += value
            if row is not None:
                picos[group] += row.rates.cost_picos(usage)
            # Bare name: raw_models is user-visible text, never a storage key.
            raws[group].add(raw_model_of_key(str(raw_model)))

        ranked: list[tuple[ModelCostRow, int]] = []
        for group, acc in counters.items():
            vendor, canonical = group
            usage = ModelUsage.from_counters(acc)
            if usage.is_zero and not include_zero_usage:
                continue
            is_unknown = canonical == UNKNOWN_MODEL
            raw_names = tuple(sorted(raws[group]))
            if is_unknown:
                # Show the actual unrecognised name(s), not the word "unknown".
                label = ", ".join(raw_names) if raw_names else UNKNOWN_MODEL
            else:
                label = (
                    self._display.get(make_vendor_key(vendor, canonical))
                    or self._display.get(canonical)
                    or _derive_display_name(canonical)
                )
            ranked.append(
                (
                    ModelCostRow(
                        model=canonical,
                        display_name=label,
                        usage=usage,
                        usd=picos_to_float(picos[group]),
                        is_unknown=is_unknown,
                        raw_models=raw_names,
                        vendor=vendor,
                    ),
                    picos[group],
                )
            )
        # Sort on the exact picodollar figure carried alongside each row, not
        # on a dict keyed by r.model - which two vendors could now collide in.
        ranked.sort(
            key=lambda pair: (
                pair[0].is_unknown,        # unknown bucket last
                -pair[1],                  # then descending cost (exact, not float)
                -pair[0].total_tokens,     # tie-break on tokens
                pair[0].display_name,
                pair[0].vendor,
            )
        )
        return tuple(row for row, _ in ranked)

    # -- window helpers ----------------------------------------------------

    def window_cost_picos(self, rollups: Iterable[DayRollup]) -> int:
        """Exact integer picodollars across many days.

        Each day is priced with the row in effect **on that day**, so a window
        spanning the Sonnet 5 rollover mixes both rates correctly.
        """
        return sum(self.day_rollup_cost_picos(rollup) for rollup in rollups)

    def window_cost(self, rollups: Iterable[DayRollup]) -> Decimal:
        """Exact ``Decimal`` USD across many days."""
        return picos_to_usd(self.window_cost_picos(rollups))

    def window_cost_usd(self, rollups: Iterable[DayRollup]) -> Usd:
        """``float`` USD across many days (contract boundary type)."""
        return picos_to_float(self.window_cost_picos(rollups))


# Aliases, so a sibling module reaching for a differently-remembered name still
# composes. All three are the same class.
PriceTable = ModelPricing
Pricing = ModelPricing


# ---------------------------------------------------------------------------
# 6. Default instance + module-level convenience wrappers
# ---------------------------------------------------------------------------

DEFAULT_PRICING: Final[ModelPricing] = ModelPricing()
"""The shipped table. Immutable and thread-safe; share it, do not rebuild it."""

PRICING: Final[ModelPricing] = DEFAULT_PRICING
"""Alias for :data:`DEFAULT_PRICING`."""


def default_pricing() -> ModelPricing:
    """Return the shared :data:`DEFAULT_PRICING` instance."""
    return DEFAULT_PRICING


def canonical_model(raw_model: str) -> ModelKey:
    """:meth:`ModelPricing.canonical_model` on the default table."""
    return DEFAULT_PRICING.canonical_model(raw_model)


def canonical_key(raw_model: str) -> VendorModelKey:
    """:meth:`ModelPricing.canonical_key` on the default table."""
    return DEFAULT_PRICING.canonical_key(raw_model)


def vendor_for(raw_model: str) -> Vendor:
    """:meth:`ModelPricing.vendor_for` on the default table."""
    return DEFAULT_PRICING.vendor_for(raw_model)


def is_known(raw_model: str) -> bool:
    """:meth:`ModelPricing.is_known` on the default table."""
    return DEFAULT_PRICING.is_known(raw_model)


def display_name(raw_model: str) -> str:
    """:meth:`ModelPricing.display_name` on the default table."""
    return DEFAULT_PRICING.display_name(raw_model)


def price_for(raw_model: str, day: dt.date) -> ModelPrice | None:
    """:meth:`ModelPricing.price_for` on the default table."""
    return DEFAULT_PRICING.price_for(raw_model, day)


def resolve(raw_model: str, day: dt.date | DayKey) -> PriceResolution:
    """:meth:`ModelPricing.resolve` on the default table."""
    return DEFAULT_PRICING.resolve(raw_model, day)


def cost_for(usage: ModelUsage, model: str, on_date: dt.date | DayKey) -> Decimal:
    """:meth:`ModelPricing.cost_for` on the default table (exact ``Decimal`` USD)."""
    return DEFAULT_PRICING.cost_for(usage, model, on_date)


def cost_usd(raw_model: str, usage: ModelUsage, day: dt.date) -> Usd:
    """:meth:`ModelPricing.cost_usd` on the default table (``float`` USD)."""
    return DEFAULT_PRICING.cost_usd(raw_model, usage, day)


def cost_picos(raw_model: str, usage: ModelUsage, day: dt.date | DayKey) -> int:
    """:meth:`ModelPricing.cost_picos` on the default table (exact ``int``)."""
    return DEFAULT_PRICING.cost_picos(raw_model, usage, day)


def day_rollup_cost_picos(
    rollup: DayRollup, *, day: dt.date | DayKey | None = None
) -> int:
    """:meth:`ModelPricing.day_rollup_cost_picos` on the default table."""
    return DEFAULT_PRICING.day_rollup_cost_picos(rollup, day=day)


def day_rollup_cost(rollup: DayRollup, *, day: dt.date | DayKey | None = None) -> Decimal:
    """:meth:`ModelPricing.day_rollup_cost` on the default table."""
    return DEFAULT_PRICING.day_rollup_cost(rollup, day=day)


def day_rollup_cost_usd(
    rollup: DayRollup, *, day: dt.date | DayKey | None = None
) -> Usd:
    """:meth:`ModelPricing.day_rollup_cost_usd` on the default table."""
    return DEFAULT_PRICING.day_rollup_cost_usd(rollup, day=day)


def day_rollup_cost_rows(
    rollup: DayRollup,
    *,
    day: dt.date | DayKey | None = None,
    include_zero_usage: bool = False,
) -> tuple[ModelCostRow, ...]:
    """:meth:`ModelPricing.day_rollup_cost_rows` on the default table."""
    return DEFAULT_PRICING.day_rollup_cost_rows(
        rollup, day=day, include_zero_usage=include_zero_usage
    )


def day_rollup_unknown_models(rollup: DayRollup) -> tuple[str, ...]:
    """:meth:`ModelPricing.day_rollup_unknown_models` on the default table."""
    return DEFAULT_PRICING.day_rollup_unknown_models(rollup)


def window_cost_picos(rollups: Iterable[DayRollup]) -> int:
    """:meth:`ModelPricing.window_cost_picos` on the default table."""
    return DEFAULT_PRICING.window_cost_picos(rollups)


def window_cost(rollups: Iterable[DayRollup]) -> Decimal:
    """:meth:`ModelPricing.window_cost` on the default table."""
    return DEFAULT_PRICING.window_cost(rollups)


def window_cost_usd(rollups: Iterable[DayRollup]) -> Usd:
    """:meth:`ModelPricing.window_cost_usd` on the default table."""
    return DEFAULT_PRICING.window_cost_usd(rollups)
