#!/usr/bin/env python3
"""Extract or generate figures for each paper in a polished weekly-report MD.

For each paper in the polished MD:
  1. Find a matching PDF in ~/Zotero/storage/
  2. Extract the best figure (architecture/result diagram) via pymupdf
  3. Fall back to ZhipuAI CogView-3 if no usable figure found

Outputs:
  <output_dir>/figures/figure_N.png
  <output_dir>/figures/figures.json
"""

import json
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from paths import PROJECT_ROOT

ZOTERO_STORAGE = Path.home() / "Zotero" / "storage"
MIN_DIM = 100       # skip icons/decorations
MIN_WIDTH = 300     # prefer architecture/result diagrams


# ── Paper title extraction ───────────────────────────────────────────────────

def extract_paper_titles(md_text: str) -> list[dict]:
    """Return list of {title_zh, title_en, summary} for each 📌 paper."""
    papers = []
    pattern = re.compile(
        r"###\s+📌\s+(.+?)(?:\s+·\s+(.+?))?\s*\n"   # zh · en
        r".*?\*\*一句话：\*\*\s*(.+?)(?:\n|$)",       # one-liner
        re.DOTALL,
    )
    for m in pattern.finditer(md_text):
        title_zh = m.group(1).strip()
        title_en = (m.group(2) or "").strip()
        summary = m.group(3).strip()
        papers.append({"title_zh": title_zh, "title_en": title_en, "summary": summary})
    return papers


# ── PDF matching ─────────────────────────────────────────────────────────────

def _title_tokens(title: str) -> set[str]:
    return {t.lower() for t in re.split(r"[\s\-_:,;.·]+", title) if len(t) > 2}


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def find_matching_pdf(title_zh: str, title_en: str) -> Path | None:
    if not ZOTERO_STORAGE.exists():
        return None
    tokens_zh = _title_tokens(title_zh)
    tokens_en = _title_tokens(title_en)
    best_path, best_score = None, 0.3  # threshold
    for pdf in ZOTERO_STORAGE.rglob("*.pdf"):
        stem_tokens = _title_tokens(pdf.stem)
        score = max(_similarity(tokens_zh, stem_tokens), _similarity(tokens_en, stem_tokens))
        if score > best_score:
            best_score, best_path = score, pdf
    return best_path


# ── PDF figure extraction ─────────────────────────────────────────────────────

def _score_image(img_info: dict, total_pages: int, page_idx: int) -> float:
    w, h = img_info.get("width", 0), img_info.get("height", 0)
    if w < MIN_DIM or h < MIN_DIM:
        return -1.0
    score = float(w)
    if page_idx < total_pages // 2:
        score *= 1.3   # prefer figures in first half (architecture diagrams)
    return score


def extract_best_figure(pdf_path: Path, dest: Path) -> bool:
    try:
        import fitz  # pymupdf
    except ImportError:
        return False

    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return False

    total_pages = len(doc)
    best_score, best_img, best_ext = -1.0, None, "png"

    for page_idx, page in enumerate(doc):
        for img in page.get_images(full=True):
            xref = img[0]
            info = doc.extract_image(xref)
            score = _score_image({"width": info["width"], "height": info["height"]}, total_pages, page_idx)
            if score > best_score:
                best_score = score
                best_img = info["image"]
                best_ext = info["ext"]

    doc.close()

    if best_img is None:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Save as PNG regardless of source format
    try:
        import fitz
        pix = fitz.Pixmap(best_img)
        if pix.n > 4:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        pix.save(str(dest))
        return True
    except Exception:
        dest.write_bytes(best_img)
        return True


# ── AI figure generation ──────────────────────────────────────────────────────

