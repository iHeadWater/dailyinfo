# DailyInfo

面向 AI for Science 研究者的自动化科研情报聚合与推送系统。

**核心流程**：RSS / API / 网页抓取 → OpenRouter LLM 中文摘要 → markdown 落盘 → Discord 推送 + 归档

**设计原则**：配置驱动（`config/sources.json`）+ 幂等 CLI（可重复执行无副作用）+ 外部 cron 调度

---

## 快速开始

```bash
git clone https://github.com/OuyangWenyu/dailyinfo.git
cd dailyinfo

cp .env.example .env
# 编辑 .env，至少填入 OPENROUTER_API_KEY 与 DISCORD_BOT_TOKEN

uv sync --python python3
uv pip install -e .

dailyinfo install   # 校验 .env、创建数据目录
dailyinfo start     # 启动 FreshRSS 容器（首次访问 http://localhost:8081 创建账号并订阅）

dailyinfo run       # 跑一次完整流水线
dailyinfo push      # 把今日 briefings 推到 Discord
```

---

## 三条流水线

| Pipeline | 数据源 | 输出目录 |
|---|---|---|
| 1 | FreshRSS（30+ 期刊 RSS）+ 抓取/接口型论文与 AI 新闻 | `briefings/papers/`、`briefings/ai_news/` |
| 2 | GitHub Trending（HTML）+ HuggingFace（API） | `briefings/code/` |
| 3 | 大工新闻站点（HTML + API） | `briefings/resource/` |

所有源在 `config/sources.json` 中声明，类型为 `rss` / `api` / `scrape`，新增 RSS / 通用 API / 普通抓取源**无需改代码**（带自定义解析器的源除外）。

---

## 数据目录

默认根目录是 `~/.myagentdata/dailyinfo/`，可通过 `DAILYINFO_DATA_ROOT` 覆盖。

```
~/.myagentdata/dailyinfo/
├── freshrss/data/         # FreshRSS SQLite + 配置
├── briefings/{category}/  # 今日生成、待推送的 markdown
└── pushed/{category}/     # 推送成功后归档（用于去重 / 审计）
```

---

## 阅读路线

- [系统架构](architecture.md) — 各层职责、数据流、目录约定
- [CLI 参考](cli.md) — 全部命令、参数、外部 cron 模板
- [数据源列表](sources.md) — 当前订阅 / 抓取 / API 源
- [与外部调度 / 备份集成（可选）](agent-config.md) — 给同时使用 myopenclaw 等生态的用户的参考
