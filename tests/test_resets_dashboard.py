"""Tests for the Fleet Reset Timeline & Capacity Forecast section in ``dashboard.py``.

Covers:
  - Mixed Claude (Fable 100% unlocking in 3h, Fable 4% perishable in 30h,
    ``claude_team`` non-Fable excluded from Fable capacity) + Codex (92% with
    1× usable reset credit, 10% with 3× banked reset credits, locked relogin
    seat with 2× banked credits).
  - KPI strip figures, Smart Fleet Advisor callout (never ``class="banner"``),
    pure inline SVG 7-day Gantt swimlane chart (``Now``..``+7d``, 24h band,
    emerald/amber/indigo markers, Codex ``↺ N×`` credit pills), and unified
    chronological schedule table.
  - XSS HTML escaping, raw email & token redaction, zero forbidden network/script
    substrings, light+dark CSS token completeness, byte stability, lazy
    ``cc_usage_widget.resets`` contract integration, and empty-fleet honesty.

Run directly (pytest is absent from the claude-swap venv)::

    CC_USAGE_WIDGET_NO_REVEAL=1 ~/.local/share/uv/tools/claude-swap/bin/python tests/test_resets_dashboard.py
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import os
import re
import sys
import tempfile
import traceback
import types
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

from cc_usage_widget import dashboard as dash  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    VENDOR_CLAUDE,
    VENDOR_CODEX,
    AccountRow,
    DayRollup,
    ModelUsage,
    local_day_key,
)
from cc_usage_widget.dashboard import (  # noqa: E402
    EMPTY_MARKER,
    RESET_RADAR_HEADING,
    build_dashboard,
    render_dashboard,
)
from cc_usage_widget.pricing import DEFAULT_PRICING  # noqa: E402
from cc_usage_widget.rollup import DailyRollupStore  # noqa: E402

NOW = 1_795_176_000.0  # Fixed UTC timestamp for byte-stable assertions
TODAY = local_day_key(NOW)
FORBIDDEN_SUBSTRINGS = ("<script", "http", "<link", "<img", "@import", "url(")


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _store(root: Path) -> DailyRollupStore:
    return DailyRollupStore(path=root / "rollups.json", keep_days=30)


def _mixed_fleet(now: float = NOW) -> tuple[tuple[AccountRow, ...], tuple[AccountRow, ...]]:
    """Mixed Claude + Codex fleet matching all reset radar states."""
    claude_accounts = (
        AccountRow(
            slot=1,
            alias="fable-cap",
            email="secret.one@belkins.io",
            is_active=True,
            five_hour_pct=50.0,
            seven_day_pct=80.0,
            seven_day_resets_at=_iso(now + 3 * 3600),
            scoped_windows=(("Fable", 100.0),),
            scoped_resets_at=(("Fable", _iso(now + 3 * 3600)),),
            plan_type="claude_max_20x",
            vendor=VENDOR_CLAUDE,
        ),
        AccountRow(
            slot=2,
            alias="fable-fresh",
            email="secret.two@belkins.io",
            is_active=False,
            five_hour_pct=4.0,
            seven_day_pct=4.0,
            seven_day_resets_at=_iso(now + 30 * 3600),
            scoped_windows=(("Fable", 4.0),),
            scoped_resets_at=(("Fable", _iso(now + 30 * 3600)),),
            plan_type="claude_max_20x",
            vendor=VENDOR_CLAUDE,
        ),
        AccountRow(
            slot=3,
            alias="team-seat",
            email="team.secret@belkins.io",
            is_active=False,
            five_hour_pct=10.0,
            seven_day_pct=20.0,
            seven_day_resets_at=_iso(now + 4 * 86_400),
            scoped_windows=(),
            scoped_resets_at=(),
            plan_type="claude_team",
            vendor=VENDOR_CLAUDE,
        ),
    )
    codex_rows = (
        AccountRow(
            slot=-1,
            alias="codex-hot",
            email="codex.hot@example.com",
            is_active=True,
            seven_day_pct=92.0,
            seven_day_resets_at=_iso(now + 18 * 3600),
            soonest_reset_at=now + 18 * 3600,
            plan_type="pro",
            vendor=VENDOR_CODEX,
            switchable=False,
            reset_credits_available=1,
            reset_credits_usable=1,
        ),
        AccountRow(
            slot=-2,
            alias="codex-cool",
            email="codex.cool@example.com",
            is_active=False,
            seven_day_pct=10.0,
            seven_day_resets_at=_iso(now + 5 * 86_400),
            soonest_reset_at=now + 5 * 86_400,
            plan_type="pro",
            vendor=VENDOR_CODEX,
            switchable=False,
            reset_credits_available=3,
            reset_credits_usable=0,
        ),
        AccountRow(
            slot=-3,
            alias="codex-locked",
            email="codex.locked@example.com",
            is_active=False,
            plan_type="pro",
            vendor=VENDOR_CODEX,
            switchable=False,
            attention_kind="warn",
            attention_note="relogin",
            reset_credits_available=2,
            reset_credits_usable=0,
        ),
    )
    return claude_accounts, codex_rows


def _extract_radar_section(page: str) -> str:
    marker = 'id="fleet-reset-radar"'
    assert marker in page, "Fleet Reset Radar section missing from dashboard HTML"
    return page.split(marker, 1)[1].split("</section>", 1)[0]


# ---------------------------------------------------------------------------
# 1. Mixed Claude + Codex fleet rendering (KPIs, SVG Gantt, Table, Privacy)
# ---------------------------------------------------------------------------


def test_mixed_claude_and_codex_fleet_kpis_gantt_and_table() -> None:
    """Full command-center section renders KPI strip, 7d SVG Gantt, and table."""
    claude_accts, codex_rows = _mixed_fleet(NOW)
    with tempfile.TemporaryDirectory() as tmp:
        page = render_dashboard(
            None,
            _store(Path(tmp)),
            codex_rows,
            claude_accts,
            now=NOW,
            pricing=DEFAULT_PRICING,
        )

    lowered = page.lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in lowered, f"forbidden substring {forbidden!r} leaked into page"

    # Never emit raw email addresses anywhere in the document.
    for raw_email in (
        "secret.one@belkins.io",
        "secret.two@belkins.io",
        "team.secret@belkins.io",
        "codex.hot@example.com",
        "codex.cool@example.com",
        "codex.locked@example.com",
        "@belkins.io",
        "@example.com",
    ):
        assert raw_email not in page, f"raw email {raw_email!r} leaked into dashboard"

    # Never collide with the indexing banner class.
    assert 'class="banner"' not in page, "advisor callout must not reuse .banner class"

    section = _extract_radar_section(page)
    assert html_lib.escape(RESET_RADAR_HEADING) in section

    # A. KPI Strip figures
    assert "0.96 / 2 seats" in section, "Claude Fable ready capacity must exclude claude_team"
    assert "fable-cap in " in section and "3h" in section
    assert "0.98 / 2 seats" in section, "Codex weekly ready capacity must exclude relogin seat"
    assert "4× live / 6× total" in section, section
    assert "1× usable now" in section
    assert "0.96 seat-eq at risk" in section
    assert "Locked relogin:" in section and "1 seats" in section

    # Smart Fleet Advisor callout
    assert 'class="rr-advisor rr-advisor-use_reset_credit"' in section

    # B. Pure SVG 7-Day Reset Gantt / Swimlane Chart
    svg_full = '<svg class="rr-gantt"' + section.split('<svg class="rr-gantt"', 1)[1].split("</svg>", 1)[0] + "</svg>"
    svg_root = ET.fromstring(svg_full)
    assert svg_root.tag == "svg"
    svg_part = svg_full
    for tick in ("Now", "+1d", "+2d", "+3d", "+4d", "+5d", "+6d", "+7d", "24h"):
        assert f">{tick}<" in svg_part, f"SVG axis missing {tick!r}"
    assert 'class="rr-band-24h"' in svg_part
    assert "✦" in svg_part and "◈" in svg_part
    assert 'class="rr-mk rr-mk-unlock"' in svg_part
    assert 'class="rr-mk rr-mk-perish"' in svg_part
    assert 'class="rr-mk rr-mk-upcoming"' in svg_part
    assert 'class="rr-bar rr-bar-unlock"' in svg_part
    assert 'class="rr-bar rr-bar-perish"' in svg_part
    assert 'class="rr-bar rr-bar-upcoming"' in svg_part
    assert 'class="rr-svg-pill-usable"' in svg_part and "↺ 1×" in svg_part
    assert 'class="rr-svg-pill-credit"' in svg_part and "↺ 3×" in svg_part

    # C. Unified Reset Schedule & Banked Reset Credits Table
    table_part = section.split('<div class="rr-table-wrap">', 1)[1]
    for header in (
        "Vendor",
        "Account",
        "Window",
        "Utilization",
        "Reset Time",
        "Countdown",
        "Status / Banked Credits",
    ):
        assert header in table_part, f"table missing header {header!r}"

    # Chronological order across mixed vendors: 3h < 18h < 30h < 4d < 5d
    positions = [
        table_part.index("fable-cap"),
        table_part.index("codex-hot"),
        table_part.index("fable-fresh"),
        table_part.index("team-seat"),
        table_part.index("codex-cool"),
    ]
    assert positions == sorted(positions), f"table rows not chronological: {positions}"

    assert "no Fable" in table_part
    assert "Unlock" in table_part
    assert "Perishable" in table_part
    assert "Upcoming" in table_part
    assert "↺ 1× (1× usable)" in table_part
    assert "↺ 3× banked" in table_part
    assert "codex-locked" in table_part and "↺ 2× banked" in table_part


# ---------------------------------------------------------------------------
# 2. XSS escaping, email stripping, and token redaction
# ---------------------------------------------------------------------------


def test_xss_escaping_and_email_token_privacy_sanitization() -> None:
    """Hostile aliases, unaliased emails, and bearer tokens are neutralized."""
    xss_claude = AccountRow(
        slot=1,
        alias='<script>alert("xss")</script>',
        email="private.owner@belkins.io",
        is_active=True,
        seven_day_pct=96.0,
        seven_day_resets_at=_iso(NOW + 2 * 3600),
        scoped_windows=(("Fable", 96.0),),
        scoped_resets_at=(("Fable", _iso(NOW + 2 * 3600)),),
        plan_type="claude_max_20x",
        vendor=VENDOR_CLAUDE,
    )
    img_codex = AccountRow(
        slot=-1,
        alias='<img src=x onerror=alert(1)>',
        email="codex.private@corp.test",
        is_active=True,
        seven_day_pct=90.0,
        seven_day_resets_at=_iso(NOW + 6 * 3600),
        soonest_reset_at=NOW + 6 * 3600,
        plan_type="pro",
        vendor=VENDOR_CODEX,
        switchable=False,
        reset_credits_available=2,
        reset_credits_usable=1,
    )
    email_alias_codex = AccountRow(
        slot=-2,
        alias="alice.secret@corp.example · 0a1b2c3d",
        email="alice.secret@corp.example",
        is_active=False,
        seven_day_pct=15.0,
        seven_day_resets_at=_iso(NOW + 24 * 3600),
        soonest_reset_at=NOW + 24 * 3600,
        plan_type="pro",
        vendor=VENDOR_CODEX,
        switchable=False,
    )

    with tempfile.TemporaryDirectory() as tmp:
        page = render_dashboard(
            None,
            _store(Path(tmp)),
            (img_codex, email_alias_codex),
            (xss_claude,),
            now=NOW,
        )

    lowered = page.lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in lowered, f"XSS/forbidden substring {forbidden!r} survived!"

    assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    # Hostile aliases inside SVG must still produce strictly well-formed XML.
    sec = _extract_radar_section(page)
    svg_full = '<svg class="rr-gantt"' + sec.split('<svg class="rr-gantt"', 1)[1].split("</svg>", 1)[0] + "</svg>"
    assert ET.fromstring(svg_full).tag == "svg"
    assert "private.owner@belkins.io" not in page
    assert "codex.private@corp.test" not in page
    assert "@corp.example" not in page
    assert "alice.secret · 0a1b2c3d" in page

    # Verify token redaction in advisor/sanitized text helper directly as well.
    assert "sk-ant-" not in dash._sanitize_text("key sk-ant-secret1234567890")
    assert "eyJhbGci" not in dash._sanitize_text("Bearer eyJhbGciOiJub25lIn0.payload.sig")


# ---------------------------------------------------------------------------
# 3. Graceful empty-fleet handling
# ---------------------------------------------------------------------------


def test_graceful_empty_fleet_and_no_accounts_handling() -> None:
    """Empty fleet omits the Reset Radar section cleanly with zero fabricated numbers."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        empty_page = render_dashboard(None, _store(root), (), (), now=NOW)
        empty_main = empty_page.split("<main>", 1)[1].split("</main>", 1)[0]
        assert EMPTY_MARKER in empty_main
        assert html_lib.escape(RESET_RADAR_HEADING) not in empty_main
        assert 'id="fleet-reset-radar"' not in empty_main
        assert "rr-gantt" not in empty_main
        assert "rr-kpi-grid" not in empty_main
        assert not re.search(r"\$\d", empty_page)

        # Store has usage, but no accounts reported -> Reset Radar still absent.
        store = _store(root)
        store.merge([DayRollup(day=TODAY, models={"claude-fable-5-2": ModelUsage(input=5_000)})])
        usage_only = render_dashboard(None, store, (), (), now=NOW, pricing=DEFAULT_PRICING)
        usage_main = usage_only.split("<main>", 1)[1].split("</main>", 1)[0]
        assert html_lib.escape(RESET_RADAR_HEADING) not in usage_main
        assert 'id="fleet-reset-radar"' not in usage_main

        # Explicit empty radar object -> section still absent.
        empty_radar = dash._FallbackFleetResetRadar(
            generated_at=NOW,
            events=(),
            unlocks=(),
            perishable=(),
            credits=dash._FallbackResetCreditsSummary(0, 0, 0, ()),
            capacity=dash._FallbackFleetCapacitySummary(0.0, 0, 0, None, 0.0, 0, 0, None, 0.0),
            recommendation=None,
        )
        explicit_empty = render_dashboard(
            None, store, (), (), now=NOW, pricing=DEFAULT_PRICING, reset_radar=empty_radar
        )
        assert 'id="fleet-reset-radar"' not in explicit_empty


