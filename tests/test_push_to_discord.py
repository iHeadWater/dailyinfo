"""Tests for ``scripts/push_to_discord.py``."""

from __future__ import annotations

from datetime import datetime
import json

import pytest


def test_split_message_short_passthrough():
    import push_to_discord as pd

    assert pd.split_message("hello") == ["hello"]


def test_split_message_splits_on_newlines_under_limit():
    import push_to_discord as pd

    # 10 lines of 500 chars each → chunks stay under the 1950-char limit.
    lines = ["x" * 500 for _ in range(10)]
    content = "\n".join(lines)
    out = pd.split_message(content, max_length=1950)

    assert len(out) >= 3
    assert all(len(chunk) <= 1950 for chunk in out)
    assert "\n".join(out).replace("\n", "") == content.replace("\n", "")


def test_split_discord_messages_reserves_prefix_budget():
    import push_to_discord as pd

    content = "\n".join(["x" * 1900 for _ in range(3)])
    chunks = pd.split_discord_messages(content)

    assert len(chunks) > 1
    for i, chunk in enumerate(chunks, start=1):
        prefixed = f"{pd._chunk_prefix(i, len(chunks))}{chunk}"
        assert len(prefixed) <= pd.DISCORD_CONTENT_LIMIT


def test_split_message_never_breaks_mid_line():
    import push_to_discord as pd

    content = "\n".join([f"line-{i}" for i in range(200)])
    for chunk in pd.split_message(content, max_length=200):
        assert not chunk.startswith(" ")
        for line in chunk.split("\n"):
            assert line.startswith("line-")


def test_is_placeholder_true_for_short_empty_notice():
    import push_to_discord as pd

    content = "# Example - 2024-01-01\n\n📭 过去 24 小时无新内容\n"
    assert pd.is_placeholder(content) is True


def test_is_placeholder_false_for_real_content():
    import push_to_discord as pd

    real = "# Example\n\n" + "这是一段真实的中文内容。" * 20
    assert pd.is_placeholder(real) is False


def test_is_low_quality_content_detection():
    import push_to_discord as pd

    assert pd.is_low_quality_content("short english only") is True
    # Chinese characters present → not flagged as low quality
    assert pd.is_low_quality_content("# 标题\n正文内容很丰富。") is False
    # Long enough English → not flagged
    assert pd.is_low_quality_content("x" * 300) is False


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _seed_briefing(briefings_dir, category, filename, content):
    cat_dir = briefings_dir / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / filename).write_text(content, encoding="utf-8")


def test_push_category_moves_file_to_pushed(monkeypatch):
    import push_to_discord as pd
    from paths import BRIEFINGS_DIR, PUSHED_DIR

    sent = []
    monkeypatch.setattr(
        pd,
        "send_to_discord",
        lambda channel, content: (sent.append((channel, content)) or True),
    )

    filename = f"mysrc_briefing_{_today()}.md"
    body = "# Real briefing\n\n" + "这是真正的简报内容。" * 30
    _seed_briefing(BRIEFINGS_DIR, "papers", filename, body)

    pushed = pd.push_category("papers", "channel-xyz")

    assert pushed == 1
    assert sent and sent[0][0] == "channel-xyz"
    assert not (BRIEFINGS_DIR / "papers" / filename).exists()
    assert (PUSHED_DIR / "papers" / filename).exists()


def test_push_category_deletes_placeholder_without_push(monkeypatch):
    import push_to_discord as pd
    from paths import BRIEFINGS_DIR, PUSHED_DIR

    sent = []
    monkeypatch.setattr(
        pd,
        "send_to_discord",
        lambda channel, content: (sent.append((channel, content)) or True),
    )

    filename = f"empty_briefing_{_today()}.md"
    _seed_briefing(
        BRIEFINGS_DIR,
        "papers",
        filename,
        "# Empty\n\n📭 过去 24 小时无新内容\n",
    )

    pushed = pd.push_category("papers", "channel-xyz")

    assert pushed == 0
    assert not (BRIEFINGS_DIR / "papers" / filename).exists()
    assert not (PUSHED_DIR / "papers" / filename).exists()
    # One notice should be sent (all files filtered out)
    assert len(sent) == 1
    assert "论文频道推送总结" in sent[0][1]
    assert "今日无文章更新" in sent[0][1]


