# cc-usage-widget

A macOS menu bar widget for **Claude Code power users**: live account quota bars,
rate-limit reset times, and what your usage *would cost* at API list prices —
computed locally from your own session transcripts.

```
📊 $42/d          ← menu bar: today's notional burn rate
```

Click it:

```
1  main (you@work.com)
   5h    ░░░░░░░░░░░░░░░░░░   0%
   7d    █████████████▍░░░░  74%     resets Aug 21 16:00  (ahead of pace)
   Fable ██████████████████ 100%  (!)  resets Aug 21 16:00  (ahead of pace)

3  personal (you@gmail.com)   ● active
   5h    ███████████░░░░░░░  61%     resets 14:50
   ...

Cost (notional, API list prices)
  Today        $41.98
  Last 7d    $310.20   ($44.31/day avg)
  Last 30d  $1,284.55
  Opus 5       61.0M tok    $31.10
  Sonnet 5     44.2M tok    $10.88
```

## What it does

- **Cost tracking (works for every Claude Code user).** Incrementally scans
  `~/.claude/projects/**/*.jsonl`, prices the token counts at Anthropic's
  published API list prices (dated — e.g. Sonnet 5's intro rate rolls over
  automatically on 2026-09-01), and shows today / 7d / 30d, per model.
  *Notional*: it is what your usage would cost on the API, not your bill.
- **Account quota + auto-switch (needs [claude-swap](https://github.com/realiti4/claude-swap)).**
  If `cswap` manages your accounts, the widget shows every account's 5h / weekly /
  per-model windows with reset times and pace, lets you switch with a click, and
  can run claude-swap's auto-switch engine (top-level ON/OFF toggle). Without
  claude-swap the accounts section simply says so; everything else works.

## What it costs your machine

Measured against 1.4 GB / ~3,300 Claude transcripts **plus** ~15 GB / ~3,000 Codex
rollouts on one machine, not estimated:

- **~0.0% CPU idle** — a steady tick opens **zero** files; it is ~100% directory
  walk. With two corpora the walks alternate (one tree per tick, tick fires twice
  as often), so a tick costs 18.7–22.8 ms rather than 30.1 ms combined
- **~45–49 MB resident** in steady state and right after a first index
- **Peak allocation 7.9 MB** during a full Claude first index (streaming, never
  `read()`-ing a file or a line whole)
- Honest caveat: `ru_maxrss` — the never-decreasing *high-water mark*, not what
  `ps` shows — touches ~72 MB while the first index runs, on the Claude side,
  from allocator pages macOS does not hand back. It settles immediately; the
  instantaneous figure never goes near it
- First index: ~5 s for 1.4 GB of Claude, ~20 s for 12.9 GB of in-window Codex,
  both chunked in the background — the menu never blocks

## Install

Requirements: macOS 13+, and either [uv](https://docs.astral.sh/uv/) or Python 3.12+.

```bash
unzip cc-usage-widget.zip && cd cc-usage-widget
./install.sh          # sets up a venv (or reuses claude-swap's), installs nothing globally
./run.sh              # look for the 📊 icon
```

Start at login (optional):

```bash
./install.sh --launch-agent    # writes ~/Library/LaunchAgents plist + prints the load command
```

Uninstall: `./uninstall.sh` (removes the venv, LaunchAgent, and state; your
transcripts are never touched).

## Privacy

Everything runs and stays **on your machine**. The widget reads your local
transcripts and (via claude-swap, if installed) Anthropic's usage endpoint for
your own accounts. It makes no other network calls and phones nothing home.
`~/.codex` is opened read-only; nothing here ever writes under it.

It writes its own state files next to itself. **These are private — do not
copy, zip or commit them:**

| file | contains |
|---|---|
| `rollups.json` | your per-day, per-model token counters |
| `scan_state.json`, `scan_state_dedup.json` | one entry per transcript |
| `codex_scan_state.json` | the absolute path of every in-window Codex session — i.e. your project and worktree names |
| `codex_scan_state_quota.json` | your ChatGPT subscription `used_percent`, plan type and reset time |
| `settings.json`, `widget.lock` | preferences and the single-instance lock |

`./package.sh` builds a shareable zip from an explicit **allowlist** of source
files, so none of the above can be swept in by accident; it fails loudly if any
of them ever appears in the archive.

## Notes & honest limits

- Numbers are near-real-time: quota ≤ ~3 min old (staleness is shown on the
  row), cost ≤ 5 min. Intervals are tunable in `settings.json`.
- On a **notched Mac with a full menu bar**, macOS silently refuses to draw new
  items. The widget self-heals the common cause (it seeds its
  `NSStatusItem Preferred Position` when missing), but if the bar is truly full
  something else may lose its slot — the widget is ~87 pt wide with the cost in
  the title, ~33 pt icon-only (Settings → title toggles).
- Don't run this **and** `cswap menubar` together — they share the autoswitch
  state and would double-poll. The widget detects and warns about this.
- Unknown models are shown with their token counts and $0 rather than guessed
  prices; incomplete indexes say `indexing…` instead of showing a low number.

## Layout

```
cc_usage_widget/   the app (pure Python; rumps + pyobjc for the menu bar)
tests/             51 tests — cost math traps, Codex traps, threading, regressions
SPEC.md            design document (performance budget, correctness traps)
SPEC-CODEX.md      the Codex (OpenAI) addendum: its three traps and its pricing
install.sh         idempotent installer
run.sh             foreground launcher (written by install.sh)
package.sh         allowlist-based release zip (never ships runtime state)
uninstall.sh       clean removal
```

Run the tests with the same interpreter the widget uses (pytest optional — each
file is its own runner):

```bash
for t in tests/*.py; do python "$t"; done
```

MIT licensed. Built with Claude Code.
