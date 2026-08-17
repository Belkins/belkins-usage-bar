# Contributing

## The one hard rule

**Never add a price you cannot cite.** Every rate in `pricing.py` must come from
the vendor's own published pricing page, with the date it took effect. A model
with no citable rate shows its tokens at `$0` and is named in the menu — that is
the correct behaviour, not a gap to paper over with an estimate. A wrong number
shown confidently is worse than an honest blank.

## Before a pull request

```bash
python tests/test_cost_math.py
python tests/test_codex.py
python tests/test_regressions.py
```

All 51 must pass. If you fix a bug, add a test that **fails on the unfixed code**
— verify it does by reverting your fix and watching it go red. Several tests here
exist because a plausible-looking implementation produced a plausible wrong
number; only a test that can actually fail proves the guard works.

## Things worth knowing before changing the indexers

- Never read a whole transcript into memory. Files are scanned incrementally
  from a stored byte offset; a 15 GB corpus is normal.
- All I/O runs on the background worker. Anything on the AppKit main thread
  freezes the menu bar.
- Token accounting is full of traps that yield a *plausible wrong number*:
  cumulative counters that reset mid-session, cached tokens that are a subset of
  input rather than an addition, model attribution that must survive a scan
  resuming mid-file. `docs/SPEC.md` and `docs/SPEC-CODEX.md` document each one
  with the measured evidence.
- Degrade honestly. Unknown model, missing field, absent corpus — show less,
  never guess, never crash.

## Scope

This is a menu bar widget for reading your own local usage. It will not grow
account switching for vendors that have no local tooling for it, invent
subscription-dollar figures, or add telemetry.
