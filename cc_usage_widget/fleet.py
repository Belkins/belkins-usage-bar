"""Fleet visibility: which Claude Code sessions are live, and on whose login.

The widget has always shown *accounts*; this module shows the **fleet** — the
Claude Code processes actually spending those accounts' quota, and which
account each one is spending.

Two facts make the attribution possible without asking any process anything:

* Claude Code writes a registry file per live instance into
  ``$CLAUDE_CONFIG_DIR/sessions/<pid>.json`` (the same mechanism claude-swap's
  ``process_detection`` reads);
* ``cswap run N`` launches Claude with ``CLAUDE_CONFIG_DIR`` pointing at
  ``<backup>/sessions/<N>-<email-slug>/``, so a registry file's *location* is
  the account it belongs to (``claude_swap.session.session_dir_for``).

So: registry files under the default config dir belong to the **default login**
(claude-swap's active slot, whatever that is right now — those sessions follow
every future switch); registry files under a profile directory belong to that
profile's slot and are **pinned**.

Honesty rules this module obeys (SPEC 4.3):

* ``cwd`` is passed through verbatim; nothing here infers a project name;
* a default-profile session is attributed to the active slot and to nothing
  else — there is no way to tell from the registry which account it *was*
  started on, so we never guess;
* a registry record we cannot read is counted and reported (``N unreadable``),
  never silently skipped: "no sessions" and "no readable records" are
  different answers;
* a directory mapping only affects the **next** ``cswap run`` in that
  directory, so it is rendered as a separate, clearly-labelled fact and never
  as the session's current login.

Cost: pure file reads (``glob`` + ``json.loads`` + ``os.kill(pid, 0)``) over a
handful of small files — no subprocess, nothing on the main thread, well inside
the SPEC 2.1 per-tick budget.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

LOGGER = logging.getLogger("cc_usage_widget.fleet")

SESSIONS_DIR_ENV = "CC_USAGE_WIDGET_SESSIONS_DIR"
"""Override for the default-profile session registry directory (tests)."""

INTERACTIVE_KIND = "interactive"
"""Only kind we render. ``bg``/``daemon``/``daemon-worker`` records are
Claude's own helpers, not seats at the fleet."""

PIN_HINT = (
    "Pins apply to the next 'cswap run --share-history' in that directory; "
    "running sessions keep their login"
)


@dataclass(frozen=True)
class SessionRow:
    """One live Claude Code instance, attributed to an account slot."""

    pid: int
    cwd: str
    """Verbatim, as the process reported it."""
    name: str = ""
    """Claude's own label for the session (``obsidian-2d``), or ``""``."""
    status: str = ""
    """``busy`` / ``idle`` / ``waiting``, verbatim; ``""`` when unreported."""
    started_at: float = 0.0
    slot: int | None = None
    """Account this session is spending. ``None`` = unknown (no active slot)."""
    pinned: bool = False
    """True = it runs in a ``cswap run`` profile, so a default-login switch
    does not move it. False = it follows the default login."""
    mapped_slot: int | None = None
    """Slot this session's directory is *mapped* to, when that differs from
    :attr:`slot`. It takes effect on the next launch, never now."""
    mapped_email: str = ""
    """Identity of the mapping above, when its slot could not be resolved."""
    pinned_here: bool = False
    """Whether THIS directory owns a pin (as opposed to inheriting one).

    Computed here, on the worker, because deciding it means normalising a path
    - a filesystem call - and the menu builder runs on the AppKit main thread
    (SPEC 2.3), where no syscall of ours is allowed.
    """


@dataclass(frozen=True)
class MappingRow:
    """One ``<backup>/mappings.json`` entry: a directory pinned to an identity."""

    path: str
    email: str
    org_uuid: str = ""
    slot: int | None = None
    """Live slot for ``(email, org_uuid)``, or ``None`` when the account is
    gone (upstream then falls back to the default login on the next launch)."""


