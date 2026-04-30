"""Open-access paper download: arXiv → Unpaywall → Semantic Scholar → Crossref → Library DB."""
import os
import re
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx

from .config import DOWNLOAD_DIR
from .utils import get_logger

log = get_logger("paper_fetcher")

HEADERS = {"User-Agent": "DailyInfo/1.0 (dailyinfo@example.com)"}
TIMEOUT = 20
# httpx reads uppercase env vars; fall back to lowercase (common on Linux)
_PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or None
)


def _client(**kwargs) -> httpx.AsyncClient:
    """Return an AsyncClient with proxy pre-configured."""
    kw: dict = {"headers": HEADERS, "follow_redirects": True, "timeout": TIMEOUT}
    if _PROXY:
        kw["proxy"] = _PROXY
    kw.update(kwargs)
    return httpx.AsyncClient(**kw)


def _client_direct(**kwargs) -> httpx.AsyncClient:
    """Return an AsyncClient WITHOUT proxy — uses campus IP directly.
    For publisher sites that authenticate via campus IP range.
    """
    kw: dict = {"headers": HEADERS, "follow_redirects": True, "timeout": TIMEOUT}
    kw.update(kwargs)
    return httpx.AsyncClient(**kw)


async def _download_pdf(url: str, dest: Path) -> bool:
    try:
        async with _client(timeout=30) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and b"%PDF" in resp.content[:10]:
                dest.write_bytes(resp.content)
                return True
    except Exception as e:
        log.debug(f"Download failed {url}: {e}")
    return False


async def _download_pdf_direct(url: str, dest: Path) -> bool:
    """Download PDF bypassing proxy — uses campus IP for publisher authentication."""
    try:
        async with _client_direct(timeout=30) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and b"%PDF" in resp.content[:10]:
                dest.write_bytes(resp.content)
                return True
            log.debug(f"Direct download {resp.status_code} for {url}")
    except Exception as e:
        log.debug(f"Direct download failed {url}: {e}")
    return False


async def _try_arxiv_api(title: str) -> Optional[str]:
    try:
        async with _client() as client:
            resp = await client.get(
                "https://export.arxiv.org/api/query",
                params={"search_query": f'ti:"{title}"', "max_results": "1"},
            )
            m = re.search(r"<id>https?://arxiv\.org/abs/([\d.v]+)</id>", resp.text)
            if m:
                return f"https://arxiv.org/pdf/{m.group(1)}.pdf"
    except Exception as e:
        log.debug(f"arXiv API error: {e}")
    return None


async def _try_unpaywall(title: str) -> Optional[str]:
    try:
        async with _client() as client:
            resp = await client.get(
                "https://api.unpaywall.org/v2/search",
                params={"query": title, "email": "dailyinfo@example.com", "is_oa": "true"},
            )
            data = resp.json()
            for r in data.get("results", []):
                oa_url = (r.get("response", {}) or {}).get("best_oa_location", {})
                if isinstance(oa_url, dict):
                    pdf = oa_url.get("url_for_pdf")
                    if pdf:
                        return pdf
    except Exception as e:
        log.debug(f"Unpaywall error: {e}")
    return None


async def _try_semantic_scholar(title: str) -> Optional[str]:
    try:
        async with _client() as client:
            resp = await client.get(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={"query": title, "fields": "openAccessPdf,title", "limit": "1"},
            )
            data = resp.json()
            papers = data.get("data", [])
            if papers:
                oa = papers[0].get("openAccessPdf")
                if oa and oa.get("url"):
                    return oa["url"]
    except Exception as e:
        log.debug(f"Semantic Scholar error: {e}")
    return None