def generate_ai_figure(summary: str, dest: Path) -> bool:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("ZHIPUAI_API_KEY", "")
    if not api_key:
        print("  ZHIPUAI_API_KEY not set, skipping AI generation", file=sys.stderr)
        return False

    prompt = (
        f"A clean scientific illustration for a research paper about: {summary}. "
        "Style: minimalist, flat design, blue and white color scheme, no text, "
        "suitable for academic WeChat article."
    )

    try:
        from zhipuai import ZhipuAI
        client = ZhipuAI(api_key=api_key)
        resp = client.images.generations(
            model="cogview-3-flash",
            prompt=prompt,
        )
        image_url = resp.data[0].url

        import urllib.request
        dest.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(image_url, dest)

        # Add "示意图" watermark in bottom-right corner
        _add_watermark(dest)
        return True
    except Exception as e:
        print(f"  CogView generation failed: {e}", file=sys.stderr)
        return False


def _add_watermark(img_path: Path) -> None:
    try:
        import fitz
        doc = fitz.open(str(img_path))
        page = doc[0]
        rect = page.rect
        pos = fitz.Point(rect.width - 80, rect.height - 20)
        page.insert_text(pos, "示意图", fontsize=14, color=(0.5, 0.5, 0.5))
        doc.save(str(img_path))
        doc.close()
    except Exception:
        pass  # watermark is cosmetic, don't fail the pipeline


# ── Cover image generation ────────────────────────────────────────────────────

def _extract_cover_context(md_text: str) -> dict:
    """Extract title, date, and 导读 summary from polished MD."""
    title = ""
    m = re.search(r"^#\s+(.+)", md_text, re.MULTILINE)
    if m:
        title = re.sub(r"[【】｜*]", " ", m.group(1)).strip()

    date = ""
    m2 = re.search(r"(\d{4}-\d{2}-\d{2})", title)
    if m2:
        date = m2.group(1)

    guodu = ""
    m3 = re.search(r"##\s+导读\s*\n+([\s\S]+?)(?=\n##|\Z)", md_text)
    if m3:
        guodu = re.sub(r"\*+", "", m3.group(1)).strip()[:200]

    return {"title": title, "date": date, "guodu": guodu}


# Anti-repetition variation pools
_LAYOUTS = ["2×2模块网格", "2×3模块网格", "左右分栏大图", "中心辐射式"]
_ICON_STYLES = ["线性图标", "霓虹发光图标", "轻拟物3D图标"]
_METAPHORS = [
    "神经网络连接节点映射成河流流域水系",
    "Transformer注意力机制可视化为多源数据融合漩涡",
    "AI芯片内部电路与地球遥感卫星轨道交织",
    "多模态数据流汇聚成统一的知识空间球体",
    "深度学习层级结构化为山脉剖面与水文过程",
    "图神经网络节点对应流域子汇水区相互连接",
]
_CHART_COMBOS = [
    "折线趋势图 + 散点分布图",
    "雷达图 + 热力地图",
    "桑基流向图 + 柱状对比图",
    "环形进度图 + 网络拓扑图",
]


