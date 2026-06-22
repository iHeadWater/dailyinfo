---
name: weekly-review
description: Use when the user wants to generate a weekly literature review — fetch recent Zotero papers by date range, deep-analyze full text (not summaries), write a Chinese WeChat public account article, and generate a NotebookLM podcast. Triggers: "本周文献综述", "weekly review", "生成周报", "公众号文章", "文献周报".
---

# Weekly Literature Review

Generate a weekly literature review from Zotero papers. The workflow fetches papers added in the last 7 days, performs deep full-text analysis one paper at a time, synthesizes findings into a ~2500-word Chinese WeChat article, and optionally generates a NotebookLM podcast.

This Skill is **decoupled from the dailyinfo Python codebase**. It operates purely through Claude Code + Zotero MCP + notebooklm CLI. Do not import or call dailyinfo Python functions.

## Contract

- Do not import or call any dailyinfo Python module (`scripts/zotero_notebooklm.py`, `scripts/run_pipelines.py`, etc.).
- Use **Zotero MCP** tools exclusively for paper access: `zotero_advanced_search`, `zotero_get_item_fulltext`, `zotero_get_recent`, `zotero_get_collection_items`.
- Claude Code is the deep analysis engine. Do not call external AI APIs for analysis.
- **Per-paper processing**: analyze ONE paper at a time, write its analysis card to disk immediately, then release full-text from context before loading the next paper. Never hold more than 3 full texts in context simultaneously.
- Output directory convention: `output/weekly-review/{YYYY-MM-DD}/` with subdirectories `cards/`, `article/`, `podcast/`. Create all directories before starting analysis.
- For NotebookLM: call the `notebooklm` CLI directly via Bash. Do not use dailyinfo's `NotebookLMAutomation` wrapper class. If the CLI is unavailable, provide manual fallback steps.

### Writing Quality Contract

These rules govern ALL article output from this Skill:

- **No exaggerated claims.** Do not call something "第一个 GPT 时刻", "革命性的", or "颠覆性的" unless the paper's own authors make that claim with evidence. Prefer the paper's own framing: if they say "a new dataset", don't upgrade it to "历史性突破".
- **Fact-check every superlative.** Before writing any claim that a paper is "first", "best", "largest", or "SOTA", verify that claim against the analysis card. If the card doesn't support it, don't write it.
- **Lead with the concrete.** Start sections with a specific finding, number, method, or result — not with "本周" or "近期" or "随着...的发展".
- **Proof before adjectives.** A number (KGE=0.66) beats an adjective ("impressive accuracy"). A mechanism description beats a label ("创新性的").
- **Earned transitions.** No "值得注意的是", "此外", "另一个重要进展是" as standalone bridges. If two topics connect, show the connection with a specific shared method, dataset, or problem.
- **Story over list.** If papers fall into genuinely different domains, split into separate articles rather than forcing a disjointed list. Each article must have one clear narrative thread.

### Banned AI Patterns

Delete and rewrite any of these before delivering the article:

- "在当今快速发展的...领域" / "随着...的不断发展"
- "值得注意的是" / "另一个值得关注的进展是" / "有趣的是"
- "本周的文献呈现...的叙事线" (解说腔 — sounds like a documentary narrator)
- "游戏改变者" / "颠覆性的" / "革命性的" (unsubstantiated hype)
- "这不仅对...有重要意义，也为...提供了新的思路" (generic AI closer)
- "让我们共同期待..." (fake engagement)
- Closing questions added only to juice engagement
- "综上所述" / "总而言之" (lazy summary transitions)
- "发自..." datelines
- "互动话题" column labels

### Golden Narrative Structure

Every article should follow a natural story arc, not a paper list:

1. **Hook (钩子)** — First 2-3 sentences must grab attention. Choose from these patterns but execute naturally, not mechanically:
   - **数据实证**: "1990到2024年，中国65%的河流流量在下降。这不是模型预测——这是310个水文站34年的实测数据刚刚揭示的事实。"
   - **好奇反问**: "如果卫星从太空拍到的河流宽度照片，能反推出每秒流过多少水——我们还需要在每条河上建水文站吗？"
   - **错过遗憾**: "本周有几篇论文，可能会改变水文遥感未来几年的研究方向。"
   - **痛点直击**: "GRDC收录的中国河流站点，只有31个。全球水文模型在中国区域几乎是盲飞的。"
   - **时势跟进**: "IBM、ESA、NASA 本周联合发布了一个模型——它在9项地球观测任务上首次超越了逐任务训练的专业模型。"
   - **认知反差**: "AI研究者说现在的世界模型缺乏因果结构，水文模型研究者说物理约束才是正解——他们说的其实是同一件事。"
