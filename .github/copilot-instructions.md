# DailyInfo 项目上下文

## 项目概述

**DailyInfo** 是面向 AI for Science 研究者的学术情报自动聚合与推送系统。

核心流程：**FreshRSS 采集 → n8n AI 摘要生成（存文件） → OpenClaw Cron 定时推送到 Slack**

设计原则：**配置驱动**（feeds.json）+ **职责分离**（n8n 只管生成文件，OpenClaw 只管推送）

---

## 架构（三层 + 配置）

```
config/feeds.json（配置中心，定义所有数据源）
    ↓
FreshRSS（统一 RSS 采集）
    ↓ SQLite DB (~/.freshrss/data/users/owen/db.sqlite)
n8n（处理层 —— 只生成文件，不推送 Slack）
    ↓ Markdown 文件 (workspace/briefings/<category>/)
OpenClaw Cron（推送层 —— 容器内定时，独立于 n8n）
    ↓ Slack #paper / #deeplearning
```

---

## 服务与容器

| 服务 | 容器名 | 端口 | 数据挂载 |
|------|--------|------|----------|
| n8n | `dailyinfo_n8n` | 5678 | `~/.n8n/`、`./config:ro`、`~/.freshrss/data:ro` |
| freshrss | `dailyinfo_freshrss` | 8081 | `~/.freshrss/data/` |
| openclaw-gateway | `dailyinfo_openclaw` | 18789 | `~/.openclaw/` |

---

## 关键文件

```
dailyinfo/
├── docker-compose.yml                      # 服务编排入口
├── Dockerfile.openclaw                     # 自定义 OpenClaw 镜像
├── config/
│   └── feeds.json                          # 📋 数据源配置（核心，35 个源）
├── workflows/
│   ├── daily_briefing_pipeline.json        # 统一工作流（n8n 导入用）
│   └── credentials-template.md             # n8n Credentials 配置指南
└── prompts/
    └── ai_news_rewriter.txt                # AI 深度改写提示词（预留）
```

---

## feeds.json 配置规范

核心配置文件，目前有 **35 个 RSS 源**（33 papers + 2 ai_news）。

每个 feed 必须包含：`name`, `display_name`, `feed_id`, `category`, `enabled`。
字段顺序：`version` → `defaults` → `feeds` → `prompt_templates` → `slack_channels`。
`category` 只允许：`papers` 或 `ai_news`。

**可覆盖字段**（在 feed 级别覆盖 defaults）：
- `lookback_hours`：查询多少小时内的文章（默认 24）
- `max_articles_per_batch` + `max_batches`：分批处理，适用于高量期刊
- `model`：OpenRouter 模型 ID

**批处理已配置的 feeds**：
- arXiv CS.AI：`max_articles_per_batch: 10, max_batches: 5`（每日最多 50 篇）
- Elsevier 期刊（Water Research/Journal of Hydrology/RSE/Advances in Water Resources/GPC）：`max_articles_per_batch: 15, max_batches: 2`（每日最多 30 篇）

**添加新期刊（零代码）**：
1. FreshRSS 中订阅 RSS → 记下 `feed_id`
2. `config/feeds.json` 中加一条配置
3. 无需改代码或重启容器

---

## n8n 工作流（daily_briefing_pipeline.json）

- **触发**：每日 06:00（Asia/Shanghai）
- **节点链**：`Read feeds.json` → `Loop Over Feeds` → `Query FreshRSS DB` → `Has Articles?` → `Build Prompt` → `Call OpenRouter` → `Prepare Save` → `Save Briefing`
- **批处理**：`Build Prompt` 节点返回多个 item；`Prepare Save` 必须用 `$('Build Prompt').item`（不是 `.first()`）取对应批次数据
- **输出文件**：`/home/node/workspace/briefings/<category>/<name>_briefing_<date>[_batch<N>].md`
- **n8n 导入要求**：workflow JSON 必须有顶层 `"id"` 字段，否则报 `SQLITE_CONSTRAINT: NOT NULL`

---

## OpenClaw Cron（推送层）

- **任务**：`papers-daily-push`（07:00）→ #paper；`ainews-daily-push`（07:05）→ #deeplearning
- **Slack 频道**：#paper = `C07N60S2M9B`，#deeplearning = `C0562HGN6LV`
- **关键陷阱**：`delivery.mode` 必须为 `"none"`，否则报错 "Delivering to Slack requires target"。**不要直接编辑** `~/.openclaw/cron/jobs.json`（重启会覆盖），必须用 CLI：
  ```bash
  docker exec dailyinfo_openclaw openclaw cron edit <job-id> --no-deliver
  ```
