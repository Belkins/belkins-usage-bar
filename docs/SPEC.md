# cc-usage-widget — Specification

A macOS menu bar widget showing Claude Code **account quota** and **per-model token cost**,
with first-class on/off switches. Replaces the upstream `cswap menubar` (do not run both).

**Status:** implemented and shipped. Kept as the design record — the performance
budget and the correctness traps below are what the tests enforce.
**Owner:** the user running it
**Date:** 2026-08-17

---

## 1. Why this exists

`cswap menubar` (upstream claude-swap) already shows account quota windows and can run the
autoswitch engine. It cannot show **cost**, because cost is not in the usage API:

- `GET /api/oauth/usage` returns `spend` = *overage credits only* (`$0`, `enabled: false` on
  these accounts). There is no subscription-cost figure to read.
- Per-model **token counts** do exist — in the session transcripts under
  `~/.claude/projects/**/*.jsonl`, as `message.usage` with `message.model`.

So cost must be computed from transcripts. That is the only new data source; everything
account-related is reused from `claude_swap`, not reimplemented.

There is also no separate **Opus rate-limit window**: all three accounts report only a
`Fable` scoped weekly window (`seven_day_opus` is `null`). Opus consumption is therefore shown
as **tokens + cost** (from transcripts), never as a quota percentage. Do not invent an Opus %.

---

## 2. Hard constraints

### 2.1 Performance budget (non-negotiable — this is the primary design driver)

Measured against a large real corpus: **~1.4 GB across ~3,200 `.jsonl` files**, avg line
~4.2 KB, with only a small fraction of files changing per hour and 39% of lines
containing `"usage"`. The ratios are what the design depends on, not the volume.

| Budget | Target | How it is met |
|---|---|---|
| Steady-state cost tick | **< 30 ms CPU** | mtime+size pre-filter → only ~40 files, only appended bytes |
| Idle CPU (time-averaged) | **< 0.3%** | 60 s UI tick, 300 s cost tick, early-exit when nothing changed |
| Resident memory (RSS) | **< 70 MB** | rumps+pyobjc baseline is ~35–45 MB; our state is bounded (see 2.2) |
| First-run index | background, **never blocks UI** | bounded by lookback window, chunked, yields between files |
| Peak allocation during scan | **< 10 MB** | stream line-by-line; never `read()` a whole file |

### 2.2 Memory rules

- **Never** `f.read()` or `f.readlines()` a transcript. Always `seek(offset)` then iterate.
- No accumulating lists of parsed records. Running counters only.
- The daily rollup is the only persistent aggregate: `{date: {model: {5 counters}}}`.
  30 days × 6 models × 5 ints ≈ a few KB.
- Dedup set holds **only the current local day's** request IDs, and is dropped at day rollover.

### 2.3 Threading

`rumps` runs an AppKit main loop. **All I/O and parsing happens on a background thread.**
UI mutation is marshalled back to the main thread. A blocked main thread freezes the menu bar
and is treated as a P0 bug.

---

## 3. Architecture

Single process, reusing claude-swap's already-installed venv
(`~/.local/share/uv/tools/claude-swap/bin/python` — has `rumps`, `pyobjc`, `claude_swap`).
No new virtualenv, no new dependencies.

```
cc_usage_widget/
  __main__.py       entry point; builds the app, starts timers
  app.py            rumps.App subclass: title rendering, menu build, toggles
  accounts.py       thin adapter over claude_swap (accounts, usage windows, autoswitch)
  indexer.py        incremental transcript scanner  ← the performance-critical module
  rollup.py         daily aggregate store + cost math
  pricing.py        model price table with effective dates
  state.py          atomic JSON persistence for scan state + settings
  settings.json     user prefs (created on first run)
```

### 3.1 `accounts.py` — reuse, do not reimplement

Wraps `claude_swap`:

