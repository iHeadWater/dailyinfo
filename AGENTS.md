# DailyInfo - AI for Science 自动化科研情报系统

本文件为 AI 代理提供当前项目上下文。详细的代码约定、测试规则和架构说明见
[`CLAUDE.md`](CLAUDE.md)，本文只保留调度与运行入口。

---

## 项目概述

DailyInfo 是面向 AI for Science 研究者的自动化科研情报聚合与推送系统：

```text
FreshRSS / 网页抓取 / API / OpenReview
  -> dailyinfo run
  -> canonical PublicationBundle + 会议 SQLite
  -> dailyinfo push（Discord）/ publish --sink web（Web）
  -> 本地归档 + delivery state
```

六条流水线互相独立：papers、ai_news、arxiv、code、resource、conference。
常规五类进入 canonical `PublicationStore`；会议投稿与公开评审进入独立
`conference/conference.sqlite3`，只推每篇一句话的速览。

---

## 调度与备份

- 每日采集/生成/Discord 推送由 dailyinfo 自己的 `scheduler` 容器负责：
  supercronic 读 `deploy/crontab.*`，`scripts/scheduler.py` 记账、补跑、healthcheck。
- 当前 `crontab.full`：03:00 arxiv、03:30 resource、03:45 code、04:00 papers、
  04:30 ai_news、05:30/06:00/07:00 三次 Discord push。
- Web 发布不在容器里：宿主 launchd 任务每天 07:40 运行
  `dailyinfo publish --sink web`（macOS 用 `deploy/launchd/install-web-publish.sh`；
  Linux 用 systemd timer/cron）。它需要宿主上的 Node 22、npm 和 git push 凭据。
- pipeline 6（会议）尚未加入 `deploy/crontab.*`；在 follow-up #101 完成前手动运行
  `dailyinfo run -p 6` / `dailyinfo push -c conference`，或加到外部 cron。
- `dailyinfo install` 只校验 `.env`、建目录、装依赖，不写宿主 crontab。
- 数据默认在 `~/.myagentdata/dailyinfo/`，可用 `DAILYINFO_DATA_ROOT` 覆盖。
  dailyinfo 本身不做备份；myopenclaw 等备份工具可直接覆盖该目录。

---

## 数据目录

```text
~/.myagentdata/dailyinfo/
├── freshrss/data/       # FreshRSS SQLite + 配置
├── conference/          # OpenReview SQLite（投稿、回复、评审摘要）
├── briefings/           # 待推送文件（papers/ai_news/arxiv/code/resource/conference）
├── pushed/              # 推送后归档
├── publications/        # Canonical Publication v1 存储
├── deliveries/          # Discord/Web 独立投递状态
└── state/               # 运行标记与 scheduler 状态
```

---

## 常用命令

```bash
# 安装与校验
uv sync --python python3 && uv pip install -e .
dailyinfo install

# 运行流水线（幂等）
dailyinfo run
dailyinfo run -p 1   # papers
dailyinfo run -p 2   # ai_news
dailyinfo run -p 3   # arxiv
dailyinfo run -p 4   # code
dailyinfo run -p 5   # resource
dailyinfo run -p 6   # conference

# Discord / Web
dailyinfo push
dailyinfo push -c conference
dailyinfo publish --sink web

# 生产调度
docker compose up -d
docker compose up -d freshrss scheduler   # 不启动 MCP
docker compose exec scheduler python -m scripts.scheduler status
docker compose exec scheduler python -m scripts.scheduler healthcheck
./deploy/launchd/install-web-publish.sh   # macOS 宿主 Web 任务

# 测试 / 文档
uv run pytest -q
ruff check . && black .
python3 scripts/build_docs.py
uv run --group docs mkdocs build --strict
```

---

## 配置与环境

所有信息源在 `config/sources.json`；类型为 `rss` / `api` / `scrape`，类别为
`papers` / `ai_news` / `arxiv` / `code` / `resource` / `conference`。

Compose 直接读取项目 `.env`，没有独立的 `producer.env` / `push.env`。关键变量：

- 模型：`DEEPSEEK_API_KEY`（必需）、`GLM_API_KEY`（备用）
- Discord：`DISCORD_BOT_TOKEN`、`DISCORD_CHANNEL_*`
- OpenReview：`OPENREVIEW_EMAIL` / `OPENREVIEW_PASSWORD`（旧键 `OPENREVIEW_USERNAME` 可回退）
- 数据根：`DAILYINFO_DATA_ROOT`
- FreshRSS：`FRESHRSS_USER`、`FRESHRSS_BIND_ADDRESS` / `FRESHRSS_PORT`、
  `FRESHRSS_IMAGE`、`FRESHRSS_CRON_MIN`、`FRESHRSS_LANGUAGE` / `FRESHRSS_BASE_URL`
- Web：`DAILYINFO_WEB_REPO`、`DAILYINFO_WEB_REMOTE`、`DAILYINFO_WEB_BRANCH`

注意：不要在 `freshrss` 服务上设置 `FRESHRSS_USER`。镜像 entrypoint 会把它当作
`create-user.php` 的 CLI 参数；scheduler/collector 才把它当用户名。

---

## 仓库边界

dailyinfo 是纯信息情报收集器（采集 → 简报 → Discord/Web）。PDF 下载、Zotero、
深度文献分析、配图、播客和 B 站发布已迁移到 `mylibrary`，不要在本仓库新增这些能力。
本仓库保留 `ai-news-weekly-polish`（pipeline 的直接产物）。
