#!/usr/bin/env python3
"""Zotero -> NotebookLM daily paper briefing workflow.

This module intentionally does not call the DailyInfo OpenRouter helpers.
NotebookLM is the only summarization layer: Zotero supplies metadata/PDFs,
NotebookLM reads the sources, and the local output directory keeps every
artifact needed to continue manually if automation fails.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import requests

from paths import WORKSPACE_ROOT


ZOTERO_BASE_URL = os.environ.get("ZOTERO_LOCAL_BASE_URL", "http://127.0.0.1:23119")
ZOTERO_LOCAL_USER = "/api/users/0"
ZOTERO_HEADERS = {"Zotero-API-Version": "3"}
PAPER_ITEM_TYPES = {"journalArticle", "conferencePaper", "preprint", "report", "thesis"}
ARTIFACT_CHOICES = {"none", "audio", "video", "both"}


@dataclass
class PdfAttachment:
    key: str
    title: str
    source_path: str | None = None
    copied_path: str | None = None
    status: str = "missing"
    error: str | None = None
    open_attempted: bool = False
    open_target: str | None = None
    open_error: str | None = None


@dataclass
class ZoteroPaper:
    key: str
    item_type: str
    title: str
    creators: list[str]
    date_added: str
    year: str = ""
    venue: str = ""
    doi: str = ""
    url: str = ""
    abstract: str = ""
    tags: list[str] = field(default_factory=list)
    pdfs: list[PdfAttachment] = field(default_factory=list)


@dataclass
class WorkflowPaths:
    output_dir: Path
    pdf_dir: Path
    source_index: Path
    briefing: Path
    status_json: Path
    manual_steps: Path
    prompt_file: Path
    audio: Path
    video: Path


def log(message: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def parse_date(value: str | None) -> dt.date:
    if not value:
        return dt.datetime.now().astimezone().date()
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"--date must be YYYY-MM-DD (got {value!r})") from exc


def _parse_zotero_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone()


def _local_date_from_zotero(value: str) -> dt.date | None:
    parsed = _parse_zotero_datetime(value)
    return parsed.date() if parsed else None


def _zotero_get(path: str, *, timeout: int = 20) -> Any:
    resp = requests.get(
        f"{ZOTERO_BASE_URL}{path}",
        headers=ZOTERO_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    content_type = getattr(resp, "headers", {}).get("Content-Type", "")
    if "application/json" in content_type:
        return resp.json()
    text = resp.text.strip()
    try:
        return resp.json()
    except ValueError:
        return text


def _query(params: dict[str, Any]) -> str:
    from urllib.parse import urlencode

    return urlencode({k: v for k, v in params.items() if v is not None})


def _creators(data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for creator in data.get("creators") or []:
        if creator.get("name"):
            names.append(str(creator["name"]))
            continue
        parts = [creator.get("firstName", ""), creator.get("lastName", "")]
        name = " ".join(p for p in parts if p).strip()
        if name:
            names.append(name)
    return names


def _year(data: dict[str, Any]) -> str:
    raw = str(data.get("date") or "")
    match = re.search(r"(19|20)\d{2}", raw)
    return match.group(0) if match else ""


def _venue(data: dict[str, Any]) -> str:
    for field_name in ("publicationTitle", "conferenceName", "repository", "institution"):
        value = data.get(field_name)
        if value:
            return str(value)
    return ""


def _tags(data: dict[str, Any]) -> list[str]:
    return [str(tag.get("tag")) for tag in data.get("tags") or [] if tag.get("tag")]


def fetch_zotero_collections() -> list[dict[str, str]]:
    collections: list[dict[str, str]] = []
    start = 0
    page_limit = 100
    while True:
        params = _query({"limit": page_limit, "start": start})
        rows = _zotero_get(f"{ZOTERO_LOCAL_USER}/collections?{params}")
        if not rows:
            break
        for row in rows:
            data = row.get("data", row)
            key = str(row.get("key") or data.get("key") or "")
            name = str(data.get("name") or "")
            if key and name:
                collections.append(
                    {
                        "key": key,
                        "name": name,
                        "parent": str(data.get("parentCollection") or ""),
                    }
                )
        if len(rows) < page_limit:
            break
        start += page_limit
    return collections


def resolve_zotero_collection(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    needle = value.strip().casefold()
    for collection in fetch_zotero_collections():
        if collection["key"].casefold() == needle or collection["name"].casefold() == needle:
            return collection
    raise ValueError(f"Zotero collection not found: {value}")


def fetch_zotero_papers_for_date(
    target_date: dt.date,
    *,
    limit: int = 50,
    collection_key: str | None = None,
) -> list[ZoteroPaper]:
    """Fetch top-level Zotero papers whose ``dateAdded`` maps to ``target_date``."""
    papers: list[ZoteroPaper] = []
    start = 0
    page_limit = 100
    items_path = (
        f"{ZOTERO_LOCAL_USER}/collections/{collection_key}/items/top"
        if collection_key
        else f"{ZOTERO_LOCAL_USER}/items/top"
    )

    while True:
        params = _query(
            {
                "limit": page_limit,
                "start": start,
                "sort": "dateAdded",
                "direction": "desc",
            }
        )
        rows = _zotero_get(f"{items_path}?{params}")
        if not rows:
            break

        stop = False
        for item in rows:
            data = item.get("data", item)
            added_date = _local_date_from_zotero(str(data.get("dateAdded") or ""))
            if added_date is None:
                continue
            if added_date > target_date:
                continue
            if added_date < target_date:
                stop = True
                continue

            item_type = str(data.get("itemType") or "")
            if item_type not in PAPER_ITEM_TYPES:
                continue
            papers.append(
                ZoteroPaper(
                    key=str(item.get("key") or data.get("key") or ""),
                    item_type=item_type,
                    title=str(data.get("title") or "(untitled)"),
                    creators=_creators(data),
                    date_added=str(data.get("dateAdded") or ""),
                    year=_year(data),
                    venue=_venue(data),
                    doi=str(data.get("DOI") or ""),
                    url=str(data.get("url") or ""),
                    abstract=str(data.get("abstractNote") or ""),
                    tags=_tags(data),
                )
            )
            if len(papers) >= limit:
                return papers

        if stop or len(rows) < page_limit:
            break
        start += page_limit

    return papers


def fetch_pdf_attachments(item_key: str) -> list[PdfAttachment]:
    children = _zotero_get(f"{ZOTERO_LOCAL_USER}/items/{item_key}/children")
    attachments: list[PdfAttachment] = []
    for child in children or []:
        data = child.get("data", child)
        if data.get("itemType") != "attachment":
            continue
        content_type = str(data.get("contentType") or "").lower()
        title = str(data.get("title") or data.get("filename") or "")
        if "pdf" not in content_type and not title.lower().endswith(".pdf"):
            continue
        key = str(child.get("key") or data.get("key") or "")
        attachments.append(PdfAttachment(key=key, title=title or f"{key}.pdf"))
    return attachments


def _attachment_file_url(attachment_key: str) -> str | None:
    try:
        value = _zotero_get(
            f"{ZOTERO_LOCAL_USER}/items/{attachment_key}/file/view/url",
            timeout=10,
        )
    except Exception:
        return None
    if isinstance(value, str) and value.startswith("file:"):
        return value
    return None


def _file_url_to_path(file_url: str) -> Path:
    parsed = urlparse(file_url)
    raw_path = url2pathname(unquote(parsed.path))
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    return Path(raw_path)


def _safe_filename(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", text, flags=re.UNICODE).strip("._")
    return (cleaned or fallback)[:140]


def _zotero_attachment_uri(attachment_key: str) -> str:
    return f"zotero://open-pdf/library/items/{attachment_key}"


def _try_open_target(target: str | Path) -> str | None:
    value = str(target)
    try:
        if os.name == "nt":
            os.startfile(value)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", value])
        else:
            subprocess.Popen(["xdg-open", value])
        return None
    except Exception as exc:
        return str(exc)


def _try_open_local_file(path: Path) -> str | None:
    return _try_open_target(path)


def _try_open_attachment_for_hydration(attachment: PdfAttachment, source: Path) -> str | None:
    """Open the Zotero attachment first so sync clients can hydrate cloud PDFs."""
    uri = _zotero_attachment_uri(attachment.key)
    attachment.open_target = uri
    uri_error = _try_open_target(uri)
    if uri_error is None:
        return None

    attachment.open_target = f"{uri} ; {source}"
    path_error = _try_open_local_file(source)
    if path_error is None:
        return f"Zotero URI open failed: {uri_error}; local file path opened"
    return f"Zotero URI open failed: {uri_error}; local file path failed: {path_error}"


def _path_access_error(path: Path) -> str | None:
    try:
        return None if path.exists() else f"PDF file not found: {path}"
    except OSError as exc:
        return f"Cannot access PDF file: {path} ({exc})"


def attach_and_copy_pdfs(
    papers: list[ZoteroPaper],
    pdf_dir: Path,
    *,
    open_missing: bool = False,
    open_wait_seconds: int = 20,
) -> None:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    seen_names: set[str] = set()
    for paper in papers:
        try:
            attachments = fetch_pdf_attachments(paper.key)
        except Exception as exc:
            paper.pdfs.append(
                PdfAttachment(
                    key="",
                    title="PDF attachment lookup",
                    status="lookup_failed",
                    error=str(exc),
                )
            )
            continue
        for attachment in attachments:
            file_url = _attachment_file_url(attachment.key)
            if not file_url:
                attachment.status = "missing"
                attachment.error = "Zotero did not return a local file URL"
                paper.pdfs.append(attachment)
                continue
            source = _file_url_to_path(file_url)
            attachment.source_path = str(source)
            error = _path_access_error(source)
            if error and open_missing:
                attachment.open_attempted = True
                attachment.open_error = _try_open_attachment_for_hydration(attachment, source)
                if open_wait_seconds > 0:
                    time.sleep(open_wait_seconds)
                error = _path_access_error(source)
            if error:
                attachment.status = "missing"
                if attachment.open_error:
                    error = f"{error}; open attempt failed: {attachment.open_error}"
                attachment.error = error
                paper.pdfs.append(attachment)
                continue

            base_name = _safe_filename(source.name, f"{attachment.key}.pdf")
            if not base_name.lower().endswith(".pdf"):
                base_name += ".pdf"
            dest_name = base_name
            counter = 2
            while dest_name.casefold() in seen_names:
                stem = Path(base_name).stem
                dest_name = f"{stem}_{counter}.pdf"
                counter += 1
            seen_names.add(dest_name.casefold())
            dest = pdf_dir / dest_name
            try:
                shutil.copy2(source, dest)
                attachment.copied_path = str(dest)
                attachment.status = "copied"
            except OSError as exc:
                if open_missing and not attachment.open_attempted:
                    attachment.open_attempted = True
                    attachment.open_error = _try_open_attachment_for_hydration(attachment, source)
                    if open_wait_seconds > 0:
                        time.sleep(open_wait_seconds)
                    try:
                        shutil.copy2(source, dest)
                        attachment.copied_path = str(dest)
                        attachment.status = "copied"
                        paper.pdfs.append(attachment)
                        continue
                    except OSError as retry_exc:
                        exc = retry_exc
                attachment.status = "copy_failed"
                attachment.error = str(exc)
                if attachment.open_error:
                    attachment.error = f"{attachment.error}; open attempt failed: {attachment.open_error}"
            paper.pdfs.append(attachment)


def make_paths(target_date: dt.date, *, collection_name: str | None = None) -> WorkflowPaths:
    dirname = target_date.isoformat()
    if collection_name:
        dirname = f"{dirname}-{_safe_filename(collection_name, 'collection')}"
    output_dir = WORKSPACE_ROOT / "zotero" / dirname
    return WorkflowPaths(
        output_dir=output_dir,
        pdf_dir=output_dir / "pdfs",
        source_index=output_dir / "source_index.md",
        briefing=output_dir / "briefing.md",
        status_json=output_dir / "notebooklm.json",
        manual_steps=output_dir / "MANUAL_NOTEBOOKLM_STEPS.md",
        prompt_file=output_dir / "briefing_prompt.md",
        audio=output_dir / "audio_overview.mp3",
        video=output_dir / "video_overview.mp4",
    )


def render_source_index(papers: list[ZoteroPaper], target_date: dt.date) -> str:
    lines = [
        f"# Zotero 新增论文索引 - {target_date.isoformat()}",
        "",
        "请基于本 notebook 中上传的 PDF 和本索引，生成面向 AI for Science / 水文科研者的中文论文简报。",
        "",
        "整理要求：",
        "- 不要泛泛复述摘要，优先解释问题、方法、数据、实验结论和局限。",
        "- 标出最值得精读的论文，以及它们对 AI4Science / 水文 / 科学数据处理的潜在启发。",
        "- 若 PDF 与索引信息不一致，以 PDF 内容为准。",
        "- 输出中文 Markdown，结构清晰，避免夸大。",
        "",
        f"共 {len(papers)} 篇论文。",
        "",
    ]
    if not papers:
        lines.append("今天没有匹配到 Zotero 新增论文。")
        return "\n".join(lines) + "\n"

    for idx, paper in enumerate(papers, 1):
        pdf_names = [
            Path(pdf.copied_path).name
            for pdf in paper.pdfs
            if pdf.status == "copied" and pdf.copied_path
        ]
        lines.extend(
            [
                f"## {idx}. {paper.title}",
                "",
                f"- Zotero key: `{paper.key}`",
                f"- 类型: {paper.item_type}",
                f"- 作者: {', '.join(paper.creators) if paper.creators else '未知'}",
                f"- 年份: {paper.year or '未知'}",
                f"- 来源: {paper.venue or '未知'}",
                f"- DOI: {paper.doi or '无'}",
                f"- URL: {paper.url or '无'}",
                f"- 标签: {', '.join(paper.tags) if paper.tags else '无'}",
                f"- 已复制 PDF: {', '.join(pdf_names) if pdf_names else '无'}",
                "",
            ]
        )
        if paper.abstract:
            lines.extend(["摘要：", paper.abstract.strip(), ""])
    return "\n".join(lines).strip() + "\n"


def render_briefing_prompt(target_date: dt.date, papers: list[ZoteroPaper]) -> str:
    return f"""请基于本 NotebookLM notebook 中的 PDF 与 source_index.md，生成一份中文论文简报。

