#!/bin/sh
# Prepare the bind-mount sources for the self-hosted DailyInfo stack.
#
#   sudo -E ./deploy/bootstrap.sh
#
# Requires DAILYINFO_DATA_ROOT and DAILYINFO_MCP_SECRETS_DIR in the
# environment (the same values compose.yml reads). On macOS, Docker Desktop
# maps ownership itself, so the chown/chgrp steps are skipped.
set -eu

: "${DAILYINFO_DATA_ROOT:?Set DAILYINFO_DATA_ROOT to the shared content root}"
: "${DAILYINFO_MCP_SECRETS_DIR:?Set DAILYINFO_MCP_SECRETS_DIR to the MCP auth directory}"

root="${DAILYINFO_DATA_ROOT%/}"
secrets="${DAILYINFO_MCP_SECRETS_DIR%/}"

# Compose uses create_host_path: false, so every bind source must exist before
# `docker compose up -d` or the stack refuses to start with a confusing error.
mkdir -p \
    "$root/freshrss/data" \
    "$root/briefings" \
    "$root/pushed" \
    "$root/publications" \
    "$root/deliveries" \
    "$root/state" \
    "$secrets"

if [ "$(uname -s)" = "Darwin" ]; then
    echo "macOS: Docker Desktop maps ownership; skipping chown/chgrp."
    echo "Prepared $root and $secrets."
    exit 0
fi

# The producer (scheduler/collector) writes as UID 33. The MCP reads
# publications and deliveries as GID 10001 through a setgid, group-readable
# directory; its key directory belongs to UID 10001 alone.
chown -R 33:33 \
    "$root/freshrss" \
    "$root/briefings" \
    "$root/pushed" \
    "$root/publications" \
    "$root/deliveries" \
    "$root/state"
chgrp -R 10001 "$root/publications" "$root/deliveries"
chmod -R g+rX "$root/publications" "$root/deliveries"
find "$root/publications" "$root/deliveries" -type d -exec chmod 2755 {} +
chown 10001:10001 "$secrets"
chmod 700 "$secrets"

echo "Prepared $root and $secrets."
echo "Next: docker compose up -d"
