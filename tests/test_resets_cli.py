"""Standalone test suite for `bswap resets`, Reset Radar CLI/TUI integration & privacy.

Covers:
  - `bswap resets` human terminal dashboard (Fleet Capacity, Banked Codex Reset Credits
    live/usable/total + per-account breakdown, Chronological Unified Timeline grouped
    into TODAY / TOMORROW / THIS WEEK across Claude ✦ and Codex ◈, color-coded badges
    🔓 UNLOCK / ⏳ EXPIRING / ↺ RESET, and Smart Fleet Advisor)
  - `bswap resets --json` machine-readable schema + strict zero raw email/token leaks
  - Exact shared contract alignment with `cc_usage_widget.resets` (verified via strict
    `slots=True` contract stub + clean lazy fallback when `resets.py` is absent)
  - Alias dispatch (`schedule`, `calendar`, `unlocks`) and flag handling
  - `bswap status` compact 2-line Reset Radar block (and `bswap status --json` purity)
  - `bswap doctor` Reset Radar check line (imminent unlocks, perishable quota, banked credits)
  - `bswap watch` TUI (`BelkinsWatchScreen` + `BelkinsResetRadarPanel`) and dashboard menu
  - `bswap help` documentation of `bswap resets [--json]`
  - Bare `python3` (no `claude_swap`, no `rumps`) compatibility with zero real `$HOME` mutation

Run directly::

    CC_USAGE_WIDGET_NO_REVEAL=1 python tests/test_resets_cli.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

from cc_usage_widget import accounts as accounts_mod  # noqa: E402
from cc_usage_widget import bswap_cli  # noqa: E402
from cc_usage_widget import fleet as fleet_mod  # noqa: E402
from cc_usage_widget.contracts import VENDOR_CLAUDE, VENDOR_CODEX, AccountRow  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Deterministic local midday anchor so +2h is ALWAYS TODAY, +26h is ALWAYS TOMORROW,
# and +74h is ALWAYS THIS WEEK regardless of the host wall-clock hour.
FIXED_NOW = dt.datetime(2026, 11, 21, 10, 0, 0).timestamp()

EMAIL_CANARY_CLAUDE = "secret.claude.founder@belkins-private.example"
EMAIL_CANARY_CODEX = "secret.codex.owner@belkins-private.example"
TOKEN_CANARY_ACCESS = "eyJhbGciOiJSUzI1NiJ9.SECRET_ACCESS_CANARY_99182.sig"
TOKEN_CANARY_REFRESH = "rt_SECRET_REFRESH_CANARY_77412_NEVER_PRINT"


def _plain(text: str) -> str:
    return ANSI_RE.sub("", text)


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claude_row(
    slot: int,
    alias: str,
    *,
    email: str = EMAIL_CANARY_CLAUDE,
    is_active: bool = False,
    plan_type: str | None = "claude_max_20x",
    five_hour_pct: float | None = 15.0,
    seven_day_pct: float | None = 25.0,
    five_hour_resets_at: str | None = None,
    seven_day_resets_at: str | None = None,
    scoped_windows: tuple[tuple[str, float], ...] = (),
    scoped_resets_at: tuple[tuple[str, str], ...] = (),
    disabled: bool = False,
    switchable: bool = True,
    attention_note: str = "",
) -> AccountRow:
    return AccountRow(
        slot=slot,
        alias=alias,
        email=email,
        is_active=is_active,
        five_hour_pct=five_hour_pct,
        seven_day_pct=seven_day_pct,
        five_hour_resets_at=five_hour_resets_at,
        seven_day_resets_at=seven_day_resets_at,
        scoped_windows=scoped_windows,
        scoped_resets_at=scoped_resets_at,
        disabled=disabled,
        switchable=switchable,
        plan_type=plan_type,
        attention_note=attention_note,
        vendor=VENDOR_CLAUDE,
    )


class _FakeAccountSource:
    def __init__(
        self,
        rows: list[AccountRow],
        *,
        sentinels: dict[int, str] | None = None,
        available: bool = True,
    ) -> None:
        self._rows = list(rows)
        self._sentinels = dict(sentinels or {})
        self._available = available
        self.last_error: str | None = None

    def available(self) -> bool:
        return self._available

    def refresh(self, *, force: bool = False) -> None:
        _ = force

    def rows(self) -> tuple[AccountRow, ...]:
        return tuple(self._rows)

    def active(self) -> AccountRow | None:
        return next((r for r in self._rows if r.is_active), None)

    def sentinels(self) -> dict[int, str]:
        return dict(self._sentinels)

    def recent_events(self) -> tuple[str, ...]:
        return ()

    def autoswitch_threshold(self) -> float | None:
        return 88.0

    def autoswitch_enabled(self) -> bool:
        return True


def _write_codex_fixture(widget_dir: Path, auth_path: Path, *, now: float = FIXED_NOW) -> None:
    """Write isolated Codex registry, quota snapshots, and auth.json with secret canaries."""
    registry = {
        "version": 1,
        "accounts": [
            {"account_id": "acct-pro-11111111", "alias": "belkins-pro", "enabled": True, "order": 0},
            {"account_id": "acct-team-22222222", "alias": "", "enabled": True, "order": 1},
            {"account_id": "acct-dead-33333333", "alias": "dead-cx", "enabled": True, "order": 2},
            {"account_id": "acct-off-44444444", "alias": "disabled-cx", "enabled": False, "order": 3},
        ],
    }
    (widget_dir / "codex_accounts.json").write_text(json.dumps(registry), encoding="utf-8")

    snapshots = {
        "version": 1,
        "desktop_account_id": "acct-pro-11111111",
        "accounts": {
            "acct-pro-11111111": {
                "quota": {
                    "account_id": "acct-pro-11111111",
                    "email": EMAIL_CANARY_CODEX,
                    "plan_type": "pro",
                    "five_hour": {
                        "used_percent": 35.0,
                        "window_seconds": 18000,
                        "reset_at": now + 3 * 3600,  # TODAY 13:00
                    },
                    "seven_day": {
                        "used_percent": 98.0,
                        "window_seconds": 604800,
                        "reset_at": now + 26 * 3600,  # TOMORROW 12:00
                    },
                    "scoped": [],
                    "limit_reached": True,
                    "allowed": False,
                    "reached_type": "rate_limit_reached",
                    "fetched_at": now - 60,
                    "credits_balance": 0.0,
                    "credits_has": False,
                    "credits_would_enable": False,
                    "availability": [],
                    "reset_credits_available": 2,
                    "reset_credits_usable": 1,
                },
                "note": "capped",
                "note_kind": "crit",
                "samples": [[now - 60, 98.0]],
            },
            "acct-team-22222222": {
                "quota": {
                    "account_id": "acct-team-22222222",
                    "email": EMAIL_CANARY_CODEX,
                    "plan_type": "team",
                    "five_hour": None,
                    "seven_day": {
                        "used_percent": 20.0,
                        "window_seconds": 604800,
                        "reset_at": now + 74 * 3600,  # THIS WEEK (+3d 2h)
                    },
                    "scoped": [],
                    "limit_reached": False,
                    "allowed": True,
                    "reached_type": None,
                    "fetched_at": now - 120,
                    "credits_balance": 0.0,
                    "credits_has": False,
                    "credits_would_enable": False,
                    "availability": [],
                    "reset_credits_available": 1,
                    "reset_credits_usable": 0,
                },
                "note": "",
                "note_kind": "",
                "samples": [[now - 120, 20.0]],
            },
            "acct-dead-33333333": {
                "quota": {
                    "account_id": "acct-dead-33333333",
                    "email": EMAIL_CANARY_CODEX,
                    "plan_type": "pro",
                    "five_hour": None,
                    "seven_day": {
                        "used_percent": 50.0,
                        "window_seconds": 604800,
                        "reset_at": now + 5 * 3600,
                    },
                    "scoped": [],
                    "limit_reached": False,
                    "allowed": True,
                    "reached_type": None,
                    "fetched_at": now - 12 * 3600,  # stale + relogin
                    "credits_balance": 0.0,
                    "credits_has": False,
                    "credits_would_enable": False,
                    "availability": [],
                    "reset_credits_available": 2,
                    "reset_credits_usable": 1,
                },
                "note": "relogin",
                "note_kind": "warn",
                "samples": [],
            },
        },
    }
    (widget_dir / "codex_quota_snapshots.json").write_text(json.dumps(snapshots), encoding="utf-8")

    auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "account_id": "acct-pro-11111111",
                    "access_token": TOKEN_CANARY_ACCESS,
                    "refresh_token": TOKEN_CANARY_REFRESH,
                }
            }
        ),
        encoding="utf-8",
    )


@contextlib.contextmanager
def _isolated_env(
    root: Path,
    *,
    fake_source: Any | None = None,
    fake_snap: Any | None = None,
    now: float = FIXED_NOW,
    write_codex: bool = True,
):
    """Isolate all CLI paths, clock, and account sources inside *root*."""
    backup_dir = root / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    widget_dir = root / "widget"
    widget_dir.mkdir(parents=True, exist_ok=True)
    shims_dir = widget_dir / "shims"
    sessions_dir = root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    claude_cfg = root / ".claude.json"
    codex_auth = root / "codex_auth.json"

    if write_codex:
        _write_codex_fixture(widget_dir, codex_auth, now=now)

    orig_widget_dir = bswap_cli.WIDGET_DIR
    orig_shims_dir = bswap_cli.SHIMS_DIR
    orig_backup_dir = bswap_cli._backup_dir
    orig_claude_cfg = bswap_cli._claude_config_path
    orig_codex_auth = bswap_cli._codex_auth_path
    orig_now_ts = bswap_cli._now_ts
    orig_create_src = accounts_mod.create_account_source
    orig_collect = fleet_mod.collect
    orig_sessions_env = os.environ.get("CC_USAGE_WIDGET_SESSIONS_DIR")

    try:
        bswap_cli.WIDGET_DIR = widget_dir
        bswap_cli.SHIMS_DIR = shims_dir
        bswap_cli._backup_dir = lambda: backup_dir  # type: ignore[assignment]
        bswap_cli._claude_config_path = lambda _src=None: claude_cfg  # type: ignore[assignment]
        bswap_cli._codex_auth_path = lambda: codex_auth  # type: ignore[assignment]
        bswap_cli._now_ts = lambda: now  # type: ignore[assignment]
        os.environ["CC_USAGE_WIDGET_SESSIONS_DIR"] = str(sessions_dir)

        if fake_source is not None:
            accounts_mod.create_account_source = lambda *_a, **_kw: fake_source  # type: ignore[assignment]
        if fake_snap is not None:
            fleet_mod.collect = lambda **_kw: fake_snap  # type: ignore[assignment]

        yield SimpleNamespace(
            root=root,
            backup_dir=backup_dir,
            widget_dir=widget_dir,
            shims_dir=shims_dir,
            sessions_dir=sessions_dir,
            claude_cfg=claude_cfg,
            codex_auth=codex_auth,
        )
    finally:
        bswap_cli.WIDGET_DIR = orig_widget_dir
        bswap_cli.SHIMS_DIR = orig_shims_dir
        bswap_cli._backup_dir = orig_backup_dir
        bswap_cli._claude_config_path = orig_claude_cfg
        bswap_cli._codex_auth_path = orig_codex_auth
        bswap_cli._now_ts = orig_now_ts
        accounts_mod.create_account_source = orig_create_src
        fleet_mod.collect = orig_collect
        if orig_sessions_env is None:
            os.environ.pop("CC_USAGE_WIDGET_SESSIONS_DIR", None)
        else:
            os.environ["CC_USAGE_WIDGET_SESSIONS_DIR"] = orig_sessions_env


def _sample_claude_rows(now: float = FIXED_NOW) -> list[AccountRow]:
    return [
        _claude_row(
            1,
            "vlad",
            is_active=True,
            plan_type="claude_max_20x",
            five_hour_pct=20.0,
            seven_day_pct=25.0,
            scoped_windows=(("Fable", 22.0),),
            scoped_resets_at=(("Fable", _iso(now + 6 * 3600)),),  # TODAY perishable (78% free)
            seven_day_resets_at=_iso(now + 6 * 3600),  # same minute -> deduped to Fable
        ),
        _claude_row(
            2,
            "capped-max",
            plan_type="claude_max_20x",
            five_hour_pct=15.0,
            seven_day_pct=60.0,
            scoped_windows=(("Fable", 96.0),),
            scoped_resets_at=(("Fable", _iso(now + 2 * 3600 + 54 * 60)),),  # TODAY unlock
            seven_day_resets_at=_iso(now + 4 * 86400),
        ),
        _claude_row(
            3,
            EMAIL_CANARY_CLAUDE,  # unaliased raw email -> MUST be sanitized!
            plan_type="claude_max_20x",
            five_hour_pct=5.0,
            seven_day_pct=10.0,
            scoped_windows=(("Fable", 15.0),),
            scoped_resets_at=(("Fable", _iso(now + 30 * 3600)),),  # TOMORROW perishable
            seven_day_resets_at=_iso(now + 30 * 3600),
        ),
        _claude_row(
            4,
            "work1",
            plan_type="claude_team",
            five_hour_pct=10.0,
            seven_day_pct=18.0,
            scoped_windows=(),
            seven_day_resets_at=_iso(now + 80 * 3600),  # THIS WEEK no-Fable
        ),
        _claude_row(
            5,
            "expired-seat",
            plan_type="claude_max_20x",
            switchable=False,
            attention_note="re-login needed",
        ),
    ]


# ---------------------------------------------------------------------------
# Strict-contract `cc_usage_widget.resets` stub (exact fields from spec)
# ---------------------------------------------------------------------------


def _build_strict_contract_resets_module():
    """Create a strict `slots=True` stub matching `resets_core`'s exact dataclass contract.

    Any attempt by `bswap_cli.py` to access a misspelled or non-contract attribute
    raises `AttributeError` immediately.
    """

    @dataclass(frozen=True, slots=True)
    class ResetEvent:
        vendor: str
        slot: int
        alias: str
        window: str
        used_pct: float | None
        free_pct: float | None
        resets_at_epoch: float
        seconds_until: float
        reset_label: str
        countdown_label: str
        kind: str
        is_active: bool = False
        disabled: bool = False
        plan_label: str = ""
        no_fable: bool = False

    @dataclass(frozen=True, slots=True)
    class ResetCreditAccount:
        vendor: str
        slot: int
        alias: str
        available: int
        usable: int
        used_pct: float | None
        is_active: bool = False
        needs_relogin: bool = False

    @dataclass(frozen=True, slots=True)
    class ResetCreditsSummary:
        total_available: int
        live_available: int
        total_usable: int
        accounts: tuple[ResetCreditAccount, ...]

    @dataclass(frozen=True, slots=True)
    class FleetCapacitySummary:
        claude_fable_free_seats: float
        claude_fable_total_seats: int
        claude_locked_relogin_seats: int
        claude_next_unlock: ResetEvent | None
        codex_free_seats: float
        codex_total_seats: int
        codex_locked_relogin_seats: int
        codex_next_unlock: ResetEvent | None
        perishable_seats_48h: float

    @dataclass(frozen=True, slots=True)
    class FleetRecommendation:
        kind: str
        headline: str
        detail: str
        target_vendor: str | None = None
        target_slot: int | None = None

    @dataclass(frozen=True, slots=True)
    class FleetResetRadar:
        generated_at: float
        events: tuple[ResetEvent, ...]
        unlocks: tuple[ResetEvent, ...]
        perishable: tuple[ResetEvent, ...]
        credits: ResetCreditsSummary
        capacity: FleetCapacitySummary
        recommendation: FleetRecommendation | None

    mod = types.ModuleType("cc_usage_widget.resets")
    mod.ResetEvent = ResetEvent  # type: ignore[attr-defined]
    mod.ResetCreditAccount = ResetCreditAccount  # type: ignore[attr-defined]
    mod.ResetCreditsSummary = ResetCreditsSummary  # type: ignore[attr-defined]
    mod.FleetCapacitySummary = FleetCapacitySummary  # type: ignore[attr-defined]
    mod.FleetRecommendation = FleetRecommendation  # type: ignore[attr-defined]
    mod.FleetResetRadar = FleetResetRadar  # type: ignore[attr-defined]
    calls: list[dict[str, Any]] = []
    mod.calls = calls  # type: ignore[attr-defined]

    def build_fleet_reset_radar(
        accounts: Any,
        quota_rows: Any,
        *,
        account_notes: Any = None,
        now: float | None = None,
        fable_guard: bool = True,
    ) -> FleetResetRadar:
        now_ts = FIXED_NOW if now is None else float(now)
        calls.append(
            {
                "accounts": list(accounts),
                "quota_rows": list(quota_rows),
                "account_notes": dict(account_notes or {}),
                "now": now_ts,
                "fable_guard": fable_guard,
            }
        )
        ev_unlock = ResetEvent(
            vendor="claude",
            slot=2,
            alias="capped-max",
            window="Fable",
            used_pct=96.0,
            free_pct=4.0,
            resets_at_epoch=now_ts + 2 * 3600 + 54 * 60,
            seconds_until=2 * 3600 + 54 * 60,
            reset_label="12:54",
            countdown_label="2h 54m",
            kind="unlock",
            is_active=False,
            disabled=False,
            plan_label="claude_max_20x",
            no_fable=False,
        )
        ev_perish = ResetEvent(
            vendor="claude",
            slot=1,
            alias="vlad",
            window="Fable",
            used_pct=22.0,
            free_pct=78.0,
            resets_at_epoch=now_ts + 26 * 3600,
            seconds_until=26 * 3600,
            reset_label="Tom 12:00",
            countdown_label="1d 2h",
            kind="perishable",
            is_active=True,
            disabled=False,
            plan_label="claude_max_20x",
            no_fable=False,
        )
        ev_codex_unlock = ResetEvent(
            vendor="codex",
            slot=-1,
            alias="belkins-pro",
            window="7d",
            used_pct=100.0,
            free_pct=0.0,
            resets_at_epoch=now_ts + 74 * 3600,
            seconds_until=74 * 3600,
            reset_label="Tue 12:00",
            countdown_label="3d 2h",
            kind="unlock",
            is_active=True,
            disabled=False,
            plan_label="pro",
            no_fable=False,
        )
        ev_upcoming = ResetEvent(
            vendor="claude",
            slot=4,
            alias="work1",
            window="7d",
            used_pct=18.0,
            free_pct=82.0,
            resets_at_epoch=now_ts + 80 * 3600,
            seconds_until=80 * 3600,
            reset_label="Tue 18:00",
            countdown_label="3d 8h",
            kind="upcoming",
            is_active=False,
            disabled=False,
            plan_label="claude_team",
            no_fable=True,
        )
        cred_live = ResetCreditAccount(
            vendor="codex",
            slot=-1,
            alias="belkins-pro",
            available=2,
            usable=1,
            used_pct=100.0,
            is_active=True,
            needs_relogin=False,
        )
        cred_relogin = ResetCreditAccount(
            vendor="codex",
            slot=-3,
            alias="dead-cx",
            available=2,
            usable=0,
            used_pct=None,
            is_active=False,
            needs_relogin=True,
        )
        return FleetResetRadar(
            generated_at=now_ts,
            events=(ev_unlock, ev_perish, ev_codex_unlock, ev_upcoming),
            unlocks=(ev_unlock, ev_codex_unlock),
            perishable=(ev_perish,),
            credits=ResetCreditsSummary(
                total_available=4,
                live_available=2,
                total_usable=1,
                accounts=(cred_live, cred_relogin),
            ),
            capacity=FleetCapacitySummary(
                claude_fable_free_seats=1.82,
                claude_fable_total_seats=3,
                claude_locked_relogin_seats=1,
                claude_next_unlock=ev_unlock,
                codex_free_seats=0.8,
                codex_total_seats=2,
                codex_locked_relogin_seats=1,
                codex_next_unlock=ev_codex_unlock,
                perishable_seats_48h=0.78,
            ),
            recommendation=FleetRecommendation(
                kind="use_reset_credit",
                headline="Use 1× Codex reset credit on belkins-pro",
                detail="belkins-pro is capped and has a usable reset credit right now.",
                target_vendor="codex",
                target_slot=-1,
            ),
        )

    mod.build_fleet_reset_radar = build_fleet_reset_radar  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bswap_resets_human_dashboard_and_strict_privacy() -> None:
    """`bswap resets` renders capacity, banked credits, TODAY/TOMORROW/THIS WEEK, advisor, zero leaks."""
    fake_src = _FakeAccountSource(_sample_claude_rows(), sentinels={5: "re-login needed"})
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_env(Path(tmp), fake_source=fake_src):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bswap_cli.main(["resets"])
            out = _plain(buf.getvalue())
            assert rc == 0, out

            # Section 1: Fleet Capacity & Banked Credits Summary
            assert "UNIFIED RESET RADAR & BANKED CREDITS" in out, out
            assert "Claude Fable:" in out and "seats free" in out, out
            assert "Codex Fleet:" in out, out
            assert "3× live" in out and "1× usable now" in out, out
            assert "5× total incl. relogin" in out, out
            assert "belkins-pro" in out and "2× banked" in out, out
            assert "dead-cx" in out and "relogin needed" in out, out
            assert "Perishable <48h:" in out, out
            assert "Locked (re-login): 1 Claude · 1 Codex" in out, out

            # Section 2: Chronological Unified Reset Timeline across BOTH Claude & Codex
            assert "TODAY" in out, out
            assert "TOMORROW" in out, out
            assert "THIS WEEK" in out, out
            assert "✦" in out and "◈" in out, out
            assert "🔓 UNLOCK" in out, out
            assert "⏳ EXPIRING" in out, out
            assert "↺  RESET" in out or "↺ RESET" in out, out
            assert "capped-max" in out and "2h 54m" in out, out
            assert "work1" in out and "no-Fable" in out, out
            assert "slot-3" in out, "Unaliased Claude email row should display sanitized slot-3"
            assert "codex-acct-tea" in out, "Unaliased Codex row should display sanitized id prefix"

            # Section 3: Smart Fleet Advisor
            assert "Smart Fleet Advisor" in out, out
            assert "belkins-pro" in out, out

            # Privacy: zero raw email or OAuth token leaks anywhere in output
            for secret in (
                EMAIL_CANARY_CLAUDE,
                EMAIL_CANARY_CODEX,
                TOKEN_CANARY_ACCESS,
                TOKEN_CANARY_REFRESH,
                "@belkins-private.example",
            ):
                assert secret not in out, f"Leaked secret {secret!r} in `bswap resets` output!"


def test_bswap_resets_json_schema_and_zero_leaks() -> None:
    """`bswap resets --json` emits pure valid JSON (zero banner/ANSI, zero emails/tokens)."""
    fake_src = _FakeAccountSource(_sample_claude_rows(), sentinels={5: "re-login needed"})
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_env(Path(tmp), fake_source=fake_src):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bswap_cli.main(["resets", "--json"])
            raw = buf.getvalue()
            assert rc == 0, raw
            assert "\x1b[" not in raw, "JSON output must never contain ANSI escape sequences"
            assert "BELKINS AI SQUAD" not in raw, "JSON output must not include ASCII banner"

            data = json.loads(raw)
            assert isinstance(data["generated_at"], (int, float))

            cap = data["capacity"]
            assert cap["claude_fable_total_seats"] == 3, cap
            assert cap["claude_locked_relogin_seats"] == 1, cap
            assert cap["codex_total_seats"] == 2, cap
            assert cap["codex_locked_relogin_seats"] == 1, cap
            assert cap["claude_next_unlock"]["alias"] == "capped-max", cap
            assert cap["codex_next_unlock"]["alias"] == "belkins-pro", cap
            assert cap["perishable_seats_48h"] > 0, cap

            cred = data["credits"]
            assert cred["live_available"] == 3, cred
            assert cred["total_usable"] == 1, cred
            assert cred["total_available"] == 5, cred
            assert len(cred["accounts"]) == 3, cred

            rec = data["recommendation"]
            assert rec["kind"] == "use_reset_credit", rec
            assert rec["target_vendor"] == "codex", rec
            assert rec["target_slot"] == -1, rec

            timeline = data["timeline"]
            assert set(timeline.keys()) == {"TODAY", "TOMORROW", "THIS WEEK"}
            assert len(timeline["TODAY"]) >= 2, timeline
            assert len(timeline["TOMORROW"]) >= 1, timeline
            assert len(timeline["THIS WEEK"]) >= 1, timeline

            # Chronological order verified
            epochs = [ev["resets_at_epoch"] for ev in data["events"]]
            assert epochs == sorted(epochs), epochs

            # Zero raw emails, tokens, or "email" keys anywhere in JSON payload
            for secret in (
                EMAIL_CANARY_CLAUDE,
                EMAIL_CANARY_CODEX,
                TOKEN_CANARY_ACCESS,
                TOKEN_CANARY_REFRESH,
                "@belkins-private.example",
            ):
                assert secret not in raw, f"Leaked secret {secret!r} in `bswap resets --json`!"
            for ev in data["events"]:
                assert "email" not in ev, ev
                assert "@" not in ev["alias"], ev


def test_strict_resets_module_contract_alignment() -> None:
    """When `cc_usage_widget.resets` is present, CLI delegates cleanly using exact contract fields."""
    import cc_usage_widget as pkg

    strict_mod = _build_strict_contract_resets_module()
    fake_src = _FakeAccountSource(_sample_claude_rows(), sentinels={5: "re-login needed"})
    orig_mod = sys.modules.get("cc_usage_widget.resets")
    had_pkg_attr = hasattr(pkg, "resets")
    orig_pkg_attr = getattr(pkg, "resets", None)
    try:
        sys.modules["cc_usage_widget.resets"] = strict_mod
        setattr(pkg, "resets", strict_mod)
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_env(Path(tmp), fake_source=fake_src):
                # 1. Human mode
                buf_h = io.StringIO()
                with contextlib.redirect_stdout(buf_h):
                    rc_h = bswap_cli.main(["resets"])
                out_h = _plain(buf_h.getvalue())
                assert rc_h == 0, out_h
                assert len(strict_mod.calls) == 1
                call0 = strict_mod.calls[0]
                assert call0["fable_guard"] is True
                assert call0["account_notes"] == {5: "re-login needed"}
                # Sanitized rows passed to canonical resets module (no raw emails!)
                assert all(r.email == "" for r in call0["accounts"])
                assert all("@" not in r.alias for r in call0["accounts"])
                assert all(r.email == "" for r in call0["quota_rows"])
                assert "1.8 / 3 seats free" in out_h, out_h
                assert "2× live" in out_h and "1× usable now" in out_h, out_h
                assert "4× total incl. relogin" in out_h, out_h
                assert "Use 1× Codex reset credit on belkins-pro" in out_h, out_h

                # 2. JSON mode
                buf_j = io.StringIO()
                with contextlib.redirect_stdout(buf_j):
                    rc_j = bswap_cli.main(["resets", "--json"])
                assert rc_j == 0
                payload = json.loads(buf_j.getvalue())
                assert payload["capacity"]["claude_fable_free_seats"] == 1.82
                assert payload["credits"]["total_available"] == 4
                assert payload["recommendation"]["kind"] == "use_reset_credit"
    finally:
        if orig_mod is None:
            sys.modules.pop("cc_usage_widget.resets", None)
        else:
            sys.modules["cc_usage_widget.resets"] = orig_mod
        if had_pkg_attr:
            setattr(pkg, "resets", orig_pkg_attr)
        elif hasattr(pkg, "resets"):
            delattr(pkg, "resets")


def test_resets_alias_dispatch_and_flags() -> None:
    """`schedule`, `calendar`, and `unlocks` all dispatch to `_cmd_resets`."""
    fake_src = _FakeAccountSource(_sample_claude_rows())
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_env(Path(tmp), fake_source=fake_src):
            for alias in ("schedule", "calendar", "unlocks"):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = bswap_cli.main([alias, "--json"])
                assert rc == 0, alias
                parsed = json.loads(buf.getvalue())
                assert "capacity" in parsed and "timeline" in parsed, alias

            # --help exits 0; unknown flag exits 2
            buf_help = io.StringIO()
            with contextlib.redirect_stdout(buf_help):
                rc_help = bswap_cli.main(["resets", "--help"])
            assert rc_help == 0 and "bswap resets [--json]" in _plain(buf_help.getvalue())

            buf_err = io.StringIO()
            with contextlib.redirect_stderr(buf_err):
                rc_bad = bswap_cli.main(["resets", "--unknown-flag"])
            assert rc_bad == 2


def test_bswap_status_and_doctor_reset_radar_integration() -> None:
    """`bswap status` prints compact 2-line Reset Radar block; `bswap doctor` prints Reset Radar check."""
    fake_src = _FakeAccountSource(_sample_claude_rows(), sentinels={5: "re-login needed"})
    fake_snap = fleet_mod.FleetSnapshot()
    orig_run_cswap = bswap_cli._run_cswap
    cswap_calls: list[tuple[list[str], bool]] = []

    def _stub_cswap(args: list[str], *, show_banner: bool = False) -> int:
        cswap_calls.append((list(args), show_banner))
        if "--json" in args:
            print(json.dumps({"activeAccount": 1}))
        else:
            print("Active account: #1 (vlad)")
        return 0

    try:
        bswap_cli._run_cswap = _stub_cswap  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_env(Path(tmp), fake_source=fake_src, fake_snap=fake_snap) as env:
                # 1. `bswap status` human output includes compact 2-line Reset Radar block
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = bswap_cli.main(["status"])
                out = _plain(buf.getvalue())
                assert rc == 0, out
                assert "Active account: #1 (vlad)" in out, out
                assert "Reset Radar:" in out, out
                assert "Next Unlock:" in out and "capped-max" in out, out
                assert "Codex Credits:" in out and "3× live (1× usable now)" in out, out
                assert "Advisor:" in out and "belkins-pro" in out, out
                assert EMAIL_CANARY_CLAUDE not in out and EMAIL_CANARY_CODEX not in out

                # 2. `bswap status --json` preserves pure JSON (no ANSI Reset Radar appended!)
                buf_j = io.StringIO()
                with contextlib.redirect_stdout(buf_j):
                    rc_j = bswap_cli.main(["status", "--json"])
                assert rc_j == 0
                parsed_status = json.loads(buf_j.getvalue())
                assert parsed_status == {"activeAccount": 1}

                # 3. `bswap doctor` includes Reset Radar check line (unlocks, perishable, credits)
                (env.backup_dir / "settings.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "autoswitch": {
                                "enabled": True,
                                "threshold": 88.0,
                                "weeklyThreshold": 88.0,
                                "intervalSeconds": 30.0,
                                "cooldownSeconds": 120.0,
                                "hysteresisPct": 10.0,
                                "model": "all",
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                buf_d = io.StringIO()
                with contextlib.redirect_stdout(buf_d):
                    rc_d = bswap_cli.main(["doctor"])
                out_d = _plain(buf_d.getvalue())
                assert rc_d == 0, out_d
                assert "Reset Radar:" in out_d, out_d
                assert "imminent unlock" in out_d and "capped-max" in out_d, out_d
                assert "perishable seat(s) <48h" in out_d, out_d
                assert "live Codex reset credits (1× usable now)" in out_d, out_d
                assert EMAIL_CANARY_CLAUDE not in out_d and EMAIL_CANARY_CODEX not in out_d
                assert TOKEN_CANARY_ACCESS not in out_d and TOKEN_CANARY_REFRESH not in out_d
    finally:
        bswap_cli._run_cswap = orig_run_cswap  # type: ignore[assignment]


def test_help_documents_bswap_resets() -> None:
    """`bswap help` cleanly documents `bswap resets [--json]` and its aliases."""
    fake_src = _FakeAccountSource([])
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_env(Path(tmp), fake_source=fake_src, write_codex=False):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bswap_cli.main(["help"])
            out = _plain(buf.getvalue())
            assert rc == 0
            assert "bswap resets [--json]" in out, out
            assert "schedule" in out and "calendar" in out and "unlocks" in out, out


def test_bare_python3_no_claude_swap_compatibility() -> None:
    """`bswap resets`, `--json`, `status`, `doctor` work cleanly in bare `python3` (no `claude_swap`)."""
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        widget_dir = root / "widget"
        widget_dir.mkdir(parents=True, exist_ok=True)
        backup_dir = root / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        auth_path = root / "auth.json"
        _write_codex_fixture(widget_dir, auth_path, now=FIXED_NOW)

        # Write a local sequence.json in isolated backup dir (with email canary)
        (backup_dir / "sequence.json").write_text(
            json.dumps(
                {
                    "activeAccountNumber": 1,
                    "accounts": {
                        "1": {
                            "alias": "bare-max",
                            "email": EMAIL_CANARY_CLAUDE,
                            "disabled": False,
                            "usage": {
                                "five_hour": {
                                    "utilization": 91.0,
                                    "resets_at": _iso(FIXED_NOW + 3600),
                                },
                                "seven_day": {
                                    "utilization": 30.0,
                                    "resets_at": _iso(FIXED_NOW + 20 * 3600),
                                },
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        script = f"""
import contextlib, io, json, os, sys
from pathlib import Path
sys.path.insert(0, {str(repo_root)!r})
sys.modules["claude_swap"] = None
sys.modules["rumps"] = None
sys.modules["cc_usage_widget.resets"] = None
from cc_usage_widget import bswap_cli
bswap_cli.WIDGET_DIR = Path({str(widget_dir)!r})
bswap_cli._backup_dir = lambda: Path({str(backup_dir)!r})
bswap_cli._codex_auth_path = lambda: Path({str(auth_path)!r})
bswap_cli._claude_config_path = lambda _s=None: Path({str(root / '.claude.json')!r})
bswap_cli._now_ts = lambda: {FIXED_NOW!r}
os.environ["CC_USAGE_WIDGET_SESSIONS_DIR"] = {str(root / 'sessions')!r}

