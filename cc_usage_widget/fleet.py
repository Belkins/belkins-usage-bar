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
import re
import time
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
    owner_slot: int | None = None
    """Slot owning the latest ``organizationUuid`` in this session's transcript,
    when computable on the worker; ``None`` otherwise."""
    org_shifted: bool = False
    """True when an unpinned session's transcript ``organizationUuid`` belongs to
    a different organization than its current ``slot`` (risking thinking drop)."""
    session_id: str = ""
    """Claude Code session UUID from ``<pid>.json`` (used for ``claude --resume``)."""
    latched_credits: bool = False
    """True when the live session's recent transcript tail tripped Claude Code's
    usage-credits latch without a subsequent successful assistant response."""
    hit_rate_limit: bool = False
    """True when the live session's recent transcript tail hit a hard rate-limit
    without a subsequent successful assistant response."""


SessionInfo = SessionRow
"""Backwards-compatible alias for :class:`SessionRow`."""


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


_PID_MATCH_CACHE: dict[tuple[int, str], tuple[float, bool]] = {}
_PID_MATCH_TTL_S = 60.0
_TRANSCRIPT_TAIL_BYTES = 256 * 1024


def _pid_matches_proc_start(pid: int, proc_start: Any) -> bool:
    """Verify ``procStart`` against the live process (cached 60s) so recycled macOS PIDs
    are rejected. When ``procStart`` is absent (older records / unit tests), returns True."""
    if not proc_start:
        return True
    key = (pid, str(proc_start))
    now = time.monotonic()
    cached = _PID_MATCH_CACHE.get(key)
    if cached is not None and (now - cached[0]) < _PID_MATCH_TTL_S:
        return cached[1]
    try:
        from claude_swap.process_detection import pid_matches_record

        matched = bool(pid_matches_record(pid, str(proc_start)))
    except ImportError:
        matched = _stdlib_pid_matches_proc_start(pid, str(proc_start))
    except Exception:
        matched = True
    if len(_PID_MATCH_CACHE) > 256:
        _PID_MATCH_CACHE.clear()
    _PID_MATCH_CACHE[key] = (now, matched)
    return matched


