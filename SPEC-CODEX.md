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

Per 1M tokens, standard tier, short context. Cached input and cache writes are real published
rates, **not** derived. Verified 2026-08-17; re-verified 2026-09-09 (Astra added, Sol's cut recorded
— `pricing.OPENAI_SOL_RATE_CUT_DAY`); re-verified 2026-09-25 (gpt-6-sol and gpt-6-luna added, the
"Cache writes" column stated per row, gpt-5.5 confirmed on OpenAI's own page).

| model | input | cached input | cache writes | output |
|---|---|---|---|---|
| gpt-6-astra (from 2026-09-03) | 10.00 | 1.00 | 12.50 | 50.00 |
| gpt-6-sol | 2.00 | 0.20 | 2.50 | 10.00 |
| gpt-6-luna | 0.10 | 0.01 | 0.125 | 0.50 |
| gpt-5.6-sol — through 2026-09-02 | 5.00 | 0.50 | (input rate) | 30.00 |
| gpt-5.6-sol — from 2026-09-03 (promotional, at least through 2026-11-21) | 4.00 | 0.40 | 5.00 | 20.00 |
| gpt-5.6-terra | 2.00 | 0.20 | 2.50 | 12.00 |
| gpt-5.6-luna | 0.20 | 0.02 | 0.25 | 1.20 |
| gpt-5.5 | 5.00 | 0.50 | - (input rate) | 30.00 |
| gpt-5.4 | 2.50 | 0.25 | - (input rate) | 15.00 |
| gpt-5.4-mini | 0.75 | 0.075 | (no column; input rate) | 4.50 |

`reasoning_output_tokens` is a *subset* of `output_tokens` — do NOT add it again.
`codex-auto-review` has no published rate → unknown-model path ($0 + surfaced name).
Sol's two rates are both real (the old one verified here on 2026-08-17, the new one on OpenAI's
page on 2026-09-09); OpenAI does not publish the cut date, so the bound sits at Astra's launch day
and the uncertainty is stated in `pricing.py` rather than hidden. A rollout does not say which
service tier ran, so Fast/Batch/Flex rates are not represented. Cache writes bill at the page's
"Cache writes" column (1.25x input wherever it is printed); only where the page prints `-` or has
no such column, and on Sol's pre-cut row, do they bill at the standard input rate. The page also
lists higher long-context rates; a rollout does not say which context tier a turn billed at and the
GPT-6 rows state no threshold, so every turn is priced at the short-context rate (long-context
turns read low) and no threshold is invented.

