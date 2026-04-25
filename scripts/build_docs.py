#!/usr/bin/env python3
"""Generate MkDocs pages from repository source files."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DOCS_DIR = PROJECT_ROOT / "docs"
README = PROJECT_ROOT / "README.md"
SOURCES_JSON = PROJECT_ROOT / "config" / "sources.json"
SOURCES_URL = "https://github.com/OuyangWenyu/dailyinfo/blob/main/config/sources.json"


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


def generate_sources_page() -> None:
    """Generate a source catalog page from config/sources.json."""
    cfg = json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
    sources = cfg.get("sources", [])
    lines = [
        "# Information Sources",
        "",
        f"This page is generated from [`config/sources.json`]({SOURCES_URL}).",
        "",
        "| Name | Display Name | Category | Type | Enabled | Lookback Hours | URL |",
        "|------|--------------|----------|------|---------|----------------|-----|",
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
                lookback=_markdown_cell(source.get("lookback_hours", default_lookback)),
                url=_markdown_cell(source.get("url", "")),
            )
        )
    lines.append("")
    (DOCS_DIR / "sources.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    """Generate all derived MkDocs pages."""
    sync_readme_index()
    generate_sources_page()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