buf_json = io.StringIO()
with contextlib.redirect_stdout(buf_json):
    rc1 = bswap_cli.main(["resets", "--json"])
assert rc1 == 0
data = json.loads(buf_json.getvalue())
assert data["credits"]["live_available"] == 3, data["credits"]
assert data["credits"]["total_usable"] == 1, data["credits"]
assert any(e["alias"] == "bare-max" for e in data["events"]), data["events"]
assert any(e["alias"] == "belkins-pro" for e in data["events"]), data["events"]
assert {EMAIL_CANARY_CLAUDE!r} not in buf_json.getvalue()
assert {EMAIL_CANARY_CODEX!r} not in buf_json.getvalue()
assert {TOKEN_CANARY_ACCESS!r} not in buf_json.getvalue()

buf_human = io.StringIO()
with contextlib.redirect_stdout(buf_human):
    rc2 = bswap_cli.main(["calendar"])
assert rc2 == 0
assert "UNIFIED RESET RADAR" in buf_human.getvalue()
assert {EMAIL_CANARY_CLAUDE!r} not in buf_human.getvalue()
assert {TOKEN_CANARY_REFRESH!r} not in buf_human.getvalue()

buf_doc = io.StringIO()
with contextlib.redirect_stdout(buf_doc):
    rc3 = bswap_cli.main(["doctor"])