@dataclass(frozen=True)
class FleetSnapshot:
    """What one fleet pass learned. Empty is a valid, silent answer."""

    sessions: tuple[SessionRow, ...] = ()
    mappings: tuple[MappingRow, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    """Lines the menu must show verbatim: ``2 unreadable``, or why a half of
    this pass could not run. Never empty *and* wrong — a fact we could not
    establish gets a note instead of a fabricated row."""
    pinned_scanned: bool = False
    """Whether the per-account profile directories were actually read. False
    means "0 pinned" is NOT a fact the menu may claim (review 2026-09-01)."""

    @property
    def default_sessions(self) -> tuple[SessionRow, ...]:
        return tuple(row for row in self.sessions if not row.pinned)

    @property
    def pinned_sessions(self) -> tuple[SessionRow, ...]:
        return tuple(row for row in self.sessions if row.pinned)


# ---------------------------------------------------------------------------
# Registry reading
# ---------------------------------------------------------------------------


def sessions_dir() -> Path:
    """Directory holding the default profile's session registry files.

    ``CC_USAGE_WIDGET_SESSIONS_DIR`` overrides it (tests). Otherwise claude-swap
    resolves the Claude config home for us — it honours ``CLAUDE_CONFIG_DIR``
    exactly as Claude Code does — and ``~/.claude`` is the last resort.
    """
    override = os.environ.get(SESSIONS_DIR_ENV)
    if override:
        return Path(override)
    try:
        from claude_swap.paths import get_claude_config_home

        return Path(get_claude_config_home()) / "sessions"
    except Exception:  # pragma: no cover - claude-swap absent
        return Path.home() / ".claude" / "sessions"


def _pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)``, with EPERM counting as alive (another uid owns it)."""
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def read_registry(directory: Path) -> tuple[tuple[dict[str, Any], ...], int]:
    """Live ``interactive`` records under *directory*, plus an unreadable count.

    Our own parser rather than ``process_detection.scan_sessions`` for one
    reason: that function's ``ClaudeSession`` drops the registry's ``name``
    field, and the whole point of a fleet row is that the operator recognises
    the session by its name. The liveness rule and the "count what you could
    not read" contract are copied from it deliberately.
    """
    if not directory.is_dir():
        return (), 0
    records: list[dict[str, Any]] = []
    unreadable = 0
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return (), 0
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data["pid"])
        except FileNotFoundError:
            # Gone between glob and read: a session that exited during the
            # pass, not a record we failed to read.
            continue
        except (
            OSError,
            ValueError,  # JSONDecodeError, UnicodeDecodeError, bad int
            TypeError,
            KeyError,
            AttributeError,  # valid JSON that is not an object
            RecursionError,
            OverflowError,
        ) as exc:
            unreadable += 1
            LOGGER.debug("fleet: unreadable session record %s: %r", path, exc)
            continue
        if not _pid_alive(pid):
            continue
        if str(data.get("kind", "")) != INTERACTIVE_KIND:
            continue
        records.append({**data, "pid": pid})
    return tuple(records), unreadable


def _row_from_record(
    record: Mapping[str, Any], *, slot: int | None, pinned: bool
) -> SessionRow:
    started = record.get("startedAt", 0)
    try:
        started_at = float(started) / 1000.0 if started else 0.0
    except (TypeError, ValueError):
        started_at = 0.0
    return SessionRow(
        pid=int(record["pid"]),
        cwd=str(record.get("cwd", "") or ""),
        name=str(record.get("name", "") or ""),
        status=str(record.get("status") or ""),
        started_at=started_at,
        slot=slot,
        pinned=pinned,
    )


# ---------------------------------------------------------------------------
# claude-swap side: identities, profiles, mappings
# ---------------------------------------------------------------------------


def account_identities(backup_dir: Path | None) -> dict[int, tuple[str, str]]:
    """``{slot: (email, organizationUuid)}`` from ``<backup>/sequence.json``.

    Read directly rather than through ``switcher``: constructing a switcher is
    the heavy path (Keychain, migrations), while this is the same plain JSON
    that ``switcher._find_account_slot`` scans. Unreadable file -> ``{}``, and
    every caller then degrades to "slot unknown" rather than to a guess.
    """
    if backup_dir is None:
        return {}
    path = Path(backup_dir) / "sequence.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    out: dict[int, tuple[str, str]] = {}
    for key, record in accounts.items():
        if not isinstance(record, Mapping):
            continue
        try:
            slot = int(key)
        except (TypeError, ValueError):
            continue
        email = str(record.get("email", "") or "")
        if not email:
            continue
        out[slot] = (email, str(record.get("organizationUuid", "") or ""))
    return out


def identity_for_slot(backup_dir: Path | None, slot: int) -> tuple[str, str] | None:
    """``(email, org_uuid)`` for *slot*, or ``None`` when it is unknown.

    A pin is stored by identity, so a slot whose identity we cannot read must
    fail the pin loudly — writing a mapping with a guessed org silently
    produces one that never resolves.
    """
    return account_identities(backup_dir).get(int(slot))


def profile_sessions_dir(backup_dir: Path, slot: int, email: str) -> Path | None:
    """Registry directory inside slot *slot*'s ``cswap run`` profile.

    The ``<num>-<slug>`` naming is upstream's (``session.session_dir_for``);
    it is imported rather than reimplemented so a rename there lands as "no
    pinned sessions found", not as a confidently wrong directory.
    """
    try:
        from claude_swap.session import session_dir_for
    except Exception:  # pragma: no cover - claude-swap absent
        return None
    try:
        return Path(session_dir_for(Path(backup_dir), str(slot), email)) / "sessions"
    except Exception:  # pragma: no cover - upstream signature change
        return None


def _normalize_path(value: str | Path) -> str:
    """Mapping key normalisation, matching ``claude_swap.mappings``."""
    try:
        resolved = Path(value).expanduser().resolve()
    except OSError:  # pragma: no cover - unresolvable path
        resolved = Path(value)
    return os.path.normcase(str(resolved))


def load_mappings(
    backup_dir: Path | None, identities: Mapping[int, tuple[str, str]] | None = None
) -> tuple[MappingRow, ...]:
    """Directory pins from ``<backup>/mappings.json``, resolved to slots.

    Resolution is by **identity** — ``(email, organizationUuid)`` — because slot
    numbers are reused when an account is removed and re-added; that is
    upstream's own rule (``mappings.py`` module docstring). An entry whose
    identity matches no live account keeps ``slot=None`` and renders as such.
    """
    if backup_dir is None:
        return ()
    path = Path(backup_dir) / "mappings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(data, dict):
        return ()
    entries = data.get("mappings")
    if not isinstance(entries, dict):
        return ()
    if identities is None:
        identities = account_identities(backup_dir)
    rows: list[MappingRow] = []
    for key, entry in entries.items():
        if not isinstance(entry, Mapping):
            continue
        email = str(entry.get("email", "") or "")
        org = str(entry.get("organizationUuid", "") or "")
        slot = None
        for candidate, (cand_email, cand_org) in identities.items():
            if cand_email == email and cand_org == org:
                slot = candidate
                break
        rows.append(MappingRow(path=str(key), email=email, org_uuid=org, slot=slot))
    return tuple(sorted(rows, key=lambda row: row.path))


def _mapping_for(cwd: str, mappings: Iterable[MappingRow]) -> MappingRow | None:
    """The longest mapped ancestor of *cwd*, mirroring ``MappingStore.resolve``."""
    if not cwd:
        return None
    target = Path(_normalize_path(cwd))
    best: MappingRow | None = None
    for row in mappings:
        candidate = Path(row.path)
        if candidate == target or candidate in target.parents:
            if best is None or len(row.path) > len(best.path):
                best = row
    return best


def exact_mapping(cwd: str, mappings: Iterable[MappingRow]) -> MappingRow | None:
    """The pin on *cwd* ITSELF, never one it merely inherits from an ancestor.

    ``Unpin`` must only offer to remove a mapping this directory owns:
    ``MappingStore.remove`` is an exact-key delete, so offering it for an
    inherited pin would either no-op (confusing) or invite removing a parent's
    mapping that governs other directories too.
    """
    key = _normalize_path(cwd) if cwd else ""
    if not key:
        return None
    for row in mappings:
        if row.path == key:
            return row
    return None


def set_mapping(backup_dir: Path, path: str | Path, email: str, org_uuid: str) -> None:
    """Pin *path* to ``(email, org_uuid)`` via upstream's atomic writer.

    Never hand-rolled: ``mappings.json`` is claude-swap's file, and its
    tempfile+replace write (and 0600/0700 modes) are part of the contract.
    """
    from claude_swap.mappings import MappingStore

    MappingStore(Path(backup_dir)).set(path, email, org_uuid)


def clear_mapping(backup_dir: Path, path: str | Path) -> bool:
    """Remove the pin for *path*. Returns whether one was there."""
    from claude_swap.mappings import MappingStore

    return bool(MappingStore(Path(backup_dir)).remove(path))


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def collect(
    *,
    backup_dir: Path | None,
    active_slot: int | None,
    emails: Mapping[int, str] | None = None,
) -> FleetSnapshot:
    """One fleet pass. Never raises; anything it could not learn becomes a note.

    Args:
        backup_dir: claude-swap's backup root, or ``None`` when claude-swap is
            not wired — the default-profile fleet is still reported.
        active_slot: the slot the default login currently points at. Every
            default-profile session is attributed to it, and to nothing else.
        emails: ``{slot: email}`` from the widget's own rows, merged with the
            identities on disk so a slot missing from either source is still
            scanned for pinned sessions.
    """
    notes: list[str] = []
    rows: list[SessionRow] = []

    records, unreadable = read_registry(sessions_dir())
    rows.extend(
        _row_from_record(record, slot=active_slot, pinned=False) for record in records
    )

    identities = account_identities(backup_dir)
    known: dict[int, str] = {slot: email for slot, (email, _org) in identities.items()}
    for slot, email in (emails or {}).items():
        if email:
            known.setdefault(int(slot), str(email))

    pinned_scanned = False
    if backup_dir is None:
        notes.append("claude-swap backup directory unknown — pinned sessions not scanned")
    elif not known:
        notes.append("account identities unreadable — pinned sessions not scanned")
    else:
        missing_upstream = False
        for slot in sorted(known):
            directory = profile_sessions_dir(Path(backup_dir), slot, known[slot])
            if directory is None:
                missing_upstream = True
                break
            profile_records, profile_unreadable = read_registry(directory)
            unreadable += profile_unreadable
            rows.extend(
                _row_from_record(record, slot=slot, pinned=True)
                for record in profile_records
            )
        if missing_upstream:
            notes.append(
                "claude-swap session module unavailable — pinned sessions not scanned"
            )
        else:
            pinned_scanned = True

    mappings = load_mappings(backup_dir, identities)
    resolved: list[SessionRow] = []
    for row in rows:
        mapping = None if row.pinned else _mapping_for(row.cwd, mappings)
        # An unresolvable mapping (its account is gone) is always pending:
        # with no active slot known, `None != None` would hide a dead pin.
        pending = mapping is not None and (mapping.slot is None or mapping.slot != row.slot)
        own = exact_mapping(row.cwd, mappings) is not None if mappings else False
        if pending or own:
            row = SessionRow(
                pid=row.pid,
                cwd=row.cwd,
                name=row.name,
                status=row.status,
                started_at=row.started_at,
                slot=row.slot,
                pinned=row.pinned,
                mapped_slot=mapping.slot if pending else None,
                mapped_email=(
                    mapping.email if pending and mapping.slot is None else ""
                ),
                pinned_here=own,
            )
        resolved.append(row)

    if unreadable:
        notes.append(
            f"{unreadable} session record{'s' if unreadable != 1 else ''} unreadable — "
            "the fleet may be larger than this"
        )

    resolved.sort(key=lambda row: (row.slot is None, row.slot or 0, row.cwd, row.pid))
    return FleetSnapshot(
        sessions=tuple(resolved),
        mappings=mappings,
        notes=tuple(notes),
        pinned_scanned=pinned_scanned,
    )