async def _lookup_pmc(title: str, doi: str = "") -> Optional[tuple[str, str]]:
    """Search PubMed → get (pmc_id, pdf_filename) or None."""
    _STOPWORDS = {"the", "a", "an", "and", "or", "of", "to", "with", "in", "for",
                  "on", "by", "from", "is", "are", "that", "this", "at", "as"}
    try:
        async with _client() as client:
            pmid = ""
            # Prefer DOI-based lookup (most reliable)
            if doi:
                r = await client.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params={"db": "pubmed", "term": f"{doi}[doi]", "retmax": "1", "retmode": "json"},
                )
                ids = r.json().get("esearchresult", {}).get("idlist", [])
                if ids:
                    pmid = ids[0]

            if not pmid:
                # Keyword-based: strip special chars, skip stop words, take first 8 content words
                words = re.sub(r'[^a-zA-Z0-9\s-]', ' ', title).split()
                keywords = [w for w in words if w.lower() not in _STOPWORDS and len(w) > 2][:8]
                query = " ".join(keywords)
                r = await client.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params={"db": "pubmed", "term": query, "retmax": "1", "retmode": "json"},
                )
                ids = r.json().get("esearchresult", {}).get("idlist", [])
                if not ids:
                    return None
                pmid = ids[0]

            r2 = await client.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                params={"db": "pubmed", "id": pmid, "retmode": "json"},
            )
            result = r2.json().get("result", {}).get(pmid, {})
            pmc_id = ""
            for uid in result.get("articleids", []):
                if uid.get("idtype") == "pmc":
                    pmc_id = uid["value"]
                    break
            if not pmc_id:
                return None

            # Get PDF filename from PMC OA API
            r3 = await client.get(
                "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi",
                params={"id": pmc_id},
            )
            m = re.search(r'href="[^"]+/([^"/]+\.pdf)"', r3.text)
            filename = m.group(1) if m else f"{pmc_id}.pdf"
            log.info(f"  PMC found: {pmc_id}  file: {filename}")
            return pmc_id, filename
    except Exception as e:
        log.debug(f"PMC lookup error: {e}")
    return None


async def _try_pubmed_central(title: str, doi: str = "") -> Optional[str]:
    """Placeholder — PMC download requires playwright; returns None so playwright path runs."""
    info = await _lookup_pmc(title, doi=doi)
    if info:
        _PMC_CACHE[title] = info
    return None


_PMC_CACHE: dict[str, tuple[str, str]] = {}


_STOPWORDS = {"a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or",
              "with", "by", "from", "is", "are", "its", "this", "that", "as"}


def _title_word_overlap(query: str, result: str) -> float:
    """Fraction of meaningful query words found in result title (case-insensitive).
    Ignores stopwords and short tokens. Returns 0.0–1.0.
    """
    def words(s: str) -> set:
        return {w.lower() for w in re.sub(r'[^\w\s]', ' ', s).split()
                if len(w) > 2 and w.lower() not in _STOPWORDS}
    q, r = words(query), words(result)
    if not q:
        return 0.0
    return len(q & r) / len(q)


async def lookup_crossref(title: str) -> dict:
    """Query Crossref for DOI, publisher, journal, and any OA PDF link.
    Returns dict with keys: doi, publisher, journal, issn, pdf_url (may be empty strings).
    Validates match quality by title word overlap to avoid wrong-DOI cascades.
    """
    result = {"doi": "", "publisher": "", "journal": "", "issn": "", "pdf_url": ""}
    try:
        async with _client() as client:
            resp = await client.get(
                "https://api.crossref.org/works",
                params={
                    "query.title": title,
                    "rows": "1",
                    "select": "DOI,publisher,container-title,ISSN,link,license,score,title",
                },
            )
            data = resp.json()
            items = data.get("message", {}).get("items", [])
            if not items:
                return result
            item = items[0]
            returned_title = (item.get("title") or [""])[0]
            overlap = _title_word_overlap(title, returned_title)
            if overlap < 0.5:
                log.debug(
                    f"Crossref title mismatch (overlap={overlap:.2f}) for '{title[:50]}' "
                    f"→ '{returned_title[:50]}' — discarding"
                )
                return result
            result["doi"] = item.get("DOI", "")
            result["publisher"] = item.get("publisher", "")
            journals = item.get("container-title", [])
            result["journal"] = journals[0] if journals else ""
            issns = item.get("ISSN", [])
            result["issn"] = issns[0] if issns else ""

            # Look for OA PDF link in crossref links
            for link in item.get("link", []):
                if link.get("content-type") == "application/pdf":
                    result["pdf_url"] = link["URL"]
                    break
    except Exception as e:
        log.debug(f"Crossref error: {e}")
    return result


async def _try_crossref_oa(title: str) -> Optional[str]:
    """Return direct OA PDF URL from Crossref if available."""
    meta = await lookup_crossref(title)
    if meta["pdf_url"]:
        log.info(f"  Crossref OA link: {meta['pdf_url']}")
        return meta["pdf_url"]
    return None


async def _try_unpaywall_doi(doi: str) -> Optional[str]:
    """Lookup Unpaywall by DOI (more reliable than title search)."""
    if not doi:
        return None
    try:
        async with _client() as client:
            r = await client.get(
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": "dailyinfo@example.com"},
            )
            data = r.json()
            best = data.get("best_oa_location") or {}
            return best.get("url_for_pdf") or best.get("url") or None
    except Exception as e:
        log.debug(f"Unpaywall DOI error: {e}")
    return None


