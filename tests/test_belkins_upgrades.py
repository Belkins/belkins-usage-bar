"""Comprehensive regression and feature suite for Belkins Usage Bar Waves 1–3 upgrades.

Covers:
  Wave 1 — Pricing (`claude-fable-5-2`), tombstone `inode == 0` round-trip, multi-root
           Claude/Codex indexing, oversize line drain cap, `session_meta` model fallback,
           Codex `5h` secondary window, git worktree attribution collapsing,
           `top_projects_window`, `history.sqlite` quota wall-time tracking, live-repriced
           `daily_project` rows, and HTML Dashboard SVG / Cache Forensics / ROI Scorecard.
  Wave 2 — `_thinking_guard_policy` (`self._rows_cache`, `scoped_windows`, `sentinels`,
           concurrency-scaled ceiling, turn-boundary handoff), `harvest_best_slot`
           (perishable-quota urgency + same-org affinity), `.bswap_switch_marker`
           external-switch suppression, `safe_switch_codex_auth` rotated-token sync +
           untracked backup, `best_codex_row` `5h` awareness, `codex_login` `max(order)+1`
           allocation + stale `new-*` pruning, `fleet` `procStart` PID-reuse guard +
           shared-org `detect_workspace_owner_slot`, and `bswap` CLI (`shim`, `roi`,
           read-only `sessions`, real `_load_companion_state`).
  Wave 3 — `menubar` negative-X multi-monitor bounds + `onscreen_other_count`,
           `render.companion_sprite_icon`, `belkins_game` (8 stage passives, Cache Mastery
           `+15%` XP/Coins, `wall_guard` mood priority, `0o600` shop save, Trophy #8
           `"Flawless Rotator"`), `themes/belkins` (Slim Hero, preserved `"⚡ Best"` pill,
           chevron hit-box, `⚠ ORG SHIFTED` + `Pin`/`Unpin`/`Restore` pills, `TOOLS`
           `keep_open=False`), `notify` (dynamic `notify.json` reload, standing alerts,
           12h expiring-quota reminder, opt-in 2-way Telegram commands with strict
           `chat_id` check & token redaction), and `app` (two-tier notch fallback +
           60s recovery, preserved `⚠` alert badge, pure copy-on-write `_set_setting_from_theme`).

Run directly or with pytest::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    $PY tests/test_belkins_upgrades.py
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import io
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

from cc_usage_widget import accounts as accounts_mod  # noqa: E402
from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import belkins_game  # noqa: E402
from cc_usage_widget import bswap_cli  # noqa: E402
from cc_usage_widget import codex_accounts  # noqa: E402
from cc_usage_widget import codex_login  # noqa: E402
from cc_usage_widget import fleet  # noqa: E402
from cc_usage_widget import indexer as indexer_mod  # noqa: E402
from cc_usage_widget import menubar  # noqa: E402
from cc_usage_widget import notify  # noqa: E402
from cc_usage_widget import render  # noqa: E402
from cc_usage_widget.attribution import (  # noqa: E402
    Attribution,
    AttributionPass,
    AttributionStore,
    claude_scope,
    codex_scope,
)
from cc_usage_widget.codex_indexer import CodexIndexer  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    ALERT_ALL_EXHAUSTED,
    ALERT_UNMANAGED_LOGIN,
    SETTINGS_DEFAULTS,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    AccountRow,
    DayRollup,
    FileScanState,
    LedgerEntry,
    ModelUsage,
    local_day_key,
    normalize_settings,
    scan_state_from_json,
    scan_state_to_json,
)
from cc_usage_widget.dashboard import build_dashboard  # noqa: E402
from cc_usage_widget.history import HistoryStore  # noqa: E402
from cc_usage_widget.indexer import Indexer  # noqa: E402
from cc_usage_widget.pricing import DEFAULT_PRICING, display_name  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402
from cc_usage_widget.state import ScanStateStore  # noqa: E402
from cc_usage_widget.themes import ThemeActions  # noqa: E402
from cc_usage_widget.themes import belkins as belkins_theme  # noqa: E402

FORBIDDEN_SUBSTRINGS = ("<script", "http", "<link", "<img", "@import", "url(")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _arow(
    slot: int,
    alias: str,
    *,
    is_active: bool = False,
    vendor: str = VENDOR_CLAUDE,
    **kwargs: Any,
) -> AccountRow:
    email = kwargs.pop("email", f"{alias}@example.com")
    return AccountRow(
        slot=slot,
        alias=alias,
        email=email,
        is_active=is_active,
        vendor=vendor,  # type: ignore[arg-type]
        **kwargs,
    )


def _fake_jwt(account_id: str, email: str, exp: float) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode("ascii")
    payload_obj = {
        "exp": int(exp),
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        "https://api.openai.com/profile": {"email": email},
    }
    body = (
        base64.urlsafe_b64encode(json.dumps(payload_obj, separators=(",", ":")).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    return f"{header}.{body}.sig"


def _codex_auth_bytes(account_id: str, email: str, exp: float, refresh: str) -> bytes:
    obj = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": _fake_jwt(account_id, email, exp),
            "id_token": _fake_jwt(account_id, email, exp),
            "refresh_token": refresh,
            "account_id": account_id,
        },
        "last_refresh": _iso(exp - 3600),
    }
    return json.dumps(obj).encode("utf-8")


# ---------------------------------------------------------------------------
# Wave 1 Tests
# ---------------------------------------------------------------------------


def test_fable_5_2_pricing_and_display_name() -> None:
    assert DEFAULT_PRICING.is_known("claude-fable-5-2")
    assert DEFAULT_PRICING.is_known("claude-fable-5-2-20261001")
    assert display_name("claude-fable-5-2") == "Fable 5.2"
    usage = ModelUsage(
        input=1_000_000,
        output=1_000_000,
        cache_read=1_000_000,
        cache_write_5m=0,
        cache_write_1h=0,
    )
    # $8.00 input + $40.00 output + $0.20 cache read = $48.20
    usd = float(DEFAULT_PRICING.cost_usd("claude-fable-5-2", usage, day="2026-10-15"))
    assert abs(usd - 48.20) < 1e-6, usd
    # codex-auto-review stays unpriced ($0) by design
    assert not DEFAULT_PRICING.is_known("codex:codex-auto-review")
    assert float(DEFAULT_PRICING.cost_usd("codex:codex-auto-review", usage, day="2026-10-15")) == 0.0


def test_tombstone_inode_zero_survives_scan_state_roundtrip() -> None:
    entry = LedgerEntry(
        day="2026-10-15",
        model="claude-fable-5-2",
        counters=(1000, 500, 0, 0, 0),
    )
    tomb = FileScanState.tombstone((entry,), scope="proj/belkins")
    assert tomb.inode == 0
    assert tomb.is_tombstone

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "scan_state.json"
        store = ScanStateStore(state_path)
        assert store.save_json(scan_state_to_json({"/vanished/session.jsonl": tomb}))
        mode = stat.S_IMODE(state_path.stat().st_mode)
        assert mode == 0o600, oct(mode)
        reloaded = scan_state_from_json(store.load_json())
        assert "/vanished/session.jsonl" in reloaded, reloaded
        restored = reloaded["/vanished/session.jsonl"]
        assert restored.is_tombstone
        assert restored.inode == 0
        assert restored.ledger == (entry,)


def test_claude_indexer_multi_root_and_oversize_line_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        primary = root / "primary_projects"
        extra = root / "profile_projects"
        primary.mkdir()
        extra.mkdir()
        now = time.time()

        def _rec(req_id: str, out_tok: int) -> str:
            return json.dumps(
                {
                    "type": "assistant",
                    "requestId": req_id,
                    "timestamp": _iso(now),
                    "message": {
                        "id": f"msg_{req_id}",
                        "role": "assistant",
                        "model": "claude-fable-5-2",
                        "usage": {"input_tokens": 10, "output_tokens": out_tok},
                    },
                }
            )

        p_file = primary / "proj-a" / "s1.jsonl"
        p_file.parent.mkdir(parents=True)
        p_file.write_text(
            json.dumps({"type": "user", "blob": "x" * 4096}) + "\n" + _rec("r1", 100) + "\n",
            encoding="utf-8",
        )
        dup_file = extra / "proj-a" / "s1.jsonl"
        dup_file.parent.mkdir(parents=True)
        dup_file.write_text(_rec("r_dup", 999) + "\n", encoding="utf-8")

        iso_file = extra / "proj-isolated" / "s2.jsonl"
        iso_file.parent.mkdir(parents=True)
        iso_file.write_text(_rec("r2", 250) + "\n", encoding="utf-8")

        orig_cap = indexer_mod._MAX_LINE_BYTES
        try:
            indexer_mod._MAX_LINE_BYTES = 1024
            idx = Indexer(
                projects_dir=primary,
                extra_roots=(extra,),
                state_path=root / "scan_state.json",
                pricing=DEFAULT_PRICING,
            )
            res = idx.scan_once()
        finally:
            indexer_mod._MAX_LINE_BYTES = orig_cap

        assert not res.errors, res.errors
        assert res.records_counted == 2, res
        total_out = sum(u.output for d in res.deltas for u in d.models.values())
        assert total_out == 350, total_out


def test_codex_indexer_multi_root_session_meta_and_5h_window() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        primary = root / "sessions"
        archived = root / "archived_sessions"
        acct_sess = root / "codex-accounts" / "acct-1" / "sessions"
        now = time.time()
        today = dt.date.today()

        def _write_rollout(base: Path, name: str, lines: list[dict[str, Any]]) -> None:
            d = base / f"{today.year:04d}" / f"{today.month:02d}" / f"{today.day:02d}"
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(
                "\n".join(json.dumps(x) for x in lines) + "\n",
                encoding="utf-8",
            )

        _write_rollout(
            archived,
            f"rollout-{today.isoformat()}T10-00-00-arch.jsonl",
            [
                {
                    "timestamp": _iso(now - 60),
                    "type": "session_meta",
                    "payload": {"model": "gpt-6-astra", "cwd": "/work/alpha"},
                },
                {
                    "timestamp": _iso(now - 30),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"input_tokens": 500},
                            "last_token_usage": {
                                "input_tokens": 500,
                                "cached_input_tokens": 100,
                                "output_tokens": 50,
                            },
                        },
                        "rate_limits": {
                            "primary": {
                                "used_percent": 42.0,
                                "window_minutes": 10080,
                                "resets_at": int(now + 86400),
                            },
                            "secondary": {
                                "used_percent": 68.0,
                                "window_minutes": 300,
                                "resets_at": int(now + 7200),
                            },
                        },
                    },
                },
            ],
        )
        _write_rollout(
            acct_sess,
            f"rollout-{today.isoformat()}T11-00-00-acct1.jsonl",
            [
                {
                    "timestamp": _iso(now - 20),
                    "type": "turn_context",
                    "payload": {"model": "gpt-6-astra", "cwd": "/work/beta"},
                },
                {
                    "timestamp": _iso(now - 10),
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"input_tokens": 300},
                            "last_token_usage": {
                                "input_tokens": 300,
                                "cached_input_tokens": 0,
                                "output_tokens": 25,
                            },
                        },
                    },
                },
            ],
        )

        cidx = CodexIndexer(
            sessions_dir=primary,
            extra_dirs=(archived, acct_sess),
            state_path=root / "codex_scan_state.json",
            pricing=DEFAULT_PRICING,
        )
        res = cidx.scan_once()
        assert res.records_counted == 2, res
        models = {m for d in res.deltas for m in d.models}
        assert "codex:gpt-6-astra" in models, models

        qrows = cidx.quota_rows()
        assert len(qrows) == 1, qrows
        qrow = qrows[0]
        assert qrow.seven_day_pct == 42.0
        assert qrow.five_hour_pct == 68.0
        assert qrow.five_hour_resets_at is not None


def test_attribution_worktree_collapsing_and_top_projects_window() -> None:
    s1 = claude_scope("/tmp/p/s1.jsonl", "/Users/dev/belkins-crm/.claude/worktrees/feat-auth")
    s2 = codex_scope("/tmp/r1.jsonl", "/Users/dev/belkins-crm/.codex/worktrees/hotfix-2")
    s3 = claude_scope("/tmp/p/s2.jsonl", "/Users/dev/belkins-crm/.worktrees/agent-3")
    assert s1.project == "belkins-crm", s1
    assert s2.project == "belkins-crm", s2
    assert s3.project == "belkins-crm", s3

    with tempfile.TemporaryDirectory() as tmp:
        astore = AttributionStore(path=Path(tmp) / "attribution.json", keep_days=30)
        today = local_day_key(time.time())
        yesterday = local_day_key(time.time() - 86400)
        scope_a = Attribution(vendor=VENDOR_CLAUDE, project="belkins-crm", session="s1")
        scope_b = Attribution(vendor=VENDOR_CLAUDE, project="usagebar", session="s2")
        p = AttributionPass(
            adds=(
                (
                    scope_a,
                    DayRollup(
                        day=today,
                        models={"claude-fable-5-2": ModelUsage(input=100_000, output=20_000)},
                    ),
                ),
                (
                    scope_b,
                    DayRollup(
                        day=yesterday,
                        models={"claude-fable-5-2": ModelUsage(input=300_000, output=50_000)},
                    ),
                ),
            )
        )
        astore.apply(p)
        win_rows = astore.top_projects_window(7, today=today, pricing=DEFAULT_PRICING, limit=5)
        names = [r.project for r in win_rows]
        assert names == ["usagebar", "belkins-crm"], names


def test_history_quota_samples_wall_hours_and_dashboard_repricing_and_roi() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hpath = root / "history.sqlite"
        hstore = HistoryStore(hpath)
        now = 1_760_000_000.0
        today = local_day_key(now)

        w_row = _arow(1, "work", is_active=True, five_hour_pct=100.0, seven_day_pct=40.0)
        free_row = replace(w_row, five_hour_pct=12.0)
        # 8 distinct 15-minute buckets at 100% cap = 2.00 wall hours; duplicate in same bucket overwrites cleanly
        for i in range(8):
            assert hstore.record_quota_samples((w_row,), now=now - (i + 1) * 900) == 1
        assert hstore.record_quota_samples((w_row,), now=now - 900 + 30) == 1
        assert hstore.record_quota_samples((free_row,), now=now) == 1

        wall_hrs = hstore.wall_hours_by_account(since_epoch=now - 86400)
        assert ("claude", "work") in wall_hrs, wall_hrs
        assert abs(wall_hrs[("claude", "work")] - 2.0) < 0.05, wall_hrs

        rollup = DayRollup(
            day=today,
            models={
                "claude-fable-5-2": ModelUsage(
                    input=1_000_000,
                    output=500_000,
                    cache_read=4_000_000,
                    cache_write_5m=200_000,
                    cache_write_1h=800_000,
                )
            },
        )
        hstore.upsert_days((rollup,), DEFAULT_PRICING)
        proj_rollup = DayRollup(
            day=today,
            models={"claude-fable-5-2": ModelUsage(input=1_000_000, output=500_000)},
        )
        hstore.upsert_projects([(today, "belkins-app", proj_rollup)], None)

        rstore = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        rstore.merge((rollup,))

        out_path = build_dashboard(
            history=hstore,
            rollups=rstore,
            quota_rows=(),
            accounts=(free_row,),
            now=now,
            pricing=DEFAULT_PRICING,
            path=root / "dashboard.html",
            monthly_subscription_usd=200.0,
        )
        html = out_path.read_text(encoding="utf-8")
        lowered = html.lower()
        for bad in FORBIDDEN_SUBSTRINGS:
            assert bad not in lowered, f"forbidden substring {bad!r} in dashboard HTML"
        assert "<svg" in html
        assert "cache" in lowered
        assert "ROI" in html
        assert "belkins-app" in html
        assert "$28.00" in html  # 1M * $8 + 0.5M * $40 = $28.00 live-repriced!


# ---------------------------------------------------------------------------
# Wave 2 Tests
# ---------------------------------------------------------------------------


def test_thinking_guard_reads_rows_cache_scoped_windows_sentinels_and_handoff() -> None:
    import dataclasses

    @dataclasses.dataclass
    class _FakePolicy:
        threshold: float = 85.0
        weekly_threshold: float | None = 85.0
        enabled: bool = True
        strategy: str = "best"

    src = accounts_mod.SwapAccountSource.__new__(accounts_mod.SwapAccountSource)
    src._lock = threading.Lock()
    src._settings_obj = {"belkins_thinking_guard": True}
    src._handoff_armed = False
    src._snapshot = None
    src._sentinels = {}
    src._log = accounts_mod.LOGGER
    src._rows_cache = (
        _arow(
            1,
            "active-seat",
            is_active=True,
            five_hour_pct=60.0,
            seven_day_pct=70.0,
            scoped_windows=(("Fable", 91.0),),
        ),
        _arow(
            2,
            "standby-seat",
            is_active=False,
            five_hour_pct=10.0,
            seven_day_pct=20.0,
        ),
    )

    orig_policy = _FakePolicy(threshold=85.0, weekly_threshold=85.0)
    engine = SimpleNamespace(_policy=orig_policy)

    with tempfile.TemporaryDirectory() as sess_tmp:
        orig_env = os.environ.get("CC_USAGE_WIDGET_SESSIONS_DIR")
        orig_read = fleet.read_registry
        try:
            os.environ["CC_USAGE_WIDGET_SESSIONS_DIR"] = sess_tmp
            # 1 busy session -> busy_5h_cap = 95.0, busy_7d_cap = 96.5; Fable is 91.0 >= 85.0
            # -> Guard holds the switch, raises thresholds, and arms turn-boundary handoff!
            fleet.read_registry = lambda _d: ([{"status": "busy"}], 0)
            guarded = src._thinking_guard_policy(engine, orig_policy)
            assert guarded is not None
            assert guarded.threshold == 95.0, guarded
            assert guarded.weekly_threshold == 96.5, guarded
            assert src.has_armed_handoff() is True

            # 6 busy sessions -> busy_7d_cap = max(90.0, 98.0 - 9.0) = 90.0;
            # Fable at 93.0 >= 90.0 -> emergency cap lets auto-switch proceed immediately!
            src._rows_cache = (
                replace(src._rows_cache[0], scoped_windows=(("Fable", 93.0),)),
                src._rows_cache[1],
            )
            fleet.read_registry = lambda _d: ([{"status": "busy"} for _ in range(6)], 0)
            assert src._thinking_guard_policy(engine, orig_policy) is None
            assert src.has_armed_handoff() is False

            # Sentinel on active slot bypasses busy-session hold immediately
            src._rows_cache = (
                replace(src._rows_cache[0], scoped_windows=(("Fable", 86.0),)),
                src._rows_cache[1],
            )
            src._sentinels = {1: "403 forbidden"}
            fleet.read_registry = lambda _d: ([{"status": "busy"}], 0)
            res_sent = src._thinking_guard_policy(engine, orig_policy)
            assert res_sent is None
            assert src.has_armed_handoff() is False
        finally:
            fleet.read_registry = orig_read
            if orig_env is None:
                os.environ.pop("CC_USAGE_WIDGET_SESSIONS_DIR", None)
            else:
                os.environ["CC_USAGE_WIDGET_SESSIONS_DIR"] = orig_env


def test_perishable_quota_harvester_and_same_org_affinity() -> None:
    now = 1_760_000_000.0
    soon_iso = _iso(now + 6 * 3600)
    far_iso = _iso(now + 6 * 86400)

    cold_seat = _arow(
        1, "cold", five_hour_pct=15.0, seven_day_pct=20.0, seven_day_resets_at=far_iso
    )
    expiring_seat = _arow(
        2, "expiring", five_hour_pct=20.0, seven_day_pct=35.0, seven_day_resets_at=soon_iso
    )
    fable_walled_seat = _arow(
        3,
        "fable-wall",
        five_hour_pct=0.0,
        seven_day_pct=10.0,
        scoped_windows=(("Fable", 100.0),),
        seven_day_resets_at=soon_iso,
    )

    picked = accounts_mod.harvest_best_slot(
        (cold_seat, expiring_seat, fable_walled_seat),
        models=("fable",),
        now=now,
    )
    assert picked is not None and picked.slot == 2, picked

    seat_org_a = _arow(4, "org-a", five_hour_pct=25.0, seven_day_pct=30.0)
    seat_org_b = _arow(5, "org-b", five_hour_pct=20.0, seven_day_pct=30.0)
    picked_org = accounts_mod.harvest_best_slot(
        (seat_org_a, seat_org_b),
        identities={4: ("a@x.com", "org-uuid-alpha"), 5: ("b@x.com", "org-uuid-beta")},
        prefer_org="org-uuid-alpha",
        now=now,
    )
    assert picked_org is not None and picked_org.slot == 4, picked_org


def test_bswap_switch_marker_suppresses_false_external_switch_alert() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        backup_dir = Path(tmp)
        accounts_mod.record_cli_switch_marker(backup_dir, target="3")
        marker = backup_dir / accounts_mod._CLI_SWITCH_MARKER_NAME
        assert marker.is_file()

        src = accounts_mod.SwapAccountSource.__new__(accounts_mod.SwapAccountSource)
        src._backend_or_none = lambda: SimpleNamespace(backup_dir=backup_dir)
        src._wall = time.time
        assert src._consume_cli_switch_marker("3") is True
        assert not marker.exists(), "marker must be consumed once"


def test_safe_switch_codex_auth_syncs_rotated_token_and_backs_up_untracked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        accts_dir = root / "codex-accounts"
        auth_path = root / ".codex" / "auth.json"
        now = time.time()

        (accts_dir / "acct-1").mkdir(parents=True)
        (accts_dir / "acct-2").mkdir(parents=True)
        old_acct1 = _codex_auth_bytes("acct-1", "one@example.com", now + 3600, "rt_old_1")
        rotated_acct1 = _codex_auth_bytes("acct-1", "one@example.com", now + 86400, "rt_rotated_1")
        acct2_bytes = _codex_auth_bytes("acct-2", "two@example.com", now + 86400, "rt_2")

        (accts_dir / "acct-1" / "auth.json").write_bytes(old_acct1)
        os.chmod(accts_dir / "acct-1" / "auth.json", 0o600)
        (accts_dir / "acct-2" / "auth.json").write_bytes(acct2_bytes)
        os.chmod(accts_dir / "acct-2" / "auth.json", 0o600)

        auth_path.parent.mkdir(parents=True)
        auth_path.write_bytes(rotated_acct1)
        os.chmod(auth_path, 0o600)

        ok, msg = codex_accounts.safe_switch_codex_auth(
            "acct-2", accounts_dir=accts_dir, auth_path=auth_path
        )
        assert ok, msg
        saved_acct1 = json.loads((accts_dir / "acct-1" / "auth.json").read_text("utf-8"))
        assert saved_acct1["tokens"]["refresh_token"] == "rt_rotated_1"
        assert auth_path.read_bytes() == acct2_bytes
        assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600

        untracked = _codex_auth_bytes("untracked99", "new@example.com", now + 86400, "rt_new")
        auth_path.write_bytes(untracked)
        ok2, msg2 = codex_accounts.safe_switch_codex_auth(
            "acct-1", accounts_dir=accts_dir, auth_path=auth_path
        )
        assert ok2, msg2
        backup_file = accts_dir / "untracked-untracked99" / "auth.json"
        assert backup_file.is_file(), "untracked login must be backed up before overwrite"
        assert backup_file.read_bytes() == untracked

        r_5h_full = _arow(
            -1,
            "busy-5h",
            is_active=True,
            vendor=VENDOR_CODEX,
            five_hour_pct=99.0,
            seven_day_pct=20.0,
            scoped_windows=(("5h", 99.0),),
        )
        r_clean = _arow(
            -2,
            "clean",
            is_active=False,
            vendor=VENDOR_CODEX,
            five_hour_pct=10.0,
            seven_day_pct=30.0,
            scoped_windows=(("5h", 10.0),),
        )
        best = app_mod.best_codex_row((r_5h_full, r_clean))
        assert best is not None and best.alias == "clean", best


def test_codex_login_order_allocation_and_abandoned_dir_pruning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        accts_dir = root / "codex-accounts"
        reg_path = root / "codex_accounts.json"
        now = time.time()

        registry = codex_accounts.Registry(reg_path)
        registry.upsert(
            codex_accounts.RegistryEntry(account_id="acct-a", alias="a", enabled=True, order=0)
        )
        registry.upsert(
            codex_accounts.RegistryEntry(account_id="acct-b", alias="b", enabled=True, order=5)
        )

        stale_dir = accts_dir / "new-stale"
        stale_dir.mkdir(parents=True)
        os.utime(stale_dir, (now - 86400 * 2, now - 86400 * 2))

        fresh_dir = accts_dir / "new-fresh"
        fresh_dir.mkdir(parents=True)
        (fresh_dir / "auth.json").write_bytes(
            _codex_auth_bytes("acct-c", "c@example.com", now + 86400, "rt_c")
        )
        os.chmod(fresh_dir / "auth.json", 0o600)

        codex_login._prune_old_commands(accts_dir, now)
        assert not stale_dir.exists(), "abandoned empty new-* directory should be pruned"

        report = codex_login.adopt_pending(
            accts_dir, registry, replace=False, clock=lambda: now
        )
        assert report.ok, report
        orders = {entry.account_id: entry.order for entry in registry.entries()}
        assert orders["acct-c"] == 6, f"expected max(order)+1 == 6, got {orders}"


def test_fleet_pid_reuse_guard_and_workspace_org_shift_detection() -> None:
    fleet._PID_MATCH_CACHE.clear()
    # PID 1 (launchd) is not a claude process and started before year 2001? No, PID 1 started AFTER 2001!
    # A recorded procStart of "Mon Jan  1 00:00:00 2001" is older than PID 1's start time, and PID 1 is launchd (not claude)!
    matched = fleet._pid_matches_proc_start(1, "Mon Jan  1 00:00:00 2001")
    assert matched is False

    with tempfile.TemporaryDirectory() as tmp:
        proj_root = Path(tmp) / "projects"
        cwd = "/Users/dev/AI Products/usagebar"
        slug = re.sub(r"[^a-zA-Z0-9-]", "-", cwd)
        pdir = proj_root / slug
        pdir.mkdir(parents=True)
        (pdir / "sess-1.jsonl").write_text(
            json.dumps({"type": "system", "organizationUuid": "org-shared-1"}) + "\n",
            encoding="utf-8",
        )
        identities = {
            1: ("seat1@belkins.io", "org-shared-1"),
            2: ("seat2@belkins.io", "org-shared-1"),
            3: ("personal@gmail.com", "org-other-9"),
        }
        owner_same, shifted_same = fleet.detect_workspace_owner_slot(
            cwd, identities, current_slot=2, session_id="sess-1", projects_root=proj_root
        )
        assert owner_same == 2 and shifted_same is False, (owner_same, shifted_same)

        owner_diff, shifted_diff = fleet.detect_workspace_owner_slot(
            cwd, identities, current_slot=3, session_id="sess-1", projects_root=proj_root
        )
        assert owner_diff == 1 and shifted_diff is True, (owner_diff, shifted_diff)


def test_bswap_cli_shim_and_roi_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shims_dir = root / "shims"
        today = local_day_key(time.time())
        rstore = DailyRollupStore(path=root / "rollups.json", keep_days=30)
        rstore.merge(
            (
                DayRollup(
                    day=today,
                    models={"claude-fable-5-2": ModelUsage(input=2_000_000, output=1_000_000)},
                ),
            ),
        )
        rstore.save()

        orig_widget_dir = bswap_cli.WIDGET_DIR
        orig_shims_dir = bswap_cli.SHIMS_DIR
        try:
            bswap_cli.WIDGET_DIR = root
            bswap_cli.SHIMS_DIR = shims_dir

            buf_shim = io.StringIO()
            with contextlib.redirect_stdout(buf_shim):
                rc_on = bswap_cli.main(["shim", "on"])
            assert rc_on == 0
            assert (shims_dir / "claude").is_file()
            assert (shims_dir / "codex").is_file()

            buf_roi = io.StringIO()
            with contextlib.redirect_stdout(buf_roi):
                rc_roi = bswap_cli.main(["roi", "200"])
            assert rc_roi == 0
            assert "ROI" in buf_roi.getvalue()
        finally:
            bswap_cli.WIDGET_DIR = orig_widget_dir
            bswap_cli.SHIMS_DIR = orig_shims_dir


# ---------------------------------------------------------------------------
# Wave 3 Tests
# ---------------------------------------------------------------------------


def test_menubar_negative_x_secondary_monitor_and_other_count() -> None:
    fake_rows = [
        {
            "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
            "kCGWindowNumber": 101,
            "kCGWindowOwnerPID": 99991,
            "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": -1400.0, "Y": 0.0, "Width": 30.0, "Height": 24.0},
        },
        {
            "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
            "kCGWindowNumber": 102,
            "kCGWindowOwnerPID": 99992,
            "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": -1360.0, "Y": 0.0, "Width": 30.0, "Height": 24.0},
        },
        {
            "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
            "kCGWindowNumber": 103,
            "kCGWindowOwnerPID": 99993,
            "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": -1320.0, "Y": 0.0, "Width": 30.0, "Height": 24.0},
        },
        {
            "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
            "kCGWindowNumber": 555,
            "kCGWindowOwnerPID": os.getpid(),
            "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": -1200.0, "Y": 0.0, "Width": 80.0, "Height": 24.0},
        },
    ]
    fake_win = SimpleNamespace(
        frame=lambda: SimpleNamespace(
            origin=SimpleNamespace(x=-1200.0, y=0.0),
            size=SimpleNamespace(width=80.0, height=24.0),
        ),
        occlusionState=lambda: 2,
    )
    orig_wl = menubar._window_list
    try:
        menubar._window_list = lambda: (lambda _opt, _rel: fake_rows)
        assert menubar.own_window_hidden(555, win=fake_win) is False
        assert menubar.onscreen_other_count(555) == 3
    finally:
        menubar._window_list = orig_wl


def test_companion_sprite_icon_renders_without_importerror() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig_path = belkins_game._shop_save_path
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            _ = render.companion_sprite_icon(stage_num=3, mono=False)
        finally:
            belkins_game._shop_save_path = orig_path


def test_belkins_game_stage_passives_cache_mastery_mood_and_trophy_8() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        shop_file = Path(tmp) / "belkins_shop.json"
        orig_path = belkins_game._shop_save_path
        try:
            belkins_game._shop_save_path = lambda: shop_file
            belkins_game._SAVE_CACHE_SIG = None
            assert belkins_game.save_game_state({"owned": ["visor"], "equipped": ["visor"]})
            assert stat.S_IMODE(shop_file.stat().st_mode) == 0o600

            rows = tuple(
                _arow(i, f"s{i}", is_active=(i == 1), five_hour_pct=20.0, seven_day_pct=25.0)
                for i in range(1, 5)
            )
            base_comp = belkins_game.compute_companion(
                tokens_today=10_000_000,
                tokens_30d=100_000_000,
                usd_today=50.0,
                usd_30d=500.0,
                accounts=rows,
                cache_hit_pct=40.0,
            )
            mastery_comp = belkins_game.compute_companion(
                tokens_today=10_000_000,
                tokens_30d=100_000_000,
                usd_today=50.0,
                usd_30d=500.0,
                accounts=rows,
                cache_hit_pct=92.0,
            )
            assert mastery_comp.cache_mastery_active is True
            assert mastery_comp.xp_total > base_comp.xp_total
            assert mastery_comp.coins_earned > base_comp.coins_earned

            belkins_game.save_game_state(
                {"owned": ["context_berry"], "equipped": ["context_berry"]}
            )
            hot_rows = (replace(rows[0], five_hour_pct=90.0), rows[1])
            hot_comp = belkins_game.compute_companion(
                tokens_today=1_000_000,
                tokens_30d=5_000_000,
                accounts=hot_rows,
                threshold_pct=85.0,
            )
            assert hot_comp.mood_key == "wall_guard", hot_comp.mood_key

            events = tuple(f"autoswitch -> slot {i}" for i in range(16))
            rot_comp = belkins_game.compute_companion(
                tokens_today=1_000_000,
                tokens_30d=5_000_000,
                accounts=rows,
                recent_events=events,
                account_notes={},
            )
            flawless = next(b for b in rot_comp.badges if b.slug == "zero_wall")
            assert flawless.unlocked is True, flawless

            for stage_num in range(1, 9):
                belkins_game.save_game_state({"selected_stage": stage_num})
                st_comp = belkins_game.compute_companion(
                    tokens_today=5_000_000_000,
                    tokens_30d=500_000_000_000,
                    usd_today=5_000.0,
                    usd_30d=250_000.0,
                    accounts=rows,
                    active_sessions=3,
                    distinct_models_count=5,
                )
                assert st_comp.stage.stage == stage_num
        finally:
            belkins_game._shop_save_path = orig_path


def test_belkins_theme_slim_hero_best_pill_chevron_and_org_shifted_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig_path = belkins_game._shop_save_path
        try:
            belkins_game._shop_save_path = lambda: Path(tmp) / "belkins_shop.json"
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_theme._LIVE_TAB = "hub"

            rows = (
                _arow(1, "main", is_active=True, five_hour_pct=45.0, seven_day_pct=30.0),
                _arow(2, "backup", is_active=False, five_hour_pct=5.0, seven_day_pct=10.0),
            )
            calls: list[tuple[str, Any]] = []
            actions = ThemeActions(
                switch_to=lambda s: calls.append(("switch_to", s)),
                switch_best=lambda: calls.append(("switch_best", None)),
                toggle_autoswitch=lambda: calls.append(("toggle_autoswitch", None)),
                set_autoswitch_threshold=lambda pct: calls.append(("threshold", pct)),
                pin_session_dir=lambda cwd, slot: calls.append(("pin", (cwd, slot))),
                unpin_session_dir=lambda cwd: calls.append(("unpin", cwd)),
            )

            now_ts = time.time()
            snap_full = app_mod.UiSnapshot(
                settings=normalize_settings(
                    {**SETTINGS_DEFAULTS, "menu_theme": "belkins", "belkins_slim_hero": False}
                ),
                accounts=rows,
                active=rows[0],
                autoswitch_enabled=True,
                autoswitch_threshold=90.0,
            )
            blocks_full = belkins_theme.build(snap_full, now_ts, actions)
            hero_full = next(b for b in blocks_full if b.key == "belkins-companion-hero")

            snap_slim = replace(
                snap_full,
                settings=normalize_settings(
                    {**SETTINGS_DEFAULTS, "menu_theme": "belkins", "belkins_slim_hero": True}
                ),
            )
            blocks_slim = belkins_theme.build(snap_slim, now_ts, actions)
            hero_slim = next(b for b in blocks_slim if b.key == "belkins-companion-hero")
            assert hero_slim.height < hero_full.height, (hero_slim.height, hero_full.height)

            class _RecMeasure(belkins_theme._Measure):
                def __init__(self) -> None:
                    super().__init__()
                    self.texts: list[str] = []

                def text(self, s: str, *a: Any, **k: Any) -> float:
                    if s:
                        self.texts.append(s)
                    return super().text(s, *a, **k)

                def chip(self, text_s: str, *a: Any, **k: Any) -> float:
                    if text_s:
                        self.texts.append(text_s)
                    return super().chip(text_s, *a, **k)

                def pill(self, text_s: str, *a: Any, **k: Any) -> float:
                    if text_s:
                        self.texts.append(text_s)
                    return super().pill(text_s, *a, **k)

            shifted_sess = fleet.SessionRow(
                pid=4242,
                cwd="/Users/dev/belkins-repo",
                status="busy",
                slot=1,
                pinned=False,
                owner_slot=2,
                org_shifted=True,
            )
            snap_claude = replace(
                snap_full,
                sessions=(shifted_sess,),
            )
            blocks_claude = belkins_theme.build(snap_claude, now_ts, actions)
            rec = _RecMeasure()
            for b in blocks_claude:
                if b.paint is not None:
                    b.paint(rec, belkins_theme.W, b.height)
            texts = rec.texts
            assert any("Best" in t for t in texts), texts
            assert any("Auto 90%" in t for t in texts), texts
            assert any("ORG SHIFTED" in t for t in texts), texts
            assert any("Restore #2" in t for t in texts), texts

            tools_blocks = [
                b for b in blocks_claude if b.key == "belkins-cfg-actions-bar"
            ]
            assert tools_blocks and tools_blocks[0].keep_open is False
        finally:
            belkins_theme._LIVE_TAB = "hub"
            belkins_theme._LIVE_SETTINGS.clear()
            belkins_game._shop_save_path = orig_path


def test_notify_dynamic_reload_standing_alerts_expiring_quota_and_telegram_commands() -> None:
    now = 1_760_000_000.0
    soon_reset = _iso(now + 6 * 3600)

    row1 = _arow(
        1,
        "seat-1",
        is_active=True,
        five_hour_pct=10.0,
        seven_day_pct=40.0,
        seven_day_resets_at=soon_reset,
    )
    settings_on = normalize_settings(
        {
            **SETTINGS_DEFAULTS,
            "notifications_enabled": True,
            "expiring_quota_alert_enabled": True,
            "telegram_notifications_enabled": True,
            "telegram_commands_enabled": True,
        }
    )
    prev_row1 = replace(row1, seven_day_resets_at=_iso(now + 48 * 3600))
    prev_snap = SimpleNamespace(
        settings=settings_on,
        accounts=(prev_row1,),
        quota_rows=(),
        alert=None,
    )
    curr_snap = SimpleNamespace(
        settings=settings_on,
        accounts=(row1,),
        active=row1,
        quota_rows=(),
        alert=(ALERT_ALL_EXHAUSTED, "all accounts exhausted · soonest 14:00"),
    )
    events = notify.detect(prev_snap, curr_snap, now=now)
    keys = {e.key for e in events}
    assert "alert:all_exhausted" in keys, keys
    assert any(k.startswith("expiring-quota:") for k in keys), keys

    unmanaged_snap = SimpleNamespace(
        settings=settings_on,
        accounts=(row1,),
        active=row1,
        quota_rows=(),
        alert=(ALERT_UNMANAGED_LOGIN, "newuser@belkins.io isn't in the pool"),
    )
    events_unmanaged = notify.detect(prev_snap, unmanaged_snap, now=now)
    assert "alert:unmanaged_login" in {e.key for e in events_unmanaged}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cred_path = root / "notify.json"
        logs: list[str] = []
        cur_settings = dict(settings_on)

        notifier = notify.Notifier(
            ledger_path=root / "notify_state.json",
            settings=lambda: cur_settings,
            credentials_path=cred_path,
            mac_sender=SimpleNamespace(send=lambda _e: True),
            log=logs.append,
        )
        assert notifier._telegram() is None

        secret_tok = "123456789:AAH_SECRET_CANARY_TOKEN_XYZ"
        cred_path.write_text(
            json.dumps({"token": secret_tok, "chat_id": "777000"}),
            encoding="utf-8",
        )
        os.chmod(cred_path, 0o600)
        assert notifier._telegram() is not None

        cur_settings["telegram_commands_enabled"] = False
        called_urls: list[str] = []

        def _forbid_opener(req: Any, timeout: float = 4.0) -> Any:
            called_urls.append(req.full_url)
            raise AssertionError("network called while telegram_commands_enabled=False")

        assert notifier.poll_telegram_commands(curr_snap, opener=_forbid_opener) == 0
        assert not called_urls

        cur_settings["telegram_commands_enabled"] = True
        sent_bodies: list[dict[str, Any]] = []
        best_called = 0

        def _handle_best(_arg: str) -> str:
            nonlocal best_called
            best_called += 1
            return "Switched to #2 (backup)"

        class _Resp:
            def __init__(self, payload: dict[str, Any]) -> None:
                self._raw = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self._raw

            def close(self) -> None:
                pass

        def _fake_opener(req: Any, timeout: float = 4.0) -> Any:
            if "/getUpdates" in req.full_url:
                return _Resp(
                    {
                        "ok": True,
                        "result": [
                            {
                                "update_id": 10,
                                "message": {"chat": {"id": 999999}, "text": "/best"},
                            },
                            {
                                "update_id": 11,
                                "message": {"chat": {"id": 777000}, "text": "/best"},
                            },
                        ],
                    }
                )
            if "/sendMessage" in req.full_url:
                import urllib.parse

                parsed = {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(req.data.decode("utf-8")).items()
                }
                sent_bodies.append(parsed)
                return _Resp({"ok": True, "result": {}})
            raise RuntimeError(f"unexpected url carrying {secret_tok}")

        handled = notifier.poll_telegram_commands(
            curr_snap, handlers={"best": _handle_best}, opener=_fake_opener
        )
        assert handled == 1
        assert best_called == 1
        assert len(sent_bodies) == 1
        assert sent_bodies[0]["chat_id"] == "777000"
        assert "Switched to #2" in sent_bodies[0]["text"]
        assert all(secret_tok not in line for line in logs), logs


def test_app_two_tier_notch_fallback_alert_badge_and_pure_set_setting() -> None:
    app = app_mod.CCUsageWidgetApp()
    try:
        app._worker.submit = lambda *_a, **_kw: None
        app.rebuild_menu = lambda *_a, **_kw: None

        # 1. Pure copy-on-write _set_setting_from_theme never mutates old snapshot.settings
        orig_settings = app._snapshot.settings
        orig_slim = orig_settings.get("belkins_slim_hero", False)
        app._set_setting_from_theme("belkins_slim_hero", not orig_slim)
        assert orig_settings.get("belkins_slim_hero", False) == orig_slim
        assert app._snapshot.settings["belkins_slim_hero"] == (not orig_slim)

        # 2. Two-tier notch fallback: 2 hidden readings -> _auto_compact
        #    -> 15s settling -> 2 hidden readings -> _auto_icon_only
        #    -> live WindowServer probe after >=60s recovers cleanly!
        app._auto_compact = False
        app._auto_icon_only = False
        app._title_hidden_streak = 0
        app._title_fit_checked_at = 0.0
        app._auto_compact_since = 0.0

        clock = [1000.0]
        hidden_state = [True]
        other_cnt: list[int | None] = [8]

        orig_mono = app_mod.time.monotonic
        orig_own_hidden = menubar.own_window_hidden
        orig_other_cnt = menubar.onscreen_other_count
        try:
            app_mod.time.monotonic = lambda: clock[0]
            menubar.own_window_hidden = lambda _wn, win=None: hidden_state[0]
            menubar.onscreen_other_count = lambda _wn=None: other_cnt[0]

            # Reading 1 + Reading 2 -> Tier 1 (_auto_compact = True)
            app._check_title_fits()
            assert app._auto_compact is False
            clock[0] = 1020.0
            app._check_title_fits()
            assert app._auto_compact is True
            assert app._auto_icon_only is False

            # Within 15s settling window -> stays Tier 1
            clock[0] = 1030.0
            app._check_title_fits()
            assert app._auto_icon_only is False

            # Past 15s settling window: Reading 1 + Reading 2 -> Tier 2 (_auto_icon_only = True)
            clock[0] = 1040.0
            app._check_title_fits()
            assert app._auto_icon_only is False
            clock[0] = 1060.0
            app._check_title_fits()
            assert app._auto_icon_only is True

            # Verify long live title (>18 chars) preserves `⚠` badge and drops `$cost` instead!
            app._running = True
            alert_row = _arow(
                1,
                "mainaccount",
                is_active=True,
                five_hour_pct=65.0,
            )
            bad_standby = _arow(
                2,
                "broken",
                is_active=False,
                attention_note="HTTP 403 forbidden",
                attention_kind="crit",
            )
            alert_snap = replace(
                app._snapshot,
                accounts=(alert_row, bad_standby),
                active=alert_row,
                account_notes={2: "HTTP 403 forbidden"},
            )
            app._auto_compact = False
            app._auto_icon_only = False
            slim_title = app.render_title(alert_snap)
            assert "⚠" in slim_title, slim_title
            app._auto_compact = True
            app._auto_icon_only = True
            app._running = False

            # Fullscreen menu bar (others_now = None) must NEVER crash with TypeError!
            clock[0] = 1130.0
            other_cnt[0] = None
            app._check_title_fits()
            assert app._auto_icon_only is True

            # Menu bar gains room (other_cnt drops from 8 -> 5) after >=60s -> restores full title!
            clock[0] = 1150.0
            hidden_state[0] = False
            other_cnt[0] = 5
            app._check_title_fits()
            assert app._auto_compact is False
            assert app._auto_icon_only is False

            # Even when user explicitly enabled title_compact=True, two hidden
            # readings still escalate to _auto_icon_only so it doesn't stay behind notch
            app._snapshot = replace(
                app.snapshot(),
                settings=normalize_settings(
                    {**SETTINGS_DEFAULTS, "title_compact": True, "title_view": "compact"}
                ),
            )
            hidden_state[0] = True
            clock[0] = 1200.0
            app._check_title_fits()
            clock[0] = 1240.0
            app._check_title_fits()
            assert app._auto_icon_only is True
        finally:
            app_mod.time.monotonic = orig_mono
            menubar.own_window_hidden = orig_own_hidden
            menubar.onscreen_other_count = orig_other_cnt
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)

    # _seed_status_item_position migrates legacy 2000.0 (far-left notch clip) -> 250.0
    # while preserving user Cmd-dragged values (e.g. 420.0)
    from cc_usage_widget import __main__ as main_mod

    class _FakeDefaults:
        def __init__(self, initial: dict[str, Any]) -> None:
            self.data = dict(initial)

        def objectForKey_(self, k: str) -> Any:
            return self.data.get(k)

        def setFloat_forKey_(self, val: float, k: str) -> None:
            self.data[k] = float(val)

    pos_key = "NSStatusItem Preferred Position Item-0"
    d_empty = _FakeDefaults({})
    main_mod._seed_status_item_position(d_empty)
    assert d_empty.data[pos_key] == 250.0

    d_legacy = _FakeDefaults({pos_key: 2000.0})
    main_mod._seed_status_item_position(d_legacy)
    assert d_legacy.data[pos_key] == 250.0

    d_custom = _FakeDefaults({pos_key: 420.0})
    main_mod._seed_status_item_position(d_custom)
    assert d_custom.data[pos_key] == 420.0


def test_macos26_proxy_window_visibility_never_false_positives() -> None:
    """macOS 26 (Tahoe) proxy NSStatusBarWindow (0x100000000, occlusionState=8192)
    must never trigger false-positive hidden verdicts during layout or fullscreen."""
    proxy_num = 0x100000000
    base_rows = [
        {
            "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
            "kCGWindowNumber": 64,
            "kCGWindowOwnerPID": 695,
            "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": 1650.0, "Y": 0.0, "Width": 150.0, "Height": 39.0},
        },
        {
            "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
            "kCGWindowNumber": 66,
            "kCGWindowOwnerPID": 695,
            "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": 1470.0, "Y": 0.0, "Width": 38.0, "Height": 39.0},
        },
        {
            "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
            "kCGWindowNumber": 311,
            "kCGWindowOwnerPID": 695,
            "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": 1057.0, "Y": 0.0, "Width": 32.0, "Height": 39.0},
        },
        # Tall layer-25 dropdown panel (e.g. WeatherMenu) must be ignored
        {
            "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
            "kCGWindowNumber": 145,
            "kCGWindowOwnerPID": 1349,
            "kCGWindowIsOnscreen": True,
            "kCGWindowBounds": {"X": 1081.0, "Y": 38.0, "Width": 332.0, "Height": 1200.0},
        },
    ]

    def _proxy_win(x: float, w: float) -> Any:
        return SimpleNamespace(
            frame=lambda: SimpleNamespace(
                origin=SimpleNamespace(x=x, y=1130.0),
                size=SimpleNamespace(width=w, height=39.0),
            ),
            occlusionState=lambda: 8192,  # bit 2 NOT set on macOS 26 proxy window
        )

    orig_wl = menubar._window_list
    try:
        # 1. Onscreen slot on macOS 26 -> False (even with occlusionState=8192)
        rows_onscreen = base_rows + [
            {
                "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
                "kCGWindowNumber": 18210,
                "kCGWindowOwnerPID": 695,
                "kCGWindowIsOnscreen": True,
                "kCGWindowBounds": {"X": 1361.0, "Y": 0.0, "Width": 109.0, "Height": 39.0},
            }
        ]
        menubar._window_list = lambda: (
            lambda opt, _rel: [r for r in rows_onscreen if r.get("kCGWindowIsOnscreen")]
            if opt == 1
            else rows_onscreen
        )
        w_vis = _proxy_win(1361.0, 109.0)
        assert menubar.own_window_hidden(proxy_num, win=w_vis) is False
        assert menubar.onscreen_other_count(proxy_num, win=w_vis) == 3

        # 2. Layout in flight after setTitle_ (width shifted 109 -> 115 before Control Centre flushed)
        # Must return None (no verdict), NEVER True!
        w_inflight = _proxy_win(1355.0, 115.0)
        assert menubar.own_window_hidden(proxy_num, win=w_inflight) is None

        # 3. Genuinely clipped behind notch (X=924 < leftmost onscreen 1057, kCGWindowIsOnscreen=None)
        rows_clipped = base_rows + [
            {
                "kCGWindowLayer": menubar.STATUS_ITEM_LAYER,
                "kCGWindowNumber": 18211,
                "kCGWindowOwnerPID": 695,
                "kCGWindowIsOnscreen": None,
                "kCGWindowBounds": {"X": 924.0, "Y": 0.0, "Width": 109.0, "Height": 39.0},
            }
        ]
        menubar._window_list = lambda: (
            lambda opt, _rel: [r for r in rows_clipped if r.get("kCGWindowIsOnscreen")]
            if opt == 1
            else rows_clipped
        )
        w_clip = _proxy_win(924.0, 109.0)
        assert menubar.own_window_hidden(proxy_num, win=w_clip) is True
    finally:
        menubar._window_list = orig_wl


# ---------------------------------------------------------------------------
# Standalone Test Runner
# ---------------------------------------------------------------------------


def _tests() -> list[tuple[str, Any]]:
    items = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def _uncollected_tests(collected: list[tuple[str, Any]]) -> list[str]:
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
    total = len(tests)
    print(f"\n{total - len(failures)} passed, {len(failures)} failed, out of {total}")
    if failures:
        print("failed: " + ", ".join(failures))
    orphans = _uncollected_tests(tests)
    if orphans:
        print(
            f"ERROR: {len(orphans)} test(s) sit below __main__: " + ", ".join(orphans)
        )
    return 1 if (failures or orphans) else 0


if __name__ == "__main__":
    raise SystemExit(main())
