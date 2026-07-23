#!/usr/bin/env python3
"""Push a polished weekly report MD to WeChat Official Account draft box.

Usage:
    python3 scripts/push_wechat.py <polished_md_file>
    python3 scripts/push_wechat.py <file> --thumb <media_id>  # custom cover
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import markdown
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from paths import PROJECT_ROOT

WECHAT_APPID = "wx3413d02885b6aa45"
WECHAT_APPSECRET = "71b0e49a51564392a1ec366d3440e9e8"
DEFAULT_THUMB_MEDIA_ID = "OD4PaYcHHxUCqehWcjHvuj3-pVN3-fwoe2YrQoTJADgaOVEYgsHjsYw-3iU_FPv_"

# ──────────────────────────────────────────────────────────
#  Inline-style definitions for WeChat
# ──────────────────────────────────────────────────────────
BRAND = "#1565C0"       # dark blue for headings / bold
ACCENT = "#1e88e5"      # lighter blue for h2 border
TEXT = "#333333"
META = "#888888"
BG_QUOTE = "#f5f8ff"

STYLES = {
    "section": (
        'font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",'
        '"Helvetica Neue",Helvetica,Arial,sans-serif;'
        "max-width:680px;margin:0 auto;padding:0 16px 32px;"
    ),
    "h1": (
        f"font-size:22px;font-weight:700;color:#1a1a1a;"
        "text-align:center;margin:24px 0 6px;line-height:1.4;"
    ),
    "h2": (
        f"font-size:18px;font-weight:700;color:{BRAND};"
        f"margin:28px 0 12px;padding-left:10px;"
        f"border-left:4px solid {ACCENT};line-height:1.4;"
    ),
    "h3": (
        "font-size:16px;font-weight:700;color:#1a1a1a;"
        "margin:20px 0 6px;line-height:1.4;"
    ),
    "p": (
        f"font-size:15px;color:{TEXT};line-height:1.9;"
        "margin:8px 0;word-break:break-word;"
    ),
    "blockquote": (
        f"font-size:13px;color:{META};margin:4px 0 12px;"
        f"padding:6px 10px;background:{BG_QUOTE};"
        "border-left:3px solid #b0bec5;border-radius:0 4px 4px 0;"
    ),
    "blockquote_p": (
        f"font-size:13px;color:{META};margin:0;line-height:1.6;"
    ),
    "ol": "margin:8px 0 12px;padding-left:24px;",
    "ul": "margin:8px 0 12px;padding-left:20px;",
    "li": f"font-size:15px;color:{TEXT};line-height:1.9;margin:4px 0;",
    "strong": f"color:{BRAND};font-weight:700;",
    "em": "font-style:italic;",
    "hr": "border:none;border-top:1px solid #e0e0e0;margin:20px 0;",
    "code": (
        "font-size:13px;background:#f4f4f4;border-radius:3px;"
        "padding:1px 5px;font-family:monospace;"
    ),
    "pre": (
        "font-size:13px;background:#f4f4f4;border-radius:4px;"
        "padding:12px;overflow-x:auto;margin:10px 0;"
    ),
}


def _set_style(html: str, tag: str, style: str) -> str:
    """Add inline style to all opening <tag> occurrences (no existing style)."""
    return re.sub(
        rf"<{tag}(?=\s|>)(?![^>]*\bstyle\b)",
        f'<{tag} style="{style}"',
        html,
    )


def md_to_html(md_text: str) -> str:
    """Convert markdown to WeChat-compatible HTML with inline styles."""
    md = markdown.Markdown(extensions=["extra", "nl2br"])
    body = md.convert(md_text)

    # Apply styles to tags
    for tag in ("h1", "h2", "h3", "ol", "ul", "hr"):
        body = _set_style(body, tag, STYLES[tag])

    # <p> inside <blockquote> needs a different style
    # Split blockquotes first, style inner <p>, then style outer <blockquote>
    def style_blockquote(m: re.Match) -> str:
        inner = m.group(1)
        inner = re.sub(
            r"<p(?=\s|>)(?![^>]*\bstyle\b)",
            f'<p style="{STYLES["blockquote_p"]}"',
            inner,
        )
        return f'<blockquote style="{STYLES["blockquote"]}">{inner}</blockquote>'

    body = re.sub(r"<blockquote>([\s\S]*?)</blockquote>", style_blockquote, body)

    # Remaining <p> (not inside blockquote)
    body = _set_style(body, "p", STYLES["p"])

    body = _set_style(body, "li", STYLES["li"])
    body = _set_style(body, "strong", STYLES["strong"])
    body = _set_style(body, "em", STYLES["em"])
    body = _set_style(body, "code", STYLES["code"])
    body = _set_style(body, "pre", STYLES["pre"])

    return f'<section style="{STYLES["section"]}">{body}</section>'


def upload_permanent_image(token: str, img_path: Path) -> str | None:
    """Upload image as permanent WeChat material. Returns media_id or None."""
    try:
        with img_path.open("rb") as f:
            resp = requests.post(
                f"https://api.weixin.qq.com/cgi-bin/material/add_material?access_token={token}&type=image",
                files={"media": (img_path.name, f, "image/png")},
                timeout=30,
            )
        resp.raise_for_status()
        data = resp.json()
        media_id = data.get("media_id")
        if not media_id:
            print(f"  permanent image upload failed: {data}", file=sys.stderr)
        return media_id
    except Exception as e:
        print(f"  permanent image upload error: {e}", file=sys.stderr)
        return None


def upload_image(token: str, img_path: Path) -> str | None:
    """Upload a local image to WeChat as article content image. Returns URL or None."""
    try:
        with img_path.open("rb") as f:
            resp = requests.post(
                f"https://api.weixin.qq.com/cgi-bin/media/uploadimg?access_token={token}",
                files={"media": (img_path.name, f, "image/png")},
                timeout=30,
            )
        resp.raise_for_status()
        data = resp.json()
        url = data.get("url")
        if not url:
            print(f"  image upload failed: {data}", file=sys.stderr)
        return url
    except Exception as e:
        print(f"  image upload error: {e}", file=sys.stderr)
        return None


def inject_figures(html: str, figures_json: Path, token: str) -> str:
    """Upload figures and inject <img> tags after each paper's blockquote (source line)."""
    import json as _json

    data = _json.loads(figures_json.read_text(encoding="utf-8"))
    figures = data.get("figures", [])
    if not figures:
        return html

    output_dir = figures_json.parent.parent
    for fig in figures:
        fig_rel = fig.get("figure_path")
        if not fig_rel:
            continue
        img_path = output_dir / fig_rel
        if not img_path.exists():
            continue

        wx_url = upload_image(token, img_path)
        if not wx_url:
            continue

        source_label = "论文原图" if fig.get("source") == "pdf_extract" else "AI生成示意图"
        img_html = (
            f'<figure style="margin:12px 0;text-align:center;">'
            f'<img src="{wx_url}" style="max-width:100%;border-radius:4px;" />'
            f'<figcaption style="font-size:12px;color:#aaa;margin-top:4px;">图源：{source_label}</figcaption>'
            f'</figure>'
        )

        # Insert after the blockquote (> 来源：...) of the matching paper
        paper_title_fragment = re.escape(fig["paper_title"][:15])
        pattern = re.compile(
            rf'(📌.*?{paper_title_fragment}.*?<\/blockquote>)',
            re.DOTALL,
        )
        html = pattern.sub(r'\1' + img_html, html, count=1)

    return html


