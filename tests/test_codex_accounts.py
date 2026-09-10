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
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_usage_widget.codex_accounts import (  # noqa: E402
    NOTE_ENDPOINT_ERROR,
    NOTE_NO_ACCESS,
    NOTE_NO_CREDENTIAL,
    NOTE_OFFLINE,
    NOTE_PENDING,
    NOTE_RATE_LIMITED,
    NOTE_RELOGIN,
    CodexAccountQuota,
    CodexAccountsSource,
    CredentialStore,
    HttpResponse,
    Registry,
    RegistryEntry,
    decode_jwt_claims,
    main as accounts_main,
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
    with temp_harness(accounts=[(PRO_ACCOUNT_ID, "a")]) as h:
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
    assert quota.capped_note == "capped workspace_owner_credits_depleted"
    row = quota.account_row(now=BASE_NOW, slot=-3, alias="work", is_active=False,
                            note=quota.capped_note, note_kind="crit")
    assert row.seven_day_pct == 100.0, "a capped plan keeps its evidence"
    assert row.attention_kind == "crit" and "{" not in row.attention_note
    # A dict that is not the {type: str} shape yields a bare "capped", never prose of a dict.
    odd = CAPTURED_business_body(); odd["rate_limit_reached_type"] = {"details": None}
    q2 = CodexAccountQuota.from_response(odd, credential_account_id="acct-biz",
                                         observed_at=BASE_NOW, include_extra=False)
    assert q2 is not None and q2.reached_type is None and q2.capped_note == "capped"


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
