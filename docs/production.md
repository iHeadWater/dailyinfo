# 自带调度的生产部署

从 2026-11-07 起，dailyinfo 的采集、生成和 Discord 推送由自己的容器栈调度，不再
依赖宿主 launchd / systemd / cron。Web 发布仍由同仓库的宿主 launchd 任务在每天
07:40 执行（需要 Node 22/npm 和 git 凭据）。新机器 clone 仓库、配好环境文件后：

```bash
docker compose up -d
```

随后：

- `freshrss` 按自己的周期更新 RSS，Web UI 默认只绑定宿主机 `127.0.0.1:8081`
  （由 `FRESHRSS_BIND_ADDRESS` / `FRESHRSS_PORT` 控制）。已安装实例修改端口后，
  还要同步 `data/config.php` 的 `base_url`：
  `php ./cli/reconfigure.php --base-url=http://localhost:<port>`；
- `scheduler` 按 `deploy/crontab.*` 的时点运行 `dailyinfo run` / `push`；
- `dailyinfo-mcp` 只读提供已生成或已投递的内容。

外部 agent（myopenclaw 等）只通过 MCP 读取。旧部署的卸载、备份归属和回滚步骤见
[迁移记录](migration-selfhosted-scheduler-20261107.md)。

## 组件

| 服务 | 职责 | 触发方式 |
|---|---|---|
| `freshrss` | 内部 RSS 采集 | 容器内每 15 分钟刷新 |
| `scheduler` | 常驻；supercronic 读 crontab，`scripts/scheduler.py` 记账、补跑、健康检查 | `deploy/crontab.full` |
| `collector` | 手工排障的一次性任务（`profiles: [jobs]`），与 scheduler 共用锁 | 手动 |
| `dailyinfo-mcp` | 只读 MCP | 不调度 |
| `web-publish`（宿主） | `dailyinfo publish --sink web`；需要 Node 22/npm/git | `deploy/launchd/`，每天 07:40 |

## 两种模式

`DAILYINFO_SCHEDULER_JOBS` 选择 crontab：

| 值 | crontab | 行为 |
|---|---|---|
| `full`（默认） | `deploy/crontab.full` | 5 个 run + 3 个 Discord push；需要 `.env` 里的 `DISCORD_BOT_TOKEN` |
| `collect` | `deploy/crontab.collect` | 只跑 5 个 run，不推送；供只经 MCP 消费的部署使用 |

> 当前 crontab 只覆盖 pipeline 1–5。Pipeline 6（OpenReview 会议）尚未接入
> scheduler，需手动执行 `dailyinfo run -p 6` / `dailyinfo push -c conference`，
> 或由外部 cron 触发；follow-up 见 #101。

full 模式启动时校验 `DISCORD_BOT_TOKEN`；缺失则容器退出并提示改用
`collect`，不会出现“前 5 个任务成功、3 次推送全失败”的半套状态。

Web 发布不受这个模式影响：无论 `full` 还是 `collect`，它都由宿主任务单独触发。

## Web 发布（宿主任务）

自托管 scheduler 容器只负责 `dailyinfo run` 和 `dailyinfo push`（Discord）。
Web 发布保留在宿主，因为它需要宿主上的 Node 22、npm 和 git push 凭据，而
producer 镜像里没有这些；Web checkout 的 `node_modules` 也是 macOS 原生的，
直接挂进 Linux 容器会加载错误的二进制。

macOS 用仓库自带的 launchd 任务，在每天 07:40（所有 Discord push 之后）运行：

```bash
./deploy/launchd/install-web-publish.sh
```

它执行：

```bash
cd <dailyinfo checkout> && uv run dailyinfo publish --sink web
```

日志在 `logs/dailyinfo-web-publish.log`。任务在当天 Web delivery 已成功时会
自动 skip，重复触发是幂等的。卸载：

```bash
./deploy/launchd/uninstall-web-publish.sh
```

Linux 主机用等价的 systemd timer / cron 跑同一条命令即可。容器化 Web 发布
是后续工作（需要 Node 22、Linux `node_modules` 和专用 git 凭据）。

## 调度语义

- **时间来源**：`deploy/crontab.full` / `crontab.collect`。supercronic 按容器
  本地时区解释 5 段 cron；镜像固定 `TZ=Asia/Shanghai` 并安装 `tzdata`。
- **启动校验**：`verify-env` 检查本地时区偏移与 `Asia/Shanghai` 一致、crontab
  与 job 表完全对应、full 模式凭据齐全。任一不符容器直接退出。
- **补跑**：`catch-up` 在启动时和每 15 分钟运行一次，补“当天已到期但未成功”
  的 job。失败按 5 / 15 分钟退避，每日最多 3 次。每次尝试的时间按实际执行时刻
  记录，不按 sweep 开始时刻。
- **单写者**：所有 job 与手工 `collector` 共用 `state/scheduler.lock`。锁被占时
  本次跳过并记日志，由下一轮 catch-up 补上；不会有两个进程同时写 SQLite。
- **健康检查**：对每个 job 取最近一次已到期的计划时刻，超过 2 小时仍无当日成功
  记录则容器 unhealthy。容器每次重启会把补跑宽限重新锚定到启动时刻（最多 2 小时），
  但 15 分钟一次的 sweep 不会延长它，持续失败仍会变红。
- **状态**：`state/scheduler_state.json`，原子写入。文件损坏时隔离为
  `.corrupt-<时间戳>`，按“今天未成功”重放，并把健康检查置红。

## 配置

### Compose 变量

Compose 直接读取项目根目录的 `.env`，不需要再维护独立的
`producer.env` / `push.env`。同一份 `.env` 既供宿主 CLI 使用，也供容器
interpolation 使用：