- **Slack 频道白名单**：`~/.openclaw/openclaw.json` → `channels.slack.channels`，新频道需手动添加并在 Slack 执行 `/invite @OpenClaw`

---

## FreshRSS 操作要点

**重要限制**：
- FreshRSS 容器内**没有 sqlite3 二进制**，不能用 `docker exec dailyinfo_freshrss sqlite3`
- n8n 挂载 FreshRSS DB 为**只读**（`:ro`），不能在 n8n 容器内写 DB

**读取 DB**（通过 n8n 容器的 Node.js sqlite3 模块）：
```bash
docker exec -e NODE_PATH=/usr/local/lib/node_modules/n8n/node_modules dailyinfo_n8n node -e "
const sqlite3 = require('sqlite3');
const db = new sqlite3.Database('/freshrss-data/users/owen/db.sqlite');
db.all('SELECT id, name, cache_nbEntries FROM feed ORDER BY id', (err, rows) => {
  rows.forEach(r => console.log(r.id, r.name, r.cache_nbEntries));
  db.close();
});
"
```

**写入 DB**（只能在宿主机用 Python）：
```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('/Users/owen/.freshrss/data/users/owen/db.sqlite')
conn.execute('UPDATE feed SET url=?, lastUpdate=0 WHERE id=?', ('new_url', feed_id))
conn.commit(); conn.close()
"
```

**批量添加 RSS 源**（OPML 导入）：
```bash
docker cp feeds.opml dailyinfo_freshrss:/tmp/feeds.opml
docker exec dailyinfo_freshrss php /var/www/FreshRSS/cli/import-for-user.php --user=owen --filename=/tmp/feeds.opml
```

**强制刷新 feeds**：
```bash
# 先在宿主机重置 TTL
python3 -c "import sqlite3; conn=sqlite3.connect('/Users/owen/.freshrss/data/users/owen/db.sqlite'); conn.execute('UPDATE feed SET lastUpdate=0 WHERE id IN (...)'); conn.commit()"
# 再触发抓取
docker exec dailyinfo_freshrss php /var/www/FreshRSS/cli/actualize-user.php --user=owen
```

注意：脚本名是 `actualize-user.php`，不是 `actualize-for-user.php`（后者不存在）。

---

## 环境变量（.env）

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxx   # 必填
N8N_BASIC_AUTH_USER=admin            # 可选
N8N_BASIC_AUTH_PASSWORD=xxxx         # 可选
```

n8n 通过 `N8N_ENV_VARS_IN_DEC=true` + `env_file` 注入；工作流用 `{{ $env.OPENROUTER_API_KEY }}` 引用。

---

## 持久化路径

```
~/.freshrss/data/                    # FreshRSS SQLite DB
~/.n8n/                              # n8n 工作流和配置
~/.openclaw/                         # OpenClaw 配置（含 cron/jobs.json、openclaw.json）
~/.openclaw/workspace/
    briefings/papers/                # n8n 输出：论文简报
    briefings/ai_news/               # n8n 输出：AI 新闻简报
    pushed/                          # 推送后归档
```

---

## 常用命令

```bash
# 服务管理
docker compose up -d
docker compose ps
docker compose logs -f n8n

# 配置验证
python3 -c "import json; json.load(open('config/feeds.json'))"
docker exec dailyinfo_n8n env | grep OPENROUTER

# OpenClaw Cron
docker exec dailyinfo_openclaw openclaw cron list
docker exec dailyinfo_openclaw openclaw cron run --expect-final --timeout 120000 <job-id>
docker exec dailyinfo_openclaw openclaw cron edit <job-id> --no-deliver

# 查看所有 feed 状态（feed_id、名称、文章数）
docker exec -e NODE_PATH=/usr/local/lib/node_modules/n8n/node_modules dailyinfo_n8n node -e "
const sqlite3 = require('sqlite3');
const db = new sqlite3.Database('/freshrss-data/users/owen/db.sqlite');
db.all('SELECT id, name, cache_nbEntries FROM feed ORDER BY id', (err, rows) => {
  rows.forEach(r => console.log(r.id, r.name, r.cache_nbEntries)); db.close();
});
"
```

---

## 已知问题

- **ESSD RSS**：Copernicus 的 ESSD RSS 偶发未转义 `&` 字符（XML 解析失败）。FreshRSS 无法解析时 `cache_nbEntries=0`，n8n 工作流会自动跳过，无需人工干预。等 Copernicus 修复后会自动恢复。
- **Elsevier ScienceDirect RSS**：首次抓取可能返回 25-100 篇文章（批量发布日期），已通过 `max_articles_per_batch: 15, max_batches: 2` 限制。

