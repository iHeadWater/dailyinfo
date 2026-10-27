# 暂停 code 分类采集

2026-10-27 按用户要求暂停 `code` 分类，待网络方案确定后再启用。此次为配置变更，没有修改采集、模型调用或 MCP 查询逻辑。

## 变更

`config/sources.json` 中以下四项设置为 `enabled=false`，其余参数保留，便于后续恢复：

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

网络方案确认后，将上述四项改回 `enabled=true`，更新生产镜像，先验证四个来源的只生成采集，再核对 canonical 和 MCP 读取。不会自动恢复启用，也不通过带发送行为的 `resume` 验证。
