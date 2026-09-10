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
* Codex access tokens last about ten days and the widget **does not refresh
  them by default**. Two days before one expires the row starts saying
  `relogin in 1d 4h`; when it expires, run
  `CODEX_HOME=…/codex-accounts/<account_id> codex login` again. See
  [Refreshing tokens](#refreshing-tokens-off-by-default) for the switch and why
  it ships off.
* Delete `codex_accounts.json` (or untick every account) and the menu goes back
  to exactly what it was.

## Refreshing tokens (off by default)

Ten days is not long, and four accounts means four browser logins every ten
days. The widget can avoid them — it knows how to exchange the refresh token in
each `auth.json` for a fresh one — but the setting that allows it,
`codex_refresh_enabled`, **ships off**, and off means silent: with it off, no
token request of any kind is ever made and the widget only ever *reads* your
credential files. There is a test whose whole job is to fail if that stops
being true.

It ships off because of one unanswered question. Every `codex login` uses the
same public OAuth client, and it is not documented whether rotating the token
for one login invalidates the others (OpenAI, like most providers, may treat a
reused refresh token as theft and revoke the whole family). A wrong guess here
does not show a wrong number — it logs you out of the account you are working
in. So the answer has to be measured on an account you can afford to lose:

```bash
"$PY" -m cc_usage_widget.codex_accounts probe-refresh gmail
```

That performs **one** refresh on that account, writes the rotated tokens into
its `auth.json` before using them, and then makes one usage request to prove
the new token is accepted. It prints the plan, email and expiry before and
after — never a token — and exits non-zero if either half fails. Then wait
**24 hours** and check that your other accounts and `~/.codex` still work
(`list` will tell you). Only if all of that is clean is the switch worth
turning on, by setting `"codex_refresh_enabled": true` in `settings.json`.

With it on, the rules are deliberately narrow:

* a refresh is attempted when a token is within **24 hours** of expiring, and
  exactly **once more** if the API answers `401` — never twice in one poll;
* a `403` never triggers a refresh (that is an answer about permissions, not
  about the token);
* the rotated tokens are written to disk, atomically and `0600`, **before** the
  new access token is used for anything, so a crash costs one grant and never
  your login;
* the old refresh token is overwritten in place — no backup copy, nothing that
  could be re-sent by accident;
* if the API says `invalid_grant`, the row says `relogin` and nothing is
  retried;
* any other failure changes nothing at all: your stored token is still valid
  until it expires, and the countdown carries on as if the refresh had never
  been attempted.

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

Once per transition, not once per tick: a standing condition notifies once and
is only re-armed when it stops being true, so an account that sits at 100 % for
four days produces one notification, and the next time it walls it notifies
again. The ledger is `notify_state.json`; deleting it costs at most one repeat.

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
[token refresh](#refreshing-tokens-off-by-default), which POSTs one OAuth
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
| `codex-accounts/<id>/auth.json` | *(live Codex quota only)* one Codex login, written by `codex login` — and by the widget **only** if you switch [token refresh](#refreshing-tokens-off-by-default) on, which is off by default |
| `notify.json` | *(Telegram notifications only)* your bot token and chat id — the one credential the widget writes |
| `notify_state.json` | which transitions have already been announced — no figures, no names but your own aliases |
| `history.sqlite` | the long-term copy of `rollups.json` — same counters, kept past the window |
| `dashboard.html` | *(dashboard only)* the page **Open dashboard** last rendered — the same counters, as charts |
| `backup-state-<ts>/` | a copy of the two files above, taken before a **Rebuild cost index** |
| `settings.json` | your preferences |
| `logs/widget.log` | only if you use `--launch-agent` |

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

Priced today (table re-verified 2026-09-09): Anthropic's Fable 5, Mythos 5,
Opus 5, Opus 4.8, Sonnet 5, Sonnet 4.6 and Haiku 4.5; OpenAI's `gpt-6-astra`
(the current Codex flagship), `gpt-5.6-sol` / `-terra` / `-luna`, `gpt-5.5`,
`gpt-5.4` and `gpt-5.4-mini`. `codex-auto-review` has no published rate and
stays at `$0` by design.

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
