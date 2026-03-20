<!--
 * @Author: Wenyu Ouyang
 * @Date: 2026-03-20
 * @LastEditTime: 2026-03-20
 * @LastEditors: Wenyu Ouyang
 * @Description: DailyInfo: 面向 AI for Science 的自动化科研信息聚合系统
 * @FilePath: /dailyinfo/README.md
 * Copyright (c) 2023-2024 Wenyu Ouyang. All rights reserved.
-->

# DailyInfo 🌊

基于 n8n + Jina Reader + Kimi API 构建的本地化 AI 资讯深度精读流水线。

## 🏗️ 架构设计 (解耦模式)

1. **数据抓取**：n8n 定时读取特定信源（如 smolai）的 RSS。
2. **全文解析**：调用 `r.jina.ai` 穿透反爬虫机制，抓取干净的 Markdown 全文。
3. **深度重写**：调用 Kimi API（Moonshot），将全文重写为微信公众号级别的深度结构化长文。
4. **本地落盘**：n8n 将最终长文保存至 `~/.openclaw/workspace/n8n_data/dailyinfo_ai_YYYY-MM-DD.md`。
5. **前台分发**：由 OpenClaw 直接读取本地文件并推送到 Slack 的 `#deeplearning` 频道。

## 📁 仓库目录结构

```
dailyinfo/
├── README.md                          # 项目说明书
├── prompts/
│   └── ai_news_rewriter.txt          # Kimi 的"主编"灵魂提示词
└── workflows/
    ├── n8n_ai_news.json              # n8n 的自动化工作流图纸
    └── credentials-template.md        # API Key 安全存储配置指南
```

## 🚀 部署指南

1. 在本地启动 n8n 容器，并确保挂载了 OpenClaw 的 `workspace` 目录。
2. 将 `workflows/n8n_ai_news.json` 导入 n8n。
3. 按照 `workflows/credentials-template.md` 创建 LLM API Key 凭证。

## 🧩 三大核心业务板块 (Core Modules)

### 模块一：AI 沿前哨与深度内容引擎 (AI Tech News Daily)

**业务流向**：n8n 监控 smolai RSS → Jina Reader 抓全文 → Kimi 深度重写 → 保存 Markdown → OpenClaw 推 Slack

**技术亮点**：使用 Jina Reader API 穿透反爬，用 Kimi 生成公众号级别的深度解读。

#### 🔧 部署步骤

**Step 1: 准备输出目录**

```bash
mkdir -p ~/.openclaw/workspace/n8n_data
```

**Step 2: 导入 n8n 工作流**

1. 启动 n8n：`docker run -d --name n8n -p 5678:5678 -v ~/.openclaw/workspace:/home/node/workspace n8nio/n8n`
2. 访问 http://localhost:5678
3. 点击左侧菜单 → **Workflows** → **Import from File**
4. 选择 `workflows/n8n_ai_news.json`

**Step 3: 配置 LLM API Key（安全方式）**

> ⚠️ 工作流使用 n8n Credentials 安全存储 API Key，**不再明文写入配置文件**。

1. 访问 http://localhost:5678 → **Settings** → **Credentials**
2. 点击 **Add Credential** → 选择 **HTTP Query Auth**
3. 填写：
   - **Name**: `LLM API Key`
   - **Query Parameter Name**: `key`
   - **Query Parameter Value**: `你的API密钥`
4. 保存后，打开工作流 → 双击 **"LLM 深度重写"** 节点 → 选择 `LLM API Key` 凭证

详细步骤参见 [credentials-template.md](workflows/credentials-template.md)

**Step 4: 切换模型（可选）**

工作流支持多模型切换。编辑 **"LLM 配置"** 节点中的 `currentModel`：

```javascript
const currentModel = llmConfig.kimi;     // Kimi (默认)
// const currentModel = llmConfig.deepseek; // DeepSeek
// const currentModel = llmConfig.openai;   // OpenAI GPT
```

| 模型 | Model Name | 特点 |
|------|------------|------|
| Kimi | `moonshot-v1-8k` | 默认推荐 |
| DeepSeek | `deepseek-chat` | 性价比高 |
| OpenAI | `gpt-4o-mini` | 需科学上网 |

**Step 5: 配置定时触发（可选）**

1. 在工作流中添加 **Schedule** 节点（替代手动触发）
2. 设置 Cron 表达式，如每天早上 8:00：`0 8 * * *`

**Step 6: 修改保存路径（可选）**

在 **"准备存盘数据"** 节点的 `jsCode` 中，修改 `fileName` 路径：
```javascript
const fileName = `/data_output/dailyinfo_ai_${date}.md`;
// 实际路径: ~/.openclaw/workspace/data_output/dailyinfo_ai_2026-03-20.md
```

#### 📝 Prompt 管理

`prompts/ai_news_rewriter.txt` 是 LLM 的"主编灵魂提示词"。

**使用方式**：将文件内容完整复制到 n8n 工作流中 **"LLM 深度重写"** 节点的 `messages[0].content` 位置。

**迭代优化**：直接编辑该文件，修改后再复制到 n8n 即可。提示词决定输出质量，可根据输出效果持续调优。

### 模块二：文献巡航与人机协同中枢 (Literature Review & Fetching)

**业务流向**：n8n 定时聚合顶刊 RSS → 大模型翻译摘要 → Slack 推送 → 人工挑选 → OpenClaw 调动 Skill 下载 → 智能精读

### 模块三：流域孪生数据与气象播报 (Digital Twin Data Push)

**业务流向**：对接自研气象/水文模型 API → n8n 定时获取预测数据 → 大模型语义化包装 → 气象水情预警推送

## 🛠️ 技术栈选型 (Tech Stack)

- **自动化引擎**：n8n (Dockerized)
- **全文抓取**：Jina Reader API
- **LLM 重写**：Kimi API (Moonshot)
- **交互中枢**：OpenClaw + Slack
- **持久化**：本地文件系统 (Markdown)
