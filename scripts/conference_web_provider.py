"""HTML-backed conference paper providers.

The conference pipeline was originally written around OpenReview notes.  ACL
Anthology and CVF Open Access publish stable HTML indexes instead, so this
module converts those indexes into the same small ``SubmissionPage`` contract
used by the pipeline.  The providers deliberately expose no review data;
their records describe published papers only.
"""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from html import unescape
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from openreview_provider import SubmissionPage, VenueCapabilities
from paper_metadata import extract_affiliations_from_html, extract_affiliations_from_pdf


class WebConferenceProviderError(RuntimeError):
    """Base error for an HTML conference source."""


class WebConferenceNotReady(WebConferenceProviderError):
    """The configured venue page exists but does not contain papers yet."""


def _text(node: Any) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _clean_fragment(fragment: str) -> str:
    """Convert a small HTML fragment to normalized text."""

    text = re.sub(r"<[^>]+>", " ", str(fragment or ""))
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _absolute(base_url: str, href: str) -> str:
    return urljoin(base_url, str(href or "").strip())


def _stable_id(provider: str, value: str) -> str:
    parsed = urlparse(value)
    canonical = parsed._replace(fragment="").geturl().rstrip("/")
    digest = sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{provider}:{digest}"


def _first_pdf_url(soup: BeautifulSoup, page_url: str, fallback: str) -> str:
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if ".pdf" in href.casefold():
            return _absolute(page_url, href)
    return fallback


_CODE_HOSTS = {"github.com"}
_CODE_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[^\s<>\]\[)\}\"']+",
    flags=re.I,
)
_CODE_LABEL_RE = re.compile(
    r"\b(?:code|source|repository|repo|github|gitlab|implementation|project)\b",
    flags=re.I,
)


def _normalize_code_url(value: str, base_url: str = "") -> str:
    """Return a repository URL, rejecting profile/issue/commit links."""

    raw = _absolute(base_url, value).strip().rstrip(".,;:)")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if parsed.scheme not in {"http", "https"} or host not in _CODE_HOSTS:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return ""
    if parts[2:3] and parts[2].casefold() in {
        "issues", "pull", "pulls", "commit", "commits", "blob", "tree",
    }:
        return ""
    normalized = parsed._replace(query="", fragment="").geturl().rstrip("/")
    return normalized


