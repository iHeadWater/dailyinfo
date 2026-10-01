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


def _seed_sources_config(
    tmp_path, monkeypatch, entries=_PAPERS_SOURCES, *, category="papers"
):
    """Point push_to_discord at a small sources.json.

    An entry is either ``(name, display)`` or a full dict (for per-source
    fields like ``lookback_hours``).
    """
    import push_to_discord as pd

    rows = []
    for entry in entries:
        if isinstance(entry, dict):
            rows.append({"enabled": True, "category": category, **entry})
        else:
            name, display = entry
            rows.append(
                {
                    "name": name,
                    "display_name": display,
                    "category": category,
                    "enabled": True,
                }
            )
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps({"sources": rows}, ensure_ascii=False),
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

    _seed_sources_config(tmp_path, monkeypatch)
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

    _seed_sources_config(tmp_path, monkeypatch)

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

    _seed_sources_config(tmp_path, monkeypatch)

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

    _seed_sources_config(tmp_path, monkeypatch)
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

    _seed_sources_config(tmp_path, monkeypatch)
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

    _seed_sources_config(tmp_path, monkeypatch)
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

    _seed_sources_config(tmp_path, monkeypatch, entries=(("nature", "Nature"),))
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

    _seed_sources_config(tmp_path, monkeypatch)
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
    # The timestamp is a contract for the future Web reader; pin the format.
    datetime.strptime(payload["generated_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    assert payload["summary_posted"] is True


def test_a_sidecar_write_failure_keeps_the_delivered_day_and_reports(
    monkeypatch, tmp_path
):
    import push_to_discord as pd

    _seed_sources_config(tmp_path, monkeypatch)
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

    _seed_sources_config(tmp_path, monkeypatch)
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

    # A configured code source, so a category leaking into SUMMARY_CATEGORIES
    # would actually produce a summary here rather than silently pass.
    _seed_sources_config(
        tmp_path,
        monkeypatch,
        entries=(("github_trending", "GitHub Trending"),),
        category="code",
    )
    date = _publish_bundle("code", "github_trending")
    sent = _capture_sends(monkeypatch, {"code": "channel-1"})

    assert pd.main(date, categories=["code"]) == 0

    assert sent
    assert not any("推送总结" in content for _, content in sent)
    assert not pd.source_status_path("code", date).exists()


def test_a_real_sidecar_write_error_is_reported_not_raised(monkeypatch, tmp_path):
    import push_to_discord as pd

    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)

    def failing_write(path, value):
        raise OSError("disk full")

    monkeypatch.setattr(pd, "_write_json_atomic", failing_write)
    status = pd.SourceStatus("nature", "Nature", "pushed")

    assert pd.write_source_status_sidecar("papers", "2026-04-25", [status]) is False
    assert any("源状态文件写入失败" in line for line in logs), logs


def test_a_forced_redelivery_does_not_rescan_an_archived_day(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_sources_config(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature", "science")
    _seed_today_files(date, zero=("wrr",), failed=("aies",))
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})
    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)

    assert pd.main(date, categories=["papers"]) == 0
    summaries = [content for _, content in sent if "论文频道推送总结" in content]
    assert len(summaries) == 1
    assert "📭 今日无文章更新 (1):" in summaries[0]

    # A forced redelivery re-sends the body, but the day's evidence is
    # archived: rebuilding the summary would report every source without a
    # bundle entry as "missing" and overwrite the good sidecar with that.
    assert pd.main(date, categories=["papers"], force=True) == 0

    summaries = [content for _, content in sent if "论文频道推送总结" in content]
    assert len(summaries) == 1
    payload = json.loads(pd.source_status_path("papers", date).read_text("utf-8"))
    assert payload["counts"]["no_update"] == 1
    assert payload["counts"]["failed"] == 1
    assert any("跳过来源总结" in line for line in logs), logs


