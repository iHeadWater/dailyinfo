"""DLUT library download via playwright (WebVPN + SSO login → OPAC → e-database full text)."""
import os
import re
from pathlib import Path
from typing import Optional

from .config import DOWNLOAD_DIR, LIB_USERNAME, LIB_PASSWORD
from .utils import get_logger

log = get_logger("browser_agent")

_PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("http_proxy")
    or None
)

WEBVPN_URL = "https://webvpn.dlut.edu.cn/login"
# Library OPAC via WebVPN
OPAC_URL = "https://opac.lib.dlut.edu.cn/space/database"

# Selectors that signal a full-text / database link on the OPAC detail page
_FULLTEXT_SELECTORS = [
    "a:has-text('全文')",
    "a:has-text('在线阅读')",
    "a:has-text('电子全文')",
    "a:has-text('Full Text')",
    "a:has-text('PDF')",
    "a[href*='doi.org']",
    "a[href*='ieee.org']",
    "a[href*='sciencedirect']",
    "a[href*='springer']",
    "a[href*='acm.org']",
    "a[href*='wiley']",
    "a[href*='nature.com']",
    "a[href*='science.org']",
    "a[href*='download']",
    "a[href*='.pdf']",
]


async def _login_webvpn(page) -> bool:
    """Login via WebVPN → SSO. Returns True on success."""
    await page.goto(WEBVPN_URL, timeout=30000)
    await page.wait_for_timeout(2000)

    # Already logged in (persistent session)
    if "webvpn.dlut.edu.cn" in page.url and "login" not in page.url:
        log.info(f"WebVPN: already logged in at {page.url}")
        return True

    await page.locator("#cas-login").click()
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(2000)

    if "sso.dlut.edu.cn" not in page.url:
        log.warning(f"Unexpected SSO URL: {page.url}")
        return False

    await page.fill("#un", LIB_USERNAME)
    await page.fill("#pd", LIB_PASSWORD)
    await page.locator("a#index_login_btn, .login-btn, button[type='submit']").first.click()
    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(2000)

    success = "webvpn.dlut.edu.cn" in page.url and "login" not in page.url
    log.info(f"WebVPN login {'success' if success else 'failed'}: {page.url}")
    return success


async def _try_download_link(page, dest: Path) -> Optional[Path]:
    """Scan current page for full-text/PDF links and attempt download."""
    for sel in _FULLTEXT_SELECTORS:
        try:
            links = await page.locator(sel).all()
            for link in links[:3]:
                href = await link.get_attribute("href") or ""
                log.info(f"  Trying fulltext link ({sel}): {href[:80]}")
                try:
                    async with page.expect_download(timeout=20000) as dl_info:
                        await link.click()
                    download = await dl_info.value
                    await download.save_as(dest)
                    if dest.exists() and dest.stat().st_size > 1000:
                        log.info(f"  Downloaded via {sel}: {dest.stat().st_size // 1024}KB")
                        return dest
                except Exception:
                    # Link opened a new page instead of triggering download
                    new_page = None
                    try:
                        async with page.context.expect_page(timeout=5000) as page_info:
                            await link.click()
                        new_page = await page_info.value
                        await new_page.wait_for_load_state("networkidle", timeout=15000)
                        result = await _try_download_link(new_page, dest)
                        if result:
                            return result
                    except Exception as e:
                        log.debug(f"  New page attempt failed: {e}")
                    finally:
                        if new_page:
                            await new_page.close()
        except Exception as e:
            log.debug(f"  {sel}: {e}")
    return None


