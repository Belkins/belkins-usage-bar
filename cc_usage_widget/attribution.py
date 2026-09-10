"""Per-project and per-session cost attribution (roadmap item 6).

The daily rollup answers *what did today cost*. This module answers the
question Vlad actually asks - *what did that swarm cost* - by carrying two
extra dimensions beside the day: which **project** a transcript belonged to,
and which **session** wrote it.

Why a separate store rather than a wider ``rollups.json``
--------------------------------------------------------

``DailyRollupStore`` is the money. It is read by the title, the cost section,
the self-audit and the history mirror, and its on-disk shape is load-bearing in
four test modules. Widening its key would put a *display convenience* inside
the one file whose correctness the whole widget rests on. So attribution lives
in its own file (``attribution.json``, beside ``rollups.json``), is a pure
cache like the rollups are, and can be deleted without costing a dollar figure.

The ledger discipline is NOT re-invented here
---------------------------------------------

Roadmap item 1 made the rollup store reversible: every scanned file records
what it contributed per ``(day, model)`` in its
:attr:`~cc_usage_widget.contracts.FileScanState.ledger`, and a re-read from
byte 0 retracts that contribution before adding the new one. Attribution rides
on **exactly the same** events, because a transcript belongs to exactly one
project and one session for its whole life: the scope is a property of the
*file*, not of the individual records inside it.

So the indexer hands this module the same two things it hands the rollup store
- the counters a file just contributed, and (on a reset) the ledger it is about
to replace - tagged with that file's scope. A rebuild therefore produces
identical project totals, and a replaced-inode transcript is not counted twice,
for the same reason the day totals are not: the addition is reversible.

Privacy
-------

The widget's promise is that it reads token counts and model names out of files
full of somebody's source code and credentials, and writes nothing else. This
feature adds exactly two strings to that list, and they are named here so the
promise stays checkable (``tests/test_attribution.py`` asserts it with a
canary):

* the **basename** of the working directory a session ran in - one path
  component, never the full path, never the parent directories;
* the **session id** - a uuid Claude Code or Codex generated.

Never the prompt, the completion, a tool result, a git branch, a file name
inside the project, or any other component of the path.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Final

from .contracts import (
    COUNTER_FIELDS,
    DayKey,
    DayRollup,
    LedgerEntry,
    ModelKey,
    ModelUsage,
    PricingTable,
    Usd,
    Vendor,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    WIDGET_HOME,
    parse_day_key,
)

__all__ = [
    "ATTRIBUTION_PATH",
    "Attribution",
    "AttributionCollector",
    "AttributionPass",
    "AttributionRow",
    "AttributionStore",
    "SESSION_KEEP_DAYS",
    "UNKNOWN_PROJECT",
    "attribution_path_for",
    "claude_scope",
    "codex_scope",
    "session_label",
]


ATTRIBUTION_PATH: Final[Path] = WIDGET_HOME / "attribution.json"
"""Per-project / per-session token counts. A pure cache, like ``rollups.json``:
deleting it costs the two menu blocks until the next full re-index, and nothing
else. 0600 is inherited from the widget home."""

SESSION_KEEP_DAYS: Final[int] = 2
"""How many days of *session* rows survive a prune - today and yesterday.

A project count is bounded by how many directories a person works in; a session
count is not (a swarm day is hundreds of transcripts). The menu only ever shows
today's five, so anything older is weight without a reader.
"""

UNKNOWN_PROJECT: Final[str] = "(unknown)"
"""Label for a transcript whose working directory could not be read.

Deliberately not a guess. A Codex rollout with no ``session_meta`` line, or a
Claude transcript whose records carry no ``cwd``, gets a row that says so
rather than being dropped (which would make the block quietly under-count) or
attributed to a neighbouring project (which would be a fabricated number).
"""

_SEP: Final[str] = "\x1f"
"""ASCII unit separator - the key join. Not a character any basename or uuid
contains, and legal inside a JSON string, so ``vendor|project|session`` round
trips without an escaping scheme."""

_MAX_LABEL: Final[int] = 64
"""Cap on a stored project or session string. A basename is short; a pathological
one must not be allowed to grow the cache file without bound."""

_SESSION_PREFIX: Final[int] = 8
"""Characters of the session id shown in the menu (SPEC 4.2 keeps rows narrow)."""

_MAX_META_BYTES: Final[int] = 262_144
"""How far into a Codex rollout to look for its ``session_meta`` line.