Cost per turn:
```
(input_tokens - cached_input_tokens) * input_rate
+ cached_input_tokens               * cached_rate
+ cache_write_input_tokens          * cache_write_rate
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

### 6.4 Token policy: refresh is on, behind four guards

The stored access token lives ~10 days (`exp = iat + 10 d`). The widget decodes
`exp` locally — base64url payload, **no signature verification**, which is safe
because nothing security-relevant is decided from it: the endpoint, not us,
decides whether a token is good. From that:

* `exp ≤ now` → the sentinel `relogin` and **no request is made** (an expired
  token has exactly one outcome; making the call would only teach the endpoint
  our polling schedule);
* `exp − now ≤ 48 h` → a dim `relogin in 1d 4h` countdown, and the poll still
  happens — **but only when nobody will renew the token** (CX-7a): refresh is
  off, the file has no refresh token, the family is known dead, the last
  attempt failed, or this is the `~/.codex` account's widget copy (never
  rotated, below) **with no unexpired app login behind it**. With refresh
  working the token is rotated at 24 h, and a countdown would cry wolf for a
  day. While the app's own login for that account is unexpired it carries the
  row when the widget copy runs out, so that copy shows no countdown either
  (SMC-3); when `~/.codex` moves to another account the countdown returns. A
  capped row keeps its countdown as an info line beside the `crit` note
  instead of dropping it.
* Every Codex row carries `AccountRow.credential_expires_at` — the `exp` claim
  of the token its last poll used (a claim, not a secret), `None` before the
  first poll. For the `~/.codex` account's widget copy while the app's login
  is unexpired, it is that login's `exp`: the deadline of the login that will
  actually carry the row, not of a copy nobody renews.

**History.** Refresh shipped switched off (`codex_refresh_enabled`, default
`False`, roadmap 13) while one question was open: does rotating one `codex
login` under the public OAuth client (`app_EMoamEEZ73f0CkXaXp7hrann`) revoke
another? On 2026-09-20 every widget copy of a ten-day token expired and every
Codex row said `relogin` for five days, with nothing in the log. On 2026-09-25
`probe-refresh` rotated three widget copies by hand; each new token answered
HTTP 200, and `~/.codex` answered HTTP 200 before and after each rotation. The
default is now **`True`** (CX-4), and a Settings item — *Refresh Codex logins
automatically*, shown while live quota is on — is the off switch. An install
whose `settings.json` already says `false` keeps it until that item is clicked.

**Off means silent.** With the setting off, `TokenRefresher` is never called:
no POST of any kind is made, and `auth.json` is read and never written. The
test that proves it sets the switch to `False` explicitly, injects a transport
whose `post_form` fails the suite, runs a full cycle over a token one hour from
expiry, and asserts the credential file is byte-identical afterwards. The
reactive path is behind the same switch, so a 401 is not a back door.

**The four guards (2026-09-25):**

1. **Never the `~/.codex` account (CX-3), failing closed (SEC-1).** The
   account `~/.codex/auth.json` is logged in as is never rotated by the widget:
   our copy of it may share the ChatGPT app's refresh family, and rotating it
   would log the app out. Its widget copy therefore shows the countdown unless
   the app's login backs it (above). The app rewrites that file in place, so a
   read can land mid-write; "could not read it this tick" is never taken for
   "nobody is logged in". The last account a read named is persisted in the
   sidecar as the top-level `desktop_account_id` (an id, not a secret) and
   stays protected — across restarts — until a read names a different
   account, or a read proves nobody is logged in: an API-key file (no
   `tokens` at all) releases it at once, and a file missing on every poll for
   longer than `CODEX_ACTIVE_GRACE_SECONDS` (600 s) releases it then (the
   next sidecar save drops the key). A torn file, a `tokens` object without
   `account_id` or a shorter absence never clears it; a torn read in the
   middle of an absence restarts that clock. With nothing ever seen, a missing file or an API-key
   file (no `tokens` at all) holds no OAuth login and blocks nothing, while an
   unreadable one blocks every rotation that poll, with one log line per
   episode (`~/.codex login unreadable and never seen; token refresh skipped
   until it reads`); the countdown does not treat that as a deadline.
   `TokenRefresher.refresh` takes the caller's answer as a required `active`
   keyword and refuses the grant itself — no POST — for that account or,
   while the answer is unknown, for any; `probe-refresh` passes it too.
2. **A refusal is terminal until the file changes (CX-2).** `invalid_grant`,
   `refresh_token_reused`, `refresh_token_expired` and
   `refresh_token_invalidated` — flat (`{"error": "…"}`) or nested
   (`{"error": {"code": "…"}}`) — record the file's `(mtime_ns, size)` as
   `FetchState.dead_refresh_sig`. While the file keeps that signature neither
   rotation point runs; a new `codex login` moves it and earns exactly one new
   attempt. The signature is persisted in the sidecar record as
   `refresh_dead_sig: [mtime_ns, size]` and rehydrated at start, so a restart
   does not buy a dead POST either. (Before: one dead POST per poll, 288 a day
   per account.)
3. **Compare before write (CX-3).** `TokenRefresher` keeps a fingerprint of
   what it read — `(mtime_ns, size, sha256 of the refresh token)`, in memory
   for that call only — and re-reads the file after its POST. If the Codex CLI
   rotated the file meanwhile, the widget's grant is discarded, the newer file
   is kept, the outcome is `raced`, the credential is re-read from disk, and
   the log says `codex <alias>: auth.json changed during refresh; kept the
   newer file`. A refusal is judged only after the same re-read: if the file's
   refresh token moved, the refusal was about the superseded token, so it is
   `raced`, not `relogin`.
4. **The app's login is borrowed, never refreshed (CX-1).** For the account
   `~/.codex` is logged in as, `CredentialStore.read_desktop` reads that file
   read-only (same `0o077` refusal; `tokens.account_id` must equal the account;
   the refresh token is never returned; the file is never opened for writing).
   The desktop token is used when it is unexpired and the widget copy is
   missing, expired or older. A row read with it says `via ChatGPT app login`
   as an info line, shows no countdown (the app renews it), and neither
   rotation point runs; a 401 on it earns one retry with an in-date widget copy,
   else `relogin`. The cycle watches that file's `(mtime_ns, size)` too, so the
   app rewriting its login clears a standing `relogin` within one tick.

**On, the grant rules are:**

| Rule | Why |
|---|---|
| `POST https://auth.openai.com/oauth/token`, form body `grant_type=refresh_token&refresh_token=…&client_id=app_EMoamEEZ73f0CkXaXp7hrann` | one endpoint, one grant type; the client id is public and already sits in every `auth.json` |
| **Persist before use** — rotated `refresh_token` / `access_token` / `id_token` written to that account's `auth.json` (temp file + `os.replace`, 0600, every other key preserved) *before* the new access token authorises anything, and the `Credential` returned is **re-read from that file** | a crash between the POST and the write costs one grant; the reverse order costs the account |
| **No temp file survives (SEC-2)** — the temp file is `auth.json.tmp.<pid>_<random>` and every exception path unlinks it. A kill between the write and the replace can still leave one, holding the new tokens, in `codex-accounts/<id>/` — which the widget-home orphan sweep skips on purpose. The poller's first cycle in each process, and then one cycle an hour (a launchd restart meets the file seconds old, too young to judge), removes `auth.json.tmp.*` there when it is over 10 minutes old and its pid is dead (a name without a pid, or with one no process could own: age alone); never `auth.json`, never a symlink. The sweep never raises | a second copy of a live refresh token must not accumulate beside the credential |
| **No replay path** — the superseded refresh token is overwritten in place. No `.prev`, no backup, no in-memory copy kept for a retry | if reuse detection is armed on this client, the only way to ask is on purpose |
| Proactive when `exp − now < 24 h` on that account's poll | 48 h is the countdown threshold; an hour leaves no room for a sleeping machine |
| Reactive **exactly once** on a 401 — one flag covers both trigger points, so one poll never posts twice | a dead family must not become a POST loop |
| **403 never refreshes** | a 403 is an answer about permissions; the token in hand is the one it refused |
| A terminal refusal → no retry in this poll or any later one until the file changes (guard 2). The row says `relogin` when the access token has expired or the refusal came from the reactive 401; a **proactive** refusal while the access token is still in date keeps reading with it and shows the `relogin in …` countdown to its real `exp` (SMC-1) — one log line (`refresh refused (invalid_grant) - relogin`), no `relogin`/`recovered` flap | the family is gone; asking again only teaches the endpoint our schedule. The access token it minted still works until `exp` |
| Any other failure (offline, 5xx, non-JSON, unwritable file) changes nothing — a diagnostics line, the stored token still valid until `exp`, and the countdown comes back | a failed rotation must not manufacture a sentinel, nor hide the deadline |
| A credential with any group/world bit is refused for rotation exactly as it is for reading | writing to it would only mint a second leaked token |
| Nothing about a token is logged — alias, outcome and remaining life only | SPEC-CODEX 6.8 rule 2, extended to the refresh token and the desktop login |

