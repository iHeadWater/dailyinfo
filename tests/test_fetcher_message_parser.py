"""Tests for dailyinfo_fetcher.message_parser (pure-function coverage)."""
from dailyinfo_fetcher.message_parser import (
    extract_items,
    extract_paper_titles,
    is_github_related,
    is_paper_message,
    resolve_intent_local,
)

_BRIEFING = """🗂 AI论文简报 今日简报

1. **Attention Is All You Need**
   Transformer paper

2. **BERT: Pre-training of Deep Bidirectional Transformers**
   Google language model

3. **GPT-4 Technical Report**
   OpenAI flagship
"""


def test_is_paper_message_true():
    assert is_paper_message("🗂 AI论文简报 今日简报")


def test_is_paper_message_false():
    assert not is_paper_message("GitHub Trending 2024-01-01")


def test_is_github_related_true():
    assert is_github_related("https://github.com/owner/repo is cool")


def test_is_github_related_false():
    assert not is_github_related("No links here at all")


def test_extract_paper_titles():
    titles = extract_paper_titles(_BRIEFING)
    assert "Attention Is All You Need" in titles
    assert "BERT: Pre-training of Deep Bidirectional Transformers" in titles
    assert len(titles) == 3


def test_extract_items_indices():
    items = extract_items(_BRIEFING)
    indices = {i.index for i in items}
    assert 1 in indices
    assert 2 in indices


def test_resolve_intent_local_chinese():
    items = extract_items(_BRIEFING)
    result = resolve_intent_local("第1条", items)
    assert result is not None
    assert result.index == 1


def test_resolve_intent_local_bare_digit():
    items = extract_items(_BRIEFING)
    result = resolve_intent_local("2", items)
    assert result is not None
    assert result.index == 2


def test_resolve_intent_local_not_found():
    items = extract_items(_BRIEFING)
    assert resolve_intent_local("something random", items) is None