The record is always the first line, but ``base_instructions`` can make it
large; a bounded read means a corrupt file cannot turn scope resolution into an
unbounded allocation on the worker thread.
"""

_VERSION: Final[int] = 1


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def _clean(value: object) -> str:
    """One display-safe path component or id: no separators, bounded length."""
    text = str(value or "").strip()
    for bad in (_SEP, "\n", "\r", "\t"):
        text = text.replace(bad, " ")
    return text[:_MAX_LABEL]


@dataclass(frozen=True, slots=True)
class Attribution:
    """Which project and session a transcript's usage belongs to.

    ``session`` is the **parent** session for a subagent transcript: a swarm's
    cost is the swarm's, not one row per agent (roadmap item 6). ``project`` is
    the basename of the working directory, which is the name a person would
    use for it.
    """

    vendor: Vendor
    project: str
    session: str

    @property
    def project_key(self) -> str:
        """Storage key for the project dimension."""
        return f"{self.vendor}{_SEP}{self.project}"

    @property
    def session_key(self) -> str:
        """Storage key for the session dimension."""
        return f"{self.vendor}{_SEP}{self.project}{_SEP}{self.session}"


def _split_key(key: str) -> tuple[Vendor, str, str]:
    """Inverse of :attr:`Attribution.project_key` / ``session_key``."""
    parts = str(key).split(_SEP)
    vendor = parts[0] if parts else VENDOR_CLAUDE
    project = parts[1] if len(parts) > 1 else UNKNOWN_PROJECT
    session = parts[2] if len(parts) > 2 else ""
    return vendor, project, session  # type: ignore[return-value]


def session_label(session: str) -> str:
    """``"5909f788-7a20-…"`` -> ``"5909f788"`` - the menu's spelling."""
    return _clean(session)[:_SESSION_PREFIX]


def claude_scope(path: str | os.PathLike[str], cwd: str | None = None) -> Attribution:
    """Scope of one Claude transcript, from its path (and its ``cwd`` if seen).

    Two layouts exist under ``~/.claude/projects``::

        <project-dir>/<session-uuid>.jsonl
        <project-dir>/<session-uuid>/subagents/<agent>.jsonl

    so a subagent's parent session is simply the directory two levels above it -
    which is what folds a swarm's agents into the session that spawned them.

    The project **name** prefers the ``cwd`` the records themselves carry,
    because ``<project-dir>`` is the cwd with every ``/`` *and* every space
    flattened to ``-``: ``-Users-x-Desktop-AI-Products--Claude-Eval-Kit`` cannot
    be decoded back to ``Claude-Eval-Kit`` without guessing which hyphens were
    separators. When no record carried a ``cwd`` the encoded directory name is
    used **verbatim** - ugly, and true, which is the right trade for a label
    (rules: never invent a name).
    """
    p = Path(path)
    parts = p.parts
    session = p.stem
    project_dir = p.parent.name
    if len(parts) >= 3 and parts[-2] == "subagents":
        session = parts[-3]
        project_dir = parts[-4] if len(parts) >= 4 else project_dir
    name = Path(cwd).name if cwd else ""
    return Attribution(
        vendor=VENDOR_CLAUDE,
        project=_clean(name or project_dir or UNKNOWN_PROJECT) or UNKNOWN_PROJECT,
        session=_clean(session),
    )


