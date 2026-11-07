# 81 服务器 DailyInfo 采集与生成部署记录

日期：2026-10-26。此次按“只采集和生成、供 MCP 阅读”部署，不发送 Discord，也不发布网站。

本文保留首次上线验收数据。后续已修复 arXiv 的 Authorization 标题误判并补入 321 条目，MCP 共 3 份简报、1,571 条目，完整分页和条目读取通过。最新校验模块、镜像来源及验收见[arXiv 修复记录](arxiv-authorization-fix-81-20261026.md)。

## 当前结果

| 项目 | 已验证状态 |
|---|---|
| RSS | `dailyinfo-freshrss` 已运行，35 个订阅，1,543 条原始条目，35 个订阅有更新时间、错误标记为 0 |
| 内容生产 | 现有 `dailyinfo run` 以独立一次性任务运行，已安装每日 06:00（Asia/Shanghai）的 systemd timer |
| 下一次计划 | 核验时为 2026-10-27 06:00；模型 key 缺失时只跳过生成，RSS 继续更新 |
| MCP | `dailyinfo-mcp` 健康，`http://10.48.0.81:8768/mcp`，Streamable HTTP |
| 可见性 | `generated`：直接读取校验后的 canonical 内容，不要求投递记录，也不创建伪造投递记录 |
| 认证 | 保留原 36 个共用账号及 key，不替换凭据 |
| 模型凭据 | 已通过受限文件配置到生产任务，真实 `deepseek-flash` 请求返回 HTTP 200；未注入 MCP |
| 首次生成 | 已结束；论文简报 1,230 条、校园资讯与招聘简报 20 条，共 2 份 canonical 简报、1,250 条目，日期 2026-10-26 |
| 完整性限制 | 本轮退出码 1，arXiv 校验失败、代码趋势四个来源网络失败；AI News 无新内容，详见[首轮采集结果](first-collection-81-20261026.md) |

1,543 是 RSS 原始条目数量，不是已生成简报数，也不代表每条都会被当日流水线选中。现有流水线仍执行时间窗口、去重和条数限制。

成功落盘的两份简报已通过正式 MCP 的列表、正文、条目读取。投递记录为 0；没有向 Discord 或网站发送内容。timer 仍 active，首次部分失败不等于下一次任务被停用。

## 运行与权限

FreshRSS 不发布宿主端口，按自己的周期更新订阅；collector 不启动发布命令，只通过现有流水线生成内容。两者和 MCP 使用同一持久化数据根目录。collector 以 UID 33 写入生产数据，MCP 以 UID 10001 只读访问 publications、deliveries，个人 key 文件单独受限存放。

模型 key 与 MCP Bearer key 分开管理。前者仅供后台生成任务使用，不注入 MCP，不需要普通 MCP 使用者提供。最初缺失凭据时，systemd 条件检查已实际验证为跳过；用户随后通过受限文件提供模型 key，已安全同步并通过真实模型请求验证。GLM 回退为可选配置，此次未启用。

canonical 内容默认原子写入为 0600，此部署仅生产端开启 `DAILYINFO_PUBLICATION_GROUP_READ=1`，将内容以 0640 写入 setgid、GID 10001 的 publications 目录。采集 UID 33 与 MCP UID 10001 保持独立，个人 key 内容、所有者和权限不变。已通过隔离目录中的真实跨 UID 原子写入和只读读取探针；旧产物权限也仅在 publications 范围调整。详见[生产权限配置](production.md)。

生成任务用 flock 防止从本入口并发；FreshRSS 更新独立运行。首次手工刷新返回 1，但官方 CLI 在没有到期订阅更新时也返回 1。此次核验数据库已有 35 个更新过的订阅及 1,543 条目、错误标记全为 0，因此没有将该退出码当作采集失败。已移除生成任务前的刷新命令，避免无更新时误阻止生成。

## 来源与回退

部署没有在服务器执行 `git pull`，也未自动提交或推送本地改动：

