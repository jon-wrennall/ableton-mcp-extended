#!/bin/bash
# Starts the AbletonParameterBridge when Live is running.
# Waits for Live to launch, starts the bridge, restarts if it exits.
#
# SETUP: Copy this to ~/bin/start-parameter-bridge.sh and update the paths below.
#   mkdir -p ~/bin
#   cp start-bridge.sh ~/bin/start-parameter-bridge.sh
#   chmod +x ~/bin/start-parameter-bridge.sh
#
# Find your npx path with: which npx

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BRIDGE_DIR="$HOME/path/to/AbletonParameterBridge"   # ← update this
LIVE_APP="/Applications/Ableton Live 12 Beta.app"    # ← update if needed
LIVE_PROCESS="Live"
NPX="/usr/local/bin/npx"                             # ← verify with: which npx

echo "[ParameterBridge] Startup script running"

while true; do
    echo "[ParameterBridge] Waiting for Ableton Live..."
    until pgrep -x "$LIVE_PROCESS" > /dev/null 2>&1; do
        sleep 5
    done

    echo "[ParameterBridge] Live detected — starting bridge"
    cd "$BRIDGE_DIR" || exit 1

    "$NPX" extensions-cli run --live "$LIVE_APP" .

    echo "[ParameterBridge] Bridge exited — waiting before retry"
    sleep 3
done