def _doi_to_publisher_pdf(doi: str) -> Optional[str]:
    """Return direct PDF URL for known OA publisher DOI patterns."""
    if doi.startswith("10.1038/"):
        suffix = doi[8:]
        # d41586 = Nature News/Views/Comment — editorial pieces, no PDF download
        if suffix.startswith("d41586-"):
            return None
        return f"https://www.nature.com/articles/{suffix}.pdf"
    if doi.startswith("10.1126/"):  # Science
        return f"https://www.science.org/doi/pdf/{doi}"
    if doi.startswith("10.1371/"):  # PLOS
        return f"https://journals.plos.org/plosone/article/file?id={doi}&type=printable"
    if doi.startswith("10.7554/"):  # eLife
        return f"https://elifesciences.org/articles/{doi.split('.')[-1]}/pdf"
    if doi.startswith("10.5194/"):  # Copernicus (HESS, ACP, GMD, etc.) — fully OA
        # Published format: 10.5194/{journal}-{vol}-{page}-{year}  (4 parts)
        # Discussion/preprint format: 10.5194/{journal}-{year}-{number}  (3 parts, needs redirect)
        suffix = doi[8:]  # e.g. hess-30-2395-2026 or hess-2024-384
        parts = suffix.split("-")
        if len(parts) >= 4:
            journal, vol, page, year = parts[0], parts[1], parts[2], parts[3]
            return f"https://{journal}.copernicus.org/articles/{vol}/{page}/{year}/{suffix}.pdf"
        # 3-part: must follow DOI redirect — handled by _try_copernicus_redirect below
        return None
    if doi.startswith("10.3390/"):  # MDPI — URL requires DOI redirect, handled async below
        return None
    if doi.startswith("10.3389/"):  # Frontiers — fully OA
        return f"https://www.frontiersin.org/articles/{doi}/pdf"
    return None


def _doi_to_campus_pdf(doi: str) -> Optional[str]:
    """Return direct PDF URL for paywalled publishers accessible via campus IP.
    These URLs require no proxy — the publisher authenticates by campus IP range.
    """
    if doi.startswith("10.1007/") or doi.startswith("10.1057/"):  # Springer
        return f"https://link.springer.com/content/pdf/{doi}.pdf"
    if doi.startswith("10.1002/") or doi.startswith("10.1111/"):  # Wiley
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    if doi.startswith("10.1080/") or doi.startswith("10.1081/"):  # T&F
        return f"https://www.tandfonline.com/doi/pdf/{doi}"
    if doi.startswith("10.1021/"):  # ACS Publications
        return f"https://pubs.acs.org/doi/pdf/{doi}"
    if doi.startswith("10.1039/"):  # Royal Society of Chemistry
        return f"https://pubs.rsc.org/en/content/articlepdf/{doi}"
    if doi.startswith("10.1109/"):  # IEEE — needs Playwright (article number varies)
        return None
    if doi.startswith("10.1016/"):  # Elsevier/ScienceDirect — needs Playwright
        return None
    if doi.startswith("10.1093/"):  # Oxford Academic — needs Playwright
        return None
    if doi.startswith("10.1017/"):  # Cambridge — needs Playwright
        return None
    return None


async def _try_mdpi_pdf(doi: str) -> Optional[str]:
    """Follow MDPI DOI redirect to derive the correct article URL, then return PDF URL.
    MDPI DOI suffixes cannot be reliably mapped to URLs without following the redirect.
    """
    if not doi.startswith("10.3390/"):
        return None
    try:
        async with _client(timeout=15) as client:
            r = await client.get(f"https://doi.org/{doi}")
            article_url = str(r.url)
            if "doi.org" in article_url:  # redirect did not resolve
                return None
            return article_url.rstrip("/") + "/pdf"
    except Exception as e:
        log.debug(f"MDPI DOI redirect error: {e}")
    return None


