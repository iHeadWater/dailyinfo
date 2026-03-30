<!--
 * @Author: Wenyu Ouyang
 * @Date: 2026-03-20
 * @LastEditTime: 2026-03-29
 * @LastEditors: Wenyu Ouyang
 * @Description: DailyInfo: AI for Science 自动化科研情报系统
 * @FilePath: /dailyinfo/README.md
 * Copyright (c) 2023-2024 Wenyu Ouyang. All rights reserved.
-->

# DailyInfo 🌊

面向 AI for Science 的**自动化科研情报聚合与精读系统**。

基于 **FreshRSS + n8n + OpenClaw + OpenRouter** 构建的本地化学术信息流水线，实现从 RSS 订阅、AI 摘要生成到 Slack 推送的全链路自动化。

**配置驱动设计**：通过 `config/feeds.json` 管理所有数据源，添加新期刊无需修改代码或工作流。

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                    📋 配置层：config/feeds.json                      │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  定义所有数据源：feed_id、category、prompt 模板           │      │
│  │  添加新期刊只需加一条配置，无需改代码                     │      │
│  └──────────────────────┬───────────────────────────────────┘      │
└─────────────────────────┼───────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    🗄️ 采集层：FreshRSS（统一管理所有 RSS 源）        │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  学术期刊 + AI 新闻 RSS 订阅                              │      │
│  │  • Nature / Science / PNAS / arXiv CS.AI ...             │      │
│  │  • AI 新闻源（smol.ai 等）                                │      │
│  └──────────────────────┬───────────────────────────────────┘      │
│                         │ SQLite 数据库                              │
└─────────────────────────┼───────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│           🤖 处理层：n8n（只生成文件，不推送）                        │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  工作流：daily_briefing_pipeline.json                     │      │
│  │                                                           │      │
│  │  每天 06:00 AM → 读取 feeds.json → 循环每个 feed：        │      │
│  │    1️⃣ 查询 FreshRSS SQLite（按 feed_id + lookback_hours） │      │
│  │    2️⃣ 无文章 → 跳过；有文章 → 构建 Prompt                │      │
│  │    3️⃣ 调用 OpenRouter API（Claude 3.5 Sonnet）            │      │
│  │    4️⃣ 保存 Markdown 到 workspace/briefings/<category>/   │      │
│  │                                                           │      │
│  │  ⚠️ n8n 不涉及 Slack，只负责"采集 → AI 处理 → 存文件"    │      │
│  └──────────────────────┬───────────────────────────────────┘      │
│                         │ 文件系统（持久化）                          │
│                         │ workspace/briefings/                       │
│                         │   ├── papers/     ← 论文简报               │
│                         │   └── ai_news/    ← AI 新闻简报            │
└─────────────────────────┼───────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│           📤 推送层：OpenClaw Cron（容器内定时，独立于 n8n）         │
│  ┌──────────────────────────────────────────────────────────┐      │
│  │  OpenClaw 内置 cron 定时任务（07:00 / 07:05 CST）        │      │
│  │                                                           │      │
│  │  发现新文件 → OpenClaw Agent 推送到 Slack：               │      │
│  │    • briefings/papers/*   → #paper                        │      │
│  │    • briefings/ai_news/*  → #deeplearning                 │      │
│  │    • 超长内容自动分段推送                                 │      │
│  │                                                           │      │
│  │  推送后归档到 pushed/<category>/                           │      │
│  └──────────────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────────┘
```

### 核心组件

| 组件 | 功能 | 端口 | 数据持久化 |
|------|------|------|-----------|
| **FreshRSS** | RSS 订阅管理与文献存储 | 8081 | `~/.freshrss/data/` |
| **n8n** | 自动化工作流（定时查询 + AI 摘要 + 存文件） | 5678 | `~/.n8n/` |
| **OpenClaw** | Slack 推送中枢 | 18789 | `~/.openclaw/` |
| **OpenRouter** | LLM API 聚合（Claude/GPT 等） | - | 云端服务 |

### 职责分离

| 层 | 职责 | 不做什么 |
|----|------|---------|
| **n8n** | 查数据 → AI 处理 → 存文件 | ❌ 不推送 Slack |
| **OpenClaw Cron** | 扫描文件 → 推送 Slack → 归档 | ❌ 不调用 AI |

---

## 📁 目录结构

```
dailyinfo/
├── .env                              # 环境变量（API Keys，已加入 .gitignore）
├── .env.example                      # 环境变量模板
├── .gitignore                        # Git 忽略配置
├── docker-compose.yml                # 服务编排
├── Dockerfile.openclaw               # OpenClaw 定制镜像
├── README.md                         # 本文档
├── .github/
│   └── copilot-instructions.md       # Copilot CLI 项目上下文
├── config/
│   └── feeds.json                    # 📋 数据源配置（核心配置文件）
├── workflows/
│   ├── daily_briefing_pipeline.json  # n8n 统一工作流
│   └── credentials-template.md       # n8n Credentials 配置指南
└── prompts/
    └── ai_news_rewriter.txt          # AI 深度改写提示词模板（预留）
```

### 数据持久化

```yaml
# 宿主机持久化（容器重建不丢失）

~/.freshrss/data/                         # FreshRSS 数据库（所有 RSS 源）
~/.n8n/                                   # n8n 工作流和配置
~/.openclaw/                              # OpenClaw 配置
~/.openclaw/workspace/                    # 工作区根目录
    ├── briefings/                        # n8n 输出（中间产物，持久化）
    │   ├── papers/                       # 论文简报
    │   │   ├── nature_briefing_2026-03-29.md
    │   │   └── science_briefing_2026-03-29.md
    │   └── ai_news/                      # AI 新闻简报
    │       └── smolai_briefing_2026-03-29.md
    ├── pushed/                           # 推送后归档
    │   ├── papers/
    │   └── ai_news/
```

**数据流生命周期**：
```
n8n 生成 → briefings/<category>/<name>_briefing_<date>.md  [持久化]
    ↓
OpenClaw 读取 → Slack 推送                                   [推送]
    ↓
归档移动 → pushed/<category>/<name>_briefing_<date>.md      [持久化]
```

---

## 📋 配置文件：config/feeds.json

这是系统的核心配置，所有数据源在此定义。目前已配置 **35 个 RSS 源**（33 papers + 2 ai_news）：

| 类别 | 期刊/来源 | feed_id |
|------|-----------|---------|
| papers | Nature, Nature Communications, Scientific Data | 2, 3, 8 |
| papers | Science, Science Advances, Science News | 15, 16, 6 |
| papers | PNAS | 13 |
| papers | Nature Machine Intelligence, Nature Reviews Physics | 9, 10 |
| papers | Nature Sustainability, Nature Geoscience, Nature Climate Change | 18, 19, 21 |
| papers | Nature Reviews Earth & Environment | 20 |
| papers | Nature Water | 34 |
| papers | AIES, BAMS | 22, 23 |
| papers | ESSD（暂时无文章，Copernicus RSS bug） | 24 |
| papers | GMD, NHESS | 25, 26 |
| papers | JAMES, Earth and Space Science, GRL, Reviews of Geophysics, Earth's Future | 27–31 |
| papers | Remote Sensing of Environment, Global and Planetary Change | 32, 33 |
| papers | HESS, Journal of Hydrometeorology (JHM), Water Resources Research | 35, 36, 37 |
| papers | Hydrological Processes, Water Research, Advances in Water Resources, Journal of Hydrology | 38–41 |
| ai_news | SmolAI News (`lookback_hours: 48`) | 17 |
| ai_news | arXiv CS.AI（分批：10篇/批 × 5批） | 7 |

**配置示例**：

```json
{
  "version": 1,
  "defaults": {
    "model": "anthropic/claude-3.5-sonnet",
    "lookback_hours": 24,
    "prompt_template": "one_line_summary",
    "freshrss_user": "owen"
  },
  "feeds": [
    {
      "name": "nature",
      "display_name": "Nature",
      "feed_id": 2,
      "category": "papers",
      "enabled": true
    },
    {
      "name": "arxiv_cs_ai",
      "display_name": "arXiv CS.AI",
      "feed_id": 7,
      "category": "ai_news",
      "enabled": true,
      "max_articles_per_batch": 10,
      "max_batches": 5
    }
  ]
}
```

### 添加新期刊（零代码）

1. 在 FreshRSS 中订阅 RSS 源，记下 `feed_id`
2. 在 `feeds.json` 的 `feeds` 数组中添加一条：
   ```json
   {
     "name": "science",
     "display_name": "Science",
     "feed_id": 4,
     "category": "papers",
     "enabled": true
   }
   ```
3. 无需修改工作流、无需重启容器（n8n 每次执行时读取最新配置）

### 可覆盖的字段

每个 feed 可覆盖 `defaults` 中的任何字段：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `model` | OpenRouter 模型 ID | `anthropic/claude-3.5-sonnet` |
| `lookback_hours` | 查询多少小时内的文章 | `24` |
| `prompt_template` | 使用的提示词模板 key | `one_line_summary` |
| `freshrss_user` | FreshRSS 用户名 | `owen` |
| `max_articles_per_batch` | 分批处理时每批文章数（不设则不分批） | 不分批 |
| `max_batches` | 最多生成几批（与上一字段配合使用） | `10` |

> **高量期刊建议设置批处理**：Elsevier 期刊（Water Research、Journal of Hydrology、RSE 等）可能单日发布 15-100 篇，建议设置 `max_articles_per_batch: 15, max_batches: 2`，避免超大 prompt 导致 API 错误或费用激增。

**分批文件命名**：设置 `max_articles_per_batch` 后，每批生成独立文件：
```
arxiv_cs_ai_briefing_2026-03-30_batch1.md
arxiv_cs_ai_briefing_2026-03-30_batch2.md
...
```
OpenClaw 推送时逐文件发送，天然间隔，适合 arXiv 等高频更新源。

---

## 🚀 快速启动

### 1. 环境准备

```bash
# 克隆仓库
git clone <your-repo-url>
cd dailyinfo

# 创建环境变量文件
cp .env.example .env

# 编辑 .env 填入你的 API Keys
# 必需：OPENROUTER_API_KEY
```

**.env 文件示例：**
```env
# OpenRouter API Key (必需)
# 获取地址：https://openrouter.ai/keys
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxx

# 其他可选配置
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=your_password
```

### 2. 启动服务

```bash
# 一键启动所有服务
docker compose up -d

# 等待初始化（约 30 秒）
sleep 30

# 检查状态
docker compose ps
```

### 3. 初始化配置

#### 3.1 FreshRSS（RSS 源管理）

访问 http://localhost:8081

1. 选择 **SQLite** 数据库
2. 创建管理员账号（用户名需与 `feeds.json` 中的 `freshrss_user` 一致，默认 `owen`）
3. 添加 RSS 订阅并记录每个 feed 的 `feed_id`（当前已订阅的源见 `config/feeds.json`）。
   完整 RSS URL 列表（35 个源）：
   - Nature 系列：`https://www.nature.com/nature.rss`、`https://www.nature.com/ncomms.rss`、`https://www.nature.com/sdata.rss`、`https://www.nature.com/natmachintell.rss`、`https://www.nature.com/natrevphys.rss`、`https://www.nature.com/natsustain.rss`、`https://www.nature.com/ngeo.rss`、`https://www.nature.com/natrevearthenviron.rss`、`https://www.nature.com/nclimate.rss`、`https://www.nature.com/natwater.rss`
   - Science 系列：`https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science`、`https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv`、`https://www.science.org/rss/news_current.xml`
   - PNAS：`https://www.pnas.org/action/showFeed?type=etoc&feed=rss&jc=PNAS`
   - Copernicus：`https://gmd.copernicus.org/xml/rss2_0.xml`、`https://nhess.copernicus.org/articles/xml/rss2_0.xml`、`https://essd.copernicus.org/articles/xml/rss2_0.xml`（当前有 XML bug）、`https://hydrol-earth-syst-sci.net/xml/rss2_0.xml`
   - AGU/Wiley：`https://agupubs.onlinelibrary.wiley.com/feed/19422466/most-recent`（JAMES）及各 `onlinelibrary.wiley.com/rss/journal/` 地址
   - Elsevier ScienceDirect：`https://rss.sciencedirect.com/publication/science/` 系列
   - AMS：`https://journals.ametsoc.org/journalissuetocrss/journals/` 系列
   - arXiv CS.AI：`https://rss.arxiv.org/rss/cs.AI`
   - SmolAI News：`https://news.smol.ai/rss.xml`
4. 将 `feed_id` 填入 `config/feeds.json`

#### 3.2 n8n（自动化引擎）

访问 http://localhost:5678

1. 完成初始化设置
2. **Credentials 自动配置**：
   - OpenRouter API Key 已通过 `.env` 环境变量注入
   - 工作流中使用表达式：`={{ $env.OPENROUTER_API_KEY }}`
3. 导入工作流：
   - Settings → Import from File
   - 选择 `workflows/daily_briefing_pipeline.json`
4. 激活工作流：
   - 点击右上角 "Activate" 按钮

#### 3.3 定时推送（OpenClaw Cron，无需额外配置）

推送由 OpenClaw 容器内置的 cron 系统实现，与 n8n 工作流完全解耦。**无需 macOS LaunchAgent**。

cron 任务已预配置在 `~/.openclaw/cron/jobs.json` 中：

| 任务 | 时间（CST） | 目录 | Slack 频道 |
|------|-------------|------|-----------|
| `papers-daily-push` | 07:00 | `briefings/papers/` | #paper |
| `ainews-daily-push` | 07:05 | `briefings/ai_news/` | #deeplearning |

**推送逻辑**：
```
OpenClaw Agent 扫描 briefings/<category>/ 目录
    ↓
发现 .md 文件 → 读取内容
    ↓
推送到对应 Slack 频道（超长自动分段）
    ↓
推送成功后归档到 pushed/<category>/
（推送失败则保留原位，下次重试）
```

```bash
# 查看 cron 任务状态
docker exec dailyinfo_openclaw openclaw cron list

# 手动触发测试
docker exec dailyinfo_openclaw openclaw cron run --expect-final --timeout 120000 <job-id>

# 查看执行历史
docker exec dailyinfo_openclaw openclaw cron runs --id <job-id> --limit 5
```

---

## ⚙️ 自动化流程详解

### 流程一：每日简报生成（06:00 AM）

**触发**：n8n Cron 节点，每天 06:00（Asia/Shanghai）执行

**步骤**：
```
1. Read feeds.json
   └─ 从 /home/node/config/feeds.json 读取所有 enabled feed 配置

2. Loop Over Feeds（循环每个 feed）
   │
   ├─ 3. Query FreshRSS DB
   │     └─ 查询 SQLite：WHERE id_feed={feed_id} AND date > now-{lookback_hours}h
   │
   ├─ 4. Has Articles?（条件分支）
   │     ├─ 有文章 → Build Prompt → Call OpenRouter → Save Briefing
   │     │     └─ 保存到 workspace/briefings/{category}/{name}_briefing_{date}.md
   │     └─ 无文章 → Skip（跳过，不生成文件）
   │
   └─ 5. Next feed（继续下一个 feed）
```

**输出示例**（`briefings/papers/nature_briefing_2026-03-29.md`）：
```markdown
## 📚 Nature 今日简报 (2026-03-29) - 6篇文章

1. **Eye drops made from pig semen deliver cancer treatment to mice**
   > 研究人员首次利用精子细胞膜制成眼药水，可有效递送抗癌药物...

2. **Motherhood derails women's academic careers**
   > 大规模调查数据显示，生育对女性学者的学术产出和职业发展...

...

🔭 **Today's Highlight**: 今日最值得关注的研究方向是...
```

### 流程二：自动推送（OpenClaw Cron，独立于 n8n）

**触发**：OpenClaw 内置 cron，每天 07:00 / 07:05（Asia/Shanghai）

**逻辑**：
```
OpenClaw Agent 扫描 briefings/<category>/ 下的 .md 文件
    ↓
读取简报内容
    ↓
推送到对应 Slack 频道
  - papers/*   → #paper (C07N60S2M9B)
  - ai_news/*  → #deeplearning (C0562HGN6LV)
  - 超长消息自动分段
    ↓
推送成功 → 归档到 pushed/<category>/
推送失败 → 保留原位等待下次重试
```

---

## 🔧 常用命令

### 服务管理

```bash
# 查看所有服务状态
docker compose ps

# 查看日志
docker compose logs -f freshrss          # FreshRSS 日志
docker compose logs -f n8n               # n8n 执行日志
docker compose logs -f openclaw-gateway  # OpenClaw 日志

# 重启单个服务
docker compose restart n8n

# 停止所有服务
docker compose down
```

### FreshRSS 管理

```bash
# 手动刷新所有 RSS 订阅
docker exec dailyinfo_freshrss php /var/www/FreshRSS/cli/actualize-user.php --user owen

# 查看某个 feed 的文章（通过 n8n 容器，因 FreshRSS 容器无 sqlite3）
docker exec -e NODE_PATH=/usr/local/lib/node_modules/n8n/node_modules dailyinfo_n8n node -e "
const sqlite3 = require('sqlite3');
const db = new sqlite3.Database('/freshrss-data/users/owen/db.sqlite');
db.all('SELECT title FROM entry WHERE id_feed=2 ORDER BY date DESC LIMIT 5',
  (err, rows) => { rows.forEach(r => console.log(r.title)); db.close(); });
"

# 查看所有 feed_id 和文章数
docker exec -e NODE_PATH=/usr/local/lib/node_modules/n8n/node_modules dailyinfo_n8n node -e "
const sqlite3 = require('sqlite3');
const db = new sqlite3.Database('/freshrss-data/users/owen/db.sqlite');
db.all('SELECT f.id, f.name, COUNT(e.id) as n FROM feed f LEFT JOIN entry e ON e.id_feed=f.id GROUP BY f.id',
  (err, rows) => { rows.forEach(r => console.log(r.id, r.name, r.n)); db.close(); });
"
```

### n8n 工作流

```bash
# 手动触发工作流执行
# 访问 http://localhost:5678，点击 "Execute Workflow"

# 查看生成的简报文件
ls -lt ~/.openclaw/workspace/briefings/papers/
ls -lt ~/.openclaw/workspace/briefings/ai_news/

# 查看某个简报
cat ~/.openclaw/workspace/briefings/papers/nature_briefing_2026-03-29.md
```

### OpenClaw Cron 推送

```bash
# 查看 cron 任务列表
docker exec dailyinfo_openclaw openclaw cron list

# 查看最近执行记录
docker exec dailyinfo_openclaw openclaw cron runs --id <job-id> --limit 5

# 手动触发推送（调试用）
docker exec dailyinfo_openclaw openclaw cron run --expect-final --timeout 120000 <job-id>

# 禁用/启用某个推送任务
docker exec dailyinfo_openclaw openclaw cron disable <job-id>
docker exec dailyinfo_openclaw openclaw cron enable <job-id>
```

---

## 🛡️ 数据安全

### 持久化策略

**全部数据本地持久化，容器重启/重建不丢失：**

```bash
~/.freshrss/data/          # RSS 订阅与文章数据库（SQLite）
~/.n8n/                    # n8n 工作流、执行历史、加密密钥
~/.openclaw/               # OpenClaw 配置
~/.openclaw/workspace/     # 简报文件（briefings/）、推送日志、归档（pushed/）
```

### 安全建议

1. **API Keys 管理**：
   - 所有密钥存储在 `.env` 文件
   - `.env` 已加入 `.gitignore`，不会泄露
   - 通过 Docker `env_file` 注入容器
   - `config/feeds.json` 中**不含任何 API Key**

2. **备份策略**：
   ```bash
   # 定期备份（建议每周）
   tar czf backup-$(date +%Y%m%d).tar.gz \
     ~/.freshrss/data \
     ~/.n8n \
     ~/.openclaw \
     ~/dailyinfo/.env
   ```

---

## 🐛 故障排查

### Q: n8n 工作流执行失败？

**检查点：**
1. `.env` 文件中 `OPENROUTER_API_KEY` 是否正确配置
2. 环境变量是否注入容器：`docker exec dailyinfo_n8n env | grep OPENROUTER`
3. `config/feeds.json` 格式是否正确：`python3 -c "import json; json.load(open('config/feeds.json'))"`
4. n8n 日志：`docker compose logs -f n8n`

### Q: 简报文件未生成？

**检查点：**
1. `feeds.json` 中的 `feed_id` 是否与 FreshRSS 中的实际 ID 匹配
2. FreshRSS 中是否有新文章（在 `lookback_hours` 时间窗口内）
3. 数据库查询：
   ```bash
   docker exec dailyinfo_freshrss sqlite3 /var/www/FreshRSS/data/users/owen/db.sqlite \
     "SELECT COUNT(*) FROM entry WHERE id_feed = 2"
   ```
4. OpenRouter API 额度是否充足

### Q: Slack 未收到推送？

**检查点：**
1. briefings/ 目录下是否有新文件：`ls -lt ~/.openclaw/workspace/briefings/papers/`
2. OpenClaw cron 任务状态：`docker exec dailyinfo_openclaw openclaw cron list`
3. 最近推送记录：`docker exec dailyinfo_openclaw openclaw cron runs --id <job-id> --limit 3`
4. OpenClaw 是否连接 Slack：`docker compose logs openclaw-gateway | tail -20`

### Q: 容器重启后数据丢失？

**检查 volumes 映射：**
```bash
docker inspect dailyinfo_n8n | grep -A 10 "Mounts"
docker inspect dailyinfo_freshrss | grep -A 10 "Mounts"
```

---

## 📝 技术栈

- **RSS 聚合**：FreshRSS (Docker + SQLite)
- **自动化引擎**：n8n (Docker + env 变量注入)
- **AI 模型**：OpenRouter (Claude 3.5 Sonnet / GPT-4o)
- **推送中枢**：OpenClaw Gateway (Socket Mode Slack)
- **定时推送**：OpenClaw Cron（容器内，无需宿主机调度）
- **容器编排**：Docker Compose
- **配置管理**：feeds.json (配置驱动，零代码扩展)

---

## 📄 License

MIT License - Wenyu Ouyang

---

## 🙏 致谢

- [FreshRSS](https://freshrss.org/) - 开源 RSS 阅读器
- [n8n](https://n8n.io/) - 工作流自动化平台
- [OpenClaw](https://github.com/openclaw) - AI 助手框架
- [OpenRouter](https://openrouter.ai/) - LLM API 聚合服务