日期：{target_date.isoformat()}
论文数：{len(papers)}

请严格使用 Markdown，并包含以下部分：

## 今日总体判断
用 2-4 句话总结这批论文的共同主题、技术趋势和最值得关注的方向。

## 重点论文速览
逐篇整理：研究问题、核心方法、关键发现、局限或风险、为什么值得关注。

## 值得精读
选出 1-3 篇最值得优先读的论文，说明理由。

## 对 AI for Science / 水文科研的启发
明确指出可能可迁移的方法、数据处理思路、实验设计或工具链启发。

## 后续行动建议
给出 3-6 条具体阅读或实验跟进建议。

要求：
- 只基于已上传 source，不要补充外部信息。
- 如果某篇 PDF 解析不足，请明确说明不确定性。
- 输出中文。
"""


def render_placeholder_briefing(target_date: dt.date, reason: str) -> str:
    return f"# Zotero 论文简报 - {target_date.isoformat()}\n\n{reason}\n"


def render_manual_steps(
    paths: WorkflowPaths,
    artifact: str,
    notebook_title: str,
    errors: list[str] | None = None,
) -> str:
    lines = [
        f"# NotebookLM 手动兜底步骤 - {notebook_title}",
        "",
        "自动化未完成或未启用时，可以按以下步骤继续：",
        "",
        "1. 若尚未授权，先运行 `notebooklm login`，在浏览器里完成 Google 登录。",
        "2. 打开 NotebookLM，并创建一个新 notebook。",
        f"3. 上传索引文件：`{paths.source_index}`。",
        f"4. 上传 PDF 目录下的文件：`{paths.pdf_dir}`。",
        "   若 PDF 目录为空或缺文件，先在 Zotero 打开对应附件等待云盘同步，或重跑本命令并加 `--open-missing-pdfs`。",
        f"5. 将 `{paths.prompt_file}` 中的提示词粘贴到 NotebookLM chat，生成中文简报。",
        f"6. 将生成结果保存为 `{paths.briefing}`。",
    ]
    next_step = 7
    if artifact in {"audio", "both"}:
        lines.append(
            f"{next_step}. 在 NotebookLM Studio 中生成 Audio Overview，并下载到 `audio_overview.mp3`。"
        )
        next_step += 1
    if artifact in {"video", "both"}:
        lines.append(
            f"{next_step}. 在 NotebookLM Studio 中生成 Video Overview，并下载到 `video_overview.mp4`。"
        )
    if errors:
        lines.extend(["", "## 自动化错误", ""])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines).strip() + "\n"


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def write_status(paths: WorkflowPaths, payload: dict[str, Any]) -> None:
    paths.status_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class NotebookLMAutomation:
    """Best-effort NotebookLM automation using the notebooklm-py CLI, then API."""

    def __init__(self, *, timeout: int = 900, notebooklm_home: str | None = None):
        self.timeout = timeout
        self.notebooklm_home = notebooklm_home

    def run(
        self,
        *,
        notebook_title: str,
        paths: WorkflowPaths,
        artifact: str,
    ) -> dict[str, Any]:
        try:
            return self._run_cli(
                notebook_title=notebook_title,
                paths=paths,
                artifact=artifact,
            )
        except Exception as cli_exc:
            cli_error = f"CLI failed: {cli_exc}"
            try:
                result = asyncio.run(
                    self._run_python_api(
                        notebook_title=notebook_title,
                        paths=paths,
                        artifact=artifact,
                    )
                )
                result.setdefault("warnings", []).append(cli_error)
                return result
            except Exception as api_exc:
                return {
                    "ok": False,
                    "mode": "failed",
                    "errors": [cli_error, f"Python API fallback failed: {api_exc}"],
                }

    async def _run_python_api(
        self,
        *,
        notebook_title: str,
        paths: WorkflowPaths,
        artifact: str,
    ) -> dict[str, Any]:
        from notebooklm import NotebookLMClient  # type: ignore

        source_ids: list[str] = []
        artifact_ids: dict[str, str] = {}
        old_home = os.environ.get("NOTEBOOKLM_HOME")
        if self.notebooklm_home:
            os.environ["NOTEBOOKLM_HOME"] = self.notebooklm_home
        try:
            async with NotebookLMClient.from_storage() as client:
                notebook = await client.notebooks.create(notebook_title)
                notebook_id = str(_attr(notebook, "id", ""))

                index_source = await client.sources.add_text(
                    notebook_id,
                    "source_index.md",
                    paths.source_index.read_text(encoding="utf-8"),
                    wait=True,
                    wait_timeout=300,
                )
                source_ids.append(str(_attr(index_source, "id", "")))

                for pdf in sorted(paths.pdf_dir.glob("*.pdf")):
                    source = await client.sources.add_file(
                        notebook_id,
                        pdf,
                        wait=True,
                        wait_timeout=600,
                        title=pdf.name,
                    )
                    source_ids.append(str(_attr(source, "id", "")))

                ask_result = await client.chat.ask(
                    notebook_id,
                    paths.prompt_file.read_text(encoding="utf-8"),
                    source_ids=[sid for sid in source_ids if sid] or None,
                )
                paths.briefing.write_text(str(_attr(ask_result, "answer", "")), encoding="utf-8")

                instructions = "请用中文面向 AI for Science / 水文科研者，聚焦关键论文、方法贡献和后续行动。"
                if artifact in {"audio", "both"}:
                    status = await client.artifacts.generate_audio(
                        notebook_id,
                        source_ids=[sid for sid in source_ids if sid] or None,
                        instructions=instructions,
                        language="zh_Hans",
                    )
                    final = await client.artifacts.wait_for_completion(
                        notebook_id,
                        str(_attr(status, "task_id", "")),
                        timeout=self.timeout,
                        initial_interval=5,
                    )
                    artifact_ids["audio"] = str(_attr(final, "task_id", ""))
                    if bool(_attr(final, "is_complete", False)):
                        await client.artifacts.download_audio(notebook_id, str(paths.audio))

                if artifact in {"video", "both"}:
                    status = await client.artifacts.generate_video(
                        notebook_id,
                        source_ids=[sid for sid in source_ids if sid] or None,
                        instructions=instructions,
                        language="zh_Hans",
                    )
                    final = await client.artifacts.wait_for_completion(
                        notebook_id,
                        str(_attr(status, "task_id", "")),
                        timeout=self.timeout,
                        initial_interval=5,
                    )
                    artifact_ids["video"] = str(_attr(final, "task_id", ""))
                    if bool(_attr(final, "is_complete", False)):
                        await client.artifacts.download_video(notebook_id, str(paths.video))

                return {
                    "ok": True,
                    "mode": "python_api",
                    "notebook_id": notebook_id,
                    "source_ids": source_ids,
                    "artifact_ids": artifact_ids,
                    "errors": [],
                    "warnings": [],
                }
        finally:
            if self.notebooklm_home:
                if old_home is None:
                    os.environ.pop("NOTEBOOKLM_HOME", None)
                else:
                    os.environ["NOTEBOOKLM_HOME"] = old_home

    def _run_cli(
        self,
        *,
        notebook_title: str,
        paths: WorkflowPaths,
        artifact: str,
    ) -> dict[str, Any]:
        executable = shutil.which("notebooklm")
        if not executable:
            raise RuntimeError("notebooklm CLI not found; install notebooklm-py or run manual steps")

        base_env = dict(os.environ)
        if self.notebooklm_home:
            base_env["NOTEBOOKLM_HOME"] = self.notebooklm_home

        created = self._cmd(
            [executable, "create", notebook_title, "--use", "--json"],
            env=base_env,
        )
        notebook_id = _extract_notebook_id(created.stdout)
        env = dict(base_env)
        if notebook_id:
            env["NOTEBOOKLM_NOTEBOOK"] = notebook_id

        source_ids: list[str] = []
        source_ids.extend(self._add_text_source_cli(executable, paths.source_index, env))
        for pdf in sorted(paths.pdf_dir.glob("*.pdf")):
            source_ids.extend(self._add_source_cli(executable, pdf, env))

        warnings: list[str] = []
        for source_id in [sid for sid in source_ids if sid]:
            try:
                self._cmd(
                    [
                        executable,
                        "source",
                        "wait",
                        source_id,
                        "--timeout",
                        "300",
                        "--interval",
                        "2",
                        "--json",
                    ],
                    env=env,
                    timeout=320,
                )
            except Exception as exc:
                warnings.append(f"Source wait failed for {source_id}: {exc}")

        ask = self._cmd(
            [
                executable,
                "ask",
                "--new",
                "--yes",
                "--json",
                "--prompt-file",
                str(paths.prompt_file),
                "--timeout",
                "120",
            ],
            env=env,
            timeout=self.timeout,
        )
        ask_payload = _extract_json_object(ask.stdout)
        if isinstance(ask_payload, dict) and ask_payload.get("answer"):
            paths.briefing.write_text(str(ask_payload["answer"]).strip() + "\n", encoding="utf-8")
        else:
            paths.briefing.write_text(ask.stdout.strip() + "\n", encoding="utf-8")

        artifact_ids: dict[str, str] = {}
        instructions = "请用中文面向 AI for Science / 水文科研者，聚焦关键论文、方法贡献和后续行动。"
        if artifact in {"audio", "both"}:
            out = self._cmd(
                [
                    executable,
                    "generate",
                    "audio",
                    instructions,
                    "--wait",
                    "--timeout",
                    str(self.timeout),
                    "--interval",
                    "5",
                    "--retry",
                    "3",
                    "--language",
                    "zh_Hans",
                    "--json",
                ],
                env=env,
                timeout=self.timeout + 30,
            )
            artifact_ids["audio"] = _extract_artifact_id(out.stdout)
            download_cmd = [executable, "download", "audio", str(paths.audio), "--force", "--json"]
            if artifact_ids["audio"]:
                download_cmd += ["--artifact", artifact_ids["audio"]]
            self._cmd(download_cmd, env=env, timeout=self.timeout)
        if artifact in {"video", "both"}:
            out = self._cmd(
                [
                    executable,
                    "generate",
                    "video",
                    instructions,
                    "--wait",
                    "--timeout",
                    str(max(self.timeout, 600)),
                    "--interval",
                    "5",
                    "--retry",
                    "3",
                    "--language",
                    "zh_Hans",
                    "--json",
                ],
                env=env,
                timeout=max(self.timeout, 600) + 30,
            )
            artifact_ids["video"] = _extract_artifact_id(out.stdout)
            download_cmd = [executable, "download", "video", str(paths.video), "--force", "--json"]
            if artifact_ids["video"]:
                download_cmd += ["--artifact", artifact_ids["video"]]
            self._cmd(download_cmd, env=env, timeout=self.timeout)

        return {
            "ok": True,
            "mode": "cli",
            "notebook_id": notebook_id,
            "source_ids": source_ids,
            "artifact_ids": artifact_ids,
            "errors": [],
            "warnings": warnings,
        }

    def _add_source_cli(self, executable: str, path: Path, env: dict[str, str]) -> list[str]:
        result = self._cmd(
            [
                executable,
                "source",
                "add",
                str(path),
                "--type",
                "file",
                "--title",
                path.name,
                "--timeout",
                "120",
                "--json",
            ],
            env=env,
            timeout=self.timeout,
        )
        source_id = _extract_source_id(result.stdout)
        return [source_id] if source_id else []

    def _add_text_source_cli(self, executable: str, path: Path, env: dict[str, str]) -> list[str]:
        result = self._cmd(
            [
                executable,
                "source",
                "add",
                "-",
                "--type",
                "text",
                "--title",
                path.name,
                "--json",
            ],
            env=env,
            timeout=self.timeout,
            input_text=path.read_text(encoding="utf-8"),
        )
        source_id = _extract_source_id(result.stdout)
        return [source_id] if source_id else []

    @staticmethod
    def _cmd(
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 120,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{' '.join(cmd)} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
            )
        return result


def _extract_json_object(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    match = re.search(r"(\{[\s\S]*\})", stripped)
    if match:
        try:
            return json.loads(match.group(1))
        except ValueError:
            return None
    return None


def _extract_notebook_id(text: str) -> str:
    data = _extract_json_object(text)
    if isinstance(data, dict):
        for key in ("active_notebook_id", "notebook_id", "id"):
            if data.get(key):
                return str(data[key])
        notebook = data.get("notebook")
        if isinstance(notebook, dict) and notebook.get("id"):
            return str(notebook["id"])
    match = re.search(r"\b[a-zA-Z0-9_-]{8,}\b", text)
    return match.group(0) if match else ""


def _extract_source_id(text: str) -> str:
    data = _extract_json_object(text)
    if isinstance(data, dict):
        for key in ("source_id", "id"):
            if data.get(key):
                return str(data[key])
        source = data.get("source")
        if isinstance(source, dict) and source.get("id"):
            return str(source["id"])
    return ""


def _extract_artifact_id(text: str) -> str:
    data = _extract_json_object(text)
    if isinstance(data, dict):
        for key in ("artifact_id", "task_id", "id"):
            if data.get(key):
                return str(data[key])
    return ""


def _papers_payload(papers: list[ZoteroPaper]) -> list[dict[str, Any]]:
    return [asdict(paper) for paper in papers]


def run_zotero_brief(
    *,
    date_str: str | None = None,
    force: bool = False,
    artifact: str = "none",
    manual_only: bool = False,
    limit: int = 50,
    collection: str | None = None,
    open_missing_pdfs: bool = False,
    pdf_wait_seconds: int = 20,
    notebooklm_home: str | None = None,
    notebook_title: str | None = None,
    adapter: NotebookLMAutomation | None = None,
) -> int:
    if artifact not in ARTIFACT_CHOICES:
        raise ValueError(f"artifact must be one of {sorted(ARTIFACT_CHOICES)}")
    target_date = parse_date(date_str)
    collection_info = resolve_zotero_collection(collection) if collection else None
    collection_key = collection_info["key"] if collection_info else None
    collection_name = collection_info["name"] if collection_info else None
    paths = make_paths(target_date, collection_name=collection_name)
    title_suffix = f" {collection_name}" if collection_name else ""
    notebook_title = notebook_title or f"DailyInfo Zotero{title_suffix} Papers {target_date.isoformat()}"

    if paths.briefing.exists() and not force:
        log(f"Zotero briefing already exists: {paths.briefing} (use --force to overwrite)")
        return 0

    paths.output_dir.mkdir(parents=True, exist_ok=True)
    paths.pdf_dir.mkdir(parents=True, exist_ok=True)

    status: dict[str, Any] = {
        "date": target_date.isoformat(),
        "notebook_title": notebook_title,
        "collection": collection_info,
        "output_dir": str(paths.output_dir),
        "artifact": artifact,
        "manual_only": manual_only,
        "open_missing_pdfs": open_missing_pdfs,
        "pdf_wait_seconds": pdf_wait_seconds,
        "notebooklm_home": notebooklm_home or os.environ.get("NOTEBOOKLM_HOME", ""),
        "papers": [],
        "notebooklm": {
            "ok": False,
            "mode": "not_started",
            "notebook_id": "",
            "source_ids": [],
            "artifact_ids": {},
            "errors": [],
            "warnings": [],
        },
    }

    try:
        papers = fetch_zotero_papers_for_date(
            target_date,
            limit=limit,
            collection_key=collection_key,
        )
        attach_and_copy_pdfs(
            papers,
            paths.pdf_dir,
            open_missing=open_missing_pdfs,
            open_wait_seconds=pdf_wait_seconds,
        )
    except Exception as exc:
        status["notebooklm"]["errors"].append(f"Zotero fetch failed: {exc}")
        paths.source_index.write_text(render_source_index([], target_date), encoding="utf-8")
        paths.prompt_file.write_text(render_briefing_prompt(target_date, []), encoding="utf-8")
        paths.briefing.write_text(
            render_placeholder_briefing(target_date, f"Zotero 读取失败：{exc}"),
            encoding="utf-8",
        )
        paths.manual_steps.write_text(
            render_manual_steps(paths, artifact, notebook_title, status["notebooklm"]["errors"]),
            encoding="utf-8",
        )
        write_status(paths, status)
        return 1

    status["papers"] = _papers_payload(papers)
    paths.source_index.write_text(render_source_index(papers, target_date), encoding="utf-8")
    paths.prompt_file.write_text(render_briefing_prompt(target_date, papers), encoding="utf-8")

    if not papers:
        paths.briefing.write_text(
            render_placeholder_briefing(target_date, "今天没有匹配到 Zotero 新增论文。"),
            encoding="utf-8",
        )
        paths.manual_steps.write_text(
            render_manual_steps(paths, artifact, notebook_title),
            encoding="utf-8",
        )
        write_status(paths, status)
        log(f"No Zotero papers found for {target_date.isoformat()}; wrote {paths.briefing}")
        return 0

    if manual_only:
        paths.briefing.write_text(
            render_placeholder_briefing(
                target_date,
                "已生成 NotebookLM 素材包。请按 MANUAL_NOTEBOOKLM_STEPS.md 手动生成简报。",
            ),
            encoding="utf-8",
        )
        paths.manual_steps.write_text(
            render_manual_steps(paths, artifact, notebook_title),
            encoding="utf-8",
        )
        write_status(paths, status)
        log(f"Manual-only Zotero package written: {paths.output_dir}")
        return 0

    automation = adapter or NotebookLMAutomation(notebooklm_home=notebooklm_home)
    notebook_result = automation.run(notebook_title=notebook_title, paths=paths, artifact=artifact)
    status["notebooklm"] = notebook_result
    errors = list(notebook_result.get("errors") or [])
    paths.manual_steps.write_text(
        render_manual_steps(paths, artifact, notebook_title, errors),
        encoding="utf-8",
    )

    if not paths.briefing.exists():
        if errors:
            reason = "NotebookLM 自动化未完成，已生成手动素材包。"
        else:
            reason = "NotebookLM 未返回简报内容，已生成手动素材包。"
        paths.briefing.write_text(render_placeholder_briefing(target_date, reason), encoding="utf-8")

    write_status(paths, status)
    log(f"Zotero NotebookLM package written: {paths.output_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build a Zotero -> NotebookLM paper briefing")
    parser.add_argument("--date", default=None, help="Date to process in YYYY-MM-DD format")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output")
    parser.add_argument("--limit", type=int, default=50, help="Maximum Zotero papers to process")
    parser.add_argument("--collection", default=None, help="Zotero collection name or key, e.g. water")
    parser.add_argument(
        "--artifact",
        choices=sorted(ARTIFACT_CHOICES),
        default="none",
        help="Optional NotebookLM artifact to generate",
    )
    parser.add_argument("--manual-only", action="store_true", help="Only prepare local sources and manual steps")
    parser.add_argument(
        "--open-missing-pdfs",
        action="store_true",
        help="Open inaccessible Zotero PDF attachments once, then retry copying",
    )
    parser.add_argument(
        "--pdf-wait-seconds",
        type=int,
        default=20,
        help="Seconds to wait after opening a Zotero PDF attachment",
    )
    parser.add_argument("--notebooklm-home", default=None, help="NotebookLM profile directory")
    parser.add_argument("--notebook-title", default=None, help="NotebookLM notebook title")
    args = parser.parse_args(argv)

    try:
        return run_zotero_brief(
            date_str=args.date,
            force=args.force,
            artifact=args.artifact,
            manual_only=args.manual_only,
            limit=args.limit,
            collection=args.collection,
            open_missing_pdfs=args.open_missing_pdfs,
            pdf_wait_seconds=args.pdf_wait_seconds,
            notebooklm_home=args.notebooklm_home,
            notebook_title=args.notebook_title,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
