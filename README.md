<!--
 * @Author: Wenyu Ouyang
 * @Date: 2026-03-20
 * @LastEditTime: 2026-03-22
 * @LastEditors: Wenyu Ouyang
 * @Description: DailyInfo: 面向 AI for Science 的自动化科研信息聚合系统
 * @FilePath: /dailyinfo/README.md
 * Copyright (c) 2023-2024 Wenyu Ouyang. All rights reserved.
-->

# DailyInfo 🌊

基于 n8n + OpenClaw + Jina Reader + Kimi API 构建的本地化 AI 资讯深度精读流水线。

## 🏗️ 架构设计 (统一容器化编排)

```
┌─────────────────────────────────────────────────────────────────┐
│                    Docker Compose Wrapper                        │
│  ┌──────────────────┐         ┌──────────────────────────┐    │
│  │    n8n Service    │         │   openclaw-gateway       │    │
│  │   (5678:5678)     │ ←────→  │   (18789:18789)          │    │
│  │                   │  共享   │                          │    │
│  │  定时任务/工作流   │ Workspace│  前台交互中枢/技能调度器  │    │
│  └────────┬─────────┘         └────────────┬─────────────┘    │
│           │                                │                   │
└───────────┼────────────────────────────────┼───────────────────┘
            │                                │
    ┌───────▼───────────────────────────────▼───────┐
    │         ~/.openclaw/workspace                  │
    │   ├── n8n_data/      (AI News 原始与精读稿)   │
    │   └── ...                                       │
    └─────────────────────────────────────────────────┘
```

1. **OpenClaw 前台交互中枢**：作为技能调度与前台交互的核心，统一管理 Skills（如 blogwatcher），并负责与 Slack 等渠道的数据分发。
2. **数据抓取**：n8n 定时读取特定信源 RSS，调用 Jina Reader 抓取全文。
3. **深度重写**：调用 Kimi API，将全文重写为微信公众号级别的深度结构化长文。
4. **本地落盘**：n8n 将最终长文保存至共享 Workspace。
5. **前台分发**：OpenClaw 直接读取共享 Workspace 文件并推送到 Slack。

**数据协同**：n8n 和 OpenClaw 通过共享的 `~/.openclaw/workspace` 目录实现无缝数据流转，全程无需额外传输层。

## 📁 仓库目录结构

```
dailyinfo/
├── README.md                          # 项目说明书
├── docker-compose.yml                 # 统一编排配置（n8n + OpenClaw）
├── Dockerfile.openclaw                 # OpenClaw 定制镜像构建文件
├── prompts/
│   └── ai_news_rewriter.txt          # Kimi 的"主编"灵魂提示词
├── workflows/
│   ├── n8n_ai_news.json              # n8n 的自动化工作流图纸
│   └── credentials-template.md        # API Key 安全存储配置指南
└── openclaw_skills/
    └── blogwatcher/
        └── SKILL.md                   # blogwatcher 技能定义
```

## 🚀 部署指南

### 一键启动（推荐）

```bash
docker-compose up -d --build
```

此命令将：
- 构建定制化的 `my-openclaw-gateway` 镜像（包含 Go 环境和 blogwatcher 工具）
- 启动 n8n 服务（端口 5678）
- 启动 OpenClaw Gateway 服务（端口 18789）
- 自动配置两者共享的 bridge 网络

### 验证服务状态

```bash
# 检查容器运行状态
docker-compose ps

# 查看日志
docker-compose logs -f n8n
docker-compose logs -f openclaw-gateway
```

### 访问服务

- **n8n 工作台**：http://localhost:5678
- **OpenClaw Gateway**：http://localhost:18789

### 导入 n8n 工作流

1. 访问 http://localhost:5678
2. 点击左侧菜单 → **Workflows** → **Import from File**
3. 选择 `workflows/n8n_ai_news.json`

详细 API Key 配置参见 [credentials-template.md](workflows/credentials-template.md)

### 环境清理

```bash
docker-compose down      # 停止服务（保留数据卷）
docker-compose down -v   # 停止服务并删除数据卷
```

## 🧩 三大核心业务板块 (Core Modules)

### 模块一：AI 沿前哨与深度内容引擎 (AI Tech News Daily)

**业务流向**：n8n 监控 smolai RSS → Jina Reader 抓全文 → Kimi 深度重写 → 保存 Markdown → OpenClaw 推 Slack

**技术亮点**：使用 Jina Reader API 穿透反爬，用 Kimi 生成公众号级别的深度解读。OpenClaw 可通过 blogwatcher Skill 自动调动扫描任务，增强内容发现能力。

### 模块二：文献巡航与人机协同中枢 (Literature Review & Fetching)

**业务流向**：n8n 定时聚合顶刊 RSS → 大模型翻译摘要 → Slack 推送 → 人工挑选 → OpenClaw 调动 Skill 下载 → 智能精读

**OpenClaw Skill 介入**：OpenClaw 可根据用户指令，自动调用文献下载 Skill，完成从筛选到下载的全链路自动化。

### 模块三：流域孪生数据与气象播报 (Digital Twin Data Push)

**业务流向**：对接自研气象/水文模型 API → n8n 定时获取预测数据 → 大模型语义化包装 → 气象水情预警推送

## 🛠️ 技术栈选型 (Tech Stack)

- **自动化引擎**：n8n (Dockerized)
- **交互中枢**：OpenClaw Gateway + Skills (Dockerized)
- **全文抓取**：Jina Reader API
- **LLM 重写**：Kimi API (Moonshot)
- **内容监控**：blogwatcher (内置于 OpenClaw 镜像)
- **消息分发**：Slack
- **持久化**：本地文件系统 (共享 Workspace)
