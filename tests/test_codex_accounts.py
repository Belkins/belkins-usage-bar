"""Live per-account Codex quota (SPEC-CODEX 6) - contracts, mapping, polling.

Three layers, in the order they were built:

1. **Guard rails** - the arbitration rule between the transcript-derived Codex
   row and the live per-account rows, the new settings keys, and the promise
   that no credential path is committable.
2. **The mapper** - a response body becomes a :class:`CodexAccountQuota` and
   then an ``AccountRow``. Window identity is the WIDTH, never the position;
   a reached limit never becomes a percentage; an answer about another account
   is dropped rather than shown under the wrong alias.
3. **The poller** - every status class, both ``Retry-After`` forms, the
   three-strike rule, token expiry, ageing, cold start, the sleep/wake
   detector, per-account backoff isolation, the active-login marker and the
   registry.

Everything in layer 3 runs through a ``FakeTransport`` and a ``FakeClock``
injected into the constructor, over **real files in a ``TemporaryDirectory``**
- no monkeypatched parser, no patched ``urllib``, no network, and no
``CC_USAGE_WIDGET_*`` env override, because every path this module touches is
a constructor argument. Nothing here reads or writes ``~/.codex`` or the live
widget home; the only access outside the temp dir is ``git`` metadata of this
checkout (skipped when there is none).

Run directly, or with pytest if it is installed::

    PY=~/.local/share/uv/tools/claude-swap/bin/python
    $PY tests/test_codex_accounts.py
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")  # never open Finder from a test

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget.codex_accounts import (  # noqa: E402
    NOTE_ENDPOINT_ERROR,
    NOTE_NO_ACCESS,
    NOTE_NO_CREDENTIAL,
    NOTE_OFFLINE,
    NOTE_PENDING,
    NOTE_RATE_LIMITED,
    NOTE_RELOGIN,
    NOTE_VIA_DESKTOP,
    REFRESH_GUARD_UNKNOWN,
    REFRESH_RACED,
    CodexAccountQuota,
    CodexAccountsSource,
    Credential,
    CredentialStore,
    HttpResponse,
    Registry,
    RegistryEntry,
    TokenRefresher,
    decode_jwt_claims,
    main as accounts_main,
    refresh_due,
)
from cc_usage_widget.contracts import (  # noqa: E402
    CODEX_FETCH_EXPIRE_SECONDS,
    CODEX_FETCH_STALE_SECONDS,
    CODEX_PSEUDO_ACCOUNT_SLOT,
    SETTINGS_BOUNDS,
    SETTINGS_DEFAULTS,
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    AccountRow,
    merge_quota_rows,
    normalize_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CREDENTIAL_PATHS = (
    "codex-accounts/0123456789abcdef/auth.json",
    "codex_accounts.json",
    "codex_quota_snapshots.json",
)
"""Every path the live source may ever write a secret or per-account state to.
The widget home is a git checkout; these must be ignored before one exists."""


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def transcript_row(*, pct: float | None = 22.0) -> AccountRow:
    """The row ``CodexQuota.account_row()`` builds today (SPEC-CODEX 4)."""
    return AccountRow(
        slot=CODEX_PSEUDO_ACCOUNT_SLOT,
        alias="Codex",
        email="",
        is_active=False,
        seven_day_pct=pct,
        vendor=VENDOR_CODEX,
        switchable=False,
        plan_type="pro",
    )


def live_row(
    slot: int,
    *,
    active: bool,
    weekly: float | None = None,
    five_hour: float | None = None,
    scoped: tuple[tuple[str, float], ...] = (),
    note: str = "",
) -> AccountRow:
    return AccountRow(
        slot=slot,
        alias=f"acct{-slot}",
        email="",
        is_active=active,
        five_hour_pct=five_hour,
        seven_day_pct=weekly,
        scoped_windows=scoped,
        vendor=VENDOR_CODEX,
        switchable=False,
        attention_note=note,
        attention_kind="warn" if note else "",
    )


def claude_row() -> AccountRow:
    return AccountRow(slot=1, alias="main", email="x@y", is_active=True, five_hour_pct=3.0)


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


def test_credential_paths_are_git_ignored_and_untracked() -> None:
    """A credential written under the widget home must never be committable."""
    if not (REPO_ROOT / ".git").exists():
        print("  (skip: not a git checkout)")
        return
    for rel in CREDENTIAL_PATHS:  # -q accepts exactly one path
        ignored = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", rel], check=False
        )
        assert ignored.returncode == 0, f"NOT covered by .gitignore: {rel}"
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", "codex-accounts",
         "codex_accounts.json", "codex_quota_snapshots.json"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert tracked == "", f"credential path is tracked by git: {tracked!r}"


def test_new_row_fields_default_to_empty_so_existing_rows_are_unchanged() -> None:
    row = claude_row()
    assert row.attention_note == "" and row.attention_kind == ""
    assert transcript_row().attention_note == ""


def test_live_settings_are_declared_and_clamped() -> None:
    """Undeclared keys are dropped by normalize_settings - so declare them."""
    out = normalize_settings({})
    assert out["codex_live_quota_enabled"] is False, "must default OFF (rollback switch)"
    assert out["codex_show_extra_limits"] is False
    assert out["codex_quota_interval_seconds"] == 300
    assert SETTINGS_BOUNDS["codex_quota_interval_seconds"] == (60, 3600)
    assert normalize_settings({"codex_quota_interval_seconds": 5})["codex_quota_interval_seconds"] == 60
    assert normalize_settings({"codex_quota_interval_seconds": 99_999})["codex_quota_interval_seconds"] == 3600
    assert normalize_settings({"codex_live_quota_enabled": 1})["codex_live_quota_enabled"] is True
    for key in ("codex_live_quota_enabled", "codex_show_extra_limits", "codex_quota_interval_seconds"):
        assert key in SETTINGS_DEFAULTS


# ---------------------------------------------------------------------------
# merge_quota_rows (SPEC-CODEX 6 arbitration)
# ---------------------------------------------------------------------------


def test_transcript_row_dropped_only_when_live_active_row_has_a_figure() -> None:
    t = transcript_row()
    live_active = live_row(-1, active=True, weekly=19.0)
    live_other = live_row(-2, active=False, weekly=41.0)
    merged = merge_quota_rows((claude_row(), t, live_active, live_other))
    assert t not in merged, "two numbers for one account is the SPEC 4.3 violation"
    assert merged == (claude_row(), live_active, live_other), "order and other rows untouched"


def test_transcript_row_returns_when_active_live_row_has_no_figure() -> None:
    t = transcript_row()
    dead_active = live_row(-1, active=True, note="relogin")
    assert merge_quota_rows((t, dead_active)) == (t, dead_active)
    withheld = live_row(-1, active=True)  # bars withheld past expiry: no figure
    assert merge_quota_rows((t, withheld)) == (t, withheld)


def test_transcript_row_returns_when_no_live_row_is_active() -> None:
    """Untracked login in ~/.codex, or the feature off: the transcript figure
    is the only honest one and it stays."""
    t = transcript_row()
    others = (live_row(-1, active=False, weekly=19.0), live_row(-2, active=False, five_hour=3.0))
    assert merge_quota_rows((t, *others)) == (t, *others)
    assert merge_quota_rows((t,)) == (t,)
    assert merge_quota_rows(()) == ()


def test_scoped_or_five_hour_figure_on_the_active_live_row_counts() -> None:
    t = transcript_row()
    assert t not in merge_quota_rows((t, live_row(-1, active=True, five_hour=7.0)))
    assert t not in merge_quota_rows((t, live_row(-1, active=True, scoped=(("24h", 1.0),))))


def test_merge_never_touches_claude_rows_or_a_claude_active_flag() -> None:
    """A Claude row being active must not be mistaken for a live Codex row."""
    t = transcript_row()
    assert merge_quota_rows((claude_row(), t)) == (claude_row(), t)
    assert VENDOR_CLAUDE != VENDOR_CODEX


# ---------------------------------------------------------------------------
# Fixtures: one VERIFIED body, one clearly SYNTHETIC shape
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402 - after the sys.path bootstrap, like the rest

BASE_NOW = _dt.datetime(2026, 9, 9, 10, 0, 0).timestamp()
"""A fixed local 10:00 so a reset one hour later is always the SAME local day,
whatever timezone this runs in - the reset formatter says ``11:00`` today and
``Sep 10 …`` tomorrow, and a test that flipped between them at midnight would
be a flake, not a finding."""

PRO_ACCOUNT_ID = "acct-pro-1111111111"
PRO_EMAIL = "pro@example.test"


def verified_pro_body(
    *, account_id: str = PRO_ACCOUNT_ID, email: str = PRO_EMAIL, used_percent: float = 19
) -> dict[str, Any]:
    """The Pro body as PROBED on this Mac 2026-09-09 (recorded in the plan).

    Every key and every stated value is from that probe: ``plan_type: "pro"``,
    ``primary_window.used_percent: 19``, ``limit_window_seconds: 604800``,
    ``secondary_window: null``, and the two ``additional_rate_limits`` pools
    with their widths. Two values the probe did NOT record are supplied here
    purely to exercise arithmetic and are marked as such: ``reset_after_seconds``
    (recorded as present, value not captured) and the account id / email, which
    are deliberately obvious placeholders rather than the real ones - a test
    fixture is not the place for a live account identifier.
    """
    return {
        "user_id": "user-placeholder",
        "account_id": account_id,
        "email": email,
        "plan_type": "pro",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": used_percent,
                "limit_window_seconds": 604800,
                "reset_after_seconds": 3600,  # value not captured by the probe
                # The body also carries an absolute `reset_at` computed on the
                # server's clock. We deliberately ignore it - this fixture sets
                # it to an obviously wrong epoch so a mapper that ever started
                # reading it would fail loudly here.
                "reset_at": 1_000_000_000,
            },
            "secondary_window": None,
        },
        "code_review_rate_limit": None,
        "additional_rate_limits": [
            {
                "limit_name": "GPT-5.3-Codex-Spark",
                "metered_feature": "spark",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 4,
                        "limit_window_seconds": 18000,
                        "reset_after_seconds": 900,
                    },
                    "secondary_window": {
                        "used_percent": 8,
                        "limit_window_seconds": 604800,
                        "reset_after_seconds": 7200,
                    },
                },
                "normal_model_slug": "gpt-5.3-codex",
            },
            {
                "limit_name": "gpt-reserve",
                "metered_feature": "base_model_inference",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 0,
                        "limit_window_seconds": 604800,
                        "reset_after_seconds": 7200,
                    }
                },
                "normal_model_slug": "gpt-5.6-luna",
            },
        ],
        "model_usage": {},
        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
        "spend_control": {"reached": False, "individual_limit": None},
        "rate_limit_reached_type": None,
        "promo": None,
        "rate_limit_reset_credits": {},
    }


def SYNTHETIC_two_window_body(
    *,
    account_id: str,
    primary_seconds: int,
    secondary_seconds: int,
    primary_pct: float = 5,
    secondary_pct: float = 41,
) -> dict[str, Any]:
    """A SYNTHETIC body - a **shape**, not captured data.

    The Business Premium body is unverified (the plan's step 2, the manual
    ``codex login`` probe, has not run), so nothing here claims to be what that
    plan returns. What it does claim is what the code must survive: a body
    whose two windows arrive in the OPPOSITE order to the Pro one, so a mapper
    that trusted position instead of width would put a week's figure on the
    5-hour bar. The percentages are arbitrary test values.
    """
    return {
        "account_id": account_id,
        "email": "shape@example.test",
        "plan_type": "SYNTHETIC-shape-not-a-real-plan-string",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": primary_pct,
                "limit_window_seconds": primary_seconds,
                "reset_after_seconds": 600,
            },
            "secondary_window": {
                "used_percent": secondary_pct,
                "limit_window_seconds": secondary_seconds,
                "reset_after_seconds": 1200,
            },
        },
        "additional_rate_limits": [],
        "rate_limit_reached_type": None,
    }


# ---------------------------------------------------------------------------
# Test doubles: a clock, a transport, and a harness over real files
# ---------------------------------------------------------------------------


class FakeClock:
    """Wall and monotonic, moved independently.

    They must be separable: on this Mac ``time.monotonic()`` pauses while the
    lid is shut, and the sleep/wake detector is precisely the code that reads
    the gap between the two. A single-clock fake could not express a sleep.
    """

    def __init__(self, wall: float = BASE_NOW, mono: float = 1_000.0) -> None:
        self.wall = wall
        self.mono = mono

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float, *, mono: float | None = None) -> None:
        self.wall += seconds
        self.mono += seconds if mono is None else mono

    def sleep(self, seconds: float) -> bool:
        """The injected ``sleeper``: time passes, nothing blocks, never stopped."""
        self.advance(seconds)
        return False


class FakeTransport:
    """Answers from a handler; records account ids and NEVER the headers.

    Deliberate: the headers carry the bearer token, and a test double that
    stashed them would put a token in a place the privacy canary does not
    look. Tests that need to prove a request was made assert on
    :attr:`calls`; the one test that must see the header wraps this class
    explicitly (``tests/test_privacy.py``'s negative control).
    """

    def __init__(self, handler: Callable[[str, int], Any]) -> None:
        self._handler = handler
        self.calls: list[str] = []

    def get(self, url: str, headers: Any, timeout: float) -> HttpResponse:
        account_id = dict(headers).get("ChatGPT-Account-Id", "")
        self.calls.append(account_id)
        attempt = self.calls.count(account_id)
        result = self._handler(account_id, attempt)
        if isinstance(result, BaseException):
            raise result
        return result

    def calls_for(self, account_id: str) -> int:
        return self.calls.count(account_id)


def json_response(status: int, body: Any, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, headers or {}, json.dumps(body).encode("utf-8"))


def ok(body: Any) -> HttpResponse:
    return json_response(200, body)


DEFAULT_SETTINGS: dict[str, Any] = {
    "codex_live_quota_enabled": True,
    "codex_quota_interval_seconds": 300,
    "codex_show_extra_limits": False,
    "codex_tracking_enabled": True,
}


def _jwt(payload: dict[str, Any]) -> str:
    """A structurally real JWT with a fake signature - which is the point:
    ``decode_jwt_claims`` does not verify, and must not start to."""

    def segment(obj: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")

    return ".".join([segment({"alg": "none", "typ": "JWT"}), segment(payload), "not-a-signature"])


def access_token(*, account_id: str, email: str, plan: str, exp: float | None) -> str:
    payload: dict[str, Any] = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": plan,
            "chatgpt_user_id": "user-placeholder",
        },
        "https://api.openai.com/profile": {"email": email},
    }
    if exp is not None:
        payload["exp"] = exp
    return _jwt(payload)


def write_credential(
    accounts_dir: Path,
    account_id: str,
    *,
    exp: float | None,
    email: str = "who@example.test",
    plan: str = "pro",
    mode: int = 0o600,
    token: str | None = None,
    refresh: str | None = None,
) -> Path:
    """A fake ``CODEX_HOME/auth.json`` in the shape ``codex login`` writes."""
    directory = accounts_dir / account_id
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    auth = directory / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "auth_mode": "chatgpt",
                "last_refresh": "2026-09-09T08:46:00Z",
                "tokens": {
                    "access_token": token
                    or access_token(account_id=account_id, email=email, plan=plan, exp=exp),
                    "refresh_token": refresh or f"refresh-for-{account_id}",
                    "id_token": f"id-for-{account_id}",
                    "account_id": account_id,
                },
            }
        )
    )
    os.chmod(auth, mode)
    return auth


def write_mirror(auth_path: Path, account_id: str | None, *, raw: str | None = None) -> None:
    """The read-only ``~/.codex/auth.json`` stand-in the active marker reads."""
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        auth_path.write_text(raw)
        return
    auth_path.write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"account_id": account_id}})
    )


class Harness:
    """A whole source over real files in a temp dir, with every seam injected."""

    def __init__(
        self,
        root: Path,
        *,
        accounts: "list[tuple[str, str]] | None" = None,
        handler: Callable[[str, int], Any] | None = None,
        settings: dict[str, Any] | None = None,
        start: float = BASE_NOW,
    ) -> None:
        self.root = Path(root)
        self.clock = FakeClock(start)
        self.accounts_dir = self.root / "codex-accounts"
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "codex_accounts.json"
        self.snapshots_path = self.root / "codex_quota_snapshots.json"
        self.auth_path = self.root / "dot-codex" / "auth.json"
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(settings or {})
        self.logs: list[str] = []
        self.transport = FakeTransport(handler or (lambda account_id, attempt: ok({})))
        if accounts is not None:
            self.write_registry(accounts)
        self.source = self.build_source()

    def write_registry(self, accounts: "list[tuple[str, str]]", *, enabled: Any = True) -> None:
        """*accounts* is ``[(account_id, alias)]`` in the order they render."""
        payload = {
            "version": 1,
            "accounts": [
                {
                    "account_id": account_id,
                    "alias": alias,
                    "enabled": enabled if isinstance(enabled, bool) else enabled(account_id),
                    "order": index,
                }
                for index, (account_id, alias) in enumerate(accounts)
            ],
        }
        self.registry_path.write_text(json.dumps(payload))

    def build_source(self) -> CodexAccountsSource:
        """A FRESH source over the same files - what a cold start looks like."""
        return CodexAccountsSource(
            registry=Registry(self.registry_path),
            credentials=CredentialStore(
                self.accounts_dir, auth_path=self.auth_path, clock=self.clock.time
            ),
            transport=self.transport,
            snapshots_path=self.snapshots_path,
            settings=lambda: self.settings,
            clock=self.clock.time,
            monotonic=self.clock.monotonic,
            sleeper=self.clock.sleep,
            log=self.logs.append,
        )

    def rows_by_alias(self) -> dict[str, AccountRow]:
        return {row.alias: row for row in self.source.quota_rows()}

    def state(self, account_id: str) -> Any:
        """The poller's per-account state. Private on purpose - a schedule is
        not a public API - but a test that could not read it could only assert
        on wall-clock sleeping, which is how flaky suites are born."""
        return self.source._states[account_id]  # noqa: SLF001


def temp_harness(**kwargs: Any) -> Any:
    """``with temp_harness(...) as h:`` - a TemporaryDirectory plus a Harness."""

    class _Ctx:
        def __enter__(self) -> Harness:
            self._tmp = tempfile.TemporaryDirectory()
            return Harness(Path(self._tmp.name), **kwargs)

        def __exit__(self, *exc: Any) -> None:
            self._tmp.cleanup()

    return _Ctx()


# ---------------------------------------------------------------------------
# The mapper (SPEC-CODEX 6: width dispatch, honesty, identity)
# ---------------------------------------------------------------------------


def test_verified_pro_body_maps_to_a_weekly_bar_and_an_anchored_reset() -> None:
    """The probed Pro body must produce exactly one bar: 19 % weekly."""
    quota = CodexAccountQuota.from_response(
        verified_pro_body(),
        credential_account_id=PRO_ACCOUNT_ID,
        observed_at=BASE_NOW,
        include_extra=False,
    )
    assert quota is not None
    assert quota.plan_type == "pro", "plan strings pass through verbatim"
    assert quota.email == PRO_EMAIL
    assert quota.seven_day is not None and quota.seven_day.used_percent == 19.0
    assert quota.seven_day.window_seconds == 604800
    assert quota.five_hour is None, "Pro reports no 5-hour window - never invent a 0%"
    assert quota.scoped == (), "additional_rate_limits are opt-in"
    # Anchored to the READ instant from reset_after_seconds, not to the body's
    # own server-clock reset_at (which this fixture sets to a wrong epoch).
    assert quota.seven_day.reset_at == BASE_NOW + 3600

    row = quota.account_row(now=BASE_NOW, slot=-1, alias="", is_active=True)
    assert row.seven_day_pct == 19.0 and row.five_hour_pct is None
    assert row.vendor == VENDOR_CODEX and row.switchable is False
    assert row.slot == -1 and row.is_active is True
    assert row.stale_after_seconds == CODEX_FETCH_STALE_SECONDS
    assert row.expired_windows == ()
    expected_clock = _dt.datetime.fromtimestamp(BASE_NOW + 3600).strftime("%H:%M")
    # The BARE clock: the renderer adds "resets" itself (the contract every
    # other source honours). A prefixed value rendered "resets resets 15:46".
    assert row.seven_day_resets_at == expected_clock, row.seven_day_resets_at
    assert not str(row.seven_day_resets_at).startswith("resets")
    assert row.alias == f"{PRO_EMAIL} · {PRO_ACCOUNT_ID[:8]}", "alias-less rows fall back"


def test_windows_dispatch_by_width_not_by_position() -> None:
    """SYNTHETIC shape: the same two widths, arrived in the other order."""
    body = SYNTHETIC_two_window_body(
        account_id="acct-shape", primary_seconds=18000, secondary_seconds=604800
    )
    quota = CodexAccountQuota.from_response(
        body, credential_account_id="acct-shape", observed_at=BASE_NOW, include_extra=False
    )
    assert quota is not None
    assert quota.five_hour is not None and quota.five_hour.used_percent == 5.0
    assert quota.seven_day is not None and quota.seven_day.used_percent == 41.0
    assert quota.seven_day.window_seconds == 604800

    hourly = SYNTHETIC_two_window_body(
        account_id="acct-shape", primary_seconds=604800, secondary_seconds=3600
    )
    other = CodexAccountQuota.from_response(
        hourly, credential_account_id="acct-shape", observed_at=BASE_NOW, include_extra=False
    )
    assert other is not None
    assert other.seven_day is not None and other.seven_day.used_percent == 5.0
    assert other.five_hour is None, "an hourly window is NOT the 5-hour bucket"
    assert [name for name, _ in other.scoped] == ["hourly"], "unknown width, named by width"
    assert other.scoped[0][1].used_percent == 41.0


def test_named_pools_render_as_themselves_and_only_when_asked() -> None:
    """A Spark pool that happens to be 5 h wide must not become THE 5-hour bar."""
    body = verified_pro_body()
    off = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert off is not None and off.scoped == ()

    on = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=True
    )
    assert on is not None
    assert on.five_hour is None, "the plan has no 5-hour window; a pool's is not one"
    assert on.seven_day is not None and on.seven_day.used_percent == 19.0
    names = [name for name, _ in on.scoped]
    assert names[0] == "GPT-5.3-Codex-Spark"
    assert "gpt-reserve" in names
    assert len(names) == len(set(names)), "one label per pool window"


def test_a_reached_limit_is_a_note_and_never_a_percentage() -> None:
    body = verified_pro_body(used_percent=19)
    body["rate_limit"]["limit_reached"] = True
    body["rate_limit"]["allowed"] = False
    body["rate_limit_reached_type"] = "primary"
    quota = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert quota is not None
    assert quota.seven_day is not None and quota.seven_day.used_percent == 19.0, (
        "limit_reached describes the PLAN, not the bar - it must never round a "
        "percentage up to 100 or replace it"
    )
    assert quota.capped_note == "capped primary"
    row = quota.account_row(
        now=BASE_NOW, slot=-1, alias="a", is_active=False,
        note=quota.capped_note, note_kind="crit",
    )
    assert row.seven_day_pct == 19.0
    assert row.attention_note == "capped primary" and row.attention_kind == "crit"


def test_a_response_about_another_account_is_dropped() -> None:
    body = verified_pro_body(account_id="acct-somebody-else")
    assert (
        CodexAccountQuota.from_response(
            body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
        )
        is None
    ), "showing another account's figure under this alias is the worst failure here"
    # A body that omits the id is still usable: we authenticated with exactly
    # one credential, so there is nothing to contradict.
    body.pop("account_id")
    assert (
        CodexAccountQuota.from_response(
            body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
        )
        is not None
    )


def test_quota_json_round_trips_and_survives_corruption() -> None:
    quota = CodexAccountQuota.from_response(
        verified_pro_body(),
        credential_account_id=PRO_ACCOUNT_ID,
        observed_at=BASE_NOW,
        include_extra=True,
    )
    assert quota is not None
    assert CodexAccountQuota.from_json(json.loads(json.dumps(quota.to_json()))) == quota
    for junk in (None, [], {}, {"account_id": ""}, {"account_id": "x", "scoped": "nope"}):
        CodexAccountQuota.from_json(junk)  # total: must not raise


def test_decode_jwt_claims_reads_identity_without_verifying() -> None:
    token = access_token(
        account_id="acct-x", email="who@example.test", plan="pro", exp=BASE_NOW + 60
    )
    claims = decode_jwt_claims(token)
    assert claims["account_id"] == "acct-x"
    assert claims["email"] == "who@example.test"
    assert claims["plan_type"] == "pro"
    assert claims["exp"] == BASE_NOW + 60
    assert set(claims) == {"account_id", "email", "plan_type", "exp"}, (
        "only these four leave the payload - the rest is session identifiers"
    )
    for junk in ("", "not.a.jwt", "a.b", "x." + "!" * 9 + ".z"):
        assert decode_jwt_claims(junk) == {}


# ---------------------------------------------------------------------------
# The poller: status classes, ladders, schedules
# ---------------------------------------------------------------------------


def test_a_healthy_cycle_produces_a_row_and_schedules_the_next_read() -> None:
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "gmail")]) as h:
        h.transport = FakeTransport(lambda account_id, attempt: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        assert h.source.available() is True
        h.source.run_cycle_once()

        rows = h.source.quota_rows()
        assert len(rows) == 1
        assert rows[0].alias == "gmail" and rows[0].seven_day_pct == 19.0
        assert rows[0].attention_note == "" and rows[0].usage_is_stale is False
        interval = DEFAULT_SETTINGS["codex_quota_interval_seconds"]
        due_in = h.state(PRO_ACCOUNT_ID).next_due_wall - BASE_NOW
        assert 0.85 * interval <= due_in <= 1.15 * interval, "interval ±15 % jitter"
        assert h.state(PRO_ACCOUNT_ID).last_status == 200
        # The jitter is a hash, not a coin: the same id schedules identically.
        second = Harness(h.root, accounts=[(PRO_ACCOUNT_ID, "gmail")])
        second.transport = h.transport
        second.source = second.build_source()
        second.source.run_cycle_once()
        assert second.state(PRO_ACCOUNT_ID).next_due_wall == h.state(PRO_ACCOUNT_ID).next_due_wall


def test_an_enabled_account_with_no_reading_says_so() -> None:
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "gmail")]) as h:
        rows = h.source.quota_rows()
        assert len(rows) == 1, "an enabled account is never silently absent"
        assert rows[0].attention_note == NOTE_PENDING
        assert rows[0].attention_kind == "info"
        assert rows[0].seven_day_pct is None, "no reading means no figure, not 0 %"


def test_available_is_false_without_the_setting_or_an_enabled_account() -> None:
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "gmail")]) as h:
        assert h.source.available() is True
        h.settings["codex_live_quota_enabled"] = False
        assert h.source.available() is False, "the rollback switch"
        h.settings["codex_live_quota_enabled"] = True
        h.write_registry([(PRO_ACCOUNT_ID, "gmail")], enabled=lambda _id: False)
        assert h.source.available() is False
        h.registry_path.unlink()
        assert h.source.available() is False
        h.source.run_cycle_once()
        assert h.transport.calls == [], "an unavailable source never touches the network"


def test_401_says_relogin_and_backs_off_half_an_hour() -> None:
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = FakeTransport(lambda a, n: json_response(401, {"detail": "expired"}))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        state = h.state(PRO_ACCOUNT_ID)
        assert (state.note, state.note_kind) == (NOTE_RELOGIN, "warn")
        assert state.next_due_wall == BASE_NOW + 1800
        assert h.source.quota_rows()[0].attention_note == NOTE_RELOGIN


def test_json_403_is_no_access_but_an_html_403_is_an_endpoint_error() -> None:
    """A challenge page is not an answer about permissions."""
    with temp_harness(accounts=[("acct-a", "a")]) as h:
        h.transport = FakeTransport(lambda a, n: json_response(403, {"detail": "forbidden"}))
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        state = h.state("acct-a")
        assert (state.note, state.note_kind) == (NOTE_NO_ACCESS, "warn")
        assert state.next_due_wall == BASE_NOW + 3600

    with temp_harness(accounts=[("acct-a", "a")]) as h:
        h.transport = FakeTransport(
            lambda a, n: HttpResponse(403, {}, b"<html>are you a robot</html>")
        )
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        for _ in range(3):
            h.source.force_due()
            h.source.run_cycle_once()
        state = h.state("acct-a")
        assert state.note == NOTE_ENDPOINT_ERROR, "HTML behind a 403 is the endpoint, not a right"


def test_429_uses_retry_after_in_both_legal_forms_clamped() -> None:
    seconds_form = {"Retry-After": "900"}
    with temp_harness(accounts=[("acct-a", "a")]) as h:
        h.transport = FakeTransport(lambda a, n: json_response(429, {}, seconds_form))
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        state = h.state("acct-a")
        assert (state.note, state.note_kind) == (NOTE_RATE_LIMITED, "warn")
        assert state.next_due_wall == BASE_NOW + 900

    # HTTP-date form, and the header name in a different case: header names are
    # case-insensitive and a case-sensitive lookup here once read as "absent".
    when = _dt.datetime.fromtimestamp(BASE_NOW + 1200, _dt.timezone.utc)
    date_form = {"retry-after": when.strftime("%a, %d %b %Y %H:%M:%S GMT")}
    with temp_harness(accounts=[("acct-a", "a")]) as h:
        h.transport = FakeTransport(lambda a, n: json_response(429, {}, date_form))
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert abs(h.state("acct-a").next_due_wall - (BASE_NOW + 1200)) <= 1.0

    # Below the poll interval it is clamped UP: an eager Retry-After must not
    # become a licence to poll faster than we would anyway.
    with temp_harness(accounts=[("acct-a", "a")]) as h:
        h.transport = FakeTransport(lambda a, n: json_response(429, {}, {"Retry-After": "5"}))
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert h.state("acct-a").next_due_wall == BASE_NOW + 300

    # No header at all: the ladder, starting at 120 s.
    with temp_harness(accounts=[("acct-a", "a")]) as h:
        h.transport = FakeTransport(lambda a, n: json_response(429, {}))
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert h.state("acct-a").next_due_wall == BASE_NOW + 120


def test_three_strikes_before_an_endpoint_error_note() -> None:
    """One 502 is noise. A note on the first blip is a note nobody reads."""
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = FakeTransport(
            lambda a, n: ok(verified_pro_body()) if n == 1 else HttpResponse(502, {}, b"nope")
        )
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert h.source.quota_rows()[0].seven_day_pct == 19.0

        notes, figures = [], []
        for _ in range(3):
            h.source.force_due()
            h.source.run_cycle_once()
            notes.append(h.state(PRO_ACCOUNT_ID).note)
            figures.append(h.source.quota_rows()[0].seven_day_pct)
        assert notes == ["", "", NOTE_ENDPOINT_ERROR], f"three strikes, got {notes}"
        # Until the third strike the last real reading stays (with its age);
        # once the sentinel stands it REPLACES the figure (SPEC 4.3).
        assert figures == [19.0, 19.0, None], figures
        row = h.source.quota_rows()[0]
        assert row.attention_kind == "warn"
        assert h.source._snapshots[PRO_ACCOUNT_ID].seven_day.used_percent == 19.0, (  # noqa: SLF001
            "the reading itself is kept, ready to return when the note clears"
        )
        # Ladder: 60, 120, 240 from the failure instant.
        assert h.state(PRO_ACCOUNT_ID).next_due_wall == h.clock.wall + 240


def test_three_strikes_before_an_offline_note() -> None:
    with temp_harness(accounts=[("acct-a", "a")]) as h:
        h.transport = FakeTransport(lambda a, n: OSError("nodename nor servname provided"))
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        notes = []
        for _ in range(3):
            h.source.force_due()
            h.source.run_cycle_once()
            notes.append(h.state("acct-a").note)
        assert notes == ["", "", NOTE_OFFLINE]
        assert h.state("acct-a").last_status is None, "we never got a status"
        assert not any("nodename" in line for line in h.logs), (
            "a transport failure is logged by TYPE - its message can carry the host"
        )


def test_an_expired_token_relogins_without_making_a_request() -> None:
    def explode(account_id: str, attempt: int) -> Any:
        raise AssertionError("an expired token must cost zero requests")

    with temp_harness(accounts=[("acct-a", "a")]) as h:
        h.transport = FakeTransport(explode)
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 1)
        h.source.run_cycle_once()
        assert h.transport.calls == []
        state = h.state("acct-a")
        assert (state.note, state.note_kind) == (NOTE_RELOGIN, "warn")


def test_a_token_expiring_within_48h_shows_a_countdown_and_still_reads() -> None:
    """With refresh OFF nobody will renew the token, so the deadline is real.
    (With refresh on and healthy the countdown is withheld - CX-7a below.)"""
    with temp_harness(
        accounts=[(PRO_ACCOUNT_ID, "a")], settings={"codex_refresh_enabled": False}
    ) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 30 * 3600)
        h.source.run_cycle_once()
        row = h.source.quota_rows()[0]
        assert row.attention_note == "relogin in 1d 6h"
        assert row.attention_kind == "info", "a countdown is information, not a fault"
        assert row.seven_day_pct == 19.0, "it still works; that is the whole point of warning"


def test_a_credential_other_users_can_read_is_refused() -> None:
    with temp_harness(accounts=[("acct-a", "a")]) as h:
        h.transport = FakeTransport(
            lambda a, n: (_ for _ in ()).throw(AssertionError("must not be used"))
        )
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400, mode=0o644)
        h.source.run_cycle_once()
        assert h.transport.calls == []
        assert h.state("acct-a").note == NOTE_NO_CREDENTIAL
        assert any("0644" in line for line in h.source.diagnostics()), (
            "a refused credential must say why, or the row just sits there"
        )


# ---------------------------------------------------------------------------
# Ageing, cold start, sleep/wake, isolation
# ---------------------------------------------------------------------------


def test_a_reading_ages_then_its_bars_are_withheld_and_the_row_survives() -> None:
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "gmail")]) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()

        h.clock.advance(CODEX_FETCH_STALE_SECONDS + 1)
        row = h.source.quota_rows()[0]
        assert row.usage_is_stale is True and row.seven_day_pct == 19.0, (
            "past 15 min the figure is shown WITH its age, not withheld"
        )

        h.clock.advance(CODEX_FETCH_EXPIRE_SECONDS)
        row = h.source.quota_rows()[0]
        assert row.alias == "gmail", "the row survives; only the numbers go"
        assert row.seven_day_pct is None and row.five_hour_pct is None
        assert row.scoped_windows == () and row.seven_day_resets_at is None
        assert row.usage_age_seconds is not None and row.usage_age_seconds > CODEX_FETCH_EXPIRE_SECONDS


def test_a_reset_that_has_passed_marks_the_window_expired_after_a_grace() -> None:
    quota = CodexAccountQuota.from_response(
        verified_pro_body(),
        credential_account_id=PRO_ACCOUNT_ID,
        observed_at=BASE_NOW,
        include_extra=False,
    )
    assert quota is not None
    just_after = BASE_NOW + 3600 + 60  # inside the 120 s clock-skew grace
    assert quota.account_row(now=just_after, slot=-1, alias="a", is_active=False).expired_windows == ()
    later = BASE_NOW + 3600 + 121
    row = quota.account_row(now=later, slot=-1, alias="a", is_active=False)
    assert row.expired_windows == ("seven_day",)
    assert row.seven_day_resets_at is not None and row.seven_day_resets_at.startswith("overdue (")


def test_a_cold_start_rehydrates_the_reading_and_a_standing_sentinel() -> None:
    """A restart must not claim a dead account is merely waiting for its first
    poll, and must not claim an old reading is new."""
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "gmail")]) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        # The credential dies (expiry passes), so the next cycle plants a
        # sentinel next to a still-recent figure.
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW - 1)
        h.source.force_due()
        h.source.run_cycle_once()
        assert h.state(PRO_ACCOUNT_ID).note == NOTE_RELOGIN

        sidecar = json.loads(h.snapshots_path.read_text())
        record = sidecar["accounts"][PRO_ACCOUNT_ID]
        assert record["note"] == NOTE_RELOGIN
        assert record["quota"]["seven_day"]["used_percent"] == 19.0
        assert "consecutive_failures" not in json.dumps(record), (
            "a ladder is a statement about the last few minutes of network - "
            "restoring it would back a fresh process off over yesterday's outage"
        )

        cold = h.build_source()  # a fresh object over the same files
        rows = cold.quota_rows()
        assert len(rows) == 1
        assert rows[0].attention_note == NOTE_RELOGIN, "the sentinel survived the restart"
        assert rows[0].seven_day_pct is None, "a warn sentinel withholds the bars"
        assert rows[0].usage_age_seconds is not None, "and the row carries its TRUE age"
        assert cold._snapshots[PRO_ACCOUNT_ID].seven_day.used_percent == 19.0  # noqa: SLF001


def test_the_wake_detector_makes_a_backed_off_account_due_again() -> None:
    """The case that needs it: the machine slept THROUGH a backoff.

    Wall time moved 200 s; monotonic did not move at all, because on this Mac
    it pauses with the lid shut. The 401's 30-minute backoff was computed
    before the sleep, so without the detector the row would sit dead until
    half an hour of AWAKE time had passed.
    """
    handler = (
        lambda account_id, attempt: json_response(401, {}) if attempt == 1 else ok(verified_pro_body())
    )
    with temp_harness(
        accounts=[("acct-a", "a"), ("acct-b", "b")],
        settings={"codex_quota_interval_seconds": 60},
    ) as h:
        h.transport = FakeTransport(handler)
        h.source = h.build_source()
        for account_id in ("acct-a", "acct-b"):
            write_credential(h.accounts_dir, account_id, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert h.transport.calls_for("acct-a") == 1 and h.transport.calls_for("acct-b") == 1
        assert h.state("acct-a").next_due_wall > h.clock.wall + 1000  # 30 min out

        h.clock.advance(200, mono=200)  # 200 s AWAKE: nothing is due yet
        h.source.run_cycle_once()
        assert h.transport.calls_for("acct-a") == 1, "an awake gap must not skip the backoff"

        h.clock.advance(200, mono=0)  # 200 s of WALL time with the lid shut
        h.source.run_cycle_once()
        assert h.transport.calls_for("acct-a") == 2, "a wake makes every account due"
        assert h.transport.calls_for("acct-b") == 2
        assert any("woke after" in line for line in h.source.diagnostics())


def test_one_accounts_backoff_never_moves_another() -> None:
    def handler(account_id: str, attempt: int) -> Any:
        if account_id == "acct-a":
            return HttpResponse(500, {}, b"boom")
        return ok(verified_pro_body(account_id="acct-b", email="b@example.test"))

    with temp_harness(accounts=[("acct-a", "broken"), ("acct-b", "fine")]) as h:
        h.transport = FakeTransport(handler)
        h.source = h.build_source()
        for account_id in ("acct-a", "acct-b"):
            write_credential(h.accounts_dir, account_id, exp=BASE_NOW + 9 * 86_400)
        for _ in range(3):
            h.source.force_due()
            h.source.run_cycle_once()

        assert h.state("acct-a").note == NOTE_ENDPOINT_ERROR
        assert h.state("acct-a").consecutive_failures == 3
        assert h.state("acct-b").note == "" and h.state("acct-b").consecutive_failures == 0
        rows = h.rows_by_alias()
        assert rows["fine"].seven_day_pct == 19.0
        assert rows["broken"].seven_day_pct is None
        # And the healthy account is still on the plain interval, not the ladder.
        assert h.state("acct-b").next_due_wall - h.clock.wall <= 1.15 * 300


def test_requests_are_spaced_out_within_a_cycle() -> None:
    """Four accounts back-to-back look exactly like a script to a rate limiter."""
    seen: list[float] = []

    with temp_harness(accounts=[("acct-a", "a"), ("acct-b", "b")]) as h:
        def handler(account_id: str, attempt: int) -> Any:
            seen.append(h.clock.wall)
            return ok(verified_pro_body(account_id=account_id))

        h.transport = FakeTransport(handler)
        h.source = h.build_source()
        for account_id in ("acct-a", "acct-b"):
            write_credential(h.accounts_dir, account_id, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert len(seen) == 2 and seen[1] - seen[0] >= 20.0


def test_the_poller_thread_starts_polls_and_joins_and_pause_stops_requests() -> None:
    """The lifecycle the app drives, exercised on the real thread.

    Everything else in this file calls ``run_cycle_once`` directly, which is
    why this one exists: a cycle that is perfect and never scheduled is a
    feature that does not ship. Synchronised on an Event, not on a sleep.
    """
    import threading

    polled = threading.Event()

    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        def handler(account_id: str, attempt: int) -> Any:
            polled.set()
            return ok(verified_pro_body())

        h.transport = FakeTransport(handler)
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        try:
            h.source.start()
            h.source.start()  # idempotent
            assert polled.wait(5.0), "the poller thread never made its first request"
        finally:
            assert h.source.stop(timeout=5.0) is True, "stop() must actually join"
        assert h.source.quota_rows()[0].seven_day_pct == 19.0
        # And a stopped source stays stopped: quit must not be undone by a tick.
        before = h.transport.calls_for(PRO_ACCOUNT_ID)
        h.source.force_due()
        h.source.run_cycle_once()
        assert h.transport.calls_for(PRO_ACCOUNT_ID) == before


def test_pausing_stops_requests_without_losing_the_reading() -> None:
    """``codex_tracking_enabled`` off: no traffic, but the row keeps its truth."""
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()

        h.source.pause(True)
        h.source.force_due()
        h.source.run_cycle_once()
        assert h.transport.calls_for(PRO_ACCOUNT_ID) == 1, "paused means no requests"
        assert h.source.quota_rows()[0].seven_day_pct == 19.0, "and the reading stays"

        h.source.pause(False)
        h.source.run_cycle_once()
        assert h.transport.calls_for(PRO_ACCOUNT_ID) == 2, "un-pausing resumes the schedule"


# ---------------------------------------------------------------------------
# The active-login marker (read-only ~/.codex)
# ---------------------------------------------------------------------------


def test_exactly_one_row_is_marked_active_from_the_codex_mirror() -> None:
    with temp_harness(accounts=[("acct-a", "a"), ("acct-b", "b"), ("acct-c", "c")]) as h:
        write_mirror(h.auth_path, "acct-b")
        rows = h.source.quota_rows()
        assert [row.is_active for row in rows] == [False, True, False]
        assert h.source.active_account_id() == "acct-b"
        assert [row.slot for row in rows] == [-1, -2, -3], "negative slots, registry order"


def test_an_untracked_active_login_marks_nothing_and_says_so() -> None:
    with temp_harness(accounts=[("acct-a", "a")]) as h:
        write_mirror(h.auth_path, "acct-not-in-the-registry")
        assert h.source.active_account_id() is None
        assert not any(row.is_active for row in h.source.quota_rows())
        assert any("not tracked" in line for line in h.source.diagnostics())


def test_a_corrupt_mirror_keeps_the_previous_login_for_the_grace_then_forgets() -> None:
    """The desktop app rewrites that file non-atomically; a mid-write read is
    normal and must not blink the marker off. Ten minutes later it is no longer
    evidence of anything."""
    with temp_harness(accounts=[("acct-a", "a")]) as h:
        write_mirror(h.auth_path, "acct-a")
        assert h.source.active_account_id() == "acct-a"
        write_mirror(h.auth_path, None, raw='{"tokens": {"account_i')
        h.clock.advance(60)
        assert h.source.active_account_id() == "acct-a", "inside the grace"
        h.clock.advance(600)
        assert h.source.active_account_id() is None, "past the grace it is not evidence"
        assert not any(row.is_active for row in h.source.quota_rows())


def test_a_full_cycle_never_writes_to_the_codex_mirror() -> None:
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "gmail")]) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        write_mirror(h.auth_path, PRO_ACCOUNT_ID)
        before = (h.auth_path.read_bytes(), h.auth_path.stat().st_mtime_ns)

        h.source.run_cycle_once()
        h.source.quota_rows()
        h.source.diagnostics()

        after = (h.auth_path.read_bytes(), h.auth_path.stat().st_mtime_ns)
        assert after == before, "~/.codex belongs to the ChatGPT app; we only read it"


# ---------------------------------------------------------------------------
# The registry, and four real-shaped accounts
# ---------------------------------------------------------------------------


def test_four_accounts_two_sharing_an_email_are_four_rows_keyed_by_id() -> None:
    """The author's actual shape: you@corp.example twice (two workspaces)."""
    shared = "shared@example.test"
    accounts = [
        ("acct-personal-1", "belkins personal"),
        ("acct-work-2", "belkins work"),
        ("acct-gmail-3", "gmail"),
        ("acct-acme-4", "acme"),
    ]
    emails = {
        "acct-personal-1": shared,
        "acct-work-2": shared,
        "acct-gmail-3": "gmail@example.test",
        "acct-acme-4": "acme@example.test",
    }
    with temp_harness(accounts=accounts) as h:
        h.transport = FakeTransport(
            lambda account_id, attempt: ok(
                verified_pro_body(account_id=account_id, email=emails[account_id])
            )
        )
        h.source = h.build_source()
        for account_id, _ in accounts:
            write_credential(
                h.accounts_dir, account_id, exp=BASE_NOW + 9 * 86_400, email=emails[account_id]
            )
        write_mirror(h.auth_path, "acct-acme-4")
        h.source.run_cycle_once()

        rows = h.source.quota_rows()
        assert len(rows) == 4, "one row per account id, never per email"
        assert [row.alias for row in rows] == [alias for _, alias in accounts]
        assert [row.slot for row in rows] == [-1, -2, -3, -4]
        assert sum(row.is_active for row in rows) == 1
        assert all(row.seven_day_pct == 19.0 for row in rows)
        assert len({row.email for row in rows}) == 3, "two rows legitimately share an email"


def test_disabling_an_account_hides_the_row_and_keeps_its_reading() -> None:
    with temp_harness(accounts=[("acct-a", "a"), ("acct-b", "b")]) as h:
        h.transport = FakeTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id))
        )
        h.source = h.build_source()
        for account_id in ("acct-a", "acct-b"):
            write_credential(h.accounts_dir, account_id, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert len(h.source.quota_rows()) == 2

        Registry(h.registry_path).set_enabled("acct-b", False)
        rows = h.source.quota_rows()
        assert [row.alias for row in rows] == ["a"], "disabled means gone from the menu"
        assert [row.slot for row in rows] == [-1], "slots renumber; ids stay the key"
        sidecar = json.loads(h.snapshots_path.read_text())
        assert "acct-b" in sidecar["accounts"], (
            "the reading is kept, so re-enabling shows it with its true age "
            "instead of an empty row pretending to be new"
        )
        h.source.force_due()
        h.source.run_cycle_once()
        assert h.transport.calls_for("acct-b") == 1, "a disabled account is not polled"


def test_the_registry_is_forgiving_reloads_on_change_and_writes_0600() -> None:
    with tempfile.TemporaryDirectory() as name:
        path = Path(name) / "codex_accounts.json"
        registry = Registry(path)
        assert registry.entries() == () and registry.exists() is False

        path.write_text("{ not json at all")
        assert registry.entries() == (), "a hand-edited typo costs rows, not a crash"

        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "accounts": [
                        {"account_id": "b", "order": 2},
                        {"alias": "no id here"},
                        {"account_id": "a", "alias": "first", "enabled": False, "order": 1},
                        {"account_id": "b", "alias": "duplicate"},
                    ],
                }
            )
        )
        entries = registry.entries()
        assert [e.account_id for e in entries] == ["a", "b"], "sorted by order, deduped"
        assert entries[0].alias == "first" and entries[0].enabled is False
        assert registry.enabled_entries() == (entries[1],)

        assert registry.set_enabled("nope", True) is False
        assert registry.set_enabled("a", True) is True
        assert registry.entries()[0].enabled is True, "our own write is visible to us"
        registry.upsert(RegistryEntry(account_id="c", alias="third", enabled=True, order=9))
        assert [e.account_id for e in registry.entries()] == ["a", "b", "c"]
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_every_file_this_source_writes_is_0600() -> None:
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        Registry(h.registry_path).set_enabled(PRO_ACCOUNT_ID, True)
        for path in (h.snapshots_path, h.registry_path):
            assert oct(path.stat().st_mode & 0o777) == "0o600", path.name
        assert not list(h.root.glob("*.tmp.*")), "no temp file left behind"


