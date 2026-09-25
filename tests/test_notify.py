"""Tests for ``cc_usage_widget.notify`` — roadmap item 7.

The feature's whole value is "once, at the moment it changed". Its whole risk
is the opposite pair: a notification that repeats every 60 s until it is muted
forever, and a bot token that escapes into a log. Both are asserted here rather
than described.

What is deliberately real and what is a seam
--------------------------------------------

* **Real**: the ledger file (a real path in a ``TemporaryDirectory``), the
  credential file and its permission bits, ``notify.json`` parsing, the CLI,
  the URL ``TelegramSender`` builds, and the transition rules themselves.
* **Seams**: the two senders and the clock. Nothing here reaches the network or
  the Notification Centre, and ``urllib`` is never patched — ``TelegramSender``
  takes its opener as a constructor argument, so the request object the real
  code builds is the one the test inspects.

The last section is the negative control: the same cycle run through a sender
that logs the URL it was given (a token lives in that URL), where the leak
check MUST fail. Without it the privacy assertions above would pass just as
well over a feature that never ran.

Run directly (pytest is absent from the claude-swap venv)::

    ~/.local/share/uv/tools/claude-swap/bin/python tests/test_notify.py
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(REPO_ROOT))

from cc_usage_widget import notify as notify_mod  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_BOUNDS,
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    normalize_settings,
)

TOKEN_CANARY = "SECRET_BOT_TOKEN_CANARY_7C2D9_do_not_leak"
"""Not a plausible bot token, so a substring hit cannot be a coincidence."""

CHAT_ID = "424242"


# ---------------------------------------------------------------------------
# Fixtures: snapshots built by hand, no corpus, no widget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snap:
    """The subset of ``UiSnapshot`` the notifier reads, plus ``audit_note``.

    A stand-in rather than the real class for one reason: ``audit_note`` is
    B1's field (roadmap item 2) and does not exist on this branch, and
    ``UiSnapshot`` is a frozen ``slots`` dataclass, so it cannot be given one
    from a test. ``notify`` reads every snapshot field through ``getattr`` with
    a default precisely so it works on both builds; this class is what asserts
    that it does.
    """

    accounts: tuple[AccountRow, ...] = ()
    quota_rows: tuple[AccountRow, ...] = ()
    account_notes: dict[int, str] = field(default_factory=dict)
    account_note_kinds: dict[int, str] = field(default_factory=dict)
    audit_note: str | None = None


def row(
    *,
    slot: int = 1,
    alias: str = "vlad",
    weekly: float | None = None,
    five_hour: float | None = None,
    vendor: str = VENDOR_CODEX,
    note: str = "",
    kind: str = "",
    expired: tuple[str, ...] = (),
    resets: str | None = None,
) -> AccountRow:
    """One account row. Defaults to a Codex pseudo-account, which is the shape
    the four tracked accounts actually have."""
    return AccountRow(
        slot=slot,
        alias=alias,
        email="",
        is_active=False,
        five_hour_pct=five_hour,
        seven_day_pct=weekly,
        seven_day_resets_at=resets,
        vendor=vendor,
        switchable=vendor != VENDOR_CODEX,
        attention_note=note,
        attention_kind=kind,
        expired_windows=expired,
    )


def snap(*rows: AccountRow, **extra: Any) -> Snap:
    return Snap(quota_rows=tuple(rows), **extra)


class RecordingSender:
    """A sender that keeps what it was asked to send. Never touches anything."""

    def __init__(self) -> None:
        self.events: list[notify_mod.Event] = []

    def send(self, event: notify_mod.Event) -> bool:
        self.events.append(event)
        return True


def make_notifier(
    root: Path,
    *,
    settings: dict[str, Any] | None = None,
    clock: Any = None,
    telegram: Any = None,
) -> tuple[notify_mod.Notifier, RecordingSender]:
    """A notifier over real files in *root*, sending into a recorder.

    ``dispatch`` is synchronous so an assertion can read the recorder on the
    next line; the daemon-thread behaviour of the production dispatcher has its
    own test below.
    """
    mac = RecordingSender()
    resolved = normalize_settings(settings or {})
    notifier = notify_mod.Notifier(
        settings=lambda: resolved,
        ledger_path=root / "notify_state.json",
        credentials_path=root / "notify.json",
        mac_sender=mac,
        telegram_sender=telegram,
        clock=clock or (lambda: 1_000.0),
        dispatch=lambda work: work(),
    )
    return notifier, mac


# ---------------------------------------------------------------------------
# 1. Transition detection is pure and covers every kind
# ---------------------------------------------------------------------------


def test_a_window_crossing_the_threshold_upward_notifies_once() -> None:
    events = notify_mod.detect(snap(row(weekly=80.0)), snap(row(weekly=87.0)), threshold=85.0)
    assert [e.kind for e in events] == ["threshold"]
    assert "87%" in events[0].subtitle and "weekly" in events[0].subtitle
    assert events[0].severity == notify_mod.SEVERITY_WARN


def test_a_window_reaching_a_hundred_percent_notifies_as_a_wall_not_a_threshold() -> None:
    """100 % is a different fact from 85 %: there is nothing left, and the only
    useful detail is when it comes back."""
    events = notify_mod.detect(
        snap(row(weekly=90.0)), snap(row(weekly=100.0, resets="Sat 09:00")), threshold=85.0
    )
    assert [e.kind for e in events] == ["capped"]
    assert events[0].severity == notify_mod.SEVERITY_CRIT
    assert events[0].message == "resets Sat 09:00", "the verbatim reset must reach the message"


def test_a_window_that_resets_notifies_that_the_account_is_back() -> None:
    events = notify_mod.detect(snap(row(weekly=100.0)), snap(row(weekly=0.0)), threshold=85.0)
    assert [e.kind for e in events] == ["back"]
    assert events[0].message == "back: vlad 0% weekly"


def test_a_drop_that_stops_above_the_recovery_floor_is_not_back_yet() -> None:
    """Falling from 100 % to 60 % is not a reset — it is a wider window or a
    correction, and announcing "back" there would be a number we invented."""
    assert notify_mod.detect(snap(row(weekly=100.0)), snap(row(weekly=60.0)), threshold=85.0) == []


def test_a_warn_or_crit_attention_note_notifies_and_an_info_one_does_not() -> None:
    """``attention_kind`` classifies, never the wording (contracts.AccountRow)."""
    warned = notify_mod.detect(
        snap(row(weekly=10.0)), snap(row(weekly=10.0, note="relogin", kind="warn"))
    )
    assert [e.kind for e in warned] == ["attention"] and warned[0].subtitle == "relogin"

    informed = notify_mod.detect(
        snap(row(weekly=10.0)),
        snap(row(weekly=10.0, note="awaiting first reading", kind="info")),
    )
    assert informed == [], "an info note is not a reason to interrupt anybody"


def test_a_self_audit_note_notifies_even_though_the_field_may_not_exist_yet() -> None:
    """B1 adds ``UiSnapshot.audit_note``; this module must work either way."""
    before = snap(row(weekly=10.0))
    after = snap(row(weekly=10.0), audit_note="audit: 2 cells drifted — rebuilt")
    events = notify_mod.detect(before, after)
    assert [e.kind for e in events] == ["audit"]
    assert "2 cells drifted" in events[0].subtitle

    from cc_usage_widget.app import UiSnapshot  # a build with no such field

    assert notify_mod.detect(UiSnapshot(), UiSnapshot()) == []


def test_a_claude_swap_sentinel_note_notifies() -> None:
    before = snap(row(weekly=10.0))
    after = snap(
        row(weekly=10.0),
        account_notes={2: "re-login needed"},
        account_note_kinds={2: "relogin"},
    )
    events = notify_mod.detect(before, after)
    assert [e.kind for e in events] == ["sentinel"]
    assert "re-login needed" in events[0].subtitle


def test_a_standing_note_that_is_reworded_is_not_a_second_notification() -> None:
    """The ledger key is the row and the KIND, never the wording.

    This is the bug the ``Event`` docstring already promised was impossible.
    ``attention_note`` carries a live countdown - ``relogin in 1d 4h``, then
    ``relogin in 1d 3h`` - and a key built from that text is a NEW key on every
    tick, so the ledger (working perfectly) suppresses nothing and the operator
    is interrupted about one standing fault once a minute until they turn the
    feature off. Same shape for a claude-swap sentinel note.
    """
    before = snap(row(weekly=10.0))
    first = snap(row(weekly=10.0, note="relogin in 1d 4h", kind="warn"))
    later = snap(row(weekly=10.0, note="relogin in 1d 3h", kind="warn"))

    opened = notify_mod.detect(before, first)
    assert [e.kind for e in opened] == ["attention"]
    # The rewording is not a transition: nothing new became true.
    assert notify_mod.detect(first, later) == [], "a countdown is not news"
    # ... and both readings carry the SAME ledger key, which is what makes the
    # suppression survive a restart rather than only this pair of snapshots.
    assert notify_mod.detect(before, later)[0].key == opened[0].key
    # The words are still delivered - they just travel in the subtitle.
    assert opened[0].subtitle == "relogin in 1d 4h"
    assert notify_mod.detect(before, later)[0].subtitle == "relogin in 1d 3h"
    # A key that names the kind, and no part of the sentence.
    assert opened[0].key.endswith(":warn"), opened[0].key
    assert "relogin" not in opened[0].key, opened[0].key

    # A change of KIND is a different fact and does notify.
    escalated = snap(row(weekly=10.0, note="relogin in 1d 3h", kind="crit"))
    assert [e.kind for e in notify_mod.detect(later, escalated)] == ["attention"]

    # The sentinel half, same rule: slot + sentinel kind.
    s_before = snap(row(weekly=10.0))
    s_first = snap(
        row(weekly=10.0),
        account_notes={2: "re-login needed (1d 4h)"},
        account_note_kinds={2: "relogin"},
    )
    s_later = snap(
        row(weekly=10.0),
        account_notes={2: "re-login needed (1d 3h)"},
        account_note_kinds={2: "relogin"},
    )
    opened = notify_mod.detect(s_before, s_first)
    assert [e.kind for e in opened] == ["sentinel"]
    assert notify_mod.detect(s_first, s_later) == [], "a countdown is not news"
    assert opened[0].key == "sentinel:2:relogin", opened[0].key
    assert "re-login needed" not in opened[0].key

    # And a slot whose source reported NO kind collapses to one key for that
    # slot rather than falling back to the text. That fallback is the whole
    # bug in miniature: it is exactly the case where there is no kind to key
    # on, so it is exactly the case where the wording would sneak back in.
    n_first = snap(row(weekly=10.0), account_notes={3: "cswap: paused (4m)"})
    n_later = snap(row(weekly=10.0), account_notes={3: "cswap: paused (3m)"})
    opened = notify_mod.detect(s_before, n_first)
    assert [e.key for e in opened] == ["sentinel:3:"], [e.key for e in opened]
    assert notify_mod.detect(n_first, n_later) == [], "no kind is not a licence"
    assert opened[0].subtitle == "cswap: paused (4m)"


def test_a_reworded_standing_note_is_still_suppressed_after_a_restart() -> None:
    """The ledger is on disk, so the wording rule has to hold across processes.

    The end-to-end version of the test above, through a real notifier and a
    real ``notify_state.json``: announce once, restart, feed a snapshot whose
    note has been rephrased by one tick of its countdown, and send NOTHING.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        notifier, sender = make_notifier(root)
        notifier.notify(
            snap(row(weekly=10.0)),
            snap(row(weekly=10.0, note="relogin in 1d 4h", kind="warn")),
        )
        assert [e.kind for e in sender.events] == ["attention"], sender.events

        restarted, second = make_notifier(root)
        restarted.notify(
            snap(row(weekly=10.0)),
            snap(row(weekly=10.0, note="relogin in 1d 3h", kind="warn")),
        )
        assert second.events == [], "the same fault, in new words, is not new"


