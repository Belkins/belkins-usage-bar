# Public release plan

Status: **proposed** — nothing published yet. Owner approves before anything ships.
Audited 2026-08-17: no git repo exists, tree is free of identity data and real
spend figures. A clean first commit is achievable, which is the single most
important fact in this document (see §3).

## 1. Name

`cc-usage-widget` reads as Claude-only and it is now dual-vendor.

| Candidate | For | Against |
|---|---|---|
| **`usagebar`** ← recommended | Vendor-neutral, says exactly what it is, short, no trademark surface | Slightly generic |
| `tokenbar` | Memorable | Wrong emphasis — the headline is *quota*, not tokens |
| `cc-usage-widget` (keep) | Zero churn | Actively misleading now; "cc" reads as Claude Code only |

**Recommendation: `usagebar`.** Tagline: *"Menu bar quota and cost for Claude Code
and Codex."* Package dir stays `cc_usage_widget` internally for v1 (renaming the
Python package is churn with no user benefit; do it in v2 if ever).

## 2. Repo structure

```
usagebar/
  README.md            rewritten for strangers (see §6)
  LICENSE              MIT, © Vladyslav Podoliako
  CHANGELOG.md         1.0.0 only
  CONTRIBUTING.md      short: run the 51 tests, don't add a price you can't cite
  .gitignore           see below — LOAD-BEARING
  install.sh uninstall.sh package.sh
  cc_usage_widget/     10 modules
  tests/               3 suites, 51 tests
  docs/SPEC.md docs/SPEC-CODEX.md
  .github/ISSUE_TEMPLATE/{bug,unpriced-model}.yml
```

**`.gitignore` — every line is a leak that would otherwise be permanent:**

```
settings.json
scan_state.json
scan_state_dedup.json
rollups.json
codex_scan_state.json
codex_scan_state_quota.json
widget.lock
logs/
dist/
.venv/
__pycache__/
*.pyc
.DS_Store
```

`codex_scan_state_quota.json` is the one that matters most — it contains the
author's ChatGPT **subscription usage**. `rollups.json` contains 29 days of real
spend. Neither may ever be committed, not once, not in any branch.

**SPECs: publish them**, moved to `docs/`. They contain corpus sizes (1.41 GB /
3,202 files; 15 GB / 2,996 files) which reveal *usage volume* but no identity.
That is credible engineering evidence and the main reason a reader will trust the
performance claims. Judgment: publish. If the owner disagrees, the fix is one
sentence per SPEC ("measured on a ~1.4 GB corpus"), not deleting the documents.

## 3. Safety pass — ordered, do not reorder

The tree is **not yet a git repo**. That is the whole opportunity: there is no
history to scrub, so the only requirement is that the *first* commit is clean.

1. `cd` to a **fresh copy**, not the live working directory — the live one has
   runtime state and a running widget writing to it.
   `rsync -a --exclude-from=.gitignore ~/.claude/cc-usage-widget/ /tmp/usagebar/`
2. `git init` in the copy. **Write `.gitignore` and commit it as the first commit,
   before adding anything else.** A gitignore that lands in commit 2 cannot protect
   commit 1.
3. Verify staging before the first content commit:
   `git add -A && git status --porcelain` — read every line. Any `.json` that is
   not a fixture is a stop.
4. Re-run the audit on the staged tree:
   `git grep -niE "podolyako|@belkins|vladislav|/Users/vladislav" -- . ; echo "exit $?"`
   (exit 1 = clean). Repeat for org UUIDs and real figures.
5. `./package.sh` and audit the zip separately — it has its own allowlist and leak
   guard, and both were negative-tested.
6. Only then create the GitHub repo, **private first**. Push. Re-audit on GitHub's
   own file view. Flip to public as a deliberate separate step.
7. After publishing, `git log -p | grep -niE "<the patterns>"` once. Cheap insurance.

**If any secret ever does land: rotate first, rewrite history second.** History
rewriting on a public repo does not un-publish anything already cloned or cached.

## 4. Attribution and legal

- **claude-swap** (github.com/realiti4/claude-swap) is a separate third-party
  project by another author. README must credit it prominently, state that account
  features *require* it and that it is optional, and link it. Do not vendor it, do
  not imply affiliation.
- **Disclaimer, required:** not affiliated with, endorsed by, or sponsored by
  Anthropic or OpenAI. "Claude", "Claude Code", "Codex", "ChatGPT" are their
  respective owners' marks, used only to say what the tool reads.
- **Prices** are reproduced from published vendor pricing pages, are *notional*,
  and may go stale. README must say so and point at the dated table.
- **Risk framing, stated plainly:** the tool reads local AI session transcripts,
  which can contain source code and secrets. It parses **only** token counts,
  model names and timestamps; it never transmits anything; it makes no network
  calls of its own. Say this explicitly — a reasonable person's first reaction to
  "it reads my transcripts" is alarm, and the answer is good.

## 5. Distribution — lowest friction first

**v1.0: GitHub Release with the zip.** That is it.

```
curl -L <release-url>/usagebar-1.0.0.zip -o usagebar.zip
unzip usagebar.zip && cd usagebar && ./install.sh && ./run.sh
```

**Gatekeeper reality, told honestly in the README:** this is a Python package run
by the user's own interpreter, not a `.app` bundle, so it is not
Gatekeeper-quarantined the way a downloaded app is — but files extracted from a
downloaded zip can still carry `com.apple.quarantine`. If `install.sh` is blocked:
`xattr -dr com.apple.quarantine .` — and the README should explain *why* that
command is needed rather than presenting it as a magic incantation.

Explicitly **not** in v1: a signed `.app` (needs a $99/yr Apple Developer cert and
notarization); a Homebrew tap (worth doing at ~50 users, not at 1); PyPI (this is
an app, not a library).

## 6. What will break for other people

This has only ever run on one machine. Realistic first-user failures, each needing
a README line and an issue-template field:

| Failure | Reality | README must say |
|---|---|---|
| No claude-swap | Accounts section absent | Expected; cost tracking still works |
| No `~/.codex` | No Codex section | Expected; not an error |
| **No menu bar icon** | Known: on a saturated/notched bar macOS silently refuses to draw a position-less `NSStatusItem`. The widget self-heals by seeding its `NSStatusItem Preferred Position`. | The single most likely first-run complaint. Document the symptom, the self-heal, and the icon-only mode (~33 pt) if the bar is full. |
| Big first index | 1.4 GB ≈ 5 s; 15 GB ≈ 21–44 s, background | Cost shows `indexing…`, never a wrong low number |
| Python version | Needs 3.12+, or uv | `install.sh` checks and says so |
| Unpriced model | Vendor ships a new model | Tokens shown, `$0`, name surfaced. **Dedicated issue template**: "unpriced model", asking for the model string and a link to the published rate. Never accept a guessed price. |
| Non-macOS | Hard fail | macOS-only, stated in the first paragraph |

Add `--dry-run` to the README as the first debugging step — it prints wiring,
paths and the composed menu without touching the menu bar.

## 7. Not in v1.0

Codex account switching (no OpenAI equivalent of claude-swap exists) · a third
vendor (the alternate-tick scheduler has exactly one corpus of headroom left) ·
historical price tables (only current published rates are shipped) · any
subscription-dollar figure (a flat fee divided by usage is an invented number) ·
Windows/Linux · telemetry of any kind, ever.
