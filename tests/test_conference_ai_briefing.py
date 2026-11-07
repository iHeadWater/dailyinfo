"""The ported AI briefing renderer: prompt, cleanup and queue semantics."""

from pathlib import Path

import pytest

from conference_ai_briefing import (
    _briefing_prompt,
    _clean_conference_briefing,
    _sanitize_conference_markdown,
    render_pending_events,
)
from conference_events import build_snapshot
from conference_store import ConferenceStore

PAPER = {
    "forum_id": "forum-1",
    "note_id": "note-1",
    "title": "A hydrology foundation model",
    "abstract": "We train a model on streamflow.",
    "authors": ["A. Researcher"],
    "keywords": ["hydrology"],
    "venue": "ICLR 2027",
    "venue_id": "ICLR.cc/2027/Conference",
    "status": "under_review",
    "pdf": "https://openreview.net/pdf?id=note-1",
    "landing_url": "https://openreview.net/forum?id=forum-1",
    "cdate": 1_000,
    "mdate": 2_000,
}
REVIEW = {
    "reply_id": "review-1",
    "forum_id": "forum-1",
    "reply_type": "review",
    "rating": "8",
    "confidence": "4",
    "summary": "The paper is solid.",
    "strengths": "novel",
    "weaknesses": "limited baselines",
    "questions": "how does it scale?",
    "reply_mdate": 2_100,
}
SUMMARY = {
    "forum_id": "forum-1",
    "review_count": 1,
    "rating_count": 1,
    "rating_mean": 0.8,
    "rating_median": 0.8,
    "rating_std": 0.0,
    "rating_min": 0.8,
    "rating_max": 0.8,
    "rating_range": 0.0,
    "confidence_mean": 4.0,
    "rating_raw_values": ["8"],
    "confidence_raw_values": ["4"],
    "decision": "",
    "presentation": "",
    "strong_signal": True,
    "controversial": False,
}


def _store(tmp_path, *, reviews=1):
    store = ConferenceStore(tmp_path / "conference.sqlite3")
    store.ensure_venue("iclr", "ICLR.cc/2027/Conference", "inv")
    run = store.start_run(
        source="iclr",
        venue_id="ICLR.cc/2027/Conference",
        invitation="inv",
        mode="incremental",
        config_hash="cfg",
        now_ms=5_000,
    )
    replies = [REVIEW] if reviews else []
    summaries = [SUMMARY] if reviews else []
    store.commit_page(
        run_id=run.run_id,
        papers=[PAPER],
        replies=replies,
        summaries=summaries,
        cursor_after="forum-1",
        candidate_watermark_ms=1_000,
        total_expected=1,
        raw_count=1,
        now_ms=5_000,
    )
    store.finish_run(run.run_id, now_ms=5_000)
    return store


def _config(**overrides):
    value = {
        "ai_briefing": {
            "enabled": True,
            "max_events_per_briefing": 10,
            "max_batches_per_run": 1,
            "max_tokens": 16_000,
        }
    }
    value["ai_briefing"].update(overrides)
    return value


def _fake_ai(calls):
    def fake(prompt, model="stub", max_tokens=0):
        calls.append((prompt, model, max_tokens))
        return "### A hydrology foundation model\n\n这是一段中文摘要。\n\n#### Reviewer 1\n\n- rating: 8"

    return fake


def test_briefing_prompt_requests_raw_ratings_and_review_sections():
    snapshot = build_snapshot(PAPER, [REVIEW], SUMMARY)
    event = {
        "event_types_json": ["REVIEWS_CHANGED"],
        "after_json": snapshot,
        "before_json": None,
    }

    prompt = _briefing_prompt("iclr", "ICLR 2027", [event])

    assert "raw_review_ratings" in prompt
    assert "原始值" in prompt
    assert "Link To Code" in prompt
    assert "Reviewer 1" in prompt
    assert "Rebuttal / Author Response" in prompt
    assert "不要输出事件类型" in prompt
    assert "不要输出“为什么值得关注”" in prompt
    assert "不可信数据" in prompt
    assert "relevance" not in prompt


def test_clean_conference_briefing_removes_internal_boilerplate():
    content = (
        "以下 10 篇论文均由 PAPER_DISCOVERED 事件发现。\n\n"
        "### Example\n\n"
        "研究总结。\n\n"
        "文中所有状态、decision、评审统计均来自输入元数据。"
    )

    assert _clean_conference_briefing(content) == "### Example\n\n研究总结。"


