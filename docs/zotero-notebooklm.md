# Zotero -> NotebookLM Agent Workflow

[English](zotero-notebooklm.md) | [中文](zotero-notebooklm.zh.md)

This document describes how to set up a new machine so a local agent such as
Claude Code, Codex, or openclaw can turn newly added Zotero papers into a
NotebookLM Chinese briefing and optional Audio/Video Overview.

DailyInfo is the capability layer. The agent is the operator. The human handles
browser login and any Google/NotebookLM prompts that cannot be automated safely.

## Human vs Agent Responsibilities

| Responsibility | Owner |
|----------------|-------|
| Install Zotero, Google Drive, browser, uv, and local agent runtime | Human |
| Sign in to Google/NotebookLM in a browser window | Human, with the agent opening or prompting when possible |
| Keep Zotero Desktop open and confirm the target collection exists | Human |
| Run environment checks, call DailyInfo, inspect status files | Agent |
| Trigger Zotero PDF opening for cloud-file hydration | Agent, with human watching Drive/Zotero if needed |
| Upload/generate/download through NotebookLM automation | Agent |
| Continue manually in NotebookLM web UI when automation breaks | Human, guided by agent and `MANUAL_NOTEBOOKLM_STEPS.md` |

## What This Workflow Does

1. Reads newly added Zotero papers by `dateAdded`.
2. Optionally restricts to a Zotero collection, for example `water`.
3. Finds child PDF attachments through Zotero's local API.
4. Copies readable PDFs into a local run directory.
5. Opens Zotero attachment URIs when PDFs are cloud-only so Google Drive or the
   sync client can hydrate them.
6. Creates or uses a NotebookLM notebook through `notebooklm-py`.
7. Uploads PDFs plus `source_index.md`.
8. Asks NotebookLM to write a Chinese paper briefing.
9. Optionally generates and downloads NotebookLM Audio Overview or Video Overview.
10. If automation fails, leaves a complete local material package and manual
    continuation steps.

It does not call DailyInfo's OpenRouter summarizer.

## Recommended Operator

Use a local agent runtime that can see your real desktop session:

- Claude Code on the same Windows user account is recommended for this workflow.
- openclaw or another local runner is also suitable.
- A heavily sandboxed Codex desktop session may run the CLI but can fail to show
  browser login windows or read `storage_state.json` written by a normal shell.

The key requirement is that the process running the agent can read the same
NotebookLM auth file and the same Zotero/Google Drive PDF paths that the user
sees.

## New Machine Setup

### 1. Install Local Applications

Install and sign in to:

- Zotero Desktop.
- Google Drive for Desktop, if Zotero PDFs are stored in Drive.
- Google Chrome or Microsoft Edge.
- Claude Code, Codex, or the local agent runtime you want to use.
- `uv`.

Open Zotero once and confirm the target collection exists, for example `water`.

### 2. Clone and Install DailyInfo

From PowerShell:

```powershell
git clone <repo-url> D:\Code\dailyinfo
cd D:\Code\dailyinfo
uv sync --python python3
uv pip install -e .
uv pip install -e ".[notebooklm]"
```

For a quick environment check:

```powershell
uv run --extra notebooklm dailyinfo zotero-brief --help
uv run --extra notebooklm notebooklm --help
```

### 3. Confirm Zotero Local API

Keep Zotero Desktop open. Then run a direct local API check:

```powershell
$headers = @{ "Zotero-API-Version" = "3" }
Invoke-RestMethod "http://127.0.0.1:23119/api/users/0/items/top?limit=1" -Headers $headers
```

The important endpoint is:

```text
http://127.0.0.1:23119/api/users/0
```

DailyInfo expects Zotero's local API on `127.0.0.1:23119`. If the API is not
running, enable Zotero's local API / connector support and restart Zotero.

### 4. Choose Stable Local Paths

Use one stable NotebookLM profile directory and one data directory:

```powershell
$env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
$env:DAILYINFO_DATA_ROOT='D:\Code\dailyinfo\.tmp\dailyinfo-run'
```

The same `NOTEBOOKLM_HOME` must be used for both login and later agent runs.
`storage_state.json` contains browser auth cookies; do not commit it.

For repeated local use, the human can place these values in the shell profile
or let the local agent set them per command.

### 5. Authenticate NotebookLM

Authentication is intentionally human-in-the-loop.

Run this from the same local user context that will run Claude Code or your
agent:

```powershell
cd D:\Code\dailyinfo
$env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
uv run --extra notebooklm notebooklm login --browser chrome
```

Complete Google login in the browser window. The command should report:

```text
Login detected.
Authentication saved to: ...\storage_state.json
```

Then verify:

```powershell
$env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
uv run --extra notebooklm notebooklm doctor
```

If Chrome is not usable, try:

```powershell
uv run --extra notebooklm notebooklm login --browser msedge
```

## Claude Code Usage

The repository includes a Claude Code slash command:

```text
.claude/commands/zotero-notebooklm.md
```

