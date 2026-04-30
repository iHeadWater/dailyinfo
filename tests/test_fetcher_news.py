"""Tests for dailyinfo_fetcher.news_fetcher (mocked network)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from dailyinfo_fetcher.news_fetcher import fetch_with_jina, generate_summary


def _make_async_client(get_return=None, post_return=None, side_effect=None):
    """Build a mock httpx.AsyncClient context manager."""
    mock_client = AsyncMock()
    if side_effect:
        mock_client.get = AsyncMock(side_effect=side_effect)
        mock_client.post = AsyncMock(side_effect=side_effect)
    else:
        if get_return is not None:
            mock_client.get = AsyncMock(return_value=get_return)
        if post_return is not None:
            mock_client.post = AsyncMock(return_value=post_return)
    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    return mock_instance


def test_fetch_with_jina_returns_content():
    resp = MagicMock()
    resp.text = "Article content " * 100

    async def _run():
        with patch("dailyinfo_fetcher.news_fetcher.httpx.AsyncClient", return_value=_make_async_client(get_return=resp)):
            return await fetch_with_jina("https://example.com/article")

    result = asyncio.run(_run())
    assert isinstance(result, str)
    assert len(result) > 0


def test_fetch_with_jina_truncates_to_8000():
    resp = MagicMock()
    resp.text = "x" * 20000

    async def _run():
        with patch("dailyinfo_fetcher.news_fetcher.httpx.AsyncClient", return_value=_make_async_client(get_return=resp)):
            return await fetch_with_jina("https://example.com")

    result = asyncio.run(_run())
    assert len(result) == 8000


def test_fetch_with_jina_returns_empty_on_error():
    async def _run():
        with patch("dailyinfo_fetcher.news_fetcher.httpx.AsyncClient", return_value=_make_async_client(side_effect=Exception("timeout"))):
            return await fetch_with_jina("https://example.com")

    result = asyncio.run(_run())
    assert result == ""


def test_generate_summary_no_api_key_returns_snippets():
    results = [
        {"title": "Article A", "url": "https://a.com", "content": "Content A"},
        {"title": "Article B", "url": "https://b.com", "content": "Content B"},
    ]

    async def _run():
        with patch("dailyinfo_fetcher.news_fetcher.OPENROUTER_API_KEY", ""):
            return await generate_summary("Test Topic", results, "")

    result = asyncio.run(_run())
    assert "Test Topic" in result
    assert "Article A" in result


def test_generate_summary_empty_results_no_key():
    async def _run():
        with patch("dailyinfo_fetcher.news_fetcher.OPENROUTER_API_KEY", ""):
            return await generate_summary("My Topic", [], "")

    result = asyncio.run(_run())
    assert "My Topic" in result
