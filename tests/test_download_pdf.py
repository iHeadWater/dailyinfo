"""Tests for ``scripts/download_pdf.py`` — PDF download helper."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure scripts/ is on sys.path for flat imports
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from download_pdf import (  # noqa: E402
    PdfMetadata,
    Publisher,
    _clean_base64,
    classify_input,
    decode_base64_file,
    default_pdf_dir,
    detect_publisher,
    doi_to_url,
    normalize_doi,
    output_path_for,
    pii_to_sciencedirect_url,
    slug_from_doi,
    verify_pdf,
)


# ---------------------------------------------------------------------------
# normalize_doi
# ---------------------------------------------------------------------------


def test_normalize_doi_bare():
    """Bare DOI is returned as-is."""
    assert normalize_doi("10.1016/j.jhydrol.2024.132471") == "10.1016/j.jhydrol.2024.132471"


def test_normalize_doi_https_prefix():
    """HTTPS DOI URL is stripped to bare DOI."""
    assert (
        normalize_doi("https://doi.org/10.1016/j.jhydrol.2024.132471")
        == "10.1016/j.jhydrol.2024.132471"
    )


def test_normalize_doi_prefix():
    """``doi:`` prefix is stripped."""
    assert (
        normalize_doi("doi:10.1016/j.jhydrol.2024.132471")
        == "10.1016/j.jhydrol.2024.132471"
    )


def test_normalize_doi_whitespace():
    """Leading/trailing whitespace is stripped."""
    assert (
        normalize_doi("  10.1016/j.jhydrol.2024.132471  ")
        == "10.1016/j.jhydrol.2024.132471"
    )


# ---------------------------------------------------------------------------
# detect_publisher
# ---------------------------------------------------------------------------


def test_detect_elsevier_sciencedirect():
    """ScienceDirect URLs return ELSEVIER."""
    assert (
        detect_publisher("https://www.sciencedirect.com/science/article/pii/S0022169424018675")
        == Publisher.ELSEVIER
    )


def test_detect_elsevier_elsevier():
    """elsevier.com URLs return ELSEVIER."""
    assert detect_publisher("https://linkinghub.elsevier.com/retrieve/pii/S0022169424018675") == Publisher.ELSEVIER


def test_detect_springer():
    """Springer URLs return SPRINGER."""
    assert detect_publisher("https://link.springer.com/article/10.1007/s11269-024-04000-0") == Publisher.SPRINGER


def test_detect_wiley():
    """Wiley URLs return WILEY."""
    assert (
        detect_publisher("https://onlinelibrary.wiley.com/doi/10.1029/2023WR036500")
        == Publisher.WILEY
    )


def test_detect_taylor_francis():
    """Taylor & Francis URLs return TAYLOR_FRANCIS."""
    assert (
        detect_publisher("https://www.tandfonline.com/doi/full/10.1080/02626667.2024.1234567")
        == Publisher.TAYLOR_FRANCIS
    )


def test_detect_agu():
    """AGU URLs (on wiley.com) return AGU."""
    assert (
        detect_publisher("https://agupubs.onlinelibrary.wiley.com/doi/10.1029/2023WR036500")
        == Publisher.AGU
    )


def test_detect_unknown():
    """Unrecognized URL returns UNKNOWN."""
    assert detect_publisher("https://example.com/paper.pdf") == Publisher.UNKNOWN


# ---------------------------------------------------------------------------
# classify_input
# ---------------------------------------------------------------------------


def test_classify_bare_doi():
    """Bare DOI is classified as 'doi'."""
    result = classify_input("10.1016/j.jhydrol.2024.132471")
    assert result["type"] == "doi"
    assert result["normalized"] == "10.1016/j.jhydrol.2024.132471"
    assert "doi.org" in result["url"]


def test_classify_doi_url():
    """Full doi.org URL is classified as 'doi'."""
    result = classify_input("https://doi.org/10.1016/j.jhydrol.2024.132471")
    assert result["type"] == "doi"
    assert result["normalized"] == "10.1016/j.jhydrol.2024.132471"


def test_classify_pii():
    """PII string is classified as 'pii'."""
    result = classify_input("S0022169424018675")
    assert result["type"] == "pii"
    assert result["normalized"] == "S0022169424018675"
    assert result["url"] == pii_to_sciencedirect_url("S0022169424018675")


def test_classify_url():
    """Full URL is classified as 'url'."""
    url = "https://www.sciencedirect.com/science/article/pii/S0022169424018675"
    result = classify_input(url)
    assert result["type"] == "url"
    assert result["url"] == url


def test_classify_unknown():
    """Unrecognizable input is classified as 'unknown'."""
    result = classify_input("random text not a doi or url")
    assert result["type"] == "unknown"
    assert result["url"] == ""


def test_classify_pii_case_insensitive():
    """PII detection is case-insensitive."""
    result = classify_input("s0022169424018675")
    assert result["type"] == "pii"
    assert result["normalized"] == "S0022169424018675"


# ---------------------------------------------------------------------------
# _clean_base64
# ---------------------------------------------------------------------------


def test_clean_base64_plain():
    """Plain base64 string passes through unchanged."""
    b64 = "JVBERi0xLjcKJYGBgYEKCjEgMCBvYmo="
    assert _clean_base64(b64) == b64


def test_clean_base64_data_url():
    """Data URL prefix is stripped."""
    raw = "data:application/pdf;base64,JVBERi0xLjcKJYGBgYEKCjEgMCBvYmo="
    expected = "JVBERi0xLjcKJYGBgYEKCjEgMCBvYmo="
    assert _clean_base64(raw) == expected


def test_clean_base64_with_whitespace():
    """Whitespace and newlines are stripped."""
    raw = "JVBERi0xLjcK\nJYGBgYEKCjEg MCBvYmo=\n"
    expected = "JVBERi0xLjcKJYGBgYEKCjEgMCBvYmo="
    assert _clean_base64(raw) == expected


def test_clean_base64_non_ascii_artifacts():
    """Non-base64 characters are removed."""
    raw = "JVBERi0xLjcK\xff\xfeJYGBgYEKCjEgMCBvYmo="
    expected = "JVBERi0xLjcKJYGBgYEKCjEgMCBvYmo="
    assert _clean_base64(raw) == expected


def test_clean_base64_json_wrapped():
    """JSON-wrapped base64 is extracted."""
    data = {"size": 100, "base64": "data:application/pdf;base64,JVBERi0xLjcK"}
    raw = json.dumps(data)
    expected = "JVBERi0xLjcK"
    assert _clean_base64(raw) == expected


# ---------------------------------------------------------------------------
# decode_base64_file
# ---------------------------------------------------------------------------


def test_decode_base64_json_file(tmp_path):
    """JSON-wrapped base64 decodes to the correct binary content."""
    original = b"hello pdf content here"
    b64 = base64.b64encode(original).decode("ascii")
    data = json.dumps({"size": len(original), "base64": f"data:application/pdf;base64,{b64}"})

    input_file = tmp_path / "input.json"
    input_file.write_text(data, encoding="utf-8")
    output_file = tmp_path / "output.bin"

    size = decode_base64_file(input_file, output_file)
    assert size == len(original)
    assert output_file.read_bytes() == original


def test_decode_base64_plain_file(tmp_path):
    """Plain base64 file decodes correctly."""
    original = b"plain pdf content"
    b64 = base64.b64encode(original).decode("ascii")

    input_file = tmp_path / "input.txt"
    input_file.write_text(b64, encoding="utf-8")
    output_file = tmp_path / "output.bin"

    size = decode_base64_file(input_file, output_file)
    assert size == len(original)
    assert output_file.read_bytes() == original


# ---------------------------------------------------------------------------
# verify_pdf
# ---------------------------------------------------------------------------


_MINIMAL_PDF = (
    b"%PDF-1.7\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
    b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF"
)


def test_verify_valid_pdf(tmp_path):
    """Valid PDF returns metadata with correct page count."""
    pdf_path = tmp_path / "valid.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)

    meta = verify_pdf(pdf_path)
    assert meta.version == "1.7"
    assert meta.pages == 1
    assert meta.file_size_bytes == len(_MINIMAL_PDF)


def test_verify_pdf_with_title_and_author(tmp_path):
    """PDF with /Title and /Author metadata extracts them."""
    pdf_with_meta = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        b"4 0 obj\n<< /Title (Test Paper Title) /Author (Smith; Jones) >>\nendobj\n"
        b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000170 00000 n \n"
        b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n260\n%%EOF"
    )
    pdf_path = tmp_path / "meta.pdf"
    pdf_path.write_bytes(pdf_with_meta)

    meta = verify_pdf(pdf_path)
    assert meta.title == "Test Paper Title"
    assert meta.authors == ["Smith", "Jones"]


def test_verify_pdf_with_doi(tmp_path):
    """PDF with /doi metadata extracts the DOI."""
    pdf_with_doi = (
        b"%PDF-1.7\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        b"4 0 obj\n<< /doi (10.1016/j.jhydrol.2024.132471) >>\nendobj\n"
        b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n0000000170 00000 n \n"
        b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n260\n%%EOF"
    )
    pdf_path = tmp_path / "doi.pdf"
    pdf_path.write_bytes(pdf_with_doi)

    meta = verify_pdf(pdf_path)
    assert meta.doi == "10.1016/j.jhydrol.2024.132471"


def test_verify_nonexistent_file():
    """Missing file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        verify_pdf("/nonexistent/path.pdf")


