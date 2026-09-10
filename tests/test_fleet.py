"""Fleet visibility tests (W3): live sessions, attribution, and directory pins.

The invariant behind all of them: the widget has always shown *accounts*, and
the thing that actually drains an account is a **session**. Four sessions on
one default login share one 5-hour window; a ``cswap run`` session on slot 3
does not. Every test here defends one half of making that visible without
inventing anything:

===  ==============================================================  ==========================================================
 #   invariant                                                        test
===  ==============================================================  ==========================================================
 1   a live interactive session is attributed to the default login    :func:`test_default_profile_sessions_belong_to_the_active_login`
 2   dead pids and non-interactive kinds are not fleet seats          :func:`test_dead_and_non_interactive_records_are_not_seats`
 3   an unreadable record is COUNTED, never silently dropped          :func:`test_unreadable_session_record_is_reported_not_swallowed`
 4   a session in a profile dir belongs to that slot, and is pinned    :func:`test_profile_sessions_are_pinned_to_their_slot`
 5   a mapping resolves by IDENTITY, never by slot number             :func:`test_mappings_resolve_by_identity_not_by_slot`
 6   a pin that has not taken effect says so, in its own words        :func:`test_pending_pin_is_labelled_next_launch`
 7   ``Unpin`` only ever offers the directory's OWN pin               :func:`test_unpin_offers_only_the_directorys_own_mapping`
 8   pinning writes an identity mapping and warns on the active login :func:`test_pin_writes_identity_and_warns_when_target_is_active`
 9   an unresolvable identity refuses the pin, loudly                 :func:`test_pin_refuses_a_slot_whose_identity_is_unreadable`
10   the fleet pass can never take the accounts job down              :func:`test_fleet_collection_never_raises`
11   the menu names the login, the counts, and the pin's limits       :func:`test_sessions_section_names_the_login_and_its_limits`
12   no fleet, no section (never an empty heading)                    :func:`test_absent_fleet_renders_no_section`
===  ==============================================================  ==========================================================

Run with pytest if it is available, or directly::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    $PY tests/test_fleet.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import traceback
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import fleet  # noqa: E402
from cc_usage_widget.app import BackgroundWorker, UiSnapshot  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    AccountRow,
    normalize_settings,
)

DEAD_PID = 2**22 - 1
"""Comfortably above the default macOS pid ceiling, so it is never alive."""

try:  # an OPTIONAL dependency (README: "account features need claude-swap")
    import claude_swap as _claude_swap
except Exception:  # pragma: no cover - a clean machine, and CI
    _claude_swap = None  # type: ignore[assignment]

_CLAUDE_SWAP_PRESENT = _claude_swap is not None
"""Whether the four profile/pin tests below can run at all.

They are not testing OUR code in isolation: ``session_dir_for`` and
``MappingStore`` are claude-swap's, and ``fleet.set_mapping`` calls the second
one in production. Re-implementing either here would test a guess about
upstream rather than upstream, so on a machine without it these four have
nothing to check."""


class _NeedsClaudeSwap(Exception):
    """A test that cannot run without the optional dependency.

    Never swallowed: the runner prints one ``skip`` line per test and the
    summary counts them, so a machine that has quietly lost claude-swap reads
    as ``13 passed, 4 skipped`` and never as ``17 passed`` - an all-green run
    that silently skipped a test is the one outcome worse than a red one.
    """


def _require_claude_swap() -> None:
    if not _CLAUDE_SWAP_PRESENT:
        raise _NeedsClaudeSwap("claude_swap is not installed")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_session(
    directory: Path,
    pid: int,
    *,
    cwd: str = "/tmp/project",
    name: str = "",
    status: str = "busy",
    kind: str = "interactive",
    started: int = 1788286894078,
    stem: str | None = None,
) -> Path:
    """One Claude Code session registry file, shaped like the real thing.

    ``stem`` lets two records carry the same (live) pid in different files,
    which is how a test gets a second GUARANTEED-alive record without
    depending on the parent process still being around.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem or pid}.json"
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": f"s-{pid}",
                "cwd": cwd,
                "name": name,
                "status": status,
                "kind": kind,
                "entrypoint": "cli",
                "startedAt": started,
                "version": "2.1.257",
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_sequence(backup: Path, accounts: dict[int, tuple[str, str]]) -> None:
    """``sequence.json`` as claude-swap writes it: slot -> identity."""
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "sequence.json").write_text(
        json.dumps(
            {
                "activeAccountNumber": "1",
                "accounts": {
                    str(slot): {"email": email, "organizationUuid": org}
                    for slot, (email, org) in accounts.items()
                },
            }
        ),
        encoding="utf-8",
    )


