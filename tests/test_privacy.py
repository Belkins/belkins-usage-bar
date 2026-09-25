"""The privacy promise, asserted rather than described.

The README tells strangers that this tool reads local AI transcripts — files
that routinely contain their source code, credentials and customer data — and
extracts **only** token counts, model names and timestamps. That is a strong
promise about somebody else's secrets, so it is tested here rather than left as
prose.

The method is deliberately blunt: plant a canary string in every free-text
position a real transcript has (prompt, completion, tool result, file path, git
branch, error payload), run the real indexers over it, then read back **every
byte the widget wrote** — state, rollups, dedup, quota, logs — and fail if the
canary appears anywhere. It cannot pass by inspection or by a mocked parser; the
canary either survives into an artifact or it does not.

The second test is the mirror image and is the one that would catch a
regression: the same records with the canary REMOVED must still produce token
counts. A test that only proves "no canary" would also pass if the indexer
silently stopped reading anything at all.

The second half of this module applies the same method to the second secret
the widget now handles: the Codex **access token** the live per-account quota
source (SPEC-CODEX 6) reads from an ``auth.json``. Same blunt method, plus an
explicit negative control — the whole cycle is run again through a transport
that deliberately logs the ``Authorization`` header, and the leak check must
find it.

Nothing here touches ``~/.claude`` or ``~/.codex``; every path is inside a
``TemporaryDirectory``.

Run directly, or with pytest if it is installed::

    python tests/test_privacy.py
"""

from __future__ import annotations

import base64
import calendar
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cc_usage_widget import codex_indexer as codex_mod  # noqa: E402
from cc_usage_widget import indexer as claude_mod  # noqa: E402
from cc_usage_widget import rollup as rollup_mod  # noqa: E402

FIXED_NOW = calendar.timegm((2026, 8, 17, 18, 0, 0))
"""The indexers' clock, pinned six hours after the fixtures' timestamps. With
the real clock the 2026-08-17 records fell outside the 30-day lookback after
~09-16 and every leak test here passed vacuously (read nothing, leaked
nothing); a pinned clock keeps the fixtures inside the window forever."""

CANARY = "SECRET_CANARY_9F3B2_do_not_leak"
"""Distinctive enough that a substring match cannot be a coincidence, and not a
plausible token in any real transcript."""


def _claude_transcript(path: Path, *, poisoned: bool) -> None:
    """One Claude session file with a real ``message.usage`` block.

    When *poisoned*, the canary sits in every free-text position a genuine
    transcript carries: the user's prompt, the assistant's completion, a tool
    result, the cwd, and the git branch.
    """
    secret = CANARY if poisoned else "ordinary text"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "user",
            "cwd": f"/Users/someone/{secret}",
            "gitBranch": f"feature/{secret}",
            "message": {"role": "user", "content": f"here is my api key: {secret}"},
        },
        {
            "type": "assistant",
            "requestId": "req_canary_1",
            "timestamp": "2026-08-17T12:00:00.000Z",
            "message": {
                "model": "claude-opus-5",
                "role": "assistant",
                "content": [{"type": "text", "text": f"I will not repeat {secret}"}],
                "usage": {
                    "input_tokens": 1234,
                    "output_tokens": 567,
                    "cache_read_input_tokens": 89,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 10,
                        "ephemeral_1h_input_tokens": 0,
                    },
                },
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": f"stdout: {secret}"},
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _codex_rollout(path: Path, *, poisoned: bool) -> None:
    """One Codex rollout with a ``token_count`` event and free text around it."""
    secret = CANARY if poisoned else "ordinary text"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "timestamp": "2026-08-17T12:00:00.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "cwd": f"/Users/someone/{secret}"},
        },
        {
            "timestamp": "2026-08-17T12:00:01.000Z",
            "type": "response_item",
            "payload": {"type": "agent_message", "text": f"the password is {secret}"},
        },
        {
            "timestamp": "2026-08-17T12:00:02.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 2000,
                        "cached_input_tokens": 500,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 300,
                        "reasoning_output_tokens": 100,
                    }
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 12.0,
                        "window_minutes": 10080,
                        "resets_at": 1787208585,
                    },
                    "plan_type": "pro",
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _scan_everything(root: Path, *, poisoned: bool) -> tuple[int, list[Path]]:
    """Run both indexers over poisoned corpora. Returns (tokens, files written)."""
    claude_root = root / "claude_projects"
    codex_root = root / "codex_sessions"
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)

    _claude_transcript(claude_root / "proj" / "session.jsonl", poisoned=poisoned)
    _codex_rollout(codex_root / "2026" / "08" / "17" / "rollout-x.jsonl", poisoned=poisoned)

    store = rollup_mod.DailyRollupStore(path=state / "rollups.json", keep_days=30)

    claude = claude_mod.Indexer(
        projects_dir=claude_root,
        state_path=state / "scan_state.json",
        lookback_days=30,
        now=lambda: FIXED_NOW,
    )
    codex = codex_mod.CodexIndexer(
        sessions_dir=codex_root,
        state_path=state / "codex_scan_state.json",
        lookback_days=30,
        now=lambda: FIXED_NOW,
    )

    tokens = 0
    for source in (claude, codex):
        for _ in range(20):  # chunked scanners: drain to completion
            result = source.scan_once()
            deltas = getattr(result, "deltas", ()) or ()
            if deltas:
                store.merge(deltas)
            for delta in deltas:
                models = getattr(delta, "models", None) or {}
                for usage in models.values():
                    tokens += sum(
                        getattr(usage, field, 0) or 0
                        for field in ("input", "output", "cache_read",
                                      "cache_write_5m", "cache_write_1h")
                    )
            if not getattr(result, "files_read", 0):
                break
        commit = getattr(source, "commit_state", None)
        if callable(commit):
            commit()
    store.save()

    written = [p for p in state.rglob("*") if p.is_file()]
    return tokens, written


