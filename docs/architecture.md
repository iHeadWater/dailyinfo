# System Architecture

DailyInfo 是面向 AI for Science 研究者的自动化情报聚合、简报与推送系统。

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Configuration Layer                              │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  config/sources.json (RSS + API + Scrape + OpenReview)       │   │
│  │  • 35+ RSS sources (journals + AI news)                      │   │
│  │  • Code sources (GitHub / HuggingFace)                       │   │
│  │  • Resource sources (DLUT websites)                          │   │
│  │  • Conference sources (OpenReview API v2)                    │   │
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
│  │  • OpenReview API v2 (submissions + details=replies)         │   │
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
│  │  • Pipeline 6: OpenReview → SQLite + review briefings        │   │
│  │                                                              │   │
│  │  DeepSeek API (primary); Zhipu GLM fallback (glm-5.3-flash)  │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                         ▼ Markdown files                            │
│                   ~/.myagentdata/dailyinfo/briefings/{category}/    │
└─────────────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   Delivery Layer (push / publish)                   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  scripts/push_to_discord.py + publication publishers         │   │
│  │  • load canonical PublicationBundle for today's briefing     │   │
│  │  • POST via DiscordPublisher (split > 2000 chars)            │   │
│  │  • atomically record deliveries/{sink}/{briefing}.json       │   │
│  │  • maintain pushed/{category}/ as a legacy archive           │   │
│  │  • conference digest → pushed/conference                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                         ▼ Discord channels                          │
│      #paper | #deeplearning | #conference | #code | #resource       │
└─────────────────────────────────────────────────────────────────────┘
                          ▲
                          │ triggers (e.g. 06:00 run, 07:00 push)