def test_sanitizer_removes_html_images_and_dangerous_links():
    content = (
        "<script>alert(1)</script>\n\n"
        "### Paper\n\n"
        "![tracking pixel](https://attacker.example/pixel.png)\n\n"
        "[bad](javascript:alert(1))\n\n"
        "@everyone <@123456789>"
    )

    cleaned = _sanitize_conference_markdown(content)

    assert "<script" not in cleaned.lower()
    assert "![tracking pixel]" not in cleaned
    assert "javascript:" not in cleaned.lower()
    assert "@everyone" not in cleaned
    assert "<@123456789>" not in cleaned
    assert "### Paper" in cleaned


def test_render_sanitizes_the_model_output(tmp_path):
    store = _store(tmp_path)
    store.seed_review_events("iclr", now_ms=6_000)

    def unsafe_ai(prompt, model="stub", max_tokens=0):
        return (
            "<script>alert(1)</script>\n\n"
            "### Paper\n\n"
            "![tracking pixel](https://attacker.example/pixel.png)"
        )

    paths = render_pending_events(
        store=store,
        source="iclr",
        display_name="ICLR 2027",
        config=_config(),
        defaults={"model": "test-model"},
        call_ai=unsafe_ai,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=7_000,
    )

    text = Path(paths[0]).read_text(encoding="utf-8")
    assert "<script" not in text.lower()
    assert "![tracking pixel]" not in text
    assert "### Paper" in text
    store.close()


def test_render_rejects_unsafe_source_or_date(tmp_path):
    store = _store(tmp_path)
    kwargs = dict(
        store=store,
        display_name="ICLR 2027",
        config=_config(),
        defaults={"model": "test-model"},
        call_ai=_fake_ai([]),
        briefings_dir=tmp_path / "briefings",
        now_ms=7_000,
    )

    with pytest.raises(ValueError, match="unsafe conference source"):
        render_pending_events(source="../evil", date="2026-11-07", **kwargs)
    with pytest.raises(ValueError, match="unsafe briefing date"):
        render_pending_events(source="iclr", date="../../x", **kwargs)
    store.close()


def test_render_pending_events_calls_ai_and_marks_rendered(tmp_path):
    store = _store(tmp_path)
    store.seed_review_events("iclr", now_ms=6_000)
    calls = []

    paths = render_pending_events(
        store=store,
        source="iclr",
        display_name="ICLR 2027",
        config=_config(),
        defaults={"model": "test-model"},
        call_ai=_fake_ai(calls),
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=7_000,
    )

    assert len(paths) == 1
    assert Path(paths[0]).exists()
    text = Path(paths[0]).read_text(encoding="utf-8")
    assert "ICLR 2027" in text
    assert "这是一段中文摘要" in text
    assert calls and calls[0][1] == "test-model"
    assert calls[0][2] == 16_000
    assert "The paper is solid." in calls[0][0]
    assert store.pending_events("iclr", 10) == []
    store.close()


def test_existing_file_skips_the_model_call(tmp_path):
    store = _store(tmp_path)
    store.seed_review_events("iclr", now_ms=6_000)
    calls = []
    kwargs = dict(
        store=store,
        source="iclr",
        display_name="ICLR 2027",
        config=_config(),
        defaults={"model": "test-model"},
        call_ai=_fake_ai(calls),
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=7_000,
    )
    paths = render_pending_events(**kwargs)

    # Put the event back into pending as an interrupted render would, and
    # retry on a different calendar day: the filename must not depend on the
    # date or the retry would miss the existing file and call the model again.
    store.conn.execute("UPDATE events SET status = 'pending'")
    store.conn.commit()
    second = render_pending_events(**{**kwargs, "date": "2026-11-08"})

    assert second == paths
    assert len(calls) == 1
    assert store.pending_events("iclr", 10) == []
    store.close()


