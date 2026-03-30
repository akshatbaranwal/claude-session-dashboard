#!/bin/bash
set -e

# Claude Session Dashboard — macOS installer
# Installs a launchd agent that keeps the dashboard running in the background.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_PY="$SCRIPT_DIR/server.py"
LABEL="com.user.claude-session-dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${1:-7891}"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"
LOG="/tmp/claude-session-dashboard.log"

echo "Claude Session Dashboard Installer"
echo "===================================="
echo ""
echo "  Server:   $SERVER_PY"
echo "  Python:   $PYTHON"
echo "  Port:     $PORT"
echo "  Log:      $LOG"
echo ""

# Verify python3 exists
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install Python 3.9+ first."
    exit 1
fi

# Verify server.py exists
if [ ! -f "$SERVER_PY" ]; then
    echo "Error: server.py not found at $SERVER_PY"
    exit 1
fi

# Verify Claude Code data exists
if [ ! -d "$HOME/.claude/projects" ]; then
    echo "Warning: ~/.claude/projects not found. Have you used Claude Code before?"
fi

# Unload existing agent if present
if launchctl list "$LABEL" &>/dev/null 2>&1; then
    echo "Stopping existing dashboard..."
    launchctl unload "$PLIST" 2>/dev/null || true
    sleep 1
fi

# Write the plist
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$PYTHON</string>
		<string>$SERVER_PY</string>
		<string>--port</string>
		<string>$PORT</string>
	</array>
	<key>WorkingDirectory</key>
	<string>$SCRIPT_DIR</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>StandardOutPath</key>
	<string>$LOG</string>
	<key>StandardErrorPath</key>
	<string>$LOG</string>
</dict>
</plist>
PLIST

# Load the agent
launchctl load "$PLIST"
sleep 2

# Verify
if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/" | grep -q 200; then
    echo "Dashboard is running at http://127.0.0.1:$PORT"
    echo ""
    echo "To open in browser:  open http://127.0.0.1:$PORT"
    echo "To view logs:        tail -f $LOG"
    echo "To uninstall:        bash $(basename "$0") --uninstall"
else
    echo "Warning: Dashboard may not have started. Check logs:"
    echo "  tail -f $LOG"
fi
