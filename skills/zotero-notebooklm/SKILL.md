---
name: zotero-notebooklm
description: Use when the user wants Codex to organize newly added Zotero papers into a NotebookLM-powered Chinese briefing, Audio Overview, or Video Overview. This skill orchestrates DailyInfo's zotero-brief capability, notebooklm-py authentication, Zotero PDF hydration, upload, generation, download, and manual fallback steps.
---

# Zotero NotebookLM Briefing

Use this skill when the user asks Codex to turn Zotero papers into a NotebookLM briefing, podcast/audio overview, or video overview.

DailyInfo is the capability layer. Codex is the operator. The user may manually complete Google/NotebookLM login or browser steps when required.

For full new-machine setup and human/agent handoff details, read `docs/zotero-notebooklm.md` or `docs/zotero-notebooklm.zh.md` when onboarding a machine or when auth/PDF access is unclear.

## Contract

- Do not use DailyInfo's OpenRouter summarizer for this workflow.
- Use `dailyinfo zotero-brief` as the primary command surface.
- Use `notebooklm-py` via `uv run --extra notebooklm ...`; do not assume the global `notebooklm` command is installed.
- Treat NotebookLM auth as manual-friendly: if auth is missing, give the exact `notebooklm login` command and explain that Codex can continue after login.
- Prefer NotebookLM sources over separate LLM calls: upload copied PDFs plus `source_index.md`, then ask NotebookLM for the Chinese briefing and optional audio/video overview.
- If automation fails, inspect `notebooklm.json` and `MANUAL_NOTEBOOKLM_STEPS.md`; report the next concrete action instead of treating the run as a total failure.

## Standard Workflow

1. Confirm environment from the DailyInfo repo root:

   ```powershell
   $env:UV_CACHE_DIR='D:\Code\dailyinfo\.tmp\uv-cache'
   uv run --extra notebooklm dailyinfo zotero-brief --help
   uv run --extra notebooklm notebooklm doctor
   ```

2. If `notebooklm doctor` reports missing auth, ask the user to complete login with the same profile directory that the run will use:

   ```powershell
   $env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
   $env:UV_CACHE_DIR='D:\Code\dailyinfo\.tmp\uv-cache'
   uv run --extra notebooklm notebooklm login --browser chrome
   ```

   In a normal user terminal, the user may omit `UV_CACHE_DIR`. In Codex sandboxed runs, keep `NOTEBOOKLM_HOME` inside the workspace if the default home directory is not writable.

3. Run a small PDF hydration smoke test when PDFs are on Google Drive or another cloud placeholder path:

   ```powershell
   $env:DAILYINFO_DATA_ROOT='D:\Code\dailyinfo\.tmp\dailyinfo-run'
   $env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
   $env:UV_CACHE_DIR='D:\Code\dailyinfo\.tmp\uv-cache'
   uv run --extra notebooklm dailyinfo zotero-brief --collection water --date 2026-05-27 --manual-only --force --limit 1 --open-missing-pdfs --pdf-wait-seconds 0 --notebooklm-home D:\Code\dailyinfo\.tmp\notebooklm
   ```

   Then read `notebooklm.json`. `open_target` should show a `zotero://open-pdf/library/items/...` URI when hydration was attempted.

4. Run the full workflow:

   ```powershell
   $env:DAILYINFO_DATA_ROOT='D:\Code\dailyinfo\.tmp\dailyinfo-run'
   $env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
   $env:UV_CACHE_DIR='D:\Code\dailyinfo\.tmp\uv-cache'
   uv run --extra notebooklm dailyinfo zotero-brief --collection water --date 2026-05-27 --artifact audio --force --open-missing-pdfs --notebooklm-home D:\Code\dailyinfo\.tmp\notebooklm
   ```

   Adjust `--collection`, `--date`, and `--artifact none|audio|video|both` to match the user's request.

5. Verify by reading the output directory:

   - `source_index.md`: paper metadata and NotebookLM instructions.
   - `pdfs/`: copied PDFs. Empty is acceptable only if the user will complete upload manually.
   - `briefing.md`: generated Chinese briefing or a placeholder with the blocker.
   - `notebooklm.json`: notebook/source/artifact ids and errors.
   - `audio_overview.mp3` or `video_overview.mp4`: present only after successful download.
   - `MANUAL_NOTEBOOKLM_STEPS.md`: browser fallback steps.

## Failure Handling

- Auth missing: run or ask the user to run `notebooklm login` with the same `NOTEBOOKLM_HOME`, then rerun `dailyinfo zotero-brief`.
- Zotero not reachable: tell the user to open Zotero Desktop and ensure the local API on `127.0.0.1:23119` is enabled.
- PDFs missing or access denied: rerun with `--open-missing-pdfs`; if Codex still cannot read cloud-drive paths, the user should hydrate/open the files in Zotero or run the same command from a normal terminal.
- NotebookLM upload/generation failed: use the already written `source_index.md`, `pdfs/`, and `briefing_prompt.md` in the NotebookLM web UI, then save results into the output directory.

## Reporting

Report:

- Date, collection, number of papers found, and output directory.
- Whether NotebookLM auth was ready.
- How many PDFs copied and which blocker remains, if any.
- Whether briefing/audio/video artifacts were created.
- The next manual step if automation stopped.
