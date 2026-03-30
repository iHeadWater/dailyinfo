# DailyInfo - AI for Science 自动化科研情报系统

本文件为 AI 代理提供项目上下文和开发规范。

---

## 项目概述

**DailyInfo** 是一个面向 AI for Science 研究者的学术情报自动聚合与推送系统。

核心流程：**FreshRSS 采集 → n8n AI 摘要生成（存文件） → OpenClaw Cron 定时推送到 Slack**

设计原则：**配置驱动**（feeds.json）+ **职责分离**（n8n 只管生成文件，OpenClaw Cron 只管推送）

---

## 技术栈

- **RSS 聚合**：FreshRSS（Docker + SQLite）
- **自动化引擎**：n8n（Docker + env 变量注入）
- **AI 模型**：OpenRouter（Claude 3.5 Sonnet / GPT-4o）
- **推送中枢**：OpenClaw Gateway（Socket Mode Slack）
- **容器编排**：Docker Compose

---

## 常用命令

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
# 验证 feeds.json 格式
python3 -c "import json; json.load(open('config/feeds.json'))"

# 验证环境变量注入
docker exec dailyinfo_n8n env | grep OPENROUTER
```

### OpenClaw Cron 管理

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
├── Dockerfile.openclaw               # 自定义 OpenClaw 镜像
├── README.md                         # 项目说明文档
├── config/
│   └── feeds.json                    # 📋 数据源配置（核心）
├── workflows/
│   ├── daily_briefing_pipeline.json  # n8n 统一工作流
│   └── credentials-template.md       # Credentials 配置指南
├── prompts/
│   └── ai_news_rewriter.txt          # AI 深度改写提示词模板
└── .github/
    └── copilot-instructions.md       # Copilot 项目上下文
```

---

## 代码规范

### feeds.json 配置规范

- 使用 2 空格缩进
- 字段按以下顺序排列：`version` → `defaults` → `feeds` → `prompt_templates` → `slack_channels`
- 每个 feed 必须包含：`name`, `display_name`, `feed_id`, `category`, `enabled`
- 可选的高级配置：`max_articles_per_batch`, `max_batches` 用于分批处理（适用于更新量大的期刊，如 arxiv_cs_ai 或 Elsevier 期刊）
- `display_name` 使用英文首字母大写（如 "Nature Communications"）
- `category` 只允许：`papers`, `ai_news`

### n8n 工作流规范

- 工作流文件使用 `.json` 格式
- 导入前验证 JSON 语法：`python3 -c "import json; json.load(open('workflows/xxx.json'))"`
- 敏感凭证通过环境变量注入，不硬编码在 workflow 中
- 使用表达式引用环境变量：`{{ $env.VARIABLE_NAME }}`

### Docker 相关规范

- 所有服务通过 `docker-compose.yml` 编排
- 环境变量存储在 `.env`（已加入 .gitignore）
- 持久化数据映射到宿主机 `~/.freshrss/`、`~/.n8n/`、`~/.openclaw/`

---

## 故障排查清单

当遇到问题时，按以下顺序排查：

1. **配置问题**：验证 `config/feeds.json` 格式正确
2. **环境变量**：检查 `OPENROUTER_API_KEY` 已正确配置
3. **数据库**：确认 FreshRSS 中 feed_id 对应的文章存在
4. **日志检查**：
   ```bash
   docker compose logs -f n8n
   docker compose logs openclaw-gateway | tail -20
   ```

---

## 添加新期刊（零代码）

1. 在 FreshRSS 订阅 RSS 源，记下 `feed_id`
2. 在 `config/feeds.json` 的 `feeds` 数组添加：
   ```json
   {
     "name": "science",
     "display_name": "Science",
     "feed_id": <新id>,
     "category": "papers",
     "enabled": true
   }
   ```
3. 无需改代码或重启容器

---

## Slack 频道映射

| Category | Slack Channel |
|----------|---------------|
| papers | #paper |
| ai_news | #deeplearning |

---

## 环境变量（.env）

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxx   # 必填
N8N_BASIC_AUTH_USER=admin            # 可选
N8N_BASIC_AUTH_PASSWORD=xxxx         # 可选
```

---

## 注意事项

1. **不提交敏感信息**：`.env` 已加入 .gitignore，切勿提交包含真实 API Key 的配置
2. **JSON 语法正确**：修改 feeds.json 后务必验证 JSON 格式
3. **职责分离**：n8n 只负责"采集→AI 处理→存文件"，不推送 Slack；OpenClaw 只负责推送
4. **Cron 独立性**：OpenClaw cron 任务独立于 n8n 工作流，即使 n8n 手动执行也不影响定时推送