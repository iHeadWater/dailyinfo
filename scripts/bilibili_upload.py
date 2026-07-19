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
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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


def _copy_to_dir(src: Path, dst_dir: Path) -> Path | None:
    """Copy file to directory; return None if source doesn't exist."""
    if not src.exists():
        return None
    import shutil as _shutil
    dst = dst_dir / src.name
    _shutil.copy2(src, dst)
    return dst


# ── theme context resolution ───────────────────────────────────────────

@dataclass
class CardInfo:
    """Metadata extracted from a single analysis card."""
    path: Path
    doi: str | None = None
    title: str | None = None
    author_surname: str = ""

@dataclass
class ThemeContext:
    """Resolved context for one audio file's theme."""
    theme: str = ""
    subtitle: str = ""
    papers: list[CardInfo] = field(default_factory=list)

_COVER_SUBTITLE_FALLBACK: dict[str, str] = {
    "hydrology":       "水文 · 径流预测 · 数据同化",
    "ai_foundations":  "大模型 · 训练效率 · 推理突破",
    "ai-index":        "AI Index 2026 · 科学趋势",
    "datasets":        "全球数据集 · CAMELS · 干旱",
    "neural-operator": "Neural Operator · 离散化无关",
    "remote_sensing":  "遥感大模型 · SAR/光学融合 · 地球观测",
}

_PAPER_REF_RE = re.compile(
    r'[*]{0,2}([A-Z][A-Za-z\xe9\xe8\xea\xeb\xe0\xe2\xe4\xf9\xfb\xfc\xf4'
    r'\xee\xef\xe7ŠČŘŽ\xc1\xc9\xcd\xd3\xda\xdd'
    r'ŇĎŤ\xc4\xcb\xcf\xd6\xdc]+)\s+et\s+al\.[*]{0,2}'
)

_DATE_SEGMENT_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _ascii_fold(text: str) -> str:
    """Fold unicode to ASCII for author name matching (Thébault → thebault)."""
    nfkd = unicodedata.normalize('NFKD', text)
    return nfkd.encode('ascii', 'ignore').decode('ascii').lower()


def _find_weekly_review_root(audio_path: Path) -> Path | None:
    """Walk up from audio path to find 'weekly-review/YYYY-MM-DD' root."""
    parts = audio_path.resolve().parts
    for i, part in enumerate(parts):
        if part == "weekly-review" and i + 1 < len(parts):
            if _DATE_SEGMENT_RE.match(parts[i + 1]):
                return Path(*parts[:i + 2])
    return None


def _audio_stem_to_theme(stem: str) -> str | None:
    """Extract theme from audio stem: 'audio_hydrology_v1' → 'hydrology'."""
    if not stem.startswith('audio_'):
        return None
    theme = stem[len('audio_'):]
    return re.sub(r'_v\d+$', '', theme)


def _find_podcast_md(audio_dir: Path, theme: str) -> Path | None:
    """Find podcast_{theme}.md in the audio's directory."""
    candidate = audio_dir / f"podcast_{theme}.md"
    return candidate if candidate.exists() else None


def _extract_author_surnames(text: str) -> list[str]:
    """Parse 'Author et al.' references from text."""
    return _PAPER_REF_RE.findall(text)


def _find_card_for_author(author_surname: str, cards_dir: Path) -> Path | None:
    """Match author surname to a card file by filename prefix."""
    if not cards_dir.exists():
        return None
    candidates = [_ascii_fold(author_surname)]
    # For hyphenated names, also try just the first part (Rodriguez-Pardo → rodriguez)
    if '-' in author_surname:
        candidates.append(_ascii_fold(author_surname.split('-')[0]))
    for target in candidates:
        for card_path in sorted(cards_dir.iterdir()):
            if not card_path.name.endswith('.md'):
                continue
            if _ascii_fold(card_path.stem).startswith(f"{target}_"):
                return card_path
    return None