def _write_mappings(backup: Path, entries: dict[str, tuple[str, str]]) -> None:
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "mappings.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "mappings": {
                    fleet._normalize_path(path): {
                        "email": email,
                        "organizationUuid": org,
                        "added": "2026-09-01T00:00:00Z",
                    }
                    for path, (email, org) in entries.items()
                },
            }
        ),
        encoding="utf-8",
    )


@contextmanager
def _sessions_dir(path: Path) -> Iterator[None]:
    """Point the default-profile registry reader at *path*."""
    previous = os.environ.get(fleet.SESSIONS_DIR_ENV)
    os.environ[fleet.SESSIONS_DIR_ENV] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(fleet.SESSIONS_DIR_ENV, None)
        else:
            os.environ[fleet.SESSIONS_DIR_ENV] = previous


def _row(slot: int, alias: str, email: str, *, active: bool = False) -> AccountRow:
    return AccountRow(slot=slot, alias=alias, email=email, is_active=active)


class _FakeAccounts:
    """A source shaped like ``SwapAccountSource``, backed by a temp backup dir."""

    def __init__(self, backup: Path | None, rows: tuple[AccountRow, ...]) -> None:
        self._backup = backup
        self._rows = rows

    def refresh(self, *, force: bool = False) -> None:
        return None

    def rows(self) -> tuple[AccountRow, ...]:
        return self._rows

    def active(self) -> AccountRow | None:
        for row in self._rows:
            if row.is_active:
                return row
        return None

    def autoswitch_enabled(self) -> bool:
        return False

    def autoswitch_state_path(self) -> Path | None:
        # The real adapter returns ``<backup>/autoswitch_state.json``; the
        # worker takes its parent, which is how it learns the backup dir
        # without importing claude_swap itself.
        return None if self._backup is None else self._backup / "autoswitch_state.json"


def _worker(accounts: Any, published: list[UiSnapshot]) -> BackgroundWorker:
    return BackgroundWorker(
        publish=published.append,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=accounts,
    )


@contextmanager
def _captured_log() -> Iterator[list[str]]:
    """Collect ``app._log`` lines instead of writing them to stderr."""
    lines: list[str] = []
    original = app_mod._log
    app_mod._log = lines.append  # type: ignore[assignment]
    try:
        yield lines
    finally:
        app_mod._log = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 1-3. reading the registry
# ---------------------------------------------------------------------------


def test_default_profile_sessions_belong_to_the_active_login() -> None:
    """A session under ``~/.claude`` is spending whatever the default login is.

    That is the whole scaling story the widget could not tell: four terminals
    on the default profile share ONE 5-hour window and drain it four times as
    fast. The attribution is the active slot and nothing else - the registry
    records no account, so guessing one would be a fabrication (SPEC 4.3).
    """
    with tempfile.TemporaryDirectory() as name:
        sessions = Path(name) / "sessions"
        _write_session(sessions, os.getpid(), cwd="/tmp/obsidian", name="obsidian-2d")
        with _sessions_dir(sessions):
            snapshot = fleet.collect(backup_dir=None, active_slot=1)

    assert len(snapshot.sessions) == 1, snapshot.sessions
    row = snapshot.sessions[0]
    assert (row.slot, row.pinned) == (1, False)
    assert row.name == "obsidian-2d"
    assert row.cwd == "/tmp/obsidian", "cwd must be verbatim"
    assert row.status == "busy"
    assert all("not scanned" in note for note in snapshot.notes), snapshot.notes


