#!/usr/bin/env python3
"""Generate MkDocs pages from repository source files."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DOCS_DIR = PROJECT_ROOT / "docs"
README = PROJECT_ROOT / "README.md"
PICTURES_DIR = PROJECT_ROOT / "pictures"
SOURCES_JSON = PROJECT_ROOT / "config" / "sources.json"
SOURCES_URL = "https://github.com/iHeadWater/dailyinfo/blob/main/config/sources.json"


def _markdown_cell(value) -> str:
    """Escape a value for use in a markdown table cell."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def sync_readme_index() -> None:
    """Copy README.md to docs/index.md so the docs homepage stays identical."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.md").write_text(
        README.read_text(encoding="utf-8"), encoding="utf-8"
    )
    mirror_dir = DOCS_DIR / "docs"
    mirror_dir.mkdir(parents=True, exist_ok=True)
    for name in ("architecture.md", "agent-config.md", "cli.md"):
        source = DOCS_DIR / name
        target = mirror_dir / name
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def sync_pictures() -> None:
    """Copy README screenshots into docs/pictures so MkDocs can serve them.

    README.md references ``pictures/xxx.png`` (relative to the repo root).
    MkDocs serves only files under ``docs/``, so the referenced images are
    mirrored to ``docs/pictures/`` for the docs site, while the GitHub README
    keeps using the root ``pictures/`` path. Only images actually referenced in
    README are copied — unrelated PNGs (e.g. historical screenshots) stay out.
    """
    if not PICTURES_DIR.exists():
        return
    # Find `pictures/<name>.png` references in README.md.
    readme_text = README.read_text(encoding="utf-8")
    referenced = set(re.findall(r"pictures/([A-Za-z0-9_.-]+\.png)", readme_text))
    target = DOCS_DIR / "pictures"
    target.mkdir(parents=True, exist_ok=True)
    for name in referenced:
        src = PICTURES_DIR / name
        if src.exists():
            shutil.copy2(src, target / name)


def generate_sources_page() -> None:
    """Generate a source catalog page from config/sources.json."""
    cfg = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
    sources = cfg.get("sources", [])
    lines = [
        "# Information Sources",
        "",
        f"This page is generated from [`config/sources.json`]({SOURCES_URL}).",
        "",
        "| Name | Display Name | Category | Type | Enabled | Poll/Lookback Hours | URL |",
        "|------|--------------|----------|------|---------|---------------------|-----|",
    ]
    defaults = cfg.get("defaults", {})
    default_lookback = defaults.get("lookback_hours", "")
    for source in sources:
        lines.append(
            "| {name} | {display_name} | {category} | {type} | {enabled} | "
            "{lookback} | {url} |".format(
                name=_markdown_cell(source.get("name", "")),
                display_name=_markdown_cell(source.get("display_name", "")),
                category=_markdown_cell(source.get("category", "")),
                type=_markdown_cell(source.get("type", "")),
                enabled=_markdown_cell(source.get("enabled", True)),
                lookback=_markdown_cell(
                    source.get(
                        "poll_interval_hours",
                        source.get("lookback_hours", default_lookback),
                    )
                ),
                url=_markdown_cell(source.get("url", "")),
            )
        )
    lines.append("")
    (DOCS_DIR / "sources.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    """Generate all derived MkDocs pages."""
    sync_readme_index()
    sync_pictures()
    generate_sources_page()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
