# DailyInfo - AI for Science 自动化科研情报系统

本文件为 AI 代理提供项目上下文和开发规范。

---

## 项目概述

**DailyInfo** 是一个面向 AI for Science 研究者的学术情报自动聚合与推送系统。

核心流程：**FreshRSS 采集 → Python 脚本 / n8n AI 摘要生成（存文件） → push_to_discord.py 定时推送到 Discord**

设计原则：**配置驱动**（sources.json）+ **职责分离**（处理层只管生成文件，推送层只管推送）

---

## 技术栈

- **RSS 聚合**：FreshRSS（Docker + SQLite）
- **处理引擎**：`scripts/run_pipelines.py`（Python, 宿主机直接运行）/ n8n（Docker, 备选）
- **AI 模型**：OpenRouter（Claude Haiku 4.5）
- **推送脚本**：`scripts/push_to_discord.py`（Discord Bot API，宿主机 crontab 定时运行）
- **OpenClaw Gateway**：飞书/Feishu 推送中枢（Discord 已迁移到独立 push 脚本）
- **容器编排**：Docker Compose

---

## 常用命令

### Pipeline 本地运行（推荐）

```bash
python3 scripts/run_pipelines.py              # 运行全部
python3 scripts/run_pipelines.py --pipeline 1  # 仅 RSS 学术简报
python3 scripts/run_pipelines.py --pipeline 2  # 仅技术趋势
python3 scripts/run_pipelines.py --pipeline 3  # 仅大工院所资讯

# 或使用 CLI（pip install -e . 后可用）
dailyinfo run
dailyinfo run -p 1
dailyinfo push       # 手动推送到 Discord
dailyinfo status     # 查看简报文件数量
```

### 服务管理

```bash
# 启动所有服务
docker compose up -d

# 查看服务状态
docker compose ps

# 查看日志
docker compose logs -f n8n
docker compose logs -f freshrss
docker compose logs -f openclaw-gateway

# 重启单个服务
docker compose restart n8n

# 停止所有服务
docker compose down
```

### 配置验证

```bash
# 验证 sources.json 格式
python3 -c "import json; json.load(open('config/sources.json'))"

# 验证环境变量注入
docker exec dailyinfo_n8n env | grep OPENROUTER
```

### OpenClaw Cron 管理（飞书推送）

推送机制：cron 任务设置 `delivery.mode: "none"`（`--no-deliver`），agent 通过 `exec` 工具调用 Discord REST API（`curl` 或 `python3 urllib`）直接发送到 Discord。不使用 `openclaw message send` CLI（Node.js v24 兼容性 bug），也不使用 `announce` delivery 模式（与 Discord 路由不兼容）。

Discord 频道 ID：#paper=`1492000490748117062`、#deeplearning=`1492815895439999016`、#code=`1492814957123993600`、#resource=`1492815107443658762`

```bash
# 查看 cron 任务列表
docker exec dailyinfo_openclaw openclaw cron list

# 手动触发推送
docker exec dailyinfo_openclaw openclaw cron run --expect-final --timeout 120000 <job-id>

# 禁用/启用任务
docker exec dailyinfo_openclaw openclaw cron disable <job-id>
docker exec dailyinfo_openclaw openclaw cron enable <job-id>
```

### FreshRSS 管理

```bash
# 手动刷新 RSS
docker exec dailyinfo_freshrss php /var/www/FreshRSS/cli/actualize-user.php --user owen

# 查看某个 feed 的文章（通过 n8n 容器）
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

---

## 目录结构

```
dailyinfo/
├── .env                              # 环境变量（API Keys，已加入 .gitignore）
├── .env.example                      # 环境变量模板
├── .gitignore                        # Git 忽略配置
├── docker-compose.yml                # 服务编排入口
├── Dockerfile.openclaw               # 自定义 OpenClaw 镜像（含 SkillHub CLI）
├── setup.sh                          # 一键安装脚本
├── pyproject.toml                    # Python 包定义（dailyinfo CLI）
├── README.md                         # 项目说明文档
├── AGENTS.md                         # AI 代理项目上下文
├── config/
│   └── sources.json                  # 📋 统一数据源配置（RSS + API + 抓取，核心）
├── scripts/
│   ├── run_pipelines.py              # 🐍 本地 Pipeline 运行脚本（推荐执行方式）
│   ├── push_to_discord.py            # 📤 Discord 推送脚本（宿主机 crontab 07:00 运行）
│   ├── datasource.py                 # DataSource 抽象层（RSS/API/Scrape 实现）
│   └── cli.py                        # dailyinfo CLI 入口
├── workflows/
│   ├── daily_briefing_pipeline.json  # n8n 统一工作流（备选）
│   ├── code_trending_pipeline.json   # n8n 技术趋势工作流（备选）
│   ├── university_news_pipeline.json # n8n 大工院所工作流（备选）
│   └── credentials-template.md       # Credentials 配置指南
├── prompts/
│   └── ai_news_rewriter.txt          # AI 深度改写提示词模板
└── .github/
    └── copilot-instructions.md       # Copilot 项目上下文