# ---------------------------------------------------------------------------
# The onboarding CLI
# ---------------------------------------------------------------------------


class _Out:
    def __init__(self) -> None:
        self.text = ""

    def write(self, chunk: str) -> None:
        self.text += chunk


def test_adopt_names_each_dir_after_the_account_its_token_claims() -> None:
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        accounts_dir = root / "codex-accounts"
        registry_path = root / "codex_accounts.json"
        for index, account_id in enumerate(("acct-one", "acct-two"), start=1):
            staged = accounts_dir / f"new-{index}"
            staged.mkdir(parents=True)
            (staged / "auth.json").write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": access_token(
                                account_id=account_id,
                                email=f"{account_id}@example.test",
                                plan="pro",
                                exp=BASE_NOW + 9 * 86_400,
                            ),
                            "account_id": account_id,
                        }
                    }
                )
            )
        out = _Out()
        code = accounts_main(
            ["adopt"], accounts_dir=accounts_dir, registry_path=registry_path, out=out
        )
        assert code == 0, out.text
        assert (accounts_dir / "acct-one" / "auth.json").is_file()
        assert not (accounts_dir / "new-1").exists()
        assert oct((accounts_dir / "acct-one").stat().st_mode & 0o777) == "0o700"
        assert oct((accounts_dir / "acct-one" / "auth.json").stat().st_mode & 0o777) == "0o600"
        entries = Registry(registry_path).entries()
        assert [e.account_id for e in entries] == ["acct-one", "acct-two"]
        assert "acct-one@example.test" in out.text and "pro" in out.text
        assert "access_token" not in out.text and "eyJ" not in out.text