┌─────────────────────────────────────────────────────────────────────┐
│              Scheduling Layer (self-hosted container)               │
│  • scheduler container runs `deploy/crontab.*` via supercronic      │
│  • catch-up replays missed jobs; healthcheck turns red on staleness │
│  • backup is handled by whatever tool watches the data root         │
└─────────────────────────────────────────────────────────────────────┘
```

## Data Persistence

| Directory | Purpose | Owner |
|-----------|---------|-------|
| `~/.myagentdata/dailyinfo/freshrss/data/` | FreshRSS DB + config | dailyinfo (freshrss container) |
| `~/.myagentdata/dailyinfo/conference/` | OpenReview submissions, replies, review summaries | `conference_sync.py` |
| `~/.myagentdata/dailyinfo/briefings/` | Generated briefings (pending push) | `dailyinfo run` |
| `~/.myagentdata/dailyinfo/pushed/` | Legacy archive after successful push | `dailyinfo push` |
| `~/.myagentdata/dailyinfo/publications/` | Canonical Publication v1 store | `dailyinfo run` / `publish` |
| `~/.myagentdata/dailyinfo/deliveries/` | Authoritative sink delivery state | `DeliveryStateStore` |
| `~/.myagentdata/dailyinfo/state/` | Runtime markers and scheduler state | `dailyinfo run` / `scheduler` |

数据根默认在 `~/.myagentdata/dailyinfo/`，可通过 `DAILYINFO_DATA_ROOT` 覆盖。dailyinfo 本身不做备份；若与 myopenclaw 等支持只读挂载 `~/.myagentdata/` 的备份方案一起使用，可直接被覆盖（详见 [Agent Config](agent-config.md)）。

## Responsibility Separation

| Layer | Responsibility | Does NOT do |
|-------|----------------|-------------|
| **Processing** (`run_pipelines.py`) | RSS/API/Scrape/OpenReview → LLM → Markdown/SQLite | ❌ 推送、调度 |
| **Push** (`push_to_discord.py`) | Canonical Publication → Publisher → Discord + delivery state; conference one-line digest; maintain legacy archive | ❌ 调用 AI、调度 |
| **Scheduling** (`scheduler` container by default) | 定时触发 `dailyinfo run` / `dailyinfo push`；外部 cron/agent runtime 可选 | ❌ 业务逻辑 |

`run` 仍只负责生成 canonical content，不自动 push。`push` 对新
Publication 依据独立 delivery state 实现正常重跑 no-op；`pushed/` 仍保留
用于历史归档兼容，而不再作为新 canonical delivery 的唯一事实来源。

## Pipeline Details

### Pipeline 1: Papers
- **Input**: FreshRSS SQLite DB + scrape/API sources (30+ journals, Chinese water journals)
- **Output**: `briefings/papers/`
- **去重**：`lookback_hours > 24` 的低频源检查 `pushed/<category>/` 里的同名文件

### Pipeline 2: AI News
- **Input**: FreshRSS SQLite DB (Latent Space AINews via deep-content processing)
- **Output**: `briefings/ai_news/`

### Pipeline 3: arXiv CS.AI
- **Input**: FreshRSS SQLite DB (arXiv RSS, up to 500 articles)
- **Output**: `briefings/arxiv/`
- **特殊处理**：运行时创建 `.arxiv_generating` marker 文件，`push` 在推送前轮询等待（最长 30 分钟）

### Pipeline 4: Code Trending
- **Input**: GitHub Trending HTML + HuggingFace API
- **Output**: `briefings/code/`

### Pipeline 5: DLUT Notices & Recruitment
- **Input**: 科研院 / 研究生院 / 教务处通知（HTML）+ 招聘/实习/选调（API）
- **Output**: `briefings/resource/`（通知合并为 `dlut_notices_briefing_*.md`，招聘类按源分文件）

### Pipeline 6: OpenReview Conference Submissions
- **Input**: OpenReview API v2（ICLR 2027、ICML 2027、NeurIPS 2026），`details=replies` 批量取公开回复
- **Store**: `conference/conference.sqlite3`（投稿、reviews、paper_review_summary、events、runs）
- **Output**:
  - `briefings/conference/{source}_{date}.md`：确定性清单
  - `briefings/conference/{source}_ai_{batch_hash}.md`：详细 AI 中文评审简报
  - Discord 只推 compact 一句话速览；详细文件推送后归档到 `pushed/conference/`
- **Review signals**: 原始 rating/confidence、strong/controversial、decision、presentation、author response
- **公开性**: 只存 `readers` 包含 `everyone` 的回复；每个会期必须声明 `reviews.rating_options`
- **调度**: pipeline 6 暂未加入 `deploy/crontab.*`，目前需手动运行或由外部 cron 触发（follow-up #101）

## Discord Channel Mapping

频道 ID 由 `.env` 配置（不在代码里硬编码）：

| Category | Prod | Dev | Staging |
|----------|------|-----|---------|
| papers   | `DISCORD_CHANNEL_PAPERS` | `_PAPERS_DEV` | `_PAPERS_STAGING` |
| ai_news  | `DISCORD_CHANNEL_AI_NEWS` | `_AI_NEWS_DEV` | `_AI_NEWS_STAGING` |
| arxiv    | `DISCORD_CHANNEL_ARXIV` (falls back to `DISCORD_CHANNEL_AI_NEWS`) | `_ARXIV_DEV` | `_ARXIV_STAGING` |
| conference | `DISCORD_CHANNEL_CONFERENCE` (falls back to `DISCORD_CHANNEL_AI_NEWS`) | `_CONFERENCE_DEV` | `_CONFERENCE_STAGING` |
| code     | `DISCORD_CHANNEL_CODE` | `_CODE_DEV` | `_CODE_STAGING` |
| resource | `DISCORD_CHANNEL_RESOURCE` | `_RESOURCE_DEV` | `_RESOURCE_STAGING` |

Set `DAILYINFO_ENV=dev` or `staging` to use the suffixed keys. If the suffixed
key is empty, dev/staging falls back to the prod channel with a warning.

缺失某个分类的频道 ID 时，`dailyinfo push` 会打 WARN 并跳过该分类，不会中断其他分类的推送。

## Canonical Publication and Web delivery (Phase 2D)

Deep contract details, field semantics, freshness rules and the delivery
state schema are in [Canonical Publication Architecture](publication-architecture.md).

`dailyinfo run` produces the existing Markdown presentation and, through the
structured publication boundary, persists a validated `PublicationBundle` in
`publications/`. `dailyinfo publish --sink web` then loads only that canonical
store and sends it through `WebPublisher`; legacy `briefings/` and `pushed/`
files are not parsed as Web input.

```text
pipeline structured result
    -> PublicationFinalizer
    -> PublicationStore
    -> DeliveryCoordinator
         ├── DiscordPublisher -> Discord
         └── WebPublisher -> dailyinfo-web generated/ -> Web gates -> Git
```

Pipeline 6 is deliberately outside this canonical flow: it keeps submissions
and reviews in `conference/conference.sqlite3`, renders its own listing and AI
briefings, and pushes a compact digest. It does not create a
`PublicationBundle` and is not queryable through the five-category MCP view.

The Web sink writes only `src/content/items/generated/` and
`src/content/briefings/generated/` in the configured persistent checkout. It
requires a clean expected branch/origin, uses fetch plus fast-forward only,
holds a process lock across the complete transaction, runs validation/test/
check/build before committing, and records sink state separately under
`deliveries/web/`. A Web push failure never rolls back Discord, and a Discord
failure never rolls back a successful Web commit.
