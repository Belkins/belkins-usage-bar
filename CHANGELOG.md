# Changelog

## Unreleased — live quota for every Codex account (SPEC-CODEX 6)

The Codex row was honest but anonymous: rollout transcripts carry no account id,
so its percentage could only describe whichever login happened to write them.
With four Codex logins — two of them the same email in two ChatGPT workspaces —
that row answered the wrong question and said nothing at all about the other
three. The widget now reads each account's quota from OpenAI directly, one row
per login, with the one the CLI is using marked `· active`.

**Off by default** (`codex_live_quota_enabled: false`). With it off, or with no
credential adopted, the menu is byte-for-byte what it was — that is the rollback
switch, and a Claude-only machine sees nothing new either way.

- **One row per account**, in your own order, from
  `GET chatgpt.com/backend-api/wham/usage` — the endpoint that reports the plan's
  limits without invoking a model, so a poll costs no quota. Each account is read
  every 5 min (60–3600 s), sequentially, ≥20 s apart, with ±15 % jitter derived
  from a hash of the account id rather than `random`.
- **Windows are identified by their width, never their position.** `604800` is
  the week, `18000` the 5-hour bucket, anything else renders under a label
  derived from the width. A named `additional_rate_limits` pool always renders as
  itself and never occupies the plan's own bar.
- **A sentinel replaces a number, never decorates one**: `relogin`, `no access`,
  `rate limited` (honouring `Retry-After` in both legal forms, clamped),
  `endpoint error`, `offline`, `awaiting first reading`, `credential unreadable`
  — coloured from a stable `attention_kind`, never from the wording. `offline`
  and `endpoint error` appear only after three consecutive failures, because a
  note that fires on the first blip is a note the eye learns to ignore.
- **Ageing has two steps**: past 15 min a reading shows its age; past 6 h its
  bars are **withheld** and the row keeps only its header and reason. A
  six-hour-old percentage of a five-hour window is not stale, it is wrong.
- **Never refreshes a token.** `exp` is decoded locally (no verification, display
  only): 48 h out the row says `relogin in 1d 4h`; once it passes, `relogin` and
  **no request is made**. Refresh stays gated behind a probe that has not run —
  whether two `codex login`s share a refresh-token family is unverified, and a
  wrong guess logs you out of the account you are coding in.
- **`~/.codex` is read-only to us**: exactly one field, `tokens.account_id`, to
  mark the active row. A test asserts the file is byte- and mtime-identical
  after a full cycle. A credential file with any group or world bit is refused
  rather than used.
- **Scheduling is on wall time.** `time.monotonic()` on this Mac pauses while the
  lid is shut; the two clocks are compared every cycle, and a divergence larger
  than two intervals is treated as a wake that makes every account due — which is
  what rescues an account sitting in a 30-minute backoff computed before the lid
  closed.
- **Onboarding CLI** in its own process (never contends with the widget's
  flock): `python -m cc_usage_widget.codex_accounts adopt | list | probe`.
  `adopt` is offline, names each directory after the account its own token
  claims, and refuses a duplicate id with both paths.
- **Privacy**: the access token is read, used in one header and dropped — never
  logged, never persisted, never in a label, and not even in a `repr`.
  `tests/test_privacy.py` plants a canary as both `access_token` and
  `refresh_token`, runs a full cycle, and asserts it reaches no file, no log line
  and no rendered row — with a negative control that proves the check fails when
  a transport deliberately logs the header.

+37 tests in `tests/test_codex_accounts.py`, +3 in `tests/test_privacy.py`.

- **Review fixes before merge** (1 blocker, 3 major, 8 minor found by two
  adversarial reviewers): live rows no longer read `resets resets 15:46` (the
  reset field carries the bare clock; the renderer adds the word); a reading
  past 6 h stays in the menu as header + age note instead of vanishing; a
  `capped` plan keeps `C100%` in the title (the alarm glyph is for a warn/crit
  row with no figure); the poller thread is created only once the source is
  available, never at boot on a Claude-only machine; a warn sentinel withholds
  the bars while it stands; a fresh `codex login` clears a standing sentinel
  within one cycle; the sidecar forgets removed accounts; a 429 streak no
  longer turns the first 502 into `endpoint error`; an unusable 200 takes the
  endpoint ladder; two unnamed windows of one width get distinct labels; the
  connect budget actually reaches the transport; the window-label clamp no
  longer touches Claude rows. 217 tests (was 141), every new one proven to
  fail against the pre-fix tree.
