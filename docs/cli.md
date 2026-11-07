# CLI Reference

DailyInfo command-line interface built on `click` + `uv`.

## Installation

### Using uv (recommended)

```bash
git clone <repo-url>
cd dailyinfo

cp .env.example .env
# edit .env with your API keys

uv sync --python python3
uv pip install -e .
```

### Using pip (fallback)

```bash
pip install -e .
```

## Environment Setup

```bash
dailyinfo install
```

This command:

1. Validates `.env` — `DEEPSEEK_API_KEY` and `DISCORD_BOT_TOKEN` must be non-empty and not a placeholder.
2. Creates the workspace under `~/.myagentdata/dailyinfo/` (`freshrss/data`, `briefings/*`, `pushed/*`).
3. Installs Python dependencies via `uv sync` (falls back to `pip install -e .`).

Pipeline 6 additionally needs `OPENREVIEW_EMAIL` + `OPENREVIEW_PASSWORD`; `dailyinfo install` does not require them because pipelines 1–5 can run without OpenReview.

**It does NOT write to the host crontab.** Production scheduling runs inside the dailyinfo container stack (`compose.yml`, supercronic + `deploy/crontab.*`) — see [Production Deployment](production.md). External cron, systemd timers, or agent runtimes remain supported for custom setups (see [Agent Config](agent-config.md)).

## Commands

### Service Management (development)

These commands manage the dev-only `docker-compose.yml` (container
`dailyinfo-freshrss-dev`, data root `DAILYINFO_DEV_DATA_ROOT`). Production uses
`docker compose up -d` — see [Production Deployment](production.md).

```bash
dailyinfo start      # Start the development FreshRSS
dailyinfo stop       # Stop the development services
dailyinfo restart    # Restart the development FreshRSS
```

### Pipeline Execution

```bash
dailyinfo run                      # Run all 6 pipelines
dailyinfo run -p 1                 # Pipeline 1 (papers)
dailyinfo run -p 2                 # Pipeline 2 (AI news)
dailyinfo run -p 3                 # Pipeline 3 (arXiv CS.AI)
dailyinfo run -p 4                 # Pipeline 4 (code trending)
dailyinfo run -p 5                 # Pipeline 5 (university news)
dailyinfo run -p 6                 # Pipeline 6 (conference submissions)
dailyinfo run -p 6 --source openreview_iclr_2027  # One conference source
dailyinfo run -f all               # Force regenerate every source today
dailyinfo run -p 1 -f arxiv_cs_ai  # Force regenerate one source only
```

`dailyinfo run` is **idempotent**: if a non-placeholder briefing already exists
for today (either in `briefings/` waiting to be pushed, or already archived in
`pushed/`), the source is skipped and no AI call is made. Use `-f / --force`
to override — pass `all` to refresh everything, or repeat the flag with
specific source names (matches `config/sources.json`).

If the primary model (`deepseek-flash` via DeepSeek API) returns empty responses after 3
retries with exponential backoff (2s / 5s / 10s), `run` automatically falls
back to the model in `DAILYINFO_FALLBACK_MODEL` (default
`glm-5.3-flash` via the Zhipu official API) for 2 more attempts before giving up.
A missing `GLM_API_KEY` disables the fallback outright rather than retrying
without credentials.

Pipeline 6 stores OpenReview submission metadata and public replies in
`$DAILYINFO_DATA_ROOT/conference/conference.sqlite3`, writes a deterministic
markdown listing to `briefings/conference/`, and can additionally render an AI
Chinese briefing for review-bearing events (`{source}_ai_{batch_hash}.md`).
It does not rank relevance. `dailyinfo push` sends a compact one-line-per-paper
digest to the conference channel (`DISCORD_CHANNEL_CONFERENCE`, falling back to
`DISCORD_CHANNEL_AI_NEWS`); the detailed AI briefing stays local and is moved to
`pushed/conference/` after delivery. When no digest is pending, one
`暂无新增投稿或评审变化` notice is sent. New submissions alone stay in the
deterministic listing unless `ai_briefing.include_new_submissions` is enabled;
`ai_briefing.max_events_per_briefing` and `max_batches_per_run` cap model calls.
Reviews are fetched in bulk with `details=replies`; only replies readable by
`everyone` are stored. The first review run stores a one-time sample of the
latest `reviews.sample_limit` submissions (default 50), then sets
`reviews_watch_from_ms`; later incremental and weekly rescan passes never go
before that cutoff. `reviews.max_pages_per_run` pauses with a resumable cursor
and `reviews.request_interval_seconds` spaces requests. Set `OPENREVIEW_EMAIL`
and `OPENREVIEW_PASSWORD` in `.env`; OpenReview authenticates by **email**, and
anonymous note queries are rejected by its browser challenge. The legacy
`OPENREVIEW_USERNAME` key is still read as a fallback. A source that is not
open yet (`NOT_OPEN`) or not due for a poll (`NOT_DUE`) is a clean outcome, not
a failure.

