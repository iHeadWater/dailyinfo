# 迁移到自带调度（2026-11-07）

> **2026-11-08 更新**：当前 compose 直接读取项目 `.env`，
> `DAILYINFO_PRODUCER_ENV` / `DAILYINFO_PUSH_ENV` 及 `producer.env` /
> `push.env` 已移除；Web 发布由宿主 launchd 任务
> `deploy/launchd/install-web-publish.sh`（每天 07:40）负责。以下正文是
> 2026-11-07 的迁移记录，涉及旧环境文件的步骤仅作历史参考。

本文是 issue #96 的切换手册：把 dailyinfo 的每日调度从宿主
launchd / systemd 收回到 dailyinfo 自己的容器栈，并停用旧入口。仓库侧的
compose、镜像和调度代码见 [生产部署](production.md)。

## 目标形态

```text
dailyinfo compose
├─ freshrss        RSS 采集，内部服务
├─ scheduler       常驻；supercronic 读 deploy/crontab.*
├─ collector       手工排障，profiles:[jobs]
└─ dailyinfo-mcp   只读 MCP
myopenclaw         只经 MCP 读取
```

调度时点保持旧 launchd 的错峰：

| 时间（Asia/Shanghai） | Job |
|---|---|
| 03:00 | arxiv |
| 03:30 | resource |
| 03:45 | code |
| 04:00 | papers |
| 04:30 | ai_news |
| 05:30 | push_early（ai_news, code, resource） |
| 06:00 | push_papers |
| 07:00 | push_arxiv |

## 切换前置

- 生产镜像已构建：`docker build -f Dockerfile.producer -t dailyinfo-producer:<tag> .`
- `DAILYINFO_DATA_ROOT`、`DAILYINFO_PRODUCER_ENV` 已就绪；full 模式还需要
  `DAILYINFO_PUSH_ENV`。
- Docker Compose ≥ 2.24（`include` + `env_file` 长语法）。
- 已执行 `sudo -E ./deploy/bootstrap.sh`，bind mount 源目录存在且所有权正确。
- 数据目录已备份。
- **同一个数据根只能有一个 FreshRSS 写者。** 以下步骤必须串行，不能新旧栈并行。

## 81：collect-only 切换

81 只采集生成、由 MCP 消费，不推送。

```bash
# 1. 停用并删除旧 systemd 入口
sudo systemctl disable --now dailyinfo-collect.timer
systemctl is-active dailyinfo-collect.timer   # 必须是 inactive
sudo rm -f /etc/systemd/system/dailyinfo-collect.timer \
           /etc/systemd/system/dailyinfo-collect.service
sudo systemctl daemon-reload

# 2. 停掉旧 compose 项目（bind 数据保留）
cd /srv/dailyinfo   # 换成实际目录
docker compose -p dailyinfo-production -f compose.production.yml down

# 3. 单写者检查：不能有任何 dailyinfo 容器（含一次性的 collector run）
docker ps -a --format '{{.Names}}' | grep -E 'dailyinfo|freshrss' \
  && { echo "STOP: a DailyInfo container is still present"; exit 1; } \
  || echo "no dailyinfo container"
sudo lsof "$DAILYINFO_DATA_ROOT/freshrss/data/users/collector/db.sqlite" 2>/dev/null \
  && { echo "STOP: a process still holds the FreshRSS DB"; exit 1; } \
  || echo "no process holds the FreshRSS DB"
sudo lsof "$DAILYINFO_DATA_ROOT/state/scheduler.lock" 2>/dev/null \
  && { echo "STOP: an old collector is still running"; exit 1; } \
  || echo "no scheduler lock holder"

# 4. 启动新栈（不含 MCP；81 的 MCP 继续按原方式运行）
#    collect 模式写进项目 .env / deploy.env，否则下次 plain `docker compose up -d`
#    会按默认 full 模式重建并因缺少 Discord token 反复退出。
echo 'DAILYINFO_SCHEDULER_JOBS=collect' >> .env
docker compose -p dailyinfo -f compose.production.yml up -d

# 5. 验收
docker compose -p dailyinfo -f compose.production.yml ps
docker compose -p dailyinfo -f compose.production.yml exec scheduler \
  python -m scripts.scheduler status
docker compose -p dailyinfo -f compose.production.yml logs --tail 50 scheduler
```

预期：`freshrss` 与 `scheduler` 都是 healthy；`status` 显示 `mode=collect`；
启动日志先打印 `verify-env` 的 job 表，再打印 catch-up 结果。若当前时间已过
03:00，catch-up 会立即补跑当天到期的 run 任务，这是预期行为。

81 的 MCP 若原本独立运行，不要在这一步把它并入 `dailyinfo` 项目；等采集稳定后
再单独迁移。`compose.yml` 的完整形态用于新机器。

## Mac / myopenclaw：full 切换

Mac 负责生成并推送 Discord。

