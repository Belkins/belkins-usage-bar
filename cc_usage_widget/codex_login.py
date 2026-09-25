"""Log in again / add an account for the live Codex rows (CX-5, CX-7b).

A dead Codex login used to be a row that said ``relogin`` and nothing else,
for five days, with the fix documented only in the README. This module makes
it one click:

1. The menu row (or ``Add Codex account…``) asks the worker for a login. The
   worker writes a small ``login-<ts>.command`` script under
   ``codex-accounts/`` (0700, paths only - no token, no email) and parks
   ``('terminal', path)`` for the AppKit thread, which opens it in Terminal.
2. The script runs ``codex login`` into a FRESH ``CODEX_HOME``
   (``codex-accounts/new-<ts>``) and then ``python -m
   cc_usage_widget.codex_login adopt --replace``.
3. :func:`adopt_pending` reads the new ``auth.json``'s own ``account_id`` and
   either swaps it into the existing ``<account_id>/auth.json`` (a relogin) or
   registers it the way ``codex_accounts adopt`` does (a new account). A
   closed Terminal is covered too: the worker calls the same function on its
   quota tick whenever a ``new-*/auth.json`` exists.

Why a fresh ``new-*`` home rather than logging straight into the account's
own: the browser flow shows a workspace picker, and two of the tracked
accounts share an email. Logging in "into" ``<id>/`` and picking the other
workspace would write the wrong account's token under this id - which
``CredentialStore.discover`` then refuses, leaving the row dead. Adopting by
the id the NEW token claims cannot make that mistake.

Lifecycle of the files this module creates:

* ``new-<ts>/`` - created by the ``.command`` (``mkdir -m 700``), consumed by
  :func:`adopt_pending` (the script's own call, or the worker tick), never
  persisted anywhere else. A login abandoned before ``auth.json`` exists
  leaves an empty dir holding no secret; nothing adopts it.
* ``login-<ts>.command`` - written by :func:`write_login_command`, deletes
  itself after a successful adopt, and any left behind (a failed or abandoned
  login) are pruned after :data:`COMMAND_MAX_AGE_SECONDS` by the next write.

``codex_accounts.py`` is imported read-only (``Registry``, ``RegistryEntry``,
``decode_jwt_claims`` and the note vocabulary); it is not edited by this lane.
This module never reads or writes ``~/.codex`` and makes no network request.

CLI::

    python -m cc_usage_widget.codex_login adopt [--replace]
        [--accounts-dir DIR] [--registry PATH]
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .codex_accounts import (
    NOTE_NO_CREDENTIAL,
    NOTE_RELOGIN,
    Registry,
    RegistryEntry,
    decode_jwt_claims,
)
from .contracts import CODEX_ACCOUNTS_DIR, CODEX_ACCOUNTS_REGISTRY_PATH

__all__ = [
    "ADD_ACCOUNT_LABEL",
    "ADOPT_SETTLE_SECONDS",
    "AdoptResult",
    "CODEX_BINARY_CANDIDATES",
    "LOGIN_AGAIN_LABEL",
    "account_id_for_slot",
    "accounts_dir_for",
    "adopt_pending",
    "has_pending",
    "main",
    "needs_login",
    "relogin_detail",
    "resolve_codex_binary",
    "write_login_command",
]

LOGIN_AGAIN_LABEL = "Log in again…"
"""The action a dead Codex row offers, and the tail of its detail line."""

ADD_ACCOUNT_LABEL = "Add Codex account…"
"""The ``Codex accounts ▸`` item that starts a login for an untracked account."""

PENDING_PREFIX = "new-"
COMMAND_PREFIX = "login-"
COMMAND_SUFFIX = ".command"

CODEX_BINARY_CANDIDATES: tuple[str, ...] = (
    "/usr/local/bin/codex",
    "/opt/homebrew/bin/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex",
)
"""Where ``codex`` lives when ``PATH`` does not say. The LaunchAgent runs with
launchd's minimal ``PATH`` (no ``/usr/local/bin``), so for the running widget
this list IS the lookup; the last entry is the binary the ChatGPT desktop app
ships."""

ADOPT_SETTLE_SECONDS = 30.0
"""How old a ``new-*/auth.json`` must be before the WORKER adopts it. The
script adopts the instant ``codex login`` returns; the worker is only the
fallback for a closed Terminal, and must not move a file ``codex login`` may
still be finishing."""

COMMAND_MAX_AGE_SECONDS = 86_400.0
"""A leftover ``login-*.command`` older than this is removed by the next write."""

_DIR_MODE = 0o700
_AUTH_MODE = 0o600
_COMMAND_MODE = 0o700

_SOURCE_CREDENTIAL_ATTRS = ("credentials", "_credentials")


# ---------------------------------------------------------------------------
# Finding things
# ---------------------------------------------------------------------------


def resolve_codex_binary(
    which: Callable[[str], str | None] = shutil.which,
    exists: Callable[[str], bool] = os.path.exists,
) -> str | None:
    """The ``codex`` executable to log in with, or ``None``.

    ``PATH`` first (an interactive install wins), then
    :data:`CODEX_BINARY_CANDIDATES` in order. ``None`` means "no Codex CLI on
    this machine" and the caller says so instead of writing a script that
    cannot run.
    """
    try:
        found = which("codex")
    except Exception:
        found = None
    if found:
        return str(found)
    for candidate in CODEX_BINARY_CANDIDATES:
        try:
            if exists(candidate):
                return candidate
        except Exception:
            continue
    return None


def accounts_dir_for(sources: Sequence[Any]) -> Path:
    """The credential directory the live source actually reads.

    Taken from the source's ``CredentialStore`` so a redirected install (or a
    test) adopts into the directory it polls; :data:`CODEX_ACCOUNTS_DIR`
    otherwise.
    """
    for source in sources or ():
        for name in _SOURCE_CREDENTIAL_ATTRS:
            store = getattr(source, name, None)
            directory = getattr(store, "accounts_dir", None)
            if directory:
                return Path(directory)
    return Path(CODEX_ACCOUNTS_DIR)


def _pending_dirs(accounts_dir: Path) -> list[Path]:
    """``new-*`` dirs holding an ``auth.json`` file, oldest name first."""
    try:
        children = sorted(accounts_dir.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for child in children:
        if not child.name.startswith(PENDING_PREFIX):
            continue
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            auth = child / "auth.json"
            if auth.is_symlink() or not auth.is_file():
                continue
        except OSError:
            continue
        out.append(child)
    return out


def has_pending(accounts_dir: os.PathLike[str] | str) -> bool:
    """Whether any ``new-*/auth.json`` waits to be adopted. One ``listdir``."""
    return bool(_pending_dirs(Path(accounts_dir)))


# ---------------------------------------------------------------------------
# Adopting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdoptResult:
    """What one :func:`adopt_pending` pass did. Holds ids and paths, never a
    token; ``lines`` are the human sentences the CLI prints (they may carry
    the email the token claims - that is what the user needs to check they
    picked the right workspace)."""

    added: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    waiting: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.refused

    def log_lines(self, *, include_refused: bool = True) -> tuple[str, ...]:
        """Widget-log lines: short ids and outcomes only, no email.

        The worker passes ``include_refused=False`` while the refusals are the
        same as last tick's, so an abandoned ``new-*`` dir is one log line, not
        one per poll."""
        out = [f"codex login: replaced the credential of {aid[:8]}" for aid in self.replaced]
        out += [f"codex login: adopted new account {aid[:8]}" for aid in self.added]
        if include_refused:
            out += [f"codex login: {reason}" for reason in self.refused]
        return tuple(out)


def _identity(auth: Path) -> tuple[str, dict[str, Any]] | None:
    """``(account_id, claims)`` from one ``auth.json``, or ``None``.

    Same rule as ``codex_accounts._cmd_adopt``: ``tokens.account_id``, else the
    access token's own ``chatgpt_account_id`` claim, decoded offline. The
    returned claims are the decoded summary (id, email, plan, exp) - never a
    token.
    """
    try:
        raw = json.loads(auth.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    tokens = raw.get("tokens") if isinstance(raw, Mapping) else None
    tokens = tokens if isinstance(tokens, Mapping) else {}
    access = tokens.get("access_token")
    claims = decode_jwt_claims(access if isinstance(access, str) else "")
    account_id = str(tokens.get("account_id") or "").strip() or str(claims.get("account_id") or "")
    if not account_id or "/" in account_id or account_id.startswith("."):
        return None
    return account_id, claims


def _describe_claims(account_id: str, claims: Mapping[str, Any]) -> str:
    exp = claims.get("exp")
    when = ""
    if exp:
        try:
            moment = dt.datetime.fromtimestamp(float(exp))
            when = f"{moment:%b} {moment.day} {moment:%H:%M}"
        except (OSError, OverflowError, TypeError, ValueError):
            when = ""
    return (
        f"{account_id} · {claims.get('email') or '(no email in token)'} · "
        f"{claims.get('plan_type') or '(no plan in token)'} · expires {when or 'unknown'}"
    )


def adopt_pending(
    accounts_dir: os.PathLike[str] | str,
    registry: Registry,
    *,
    replace: bool,
    min_age_seconds: float = 0.0,
    clock: Callable[[], float] = time.time,
) -> AdoptResult:
    """Adopt every ``new-*/auth.json`` under *accounts_dir*. Never raises.

    For each pending login, by the id its own token claims:

    * **untracked** (no registry entry, no ``<id>/`` dir): renamed to
      ``<id>/`` at 0700/0600 and registered, exactly as ``codex_accounts
      adopt`` does;
    * **tracked, ``replace``**: its ``auth.json`` is chmodded 0600 and
      ``os.replace``d over ``<id>/auth.json`` (atomic: the poller sees the old
      file or the new one, never half of either), then the ``new-*`` dir is
      removed. The registry is untouched, so the alias and the enabled flag
      survive. The file's ``(mtime, size)`` moves, which is what the live
      source's credential signature watches: the ``relogin`` verdict clears
      on its next ``run_cycle_once``;
    * **tracked, not ``replace``**: refused with both paths, as ``codex_accounts
      adopt`` refuses - nothing is moved.

    ``min_age_seconds`` leaves an ``auth.json`` younger than that where it is
    (reported as ``waiting``): the worker passes :data:`ADOPT_SETTLE_SECONDS`
    so it never moves a file ``codex login`` may still be writing.

    A second process racing this one (the script and the worker tick) finds
    the file gone and skips it; that is not an error.
    """
    root = Path(accounts_dir)
    added: list[str] = []
    replaced: list[str] = []
    refused: list[str] = []
    waiting: list[str] = []
    lines: list[str] = []
    now = float(clock())
    try:
        tracked = {entry.account_id for entry in registry.entries()}
    except Exception:
        tracked = set()
    order = len(tracked)
    for source in _pending_dirs(root):
        auth = source / "auth.json"
        try:
            age = now - auth.stat().st_mtime
        except OSError:
            continue  # adopted by the other process a moment ago
        if min_age_seconds > 0 and age < min_age_seconds:
            waiting.append(source.name)
            continue
        identity = _identity(auth)
        if identity is None:
            refused.append(f"{source.name}: no readable account id in auth.json")
            continue
        account_id, claims = identity
        target = root / account_id
        known = account_id in tracked or target.is_dir()
        try:
            if known:
                if not replace:
                    refused.append(
                        f"refusing to adopt {source.name}: account {account_id[:8]} is already "
                        f"at {target} (use adopt --replace to swap the fresh login in)"
                    )
                    continue
                target.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
                os.chmod(target, _DIR_MODE)
                os.chmod(auth, _AUTH_MODE)
                os.replace(auth, target / "auth.json")
                shutil.rmtree(source, ignore_errors=True)
                if account_id not in tracked:
                    registry.upsert(
                        RegistryEntry(account_id=account_id, alias="", enabled=True, order=order)
                    )
                    order += 1
                    tracked.add(account_id)
                replaced.append(account_id)
                lines.append(f"replaced {_describe_claims(account_id, claims)}")
                continue
            source.rename(target)
            os.chmod(target, _DIR_MODE)
            os.chmod(target / "auth.json", _AUTH_MODE)
        except FileNotFoundError:
            continue  # the other process got there first
        except OSError as exc:
            refused.append(f"{source.name}: could not adopt ({type(exc).__name__})")
            continue
        registry.upsert(RegistryEntry(account_id=account_id, alias="", enabled=True, order=order))
        order += 1
        tracked.add(account_id)
        added.append(account_id)
        lines.append(f"adopted {_describe_claims(account_id, claims)}")
    return AdoptResult(
        added=tuple(added),
        replaced=tuple(replaced),
        refused=tuple(refused),
        waiting=tuple(waiting),
        lines=tuple(lines),
    )


# ---------------------------------------------------------------------------
# The Terminal script
# ---------------------------------------------------------------------------


def _package_root() -> Path:
    """The directory ``-m cc_usage_widget`` must run from (the checkout)."""
    return Path(__file__).resolve().parents[1]


def _prune_old_commands(accounts_dir: Path, now: float) -> None:
    try:
        children = list(accounts_dir.iterdir())
    except OSError:
        return
    for child in children:
        if not (child.name.startswith(COMMAND_PREFIX) and child.name.endswith(COMMAND_SUFFIX)):
            continue
        try:
            if now - child.stat().st_mtime > COMMAND_MAX_AGE_SECONDS:
                child.unlink()
        except OSError:
            continue


def write_login_command(
    accounts_dir: os.PathLike[str] | str,
    python: str,
    codex: str,
    account_id: str | None,
    *,
    registry_path: os.PathLike[str] | str | None = None,
    clock: Callable[[], float] = time.time,
) -> Path:
    """Write the 0700 ``login-<ts>.command`` Terminal runs. Returns its path.

    The script holds paths and nothing else - no token, no email, no alias: a
    file that Finder can open is not a place for an identity. It logs in into
    ``new-<ts>`` and hands the result to :func:`adopt_pending` via the CLI
    with ``--replace`` (for a NEW account ``--replace`` changes nothing; for a
    relogin it is the whole point). *account_id* only chooses the first line
    it prints (the id's first 8 characters, which ``codex_accounts adopt``
    already prints), so the user knows which workspace to pick.
    """
    root = Path(accounts_dir)
    root.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
    now = float(clock())
    _prune_old_commands(root, now)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    suffix = ""
    serial = 1
    while (root / f"{COMMAND_PREFIX}{stamp}{suffix}{COMMAND_SUFFIX}").exists() or (
        root / f"{PENDING_PREFIX}{stamp}{suffix}"
    ).exists():
        serial += 1
        suffix = f"-{serial}"
    home = root / f"{PENDING_PREFIX}{stamp}{suffix}"
    script = root / f"{COMMAND_PREFIX}{stamp}{suffix}{COMMAND_SUFFIX}"
    registry = Path(registry_path) if registry_path is not None else Path(CODEX_ACCOUNTS_REGISTRY_PATH)
    q = shlex.quote
    if account_id:
        who = f"Codex login for account {account_id[:8]}: pick THAT workspace in the browser."
    else:
        who = "Codex login for a new account: pick its workspace in the browser."
    body = "\n".join(
        [
            "#!/bin/bash",
            "# Belkins Usage Bar - Codex login. Written by the widget; holds paths only.",
            f"echo {q(who)}",
            f"cd {q(str(_package_root()))} || exit 1",
            f"mkdir -p -m 700 {q(str(home))} || exit 1",
            f"CODEX_HOME={q(str(home))} {q(codex)} login"
            f" && {q(python)} -m cc_usage_widget.codex_login adopt --replace"
            f" --accounts-dir {q(str(root))} --registry {q(str(registry))}"
            ' && rm -f -- "$0" && echo "Done - the Usage Bar picks it up on its next poll."',
            "",
        ]
    )
    fd = os.open(str(script), os.O_WRONLY | os.O_CREAT | os.O_EXCL, _COMMAND_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(script, _COMMAND_MODE)
    return script


# ---------------------------------------------------------------------------
# Menu helpers (pure)
# ---------------------------------------------------------------------------


def needs_login(row: Any) -> bool:
    """Whether a quota row's verdict is fixed by logging in again (by NOTE
    identity - the two constants ``codex_accounts`` sets verbatim)."""
    return (getattr(row, "attention_note", "") or "") in (NOTE_RELOGIN, NOTE_NO_CREDENTIAL)


def account_id_for_slot(
    accounts: Sequence[tuple[str, str, bool]], slot: int
) -> str | None:
    """The account id behind a live Codex row's negative *slot*.

    ``quota_rows`` numbers the ENABLED registry entries in registry order as
    ``-1, -2, …``; *accounts* is the worker's cached ``(account_id, alias,
    enabled)`` tuple in that same order, so the mapping is the same rule run
    backwards. ``None`` for a non-live slot or one out of range.
    """
    if slot >= 0:
        return None
    enabled = [account_id for account_id, _alias, flag in accounts if flag]
    index = -slot - 1
    return enabled[index] if index < len(enabled) else None


def _age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def relogin_detail(row: Any, *, now: float | None = None) -> str:
    """The one dim line under a dead Codex row (CX-7b), or ``""``.

    ``token expired Sep 20 11:13 · Log in again…`` from
    ``AccountRow.credential_expires_at`` (lane codex-credentials; read with
    ``getattr``). With the expiry still ahead, the ``relogin`` verdict came
    from an HTTP 401, so the line says ``token refused``. Without an expiry it
    falls back to the reading's age, ``last reading 5d ago``. A
    ``credential unreadable`` row gets the action alone - its note already
    says what is wrong. Every other row: ``""``, so the menu is byte-for-byte
    unchanged. ``attention_note`` itself is never altered.
    """
    note = getattr(row, "attention_note", "") or ""
    if note == NOTE_NO_CREDENTIAL:
        return LOGIN_AGAIN_LABEL
    if note != NOTE_RELOGIN:
        return ""
    moment = time.time() if now is None else float(now)
    expires = getattr(row, "credential_expires_at", None)
    head = ""
    if expires is not None:
        try:
            when = dt.datetime.fromtimestamp(float(expires))
            stamp = f"{when:%b} {when.day} {when:%H:%M}"
            head = f"token expired {stamp}" if float(expires) <= moment else "token refused"
        except (OSError, OverflowError, TypeError, ValueError):
            head = ""
    if not head:
        age = getattr(row, "usage_age_seconds", None)
        if age is not None:
            head = f"last reading {_age(age)} ago"
    return f"{head} · {LOGIN_AGAIN_LABEL}" if head else LOGIN_AGAIN_LABEL


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = """usage: python -m cc_usage_widget.codex_login adopt [--replace]
                                    [--accounts-dir DIR] [--registry PATH]

  adopt       adopt every codex-accounts/new-*/auth.json. A NEW account id is
              renamed into place and registered (as `codex_accounts adopt`).
              An id that is already tracked is refused - unless --replace, in
              which case the fresh login's auth.json replaces the old one and
              the new-* dir is removed. Offline: the id comes from the token's
              own claims. Prints no token.
