"""Tests for ``scripts/push_to_discord.py``."""

from __future__ import annotations

from datetime import datetime


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
    assert "暂无新内容" in sent[0][1] or "均无新内容" in sent[0][1]


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


def test_push_category_returns_zero_when_directory_missing(monkeypatch):
    import push_to_discord as pd

    monkeypatch.setattr(pd, "send_to_discord", lambda *a, **k: True)
    # category dir not created → function should bail out cleanly
    assert pd.push_category("nope", "chan") == 0


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