def test_dead_and_non_interactive_records_are_not_seats() -> None:
    """Liveness and ``kind`` both gate a row.

    A stale record for a process that exited is not a seat at the fleet, and
    Claude's own ``bg``/``daemon`` helpers are not terminals a human is typing
    in. Counting either inflates the "4 on main" number the operator uses to
    decide whether to spread the fleet.
    """
    with tempfile.TemporaryDirectory() as name:
        sessions = Path(name) / "sessions"
        _write_session(sessions, os.getpid(), cwd="/tmp/live")
        _write_session(sessions, DEAD_PID, cwd="/tmp/dead")
        # A LIVE pid whose kind is not interactive: the kind gate, not the
        # liveness gate, is what must drop this one.
        _write_session(sessions, os.getpid(), cwd="/tmp/bg", kind="bg", stem="bg")
        with _sessions_dir(sessions):
            snapshot = fleet.collect(backup_dir=None, active_slot=2)

    cwds = [row.cwd for row in snapshot.sessions]
    assert cwds == ["/tmp/live"], cwds
    assert all("not scanned" in note for note in snapshot.notes), snapshot.notes


def test_unreadable_session_record_is_reported_not_swallowed() -> None:
    """"No sessions" and "no readable records" are different answers.

    claude-swap's own scanner returns the unreadable count for exactly this
    reason; dropping it here would let a corrupt registry file render as a
    smaller, confident fleet.
    """
    with tempfile.TemporaryDirectory() as name:
        sessions = Path(name) / "sessions"
        _write_session(sessions, os.getpid(), cwd="/tmp/live")
        (sessions / "9999.json").write_text("{not json", encoding="utf-8")
        with _sessions_dir(sessions):
            snapshot = fleet.collect(backup_dir=None, active_slot=1)

    assert len(snapshot.sessions) == 1
    assert any("1 session record unreadable" in note for note in snapshot.notes), snapshot.notes


# ---------------------------------------------------------------------------
# 4-6. attribution across profiles and mappings
# ---------------------------------------------------------------------------


def test_profile_sessions_are_pinned_to_their_slot() -> None:
    """A registry file inside ``<backup>/sessions/<N>-<slug>/`` belongs to N.

    That is the only durable account attribution available: ``cswap run N``
    exports ``CLAUDE_CONFIG_DIR`` at that path, so Claude writes its pid file
    there. Such a session does NOT follow a default-login switch, which is
    what ``pinned`` tells the operator.
    """
    _require_claude_swap()
    from claude_swap.session import session_dir_for

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        backup = root / "backup"
        default_sessions = root / "default" / "sessions"
        _write_sequence(backup, {1: ("main@x.io", "org-1"), 3: ("podol@x.io", "org-3")})
        _write_session(default_sessions, os.getpid(), cwd="/tmp/default-seat")
        profile = Path(session_dir_for(backup, "3", "podol@x.io")) / "sessions"
        _write_session(profile, os.getpid(), cwd="/tmp/pinned-seat", name="linkagent")

        with _sessions_dir(default_sessions):
            snapshot = fleet.collect(backup_dir=backup, active_slot=1)

    by_cwd = {row.cwd: row for row in snapshot.sessions}
    assert set(by_cwd) == {"/tmp/default-seat", "/tmp/pinned-seat"}, by_cwd
    assert (by_cwd["/tmp/default-seat"].slot, by_cwd["/tmp/default-seat"].pinned) == (1, False)
    assert (by_cwd["/tmp/pinned-seat"].slot, by_cwd["/tmp/pinned-seat"].pinned) == (3, True)
    assert by_cwd["/tmp/pinned-seat"].name == "linkagent"


