"""Process lifecycle, file hygiene and log volume (Usage Bar push 2026-09-25,
lane ops-lifecycle).

Each test pins one operational failure found on the running widget:

* OPS-1  - the startup tmp sweep globbed ``<state>.tmp.*`` and so never matched
  ``scan_state_dedup.json.tmp.<pid>``; seven orphans (Aug 25 .. Sep 23)
  survived every restart.
* OPS-2  - launchd's default 5 s exit timeout versus a 5 s quit grace, and
  writers that streamed ``json.dump`` into an open tmp, caught only OSError and
  left truncated tmp files behind.
* drift-1 - the dedup sidecar's only writer was the shutdown flush, so it sat
  on 2026-09-18 for a week.
* OPS-5  - one incident (account 6 http-403, then 429) wrote ~800 identical
  WARNING lines and buried everything around them.
* OPS-7  - the log bound ran at launch only; 68% of the file was
  ``MallocStackLogging`` noise.
* drift-5 - the claude_swap symbols the widget imports, two of them private,
  are pinned so an upstream upgrade that drops one goes red here.

Every path is inside a ``TemporaryDirectory``; nothing touches the installed
widget's home, ``~/.codex`` or the network. Request ids are ``SYNTHETIC_*``.

Run directly (pytest is absent from the claude-swap venv)::

    ~/.local/share/uv/tools/claude-swap/bin/python tests/test_ops.py
"""

from __future__ import annotations

import datetime as dt
import inspect
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget import __main__ as main_mod  # noqa: E402
from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import codex_indexer as codex_mod  # noqa: E402
from cc_usage_widget import indexer as indexer_mod  # noqa: E402
from cc_usage_widget.app import BackgroundWorker, UiSnapshot  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    local_day_key,
    normalize_settings,
)
from cc_usage_widget.indexer import Indexer  # noqa: E402
from cc_usage_widget.pricing import DEFAULT_PRICING  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402

FABLE = "claude-fable-5-20260514"
TWO_DAYS = 2 * 86_400

try:  # an OPTIONAL dependency (README: "account features need claude-swap")
    import claude_swap as _claude_swap
except ImportError:  # a clean machine, and CI
    _claude_swap = None  # type: ignore[assignment]

_CLAUDE_SWAP_PRESENT = _claude_swap is not None
"""Whether drift-5's contract test has anything to resolve. Only the PACKAGE
being absent skips it; a symbol missing from an installed claude-swap is
exactly what that test exists to turn red."""


class _NeedsClaudeSwap(Exception):
    """A test that cannot run without the optional dependency. The runner
    prints one ``skip`` line and counts it; never counted as a pass (the
    convention tests/test_fleet.py set in 392b4dd)."""