Our own rotation rewrites `auth.json`, which is the same signal the cycle uses
to spot a human running `codex login` under a standing sentinel. The stored
signature is therefore re-stamped after a successful (or raced) refresh;
without that, a warn sentinel's backoff would be cancelled on every cycle by
our own write.

**Logging (OPS-3/OPS-4).** A move into or out of `relogin`, `credential
unreadable` or `offline` writes one line — `codex vlad: relogin (access token
expired Sep 20 11:13)`, `codex vlad: recovered` — and a steady state writes
none; the last announced note is rehydrated with the sidecar note, so a restart
does not repeat it. The per-request `codex <alias>: 200 in N ms` line is
written only when the status changes, is not 200, or took over 3 s; the
request counter still counts every call. Diagnostics give the last status its
age (`last 200 5d ago`).

**The probe** stays, for evidence before and after any change here:

```
python -m cc_usage_widget.codex_accounts probe-refresh <alias>
```

One grant on that account, persisted, then one usage `GET` proving the new
access token is accepted — printing plan / email / expiry before and after, and
never a token. It is deliberately **not** gated on `codex_refresh_enabled` and
deliberately one-shot. The deliberate replay of a superseded token remains a
separate, manual experiment; there is no code path that can perform it by
accident.

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
  and a wrong unit beside a real number is worse than silence.
