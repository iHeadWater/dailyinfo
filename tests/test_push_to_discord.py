"""Tests for ``scripts/push_to_discord.py``."""

from __future__ import annotations

from datetime import datetime
import json


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


def test_send_figure_to_discord_uses_multipart_and_attachment_embed(
    monkeypatch, tmp_path
):
    import push_to_discord as pd

    image = tmp_path / "hero.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfigure")
    captured = []

    class _Resp:
        status_code = 200
        text = ""

    def fake_post(url, headers, data, files, timeout):
        captured.append((url, headers, data, files, timeout))
        return _Resp()

    monkeypatch.setattr(pd.requests, "post", fake_post)
    monkeypatch.setattr(pd.time, "sleep", lambda *_: None)

    assert pd.send_figure_to_discord(
        "chan-123",
        image,
        title="Example model",
        caption="Figure 1: Overview.",
        nonce_prefix="bundle-1",
    ) is True
    assert captured
    _url, headers, data, files, _timeout = captured[0]
    assert "Content-Type" not in headers
    payload = json.loads(data["payload_json"])
    assert payload["embeds"][0]["image"]["url"] == "attachment://hero.png"
    assert payload["enforce_nonce"] is True
    assert "files[0]" in files


def test_conference_briefing_places_each_figure_after_its_paper(monkeypatch, tmp_path):
    import push_to_discord as pd

    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"png-1")
    second.write_bytes(b"png-2")
    content = (
        "# ICLR Briefing\n\n"
        "### Paper One（状态：accepted）\n\nSummary one.\n\n"
        "### Paper Two\n\nSummary two."
    )
    sidecar = {
        "attachments": [
            {
                "event_id": "event-2",
                "title": "Paper Two",
                "manifest": {"path": str(second), "caption": "Figure 2"},
            },
            {
                "event_id": "event-1",
                "title": "Paper One",
                "manifest": {"path": str(first), "caption": "Figure 1"},
            },
        ]
    }
    sent = []
    monkeypatch.setattr(
        pd,
        "send_to_discord",
        lambda channel, text, **kwargs: (
            sent.append(("text", text.splitlines()[0])), True
        )[1],
    )
    monkeypatch.setattr(
        pd,
        "send_figure_to_discord",
        lambda channel, path, **kwargs: (
            sent.append(("figure", kwargs["title"])), True
        )[1],
    )
    monkeypatch.setattr(pd, "_save_receipt", lambda *_args: None)

    assert pd.send_conference_briefing(
        "channel-1", content, sidecar, {}, filename="briefing.md"
    )
    assert sent == [
        ("text", "# ICLR Briefing"),
        ("text", "### Paper One（状态：accepted）"),
        ("figure", "Paper One"),
        ("text", "### Paper Two"),
        ("figure", "Paper Two"),
    ]