2. **Setup (引入)** — Establish why these papers matter together. What problem do they collectively address? What gap do they fill?
3. **Climax (展开与转折)** — The 3-5 deepest dives, each with a clear "具体问题 → 核心机制 → 实际发现 → 为什么重要" rhythm. This is where the reader should feel the intellectual payoff.
4. **CTA (总结与互动)** — One paragraph: what to watch next week, what this means for practitioners. No fake engagement, no "让我们共同期待".

## Standard Workflow

### Phase 0: Setup

1. Compute the date range: today minus 7 days. Use ISO 8601 format (`YYYY-MM-DD`).

2. Create the output directory structure:
   ```
   output/weekly-review/{YYYY-MM-DD}/
     cards/
     article/
     podcast/
   ```

3. Verify Zotero MCP connectivity with a lightweight call such as listing collections. If the MCP server returns a connection error, stop and tell the user:
   > Zotero MCP 无法连接。请确认 Zotero Desktop 7 已启动，且在 Settings → Advanced → Allow other applications to communicate with Zotero（允许其他程序通过 API 访问 Zotero）已勾选。

### Phase 1: Fetch Papers

4. Search for papers added in the last 7 days using `zotero_advanced_search`:
   - `dateAdded` `isAfter` `{seven_days_ago}` (ISO 8601 date)
   - `itemType` `is` `journalArticle` (also match `conferencePaper`, `preprint`, `report`, `thesis` if applicable)
   - Sort by `dateAdded` descending
   - If the user specifies a Zotero collection, use `zotero_get_collection_items` instead with the collection key or name.

5. Present the paper list to the user as a numbered table:
   ```
   | # | Title | First Author | Year | Journal | Date Added |
   |---|-------|-------------|------|---------|------------|
   | 1 | ...   | ...         | ...  | ...     | ...        |
   ```
   Ask the user to confirm which papers to include. The user may say "全部", "前10篇", or select specific numbers. **Do not proceed without user confirmation** — full-text extraction is expensive.

6. For each confirmed paper, call `zotero_get_item_fulltext` to retrieve the full text. If full text is unavailable (e.g., scanned PDF without OCR), fall back to `zotero_get_item_metadata` and note "⚠️ 全文不可用，仅基于摘要分析" in the analysis card.

### Phase 2: Deep Analysis (per paper, sub-agent powered)

Each paper gets its own **sub-agent** for analysis. This ensures each paper receives focused attention without context pollution, and prevents the main agent from skimming.

7. For each paper, launch a sub-agent with:
   - The paper's **full text** (from `zotero_get_item_fulltext`)
   - The **analysis card template** (8 sections below)
   - Instruction: "Write the analysis card to `cards/{slug}.md`. Read the full paper carefully. Every factual claim in the card must be traceable to a specific section of the paper. If the paper does not claim something, do not infer it."

   **Slug construction**: `{first_author_surname}_{2-3_key_title_words}` — lowercase, hyphen-separated, ASCII-safe (transliterate if needed). Example: `wang_water_quality_transformer.md`.

   **Analysis card template** (exactly 8 sections):

   ```markdown
   # {论文标题}

   - **作者**: {creators}
   - **年份**: {year}
   - **期刊**: {venue}
   - **DOI**: {doi}
   - **Zotero Key**: `{key}`

   ## 研究问题
   {What problem does this paper try to solve? Quote the research question or objective from the paper's introduction. 2-4 sentences in Chinese.}

   ## 核心方法
   {What method/framework/model does it use? Name the architecture, loss function, data pipeline specifically. If the method has a name (e.g., "MSAF-TL", "TiM"), use it. 3-6 sentences in Chinese.}

   ## 实验设计
   {What datasets, baselines, metrics were used? What was the scale (N samples, time range, spatial extent)? 2-5 sentences in Chinese.}

   ## 关键发现
   {The most important results. Include specific numbers whenever possible (accuracy, NSE, KGE, improvement %). Distinguish between the paper's claimed contribution and supporting results. 3-6 sentences in Chinese.}

   ## 局限性
   {Limitations acknowledged by the authors AND potential issues you spot (small sample, narrow geography, missing ablation, etc.). Be specific about what the paper CANNOT do. 2-4 sentences in Chinese.}

   ## 与领域的关系
   {Relevance to AI for Science / hydrology / remote sensing / scientific data processing. What methods or insights could transfer? What gap does it fill or create? 2-4 sentences in Chinese.}

   ## 精读理由
   {Why is this paper worth (or not worth) reading in depth? One clear verdict sentence. Be honest — if it's incremental, say so.}

   ## 标签
   `{tag1}` `{tag2}` `{tag3}`
   ```