def _load_card_info(card_path: Path, author_surname: str) -> CardInfo:
    """Extract DOI and English title from a card file."""
    text = card_path.read_text(encoding='utf-8')
    # Cards use **DOI**: or **DOI:** (colon position varies by card version)
    doi_m = (re.search(r'\*\*DOI\*\*:\s*(.+?)$', text, re.MULTILINE)
             or re.search(r'\*\*DOI:\*\*\s*(.+?)$', text, re.MULTILINE))
    title_m = (re.search(r'\*\*原文标题\*\*:\s*(.+?)$', text, re.MULTILINE)
               or re.search(r'\*\*原文标题:\*\*\s*(.+?)$', text, re.MULTILINE)
               or re.search(r'\*\*报告全称:\*\*\s*(.+?)$', text, re.MULTILINE))
    return CardInfo(
        path=card_path,
        doi=doi_m.group(1).strip() if doi_m else None,
        title=title_m.group(1).strip() if title_m else None,
        author_surname=author_surname,
    )


def _build_subtitle(theme: str, podcast_md: Path | None) -> str:
    """Generate cover subtitle from podcast H1 or fallback map."""
    if podcast_md:
        text = podcast_md.read_text(encoding='utf-8')
        m = re.search(r'^#\s+Podcast Instructions:\s*(.+)$', text, re.MULTILINE)
        if m and len(m.group(1).strip()) <= 24:
            return m.group(1).strip()
    return _COVER_SUBTITLE_FALLBACK.get(theme, "AI for Science · 前沿论文")


def _build_description(contexts: list[ThemeContext | None]) -> str:
    """Build video description with DOIs organized by theme."""
    lines: list[str] = []
    idx = 1
    for ctx in contexts:
        if not ctx or not ctx.papers:
            continue
        label = ctx.theme.replace('-', ' ').title()
        lines.append(f"【{label}】")
        for card in ctx.papers:
            parts = [f"{idx}. {card.author_surname} et al."]
            if card.title:
                t = card.title if len(card.title) <= 80 else card.title[:77] + "..."
                parts.append(f" — {t}")
            lines.append("".join(parts))
            if card.doi:
                lines.append(f"   https://doi.org/{card.doi}")
            idx += 1
        lines.append("")
    if lines:
        lines.append("由 dailyinfo 自动生成 | https://github.com/iHeadWater/dailyinfo")
    return "\n".join(lines)


def _find_article_md(wr_root: Path, theme: str) -> Path | None:
    """Find article_{date}_{theme}.md in the article directory."""
    article_dir = wr_root / "article"
    if not article_dir.exists():
        return None
    date_str = wr_root.name  # YYYY-MM-DD
    candidate = article_dir / f"article_{date_str}_{theme}.md"
    return candidate if candidate.exists() else None


def _extract_authors_from_article(text: str) -> list[str]:
    """Extract first-author surnames from article text.

    Matches: 'Crow et al.', 'Crow 等人', 'Rodriguez-Pardo 与 Tavoni',
    'Guidi 与 Dominici', 'Author1、Author2 等人'
    """
    surnames: list[str] = []
    seen: set[str] = set()
    patterns = [
        # English: Author et al. (optional comma-initial)
        re.compile(r'(?:^|[.。\s\n])([A-Z][A-Za-z\xe9\xe8\xea\xeb\xe0\xe2\xe4\xf9\xfb\xfc\xf4\xee\xef\xe7\xc1\xc9\xcd\xd3\xda\xdd\xc4\xcb\xcf\xd6\xdc-]+)(?:,\s*[A-Z]\.?,\s*)?et\s+al\.'),
        # Chinese: Author 等人
        re.compile(r'(?:^|[.。\s\n])([A-Z][A-Za-z\xe9\xe8\xea\xeb\xe0\xe2\xe4\xf9\xfb\xfc\xf4\xee\xef\xe7\xc1\xc9\xcd\xd3\xda\xdd\xc4\xcb\xcf\xd6\xdc-]+)\s*等人'),
        # Chinese co-author: Author1 与 Author2
        re.compile(r'(?:^|[.。\s\n])([A-Z][A-Za-z\xe9\xe8\xea\xeb\xe0\xe2\xe4\xf9\xfb\xfc\xf4\xee\xef\xe7\xc1\xc9\xcd\xd3\xda\xdd\xc4\xcb\xcf\xd6\xdc-]+)\s*[与和]\s*[A-Z]'),
    ]
    for pat in patterns:
        for m in pat.finditer(text):
            name = m.group(1)
            key = _ascii_fold(name)
            if key not in seen:
                seen.add(key)
                surnames.append(name)
    return surnames