- accounts + active account + per-window usage → `switcher.accounts_snapshot()`
  (backed by claude-swap's paced usage store — **no additional API calls**)
- autoswitch → `claude_swap.autoswitch.AutoSwitchEngine`, sharing
  `~/.claude-swap-backup/autoswitch_state.json` and the `autoswitch.*` settings
- switching → `claude_swap.switcher`

Consequence: our autoswitch toggle and `cswap config set autoswitch.*` stay one source of truth.

### 3.2 `indexer.py` — incremental transcript scanner

This is where the performance budget is won or lost.

**Scan state** (persisted, one entry per file):

```json
{"<abs path>": {"inode": 12345, "size": 7364512, "mtime": 1786956130.1, "offset": 7364512}}
```

**Per-tick algorithm:**

1. `os.scandir` the project tree (cheap `stat` from the dirent, no extra syscall per file).
2. Skip any file whose `(size, mtime)` equals the stored pair → **the common case; 3,162 of
   ~3,200 files exit here.**
3. Skip any file with `mtime` older than the lookback window (default 30 days).
4. Truncation / rotation guard: if `size < stored offset` **or** `inode` changed, reset
   `offset = 0` for that file and re-read it whole.
5. Open, `seek(offset)`, iterate lines. **Discard a trailing partial line** (a session actively
   being written can leave an incomplete final line) — do not advance the offset past it.
6. Per line: `if '"usage"' not in line: continue` before `json.loads`. Skips ~61% of parses.
7. Extract, accumulate into the daily rollup, update `offset` to the last complete line.
8. Write scan state atomically (temp file + `os.replace`).

**First run:** no state → every in-window file is read once. Runs on the background thread in
chunks, publishing partial results as it goes, with a menu line showing progress
(`indexing… 1,204/3,200`). The UI shows accounts immediately; cost appears as it fills in.
The lookback window is what keeps this bounded — do **not** index all time by default.

### 3.3 Extraction — correctness traps

From each record with `message.usage`, read `message.model` and:

| Counter | Source field |
|---|---|
| `input` | `usage.input_tokens` |
| `output` | `usage.output_tokens` |
| `cache_write_5m` | `usage.cache_creation.ephemeral_5m_input_tokens` |
| `cache_write_1h` | `usage.cache_creation.ephemeral_1h_input_tokens` |
| `cache_read` | `usage.cache_read_input_tokens` |

**Traps that must be handled — each needs a test:**

1. **`usage.iterations` is a per-attempt breakdown of the same request.** Summing it *and* the
   top-level fields double-counts. Use top-level only.
2. **`usage.cache_creation_input_tokens` is the sum** of the 5m and 1h sub-fields. Adding the
   flat field *and* the split fields double-counts cache writes. Use the split; fall back to
   the flat field as 5m only when `cache_creation` is absent.
3. **Dedup.** Key on `requestId` (else message `id`) for the current local day; a resumed or
   copied session can repeat a record. Per-file offsets prevent re-reading, but not duplicates
   across files.
4. **Day bucketing uses local time**, so "today" matches what the user sees.
5. **Unknown model string** → bucket as `unknown`, count tokens, price at `$0`, and surface the
   unknown name in the menu. Never silently price an unrecognized model at another model's rate.
6. **Missing/partial `usage`** → skip the record; never assume 0 and never crash.

### 3.4 `pricing.py` — dated price table

Per million tokens. Cache rates are **derived** from base input price by the documented
multipliers — write 5m `1.25×`, write 1h `2.0×`, read `0.1×` — so only base rates are stored.

```
model            input   output   effective_from   effective_until
claude-fable-5   10.00    50.00   —                —
claude-opus-5     5.00    25.00   —                —
claude-sonnet-5   2.00    10.00   —                2026-08-31   # intro
claude-sonnet-5   3.00    15.00   2026-09-01       —            # standard
claude-haiku-4-5  1.00     5.00   —                —
```

**Sonnet 5's introductory rate expires 2026-08-31** — 14 days from this spec's date. The table
must resolve a price *by the date of the usage record*, not by today, so historical days stay
correct after the rollover. A hardcoded constant is a bug.

Costs are **notional**: these are API list prices, and the account is a flat-rate Max
subscription. The UI must label it as such (see 4.3).

### 3.5 Cadence

| Job | Interval | Skip condition |
|---|---|---|
| Title + account refresh | 60 s | — (reads claude-swap's usage store; no network of our own) |
| Cost scan | 300 s | no file's `(size, mtime)` changed → exit before opening anything |
| Autoswitch evaluation | claude-swap's own cadence | disabled when the toggle is off |

All intervals live in `settings.json`.

---

## 4. UI

### 4.1 Title

Compact, every component individually toggleable:

```
⇄ personal 17% F3% $12/d
```

`⇄` · alias · 5h% · `F` + Fable weekly% · today's notional cost. Keep the icon **stable** —
the user finds the widget by its glyph.

### 4.2 Menu

```
personal (jane@example.com) — active
─────────────────────────────────────
Auto-switch:      ON        ← top-level, one click
Cost tracking:    ON        ← top-level, one click
─────────────────────────────────────
Accounts
  1 main       5h  84%  · 7d 73%  · Fable 100% (!)  resets 10:59
  2 jan       5h  19%  · 7d 33%  · Fable  65%      resets 14:20
▸ 3 personal      5h  17%  · 7d  5%  · Fable   3%      resets 14:50
─────────────────────────────────────
Cost (notional, API list prices)
  Today                     $12.40
  Last 7d                   $86.10   ($12.30/day avg)
  Last 30d                 $291.55
  ── by model ──────────────────────
  Fable 5      41.2M tok    $8.90
  Opus 5       12.8M tok    $2.60
  Sonnet 5      9.1M tok    $0.90
  Haiku 4.5     2.2M tok    $0.00
─────────────────────────────────────
Switch account          ▸
Refresh now
Settings                ▸
Quit
```

The two on/off switches are **top-level**, per explicit user requirement — not inside Settings.

### 4.3 Honesty requirements

- Cost is labeled **"notional, API list prices"** wherever it appears. It is not a bill.
- Reset times come from the API and are shown verbatim, never recomputed by us.
- While the first index is incomplete, cost rows read `indexing… n/N`, **not** a low number
  that looks like a real total.
- A stale usage read shows its age (claude-swap's `usageAgeSeconds`) rather than implying live data.

---

## 5. Install / run

```bash
PY=~/.local/share/uv/tools/claude-swap/bin/python
$PY -m cc_usage_widget          # foreground
```

Uses `NSApplicationActivationPolicyAccessory` so there is no Dock icon or Cmd-Tab entry
(same as upstream). Quit upstream's `cswap menubar` first — two engines sharing
`autoswitch_state.json` would double-poll.

**Location note:** this lives under `~/.claude/`, deliberately **not** under `~/Desktop/`.
A launchd agent running a script from Desktop hits macOS TCC and dies with exit 126.

Auto-start at login is a follow-up (LaunchAgent plist) and is out of scope for v1.

---

## 6. Definition of done

1. `⇄` appears in the menu bar with a correct compact title.
2. Both toggles work and persist across restarts; autoswitch off means the engine does not run.
3. Account rows match `cswap list` exactly (same source of truth).
4. **Measured** steady-state tick < 30 ms CPU and RSS < 70 MB, with the numbers recorded.
5. First index completes in the background without the menu ever hanging.
6. Every trap in 3.3 has a test with a synthetic fixture; the `iterations` and
   `cache_creation` double-count cases in particular.
7. Cost for a hand-built fixture matches a hand-computed figure exactly.
8. Sonnet 5 intro→standard rollover is tested by pricing a record dated after 2026-08-31.
