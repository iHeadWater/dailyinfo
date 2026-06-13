# System Architecture

DailyInfo 是面向 AI for Science 研究者的自动化情报聚合与精读系统。

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Configuration Layer                              │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  config/sources.json (RSS + API + Scrape)                    │   │
│  │  • 35+ RSS sources (journals + AI news)                      │   │
│  │  • Code sources (GitHub / HuggingFace)                       │   │
│  │  • Resource sources (DLUT websites)                          │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Collection Layer                                 │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  FreshRSS (RSS unified collection)                           │   │
│  │  • Docker container (port 8081)                              │   │
│  │  • SQLite at ~/.myagentdata/dailyinfo/freshrss/data          │   │
│  │                                                              │   │
│  │  Direct API / HTML scraping (code + university sources)      │   │
│  │  • GitHub Trending (HTML scraper)                            │   │
│  │  • HuggingFace API                                           │   │
│  │  • DLUT websites (regex parsing)                             │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                         ▼ SQLite / API / HTML                       │
└─────────────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Processing Layer (dailyinfo run)                 │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  scripts/run_pipelines.py                                    │   │
│  │  • Pipeline 1: Papers → AI summary → briefings/papers        │   │
│  │  • Pipeline 2: AI News → AI summary → briefings/ai_news      │   │
│  │  • Pipeline 3: arXiv CS.AI → AI summary → briefings/arxiv    │   │
│  │  • Pipeline 4: Code trending → AI summary → briefings/code   │   │
│  │  • Pipeline 5: University news → AI summary → briefings/res. │   │
│  │                                                              │   │
│  │  DeepSeek V4 Pro API (primary); OpenRouter fallback (kimi-k2.5)    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                         ▼ Markdown files                            │
│                   ~/.myagentdata/dailyinfo/briefings/{category}/    │
└─────────────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Push Layer (dailyinfo push)                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  scripts/push_to_discord.py  (plain Python, no LLM)          │   │
│  │  • scan briefings/{category}/ for today's files              │   │
│  │  • POST to Discord channel (split > 2000 chars)              │   │
│  │  • mv to pushed/{category}/ after success                    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                         ▼ Discord channels                          │
│              #paper | #deeplearning | #code | #resource             │
└─────────────────────────────────────────────────────────────────────┘
                          ▲
                          │ triggers (e.g. 06:00 run, 07:00 push)
┌─────────────────────────────────────────────────────────────────────┐
│                Scheduling Layer (external cron, optional)           │
│  • any cron / systemd timer / agent invokes `dailyinfo run / push`  │
│  • backup is handled by whatever tool watches the data root         │
└─────────────────────────────────────────────────────────────────────┘
```

## Data Persistence

| Directory | Purpose | Owner |
|-----------|---------|-------|
| `~/.myagentdata/dailyinfo/freshrss/data/` | FreshRSS DB + config | dailyinfo (freshrss container) |
| `~/.myagentdata/dailyinfo/briefings/` | Generated briefings (pending push) | `dailyinfo run` |
| `~/.myagentdata/dailyinfo/pushed/` | Archive after successful push | `dailyinfo push` |
| `~/.myagentdata/dailyinfo/state/` | Runtime state (marker files) | `dailyinfo run` |

数据根默认在 `~/.myagentdata/dailyinfo/`，可通过 `DAILYINFO_DATA_ROOT` 覆盖。dailyinfo 本身不做备份；若与 myopenclaw 等支持只读挂载 `~/.myagentdata/` 的备份方案一起使用，可直接被覆盖（详见 [Agent Config](agent-config.md)）。

## Responsibility Separation

| Layer | Responsibility | Does NOT do |
|-------|----------------|-------------|
| **Processing** (`run_pipelines.py`) | RSS/API/Scrape → LLM → Markdown file | ❌ 推送、调度 |
| **Push** (`push_to_discord.py`) | 扫 briefings → POST Discord → 归档 | ❌ 调用 AI、调度 |
| **Scheduling** (external cron) | 定时触发 `dailyinfo run` / `dailyinfo push` | ❌ 业务逻辑 |

两层脚本都是幂等纯函数：`run` 重跑只会覆盖当天文件；`push` 重跑不会重复推送（因为成功后会 `mv`）。

## Pipeline Details

### Pipeline 1: Papers
- **Input**: FreshRSS SQLite DB + scrape/API sources (30+ journals, Chinese water journals)
- **Output**: `briefings/papers/`
- **去重**：`lookback_hours > 24` 的低频源检查 `pushed/<category>/` 里的同名文件

### Pipeline 2: AI News
- **Input**: FreshRSS SQLite DB (smolai via deep-content processing)
- **Output**: `briefings/ai_news/`

### Pipeline 3: arXiv CS.AI
- **Input**: FreshRSS SQLite DB (arXiv RSS, up to 500 articles)
- **Output**: `briefings/arxiv/`
- **特殊处理**：运行时创建 `.arxiv_generating` marker 文件，`push` 在推送前轮询等待（最长 30 分钟）

### Pipeline 4: Code Trending
- **Input**: GitHub Trending HTML + HuggingFace API
- **Output**: `briefings/code/`

### Pipeline 5: University News
- **Input**: DLUT 网站（HTML + API）
- **Output**: `briefings/resource/`

## Discord Channel Mapping

频道 ID 由 `.env` 配置（不在代码里硬编码）：

| Category | Prod | Dev | Staging |
|----------|------|-----|---------|
| papers   | `DISCORD_CHANNEL_PAPERS` | `_PAPERS_DEV` | `_PAPERS_STAGING` |
| ai_news  | `DISCORD_CHANNEL_AI_NEWS` | `_AI_NEWS_DEV` | `_AI_NEWS_STAGING` |
| arxiv    | `DISCORD_CHANNEL_ARXIV` (falls back to `DISCORD_CHANNEL_AI_NEWS`) | `_ARXIV_DEV` | `_ARXIV_STAGING` |
| code     | `DISCORD_CHANNEL_CODE` | `_CODE_DEV` | `_CODE_STAGING` |
| resource | `DISCORD_CHANNEL_RESOURCE` | `_RESOURCE_DEV` | `_RESOURCE_STAGING` |

Set `DAILYINFO_ENV=dev` or `staging` to use the suffixed keys. If the suffixed
key is empty, dev/staging falls back to the prod channel with a warning.

缺失某个分类的频道 ID 时，`dailyinfo push` 会打 WARN 并跳过该分类，不会中断其他分类的推送。
