---
name: bilibili-upload
description: Upload weekly podcast audio to Bilibili. Converts audio + auto-generated cover to MP4 via ffmpeg, then uploads via biliup CLI. Triggers: "上传到B站", "B站投稿", "上传音频", "bilibili upload", "发布周报音频".
---

# Bilibili Audio Upload

Upload a podcast/audio file to Bilibili as a video (audio + cover image → MP4 via ffmpeg → biliup upload).

## Prerequisites (one-time)

```bash
winget install --id=ForgQi.biliup-rs -e
biliup -u ~/.bilibili/cookies.json login   # scan QR code, valid ~2 years
```

## Workflow

### Step 1: Locate the audio file

Ask the user which audio to upload. Common locations:

- `output/weekly-review/{date}/podcast/audio_hydrology.mp3`
- `output/weekly-review/{date}/podcast/audio_overview.mp3`
- User may provide a custom path

If the user says "this week's podcast" without a path, list available audio files under `output/weekly-review/` and ask which one.

### Step 2: Confirm metadata

Confirm with the user:
- **Title** (required, max 80 chars): e.g. "水文AI周报 2026-W26"
- **Tags** (comma-separated): e.g. "AI,水文,科研,周报"
- **Partition** (tid): default 171 (科技·人工智能)
- **Description** (optional): video description
- **Cover** (optional): custom cover image; auto-generated if omitted

### Step 3: Run the CLI

```bash
dailyinfo bilibili-upload "<audio_path>" \
  --title "<title>" \
  --tags "<tags>" \
  --tid 171
```

Add `--dry-run` to preview without uploading (generates cover + MP4 locally).

Add `--cover <path>` to use a custom cover image.

### Step 4: Report the result

On success, the script outputs the video path. The Bilibili URL format:
`https://www.bilibili.com/video/BV{...}` — the user can find it in their Bilibili创作中心.

## Notes

- Audio is converted to MP4 with a static cover image (Bilibili requires video format)
- Cover is auto-generated with Pillow (1920×1080, gradient background + title + date)
- biliup cookie at `~/.bilibili/cookies.json` is valid for ~2 years
- If upload fails with code 601, wait a few minutes and retry (rate limit)