def test_the_ledgers_temp_file_matches_the_repos_ignore_rules() -> None:
    """A half-written ``notify.json`` holds a Telegram bot token.

    ``mkstemp`` + ``os.replace`` leaks its temporary file if the process is
    killed between the two syscalls, and the widget home IS this repository on
    a source install - so a name that matches no ignore rule is an untracked
    file holding a credential, sitting in ``git status`` waiting to be added.
    Asserted against the real ``.gitignore``, not against a remembered pattern.
    """
    import fnmatch

    patterns = [
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        target = root / "notify.json"
        seen: list[str] = []
        real_mkstemp = notify_mod.tempfile.mkstemp

        def watching(*args: Any, **kwargs: Any) -> Any:
            fd, path = real_mkstemp(*args, **kwargs)
            seen.append(Path(path).name)
            return fd, path

        notify_mod.tempfile.mkstemp = watching  # type: ignore[assignment]
        try:
            assert notify_mod._atomic_write_json(target, {"a": 1}, log=lambda _m: None)
        finally:
            notify_mod.tempfile.mkstemp = real_mkstemp  # type: ignore[assignment]

        assert len(seen) == 1, seen
        assert any(fnmatch.fnmatch(seen[0], pattern) for pattern in patterns), (
            seen[0],
            patterns,
        )


def test_an_expired_window_is_never_notified_about() -> None:
    """Its percentage describes a window that has ENDED (SPEC 4.3)."""
    before = snap(row(weekly=80.0))
    after = snap(row(weekly=100.0, expired=("seven_day",)))
    assert notify_mod.detect(before, after, threshold=85.0) == []


def test_the_first_snapshot_ever_reports_no_percentage_transition() -> None:
    """Otherwise every launch would replay four already-capped accounts, and the
    feature would be muted within a week."""
    assert notify_mod.detect(None, snap(row(weekly=100.0)), threshold=85.0) == []


def test_a_row_that_did_not_move_reports_nothing() -> None:
    assert notify_mod.detect(snap(row(weekly=87.0)), snap(row(weekly=87.0)), threshold=85.0) == []


def test_rows_are_identified_by_vendor_slot_and_alias_not_by_position() -> None:
    """Two accounts must not be confused when the row order changes — a Codex
    registry re-order would otherwise read as a 0 %-to-100 % jump."""
    before = snap(row(slot=-1, alias="vlad", weekly=10.0), row(slot=-2, alias="astra", weekly=90.0))
    after = snap(row(slot=-2, alias="astra", weekly=91.0), row(slot=-1, alias="vlad", weekly=11.0))
    assert notify_mod.detect(before, after, threshold=85.0) == []


def test_detection_touches_no_file_and_sends_nothing() -> None:
    """``observe`` is the pure half: the contract every test above relies on."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        notifier, mac = make_notifier(root)
        events = notifier.observe(snap(row(weekly=10.0)), snap(row(weekly=100.0)))
        assert [e.kind for e in events] == ["capped"]
        assert mac.events == [], "observe must not send"
        assert list(root.iterdir()) == [], "observe must not write"


def test_the_threshold_setting_moves_the_edge() -> None:
    with tempfile.TemporaryDirectory() as name:
        notifier, _mac = make_notifier(
            Path(name), settings={"notification_threshold_pct": 95}
        )
        assert notifier.threshold() == 95.0
        assert notifier.observe(snap(row(weekly=80.0)), snap(row(weekly=90.0))) == []
        crossed = notifier.observe(snap(row(weekly=90.0)), snap(row(weekly=96.0)))
        assert [e.kind for e in crossed] == ["threshold"]


def test_the_threshold_setting_is_declared_and_bounded_in_contracts() -> None:
    """``normalize_settings`` drops undeclared keys and needs bounds for every
    int, so a key added only in ``notify.py`` would vanish on the next save."""
    for key in (
        "notifications_enabled",
        "telegram_notifications_enabled",
        "notification_threshold_pct",
    ):
        assert key in SETTINGS_DEFAULTS, f"{key} is not declared"
    assert SETTINGS_DEFAULTS["notifications_enabled"] is True
    assert SETTINGS_DEFAULTS["telegram_notifications_enabled"] is False
    assert SETTINGS_BOUNDS["notification_threshold_pct"] == (50, 100)
    assert normalize_settings({"notification_threshold_pct": 5})["notification_threshold_pct"] == 50
    assert (
        normalize_settings({"notification_threshold_pct": 500})["notification_threshold_pct"] == 100
    )


# ---------------------------------------------------------------------------
# 2. Once per transition — the ledger
# ---------------------------------------------------------------------------


def test_a_standing_condition_notifies_once_not_once_per_tick() -> None:
    with tempfile.TemporaryDirectory() as name:
        clock = [1_000.0]
        notifier, mac = make_notifier(Path(name), clock=lambda: clock[0])
        before, after = snap(row(weekly=80.0)), snap(row(weekly=100.0))
        assert len(notifier.notify(before, after)) == 1
        for _ in range(10):
            clock[0] += 60.0
            assert notifier.notify(after, after) == []
        assert len(mac.events) == 1


def test_a_transition_is_not_replayed_after_a_restart() -> None:
    """A fresh Notifier over the same ledger file is what a widget restart is."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        first, mac_one = make_notifier(root)
        assert len(first.notify(snap(row(weekly=80.0)), snap(row(weekly=100.0)))) == 1

        second, mac_two = make_notifier(root)
        # After a restart the previous snapshot is gone, so the standing
        # condition is re-detected from scratch — and must still be silent.
        assert second.notify(snap(row(weekly=99.0)), snap(row(weekly=100.0))) == []
        assert mac_two.events == [] and len(mac_one.events) == 1


def test_the_same_account_notifies_again_the_next_time_it_walls() -> None:
    """The ledger releases a key when its condition goes false; without that,
    one wall would mute the account forever."""
    with tempfile.TemporaryDirectory() as name:
        clock = [1_000.0]
        notifier, mac = make_notifier(Path(name), clock=lambda: clock[0])
        capped, back = snap(row(weekly=100.0)), snap(row(weekly=0.0))
        notifier.notify(snap(row(weekly=80.0)), capped)
        clock[0] += 3_600.0
        notifier.notify(capped, back)          # released here
        clock[0] += 3_600.0
        again = notifier.notify(back, capped)  # a second wall, days later
        assert [e.kind for e in again] == ["capped"]
        assert [e.kind for e in mac.events] == ["capped", "back", "capped"]


def test_a_flapping_condition_is_throttled_for_a_minute() -> None:
    """Released is not the same as forgotten: a note that clears and returns
    inside the throttle window must not notify twice."""
    with tempfile.TemporaryDirectory() as name:
        clock = [1_000.0]
        notifier, mac = make_notifier(Path(name), clock=lambda: clock[0])
        clear = snap(row(weekly=10.0))
        noted = snap(row(weekly=10.0, note="offline", kind="warn"))
        assert len(notifier.notify(clear, noted)) == 1
        clock[0] += 5.0
        notifier.notify(noted, clear)
        clock[0] += 5.0
        assert notifier.notify(clear, noted) == [], "inside the throttle window"
        clock[0] += notify_mod.THROTTLE_SECONDS + 1.0
        notifier.notify(noted, clear)
        clock[0] += notify_mod.THROTTLE_SECONDS + 1.0
        assert len(notifier.notify(clear, noted)) == 1, "past it, the fact is news again"
        assert len(mac.events) == 2


def test_a_source_that_publishes_nothing_for_a_tick_does_not_re_announce() -> None:
    """The failure this feature is most likely to have in the field.

    ``quota_rows`` goes EMPTY for a tick when a Codex poll fails. The row then
    comes back carrying the same standing note, which is a brand new fact
    against the previous (empty) snapshot — and would be announced a second
    time if the ledger released the key just because nothing was observed.
    """
    with tempfile.TemporaryDirectory() as name:
        clock = [1_000.0]
        notifier, mac = make_notifier(Path(name), clock=lambda: clock[0])
        clear = snap(row(weekly=10.0))
        noted = snap(row(weekly=10.0, note="endpoint error", kind="warn"))
        blank = snap()  # the poll failed; there are no rows to publish

        assert len(notifier.notify(clear, noted)) == 1
        # Well past the throttle window, so the ONLY thing that can still be
        # holding the key is "this scope was not observed" (a 5-minute Codex
        # poll period makes that the realistic gap, not 60 s).
        clock[0] += 300.0
        assert notifier.notify(noted, blank) == []
        clock[0] += 300.0
        assert notifier.notify(blank, noted) == [], "one blip must not notify twice"
        assert len(mac.events) == 1


def test_a_key_held_for_an_unobserved_scope_is_eventually_forgotten() -> None:
    """The backstop on the rule above: an account that is deleted must not
    leave an entry in the ledger for ever."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = [1_000.0]
        notifier, _mac = make_notifier(root, clock=lambda: clock[0])
        noted = snap(row(weekly=10.0, note="no access", kind="warn"))
        notifier.notify(snap(row(weekly=10.0)), noted)
        assert json.loads((root / "notify_state.json").read_text())["fired"]

        clock[0] += notify_mod.LEDGER_MAX_AGE_SECONDS + 1.0
        notifier.notify(noted, snap())  # the account is gone for good
        assert json.loads((root / "notify_state.json").read_text())["fired"] == {}


def test_the_ledger_file_is_owner_only_and_survives_a_corrupt_read() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        notifier, _mac = make_notifier(root)
        notifier.notify(snap(row(weekly=80.0)), snap(row(weekly=100.0)))
        ledger = root / "notify_state.json"
        assert stat.S_IMODE(os.stat(ledger).st_mode) == 0o600
        assert json.loads(ledger.read_text())["fired"], "the ledger must hold the key"

        # A hand-edited entry with no scope is read as "always observable"
        # rather than dropped: an unparseable ledger costs a duplicate, and a
        # half-understood one must not cost more than that.
        ledger.write_text(json.dumps({"version": 1, "fired": {"capped:codex:1:vlad:seven_day": 1.0}}))
        legacy, legacy_mac = make_notifier(root)
        assert legacy.notify(snap(row(slot=1, weekly=99.0)), snap(row(slot=1, weekly=100.0))) == []
        assert legacy_mac.events == []

        ledger.write_text("{not json")
        fresh, mac = make_notifier(root)
        assert len(fresh.notify(snap(row(weekly=80.0)), snap(row(weekly=100.0)))) == 1, (
            "a corrupt ledger must cost one duplicate, never a crash"
        )
        assert mac.events


# ---------------------------------------------------------------------------
# 3. The off switches
# ---------------------------------------------------------------------------


def test_notifications_disabled_sends_nothing_and_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        notifier, mac = make_notifier(root, settings={"notifications_enabled": False})
        assert notifier.notify(snap(row(weekly=80.0)), snap(row(weekly=100.0))) == []
        assert mac.events == []
        assert list(root.iterdir()) == [], "a disabled feature leaves no state behind"


def test_telegram_is_silent_until_its_own_switch_is_on() -> None:
    with tempfile.TemporaryDirectory() as name:
        telegram = RecordingSender()
        notifier, mac = make_notifier(Path(name), telegram=telegram)
        notifier.notify(snap(row(weekly=80.0)), snap(row(weekly=100.0)))
        assert len(mac.events) == 1 and telegram.events == []

    with tempfile.TemporaryDirectory() as name:
        telegram = RecordingSender()
        notifier, mac = make_notifier(
            Path(name), settings={"telegram_notifications_enabled": True}, telegram=telegram
        )
        notifier.notify(snap(row(weekly=80.0)), snap(row(weekly=100.0)))
        assert len(mac.events) == 1 and len(telegram.events) == 1


# ---------------------------------------------------------------------------
# 4. Credentials: one file, 0600, never argv
# ---------------------------------------------------------------------------


def write_credentials(path: Path, *, mode: int = 0o600, token: str = TOKEN_CANARY) -> None:
    path.write_text(json.dumps({"version": 1, "telegram": {"token": token, "chat_id": CHAT_ID}}))
    path.chmod(mode)


def test_a_group_readable_credential_file_is_refused() -> None:
    """A bot token is a password. Using one the whole machine can read would be
    the widget blessing a mistake it can see."""
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "notify.json"
        write_credentials(path, mode=0o644)
        credentials, reason = notify_mod.load_telegram_credentials(path)
        assert credentials is None
        assert "chmod 600" in reason
        assert TOKEN_CANARY not in reason


def test_missing_or_incomplete_credentials_report_a_reason_without_a_token() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        assert notify_mod.load_telegram_credentials(root / "absent.json")[0] is None
        partial = root / "notify.json"
        partial.write_text(json.dumps({"telegram": {"token": TOKEN_CANARY}}))
        partial.chmod(0o600)
        credentials, reason = notify_mod.load_telegram_credentials(partial)
        assert credentials is None and "chat_id" in reason and TOKEN_CANARY not in reason


def test_setup_reads_the_token_from_the_environment_and_writes_it_0600() -> None:
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "notify.json"
        out = io.StringIO()
        code = notify_mod.main(
            ["setup", "--telegram-token-from-env", "MY_BOT_TOKEN", "--chat-id", CHAT_ID],
            credentials_path=path,
            env={"MY_BOT_TOKEN": TOKEN_CANARY},
            out=out,
        )
        assert code == 0, out.getvalue()
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        credentials, reason = notify_mod.load_telegram_credentials(path)
        assert reason == "ok" and credentials.token == TOKEN_CANARY
        assert credentials.chat_id == CHAT_ID
        assert TOKEN_CANARY not in out.getvalue(), "setup must not echo the token"


def test_setup_refuses_a_token_handed_on_the_command_line() -> None:
    """argv is visible in ``ps`` and lands in shell history; the whole point of
    ``--telegram-token-from-env`` is that the secret never goes there."""
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "notify.json"
        out = io.StringIO()
        code = notify_mod.main(
            ["setup", "--telegram-token", TOKEN_CANARY, "--chat-id", CHAT_ID],
            credentials_path=path,
            env={},
            out=out,
        )
        assert code == 2
        assert not path.exists()
        assert "process list" in out.getvalue()


def test_setup_refuses_an_empty_environment_variable() -> None:
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "notify.json"
        out = io.StringIO()
        code = notify_mod.main(
            ["setup", "--telegram-token-from-env", "NOPE", "--chat-id", CHAT_ID],
            credentials_path=path,
            env={},
            out=out,
        )
        assert code == 2 and not path.exists() and "$NOPE" in out.getvalue()


def test_status_reports_the_state_without_printing_the_token() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        write_credentials(root / "notify.json")
        notifier, _mac = make_notifier(root)
        notifier.notify(snap(row(weekly=80.0)), snap(row(weekly=100.0)))
        out = io.StringIO()
        code = notify_mod.main(
            ["status"],
            credentials_path=root / "notify.json",
            ledger_path=root / "notify_state.json",
            out=out,
        )
        text = out.getvalue()
        assert code == 0
        assert TOKEN_CANARY not in text
        assert CHAT_ID in text and "transitions held: 1" in text


# ---------------------------------------------------------------------------
# 5. The Telegram transport, and the privacy check it must survive
# ---------------------------------------------------------------------------


class FakeOpener:
    """Records the request object the real sender built. No network."""

    def __init__(self, status: int = 200, logger: Any = None) -> None:
        self.requests: list[Any] = []
        self._status = status
        self._logger = logger

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append(request)
        if self._logger is not None:  # the negative control's careless line
            self._logger(f"POST {request.full_url}")

        class _Response:
            status = self._status

            def close(self) -> None:
                return None

        return _Response()


def send_one(root: Path, *, logger: Any = None, status: int = 200):
    """One real ``TelegramSender.send`` over a real credential file."""
    path = root / "notify.json"
    write_credentials(path)
    credentials, reason = notify_mod.load_telegram_credentials(path)
    assert credentials is not None, reason
    logs: list[str] = []
    opener = FakeOpener(status=status, logger=logger)
    sender = notify_mod.TelegramSender(credentials, opener=opener, log=logs.append)
    ok = sender.send(
        notify_mod.Event(
            key="capped:codex:-1:vlad:seven_day",
            kind="capped",
            severity="crit",
            title="Codex vlad",
            subtitle="weekly 100%",
            message="resets Sat 09:00",
        )
    )
    return ok, opener, logs, credentials


def leaks(opener: FakeOpener, logs: list[str], credentials: Any) -> list[str]:
    """Every place the token could have escaped to, except the URL itself."""
    found: list[str] = []
    for request in opener.requests:
        body = request.data.decode("utf-8") if request.data else ""
        if TOKEN_CANARY in body:
            found.append("request body")
        for name, value in (request.headers or {}).items():
            if TOKEN_CANARY in f"{name}{value}":
                found.append("request header")
    for line in logs:
        if TOKEN_CANARY in line:
            found.append("log line")
    if TOKEN_CANARY in repr(credentials) or TOKEN_CANARY in str(credentials):
        found.append("credentials repr")
    return found


def test_the_message_body_carries_the_event_and_never_the_token() -> None:
    with tempfile.TemporaryDirectory() as name:
        ok, opener, logs, credentials = send_one(Path(name))
        assert ok and len(opener.requests) == 1
        request = opener.requests[0]
        assert request.full_url.endswith("/sendMessage")
        assert TOKEN_CANARY in request.full_url, (
            "the token belongs in the URL and nowhere else - if it is not here "
            "the leak check below proves nothing"
        )
        body = request.data.decode("utf-8")
        assert "chat_id=" + CHAT_ID in body
        assert "Codex+vlad" in body and "weekly+100%25" in body
        assert "disable_notification=false" in body
        assert leaks(opener, logs, credentials) == []


def test_a_failing_transport_is_logged_with_the_token_redacted() -> None:
    """The most likely real leak: an exception whose text quotes the URL."""

    class _Boom:
        def __call__(self, request: Any, timeout: float | None = None) -> Any:
            raise OSError(f"connection refused for {request.full_url}")

    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "notify.json"
        write_credentials(path)
        credentials, _reason = notify_mod.load_telegram_credentials(path)
        logs: list[str] = []
        sender = notify_mod.TelegramSender(credentials, opener=_Boom(), log=logs.append)
        assert sender.send(notify_mod.Event(key="k", kind="capped", severity="crit", title="t"))\
            is False
        assert logs, "a failed send must be diagnosable"
        assert TOKEN_CANARY not in "".join(logs)
        assert "<redacted>" in "".join(logs)


def test_an_http_error_is_reported_as_a_failure_not_a_success() -> None:
    with tempfile.TemporaryDirectory() as name:
        ok, _opener, logs, _credentials = send_one(Path(name), status=401)
        assert ok is False and any("401" in line for line in logs)


def test_the_telegram_privacy_check_can_actually_fail() -> None:
    """The negative control. A sender that logs the URL is exactly the careless
    debug line the checks above exist to catch; if they do not catch it here,
    they are decoration."""
    with tempfile.TemporaryDirectory() as name:
        logs: list[str] = []
        _ok, opener, sender_logs, credentials = send_one(Path(name), logger=logs.append)
        found = leaks(opener, logs + sender_logs, credentials)
        assert found, (
            "a transport that logged the bot-token URL went UNDETECTED - the "
            "privacy assertions above are not testing anything"
        )
        assert "log line" in found


# ---------------------------------------------------------------------------
# 6. Dispatch: a Telegram timeout must never delay a tick
# ---------------------------------------------------------------------------


def test_sending_happens_off_the_calling_thread() -> None:
    """The production dispatcher, not the synchronous test one."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        started = threading.Event()
        release = threading.Event()
        seen: list[str] = []

        class _Slow:
            def send(self, event: notify_mod.Event) -> bool:
                started.set()
                release.wait(5.0)
                seen.append(threading.current_thread().name)
                return True

        notifier = notify_mod.Notifier(
            settings=lambda: normalize_settings({}),
            ledger_path=root / "notify_state.json",
            credentials_path=root / "notify.json",
            mac_sender=_Slow(),
            clock=lambda: 1_000.0,
        )
        caller = threading.current_thread().name
        began = time.monotonic()
        fired = notifier.notify(snap(row(weekly=80.0)), snap(row(weekly=100.0)))
        elapsed = time.monotonic() - began
        assert len(fired) == 1
        assert elapsed < 1.0, f"notify blocked the caller for {elapsed:.2f}s"
        assert started.wait(5.0), "the batch never reached the sender"
        release.set()
        for _ in range(500):
            if seen:
                break
            time.sleep(0.01)
        assert seen and seen[0] != caller, "the send ran on the caller's thread"


def test_a_sender_that_raises_does_not_lose_the_rest_of_the_batch() -> None:
    with tempfile.TemporaryDirectory() as name:
        telegram = RecordingSender()

        class _Broken:
            def send(self, event: notify_mod.Event) -> bool:
                raise RuntimeError("Notification Centre is unavailable")

        logs: list[str] = []
        notifier = notify_mod.Notifier(
            settings=lambda: normalize_settings({"telegram_notifications_enabled": True}),
            ledger_path=Path(name) / "notify_state.json",
            credentials_path=Path(name) / "notify.json",
            mac_sender=_Broken(),
            telegram_sender=telegram,
            clock=lambda: 1_000.0,
            dispatch=lambda work: work(),
            log=logs.append,
        )
        assert len(notifier.notify(snap(row(weekly=80.0)), snap(row(weekly=100.0)))) == 1
        assert len(telegram.events) == 1, "one broken sender must not silence the other"
        assert logs


# ---------------------------------------------------------------------------
# 7. The wiring: the worker's publish path, and nothing else
# ---------------------------------------------------------------------------


def test_the_worker_hands_every_publish_to_the_notifier_as_a_pair() -> None:
    """``BackgroundWorker._publish`` is the single choke point every snapshot
    passes through, which is why the hook lives there and not in five jobs."""
    from cc_usage_widget.app import BackgroundWorker, UiSnapshot

    seen: list[tuple[Any, Any]] = []

    class _Spy:
        def notify(self, previous: Any, current: Any) -> list[Any]:
            seen.append((previous, current))
            return []

    first = UiSnapshot()
    published: list[UiSnapshot] = []
    worker = BackgroundWorker(publish=published.append, snapshot=first, notifier=_Spy())
    second = replace(first, cost_error="x")
    third = replace(second, cost_error="y")
    worker._publish(second)
    worker._publish(third)

    assert published == [second, third]
    assert seen == [(first, second), (second, third)], (
        "the notifier needs the PREVIOUS snapshot; a hook that only sees the "
        "new one cannot detect a transition at all"
    )


def test_a_notifier_that_raises_cannot_kill_the_worker_or_the_repaint() -> None:
    from cc_usage_widget.app import BackgroundWorker, UiSnapshot

    class _Angry:
        def notify(self, previous: Any, current: Any) -> list[Any]:
            raise RuntimeError("ledger on fire")

    published: list[UiSnapshot] = []
    worker = BackgroundWorker(publish=published.append, snapshot=UiSnapshot(), notifier=_Angry())
    worker._publish(replace(UiSnapshot(), cost_error="x"))
    assert len(published) == 1, "the UI hand-off must happen regardless"


def test_the_settings_menu_greys_out_telegram_until_notify_json_exists() -> None:
    """A control that cannot do anything is worse than no control — the same
    rule the Codex live-quota switch follows. The credential path is read
    through the module global, so pointing it at a temporary directory
    exercises both states without touching the real widget home."""
    from cc_usage_widget import app as app_mod
    from cc_usage_widget.app import UiSnapshot

    original = notify_mod.NOTIFY_CREDENTIALS_PATH
    app = app_mod.CCUsageWidgetApp()
    snapshot = UiSnapshot(settings=normalize_settings({}))
    try:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "notify.json"
            notify_mod.NOTIFY_CREDENTIALS_PATH = path

            settings_titles = list(app._settings_submenu(snapshot).keys())
            assert "Notifications" in settings_titles, settings_titles

            absent = list(app._settings_submenu(snapshot)["Notifications"].keys())
            assert any(t.startswith("Notifications") for t in absent), absent
            assert any("notify setup" in t for t in absent), absent
            assert not any(t.startswith("Telegram ") for t in absent), absent

            write_credentials(path)
            present = list(app._settings_submenu(snapshot)["Notifications"].keys())
            assert any(t.startswith("Telegram") and "notify setup" not in t for t in present), (
                present
            )

            # A world-readable credential counts as absent: the sender would
            # refuse it, so the menu must not offer to use it either.
            path.chmod(0o644)
            unsafe = list(app._settings_submenu(snapshot)["Notifications"].keys())
            assert any("notify setup" in t for t in unsafe), unsafe
    finally:
        notify_mod.NOTIFY_CREDENTIALS_PATH = original


def test_a_worker_with_no_notifier_behaves_exactly_as_before() -> None:
    """The default, and what every existing test in the suite constructs: a
    widget that is built but never run notifies nothing and writes nothing."""
    from cc_usage_widget.app import BackgroundWorker, UiSnapshot

    published: list[UiSnapshot] = []
    worker = BackgroundWorker(publish=published.append, snapshot=UiSnapshot())
    worker._publish(replace(UiSnapshot(), cost_error="x"))
    assert len(published) == 1


# ---------------------------------------------------------------------------
# 7. Lane codex-relogin-ux: pre-expiry, daily re-arm, 'notify: sent', spend,
#    unpriced models, usable Codex resets (CX-6, F2c, F8, RS-2)
# ---------------------------------------------------------------------------
#
# The fields these rules read (credential_expires_at, spend_*,
# reset_credits_usable) land on AccountRow from three OTHER lanes. `XRow` is an
# AccountRow that carries them either way, so these tests run on this branch
# and keep running after the merge, when the real fields exist.


@dataclass(frozen=True)
class XRow(AccountRow):
    credential_expires_at: float | None = None
    spend_used: float | None = None
    spend_limit: float | None = None
    spend_pct: float | None = None
    spend_currency: str | None = None
    reset_credits_usable: int | None = None


NOW = 1_000_000.0


def xrow(**fields: Any) -> XRow:
    base = dict(
        slot=-1, alias="vlad", email="", is_active=False,
        vendor=VENDOR_CODEX, switchable=False,
    )
    base.update(fields)
    return XRow(**base)


def countdown(**fields: Any) -> XRow:
    """A live Codex row showing the dim ``relogin in …`` countdown (kind info)."""
    return xrow(attention_note="relogin in 20h", attention_kind="info", **fields)


def test_a_login_inside_a_day_of_expiry_notifies_once() -> None:
    before = snap(countdown(credential_expires_at=NOW + 30 * 3600))
    after = snap(countdown(credential_expires_at=NOW + 20 * 3600))
    events = notify_mod.detect(before, after, now=NOW)
    assert [e.key for e in events] == ["expiring:codex:-1:vlad"], events
    assert events[0].kind == "expiring" and events[0].severity == notify_mod.SEVERITY_WARN
    # The second identical tick says nothing: the condition was already true.
    assert notify_mod.detect(after, after, now=NOW + 60) == []
    # Through the ledger, across a restart, it stays once.
    with tempfile.TemporaryDirectory() as name:
        first, mac = make_notifier(Path(name), clock=lambda: NOW)
        assert [e.kind for e in first.notify(before, after)] == ["expiring"]
        assert first.notify(after, after) == []
        second, mac_two = make_notifier(Path(name), clock=lambda: NOW + 120)
        assert second.notify(None, after) == [] and mac_two.events == []


def test_the_expiry_rule_is_kind_based_and_can_fail() -> None:
    """Negative controls: each one flips ONE input and the event must vanish."""
    fires = snap(countdown(credential_expires_at=NOW + 20 * 3600))
    assert len(notify_mod.detect(None, fires, now=NOW)) == 1, "the positive case must fire"
    # No clock -> the rule is absent (detect stays pure for a caller without one).
    assert notify_mod.detect(None, fires) == []
    # Outside the window, already expired, unknown expiry: nothing.
    for exp in (NOW + 30 * 3600, NOW - 1, None):
        assert notify_mod.detect(None, snap(countdown(credential_expires_at=exp)), now=NOW) == [], exp
    # A warn note owns the row (the relogin verdict has its own key).
    warned = xrow(attention_note="relogin", attention_kind="warn", credential_expires_at=NOW + 3600)
    assert [e.kind for e in notify_mod.detect(None, snap(warned), now=NOW)] == ["attention"]
    # No countdown = the source expects to renew it itself; same wording, other kind.
    quiet = xrow(attention_note="relogin in 20h", attention_kind="", credential_expires_at=NOW + 3600)
    assert notify_mod.detect(None, snap(quiet), now=NOW) == []
    # A Claude row never carries a Codex login.
    claude = xrow(vendor="claude", switchable=True, slot=1, attention_kind="info",
                  attention_note="x", credential_expires_at=NOW + 3600)
    assert notify_mod.detect(None, snap(claude), now=NOW) == []


def test_the_expiring_key_is_released_when_the_expiry_moves_later() -> None:
    with tempfile.TemporaryDirectory() as name:
        clock = [NOW]
        notifier, mac = make_notifier(Path(name), clock=lambda: clock[0])
        near = snap(countdown(credential_expires_at=NOW + 20 * 3600))
        assert len(notifier.notify(None, near)) == 1
        clock[0] += 300
        renewed = snap(xrow(credential_expires_at=NOW + 10 * 86_400))
        assert notifier.notify(near, renewed) == []
        state = json.loads((Path(name) / "notify_state.json").read_text())
        assert "expiring:codex:-1:vlad" not in state["fired"], state
        # Ten days later the next token nears its end: it announces again.
        clock[0] = NOW + 10 * 86_400 - 20 * 3600
        again = snap(countdown(credential_expires_at=NOW + 10 * 86_400))
        assert [e.kind for e in notifier.notify(renewed, again)] == ["expiring"]


def test_a_standing_codex_warn_is_sent_again_after_a_day() -> None:
    dead = snap(xrow(attention_note="relogin", attention_kind="warn"))
    with tempfile.TemporaryDirectory() as name:
        clock = [NOW]
        notifier, mac = make_notifier(Path(name), clock=lambda: clock[0])
        assert [e.key for e in notifier.notify(None, dead)] == ["attention:codex:-1:vlad:warn"]
        clock[0] += notify_mod.ATTENTION_REARM_SECONDS - 60
        assert notifier.notify(dead, dead) == [], "not yet a day"
        clock[0] += 120
        again = notifier.notify(dead, dead)
        assert [e.key for e in again] == ["attention:codex:-1:vlad:warn"], again
        # Restamped: the next reminder is a day after THIS one, not every tick.
        clock[0] += 60
        assert notifier.notify(dead, dead) == []
        assert len(mac.events) == 2
        # A restart a day later reminds too (the ledger holds the age).
        clock[0] += notify_mod.ATTENTION_REARM_SECONDS + 1
        fresh, mac_two = make_notifier(Path(name), clock=lambda: clock[0])
        assert len(fresh.notify(None, dead)) == 1


def test_claude_sentinels_and_codex_crit_are_not_re_armed() -> None:
    """Documented choice: the daily re-arm is Codex ``warn`` only."""
    assert notify_mod.rearms("attention:codex:-1:vlad:warn")
    assert not notify_mod.rearms("attention:codex:-1:vlad:crit")
    assert not notify_mod.rearms("sentinel:3:fetch-failing")
    assert not notify_mod.rearms("attention:claude:1:main:warn")
    sentinel = Snap(account_notes={3: "usage forbidden"}, account_note_kinds={3: "fetch-failing"},
                    accounts=(row(slot=3, alias="work", vendor="claude"),))
    with tempfile.TemporaryDirectory() as name:
        clock = [NOW]
        notifier, _mac = make_notifier(Path(name), clock=lambda: clock[0])
        assert len(notifier.notify(None, sentinel)) == 1
        clock[0] += 3 * notify_mod.ATTENTION_REARM_SECONDS
        assert notifier.notify(sentinel, sentinel) == []


def test_a_successful_send_is_logged_by_key_without_its_wording() -> None:
    logs: list[str] = []
    with tempfile.TemporaryDirectory() as name:
        mac = RecordingSender()
        notifier = notify_mod.Notifier(
            settings=lambda: normalize_settings({}),
            ledger_path=Path(name) / "notify_state.json",
            credentials_path=Path(name) / "notify.json",
            mac_sender=mac,
            clock=lambda: NOW,
            dispatch=lambda work: work(),
            log=logs.append,
        )
        dead = snap(xrow(attention_note="relogin", attention_kind="warn"))
        notifier.notify(None, dead)
    assert logs == ["notify: sent attention:codex:-1:vlad:warn"], logs
    assert not any("needs attention" in line or "relogin" == line for line in logs)

    class Refusing:
        def send(self, event: Any) -> bool:
            return False

    refused: list[str] = []
    with tempfile.TemporaryDirectory() as name:
        notifier = notify_mod.Notifier(
            settings=lambda: normalize_settings({}),
            ledger_path=Path(name) / "notify_state.json",
            credentials_path=Path(name) / "notify.json",
            mac_sender=Refusing(),
            clock=lambda: NOW,
            dispatch=lambda work: work(),
            log=refused.append,
        )
        notifier.notify(None, snap(xrow(attention_note="relogin", attention_kind="warn")))
    assert refused == [], "a failed send must not be logged as sent"


def spender(pct: float | None, *, used: float | None = 480.00, limit: float | None = 500.00) -> XRow:
    return xrow(vendor="claude", switchable=True, slot=1, alias="main",
                spend_used=used, spend_limit=limit, spend_pct=pct, spend_currency="USD")


def test_extra_usage_spend_crossing_the_threshold_notifies_once_then_at_the_cap() -> None:
    low, high, cap = snap(spender(80.0, used=400.00)), snap(spender(95.6)), snap(spender(100.0, used=500.00))
    events = notify_mod.detect(low, high, threshold=85.0)
    assert [(e.kind, e.severity) for e in events] == [("spend", notify_mod.SEVERITY_WARN)], events
    assert "$480.00 / $500.00" in events[0].text, events[0].text
    assert notify_mod.detect(high, high, threshold=85.0) == []
    capped = notify_mod.detect(high, cap, threshold=85.0)
    assert [(e.kind, e.severity) for e in capped] == [("spend", notify_mod.SEVERITY_CRIT)], capped
    with tempfile.TemporaryDirectory() as name:
        notifier, mac = make_notifier(Path(name), settings={"notification_threshold_pct": 85})
        assert len(notifier.notify(low, high)) == 1
        assert notifier.notify(high, high) == []
        assert len(notifier.notify(high, cap)) == 1
        assert notifier.notify(cap, cap) == []
        assert [e.severity for e in mac.events] == ["warn", "crit"]


def test_spend_without_a_limit_or_under_the_threshold_says_nothing() -> None:
    assert notify_mod.detect(None, snap(spender(96.0, limit=None)), threshold=85.0) == []
    assert notify_mod.detect(None, snap(spender(None)), threshold=85.0) == []
    assert notify_mod.detect(None, snap(spender(84.0)), threshold=85.0) == []
    # Negative control for the three above: the same row over the line fires.
    assert len(notify_mod.detect(None, snap(spender(86.0)), threshold=85.0)) == 1
    # Currency rides verbatim when it is not USD.
    euro = replace(spender(90.0), spend_currency="EUR")
    assert "480.00 EUR / 500.00 EUR" in notify_mod.detect(None, snap(euro), threshold=85.0)[0].text


def _cost(*buckets: tuple[str, tuple[str, ...], int], partial: bool = False) -> Any:
    from cc_usage_widget.contracts import (
        UNKNOWN_MODEL, CostBreakdown, IndexProgress, ModelCostRow, ModelUsage, WindowCost,
    )

    window = WindowCost(label="Today", usd=0.0, total_tokens=0, window_days=1, days_counted=1)
    rows = tuple(
        ModelCostRow(model=UNKNOWN_MODEL, display_name=names[0], usage=ModelUsage(input=tokens),
                     usd=0.0, is_unknown=True, raw_models=names, vendor=vendor)
        for vendor, names, tokens in buckets
    )
    return CostBreakdown(
        today=window, last_7d=window, last_30d=window, by_model=rows,
        unknown_models=tuple(sorted({n for _v, names, _t in buckets for n in names})),
        progress=IndexProgress(complete=not partial),
    )


@dataclass(frozen=True)
class CostSnap(Snap):
    cost: Any = None


def test_an_unpriced_model_over_the_floor_notifies_once() -> None:
    model = "SYNTHETIC-unpriced-model"
    big = CostSnap(cost=_cost((VENDOR_CODEX, (model,), 2_000_000)))
    events = notify_mod.detect(CostSnap(), big)
    assert [e.key for e in events] == [f"unpriced:{model}"], events
    assert "2.0M tokens today" in events[0].message
    assert notify_mod.detect(big, big) == []
    with tempfile.TemporaryDirectory() as name:
        clock = [NOW]
        notifier, mac = make_notifier(Path(name), clock=lambda: clock[0])
        assert len(notifier.notify(CostSnap(), big)) == 1
        # The next day starts at 0 tokens: the name is still in the 30-day
        # unpriced list, so the key is held and tomorrow's crossing is silent.
        clock[0] += 86_400
        tomorrow_empty = CostSnap(cost=_cost((VENDOR_CODEX, (model,), 0)))
        assert notifier.notify(big, tomorrow_empty) == []
        clock[0] += 3_600
        assert notifier.notify(tomorrow_empty, big) == []
        assert len(mac.events) == 1


def test_the_unpriced_rule_respects_its_floor_its_switch_and_a_partial_index() -> None:
    model = "SYNTHETIC-unpriced-model"
    small = CostSnap(cost=_cost((VENDOR_CODEX, (model,), 999_999)))
    assert notify_mod.detect(None, small) == []
    big = CostSnap(cost=_cost((VENDOR_CODEX, (model,), 1_000_000)))
    assert len(notify_mod.detect(None, big)) == 1, "negative control: at the floor it fires"
    assert notify_mod.detect(None, big, unpriced_min_tokens=0) == [], "0 = off"
    partial = CostSnap(cost=_cost((VENDOR_CODEX, (model,), 5_000_000), partial=True))
    assert notify_mod.detect(None, partial) == [], "a half-built index is not a figure"
    # Two names in one bucket: the figure is the bucket's, said to be shared.
    shared = CostSnap(cost=_cost((VENDOR_CODEX, ("SYNTHETIC-a", "SYNTHETIC-b"), 3_000_000)))
    keys = sorted(e.key for e in notify_mod.detect(None, shared))
    assert keys == ["unpriced:SYNTHETIC-a", "unpriced:SYNTHETIC-b"], keys
    assert "between SYNTHETIC-a, SYNTHETIC-b" in notify_mod.detect(None, shared)[0].message
    # The settings key exists (normalize_settings drops undeclared keys).
    assert normalize_settings({"unpriced_alert_min_tokens": 5})["unpriced_alert_min_tokens"] == 5
    assert SETTINGS_DEFAULTS["unpriced_alert_min_tokens"] == 1_000_000


def resets(usable: int | None, *, weekly: float | None = 87.0) -> XRow:
    return xrow(reset_credits_usable=usable, seven_day_pct=weekly)


def test_a_codex_reset_credit_becoming_usable_notifies_once_per_transition() -> None:
    events = notify_mod.detect(snap(resets(0)), snap(resets(1)))
    assert [e.key for e in events] == ["reset-usable:codex:-1:vlad"], events
    assert events[0].title == "Codex reset available"
    assert events[0].message == "vlad: 1 reset credit usable now — weekly 87%", events[0].message
    assert notify_mod.detect(snap(resets(1)), snap(resets(1))) == []
    assert notify_mod.detect(snap(resets(None)), snap(resets(None))) == []
    assert notify_mod.detect(None, snap(resets(None))) == [], "None never fires"
    assert notify_mod.detect(None, snap(resets(0))) == []
    no_weekly = notify_mod.detect(None, snap(resets(2, weekly=None)))
    assert no_weekly[0].message == "vlad: 2 reset credits usable now", no_weekly[0].message
    with tempfile.TemporaryDirectory() as name:
        clock = [NOW]
        notifier, mac = make_notifier(Path(name), clock=lambda: clock[0])
        assert len(notifier.notify(snap(resets(0)), snap(resets(1)))) == 1
        restarted, mac_two = make_notifier(Path(name), clock=lambda: clock[0] + 60)
        assert restarted.notify(None, snap(resets(1))) == [], "no repeat across a restart"
        clock[0] += 3_600
        restarted.notify(snap(resets(1)), snap(resets(0)))   # spent: re-armed
        clock[0] += 86_400
        assert len(restarted.notify(snap(resets(0)), snap(resets(1)))) == 1
        assert len(mac.events) + len(mac_two.events) == 2


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
