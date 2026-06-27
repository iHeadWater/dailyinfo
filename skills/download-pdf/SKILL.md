---
name: download-pdf
description: |
  Download academic paper PDFs through institutional access using Playwright browser automation.
  Supports ScienceDirect (Elsevier) with extension architecture for Springer, Wiley, and other publishers.
  Optional Zotero sync via linked_file attachment (ZotMoov + Google Drive, zero cloud quota).
  Use when the user wants to download a paper by DOI, URL, or PII, or sync a downloaded paper to Zotero.
  Triggers: "下载论文", "download pdf", "download paper", "/download-pdf", "download_pdf",
  "下载并加到Zotero", "同步到Zotero", "加到文献库", "add to Zotero", "save to Zotero".
---

# Download Academic PDFs

Download academic paper PDFs through institutional access (Dalian University of Technology) using Playwright browser automation. The user's browser cookies (WebVPN + Elsevier SSO) enable automatic institutional access — no manual login needed per session.

## Contract

- Use Playwright MCP tools (`browser_navigate`, `browser_click`, `browser_evaluate`) for all browser interaction.
- Delegate PDF decoding to `python scripts/download_pdf.py decode <input> <output>` — never inline base64 decoding logic.
- Always verify the downloaded PDF: check size > 0, file header `%PDF-`, and extract metadata.
- **Default output path**: computed by `python scripts/download_pdf.py output-path <doi|pii>`.

  - Respects ``DAILYINFO_DATA_ROOT`` if set, otherwise defaults to ``~/.myagentdata/dailyinfo/papers/``.
  - Filename is auto-generated from the DOI slug (e.g. ``10.1016/j.jhydrol.2024.132471`` → ``j.jhydrol.2024.132471.pdf``).
  - Use the `-o` flag to override: ``/download-pdf <doi> -o /custom/path/paper.pdf``.

- If institutional access fails, report the blocker and suggest manual browser login steps.

## Architecture: Publisher Plugins

Each publisher has a `(detect, access, download)` triple:

| Publisher | Detect Pattern | Access Method | Download Method |
|-----------|---------------|---------------|-----------------|
| `elsevier` | `sciencedirect.com` | Elsevier IdP SSO (auto-detects DUT) | Click "View PDF" → fetch via browser |

New publishers are added below as we test them. The structure is intentionally simple:
switch on URL pattern → follow publisher-specific steps → save PDF.

## Standard Workflow

### Phase 0: Resolve Input

1. Accept input in any of these forms:
   - **DOI**: `10.1016/j.jhydrol.2024.132471` or `https://doi.org/10.1016/j.jhydrol.2024.132471`
   - **PII**: `S0022169424018675`
   - **ScienceDirect URL**: `https://www.sciencedirect.com/science/article/pii/S0022169424018675`
   - **Direct PDF URL**: `https://pdf.sciencedirectassets.com/.../main.pdf?...`

2. Resolve to article URL:
   - If DOI: navigate to `https://doi.org/<doi>` which redirects to the article page
   - If PII: construct `https://www.sciencedirect.com/science/article/pii/<pii>`
   - If URL: use directly

3. Detect publisher from the resolved URL with `python scripts/download_pdf.py detect <url>`.

### Phase 1: ScienceDirect / Elsevier

This is the only publisher tested end-to-end. The flow:

4. Navigate to the article page:
   ```
   browser_navigate → https://www.sciencedirect.com/science/article/pii/<PII>
   ```

5. Check for institutional access. Evaluate in browser:
   ```javascript
   () => {
     const banners = document.querySelectorAll('[class*="access"], [class*="institution"], [class*="entitled"]');
     const texts = Array.from(banners).map(b => b.textContent.trim()).filter(Boolean);
     const pdfLink = document.querySelector('a[href*="pdfft"]');
     return { accessBanners: texts.slice(0, 5), hasPdfLink: !!pdfLink, url: window.location.href };
   }
   ```
   - Expected: "Dalian University of Technology" or "Full text access" in the banners.
   - If access banner is MISSING: user's SSO session may have expired. Tell the user to:
     1. Open `https://www.sciencedirect.com/` in the shared browser
     2. Click "Access through your organization" → select "Dalian University of Technology"
     3. Complete SSO login (CAPTCHA/verification code if needed)
     4. Then retry.

