#!/usr/bin/env bash
# Build dist/cc-usage-widget-<version>.zip from an explicit ALLOWLIST.
#
# Why an allowlist and not a denylist: WIDGET_HOME defaults to this directory,
# so a live install accumulates its own runtime state right here -
#
#   rollups.json                    every day's token counters
#   scan_state.json                 ~860 KB, one entry per transcript
#   scan_state_dedup.json[.tmp.*]   the current day's request ids
#   codex_scan_state.json           ~250 KB, the absolute path of every
#                                   in-window Codex session (i.e. the names of
#                                   your projects and worktrees)
#   codex_scan_state_quota.json     your ChatGPT subscription used_percent,
#                                   plan_type and reset time
#   settings.json, widget.lock
#
# - and a hand-built zip from this directory ships all of it. The pre-Codex
# dist zip omitted them only because it was built before those files existed;
# the exclusion was manual, undocumented, and already incomplete. Listing what
# goes IN cannot rot the same way: a new state file is excluded by default, and
# a new source file that is genuinely missing fails the check below loudly.
#
# Usage:  ./package.sh [version]        (default: 1.0.0)

set -euo pipefail

cd "$(dirname "$0")"
VERSION="${1:-1.0.0}"
NAME="cc-usage-widget-${VERSION}"
OUT="dist/${NAME}.zip"

# --- the allowlist ---------------------------------------------------------
FILES=(
  cc_usage_widget/__init__.py
  cc_usage_widget/__main__.py
  cc_usage_widget/accounts.py
  cc_usage_widget/app.py
  cc_usage_widget/codex_indexer.py
  cc_usage_widget/contracts.py
  cc_usage_widget/indexer.py
  cc_usage_widget/pricing.py
  cc_usage_widget/render.py
  cc_usage_widget/rollup.py
  cc_usage_widget/state.py
  tests/test_codex.py
  tests/test_cost_math.py
  tests/test_regressions.py
  SPEC.md
  SPEC-CODEX.md
  README.md
  CHANGELOG.md
  LICENSE
  install.sh
  uninstall.sh
  package.sh
)

# Every .py under cc_usage_widget/ must be listed, or a new module ships broken.
missing=0
while IFS= read -r found; do
  case " ${FILES[*]} " in
    *" ${found} "*) ;;
    *) echo "NOT IN THE ALLOWLIST: ${found}" >&2; missing=1 ;;
  esac
done < <(find cc_usage_widget tests -name '*.py' -not -path '*__pycache__*' | sort)
[ "$missing" -eq 0 ] || { echo "refusing to package an incomplete tree" >&2; exit 1; }

for file in "${FILES[@]}"; do
  [ -f "$file" ] || { echo "allowlisted but absent: ${file}" >&2; exit 1; }
done

# The archive must contain ONE top-level directory. Zipping from *inside* the
# staging dir (`cd dist/$NAME && zip -r ../x.zip .`) flattens it, so `unzip`
# sprays 22 entries into whatever directory the user happened to be in -- their
# Downloads folder -- and the README's own first line, `cd cc-usage-widget`,
# exits 1. The folder is deliberately UNversioned so that one README instruction
# stays correct for every release.
TOPDIR="cc-usage-widget"
rm -rf "dist/${TOPDIR}" "$OUT"
mkdir -p "dist/${TOPDIR}"
for file in "${FILES[@]}"; do
  mkdir -p "dist/${TOPDIR}/$(dirname "$file")"
  cp "$file" "dist/${TOPDIR}/${file}"
done

(cd dist && zip -q -r "${NAME}.zip" "${TOPDIR}")
rm -rf "dist/${TOPDIR}"

echo "wrote ${OUT}"
LISTING="$(unzip -l "$OUT")"
tail -n +4 <<<"$LISTING"

# Fail loudly if any runtime-state file made it in anyway (Rule 12).
#
# Read from $LISTING with a here-string, never `unzip -l | grep -q`: under
# `set -o pipefail` grep -q exits on the first match, unzip dies of SIGPIPE, the
# PIPELINE status is therefore non-zero, and the `if` concludes "no leak" for
# exactly the input it was supposed to catch. Verified: the pipe form let a
# deliberately allowlisted settings.json through with exit 0.
if grep -E 'rollups\.json|scan_state|settings\.json|widget\.lock' \
    <<<"$LISTING" >/dev/null; then
  echo "PRIVATE RUNTIME STATE LEAKED INTO ${OUT}" >&2
  exit 1
fi
