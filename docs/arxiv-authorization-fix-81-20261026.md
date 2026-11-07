# arXiv 论文标题被 Authorization 校验误判的修复

## 原因与修改

2026-10-26 首轮 arXiv 已生成 321 条结构化结果，但第 25 篇论文标题 `From Review to Authorization: Key-Isolated Threshold Signing for LLM Agents` 中的 `Authorization: Key-Isolated` 被原有凭据扫描表达式匹配。整份分类简报因 `Briefing.body` 校验失败未进入 canonical 存储。

此次只修改 `scripts/publication/validation.py` 的 Authorization 扫描规则：

- 通用 Authorization 头要求具有独立行、Markdown 引用/列表或引号包裹的上下文，避免将普通标题中的相同词语当作认证头。
- 行内 Basic、Digest、Negotiate 凭据继续检测；既有 Bearer、key 赋值、密钥前缀、Webhook、调试路径、回溯及私网地址规则保留。
- 不修改 MCP 的个人 Bearer 认证逻辑，也不对某篇论文设置例外白名单。

测试新增真实标题及摘要、正文的 canonical 写入/读取回归，并新增 18 组敏感内容拦截用例，覆盖简报正文和单条摘要。先复现原始失败，再修复；完整测试 530 项通过，Black 和 Ruff 检查通过。

## 部署来源

此次部署沿用 `codex/dailyinfo-readonly-mcp` 工作树，基线为 `60416b73f7f377ade0454b8efb8843159e722225`；部署时代码尚未提交推送，没有在服务器执行 `git pull`。仅离线覆盖校验模块，不改变依赖；后续本地 Git 合并不改变这些镜像的来源。

- 校验模块 SHA-256：`5318f41c42dace90004fcd97f7f4c5cd206830d1ea146b448758c05aa2821fe5`。
- 生产镜像：`dailyinfo-producer:arxiv-auth-5318f41c42da`，ID `sha256:00741d5249dfce287968ed7d24a705aa63fa14578670ac05f135d5f0642bb203`。
- MCP 镜像：`dailyinfo-mcp:arxiv-auth-5318f41c42da`，ID `sha256:1982b57d0d21fb6304d09a839a961f3c77b19ac8c03c7fdf0c5dcf47b794cf36`。
- MCP 候选环境先使用正式只读数据完成 36 个 key、三个工具和已有内容读取验证，再更新正式容器。
- 已保存正文先在隔离容器中通过修复后校验，再启动仅 arXiv 的只生成重跑：`dailyinfo run -p 3 --force arxiv_cs_ai`。

独立恢复任务与日常生成共用 flock，避免并发；没有调用带发布行为的 `resume`，没有发送 Discord 或发布网站。MCP endpoint 保持 `http://10.48.0.81:8768/mcp`，个人 key 内容、所有者和权限不变，MCP 内容挂载仍只读。旧镜像和部署环境备份保留。

## 真实内容验收

arXiv 专项重跑已成功结束，退出码 0；`arxiv-2026-10-26` 已写入 321 条目。当前 MCP 共 3 份正式简报、1,571 条目。

- 原先触发误判的论文标题、单条内容及其所属简报可正常读取。
- 正式 MCP 的 36 个原 key、三个工具和只读写入探针通过。
- 新生成 arXiv 内容的 0640 权限与 GID 10001 正确，无需事后放宽文件权限。
- 1,250 份其他分类的条目文件逐一比对未变化，已有论文、校园资讯简报未重写。
- 从开发电脑通过正式 HTTP endpoint，完整读取 arXiv 正文 4 页、47,449 字符，并读到单条内容；原论文简报 17 页及校园资讯也读取通过。
- 投递记录仍为 0，每日 timer 保持 active，恢复日志未发现模型 key。

本次仅修复 arXiv 的标题误判；首轮代码趋势来源的外部网络失败仍需另行处理。论文输入内容的范围没有改变，仍为当前基于标题的简述。

首轮历史结果见[首次采集记录](first-collection-81-20261026.md)，完整部署方式见[采集部署](production.md)。