8. **Verification pass**: After the sub-agent writes the card, read the card and spot-check 2-3 factual claims against the original paper text. If a claim doesn't check out, correct it. This is a lightweight gate, not a full re-analysis.

9. **After writing each card to disk**, explicitly release the full-text content from context before moving to the next paper. If the context window is filling up (>70%), suggest running `/compact` before continuing.

### Phase 3: Story Architecture & Article Synthesis

#### Step 3.0: Theme Clustering (mandatory — before writing)

10. Read all analysis cards from the `cards/` directory. **Do not re-read full-text papers.**

11. Cluster the cards by shared themes, methods, problems, or data sources. Ask:

    - **Single narrative test**: Can I write ONE story that connects at least 70% of these papers through a shared intellectual thread? If yes → one article. If no → propose split points to the user.
    - **Split criteria**: Papers belong to genuinely different fields (e.g., optimal transport theory vs. satellite hydrology vs. AGI philosophy) with no shared method, problem, or data → they should be separate articles.
    - **Group card option**: When 3+ papers cluster tightly on one theme, consider a "group analysis card" that synthesizes them rather than treating each individually.

    Show the user the proposed architecture before writing. If splitting, create separate article files: `article/article_{date}_{theme_slug}.md`.

#### Step 3.1: Write Article(s)

12. Generate ~2000-3000 word Chinese WeChat article(s) following the **Golden Narrative Structure** and **Writing Quality Contract** above.

    **Hook-first writing process**:
    1. Write the hook first. It must name a specific number, finding, contradiction, or question — not a general statement.
    2. Write the setup: why these papers matter together. What problem do they address?
    3. Write each deep-dive section: 具体问题 → 核心机制 → 实际发现 → 为什么重要. Use specific numbers from the cards. Every section must earn its place in the story.
    4. Write the CTA: what to watch next, what this means. One paragraph. No fake engagement.
    5. Write the title last — it should emerge from the finished article.

    **Article file structure**:

    ```markdown
    # {从文章内容自然产生的标题，不是模板化的}

    {Hook — 2-3 sentences, concrete and specific. No "本周" or "随着" openers.}

    ## {Setup section — named for the problem/theme, not "本周研究概览"}

    {Why these papers matter together. What problem or gap they collectively address.
    If there are distinct sub-themes that connect, name them here with specific papers.
    No paper-by-paper listing.}

    ## {Deep-dive section 1 — named for the insight, not "重点论文深度解读"}

    ### {Paper 1: concrete finding as mini-hook}

    {400-500 words. Problem → method → findings → why it matters. Specific numbers.
    Natural connection to next paper if one exists.}

    ### {Paper 2: concrete finding as mini-hook}

    {400-500 words. Same rhythm. If connected to Paper 1, make the link explicit:
    shared method? complementary data? conflicting result?}

    {3-5 deep dives}

    ## {如果有必要，一个"其他值得关注"的简洁段落}

    {Not a list. A paragraph or two that connects remaining papers to the main thread.
    Each paper gets 1-2 sentences — core contribution + why it's relevant to the story.
    If a paper doesn't connect to the main thread, consider whether it belongs in a separate article.}

    ## {Forward-looking section — named for what's next, not "总结与展望"}

    {One paragraph. What to watch next week. What this means for practitioners.
    No "综上所述", no "让我们共同期待".}
    ```

    **Section naming rule**: Section titles must describe the CONTENT, not the FUNCTION. Bad: "重点论文深度解读", "本周研究概览", "总结与展望". Good: "当卫星学会'想象'：TerraMind的多模态思维链", "中国河流的34年：从31个站点到310个".

