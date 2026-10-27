# 暂停 code 分类采集

2026-10-27 曾按用户要求仅在 81 服务器暂停 `code` 分类；同日部署独立代理后已恢复。以下保留暂停过程，当前状态见文末“恢复验收”。暂停为配置变更，没有修改采集、模型调用或 MCP 查询逻辑。

## 变更

仅 81 服务器发布镜像内的 `config/sources.json` 将以下四项设置为 `enabled=false`，其余参数保留。仓库本地及远端默认均为 `enabled=true`，不将服务器暂停状态作为仓库默认配置：

- `github_trending`
- `huggingface_models`
- `huggingface_datasets`
- `huggingface_spaces`

其他分类、每日 06:00 调度和既有内容保持原状。MCP 的只读查询能力及 endpoint 不变；停止采集不会删除已有简报、归档或失败记录。

## 部署与验证

此次部署使用 `codex/dailyinfo-readonly-mcp` 工作树中当时尚未提交、推送的变更。服务器没有执行 `git pull`：只在现有已验收生产镜像上离线覆盖 sources.json，再更新生产任务的镜像引用。后续本地 Git 合并不改变该部署记录。

- 新镜像：`dailyinfo-producer:code-paused-393dd9f3da86`。
- 镜像 ID：`sha256:b579ba7205c08839b0a93d3e9367a0897da7f47c9a9ab17eef013b718369ab3e`。
- 配置 SHA-256：`393dd9f3da865a58261e14b1e09a401075295e3ecfe3fe2d94f4a2f0d772b503`。
- 已对照旧生产配置，确认只有四个 code 来源的 enabled 标记变化。
- 既有 code 相关测试 7 项通过。
- 隔离容器使用新镜像，在禁止网络并阻止数据源创建及模型调用的条件下验证：canonical 和旧流程均直接跳过，网络/模型调用为 0，没有创建数据目录，也没有新增失败汇总。
- 生产 Compose 已解析并指向新镜像，timer active；核验时下一次计划为 2026-10-28 06:00（Asia/Shanghai）。
- MCP 与 FreshRSS 容器 ID、启动时间未变化，个人 key 文件元数据未变化。

仅生成任务镜像更新；没有修改代理、DNS、hosts、防火墙或其他容器。旧镜像与环境文件备份保留。

## 重新启用

网络方案确认后，移除服务器侧对上述四项的停用覆盖，更新生产镜像，先验证四个来源的只生成采集，再核对 canonical 和 MCP 读取；无需改动仓库默认的启用配置。不会自动恢复启用，也不通过带发送行为的 `resume` 验证。

## 恢复验收

2026-10-27 经用户授权，在 81 部署独立的 `dailyinfo-proxy` 容器，使用已校验官方摘要的 Mihomo v1.19.33。代理与采集容器通过专用 Docker 桥接网络连接，未映射宿主机端口，访问要求独立代理密码；未修改宿主机默认路由。节点配置及凭据保存在服务器受限文件中，不纳入仓库。当前固定使用已验证节点，未开启订阅自动更新。

生产采集镜像恢复为 `dailyinfo-producer:arxiv-auth-5318f41c42da`，已对照确认仅恢复上述四项 enabled 标记。只运行 `dailyinfo run -p 4`，成功写入 `code-2026-10-27` 共 54 条内容：GitHub 14、Hugging Face models 20、datasets 10、spaces 10。一次 Hugging Face 临时 SSL 错误通过项目自带重试恢复，任务退出码为 0。

通过项目锁定的 MCP SDK 1.30.0，从开发电脑完整读取简报正文 4,939 字符及全部 54 条内容；未带个人 key 返回 401，未带代理密码返回 407。其他分类已有内容逐一比对未变，投递记录仍为 0，没有发送 Discord 或发布网站。MCP endpoint、个人 key、MCP 和 FreshRSS 容器保持不变。每日 timer 为 active，并在采集前确认代理就绪。
