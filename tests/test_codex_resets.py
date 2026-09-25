"""Codex reset credits (``rate_limit_reset_credits``) - parse, persist, show.

Every live ``/backend-api/wham/usage`` body probed on 2026-09-25 carries
``rate_limit_reset_credits: {available_count, applicable_available_count}``
(evidence: ``~/.claude/plans/usage-bar-2026-09-25/evidence/
main-resets-probe-2026-09-25.md``). One account held ``{1, 0}``, three held
``{0, 0}``, and an older Pro fixture carried ``{}``. There is no known API to
CONSUME a reset credit, so the widget only DISPLAYS the two counters:

* a missing key, a non-number or an empty object is UNKNOWN (``None``), never
  0 - a zero we invented would read as "you have none" on an account that may;
* ``usable > 0`` puts ``↺ N reset credit(s) usable now — use it/them in Codex``
  FIRST among the row's info notes; ``available > 0`` with ``usable == 0``
  puts ``reset credits: N (not usable now)``; nothing otherwise;
* a dead credential (a ``warn`` sentinel) withholds the note and the
  ``AccountRow`` counters with the rest of the reading;
* the Codex fleet heading appends `` · N reset(s) usable`` or
  `` · N reset(s) banked`` and is byte-for-byte unchanged when every counter is
  unknown or zero.

Fixtures are the captured body shape from ``tests/test_codex_accounts.py``
(placeholder ids and ``example.test`` emails) with only the reset object
varied; the counts are the probed ``{1, 0}`` plus clearly synthetic values.

Run directly, or with pytest if it is installed::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    CC_USAGE_WIDGET_NO_REVEAL=1 $PY tests/test_codex_resets.py
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget.codex_accounts import (  # noqa: E402
    KIND_CRIT,
    KIND_WARN,
    NOTE_NO_ACCESS,
    NOTE_RELOGIN,
    CodexAccountQuota,
)
from cc_usage_widget.contracts import (  # noqa: E402
    CODEX_FETCH_STALE_SECONDS,
    VENDOR_CODEX,
    AccountRow,
)

BASE_NOW = _dt.datetime(2026, 9, 25, 10, 0, 0).timestamp()
"""A fixed local 10:00, like ``test_codex_accounts.BASE_NOW``: every instant in
this module is relative to it, so nothing depends on the real clock."""

ACCOUNT_ID = "acct-resets-2222222222"
EMAIL = "resets@example.test"

USABLE_1 = "↺ 1 reset credit usable now — use it in Codex"
BANKED_1 = "reset credits: 1 (not usable now)"


def body(
    reset_credits: Any = None,
    *,
    omit_key: bool = False,
    used_percent: float = 0,
    capped: bool = False,
) -> dict[str, Any]:
    """The probed wham/usage shape with only ``rate_limit_reset_credits`` varied.

    Key set as recorded in the 2026-09-25 probe (account_id,
    additional_rate_limits, code_review_rate_limit, credits, email,
    model_usage, plan_type, promo, rate_limit, rate_limit_reached_type,
    rate_limit_reset_credits, spend_control, user_id). ``capped`` flips the
    rate limit to reached, the state in which a reset credit becomes usable.
    """
    out: dict[str, Any] = {
        "user_id": "user-placeholder",
        "account_id": ACCOUNT_ID,
        "email": EMAIL,
        "plan_type": "pro",
        "rate_limit": {
            "allowed": not capped,
            "limit_reached": capped,
            "primary_window": {
                "used_percent": 100 if capped else used_percent,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 3 * 86_400,
            },
            "secondary_window": None,
        },
        "code_review_rate_limit": None,
        "additional_rate_limits": [],
        "model_usage": {},
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "spend_control": {"reached": False, "individual_limit": None},
        "rate_limit_reached_type": (
            {"type": "rate_limit_reached", "details": "default"} if capped else None
        ),
        "promo": None,
    }
    if not omit_key:
        out["rate_limit_reset_credits"] = reset_credits
    return out


def quota(payload: dict[str, Any]) -> CodexAccountQuota:
    result = CodexAccountQuota.from_response(
        payload,
        credential_account_id=ACCOUNT_ID,
        observed_at=BASE_NOW,
        include_extra=False,
    )
    assert result is not None
    return result


def row_of(q: CodexAccountQuota, *, note: str = "", kind: str = "") -> AccountRow:
    return q.account_row(
        now=BASE_NOW + 60, slot=-1, alias="work", is_active=False, note=note, note_kind=kind
    )


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def test_a_banked_but_not_applicable_credit_reads_as_banked() -> None:
    """The probed ``belkins work`` shape: one credit held, none applicable on
    an account at 0 % of its week. It is good news, not a verdict: an info
    note, and the counters travel on the row for the notifier."""
    q = quota(body({"available_count": 1, "applicable_available_count": 0}))
    assert (q.reset_credits_available, q.reset_credits_usable) == (1, 0)
    assert q.info_notes(now=BASE_NOW + 60) == (BANKED_1,), q.info_notes(now=BASE_NOW + 60)
    row = row_of(q)
    assert (row.reset_credits_available, row.reset_credits_usable) == (1, 0)
    assert row.info_notes == (BANKED_1,), row.info_notes
    # Good news never becomes the row's standing verdict.
    assert row.attention_note == "" and row.attention_kind == ""


def test_a_usable_credit_is_the_first_info_note_on_a_capped_row() -> None:
    """A capped account holding an applicable credit: that is the one moment
    the credit changes what the user should do next, so it leads the notes -
    ahead of a credit balance that would otherwise be first."""
    payload = body({"available_count": 2, "applicable_available_count": 1}, capped=True)
    payload["credits"] = {"has_credits": True, "unlimited": False, "balance": "12"}
    q = quota(payload)
    assert (q.reset_credits_available, q.reset_credits_usable) == (2, 1)
    row = row_of(q, note=q.capped_note, kind=KIND_CRIT)
    assert row.info_notes, "a capped (crit) row keeps its info notes beside the bars"
    assert row.info_notes[0] == USABLE_1, row.info_notes
    assert "credits 12" in row.info_notes, row.info_notes
    assert not any(n.startswith("reset credits:") for n in row.info_notes), (
        "the usable note already says it; a second banked line would double-count"
    )
    assert (row.reset_credits_available, row.reset_credits_usable) == (2, 1)


def test_the_usable_note_is_plural_correct() -> None:
    q = quota(body({"available_count": 3, "applicable_available_count": 2}, capped=True))
    assert q.info_notes(now=BASE_NOW)[0] == (
        "↺ 2 reset credits usable now — use them in Codex"
    ), q.info_notes(now=BASE_NOW)
    banked = quota(body({"available_count": 3, "applicable_available_count": 0}))
    assert banked.info_notes(now=BASE_NOW) == ("reset credits: 3 (not usable now)",)


def test_unknown_counters_are_none_never_zero_and_carry_no_note() -> None:
    """``{}`` (the older Pro fixture), a missing key, ``null``, junk values,
    a negative count: all UNKNOWN. An invented 0 would tell the user they hold
    no reset credit on an account that may hold one."""
    cases = [
        body({}),
        body(omit_key=True),
        body(None),
        body("x"),
        body([1, 0]),
        body({"available_count": "x"}),
        body({"available_count": "x", "applicable_available_count": "y"}),
        body({"available_count": True, "applicable_available_count": False}),
        body({"available_count": -1, "applicable_available_count": -1}),
    ]
    for payload in cases:
        q = quota(payload)
        shown = payload.get("rate_limit_reset_credits", "<missing>")
        assert q.reset_credits_available is None, shown
        assert q.reset_credits_usable is None, shown
        assert q.info_notes(now=BASE_NOW) == (), (shown, q.info_notes(now=BASE_NOW))
        row = row_of(q)
        assert row.reset_credits_available is None and row.reset_credits_usable is None
    # One counter present, the other not: each is read on its own.
    half = quota(body({"available_count": 1}))
    assert (half.reset_credits_available, half.reset_credits_usable) == (1, None)
    assert half.info_notes(now=BASE_NOW) == (), "usable unknown -> no 'not usable' claim"


def test_zero_zero_is_a_known_zero_and_shows_nothing() -> None:
    """The three other probed accounts: ``{0, 0}`` is KNOWN (0, not None) but
    says nothing worth a line."""
    q = quota(body({"available_count": 0, "applicable_available_count": 0}))
    assert (q.reset_credits_available, q.reset_credits_usable) == (0, 0)
    assert q.info_notes(now=BASE_NOW) == ()
    row = row_of(q)
    assert (row.reset_credits_available, row.reset_credits_usable) == (0, 0)


def test_the_upsell_object_is_ignored_on_purpose() -> None:
    """``rate_limit_upsell`` is OpenAI marketing copy on a capped body; none
    of its text may reach a row."""
    payload = body({"available_count": 0, "applicable_available_count": 0}, capped=True)
    payload["rate_limit_upsell"] = {
        "banner_type": "pro_rate_limit_reached",
        "title": "SYNTHETIC upsell title",
        "description": "SYNTHETIC upsell description",
        "ctas": [{"action": "add_credits", "label": "SYNTHETIC cta"}],
        "reset_at": 1_000_000_000,
        "referral": None,
        "request_url": None,
    }
    row = row_of(quota(payload), note="capped rate_limit_reached", kind=KIND_CRIT)
    blob = json.dumps([row.info_notes, row.attention_note], ensure_ascii=False)
    assert "SYNTHETIC" not in blob, blob


# ---------------------------------------------------------------------------
# Dead credential
# ---------------------------------------------------------------------------


def test_a_dead_credential_row_carries_no_reset_note_or_counters() -> None:
    """``relogin`` / ``no access`` REPLACE the figures; a reset count read at
    the same instant as the withheld bars is withheld with them."""
    q = quota(body({"available_count": 2, "applicable_available_count": 1}, capped=True))
    for note in (NOTE_RELOGIN, NOTE_NO_ACCESS):
        row = row_of(q, note=note, kind=KIND_WARN)
        assert not any("reset credit" in n for n in row.info_notes), row.info_notes
        assert row.reset_credits_available is None, note
        assert row.reset_credits_usable is None, note


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------


def test_the_sidecar_round_trips_both_counters() -> None:
    q = quota(body({"available_count": 1, "applicable_available_count": 0}))
    record = json.loads(json.dumps(q.to_json()))
    assert record["reset_credits_available"] == 1
    assert record["reset_credits_usable"] == 0
    back = CodexAccountQuota.from_json(record)
    assert back is not None
    assert (back.reset_credits_available, back.reset_credits_usable) == (1, 0)
    assert back == q
    unknown = quota(body({}))
    back_unknown = CodexAccountQuota.from_json(json.loads(json.dumps(unknown.to_json())))
    assert back_unknown is not None
    assert (back_unknown.reset_credits_available, back_unknown.reset_credits_usable) == (
        None,
        None,
    )


def test_a_legacy_sidecar_record_loads_with_unknown_counters() -> None:
    """A record in exactly the pre-change ``to_json`` shape (no reset keys),
    as written by the running widget before this change."""
    legacy = {
        "account_id": ACCOUNT_ID,
        "email": EMAIL,
        "plan_type": "pro",
        "five_hour": None,
        "seven_day": {"used_percent": 12.0, "window_seconds": 604800, "reset_at": BASE_NOW + 3600},
        "scoped": [],
        "limit_reached": False,
        "allowed": True,
        "reached_type": None,
        "fetched_at": BASE_NOW,
        "credits_balance": 0.0,
        "credits_has": False,
        "credits_would_enable": False,
        "availability": [],
    }
    q = CodexAccountQuota.from_json(legacy)
    assert q is not None
    assert q.reset_credits_available is None and q.reset_credits_usable is None
    assert q.info_notes(now=BASE_NOW) == ()
    # Junk in a hand-edited sidecar is unknown too, never a crash.
    junk = dict(legacy, reset_credits_available="1", reset_credits_usable=-3)
    q2 = CodexAccountQuota.from_json(junk)
    assert q2 is not None
    assert q2.reset_credits_available is None and q2.reset_credits_usable is None


# ---------------------------------------------------------------------------
# Fleet heading
# ---------------------------------------------------------------------------


def fleet_row(
    slot: int,
    alias: str,
    *,
    pct: float | None,
    reset_in: float | None = None,
    kind: str = "",
    available: int | None = None,
    usable: int | None = None,
    disabled: bool = False,
) -> AccountRow:
    """One live per-account Codex row, the shape ``account_row`` emits (same
    as ``test_codex_accounts.codex_row`` plus the two reset counters)."""
    return AccountRow(
        slot=slot,
        alias=alias,
        email="",
        is_active=False,
        seven_day_pct=pct,
        seven_day_resets_at="Sep 28 10:00" if pct is not None else None,
        vendor=VENDOR_CODEX,
        switchable=False,
        disabled=disabled,
        plan_type="pro",
        usage_age_seconds=60.0,
        stale_after_seconds=CODEX_FETCH_STALE_SECONDS,
        attention_note="capped" if kind == KIND_CRIT else "",
        attention_kind=kind,
        soonest_reset_at=None if reset_in is None else BASE_NOW + reset_in,
        reset_credits_available=available,
        reset_credits_usable=usable,
    )


def test_the_fleet_heading_is_unchanged_when_every_counter_is_unknown_or_zero() -> None:
    """Byte-for-byte the pre-change output: ``Codex 3/3`` and the ``next``
    form, for all-None and for all-zero counters."""
    from cc_usage_widget import render
    from cc_usage_widget.app import _codex_fleet_heading

    for counters in ((None, None), (0, 0), (0, None), (None, 0)):
        a, u = counters
        free = tuple(
            fleet_row(-(i + 1), f"a{i}", pct=10.0, available=a, usable=u) for i in range(3)
        )
        assert _codex_fleet_heading(free, now=BASE_NOW) == "Codex 3/3", counters
        capped = (
            fleet_row(-1, "vlad", pct=100.0, reset_in=2 * 86_400, kind=KIND_CRIT,
                      available=a, usable=u),
            fleet_row(-2, "work", pct=100.0, reset_in=5 * 86_400, kind=KIND_CRIT,
                      available=a, usable=u),
        )
        clock = render.fleet_reset_label(BASE_NOW + 2 * 86_400, BASE_NOW)
        assert clock
        assert _codex_fleet_heading(capped, now=BASE_NOW) == f"Codex 0/2 · next {clock} (vlad)"


def test_the_fleet_heading_names_usable_then_banked_resets() -> None:
    from cc_usage_widget import render
    from cc_usage_widget.app import _codex_fleet_heading

    banked = (
        fleet_row(-1, "vlad", pct=10.0, available=0, usable=0),
        fleet_row(-2, "work", pct=0.0, available=1, usable=0),
    )
    assert _codex_fleet_heading(banked, now=BASE_NOW) == "Codex 2/2 · 1 reset banked"

    two_banked = banked + (fleet_row(-3, "gmail", pct=5.0, available=1, usable=None),)
    assert _codex_fleet_heading(two_banked, now=BASE_NOW) == "Codex 3/3 · 2 resets banked"

    # Usable outranks banked, and the suffix follows the "next" half.
    usable = (
        fleet_row(-1, "vlad", pct=100.0, reset_in=2 * 86_400, kind=KIND_CRIT,
                  available=2, usable=1),
        fleet_row(-2, "work", pct=0.0, available=1, usable=0),
    )
    clock = render.fleet_reset_label(BASE_NOW + 2 * 86_400, BASE_NOW)
    assert _codex_fleet_heading(usable, now=BASE_NOW) == (
        f"Codex 1/2 · next {clock} (vlad) · 1 reset usable"
    )
    two_usable = (
        fleet_row(-1, "vlad", pct=100.0, kind=KIND_CRIT, available=1, usable=1),
        fleet_row(-2, "work", pct=100.0, kind=KIND_CRIT, available=3, usable=1),
    )
    assert _codex_fleet_heading(two_usable, now=BASE_NOW) == "Codex 0/2 · 2 resets usable"

    # A disabled row is not part of the fleet the suffix describes.
    off = (
        fleet_row(-1, "vlad", pct=10.0, available=0, usable=0),
        fleet_row(-2, "work", pct=10.0, available=1, usable=1, disabled=True),
    )
    assert _codex_fleet_heading(off, now=BASE_NOW) == "Codex 2/2"


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
