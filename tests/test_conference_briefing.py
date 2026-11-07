from conference_briefing import (
    baseline_path,
    briefing_path,
    render_baseline,
    render_briefing,
    write_briefing,
)


def _paper(index=1, **overrides):
    paper = {
        "forum_id": f"forum-{index}",
        "title": f"Paper {index}",
        "authors": ["Alice", "Bob"],
        "keywords": ["hydrology", "PINN"],
        "status": "under_review",
        "cdate": 1_700_000_000_000,
        "landing_url": f"https://openreview.net/forum?id=forum-{index}",
        "pdf": f"https://openreview.net/pdf/{index}.pdf",
    }
    paper.update(overrides)
    return paper


def test_render_briefing_is_deterministic():
    papers = [_paper(2), _paper(1)]
    first = render_briefing(
        display_name="ICLR 2027", source="iclr", date="2026-11-07", papers=papers
    )
    second = render_briefing(
        display_name="ICLR 2027", source="iclr", date="2026-11-07", papers=papers
    )

    assert first == second
    assert "今日新增投稿 2 篇" in first
    assert "1. **Paper 2**" in first
    assert "2. **Paper 1**" in first


def test_render_briefing_lists_metadata_without_ai():
    text = render_briefing(
        display_name="ICLR 2027",
        source="iclr",
        date="2026-11-07",
        papers=[_paper()],
    )

    assert "作者: Alice, Bob" in text
    assert "关键词: hydrology, PINN" in text
    assert "状态: 审稿中" in text
    assert "提交: 2023-11-15" in text
    assert "https://openreview.net/forum?id=forum-1" in text
    assert "https://openreview.net/pdf/1.pdf" in text


def test_render_briefing_flattens_control_characters_in_titles():
    text = render_briefing(
        display_name="ICLR 2027",
        source="iclr",
        date="2026-11-07",
        papers=[_paper(title="Line one\nLine two\twith tab")],
    )

    assert "Line one Line two with tab" in text
    assert "\t" not in text


def test_render_briefing_handles_empty_input():
    text = render_briefing(
        display_name="ICLR 2027",
        source="iclr",
        date="2026-11-07",
        papers=[],
    )

    assert "今日无新增投稿" in text


def test_render_baseline_reports_counts_only():
    text = render_baseline(
        display_name="ICLR 2027",
        source="iclr",
        date="2026-11-07",
        status_counts={"under_review": 10, "accepted": 2},
    )

    assert "首次同步已入库 12 篇投稿" in text
    assert "审稿中 (under_review): 10" in text
    assert "已录用 (accepted): 2" in text
    assert "Paper" not in text


def test_write_briefing_replaces_atomically(tmp_path):
    path = briefing_path(tmp_path, "iclr", "2026-11-07")
    write_briefing(path, "first")
    write_briefing(path, "second")

    assert path.read_text(encoding="utf-8") == "second"
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_baseline_path_uses_distinct_name(tmp_path):
    path = baseline_path(tmp_path, "iclr", "2026-11-07")
    assert path.name == "iclr_baseline_2026-11-07.md"
    assert path.parent == tmp_path / "conference"