def test_adopt_refuses_a_duplicate_account_id() -> None:
    """Two dirs claiming one account means the workspace picker was not used;
    keeping one silently would hide a login that is not being tracked."""
    with tempfile.TemporaryDirectory() as name:
        root = Path(name)
        accounts_dir = root / "codex-accounts"
        registry_path = root / "codex_accounts.json"
        for index in (1, 2):
            staged = accounts_dir / f"new-{index}"
            staged.mkdir(parents=True)
            (staged / "auth.json").write_text(
                json.dumps({"tokens": {"account_id": "acct-same", "access_token": "x.y.z"}})
            )
        out = _Out()
        assert accounts_main(
            ["adopt"], accounts_dir=accounts_dir, registry_path=registry_path, out=out
        ) == 1
        assert "new-1" in out.text and "new-2" in out.text, "both paths, so it is fixable"
        assert (accounts_dir / "new-1").is_dir() and (accounts_dir / "new-2").is_dir()
        assert not registry_path.exists(), "a refused adopt writes nothing"


def test_list_reports_the_registry_and_the_active_login() -> None:
    with temp_harness(accounts=[("acct-a", "alpha"), ("acct-b", "")]) as h:
        write_mirror(h.auth_path, "acct-b")
        out = _Out()
        code = accounts_main(
            ["list"],
            accounts_dir=h.accounts_dir,
            registry_path=h.registry_path,
            auth_path=h.auth_path,
            out=out,
        )
        assert code == 0
        assert "alpha" in out.text and "acct-b" in out.text and "active" in out.text
        assert "no credential" in out.text, "registered but never logged in is worth saying"
    assert accounts_main([], out=_Out()) == 2, "no command is a usage error"


# ---------------------------------------------------------------------------
# Review fixes, 2026-09-09 (each guard proven to fail before the fix)
# ---------------------------------------------------------------------------


def test_no_thread_while_unavailable_and_exactly_one_once_available() -> None:
    """A Claude-only install must not grow a poller (review: major)."""
    import threading

    name = "cc-usage-codex-poll"
    alive = lambda: [t for t in threading.enumerate() if t.name == name]  # noqa: E731
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")],
                      settings={"codex_live_quota_enabled": False}) as h:
        before = len(alive())
        h.source.start()
        h.source.start()
        assert len(alive()) == before, "start() spawned while unavailable"
        h.settings["codex_live_quota_enabled"] = True
        try:
            h.source.start()
            h.source.start()
            assert len(alive()) == before + 1, "exactly one poller once available"
        finally:
            assert h.source.stop(timeout=2.0)
        assert len(alive()) == before


def test_a_warn_sentinel_withholds_the_bars_but_capped_keeps_them() -> None:
    """The split app._quota_alarm relies on: warn replaces, capped sits beside."""
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body()) if n == 1
                                    else json_response(401, {"detail": "expired"}))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert h.source.quota_rows()[0].seven_day_pct == 19.0
        h.source.force_due()
        h.source.run_cycle_once()
        row = h.source.quota_rows()[0]
        assert (row.attention_note, row.attention_kind) == (NOTE_RELOGIN, "warn")
        assert row.seven_day_pct is None and row.seven_day_resets_at is None

    capped = verified_pro_body()
    capped["rate_limit"]["limit_reached"] = True
    capped["rate_limit_reached_type"] = "weekly"
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = FakeTransport(lambda a, n: ok(capped))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        row = h.source.quota_rows()[0]
        assert row.attention_kind == "crit" and row.attention_note.startswith("capped")
        assert row.seven_day_pct == 19.0, "a capped plan keeps its evidence"


def test_a_fresh_login_clears_a_standing_sentinel_within_one_cycle() -> None:
    """The credential file moving under a warn note means the user logged in
    again: the verdict is forgotten and the account is read now, not in 30 min."""
    import os

    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = FakeTransport(lambda a, n: json_response(401, {}) if n == 1
                                    else ok(verified_pro_body()))
        h.source = h.build_source()
        path = write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert h.state(PRO_ACCOUNT_ID).note == NOTE_RELOGIN
        assert h.state(PRO_ACCOUNT_ID).next_due_wall == BASE_NOW + 1800
        # Nothing changed on disk: the backoff holds.
        h.clock.advance(60)
        h.source.run_cycle_once()
        assert h.transport.calls_for(PRO_ACCOUNT_ID) == 1
        # A new login lands (new content, later mtime).
        path = write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 10 * 86_400)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        h.clock.advance(60)
        h.source.run_cycle_once()
        assert h.transport.calls_for(PRO_ACCOUNT_ID) == 2, "read on the next cycle"
        row = h.source.quota_rows()[0]
        assert row.attention_note == "" and row.seven_day_pct == 19.0


