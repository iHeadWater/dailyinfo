# DailyInfo 项目上下文

## 项目概述

**DailyInfo** 是一个面向 **AI for Science** 研究者的学术情报自动聚合与推送系统。

核心流程：**FreshRSS 采集 → n8n AI 摘要生成（存文件） → OpenClaw Slack 推送**

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
OpenClaw + macOS LaunchAgent（推送层 —— 独立于 n8n）
    ↓ Slack #paper / #deeplearning
```

---

## 服务与容器

通过 `docker-compose.yml` 编排，包含三个服务：

| 服务 | 镜像 | 端口 | 数据挂载 |
|------|------|------|----------|
| n8n | n8nio/n8n:latest | 5678 | `~/.n8n/`、`./config:ro` |
| freshrss | freshrss/freshrss:latest | 8081 | `~/.freshrss/data/` |
| openclaw-gateway | 自定义（Dockerfile.openclaw） | 18789 | `~/.openclaw/` |

---

## 关键文件

```
dailyinfo/
├── docker-compose.yml                      # 服务编排入口
├── Dockerfile.openclaw                     # 自定义 OpenClaw 镜像
├── config/
│   └── feeds.json                          # 📋 数据源配置（核心）
├── workflows/
│   ├── daily_briefing_pipeline.json        # 统一工作流（循环处理所有 feed）
│   └── credentials-template.md             # n8n Credentials 配置指南
├── prompts/
│   └── ai_news_rewriter.txt                # AI 深度改写提示词（预留）
└── .github/
    └── copilot-instructions.md             # 本文件
```

---

## feeds.json 配置

核心配置文件，所有数据源在此定义。添加新期刊只需：
1. 在 FreshRSS 订阅 RSS → 记下 feed_id
2. 在 feeds.json 添加一条 feed 配置
3. 无需改代码或重启

每个 feed 可覆盖 defaults（model、lookback_hours、prompt_template、freshrss_user）。

---

## 工作流说明

### daily_briefing_pipeline.json（统一工作流）
- **触发**：每日 06:00（Asia/Shanghai）
- **步骤**：读取 feeds.json → 循环每个 enabled feed → 查 FreshRSS SQLite → 有文章则 AI 摘要 → 保存到 `briefings/<category>/<name>_briefing_<date>.md`
- **不做**：不推送 Slack（由 OpenClaw 负责）

### Slack 推送（macOS LaunchAgent + OpenClaw）
- **触发**：每 5 分钟轮询
- **逻辑**：扫描 briefings/ → 新文件通过 OpenClaw 推送 → 归档到 pushed/
- **频道映射**：papers → #paper，ai_news → #deeplearning

---

## 技术栈

- **RSS 聚合**：FreshRSS（SQLite，用户名 owen）
- **工作流编排**：n8n（Cron + Code + HTTP Request 节点）
- **AI/LLM**：OpenRouter API（Claude 3.5 Sonnet）
- **Slack 推送**：OpenClaw Gateway（Socket Mode）
- **调度**：n8n Cron（容器内）+ macOS LaunchAgent（宿主机）
- **配置管理**：feeds.json（配置驱动，零代码扩展）

---

## 持久化设计

```
宿主机持久化（容器重建不丢失）
├── ~/.freshrss/data/                    # FreshRSS 数据库
├── ~/.n8n/                              # n8n 工作流和配置
├── ~/.openclaw/                         # OpenClaw 配置
└── ~/.openclaw/workspace/
    ├── briefings/papers/                # 论文简报（n8n 输出）
    ├── briefings/ai_news/               # AI 新闻简报（n8n 输出）
    ├── pushed/                          # 推送后归档
    └── .push.log                        # 推送日志

代码库中（git 管理）
├── config/feeds.json                    # 数据源配置（无 API Key）
├── workflows/                           # 工作流模板
└── prompts/                             # 提示词模板
```

---

## 环境变量（`.env`）

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxx   # 必填
N8N_BASIC_AUTH_USER=admin            # 可选
N8N_BASIC_AUTH_PASSWORD=xxxx         # 可选
```

n8n 通过 `N8N_ENV_VARS_IN_DEC=true` + Docker `env_file` 注入，工作流用 `{{ $env.OPENROUTER_API_KEY }}` 引用。

---

## 常用命令

```bash
docker compose up -d                # 启动所有服务
docker compose ps                   # 查看状态
docker compose logs -f n8n          # 查看 n8n 日志
ls ~/.openclaw/workspace/briefings/ # 查看生成的简报
python3 -c "import json; json.load(open('config/feeds.json'))"  # 验证配置
```