```bash
# 1. 卸载 myopenclaw 的 8 个 launchd job
for label in run-arxiv run-resource run-code run-papers run-ai_news \
             push-early push-papers push-arxiv; do
  launchctl bootout "gui/$(id -u)/ai.dailyinfo.$label" 2>/dev/null || true
done
launchctl list | grep dailyinfo || echo "no dailyinfo launchd job"

# 2. 删除 myopenclaw compose 里的 freshrss（stop 不够：restart: always 会在
#    Docker/宿主重启后把它拉回来）
cd ~/code/myopenclaw   # 换成实际目录
docker compose rm -sf freshrss

# 3. 单写者检查（同上，含一次性的 collector run）
docker ps -a --format '{{.Names}}' | grep -E 'dailyinfo|freshrss' \
  && { echo "STOP: a DailyInfo container is still present"; exit 1; } \
  || echo "no dailyinfo container"

# 4. 启动完整栈（production + MCP）
cd ~/code/dailyinfo
DAILYINFO_SCHEDULER_JOBS=full docker compose up -d

# 5. 验收
docker compose ps
docker compose exec scheduler python -m scripts.scheduler status
```

预期：`scheduler` 的 `verify-env` 打印 8 个 job；当天已过时点由 catch-up 补跑；
full 模式的 push 任务使用 `DAILYINFO_PUSH_ENV` 中的 Discord 凭据。

## 回滚到 systemd 调度

仓库中的 `deploy/dailyinfo-collect.{service.in,timer}` 已删除；需要回滚时按下面
内容原样恢复（`@SERVICE_ROOT@` 替换为部署目录）：

`/etc/systemd/system/dailyinfo-collect.service`：

```ini
[Unit]
Description=Collect and generate DailyInfo content (no publishing)
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=@SERVICE_ROOT@
EnvironmentFile=@SERVICE_ROOT@/secrets/producer.env
# Missing credentials skip the job; do not generate fake or placeholder content.
ExecCondition=/bin/sh -c 'test -n "$$DEEPSEEK_API_KEY"'
ExecStart=/usr/bin/flock -n /run/lock/dailyinfo-collect.lock /usr/bin/docker compose --env-file @SERVICE_ROOT@/deploy.env -p dailyinfo-production -f @SERVICE_ROOT@/compose.production.yml run --rm --no-deps collector
TimeoutStartSec=2h
UMask=0022
```

`/etc/systemd/system/dailyinfo-collect.timer`：

```ini
[Unit]
Description=Generate DailyInfo content every morning

[Timer]
OnCalendar=*-*-* 06:00:00 Asia/Shanghai
Persistent=true
Unit=dailyinfo-collect.service

[Install]
WantedBy=timers.target
```

回滚步骤：

```bash
# 1. 停新栈
docker compose -p dailyinfo -f compose.production.yml down
docker compose -p dailyinfo -f compose.mcp.yml down   # 仅当 MCP 也并入了新项目

# 2. 恢复并启用旧 timer
sudo systemctl daemon-reload
sudo systemctl enable --now dailyinfo-collect.timer

# 3. 起回旧 FreshRSS
docker compose -p dailyinfo-production -f compose.production.yml up -d freshrss
```

注意：旧的 systemd service 使用 `collector` 服务，其 `command` 现在已改为
`scripts.scheduler exec ...`；回滚前若不需要锁包装，可在旧部署目录保留旧版
compose 的副本。**旧 collector 不带 scheduler 锁**，回滚后必须确认新栈已经
完全停止（`docker ps -a` 中没有 `dailyinfo-*`），否则两个 writer 会同时写
publication store 和 FreshRSS SQLite。

## myopenclaw 交接清单

1. 注册 `dailyinfo` MCP（Streamable HTTP + Bearer），与其他 MCP 同构。
2. 删除 myopenclaw 的 `freshrss` service。
3. 删除 `scripts/launchd/ai.dailyinfo.*` 模板和 `install-dailyinfo.sh`。
4. 备份归属：旧 `backup-data.sh` 覆盖 `~/.myagentdata`（含 FreshrSS DB）。
   迁移后确认数据根仍在备份范围内，或把备份责任移到 dailyinfo 侧；否则会静默
   断档。
5. MCP 网络可达性三选一：`127.0.0.1:8768` + `host.docker.internal`、加入
   外部网络、直连 81 远程 MCP。

## 两天验收

| 日期 | 生成 | full 推送 | healthcheck | MCP 读取 |
|---|---|---|---|---|
| 2026-11-07 | catch-up 补当天 | 观察 | `docker compose ps` | 当日简报可读 |
| 2026-11-08 | 按 03:00–04:30 生成 | 05:30 / 06:00 / 07:00 | 全天 healthy | 当日简报可读 |

补充检查：

- `docker compose exec scheduler python -m scripts.scheduler healthcheck` 返回 0。
- 重启 scheduler 容器后，`status` 显示错过的 job 被补跑，Discord 没有重复消息。
- `state/scheduler_state.json` 中每个 job 的 `success_date` 等于当天日期。
