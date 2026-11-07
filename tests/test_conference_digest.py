"""Tests for the compact conference digest generated for Discord."""

from __future__ import annotations

import json
import os


def _block(
    forum_id: str,
    *,
    title: str = "Test Paper",
    ratings: str = "2、3",
    summary: str = "这是测试论文做了什么。第二句不应进入速览。",
) -> str:
    return f"""### {title}（under_review）

- 会议：ICLR 2027
- 作者：未提供
- 论文详情页：[OpenReview](https://openreview.net/forum?id={forum_id})
- PDF：[PDF](https://openreview.net/pdf/{forum_id}.pdf)
- 状态：under_review

**研究内容：** {summary}

**公开评审评分**：原始评分（raw_review_ratings）：{ratings}；原始 confidence：3、3。

**Paper Decision：** 尚未获取公开决定。

**Reviewer 意见：**
- Reviewer 1：肯定方法，质疑基线。

**Rebuttal / Author Response：** 未提供公开作者回复。
"""


def _briefing(*blocks: str) -> str:
    return "# ICLR 2027 会议评审简报 — 2026-11-07\n\n" + "\n".join(blocks)


def _write_briefing(directory, source: str, name: str, content: str):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{source}_ai_{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _write_sidecar(path, papers: dict[str, str], *, version=None, complete=True):
    if version is None:
        import conference_digest as cd

        version = cd.ONE_LINER_PROMPT_VERSION
    sidecar = path.with_name(path.name + ".digest.json")
    payload = {
        "version": version,
        "generated_ms": 0,
        "complete": complete,
        "papers": {
            forum_id: {
                "title": f"Paper {forum_id}",
                "one_line": one_line,
                "fallback": False,
            }
            for forum_id, one_line in papers.items()
        },
    }
    sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return sidecar


def _fake_call_ai(prompt: str, max_tokens: int) -> str:
    forum_ids = __import__("re").findall(
        r"^forum_id: (.+)$", prompt, __import__("re").M
    )
    return "\n".join(
        f"{forum_id}\t做了测试工作；评审：肯定，方法清晰。" for forum_id in forum_ids
    )


def _sources():
    return [
        {
            "name": "openreview_iclr_2027",
            "display_name": "ICLR 2027",
            "category": "conference",
            "enabled": True,
        }
    ]


def _summary_lookup(source, forum_ids):
    return {
        forum_id: {
            "decision": "Accept" if forum_id.endswith("1") else "",
            "ratings": ["2", "3"],
        }
        for forum_id in forum_ids
    }


def test_parse_ai_briefing_extracts_metadata_and_summary():
    import conference_digest as cd

    parsed = cd.parse_ai_briefing(_briefing(_block("abc123", title="My Paper")))

    assert len(parsed) == 1
    paper = parsed[0]
    assert paper["forum_id"] == "abc123"
    assert paper["title"] == "My Paper"
    assert paper["ratings"] == ("2", "3")
    assert paper["summary"] == "这是测试论文做了什么。"
    assert paper["url"] == "https://openreview.net/forum?id=abc123"


def test_title_status_suffix_is_removed():
    import conference_digest as cd

    parsed = cd.parse_ai_briefing(
        _briefing(_block("abc123", title="A Title（状态：under_review）"))
    )

    assert parsed[0]["title"] == "A Title"


def test_build_digest_uses_paper_style_and_metadata(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
    )

    assert len(digests) == 1
    content = digests[0].content
    assert content.startswith("## 📚 ICLR 2027 会议速览 (2026-11-07) - 1 篇评审更新")
    assert "1. **[Test Paper](https://openreview.net/forum?id=abc123)**" in content
    assert "> 做了测试工作；评审：肯定，方法清晰。" in content
    assert "> （评分 2/3 · Decision: 暂无）" in content
    assert "Reviewer 意见" not in content
    assert "Rebuttal" not in content


def test_decision_is_rendered_when_available(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("forum-1"))
    )

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
    )

    assert "Decision: Accept" in digests[0].content


def test_prompt_keeps_reason_concise():
    import conference_digest as cd

    prompt = cd._model_prompt(
        [
            {
                "forum_id": "abc123",
                "title": "Test Paper",
                "block": "### Test Paper\n\n研究内容：测试。",
            }
        ]
    )

    assert "理由点到为止即可" in prompt
    assert "具体到实验" not in prompt


def test_missing_review_colon_is_normalised(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    path = _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )
    _write_sidecar(path, {"abc123": "做了一件事；评审偏负面，证据不足。"})

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
    )

    assert "评审：偏负面" in digests[0].content


def test_double_colon_after_review_attitude_is_normalised(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    path = _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )
    _write_sidecar(path, {"abc123": "做了一件事；评审：混合：肯定实验，质疑基线。"})

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
    )

    assert "评审：混合，肯定实验" in digests[0].content
    assert "评审：混合：肯定" not in digests[0].content


def test_sidecar_cache_is_reused(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )
    calls = []

    def call_ai(prompt, max_tokens):
        calls.append(prompt)
        return _fake_call_ai(prompt, max_tokens)

    first = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=call_ai,
    )
    second = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=call_ai,
    )

    assert first[0].content == second[0].content
    assert len(calls) == 1
    sidecars = list(directory.glob("*.digest.json"))
    assert len(sidecars) == 1


