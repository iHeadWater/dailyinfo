#!/bin/sh
# Remove the host-side Web publish job.
set -eu

LABEL="ai.dailyinfo.web-publish"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl unload -w "$DEST" >/dev/null 2>&1 || true
rm -f "$DEST"
echo "removed $LABEL"