def test_the_sidecar_forgets_accounts_the_registry_no_longer_knows() -> None:
    with temp_harness(accounts=[("acct-a", "a"), ("acct-b", "b")]) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body(account_id=a)))
        h.source = h.build_source()
        for acct in ("acct-a", "acct-b"):
            write_credential(h.accounts_dir, acct, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert set(json.loads(h.snapshots_path.read_text())["accounts"]) == {"acct-a", "acct-b"}
        h.write_registry([("acct-a", "a")])
        h.source.force_due()
        h.clock.advance(1)
        h.source.run_cycle_once()
        assert set(json.loads(h.snapshots_path.read_text())["accounts"]) == {"acct-a"}, (
            "a removed account must not keep a stale figure or a standing note on disk"
        )
        # A disabled account is NOT removed: it keeps its reading.
        h.write_registry([("acct-a", "a")], enabled=False)
        h.source.force_due()
        h.source.run_cycle_once()
        assert set(json.loads(h.snapshots_path.read_text())["accounts"]) == {"acct-a"}

    with temp_harness(accounts=[("acct-a", "a")]) as h:
        ghost = {"version": 1, "accounts": {"ghost": {"quota": None, "note": NOTE_RELOGIN,
                                                        "note_kind": "warn"}}}
        h.snapshots_path.write_text(json.dumps(ghost))
        cold = h.build_source()
        assert [r.alias for r in cold.quota_rows()] == ["a"]
        assert "ghost" not in cold._states  # noqa: SLF001


def test_an_unusable_200_for_this_account_takes_the_endpoint_ladder() -> None:
    """Distinct from an answer about ANOTHER account, which keeps the reading
    silently: a matching body with nothing in it is an endpoint fault."""
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = FakeTransport(lambda a, n: ok({"account_id": PRO_ACCOUNT_ID,
                                                     "email": PRO_EMAIL}))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        notes = []
        for _ in range(3):
            h.source.force_due()
            h.source.run_cycle_once()
            notes.append(h.state(PRO_ACCOUNT_ID).note)
        assert notes == ["", "", NOTE_ENDPOINT_ERROR], notes
        assert h.state(PRO_ACCOUNT_ID).next_due_wall == h.clock.wall + 240


def test_two_unnamed_windows_of_one_width_get_distinct_labels() -> None:
    body = verified_pro_body()
    body["rate_limit"]["primary_window"] = {"used_percent": 5, "limit_window_seconds": 3600,
                                            "reset_after_seconds": 100, "reset_at": 0}
    body["rate_limit"]["secondary_window"] = {"used_percent": 7, "limit_window_seconds": 3600,
                                              "reset_after_seconds": 200, "reset_at": 0}
    quota = CodexAccountQuota.from_response(body, credential_account_id=PRO_ACCOUNT_ID,
                                            observed_at=BASE_NOW, include_extra=False)
    assert quota is not None
    labels = [label for label, _sample in quota.scoped]
    assert len(labels) == 2 and len(set(labels)) == 2, labels
    assert labels[1].endswith("(2)"), labels


def test_a_429_streak_does_not_make_the_first_502_an_endpoint_error() -> None:
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = FakeTransport(lambda a, n: json_response(429, {}) if n <= 2
                                    else HttpResponse(502, {}, b"nope"))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        for _ in range(3):
            h.source.force_due()
            h.source.run_cycle_once()
        state = h.state(PRO_ACCOUNT_ID)
        assert state.note == NOTE_RATE_LIMITED, state.note
        assert state.consecutive_failures == 1, "the counter restarted with the class"


def test_the_connect_budget_reaches_the_transport() -> None:
    from cc_usage_widget.codex_accounts import _CONNECT_TIMEOUT

    seen: list[float] = []

    class Spy(FakeTransport):
        def get(self, url: str, headers: Any, timeout: float) -> HttpResponse:
            seen.append(timeout)
            return super().get(url, headers, timeout)

    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
        h.transport = Spy(lambda a, n: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
    assert seen == [_CONNECT_TIMEOUT] and _CONNECT_TIMEOUT == 10.0, seen


def CAPTURED_business_body() -> dict[str, Any]:
    """The self_serve_business_prolite body as PROBED on this Mac 2026-09-10
    (identity fields replaced; every other value verbatim). Differences from
    the Pro body worth a test: ``rate_limit_reached_type`` is an OBJECT,
    ``additional_rate_limits`` is ``null``, ``credits.balance`` is ``null``,
    and the plan is capped with ``allowed: false``.
    """
    return {
        "user_id": "user-biz", "account_id": "acct-biz", "email": "biz@example.test",
        "plan_type": "self_serve_business_prolite",
        "rate_limit": {"allowed": False, "limit_reached": True,
                       "primary_window": {"used_percent": 100, "limit_window_seconds": 604800,
                                          "reset_after_seconds": 462084, "reset_at": 1789497355},
                       "secondary_window": None},
        "code_review_rate_limit": None, "additional_rate_limits": None,
        "model_usage": {"gpt-6-astra": {"available": False,
                                        "available_at": "2026-09-15T18:35:56.051122Z",
                                        "credits_would_enable": True}},
        "credits": {"has_credits": False, "unlimited": False, "overage_limit_reached": False,
                    "balance": None, "approx_local_messages": None, "approx_cloud_messages": None},
        "spend_control": {"reached": False, "individual_limit": None},
        "rate_limit_reached_type": {"type": "workspace_owner_credits_depleted", "details": None},
        "promo": None, "rate_limit_reset_credits": {"available_count": 0, "applicable_available_count": 0},
    }


def test_captured_business_body_maps_to_a_capped_weekly_bar_with_its_type() -> None:
    quota = CodexAccountQuota.from_response(
        CAPTURED_business_body(), credential_account_id="acct-biz",
        observed_at=BASE_NOW, include_extra=True,
    )
    assert quota is not None
    assert quota.plan_type == "self_serve_business_prolite", "verbatim, never mapped"
    assert quota.seven_day is not None and quota.seven_day.used_percent == 100.0
    assert quota.five_hour is None and quota.scoped == (), "null extras are tolerated"
    assert quota.allowed is False and quota.limit_reached is True
    assert quota.reached_type == "workspace_owner_credits_depleted", "read from the object form"
    # roadmap 11: the reason is a CREDIT block, and the action is the sentence.
    assert quota.capped_note == "out of credits · Add credits"
    row = quota.account_row(now=BASE_NOW, slot=-3, alias="work", is_active=False,
                            note=quota.capped_note, note_kind="crit")
    assert row.seven_day_pct == 100.0, "a capped plan keeps its evidence"
    assert row.attention_kind == "crit" and "{" not in row.attention_note
    # A dict that is not the {type: str} shape yields a bare "capped", never prose of a dict.
    # `model_usage` is emptied so this control is about the reached_type SHAPE
    # alone: left in place, its `credits_would_enable` would earn the row the
    # `out of credits` verdict by the second route (roadmap 11), which is a
    # different question and has its own test.
    odd = CAPTURED_business_body(); odd["rate_limit_reached_type"] = {"details": None}
    odd["model_usage"] = {}
    q2 = CodexAccountQuota.from_response(odd, credential_account_id="acct-biz",
                                         observed_at=BASE_NOW, include_extra=False)
    assert q2 is not None and q2.reached_type is None and q2.capped_note == "capped"


# ---------------------------------------------------------------------------
# Credits, availability and the tier label (roadmap 11 + 12)
# ---------------------------------------------------------------------------


def test_the_business_body_credits_and_availability_become_facts_beside_the_bars() -> None:
    """``credits N`` and ``<model> back Sep 15`` are info lines, not verdicts.

    The captured Business body is the whole reason these fields are read: the
    workspace is at 100 % of its week AND blocked on credits until Sep 15, and
    before this the row said only ``capped
    workspace_owner_credits_depleted`` - which reads as "wait for the reset"
    when the reset is four days away and fixes nothing.

    The model is named by its own slug (``gpt-6-astra``), the choice
    ``pricing.MODEL_DISPLAY_NAMES`` already makes for every OpenAI model, so
    one surface cannot call it Astra while the cost block calls it
    gpt-6-astra.
    """
    quota = CodexAccountQuota.from_response(
        CAPTURED_business_body(), credential_account_id="acct-biz",
        observed_at=BASE_NOW, include_extra=True,
    )
    assert quota is not None
    assert quota.credits_has is False and quota.credits_would_enable is True
    assert quota.credits_balance is None, "null balance is not a zero balance"
    assert [slug for slug, _when in quota.availability] == ["gpt-6-astra"]

    row = quota.account_row(now=BASE_NOW, slot=-3, alias="work", is_active=False,
                            note=quota.capped_note, note_kind="crit")
    assert row.info_notes == ("gpt-6-astra back Sep 15",), row.info_notes
    assert row.attention_note == "out of credits · Add credits"
    assert row.seven_day_pct == 100.0, "the facts sit BESIDE the figure"


def test_a_balance_renders_as_a_count_with_no_unit_and_no_invented_precision() -> None:
    """``credits.balance`` arrives as a string on the probed Pro body and as a
    number elsewhere; both are counts, and neither is a currency."""
    body = verified_pro_body()
    body["credits"] = {"has_credits": True, "unlimited": False, "balance": "12"}
    quota = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert quota is not None and quota.credits_balance == 12.0
    row = quota.account_row(now=BASE_NOW, slot=-1, alias="pro", is_active=False)
    assert row.info_notes == ("credits 12",), row.info_notes
    assert "$" not in row.info_notes[0] and "." not in row.info_notes[0]

    body["credits"]["balance"] = 12.5
    fractional = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert fractional is not None
    assert fractional.account_row(now=BASE_NOW, slot=-1, alias="pro",
                                  is_active=False).info_notes == ("credits 12.5",)

    body["credits"]["balance"] = "not a number"
    junk = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert junk is not None and junk.credits_balance is None, "junk is silence, not zero"


def test_has_credits_false_alone_is_not_out_of_credits() -> None:
    """The probed Pro body reports ``has_credits: false`` and is perfectly
    healthy. Reading that flag alone would print "out of credits" on an
    account with 81 % of its week left - which is why the second, corroborating
    half (a model saying ``credits_would_enable``) is required.
    """
    body = verified_pro_body()
    body["rate_limit"]["limit_reached"] = True  # capped, but on the WINDOW
    quota = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert quota is not None
    assert quota.credits_has is False and quota.credits_would_enable is False
    assert quota.out_of_credits is False
    assert quota.capped_note == "capped", quota.capped_note

    # Add the corroboration and the verdict flips - and only then.
    body["model_usage"] = {"gpt-6-astra": {"available": False, "credits_would_enable": True}}
    corroborated = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert corroborated is not None and corroborated.out_of_credits is True
    assert corroborated.capped_note == "out of credits · Add credits"


def test_an_available_at_that_has_passed_is_not_printed() -> None:
    """A snapshot can be hours old, so "back Sep 15" must not still be on
    screen on Sep 16 - the same bygone-reset bug SPEC-CODEX 6 fixed for
    windows, in a new field."""
    body = CAPTURED_business_body()
    quota = CodexAccountQuota.from_response(
        body, credential_account_id="acct-biz", observed_at=BASE_NOW, include_extra=False
    )
    assert quota is not None and quota.availability
    when = quota.availability[0][1]
    assert quota.info_notes(now=when - 60) == ("gpt-6-astra back Sep 15",)
    assert quota.info_notes(now=when + 60) == (), "a bygone return date says nothing"


def test_a_model_that_is_already_available_is_not_listed() -> None:
    body = verified_pro_body()
    body["model_usage"] = {
        "gpt-6-astra": {"available": True, "available_at": "2026-09-01T00:00:00Z"},
        "gpt-5.6-luna": {"available": False, "available_at": "2026-09-15T18:35:56.051122Z"},
    }
    quota = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert quota is not None
    assert [slug for slug, _when in quota.availability] == ["gpt-5.6-luna"]


def test_the_new_fields_round_trip_and_a_legacy_sidecar_still_loads() -> None:
    """A sidecar written before roadmap 11 has none of these keys, and must
    read back as "not reported" rather than as False/0."""
    quota = CodexAccountQuota.from_response(
        CAPTURED_business_body(), credential_account_id="acct-biz",
        observed_at=BASE_NOW, include_extra=False,
    )
    assert quota is not None
    again = CodexAccountQuota.from_json(json.loads(json.dumps(quota.to_json())))
    assert again is not None
    assert again.credits_has is False and again.credits_would_enable is True
    assert again.availability == quota.availability
    assert again.capped_note == quota.capped_note

    legacy = quota.to_json()
    for key in ("credits_balance", "credits_has", "credits_would_enable", "availability"):
        del legacy[key]
    old = CodexAccountQuota.from_json(legacy)
    assert old is not None
    assert old.credits_has is None and old.credits_would_enable is False
    assert old.availability == ()
    # It still reaches the right verdict from the field it always had: the
    # explicit reached_type is route one, and it does not need the new keys.
    assert old.capped_note == "out of credits · Add credits"
    assert old.info_notes(now=BASE_NOW) == (), "a legacy record has no facts to add"


def test_a_withheld_row_withholds_its_facts_and_its_reset_too() -> None:
    """Credits, a return date and a reset epoch were all read at ``fetched_at``.

    A reading too old to show a percentage (SPEC-CODEX 6.6) is too old to show
    any of them: a six-hour-old "credits 12" is the same class of lie as a
    six-hour-old bar, and ``soonest_reset_at`` feeding the title's countdown
    makes it worse - the bar would count down to a reset nobody re-read.
    """
    body = verified_pro_body()
    body["credits"] = {"has_credits": True, "balance": 12}
    quota = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert quota is not None
    fresh = quota.account_row(now=BASE_NOW + 60, slot=-1, alias="pro", is_active=False)
    assert fresh.info_notes == ("credits 12",) and fresh.soonest_reset_at is not None

    aged = quota.account_row(
        now=BASE_NOW + CODEX_FETCH_EXPIRE_SECONDS + 60, slot=-1, alias="pro", is_active=False
    )
    assert aged.seven_day_pct is None, "the precondition: the bars are withheld"
    assert aged.info_notes == () and aged.soonest_reset_at is None

    sentinel = quota.account_row(now=BASE_NOW + 60, slot=-1, alias="pro", is_active=False,
                                 note=NOTE_RELOGIN, note_kind="warn")
    assert sentinel.info_notes == () and sentinel.soonest_reset_at is None


def test_soonest_reset_at_is_the_earliest_of_every_window() -> None:
    """The 5-hour bucket resets long before the week, and the title's countdown
    must be to the first door that opens, not to the first one listed."""
    body = SYNTHETIC_two_window_body(
        account_id=PRO_ACCOUNT_ID, primary_seconds=604_800, secondary_seconds=18_000
    )
    quota = CodexAccountQuota.from_response(
        body, credential_account_id=PRO_ACCOUNT_ID, observed_at=BASE_NOW, include_extra=False
    )
    assert quota is not None
    # The fixture's weekly window resets in 600 s and the 5-hour one in 1200 s.
    assert quota.seven_day is not None and quota.five_hour is not None
    assert quota.soonest_reset_at == BASE_NOW + 600

    empty = CodexAccountQuota(account_id="x", fetched_at=BASE_NOW)
    assert empty.soonest_reset_at is None, "no reported reset means no reset"


def test_the_pricing_tier_changes_the_label_and_never_a_number() -> None:
    """roadmap 11: rollouts do not say which tier ran and this repo carries
    standard rates only, so a non-standard tier says so instead of re-scaling
    a figure nobody measured. Nothing else reads the setting.
    """
    from cc_usage_widget.codex_accounts import pricing_tier_note

    assert pricing_tier_note({"codex_pricing_tier": "standard"}) == "(standard tier)"
    assert pricing_tier_note({"codex_pricing_tier": "fast"}) == "(fast tier rates not loaded)"
    assert pricing_tier_note({"codex_pricing_tier": "batch"}) == "(batch tier rates not loaded)"
    # A junk or absent value is the default, exactly as `normalize_settings`
    # would have made it - the label can never carry free text from the file.
    assert pricing_tier_note({"codex_pricing_tier": "premium"}) == "(standard tier)"
    assert pricing_tier_note({}) == "(standard tier)"
    assert pricing_tier_note(None) == "(standard tier)"
    assert normalize_settings({"codex_pricing_tier": "premium"})["codex_pricing_tier"] == "standard"
    assert normalize_settings({"codex_pricing_tier": "fast"})["codex_pricing_tier"] == "fast"
    assert SETTINGS_DEFAULTS["codex_pricing_tier"] == "standard"


# ---------------------------------------------------------------------------
# Pace forecast (roadmap 10)
# ---------------------------------------------------------------------------


def test_a_forecast_needs_three_samples_over_half_an_hour_and_a_rising_trend() -> None:
    """The honest rule, and it is the whole feature: below it, no note.

    ``used_percent`` is reported as a whole number, so two reads five minutes
    apart routinely differ by one rounding step - a forecast built from that
    "projects" the wall four hours out and then unprojects it on the next tick.
    """
    from cc_usage_widget.codex_accounts import PACE_MIN_SPAN_SECONDS, pace_note

    rising = [(0.0, 10.0), (1_800.0, 20.0), (3_600.0, 30.0)]
    assert pace_note(rising, now=3_600.0) == "at this pace: wall in 3h"

    assert pace_note(rising[:2], now=1_800.0) == "", "two points are a line through noise"
    short = [(0.0, 10.0), (600.0, 20.0), (1_200.0, 30.0)]
    assert 1_200.0 < PACE_MIN_SPAN_SECONDS
    assert pace_note(short, now=1_200.0) == "", "twenty minutes is not a trend"
    flat = [(0.0, 30.0), (1_800.0, 30.0), (3_600.0, 30.0)]
    assert pace_note(flat, now=3_600.0) == "", "an idle account is not heading anywhere"
    falling = [(0.0, 30.0), (1_800.0, 25.0), (3_600.0, 20.0)]
    assert pace_note(falling, now=3_600.0) == ""
    assert pace_note([], now=0.0) == "" and pace_note([("x", None)], now=0.0) == ""


def test_a_forecast_says_resets_first_when_the_window_refills_before_the_wall() -> None:
    """A countdown the plan makes impossible is worse than no countdown: the
    window reopens before the projection lands, so the wall is never reached.

    (The build contract worded this branch ``wall before reset``; the roadmap's
    own item 10 words it ``resets first``, which is the sentence that is true
    in the branch it names. Flagged for the integrator.)
    """
    from cc_usage_widget.codex_accounts import pace_note

    rising = [(0.0, 10.0), (1_800.0, 20.0), (3_600.0, 30.0)]
    # The wall is 3 h out; a reset 1 h out gets there first.
    assert pace_note(rising, now=3_600.0, reset_at=3_600.0 + 3_600) == \
        "at this pace: resets first"
    # ...and a reset AFTER the wall leaves the countdown standing.
    assert pace_note(rising, now=3_600.0, reset_at=3_600.0 + 5 * 3_600) == \
        "at this pace: wall in 3h"


def test_a_capped_row_and_a_reset_inside_the_ring_never_forecast() -> None:
    """Two silences that would otherwise produce nonsense.

    A capped row has no projection to make - the wall is here. And a ring that
    spans a reset (98 % -> 3 %) has no trend through the seam: the samples
    before the drop describe a window that no longer exists, so the tail after
    it is all that counts, and three samples of it are needed before anything
    is said.
    """
    from cc_usage_widget.codex_accounts import pace_note

    rising = [(0.0, 10.0), (1_800.0, 20.0), (3_600.0, 30.0)]
    assert pace_note(rising, now=3_600.0, capped=True) == ""

    across_a_reset = [(0.0, 90.0), (1_800.0, 95.0), (3_600.0, 3.0), (5_400.0, 6.0)]
    assert pace_note(across_a_reset, now=5_400.0) == "", "two samples after the drop"
    with_a_tail = across_a_reset + [(7_200.0, 9.0), (9_000.0, 12.0)]
    assert pace_note(with_a_tail, now=9_000.0).startswith("at this pace: wall in ")

    at_the_wall = [(0.0, 98.0), (1_800.0, 99.0), (3_600.0, 100.0)]
    assert pace_note(at_the_wall, now=3_600.0) == "", "100 % is not a forecast"


def test_a_forecast_whose_wall_is_already_behind_us_says_nothing() -> None:
    """A stale ring must not count down to a moment that has passed.

    The ring is a cache: a poll that stops (an offline laptop, a source that
    errors for an hour) leaves the last three samples in place while the clock
    runs on. The projection built from them then lands BEFORE ``now``, and
    ``coarse_duration`` floors at ``"<1m"`` - so the row advertised
    "at this pace: wall in <1m" indefinitely, a countdown to an instant that is
    already behind us. SPEC 4.3: silence, not a fabricated moment.
    """
    from cc_usage_widget.codex_accounts import pace_note

    rising = [(0.0, 10.0), (1_800.0, 20.0), (3_600.0, 30.0)]
    # 20 points per 3_600 s from 30 % puts the wall at t = 16_200. Read a
    # second later there is nothing left to forecast, and nothing is said -
    # not even with a reset far enough away to leave the countdown standing.
    wall = 16_200.0
    assert pace_note(rising, now=wall + 1) == ""
    assert pace_note(rising, now=wall + 86_400) == ""
    assert pace_note(rising, now=wall + 1, reset_at=wall + 5 * 86_400) == ""
    # Exactly at the wall is not a countdown either.
    assert pace_note(rising, now=wall) == ""
    # One second before it still is - the guard trims nothing it should not.
    assert pace_note(rising, now=wall - 1).startswith("at this pace: wall in ")


def test_the_sample_ring_is_bounded_persisted_and_survives_a_cold_start() -> None:
    """The ring lives in the sidecar so a forecast does not have to rebuild an
    hour of history after every launch, and it is bounded so the sidecar stays
    a cache."""
    from cc_usage_widget.codex_accounts import PACE_RING_SIZE

    percentages = iter(range(10, 10 + 20))
    def handler(account_id: str, attempt: int) -> Any:
        return ok(verified_pro_body(used_percent=next(percentages)))

    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")], handler=handler) as h:
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        for _ in range(PACE_RING_SIZE + 4):
            h.source.force_due()
            h.source.run_cycle_once()
            h.clock.advance(900)
        ring = h.source._samples[PRO_ACCOUNT_ID]  # noqa: SLF001
        assert len(ring) == PACE_RING_SIZE, len(ring)
        assert [pct for _when, pct in ring] == sorted(pct for _when, pct in ring)

        stored = json.loads(h.snapshots_path.read_text())
        assert len(stored["accounts"][PRO_ACCOUNT_ID]["samples"]) == PACE_RING_SIZE
        # A fresh source over the same files - what a restart looks like.
        cold = h.build_source()
        cold.quota_rows()
        assert cold._samples[PRO_ACCOUNT_ID] == ring  # noqa: SLF001
        # And it can speak on its first repaint, with no fetch of its own -
        # which is the whole reason the ring is persisted. (Which of the two
        # sentences it says is the Pro fixture's business: its window resets an
        # hour after every read, so this account always resets first.)
        assert cold.quota_rows()[0].info_notes[0].startswith("at this pace: ")


def test_the_forecast_setting_gates_the_note_and_not_the_ring() -> None:
    """Switching the forecast back on must not cost an hour of silence, so the
    samples are recorded whatever the setting says - only the note is gated."""
    percentages = iter(range(10, 40))
    def handler(account_id: str, attempt: int) -> Any:
        return ok(verified_pro_body(used_percent=next(percentages)))

    with temp_harness(
        accounts=[(PRO_ACCOUNT_ID, "a")],
        handler=handler,
        settings={"codex_pace_forecast_enabled": False},
    ) as h:
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        for _ in range(4):
            h.source.force_due()
            h.source.run_cycle_once()
            h.clock.advance(1_200)
        assert len(h.source._samples[PRO_ACCOUNT_ID]) == 4  # noqa: SLF001
        assert h.source.quota_rows()[0].info_notes == (), "the setting is off"
        h.settings["codex_pace_forecast_enabled"] = True
        note = h.source.quota_rows()[0].info_notes
        assert note and note[0].startswith("at this pace: "), note


# ---------------------------------------------------------------------------
# Earliest reset: the title suffix and the Codex fleet line (roadmap 4)
# ---------------------------------------------------------------------------


def codex_row(
    slot: int,
    alias: str,
    *,
    pct: float | None,
    reset_in: float | None = None,
    note: str = "",
    kind: str = "",
    active: bool = False,
) -> AccountRow:
    """One live per-account Codex row, in the shape ``account_row`` emits."""
    return AccountRow(
        slot=slot,
        alias=alias,
        email="",
        is_active=active,
        seven_day_pct=pct,
        seven_day_resets_at="Sep 12 14:00" if pct is not None else None,
        vendor=VENDOR_CODEX,
        switchable=False,
        plan_type="pro",
        usage_age_seconds=60.0,
        stale_after_seconds=CODEX_FETCH_STALE_SECONDS,
        attention_note=note,
        attention_kind=kind,
        soonest_reset_at=None if reset_in is None else BASE_NOW + reset_in,
    )


def test_the_title_countdown_is_never_wider_than_its_budget() -> None:
    """Four characters is the whole width this component was given, and the
    check is a sweep rather than three examples: a duration that rendered as
    ``↺100d`` would silently cost the bar a neighbour (RCA 2026-08-17).
    """
    from cc_usage_widget.render import (
        TITLE_RESET_SUFFIX_MAX,
        coarse_duration,
        title_reset_suffix,
    )

    for seconds in (30, 60, 3_599, 3_600, 86_399, 86_400, 4 * 86_400, 400 * 86_400):
        suffix = title_reset_suffix(BASE_NOW + seconds, BASE_NOW)
        assert suffix.startswith("↺") and len(suffix) <= TITLE_RESET_SUFFIX_MAX, (seconds, suffix)
    assert title_reset_suffix(BASE_NOW + 4 * 86_400, BASE_NOW) == "↺4d"
    assert title_reset_suffix(BASE_NOW + 18 * 3_600, BASE_NOW) == "↺18h"
    assert title_reset_suffix(BASE_NOW + 35 * 60, BASE_NOW) == "↺35m"
    # Rounding is DOWN: a countdown must never claim more time than was reported.
    assert coarse_duration(2 * 86_400 - 1) == "1d" and coarse_duration(3_600 - 1) == "59m"
    # No reset reported, and a reset already past: silence, never a bare glyph.
    assert title_reset_suffix(None, BASE_NOW) == ""
    assert title_reset_suffix(BASE_NOW - 60, BASE_NOW) == ""


def test_the_title_shows_the_reset_only_on_a_capped_active_row() -> None:
    """``C100%↺4d`` at the wall, ``C19%`` below it.

    The countdown answers the one question left when the room is full. Below
    the wall it is noise, and on a row whose source carried no epoch - every
    claude-swap row, and the transcript-derived Codex row that a
    live-quota-off machine shows - there is nothing to count down to, which is
    what keeps that machine's title byte-for-byte today's.
    """
    from cc_usage_widget import app as app_mod
    from cc_usage_widget.app import UiSnapshot

    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    settings.update({"title_show_icon": False, "title_show_cost": False,
                     "title_show_fleet": False, "title_show_codex_pct": True})
    app = app_mod.CCUsageWidgetApp()
    try:
        def title(row: AccountRow) -> str:
            return app.render_title(UiSnapshot(settings=settings, quota_rows=(row,)))

        capped = codex_row(-1, "vlad", pct=100.0, reset_in=4 * 86_400,
                           note="capped weekly", kind="crit", active=True)
        rendered = app._title_reset(capped, expired=False, now=BASE_NOW)
        assert rendered == "↺4d", rendered
        assert title(capped).startswith("C100%")

        # Below the wall: no countdown, whatever the reset says.
        healthy = codex_row(-1, "vlad", pct=19.0, reset_in=4 * 86_400, active=True)
        assert app._title_reset(healthy, expired=False, now=BASE_NOW) == ""
        assert title(healthy) == "C19%"

        # A source that carried no epoch adds nothing - today's title, exactly.
        no_epoch = codex_row(-1, "vlad", pct=100.0, note="capped weekly",
                             kind="crit", active=True)
        assert app._title_reset(no_epoch, expired=False, now=BASE_NOW) == ""
        assert title(no_epoch) == "C100%(!)", title(no_epoch)

        # An ENDED window counts down to nothing: its reset is in the past.
        assert app._title_reset(capped, expired=True, now=BASE_NOW) == ""
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_codex_fleet_line_counts_rooms_and_names_the_next_one() -> None:
    """``Codex 0/4 · next Sat 09:00 (vlad)`` - the twin of the Claude suffix.

    Four accounts at 100 % is the state this widget was built for, and the two
    questions at that moment are "is any of them free" and "when does one
    open". The alias is part of the answer: a bare time does not say which
    login to switch to.
    """
    from cc_usage_widget.app import _codex_fleet_heading

    rows = (
        codex_row(-1, "vlad", pct=100.0, reset_in=4 * 86_400 + 82_800, note="capped", kind="crit"),
        codex_row(-2, "work", pct=100.0, reset_in=6 * 86_400, note="capped", kind="crit"),
        codex_row(-3, "gmail", pct=100.0, reset_in=5 * 86_400, note="capped", kind="crit"),
        codex_row(-4, "spare", pct=100.0, reset_in=None, note="capped", kind="crit"),
    )
    heading = _codex_fleet_heading(rows, now=BASE_NOW)
    assert heading.startswith("Codex 0/4 · next "), heading
    assert heading.endswith(" (vlad)"), "the soonest reset names its own account"

    # One account with room: the count moves, and the "next" half still
    # describes the capped ones - it is about the doors that are shut.
    with_room = (codex_row(-1, "vlad", pct=42.0, reset_in=4 * 86_400),) + rows[1:]
    assert _codex_fleet_heading(with_room, now=BASE_NOW).startswith("Codex 1/4 · next ")

    # A withheld figure is NOT room: the last good number can be hours old, and
    # advertising it would send the operator to a dead login.
    sentinel = (codex_row(-1, "vlad", pct=None, note="relogin", kind="warn"),) + rows[1:]
    assert _codex_fleet_heading(sentinel, now=BASE_NOW).startswith("Codex 0/4 · next ")

    # Nor is an account blocked on CREDITS with most of its week unspent -
    # the captured Business shape (roadmap 11). Its percentage looks like room
    # and it is the one account that cannot run a session at all, so the count
    # keys on the verdict's KIND, not on the number beside it.
    blocked = (
        codex_row(-1, "vlad", pct=40.0, reset_in=4 * 86_400,
                  note="out of credits · Add credits", kind="crit"),
    ) + rows[1:]
    assert _codex_fleet_heading(blocked, now=BASE_NOW).startswith("Codex 0/4 · next ")

    # Nothing capped: the count stands alone rather than inventing a "next".
    free = tuple(codex_row(-(i + 1), f"a{i}", pct=10.0) for i in range(3))
    assert _codex_fleet_heading(free, now=BASE_NOW) == "Codex 3/3"


def test_the_fleet_line_never_advertises_a_reset_that_has_already_passed() -> None:
    """"next" means the next door to open, not the last one that did not.

    A capped row keeps the ``reset_at`` its source anchored at read time. If
    the source stops polling - an offline laptop, an endpoint erroring - that
    instant slides into the past while the row stays capped, and the fleet line
    went on printing it: ``Codex 0/4 · next Sat 09:00 (vlad)`` over a Saturday
    that has been and gone. That is worse than no line: it is a specific
    instruction to go and wait for a door that is not going to open.

    Both halves of the fix are asserted, because either alone would leave the
    bug reachable: a past epoch is dropped BEFORE the ``min`` (so a stale row
    cannot win the race and silence the accounts that do reopen), and
    ``fleet_reset_label`` returns ``""`` for a past epoch on its own, matching
    ``title_reset_suffix``.
    """
    from cc_usage_widget.app import _codex_fleet_heading
    from cc_usage_widget.render import fleet_reset_label

    # The label itself, first: the twin of `title_reset_suffix`'s rule.
    assert fleet_reset_label(BASE_NOW - 60, BASE_NOW) == ""
    assert fleet_reset_label(BASE_NOW, BASE_NOW) == ""
    assert fleet_reset_label(None, BASE_NOW) == ""
    assert fleet_reset_label(BASE_NOW + 3_600, BASE_NOW) != ""

    # One capped account whose reset went by an hour ago: the count still
    # describes the fleet, and the "next" half is simply absent.
    stale = (
        codex_row(-1, "vlad", pct=100.0, reset_in=-3_600, note="capped", kind="crit"),
    )
    assert _codex_fleet_heading(stale, now=BASE_NOW) == "Codex 0/1", (
        _codex_fleet_heading(stale, now=BASE_NOW)
    )

    # And a stale row must not win the race for "soonest": with a real reset
    # four days out beside it, the line names the account that really reopens.
    mixed = stale + (
        codex_row(-2, "work", pct=100.0, reset_in=4 * 86_400, note="capped", kind="crit"),
    )
    heading = _codex_fleet_heading(mixed, now=BASE_NOW)
    assert heading.startswith("Codex 0/2 · next "), heading
    assert heading.endswith(" (work)"), heading


def test_the_fleet_line_is_absent_without_a_live_fleet_or_with_the_setting_off() -> None:
    """A Codex-only machine with the live source off must lay out exactly as
    it did: the transcript-derived row (slot 0) describes whichever login wrote
    the logs and has no identity, so it is not a fleet of one."""
    from cc_usage_widget import app as app_mod
    from cc_usage_widget.app import UiSnapshot, _codex_fleet_heading

    scanned = AccountRow(
        slot=CODEX_PSEUDO_ACCOUNT_SLOT, alias="Codex", email="", is_active=False,
        seven_day_pct=19.0, vendor=VENDOR_CODEX, switchable=False, plan_type="pro",
        usage_age_seconds=60.0, stale_after_seconds=7_200.0,
    )
    claude = AccountRow(slot=1, alias="main", email="m@x", is_active=True, five_hour_pct=12.0)
    assert _codex_fleet_heading((scanned, claude), now=BASE_NOW) == ""
    assert _codex_fleet_heading((), now=BASE_NOW) == ""

    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    live = codex_row(-1, "vlad", pct=100.0, reset_in=86_400, note="capped", kind="crit")
    app = app_mod.CCUsageWidgetApp()
    try:
        on = app._quota_items(UiSnapshot(settings=settings, quota_rows=(live,)))
        assert len(on) == 2 and str(on[0].title).startswith("Codex 0/1"), [str(i.title) for i in on]
        off = dict(settings)
        off["codex_fleet_line_enabled"] = False
        items = app._quota_items(UiSnapshot(settings=off, quota_rows=(live,)))
        assert len(items) == 1 and not str(items[0].title).startswith("Codex 0/1")
        # And with only the scanned row the section is byte-for-byte the old one.
        assert len(app._quota_items(UiSnapshot(settings=settings, quota_rows=(scanned,)))) == 1
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_info_lines_reach_both_the_bar_block_and_the_plain_fallback() -> None:
    """VoiceOver reads the plain label, so it must say what the block draws."""
    from cc_usage_widget import app as app_mod
    from cc_usage_widget.app import UiSnapshot, _quota_row_label

    row = replace(
        codex_row(-1, "work", pct=100.0, reset_in=86_400,
                  note="out of credits · Add credits", kind="crit"),
        info_notes=("gpt-6-astra back Sep 15",),
    )
    plain = _quota_row_label(row)
    assert "out of credits · Add credits" in plain and "gpt-6-astra back Sep 15" in plain

    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    app = app_mod.CCUsageWidgetApp()
    try:
        items = app._quota_items(UiSnapshot(settings=settings, quota_rows=(row,)))
        drawn = str(items[-1].title)
        assert "gpt-6-astra back Sep 15" in drawn, drawn
        assert "100%" in drawn, "a fact never replaces the figure"
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


# ---------------------------------------------------------------------------
# `best` and `link` (roadmap 5)
# ---------------------------------------------------------------------------


def run_cli(harness: "Harness", *argv: str, transport: Any = None) -> tuple[int, str]:
    """The CLI over the harness's own files - registry, sidecar, settings.

    *transport* is left ``None`` for every offline command (``best``, ``link``,
    ``adopt``, ``list``), which is itself part of what those tests assert: a
    command that reached the network would build a real ``UrllibTransport``.
    """
    settings_path = harness.root / "settings.json"
    if not settings_path.exists():
        settings_path.write_text(json.dumps(harness.settings))
    out = _Out()
    code = accounts_main(
        list(argv),
        accounts_dir=harness.accounts_dir,
        registry_path=harness.registry_path,
        auth_path=harness.auth_path,
        transport=transport,
        snapshots_path=harness.snapshots_path,
        settings_path=settings_path,
        clock=harness.clock.time,
        out=out,
    )
    return code, out.text


def seed_three_accounts(harness: "Harness", percentages: dict[str, float]) -> None:
    """One healthy cycle per account, so the sidecar holds a real reading."""
    for account_id in percentages:
        write_credential(harness.accounts_dir, account_id, exp=BASE_NOW + 9 * 86_400)
    harness.transport = FakeTransport(
        lambda account_id, attempt: ok(
            verified_pro_body(account_id=account_id, used_percent=percentages[account_id])
        )
    )
    harness.source = harness.build_source()
    harness.source.run_cycle_once()


def test_best_prints_the_home_of_the_account_with_the_most_headroom() -> None:
    """The ranking is the one a person makes by eye from the menu, so the CLI
    and the menu can never disagree about which account is the good one."""
    accounts = [("acct-a", "vlad"), ("acct-b", "work"), ("acct-c", "gmail")]
    with temp_harness(accounts=accounts) as h:
        seed_three_accounts(h, {"acct-a": 80.0, "acct-b": 12.0, "acct-c": 44.0})
        code, text = run_cli(h, "best")
        assert code == 0, text
        assert text.strip() == str(h.accounts_dir / "acct-b"), text
        # The path is a real directory, which is what makes it usable as
        # CODEX_HOME - a registry entry with no credential dir is not a home.
        assert Path(text.strip()).is_dir()

    # The account with the LOWEST percentage is not the answer when it is
    # blocked on credits: the captured Business workspace is at 100 % of its
    # week today, but a credit block can stand over an unspent one, and the
    # number would then read as the most room on the machine.
    with temp_harness(accounts=[("acct-a", "vlad"), ("acct-b", "work")]) as h:
        for account_id in ("acct-a", "acct-b"):
            write_credential(h.accounts_dir, account_id, exp=BASE_NOW + 9 * 86_400)

        def handler(account_id: str, attempt: int) -> Any:
            if account_id == "acct-b":
                body = CAPTURED_business_body()
                body["account_id"] = account_id
                body["rate_limit"]["primary_window"]["used_percent"] = 4
                return ok(body)
            return ok(verified_pro_body(account_id=account_id, used_percent=61))

        h.transport = FakeTransport(handler)
        h.source = h.build_source()
        h.source.run_cycle_once()
        blocked = {row.alias: row for row in h.source.quota_rows()}["work"]
        assert blocked.seven_day_pct == 4.0 and blocked.attention_kind == "crit"
        code, text = run_cli(h, "best")
        assert code == 0 and text.strip() == str(h.accounts_dir / "acct-a"), text


def test_best_falls_back_to_the_soonest_reset_when_every_account_is_capped() -> None:
    """Four accounts at 100 % is the ordinary state on this machine; the answer
    then is not "none", it is "this one, in four days"."""
    accounts = [("acct-a", "vlad"), ("acct-b", "work")]
    with temp_harness(accounts=accounts) as h:
        for account_id in ("acct-a", "acct-b"):
            write_credential(h.accounts_dir, account_id, exp=BASE_NOW + 9 * 86_400)

        def handler(account_id: str, attempt: int) -> Any:
            body = verified_pro_body(account_id=account_id, used_percent=100)
            body["rate_limit"]["limit_reached"] = True
            body["rate_limit"]["allowed"] = False
            # acct-b reopens first.
            body["rate_limit"]["primary_window"]["reset_after_seconds"] = (
                6 * 86_400 if account_id == "acct-a" else 4 * 86_400
            )
            return ok(body)

        h.transport = FakeTransport(handler)
        h.source = h.build_source()
        h.source.run_cycle_once()
        code, text = run_cli(h, "best")
        assert code == 0, text
        assert text.strip() == str(h.accounts_dir / "acct-b"), text

        code, payload = run_cli(h, "best", "--json")
        parsed = json.loads(payload)
        assert code == 0 and parsed["alias"] == "work"
        assert parsed["reason"] == "all capped; soonest reset"
        assert parsed["weekly_used_percent"] == 100.0
        # Anchored to the instant of the READ, not to the start of the cycle:
        # the poller spaces requests out, so acct-b was read a little after
        # BASE_NOW and its reset is four days from THAT (SPEC-CODEX 6.5).
        assert BASE_NOW + 4 * 86_400 <= parsed["reset_at"] < BASE_NOW + 4 * 86_400 + 300


def test_best_exits_2_with_one_line_when_nothing_is_usable() -> None:
    """``codexb`` propagates the code, so a refusal must fail loudly rather
    than launch Codex with ``CODEX_HOME=`` (which silently means ~/.codex)."""
    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        # 1. The feature is off: nothing has been read, so nothing may be ranked.
        h.settings["codex_live_quota_enabled"] = False
        code, text = run_cli(h, "best")
        assert code == 2 and "codex_live_quota_enabled" in text and text.count("\n") == 1

    with temp_harness() as h:
        # 2. No registry at all.
        code, text = run_cli(h, "best")
        assert code == 2 and "registry" in text, text

    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        # 3. A registered account whose credential is dead is not a place to
        #    send work, and it is the only one: refuse rather than pick it.
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)
        h.source.run_cycle_once()
        assert h.source.quota_rows()[0].attention_note == NOTE_RELOGIN
        code, text = run_cli(h, "best")
        assert code == 2 and "attention" in text, text
        code, payload = run_cli(h, "best", "--json")
        assert code == 2 and json.loads(payload)["home"] is None


def test_best_makes_no_request_even_when_the_sidecar_is_empty() -> None:
    """It is a local answer to a local question. The source is built with a
    transport that RAISES, so the promise is enforced by construction: a future
    edit that reached the endpoint from this CLI would fail here, loudly."""
    from cc_usage_widget.codex_accounts import _OfflineTransport

    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        code, text = run_cli(h, "best")
        assert code == 2, text  # no reading yet, and none was fetched to fix that
        assert h.transport.calls == [], "best must not poll"
        assert not h.snapshots_path.exists(), "and must not write the widget's sidecar"

        # With a reading present, it still only reads: the sidecar is the
        # running widget's file and a second process must not touch it.
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body(account_id="acct-a")))
        h.source = h.build_source()
        h.source.run_cycle_once()
        before = h.snapshots_path.read_bytes()
        assert run_cli(h, "best")[0] == 0
        assert h.snapshots_path.read_bytes() == before
    try:
        _OfflineTransport().get("https://example.invalid", {}, 1.0)
    except OSError as exc:
        assert "no network request" in str(exc)
    else:  # pragma: no cover - the guard is the test
        raise AssertionError("the offline transport answered a request")


