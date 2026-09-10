<div align="center">

# Belkins Usage Bar

**How much of your AI coding subscriptions have you actually used?**

A macOS menu bar widget for Claude Code and Codex — live quota bars, reset times,
and what your usage would cost at published API rates. Computed entirely on your
own machine.

[![tests](https://img.shields.io/badge/tests-217%20passing-success)](tests/)
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

**Both** — notional per-model cost, computed by scanning your own transcripts.

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
  them**. Two days before one expires the row starts saying `relogin in 1d 4h`;
  when it expires, run `CODEX_HOME=…/codex-accounts/<account_id> codex login`
  again. Refreshing tokens ourselves could log you out of the account you are
  working in, so it is not done until that is proven safe.
* Delete `codex_accounts.json` (or untick every account) and the menu goes back
  to exactly what it was.

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

There is exactly one exception, and you have to switch it on: the [live
per-account Codex quota](#optional-a-live-row-for-each-of-your-codex-accounts)
reads `https://chatgpt.com/backend-api/wham/usage` — your own account, with your
own login, for your own quota numbers. Nothing is sent anywhere else, nothing is
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
| `codex-accounts/<id>/auth.json` | *(live Codex quota only)* one Codex login, written by `codex login`, **never by the widget** |
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
```

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