def test_single_event_failure_is_quarantined_after_max_attempts(tmp_path):
    store = _store(tmp_path)
    store.seed_review_events("iclr", now_ms=6_000)
    calls = []

    def boom(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("model timeout")

    kwargs = dict(
        store=store,
        source="iclr",
        display_name="ICLR 2027",
        config=_config(max_attempts=2),
        defaults={"model": "test-model"},
        call_ai=boom,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=7_000,
    )

    assert render_pending_events(**kwargs) == []
    assert len(store.pending_events("iclr", 10)) == 1
    assert render_pending_events(**kwargs) == []

    assert store.pending_events("iclr", 10) == []
    row = store.conn.execute(
        "SELECT status, attempts, last_error FROM events"
    ).fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 2
    assert "RuntimeError" in row["last_error"]
    assert len(calls) == 2
    store.close()


def test_batch_failure_splits_into_smaller_batches(tmp_path):
    store = _store(tmp_path)
    paper = dict(PAPER, forum_id="forum-2", note_id="note-2")
    review = dict(REVIEW, reply_id="review-2", forum_id="forum-2")
    summary = dict(SUMMARY, forum_id="forum-2")
    store.commit_page(
        run_id=store.last_run("iclr").run_id,
        papers=[paper],
        replies=[review],
        summaries=[summary],
        cursor_after="forum-2",
        candidate_watermark_ms=1_000,
        total_expected=2,
        raw_count=1,
        now_ms=6_000,
    )
    store.seed_review_events("iclr", now_ms=7_000)
    calls = []

    def fails_on_pair(prompt, model="stub", max_tokens=0):
        calls.append(prompt)
        if '"forum_id": "forum-1"' in prompt and '"forum_id": "forum-2"' in prompt:
            raise RuntimeError("context too long")
        return "### One paper\n\n摘要。"

    paths = render_pending_events(
        store=store,
        source="iclr",
        display_name="ICLR 2027",
        config=_config(max_events_per_briefing=10, max_batches_per_run=1),
        defaults={"model": "test-model"},
        call_ai=fails_on_pair,
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=8_000,
    )

    assert len(paths) == 2
    assert len(calls) == 3  # one failed pair, then two single-paper batches
    assert store.pending_events("iclr", 10) == []
    store.close()


def test_batch_limit_leaves_the_rest_pending(tmp_path):
    store = _store(tmp_path)
    # Three papers, each with one review.
    for index in (2, 3):
        paper = dict(PAPER, forum_id=f"forum-{index}", note_id=f"note-{index}")
        review = dict(REVIEW, reply_id=f"review-{index}", forum_id=f"forum-{index}")
        summary = dict(SUMMARY, forum_id=f"forum-{index}")
        store.commit_page(
            run_id=store.last_run("iclr").run_id,
            papers=[paper],
            replies=[review],
            summaries=[summary],
            cursor_after=f"forum-{index}",
            candidate_watermark_ms=1_000,
            total_expected=3,
            raw_count=1,
            now_ms=6_000,
        )
    store.seed_review_events("iclr", now_ms=7_000)
    calls = []

    first = render_pending_events(
        store=store,
        source="iclr",
        display_name="ICLR 2027",
        config=_config(max_events_per_briefing=2, max_batches_per_run=1),
        defaults={"model": "test-model"},
        call_ai=_fake_ai(calls),
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=8_000,
    )

    assert len(first) == 1
    assert len(calls) == 1
    assert len(store.pending_events("iclr", 10)) == 1
    store.close()


def test_max_batches_per_run_renders_multiple_batches(tmp_path):
    store = _store(tmp_path)
    for index in (2, 3):
        paper = dict(PAPER, forum_id=f"forum-{index}", note_id=f"note-{index}")
        review = dict(REVIEW, reply_id=f"review-{index}", forum_id=f"forum-{index}")
        summary = dict(SUMMARY, forum_id=f"forum-{index}")
        store.commit_page(
            run_id=store.last_run("iclr").run_id,
            papers=[paper],
            replies=[review],
            summaries=[summary],
            cursor_after=f"forum-{index}",
            candidate_watermark_ms=1_000,
            total_expected=3,
            raw_count=1,
            now_ms=6_000,
        )
    store.seed_review_events("iclr", now_ms=7_000)
    calls = []

    paths = render_pending_events(
        store=store,
        source="iclr",
        display_name="ICLR 2027",
        config=_config(max_events_per_briefing=1, max_batches_per_run=2),
        defaults={"model": "test-model"},
        call_ai=_fake_ai(calls),
        briefings_dir=tmp_path / "briefings",
        date="2026-11-07",
        now_ms=8_000,
    )

    assert len(paths) == 2
    assert len(calls) == 2
    assert len(store.pending_events("iclr", 10)) == 1
    store.close()