def test_an_unreadable_notice_file_is_reported(monkeypatch, tmp_path):
    import push_to_discord as pd
    from paths import BRIEFINGS_DIR

    _seed_sources_config(
        tmp_path,
        monkeypatch,
        entries=(("nature", "Nature"), ("wrr", "Water Resources Research (WRR)")),
    )
    date = _publish_bundle("papers", "nature")
    path = BRIEFINGS_DIR / "papers" / f"wrr_briefing_{date}.md"
    _seed_briefing(BRIEFINGS_DIR, "papers", f"wrr_briefing_{date}.md", "x")
    path.chmod(0)

    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)
    try:
        statuses = pd.collect_source_status("papers", date)
    finally:
        path.chmod(0o644)

    assert [(s.name, s.status) for s in statuses] == [
        ("nature", "pushed"),
        ("wrr", "missing"),
    ]
    assert any("wrr_briefing" in line for line in logs), logs


def test_a_non_utf8_notice_file_does_not_block_the_delivery(monkeypatch, tmp_path):
    import push_to_discord as pd
    from paths import BRIEFINGS_DIR

    _seed_sources_config(
        tmp_path,
        monkeypatch,
        entries=(("nature", "Nature"), ("wrr", "Water Resources Research (WRR)")),
    )
    date = _publish_bundle("papers", "nature")
    cat_dir = BRIEFINGS_DIR / "papers"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"wrr_briefing_{date}.md").write_bytes(b"\xff\xfe not utf-8")
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})
    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)

    assert pd.main(date, categories=["papers"]) == 0
    assert any("nature body" in content for _, content in sent)
    assert any("wrr_briefing" in line for line in logs), logs


def test_a_sources_config_failure_is_reported(monkeypatch, tmp_path):
    import push_to_discord as pd

    monkeypatch.setattr(pd, "SOURCES_JSON", str(tmp_path / "missing.json"))
    date = _publish_bundle("papers", "nature")
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})
    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)

    assert pd.main(date, categories=["papers"]) == 1
    assert not any("推送总结" in content for _, content in sent)
    assert any("推送后续步骤失败" in line for line in logs), logs


def test_failure_markers_match_the_run_notices():
    import push_to_discord as pd
    import run_pipelines as rp

    run_failure_markers = set(rp._PLACEHOLDER_MARKERS) - {"📭 过去"}
    assert {marker for marker, _ in pd._FAILURE_MARKERS} == run_failure_markers


def test_a_source_with_both_a_bundle_item_and_a_failure_notice_is_pushed(
    monkeypatch, tmp_path
):
    import push_to_discord as pd
    from paths import BRIEFINGS_DIR

    _seed_sources_config(
        tmp_path,
        monkeypatch,
        entries=(("nature", "Nature"), ("aies", "AIES")),
    )
    date = _publish_bundle("papers", "nature")
    # nature has both published content and a stale failure notice.
    _seed_briefing(
        BRIEFINGS_DIR,
        "papers",
        f"nature_briefing_{date}_failed.md",
        f"# Nature - {date}\n\n⚠️ 获取失败\n",
    )

    statuses = pd.collect_source_status("papers", date)

    assert [(s.name, s.status, s.reason) for s in statuses] == [
        ("nature", "pushed", None),
        ("aies", "missing", None),
    ]


def test_source_names_resolve_from_part_retry_and_failed_filenames():
    import push_to_discord as pd

    sources = [
        {"name": "nature", "display_name": "Nature"},
        {"name": "nature_communications", "display_name": "Nature Communications"},
        {"name": "latent_space", "display_name": "Latent Space"},
    ]
    cases = {
        "nature_briefing_2026-10-01.md": "nature",
        "nature_communications_briefing_2026-10-01.md": "nature_communications",
        "latent_space_briefing_2026-10-01_part2.md": "latent_space",
        "latent_space_briefing_2026-10-01_retry1.md": "latent_space",
        "latent_space_briefing_2026-10-01_failed2.md": "latent_space",
    }

    for filename, expected in cases.items():
        assert pd._source_name_from_filename(filename, sources) == expected


