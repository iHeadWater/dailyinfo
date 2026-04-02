# API 配置指南

## OpenRouter API（唯一 LLM 接口）

DailyInfo 通过 [OpenRouter](https://openrouter.ai/) 统一调用各种 LLM，无需为每个模型单独配置 API。

### 配置方式

**Step 1**：在 `.env` 文件中设置 API Key：

```env
# 获取地址：https://openrouter.ai/keys
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxx
```

**Step 2**：环境变量通过 `docker-compose.yml` 自动注入 n8n 容器：

```yaml
services:
  n8n:
    env_file: .env
    environment:
      - N8N_ENV_VARS_IN_DEC=true
```

**Step 3**：n8n 工作流中通过表达式引用：

```
{{ $env.OPENROUTER_API_KEY }}
```

> **无需在 n8n UI 中创建 Credentials**。环境变量注入比 UI Credentials 更简单、可复现。

### 当前模型

在 `config/feeds.json` 的 `defaults.model` 中配置：

```json
{
  "defaults": {
    "model": "anthropic/claude-haiku-4.5"
  }
}
```

支持 OpenRouter 上的所有模型，更换只需修改此字段。

### 常用模型参考

| 模型 | OpenRouter ID | 说明 |
|------|--------------|------|
| Claude Haiku 4.5 | `anthropic/claude-haiku-4.5` | 当前使用，性价比高 |
| Claude Sonnet 4 | `anthropic/claude-sonnet-4` | 更强推理能力 |
| GPT-4o mini | `openai/gpt-4o-mini` | OpenAI 轻量版 |
| DeepSeek V3 | `deepseek/deepseek-chat` | 国产模型，成本低 |

### 验证配置

```bash
# 检查环境变量是否注入成功
docker exec dailyinfo_n8n env | grep OPENROUTER

# 测试 API 连通性
curl -s https://openrouter.ai/api/v1/models \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | head -c 200
```

## 其他 API（无需配置）

| 数据源 | 认证方式 | 说明 |
|--------|---------|------|
| GitHub Trending | 无需 | HTML 爬取公开页面 |
| HuggingFace API | 无需 | 公开接口 |
| DUT 大工新闻网 | 无需 | 公开 HTML 页面 |

> 所有外部数据源均为公开访问，不需要额外 API Key。
