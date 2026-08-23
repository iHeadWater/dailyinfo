"""Extract a representative architecture figure from an OpenReview PDF.

This module intentionally stays small and deterministic.  It is a display
enrichment for Pipeline 6, not a general-purpose PDF library: the caller
downloads a public PDF, passes its bytes here, and keeps only the derived
figure/manifest. Caption scoring is lexical by default; an optional callback
can review only low-confidence captions without changing the default path.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urlparse

import requests


DEFAULT_ALLOWED_HOSTS = {
    "openreview.net",
    "api2.openreview.net",
    # Some OpenReview Notes store a public arXiv mirror in the pdf field.
    "arxiv.org",
    "export.arxiv.org",
    # Public proceedings hosts used by the non-OpenReview providers.
    "aclanthology.org",
    "www.aclanthology.org",
    "openaccess.thecvf.com",
    "papers.nips.cc",
}
OPENREVIEW_AUTH_HOSTS = {"openreview.net", "api2.openreview.net"}
DEFAULT_MAX_PDF_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = (10.0, 45.0)
EXTRACTOR_VERSION = "caption-v4"

# The reviewer is deliberately an optional callback.  Keeping the model call
# outside this module preserves a deterministic caption-only default and makes
# the extractor easy to test without credentials or network access.
CaptionReviewer = Callable[[list[dict[str, Any]]], set[int] | None]
VisionReviewer = Callable[[list[dict[str, Any]]], set[int] | None]

_CAPTION_RE = re.compile(
    r"(?im)^\s*(?:figure|fig(?:ure)?\.?)[ \t]*(?P<number>\d+[a-z]?)\s*[:.\-]"
)
_POSITIVE_TERMS: tuple[tuple[str, int], ...] = (
    ("architecture", 5),
    ("framework", 4),
    ("overview", 3),
    ("pipeline", 3),
    ("workflow", 3),
    ("network", 3),
    ("encoder", 2),
    ("decoder", 2),
    ("module", 2),
    ("algorithm", 2),
    ("mechanism", 2),
    ("illustration", 2),
    ("design", 2),
    ("method", 2),
    ("proposed", 1),
    ("model", 1),
    ("system", 1),
)
_POSITIVE_PHRASES: tuple[tuple[str, int], ...] = (
    ("overall framework", 4),
    ("overall architecture", 4),
    ("model architecture", 4),
    ("proposed approach", 3),
)
_NEGATIVE_TERMS: tuple[tuple[str, int], ...] = (
    ("ablation", 6),
    ("comparison", 5),
    ("performance", 4),
    ("accuracy", 4),
    ("loss", 3),
    ("dataset", 3),
    ("visualization", 3),
    ("qualitative", 3),
    ("different", 2),
    ("sota", 2),
    ("baseline", 4),
    ("error", 2),
)
_NEGATIVE_PHRASES: tuple[tuple[str, int], ...] = (
    ("system architecture", 8),
    ("evaluation framework", 8),
    ("testing framework", 8),
    ("data pipeline", 6),
    ("data collection", 5),
    ("dataset construction", 5),
    ("benchmark framework", 6),
)


class FigureExtractionError(RuntimeError):
    """Base exception for a single PDF figure extraction."""


class FigureDownloadError(FigureExtractionError):
    """Raised when the public OpenReview PDF cannot be downloaded safely."""


@dataclass(frozen=True)
class FigureCandidate:
    entry_index: int
    figure_id: str
    page: int
    caption: str
    caption_bbox: tuple[float, float, float, float]
    bbox: tuple[float, float, float, float]
    score: int
    side: str
    has_graphics: bool
    image_bytes: bytes


@dataclass(frozen=True)
class FigureExtraction:
    """Serializable extraction result plus the derived image bytes."""

    status: str
    manifest: dict[str, Any]
    image_bytes: bytes | None = None


def _host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    host = (host or "").casefold().rstrip(".")
    return host in {str(item).casefold().rstrip(".") for item in allowed_hosts}


def normalize_pdf_url(
    value: str | None,
    *,
    note_id: str = "",
    allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS,
) -> str:
    """Return a safe absolute OpenReview PDF URL.

    A missing PDF field falls back to the API-v2 ``/pdf?id=`` endpoint.  We
    reject non-HTTPS or non-OpenReview URLs because Note content is external
    input and must not become an SSRF primitive.
    """

    raw = str(value or "").strip()
    if not raw and note_id:
        raw = f"https://api2.openreview.net/pdf?id={note_id}"
    if raw.startswith("/"):
        raw = f"https://openreview.net{raw}"
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not _host_allowed(parsed.hostname or "", allowed_hosts):
        raise FigureDownloadError(f"unsupported PDF URL: {raw!r}")
    return raw


def pdf_url_candidates(
    value: str | None,
    *,
    note_id: str = "",
    allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS,
) -> list[str]:
    """Build an ordered set of safe PDF endpoints for one OpenReview Note.

    OpenReview currently exposes the same attachment through several routes.
    The web-facing ``/pdf/<hash>.pdf`` route can be protected by a browser
    challenge even when the API attachment route is available, so the latter
    is deliberately tried as a fallback.  Explicit external URLs (for
    example an arXiv mirror in the Note's ``pdf`` field) are retained.
    """

    raw = str(value or "").strip()
    candidates: list[str] = []

    def add(url: str) -> None:
        if url and url not in candidates:
            candidates.append(url)

    if raw:
        add(normalize_pdf_url(raw, note_id=note_id, allowed_hosts=allowed_hosts))
    raw_host = (urlparse(raw).hostname or "").casefold() if raw else ""
    openreview_raw = raw_host in {"openreview.net", "api2.openreview.net"}
    if note_id and (not raw or openreview_raw):
        # The API attachment endpoint is the canonical openreview-py fallback.
        add(
            "https://api2.openreview.net/attachment?"
            + urlencode({"id": note_id, "name": "pdf"})
        )
        add(
            "https://api2.openreview.net/pdf?"
            + urlencode({"id": note_id})
        )
        add("https://openreview.net/pdf?" + urlencode({"id": note_id}))
    if not candidates:
        raise FigureDownloadError("OpenReview PDF URL and note id are both missing")
    return candidates


def download_pdf(
    url: str,
    *,
    note_id: str = "",
    session: requests.Session | Any = requests,
    max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Download a PDF with endpoint fallback, auth-header support and checks."""

    candidates = pdf_url_candidates(
        url, note_id=note_id, allowed_hosts=allowed_hosts
    )
    errors: list[str] = []
    base_headers = {
        "User-Agent": (
            "DailyInfo/0.2 (public OpenReview figure enrichment; "
            "https://github.com/dailyinfo)"
        ),
        "Accept": "application/pdf,*/*;q=0.8",
    }
    for candidate in candidates:
        candidate_headers = dict(base_headers)
        candidate_host = (urlparse(candidate).hostname or "").casefold().rstrip(".")
        # ``headers`` comes from the authenticated OpenReview client and may
        # contain a Bearer token. Never forward that client context to arXiv,
        # proceedings sites, DOI resolvers, or other third-party PDF hosts.
        if headers and candidate_host in OPENREVIEW_AUTH_HOSTS:
            candidate_headers.update(headers)
            # Never let a JSON-only client header prevent PDF negotiation.
            candidate_headers["Accept"] = "application/pdf,*/*;q=0.8"
        for attempt, delay in enumerate((0, 2, 5), start=1):
            if delay:
                time.sleep(delay)
            response = None
            try:
                response = session.get(
                    candidate,
                    stream=True,
                    timeout=timeout,
                    headers=candidate_headers,
                )
                status = int(getattr(response, "status_code", 200))
                if status in {403, 404}:
                    errors.append(f"{candidate}: HTTP {status}")
                    break
                if status == 429 or status >= 500:
                    if attempt < 3:
                        continue
                response.raise_for_status()

                final_url = str(getattr(response, "url", candidate) or candidate)
                final = urlparse(final_url)
                if final.scheme != "https" or not _host_allowed(
                    final.hostname or "", allowed_hosts
                ):
                    raise FigureDownloadError(
                        f"redirected outside allowed PDF hosts: {final_url!r}"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise FigureDownloadError(
                        "OpenReview PDF exceeds configured size limit"
                    )

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise FigureDownloadError(
                            "OpenReview PDF exceeds configured size limit"
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not data.startswith(b"%PDF-"):
                    raise FigureDownloadError("response is not a PDF")
                return data
            except FigureDownloadError as exc:
                errors.append(f"{candidate}: {exc}")
                break
            except requests.RequestException as exc:
                errors.append(f"{candidate}: {exc}")
                if attempt == 3:
                    break
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

    detail = "; ".join(errors[-4:])
    raise FigureDownloadError(f"PDF request failed after endpoint fallback: {detail}")


def pdf_sha256(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


def caption_score(caption: str) -> int:
    """Score a caption for a main model architecture figure."""

    text = re.sub(r"\s+", " ", caption.casefold())
    score = 0
    for term, weight in _POSITIVE_TERMS:
        if re.search(rf"(?<!\w){re.escape(term)}(?:s)?(?!\w)", text):
            score += weight
    for phrase, weight in _POSITIVE_PHRASES:
        if phrase in text:
            score += weight
    for term, weight in _NEGATIVE_TERMS:
        if re.search(rf"(?<!\w){re.escape(term)}(?:s)?(?!\w)", text):
            score -= weight
    for phrase, weight in _NEGATIVE_PHRASES:
        if phrase in text:
            score -= weight
    return score


def _clip_tuple(rect: Any) -> tuple[float, float, float, float]:
    return tuple(round(float(value), 2) for value in (rect.x0, rect.y0, rect.x1, rect.y1))


def _overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _candidate_window(
    page: Any,
    caption_bbox: tuple[float, float, float, float],
    side: str,
    caption_text: str = "",
) -> Any:
    """Build a bounded column/full-width crop around a caption.

    Captions in two-column papers are kept in their column.  The bounded
    window is intentionally conservative; graphics and text blocks inside the
    window are used to tighten it when PyMuPDF exposes them.
    """

    import pymupdf as fitz  # type: ignore

    page_rect = page.rect
    x0, y0, x1, y1 = caption_bbox
    full_width = (x1 - x0) >= page_rect.width * 0.58 or (
        x0 <= page_rect.x0 + page_rect.width * 0.12
        and len(caption_text) >= 80
    )
    if not full_width:
        # Many two-column papers use a narrow centered caption for a
        # full-width figure.  Prefer the graphic's actual extent when an
        # image/vector object clearly crosses both columns.
        try:
            for image in page.get_images(full=True):
                for graphic in page.get_image_rects(image[0]):
                    crosses_page = (
                        graphic.x0 <= page_rect.x0 + page_rect.width * 0.20
                        and graphic.x1 >= page_rect.x1 - page_rect.width * 0.20
                    )
                    on_side = (
                        graphic.y1 <= y0 if side == "above" else graphic.y0 >= y1
                    )
                    if crosses_page and on_side:
                        full_width = True
                        break
                if full_width:
                    break
        except Exception:
            pass
        if not full_width:
            try:
                for drawing in page.get_drawings():
                    graphic = drawing.get("rect")
                    if graphic is None:
                        continue
                    crosses_page = (
                        graphic.x0 <= page_rect.x0 + page_rect.width * 0.20
                        and graphic.x1 >= page_rect.x1 - page_rect.width * 0.20
                    )
                    on_side = (
                        graphic.y1 <= y0 if side == "above" else graphic.y0 >= y1
                    )
                    if crosses_page and on_side:
                        full_width = True
                        break
            except Exception:
                pass
    if full_width:
        left, right = page_rect.x0 + 8, page_rect.x1 - 8
    else:
        left, right = max(page_rect.x0, x0 - 12), min(page_rect.x1, x1 + 12)
    window_height = min(page_rect.height * 0.68, max(page_rect.height * 0.28, 330))
    if side == "above":
        top, bottom = max(page_rect.y0, y0 - window_height), y0 - 2
    else:
        top, bottom = y1 + 2, min(page_rect.y1, y1 + window_height)
    if bottom <= top:
        return fitz.Rect(left, page_rect.y0, right, page_rect.y1)
    return fitz.Rect(left, top, right, bottom)


def _tighten_to_graphics(
    page: Any,
    window: Any,
    side: str,
    caption_bbox: tuple[float, float, float, float],
) -> tuple[Any, bool]:
    """Use image/vector bounds when available, retaining a text-diagram fallback."""

    import pymupdf as fitz  # type: ignore

    graphics: list[Any] = []
    try:
        for image in page.get_images(full=True):
            for rect in page.get_image_rects(image[0]):
                if rect.intersects(window):
                    graphics.append(rect)
    except Exception:
        pass
    try:
        for drawing in page.get_drawings():
            rect = drawing.get("rect")
            if rect is not None and rect.intersects(window):
                graphics.append(rect)
    except Exception:
        pass
    if not graphics:
        return window, False

    # Tighten to the graphics themselves and add a small margin for labels.
    # Figure boundaries already include their own border/labels in most
    # conference PDFs.  A near-zero margin avoids leaking adjacent body text
    # into a hero image (especially in two-column layouts).
    margin = 1
    graphic_x0 = min(rect.x0 for rect in graphics)
    graphic_x1 = max(rect.x1 for rect in graphics)
    caption_x0, _caption_y0, caption_x1, _caption_y1 = caption_bbox
    wide_graphic = (
        graphic_x1 - graphic_x0 >= page.rect.width * 0.58
        or graphic_x0 < caption_x0 - page.rect.width * 0.15
        or graphic_x1 > caption_x1 + page.rect.width * 0.15
    )
    x0 = (graphic_x0 if wide_graphic else caption_x0) - margin
    y0 = min(rect.y0 for rect in graphics) - margin
    x1 = (graphic_x1 if wide_graphic else caption_x1) + margin
    y1 = max(rect.y1 for rect in graphics) + margin
    if side == "above":
        y1 = min(y1, caption_bbox[1] - 2)
    else:
        y0 = max(y0, caption_bbox[3] + 2)
    rect = fitz.Rect(
        max(window.x0, x0),
        max(window.y0, y0),
        min(window.x1, x1),
        min(window.y1, y1),
    )
    if rect.width < 30 or rect.height < 20:
        return window, False
    return rect, True


def _render(page: Any, clip: Any, dpi: int, max_bytes: int) -> tuple[bytes, int]:
    for candidate_dpi in (dpi, 150, 120, 96):
        pixmap = page.get_pixmap(dpi=candidate_dpi, clip=clip, alpha=False)
        data = pixmap.tobytes("png")
        if len(data) <= max_bytes or candidate_dpi == 96:
            return data, candidate_dpi
    raise AssertionError("unreachable")


def extract_architecture_figure(
    pdf_bytes: bytes,
    *,
    max_pages: int = 15,
    render_dpi: int = 360,
    min_score: int = 4,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    extractor_version: str = EXTRACTOR_VERSION,
    caption_reviewer: CaptionReviewer | None = None,
    review_score_below: int | None = None,
    review_max_candidates: int = 5,
    vision_reviewer: VisionReviewer | None = None,
) -> FigureExtraction:
    """Extract the highest-scoring architecture candidate from PDF bytes.

    Caption rules remain the primary path.  If a reviewer callback is supplied,
    only low-confidence rule candidates are sent to it; when no rule candidate
    survives, the callback receives the best few captions as a second chance.
    Reviewer failures (``None``) fall back to the rules for READY papers and
    keep NO_FIGURE papers as NO_FIGURE, so model outages never break a run.
    When enabled, the vision callback receives only rendered low-confidence
    candidates and can remove a text-approved crop.
    """

    if not pdf_bytes.startswith(b"%PDF-"):
        raise FigureExtractionError("input is not a PDF")
    try:
        import pymupdf as fitz  # type: ignore
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # pragma: no cover - depends on native library
        raise FigureExtractionError(f"cannot open PDF: {exc}") from exc

    candidates: list[FigureCandidate] = []
    caption_entries: list[dict[str, Any]] = []
    try:
        for page_index in range(min(len(document), max_pages)):
            page = document[page_index]
            blocks = page.get_text("blocks", sort=True)
            for block in blocks:
                if len(block) < 5 or not str(block[4]).strip():
                    continue
                text = str(block[4]).strip()
                match = _CAPTION_RE.search(text)
                if not match:
                    continue
                caption = re.sub(r"\s+", " ", text).strip()
                score = caption_score(caption)
                caption_bbox = tuple(float(value) for value in block[:4])
                caption_entries.append(
                    {
                        "index": len(caption_entries),
                        "page_index": page_index,
                        "figure_id": f"fig{match.group('number')}",
                        "caption": caption,
                        "caption_bbox": caption_bbox,
                        "score": score,
                    }
                )

        rule_entries = [
            entry for entry in caption_entries if entry["score"] >= min_score
        ]
        selected_entries = list(rule_entries)
        review_entries: list[dict[str, Any]] = []
        if caption_reviewer is not None:
            score_cutoff = (
                review_score_below
                if review_score_below is not None
                else min_score + 3
            )
            if rule_entries:
                review_entries = sorted(
                    (entry for entry in rule_entries if entry["score"] < score_cutoff),
                    key=lambda entry: (-entry["score"], entry["index"]),
                )[: max(1, int(review_max_candidates))]
            else:
                review_entries = sorted(
                    caption_entries,
                    key=lambda entry: (-entry["score"], entry["index"]),
                )[: max(1, int(review_max_candidates))]
            if review_entries:
                try:
                    accepted = caption_reviewer(review_entries)
                except Exception:
                    accepted = None
                if accepted is not None:
                    accepted_indices = set(int(index) for index in accepted)
                    if rule_entries:
                        # High-confidence rule hits do not need model review;
                        # low-confidence hits must be accepted by the reviewer.
                        selected_entries = [
                            entry
                            for entry in rule_entries
                            if entry["score"] >= score_cutoff
                            or entry["index"] in accepted_indices
                        ]
                    else:
                        selected_entries = [
                            entry
                            for entry in caption_entries
                            if entry["index"] in accepted_indices
                        ]
                    for entry in selected_entries:
                        entry["caption_reviewed"] = entry["index"] in {
                            item["index"] for item in review_entries
                        }

        review_indices = {entry["index"] for entry in review_entries}
        render_entries = list(selected_entries)
        if vision_reviewer is not None:
            existing_indices = {entry["index"] for entry in render_entries}
            render_entries.extend(
                entry
                for entry in review_entries
                if entry["index"] not in existing_indices
            )

        for entry in render_entries:
            page = document[entry["page_index"]]
            caption = str(entry["caption"])
            caption_bbox = entry["caption_bbox"]
            score = int(entry["score"])
            figure_id = str(entry["figure_id"])
            for side in ("above", "below"):
                window = _candidate_window(
                    page, caption_bbox, side, caption_text=caption
                )
                clip, has_graphics = _tighten_to_graphics(
                    page, window, side, caption_bbox
                )
                if clip.width < 30 or clip.height < 30:
                    continue
                image_bytes, _actual_dpi = _render(
                    page, clip, render_dpi, max_image_bytes
                )
                candidates.append(
                    FigureCandidate(
                        entry_index=int(entry["index"]),
                        figure_id=figure_id,
                        page=entry["page_index"] + 1,
                        caption=caption,
                        caption_bbox=caption_bbox,
                        bbox=_clip_tuple(clip),
                        score=score,
                        side=side,
                        has_graphics=has_graphics,
                        image_bytes=image_bytes,
                    )
                )

        if vision_reviewer is not None and review_entries and candidates:
            by_entry: dict[int, FigureCandidate] = {}
            for candidate in candidates:
                if candidate.entry_index not in review_indices:
                    continue
                current = by_entry.get(candidate.entry_index)
                if current is None or (
                    candidate.has_graphics,
                    candidate.side == "above",
                ) > (
                    current.has_graphics,
                    current.side == "above",
                ):
                    by_entry[candidate.entry_index] = candidate
            vision_items = [
                {
                    "index": entry["index"],
                    "page": entry["page_index"] + 1,
                    "score": entry["score"],
                    "caption": entry["caption"],
                    "image_bytes": by_entry[entry["index"]].image_bytes,
                }
                for entry in review_entries
                if entry["index"] in by_entry
            ]
            try:
                vision_accepted = vision_reviewer(vision_items)
            except Exception:
                vision_accepted = None
            if vision_accepted is not None:
                accepted_indices = {int(index) for index in vision_accepted}
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.entry_index not in review_indices
                    or candidate.entry_index in accepted_indices
                ]
    finally:
        document.close()

    if not candidates:
        return FigureExtraction(
            status="NO_FIGURE",
            manifest={
                "status": "NO_FIGURE",
                "extractor_version": extractor_version,
                "reason": "no architecture-like caption met the score threshold",
                "caption_count": len(caption_entries),
                "review_attempted": bool(review_entries),
            },
        )

    # Prefer the caption score, then a crop that is not suspiciously tiny.
    selected = max(
        candidates,
        key=lambda candidate: (
            candidate.score,
            1 if candidate.has_graphics else 0,
            1 if candidate.side == "above" else 0,
            -((candidate.bbox[2] - candidate.bbox[0]) * (candidate.bbox[3] - candidate.bbox[1])),
        ),
    )
    return FigureExtraction(
        status="READY",
        image_bytes=selected.image_bytes,
        manifest={
            "status": "READY",
            "extractor_version": extractor_version,
            "figure_id": selected.figure_id,
            "page": selected.page,
            "caption": selected.caption,
            "caption_bbox": list(selected.caption_bbox),
            "bbox": list(selected.bbox),
            "score": selected.score,
            "side": selected.side,
            "candidate_count": len(candidates),
            "caption_reviewed": bool(
                caption_reviewer is not None
                and selected.caption in {
                    str(entry["caption"])
                    for entry in review_entries
                }
            ),
        },
    )


def write_cached_extraction(
    extraction: FigureExtraction,
    *,
    assets_root: Path,
    source: str,
    forum_id: str,
    pdf_hash: str,
) -> dict[str, Any]:
    """Atomically write a derived image and manifest under a content hash."""

    target_dir = Path(assets_root) / "conference" / source / forum_id / pdf_hash
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest = dict(extraction.manifest)
    manifest.update(
        {
            "source": source,
            "forum_id": forum_id,
            "pdf_sha256": pdf_hash,
        }
    )
    if extraction.status == "READY" and extraction.image_bytes:
        image_path = target_dir / "hero.png"
        tmp_image = image_path.with_suffix(".png.tmp")
        tmp_image.write_bytes(extraction.image_bytes)
        tmp_image.replace(image_path)
        manifest["path"] = str(image_path)
    manifest_path = target_dir / "manifest.json"
    tmp_manifest = manifest_path.with_suffix(".json.tmp")
    tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_manifest.replace(manifest_path)
    return manifest