```dotenv
FRESHRSS_IMAGE=freshrss/freshrss:1.30.4
DAILYINFO_IMAGE=dailyinfo-producer:<tag>
DAILYINFO_DATA_ROOT=/srv/dailyinfo
DAILYINFO_MCP_SECRETS_DIR=/srv/dailyinfo/secrets/mcp
DAILYINFO_SCHEDULER_JOBS=full
# Web UI 默认只绑 loopback，端口可按需改：
FRESHRSS_BIND_ADDRESS=127.0.0.1
FRESHRSS_PORT=8081
# 仅首次安装使用；改端口时同步修改 base URL：
FRESHRSS_LANGUAGE=zh-cn
FRESHRSS_BASE_URL=http://localhost:8081
FRESHRSS_CRON_MIN=*/15
```

`compose.yml` 使用 `include`，需要 Docker Compose ≥ 2.24。

### 凭据来源

凭据只写一次，放在项目 `.env`：

```dotenv
DEEPSEEK_API_KEY=
# 可选故障回退：
GLM_API_KEY=
# pipeline 6（OpenReview）需要；旧键 OPENREVIEW_USERNAME 仍可作为邮箱回退：
OPENREVIEW_EMAIL=
OPENREVIEW_PASSWORD=

# full 模式需要：
DISCORD_BOT_TOKEN=
DISCORD_CHANNEL_PAPERS=
DISCORD_CHANNEL_AI_NEWS=
DISCORD_CHANNEL_CODE=
DISCORD_CHANNEL_RESOURCE=
DISCORD_CHANNEL_ARXIV=
# 可选；未配置时会议速览回落到 DISCORD_CHANNEL_AI_NEWS
DISCORD_CHANNEL_CONFERENCE=
```

Compose 只把 Discord 相关键注入 scheduler；collector 拿到模型和
OpenReview 键，但拿不到 `DISCORD_*`。

### 镜像

```bash
docker build -f Dockerfile.producer -t dailyinfo-producer:$(git rev-parse --short HEAD) .
```

镜像基于 `python:3.12-slim`，安装 `tzdata` 和校验过 SHA256 的 supercronic，
**不设置 ENTRYPOINT**。collector 的 `command` 与 scheduler 的 `entrypoint`
都由 compose 指定，避免镜像入口把命令变成 supercronic 的参数。

### 凭据边界

- `.env` 是唯一凭据来源；Compose interpolation 只把 `DISCORD_*` 注入
  scheduler，collector 的环境里没有这些键。
- scheduler 必须持有 token 才能推送；collection job 的子进程还会被剥离
  `DISCORD_*` 环境变量，但同容器同 UID 的 PID 1 仍可读到 token。彻底隔离
  需要把 push 放到独立容器 / UID，当前以“剥离子进程环境”作为折中。

## 首次部署

```bash
export DAILYINFO_DATA_ROOT=/srv/dailyinfo
export DAILYINFO_MCP_SECRETS_DIR=/srv/dailyinfo/secrets/mcp
sudo -E ./deploy/bootstrap.sh     # 创建 bind mount 源并设置所有权
docker compose up -d

# macOS：安装宿主 Web 发布任务（Linux 用 systemd timer / cron 跑同一命令）
./deploy/launchd/install-web-publish.sh
```

所有 bind mount 都使用 `create_host_path: false`，因此上述目录必须事先存在，
否则 `docker compose up -d` 会在创建容器时失败。脚本还会给 scheduler/collector
以 UID 33 写权限，并把 `publications`、`deliveries` 设为 GID 10001 的 setgid
目录供 MCP 只读。macOS 上 Docker Desktop 自行映射所有权，脚本只创建目录。

## 运行与排障

```bash
docker compose ps
docker compose logs -f scheduler
docker compose exec scheduler python -m scripts.scheduler status
docker compose exec scheduler python -m scripts.scheduler healthcheck
```

手工排障仍走 collector（默认命令已经套了同一把锁）：

```bash
docker compose run --rm collector          # 跑全部 pipeline
# 只跑其中一个 pipeline 时，显式保留锁包装：
docker compose run --rm --entrypoint python collector \
  -m scripts.scheduler exec -- python -m scripts.cli run -p 1
```

`docker compose run collector <command>` 会覆盖 compose 里的默认命令；直接写成
`... collector python -m scripts.cli run` 会绕过锁，只用于确知没有定时任务在跑
的场景。

## 权限

scheduler 与 collector 以 UID 33 写入数据；MCP 以 UID 10001 只读挂载
`publications/` 和 `deliveries/`。`DAILYINFO_PUBLICATION_GROUP_READ=1` 让新内容
以 0640 写入 setgid、GID 10001 的目录。管理员仍需把 `publications/` 的组设为
10001 并启用 setgid，使新目录和新文件持续可读；这部分与旧部署相同。

生产任务必须写 `freshrss/data`（首次运行会自动注册 RSS 订阅），因此整个数据根
以读写方式挂载，而不是把 FreshRSS 数据库单独设为只读。

## 验收与撤回

- `docker compose ps`：`freshrss` 与 `scheduler` 均为 healthy。
- `scheduler status`：当日每个 job 的 `success` 日期正确。
- 次日按 crontab 时间生成；full 模式推送 Discord。连续观察 2 天。
- 07:40 后检查 `logs/dailyinfo-web-publish.log`，确认 Web 发布成功或按当天 delivery 状态 skip。
- 重启 scheduler 容器：错过的 job 被补跑，推送不重复（canonical delivery 幂等）。
- 停用：`docker compose stop scheduler`，数据保留；Web 任务用 `./deploy/launchd/uninstall-web-publish.sh` 停止。
- 回退到 systemd 调度：见[迁移记录](migration-selfhosted-scheduler-20261107.md)。