def _require_claude_swap() -> None:
    if not _CLAUDE_SWAP_PRESENT:
        raise _NeedsClaudeSwap("claude_swap is not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def _iso_utc(epoch: float) -> str:
    moment = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _record(request_id: str, epoch: float, input_tokens: int = 1_000) -> dict[str, Any]:
    """One assistant record shaped like a Claude Code transcript line."""
    return {
        "type": "assistant",
        "requestId": request_id,
        "timestamp": _iso_utc(epoch),
        "message": {
            "id": f"msg_{request_id}",
            "role": "assistant",
            "model": FABLE,
            "usage": {"input_tokens": input_tokens},
        },
    }


def _write_transcript(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _indexer(root: Path, *, now: float) -> Indexer:
    """The wired shape: deferred commit, own state path, injected clock."""
    return Indexer(
        projects_dir=root / "projects",
        state_path=root / "scan_state.json",
        lookback_days=30,
        pricing=DEFAULT_PRICING,
        now=lambda: now,
        defer_state_commit=True,
    )


def _worker(indexer: Any, rollups: Any = None) -> BackgroundWorker:
    return BackgroundWorker(
        publish=lambda _snapshot: None,
        snapshot=UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        indexer=indexer,
        rollups=rollups,
        pricing=DEFAULT_PRICING,
    )


def _tmp_leftovers(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if ".tmp" in p.name)


class _Patched:
    """Swap one attribute for the duration of a ``with``; always restored."""

    def __init__(self, owner: Any, name: str, value: Any) -> None:
        self._owner, self._name, self._value = owner, name, value

    def __enter__(self) -> None:
        self._saved = getattr(self._owner, self._name)
        setattr(self._owner, self._name, self._value)

    def __exit__(self, *_exc: Any) -> None:
        setattr(self._owner, self._name, self._saved)


def _interrupting_replace(_src: Any, _dst: Any) -> None:
    raise KeyboardInterrupt("SYNTHETIC interrupt between write and replace")


# ---------------------------------------------------------------------------
# OPS-1 - the startup sweep matches the files that actually leak
# ---------------------------------------------------------------------------


def test_the_orphan_sweep_removes_every_helpers_aged_tmp_and_nothing_else() -> None:
    """Before the fix only ``<state>.tmp.*`` of three names was globbed, so
    ``scan_state_dedup.json.tmp.99999`` and every dot-prefixed mkstemp name
    survived: the first assertion below failed on all five aged files."""
    with tempfile.TemporaryDirectory() as name:
        home = Path(name)
        aged = [
            "scan_state_dedup.json.tmp.99999",  # indexer.flush_dedup
            ".rollups.json.abc123.tmp",  # rollup mkstemp
            ".notify_state.json.x1.tmp.json",  # notify mkstemp
            "codex_quota_snapshots.json.tmp.k9",  # codex_accounts mkstemp
            "attribution.json.tmp",  # attribution
            "dashboard.html.tmp",  # dashboard
            ".audit_state.json.tmp.99998",  # audit
        ]
        kept_real = [
            "scan_state_dedup.json",
            "scan_state.json",
            "rollups.json",
            "settings.json",
            "dashboard.html",
            "history.sqlite",
            "widget.lock",
        ]
        fresh = "scan_state_dedup.json.tmp.1"
        live_pid = f"scan_state.json.tmp.{os.getpid()}"
        for item in aged + kept_real + [fresh, live_pid]:
            (home / item).write_text("{}", encoding="utf-8")
        for item in aged + kept_real + [live_pid]:
            _age(home / item, TWO_DAYS)
        # A real state file must never look like a temp name, whatever its age.
        for item in kept_real:
            assert not main_mod._TMP_ORPHAN_RE.match(item), item

        main_mod._sweep_tmp_orphans(home=home)

        left = {p.name for p in home.iterdir()}
        assert not (set(aged) & left), sorted(set(aged) & left)
        assert set(kept_real) <= left, sorted(set(kept_real) - left)
        assert fresh in left, "a tmp younger than a day may belong to a live write"
        assert live_pid in left, "a tmp whose pid is alive is not an orphan"


def test_a_live_pid_reprieve_ends_after_a_week() -> None:
    """R2-SEC-3. The pid reprieve had no ceiling, and macOS reuses pids: a
    23-day-old ``scan_state_dedup.json.tmp.1068`` was pinned by an unrelated
    TextInputMenuAgent that now owns 1068, and would have been kept forever.
    Past a week the pid no longer matters; inside it, a live pid still
    protects. A pid token no process can own (>= 2**31) raised OverflowError
    inside ``os.kill`` - that name is judged on age and the sweep goes on."""
    with tempfile.TemporaryDirectory() as name:
        home = Path(name)
        month_old = f"scan_state_dedup.json.tmp.{os.getpid()}"
        day_and_a_bit = f"scan_state.json.tmp.{os.getpid()}"
        oversized = "rollups.json.tmp.99999999999999999999999"
        for item, age in (
            (month_old, 30 * 86_400),
            (day_and_a_bit, 25 * 3_600),
            (oversized, TWO_DAYS),
        ):
            (home / item).write_text("{}", encoding="utf-8")
            _age(home / item, age)

        main_mod._sweep_tmp_orphans(home=home)

        left = {p.name for p in home.iterdir()}
        assert month_old not in left, "a reused pid pinned a month-old orphan"
        assert day_and_a_bit in left, "inside the week a live pid still protects"
        assert oversized not in left, "no process owns that pid: age decides"


def test_the_sweep_leaves_subdirectories_alone() -> None:
    """``codex-accounts/<id>/`` holds credentials: top level only."""
    with tempfile.TemporaryDirectory() as name:
        home = Path(name)
        nested = home / "codex-accounts" / "SYNTHETIC_acct"
        nested.mkdir(parents=True)
        inner = nested / "auth.json.tmp.abc"
        inner.write_text("{}", encoding="utf-8")
        _age(inner, TWO_DAYS)
        main_mod._sweep_tmp_orphans(home=home)
        assert inner.exists()


# ---------------------------------------------------------------------------
# OPS-2 - bytes-first atomic writes; no tmp survives a failure; ExitTimeOut
# ---------------------------------------------------------------------------


def _dedup_indexer(root: Path) -> Indexer:
    now = time.time()
    _write_transcript(root / "projects" / "p" / "a.jsonl", [_record("SYNTHETIC_req1", now)])
    indexer = _indexer(root, now=now)
    indexer.scan_once()
    assert indexer.dedup_size == 1, "the fixture credited nothing"
    return indexer


def test_flush_dedup_leaves_no_tmp_when_serialisation_fails() -> None:
    """Before the fix json.dump streamed into an already-open tmp and only
    OSError was caught: the TypeError left ``scan_state_dedup.json.tmp.<pid>``
    behind (asserted empty below)."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        indexer = _dedup_indexer(root)
        indexer._dedup_usage["SYNTHETIC_bad"] = object()  # type: ignore[assignment]
        raised = False
        try:
            indexer.flush_dedup()
        except TypeError:
            raised = True  # a bug must surface, not vanish
        assert raised
        assert _tmp_leftovers(root) == [], _tmp_leftovers(root)
        assert not (root / "scan_state_dedup.json").exists()


def test_flush_dedup_leaves_no_tmp_when_interrupted_before_replace() -> None:
    """A BaseException between write and replace: tmp unlinked, re-raised."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        indexer = _dedup_indexer(root)
        raised = False
        with _Patched(indexer_mod.os, "replace", _interrupting_replace):
            try:
                indexer.flush_dedup()
            except KeyboardInterrupt:
                raised = True
        assert raised
        assert _tmp_leftovers(root) == [], _tmp_leftovers(root)


def test_the_scan_state_write_leaves_no_tmp_when_interrupted() -> None:
    """The indexer's own scan-state write (no saver wired): same guarantee,
    and the offsets stay dirty so the next commit retries."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        indexer = _dedup_indexer(root)
        raised = False
        with _Patched(indexer_mod.os, "replace", _interrupting_replace):
            try:
                indexer.commit_state()
            except KeyboardInterrupt:
                raised = True
        assert raised
        assert _tmp_leftovers(root) == [], _tmp_leftovers(root)
        assert indexer.commit_state() is True, "the dirty flag must survive"
        assert (root / "scan_state.json").exists()
        assert indexer.commit_state() is False, "nothing new -> nothing written"


def test_an_io_failure_still_returns_false_and_never_raises() -> None:
    """The negative control: OSError keeps its old contract (False)."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        blocker = root / "not_a_dir"
        blocker.write_text("", encoding="utf-8")
        target = str(blocker / "x.json")
        assert indexer_mod._write_json_atomic(target, {"a": 1}) is False
        source = codex_mod.CodexIndexer.__new__(codex_mod.CodexIndexer)
        assert source._atomic_write_json(target, {"a": 1}) is False


def test_codex_atomic_write_leaves_no_tmp_on_failure() -> None:
    """codex_indexer's writer: before the fix a TypeError mid-dump escaped
    with the tmp still on disk."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        source = codex_mod.CodexIndexer.__new__(codex_mod.CodexIndexer)
        target = str(root / "codex_scan_state.json")
        raised = False
        try:
            source._atomic_write_json(target, {"SYNTHETIC": object()})
        except TypeError:
            raised = True
        assert raised
        assert _tmp_leftovers(root) == [], _tmp_leftovers(root)
        raised = False
        with _Patched(codex_mod.os, "replace", _interrupting_replace):
            try:
                source._atomic_write_json(target, {"ok": 1})
            except KeyboardInterrupt:
                raised = True
        assert raised
        assert _tmp_leftovers(root) == [], _tmp_leftovers(root)
        assert source._atomic_write_json(target, {"ok": 1}) is True
        assert json.loads(Path(target).read_text(encoding="utf-8")) == {"ok": 1}


def _plist_heredoc() -> str:
    text = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
    match = re.search(r'cat > "\$PLIST" <<EOF\n(.*?)\nEOF\n', text, re.DOTALL)
    assert match, "install.sh no longer writes the LaunchAgent plist via a heredoc"
    return match.group(1)


def test_the_launch_agent_exit_timeout_covers_the_quit_grace() -> None:
    """launchd SIGKILLs after ExitTimeOut (default 5 s). Before the fix the key
    was absent, so a quit that used its whole 5 s worker grace plus one tick
    was killed mid final flush."""
    import plistlib

    body = _plist_heredoc().replace("$HERE", "/SYNTHETIC/home")
    plist = plistlib.loads(body.encode("utf-8"))
    need = (
        app_mod.CCUsageWidgetApp.QUIT_GRACE_SECONDS + app_mod.SYNC_INTERVAL_SECONDS + 5
    )
    assert "ExitTimeOut" in plist, "ExitTimeOut missing: launchd's 5 s default applies"
    assert plist["ExitTimeOut"] >= need, (plist["ExitTimeOut"], need)
    assert plist["Label"] == "com.cc-usage-widget"


_FAKE_LAUNCHCTL = """#!/bin/sh
# A stand-in for launchctl: logs every call, keeps "loaded" as a file. The
# real one refuses to bootstrap a loaded label, and so does this.
echo "$*" >> "$FAKE_LAUNCHCTL_LOG"
case "$1" in
  print) [ -f "$FAKE_LAUNCHCTL_STATE" ] || exit 113; echo "	exit timeout = 20"; exit 0 ;;
  bootout) rm -f "$FAKE_LAUNCHCTL_STATE"; exit 0 ;;
  bootstrap)
    if [ -f "$FAKE_LAUNCHCTL_STATE" ]; then
      echo "Bootstrap failed: 5: Input/output error" >&2; exit 5
    fi
    touch "$FAKE_LAUNCHCTL_STATE"; exit 0 ;;