def test_push_category_sends_notice_when_no_files(monkeypatch):
    import push_to_discord as pd
    from paths import BRIEFINGS_DIR

    (BRIEFINGS_DIR / "papers").mkdir(parents=True, exist_ok=True)

    sent = []
    monkeypatch.setattr(
        pd,
        "send_to_discord",
        lambda channel, content: (sent.append((channel, content)) or True),
    )

    pushed = pd.push_category("papers", "channel-xyz")
    assert pushed == 0
    assert len(sent) == 1
    assert "暂无新简报" in sent[0][1]


def test_build_push_summary_lists_pushed_and_no_update(monkeypatch, tmp_path):
    import push_to_discord as pd

    sources = tmp_path / "sources.json"
    sources.write_text(
        """
{
  "sources": [
    {"name": "nature", "display_name": "Nature", "category": "papers", "enabled": true},
    {"name": "wrr", "display_name": "Water Resources Research (WRR)", "category": "papers", "enabled": true},
    {"name": "code", "display_name": "Code", "category": "code", "enabled": true}
  ]
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(pd, "SOURCES_JSON", str(sources))

    summary = pd.build_push_summary(
        "papers", "2026-04-25", pushed_names=["nature"], placeholder_names=["wrr"]
    )

    assert "Nature (`nature`)" in summary
    assert "Water Resources Research (WRR) (`wrr`)" in summary
    assert "Code" not in summary


def test_push_category_returns_zero_when_directory_missing(monkeypatch):
    import push_to_discord as pd

    monkeypatch.setattr(pd, "send_to_discord", lambda *a, **k: True)
    # category dir not created → function should bail out cleanly
    assert pd.push_category("nope", "chan") == 0


def test_push_category_with_explicit_date_targets_that_day(monkeypatch):
    """``push_category`` honours an explicit ``date`` and ignores today's files."""
    import push_to_discord as pd
    from paths import BRIEFINGS_DIR, PUSHED_DIR

    sent = []
    monkeypatch.setattr(
        pd,
        "send_to_discord",
        lambda channel, content: (sent.append((channel, content)) or True),
    )

    backfill_date = "2024-01-02"
    today_name = f"src_briefing_{_today()}.md"
    backfill_name = f"src_briefing_{backfill_date}.md"
    body = "# Backfill\n\n" + "历史简报正文。" * 30

    _seed_briefing(BRIEFINGS_DIR, "papers", today_name, "today content placeholder")
    _seed_briefing(BRIEFINGS_DIR, "papers", backfill_name, body)

    pushed = pd.push_category("papers", "chan", date=backfill_date)

    assert pushed == 1
    assert not (BRIEFINGS_DIR / "papers" / backfill_name).exists()
    assert (PUSHED_DIR / "papers" / backfill_name).exists()
    # Today's file must stay put because the caller asked for 2024-01-02 only.
    assert (BRIEFINGS_DIR / "papers" / today_name).exists()


def test_parse_date_rejects_invalid_format():
    import push_to_discord as pd
    import pytest

    assert pd._parse_date("2024-01-02") == "2024-01-02"
    with pytest.raises(ValueError):
        pd._parse_date("not-a-date")


def test_send_to_discord_uses_requests_post(monkeypatch):
    import push_to_discord as pd

    captured = []

    class _Resp:
        status_code = 200
        text = ""

    def fake_post(url, headers, json, timeout):
        captured.append((url, headers, json))
        return _Resp()

    monkeypatch.setattr(pd.requests, "post", fake_post)
    monkeypatch.setattr(pd.time, "sleep", lambda *_: None)

    assert pd.send_to_discord("chan-123", "hello") is True
    assert captured
    assert "chan-123" in captured[0][0]
    assert captured[0][2]["content"] == "hello"


def test_send_to_discord_suppresses_all_mentions(monkeypatch):
    """Briefing text comes from external feeds, so it must never ping anyone.

    Discord parses user/role/everyone mentions by default when allowed_mentions
    is omitted, which would let feed content trigger a real notification in the
    channel.
    """
    import push_to_discord as pd

    captured = []

    class _Resp:
        status_code = 200
        text = ""

    def fake_post(url, headers, json, timeout):
        captured.append(json)
        return _Resp()

    monkeypatch.setattr(pd.requests, "post", fake_post)
    monkeypatch.setattr(pd.time, "sleep", lambda *_: None)

    assert pd.send_to_discord("chan-123", "@everyone look at this") is True
    assert captured
    assert captured[0]["allowed_mentions"] == {"parse": []}


def test_send_error_log_redacts_the_bot_token(monkeypatch):
    """requests' InvalidHeader embeds the header value, i.e. the token."""
    import push_to_discord as pd

    logs: list[str] = []
    monkeypatch.setattr(pd, "DISCORD_BOT_TOKEN", "sk-bot-secret")
    monkeypatch.setattr(pd, "log", lambda msg: logs.append(msg))
    monkeypatch.setattr(pd.time, "sleep", lambda *_: None)

    def boom(*args, **kwargs):
        raise pd.requests.exceptions.InvalidHeader("header value: 'Bot sk-bot-secret'")

    monkeypatch.setattr(pd.requests, "post", boom)

    assert pd.send_to_discord("chan-1", "hello") is False

    joined = "\n".join(logs)
    # Assert something was logged, so deleting the log line cannot satisfy the
    # absence check below by logging nothing.
    assert any("发送错误" in m or "发送失败" in m for m in logs), logs
    assert "header value" in joined, joined  # the detail survived redaction
    assert "sk-bot-secret" not in joined, joined


def test_send_failure_redacts_and_bounds_the_response_body(monkeypatch):
    """Empty end-to-end evidence in the audit: a non-200 body is
    provider-controlled and is the last unredacted string in this module."""
    import push_to_discord as pd

    logs: list[str] = []
    monkeypatch.setattr(pd, "DISCORD_BOT_TOKEN", "sk-bot-secret")
    monkeypatch.setattr(pd, "log", lambda msg: logs.append(msg))
    monkeypatch.setattr(pd.time, "sleep", lambda *_: None)

    class _Resp:
        status_code = 400
        text = "token=sk-bot-secret\n[WARN] forged " + "x" * 300

    monkeypatch.setattr(pd.requests, "post", lambda *a, **k: _Resp())

    assert pd.send_to_discord("chan-1", "hello") is False

    joined = "\n".join(logs)
    assert "400" in joined, logs
    assert "sk-bot-secret" not in joined, joined
    assert "x" * 250 not in joined, joined  # body excerpt is bounded
    assert all(len(m.splitlines()) == 1 for m in logs), logs


# ---------------------------------------------------------------------------
# Canonical delivery: the per-source push summary (papers)
# ---------------------------------------------------------------------------

_PAPERS_SOURCES = (
    ("nature", "Nature"),
    ("aies", "AIES"),
    ("wrr", "Water Resources Research (WRR)"),
    ("science", "Science"),
)


def _publish_bundle(category, *source_names):
    """Finalize a canonical bundle for today with one item per source."""
    from datetime import timezone

    import run_pipelines as rp
    from datasource import Item as PipelineItem
    from publication import PublicationRunCollector
    from publication.pipeline import results_from_response

    collector = PublicationRunCollector(category)
    for index, name in enumerate(source_names):
        item = PipelineItem(
            title=f"{name} title",
            date=rp.DATE,
            url=f"https://example.org/{name}/{index}",
            extra={"item_id": f"{name}-{index}"},
        )
        results = results_from_response(
            json.dumps(
                {
                    "items": [
                        {
                            "source_ref": "item-0001",
                            "summary": f"{name} summary",
                            "why_it_matters": None,
                            "tags": [],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            [item],
            retrieved_at=datetime(2026, 8, 27, 1, tzinfo=timezone.utc),
            source_name=name,
        )
        collector.add(results)
        collector.add_body(f"# {name}\n\n{name} body", source_name=name)
    rp._finalize_category_publication(category, collector)
    return rp.DATE


def _seed_papers_sources(tmp_path, monkeypatch, entries=_PAPERS_SOURCES):
    """Point push_to_discord at a small papers sources.json."""
    import push_to_discord as pd

    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "name": name,
                        "display_name": display,
                        "category": "papers",
                        "enabled": True,
                    }
                    for name, display in entries
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pd, "SOURCES_JSON", str(path))


def _seed_today_files(date, *, zero=(), failed=()):
    from paths import BRIEFINGS_DIR

    for name in zero:
        _seed_briefing(
            BRIEFINGS_DIR,
            "papers",
            f"{name}_briefing_{date}.md",
            f"# {name} - {date}\n\n📭 过去 24 小时无新内容\n",
        )
    for name in failed:
        _seed_briefing(
            BRIEFINGS_DIR,
            "papers",
            f"{name}_briefing_{date}_failed.md",
            f"# {name} - {date}\n\n⚠️ 以下文章 AI 摘要生成失败，仅保留标题和链接：\n",
        )


def _capture_sends(monkeypatch, channels):
    import push_to_discord as pd

    sent = []
    monkeypatch.setattr(pd, "DISCORD_CHANNELS", dict(channels))
    monkeypatch.setattr(
        pd,
        "send_to_discord",
        lambda channel, content: (sent.append((channel, content)) or True),
    )
    return sent


def test_collect_source_status_buckets_every_configured_source(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_papers_sources(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature", "science")
    _seed_today_files(date, zero=("wrr",), failed=("aies",))

    statuses = pd.collect_source_status("papers", date)

    assert [(s.name, s.status, s.reason) for s in statuses] == [
        ("nature", "pushed", None),
        ("aies", "failed", "generation_failed"),
        ("wrr", "no_update", None),
        ("science", "pushed", None),
    ]
    assert statuses[0].display_name == "Nature"
    assert statuses[2].display_name == "Water Resources Research (WRR)"


def test_collect_source_status_requires_a_canonical_briefing(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_papers_sources(tmp_path, monkeypatch)

    with pytest.raises(FileNotFoundError):
        pd.collect_source_status("papers", "2020-01-01")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("# X\n\n⚠️ 获取失败\n", "fetch_failed"),
        (
            "# X\n\n⚠️ 以下文章 AI 摘要生成失败，仅保留标题和链接：\n",
            "generation_failed",
        ),
        ("# X\n\n⚠️ AI 生成失败\n", "generation_failed"),
        ("# X\n\n📭 过去 24 小时无新内容\n", None),
        ("# X\n\n1. **真实论文**\n   > 摘要\n", None),
    ],
)
def test_failure_notices_are_recognised_but_a_real_briefing_is_not(content, expected):
    import push_to_discord as pd

    assert pd.failure_reason(content) == expected


def test_build_push_summary_lists_failed_sources_apart_from_missing(
    monkeypatch, tmp_path
):
    import push_to_discord as pd

    _seed_papers_sources(tmp_path, monkeypatch)

    summary = pd.build_push_summary(
        "papers",
        "2026-04-25",
        pushed_names=["nature"],
        placeholder_names=["wrr"],
        failed_names=["aies"],
    )

    assert "✅ 已推送期刊 (1):\n- Nature (`nature`)" in summary
    assert "⚠️ 抓取或摘要失败 (1):\n- AIES (`aies`)" in summary
    assert "⚠️ 未发现今日简报文件 (1):\n- Science (`science`)" in summary


def test_canonical_delivery_posts_the_body_then_the_source_summary(
    monkeypatch, tmp_path
):
    import push_to_discord as pd
    from paths import BRIEFINGS_DIR

    _seed_papers_sources(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature", "science")
    _seed_today_files(date, zero=("wrr",), failed=("aies",))
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})

    assert pd.main(date, categories=["papers"]) == 0

    assert len(sent) == 2
    body_channel, body = sent[0]
    assert body_channel == "channel-1"
    assert "# nature" in body and "nature body" in body
    summary_channel, summary = sent[1]
    assert summary_channel == "channel-1"
    assert "📊 论文频道推送总结" in summary
    assert "✅ 已推送期刊 (2):" in summary
    assert "- Nature (`nature`)" in summary
    assert "- Science (`science`)" in summary
    assert "📭 今日无文章更新 (1):" in summary
    assert "- Water Resources Research (WRR) (`wrr`)" in summary
    assert "⚠️ 抓取或摘要失败 (1):" in summary
    assert "- AIES (`aies`)" in summary
    # The scan ran before the archive: the placeholder evidence is gone now.
    assert not (BRIEFINGS_DIR / "papers" / f"wrr_briefing_{date}.md").exists()


def test_the_summary_is_not_posted_by_a_skipped_run(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_papers_sources(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature")
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})

    assert pd.main(date, categories=["papers"]) == 0
    after_first = len(sent)
    assert pd.main(date, categories=["papers"]) == 0

    assert len(sent) == after_first
    summaries = [content for _, content in sent if "论文频道推送总结" in content]
    assert len(summaries) == 1


def test_no_summary_when_the_delivery_fails(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_papers_sources(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature")
    _seed_today_files(date, zero=("wrr",))

    sent = []
    monkeypatch.setattr(pd, "DISCORD_CHANNELS", {"papers": "channel-1"})
    monkeypatch.setattr(
        pd,
        "send_to_discord",
        lambda channel, content: (sent.append((channel, content)) or False),
    )

    assert pd.main(date, categories=["papers"]) == 1
    assert sent  # the body was attempted
    assert not any("推送总结" in content for _, content in sent)


def test_the_summary_counts_a_source_whose_file_was_archived(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_papers_sources(tmp_path, monkeypatch, entries=(("nature", "Nature"),))
    date = _publish_bundle("papers", "nature")
    # No file for nature exists: the bundle is the only place it appears.
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})

    assert pd.main(date, categories=["papers"]) == 0

    summary = sent[-1][1]
    assert "✅ 已推送期刊 (1):" in summary
    assert "- Nature (`nature`)" in summary
    assert "未发现" not in summary


def test_the_source_status_sidecar_records_every_source(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_papers_sources(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature", "science")
    _seed_today_files(date, zero=("wrr",), failed=("aies",))
    _capture_sends(monkeypatch, {"papers": "channel-1"})

    assert pd.main(date, categories=["papers"]) == 0

    payload = json.loads(pd.source_status_path("papers", date).read_text("utf-8"))
    assert payload["schema_version"] == 1
    assert payload["category"] == "papers"
    assert payload["date"] == date
    assert payload["counts"] == {
        "configured": 4,
        "pushed": 2,
        "no_update": 1,
        "failed": 1,
        "missing": 0,
    }
    assert [(s["name"], s["status"], s["reason"]) for s in payload["sources"]] == [
        ("nature", "pushed", None),
        ("aies", "failed", "generation_failed"),
        ("wrr", "no_update", None),
        ("science", "pushed", None),
    ]


def test_a_sidecar_write_failure_keeps_the_delivered_day_and_reports(
    monkeypatch, tmp_path
):
    import push_to_discord as pd

    _seed_papers_sources(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature")
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})
    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)
    monkeypatch.setattr(pd, "write_source_status_sidecar", lambda *a, **k: False)

    assert pd.main(date, categories=["papers"]) == 1
    assert any("推送总结" in content for _, content in sent)
    assert any("推送后续步骤失败" in line for line in logs), logs


def test_a_summary_send_failure_is_counted_as_failed(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_papers_sources(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature")
    sent = []
    logs: list[str] = []
    monkeypatch.setattr(pd, "DISCORD_CHANNELS", {"papers": "channel-1"})
    monkeypatch.setattr(
        pd,
        "send_to_discord",
        lambda channel, content: (
            sent.append((channel, content)) or "推送总结" not in content
        ),
    )
    monkeypatch.setattr(pd, "log", logs.append)

    assert pd.main(date, categories=["papers"]) == 1
    assert any("来源总结发送失败" in line for line in logs), logs
    # The day's source list is still on disk for the Web sink.
    assert pd.source_status_path("papers", date).exists()


def test_the_summary_is_papers_only(monkeypatch, tmp_path):
    import push_to_discord as pd

    date = _publish_bundle("code", "github_trending")
    sent = _capture_sends(monkeypatch, {"code": "channel-1"})

    assert pd.main(date, categories=["code"]) == 0

    assert sent
    assert not any("推送总结" in content for _, content in sent)
    assert not pd.source_status_path("code", date).exists()
