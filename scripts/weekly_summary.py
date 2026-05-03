"""Weekly AI news recap generator.

Collects the past 7 days of ai_news briefings, pre-washes the text to
strip noise (URLs, images, HTML), then calls DeepSeek API directly to
produce a structured ~1000-1500 word weekly digest.

Usage:
    python3 scripts/weekly_summary.py
    python3 scripts/weekly_summary.py --force   # overwrite existing
    python3 scripts/weekly_summary.py --days 14 # extend lookback
"""
import argparse
import datetime
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from paths import BRIEFINGS_DIR, PUSHED_DIR
import run_pipelines
from run_pipelines import log

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"


def _load_deepseek_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    log("ERROR: DEEPSEEK_API_KEY not found in .env or environment")
    sys.exit(1)


def call_deepseek(prompt: str, max_tokens: int = 2000) -> str:
    api_key = _load_deepseek_key()
    resp = requests.post(
        DEEPSEEK_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()

DATE = datetime.datetime.now().strftime("%Y-%m-%d")

WEEKLY_PROMPT_TEMPLATE = """\
# Role
你是一位 AI 行业资深研究员，以精炼、客观的分析师口吻写作，全程使用中文。

# Task
基于下方本周原始情报，生成一份 1000-1500 字的精炼周报。

# Selection Criteria
1. **剔除杂讯**：过滤所有"UI小更新"、"日常宣发"、"无实质技术突破"的新闻。
2. **深度聚焦**：从本周动态中，仅挑选 3 个最具"行业范式改变"潜力的模型或技术事件进行详细拆解。

# Content Depth（针对选出的 3 个重点）
- **技术本质**：底层改变了什么？（例如：架构创新、训练效率提升、推理能力突破等）
- **行业涟漪**：谁会感到压力？开发者获得哪些新机会？
- **竞品对比**：与同期竞品相比，核心差异在哪里？（仅限本周情报中提及的内容）

# Output Format
严格按以下结构输出，不要添加额外章节：

## 本周核心综述
（约 200 字，提炼本周 AI 领域整体趋势）

## 三大深度解析

### 1. [事件标题]
（约 300-400 字，含技术本质 / 行业涟漪 / 竞品对比）

### 2. [事件标题]
（约 300-400 字）

### 3. [事件标题]
（约 300-400 字）

## 本周值得关注的项目
（仅列出本周情报中**明确出现**的项目，不得自行补充；每项 1 句推荐理由；最多 5 项）

---

# 本周原始情报
{content}
"""


def prewash(text: str) -> str:
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)   # markdown images
    text = re.sub(r"https?://\S+", "", text)       # URLs
    text = re.sub(r"<[^>]+>", "", text)            # HTML tags
    text = re.sub(r"`{3}.*?`{3}", "", text, flags=re.DOTALL)  # code blocks
    text = re.sub(r"\n{3,}", "\n\n", text)         # excessive blank lines
    return text.strip()


def collect_week_briefings(category: str = "ai_news", days: int = 7) -> list[str]:
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    collected: list[tuple[str, str]] = []  # (date_str, content)

    for base_dir in (BRIEFINGS_DIR, PUSHED_DIR):
        cat_dir = base_dir / category
        if not cat_dir.exists():
            continue
        for fpath in cat_dir.glob("*.md"):
            # extract date from filename pattern *_YYYY-MM-DD*.md
            m = re.search(r"(\d{4}-\d{2}-\d{2})", fpath.name)
            if not m:
                continue
            try:
                file_date = datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
            except ValueError:
                continue
            if file_date < cutoff:
                continue
            text = fpath.read_text(encoding="utf-8")
            collected.append((m.group(1), text))

    collected.sort(key=lambda x: x[0])
    return [content for _, content in collected]


def run_weekly_summary(days: int = 7, force: bool = False) -> int:
    out_dir = BRIEFINGS_DIR / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"weekly_recap_{DATE}.md"

    if out_path.exists() and not force:
        log(f"  weekly recap already exists for {DATE}, skip (use --force to overwrite)")
        return 0

    log(f"  collecting last {days} days of ai_news briefings...")
    briefings = collect_week_briefings("ai_news", days)
    if not briefings:
        log("  no briefings found in the past week, abort")
        return 1

    log(f"  found {len(briefings)} briefing files")
    raw_content = "\n\n---\n\n".join(briefings)
    clean_content = prewash(raw_content)
    log(f"  pre-wash: {len(raw_content)} → {len(clean_content)} chars")

    prompt = WEEKLY_PROMPT_TEMPLATE.replace("{content}", clean_content)
    log(f"  calling DeepSeek API...")
    try:
        result = call_deepseek(prompt, max_tokens=2000)
    except Exception as e:
        log(f"  AI call failed: {e}")
        return 1

    header = f"# AI 行业周报 — {DATE}\n\n"
    out_path.write_text(header + result, encoding="utf-8")
    log(f"  saved → {out_path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate weekly AI news recap")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing recap for today")
    args = parser.parse_args()

    log("=== Weekly Summary ===")
    code = run_weekly_summary(days=args.days, force=args.force)
    sys.exit(code)


if __name__ == "__main__":
    main()
