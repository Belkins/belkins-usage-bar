# cc-usage-widget — Codex (OpenAI) support

Addendum to SPEC.md. Adds a **second vendor** so one widget shows Claude Code and
Codex usage together. Everything in SPEC.md still holds; this documents only the
deltas. **Status:** implemented and shipped. Kept as the design record.

## 1. Evidence base (probed on this machine 2026-08-17, not assumed)

| Fact | Measured |
|---|---|
| Corpus | `~/.codex/sessions/**/rollout-*.jsonl` — **~15 GB, ~3,000 files** |
| Files touched in 24 h | **90** → the mtime/size pre-filter design applies unchanged |
| Usage record | `type: event_msg` → `payload.type: token_count` |
| Quota record | same record, `payload.rate_limits.primary` (`used_percent`, `window_minutes: 10080`, `resets_at` epoch), `plan_type: "pro"` |
| Models seen (30 d) | `gpt-5.6-sol` (15,560), `-terra` (236), `-luna` (154), `gpt-5.4-mini` (48), `gpt-5.4` (4), `codex-auto-review` (4) |
| **Re-measured 2026-09-09** (rollouts touched in the last 3 d, `turn_context` records) | **`gpt-6-astra`** is now the dominant model — about three Astra turns per Sol turn — followed by `codex-auto-review`, `gpt-5.6-sol`, then a handful of `-luna` / `-terra`. Astra has been the headline model since its 2026-09-03 launch, `config.toml` runs on it, and priced at its rate it is already the larger notional line in the 7-day window. |

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

Per 1M tokens, standard tier. Cached input is a real published rate (10% of input), **not** derived.
Verified 2026-08-17; re-verified 2026-09-09 (Astra added, Sol's cut recorded — `pricing.OPENAI_SOL_RATE_CUT_DAY`).

| model | input | cached input | output |
|---|---|---|---|
| gpt-6-astra (from 2026-09-03) | 10.00 | 1.00 | 50.00 |
| gpt-5.6-sol — through 2026-09-02 | 5.00 | 0.50 | 30.00 |
| gpt-5.6-sol — from 2026-09-03 | 4.00 | 0.40 | 20.00 |
| gpt-5.6-terra | 2.00 | 0.20 | 12.00 |
| gpt-5.6-luna | 0.20 | 0.02 | 1.20 |
| gpt-5.4 | 2.50 | 0.25 | 15.00 |
| gpt-5.4-mini | 0.75 | 0.075 | 4.50 |

`reasoning_output_tokens` is a *subset* of `output_tokens` — do NOT add it again.
`codex-auto-review` has no published rate → unknown-model path ($0 + surfaced name).
Sol's two rates are both real (the old one verified here on 2026-08-17, the new one on OpenAI's
page on 2026-09-09); OpenAI does not publish the cut date, so the bound sits at Astra's launch day
and the uncertainty is stated in `pricing.py` rather than hidden. A rollout does not say which
service tier ran, so Fast/Batch/Flex rates are not represented.

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

## 6. Live per-account quota — one row per Codex login (2026-09-09)

**Status:** built on `feat/codex-accounts`. Off by default
(`codex_live_quota_enabled: false`), which is the rollback switch: with it off,
or with no credential adopted, the menu is byte-for-byte §5's single
transcript-derived row.

### 6.1 Why

§4's Codex row is honest but **anonymous**: a rollout carries no account id, so
its percentage can only describe whichever login wrote it. With four Codex
logins — two of them the same email in two ChatGPT workspaces — that one row
answers the wrong question, and it goes silent about the other three entirely.
So we ask the vendor: one authenticated read per tracked account.

Display only. Switching the active Codex login from the widget is deliberately
out of scope (the user chose it that way); the widget marks which login the CLI
is on and never changes it.

### 6.2 Endpoint contract (probed 2026-09-09, HTTP 200)

```
GET https://chatgpt.com/backend-api/wham/usage
Authorization: Bearer <tokens.access_token>
ChatGPT-Account-Id: <tokens.account_id>
Accept: application/json
```

Body (Pro): `{user_id, account_id, email, plan_type:"pro",
rate_limit:{allowed, limit_reached, primary_window:{used_percent,
limit_window_seconds, reset_after_seconds, reset_at}, secondary_window},
code_review_rate_limit, additional_rate_limits:[{limit_name, metered_feature,
rate_limit:{primary, secondary}, normal_model_slug}], model_usage, credits,
spend_control, rate_limit_reached_type, promo, rate_limit_reset_credits}`.

It reports the plan's limits **without invoking a model**, so a poll costs no
quota. Transport rules: no redirects (a 3xx from an authenticated API is a
login wall or a captive portal — following it would hand the bearer token to
whatever it points at), 10 s connect / 15 s read, body capped at 256 KB.
Schedule: `codex_quota_interval_seconds` (default 300, clamped 60–3600) per
account, ≥20 s between two accounts, ±15 % jitter from a hash of the account id
(never `random`, so a scheduling bug reproduces).

