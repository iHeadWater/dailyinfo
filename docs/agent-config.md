# Integration with myopenclaw

dailyinfo 与 [myopenclaw](https://github.com/OuyangWenyu/myopenclaw) 通过文件系统和 CLI 调用解耦。本文说明如何把 dailyinfo 接入 myopenclaw 的调度与备份流程。

## 数据根路径

dailyinfo 的数据根由 `scripts/paths.py` 决定：

1. 如果 `.env` 或环境变量中有 `DAILYINFO_DATA_ROOT`，使用该路径；
2. 否则默认 `~/.myagentdata/dailyinfo`。

验证当前路径：

```bash
python3 -c "from scripts.paths import WORKSPACE_ROOT; print(WORKSPACE_ROOT)"
```

## 备份（自动）

myopenclaw 的 `docker-compose.yml` 里 `backup-cron` 服务已经挂载：

```yaml
volumes:
  - ${HOME}/.myagentdata:/.myagentdata:ro
```

只要 dailyinfo 的数据落在 `~/.myagentdata/dailyinfo/`（默认），就会被定时快照到云盘，无需额外配置。

验证备份路径下已有 dailyinfo 数据：

```bash
ls ~/.myagentdata/dailyinfo/
# freshrss/  briefings/  pushed/
```

## 调度（推荐）

推荐由 myopenclaw 的 hermes cron 触发 dailyinfo CLI。调度项示例：

| Cron | Command | Purpose |
|------|---------|---------|
| `0 6 * * *`  | `dailyinfo run -p 1` | Pipeline 1: RSS papers + AI news |
| `15 6 * * *` | `dailyinfo run -p 2` | Pipeline 2: code trending |
| `30 6 * * *` | `dailyinfo run -p 3` | Pipeline 3: university news |
| `0 7 * * *`  | `dailyinfo push`     | 推送 Discord + 归档 |

在 hermes 的 `cron/` 配置里注册对应脚本或 shell 调用即可。

> hermes / openclaw 不需要理解 markdown 内容，也不需要自己发 Discord 消息。它们只是定时触发器。真正的"读文件 + POST Discord"逻辑全在 `scripts/push_to_discord.py` 里，是纯 Python 确定性代码。

## 手动触发（排错 / 临时运行）

```bash
dailyinfo run                # 立即跑一次所有 pipeline
dailyinfo push               # 立即推送今天已生成的 briefings
dailyinfo status             # 查看 briefings/ 和 pushed/ 的文件数
```

## Discord Channel Mapping

由 `.env` 配置，`push_to_discord.py` 启动时读取。每个分类一个变量：

```env
DISCORD_CHANNEL_PAPERS=...
DISCORD_CHANNEL_AI_NEWS=...
DISCORD_CHANNEL_CODE=...
DISCORD_CHANNEL_RESOURCE=...
```

缺失某一个时该分类会被跳过（WARN），不影响其他分类。

## Troubleshooting

### FreshRSS DB 找不到

`run_pipelines.py` 会打印 warning 并跳过 Pipeline 1。排查步骤：

```bash
# 1. 检查 FreshRSS 容器是否在跑
docker compose ps

# 2. 检查数据目录
ls ~/.myagentdata/dailyinfo/freshrss/data/users/

# 3. 如果用户名和 $USER 不同，在 .env 里设置 FRESHRSS_USER
```

### 推送失败 / 没有今日文件

`dailyinfo push` 当 `briefings/{category}/` 里没有今日文件时，会向对应频道发一条"暂无新简报"的提示，并继续处理下一个分类。

### 数据没被备份

- 确认 dailyinfo 写入的是 `~/.myagentdata/dailyinfo/`（用上文验证命令）
- 确认 myopenclaw 的 `backup-cron` 容器在跑：`docker compose ps` in myopenclaw
- 手动触发备份：`docker compose exec backup-cron /scripts/backup-all-docker.sh`