def test_verify_empty_file(tmp_path):
    """Empty file raises ValueError."""
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")

    with pytest.raises(ValueError, match="empty"):
        verify_pdf(empty)


def test_verify_not_a_pdf(tmp_path):
    """Non-PDF file raises ValueError."""
    not_pdf = tmp_path / "not_pdf.txt"
    not_pdf.write_text("Hello, I am not a PDF", encoding="utf-8")

    with pytest.raises(ValueError, match="Not a valid PDF"):
        verify_pdf(not_pdf)


# ---------------------------------------------------------------------------
# PdfMetadata
# ---------------------------------------------------------------------------


def test_pdf_metadata_defaults():
    """New PdfMetadata has empty defaults."""
    meta = PdfMetadata()
    assert meta.title == ""
    assert meta.authors == []
    assert meta.doi == ""
    assert meta.pages == 0


def test_pdf_metadata_as_dict():
    """as_dict serializes all fields."""
    meta = PdfMetadata(
        title="Test",
        authors=["Alice", "Bob"],
        doi="10.1000/test",
        pages=5,
        version="1.7",
        file_size_bytes=1024,
    )
    d = meta.as_dict()
    assert d["title"] == "Test"
    assert d["authors"] == ["Alice", "Bob"]
    assert d["doi"] == "10.1000/test"
    assert d["pages"] == 5
    assert d["version"] == "1.7"
    assert d["file_size_bytes"] == 1024