- **Codex is priced through Astra now.** `gpt-6-astra` (OpenAI's flagship since
  2026-09-03, and the dominant model in this corpus by 2026-09-09 — roughly
  three Astra turns per Sol turn) was costing `$0` on the unknown-model path. Added at its official standard rate ($10 / $1 cached /
  $50 per Mtok). `gpt-5.6-sol` is split into its two real rates ($5/$0.50/$30
  verified 2026-08-17; $4/$0.40/$20 on OpenAI's page 2026-09-09), bounded at
  Astra's launch day with the uncertainty recorded in
  `pricing.OPENAI_SOL_RATE_CUT_DAY`.
- **Five cost tests had never run.** `tests/test_cost_math.py` kept its
  `__main__` block mid-file, so every test defined below it (the unpriced-floor
  pair and the gpt-5.5 pin) was skipped by the script runner: 14 ran, 19 exist.
  The block is now last; all 19 run.

## 2026-09-01 — switching & scaling pass (4 accounts at the 5h wall, 8 switches in 80 min)

Provoked by one evening: all four enabled accounts hit 5h = 100 % with staggered
resets, the engine switched eight times in eighty minutes, and the active login
flipped back 5→1 three times with no line anywhere — a running Claude Code
session refreshing account 1's token back into the shared default login while a
bare `cswap` TUI was also open. The widget could neither see nor say any of it.

- **External switches are detected and named.** The adapter compares the active
  slot tick to tick; a change it did not make (no engine event, no menu click)
  logs a WARNING, lands in the journal as `HH:MM active A→B (external)` and
  raises a standing `⚠ ext` verdict that only a widget-made switch retracts.
  A `no-viable-target` / `no-candidates` tick is now a standing `⚠ no target`
  instead of a DEBUG line that also cleared the alert.
- **Recent switches** block in the menu (last 5 journal lines, newest first,
  local `HH:MM`), a `last switch HH:MM (trigger) · cooldown Nm left` line read
  from claude-swap's own `autoswitch_state.json`, and the all-exhausted line's
  ISO-UTC reset re-expressed in local time (it was seven hours off the clock).
- **Rival actors** are re-scanned every 5 min (one `pgrep`), and the pattern now
  catches a bare `cswap` / `cswap tui|watch` (the TUI switches on a keypress and
  can host its own engine), not only `cswap auto|menubar`.
- **Sessions section**: every live Claude Code instance with its name, directory,
  status and the account it is spending — default-login sessions follow every
  switch, `cswap run` profile sessions are pinned. Each row can **pin its
  directory to an account** (`mappings.json`, written by claude-swap's own
  `MappingStore`, by identity not slot) for the next `cswap run --share-history`
  there; the menu says a pin never moves a running session, and warns that
  pinning to the current default login launches an unpinned session.
- **Title at the wall**: `main 100% ⛔ exhausted 0/4 · next 00:00` — rooms with
  headroom under the engine's own threshold, and the soonest 5h reset among the
  full ones (verbatim reset strings; a next-day reset is not a "next room").
  Budgeted so a full menu bar sheds the time first, then the count.
- **Switch UX**: one-click `Switch to best now` (claude-swap's `strategy="best"`
  pick, folding in the policy's per-model windows; upstream `warnings` reach the
  journal), and a `Switch account ▸` submenu sorted by headroom that shows
  `5h 100% ↺00:00 · 7d 20% · Fable 40% (at limit)` instead of the 5h pct alone.
- **Engine cadence**: the worker honours the engine's own due time instead of
  rounding it up to the 60 s UI tick; a between-tick switch publishes rows,
  verdict and journal together.

141 tests (was 80).

## 2026-08-25 — hardening pass (post 4-dimension audit)

Audit verdict: the switching engine is sound — every observed "bug" was a
display artifact or a designed behavior. Widget-side fixes:

- **Display honesty:** "100%" now reserved for windows truly >= 100; 99.x
  floors to 99% (the menu had contradicted a legitimate at-limit switch).
- **Cost floors:** window totals carry a `+` marker and an
  "unpriced at $0 (N tok/30d)" line when unpriced tokens are excluded;
  `WindowCost.unpriced_tokens` is the new plumbing. Added the missing
  gpt-5.5 price row ($5/$0.50/$30 per Mtok, three agreeing sources).
- **Manual switches** log INFO + WARN when the target carries a sentinel
  (the 15:06 switch onto a token-dead account left zero log lines).
- **Supervisor:** 6th worker death exits(1) for a clean launchd relaunch
  (was: silently paint stale data forever); restart budget heals after 1h.
- **Ops:** dated log stamps, ~5MB log bound, startup tmp-orphan sweep,
  all-exhausted WARNING deduped to episode+hourly, claude-swap version
  contract warning, run.sh interpreter guard, launchd ThrottleInterval=60.

61 tests (was 57).

## 1.0.4 — 2026-08-21

Provoked by a real 4-day incident: Codex hit its weekly cap, the vendor's client
then wrote only aborted stubs (`info: null`, and a new `rate_limits` shape with
`primary: null`), and the widget — correctly refusing to invent data — kept
showing the last real sample: "100% (!), resets Aug 20" for a day after Aug 20
had passed. The pipeline was right; the display was dishonest.

- A quota window whose reset instant has passed now renders as **overdue**, never
  as a live percentage with a `(!)`: the widget cannot know the current value, so
  it says so.
- A quota sample older than 2 hours shows its age, same idiom as account rows.
- Adversarially verified against clock edge cases: an indexer living across
  month boundaries and 30-day lookback aging counts every day correctly (a
  frozen-clock hypothesis was disproved by execution — regression-locked anyway).
- 4 new tests (57 total).

## 1.0.3 — 2026-08-17

Fixes found by running the release on simulated stranger machines (fake HOME, no
claude-swap, no corpora, stock-macOS Python) rather than only on the author's.

- **The archive had no top-level directory**, so `unzip` sprayed 22 entries into
  whatever folder the user was in and the README's own first command,
  `cd cc-usage-widget`, exited 1. The zip now contains one `cc-usage-widget/`
  folder, deliberately unversioned so the instruction stays correct every release.
- **`uninstall.sh` stopped every widget on the machine.** `pkill -f` is an argv
  substring match; it reached out of an isolated test environment and killed an
  unrelated running instance. It now reads the owning PID from `widget.lock` and
  verifies that process really belongs to this install.
- **`install.sh` could build a Python 3.9 venv** that byte-compiles cleanly and
  then dies at import. The `uv` path now fails loudly instead of falling back,
  and an existing venv is version-checked before being reused — the pin alone
  only guarded creation.
- **Missing claude-swap showed a raw `ModuleNotFoundError`** in the menu. It now
  reads: "claude-swap not installed - account features are off (cost tracking
  still works)".
