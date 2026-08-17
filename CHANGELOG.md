# Changelog

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
