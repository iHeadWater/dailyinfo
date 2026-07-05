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
- **Per-paper sub-agent rule**: EVERY paper gets its own dedicated sub-agent for reading and analysis. The main agent does NOT read full papers directly — it only orchestrates. For each paper, launch a sub-agent that (a) reads the full text in sequential chunks until 100% coverage, (b) writes the analysis card following the 8-section template, and (c) returns a brief summary. The main agent then verifies the card's structure and spot-checks 2-3 claims. This isolates each paper's context, prevents pollution across papers, and lets the sub-agent's reading technique be optimized independently.
- **Sub-agent reading technique**: The sub-agent MUST read the full paper completely — no skimming, no abstract-only analysis. Read the file in small sequential chunks (limit=50-80 lines each) until the entire content is consumed. Before writing any card, the sub-agent MUST state what percentage of the paper it has read. If the paper is too large to fit in a single chunk, the sub-agent reads across multiple turns, accumulating understanding before writing the card.
- **No card, no article.** Do not proceed to Phase 3 (article writing) for any paper that lacks a completed analysis card in `cards/`. The card is the fact layer; the article is the narrative layer. Writing an article without cards means all claims are unverifiable — the audit found this is the strongest predictor of factual errors. Papers without cards may be listed in a brief "also added this week" note, but not analyzed in depth.
- **Card-article sync.** If an article is substantially rewritten after initial creation (Phase 3.5 evaluation feedback, user revision requests), the corresponding analysis cards must be re-audited before the next evaluation pass. After any rewrite that changes factual claims, numbers, terminology, or limitation coverage, the main agent must read each card alongside the revised article and verify: (a) all numbers cited in the article still match the card, (b) no new factual claims were added that the card doesn't support, (c) no card-documented limitations or frameworks were dropped from the article. Run this sync check before re-invoking the Phase 3.5 evaluator.
- Output directory convention: `output/weekly-review/{YYYY-MM-DD}/` with subdirectories `cards/`, `article/`, `podcast/`. Create all directories before starting analysis.
- For NotebookLM: call the `notebooklm` CLI directly via Bash. Do not use dailyinfo's `NotebookLMAutomation` wrapper class. If the CLI is unavailable, provide manual fallback steps.
- **Evaluation is NOT optional**: Phase 3.5 (independent evaluation agent) is a hard gate. Every article must pass evaluation before delivery. The main agent MUST NOT skip or shortcut this phase. If an evaluation finds issues, fix them and re-evaluate until the verdict is "通过".

### Writing Quality Contract

These rules govern ALL article output from this Skill:

- **No exaggerated claims.** Do not call something "第一个 GPT 时刻", "革命性的", or "颠覆性的" unless the paper's own authors make that claim with evidence. Prefer the paper's own framing: if they say "a new dataset", don't upgrade it to "历史性突破".
- **Fact-check every superlative.** Before writing any claim that a paper is "first", "best", "largest", or "SOTA", verify that claim against the analysis card. If the card doesn't support it, don't write it.
- **Lead with the concrete.** Start sections with a specific finding, number, method, or result — not with "本周" or "近期" or "随着...的发展".
- **Proof before adjectives.** A number (KGE=0.66) beats an adjective ("impressive accuracy"). A mechanism description beats a label ("创新性的").
- **Earned transitions.** No "值得注意的是", "此外", "另一个重要进展是" as standalone bridges. If two topics connect, show the connection with a specific shared method, dataset, or problem.
- **Story over list.** If papers fall into genuinely different domains, split into separate articles rather than forcing a disjointed list. Each article must have one clear narrative thread.
- **Respect the source's own emphasis. Do NOT cherry-pick.** When reporting on a broad document (survey, annual report, multi-topic review), do not amplify a minor mention into the appearance of a major focus just because it aligns with the reader's domain. If a report devotes 2% of its Science chapter to hydrology, the article must reflect that proportion — not restructure the narrative as if the chapter were about hydrology. Always preserve: (a) the source's chapter structure and relative weight, (b) which claims are the source's own framing vs. your extraction, (c) what broader context surrounds any specific data point you highlight. **The reader should finish the article understanding what the source actually emphasizes, not just what is relevant to them.**
- **For AI/ML methods papers: present the paper first, connect to domain second.** The default ratio should be ~80% faithful presentation of what the paper says and ~20% brief pointers on domain relevance. Do not over-translate — don't reframe every concept through a hydrology lens. Trust the reader to make their own connections. The domain guidance should be a short paragraph at the end, clearly signaled as our extrapolation (e.g., "从水文建模的角度看，这篇论文的三个思想值得关注：…"), not woven into the main exposition as if the paper itself addresses hydrology. A hydrologist reading an AI methods paper wants to understand the method on its own terms first.

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

