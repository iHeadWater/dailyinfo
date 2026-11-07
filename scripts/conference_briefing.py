"""Deterministic markdown briefings for conference submissions.

No model is called here.  The output is a pure function of the rows the store
returns, which is what makes the briefing reproducible and testable: the same
database state renders the same bytes.  AI summaries, relevance ranking and
Discord formatting are deliberately separate, later layers.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import tempfile
from typing import Iterable
from zoneinfo import ZoneInfo

CONTENT_TIMEZONE = ZoneInfo("Asia/Shanghai")

_STATUS_LABELS = {
    "under_review": "审稿中",
    "accepted": "已录用",
    "rejected": "已拒稿",
    "withdrawn": "已撤稿",
    "desk_rejected": "桌拒",
    "unknown": "未知",
}


def briefing_filename(source: str, date: str) -> str:
    return f"{source}_{date}.md"


def baseline_filename(source: str, date: str) -> str:
    return f"{source}_baseline_{date}.md"


def briefing_path(briefings_dir: str | Path, source: str, date: str) -> Path:
    return Path(briefings_dir) / "conference" / briefing_filename(source, date)


def baseline_path(briefings_dir: str | Path, source: str, date: str) -> Path:
    return Path(briefings_dir) / "conference" / baseline_filename(source, date)


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _submission_date(cdate: object) -> str:
    try:
        stamp = int(cdate or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return datetime.fromtimestamp(stamp / 1000, tz=CONTENT_TIMEZONE).strftime(
        "%Y-%m-%d"
    )


def render_briefing(
    *,
    display_name: str,
    source: str,
    date: str,
    papers: Iterable[dict],
) -> str:
    """Render the new-submission listing for one source and date."""

    papers = list(papers)
    lines = [f"# {_one_line(display_name)} — {date}", ""]
    if not papers:
        lines.append("今日无新增投稿。")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"今日新增投稿 {len(papers)} 篇。")
    lines.append("")
    for index, paper in enumerate(papers, 1):
        title = _one_line(paper.get("title")) or "(无标题)"
        lines.append(f"{index}. **{title}**")
        authors = [_one_line(author) for author in paper.get("authors") or []]
        authors = [author for author in authors if author]
        if authors:
            lines.append(f"   - 作者: {', '.join(authors)}")
        keywords = [_one_line(keyword) for keyword in paper.get("keywords") or []]
        keywords = [keyword for keyword in keywords if keyword]
        if keywords:
            lines.append(f"   - 关键词: {', '.join(keywords)}")
        status = str(paper.get("status") or "unknown")
        lines.append(f"   - 状态: {_STATUS_LABELS.get(status, status)}")
        submitted = _submission_date(paper.get("cdate"))
        if submitted:
            lines.append(f"   - 提交: {submitted}")
        landing = _one_line(paper.get("landing_url"))
        if landing:
            lines.append(f"   - 链接: {landing}")
        pdf = _one_line(paper.get("pdf"))
        if pdf:
            lines.append(f"   - PDF: {pdf}")
        lines.append("")
    return "\n".join(lines)


def render_baseline(
    *,
    display_name: str,
    source: str,
    date: str,
    status_counts: dict[str, int],
) -> str:
    """Render the counts-only report written when a source is first imported."""

    total = sum(status_counts.values())
    lines = [f"# {_one_line(display_name)} 基线 — {date}", ""]
    lines.append(
        f"首次同步已入库 {total} 篇投稿。为避免刷屏，基线只记录计数，不逐篇列出。"
    )
    lines.append("")
    if status_counts:
        lines.append("状态分布：")
        for status in sorted(status_counts):
            label = _STATUS_LABELS.get(status, status)
            lines.append(f"- {label} ({status}): {status_counts[status]}")
        lines.append("")
    return "\n".join(lines)


def write_briefing(path: str | Path, content: str) -> str:
    """Write atomically: a crash leaves either the old file or the new one."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".tmp-",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return str(path)