def test_a_low_frequency_source_skipped_by_the_run_reads_as_no_update(
    monkeypatch, tmp_path
):
    import push_to_discord as pd
    from paths import PUSHED_DIR

    _seed_sources_config(
        tmp_path,
        monkeypatch,
        entries=(
            ("nature", "Nature"),
            {
                "name": "shuili_xuebao",
                "display_name": "水利学报",
                "lookback_hours": 720,
            },
        ),
    )
    date = _publish_bundle("papers", "nature")
    pushed_dir = PUSHED_DIR / "papers"
    pushed_dir.mkdir(parents=True, exist_ok=True)
    # Skipped by run_pipelines._already_pushed_within: a recent archive still
    # sits inside this source's lookback window, and no file is written today.
    (pushed_dir / "shuili_xuebao_briefing_2026-09-28.md").write_text(
        "旧简报", encoding="utf-8"
    )

    statuses = pd.collect_source_status("papers", date)

    assert [(s.name, s.status) for s in statuses] == [
        ("nature", "pushed"),
        ("shuili_xuebao", "no_update"),
    ]


def test_a_non_utf8_leftover_does_not_crash_a_bundleless_category(
    monkeypatch, tmp_path
):
    import push_to_discord as pd
    from paths import BRIEFINGS_DIR

    # papers has no canonical bundle, so the push falls into the legacy
    # branch -- where an unreadable leftover used to raise out of main() and
    # take every later category down with it.
    date = _publish_bundle("code", "github_trending")
    cat_dir = BRIEFINGS_DIR / "papers"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / f"leftover_{date}.md").write_bytes(b"\xff\xfe not utf-8")
    sent = _capture_sends(monkeypatch, {"papers": "channel-1", "code": "channel-1"})
    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)

    assert pd.main(date, categories=["papers", "code"]) == 1

    # code still delivered: the failure stays contained to papers.
    assert any("github_trending body" in content for _, content in sent)
    assert any("refusing fallback" in line for line in logs), logs


def test_cleanup_placeholder_files_tolerates_unreadable_files(tmp_path, monkeypatch):
    import push_to_discord as pd

    bad = tmp_path / "bad_briefing.md"
    bad.write_bytes(b"\xff\xfe not utf-8")
    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)

    pd._cleanup_placeholder_files([str(bad)])

    assert bad.exists()
    assert any("出错" in line for line in logs), logs


def test_a_missing_summary_is_repaired_by_a_forced_redelivery(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_sources_config(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature", "science")
    _seed_today_files(date, zero=("wrr",), failed=("aies",))
    sent: list[tuple] = []
    logs: list[str] = []
    monkeypatch.setattr(pd, "DISCORD_CHANNELS", {"papers": "channel-1"})
    monkeypatch.setattr(pd, "log", logs.append)

    def delivered_summaries():
        return [
            content for _, content, ok in sent if ok and "论文频道推送总结" in content
        ]

    # The body delivers, its summary never does.
    def send_failing_summary(channel, content):
        delivered = "推送总结" not in content
        sent.append((channel, content, delivered))
        return delivered

    monkeypatch.setattr(pd, "send_to_discord", send_failing_summary)

    assert pd.main(date, categories=["papers"]) == 1
    assert delivered_summaries() == []
    payload = json.loads(pd.source_status_path("papers", date).read_text("utf-8"))
    assert payload["summary_posted"] is False

    # A forced redelivery repairs it from the record of that run.
    monkeypatch.setattr(
        pd,
        "send_to_discord",
        lambda channel, content: (sent.append((channel, content, True)) or True),
    )
    assert pd.main(date, categories=["papers"], force=True) == 0

    summaries = delivered_summaries()
    assert len(summaries) == 1
    assert "📭 今日无文章更新 (1):" in summaries[0]
    assert "⚠️ 抓取或摘要失败 (1):" in summaries[0]
    payload = json.loads(pd.source_status_path("papers", date).read_text("utf-8"))
    assert payload["summary_posted"] is True

    # Once posted, further redeliveries stay silent.
    assert pd.main(date, categories=["papers"], force=True) == 0
    assert len(delivered_summaries()) == 1


def test_a_regenerated_day_with_old_archive_still_posts_the_summary(
    monkeypatch, tmp_path
):
    import push_to_discord as pd
    from paths import PUSHED_DIR

    _seed_sources_config(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature", "science")
    _seed_today_files(date, zero=("wrr",), failed=("aies",))
    # A previous delivery's archive exists for the date, but the files on
    # disk are fresh again: a forced re-run regenerated the day and voided
    # its delivery state (the pending attempt below stands in for that).
    pushed_dir = PUSHED_DIR / "papers"
    pushed_dir.mkdir(parents=True, exist_ok=True)
    (pushed_dir / f"nature_briefing_{date}.md").write_text("旧归档", encoding="utf-8")
    from datetime import timezone

    pd.DeliveryStateStore().begin_attempt(
        f"papers-{date}", "discord", attempted_at=datetime.now(timezone.utc)
    )
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})

    assert pd.main(date, categories=["papers"]) == 0

    summaries = [content for _, content in sent if "论文频道推送总结" in content]
    assert len(summaries) == 1
    assert "📭 今日无文章更新 (1):" in summaries[0]


def test_a_scan_crash_does_not_block_the_delivery(monkeypatch, tmp_path):
    import push_to_discord as pd

    _seed_sources_config(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature")
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})
    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pd, "collect_source_status", boom)

    # The summary is auxiliary, but its failure is counted, not swallowed.
    assert pd.main(date, categories=["papers"]) == 1
    assert any("nature body" in content for _, content in sent)
    assert any("来源状态扫描失败" in line for line in logs), logs


