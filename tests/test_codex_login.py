"""Codex relogin / add-account from the menu (CX-5) and the relogin row's
detail line (CX-7b).

Everything runs over **real files in a ``TemporaryDirectory``** with the
credential fixtures of ``tests/test_codex_accounts.py`` (imported, not copied).
Nothing here reads or writes ``~/.codex`` or the live widget home, makes a
network request, or opens Terminal: ``CC_USAGE_WIDGET_NO_REVEAL`` is set before
the app module is imported, and the one test that drives the real hand-off
asserts it opened nothing.

Run directly (pytest is absent from the claude-swap venv)::

    ~/.local/share/uv/tools/claude-swap/bin/python tests/test_codex_login.py
"""

from __future__ import annotations

import os

os.environ.setdefault("CC_USAGE_WIDGET_NO_REVEAL", "1")

import datetime as _dt  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import stat  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import traceback  # noqa: E402
from dataclasses import dataclass, replace  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import test_codex_accounts as tca  # noqa: E402

from cc_usage_widget import app as app_mod  # noqa: E402
from cc_usage_widget import codex_login  # noqa: E402
from cc_usage_widget.codex_accounts import (  # noqa: E402
    NOTE_NO_ACCESS,
    NOTE_NO_CREDENTIAL,
    NOTE_RELOGIN,
    CredentialStore,
    Registry,
    RegistryEntry,
    decode_jwt_claims,
)
from cc_usage_widget.codex_accounts import main as accounts_main  # noqa: E402
from cc_usage_widget.contracts import (  # noqa: E402
    SETTINGS_DEFAULTS,
    VENDOR_CODEX,
    AccountRow,
    normalize_settings,
)

BASE_NOW = tca.BASE_NOW
APP_BUNDLE_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _layout(root: Path) -> tuple[Path, Path]:
    accounts_dir = root / "codex-accounts"
    registry_path = root / "codex_accounts.json"
    accounts_dir.mkdir(parents=True)
    return accounts_dir, registry_path


def _stage(accounts_dir: Path, name: str, account_id: str, *, exp: float) -> Path:
    """A finished ``CODEX_HOME=<accounts_dir>/<name> codex login``."""
    tca.write_credential(accounts_dir, name, exp=exp)
    # write_credential names the dir and the id alike; a staged login's dir is
    # `new-*` while its token claims the real account.
    auth = accounts_dir / name / "auth.json"
    raw = json.loads(auth.read_text())
    raw["tokens"]["access_token"] = tca.access_token(
        account_id=account_id, email="who@example.test", plan="pro", exp=exp
    )
    raw["tokens"]["account_id"] = account_id
    raw["tokens"]["refresh_token"] = f"refresh-for-{name}"
    auth.write_text(json.dumps(raw))
    os.chmod(auth, 0o644)  # codex's own mode; adoption must tighten it
    return auth


def _exp_of(auth: Path) -> float:
    raw = json.loads(auth.read_text())
    return float(decode_jwt_claims(raw["tokens"]["access_token"])["exp"])


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@dataclass(frozen=True)
class XRow(AccountRow):
    """An AccountRow carrying lane codex-credentials' field on this branch too."""

    credential_expires_at: float | None = None


def live_row(slot: int, alias: str, *, note: str = "", kind: str = "", **extra: Any) -> XRow:
    return XRow(
        slot=slot, alias=alias, email="", is_active=False, vendor=VENDOR_CODEX,
        switchable=False, attention_note=note, attention_kind=kind, **extra,
    )


# ---------------------------------------------------------------------------
# 1. adopt --replace
# ---------------------------------------------------------------------------


