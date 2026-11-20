"""Standalone test suite for Fable-safe `bswap` CLI commands & isolation hardening.

Covers:
  - `bswap doctor` & `bswap doctor --fix` (policy misconfigs, 5-way Fleet Fable table,
    latched session `claude --resume` recovery commands, shim status, stale
    `~/.claude.json` `cachedExtraUsageDisabledReason` detection/clearing, LaunchAgent
    check, privacy check, and idempotency)
  - `bswap fable-guard [on|off|status]` (`SettingsStore` persistence + seat partition)
  - `_slot_is_healthy_for_session` Fable Guard enforcement + non-Fable fleet fallback
  - `bswap claude` Fable-safe selection, pinned non-Fable skip, all-Fable-capped safety,
    explicit override warning, and shim `--` double-dash strip
  - `bswap best` Fable Guard skip explanation, same-org affinity, perishable-quota
    priority, `--dry-run`, and all-Fable-capped protection
  - `bswap sessions` / `bswap fix-thinking` `⚡ LATCHED (credits)` surfacing with exact
    `claude --resume <session_id>` and refusing to restore/pin onto non-Fable Team seats
  - `bswap help` listing `bswap doctor [--fix]` and `bswap fable-guard [on|off|status]`
  - Full CI compatibility with `claude_swap` masked (`sys.modules['claude_swap'] = None`)

Run directly::

    CC_USAGE_WIDGET_NO_REVEAL=1 python tests/test_bswap_fable_cli.py
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

from cc_usage_widget import accounts as accounts_mod  # noqa: E402
from cc_usage_widget import bswap_cli  # noqa: E402
from cc_usage_widget import fleet as fleet_mod  # noqa: E402
from cc_usage_widget.contracts import VENDOR_CLAUDE, AccountRow  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return ANSI_RE.sub("", text)


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(
    slot: int,
    alias: str,
    *,
    is_active: bool = False,
    plan_type: str | None = "claude_max_20x",
    five_hour_pct: float | None = 10.0,
    seven_day_pct: float | None = 25.0,
    scoped_windows: tuple[tuple[str, float], ...] = (),
    scoped_resets_at: tuple[tuple[str, str], ...] = (),
    seven_day_resets_at: str | None = None,
    disabled: bool = False,
    switchable: bool = True,
    attention_note: str = "",
) -> AccountRow:
    return AccountRow(
        slot=slot,
        alias=alias,
        email=f"{alias}@example.com",
        is_active=is_active,
        five_hour_pct=five_hour_pct,
        seven_day_pct=seven_day_pct,
        scoped_windows=scoped_windows,
        scoped_resets_at=scoped_resets_at,
        seven_day_resets_at=seven_day_resets_at,
        disabled=disabled,
        switchable=switchable,
        plan_type=plan_type,
        attention_note=attention_note,
        vendor=VENDOR_CLAUDE,
    )


@dataclass(frozen=True)
class _FakeSession:
    """Session stub exposing future `engine` fields (`session_id`, `latched_credits`, `hit_rate_limit`)."""

    pid: int
    cwd: str
    name: str = ""
    status: str = "idle"
    started_at: float = 0.0
    slot: int | None = None
    pinned: bool = False
    mapped_slot: int | None = None
    mapped_email: str = ""
    pinned_here: bool = False
    owner_slot: int | None = None
    org_shifted: bool = False
    session_id: str = ""
    latched_credits: bool = False
    hit_rate_limit: bool = False


class _FakeAccountSource:
    def __init__(
        self,
        rows: list[AccountRow],
        *,
        sentinels: dict[int, str] | None = None,
        autoswitch_on: bool = True,
        model: str = "all",
        burn_rate: float | None = None,
        burn_eta: float | None = None,
    ) -> None:
        self._rows = list(rows)
        self._sentinels = dict(sentinels or {})
        self._autoswitch_on = autoswitch_on
        self._model = model
        self._burn_rate = burn_rate
        self._burn_eta = burn_eta
        self.switched_to: list[str] = []
        self.last_error: str | None = None

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
        return 85.0

    def autoswitch_enabled(self) -> bool:
        return self._autoswitch_on

    def autoswitch_model(self) -> str:
        return self._model

    def switch_to(self, target: str) -> bool:
        self.switched_to.append(str(target))
        return True

    def fable_burn_rate_pct_per_min(self, slot: int | None = None) -> float | None:
        _ = slot
        return self._burn_rate

    def fable_burn_eta_minutes(self, slot: int | None = None) -> float | None:
        _ = slot
        return self._burn_eta

    def fable_guard_status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "fable_slots": [r.slot for r in self._rows if accounts_mod.row_has_fable_quota(r)],
            "excluded_slots": [r.slot for r in self._rows if accounts_mod.row_lacks_fable_quota(r)],
            "active_eta_min": self._burn_eta,
            "active_burn_pct_per_min": self._burn_rate,
        }


@contextlib.contextmanager
def _isolated_cli(
    root: Path,
    *,
    fake_source: Any | None = None,
    fake_snap: Any | None = None,
    identities: dict[int, tuple[str, str]] | None = None,
    mappings: tuple[Any, ...] = (),
):
    """Isolate all CLI paths and external backends inside *root* so $HOME is never touched."""
    backup_dir = root / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    widget_dir = root / "widget"
    widget_dir.mkdir(parents=True, exist_ok=True)
    shims_dir = widget_dir / "shims"
    sessions_dir = root / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    claude_cfg = root / ".claude.json"

    if identities is not None:
        seq = {
            "accounts": {
                str(slot): {"email": email, "organizationUuid": org}
                for slot, (email, org) in identities.items()
            }
        }
        (backup_dir / "sequence.json").write_text(json.dumps(seq), encoding="utf-8")

    orig_widget_dir = bswap_cli.WIDGET_DIR
    orig_shims_dir = bswap_cli.SHIMS_DIR
    orig_backup_dir = bswap_cli._backup_dir
    orig_claude_cfg = bswap_cli._claude_config_path
    orig_create_src = accounts_mod.create_account_source
    orig_collect = fleet_mod.collect
    orig_load_mappings = fleet_mod.load_mappings
    orig_set_mapping = fleet_mod.set_mapping
    orig_detect_owner = fleet_mod.detect_workspace_owner_slot
    orig_sessions_env = os.environ.get("CC_USAGE_WIDGET_SESSIONS_DIR")

    pinned_calls: list[tuple[Path, str, str, str]] = []

    try:
        bswap_cli.WIDGET_DIR = widget_dir
        bswap_cli.SHIMS_DIR = shims_dir
        bswap_cli._backup_dir = lambda: backup_dir  # type: ignore[assignment]
        bswap_cli._claude_config_path = lambda _src=None: claude_cfg  # type: ignore[assignment]
        os.environ["CC_USAGE_WIDGET_SESSIONS_DIR"] = str(sessions_dir)

        if fake_source is not None:
            accounts_mod.create_account_source = lambda *_a, **_kw: fake_source  # type: ignore[assignment]
        if fake_snap is not None:
            fleet_mod.collect = lambda **_kw: fake_snap  # type: ignore[assignment]
        if mappings is not None:
            fleet_mod.load_mappings = lambda _b, _id=None: tuple(mappings)  # type: ignore[assignment]
        fleet_mod.set_mapping = lambda b, p, e, o: pinned_calls.append((Path(b), str(p), e, o))  # type: ignore[assignment]

        yield SimpleNamespace(
            root=root,
            backup_dir=backup_dir,
            widget_dir=widget_dir,
            shims_dir=shims_dir,
            sessions_dir=sessions_dir,
            claude_cfg=claude_cfg,
            pinned_calls=pinned_calls,
        )
    finally:
        bswap_cli.WIDGET_DIR = orig_widget_dir
        bswap_cli.SHIMS_DIR = orig_shims_dir
        bswap_cli._backup_dir = orig_backup_dir
        bswap_cli._claude_config_path = orig_claude_cfg
        accounts_mod.create_account_source = orig_create_src
        fleet_mod.collect = orig_collect
        fleet_mod.load_mappings = orig_load_mappings
        fleet_mod.set_mapping = orig_set_mapping
        fleet_mod.detect_workspace_owner_slot = orig_detect_owner
        if orig_sessions_env is None:
            os.environ.pop("CC_USAGE_WIDGET_SESSIONS_DIR", None)
        else:
            os.environ["CC_USAGE_WIDGET_SESSIONS_DIR"] = orig_sessions_env


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bswap_doctor_flags_misconfigs_classifies_fleet_and_reports_latched_sessions() -> None:
    now = dt.datetime.now().timestamp()
    soon_iso = _iso(now + 6 * 3600)
    later_iso = _iso(now + 3 * 86400)

    rows = [
        _row(
            1,
            "vlad",
            is_active=True,
            plan_type="claude_max_20x",
            five_hour_pct=20.0,
            seven_day_pct=30.0,
            scoped_windows=(("Fable", 35.0),),
            scoped_resets_at=(("Fable", soon_iso),),
            seven_day_resets_at=soon_iso,
        ),
        _row(
            2,
            "capped-max",
            plan_type="claude_max_20x",
            five_hour_pct=15.0,
            seven_day_pct=50.0,
            scoped_windows=(("Fable", 96.0),),
            scoped_resets_at=(("Fable", later_iso),),
            seven_day_resets_at=later_iso,
        ),
        _row(
            4,
            "work1",
            plan_type="claude_team",
            five_hour_pct=10.0,
            seven_day_pct=18.0,
            scoped_windows=(),
            seven_day_resets_at=later_iso,
        ),
        _row(
            5,
            "paused-seat",
            plan_type="claude_max_20x",
            scoped_windows=(("Fable", 10.0),),
            disabled=True,
        ),
        _row(
            6,
            "expired-seat",
            plan_type="claude_max_20x",
            scoped_windows=(),
        ),
    ]
    fake_src = _FakeAccountSource(
        rows,
        sentinels={6: "re-login needed"},
        autoswitch_on=True,
        burn_rate=1.6,
        burn_eta=19.0,
    )
    fake_snap = fleet_mod.FleetSnapshot(
        sessions=(
            _FakeSession(
                pid=4101,
                cwd="/work/belkins-app",
                status="idle",
                slot=1,
                session_id="sess-uuid-42",
                latched_credits=True,
            ),
            _FakeSession(
                pid=4102,
                cwd="/work/AI Products/usagebar",
                status="idle",
                slot=1,
                session_id="",
                latched_credits=True,
            ),
            _FakeSession(
                pid=4103,
                cwd="/work/crm",
                status="busy",
                slot=1,
                owner_slot=2,
                org_shifted=True,
            ),
        ),
    )

    secret_canary = "SECRET_OAUTH_TOKEN_NEVER_PRINT_9921"
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_cli(Path(tmp), fake_source=fake_src, fake_snap=fake_snap) as env:
            # Misconfigured autoswitch policy
            pol_path = env.backup_dir / "settings.json"
            orig_pol = {
                "schemaVersion": 1,
                "autoswitch": {
                    "enabled": True,
                    "threshold": 90.0,
                    "weeklyThreshold": 95.0,
                    "intervalSeconds": 60.0,
                    "cooldownSeconds": 300.0,
                    "hysteresisPct": 5.0,
                },
            }
            pol_path.write_text(json.dumps(orig_pol), encoding="utf-8")

            # Widget settings with Fable Guard OFF
            (env.widget_dir / "settings.json").write_text(
                json.dumps({"belkins_fable_guard": False}), encoding="utf-8"
            )

            # Stale credit cache + canary secret that must NEVER appear in stdout
            env.claude_cfg.write_text(
                json.dumps(
                    {
                        "cachedExtraUsageDisabledReason": "out_of_credits",
                        "oauthToken": secret_canary,
                        "theme": "dark",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(env.claude_cfg, 0o600)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bswap_cli.main(["doctor"])

            out = _plain(buf.getvalue())
            assert rc == 0, out
            # 1. Policy checks flagged
            assert "autoswitch.model is unset" in out, out
            assert "weeklyThreshold=95% >= 92%" in out, out
            assert "cooldownSeconds=300s >= 300s" in out, out
            assert "intervalSeconds=60s > 30s" in out, out
            assert "hysteresisPct=5% < 10%" in out, out
            assert "belkins_fable_guard is OFF" in out, out
            # Live burn telemetry from engine contract
            assert "1.60%/min" in out and "~19m to wall" in out, out

            # 2. All 5 fleet capability classes
            assert "FABLE READY" in out, out
            assert "Fable 65% free" in out, out
            assert "FABLE LOW/CAPPED" in out, out
            assert "NO FABLE" in out and "work1" in out, out
            assert "DISABLED" in out and "paused-seat" in out, out
            assert "RE-LOGIN NEEDED" in out and "expired-seat" in out, out

            # 3. Live session health + exact recovery commands + shim status
            assert "LATCHED (credits)" in out, out
            assert "cd /work/belkins-app && claude --resume sess-uuid-42" in out, out
            assert "cd '/work/AI Products/usagebar' && claude --resume" in out, out
            assert "ORG SHIFTED" in out, out
            assert "bswap shim:" in out, out

            # 4. Stale credit cache + LaunchAgent + strict privacy
            assert "cachedExtraUsageDisabledReason=out_of_credits" in out, out
            assert "com.cc-usage-widget" in out, out
            assert secret_canary not in out, "Secret key from ~/.claude.json leaked!"

            # Read-only doctor MUST NOT mutate files on disk
            assert json.loads(pol_path.read_text(encoding="utf-8")) == orig_pol
            assert "cachedExtraUsageDisabledReason" in json.loads(
                env.claude_cfg.read_text(encoding="utf-8")
            )


def test_bswap_doctor_fix_heals_policy_credit_cache_and_fable_guard() -> None:
    rows = [
        _row(1, "vlad", is_active=True, plan_type="claude_max_20x", scoped_windows=(("Fable", 30.0),)),
        _row(4, "work1", plan_type="claude_team", seven_day_pct=15.0, scoped_windows=()),
    ]
    fake_src = _FakeAccountSource(rows, autoswitch_on=True)
    fake_snap = fleet_mod.FleetSnapshot()

    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_cli(Path(tmp), fake_source=fake_src, fake_snap=fake_snap) as env:
            pol_path = env.backup_dir / "settings.json"
            pol_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "autoswitch": {
                            "enabled": True,
                            "threshold": 88.0,
                            "weeklyThreshold": 96.0,
                            "intervalSeconds": 90.0,
                            "cooldownSeconds": 600.0,
                            "hysteresisPct": 4.0,
                            "model": "",
                        },
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(pol_path, 0o600)
            (env.widget_dir / "settings.json").write_text(
                json.dumps({"belkins_fable_guard": False}), encoding="utf-8"
            )
            env.claude_cfg.write_text(
                json.dumps(
                    {
                        "cachedExtraUsageDisabledReason": "org_level_disabled",
                        "theme": "dark",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(env.claude_cfg, 0o600)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bswap_cli.main(["doctor", "--fix"])
            out = _plain(buf.getvalue())
            assert rc == 0, out

            # Verify healed settings.json
            healed_auto = json.loads(pol_path.read_text(encoding="utf-8"))["autoswitch"]
            assert healed_auto["model"] == "all", healed_auto
            assert healed_auto["weeklyThreshold"] == 88.0, healed_auto
            assert healed_auto["cooldownSeconds"] == 120.0, healed_auto
            assert healed_auto["intervalSeconds"] == 30.0, healed_auto
            assert healed_auto["hysteresisPct"] == 10.0, healed_auto

            # Verify stale credit cache cleared atomically while preserving other keys & 0o600 mode
            healed_cfg = json.loads(env.claude_cfg.read_text(encoding="utf-8"))
            assert "cachedExtraUsageDisabledReason" not in healed_cfg, healed_cfg
            assert healed_cfg["theme"] == "dark", healed_cfg
            assert stat.S_IMODE(env.claude_cfg.stat().st_mode) == 0o600

            # Verify belkins_fable_guard re-enabled
            healed_widget = json.loads((env.widget_dir / "settings.json").read_text(encoding="utf-8"))
            assert healed_widget["belkins_fable_guard"] is True, healed_widget

            # Verify healed output
            assert "Auto-Heal (--fix)" in out, out
            assert "Updated claude-swap autoswitch policy" in out, out
            assert "Cleared stale cachedExtraUsageDisabledReason" in out, out
            assert "Enabled belkins_fable_guard=True" in out, out
            assert "NO FABLE (Team/Pro — skipped by Fable Guard)" in out, out
            assert "Optimal Fable-safe auto-switch policy active" in out, out
            assert "cachedExtraUsageDisabledReason: clean" in out, out

            # Second --fix run is idempotent
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = bswap_cli.main(["doctor", "--fix"])
            out2 = _plain(buf2.getvalue())
            assert rc2 == 0
            assert "Nothing needed fixing" in out2, out2


def test_bswap_fable_guard_cli_on_off_status() -> None:
    rows = [
        _row(2, "vlad", is_active=True, plan_type="claude_max_20x", scoped_windows=(("Fable", 40.0),)),
        _row(4, "work1", plan_type="claude_team", seven_day_pct=20.0, scoped_windows=()),
        _row(9, "d.max", plan_type="claude_max_20x", scoped_windows=(("Fable", 15.0),)),
    ]
    fake_src = _FakeAccountSource(rows, burn_rate=0.9, burn_eta=45.0)

    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_cli(Path(tmp), fake_source=fake_src) as env:
            # Default status is ON (read-only, does not seed file)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bswap_cli.main(["fable-guard", "status"])
            out = _plain(buf.getvalue())
            assert rc == 0
            assert "ON (PROTECTED)" in out, out
            assert "Protected Fable Seats (2)" in out, out
            assert "Excluded Non-Fable Seats (1)" in out, out
            assert "skipped by Fable Guard" in out, out
            assert not (env.widget_dir / "settings.json").exists()

            # Toggle OFF
            buf_off = io.StringIO()
            with contextlib.redirect_stdout(buf_off):
                rc_off = bswap_cli.main(["fable-guard", "off"])
            out_off = _plain(buf_off.getvalue())
            assert rc_off == 0
            assert "DISABLED" in out_off and "NOT skipped" in out_off, out_off
            saved_off = json.loads((env.widget_dir / "settings.json").read_text(encoding="utf-8"))
            assert saved_off["belkins_fable_guard"] is False

            # Toggle ON
            buf_on = io.StringIO()
            with contextlib.redirect_stdout(buf_on):
                rc_on = bswap_cli.main(["fable-guard", "on"])
            out_on = _plain(buf_on.getvalue())
            assert rc_on == 0
            assert "ENABLED" in out_on and "skipped by Fable Guard" in out_on, out_on
            saved_on = json.loads((env.widget_dir / "settings.json").read_text(encoding="utf-8"))
            assert saved_on["belkins_fable_guard"] is True

            # Invalid action returns exit 2
            buf_err = io.StringIO()
            with contextlib.redirect_stderr(buf_err):
                rc_bad = bswap_cli.main(["fable-guard", "bogus"])
            assert rc_bad == 2


def test_slot_is_healthy_for_session_fable_guard_and_non_fable_fleet_fallback() -> None:
    fable_healthy = _row(2, "vlad", plan_type="claude_max_20x", five_hour_pct=10.0, seven_day_pct=20.0, scoped_windows=(("Fable", 40.0),))
    fable_capped = _row(3, "capped", plan_type="claude_max_20x", five_hour_pct=10.0, seven_day_pct=20.0, scoped_windows=(("Fable", 96.0),))
    team_seat = _row(4, "work1", plan_type="claude_team", five_hour_pct=5.0, seven_day_pct=10.0, scoped_windows=())
    pro_seat = _row(5, "pro1", plan_type="claude_pro", five_hour_pct=5.0, seven_day_pct=10.0, scoped_windows=())
    disabled_fable = _row(6, "dis", plan_type="claude_max_20x", scoped_windows=(("Fable", 0.0),), disabled=True)

    mixed_fleet = [fable_healthy, fable_capped, team_seat, disabled_fable]
    non_fable_fleet = [team_seat, pro_seat]

    # Mixed fleet + Fable Guard ON -> Team seat rejected; healthy Fable accepted; capped/disabled rejected
    assert bswap_cli._slot_is_healthy_for_session(team_seat, {}, fleet_rows=mixed_fleet, fable_guard=True) is False
    assert bswap_cli._slot_is_healthy_for_session(fable_healthy, {}, fleet_rows=mixed_fleet, fable_guard=True) is True
    assert bswap_cli._slot_is_healthy_for_session(fable_capped, {}, fleet_rows=mixed_fleet, fable_guard=True) is False
    assert bswap_cli._slot_is_healthy_for_session(disabled_fable, {}, fleet_rows=mixed_fleet, fable_guard=True) is False
    assert bswap_cli._slot_is_healthy_for_session(fable_healthy, {2: "re-login needed"}, fleet_rows=mixed_fleet, fable_guard=True) is False

    # Mixed fleet + Fable Guard OFF -> Team seat allowed
    assert bswap_cli._slot_is_healthy_for_session(team_seat, {}, fleet_rows=mixed_fleet, fable_guard=False) is True

    # Pure non-Fable fleet + Fable Guard ON -> gracefully falls back to allowing Team/Pro seats!
    assert bswap_cli._slot_is_healthy_for_session(team_seat, {}, fleet_rows=non_fable_fleet, fable_guard=True) is True
    assert bswap_cli._slot_is_healthy_for_session(pro_seat, {}, fleet_rows=non_fable_fleet, fable_guard=True) is True


def test_bswap_claude_never_launches_on_non_fable_team_seat_and_strips_shim_double_dash() -> None:
    fable_a = _row(7, "ana", is_active=False, plan_type="claude_max_20x", five_hour_pct=10.0, seven_day_pct=30.0, scoped_windows=(("Fable", 45.0),))
    fable_b = _row(9, "dmax", is_active=False, plan_type="claude_max_20x", five_hour_pct=5.0, seven_day_pct=25.0, scoped_windows=(("Fable", 35.0),))
    team_pinned = _row(4, "work1", is_active=True, plan_type="claude_team", five_hour_pct=2.0, seven_day_pct=5.0, scoped_windows=())

    identities = {
        4: ("work1@belkins.io", "org-team"),
        7: ("ana@belkins.io", "org-max-1"),
        9: ("dmax@belkins.io", "org-max-2"),
    }
    cwd_norm = os.path.normcase(str(Path.cwd().resolve()))
    team_pin = fleet_mod.MappingRow(path=cwd_norm, email="work1@belkins.io", org_uuid="org-team", slot=4)

    launched_cmds: list[list[str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        fake_src = _FakeAccountSource([team_pinned, fable_a, fable_b])
        with _isolated_cli(Path(tmp), fake_source=fake_src, identities=identities, mappings=(team_pin,)) as _env:
            orig_find = bswap_cli._find_cswap
            orig_run = subprocess.run
            fleet_mod.detect_workspace_owner_slot = lambda *_a, **_kw: (4, False)  # type: ignore[assignment]
            try:
                bswap_cli._find_cswap = lambda: Path("/fake/cswap")  # type: ignore[assignment]
                subprocess.run = lambda cmd, **_kw: (launched_cmds.append(list(cmd)), SimpleNamespace(returncode=0))[1]  # type: ignore[assignment]

                # 1. Shim passes `claude -- --resume sess-999`: must skip pinned Team #4, pick Fable #9,
                # and NOT emit duplicate `-- --`!
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = bswap_cli.main(["claude", "--", "--resume", "sess-999"])
                out = _plain(buf.getvalue())
                assert rc == 0, out
                assert len(launched_cmds) == 1
                assert launched_cmds[0] == ["/fake/cswap", "run", "9", "--share-history", "--", "--resume", "sess-999"], launched_cmds[0]
                assert "skipped pinned non-Fable Slot #4" in out, out

                # 2. Even when ALL Fable seats are 100% capped, `bswap claude` still refuses Team #4
                # while Fable Guard is ON!
                launched_cmds.clear()
                capped_a = _row(7, "ana", plan_type="claude_max_20x", five_hour_pct=10.0, seven_day_pct=50.0, scoped_windows=(("Fable", 100.0),))
                capped_b = _row(9, "dmax", plan_type="claude_max_20x", five_hour_pct=10.0, seven_day_pct=50.0, scoped_windows=(("Fable", 100.0),))
                fake_src._rows = [team_pinned, capped_a, capped_b]
                buf2 = io.StringIO()
                with contextlib.redirect_stdout(buf2):
                    rc2 = bswap_cli.main(["claude"])
                assert rc2 == 0
                assert launched_cmds[0][2] in {"7", "9"}, f"Must not launch on Team #4: {launched_cmds[0]}"

                # 3. Pure non-Fable fleet falls back cleanly onto Team #4
                launched_cmds.clear()
                fake_src._rows = [team_pinned]
                buf3 = io.StringIO()
                with contextlib.redirect_stdout(buf3):
                    rc3 = bswap_cli.main(["claude"])
                assert rc3 == 0
                assert launched_cmds[0][2] == "4", launched_cmds[0]

                # 4. Explicit slot override `bswap claude 4` is honoured with a warning
                launched_cmds.clear()
                fake_src._rows = [team_pinned, fable_a, fable_b]
                buf4 = io.StringIO()
                with contextlib.redirect_stdout(buf4):
                    rc4 = bswap_cli.main(["claude", "4", "--", "-p", "hi"])
                out4 = _plain(buf4.getvalue())
                assert rc4 == 0
                assert launched_cmds[0] == ["/fake/cswap", "run", "4", "--share-history", "--", "-p", "hi"], launched_cmds[0]
                assert "lacks included Fable quota" in out4, out4
            finally:
                bswap_cli._find_cswap = orig_find
                subprocess.run = orig_run


def test_bswap_best_skips_higher_7d_non_fable_honors_same_org_and_perishable_quota() -> None:
    now = dt.datetime.now().timestamp()
    far_iso = _iso(now + 6 * 86400)
    soon_iso = _iso(now + 6 * 3600)

    # Team seat #4 has 88% 7d free (much higher 7d than Max seats), active is #2 in org-alpha
    active_low = _row(2, "vlad", is_active=True, plan_type="claude_max_20x", five_hour_pct=20.0, seven_day_pct=40.0, scoped_windows=(("Fable", 90.0),), seven_day_resets_at=far_iso)
    team_high_7d = _row(4, "work1", plan_type="claude_team", five_hour_pct=5.0, seven_day_pct=12.0, scoped_windows=(), seven_day_resets_at=far_iso)
    same_org_max = _row(7, "ana-same-org", plan_type="claude_max_20x", five_hour_pct=15.0, seven_day_pct=30.0, scoped_windows=(("Fable", 42.0),), seven_day_resets_at=far_iso)
    other_org_max = _row(9, "d-other-org", plan_type="claude_max_20x", five_hour_pct=10.0, seven_day_pct=25.0, scoped_windows=(("Fable", 38.0),), seven_day_resets_at=far_iso)

    identities = {
        2: ("vlad@belkins.io", "org-alpha"),
        4: ("work1@belkins.io", "org-team"),
        7: ("ana@belkins.io", "org-alpha"),
        9: ("d@belkins.io", "org-beta"),
    }

    with tempfile.TemporaryDirectory() as tmp:
        fake_src = _FakeAccountSource([active_low, team_high_7d, same_org_max, other_org_max])
        with _isolated_cli(Path(tmp), fake_source=fake_src, identities=identities):
            fleet_mod.detect_workspace_owner_slot = lambda *_a, **_kw: (2, False)  # type: ignore[assignment]

            # 1. --dry-run does not switch, explains skipped #4 work1, picks same-org #7
            buf_dry = io.StringIO()
            with contextlib.redirect_stdout(buf_dry):
                rc_dry = bswap_cli.main(["best", "--dry-run"])
            out_dry = _plain(buf_dry.getvalue())
            assert rc_dry == 0, out_dry
            assert fake_src.switched_to == []
            assert "Fable Guard skipped higher-7d non-Fable seat(s)" in out_dry and "#4 work1" in out_dry, out_dry
            assert "Slot #7 (ana-same-org)" in out_dry and "same-org" in out_dry, out_dry

            # 2. Live `bswap best` switches to #7 (same-org Fable seat), never #4 Team!
            buf_live = io.StringIO()
            with contextlib.redirect_stdout(buf_live):
                rc_live = bswap_cli.main(["best"])
            out_live = _plain(buf_live.getvalue())
            assert rc_live == 0, out_live
            assert fake_src.switched_to == ["7"], fake_src.switched_to
            assert "Fable Guard skipped higher-7d non-Fable seat(s)" in out_live, out_live

            # 3. Perishable-quota priority: expiring seat #8 (6h left, 55% free) beats cold #9 (6d left)
            fake_src.switched_to.clear()
            expiring_max = _row(8, "expiring-max", plan_type="claude_max_20x", five_hour_pct=15.0, seven_day_pct=35.0, scoped_windows=(("Fable", 42.0),), seven_day_resets_at=soon_iso)
            fake_src._rows = [active_low, team_high_7d, expiring_max, other_org_max]
            fleet_mod.detect_workspace_owner_slot = lambda *_a, **_kw: (None, False)  # type: ignore[assignment]
            buf_perish = io.StringIO()
            with contextlib.redirect_stdout(buf_perish):
                rc_p = bswap_cli.main(["best"])
            out_p = _plain(buf_perish.getvalue())
            assert rc_p == 0, out_p
            assert fake_src.switched_to == ["8"], fake_src.switched_to
            assert "perishable-quota priority" in out_p, out_p

            # 4. All Fable seats 100% capped -> refuses to land on Team #4!
            fake_src.switched_to.clear()
            capped_max = _row(2, "vlad", is_active=True, plan_type="claude_max_20x", scoped_windows=(("Fable", 100.0),))
            fake_src._rows = [capped_max, team_high_7d]
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc_cap = bswap_cli.main(["best"])
            assert rc_cap == 1
            assert fake_src.switched_to == []
            assert "No healthy Fable-capable Claude account available" in _plain(buf_err.getvalue())


def test_bswap_sessions_surfaces_latched_resume_and_never_restores_non_fable() -> None:
    fable_active = _row(2, "vlad", is_active=True, plan_type="claude_max_20x", scoped_windows=(("Fable", 40.0),))
    team_owner = _row(4, "work1", plan_type="claude_team", five_hour_pct=5.0, seven_day_pct=10.0, scoped_windows=())
    fable_other = _row(7, "ana", plan_type="claude_max_20x", scoped_windows=(("Fable", 20.0),))

    identities = {
        2: ("vlad@belkins.io", "org-vlad"),
        4: ("work1@belkins.io", "org-team-only"),
        7: ("ana@belkins.io", "org-ana"),
    }

    fake_snap = fleet_mod.FleetSnapshot(
        sessions=(
            _FakeSession(
                pid=9001,
                cwd="/work/crm",
                status="idle",
                slot=2,
                owner_slot=2,
                org_shifted=False,
                session_id="abc-latched-77",
                latched_credits=True,
            ),
            _FakeSession(
                pid=9002,
                cwd="/work/team-repo",
                status="busy",
                slot=2,
                owner_slot=4,
                org_shifted=True,
            ),
        ),
    )

    with tempfile.TemporaryDirectory() as tmp:
        fake_src = _FakeAccountSource([fable_active, team_owner, fable_other])
        with _isolated_cli(Path(tmp), fake_source=fake_src, fake_snap=fake_snap, identities=identities) as env:
            # 1. Read-only `bswap sessions`
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bswap_cli.main(["sessions"])
            out = _plain(buf.getvalue())
            assert rc == 0, out
            assert "⚡ LATCHED (credits)" in out, out
            assert "cd /work/crm && claude --resume abc-latched-77" in out, out
            assert "Fable Guard skipped restoring onto non-Fable seat(s): #4 (work1)" in out, out
            assert "restore Slot #4" not in out, out
            assert not (env.backup_dir / "settings.json").exists(), "Read-only sessions must not write settings.json"

            # 2. `bswap sessions --fix` still MUST NOT switch onto or pin non-Fable Team #4!
            buf_fix = io.StringIO()
            with contextlib.redirect_stdout(buf_fix):
                rc_fix = bswap_cli.main(["sessions", "--fix"])
            out_fix = _plain(buf_fix.getvalue())
            assert rc_fix == 0, out_fix
            assert "4" not in fake_src.switched_to, fake_src.switched_to
            assert all(call[2] != "work1@belkins.io" for call in env.pinned_calls), env.pinned_calls

            # 3. Healthy Fable owner (#7 ana) IS recommended and restored cleanly by `--fix`
            fake_src.switched_to.clear()
            env.pinned_calls.clear()
            fable_owner_snap = fleet_mod.FleetSnapshot(
                sessions=(
                    _FakeSession(
                        pid=9003,
                        cwd="/work/ana-proj",
                        status="busy",
                        slot=2,
                        owner_slot=7,
                        org_shifted=True,
                    ),
                )
            )
            fleet_mod.collect = lambda **_kw: fable_owner_snap  # type: ignore[assignment]
            buf_fable = io.StringIO()
            with contextlib.redirect_stdout(buf_fable):
                rc_f = bswap_cli.main(["sessions", "--fix"])
            out_f = _plain(buf_fable.getvalue())
            assert rc_f == 0, out_f
            assert fake_src.switched_to == ["7"], fake_src.switched_to
            assert "Restored global login to Slot #7 (ana)" in out_f, out_f


def test_help_mentions_doctor_and_fable_guard() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        with _isolated_cli(Path(tmp), fake_source=_FakeAccountSource([])):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = bswap_cli.main(["help"])
            out = _plain(buf.getvalue())
            assert rc == 0
            assert "bswap doctor [--fix]" in out, out
            assert "bswap fable-guard [on|off|status]" in out, out


def test_ci_compatibility_without_claude_swap() -> None:
    """Every new & hardened bswap CLI command works cleanly when `claude_swap` is absent."""
    saved_modules: dict[str, Any] = {}
    block_keys = [
        k for k in list(sys.modules) if k == "claude_swap" or k.startswith("claude_swap.")
    ]
    for k in (
        "claude_swap",
        "claude_swap.settings",
        "claude_swap.paths",
        "claude_swap.session",
        "claude_swap.mappings",
        "claude_swap.process_detection",
        "claude_swap.switcher",
        "claude_swap.snapshot_source",
        "claude_swap.autoswitch",
        "claude_swap.oauth",
        "claude_swap.menubar",
    ):
        if k not in block_keys:
            block_keys.append(k)

    for k in block_keys:
        saved_modules[k] = sys.modules.get(k)
        sys.modules[k] = None  # type: ignore[assignment]

    try:
        with tempfile.TemporaryDirectory() as tmp:
            with _isolated_cli(Path(tmp)) as env:
                pol_path = env.backup_dir / "settings.json"
                pol_path.write_text(
                    json.dumps({"autoswitch": {"weeklyThreshold": 96.0, "cooldownSeconds": 600.0}}),
                    encoding="utf-8",
                )
                env.claude_cfg.write_text(
                    json.dumps({"cachedExtraUsageDisabledReason": "out_of_credits", "ok": True}),
                    encoding="utf-8",
                )
                (env.widget_dir / "settings.json").write_text(
                    json.dumps({"belkins_fable_guard": False}), encoding="utf-8"
                )

                # doctor (read-only)
                buf1 = io.StringIO()
                with contextlib.redirect_stdout(buf1):
                    assert bswap_cli.main(["doctor"]) == 0
                out1 = _plain(buf1.getvalue())
                assert "BELKINS FABLE & AUTO-SWITCH DOCTOR" in out1
                assert "com.cc-usage-widget" in out1

                # doctor --fix works via stdlib atomic fallback without claude_swap!
                buf2 = io.StringIO()
                with contextlib.redirect_stdout(buf2):
                    assert bswap_cli.main(["doctor", "--fix"]) == 0
                healed = json.loads(pol_path.read_text(encoding="utf-8"))["autoswitch"]
                assert healed["model"] == "all"
                assert healed["weeklyThreshold"] == 88.0
                assert healed["cooldownSeconds"] == 120.0
                assert healed["intervalSeconds"] == 30.0
                assert "cachedExtraUsageDisabledReason" not in json.loads(
                    env.claude_cfg.read_text(encoding="utf-8")
                )
                assert json.loads((env.widget_dir / "settings.json").read_text(encoding="utf-8"))[
                    "belkins_fable_guard"
                ] is True

                # fable-guard off / on / status
                buf3 = io.StringIO()
                with contextlib.redirect_stdout(buf3):
                    assert bswap_cli.main(["fable-guard", "off"]) == 0
                    assert bswap_cli.main(["fable-guard", "on"]) == 0
                    assert bswap_cli.main(["fable-guard", "status"]) == 0

                # sessions
                buf4 = io.StringIO()
                with contextlib.redirect_stdout(buf4):
                    assert bswap_cli.main(["sessions"]) == 0

                # shim on / status / off
                buf5 = io.StringIO()
                with contextlib.redirect_stdout(buf5):
                    assert bswap_cli.main(["shim", "on"]) == 0
                    assert bswap_cli.main(["shim", "status"]) == 0
                    assert bswap_cli.main(["shim", "off"]) == 0
    finally:
        for k, orig in saved_modules.items():
            if orig is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = orig


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
        print(f"ERROR: {len(orphans)} test(s) sit below __main__: " + ", ".join(orphans))
    return 1 if (failures or orphans) else 0


if __name__ == "__main__":
    raise SystemExit(main())
