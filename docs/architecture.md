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
│  │  • Pipeline 1: RSS → AI summary → briefings/papers, ai_news  │   │
│  │  • Pipeline 2: Code trending → AI summary → briefings/code   │   │
│  │  • Pipeline 3: University news → AI summary → briefings/res. │   │
│  │                                                              │   │
│  │  OpenRouter API (LLM aggregation, default kimi-k2.5)         │   │
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
                          │ triggers (6:00 run, 7:00 push, ...)
┌─────────────────────────────────────────────────────────────────────┐
│                    Scheduling Layer (myopenclaw)                    │
│  • hermes cron invokes `dailyinfo run` and `dailyinfo push`         │
│  • backup-cron snapshots ~/.myagentdata/ to cloud drive             │
└─────────────────────────────────────────────────────────────────────┘
```

## Data Persistence

| Directory | Purpose | Owner |
|-----------|---------|-------|
| `~/.myagentdata/dailyinfo/freshrss/data/` | FreshRSS DB + config | dailyinfo (freshrss container) |
| `~/.myagentdata/dailyinfo/briefings/` | Generated briefings (pending push) | `dailyinfo run` |
| `~/.myagentdata/dailyinfo/pushed/` | Archive after successful push | `dailyinfo push` |

所有数据位于 `~/.myagentdata/` 下，由 myopenclaw 的 `backup-cron` 容器只读挂载并定期快照到云盘。

## Responsibility Separation

| Layer | Responsibility | Does NOT do |
|-------|----------------|-------------|
| **Processing** (`run_pipelines.py`) | RSS/API/Scrape → LLM → Markdown file | ❌ 推送、调度 |
| **Push** (`push_to_discord.py`) | 扫 briefings → POST Discord → 归档 | ❌ 调用 AI、调度 |
| **Scheduling** (myopenclaw hermes cron) | 定时触发 `dailyinfo run` / `dailyinfo push` | ❌ 业务逻辑 |

两层脚本都是幂等纯函数：`run` 重跑只会覆盖当天文件；`push` 重跑不会重复推送（因为成功后会 `mv`）。

## Pipeline Details

### Pipeline 1: RSS Papers + AI News
- **Input**: FreshRSS SQLite DB (`~/.myagentdata/dailyinfo/freshrss/data/users/<user>/db.sqlite`)
- **Output**: `briefings/papers/`, `briefings/ai_news/`
- **去重**：`lookback_hours > 24` 的低频源检查 `pushed/<category>/` 里 mtime 在 lookback 内的同名文件，避免重复调用 AI

### Pipeline 2: Code Trending
- **Input**: GitHub Trending HTML + HuggingFace API
- **Output**: `briefings/code/`
- **Prompt**: 复用 `sources.json` 的 `prompt_templates.code_trending`

### Pipeline 3: University News
- **Input**: DLUT 网站（HTML + API）
- **Output**: `briefings/resource/`

## Discord Channel Mapping

频道 ID 由 `.env` 配置（不在代码里硬编码）：

| Category | 环境变量 |
|----------|----------|
| papers   | `DISCORD_CHANNEL_PAPERS` |
| ai_news  | `DISCORD_CHANNEL_AI_NEWS` |
| code     | `DISCORD_CHANNEL_CODE` |
| resource | `DISCORD_CHANNEL_RESOURCE` |

缺失某个分类的频道 ID 时，`dailyinfo push` 会打 WARN 并跳过该分类，不会中断其他分类的推送。
