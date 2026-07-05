"""Tests for ``scripts/zotero_sync.py`` — Zotero linked_file sync."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts/ is on sys.path for flat imports
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from download_pdf import PdfMetadata  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pdf_meta(title="Test Title", authors=None, doi="10.1234/test.1", pages=10) -> PdfMetadata:
    """Build a minimal PdfMetadata for testing."""
    return PdfMetadata(
        title=title,
        authors=authors or ["Smith, John"],
        doi=doi,
        pages=pages,
        version="1.7",
        file_size_bytes=12345,
    )


def _make_crossref_message():
    """Return a realistic Crossref API response for a journal article."""
    return {
        "title": ["Environmental modulation of storm–flood response"],
        "author": [
            {"given": "Alice", "family": "Brown"},
            {"given": "Bob", "family": "Chen"},
        ],
        "abstract": "<p>This paper investigates environmental modulation effects.</p>",
        "published-print": {"date-parts": [[2026, 5, 15]]},
        "type": "journal-article",
        "container-title": ["Journal of Hydrology"],
        "volume": "635",
        "issue": "3",
        "page": "135902",
        "ISSN": ["0022-1694"],
        "publisher": "Elsevier",
    }


# ---------------------------------------------------------------------------
# get_gdrive_papers_path
# ---------------------------------------------------------------------------

from zotero_sync import get_gdrive_papers_path  # noqa: E402


def test_get_gdrive_papers_path_returns_path(monkeypatch):
    """Returns a Path when GDRIVE_PAPERS_PATH is set."""
    monkeypatch.setenv("GDRIVE_PAPERS_PATH", "/home/user/GDrive/papers")
    result = get_gdrive_papers_path()
    assert result == Path("/home/user/GDrive/papers")


def test_get_gdrive_papers_path_raises_when_not_set(monkeypatch):
    """Raises ValueError when GDRIVE_PAPERS_PATH is not set."""
    monkeypatch.delenv("GDRIVE_PAPERS_PATH", raising=False)
    with pytest.raises(ValueError, match="GDRIVE_PAPERS_PATH must be set"):
        get_gdrive_papers_path()


def test_get_gdrive_papers_path_raises_when_empty(monkeypatch):
    """Raises ValueError when GDRIVE_PAPERS_PATH is empty string."""
    monkeypatch.setenv("GDRIVE_PAPERS_PATH", "")
    with pytest.raises(ValueError, match="GDRIVE_PAPERS_PATH must be set"):
        get_gdrive_papers_path()


# ---------------------------------------------------------------------------
# get_zotero_client
# ---------------------------------------------------------------------------

from zotero_sync import get_zotero_client  # noqa: E402


def test_get_zotero_client_raises_when_api_key_missing(monkeypatch):
    """Raises ValueError when ZOTERO_API_KEY is missing."""
    monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "12345")
    with pytest.raises(ValueError, match="ZOTERO_API_KEY"):
        get_zotero_client()


def test_get_zotero_client_raises_when_library_id_missing(monkeypatch):
    """Raises ValueError when ZOTERO_LIBRARY_ID is missing."""
    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.delenv("ZOTERO_LIBRARY_ID", raising=False)
    with pytest.raises(ValueError, match="ZOTERO_LIBRARY_ID"):
        get_zotero_client()


def test_get_zotero_client_returns_client(monkeypatch):
    """Returns a pyzotero Zotero client when env vars are set."""
    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "12345")
    client = get_zotero_client()
    assert client is not None
    assert client.library_id == "12345"


# ---------------------------------------------------------------------------
# _copy_to_gdrive
# ---------------------------------------------------------------------------

from zotero_sync import _copy_to_gdrive  # noqa: E402


def test_copy_to_gdrive_copies_file(tmp_path):
    """Copies a PDF to the GDrive directory with the given filename."""
    # Arrange
    src = tmp_path / "source.pdf"
    src.write_bytes(b"%PDF-1.4\nfake content")
    gdrive_dir = tmp_path / "gdrive"
    gdrive_dir.mkdir()

    # Act
    result = _copy_to_gdrive(src, gdrive_dir, "j.jhydrol.2024.132471.pdf")

    # Assert
    assert result.exists()
    assert result == gdrive_dir / "j.jhydrol.2024.132471.pdf"
    assert result.read_bytes() == b"%PDF-1.4\nfake content"
    # Source is still in place (copy, not move)
    assert src.exists()


def test_copy_to_gdrive_creates_parent_dirs(tmp_path):
    """Creates parent directories if they do not exist."""
    src = tmp_path / "source.pdf"
    src.write_bytes(b"content")
    gdrive_dir = tmp_path / "gdrive" / "subdir"

    result = _copy_to_gdrive(src, gdrive_dir, "paper.pdf")
    assert result.exists()
    assert result.parent == gdrive_dir


# ---------------------------------------------------------------------------
# _fetch_crossref_metadata
# ---------------------------------------------------------------------------

from zotero_sync import _fetch_crossref_metadata  # noqa: E402


def test_fetch_crossref_metadata_returns_message_on_success(monkeypatch):
    """Returns the message dict from a successful Crossref response."""
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.read.return_value = (
        b'{"status":"ok","message":{"title":["Test Paper"],"type":"journal-article"}}'
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: mock_resp)

    result = _fetch_crossref_metadata("10.1234/test.1")
    assert result is not None
    assert result["title"] == ["Test Paper"]
    assert result["type"] == "journal-article"


def test_fetch_crossref_metadata_returns_none_on_http_error(monkeypatch):
    """Returns None when the Crossref API call raises an exception."""
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **kw: (_ for _ in ()).throw(OSError("network down"))
    )

    result = _fetch_crossref_metadata("10.1234/test.1")
    assert result is None


def test_fetch_crossref_metadata_returns_none_on_json_decode_error(monkeypatch):
    """Returns None when the response body is not valid JSON."""
    mock_resp = MagicMock()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.read.return_value = b"not json"
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: mock_resp)

    result = _fetch_crossref_metadata("10.1234/test.1")
    assert result is None


# ---------------------------------------------------------------------------
# _build_zotero_item
# ---------------------------------------------------------------------------

from zotero_sync import _build_zotero_item  # noqa: E402


def test_build_zotero_item_from_crossref():
    """Builds a full Zotero item from Crossref metadata."""
    cr = _make_crossref_message()
    pdf = _make_pdf_meta()

    item, extra = _build_zotero_item("10.1234/test.1", cr, pdf)

    assert item["title"] == "Environmental modulation of storm–flood response"
    assert item["DOI"] == "10.1234/test.1"
    assert item["url"] == "https://doi.org/10.1234/test.1"
    assert item["itemType"] == "journalArticle"
    assert item["language"] == "en"
    assert item["date"] == "2026-05-15"
    assert item["abstractNote"] == "<p>This paper investigates environmental modulation effects.</p>"

    # Authors
    assert len(item["creators"]) == 2
    assert item["creators"][0] == {
        "creatorType": "author",
        "firstName": "Alice",
        "lastName": "Brown",
    }

    # Journal fields — now in item dict, not extra
    assert item["publicationTitle"] == "Journal of Hydrology"
    assert item["volume"] == "635"
    assert item["issue"] == "3"
    assert item["pages"] == "135902"
    assert item["ISSN"] == "0022-1694"
    assert item["publisher"] == "Elsevier"
    # extra dict is empty when all fields map to Zotero item fields
    assert extra == {}


def test_build_zotero_item_falls_back_to_pdf_metadata():
    """When Crossref data is None, uses PDF-embedded metadata."""
    pdf = _make_pdf_meta(title="A PDF-only paper", authors=["Doe, Jane"])

    item, extra = _build_zotero_item("10.1234/test.1", None, pdf)

    assert item["title"] == "A PDF-only paper"
    assert item["DOI"] == "10.1234/test.1"
    assert item["itemType"] == "journalArticle"
    assert len(item["creators"]) == 1
    # "Doe, Jane" rsplit(" ", 1) → firstName="Doe,", lastName="Jane"
    assert item["creators"][0]["lastName"] == "Jane"
    assert item["creators"][0]["firstName"] == "Doe,"
    # No extra fields when Crossref is unavailable
    assert extra == {}


def test_build_zotero_item_uses_pdf_title_when_crossref_title_empty():
    """Falls back to PDF title when Crossref title list is empty."""
    cr = {"title": [], "type": "journal-article"}
    pdf = _make_pdf_meta(title="Fallback Title")

    item, extra = _build_zotero_item("10.1234/test.1", cr, pdf)

    assert item["title"] == "Fallback Title"


def test_build_zotero_item_handles_date_parts_with_year_only():
    """Handles date-parts that only have a year (e.g. [[2026]])."""
    cr = {
        "title": ["Annual Report"],
        "issued": {"date-parts": [[2026]]},
        "type": "journal-article",
    }
    pdf = _make_pdf_meta()

    item, extra = _build_zotero_item("10.1234/test.1", cr, pdf)
    assert item["date"] == "2026"


def test_build_zotero_item_handles_missing_date():
    """No date field when Crossref has no date information."""
    cr = {"title": ["No-date paper"], "type": "journal-article"}
    pdf = _make_pdf_meta()

    item, extra = _build_zotero_item("10.1234/test.1", cr, pdf)
    assert "date" not in item


def test_build_zotero_item_handles_missing_optional_fields():
    """Builds a minimal item when Crossref has only title and type."""
    cr = {"title": ["Minimal paper"], "type": "book"}
    pdf = _make_pdf_meta()

    item, extra = _build_zotero_item("10.1234/test.1", cr, pdf)

    assert item["title"] == "Minimal paper"
    assert item["itemType"] == "journalArticle"  # non-journal-article → journalArticle default
    assert "abstractNote" not in item
    assert "date" not in item
    assert extra == {}


def test_build_zotero_item_with_preprint_type():
    """Maps Crossref 'journal-article' type to 'journalArticle'."""
    cr = {"title": ["Preprint test"], "type": "journal-article"}
    pdf = _make_pdf_meta()

    item, extra = _build_zotero_item("10.1234/test.1", cr, pdf)
    assert item["itemType"] == "journalArticle"


# ---------------------------------------------------------------------------
# _create_zotero_item
# ---------------------------------------------------------------------------

from zotero_sync import _create_zotero_item  # noqa: E402


def test_create_zotero_item_creates_parent_and_attachment():
    """Creates a parent item with a linked_file attachment."""
    # Arrange
    mock_zot = MagicMock()

    def _item_template(t, *args):
        return {"itemType": t, "title": "", "creators": []}

    mock_zot.item_template.side_effect = _item_template

    # Simulate successful parent creation
    parent_key = "ABC12345"
    mock_zot.create_items.side_effect = [
        {"success": {"0": parent_key}},  # parent item
        {"success": {"0": "XYZ98765"}},  # attachment
    ]

    item_data = {
        "title": "Test Paper",
        "DOI": "10.1234/test.1",
        "itemType": "journalArticle",
        "creators": [{"creatorType": "author", "firstName": "Alice", "lastName": "Brown"}],
        "date": "2026-05-15",
    }
    extra = {"publicationTitle": "Test Journal", "volume": "10"}

    # Act
    parent, attach = _create_zotero_item(mock_zot, item_data, extra, "test.pdf")

    # Assert
    assert parent == parent_key
    assert attach == "XYZ98765"

    # Verify the attachment was created with linked_file type
    attach_call = mock_zot.item_template.call_args_list[1]
    assert attach_call[0] == ("attachment", "linked_file")

    # Verify attachment path uses attachments: scheme
    attach_item = mock_zot.create_items.call_args_list[1][0][0][0]
    assert attach_item["path"] == "attachments:test.pdf"
    assert attach_item["parentItem"] == parent_key


def test_create_zotero_item_raises_on_parent_failure():
    """Raises RuntimeError when the Zotero API rejects the parent item."""
    mock_zot = MagicMock()
    mock_zot.item_template.return_value = {"itemType": "journalArticle"}
    mock_zot.create_items.return_value = {
        "success": {},
        "failed": {"0": {"message": "Title is required"}},
    }

    with pytest.raises(RuntimeError, match="Failed to create Zotero item"):
        _create_zotero_item(mock_zot, {"itemType": "journalArticle"}, {}, "test.pdf")


def test_create_zotero_item_attachment_failure_is_not_fatal():
    """Attachment key is empty string when attachment creation fails."""
    mock_zot = MagicMock()

    def _item_template(t, *args):
        return {"itemType": t}

    mock_zot.item_template.side_effect = _item_template
    mock_zot.create_items.side_effect = [
        {"success": {"0": "ABC12345"}},  # parent OK
        {"success": {}},  # attachment failed
    ]

    parent, attach = _create_zotero_item(mock_zot, {"itemType": "journalArticle"}, {}, "test.pdf")
    assert parent == "ABC12345"
    assert attach == ""
