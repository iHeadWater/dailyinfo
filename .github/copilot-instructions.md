# DailyInfo 项目上下文

## 项目概述

**DailyInfo** 是面向 AI for Science 研究者的学术情报自动聚合与推送系统。

核心流程：**FreshRSS 采集 → Python 脚本 / n8n AI 摘要生成（存文件） → OpenClaw Cron 定时推送到 Slack**

另有独立的**技术趋势流水线**：**Python 脚本 / n8n 直接调用 GitHub/HuggingFace API → AI 摘要 → 推送到 Slack #code**

设计原则：**配置驱动**（feeds.json / scrapers.json）+ **职责分离**（处理层只管生成文件，OpenClaw 只管推送）

---

## 架构（三层 + 配置）

```
config/feeds.json（RSS 配置中心，定义所有 RSS 数据源）
config/scrapers.json（API/抓取配置中心，12 源：GitHub Trending + HuggingFace + DUT 8 站点）
     ↓
FreshRSS（统一 RSS 采集） / API 直接调用 / HTTP 抓取
     ↓ SQLite DB / API 响应 / HTML
处理层（两种方式二选一 —— 只生成文件，不推送 Slack）
  方式 A: scripts/run_pipelines.py（推荐，宿主机直接运行）
    Pipeline 1: RSS → 学术简报（读取 FreshRSS SQLite）
    Pipeline 2: API → 技术趋势（GitHub/HuggingFace）
    Pipeline 3: HTML 抓取 → 高校资讯（DUT 站点）
  方式 B: n8n 工作流（Docker 容器内，备选）
    工作流 1: daily_briefing_pipeline.json（06:00）
    工作流 2: code_trending_pipeline.json（06:15）
    工作流 3: university_news_pipeline.json（06:30）
     ↓ Markdown 文件 (workspace/briefings/<category>/)
OpenClaw Cron（推送层 —— 容器内定时，独立于处理层）
     ↓ Slack #paper / #deeplearning / #code / #resource
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
├── AGENTS.md                               # AI 代理项目上下文
├── config/
│   ├── feeds.json                          # 📋 RSS 数据源配置（35 个源）
│   └── scrapers.json                       # 📋 API/抓取数据源配置（12 源：GitHub Trending + HuggingFace + DUT 8 站点）
├── scripts/
│   └── run_pipelines.py                    # 🐍 本地 Pipeline 运行脚本（推荐执行方式）
├── workflows/
│   ├── daily_briefing_pipeline.json        # n8n 工作流：RSS 学术简报（备选）
│   ├── code_trending_pipeline.json         # n8n 工作流：技术趋势简报（备选）
│   ├── university_news_pipeline.json       # n8n 工作流：大工院所资讯（备选）
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

**SmolAI 深度分析**：SmolAI News 配置了 `use_content: true`（读取 FreshRSS 全文而非仅标题）和 `prompt_template: "smolai_categorized"`（四分类：🧠 模型进展 / 🤖 Agent·产品 / 🔬 AI for Science / 🏭 产业新闻）。

## scrapers.json 配置规范

API/抓取类数据源配置（非 RSS），目前有 **12 个源**：
- `code` 类别（4 个）：GitHub Trending (HTML 爬取) + HuggingFace 模型/数据集/Spaces (API)
- `resource` 类别（8 个）：大工新闻网 5 板块（综合新闻/人才培养/学术科研/合作交流/一线风采）+ 3 学院（建工/未来技术/科研院）

每个 source 包含：`name`, `display_name`, `category`, `enabled`, `source_type`（api/scrape）。

**GitHub Trending**：HTML 爬取 `github.com/trending?since=daily`，解析 `article.Box-row` 提取项目信息，无需 API Token。
**HuggingFace API**：排序参数必须用 `sort=trendingScore&direction=-1`（不是 `sort=trending`）。

**DUT 网站 HTML 解析（已验证）**：
- 大工新闻网 5 板块（news.dlut.edu.cn）：标题在 `<h4><a class="l2">`（注意不是 `div.pic` 中的图片链接）
- `dlut_sche`（sche.dlut.edu.cn）：`<a class="name">` 标题，日期有 `MM-DD` 格式需补全年份
- `dlut_futureschool`（futureschool.dlut.edu.cn）：`<a class="name">` 标题
- `dlut_scidep`（scidep.dlut.edu.cn/zytz.htm）：`div.tz-ul-tt` 标题

**增量过滤**：所有 DUT/学院站点配置 `lookback_hours: 48`，仅推送 48 小时内新闻。无更新时生成 "📭 过去48小时无新内容" 提示文件。

**注意**：这些站点 HTML 结构各异，university_news_pipeline.json 用 JS Code 节点正则解析，勿改用 HTML Extract 节点。

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

## 处理层执行方式

### 方式 A：scripts/run_pipelines.py（推荐）

本地 Python 脚本，宿主机直接运行，无需 n8n 容器：

```bash
python3 scripts/run_pipelines.py              # 运行全部 3 条流水线
python3 scripts/run_pipelines.py --pipeline 1  # 仅 RSS 学术简报
python3 scripts/run_pipelines.py --pipeline 2  # 仅技术趋势
python3 scripts/run_pipelines.py --pipeline 3  # 仅大工院所资讯
```

- **Pipeline 1**（RSS 学术简报）：读取 feeds.json → 查询 FreshRSS SQLite（`~/.freshrss/data/users/owen/db.sqlite`）→ OpenRouter AI 摘要 → 保存 `briefings/<category>/<name>_briefing_<date>.md`
- **Pipeline 2**（技术趋势）：读取 scrapers.json（category=code）→ GitHub HTML 爬取 / HuggingFace API → AI 摘要 → 保存 `briefings/code/<name>_briefing_<date>.md`
- **Pipeline 3**（大工院所）：读取 scrapers.json（category=resource）→ HTTP 抓取 HTML → 正则解析 → AI 摘要 → 保存 `briefings/resource/<name>_briefing_<date>.md`
- **依赖**：`requests`（`pip install requests`），可选 `python-dotenv`
- **SmolAI 深度路径**：自动提取 HTML 全文 → 去标签 → 截断 12000 字 → 按 `smolai_categorized` 模板生成
- **批处理**：配置了 `max_articles_per_batch` 的 feed 自动分批，输出 `_batch1.md`、`_batch2.md`…
- **DUT HTML 解析**：4 种日期解析器（`dlut_news` / `standard` / `dlut_future` / `dlut_scidep`），无更新时生成 placeholder 文件
- **输出路径**：`~/.openclaw/workspace/briefings/`（与 n8n 一致）

### 方式 B：n8n 工作流（备选）

### daily_briefing_pipeline.json（RSS 学术简报）

- **触发**：每日 06:00（Asia/Shanghai）
- **节点链**：`Read feeds.json` → `Loop Over Feeds` → `Query FreshRSS DB` → `Has Articles?` → `Build Prompt` → `Call OpenRouter` → `Prepare Save` → `Save Briefing`
- **批处理**：`Build Prompt` 节点返回多个 item；`Prepare Save` 必须用 `$('Build Prompt').item`（不是 `.first()`）取对应批次数据
- **输出文件**：`/home/node/workspace/briefings/<category>/<name>_briefing_<date>[_batch<N>].md`
- **n8n 导入要求**：workflow JSON 必须有顶层 `"id"` 字段，否则报 `SQLITE_CONSTRAINT: NOT NULL`

### code_trending_pipeline.json（技术趋势简报）

- **触发**：每日 06:15（Asia/Shanghai）
- **节点链**：`Read scrapers.json` → `Loop Over Sources` → `Fetch API` → `Has Items?` → `Build Prompt` → `Call OpenRouter` → `Save Briefing`
- **数据获取**：GitHub Trending 通过 HTML 爬取（非 API），HuggingFace 通过 JSON API
- **输出文件**：`/home/node/workspace/briefings/code/code_trending_<date>.md`

### university_news_pipeline.json（大工院所资讯）

- **触发**：每日 06:30（Asia/Shanghai）
- **节点链**：`Read scrapers.json (category=resource)` → `Loop Over Sources` → `Fetch HTML (HTTP Request)` → `Parse HTML (Code/JS)` → `Has Items?` → `Build Prompt` → `Call OpenRouter` → `Save Briefing`
- **HTML 解析**：各站点结构不同，Code 节点用正则表达式提取 title/date/url
- **输出文件**：`/home/node/workspace/briefings/resource/{site_name}_{date}.md`
- **n8n 导入要求**：POST 时只传 `name/nodes/connections/settings`，不含 `id`（与 daily_briefing_pipeline 不同）

---

## OpenClaw Cron（推送层）

- **任务**：`papers-daily-push`（07:00）→ #paper；`ainews-daily-push`（07:05）→ #deeplearning；`code-daily-push`（07:10）→ #code；`resource-daily-push`（07:15）→ #resource
- **Slack 频道**：#paper = `C07N60S2M9B`，#deeplearning = `C0562HGN6LV`，#code = `C0228MSP884`，#resource = `C022CTEDJJ0`
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
    briefings/papers/                # 输出：论文简报
    briefings/ai_news/               # 输出：AI 新闻简报
    briefings/code/                  # 输出：技术趋势简报
    briefings/resource/              # 输出：大工院所资讯
    pushed/                          # 推送后归档
```

---

## 常用命令

```bash
# Pipeline 本地运行（推荐）
python3 scripts/run_pipelines.py              # 运行全部
python3 scripts/run_pipelines.py --pipeline 1  # 仅 RSS 学术简报
python3 scripts/run_pipelines.py --pipeline 2  # 仅技术趋势
python3 scripts/run_pipelines.py --pipeline 3  # 仅大工院所资讯

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

