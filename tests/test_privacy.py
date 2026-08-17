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

Nothing here touches ``~/.claude`` or ``~/.codex``; every path is inside a
``TemporaryDirectory``.

Run directly, or with pytest if it is installed::

    python tests/test_privacy.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cc_usage_widget import codex_indexer as codex_mod  # noqa: E402
from cc_usage_widget import indexer as claude_mod  # noqa: E402
from cc_usage_widget import rollup as rollup_mod  # noqa: E402

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
    )
    codex = codex_mod.CodexIndexer(
        sessions_dir=codex_root,
        state_path=state / "codex_scan_state.json",
        lookback_days=30,
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