def test_link_is_idempotent_and_never_overwrites_a_real_file() -> None:
    """Each account home holds one file - auth.json - so a session started with
    it sees none of the user's config. ``link`` mirrors the config in, and the
    two things it must never do are overwrite something a user put there and
    write anything at all under ~/.codex."""
    from cc_usage_widget.codex_accounts import COPY_TARGETS, LINK_TARGETS

    with temp_harness(accounts=[("acct-a", "vlad"), ("acct-b", "work")]) as h:
        codex_home = h.auth_path.parent
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "config.toml").write_text("model = 'gpt-6-astra'\n")
        (codex_home / "AGENTS.md").write_text("# agents\n")
        (codex_home / "skills").mkdir()
        (codex_home / "skills" / "a.md").write_text("skill\n")
        before = sorted(p.name for p in codex_home.iterdir())
        for account_id in ("acct-a", "acct-b"):
            write_credential(h.accounts_dir, account_id, exp=BASE_NOW + 9 * 86_400)
        # A real file the user put in one home: it must survive untouched.
        mine = h.accounts_dir / "acct-a" / "AGENTS.md"
        mine.write_text("mine, not a link\n")

        code, text = run_cli(h, "link")
        assert code == 0, text
        home = h.accounts_dir / "acct-a"
        # The writable name is a COPY, never a link (see the next test).
        assert not (home / "config.toml").is_symlink()
        assert (home / "config.toml").read_text() == "model = 'gpt-6-astra'\n"
        assert (home / "skills").is_symlink()
        assert os.readlink(home / "skills") == str(codex_home / "skills")
        assert not mine.is_symlink() and mine.read_text() == "mine, not a link\n"
        # A name that does not exist upstream is skipped, not linked to nothing.
        assert not (home / "plugins").exists()
        assert sorted(p.name for p in codex_home.iterdir()) == before, "~/.codex untouched"

        # Idempotent: a second run creates nothing and still exits 0.
        code, again = run_cli(h, "link")
        assert code == 0 and "linked 0" in again, again
        assert ", copied" not in again, again  # nothing was copied a second time
        assert (home / "config.toml").read_text() == "model = 'gpt-6-astra'\n"
        assert set(LINK_TARGETS) >= {"AGENTS.md", "skills"}
        assert set(COPY_TARGETS) == {"config.toml", "memories"}
        assert not (set(LINK_TARGETS) & set(COPY_TARGETS)), "one name, one treatment"


def test_a_writable_name_is_copied_so_codex_cannot_write_through_into_dot_codex() -> None:
    """SPEC-CODEX 6.3, defended against a process this program does not run.

    ``config.toml`` and ``memories/`` are the names Codex itself writes to. As
    symlinks - which is what this command made until 2026-09-10 - the first
    ``codex config set`` inside a ``codexb`` session rewrote ``~/.codex``
    through the link: the widget's one hard promise about ``~/.codex`` broken
    by a write the widget never makes and could never log.

    The write below is the whole test: a real edit in the account home, and
    then the upstream file asserted byte for byte.
    """
    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        codex_home = h.auth_path.parent
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "config.toml").write_text("model = 'gpt-6-astra'\n")
        (codex_home / "memories").mkdir()
        (codex_home / "memories" / "notes.md").write_text("shared note\n")
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)

        assert run_cli(h, "link")[0] == 0
        home = h.accounts_dir / "acct-a"
        assert not (home / "config.toml").is_symlink(), "a writable name must be a copy"
        assert not (home / "memories").is_symlink(), "a writable tree must be a copy"
        assert (home / "memories" / "notes.md").read_text() == "shared note\n"

        # What Codex does in that home, done by hand.
        (home / "config.toml").write_text("model = 'gpt-5.6-sol'\n")
        (home / "memories" / "notes.md").write_text("this account's note\n")

        assert (codex_home / "config.toml").read_text() == "model = 'gpt-6-astra'\n", (
            "the session wrote through into ~/.codex"
        )
        assert (codex_home / "memories" / "notes.md").read_text() == "shared note\n", (
            "the session wrote through into ~/.codex"
        )
        # And a re-run keeps the per-account edit rather than restoring upstream.
        assert run_cli(h, "link")[0] == 0
        assert (home / "config.toml").read_text() == "model = 'gpt-5.6-sol'\n"


