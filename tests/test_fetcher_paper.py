"""Tests for dailyinfo_fetcher.paper_fetcher (mocked network)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from dailyinfo_fetcher.paper_fetcher import (
    _safe_filename,
    _try_arxiv_api,
    fetch_paper_oa,
    lookup_crossref,
)

_EMPTY_CROSSREF = {"doi": "", "publisher": "", "journal": "", "issn": "", "pdf_url": ""}


def _async_client_ctx(client_mock):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client_mock)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# _safe_filename — pure function
# ---------------------------------------------------------------------------

def test_safe_filename_basic():
    name = _safe_filename("Attention Is All You Need")
    assert name.endswith(".pdf")
    assert " " not in name


def test_safe_filename_strips_special_chars():
    name = _safe_filename("Test: Paper (2024)")
    assert ":" not in name
    assert name.endswith(".pdf")


def test_safe_filename_truncates_long_title():
    name = _safe_filename("A" * 200)
    assert len(name) <= 85  # 80 chars base + ".pdf" + possible underscores


# ---------------------------------------------------------------------------
# _try_arxiv_api
# ---------------------------------------------------------------------------

def test_try_arxiv_api_found():
    xml = (
        "<feed><entry>"
        "<id>https://arxiv.org/abs/1706.03762v5</id>"
        "</entry></feed>"
    )
    resp = MagicMock()
    resp.text = xml
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)

    async def _run():
        with patch("dailyinfo_fetcher.paper_fetcher._client", return_value=_async_client_ctx(client)):
            return await _try_arxiv_api("Attention Is All You Need")

    url = asyncio.run(_run())
    assert url is not None
    assert "arxiv.org/pdf" in url


def test_try_arxiv_api_not_found():
    resp = MagicMock()
    resp.text = "<feed></feed>"
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)

    async def _run():
        with patch("dailyinfo_fetcher.paper_fetcher._client", return_value=_async_client_ctx(client)):
            return await _try_arxiv_api("Unknown Title XYZ 99999")

    url = asyncio.run(_run())
    assert url is None


def test_try_arxiv_api_error_returns_none():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("network error"))

    async def _run():
        with patch("dailyinfo_fetcher.paper_fetcher._client", return_value=_async_client_ctx(client)):
            return await _try_arxiv_api("Some Paper")

    url = asyncio.run(_run())
    assert url is None


# ---------------------------------------------------------------------------
# lookup_crossref
# ---------------------------------------------------------------------------

def test_lookup_crossref_ok():
    api_resp = {
        "message": {
            "items": [{
                "DOI": "10.1234/test",
                "publisher": "Test Publisher",
                "container-title": ["Test Journal"],
                "ISSN": ["1234-5678"],
                "link": [{"content-type": "application/pdf", "URL": "https://pub.com/paper.pdf"}],
            }]
        }
    }
    resp = MagicMock()
    resp.json.return_value = api_resp
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)

    async def _run():
        with patch("dailyinfo_fetcher.paper_fetcher._client", return_value=_async_client_ctx(client)):
            return await lookup_crossref("Some Paper Title")

    meta = asyncio.run(_run())
    assert meta["doi"] == "10.1234/test"
    assert meta["publisher"] == "Test Publisher"
    assert meta["journal"] == "Test Journal"
    assert meta["pdf_url"] == "https://pub.com/paper.pdf"


def test_lookup_crossref_empty_items():
    resp = MagicMock()
    resp.json.return_value = {"message": {"items": []}}
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)

    async def _run():
        with patch("dailyinfo_fetcher.paper_fetcher._client", return_value=_async_client_ctx(client)):
            return await lookup_crossref("Unknown Paper")

    meta = asyncio.run(_run())
    assert meta["doi"] == ""
    assert meta["pdf_url"] == ""


# ---------------------------------------------------------------------------
# fetch_paper_oa — all sources fail → returns (None, "未能获取")
# ---------------------------------------------------------------------------

def test_fetch_paper_oa_all_sources_fail(tmp_path):
    async def _run():
        with patch("dailyinfo_fetcher.paper_fetcher._try_arxiv_api", return_value=None), \
             patch("dailyinfo_fetcher.paper_fetcher._try_unpaywall", return_value=None), \
             patch("dailyinfo_fetcher.paper_fetcher._try_semantic_scholar", return_value=None), \
             patch("dailyinfo_fetcher.paper_fetcher._try_crossref_oa", return_value=None), \
             patch("dailyinfo_fetcher.paper_fetcher.lookup_crossref", return_value=_EMPTY_CROSSREF), \
             patch("dailyinfo_fetcher.paper_fetcher._try_pubmed_central", return_value=None), \
             patch("dailyinfo_fetcher.paper_fetcher.DOWNLOAD_DIR", tmp_path):
            return await fetch_paper_oa("Unknown Paper Title XYZ")

    path, source = asyncio.run(_run())
    assert path is None
    assert source == "未能获取"