def _extract_h1(text: str) -> str | None:
    """Extract the first H1 heading from markdown text."""
    m = re.search(r'^#\s+(.+?)$', text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _build_contexts_from_content(audio_paths: list[Path]) -> list[ThemeContext]:
    """Build theme contexts purely from article/card content, not filenames.

    1. Scan all article_{date}_*.md files in natural sort order
    2. For each article: extract H1 as subtitle, authors → match cards → DOIs
    3. Pair articles with audio files by index (1:1)
    4. Enrich subtitle from podcast H1 where content overlaps
    """
    if not audio_paths:
        return []

    wr_root = _find_weekly_review_root(audio_paths[0])
    if not wr_root:
        return [ThemeContext(subtitle="AI for Science · 前沿论文") for _ in audio_paths]

    article_dir = wr_root / "article"
    cards_dir = wr_root / "cards"
    podcast_dir = audio_paths[0].parent

    # Scan ALL articles in sort order
    art_paths = sorted(article_dir.glob("article_*.md")) if article_dir.exists() else []

    contexts: list[ThemeContext] = []
    for i, art_path in enumerate(art_paths):
        if i >= len(audio_paths):
            break
        text = art_path.read_text(encoding='utf-8')
        h1 = _extract_h1(text)
        authors = _extract_authors_from_article(text)

        # Match cards for DOIs
        papers: list[CardInfo] = []
        if cards_dir.exists():
            for surname in authors:
                card_path = _find_card_for_author(surname, cards_dir)
                if card_path:
                    papers.append(_load_card_info(card_path, surname))

        subtitle = h1 or art_path.stem
        # Try content-based podcast H1 enrichment
        if podcast_dir.exists():
            for pod in sorted(podcast_dir.glob("podcast_*.md")):
                pod_text = pod.read_text(encoding='utf-8')
                pod_h1 = _extract_h1(pod_text)
                if pod_h1 and len(pod_h1) <= 24:
                    pod_authors = set(_ascii_fold(a) for a in _extract_authors_from_article(pod_text))
                    art_authors = set(_ascii_fold(a) for a in authors)
                    if pod_authors and art_authors and pod_authors & art_authors:
                        subtitle = pod_h1
                        break

        contexts.append(ThemeContext(theme=art_path.stem, subtitle=subtitle, papers=papers))

    # Fill remainder with fallback
    while len(contexts) < len(audio_paths):
        contexts.append(ThemeContext(subtitle="AI for Science · 前沿论文"))

    return contexts


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

    date_str = datetime.date.today().strftime("%Y 第 %U 周")

    # ── build theme contexts from content (scan articles, match cards) ──
    theme_contexts = _build_contexts_from_content(resolved)
    for ctx in theme_contexts:
        if ctx.papers:
            log(f"  theme '{ctx.subtitle}': {len(ctx.papers)} paper(s) matched")

    # ── auto-generate description if not provided ──
    if not desc and any(tc for tc in theme_contexts if tc and tc.papers):
        auto = _build_description(theme_contexts)
        if auto:
            desc = auto
            log(f"  auto-generated description ({len(desc)} chars, {desc.count('doi.org')} DOIs)")

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
        for ap, ctx in zip(resolved, theme_contexts):
            sub = ctx.subtitle if ctx and ctx.subtitle else "AI for Science · 前沿论文"
            log(f"  generating cover for {ap.stem} → '{sub}'")
            cover_out = OUTPUT_DIR / f"{ap.stem}_cover.png"
            cp = generate_cover(title=title, date_str=date_str, subtitle=sub,
                                output_path=cover_out)
            cover_paths.append(cp)
        cover_path = cover_paths[0]  # main cover for upload

    # ── convert (ffmpeg needed) ──
    if shutil.which("ffmpeg") is None:
        log("ERROR: ffmpeg not found. Install: winget install ffmpeg")
        return 1

    # Save artifacts to date-organized folder
    today = datetime.date.today().isoformat()
    artifact_dir = OUTPUT_DIR / today
    artifact_dir.mkdir(parents=True, exist_ok=True)

    video_paths = []
    for ap, cp in zip(resolved, cover_paths):
        vp = audio_to_video(ap, cp)
        video_paths.append(vp)
        # Copy artifacts to date folder
        _copy_to_dir(cp, artifact_dir)
        _copy_to_dir(vp, artifact_dir)

    # Save description
    if desc:
        (artifact_dir / "description.md").write_text(desc, encoding="utf-8")
    log(f"  artifacts saved → {artifact_dir}")

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
