#!/usr/bin/env bash
# setup.sh — one-command setup for dailyinfo on a new machine
# Usage: ./setup.sh
# Prerequisites: git, docker, docker-compose, python3, pip3

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC}  $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }
step() { echo -e "\n${CYAN}==>${NC} $*"; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRESHRSS_USER="sjiaxin"
FRESHRSS_URL="http://localhost:8081"
FRESHRSS_API="${FRESHRSS_URL}/api/greader.php"

# ---------------------------------------------------------------------------
# 1. Check .env exists
# ---------------------------------------------------------------------------
step "Checking .env file..."
if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
    if [[ -f "${PROJECT_DIR}/.env.example" ]]; then
        cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
        err ".env not found. Copied from .env.example — fill in your API keys then re-run."
        echo "    Required keys: OPENROUTER_API_KEY, DISCORD_BOT_TOKEN"
        exit 1
    else
        err ".env not found and no .env.example to copy from."
        exit 1
    fi
fi

# Check required keys
OPENROUTER_KEY=$(grep '^OPENROUTER_API_KEY=' "${PROJECT_DIR}/.env" | cut -d= -f2- | tr -d '"'"'" | xargs)
DISCORD_TOKEN=$(grep '^DISCORD_BOT_TOKEN=' "${PROJECT_DIR}/.env" | cut -d= -f2- | tr -d '"'"'" | xargs)

if [[ -z "${OPENROUTER_KEY}" || "${OPENROUTER_KEY}" == your_* ]]; then
    err "OPENROUTER_API_KEY not set in .env"
    exit 1
fi
if [[ -z "${DISCORD_TOKEN}" || "${DISCORD_TOKEN}" == your_* ]]; then
    err "DISCORD_BOT_TOKEN not set in .env"
    exit 1
fi
ok ".env looks good"

# ---------------------------------------------------------------------------
# 2. Create workspace directories
# ---------------------------------------------------------------------------
step "Creating workspace directories..."
mkdir -p ~/.openclaw/workspace/briefings/{papers,ai_news,code,resource}
mkdir -p ~/.openclaw/workspace/pushed/{papers,ai_news,code,resource}
mkdir -p ~/.freshrss/data
mkdir -p ~/.n8n
mkdir -p "${PROJECT_DIR}/logs"
ok "Directories ready"

# ---------------------------------------------------------------------------
# 3. Start Docker services
# ---------------------------------------------------------------------------
step "Starting Docker services (FreshRSS, n8n, openclaw)..."
cd "${PROJECT_DIR}"
docker compose up -d
ok "Docker services started"

# Wait for FreshRSS to be ready
step "Waiting for FreshRSS to be ready..."
for i in $(seq 1 30); do
    if curl -sf "${FRESHRSS_URL}" > /dev/null 2>&1; then
        ok "FreshRSS is up"
        break
    fi
    if [[ $i -eq 30 ]]; then
        err "FreshRSS did not start in 60s. Check: docker compose logs freshrss"
        exit 1
    fi
    echo -n "."
    sleep 2
done

# ---------------------------------------------------------------------------
# 4. Create FreshRSS account + subscribe to feeds
# ---------------------------------------------------------------------------
step "Setting up FreshRSS account..."

FRESHRSS_PASS=$(grep '^FRESHRSS_PASSWORD=' "${PROJECT_DIR}/.env" 2>/dev/null | cut -d= -f2- | tr -d '"'"'" | xargs || echo "")
if [[ -z "${FRESHRSS_PASS}" ]]; then
    # Try default
    FRESHRSS_PASS="freshrss123"
    warn "FRESHRSS_PASSWORD not in .env, using default: ${FRESHRSS_PASS}"
fi

# Create account if not exists, then ensure API password is set
FRESHRSS_AUTH=""
SUBSCRIBED=0
SKIPPED=0

# Try to create (silently fails if already exists)
docker exec dailyinfo_freshrss php /var/www/FreshRSS/cli/create-user.php \
    --user "${FRESHRSS_USER}" \
    --password "${FRESHRSS_PASS}" \
    --api-password "${FRESHRSS_PASS}" \
    --no-default-feeds 2>/dev/null || true

# Always update API password to ensure it matches (handles pre-existing accounts)
docker exec dailyinfo_freshrss php /var/www/FreshRSS/cli/update-user.php \
    --user "${FRESHRSS_USER}" \
    --api-password "${FRESHRSS_PASS}" 2>/dev/null || true

# Login
LOGIN_RESP=$(curl -sf -X POST "${FRESHRSS_API}/accounts/ClientLogin" \
    -d "Email=${FRESHRSS_USER}&Passwd=${FRESHRSS_PASS}" 2>/dev/null || echo "")

if echo "${LOGIN_RESP}" | grep -q "Auth="; then
    FRESHRSS_AUTH=$(echo "${LOGIN_RESP}" | grep "^Auth=" | cut -d= -f2)
    ok "FreshRSS account '${FRESHRSS_USER}' ready"
else
    warn "Could not login to FreshRSS — feed subscriptions will be skipped. Visit ${FRESHRSS_URL} to set up manually."
fi

# Subscribe to RSS feeds via OPML import
step "Subscribing to RSS feeds in FreshRSS..."
OPML_FILE=$(mktemp /tmp/dailyinfo_feeds_XXXXXX.opml)
python3 - <<PYEOF
import json
cfg = json.load(open('${PROJECT_DIR}/config/feeds.json'))
feeds = [f for f in cfg.get('feeds', []) if f.get('enabled') and f.get('url')]
lines = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<opml version="1.0"><head><title>DailyInfo</title></head><body>']
for f in feeds:
    name = f.get('display_name', f.get('name', '')).replace('"', '&quot;')
    url = f['url'].replace('&', '&amp;')
    lines.append(f'  <outline type="rss" text="{name}" xmlUrl="{url}"/>')
lines.append('</body></opml>')
open('${OPML_FILE}', 'w').write('\n'.join(lines))
print(f"Generated OPML with {len(feeds)} feeds")
PYEOF
docker cp "${OPML_FILE}" dailyinfo_freshrss:/tmp/feeds.opml
docker exec dailyinfo_freshrss php /var/www/FreshRSS/cli/import-for-user.php \
    --user "${FRESHRSS_USER}" --filename /tmp/feeds.opml 2>&1 | grep -v "^$" || true
rm -f "${OPML_FILE}"
docker exec dailyinfo_freshrss php /var/www/FreshRSS/cli/actualize-user.php \
    --user "${FRESHRSS_USER}" 2>&1 | tail -2 || true
ok "Feed subscriptions complete"

# ---------------------------------------------------------------------------
# 5. Install Python dependencies
# ---------------------------------------------------------------------------
step "Installing Python dependencies..."
pip install -e "${PROJECT_DIR}" -q
ok "Python packages installed (dailyinfo CLI available)"

# ---------------------------------------------------------------------------
# 6. Install crontab
# ---------------------------------------------------------------------------
step "Installing crontab..."
CRON_FILE=$(mktemp)
# Preserve existing crontab entries (remove old dailyinfo entries first)
crontab -l 2>/dev/null | grep -v 'dailyinfo' > "${CRON_FILE}" || true

# Add fresh dailyinfo entries
PROXY_HOST=$(grep '^https_proxy=' ~/.bashrc ~/.bash_profile ~/.profile /etc/environment 2>/dev/null | head -1 | cut -d= -f2- | xargs || echo "http://127.0.0.1:7890")
cat >> "${CRON_FILE}" << CRON
# DailyInfo — auto-generated by setup.sh
HTTPS_PROXY=${PROXY_HOST}
HTTP_PROXY=${PROXY_HOST}
ALL_PROXY=socks5://127.0.0.1:7890
0 6 * * * cd ${PROJECT_DIR} && python3 scripts/run_pipelines.py --pipeline 1 >> ${PROJECT_DIR}/logs/pipeline1.log 2>&1
15 6 * * * cd ${PROJECT_DIR} && python3 scripts/run_pipelines.py --pipeline 2 >> ${PROJECT_DIR}/logs/pipeline2.log 2>&1
30 6 * * * cd ${PROJECT_DIR} && python3 scripts/run_pipelines.py --pipeline 3 >> ${PROJECT_DIR}/logs/pipeline3.log 2>&1
0 7 * * * cd ${PROJECT_DIR} && python3 scripts/push_to_discord.py >> ${PROJECT_DIR}/logs/discord_push.log 2>&1
CRON

crontab "${CRON_FILE}"
rm "${CRON_FILE}"
ok "Crontab installed (run: crontab -l to verify)"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}=== Setup complete! ===${NC}"
echo ""
echo "Services:"
echo "  FreshRSS:  ${FRESHRSS_URL}"
echo "  n8n:       http://localhost:5678"
echo ""
echo "CLI commands:"
echo "  dailyinfo run        # run all pipelines now"
echo "  dailyinfo run -p 2   # run only code trending"
echo "  dailyinfo push       # push today's briefings to Discord"
echo "  dailyinfo status     # check briefing file counts"
echo ""
echo "Schedule (cron): pipelines at 06:00/06:15/06:30, Discord push at 07:00"
