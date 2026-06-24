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

8. Fetch the PDF via browser `fetch()` (preserves cookies/browser context):
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
   Save output to `D:\Code\dailyinfo\ouyang_b64.txt` (allowed path for Playwright MCP).

9. Compute the output path (if the user didn't specify one):
   ```bash
   python scripts/download_pdf.py output-path 10.1016/j.jhydrol.2024.132471
   ```
   This prints the default path (e.g. `~/.myagentdata/dailyinfo/papers/j.jhydrol.2024.132471.pdf`).

10. Decode the PDF using the Python helper:
    ```bash
    python scripts/download_pdf.py decode D:/Code/dailyinfo/temp_b64.txt <output_path>
    ```

11. Clean up the temp base64 file:
    ```bash
    rm D:/Code/dailyinfo/temp_b64.txt
    ```

12. Verify the downloaded PDF:
    ```bash
    python scripts/download_pdf.py verify <output_path>
    ```

### Phase 2: Other Publishers (Extension Points)

Template for adding a new publisher:

12. **Detect**: Add the URL pattern to `detect_publisher()` in `scripts/download_pdf.py`.

13. **Access**: Identify the institutional login flow:
    - Does the publisher support Shibboleth/OpenAthens/CARSI?
    - Is there a "Login via Institution" button?
    - Does it auto-detect from IP/cookies like Elsevier?

14. **Download**: Identify the PDF access pattern:
    - Direct PDF link on article page?
    - "Download PDF" button opening a new tab?
    - Redirect to a CDN (e.g., CloudFront, Akamai, S3)?

15. **Test** with a known-access paper, then add the flow to this SKILL.md.

### Phase 3: Sync to Zotero (linked_file)

After a PDF is downloaded and verified, optionally sync it to Zotero with a linked_file attachment. The PDF is copied to the user's Google Drive papers folder (managed by ZotMoov), and Zotero stores only a pointer — **zero Zotero cloud storage used**.

**Prerequisites (one-time):**
- `ZOTERO_API_KEY` — create at https://www.zotero.org/settings/keys (enable "Allow library access")
- `ZOTERO_LIBRARY_ID` — your numeric user ID (visible on the same page)
- `GDRIVE_PAPERS_PATH` — local path to the Google Drive folder where ZotMoov stores linked PDFs
- Zotero desktop → Preferences → Advanced → Files and Folders → "Linked Attachment Base Directory" set to the same Google Drive folder
- Dependencies: `uv pip install -e ".[zotero]"` (installs pyzotero + zotero-mcp-server)

**Flow:**

16. **Ask the user** if they want to sync to Zotero. If the user invokes the skill with trigger phrases like "下载并加到Zotero", "加到文献库", or "add to Zotero", proceed automatically.

17. **[Optional] Resolve the target collection.** If the user specified a collection name, resolve its key via zotero-mcp:
    ```
    zotero_search_collections(query="<collection_name>")
    ```
    Extract the 8-character key from the result.

18. **Run the sync script:**
    ```bash
    python scripts/zotero_sync.py <pdf_path> <doi> --json
    ```
    This single command:
    - Copies the PDF to `$GDRIVE_PAPERS_PATH/{doi-slug}.pdf`
    - Fetches rich metadata from Crossref (title, authors, journal, year, abstract)
    - Creates a Zotero parent item via pyzotero
    - Creates a linked_file attachment using the portable `attachments:` scheme
    - Outputs JSON: `{"ok": true, "zotero_key": "...", "title": "...", ...}`

19. **[Optional] File the item into a collection.** If a collection key was resolved in step 17:
    ```
    zotero_manage_collections(item_keys=["<zotero_key>"], add_to=["<collection_key>"])
    ```

20. **[Optional] Verify the item was created correctly:**
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
| DOI resolves to wrong page | Navigate directly to `https://www.sciencedirect.com/science/article/pii/<PII>` instead |
| "Access through your organization" modal blocking | Click the modal button first, wait for SSO redirect, then retry |
| Stale element reference | Re-snapshot the page before clicking |
| Base64 file too large (>100MB) | PDF likely contains embedded high-res figures. Try downloading in chunks or accept a lower-resolution version. |
| `python` not `python3` | This environment uses `python`, not `python3` |
| Institutional access expired | Tell user: open browser, go to ScienceDirect, re-authenticate via SSO |

## Reporting

After each download, report:
- Paper title and first author
- File size and page count
- Output path
- Publisher used
- Whether institutional access was automatic or required manual intervention
- If synced to Zotero: item key, collection, and `attachments:` path