async def _navigate_to_library(context, portal_page):
    """Click 图书馆 from WebVPN portal. Returns the page to use (new tab or same tab)."""
    # Remove target="_blank" so same-tab click is possible
    try:
        await portal_page.evaluate(
            "document.querySelectorAll('a').forEach("
            "function(a){if(a.href&&a.href.indexOf('lib.dlut.edu.cn')>-1)"
            "a.removeAttribute('target')})"
        )
    except Exception:
        pass

    prev_url = portal_page.url
    for sel in ["a[href*='lib.dlut.edu.cn']", "div[title='图书馆']"]:
        try:
            el = portal_page.locator(sel).first
            if not await el.is_visible(timeout=3000):
                continue
            # Watch for new tab first; if none opens within 5s, check same-tab
            try:
                async with context.expect_page(timeout=5000) as new_page_info:
                    await el.click()
                new_page = await new_page_info.value
                await new_page.wait_for_load_state("networkidle", timeout=20000)
                log.info(f"Library new tab: {new_page.url}")
                return new_page
            except Exception:
                pass
            # Same-tab navigation?
            await portal_page.wait_for_timeout(2000)
            if portal_page.url != prev_url:
                log.info(f"Library same-tab: {portal_page.url}")
                return portal_page
        except Exception as e:
            log.debug(f"Library nav ({sel}): {e}")

    # Fall back to hardcoded OPAC URL in portal page
    log.info(f"Portal nav failed, trying OPAC_URL: {OPAC_URL}")
    await portal_page.goto(OPAC_URL, timeout=30000)
    await portal_page.wait_for_load_state("networkidle", timeout=15000)
    log.info(f"OPAC URL result: {portal_page.url}")
    return portal_page


async def _navigate_to_edb(lib_page) -> object:
    """From the library homepage, click 电子数据库 to reach the OPAC database search page."""
    # Already on the OPAC database page (e.g. _navigate_to_library fell back directly)
    if "opac.lib.dlut.edu.cn" in lib_page.url or "space/database" in lib_page.url:
        log.info(f"Already on OPAC database page: {lib_page.url}")
        return lib_page

    # Try clicking the 电子数据库 link
    for sel in [
        "a:has-text('电子数据库')",
        "a[href*='opac.lib.dlut.edu.cn']",
        "a[href*='space/database']",
        "a:has-text('数据库')",
    ]:
        try:
            el = lib_page.locator(sel).first
            if not await el.is_visible(timeout=3000):
                continue
            href = await el.get_attribute("href") or ""
            log.info(f"Clicking EDB link ({sel}): {href[:80]}")
            try:
                async with lib_page.context.expect_page(timeout=5000) as pg_info:
                    await el.click()
                new_page = await pg_info.value
                await new_page.wait_for_load_state("networkidle", timeout=20000)
                log.info(f"EDB new tab: {new_page.url}")
                return new_page
            except Exception:
                pass
            await lib_page.wait_for_load_state("networkidle", timeout=10000)
            if "opac" in lib_page.url or "database" in lib_page.url:
                log.info(f"EDB same-tab: {lib_page.url}")
                return lib_page
        except Exception as e:
            log.debug(f"EDB nav ({sel}): {e}")

    # Fall back: navigate directly to OPAC database URL
    log.info(f"EDB click failed, going directly to {OPAC_URL}")
    await lib_page.goto(OPAC_URL, timeout=30000)
    await lib_page.wait_for_load_state("networkidle", timeout=15000)
    return lib_page