# ---------------------------------------------------------------------------
# 4. Dark/light theme token parity and byte stability
# ---------------------------------------------------------------------------


def test_dark_and_light_theme_tokens_and_byte_stability() -> None:
    """Every --rr-* token is defined in both light :root and dark media query."""
    claude_accts, codex_rows = _mixed_fleet(NOW)
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(Path(tmp))
        first = render_dashboard(None, store, codex_rows, claude_accts, now=NOW)
        second = render_dashboard(None, store, codex_rows, claude_accts, now=NOW)

    assert first == second, "dashboard render must be byte-stable for identical inputs"
    head, _, tail = first.partition("@media (prefers-color-scheme: dark)")
    for token in (
        "--rr-unlock:",
        "--rr-perish:",
        "--rr-upcoming:",
        "--rr-credit:",
        "--rr-band24:",
        "--rr-card:",
    ):
        assert token in head, f"light :root missing {token}"
        assert token in tail, f"dark theme override missing {token}"


# ---------------------------------------------------------------------------
# 5. Shared resets.py contract integration, multi-event lane, and all advisor tiers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ContractResetEvent:
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
class _ContractCreditAcct:
    vendor: str
    slot: int
    alias: str
    available: int
    usable: int
    used_pct: float | None
    is_active: bool = False
    needs_relogin: bool = False