* `credits` and `model_usage` are read for **three** facts and nothing else
  (roadmap 11/12): `credits.balance` (a count, printed with no currency and
  only when it is positive or `has_credits` is true — the probed Pro body sends
  `has_credits:false, balance:"0"`, and `credits 0` there would read as a
  problem on a healthy account), `credits.has_credits` + a model's
  `credits_would_enable` (the corroborating route to the out-of-credits
  verdict), and `model_usage.<slug>.available_at` (rendered as
  `gpt-6-astra back Sep 15`, dropped once it has passed). Per-model token
  counts are still ignored: they come from the corpus, and two definitions of
  one quantity is how two numbers for one thing get shipped. The model is named
  by its own slug, matching `pricing.MODEL_DISPLAY_NAMES`.
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
| `out of credits · Add credits` | a capped 200 whose `rate_limit_reached_type.type` is `workspace_owner_credits_depleted`, or `credits.has_credits:false` **with** a model's `credits_would_enable` | the next 200 that is not capped | — (also beside the figures) |

**Facts beside the bars** (`AccountRow.info_notes`, roadmap 10/11/12). A note
is one standing verdict; these are dim lines that sit *under* the header and
never replace a figure: `credits 12`, `gpt-6-astra back Sep 15`, `at this pace:
wall in 6h`. They were all read at `fetched_at`, so a row that withholds its
bars (expired reading, or a warn sentinel) withholds these **and**
`soonest_reset_at` with them — otherwise the title would count down to a reset
nobody re-read.

**Pace** (roadmap 10). The sidecar keeps a per-account ring of the last 12
`(fetched_at, weekly used_percent)` samples, recorded on every healthy 200
whatever `codex_pace_forecast_enabled` says — the setting gates the note, not
the memory, so switching it on does not start an hour of silence. A forecast
needs **three** samples spanning **30 minutes** with a strictly rising
percentage, is never made on a capped row, and is trimmed at any percentage
DROP (a window that went 98 % → 3 % has no trend through the seam). When the
projection lands at or beyond the window's reset the note is `at this pace:
resets first` rather than a countdown the plan makes impossible.

**Earliest reset** (roadmap 4). `AccountRow.soonest_reset_at` is the epoch of
the earliest reset the row reports — the one numeric reset on a row, because
`*_resets_at` are display strings and nothing can order two rows by them. Two
renderers read it: the menu-bar suffix `↺4d` on a **capped active** row, hard
capped at four characters (`render.TITLE_RESET_SUFFIX_MAX`) and absent below the
wall, and the Codex block's fleet heading `Codex 1/4 · next Sat 09:00 (vlad)` —
rooms are live rows whose weekly figure is known, under 100 and carrying no
warn/crit verdict (a credit-blocked account with an unspent week is **not** a
room), and `next` is the soonest reset among the capped ones, with its alias. A
row whose source carries no epoch — every claude-swap row, and the
transcript-derived Codex row — renders neither, which is what keeps a
live-quota-off machine byte-for-byte as it was.

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

### 6.7a Launching on the best account (roadmap 5)

`python -m cc_usage_widget.codex_accounts best [--json]` prints the `CODEX_HOME`
of the enabled account with the lowest weekly usage that still has room; when
every account is capped, the one whose window reopens first. It ranks the very
rows `quota_rows()` builds — so the CLI and the menu can never disagree — and it
is offline **by construction**: the source is built with a transport that raises
on any request. Skipped as candidates: an account with no credential directory
(there is no home to hand out) and one carrying a `warn` sentinel (there is
nowhere to send work). Nothing usable, no registry, or the feature off ⇒ one
line on stdout and **exit 2**, because the README's `codexb` propagates the code
and an empty `CODEX_HOME=` silently means `~/.codex`.

`link [--unlink]` symlinks `~/.codex/{config.toml,AGENTS.md,skills,plugins,
agents,rules,memories}` into each account home when the target exists and the
name is free. It never overwrites (a real file, or a link that points elsewhere,
is counted as `kept`), never writes anything under `~/.codex`, and `--unlink`
removes only links whose target is the matching upstream name. `auth.json`,
`sessions`, `history.jsonl` and `log` are deliberately not linked — each home
must keep its own credential and its own history.

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
5. With `codex_refresh_enabled` explicitly off, a full cycle over a token an
   hour from expiry makes **zero** token requests and leaves `auth.json`
   byte-identical — asserted by a transport whose `post_form` fails the suite,
   not by reading the code (6.4).
