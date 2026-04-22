<!--
 * @Author: Wenyu Ouyang
 * @Date: 2026-03-20
 * @LastEditTime: 2026-04-22
 * @LastEditors: Wenyu Ouyang
 * @Description: DailyInfo - AI for Science 自动化科研情报系统
 * @FilePath: /dailyinfo/README.md
 * Copyright (c) 2023-2024 Wenyu Ouyang. All rights reserved.
-->

# DailyInfo

面向 AI for Science 研究者的自动化科研情报聚合与推送系统。

**核心流程**: FreshRSS → AI 摘要 → Discord 推送

**设计原则**: 配置驱动（`config/sources.json`）+ 职责分离

---

## 快速开始

```bash
# 1. 克隆并进入项目
git clone <repo-url>
cd dailyinfo

# 2. 创建配置文件
cp .env.example .env
# 编辑 .env，填入 OPENROUTER_API_KEY 和 DISCORD_BOT_TOKEN

# 3. 环境初始化
uv sync --python python3
uv pip install -e .
dailyinfo install

# 4. 启动服务
dailyinfo start

# 5. 立即运行
dailyinfo run
dailyinfo push
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `dailyinfo install` | 环境初始化（一次性） |
| `dailyinfo start` | 启动 FreshRSS |
| `dailyinfo stop` | 停止服务 |
| `dailyinfo run` | 运行全部流水线 |
| `dailyinfo run -p 2` | 仅运行指定流水线 |
| `dailyinfo push` | 推送到 Discord |
| `dailyinfo status` | 查看简报文件数量 |

## 配置

所有数据源在 `config/sources.json` 中配置（RSS + API + Scrape）。

添加新期刊：只需在配置文件中添加条目，无需修改代码。

## 文档

- [系统架构](docs/architecture.md)
- [Agent 配置指南](docs/agent-config.md)
- [CLI 参考](docs/cli.md)

## 技术栈

- **RSS 聚合**: FreshRSS (Docker)
- **AI 处理**: OpenRouter (moonshotai/kimi-k2.5)
- **推送**: Discord Bot API

## License

MIT License