def test_adopt_replace_swaps_a_fresh_login_into_the_existing_home() -> None:
    with tempfile.TemporaryDirectory() as name:
        accounts_dir, registry_path = _layout(Path(name))
        old = tca.write_credential(accounts_dir, "acct-a", exp=BASE_NOW - 60)
        Registry(registry_path).upsert(RegistryEntry("acct-a", alias="vlad", enabled=True, order=0))
        _stage(accounts_dir, "new-1", "acct-a", exp=BASE_NOW + 10 * 86_400)

        # Today's path refuses: the id is already tracked, so a relogin through
        # new-* is impossible without this module.
        refused = tca._Out()
        assert accounts_main(
            ["adopt"], accounts_dir=accounts_dir, registry_path=registry_path, out=refused
        ) == 1, refused.text
        plain = io.StringIO()
        assert codex_login.main(
            ["adopt"], accounts_dir=accounts_dir, registry_path=registry_path, out=plain
        ) == 1, "without --replace a tracked id is still refused"
        assert (accounts_dir / "new-1").is_dir() and _exp_of(old) == BASE_NOW - 60

        out = io.StringIO()
        code = codex_login.main(
            ["adopt", "--replace"], accounts_dir=accounts_dir, registry_path=registry_path, out=out
        )
        assert code == 0, out.getvalue()
        assert _exp_of(old) == BASE_NOW + 10 * 86_400, "the fresh token is in the old home"
        assert _mode(old) == 0o600 and _mode(accounts_dir / "acct-a") == 0o700
        assert not (accounts_dir / "new-1").exists()
        entries = Registry(registry_path).entries()
        assert [(e.account_id, e.alias) for e in entries] == [("acct-a", "vlad")], entries
        text = out.getvalue()
        assert "replaced acct-a" in text, text
        assert "refresh-for" not in text and "eyJ" not in text and "access_token" not in text
        # The widget's own reader accepts the swapped file (dir name == claimed id).
        assert "acct-a" in CredentialStore(accounts_dir, auth_path=Path(name) / "none").discover()


def test_adopt_registers_a_new_account_the_way_codex_accounts_adopt_does() -> None:
    with tempfile.TemporaryDirectory() as name:
        accounts_dir, registry_path = _layout(Path(name))
        Registry(registry_path).upsert(RegistryEntry("acct-a", alias="vlad", enabled=True, order=0))
        tca.write_credential(accounts_dir, "acct-a", exp=BASE_NOW + 86_400)
        _stage(accounts_dir, "new-2", "acct-b", exp=BASE_NOW + 10 * 86_400)
        result = codex_login.adopt_pending(accounts_dir, Registry(registry_path), replace=True)
        assert result.added == ("acct-b",) and result.replaced == () and result.ok, result
        assert _mode(accounts_dir / "acct-b" / "auth.json") == 0o600
        assert [e.account_id for e in Registry(registry_path).entries()] == ["acct-a", "acct-b"]
        assert all("who@example" not in line for line in result.log_lines()), "no email in the log"


def test_the_worker_path_leaves_a_login_that_may_still_be_writing() -> None:
    with tempfile.TemporaryDirectory() as name:
        accounts_dir, registry_path = _layout(Path(name))
        auth = _stage(accounts_dir, "new-3", "acct-c", exp=BASE_NOW + 86_400)
        mtime = auth.stat().st_mtime
        result = codex_login.adopt_pending(
            accounts_dir, Registry(registry_path), replace=True,
            min_age_seconds=codex_login.ADOPT_SETTLE_SECONDS, clock=lambda: mtime + 5,
        )
        assert result.waiting == ("new-3",) and auth.is_file(), result
        result = codex_login.adopt_pending(
            accounts_dir, Registry(registry_path), replace=True,
            min_age_seconds=codex_login.ADOPT_SETTLE_SECONDS, clock=lambda: mtime + 60,
        )
        assert result.added == ("acct-c",), result


# ---------------------------------------------------------------------------
# 2. Finding codex
# ---------------------------------------------------------------------------