"""


def _flag_value(args: list[str], name: str) -> str | None:
    if name in args:
        index = args.index(name)
        if index + 1 < len(args):
            return args[index + 1]
    return None


def main(
    argv: Sequence[str],
    *,
    accounts_dir: os.PathLike[str] | str | None = None,
    registry_path: os.PathLike[str] | str | None = None,
    out: Any = None,
) -> int:
    """``adopt [--replace]``. Keyword arguments are test seams only."""
    write = (out or sys.stdout).write
    args = list(argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        write(_USAGE)
        return 0 if args else 2
    if args[0] != "adopt":
        write(_USAGE)
        return 2
    rest = args[1:]
    directory = Path(
        accounts_dir
        if accounts_dir is not None
        else (_flag_value(rest, "--accounts-dir") or CODEX_ACCOUNTS_DIR)
    )
    registry = Registry(
        registry_path
        if registry_path is not None
        else (_flag_value(rest, "--registry") or CODEX_ACCOUNTS_REGISTRY_PATH)
    )
    if not directory.is_dir():
        write(f"no credential dir at {directory}\n")
        return 1
    result = adopt_pending(directory, registry, replace="--replace" in rest)
    for line in result.lines:
        write(line + "\n")
    for line in result.refused:
        write(line + "\n")
    if not (result.added or result.replaced or result.refused):
        write(f"nothing to adopt in {directory} (expected new-*/auth.json)\n")
    elif result.added:
        write(f"set aliases in {registry.path}\n")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main(sys.argv[1:]))
