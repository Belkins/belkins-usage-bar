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

### 3.2a Per-file contribution ledger and the daily self-audit (addendum, 2026-09-10)

`rollups.json` is add-only, so before this addendum any re-read from byte 0 — step 4's
truncation/rotation guard, a lost scan-state entry, a corpus restored from a copy — added that
file's whole history **again**. Live day/model cells were inflated up to **1,650x** and nothing in
the widget noticed for weeks. Two additions close it.

**1. The ledger.** Each scan-state entry gains a sixth, optional key recording what that file has
contributed, per `(day, vendor_model_key)`, in `COUNTER_FIELDS` order:

```json
{"<abs path>": {"inode": 12345, "size": 7364512, "mtime": 1786956130.1, "offset": 7364512,
                "ledger": {"2026-09-09": {"codex:gpt-5.6-sol": [4210, 380, 0, 0, 91000]}}}}
```

* Omitted when empty, so a Claude entry for a transcript with no in-window usage carries no ledger
  bytes (only the two-byte `"lv"` marker below) and shipping this costs no re-index.
* An **append** accumulates into the ledger; a **reset** (step 4) replaces it, and the old ledger
  is handed back on `ScanResult.retractions`. The owner applies `RollupStore.retract_rollups`
  **before** `merge`, so a re-read replaces a contribution instead of adding a second copy.
  Retraction is clamped at zero and every clamp is logged and counted.
* Rows older than the lookback window are dropped on write, which is what bounds the file.
* A **legacy** entry (no ledger) keeps its offset and starts a ledger from its next appended
  bytes. **The ledger cannot retract what it never saw:** such an entry, if its file is later
  replaced, re-adds its history once and is correct from then on. That is deliberate — re-reading
  every 36 MB transcript at upgrade time would be strictly worse.
* Which of the two an **empty** ledger means is carried by a seventh key, `"lv": 1`, written by any
  build that maintains a ledger and inherited across appends: an entry read from byte 0 is
  *complete* even when its ledger is empty (the file contributed nothing); an entry from before the
  ledger is not, and appending to it does not make it so. Nothing else can tell "no usage" from
  "usage this side cannot describe", and both consumers below need to.
* A file that **vanishes** is *not* retracted. Claude Code prunes `~/.claude/projects` on its own
  `cleanupPeriodDays`, so an aged-out transcript's tokens exist only in `rollups.json` and deleting
  them there is permanent. Its entry becomes a **tombstone** (`inode 0 / size 0 / mtime 0`) holding
  the ledger, so a path that ever returns cannot match a real file, reads as a reset, and replaces
  its contribution rather than doubling it. The tombstone goes when its last day ages out.
  A vanished entry that is **not complete** becomes a **legacy tombstone** — kept even with an empty
  ledger, carrying `"legacy_day"` (the file's last-written local day, the honest upper bound on the
  days its records can belong to) and dropped when that day leaves the window. Deleting those
  entries outright is what let the first self-audit read a pruned pre-ledger file's real
  contribution as drift and repair it away.
* **Retraction and re-read are symmetric.** A retraction that the re-read cannot undo is a
  deletion. When a path restarts, the same-day `requestId` dedup map hands back exactly the ids
  *that path* first credited (they are tracked per path, in memory and in the `*_dedup.json`
  sidecar) and the mid-file resume snapshots for that path are dropped. Without it a transcript
  holding TODAY's records was retracted whole and then found every one of its requests already
  credited, so its whole contribution to today silently went to zero.
  The sidecar is **versioned** (`"v": 2`) and an unrecognised version is discarded *whole*, requests
  included: the requests map suppresses a re-read and the owners map is the only thing that can
  un-suppress it, so honouring half the pair costs a file-day of $0, while ignoring it costs at most
  one over-credited in-flight request.
* An entry also carries the attribution **scope** its tokens went into (`"sc"`, roadmap item 6 —
  `vendor\x1fproject\x1fsession`, written only while `cost_by_project_enabled` is on and kept on the
  tombstone). The collector pins a path's scope for the life of the process so a retraction goes
  back where the tokens came from; across a restart there was nothing to pin it to, and the
  retraction was resolved from the file on disk *now* — landing on the new project, which clamps at
  zero, while the old one kept tokens its file no longer holds.

**2. The self-audit** (`audit.py`, `self_audit_enabled`, default on). Once per local day, on the
cost cadence and on a daemon thread of its own, the last **2** days of every corpus are re-indexed
into a `TemporaryDirectory` **using the same indexer classes** (`clone_for_audit`) and compared
with the live store cell by cell. Rules that are not negotiable:

* Never repair from an index that did not finish — a partial twin reads as "the store has far too
  much", and repairing from it would destroy real usage.
* Reconcile with the scanners' tombstones first, or every deleted file looks like drift.
* A cell must differ by **more than 100k tokens AND more than 0.5 %** before it counts. Both gates:
  either alone fires on honest differences, and a `!` line that fires every morning is one nobody
  reads.
