#!/usr/bin/env bash
# cc-usage-widget installer — idempotent, nothing global, macOS only.
#
#   ./install.sh                  set up the venv + run.sh
#   ./install.sh --launch-agent   also write a start-at-login LaunchAgent
#
# Env overrides (mostly for testing):
#   CC_WIDGET_VENV   where the venv lives   (default: <here>/.venv, or claude-swap's)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CSWAP_PY="$HOME/.local/share/uv/tools/claude-swap/bin/python"
VENV="${CC_WIDGET_VENV:-$HERE/.venv}"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || fail "macOS only (this is a menu bar app)"

# ── 1. Pick a Python that has (or can get) rumps + pyobjc ────────────────────
PYTHON=""
if [ -x "$CSWAP_PY" ] && "$CSWAP_PY" -c "import rumps" >/dev/null 2>&1; then
    PYTHON="$CSWAP_PY"
    say "using claude-swap's venv (rumps already present; account features enabled)"
elif [ -x "$VENV/bin/python" ] \
        && "$VENV/bin/python" -c "import rumps" >/dev/null 2>&1 \
        && "$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    # The version check matters as much as the rumps check: a 3.9 venv built by
    # an older installer would otherwise be adopted forever, since the pin above
    # only guards CREATION.
    PYTHON="$VENV/bin/python"
    say "reusing existing venv at $VENV"
else
    say "creating venv at $VENV (one-time; installs rumps + pyobjc locally, nothing global)"
    if command -v uv >/dev/null 2>&1; then
        # Pin the interpreter: bare `uv venv` uses whatever it finds first,
        # which on a Mac with Xcode CLT can be a Python that cannot build or
        # load pyobjc, producing a venv that installs fine and then fails at
        # import time. 3.12 is the floor the package needs.
        # No `||` fallback: a bare `uv venv` picks whatever it finds first,
        # which on a stock Mac is Apple's 3.9 — it byte-compiles fine and then
        # dies at import on `typing.Self`. Failing loudly beats a venv that
        # installs cleanly and never runs.
        uv venv --quiet --python 3.12 "$VENV" \
            || fail "uv could not provide Python >= 3.12 (try: uv python install 3.12)"
        uv pip install --quiet --python "$VENV/bin/python" rumps
    else
        command -v python3 >/dev/null 2>&1 || fail "need uv or python3 — https://docs.astral.sh/uv/"
        python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
            || fail "python3 >= 3.12 required (or install uv, which brings its own)"
        python3 -m venv "$VENV"
        "$VENV/bin/pip" install --quiet --upgrade pip rumps
    fi
    PYTHON="$VENV/bin/python"
fi

"$PYTHON" -c "import rumps, AppKit" >/dev/null 2>&1 || fail "rumps/pyobjc failed to install"

# ── 2. Byte-compile as a sanity check (catches a corrupt download) ───────────
"$PYTHON" -m compileall -q "$HERE/cc_usage_widget" || fail "package failed to compile"

# ── 3. Launcher ──────────────────────────────────────────────────────────────
cat > "$HERE/run.sh" <<EOF
#!/usr/bin/env bash
# Launch cc-usage-widget (foreground; Ctrl-C or menu Quit to stop).
cd "$HERE" && exec "$PYTHON" -m cc_usage_widget "\$@"
EOF
chmod +x "$HERE/run.sh"

# ── 4. Optional start-at-login ───────────────────────────────────────────────
if [ "${1:-}" = "--launch-agent" ]; then
    PLIST="$HOME/Library/LaunchAgents/com.cc-usage-widget.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$HERE/logs"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.cc-usage-widget</string>
  <key>ProgramArguments</key><array>
    <string>$HERE/run.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HERE/logs/widget.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/widget.log</string>
</dict></plist>
EOF
    say "LaunchAgent written: $PLIST"
    say "activate it now with:"
    echo "    launchctl bootstrap gui/\$(id -u) \"$PLIST\""
fi

say ""
say "installed. start it:   $HERE/run.sh"
say "then look for the bar-chart icon in your menu bar and click it."
