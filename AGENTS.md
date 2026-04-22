# DailyInfo - AI for Science 自动化科研情报系统

本文件为 AI 代理提供项目上下文和开发规范。

---

## 项目概述

**DailyInfo** 是一个面向 AI for Science 研究者的学术情报自动聚合与推送系统。

核心流程：**FreshRSS 采集 → Python 脚本 AI 摘要生成（存文件） → push_to_discord.py 定时推送到 Discord**

设计原则：**配置驱动**（sources.json）+ **职责分离**（处理层只管生成文件，推送层只管推送）

---

## 技术栈

- **RSS 聚合**：FreshRSS（Docker + SQLite）
- **处理引擎**：`scripts/run_pipelines.py`（Python, 宿主机直接运行）
- **AI 模型**：OpenRouter（moonshotai/kimi-k2.5）
- **推送脚本**：`scripts/push_to_discord.py`（Discord Bot API，宿主机 crontab 定时运行）
- **容器编排**：Docker Compose（仅 FreshRSS）

---

## 常用命令

### Pipeline 本地运行

```bash
python3 scripts/run_pipelines.py              # 运行全部
python3 scripts/run_pipelines.py --pipeline 1  # 仅 RSS 学术简报
python3 scripts/run_pipelines.py --pipeline 2  # 仅技术趋势
python3 scripts/run_pipelines.py --pipeline 3  # 仅大工院所资讯

# 或使用 CLI（uv pip install -e . 后可用）
dailyinfo run
dailyinfo run -p 1
dailyinfo push       # 手动推送到 Discord
dailyinfo status     # 查看简报文件数量
```

### 服务管理

```bash
# 启动 FreshRSS
dailyinfo start

# 停止服务
dailyinfo stop

# 查看日志
docker compose logs -f freshrss
```

### 配置验证

```bash
# 验证 sources.json 格式
python3 -c "import json; json.load(open('config/sources.json'))"
```

---

## 配置

所有数据源在 `config/sources.json` 中配置。

### 配置结构

配置文件使用统一格式，合并了原 `feeds.json` 和 `scrapers.json`：

```json
{
  "version": 2,
  "defaults": {
    "model": "moonshotai/kimi-k2.5",
    "lookback_hours": 24
  },
  "sources": [
    {
      "name": "nature",
      "type": "rss",
      "url": "https://www.nature.com/nature.rss",
      "category": "papers",
      "enabled": true
    },
    {
      "name": "github_trending",
      "type": "scrape",
      "url": "https://github.com/trending?since=daily",
      "category": "code",
      "enabled": true
    }
  ]
}
```

### 数据源类型

- `type: "rss"` — RSS 订阅（通过 FreshRSS 读取）
- `type: "api"` — 直接 API 调用（HuggingFace 等）
- `type: "scrape"` — HTML 抓取（GitHub Trending、DUT 网站）

### 分类

- `papers` — 学术论文
- `ai_news` — AI 新闻
- `code` — 技术趋势
- `resource` — 高校资讯

### 字段覆盖

每个 source 可覆盖 defaults 中的字段：
- `lookback_hours` — 查询时间窗口
- `model` — AI 模型
- `max_articles_per_batch` + `max_batches` — 批处理

---

## 工作目录

```
~/.dailyinfo/workspace/
├── briefings/          # 生成简报
│   ├── papers/
│   ├── ai_news/
│   ├── code/
│   └── resource/
└── pushed/           # 推送后归档
    ├── papers/
    ├── ai_news/
    ├── code/
    └── resource/

~/.freshrss/data/    # FreshRSS 数据库
```

---

## 输出路径

所有简报输出到 `~/.dailyinfo/workspace/briefings/{category}/`：

- Pipeline 1：`papers/`、`ai_news/`
- Pipeline 2：`code/`
- Pipeline 3：`resource/`

文件名格式：`{name}_briefing_{date}.md`

---

## 已知问题

- ESSD RSS：Copernicus 偶发 RSS 解析错误，等待自动恢复
- Elsevier 期刊：大批量发布时已通过批处理限制