def test_resolve_codex_binary_falls_back_to_the_app_bundle() -> None:
    only_bundle = lambda path: path == APP_BUNDLE_CODEX  # noqa: E731
    assert codex_login.resolve_codex_binary(which=lambda _n: None, exists=only_bundle) == APP_BUNDLE_CODEX
    assert codex_login.resolve_codex_binary(
        which=lambda _n: "/somewhere/on/path/codex", exists=only_bundle
    ) == "/somewhere/on/path/codex", "a PATH hit wins"
    assert codex_login.resolve_codex_binary(
        which=lambda _n: None, exists=lambda p: p == "/opt/homebrew/bin/codex"
    ) == "/opt/homebrew/bin/codex"
    assert codex_login.resolve_codex_binary(which=lambda _n: None, exists=lambda _p: False) is None


# ---------------------------------------------------------------------------
# 3. The menu: Log in again… / Add Codex account… -> ('terminal', .command)
# ---------------------------------------------------------------------------


def _fake_source(accounts_dir: Path, registry_path: Path) -> Any:
    """The two attributes the worker reads from a live Codex source."""
    return SimpleNamespace(
        vendor=VENDOR_CODEX,
        registry=Registry(registry_path),
        credentials=CredentialStore(accounts_dir, auth_path=accounts_dir.parent / "no-codex-auth.json"),
    )


def _worker(sources: tuple[Any, ...]) -> Any:
    return app_mod.BackgroundWorker(
        publish=lambda _snapshot: None,
        snapshot=app_mod.UiSnapshot(settings=normalize_settings(dict(SETTINGS_DEFAULTS))),
        accounts=None,
        indexer=None,
        sources=sources,
    )


def test_a_dead_codex_row_offers_log_in_again_and_parks_a_terminal_hand_off() -> None:
    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    rows = (
        live_row(-1, "vlad", seven_day_pct=40.0, usage_age_seconds=60.0),
        live_row(-2, "work", note=NOTE_RELOGIN, kind="warn",
                 credential_expires_at=_dt.datetime(2026, 9, 20, 11, 13).timestamp()),
        live_row(-3, "gmail", note=NOTE_NO_CREDENTIAL, kind="warn"),
        live_row(-4, "denied", note=NOTE_NO_ACCESS, kind="warn"),
    )
    app = app_mod.CCUsageWidgetApp()
    try:
        # Registry order, ENABLED entries only -> slots -1, -2, -3, -4.
        app._worker._codex_accounts = (
            ("acct-v", "vlad", True), ("acct-off", "", False), ("acct-w", "work", True),
            ("acct-g", "gmail", True), ("acct-d", "denied", True),
        )
        items = app._quota_items(app_mod.UiSnapshot(settings=settings, quota_rows=rows))
        by_alias = {row.alias: item for row, item in zip(rows, items[-len(rows):])}
        assert by_alias["vlad"].callback is None, "a healthy row stays unclickable"
        assert by_alias["denied"].callback is None, "logging in does not grant a permission"
        for alias in ("work", "gmail"):
            assert by_alias[alias].callback is not None, alias
            assert codex_login.LOGIN_AGAIN_LABEL in str(by_alias[alias].title), by_alias[alias].title
        submitted: list[tuple[str, Any]] = []
        app._worker.submit = lambda name, payload=None: submitted.append((name, payload))  # type: ignore[assignment]
        by_alias["work"].callback(by_alias["work"])
        by_alias["gmail"].callback(by_alias["gmail"])
        assert submitted == [(app_mod._CMD_CODEX_LOGIN, "acct-w"), (app_mod._CMD_CODEX_LOGIN, "acct-g")]

        # Under CC_USAGE_WIDGET_NO_REVEAL the real hand-off opens nothing.
        assert os.environ.get("CC_USAGE_WIDGET_NO_REVEAL")
        assert app._desktop_handoff(app_mod._DESKTOP_TERMINAL, Path("/nonexistent/x.command")) is False
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)

    # The worker half: the command writes the script and parks ('terminal', path).
    original = codex_login.resolve_codex_binary
    codex_login.resolve_codex_binary = lambda *a, **k: APP_BUNDLE_CODEX  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as name:
            accounts_dir, registry_path = _layout(Path(name))
            token_file = tca.write_credential(accounts_dir, "acct-w", exp=BASE_NOW - 60)
            worker = _worker((_fake_source(accounts_dir, registry_path),))
            worker._handle_command((app_mod._CMD_CODEX_LOGIN, "acct-w"))
            parked = worker.take_desktop_requests()
            assert len(parked) == 1, parked
            action, path = parked[0]
            assert action == app_mod._DESKTOP_TERMINAL == "terminal"
            path = Path(path)
            assert path.parent == accounts_dir and path.suffix == ".command"
            assert _mode(path) == 0o700
            body = path.read_text()
            assert f"CODEX_HOME={accounts_dir}/new-" in body, body
            assert f"{APP_BUNDLE_CODEX} login" in body
            assert f"{sys.executable} -m cc_usage_widget.codex_login adopt --replace" in body
            raw = json.loads(token_file.read_text())["tokens"]
            for secret in (raw["access_token"], raw["refresh_token"], raw["id_token"]):
                assert secret not in body
            assert "eyJ" not in body and "who@example" not in body
            assert worker.take_desktop_requests() == (), "parked once"
    finally:
        codex_login.resolve_codex_binary = original  # type: ignore[assignment]