### Push to Discord

```bash
dailyinfo push                       # Push today's briefings (all categories)
dailyinfo push -d 2026-04-22         # Backfill a specific day (YYYY-MM-DD)
dailyinfo push -c papers,ai_news     # Push only selected categories
dailyinfo push -c conference         # Conference one-line review digest only
dailyinfo push -c weekly             # Push weekly recap only (noon cron)
dailyinfo push --force               # Explicitly retry a successful Discord delivery
```

For Phase 2C publications, `push` loads the canonical
`publications/briefings/.../briefing.json` bundle, sends its `Briefing.body`
through `DiscordPublisher`, and records `(briefing_id, discord)` in the
independent `deliveries/` store. Conference is the exception: it has no
canonical bundle, so `push -c conference` sends the compact one-line digest
built from `briefings/conference/` and archives the detailed files. A recorded successful delivery is skipped on
normal repeats; `--force` explicitly sends it again. Failed deliveries return
non-zero and remain retryable. The old `briefings/` and `pushed/` Markdown paths
remain for compatibility and archive maintenance; a real legacy pending file
without a canonical bundle is rejected instead of being used as a new
publication source.

### Publish canonical Publications

```bash
dailyinfo publish --sink web                       # publish today to dailyinfo-web
dailyinfo publish --sink web -d 2026-04-22         # publish one date
dailyinfo publish --sink web -c papers,arxiv       # select categories
dailyinfo publish --sink web --force                # reconcile Web despite success state
dailyinfo publish --sink discord                   # canonical Discord delivery
dailyinfo publish --sink all                       # independent Discord + Web attempts
```

The Web sink requires `DAILYINFO_WEB_REPO` to name a persistent local
`dailyinfo-web` checkout. Optional `DAILYINFO_WEB_REMOTE` and
`DAILYINFO_WEB_BRANCH` protect against publishing to the wrong remote or branch.
The checkout must be clean and on the configured branch. WebPublisher fetches
and fast-forwards only, writes only `src/content/items/generated/` and
`src/content/briefings/generated/`, runs the Web validation/test/check/build
gates, then creates and pushes one ordinary DailyInfo Bot commit for each
changed Briefing. A push failure leaves that local publisher commit for retry.

Each successful publish also enforces the rolling retention window
(`DAILYINFO_WEB_WINDOW_DAYS`, default `7`; `0` disables the sweep): the sweep
is anchored to the newest canonical date, prunes Briefings and Items that fell
out of the window in the same commit, and never touches another publisher's
content in the shared generated directories (see the retention section of
`docs/publication-architecture.md`).

`dailyinfo publish` reads canonical `publications/` only; it never reconstructs
a PublicationBundle from legacy Markdown. With `--sink all`, a failed sink does
not roll back a successful other sink, and the command exits non-zero if either
sink fails. Web publishing is intentionally not part of `dailyinfo run` in
Phase 2D.

### Weekly Recap

```bash
dailyinfo weekly                # 汇总过去 7 天的 AI 新闻
dailyinfo weekly --days 14      # 自定义回溯窗口
dailyinfo weekly --force        # 覆盖今天已生成的 recap
```

### Code Weekly

```bash
dailyinfo code-weekly             # 汇总过去 7 天的 GitHub Trending
dailyinfo code-weekly --days 14   # 自定义回溯窗口
dailyinfo code-weekly --force     # 覆盖今天已生成的数据文件
```

Aggregates the repos appearing in `github_trending_briefing_*.md` and ranks them
by how many days they appeared on, writing the top 5 to
`briefings/code_weekly/data_{DATE}.json`.

Pure local processing — no network calls, no credentials, no LLM. It reads
briefing files only, so the same inputs always produce the same ranking; repos
tied on day count keep the order they first appeared in.

### Status & Logs

```bash
dailyinfo status    # Counts of today's briefings / pushed files
dailyinfo logs      # Tail logs/dailyinfo.log (if enabled)
```

## Environment Variables

Create `.env` in the project root:

```env
DEEPSEEK_API_KEY=sk-your_deepseek_key_here
DISCORD_BOT_TOKEN=your_discord_token
OPENREVIEW_EMAIL=you@example.com
OPENREVIEW_PASSWORD=your_openreview_password
FRESHRSS_USER=owen
FRESHRSS_PASSWORD=freshrss123
# DAILYINFO_DATA_ROOT=   # optional override, defaults to ~/.myagentdata/dailyinfo
```

