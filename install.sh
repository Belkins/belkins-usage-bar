#!/usr/bin/env bash
# cc-usage-widget installer — idempotent, nothing global, macOS only.
#
#   ./install.sh                           set up the venv + run.sh
#   ./install.sh --launch-agent            also write a start-at-login LaunchAgent
#   ./install.sh --launch-agent --reload   ...and (re)load it now: bootout the
#                                          running agent, wait, bootstrap again
#
# Env overrides (mostly for testing):
#   CC_WIDGET_VENV   where the venv lives   (default: <here>/.venv, or claude-swap's)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CSWAP_PY="$HOME/.local/share/uv/tools/claude-swap/bin/python"
VENV="${CC_WIDGET_VENV:-$HERE/.venv}"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

LAUNCH_AGENT=0
RELOAD=0
for arg in "$@"; do
    case "$arg" in
        --launch-agent) LAUNCH_AGENT=1 ;;
        --reload) LAUNCH_AGENT=1; RELOAD=1 ;;
        *) fail "unknown option: $arg (see the header of $0)" ;;
    esac
done

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
# The interpreter guard is load-bearing: a uv tool reinstall, python upgrade or
# cache prune can move or delete the venv, and launchd's KeepAlive would then
# respawn a dead exec in a loop. Re-resolve once, then fail loudly.
cat > "$HERE/run.sh" <<EOF
#!/usr/bin/env bash
# Launch cc-usage-widget (foreground; Ctrl-C or menu Quit to stop).
#
# The interpreter can move or vanish (uv tool reinstall, python upgrade,
# cache prune); without the guard below, launchd's KeepAlive would respawn a
# dead exec in a loop. Re-resolve once (claude-swap's uv venv), then fail loudly.
PY="$PYTHON"
if [ ! -x "\$PY" ]; then
  TOOLS_DIR="\$(uv tool dir 2>/dev/null)"
  if [ -n "\$TOOLS_DIR" ] && [ -x "\$TOOLS_DIR/claude-swap/bin/python" ]; then
    PY="\$TOOLS_DIR/claude-swap/bin/python"
    echo "[run.sh] interpreter moved — re-resolved to \$PY" >&2
  else
    echo "[run.sh] FATAL: python not found at $PYTHON (venv removed or moved?). Re-run: $HERE/install.sh" >&2
    exit 1
  fi
fi
cd "$HERE" && exec "\$PY" -m cc_usage_widget "\$@"
EOF
chmod +x "$HERE/run.sh"

# ── 4. Optional start-at-login ───────────────────────────────────────────────
if [ "$LAUNCH_AGENT" = 1 ]; then
    LABEL="com.cc-usage-widget"
    DOMAIN="gui/$(id -u)"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents" "$HERE/logs"
    # ExitTimeOut: launchd's default 5 s SIGKILLs a quit whose 5 s worker grace
    # plus a tick overruns it, mid final flush (tests/test_ops.py pins >= 11).
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
  <key>ExitTimeOut</key><integer>20</integer>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$HERE/logs/widget.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/widget.log</string>
</dict></plist>
EOF
    say "LaunchAgent written: $PLIST"
    # launchd reads a plist at bootstrap only, and refuses to bootstrap a label
    # that is already loaded - so on a running agent the new plist (ExitTimeOut
    # above) never takes effect until it is booted out first (R2-OPS-1). Same
    # on/off semantics as ~/.local/bin/usagebar: bootout, WAIT for the teardown
    # (an immediate bootstrap fails with "5: Input/output error"), bootstrap.
    loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
    if [ "$RELOAD" = 1 ]; then
        if loaded; then
            say "reloading $LABEL so launchd reads the new plist"
            launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                loaded || break
                sleep 1
            done
            if loaded; then
                fail "bootout sent but $LABEL is still tearing down; re-run with --reload"
            fi
        fi
        ERR=""
        for _ in 1 2 3 4 5; do
            ERR="$(launchctl bootstrap "$DOMAIN" "$PLIST" 2>&1)" && break
            loaded && break
            sleep 1
        done
        loaded || fail "could not load $PLIST: ${ERR:-unknown}"
        say "LaunchAgent loaded ($(launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
            | awk '/exit timeout/ {sub(/^[ \t]+/, ""); print; exit}'))"
    elif loaded; then
        say "$LABEL is already loaded and keeps its OLD plist until reloaded:"
        echo "    launchctl bootout $DOMAIN/$LABEL"
        echo "    # wait until 'launchctl print $DOMAIN/$LABEL' fails, then:"
        echo "    launchctl bootstrap $DOMAIN \"$PLIST\""
        say "or let this script do it:   $HERE/install.sh --launch-agent --reload"
    else
        say "activate it now with:"
        echo "    launchctl bootstrap $DOMAIN \"$PLIST\""
    fi
fi

say ""
say "installed. start it:   $HERE/run.sh"
say "then look for the bar-chart icon in your menu bar and click it."
