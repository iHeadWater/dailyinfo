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

## CLI 命令

| 命令 | 说明 |
|---|---|
| `dailyinfo install` | 校验 `.env`，创建工作目录，安装依赖 |
| `dailyinfo start` / `stop` / `restart` | 管理 FreshRSS 容器 |
| `dailyinfo run` | 运行全部流水线（幂等：今日已有 briefing 的源自动跳过） |
| `dailyinfo run -p {1\|2\|3}` | 只跑指定流水线 |
| `dailyinfo run -f all` / `-f <source>` | 强制重生（可重复 `-f`） |
| `dailyinfo push` | 扫 `briefings/` → POST Discord → 归档到 `pushed/` |
| `dailyinfo push -d 2026-05-20` | 补推指定日期 |
| `dailyinfo weekly [--days N] [--force]` | 汇总过去 N 天的 AI 新闻生成一份周报 |
| `dailyinfo status` | 查看今日 briefings / pushed 文件数 |
| `dailyinfo logs` | tail 流水线执行日志 |

`dailyinfo install` 不写系统 crontab，定时调度交给任意外部 cron。

`run` 在主模型（默认 `moonshotai/kimi-k2.5`）连续返回空响应时会自动切换到 `DAILYINFO_FALLBACK_MODEL`（默认 `deepseek/deepseek-v4-pro`）。

---

## 数据目录

默认根目录是 `~/.myagentdata/dailyinfo/`，可通过 `DAILYINFO_DATA_ROOT` 覆盖。

```
~/.myagentdata/dailyinfo/
├── freshrss/data/         # FreshRSS SQLite + 配置
├── briefings/{category}/  # 今日生成、待推送的 markdown
└── pushed/{category}/     # 推送成功后归档（用于去重 / 审计）
```

四个分类：`papers`、`ai_news`、`code`、`resource`。

---

## 环境变量

完整模板见 [`.env.example`](.env.example)。最常用的：

| 变量 | 说明 |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter LLM API key（必填） |
| `DISCORD_BOT_TOKEN` | Discord Bot Token（`dailyinfo push` 必填） |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_CODE` / `_RESOURCE` / `_ARXIV` | 各分类 Discord 频道 ID；缺失的分类会在推送时跳过 |
| `FRESHRSS_USER` / `FRESHRSS_PASSWORD` | FreshRSS 凭据（用户名默认 `$USER`） |
| `DAILYINFO_DATA_ROOT` | 覆盖默认数据根 |
| `DAILYINFO_FALLBACK_MODEL` | AI 备用模型（主模型空响应时切换） |

---

## 文档

- [系统架构](docs/architecture.md)
- [CLI 参考](docs/cli.md)
- [数据源列表](docs/sources.md)
- [与外部调度 / 备份集成（可选）](docs/agent-config.md)

---

## 技术栈

- Python 3.11+（包管理：`uv`）
- CLI：Click 8+
- RSS：FreshRSS (Docker + SQLite)
- AI：OpenRouter（默认 `moonshotai/kimi-k2.5`）
- 推送：Discord Bot API (`requests`)
- 文档：MkDocs Material

## License

BSD 3-Clause. 详见 [LICENSE](LICENSE)。