esac
exit 64
"""


def _run_install(root: Path, *args: str, loaded: bool) -> tuple[str, list[str], bool]:
    """Run a COPY of install.sh with a temp HOME, the test's own Python as the
    venv and a fake ``launchctl`` first on PATH - the live agent is never
    touched. Returns (output, launchctl calls, loaded afterwards)."""
    import shutil
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    here = root / "widget"
    shutil.copytree(repo / "cc_usage_widget", here / "cc_usage_widget",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(repo / "install.sh", here / "install.sh")
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    fakebin = root / "fakebin"
    fakebin.mkdir()
    (fakebin / "launchctl").write_text(_FAKE_LAUNCHCTL)
    for tool in (venv / "bin" / "python", fakebin / "launchctl"):
        tool.chmod(0o755)
    state, log = root / "loaded", root / "launchctl.log"
    if loaded:
        state.touch()
    home = root / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": f"{fakebin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "CC_WIDGET_VENV": str(venv),
        "FAKE_LAUNCHCTL_STATE": str(state),
        "FAKE_LAUNCHCTL_LOG": str(log),
    }
    done = subprocess.run(
        ["/bin/bash", str(here / "install.sh"), *args],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, (done.returncode, done.stdout, done.stderr)
    assert (home / "Library/LaunchAgents/com.cc-usage-widget.plist").exists()
    calls = log.read_text().splitlines() if log.exists() else []
    return done.stdout, calls, state.exists()


def _verbs(calls: list[str]) -> list[str]:
    return [call.split()[0] for call in calls]


def test_install_keeps_the_respawn_guards() -> None:
    """A re-run of install.sh on the live Mac (2026-09-25) wrote a bare run.sh
    and a plist without ThrottleInterval, silently undoing the Aug 25
    hardening: with the venv gone, KeepAlive would respawn a dead exec every
    10 s. Both guards must come from the script, not from a hand edit."""
    import plistlib
    import subprocess

    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _run_install(root, "--launch-agent", loaded=False)
        plist = plistlib.loads(
            (root / "home/Library/LaunchAgents/com.cc-usage-widget.plist").read_bytes()
        )
        assert plist.get("ThrottleInterval", 0) >= 60, plist
        run_sh = root / "widget" / "run.sh"
        text = run_sh.read_text()
        assert 'if [ ! -x "$PY" ]' in text and "exit 1" in text, text
        # The guard really fails loudly: point it at a missing interpreter
        # with no uv on PATH and it must exit 1, not exec.
        (root / "venv" / "bin" / "python").unlink()
        done = subprocess.run(
            ["/bin/bash", str(run_sh)], capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": str(root / "home")},
        )
        assert done.returncode == 1 and "FATAL" in done.stderr, (done.returncode, done.stderr)


def test_install_launch_agent_reaches_an_already_loaded_agent() -> None:
    """R2-OPS-1. launchd reads a plist at bootstrap only and refuses to
    bootstrap a loaded label, and the script printed ONLY ``launchctl
    bootstrap`` - so on the running widget the new ExitTimeOut never landed.
    Loaded: the hint is bootout, wait, bootstrap (nothing is run). With
    --reload the script does exactly that. Not loaded: bootstrap, as before."""
    with tempfile.TemporaryDirectory() as name:
        out, calls, still = _run_install(Path(name), "--launch-agent", loaded=True)
        assert set(_verbs(calls)) == {"print"}, ("a hint must not act", calls)
        assert still
        boot_out = out.find("launchctl bootout gui/")
        boot_strap = out.find("launchctl bootstrap gui/")
        assert 0 <= boot_out < boot_strap, out
        assert "--reload" in out, out

    with tempfile.TemporaryDirectory() as name:
        out, calls, still = _run_install(Path(name), "--launch-agent", "--reload", loaded=True)
        verbs = _verbs(calls)
        assert "bootout" in verbs and "bootstrap" in verbs, calls
        assert verbs.index("bootout") < verbs.index("bootstrap"), calls
        between = verbs[verbs.index("bootout") + 1 : verbs.index("bootstrap")]
        assert "print" in between, ("bootstrap before the teardown was checked", calls)
        assert still, "the agent must end up loaded"
        assert "exit timeout = 20" in out, out

    with tempfile.TemporaryDirectory() as name:
        out, calls, still = _run_install(Path(name), "--launch-agent", loaded=False)
        assert "launchctl bootstrap gui/" in out and "bootout" not in out, out
        assert set(_verbs(calls)) == {"print"} and not still, calls


def test_package_ships_every_module_and_no_runtime_state() -> None:
    """package.sh's hand-kept allowlist named 11 of the package's modules, so
    its own completeness check refused every build (codex_accounts.py,
    codex_login.py, fleet.py ... NOT IN THE ALLOWLIST). The code half is now
    derived; --dry-run runs every check and writes nothing."""
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        ["/bin/bash", str(repo / "package.sh"), "--dry-run"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, (done.stdout, done.stderr)
    listed = set(done.stdout.split())
    on_disk = {
        str(path.relative_to(repo))
        for folder in ("cc_usage_widget", "tests")
        for path in (repo / folder).glob("*.py")
    }
    assert on_disk <= listed, sorted(on_disk - listed)
    assert {"README.md", "LICENSE", "install.sh"} <= listed, listed
    leaked = [f for f in listed if re.search(r"rollups\.json|scan_state|settings\.json|widget\.lock", f)]
    assert not leaked, leaked
    assert "nothing written" in done.stderr, done.stderr


def test_package_accepts_the_public_docs_layout() -> None:
    """The public repository keeps the SPECs under docs/ (REPO-PLAN §2), and
    package.sh's root-only SPEC.md entry failed there with 'allowlisted but
    absent: SPEC.md' - i.e. on the public CI of the 2026-09-25 sync. Both
    layouts package; neither is silently incomplete."""
    import shutil
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as name:
        tree = Path(name) / "pub"
        shutil.copytree(repo / "cc_usage_widget", tree / "cc_usage_widget",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(repo / "tests", tree / "tests",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (tree / "docs").mkdir()
        for doc in ("SPEC.md", "SPEC-CODEX.md"):
            src = repo / doc if (repo / doc).exists() else repo / "docs" / doc
            shutil.copy2(src, tree / "docs" / doc)
        for top in ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE",
                    "install.sh", "uninstall.sh", "package.sh"):
            shutil.copy2(repo / top, tree / top)
        done = subprocess.run(
            ["/bin/bash", str(tree / "package.sh"), "--dry-run"],
            cwd=tree, capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 0, (done.stdout, done.stderr)
        listed = set(done.stdout.split())
        assert {"docs/SPEC.md", "docs/SPEC-CODEX.md"} <= listed, listed
        # And a SPEC missing from both places still refuses the build.
        (tree / "docs" / "SPEC.md").unlink()
        gone = subprocess.run(
            ["/bin/bash", str(tree / "package.sh"), "--dry-run"],
            cwd=tree, capture_output=True, text=True, timeout=60,
        )
        assert gone.returncode != 0 and "SPEC.md" in gone.stderr, (gone.returncode, gone.stderr)


# ---------------------------------------------------------------------------
# drift-1 - the dedup sidecar is persisted with every offsets commit
# ---------------------------------------------------------------------------


def test_a_committing_tick_persists_the_dedup_sidecar() -> None:
    """Before the fix ``_commit_scan_state`` wrote offsets only; the sidecar
    waited for a shutdown flush (``exists()`` below was False)."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = time.time()
        _write_transcript(root / "projects" / "p" / "a.jsonl", [_record("SYNTHETIC_req1", clock)])
        indexer = _indexer(root, now=clock)
        worker = _worker(indexer)
        indexer.scan_once()
        worker._commit_scan_state()

        assert (root / "scan_state.json").exists(), "offsets were not committed"
        sidecar = root / "scan_state_dedup.json"
        assert sidecar.exists(), "a commit that landed must persist the sidecar"
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        assert raw["day"] == local_day_key(clock), raw["day"]
        assert "SYNTHETIC_req1" in raw["requests"], raw


