"""Build a compact, one-line-per-paper digest for conference briefings.

The detailed AI briefing remains the archival record.  This module produces the
shorter text meant for human consumption in Discord: one sentence per paper,
plus its raw ratings and decision.  It never sends anything; the CLI only
prints a preview.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from paths import BRIEFINGS_DIR, CONFERENCE_DIR, PROJECT_ROOT

ONE_LINER_PROMPT_VERSION = 4
MODEL_BATCH_SIZE = 10
MODEL_MAX_TOKENS = 3000
DISCORD_CHUNK_LIMIT = 1950

CONTENT_TIMEZONE = ZoneInfo("Asia/Shanghai")

_BLOCK_SPLIT = re.compile(r"(?:\A|\n)###\s+")
_FORUM_ID = re.compile(r"forum\?id=([A-Za-z0-9_-]+)")
_RATINGS = re.compile(r"raw_review_ratings[）)]?\s*[：:]\s*([^\n]+)")
_SUMMARY = re.compile(r"(?:\*\*)?研究内容[：:](?:\*\*)?\s*([^\n]+)")
_ONE_LINER = re.compile(r"(?:\*\*)?一句话(?:总结)?[：:](?:\*\*)?\s*([^\n]+)")
_STATUS_SUFFIX = re.compile(
    r"[（(]\s*(?:状态\s*[：:]\s*)?"
    r"(?:under_review|accepted|rejected|withdrawn|desk_rejected)"
    r"\s*[）)]\s*$"
)

SummaryLookup = Callable[[str, Sequence[str]], dict[str, dict]]
CallAI = Callable[[str, int], str]


@dataclass(frozen=True)
class DigestPaper:
    forum_id: str
    title: str
    one_line: str
    ratings: tuple[str, ...]
    decision: str
    url: str
    fallback: bool = False


@dataclass(frozen=True)
class Digest:
    source: str
    display_name: str
    date: str
    papers: tuple[DigestPaper, ...]
    content: str
    files: tuple[Path, ...] = ()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _today() -> str:
    return datetime.now(CONTENT_TIMEZONE).strftime("%Y-%m-%d")


def _one_line_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _wrap_text(value: object, width: int = 70) -> list[str]:
    """Wrap a quote at punctuation near ``width`` without losing any text."""

    text = _one_line_text(value)
    if not text:
        return [""]
    lines: list[str] = []
    remaining = text
    while len(remaining) > width:
        cut = remaining[:width]
        boundary = max(
            cut.rfind("；"),
            cut.rfind("，"),
            cut.rfind("。"),
            cut.rfind("、"),
            cut.rfind(" "),
        )
        if boundary < width // 2:
            boundary = width - 1
        lines.append(remaining[: boundary + 1].strip())
        remaining = remaining[boundary + 1 :].lstrip()
    if remaining:
        lines.append(remaining)
    return lines or [""]


def _clean_title(value: object) -> str:
    title = _one_line_text(value)
    while True:
        stripped = _STATUS_SUFFIX.sub("", title).strip()
        if stripped == title:
            return title
        title = stripped


def _first_sentence(value: object) -> str:
    text = _one_line_text(value).lstrip("*").strip()
    if not text:
        return ""
    return re.split(r"(?<=[。！？])", text)[0].strip()


def _normalize_one_line(value: object) -> str:
    """Keep the review clause visually separate even when the model omits ':'.

    The prompt asks for ``评审：``, but two of the 42 seed summaries came back as
    ``评审偏负面``.  Normalising here keeps the compact format consistent without
    spending another model call.
    """

    text = _one_line_text(value)
    if text and "评审" in text and not re.search(r"评审\s*[：:]", text):
        text = text.replace("评审", "评审：", 1)
    # ``评审：混合：肯定...`` is a common model variant; keep one separator.
    text = re.sub(r"评审：([^：:，,；;。]{1,8})[：:]", r"评审：\1，", text)
    return text


def _extract_ratings(block: str) -> tuple[str, ...]:
    match = _RATINGS.search(block)
    if not match:
        return ()
    raw = match.group(1).split("；")[0].split("。")[0].replace("**", "").strip()
    values = []
    for part in re.split(r"[、,，/]", raw):
        part = part.strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", part):
            values.append(part)
    return tuple(values)


def parse_ai_briefing(text: str) -> list[dict]:
    """Parse the paper blocks of one detailed AI briefing."""

    papers: list[dict] = []
    for block in _BLOCK_SPLIT.split(text)[1:]:
        if not block.strip():
            continue
        forum_match = _FORUM_ID.search(block)
        if not forum_match:
            continue
        forum_id = forum_match.group(1)
        title = _clean_title(block.splitlines()[0])
        one_liner_match = _ONE_LINER.search(block)
        summary_match = _SUMMARY.search(block)
        summary = _first_sentence(
            one_liner_match.group(1)
            if one_liner_match
            else (summary_match.group(1) if summary_match else "")
        )
        papers.append(
            {
                "forum_id": forum_id,
                "title": title,
                "ratings": _extract_ratings(block),
                "summary": summary,
                "url": f"https://openreview.net/forum?id={forum_id}",
                "block": block,
            }
        )
    return papers


def _sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + ".digest.json")


def _load_sidecar(path: Path) -> dict | None:
    sidecar = _sidecar_path(path)
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != ONE_LINER_PROMPT_VERSION:
        return None
    papers = payload.get("papers")
    if not isinstance(papers, dict):
        return None
    return payload


def _write_sidecar(path: Path, papers: dict[str, dict], *, complete: bool) -> None:
    payload = {
        "version": ONE_LINER_PROMPT_VERSION,
        "generated_ms": _now_ms(),
        "complete": complete,
        "papers": papers,
    }
    sidecar = _sidecar_path(path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{sidecar.name}.", suffix=".tmp", dir=str(sidecar.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, sidecar)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _fallback_one_line(paper: dict) -> str:
    return paper.get("summary") or _one_line_text(paper.get("title"))


def _model_prompt(batch: Sequence[dict]) -> str:
    payload = []
    for paper in batch:
        payload.append(
            f"forum_id: {paper['forum_id']}\n"
            f"title: {paper['title']}\n"
            f"{paper['block'][:3000]}"
        )
    return f"""下面是 {len(batch)} 篇会议论文的详细中文简报。请为每篇写一句中文总结，严格按下面格式：