def get_access_token() -> str:
    resp = requests.get(
        "https://api.weixin.qq.com/cgi-bin/token",
        params={
            "grant_type": "client_credential",
            "appid": WECHAT_APPID,
            "secret": WECHAT_APPSECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"WeChat token error: {data}")
    return data["access_token"]


def push_draft(token: str, title: str, html_content: str, thumb_media_id: str) -> str:
    payload = {
        "articles": [
            {
                "title": title,
                "author": "DailyInfo Bot",
                "content": html_content,
                "content_source_url": "",
                "thumb_media_id": thumb_media_id,
                "need_open_comment": 0,
                "only_fans_can_comment": 0,
            }
        ]
    }
    # Must use ensure_ascii=False — otherwise Chinese chars become \uXXXX
    # escape sequences that WeChat renders literally instead of decoding.
    resp = requests.post(
        f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={token}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"WeChat draft error: {data}")
    return data.get("media_id", "")


def extract_title(md_text: str, max_bytes: int = 60) -> str:
    for line in md_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            # WeChat rejects fullwidth brackets and some Unicode punctuation
            title = title.replace("【", "").replace("】", "").replace("｜", " - ").replace("｜", "|")
            title = re.sub(r"\s{2,}", " ", title).strip()
            # Truncate to byte limit
            encoded = title.encode("utf-8")
            if len(encoded) > max_bytes:
                title = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
            return title
    return "周报"


def main() -> None:
    parser = argparse.ArgumentParser(description="Push polished MD to WeChat draft box")
    parser.add_argument("input", help="Polished markdown file path")
    parser.add_argument("--title", default=None, help="Override article title")
    parser.add_argument(
        "--thumb",
        default=DEFAULT_THUMB_MEDIA_ID,
        help="Cover image media_id (WeChat permanent image)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print HTML without pushing")
    parser.add_argument("--figures", default=None, help="Path to figures/figures.json to inject images")
    args = parser.parse_args()

    md_path = Path(args.input)
    if not md_path.exists():
        print(f"File not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    md_text = md_path.read_text(encoding="utf-8")

    # Strip any LLM preamble before the first H1 heading
    lines = md_text.splitlines()
    h1_idx = next((i for i, l in enumerate(lines) if l.startswith("# ")), 0)
    if h1_idx > 0:
        md_text = "\n".join(lines[h1_idx:])

    title = args.title if args.title else extract_title(md_text)
    print(f"[1/3] Title: {title}")

    print("[2/3] Converting MD to HTML...")
    html = md_to_html(md_text)

    print("[3/3] Getting WeChat access token...")
    token = get_access_token() if not args.dry_run else ""

    if args.figures:
        figures_json = Path(args.figures)
        cover_png = figures_json.parent / "cover.png"
        if figures_json.exists():
            if args.dry_run:
                print("Dry-run: skipping figure upload")
            else:
                # Upload cover as permanent material and use as thumb
                if cover_png.exists():
                    print("Uploading cover image as permanent material...")
                    cover_media_id = upload_permanent_image(token, cover_png)
                    if cover_media_id:
                        args.thumb = cover_media_id
                        print(f"  Cover media_id: {cover_media_id}")
                print("Injecting figures...")
                html = inject_figures(html, figures_json, token)
        else:
            print(f"Warning: figures.json not found: {figures_json}", file=sys.stderr)

    if args.dry_run:
        out = md_path.with_suffix(".html")
        out.write_text(html, encoding="utf-8")
        print(f"Dry-run: HTML saved to {out}")
        return

    print("Pushing draft to WeChat...")
    media_id = push_draft(token, title, html, args.thumb)
    print(f"Done! media_id: {media_id}")


if __name__ == "__main__":
    main()