#### Step 3.2: Article Self-Check (before evaluation)

13. Before handing off to the evaluation agent, check:
    - [ ] Hook is concrete (number, finding, contradiction, or question), not a general statement
    - [ ] No banned AI patterns from the Contract
    - [ ] Every superlative ("first", "largest", "SOTA") is backed by a specific card claim
    - [ ] Section titles describe content, not function
    - [ ] At least 3 specific numbers appear in the article
    - [ ] No paper-by-paper listing in the setup section
    - [ ] Story thread is traceable from hook to CTA

### Phase 3.5: Evaluation (mandatory — dedicated agent)

The article must be evaluated by a **separate agent** — not the same agent that wrote it. This prevents lazy self-review.

14. Launch an **evaluation agent** with:
    - The article text
    - All analysis cards from `cards/`
    - The evaluation checklist below

    The evaluation agent's job:

    #### Accuracy Check
    | Check | Method |
    |-------|--------|
    | Factual claims match cards | For every quantitative claim (numbers, percentages, rankings), verify the card supports it. Flag any mismatch. |
    | No exaggerated claims | If the article says "first", "largest", "SOTA", "GPT moment", verify the card explicitly supports that framing. If the card doesn't, flag as **HIGH — exaggeration risk**. |
    | Paper attribution correct | Every paper mention maps to exactly one card. No phantom papers. |
    | Method descriptions accurate | Spot-check 2-3 method descriptions against their cards. |

    #### Narrative Coherence Check
    | Check | Method |
    |-------|--------|
    | Single story thread | Can you trace one intellectual thread from hook to CTA? If the article jumps between unrelated topics, flag as **MEDIUM — narrative break**. |
    | Hook effectiveness | Does the first paragraph name something concrete? If it starts with "本周" or "随着", flag as **HIGH — weak hook**. |
    | Section flow | Does each section logically follow from the previous? If a section could be moved to a different position without loss, flag as **MEDIUM — weak connection**. |
    | AI flavor scan | Check for banned patterns from the Contract. Flag each instance with signal strength (强/中/弱). |
    | Listing feel | If any section reads as "Paper A did X. Paper B did Y. Paper C did Z." without connecting tissue, flag as **HIGH — list mode**. |

    #### Output Format
    ```
    ## Evaluation Report: {article_title}

    ### Accuracy
    - ✅/⚠️/❌ {finding} — {specific reference to card}

    ### Narrative Coherence
    - ✅/⚠️/❌ {finding} — {specific reference to article section}

    ### AI Flavor Scan
    - 强/中/弱 — {pattern} at {location}

    ### Verdict
    - 通过 / 需修改 (N issues)
    ```

15. **Address evaluation findings** before proceeding. If the evaluation returns ❌ on accuracy or HIGH on narrative:
    - Fix the issues
    - Re-run the evaluation agent on the changed sections only
    - **Do not proceed to Phase 4 until evaluation passes**

### Phase 4: Podcast Materials

16. Based on the synthesized analysis cards and article, write a NotebookLM podcast prompt to `podcast/podcast_prompt.md`. The prompt should:
    - Be in Chinese, targeting AI for Science / hydrology researchers
    - Cover the 3-5 featured papers with their key findings
    - Include discussion questions for the AI hosts
    - Be under 10,000 characters (NotebookLM limit)