### Writing Style

Every article should follow a natural arc, not a paper list. The default tone is **objective and restrained** — let the papers speak for themselves.

1. **Opening** — First 2-3 sentences state what the papers are about, without dramatization. Acceptable patterns:
   - **具体发现**: "一项针对56位水文学者的调查显示，54%承认FAIR数据原则在自己的课题组里经常未被遵守。"
   - **方法观察**: "LLM正在进入水文模拟——不是替代物理模型，而是替代手写的行为假设。"
   - **直接陈述**: "本周有两篇论文讨论了成像光谱学的未来。一篇是NASA的路线图，一篇是港大的预测模型。"
   - Avoid: "可能会改变未来几年的研究方向", "历史性突破", "GPT时刻", "首次"（unless the paper itself uses these terms with evidence）。
2. **Setup** — If papers share a genuine theme, name it. If they don't, say so and discuss them separately. Do not invent connections.
3. **Deep-dive** — Each paper gets a "具体问题 → 核心机制 → 实际发现 → 为什么重要" treatment. Specific numbers over adjectives.
4. **Close** — One paragraph on what to watch next. No "让我们共同期待", no rhetorical questions.

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

### Phase 2: Deep Analysis (per-paper sub-agents)

The main agent NEVER reads full papers directly. Every paper gets its own **dedicated sub-agent**. The main agent's role is pure orchestration: launch sub-agents, verify card output, keep context clean for Phase 3 synthesis.

7. For each confirmed paper, the main agent:
   a. First calls `zotero_get_item_fulltext` to retrieve the full text. If the result is inline, pass it directly to the sub-agent. If it's a persisted-output file path, pass the file path to the sub-agent — the sub-agent will read the file in chunks. **If the paper is extremely large (>100K chars, e.g. a multi-hundred-page annual report), pre-split it first:** run `python scripts/chunk_fulltext.py <saved_json> --output-dir output/weekly-review/{date}/chunks/ [--markers markers.json]` to split the fulltext into manageable files by section markers (or fixed-size chunks as fallback). Then pass individual chunk file paths to sub-agents.
   b. Launches a sub-agent with:
      - The paper's full text (inline) OR the file path to read (persisted output)
      - The **analysis card template** (8 sections below)
      - The **slug** for the output file
      - Instruction: "Read the FULL paper — no skimming. If reading from a file, use sequential chunks (limit=50-80 lines) until 100% coverage. Before writing the card, state what percentage you read. Every factual claim must be traceable to a specific section of the paper. Write the card to `cards/{slug}.md`."

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

8. **Verification pass**: After each sub-agent writes its card, the main agent reads the card and spot-checks 2-3 factual claims. If the paper's full text is available (inline or via the persisted-output file), verify claims against the original text. If a claim doesn't check out, correct it in the card. This is a lightweight gate, not a full re-analysis.

9. **Context discipline**: The main agent never accumulates full-text content. Each sub-agent's output is just the analysis card (~2-5K chars) — small enough that all 7-10 cards can coexist in the main session during Phase 3 synthesis. If context still exceeds 70%, suggest `/compact` before proceeding to article writing.

### Phase 3: Story Architecture & Article Synthesis

#### Step 3.0: Theme Clustering (mandatory — before writing)

10. Read all analysis cards from the `cards/` directory. **Do not re-read full-text papers.**

11. Cluster the cards by shared themes, methods, problems, or data sources. Ask:

    - **Connection check**: Do these papers share a genuine intellectual thread (shared method, problem, dataset, or finding)? If yes, they belong in one article and the connection should be named explicitly. If no — if they represent different fields or unrelated questions — **write separate articles**. Do not invent connections. The reader can handle three honest short articles better than one forced long one.
    - **Split criteria**: Papers from genuinely different fields with no shared method, problem, or data → separate articles. This is the NORMAL case; single-narrative is the exception, not the expectation.
    - **Group card option**: When 3+ papers cluster tightly on one theme, a group analysis card can synthesize them.

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

### Phase 3.5: Evaluation (mandatory — dedicated agent, DO NOT SKIP)

The article must be evaluated by a **separate agent** — not the same agent that wrote it. This prevents lazy self-review. **This phase is a hard gate: do not proceed to Phase 4 until every article passes evaluation.**

