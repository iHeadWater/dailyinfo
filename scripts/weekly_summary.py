"""Weekly AI news recap generator.

Collects the past 7 days of ai_news briefings, parses them into individual
news items, clusters related items across days into event cards, then calls
DeepSeek API with a structured prompt to produce a ~1500-2500 word weekly
digest.

Usage:
    python3 scripts/weekly_summary.py
    python3 scripts/weekly_summary.py --force   # overwrite existing
    python3 scripts/weekly_summary.py --days 14  # extend lookback
    python3 scripts/weekly_summary.py --threshold 0.35  # tune clustering
"""
import argparse
import datetime
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from paths import BRIEFINGS_DIR, PUSHED_DIR

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"
_BACKOFF_SECONDS = [2, 5, 10]

# ── Data structures ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NewsItem:
    """A single parsed news bullet from a daily briefing."""

    date: str  # YYYY-MM-DD
    section: str  # canonical section name
    text: str  # cleaned bullet text (no markdown prefix)


@dataclass
class EventCard:
    """A cross-day event built from clustered NewsItems."""

    id: int
    title: str
    section: str
    mentions: list[tuple[str, str]]  # [(date, text), ...]
    first_seen: str
    last_seen: str
    day_count: int
    mention_count: int


# ── Logging ──────────────────────────────────────────────────────────────────


def log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── API key loading ──────────────────────────────────────────────────────────


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


# ── AI call ──────────────────────────────────────────────────────────────────