async def _try_copernicus_redirect(doi: str) -> Optional[str]:
    """For Copernicus discussion-paper DOIs (3-part format), follow the DOI redirect
    and derive the final article PDF URL."""
    if not doi.startswith("10.5194/"):
        return None
    parts = doi[8:].split("-")
    if len(parts) >= 4:
        return None  # already handled by _doi_to_publisher_pdf
    try:
        async with _client(timeout=15) as client:
            r = await client.get(f"https://doi.org/{doi}")
            url = str(r.url)
            if "copernicus.org" not in url:
                return None
            if ".html" in url:
                # discussion: .../hess-30-2455-2026-discussion.html → .../hess-30-2455-2026.pdf
                pdf_url = url.replace("-discussion.html", ".pdf").replace(".html", ".pdf")
            else:
                # preprint directory: .../preprints/essd-2026-302/ → .../preprints/essd-2026-302/essd-2026-302.pdf
                path = url.rstrip("/")
                article_code = path.split("/")[-1]
                pdf_url = f"{path}/{article_code}.pdf"
            log.info(f"  Copernicus redirect PDF: {pdf_url}")
            return pdf_url
    except Exception as e:
        log.debug(f"Copernicus redirect error: {e}")
    return None


def _safe_filename(title: str) -> str:
    name = re.sub(r'[^\w\s-]', '', title)[:80].strip()
    return name.replace(" ", "_") + ".pdf"


