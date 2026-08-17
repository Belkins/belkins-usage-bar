#!/usr/bin/env bash
# cc-usage-widget uninstaller. Removes the LaunchAgent, venv, and state files.
# Never touches your transcripts (~/.claude/projects) or claude-swap.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

PLIST="$HOME/Library/LaunchAgents/com.cc-usage-widget.plist"
if [ -f "$PLIST" ]; then
    launchctl bootout "gui/$(id -u)/com.cc-usage-widget" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed LaunchAgent"
fi

# Stop ONLY the instance installed here. `pkill -f "python -m cc_usage_widget"`
# is an argv substring match across the whole machine: it kills a copy running
# from any other directory, which is somebody else's widget, not this install.
# widget.lock carries the owning PID (written under an exclusive flock), so use
# it and verify the PID really is this install before signalling anything.
LOCK="$HERE/widget.lock"
if [ -f "$LOCK" ]; then
    PID="$(tr -dc '0-9' < "$LOCK" 2>/dev/null || true)"
    if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
        # Confirm the PID's argv actually points at THIS directory.
        if ps -o command= -p "$PID" 2>/dev/null | grep -qF "$HERE"; then
            kill "$PID" 2>/dev/null && echo "stopped the widget installed here (pid $PID)"
        else
            echo "widget.lock names pid $PID, but it is not this install - leaving it alone"
        fi
    fi
fi

rm -rf "$HERE/.venv" "$HERE/logs"
rm -f "$HERE"/codex_scan_state.json "$HERE"/codex_scan_state_quota.json \
      "$HERE"/scan_state.json "$HERE"/scan_state_dedup.json \
      "$HERE"/rollups.json "$HERE"/settings.json "$HERE"/widget.lock "$HERE"/run.sh
echo "removed venv and state. Delete this folder to finish: $HERE"
