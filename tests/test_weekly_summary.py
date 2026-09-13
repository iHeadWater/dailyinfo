"""Tests for weekly_summary.py — parsing, clustering, card building, end-to-end."""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

# Ensure scripts/ is on path so flat imports work
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from weekly_summary import (  # noqa: E402
    NewsItem,
    EventCard,
    parse_briefing,
    cluster_items,
    build_event_cards,
    build_weekly_prompt,
    collect_week_briefings,
    run_weekly_summary,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


# ── parse_briefing ───────────────────────────────────────────────────────


class TestParseBriefing:
    def test_parses_all_four_sections(self):
        content = read_fixture("ai_news_2026-06-26.md")
        items = parse_briefing("2026-06-26", content)

        sections = {it.section for it in items}
        assert "模型进展" in sections
        assert "Agent/产品进展" in sections
        assert "产业新闻" in sections
        # "AI for Science" section had "暂无重要进展" — should produce no items
        assert "AI for Science" not in sections

    def test_all_items_have_correct_date(self):
        content = read_fixture("ai_news_2026-06-26.md")
        items = parse_briefing("2026-06-26", content)

        assert len(items) > 0
        for item in items:
            assert item.date == "2026-06-26"

    def test_items_have_non_empty_text(self):
        content = read_fixture("ai_news_2026-06-26.md")
        items = parse_briefing("2026-06-26", content)

        for item in items:
            assert len(item.text) > 5, f"Item text too short: {item.text!r}"

    def test_h3_section_headers(self):
        """h3 headers work (observed in 06-27 briefing)."""
        content = read_fixture("ai_news_2026-06-27.md")
        items = parse_briefing("2026-06-27", content)

        sections = {it.section for it in items}
        assert "模型进展" in sections
        assert "Agent/产品进展" in sections
        assert "产业新闻" in sections

    def test_no_data_section_produces_zero_items(self):
        """Sections with 暂无重要进展 should yield no NewsItems."""
        content = (
            "# AI Daily\n\n"
            "🧠 **模型进展**\n"
            "- GPT-5 released\n\n"
            "🔬 **AI for Science**\n"
            "暂无重要进展。\n\n"
            "🤖 **Agent/产品进展**\n"
            "- Claude Tag launched\n"
        )
        items = parse_briefing("2026-06-25", content)

        sections = {it.section for it in items}
        assert "模型进展" in sections
        assert "Agent/产品进展" in sections
        assert "AI for Science" not in sections, "no-data section should be skipped"

    def test_empty_content_returns_empty_list(self):
        items = parse_briefing("2026-06-25", "")
        assert items == []

    def test_multi_line_bullet(self):
        """Continuation lines should be appended to the previous bullet."""
        content = (
            "## 🧠 模型进展\n"
            "- First line of a bullet\n"
            "  continuation of the same bullet\n"
            "- Second bullet\n"
        )
        items = parse_briefing("2026-06-25", content)
        assert len(items) == 2
        assert "continuation" in items[0].text

    def test_markdown_bold_stripped(self):
        content = (
            "## 🧠 模型进展\n"
            "- **GLM-5.2** reaches top benchmark scores\n"
        )
        items = parse_briefing("2026-06-25", content)
        assert len(items) == 1
        assert "**" not in items[0].text
        assert "GLM-5.2" in items[0].text


# ── cluster_items ────────────────────────────────────────────────────────


class TestClusterItems:
    def test_similar_items_cluster_together(self):
        """Two items about the same model on different days should cluster."""
        items = [
            NewsItem("2026-06-24", "模型进展",
                     "智谱GLM-5.2成为首个开源Agent前沿模型 在多项Agent基准测试中表现接近甚至超越顶尖闭源模型"),
            NewsItem("2026-06-26", "模型进展",
                     "Zhipu AI的GLM-5.2在多个人工分析Agent及代码竞技场中登顶 并在ARC-AGI-2上获开源模型最高分"),
            NewsItem("2026-06-27", "模型进展",
                     "GLM-5.2跻身顶级编程基准前列 在Code Arena前端编程测试中达到1595分超越了Opus 4.8"),
        ]
        # Chinese short texts have naturally low char-ngram cosine similarity.
        # Use a low threshold to catch the shared entity "GLM-5.2".
        clusters = cluster_items(items, threshold=0.08)

        # All three should be in the same cluster
        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_dissimilar_items_stay_separate(self):
        """Unrelated topics should remain in separate clusters."""
        items = [
            NewsItem("2026-06-26", "模型进展",
                     "OpenAI发布首款自研AI推理芯片Jalapeño 与Broadcom合作"),
            NewsItem("2026-06-26", "产业新闻",
                     "Hugging Face年经常性收入突破1亿美元里程碑"),
            NewsItem("2026-06-26", "Agent/产品进展",
                     "Anthropic将Claude深度集成到Slack"),
        ]
        clusters = cluster_items(items, threshold=0.30)

        # Each should be in its own cluster
        assert len(clusters) >= 2  # at minimum, chip and HF revenue shouldn't merge

    def test_empty_items_returns_empty(self):
        assert cluster_items([]) == []

    def test_single_item_returns_single_cluster(self):
        items = [NewsItem("2026-06-26", "模型进展", "OpenAI GPT-5 released")]
        clusters = cluster_items(items)
        assert len(clusters) == 1
        assert len(clusters[0]) == 1


# ── build_event_cards ────────────────────────────────────────────────────


class TestBuildEventCards:
    def test_single_day_event(self):
        clusters = [
            [NewsItem("2026-06-26", "模型进展", "OpenAI GPT-5.6 preview released")],
        ]
        cards = build_event_cards(clusters)

        assert len(cards) == 1
        assert cards[0].day_count == 1
        assert cards[0].mention_count == 1
        assert cards[0].first_seen == "2026-06-26"
        assert cards[0].last_seen == "2026-06-26"

    def test_cross_day_event(self):
        clusters = [
            [
                NewsItem("2026-06-24", "模型进展", "GLM-5.2 first appeared"),
                NewsItem("2026-06-26", "模型进展", "GLM-5.2 tops benchmarks"),
                NewsItem("2026-06-27", "Agent/产品进展", "GLM-5.2 code arena win"),
            ],
        ]
        cards = build_event_cards(clusters)

        assert len(cards) == 1
        assert cards[0].day_count == 3
        assert cards[0].mention_count == 3
        assert cards[0].first_seen == "2026-06-24"
        assert cards[0].last_seen == "2026-06-27"

    def test_cross_day_events_sorted_first(self):
        """Cross-day events should sort before single-day events."""
        clusters = [
            [NewsItem("2026-06-26", "产业新闻", "Single day news")],
            [
                NewsItem("2026-06-24", "模型进展", "Multi day event day1"),
                NewsItem("2026-06-26", "模型进展", "Multi day event day3"),
            ],
        ]
        cards = build_event_cards(clusters)

        # Cross-day event should be first
        assert cards[0].day_count >= 2
        assert cards[0].id == 1

    def test_title_truncation(self):
        """Very long titles should be truncated to 120 chars."""
        long_text = "这是一个非常长的新闻标题" * 20
        clusters = [[NewsItem("2026-06-26", "模型进展", long_text)]]
        cards = build_event_cards(clusters)

        assert len(cards[0].title) <= 120


# ── build_weekly_prompt ──────────────────────────────────────────────────


class TestBuildWeeklyPrompt:
    def test_prompt_includes_four_sections(self):
        cards = [
            EventCard(
                id=1, title="Cross-day event",
                section="模型进展",
                mentions=[("2026-06-24", "text1"), ("2026-06-26", "text2")],
                first_seen="2026-06-24", last_seen="2026-06-26",
                day_count=2, mention_count=2,
            ),
            EventCard(
                id=2, title="Single day event",
                section="产业新闻",
                mentions=[("2026-06-26", "text3")],
                first_seen="2026-06-26", last_seen="2026-06-26",
                day_count=1, mention_count=1,
            ),
        ]
        prompt = build_weekly_prompt(cards)

        # Four-section structure + intro + outro
        assert "导读" in prompt
        assert "🧠 模型进展" in prompt
        assert "🤖 Agent/产品进展" in prompt
        assert "🔬 AI for Science" in prompt
        assert "🏭 产业新闻" in prompt
        assert "事件演化" in prompt

    def test_prompt_includes_event_details(self):
        cards = [
            EventCard(
                id=1, title="GLM-5.2 benchmarks",
                section="模型进展",
                mentions=[
                    ("2026-06-24", "GLM-5.2 first appeared"),
                    ("2026-06-26", "GLM-5.2 tops benchmarks"),
                ],
                first_seen="2026-06-24", last_seen="2026-06-26",
                day_count=2, mention_count=2,
            ),
        ]
        prompt = build_weekly_prompt(cards)

        assert "GLM-5.2" in prompt
        assert "2026-06-24" in prompt
        assert "2026-06-26" in prompt
        assert "跨日事件" in prompt


# ── End-to-end ───────────────────────────────────────────────────────────


class TestEndToEnd:
    def test_run_with_fake_ai(self, tmp_path, monkeypatch):
        """Full pipeline with a stubbed AI call writes expected output file."""
        # Set up isolated data dir
        data_root = tmp_path / "data"
        pushed_dir = data_root / "pushed" / "ai_news"
        pushed_dir.mkdir(parents=True)
        briefings_dir = data_root / "briefings"
        weekly_dir = briefings_dir / "weekly"
        weekly_dir.mkdir(parents=True)

        # Write a sample briefing to pushed/ (within the 7-day lookback window)
        recent = (datetime.date.today() - datetime.timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        fixture_content = read_fixture("ai_news_2026-06-27.md")
        (pushed_dir / f"smolai_news_briefing_{recent}.md").write_text(
            fixture_content, encoding="utf-8"
        )

        # Patch paths
        monkeypatch.setattr(
            "weekly_summary.BRIEFINGS_DIR", briefings_dir
        )
        monkeypatch.setattr(
            "weekly_summary.PUSHED_DIR", data_root / "pushed"
        )

        # Stub AI call
        fake_result = "# AI 行业周报\n\n## 导读\n本周核心主线：GLM-5.2发布。\n"

        def _fake_call(prompt, max_tokens=4096):
            return fake_result

        monkeypatch.setattr("weekly_summary.call_deepseek", _fake_call)

        # Run
        code = run_weekly_summary(days=7, force=True, sim_threshold=0.15)
        assert code == 0

        # Verify output
        out_files = list(weekly_dir.glob("weekly_recap_*.md"))
        assert len(out_files) == 1
        content = out_files[0].read_text(encoding="utf-8")
        assert fake_result in content
        assert "<!-- weekly-summary v2:" in content


# ── collect_week_briefings ───────────────────────────────────────────────


class TestCollectWeekBriefings:
    def test_dedup_same_date(self, tmp_path, monkeypatch):
        """If same date appears in both briefings/ and pushed/, keep first."""
        data_root = tmp_path / "data"
        briefings_ai = data_root / "briefings" / "ai_news"
        pushed_ai = data_root / "pushed" / "ai_news"
        briefings_ai.mkdir(parents=True)
        pushed_ai.mkdir(parents=True)

        # Write same date to both dirs (within the 7-day lookback window)
        recent = (datetime.date.today() - datetime.timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        content_b = "briefings version"
        content_p = "pushed version"
        (briefings_ai / f"news_briefing_{recent}.md").write_text(
            content_b, encoding="utf-8"
        )
        (pushed_ai / f"news_briefing_{recent}.md").write_text(
            content_p, encoding="utf-8"
        )

        monkeypatch.setattr("weekly_summary.BRIEFINGS_DIR", data_root / "briefings")
        monkeypatch.setattr("weekly_summary.PUSHED_DIR", data_root / "pushed")

        result = collect_week_briefings("ai_news", days=7)
        assert len(result) == 1
        # Should keep briefings/ version (scanned first)
        assert result[0][1] == content_b
