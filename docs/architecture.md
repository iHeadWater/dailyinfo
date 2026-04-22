# System Architecture

DailyInfo is an automated research情报聚合与精读 system for AI for Science researchers.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Configuration Layer                  │
│  ┌──────────────────────────────────────────────┐  │
│  │  config/sources.json (RSS + API + Scrape)      │  │
│  │  • 35+ RSS sources (journals + AI news)     │  │
│  │  • Code sources (GitHub/HuggingFace)       │  │
│  │  • Resource sources (DLUT websites)     │  │
│  └──────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Collection Layer               │
│  ┌──────────────────────────────────────────────┐  │
│  │  FreshRSS (RSS unified collection)           │  │
│  │  • Docker container (port 8081)           │  │
│  │  • SQLite database                       │  │
│  │                                         │  │
│  │  Direct API calls (code/trending)          │  │
│  │  • GitHub Trending (HTML scraper)         │  │
│  │  • HuggingFace API                      │  │
│  │                                         │  │
│  │  HTML scraping (university news)          │  │
│  │  • DLUT websites (regex parsing)         │  │
│  └──────────────────────────────────────────────┘  │
│                         ▼ SQLite / API / HTML     │
└─────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Processing Layer                │
│  ┌──────────────────────────────────────────────┐  │
│  │  scripts/run_pipelines.py                    │  │
│  │  • Pipeline 1: RSS → AI summary → briefings │  │
│  │  • Pipeline 2: Code trending → AI summary │  │
│  │  • Pipeline 3: University news → AI sum  │  │
│  │                                         │  │
│  │  OpenRouter API (LLM aggregation)         │  │
│  │  • Default: moonshotai/kimi-k2.5         │  │
│  └──────────────────────────────────────────────┘  │
│                         ▼ Markdown files            │
│                     ~/.dailyinfo/workspace/           │
│                     briefings/{category}/         │
└─────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Push Layer                    │
│  ┌──────────────────────────────────────────────┐  │
│  │  scripts/push_to_discord.py                  │  │
│  │  • Discord Bot API (REST)                   │  │
│  │  • Auto-split long messages                 │  │
│  │  • Archive after successful push            │  │
│  └──────────────────────────────────────────────┘  │
│                         ▼ Discord channels           │
│              #paper | #deeplearning | #code | #resource   │
└─────────────────────────────────────────────────────┘
```

## Data Persistence

| Directory | Purpose | Container |
|----------|---------|----------|
| `~/.freshrss/data/` | RSS database | freshrss container |
| `~/.dailyinfo/workspace/briefings/` | Generated briefings | Host |
| `~/.dailyinfo/workspace/pushed/` | Archive after push | Host |

## Responsibility Separation

| Layer | Responsibility | Does NOT do |
|-------|---------------|------------|
| **Processing** (run_pipelines.py) | Data → AI → File | ❌ Push to Discord |
| **Push** (push_to_discord.py) | Scan → Discord → Archive | ❌ Call AI |

## Pipeline Details

### Pipeline 1: RSS Papers + AI News
- **Trigger**: crontab at 06:00 daily
- **Input**: FreshRSS SQLite DB
- **Output**: `briefings/papers/`, `briefings/ai_news/`

### Pipeline 2: Code Trending
- **Trigger**: crontab at 06:15 daily
- **Input**: GitHub Trending + HuggingFace API
- **Output**: `briefings/code/`

### Pipeline 3: University News
- **Trigger**: crontab at 06:30 daily
- **Input**: DLUT websites (HTML)
- **Output**: `briefings/resource/`

## Discord Channel Mapping

| Category | Channel | Channel ID |
|----------|---------|-----------|
| papers | #paper | `1489102139597787181` |
| ai_news | #deeplearning | `1489102139597787182` |
| code | #code | `1489102139597787183` |
| resource | #resource | `1489102139597787178` |