def test_no_transcript_content_reaches_any_file_the_widget_writes() -> None:
    """The promise: read token counts, never content.

    Fails if the canary survives into rollups, scan state, the dedup file, the
    quota snapshot, or any other artifact — including inside a path, which is
    why the canary is planted in ``cwd`` and ``gitBranch`` too.
    """
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        _tokens, written = _scan_everything(root, poisoned=True)

        assert written, "the indexers wrote nothing at all - the test proves nothing"

        leaked: list[str] = []
        for path in written:
            # The canary must not appear in the CONTENT ...
            try:
                body = path.read_text(errors="replace")
            except OSError:  # pragma: no cover - unreadable artifact
                continue
            if CANARY in body:
                leaked.append(f"{path.name}: content")
            # ... nor in the NAME of anything created.
            if CANARY in str(path):
                leaked.append(f"{path.name}: filename")

        assert not leaked, (
            "transcript content escaped into files the widget writes: "
            + ", ".join(leaked)
        )


def test_the_canary_test_can_actually_fail() -> None:
    """Guard against a vacuous pass.

    If the indexers silently stopped reading records, the canary test above
    would pass for the wrong reason. This asserts the same fixtures still
    produce real token counts, so "no leak" means "read it and discarded the
    text", not "read nothing".
    """
    with tempfile.TemporaryDirectory() as name:
        tokens, written = _scan_everything(Path(name), poisoned=False)
        assert tokens > 0, (
            "the fixtures produced no tokens, so the leak test above would pass "
            "vacuously - fix the fixtures before trusting it"
        )
        assert written, "no state files were written"


# ---------------------------------------------------------------------------
# The second promise: a Codex credential never leaves its file (SPEC-CODEX 6)
# ---------------------------------------------------------------------------
#
# The live per-account source (``codex_accounts.py``) holds something a
# transcript never does: a bearer token with ten days of life on a paid
# account. It is read from ``auth.json``, put in one ``Authorization`` header
# and dropped. The method below is the same blunt one as above — plant a canary
# where the token lives, run the real poll cycle, then read back every byte the
# widget wrote plus every log line and every rendered menu label.
#
# The negative control matters more here than anywhere else in the suite: a
# leak test over a component that never ran would pass beautifully. So the same
# fixtures are run again through a transport that deliberately logs the
# Authorization header, and the leak check must FAIL on it.

TOKEN_CANARY = "SECRET_TOKEN_CANARY_4A7E1_do_not_leak"
"""Planted as both ``access_token`` and ``refresh_token``. Not a plausible
value in any real ``auth.json``, so a substring hit cannot be a coincidence."""

CODEX_ACCOUNT_ID = "acct-canary-0000"

DESKTOP_TOKEN_CANARY = "SECRET_DESKTOP_CANARY_B81F6_do_not_leak"
"""Planted in the ChatGPT app's own ``~/.codex/auth.json`` stand-in (CX-1): as
the access token's signature segment (so the JWT still decodes and the widget
really uses it) and as the refresh token (which must never even be read)."""