def test_a_structurally_broken_sources_config_is_reported(monkeypatch, tmp_path):
    import push_to_discord as pd

    broken = tmp_path / "sources.json"
    broken.write_text(json.dumps({"sources": "not-a-list"}), encoding="utf-8")
    monkeypatch.setattr(pd, "SOURCES_JSON", str(broken))
    date = _publish_bundle("papers", "nature")
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})
    logs: list[str] = []
    monkeypatch.setattr(pd, "log", logs.append)

    assert pd.main(date, categories=["papers"]) == 1
    assert not any("推送总结" in content for _, content in sent)
    assert any("推送后续步骤失败" in line for line in logs), logs


def test_an_unreadable_archive_does_not_block_the_delivery(monkeypatch, tmp_path):
    import push_to_discord as pd
    from paths import PUSHED_DIR

    _seed_sources_config(tmp_path, monkeypatch)
    date = _publish_bundle("papers", "nature")
    pushed_dir = PUSHED_DIR / "papers"
    pushed_dir.mkdir(parents=True, exist_ok=True)
    pushed_dir.chmod(0)
    sent = _capture_sends(monkeypatch, {"papers": "channel-1"})
    try:
        assert pd.main(date, categories=["papers"]) == 0
    finally:
        pushed_dir.chmod(0o755)

    assert any("nature body" in content for _, content in sent)


def test_the_lookback_mirror_matches_the_run_side(monkeypatch):
    import os
    import time

    import push_to_discord as pd
    import run_pipelines as rp
    from paths import PUSHED_DIR

    monkeypatch.setattr(rp, "FORCE_ALL", False)
    monkeypatch.setattr(rp, "FORCE_SOURCES", set())

    pushed_dir = PUSHED_DIR / "papers"
    pushed_dir.mkdir(parents=True, exist_ok=True)
    (pushed_dir / "nature_briefing_2026-09-30.md").write_text("x", encoding="utf-8")
    stale = pushed_dir / "science_briefing_2026-09-30.md"
    stale.write_text("x", encoding="utf-8")
    long_ago = time.time() - 72 * 3600
    os.utime(stale, (long_ago, long_ago))
    (pushed_dir / "other_briefing_2026-09-30.md").write_text("x", encoding="utf-8")

    for name in ("nature", "science", "ghost"):
        for lookback in (48, 720):
            assert pd._pushed_within_lookback("papers", name, lookback) == (
                rp._already_pushed_within(name, "papers", lookback)
            )
