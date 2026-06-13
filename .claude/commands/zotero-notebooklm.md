---
description: Process Zotero papers with NotebookLM via DailyInfo, including auth handoff and audio/video overview generation.
argument-hint: "[collection] [date] [artifact]"
---

# Zotero NotebookLM Briefing

Use this command when the user wants Claude Code to turn newly added Zotero papers into a NotebookLM Chinese briefing, Audio Overview, or Video Overview.

DailyInfo is only the capability layer. Claude Code is the operator.

For first-time setup on a new machine, read `docs/zotero-notebooklm.md` or `docs/zotero-notebooklm.zh.md` before running the workflow.

## Defaults

- Collection: use the first argument, or `water` if omitted.
- Date: use the second argument, or today's local date if omitted.
- Artifact: use the third argument, or `audio` if omitted. Valid values: `none`, `audio`, `video`, `both`.
- NotebookLM profile root: `D:\Code\dailyinfo\.tmp\notebooklm`.
- Data root: `D:\Code\dailyinfo\.tmp\dailyinfo-run`.

## Workflow

1. From the repository root, verify the command surface:

   ```powershell
   $env:UV_CACHE_DIR='D:\Code\dailyinfo\.tmp\uv-cache'
   uv run --extra notebooklm dailyinfo zotero-brief --help
   ```

2. Check NotebookLM auth:

   ```powershell
   $env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
   $env:UV_CACHE_DIR='D:\Code\dailyinfo\.tmp\uv-cache'
   uv run --extra notebooklm notebooklm doctor
   ```

3. If auth is missing, ask the user to complete the visible browser login, then rerun doctor:

   ```powershell
   $env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
   $env:UV_CACHE_DIR='D:\Code\dailyinfo\.tmp\uv-cache'
   uv run --extra notebooklm notebooklm login --browser chrome
   ```

4. Run the workflow. Replace arguments as needed:

   ```powershell
   $env:DAILYINFO_DATA_ROOT='D:\Code\dailyinfo\.tmp\dailyinfo-run'
   $env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
   $env:UV_CACHE_DIR='D:\Code\dailyinfo\.tmp\uv-cache'
   uv run --extra notebooklm dailyinfo zotero-brief --collection water --date 2026-05-28 --artifact audio --force --open-missing-pdfs --notebooklm-home D:\Code\dailyinfo\.tmp\notebooklm
   ```

5. Inspect the output directory:

   ```powershell
   Get-ChildItem D:\Code\dailyinfo\.tmp\dailyinfo-run\zotero
   ```

   Then inspect the specific run's `notebooklm.json`, `briefing.md`, `MANUAL_NOTEBOOKLM_STEPS.md`, and any `audio_overview.mp3` or `video_overview.mp4`.

## Handling Expected Blockers

- If Zotero is not reachable, ask the user to open Zotero Desktop and ensure local API is enabled.
- If PDFs are cloud-only, `--open-missing-pdfs` opens Zotero attachment URIs first. Wait for Google Drive to hydrate files, then rerun.
- If NotebookLM auth succeeds in a normal terminal but this command cannot read `storage_state.json`, run Claude Code from the same local user context and keep `NOTEBOOKLM_HOME` unchanged.
- If NotebookLM automation fails after materials are prepared, continue in the NotebookLM web UI using `source_index.md`, copied PDFs, and `briefing_prompt.md`.

## Report Back

Summarize:

- date, collection, artifact requested
- number of Zotero papers found
- number of PDFs copied
- NotebookLM auth status
- output directory
- whether `briefing.md` and `audio_overview.mp3` or `video_overview.mp4` were created
- exact next manual step if blocked