# ---------------------------------------------------------------------------
# URL constructors
# ---------------------------------------------------------------------------


def test_doi_to_url():
    """DOI is converted to the correct resolver URL."""
    assert doi_to_url("10.1016/j.jhydrol.2024.132471") == "https://doi.org/10.1016/j.jhydrol.2024.132471"


def test_pii_to_sciencedirect_url():
    """PII is converted to the correct ScienceDirect URL."""
    assert (
        pii_to_sciencedirect_url("S0022169424018675")
        == "https://www.sciencedirect.com/science/article/pii/S0022169424018675"
    )


# ---------------------------------------------------------------------------
# Output path resolution
# ---------------------------------------------------------------------------


def test_slug_from_doi_elsevier():
    """Elsevier DOI produces a clean slug."""
    slug = slug_from_doi("10.1016/j.jhydrol.2024.132471")
    assert slug == "j.jhydrol.2024.132471"


def test_slug_from_doi_special_chars():
    """Hyphens are preserved in slugs; truly problematic chars become underscores."""
    slug = slug_from_doi("10.1007/s11269-024-04000-0")
    assert slug == "s11269-024-04000-0"


def test_slug_from_doi_slashes_replaced():
    """Slash-separated components are collapsed."""
    slug = slug_from_doi("10.1080/02626667.2024.1234567")
    assert slug == "02626667.2024.1234567"


def test_slug_from_doi_with_prefix():
    """DOI with https prefix is normalized before slugification."""
    slug = slug_from_doi("https://doi.org/10.1029/2023WR036500")
    assert slug == "2023WR036500"


def test_default_pdf_dir_respects_env(monkeypatch, tmp_path):
    """DAILYINFO_DATA_ROOT overrides the default papers directory."""
    custom = tmp_path / "custom-data"
    monkeypatch.setenv("DAILYINFO_DATA_ROOT", str(custom))
    assert default_pdf_dir() == custom / "papers"


def test_default_pdf_dir_uses_home_when_no_env(monkeypatch):
    """Without DAILYINFO_DATA_ROOT, uses ~/.myagentdata/dailyinfo/papers."""
    monkeypatch.delenv("DAILYINFO_DATA_ROOT", raising=False)
    expected = Path.home() / ".myagentdata" / "dailyinfo" / "papers"
    assert default_pdf_dir() == expected


def test_output_path_for_doi():
    """DOI input generates a slug-based filename."""
    path = output_path_for("10.1016/j.jhydrol.2024.132471")
    assert path.name == "j.jhydrol.2024.132471.pdf"
    assert str(default_pdf_dir()) in str(path)


def test_output_path_for_pii():
    """PII input generates a slug-based filename."""
    path = output_path_for("S0022169424018675")
    assert path.name == "S0022169424018675.pdf"


def test_output_path_for_with_output_dir(tmp_path):
    """Custom output_dir overrides the default."""
    path = output_path_for("10.1016/j.jhydrol.2024.132471", output_dir=tmp_path)
    assert path.parent == tmp_path
    assert path.name == "j.jhydrol.2024.132471.pdf"
