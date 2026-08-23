"""Tests for caption-first OpenReview architecture figure extraction."""

from pathlib import Path
import sys

import pymupdf as fitz
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from conference_figures import (  # noqa: E402
    FigureDownloadError,
    caption_score,
    download_pdf,
    extract_architecture_figure,
    normalize_pdf_url,
    pdf_url_candidates,
    pdf_sha256,
    write_cached_extraction,
)


def _pdf_with_architecture_caption(caption: str = "Figure 1: Overall architecture of the proposed framework.") -> bytes:
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    for x in (150, 300, 450):
        page.draw_rect(
            fitz.Rect(x, 220, x + 80, 270),
            color=(0, 0, 0),
            fill=(0.9, 0.9, 0.9),
        )
    page.draw_line((230, 245), (300, 245), color=(0, 0, 0))
    page.draw_line((380, 245), (450, 245), color=(0, 0, 0))
    page.insert_text((165, 250), "Encoder")
    page.insert_text((315, 250), "Fusion")
    page.insert_text((460, 250), "Decoder")
    page.insert_textbox(fitz.Rect(50, 300, 560, 350), caption, fontsize=10)
    value = document.tobytes()
    document.close()
    return value


def test_caption_scoring_prefers_architecture_and_rejects_results():
    assert caption_score("Figure 1: Overall architecture of the proposed framework") >= 9
    assert caption_score(
        "Figure 1: Overall framework of our proposed approach evaluated on datasets"
    ) >= 4
    assert caption_score("Figure 2: Ablation performance comparison") < 0
    assert caption_score("Figure 1: SmartPark system architecture workflow") < 4
    assert caption_score("Figure 1: Evaluation framework for benchmark testing") < 4


def test_normalize_pdf_url_requires_openreview_https():
    assert normalize_pdf_url("/pdf?id=abc") == "https://openreview.net/pdf?id=abc"
    assert normalize_pdf_url("", note_id="abc") == "https://api2.openreview.net/pdf?id=abc"
    with pytest.raises(FigureDownloadError):
        normalize_pdf_url("http://example.com/paper.pdf")


def test_pdf_url_candidates_include_attachment_api_fallback():
    candidates = pdf_url_candidates(
        "https://openreview.net/pdf/hash.pdf", note_id="note-1"
    )
    assert candidates[0].endswith("/pdf/hash.pdf")
    assert "https://api2.openreview.net/attachment?id=note-1&name=pdf" in candidates
    assert "https://api2.openreview.net/pdf?id=note-1" in candidates


def test_pdf_url_candidates_do_not_turn_acl_pdf_into_openreview_fallback():
    candidates = pdf_url_candidates(
        "https://aclanthology.org/2026.acl-long.1.pdf", note_id="acl:paper"
    )
    assert candidates == ["https://aclanthology.org/2026.acl-long.1.pdf"]


def test_download_pdf_falls_back_after_web_challenge(monkeypatch):
    calls = []

    class Response:
        headers = {"Content-Length": "6"}

        def __init__(self, url, status_code, body=b"%PDF-1"):
            self.url = url
            self.status_code = status_code
            self.body = body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise __import__("requests").HTTPError(f"HTTP {self.status_code}")

        def iter_content(self, chunk_size):
            return [self.body]

        def close(self):
            return None

    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        if len(calls) == 1:
            return Response(url, 403, b"challenge")
        return Response(url, 200)

    monkeypatch.setattr("conference_figures.requests.get", get)
    monkeypatch.setattr("conference_figures.time.sleep", lambda _seconds: None)
    data = download_pdf(
        "https://openreview.net/pdf/hash.pdf",
        note_id="note-1",
        headers={"Authorization": "Bearer test"},
    )
    assert data == b"%PDF-1"
    assert calls[0][0].endswith("/pdf/hash.pdf")
    assert calls[1][0] == "https://api2.openreview.net/attachment?id=note-1&name=pdf"
    assert calls[1][1]["Authorization"] == "Bearer test"