**Business shape — captured 2026-09-10** (`plan_type: self_serve_business_prolite`,
fixture `CAPTURED_business_body` in the tests): same envelope as Pro with four
differences the mapper now handles — `rate_limit_reached_type` is an **object**
(`{"type": "workspace_owner_credits_depleted", "details": null}`; the `type` is
read, anything else yields a bare `capped`), `additional_rate_limits` is `null`,
`credits.balance` is `null`, and a new `rate_limit_upsell` block is present and
ignored. That workspace was at 100 % weekly with `allowed: false` — the capped
row keeps its bar and names the type. Enterprise shapes remain unverified; the
`SYNTHETIC_*` test still covers windows arriving in the other order.

### 6.3 Credential layout

| Path | Written by | Contains |
|---|---|---|
| `codex-accounts/<account_id>/auth.json` | `codex login` only, **never the widget** | one account's tokens (0600, dir 0700) |
| `codex_accounts.json` | the widget / the `adopt` CLI | `{version, accounts:[{account_id, alias, enabled, order}]}` — no token, no email, no percentage |
| `codex_quota_snapshots.json` | the widget | last reading + standing note per account; no token |
| `~/.codex/auth.json` | the ChatGPT desktop app | read for **exactly one field**, `tokens.account_id`, to mark the active row. Never opened for writing. |

All three widget paths are git-ignored, and a test asserts they are neither
tracked nor committable. A credential file with any group or world bit is
**refused, not used** — a token another user can read is a token to rotate, and
polling with it quietly would hide that.

The directory name must equal the `tokens.account_id` inside it. That is the
whole identity check: a copied or renamed dir fails loudly instead of showing
one account's quota under another's alias.

Onboarding is its own process, so it never contends with the widget's flock:

```
CODEX_HOME=~/.claude/cc-usage-widget/codex-accounts/new-1 codex login
python -m cc_usage_widget.codex_accounts adopt   # offline: decode, rename, chmod, register
python -m cc_usage_widget.codex_accounts list    # registry + which id ~/.codex holds
python -m cc_usage_widget.codex_accounts probe <alias>   # one GET, raw window widths
```

`adopt` makes **no network request** (the id, email, plan and expiry all come
from the token payload) and **refuses a duplicate account id**, printing both
paths: two dirs claiming one account means the workspace picker was not used as
intended, and silently keeping one would hide a login that is not tracked.

### 6.4 Token policy: phase 1 never refreshes

The stored access token lives ~10 days (`exp = iat + 10 d`). The widget decodes
`exp` locally — base64url payload, **no signature verification**, which is safe
because nothing security-relevant is decided from it: the endpoint, not us,
decides whether a token is good. From that:

* `exp − now ≤ 48 h` → a dim `relogin in 1d 4h` countdown, and the poll still happens;
* `exp ≤ now` → the sentinel `relogin` and **no request is made** (an expired
  token has exactly one outcome; making the call would only teach the endpoint
  our polling schedule).

Refresh is not implemented. Whether two `codex login` sessions under the one
public OAuth client (`app_EMoamEEZ73f0CkXaXp7hrann`) share a refresh-token
family is unverified, and a wrong guess logs the user out of the account they
are coding in.

**Revisit trigger** (the only thing that opens phase 2): a throwaway
`CODEX_HOME`, one refresh grant through the transport, persist-before-use, a
24 h soak confirming the first store *and* `~/.codex` still work, then a
deliberate replay of the superseded refresh token to learn whether reuse
detection revokes the family. Only a clean result earns
`codex_refresh_enabled` (default off, proactive at `exp − 24 h`, reactive once
on 401, never on 403). Until then the 10-day relogin is the shipped cost and
it is stated on the row rather than hidden.

### 6.5 Mapping rules

* **A window is identified by its WIDTH, never its position.**
  `limit_window_seconds == 604800` → `seven_day_pct`; `== 18000` →
  `five_hour_pct`; anything else → `scoped_windows`, labelled by
  `render.window_minutes_label` (so `3600` reads as `hourly`). Which of
  `primary_window` / `secondary_window` is the week is not guaranteed and
  differs by plan.
* **First writer wins.** A second window of a width already filled goes to
  `scoped` under its own name rather than displacing the plan's figure.
* **`additional_rate_limits` are opt-in** (`codex_show_extra_limits`, default
  off) and always render under their own `limit_name`, never in the two named
  slots — a Spark pool that happens to be 5 h wide is a different quantity from
  the plan's 5-hour bucket.
* **Reset clocks are anchored to the read instant** from `reset_after_seconds`,
  not taken from the body's absolute `reset_at` (computed on the server's
  clock). A reset more than 120 s in the past marks the window expired and its
  note becomes `overdue (<clock>)`, exactly as §4's row does.
* `plan_type` passes through **verbatim** (treat it as opaque: the enum
  includes `self_serve_business_prolite`, `ent26`, `enterprise_cbp_*` …).