def test_mappings_resolve_by_identity_not_by_slot() -> None:
    """A pin stores ``(email, organizationUuid)``; slots are reused.

    Upstream is explicit that slot numbers are recycled when an account is
    removed and re-added, so resolving a mapping by anything but the identity
    composite would eventually point a directory at a stranger's account. The
    org half matters: the same email under a different organization is a
    DIFFERENT account, and must not resolve.
    """
    with tempfile.TemporaryDirectory() as name:
        backup = Path(name) / "backup"
        _write_sequence(
            backup,
            {
                1: ("main@x.io", "org-1"),
                2: ("shared@x.io", "org-A"),
                5: ("shared@x.io", "org-B"),
            },
        )
        _write_mappings(
            backup,
            {
                "/tmp/work": ("shared@x.io", "org-B"),
                "/tmp/orphan": ("gone@x.io", "org-Z"),
            },
        )
        rows = fleet.load_mappings(backup)

    by_path = {Path(row.path).name: row for row in rows}
    assert by_path["work"].slot == 5, by_path["work"]
    # The account was removed: no slot, and the email is kept so the menu can
    # still say WHICH identity the stale pin names.
    assert by_path["orphan"].slot is None
    assert by_path["orphan"].email == "gone@x.io"


def test_pending_pin_is_labelled_next_launch() -> None:
    """A mapping never moves a RUNNING session, and the row must not imply it.

    ``cswap map`` governs the next ``cswap run`` in that directory. A session
    already running on the default login keeps that login, so its row shows the
    real attribution plus the pending pin as a separate, future-tense fact.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        backup = root / "backup"
        sessions = root / "default" / "sessions"
        work = root / "work"
        (work / "nested").mkdir(parents=True)
        _write_sequence(backup, {1: ("main@x.io", "org-1"), 3: ("podol@x.io", "org-3")})
        _write_mappings(backup, {str(work): ("podol@x.io", "org-3")})
        _write_session(sessions, os.getpid(), cwd=str(work / "nested"), name="linkagent")
        with _sessions_dir(sessions):
            snapshot = fleet.collect(backup_dir=backup, active_slot=1)

    row = snapshot.sessions[0]
    assert (row.slot, row.pinned) == (1, False), "it is still on the default login"
    assert row.mapped_slot == 3, "the nested dir inherits its ancestor's pin"

    label = app_mod._session_row_label(row, {1: "main", 3: "podol"})
    assert "→ main (default)" in label, label
    assert "(mapped → podol, next launch)" in label, label


def test_unpin_offers_only_the_directorys_own_mapping() -> None:
    """``MappingStore.remove`` is an exact-key delete.

    Offering "Unpin" for a pin a directory merely INHERITED would either no-op
    or invite deleting a parent mapping that governs sibling directories too.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        backup = root / "backup"
        parent = root / "work"
        child = parent / "nested"
        child.mkdir(parents=True)
        _write_sequence(backup, {3: ("podol@x.io", "org-3")})
        _write_mappings(backup, {str(parent): ("podol@x.io", "org-3")})
        mappings = fleet.load_mappings(backup)

        assert fleet.exact_mapping(str(parent), mappings) is not None
        assert fleet.exact_mapping(str(child), mappings) is None
        # ...while attribution still walks ancestors, as upstream does.
        assert fleet._mapping_for(str(child), mappings) is not None

        # And the verdict reaches the row, decided on the worker: the menu
        # builder must not resolve a path on the AppKit main thread (SPEC 2.3).
        sessions = root / "default" / "sessions"
        _write_session(sessions, os.getpid(), cwd=str(parent), stem="parent")
        _write_session(sessions, os.getpid(), cwd=str(child), stem="child")
        with _sessions_dir(sessions):
            snapshot = fleet.collect(backup_dir=backup, active_slot=1)
        by_cwd = {row.cwd: row for row in snapshot.sessions}
        assert by_cwd[str(parent)].pinned_here is True
        assert by_cwd[str(child)].pinned_here is False


# ---------------------------------------------------------------------------
# 7-9. writing a pin from the menu
# ---------------------------------------------------------------------------