14. For EACH article, launch an independent **evaluation agent** with:
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

    #### Terminology Precision Check
    | Check | Method |
    |-------|--------|
    | Domain term accuracy | For every domain-specific term (e.g., "流域属性", "产流机制", "同化"), verify it's used with its accepted disciplinary meaning. "流域属性" must refer to static catchment characteristics, not time series. Flag misuse as **HIGH — term misuse**. |
    | Jargon accessibility | If a technical term is essential to understanding the sentence and is NOT common knowledge for a hydrology graduate student (e.g., "C 波段散射计", "Budyko 框架", "求积权重"), it should be briefly explained on first use. Flag unexplained niche jargon as **MEDIUM — accessibility gap**. |
    | Concept conflation | Check whether distinct concepts are conflated under one term (e.g., calling both time series and static attributes "流域属性"). Flag as **HIGH — concept conflation**. |
    | Title-to-content alignment | Does the section title match what the section actually discusses? A title promising "属性增强" but describing time series upgrades is a mismatch. Flag as **MEDIUM — title-content mismatch**. |

    #### Source Fidelity Check (for survey/report/multi-topic sources)
    | Check | Method |
    |-------|--------|
    | Proportional representation | Compare the article's word count per topic to the source's. If the source devotes 15% of its Science chapter to Earth science and 2% to hydrology, the article should not read as if the chapter is about hydrology. Flag disproportionate emphasis as **HIGH — cherry-picking**. |
    | Source's own framing preserved | If the source says "AI is beginning to show promise in X" and the article says "AI is transforming X", that's a distortion. Check 3-5 key claims against the source's exact wording. Flag framing inflation as **HIGH — source distortion**. |
    | Context transparency | When spotlighting a specific data point (e.g., "LSTM outperforms process-based models"), does the article indicate where this sits in the source's broader structure? If a reader would mistakenly think this was a headline finding of the source rather than a detail in a subsection, flag as **MEDIUM — missing context**. |
    | AI methods paper: domain-application ratio | For articles on AI/ML methods papers (not application papers), count the proportion of text devoted to domain application vs. faithful presentation of the method. If domain application exceeds ~30% of total word count, flag as **MEDIUM — over-translation**. The article should present the method on its own terms; domain pointers belong in a clearly marked short section at the end. |

    #### Output Format
    ```
    ## Evaluation Report: {article_title}

    ### Accuracy
    - ✅/⚠️/❌ {finding} — {specific reference to card}

    ### Terminology Precision
    - ✅/⚠️/❌ {finding} — {specific reference to article text}

    ### Narrative Coherence
    - ✅/⚠️/❌ {finding} — {specific reference to article section}

    ### AI Flavor Scan
    - 强/中/弱 — {pattern} at {location}

    ### Verdict
    - 通过 / 需修改 (N issues)
    ```

15. **Address evaluation findings** before proceeding. If the evaluation returns ❌ on accuracy or HIGH on narrative:
    - Fix the issues in the article immediately
    - Re-run the evaluation agent on the changed article
    - **Repeat until the verdict is "通过" (pass). Do not proceed to Phase 4 with unresolved issues.**
    - For LOW/MEDIUM issues, fix them but re-evaluation is at the main agent's discretion based on the number and severity of changes.

### Phase 4: Per-Direction Podcasts

Each article/direction gets its own independent podcast. One podcast covers 2-4 papers on a single theme. This keeps each episode focused, prevents forced connections between unrelated topics, and produces digestible ~15-20 minute episodes.

#### Factual Grounding Principle

**Upload all analysis cards and original PDFs for each direction.** The cards contain structured, fact-checked extractions; the PDFs give raw completeness. The article is uploaded for narrative structure only. The AI host is instructed to ground every factual claim in a card or PDF, not in an article.

#### Per-Direction Pipeline

16. For EACH direction/article, create a dedicated NotebookLM notebook and upload only the sources relevant to that direction:

    **Notebook naming**: `"{Theme} {YYYY-MM-DD}"` — e.g., `"Hydrology 2026-06-28"`, `"Remote Sensing 2026-06-28"`, `"AI Foundations 2026-06-28"`.

    **For each notebook, upload in this order:**

    **Step 16a — Analysis cards for this direction only (mandatory):**
    ```bash
    NOTEBOOKLM_HOME="..." \
      notebooklm source add output/weekly-review/{date}/cards/{paper_slug}.md \
        --type text --title "{Paper Title} — Analysis Card" --json
    ```

    **Step 16b — Original PDFs for this direction only (mandatory):**
    ```bash
    NOTEBOOKLM_HOME="..." \
      notebooklm source add "podcast/pdfs/{paper_slug}.pdf" \
        --type file --title "{Paper Title} (Original PDF)" --json
    ```

    **Step 16c — Article for this direction (narrative structure):**
    ```bash
    NOTEBOOKLM_HOME="..." \
      notebooklm source add output/weekly-review/{date}/article/article_{date}_{theme}.md \
        --type text --title "Article: {theme}" --json
    ```

    **Step 16d — Podcast prompt for this direction:**
    ```bash
    NOTEBOOKLM_HOME="..." \
      notebooklm source add output/weekly-review/{date}/podcast/podcast_{theme}.md \
        --type text --title "Podcast Instructions" --json
    ```