* `spend_control.individual_limit` is **not rendered** — the unit is unverified,
  and a wrong unit beside a real number is worse than silence. `credits` stay
  ignored for §3a's reason.
* A response whose `account_id` is not the credential's is **dropped**: the
  previous snapshot is kept and a `!` diagnostics line says so. Showing another
  account's figure under this alias is the worst failure this file can have.

### 6.6 Honesty rules (SPEC 4.3, applied)

A sentinel **replaces** a number; it never decorates one. Each is a
`(attention_note, attention_kind)` pair on the row — renderers colour on the
*kind*, never on the wording, because a reworded note once silently lost its
colour. Structurally: while a **`warn`** sentinel stands the source withholds the
row's bars (the reading is kept in the sidecar and returns when the note
clears); the one **`crit`** note, `capped`, keeps the bars because the
percentage is the evidence. The title alarms (`C⚠`) only for a warn/crit row
that carries **no figure** — so a capped plan still reads `C100%`.

| Sentinel | Set by | Cleared by | Backoff |
|---|---|---|---|
| `relogin` | `exp ≤ now` (no request) | the next 200, or the credential file changing (`(mtime_ns, size)` moved: a fresh `codex login` shows within one cycle) | the normal interval — a login can land any moment and re-checking costs no request |
| `relogin` | 401 | same | +30 min |
| `no access` | 403 with a JSON body | the next 200 | +1 h |
| `rate limited` | 429 | the next 200 | `Retry-After` (seconds **or** HTTP-date) clamped to `[interval, 1 h]`, else ladder from 120 s |
| `endpoint error` | 5xx, non-JSON body (incl. an HTML 403 challenge), garbage — **after 3 consecutive** | the next 200 | 60/120/240/480/960 s, cap 30 min |
| `offline` | `OSError` from the transport — **after 3 consecutive** | the next 200 | same ladder |
| `awaiting first reading` | an enabled account with no snapshot | the first outcome of any kind | — |
| `credential unreadable` | `auth.json` missing, unparseable, claims another id, or not 0600 | a successful read, or the file changing | interval |
| `capped <type>` | `limit_reached` / `allowed:false` | the next healthy 200 | — (this one sits **beside** the figures: they are still true) |

Ageing has two steps, both from one number: past **15 min**
(`CODEX_FETCH_STALE_SECONDS`) the row shows its age beside the figure; past
**6 h** (`CODEX_FETCH_EXPIRE_SECONDS`) the **bars are withheld entirely** and
the row keeps only its header and its note. A six-hour-old percentage of a
five-hour window is not a stale figure, it is a wrong one.

The three-strike rule on `endpoint error` / `offline` is deliberate: a note
that appears on the first blip is a note the eye learns to ignore, and then the
real one is invisible too. The counter belongs to one ladder at a time
(`FetchState.failure_class`): two 429s followed by a 502 leave the row at
`rate limited`, not at a first-strike `endpoint error`. A 200 that answers for
**another** account keeps the previous reading with a `!` diagnostics line; a
200 for *this* account that carries nothing usable takes the endpoint ladder.
The snapshots sidecar holds only accounts the registry still lists (disabled
ones included), so a removed account cannot resurrect a figure or a note.

Snapshots **and** standing notes are persisted to the sidecar and rehydrated at
cold start (so a restart does not claim a dead credential is merely awaiting
its first poll); backoff **counters are not** — a ladder is a statement about
the last few minutes of network, and restoring it would back a fresh process
off over yesterday's outage.

**Scheduling is on `time.time()`.** On this Mac `time.monotonic()` is
`mach_absolute_time()` and pauses while the lid is shut, so a monotonic
schedule would simply skip the sleep. The two clocks are still compared every
cycle: a wall-minus-monotonic divergence larger than two intervals is a wake,
and every account is marked due — which is what rescues an account sitting in
a 30-minute backoff computed before the lid closed.

### 6.7 Arbitration with §4's row

`contracts.merge_quota_rows(rows)` runs at the end of every worker tick, pure
and total: the transcript-derived row (slot 0) is dropped **iff** a live row
(negative slot) that is `is_active` carries a figure. Otherwise it returns
unchanged with its own age note. Both are never shown for one account, and
nothing else is touched. Zero credentials, or the feature off, means the menu
is exactly §5's.

### 6.8 Definition of done

1. Four rows in registry order, exactly one `· active`, plan strings verbatim,
   two rows legitimately sharing an email and keyed by id.
2. `grep -c "<first 12 chars of any access token>" logs/widget.log` = 0; the
   registry and the sidecar contain no token. Asserted by the canary test in
   `tests/test_privacy.py`, which plants the canary as both `access_token` and
   `refresh_token`, runs a full cycle, and has a negative control: the same
   check run through a transport that deliberately logs the `Authorization`
   header must FAIL.
3. `~/.codex/auth.json` is byte- and mtime-identical after a full cycle.
4. Claude-only and Codex-only machines keep the pre-6 layouts byte-for-byte.