def test_prompt_version_invalidates_sidecar(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    path = _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )
    _write_sidecar(path, {"abc123": "旧的一句话；评审：混合。"}, version=1)
    calls = []

    def call_ai(prompt, max_tokens):
        calls.append(prompt)
        return _fake_call_ai(prompt, max_tokens)

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=call_ai,
    )

    assert len(calls) == 1
    assert "做了测试工作" in digests[0].content


def test_fallback_when_model_fails(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    path = _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )

    def fail(prompt, max_tokens):
        raise RuntimeError("model down")

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=fail,
    )

    assert "这是测试论文做了什么" in digests[0].content
    assert digests[0].papers[0].fallback is True
    payload = json.loads(
        path.with_name(path.name + ".digest.json").read_text(encoding="utf-8")
    )
    assert payload["complete"] is False


def test_incomplete_sidecar_is_regenerated(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    path = _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )
    _write_sidecar(path, {"abc123": "这是测试论文做了什么。"}, complete=False)
    calls = []

    def call_ai(prompt, max_tokens):
        calls.append(prompt)
        return _fake_call_ai(prompt, max_tokens)

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=call_ai,
    )

    assert len(calls) == 1
    assert "做了测试工作" in digests[0].content


def test_dedupe_keeps_latest_file(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    first = _write_briefing(
        directory, "openreview_iclr_2027", "first", _briefing(_block("abc123"))
    )
    second = _write_briefing(
        directory, "openreview_iclr_2027", "second", _briefing(_block("abc123"))
    )
    _write_sidecar(first, {"abc123": "旧版本；评审：混合。"})
    _write_sidecar(second, {"abc123": "新版本；评审：肯定。"})
    os.utime(first, (1_700_000_000, 1_700_000_000))
    os.utime(second, (1_700_000_100, 1_700_000_100))

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
    )

    assert len(digests[0].papers) == 1
    assert "新版本" in digests[0].content


def test_limit_caps_papers(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    content = _briefing(*(_block(f"forum-{i}") for i in range(5)))
    _write_briefing(directory, "openreview_iclr_2027", "one", content)

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
        limit=2,
    )

    assert len(digests[0].papers) == 2


def test_42_papers_keep_full_titles_and_stay_within_seven_messages(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    blocks = [_block(f"forum-{i}", title=f"Paper {i} " + "x" * 80) for i in range(42)]
    _write_briefing(directory, "openreview_iclr_2027", "one", _briefing(*blocks))

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
    )

    content = digests[0].content
    assert len(digests[0].papers) == 42
    assert "Paper 0 " + "x" * 80 in content
    assert len(content) < 12000
    assert cd.estimated_discord_messages(content) <= 7


def test_long_title_is_not_truncated(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    long_title = "A Very Long Title " + "y" * 120
    _write_briefing(
        directory,
        "openreview_iclr_2027",
        "one",
        _briefing(_block("abc123", title=long_title)),
    )

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
    )

    assert (
        f"**[{long_title}](https://openreview.net/forum?id=abc123)**"
        in digests[0].content
    )
    assert "…" not in digests[0].papers[0].title


def test_long_one_line_is_not_truncated(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    path = _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )
    long_one_line = "做了很长的工作；评审：混合，" + "核心理由" * 20 + "。"
    _write_sidecar(path, {"abc123": long_one_line})

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
    )

    quote = "".join(
        line.strip()[2:]
        for line in digests[0].content.splitlines()
        if line.strip().startswith(">")
    )
    assert long_one_line in quote
    assert "…" not in digests[0].papers[0].one_line


def test_quote_lines_are_wrapped_at_sixty_chars(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    path = _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )
    _write_sidecar(path, {"abc123": "很长的一句话；" * 30})

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=_fake_call_ai,
    )

    quote_lines = [
        line.strip()[2:]
        for line in digests[0].content.splitlines()
        if line.strip().startswith(">")
    ]
    assert len(quote_lines) > 1
    assert all(len(line) <= 60 for line in quote_lines)


def test_wrap_text_preserves_every_character():
    import conference_digest as cd

    text = "这是一句很长的评审总结，" * 8 + "结束。"
    wrapped = cd._wrap_text(text, width=30)

    assert len(wrapped) > 1
    assert "".join(wrapped) == text
    assert all(len(line) <= 30 for line in wrapped)


def test_database_title_overrides_truncated_briefing_title(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    _write_briefing(
        directory,
        "openreview_iclr_2027",
        "one",
        _briefing(_block("abc123", title="Truncated Ti…")),
    )

    def lookup(source, forum_ids):
        return {
            "abc123": {
                "title": "The Complete Untruncated Paper Title",
                "decision": "",
                "ratings": ["2", "3"],
            }
        }

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=lookup,
        call_ai=_fake_call_ai,
    )

    assert "The Complete Untruncated Paper Title" in digests[0].content
    assert "Truncated Ti…" not in digests[0].content


def test_no_model_generation_uses_extractive_fallback(tmp_path):
    import conference_digest as cd

    directory = tmp_path / "conference"
    _write_briefing(
        directory, "openreview_iclr_2027", "one", _briefing(_block("abc123"))
    )

    def call_ai(prompt, max_tokens):
        raise AssertionError("model must not be called")

    digests = cd.build_digests(
        "2026-11-07",
        sources=_sources(),
        briefings_dir=directory,
        summary_lookup=_summary_lookup,
        call_ai=call_ai,
        generate=False,
    )

    assert "这是测试论文做了什么" in digests[0].content
    assert digests[0].papers[0].fallback is True
