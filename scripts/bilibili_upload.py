#!/usr/bin/env python3
"""Upload audio to Bilibili via biliup (audio → cover → MP4 → upload).

Usage:
    python scripts/bilibili_upload.py audio.mp3 --title "标题" --tags "标签1,标签2"
    python scripts/bilibili_upload.py audio.mp3 --title "标题" --dry-run
    python scripts/bilibili_upload.py audio.mp3 --title "标题" --cover my_cover.png

One-time setup:
    winget install --id=ForgQi.biliup-rs -e
    biliup -u ~/.bilibili/cookies.json login
"""

import argparse
import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path


def log(msg: str) -> None:
    """Print timestamped log message (mirrors run_pipelines.log)."""
    print(f'[{datetime.datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)

# ── constants ──────────────────────────────────────────────────────────
COVER_WIDTH = 1920
COVER_HEIGHT = 1080
DEFAULT_TID = 171  # 科技·人工智能
DEFAULT_COOKIE_PATH = Path.home() / ".bilibili" / "cookies.json"
OUTPUT_DIR = Path(__file__).parent.parent / "output" / "bilibili-upload"

# Windows 微软雅黑，Linux/Mac fallback
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

# ── helpers ────────────────────────────────────────────────────────────


def _find_font() -> str | None:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def _find_biliup() -> str | None:
    """Find biliup binary; checks PATH first, then common install locations."""
    found = shutil.which("biliup")
    if found:
        return found
    # Check winget install location (PATH may not be refreshed)
    local_pkg = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if local_pkg.exists():
        for d in local_pkg.iterdir():
            if d.name.startswith("ForgQi.biliup-rs"):
                for f in d.rglob("biliup.exe"):
                    if f.is_file():
                        return str(f)
    return None


def _resolve_output(audio_path: str | Path, ext: str = ".mp4") -> Path:
    stem = Path(audio_path).stem
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"{stem}{ext}"


# ── cover generation ───────────────────────────────────────────────────


def generate_cover(
    title: str,
    date_str: str = "",
    output_path: str | Path | None = None,
    subtitle: str = "",
) -> Path:
    """Generate a 1920×1080 cover image with gradient background and title text.

    Args:
        title: Main title text (e.g. "AI for Science 周报").
        date_str: Date line (e.g. "2026 第 26 周").
        output_path: Output PNG path; auto-generated from title if not given.
        subtitle: Optional tagline below the date.

    Returns:
        Path to the generated cover PNG.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log("ERROR: Pillow is not installed. Run: uv pip install Pillow")
        log("       or: pip install Pillow")
        sys.exit(1)

    if output_path is None:
        output_path = _resolve_output(title.replace(" ", "_"), ".png")
    output_path = Path(output_path)

    # ── gradient background (deep blue → dark purple) ──
    img = Image.new("RGB", (COVER_WIDTH, COVER_HEIGHT))
    draw = ImageDraw.Draw(img)

    top_color = (15, 23, 42)     # slate-900
    bot_color = (49, 46, 129)    # indigo-900
    for y in range(COVER_HEIGHT):
        ratio = y / COVER_HEIGHT
        r = int(top_color[0] + (bot_color[0] - top_color[0]) * ratio)
        g = int(top_color[1] + (bot_color[1] - top_color[1]) * ratio)
        b = int(top_color[2] + (bot_color[2] - top_color[2]) * ratio)
        draw.line([(0, y), (COVER_WIDTH, y)], fill=(r, g, b))

    # ── fonts ──
    font_path = _find_font()
    try:
        title_font = ImageFont.truetype(font_path, 72) if font_path else ImageFont.load_default()
        date_font = ImageFont.truetype(font_path, 36) if font_path else ImageFont.load_default()
        sub_font = ImageFont.truetype(font_path, 28) if font_path else ImageFont.load_default()
        tag_font = ImageFont.truetype(font_path, 22) if font_path else ImageFont.load_default()
    except Exception:
        title_font = date_font = sub_font = tag_font = ImageFont.load_default()

    # ── text layout ──
    white = (248, 250, 252)
    accent = (148, 163, 184)  # slate-400

    def _center_y(text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        return (COVER_WIDTH - tw) // 2, tw

    # Title
    tx, _ = _center_y(title, title_font)
    draw.text((tx, 360), title, fill=white, font=title_font)

    # Decorative line
    line_y = 470
    draw.line([(860, line_y), (1060, line_y)], fill=accent, width=2)

    # Date
    if date_str:
        dx, _ = _center_y(date_str, date_font)
        draw.text((dx, 500), date_str, fill=accent, font=date_font)

    # Subtitle
    if subtitle:
        sx, _ = _center_y(subtitle, sub_font)
        draw.text((sx, 560), subtitle, fill=accent, font=sub_font)

    # Footer tagline
    footer = "dailyinfo"
    fx, _ = _center_y(footer, tag_font)
    draw.text((fx, COVER_HEIGHT - 80), footer, fill=(100, 116, 139), font=tag_font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, "PNG")
    log(f"  cover generated → {output_path}")
    return output_path


# ── prerequisite checks ────────────────────────────────────────────────


def check_prereqs() -> None:
    """Verify ffmpeg and biliup are available; exit with hint if not."""
    missing = []
    if shutil.which("ffmpeg") is None:
        missing.append(
            "ffmpeg — install from https://ffmpeg.org/download.html\n"
            "       or: winget install ffmpeg"
        )
    if _find_biliup() is None:
        missing.append(
            "biliup — install with:\n"
            "       winget install --id=ForgQi.biliup-rs -e"
        )
    if missing:
        log("ERROR: Required tools not found:")
        for m in missing:
            log(f"  - {m}")
        sys.exit(1)


def check_cookie(cookie_path: str | Path) -> Path:
    """Ensure the biliup cookie file exists; exit with login hint if not."""
    cp = Path(cookie_path).expanduser().resolve()
    if not cp.exists():
        log(f"ERROR: Cookie file not found: {cp}")
        log("  Run once to login (valid ~2 years):")
        log(f"  biliup -u {cp} login")
        sys.exit(1)
    return cp


# ── audio → video conversion ───────────────────────────────────────────


def audio_to_video(
    audio_path: str | Path,
    cover_path: str | Path,
    output_path: str | Path | None = None,
) -> Path:
    """Convert audio + cover image to MP4 via ffmpeg.

    Returns the output video path.
    """
    audio_path = Path(audio_path)
    cover_path = Path(cover_path)
    if output_path is None:
        output_path = _resolve_output(audio_path, ".mp4")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(cover_path),
        "-i", str(audio_path),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path),
    ]
    log(f"  ffmpeg: {audio_path.name} + {cover_path.name} → {output_path.name}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"ERROR: ffmpeg failed:")
        log(result.stderr.strip())
        sys.exit(1)
    log(f"  video saved → {output_path}")
    return output_path


# ── bilibili upload ────────────────────────────────────────────────────


def upload_video(
    video_paths: list[str | Path],
    title: str,
    tid: int,
    tags: str,
    desc: str,
    cover_path: str | Path | None = None,
    cookie_path: str | Path = DEFAULT_COOKIE_PATH,
) -> int:
    """Upload video(s) to Bilibili via biliup CLI (multi-P if >1).

    Returns the biliup exit code (0 = success).
    """
    cookie_path = Path(cookie_path).expanduser().resolve()
    biliup_bin = _find_biliup() or "biliup"
    cmd = [
        biliup_bin, "-u", str(cookie_path),
        "upload",
        *[str(p) for p in video_paths],
        "--title", title,
        "--tid", str(tid),
        "--tag", tags,
        "--desc", desc,
        "--copyright", "1",
    ]
    if cover_path:
        cmd += ["--cover", str(cover_path)]

    log(f"  uploading to Bilibili ({len(video_paths)}P)...")
    log(f"  title: {title}")
    log(f"  tags:  {tags}")
    log(f"  tid:   {tid}")

    result = subprocess.run(cmd)
    if result.returncode != 0:
        log(f"ERROR: biliup exited with code {result.returncode}")
        if result.returncode == 601:
            log("  Hint: 投稿过于频繁，请稍后再试 (code 601)")
    return result.returncode


# ── main flow ──────────────────────────────────────────────────────────


def run_bilibili_upload(
    audio_paths: list[str],
    title: str,
    tags: str = "",
    tid: int = DEFAULT_TID,
    cover: str | None = None,
    desc: str = "",
    cookie_path: str = str(DEFAULT_COOKIE_PATH),
    dry_run: bool = False,
    keep_cover: bool = False,
) -> int:
    """Full pipeline: cover(s) → MP4(s) → upload (multi-P if >1 audio).

    Returns exit code (0 = success, 1 = error, 2 = dry-run OK).
    """
    log(f"=== Bilibili Upload ({len(audio_paths)}P) ===")

    # ── resolve paths ──
    resolved = []
    for ap in audio_paths:
        p = Path(ap).resolve()
        if not p.exists():
            log(f"ERROR: audio file not found: {p}")
            return 1
        resolved.append(p)

    date_str = datetime.date.today().strftime("%Y 第 %W 周")

    # ── covers ──
    if cover:
        cover_path = Path(cover).resolve()
        if not cover_path.exists():
            log(f"ERROR: cover file not found: {cover_path}")
            return 1
        log(f"  using custom cover: {cover_path}")
        cover_paths = [cover_path] * len(resolved)
    else:
        cover_paths = []
        for ap in resolved:
            # Derive per-P subtitle from filename
            fname = ap.stem
            topic_map = {
                "audio_hydrology": "水文 · 径流预测 · 数据同化",
                "audio_ai_foundations": "大模型 · 训练效率 · 推理突破",
                "audio_remote_sensing": "遥感大模型 · SAR/光学融合 · 地球观测",
                "audio_overview": "综述 · 本周精选",
            }
            sub = topic_map.get(fname, "AI for Science · 前沿论文")
            log(f"  generating cover for {fname} ...")
            cp = generate_cover(title=title, date_str=date_str, subtitle=sub)
            cover_paths.append(cp)
        cover_path = cover_paths[0]  # main cover for upload

    # ── convert (ffmpeg needed) ──
    if shutil.which("ffmpeg") is None:
        log("ERROR: ffmpeg not found. Install: winget install ffmpeg")
        return 1

    video_paths = []
    for ap, cp in zip(resolved, cover_paths):
        vp = audio_to_video(ap, cp)
        video_paths.append(vp)

    # ── upload ──
    if dry_run:
        log(f"  [dry-run] skipping upload")
        log(f"  [dry-run] videos: {len(video_paths)} files")
        log(f"  [dry-run] covers: {len(cover_paths)} files")
        log("=== Dry-run complete ===")
        return 2

    # Upload path: need biliup + cookie
    if _find_biliup() is None:
        log("ERROR: biliup not found. Install:")
        log("  winget install --id=ForgQi.biliup-rs -e")
        return 1
    check_cookie(cookie_path)
    rc = upload_video(
        video_paths=video_paths,
        title=title,
        tid=tid,
        tags=tags,
        desc=desc,
        cover_path=cover_path,
        cookie_path=cookie_path,
    )

    # ── cleanup ──
    if not keep_cover and not cover:
        for cp in cover_paths:
            try:
                cp.unlink(missing_ok=True)
            except Exception:
                pass
        log(f"  cleaned up auto covers")

    if rc == 0:
        log("=== Upload complete! ===")
    else:
        log("=== Upload FAILED ===")
    return rc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload audio(s) to Bilibili via biliup (cover → MP4 → upload).\n"
                    "Pass multiple audio files for a multi-P submission.",
    )
    parser.add_argument(
        "audio", nargs="+",
        help="Audio file(s) (.mp3, .m4a, etc.). Multiple = multi-P.",
    )
    parser.add_argument("--title", required=True, help="Video title (max 80 chars)")
    parser.add_argument(
        "--tags", default="", help="Comma-separated tags (e.g. 'AI,科研,周报')"
    )
    parser.add_argument(
        "--tid", type=int, default=DEFAULT_TID,
        help=f"Bilibili partition ID (default: {DEFAULT_TID} 科技·人工智能)",
    )
    parser.add_argument(
        "--cover", default=None,
        help="Custom cover image path; auto-generated if not provided",
    )
    parser.add_argument("--desc", default="", help="Video description")
    parser.add_argument(
        "--cookie-path", default=str(DEFAULT_COOKIE_PATH),
        help="Path to biliup cookies.json",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate cover and MP4 but skip upload",
    )
    parser.add_argument(
        "--keep-cover", action="store_true",
        help="Keep the auto-generated cover after upload",
    )

    args = parser.parse_args()
    rc = run_bilibili_upload(
        audio_paths=args.audio,
        title=args.title,
        tags=args.tags,
        tid=args.tid,
        cover=args.cover,
        desc=args.desc,
        cookie_path=args.cookie_path,
        dry_run=args.dry_run,
        keep_cover=args.keep_cover,
    )
    sys.exit(0 if rc in (0, 2) else rc)


if __name__ == "__main__":
    main()