@dataclass(frozen=True, slots=True)
class _ContractCredits:
    total_available: int
    live_available: int
    total_usable: int
    accounts: tuple[_ContractCreditAcct, ...]


@dataclass(frozen=True, slots=True)
class _ContractCapacity:
    claude_fable_free_seats: float
    claude_fable_total_seats: int
    claude_locked_relogin_seats: int
    claude_next_unlock: _ContractResetEvent | None
    codex_free_seats: float
    codex_total_seats: int
    codex_locked_relogin_seats: int
    codex_next_unlock: _ContractResetEvent | None
    perishable_seats_48h: float


@dataclass(frozen=True, slots=True)
class _ContractRecommendation:
    kind: str
    headline: str
    detail: str
    target_vendor: str | None = None
    target_slot: int | None = None


@dataclass(frozen=True, slots=True)
class _ContractRadar:
    generated_at: float
    events: tuple[_ContractResetEvent, ...]
    unlocks: tuple[_ContractResetEvent, ...]
    perishable: tuple[_ContractResetEvent, ...]
    credits: _ContractCredits
    capacity: _ContractCapacity
    recommendation: _ContractRecommendation | None


def test_resets_module_contract_integration_and_all_advisor_tiers() -> None:
    """Verifies exact shared contract attributes, lazy module hook, multi-event lanes, and all 5 advisor kinds."""
    ev_5h = _ContractResetEvent(
        vendor="claude",
        slot=1,
        alias="dual-window",
        window="5h",
        used_pct=95.0,
        free_pct=5.0,
        resets_at_epoch=NOW + 2 * 3600,
        seconds_until=2 * 3600,
        reset_label="14:00",
        countdown_label="2h",
        kind="unlock",
        is_active=True,
        plan_label="Max 20x",
    )
    ev_7d = _ContractResetEvent(
        vendor="claude",
        slot=1,
        alias="dual-window",
        window="Fable",
        used_pct=15.0,
        free_pct=85.0,
        resets_at_epoch=NOW + 9 * 86_400,  # >7d horizon clamp test
        seconds_until=9 * 86_400,
        reset_label="Nov 30 12:00",
        countdown_label="9d",
        kind="upcoming",
        is_active=True,
        plan_label="Max 20x",
    )
    ev_perish = _ContractResetEvent(
        vendor="codex",
        slot=-1,
        alias="codex-expiring",
        window="7d",
        used_pct=20.0,
        free_pct=80.0,
        resets_at_epoch=NOW + 20 * 3600,
        seconds_until=20 * 3600,
        reset_label="Tom 08:00",
        countdown_label="20h",
        kind="perishable",
        plan_label="Pro",
    )

    base_cap = _ContractCapacity(
        claude_fable_free_seats=0.85,
        claude_fable_total_seats=1,
        claude_locked_relogin_seats=1,
        claude_next_unlock=ev_5h,
        codex_free_seats=0.80,
        codex_total_seats=1,
        codex_locked_relogin_seats=1,
        codex_next_unlock=None,
        perishable_seats_48h=0.80,
    )
    base_credits = _ContractCredits(
        total_available=2,
        live_available=2,
        total_usable=0,
        accounts=(
            _ContractCreditAcct(
                vendor="codex",
                slot=-1,
                alias="codex-expiring",
                available=2,
                usable=0,
                used_pct=20.0,
            ),
        ),
    )

    for rec_kind in (
        "use_reset_credit",
        "burn_perishable",
        "unlock_soon",
        "relogin_capacity",
        "healthy",
    ):
        radar = _ContractRadar(
            generated_at=NOW,
            events=(ev_5h, ev_perish, ev_7d),
            unlocks=(ev_5h,),
            perishable=(ev_perish,),
            credits=base_credits,
            capacity=base_cap,
            recommendation=_ContractRecommendation(
                kind=rec_kind,
                headline=f"Advisor headline for {rec_kind} (owner@leak.test)",
                detail=f"Detail for {rec_kind} with <b>escaped</b> text",
                target_vendor="codex",
                target_slot=-1,
            ),
        )
        page = render_dashboard(None, None, (), (), now=NOW, reset_radar=radar)
        sec = _extract_radar_section(page)
        assert f'class="rr-advisor rr-advisor-{rec_kind}"' in sec
        assert f"Advisor headline for {rec_kind} (owner)" in sec
        assert "owner@leak.test" not in page
        assert "&lt;b&gt;escaped&lt;/b&gt;" in sec and "<b>escaped</b>" not in sec
        # Dual-window account renders ONE swimlane (<g class="rr-lane">) with TWO markers.
        assert sec.count('<g class="rr-lane">') == 2, "expected 2 account swimlanes (dual-window + codex-expiring)"
        assert "&gt;7d" in sec or "· &gt;7d" in sec

    # Verify lazy import calls cc_usage_widget.resets.build_fleet_reset_radar when present.
    called: list[tuple[Any, Any, Any, Any, Any]] = []
    stub_radar = _ContractRadar(
        generated_at=NOW,
        events=(ev_5h,),
        unlocks=(ev_5h,),
        perishable=(),
        credits=_ContractCredits(0, 0, 0, ()),
        capacity=base_cap,
        recommendation=_ContractRecommendation("unlock_soon", "Stub radar fired", "Verified lazy hook."),
    )
    fake_mod = types.ModuleType("cc_usage_widget.resets")

    def _fake_builder(accounts, quota_rows, *, account_notes=None, now=None, fable_guard=True):  # noqa: ANN001, ANN202
        called.append((accounts, quota_rows, account_notes, now, fable_guard))
        return stub_radar

    fake_mod.build_fleet_reset_radar = _fake_builder  # type: ignore[attr-defined]
    prev_mod = sys.modules.get("cc_usage_widget.resets")
    prev_attr = getattr(sys.modules["cc_usage_widget"], "resets", None)
    try:
        sys.modules["cc_usage_widget.resets"] = fake_mod
        setattr(sys.modules["cc_usage_widget"], "resets", fake_mod)
        claude_accts, codex_rows = _mixed_fleet(NOW)
        page = render_dashboard(
            None,
            None,
            codex_rows,
            claude_accts,
            now=NOW,
            account_notes={9: "relogin"},
            fable_guard=False,
        )
        assert len(called) == 1, "dashboard must delegate to cc_usage_widget.resets.build_fleet_reset_radar"
        assert called[0][2] == {9: "relogin"}
        assert called[0][3] == NOW
        assert called[0][4] is False
        assert "Stub radar fired" in page
    finally:
        if prev_mod is None:
            sys.modules.pop("cc_usage_widget.resets", None)
        else:
            sys.modules["cc_usage_widget.resets"] = prev_mod
        if prev_attr is None:
            if hasattr(sys.modules["cc_usage_widget"], "resets"):
                delattr(sys.modules["cc_usage_widget"], "resets")
        else:
            setattr(sys.modules["cc_usage_widget"], "resets", prev_attr)


