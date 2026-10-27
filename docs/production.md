# 只采集、生成，供 MCP 阅读

此部署模式运行 FreshRSS 和 DailyInfo 生成任务，不发送 Discord、不发布网站。MCP 只读挂载相同内容目录，使用 `generated` 可见性，明确返回已生成状态；不以伪造投递记录的方式放行。

2026-10-27 起按用户要求暂时停用 `code` 分类的四个来源：`github_trending`、`huggingface_models`、`huggingface_datasets`、`huggingface_spaces`，在 `config/sources.json` 中均为 `enabled=false`。其他分类和每日调度继续运行。待网络方案确定并验收后，将这四项恢复为 `true` 并更新生产镜像即可重新启用；保留已有内容，不删除失败记录或历史归档。部署及验收见[暂停记录](code-category-paused-81-20261027.md)。

81 的已部署组件、真实采集量和验收状态见[部署记录](deployment-production-81-20261026.md)。

## 组件与数据流

```text
FreshRSS 定期更新 RSS → 持久化 SQLite
每日生成任务 → 读取 RSS / 网页 / API → 模型摘要 → canonical publications
Agent → 只读 MCP → canonical publications
```

`compose.production.yml` 只负责内容生产：

- `freshrss`：固定 FreshRSS 1.30.4，内部运行，不发布宿主端口；每 15 分钟触发更新，单个 feed 的 TTL 仍决定实际更新频率。
- `collector`：一次性生成任务，沿用现有 `dailyinfo run`；不运行 `push`、`publish` 或 `resume`。使用与 FreshRSS 一致的 UID 33 访问 SQLite；设置 `USER=collector` 兼容现有采集账号解析。
- `deploy/dailyinfo-collect.service.in` 与 `.timer`：外部 systemd 调度，每天 06:00（Asia/Shanghai）读取已采集数据并生成内容；通过 flock 防止本调度入口并发。

RSS 更新由 FreshRSS 自己的定时任务负责，不作为生成任务的前置命令。其 `actualize-user.php` 在没有到期订阅时也会返回 1，即使数据库已有完整条目；不能仅凭此退出码阻止生成。运维应另外核查订阅更新时间、错误字段及条目数。

`compose.mcp.yml` 仍单独运行。MCP 的个人 key、模型平台 API key、FreshRSS 管理密码是三类不同凭据，不能互相替代，也不需要发给同一批用户。

生产进程 UID 33 与 MCP UID 10001 保持独立。canonical 存储默认用 0600 原子写入；此部署显式设置 `DAILYINFO_PUBLICATION_GROUP_READ=1`，仅将生成内容以 0640 写入，其他值均保留默认私有权限。管理员须把 `publications/` 的组设为 MCP 的 GID 10001，并让目录使用 setgid（例如 2755），使新目录和新文件持续继承读取组。既有内容也须单独核对组和只读权限。此选项不影响认证文件或投递存储；不能依靠一次性 chmod 解决后续文件的读取权限。

MCP key 目录继续由 UID 10001 持有，权限 0700，key 文件 0400；生产任务不加入该进程身份，不挂载该认证目录，也不获取个人 Bearer key。MCP 只读挂载内容，不注入模型 key。

## 配置

生产 Compose 需要 `FRESHRSS_IMAGE`、`DAILYINFO_IMAGE`、`DAILYINFO_DATA_ROOT`、`DAILYINFO_PRODUCER_ENV`。其中内容根目录须与 MCP 的只读挂载源一致。

可以用项目的 `Dockerfile.mcp` 构建共同的运行镜像：`docker build -f Dockerfile.mcp -t dailyinfo:local .`，并将 `DAILYINFO_IMAGE` 设为该标签。镜像包含 scripts、config 及锁定的 MCP 可选依赖，PATH 优先使用虚拟环境；生产 Compose 通过 `python -m scripts.cli run` 启动生成任务。

受限的 producer env 文件只需：

```dotenv
DEEPSEEK_API_KEY=
# 可选故障回退：
GLM_API_KEY=
```

不需要 Discord token 或频道配置。此模式不使用要求 Discord 配置的通用 `dailyinfo install`，由部署准备数据目录和锁定依赖。

模型 key 缺失时，systemd 的条件检查跳过生成任务；FreshRSS 可继续采集原始 RSS，但 MCP 不会把原始 RSS 数据库冒充成已经生成的简报。不能以“容器运行”或“定时器启用”宣称模型生成已验收。

## 初始化与运行

1. 准备持久化内容目录与受限 producer env；不得放入 Git。
2. 启动 FreshRSS，完成 SQLite 初始化与内部采集账号创建。
3. 使用项目现有 `freshrss_admin.ensure_subscription` 注册已启用的 RSS 数据源，再刷新 feed。
4. 部署审核后的 DailyInfo 镜像，以 `collector` 一次性任务执行现有流水线。
5. 渲染 systemd service 模板的 `@SERVICE_ROOT@`，校验 unit 后启用 timer。生产目录中须提供相应 Compose 和 deploy env。
6. 在 MCP 部署环境设置 `DAILYINFO_MCP_VISIBILITY=generated`，按生成态返回内容。

手工运行和周期任务都应走同一 systemd service，避免额外绕过调度互斥。原始 RSS 更新失败与模型生成失败分开记录；部分源失败时原流水线可能保存成功部分并返回非零，需要检查数据源汇总。

## 验收与撤回

分别验证 RSS 订阅数/条目数、真实模型调用、canonical 简报和条目数、MCP 三个工具的真实内容读取、认证和只读边界。生成态结果必须是 `content_state=generated`，不能带伪造的 Discord 成功记录。

若模型凭据尚未提供，应报告“RSS 已采集，生成待配置”，并保留未完成的真实模型与 MCP 内容验收。新增凭据后先手动执行一次，再确认下次计划时间。

停止 timer 即可停用每日生成，停止 FreshRSS 即可停止其更新；均保留数据。MCP 仍可读取已生成内容。MCP 的旧镜像、Compose 配置和环境文件保留用于单独回退。

FreshRSS 初始化与运行方式依据其[官方 Docker 文档](https://github.com/FreshRSS/FreshRSS/blob/1.30.4/Docker/README.md)与[官方 CLI 文档](https://github.com/FreshRSS/FreshRSS/blob/1.30.4/cli/README.md)，版本选择参考[1.30.4 官方发行说明](https://github.com/FreshRSS/FreshRSS/releases/tag/1.30.4)。