def test_no_codex_binary_writes_nothing_and_parks_nothing() -> None:
    original = codex_login.resolve_codex_binary
    codex_login.resolve_codex_binary = lambda *a, **k: None  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as name:
            accounts_dir, registry_path = _layout(Path(name))
            worker = _worker((_fake_source(accounts_dir, registry_path),))
            worker._handle_command((app_mod._CMD_CODEX_LOGIN, None))
            assert worker.take_desktop_requests() == ()
            assert list(accounts_dir.iterdir()) == []
    finally:
        codex_login.resolve_codex_binary = original  # type: ignore[assignment]


def test_the_accounts_submenu_offers_add_codex_account() -> None:
    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    app = app_mod.CCUsageWidgetApp()
    try:
        app._worker._codex_accounts = (("acct-v", "vlad", True),)
        menu = app._codex_accounts_submenu(app_mod.UiSnapshot(settings=settings))
        titles = [str(item.title) for item in menu.values() if item is not None and hasattr(item, "title")]
        assert codex_login.ADD_ACCOUNT_LABEL in titles, titles
        item = next(i for i in menu.values() if str(getattr(i, "title", "")) == codex_login.ADD_ACCOUNT_LABEL)
        submitted: list[tuple[str, Any]] = []
        app._worker.submit = lambda name, payload=None: submitted.append((name, payload))  # type: ignore[assignment]
        item.callback(item)
        assert submitted == [(app_mod._CMD_CODEX_LOGIN, None)]
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)


def test_the_worker_tick_adopts_a_login_whose_terminal_was_closed() -> None:
    with tempfile.TemporaryDirectory() as name:
        accounts_dir, registry_path = _layout(Path(name))
        old = tca.write_credential(accounts_dir, "acct-a", exp=BASE_NOW - 60)
        Registry(registry_path).upsert(RegistryEntry("acct-a", alias="vlad", enabled=True, order=0))
        staged = _stage(accounts_dir, "new-9", "acct-a", exp=BASE_NOW + 10 * 86_400)
        settled = staged.stat().st_mtime - 2 * codex_login.ADOPT_SETTLE_SECONDS
        os.utime(staged, (settled, settled))
        worker = _worker((_fake_source(accounts_dir, registry_path),))
        worker._collect_quota_rows()
        assert _exp_of(old) == BASE_NOW + 10 * 86_400, "adopted on the tick"
        assert not (accounts_dir / "new-9").exists()
        assert worker.codex_accounts == (("acct-a", "vlad", True),)


