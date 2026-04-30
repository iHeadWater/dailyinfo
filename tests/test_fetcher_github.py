"""Tests for dailyinfo_fetcher.github_fetcher (mocked network)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from dailyinfo_fetcher.github_fetcher import extract_github_repo, fetch_github_card


def _async_client_ctx(client_mock):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client_mock)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# extract_github_repo — pure function
# ---------------------------------------------------------------------------

def test_extract_github_repo_from_full_url():
    assert extract_github_repo("https://github.com/owner/repo") == "owner/repo"


def test_extract_github_repo_from_bare_domain():
    assert extract_github_repo("github.com/foo/bar-baz is cool") == "foo/bar-baz"


def test_extract_github_repo_strips_trailing_slash():
    result = extract_github_repo("github.com/a/b/")
    assert result is not None
    assert result.endswith("b") or "/" not in result.rstrip("/")


def test_extract_github_repo_none():
    assert extract_github_repo("no github link here") is None


# ---------------------------------------------------------------------------
# fetch_github_card
# ---------------------------------------------------------------------------

def test_fetch_github_card_formats_correctly():
    repo_data = {
        "name": "myrepo",
        "stargazers_count": 1500,
        "forks_count": 200,
        "license": {"spdx_id": "MIT"},
        "description": "A great repo",
        "language": "Python",
        "topics": ["ml", "ai"],
        "html_url": "https://github.com/owner/myrepo",
    }
    readme_resp = MagicMock()
    readme_resp.status_code = 200
    readme_resp.text = "# My Repo\nThis is the readme content."

    repo_resp = MagicMock()
    repo_resp.json.return_value = repo_data
    repo_resp.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get = AsyncMock(side_effect=[repo_resp, readme_resp])

    async def _run():
        with patch("dailyinfo_fetcher.github_fetcher.httpx.AsyncClient", return_value=_async_client_ctx(client)), \
             patch("dailyinfo_fetcher.github_fetcher.GITHUB_TOKEN", ""):
            return await fetch_github_card("owner/myrepo")

    card = asyncio.run(_run())
    assert "myrepo" in card
    assert "1.5k" in card
    assert "MIT" in card
    assert "Python" in card
    assert "https://github.com/owner/myrepo" in card


def test_fetch_github_card_api_error_returns_fallback():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("API error"))

    async def _run():
        with patch("dailyinfo_fetcher.github_fetcher.httpx.AsyncClient", return_value=_async_client_ctx(client)):
            return await fetch_github_card("owner/repo")

    card = asyncio.run(_run())
    assert "owner/repo" in card
    assert "无法获取" in card
