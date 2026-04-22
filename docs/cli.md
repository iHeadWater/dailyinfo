# CLI Reference

DailyInfo command-line interface using uv and Click.

## Installation

### Using uv (Recommended)

```bash
# Clone and enter project
git clone <repo-url>
cd dailyinfo

# Create .env from template
cp .env.example .env
# Edit .env with your API keys

# Install dependencies
uv sync --python python3

# Install CLI
uv pip install -e .
```

### Using pip (Fallback)

```bash
pip install -e .
```

## Environment Setup

```bash
dailyinfo install
```

This command:
1. Validates `.env` configuration
2. Creates workspace directories (`~/.dailyinfo/workspace/`)
3. Installs Python dependencies
4. Sets up crontab for scheduled runs

## Commands

### Service Management

```bash
dailyinfo start      # Start FreshRSS (Docker)
dailyinfo stop      # Stop services
dailyinfo restart  # Restart FreshRSS
```

### Pipeline Execution

```bash
dailyinfo run           # Run all pipelines
dailyinfo run -p 1    # Run Pipeline 1 (RSS papers/news)
dailyinfo run -p 2    # Run Pipeline 2 (code trending)
dailyinfo run -p 3    # Run Pipeline 3 (university news)
```

### Push to Discord

```bash
dailyinfo push    # Push today's briefings to Discord
```

### Status & Logs

```bash
dailyinfo status    # Show briefing file counts
dailyinfo logs      # Tail execution log
```

## Cron Schedule

After `dailyinfo install`, the following cron jobs are set:

| Time | Command |
|------|---------|
| 06:00 | Pipeline 1 (RSS) |
| 06:15 | Pipeline 2 (Code) |
| 06:30 | Pipeline 3 (University) |
| 07:00 | Push to Discord |

## Environment Variables

Create `.env` in project root:

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxx
DISCORD_BOT_TOKEN=your_discord_token
FRESHRSS_USER=owen
```

### Required Keys

| Key | Purpose |
|-----|---------|
| `OPENROUTER_API_KEY` | LLM API for generating summaries |
| `DISCORD_BOT_TOKEN` | Discord Bot for pushing briefings |

### Optional Keys

| Key | Default |
|-----|---------|
| `FRESHRSS_USER` | System user (`$USER`) |

## Manual Crontab Setup

If you prefer manual setup:

```bash
crontab -e

# Add:
0 6 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 1 >> logs/pipeline1.log 2>&1
15 6 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 2 >> logs/pipeline2.log 2>&1
30 6 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 3 >> logs/pipeline3.log 2>&1
0 7 * * * cd /path/to/dailyinfo && python3 scripts/push_to_discord.py >> logs/discord_push.log 2>&1
```

## Docker Services

Only FreshRSS runs in Docker:

```bash
# Start
docker compose up -d freshrss

# Stop
docker compose down

# Logs
docker compose logs -f freshrss

# Access
# http://localhost:8081
```