def test_an_idle_tick_writes_neither_file() -> None:
    """The steady budget: a commit with nothing new writes no sidecar."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = time.time()
        _write_transcript(root / "projects" / "p" / "a.jsonl", [_record("SYNTHETIC_req1", clock)])
        indexer = _indexer(root, now=clock)
        worker = _worker(indexer)
        indexer.scan_once()
        worker._commit_scan_state()
        sidecar = root / "scan_state_dedup.json"
        sidecar.unlink()
        indexer.scan_once()  # nothing appended
        worker._commit_scan_state()
        assert not sidecar.exists(), "an idle tick rewrote the sidecar"
        # ...but the quit path still flushes whatever it holds.
        worker._flush()
        assert sidecar.exists(), "the final flush must write regardless"


def test_the_persist_limit_still_skips_the_write() -> None:
    """Unchanged guard: a map over ``_DEDUP_PERSIST_LIMIT`` is never written."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        clock = time.time()
        _write_transcript(root / "projects" / "p" / "a.jsonl", [_record("SYNTHETIC_req1", clock)])
        indexer = _indexer(root, now=clock)
        worker = _worker(indexer)
        indexer.scan_once()
        with _Patched(indexer_mod, "_DEDUP_PERSIST_LIMIT", 0):
            worker._commit_scan_state()
        assert (root / "scan_state.json").exists()
        assert not (root / "scan_state_dedup.json").exists()