def test_link_converts_a_write_through_symlink_left_by_the_old_version() -> None:
    """A machine already linked is the machine most at risk, so it is migrated.

    Replacing OUR link with a copy of the file it pointed at changes no content
    and is the only way the fix reaches a home that was linked yesterday.
    Somebody else's link, pointing anywhere else, is still left alone.
    """
    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        codex_home = h.auth_path.parent
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "config.toml").write_text("model = 'gpt-6-astra'\n")
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        home = h.accounts_dir / "acct-a"
        # Exactly what the previous version of `link` created.
        os.symlink(codex_home / "config.toml", home / "config.toml")
        elsewhere = h.root / "someone-elses-config.toml"
        elsewhere.write_text("not ours\n")
        os.symlink(elsewhere, home / "memories")

        code, text = run_cli(h, "link")
        assert code == 0, text
        assert not (home / "config.toml").is_symlink(), "the write-through link survived"
        assert (home / "config.toml").read_text() == "model = 'gpt-6-astra'\n"
        assert (home / "memories").is_symlink(), "a link we did not make stays"
        assert os.readlink(home / "memories") == str(elsewhere)
        (home / "config.toml").write_text("model = 'gpt-5.6-sol'\n")
        assert (codex_home / "config.toml").read_text() == "model = 'gpt-6-astra'\n"


def test_unlink_removes_only_the_links_it_made() -> None:
    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        codex_home = h.auth_path.parent
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / "config.toml").write_text("model = 'gpt-6-astra'\n")
        (codex_home / "rules").mkdir()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        home = h.accounts_dir / "acct-a"
        elsewhere = h.root / "somewhere-else"
        elsewhere.mkdir()
        os.symlink(elsewhere, home / "agents")  # somebody else's link
        (home / "AGENTS.md").write_text("mine\n")

        assert run_cli(h, "link")[0] == 0
        code, text = run_cli(h, "link", "--unlink")
        assert code == 0, text
        assert not (home / "rules").exists(), "the link it made is gone"
        # The copy is that home's own file now - un-linking is not un-configuring.
        assert (home / "config.toml").is_file() and not (home / "config.toml").is_symlink()
        assert (home / "config.toml").read_text() == "model = 'gpt-6-astra'\n"
        assert (home / "agents").is_symlink(), "a link we did not make stays"
        assert (home / "AGENTS.md").read_text() == "mine\n"
        assert (home / "auth.json").is_file(), "the credential is never touched"
        assert (codex_home / "config.toml").read_text() == "model = 'gpt-6-astra'\n"


# ---------------------------------------------------------------------------
# Token refresh, default ON since CX-4 (roadmap 13, SPEC-CODEX 6.4)
# ---------------------------------------------------------------------------
#
# The feature this suite must be hardest on, because its failure mode is not a
# wrong number on a menu — it is Vlad logged out of the account he is coding
# in. Four properties are asserted rather than described:
#
#   off      with ``codex_refresh_enabled`` explicitly False, NOT ONE POST is
#            made. Proven by a transport whose ``post_form`` fails the test.
#   order    the rotated tokens are on disk BEFORE the access token they carry
#            authorises anything. Proven from inside the usage handler, which
#            reads ``auth.json`` at the moment the GET is made.
#   no replay
#            the superseded refresh token is gone from the account directory —
#            no ``.prev``, no backup, nothing to re-send by accident.
#   silence  no token value reaches a log line, a diagnostics line or the
#            sidecar. Same canary method as ``tests/test_privacy.py``.
#
# Everything runs over real files in a TemporaryDirectory with an injected
# clock and transport; nothing here reaches the network or ``~/.codex``.

REFRESH_CANARY = "SECRET_REFRESH_CANARY_7C2D9_do_not_leak"
"""Planted as the stored refresh token and as the rotated one. Not a plausible
value in a real ``auth.json``, so a substring hit cannot be a coincidence."""


class RefreshingTransport(FakeTransport):
    """A :class:`FakeTransport` that can also answer the grant POST.

    Unlike ``get``, this one DOES record what it was given: the form body is
    the thing under test — which refresh token was sent, and how many times —
    and there is no other way to prove that a superseded token was never
    replayed. The recording lives in the test process and never reaches a file
    the widget writes, which is exactly what the canary test below asserts.
    """

    def __init__(
        self,
        handler: Callable[[str, int], Any],
        grant: Callable[[dict, int], Any],
    ) -> None:
        super().__init__(handler)
        self._grant = grant
        self.posts: list[dict] = []

    def post_form(self, url: str, data: Any, timeout: float) -> HttpResponse:
        assert url == "https://auth.openai.com/oauth/token", url
        body = dict(data)
        self.posts.append(body)
        result = self._grant(body, len(self.posts))
        if isinstance(result, BaseException):
            raise result
        return result


class NoPostTransport(FakeTransport):
    """The off-state control: any POST at all fails the test on the spot."""

    def post_form(self, url: str, data: Any, timeout: float) -> HttpResponse:
        raise AssertionError(
            "codex_refresh_enabled is off and a token request was made anyway"
        )


def grant_body(
    *,
    account_id: str,
    exp: float,
    refresh: str = "rotated-refresh-token",
    access: str | None = None,
    include_refresh: bool = True,
) -> dict[str, Any]:
    """The shape OpenAI's ``/oauth/token`` returns for a refresh_token grant."""
    body: dict[str, Any] = {
        "access_token": access
        or access_token(account_id=account_id, email="who@example.test", plan="pro", exp=exp),
        "id_token": "rotated-id-token",
        "token_type": "Bearer",
        "expires_in": 864_000,
    }
    if include_refresh:
        body["refresh_token"] = refresh
    return body


def tokens_on_disk(harness: "Harness", account_id: str) -> dict[str, Any]:
    auth = harness.accounts_dir / account_id / "auth.json"
    return json.loads(auth.read_text())["tokens"]


def test_the_refresh_switch_is_declared_and_defaults_on() -> None:
    """CX-4. A key only ``app.py`` knew about would be dropped on the next
    save. The default is ON since 2026-09-25: off, every widget copy of a
    ten-day token expired unannounced on Sep 20 and stayed dead for five days;
    the CX-2/CX-3 guards remove the risk the off default protected against.
    An explicit False must still survive a save - it is the off switch."""
    assert SETTINGS_DEFAULTS["codex_refresh_enabled"] is True
    assert normalize_settings({})["codex_refresh_enabled"] is True
    assert normalize_settings({"codex_refresh_enabled": False})["codex_refresh_enabled"] is False
    assert normalize_settings({"codex_refresh_enabled": True})["codex_refresh_enabled"] is True
    assert normalize_settings({"codex_refresh_enabled": "yes"})["codex_refresh_enabled"] is True, (
        "a hand-edited junk value falls back to the declared default"
    )


def test_refresh_due_needs_a_known_exp_and_covers_a_passed_one() -> None:
    """A token we cannot date is never rotated on a guess; one that has already
    expired IS rotated, because that is the last chance to avoid a browser."""
    unknown = Credential(account_id="a", access_token="x", exp=None)
    assert refresh_due(unknown, BASE_NOW) is False
    far = Credential(account_id="a", access_token="x", exp=BASE_NOW + 9 * 86_400)
    assert refresh_due(far, BASE_NOW) is False
    near = Credential(account_id="a", access_token="x", exp=BASE_NOW + 23 * 3_600)
    assert refresh_due(near, BASE_NOW) is True
    gone = Credential(account_id="a", access_token="x", exp=BASE_NOW - 60)
    assert refresh_due(gone, BASE_NOW) is True


def test_the_switch_off_makes_not_one_token_request() -> None:
    """The whole default state, asserted by construction: the transport fails
    the test if it is ever asked to post, and the auth.json bytes must be
    identical after a cycle over a token that is an hour from expiry."""
    with temp_harness(
        accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": False}
    ) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        before = auth.read_bytes()
        h.transport = NoPostTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id))
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert h.settings["codex_refresh_enabled"] is False, "the explicit off switch"
        assert auth.read_bytes() == before, "the widget wrote to a credential file with refresh off"
        assert h.rows_by_alias()["vlad"].seven_day_pct == 19.0, "the poll still happened"


def test_a_401_with_the_switch_off_still_makes_no_token_request() -> None:
    """The reactive path is gated on the same switch as the proactive one — a
    401 must not become a back door into rotating a credential."""
    with temp_harness(
        accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": False}
    ) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        before = auth.read_bytes()
        h.transport = NoPostTransport(lambda account_id, attempt: json_response(401, {}))
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert h.rows_by_alias()["vlad"].attention_note == NOTE_RELOGIN
        assert auth.read_bytes() == before


def test_a_token_inside_24h_is_rotated_and_persisted_before_it_is_used() -> None:
    """Persist-before-use, asserted from inside the request it authorises.

    The usage handler reads ``auth.json`` at the moment the GET is made: if the
    rotation were applied after the request (or only in memory), the file would
    still carry the old tokens here and this test would fail. That ordering is
    the whole safety property — a crash between the POST and the write must
    cost one grant, never the account.
    """
    new_access = access_token(
        account_id="acct-a", email="who@example.test", plan="pro", exp=BASE_NOW + 10 * 86_400
    )
    seen: list[dict[str, Any]] = []
    auth_holder: list[Any] = [None]

    def handler(account_id: str, attempt: int) -> Any:
        # Read the credential file AT THE MOMENT the authorised request is made.
        seen.append(dict(json.loads(auth_holder[0].read_text())["tokens"]))
        return ok(verified_pro_body(account_id=account_id))

    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        auth_holder[0] = write_credential(
            h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600, refresh=REFRESH_CANARY
        )
        h.transport = RefreshingTransport(
            handler,
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400,
                                          access=new_access)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert len(h.transport.posts) == 1, "exactly one grant"
        assert h.transport.posts[0] == {
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_CANARY,
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        }
        assert seen and seen[0]["access_token"] == new_access, (
            "the usage request was made before the rotation reached the disk"
        )
        assert seen[0]["refresh_token"] == "rotated-refresh-token"
        assert h.rows_by_alias()["vlad"].seven_day_pct == 19.0


def test_the_superseded_refresh_token_is_gone_with_no_replay_copy() -> None:
    """No ``.prev``, no backup, no temp file left behind: the only way to ask
    OpenAI's reuse detector a question is to do it on purpose."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600, refresh=REFRESH_CANARY)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        home = h.accounts_dir / "acct-a"
        assert sorted(p.name for p in home.iterdir()) == ["auth.json"], "a second file appeared"
        assert tokens_on_disk(h, "acct-a")["refresh_token"] == "rotated-refresh-token"
        for path in home.rglob("*"):
            assert REFRESH_CANARY not in path.read_text(errors="replace"), (
                f"the superseded refresh token survived in {path.name}"
            )
        assert (home / "auth.json").stat().st_mode & 0o777 == 0o600


def test_a_rotation_keeps_every_other_key_the_codex_cli_wrote() -> None:
    """A refresh that quietly dropped ``auth_mode`` would break the CLI for the
    account it was trying to help, so the whole object is written back."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        raw = json.loads(auth.read_text())
        raw["some_future_key"] = {"kept": True}
        auth.write_text(json.dumps(raw))
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        after = json.loads(auth.read_text())
        assert after["some_future_key"] == {"kept": True}
        assert after["auth_mode"] == "chatgpt"
        assert after["tokens"]["account_id"] == "acct-a", "identity is never rewritten"
        assert after["last_refresh"].endswith("Z")


def test_a_grant_without_a_rotated_refresh_token_keeps_the_stored_one() -> None:
    """Blanking it would turn the next proactive tick into a hard relogin."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600, refresh="stored-refresh")
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(
                grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400, include_refresh=False)
            ),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert tokens_on_disk(h, "acct-a")["refresh_token"] == "stored-refresh"


def test_a_401_refreshes_exactly_once_and_re_reads_with_the_new_token() -> None:
    """Reactive once: 401 → one grant → one retry. A second 401 in the same
    poll must NOT buy a second grant, or a dead family becomes a POST loop."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: (
                json_response(401, {}) if attempt == 1 else ok(verified_pro_body(account_id=account_id))
            ),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert len(h.transport.posts) == 1, "exactly one grant per poll"
        assert h.transport.calls_for("acct-a") == 2, "the read was retried once"
        row = h.rows_by_alias()["vlad"]
        assert row.seven_day_pct == 19.0 and row.attention_note == ""


def test_a_401_that_survives_the_refresh_relogins_without_a_second_grant() -> None:
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: json_response(401, {}),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert len(h.transport.posts) == 1
        assert h.transport.calls_for("acct-a") == 2
        assert h.rows_by_alias()["vlad"].attention_note == NOTE_RELOGIN
        assert h.state("acct-a").next_due_wall == BASE_NOW + 1_800


def test_a_proactive_refresh_spends_the_polls_one_grant() -> None:
    """The once-per-poll guard, from the other side: a token that was just
    rotated and STILL gets a 401 must not buy a second grant in the same poll.
    Two grants for one poll is how a dead family becomes a POST loop, and it is
    also the shape that would trip reuse detection on a rotated token."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: json_response(401, {}),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert len(h.transport.posts) == 1, "the proactive grant did not spend the poll's one try"
        assert h.transport.calls_for("acct-a") == 1, "and there was nothing to retry with"
        assert h.rows_by_alias()["vlad"].attention_note == NOTE_RELOGIN


def test_the_refresher_refuses_a_world_readable_credential_on_its_own() -> None:
    """Belt and braces, asserted directly on :class:`TokenRefresher`.

    The poller happens to check the mode first (``CredentialStore.read`` runs
    before any rotation), so this rule would be untested through the source -
    and an untested rule in the ONE class with a write path to a credential
    file is exactly the kind that quietly stops holding.
    """
    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600, mode=0o644)
        before = auth.read_bytes()
        transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        refresher = TokenRefresher(
            transport,
            CredentialStore(h.accounts_dir, auth_path=h.auth_path, clock=h.clock.time),
            clock=h.clock.time,
            log=h.logs.append,
        )
        outcome = refresher.refresh("acct-a", active=None, label="vlad")

        assert outcome.status == "failed" and "refusing to rotate" in outcome.detail
        assert transport.posts == [], "a leaky credential bought a grant anyway"
        assert refresher.posts == 0
        assert auth.read_bytes() == before


def test_a_403_never_refreshes() -> None:
    """A 403 is an answer about permissions: the token in hand is the one it
    refused, and rotating it would spend a grant to be refused again."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: json_response(403, {"detail": "no access"}),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert h.transport.posts == [], "a 403 bought a token request"
        assert h.rows_by_alias()["vlad"].attention_note == NOTE_NO_ACCESS


def test_invalid_grant_with_a_live_token_counts_down_and_never_retries() -> None:
    """The one terminal answer: the family is gone, so no retry - but the
    ACCESS token is still in date and still works until ``exp`` (SMC-1: the
    old contract skipped the read here, and the next poll's 200 proved the
    token was never dead). The row keeps its figures and counts down to the
    real deadline; only an expired token turns the refusal into ``relogin``."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        before = auth.read_bytes()
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: json_response(400, {"error": "invalid_grant"}),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert len(h.transport.posts) == 1, "one attempt, never a retry"
        assert h.transport.calls == ["acct-a"], "the in-date token still reads"
        row = h.rows_by_alias()["vlad"]
        assert (row.attention_note, row.attention_kind) == ("relogin in 1h", "info"), row
        assert row.seven_day_pct == 19.0
        assert auth.read_bytes() == before, "a refused grant must not touch the file"

        h.clock.advance(300)
        h.source.run_cycle_once()
        assert len(h.transport.posts) == 1, "a dead family is not asked twice"


def test_a_failed_refresh_changes_nothing_and_the_old_token_still_reads() -> None:
    """5xx, offline, garbage: the stored token is valid until ``exp``, so a
    failed rotation is a diagnostics line, not a sentinel."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        before = auth.read_bytes()
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: json_response(503, {"error": "server_error"}),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert auth.read_bytes() == before
        row = h.rows_by_alias()["vlad"]
        assert row.seven_day_pct == 19.0, "the old token still worked"
        assert row.attention_note.startswith(NOTE_RELOGIN + " in"), "the countdown is unchanged"
        assert any("refresh failed" in line for line in h.source.diagnostics())


def test_an_unreachable_token_endpoint_is_a_failure_not_a_relogin() -> None:
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: OSError("ConnectionRefusedError"),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert h.rows_by_alias()["vlad"].seven_day_pct == 19.0
        assert any("unreachable" in line for line in h.source.diagnostics())


def test_a_rotation_that_cannot_be_persisted_is_never_used() -> None:
    """The strict half of persist-before-use. With the home read-only the write
    fails; the new access token must then be discarded, not used, because a
    token no restart could ever find is worse than the expiry we already have.
    """
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        before = auth.read_bytes()
        new_access = access_token(
            account_id="acct-a", email="who@example.test", plan="pro", exp=BASE_NOW + 10 * 86_400
        )
        used: list[str] = []

        class _Recording(RefreshingTransport):
            def get(self, url: str, headers: Any, timeout: float) -> HttpResponse:
                used.append(dict(headers).get("Authorization", ""))
                return super().get(url, headers, timeout)

        h.transport = _Recording(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(
                grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400, access=new_access)
            ),
        )
        h.source = h.build_source()
        os.chmod(h.accounts_dir / "acct-a", 0o500)
        try:
            h.source.run_cycle_once()
        finally:
            os.chmod(h.accounts_dir / "acct-a", 0o700)

        assert auth.read_bytes() == before
        assert used and new_access not in used[0], "an unpersisted token authorised a request"
        assert any("could not be persisted" in line for line in h.source.diagnostics())


def test_a_credential_other_users_can_read_is_never_rotated() -> None:
    """Same rule as the read path: a token another user can read is one to
    rotate by hand. Writing to it would only mint a second leaked token."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600, mode=0o644)
        before = auth.read_bytes()
        h.transport = NoPostTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id))
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert auth.read_bytes() == before
        assert h.rows_by_alias()["vlad"].attention_note == NOTE_NO_CREDENTIAL


def test_our_own_rotation_does_not_look_like_a_fresh_login() -> None:
    """A refresh rewrites ``auth.json``, which is exactly the signal the cycle
    uses to spot a human running ``codex login`` under a standing sentinel. If
    the signature were not re-stamped after our own write, the next cycle would
    clear the verdict and cancel its hour of backoff — every single cycle."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: json_response(403, {"detail": "no access"}),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert h.rows_by_alias()["vlad"].attention_note == NOTE_NO_ACCESS

        h.clock.advance(300)
        h.source.run_cycle_once()
        assert h.transport.calls_for("acct-a") == 1, "the backoff was cancelled by our own write"
        assert len(h.transport.posts) == 1
        assert h.rows_by_alias()["vlad"].attention_note == NOTE_NO_ACCESS


def test_no_token_value_from_a_refresh_reaches_a_log_or_the_sidecar() -> None:
    """The canary method of ``tests/test_privacy.py``, applied to the second
    secret this feature handles: the refresh token, before and after rotation.
    """
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        auth = write_credential(
            h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600, refresh=REFRESH_CANARY
        )
        rotated = REFRESH_CANARY + "-rotated"
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(
                grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400, refresh=rotated)
            ),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert REFRESH_CANARY in auth.read_text(), "the canary was not planted"
        assert h.logs, "nothing was logged at all - the log check proves nothing"
        assert any("refreshed" in line for line in h.logs), "the rotation must be logged"
        haystack = list(h.logs) + list(h.source.diagnostics())
        haystack.append(h.snapshots_path.read_text())
        for line in haystack:
            assert REFRESH_CANARY not in line, "a refresh token escaped"


# ---------------------------------------------------------------------------
# Credential engine 2026-09-25: CX-1 desktop mirror, CX-2 dead families, CX-3
# refresh guards, CX-4 default on, OPS-3/OPS-4 logging, CX-7a countdown
# ---------------------------------------------------------------------------
#
# The outage these exist for: every widget copy of a ten-day Codex token
# expired on 2026-09-20 with refresh off, the ~/.codex account's copy included,
# while the ChatGPT app held a fresh token for exactly that account - and the
# log said nothing for five days.


