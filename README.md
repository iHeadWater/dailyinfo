<!--
 * @Author: Wenyu Ouyang
 * @Date: 2026-03-20
 * @LastEditTime: 2026-04-23
 * @LastEditors: Wenyu Ouyang
 * @Description: DailyInfo - AI for Science 自动化科研情报系统
 * @FilePath: /dailyinfo/README.md
 * Copyright (c) 2023-2024 Wenyu Ouyang. All rights reserved.
-->

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
│   ├── code/
│   └── resource/
└── pushed/              # 已推送归档（用于去重与审计）
    ├── papers/
    ├── ai_news/
    ├── code/
    └── resource/
```

---

## 快速开始

```bash
# 1. 克隆并进入项目
git clone <repo-url>
cd dailyinfo

# 2. 创建配置文件
cp .env.example .env
# 编辑 .env，填入 OPENROUTER_API_KEY 和 DISCORD_BOT_TOKEN

# 3. 环境初始化（创建 ~/.myagentdata/dailyinfo 目录，安装依赖）
uv sync --python python3
uv pip install -e .
dailyinfo install

# 4. 启动 FreshRSS
dailyinfo start
# 首次启动需到 http://localhost:8081 创建账号并订阅源

# 5. 手动跑一次验证
dailyinfo run
dailyinfo push
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `dailyinfo install` | 验证 `.env` + 创建数据目录 + 安装依赖（一次性） |
| `dailyinfo start` | 启动 FreshRSS 容器 |
| `dailyinfo stop` | 停止 FreshRSS 容器 |
| `dailyinfo restart` | 重启 FreshRSS 容器 |
| `dailyinfo run` | 运行全部流水线（生成 markdown，**幂等**：今日已有 briefing 的源会跳过） |
| `dailyinfo run -p 2` | 仅运行指定流水线（1=RSS、2=code、3=university） |
| `dailyinfo run -f all` / `-f arxiv_cs_ai` | 强制重生（`all` 或具体源名，可重复 `-f`） |
| `dailyinfo push` | 扫描 `briefings/` → 推送 Discord → 归档到 `pushed/` |
| `dailyinfo push -d 2026-04-22` | 补推指定日期的 briefings |
| `dailyinfo status` | 查看今日 briefings / pushed 文件数量 |

> `dailyinfo install` 不再写系统 crontab；调度请交给 myopenclaw 的 hermes cron 或外部调度器。
> `run` 的 AI 调用会在主模型（`moonshotai/kimi-k2.5`）连续返回空响应时自动切到 `DAILYINFO_FALLBACK_MODEL`（默认 `deepseek/deepseek-chat-v3.1`）。

## 配置

所有数据源在 `config/sources.json` 中配置（RSS + API + Scrape 三种类型），添加新源无需改代码。

主要环境变量（在 `.env`）：

| 变量 | 说明 |
|------|------|
| `OPENROUTER_API_KEY` | OpenRouter LLM API key（必填） |
| `DISCORD_BOT_TOKEN` | Discord Bot Token（`dailyinfo push` 需要） |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_CODE` / `_RESOURCE` | 四个频道 ID，缺失则跳过该分类 |
| `FRESHRSS_USER` | FreshRSS 用户名，默认 `$USER` |
| `FRESHRSS_PASSWORD` | FreshRSS 初始密码 |
| `DAILYINFO_DATA_ROOT` | 覆盖默认数据根（默认 `~/.myagentdata/dailyinfo`） |
| `DAILYINFO_FALLBACK_MODEL` | AI 备用模型（主模型空响应后切换，默认 `deepseek/deepseek-chat-v3.1`） |

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

## Discord Bot 论文下载（可选功能）
`scripts/discord_bot.py` 提供一个交互式 Discord Bot，用户可在频道中通过命令触发论文下载

| 命令 | 说明 |                                                                                                                  
|------|------|                                                                                                                  
| `!paper <标题或DOI>` | 搜索并下载论文 PDF，Bot 回复下载结果 | 

**下载链路**（按优先级依次尝试）：arXiv → Unpaywall → Semantic Scholar → Crossref → PMC → OA Publisher 

启动方式

```bash                                                                                                                   
python3 scripts/discord_bot.py                                                                                            
# 或用 systemd 托管（见 scripts/discord_bot.service.example） 
```

--- 

## 文档

- [系统架构](docs/architecture.md)
- [Agent 配置指南](docs/agent-config.md)
- [CLI 参考](docs/cli.md)

## 技术栈

- **RSS 聚合**: FreshRSS (Docker)
- **AI 处理**: OpenRouter (moonshotai/kimi-k2.5)
- **推送**: Discord Bot API（Python `requests`）

## License

BSD 3-Clause License. See [LICENSE](LICENSE) for the full text.