def test_an_unadoptable_login_is_logged_once_not_every_tick() -> None:
    with tempfile.TemporaryDirectory() as name:
        accounts_dir, registry_path = _layout(Path(name))
        Registry(registry_path).upsert(RegistryEntry("acct-a", alias="vlad", enabled=True, order=0))
        broken = accounts_dir / "new-7"
        broken.mkdir()
        (broken / "auth.json").write_text("{ not json")
        settled = (broken / "auth.json").stat().st_mtime - 2 * codex_login.ADOPT_SETTLE_SECONDS
        os.utime(broken / "auth.json", (settled, settled))
        worker = _worker((_fake_source(accounts_dir, registry_path),))
        logged: list[str] = []
        original = app_mod._log
        app_mod._log = logged.append  # type: ignore[assignment]
        try:
            for _ in range(3):
                worker._collect_quota_rows()
        finally:
            app_mod._log = original  # type: ignore[assignment]
        refusals = [line for line in logged if line.startswith("codex login: new-7")]
        assert refusals == ["codex login: new-7: no readable account id in auth.json"], logged
        assert broken.is_dir(), "a refused login is left for the user, never deleted"


# ---------------------------------------------------------------------------
# 4. CX-7b: the relogin row says when and what to do
# ---------------------------------------------------------------------------


def test_a_relogin_row_says_when_its_token_expired_and_what_to_do() -> None:
    expired = _dt.datetime(2026, 9, 20, 11, 13).timestamp()
    row = live_row(-2, "work", note=NOTE_RELOGIN, kind="warn", credential_expires_at=expired,
                   usage_age_seconds=5 * 86_400.0)
    expected = "token expired Sep 20 11:13 · Log in again…"
    assert codex_login.relogin_detail(row) == expected
    assert row.attention_note == NOTE_RELOGIN, "the verdict itself is untouched"
    plain = app_mod._quota_row_label(row)
    assert expected in plain and "(relogin)" in plain, plain

    settings = normalize_settings(dict(SETTINGS_DEFAULTS))
    app = app_mod.CCUsageWidgetApp()
    try:
        drawn: list[list[tuple[str, Any]]] = []
        original = app_mod.render.apply_attributed
        app_mod.render.apply_attributed = lambda item, segments: drawn.append(list(segments))
        try:
            item = app_mod.rumps.MenuItem("x")
            app._decorate_quota_item(item, row)
        finally:
            app_mod.render.apply_attributed = original
        dim = [text.strip() for text, style in drawn[0] if style == "dim"]
        assert dim.count(expected) == 1, drawn[0]
    finally:
        app._running = False
        app._worker.stop(timeout=2.0)

    # No expiry on the row (the other lane's field absent): the reading's age.
    aged = replace(row, credential_expires_at=None)
    assert codex_login.relogin_detail(aged) == "last reading 5d ago · Log in again…"
    # A future expiry means the endpoint refused it (401), not that it ran out.
    refused = replace(row, credential_expires_at=expired + 400 * 86_400)
    assert codex_login.relogin_detail(refused, now=expired) == "token refused · Log in again…"


def test_every_other_row_renders_exactly_as_before() -> None:
    healthy = live_row(-1, "vlad", seven_day_pct=40.0, usage_age_seconds=60.0,
                       credential_expires_at=BASE_NOW + 86_400)
    countdown = live_row(-1, "vlad", note="relogin in 1d 4h", kind="info", seven_day_pct=40.0)
    plain_row = AccountRow(slot=-1, alias="vlad", email="", is_active=False,
                           seven_day_pct=40.0, vendor=VENDOR_CODEX, switchable=False)
    for row in (healthy, countdown, plain_row):
        assert codex_login.relogin_detail(row) == ""
        assert "Log in again" not in app_mod._quota_row_label(row)
    # The negative control: the same healthy row with the verdict does change.
    assert "Log in again" in app_mod._quota_row_label(replace(healthy, attention_note=NOTE_RELOGIN))


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