# ---------------------------------------------------------------------------
# OPS-5 - identical WARNINGs collapse to one line and a count
# ---------------------------------------------------------------------------

_HTTP_403 = "Usage fetch failed for account 6: http-403"


def _collapsing_logger(clock: list[float]) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
    handler.addFilter(main_mod._RepeatCollapser(window_s=3600.0, clock=lambda: clock[0]))
    logger = logging.getLogger("claude-swap.SYNTHETIC_ops_test")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def _lines(stream: io.StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line]


def test_repeated_warnings_collapse_to_one_line_and_a_count() -> None:
    """Before the fix there was no filter: 10 records -> 10 lines."""
    base = dt.datetime(2026, 9, 24, 10, 0).timestamp()
    clock = [base]
    logger, stream = _collapsing_logger(clock)
    for minute in range(10):
        clock[0] = base + 60 * minute
        logger.warning(_HTTP_403)
    assert _lines(stream) == [f"claude-swap.SYNTHETIC_ops_test WARNING: {_HTTP_403}"], _lines(stream)

    # Different keys pass at once: another account, and the 429 variant.
    logger.warning("Usage fetch failed for account 3: network")
    logger.warning(
        "Usage fetch failed for account 6: http-429, retry-after 3600s "
        "(usage-endpoint budget reached; backing off)"
    )
    assert len(_lines(stream)) == 3, _lines(stream)
    # The same 429 with a different retry-after is the same incident.
    logger.warning(
        "Usage fetch failed for account 6: http-429, retry-after 3541s "
        "(usage-endpoint budget reached; backing off)"
    )
    assert len(_lines(stream)) == 3, _lines(stream)

    # After the window the next repeat is emitted with the count.
    clock[0] = base + 3600
    logger.warning(_HTTP_403)
    last = _lines(stream)[-1]
    assert last.endswith(f"{_HTTP_403} (repeated 9 times since 10:00)"), last
    # ...and opens a fresh window.
    clock[0] = base + 3660
    logger.warning(_HTTP_403)
    assert len(_lines(stream)) == 4, _lines(stream)