- 源码来自 `codex/dailyinfo-readonly-mcp` 工作树，基线 `60416b73f7f377ade0454b8efb8843159e722225`。
- 沿用[首次 MCP 部署](deployment-mcp-81-20261026.md)的已校验镜像与锁定依赖，离线覆盖两个 MCP Python 模块；依赖没有改变。
- 覆盖包摘要：`3861d12af75f8c20ebf3a302a2ab5cb73a27dcdecb5f231946a80cf630873b91`，含两个 MCP 模块、两个 Compose 文件及初版调度模板。
- generated 策略初版 MCP 镜像：`sha256:41c4987bef64b25608df7d5932edb26cd2301901c4f4af535afbfb05f90d7994`。
- 真实大简报暴露了读取正文时的重复目录扫描，已将 MCP 的条目索引限定在单次请求内复用，完整性校验仍执行，后续请求重新读盘。当前镜像 `dailyinfo-mcp:index-33c44a231f75`，ID `sha256:8208387b91079f423a19d899c64148937f599dc634bf72c197e95a368b2cd079`，查询模块摘要 `33c44a231f75d7941e562f5fc52932cf67c5c50682cb01ed6d979b0ee6c0d16e`；候选环境先用真实内容和 36 个 key 验收，再更新正式容器。
- 后续生产镜像覆盖了 `publication/store.py` 的可选内容组读取支持：`dailyinfo-producer:content-98f8a7004145`，镜像 `sha256:4e9fc110547f78a560753ae0413a251768da00d3339fa0e0f359f856c120ad7d`，该源码文件摘要 `98f8a70041452d0fa10d9f676d8a0e0d987224223bd826a3e12aa52a3059e9f1`。调度后的任务使用此镜像；首轮使用先前镜像，结束后仅修正 publications 的组与读取权限，共 1,254 个 JSON/Markdown 文件。
- FreshRSS 使用官方 `ghcr.io/freshrss/freshrss:1.30.4`，拉取摘要 `sha256:af44385229541bee24aa9148d0ff8f3b40e095373c63a2c0f74e09a27d3c59df`。
- 随后独立更新 systemd service，移除 RSS 刷新前置命令；已安装 unit 摘要为 `b736a026bafe327c886ef982ef1ffd2782c607d77daef14e74f6e800046af03e`。旧 unit 和旧 MCP 环境文件均保留备份，初版覆盖包保持不变。

停止 `dailyinfo-collect.timer` 可停止后续每日生成；停止 FreshRSS 可停止 RSS 更新，均保留数据。MCP 可单独回退旧镜像与配置，原个人 key 不变。

## 验证

- 本地完整测试：511 项通过，包含 generated/delivered 两种策略、认证、只读、分页、内容归属、可选权限策略的原子替换和大简报的有界读盘次数。
- 隔离样例：generated 模式能读到两份简报，未伪造投递记录；三个工具读取通过。
- 服务器：36 个原 key 通过 SDK 初始化和工具发现，认证文件未替换，写入探针被只读挂载拒绝。
- 正式容器中 36 个原 key、三个工具和真实内容读取均通过，文件写入仍被只读挂载拒绝；认证文件的内容、所有者和权限未变。
- 开发电脑通过正式私网 HTTP 连接：两个分类的简报和单篇条目均可读；论文正文 17 页、200,837 字符完整读取，校园资讯正文 1 页、2,349 字符。缺失 key 返回 401，generated 模式传入 sink 被拒绝。
- 调度 unit 校验通过，timer active；缺失模型凭据的条件跳过已验证。
- 已验证真实 RSS、真实模型摘要、两份 canonical 简报及其 MCP 读取；生成日志未发现模型 key 或 36 个个人 key。首次采集仍有明确失败项，不能将本次部署描述为所有来源均成功。

配置方式见[采集与生成部署](production.md)，普通 Agent 接入见[MCP 文档](mcp.md)。