async def _search_opac(page, query: str, db_search: bool = False) -> bool:
    """Search on the OPAC page. db_search=True uses the database-name box; False uses the
    general article search box. Returns True if at least one result link was found."""
    log.info(f"OPAC page: {page.url} — {await page.title()}")
    await page.wait_for_timeout(2000)  # Vue SPA needs time to render

    # The new OPAC (opac.lib.dlut.edu.cn) uses Element UI — no name attributes.
    # Two visible inputs: database-name box and general search box.
    if db_search:
        search_sel = "input[placeholder='请输入数据库名称']"
    else:
        search_sel = "input[placeholder='可同时输入多个检索条件和检索词']"

    filled = False
    for sel in [search_sel, "input.el-input__inner:visible", "input[type='text']:visible"]:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3000):
                await el.fill(query[:100])
                filled = True
                log.info(f"  Filled search ({sel}): {query[:60]}")
                break
        except Exception:
            continue

    if not filled:
        log.warning("Could not fill OPAC search box")
        return False

    # Submit — try Chinese 检索 first (OPAC), then common English variants
    for btn_sel in [
        "button:has-text('检索'):visible", "button:has-text('检索')",
        "button:has-text('Search'):visible", "button:has-text('Search')",
        "input[value='Search']", "input[type='submit']", "button[type='submit']",
    ]:
        try:
            el = page.locator(btn_sel).first
            if await el.is_visible(timeout=2000):
                await el.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                await page.wait_for_timeout(2000)
                break
        except Exception:
            continue

    log.info(f"  Search results page: {page.url}")

    # Click the first database/result link
    result_selectors = [
        "a[href*='databaseDetail']",   # OPAC database result
        "a[href*='searchDetailLocal']", # OPAC article result
        "a.title", ".result-title a", ".list-item a",
        "td.title a", "h3 a", ".detailLink",
    ]
    for sel in result_selectors:
        try:
            links = await page.locator(sel).all()
            visible = [l for l in links if await l.is_visible()]
            if visible:
                href = await visible[0].get_attribute("href") or ""
                log.info(f"  Clicking result ({sel}): {href[:80]}")
                try:
                    async with page.context.expect_page(timeout=5000) as pg_info:
                        await visible[0].click()
                    new_page = await pg_info.value
                    await new_page.wait_for_load_state("networkidle", timeout=15000)
                    log.info(f"  Result opened in new tab: {new_page.url}")
                    return new_page  # return the new page
                except Exception:
                    await visible[0].click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    log.info(f"  Result opened same-tab: {page.url}")
                    return page
        except Exception:
            continue

    log.warning(f"  No results found for: {query[:60]}")
    return None