def test_pin_writes_identity_and_warns_when_target_is_active() -> None:
    """Pinning writes ``(email, org)`` - and says when the pin buys nothing.

    ``cswap run N`` on the account that is ALREADY the default login takes the
    same-account fast path: it execs plain ``claude`` on the default profile,
    so the session is not isolated and will follow the next switch. Silently
    writing that mapping would let the operator believe the fleet was spread
    when it was not.
    """
    # `fleet.set_mapping` writes through claude-swap's own `MappingStore`.
    _require_claude_swap()
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        backup = root / "backup"
        work = root / "work"
        work.mkdir()
        _write_sequence(backup, {1: ("main@x.io", "org-1"), 3: ("podol@x.io", "org-3")})
        accounts = _FakeAccounts(
            backup, (_row(1, "main", "main@x.io", active=True), _row(3, "podol", "podol@x.io"))
        )
        published: list[UiSnapshot] = []
        worker = _worker(accounts, published)

        with _sessions_dir(root / "no-sessions"), _captured_log() as log:
            worker._map_directory(str(work), 3)
            quiet = list(log)
            worker._map_directory(str(work), 1)
            noisy = list(log)

        stored = json.loads((backup / "mappings.json").read_text(encoding="utf-8"))

    entry = stored["mappings"][fleet._normalize_path(work)]
    assert (entry["email"], entry["organizationUuid"]) == ("main@x.io", "org-1"), entry
    assert not any("default login" in line for line in quiet), quiet
    assert any("UNPINNED" in line for line in noisy), noisy
    assert published, "a pin must repaint the fleet"
    assert published[-1].mappings and published[-1].mappings[0].slot == 1


