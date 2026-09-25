<div align="center">

# Belkins Usage Bar

**How much of your AI coding subscriptions have you actually used?**

A macOS menu bar widget for Claude Code and Codex — live quota bars, reset times,
and what your usage would cost at published API rates. Computed entirely on your
own machine.

[![tests](https://github.com/Belkins/belkins-usage-bar/actions/workflows/tests.yml/badge.svg)](https://github.com/Belkins/belkins-usage-bar/actions/workflows/tests.yml)
[![platform](https://img.shields.io/badge/platform-macOS%2013%2B-lightgrey)](#requirements)
[![python](https://img.shields.io/badge/python-3.12%2B-blue)](#requirements)
[![license](https://img.shields.io/badge/license-MIT-informational)](LICENSE)
[![no telemetry](https://img.shields.io/badge/telemetry-none-brightgreen)](#privacy)

</div>

---

```
▁▄█ $42/d                                    ← always visible in your menu bar
└─────────────────────────────────────────────────────────────────┐
  Auto-switch:      ON                                            │
  Cost tracking:    ON                                            │
                                                                  │
  Accounts                                                        │
  1  work (you@company.com)                                       │
     5h    ░░░░░░░░░░░░░░░░░░   0%                                │
     7d    █████████████▍░░░░  74%     resets Aug 21  (ahead)     │
     Opus  ██████████████████ 100% (!) resets Aug 21  (ahead)     │
  2  personal (you@gmail.com)                    ● active         │
     5h    ██████████▋░░░░░░░  59%     resets 14:50               │
     7d    ██▊░░░░░░░░░░░░░░░  15%     resets Aug 22               │
                                                                  │
  Codex (pro)                                                     │
     weekly ██████░░░░░░░░░░░  34%     resets Aug 20              │
                                                                  │
  Cost (notional, API list prices)                                │
    Today        $41.98                                           │
    Last 7d     $310.20   ($44.31/day avg)                        │
    Last 30d  $1,284.55                                           │
    ── by model ─────────────────────────                         │
    Opus 5        61.0M tok    $31.10                             │
    gpt-5.6-sol   44.2M tok    $10.88                             │
└──────────────────────────────────────────────────────────────────┘
```

## Why

Subscription plans hide the two numbers you actually need: **how close to the
wall am I**, and **which account still has room**. This puts both in the menu
bar, for both tools, and adds a burn-rate figure so you can see a heavy week
coming before it lands.

## What it does

**Claude Code** — every account's 5-hour, weekly, and per-model quota windows as
bars, with real reset times and an *ahead of pace* warning when you're burning
faster than the window refills. Click any account to switch to it. Optional
auto-switch moves you off an account before it hits the wall.
*Account features need [claude-swap](https://github.com/realiti4/claude-swap); everything else works without it.*

**Codex** — your weekly subscription quota, read from the local rollout logs.
Optionally, one live row **per Codex account** — see below.

**Both** — notional per-model cost, computed by scanning your own transcripts,
and a [notification](#notifications) the moment an account crosses 85 %, hits the
wall, or comes back — once per transition, macOS and optionally Telegram.

## Install

Requires macOS 13+ and either [uv](https://docs.astral.sh/uv/) or Python 3.12+.

```bash
unzip cc-usage-widget-*.zip
cd cc-usage-widget
./install.sh          # creates a local venv — installs nothing globally
./run.sh              # a bar-chart icon appears in your menu bar
```

Start at login: `./install.sh --launch-agent`
Remove everything: `./uninstall.sh`

## Reading the menu

The dropdown opens on two cards that answer the questions you open it for:

* **Claude** — the account you are on, its *binding* window (the one
  auto-switch decides on) and when it resets (`↺ 14:50`), then the next
  account to move to, how many rooms have headroom (`4/5 room`), each model
  fleet line (`Fable 4/4`) and extra-usage spend once it passes 70 %. A badge
  counts the slots that need you.
* **Codex** — the login `~/.codex` is using, its weekly figure (or `⚠ relogin`
  instead of a figure it cannot vouch for), then `best:` (the same account
  `codex_accounts best` prints), the fleet count, the next reset and any reset
  credits. A reset credit you can use **now** is the one green line in the menu.

Below them: **Needs attention** (every slot or login waiting on you, with the
remedy — absent when nothing is), **Claude** (the active account's full bars,
then one line per other account, most headroom first), **Codex** (one line per
account plus its single most useful fact), recent switches and cost, then the
tools. Every reset reads the same way everywhere: `↺ 14:50` today, `↺ Thu
20:25` this week, `↺ Oct 2 10:49` later, `↺ overdue` once passed. The full
bars are one submenu away (**All Claude bars ▸**, **All Codex bars ▸**).

**Settings ▸ Classic menu layout** (`menu_layout_classic`) brings back the
previous menu exactly; it is also what a machine with no Claude accounts and no
live Codex row shows. **Settings ▸ Title ▸ Merge alerts into ⚠N**
(`title_merge_alerts`, off) folds `C⚠` and the bare `⚠` into one count that
matches the Needs attention section, which makes the title narrower. When the
Codex account you are on is capped and holds a usable reset credit, its
`↺<time>` countdown becomes a green `↺now` (**Reset-credit marker**,
`title_show_reset_credit`, on). The title text is otherwise unchanged; each
part is now coloured by its severity.

## Reading the Accounts block

With [claude-swap](https://github.com/realiti4/claude-swap) installed, the
Accounts block shows more than the bars:

* **A fleet line per model window**, directly under `Accounts` —
  `Fable 2/4 · next Tue 09:00`: how many enabled, switchable slots still have
  room in that weekly window, and when the soonest full one resets. One line
  per scoped window your slots report; a machine that reports none never sees
  it. **Settings ▸ Fable fleet line** (`Model fleet line` when there are
  several) turns it off — `scoped_fleet_line_enabled`, on by default — and the
  block is then exactly what it was before.
* **Extra usage is real money**, so it gets its own line under the account that
  spends it — `extra $480.00 / $500.00  96%  resets …` — and a
  `Real spend (extra usage) … · real, not notional` line in the Cost section. A
  notification fires when it crosses your threshold and again at the limit.
* **A window whose reset has passed is drawn dim**, without its `(!)`: the
  number is from the previous window. `(!)` itself now means *at the wall*
  (100 %); red still starts at 90 %.
* **A slot with a standing problem is dimmed and says so once** — its header
  reads `⚠ relogin · last seen 3d ago` or `⚠ 403`, and the full remedy is in
  the alert line at the top. A slot whose usage has been refused three times
  or more says why and what to run: `usage forbidden (HTTP 403) since <day> ·
  <n> failed polls — … log in as <email> and run cswap add, or cswap remove
  <slot> to stop polling`. A slot held in upstream's `429` backoff with no good
  fetch for a day or more says so too: `usage refused for N days since <day>
  (last error HTTP 429, upstream backoff)`, with `⚠ 429` in the title.
* **A `cswap disable`d slot is marked `disabled`**, sorts last in **Switch
  account** with `(disabled)`, and is never counted as room.
* **Account flips are named.** A running Claude Code session that rewrites
  `~/.claude.json` behind claude-swap's back is a *ghost flip*; three in an
  hour is a *login fight* (`⚠ ghost` in the title, always), and the alert lists
  the remedies. A switch that was reverted before the next pass says so
  (`switch →2 reverted to 1`); when claude-swap's log shows no revert, it is a
  ghost flip (`switch →2 reverted: ~/.claude.json rewritten to …`) and counts
  toward a login fight. A plain `cswap switch` or a single ghost flip to a
  healthy slot no longer badges the title. The newest switch is shown inline,
  and the rest are under a **Recent switches** submenu.
* **The title follows the window auto-switch actually decides on.** If that is
  the Fable window rather than the 5-hour one, the fleet suffix leads with it
  (`F90%`, `7d92%`), so you can see which wall is closing. A window whose reset
  has passed never binds the title and never costs the fleet a room.

## Optional: a live row for each of your Codex accounts

Out of the box the Codex row is read from your local rollout logs. Those logs
carry no account id, so the figure describes whichever login wrote them — fine
for one account, useless for several (and two ChatGPT workspaces under one email
are two accounts).

If you run more than one Codex login, the widget can show **one row per
account**, all of them live, with the login the CLI is currently using marked
`· active`. It is **display only** — the widget never switches your Codex login,
and never writes to `~/.codex`.

This is the one feature that makes a network request, so it is **off by
default**. Turning it on takes one login per account:

```bash
cd ~/.claude/cc-usage-widget                      # your install directory

# 1. Log in once per account, each into its own CODEX_HOME. The directory
#    must exist first: codex refuses a CODEX_HOME it cannot find.
#    The browser flow shows a workspace picker — pick a different workspace
#    each time if two of your accounts share an email.
mkdir -p -m 700 codex-accounts/new-1 codex-accounts/new-2
CODEX_HOME=$PWD/codex-accounts/new-1 codex login
CODEX_HOME=$PWD/codex-accounts/new-2 codex login

# 2. Adopt them. Offline: reads each token's own claims, renames the directory
#    to the account id, fixes permissions, registers it. No network request.
#    PY is the interpreter install.sh chose - the one named inside ./run.sh.
PY=$(sed -n 's/.*exec "\(.*\)" -m cc_usage_widget.*/\1/p' run.sh)
"$PY" -m cc_usage_widget.codex_accounts adopt

# 3. Name them, then switch the feature on.
open codex_accounts.json      # set "alias" per account: "work", "personal", …
```

Then in the menu: **Settings ▸ Codex accounts** — tick the accounts you want,
and turn on the live quota. Useful checks:

```bash
"$PY" -m cc_usage_widget.codex_accounts list   # registry + which login ~/.codex holds
"$PY" -m cc_usage_widget.codex_accounts probe work   # one request, raw numbers
```

Each command runs in its own process, so none of them interferes with the
running widget.

What to expect:

* Each account is read every 5 minutes (configurable 1–60 min), spaced out and
  sequentially — never four requests at once.
* A row says what is wrong instead of showing a wrong number: `relogin`,
  `no access`, `rate limited`, `endpoint error`, `offline`. A reading older than
  6 hours drops its bars and keeps the reason.
* Codex access tokens last about ten days. The widget **refreshes them by
  default**, about a day before one expires; see
  [Refreshing tokens](#refreshing-tokens) for the switch and its
  guards. Only when a refresh cannot happen does the row say `relogin in 1d 4h`
  two days out, and a dead login offers **Log in again…** in the menu (see
  below).
* Delete `codex_accounts.json` (or untick every account) and the menu goes back
  to exactly what it was.

### Log in again / add an account from the menu

A Codex row whose login is dead (`relogin`, or `credential unreadable`) is
clickable, and says when and what: `token expired Sep 20 11:13 · Log in again…`
(or `last reading 5d ago · Log in again…` when the expiry is not known).
**Settings ▸ Codex accounts ▸ Add Codex account…** does the same for an
account the widget does not track yet.

Either click writes a small script, `codex-accounts/login-<time>.command`
(0700, paths only — no token, no email), and opens it in Terminal. It runs
`CODEX_HOME=…/codex-accounts/new-<time> codex login` and then
`python -m cc_usage_widget.codex_login adopt --replace`, which reads the id the
NEW token claims and either swaps the fresh `auth.json` into that account's
existing home (a relogin: alias and checkbox kept) or registers it (a new
account). The browser shows a workspace picker; pick the workspace of the
account you clicked — two of these accounts can share an email, and the adopt
step files the login under whichever account it really is. The script deletes
itself when it succeeds.

Closed the Terminal before it finished? The widget adopts any finished
`new-*/auth.json` on its next poll (after a 30 s settle, so it never moves a
file `codex login` is still writing). `codex` is looked up on `PATH`, then
`/usr/local/bin`, `/opt/homebrew/bin`, and the ChatGPT app's own copy — the
LaunchAgent has no `/usr/local/bin` on its `PATH`. By hand:

```bash
"$PY" -m cc_usage_widget.codex_login adopt --replace
```

Plain `adopt` (either module) still refuses an account that is already
tracked; only `--replace` swaps a login in.

<!-- The old anchor, kept so links from before 2026-09-25 still land here. -->
<a id="refreshing-tokens-off-by-default"></a>

## Refreshing tokens

Ten days is not long, and four accounts means four browser logins every ten
days. The widget avoids them by exchanging the refresh token in each
`auth.json` for a fresh one about a day before the old one expires. The setting
that allows it, `codex_refresh_enabled`, is **on by default** since 2026-09-25,
and **Settings → Refresh Codex logins automatically** (shown while live Codex
quota is on) turns it off. Off means silent: no token request of any kind is
made and the widget only ever *reads* your credential files. There is a test
whose whole job is to fail if that stops being true.

It used to ship off, because of one unanswered question: every `codex login`
uses the same public OAuth client, and it is not documented whether rotating
the token for one login invalidates the others. The off default then cost more
than it protected: on 2026-09-20 every stored token expired and the Codex rows
said `relogin` for five days. Hand-run refreshes on 2026-09-25 rotated three
accounts cleanly while `~/.codex` kept working, and four guards now keep
refresh away from everything it could break:

* **The account `~/.codex` is logged in as is never refreshed.** The ChatGPT
  app owns that login. For that one account the widget instead borrows the
  app's own, fresher token from `~/.codex/auth.json` — read-only, never
  refreshed, never written — and the row says `via ChatGPT app login`. While
  the app's login is live, the widget's own copy of that account shows no
  countdown. The guard fails closed: the last account a read of
  `~/.codex/auth.json` named stays protected through a torn or missing file,
  and while an existing file has never been readable no account is refreshed.
* **A refused refresh is final** (`invalid_grant` and the
  `refresh_token_reused` / `_expired` / `_invalidated` codes): nothing is
  retried — not on the next poll, not after a restart — until you log in again
  and the file changes. While the stored token still works the row keeps its
  figures and counts down (`relogin in …`); once it expires the row says
  `relogin`.
* **A file the Codex CLI rotated mid-refresh is kept.** If `auth.json` changed
  while the widget's request was in flight, the widget throws its own result
  away and uses what the CLI wrote.
* **Nothing about a token is logged.** Log lines carry the alias, the outcome
  and how long the new token lasts.

And the grant itself stays narrow:

* a refresh is attempted when a token is within **24 hours** of expiring, and
  exactly **once more** if the API answers `401` — never twice in one poll;
* a `403` never triggers a refresh (that is an answer about permissions, not
  about the token);
* the rotated tokens are written to disk, atomically and `0600`, **before** the
  new access token is used for anything, so a crash costs one grant and never
  your login;
* the old refresh token is overwritten in place — no backup copy, nothing that
  could be re-sent by accident;
* any other failure changes nothing: your stored token is still valid until it
  expires, and the `relogin in 1d 4h` countdown comes back so the deadline is
  not hidden. While refresh is working that countdown stays away, because
  nobody needs to do anything.

To see one refresh happen end to end, on an account you choose:

```bash
"$PY" -m cc_usage_widget.codex_accounts probe-refresh gmail
```

That performs **one** refresh on that account, writes the rotated tokens into
its `auth.json` before using them, and then makes one usage request to prove
the new token is accepted. It prints the plan, email and expiry before and
after — never a token — and exits non-zero if either half fails. It refuses
the account `~/.codex` is logged in as.

## Notifications

The widget knows the moment an account crosses a line; by default it tells you
about it once, in Notification Center, and never again for the same transition.

It notifies on:

| | |
|---|---|
| a window crossing **85 %** upward | `Codex vlad · weekly 87% · crossed 85%` |
| a window reaching **100 %** | `Codex vlad · weekly 100% · resets Sat 09:00` |
| a window **coming back** (below 50 % after having been at or over the threshold) | `back: vlad 0% weekly` |
| a row that needs attention (`relogin`, `no access`, `offline`, `endpoint error`, out of credits) | the row's own wording |
| a claude-swap sentinel, or a self-audit that found drift | the note itself |
| a Codex login **within 24 h of expiring** while its row shows the `relogin in …` countdown | `Codex vlad · login expires Sep 20 11:13 · Log in again from the menu` |
| Claude **extra-usage spend** crossing the threshold, and again at its limit | `Claude main · extra usage $480.00 / $500.00 (96%) · crossed 85% of the extra-usage limit` |
| a model with **no published rate** reaching `unpriced_alert_min_tokens` today (default 1,000,000; 0 = off) | `Usage Bar · unpriced model · Codex gpt-… · 2.0M tokens today, counted at $0 (no published rate)` |
| a Codex **reset credit** becoming usable | `Codex reset available · vlad: 1 reset credit usable now — weekly 87%` |

Once per transition, not once per tick: a standing condition notifies once and
is only re-armed when it stops being true, so an account that sits at 100 % for
four days produces one notification, and the next time it walls it notifies
again. The ledger is `notify_state.json`; deleting it costs at most one repeat.
One exception: a standing Codex `warn` (a dead login, no access, offline) is
sent again once a day while it stands — a dead login announced once and then
never again is how four rows once sat on `relogin` for five days. Claude
sentinels are not repeated. Every notification actually delivered leaves one
line in `logs/widget.log`: `notify: sent <key>` (the key, never the wording).

Settings ▸ Notifications has the master switch and the threshold. The threshold
lives in `settings.json` as `notification_threshold_pct` (50–100).

### Telegram (optional)

Off until you configure it, and it is the only thing here that touches the
network. The bot token is read from an **environment variable**, never from the
command line, so it cannot land in your shell history or in `ps`:

```bash
cd ~/.claude/cc-usage-widget                      # your install directory
export TELEGRAM_BOT_TOKEN='…'                     # from @BotFather

# PY is the interpreter install.sh chose - the one named inside ./run.sh.
PY=$(sed -n 's/.*exec "\(.*\)" -m cc_usage_widget.*/\1/p' run.sh)
"$PY" -m cc_usage_widget.notify setup \
    --telegram-token-from-env TELEGRAM_BOT_TOKEN \
    --chat-id 123456789
"$PY" -m cc_usage_widget.notify test     # one macOS + one Telegram message
"$PY" -m cc_usage_widget.notify status   # prints no secret
```

`setup` writes `notify.json` at `0600`. The widget refuses to send from a
credential file that anyone else on the machine can read, and the Telegram
switch in Settings stays greyed out until the file is there. The token goes into
exactly one URL and is never logged, never rendered and never written anywhere
else — there is a test that plants a canary token and asserts it, plus a
companion test proving that check can fail.

Sends happen on their own short-lived thread: a Telegram request that times out
cannot delay the widget's tick.

What the block tells you once more than one account is live:

* a heading — `Codex 1/4 · next Sat 09:00 (vlad)` — how many logins still have
  weekly headroom, and when the first capped one reopens, named;
* `out of credits · Add credits` instead of a bare `capped …` when a workspace
  is blocked on credits rather than on a spent window, plus `credits 12` and
  `gpt-6-astra back Sep 15` when the endpoint reports them;
* `at this pace: wall in 6h` on a rising account — only with three readings
  spanning at least half an hour, and never on a capped one;
* `↺ 1 reset credit usable now — use it in Codex` first on a row whose
  account holds a rate-limit reset credit that applies now (`reset credits: 2
  (not usable now)` when it holds some and none applies), `· 1 reset usable` /
  `· 2 resets banked` on the heading, and a one-time `Codex reset available`
  notification. Display only: the widget never spends a credit;
* `C100%↺4d` in the menu bar when the account you are coding on is at the wall
  (that component is `title_show_codex_pct`, off by default).

**A narrow menu bar.** Set `"title_compact": true` (or **Settings ▸ Title ▸
Compact**) and the title becomes `V·C 100/100` — the active Claude account's
initial and 5-hour percentage, then Codex's initial and the active login's
weekly percentage, at most twelve characters. The other title switches are
inert while it is on, and a standing problem still replaces the figure it
invalidates (`V·C ⚠/100`) rather than showing a percentage the account can no
longer support.

## Run Codex on the best account

With several logins tracked, the widget can tell you which one to start the
next session on. It is a read of the widget's own sidecar — no network request,
no switching, and nothing written to `~/.codex`:

```bash
"$PY" -m cc_usage_widget.codex_accounts best          # prints a CODEX_HOME path
"$PY" -m cc_usage_widget.codex_accounts best --json   # + alias, weekly %, reset
```

It picks the enabled account with the lowest weekly usage that still has room,
skipping any that needs attention (`relogin`, `no access`, out of credits). When
every account is capped it picks the one whose window reopens first. When
nothing is usable it prints one line saying why and **exits 2**.

The per-account homes hold only `auth.json`, so a session started in one sees
none of your configuration. `link` mirrors it in, and it does so two different
ways on purpose:

```bash
"$PY" -m cc_usage_widget.codex_accounts link
#   copied once:  config.toml, memories        (Codex writes to these)
#   symlinked:    AGENTS.md, skills, plugins, agents, rules
```

**The names Codex writes to are copied, never symlinked.** A symlink at
`config.toml` would send the next `codex config set` inside that session
straight through into `~/.codex` — the one thing this widget promises never to
do — by a write the widget does not make and could not log. Copied, each
account gets its own model, approval mode and memories, and `~/.codex` stays a
file this program only ever reads. The copy happens once: a `config.toml` you
have since edited in an account home is that account's configuration and a
later `link` leaves it alone. A home still carrying a write-through symlink
from an earlier version is converted to a copy of the same file.

Everything else is linked, only when the name exists under `~/.codex`, never
overwriting anything already there — so an edit to a skill or a rule reaches
every account at once. `--unlink` removes exactly the links it made; the copies
are the home's own files and stay.

Then put this in your shell profile:

```bash
codexb() {
  local h
  h=$(python -m cc_usage_widget.codex_accounts best) || return $?
  CODEX_HOME="$h" codex "$@"
}
```

Caveat worth knowing before you use it daily: **Codex writes under the
`CODEX_HOME` you give it** — sessions, history and logs land in that account's
directory, not in `~/.codex`. That is the point (each account keeps its own
credential), but it means your rollout history is split across the account
homes, and `~/.codex/sessions` no longer sees everything. The widget's Codex
**cost** figures are read from `~/.codex/sessions/**/rollout-*.jsonl`, so tokens
spent in a `codexb` session show up in that account's quota row (which comes
from the vendor) but not in the cost block (which comes from the corpus). The
quota rows stay right either way; the dollar figures under-count by whatever
you ran on another home.

## What it costs your machine

Measured against a 1.4 GB Claude corpus and a 15 GB Codex corpus:

| | |
|---|---|
| Idle CPU | **~0%** — unchanged files are never opened |
| Memory | ~55–85 MB |
| Steady tick | ~19 ms |
| First index | seconds to under a minute, in the background — the menu never blocks |

Transcripts are read incrementally: each file is remembered by size and
modification time, and only the bytes appended since last time are parsed.

## Privacy

**Everything stays on your machine.** The widget has no telemetry, no analytics,
and no update check, and by default it makes no network calls at all.

There are exactly three exceptions, and you have to switch each of them on. The
first is [Telegram notifications](#telegram-optional), which POST one line of
text — the same line Notification Center shows — to your own bot. The second is
the [live per-account Codex quota](#optional-a-live-row-for-each-of-your-codex-accounts), which
reads `https://chatgpt.com/backend-api/wham/usage` — your own account, with your
own login, for your own quota numbers. The third is
[token refresh](#refreshing-tokens), which POSTs one OAuth
refresh grant to `https://auth.openai.com/oauth/token` for a login of yours that
is about to expire. Nothing is sent anywhere else, nothing is
uploaded, and the request contains no transcript content. Your Codex access
token is read from its file, used in that one request's `Authorization` header,
and dropped: it is never logged, never written to any file the widget creates,
and never shown in the menu. A credential file that other users can read is
refused rather than used. There is a test that plants a canary token, runs a
full poll cycle and asserts the canary reaches no file, no log line and no menu
label — and a companion test proving that check can actually fail.

Your transcripts contain your source code and possibly your secrets. Records are
parsed as JSON — so content passes through memory, as it must for any parser —
but the indexers **extract only** token counts, the model name and a timestamp.
No prompt, completion, file content or tool output is ever stored, aggregated,
transmitted, or written to any file — there is a test that plants a canary secret in a
fixture and asserts it appears in none of the files the widget writes.

State files are created `0600` in the install directory:

| File | Contains |
|---|---|
| `rollups.json` | per-day, per-model token counters |
| `scan_state.json`, `codex_scan_state.json` | absolute path, size, offset per transcript |
| `codex_scan_state_quota.json` | your most recent Codex subscription quota |
| `scan_state_dedup.json` | request IDs seen today, for de-duplication |
| `codex_accounts.json` | *(live Codex quota only)* account ids, your aliases, order — no token |
| `codex_quota_snapshots.json` | *(live Codex quota only)* the last quota reading per account — no token |
| `codex-accounts/<id>/auth.json` | *(live Codex quota only)* one Codex login, written by `codex login` — and by the widget when [token refresh](#refreshing-tokens) rotates it (on by default; Settings → Refresh Codex logins automatically turns it off) |
| `notify.json` | *(Telegram notifications only)* your bot token and chat id — the one credential the widget writes |
| `notify_state.json` | which transitions have already been announced — no figures, no names but your own aliases |
| `history.sqlite` | the long-term copy of `rollups.json` — same counters, kept past the window |
| `dashboard.html` | *(dashboard only)* the page **Open dashboard** last rendered — the same counters, as charts |
| `backup-state-<ts>/` | a copy of the two files above, taken before a **Rebuild cost index** |
| `settings.json` | your preferences |
| `logs/widget.log` | only if you use `--launch-agent`; trimmed to its newest half once a day past 5 MB, and a warning that repeats within an hour is written once, then as `(repeated N times since HH:MM)` |

If [claude-swap](https://github.com/realiti4/claude-swap) is installed, *it*
talks to Anthropic to read your own account quotas. That is its network activity,
on your behalf, not ours.

## About the dollar figures

They are **notional**: what your usage would cost at published API list prices.
You are on a flat-rate subscription, so this is **not a bill** — it is a
burn-rate signal, and a decent answer to "is this subscription worth it".

Prices come only from vendors' published tables and are dated, so a rate change
applies from the day it took effect. **A model we don't have a published price
for shows its token count at `$0` and is named in the menu.** Prices are never
guessed — if you see an unpriced model, please
[open an issue](../../issues/new?template=unpriced-model.yml) with a link to the
published rate.

Priced today (Anthropic's table re-verified 2026-09-09, with Opus 5.5 and
Fable 5.1 read 2026-09-23; OpenAI's re-read 2026-09-25): Anthropic's Fable 5, Fable 5.1, Mythos 5, Opus 5, Opus 5.5, Opus 4.8, Sonnet 5,
Sonnet 4.6 and Haiku 4.5; OpenAI's `gpt-6-astra` (the current Codex flagship),
`gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol` / `-terra` / `-luna`, `gpt-5.5`,
`gpt-5.4` and `gpt-5.4-mini`, with cache writes at OpenAI's published "Cache
writes" rate where it prints one. `codex-auto-review` has no published rate
and stays at `$0` by design; any other unpriced model with heavy use in a day
[notifies you](#notifications) once.

## History and export

`rollups.json` only keeps `lookback_days` (30 by default), and Claude Code
prunes `~/.claude/projects` on its own schedule, so once a day ages out there is
nothing left to rebuild it from.

The widget therefore keeps a second copy in `history.sqlite`, in the install
directory, `0600`. It is a mirror of the same per-day, per-model token counters
— no prompt, path or session text, the same as everything else the widget writes
— and it is **append-only**: days are added and corrected, never removed. A
rebuild or a repair that lowers a figure is copied across, so the long-term
record cannot preserve a number the live store has since fixed.

Under **Cost** in the menu, once the mirror has actually recorded something:

* **Export usage (CSV)…**
* **Export usage (JSON)…**

Both write `~/Downloads/usage-bar-<today>.csv` / `.json` and reveal the file in
Finder. Before the first scan has finished — and on a machine where history is
off — the two items are replaced by a single `! history: nothing recorded yet`
line, because a file with a header and no rows is not an empty answer: read back
a week later it is the claim that nothing was spent. Each row is one day × one
model:

| Column | Meaning |
|---|---|
| `day`, `vendor`, `model` | the cell |
| `input`, `output`, `cache_read`, `cache_write_5m`, `cache_write_1h`, `total_tokens` | the counters |
| `usd_at_record_notional` | what that day cost at the rates in effect **on that day** |
| `usd_at_today_rates_notional` | the same tokens repriced at **today's** table |

Two dollar columns, because a price change should read as a difference rather
than quietly rewrite your past. Both are [notional](#about-the-dollar-figures).

To switch the mirror off, set `"history_enabled": false` in `settings.json`; the
file is then never opened or created, and the Cost section goes back to exactly
what it drew before this feature existed — no export items, and no line about a
mirror you deliberately turned off.

## The dashboard

**Cost → Open dashboard** writes one HTML file to the install directory
(`dashboard.html`, `0600`) and opens it in your browser. It is regenerated on
every open, so it is a snapshot of what the stores held at that moment, with the
timestamp printed at the top.

It answers the questions a menu cannot: how the last 30 and 90 days compare, how
tokens and notional dollars split by vendor and by model, how each vendor's model
mix moved day by day (this is where "Astra took over from Sol" is visible), which
windows each account is currently sitting in, and how much volume is unpriced.

Workflow swarms count toward the session that ran them. When one ran today, the
menu's Cost section adds `today's top workflow runs` (`wf_<id> · <project>`,
tokens, notional $; behind **Settings ▸ Cost by project**), and the dashboard's
`Workflow runs` table splits today's and yesterday's runs by phase, model and
dearest agent. Phase and agent names are read from the run's own
`journal.jsonl` and never stored.

Everything about the file is deliberate:

* **It loads nothing.** Inline CSS, inline SVG, no `<script>`, no CDN, no
  network of any kind. It is a record of your spend, so it does not announce
  itself to anyone, and it will still render years from now with no library
  available.
* **Light and dark**, following the browser via `prefers-color-scheme`.
* **Every number is a measurement.** Compact figures (`1.2M`) sit beside their
  exact integers so the page reconciles against the menu, unpriced models are
  named with their token magnitude and never given a dollar amount, and an empty
  store renders an honest empty state rather than sample data.
* **Days inside the 30-day window come from the live store; older days come from
  `history.sqlite`.** Where both hold a day the live store wins, because a
  rebuild or an audit repair lands there first.
* **What is not measured is not drawn.** Nothing in the widget stores a quota
  time series, so the "hours at the wall" panel reports the wall each account is
  at right now and says why a weekly total is not derivable, instead of
  estimating one.

To switch it off, set `"dashboard_enabled": false` in `settings.json`: the menu
item disappears and no `dashboard.html` is ever written or opened.

### Rebuilding, and undoing a rebuild

**Settings ▸ Rebuild cost index** re-reads every transcript from the start. It is
the right move after a corrupt scan state or a suspicious total — but it is not
free: a day whose transcripts Claude Code or Codex has since pruned cannot come
back, so a rebuild can legitimately produce *less* than it destroyed.

So it snapshots first. Before anything is cleared, `rollups.json`, the scan
states and the dedup sidecar are copied into `backup-state-<timestamp>/` in the
install directory, and the confirmation dialog names that directory before you
agree to anything. If the copy cannot be written, the rebuild does not run.

**Settings ▸ Restore last backup…** puts them back. Indexing then stops until you
relaunch the widget: a scanner reads its offsets once, so a running process
holds the *post*-rebuild offsets while the files on disk are the *pre*-rebuild
ones — scanning on would double-count and saving would undo the restore. The
menu says so in the diagnostics block, the restored figures are shown, and a
relaunch picks everything up normally. Backups are never deleted by the widget.

## Troubleshooting

<details>
<summary><b>No icon appeared in my menu bar</b></summary>

The most common first-run problem, and usually not a crash — check
`ps aux | grep cc_usage_widget` first.

macOS assigns menu bar slots by a stored per-app position. On a **full menu bar**
(very likely on a notched MacBook), an item with no stored position silently
loses arbitration and is never drawn — no error anywhere. The widget seeds its own
position to avoid this, but if the bar is genuinely full something has to give.

Fixes, in order:
1. Quit a menu bar app you don't need and restart the widget.
2. Shrink the widget: **Settings → title** — turning the cost text off leaves an
   icon-only item roughly a third of the width.
3. Confirm it is otherwise healthy: `./run.sh --dry-run` prints the composed menu
   and every path it uses, without touching the menu bar.
</details>

<details>
<summary><b>The accounts section says claude-swap is not installed</b></summary>

Expected — account bars, switching and auto-switch come from
[claude-swap](https://github.com/realiti4/claude-swap). Cost tracking and the
Codex section work fine without it.
</details>

<details>
<summary><b>Cost says "indexing…"</b></summary>

The first scan is still running. It shows `indexing…` rather than a partial
number that would look like a real total. Large corpora take under a minute.
</details>

<details>
<summary><b>install.sh says Python 3.12+ is required</b></summary>

macOS ships Python 3.9, which cannot run this. Install
[uv](https://docs.astral.sh/uv/) (it brings its own Python) and re-run
`./install.sh`.
</details>

## Development

```bash
python tests/test_cost_math.py     # Claude cost math
python tests/test_codex.py         # Codex extraction
python tests/test_privacy.py       # the canary: no transcript content escapes
python tests/test_regressions.py   # everything previously broken
python tests/test_statebackup.py   # the backup taken before a rebuild
```

Each file is its own runner (there is no test framework to install) and exits
non-zero on a failure. `.github/workflows/tests.yml` runs **every**
`tests/test_*.py` on a macOS runner that has neither `~/.claude` nor `~/.codex`
— which is the point: most of the guarantees below are claims about a machine
with no corpus, and only a machine with no corpus can check them.

The tests are the interesting part of this repository. AI transcript accounting
is full of traps that produce a *plausible wrong number* rather than an error —
cumulative counters that reset mid-session, cached tokens that are a subset of
input rather than an addition, a model attribution that has to survive a scan
resuming mid-file. Each has a test that fails on the naive implementation.

Design notes: [docs/SPEC.md](docs/SPEC.md), [docs/SPEC-CODEX.md](docs/SPEC-CODEX.md).

## Credits

Account features build on [claude-swap](https://github.com/realiti4/claude-swap)
by [@realiti4](https://github.com/realiti4) — a separate project, gratefully used.

## Disclaimer

Not affiliated with, endorsed by, or sponsored by Anthropic or OpenAI. "Claude",
"Claude Code", "Codex" and "ChatGPT" are their respective owners' marks, used
only to describe what this reads. Pricing is reproduced from public pages and may
be out of date — check the vendor's own page before making decisions about money.

MIT licensed.