def _installed_collapser(
    clock: list[float],
) -> tuple[logging.Logger, io.StringIO, Any, list[tuple[float, Any]]]:
    """A collapser wired the way ``_configure_logging`` wires it, with the
    clock and the window-close timer both in the test's hands."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
    timers: list[tuple[float, Any]] = []
    collapser = main_mod._RepeatCollapser(
        window_s=3600.0,
        clock=lambda: clock[0],
        schedule=lambda delay, callback: timers.append((delay, callback)),
    )
    collapser.install(handler)
    logger = logging.getLogger("claude-swap.SYNTHETIC_burst_test")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream, collapser, timers


def test_a_burst_that_stops_inside_its_window_still_reports_its_count() -> None:
    """R2-SEC-2. Six 403s at t = 0, 600 .. 3000 s and then silence left ONE
    line and no count: the count was only ever attached to a repeat arriving
    after the window. It now lands when the window closes (the timer), on the
    next record of any key past the close, or at the exit flush."""
    base = dt.datetime(2026, 9, 24, 1, 0).timestamp()
    counted = f"{_HTTP_403} (repeated 5 times since 01:00)"

    # (a) the window-close timer
    clock = [base]
    logger, stream, _collapser, timers = _installed_collapser(clock)
    for step in range(6):
        clock[0] = base + 600 * step
        logger.warning(_HTTP_403)
    assert len(_lines(stream)) == 1, _lines(stream)
    assert len(timers) == 1, "one timer, armed by the first dropped repeat"
    delay, fire = timers[0]
    assert delay == 3600 - 600, delay
    clock[0] = base + 3600
    fire()
    assert _lines(stream)[-1].endswith(counted), _lines(stream)
    assert len(_lines(stream)) == 2
    clock[0] = base + 3700
    logger.warning(_HTTP_403)  # a later 403 is a new first line, not a count
    assert _lines(stream)[-1].endswith(_HTTP_403), _lines(stream)

    # (b) the next record of ANOTHER key, once the window has closed
    clock = [base]
    logger, stream, _collapser, _timers = _installed_collapser(clock)
    for step in range(6):
        clock[0] = base + 600 * step
        logger.warning(_HTTP_403)
    clock[0] = base + 4000
    logger.info("Switched Account-1 -> Account-5 (failover)")
    lines = _lines(stream)
    assert len(lines) == 3 and lines[1].endswith(counted), lines

    # (c) the exit flush, window still open; nothing pending -> nothing written
    clock = [base]
    logger, stream, collapser, _timers = _installed_collapser(clock)
    for step in range(6):
        clock[0] = base + 600 * step
        logger.warning(_HTTP_403)
    collapser.flush()
    lines = _lines(stream)
    assert len(lines) == 2 and lines[1].endswith(counted), lines
    collapser.flush()
    assert len(_lines(stream)) == 2, "a count is written once"


def test_info_records_and_other_handlers_are_untouched() -> None:
    """INFO (switches) always passes, and a second handler on the same logger
    - claude-swap's own file handler - sees every record unmodified."""
    clock = [1_000_000.0]
    logger, stream = _collapsing_logger(clock)
    other = io.StringIO()
    plain = logging.StreamHandler(other)
    plain.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(plain)
    for _ in range(3):
        logger.info("Switched Account-1 -> Account-5 (failover)")
    for _ in range(3):
        logger.warning(_HTTP_403)
    clock[0] += 3600
    logger.warning(_HTTP_403)
    assert len(_lines(stream)) == 3 + 1 + 1, _lines(stream)
    assert _lines(other) == ["Switched Account-1 -> Account-5 (failover)"] * 3 + [_HTTP_403] * 4


