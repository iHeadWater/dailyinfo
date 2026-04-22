# Agent Configuration Guide

This document explains how to configure an external Agent (such as OpenClaw) to consume briefings from `~/.dailyinfo/workspace/briefings/` and push to Discord.

## Overview

DailyInfo generates briefing files to:
```
~/.dailyinfo/workspace/briefings/{category}/*.md
```

An external Agent can monitor this directory and push to Discord via Discord Bot API.

## Agent Configuration

### 1. Workspace Mount (if Agent runs in Docker)

If the Agent runs in a Docker container, mount the workspace:
```yaml
volumes:
  - ~/.dailyinfo/workspace:/home/node/workspace:ro
```

### 2. Discord Bot Setup

**Required**: Discord Bot Token with following permissions:
- `Send Messages`
- `Read Message History`

Get token from: Discord Developer Portal → Applications → Your App → Bot → Reset Token

### 3. Cron Job Configuration

Configure the Agent to poll the briefings directory:

```bash
# Example cron (run every hour)
0 * * * * cd /path/to/agent && ./bin/push-briefings.sh >> /var/log/agent.log 2>&1
```

### 4. Push Script Example

```python
#!/usr/bin/env python3
"""External Agent: push briefings to Discord."""

import os
import shutil
from datetime import datetime
from pathlib import Path

import requests

WORKSPACE = Path.home() / '.dailyinfo' / 'workspace'
BRIEFINGS = WORKSPACE / 'briefings'
PUSHED = WORKSPACE / 'pushed'
DATE = datetime.now().strftime('%Y-%m-%d')

DISCORD_API = 'https://discord.com/api/v10'
DISCORD_TOKEN = os.environ['DISCORD_BOT_TOKEN']

CHANNELS = {
    'papers': '1489102139597787181',
    'ai_news': '1489102139597787182',
    'code': '1489102139597787183',
    'resource': '1489102139597787178',
}

def send(channel_id: str, content: str) -> bool:
    headers = {
        'Authorization': f'Bot {DISCORD_TOKEN}',
        'Content-Type': 'application/json',
    }
    resp = requests.post(
        f'{DISCORD_API}/channels/{channel_id}/messages',
        headers=headers,
        json={'content': content},
        timeout=10,
    )
    return resp.status_code in (200, 201)

def main():
    for category, channel_id in CHANNELS.items():
        dir_path = BRIEFINGS / category
        if not dir_path.exists():
            continue

        for md_file in dir_path.glob(f'*_{DATE}.md'):
            content = md_file.read_text()
            if send(channel_id, content):
                # Archive
                dest = PUSHED / category / md_file.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(md_file), str(dest))

if __name__ == '__main__':
    main()
```

## Alternative: Use DailyInfo's Built-in Push

DailyInfo already includes `push_to_discord.py`. Use it directly:

```bash
dailyinfo push
```

This is the recommended approach — no external Agent needed.

## Troubleshooting

### Bot can't see messages
- Ensure Bot is added to the server
- Check channel permissions: Bot needs "Read Messages" and "Send Messages"

### No files found
- Verify path: `ls ~/.dailyinfo/workspace/briefings/`
- Check pipeline ran: `dailyinfo run` first

### Permission denied
- If using Docker, ensure volume is mounted with correct permissions
- Or run push script on host instead of in container