def _read_session_meta(path: str | os.PathLike[str]) -> Mapping[str, Any] | None:
    """The ``session_meta`` payload of a Codex rollout, or ``None``.

    It is the first line of the file, so this is one bounded read, not a scan.
    Every failure - unreadable file, truncated line, a first record of some
    other type - answers ``None`` and the caller falls back to the path.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_META_BYTES)
    except OSError:
        return None
    line, sep, _rest = raw.partition(b"\n")
    if not sep and len(raw) >= _MAX_META_BYTES:
        return None  # a first line longer than the cap: not worth chasing
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else None


def codex_scope(
    path: str | os.PathLike[str],
    cwd: str | None = None,
    *,
    meta: Mapping[str, Any] | None = None,
) -> Attribution:
    """Scope of one Codex rollout, from its ``session_meta`` record.

    ``payload.cwd`` gives the project; ``payload.id`` gives the thread. A
    subagent rollout carries ``source.subagent.thread_spawn.parent_thread_id``
    and is folded into that parent, exactly as a Claude ``subagents/`` file is.

    *meta* is injectable so a test can state the record instead of writing a
    file; *cwd* is an override for the same reason.
    """
    payload = meta if meta is not None else _read_session_meta(path)
    session = ""
    directory = cwd or ""
    if payload is not None:
        if not directory:
            raw_cwd = payload.get("cwd")
            directory = raw_cwd if isinstance(raw_cwd, str) else ""
        parent = _codex_parent_thread(payload)
        for candidate in (parent, payload.get("id"), payload.get("session_id")):
            if isinstance(candidate, str) and candidate:
                session = candidate
                break
    if not session:
        # `rollout-2026-08-24T16-06-26-<uuid>` - the file's own identity is the
        # honest fallback, and it is stable across passes.
        session = Path(path).stem
    name = Path(directory).name if directory else ""
    return Attribution(
        vendor=VENDOR_CODEX,
        project=_clean(name) or UNKNOWN_PROJECT,
        session=_clean(session),
    )


def _codex_parent_thread(payload: Mapping[str, Any]) -> str | None:
    """``source.subagent.thread_spawn.parent_thread_id``, defensively."""
    node: Any = payload.get("source")
    for key in ("subagent", "thread_spawn", "parent_thread_id"):
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node if isinstance(node, str) and node else None


# ---------------------------------------------------------------------------
# What one scan pass attributed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttributionPass:
    """One scan's attributed additions and retractions.

    The twin of :attr:`~cc_usage_widget.contracts.ScanResult.deltas` /
    ``retractions``, and applied under the same rule: **retract first, then
    add**, in the same tick. Kept off ``ScanResult`` on purpose - that shape is
    shared contract between two indexers, four test modules and the audit, and
    a display feature has no business widening it. The owner pulls this from
    the scanner instead (:meth:`AttributionCollector.take`).
    """

    adds: tuple[tuple[Attribution, DayRollup], ...] = ()
    retractions: tuple[tuple[Attribution, DayRollup], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.adds or self.retractions)


def _fold(
    target: dict[Attribution, dict[DayKey, dict[ModelKey, list[int]]]],
    scope: Attribution,
    counters: Mapping[DayKey, Mapping[ModelKey, Sequence[int]]],
) -> None:
    bucket = target.setdefault(scope, {})
    for day, models in counters.items():
        day_bucket = bucket.setdefault(str(day), {})
        for model, values in models.items():
            row = day_bucket.get(str(model))
            if row is None:
                day_bucket[str(model)] = [int(v) for v in values]
                continue
            for i, value in enumerate(values):
                row[i] += int(value)


def _rollups(
    source: Mapping[Attribution, Mapping[DayKey, Mapping[ModelKey, Sequence[int]]]],
) -> tuple[tuple[Attribution, DayRollup], ...]:
    out: list[tuple[Attribution, DayRollup]] = []
    for scope in source:
        for day in sorted(source[scope]):
            models = {
                str(model): ModelUsage.from_counters(tuple(values))
                for model, values in source[scope][day].items()
                if any(values)
            }
            if models:
                out.append((scope, DayRollup(day=day, models=models)))
    return tuple(out)


class AttributionCollector:
    """Accumulates one scanner's attributed contributions for a pass.

    Lives on the indexer, is fed from the same two decision points that feed
    the rollup ledger, and is drained by the owner with :meth:`take`. Holding
    it *outside* ``ScanResult`` is what keeps the indexer diff to a handful of
    lines and leaves the shared contract shape alone.

    The resolved scope of a path is remembered for the life of the collector so
    a retraction can be attributed even when the replacement read yields no
    ``cwd`` of its own - the old file's money must come back out of the project
    it went into.
    """

    __slots__ = ("_resolver", "_scopes", "_adds", "_retractions")

    def __init__(self, resolver: Callable[[str, str | None], Attribution]) -> None:
        self._resolver = resolver
        self._scopes: dict[str, Attribution] = {}
        self._adds: dict[Attribution, dict[DayKey, dict[ModelKey, list[int]]]] = {}
        self._retractions: dict[Attribution, dict[DayKey, dict[ModelKey, list[int]]]] = {}

    def scope_for(self, path: str, hint: str | None = None) -> Attribution:
        """The scope of *path*, resolved once and cached.

        **Stable for the life of the collector**, and deliberately so: a
        retraction has to go back to the project the original contribution went
        into. Re-resolving on every pass would let a path whose ``cwd`` changed
        (a moved or renamed directory) take its money out of one project and
        put it into another, leaving the first clamped at zero and the second
        inflated - the exact failure mode roadmap item 1 exists to prevent.

        The one case that re-resolves is a scope that came back
        :data:`UNKNOWN_PROJECT` and a later pass arriving with a *hint*: there
        is nothing to be wrong about yet, and a row that can be named should
        be.
        """
        known = self._scopes.get(path)
        if known is not None and (hint is None or known.project != UNKNOWN_PROJECT):
            return known
        scope = self._resolver(path, hint)
        self._scopes[path] = scope
        return scope

    def remember(self, path: str, key: str) -> Attribution | None:
        """Seed the scope of *path* from a PERSISTED key. Returns it, or ``None``.

        The restart half of :meth:`scope_for`'s stability rule (roadmap item 6,
        2026-09-10). The in-memory cache dies with the process, so after a
        relaunch the first thing a replaced transcript does is retract its old
        ledger - and with nothing remembered, the scope of that retraction is
        resolved from the file that is on disk NOW. A session file rewritten in
        a different working directory therefore took its money out of the wrong
        project and left the right one clamped at zero, which is precisely the
        failure the cache exists to prevent - it just needed to survive a
        restart, so ``FileScanState.scope`` carries it.

        Never overwrites a scope this process has already resolved: the live
        value is at least as new as the persisted one.
        """
        text = str(key or "")
        if not text or path in self._scopes:
            return self._scopes.get(path)
        vendor, project, session = _split_key(text)
        scope = Attribution(
            vendor=vendor, project=project or UNKNOWN_PROJECT, session=session
        )
        self._scopes[path] = scope
        return scope

    def forget_path(self, path: str) -> None:
        """Drop the cached scope of one path, for a file the pass found GONE.

        What bounds the cache: without it every transcript this process ever
        saw keeps an entry for the life of the widget, including the ones
        Claude Code pruned weeks ago. A path that comes BACK re-resolves - and
        if it is a returning file with a ledger to retract, its tombstone
        carries the persisted scope, so :meth:`remember` puts the old value
        back before the retraction is attributed.
        """
        self._scopes.pop(str(path), None)

    def add(
        self,
        path: str,
        counters: Mapping[DayKey, Mapping[ModelKey, Sequence[int]]],
        *,
        hint: str | None = None,
    ) -> None:
        """Attribute what *path* just contributed."""
        if not counters:
            return
        _fold(self._adds, self.scope_for(path, hint), counters)

    def retract(
        self,
        path: str,
        entries: Sequence[LedgerEntry],
        *,
        hint: str | None = None,
    ) -> None:
        """Take back what *path* contributed before it was re-read from byte 0."""
        if not entries:
            return
        counters: dict[DayKey, dict[ModelKey, list[int]]] = {}
        for entry in entries:
            counters.setdefault(entry.day, {})[entry.model] = list(entry.counters)
        _fold(self._retractions, self.scope_for(path, hint), counters)

    def forget(self) -> None:
        """Drop everything - the scanner was reset and will re-read from zero."""
        self._scopes.clear()
        self._adds.clear()
        self._retractions.clear()

    def take(self) -> AttributionPass:
        """Drain the pass. Called exactly once per scan by the owner."""
        result = AttributionPass(
            adds=_rollups(self._adds), retractions=_rollups(self._retractions)
        )
        self._adds = {}
        self._retractions = {}
        return result


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttributionRow:
    """One rendered project or session row.

    ``unpriced_tokens`` exists for the same reason :class:`WindowCost` has it
    (roadmap item 3): a project that ran only ``codex-auto-review`` costs ``$0``
    and that is a true number about an untrue impression, so the row says how
    many tokens are behind it.
    """

    vendor: Vendor
    project: str
    session: str
    usage: ModelUsage
    usd: Usd
    unpriced_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.usage.total_tokens

    @property
    def label(self) -> str:
        """``"cc-usage-widget"`` or ``"5909f788 · cc-usage-widget"``."""
        if not self.session:
            return self.project
        return f"{session_label(self.session)} · {self.project}"


def _quantise(total: Decimal) -> Usd:
    """Cents, once, at the publish boundary - the rollup module's rule."""
    return float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def attribution_path_for(rollups_path: Path | str | None) -> Path:
    """Where the attribution cache for a given rollup store lives.

    **Beside the aggregate it decomposes**, for the reason ``history.py``
    documents: a test (or a second widget home, or a ``--rollups`` override)
    that redirects the cache into a temporary directory must not leave this
    file appending fixture projects to the operator's real one.
    """
    if rollups_path is None:
        return ATTRIBUTION_PATH
    return Path(rollups_path).parent / ATTRIBUTION_PATH.name