| Key | Purpose |
|-----|---------|
| `DEEPSEEK_API_KEY` | DeepSeek API key (required — primary model: `deepseek-flash`) |
| `GLM_API_KEY` | Zhipu GLM API key (optional, only needed for fallback) |
| `DISCORD_BOT_TOKEN` | Discord bot token used by `dailyinfo push` |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_CODE` / `_RESOURCE` / `_ARXIV` / `_CONFERENCE` | Per-category channel IDs (missing ones are skipped, not fatal; conference falls back to `_AI_NEWS`) |
| `DISCORD_CHANNEL_*_DEV` / `_STAGING` | Env-specific channel IDs when `DAILYINFO_ENV=dev` or `staging` |
| `OPENREVIEW_EMAIL` / `OPENREVIEW_PASSWORD` | OpenReview credentials for pipeline 6 (email, not display name; legacy `OPENREVIEW_USERNAME` still read) |
| `DAILYINFO_ENV` | Environment: `prod` / `dev` / `staging` (default `prod`) — controls data dir and channel suffix |
| `DAILYINFO_DATA_ROOT` | Override data root (default `~/.myagentdata/dailyinfo`; env-suffixed for dev/staging) |
| `DAILYINFO_WEB_REPO` | Local `dailyinfo-web` checkout required by `publish --sink web` |
| `DAILYINFO_WEB_REMOTE` | Expected Web `origin` URL (default `git@github.com:iHeadWater/dailyinfo-web.git`) |
| `DAILYINFO_WEB_BRANCH` | Expected Web branch (default `main`) |
| `DAILYINFO_WEB_WINDOW_DAYS` | Rolling Web retention window in days (default `7`; `0` disables the sweep) |
| `FRESHRSS_USER` | FreshRSS username (default: `$USER`) |
| `FRESHRSS_PASSWORD` | FreshRSS password |
| `DAILYINFO_FALLBACK_MODEL` | Fallback LLM when the primary model returns empty (default `glm-5.3-flash`, a Zhipu model name) |

## Scheduling

生产环境默认由 dailyinfo 自己的 compose 栈调度（`scheduler` + supercronic + `deploy/crontab.*`），不再依赖宿主 cron。外部 cron、systemd timer 或 agent runtime 仍可用于自定义部署。

当前 self-hosted 时刻表（`deploy/crontab.full`）：

| Command | Scheduled time | Purpose |
|---------|----------------|---------|
| `dailyinfo run -p 3` | 03:00 | arXiv CS.AI |
| `dailyinfo run -p 5` | 03:30 | university notices |
| `dailyinfo run -p 4` | 03:45 | code trending |
| `dailyinfo run -p 1` | 04:00 | papers |
| `dailyinfo run -p 2` | 04:30 | AI news |
| `push --categories ai_news,code,resource` | 05:30 | early push |
| `push --categories papers` | 06:00 | papers push |
| `push --categories arxiv` | 07:00 | arXiv push |

> Pipeline 6（会议）暂未加入 `deploy/crontab.*`。在 follow-up #101 完成前，需要手动运行 `dailyinfo run -p 6` 和 `dailyinfo push -c conference`，或自己加到外部 cron。

外置调度示例（包含 pipeline 6）：

```cron
0 3 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 3 >> logs/pipeline3.log 2>&1
30 3 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 5 >> logs/pipeline5.log 2>&1
45 3 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 4 >> logs/pipeline4.log 2>&1
0 4 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 1 >> logs/pipeline1.log 2>&1
30 4 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 2 >> logs/pipeline2.log 2>&1
0 5 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 6 >> logs/pipeline6.log 2>&1
30 5 * * * cd /path/to/dailyinfo && python3 scripts/push_to_discord.py --categories ai_news,code,resource >> logs/discord_push.log 2>&1
0 6 * * * cd /path/to/dailyinfo && python3 scripts/push_to_discord.py --categories papers >> logs/discord_push.log 2>&1
0 7 * * * cd /path/to/dailyinfo && python3 scripts/push_to_discord.py --categories arxiv >> logs/discord_push.log 2>&1
30 7 * * * cd /path/to/dailyinfo && python3 scripts/push_to_discord.py --categories conference >> logs/discord_push.log 2>&1
```

如果也在用 myopenclaw 的 agent 生态管理外部调度，可以参见 [Agent Config](agent-config.md)。

## Docker Services

开发环境只需要 FreshRSS；生产环境使用完整的 compose 栈：

```bash
# 开发：仅 FreshRSS
docker compose -f docker-compose.yml up -d freshrss
# 生产：freshrss + scheduler + collector profile + 只读 MCP
docker compose up -d
docker compose exec scheduler python -m scripts.scheduler status
```

FreshRSS data 持久化在 `~/.myagentdata/dailyinfo/freshrss/data/`。生产部署的镜像、凭据边界、补跑和回滚见 [Production Deployment](production.md)。
