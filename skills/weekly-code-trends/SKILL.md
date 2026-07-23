---
name: weekly-code-trends
description: Generate a weekly GitHub Trending deep-dive article. Runs the data pipeline, then launches parallel sub-agents for per-repo code analysis, synthesizes into a structured Chinese WeChat article. Triggers: "代码周报", "GitHub weekly", "开源项目周报", "code trends weekly", "本周开源精选".
---

# GitHub Trending Weekly Deep-Dive

Generate a weekly GitHub Trending curated article with deep code-level analysis. The workflow runs the data preparation pipeline (`dailyinfo code-weekly`), then orchestrates multiple Claude Code sub-agents for parallel per-repo analysis, and synthesizes findings into a ~1500-word Chinese article.

This skill is designed to be invoked by external agent runtimes (myopenclaw / Claude Code). It assumes `dailyinfo` CLI is installed and functional.

## Contract

- Do not modify `weekly_code_trends.py`. This skill only reads the JSON output.
- Run `dailyinfo code-weekly` (or `python scripts/weekly_code_trends.py`) to generate the JSON data file. If today's data already exists, pass `--force` to regenerate.
- The JSON data file lives at `{briefings_dir}/code_weekly/data_{DATE}.json`.
- **Per-repo sub-agent rule**: EVERY repo in the top-5 list gets its own dedicated sub-agent for deep analysis. The main agent does NOT analyze repos directly — it only orchestrates.
- Each sub-agent MUST: (a) fetch the repo's README + recent commits via GitHub API, (b) analyze what the repo does, its technical approach, and why it's trending, (c) output a structured analysis card following the 4-section template.
- **Synthesis agent**: After all 5 sub-agents complete, launch a synthesis agent that reads all 5 cards and produces the final Markdown article.
- **Evaluation agent**: Launch an independent evaluation agent to audit the final article — accuracy, depth, AI-pattern detection. Fix issues and re-evaluate until the verdict is "通过".
- Output: write the final article to `{briefings_dir}/code_weekly/code_weekly_{DATE}.md`, then return it as the reply message (CC飞总会自动推送到飞书对话).

## Quality Standards

A polished article must meet these gates:

- [ ] **导读** hooks with a specific trend or insight — not "本周GitHub上有许多优秀项目". 不超过 5 句话。
- [ ] **Each repo** has all 4 analysis dimensions: 是什么 / 能做什么 / 为什么火 / 技术亮点
- [ ] **At least 5 specific numbers** (star count, release date, version numbers, etc.), 每个数字有参照系
- [ ] **No AI filler patterns**: 检查完整的 Banned AI Patterns 黑名单
- [ ] **Technical depth**: each repo analysis shows evidence of reading the actual README/code — cite specific features, API endpoints, or architecture decisions
- [ ] **Why it matters**: each repo explains WHY it's trending — technical breakthrough, ecosystem gap, community effect, or industry timing
- [ ] **5 repos, not more, not less** — 在精不在多（数据不足 5 个时如实说明）
- [ ] **取舍分明**: 有深度的 repo 展开写，无实质内容的 repo 100 字带过
- [ ] **长短句交替**: 不连续 3 个长句（>40 字），不连续 3 个短句（<15 字）
- [ ] **信息来源**: 文章末尾包含 "📮 信息来源" 脚注
- [ ] **Phase 3 评估 + Phase 3.5 Re-Audit 双审计**: 8 维全部 ≥3，均分 ≥4.0

### Banned AI Patterns

以下表述永远不要出现在最终文章中（持续更新）：

- "在当今快速发展的...领域" / "随着...的不断发展"
- "值得注意的是" / "值得一提的是" / "另一个值得注意的是"
- "此外" / "另一个重要进展是" / "与此同时"（当没有真实并发关系时）
- "综上所述" / "总而言之" / "整体来看"
- "本周GitHub上有许多优秀项目" / "本周开源社区十分活跃"
- "让我们共同期待..." / "值得持续关注"
- "Star 数快速增长" without a specific number
- "该仓库的出现标志着..." (overclaiming)
- "不仅在...也在..." / "既是...也是..." / "从...到...再到..."
- "不可否认" / "毫无疑问" / "显然"
- Closing questions added only to juice engagement

## Workflow

### Phase 0: Run the data pipeline