def _jwt(payload: dict, signature: str) -> str:
    """A structurally real JWT; ``decode_jwt_claims`` never checks signatures."""

    def segment(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")

    return ".".join([segment({"alg": "none"}), segment(payload), signature])


class _FakeUsageTransport:
    """Answers one usage body; records nothing. See ``_LeakyTransport``."""

    def __init__(self, body: dict) -> None:
        self._body = body

    def get(self, url, headers, timeout):  # noqa: ANN001, ANN201
        from cc_usage_widget.codex_accounts import HttpResponse

        return HttpResponse(200, {}, json.dumps(self._body).encode("utf-8"))


class _LeakyTransport:
    """The negative control: a transport that logs the header it was given.

    This is what a careless debug line looks like, and it is exactly the bug
    the canary test exists to catch. If the assertions below can be satisfied
    while this class is in play, they are not testing anything.
    """

    def __init__(self, inner, log) -> None:  # noqa: ANN001
        self._inner = inner
        self._log = log

    def get(self, url, headers, timeout):  # noqa: ANN001, ANN201
        self._log(f"GET {url} with {dict(headers)}")
        return self._inner.get(url, headers, timeout)


def _usage_body() -> dict:
    """The probed Pro shape (SPEC-CODEX 6): one weekly window at 19 %."""
    return {
        "account_id": CODEX_ACCOUNT_ID,
        "email": "canary@example.test",
        "plan_type": "pro",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": 19,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 3600,
            },
            "secondary_window": None,
        },
        "additional_rate_limits": [],
        "rate_limit_reached_type": None,
    }


def _poll_codex_account(
    root: Path, *, poisoned: bool, leaky: bool = False, desktop: bool = False, before_poll=None
):  # noqa: ANN001
    """One full live-quota cycle over real files. Returns (rows, written, logs).

    ``written`` deliberately EXCLUDES the credential directory: that file is
    where the secret is supposed to live, and it is the one artifact the widget
    never writes (0600, written only by ``codex login``).
    """
    from cc_usage_widget import codex_accounts as accounts_mod

    secret = TOKEN_CANARY if poisoned else "ordinary-token-value"
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    credentials_dir = root / "credentials"
    account_dir = credentials_dir / CODEX_ACCOUNT_ID
    account_dir.mkdir(parents=True, exist_ok=True)
    auth = account_dir / "auth.json"
    widget_access = secret
    if desktop:
        # CX-1 variant: the widget copy is EXPIRED, so the only live token for
        # this account is the app's - the one carrying the desktop canary.
        widget_access = _jwt({"exp": time.time() - 60}, "widget-signature")
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": widget_access,
                    "refresh_token": secret,
                    "id_token": secret,
                    "account_id": CODEX_ACCOUNT_ID,
                },
            }
        )
    )
    auth.chmod(0o600)

    registry_path = state / "codex_accounts.json"
    registry_path.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": [
                    {
                        "account_id": CODEX_ACCOUNT_ID,
                        "alias": "canary",
                        "enabled": True,
                        "order": 0,
                    }
                ],
            }
        )
    )
    mirror = root / "dot-codex" / "auth.json"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    if desktop:
        mirror.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "account_id": CODEX_ACCOUNT_ID,
                        "access_token": _jwt(
                            {"exp": time.time() + 5 * 86_400}, DESKTOP_TOKEN_CANARY
                        ),
                        "refresh_token": DESKTOP_TOKEN_CANARY,
                        "id_token": DESKTOP_TOKEN_CANARY,
                    },
                }
            )
        )
        mirror.chmod(0o600)
    else:
        mirror.write_text(json.dumps({"tokens": {"account_id": CODEX_ACCOUNT_ID}}))

    logs: list[str] = []
    transport = _FakeUsageTransport(_usage_body())
    if leaky:
        transport = _LeakyTransport(transport, logs.append)
    source = accounts_mod.CodexAccountsSource(
        registry=accounts_mod.Registry(registry_path),
        credentials=accounts_mod.CredentialStore(credentials_dir, auth_path=mirror),
        transport=transport,
        snapshots_path=state / "codex_quota_snapshots.json",
        settings=lambda: {
            "codex_live_quota_enabled": True,
            "codex_quota_interval_seconds": 300,
            "codex_show_extra_limits": True,
        },
        log=logs.append,
    )
    if before_poll is not None:
        before_poll(mirror)
    source.run_cycle_once()
    rows = source.quota_rows()
    logs.extend(source.diagnostics())
    written = [p for p in state.rglob("*") if p.is_file()]
    return rows, written, logs