def _extract_code_url_from_html(html: str, page_url: str = "") -> str:
    """Find the most likely repository link in a paper detail page."""

    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Site-wide navigation/footer links (for example ACL Anthology's own
    # GitHub repository) are not paper code links.
    for chrome in soup.select("nav, header, footer, script, style, form, .modal"):
        chrome.decompose()
    ranked: list[tuple[int, int, str]] = []
    order = 0
    for anchor in soup.find_all("a", href=True):
        candidate = _normalize_code_url(str(anchor.get("href") or ""), page_url)
        if not candidate:
            continue
        label = _text(anchor)
        context = " ".join(
            part
            for part in (
                label,
                str(anchor.get("class") or ""),
                str(anchor.parent.get_text(" ", strip=True) if anchor.parent else ""),
            )
            if part
        )
        score = 2 if _CODE_LABEL_RE.search(context) else 1
        ranked.append((-score, order, candidate))
        order += 1
    if ranked:
        ranked.sort()
        return ranked[0][2]
    for match in _CODE_URL_RE.finditer(str(soup)):
        candidate = _normalize_code_url(match.group(0), page_url)
        if candidate:
            return candidate
    return ""


def _extract_code_url_from_pdf(pdf_bytes: bytes) -> str:
    """Search only the first three PDF pages (title/abstract area)."""

    if not pdf_bytes:
        return ""
    try:
        import pymupdf as fitz  # type: ignore

        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            text = "\n".join(
                document[index].get_text("text", sort=True)
                for index in range(min(len(document), 3))
            )[:16000]
        finally:
            document.close()
    except Exception:
        return ""
    abstract_match = re.search(r"\babstract\b", text, flags=re.I)
    if abstract_match:
        text = text[abstract_match.start() : abstract_match.start() + 8000]
    for match in _CODE_URL_RE.finditer(text):
        candidate = _normalize_code_url(match.group(0))
        if candidate:
            return candidate
    return ""


@dataclass(frozen=True)
class WebProviderOptions:
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    user_agent: str = "DailyInfo/0.2 (conference paper metadata)"
    detail_cache_ttl_seconds: int = 7 * 24 * 3600
    retries: int = 2
    detail_workers: int = 4


class WebConferenceProvider:
    """Base provider for one static conference listing."""

    provider_name = "web"

    def __init__(self, config: dict, *, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        request_cfg = config.get("request", {}) or {}
        self.options = WebProviderOptions(
            connect_timeout=float(
                request_cfg.get(
                    "connect_timeout_seconds",
                    config.get("api_connect_timeout_seconds", 10),
                )
            ),
            read_timeout=float(
                request_cfg.get(
                    "read_timeout_seconds",
                    config.get("api_read_timeout_seconds", 30),
                )
            ),
            user_agent=str(
                request_cfg.get(
                    "user_agent", "DailyInfo/0.2 (conference paper metadata)"
                )
            ),
            detail_cache_ttl_seconds=int(
                request_cfg.get("detail_cache_ttl_seconds", 7 * 24 * 3600)
            ),
            retries=max(0, int(request_cfg.get("retries", 2))),
            detail_workers=max(1, int(request_cfg.get("detail_workers", 4))),
        )
        if self.options.connect_timeout <= 0 or self.options.read_timeout <= 0:
            raise ValueError("conference web request timeouts must be positive")
        self.session.headers.update({"User-Agent": self.options.user_agent})
        cache_dir = config.get("provider_cache_dir")
        self.cache_dir = Path(cache_dir).expanduser() if cache_dir else None
        self._listing_url = str(config.get("url") or "").strip()
        if not self._listing_url:
            raise ValueError("web conference source requires url")
        self._papers: dict[str, dict] = {}
        self._memory_cache: dict[str, str] = {}
        self._code_url_cache: dict[str, str] = {}
        self._affiliations_cache: dict[str, list[str]] = {}
        self._affiliation_source_cache: dict[str, str] = {}
        self._pdf_bytes_cache: dict[str, bytes] = {}

    @property
    def client(self):
        """Compatibility with the PDF enrichment path."""

        return self

    def close(self) -> None:
        self.session.close()

    @property
    def pdf_bytes_cache(self) -> dict[str, bytes]:
        """PDFs fetched for code-link fallback, reusable by figure extraction."""

        return self._pdf_bytes_cache

    def _cache_path(self, url: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = sha256(url.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / self.provider_name / f"{digest}.json"

    def _get_text(self, url: str, *, detail: bool = False) -> str:
        if url in self._memory_cache:
            return self._memory_cache[url]
        cache_path = self._cache_path(url)
        if detail and cache_path and cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                age = time.time() - float(cached.get("fetched_at", 0))
                if age <= self.options.detail_cache_ttl_seconds:
                    return str(cached.get("text") or "")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        last_error: Exception | None = None
        response = None
        for attempt in range(self.options.retries + 1):
            try:
                response = self.session.get(
                    url,
                    timeout=(
                        self.options.connect_timeout,
                        self.options.read_timeout,
                    ),
                )
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                last_error = exc
                response = getattr(exc, "response", None)
                if response is not None and response.status_code == 404:
                    raise
                if attempt >= self.options.retries:
                    raise
                time.sleep(min(2.0**attempt, 5.0))
        if response is None:
            raise WebConferenceProviderError(
                f"failed to fetch conference page: {url}"
            ) from last_error
        text = response.text
        self._memory_cache[url] = text
        if detail and cache_path:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(
                        {"url": url, "fetched_at": time.time(), "text": text},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                # Metadata caching is best effort and must not hide a valid
                # source response.
                pass
        return text

    def discover_venue(self) -> VenueCapabilities:
        try:
            html = self._get_text(self._listing_url)
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 404:
                raise WebConferenceNotReady(
                    f"venue page not published yet: {self._listing_url}"
                ) from exc
            raise
        if not html.strip():
            raise WebConferenceNotReady(f"empty venue page: {self._listing_url}")
        return VenueCapabilities(
            venue_id=str(self.config.get("venue_id") or self._listing_url),
            submission_invitation=self._listing_url,
            submission_venue_id=str(self.config.get("venue_id") or ""),
            public_submissions=True,
        )

    def _parse_listing(self, html: str, listing_url: str) -> list[dict]:
        raise NotImplementedError

    def _load_papers(self) -> list[dict]:
        if self._papers:
            return list(self._papers.values())
        papers = self._parse_listing(self._get_text(self._listing_url), self._listing_url)
        for paper in papers:
            self._papers[str(paper["forum_id"])] = paper
        return list(self._papers.values())

    def iter_submission_pages(
        self,
        _capabilities: VenueCapabilities,
        min_cdate: int | None = None,
        after_id: str | None = None,
        page_size: int = 1000,
        total_hint: int | None = None,
    ):
        del min_cdate, total_hint
        papers = self._load_papers()
        page_size = max(1, int(page_size))
        if not papers:
            raise WebConferenceNotReady(
                f"no papers found at {self._listing_url}"
            )
        start = 0
        if after_id:
            matching = next(
                (index for index, paper in enumerate(papers) if paper["forum_id"] == after_id),
                None,
            )
            if matching is None:
                raise WebConferenceProviderError(
                    f"web provider cursor not found: {after_id}"
                )
            start = matching + 1
        for offset in range(start, len(papers), page_size):
            page = papers[offset : offset + page_size]
            yield SubmissionPage(
                papers=page,
                cursor_after=str(page[-1]["forum_id"]),
                total=len(papers),
                page_number=offset // page_size + 1,
                raw_count=len(page),
            )

    def fetch_submissions(
        self, capabilities: VenueCapabilities, min_cdate: int | None = None
    ) -> list[dict]:
        return [
            paper
            for page in self.iter_submission_pages(
                capabilities, min_cdate=min_cdate
            )
            for paper in page.papers
        ]

    def _paper_code_url(
        self, paper: dict, *, detail_html: str | None = None
    ) -> str:
        """Extract code from HTML first, then the first PDF pages if needed."""

        landing_url = str(paper.get("landing_url") or "")
        cache_key = landing_url or str(paper.get("forum_id") or "")
        if cache_key in self._code_url_cache:
            return self._code_url_cache[cache_key]
        code_url = _extract_code_url_from_html(detail_html or "", landing_url)
        if not code_url and landing_url:
            try:
                detail_html = detail_html or self._get_text(landing_url, detail=True)
                code_url = _extract_code_url_from_html(detail_html, landing_url)
            except Exception:
                detail_html = detail_html or ""
        if not code_url:
            pdf_url = str(paper.get("pdf") or paper.get("pdf_field") or "")
            if pdf_url:
                try:
                    from conference_figures import DEFAULT_ALLOWED_HOSTS, download_pdf

                    figure_cfg = self.config.get("figures", {}) or {}
                    pdf_bytes = download_pdf(
                        pdf_url,
                        note_id="",
                        session=self.session,
                        max_bytes=int(figure_cfg.get("max_pdf_mb", 50)) * 1024 * 1024,
                        timeout=(
                            self.options.connect_timeout,
                            self.options.read_timeout,
                        ),
                        allowed_hosts=figure_cfg.get("allowed_hosts")
                        or DEFAULT_ALLOWED_HOSTS,
                    )
                    self._pdf_bytes_cache[pdf_url] = pdf_bytes
                    code_url = _extract_code_url_from_pdf(pdf_bytes)
                except Exception:
                    # Code discovery is an enrichment; metadata processing must
                    # continue when a publisher PDF is unavailable.
                    code_url = ""
        self._code_url_cache[cache_key] = code_url
        return code_url

    def _enrich_paper_code_url(
        self, paper: dict | None, *, detail_html: str | None = None
    ) -> dict | None:
        if paper is None or paper.get("code_url"):
            return paper
        code_url = self._paper_code_url(paper, detail_html=detail_html)
        if code_url:
            paper["code_url"] = code_url
        return paper

    def _paper_affiliations(
        self, paper: dict, *, detail_html: str | None = None
    ) -> list[str]:
        """Resolve affiliations from detail HTML, then the PDF first pages."""

        cache_key = str(paper.get("landing_url") or paper.get("forum_id") or "")
        if cache_key in self._affiliations_cache:
            return self._affiliations_cache[cache_key]
        affiliations = extract_affiliations_from_html(detail_html or "")
        source = "detail_page" if affiliations else "missing"
        if not affiliations and paper.get("landing_url"):
            try:
                detail_html = detail_html or self._get_text(
                    str(paper["landing_url"]), detail=True
                )
                affiliations = extract_affiliations_from_html(detail_html)
                if affiliations:
                    source = "detail_page"
            except Exception:
                pass
        if not affiliations:
            pdf_url = str(paper.get("pdf") or paper.get("pdf_field") or "")
            if pdf_url:
                try:
                    from conference_figures import DEFAULT_ALLOWED_HOSTS, download_pdf

                    figure_cfg = self.config.get("figures", {}) or {}
                    pdf_bytes = self._pdf_bytes_cache.get(pdf_url)
                    if pdf_bytes is None:
                        pdf_bytes = download_pdf(
                            pdf_url,
                            note_id="",
                            session=self.session,
                            max_bytes=int(figure_cfg.get("max_pdf_mb", 50)) * 1024 * 1024,
                            timeout=(
                                self.options.connect_timeout,
                                self.options.read_timeout,
                            ),
                            allowed_hosts=figure_cfg.get("allowed_hosts")
                            or DEFAULT_ALLOWED_HOSTS,
                        )
                        self._pdf_bytes_cache[pdf_url] = pdf_bytes
                    affiliations = extract_affiliations_from_pdf(pdf_bytes)
                    if affiliations:
                        source = "pdf"
                except Exception:
                    pass
        self._affiliations_cache[cache_key] = affiliations
        self._affiliation_source_cache[cache_key] = source
        return affiliations

    def _enrich_paper_affiliations(
        self, paper: dict | None, *, detail_html: str | None = None
    ) -> dict | None:
        if paper is None:
            return paper
        paper["affiliations"] = self._paper_affiliations(
            paper, detail_html=detail_html
        )
        cache_key = str(paper.get("landing_url") or paper.get("forum_id") or "")
        paper["affiliation_source"] = self._affiliation_source_cache.get(
            cache_key, "missing"
        )
        return paper

    def fetch_forum(
        self, forum_id: str, _capabilities: VenueCapabilities
    ) -> tuple[dict | None, list[dict]]:
        # Proceedings sources are published-paper catalogs and do not expose
        # Proceedings sources are published-paper catalogs and do not expose
        # OpenReview reviews/rebuttals. Returning an empty reply list lets the
        # existing snapshot/event machinery retain its review-neutral fields.
        paper = self._papers.get(str(forum_id))
        self._enrich_paper_code_url(paper)
        return self._enrich_paper_affiliations(paper), []


class ACLAnthologyProvider(WebConferenceProvider):
    """Read ACL/EMNLP/NAACL/EACL/COLING event or volume pages."""

    provider_name = "acl"
    _paper_href = re.compile(
        r"^/(?:\d{4}\.[A-Za-z0-9-]+\.\d+|[A-Za-z]\d{2}-\d+)/?$"
    )
    _volume_href = re.compile(r"^/volumes/[A-Za-z0-9._-]+/$")

    def _volume_urls(self, html: str, source_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        links = {
            _absolute(source_url, anchor.get("href"))
            for anchor in soup.find_all("a", href=True)
            if self._volume_href.match(str(anchor.get("href") or ""))
        }
        return sorted(links) or [source_url]

    def _parse_volume(self, html: str, volume_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[dict] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "")
            if not self._paper_href.match(href):
                continue
            paper_url = _absolute(volume_url, href)
            if paper_url in seen or _text(anchor).casefold().startswith("proceedings of"):
                continue
            seen.add(paper_url)
            paper_id = paper_url.rstrip("/").rsplit("/", 1)[-1]
            abstract_id = "abstract-" + paper_id.replace(".", "--")
            abstract = _text(soup.find(id=abstract_id))
            container = anchor.find_parent()
            authors = []
            for candidate in (container, container.parent if container else None):
                if candidate is None:
                    continue
                authors = [
                    _text(item)
                    for item in candidate.find_all(
                        "a", href=re.compile(r"/people/")
                    )
                    if _text(item)
                ]
                if authors:
                    break
            # ACL Anthology exposes a stable ``<paper-slug>.pdf`` route. Do
            # not select the first PDF link in the whole volume page because
            # that can be a volume-level metadata download.
            pdf = paper_url.rstrip("/") + ".pdf"
            records.append(
                {
                    "id": paper_id,
                    "note_id": paper_id,
                    "forum_id": _stable_id(self.provider_name, paper_url),
                    "source_provider": self.provider_name,
                    "title": _text(anchor),
                    "abstract": abstract,
                    "authors": authors,
                    "affiliations": [],
                    "keywords": [],
                    "venue": str(self.config.get("display_name") or ""),
                    "venue_id": str(self.config.get("venue_id") or ""),
                    "status": "published",
                    "pdf": pdf,
                    "pdf_field": pdf,
                    "landing_url": paper_url,
                    "code_url": "",
                    "cdate": 0,
                    "mdate": 0,
                    "camera_ready": True,
                }
            )
        return records

    def _parse_listing(self, html: str, listing_url: str) -> list[dict]:
        records: list[dict] = []
        seen: set[str] = set()
        for volume_url in self._volume_urls(html, listing_url):
            volume_html = html if volume_url == listing_url else self._get_text(volume_url, detail=True)
            for paper in self._parse_volume(volume_html, volume_url):
                if paper["forum_id"] not in seen:
                    records.append(paper)
                    seen.add(paper["forum_id"])
        return records


class CVFOpenAccessProvider(WebConferenceProvider):
    """Read CVF/ECVA open-access conference indexes."""

    provider_name = "cvf"

    def _parse_listing(self, html: str, listing_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        entries: list[tuple[str, str, list[str], str]] = []
        seen: set[str] = set()
        target_venue = str(self.config.get("venue_id") or "").casefold()
        for title_node in soup.find_all("dt", class_="ptitle"):
            anchor = title_node.find("a", href=True)
            if anchor is None:
                continue
            paper_url = _absolute(listing_url, anchor.get("href"))
            normalized_url = paper_url.casefold()
            is_cvf_paper = normalized_url.endswith("_paper.html")
            is_ecva_paper = "/eccv_" in normalized_url and normalized_url.endswith(".php")
            if is_ecva_paper and target_venue and target_venue.startswith("eccv"):
                year = target_venue.removeprefix("eccv")
                is_ecva_paper = f"/eccv_{year}/" in normalized_url
            if not (is_cvf_paper or is_ecva_paper):
                continue
            forum_id = _stable_id(self.provider_name, paper_url)
            if forum_id in seen:
                continue
            seen.add(forum_id)
            authors: list[str] = []
            sibling = title_node.find_next_sibling()
            for _ in range(3):
                if sibling is None:
                    break
                authors.extend(
                    _text(item)
                    for item in sibling.find_all("a", href="#")
                    if _text(item)
                )
                if authors:
                    break
                sibling = sibling.find_next_sibling()
            entries.append((forum_id, paper_url, list(dict.fromkeys(authors)), _text(anchor)))

        detail_html_by_url: dict[str, str] = {}
        if not self.config.get("defer_detail_fetch", False):
            with ThreadPoolExecutor(max_workers=self.options.detail_workers) as executor:
                pending = {
                    executor.submit(self._get_text, paper_url, detail=True): paper_url
                    for _forum_id, paper_url, _authors, _title in entries
                }
                for future in as_completed(pending):
                    paper_url = pending[future]
                    try:
                        detail_html_by_url[paper_url] = future.result()
                    except requests.RequestException:
                        # A single withdrawn/missing CVF detail page must not
                        # discard the entire conference listing. Keep title and
                        # authors; the deterministic PDF fallback still permits
                        # lexical retrieval and a later retry.
                        detail_html_by_url[paper_url] = ""

        records: list[dict] = []
        for forum_id, paper_url, authors, title in entries:
            detail = BeautifulSoup(
                detail_html_by_url.get(paper_url, ""), "html.parser"
            )
            fallback_pdf = paper_url.replace("_paper.html", "_paper.pdf")
            pdf = _first_pdf_url(detail, paper_url, fallback_pdf)
            records.append(
                {
                    "id": forum_id,
                    "note_id": forum_id,
                    "forum_id": forum_id,
                    "source_provider": self.provider_name,
                    "title": title,
                    "abstract": _text(detail.find(id="abstract")),
                    "authors": list(dict.fromkeys(authors)),
                    "affiliations": [],
                    "keywords": [],
                    "venue": str(self.config.get("display_name") or ""),
                    "venue_id": str(self.config.get("venue_id") or ""),
                    "status": "published",
                    "pdf": pdf,
                    "pdf_field": pdf,
                    "landing_url": paper_url,
                    "code_url": "",
                    "cdate": 0,
                    "mdate": 0,
                    "camera_ready": True,
                }
            )
        return records

    def fetch_forum(
        self, forum_id: str, _capabilities: VenueCapabilities
    ) -> tuple[dict | None, list[dict]]:
        paper = self._papers.get(str(forum_id))
        if paper is None:
            return None, []
        html = self._get_text(str(paper.get("landing_url") or ""), detail=True)
        soup = BeautifulSoup(html, "html.parser")
        abstract = _text(soup.find(id="abstract"))
        if abstract:
            paper["abstract"] = abstract
        fallback_pdf = str(paper.get("pdf") or "")
        paper["pdf"] = paper["pdf_field"] = _first_pdf_url(
            soup, str(paper.get("landing_url") or ""), fallback_pdf
        )
        self._enrich_paper_code_url(paper, detail_html=html)
        return self._enrich_paper_affiliations(paper, detail_html=html), []


class DBLPProvider(WebConferenceProvider):
    """Read a DBLP conference table-of-contents page.

    DBLP is a bibliographic index, so abstracts are usually unavailable.  The
    detail/DOI links are retained as the landing and PDF candidates; the
    conference pipeline can still filter by title and enrich the selected
    papers from the linked publisher page when available.
    """

    provider_name = "dblp"

    def _parse_listing(self, html: str, listing_url: str) -> list[dict]:
        records: list[dict] = []
        # DBLP TOC pages contain a large amount of navigation/schema markup
        # (AAAI 2026 is tens of MB). Splitting on entry boundaries avoids the
        # very slow full-document BeautifulSoup parse while preserving the
        # fields needed by the provider.
        chunks = re.split(r'(?=<li\s+class=["\']entry\b)', html, flags=re.I)
        for chunk in chunks[1:]:
            class_match = re.match(
                r'<li\s+class=["\']([^"\']+)', chunk, flags=re.I
            )
            if not class_match or "inproceedings" not in class_match.group(1).casefold():
                continue
            title_match = re.search(
                r'<(?:span|div)\b[^>]*class=["\'][^"\']*\btitle\b[^"\']*["\'][^>]*>(.*?)</(?:span|div)>',
                chunk,
                flags=re.I | re.S,
            )
            title = _clean_fragment(title_match.group(1)) if title_match else ""
            if not title:
                continue
            author_part = re.split(r'class=["\'][^"\']*\btitle\b', chunk, maxsplit=1, flags=re.I)[0]
            authors = [
                _clean_fragment(match)
                for match in re.findall(
                    r'itemprop=["\']name["\'][^>]*>(.*?)</', author_part, flags=re.I | re.S
                )
            ]
            detail_match = re.search(
                r'<a\b[^>]*href=["\']([^"\']*/rec/[^"\']+)["\'][^>]*>',
                chunk,
                flags=re.I,
            )
            doi_match = re.search(
                r'<li\b[^>]*class=["\'][^"\']*\bee\b[^"\']*["\'][\s\S]*?<a\b[^>]*href=["\']([^"\']+)["\']',
                chunk,
                flags=re.I,
            )
            detail_url = _absolute(listing_url, detail_match.group(1)) if detail_match else ""
            doi_url = _absolute(listing_url, doi_match.group(1)) if doi_match else ""
            landing_url = detail_url or doi_url
            if not landing_url:
                continue
            forum_id = _stable_id(self.provider_name, landing_url)
            records.append(
                {
                    "id": forum_id,
                    "note_id": forum_id,
                    "forum_id": forum_id,
                    "source_provider": self.provider_name,
                    "title": title,
                    "abstract": "",
                    "authors": list(dict.fromkeys(authors)),
                    "affiliations": [],
                    "keywords": [],
                    "venue": str(self.config.get("display_name") or ""),
                    "venue_id": str(self.config.get("venue_id") or ""),
                    "status": "published",
                    "pdf": doi_url or landing_url,
                    "pdf_field": doi_url or landing_url,
                    "landing_url": landing_url,
                    "code_url": "",
                    "cdate": 0,
                    "mdate": 0,
                    "camera_ready": True,
                }
            )
        return records

    def fetch_forum(
        self, forum_id: str, _capabilities: VenueCapabilities
    ) -> tuple[dict | None, list[dict]]:
        paper = self._papers.get(str(forum_id))
        if paper and not paper.get("abstract"):
            # A DBLP record itself normally has no abstract.  For selected
            # candidates only, follow its DOI/publisher page when possible so
            # the briefing can still contain a source-backed introduction.
            url = str(paper.get("pdf") or "")
            if url.startswith("https://doi.org/"):
                try:
                    html = self._get_text(url, detail=True)
                    soup = BeautifulSoup(html, "html.parser")
                    abstract_node = soup.select_one(
                        "[itemprop='abstract'], meta[name='citation_abstract'], #abstract"
                    )
                    abstract = (
                        str(abstract_node.get("content") or "").strip()
                        if abstract_node and abstract_node.name == "meta"
                        else _text(abstract_node)
                    )
                    if abstract:
                        paper["abstract"] = abstract
                    citation = soup.select_one("meta[name='citation_pdf_url'][content]")
                    if citation:
                        pdf = _absolute(url, citation.get("content"))
                        paper["pdf"] = paper["pdf_field"] = pdf
                except requests.RequestException:
                    # DOI/publisher pages may be rate-limited or unavailable;
                    # retaining bibliographic metadata is still useful.
                    pass
        if paper:
            self._enrich_paper_code_url(paper)
            self._enrich_paper_affiliations(paper)
        return paper, []


class NeurIPSProceedingsProvider(WebConferenceProvider):
    """Read the official NeurIPS Proceedings volume and paper pages."""

    provider_name = "neurips"

    def _parse_listing(self, html: str, listing_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        records: list[dict] = []
        seen: set[str] = set()
        for anchor in soup.select("a[title='paper title'][href]"):
            paper_url = _absolute(listing_url, anchor.get("href"))
            if paper_url in seen:
                continue
            seen.add(paper_url)
            container = anchor.find_parent(class_="paper-content") or anchor.parent
            author_node = container.select_one(".paper-authors") if container else None
            authors = [
                name.strip()
                for name in _text(author_node).split(",")
                if name.strip()
            ]
            forum_id = _stable_id(self.provider_name, paper_url)
            records.append(
                {
                    "id": forum_id,
                    "note_id": forum_id,
                    "forum_id": forum_id,
                    "source_provider": self.provider_name,
                    "title": _text(anchor),
                    "abstract": "",
                    "authors": authors,
                    "affiliations": [],
                    "keywords": [],
                    "venue": str(self.config.get("display_name") or ""),
                    "venue_id": str(self.config.get("venue_id") or ""),
                    "status": "published",
                    "pdf": "",
                    "pdf_field": "",
                    "landing_url": paper_url,
                    "code_url": "",
                    "cdate": 0,
                    "mdate": 0,
                    "camera_ready": True,
                }
            )
        return records

    def fetch_forum(
        self, forum_id: str, _capabilities: VenueCapabilities
    ) -> tuple[dict | None, list[dict]]:
        paper = self._papers.get(str(forum_id))
        if paper and not paper.get("abstract"):
            html = self._get_text(str(paper.get("landing_url") or ""), detail=True)
            soup = BeautifulSoup(html, "html.parser")
            abstract = _text(soup.select_one(".paper-abstract"))
            pdf = ""
            citation = soup.select_one("meta[name='citation_pdf_url'][content]")
            if citation:
                pdf = _absolute(str(paper["landing_url"]), citation.get("content"))
            if not pdf:
                pdf = _first_pdf_url(soup, str(paper["landing_url"]), "")
            if abstract:
                paper["abstract"] = abstract
            if pdf:
                paper["pdf"] = paper["pdf_field"] = pdf
            self._enrich_paper_code_url(paper, detail_html=html)
            self._enrich_paper_affiliations(paper, detail_html=html)
        elif paper:
            self._enrich_paper_code_url(paper)
            self._enrich_paper_affiliations(paper)
        return paper, []


def create_web_conference_provider(
    config: dict, *, session: requests.Session | None = None
) -> WebConferenceProvider:
    provider = str(config.get("provider") or "").casefold()
    if provider == "acl":
        return ACLAnthologyProvider(config, session=session)
    if provider == "cvf":
        return CVFOpenAccessProvider(config, session=session)
    if provider == "dblp":
        return DBLPProvider(config, session=session)
    if provider == "neurips":
        return NeurIPSProceedingsProvider(config, session=session)
    raise ValueError(f"unsupported web conference provider: {provider or '<empty>'}")
