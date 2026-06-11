# 与外部调度 / 备份集成（可选）

dailyinfo 自身**不依赖任何外部 agent 框架**。本文是给已经在用 [myopenclaw](https://github.com/OuyangWenyu/myopenclaw) 之类生态的用户的一份"开箱即用"说明。

如果你只想跑 dailyinfo，请直接看 [CLI 参考](cli.md) 配置一条系统 crontab 即可，本文可以跳过。

---

## 为什么默认数据根是 `~/.myagentdata/dailyinfo/`

dailyinfo 把所有持久化数据（FreshRSS DB、briefings、pushed 归档）放在用户目录下一个统一的子目录里：

```
~/.myagentdata/dailyinfo/
```

这只是一个**约定的默认值**，可以通过环境变量 `DAILYINFO_DATA_ROOT` 覆盖到任何位置。

之所以默认选这个路径，是为了让 myopenclaw 这类把整个 `~/.myagentdata/` 作为统一备份卷的 agent 生态可以**零配置**地接管备份。如果你不用 myopenclaw，这个路径对你而言只是一个普通目录。

验证当前生效的数据根：

```bash
uv run python -c "from scripts.paths import WORKSPACE_ROOT; print(WORKSPACE_ROOT)"
```

---

## 备份（如果你用 myopenclaw）

myopenclaw 的 `docker-compose.yml` 通常把 `~/.myagentdata/` 以只读方式挂进 `backup-cron` 容器：

```yaml
volumes:
  - ${HOME}/.myagentdata:/.myagentdata:ro
```

只要 dailyinfo 的数据根保持默认值，就会自动跟着这套备份流程被快照到云盘，**dailyinfo 这一侧不用做任何事**。

排查：

```bash
ls ~/.myagentdata/dailyinfo/                            # 数据是否存在
docker compose ps                                       # backup-cron 是否在跑（在 myopenclaw 项目目录里执行）
```

---

## 调度（如果你用 myopenclaw 的 hermes cron）

把下面这几条命令注册到 hermes cron（或你自己的等价机制）即可：

| Cron | Command | Purpose |
|------|---------|---------|
| `0 6 * * *`  | `dailyinfo run -p 1` | Pipeline 1：RSS 论文 + AI 新闻 |
| `15 6 * * *` | `dailyinfo run -p 2` | Pipeline 2：代码趋势 |
| `30 6 * * *` | `dailyinfo run -p 3` | Pipeline 3：高校资讯 |
| `0 7 * * *`  | `dailyinfo push`     | 推送 Discord + 归档 |

> 调度器只是"定时触发器"——它**不需要**理解 markdown 内容，**不需要**直接发 Discord。所有读取文件 + POST Discord 的逻辑都在 `scripts/push_to_discord.py`（纯 Python，无 AI 调用）。

### 幂等保证

`dailyinfo run` 和 `dailyinfo push` 都是幂等的，可以安全地重复触发：

- **`run`**：若某数据源今日已有非占位 briefing（`briefings/` 或 `pushed/` 任一目录），跳过抓取与 AI 调用，所以重试不会重复烧 token。
- **`push`**：若今天没有待推送文件，对应频道会收到一条"暂无新简报"提示，不会重复发已归档内容。

退出码语义：`0` 表示本次至少处理了一份内容；非零仅代表"没东西可干"或出错。调度器建议不要把非零退出当告警，只监控脚本崩溃即可。

---

## 手动触发（排错 / 临时运行）

```bash
dailyinfo run                      # 立即跑一次所有 pipeline（幂等，可重复触发）
dailyinfo run -f all               # 强制刷新今天所有数据源
dailyinfo run -p 1 -f arxiv_cs_ai  # 强制刷新单个源
dailyinfo push                     # 立即推送今天已生成的 briefings
dailyinfo push -d 2026-04-22       # 补推指定日期的 briefings
dailyinfo status                   # 查看 briefings / pushed 的文件数
```

---

## Discord 频道映射

由 `.env` 配置，`push_to_discord.py` 启动时读取。每个分类一个变量：

```env
DISCORD_CHANNEL_PAPERS=...
DISCORD_CHANNEL_AI_NEWS=...
DISCORD_CHANNEL_CODE=...
DISCORD_CHANNEL_RESOURCE=...
DISCORD_CHANNEL_ARXIV=...
```

缺失某一个时该分类会被跳过（WARN），不影响其他分类。

---

## Troubleshooting

### FreshRSS DB 找不到

`run_pipelines.py` 会打印 warning 并跳过 Pipeline 1 的 RSS 部分。排查：

```bash
docker compose ps                                        # FreshRSS 容器是否在跑
ls ~/.myagentdata/dailyinfo/freshrss/data/users/         # 用户目录是否存在
```

如果你的 FreshRSS 用户名和 `$USER` 不一致，在 `.env` 里设置 `FRESHRSS_USER`。

### 推送时没有今日文件

`dailyinfo push` 在 `briefings/{category}/` 里没有今日文件时，会向对应频道发一条"暂无新简报"提示并继续处理下一个分类，不算错误。

### 数据没被备份（仅 myopenclaw 用户）

- 确认 dailyinfo 写入的是 `~/.myagentdata/dailyinfo/`：
  ```bash
  uv run python -c "from scripts.paths import WORKSPACE_ROOT; print(WORKSPACE_ROOT)"
  ```
- 确认 myopenclaw 的 `backup-cron` 容器在跑（在 myopenclaw 项目目录执行 `docker compose ps`）
- 手动触发备份（在 myopenclaw 项目目录）：
  ```bash
  docker compose exec backup-cron /scripts/backup-all-docker.sh
  ```