<论文做了什么>；评审：<总体态度 + 核心理由>。
要求：
1. 总长不超过 60 个汉字，超过会被截断；
2. 前半句说方法/贡献，后半句说评审态度（肯定/混合/偏负面）和核心理由，理由点到为止即可；
3. 不用“论文”开头，不编号，不用 Markdown，不用引号；
4. 首次出现的英文缩写写中文全称，或直接省略；
5. 只能用输入信息，不要编造。
输出格式严格为每行：forum_id<TAB>一句话总结

""" + "\n\n---\n\n".join(
        payload
    )


def _default_call_ai(prompt: str, max_tokens: int) -> str:
    from run_pipelines import call_ai

    return call_ai(prompt, max_tokens=max_tokens)


def _generate_one_liners(
    papers: Sequence[dict],
    call_ai: CallAI,
) -> dict[str, str]:
    generated: dict[str, str] = {}
    for start in range(0, len(papers), MODEL_BATCH_SIZE):
        batch = list(papers[start : start + MODEL_BATCH_SIZE])
        try:
            response = call_ai(_model_prompt(batch), MODEL_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001 - preview must survive provider errors
            print(
                f"[conference_digest] one-liner model failed: {exc}",
                file=sys.stderr,
            )
            continue
        for line in response.splitlines():
            if "\t" not in line:
                continue
            forum_id, one_line = line.split("\t", 1)
            forum_id = forum_id.strip()
            one_line = _one_line_text(one_line)
            if forum_id and one_line:
                generated[forum_id] = one_line
    return generated


def _ensure_one_liners(
    path: Path,
    papers: Sequence[dict],
    *,
    refresh: bool,
    generate: bool,
    call_ai: CallAI,
) -> tuple[dict[str, dict], bool]:
    """Return sidecar entries and whether the file came from cache."""

    cached = None if refresh else _load_sidecar(path)
    if cached is not None and (cached.get("complete") or not generate):
        entries = {
            forum_id: {
                **entry,
                "one_line": _normalize_one_line(entry.get("one_line")),
            }
            for forum_id, entry in cached["papers"].items()
        }
        return entries, True

    if not generate:
        entries = {
            paper["forum_id"]: {
                "title": paper["title"],
                "one_line": _normalize_one_line(_fallback_one_line(paper)),
                "fallback": True,
            }
            for paper in papers
        }
        _write_sidecar(path, entries, complete=False)
        return entries, False

    generated = _generate_one_liners(papers, call_ai)
    entries: dict[str, dict] = {}
    complete = True
    for paper in papers:
        forum_id = paper["forum_id"]
        one_line = _normalize_one_line(
            generated.get(forum_id) or _fallback_one_line(paper)
        )
        fallback = forum_id not in generated
        complete = complete and not fallback
        entries[forum_id] = {
            "title": paper["title"],
            "one_line": one_line,
            "fallback": fallback,
        }
    _write_sidecar(path, entries, complete=complete)
    return entries, False


def _default_summary_lookup(source: str, forum_ids: Sequence[str]) -> dict[str, dict]:
    database = CONFERENCE_DIR / "conference.sqlite3"
    if not database.is_file():
        return {}
    try:
        from conference_store import ConferenceStore

        store = ConferenceStore(database)
        try:
            states = store.forum_states(source, forum_ids)
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - metadata is optional for a preview
        print(
            f"[conference_digest] summary lookup failed: {exc}",
            file=sys.stderr,
        )
        return {}

    result: dict[str, dict] = {}
    for forum_id, state in states.items():
        summary = state.get("summary") or {}
        paper = state.get("paper") or {}
        result[forum_id] = {
            "title": str(paper.get("title") or ""),
            "decision": str(summary.get("decision") or ""),
            "ratings": [
                str(value)
                for value in summary.get("rating_raw_values") or []
                if str(value).strip()
            ],
        }
    return result


def _conference_sources() -> list[dict]:
    config_path = PROJECT_ROOT / "config" / "sources.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        source
        for source in config.get("sources", [])
        if source.get("category") == "conference" and source.get("enabled", True)
    ]


def _files_for_source(directory: Path, source: str) -> list[Path]:
    return sorted(
        (path for path in directory.glob(f"{source}_ai_*.md") if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
    )


def render_digest(
    *,
    display_name: str,
    date: str,
    papers: Sequence[DigestPaper],
) -> str:
    lines = [
        f"## 📚 {display_name} 会议速览 ({date}) - {len(papers)} 篇评审更新",
        "",
    ]
    for index, paper in enumerate(papers, 1):
        lines.append(f"{index}. **[{paper.title}]({paper.url})**")
        metadata = []
        if paper.ratings:
            metadata.append("评分 " + "/".join(paper.ratings))
        metadata.append("Decision: " + (paper.decision or "暂无"))
        for line in _wrap_text(paper.one_line, width=60):
            lines.append(f"   > {line}")
        lines.append(f"   > （{' · '.join(metadata)}）")
        lines.append("")
    if not papers:
        lines.append("今日暂无评审更新。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def estimated_discord_messages(content: str) -> int:
    if not content:
        return 0
    return max(1, math.ceil(len(content) / DISCORD_CHUNK_LIMIT))


def build_digests(
    date: str,
    *,
    sources: Iterable[dict] | None = None,
    briefings_dir: str | Path | None = None,
    summary_lookup: SummaryLookup | None = None,
    call_ai: CallAI | None = None,
    refresh: bool = False,
    generate: bool = True,
    limit: int | None = None,
) -> list[Digest]:
    """Build one compact digest per configured conference source."""

    source_list = list(sources) if sources is not None else _conference_sources()
    directory = Path(briefings_dir) if briefings_dir else BRIEFINGS_DIR / "conference"
    lookup = summary_lookup or _default_summary_lookup
    ai_call = call_ai or _default_call_ai

    digests: list[Digest] = []
    for source_cfg in source_list:
        source = str(source_cfg.get("name") or "")
        if not source:
            continue
        files = _files_for_source(directory, source)
        if not files:
            continue

        merged: dict[str, dict] = {}
        used_files: list[Path] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                print(
                    f"[conference_digest] cannot read {path.name}: {exc}",
                    file=sys.stderr,
                )
                continue
            parsed = parse_ai_briefing(text)
            if not parsed:
                continue
            used_files.append(path)
            entries, _cached = _ensure_one_liners(
                path,
                parsed,
                refresh=refresh,
                generate=generate,
                call_ai=ai_call,
            )
            for paper in parsed:
                entry = entries.get(paper["forum_id"]) or {
                    "title": paper["title"],
                    "one_line": _fallback_one_line(paper),
                    "fallback": True,
                }
                merged[paper["forum_id"]] = {
                    "paper": paper,
                    "entry": entry,
                }

        if not merged:
            continue

        forum_ids = list(merged)
        try:
            summaries = lookup(source, forum_ids)
        except Exception as exc:  # noqa: BLE001 - preview without metadata is valid
            print(
                f"[conference_digest] summary lookup failed: {exc}",
                file=sys.stderr,
            )
            summaries = {}

        papers: list[DigestPaper] = []
        for forum_id in forum_ids:
            item = merged[forum_id]
            paper = item["paper"]
            entry = item["entry"]
            summary = summaries.get(forum_id) or {}
            ratings = tuple(paper.get("ratings") or ())
            if not ratings:
                ratings = tuple(
                    str(value)
                    for value in summary.get("ratings") or []
                    if str(value).strip()
                )
            papers.append(
                DigestPaper(
                    forum_id=forum_id,
                    title=str(summary.get("title") or paper["title"]),
                    one_line=entry.get("one_line") or _fallback_one_line(paper),
                    ratings=ratings,
                    decision=str(summary.get("decision") or ""),
                    url=paper["url"],
                    fallback=bool(entry.get("fallback")),
                )
            )
        if limit is not None:
            papers = papers[: max(0, int(limit))]
        display_name = str(source_cfg.get("display_name") or source)
        content = render_digest(
            display_name=display_name,
            date=date,
            papers=papers,
        )
        digests.append(
            Digest(
                source=source,
                display_name=display_name,
                date=date,
                papers=tuple(papers),
                content=content,
                files=tuple(used_files),
            )
        )
    return digests


def _parse_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"Invalid --date value {value!r}; expected YYYY-MM-DD"
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print the one-line-per-paper conference digest to the terminal. "
            "This command never sends Discord messages."
        )
    )
    parser.add_argument("--date", default=_today(), help="YYYY-MM-DD (default: today)")
    parser.add_argument("--source", default="", help="Only preview one source")
    parser.add_argument(
        "--limit", type=int, default=None, help="Limit papers per source"
    )
    parser.add_argument(
        "--refresh-oneliners",
        action="store_true",
        help="Regenerate cached one-line summaries",
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="Do not call the model; use extractive fallback only",
    )
    parser.add_argument("--output", default="", help="Write the digest to a file")
    args = parser.parse_args(argv)

    try:
        date = _parse_date(args.date)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    digests = build_digests(
        date,
        refresh=args.refresh_oneliners,
        generate=not args.no_generate,
        limit=args.limit,
    )
    if args.source:
        digests = [digest for digest in digests if digest.source == args.source]
    if not digests:
        print("没有可预览的会议 AI 简报。", file=sys.stderr)
        return 0

    content = "\n---\n\n".join(digest.content.strip() for digest in digests) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    else:
        print(content)
    papers = sum(len(digest.papers) for digest in digests)
    fallback = sum(1 for digest in digests for paper in digest.papers if paper.fallback)
    print(
        f"[conference_digest] sources={len(digests)} papers={papers} "
        f"chars={len(content)} messages={estimated_discord_messages(content)} "
        f"fallback={fallback}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