def _stdlib_pid_matches_proc_start(pid: int, proc_start: str) -> bool:
    """Fallback ``ps -p <pid> -o lstart=,comm=`` check when ``claude_swap`` is absent."""
    import datetime as dt
    import subprocess

    if proc_start.isdigit():
        return True
    try:
        rec_dt = dt.datetime.strptime(" ".join(proc_start.split()), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return True
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=,comm="],
            env={**os.environ, "LC_ALL": "C"},
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except Exception:
        return True
    if proc.returncode != 0:
        return False
    parts = proc.stdout.strip().split(None, 5)
    if len(parts) < 5:
        return True
    try:
        live_dt = dt.datetime.strptime(" ".join(parts[:5]), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return True
    if live_dt <= rec_dt + dt.timedelta(seconds=5):
        return True
    comm = parts[5].lower() if len(parts) > 5 else ""
    return "claude" in comm


def detect_jsonl_owner_org(jsonl_path: Path) -> str | None:
    """Read at most the trailing 256 KiB of *jsonl_path* and return the most recent
    ``organizationUuid`` recorded in ``credential_org``, ``session-owner``, or turn metadata."""
    try:
        with jsonl_path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size <= 0:
                return None
            start = max(0, size - _TRANSCRIPT_TAIL_BYTES)
            fh.seek(start)
            data = fh.read(_TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return None
    for needle in (b'"credential_org"', b'"session-owner"', b'"organizationUuid"'):
        pos = data.rfind(needle)
        while pos != -1:
            line_start = data.rfind(b"\n", 0, pos) + 1
            line_end = data.find(b"\n", pos)
            if line_end == -1:
                line_end = len(data)
            if start == 0 or line_start > 0:
                try:
                    obj = json.loads(data[line_start:line_end].decode("utf-8", errors="replace"))
                    if isinstance(obj, Mapping):
                        att = obj.get("attachment")
                        org = (
                            (att.get("organizationUuid") if isinstance(att, Mapping) else None)
                            or obj.get("organizationUuid")
                        )
                        if isinstance(org, str) and org.strip():
                            return org.strip()
                except Exception:
                    pass
            if line_start <= 0:
                break
            pos = data.rfind(needle, 0, line_start - 1)
    return None


def detect_workspace_owner_slot(
    cwd: Path | str,
    identities: Mapping[int, tuple[str, str]],
    *,
    current_slot: int | None = None,
    session_id: str | None = None,
    pid: int | None = None,
    projects_root: Path | None = None,
) -> tuple[int | None, bool]:
    """Return ``(owner_slot, org_shifted)`` for *cwd* using bounded 256 KiB tail reads.

    Shared-organization safe: when multiple slots belong to the same ``organizationUuid``
    and *current_slot* already shares that ``organizationUuid``, returns
    ``(current_slot, False)`` so same-org switches are never misreported as shifted.
    """
    org_to_slots: dict[str, list[int]] = {}
    for slot in sorted(identities):
        _email, org = identities[slot]
        if org:
            org_to_slots.setdefault(org, []).append(slot)
    if not org_to_slots:
        return None, False

    cwd_str = str(cwd or "").strip()
    if not cwd_str:
        return None, False

    if projects_root is not None:
        root = projects_root
    else:
        override = os.environ.get(SESSIONS_DIR_ENV)
        root = (
            Path(override).parent / "projects"
            if override
            else (Path.home() / ".claude" / "projects")
        )
    if not root.is_dir():
        return None, False

    slugs = _candidate_project_slugs(cwd_str)

    pdirs = [root / s for s in slugs if (root / s).is_dir()]
    if not pdirs:
        return None, False

    sid = (session_id or "").strip()
    if not sid and pid is not None:
        reg_file = sessions_dir() / f"{pid}.json"
        if reg_file.is_file():
            try:
                sid = str(
                    json.loads(reg_file.read_text(encoding="utf-8", errors="replace")).get("sessionId")
                    or ""
                ).strip()
            except Exception:
                sid = ""

    owner_org: str | None = None
    if sid:
        for pdir in pdirs:
            cand = pdir / f"{sid}.jsonl"
            if cand.is_file():
                org = detect_jsonl_owner_org(cand)
                if org and org in org_to_slots:
                    owner_org = org
                    break

    if owner_org is None:
        newest: list[tuple[float, Path]] = []
        for pdir in pdirs:
            try:
                for p in pdir.glob("*.jsonl"):
                    try:
                        newest.append((p.stat().st_mtime, p))
                    except OSError:
                        continue
            except OSError:
                continue
        newest.sort(key=lambda item: item[0], reverse=True)
        for _mt, cand in newest[:3]:
            org = detect_jsonl_owner_org(cand)
            if org and org in org_to_slots:
                owner_org = org
                break

    if owner_org is None:
        return None, False

    slots_for_org = org_to_slots[owner_org]
    if current_slot is not None:
        cur_ident = identities.get(current_slot)
        cur_org = cur_ident[1] if cur_ident else ""
        if cur_org and cur_org == owner_org:
            return current_slot, False
        return slots_for_org[0], True
    return slots_for_org[0], False


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
        if not _pid_matches_proc_start(pid, data.get("procStart")):
            continue
        if str(data.get("kind", "")) != INTERACTIVE_KIND:
            continue
        records.append({**data, "pid": pid})
    return tuple(records), unreadable


_INTERRUPTION_TAIL_BYTES = 16 * 1024
"""Maximum bytes read from the tail of a live session transcript when scanning
for usage-credits latches or rate-limit interruptions."""

_LATCH_CREDIT_SIGNATURES: tuple[str, ...] = (
    "now requires usage credits",
    "model_requires_usage_credits",
    "consent_unanswered",
    "out of usage credits",
    "you're out of usage credits",
)

_RATE_LIMIT_SIGNATURES: tuple[str, ...] = (
    "hit your fable limit",
    "hit your limit",
    "usage_limit_reached",
)


def _candidate_project_slugs(cwd: str) -> list[str]:
    """Claude Code project-directory slugs corresponding to *cwd*."""
    slugs: list[str] = []
    for cand_slug in (
        re.sub(r"[^a-zA-Z0-9-]", "-", cwd),
        cwd.replace("/", "-").replace(" ", "-"),
        cwd.replace("/", "-"),
    ):
        if cand_slug and cand_slug not in slugs:
            slugs.append(cand_slug)
    return slugs


def _default_projects_roots_for(sess_dir: Path) -> list[Path]:
    """Resolve Claude ``projects/`` roots for *sess_dir*.

    When ``CC_USAGE_WIDGET_SESSIONS_DIR`` is set (tests), stays strictly inside
    the test hierarchy and never touches ``~/.claude``.
    """
    roots: list[Path] = []
    sibling = Path(sess_dir).parent / "projects"
    if sibling.is_dir():
        roots.append(sibling)

    override = os.environ.get(SESSIONS_DIR_ENV)
    if override:
        env_projects = Path(override).parent / "projects"
        if env_projects.is_dir() and env_projects not in roots:
            roots.append(env_projects)
        return roots

    try:
        from claude_swap.paths import get_claude_config_home

        default_projects = Path(get_claude_config_home()) / "projects"
    except Exception:
        default_projects = Path.home() / ".claude" / "projects"
    if default_projects.is_dir() and default_projects not in roots:
        roots.append(default_projects)
    return roots


def _locate_session_jsonl(
    session_id: str,
    cwd: str,
    roots: list[Path],
    subdir_cache: dict[Path, list[Path]],
) -> Path | None:
    """Locate ``<session_id>.jsonl`` in O(1) via *cwd* slug, else scan root subdirs."""
    sid = session_id.strip()
    if not sid or "/" in sid or "\\" in sid or ".." in sid:
        return None
    filename = f"{sid}.jsonl"

    if cwd:
        slugs = _candidate_project_slugs(cwd)
        for root in roots:
            for slug in slugs:
                cand = root / slug / filename
                if cand.is_file():
                    return cand

    for root in roots:
        direct = root / filename
        if direct.is_file():
            return direct

    for root in roots:
        subdirs = subdir_cache.get(root)
        if subdirs is None:
            subdirs = []
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        try:
                            if entry.is_dir():
                                subdirs.append(Path(entry.path))
                        except OSError:
                            continue
            except OSError:
                pass
            subdir_cache[root] = subdirs
        for subdir in subdirs:
            cand = subdir / filename
            if cand.is_file():
                return cand
    return None


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _has_assistant_content(msg: Mapping[str, Any]) -> bool:
    """True when an assistant ``message`` carries non-empty response content."""
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str) and item.strip():
                return True
            if isinstance(item, Mapping):
                itype = str(item.get("type") or "").strip().lower()
                if itype in ("tool_use", "thinking"):
                    return True
                if itype == "text" and str(item.get("text") or "").strip():
                    return True
    return False


def _match_interruption_signatures(line_bytes: bytes) -> tuple[bool, bool]:
    """Check one JSONL record's bytes for latch/rate-limit signatures.

    Returns ``(latched_credits, hit_rate_limit)``. Never stores, logs, or
    returns any transcript text.
    """
    norm = (
        line_bytes.decode("utf-8", errors="replace")
        .replace("\u2019", "'")
        .replace("\\u2019", "'")
        .replace("\\'", "'")
        .lower()
    )
    latched = any(sig in norm for sig in _LATCH_CREDIT_SIGNATURES)
    rate_limited = any(sig in norm for sig in _RATE_LIMIT_SIGNATURES)
    return latched, rate_limited


def _analyze_interruption_tail(raw_tail: bytes, *, truncated: bool) -> tuple[bool, bool]:
    """Scan bounded transcript tail bytes in chronological order.

    A later successful assistant response clears any earlier latch or rate-limit
    flag in the tail. User prompts and tool results are ignored to prevent false
    positives when editing code or discussing errors.
    """
    if not raw_tail:
        return False, False
    if truncated:
        nl = raw_tail.find(b"\n")
        if nl == -1:
            return False, False
        raw_tail = raw_tail[nl + 1 :]

    latched = False
    rate_limited = False
    for raw_line in raw_tail.splitlines():
        line_bytes = raw_line.strip()
        if not line_bytes:
            continue
        try:
            obj = json.loads(line_bytes.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(obj, Mapping):
            continue
        rec_type = str(obj.get("type") or "").strip().lower()
        if rec_type == "user":
            continue
        if rec_type == "assistant":
            msg = obj.get("message")
            msg_map = msg if isinstance(msg, Mapping) else {}
            model = str(msg_map.get("model") or "").strip().lower()
            usage = msg_map.get("usage")
            usage_map = usage if isinstance(usage, Mapping) else {}
            has_real_tokens = (
                (
                    _int_or_zero(usage_map.get("input_tokens")) > 0
                    or _int_or_zero(usage_map.get("output_tokens")) > 0
                    or _int_or_zero(usage_map.get("cache_read_input_tokens")) > 0
                    or _int_or_zero(usage_map.get("cache_creation_input_tokens")) > 0
                )
                and bool(model)
                and model != "<synthetic>"
            )
            is_err_obj = bool(
                obj.get("isApiErrorMessage")
                or obj.get("is_error")
                or obj.get("error")
                or msg_map.get("error")
                or model == "<synthetic>"
            )
            if not is_err_obj and has_real_tokens:
                latched = False
                rate_limited = False
                continue
            m_latch, m_rl = _match_interruption_signatures(line_bytes)
            if m_latch or m_rl:
                if m_latch:
                    latched = True
                if m_rl:
                    rate_limited = True
                continue
            if not is_err_obj and _has_assistant_content(msg_map):
                latched = False
                rate_limited = False
            continue

        subtype = str(obj.get("subtype") or "").strip().lower()
        is_explicit_err = bool(
            obj.get("isApiErrorMessage")
            or obj.get("is_error")
            or obj.get("error")
        )
        is_error_candidate = (
            rec_type in ("error", "api_error")
            or (
                rec_type == "system"
                and (subtype in ("", "api_error", "error", "rate_limit") or is_explicit_err)
            )
            or (rec_type == "result" and is_explicit_err)
            or (rec_type == "" and is_explicit_err)
        )
        if not is_error_candidate:
            continue
        m_latch, m_rl = _match_interruption_signatures(line_bytes)
        if m_latch:
            latched = True
        if m_rl:
            rate_limited = True
    return latched, rate_limited


def detect_session_interruptions(
    sessions_dir: Path,
    projects_roots: Iterable[Path] | None = None,
    *,
    max_age_s: float = 180.0,
    now: float | None = None,
) -> dict[int, dict[str, Any]]:
    """Detect usage-credits latches and hard rate-limit hits in live sessions.

    Reads ``sessionId`` from live interactive ``<pid>.json`` records under
    *sessions_dir*, locates each ``<sessionId>.jsonl`` across *projects_roots*,
    checks ``mtime`` against *max_age_s*, and tail-reads at most 16 KiB per
    qualifying file. Never reads whole transcripts and never logs or returns
    prompt or transcript text.

    Returns ``{pid: {"session_id": str, "latched_credits": bool,
    "hit_rate_limit": bool}}`` for sessions currently in an interrupted state.
    """
    sess_path = Path(sessions_dir)
    records, _unreadable = read_registry(sess_path)
    if not records:
        return {}

    if projects_roots is None:
        roots = _default_projects_roots_for(sess_path)
    else:
        roots = [Path(r) for r in projects_roots if Path(r).is_dir()]
    if not roots:
        return {}

    now_ts = time.time() if now is None else float(now)
    subdir_cache: dict[Path, list[Path]] = {}
    out: dict[int, dict[str, Any]] = {}

    for record in records:
        pid = int(record["pid"])
        sid = str(record.get("sessionId") or "").strip()
        if not sid:
            continue
        cwd = str(record.get("cwd") or "").strip()
        cand = _locate_session_jsonl(sid, cwd, roots, subdir_cache)
        if cand is None:
            continue
        try:
            st = cand.stat()
        except OSError:
            continue
        if max_age_s >= 0.0 and (now_ts - st.st_mtime) > max_age_s:
            continue
        try:
            with cand.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size <= 0:
                    continue
                start = max(0, size - _INTERRUPTION_TAIL_BYTES)
                fh.seek(start)
                raw_tail = fh.read(_INTERRUPTION_TAIL_BYTES)
        except OSError:
            continue
        latched, rate_limited = _analyze_interruption_tail(raw_tail, truncated=(start > 0))
        if latched or rate_limited:
            prev = out.get(pid)
            if prev is not None:
                out[pid] = {
                    "session_id": sid,
                    "latched_credits": bool(prev["latched_credits"] or latched),
                    "hit_rate_limit": bool(prev["hit_rate_limit"] or rate_limited),
                }
            else:
                out[pid] = {
                    "session_id": sid,
                    "latched_credits": latched,
                    "hit_rate_limit": rate_limited,
                }
            LOGGER.debug(
                "fleet: session interruption pid=%d latched_credits=%s hit_rate_limit=%s",
                pid,
                latched,
                rate_limited,
            )
    return out


def _row_from_record(
    record: Mapping[str, Any],
    *,
    slot: int | None,
    pinned: bool,
    interruption: Mapping[str, Any] | None = None,
) -> SessionRow:
    started = record.get("startedAt", 0)
    try:
        started_at = float(started) / 1000.0 if started else 0.0
    except (TypeError, ValueError):
        started_at = 0.0
    sid = str(record.get("sessionId", "") or "").strip()
    latched = False
    hit_rl = False
    if interruption:
        intr_sid = str(interruption.get("session_id") or "").strip()
        if not intr_sid or not sid or intr_sid == sid:
            latched = bool(interruption.get("latched_credits", False))
            hit_rl = bool(interruption.get("hit_rate_limit", False))
    return SessionRow(
        pid=int(record["pid"]),
        cwd=str(record.get("cwd", "") or ""),
        name=str(record.get("name", "") or ""),
        status=str(record.get("status") or ""),
        started_at=started_at,
        slot=slot,
        pinned=pinned,
        session_id=sid,
        latched_credits=latched,
        hit_rate_limit=hit_rl,
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

    default_dir = sessions_dir()
    records, unreadable = read_registry(default_dir)
    default_interruptions: dict[int, dict[str, Any]] = {}
    if records:
        try:
            default_interruptions = detect_session_interruptions(default_dir)
        except Exception:
            default_interruptions = {}
    rows.extend(
        _row_from_record(
            record,
            slot=active_slot,
            pinned=False,
            interruption=default_interruptions.get(int(record["pid"])),
        )
        for record in records
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
            prof_interruptions: dict[int, dict[str, Any]] = {}
            if profile_records:
                try:
                    prof_interruptions = detect_session_interruptions(directory)
                except Exception:
                    prof_interruptions = {}
            rows.extend(
                _row_from_record(
                    record,
                    slot=slot,
                    pinned=True,
                    interruption=prof_interruptions.get(int(record["pid"])),
                )
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
        owner_slot: int | None = row.slot if row.pinned else None
        org_shifted = False
        if not row.pinned and identities and row.cwd:
            try:
                owner_slot, org_shifted = detect_workspace_owner_slot(
                    row.cwd,
                    identities,
                    current_slot=row.slot,
                    session_id=row.session_id or None,
                    pid=row.pid,
                )
            except Exception:
                owner_slot, org_shifted = None, False
        if pending or own or owner_slot is not None or org_shifted:
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
                owner_slot=owner_slot,
                org_shifted=org_shifted,
                session_id=row.session_id,
                latched_credits=row.latched_credits,
                hit_rate_limit=row.hit_rate_limit,
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
