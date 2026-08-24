# DailyInfo

[中文](https://github.com/iHeadWater/dailyinfo/blob/main/README.zh-CN.md) | English

DailyInfo is an automated research intelligence collector for AI for Science researchers. Every day it gathers journal papers, AI news, arXiv preprints, code trends, and university announcements from RSS feeds, scraped pages, and APIs — then turns them into concise Chinese-language briefings delivered straight to your Discord.

## How It Works

One pipeline, six independent sources of information:

```text
FreshRSS / scrape / API sources
  -> dailyinfo run
  -> AI-generated Markdown briefings (Chinese)
  -> dailyinfo push
  -> Discord channels + local archive
```

1. **Collect** — six independent pipelines pull from 40+ feeds, pages, and APIs. A failure in one never blocks the others.
2. **Summarize** — DeepSeek writes a readable Chinese briefing per source, tuned for researchers: what the papers are, why they matter, and what's worth a closer look.
3. **Deliver** — briefings land in your Discord channels and are archived locally. Nothing is ever sent twice.

You wake up to a curated digest of everything relevant to your field — no feed readers to check, no email, no noise.

## Features

| | |
|---|---|
| **Six pipelines** | Journal papers · AI news · arXiv CS.AI · GitHub + HuggingFace trends · University updates · conference papers and OpenReview lifecycle events |
| **Chinese-first briefings** | AI summaries in Chinese with automatic fallback to an OpenRouter model when the primary API fails |
| **Configuration-driven** | Add RSS, scrape, or API sources in `config/sources.json` — no code changes required |
| **Idempotent & safe to rerun** | Sources with today's briefing are skipped; pushed files are never re-sent |
| **Resilient** | Retries with exponential backoff, batch splitting on partial AI responses, per-source isolation |
| **Scheduler-agnostic** | Bring your own cron, systemd timer, or agent runtime — DailyInfo owns the pipeline, not the clock |

### Conference pipeline and actual sources

Pipeline 6 normalizes several public conference sources into one resumable
SQLite workflow at `~/.myagentdata/dailyinfo/state/openreview.sqlite3`.
The source determines which information is available:

| Enabled source | Conferences | Available data |
|---|---|---|
| OpenReview API v2 | ICLR 2026, ICML 2026, NeurIPS 2026 | Submissions plus public reviews, rebuttals, decisions, and status changes |
| ACL Anthology | ACL 2026, EMNLP 2025, NAACL 2025 | Published-paper metadata; no review lifecycle |
| CVF Open Access / ECVA | CVPR 2026, ICCV 2025, ECCV 2024 | Published-paper metadata; no review lifecycle |
| DBLP | AAAI 2026, KDD 2026, IJCAI 2026 | Bibliographic metadata; no review lifecycle |
| NeurIPS Proceedings | NeurIPS 2025 | Published-paper metadata; no review lifecycle |

Additional OpenReview venue entries for AAAI, KDD, CVPR, ACL, EMNLP, ICCV,
and NAACL are retained in `config/sources.json` but disabled. They are not
polled unless explicitly enabled after a suitable public OpenReview venue is
verified. Use the enabled canonical proceedings source shown above for those
conferences.

- New OpenReview submissions are discovered with a creation-time watermark; tracked forums are re-polled, and periodic full rescans catch other changes.
- Static proceedings sources are periodically rescanned for newly published metadata.
- Relevance uses the configured keyword/Embedding union; DeepSeek is used only for the final Chinese briefing.
- `dailyinfo status` exposes run phase, cursor progress, candidates, and errors so interrupted runs can resume safely.

Optional OpenReview authentication can be provided with
`OPENREVIEW_USERNAME` and `OPENREVIEW_PASSWORD`; public-only filtering remains
enabled by default, and OpenReview credentials are never forwarded to external
PDF or proceedings hosts.

## Screenshots

Put screenshots in `pictures/` with these names and they will render here.

### Discord Briefings

#### University Updates

![Discord university updates briefing](pictures/discord-university-updates.png)

#### Journal Papers

![Discord journal papers briefing](pictures/discord-journal-papers.png)

#### arXiv Papers

![Discord arXiv papers briefing](pictures/discord-arxiv-papers.png)

#### AI News

![Discord AI news briefing](pictures/discord-ai-news.png)

#### Code Trending

![Discord code trending briefing](pictures/discord-code-trending.png)

## Data Layout

Default data root: `~/.myagentdata/dailyinfo/`. Override it with `DAILYINFO_DATA_ROOT`.

```text
~/.myagentdata/dailyinfo/
├── freshrss/data/       # FreshRSS SQLite + config
├── briefings/           # Markdown files waiting to be pushed
│   ├── papers/
│   ├── ai_news/
│   ├── arxiv/
│   ├── code/
│   ├── conference/
│   └── resource/
└── pushed/              # Successfully pushed archive
    ├── papers/
    ├── ai_news/
    ├── arxiv/
    ├── code/
    ├── conference/
    └── resource/
```

## Quick Start

```bash
git clone <repo-url>
cd dailyinfo

cp .env.example .env
# Fill DEEPSEEK_API_KEY and DISCORD_BOT_TOKEN.

uv sync --python python3
uv pip install -e .
dailyinfo install

dailyinfo start
dailyinfo run
dailyinfo push
```

`dailyinfo install` validates `.env`, creates local data directories, and installs dependencies. It does not write crontab entries; scheduling belongs to your cron or agent runtime.

## Main Commands

| Command | Purpose |
|---------|---------|
| `dailyinfo install` | Validate environment and create data directories |
| `dailyinfo start` / `stop` / `restart` | Manage the FreshRSS container |
| `dailyinfo run` | Run all briefing pipelines |
| `dailyinfo run -p 1` | Pipeline 1: journal papers |
| `dailyinfo run -p 2` | Pipeline 2: AI news |
| `dailyinfo run -p 3` | Pipeline 3: arXiv CS.AI |
| `dailyinfo run -p 4` | Pipeline 4: code trending |
| `dailyinfo run -p 5` | Pipeline 5: university/resource |
| `dailyinfo run -p 6` | Pipeline 6: configured conference sources |
| `dailyinfo run -p 6 --source openreview_iclr_2026` | Run ICLR 2026 from OpenReview |
| `dailyinfo run -p 6 --source cvf_cvpr_2026` | Run CVPR 2026 from CVF Open Access |
| `dailyinfo status` | Show conference checkpoint phase and progress |
| `dailyinfo run -f all` | Force regeneration for all sources |
| `dailyinfo push` | Push pending briefings to Discord and archive them |
| `dailyinfo push -d 2026-04-22` | Push briefings for a specific date |
| `dailyinfo push -c weekly` | Push the weekly recap only |
| `dailyinfo weekly` | Generate a weekly AI news recap from recent briefings |
| `dailyinfo status` | Show today's briefing/archive counts |
| `dailyinfo logs` | Tail the execution log |
| `dailyinfo cache-clear` | Clear a source's FreshRSS cache (default `arxiv_cs_ai`) |
| `dailyinfo clean-cache` | Delete FreshRSS cache files older than N hours (default 24h) |

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `DEEPSEEK_API_KEY` | DeepSeek API key (primary model for `dailyinfo run`) |
| `DISCORD_BOT_TOKEN` | Discord bot token for `dailyinfo push` |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_ARXIV` / `_CODE` / `_RESOURCE` / `_CONFERENCE` | Optional category channel IDs |
| `OPENREVIEW_USERNAME` / `OPENREVIEW_PASSWORD` | Optional OpenReview authentication; set both (public-only push remains the default) |
| `FRESHRSS_USER` | FreshRSS username |
| `FRESHRSS_PASSWORD` | FreshRSS initial password |
| `DAILYINFO_DATA_ROOT` | Override default data root |
| `OPENROUTER_API_KEY` | OpenRouter API key (optional, used for fallback model) |
| `DAILYINFO_ENV` | Environment: `prod` / `dev` / `staging` (default `prod`) |
| `DAILYINFO_FALLBACK_MODEL` | Fallback model when DeepSeek returns empty (default `moonshotai/kimi-k2.5`) |

## Scheduling and Agents

DailyInfo intentionally avoids owning the scheduler. Recommended ownership:

| Responsibility | Owner |
|----------------|-------|
| FreshRSS container and local data layout | DailyInfo |
| Markdown generation via `dailyinfo run` | DailyInfo |
| Discord push/archive via `dailyinfo push` | DailyInfo |
| Timed execution | cron, myopenclaw, openclaw, or agent runtime |

## Documentation

- [Architecture](architecture.md)
- [CLI Reference](cli.md)
- [Agent Config](agent-config.md)
- [Information Sources](sources.md)

## License

BSD 3-Clause License. See [LICENSE](LICENSE) for details.