6. Click the "View PDF" link:
   - The URL pattern is `.../pii/<PII>/pdfft?md5=...&pid=1-s2.0-<PII>-main.pdf`
   - Use selector: `a[href*="<PII>/pdfft"]` or the first `a[href*="/pdfft?"]`
   - This opens the PDF in a new tab (Chrome's built-in PDF viewer).

7. Switch to the PDF tab:
   ```
   browser_tabs(action='select', index=<pdf_tab_index>)
   ```

8. Download the PDF. **CRITICAL**: Use `browser_run_code` with `response.body()` — NEVER use `browser_evaluate` with `fetch() + readAsDataURL()` for PDFs over 1MB. The base64 encoding adds 33% overhead and crashes MCP transport on large papers.

   **Method A (preferred): browser_run_code with response.body()**
   ```javascript
   async (page) => {
     const response = await page.goto(page.url(), { waitUntil: 'networkidle' });
     const contentType = response.headers()['content-type'] || '';
     if (!contentType.includes('pdf')) {
       return { error: 'Not a PDF: ' + contentType, status: response.status() };
     }
     const buffer = await response.body();
     const fs = require('fs');
     const outPath = 'D:/Code/dailyinfo/temp_pdf_download.pdf';
     fs.writeFileSync(outPath, Buffer.from(buffer));
     return { ok: true, size: buffer.length, path: outPath };
   }
   ```

   **Method B (fallback, only for small PDFs < 1MB): base64 via evaluate + decode**
   ```javascript
   async () => {
     const response = await fetch(window.location.href);
     const blob = await response.blob();
     const reader = new FileReader();
     return new Promise(resolve => {
       reader.onloadend = () => resolve({size: blob.size, base64: reader.result});
       reader.readAsDataURL(blob);
     });
   }
   ```
   If using Method B: save output to a temp file, then decode with `python scripts/download_pdf.py decode <input> <output>`.

9. Compute the output path (if the user didn't specify one):
   ```bash
   python scripts/download_pdf.py output-path 10.1016/j.jhydrol.2024.132471
   ```
   This prints the default path (e.g. `~/.myagentdata/dailyinfo/papers/j.jhydrol.2024.132471.pdf`).

10. Copy the downloaded PDF to the final path:
    ```bash
    cp D:/Code/dailyinfo/temp_pdf_download.pdf <output_path>
    ```

11. Clean up the temp file:
    ```bash
    rm D:/Code/dailyinfo/temp_pdf_download.pdf
    ```

12. Verify the downloaded PDF:
    ```bash
    python scripts/download_pdf.py verify <output_path>
    ```

### Phase 2: Nature / Springer

Nature articles use a simple download pattern: click "Download PDF" on the article page, and Chrome's native download manager saves the PDF. No base64 encoding needed.

The Playwright browser profile persists WAYF/SSO cookies, so once you've logged in once, subsequent Nature papers download without re-authentication.

**Access types:**

| Type | Indicator | Action |
|------|-----------|--------|
| **Open Access** | Gold OA badge on article page | Click "Download PDF" directly |
| **Institutional** | Redirects to `wayf.springernature.com` or shows "Access through your institution" | User completes WAYF → DUT SSO login, then click "Download PDF" |
| **Subscription only** | "Buy or subscribe" button, no institutional option, `.pdf` URL redirects to article | Skip |

**Deterministic flow:**

12. Navigate to the article page:
    ```
    mcp__plugin_playwright_playwright__browser_navigate → https://www.nature.com/articles/<article-id>
    ```

13. **If redirected to `wayf.springernature.com`** (institutional login):
    - Take a snapshot to find the institution search box
    - Type "Dalian" in the search box (`browser_type`)
    - Press Enter to show results (`browser_press_key key=Enter`)
    - Click "Dalian University of Technology" from the dropdown
    - **Pause**: tell user to complete DUT SSO login in the browser window, wait for "done"
    - After user confirms, the page returns to the article

14. **Click "Download PDF"**:
    - Grep the snapshot YAML for `Download PDF` to find the ref
    - `browser_click(ref="<ref>")`
    - Chrome's native download manager saves the PDF to `.playwright-mcp/<filename>.pdf`
    - The tool result includes "Downloaded file ... to ..." confirming success

15. **Do NOT use** `browser_evaluate` + `fetch() + readAsDataURL()` — crashes on PDFs >1MB.
    **Do NOT use** `browser_run_code` + `require('fs')` — `require` is not defined in the Playwright MCP runtime.
    **DO use** Chrome native download triggered by clicking the download link.

### Phase 3: Wiley / AGU

Wiley Online Library (including AGU journals on `agupubs.onlinelibrary.wiley.com`) uses Cloudflare Turnstile anti-bot protection. The user must pass the challenge manually once per session.

**Key insight**: Wiley supports a `pdfdirect` endpoint with `?download=true` that triggers Chrome native download without opening the PDF viewer.

**Pattern:**
```
https://onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true
https://agupubs.onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true
```

**Deterministic flow:**

16. **First download of the session**: Navigate to an article page. If Cloudflare Turnstile blocks (page title "请稍候…" or 403 errors), tell the user:
    > Cloudflare 验证拦截了。请在浏览器窗口中手动完成人机验证，完成后回复 "done"。

17. After Cloudflare passed: navigate directly to the `pdfdirect` URL:
    ```
    mcp__plugin_playwright_playwright__browser_navigate → https://agupubs.onlinelibrary.wiley.com/doi/pdfdirect/10.1029/2024EA004157?download=true
    ```
    This triggers Chrome's native download immediately. The PDF lands in `.playwright-mcp/<filename>.pdf`.

18. Check for the downloaded file:
    ```bash
    ls -t .playwright-mcp/*.pdf | head -1
    ```

19. Cloudflare cookies persist in the browser profile, so subsequent Wiley/AGU papers in the same session can skip step 16 and go directly to the `pdfdirect` URL.

### Phase 4: Sync to Zotero (linked_file)

After a PDF is downloaded and verified, optionally sync it to Zotero with a linked_file attachment. The PDF is copied to the user's Google Drive papers folder (managed by ZotMoov), and Zotero stores only a pointer — **zero Zotero cloud storage used**.

**Prerequisites (one-time):**
- `ZOTERO_API_KEY` — create at https://www.zotero.org/settings/keys (enable "Allow library access")
- `ZOTERO_LIBRARY_ID` — your numeric user ID (visible on the same page)
- `GDRIVE_PAPERS_PATH` — local path to the Google Drive folder where ZotMoov stores linked PDFs
- Zotero desktop → Preferences → Advanced → Files and Folders → "Linked Attachment Base Directory" set to the same Google Drive folder
- Dependencies: `uv pip install -e ".[zotero]"` (installs pyzotero)

**Flow:**

20. **Ask the user** if they want to sync to Zotero. If the user invokes the skill with trigger phrases like "下载并加到Zotero", "加到文献库", or "add to Zotero", proceed automatically.

21. **[Optional] Resolve the target collection.** If the user specified a collection name, resolve its key via zotero-mcp:
    ```
    zotero_search_collections(query="<collection_name>")
    ```
    Extract the 8-character key from the result.

22. **Run the sync script.** ⚠️ **MUST use `uv run python`**, NOT bare `python` (conda Python lacks pyzotero):
    ```bash
    uv run python scripts/zotero_sync.py <pdf_path> <doi> --json
    ```
    This single command:
    - Copies the PDF to `$GDRIVE_PAPERS_PATH/{doi-slug}.pdf`
    - Fetches rich metadata from Crossref (title, authors, journal, year, abstract)
    - Creates a Zotero parent item via pyzotero
    - Creates a linked_file attachment using the portable `attachments:` scheme
    - Outputs JSON: `{"ok": true, "zotero_key": "...", "title": "...", ...}`

23. **[Optional] File the item into a collection.** If a collection key was resolved in step 21:
    ```
    zotero_manage_collections(item_keys=["<zotero_key>"], add_to=["<collection_key>"])
    ```

24. **[Optional] Verify the item was created correctly:**
    ```
    zotero_get_item_metadata(item_key="<zotero_key>")
    ```
    Confirm the title, DOI, and linked_file attachment are present.

21. **Report** the Zotero item key, title, collection, and local file path.

**How linked_file works:**

The script uses Zotero's portable `attachments:<filename>` scheme. Zotero resolves this against the "Linked Attachment Base Directory" set in Zotero preferences. The PDF lives in Google Drive (managed by ZotMoov), so:
- Zotero cloud storage quota is never touched
- The PDF is available on all machines with the same Google Drive + Zotero setup
- ZotMoov handles file organization within the linked folder

**Zotero Sync Failure Handling:**

| Scenario | Action |
|----------|--------|
| `ZOTERO_API_KEY` / `ZOTERO_LIBRARY_ID` not set | Report missing env vars; PDF is already saved locally |
| `GDRIVE_PAPERS_PATH` not set | Report missing env var; PDF is already saved locally |
| Crossref metadata fetch fails | Script falls back to PDF-embedded metadata (title, author, DOI) |
| Zotero API write fails | Report error; PDF is already in GDrive folder — user can manually link |
| Collection not found | Skip collection filing; item goes to "My Library" root |
| `attachments:` path not resolving | User needs to configure "Linked Attachment Base Directory" in Zotero preferences |

## Failure Handling

| Scenario | Action |
|----------|--------|
| DOI resolves to wrong page | Navigate directly to the publisher's article URL instead |
| "Access through your organization" modal blocking | Click the modal button first, wait for SSO redirect, then retry |
| Stale element reference | Re-snapshot the page before clicking |
| **MCP crash on `browser_evaluate` + base64** | **NEVER use this pattern for PDFs.** Use Chrome native download: click the download link or navigate to `pdfdirect?download=true`. |
| `require('fs')` not defined in `browser_run_code` | Playwright MCP runtime has no Node.js `require`. Use Chrome native download instead. |
| `python` lacks `pyzotero` (ModuleNotFoundError) | **Always use `uv run python`** for zotero_sync.py. Conda Python doesn't have project dependencies. |
| **Wiley/AGU: "请稍候…" page or 403 errors** | Cloudflare Turnstile blocking. Tell user to pass the challenge manually in the browser window, then retry. |
| **Nature `.pdf` URL redirects to article page** | Paper requires institutional access. Follow Phase 2 WAYF login flow, then click "Download PDF" on the article page. |
| **Nature Reviews journals: no "Download PDF" link** | DUT may not subscribe to that Nature-branded journal. Check for "Buy or subscribe" vs "Access via your institution". |
| `zotero_sync.py` prints `UnicodeEncodeError: 'gbk'` | Windows terminal encoding issue. Sync itself succeeds — the error is only in the print output. |
| Playwright MCP "No such tool available" | Use `mcp__plugin_playwright_playwright__*` (standalone Chromium from official plugin). NOT `plugin_ecc_playwright` (needs Chrome extension). |
| `zotero-mcp` in local-only mode (can't write) | Use `scripts/zotero_sync.py` for writes (uses Web API). zotero-mcp is read-only / collection management only. |
| Institution login needs manual intervention | **Pause and wait for user**. Tell user what to do in the browser, wait for "done" signal, then continue. |

## Deterministic Patterns (for agents)

These are the ONLY patterns to use. Do not deviate.

### Download: Nature OA paper
```
1. browser_navigate → https://www.nature.com/articles/<id>
2. Grep snapshot for "Download PDF" ref
3. browser_click(ref="<ref>")  → triggers Chrome native download
4. File lands in .playwright-mcp/<name>.pdf
```

### Download: Nature institutional-access paper
```
1. browser_navigate → https://www.nature.com/articles/<id>
2. If WAYF redirect: type "Dalian" → Enter → click "Dalian University of Technology"
3. ⚠️ PAUSE: tell user to complete DUT SSO login, wait for "done"
4. After login: Grep for "Download PDF" ref, click it
```

### Download: Wiley/AGU paper (any access type)
```
1. browser_navigate → https://onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true
   OR: https://agupubs.onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true
2. If Cloudflare blocks: ⚠️ PAUSE: tell user to pass challenge, wait for "done"
3. Chrome native download triggers automatically
4. File lands in .playwright-mcp/<name>.pdf
```

### Sync to Zotero (all publishers)
```
1. uv run python scripts/zotero_sync.py <pdf_path> <doi> --json
2. Output: {"ok": true, "zotero_key": "...", ...}
```

## Reporting

After each download, report:
- Paper title and first author
- File size and page count
- Output path
- Publisher used
- Whether institutional access was automatic or required manual intervention
- If synced to Zotero: item key, collection, and `attachments:` path