* Only the vendors that were actually audited are compared or repaired.
* **Never repair a vendor whose scanner reports a legacy tombstone in the window.** That vendor has
  usage in `rollups.json` that no ledger describes and no re-index can reproduce, so its drift and
  that usage are the same measurement. The note says so (`— NOT rebuilt (pre-ledger history:
  Claude)`) instead.
* **The audit thread computes; the worker applies.** `SelfAudit.run` reads the store and returns a
  plan (`AuditRepair(day, vendor, fresh, observed)`); the cost job applies it at the top of its next
  tick through one `DailyRollupStore.replace_day_for_vendor(day, vendor, models, observed=…)` per
  pair, which holds the store lock across retract *and* re-add. The plan carries what the audit
  observed so the change lands as a **correction** (`current − observed + fresh`): a delta the
  worker merged while the audit was running survives, and it must — the indexer has already advanced
  past the bytes that produced it. Repairing from the daemon thread interleaved two
  retract-then-merge sequences on one store and could lose a whole tick.
* **Everything the audit needs from the live scanners is snapshotted on the worker thread first**
  (`SelfAudit.snapshot_scanners`): `tombstone_rollups`, `legacy_tombstone_days` and `audit_offsets`
  read — and can load from disk — the scan state a scan is rewriting, and all three take the scan
  lock.
* **The twin reads only the bytes the STORE holds.** The snapshot carries each live scanner's
  per-file `(path, inode, offset)` (`audit_offsets`) and `clone_for_audit(…, limits=…)` bounds every
  read to it. Without that bound the twin re-indexed each file to its *current* end while the live
  scanner had consumed only part of it: the un-read tail counted as drift, the repair installed it,
  and the same cost tick merged it again — probed at 10M indexed + 5M appended → 20M, where the
  truth is 15M. A file **absent** from the snapshot is skipped (the live scanner never read it, so
  the store holds nothing of it either). A file whose **inode changed**, that **shrank** below the
  offset, or that has **vanished** since the snapshot is skipped as well and its days are reported
  **not comparable** — the store holds a contribution no index can reproduce, so the difference is
  not drift. Those days are named in the note and never repaired, as is every day of a vendor whose
  twin cannot be bounded at all (a scanner with no `limits` parameter).
* **A plan is only valid against the store it was computed from.** `DailyRollupStore` keeps a
  `generation` counter, bumped by `clear()` and by `bump_generation()` and persisted in
  `rollups.json` under the non-day key `"#generation"` (older builds skip it, as they skip any
  malformed day). `AuditRepair` carries the generation it was computed against and
  `_apply_audit_repairs` refuses any plan that does not match; `Rebuild cost index` and
  `Restore last backup` both drop the outstanding plan and bump the generation. Recognising only an
  *empty* store was not enough: a rebuild stops being empty on its first delta, and a plan landing
  one tick later zeroed the freshly re-indexed day (`max(0, 1M − 50M + 1M)`).
* A repaired day is **replaced** in `history.sqlite` (`HistoryStore.replace_day`, called through
  `getattr`), or the long-term record keeps the figure the store just corrected.
* **A repaired day loses its per-project split.** The plan is per `(day, model)` and says nothing
  about which project the tokens belonged to, so the decomposition cannot be rebuilt from it:
  `AttributionStore.drop_day_for_vendor` and `HistoryStore.drop_project_day` (rows zeroed, never
  deleted — the `replace_day` rule) drop that `(day, vendor)` and the note says
  `— rebuilt (project split for 2026-09-09 reset)`. The next scan re-attributes whatever it reads.

Drift sets `UiSnapshot.audit_note` (`audit: 2 cells drifted (codex 2026-09-09 gpt-5.6-sol 12.7x) —
rebuilt`), rendered in the Cost section; a clean audit says nothing and logs `audit: 0 drift`.
**The tail is a claim about the plan, and only the worker may make it.** The audit thread publishes
`— repair pending`; `_apply_audit_repairs` rewrites it to `— rebuilt` once the plan has landed, and
to `— NOT rebuilt - <reason>` on every exit that does not apply it (the index was rebuilt since, the
store cannot repair a day, every repair failed). A note that claims a rebuild that never happened is
the one line an operator would use to decide the number can be trusted.
The plan and its note change hands under one lock (`_audit_lock`): the swap that takes the plan is a
read followed by a write, and a plan published into the gap was lost with the audit already marked
done for the day.
Settings carries `Self-audit` and `Last audit: 11:40 · 0 drift`, read from a sidecar
(`audit_state.json`, beside `rollups.json`) that also stops a 300 s cadence auditing 288 times a day.
The label is recomputed on the worker every cost tick and cached as a string; the AppKit thread
reads that attribute and never the sidecar. Switching `self_audit_enabled` off clears the note and
any pending plan, so no `!` line outlives the feature.

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