17. Generate the podcast via `notebooklm` CLI. The CLI requires a **profile directory** — first check if `NOTEBOOKLM_HOME` is set, otherwise default to the project-local `.tmp/notebooklm/`:

    ```bash
    # Check auth first
    NOTEBOOKLM_HOME="${NOTEBOOKLM_HOME:-D:/code/dailyinfo/.tmp/notebooklm}" \
      notebooklm doctor

    # If auth missing, tell user:
    # NOTEBOOKLM_HOME="D:/code/dailyinfo/.tmp/notebooklm" notebooklm login --browser chrome
    ```

    Once auth is confirmed, run the 4-step pipeline:

    ```bash
    NOTEBOOKLM_HOME="${NOTEBOOKLM_HOME:-D:/code/dailyinfo/.tmp/notebooklm}" \
      notebooklm create "Weekly Review {YYYY-MM-DD}" --use --json

    NOTEBOOKLM_HOME="..." \
      notebooklm source add output/weekly-review/{date}/article/article_{date}.md \
        --type text --title "Weekly Article {date}" --json

    NOTEBOOKLM_HOME="..." \
      notebooklm source add output/weekly-review/{date}/podcast/podcast_prompt.md \
        --type text --title "Podcast Prompt" --json

    NOTEBOOKLM_HOME="..." \
      notebooklm generate audio \
        "请用中文对话，面向AI for Science研究者，深入讨论本周论文的核心方法和启示。" \
        --wait --timeout 900 --interval 5 --retry 3 --language zh_Hans --json

    NOTEBOOKLM_HOME="..." \
      notebooklm download audio output/weekly-review/{date}/podcast/audio_overview.mp3 \
        --force --json
    ```

    If any step fails, write `podcast/MANUAL_NOTEBOOKLM_STEPS.md` with the exact commands and paths so the user can complete the process manually in the NotebookLM web UI.

## Failure Handling

| Scenario | Resolution |
|----------|------------|
| Zotero MCP unreachable | Tell user: open Zotero Desktop 7 → Settings → Advanced → 勾选 "Allow other applications to communicate with Zotero" |
| No papers found in 7-day range | Offer to expand to 14 days, or check if the user has the correct Zotero collection selected |
| Full text unavailable (scanned/OCR-failed PDF) | Use `zotero_get_item_metadata` for abstract-based analysis. Mark card clearly: "⚠️ 全文不可用，仅基于摘要分析" |
| Context window >70% full | Pause after current paper, ask user to run `/compact`, then resume from the next unprocessed paper |
| Sub-agent fails to write analysis card | Retry once with a tighter prompt. If it still fails, write the card directly (main agent) and flag it for extra scrutiny in evaluation. |
| Evaluation agent finds accuracy issues | Fix issues → re-evaluate changed sections. Do not skip this loop. |
| Stories cannot form single narrative | Propose split to user. Wait for confirmation, then write separate articles. |
| `notebooklm` CLI not installed | `pip install notebooklm-py[browser]` into the project environment |
| `notebooklm doctor` reports no auth | Give exact command: `NOTEBOOKLM_HOME="D:/code/dailyinfo/.tmp/notebooklm" notebooklm login --browser chrome`. Explain that the user must complete browser login, then you can continue. |
| NotebookLM generation times out | The `--timeout 900` flag gives 15 minutes. If it still times out, the podcast may be very long. Write `MANUAL_NOTEBOOKLM_STEPS.md` and tell user to generate in the web UI. |
| Article too long for single NotebookLM source | Split into multiple text sources: `article_part1.md`, `article_part2.md`. NotebookLM limit is ~500K words per source. |

## Reporting

After each phase, report progress:

- **Phase 0**: "输出目录已创建: `output/weekly-review/{date}/`"
- **Phase 1**: "从 Zotero 获取到 N 篇论文（{date_range}），用户确认 M 篇"
- **Phase 2**: "已完成 M 篇深度解析（N 篇通过子代理），分析卡保存至 `cards/`" — list the slugs
- **Phase 3**: "故事架构: {single narrative / N separate articles}" → "文章已生成: `article/article_{date}.md` ({word_count} 字)"
- **Phase 3.5**: "评估结果: {通过 / N issues found and resolved}"
- **Phase 4**: "播客已生成: `podcast/audio_overview.mp3`" OR "播客素材已准备，需手动完成: 见 `podcast/MANUAL_NOTEBOOKLM_STEPS.md`"

Final summary:

> ## 本周文献综述完成
> - **日期范围**: {start} ~ {end}
> - **论文数**: {N} 篇深度解析
> - **文章**: `output/weekly-review/{date}/article/article_{date}.md`
> - **评估**: {通过 / N issues resolved}
> - **分析卡**: `output/weekly-review/{date}/cards/`
> - **播客**: {audio_path 或 "需手动生成"}
> - **下一步**: {建议}