def call_deepseek(prompt: str, max_tokens: int = 4096) -> str:
    """Call DeepSeek API with retries and exponential backoff.

    3 attempts on DeepSeek with backoff (2s / 5s / 10s).
    Raises RuntimeError if all attempts are exhausted.
    """
    api_key = _load_deepseek_key()

    for i in range(3):
        try:
            resp = requests.post(
                DEEPSEEK_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                },
                timeout=120,
            )
            resp.raise_for_status()
            body = resp.json()
            choice = (body.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            finish_reason = choice.get("finish_reason") or "unknown"
            content = (message.get("content") or "").strip()
            if content and finish_reason != "length":
                return content
            log(
                f"  [call_deepseek] attempt {i + 1}/3 incomplete "
                f"(finish_reason={finish_reason}, chars={len(content)})"
            )
        except requests.RequestException as exc:
            log(f"  [call_deepseek] attempt {i + 1}/3 http_error={exc}")

        if i < 2:
            time.sleep(_BACKOFF_SECONDS[i])

    raise RuntimeError(
        f"call_deepseek: empty response after 3 attempts (model={DEEPSEEK_MODEL})"
    )


DATE = datetime.datetime.now().strftime("%Y-%m-%d")

_SECTION_NAMES = ["模型进展", "Agent/产品进展", "AI for Science", "产业新闻"]

# Maps emoji to canonical section name for header detection.
_SECTION_EMOJI_MAP: dict[str, str] = {
    "🧠": "模型进展",
    "🤖": "Agent/产品进展",
    "🔬": "AI for Science",
    "🏭": "产业新闻",
}

_NO_DATA_PATTERN = re.compile(r"暂无.*进展|无.*重要.*进展")


# ── Collection ───────────────────────────────────────────────────────────────


def collect_week_briefings(
    category: str = "ai_news", days: int = 7
) -> list[tuple[str, str]]:
    """Return (date, content) tuples for briefings in the lookback window."""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    collected: list[tuple[str, str]] = []

    for base_dir in (BRIEFINGS_DIR, PUSHED_DIR):
        cat_dir = base_dir / category
        if not cat_dir.exists():
            continue
        for fpath in cat_dir.glob("*.md"):
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

    # Deduplicate: same date can appear in both briefings/ and pushed/.
    # Keep the first version found (briefings/ is scanned first).
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for date, content in collected:
        if date not in seen:
            seen.add(date)
            deduped.append((date, content))

    deduped.sort(key=lambda x: x[0])
    return deduped


# ── Parsing ──────────────────────────────────────────────────────────────────


def _detect_section(line: str) -> str | None:
    """Return canonical section name if line is a section header, else None."""
    stripped = line.strip().lstrip("#").strip()
    # Remove bold markers
    stripped = re.sub(r"\*\*([^*]+)\*\*", r"\1", stripped)

    # Check emoji mapping
    for emoji, name in _SECTION_EMOJI_MAP.items():
        if stripped.startswith(emoji):
            return name

    # Check plain text match
    for name in _SECTION_NAMES:
        if name in stripped:
            return name

    return None


def _is_no_data(content: str) -> bool:
    """Check if a section has no meaningful data."""
    return bool(_NO_DATA_PATTERN.search(content))


def _clean_bullet(text: str) -> str:
    """Strip markdown bullet markers and bold from a bullet line."""
    text = re.sub(r"^\s*[-*]\s+", "", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    return text.strip()


def parse_briefing(date: str, content: str) -> list[NewsItem]:
    """Parse a day's briefing markdown into individual NewsItems.

    Handles these header variants observed in real output:
      - ``## 🧠 模型进展``
      - ``### 🧠 模型进展``
      - ``🧠 **模型进展**``
      - ``### 🤖 Agent/产品进展``
    """
    lines = content.split("\n")
    items: list[NewsItem] = []
    current_section: str | None = None
    current_bullet: str | None = None  # accumulator for multi-line bullets
    section_has_data = True

    def _flush_bullet():
        nonlocal current_bullet
        if current_bullet and section_has_data:
            items.append(
                NewsItem(date=date, section=current_section or "未知", text=current_bullet)
            )
        current_bullet = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            _flush_bullet()
            continue

        # Check for section header
        detected = _detect_section(stripped)
        if detected:
            _flush_bullet()
            current_section = detected
            # Check the rest of the line and the next few lines for no-data
            remaining = stripped[stripped.index(detected) + len(detected) :] if detected in stripped else ""
            # Also check the next non-empty line
            section_has_data = not (
                _is_no_data(stripped) or _is_no_data(remaining)
            )
            continue

        # Check for no-data in current section
        if _is_no_data(stripped) and current_bullet is None:
            section_has_data = False
            continue

        if not section_has_data:
            continue

        # Bullet lines
        if re.match(r"^\s*[-*]\s+", line):
            _flush_bullet()
            current_bullet = _clean_bullet(line)
        elif current_bullet:
            # Continuation line: append to current bullet
            continuation = stripped
            current_bullet += " " + continuation

    _flush_bullet()
    return items


# ── Clustering ───────────────────────────────────────────────────────────────


def _vectorize_items(texts: list[str]):
    """TF-IDF vectorisation with character n-grams for Chinese text.

    For very small datasets (< 5 items) we disable ``max_df`` / ``min_df``
    filtering so that a single-item corpus doesn't trigger a scikit-learn
    ValueError.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer

    n = len(texts)
    vec = TfidfVectorizer(
        analyzer="char",
        ngram_range=(2, 3),
        max_features=5000,
        max_df=0.85 if n >= 5 else 1.0,
        min_df=1 if n >= 5 else 1,
        sublinear_tf=True,
    )
    return vec.fit_transform(texts)


def cluster_items(
    items: list[NewsItem], threshold: float = 0.15
) -> list[list[NewsItem]]:
    """Greedy single-pass clustering by TF-IDF cosine similarity.

    Sorts items chronologically so earlier mentions become cluster seeds.
    Returns clusters sorted by size descending.

    Args:
        items: News items to cluster.
        threshold: Cosine similarity threshold (0.0-1.0).

    Returns:
        List of clusters, each a list of NewsItems.
    """
    if not items:
        return []

    from sklearn.metrics.pairwise import cosine_similarity

    sorted_items = sorted(items, key=lambda x: x.date)
    texts = [item.text for item in sorted_items]
    tfidf = _vectorize_items(texts)
    sim = cosine_similarity(tfidf)

    n = len(sorted_items)
    assigned = [False] * n
    clusters: list[list[NewsItem]] = []

    for i in range(n):
        if assigned[i]:
            continue
        cluster = [sorted_items[i]]
        assigned[i] = True
        for j in range(i + 1, n):
            if not assigned[j] and sim[i, j] >= threshold:
                cluster.append(sorted_items[j])
                assigned[j] = True
        clusters.append(cluster)

    # Sort by size descending — most-mentioned events first
    clusters.sort(key=len, reverse=True)
    return clusters


# ── Event cards ──────────────────────────────────────────────────────────────


def _extract_title(mentions: list[tuple[str, str]]) -> str:
    """Pick the most informative mention as the event title.

    Prefers the first mention >=10 chars. Falls back to first mention.
    """
    sorted_mentions = sorted(mentions, key=lambda x: x[0])
    for _date, text in sorted_mentions:
        cleaned = text.strip().rstrip("。，.,;；")
        if len(cleaned) >= 10:
            return cleaned[:120]
    first = sorted_mentions[0][1].strip().rstrip("。，.,;；")
    return first[:120]


def build_event_cards(clusters: list[list[NewsItem]]) -> list[EventCard]:
    """Build EventCards from clustered NewsItems."""
    cards: list[EventCard] = []
    for idx, cluster in enumerate(clusters):
        mentions = [(it.date, it.text) for it in cluster]
        dates = sorted({it.date for it in cluster})
        sections = Counter(it.section for it in cluster)
        primary_section = sections.most_common(1)[0][0]
        day_count = len(dates)
        mention_count = len(cluster)

        cards.append(
            EventCard(
                id=idx + 1,
                title=_extract_title(mentions),
                section=primary_section,
                mentions=mentions,
                first_seen=dates[0],
                last_seen=dates[-1],
                day_count=day_count,
                mention_count=mention_count,
            )
        )

    # Sort by significance: cross-day events first, then by mention count
    cards.sort(key=lambda c: (c.day_count > 1, c.day_count, c.mention_count), reverse=True)
    for i, card in enumerate(cards):
        card.id = i + 1
    return cards


# ── Prompt building ──────────────────────────────────────────────────────────


def _render_event_cards(cards: list[EventCard]) -> str:
    """Format event cards grouped by section for the AI prompt."""
    section_order = ["模型进展", "Agent/产品进展", "AI for Science", "产业新闻"]

    # Group cards by section
    grouped: dict[str, list[EventCard]] = {s: [] for s in section_order}
    for card in cards:
        grouped.setdefault(card.section, []).append(card)

    lines: list[str] = []
    for section in section_order:
        section_cards = grouped.get(section, [])
        if not section_cards:
            continue
        cross = sum(1 for c in section_cards if c.day_count >= 2)
        single = sum(1 for c in section_cards if c.day_count < 2)
        lines.append(f"## {section}（{len(section_cards)} 事件: {cross} 跨日, {single} 单日）")
        lines.append("")
        for card in section_cards:
            tag = "跨日" if card.day_count >= 2 else "单日"
            lines.append(
                f"[{tag} #{card.id}] {card.title} "
                f"({card.day_count}d, {card.mention_count}m)"
            )
            dates_seen = sorted(set(d for d, _ in card.mentions))
            for date in dates_seen:
                day_texts = [t for d, t in card.mentions if d == date]
                for t in day_texts:
                    lines.append(f"    {date}: {t}")
            lines.append("")
    return "\n".join(lines)


def build_weekly_prompt(cards: list[EventCard]) -> str:
    """Build a structured prompt with editorial intro and four content sections.

    Cross-day events get deeper treatment within their section; single-day
    events are summarized briefly.
    """
    cards_text = _render_event_cards(cards)

    return f"""\
# Role
你是一位 AI 行业资深研究员，以精炼、客观的分析师口吻写作，全程使用中文。

# Task
基于下方按板块分组的事件卡片，生成一份 AI 行业周报。格式与每日简报一致，但内容更深——跨日事件需体现演化轨迹。

# Output Structure（严格按此顺序）

## 导读
约 150 字。用一段精炼的文字提炼本周 AI 领域最核心的 1-2 条主线，让读者 30 秒内判断这周发生了什么大事、值不值得细读。像一个编辑在给读者写导读，而非机器摘要。避免"本周AI领域发生了许多重要事件"这种废话开头。用具体的事件或数字切入。

## 🧠 模型进展
从事件卡片「模型进展」板块中选取本周最重要的 2-4 个事件。跨日事件约 300 字深度分析（含事件演化、技术本质、行业涟漪），单日事件 1-2 句带过。

## 🤖 Agent/产品进展
同上，从「Agent/产品进展」板块选取。

## 🔬 AI for Science
从「AI for Science」板块选取。如果该板块本周无事件卡片，写"本周暂无重要进展。"

## 🏭 产业新闻
同上，从「产业新闻」板块选取。

# 写作要求
- 每个板块内，跨日事件优先、深度优先。单日事件作为补充。
- 跨日事件必须写出从首次出现到最后更新的演化轨迹，而非压成一段。
- 仅使用事件卡片中提供的信息，不得自行补充或推测。
- 不要使用"值得注意的是"、"此外"、"另一个重要进展是"等 AI 套话。
- 涉及模型"蒸馏"争议等内容时，请客观描述而非站队。

# 事件卡片
{cards_text}"""


# ── Orchestrator ─────────────────────────────────────────────────────────────


def run_weekly_summary(
    days: int = 7, force: bool = False, sim_threshold: float = 0.15
) -> int:
    """Generate a weekly AI news recap with event clustering.

    Args:
        days: Lookback window in days.
        force: Overwrite existing recap for today.
        sim_threshold: Cosine similarity threshold for event clustering.

    Returns:
        0 on success, 1 on failure.
    """
    out_dir = BRIEFINGS_DIR / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"weekly_recap_{DATE}.md"

    if out_path.exists() and not force:
        log(f"  weekly recap already exists for {DATE}, skip (use --force to overwrite)")
        return 0

    # 1. Collect
    log(f"  collecting last {days} days of ai_news briefings...")
    dated_briefings = collect_week_briefings("ai_news", days)
    if not dated_briefings:
        log("  no ai_news briefings found in the past week, abort")
        return 1
    log(f"  found {len(dated_briefings)} briefings: "
        f"{dated_briefings[0][0]} ~ {dated_briefings[-1][0]}")

    # 2. Parse
    all_items: list[NewsItem] = []
    for date, content in dated_briefings:
        items = parse_briefing(date, content)
        all_items.extend(items)
    if not all_items:
        log("  no parseable items found, abort")
        return 1
    log(f"  parsed {len(all_items)} individual news items from "
        f"{len(dated_briefings)} briefings")

    # 3. Cluster
    clusters = cluster_items(all_items, threshold=sim_threshold)
    log(f"  clustered into {len(clusters)} event groups "
        f"(threshold={sim_threshold})")

    # 4. Build event cards
    event_cards = build_event_cards(clusters)
    major = sum(1 for c in event_cards if c.day_count >= 2)
    minor = sum(1 for c in event_cards if c.day_count < 2)
    log(f"  event cards: {len(event_cards)} total "
        f"({major} cross-day, {minor} single-day)")

    # Show top events for transparency
    for card in event_cards[:5]:
        cross = "↔" if card.day_count >= 2 else "○"
        log(f"    {cross} [{card.section}] {card.title[:60]}... "
            f"({card.day_count}d, {card.mention_count}m)")

    if not event_cards:
        log("  no event cards generated, abort")
        return 1

    # 5. Build prompt
    prompt = build_weekly_prompt(event_cards)
    old_size_est = sum(len(c) for _, c in dated_briefings)
    log(f"  prompt: {len(prompt)} chars ({len(event_cards)} cards) "
        f"vs old raw dump ~{old_size_est} chars "
        f"({len(prompt) / max(old_size_est, 1) * 100:.0f}% of old size)")

    # 6. Call AI
    log(f"  calling AI (model={DEEPSEEK_MODEL})...")
    try:
        result = call_deepseek(prompt, max_tokens=4096)
    except RuntimeError as exc:
        log(f"  AI call failed: {exc}")
        return 1

    # 7. Save
    meta_comment = (
        f"<!-- weekly-summary v2: days={days} threshold={sim_threshold} "
        f"briefings={len(dated_briefings)} items={len(all_items)} "
        f"events={len(event_cards)} major={major} minor={minor} -->\n"
    )
    header = f"# AI 行业周报 — {DATE}\n\n"
    out_path.write_text(header + meta_comment + result, encoding="utf-8")
    log(f"  saved ({len(result)} chars) -> {out_path}")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate weekly AI news recap with event clustering"
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Lookback window in days (default: 7)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing recap for today",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.15,
        help="Similarity threshold for clustering (0.0-1.0, default: 0.15)",
    )
    args = parser.parse_args()

    log("=== Weekly Summary ===")
    code = run_weekly_summary(
        days=args.days,
        force=args.force,
        sim_threshold=args.threshold,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