assert rc3 == 0
assert "Reset Radar:" in buf_doc.getvalue()
print("BARE_PYTHON3_OK")
"""
        env = dict(os.environ)
        env["CC_USAGE_WIDGET_NO_REVEAL"] = "1"
        env["HOME"] = str(root / "fake_home")
        (root / "fake_home").mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["python3", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        assert "BARE_PYTHON3_OK" in proc.stdout, proc.stdout


def test_watch_tui_reset_radar_panel_integration() -> None:
    """When `claude_swap.tui` is installed, `BelkinsTuiApp` wraps `WatchScreen` with Reset Radar HUD."""
    try:
        import claude_swap.tui.dashboard as cswap_dash  # noqa: F401
        from claude_swap.tui.theme import CSWAP_DARK  # noqa: F401
    except Exception:
        # Gracefully skip TUI widget test under bare python3 without textual/claude_swap.
        return

    fake_src = _FakeAccountSource(_sample_claude_rows(), sentinels={5: "re-login needed"})
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_env(Path(tmp), fake_source=fake_src):
            app = bswap_cli._build_belkins_tui_app(SimpleNamespace(), start="watch", detected="dark")
            # Intercept super().push_screen to inspect the upgraded screen class cleanly without starting Textual loop.
            pushed: list[Any] = []
            orig_super = bswap_cli._build_belkins_tui_app  # keep lint quiet
            _ = orig_super

            from claude_swap.tui.app import CswapApp

            orig_cswap_push = CswapApp.push_screen
            try:
                CswapApp.push_screen = lambda self, screen, *a, **kw: pushed.append(screen)  # type: ignore[method-assign]
                app.push_screen(cswap_dash.WatchScreen())
                app.push_screen(cswap_dash.DashboardScreen())
            finally:
                CswapApp.push_screen = orig_cswap_push  # type: ignore[method-assign]

            assert len(pushed) == 2
            watch_scr, dash_scr = pushed
            assert type(watch_scr).__name__ == "BelkinsWatchScreen", type(watch_scr)
            assert type(dash_scr).__name__ == "BelkinsDashboardScreen", type(dash_scr)

            # Verify Dashboard menu exposes Reset Radar entry
            root_ids = [aid for _lbl, aid in dash_scr._root_entries()]
            assert "belkins-resets" in root_ids, root_ids
            resets_entries = dash_scr._resets_entries()
            assert any("Claude Fable" in lbl for lbl, _ in resets_entries), resets_entries

            # Verify Watch screen composes BelkinsResetRadarPanel and renders cleanly without raw emails
            composed = list(watch_scr.compose())
            radar_panels = [w for w in composed if getattr(w, "id", None) == "belkins-reset-radar"]
            assert len(radar_panels) == 1, composed
            panel = radar_panels[0]
            # Stub app.current_theme on the panel so render() works headless
            panel._app_override = SimpleNamespace(current_theme=CSWAP_DARK)  # type: ignore[attr-defined]
            panel_cls = type(panel)
            orig_app_prop = panel_cls.app
            try:
                panel_cls.app = property(lambda self: self._app_override)  # type: ignore[assignment]
                rendered = panel.render().plain
            finally:
                panel_cls.app = orig_app_prop  # type: ignore[assignment]

            assert "RESET RADAR" in rendered, rendered
            assert "Credits 3× live (1× usable now)" in rendered, rendered
            assert "capped-max" in rendered, rendered
            assert EMAIL_CANARY_CLAUDE not in rendered and EMAIL_CANARY_CODEX not in rendered
            assert TOKEN_CANARY_ACCESS not in rendered


def test_empty_fleet_resets_handles_gracefully() -> None:
    """Empty Claude + Codex fleet renders clean empty state and valid JSON without crashing."""
    fake_src = _FakeAccountSource([])
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_env(Path(tmp), fake_source=fake_src, write_codex=False):
            buf_h = io.StringIO()
            with contextlib.redirect_stdout(buf_h):
                rc_h = bswap_cli.main(["resets"])
            out_h = _plain(buf_h.getvalue())
            assert rc_h == 0, out_h
            assert "No upcoming Claude or Codex resets reported yet." in out_h, out_h

            buf_j = io.StringIO()
            with contextlib.redirect_stdout(buf_j):
                rc_j = bswap_cli.main(["resets", "--json"])
            assert rc_j == 0
            data = json.loads(buf_j.getvalue())
            assert data["events"] == []
            assert data["credits"]["total_available"] == 0
            assert data["timeline"] == {"TODAY": [], "TOMORROW": [], "THIS WEEK": []}


def test_codex_snapshots_only_scoped_and_none_recommendation_edge_cases() -> None:
    """Snapshots-only Codex dir, Codex scoped limits, stale withholding, and `recommendation=None`."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with _isolated_env(root, fake_source=_FakeAccountSource([]), write_codex=False) as env:
            # No codex_accounts.json, only codex_quota_snapshots.json with scoped limit + stale row
            snaps = {
                "version": 1,
                "desktop_account_id": "acct-spark-aaaa1111",
                "accounts": {
                    "acct-spark-aaaa1111": {
                        "quota": {
                            "account_id": "acct-spark-aaaa1111",
                            "email": EMAIL_CANARY_CODEX,
                            "plan_type": "pro",
                            "five_hour": None,
                            "seven_day": {
                                "used_percent": 15.0,
                                "window_seconds": 604800,
                                "reset_at": FIXED_NOW + 4 * 3600,
                            },
                            "scoped": [
                                [
                                    "Spark",
                                    {
                                        "used_percent": 92.0,
                                        "window_seconds": 18000,
                                        "reset_at": FIXED_NOW + 2 * 3600,
                                    },
                                ]
                            ],
                            "limit_reached": False,
                            "allowed": True,
                            "reached_type": None,
                            "fetched_at": FIXED_NOW - 30,
                            "credits_balance": 0.0,
                            "credits_has": False,
                            "credits_would_enable": False,
                            "availability": [],
                            "reset_credits_available": 1,
                            "reset_credits_usable": 0,
                        },
                        "note": "",
                        "note_kind": "",
                    },
                    "acct-stale-bbbb2222": {
                        "quota": {
                            "account_id": "acct-stale-bbbb2222",
                            "email": EMAIL_CANARY_CODEX,
                            "plan_type": "pro",
                            "five_hour": None,
                            "seven_day": {
                                "used_percent": 99.0,
                                "window_seconds": 604800,
                                "reset_at": FIXED_NOW + 3600,
                            },
                            "scoped": [],
                            "limit_reached": True,
                            "allowed": False,
                            "reached_type": None,
                            "fetched_at": FIXED_NOW - 10 * 3600,  # >6h stale, not relogin
                            "credits_balance": 0.0,
                            "credits_has": False,
                            "credits_would_enable": False,
                            "availability": [],
                            "reset_credits_available": 4,
                            "reset_credits_usable": 2,
                        },
                        "note": "",
                        "note_kind": "",
                    },
                },
            }
            (env.widget_dir / "codex_quota_snapshots.json").write_text(
                json.dumps(snaps), encoding="utf-8"
            )
            codex_rows = bswap_cli._collect_codex_quota_rows(now=FIXED_NOW)
            assert len(codex_rows) == 2
            live_row = next(r for r in codex_rows if r.alias == "codex-acct-spa")
            stale_row = next(r for r in codex_rows if r.alias == "codex-acct-sta")
            assert live_row.is_active is True
            assert live_row.scoped_windows == (("Spark", 92.0),)
            assert live_row.reset_credits_available == 1 and live_row.reset_credits_usable == 0
            # Stale non-relogin reading withholds percentages and reset credits
            assert stale_row.seven_day_pct is None
            assert stale_row.reset_credits_available is None
            assert stale_row.reset_credits_usable is None

            # Verify formatting handles `recommendation=None` without raising
            radar = bswap_cli._build_fleet_reset_radar([], codex_rows, now=FIXED_NOW)
            radar_no_rec = SimpleNamespace(
                generated_at=radar.generated_at,
                events=radar.events,
                unlocks=radar.unlocks,
                perishable=radar.perishable,
                credits=radar.credits,
                capacity=radar.capacity,
                recommendation=None,
            )
            l1, l2 = bswap_cli._format_status_radar_lines(radar_no_rec)
            assert "Reset Radar:" in _plain(l1) and "Codex" in _plain(l2)
            payload = bswap_cli._radar_to_json(radar_no_rec, now_ts=FIXED_NOW)
            assert payload["recommendation"] is None
            assert EMAIL_CANARY_CODEX not in json.dumps(payload)


def main() -> int:
    tests = [
        test_bswap_resets_human_dashboard_and_strict_privacy,
        test_bswap_resets_json_schema_and_zero_leaks,
        test_strict_resets_module_contract_alignment,
        test_resets_alias_dispatch_and_flags,
        test_bswap_status_and_doctor_reset_radar_integration,
        test_help_documents_bswap_resets,
        test_bare_python3_no_claude_swap_compatibility,
        test_watch_tui_reset_radar_panel_integration,
        test_empty_fleet_resets_handles_gracefully,
        test_codex_snapshots_only_scoped_and_none_recommendation_edge_cases,
    ]
    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"pass  {fn.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed, out of {len(tests)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