async def search_and_download(page, title: str, doi: str = "", journal: str = "") -> Optional[Path]:
    """Search OPAC e-database by journal name then article title/DOI, follow full-text link."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^\w]', '_', title[:60])
    dest = DOWNLOAD_DIR / f"{safe}.pdf"

    db_query = journal or title[:40]

    # Step 1: search for the database/journal by name
    log.info(f"  OPAC database search: {db_query[:60]}")
    db_page = await _search_opac(page, db_query, db_search=True)
    if not db_page:
        log.warning("  Database not found, trying article title as fallback")
        db_page = await _search_opac(page, title, db_search=True)
    if not db_page:
        return None

    # Step 2: on the database detail page, search for the article
    log.info(f"  Searching article: {doi or title[:60]}")
    article_query = doi if doi else title
    article_page = await _search_opac(db_page, article_query, db_search=False)
    if article_page:
        result = await _try_download_link(article_page, dest)
        if result:
            return result

    # Fallback: try download links on wherever we ended up
    result = await _try_download_link(db_page, dest)
    return result


async def download_from_pmc(pmc_id: str, filename: str) -> Optional[Path]:
    """Download PDF from PubMed Central.
    Loads the article page with playwright (solves JS challenge / sets cookies),
    then uses those cookies with httpx to fetch the actual PDF bytes.
    """
    try:
        from playwright.async_api import async_playwright
        import httpx
    except ImportError:
        log.debug("playwright not installed")
        return None

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-proxy-server"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        )
        page = await context.new_page()
        try:
            article_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/"
            log.info(f"PMC: loading article page {article_url}")
            await page.goto(article_url, timeout=30000)
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)

            # Discover real PDF filename from the page's links
            pdf_href = ""
            for sel in ["a[href$='.pdf']", "a:has-text('PDF')"]:
                links = await page.locator(sel).all()
                for link in links:
                    href = await link.get_attribute("href") or ""
                    if ".pdf" in href and "_sm" not in href and "supp" not in href.lower():
                        pdf_href = href
                        break
                if pdf_href:
                    break

            if not pdf_href:
                pdf_href = f"pdf/{filename}"
            if not pdf_href.startswith("http"):
                pdf_href = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/{pdf_href.lstrip('/')}"
            log.info(f"  PMC PDF URL: {pdf_href}")

            # Extract cookies from the browser context
            cookies = await context.cookies()
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

            # Download PDF with httpx using those cookies
            async with httpx.AsyncClient(
                proxy=_PROXY,
                follow_redirects=True,
                timeout=60,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                    "Cookie": cookie_header,
                    "Referer": article_url,
                },
            ) as client:
                resp = await client.get(pdf_href)
                if resp.status_code == 200 and b"%PDF" in resp.content[:10]:
                    safe = re.sub(r'[^\w]', '_', filename.replace('.pdf', ''))
                    dest = DOWNLOAD_DIR / f"{safe}.pdf"
                    dest.write_bytes(resp.content)
                    log.info(f"  PMC PDF saved: {dest} ({dest.stat().st_size // 1024}KB)")
                    return dest
                log.debug(f"  PMC httpx response: {resp.status_code}, not a PDF")
        except Exception as e:
            log.error(f"PMC download error for {pmc_id}: {e}")
        finally:
            await browser.close()
    return None


async def download_from_biorxiv(doi: str, dest: Path) -> Optional[Path]:
    """Download bioRxiv/medRxiv preprint PDF using Playwright.
    Clicks the PDF download link directly so Cloudflare passes the full browser context.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.debug("playwright not installed")
        return None

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    for base in ["https://www.biorxiv.org", "https://www.medrxiv.org"]:
        article_url = f"{base}/content/{doi}"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-proxy-server"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                accept_downloads=True,
            )
            page = await context.new_page()
            try:
                log.info(f"bioRxiv: loading {article_url}")
                await page.goto(article_url, timeout=30000)
                await page.wait_for_load_state("domcontentloaded", timeout=20000)

                # Find PDF download link (exclude +html and supplementary)
                pdf_link = None
                for sel in ["a[href$='.full.pdf']", "a[data-article-pdf]", "a:has-text('Full Text PDF')", "a:has-text('PDF')"]:
                    links = await page.locator(sel).all()
                    for link in links:
                        href = await link.get_attribute("href") or ""
                        if "full.pdf" in href and "+html" not in href and "supp" not in href.lower():
                            pdf_link = link
                            log.info(f"  bioRxiv PDF link: {href}")
                            break
                    if pdf_link:
                        break

                if not pdf_link:
                    log.debug(f"  No PDF link found on {article_url}")
                    continue

                async with page.expect_download(timeout=60000) as dl_info:
                    await pdf_link.click()
                download = await dl_info.value
                await download.save_as(dest)
                if dest.exists() and dest.stat().st_size > 10_000:
                    log.info(f"  bioRxiv PDF saved: {dest} ({dest.stat().st_size // 1024}KB)")
                    return dest
                log.debug(f"  bioRxiv download too small: {dest.stat().st_size if dest.exists() else 0}B")
            except Exception as e:
                log.error(f"bioRxiv download error ({base}): {e}")
            finally:
                await browser.close()
    return None


