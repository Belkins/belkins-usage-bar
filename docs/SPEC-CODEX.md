# cc-usage-widget — Codex (OpenAI) support

Addendum to SPEC.md. Adds a **second vendor** so one widget shows Claude Code and
Codex usage together. Everything in SPEC.md still holds; this documents only the
deltas. **Status:** implemented and shipped. Kept as the design record.

## 1. Evidence base (probed on this machine 2026-08-17, not assumed)

| Fact | Measured |
|---|---|
| Corpus | `~/.codex/sessions/**/rollout-*.jsonl` — **15 GB, ~3,000 files** |
| Files touched in 24 h | **90** → the mtime/size pre-filter design applies unchanged |
| Usage record | `type: event_msg` → `payload.type: token_count` |
| Quota record | same record, `payload.rate_limits.primary` (`used_percent`, `window_minutes: 10080`, `resets_at` epoch), `plan_type: "pro"` |
| Models seen (30 d) | `gpt-5.6-sol` (15,560), `-terra` (236), `-luna` (154), `gpt-5.4-mini` (48), `gpt-5.4` (4), `codex-auto-review` (4) |

## 2. The three traps — each needs a fixture test

1. **Model is NOT on the usage record.** It lives in a separate `type: turn_context`
   record (`payload.model`). The extractor is therefore **stateful**: a
   `turn_context` sets the current model; every following `token_count` attributes
   to it. This differs from Claude, where model and usage share one record.
   *Resume safety*: when a scan starts mid-file at a stored offset, the current
   model is unknown — persist the last-seen model per file in scan state, else
   post-resume turns get mis-attributed. Unknown → bucket as `codex:unknown`, $0.
2. **`total_token_usage` is cumulative AND resets mid-session.** Measured in one
   session: sum of per-turn `last_token_usage.input` = **252,100,617** vs final
   `total_token_usage.input` = **230,324,294**. So `total_*` is neither a per-turn
   delta nor a reliable session total. **Only sum `last_token_usage`.**
3. **`cached_input_tokens` is a SUBSET of `input_tokens`, not additive.** Uncached
   input = `input_tokens - cached_input_tokens`. Pricing must subtract first, or
   every turn is overcharged. (Anthropic reports these disjointly; OpenAI does not.)
   `cache_write_input_tokens` is separate and priced at the standard input rate.

## 3. Pricing — from OpenAI's official docs (developers.openai.com/api/docs/pricing)

Per 1M tokens. Cached input is a real published rate (10% of input), **not** derived.

| model | input | cached input | output |
|---|---|---|---|
| gpt-5.6-sol | 5.00 | 0.50 | 30.00 |
| gpt-5.6-terra | 2.00 | 0.20 | 12.00 |
| gpt-5.6-luna | 0.20 | 0.02 | 1.20 |
| gpt-5.4 | 2.50 | 0.25 | 15.00 |
| gpt-5.4-mini | 0.75 | 0.075 | 4.50 |

`reasoning_output_tokens` is a *subset* of `output_tokens` — do NOT add it again.
`codex-auto-review` has no published rate → unknown-model path ($0 + surfaced name).

Cost per turn:
```
(input_tokens - cached_input_tokens) * input_rate
+ cached_input_tokens               * cached_rate
+ cache_write_input_tokens          * input_rate
+ output_tokens                     * output_rate
```

## 3a. Subscription usage is the headline; dollars are the shadow price

**Clarified by the user 2026-08-17.** Codex runs on a **ChatGPT Pro subscription**,
not metered API billing — exactly as Claude Code here runs on Max/Team. So the
figure that answers "how much have I used?" is the **subscription quota**:

    rate_limits.primary.used_percent   (12%)
    rate_limits.primary.window_minutes (10080 = weekly)
    rate_limits.primary.resets_at      (epoch)
    rate_limits.plan_type              ("pro")

That is a REAL number from OpenAI, about the plan actually being consumed. The
per-model dollar figure priced from the API table is **notional** — what the same
tokens would have cost on the API — and must stay labelled as such wherever it
appears, never presented as a bill or as subscription spend.

Implication for the UI: the Codex quota bar is the primary element and belongs
with the account blocks; the Codex dollar rows sit under the existing
"Cost (notional, API list prices)" heading, which already carries the disclaimer.

Two things deliberately NOT built (documented so nobody re-derives them later):

- **No subscription-dollar figure.** A Pro plan is a flat fee; dividing it by usage
  to synthesise "$ spent" would be an invented number. Not shipped.
- **`credits` / `spend_control_reached` are ignored for now.** The record carries
  `credits: {has_credits: false, unlimited: false, balance: "0"}`. On this account
  they are inert, and rendering a $0 credit balance would imply metered billing
  that is not happening. Revisit only if a user turns credits on.

## 4. Architecture deltas

- `contracts.py` — add a `vendor` field (`"claude"` | `"codex"`) to the usage/cost
  types and to `AccountRow`. Rollup keys become `(vendor, model)`.
- `codex_indexer.py` — new module, same 8-step incremental algorithm as SPEC 3.2
  (scandir → (size,mtime) skip → lookback → truncation guard → seek → prefilter →
  parse → atomic state). Prefilter substring is `"token_count"`. Roots `~/.codex/sessions`.
- `pricing.py` — vendor-aware table; OpenAI rows above with their own cached rates
  (Claude's derive-from-input multipliers must NOT be applied to OpenAI models).
- `accounts.py` — unchanged. Codex has no claude-swap equivalent: **no account
  switching, no autoswitch.** A Codex quota row is a *pseudo-account* built from the
  newest `rate_limits.primary`, rendered read-only (not clickable).
- `app.py` — vendor label per row; cost section totals across both vendors.

## 5. Definition of done

1. Menu shows a **Codex** section: weekly quota bar (from `primary.used_percent`,
   reset from `resets_at`) + per-model token/cost rows, beside the Claude accounts.
2. Cost totals (today / 7 d / 30 d) span both vendors; per-model rows are labelled
   by vendor.
3. All three traps in §2 have fixture tests that FAIL on the naive implementation.
4. Perf on the real 15 GB corpus: steady tick still < 30 ms, RSS still < 70 MB,
   first index background and lookback-bounded. **Measured, not asserted.**
5. Claude-only users see no Codex section (absent `~/.codex` is normal, not an error);
   Codex-only users see no accounts section. 31 existing tests still pass.