# ---------------------------------------------------------------------------
# 6. build_dashboard backwards compatibility, 0600 mode, and account_notes
# ---------------------------------------------------------------------------


def test_build_dashboard_backwards_compatibility_and_0600_write() -> None:
    """build_dashboard keeps exact existing signature, writes 0600, and forwards account_notes."""
    claude_accts, codex_rows = _mixed_fleet(NOW)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = _store(root)
        # Backwards-compatible call without any new kwargs
        written = build_dashboard(
            None,
            store,
            codex_rows,
            claude_accts,
            now=NOW,
            pricing=DEFAULT_PRICING,
            path=root / "dashboard.html",
        )
        assert written.exists()
        assert oct(written.stat().st_mode & 0o777) == "0o600"
        body = written.read_text(encoding="utf-8")
        assert html_lib.escape(RESET_RADAR_HEADING) in body
        for bad in FORBIDDEN_SUBSTRINGS:
            assert bad not in body.lower()

        # Optional account_notes marks Claude slot 1 locked -> Locked relogin rises to 2 seats!
        written2 = build_dashboard(
            None,
            store,
            codex_rows,
            claude_accts,
            now=NOW,
            pricing=DEFAULT_PRICING,
            path=root / "dashboard_locked.html",
            account_notes={1: "relogin required"},
        )
        body2 = written2.read_text(encoding="utf-8")
        sec2 = _extract_radar_section(body2)
        assert "2 seats" in sec2, "locking Claude slot 1 + Codex slot -3 must report 2 locked seats"


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
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
