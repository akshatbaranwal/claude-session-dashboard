#!/bin/bash
set -e

# Claude Session Dashboard — macOS uninstaller

LABEL="com.user.claude-session-dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "Uninstalling Claude Session Dashboard..."

if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm "$PLIST"
    echo "Removed launchd agent."
else
    echo "No launchd agent found."
fi

echo "Done. The server.py file has not been removed."