In Claude Code, open `D:\Code\dailyinfo` and run:

```text
/zotero-notebooklm water 2026-05-28 audio
```

Argument order:

1. Zotero collection name or key, for example `water`.
2. Date in `YYYY-MM-DD`; omit only if the agent knows today's date.
3. Artifact: `none`, `audio`, `video`, or `both`.

Claude Code should:

1. Check `dailyinfo zotero-brief --help`.
2. Check `notebooklm doctor`.
3. Ask you to complete browser login only if auth is missing.
4. Run `dailyinfo zotero-brief`.
5. Inspect `notebooklm.json`.
6. Report whether `briefing.md`, `audio_overview.mp3`, or `video_overview.mp4`
   were created.
7. Continue from `MANUAL_NOTEBOOKLM_STEPS.md` when NotebookLM automation fails.

## Codex Skill Usage

The repository also contains a Codex skill:

```text
skills/zotero-notebooklm/SKILL.md
```

To install it into a local Codex skills directory on Windows:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.codex\skills" | Out-Null
New-Item -ItemType Junction `
  -Path "$env:USERPROFILE\.codex\skills\zotero-notebooklm" `
  -Target "D:\Code\dailyinfo\skills\zotero-notebooklm"
```

Then restart Codex and ask:

```text
Use the zotero-notebooklm skill to process today's new papers in Zotero collection water and generate a NotebookLM audio overview.
```

If the Codex environment cannot show browser windows or read the NotebookLM auth
file, use Claude Code or a local unsandboxed runner for the actual execution.

## Direct Command

Agents ultimately call this DailyInfo capability:

```powershell
cd D:\Code\dailyinfo
$env:DAILYINFO_DATA_ROOT='D:\Code\dailyinfo\.tmp\dailyinfo-run'
$env:NOTEBOOKLM_HOME='D:\Code\dailyinfo\.tmp\notebooklm'
uv run --extra notebooklm dailyinfo zotero-brief `
  --collection water `
  --date <YYYY-MM-DD> `
  --artifact audio `
  --force `
  --open-missing-pdfs `
  --notebooklm-home D:\Code\dailyinfo\.tmp\notebooklm
```

For video:

```powershell
uv run --extra notebooklm dailyinfo zotero-brief --collection water --artifact video --open-missing-pdfs
```

For local materials only:

```powershell
uv run --extra notebooklm dailyinfo zotero-brief --collection water --manual-only --open-missing-pdfs
```

## Output Directory

By default, output is written under:

```text
%DAILYINFO_DATA_ROOT%\zotero\YYYY-MM-DD[-collection]\
```

Important files:

| File | Purpose |
|------|---------|
| `source_index.md` | Metadata and Chinese reading instructions uploaded to NotebookLM |
| `briefing_prompt.md` | Prompt to paste into NotebookLM chat for manual fallback |
| `pdfs/` | Copied Zotero PDFs |
| `briefing.md` | NotebookLM-generated briefing, or a placeholder if blocked |
| `notebooklm.json` | Notebook/source/artifact ids, copied PDF status, warnings, errors |
| `audio_overview.mp3` | Downloaded Audio Overview, when successful |
| `video_overview.mp4` | Downloaded Video Overview, when successful |
| `MANUAL_NOTEBOOKLM_STEPS.md` | Step-by-step web UI fallback |

## Troubleshooting

| Symptom | Meaning | Action |
|---------|---------|--------|
| `AUTH_REQUIRED` | NotebookLM auth file is missing or unreadable | Run `notebooklm login` with the same `NOTEBOOKLM_HOME` |
| Login succeeds in PowerShell but agent cannot read auth | Agent runs in a different sandbox/user context | Run the agent locally under the same user, or use Claude Code |
| Browser login window is invisible | Agent runtime cannot display local browser windows | Run login manually in PowerShell or use Claude Code |
| Zotero API unreachable | Zotero Desktop is closed or local API disabled | Open Zotero and enable/restart local API |
| PDF status is `missing` with a Google Drive path | File is cloud-only or inaccessible | Open the Zotero attachment, wait for Drive hydration, rerun with `--open-missing-pdfs` |
| `pdfs/` is empty but `source_index.md` exists | Metadata package is ready, PDFs were not copied | Manually upload PDFs in NotebookLM or fix PDF access and rerun |
| NotebookLM upload/generation fails | `notebooklm-py` or NotebookLM UI changed | Use `MANUAL_NOTEBOOKLM_STEPS.md` and complete in the web UI |
| Audio/video is not downloaded | Generation may still be processing or failed | Check `notebooklm.json`, then use NotebookLM Studio manually if needed |

## Agent Reporting Checklist

An agent should end each run with:

- Date and Zotero collection.
- Number of Zotero papers found.
- Number of PDFs copied.
- NotebookLM auth status.
- Notebook id and source ids, if available.
- Whether `briefing.md` was generated by NotebookLM or is a placeholder.
- Whether audio/video artifact was downloaded.
- The exact next manual step if blocked.
