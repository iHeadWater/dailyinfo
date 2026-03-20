# N8N Credentials 配置指南

## 创建 LLM API Key Credential

### Step 1: 在 n8n UI 中创建 Credential

1. 启动 n8n 并登录 http://localhost:5678
2. 点击左侧菜单 **Settings** → **Credentials**
3. 点击右上角 **Add Credential**
4. 选择类型 **"HTTP Header Auth"**（或根据你的 n8n 版本选择合适的类型）
5. 填写以下信息：
   - **Name**: `LLM API Key`
   - **Header Name**: `Authorization`
   - **Header Value**: `Bearer 你的API密钥`（直接填 key，不需要 Bearer 前缀，因为工作流中已处理）
6. 点击 **Save**

### Step 2: 关联到工作流

导入工作流后：
1. 打开工作流，双击 **"LLM 深度重写"** 节点
2. 点击 **Credential** 下拉框
3. 选择 `LLM API Key`
4. 保存节点

## 支持的模型

在 **"LLM 配置"** 节点中修改 `currentModel` 来切换：

```javascript
const currentModel = llmConfig.kimi;   // Kimi (默认)
// const currentModel = llmConfig.deepseek; // DeepSeek
// const currentModel = llmConfig.openai;  // OpenAI GPT
```

### 各模型配置

| 模型 | API Endpoint | Model Name | 说明 |
|------|-------------|------------|------|
| Kimi | `https://api.moonshot.cn/v1/chat/completions` | `moonshot-v1-8k` | 默认推荐 |
| DeepSeek | `https://api.deepseek.com/v1/chat/completions` | `deepseek-chat` | 性价比高 |
| OpenAI | `https://api.openai.com/v1/chat/completions` | `gpt-4o-mini` | 需科学上网 |

## 添加自定义模型

在 `llmConfig` 对象中添加新配置：

```javascript
const llmConfig = {
  // ... 现有配置

  // 添加新模型
  yourmodel: {
    provider: "yourmodel",
    apiUrl: "https://api.yourmodel.com/v1/chat/completions",
    model: "your-model-name",
    credentialName: "llmApiKey"
  }
};
```

## 安全建议

- **不要**将真实的 API Key 直接写在工作流 JSON 文件中
- 使用 n8n Credentials 功能安全存储
- 定期轮换 API Key
- 为不同的环境（开发/生产）创建不同的 Credentials