def test_configure_logging_attaches_the_collapser_to_its_handler() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        root.handlers[:] = []
        stream = io.StringIO()
        handler = main_mod._configure_logging(stream=stream)
        assert handler is not None and handler in root.handlers
        assert any(isinstance(f, main_mod._RepeatCollapser) for f in handler.filters)
        log = logging.getLogger("claude-swap.SYNTHETIC_cfg")
        for _ in range(5):
            log.warning(_HTTP_403)
        assert len(_lines(stream)) == 1, _lines(stream)
        # The menu Quit path: rumps emits before_quit from
        # applicationWillTerminate_, which skips atexit, so the flush must be
        # registered there too or a burst still inside its window loses its count.
        try:
            import rumps.events as rumps_events
        except ImportError:
            rumps_events = None
        if rumps_events is not None:
            assert main_mod._flush_repeat_counts in rumps_events.before_quit.callbacks
            rumps_events.before_quit.emit()
        else:
            # The exit flush (atexit and the SIGTERM handler) reaches this handler.
            main_mod._flush_repeat_counts()
        assert len(_lines(stream)) == 2 and "(repeated 4 times since" in _lines(stream)[1], (
            _lines(stream)
        )
        # A host that already configured logging keeps its own handler.
        assert main_mod._configure_logging(stream=io.StringIO()) is None
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# OPS-7 - the log bound drops the noise and runs daily from the worker
# ---------------------------------------------------------------------------

_MALLOC = b"python(46941) MallocStackLogging: can't turn off malloc stack logging because it was not enabled.\n"