def _codex_leaks(rows, written, logs, canary: str = TOKEN_CANARY) -> list[str]:  # noqa: ANN001
    """Every place the token could have escaped to."""
    from cc_usage_widget import render as render_mod

    leaked: list[str] = []
    for path in written:
        try:
            body = path.read_text(errors="replace")
        except OSError:  # pragma: no cover - unreadable artifact
            continue
        if canary in body:
            leaked.append(f"{path.name}: content")
        if canary in str(path):
            leaked.append(f"{path.name}: filename")
    for line in logs:
        if canary in line:
            leaked.append("log line")
    for row in rows:
        segments = list(
            render_mod.quota_header(row.alias, row.plan_type or "", row.attention_note)
        )
        for window, pct in (("5h", row.five_hour_pct), ("7d", row.seven_day_pct)):
            segments.extend(render_mod.window_line(window, pct))
        label = "".join(text for text, _ in segments) + repr(row)
        if canary in label:
            leaked.append("rendered row")
    return leaked


def test_a_codex_credential_never_leaves_its_file() -> None:
    """The token reaches exactly one place: the Authorization header."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        rows, written, logs = _poll_codex_account(root, poisoned=True)

        auth = root / "credentials" / CODEX_ACCOUNT_ID / "auth.json"
        assert TOKEN_CANARY in auth.read_text(), (
            "the canary was not planted - this test would pass vacuously"
        )
        assert written, "the source wrote nothing at all - the test proves nothing"
        assert logs, "the source logged nothing at all - the log check proves nothing"
        assert rows and rows[0].seven_day_pct == 19.0, (
            "the cycle must actually have completed, or there was nothing to leak"
        )

        leaked = _codex_leaks(rows, written, logs)
        assert not leaked, "the Codex access token escaped into: " + ", ".join(leaked)


def test_the_codex_canary_test_can_actually_fail() -> None:
    """The negative control, and the reason to believe the test above.

    Same fixtures, same assertions, one deliberately careless transport that
    logs the Authorization header. The leak check MUST find it; if it does not,
    the passing test above is decoration.
    """
    with tempfile.TemporaryDirectory() as name:
        rows, written, logs = _poll_codex_account(Path(name), poisoned=True, leaky=True)
        leaked = _codex_leaks(rows, written, logs)
        assert leaked, (
            "a transport that logs the Authorization header went UNDETECTED - "
            "the canary check above is not testing anything"
        )
        assert "log line" in leaked


def test_the_codex_row_still_carries_a_figure_without_the_canary() -> None:
    """The mirror image: 'no leak' must mean 'read it and kept the number',
    not 'the poller quietly did nothing'."""
    with tempfile.TemporaryDirectory() as name:
        rows, written, logs = _poll_codex_account(Path(name), poisoned=False)
        assert rows and rows[0].seven_day_pct == 19.0
        assert rows[0].plan_type == "pro" and rows[0].attention_note == ""
        assert any(p.name == "codex_quota_snapshots.json" for p in written)


def test_the_desktop_login_never_leaves_its_file_and_is_never_written() -> None:
    """CX-1: the widget now reads the ChatGPT app's own login for the account
    it is logged in as. That token reaches one place (the Authorization
    header), its refresh token reaches none, and the file is never written."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        mirror = root / "dot-codex" / "auth.json"
        seen: list[tuple[bytes, int]] = []
        rows, written, logs = _poll_codex_account(
            root, poisoned=False, desktop=True,
            before_poll=lambda path: seen.append((path.read_bytes(), path.stat().st_mtime_ns)),
        )
        assert len(seen) == 1, "the pre-poll snapshot of the mirror was not taken"
        before = seen[0]
        assert DESKTOP_TOKEN_CANARY in mirror.read_text(), "the canary was not planted"
        assert rows and rows[0].seven_day_pct == 19.0, "the desktop token was not used"
        assert "via ChatGPT app login" in rows[0].info_notes, "not the desktop path"
        assert written and logs, "nothing written or logged - the check proves nothing"
        leaked = _codex_leaks(rows, written, logs, DESKTOP_TOKEN_CANARY)
        assert not leaked, "the desktop token escaped into: " + ", ".join(leaked)
        assert (mirror.read_bytes(), mirror.stat().st_mtime_ns) == before


def test_the_desktop_canary_test_can_actually_fail() -> None:
    """Negative control for the test above: a transport that logs the
    Authorization header must be caught carrying the DESKTOP canary - which
    also proves that token, not the expired widget copy, made the request."""
    with tempfile.TemporaryDirectory() as name:
        rows, written, logs = _poll_codex_account(
            Path(name), poisoned=False, leaky=True, desktop=True
        )
        leaked = _codex_leaks(rows, written, logs, DESKTOP_TOKEN_CANARY)
        assert "log line" in leaked, leaked


def _tests() -> list[tuple[str, object]]:
    items = [
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
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
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