def test_pin_refuses_a_slot_whose_identity_is_unreadable() -> None:
    """No identity, no mapping - and the refusal is visible in the menu.

    A mapping written with a guessed organization uuid resolves to nothing on
    the next launch: upstream matches on the (email, org) composite. Failing
    loudly beats writing a pin that silently never fires (Rule 12).
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        backup = root / "backup"
        backup.mkdir()  # no sequence.json: identities unreadable
        work = root / "work"
        work.mkdir()
        accounts = _FakeAccounts(backup, (_row(1, "main", "main@x.io", active=True),))
        published: list[UiSnapshot] = []
        worker = _worker(accounts, published)
        with _sessions_dir(root / "no-sessions"), _captured_log():
            worker._handle_command((app_mod._CMD_MAP_DIR, (str(work), 1)))

        assert not (backup / "mappings.json").exists(), "nothing may be written"

    assert published, "the refusal must reach the menu"
    error = published[-1].accounts_error or ""
    assert "identity" in error and "1" in error, error


def test_fleet_collection_never_raises() -> None:
    """A broken fleet read degrades to a note, never to a failed accounts tick.

    The accounts job owns the title. A fleet pass is an extra, and an extra
    that can raise would take the whole menu with it.
    """

    class _Exploding(_FakeAccounts):
        def autoswitch_state_path(self) -> Path:
            raise RuntimeError("backup dir unreadable")

    published: list[UiSnapshot] = []
    worker = _worker(_Exploding(None, (_row(1, "main", "main@x.io", active=True),)), published)
    with tempfile.TemporaryDirectory() as name, _sessions_dir(Path(name)):
        worker._run_accounts_job(force=False)

    assert published, "the accounts job must still publish"
    snapshot = published[-1]
    assert snapshot.accounts_error is None, snapshot.accounts_error
    assert snapshot.sessions == ()
    # `_backup_dir` swallows the failure, so the pass simply finds no backup
    # dir and reports an empty, note-free fleet rather than a fabricated one.
    assert any("not scanned" in note for note in snapshot.fleet_notes), snapshot.fleet_notes


# ---------------------------------------------------------------------------
# 10-12. the menu section
# ---------------------------------------------------------------------------


def test_sessions_section_names_the_login_and_its_limits() -> None:
    """The section has to answer three questions in one glance.

    How many seats share the default login (the scaling problem), which ones
    are pinned (the fix), and what a pin actually does (it applies to the next
    launch, not to anything currently running).
    """
    rows = (
        _row(1, "main", "main@x.io", active=True),
        _row(3, "podol", "podol@x.io"),
    )
    sessions = (
        fleet.SessionRow(
            pid=1, cwd="/tmp/a", name="obsidian-2d", status="busy", slot=1, pinned_here=True
        ),
        fleet.SessionRow(pid=2, cwd="/tmp/b", name="linkagent", status="idle", slot=1),
        fleet.SessionRow(pid=3, cwd="/tmp/c", name="flick", status="busy", slot=3, pinned=True),
    )
    snapshot = UiSnapshot(
        settings=normalize_settings(dict(SETTINGS_DEFAULTS)),
        accounts=rows,
        active=rows[0],
        sessions=sessions,
        mappings=(fleet.MappingRow(path=fleet._normalize_path("/tmp/a"), email="podol@x.io", slot=3),),
        fleet_notes=("1 session record unreadable — the fleet may be larger than this",),
        fleet_scanned=True,
    )

    app = app_mod.CCUsageWidgetApp()
    try:
        items = app._session_items(snapshot)
        titles = [item.title for item in items]
        assert titles[0] == "Sessions — 2 on main (default login) · 1 pinned", titles[0]
        assert any("obsidian-2d" in t and "→ main (default)" in t for t in titles), titles
        assert any("flick" in t and "→ podol (pinned)" in t for t in titles), titles
        assert any("unreadable" in t for t in titles), titles
        assert any(fleet.PIN_HINT in t for t in titles), titles

        # The row carrying an exact pin offers to remove it; the others do not.
        pinned_row = next(item for item in items if "obsidian-2d" in item.title)
        children = [child.title for child in pinned_row.values()]
        assert any(title.startswith("Pin directory to") for title in children), children
        assert "Unpin directory" in children, children
        plain_row = next(item for item in items if "linkagent" in item.title)
        plain_children = [child.title for child in plain_row.values()]
        assert any("not pinned" in title for title in plain_children), plain_children

        # And the whole menu still builds with the section in it.
        app.rebuild_menu(snapshot)
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_absent_fleet_renders_no_section() -> None:
    """No sessions and nothing to report: no heading at all.

    Same rule the Codex quota block follows (SPEC-CODEX 5.5) - an empty
    heading is a claim, and "we found nothing" is not worth a claim.
    """
    app = app_mod.CCUsageWidgetApp()
    try:
        empty = UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS)))
        assert app._session_items(empty) == []
        # ...but a note ALONE is enough to draw it: a fleet we could not read
        # must never look like a fleet that is not there.
        noted = replace(empty, fleet_notes=("2 session records unreadable",))
        assert app._session_items(noted), "a note must still be shown"
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# Runner (pytest is not installed in claude-swap's venv)
# ---------------------------------------------------------------------------


# -- review fixes 2026-09-01 -------------------------------------------------


def test_pinned_count_is_only_claimed_when_the_profile_scan_ran() -> None:
    """``· 0 pinned`` was asserted even when no profile directory was read
    (no backup dir, or no identities) — an invented number about the very
    fact the section exists to convey."""
    # `fleet.collect` reaches claude-swap's profile-session layout to decide
    # whether the pinned count was scanned at all.
    _require_claude_swap()
    with tempfile.TemporaryDirectory() as tmp:
        sessions = Path(tmp) / "sessions"
        _write_session(sessions, os.getpid(), cwd="/tmp/a", name="a")
        with _sessions_dir(sessions):
            unscanned = fleet.collect(backup_dir=None, active_slot=1)
            assert unscanned.pinned_scanned is False
            assert any("not scanned" in n for n in unscanned.notes), unscanned.notes
            names = {1: "main"}
            heading = app_mod._fleet_heading(unscanned.sessions, names, scanned=False)
            assert "pinned" not in heading, heading

            backup = Path(tmp) / "backup"
            _write_sequence(backup, {1: ("m@x.io", "org-1")})
            scanned = fleet.collect(backup_dir=backup, active_slot=1)
            assert scanned.pinned_scanned is True, scanned.notes
            assert "· 0 pinned" in app_mod._fleet_heading(scanned.sessions, names, scanned=True)

            empty = fleet.collect(backup_dir=Path(tmp) / "nowhere", active_slot=1)
            assert empty.pinned_scanned is False
            assert any("identities unreadable" in n for n in empty.notes), empty.notes


def test_missing_session_module_is_reported_not_silent() -> None:
    original = fleet.profile_sessions_dir
    fleet.profile_sessions_dir = lambda *a, **k: None  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "backup"
            _write_sequence(backup, {3: ("p@x.io", "org-3")})
            with _sessions_dir(Path(tmp) / "none"):
                snapshot = fleet.collect(backup_dir=backup, active_slot=1)
        assert snapshot.pinned_scanned is False
        assert any("pinned sessions not scanned" in n for n in snapshot.notes), snapshot.notes
        assert not [r for r in snapshot.sessions if r.pinned]
    finally:
        fleet.profile_sessions_dir = original  # type: ignore[assignment]


def test_emails_fallback_scans_profiles_without_sequence_json() -> None:
    """The widget's own rows know slot→email even when ``sequence.json`` is
    unreadable; a pinned session must still be found through them."""
    _require_claude_swap()
    from claude_swap.session import session_dir_for

    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "backup"
        backup.mkdir()
        profile = Path(session_dir_for(backup, "3", "p@x.io")) / "sessions"
        _write_session(profile, os.getpid(), cwd="/tmp/p", name="pinned-one")
        with _sessions_dir(Path(tmp) / "none"):
            snapshot = fleet.collect(backup_dir=backup, active_slot=1, emails={3: "p@x.io"})
    pinned = [r for r in snapshot.sessions if r.pinned]
    assert pinned and pinned[0].slot == 3 and pinned[0].name == "pinned-one", snapshot


def test_pin_refuses_a_session_without_a_directory() -> None:
    """``MappingStore.set("")`` resolves to the widget's OWN cwd; a session
    that reported no directory must never reach it."""
    published: list[UiSnapshot] = []
    worker = _worker(_FakeAccounts(None, ()), published)
    for bad in ("", "relative/path"):
        try:
            worker._map_directory(bad, 1)
        except RuntimeError as exc:
            assert "no absolute directory" in str(exc), exc
        else:
            raise AssertionError(f"{bad!r} was accepted as a pin target")


def test_a_record_that_vanishes_mid_pass_is_not_an_unreadable_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sessions = Path(tmp) / "sessions"
        path = _write_session(sessions, os.getpid(), cwd="/tmp/a")
        original = Path.read_text

        def _vanish(self: Path, *a: Any, **k: Any) -> str:
            if self == path:
                path.unlink(missing_ok=True)
                raise FileNotFoundError(str(self))
            return original(self, *a, **k)

        Path.read_text = _vanish  # type: ignore[assignment]
        try:
            records, unreadable = fleet.read_registry(sessions)
        finally:
            Path.read_text = original  # type: ignore[assignment]
    assert records == () and unreadable == 0, (records, unreadable)


def _tests() -> list[tuple[str, Any]]:
    items = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def _uncollected_tests(collected: list[tuple[str, Any]], source: str | None = None) -> list[str]:
    """Tests declared in this file but never collected (see test_regressions)."""
    if source is None:
        try:
            source = Path(__file__).read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - the file is right there
            return []
    declared = re.findall(r"^def (test_\w+)", source, re.MULTILINE)
    found = {name for name, _ in collected}
    return [name for name in declared if name not in found]


def main() -> int:
    failures: list[str] = []
    skipped: list[str] = []
    tests = _tests()
    for name, func in tests:
        try:
            func()
        except _NeedsClaudeSwap as exc:
            # Counted and named, never hidden: the summary must not be able to
            # read "all green" on a machine that could not run four of them.
            skipped.append(name)
            print(f"skip  {name}: {exc}")
        except Exception:
            failures.append(name)
            print(f"FAIL  {name}")
            print(traceback.format_exc().rstrip())
        else:
            print(f"pass  {name}")
    total = len(tests)
    passed = total - len(failures) - len(skipped)
    tail = f", {len(skipped)} skipped" if skipped else ""
    print(f"\n{passed} passed, {len(failures)} failed{tail}, out of {total}")
    if skipped:
        print("skipped (claude_swap not installed): " + ", ".join(skipped))
    if failures:
        print("failed: " + ", ".join(failures))
    orphans = _uncollected_tests(tests)
    if orphans:
        print(
            f"ERROR: {len(orphans)} test(s) are defined in this file but were never "
            f"collected — they sit below the `if __name__` guard: " + ", ".join(orphans)
        )
    return 1 if (failures or orphans) else 0


if __name__ == "__main__":
    raise SystemExit(main())
