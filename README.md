# DailyInfo

面向 AI for Science 研究者的自动化科研情报聚合与推送系统。

**核心流程**: FreshRSS 采集 → AI 摘要生成（markdown 落盘）→ Discord 推送 + 归档

**设计原则**: 配置驱动（`config/sources.json`）+ 确定性 CLI + 外部调度

---

## 与 myopenclaw 的分工

dailyinfo 自身只提供幂等的 CLI 动作，调度由外部 cron 驱动。推荐的部署拓扑：

| 职责 | 归属 |
|------|------|
| RSS 采集（FreshRSS 容器） | dailyinfo |
| AI 摘要生成 `dailyinfo run` | dailyinfo |
| Discord 推送 + 归档 `dailyinfo push` | dailyinfo（纯 Python，不含 AI） |
| 定时触发上述 CLI | myopenclaw 的 hermes cron |
| 数据备份 | myopenclaw 的 `backup-cron`（只读挂载 `~/.myagentdata`） |

数据全部落在 `~/.myagentdata/dailyinfo/` 下，自然被 myopenclaw 的备份流程覆盖，无需额外配置。

```
~/.myagentdata/dailyinfo/
├── freshrss/data/       # FreshRSS SQLite + 配置
├── briefings/           # 今日待推送的 markdown
│   ├── papers/
│   ├── ai_news/
│   ├── arxiv/
│   ├── code/
│   └── resource/
├── pushed/              # 已推送归档（用于去重与审计）
│   ├── papers/
│   ├── ai_news/
│   ├── arxiv/
│   ├── code/
│   └── resource/
└── state/               # 运行时状态（marker 文件等）
```

---

## 快速开始

```bash
git clone https://github.com/OuyangWenyu/dailyinfo.git
cd dailyinfo

cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 和 DISCORD_BOT_TOKEN（OPENROUTER_API_KEY 可选，用于回退模型）

uv sync --python python3
uv pip install -e .

dailyinfo install   # 校验 .env、创建数据目录
dailyinfo start     # 启动 FreshRSS 容器（首次访问 http://localhost:8081 创建账号并订阅）

dailyinfo run       # 跑一次完整流水线
dailyinfo push      # 把今日 briefings 推到 Discord
```

---

## CLI 命令

| 命令 | 说明 |
|------|------|
| `dailyinfo install` | 验证 `.env` + 创建数据目录 + 安装依赖（一次性） |
| `dailyinfo start` | 启动 FreshRSS 容器 |
| `dailyinfo stop` | 停止 FreshRSS 容器 |
| `dailyinfo restart` | 重启 FreshRSS 容器 |
| `dailyinfo run` | 运行全部流水线（生成 markdown，**幂等**：今日已有 briefing 的源会跳过） |
| `dailyinfo run -p N` | 运行指定流水线（1=papers、2=ai_news、3=arxiv、4=code、5=resource） |
| `dailyinfo run -f all` / `-f arxiv_cs_ai` | 强制重生（`all` 或具体源名，可重复 `-f`） |
| `dailyinfo push` | 扫描 `briefings/` → 推送 Discord → 归档到 `pushed/` |
| `dailyinfo push -d 2026-04-22` | 补推指定日期的 briefings |
| `dailyinfo status` | 查看今日 briefings / pushed 文件数量 |

> `dailyinfo install` 不再写系统 crontab；调度请交给 myopenclaw 的 hermes cron 或外部调度器。
> `run` 的 AI 调用会在主模型 DeepSeek V4 Pro 连续返回空响应时自动回退到 OpenRouter 的 `DAILYINFO_FALLBACK_MODEL`（默认 `moonshotai/kimi-k2.5`）。

---

## 五条流水线

| Pipeline | 数据源 | 输出目录 |
|---|---|---|
| 1 | Papers（30+ 期刊 RSS）+ 抓取/接口型论文 | `briefings/papers/` |
| 2 | AI News（smolai via RSS with deep-content） | `briefings/ai_news/` |
| 3 | arXiv CS.AI（RSS，最多 500 篇） | `briefings/arxiv/` |
| 4 | GitHub Trending（scrape）+ HuggingFace（API） | `briefings/code/` |
| 5 | DLUT university sites（scrape + API） | `briefings/resource/` |

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

五个分类：`papers`、`ai_news`、`arxiv`、`code`、`resource`。

## 环境分离

通过 `DAILYINFO_ENV`（`dev` / `staging` / `prod`）隔离不同环境的数据目录和 Discord 频道。dev 和 staging 使用独立的数据目录（`dailyinfo-dev` / `dailyinfo-staging`）和频道 ID（`DISCORD_CHANNEL_*_DEV` / `DISCORD_CHANNEL_*_STAGING`），避免测试推送打到生产频道。

---

## 环境变量

完整模板见 [`.env.example`](.env.example)。最常用的：

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek 官方 API key（必填） |
| `OPENROUTER_API_KEY` | OpenRouter API key（可选，仅回退模型使用） |
| `DISCORD_BOT_TOKEN` | Discord Bot Token（`dailyinfo push` 需要） |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_CODE` / `_RESOURCE` / `_ARXIV` | 各分类频道 ID，缺失则跳过；arxiv 未配则复用 `DISCORD_CHANNEL_AI_NEWS` |
| `FRESHRSS_USER` | FreshRSS 用户名，默认 `$USER` |
| `FRESHRSS_PASSWORD` | FreshRSS 初始密码 |
| `DAILYINFO_DATA_ROOT` | 覆盖默认数据根（默认 `~/.myagentdata/dailyinfo`） |
| `DAILYINFO_FALLBACK_MODEL` | AI 备用模型（主模型空响应后切换，默认 `moonshotai/kimi-k2.5`） |

## 从旧版本迁移

如果旧数据在 `~/.freshrss/data/` 和 `~/.dailyinfo/workspace/`：

```bash
mkdir -p ~/.myagentdata/dailyinfo/freshrss
[ -d ~/.freshrss/data ] && mv ~/.freshrss/data ~/.myagentdata/dailyinfo/freshrss/data
[ -d ~/.dailyinfo/workspace/briefings ] && mv ~/.dailyinfo/workspace/briefings ~/.myagentdata/dailyinfo/briefings
[ -d ~/.dailyinfo/workspace/pushed ] && mv ~/.dailyinfo/workspace/pushed ~/.myagentdata/dailyinfo/pushed

# 如存在旧 crontab 条目，手动移除：
crontab -l | grep -v dailyinfo | crontab -

# 重启 FreshRSS 生效
docker compose down && dailyinfo start
```

---

## 技术栈

- Python 3.10+（包管理：`uv`）
- CLI：Click 8+
- RSS：FreshRSS (Docker + SQLite)
- AI：DeepSeek V4 Pro 官方 API（回退：OpenRouter Kimi K2.5）
- 推送：Discord Bot API (`requests`)
- 文档：MkDocs Material

## 文档

- [系统架构](docs/architecture.md)
- [CLI 参考](docs/cli.md)
- [数据源列表](docs/sources.md)
- [与外部调度 / 备份集成（可选）](docs/agent-config.md)

## License

BSD 3-Clause. 详见 [LICENSE](LICENSE)。