17. Write a podcast prompt for each direction to `podcast/podcast_{theme}.md`. Each prompt MUST include these rules:

    > **事实锚定**：讨论的每一条具体事实（数字、百分比、方法名称）必须来自 Analysis Card 或原始 PDF。Article 的内容已经过改写和筛选，仅用于了解论文之间的关系。
    >
    > **开场白**：直接从论文内容出发，用一两句平实的陈述开场。例如——"这周有三篇和水文监测相关的论文。第一篇讨论了野外数据采集中的常见问题..."。不要假设场景（"想象你是一个..."），不要设问（"你有没有想过..."），不要震惊体，不要编故事。
    >
    > **结构**：逐篇讨论。每篇四段——它做了什么、怎么做的、发现了什么、对水文研究意味着什么。论文之间有自然关联就提，没有就各自独立。不强求串联。
    >
    > **水文学者视角**：从水资源/水文/地球科学研究者的关切出发。AI 方法论文的重点是"对我有什么用"而非技术炫技。深度严谨，表达平实。

    Each prompt should additionally:
    - Be in Chinese, single-host narration style (not two-host dialogue)
    - Cover only the papers in this direction (2-4 papers)
    - Be under 5,000 characters (~500 words in Chinese, much shorter since only 2-4 papers)

18. Generate audio for each direction independently:

    ```bash
    # Per direction:
    NOTEBOOKLM_HOME="${NOTEBOOKLM_HOME:-D:/code/dailyinfo/.tmp/notebooklm}" \
      notebooklm create "{Theme} {YYYY-MM-DD}" --use --json

    # Upload direction-specific sources (Steps 16a-16d)

    NOTEBOOKLM_HOME="..." \
      notebooklm generate audio \
        "一位水文学者，单人讲解，平实客观地介绍这组论文。开场白直接从论文内容出发，不要假设场景或设问。逐篇讨论，每篇四段：做了什么、怎么做的、发现了什么、意味着什么。所有事实来自Analysis Card和PDF。" \
        --wait --timeout 900 --interval 5 --retry 3 --language zh_Hans --json

    NOTEBOOKLM_HOME="..." \
      notebooklm download audio output/weekly-review/{date}/podcast/audio_{theme}.mp3 \
        --force --json
    ```

    Audio files: `podcast/audio_{theme}.mp3` — one per direction. If any step fails, write `podcast/MANUAL_NOTEBOOKLM_STEPS_{theme}.md`.

## Failure Handling

| Scenario | Resolution |
|----------|------------|
| Zotero MCP unreachable | Tell user: open Zotero Desktop 7 → Settings → Advanced → 勾选 "Allow other applications to communicate with Zotero" |
| No papers found in 7-day range | Offer to expand to 14 days, or check if the user has the correct Zotero collection selected |
| Full text unavailable (scanned/OCR-failed PDF) | Use `zotero_get_item_metadata` for abstract-based analysis. Mark card clearly: "⚠️ 全文不可用，仅基于摘要分析". Still use a sub-agent — it works from metadata instead of full text. |
| Sub-agent fails to read paper completely | Check if the sub-agent reported <100% coverage. If so, re-launch with a smaller chunk size (limit=30-40) and explicit instruction to continue until end-of-file. If the file is fundamentally unreadable (corrupted JSON, encoding error), fall back to abstract-based analysis with clear marking. |
| Full text too large for single sub-agent (>100K chars) | Use `python scripts/chunk_fulltext.py <fulltext.json> --output-dir output/weekly-review/{date}/chunks/ --markers markers.json` to pre-split the fulltext. Create a `markers.json` with `{"section": {"start": "...", "end": "..."}}` entries using the paper's section headers. Then launch one sub-agent per chunk file. For a 1M+ character document, this can produce ~10 manageable chunks. |
| Sub-agent fails to write analysis card | Retry once with a tighter prompt. If it still fails, the main agent writes the card directly from metadata and flags it for extra scrutiny in evaluation. |
| Sub-agent writes card but factual claims are unverifiable | If the card lacks specific section/page references, ask the sub-agent (via SendMessage) to add them. If the sub-agent is gone, spot-verify 3-5 claims directly and annotate the card with verification notes. |
| Sub-agent accumulation of mediocre cards | If multiple sub-agents return shallow or generic analysis, pause. The reading technique may need adjustment — try adding more specific extraction instructions (e.g., "quote the paper's own research question from Section 1", "extract exact numbers from results tables"). |
| Evaluation agent finds accuracy issues | Fix issues → re-evaluate changed sections. **Do not skip this loop.** Repeat until passing verdict. |
| Evaluation phase skipped or bypassed | This is a Contract violation. Stop, launch evaluation agents for all articles, and do not deliver articles until all evaluations pass. |
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