def write_desktop_login(
    auth_path: Path,
    account_id: str,
    *,
    exp: float | None,
    token: str | None = None,
    mode: int = 0o600,
) -> Path:
    """``~/.codex/auth.json`` as the ChatGPT app writes it: a full login."""
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "account_id": account_id,
                    "access_token": token
                    or access_token(
                        account_id=account_id, email="who@example.test", plan="pro", exp=exp
                    ),
                    "refresh_token": "desktop-rt",
                    "id_token": "desktop-id",
                },
            }
        )
    )
    os.chmod(auth_path, mode)
    return auth_path


class HeaderRecordingTransport(RefreshingTransport):
    """Records which bearer each GET carried. Test-process memory only - the
    point is to prove WHICH credential authorised a request."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.bearers: list[str] = []

    def get(self, url: str, headers: Any, timeout: float) -> HttpResponse:
        self.bearers.append(dict(headers).get("Authorization", ""))
        return super().get(url, headers, timeout)


def _no_grant(data: Any, n: int) -> Any:
    return AssertionError("a token grant was requested where none may be")


def test_the_desktop_login_speaks_for_its_own_account_when_the_widget_copy_is_dead() -> None:
    """CX-1, the literal complaint: the ~/.codex account's row said relogin for
    five days while the app's own login for that account was fresh."""
    with temp_harness(accounts=[("acct-a", "personal")]) as h:  # shipped default: refresh ON
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)
        mirror = write_desktop_login(h.auth_path, "acct-a", exp=BASE_NOW + 5 * 86_400)
        before_bytes, before_mtime = mirror.read_bytes(), mirror.stat().st_mtime_ns
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)), _no_grant
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert h.transport.calls == ["acct-a"], h.transport.calls
        assert h.transport.posts == [], "never a refresh with the desktop token"
        row = h.rows_by_alias()["personal"]
        assert row.attention_note == "", row.attention_note
        assert row.seven_day_pct == 19.0 and row.is_active
        assert NOTE_VIA_DESKTOP == "via ChatGPT app login"
        assert NOTE_VIA_DESKTOP in row.info_notes, row.info_notes
        assert row.credential_expires_at == BASE_NOW + 5 * 86_400
        assert h.state("acct-a").credential_source == "desktop"
        assert mirror.read_bytes() == before_bytes, "the widget wrote to ~/.codex"
        assert mirror.stat().st_mtime_ns == before_mtime, "the widget touched ~/.codex"


def test_a_desktop_login_for_another_account_never_speaks_for_this_one() -> None:
    """Identity is the file's ``tokens.account_id``, never a shared email."""
    with temp_harness(
        accounts=[("acct-a", "personal"), ("acct-b", "work")],
        settings={"codex_refresh_enabled": False},
    ) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)
        write_credential(h.accounts_dir, "acct-b", exp=BASE_NOW + 9 * 86_400)
        write_desktop_login(h.auth_path, "acct-b", exp=BASE_NOW + 5 * 86_400)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)), _no_grant
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert h.transport.calls_for("acct-a") == 0
        assert h.rows_by_alias()["personal"].attention_note == NOTE_RELOGIN
        assert NOTE_VIA_DESKTOP not in h.rows_by_alias()["personal"].info_notes
        assert h.source._credentials.read_desktop("acct-a") is None  # noqa: SLF001
        assert h.transport.posts == []


def test_a_refused_desktop_token_retries_once_with_the_widget_copy_and_never_refreshes() -> None:
    """A 401 on the app's token: one retry with our own copy if it is still in
    date, else relogin - and in neither case a grant request."""
    desktop_token = access_token(
        account_id="acct-a", email="who@example.test", plan="pro", exp=BASE_NOW + 5 * 86_400
    )
    with temp_harness(accounts=[("acct-a", "personal")]) as h:
        # 3 days: outside the 48 h countdown, older than the app's 5-day token.
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3 * 86_400)
        write_desktop_login(h.auth_path, "acct-a", exp=None, token=desktop_token)
        h.transport = HeaderRecordingTransport(
            lambda account_id, attempt: (
                json_response(401, {}) if attempt == 1 else ok(verified_pro_body(account_id=account_id))
            ),
            _no_grant,
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert h.transport.posts == []
        assert h.transport.calls == ["acct-a", "acct-a"], "exactly one retry"
        assert h.transport.bearers[0] == f"Bearer {desktop_token}", "the newer desktop token went first"
        assert h.transport.bearers[1] != h.transport.bearers[0], "the retry used the widget copy"
        row = h.rows_by_alias()["personal"]
        assert row.seven_day_pct == 19.0 and row.attention_note == ""
        assert NOTE_VIA_DESKTOP not in row.info_notes, "the reading came from the widget copy"

    with temp_harness(accounts=[("acct-a", "personal")]) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)
        write_desktop_login(h.auth_path, "acct-a", exp=BASE_NOW + 5 * 86_400)
        h.transport = RefreshingTransport(lambda account_id, attempt: json_response(401, {}), _no_grant)
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert h.transport.posts == []
        assert h.transport.calls == ["acct-a"], "no widget copy in date, so no retry"
        assert h.rows_by_alias()["personal"].attention_note == NOTE_RELOGIN


def test_a_desktop_login_other_users_can_read_is_refused() -> None:
    """The same 0o077 rule as our own files, and it says why."""
    with temp_harness(accounts=[("acct-a", "personal")]) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)
        write_desktop_login(h.auth_path, "acct-a", exp=BASE_NOW + 5 * 86_400, mode=0o644)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)), _no_grant
        )
        h.source = h.build_source()
        h.source.run_cycle_once()

        assert h.transport.calls == [] and h.transport.posts == []
        assert h.rows_by_alias()["personal"].attention_note == NOTE_RELOGIN
        assert any("0644" in line for line in h.source.diagnostics())


def test_the_app_rewriting_its_login_clears_a_standing_relogin_within_one_tick() -> None:
    """``_credential_sig`` watches ~/.codex too while it is this account's."""
    with temp_harness(accounts=[("acct-a", "personal")]) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)
        write_mirror(h.auth_path, "acct-a")  # logged in, but no usable token in it
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)), _no_grant
        )
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert h.rows_by_alias()["personal"].attention_note == NOTE_RELOGIN

        h.clock.advance(60)
        write_desktop_login(h.auth_path, "acct-a", exp=BASE_NOW + 5 * 86_400)
        h.source.run_cycle_once()  # 60 s later, well inside the 300 s interval
        assert h.transport.calls == ["acct-a"], "the app's rewrite was not noticed"
        assert h.rows_by_alias()["personal"].attention_note == ""


def test_invalid_grant_is_not_retried_on_later_polls_until_the_file_changes() -> None:
    """CX-2: before, a dead grant cost one POST per poll (288/day/account)."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: json_response(400, {"error": "invalid_grant"}),
        )
        h.source = h.build_source()
        for _ in range(6):
            h.source.run_cycle_once()
            h.clock.advance(300)
        assert len(h.transport.posts) == 1, h.transport.posts
        assert h.rows_by_alias()["vlad"].attention_note == NOTE_RELOGIN

        auth = write_credential(
            h.accounts_dir, "acct-a", exp=BASE_NOW - 60, refresh="refresh-after-a-new-login"
        )
        h.source.run_cycle_once()
        assert len(h.transport.posts) == 2, "a new login earns exactly one new attempt"

        st = auth.stat()
        sidecar = json.loads(h.snapshots_path.read_text())
        assert sidecar["accounts"]["acct-a"]["refresh_dead_sig"] == [st.st_mtime_ns, st.st_size]

        h.source = h.build_source()  # cold start over the same home
        h.clock.advance(300)
        h.source.run_cycle_once()
        assert len(h.transport.posts) == 2, "a restart bought a dead POST"
        assert h.rows_by_alias()["vlad"].attention_note == NOTE_RELOGIN


def test_the_account_codex_is_logged_in_as_is_never_refreshed() -> None:
    """CX-3: our copy of the ~/.codex account may share the app's refresh
    family; rotating it would log the app out."""
    with temp_harness(accounts=[("acct-a", "personal")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        write_mirror(h.auth_path, "acct-a")
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert h.transport.posts == []
        # Nobody will renew this copy, so its deadline is real (CX-7a).
        assert h.rows_by_alias()["personal"].attention_note.startswith(NOTE_RELOGIN + " in")

    # The control: logged in as a TRACKED second account, so the guard really
    # names acct-b (an untracked id used to fall through the same fail-open
    # branch the positive case was meant to exclude, SEC-1).
    with temp_harness(
        accounts=[("acct-a", "personal"), ("acct-b", "work")],
        settings={"codex_refresh_enabled": True},
    ) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        write_credential(h.accounts_dir, "acct-b", exp=BASE_NOW + 3_600)
        write_mirror(h.auth_path, "acct-b")
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert h.source.active_account_id() == "acct-b", "the control must be a tracked login"
        assert len(h.transport.posts) == 1, "acct-a rotated, acct-b (the app's) did not"
        assert tokens_on_disk(h, "acct-a")["refresh_token"] == "rotated-refresh-token"
        assert tokens_on_disk(h, "acct-b")["refresh_token"] == "refresh-for-acct-b"


# ---------------------------------------------------------------------------
# Review round 1 (2026-09-25): SEC-1 fail-closed guard, SMC-1 proactive
# refusal, SMC-3 desktop backstop, SEC-2 rotation temp files
# ---------------------------------------------------------------------------

TORN_MIRROR = '{"auth_mode": "chatgpt", "tokens": {"acco'
"""What a read lands on while the ChatGPT app rewrites ~/.codex/auth.json in
place: the first bytes of the new file."""
ID_LESS_MIRROR = '{"auth_mode": "chatgpt", "tokens": {}}'


class _NotifySnap:
    """The attributes ``notify.detect`` reads from a UiSnapshot, and no more."""

    def __init__(self, rows: Any) -> None:
        self.accounts = ()
        self.quota_rows = rows
        self.account_notes: dict[Any, Any] = {}
        self.cost = None


def _refreshing(h: Harness) -> RefreshingTransport:
    return RefreshingTransport(
        lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
        lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
    )


def test_a_cold_start_on_a_torn_mirror_never_refreshes_the_last_known_desktop_account() -> None:
    """SEC-1. The guard used to read "unknown this tick" as "nobody": a restart
    whose first read of ~/.codex landed mid-rewrite POSTed a grant for the
    widget copy of the very account the app is logged in as. The last account
    a read ever named is persisted and stays protected until a read names
    another; a torn, id-less or missing file is not such a read."""
    for bad in (TORN_MIRROR, ID_LESS_MIRROR, None):
        with temp_harness(
            accounts=[("acct-a", "personal")], settings={"codex_refresh_enabled": True}
        ) as h:
            write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
            write_mirror(h.auth_path, "acct-a")
            h.transport = _refreshing(h)
            h.source = h.build_source()
            h.source.run_cycle_once()  # a healthy run learns who the app is
            sidecar = json.loads(h.snapshots_path.read_text())
            assert sidecar["desktop_account_id"] == "acct-a", sidecar.keys()

            # The widget copy is now due (and even expired); the app rewrites.
            write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)
            if bad is None:
                h.auth_path.unlink()
            else:
                write_mirror(h.auth_path, None, raw=bad)
            h.clock.advance(3_600)  # far past the store's 600 s in-memory grace
            h.source = h.build_source()  # cold start
            h.source.run_cycle_once()
            assert h.transport.posts == [], (bad, "a grant for the app's account")
            assert h.source.active_account_id() is None, "the UI marker is not faked"


def test_a_definitive_no_login_read_releases_the_persisted_desktop_account() -> None:
    """R2-SEC1. The persisted id was cleared by nothing, so after the app
    logged out (file gone) or moved to API-key mode (no `tokens`), that
    account's widget copy was never rotated again and sat in `relogin`. An
    API-key file releases it at once, cold or warm; a missing file releases it
    once it has been missing on every poll for longer than the grace - a torn
    read in between restarts that clock, and a torn file alone never does."""
    api_key = '{"OPENAI_API_KEY": "sk-placeholder", "tokens": null}'

    def learned(h: Harness) -> None:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        write_mirror(h.auth_path, "acct-a")
        h.transport = _refreshing(h)
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert json.loads(h.snapshots_path.read_text())["desktop_account_id"] == "acct-a"
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)

    # API-key mode, cold start: released on the first read.
    with temp_harness(accounts=[("acct-a", "personal")], settings={"codex_refresh_enabled": True}) as h:
        learned(h)
        write_mirror(h.auth_path, None, raw=api_key)
        h.clock.advance(3_600)
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert len(h.transport.posts) == 1, "an API-key ~/.codex protects nothing"
        assert "desktop_account_id" not in json.loads(h.snapshots_path.read_text())
        assert sum("holds no login any more" in line for line in h.logs) == 1, h.logs

    # API-key mode, warm: released once the store's own grace lets go.
    with temp_harness(accounts=[("acct-a", "personal")], settings={"codex_refresh_enabled": True}) as h:
        learned(h)
        write_mirror(h.auth_path, None, raw=api_key)
        h.clock.advance(3_600)
        h.source.force_due()
        h.source.run_cycle_once()
        assert len(h.transport.posts) == 1, "warm, API-key: released"

    # Missing file: held through the grace, released past it.
    with temp_harness(accounts=[("acct-a", "personal")], settings={"codex_refresh_enabled": True}) as h:
        learned(h)
        h.auth_path.unlink()
        h.clock.advance(3_600)
        h.source = h.build_source()  # cold start on a missing file
        h.source.run_cycle_once()
        assert h.transport.posts == [], "a fresh absence is not yet definitive"
        h.clock.advance(300)
        h.source.force_due()
        h.source.run_cycle_once()
        assert h.transport.posts == [], "300 s missing: still held"
        write_mirror(h.auth_path, None, raw=TORN_MIRROR)  # the file is back, torn
        h.clock.advance(60)
        h.source.force_due()
        h.source.run_cycle_once()
        h.auth_path.unlink()
        h.clock.advance(400)  # 760 s since the first miss; this poll restarts it
        h.source.force_due()
        h.source.run_cycle_once()
        assert h.transport.posts == [], "a torn read restarts the absence clock"
        h.clock.advance(601)  # 601 s missing on every poll
        h.source.force_due()
        h.source.run_cycle_once()
        assert len(h.transport.posts) == 1, "missing past the grace: released"
        assert "desktop_account_id" not in json.loads(h.snapshots_path.read_text())

    # Control: a torn file, however long, never releases it.
    with temp_harness(accounts=[("acct-a", "personal")], settings={"codex_refresh_enabled": True}) as h:
        learned(h)
        write_mirror(h.auth_path, None, raw=TORN_MIRROR)
        for _ in range(4):
            h.clock.advance(3_600)
            h.source.force_due()
            h.source.run_cycle_once()
        assert h.transport.posts == [], "torn proves nothing"
        assert json.loads(h.snapshots_path.read_text())["desktop_account_id"] == "acct-a"


def test_no_account_is_refreshed_while_the_desktop_login_was_never_readable() -> None:
    """SEC-1, first run: nothing persisted and the mirror unreadable - the one
    account to leave alone cannot be named, so none is rotated, with ONE log
    line. A later read that names the app's account (a tracked one here)
    restores rotation for everyone else. A missing file or an API-key file
    holds no OAuth login, so rotation there needs no knowledge at all."""
    with temp_harness(
        accounts=[("acct-a", "personal"), ("acct-b", "work")],
        settings={"codex_refresh_enabled": True},
    ) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        write_credential(h.accounts_dir, "acct-b", exp=BASE_NOW + 9 * 86_400)
        write_mirror(h.auth_path, None, raw=TORN_MIRROR)
        h.transport = _refreshing(h)
        h.source = h.build_source()
        h.source.run_cycle_once()
        h.source.force_due()
        h.source.run_cycle_once()
        assert h.transport.posts == []
        skipped = [line for line in h.logs if "token refresh skipped" in line]
        assert len(skipped) == 1, h.logs
        row = h.rows_by_alias()["personal"]
        assert row.attention_note == "" and row.seven_day_pct == 19.0, (
            "a deferred rotation is not a deadline: no countdown flash"
        )

        write_mirror(h.auth_path, "acct-b")
        h.source.force_due()
        h.source.run_cycle_once()
        assert len(h.transport.posts) == 1, "acct-a rotates once the app is named"

    for harmless in (None, '{"OPENAI_API_KEY": "sk-placeholder", "tokens": null}'):
        with temp_harness(
            accounts=[("acct-a", "personal")], settings={"codex_refresh_enabled": True}
        ) as h:
            write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
            if harmless is not None:
                write_mirror(h.auth_path, None, raw=harmless)
            h.transport = _refreshing(h)
            h.source = h.build_source()
            h.source.run_cycle_once()
            assert len(h.transport.posts) == 1, harmless
            assert not any("token refresh skipped" in line for line in h.logs)


def test_the_refresher_itself_refuses_the_account_codex_is_logged_in_as() -> None:
    """SEC-1 defence in depth: the guard lives in TokenRefresher too, so a
    caller that skipped its own check still cannot rotate the app's account -
    nor any account while that one is unknown. `active` is a required
    keyword, so no caller can forget to say."""
    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        before = auth.read_bytes()
        transport = _refreshing(h)
        refresher = TokenRefresher(
            transport,
            CredentialStore(h.accounts_dir, auth_path=h.auth_path, clock=h.clock.time),
            clock=h.clock.time,
            log=h.logs.append,
        )
        for active in ("acct-a", REFRESH_GUARD_UNKNOWN):
            outcome = refresher.refresh("acct-a", active=active, label="vlad")
            assert outcome.status == "failed" and "refused" in outcome.detail, outcome
        assert transport.posts == [] and refresher.posts == 0
        assert auth.read_bytes() == before

        assert refresher.refresh("acct-a", active="acct-b", label="vlad").status == "ok"
        assert len(transport.posts) == 1, "the control: another account's login blocks nothing"


def test_probe_refresh_refuses_the_account_codex_is_logged_in_as() -> None:
    with temp_harness(accounts=[("acct-a", "gmail")]) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        before = auth.read_bytes()
        write_mirror(h.auth_path, "acct-a")
        transport = _refreshing(h)
        code, text = run_cli(h, "probe-refresh", "gmail", transport=transport)
        assert code == 1 and "refused" in text, text
        assert transport.posts == [] and auth.read_bytes() == before


def test_a_proactive_refusal_with_a_live_token_counts_down_once_and_never_flaps() -> None:
    """SMC-1, the verifier's timeline: exp = now + 20 h (inside the 24 h
    proactive window) and the grant answers invalid_grant. Before: cycle 0 said
    `relogin` (warn) with no read, cycle 1 read a 200 and logged a false
    `recovered`, and the real expiry announced relogin a third time. Now: the
    row keeps its figures and counts down, one log line names the dead family,
    nothing `recovered`, and the only relogin verdict comes at the real exp."""
    from cc_usage_widget import notify

    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 20 * 3_600)
        before = auth.read_bytes()
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: json_response(400, {"error": "invalid_grant"}),
        )
        h.source = h.build_source()
        previous = None
        seen: list[tuple[str, str, list[str]]] = []
        reads: list[int] = []
        for delta in (0, 300, 300, 20 * 3_600, 300):
            h.clock.advance(delta)
            h.source.run_cycle_once()
            rows = h.source.quota_rows()
            snap = _NotifySnap(rows)
            keys = [e.key for e in notify.detect(previous, snap, now=h.clock.time())]
            previous = snap
            seen.append((rows[0].attention_note, rows[0].attention_kind, keys))
            reads.append(len(h.transport.calls))

        assert len(h.transport.posts) == 1, "a dead family is asked once"
        for note, kind, _ in seen[:3]:
            assert note.startswith(NOTE_RELOGIN + " in") and kind == "info", seen
        assert seen[0][2] and all(k.startswith("expiring:") for k in seen[0][2]), seen[0]
        assert seen[1][2] == [] and seen[2][2] == [], "no flap between polls"
        assert seen[3][0] == NOTE_RELOGIN and seen[3][1] == "warn", seen[3]
        assert [k for k in seen[3][2] if k.startswith("attention:")], seen[3]
        assert seen[4][2] == [], seen[4]
        assert reads[0] == 1, "the in-date token read on the refusal's own poll"
        assert reads[4] == reads[3] == reads[2], "no read once the token expired"
        refused = [line for line in h.logs if "refresh refused" in line]
        assert len(refused) == 1 and refused[0].startswith("codex vlad:"), h.logs
        assert not any("recovered" in line for line in h.logs), h.logs
        relogin = [line for line in h.logs if line.startswith("codex vlad: relogin")]
        assert len(relogin) == 1 and "access token expired" in relogin[0], h.logs
        assert auth.read_bytes() == before


def test_the_codex_accounts_widget_copy_has_no_countdown_while_the_app_backs_it() -> None:
    """SMC-3: the widget copy of the ~/.codex account is never rotated, so it
    counted down and notify said "login expires ... Log in again" - but the
    app's own login for that account is unexpired and renewed by the app, and
    carries the row when the copy runs out. No countdown, and the row's
    deadline is the app's login, not the copy's. When ~/.codex moves to
    another account the backstop is gone and the countdown returns."""
    from cc_usage_widget import notify

    with temp_harness(
        accounts=[("acct-a", "personal"), ("acct-b", "work")],
        settings={"codex_refresh_enabled": False},
    ) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 20 * 3_600)
        write_credential(h.accounts_dir, "acct-b", exp=BASE_NOW + 9 * 86_400)
        # Older than the widget copy, so the widget copy is the one used.
        write_desktop_login(h.auth_path, "acct-a", exp=BASE_NOW + 10 * 3_600)
        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)), _no_grant
        )
        h.source = h.build_source()
        h.source.run_cycle_once()
        row = h.rows_by_alias()["personal"]
        assert h.state("acct-a").credential_source == "widget"
        assert (row.attention_note, row.attention_kind) == ("", ""), row.attention_note
        assert row.credential_expires_at == BASE_NOW + 10 * 3_600, "the app's login, not the copy"
        events = notify.detect(None, _NotifySnap(h.source.quota_rows()), now=h.clock.time())
        assert not [e for e in events if e.key.startswith("expiring:")], events

        write_mirror(h.auth_path, "acct-b")  # the app logs in elsewhere
        h.source.force_due()
        h.source.run_cycle_once()
        row = h.rows_by_alias()["personal"]
        assert row.attention_note.startswith(NOTE_RELOGIN + " in"), row.attention_note
        assert row.credential_expires_at == BASE_NOW + 20 * 3_600


def test_a_rotation_that_fails_mid_write_leaves_no_temp_file() -> None:
    """SEC-2, the exception half: every failure between mkstemp and replace
    unlinks the temp file, which holds the NEW tokens. Its name carries the
    writer's pid so the sweep can tell a dead writer's file from a live one."""
    from cc_usage_widget import codex_accounts as accounts_mod

    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        before = auth.read_bytes()
        h.transport = _refreshing(h)
        h.source = h.build_source()
        sources: list[str] = []
        real_replace = accounts_mod.os.replace

        def failing_replace(src: Any, dst: Any) -> None:
            if Path(dst).name == "auth.json":
                sources.append(Path(src).name)
                raise OSError(28, "No space left on device")
            real_replace(src, dst)

        accounts_mod.os.replace = failing_replace
        try:
            h.source.run_cycle_once()
        finally:
            accounts_mod.os.replace = real_replace

        assert sources and sources[0].startswith(f"auth.json.tmp.{os.getpid()}_"), sources
        home = h.accounts_dir / "acct-a"
        assert sorted(p.name for p in home.iterdir()) == ["auth.json"], list(home.iterdir())
        assert auth.read_bytes() == before
        assert any("could not be persisted" in line for line in h.source.diagnostics())


def test_the_poller_sweeps_a_killed_rotations_temp_file_and_nothing_else() -> None:
    """SEC-2, the kill half: a SIGKILL between write and replace leaves
    `auth.json.tmp.*` beside the credential, where the widget-home sweep never
    looks. The poller's first cycle removes it when it is over ten minutes old
    and its writer is dead - never a live writer's file, never a fresh one,
    never auth.json."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        before = auth.read_bytes()
        home = h.accounts_dir / "acct-a"
        old = h.clock.time() - 3_600
        planted = {
            f"auth.json.tmp.{dead.pid}_x1": (old, False),
            "auth.json.tmp.legacyrand": (old, False),  # mkstemp name before the pid
            f"auth.json.tmp.{os.getpid()}_x2": (old, True),  # a live writer
            f"auth.json.tmp.{dead.pid}_x3": (h.clock.time() - 60, True),  # too fresh
            "notes.txt": (old, True),
        }
        for name, (mtime, _) in planted.items():
            (home / name).write_text("{}")
            os.utime(home / name, (mtime, mtime))
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body(account_id=a)))
        h.source = h.build_source()
        h.source.run_cycle_once()

        left = {p.name for p in home.iterdir()}
        for name, (_, kept) in planted.items():
            assert (name in left) is kept, (name, sorted(left))
        assert "auth.json" in left and auth.read_bytes() == before
        swept = [line for line in h.logs if "stale rotation temp file" in line]
        assert len(swept) == 2 and all("acct-a" in line for line in swept), h.logs

        (home / "auth.json.tmp.legacy2").write_text("{}")
        os.utime(home / "auth.json.tmp.legacy2", (old, old))
        h.source.force_due()
        h.source.run_cycle_once()
        assert (home / "auth.json.tmp.legacy2").exists(), "at most hourly, not per cycle"