async def fetch_paper_oa(title: str) -> tuple[Optional[Path], str]:
    """Try all OA sources. Returns (local_pdf_path, source_name) or (None, reason)."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOAD_DIR / _safe_filename(title)

    # Pass 1: quick OA sources
    for fetcher, name in [
        (_try_arxiv_api, "arXiv"),
        (_try_unpaywall, "Unpaywall"),
        (_try_semantic_scholar, "Semantic Scholar"),
        (_try_crossref_oa, "Crossref"),
    ]:
        pdf_url = await fetcher(title)
        if pdf_url:
            log.info(f"  Found via {name}: {pdf_url}")
            if await _download_pdf(pdf_url, dest):
                return dest, name
            log.debug(f"  Download failed for {pdf_url}")

    # Pass 2: get DOI from Crossref, then use it for precise lookups
    meta = await lookup_crossref(title)
    doi = meta.get("doi", "")
    if doi:
        # DOI-based Unpaywall
        pdf_url = await _try_unpaywall_doi(doi)
        if pdf_url:
            log.info(f"  Found via Unpaywall(DOI): {pdf_url}")
            if await _download_pdf(pdf_url, dest):
                return dest, "Unpaywall(DOI)"

        # Direct publisher PDF by DOI pattern (for OA publishers not yet in Unpaywall)
        pdf_url = _doi_to_publisher_pdf(doi)
        if pdf_url:
            log.info(f"  Trying publisher direct PDF: {pdf_url}")
            if await _download_pdf(pdf_url, dest):
                return dest, "Publisher(direct)"
            # httpx failed (JS/cookie wall) — try Playwright
            log.info(f"  httpx failed, trying Playwright for OA publisher: {pdf_url}")
            article_url = pdf_url.replace(".pdf", "")
            from .browser_agent import download_oa_publisher_pdf
            pl_path = await download_oa_publisher_pdf(article_url, dest)
            if pl_path:
                return pl_path, "Publisher(playwright)"

        # Copernicus discussion-paper DOIs (3-part) — follow redirect to get article PDF
        if doi.startswith("10.5194/") and len(doi[8:].split("-")) < 4:
            pdf_url = await _try_copernicus_redirect(doi)
            if pdf_url and await _download_pdf(pdf_url, dest):
                return dest, "Copernicus(redirect)"

        # MDPI — DOI redirect required to construct the correct URL
        if doi.startswith("10.3390/"):
            mdpi_pdf = await _try_mdpi_pdf(doi)
            if mdpi_pdf:
                log.info(f"  MDPI PDF URL: {mdpi_pdf}")
                if await _download_pdf(mdpi_pdf, dest):
                    return dest, "MDPI(direct)"
                # httpx blocked (403) — try Playwright with the article page URL
                article_url = mdpi_pdf.rsplit("/pdf", 1)[0]
                from .browser_agent import download_oa_publisher_pdf
                pl = await download_oa_publisher_pdf(article_url, dest)
                if pl:
                    return pl, "MDPI(playwright)"

        # bioRxiv / medRxiv: DOI resolves to biorxiv.org → Playwright download
        log.info(f"  Checking bioRxiv/medRxiv for DOI: {doi}")
        try:
            async with _client(timeout=10) as client:
                r = await client.get(f"https://doi.org/{doi}",
                                     headers={"User-Agent": "Mozilla/5.0"})
                final_url = str(r.url)
        except Exception:
            final_url = ""
        if "biorxiv.org" in final_url or "medrxiv.org" in final_url:
            log.info(f"  bioRxiv preprint detected: {final_url}")
            # Quick probe: if Cloudflare blocks the PDF URL, skip all browser attempts
            base = "https://www.biorxiv.org" if "biorxiv" in final_url else "https://www.medrxiv.org"
            probe_url = f"{base}/content/{doi}v1.full.pdf"
            try:
                async with _client(timeout=8) as client:
                    probe = await client.get(probe_url, headers={"User-Agent": "Mozilla/5.0"})
                    if probe.status_code == 403:
                        log.warning(f"  bioRxiv Cloudflare block (403) — skipping browser attempts")
                        return None, "未能获取(bioRxiv被Cloudflare拦截)"
                    if probe.status_code == 200 and len(probe.content) > 10_000:
                        dest.write_bytes(probe.content)
                        log.info(f"  bioRxiv direct PDF saved: {dest} ({len(probe.content)//1024}KB)")
                        return dest, "bioRxiv(direct)"
            except Exception:
                pass
            # Try Playwright direct download
            from .browser_agent import download_from_biorxiv
            biorxiv_path = await download_from_biorxiv(doi, dest)
            if biorxiv_path:
                return biorxiv_path, "bioRxiv"
            # bioRxiv detected — skip library (preprints not in OPAC)
            return None, "未能获取(bioRxiv预印本，无法自动下载)"

        # PMC lookup using DOI (most reliable search key)
        log.info(f"  Looking up PMC by DOI: {doi}")
        await _try_pubmed_central(title, doi=doi)

    # Pass 3: playwright PMC download (handles JS challenge)
    pmc_info = _PMC_CACHE.get(title)
    if pmc_info:
        from .browser_agent import download_from_pmc
        pmc_id, filename = pmc_info
        log.info(f"  Playwright PMC download: {pmc_id}/{filename}")
        pmc_path = await download_from_pmc(pmc_id, filename)
        if pmc_path:
            return pmc_path, "PubMed Central"

    journal = meta.get("journal", "")
    publisher = meta.get("publisher", "")

    # Pass 3.5: campus IP direct access — bypasses proxy, publisher authenticates by IP.
    # Runs for ALL papers (OA and paywalled) that haven't been downloaded yet.
    # Nature OA papers that failed Pass 2's Playwright also get another chance here.
    if doi:
        campus_pdf = _doi_to_campus_pdf(doi)
        if campus_pdf:
            log.info(f"  CampusIP direct: {campus_pdf}")
            if await _download_pdf_direct(campus_pdf, dest):
                return dest, "CampusIP(direct)"
            log.info("  CampusIP httpx failed, trying Playwright")

        log.info(f"  CampusIP Playwright: doi={doi}")
        from .browser_agent import download_publisher_campus_ip
        campus_path = await download_publisher_campus_ip(doi, dest)
        if campus_path:
            return campus_path, "CampusIP(playwright)"

    # Skip library for known OA publishers — library adds nothing they don't already have
    _oa_prefixes = ("10.5194/", "10.3390/", "10.1371/", "10.7554/", "10.3389/")
    if doi and (any(doi.startswith(p) for p in _oa_prefixes) or _doi_to_publisher_pdf(doi)):
        log.info(f"  Skipping library — OA publisher, campus IP also failed: {doi}")
        return None, "未能获取(OA出版商，下载失败)"

    # Pass 4: playwright library download (OPAC e-database search)
    log.info("  playwright: trying DLUT library")
    from .browser_agent import download_from_library
    pl_path = await download_from_library(title, doi=doi, journal=journal)
    if pl_path:
        return pl_path, "图书馆(playwright)"

    # Pass 5: browser-use AI agent (DLUT library, persistent session, LLM fallback)
    log.info("  browser-use: trying DLUT library")
    from .browser_use_agent import find_and_download_paper
    lib_path = await find_and_download_paper(
        title, doi=doi, journal=journal, publisher=publisher
    )
    if lib_path:
        return lib_path, "图书馆(browser-use)"

    return None, "未能获取"


async def fetch_all_papers(titles: list[str]) -> list[tuple[str, Optional[Path], str]]:
    """Fetch multiple papers concurrently. Returns list of (title, path, source)."""
    import asyncio
    tasks = [fetch_paper_oa(t) for t in titles]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    output = []
    for title, result in zip(titles, results):
        if isinstance(result, Exception):
            output.append((title, None, str(result)))
        else:
            path, source = result
            output.append((title, path, source))
    return output