class AttributionStore:
    """``{day: {scope: {model: counters}}}`` for two scopes, on disk as JSON.

    Same shape and same discipline as :class:`~cc_usage_widget.rollup.DailyRollupStore`
    - mutable counter lists inside, frozen contract shapes only at the publish
    boundary, retraction clamped at zero and never silent - with one extra rule:
    the session dimension is pruned to :data:`SESSION_KEEP_DAYS`, because the
    menu only ever reads today's and the count is unbounded on a swarm day.
    """

    def __init__(
        self,
        *,
        path: Path | str | None = None,
        keep_days: int = 30,
        session_keep_days: int = SESSION_KEEP_DAYS,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._path: Path = Path(path) if path is not None else ATTRIBUTION_PATH
        self._keep_days = max(1, int(keep_days))
        self._session_keep_days = max(1, int(session_keep_days))
        self._projects: dict[DayKey, dict[str, dict[ModelKey, list[int]]]] = {}
        self._sessions: dict[DayKey, dict[str, dict[ModelKey, list[int]]]] = {}
        self._lock = threading.RLock()
        self._dirty = False
        self._logger = logger

    # -- introspection ----------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def keep_days(self) -> int:
        return self._keep_days

    def set_keep_days(self, keep_days: int) -> None:
        with self._lock:
            self._keep_days = max(1, int(keep_days))

    def __len__(self) -> int:
        with self._lock:
            return len(self._projects)

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        """Read the cache; a missing or corrupt file means "start empty"."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            parsed: object = None
        else:
            try:
                parsed = json.loads(raw)
            except (ValueError, RecursionError):
                parsed = None
        projects = _days_from_json(parsed, "projects")
        sessions = _days_from_json(parsed, "sessions")
        with self._lock:
            self._projects = projects
            self._sessions = sessions
            self._dirty = False

    def save(self, *, force: bool = False) -> None:
        """Persist atomically, 0600. A clean store with a file on disk is skipped."""
        with self._lock:
            if not (force or self._dirty or not self._path.exists()):
                return
            payload = {
                "version": _VERSION,
                "projects": _days_to_json(self._projects),
                "sessions": _days_to_json(self._sessions),
            }
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_name(self._path.name + ".tmp")
                tmp.write_text(
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    encoding="utf-8",
                )
                os.chmod(tmp, 0o600)
                os.replace(tmp, self._path)
            except OSError as exc:
                self._log(f"attribution: save failed: {exc}")
                return
            self._dirty = False

    # -- mutation ---------------------------------------------------------

    def apply(self, result: AttributionPass) -> int:
        """Retract, then add. Returns the number of cells clamped at zero.

        The order is the whole point and matches ``_run_cost_job``'s rule for
        the rollup store: adding first and subtracting after would take the
        re-added contribution straight back out again.
        """
        if not isinstance(result, AttributionPass):
            raise TypeError(f"apply expects AttributionPass, got {type(result)!r}")
        clamped = self.retract(result.retractions)
        self.merge(result.adds)
        return clamped

    def merge(self, entries: Iterable[tuple[Attribution, DayRollup]]) -> None:
        """Add attributed usage, field-wise, into both dimensions."""
        with self._lock:
            for scope, rollup in entries:
                parse_day_key(rollup.day)
                for model, usage in rollup.models.items():
                    if not isinstance(usage, ModelUsage):
                        raise TypeError(
                            f"merge expects ModelUsage, got {type(usage)!r}"
                        )
                    self._add_locked(
                        self._projects, rollup.day, scope.project_key, str(model), usage
                    )
                    self._add_locked(
                        self._sessions, rollup.day, scope.session_key, str(model), usage
                    )
                    self._dirty = True

    def retract(self, entries: Iterable[tuple[Attribution, DayRollup]]) -> int:
        """Subtract attributed usage, clamped at zero. Returns clamped cells.

        Clamping is the same honest compromise the rollup store makes: a ledger
        can describe more than this cache holds (it was deleted, or the feature
        was switched on after the day started), and a negative token count is
        not a state any row can render.
        """
        clamped = 0
        with self._lock:
            for scope, rollup in entries:
                parse_day_key(rollup.day)
                for model, usage in rollup.models.items():
                    for target, key in (
                        (self._projects, scope.project_key),
                        (self._sessions, scope.session_key),
                    ):
                        if self._retract_locked(target, rollup.day, key, str(model), usage):
                            clamped += 1
                    self._dirty = True
        if clamped:
            self._log(f"attribution: retraction clamped {clamped} cell(s) at zero")
        return clamped

    def prune(self, *, today: DayKey, keep_days: int | None = None) -> None:
        """Drop project days outside the window and session days beyond two.

        The window rule is deliberately the rollup store's, symmetric bound
        included: a future-dated bucket (a corrected clock, a transcript synced
        from a further-east host) is carried until it ages out rather than
        silently deleted, so a project total and the day total it decomposes
        cannot disagree about which days exist.
        """
        parse_day_key(today)
        window = self._keep_days if keep_days is None else max(1, int(keep_days))
        with self._lock:
            self._keep_days = window
            before = (
                sum(len(rows) for rows in self._projects.values()),
                sum(len(rows) for rows in self._sessions.values()),
            )
            self._projects = _within(self._projects, today, window)
            self._sessions = _within(self._sessions, today, self._session_keep_days)
            after = (
                sum(len(rows) for rows in self._projects.values()),
                sum(len(rows) for rows in self._sessions.values()),
            )
            if before != after:
                self._dirty = True

    def clear(self) -> None:
        """Empty the store - what a ``Rebuild cost index`` does to the rollups."""
        with self._lock:
            self._projects = {}
            self._sessions = {}
            self._dirty = True

    def drop_day_for_vendor(self, day: DayKey, vendor: Vendor) -> int:
        """Forget one vendor's rows for one day, both dimensions. Returns cells.

        The attribution half of an audit repair (roadmap item 2, 2026-09-10).
        The audit rebuilds a ``(day, vendor)`` of the AGGREGATE from the corpus;
        this cache is a decomposition of that aggregate and there is nothing in
        the repair plan that says how to decompose it - the plan is per
        ``(day, model)``. Keeping the old split beside a rebuilt total is the
        one thing worse than having no split: two numbers about the same day
        that disagree, with nothing to tell the reader which is the true one.

        So the day's rows for that vendor go, and the menu says why (the audit
        note gains "(project split for <day> reset)"). The next scan re-attributes
        whatever it reads; a day whose transcripts have been pruned simply has
        no decomposition any more, which is honest.
        """
        parse_day_key(day)
        target = str(vendor)
        removed = 0
        with self._lock:
            for store in (self._projects, self._sessions):
                rows = store.get(day)
                if not rows:
                    continue
                for key in list(rows):
                    if _split_key(key)[0] == target:
                        removed += len(rows.pop(key))
                if not rows:
                    del store[day]
            if removed:
                self._dirty = True
        return removed

    def drop_vendors(self, vendors: Iterable[Vendor]) -> int:
        """Forget every row of the named vendors. Returns cells removed."""
        targets = {str(v) for v in vendors}
        if not targets:
            return 0
        removed = 0
        with self._lock:
            for store in (self._projects, self._sessions):
                for day in list(store):
                    for key in list(store[day]):
                        if _split_key(key)[0] in targets:
                            removed += len(store[day].pop(key))
                    if not store[day]:
                        del store[day]
            if removed:
                self._dirty = True
        return removed

    # -- reading ----------------------------------------------------------

    def project_rollups(self) -> tuple[tuple[DayKey, str, DayRollup], ...]:
        """``(day, project, rollup)`` for every stored project cell.

        What the history mirror consumes. The vendor stays inside each model
        key, so ``history.py`` derives the ``vendor`` column exactly as it does
        for the aggregate rows and no second spelling of it exists.
        """
        out: list[tuple[DayKey, str, DayRollup]] = []
        with self._lock:
            for day in sorted(self._projects):
                for key in sorted(self._projects[day]):
                    models = {
                        model: ModelUsage.from_counters(tuple(counters))
                        for model, counters in self._projects[day][key].items()
                        if any(counters)
                    }
                    if models:
                        out.append((day, _split_key(key)[1], DayRollup(day=day, models=models)))
        return tuple(out)

    def top_projects(
        self,
        day: DayKey,
        pricing: PricingTable | None = None,
        *,
        limit: int = 5,
    ) -> tuple[AttributionRow, ...]:
        """The most expensive projects of *day*, dearest first."""
        return self._rows(self._projects, day, pricing, limit=limit, sessions=False)

    def top_sessions(
        self,
        day: DayKey,
        pricing: PricingTable | None = None,
        *,
        limit: int = 5,
    ) -> tuple[AttributionRow, ...]:
        """The most expensive sessions of *day*, dearest first."""
        return self._rows(self._sessions, day, pricing, limit=limit, sessions=True)

    # -- internals --------------------------------------------------------

    @staticmethod
    def _add_locked(
        store: dict[DayKey, dict[str, dict[ModelKey, list[int]]]],
        day: DayKey,
        key: str,
        model: ModelKey,
        usage: ModelUsage,
    ) -> None:
        day_bucket = store.setdefault(day, {})
        scope_bucket = day_bucket.setdefault(key, {})
        row = scope_bucket.get(model)
        counters = usage.as_counters()
        if row is None:
            scope_bucket[model] = list(counters)
            return
        for i, value in enumerate(counters):
            row[i] += value

    @staticmethod
    def _retract_locked(
        store: dict[DayKey, dict[str, dict[ModelKey, list[int]]]],
        day: DayKey,
        key: str,
        model: ModelKey,
        usage: ModelUsage,
    ) -> bool:
        """Subtract one cell. Returns True when something had to be clamped."""
        row = store.get(day, {}).get(key, {}).get(model)
        if row is None:
            return any(usage.as_counters())
        clamped = False
        for i, value in enumerate(usage.as_counters()):
            if value > row[i]:
                clamped = True
                row[i] = 0
            else:
                row[i] -= value
        if not any(row):
            del store[day][key][model]
            if not store[day][key]:
                del store[day][key]
            if not store[day]:
                del store[day]
        return clamped

    def _rows(
        self,
        store: Mapping[DayKey, Mapping[str, Mapping[ModelKey, Sequence[int]]]],
        day: DayKey,
        pricing: PricingTable | None,
        *,
        limit: int,
        sessions: bool,
    ) -> tuple[AttributionRow, ...]:
        parse_day_key(day)
        day_date = parse_day_key(day)
        with self._lock:
            bucket = {key: dict(models) for key, models in store.get(day, {}).items()}
        rows: list[AttributionRow] = []
        for key, models in bucket.items():
            vendor, project, session = _split_key(key)
            total = ModelUsage()
            usd = Decimal(0)
            unpriced = 0
            for model, counters in models.items():
                usage = ModelUsage.from_counters(tuple(counters))
                total = total + usage
                if pricing is None:
                    continue
                try:
                    known = bool(pricing.is_known(model))
                except Exception:
                    known = False
                if not known:
                    unpriced += usage.total_tokens
                    continue
                usd += _price(pricing, model, usage, day_date)
            if total.is_zero:
                continue
            rows.append(
                AttributionRow(
                    vendor=vendor,
                    project=project,
                    session=session if sessions else "",
                    usage=total,
                    usd=_quantise(usd),
                    unpriced_tokens=unpriced,
                )
            )
        rows.sort(key=lambda row: (-row.usd, -row.total_tokens, row.label))
        return tuple(rows[: max(0, int(limit))])

    def _log(self, message: str) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            logger(message)
        except Exception:
            pass


def _price(
    pricing: PricingTable, model: str, usage: ModelUsage, day: dt.date
) -> Decimal:
    """``pricing.cost_usd`` with every failure mode flattened to zero."""
    try:
        value = pricing.cost_usd(model, usage, day)
    except Exception:
        return Decimal(0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Decimal(0)
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        return Decimal(0)
    return Decimal(str(value))


def _days_to_json(
    store: Mapping[DayKey, Mapping[str, Mapping[ModelKey, Sequence[int]]]],
) -> dict[str, dict[str, list[int]]]:
    out: dict[str, dict[str, list[int]]] = {}
    for day in sorted(store):
        rows: dict[str, list[int]] = {}
        for key in sorted(store[day]):
            for model, counters in sorted(store[day][key].items()):
                if any(counters):
                    rows[f"{key}{_SEP}{model}"] = [int(v) for v in counters]
        if rows:
            out[str(day)] = rows
    return out


def _days_from_json(
    parsed: object, section: str
) -> dict[DayKey, dict[str, dict[ModelKey, list[int]]]]:
    """Parse one section, skipping malformed rows rather than raising.

    Lenient for the same reason ``ledger_from_json`` is: this is a cache, and a
    row we cannot read must cost that row, never the file.
    """
    out: dict[DayKey, dict[str, dict[ModelKey, list[int]]]] = {}
    if not isinstance(parsed, Mapping):
        return out
    days = parsed.get(section)
    if not isinstance(days, Mapping):
        return out
    for day, rows in days.items():
        if not isinstance(rows, Mapping):
            continue
        try:
            parse_day_key(str(day))
        except ValueError:
            continue
        for flat, counters in rows.items():
            if not isinstance(counters, Sequence) or isinstance(counters, (str, bytes)):
                continue
            if len(counters) != len(COUNTER_FIELDS):
                continue
            values: list[int] = []
            ok = True
            for value in counters:
                if isinstance(value, bool) or not isinstance(value, int):
                    ok = False
                    break
                values.append(value if value > 0 else 0)
            if not ok or not any(values):
                continue
            key, sep, model = str(flat).rpartition(_SEP)
            if not sep or not key or not model:
                continue
            out.setdefault(str(day), {}).setdefault(key, {})[model] = values
    return out


def _within(
    store: dict[DayKey, dict[str, dict[ModelKey, list[int]]]],
    today: DayKey,
    keep_days: int,
) -> dict[DayKey, dict[str, dict[ModelKey, list[int]]]]:
    """The subset of *store* inside the ``keep_days`` window around *today*."""
    today_date = parse_day_key(today)
    start = today_date - dt.timedelta(days=keep_days - 1)
    end = today_date + dt.timedelta(days=keep_days)
    out: dict[DayKey, dict[str, dict[ModelKey, list[int]]]] = {}
    for day, rows in store.items():
        try:
            day_date = parse_day_key(day)
        except ValueError:
            continue
        if start <= day_date <= end:
            out[day] = rows
    return out


def open_attribution(
    path: Path | str | None = None,
    *,
    keep_days: int = 30,
    logger: Callable[[str], None] | None = None,
) -> AttributionStore:
    """Construct a store. Never touches the disk by itself."""
    return AttributionStore(path=path, keep_days=keep_days, logger=logger)
