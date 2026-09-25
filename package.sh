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
#         ./package.sh --dry-run        print the file list and run the checks;
#                                       build nothing

set -euo pipefail

cd "$(dirname "$0")"
DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
  shift
fi
VERSION="${1:-1.0.0}"
NAME="cc-usage-widget-${VERSION}"
OUT="dist/${NAME}.zip"

# --- the allowlist ---------------------------------------------------------
# The code half is DERIVED: every tracked .py in the package and in tests/.
# A hand-kept list went stale (it had 11 of 20 modules - codex_accounts.py,
# codex_login.py, fleet.py and the rest were missing, so the check below
# refused every build) and a new module is the one thing it must never miss.
# Outside a git checkout (an unpacked release) the .py files on disk are the
# list. Runtime state never lives under these two directories.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  CODE="$(git ls-files -- 'cc_usage_widget/*.py' 'tests/*.py')"
else
  CODE="$(find cc_usage_widget tests -name '*.py' -not -path '*__pycache__*' | sort)"
fi
[ -n "$CODE" ] || { echo "no source files found - wrong directory?" >&2; exit 1; }
FILES=()
while IFS= read -r file; do
  FILES+=("$file")
done <<<"$CODE"
# The documents and scripts stay an explicit list. The SPECs sit at the root
# here and under docs/ in the public repository (REPO-PLAN §2); take whichever
# exists, and let the absence check below name the root path when neither does.
for spec in SPEC.md SPEC-CODEX.md; do
  if [ ! -f "$spec" ] && [ -f "docs/$spec" ]; then
    FILES+=("docs/$spec")
  else
    FILES+=("$spec")
  fi
done
FILES+=(
  README.md
  CHANGELOG.md
  CONTRIBUTING.md
  LICENSE
  install.sh
  uninstall.sh
  package.sh
)

# Every .py under cc_usage_widget/ and tests/ must be listed: an untracked new
# module would otherwise ship broken, so it fails here until it is committed.
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

if [ "$DRY_RUN" = 1 ]; then
  printf '%s\n' "${FILES[@]}"
  echo "dry run: ${#FILES[@]} files would go into ${OUT}; nothing written" >&2
  exit 0
fi

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