1. Run `dailyinfo code-weekly --force` to generate fresh JSON data.
2. Read the JSON file at `{briefings_dir}/code_weekly/data_{DATE}.json`.
3. If no file exists, the data pipeline failed — report the error and stop.
4. Confirm 5 repos in `top_repos`. If fewer than 5, proceed with what's available.

### Phase 1: Per-Repo Deep Analysis (5 parallel sub-agents)

For each repo in `top_repos`, launch a sub-agent with this instruction template:

```
You are a code analysis specialist. Analyze this GitHub repository deeply.

**Repository**: {full_name} (⭐ {stars}, language: {language})
**Description**: {description}
**URL**: {url}
**Appeared on**: {day_count} of 7 days this week

## Your Task

1. **Fetch README**: Use `gh api repos/{full_name}/readme` or WebFetch to visit `https://github.com/{full_name}`. Read and understand what this project does.
2. **Fetch recent activity**: Use `gh api repos/{full_name}/commits?per_page=5` or WebFetch the repo's commit page to see recent development activity.
3. **Analyze** and output a structured analysis card in Chinese:

### [{full_name}]({url}) — ⭐ {stars}

- **是什么**：{one paragraph, concrete description of what this project is, not marketing speak}
- **能做什么**：{specific use cases, ideally with code-level detail — what problem does it solve and HOW}
- **为什么火**：{why is this trending NOW — technical breakthrough? ecosystem timing? community momentum? shipping something people needed?}
- **技术亮点**：{1-2 specific technical observations from reading the README/code — architecture choice, algorithm, API design, performance characteristic}
- **一句话推荐**：{one sharp sentence for why a developer should check this out}

IMPORTANT: Read the actual README and commits. Do not summarize from the description alone.
Evidence of reading: cite specific features, API endpoints, or architecture decisions mentioned in the README.
```

Launch all 5 sub-agents in parallel. Wait for all to complete before proceeding to Phase 2.

### Phase 2: Synthesis

Launch a synthesis agent with this instruction:

```
You are a tech editor for a Chinese WeChat public account focused on AI and open-source tools.

## Your Task

Read 5 analysis cards below and write a ~1500-word Chinese article: "本周 GitHub 精选 — {DATE}"

## Article Structure

### 导读 (~100 words)
Open with a sharp observation about this week's trend. Pick a specific repo or number to hook the reader. One paragraph. No "本周GitHub上".

### 本周精选仓库
For each of the 5 repos, synthesize the analysis card into ~250 words. Preserve all 4 dimensions (是什么/能做什么/为什么火/技术亮点). Maintain technical depth — readers are developers.

### 其他值得关注
If the JSON data includes repos beyond the top 5, list them as one-liners with star counts.

## Writing Rules
- Chinese throughout, with repo names and technical terms in English where appropriate
- No AI filler patterns (值得注意的是, 此外, 综上所述, etc.)
- Every claim about a repo must come from the analysis card
- Keep the tone: knowledgeable but not hype-y, technical but readable
- Ranking rationale: explain briefly why each repo made the cut (day count, star velocity)

