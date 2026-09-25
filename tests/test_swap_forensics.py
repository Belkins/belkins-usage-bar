"""Claude-side forensics in the accounts adapter (usage-bar push 2026-09-25).

Each test pins one thing the 2026-09-24/25 evidence showed the widget getting
wrong or not saying at all:

* swap-1  a slot whose usage fetches have answered HTTP 403 for twelve days
          rendered ``5h 88% · 7d 0%`` with no cause and no remedy;
* swap-2  an engine switch that a running session reverted before the next
          pass was never reported (the expectation was consumed silently);
* swap-3  a config-only ``~/.claude.json`` rewrite (a "ghost flip") and a real
          ``cswap`` switch by another actor were reported identically, and a
          repeated ghost flip ("login fight") was never named;
* F2a     claude-swap stores extra-usage spend and no widget code read it;
* UX-1a   the engine's binding models were not exposed beside its threshold.

Run directly: ``CC_USAGE_WIDGET_NO_REVEAL=1 python tests/test_swap_forensics.py``.
Every identity below is SYNTHETIC (example.invalid); no real account is named.
"""

from __future__ import annotations

import os

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

import datetime as dt  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget.accounts import SwapAccountSource  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    ALERT_EXTERNAL_SWITCH,
    ALERT_GHOST_FLIP,
    ALERT_KINDS,
    FETCH_FAILING_MIN_FAILURES,
    GHOST_FLIP_FIGHT_COUNT,
    GHOST_FLIP_WINDOW_SECONDS,
    AccountRow,
)