def test_a_rotation_temp_file_too_young_at_the_first_sweep_is_swept_later() -> None:
    """R2-SEC-1. launchd restarts a killed widget within seconds, so the
    poller's first sweep sees the orphan ~5 s old - under the 10 minute floor
    - and the sweep used to run once per process: the file, holding the
    rotated refresh token, stayed for the life of the process. It is now
    re-run hourly, and nothing sooner (one listdir per account an hour)."""
    hour = 3_600  # the contract: at most hourly (ROTATION_SWEEP_INTERVAL_SECONDS)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with temp_harness(accounts=[("acct-a", "vlad")]) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        home = h.accounts_dir / "acct-a"
        orphan = home / f"auth.json.tmp.{dead.pid}_kill1"
        orphan.write_text('{"tokens": {"refresh_token": "SYNTHETIC-ROTATED"}}')
        written = h.clock.time() - 5
        os.utime(orphan, (written, written))
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body(account_id=a)))
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert orphan.exists(), "5 s old: too young to judge"

        h.clock.advance(hour - 60)
        h.source.force_due()
        h.source.run_cycle_once()
        assert orphan.exists(), "not re-swept inside the hour"

        h.clock.advance(60)
        h.source.force_due()
        h.source.run_cycle_once()
        assert not orphan.exists(), "an hour on, the dead writer's temp file is gone"
        assert sum("stale rotation temp file" in line for line in h.logs) == 1, h.logs


def test_the_rotation_sweep_survives_a_pid_token_no_process_can_own() -> None:
    """R2-SEC2. `auth.json.tmp.<digits>_x` with digits >= 2**31 made os.kill
    raise OverflowError, which escaped the never-raises sweep and cost the
    poll cycle; a non-ASCII digit (`isdigit` but not `int`) did the same with
    ValueError. Both are judged on age alone now, and the sweep goes on to
    the next file."""
    import time

    from cc_usage_widget import codex_accounts as accounts_mod

    with tempfile.TemporaryDirectory() as name:
        accounts = Path(name)
        home = accounts / "acct-a"
        home.mkdir()
        (home / "auth.json").write_text("{}")
        now = time.time()
        old = now - 3_600
        names = [
            "auth.json.tmp.99999999999999999999999_x",
            f"auth.json.tmp.{2**31}_y",
            "auth.json.tmp.\u00b2_z",
            "auth.json.tmp.legacyrand",
        ]
        for leaf in names:
            (home / leaf).write_text("{}")
            os.utime(home / leaf, (old, old))
        logs: list[str] = []
        removed = accounts_mod.sweep_rotation_orphans(accounts, now=now, log=logs.append)
        assert removed == 4, (removed, sorted(p.name for p in home.iterdir()))
        assert sorted(p.name for p in home.iterdir()) == ["auth.json"]


def test_a_cli_rotation_during_our_post_is_not_clobbered() -> None:
    """CX-3 compare-before-write: the CLI's newer file wins."""
    cli_exp = BASE_NOW + 9 * 86_400

    def build(h: Harness) -> RefreshingTransport:
        def grant(data: Any, n: int) -> Any:
            write_credential(h.accounts_dir, "acct-a", exp=cli_exp, refresh="cli-rotated")
            return ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400,
                                 refresh="widget-rotated"))
        return RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)), grant
        )

    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        refresher = TokenRefresher(
            build(h), CredentialStore(h.accounts_dir, auth_path=h.auth_path, clock=h.clock.time),
            clock=h.clock.time, log=h.logs.append,
        )
        outcome = refresher.refresh("acct-a", active=None, label="vlad")
        assert outcome.status == REFRESH_RACED, outcome
        assert outcome.credential is not None and outcome.credential.exp == cli_exp, "re-read from disk"
        assert tokens_on_disk(h, "acct-a")["refresh_token"] == "cli-rotated"

    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        h.transport = build(h)
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert tokens_on_disk(h, "acct-a")["refresh_token"] == "cli-rotated"
        raced = [l for l in h.logs if "auth.json changed during refresh; kept the newer file" in l]
        assert len(raced) == 1 and raced[0].startswith("codex vlad:"), h.logs
        row = h.rows_by_alias()["vlad"]
        assert row.seven_day_pct == 19.0 and row.credential_expires_at == cli_exp


def test_a_nested_reuse_error_is_terminal() -> None:
    """OpenAI's reuse/expiry codes arrive nested; before, ``_to_str`` of the
    object was ``""`` and the dead family was re-asked every poll."""
    for body in (
        {"error": {"code": "refresh_token_reused"}},
        {"error": {"code": "refresh_token_expired"}},
        {"error": {"code": "refresh_token_invalidated"}},
        {"error": "invalid_grant"},
    ):
        with temp_harness(
            accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}
        ) as h:
            write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60)
            h.transport = RefreshingTransport(
                lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
                lambda data, n, body=body: json_response(400, body),
            )
            h.source = h.build_source()
            for _ in range(3):
                h.source.run_cycle_once()
                h.clock.advance(300)
            assert len(h.transport.posts) == 1, (body, len(h.transport.posts))
            assert h.rows_by_alias()["vlad"].attention_note == NOTE_RELOGIN, body


def test_a_refusal_after_a_concurrent_rotation_is_not_terminal() -> None:
    """The refusal was about the token the CLI just superseded: re-read, go on."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": True}) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)

        def grant(data: Any, n: int) -> Any:
            write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400, refresh="cli-rotated")
            return json_response(400, {"error": {"code": "refresh_token_reused"}})

        h.transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)), grant
        )
        h.source = h.build_source()
        h.source.run_cycle_once()
        assert h.state("acct-a").dead_refresh_sig is None
        row = h.rows_by_alias()["vlad"]
        assert row.attention_note == "" and row.seven_day_pct == 19.0
        assert tokens_on_disk(h, "acct-a")["refresh_token"] == "cli-rotated"


def test_the_settings_menu_offers_the_refresh_switch_while_live_quota_is_on() -> None:
    """CX-4: the switch used to be a hand-edit of settings.json."""
    from cc_usage_widget import app as app_mod
    from cc_usage_widget.app import UiSnapshot

    title = "Refresh Codex logins automatically"
    original = app_mod.CODEX_ACCOUNTS_REGISTRY_PATH
    app = app_mod.CCUsageWidgetApp()
    try:
        with tempfile.TemporaryDirectory() as name:
            registry = Path(name) / "codex_accounts.json"
            registry.write_text('{"version": 1, "accounts": []}', encoding="utf-8")
            app_mod.CODEX_ACCOUNTS_REGISTRY_PATH = registry

            def menu(**overrides: Any) -> Any:
                settings = normalize_settings({**SETTINGS_DEFAULTS, **overrides})
                return app._settings_submenu(UiSnapshot(settings=settings))

            assert title not in list(menu(codex_live_quota_enabled=False).keys())
            assert menu(codex_live_quota_enabled=True)[title].state == 1, "default on"
            off = menu(codex_live_quota_enabled=True, codex_refresh_enabled=False)[title]
            assert off.state == 0, "the checkmark mirrors the setting"
            assert off.callback is not None
    finally:
        app_mod.CODEX_ACCOUNTS_REGISTRY_PATH = original
        app._running = False
        app._worker.stop(timeout=2.0)


def test_a_dead_credential_is_logged_once_and_its_recovery_once() -> None:
    """OPS-3: widget.log had zero Codex lines from Sep 20 11:13 to Sep 25."""
    with temp_harness(accounts=[("acct-a", "vlad")], settings={"codex_refresh_enabled": False}) as h:
        h.transport = FakeTransport(lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)))
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 3_600, refresh=REFRESH_CANARY)
        h.source.run_cycle_once()
        h.clock.advance(300)
        h.source.run_cycle_once()
        relogin = [line for line in h.logs if "relogin" in line]
        assert len(relogin) == 1, h.logs
        assert relogin[0].startswith("codex vlad: relogin (access token expired "), relogin
        for line in h.logs:
            assert REFRESH_CANARY not in line and "eyJ" not in line, "a token-like value was logged"

        h.clock.advance(300)
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        h.source.run_cycle_once()
        assert [line for line in h.logs if "recovered" in line] == ["codex vlad: recovered"], h.logs

        h.clock.advance(300)
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW - 60, refresh="another-length-x")
        h.source.force_due()
        h.source.run_cycle_once()
        assert len([line for line in h.logs if "relogin" in line]) == 2
        h.source = h.build_source()  # a restart must not announce it again
        h.clock.advance(300)
        h.source.run_cycle_once()
        assert len([line for line in h.logs if "relogin" in line]) == 2, h.logs


def test_a_steady_200_is_logged_once_and_a_change_is_logged() -> None:
    """OPS-4: the per-poll ``200 in N ms`` line was 91 % of widget.log."""
    holder: list[Harness] = []

    def handler(account_id: str, attempt: int) -> Any:
        if attempt == 6:
            holder[0].clock.mono += 4.0  # a slow answer is news even at 200
        return json_response(429, {}) if attempt == 4 else ok(verified_pro_body(account_id=account_id))

    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")], handler=handler) as h:
        holder.append(h)
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 9 * 86_400)
        for _ in range(3):
            h.source.force_due()
            h.source.run_cycle_once()
        assert [l for l in h.logs if l.startswith("codex a: ")] == ["codex a: 200 in 0 ms"], h.logs
        h.source.force_due(); h.source.run_cycle_once()
        assert "codex a: 429 in 0 ms" in h.logs
        h.source.force_due(); h.source.run_cycle_once()
        assert h.logs.count("codex a: 200 in 0 ms") == 2, "the change back to 200 is news"
        h.source.force_due(); h.source.run_cycle_once()
        assert "codex a: 200 in 4000 ms" in h.logs
        assert h.source._requests == 6, "every request still counts"  # noqa: SLF001


def test_no_countdown_while_refresh_is_on_and_healthy() -> None:
    """CX-7a: a healthy refresher rotates at 24 h, so a 48 h countdown would
    cry wolf for a day. The row still carries the claim for anyone who asks."""
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")], settings={"codex_refresh_enabled": True}) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 40 * 3_600)
        h.source.run_cycle_once()
        row = h.source.quota_rows()[0]
        assert row.attention_note == "", row.attention_note
        assert row.credential_expires_at == BASE_NOW + 40 * 3_600


def test_a_capped_row_keeps_its_relogin_countdown() -> None:
    """CX-7a: the capped verdict used to swallow the deadline entirely."""
    body = CAPTURED_business_body()
    with temp_harness(accounts=[("acct-biz", "work")], settings={"codex_refresh_enabled": False}) as h:
        h.transport = FakeTransport(lambda a, n: ok(body))
        h.source = h.build_source()
        write_credential(h.accounts_dir, "acct-biz", exp=BASE_NOW + 30 * 3_600)
        h.source.run_cycle_once()
        row = h.rows_by_alias()["work"]
        assert (row.attention_note, row.attention_kind) == ("out of credits · Add credits", "crit")
        assert "relogin in 1d 6h" in row.info_notes, row.info_notes


def test_diagnostics_give_the_last_status_its_age() -> None:
    """CX-7a: on the expired path no request is made, and a bare ``200`` read
    as current for five days."""
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")], settings={"codex_refresh_enabled": False}) as h:
        h.transport = FakeTransport(lambda a, n: ok(verified_pro_body()))
        h.source = h.build_source()
        write_credential(h.accounts_dir, PRO_ACCOUNT_ID, exp=BASE_NOW + 3_600)
        h.source.run_cycle_once()
        h.clock.advance(5 * 86_400)
        h.source.run_cycle_once()
        line = next(l for l in h.source.diagnostics() if l.startswith("  a: "))
        assert line.startswith("  a: last 200 5d ago, "), line
        assert NOTE_RELOGIN in line


# ---------------------------------------------------------------------------
# `probe-refresh` (SPEC-CODEX 6.4)
# ---------------------------------------------------------------------------


def test_probe_refresh_shows_the_rotation_and_proves_the_new_token() -> None:
    """The command that produces the evidence for the switch: one grant, one
    usage request, expiry before and after, and never a token on stdout."""
    with temp_harness(accounts=[("acct-a", "gmail")]) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600, refresh=REFRESH_CANARY)
        transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        code, text = run_cli(h, "probe-refresh", "gmail", transport=transport)

        assert code == 0, text
        assert len(transport.posts) == 1 and transport.calls_for("acct-a") == 1
        assert "before" in text and "after" in text
        assert "2026-09-09 11:00" in text and "2026-09-19 10:00" in text
        assert "HTTP 200" in text and "weekly 19.0%" in text
        assert REFRESH_CANARY not in text and "rotated-refresh-token" not in text
        assert tokens_on_disk(h, "acct-a")["refresh_token"] == "rotated-refresh-token"


def test_probe_refresh_runs_with_the_switch_off() -> None:
    """It is the probe that opens the switch, so requiring the switch would be
    circular. The setting gates the widget's polling, never this command."""
    with temp_harness(accounts=[("acct-a", "gmail")]) as h:
        assert h.settings.get("codex_refresh_enabled", False) is False
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 9 * 86_400)
        transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        code, text = run_cli(h, "probe-refresh", "gmail", transport=transport)
        assert code == 0, text


def test_probe_refresh_reports_a_refused_grant_and_exits_1() -> None:
    with temp_harness(accounts=[("acct-a", "gmail")]) as h:
        auth = write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        before = auth.read_bytes()
        transport = RefreshingTransport(
            lambda account_id, attempt: ok(verified_pro_body(account_id=account_id)),
            lambda data, n: json_response(400, {"error": "invalid_grant"}),
        )
        code, text = run_cli(h, "probe-refresh", "gmail", transport=transport)
        assert code == 1
        assert "invalid_grant" in text
        assert transport.calls == [], "no usage request after a refused grant"
        assert auth.read_bytes() == before


def test_probe_refresh_fails_when_the_rotated_token_is_not_accepted() -> None:
    """The half that matters for the soak: a grant can succeed and still mint a
    token the API refuses, and the probe must say so rather than exit 0."""
    with temp_harness(accounts=[("acct-a", "gmail")]) as h:
        write_credential(h.accounts_dir, "acct-a", exp=BASE_NOW + 3_600)
        transport = RefreshingTransport(
            lambda account_id, attempt: json_response(401, {}),
            lambda data, n: ok(grant_body(account_id="acct-a", exp=BASE_NOW + 10 * 86_400)),
        )
        code, text = run_cli(h, "probe-refresh", "gmail", transport=transport)
        assert code == 1
        assert "NOT accepted" in text


def test_probe_refresh_needs_a_known_account() -> None:
    with temp_harness(accounts=[("acct-a", "gmail")]) as h:
        assert run_cli(h, "probe-refresh")[0] == 2
        code, text = run_cli(h, "probe-refresh", "nobody")
        assert code == 1 and "no registry entry" in text


# ---------------------------------------------------------------------------
# Runner (pytest is not installed in claude-swap's venv)
# ---------------------------------------------------------------------------


def _tests() -> list[tuple[str, Any]]:
    items = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    return sorted(items, key=lambda pair: pair[1].__code__.co_firstlineno)


def main() -> int:
    tests = _tests()
    failures: list[str] = []
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
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