## Analysis Cards
{insert 5 analysis cards here}
```

#### 写作风格（CRITICAL）

##### 节奏控制

- **长短句交替**。不要连续三个长句（>40 字），也不要连续三个短句（<15 字）。用短句收束一个观点，用长句展开一个论证。
- **段落开头换句式**。不要每段都以"XXX 是一个..."或"XXX 发布了..."开头。可以以数字开头、以问题开头、以场景开头。
- **导读不超过 5 句话**。如果写了 6 句，删一句。导读的作用是让读者 30 秒决定是否读下去，不是概括全文。

##### 分析深度

- **不只说"是什么"，要说"为什么重要"**。写了"本周获得 X star"只是报道；写了"本周获得 X star——这意味着 Y 领域正在 Z"才是分析。
- **数字要有参照系**。"5000 star" 不如 "5000 star——在 Haskell 社区这属于现象级增速"。
- **用具体代替抽象**。"性能优异" → "benchmark 显示比原生基线快 3 倍"。

##### 取舍纪律

- **不是 5 个 repo 都要写 250 字**。如果某个 repo 没有实质技术内容，100 字带过即可。把篇幅留给真正有深度的项目。
- **一个论证用 2-3 个论据支撑即可，不必堆到 5 个**。读者不需要被说服 5 次。
- **如果两个 repo 本质在解决同一个问题，合并成一个论证段落，不要分别写**。

#### 信息来源脚注

文章末尾必须追加：

> 📮 **信息来源**：本文基于 dailyinfo 每日 GitHub Trending 简报生成，仓库数据经 GitHub API 实时验证。数据截至本周日。

### Phase 3: Independent Evaluation (HARD GATE — 不可跳过)

Launch an evaluation agent that reads the final article and scores it:

| 维度 | What to check | Score (1-5) |
|------|--------------|-------------|
| 导读质量 | 用具体 trend/数字/矛盾切入，非泛化开头 | |
| 技术深度 | 每 repo 展示读了 README/code 的证据（引用具体 feature/API/架构决策），非仅描述 | |
| 主线聚焦 | 每 repo 分析有主次之分，非平均分配篇幅 | |
| 准确性 | 所有声称匹配分析卡片，无编造 | |
| AI味扫描 | 无黑名单套话 | |
| 递进克制 | 只有真实因果/时序关系才写"随后/紧接着"，独立事件各自成段 | |
| 术语密度 | 每 repo 不堆砌框架/公司名，核心项目名保留 | |
| 可读性 | 长短句交替，段落开头换句式，适合开发者阅读 | |

**Pass 阈值**: 全部 8 维 ≥ 3，均分 ≥ 4.0。

If the article fails:
1. Report the specific weaknesses with line references
2. Fix them (rewrite the problematic sections)
3. Re-evaluate

Loop until pass or 3 attempts (whichever comes first). If still failing after 3 attempts, report remaining issues and deliver with caveats.

### Phase 3.5: Independent Re-Audit（独立子 agent — 不可跳过）

启动一个**全新的 sub-agent**（不是写文章或首次评估的 agent）重评最终文章：

1. 给 sub-agent 完整的 8 维评分表 + 文章全文
2. Sub-agent 逐维打分，每项扣分必须引用具体行号
3. **Exit criteria**（全部满足才通过）:
   - 全部 8 维 ≥ 3
   - 均分 ≥ 4.0
   - 至少 3 维比首次评估（Phase 3）提升 ≥ 1 分
4. 不通过 → 回到 Phase 2 修复指定章节 → 重新 Phase 3 评估 → 重新 Phase 3.5 Re-Audit
5. **最多 3 轮完整 rewrite 循环**。超过则 `<!-- outstanding: ... -->` 标注剩余问题，继续交付。

### Phase 4: Deliver

1. 保存最终文章到 `{briefings_dir}/code_weekly/code_weekly_{DATE}.md`。文件头部包含 metadata comment：
```markdown
<!-- code-weekly: date={DATE} repos={N} day_count={D} -->

# 本周 GitHub 精选 — {DATE}

{article content}

---
> 📮 **信息来源**：本文基于 dailyinfo 每日 GitHub Trending 简报生成，仓库数据经 GitHub API 实时验证。数据截至本周日。
```

2. **将文章全文作为回复返回给用户**（CC飞总会自动推送到飞书对话）。
3. 附带简短摘要：
   - Top 5 repos 列表（含 star 数）
   - Phase 3 首次评估分数 + Phase 3.5 Re-Audit 分数
   - 文件路径
   - 如果 Re-Audit 未完全通过，注明 outstanding 维度

## Failure Handling

| 场景 | 处理 |
|------|------|
| 无 github_trending briefings | 告知用户：本周暂无 GitHub Trending 数据，无法生成周报 |
| GitHub API 无法访问某个 repo | 用 JSON 中已有的 description 作为 fallback，在分析卡片中标注 `<!-- TODO: verify -->` |
| Sub-agent 分析内容空洞 | 该 repo 缩减为 2-3 句话简介，不再展开 4 维分析 |
| 评估 3 轮不通过 | 带 `<!-- outstanding: ... -->` 交付，注明未达标维度和剩余问题 |
| JSON 中 repo 不足 5 个 | 有几个写几个，不编造。导读中如实说明"本周共收录 N 个热门仓库" |
| `dailyinfo code-weekly` 执行失败 | 报告错误信息给用户，不继续后续步骤 |
| WebFetch 无法访问 GitHub | 回退到 `gh api` CLI 方式获取数据 |