def generate_cover(md_text: str, output_dir: Path) -> Path | None:
    """Generate a weekly-report cover image via CogView using rich prompt template."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("ZHIPUAI_API_KEY", "")
    if not api_key:
        print("  ZHIPUAI_API_KEY not set, skipping cover generation", file=sys.stderr)
        return None

    ctx = _extract_cover_context(md_text)
    layout = random.choice(_LAYOUTS)
    icon_style = random.choice(_ICON_STYLES)
    metaphor = random.choice(_METAPHORS)
    charts = random.choice(_CHART_COMBOS)

    prompt = (
        "生成一张科技研究周报信息图封面，用于展示AI与水文科学论文综述内容。\n\n"
        "【风格系统】\n"
        "科技感信息可视化风格，类似arXiv/Nature Graphical Abstract与科技媒体封面结合。"
        "主色调：深蓝/靛蓝/青绿/白色（每期允许轻微偏移一个主色）。"
        f"设计语言：{icon_style}，卡片式模块，轻拟物与扁平图标结合。"
        f"布局：{layout}，严格网格系统。"
        "有发光数据线/连接网络元素，但本期形态独特不重复。\n\n"
        "【本期内容】\n"
        f"主题：{ctx['guodu'][:150]}\n\n"
        "【主视觉隐喻】\n"
        f"核心概念视觉隐喻：{metaphor}。"
        "必须是隐喻图，不是文字堆叠，构图独特不重复。\n\n"
        "【图表元素】\n"
        f"包含以下图表组合：{charts}，数据风格抽象化，不显示真实数字。\n\n"
        "【禁止项】\n"
        "不要照片写实风人物，不要论文截图，不要纯文字排版，不要低信息密度装饰图，"
        "不要任何文字/字母/数字出现在图中。\n\n"
        "【输出要求】\n"
        "完整信息图，16:9横幅比例，信息层级清晰，高质量数字艺术，细节丰富。"
    )

    try:
        from zhipuai import ZhipuAI
        import urllib.request

        client = ZhipuAI(api_key=api_key)
        resp = client.images.generations(model="cogview-3-flash", prompt=prompt)
        image_url = resp.data[0].url

        figures_dir = output_dir / "figures"
        figures_dir.mkdir(parents=True, exist_ok=True)
        dest = figures_dir / "cover.png"
        urllib.request.urlretrieve(image_url, dest)
        print(f"  ✓ 封面图生成: {dest.name}  [布局:{layout} | 隐喻:{metaphor[:20]}...]")
        return dest
    except Exception as e:
        print(f"  ✗ 封面图生成失败: {e}", file=sys.stderr)
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def extract_figures(polished_md: Path, output_dir: Path) -> list[dict]:
    """Extract/generate figures for all papers. Returns figures manifest."""
    md_text = polished_md.read_text(encoding="utf-8")
    papers = extract_paper_titles(md_text)

    if not papers:
        print("No papers found in MD.", file=sys.stderr)
        return []

    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    manifest = []

    for idx, paper in enumerate(papers, 1):
        title_zh = paper["title_zh"]
        title_en = paper["title_en"]
        summary = paper["summary"]
        dest = figures_dir / f"figure_{idx}.png"

        print(f"  [{idx}/{len(papers)}] {title_zh[:30]}...")

        # Try PDF extraction first
        pdf = find_matching_pdf(title_zh, title_en)
        if pdf and extract_best_figure(pdf, dest):
            source = "pdf_extract"
            note = f"提取自 {pdf.name}"
            print(f"    ✓ 提取自 PDF: {pdf.name}")
        else:
            if pdf:
                print(f"    PDF found but no usable figure, falling back to AI...")
            else:
                print(f"    No matching PDF, generating via CogView...")
            time.sleep(1)  # rate limit
            if generate_ai_figure(summary, dest):
                source = "ai_generated"
                note = "AI生成示意图"
                print(f"    ✓ AI生成: {dest.name}")
            else:
                print(f"    ✗ Both methods failed, skipping.")
                manifest.append({
                    "paper_title": title_zh,
                    "figure_path": None,
                    "source": "failed",
                    "note": "提取和生成均失败",
                })
                continue

        manifest.append({
            "paper_title": title_zh,
            "figure_path": str(dest.relative_to(output_dir)),
            "source": source,
            "note": note,
        })

    # Generate cover image for the whole article
    print("  [封面] 根据整体内容生成封面图...")
    generate_cover(md_text, output_dir)

    out_json = figures_dir / "figures.json"
    out_json.write_text(
        json.dumps({"figures": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"  figures.json saved: {out_json}")
    return manifest


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Extract or generate figures for weekly report papers")
    parser.add_argument("input", help="Polished MD file path")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: same as input file)")
    args = parser.parse_args()

    polished_md = Path(args.input)
    output_dir = Path(args.output_dir) if args.output_dir else polished_md.parent

    print(f"Extracting figures for: {polished_md.name}")
    manifest = extract_figures(polished_md, output_dir)
    ok = sum(1 for f in manifest if f.get("figure_path"))
    print(f"Done: {ok}/{len(manifest)} figures ready.")


if __name__ == "__main__":
    main()
