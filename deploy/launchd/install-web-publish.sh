#!/bin/sh
# Install the host-side Web publish job (macOS launchd).
#
# The producer container has no Node 22 or git credentials, so Web publishing
# remains on the host. This job runs after the container's last Discord push.
#
#   ./deploy/launchd/install-web-publish.sh
#
# Run as the logged-in user (not with sudo).
set -eu

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
TEMPLATE="$REPO_ROOT/deploy/launchd/ai.dailyinfo.web-publish.plist.template"
LABEL="ai.dailyinfo.web-publish"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$REPO_ROOT/logs"
LOG_PATH="$LOG_DIR/dailyinfo-web-publish.log"

UV_BIN=${UV_BIN:-$(command -v uv || true)}
if [ -z "$UV_BIN" ]; then
    echo "uv not found; set UV_BIN or add uv to PATH" >&2
    exit 2
fi

if [ -n "${DAILYINFO_DATA_ROOT:-}" ]; then
    DATA_ROOT=$DAILYINFO_DATA_ROOT
elif [ -f "$REPO_ROOT/.env" ]; then
    DATA_ROOT=$(sed -n 's/^DAILYINFO_DATA_ROOT=//p' "$REPO_ROOT/.env" | tail -n 1)
fi
if [ -z "${DATA_ROOT:-}" ]; then
    DATA_ROOT="$HOME/.myagentdata/dailyinfo"
fi
DATA_ROOT=${DATA_ROOT%/}

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

PATH_VALUE="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

sed \
    -e "s#__DAILYINFO_DIR__#$REPO_ROOT#g" \
    -e "s#__UV_BIN__#$UV_BIN#g" \
    -e "s#__DATA_ROOT__#$DATA_ROOT#g" \
    -e "s#__LOG_PATH__#$LOG_PATH#g" \
    -e "s#__PATH__#$PATH_VALUE#g" \
    "$TEMPLATE" > "$DEST"
chmod 0644 "$DEST"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
if ! launchctl bootstrap "gui/$(id -u)" "$DEST" 2>/dev/null; then
    # macOS GUI sessions occasionally reject bootstrap with EIO; the legacy
    # load path targets the same user domain and works on those hosts.
    launchctl load -w "$DEST"
fi
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true

echo "installed $DEST"
echo "schedule: daily 07:40 (host time)"
echo "log: $LOG_PATH"
echo "The job does not run now; the next fire publishes after the container's Discord pushes."