def test_download_pdf_does_not_forward_openreview_auth_to_external_host(monkeypatch):
    calls = []

    class Response:
        url = "https://arxiv.org/pdf/2608.00001"
        status_code = 200
        headers = {"Content-Length": "6"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            return [b"%PDF-1"]

        def close(self):
            return None

    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        return Response()

    monkeypatch.setattr("conference_figures.requests.get", get)

    data = download_pdf(
        "https://arxiv.org/pdf/2608.00001",
        headers={"Authorization": "Bearer openreview-secret"},
    )

    assert data == b"%PDF-1"
    assert calls[0][0] == "https://arxiv.org/pdf/2608.00001"
    assert "Authorization" not in calls[0][1]


def test_extracts_vector_architecture_above_caption():
    result = extract_architecture_figure(_pdf_with_architecture_caption())
    assert result.status == "READY"
    assert result.manifest["figure_id"] == "fig1"
    assert result.manifest["side"] == "above"
    assert result.manifest["candidate_count"] == 2
    assert result.image_bytes and result.image_bytes.startswith(b"\x89PNG")


def test_extract_returns_no_figure_for_result_caption():
    result = extract_architecture_figure(
        _pdf_with_architecture_caption("Figure 1: Ablation performance comparison.")
    )
    assert result.status == "NO_FIGURE"
    assert result.image_bytes is None


def test_caption_reviewer_rescues_low_score_caption():
    reviewed = []

    def reviewer(captions):
        reviewed.append(captions)
        return {captions[0]["index"]}

    result = extract_architecture_figure(
        _pdf_with_architecture_caption("Figure 1: Proposed decoder."),
        caption_reviewer=reviewer,
        review_score_below=7,
    )

    assert result.status == "READY"
    assert result.manifest["caption_reviewed"] is True
    assert len(reviewed) == 1
    assert reviewed[0][0]["score"] < 4


def test_caption_reviewer_rejects_low_score_false_positive():
    result = extract_architecture_figure(
        _pdf_with_architecture_caption("Figure 1: Proposed decoder."),
        caption_reviewer=lambda _captions: set(),
    )

    assert result.status == "NO_FIGURE"
    assert result.manifest["review_attempted"] is True


def test_high_confidence_caption_skips_reviewer():
    def reviewer(_captions):
        raise AssertionError("high-confidence captions must not call the model")

    result = extract_architecture_figure(
        _pdf_with_architecture_caption(),
        caption_reviewer=reviewer,
        review_score_below=7,
    )

    assert result.status == "READY"
    assert result.manifest["caption_reviewed"] is False


def test_vision_reviewer_can_reject_text_reviewer_candidate():
    seen = []

    def text_reviewer(captions):
        return {captions[0]["index"]}

    def vision_reviewer(items):
        seen.extend(items)
        return set()

    result = extract_architecture_figure(
        _pdf_with_architecture_caption("Figure 1: Proposed decoder."),
        caption_reviewer=text_reviewer,
        vision_reviewer=vision_reviewer,
    )

    assert result.status == "NO_FIGURE"
    assert seen and seen[0]["image_bytes"].startswith(b"\x89PNG")


def test_download_pdf_checks_magic_and_size(monkeypatch):
    class Response:
        url = "https://openreview.net/pdf?id=abc"
        headers = {"Content-Length": "5"}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            return [b"%PDF-1"]

        def close(self):
            return None

    monkeypatch.setattr("conference_figures.requests.get", lambda *a, **k: Response())
    assert download_pdf("https://openreview.net/pdf?id=abc") == b"%PDF-1"


def test_write_cached_extraction_uses_pdf_hash(tmp_path):
    pdf = _pdf_with_architecture_caption()
    result = extract_architecture_figure(pdf)
    digest = pdf_sha256(pdf)
    manifest = write_cached_extraction(
        result,
        assets_root=tmp_path / "assets",
        source="openreview_test",
        forum_id="forum-1",
        pdf_hash=digest,
    )
    image_path = Path(manifest["path"])
    assert image_path == tmp_path / "assets" / "conference" / "openreview_test" / "forum-1" / digest / "hero.png"
    assert image_path.is_file()
    assert image_path.with_name("manifest.json").is_file()