```

---

## 代码规范

### sources.json 配置规范（核心配置文件）

`config/sources.json` 是唯一的数据源配置文件，同时管理 RSS、API 和抓取类数据源。

- 使用 2 空格缩进
- 顶层字段顺序：`version` → `defaults` → `prompt_templates` → `sources` → `discord_channels`
- 每个 source 必须包含：`name`, `display_name`, `category`, `enabled`, `type`
- RSS 类型（`"type": "rss"`）必须包含 `url` 字段，否则代码无法通过 URL 匹配 FreshRSS feed_id，导致该源永远查不到文章
- `category` 只允许：`papers`, `ai_news`, `code`, `resource`
- `type` 只允许：`rss`, `api`, `scrape`

**RSS 源必填字段**：
```json
{
  "name": "nature",
  "display_name": "Nature",
  "category": "papers",
  "enabled": true,
  "url": "https://www.nature.com/nature.rss",
  "type": "rss"
}
```

**可选高级配置**：
- `max_articles_per_batch` + `max_batches`：分批处理，适用于高量期刊（Elsevier、WRR、GRL 等）
- `max_articles`：限制总文章数
- `use_content: true`：读取全文（用于 SmolAI News）
- `prompt_template`：指定提示词模板 key
- `lookback_hours`：覆盖默认查询窗口（默认 24h）
- `model`：覆盖默认 OpenRouter 模型

**SmolAI 深度分析**：SmolAI News 配置了 `use_content: true`（读取 FreshRSS 全文而非仅标题）和 `prompt_template: "smolai_categorized"`（四分类分析：🧠 模型进展 / 🤖 Agent·产品 / 🔬 AI for Science / 🏭 产业新闻）。

### API/抓取类数据源（sources.json 中的 code/resource 源）

目前配置了以下非 RSS 源：
- `code` 类别（4 个）：GitHub Trending (HTML 爬取, `"type": "scrape"`) + HuggingFace 模型/数据集/Spaces (`"type": "api"`)
- `resource` 类别（11 个）：大工新闻网 5 板块 + 3 学院 + 招聘/实习/选调（`"type": "api"` 调用 job.dlut.edu.cn）

**GitHub Trending**：HTML 爬取 `github.com/trending?since=daily`，解析 `article.Box-row` 提取项目信息，无需 API Token。
**HuggingFace API**：排序参数必须用 `sort=trendingScore&direction=-1`（不是 `sort=trending`）。

**DUT 网站 HTML 解析（已验证）**：
- 大工新闻网 5 板块（news.dlut.edu.cn）：标题在 `<h4><a class="l2">`，date_format 为 `dlut_news`
- `dlut_sche`（sche.dlut.edu.cn）：date_format 为 `standard`，日期有 `MM-DD` 格式需补全年份
- `dlut_futureschool`（futureschool.dlut.edu.cn）：date_format 为 `dlut_future`
- `dlut_scidep`（scidep.dlut.edu.cn/zytz.htm）：date_format 为 `dlut_scidep`，`div.tz-ul-tt` 标题

**增量过滤**：所有 DUT/学院站点配置 `lookback_hours: 24`，仅推送指定时间窗口内新闻。无更新时生成 "📭 过去 N 小时无新内容" 提示文件。

**注意**：这些站点 HTML 结构各异，`scripts/run_pipelines.py` 中用 Python 正则解析（`datasource.py` 中的 `_parse_dlut_html`），n8n 工作流中用 JS Code 节点正则解析。

### scripts/run_pipelines.py 规范

本地 Python 脚本，可在宿主机直接运行三条流水线，无需 n8n 容器：
```bash
python3 scripts/run_pipelines.py              # 运行全部
python3 scripts/run_pipelines.py --pipeline 1  # 仅 RSS 学术简报
python3 scripts/run_pipelines.py --pipeline 2  # 仅技术趋势
python3 scripts/run_pipelines.py --pipeline 3  # 仅大工院所资讯
```

- 读取 `config/sources.json`（统一配置），输出到 `~/.openclaw/workspace/briefings/`
- 依赖：`requests`，可选 `python-dotenv`（`pip install -e .` 一并安装）
- 从 `.env` 读取 `OPENROUTER_API_KEY`
- 直接读取宿主机 `~/.freshrss/data/users/owen/db.sqlite`
- **RSS feed 匹配机制**：通过 `url` 字段与 FreshRSS DB 中的 feed URL 精确匹配（或 base URL 模糊匹配）。sources.json 中缺少 `url` 的 RSS 源将永远查不到文章。

### scripts/push_to_discord.py 规范

宿主机 crontab 每日 07:00 自动运行（由 `setup.sh` 安装）：
- 扫描 `~/.openclaw/workspace/briefings/<category>/` 下今日 `.md` 文件
- 使用 `DISCORD_BOT_TOKEN` 调用 Discord REST API v10
- 超长消息自动分段（单段 ≤1950 字符，按行切割）
- 推送成功后归档到 `~/.openclaw/workspace/pushed/<category>/`
- 过滤 placeholder 文件（`📭 过去...无新内容`）和低质量内容，不推送

### n8n 工作流规范（备选方式）

- 工作流文件使用 `.json` 格式
- 导入前验证 JSON 语法：`python3 -c "import json; json.load(open('workflows/xxx.json'))"`
- 敏感凭证通过环境变量注入，不硬编码在 workflow 中
- 使用表达式引用环境变量：`{{ $env.VARIABLE_NAME }}`

### Docker 相关规范

- 所有服务通过 `docker-compose.yml` 编排
- 环境变量存储在 `.env`（已加入 .gitignore）
- 持久化数据映射到宿主机 `~/.freshrss/`、`~/.n8n/`、`~/.openclaw/`、`~/.mineru/`

### Skill 管理（SkillHub CLI）

- SkillHub CLI（腾讯 Skill 市场）预装在 `Dockerfile.openclaw` 中，容器重建不丢失
- Skill 数据安装到 `~/.openclaw/workspace/skills/`，通过 Docker volume 自动持久化
- 常用命令：
  ```bash
  docker exec dailyinfo_openclaw skillhub search <关键词>   # 搜索
  docker exec dailyinfo_openclaw skillhub install <slug>     # 安装
  docker exec dailyinfo_openclaw skillhub list               # 已安装列表
  docker exec dailyinfo_openclaw skillhub upgrade             # 升级 Skill
  docker exec dailyinfo_openclaw skillhub self-upgrade        # 升级 CLI
  ```

---

## 故障排查清单

当遇到问题时，按以下顺序排查：

1. **配置问题**：验证 `config/sources.json` 格式正确
   ```bash
   python3 -c "import json; json.load(open('config/sources.json'))"
   ```
2. **RSS 源匹配问题**：确认 sources.json 中所有 RSS 源都有 `url` 字段，否则代码无法匹配 FreshRSS feed_id
3. **环境变量**：检查 `OPENROUTER_API_KEY` 和 `DISCORD_BOT_TOKEN` 已正确配置
4. **数据库**：确认 FreshRSS 中有文章
   ```bash
   docker exec -e NODE_PATH=/usr/local/lib/node_modules/n8n/node_modules dailyinfo_n8n node -e "
   const sqlite3 = require('sqlite3');
   const db = new sqlite3.Database('/freshrss-data/users/owen/db.sqlite');
   db.all('SELECT id, name, cache_nbEntries FROM feed ORDER BY id', (err, rows) => {
     rows.forEach(r => console.log(r.id, r.name, r.cache_nbEntries)); db.close();
   });"
   ```
5. **日志检查**：
   ```bash
   docker compose logs -f n8n
   docker compose logs openclaw-gateway | tail -20
   tail -f logs/pipeline1.log
   tail -f logs/discord_push.log
   ```

---

## 添加新期刊（零代码）

1. 在 FreshRSS 订阅 RSS 源（访问 http://localhost:8081）
2. 在 `config/sources.json` 的 `sources` 数组添加（**必须包含 `url` 字段**）：
   ```json
   {
     "name": "science",
     "display_name": "Science",
     "url": "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
     "category": "papers",
     "enabled": true,
     "type": "rss"
   }
   ```
3. 无需改代码或重启容器

---

## Discord 频道映射

| Category | Discord Channel |
|----------|---------------|
| papers | #paper |
| ai_news | #deeplearning |
| code | #code |
| resource | #resource |

Discord 频道 ID（push_to_discord.py 中硬编码，与 OpenClaw cron jobs 保持一致）：
- #paper = `1489102139597787181`
- #deeplearning = `1489102139597787182`
- #code = `1489102139597787183`
- #resource = `1489102139597787178`

## 飞书（Feishu/Lark）

OpenClaw 同时配置了飞书插件（WebSocket 模式），支持私聊（`dmPolicy: "open"`）和群聊（`groupPolicy: "allowlist"`）。
配置位于 `~/.openclaw/openclaw.json` → `channels.feishu`。

---

## 环境变量（.env）

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxx   # 必填
DISCORD_BOT_TOKEN=your_token_here   # 必填，用于 push_to_discord.py
FRESHRSS_PASSWORD=freshrss123       # 可选，FreshRSS 账号密码
N8N_BASIC_AUTH_USER=admin           # 可选
N8N_BASIC_AUTH_PASSWORD=xxxx        # 可选
```

---

## 注意事项

1. **不提交敏感信息**：`.env` 已加入 .gitignore，切勿提交包含真实 API Key 的配置
2. **JSON 语法正确**：修改 sources.json 后务必验证 JSON 格式
3. **RSS 源必须有 url**：sources.json 中 `"type": "rss"` 的源必须包含 `url` 字段，代码通过 URL 匹配 FreshRSS feed_id，缺失 url 会导致该期刊永远不被处理
4. **职责分离**：处理层（Python 脚本 / n8n）只负责"采集→AI 处理→存文件"，不推送 Discord；`push_to_discord.py` 只负责推送
5. **Cron 独立性**：宿主机 crontab 定时任务独立于处理层，处理与推送互不影响