async def download_oa_publisher_pdf(article_url: str, dest: Path) -> Optional[Path]:
    """Download an OA publisher PDF via Playwright (handles JS/cookie requirements).
    Navigates to the article page, dismisses cookie banners, then triggers PDF download.
    Works for Nature, PLOS, eLife, Science, and similar OA publishers.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.debug("playwright not installed")
        return None

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        # Use campus IP (no proxy) — works for OA publishers and subscription publishers alike
        browser = await pw.chromium.launch(headless=True, args=["--no-proxy-server"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            log.info(f"OA publisher: loading {article_url}")
            await page.goto(article_url, timeout=30000)
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)  # let JS render after DOM load

            # Dismiss common cookie/GDPR banners
            for btn_sel in [
                "button:has-text('Accept all')", "button:has-text('Accept')",
                "button:has-text('I agree')", "button:has-text('Agree')",
                "#onetrust-accept-btn-handler", ".cc-accept", "[aria-label='Accept cookies']",
            ]:
                try:
                    btn = page.locator(btn_sel).first
                    if await btn.is_visible(timeout=1500):
                        await btn.click()
                        await page.wait_for_load_state("networkidle", timeout=8000)
                        await page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass

            # Try PDF download button — check is_visible() to pick the right instance.
            # Note: nature.com names the article PDF href ending in "_reference.pdf",
            # so we do NOT filter on "reference" in href; only skip clearly supplementary files.
            pdf_selectors = [
                "[data-article-pdf='true']",
                "[data-test='download-pdf']",
                "a:has-text('Download PDF')",
                "a:has-text('PDF')",
                "a[href$='.pdf']",
            ]
            for sel in pdf_selectors:
                try:
                    links = await page.locator(sel).all()
                    for link in links:
                        if not await link.is_visible():
                            continue
                        href = await link.get_attribute("href") or ""
                        if "supp" in href.lower() or href.endswith("_sm.pdf"):
                            continue
                        log.info(f"  OA publisher PDF link ({sel}): {href[:80]}")
                        try:
                            async with page.expect_download(timeout=30000) as dl_info:
                                await link.click()
                            download = await dl_info.value
                            await download.save_as(dest)
                            if dest.exists() and dest.stat().st_size > 10_000:
                                log.info(f"  OA publisher PDF saved: {dest} ({dest.stat().st_size // 1024}KB)")
                                return dest
                        except Exception as e:
                            log.debug(f"  click download failed ({sel}): {e}")
                except Exception as e:
                    log.debug(f"  selector {sel}: {e}")

            # Fallback: navigate directly to PDF URL (works for nature.com, eLife, Frontiers, MDPI)
            for suffix in [".pdf", "/pdf"]:
                direct_pdf = article_url.rstrip("/") + suffix
                if direct_pdf == article_url:
                    continue
                try:
                    log.info(f"  OA publisher: direct PDF navigation to {direct_pdf[:80]}")
                    async with page.expect_download(timeout=30000) as dl_info:
                        await page.goto(direct_pdf, timeout=30000)
                    download = await dl_info.value
                    await download.save_as(dest)
                    if dest.exists() and dest.stat().st_size > 10_000:
                        log.info(f"  OA publisher PDF saved (direct nav): {dest} ({dest.stat().st_size // 1024}KB)")
                        return dest
                except Exception as e:
                    log.debug(f"  direct PDF nav failed ({suffix}): {e}")

        except Exception as e:
            log.error(f"OA publisher download error for {article_url}: {e}")
        finally:
            await browser.close()
    log.warning(f"OA publisher: no PDF found for {article_url[:80]}")
    return None


async def download_publisher_campus_ip(doi: str, dest: Path) -> Optional[Path]:
    """Download a paywalled paper using campus IP authentication (no proxy).
    Follows the DOI to the publisher's article page, dismisses cookie banners,
    then attempts PDF download via button click or direct URL navigation.
    Works for Elsevier, Springer, Wiley, IEEE, T&F, ACS, etc.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-proxy-server"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        try:
            log.info(f"CampusIP: navigating to https://doi.org/{doi}")
            await page.goto(f"https://doi.org/{doi}", timeout=30000)
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
            article_url = page.url
            log.info(f"CampusIP: article page {article_url}")

            # Dismiss cookie/GDPR banners
            for btn_sel in [
                "button:has-text('Accept all')", "button:has-text('Accept cookies')",
                "button:has-text('Accept')", "button:has-text('I agree')",
                "button:has-text('同意')", "button:has-text('接受')",
                "#onetrust-accept-btn-handler", ".cc-accept",
            ]:
                try:
                    btn = page.locator(btn_sel).first
                    if await btn.is_visible(timeout=1500):
                        await btn.click()
                        await page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass

            # PDF download button selectors covering major publishers
            pdf_selectors = [
                # Elsevier/ScienceDirect
                "a.pdf-download", ".pdf-download-btn-link",
                # Springer
                "a.c-pdf-download__link", ".c-pdf-download a",
                # Wiley
                "a[href*='doi/pdfdirect']", "a[href*='/doi/pdf/']",
                # IEEE
                "a.stats-document-lh-action-downloadPdf_2",
                # T&F / ACS / generic
                "a.show-pdf", "[data-article-pdf='true']",
                "[data-test='download-pdf']", "a:has-text('Download PDF')",
                "a:has-text('Full Text PDF')", "a:has-text('PDF')",
                "a[href$='.pdf']",
            ]
            for sel in pdf_selectors:
                try:
                    links = await page.locator(sel).all()
                    for link in links:
                        if not await link.is_visible():
                            continue
                        href = await link.get_attribute("href") or ""
                        if "supp" in href.lower() or href.endswith("_sm.pdf"):
                            continue
                        log.info(f"  CampusIP PDF link ({sel}): {href[:80]}")
                        try:
                            async with page.expect_download(timeout=30000) as dl_info:
                                await link.click()
                            download = await dl_info.value
                            await download.save_as(dest)
                            if dest.exists() and dest.stat().st_size > 10_000:
                                log.info(f"  CampusIP PDF saved: {dest} ({dest.stat().st_size // 1024}KB)")
                                return dest
                        except Exception as e:
                            log.debug(f"  CampusIP click failed ({sel}): {e}")
                except Exception:
                    pass

            # Fallback: navigate directly to article_url + .pdf or /pdf
            for suffix in [".pdf", "/pdf"]:
                direct_pdf = article_url.rstrip("/") + suffix
                try:
                    log.info(f"  CampusIP direct PDF nav: {direct_pdf[:80]}")
                    async with page.expect_download(timeout=30000) as dl_info:
                        await page.goto(direct_pdf, timeout=30000)
                    download = await dl_info.value
                    await download.save_as(dest)
                    if dest.exists() and dest.stat().st_size > 10_000:
                        log.info(f"  CampusIP PDF saved (direct nav): {dest} ({dest.stat().st_size // 1024}KB)")
                        return dest
                except Exception as e:
                    log.debug(f"  CampusIP direct nav failed ({suffix}): {e}")

        except Exception as e:
            log.error(f"CampusIP download error for {doi}: {e}")
        finally:
            await browser.close()
    log.warning(f"CampusIP: no PDF found for doi={doi}")
    return None


async def download_from_library(title: str, doi: str = "", journal: str = "") -> Optional[Path]:
    """Full flow: WebVPN login → OPAC e-database search → full text → PDF.
    Returns PDF path or None.
    """
    if not LIB_USERNAME or not LIB_PASSWORD:
        log.debug("LIB_USERNAME/LIB_PASSWORD not set")
        return None

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.debug("playwright not installed")
        return None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-proxy-server"])
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        try:
            if not await _login_webvpn(page):
                return None
            # Step 1: navigate to library homepage via WebVPN portal click
            lib_page = await _navigate_to_library(context, page)
            log.info(f"Library page: {lib_page.url}")
            # Step 2: from library homepage, click 电子数据库 to reach OPAC database search
            lib_page = await _navigate_to_edb(lib_page)
            log.info(f"EDB page: {lib_page.url}")
            return await search_and_download(lib_page, title, doi=doi, journal=journal)
        except Exception as e:
            log.error(f"Library download error for '{title}': {e}")
            return None
        finally:
            await browser.close()