- **`--dry-run` and `--help` wrote to global macOS user defaults.** Both are
  documented as read-only; the status-item position seed now runs only on a real
  launch.
- **State files were created world-readable (0644)**, including the one holding
  subscription usage. New files are 0600.
- `uninstall.sh` now removes `codex_scan_state.json` and
  `codex_scan_state_quota.json`, which it previously left behind.

## 1.0.1 — 2026-08-17

**Codex (OpenAI) support — the widget is now dual-vendor.**

Added
- **Codex subscription quota** in the menu: the weekly window read from the local
  rollout logs (`used_percent`, reset time, plan), rendered beside the Claude
  accounts. This is a real number from OpenAI about the plan you are consuming —
  it is the headline figure, not the dollars.
- Notional per-model Codex cost from `~/.codex/sessions`, priced from OpenAI's
  published table (`gpt-5.6-sol` / `-terra` / `-luna`, `gpt-5.4`, `gpt-5.4-mini`).
- Vendor labelling throughout; cost totals span both vendors.
- 20 new tests (51 total) covering the Codex-specific correctness traps.

Notes on correctness — each of these is a trap that produces a *plausible wrong
number* rather than an error, so each has a test that fails on the naive version:
- The model is not on the Codex usage record; it is announced separately, so
  attribution is stateful and must survive a scan resuming mid-file.
- `total_token_usage` is cumulative **and resets mid-session** — only per-turn
  `last_token_usage` is summed.
- `cached_input_tokens` is a **subset** of `input_tokens` (OpenAI reports these
  overlapping; Anthropic does not), so uncached is `input - cached`.
- `reasoning_output_tokens` is a subset of `output_tokens` and is not re-added.

Fixed
- A lost or deleted Codex scan-state file could wipe the **entire** shared rollup,
  destroying Claude history too. Recovery is now per-vendor.
- A source joining mid-session could be merged twice (exactly 2× counts).
- A rate-limit record carrying a plan but no window could blank the quota bar.
- An absent `~/.claude/projects` now degrades silently instead of showing a
  permanent error row (matters for Codex-only users).
- Packaging now uses an explicit allowlist plus a leak guard, so runtime state —
  including the file holding subscription usage — cannot be swept into a release.

Performance
- Scanning the two corpora alternates ticks, keeping the combined tick inside
  budget (median 34 ms → 19 ms). Each vendor keeps its own cadence.

Unpriced models (e.g. `gpt-5.5`, `codex-auto-review`) show their token counts at
`$0` and are surfaced by name. Prices are never guessed.

## 1.0.0 — 2026-08-17

Initial release. Claude Code only: per-account quota bars (5h / weekly /
per-model), reset times, pace warnings, click-to-switch, optional auto-switch via
claude-swap, and notional cost from local transcripts.
