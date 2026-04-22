# OpenClaw Agent Configuration

DailyInfo writes briefings to a shared workspace. This guide explains how to configure OpenClaw to mount that workspace and consume the briefings.

## Workspace Path Priority

DailyInfo resolves `WORKSPACE_ROOT` in this order:

1. `WORKSPACE_ROOT` in `.env` (if set and valid)
2. `~/Google Drive/dailyinfo/workspace` (if Google Drive exists)
3. `~/.dailyinfo/workspace` (fallback)

**Default on this machine**: `~/Google Drive/dailyinfo/workspace`

To verify:
```bash
python3 -c "from scripts.paths import WORKSPACE_ROOT; print(WORKSPACE_ROOT)"
```

## OpenClaw Docker Mount

The OpenClaw container runs as `dailyinfo_openclaw`. To mount the shared workspace, add a volume bind to the container's mount configuration.

**Get current mount config:**
```bash
docker inspect dailyinfo_openclaw --format '{{json .Mounts}}' | python3 -m json.tool
```

**Add mount for shared workspace:**

Find the OpenClaw container's Docker run config. The key addition is:
```yaml
volumes:
  - ~/Google Drive/dailyinfo/workspace:/home/node/workspace:ro
```

This makes dailyinfo's briefings available inside the OpenClaw container at `/home/node/workspace/briefings/`.

**Example (docker compose snippet):**
```yaml
services:
  openclaw-gateway:
    # ... existing config ...
    volumes:
      - ~/.openclaw:/home/node/.openclaw
      - ~/.mineru:/home/node/.mineru
      - ~/Google Drive/dailyinfo/workspace:/home/node/workspace:ro  # <-- add this
```

Then restart:
```bash
docker compose down
docker compose up -d
```

Or if OpenClaw uses a startup script, add the mount to that script's `docker run` command.

## OpenClaw Agent: Read Briefings

Once mounted, an OpenClaw sub-agent can read briefings:

```
/home/node/workspace/briefings/{category}/*_{DATE}.md
```

For example, a briefing reader skill or agent that:
1. Lists `briefings/papers/` for today's `.md` files
2. Reads the content
3. Pushes to Discord via Bot API

## Discord Channel IDs

| Category | Channel ID |
|----------|-----------|
| papers | `1489102139597787181` |
| ai_news | `1489102139597787182` |
| code | `1489102139597787183` |
| resource | `1489102139597787178` |

## Troubleshooting

### OpenClaw can't see briefings
- Verify mount: `docker exec dailyinfo_openclaw ls /home/node/workspace/briefings/`
- Check path matches dailyinfo's output: run the verify command above

### Permission denied
- Mount as `:ro` (read-only) is recommended — OpenClaw should only read, not write
- Ensure the host folder is readable by the container user