def _oversized_log(path: Path, size: int = 6_000_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = b"[2026-09-24 10:00:00] claude-swap INFO: SYNTHETIC line of ordinary width here\n"
    chunk = (line * 9 + _MALLOC) * 100
    with path.open("wb") as handle:
        written = 0
        while written < size:
            handle.write(chunk)
            written += len(chunk)


def test_log_bound_keeps_a_line_aligned_tail_without_malloc_noise() -> None:
    """Before the fix there was no *log_path* seam (TypeError) and the kept
    tail still carried every MallocStackLogging line."""
    with tempfile.TemporaryDirectory() as name:
        log = Path(name) / "logs" / "widget.log"
        _oversized_log(log)
        inode = log.stat().st_ino
        main_mod._bound_widget_log(max_bytes=5_000_000, log_path=log)
        data = log.read_bytes()
        assert 0 < len(data) <= 2_500_000, len(data)
        assert data.startswith(b"[2026-09-24"), data[:40]
        assert b"MallocStackLogging" not in data
        assert log.stat().st_ino == inode, "renamed: launchd's O_APPEND fd is now orphaned"


def test_log_bound_leaves_a_small_log_alone() -> None:
    with tempfile.TemporaryDirectory() as name:
        log = Path(name) / "widget.log"
        log.write_bytes(_MALLOC * 3)
        main_mod._bound_widget_log(max_bytes=5_000_000, log_path=log)
        assert log.read_bytes() == _MALLOC * 3


def test_the_worker_bounds_the_log_beside_its_store_once_per_day() -> None:
    """Before the fix only launch bounded the log; a worker running for weeks
    never did (the first assertion stayed at 6 MB)."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        now = time.time()
        _write_transcript(root / "projects" / "p" / "a.jsonl", [_record("SYNTHETIC_req1", now)])
        log = root / "logs" / "widget.log"
        _oversized_log(log)
        indexer = _indexer(root, now=now)
        rollups = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        worker = _worker(indexer, rollups)
        worker._run_cost_job()
        assert log.stat().st_size <= 2_500_000, log.stat().st_size
        # Same day: not again, even if the log has grown back.
        _oversized_log(log)
        worker._run_cost_job()
        assert log.stat().st_size >= 6_000_000, "bounded twice on one day"
        # A new local day: again.
        worker._log_bound_day = "SYNTHETIC-yesterday"
        worker._run_cost_job()
        assert log.stat().st_size <= 2_500_000, log.stat().st_size


# ---------------------------------------------------------------------------
# drift-5 - the claude_swap symbols the widget imports
# ---------------------------------------------------------------------------

CLAUDE_SWAP_CONTRACT: tuple[tuple[str, str], ...] = (
    # accounts.py:_import_backend
    ("claude_swap.autoswitch", "STATE_FILENAME"),
    ("claude_swap.autoswitch", "AutoSwitchEngine"),
    ("claude_swap.oauth", "fresh_reset_strings"),
    ("claude_swap.settings", "SETTINGS_SCHEMA_VERSION"),
    ("claude_swap.settings", "atomic_write_json"),
    ("claude_swap.settings", "load_settings"),
    ("claude_swap.settings", "settings_path"),
    ("claude_swap.snapshot_source", "SnapshotSource"),
    ("claude_swap.switcher", "SENTINEL_NOTES"),
    ("claude_swap.switcher", "ClaudeAccountSwitcher"),
    ("claude_swap.menubar", "_rolled_weekly_window"),  # PRIVATE upstream name
    # accounts.py elsewhere
    ("claude_swap.settings", "parse_model_names"),
    ("claude_swap.pace", "compute_pace"),
    # fleet.py
    ("claude_swap.paths", "get_claude_config_home"),
    ("claude_swap.session", "session_dir_for"),
    ("claude_swap.mappings", "MappingStore"),
    # methods called on the switcher
    ("claude_swap.switcher", "ClaudeAccountSwitcher.switch_to"),
    ("claude_swap.switcher", "ClaudeAccountSwitcher._account_is_switchable"),  # PRIVATE
)
"""18 (module, name) pairs, verified against claude-swap 0.25.0 and 0.26.0
(evidence/drift_symbol_check.py). A future release that drops or renames one
degrades the accounts adapter silently; this goes red instead."""

PINNED_SIGNATURES: dict[tuple[str, str], list[str]] = {
    ("claude_swap.menubar", "_rolled_weekly_window"): ["window", "now"],
    ("claude_swap.switcher", "ClaudeAccountSwitcher._account_is_switchable"): [
        "self",
        "account_num",
    ],
}


def _resolve(module: str, dotted: str) -> Any:
    import importlib

    obj: Any = importlib.import_module(module)
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def test_claude_swap_contract() -> None:
    """Proved once by renaming ``compute_pace`` to ``compute_pacex`` in the
    table: AttributeError ... red. Needs the claude-swap venv; without the
    package (CI) it is a counted skip, never a pass (R2-CI-1)."""
    assert len(CLAUDE_SWAP_CONTRACT) == 18
    _require_claude_swap()
    missing = []
    for module, dotted in CLAUDE_SWAP_CONTRACT:
        try:
            _resolve(module, dotted)
        except (ImportError, AttributeError) as exc:
            missing.append(f"{module}.{dotted}: {type(exc).__name__}")
    assert not missing, missing
    for (module, dotted), params in PINNED_SIGNATURES.items():
        got = list(inspect.signature(_resolve(module, dotted)).parameters)
        assert got == params, (module, dotted, got)


def test_the_contract_table_covers_every_claude_swap_import() -> None:
    """The table is only a guard if it is complete: every ``from claude_swap.X
    import Y`` in accounts.py / fleet.py must be listed."""
    import ast

    pkg = Path(__file__).resolve().parents[1] / "cc_usage_widget"
    listed = {(m, n.split(".")[0]) for m, n in CLAUDE_SWAP_CONTRACT}
    found: set[tuple[str, str]] = set()
    for source in ("accounts.py", "fleet.py"):
        tree = ast.parse((pkg / source).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module.startswith("claude_swap."):
                found.update((node.module, alias.name) for alias in node.names)
            elif node.module == "claude_swap":
                # `from claude_swap import pace` - a module; its members are
                # listed by name (compute_pace) and resolved above.
                found.update(
                    (f"claude_swap.{alias.name}", "") for alias in node.names
                )
    modules_listed = {m for m, _n in CLAUDE_SWAP_CONTRACT}
    whole_modules = {m for m, n in found if n == ""}
    assert whole_modules <= modules_listed, sorted(whole_modules - modules_listed)
    named = {pair for pair in found if pair[1]}
    assert named, "no claude_swap import found: the parse is broken"
    assert named <= listed, sorted(named - listed)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _tests() -> list[tuple[str, Any]]:
    items = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def _uncollected_tests(collected: list[tuple[str, Any]]) -> list[str]:
    """Test defs present in the source but absent from ``globals()`` - i.e.
    placed below the ``__main__`` guard, where the direct run never sees them."""
    source = Path(__file__).read_text(encoding="utf-8")
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
            # Counted and named, never hidden (test_fleet.py's convention).
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
        print(f"ERROR: {len(orphans)} test(s) sit below the __main__ guard: " + ", ".join(orphans))
    return 1 if (failures or orphans) else 0


if __name__ == "__main__":
    raise SystemExit(main())
