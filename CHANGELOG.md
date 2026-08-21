# Changelog

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