# Reused, not copied: the W1 forensics fakes already model a backend whose
# snapshot pass reads back whatever the test set as the live slot.
from test_regressions import (  # noqa: E402
    _CLAUDE_SWAP_PRESENT,
    _CapturedLog,
    _EngineEvent,
    _forensics_source,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _local_epoch(year: int, month: int, day: int, hour: int, minute: int) -> float:
    """A fixed local wall-clock instant (in the past, so never date-fragile)."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def _iso_utc(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat()


# 2026-09-12 18:10 local: the last good fetch of the forbidden slot, and the
# reset instant its stored windows carry. Both already past, forever.
_LAST_GOOD_AT = _local_epoch(2026, 9, 12, 18, 10)
_PAST = _iso_utc(_LAST_GOOD_AT)


def _future_iso(seconds: float = 3 * 3600.0) -> str:
    return _iso_utc(time.time() + seconds)


def _fake_roll(window: Any, now: float) -> Any:
    """Upstream's weekly roll-forward, in miniature: a passed weekly reset
    becomes ``pct 0`` with the reset advanced into the future."""
    if not isinstance(window, dict):
        return window
    raw = window.get("resets_at")
    try:
        ts = dt.datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError):
        return window
    if ts > now:
        return window
    week = 7 * 86400.0
    rolled = dict(window)
    rolled["pct"] = 0.0
    rolled["resets_at"] = _iso_utc(ts + (int((now - ts) // week) + 1) * week)
    return rolled


def _row_backend() -> Any:
    """The attributes ``_build_row`` reads off a backend."""
    return SimpleNamespace(
        roll_weekly=_fake_roll,
        fresh_reset_strings=lambda window: None,  # -> raw resets_at verbatim
        sentinel_notes={},
    )


def _account(
    number: str,
    *,
    last_good: dict[str, Any] | None,
    last_error: str | None = None,
    failures: int = 0,
    fetched_at: float | None = None,
    sentinel: str | None = None,
) -> Any:
    return SimpleNamespace(
        number=number,
        email=f"synthetic-{number}@example.invalid",
        alias="",
        is_active=False,
        switchable=True,
        disabled=False,
        usage=SimpleNamespace(
            sentinel=sentinel,
            last_good=last_good,
            fetched_at=fetched_at,
            age_s=None,
            last_error=last_error,
            consecutive_failures=failures,
        ),
    )


def _stale_windows() -> dict[str, Any]:
    """slot 6's cache as of 2026-09-25: every stored reset already passed."""
    return {
        "five_hour": {"pct": 88.0, "resets_at": _PAST},
        "seven_day": {"pct": 41.0, "resets_at": _PAST},
        "scoped": [{"name": "Fable", "pct": 37.0, "resets_at": _PAST}],
    }


def _rows_source(*accounts: Any) -> tuple[SwapAccountSource, dict[int, AccountRow]]:
    source = SwapAccountSource(settings={"autoswitch_enabled": False})
    source._backend = _row_backend()
    snapshot = SimpleNamespace(active_number="1", accounts=tuple(accounts))
    rows = source._build_rows(snapshot)
    return source, {row.slot: row for row in rows}


# ---------------------------------------------------------------------------
# swap-1 - a forbidden slot says why, how to fix it, and stops faking figures
# ---------------------------------------------------------------------------


def test_a_forbidden_slot_says_why_and_how_to_fix_it() -> None:
    """usage.json slot 6 (2026-09-25): ``http-403``, 802 consecutive failures
    since Sep 12, and a menu row reading ``5h 88% · 7d 0%`` with no cause.

    The adapter must name the state (``fetch-failing``), the evidence (HTTP
    403, since when, how many polls) and the remedies - including the trap
    that ``cswap disable`` does NOT stop the polling - and must mark every
    window whose reset already passed as expired, so neither the dead 88% nor
    upstream's rolled-forward 0% is shown as a live reading.
    """
    forbidden = _account(
        "6",
        last_good=_stale_windows(),
        last_error="http-403",
        failures=802,
        fetched_at=_LAST_GOOD_AT,
    )
    source, rows = _rows_source(forbidden)

    kinds = source.sentinel_kinds()
    assert kinds.get(6) == "fetch-failing", kinds
    note = source.sentinels()[6]
    for needle in (
        "HTTP 403",
        "since Sep 12",
        "802 failed polls",
        "synthetic-6@example.invalid",
        "cswap add",
        "cswap remove 6",
        "cswap disable",
        "only takes it out of rotation; it is still polled",
    ):
        assert needle in note, (needle, note)

    row = rows[6]
    for window in ("five_hour", "seven_day", "Fable"):
        assert window in row.expired_windows, (window, row.expired_windows)
    # Annotate, never rewrite: the upstream figures are untouched.
    assert row.five_hour_pct == 88.0, row.five_hour_pct

    # The title shows the cause in the active slot's place, not a percentage.
    assert app_mod._title_note(note, kinds[6]) == "⚠ 403"


def test_the_fetch_failing_gate_ignores_auth_throttle_network_and_blips() -> None:
    """Only a standing 4xx refusal is this state. 401 is a credential problem
    upstream already names; 429 is throttling that heals itself; network and
    timeout are ours, not the account's; fewer than the minimum consecutive
    failures is a blip. None of them may produce the note."""
    # A 429 is judged against a recent good fetch: throttling that heals.
    # (A 429 with no good fetch for a day is the cold-start case below.)
    recent = time.time() - 600.0
    cases = [
        ("http-401", 50, _LAST_GOOD_AT),
        ("http-429", 50, recent),
        ("network", 50, _LAST_GOOD_AT),
        ("timeout", 50, _LAST_GOOD_AT),
        (None, 50, _LAST_GOOD_AT),
        ("http-403", FETCH_FAILING_MIN_FAILURES - 1, _LAST_GOOD_AT),
        ("http-500", 50, _LAST_GOOD_AT),
    ]
    for error, failures, fetched_at in cases:
        account = _account(
            "6", last_good=_stale_windows(), last_error=error, failures=failures,
            fetched_at=fetched_at,
        )
        source, _ = _rows_source(account)
        assert source.sentinel_kinds() == {}, (error, failures, source.sentinel_kinds())

    # Any other 4xx at the threshold IS the state, and says its own code.
    account = _account(
        "6", last_good=_stale_windows(), last_error="http-404",
        failures=FETCH_FAILING_MIN_FAILURES, fetched_at=_LAST_GOOD_AT,
    )
    source, _ = _rows_source(account)
    assert source.sentinel_kinds() == {6: "fetch-failing"}, source.sentinel_kinds()
    assert "HTTP 404" in source.sentinels()[6], source.sentinels()
    assert app_mod._title_note(source.sentinels()[6], "fetch-failing") == "⚠ 404"


def test_the_hourly_429_backoff_does_not_blink_the_note_off() -> None:
    """Live usage.json, 2026-09-25 10:24: slot 6 read ``lastError http-429``
    with 803 consecutive failures - upstream's usage-endpoint budget backoff,
    which follows every four 403s and lasts an hour. Gating on the LAST error
    alone showed the note well under half the time. A 429 that follows a
    standing refusal with no good fetch in between keeps it; a good fetch (or
    a 429 with no refusal before it) does not."""
    source = SwapAccountSource(settings={"autoswitch_enabled": False})
    source._backend = _row_backend()

    def build(error: str | None, failures: int, fetched_at: float) -> dict[int, str]:
        account = _account(
            "6", last_good=_stale_windows(), last_error=error, failures=failures,
            fetched_at=fetched_at,
        )
        source._build_rows(SimpleNamespace(active_number="1", accounts=(account,)))
        return source.sentinel_kinds()

    assert build("http-403", 802, _LAST_GOOD_AT) == {6: "fetch-failing"}
    assert build("http-429, retry-after 3600s (usage-endpoint budget reached; backing off)",
                 803, _LAST_GOOD_AT) == {6: "fetch-failing"}
    assert "HTTP 403" in source.sentinels()[6], source.sentinels()
    assert build("timeout", 804, _LAST_GOOD_AT) == {6: "fetch-failing"}
    # a good fetch in between ends it, even if the next poll is throttled
    assert build("http-429", 1, _LAST_GOOD_AT + 86400.0) == {}
    assert build(None, 0, _LAST_GOOD_AT + 86400.0) == {}


def test_a_widget_started_inside_the_429_backoff_still_names_the_refusal() -> None:
    """SMC-4. usage.json slot 6 on 2026-09-25: ``lastError http-429``, 803
    consecutive failures, ``fetchedAt`` Sep 12. The standing refusal was
    remembered only in memory, from a 403 this process saw; a widget started
    inside the backoff hour showed no note and a live-looking ``5h 88%`` for
    up to an hour. The persisted entry alone proves it: days of failures and
    no good fetch. The note says only what is known - refused for N days,
    last error the 429 backoff."""
    account = _account(
        "6",
        last_good=_stale_windows(),
        last_error="http-429, retry-after 3600s (usage-endpoint budget reached; backing off)",
        failures=803,
        fetched_at=_LAST_GOOD_AT,
    )
    source, rows = _rows_source(account)  # a fresh source: no 403 ever seen
    assert source.sentinel_kinds() == {6: "fetch-failing"}, source.sentinel_kinds()
    note = source.sentinels()[6]
    days = int((time.time() - _LAST_GOOD_AT) // 86400)
    for needle in (
        f"usage refused for {days} days since Sep 12",
        "last error HTTP 429, upstream backoff",
        "803 failed polls",
        "cswap remove 6",
    ):
        assert needle in note, (needle, note)
    assert "forbidden" not in note, "no 403 was seen; the note must not claim one"
    for window in ("five_hour", "seven_day", "Fable"):
        assert window in rows[6].expired_windows, (window, rows[6].expired_windows)
    assert app_mod._title_note(note, "fetch-failing") == "⚠ 429"

    # Not for a throttle that follows a recent good fetch, nor for a blip.
    for failures, fetched_at in ((803, time.time() - 600.0),
                                 (FETCH_FAILING_MIN_FAILURES - 1, _LAST_GOOD_AT)):
        blip = _account("6", last_good=_stale_windows(), last_error="http-429",
                        failures=failures, fetched_at=fetched_at)
        source, _ = _rows_source(blip)
        assert source.sentinel_kinds() == {}, (failures, source.sentinel_kinds())


def test_an_upstream_sentinel_wins_over_the_fetch_failing_note() -> None:
    """The note is the fallback for a slot upstream has no word for; a real
    sentinel (``re-login needed``) keeps its own key and prose."""
    account = _account(
        "6", last_good=_stale_windows(), last_error="http-403", failures=802,
        fetched_at=_LAST_GOOD_AT, sentinel="re-login needed",
    )
    source, _ = _rows_source(account)
    assert source.sentinel_kinds() == {6: "re-login needed"}, source.sentinel_kinds()


def test_the_note_clears_as_soon_as_the_slot_recovers() -> None:
    """Lifecycle: derived per build from the cache entry, nothing persisted -
    the pass after a good fetch shows figures again, not a stale warning."""
    failing = _account(
        "6", last_good=_stale_windows(), last_error="http-403", failures=802,
        fetched_at=_LAST_GOOD_AT,
    )
    source, _ = _rows_source(failing)
    assert source.sentinel_kinds() == {6: "fetch-failing"}

    healthy = _account(
        "6",
        last_good={
            "five_hour": {"pct": 12.0, "resets_at": _future_iso()},
            "seven_day": {"pct": 30.0, "resets_at": _future_iso(3 * 86400.0)},
        },
        last_error=None,
        failures=0,
        fetched_at=time.time(),
    )
    rows = source._build_rows(SimpleNamespace(active_number="1", accounts=(healthy,)))
    assert source.sentinel_kinds() == {}, source.sentinel_kinds()
    assert source.sentinels() == {}, source.sentinels()
    assert rows[0].expired_windows == (), rows[0].expired_windows


def test_expired_windows_on_healthy_rows_follow_the_stored_reset() -> None:
    """A future reset stays live (``()`` unchanged). A healthy slot whose 5h
    reset passed (idle since) has an ENDED 5h window - expired. A weekly
    window upstream rolled forward is upstream's honest ``0%`` for a healthy
    slot and stays live; only a failing slot's roll is suspect."""
    fresh = _account(
        "2",
        last_good={
            "five_hour": {"pct": 12.0, "resets_at": _future_iso()},
            "seven_day": {"pct": 30.0, "resets_at": _future_iso(3 * 86400.0)},
            "scoped": [{"name": "Fable", "pct": 9.0, "resets_at": _future_iso(86400.0)}],
        },
        fetched_at=time.time(),
    )
    idle = _account("3", last_good=_stale_windows(), fetched_at=_LAST_GOOD_AT)
    _, rows = _rows_source(fresh, idle)
    assert rows[2].expired_windows == (), rows[2].expired_windows
    assert rows[3].expired_windows == ("five_hour",), rows[3].expired_windows


# ---------------------------------------------------------------------------
# swap-2 - a reverted engine switch is reported, not silently dropped
# ---------------------------------------------------------------------------


def test_an_engine_switch_that_is_reverted_before_the_next_pass_is_reported() -> None:
    """2026-09-24 19:14->19:24 and 20:24->20:25: the engine switched, a
    running session put the old login back before the widget's next pass,
    and the one-shot expectation was consumed with no word anywhere - while
    the recent-events block claimed the switch had happened."""
    source, backend = _forensics_source("1")
    source._on_engine_event(
        _EngineEvent(
            "switch",
            "Switched Account-1 -> Account-3 (synthetic-3@example.invalid) (at-limit)",
            to_ref={"number": "3", "email": "synthetic-3@example.invalid"},
        )
    )
    # the snapshot never shows slot 3: it was reverted before this pass read it
    with _CapturedLog() as captured:
        source._drain_events()
        source.refresh(force=True)

    alert = source.current_alert()
    assert alert is not None, "a reverted switch must reach the menu bar"
    assert alert[0] == ALERT_EXTERNAL_SWITCH, alert
    assert any("reverted" in line for line in source.recent_events()), source.recent_events()
    assert any(
        "switch to 3 did not stick: active is 1" in m for m in captured.messages()
    ), captured.messages()


def test_a_skipped_pass_or_a_refused_switch_reports_no_revert() -> None:
    """The check runs only on a completed pass, against an expectation a
    switch that really happened set. A pass skipped because another worker
    holds the take lock consumes nothing; a refused manual click sets nothing."""
    source, backend = _forensics_source("1")
    source._on_engine_event(
        _EngineEvent("switch", "Switched Account-1 -> Account-3", to_ref={"number": "3"})
    )
    assert source._take_lock.acquire(blocking=False)
    try:
        source._drain_events()  # its own refresh is skipped: lock is busy
        source.refresh(force=True)
    finally:
        source._take_lock.release()
    assert source.current_alert() is None, source.current_alert()
    assert not any("reverted" in line for line in source.recent_events())

    source, backend = _forensics_source("1")
    backend.switcher.switch_to = lambda identifier, json_output=False: {
        "switched": False,
        "reason": "refused",
    }
    with _CapturedLog() as captured:
        assert source.switch_to("3") is False
        source.refresh(force=True)
    assert source.current_alert() is None, source.current_alert()
    assert not any("reverted" in line for line in source.recent_events())
    assert not any("did not stick" in m for m in captured.messages())


# ---------------------------------------------------------------------------
# swap-3 - ghost flip vs external switch, and the login fight
# ---------------------------------------------------------------------------


def _log_stamp(epoch: float) -> str:
    """claude-swap.log's ``%(asctime)s`` (local time, comma milliseconds)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) + ",302"


def _ghost_source(backup_dir: Path, active: str = "5") -> tuple[Any, Any]:
    source, backend = _forensics_source(active)
    backend.backup_dir = backup_dir
    source._alias_by_slot = {1: "synthetic-one", 5: "synthetic-five"}
    return source, backend


def _flip(source: Any, backend: Any, to: str) -> tuple[str, str] | None:
    backend.snapshot.active_number = to
    source.refresh(force=True)
    return source.current_alert()


def _engine_back_to(source: Any, backend: Any, slot: str) -> None:
    """Our own engine switch - claimed, so never reported."""
    source._on_engine_event(
        _EngineEvent("switch", f"Switched -> Account-{slot}", to_ref={"number": slot})
    )
    backend.snapshot.active_number = slot
    source._drain_events()


def test_a_config_only_flip_is_named_ghost_and_a_cswap_switch_is_named_external() -> None:
    """2026-09-24: six flips back to account 1, none with a ``Switched from
    account`` line in claude-swap.log - running Claude Code sessions rewriting
    ``~/.claude.json``. 2026-09-25 09:56 had that line - a real cswap actor.
    One label for both hid which remedy applies."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "claude-swap.log"

        # A: claude-swap.log records the switch -> another cswap actor.
        log.write_text(
            f"{_log_stamp(time.time())} - INFO - Switched from account 5 to 1\n",
            encoding="utf-8",
        )
        source, backend = _ghost_source(Path(tmp))
        alert = _flip(source, backend, "1")
        assert alert is not None and alert[0] == ALERT_EXTERNAL_SWITCH, alert

        # B: the same flip with no such line -> a ghost flip.
        log.write_text("", encoding="utf-8")
        source, backend = _ghost_source(Path(tmp))
        with _CapturedLog() as captured:
            alert = _flip(source, backend, "1")
        assert alert is not None and alert[0] == ALERT_GHOST_FLIP, alert
        assert (
            "~/.claude.json rewritten to synthetic-one by a running Claude Code "
            "session; keychain still holds synthetic-five"
        ) in alert[1], alert[1]
        assert "login fight" not in alert[1], alert[1]
        assert source.recent_events()[-1] == alert[1], source.recent_events()
        assert any("outside the widget" in m for m in captured.messages())
        assert app_mod._title_alert(ALERT_GHOST_FLIP) == "⚠ ghost"
        assert ALERT_GHOST_FLIP in ALERT_KINDS


def test_three_ghost_flips_in_an_hour_are_a_login_fight_with_remedies() -> None:
    """Each ghost flip was repaired by a fleet failover (20:24, 20:25, 20:38),
    and the next session rewrite undid it. At the third flip onto one slot in
    an hour the alert must say so and name the remedies; it ages out."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "claude-swap.log"
        log.write_text("", encoding="utf-8")
        source, backend = _ghost_source(Path(tmp))
        clock = [time.time()]
        source._wall = lambda: clock[0]

        lines = []
        for _ in range(GHOST_FLIP_FIGHT_COUNT):
            alert = _flip(source, backend, "1")
            assert alert is not None and alert[0] == ALERT_GHOST_FLIP, alert
            lines.append(alert[1])
            # our engine takes it back; its own log line must not confuse the
            # classifier (it records 1 -> 5, not the 5 -> 1 flip)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(f"{_log_stamp(clock[0])} - INFO - Switched from account 1 to 5\n")
            _engine_back_to(source, backend, "5")
            clock[0] += 600.0

        assert all("login fight" not in line for line in lines[:-1]), lines
        fight = lines[-1]
        for needle in (
            "login fight",
            "synthetic-one",
            "per-terminal profiles",
            "cswap map",
            "cswap run",
            "--share-history",
            "restart",
        ):
            assert needle in fight, (needle, fight)

        # Standing: our engine's repair switch (which retracts the per-flip
        # alert) must not retract the fight while the hour still holds it.
        standing = source.current_alert()
        assert standing is not None and standing[0] == ALERT_GHOST_FLIP, standing
        assert "login fight" in standing[1], standing

        # The deque is in-memory and windowed: an hour later it starts over.
        clock[0] += GHOST_FLIP_WINDOW_SECONDS + 1.0
        assert source.current_alert() is None, "the fight must age out on its own"
        alert = _flip(source, backend, "1")
        assert alert is not None and alert[0] == ALERT_GHOST_FLIP, alert
        assert "login fight" not in alert[1], alert[1]

        # Flips spread wider than the window never add up to a fight: two,
        # an hour of quiet, two more is two in any hour, not four.
        source, backend = _ghost_source(Path(tmp))
        source._wall = lambda: clock[0]
        for gap in (60.0, GHOST_FLIP_WINDOW_SECONDS + 1.0, 60.0, 0.0):
            alert = _flip(source, backend, "1")
            assert "login fight" not in alert[1], alert[1]
            _engine_back_to(source, backend, "5")
            clock[0] += gap


def test_engine_switches_reverted_before_the_next_pass_count_toward_the_login_fight() -> None:
    """SMC-2, the 2026-09-24 shape: the engine switched 1 -> 5 at 20:24:38
    and a running session had put slot 1 back by 20:25:37, before any pass
    saw slot 5. Such a revert took the reverted-switch branch alone, so it
    never reached the ghost counter and three of them in an hour raised no
    login fight. A config-only revert (no ``Switched from account 5 to 1``
    in claude-swap.log - only the engine's own 1 -> 5) is a ghost flip."""
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "claude-swap.log"
        log.write_text("", encoding="utf-8")
        source, backend = _ghost_source(Path(tmp), active="1")
        clock = [time.time()]
        source._wall = lambda: clock[0]

        alerts = []
        for _ in range(GHOST_FLIP_FIGHT_COUNT):
            with log.open("a", encoding="utf-8") as fh:  # the engine's own switch
                fh.write(f"{_log_stamp(clock[0])} - INFO - Switched from account 1 to 5\n")
            source._on_engine_event(
                _EngineEvent("switch", "Switched Account-1 -> Account-5",
                             to_ref={"number": "5"})
            )
            # reverted before the pass: the snapshot still reads slot 1
            with _CapturedLog() as captured:
                source._drain_events()
                source.refresh(force=True)
            assert any("switch to 5 did not stick" in m for m in captured.messages())
            alerts.append(source.current_alert())
            clock[0] += 600.0

        first = alerts[0]
        assert first is not None and first[0] == ALERT_GHOST_FLIP, first
        assert "switch \u21925 reverted" in first[1], first[1]
        assert "~/.claude.json rewritten to synthetic-one" in first[1], first[1]
        fight = alerts[-1]
        assert fight is not None and fight[0] == ALERT_GHOST_FLIP, fight
        assert "login fight" in fight[1] and "synthetic-one" in fight[1], fight[1]

    # A revert claude-swap.log records (another cswap actor) keeps the
    # generic line and never feeds the fight counter.
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "claude-swap.log"
        source, backend = _ghost_source(Path(tmp), active="1")
        source._on_engine_event(
            _EngineEvent("switch", "Switched Account-1 -> Account-5", to_ref={"number": "5"})
        )
        log.write_text(
            f"{_log_stamp(time.time())} - INFO - Switched from account 5 to 1\n",
            encoding="utf-8",
        )
        source._drain_events()
        alert = source.current_alert()
        assert alert is not None and alert[0] == ALERT_EXTERNAL_SWITCH, alert
        assert "switch \u21925 reverted to 1" in alert[1], alert[1]
        assert source._ghost_flips == {}, source._ghost_flips


def test_the_log_read_is_tail_only_mtime_gated_and_fails_to_the_generic_alert() -> None:
    """The classifier must cost nothing in the steady state and never guess:
    an unreadable log means "we cannot tell" -> today's external alert."""
    # Unreadable: the backup dir does not exist.
    with tempfile.TemporaryDirectory() as tmp:
        source, backend = _ghost_source(Path(tmp) / "missing")
        alert = _flip(source, backend, "1")
        assert alert is not None and alert[0] == ALERT_EXTERNAL_SWITCH, alert

    # Tail-only: a matching line older than the last 64 KB is not read.
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "claude-swap.log"
        filler = "2026-01-01 00:00:00,000 - DEBUG - " + "x" * 100 + "\n"
        log.write_text(
            f"{_log_stamp(time.time())} - INFO - Switched from account 5 to 1\n"
            + filler * 800,
            encoding="utf-8",
        )
        source, backend = _ghost_source(Path(tmp))
        alert = _flip(source, backend, "1")
        assert alert is not None and alert[0] == ALERT_GHOST_FLIP, alert

    # mtime-gated: content changed under an unchanged mtime is not re-read.
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "claude-swap.log"
        log.write_text("", encoding="utf-8")
        os.utime(log, (1_000_000, 1_000_000))
        source, backend = _ghost_source(Path(tmp))
        assert _flip(source, backend, "1")[0] == ALERT_GHOST_FLIP
        _engine_back_to(source, backend, "5")
        log.write_text(
            f"{_log_stamp(time.time())} - INFO - Switched from account 5 to 1\n",
            encoding="utf-8",
        )
        os.utime(log, (1_000_000, 1_000_000))
        assert _flip(source, backend, "1")[0] == ALERT_GHOST_FLIP, "re-read an unchanged mtime"
        # ...and a changed mtime IS re-read.
        _engine_back_to(source, backend, "5")
        os.utime(log, None)
        assert _flip(source, backend, "1")[0] == ALERT_EXTERNAL_SWITCH


# ---------------------------------------------------------------------------
# F2a - extra-usage spend from the claude-swap cache
# ---------------------------------------------------------------------------


def _spend_row(spend: Any) -> AccountRow:
    last_good: dict[str, Any] = {"five_hour": {"pct": 10.0, "resets_at": _future_iso()}}
    if spend is not None:
        last_good["spend"] = spend
    _, rows = _rows_source(_account("1", last_good=last_good, fetched_at=time.time()))
    return rows[1]


def test_extra_usage_spend_reaches_the_row_and_a_partial_entry_is_never_zero() -> None:
    """oauth.py stores ``lastGood['spend']`` (slot 1 at 95.6% of its cap on
    2026-09-25) and nothing read it. Read-only pass-through; a partial entry
    is ALL ``None`` - a missing figure rendered as ``$0`` would be a lie."""
    resets = _future_iso(6 * 86400.0)
    row = _spend_row(
        {"used": 480.00, "limit": 500.00, "pct": 95.6, "currency": "USD", "resets_at": resets}
    )
    assert row.spend_used == 480.00, row.spend_used
    assert row.spend_limit == 500.00, row.spend_limit
    assert row.spend_pct == 95.6, row.spend_pct
    assert row.spend_currency == "USD", row.spend_currency
    assert row.spend_resets_at == resets, row.spend_resets_at

    # currency verbatim; no stored reset -> that one field alone is None
    row = _spend_row({"used": 1.0, "limit": 2.0, "pct": 50.0, "currency": "EUR"})
    assert (row.spend_used, row.spend_currency, row.spend_resets_at) == (1.0, "EUR", None)

    for partial in (
        None,
        {"used": 480.00, "pct": 95.6, "currency": "USD"},
        {"used": 480.00, "limit": 500.00, "pct": 95.6},
        {"used": "480.00", "limit": 500.00, "pct": 95.6, "currency": "USD"},
        {"used": True, "limit": 500.00, "pct": 95.6, "currency": "USD"},
        "junk",
    ):
        row = _spend_row(partial)
        spend = (row.spend_used, row.spend_limit, row.spend_pct, row.spend_currency,
                 row.spend_resets_at)
        assert spend == (None,) * 5, (partial, spend)

    # Rows that never pass through the adapter (Codex) are unchanged.
    plain = AccountRow(slot=0, alias="Codex", email="", is_active=False)
    assert plain.spend_used is None and plain.spend_currency is None


# ---------------------------------------------------------------------------
# UX-1a - the engine's binding models, cached beside its threshold
# ---------------------------------------------------------------------------


def test_cached_autoswitch_models_come_from_the_engine_policy_without_a_read() -> None:
    """The engine binds on ``autoswitch.model`` (Fable) as well as 5h/7d; the
    title needs the same models to bind on the same window. Same contract as
    ``cached_autoswitch_threshold``: captured when the engine is built, no
    file read on the cached path, ``()`` until an engine exists."""
    with tempfile.TemporaryDirectory() as tmp:
        reads = [0]

        def load_policy(_backup_dir: Path) -> Any:
            reads[0] += 1
            return SimpleNamespace(threshold=85.0, model="Fable", interval_seconds=60)

        backend = SimpleNamespace(
            backup_dir=Path(tmp),
            settings_path=lambda d: Path(d) / "settings.json",
            load_policy=load_policy,
            engine_cls=lambda switcher, policy, callback, dry_run=False: SimpleNamespace(
                settings=policy, stop=lambda: None
            ),
            switcher=SimpleNamespace(clear_poll_policy_inputs=lambda: None),
        )
        source = SwapAccountSource(settings={"autoswitch_enabled": True})
        source._backend_or_none = lambda: backend

        assert source.cached_autoswitch_models() == ()
        assert source._ensure_engine() is not None
        assert reads[0] == 1, reads

        # The model names go through upstream's own parser, so without
        # claude_swap (CI) the widget legitimately compares 5h/7d only.
        expected = ("Fable",) if _CLAUDE_SWAP_PRESENT else ()
        assert source.cached_autoswitch_models() == expected, (expected, _CLAUDE_SWAP_PRESENT)
        assert source.cached_autoswitch_threshold() == 85.0
        for _ in range(3):
            source.cached_autoswitch_models()
        assert reads[0] == 1, "the cached path must not read the policy"


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


def _uncollected_tests(collected: list[tuple[str, Any]]) -> list[str]:
    """Tests declared in this file but never collected (below the guard)."""
    source = Path(__file__).read_text(encoding="utf-8")
    declared = re.findall(r"^def (test_\w+)", source, re.MULTILINE)
    found = {name for name, _ in collected}
    return [name for name in declared if name not in found]


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
    orphans = _uncollected_tests(tests)
    if orphans:
        print("ERROR: never collected (below the __main__ guard): " + ", ".join(orphans))
    total = len(tests)
    print(f"\n{total - len(failures)} passed, {len(failures)} failed, out of {total}")
    return 1 if (failures or orphans) else 0


if __name__ == "__main__":
    raise SystemExit(main())
